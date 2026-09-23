"""
Paper trading: portafoglio finto, rendimenti veri.

I depositi vengono eseguiti contro un saldo virtuale e ogni ciclo la
posizione matura l'APY *corrente* del pool secondo DefiLlama (base pieno,
ricompense pesate con REWARD_APY_WEIGHT come nel confronto fra pool).
Cosi' si vede la strategia di rotazione lavorare senza capitale sul wallet.

Semplificazioni dichiarate: le posizioni sono tenute in USD, quindi per un
vault su WETH o cbBTC si simula il rendimento ma non l'oscillazione di
prezzo dell'asset; gli swap costano lo slippage di default e il gas e'
forfettario (PAPER_GAS_USD per transazione).
"""

import json
import logging
import os
import time
from typing import Any, Dict

import config

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = 31_536_000


class PaperBook:
    def __init__(self, path: str = None, start_usdc: float = None, start_eth: float = None):
        self.path = path or config.PAPER_STATE_PATH
        self.start_usdc = config.PAPER_START_USDC if start_usdc is None else start_usdc
        self.start_eth = config.PAPER_START_ETH if start_eth is None else start_eth
        self.state: Dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------ persistenza
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.state = json.load(fh) or {}
        except (OSError, ValueError):
            self.state = {}
        if not self.state:
            self.state = {
                "usdc": float(self.start_usdc),
                "eth": float(self.start_eth),
                "initial_usdc": float(self.start_usdc),
                "positions": {},       # pool_id -> {value_usd, last_accrual, asset}
                "created_at": time.time(),
                "gas_spent_usd": 0.0,
                "yield_earned_usd": 0.0,
                "operations": 0,
            }
            self.save()

    def save(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, indent=2)
        except OSError as exc:
            logger.warning("Impossibile salvare %s: %s", self.path, exc)

    # ------------------------------------------------------------ saldi
    @property
    def usdc(self) -> float:
        return float(self.state.get("usdc", 0.0))

    @property
    def eth(self) -> float:
        return float(self.state.get("eth", 0.0))

    @property
    def positions(self) -> Dict[str, Dict[str, Any]]:
        return self.state.setdefault("positions", {})

    def value_of(self, pool_id: str) -> float:
        return float(self.positions.get(pool_id, {}).get("value_usd", 0.0))

    # ------------------------------------------------------------ maturazione
    def accrue(self, apy_by_pool: Dict[str, float], now: float = None) -> float:
        """
        Fa maturare ogni posizione dall'ultimo aggiornamento, all'APY
        corrente del pool. Restituisce il rendimento maturato in questo passo.
        """
        now = now or time.time()
        earned = 0.0
        for pool_id, pos in self.positions.items():
            apy = apy_by_pool.get(pool_id)
            last = float(pos.get("last_accrual", now))
            dt = max(0.0, now - last)
            if apy is not None and dt > 0:
                growth = (1 + max(apy, 0.0) / 100) ** (dt / SECONDS_PER_YEAR) - 1
                gain = float(pos["value_usd"]) * growth
                pos["value_usd"] = float(pos["value_usd"]) + gain
                earned += gain
                pos["last_apy"] = apy
            pos["last_accrual"] = now
        self.state["yield_earned_usd"] = float(self.state.get("yield_earned_usd", 0.0)) + earned
        self.save()
        return earned

    # ------------------------------------------------------------ operazioni
    def _charge(self, n_tx: int, swap_usd: float = 0.0) -> float:
        gas = config.PAPER_GAS_USD * n_tx
        slip = swap_usd * config.DEFAULT_SLIPPAGE_BPS / 10_000
        self.state["gas_spent_usd"] = float(self.state.get("gas_spent_usd", 0.0)) + gas
        return gas + slip

    def deposit(self, pool: Dict[str, Any], amount_usd: float) -> Dict[str, Any]:
        amount_usd = min(amount_usd, self.usdc)
        needs_swap = pool.get("asset") != "USDC"
        n_tx = 2 + (2 if needs_swap else 0)            # approve+deposit, approve+swap
        cost = self._charge(n_tx, amount_usd if needs_swap else 0.0)
        net = max(0.0, amount_usd - cost)
        self.state["usdc"] = self.usdc - amount_usd
        pos = self.positions.setdefault(pool["pool_id"], {
            "value_usd": 0.0, "asset": pool.get("asset"), "last_accrual": time.time(),
        })
        pos["value_usd"] = float(pos["value_usd"]) + net
        self.state["operations"] = int(self.state.get("operations", 0)) + 1
        self.save()
        return {"deposited_usd": net, "cost_usd": cost}

    def withdraw(self, pool_id: str, fraction: float) -> Dict[str, Any]:
        pos = self.positions.get(pool_id)
        if not pos:
            return {"withdrawn_usd": 0.0, "cost_usd": 0.0}
        fraction = max(0.0, min(1.0, fraction))
        gross = float(pos["value_usd"]) * fraction
        needs_swap = pos.get("asset") != "USDC"
        cost = self._charge(1 + (2 if needs_swap else 0), gross if needs_swap else 0.0)
        net = max(0.0, gross - cost)
        if fraction >= 0.999:
            del self.positions[pool_id]
        else:
            pos["value_usd"] = float(pos["value_usd"]) - gross
        self.state["usdc"] = self.usdc + net
        self.state["operations"] = int(self.state.get("operations", 0)) + 1
        self.save()
        return {"withdrawn_usd": net, "cost_usd": cost}

    def summary(self) -> Dict[str, Any]:
        invested = sum(float(p["value_usd"]) for p in self.positions.values())
        return {
            "initial_usdc": float(self.state.get("initial_usdc", self.start_usdc)),
            "yield_earned_usd": float(self.state.get("yield_earned_usd", 0.0)),
            "gas_spent_usd": float(self.state.get("gas_spent_usd", 0.0)),
            "operations": int(self.state.get("operations", 0)),
            "invested_usd": invested,
            "created_at": self.state.get("created_at"),
        }
