"""
Registro delle posizioni aperte nei pool.

On-chain esiste solo il saldo depositato: quanto si e' messo dentro, quando,
a che APY e con che TVL del pool non lo sa nessuno. Serve per calcolare il
rendimento maturato, per l'anti-churn (tempo minimo di permanenza) e per
l'uscita automatica se il TVL del pool crolla dopo l'ingresso.
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import config

logger = logging.getLogger(__name__)


class PositionStore:
    def __init__(self, path: str = None):
        self.path = path or config.POSITIONS_PATH
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.positions = json.load(fh) or {}
        except (OSError, ValueError):
            self.positions = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.positions, fh, indent=2)
        except OSError as exc:
            logger.warning("Impossibile salvare %s: %s", self.path, exc)

    def get(self, pool_id: str) -> Optional[Dict[str, Any]]:
        return self.positions.get(pool_id)

    def record_deposit(self, pool: Dict[str, Any], adapter: Dict[str, Any],
                       amount_usd: float, amount_asset: float):
        now = time.time()
        pos = self.positions.get(pool["pool_id"])
        if pos is None:
            pos = {
                "pool_id": pool["pool_id"],
                "project": pool.get("project"),
                "symbol": pool.get("symbol"),
                "pool_meta": pool.get("pool_meta"),
                "asset": pool.get("asset"),
                "adapter": adapter,
                "deposited_usd": 0.0,
                "deposited_amount": 0.0,
                "opened_at": now,
                "entry_apy": pool.get("effective_apy"),
                "entry_tvl": pool.get("tvl_usd"),
                "is_loop": bool(pool.get("is_loop") or adapter.get("kind") == "aave-v3-loop"),
                "leverage": pool.get("leverage") or adapter.get("leverage"),
                "borrow_asset": pool.get("borrow_asset") or adapter.get("borrow_asset"),
                "health_factor": pool.get("target_health_factor"),
            }
            self.positions[pool["pool_id"]] = pos
        pos["deposited_usd"] = float(pos.get("deposited_usd", 0.0)) + float(amount_usd)
        pos["deposited_amount"] = float(pos.get("deposited_amount", 0.0)) + float(amount_asset)
        pos["updated_at"] = now
        self.save()
        return pos

    def record_withdraw(self, pool_id: str, fraction: float):
        """Riduce il costo di carico in proporzione; a 1.0 chiude la posizione."""
        pos = self.positions.get(pool_id)
        if not pos:
            return
        if fraction >= 0.999:
            del self.positions[pool_id]
        else:
            keep = 1.0 - max(0.0, fraction)
            pos["deposited_usd"] = float(pos.get("deposited_usd", 0.0)) * keep
            pos["deposited_amount"] = float(pos.get("deposited_amount", 0.0)) * keep
            pos["updated_at"] = time.time()
        self.save()

    def adopt(self, pool: Dict[str, Any], adapter: Dict[str, Any], value_usd: float, amount: float):
        """Posizione trovata on-chain ma non nel registro (deposito manuale)."""
        pos = self.record_deposit(pool, adapter, value_usd, amount)
        pos["adopted"] = True
        self.save()
        return pos
