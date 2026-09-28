"""
Gestione delle posizioni di rendimento: stato del portafoglio, limiti di
rischio, esecuzione di depositi, prelievi e ribilanciamenti.

Il modello propone, l'esecutore dispone: ogni limite (quota per pool e per
protocollo, numero di posizioni, anti-churn, convenienza del
ribilanciamento, riserva di gas) e' applicato qui e non si aggira dal prompt.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import config
import looping
import protocols
import yield_scanner
from base_client import BaseChainError, BaseClient
from paper import PaperBook
from positions import PositionStore

logger = logging.getLogger(__name__)

ADAPTER_CLASSES = {
    "aave-v3": protocols.AaveV3Adapter,
    "erc4626": protocols.ERC4626Adapter,
    "compound-v3": protocols.CometAdapter,
    "ctoken": protocols.CTokenAdapter,
}


class RiskRejection(RuntimeError):
    """Il segnale viola un limite dell'esecutore."""


def _adapter_from_dict(client: BaseClient, data: Dict[str, Any]) -> protocols.Adapter:
    kind = data["kind"]
    if kind == "aave-v3":
        return protocols.AaveV3Adapter(client, data["asset"])
    return ADAPTER_CLASSES[kind](client, data["address"], data["asset"])


class YieldManager:
    def __init__(self, client: BaseClient):
        self.client = client
        self.paper = PaperBook() if config.PAPER_TRADING else None
        # registro separato in paper: le posizioni finte non devono mai
        # confondersi con quelle vere quando si passa in live
        self.store = PositionStore(config.PAPER_POSITIONS_PATH if self.paper else None)
        self.adapters: Dict[str, protocols.Adapter] = {}
        self.universe: List[Dict[str, Any]] = []
        self._prices: Optional[Dict[str, float]] = None
        self._uniswap = None
        self.loops = looping.LoopManager(self) if config.LOOP_ENABLED else None

    # ------------------------------------------------------------ prezzi
    @property
    def uniswap(self):
        if self._uniswap is None:
            from uniswap import UniswapV3
            self._uniswap = UniswapV3(self.client)
        return self._uniswap

    def prices(self) -> Dict[str, float]:
        """Prezzo in USD degli asset gestiti (USDC = 1 per definizione)."""
        if self._prices is None:
            prices = {"USDC": 1.0}
            # anche gli asset delle posizioni gia' aperte: se DEPOSIT_ASSETS
            # e' stato ristretto dopo, vanno comunque valorizzate
            held = {p.get("asset") for p in self.store.positions.values()}
            if getattr(self, "loops", None):
                held.add("WETH")   # i loop si valutano in ETH
            for sym, meta in config.KNOWN_ASSETS.items():
                if sym == "USDC" or (sym not in config.DEPOSIT_ASSETS and sym not in held):
                    continue
                try:
                    # quota piccola: il prezzo non deve risentire della profondita' del pool
                    price = self.uniswap.price_in_quote(meta["address"], config.USDC, probe_units=0.01)
                    if price:
                        prices[sym] = price
                except Exception as exc:
                    logger.warning("Prezzo %s non disponibile: %s", sym, exc)
            self._prices = prices
        return self._prices

    # ------------------------------------------------------------ universo
    def enrich_universe(self, universe: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Riconosce on-chain il contratto di ogni pool: solo quelli diventano eseguibili."""
        prices = self.prices()
        for pool in universe:
            adapter, reason = protocols.resolve(self.client, pool, prices)
            if adapter:
                self.adapters[pool["pool_id"]] = adapter
                pool["adapter"] = adapter.kind
                pool["contract"] = adapter.address
                onchain = adapter.onchain_apy()
                if onchain is not None:
                    pool["onchain_apy"] = round(onchain, 4)
            else:
                pool["adapter"] = None
                pool["adapter_reason"] = reason
        if getattr(self, "loops", None):
            try:
                self.loops.refresh()
                universe.extend(self.loops.pseudo_pools())
            except Exception as exc:
                logger.warning("Mercati di looping non disponibili: %s", exc)
        self.universe = universe
        return universe

    def _pool_in_universe(self, pool_id: str) -> Optional[Dict[str, Any]]:
        pool_id = str(pool_id or "").strip()
        for pool in self.universe:
            if pool["pool_id"] == pool_id or pool_id.lower() in (pool.get("addresses") or []):
                return pool
        return None

    def _adapter_for_position(self, pos: Dict[str, Any]) -> Optional[protocols.Adapter]:
        pid = pos["pool_id"]
        if pid not in self.adapters:
            try:
                self.adapters[pid] = _adapter_from_dict(self.client, pos["adapter"])
            except Exception as exc:
                logger.warning("Adapter per %s non ricostruibile: %s", pid, exc)
                return None
        return self.adapters[pid]

    # ------------------------------------------------------------ stato
    def get_account_status(self) -> Dict[str, Any]:
        prices = self.prices()
        held_ids = list(self.paper.positions) if self.paper else list(self.store.positions)
        current = yield_scanner.pools_by_id(held_ids)
        for pool in self.universe:
            current.setdefault(pool["pool_id"], pool)

        positions: List[Dict[str, Any]] = []
        loops = getattr(self, "loops", None)
        if self.paper:
            self.paper.accrue({pid: current[pid]["effective_apy"] for pid in held_ids if pid in current})
            if loops:
                loops.accrue_paper()
            usdc = self.paper.usdc
            eth = self.paper.eth
            idle_assets: Dict[str, float] = {}
            for pid, ppos in self.paper.positions.items():
                meta = self.store.get(pid) or {}
                if looping.is_loop(pid):
                    row = loops.position_row(pid, meta) if loops else None
                    if row is None:
                        row = self._position_row(pid, meta, None, ppos["value_usd"], None)
                    positions.append(row)
                    continue
                positions.append(self._position_row(pid, meta, current.get(pid), ppos["value_usd"], None))
        else:
            usdc = self.client.balance_of_float(config.USDC)
            eth = self.client.eth_balance()
            idle_assets = {}
            for sym in config.DEPOSIT_ASSETS:
                if sym == "USDC":
                    continue
                amount = self.client.balance_of_float(config.KNOWN_ASSETS[sym]["address"])
                if amount * prices.get(sym, 0.0) >= config.DUST_USD:
                    idle_assets[sym] = amount

            # posizioni nel registro
            for pid, meta in list(self.store.positions.items()):
                if looping.is_loop(pid):
                    row = None
                    if loops:
                        try:
                            row = loops.position_row(pid, meta)
                        except Exception as exc:
                            logger.warning("Loop %s non leggibile: %s", pid, exc)
                    if row is None:
                        logger.warning("Loop %s nel registro ma LOOP_ENABLED e' spento o non leggibile", pid)
                    elif row["value_usd"] < config.DUST_USD and row["loop"]["debt_usd"] <= 0:
                        self.store.record_withdraw(pid, 1.0)
                    else:
                        positions.append(row)
                    continue
                adapter = self._adapter_for_position(meta)
                if not adapter:
                    continue
                asset = meta["asset"]
                amount = protocols.raw_to_float(adapter.balance_raw(), asset)
                if asset not in prices:
                    logger.warning("Prezzo di %s assente: posizione %s non valorizzata", asset, pid)
                value = amount * prices.get(asset, 0.0)
                if asset in prices and value < config.DUST_USD:
                    # ritirata fuori dal bot: il registro si allinea alla chain
                    self.store.record_withdraw(pid, 1.0)
                    continue
                withdrawable = protocols.raw_to_float(adapter.withdrawable_raw(), asset) * prices.get(asset, 0.0)
                positions.append(self._position_row(pid, meta, current.get(pid), value, withdrawable))

            # depositi fatti a mano nei pool dell'universo: li adottiamo
            for pool in self.universe:
                pid = pool["pool_id"]
                adapter = self.adapters.get(pid)
                if not adapter or pid in self.store.positions:
                    continue
                try:
                    amount = protocols.raw_to_float(adapter.balance_raw(), pool["asset"])
                except Exception:
                    continue
                value = amount * prices.get(pool["asset"], 0.0)
                if value >= config.DUST_USD:
                    meta = self.store.adopt(pool, adapter.to_dict(), value, amount)
                    positions.append(self._position_row(pid, meta, pool, value, None))

        idle_other_usd = sum(a * prices.get(s, 0.0) for s, a in idle_assets.items())
        invested = sum(p["value_usd"] for p in positions)
        total = usdc + idle_other_usd + invested
        weighted_apy = (
            sum(p["value_usd"] * (p["apy_now"] or 0.0) for p in positions) / invested
            if invested > 0 else 0.0
        )
        status = {
            "wallet": self.client.address,
            "mode": "paper" if self.paper else ("dry_run" if config.DRY_RUN else "live"),
            "usdc_balance": round(usdc, 6),
            "eth_balance": round(eth, 8),
            "idle_assets": idle_assets,
            "idle_usd": round(usdc + idle_other_usd, 4),
            "invested_usd": round(invested, 4),
            "total_value_usd": round(total, 4),
            "weighted_apy": round(weighted_apy, 4),
            "est_yearly_usd": round(invested * weighted_apy / 100, 4),
            "positions": positions,
            "prices": prices,
        }
        if loops:
            status["loops"] = {
                "enabled": True,
                "max_leverage": config.LOOP_MAX_LEVERAGE,
                "min_health_factor": config.LOOP_MIN_HEALTH_FACTOR,
                "markets": list(loops.markets.values()),
            }
        if self.paper:
            summary = self.paper.summary()
            status["paper"] = summary
            status["pnl_since_start_usd"] = round(total - summary["initial_usdc"], 4)
        return status

    def _position_row(self, pid: str, meta: Dict[str, Any], pool: Optional[Dict[str, Any]],
                      value_usd: float, withdrawable_usd: Optional[float]) -> Dict[str, Any]:
        pool = pool or {}
        opened = meta.get("opened_at") or time.time()
        deposited = float(meta.get("deposited_usd") or 0.0)
        return {
            "pool_id": pid,
            "project": meta.get("project") or pool.get("project"),
            "symbol": meta.get("symbol") or pool.get("symbol"),
            "pool_meta": meta.get("pool_meta") or pool.get("pool_meta"),
            "asset": meta.get("asset") or pool.get("asset"),
            "value_usd": round(float(value_usd), 4),
            "deposited_usd": round(deposited, 4),
            "pnl_usd": round(float(value_usd) - deposited, 4) if deposited else None,
            "apy_now": pool.get("effective_apy"),
            "entry_apy": meta.get("entry_apy"),
            "tvl_now": pool.get("tvl_usd"),
            "entry_tvl": meta.get("entry_tvl"),
            "held_hours": round((time.time() - opened) / 3600, 2),
            "withdrawable_usd": round(withdrawable_usd, 4) if withdrawable_usd is not None else None,
            "in_universe": self._pool_in_universe(pid) is not None,
        }

    # ------------------------------------------------------------ uscite di rischio
    def risk_exits(self, status: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Posizioni da chiudere senza chiedere al modello."""
        exits = []
        for pos in status["positions"]:
            trigger = None
            meta = self.store.get(pos["pool_id"]) or {}
            keys = {pos["pool_id"].lower(), str((meta.get("adapter") or {}).get("address") or "").lower()}
            if looping.is_loop(pos["pool_id"]):
                if getattr(self, "loops", None) and pos.get("loop"):
                    trigger = self.loops.risk_trigger(pos, meta)
            elif keys & config.POOL_BLOCKLIST:
                trigger = "POOL_BLOCKLIST"
            elif pos.get("entry_tvl") and pos.get("tvl_now") is not None:
                drop = (1 - float(pos["tvl_now"]) / float(pos["entry_tvl"])) * 100
                if drop >= config.EXIT_TVL_DROP_PCT:
                    trigger = f"TVL_DROP_{drop:.0f}%"
            if trigger:
                exits.append(dict(pos, trigger=trigger))
        return exits

    # ------------------------------------------------------------ limiti
    def _exposure(self, status: Dict[str, Any], key: str, value: str) -> float:
        return sum(p["value_usd"] for p in status["positions"] if p.get(key) == value)

    def _deposit_cap(self, pool: Dict[str, Any], status: Dict[str, Any],
                     freed_usd: float = 0.0, leaving: Optional[Dict[str, Any]] = None) -> float:
        """Massimo depositabile in `pool` rispettando tutti i limiti."""
        total = status["total_value_usd"]
        in_pool = self._exposure(status, "pool_id", pool["pool_id"])
        in_protocol = self._exposure(status, "project", pool["project"])
        if leaving is not None:
            if leaving["pool_id"] == pool["pool_id"]:
                in_pool -= freed_usd
            if leaving.get("project") == pool["project"]:
                in_protocol -= freed_usd
        caps = [
            config.MAX_DEPOSIT_USD,
            status["usdc_balance"] + freed_usd,
            config.MAX_POOL_PCT * total - in_pool,
            config.MAX_PROTOCOL_PCT * total - in_protocol,
        ]
        if pool.get("loop"):
            in_loops = sum(p["value_usd"] for p in status["positions"] if looping.is_loop(p["pool_id"]))
            caps.append(config.LOOP_MAX_CAPITAL_PCT * total - in_loops)
            caps.append(pool["loop"].get("capacity_usd", float("inf")))
        return max(0.0, min(caps))

    def _held(self, status: Dict[str, Any], pool_id: str) -> Optional[Dict[str, Any]]:
        pool_id = str(pool_id or "").strip()
        for pos in status["positions"]:
            if pos["pool_id"] == pool_id:
                return pos
        return None

    def _check_gas_reserve(self, status: Dict[str, Any]):
        if not self.paper and status["eth_balance"] < config.MIN_ETH_RESERVE:
            raise RiskRejection(
                f"ETH per il gas {status['eth_balance']:.5f} sotto la riserva "
                f"{config.MIN_ETH_RESERVE}"
            )

    @staticmethod
    def switch_cost_usd(amount_usd: float, from_asset: str, to_asset: str) -> float:
        """Costo stimato di spostare `amount_usd` da un pool all'altro."""
        n_tx = 3  # withdraw, approve, deposit
        swap_usd = 0.0
        if from_asset != "USDC":
            n_tx += 2
            swap_usd += amount_usd
        if to_asset != "USDC":
            n_tx += 2
            swap_usd += amount_usd
        return n_tx * config.EST_TX_COST_USD + swap_usd * config.DEFAULT_SLIPPAGE_BPS / 10_000

    # ------------------------------------------------------------ esecuzione
    def execute_signal(self, decision: Dict[str, Any], status: Dict[str, Any]) -> Dict[str, Any]:
        op = str(decision.get("operation", "hold")).lower()
        try:
            if op == "deposit":
                return self._deposit(decision, status)
            if op == "withdraw":
                return self._withdraw(decision, status)
            if op == "rebalance":
                return self._rebalance(decision, status)
            return {"status": "hold", "operation": "hold", "reason": decision.get("reason", "")}
        except RiskRejection as exc:
            return {"status": "rejected", "operation": op, "reason": str(exc),
                    "pool": decision.get("pool"), "from_pool": decision.get("from_pool")}
        except BaseChainError as exc:
            return {"status": "error", "operation": op, "reason": str(exc),
                    "pool": decision.get("pool"), "from_pool": decision.get("from_pool")}

    def liquidate(self, position: Dict[str, Any], trigger: str) -> Dict[str, Any]:
        """Uscita di rischio: ritira tutto dal pool."""
        decision = {"operation": "withdraw", "pool": position["pool_id"], "withdraw_portion": 1.0,
                    "reason": f"uscita automatica {trigger}"}
        # la liquidazione non passa dal controllo gas del modello: se manca ETH fallira' on-chain
        status = {"positions": [position], "eth_balance": float("inf")}
        result = self._withdraw(decision, status)
        result["trigger"] = trigger
        return result

    # -------- deposito
    def _validate_deposit(self, pool_id: str, amount_usd: float, status: Dict[str, Any],
                          freed_usd: float = 0.0, leaving: Optional[Dict[str, Any]] = None):
        pool = self._pool_in_universe(pool_id)
        if not pool:
            raise RiskRejection(f"pool '{pool_id}' non presente nell'universo filtrato")
        if not pool.get("adapter"):
            raise RiskRejection(
                f"pool {pool['project']} {pool['symbol']} non eseguibile: "
                f"{pool.get('adapter_reason', 'contratto non riconosciuto')}"
            )
        already_held = self._held(status, pool["pool_id"]) is not None
        n_positions = len(status["positions"]) - (1 if leaving and freed_usd >= leaving["value_usd"] * 0.999 else 0)
        if not already_held and n_positions >= config.MAX_POSITIONS:
            raise RiskRejection(f"gia' {n_positions} posizioni aperte (max {config.MAX_POSITIONS})")
        cap = self._deposit_cap(pool, status, freed_usd, leaving)
        amount = min(amount_usd, cap)
        if amount < config.MIN_DEPOSIT_USD:
            raise RiskRejection(
                f"importo ${amount:.2f} sotto il minimo ${config.MIN_DEPOSIT_USD:.2f} "
                f"(richiesti ${amount_usd:.2f}, disponibili entro i limiti ${cap:.2f})"
            )
        return pool, amount

    def _deposit(self, decision: Dict[str, Any], status: Dict[str, Any]) -> Dict[str, Any]:
        self._check_gas_reserve(status)
        requested = float(decision.get("target_portion_of_portfolio", 0.0)) * status["total_value_usd"]
        pool, amount = self._validate_deposit(decision.get("pool"), requested, status)
        base = {"operation": "deposit", "pool": pool["pool_id"], "project": pool["project"],
                "symbol": pool["symbol"], "amount_usd": round(amount, 4),
                "apy": pool["effective_apy"], "reason": decision.get("reason", "")}

        if pool.get("loop"):
            if not getattr(self, "loops", None):
                raise RiskRejection("looping disattivato (LOOP_ENABLED=false)")
            return self.loops.open(pool, amount, base)
        if self.paper:
            res = self.paper.deposit(pool, amount)
            self.store.record_deposit(pool, {"kind": pool["adapter"], "address": pool.get("contract"),
                                             "asset": pool["asset_address"]}, res["deposited_usd"], 0.0)
            return dict(base, status="paper", **res)
        if config.DRY_RUN:
            return dict(base, status="dry_run", plan=self._plan_deposit(pool, amount))
        return dict(base, **self._live_deposit(pool, amount))

    def _plan_deposit(self, pool: Dict[str, Any], amount_usd: float) -> List[str]:
        steps = []
        if pool["asset"] != "USDC":
            steps.append(f"swap ${amount_usd:.2f} USDC -> {pool['asset']} su Uniswap V3")
        steps.append(f"approve {pool['asset']} a {pool.get('contract')}")
        steps.append(f"deposit in {pool['project']} {pool['symbol']} ({pool['adapter']})")
        return steps

    def _send(self, fn, description: str) -> Dict[str, Any]:
        tx = fn.build_transaction({
            "from": self.client.address,
            "value": 0,
            "nonce": self.client.w3.eth.get_transaction_count(self.client.address),
            "chainId": config.CHAIN_ID,
        })
        tx.pop("maxFeePerGas", None)
        tx.pop("maxPriorityFeePerGas", None)
        return self.client.send_transaction(tx, description=description)

    def _swap(self, token_in: str, token_out: str, amount_raw: int, label: str) -> Dict[str, Any]:
        route = self.uniswap.best_route(token_in, token_out, amount_raw)
        if not route:
            raise BaseChainError(f"nessuna rotta Uniswap per {label}")
        return self.uniswap.swap(route, config.DEFAULT_SLIPPAGE_BPS)

    def _live_deposit(self, pool: Dict[str, Any], amount_usd: float) -> Dict[str, Any]:
        adapter = self.adapters[pool["pool_id"]]
        asset_sym = pool["asset"]
        txs = []

        if asset_sym == "USDC":
            amount_raw = protocols.float_to_raw(amount_usd, "USDC")
        else:
            before = self.client.balance_of(adapter.asset)
            txs.append(self._swap(config.USDC, adapter.asset,
                                  protocols.float_to_raw(amount_usd, "USDC"),
                                  f"USDC -> {asset_sym}"))
            amount_raw = self.client.balance_of(adapter.asset) - before
            if amount_raw <= 0:
                raise BaseChainError(f"lo swap non ha prodotto {asset_sym}")

        approve = self.client.ensure_allowance(adapter.asset, adapter.spender, amount_raw)
        if approve:
            txs.append(approve)

        fn = adapter.deposit_fn(amount_raw)
        if adapter.kind == "ctoken":
            # i fork Compound V2 segnalano l'errore col valore di ritorno, non col revert
            code = fn.call({"from": self.client.address})
            if int(code) != 0:
                raise BaseChainError(f"mint rifiutato dal cToken (codice {code})")
        txs.append(self._send(fn, f"deposit {pool['project']} {pool['symbol']}"))

        amount_asset = protocols.raw_to_float(amount_raw, asset_sym)
        self.store.record_deposit(pool, adapter.to_dict(), amount_usd, amount_asset)
        return {"status": "success", "transactions": txs, "amount_asset": amount_asset}

    # -------- prelievo
    def _withdraw(self, decision: Dict[str, Any], status: Dict[str, Any]) -> Dict[str, Any]:
        self._check_gas_reserve(status)
        pos = self._held(status, decision.get("pool"))
        if not pos:
            raise RiskRejection(f"nessuna posizione aperta nel pool '{decision.get('pool')}'")
        fraction = max(0.0, min(1.0, float(decision.get("withdraw_portion", 1.0) or 1.0)))
        amount_usd = pos["value_usd"] * fraction
        if fraction < 0.999 and amount_usd < config.MIN_DEPOSIT_USD:
            raise RiskRejection(f"prelievo di ${amount_usd:.2f} troppo piccolo")
        base = {"operation": "withdraw", "pool": pos["pool_id"], "project": pos.get("project"),
                "symbol": pos.get("symbol"), "amount_usd": round(amount_usd, 4),
                "withdraw_portion": fraction, "reason": decision.get("reason", "")}

        if looping.is_loop(pos["pool_id"]):
            if not getattr(self, "loops", None):
                raise RiskRejection("looping disattivato (LOOP_ENABLED=false): riattivalo per smontare il loop")
            return self.loops.close(pos, fraction, base)
        if self.paper:
            res = self.paper.withdraw(pos["pool_id"], fraction)
            self.store.record_withdraw(pos["pool_id"], fraction)
            return dict(base, status="paper", **res)
        if config.DRY_RUN:
            plan = [f"withdraw {fraction:.0%} da {pos.get('project')} {pos.get('symbol')}"]
            if pos.get("asset") != "USDC":
                plan.append(f"swap {pos.get('asset')} -> USDC su Uniswap V3")
            return dict(base, status="dry_run", plan=plan)
        return dict(base, **self._live_withdraw(pos, fraction))

    def _live_withdraw(self, pos: Dict[str, Any], fraction: float) -> Dict[str, Any]:
        meta = self.store.get(pos["pool_id"]) or {}
        adapter = self.adapters.get(pos["pool_id"]) or self._adapter_for_position(meta)
        if not adapter:
            raise BaseChainError(f"contratto del pool {pos['pool_id']} non ricostruibile")
        asset_sym = pos["asset"]
        txs = []

        before = self.client.balance_of(adapter.asset)
        if fraction >= 0.999:
            fn = adapter.withdraw_fn(None)
        else:
            raw = int(adapter.balance_raw() * fraction)
            raw = min(raw, adapter.withdrawable_raw())
            fn = adapter.withdraw_fn(raw)
        txs.append(self._send(fn, f"withdraw {pos.get('project')} {pos.get('symbol')}"))
        received_raw = self.client.balance_of(adapter.asset) - before

        # se il pool non aveva liquidita' sufficiente il prelievo e' parziale
        remaining_usd = protocols.raw_to_float(adapter.balance_raw(), asset_sym) * self.prices().get(asset_sym, 0.0)
        effective_fraction = 1.0 if remaining_usd < config.DUST_USD else fraction
        self.store.record_withdraw(pos["pool_id"], effective_fraction)

        received_usd = protocols.raw_to_float(received_raw, asset_sym) * self.prices().get(asset_sym, 0.0)
        if asset_sym != "USDC" and received_raw > 0:
            before_usdc = self.client.balance_of(config.USDC)
            txs.append(self._swap(adapter.asset, config.USDC, received_raw, f"{asset_sym} -> USDC"))
            received_usd = protocols.raw_to_float(self.client.balance_of(config.USDC) - before_usdc, "USDC")

        return {"status": "success", "transactions": txs,
                "withdrawn_usd": round(received_usd, 4),
                "partial": remaining_usd >= config.DUST_USD and fraction >= 0.999}

    # -------- ribilanciamento
    def _rebalance(self, decision: Dict[str, Any], status: Dict[str, Any]) -> Dict[str, Any]:
        self._check_gas_reserve(status)
        src = self._held(status, decision.get("from_pool"))
        if not src:
            raise RiskRejection(f"nessuna posizione aperta nel pool '{decision.get('from_pool')}'")
        dst = self._pool_in_universe(decision.get("pool"))
        if not dst:
            raise RiskRejection(f"pool di destinazione '{decision.get('pool')}' non nell'universo filtrato")
        if dst["pool_id"] == src["pool_id"]:
            raise RiskRejection("origine e destinazione coincidono")
        if looping.is_loop(src["pool_id"]) or looping.is_loop(dst["pool_id"]):
            raise RiskRejection("un loop non si ribilancia: usa withdraw e poi deposit")

        if src["held_hours"] < config.MIN_HOLD_HOURS:
            raise RiskRejection(
                f"posizione aperta da {src['held_hours']:.1f}h, minimo {config.MIN_HOLD_HOURS:.0f}h"
            )
        src_apy = float(src.get("apy_now") or 0.0)
        gain_pts = float(dst["effective_apy"]) - src_apy
        if gain_pts < config.MIN_APY_IMPROVEMENT:
            raise RiskRejection(
                f"miglioramento APY {gain_pts:+.2f} punti sotto il minimo {config.MIN_APY_IMPROVEMENT:.2f}"
            )

        fraction = max(0.0, min(1.0, float(decision.get("withdraw_portion", 1.0) or 1.0)))
        amount = src["value_usd"] * fraction
        cost = self.switch_cost_usd(amount, src.get("asset") or "USDC", dst["asset"])
        daily_gain = amount * gain_pts / 100 / 365
        breakeven = cost / daily_gain if daily_gain > 0 else float("inf")
        if breakeven > config.MAX_BREAKEVEN_DAYS:
            raise RiskRejection(
                f"lo spostamento si ripaga in {breakeven:.1f} giorni (max {config.MAX_BREAKEVEN_DAYS:.0f})"
            )
        # verifica i limiti del deposito prima di toccare la posizione di origine
        self._validate_deposit(dst["pool_id"], amount, status, freed_usd=amount, leaving=src)

        withdraw = self._withdraw({"pool": src["pool_id"], "withdraw_portion": fraction,
                                   "reason": decision.get("reason", "")}, status)
        if withdraw["status"] in ("rejected", "error"):
            return dict(withdraw, operation="rebalance")

        if self.paper:
            freed = withdraw.get("withdrawn_usd", amount)
            new_status = self.get_account_status()
            dep_pool, dep_amount = self._validate_deposit(dst["pool_id"], freed, new_status)
            res = self.paper.deposit(dep_pool, dep_amount)
            self.store.record_deposit(dep_pool, {"kind": dep_pool["adapter"], "address": dep_pool.get("contract"),
                                                 "asset": dep_pool["asset_address"]}, res["deposited_usd"], 0.0)
            deposit = dict(res, status="paper")
        elif config.DRY_RUN:
            deposit = {"status": "dry_run", "plan": self._plan_deposit(dst, amount)}
        else:
            freed = withdraw.get("withdrawn_usd", 0.0)
            deposit = self._live_deposit(dst, min(freed, self._deposit_cap(dst, self.get_account_status())))

        return {
            "operation": "rebalance",
            "status": deposit.get("status", "success"),
            "from_pool": src["pool_id"],
            "pool": dst["pool_id"],
            "project": dst["project"],
            "symbol": dst["symbol"],
            "amount_usd": round(amount, 4),
            "apy_gain_points": round(gain_pts, 4),
            "breakeven_days": round(breakeven, 2),
            "withdraw": withdraw,
            "deposit": deposit,
            "reason": decision.get("reason", ""),
        }
