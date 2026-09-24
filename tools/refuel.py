"""
CLI tool per verificare lo stato del wallet e convertire l'ETH in eccesso in USDC.

Uso:
  python tools/refuel.py                # Controlla saldi ed esegue il refuel se necessario
  python tools/refuel.py --status       # Mostra solo i saldi correnti
  python tools/refuel.py --force        # Esegue il refuel anche se USDC e' sopra la soglia
  python tools/refuel.py --amount 0.01  # Swappa una specifica quantita' di ETH in USDC
  python tools/refuel.py --dry-run      # Simula lo swap senza inviare transazioni
"""

import argparse
import os
import sys

# Assicura import da radice
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from base_client import BaseClient
from uniswap import UniswapV3

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def get_wallet_balances(client: BaseClient) -> dict:
    eth_bal = client.eth_balance()
    weth_bal = client.balance_of_float(config.WETH)
    usdc_bal = client.balance_of_float(config.USDC)
    gas_reserve = getattr(config, "ETH_GAS_RESERVE", 0.003)
    min_swap_eth = getattr(config, "MIN_ETH_SWAP_AMOUNT", 0.002)
    threshold_usdc = getattr(config, "USDC_AUTO_SWAP_THRESHOLD", 5.0)

    swappable_native = max(0.0, eth_bal - gas_reserve)
    total_swappable = swappable_native + weth_bal

    return {
        "wallet": client.address,
        "eth_balance": eth_bal,
        "weth_balance": weth_bal,
        "usdc_balance": usdc_bal,
        "gas_reserve": gas_reserve,
        "min_swap_eth": min_swap_eth,
        "threshold_usdc": threshold_usdc,
        "swappable_native_eth": swappable_native,
        "total_swappable_eth": total_swappable,
        "needs_refuel": usdc_bal < threshold_usdc and total_swappable >= min_swap_eth,
    }


def display_balances(info: dict):
    print("=" * 60)
    print("[REFUEL] STATO RIFORNIMENTO WALLET (ETH -> USDC)")
    print("=" * 60)
    print(f"Indirizzo wallet:       {info['wallet']}")
    print(f"ETH Nativo:            {info['eth_balance']:.6f} ETH")
    print(f"WETH:                  {info['weth_balance']:.6f} WETH")
    print(f"USDC:                  ${info['usdc_balance']:.2f}")
    print("-" * 60)
    print(f"Riserva Gas (min ETH): {info['gas_reserve']:.4f} ETH")
    print(f"Soglia minima swap:    {info['min_swap_eth']:.4f} ETH")
    print(f"Soglia USDC refuel:    ${info['threshold_usdc']:.2f}")
    print(f"ETH Spendibile:        {info['total_swappable_eth']:.6f} ETH (preserva riserva gas)")
    print("-" * 60)
    if info["needs_refuel"]:
        print(f"STATUS: [ATTENZIONE] Refuel necessario! (USDC ${info['usdc_balance']:.2f} < ${info['threshold_usdc']:.2f})")
    elif info["usdc_balance"] >= info["threshold_usdc"]:
        print(f"STATUS: [OK] Saldo USDC sufficiente (${info['usdc_balance']:.2f} >= ${info['threshold_usdc']:.2f})")
    else:
        print(f"STATUS: [INFO] ETH spendibile ({info['total_swappable_eth']:.6f}) < minimo ({info['min_swap_eth']:.4f})")
    print("=" * 60)


def run_refuel_cli(client: BaseClient = None, args: list = None):
    parser = argparse.ArgumentParser(description="Auto-refuel USDC da ETH")
    parser.add_argument("--address", type=str, default=None, help="Indirizzo wallet da controllare")
    parser.add_argument("--status", action="store_true", help="Mostra solo i saldi")
    parser.add_argument("--force", action="store_true", help="Forza il refuel anche se USDC e' sufficiente")
    parser.add_argument("--amount", type=float, default=None, help="Importo ETH esatto da swappare in USDC")
    parser.add_argument("--dry-run", action="store_true", help="Simula senza inviare transazioni on-chain")
    opts = parser.parse_args(args if args is not None else sys.argv[1:])

    if opts.dry_run:
        config.DRY_RUN = True

    if client is None:
        client = BaseClient(address=opts.address)
        if not client.is_connected():
            print(f"❌ Impossibile connettersi all'RPC: {config.BASE_RPC_URL}")
            sys.exit(1)

    if not client.address:
        print("❌ Nessun indirizzo wallet configurato (imposta WALLET_ADDRESS nel .env oppure passa --address 0x...)")
        sys.exit(1)

    info = get_wallet_balances(client)
    display_balances(info)

    if opts.status:
        return

    swap_amount = opts.amount
    if swap_amount is None:
        if opts.force or info["needs_refuel"]:
            swap_amount = info["total_swappable_eth"]
        else:
            print("Nessun refuel richiesto.")
            return

    if swap_amount <= 0:
        print("[ERRORE] Importo da swappare nullo o negativo.")
        return

    uniswap = UniswapV3(client)
    print(f"\n[SWAP] Avvio swap di {swap_amount:.5f} ETH/WETH in USDC (DRY_RUN={config.DRY_RUN})...")
    try:
        res = uniswap.swap_eth_to_usdc(swap_amount)
        print("[OK] Swap completato con successo!")
        if res.get("wrap_tx"):
            print(f"   Wrap TX: {res['wrap_tx']}")
        if res.get("tx_hash"):
            print(f"   Swap TX: {res['tx_hash']}")
        print(f"   Dettagli: {res.get('description', '')}")

        # Mostra nuovi saldi
        new_info = get_wallet_balances(client)
        print("\nNuovo stato wallet:")
        print(f"   ETH:  {new_info['eth_balance']:.6f} ETH")
        print(f"   WETH: {new_info['weth_balance']:.6f} WETH")
        print(f"   USDC: ${new_info['usdc_balance']:.2f}")
    except Exception as exc:
        print(f"[ERRORE] Errore durante il refuel: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    run_refuel_cli()
