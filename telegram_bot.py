"""
Notifiche e comandi Telegram (opzionali).

Come nei bot fratelli: report a ogni ciclo, allarme in caso di errore e un
piccolo set di comandi. I comandi sono accettati solo dalla chat
configurata in TELEGRAM_CHAT_ID: /run fa partire un ciclo che puo' firmare.
"""

import html
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
SEP = "━━━━━━━━━━━━━━━━━━━━━━"

_run_lock = threading.Lock()


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_telegram_message(text: str, chat_id: Optional[str] = None, parse_mode: str = "HTML") -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    target = chat_id or TELEGRAM_CHAT_ID
    if not target:
        return False
    try:
        resp = requests.post(f"{API_BASE_URL}/sendMessage", json={
            "chat_id": target, "text": text, "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }, timeout=10)
        ok = resp.json().get("ok")
        if not ok:
            logger.warning("[Telegram] errore API: %s", resp.text[:200])
        return bool(ok)
    except Exception as exc:
        logger.warning("[Telegram] invio fallito: %s", exc)
        return False


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _pool_label(result: Dict[str, Any]) -> str:
    return f"{_e(result.get('project'))} {_e(result.get('symbol'))}".strip() or _e(result.get("pool"))


def _mode_line(status: Dict[str, Any]) -> Optional[str]:
    mode = status.get("mode")
    if mode == "paper":
        return f"📝 <i>PAPER — P&amp;L ${status.get('pnl_since_start_usd', 0.0):+.2f}</i>"
    if mode == "dry_run":
        return "🧪 <i>DRY-RUN: nessuna transazione firmata</i>"
    return None


def format_positions(status: Dict[str, Any], limit: int = 6) -> List[str]:
    lines = []
    for p in status.get("positions", [])[:limit]:
        apy = p.get("apy_now")
        apy_txt = f"{apy:.2f}%" if apy is not None else "n/d"
        lines.append(f"   • {_e(p.get('project'))} {_e(p.get('symbol'))}: "
                     f"${p.get('value_usd', 0):.2f} @ {apy_txt}")
    return lines


def notify_cycle_result(decision: Dict[str, Any], result: Dict[str, Any],
                        status: Dict[str, Any], exits: List[Dict[str, Any]] = None):
    if not is_configured():
        return
    op = str(result.get("operation") or decision.get("operation") or "hold").lower()
    res_status = str(result.get("status", "")).lower()

    titles = {
        "deposit": f"🟢 <b>DEPOSITO: {_pool_label(result)}</b>",
        "withdraw": f"🔴 <b>PRELIEVO: {_pool_label(result)}</b>",
        "rebalance": f"🔁 <b>RIBILANCIAMENTO → {_pool_label(result)}</b>",
    }
    lines = ["🌾 <b>Yield Agent • Report Ciclo</b>", SEP,
             titles.get(op, "⏸️ <b>HOLD: nessuna operazione</b>")]
    mode = _mode_line(status)
    if mode:
        lines.append(mode)

    if op != "hold":
        if res_status == "rejected":
            lines.append(f"🛑 <b>Rifiutato dai limiti:</b> {_e(result.get('reason'))}")
        elif res_status == "error":
            lines.append(f"❌ <b>Errore:</b> {_e(result.get('reason'))}")
        else:
            if result.get("amount_usd") is not None:
                lines.append(f"💵 <b>Importo:</b> ${float(result['amount_usd']):.2f}")
            if result.get("apy") is not None:
                lines.append(f"📈 <b>APY effettivo:</b> {float(result['apy']):.2f}%")
            if result.get("apy_gain_points") is not None:
                lines.append(f"📈 <b>Guadagno APY:</b> {float(result['apy_gain_points']):+.2f} punti, "
                             f"si ripaga in {result.get('breakeven_days')} giorni")
            for tx in (result.get("transactions") or []):
                if tx.get("explorer"):
                    lines.append(f"🔗 <a href=\"{tx['explorer']}\">{_e(tx.get('description'))}</a>")

    for ex in exits or []:
        lines.append(f"🚨 Uscita automatica {_e(ex.get('trigger'))}: {_pool_label(ex)} ({_e(ex.get('status'))})")

    lines += ["", f"💰 <b>Portafoglio:</b> ${status.get('total_value_usd', 0):.2f} "
                  f"(inattivo ${status.get('idle_usd', 0):.2f})",
              f"🌾 <b>Investito:</b> ${status.get('invested_usd', 0):.2f} @ "
              f"{status.get('weighted_apy', 0):.2f}% → ~${status.get('est_yearly_usd', 0):.2f}/anno"]
    lines += format_positions(status)
    lines += ["", "🧠 <b>Motivazione AI:</b>", f"<i>{_e(decision.get('reason'))}</i>", SEP]
    send_telegram_message("\n".join(lines))


def notify_error(error_msg: str):
    if not is_configured():
        return
    send_telegram_message(
        f"⚠️ <b>Allarme Yield Agent</b>\n{SEP}\n"
        f"Errore durante il ciclo:\n<code>{_e(error_msg)[:3000]}</code>\n{SEP}\n"
        "<i>Il bot riprovera' al prossimo intervallo.</i>"
    )


# ---------------------------------------------------------------- comandi

def _latest():
    import db_utils
    return db_utils.fetch_dashboard_data(limit=5)


def _handle_command(text: str, chat_id: str):
    cmd = text.strip().split()[0].lower().split("@")[0]

    if cmd in ("/start", "/help"):
        send_telegram_message(
            "🌾 <b>Yield Agent</b>\n\n"
            "• /status - valore, capitale investito, APY medio\n"
            "• /positions - posizioni aperte nei pool\n"
            "• /pools - migliori opportunita' dell'ultima scansione\n"
            "• /last - ultima decisione dell'AI\n"
            "• /run - esegue subito un ciclo\n", chat_id=chat_id)

    elif cmd == "/status":
        data = _latest()
        status = data.get("status")
        if not status:
            send_telegram_message("ℹ️ Nessuno snapshot ancora: il primo ciclo non e' terminato.", chat_id=chat_id)
            return
        lines = ["📊 <b>Stato Yield Agent</b>", SEP]
        mode = _mode_line(status)
        if mode:
            lines.append(mode)
        lines += [f"💰 Totale: ${status['total_value_usd']:.2f}",
                  f"💤 Inattivo: ${status['idle_usd']:.2f}",
                  f"🌾 Investito: ${status['invested_usd']:.2f} @ {status['weighted_apy']:.2f}%",
                  f"📅 Stima annua: ${status['est_yearly_usd']:.2f}",
                  f"🕒 {_e(data.get('snapshot_at'))}", SEP]
        send_telegram_message("\n".join(lines), chat_id=chat_id)

    elif cmd == "/positions":
        status = _latest().get("status") or {}
        rows = format_positions(status, limit=20)
        send_telegram_message("\n".join(["🌾 <b>Posizioni</b>", SEP] + (rows or ["nessuna"])), chat_id=chat_id)

    elif cmd == "/pools":
        pools = [p for p in _latest().get("pools", []) if p.get("status") == "approved"][:8]
        lines = ["🔎 <b>Migliori pool (ultima scansione)</b>", SEP]
        for p in pools:
            flag = "✅" if p.get("adapter") else "👁️"
            lines.append(f"{flag} {_e(p['project'])} {_e(p['symbol'])}: {p['effective_apy']:.2f}% "
                         f"| TVL ${p['tvl_usd'] / 1e6:.1f}M")
        if not pools:
            lines.append("nessun pool approvato")
        lines.append("<i>✅ eseguibile · 👁️ solo monitoraggio</i>")
        send_telegram_message("\n".join(lines), chat_id=chat_id)

    elif cmd == "/last":
        ops = _latest().get("operations", [])
        if not ops:
            send_telegram_message("ℹ️ Nessuna decisione salvata.", chat_id=chat_id)
            return
        o = ops[0]
        send_telegram_message(
            f"🧠 <b>Ultima decisione</b>\n{SEP}\n"
            f"📍 {_e((o.get('operation') or '').upper())} {_e(o.get('project'))} {_e(o.get('symbol'))}\n"
            f"📌 Esito: {_e(o.get('status'))} {_e(o.get('result_reason') or '')}\n"
            f"🕒 {_e(o.get('created_at'))}\n\n<i>{_e(o.get('llm_reason'))}</i>", chat_id=chat_id)

    elif cmd == "/run":
        if not _run_lock.acquire(blocking=False):
            send_telegram_message("⏳ Un ciclo e' gia' in corso.", chat_id=chat_id)
            return
        send_telegram_message("⚡ <b>Avvio ciclo...</b> riceverai il report a breve.", chat_id=chat_id)

        def _run():
            try:
                subprocess.run([sys.executable, "main.py"], check=False)
            finally:
                _run_lock.release()
        threading.Thread(target=_run, daemon=True).start()

    else:
        send_telegram_message("Comando non riconosciuto. Usa /help.", chat_id=chat_id)


def run_telegram_listener():
    if not TELEGRAM_BOT_TOKEN:
        logger.info("[Telegram] TELEGRAM_BOT_TOKEN non impostato: listener disattivato.")
        return
    print("📱 Telegram listener avviato.")
    offset = 0
    while True:
        try:
            resp = requests.get(f"{API_BASE_URL}/getUpdates",
                                params={"offset": offset, "timeout": 25}, timeout=35)
            data = resp.json()
            if not data.get("ok"):
                time.sleep(3)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                text = msg.get("text")
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if not text or not text.startswith("/"):
                    continue
                if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                    logger.warning("[Telegram] comando ignorato da chat non autorizzata %s", chat_id)
                    continue
                try:
                    _handle_command(text, chat_id)
                except Exception as exc:
                    send_telegram_message(f"❌ Errore: {_e(exc)}", chat_id=chat_id)
        except Exception:
            time.sleep(5)


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️ TELEGRAM_BOT_TOKEN non configurato nel .env.")
    else:
        run_telegram_listener()
