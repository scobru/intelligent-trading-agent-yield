"""
Persistenza SQLite: snapshot del portafoglio, pool analizzati, operazioni,
errori. Stesso approccio dei bot fratelli: un file, nessun server.
"""

import json
import sqlite3
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    mode TEXT,
    total_value_usd REAL,
    idle_usd REAL,
    invested_usd REAL,
    weighted_apy REAL,
    est_yearly_usd REAL,
    positions_json TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS pool_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    pool_id TEXT,
    project TEXT,
    symbol TEXT,
    pool_meta TEXT,
    asset TEXT,
    tvl_usd REAL,
    apy REAL,
    apy_base REAL,
    apy_reward REAL,
    apy_mean_30d REAL,
    effective_apy REAL,
    score REAL,
    status TEXT,
    reason TEXT,
    adapter TEXT,
    contract TEXT
);
CREATE INDEX IF NOT EXISTS idx_pool_scans_created ON pool_scans(created_at);

CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    operation TEXT,
    pool TEXT,
    from_pool TEXT,
    project TEXT,
    symbol TEXT,
    amount_usd REAL,
    status TEXT,
    llm_reason TEXT,
    result_reason TEXT,
    trigger TEXT,
    decision_json TEXT,
    result_json TEXT,
    system_prompt TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT,
    error_type TEXT,
    message TEXT,
    traceback TEXT,
    context_json TEXT
);

CREATE TABLE IF NOT EXISTS bot_control (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection(path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.SQLITE_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str = None) -> None:
    with get_connection(path) as conn:
        conn.executescript(SCHEMA)


def _json(val: Any) -> str:
    try:
        return json.dumps(val, default=str)
    except Exception:
        return json.dumps(str(val))


def log_snapshot(status: Dict[str, Any], path: str = None) -> int:
    init_db(path)
    with get_connection(path) as conn:
        cur = conn.execute(
            """INSERT INTO snapshots (created_at, mode, total_value_usd, idle_usd, invested_usd,
               weighted_apy, est_yearly_usd, positions_json, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now(), status.get("mode"), status.get("total_value_usd"), status.get("idle_usd"),
             status.get("invested_usd"), status.get("weighted_apy"), status.get("est_yearly_usd"),
             _json(status.get("positions", [])), _json(status)),
        )
        return cur.lastrowid


def log_pool_scan(pools: List[Dict[str, Any]], path: str = None) -> int:
    init_db(path)
    now = _now()
    rows = [
        (now, p.get("pool_id"), p.get("project"), p.get("symbol"), p.get("pool_meta"), p.get("asset"),
         p.get("tvl_usd"), p.get("apy"), p.get("apy_base"), p.get("apy_reward"), p.get("apy_mean_30d"),
         p.get("effective_apy"), p.get("score"), p.get("status"),
         p.get("reason") or p.get("adapter_reason"), p.get("adapter"), p.get("contract"))
        for p in pools
    ]
    with get_connection(path) as conn:
        conn.executemany(
            """INSERT INTO pool_scans (created_at, pool_id, project, symbol, pool_meta, asset, tvl_usd,
               apy, apy_base, apy_reward, apy_mean_30d, effective_apy, score, status, reason,
               adapter, contract) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return len(rows)


def log_operation(decision: Dict[str, Any], result: Dict[str, Any],
                  system_prompt: str = None, path: str = None) -> int:
    init_db(path)
    decision = decision or {}
    result = result or {}
    with get_connection(path) as conn:
        cur = conn.execute(
            """INSERT INTO operations (created_at, operation, pool, from_pool, project, symbol,
               amount_usd, status, llm_reason, result_reason, trigger, decision_json, result_json,
               system_prompt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now(), result.get("operation") or decision.get("operation"),
             result.get("pool") or decision.get("pool"),
             result.get("from_pool") or decision.get("from_pool"),
             result.get("project"), result.get("symbol"), result.get("amount_usd"),
             result.get("status"), decision.get("reason"),
             result.get("reason") if result.get("status") in ("rejected", "error") else None,
             result.get("trigger"), _json(decision), _json(result), system_prompt),
        )
        return cur.lastrowid


def log_error(exc: BaseException, context: Optional[Dict[str, Any]] = None,
              source: str = "yield_agent", path: str = None) -> int:
    init_db(path)
    with get_connection(path) as conn:
        cur = conn.execute(
            """INSERT INTO errors (created_at, source, error_type, message, traceback, context_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (_now(), source, type(exc).__name__, str(exc),
             "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
             _json(context or {})),
        )
        return cur.lastrowid


def fetch_dashboard_data(path: str = None, limit: int = 50) -> Dict[str, Any]:
    """Tutto cio' che serve alla dashboard, in una lettura."""
    init_db(path)
    with get_connection(path) as conn:
        latest = conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        equity = conn.execute(
            """SELECT created_at, total_value_usd, invested_usd, weighted_apy FROM
               (SELECT * FROM snapshots ORDER BY id DESC LIMIT 500) ORDER BY id ASC"""
        ).fetchall()
        last_scan = conn.execute("SELECT MAX(created_at) AS t FROM pool_scans").fetchone()["t"]
        scans = conn.execute(
            "SELECT * FROM pool_scans WHERE created_at = ? ORDER BY status ASC, score DESC",
            (last_scan,),
        ).fetchall() if last_scan else []
        ops = conn.execute("SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        errors = conn.execute("SELECT id, created_at, source, error_type, message FROM errors "
                              "ORDER BY id DESC LIMIT 20").fetchall()

    status = json.loads(latest["raw_json"]) if latest else None
    return {
        "status": status,
        "snapshot_at": latest["created_at"] if latest else None,
        "equity": [dict(r) for r in equity],
        "scan_at": last_scan,
        "pools": [dict(r) for r in scans],
        "operations": [
            {k: r[k] for k in r.keys() if k not in ("system_prompt", "decision_json")}
            for r in ops
        ],
        "errors": [dict(r) for r in errors],
    }


def is_bot_paused() -> bool:
    """Verifica se il bot yield e' in stato di pausa."""
    try:
        init_db()
        with get_connection() as conn:
            row = conn.execute("SELECT value FROM bot_control WHERE key = 'is_paused'").fetchone()
            if row:
                return str(row["value"]).lower() in ("1", "true", "yes")
    except Exception:
        pass
    return False


def get_pause_info() -> Dict[str, Any]:
    """Recupera dettagli sullo stato di pausa del bot."""
    info = {"is_paused": False, "reason": "", "updated_at": ""}
    try:
        init_db()
        with get_connection() as conn:
            rows = conn.execute("SELECT key, value, updated_at FROM bot_control WHERE key IN ('is_paused', 'pause_reason')").fetchall()
            for row in rows:
                if row["key"] == "is_paused":
                    info["is_paused"] = str(row["value"]).lower() in ("1", "true", "yes")
                    info["updated_at"] = row["updated_at"]
                elif row["key"] == "pause_reason":
                    info["reason"] = row["value"]
    except Exception:
        pass
    return info


def set_bot_paused(paused: bool, reason: str = "") -> None:
    """Imposta o rimuove lo stato di pausa del bot yield."""
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    val = "1" if paused else "0"
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO bot_control (key, value, updated_at) VALUES ('is_paused', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
        """, (val, now))
        conn.execute("""
            INSERT INTO bot_control (key, value, updated_at) VALUES ('pause_reason', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
        """, (reason or ("Pausa da Coordinatore/Operatore" if paused else "Operativo"), now))

