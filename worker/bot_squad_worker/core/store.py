"""Core data layer — metadata envelope + 4 entities + tags + edge graph (SQLite).

Implements the LOCKED product-core data model (``claude-memory/product-core/
MODEL.md``):

- **Class Table Inheritance.** One ``metadata`` envelope row per entity (1:1),
  holding the universal cross-cutting fields (``id``, ``type``, ``label``,
  ``trust_status``, timestamps). Each of the 4 entity types has its OWN payload
  table (task/event/worker/artifact) carrying only its type-specific fields.
- **Tags are first-class** (``tag`` table) and connect to entities through a
  many-to-many join — a "project"/"goal" is just the set of entities sharing a
  tag. A tag may point at a librarian-generated meta-artifact.
- **Edges** live in one ``edge`` table keyed on envelope ids, so any entity links
  to any entity. ``blocks`` is the CANONICAL ordering edge; ``depends-on`` is
  DERIVED at query time by reading incoming ``blocks`` edges (no reverse edges to
  keep in sync).
- **Two status axes:** *workflow* lives ON each entity (its own enum); *trust*
  (proposed→confirmed) lives in the envelope.

House style (matches ``tracking_store``): every function takes an explicit
``sqlite3.Connection``; ``connect()`` opens + migrates a db (tests use ``:memory:``
or a temp path). Timestamps are unix seconds (UTC); dates (due/start/end) are
ISO-8601 strings.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# --- vocabularies (the fixed model) ----------------------------------------
TYPES = ("task", "event", "artifact", "worker")
TRUST = ("proposed", "confirmed", "rejected")
EDGE_TYPES = (
    "blocks",          # canonical ordering (A blocks B); depends-on is derived
    "serves",          # task -> goal-artifact (intent)
    "produces",        # event -> task
    "prepares_for",    # task -> event
    "justified_by",    # task/event -> artifact (provenance)
    "assigned_to",     # worker -> task
    "attends",         # worker -> event
    "authored",        # worker -> artifact
    "duplicate_of",    # reconciliation
    "supersedes",      # reconciliation
    "relates_to",      # generic
)

# type -> (payload table, [type-specific columns], {column: default})
_PAYLOAD: dict[str, tuple[str, list[str], dict[str, Any]]] = {
    "task":     ("task",     ["status", "due_date", "priority",
                              "assignee", "risk", "approval", "archive_reason",
                              "ask", "recommend"],
                 {"status": "backlog", "priority": "P2",
                  "assignee": "unassigned", "approval": "none",
                  "archive_reason": None}),
    "event":    ("event",    ["status", "start_dt", "end_dt", "location"],
                 {"status": "scheduled"}),
    "worker":   ("worker",   ["capabilities", "channel", "interaction_style",
                              "kind", "role", "active"],
                 {"kind": "human", "role": "member", "active": 1}),
    "artifact": ("artifact", ["content", "state"], {"state": "raw"}),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    id            TEXT PRIMARY KEY,                         -- uuid4
    type          TEXT NOT NULL,                            -- task|event|artifact|worker
    label         TEXT,                                     -- optional human-readable
    trust_status  TEXT NOT NULL DEFAULT 'proposed',         -- proposed|confirmed|rejected
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metadata_type  ON metadata(type);
CREATE INDEX IF NOT EXISTS idx_metadata_trust ON metadata(trust_status);

CREATE TABLE IF NOT EXISTS task (
    metadata_id TEXT PRIMARY KEY REFERENCES metadata(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'backlog',            -- backlog|in_progress|done|archived
    due_date    TEXT,                                       -- ISO-8601, nullable
    priority    TEXT NOT NULL DEFAULT 'P2',                 -- P0|P1|P2|P3
    -- task-autonomy lifecycle (see core.lifecycle / AUTONOMY.md). EXISTENCE is
    -- never human-gated; the only human gate is approving HIGH-RISK AI-task
    -- EXECUTION (approval='needed').
    assignee    TEXT NOT NULL DEFAULT 'unassigned',         -- unassigned|ai|human
    risk        TEXT,                                       -- low|high|NULL
    approval    TEXT NOT NULL DEFAULT 'none',               -- none|needed|approved|rejected
    -- archive filing: why a task was filed away (status='archived'). NULL when
    -- the task is live on the board. 'not_relevant' = won't do; 'done' = already
    -- done then filed away. See core.lifecycle.archive / unarchive.
    archive_reason TEXT,                                    -- not_relevant|done|NULL
    -- THE DECISION ROW. A task's label is an INSTRUCTION written by a
    -- coordinator FOR a coordinator (200-2000 chars); a human at the approval
    -- centre needs the QUESTION, not the brief. 'ask' is that question in one
    -- line, 'recommend' is what the coordinator advises AND what it will do by
    -- DEFAULT if nobody ever answers — the default is what makes not-deciding
    -- safe. Both nullable: a row with no ask renders its label and SAYS the ask
    -- is missing, because a blank question reads as no question.
    ask         TEXT,
    recommend   TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_status ON task(status);

CREATE TABLE IF NOT EXISTS event (
    metadata_id TEXT PRIMARY KEY REFERENCES metadata(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'scheduled',          -- scheduled|attended|missed
    start_dt    TEXT,
    end_dt      TEXT,
    location    TEXT
);

CREATE TABLE IF NOT EXISTS worker (
    metadata_id       TEXT PRIMARY KEY REFERENCES metadata(id) ON DELETE CASCADE,
    capabilities      TEXT,                                 -- free text / csv for now
    channel           TEXT,
    interaction_style TEXT,                                 -- direct|subtle|...
    -- multi-user + role model (curated roster, added BY HAND — not auto-derived
    -- from contacts). kind/role/active drive role-based approval routing.
    kind              TEXT NOT NULL DEFAULT 'human',        -- human|ai
    role              TEXT NOT NULL DEFAULT 'member',       -- owner|admin|member
    active            INTEGER NOT NULL DEFAULT 1            -- 1=active, 0=deactivated
);

CREATE TABLE IF NOT EXISTS artifact (
    metadata_id TEXT PRIMARY KEY REFERENCES metadata(id) ON DELETE CASCADE,
    content     TEXT,
    state       TEXT NOT NULL DEFAULT 'raw'                 -- raw|distilled|canonical
);

CREATE TABLE IF NOT EXISTS tag (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL UNIQUE,
    meta_artifact_id TEXT REFERENCES metadata(id) ON DELETE SET NULL,  -- librarian rollup
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata_tag (
    metadata_id TEXT NOT NULL REFERENCES metadata(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
    PRIMARY KEY (metadata_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_metatag_tag ON metadata_tag(tag_id);

CREATE TABLE IF NOT EXISTS edge (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id    TEXT NOT NULL REFERENCES metadata(id) ON DELETE CASCADE,
    to_id      TEXT NOT NULL REFERENCES metadata(id) ON DELETE CASCADE,
    type       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (from_id, to_id, type)
);
CREATE INDEX IF NOT EXISTS idx_edge_from ON edge(from_id, type);
CREATE INDEX IF NOT EXISTS idx_edge_to   ON edge(to_id, type);   -- reverse lookup => derives depends-on
"""


# --- the in-memory view of an entity (envelope + payload) ------------------
@dataclass
class Entity:
    """Joined view: the metadata envelope plus the type-specific payload.

    ``payload`` holds the type's own fields (e.g. a task's status/due_date/
    priority). Convenience: ``e["status"]`` / ``e.get("priority")``.
    """
    id: str
    type: str
    label: Optional[str]
    trust: str
    created_at: int
    updated_at: int
    payload: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


def _now() -> int:
    return int(time.time())


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open (and migrate) a core db. ``:memory:`` for tests."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate_task_columns(conn)
    _migrate_worker_columns(conn)
    return conn


# Task-lifecycle columns added after the first core.db shipped. CREATE TABLE IF
# NOT EXISTS won't retro-fit an existing file, so add missing columns in place
# (idempotent) and fold the retired 'todo' workflow status onto 'backlog'. New
# dbs already have the columns from SCHEMA; this is a cheap no-op for them.
_TASK_LIFECYCLE_COLUMNS = (
    ("assignee", "TEXT NOT NULL DEFAULT 'unassigned'"),
    ("risk", "TEXT"),
    ("approval", "TEXT NOT NULL DEFAULT 'none'"),
    # archive filing (added after assignee/risk/approval shipped). Nullable text:
    # NULL for live tasks, 'not_relevant'|'done' once archived.
    ("archive_reason", "TEXT"),
    # the decision row (added last): the one-line ask + the coordinator's
    # recommendation-with-default that the approval centre renders instead of
    # the label. Nullable — every task that predates them has neither.
    ("ask", "TEXT"),
    ("recommend", "TEXT"),
)


def _migrate_task_columns(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(task)")}
    for col, decl in _TASK_LIFECYCLE_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE task ADD COLUMN {col} {decl}")
    conn.execute("UPDATE task SET status='backlog' WHERE status='todo'")


# Worker role-model columns added after the first core.db shipped (multi-user +
# role-based approval routing). Same in-place retrofit pattern as tasks: add any
# missing column (idempotent). New dbs already have them from SCHEMA.
_WORKER_ROLE_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'human'"),
    ("role", "TEXT NOT NULL DEFAULT 'member'"),
    ("active", "INTEGER NOT NULL DEFAULT 1"),
)


def _migrate_worker_columns(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(worker)")}
    for col, decl in _WORKER_ROLE_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE worker ADD COLUMN {col} {decl}")


# --- create / read / update ------------------------------------------------
def _create(conn: sqlite3.Connection, type_: str, payload: dict[str, Any],
            label: Optional[str], trust: str,
            tags: Optional[list[str]]) -> Entity:
    if type_ not in _PAYLOAD:
        raise ValueError(f"unknown entity type: {type_!r}")
    if trust not in TRUST:
        raise ValueError(f"unknown trust status: {trust!r}")
    table, cols, defaults = _PAYLOAD[type_]
    eid = str(uuid.uuid4())
    now = _now()
    conn.execute(
        "INSERT INTO metadata(id,type,label,trust_status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)", (eid, type_, label, trust, now, now))
    vals = [payload.get(c, defaults.get(c)) for c in cols]
    ph = ",".join(["?"] * (len(cols) + 1))
    conn.execute(
        f"INSERT INTO {table}(metadata_id,{','.join(cols)}) VALUES({ph})",
        (eid, *vals))
    for t in (tags or []):
        add_tag(conn, eid, t)
    return get(conn, eid)  # type: ignore[return-value]


def create_task(conn, *, status="backlog", due_date=None, priority="P2",
                label=None, trust="proposed", tags=None) -> Entity:
    return _create(conn, "task",
                   {"status": status, "due_date": due_date, "priority": priority},
                   label, trust, tags)


def create_event(conn, *, status="scheduled", start_dt=None, end_dt=None,
                 location=None, label=None, trust="proposed", tags=None) -> Entity:
    return _create(conn, "event",
                   {"status": status, "start_dt": start_dt, "end_dt": end_dt,
                    "location": location}, label, trust, tags)


def create_artifact(conn, *, content=None, state="raw", label=None,
                    trust="proposed", tags=None) -> Entity:
    return _create(conn, "artifact", {"content": content, "state": state},
                   label, trust, tags)


def create_worker(conn, *, capabilities=None, channel=None,
                  interaction_style=None, kind="human", role="member",
                  active=1, label=None, trust="confirmed", tags=None) -> Entity:
    # workers default to confirmed (you onboard a known collaborator). kind/role/
    # active carry the role model (see core.team for validated onboarding).
    return _create(conn, "worker",
                   {"capabilities": capabilities, "channel": channel,
                    "interaction_style": interaction_style,
                    "kind": kind, "role": role,
                    "active": 1 if active else 0}, label, trust, tags)


def get(conn: sqlite3.Connection, eid: str) -> Optional[Entity]:
    row = conn.execute("SELECT * FROM metadata WHERE id=?", (eid,)).fetchone()
    if row is None:
        return None
    table, cols, _ = _PAYLOAD[row["type"]]
    prow = conn.execute(
        f"SELECT * FROM {table} WHERE metadata_id=?", (eid,)).fetchone()
    payload = {c: prow[c] for c in cols} if prow else {}
    return Entity(id=row["id"], type=row["type"], label=row["label"],
                  trust=row["trust_status"], created_at=row["created_at"],
                  updated_at=row["updated_at"], payload=payload)


def _touch(conn: sqlite3.Connection, eid: str) -> None:
    conn.execute("UPDATE metadata SET updated_at=? WHERE id=?", (_now(), eid))


def update_payload(conn: sqlite3.Connection, eid: str, **fields: Any) -> Entity:
    """Update type-specific fields on an entity (e.g. a task's status/priority)."""
    e = get(conn, eid)
    if e is None:
        raise KeyError(eid)
    table, cols, _ = _PAYLOAD[e.type]
    upd = {k: v for k, v in fields.items() if k in cols}
    if upd:
        sets = ",".join(f"{k}=?" for k in upd)
        conn.execute(f"UPDATE {table} SET {sets} WHERE metadata_id=?",
                     (*upd.values(), eid))
        _touch(conn, eid)
    return get(conn, eid)  # type: ignore[return-value]


def set_label(conn: sqlite3.Connection, eid: str, label: Optional[str]) -> None:
    conn.execute("UPDATE metadata SET label=?, updated_at=? WHERE id=?",
                 (label, _now(), eid))


def set_trust(conn: sqlite3.Connection, eid: str, trust: str) -> None:
    if trust not in TRUST:
        raise ValueError(f"unknown trust status: {trust!r}")
    conn.execute("UPDATE metadata SET trust_status=?, updated_at=? WHERE id=?",
                 (trust, _now(), eid))


def confirm(conn, eid):  # promote proposed -> confirmed (the human-in-loop gate)
    set_trust(conn, eid, "confirmed")


def reject(conn, eid):
    set_trust(conn, eid, "rejected")


def delete(conn: sqlite3.Connection, eid: str) -> None:
    """Delete an entity; payload, tag-links and edges cascade."""
    conn.execute("DELETE FROM metadata WHERE id=?", (eid,))


# --- queries ---------------------------------------------------------------
def by_type(conn, type_: str, *, trust: Optional[str] = None) -> list[Entity]:
    q = "SELECT id FROM metadata WHERE type=?"
    args: list[Any] = [type_]
    if trust:
        q += " AND trust_status=?"
        args.append(trust)
    q += " ORDER BY created_at"
    return [get(conn, r["id"]) for r in conn.execute(q, args)]  # type: ignore[misc]


def by_trust(conn, trust: str) -> list[Entity]:
    return [get(conn, r["id"]) for r in  # type: ignore[misc]
            conn.execute("SELECT id FROM metadata WHERE trust_status=? "
                         "ORDER BY created_at", (trust,))]


# --- tags (first-class) ----------------------------------------------------
def get_or_create_tag(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM tag WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO tag(name,created_at) VALUES(?,?)",
                       (name, _now()))
    return int(cur.lastrowid)


def add_tag(conn: sqlite3.Connection, eid: str, name: str) -> None:
    tid = get_or_create_tag(conn, name)
    conn.execute("INSERT OR IGNORE INTO metadata_tag(metadata_id,tag_id) "
                 "VALUES(?,?)", (eid, tid))


def remove_tag(conn: sqlite3.Connection, eid: str, name: str) -> None:
    conn.execute(
        "DELETE FROM metadata_tag WHERE metadata_id=? AND tag_id="
        "(SELECT id FROM tag WHERE name=?)", (eid, name))


def tags_of(conn: sqlite3.Connection, eid: str) -> list[str]:
    return [r["name"] for r in conn.execute(
        "SELECT t.name FROM tag t JOIN metadata_tag mt ON mt.tag_id=t.id "
        "WHERE mt.metadata_id=? ORDER BY t.name", (eid,))]


def entities_by_tag(conn, name: str, *, type_: Optional[str] = None,
                    trust: Optional[str] = None) -> list[Entity]:
    """All entities sharing a tag — i.e. a project/goal collection. The whole
    point of the envelope: this is ONE query across all entity types. Optional
    ``type_`` / ``trust`` filters."""
    q = ("SELECT m.id FROM metadata m "
         "JOIN metadata_tag mt ON mt.metadata_id=m.id "
         "JOIN tag t ON t.id=mt.tag_id WHERE t.name=?")
    args: list[Any] = [name]
    if type_:
        q += " AND m.type=?"
        args.append(type_)
    if trust:
        q += " AND m.trust_status=?"
        args.append(trust)
    q += " ORDER BY m.created_at"
    return [get(conn, r["id"]) for r in conn.execute(q, args)]  # type: ignore[misc]


def set_tag_meta_artifact(conn: sqlite3.Connection, name: str,
                          artifact_id: Optional[str]) -> None:
    """Point a tag at its librarian-generated meta-artifact (the rolled-up
    project/goal state)."""
    tid = get_or_create_tag(conn, name)
    conn.execute("UPDATE tag SET meta_artifact_id=? WHERE id=?",
                 (artifact_id, tid))


def tag_meta_artifact(conn: sqlite3.Connection, name: str) -> Optional[Entity]:
    row = conn.execute("SELECT meta_artifact_id FROM tag WHERE name=?",
                       (name,)).fetchone()
    if not row or not row["meta_artifact_id"]:
        return None
    return get(conn, row["meta_artifact_id"])


# --- edges (graph) ---------------------------------------------------------
def add_edge(conn: sqlite3.Connection, from_id: str, to_id: str,
             type_: str) -> None:
    if type_ not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {type_!r}")
    conn.execute(
        "INSERT OR IGNORE INTO edge(from_id,to_id,type,created_at) "
        "VALUES(?,?,?,?)", (from_id, to_id, type_, _now()))


def remove_edge(conn, from_id: str, to_id: str, type_: str) -> None:
    conn.execute("DELETE FROM edge WHERE from_id=? AND to_id=? AND type=?",
                 (from_id, to_id, type_))


def edges_out(conn, eid: str, type_: Optional[str] = None) -> list[str]:
    q = "SELECT to_id FROM edge WHERE from_id=?"
    args: list[Any] = [eid]
    if type_:
        q += " AND type=?"
        args.append(type_)
    return [r["to_id"] for r in conn.execute(q, args)]


def edges_in(conn, eid: str, type_: Optional[str] = None) -> list[str]:
    q = "SELECT from_id FROM edge WHERE to_id=?"
    args: list[Any] = [eid]
    if type_:
        q += " AND type=?"
        args.append(type_)
    return [r["from_id"] for r in conn.execute(q, args)]


def blocks(conn, eid: str) -> list[str]:
    """Entities this one BLOCKS (outgoing canonical `blocks` edges)."""
    return edges_out(conn, eid, "blocks")


def depends_on(conn, eid: str) -> list[str]:
    """DERIVED: entities this one DEPENDS ON = the incoming `blocks` edges
    (whoever blocks it). No reverse edge is stored — single source of truth."""
    return edges_in(conn, eid, "blocks")


def justified_by(conn, eid: str) -> list[str]:
    """Provenance: the artifact(s) that justify this entity (outgoing
    `justified_by` edges). Reads nicer than ``edges_out(conn, eid,
    'justified_by')`` at call sites."""
    return edges_out(conn, eid, "justified_by")


def edge_count(conn, eid: str, type_: str, *, direction: str = "out") -> int:
    """Count edges of a type on an entity without materialising ids — e.g.
    worker load = ``edge_count(conn, worker_id, 'assigned_to')``."""
    col = "from_id" if direction == "out" else "to_id"
    return conn.execute(
        f"SELECT COUNT(*) FROM edge WHERE {col}=? AND type=?",
        (eid, type_)).fetchone()[0]
