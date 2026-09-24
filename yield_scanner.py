"""
Scoperta e screening delle opportunita' di rendimento passivo su Base.

La fonte e' DefiLlama Yields (https://yields.llama.fi/pools): un solo
endpoint pubblico con tutti i pool di tutte le chain, con APY base, APY da
ricompense, TVL, esposizione e rischio di impermanent loss. Qui si tiene
solo Base e si scarta, conservando il motivo, tutto cio' che non e':

  - un protocollo in allowlist (lending e vault con storia);
  - single-asset e senza impermanent loss (niente LP);
  - su un asset che il bot sa detenere (DEPOSIT_ASSETS);
  - con TVL sopra soglia e abbastanza giorni di storico;
  - con un APY credibile, non gonfiato da emissioni o da un outlier.

Ogni scarto finisce in dashboard: si vede *perche'* un pool non e' stato
considerato, esattamente come lo screening dei token nel bot degen.
"""

import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import config
import http_client

logger = logging.getLogger(__name__)

ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")

_last_rejected: List[Dict[str, Any]] = []
_universe_cache: Dict[str, Any] = {"time": 0.0, "data": None, "rejected": None}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _asset_by_address() -> Dict[str, str]:
    return {meta["address"].lower(): sym for sym, meta in config.KNOWN_ASSETS.items()}


def pool_addresses(pool: Dict[str, Any]) -> List[str]:
    """Indirizzi candidati per il contratto del pool, estratti dall'id DefiLlama."""
    seen: List[str] = []
    for match in ADDRESS_RE.findall(str(pool.get("pool") or "")):
        if match.lower() not in seen:
            seen.append(match.lower())
    return seen


def effective_apy(pool: Dict[str, Any]) -> float:
    """APY usato per confrontare i pool: base pieno, ricompense scontate."""
    base = _num(pool.get("apyBase"))
    reward = _num(pool.get("apyReward"))
    if base == 0 and reward == 0:
        base = _num(pool.get("apy"))
    return base + reward * config.REWARD_APY_WEIGHT


def _is_watched(pool: Dict[str, Any]) -> bool:
    if not config.POOL_WATCHLIST:
        return False
    keys = {str(pool.get("pool", "")).lower(), *pool_addresses(pool)}
    return bool(keys & config.POOL_WATCHLIST)


def _is_blocked(pool: Dict[str, Any]) -> bool:
    if not config.POOL_BLOCKLIST:
        return False
    keys = {str(pool.get("pool", "")).lower(), *pool_addresses(pool)}
    return bool(keys & config.POOL_BLOCKLIST)


def score_pool(pool: Dict[str, Any]) -> float:
    """
    Punteggio di ordinamento, trasparente e volutamente semplice:
    APY effettivo, smorzato dalla media a 30 giorni se l'APY di oggi e'
    un picco, e pesato per la profondita' del pool (log del TVL).
    """
    apy = effective_apy(pool)
    mean30 = _num(pool.get("apyMean30d"), apy)
    # un picco di oggi non vale piu' di 1.5 volte la media del mese
    if mean30 > 0:
        apy = min(apy, mean30 * 1.5)
    tvl = max(_num(pool.get("tvlUsd")), 1.0)
    depth = min(1.0, math.log10(tvl) / 8.0)   # $100M di TVL = profondita' piena
    return round(apy * (0.5 + 0.5 * depth), 4)


def screen_pool(pool: Dict[str, Any]) -> Optional[str]:
    """Restituisce il motivo dello scarto, o None se il pool passa."""
    assets = _asset_by_address()
    project = str(pool.get("project") or "").lower()
    watched = _is_watched(pool)

    if _is_blocked(pool):
        return "in blocklist"
    if not watched and project not in config.PROTOCOL_ALLOWLIST:
        return f"protocollo '{project}' non in allowlist"
    if str(pool.get("exposure") or "single").lower() != "single":
        return "esposizione multi-asset (LP)"
    if str(pool.get("ilRisk") or "no").lower() != "no":
        return "rischio di impermanent loss"

    underlying = [str(u).lower() for u in (pool.get("underlyingTokens") or []) if u]
    if len(underlying) != 1:
        return "asset sottostante non univoco"
    symbol = assets.get(underlying[0])
    if not symbol:
        return f"asset {pool.get('symbol')} non gestito dal bot"
    if symbol not in config.DEPOSIT_ASSETS:
        return f"asset {symbol} non in DEPOSIT_ASSETS"

    tvl = _num(pool.get("tvlUsd"))
    if tvl < config.SCREEN_MIN_TVL_USD:
        return f"TVL ${tvl:,.0f} sotto soglia"
    if pool.get("outlier"):
        return "APY marcato come outlier da DefiLlama"

    apy = effective_apy(pool)
    total = _num(pool.get("apy"))
    if apy < config.SCREEN_MIN_APY:
        return f"APY effettivo {apy:.2f}% sotto soglia"
    if total > config.SCREEN_MAX_APY:
        return f"APY {total:.1f}% sopra il massimo credibile"

    reward = _num(pool.get("apyReward"))
    if total > 0 and reward / total > config.SCREEN_MAX_REWARD_SHARE:
        return f"{reward / total:.0%} del rendimento viene da emissioni"

    history = int(_num(pool.get("count")))
    if history and history < config.SCREEN_MIN_HISTORY_DAYS:
        return f"solo {history} giorni di storico"

    return None


def normalize(pool: Dict[str, Any], status: str, reason: str = "") -> Dict[str, Any]:
    """Forma compatta usata dal resto del bot, dal prompt e dal DB."""
    underlying = [str(u).lower() for u in (pool.get("underlyingTokens") or []) if u]
    asset_symbol = _asset_by_address().get(underlying[0]) if underlying else None
    return {
        "pool_id": str(pool.get("pool")),
        "project": str(pool.get("project") or ""),
        "symbol": str(pool.get("symbol") or ""),
        "pool_meta": pool.get("poolMeta"),
        "asset": asset_symbol,
        "asset_address": underlying[0] if underlying else None,
        "addresses": pool_addresses(pool),
        "tvl_usd": round(_num(pool.get("tvlUsd")), 2),
        "apy": round(_num(pool.get("apy")), 4),
        "apy_base": round(_num(pool.get("apyBase")), 4),
        "apy_reward": round(_num(pool.get("apyReward")), 4),
        "apy_mean_30d": round(_num(pool.get("apyMean30d")), 4),
        "apy_change_7d": round(_num(pool.get("apyPct7D")), 4),
        "effective_apy": round(effective_apy(pool), 4),
        "stablecoin": bool(pool.get("stablecoin")),
        "history_days": int(_num(pool.get("count"))),
        "reward_tokens": pool.get("rewardTokens") or [],
        "score": score_pool(pool),
        "status": status,
        "reason": reason,
    }


def build_loop_pools(base_pools: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Costruisce e valuta le opportunita' di Looping (leveraged staking) su Aave V3.
    Collateral: wstETH / cbETH, Debito: WETH.
    """
    if not config.ENABLE_LOOPING:
        return [], []

    weth_pool = None
    lst_pools = {}
    for p in base_pools:
        if str(p.get("project") or "").lower() == "aave-v3":
            sym = str(p.get("symbol") or "").upper()
            if sym == "WETH":
                weth_pool = p
            elif sym in ("WSTETH", "CBETH"):
                lst_pools[sym] = p

    # Tasso di prestito WETH stimato
    weth_supply = _num(weth_pool.get("apyBase"), 1.4) if weth_pool else 1.4
    borrow_apy = max(weth_supply * 1.45, 2.0)

    loop_candidates = [
        ("wstETH", config.WSTETH, "WSTETH"),
        ("cbETH", config.CBETH, "CBETH"),
    ]

    approved, rejected = [], []
    leverage = config.LOOP_MAX_LEVERAGE

    for sym, addr, key in loop_candidates:
        p_data = lst_pools.get(key)
        if not p_data:
            continue
        staking_yield = config.KNOWN_ASSETS.get(sym, {}).get("staking_yield", 3.0)
        tvl_usd = _num(p_data.get("tvlUsd"), 30_000_000)
        supply_apy = _num(p_data.get("apyBase"), 0.0)

        # Net APY = (staking_yield + supply_apy) * L - borrow_apy * (L - 1)
        net_apy = (staking_yield + supply_apy) * leverage - borrow_apy * (leverage - 1.0)
        pool_id = f"aave-v3-loop-{sym.lower()}-weth"

        depth = min(1.0, math.log10(max(tvl_usd, 1.0)) / 8.0)
        score = round(net_apy * (0.5 + 0.5 * depth), 4)

        item = {
            "pool_id": pool_id,
            "project": "aave-v3-loop",
            "symbol": f"{sym}-WETH ({leverage:.1f}x Loop)",
            "pool_meta": f"Leveraged Staking: {sym} supply + WETH borrow ({leverage:.1f}x)",
            "asset": sym,
            "asset_address": addr,
            "borrow_asset": "WETH",
            "borrow_asset_address": config.WETH,
            "addresses": [config.AAVE_V3_POOL.lower()],
            "tvl_usd": round(tvl_usd, 2),
            "apy": round(net_apy, 4),
            "apy_base": round((staking_yield + supply_apy) * leverage, 4),
            "apy_reward": 0.0,
            "apy_mean_30d": round(net_apy, 4),
            "apy_change_7d": 0.0,
            "effective_apy": round(net_apy, 4),
            "stablecoin": False,
            "history_days": 180,
            "reward_tokens": [],
            "score": score,
            "is_loop": True,
            "leverage": leverage,
            "staking_yield": staking_yield,
            "borrow_apy": round(borrow_apy, 4),
            "target_health_factor": config.LOOP_TARGET_HEALTH_FACTOR,
        }

        if _is_blocked({"pool": pool_id, "addresses": [config.AAVE_V3_POOL]}):
            rejected.append(dict(item, status="rejected", reason="in blocklist"))
        elif net_apy < config.LOOP_MIN_NET_APY:
            rejected.append(dict(item, status="rejected",
                                 reason=f"Net APY del loop ({net_apy:.2f}%) sotto soglia minima ({config.LOOP_MIN_NET_APY:.2f}%)"))
        else:
            approved.append(dict(item, status="approved", reason=""))

    return approved, rejected


def fetch_base_pools() -> List[Dict[str, Any]]:
    data = http_client.get_json(
        config.DEFILLAMA_POOLS_URL, timeout=config.HTTP_TIMEOUT, cache_ttl=300
    )
    if not data or not isinstance(data, dict):
        raise RuntimeError("DefiLlama Yields non raggiungibile")
    pools = data.get("data") or []
    return [p for p in pools if str(p.get("chain")) == config.CHAIN_NAME]


def screen(pools: List[Dict[str, Any]]):
    approved, rejected = [], []
    for pool in pools:
        reason = screen_pool(pool)
        if reason:
            rejected.append(normalize(pool, "rejected", reason))
        else:
            approved.append(normalize(pool, "approved"))

    if config.ENABLE_LOOPING:
        loop_app, loop_rej = build_loop_pools(pools)
        approved.extend(loop_app)
        rejected.extend(loop_rej)

    approved.sort(key=lambda p: p["score"], reverse=True)
    return approved, rejected


def get_universe(force: bool = False) -> List[Dict[str, Any]]:
    """
    I migliori pool che hanno passato lo screening, ordinati per punteggio.
    Il risultato e' in cache per SCREEN_CACHE_SECONDS.
    """
    global _last_rejected
    now = time.time()
    if (not force and _universe_cache["data"] is not None
            and now - _universe_cache["time"] < config.SCREEN_CACHE_SECONDS):
        _last_rejected = _universe_cache["rejected"]
        return _universe_cache["data"]

    approved, rejected = screen(fetch_base_pools())
    universe = approved[: config.SCREEN_MAX_CANDIDATES]
    # quelli oltre il limite non sono scarti di rischio: li teniamo come tali
    for extra in approved[config.SCREEN_MAX_CANDIDATES:]:
        extra = dict(extra, status="rejected", reason="fuori dai migliori per punteggio")
        rejected.append(extra)

    _universe_cache.update({"time": now, "data": universe, "rejected": rejected})
    _last_rejected = rejected
    return universe


def get_last_rejected() -> List[Dict[str, Any]]:
    return list(_last_rejected)


def find_pool(pool_id: str) -> Optional[Dict[str, Any]]:
    """Dati correnti di un pool qualsiasi su Base (anche se non passa lo screening)."""
    if pool_id.startswith("aave-v3-loop"):
        res = pools_by_id([pool_id])
        return res.get(pool_id)
    try:
        for pool in fetch_base_pools():
            if str(pool.get("pool")) == pool_id:
                return normalize(pool, "tracked")
    except Exception as exc:
        logger.warning("Lookup pool %s fallito: %s", pool_id, exc)
    return None


def pools_by_id(pool_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Come find_pool, ma per piu' id con un solo download."""
    wanted = set(pool_ids)
    out: Dict[str, Dict[str, Any]] = {}
    if not wanted:
        return out

    loop_ids = {pid for pid in wanted if pid.startswith("aave-v3-loop")}
    pools = []
    try:
        pools = fetch_base_pools()
        for pool in pools:
            pid = str(pool.get("pool"))
            if pid in wanted:
                out[pid] = normalize(pool, "tracked")
    except Exception as exc:
        logger.warning("Lookup pool fallito: %s", exc)

    if loop_ids:
        loop_app, loop_rej = build_loop_pools(pools)
        for lp in loop_app + loop_rej:
            if lp["pool_id"] in loop_ids:
                out[lp["pool_id"]] = lp

    return out


def format_universe_for_prompt(universe: List[Dict[str, Any]]) -> str:
    if not universe:
        return "Nessun pool ha passato lo screening in questo ciclo."
    lines = [
        "pool_id | protocollo | asset | APY effettivo (base + reward) | media 30g | "
        "TVL | storico | eseguibile"
    ]
    for p in universe:
        if p.get("is_loop"):
            apy_desc = (
                f"{p['effective_apy']:.2f}% (net loop: {p.get('staking_yield', 0):.2f}% staking "
                f"@{p.get('leverage', 1):.1f}x - {p.get('borrow_apy', 0):.2f}% WETH borrow)"
            )
        else:
            apy_desc = f"{p['effective_apy']:.2f}% ({p['apy_base']:.2f}% + {p['apy_reward']:.2f}%)"
        lines.append(
            f"{p['pool_id']} | {p['project']}{' (' + p['pool_meta'] + ')' if p.get('pool_meta') else ''} | "
            f"{p['asset']} | {apy_desc} | "
            f"{p['apy_mean_30d']:.2f}% | ${p['tvl_usd']:,.0f} | {p['history_days']}g | "
            f"{'si (' + p['adapter'] + ')' if p.get('adapter') else 'no, solo monitoraggio'}"
        )
    return "\n".join(lines)
