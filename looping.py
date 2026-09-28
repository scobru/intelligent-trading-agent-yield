"""
Looping sul lending: LST come collaterale su Aave V3, prestito in WETH.

Un loop parte da capitale in USDC, lo trasforma in un liquid staking token
(wstETH, cbETH, weETH), lo deposita su Aave e poi ripete:

    prendi in prestito WETH -> compralo in LST -> deposita

finche' il collaterale vale `leva` volte il capitale. Lo staking matura su
tutto il collaterale, il tasso del prestito si paga sul debito:

    APY netto = leva x (staking + supply LST) - (leva - 1) x borrow WETH

Collaterale e debito sono entrambi ETH: il prezzo di ETH non muove l'health
factor, lo muovono solo il rapporto LST/ETH (depeg) e gli interessi. Il
netto pero' e' in ETH, quindi il valore in dollari del loop segue ETH.

Il bot non si fida dei numeri a memoria: LTV e soglia di liquidazione
(e-mode compresa), tassi, cap di supply e di borrow, liquidita' di WETH e
prezzi dell'oracolo di Aave si leggono dalla chain a ogni ciclo. L'health
factor usato per le uscite e' quello del solo loop (collaterale LST contro
debito WETH): se sul conto Aave ci sono anche altri depositi, quello reale
e' piu' alto, mai piu' basso.

Paper: stesso calcolo, sui tassi veri letti dalla chain, con il netto in ETH
valorizzato al prezzo corrente (si vede l'esposizione a ETH). Semplificazione
dichiarata: in paper un LST vale 1 ETH piu' lo staking maturato, niente
depeg simulato.
"""

import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from web3 import Web3

import config
import http_client
from base_client import MAX_UINT256, BaseChainError

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = 31_536_000
VARIABLE_RATE = 2
LOOP_PREFIX = "loop:"
LOOP_KIND = "aave-loop"
EMODE_SCAN_MAX = 16

LOOP_POOL_ABI = [
    {"inputs": [{"name": "asset", "type": "address"}], "name": "getReserveData",
     "outputs": [{"components": [
         {"name": "configuration", "type": "uint256"},
         {"name": "liquidityIndex", "type": "uint128"},
         {"name": "currentLiquidityRate", "type": "uint128"},
         {"name": "variableBorrowIndex", "type": "uint128"},
         {"name": "currentVariableBorrowRate", "type": "uint128"},
         {"name": "currentStableBorrowRate", "type": "uint128"},
         {"name": "lastUpdateTimestamp", "type": "uint40"},
         {"name": "id", "type": "uint16"},
         {"name": "aTokenAddress", "type": "address"},
         {"name": "stableDebtTokenAddress", "type": "address"},
         {"name": "variableDebtTokenAddress", "type": "address"},
         {"name": "interestRateStrategyAddress", "type": "address"},
         {"name": "accruedToTreasury", "type": "uint128"},
         {"name": "unbacked", "type": "uint128"},
         {"name": "isolationModeTotalDebt", "type": "uint128"}],
         "name": "", "type": "tuple"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "onBehalfOf", "type": "address"}, {"name": "referralCode", "type": "uint16"}],
     "name": "supply", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "to", "type": "address"}],
     "name": "withdraw", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "interestRateMode", "type": "uint256"}, {"name": "referralCode", "type": "uint16"},
                {"name": "onBehalfOf", "type": "address"}],
     "name": "borrow", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"},
                {"name": "interestRateMode", "type": "uint256"}, {"name": "onBehalfOf", "type": "address"}],
     "name": "repay", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "categoryId", "type": "uint8"}], "name": "setUserEMode",
     "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "user", "type": "address"}], "name": "getUserEMode",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "ADDRESSES_PROVIDER",
     "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    # Aave >= 3.2: categorie e-mode con bitmap di collaterali e asset prestabili
    {"inputs": [{"name": "id", "type": "uint8"}], "name": "getEModeCategoryCollateralConfig",
     "outputs": [{"components": [
         {"name": "ltv", "type": "uint16"},
         {"name": "liquidationThreshold", "type": "uint16"},
         {"name": "liquidationBonus", "type": "uint16"}], "name": "", "type": "tuple"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "id", "type": "uint8"}], "name": "getEModeCategoryCollateralBitmap",
     "outputs": [{"name": "", "type": "uint128"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "id", "type": "uint8"}], "name": "getEModeCategoryBorrowableBitmap",
     "outputs": [{"name": "", "type": "uint128"}], "stateMutability": "view", "type": "function"},
    # Aave 3.0/3.1: la categoria sta nella configurazione della riserva
    {"inputs": [{"name": "id", "type": "uint8"}], "name": "getEModeCategoryData",
     "outputs": [{"components": [
         {"name": "ltv", "type": "uint16"},
         {"name": "liquidationThreshold", "type": "uint16"},
         {"name": "liquidationBonus", "type": "uint16"},
         {"name": "priceSource", "type": "address"},
         {"name": "label", "type": "string"}], "name": "", "type": "tuple"}],
     "stateMutability": "view", "type": "function"},
]

PROVIDER_ABI = [
    {"inputs": [], "name": "getPriceOracle", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
]

ORACLE_ABI = [
    {"inputs": [{"name": "asset", "type": "address"}], "name": "getAssetPrice",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

TOKEN_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


# ================================================================ matematica
def rate_to_apy(rate_ray: int) -> float:
    """Tasso Aave (APR in ray, composto al secondo) -> APY in percentuale."""
    apr = int(rate_ray) / 1e27
    return ((1 + apr / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1) * 100


def net_apy(leverage: float, collateral_apy: float, borrow_apy: float) -> float:
    """Rendimento sul capitale proprio, in punti percentuali."""
    return leverage * collateral_apy - (leverage - 1) * borrow_apy


def health_factor(collateral: float, debt: float, liq_threshold: float) -> float:
    if debt <= 0:
        return float("inf")
    return collateral * liq_threshold / debt


def leverage_for_hf(hf: float, liq_threshold: float) -> float:
    """Leva che lascia l'health factor a `hf`: HF = LT x L / (L - 1)."""
    if hf <= liq_threshold:
        return float("inf")
    return hf / (hf - liq_threshold)


def max_leverage_for_ltv(ltv: float) -> float:
    """Leva massima raggiungibile a forza di iterazioni con l'LTV dato."""
    usable = ltv * config.LOOP_LTV_BUFFER
    if usable >= 1:
        return float("inf")
    return 1 / (1 - usable)


def target_leverage(ltv: float, liq_threshold: float) -> float:
    lev = min(config.LOOP_MAX_LEVERAGE,
              leverage_for_hf(config.LOOP_TARGET_HEALTH_FACTOR, liq_threshold),
              max_leverage_for_ltv(ltv))
    return max(1.0, lev)


def plan_iterations(equity: float, leverage: float, ltv: float,
                    max_iterations: int = None) -> Tuple[List[float], float, float]:
    """
    Prestiti successivi (nell'unita' di `equity`) per arrivare alla leva.
    Ogni prestito e' limitato dall'LTV sul collaterale accumulato fino a li'.
    Restituisce (prestiti, collaterale finale, debito finale).
    """
    max_iterations = config.LOOP_MAX_ITERATIONS if max_iterations is None else max_iterations
    coll, debt = equity, 0.0
    target_debt = equity * (leverage - 1)
    borrows: List[float] = []
    for _ in range(max_iterations):
        room = coll * ltv * config.LOOP_LTV_BUFFER - debt
        amount = min(target_debt - debt, room)
        if amount <= equity * 1e-3:
            break
        borrows.append(amount)
        debt += amount
        coll += amount
    return borrows, coll, debt


def _bits(value: int, start: int, length: int) -> int:
    return (int(value) >> start) & ((1 << length) - 1)


def parse_reserve_config(cfg: int) -> Dict[str, Any]:
    """Campi della ReserveConfigurationMap di Aave V3 che servono al loop."""
    return {
        "ltv": _bits(cfg, 0, 16) / 10_000,
        "lt": _bits(cfg, 16, 16) / 10_000,
        "decimals": _bits(cfg, 48, 8),
        "active": bool(_bits(cfg, 56, 1)),
        "frozen": bool(_bits(cfg, 57, 1)),
        "borrowing": bool(_bits(cfg, 58, 1)),
        "paused": bool(_bits(cfg, 60, 1)),
        "borrow_cap": _bits(cfg, 80, 36),     # in token interi, 0 = nessun cap
        "supply_cap": _bits(cfg, 116, 36),
        "legacy_emode": _bits(cfg, 168, 8),
    }


def staking_apys() -> Dict[str, Optional[float]]:
    """
    Rendimento di staking di ogni LST, dal pool di riferimento su Ethereum
    in DefiLlama (stessa API, gia' in cache, dello scanner). Si puo'
    forzare con LOOP_STAKING_APY_<ASSET>.
    """
    import os
    out: Dict[str, Optional[float]] = {}
    pools: List[Dict[str, Any]] = []
    try:
        data = http_client.get_json(config.DEFILLAMA_POOLS_URL, timeout=config.HTTP_TIMEOUT, cache_ttl=300)
        pools = (data or {}).get("data") or []
    except Exception as exc:
        logger.warning("APY di staking non disponibili da DefiLlama: %s", exc)
    for sym, meta in config.LOOP_TOKENS.items():
        override = os.getenv(f"LOOP_STAKING_APY_{sym}")
        if override:
            try:
                out[sym] = float(override)
                continue
            except ValueError:
                pass
        project, symbol = meta["staking"]
        best = None
        for p in pools:
            if (p.get("project") == project and str(p.get("symbol", "")).upper() == symbol
                    and p.get("chain") == "Ethereum"):
                if best is None or float(p.get("tvlUsd") or 0) > float(best.get("tvlUsd") or 0):
                    best = p
        value = None
        if best is not None:
            value = best.get("apyBase")
            if value is None:
                value = best.get("apy")
        out[sym] = float(value) if value is not None else None
    return out


# ================================================================ lettura on-chain
class AaveLoopReader:
    """Tutto quello che serve sapere su Aave per un loop, letto dalla chain."""

    def __init__(self, client):
        self.client = client
        self.w3 = client.w3
        self.pool = self.w3.eth.contract(address=Web3.to_checksum_address(config.AAVE_V3_POOL),
                                         abi=LOOP_POOL_ABI)
        self._oracle = None
        self._emode_cache: Dict[int, Optional[Dict[str, Any]]] = {}

    def reserve(self, asset: str) -> Dict[str, Any]:
        data = self.pool.functions.getReserveData(Web3.to_checksum_address(asset)).call()
        out = parse_reserve_config(data[0])
        out.update({
            "id": int(data[7]),
            "supply_apy": rate_to_apy(data[2]),
            "borrow_apy": rate_to_apy(data[4]),
            "a_token": Web3.to_checksum_address(data[8]),
            "debt_token": Web3.to_checksum_address(data[10]),
        })
        if out["a_token"] == "0x0000000000000000000000000000000000000000":
            raise ValueError("asset non listato su Aave V3")
        return out

    def token(self, address: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=TOKEN_ABI)

    def oracle_price(self, asset: str) -> float:
        """Prezzo in USD che Aave usa per l'health factor (8 decimali)."""
        if self._oracle is None:
            provider = self.pool.functions.ADDRESSES_PROVIDER().call()
            oracle = self.w3.eth.contract(address=provider, abi=PROVIDER_ABI).functions.getPriceOracle().call()
            self._oracle = self.w3.eth.contract(address=oracle, abi=ORACLE_ABI)
        return int(self._oracle.functions.getAssetPrice(Web3.to_checksum_address(asset)).call()) / 1e8

    def user_emode(self, user: str = None) -> int:
        return int(self.pool.functions.getUserEMode(Web3.to_checksum_address(user or self.client.address)).call())

    # -------- e-mode
    def _category(self, cat: int) -> Optional[Dict[str, Any]]:
        if cat in self._emode_cache:
            return self._emode_cache[cat]
        info = None
        try:
            ltv, lt, _ = self.pool.functions.getEModeCategoryCollateralConfig(cat).call()
            if int(lt) > 0:
                info = {
                    "id": cat, "ltv": int(ltv) / 10_000, "lt": int(lt) / 10_000,
                    "collateral": int(self.pool.functions.getEModeCategoryCollateralBitmap(cat).call()),
                    "borrowable": int(self.pool.functions.getEModeCategoryBorrowableBitmap(cat).call()),
                }
        except Exception:
            try:
                data = self.pool.functions.getEModeCategoryData(cat).call()
                if int(data[1]) > 0:
                    info = {"id": cat, "ltv": int(data[0]) / 10_000, "lt": int(data[1]) / 10_000,
                            "legacy": True}
            except Exception:
                info = None
        self._emode_cache[cat] = info
        return info

    def emode_for(self, coll: Dict[str, Any], debt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Categoria e-mode in cui `coll` e' collaterale e `debt` si puo' prendere in prestito."""
        forced = config.LOOP_EMODE_CATEGORY
        if forced == 0:
            return None
        candidates = [forced] if forced > 0 else range(1, EMODE_SCAN_MAX + 1)
        best = None
        for cat in candidates:
            info = self._category(cat)
            if not info:
                continue
            if info.get("legacy"):
                ok = coll["legacy_emode"] == cat and debt["legacy_emode"] == cat
            else:
                ok = bool(info["collateral"] >> coll["id"] & 1) and bool(info["borrowable"] >> debt["id"] & 1)
            if ok and (best is None or info["lt"] > best["lt"]):
                best = info
        if forced > 0 and best is None:
            logger.warning("LOOP_EMODE_CATEGORY=%s non ammette questo loop: niente e-mode", forced)
        return best


# ================================================================ gestore
class LoopManager:
    """
    Mercati, posizioni ed esecuzione dei loop. Vive dentro YieldManager e ne
    usa client, paper book, registro delle posizioni e swap Uniswap: per il
    resto del bot un loop e' un pool come gli altri, con pool_id "loop:<ASSET>".
    """

    def __init__(self, manager):
        self.manager = manager
        self.client = manager.client
        self._reader: Optional[AaveLoopReader] = None
        self.markets: Dict[str, Dict[str, Any]] = {}

    @property
    def reader(self) -> AaveLoopReader:
        if self._reader is None:
            self._reader = AaveLoopReader(self.client)
        return self._reader

    @property
    def paper(self):
        return self.manager.paper

    @property
    def store(self):
        return self.manager.store

    # ------------------------------------------------------------ mercati
    def refresh(self) -> Dict[str, Dict[str, Any]]:
        stakes = staking_apys()
        markets: Dict[str, Dict[str, Any]] = {}
        try:
            weth = self.reader.reserve(config.WETH)
            weth_price = self.reader.oracle_price(config.WETH)
            weth_cash = self.client.balance_of(config.WETH, weth["a_token"]) / 1e18
            weth_debt = self.reader.token(weth["debt_token"]).functions.totalSupply().call() / 1e18
        except Exception as exc:
            logger.warning("Riserva WETH di Aave non leggibile: %s", exc)
            self.markets = {sym: self._unavailable(sym, f"riserva WETH non leggibile: {exc}")
                            for sym in config.LOOP_ASSETS}
            return self.markets
        for sym in config.LOOP_ASSETS:
            try:
                markets[sym] = self._market(sym, stakes.get(sym), weth, weth_price, weth_cash, weth_debt)
            except Exception as exc:
                logger.warning("Loop %s non valutabile: %s", sym, exc)
                markets[sym] = self._unavailable(sym, str(exc))
        self.markets = markets
        return markets

    @staticmethod
    def _unavailable(sym: str, reason: str) -> Dict[str, Any]:
        meta = config.LOOP_TOKENS[sym]
        return {"loop_id": LOOP_PREFIX + sym, "asset": sym, "symbol": f"{meta['symbol']}/WETH",
                "executable": False, "reason": reason, "net_apy": None}

    def _market(self, sym: str, staking: Optional[float], weth: Dict[str, Any], weth_price: float,
                weth_cash: float, weth_debt: float) -> Dict[str, Any]:
        meta = config.LOOP_TOKENS[sym]
        token = meta["address"]
        onchain_symbol = self.client.symbol(token)
        if onchain_symbol.upper() != sym:
            raise ValueError(f"all'indirizzo {token} c'e' '{onchain_symbol}', non {meta['symbol']}")
        coll = self.reader.reserve(token)
        emode = self.reader.emode_for(coll, weth)
        ltv = emode["ltv"] if emode else coll["ltv"]
        lt = emode["lt"] if emode else coll["lt"]
        lev = target_leverage(ltv, lt) if lt > 0 else 1.0
        coll_apy = (staking or 0.0) + coll["supply_apy"]
        price = self.reader.oracle_price(token)
        supplied = self.reader.token(coll["a_token"]).functions.totalSupply().call() / 10 ** meta["decimals"]
        supply_room = (coll["supply_cap"] - supplied) if coll["supply_cap"] else float("inf")
        borrow_room = (weth["borrow_cap"] - weth_debt) if weth["borrow_cap"] else float("inf")

        reason = ""
        if staking is None:
            reason = "APY di staking sconosciuto (imposta LOOP_STAKING_APY_%s)" % sym
        elif not coll["active"] or coll["frozen"] or coll["paused"]:
            reason = f"riserva {meta['symbol']} non attiva, congelata o in pausa"
        elif not weth["active"] or weth["frozen"] or weth["paused"] or not weth["borrowing"]:
            reason = "prestiti WETH non disponibili"
        elif ltv <= 0:
            reason = f"{meta['symbol']} non e' collaterale su Aave (LTV 0)"
        elif lev < 1.2:
            reason = f"leva raggiungibile {lev:.2f}x troppo bassa"
        elif supply_room <= 0:
            reason = f"supply cap di {meta['symbol']} pieno"
        elif min(borrow_room, weth_cash) <= 0:
            reason = "nessun WETH disponibile da prendere in prestito"

        net = net_apy(lev, coll_apy, weth["borrow_apy"])
        return {
            "loop_id": LOOP_PREFIX + sym,
            "asset": sym,
            "symbol": f"{meta['symbol']}/WETH",
            "token": Web3.to_checksum_address(token),
            "a_token": coll["a_token"],
            "debt_token": weth["debt_token"],
            "staking_apy": round(staking, 4) if staking is not None else None,
            "supply_apy": round(coll["supply_apy"], 4),
            "collateral_apy": round(coll_apy, 4),
            "borrow_apy": round(weth["borrow_apy"], 4),
            "ltv": ltv,
            "lt": lt,
            "emode_id": emode["id"] if emode else 0,
            "leverage": round(lev, 4),
            "net_apy": round(net, 4),
            "hf_target": round(health_factor(lev, lev - 1, lt), 4) if lev > 1 else None,
            "price_usd": price,
            "weth_price_usd": weth_price,
            # quanto capitale proprio (in USD) ci sta ancora, dati i cap e la liquidita'
            "capacity_usd": round(self._capacity_usd(lev, supply_room * price,
                                                     min(borrow_room, weth_cash) * weth_price), 2),
            "weth_liquidity_usd": round(weth_cash * weth_price, 2),
            "executable": not reason,
            "reason": reason,
        }

    @staticmethod
    def _capacity_usd(lev: float, supply_room_usd: float, borrow_room_usd: float) -> float:
        caps = [supply_room_usd / lev]
        if lev > 1:
            caps.append(borrow_room_usd / (lev - 1))
        # meta' dello spazio: non si vuole essere chi riempie il cap
        return max(0.0, min(caps) * 0.5)

    def pseudo_pools(self) -> List[Dict[str, Any]]:
        """I loop convenienti, nella stessa forma dei pool dello scanner."""
        pools = []
        for m in self.markets.values():
            if not m.get("executable") or m["net_apy"] < config.LOOP_MIN_NET_APY:
                continue
            pools.append({
                "pool_id": m["loop_id"],
                "project": "aave-v3",
                "symbol": m["symbol"],
                "pool_meta": f"loop {m['leverage']:.2f}x",
                "asset": m["asset"],
                "asset_address": m["token"].lower(),
                "addresses": [],
                "tvl_usd": m["weth_liquidity_usd"],
                "apy": m["net_apy"],
                "apy_base": m["net_apy"],
                "apy_reward": 0.0,
                "apy_mean_30d": m["net_apy"],
                "apy_change_7d": 0.0,
                "effective_apy": m["net_apy"],
                "stablecoin": False,
                "history_days": 0,
                "reward_tokens": [],
                "score": m["net_apy"],
                "status": "approved",
                "reason": "",
                "adapter": LOOP_KIND,
                "contract": config.AAVE_V3_POOL,
                "loop": m,
            })
        return pools

    def market_for(self, pool_id: str) -> Optional[Dict[str, Any]]:
        return self.markets.get(str(pool_id)[len(LOOP_PREFIX):]) if is_loop(pool_id) else None

    # ------------------------------------------------------------ posizioni
    def position_row(self, pid: str, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        m = self.market_for(pid) or {}
        if self.paper:
            pos = self.paper.positions.get(pid)
            if not pos:
                return None
            coll, debt, lt = float(pos["collateral_eth"]), float(pos["debt_eth"]), float(pos["lt"])
            value = float(pos["value_usd"])
            coll_usd = coll * (value / (coll - debt)) if coll > debt else 0.0
            debt_usd = coll_usd - value
        else:
            live = self.live_position(pid)
            if live is None:
                return None
            coll_usd, debt_usd, lt = live["collateral_usd"], live["debt_usd"], live["lt"]
            value = coll_usd - debt_usd
        lev = coll_usd / value if value > 0 else float("inf")
        apy_now = net_apy(lev, m["collateral_apy"], m["borrow_apy"]) if m.get("collateral_apy") is not None else None
        opened = meta.get("opened_at") or time.time()
        deposited = float(meta.get("deposited_usd") or 0.0)
        return {
            "pool_id": pid,
            "project": "aave-v3",
            "symbol": meta.get("symbol") or m.get("symbol"),
            "pool_meta": "loop",
            "asset": meta.get("asset") or m.get("asset"),
            "value_usd": round(value, 4),
            "deposited_usd": round(deposited, 4),
            "pnl_usd": round(value - deposited, 4) if deposited else None,
            "apy_now": round(apy_now, 4) if apy_now is not None else None,
            "entry_apy": meta.get("entry_apy"),
            "tvl_now": None,
            "entry_tvl": None,
            "held_hours": round((time.time() - opened) / 3600, 2),
            "withdrawable_usd": None,
            "in_universe": bool(m.get("executable")),
            "loop": {
                "collateral_usd": round(coll_usd, 4),
                "debt_usd": round(debt_usd, 4),
                "leverage": round(lev, 4) if math.isfinite(lev) else None,
                "health_factor": round(health_factor(coll_usd, debt_usd, lt), 4) if debt_usd > 0 else None,
                "liq_threshold": lt,
                "borrow_apy": m.get("borrow_apy"),
                "collateral_apy": m.get("collateral_apy"),
            },
        }

    def live_position(self, pid: str) -> Optional[Dict[str, Any]]:
        sym = pid[len(LOOP_PREFIX):]
        meta = config.LOOP_TOKENS.get(sym)
        if not meta:
            return None
        m = self.markets.get(sym) or {}
        coll_res = self.reader.reserve(meta["address"])
        weth_res = self.reader.reserve(config.WETH)
        coll_raw = int(self.reader.token(coll_res["a_token"]).functions.balanceOf(self.client.address).call())
        debt_raw = int(self.reader.token(weth_res["debt_token"]).functions.balanceOf(self.client.address).call())
        price = self.reader.oracle_price(meta["address"])
        weth_price = self.reader.oracle_price(config.WETH)
        lt = m.get("lt") or coll_res["lt"]
        if m.get("emode_id") and self.reader.user_emode() != m["emode_id"]:
            lt = coll_res["lt"]
        return {
            "token": meta["address"], "coll_raw": coll_raw, "debt_raw": debt_raw,
            "price": price, "weth_price": weth_price, "lt": lt,
            "collateral_usd": coll_raw / 10 ** meta["decimals"] * price,
            "debt_usd": debt_raw / 1e18 * weth_price,
        }

    def accrue_paper(self):
        if not self.paper:
            return 0.0
        eth_price = self.manager.prices().get("WETH")
        if not eth_price:
            return 0.0
        return self.paper.accrue_loops(self.markets, eth_price)

    def risk_trigger(self, row: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
        """Uscita automatica: health factor troppo basso o carry negativo che dura."""
        info = row.get("loop") or {}
        hf = info.get("health_factor")
        if hf is not None and hf < config.LOOP_MIN_HEALTH_FACTOR:
            return f"HEALTH_FACTOR_{hf:.2f}"
        apy = row.get("apy_now")
        if apy is not None and apy < config.LOOP_EXIT_NET_APY:
            meta["negative_cycles"] = int(meta.get("negative_cycles", 0)) + 1
            self.store.save()
            if meta["negative_cycles"] >= config.LOOP_EXIT_CYCLES:
                return f"CARRY_NEGATIVO_{apy:.1f}%"
        elif meta.get("negative_cycles"):
            meta["negative_cycles"] = 0
            self.store.save()
        return None

    # ------------------------------------------------------------ apertura
    def open(self, pool: Dict[str, Any], amount_usd: float, base: Dict[str, Any]) -> Dict[str, Any]:
        m = pool["loop"]
        base = dict(base, leverage=m["leverage"], hf_target=m["hf_target"],
                    staking_apy=m["staking_apy"], borrow_apy=m["borrow_apy"])
        adapter = {"kind": LOOP_KIND, "address": config.AAVE_V3_POOL, "asset": m["token"]}
        if self.paper:
            eth_price = self.manager.prices().get("WETH")
            if not eth_price:
                return dict(base, status="error", reason="prezzo di ETH non disponibile")
            res = self.paper.open_loop(pool["pool_id"], m, amount_usd, eth_price)
            self.store.record_deposit(pool, adapter, res["deposited_usd"], 0.0)
            return dict(base, status="paper", **res)
        if config.DRY_RUN:
            return dict(base, status="dry_run", plan=self.plan_open(m, amount_usd))
        res = self._live_open(m, amount_usd)
        self.store.record_deposit(pool, adapter, amount_usd, 0.0)
        return dict(base, **res)

    def plan_open(self, m: Dict[str, Any], amount_usd: float) -> List[str]:
        borrows, coll, debt = plan_iterations(1.0, m["leverage"], m["ltv"])
        steps = []
        if m["emode_id"]:
            steps.append(f"Aave setUserEMode({m['emode_id']}) se non gia' attivo")
        steps.append(f"swap ${amount_usd:.2f} USDC -> {m['symbol'].split('/')[0]} su Uniswap V3")
        steps.append("approve + supply del LST su Aave V3")
        for i, b in enumerate(borrows, 1):
            steps.append(f"giro {i}: borrow {b * amount_usd:.2f}$ di WETH, swap in LST, supply")
        steps.append(f"risultato atteso: leva {coll:.2f}x, HF ~{health_factor(coll, debt, m['lt']):.2f}, "
                     f"APY netto ~{m['net_apy']:.2f}%")
        return steps

    def _send(self, fn, description: str) -> Dict[str, Any]:
        return self.manager._send(fn, description)

    def _supply(self, token: str, raw: int) -> List[Dict[str, Any]]:
        txs = []
        approve = self.client.ensure_allowance(token, config.AAVE_V3_POOL, raw)
        if approve:
            txs.append(approve)
        txs.append(self._send(self.reader.pool.functions.supply(token, int(raw), self.client.address, 0),
                              "Aave supply LST"))
        return txs

    def _swap_delta(self, token_in: str, token_out: str, raw: int, label: str,
                    txs: List[Dict[str, Any]]) -> int:
        before = self.client.balance_of(token_out)
        txs.append(self.manager._swap(token_in, token_out, int(raw), label))
        got = self.client.balance_of(token_out) - before
        if got <= 0:
            raise BaseChainError(f"lo swap {label} non ha prodotto nulla")
        return got

    def _live_open(self, m: Dict[str, Any], amount_usd: float) -> Dict[str, Any]:
        token = m["token"]
        name = m["symbol"].split("/")[0]
        pool = self.reader.pool
        txs: List[Dict[str, Any]] = []
        if m["emode_id"] and self.reader.user_emode() != m["emode_id"]:
            txs.append(self._send(pool.functions.setUserEMode(int(m["emode_id"])),
                                  f"Aave e-mode {m['emode_id']}"))

        got = self._swap_delta(config.USDC, token, int(amount_usd * 1e6), f"USDC -> {name}", txs)
        txs += self._supply(token, got)

        pid = m["loop_id"]
        for _ in range(config.LOOP_MAX_ITERATIONS):
            pos = self.live_position(pid)
            equity = pos["collateral_usd"] - pos["debt_usd"]
            room = pos["collateral_usd"] * m["ltv"] * config.LOOP_LTV_BUFFER - pos["debt_usd"]
            borrow_usd = min(equity * (m["leverage"] - 1) - pos["debt_usd"], room)
            if borrow_usd < max(config.DUST_USD, equity * 0.01):
                break
            borrow_raw = int(borrow_usd / pos["weth_price"] * 1e18)
            before = self.client.balance_of(config.WETH)
            txs.append(self._send(pool.functions.borrow(Web3.to_checksum_address(config.WETH), borrow_raw,
                                                        VARIABLE_RATE, 0, self.client.address),
                                  "Aave borrow WETH"))
            weth_got = self.client.balance_of(config.WETH) - before
            got = self._swap_delta(config.WETH, token, weth_got, f"WETH -> {name}", txs)
            txs += self._supply(token, got)

        final = self.live_position(pid)
        value = final["collateral_usd"] - final["debt_usd"]
        hf = health_factor(final["collateral_usd"], final["debt_usd"], final["lt"])
        return {"status": "success", "transactions": txs, "deposited_usd": round(value, 4),
                "leverage": round(final["collateral_usd"] / value, 4) if value > 0 else None,
                "health_factor": round(hf, 4) if math.isfinite(hf) else None}

    # ------------------------------------------------------------ chiusura
    def close(self, pos: Dict[str, Any], fraction: float, base: Dict[str, Any]) -> Dict[str, Any]:
        pid = pos["pool_id"]
        if self.paper:
            eth_price = self.manager.prices().get("WETH")
            if not eth_price:
                return dict(base, status="error", reason="prezzo di ETH non disponibile")
            res = self.paper.close_loop(pid, fraction, eth_price)
            self.store.record_withdraw(pid, fraction)
            return dict(base, status="paper", **res)
        if config.DRY_RUN:
            info = pos.get("loop") or {}
            return dict(base, status="dry_run", plan=[
                f"ridurre il debito WETH del {fraction:.0%} (~${(info.get('debt_usd') or 0) * fraction:.2f}) "
                "a giri: withdraw LST, swap in WETH, repay",
                f"withdraw del {fraction:.0%} del collaterale restante e swap LST -> USDC",
            ])
        res = self._live_close(pid, fraction)
        remaining = self.live_position(pid)
        gone = remaining is None or remaining["coll_raw"] == 0 or (
            remaining["collateral_usd"] - remaining["debt_usd"] < config.DUST_USD)
        self.store.record_withdraw(pid, 1.0 if gone else fraction)
        return dict(base, **res)

    def _live_close(self, pid: str, fraction: float) -> Dict[str, Any]:
        full = fraction >= 0.999
        sym = pid[len(LOOP_PREFIX):]
        token = Web3.to_checksum_address(config.LOOP_TOKENS[sym]["address"])
        weth = Web3.to_checksum_address(config.WETH)
        name = config.LOOP_TOKENS[sym]["symbol"]
        pool = self.reader.pool
        me = self.client.address
        txs: List[Dict[str, Any]] = []
        start = self.live_position(pid)
        usdc_before = self.client.balance_of(config.USDC)
        weth_before = self.client.balance_of(weth)
        target_debt = 0 if full else int(start["debt_raw"] * (1 - fraction))
        target_coll = 0 if full else int(start["coll_raw"] * (1 - fraction))
        # si ritira collaterale solo finche' l'HF del loop resta sopra questa soglia
        safe_hf = max(config.LOOP_MIN_HEALTH_FACTOR, 1.02)

        for _ in range(config.LOOP_MAX_ITERATIONS * 2):
            p = self.live_position(pid)
            if p["debt_raw"] <= target_debt:
                break
            need_usd = (p["debt_raw"] - target_debt) / 1e18 * p["weth_price"] * 1.01
            min_coll_usd = p["debt_usd"] * safe_hf / p["lt"] if p["lt"] > 0 else p["collateral_usd"]
            free_usd = p["collateral_usd"] - min_coll_usd
            w_usd = min(free_usd, need_usd)
            if w_usd <= config.DUST_USD / 10:
                raise BaseChainError(
                    f"impossibile ritirare collaterale senza scendere sotto HF {safe_hf:.2f}: "
                    "servono WETH nel wallet per rimborsare")
            w_raw = min(int(w_usd / p["price"] * 1e18), p["coll_raw"])
            before = self.client.balance_of(token)
            txs.append(self._send(pool.functions.withdraw(token, w_raw, me), f"Aave withdraw {name}"))
            got = self.client.balance_of(token) - before
            weth_got = self._swap_delta(token, weth, got, f"{name} -> WETH", txs)
            # Aave rimborsa al massimo il debito: l'eccedenza resta nel wallet
            approve = self.client.ensure_allowance(weth, config.AAVE_V3_POOL, weth_got)
            if approve:
                txs.append(approve)
            txs.append(self._send(pool.functions.repay(weth, int(weth_got), VARIABLE_RATE, me),
                                  "Aave repay WETH"))

        p = self.live_position(pid)
        if full and p["debt_raw"] > 0:
            raise BaseChainError(f"debito residuo {p['debt_raw'] / 1e18:.6f} WETH dopo lo smontaggio")
        withdraw_raw = MAX_UINT256 if full else max(0, p["coll_raw"] - target_coll)
        if withdraw_raw:
            before = self.client.balance_of(token)
            txs.append(self._send(pool.functions.withdraw(token, withdraw_raw, me), f"Aave withdraw {name}"))
            got = self.client.balance_of(token) - before
            if got > 0:
                self._swap_delta(token, config.USDC, got, f"{name} -> USDC", txs)
        leftover = self.client.balance_of(weth) - weth_before
        if leftover > 0:
            self._swap_delta(weth, config.USDC, leftover, "WETH -> USDC", txs)
        received = (self.client.balance_of(config.USDC) - usdc_before) / 1e6
        return {"status": "success", "transactions": txs, "withdrawn_usd": round(received, 4)}


def is_loop(pool_id: Any) -> bool:
    return str(pool_id or "").startswith(LOOP_PREFIX)
