"""Tracking-state data model (chat-tracking system, increment 1).

A SQLite store at ``data/_tg/tracking.db`` (``Config.tg_tracking_db_path``) —
SEPARATE from the ingest ``messages.db`` so the Telethon ingest worker and the
tracking pipelines never contend on one file/WAL. Holds the durable products of
the three pipelines plus a per-(slug,chat,pipeline) watermark:

- ``todos``              — extracted action items (approval-gated execution).
- ``meetings``           — detected meetings/calls (feed the iCal generator).
- ``suggested_replies``  — drafted send-as-Tim replies (approval-gated send).
- ``pipeline_watermark`` — last processed msg per pipeline → process only new.

This module is small and side-effect-local: every function takes an explicit
``sqlite3.Connection`` (or a path, for ``init_tracking_db``) so tests use a temp
db. ``slug`` scopes every row to a project. Timestamps are unix seconds (UTC),
except meeting ``start_dt``/``end_dt`` which are ISO-8601 strings.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    slug             TEXT,
    chat_id          INTEGER,
    chat_title       TEXT,
    msg_id           INTEGER,
    ts               INTEGER,
    text             TEXT,
    suggested_action TEXT,
    actionable       INTEGER DEFAULT 0,
    status           TEXT DEFAULT 'pending',   -- pending|approved|rejected|executing|done|failed
    created_at       INTEGER,
    decided_at       INTEGER,
    result           TEXT
);
CREATE INDEX IF NOT EXISTS idx_todos_slug_status ON todos (slug, status);

CREATE TABLE IF NOT EXISTS meetings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT,
    chat_id      INTEGER,
    chat_title   TEXT,
    msg_id       INTEGER,
    ts           INTEGER,
    title        TEXT,
    start_dt     TEXT,                          -- ISO-8601
    end_dt       TEXT,                          -- ISO-8601
    location     TEXT,
    participants TEXT,
    link         TEXT,
    ics_uid      TEXT,
    status       TEXT DEFAULT 'new',            -- new|scheduled|invited|accepted|declined|approved|dismissed|duplicate
                                                -- 'scheduled' = live as a PLAIN (editable) Swarm-calendar event (two-way sync);
                                                -- 'invited' is the legacy Accept/Decline invitation flow (pre-migration)
    created_at   INTEGER,
    dup_of       INTEGER                         -- id of the kept meeting a semantic-duplicate folds into
);
CREATE INDEX IF NOT EXISTS idx_meetings_slug_status ON meetings (slug, status);

CREATE TABLE IF NOT EXISTS suggested_replies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT,
    chat_id         INTEGER,
    chat_title      TEXT,
    reply_to_msg_id INTEGER,
    ts              INTEGER,
    draft_text      TEXT,
    status          TEXT DEFAULT 'pending',     -- pending|sent|edited|skipped
    created_at      INTEGER,
    decided_at      INTEGER,
    sent_at         INTEGER,
    context_json    TEXT                         -- layered context the draft saw
);
CREATE INDEX IF NOT EXISTS idx_replies_slug_status ON suggested_replies (slug, status);

CREATE TABLE IF NOT EXISTS pipeline_watermark (
    slug        TEXT,
    chat_id     INTEGER,
    pipeline    TEXT,                            -- todos|meetings|digest
    last_msg_id INTEGER DEFAULT 0,
    last_ts     INTEGER DEFAULT 0,
    PRIMARY KEY (slug, chat_id, pipeline)
);

-- LIVE tracking config (chat-tracking inc. 3 / management frontend). DB-backed
-- mirror of the per-project ``tg_track_*`` file config so the UI can change
-- scope WITHOUT a worker restart. When a row exists it OVERRIDES the file
-- config (tg_tracking.get_effective_tracking); when absent the file config is
-- the default. ``chats_json`` / ``realtime_json`` are JSON-encoded lists whose
-- entries keep their type (int chat_id or string title/@username), matching the
-- file-config convention consumed by tg_tracking's pure matchers.
-- ``claude_json`` is the @claude ALLOWLIST: a JSON-encoded list (same entry
-- convention as chats_json) of chats where the @claude trigger / auto-help is
-- permitted. EMPTY (the default) ⇒ @claude is OFF everywhere — a
-- prompt-injection safety gate Tim controls from the UI. Independent of
-- tracking scope (a chat can be @claude-enabled without being tracked).
-- ``auto_draft_json`` is the AUTO-DRAFT ALLOWLIST: a JSON-encoded list (same
-- entry convention as chats_json) of chats where, whenever the system has a
-- suggested reply, the bridge SETS IT AS THE CHAT'S DRAFT (it sits in Tim's
-- input box — nothing is sent). EMPTY (the default) ⇒ auto-draft OFF
-- everywhere. Independent of tracking scope / @claude (a chat can be
-- auto-draft-enabled on its own), and Tim toggles it per chat from the UI.
CREATE TABLE IF NOT EXISTS tracking_config (
    slug            TEXT PRIMARY KEY,
    enabled         INTEGER,
    mode            TEXT,                        -- include|exclude
    chats_json      TEXT,
    realtime_json   TEXT,
    claude_json     TEXT,
    auto_draft_json TEXT,
    updated_at      INTEGER
);

-- Per-day chat summaries (chat-tracking: 30-day memory). One factual 1-3
-- sentence summary per (slug, chat_id, UTC day), produced by the summaries
-- pipeline (tg_pipelines.summarize_chat_day) and consumed as BROADER CONTEXT by
-- the reply pipeline (tg_pipelines._daily_summaries). ``day`` is 'YYYY-MM-DD'
-- (UTC). CREATE IF NOT EXISTS so an existing tracking.db gains it on next open.
CREATE TABLE IF NOT EXISTS chat_daily_summaries (
    slug       TEXT,
    chat_id    INTEGER,
    day        TEXT,                             -- 'YYYY-MM-DD' (UTC)
    summary    TEXT,
    msg_count  INTEGER,
    created_at INTEGER,
    PRIMARY KEY (slug, chat_id, day)
);

-- OUTGOING send queue (send-as-Tim). The Telethon session for Tim's account is
-- owned by the long-lived ingest bridge (tg_user.run) — a second process cannot
-- open the same session concurrently. So the frontend never sends directly: it
-- ENQUEUES a row here, and the bridge (which holds the live client) drains the
-- queue and actually sends. ``status``: queued|sent|failed. ``source_reply_id``
-- links back to the suggested_replies row the send came from (nullable for
-- ad-hoc sends). CREATE IF NOT EXISTS so an existing tracking.db gains it.
CREATE TABLE IF NOT EXISTS outgoing_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT,
    chat_id         INTEGER,
    reply_to_msg_id INTEGER,
    text            TEXT,
    status          TEXT DEFAULT 'queued',       -- queued|sent|failed
    created_at      INTEGER,
    sent_at         INTEGER,
    error           TEXT,
    source_reply_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_outgoing_status ON outgoing_messages (status, id);

-- AUTO-HELP windows (@claude auto-reply feature). When someone types
-- "@claude help [for N min]" in a chat, a per-chat window is opened: for the
-- next N minutes NEW incoming messages in that chat get an automated AI reply
-- (sent as Tim) without another @claude. Durable so the window survives a
-- bridge restart. ``until_ts`` is the unix-second expiry; a row with
-- until_ts <= now is treated as no window. ``enabled_by`` records who opened
-- it. CREATE IF NOT EXISTS so an existing tracking.db gains it on next open.
-- INDEPENDENT of tracking scope: keyed by chat_id only (no slug).
CREATE TABLE IF NOT EXISTS auto_help (
    chat_id    INTEGER PRIMARY KEY,
    until_ts   INTEGER,
    enabled_by TEXT,
    created_at INTEGER
);

-- AUTO-DRAFT queue (per-chat draft pre-fill). Mirrors the outgoing_messages
-- pattern: the Telethon session is owned by the long-lived bridge (tg_user.run),
-- so neither the WORKER (periodic tick) nor the BRIDGE's thread-executor realtime
-- path can call SaveDraftRequest directly. They ENQUEUE here (one PENDING row per
-- chat, keyed by chat_id) and the bridge's _draft_setter drains it: it SETS the
-- chat's Telegram draft to ``text`` (empty text CLEARS the draft). ``status``:
-- queued|set|failed. ``last_set_text`` records what the setter last wrote so the
-- CLOBBER-GUARD can tell our own draft from one Tim typed/edited. CREATE IF NOT
-- EXISTS so an existing tracking.db gains it on next open.
CREATE TABLE IF NOT EXISTS draft_queue (
    chat_id       INTEGER PRIMARY KEY,
    text          TEXT,
    status        TEXT DEFAULT 'queued',         -- queued|set|failed
    created_at    INTEGER,
    set_at        INTEGER,
    error         TEXT,
    last_set_text TEXT
);

-- REPLY WATCHES. When the assistant sends a message on Tim's behalf and expects
-- a reply (e.g. texting Макс to agree a meeting time), it opens a watch here.
-- ``after_msg_id`` is the msg_id of OUR sent message: any later INCOMING message
-- in that chat (raw_json.out is false) counts as the awaited reply. A periodic
-- detector (tg_pipelines.detect_reply_watches) flips the watch to 'fired' and
-- records the reply so coord-life can surface it to Tim, instead of the reply
-- being missed until Tim happens to ping. ``status``: open|fired|cancelled.
-- CREATE IF NOT EXISTS so an existing tracking.db gains it on next open.
CREATE TABLE IF NOT EXISTS reply_watches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT,
    chat_id      INTEGER NOT NULL,
    chat_title   TEXT,
    after_msg_id INTEGER NOT NULL DEFAULT 0,     -- reply must have msg_id > this
    context      TEXT,                           -- what we're waiting on (for the ping)
    status       TEXT NOT NULL DEFAULT 'open',   -- open|fired|cancelled
    created_at   INTEGER,
    fired_at     INTEGER,
    fired_msg_id INTEGER,
    fired_text   TEXT,
    notified_at  INTEGER                         -- when the fired watch was DELIVERED to Tim
                                                 -- (NULL = fired but undelivered: the safety-net set)
);
CREATE INDEX IF NOT EXISTS idx_reply_watch_status ON reply_watches (status, chat_id);

-- Small KV settings for assistant features that need durable state/config that
-- is NOT per-project UI config (that's tracking_config) — e.g. the morning
-- reminder time and its sent-today stamp, future sync cursors. CREATE IF NOT
-- EXISTS so an existing tracking.db gains it on next open.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER
);
"""


def _now() -> int:
    return int(time.time())


def get_setting(conn: sqlite3.Connection, key: str,
                default: Optional[str] = None) -> Optional[str]:
    """Read one ``app_settings`` value (KV feature state), or ``default``."""
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert one ``app_settings`` value."""
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, str(value), _now()),
    )


def init_tracking_db(path: str | Path) -> sqlite3.Connection:
    """Open (creating parent dir) and initialize the tracking db; return conn.

    Idempotent: CREATE TABLE IF NOT EXISTS, so re-opening an existing db is a
    no-op. Rows come back as ``sqlite3.Row`` for dict-like access. Mirrors
    ``tg_user.open_db`` (WAL, autocommit).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-only column migrations for dbs created before a column existed.

    CREATE TABLE IF NOT EXISTS never alters an existing table, so a db built by
    an earlier schema is missing newer columns. Add them defensively (ADD COLUMN
    is cheap + non-rewriting in SQLite). Currently: ``suggested_replies``'s
    ``context_json`` (the layered context a draft was generated against).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(suggested_replies)")}
    if "context_json" not in cols:
        conn.execute("ALTER TABLE suggested_replies ADD COLUMN context_json TEXT")
    # ``tracking_config.claude_json`` — the @claude allowlist (added later than
    # the table). A db built before this column gains it here.
    cfg_cols = {r[1] for r in conn.execute("PRAGMA table_info(tracking_config)")}
    if "claude_json" not in cfg_cols:
        conn.execute("ALTER TABLE tracking_config ADD COLUMN claude_json TEXT")
    # ``tracking_config.auto_draft_json`` — the auto-draft allowlist (added later
    # than the table). A db built before this column gains it here.
    if "auto_draft_json" not in cfg_cols:
        conn.execute("ALTER TABLE tracking_config ADD COLUMN auto_draft_json TEXT")
    # ``meetings.dup_of`` — the id of the surviving meeting a SEMANTIC-duplicate
    # row was folded into (see tg_pipelines.dedupe_meetings). Non-destructive: a
    # loser is marked status='duplicate' and dup_of points at the kept row, so the
    # merge is fully recoverable. A db built before this column gains it here.
    mtg_cols = {r[1] for r in conn.execute("PRAGMA table_info(meetings)")}
    if "dup_of" not in mtg_cols:
        conn.execute("ALTER TABLE meetings ADD COLUMN dup_of INTEGER")
    # ``reply_watches.notified_at`` — stamped by reply_watch_notify_tick when a
    # fired watch was actually DELIVERED to the owner on the configured bot. Distinguishes
    # delivered from send-failed, so coord-life's safety net re-surfaces exactly
    # ``status='fired' AND notified_at IS NULL`` without double-notifying. A db
    # built before this column gains it here.
    rw_cols = {r[1] for r in conn.execute("PRAGMA table_info(reply_watches)")}
    if "notified_at" not in rw_cols:
        conn.execute("ALTER TABLE reply_watches ADD COLUMN notified_at INTEGER")


# ---------------------------------------------------------------------------
# Watermarks — process-only-new bookkeeping per (slug, chat_id, pipeline).
# ---------------------------------------------------------------------------


def get_watermark(
    conn: sqlite3.Connection, slug: str, chat_id: int, pipeline: str
) -> int:
    """Return the last processed ``msg_id`` for this pipeline; 0 if none yet."""
    row = conn.execute(
        "SELECT last_msg_id FROM pipeline_watermark "
        "WHERE slug = ? AND chat_id = ? AND pipeline = ?",
        (slug, int(chat_id), pipeline),
    ).fetchone()
    return int(row[0]) if row else 0


def set_watermark(
    conn: sqlite3.Connection,
    slug: str,
    chat_id: int,
    pipeline: str,
    last_msg_id: int,
    last_ts: int,
) -> None:
    """Upsert the watermark for this (slug, chat_id, pipeline)."""
    conn.execute(
        """
        INSERT INTO pipeline_watermark (slug, chat_id, pipeline, last_msg_id, last_ts)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(slug, chat_id, pipeline) DO UPDATE SET
            last_msg_id = excluded.last_msg_id,
            last_ts     = excluded.last_ts
        """,
        (slug, int(chat_id), pipeline, int(last_msg_id), int(last_ts)),
    )


# ---------------------------------------------------------------------------
# Inserts — one row per detected item; return the new rowid.
# ---------------------------------------------------------------------------


def insert_todo(
    conn: sqlite3.Connection,
    slug: str,
    chat_id: int,
    chat_title: Optional[str],
    msg_id: int,
    ts: int,
    text: str,
    suggested_action: Optional[str] = None,
    actionable: bool = False,
    status: str = "pending",
    created_at: Optional[int] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO todos
            (slug, chat_id, chat_title, msg_id, ts, text, suggested_action,
             actionable, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slug, int(chat_id), chat_title, int(msg_id), int(ts), text,
            suggested_action, 1 if actionable else 0, status,
            created_at if created_at is not None else _now(),
        ),
    )
    return int(cur.lastrowid)


def insert_meeting(
    conn: sqlite3.Connection,
    slug: str,
    chat_id: int,
    chat_title: Optional[str],
    msg_id: int,
    ts: int,
    title: str,
    start_dt: Optional[str] = None,
    end_dt: Optional[str] = None,
    location: Optional[str] = None,
    participants: Optional[str] = None,
    link: Optional[str] = None,
    ics_uid: Optional[str] = None,
    status: str = "new",
    created_at: Optional[int] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO meetings
            (slug, chat_id, chat_title, msg_id, ts, title, start_dt, end_dt,
             location, participants, link, ics_uid, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slug, int(chat_id), chat_title, int(msg_id), int(ts), title,
            start_dt, end_dt, location, participants, link, ics_uid, status,
            created_at if created_at is not None else _now(),
        ),
    )
    return int(cur.lastrowid)


def insert_reply(
    conn: sqlite3.Connection,
    slug: str,
    chat_id: int,
    chat_title: Optional[str],
    reply_to_msg_id: int,
    ts: int,
    draft_text: str,
    status: str = "pending",
    created_at: Optional[int] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO suggested_replies
            (slug, chat_id, chat_title, reply_to_msg_id, ts, draft_text,
             status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slug, int(chat_id), chat_title, int(reply_to_msg_id), int(ts),
            draft_text, status,
            created_at if created_at is not None else _now(),
        ),
    )
    return int(cur.lastrowid)


def get_pending_reply(
    conn: sqlite3.Connection, slug: str, chat_id: int
) -> Optional[sqlite3.Row]:
    """Return the single PENDING suggested reply for (slug, chat_id), or None.

    There is at most one pending draft per chat (see ``upsert_pending_reply``);
    if more than one exists (legacy data) the newest is returned.
    """
    return conn.execute(
        "SELECT * FROM suggested_replies "
        "WHERE slug = ? AND chat_id = ? AND status = 'pending' "
        "ORDER BY id DESC LIMIT 1",
        (slug, int(chat_id)),
    ).fetchone()


def clear_pending_reply(
    conn: sqlite3.Connection, chat_id: int, slug: Optional[str] = None
) -> int:
    """Mark the PENDING suggested reply(ies) for a chat as 'skipped'.

    Used when Tim answers the chat HIMSELF, so a stale draft doesn't linger in
    the feed. Scopes to ``slug`` when given, else clears the chat across slugs.
    Only 'pending' rows are touched (a decided reply stays decided). Returns the
    number of rows cleared.
    """
    if slug is None:
        cur = conn.execute(
            "UPDATE suggested_replies SET status = 'skipped', decided_at = ? "
            "WHERE chat_id = ? AND status = 'pending'",
            (_now(), int(chat_id)),
        )
    else:
        cur = conn.execute(
            "UPDATE suggested_replies SET status = 'skipped', decided_at = ? "
            "WHERE slug = ? AND chat_id = ? AND status = 'pending'",
            (_now(), slug, int(chat_id)),
        )
    return cur.rowcount


def upsert_pending_reply(
    conn: sqlite3.Connection,
    slug: str,
    chat_id: int,
    chat_title: Optional[str],
    reply_to_msg_id: int,
    ts: int,
    draft_text: str,
    context_json: Optional[str] = None,
    created_at: Optional[int] = None,
) -> int:
    """Upsert the at-most-one PENDING draft for (slug, chat_id); return its id.

    If a ``pending`` row already exists it is UPDATED in place (a regenerated
    draft replaces the prior one — ``draft_text``/``reply_to_msg_id``/``ts``/
    ``context_json`` overwritten, ``created_at`` refreshed) so the feed never
    accumulates stale drafts for a chat. Non-pending rows (sent/edited/skipped/
    approved) are NEVER touched — a decided reply stays decided. If no pending
    row exists, a fresh one is INSERTed.
    """
    now = created_at if created_at is not None else _now()
    existing = get_pending_reply(conn, slug, chat_id)
    if existing is not None:
        conn.execute(
            "UPDATE suggested_replies SET chat_title = ?, reply_to_msg_id = ?, "
            "ts = ?, draft_text = ?, context_json = ?, created_at = ? "
            "WHERE id = ?",
            (
                chat_title, int(reply_to_msg_id), int(ts), draft_text,
                context_json, now, int(existing["id"]),
            ),
        )
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO suggested_replies
            (slug, chat_id, chat_title, reply_to_msg_id, ts, draft_text,
             status, context_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            slug, int(chat_id), chat_title, int(reply_to_msg_id), int(ts),
            draft_text, context_json, now,
        ),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Lists — newest first, optionally filtered by slug and/or status.
# ---------------------------------------------------------------------------


def _list(
    conn: sqlite3.Connection,
    table: str,
    slug: Optional[str],
    status: Optional[str],
    limit: int,
) -> list[sqlite3.Row]:
    where: list[str] = []
    args: list[Any] = []
    if slug is not None:
        where.append("slug = ?")
        args.append(slug)
    if status is not None:
        where.append("status = ?")
        args.append(status)
    sql = f"SELECT * FROM {table}"  # noqa: S608 - table is an internal literal
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return list(conn.execute(sql, args).fetchall())


def list_todos(
    conn: sqlite3.Connection,
    slug: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    return _list(conn, "todos", slug, status, limit)


def list_meetings(
    conn: sqlite3.Connection,
    slug: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    return _list(conn, "meetings", slug, status, limit)


def list_replies(
    conn: sqlite3.Connection,
    slug: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    return _list(conn, "suggested_replies", slug, status, limit)


# ---------------------------------------------------------------------------
# Status transitions — stamp the decision time as a side effect.
# ---------------------------------------------------------------------------


def set_todo_status(
    conn: sqlite3.Connection,
    todo_id: int,
    status: str,
    result: Optional[str] = None,
    decided_at: Optional[int] = None,
) -> int:
    """Update a todo's status; stamp ``decided_at`` and optionally ``result``.

    Returns the number of rows updated (0 ⇒ no such id) so callers can tell a
    real transition apart from a no-op against a missing/removed row.
    """
    cur = conn.execute(
        "UPDATE todos SET status = ?, decided_at = ?, "
        "result = COALESCE(?, result) WHERE id = ?",
        (status, decided_at if decided_at is not None else _now(), result, int(todo_id)),
    )
    return cur.rowcount


def set_reply_status(
    conn: sqlite3.Connection,
    reply_id: int,
    status: str,
    sent_at: Optional[int] = None,
    decided_at: Optional[int] = None,
) -> int:
    """Update a suggested reply's status.

    ``decided_at`` is stamped on every transition. ``sent_at`` is set when
    given, or auto-stamped when transitioning to ``sent``. Returns the number of
    rows updated (0 ⇒ no such id).
    """
    if sent_at is None and status == "sent":
        sent_at = _now()
    cur = conn.execute(
        "UPDATE suggested_replies SET status = ?, decided_at = ?, "
        "sent_at = COALESCE(?, sent_at) WHERE id = ?",
        (
            status,
            decided_at if decided_at is not None else _now(),
            sent_at,
            int(reply_id),
        ),
    )
    return cur.rowcount


def set_meeting_status(
    conn: sqlite3.Connection, meeting_id: int, status: str
) -> int:
    """Update a meeting's status (the meetings table has no decided_at column).

    Returns the number of rows updated (0 ⇒ no such id).
    """
    cur = conn.execute(
        "UPDATE meetings SET status = ? WHERE id = ?",
        (status, int(meeting_id)),
    )
    return cur.rowcount


def update_meeting_fields(
    conn: sqlite3.Connection, meeting_id: int, *,
    title: Optional[str] = None, start_dt: Optional[str] = None,
) -> None:
    """Apply Tim's calendar edits back onto a meeting row (title and/or start).

    Only the passed fields are touched; None means 'leave unchanged'. Used by
    the calendar sync tick's plain-event reconcile.
    """
    sets, vals = [], []
    if title is not None:
        sets.append("title = ?")
        vals.append(title)
    if start_dt is not None:
        sets.append("start_dt = ?")
        vals.append(start_dt)
    if not sets:
        return
    vals.append(int(meeting_id))
    conn.execute(f"UPDATE meetings SET {', '.join(sets)} WHERE id = ?", vals)


def set_meeting_ics_uid(
    conn: sqlite3.Connection, meeting_id: int, uid: str
) -> None:
    """Stamp the calendar event UID on a meeting row (after an invite is written)."""
    conn.execute(
        "UPDATE meetings SET ics_uid = ? WHERE id = ?",
        (uid, int(meeting_id)),
    )


def mark_meeting_duplicate(
    conn: sqlite3.Connection, meeting_id: int, dup_of: Optional[int] = None
) -> None:
    """Mark a meeting as a SEMANTIC duplicate of ``dup_of`` (non-destructive).

    Sets ``status='duplicate'`` (hidden from the UI, recoverable) and stamps
    ``dup_of`` with the id of the surviving meeting it was folded into. Used by
    ``tg_pipelines.dedupe_meetings``.
    """
    conn.execute(
        "UPDATE meetings SET status = 'duplicate', dup_of = ? WHERE id = ?",
        (int(dup_of) if dup_of is not None else None, int(meeting_id)),
    )


def list_meetings_excluding_duplicates(
    conn: sqlite3.Connection,
    slug: Optional[str] = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Like ``list_meetings`` but drops rows marked ``status='duplicate'``.

    Backs the UI listing so Tim never sees a meeting that was folded into
    another (the survivor stays visible). Duplicates remain queryable directly
    (``list_meetings(status='duplicate')``) for recovery.
    """
    where = ["status IS NOT 'duplicate'"]
    args: list[Any] = []
    if slug is not None:
        where.insert(0, "slug = ?")
        args.insert(0, slug)
    sql = "SELECT * FROM meetings WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return list(conn.execute(sql, args).fetchall())


def recent_meetings_for_chat(
    conn: sqlite3.Connection,
    slug: str,
    chat_id: int,
    limit: int = 20,
) -> list[sqlite3.Row]:
    """Return the most-recent NON-duplicate meetings for (slug, chat_id).

    Candidate set for the at-insert semantic-dedup guard in
    ``extract_from_chat`` — newest first, already-duplicate rows excluded.
    """
    return list(conn.execute(
        "SELECT * FROM meetings WHERE slug = ? AND chat_id = ? "
        "AND status IS NOT 'duplicate' ORDER BY id DESC LIMIT ?",
        (slug, int(chat_id), int(limit)),
    ).fetchall())


def find_meeting(
    conn: sqlite3.Connection,
    slug: str,
    chat_id: int,
    title: str,
    start_dt: Optional[str],
) -> Optional[sqlite3.Row]:
    """Return a prior meeting row matching (slug, chat_id, title, start_dt).

    Used as the DEDUP key so the same detected meeting is not written to the
    calendar twice. ``start_dt`` is matched with IS (so NULL==NULL works).
    Newest row first.
    """
    return conn.execute(
        "SELECT * FROM meetings "
        "WHERE slug = ? AND chat_id = ? AND title = ? AND start_dt IS ? "
        "ORDER BY id DESC LIMIT 1",
        (slug, int(chat_id), title, start_dt),
    ).fetchone()


# ---------------------------------------------------------------------------
# Per-day chat summaries — 30-day chat memory feeding the reply context.
# ---------------------------------------------------------------------------


def upsert_daily_summary(
    conn: sqlite3.Connection,
    slug: str,
    chat_id: int,
    day: str,
    summary: str,
    msg_count: int,
    created_at: Optional[int] = None,
) -> None:
    """Upsert the summary for one (slug, chat_id, UTC ``day``).

    Re-summarizing a day (``force=True`` in the pipeline) overwrites the prior
    text + ``msg_count`` and refreshes ``created_at``.
    """
    conn.execute(
        """
        INSERT INTO chat_daily_summaries
            (slug, chat_id, day, summary, msg_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug, chat_id, day) DO UPDATE SET
            summary    = excluded.summary,
            msg_count  = excluded.msg_count,
            created_at = excluded.created_at
        """,
        (
            slug, int(chat_id), str(day), summary, int(msg_count),
            created_at if created_at is not None else _now(),
        ),
    )


def get_daily_summaries(
    conn: sqlite3.Connection, slug: str, chat_id: int, limit: int = 30
) -> list[sqlite3.Row]:
    """Return the most recent daily summaries for this chat (newest day first)."""
    return list(conn.execute(
        "SELECT slug, chat_id, day, summary, msg_count, created_at "
        "FROM chat_daily_summaries "
        "WHERE slug = ? AND chat_id = ? ORDER BY day DESC LIMIT ?",
        (slug, int(chat_id), int(limit)),
    ).fetchall())


def get_summaries_for_day(
    conn: sqlite3.Connection, day: str, slug: Optional[str] = None
) -> list[sqlite3.Row]:
    """All chats' summaries for one UTC ``day`` (busiest chats first).

    ``slug=None`` spans projects — the daily digest wants Tim's whole day, not
    one project's slice.
    """
    where, args = "WHERE day = ?", [str(day)]
    if slug is not None:
        where += " AND slug = ?"
        args.append(slug)
    return list(conn.execute(
        "SELECT slug, chat_id, day, summary, msg_count, created_at "
        f"FROM chat_daily_summaries {where} ORDER BY msg_count DESC",
        args,
    ).fetchall())


def has_daily_summary(
    conn: sqlite3.Connection, slug: str, chat_id: int, day: str
) -> bool:
    """True iff a summary row already exists for (slug, chat_id, ``day``)."""
    row = conn.execute(
        "SELECT 1 FROM chat_daily_summaries "
        "WHERE slug = ? AND chat_id = ? AND day = ? LIMIT 1",
        (slug, int(chat_id), str(day)),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Outgoing send queue — frontend enqueues, the bridge (with the live Telethon
# session) drains + sends. Decouples the FastAPI process from the single-owner
# Telethon session (send-as-Tim).
# ---------------------------------------------------------------------------


def enqueue_outgoing(
    conn: sqlite3.Connection,
    slug: str,
    chat_id: int,
    text: str,
    reply_to_msg_id: Optional[int] = None,
    source_reply_id: Optional[int] = None,
    created_at: Optional[int] = None,
) -> int:
    """Queue one outgoing message for the bridge to send; return its rowid.

    Status starts ``queued``; the bridge sender loop flips it to ``sent`` or
    ``failed``. ``reply_to_msg_id``/``source_reply_id`` are optional.
    """
    cur = conn.execute(
        """
        INSERT INTO outgoing_messages
            (slug, chat_id, reply_to_msg_id, text, status, created_at,
             source_reply_id)
        VALUES (?, ?, ?, ?, 'queued', ?, ?)
        """,
        (
            slug, int(chat_id),
            int(reply_to_msg_id) if reply_to_msg_id is not None else None,
            text,
            created_at if created_at is not None else _now(),
            int(source_reply_id) if source_reply_id is not None else None,
        ),
    )
    return int(cur.lastrowid)


def list_outgoing(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Return outgoing messages (oldest first — FIFO send order), optionally
    filtered by status. Ordering by id ASC so the bridge sends in queue order."""
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM outgoing_messages WHERE status = ? "
            "ORDER BY id ASC LIMIT ?",
            (status, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM outgoing_messages ORDER BY id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return list(rows)


def count_outgoing(conn: sqlite3.Connection, status: str = "queued") -> int:
    """Cheap count of rows in a given status (the bridge polls this first)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM outgoing_messages WHERE status = ?", (status,)
    ).fetchone()
    return int(row[0]) if row else 0


def mark_outgoing_sent(
    conn: sqlite3.Connection, msg_id: int, sent_at: Optional[int] = None
) -> None:
    """Mark a queued outgoing message as sent; stamp ``sent_at`` + clear error."""
    conn.execute(
        "UPDATE outgoing_messages SET status = 'sent', sent_at = ?, error = NULL "
        "WHERE id = ?",
        (sent_at if sent_at is not None else _now(), int(msg_id)),
    )


def mark_outgoing_failed(
    conn: sqlite3.Connection, msg_id: int, error: str
) -> None:
    """Mark a queued outgoing message as failed; record the error string."""
    conn.execute(
        "UPDATE outgoing_messages SET status = 'failed', error = ? WHERE id = ?",
        (str(error)[:300], int(msg_id)),
    )


# ---------------------------------------------------------------------------
# Reply watches — "we sent X and are waiting on their reply". Opened when the
# assistant sends on Tim's behalf; a periodic detector fires them when the
# awaited incoming reply lands so coord-life can surface it (not miss it).
# ---------------------------------------------------------------------------


def add_reply_watch(
    conn: sqlite3.Connection,
    slug: str,
    chat_id: int,
    after_msg_id: int,
    context: Optional[str] = None,
    chat_title: Optional[str] = None,
    created_at: Optional[int] = None,
) -> int:
    """Open a reply watch on ``chat_id``; fires on the first incoming message
    with ``msg_id > after_msg_id``. Returns the watch id."""
    cur = conn.execute(
        """
        INSERT INTO reply_watches
            (slug, chat_id, chat_title, after_msg_id, context, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            slug, int(chat_id), chat_title, int(after_msg_id), context,
            created_at if created_at is not None else _now(),
        ),
    )
    return int(cur.lastrowid)


def list_open_reply_watches(
    conn: sqlite3.Connection, slug: Optional[str] = None
) -> list[sqlite3.Row]:
    """All watches still awaiting a reply (status='open')."""
    if slug is None:
        rows = conn.execute(
            "SELECT * FROM reply_watches WHERE status = 'open' ORDER BY id ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM reply_watches WHERE status = 'open' AND slug = ? "
            "ORDER BY id ASC",
            (slug,),
        ).fetchall()
    return list(rows)


def list_fired_reply_watches(
    conn: sqlite3.Connection, slug: Optional[str] = None,
    undelivered_only: bool = False,
) -> list[sqlite3.Row]:
    """Watches that have fired (reply landed) — for coord-life to surface.

    ``undelivered_only=True`` narrows to fired watches with NO ``notified_at``
    stamp — i.e. the notify tick never delivered them to Tim (send failed or
    the tick was down). That is the safety-net set coord-life re-surfaces;
    delivered watches are excluded so Tim is never double-notified.
    """
    q = "SELECT * FROM reply_watches WHERE status = 'fired'"
    args: list = []
    if slug is not None:
        q += " AND slug = ?"
        args.append(slug)
    if undelivered_only:
        q += " AND notified_at IS NULL"
    rows = conn.execute(q + " ORDER BY fired_at ASC", args).fetchall()
    return list(rows)


def mark_reply_watch_fired(
    conn: sqlite3.Connection,
    watch_id: int,
    fired_msg_id: int,
    fired_text: Optional[str],
    fired_at: Optional[int] = None,
) -> None:
    """Flip an open watch to 'fired' and record the reply that satisfied it."""
    conn.execute(
        "UPDATE reply_watches SET status = 'fired', fired_at = ?, "
        "fired_msg_id = ?, fired_text = ? WHERE id = ? AND status = 'open'",
        (
            fired_at if fired_at is not None else _now(),
            int(fired_msg_id), fired_text, int(watch_id),
        ),
    )


def mark_reply_watch_notified(
    conn: sqlite3.Connection, watch_id: int, notified_at: Optional[int] = None,
) -> None:
    """Stamp a fired watch as DELIVERED to Tim (notify tick sent it)."""
    conn.execute(
        "UPDATE reply_watches SET notified_at = ? "
        "WHERE id = ? AND status = 'fired'",
        (notified_at if notified_at is not None else _now(), int(watch_id)),
    )


def cancel_reply_watch(conn: sqlite3.Connection, watch_id: int) -> None:
    """Cancel an open watch (e.g. Tim resolved the thread himself)."""
    conn.execute(
        "UPDATE reply_watches SET status = 'cancelled' "
        "WHERE id = ? AND status = 'open'",
        (int(watch_id),),
    )


# ---------------------------------------------------------------------------
# Auto-help windows (@claude auto-reply). Per-chat, independent of tracking
# scope. A window is active while ``until_ts`` is in the future.
# ---------------------------------------------------------------------------


def set_auto_help(
    conn: sqlite3.Connection,
    chat_id: int,
    until_ts: int,
    enabled_by: Optional[str] = None,
    created_at: Optional[int] = None,
) -> None:
    """Open (or extend) the auto-help window for ``chat_id`` until ``until_ts``.

    Upsert by chat_id: a fresh "@claude help" call simply moves the expiry.
    """
    conn.execute(
        """
        INSERT INTO auto_help (chat_id, until_ts, enabled_by, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            until_ts   = excluded.until_ts,
            enabled_by = excluded.enabled_by,
            created_at = excluded.created_at
        """,
        (
            int(chat_id), int(until_ts), enabled_by,
            created_at if created_at is not None else _now(),
        ),
    )


def get_auto_help_until(
    conn: sqlite3.Connection, chat_id: int, now: Optional[int] = None
) -> int:
    """Return this chat's auto-help expiry if a window is ACTIVE, else 0.

    A row whose ``until_ts`` is at/before ``now`` (default: wall clock) is
    treated as no window — callers get 0 and need no separate expiry check.
    """
    row = conn.execute(
        "SELECT until_ts FROM auto_help WHERE chat_id = ?", (int(chat_id),)
    ).fetchone()
    if not row:
        return 0
    until = int(row[0] or 0)
    ref = now if now is not None else _now()
    return until if until > int(ref) else 0


def clear_auto_help(conn: sqlite3.Connection, chat_id: int) -> None:
    """Close the auto-help window for ``chat_id`` (delete its row)."""
    conn.execute("DELETE FROM auto_help WHERE chat_id = ?", (int(chat_id),))


# ---------------------------------------------------------------------------
# Live tracking config — DB-backed override of the per-project file config so
# the management UI can change scope without a worker restart (inc. 3).
# ---------------------------------------------------------------------------


def get_config_row(
    conn: sqlite3.Connection, slug: str
) -> Optional[sqlite3.Row]:
    """Return the ``tracking_config`` row for ``slug``, or None if none set."""
    return conn.execute(
        "SELECT slug, enabled, mode, chats_json, realtime_json, claude_json, "
        "auto_draft_json, updated_at FROM tracking_config WHERE slug = ?",
        (slug,),
    ).fetchone()


def set_config_row(
    conn: sqlite3.Connection,
    slug: str,
    enabled: bool,
    mode: str,
    chats: list,
    realtime: list,
    claude: Optional[list] = None,
    auto_draft: Optional[list] = None,
    updated_at: Optional[int] = None,
) -> None:
    """Upsert the live tracking config for ``slug``.

    ``chats`` / ``realtime`` / ``claude`` / ``auto_draft`` are stored
    JSON-encoded, preserving each entry's type (int chat_id or string
    title/@username) so the pure scope matchers in ``tg_tracking`` treat them
    identically to file-config entries. ``claude`` is the @claude allowlist;
    ``auto_draft`` is the auto-draft allowlist. Both default to ``[]`` (off
    everywhere).
    """
    conn.execute(
        """
        INSERT INTO tracking_config
            (slug, enabled, mode, chats_json, realtime_json, claude_json,
             auto_draft_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            enabled         = excluded.enabled,
            mode            = excluded.mode,
            chats_json      = excluded.chats_json,
            realtime_json   = excluded.realtime_json,
            claude_json     = excluded.claude_json,
            auto_draft_json = excluded.auto_draft_json,
            updated_at      = excluded.updated_at
        """,
        (
            slug,
            1 if enabled else 0,
            mode,
            json.dumps(list(chats)),
            json.dumps(list(realtime)),
            json.dumps(list(claude or [])),
            json.dumps(list(auto_draft or [])),
            updated_at if updated_at is not None else _now(),
        ),
    )


# ---------------------------------------------------------------------------
# Auto-draft queue — the periodic tick (WORKER) / realtime path (BRIDGE thread)
# enqueue here; the bridge's _draft_setter (with the live Telethon session)
# drains it and sets each chat's Telegram draft. One PENDING row per chat.
# ---------------------------------------------------------------------------


def upsert_draft(conn: sqlite3.Connection, chat_id: int, text: str) -> None:
    """Queue (or re-queue) the draft to set for ``chat_id``; one pending per chat.

    If a row already exists it is UPDATED in place — ``text`` overwritten and
    ``status`` reset to ``queued`` (a regenerated suggestion replaces the prior
    queued one; ``last_set_text`` is PRESERVED so the clobber-guard still knows
    our last-written draft). An empty ``text`` is a CLEAR request: the setter
    will blank the draft (guarded by the clobber-guard). Otherwise a fresh row is
    INSERTed.
    """
    now = _now()
    existing = conn.execute(
        "SELECT chat_id FROM draft_queue WHERE chat_id = ?", (int(chat_id),)
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE draft_queue SET text = ?, status = 'queued', created_at = ?, "
            "error = NULL WHERE chat_id = ?",
            (text or "", now, int(chat_id)),
        )
        return
    conn.execute(
        """
        INSERT INTO draft_queue (chat_id, text, status, created_at)
        VALUES (?, ?, 'queued', ?)
        """,
        (int(chat_id), text or "", now),
    )


def list_queued_drafts(
    conn: sqlite3.Connection, limit: int = 50
) -> list[sqlite3.Row]:
    """Return queued draft rows (oldest first) for the bridge setter to drain."""
    return list(conn.execute(
        "SELECT * FROM draft_queue WHERE status = 'queued' "
        "ORDER BY created_at ASC, chat_id ASC LIMIT ?",
        (int(limit),),
    ).fetchall())


def mark_draft_set(
    conn: sqlite3.Connection, chat_id: int, text: str,
    set_at: Optional[int] = None,
) -> None:
    """Mark a chat's draft as SET; stamp ``set_at`` + record ``last_set_text``.

    ``last_set_text`` is what we actually wrote to the Telegram draft, so a later
    pass can distinguish our own draft (safe to overwrite) from text Tim typed.
    """
    conn.execute(
        "UPDATE draft_queue SET status = 'set', set_at = ?, last_set_text = ?, "
        "error = NULL WHERE chat_id = ?",
        (set_at if set_at is not None else _now(), text or "", int(chat_id)),
    )


def mark_draft_failed(
    conn: sqlite3.Connection, chat_id: int, error: str
) -> None:
    """Mark a chat's queued draft as failed; record the error string."""
    conn.execute(
        "UPDATE draft_queue SET status = 'failed', error = ? WHERE chat_id = ?",
        (str(error)[:300], int(chat_id)),
    )
