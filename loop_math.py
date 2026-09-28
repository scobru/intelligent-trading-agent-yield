"""
Conti del looping su Aave V3, separati dall'I/O per poterli testare.

Un loop parte da un LST (wstETH, cbETH) depositato come collaterale e ripete
"prendi WETH in prestito -> ricompralo in LST -> deposita" finche' il
collaterale vale `leva` volte il capitale. Ogni prestito e' limitato dall'LTV
del collaterale accumulato fino a quel momento: per arrivare a 2x servono
piu' giri, un prestito unico da (leva - 1) x capitale verrebbe rifiutato.

    APY netto = leva x (staking + supply) - (leva - 1) x borrow
    HF        = collaterale x soglia di liquidazione / debito
"""

import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import config

logger = logging.getLogger(__name__)

SECONDS_PER_YEAR = 31_536_000
EMODE_SCAN_MAX = 16

# pool DefiLlama (su Ethereum) che misura lo staking di ogni LST
STAKING_SOURCES = {
    "wstETH": ("lido", "STETH"),
    "cbETH": ("coinbase-wrapped-staked-eth", "CBETH"),
}


def rate_to_apy(rate_ray: int) -> float:
    """Tasso Aave (APR in ray, composto al secondo) -> APY in percentuale."""
    apr = int(rate_ray) / 1e27
    return ((1 + apr / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1) * 100


def net_apy(leverage: float, collateral_apy: float, borrow_apy: float) -> float:
    return leverage * collateral_apy - (leverage - 1) * borrow_apy


def health_factor(collateral: float, debt: float, liq_threshold: float) -> float:
    if debt <= 0:
        return float("inf")
    return collateral * liq_threshold / debt


def leverage_for_hf(hf: float, liq_threshold: float) -> float:
    """Leva che lascia l'health factor a `hf` (HF = LT x L / (L - 1))."""
    if hf <= liq_threshold:
        return float("inf")
    return hf / (hf - liq_threshold)


def max_leverage_for_ltv(ltv: float) -> float:
    """Leva massima raggiungibile a forza di giri con l'LTV dato."""
    usable = ltv * config.LOOP_LTV_BUFFER
    if usable >= 1:
        return float("inf")
    return 1 / (1 - usable)


def safe_leverage(ltv: float, liq_threshold: float, requested: float = None) -> float:
    """La minore fra leva richiesta, leva per l'HF obiettivo e leva raggiungibile con l'LTV."""
    if ltv <= 0 or liq_threshold <= 0:
        return 1.0
    requested = config.LOOP_MAX_LEVERAGE if requested is None else requested
    lev = min(requested,
              leverage_for_hf(config.LOOP_TARGET_HEALTH_FACTOR, liq_threshold),
              max_leverage_for_ltv(ltv))
    return max(1.0, lev)


def plan_iterations(equity: float, leverage: float, ltv: float,
                    max_iterations: int = None) -> Tuple[List[float], float, float]:
    """Prestiti successivi per arrivare alla leva: (prestiti, collaterale, debito)."""
    max_iterations = config.LOOP_MAX_ITERATIONS if max_iterations is None else max_iterations
    coll, debt = equity, 0.0
    target_debt = equity * (leverage - 1)
    borrows: List[float] = []
    for _ in range(max_iterations):
        amount = min(target_debt - debt, coll * ltv * config.LOOP_LTV_BUFFER - debt)
        if amount <= equity * 1e-3:
            break
        borrows.append(amount)
        debt += amount
        coll += amount
    return borrows, coll, debt


def next_borrow_usd(collateral_usd: float, debt_usd: float, leverage: float, ltv: float) -> float:
    """Quanto prendere in prestito al prossimo giro (0 = leva raggiunta)."""
    equity = collateral_usd - debt_usd
    room = collateral_usd * ltv * config.LOOP_LTV_BUFFER - debt_usd
    return max(0.0, min(equity * (leverage - 1) - debt_usd, room))


def next_unwind_usd(collateral_usd: float, debt_usd: float, target_debt_usd: float,
                    liq_threshold: float, safe_hf: float) -> float:
    """
    Collaterale da ritirare al prossimo giro di smontaggio: quanto serve a
    rimborsare fino al debito obiettivo (piu' l'1% di margine per lo swap),
    ma mai tanto da portare l'HF del loop sotto `safe_hf`.
    """
    need = max(0.0, debt_usd - target_debt_usd) * 1.01
    if debt_usd <= 0:
        return need
    free = collateral_usd - debt_usd * safe_hf / liq_threshold if liq_threshold > 0 else 0.0
    return max(0.0, min(need, free))


def _bits(value: int, start: int, length: int) -> int:
    return (int(value) >> start) & ((1 << length) - 1)


def parse_reserve_config(cfg: int) -> Dict[str, Any]:
    """Campi della ReserveConfigurationMap di Aave V3 che servono al loop."""
    return {
        "ltv": _bits(cfg, 0, 16) / 10_000,
        "lt": _bits(cfg, 16, 16) / 10_000,
        "active": bool(_bits(cfg, 56, 1)),
        "frozen": bool(_bits(cfg, 57, 1)),
        "borrowing": bool(_bits(cfg, 58, 1)),
        "paused": bool(_bits(cfg, 60, 1)),
        "borrow_cap": _bits(cfg, 80, 36),     # in token interi, 0 = nessun cap
        "supply_cap": _bits(cfg, 116, 36),
        "legacy_emode": _bits(cfg, 168, 8),
    }


def pick_emode(category: Callable[[int], Optional[Dict[str, Any]]],
               coll: Dict[str, Any], debt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Categoria e-mode in cui `coll` e' collaterale e `debt` si puo' prendere in
    prestito, con la soglia di liquidazione piu' alta. `category(id)` restituisce
    {id, ltv, lt, collateral, borrowable} (Aave >= 3.2) o {id, ltv, lt, legacy}.
    """
    forced = config.LOOP_EMODE_CATEGORY
    if forced == 0:
        return None
    best = None
    for cat in ([forced] if forced > 0 else range(1, EMODE_SCAN_MAX + 1)):
        info = category(cat)
        if not info:
            continue
        if info.get("legacy"):
            ok = coll["legacy_emode"] == cat and debt["legacy_emode"] == cat
        else:
            ok = bool(info["collateral"] >> coll["id"] & 1) and bool(info["borrowable"] >> debt["id"] & 1)
        if ok and (best is None or info["lt"] > best["lt"]):
            best = info
    return best


def staking_apys() -> Dict[str, float]:
    """
    Staking di ogni LST dal suo pool di riferimento su DefiLlama (la stessa
    risposta, in cache, che usa lo scanner). LOOP_STAKING_APY_<ASSET> lo
    forza; se manca si usa `staking_yield` di KNOWN_ASSETS.
    """
    import http_client
    pools: List[Dict[str, Any]] = []
    try:
        data = http_client.get_json(config.DEFILLAMA_POOLS_URL, timeout=config.HTTP_TIMEOUT, cache_ttl=300)
        pools = (data or {}).get("data") or []
    except Exception as exc:
        logger.warning("APY di staking non disponibili da DefiLlama: %s", exc)
    out: Dict[str, float] = {}
    for sym, (project, symbol) in STAKING_SOURCES.items():
        override = os.getenv(f"LOOP_STAKING_APY_{sym.upper()}")
        if override:
            try:
                out[sym] = float(override)
                continue
            except ValueError:
                pass
        best = None
        for p in pools:
            if (p.get("project") == project and str(p.get("symbol", "")).upper() == symbol
                    and p.get("chain") == "Ethereum"
                    and (best is None or float(p.get("tvlUsd") or 0) > float(best.get("tvlUsd") or 0))):
                best = p
        value = None
        if best is not None:
            value = best.get("apyBase") if best.get("apyBase") is not None else best.get("apy")
        if value is None:
            value = config.KNOWN_ASSETS.get(sym, {}).get("staking_yield")
        if value is not None:
            out[sym] = float(value)
    return out
