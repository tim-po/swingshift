"""Migration: tracking.db (chat-tracking inc. 1-3) -> product-core entities.

Milestone 2 of the product-core build (``claude-memory/product-core/MODEL.md`` +
``BUILD-LOG.md``). Maps the durable products of the three tracking pipelines onto
the locked core data model (``bot_squad_worker.core.store``) WITHOUT touching the
tracking schema or the (locked) ``store``/``__init__`` foundation:

    tracking row            -> core entity        trust              workflow state
    --------------------------------------------------------------------------------
    todos                   -> Task               see _todo_trust    see _todo_status
    meetings                -> Event              see _meeting_trust see _meeting_status
    suggested_replies       -> Artifact(raw)      see _reply_trust   state='raw'
    chat_daily_summaries    -> Artifact(distilled)'proposed'         state='distilled'

Cross-cutting:
- **Tags.** Every migrated entity is tagged ``chat:<chat_id>`` and
  ``project:<slug>`` (chats/projects are tags, not entities — MODEL.md).
- **Provenance.** A todo/meeting carrying a source ``msg_id`` gets a small source
  Artifact (``state='raw'``, content ``"tg msg <chat>/<msg>"``) and an
  ``entity -justified_by-> source`` edge. Source artifacts are DEDUPED per
  (chat_id, msg_id).

Idempotency: a ``migration_map`` table IN THE CORE DB (created here, NOT in
``store``) records (kind, origin_id) -> entity_id. Every row checks the map first
and SKIPs if already migrated, so re-running ``migrate_tracking`` never
duplicates. ``origin_id`` is a stable string per source row.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

from . import lifecycle, store
from .relabel import sanitize_title

# kinds recorded in the migration_map (origin namespace per source table)
_K_TODO = "todo"
_K_MEETING = "meeting"
_K_REPLY = "reply"
_K_SUMMARY = "summary"
_K_SOURCE = "source"   # the deduped provenance artifact per (chat_id, msg_id)

_MAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_map (
    kind       TEXT NOT NULL,             -- todo|meeting|reply|summary|source
    origin_id  TEXT NOT NULL,             -- stable id of the source row
    entity_id  TEXT NOT NULL,             -- core metadata.id it became
    created_at INTEGER NOT NULL,
    PRIMARY KEY (kind, origin_id)
);
"""


def _now() -> int:
    return int(time.time())


def _ensure_map(core_conn: sqlite3.Connection) -> None:
    core_conn.executescript(_MAP_SCHEMA)


def _mapped(core_conn: sqlite3.Connection, kind: str,
            origin_id: str) -> Optional[str]:
    row = core_conn.execute(
        "SELECT entity_id FROM migration_map WHERE kind=? AND origin_id=?",
        (kind, origin_id),
    ).fetchone()
    return row[0] if row else None


def _record(core_conn: sqlite3.Connection, kind: str, origin_id: str,
            entity_id: str) -> None:
    core_conn.execute(
        "INSERT OR IGNORE INTO migration_map(kind,origin_id,entity_id,created_at) "
        "VALUES(?,?,?,?)", (kind, origin_id, entity_id, _now()))


# --- status / trust mappings (the migration's domain logic) ----------------
def _todo_status(s: Optional[str]) -> str:
    # Board workflow is backlog|in_progress|done. A migrated todo lands in the
    # backlog (its board owner/assignee is decided later via core.lifecycle);
    # only an already-done todo is done.
    if s == "done":
        return "done"
    return "backlog"


def _todo_trust(s: Optional[str]) -> str:
    if s in ("approved", "done"):
        return "confirmed"
    if s == "rejected":
        return "rejected"
    return "proposed"


def _meeting_status(s: Optional[str]) -> str:
    # event workflow: accepted -> 'scheduled', declined -> 'missed', else
    # 'scheduled'
    if s == "declined":
        return "missed"
    return "scheduled"


def _meeting_trust(s: Optional[str]) -> str:
    if s in ("invited", "accepted"):
        return "confirmed"
    if s == "declined":
        return "rejected"
    return "proposed"


def _reply_trust(s: Optional[str]) -> str:
    # suggested_replies status: pending|sent|edited|skipped
    if s in ("sent", "edited"):
        return "confirmed"
    if s == "skipped":
        return "rejected"
    return "proposed"


def _snippet(text: Optional[str], n: int = 60) -> str:
    t = (text or "").strip().replace("\n", " ")
    return t[:n]


# --- chat naming -----------------------------------------------------------
def _chat_names(tracking_conn: sqlite3.Connection, slug: str) -> dict:
    """Build a ``chat_id -> human title`` map from the slug's tracking rows.

    ``chat_title`` lives on todos/meetings/suggested_replies (NOT on
    ``chat_daily_summaries``), so we harvest it there and reuse it for every
    entity of the chat — including summaries. Titles are sanitized; blank titles
    are ignored (the chat then falls back to its numeric id in ``_chat_tag``).
    """
    names: dict = {}
    for tbl in ("todos", "meetings", "suggested_replies"):
        for r in tracking_conn.execute(
                f"SELECT chat_id, chat_title FROM {tbl} WHERE slug=?", (slug,)):
            cid = r["chat_id"]
            title = sanitize_title(r["chat_title"])
            if cid is not None and title:
                names[cid] = title
    return names


def _chat_tag(chat_id, names: dict) -> str:
    """The ``chat:<name>`` scope tag for a chat, falling back to ``chat:<id>``
    when the chat has no known human title."""
    title = names.get(chat_id)
    return f"chat:{title}" if title else f"chat:{chat_id}"


# --- provenance ------------------------------------------------------------
def _source_artifact(core_conn: sqlite3.Connection, chat_id, msg_id, slug: str,
                     counts: dict, names: dict) -> str:
    """Return the (deduped) source-artifact id for (chat_id, msg_id), creating
    it + tagging it on first sight. Counts the artifact/tags only when new."""
    origin = f"{chat_id}:{msg_id}"
    existing = _mapped(core_conn, _K_SOURCE, origin)
    if existing:
        return existing
    a = store.create_artifact(
        core_conn,
        content=f"tg msg {chat_id}/{msg_id}",
        state="raw",
        label=f"source tg {chat_id}/{msg_id}",
        trust="proposed",
    )
    _tag(core_conn, a.id, chat_id, slug, counts, names)
    lifecycle.auto_confirm(core_conn, a.id)
    counts["artifacts"] += 1
    _record(core_conn, _K_SOURCE, origin, a.id)
    return a.id


def _tag(core_conn: sqlite3.Connection, eid: str, chat_id, slug: str,
         counts: dict, names: dict) -> None:
    """Apply the two scope tags to a freshly created entity; count them.

    The chat tag is ``chat:<contact name>`` (human, unique in the owner's Telegram) when
    a title is known, else ``chat:<id>``. The project tag is unchanged.
    """
    store.add_tag(core_conn, eid, _chat_tag(chat_id, names))
    # NOTE (Tim, 2026-07-03): personal-tracking entities are NOT blanket-tagged
    # project:<slug> — that made every chat hang off one project hub in the graph.
    # project:<slug> is for entities GENUINELY about the project, added
    # deliberately, not every migrated personal-chat item.
    counts["tags"] += 1


def _provenance(core_conn: sqlite3.Connection, eid: str, chat_id, msg_id,
                slug: str, counts: dict, names: dict) -> None:
    if msg_id is None:
        return
    src = _source_artifact(core_conn, chat_id, msg_id, slug, counts, names)
    store.add_edge(core_conn, eid, src, "justified_by")
    counts["edges"] += 1


# --- the migration ---------------------------------------------------------
def migrate_tracking(tracking_conn: sqlite3.Connection,
                     core_conn: sqlite3.Connection, slug: str) -> dict:
    """Migrate every ``slug``-scoped tracking row into core entities.

    Idempotent: re-running skips rows already in ``migration_map`` (returned in
    ``skipped``). Returns counts ``{tasks, events, artifacts, tags, edges,
    skipped}``. NOTE: ``artifacts`` includes deduped provenance *source*
    artifacts (not only replies/summaries); ``tags`` counts tag-applications.
    """
    _ensure_map(core_conn)
    counts = {"tasks": 0, "events": 0, "artifacts": 0,
              "tags": 0, "edges": 0, "skipped": 0}
    # Resolve chat_id -> human title once so every entity (incl. summaries) is
    # tagged chat:<contact name> rather than chat:<numeric id>.
    names = _chat_names(tracking_conn, slug)

    # --- todos -> Task -----------------------------------------------------
    for r in tracking_conn.execute(
            "SELECT * FROM todos WHERE slug=? ORDER BY id", (slug,)):
        origin = str(r["id"])
        if _mapped(core_conn, _K_TODO, origin):
            counts["skipped"] += 1
            continue
        t = store.create_task(
            core_conn,
            status=_todo_status(r["status"]),
            priority="P2",
            label=r["text"],
            trust=_todo_trust(r["status"]),
        )
        _tag(core_conn, t.id, r["chat_id"], slug, counts, names)
        _provenance(core_conn, t.id, r["chat_id"], r["msg_id"], slug, counts,
                    names)
        lifecycle.auto_confirm(core_conn, t.id)  # existence is not human-gated
        _record(core_conn, _K_TODO, origin, t.id)
        counts["tasks"] += 1

    # --- meetings -> Event -------------------------------------------------
    for r in tracking_conn.execute(
            "SELECT * FROM meetings WHERE slug=? ORDER BY id", (slug,)):
        origin = str(r["id"])
        if _mapped(core_conn, _K_MEETING, origin):
            counts["skipped"] += 1
            continue
        e = store.create_event(
            core_conn,
            status=_meeting_status(r["status"]),
            start_dt=r["start_dt"],
            end_dt=r["end_dt"],
            location=r["location"],
            label=r["title"],
            trust=_meeting_trust(r["status"]),
        )
        _tag(core_conn, e.id, r["chat_id"], slug, counts, names)
        _provenance(core_conn, e.id, r["chat_id"], r["msg_id"], slug, counts,
                    names)
        lifecycle.auto_confirm(core_conn, e.id)
        _record(core_conn, _K_MEETING, origin, e.id)
        counts["events"] += 1

    # --- suggested_replies -> Artifact(raw) --------------------------------
    for r in tracking_conn.execute(
            "SELECT * FROM suggested_replies WHERE slug=? ORDER BY id", (slug,)):
        origin = str(r["id"])
        if _mapped(core_conn, _K_REPLY, origin):
            counts["skipped"] += 1
            continue
        a = store.create_artifact(
            core_conn,
            content=r["draft_text"],
            state="raw",
            label=_snippet(r["draft_text"]),
            trust=_reply_trust(r["status"]),
        )
        _tag(core_conn, a.id, r["chat_id"], slug, counts, names)
        lifecycle.auto_confirm(core_conn, a.id)
        _record(core_conn, _K_REPLY, origin, a.id)
        counts["artifacts"] += 1

    # --- chat_daily_summaries -> Artifact(distilled) -----------------------
    for r in tracking_conn.execute(
            "SELECT * FROM chat_daily_summaries WHERE slug=? ORDER BY day",
            (slug,)):
        origin = f"{r['chat_id']}:{r['day']}"
        if _mapped(core_conn, _K_SUMMARY, origin):
            counts["skipped"] += 1
            continue
        a = store.create_artifact(
            core_conn,
            content=r["summary"],
            state="distilled",
            # Date STUB label (retitle_summaries later replaces it with a real
            # subject). Uses the chat NAME, not the numeric id.
            label=f"{r['day']} {_chat_tag(r['chat_id'], names)}",
            trust="proposed",
        )
        _tag(core_conn, a.id, r["chat_id"], slug, counts, names)
        lifecycle.auto_confirm(core_conn, a.id)
        _record(core_conn, _K_SUMMARY, origin, a.id)
        counts["artifacts"] += 1

    return counts
