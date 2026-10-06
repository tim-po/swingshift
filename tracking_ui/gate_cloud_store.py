"""Vendored slice of the coordinator's ``coord_shared.cloud_store`` for the dashgate.

The dashgate's ``/api/project/{project}/cloud-config`` routes need exactly two
things from the CONTEXT CLOUD settings registry (CONTEXT_CLOUD.md §7): the
``DEFAULTS`` table (validation + display) and ``set_config`` (persist a knob).
``coord_shared`` lives only in the coordinator's tree, so when the gate runs from
loopyard-devhome this module stands in for it — same table, same SQL, same
db-path resolution (``$SWARM_TEST_DATA_DIR`` sandbox, then ``$CORE_DB``, then
``$BOT_SQUAD_HOME/data/_tg/core.db``, BOT_SQUAD_HOME defaulting to this install
root like gate.py's HOME). Snapshot of the coordinator's cloud_store.py
DEFAULTS as of the decouple (coordinator commit ed8fdd9); keep in step if the registry
there grows a key the dashboard should expose.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

# install root (this file's tree), same default as gate.py HOME — no fixed home dir
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SANDBOX_ENV = "SWARM_TEST_DATA_DIR"  # coord_shared.harness.SANDBOX_ENV

DEFAULTS: dict[str, object] = {
    "conv_window_chars": 8000,
    "conv_window_chars_dispatcher": 4000,
    "brief_target_chars": 1500,
    "retrieval_accept_threshold": 0.09,
    "retrieval_k": 5,
    "retrieval_budget_chars": 8000,
    "distill_batch_chars": 12000,
    "idle_trigger_days": 7,
    "watermark_gap_chars": 24000,
    "max_concurrent_turns": 4,
    "staged_stale_s": 86400,
    "laws_budget_chars": 20000,
    "mode_quick_model": "claude-sonnet-5",
    "mode_quick_effort": "low",
    "mode_manual_model": "claude-opus-4-8",
    "mode_manual_effort": "high",
    "mode_planning_model": "claude-opus-4-8",
    "mode_planning_effort": "high",
    "mode_manager_model": "claude-opus-4-8",
    "mode_manager_effort": "high",
    "carrier_quick": "windowed",
    "carrier_manual": "resume",
    "carrier_planning": "resume",
    "carrier_manager": "resume",
    "latest_contract": "",
    "sprint_drive": "off",
    "sprint_max_iters": 8,
    "sprint_budget_wtok": 400000,
    "quick_max_iters": 3,
    "issues_sprint_threshold": 0,
    "worker_model": "claude-sonnet-5",
    "worker_effort": "medium",
    "worker_max_seconds": 900,
    "worker_judge_model": "claude-sonnet-5",
    "worker_scribe_model": "claude-haiku-4-5-20251001",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_exchange (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL,
    lane_id    TEXT NOT NULL,
    msg_id     TEXT NOT NULL,
    task_id    TEXT,
    role       TEXT NOT NULL,
    source     TEXT,
    mode       TEXT,
    text       TEXT NOT NULL,
    ts         REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'staged',
    entity_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_cloud_exchange_key
    ON cloud_exchange(project, lane_id, msg_id);
CREATE INDEX IF NOT EXISTS ix_cloud_exchange_status
    ON cloud_exchange(project, status, ts);
CREATE TABLE IF NOT EXISTS cloud_config (
    project    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_ts REAL NOT NULL,
    PRIMARY KEY (project, key)
);
"""


def _db_path() -> str:
    sandbox = os.environ.get(_SANDBOX_ENV)
    if sandbox:
        return os.path.join(sandbox, "core.db")
    if os.environ.get("CORE_DB"):
        return os.environ["CORE_DB"]
    home = os.environ.get("BOT_SQUAD_HOME", _REPO_ROOT)
    return os.path.join(home, "data", "_tg", "core.db")


def _connect(timeout: float = 10) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def set_config(project: str, key: str, value) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO cloud_config(project, key, value, updated_ts) "
            "VALUES(?,?,?,?) ON CONFLICT(project, key) DO UPDATE SET "
            "value=excluded.value, updated_ts=excluded.updated_ts",
            (project, key, json.dumps(value), time.time()))
        conn.commit()
    finally:
        conn.close()
