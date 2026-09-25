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
import protocols
import yield_scanner
from base_client import BaseChainError, BaseClient
from paper import PaperBook
from positions import PositionStore

logger = logging.getLogger(__name__)

ADAPTER_CLASSES = {
    "aave-v3": protocols.AaveV3Adapter,
    "aave-v3-loop": protocols.AaveV3LoopAdapter,
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
    if kind == "aave-v3-loop":
        return protocols.AaveV3LoopAdapter(
            client,
            data["asset"],
            data.get("borrow_asset", config.WETH),
            leverage=float(data.get("leverage", config.LOOP_MAX_LEVERAGE)),
            staking_yield=float(data.get("staking_yield", 3.2)) if "staking_yield" in data else None,
        )
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

    # ------------------------------------------------------------ prezzi
    @property
    def uniswap(self):
        if self._uniswap is None:
            from uniswap import UniswapV3
            self._uniswap = UniswapV3(self.client)
        return self._uniswap

    # ------------------------------------------------------------ auto-refuel
    def ensure_usdc_balance(self) -> Optional[Dict[str, Any]]:
        """
        Se in modalita' on-chain e il saldo USDC e' insufficiente,
        ma c'e' ETH spendibile oltre la riserva gas, effettua l'auto-refuel.
        """
        if self.paper or not self.client:
            return None
        return self.uniswap.auto_refuel_usdc()

    def prices(self) -> Dict[str, float]:
        """Prezzo in USD degli asset gestiti (USDC = 1 per definizione)."""
        if self._prices is None:
            prices = {"USDC": 1.0}
            held = {p.get("asset") for p in self.store.positions.values()}

            # 1. Se serve prezzare WETH o asset LST (wstETH, cbETH), quota prima WETH in USDC
            needs_eth = any(s in config.DEPOSIT_ASSETS or s in held for s in ("WETH", "wstETH", "cbETH"))
            if needs_eth and "WETH" not in prices:
                try:
                    weth_price = self.uniswap.price_in_quote(config.WETH, config.USDC, probe_units=0.01)
                    if weth_price:
                        prices["WETH"] = weth_price
                except Exception as exc:
                    logger.warning("Prezzo WETH non disponibile: %s", exc)

            # 2. Prezza gli altri asset
            for sym, meta in config.KNOWN_ASSETS.items():
                if sym in prices or (sym not in config.DEPOSIT_ASSETS and sym not in held):
                    continue
                try:
                    if sym in ("wstETH", "cbETH") and "WETH" in prices:
                        # Gli LST hanno pool diretti liquidi con WETH
                        probe = self.uniswap.price_in_quote(meta["address"], config.WETH, probe_units=0.01)
                        if probe:
                            prices[sym] = probe * prices["WETH"]
                        else:
                            fallback_ratio = 1.18 if sym == "wstETH" else 1.08
                            prices[sym] = prices["WETH"] * fallback_ratio
                    else:
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
        if self.paper:
            self.paper.accrue({pid: current[pid]["effective_apy"] for pid in held_ids if pid in current})
            usdc = self.paper.usdc
            eth = self.paper.eth
            idle_assets: Dict[str, float] = {}
            for pid, ppos in self.paper.positions.items():
                meta = self.store.get(pid) or {}
                positions.append(self._position_row(
                    pid, meta, current.get(pid), ppos["value_usd"], None,
                    is_loop=ppos.get("is_loop"),
                    leverage=ppos.get("leverage"),
                    health_factor=ppos.get("health_factor")
                ))
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
                hf = None
                if isinstance(adapter, protocols.AaveV3LoopAdapter):
                    try:
                        hf = adapter.health_factor()
                    except Exception as exc:
                        logger.warning("Errore lettura health factor per %s: %s", pid, exc)
                positions.append(self._position_row(
                    pid, meta, current.get(pid), value, withdrawable,
                    is_loop=isinstance(adapter, protocols.AaveV3LoopAdapter) or meta.get("is_loop"),
                    leverage=meta.get("leverage"),
                    health_factor=hf
                ))

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
                    hf = None
                    if isinstance(adapter, protocols.AaveV3LoopAdapter):
                        try:
                            hf = adapter.health_factor()
                        except Exception:
                            pass
                    positions.append(self._position_row(
                        pid, meta, pool, value, None,
                        is_loop=isinstance(adapter, protocols.AaveV3LoopAdapter) or pool.get("is_loop"),
                        leverage=pool.get("leverage"),
                        health_factor=hf
                    ))

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
        if self.paper:
            summary = self.paper.summary()
            status["paper"] = summary
            status["pnl_since_start_usd"] = round(total - summary["initial_usdc"], 4)
        return status

    def _position_row(self, pid: str, meta: Dict[str, Any], pool: Optional[Dict[str, Any]],
                      value_usd: float, withdrawable_usd: Optional[float],
                      is_loop: bool = False, leverage: Optional[float] = None,
                      health_factor: Optional[float] = None) -> Dict[str, Any]:
        pool = pool or {}
        opened = meta.get("opened_at") or time.time()
        deposited = float(meta.get("deposited_usd") or 0.0)
        is_loop_val = bool(is_loop or meta.get("is_loop") or pool.get("is_loop"))
        lev_val = leverage or meta.get("leverage") or pool.get("leverage")
        hf_val = health_factor if health_factor is not None else meta.get("health_factor")
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
            "is_loop": is_loop_val,
            "leverage": round(float(lev_val), 2) if lev_val else None,
            "health_factor": round(float(hf_val), 2) if hf_val is not None and math.isfinite(hf_val) else (hf_val if hf_val is not None else None),
        }

    # ------------------------------------------------------------ uscite di rischio
    def risk_exits(self, status: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Posizioni da chiudere senza chiedere al modello."""
        exits = []
        for pos in status["positions"]:
            trigger = None
            meta = self.store.get(pos["pool_id"]) or {}
            keys = {pos["pool_id"].lower(), str((meta.get("adapter") or {}).get("address") or "").lower()}
            if keys & config.POOL_BLOCKLIST:
                trigger = "POOL_BLOCKLIST"
            elif pos.get("is_loop") and pos.get("health_factor") is not None:
                hf = pos["health_factor"]
                if isinstance(hf, (int, float)) and hf < config.LOOP_MIN_HEALTH_FACTOR:
                    trigger = f"HEALTH_FACTOR_LOW_{hf:.2f}"
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
        return max(0.0, min(
            config.MAX_DEPOSIT_USD,
            status["usdc_balance"] + freed_usd,
            config.MAX_POOL_PCT * total - in_pool,
            config.MAX_PROTOCOL_PCT * total - in_protocol,
        ))

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

    def release_funds(self, target_usdc: float = 0.0) -> Dict[str, Any]:
        """
        Ritira capitale dai pool di rendimento (Aave, Moonwell, etc.) per liberare USDC nel wallet.
        Se target_usdc <= 0, ritira tutte le posizioni.
        Altrimenti ritira fino a raggiungere target_usdc.
        """
        status = self.get_account_status()
        positions = list(status.get("positions", []))
        if not positions:
            idle = float(status.get("usdc_balance", 0.0))
            return {
                "status": "success",
                "message": "Nessuna posizione di rendimento attiva",
                "released_usd": 0.0,
                "usdc_balance": round(idle, 2),
                "withdrawals": []
            }

        withdrawals = []
        total_freed = 0.0

        for pos in positions:
            if target_usdc > 0 and total_freed >= target_usdc:
                break
            try:
                logger.info("Ritiro da pool %s per release_funds...", pos.get("pool_id"))
                res = self.liquidate(pos, trigger="svincolo fondi")
                freed = float(res.get("amount_usd", pos.get("value_usd", 0.0)))
                total_freed += freed
                withdrawals.append({"pool_id": pos.get("pool_id"), "result": res})
            except Exception as exc:
                logger.error("Errore ritiro da pool %s: %s", pos.get("pool_id"), exc)

        new_status = self.get_account_status()
        wallet_usdc = float(new_status.get("usdc_balance", 0.0))
        return {
            "status": "success",
            "message": f"Liberati ${total_freed:.2f} USDC (saldo attuale: ${wallet_usdc:.2f})",
            "released_usd": round(total_freed, 2),
            "usdc_balance": round(wallet_usdc, 2),
            "withdrawals": withdrawals
        }

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
        if pool.get("is_loop"):
            leverage = float(pool.get("leverage") or config.LOOP_MAX_LEVERAGE)
            collateral_sym = pool.get("asset", "wstETH")
            borrow_sym = pool.get("borrow_asset", "WETH")
            if collateral_sym != "USDC":
                steps.append(f"swap ${amount_usd:.2f} USDC -> {collateral_sym} su Uniswap V3")
            steps.append(f"approve {collateral_sym} ad Aave V3 Pool ({config.AAVE_V3_POOL})")
            steps.append(f"supply {collateral_sym} come collateral iniziale")
            steps.append(f"loop: borrow {borrow_sym} a tasso variabile (target leva {leverage:.1f}x)")
            steps.append(f"loop: swap {borrow_sym} -> {collateral_sym} su Uniswap V3")
            steps.append(f"loop: supply {collateral_sym} per leva {leverage:.1f}x (Health Factor target ~{config.LOOP_TARGET_HEALTH_FACTOR:.2f})")
            return steps

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

        if pool.get("is_loop") or isinstance(adapter, protocols.AaveV3LoopAdapter):
            # 1. Swap USDC -> Collateral (wstETH/cbETH) se necessario
            if asset_sym == "USDC":
                collateral_raw = protocols.float_to_raw(amount_usd, "USDC")
            else:
                before = self.client.balance_of(adapter.asset)
                txs.append(self._swap(config.USDC, adapter.asset,
                                      protocols.float_to_raw(amount_usd, "USDC"),
                                      f"USDC -> {asset_sym}"))
                collateral_raw = self.client.balance_of(adapter.asset) - before
                if collateral_raw <= 0:
                    raise BaseChainError(f"lo swap non ha prodotto {asset_sym}")

            # 2. Approve e supply iniziale del collateral
            app = self.client.ensure_allowance(adapter.asset, adapter.spender, collateral_raw)
            if app:
                txs.append(app)
            txs.append(self._send(adapter.deposit_fn(collateral_raw), f"supply {asset_sym} collateral"))

            # 3. Looping: borrow WETH, swap in collateral e rideposita
            leverage = getattr(adapter, "leverage", config.LOOP_MAX_LEVERAGE)
            if leverage > 1.05:
                borrow_weth_raw = int(collateral_raw * (leverage - 1.0))
                if borrow_weth_raw > 0:
                    txs.append(self._send(adapter.borrow_fn(borrow_weth_raw), f"borrow WETH (loop {leverage:.1f}x)"))
                    before_loop = self.client.balance_of(adapter.asset)
                    txs.append(self._swap(config.WETH, adapter.asset, borrow_weth_raw, f"WETH -> {asset_sym}"))
                    reinvest_raw = self.client.balance_of(adapter.asset) - before_loop
                    if reinvest_raw > 0:
                        app_reinvest = self.client.ensure_allowance(adapter.asset, adapter.spender, reinvest_raw)
                        if app_reinvest:
                            txs.append(app_reinvest)
                        txs.append(self._send(adapter.deposit_fn(reinvest_raw), f"re-supply {asset_sym} collateral"))

            amount_asset = protocols.raw_to_float(collateral_raw, asset_sym)
            adapter_dict = adapter.to_dict()
            self.store.record_deposit(pool, adapter_dict, amount_usd, amount_asset)
            return {"status": "success", "transactions": txs, "amount_asset": amount_asset}

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

        if self.paper:
            res = self.paper.withdraw(pos["pool_id"], fraction)
            self.store.record_withdraw(pos["pool_id"], fraction)
            return dict(base, status="paper", **res)
        if config.DRY_RUN:
            if pos.get("is_loop"):
                collateral_sym = pos.get("asset", "wstETH")
                borrow_sym = "WETH"
                plan = [
                    f"de-loop: withdraw partial collateral {collateral_sym} da Aave V3",
                    f"de-loop: swap {collateral_sym} -> {borrow_sym} su Uniswap V3",
                    f"de-loop: repay debito {borrow_sym} su Aave V3",
                    f"withdraw remaining collateral {collateral_sym} ({fraction:.0%})",
                ]
                if collateral_sym != "USDC":
                    plan.append(f"swap {collateral_sym} -> USDC su Uniswap V3")
                return dict(base, status="dry_run", plan=plan)

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

        if pos.get("is_loop") or isinstance(adapter, protocols.AaveV3LoopAdapter):
            # De-looping: rimborso del debito e prelievo del collateral
            debt_raw = adapter.debt_raw() if hasattr(adapter, "debt_raw") else 0
            if debt_raw > 0:
                weth_bal = self.client.balance_of(config.WETH)
                if weth_bal < debt_raw:
                    withdrawable = adapter.withdrawable_raw()
                    if withdrawable > 0:
                        txs.append(self._send(adapter.withdraw_fn(withdrawable), f"withdraw partial {asset_sym} for de-loop"))
                        avail_collateral = self.client.balance_of(adapter.asset)
                        if avail_collateral > 0:
                            txs.append(self._swap(adapter.asset, config.WETH, avail_collateral, f"{asset_sym} -> WETH"))
                repay_amt = min(self.client.balance_of(config.WETH), debt_raw)
                if repay_amt > 0:
                    app = self.client.ensure_allowance(config.WETH, adapter.spender, repay_amt)
                    if app:
                        txs.append(app)
                    txs.append(self._send(adapter.repay_fn(repay_amt), "repay WETH debt"))

            before_col = self.client.balance_of(adapter.asset)
            fn = adapter.withdraw_fn(None if fraction >= 0.999 else int(adapter.balance_raw() * fraction))
            txs.append(self._send(fn, f"withdraw {pos.get('symbol')} collateral"))
            received_raw = self.client.balance_of(adapter.asset) - before_col

            self.store.record_withdraw(pos["pool_id"], fraction)
            received_usd = protocols.raw_to_float(received_raw, asset_sym) * self.prices().get(asset_sym, 0.0)

            if asset_sym != "USDC" and received_raw > 0:
                before_usdc = self.client.balance_of(config.USDC)
                txs.append(self._swap(adapter.asset, config.USDC, received_raw, f"{asset_sym} -> USDC"))
                received_usd = protocols.raw_to_float(self.client.balance_of(config.USDC) - before_usdc, "USDC")

            return {"status": "success", "transactions": txs, "withdrawn_usd": round(received_usd, 4), "partial": False}

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
