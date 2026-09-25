"""
Un ciclo dell'agente yield: scoperta, riconoscimento on-chain, stato,
uscite di rischio, decisione, esecuzione.

Fratello dei bot perp (SynFutures) e spot (degen): stessa struttura, stesso
ciclo decisionale, ma qui non si specula sul prezzo. Si cerca il miglior
rendimento passivo su Base e ci si sposta solo quando conviene davvero.
"""

import json
import os
import sys

import config
import db_utils
import yield_scanner
from base_client import BaseClient
from yield_agent import OPENROUTER_MODEL, decide_allocation
from yield_manager import YieldManager


def _positions_for_prompt(status):
    lines = []
    for p in status["positions"]:
        loop_str = ""
        if p.get("is_loop"):
            hf_str = f", HF {p['health_factor']:.2f}" if p.get("health_factor") is not None else ""
            loop_str = f" | LOOP {p.get('leverage', 1.0):.1f}x{hf_str}"
        lines.append(
            f"- pool_id={p['pool_id']} | {p['project']} {p['symbol']}"
            f"{' (' + p['pool_meta'] + ')' if p.get('pool_meta') else ''}{loop_str} | "
            f"valore ${p['value_usd']:.2f} | APY ora "
            f"{p['apy_now'] if p['apy_now'] is not None else 'n/d'}% (ingresso {p.get('entry_apy')}%) | "
            f"TVL ${p['tvl_now'] or 0:,.0f} | aperta da {p['held_hours']:.0f}h | "
            f"prelevabile {'$%.2f' % p['withdrawable_usd'] if p.get('withdrawable_usd') is not None else 'n/d'}"
        )
    return "\n".join(lines) or "nessuna posizione aperta"


def run_cycle():
    if db_utils.is_bot_paused():
        pinfo = db_utils.get_pause_info()
        print(f"⏸️ Bot Yield in stato di PAUSA ({pinfo.get('reason', 'Pausa attiva')}). Ciclo ignorato.")
        return None

    print(f"🚀 Avvio Yield Agent su Base (wallet: {config.WALLET_ADDRESS})")
    if config.PAPER_TRADING:
        print(f"📝 PAPER: portafoglio virtuale, rendimenti reali dei pool. "
              f"Capitale iniziale ${config.PAPER_START_USDC:.2f}.")
    elif config.DRY_RUN:
        print("🧪 DRY-RUN attivo: nessuna transazione verra' firmata.")

    client = BaseClient()
    if not client.is_connected():
        raise RuntimeError(f"RPC Base non raggiungibile: {config.BASE_RPC_URL}")
    problems = client.verify_contracts()
    if problems:
        raise RuntimeError("Verifica dei contratti fallita:\n  - " + "\n  - ".join(problems))
    print(f"⛓️  Connesso a Base (chain {client.chain_id()}), gas {client.gas_price_gwei():.4f} gwei")

    manager = YieldManager(client)

    # 0. Auto-refuel USDC se il saldo e' sotto soglia ma c'e' ETH disponibile
    try:
        refuel_res = manager.ensure_usdc_balance()
        if refuel_res:
            print(f"⛽ Auto-refuel completato: {refuel_res.get('description', '')}")
    except Exception as exc:
        print(f"⚠️  Auto-refuel non riuscito (proseguo con saldo attuale): {exc}")

    # 1. Scoperta e screening
    print("🔎 Scansione dei pool su Base (DefiLlama Yields)...")
    universe = yield_scanner.get_universe()
    rejected = yield_scanner.get_last_rejected()
    print(f"   {len(universe)} pool approvati, {len(rejected)} scartati")

    # 2. Riconoscimento on-chain: solo i contratti verificati sono eseguibili
    print("🧩 Riconoscimento on-chain dei contratti...")
    manager.enrich_universe(universe)
    executable = [p for p in universe if p.get("adapter")]
    print(f"   {len(executable)} pool eseguibili su {len(universe)}")
    try:
        db_utils.log_pool_scan(universe + rejected)
    except Exception as db_err:
        print(f"[db_utils] scansione non salvata: {db_err}")

    # 3. Stato del portafoglio
    print("👛 Lettura del portafoglio...")
    status = manager.get_account_status()
    print(f"   Valore totale ${status['total_value_usd']:.2f} | inattivo ${status['idle_usd']:.2f} | "
          f"investito ${status['invested_usd']:.2f} @ {status['weighted_apy']:.2f}% APY")

    # 4. Uscite di rischio, prima di sentire il modello
    exit_results = []
    for pos in manager.risk_exits(status):
        print(f"🚨 {pos['trigger']} su {pos['project']} {pos['symbol']}: uscita dal pool")
        if config.AUTO_RISK_EXITS:
            result = manager.liquidate(pos, pos["trigger"])
            exit_results.append(result)
            try:
                db_utils.log_operation({"operation": "withdraw", "pool": pos["pool_id"],
                                        "reason": f"uscita automatica {pos['trigger']}"}, result)
            except Exception as db_err:
                print(f"[db_utils] uscita non salvata: {db_err}")
    if exit_results:
        status = manager.get_account_status()

    try:
        db_utils.log_snapshot(status)
    except Exception as db_err:
        print(f"[db_utils] snapshot non salvato: {db_err}")

    # 5. Decisione
    context = (
        f"<universo_pool>\n{yield_scanner.format_universe_for_prompt(universe)}\n</universo_pool>\n\n"
        f"<posizioni>\n{_positions_for_prompt(status)}\n</posizioni>\n\n"
        f"<limiti_esecutore>\n"
        f"max per pool {config.MAX_POOL_PCT:.0%}, max per protocollo {config.MAX_PROTOCOL_PCT:.0%}, "
        f"max {config.MAX_POSITIONS} posizioni, deposito ${config.MIN_DEPOSIT_USD:.0f}-${config.MAX_DEPOSIT_USD:.0f}, "
        f"ribilanciamento solo con +{config.MIN_APY_IMPROVEMENT:.2f} punti di APY, dopo "
        f"{config.MIN_HOLD_HOURS:.0f}h e se si ripaga entro {config.MAX_BREAKEVEN_DAYS:.0f} giorni; "
        f"looping: leva max {config.LOOP_MAX_LEVERAGE:.1f}x, uscita automatica se Health Factor < {config.LOOP_MIN_HEALTH_FACTOR:.2f}\n"
        f"</limiti_esecutore>\n"
    )
    portfolio_data = (
        f"{json.dumps({k: v for k, v in status.items() if k != 'positions'}, default=str)}\n"
        f"Uscite automatiche in questo ciclo: "
        f"{json.dumps(exit_results, default=str) if exit_results else 'nessuna'}"
    )
    with open("system_prompt.txt", encoding="utf-8") as f:
        system_prompt = f.read().format(portfolio_data, context)

    print(f"🤖 L'agente AI (OpenRouter {OPENROUTER_MODEL}) sta decidendo...")
    decision = decide_allocation(system_prompt)
    print(f"   Decisione: {json.dumps(decision, indent=2)}")

    # 6. Esecuzione
    print("⚡ Esecuzione...")
    result = manager.execute_signal(decision, status)
    print(f"   Risultato: {json.dumps(result, default=str)}")
    try:
        db_utils.log_operation(decision, result, system_prompt=system_prompt)
    except Exception as db_err:
        print(f"[db_utils] operazione non salvata: {db_err}")

    if result.get("status") not in ("hold", "rejected"):
        status = manager.get_account_status()
        try:
            db_utils.log_snapshot(status)
        except Exception:
            pass

    try:
        from telegram_bot import notify_cycle_result
        notify_cycle_result(decision, result, status, exit_results)
    except Exception as tg_err:
        print(f"[telegram] notifica non inviata: {tg_err}")

    print("✅ Ciclo completato.")
    return status


if __name__ == "__main__":
    if "--refuel" in sys.argv:
        from tools.refuel import run_refuel_cli
        from base_client import BaseClient
        client = BaseClient()
        run_refuel_cli(client)
        sys.exit(0)

    if not config.WALLET_ADDRESS:
        raise RuntimeError("WALLET_ADDRESS mancante nel .env")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY mancante nel .env")

    try:
        run_cycle()
    except Exception as exc:
        try:
            from telegram_bot import notify_error
            notify_error(str(exc))
        except Exception:
            pass
        try:
            db_utils.log_error(exc, context={"wallet": config.WALLET_ADDRESS})
        except Exception:
            pass
        print(f"❌ Si è verificato un errore: {exc}")
        sys.exit(1)
