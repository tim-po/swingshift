"""Standalone management frontend for the Telegram chat-tracking system (inc. 3).

A small, self-contained FastAPI app — SEPARATE from the worker daemon — that lets
Tim (1) manage tracking scope LIVE (enable, include/exclude mode, the tracked
chats list, the realtime sub-list) with no worker restart, and (2) view/approve
todos / meetings / suggested-replies.

It reuses the worker's own modules (``bot_squad_worker.config`` /
``tg_tracking`` / ``tracking_store``) so the UI writes the EXACT same live
``tracking_config`` rows the pipelines read. Approvals only RECORD the decision +
status for now; executing todos and sending replies is a later increment (inc. 4).

SECURITY POSTURE (inc. 3): bind 127.0.0.1 ONLY. Personal message content lives in
messages.db / tracking.db on-host and must never leave it; public exposure of this
UI is still an open coord/Tim decision (localhost + SSH tunnel vs public + token
vs HTTPS). Every ``/api`` route requires ``Authorization: Bearer <token>``.

Run (from the install root — the tree this file ships in):
    worker/.venv/bin/python tracking_ui/app.py
It defaults to that install root; set ``BOT_SQUAD_HOME`` to run it from elsewhere.
The bearer token is read from (or generated into) config/secrets.toml under
``[tracking_ui] token``.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

# Make the worker package importable when launched as a bare script. The systemd
# unit runs this via the worker venv (which has bot_squad_worker installed), but
# add the parent worker dir to sys.path too so a plain ``python app.py`` works.
_HERE = Path(__file__).resolve().parent
_WORKER_DIR = _HERE.parent / "worker"
if _WORKER_DIR.exists() and str(_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(_WORKER_DIR))

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bot_squad_worker import icloud_calendar, tg_pipelines, tg_tracking, tracking_store  # noqa: E402
from bot_squad_worker.core import (  # noqa: E402
    inspect as core_inspect,
    lifecycle as core_lifecycle,
    store as core_store,
    team as core_team,
)

_STATIC = _HERE / "static"
_INDEX = _STATIC / "index.html"

# Note appended to every decision response — execution/sending is deferred.
_DEFER_NOTE = (
    "Decision recorded. Execution (running todos) and sending (replies as Tim) "
    "are NOT wired yet — deferred to a later increment."
)

# Per-type decision vocabulary. The UI sends {"approve","dismiss"}; we map each
# to the status the underlying store expects.
_TODO_STATUS = {"approve": "approved", "dismiss": "rejected"}
_REPLY_STATUS = {"approve": "approved", "dismiss": "skipped"}
_MEETING_STATUS = {"approve": "approved", "dismiss": "dismissed"}

_VALID_TYPES = {"todos", "meetings", "replies"}


# ---------------------------------------------------------------------------
# Request models.
# ---------------------------------------------------------------------------


class ConfigBody(BaseModel):
    slug: str
    enabled: bool = False
    mode: str = "exclude"
    chats: list = []
    realtime: list = []
    # @claude allowlist — chats where the @claude trigger / auto-help may fire.
    # Independent of tracked/realtime; default [] ⇒ @claude off everywhere.
    claude: list = []
    # Auto-draft allowlist — chats where a suggested reply is pre-filled into
    # Tim's input box as the chat DRAFT (nothing sent). Independent of the other
    # toggles; default [] ⇒ auto-draft off everywhere.
    auto_draft: list = []


class DecisionBody(BaseModel):
    decision: str


class SendBody(BaseModel):
    # Tim's (optionally edited) text. When omitted/empty the reply's stored
    # draft_text is sent verbatim.
    text: Optional[str] = None


class SweepBody(BaseModel):
    slug: str = "swarmdev"
    chat_id: Optional[int] = None
    days: int = 7


class WorkerBody(BaseModel):
    # Onboard a worker BY HAND into the roster (curated, not auto-derived).
    name: str
    kind: str = "human"
    role: str = "member"
    capabilities: Optional[str] = None
    channel: Optional[str] = None


# ---------------------------------------------------------------------------
# DB helpers.
# ---------------------------------------------------------------------------


def _open_messages_ro(path: Path) -> Optional[sqlite3.Connection]:
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _entry_key(v: Any) -> str:
    """Normalize a chats/realtime entry for subset comparison (int vs str)."""
    return str(v).strip().lstrip("@").lower()


# ---------------------------------------------------------------------------
# App factory.
# ---------------------------------------------------------------------------


def create_app(cfg: Any, token: str) -> FastAPI:
    """Build the FastAPI app over a ``cfg`` (paths + projects) and bearer token.

    ``cfg`` must expose ``tg_user_db_path``, ``tg_tracking_db_path`` and
    ``projects`` (a real ``bot_squad_worker.config.Config`` in production; a
    SimpleNamespace with temp paths in tests).
    """
    app = FastAPI(title="TG chat-tracking UI", docs_url=None, redoc_url=None)

    def require_token(authorization: str = Header(default="")) -> None:
        expected = f"Bearer {token}"
        # Constant-time compare to avoid leaking the token via timing.
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    auth = [Depends(require_token)]

    # --- chats ---------------------------------------------------------------

    @app.get("/api/chats", dependencies=auth)
    def api_chats() -> list[dict]:
        conn = _open_messages_ro(cfg.tg_user_db_path)
        if conn is None:
            return []
        try:
            rows = conn.execute(
                """
                SELECT c.chat_id AS chat_id, c.title AS title, c.kind AS kind,
                       MAX(m.ts) AS last_ts, COUNT(m.msg_id) AS msg_count
                FROM chats c
                LEFT JOIN messages m ON m.chat_id = c.chat_id
                GROUP BY c.chat_id, c.title, c.kind
                ORDER BY last_ts DESC NULLS LAST
                LIMIT 500
                """
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "chat_id": r["chat_id"],
                "title": r["title"],
                "kind": r["kind"],
                "last_ts": r["last_ts"],
                "msg_count": r["msg_count"],
            }
            for r in rows
        ]

    # --- config (effective, DB-first) ---------------------------------------

    @app.get("/api/config", dependencies=auth)
    def api_get_config(slug: str = "swarmdev") -> dict:
        eff = tg_tracking.get_effective_tracking(cfg, slug)
        eff["slug"] = slug
        return eff

    @app.put("/api/config", dependencies=auth)
    def api_put_config(body: ConfigBody) -> dict:
        mode = (body.mode or "").lower()
        if mode not in {"include", "exclude"}:
            raise HTTPException(status_code=422, detail="mode must be include|exclude")
        if mode == "include":
            chat_keys = {_entry_key(c) for c in body.chats}
            stray = [r for r in body.realtime if _entry_key(r) not in chat_keys]
            if stray:
                raise HTTPException(
                    status_code=422,
                    detail=f"realtime must be a subset of chats; stray: {stray}",
                )
        conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
        try:
            tracking_store.set_config_row(
                conn, body.slug, body.enabled, mode,
                list(body.chats), list(body.realtime),
                claude=list(body.claude),
                auto_draft=list(body.auto_draft),
            )
        finally:
            conn.close()
        return {"ok": True, "slug": body.slug}

    # --- items (todos / meetings / replies) ---------------------------------

    @app.get("/api/items", dependencies=auth)
    def api_items(
        slug: str = "swarmdev",
        type: str = "todos",
        status: Optional[str] = None,
    ) -> list[dict]:
        if type not in _VALID_TYPES:
            raise HTTPException(status_code=422, detail="type must be todos|meetings|replies")
        lister = {
            "todos": tracking_store.list_todos,
            "meetings": tracking_store.list_meetings,
            "replies": tracking_store.list_replies,
        }[type]
        conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
        try:
            # Meetings: hide SEMANTIC duplicates (status='duplicate') — a row that
            # was folded into another by dedupe_meetings — unless the caller
            # explicitly asks for that status (recovery). The survivor stays
            # visible. Other types / explicit-status queries are unchanged.
            if type == "meetings" and status is None:
                rows = tracking_store.list_meetings_excluding_duplicates(
                    conn, slug=slug, limit=200)
            else:
                rows = lister(conn, slug=slug, status=status, limit=200)
        finally:
            conn.close()
        items = [dict(r) for r in rows]
        # For replies, attach the recent conversation the draft is answering, so
        # the reviewer sees context above the suggested reply. Prefer the
        # context the draft was generated against (stored as context_json by the
        # reply pipeline); only fall back to a live messages.db query for older
        # rows that predate that column.
        if type == "replies" and items:
            need_live = []
            for it in items:
                cj = it.get("context_json")
                if cj:
                    try:
                        it["context"] = json.loads(cj)
                        continue
                    except Exception:  # noqa: BLE001
                        pass
                need_live.append(it)
            mconn = _open_messages_ro(cfg.tg_user_db_path) if need_live else None
            if mconn is not None:
                try:
                    for it in need_live:
                        ctx = []
                        for m in mconn.execute(
                            "SELECT sender_name, text, ts, raw_json FROM messages "
                            "WHERE chat_id=? AND msg_id<=? AND text!='' "
                            "ORDER BY msg_id DESC LIMIT 8",
                            (it.get("chat_id"), it.get("reply_to_msg_id") or 1 << 62),
                        ):
                            try:
                                out = bool(json.loads(m["raw_json"] or "{}").get("out"))
                            except Exception:  # noqa: BLE001
                                out = False
                            ctx.append({"sender_name": m["sender_name"], "text": m["text"],
                                        "ts": m["ts"], "out": out})
                        it["context"] = list(reversed(ctx))  # chronological
                finally:
                    mconn.close()
        return items

    # --- decision (record only; no execution/sending yet) -------------------

    @app.post("/api/items/{type}/{item_id}/decision", dependencies=auth)
    def api_decision(type: str, item_id: int, body: DecisionBody) -> dict:
        if type not in _VALID_TYPES:
            raise HTTPException(status_code=422, detail="type must be todos|meetings|replies")
        decision = (body.decision or "").lower()
        table = {
            "todos": _TODO_STATUS,
            "replies": _REPLY_STATUS,
            "meetings": _MEETING_STATUS,
        }[type]
        if decision not in table:
            raise HTTPException(status_code=422, detail="decision must be approve|dismiss")
        new_status = table[decision]
        note = _DEFER_NOTE
        conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
        try:
            # Each set_*_status returns the rowcount; a 0-row UPDATE means the id
            # doesn't exist (stale UI, already-removed item). Report that as a 404
            # instead of a silent 200 {"ok":true} — matching /send and /api/core/*
            # so the operator can't confuse a real decision with one into the void.
            if type == "todos":
                changed = tracking_store.set_todo_status(conn, item_id, new_status)
            elif type == "replies":
                # Replies no longer carry the generic "sending not wired" defer
                # note — sending is the explicit /send endpoint. Dismiss (skipped)
                # is a plain decision record.
                changed = tracking_store.set_reply_status(conn, item_id, new_status)
                note = "Dismissed." if decision == "dismiss" else "Recorded."
            else:
                changed = tracking_store.set_meeting_status(conn, item_id, new_status)
            if not changed:
                singular = {"todos": "todo", "replies": "reply", "meetings": "meeting"}[type]
                raise HTTPException(status_code=404, detail=f"{singular} not found")
            if type == "meetings" and decision == "approve":
                note = _approve_meeting_to_calendar(conn, item_id)
        finally:
            conn.close()
        return {
            "ok": True,
            "type": type,
            "id": item_id,
            "status": new_status,
            "note": note,
        }

    def _approve_meeting_to_calendar(conn: Any, meeting_id: int) -> str:
        """On meeting approval, add it to the iCloud Swarm calendar (if it has a
        real datetime + iCloud is configured). Idempotent via the ics_uid column."""
        try:
            row = conn.execute(
                "SELECT title, start_dt, end_dt, location, ics_uid FROM meetings "
                "WHERE id=?", (meeting_id,)
            ).fetchone()
        except Exception:  # noqa: BLE001
            return "Approved (could not read meeting row)."
        if row is None:
            return "Approved."
        title, start_dt, end_dt, location, ics_uid = (
            row["title"], row["start_dt"], row["end_dt"], row["location"], row["ics_uid"])
        if ics_uid:
            return "Approved — already in your Swarm calendar."
        if not start_dt:
            return "Approved — no date/time on this meeting, so nothing added to the calendar."
        if not icloud_calendar.is_configured(cfg.config_dir):
            return "Approved — iCloud not configured; calendar event not added."
        try:
            uid = icloud_calendar.add_meeting(
                cfg.config_dir, title or "(meeting)", start_dt, end_dt, location=location)
            conn.execute("UPDATE meetings SET ics_uid=? WHERE id=?", (uid, meeting_id))
            conn.commit()
            return "Approved and added to your iCloud Swarm calendar ✓"
        except Exception as e:  # noqa: BLE001
            return f"Approved, but adding to calendar failed: {type(e).__name__}: {str(e)[:120]}"

    # --- send-as-Tim (enqueue for the bridge to send) -----------------------
    # The bridge process owns the only live Telethon session, so this endpoint
    # NEVER sends directly: it enqueues an outgoing_messages row and flips the
    # reply to 'sent'. The bridge's _outgoing_sender drains the queue and does
    # the actual Telegram send (from Tim's account). Sending is the EXPLICIT
    # confirm, per-reply (separate from the Dismiss decision endpoint).

    @app.post("/api/items/replies/{reply_id}/send", dependencies=auth)
    def api_send_reply(reply_id: int, body: SendBody) -> dict:
        conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
        try:
            row = conn.execute(
                "SELECT slug, chat_id, reply_to_msg_id, draft_text "
                "FROM suggested_replies WHERE id = ?",
                (int(reply_id),),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="reply not found")
            text = (body.text if body.text is not None else None)
            if text is None or not str(text).strip():
                text = row["draft_text"] or ""
            if not str(text).strip():
                raise HTTPException(status_code=422, detail="empty message")
            out_id = tracking_store.enqueue_outgoing(
                conn,
                slug=row["slug"],
                chat_id=row["chat_id"],
                text=text,
                reply_to_msg_id=row["reply_to_msg_id"],
                source_reply_id=int(reply_id),
            )
            tracking_store.set_reply_status(conn, int(reply_id), "sent")
        finally:
            conn.close()
        return {
            "ok": True,
            "id": reply_id,
            "outgoing_id": out_id,
            "status": "sent",
            "note": "Queued — sending from your Telegram account.",
        }

    # --- on-demand historic sweep -------------------------------------------
    # The sweep runs LLM calls (claude subprocesses, via tg_pipelines, which
    # strips the proxy itself) and takes MINUTES, so it runs in a BACKGROUND
    # thread and the POST returns immediately. A single in-process flag guards
    # against concurrent sweeps; GET /api/sweep/status reports progress + the
    # last result. Items appear in the tabs as extraction writes them.

    sweep_lock = threading.Lock()
    sweep_state: dict = {
        "running": False, "target": None, "started_at": None, "last_result": None,
    }

    def _run_sweep(slug: str, chat_id: Optional[int], days: int) -> None:
        try:
            if chat_id is not None:
                proj = (getattr(cfg, "projects", {}) or {}).get(slug)
                if proj is None:
                    proj = SimpleNamespace(
                        slug=slug, tg_track_enabled=True, tg_track_mode="exclude",
                        tg_track_chats=(), tg_track_realtime=(),
                    )
                eff = tg_tracking.effective_proj(cfg, proj)
                res = tg_pipelines.sweep_chat(cfg, eff, int(chat_id), days=days)
            else:
                res = tg_pipelines.sweep_tracked(cfg, slug, days=days)
            result = {"ok": True, **res}
        except Exception as e:  # noqa: BLE001 — surface the error via status.
            result = {"ok": False, "error": str(e)}
        with sweep_lock:
            sweep_state["last_result"] = result
            sweep_state["running"] = False

    @app.post("/api/sweep", dependencies=auth)
    def api_sweep(body: SweepBody) -> dict:
        days = max(1, int(body.days or 7))
        target = {"slug": body.slug, "chat_id": body.chat_id, "days": days}
        with sweep_lock:
            if sweep_state["running"]:
                return {"started": False, "busy": True, "target": sweep_state["target"]}
            sweep_state["running"] = True
            sweep_state["target"] = target
            sweep_state["started_at"] = int(time.time())
            sweep_state["last_result"] = None
        threading.Thread(
            target=_run_sweep,
            args=(body.slug, body.chat_id, days),
            daemon=True,
        ).start()
        return {"started": True, "target": target}

    @app.get("/api/sweep/status", dependencies=auth)
    def api_sweep_status() -> dict:
        with sweep_lock:
            return {
                "running": sweep_state["running"],
                "target": sweep_state["target"],
                "started_at": sweep_state["started_at"],
                "last_result": sweep_state["last_result"],
            }

    # --- product-core review surface ----------------------------------------
    # Read-only-ish view over the sibling core.db (entities + tags-as-collections
    # + edge graph + the pending-confirm queue). The one write is the visual
    # confirm/reject gate. Every handler opens its OWN conn and closes it. If the
    # core.db file doesn't exist yet, endpoints degrade to an empty typed shape
    # with ``"core_db": false`` rather than 500-ing.

    def _core_db_path() -> Path:
        return Path(cfg.tg_tracking_db_path).parent / "core.db"

    def _core_conn():
        return core_store.connect(_core_db_path())

    def _entity_dict(conn: Any, e: Any) -> dict:
        return {
            "id": e.id, "type": e.type, "label": e.label, "trust": e.trust,
            "payload": e.payload, "tags": core_store.tags_of(conn, e.id),
        }

    def _ref(conn: Any, eid: str) -> dict:
        t = core_store.get(conn, eid)
        if t is None:
            return {"id": eid, "type": None, "label": None}
        return {"id": t.id, "type": t.type, "label": t.label}

    @app.get("/api/core/snapshot", dependencies=auth)
    def api_core_snapshot() -> dict:
        if not _core_db_path().exists():
            return {"core_db": False,
                    "totals": {"by_type": {}, "by_trust": {}},
                    "pending_confirm": [], "tags": {}, "dependencies": []}
        conn = _core_conn()
        try:
            snap = core_inspect.snapshot(conn)
        finally:
            conn.close()
        snap["core_db"] = True
        return snap

    @app.get("/api/core/entities", dependencies=auth)
    def api_core_entities(
        type: Optional[str] = None,
        trust: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> dict:
        if not _core_db_path().exists():
            return {"core_db": False, "entities": []}
        conn = _core_conn()
        try:
            if tag:
                ents = core_store.entities_by_tag(conn, tag, type_=type, trust=trust)
            elif type:
                ents = core_store.by_type(conn, type, trust=trust)
            elif trust:
                ents = core_store.by_trust(conn, trust)
            else:
                ents = []
                for t in core_store.TYPES:
                    ents.extend(core_store.by_type(conn, t))
            out = [_entity_dict(conn, e) for e in ents if e]
        finally:
            conn.close()
        return {"core_db": True, "entities": out}

    @app.get("/api/core/entity/{eid}", dependencies=auth)
    def api_core_entity(eid: str) -> dict:
        if not _core_db_path().exists():
            return {"core_db": False, "entity": None}
        conn = _core_conn()
        try:
            e = core_store.get(conn, eid)
            if e is None:
                raise HTTPException(status_code=404, detail="unknown entity")
            detail = _entity_dict(conn, e)
            detail["core_db"] = True
            detail["blocks"] = [_ref(conn, i) for i in core_store.blocks(conn, eid)]
            detail["depends_on"] = [_ref(conn, i) for i in core_store.depends_on(conn, eid)]
            detail["provenance"] = [_ref(conn, i) for i in core_store.justified_by(conn, eid)]
            # any OTHER outgoing edges, grouped by type (blocks + justified_by
            # already surfaced above under their own keys).
            edges: dict = {}
            for et in core_store.EDGE_TYPES:
                if et in ("blocks", "justified_by"):
                    continue
                outs = core_store.edges_out(conn, eid, et)
                if outs:
                    edges[et] = [_ref(conn, i) for i in outs]
            detail["edges"] = edges
        finally:
            conn.close()
        return detail

    @app.get("/api/core/tag/{name}", dependencies=auth)
    def api_core_tag(name: str) -> dict:
        if not _core_db_path().exists():
            return {"core_db": False, "members": [], "meta_artifact": None}
        conn = _core_conn()
        try:
            members = [_entity_dict(conn, e) for e in core_store.entities_by_tag(conn, name)]
            meta = core_store.tag_meta_artifact(conn, name)
            meta_content = meta.get("content") if meta else None
        finally:
            conn.close()
        return {"core_db": True, "members": members, "meta_artifact": meta_content}

    @app.post("/api/core/entity/{eid}/{decision}", dependencies=auth)
    def api_core_decision(eid: str, decision: str) -> dict:
        if decision not in ("confirm", "reject"):
            raise HTTPException(status_code=422, detail="decision must be confirm|reject")
        if not _core_db_path().exists():
            raise HTTPException(status_code=404, detail="core.db not found")
        conn = _core_conn()
        try:
            e = core_store.get(conn, eid)
            if e is None:
                raise HTTPException(status_code=404, detail="unknown entity")
            if decision == "confirm":
                core_store.confirm(conn, eid)
            else:
                core_store.reject(conn, eid)
            new = core_store.get(conn, eid)
        finally:
            conn.close()
        return {"ok": True, "trust": new.trust if new else None}

    # --- Knowledge view (readable artifacts + a connections graph) -----------
    # A human-readable window on the artifact entities in core.db: a filterable
    # list (list/search), a full single-artifact reader (content + provenance +
    # resolved links), and a capped connections graph for charting. All reads;
    # every handler opens + closes its own conn and degrades to an empty typed
    # shape with ``"core_db": false`` when core.db doesn't exist yet.

    # Max nodes a graph response will carry so the chart stays renderable. Nodes
    # are picked most-recent-first; anything beyond the cap sets truncated=true.
    _GRAPH_NODE_CAP = 120
    # Cap on how many tag nodes a graph carries so hundreds of rarely-shared
    # tags can't explode the chart. Tags are picked most-members-first; only
    # tags with >=1 included member are ever emitted.
    _GRAPH_TAG_CAP = 60

    def _title_of(conn: Any, eid: str) -> Optional[str]:
        t = core_store.get(conn, eid)
        return t.label if t else None

    @app.get("/api/core/knowledge", dependencies=auth)
    def api_core_knowledge(
        tag: Optional[str] = None,
        state: Optional[str] = None,
        q: Optional[str] = None,
    ) -> dict:
        """Readable artifact list. ``?tag=`` scopes to a collection, ``?state=``
        filters raw|distilled|canonical|archived (or ``all``), ``?q=`` is a
        case-insensitive substring search over label+content. Newest first. The
        DEFAULT view (no ``?state=``) shows only active distilled+canonical —
        raw stubs and librarian-archived artifacts are hidden."""
        if not _core_db_path().exists():
            return {"core_db": False, "artifacts": []}
        conn = _core_conn()
        try:
            sql = ["SELECT m.id FROM metadata m "
                   "JOIN artifact a ON a.metadata_id = m.id"]
            args: list[Any] = []
            if tag:
                sql.append("JOIN metadata_tag mt ON mt.metadata_id = m.id "
                           "JOIN tag t ON t.id = mt.tag_id")
            sql.append("WHERE m.type = 'artifact'")
            if tag:
                sql.append("AND t.name = ?")
                args.append(tag)
            if state in ("raw", "distilled", "canonical", "archived"):
                sql.append("AND a.state = ?")
                args.append(state)
            elif state != "all":
                # Default (no state / "any") = ACTIVE MEANINGFUL only: hide raw
                # per-message provenance stubs + draft artifacts (noise) AND the
                # librarian-archived (stale/irrelevant) ones. Only active
                # distilled/canonical belong here. ?state=all / raw / archived
                # still returns the hidden ones (archived stays recoverable).
                sql.append("AND a.state NOT IN ('raw', 'archived')")
            if q:
                sql.append("AND (LOWER(COALESCE(m.label, '')) LIKE ? "
                           "OR LOWER(COALESCE(a.content, '')) LIKE ?)")
                like = f"%{q.lower()}%"
                args.extend([like, like])
            # newest-first; rowid breaks same-second ties deterministically.
            sql.append("ORDER BY m.created_at DESC, m.rowid DESC")
            ids = [r["id"] for r in conn.execute(" ".join(sql), args)]
            out = []
            for eid in ids:
                e = core_store.get(conn, eid)
                if e is None:
                    continue
                content = e.get("content") or ""
                out.append({
                    "id": e.id,
                    "title": e.label,
                    "snippet": content[:200],
                    "state": e.get("state"),
                    "tags": core_store.tags_of(conn, e.id),
                    "created": e.created_at,
                    "updated": e.updated_at,
                    "has_content": bool(content),
                })
        finally:
            conn.close()
        return {"core_db": True, "artifacts": out}

    @app.get("/api/core/artifact/{aid}", dependencies=auth)
    def api_core_artifact(aid: str) -> dict:
        """Full readable artifact: FULL content, provenance (the artifact's
        outgoing ``justified_by`` targets), and every OTHER edge in/out resolved
        to titles. 404 if the id is missing or is not an artifact."""
        if not _core_db_path().exists():
            raise HTTPException(status_code=404, detail="unknown artifact")
        conn = _core_conn()
        try:
            e = core_store.get(conn, aid)
            if e is None or e.type != "artifact":
                raise HTTPException(status_code=404, detail="unknown artifact")
            provenance = [
                {"id": i, "title": _title_of(conn, i)}
                for i in core_store.justified_by(conn, aid)
            ]
            links = []
            for r in conn.execute(
                    "SELECT to_id AS other, type FROM edge WHERE from_id = ?",
                    (aid,)):
                # outgoing justified_by is surfaced as provenance above.
                if r["type"] == "justified_by":
                    continue
                links.append({"id": r["other"], "title": _title_of(conn, r["other"]),
                              "type": r["type"], "dir": "out"})
            for r in conn.execute(
                    "SELECT from_id AS other, type FROM edge WHERE to_id = ?",
                    (aid,)):
                links.append({"id": r["other"], "title": _title_of(conn, r["other"]),
                              "type": r["type"], "dir": "in"})
            detail = {
                "core_db": True,
                "id": e.id,
                "title": e.label,
                "content": e.get("content"),
                "state": e.get("state"),
                "tags": core_store.tags_of(conn, e.id),
                "created": e.created_at,
                "updated": e.updated_at,
                "provenance": provenance,
                "links": links,
            }
        finally:
            conn.close()
        return detail

    @app.get("/api/core/graph", dependencies=auth)
    def api_core_graph(tag: Optional[str] = None) -> dict:
        """Connections graph for a chart. Nodes = artifacts + the entities they
        connect to via edges (or, with ``?tag=``, the whole collection), PLUS a
        ``type:"tag"`` node per shared tag so entities that only share a tag
        (e.g. a ``chat:`` collection with no entity->entity edges) still cluster
        visibly. Links = edge rows where BOTH endpoints are in the node set, PLUS
        ``type:"tagged"`` membership links from each entity to its tag nodes.
        Capped most-recent-first at ~120 entity nodes + ~60 tag nodes with a
        ``truncated`` flag so it stays renderable."""
        if not _core_db_path().exists():
            return {"core_db": False, "nodes": [], "links": [], "truncated": False}
        conn = _core_conn()
        try:
            candidates: dict[str, Any] = {}
            if tag:
                # scope to one collection (all entity types sharing the tag).
                for e in core_store.entities_by_tag(conn, tag):
                    candidates[e.id] = e
            else:
                # artifacts + every entity they touch via an edge.
                arts = core_store.by_type(conn, "artifact")
                for a in arts:
                    candidates[a.id] = a
                for a in arts:
                    for nid in (core_store.edges_out(conn, a.id)
                                + core_store.edges_in(conn, a.id)):
                        if nid not in candidates:
                            ne = core_store.get(conn, nid)
                            if ne is not None:
                                candidates[nid] = ne
            ents = sorted(candidates.values(),
                          key=lambda e: e.created_at, reverse=True)
            truncated = len(ents) > _GRAPH_NODE_CAP
            ents = ents[:_GRAPH_NODE_CAP]
            node_ids = {e.id for e in ents}
            nodes = []
            for e in ents:
                node = {"id": e.id, "type": e.type,
                        "label": e.label or (e.id[:8] if e.id else "")}
                if e.type == "artifact":
                    node["state"] = e.get("state")
                nodes.append(node)
            links = []
            for r in conn.execute("SELECT from_id, to_id, type FROM edge"):
                if r["from_id"] in node_ids and r["to_id"] in node_ids:
                    links.append({"source": r["from_id"], "target": r["to_id"],
                                  "type": r["type"]})
            # Tag nodes + membership links: collect every tag each included
            # entity carries (skipping the scoping tag itself, which every node
            # shares and so adds no structure), keep only tags with >=1 member,
            # then take the most-shared tags first up to _GRAPH_TAG_CAP.
            memberships: dict[str, list[str]] = {}
            for e in ents:
                for tname in core_store.tags_of(conn, e.id):
                    if tag and tname == tag:
                        continue
                    memberships.setdefault(tname, []).append(e.id)
            ordered_tags = sorted(memberships.items(),
                                  key=lambda kv: (-len(kv[1]), kv[0]))
            if len(ordered_tags) > _GRAPH_TAG_CAP:
                truncated = True
                ordered_tags = ordered_tags[:_GRAPH_TAG_CAP]
            for name, _members in ordered_tags:
                nodes.append({"id": "tag:" + name, "type": "tag",
                              "label": name})
            for name, members in ordered_tags:
                tid = "tag:" + name
                for eid in members:
                    links.append({"source": eid, "target": tid,
                                  "type": "tagged"})
        finally:
            conn.close()
        return {"core_db": True, "nodes": nodes, "links": links,
                "truncated": truncated}

    # --- agile board (task-autonomy lifecycle) -------------------------------
    # EXISTENCE is never human-gated; the only human gate is approving HIGH-risk
    # AI-task EXECUTION. These endpoints drive core.lifecycle over the tasks in
    # core.db and degrade to an empty board when core.db doesn't exist yet.

    def _task_card(conn: Any, e: Any) -> dict:
        tags = core_store.tags_of(conn, e.id)
        # WHO IS ASKING. The board is ONE shared core.db sliced by a
        # `project:<slug>` tag, so a decision row without its slice is an
        # anonymous demand: Tim saw a bare label and could not tell whether
        # plancheck, computation or the platform was waiting on him, nor which
        # coordinator would act on the answer. Derived here rather than in the
        # page so every consumer of a card gets it.
        project = next((t.split(":", 1)[1] for t in tags
                        if t.startswith("project:")), None)
        return {
            "id": e.id, "label": e.label, "priority": e.get("priority"),
            "due_date": e.get("due_date"), "status": e.get("status"),
            "assignee": e.get("assignee"), "risk": e.get("risk"),
            "approval": e.get("approval"),
            "archive_reason": e.get("archive_reason"),
            "project": project,
            # THE QUESTION, not the brief. Nullable by construction — the page
            # must say an ask is MISSING rather than render a blank row.
            "ask": e.get("ask"),
            "recommend": e.get("recommend"),
            "tags": tags,
        }

    def _get_task_or_404(conn: Any, tid: str) -> Any:
        t = core_store.get(conn, tid)
        if t is None or t.type != "task":
            raise HTTPException(status_code=404, detail="unknown task")
        return t

    @app.get("/api/core/board", dependencies=auth)
    def api_core_board() -> dict:
        if not _core_db_path().exists():
            return {"core_db": False,
                    "columns": {"backlog": [], "in_progress": [], "done": []},
                    "needs_approval": [], "stale_approvals": [], "archived": []}
        conn = _core_conn()
        try:
            columns: dict[str, list] = {"backlog": [], "in_progress": [], "done": []}
            needs_approval: list = []
            stale_approvals: list = []
            archived: list = []
            for t in core_store.by_type(conn, "task"):
                card = _task_card(conn, t)
                # Archived tasks are filed away: keep them OUT of the board
                # columns + the approval lane; surface them under their own key.
                if card["status"] == "archived":
                    archived.append(card)
                    continue
                if card["status"] in columns:
                    columns[card["status"]].append(card)
                if card["approval"] == "needed":
                    # A DONE task is not awaiting a decision. Coordinators close
                    # work without clearing the flag (the answer arrived in chat,
                    # or events overtook the question), and every one of those
                    # sat in Tim's queue forever. MEASURED on the live board
                    # 2026-07-22: 7 of 15 rows — 47% of what we were calling
                    # 'decisions waiting on Tim' was already finished. A queue
                    # that is half noise is a queue nobody reads, and the real
                    # ones drown in it. core.heartbeat.should_ping_tim has always
                    # required a LIVE status; this page was the outlier.
                    #
                    # Dropped from the lane but NOT swallowed: a done row still
                    # flagged 'needed' is a real inconsistency, and hiding it is
                    # how it becomes permanent. It gets its own key so the page
                    # can say so and someone can clear it.
                    if card["status"] == "done":
                        stale_approvals.append(card)
                    else:
                        needs_approval.append(card)
        finally:
            conn.close()
        return {"core_db": True, "columns": columns,
                "needs_approval": needs_approval,
                "stale_approvals": stale_approvals, "archived": archived}

    @app.post("/api/core/task/{tid}/assign/{who}", dependencies=auth)
    def api_core_assign(tid: str, who: str) -> dict:
        if who not in ("ai", "human"):
            raise HTTPException(status_code=422, detail="who must be ai|human")
        if not _core_db_path().exists():
            raise HTTPException(status_code=404, detail="core.db not found")
        conn = _core_conn()
        try:
            _get_task_or_404(conn, tid)
            if who == "human":
                core_lifecycle.assign_human(conn, tid)
                outcome = {"risk": None, "auto_started": False,
                           "needs_approval": False}
            else:
                # HIGH-risk approvals route by ROLE (active owners/admins),
                # falling back to the configured chat — see core.lifecycle.
                outcome = core_lifecycle.assign_ai(
                    conn, tid,
                    tg_notify=lambda task: core_lifecycle.notify_approval(cfg, conn, task))
            card = _task_card(conn, core_store.get(conn, tid))
        finally:
            conn.close()
        return {"core_db": True, "task": card, **outcome}

    def _lifecycle_transition(tid: str, fn) -> dict:
        if not _core_db_path().exists():
            raise HTTPException(status_code=404, detail="core.db not found")
        conn = _core_conn()
        try:
            _get_task_or_404(conn, tid)
            fn(conn, tid)
            card = _task_card(conn, core_store.get(conn, tid))
        finally:
            conn.close()
        return {"core_db": True, "task": card}

    def _utc_stamp() -> str:
        """UTC, always. The host clock is Etc/UTC and Tim is UTC+3, so a naive
        local timestamp in an audit line is a lie waiting to be read wrong."""
        return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())

    def _record_decision(conn: Any, tid: str, before: str, verdict: str) -> None:
        """Write the AUDIT TRAIL for a human decision: what, when, from where.

        The approval field records only the OUTCOME. It cannot say when the
        decision was taken, or that a human took it at all — a coordinator
        running `set-approval approved` on its own board produces a byte-identical
        row to Tim clicking Approve. So an approval could not be distinguished
        from a coordinator approving its own work, which is the one thing the
        gate exists to prevent.

        RECORDED ONLY WHEN THE STATE ACTUALLY MOVED. The taps are idempotent
        (af7bcb9), so a double-click is a no-op — minting a second artifact for
        it would fabricate a decision that never happened and make the audit
        trail lie in the direction of MORE human oversight than there was.
        """
        artifact = core_store.create_artifact(
            conn,
            content=(f"APPROVAL DECISION: {verdict} (was '{before}') via the "
                     f"approval centre on {_utc_stamp()}. Task {tid}."),
            state="distilled", trust="confirmed",
            label=f"decision: {verdict}",
            tags=[t for t in core_store.tags_of(conn, tid)
                  if t.startswith("project:")])
        # task --justified_by--> artifact: the decision is the PROVENANCE of the
        # task's new state, which is exactly what justified_by means here.
        core_store.add_edge(conn, tid, artifact.id, "justified_by")

    def _decided_transition(tid: str, fn, verdict: str) -> dict:
        if not _core_db_path().exists():
            raise HTTPException(status_code=404, detail="core.db not found")
        conn = _core_conn()
        try:
            t = _get_task_or_404(conn, tid)
            before = t.get("approval") or "none"
            fn(conn, tid)
            after = core_store.get(conn, tid)
            if (after.get("approval") or "none") != before:
                _record_decision(conn, tid, before, verdict)
                conn.commit()
            card = _task_card(conn, core_store.get(conn, tid))
        finally:
            conn.close()
        return {"core_db": True, "task": card}

    @app.post("/api/core/task/{tid}/approve", dependencies=auth)
    def api_core_approve(tid: str) -> dict:
        return _decided_transition(tid, core_lifecycle.approve, "approved")

    @app.post("/api/core/task/{tid}/reject", dependencies=auth)
    def api_core_reject(tid: str) -> dict:
        return _decided_transition(tid, core_lifecycle.reject, "rejected")

    @app.post("/api/core/task/{tid}/clear-approval", dependencies=auth)
    def api_core_clear_approval(tid: str) -> dict:
        """Clear a STALE approval flag on an already-DONE task.

        Deliberately NOT a general 'set approval to none' button. It refuses any
        task that is not done, so it can only ever retire a flag whose decision
        is moot — it can never be used to wipe a live 'needed' and make a real
        decision disappear from the lane, nor to overturn a recorded
        approved/rejected. The narrow door is the point: a button that could do
        both would eventually do the wrong one.
        """
        def _clear(conn: Any, task_id: str) -> None:
            t = core_store.get(conn, task_id)
            if t.get("status") != "done":
                raise HTTPException(
                    status_code=409,
                    detail="only a DONE task's stale approval flag can be cleared")
            if (t.get("approval") or "none") != "needed":
                return  # idempotent: already cleared
            core_store.update_payload(conn, task_id, approval="none")
        return _decided_transition(tid, _clear, "stale flag cleared")

    @app.post("/api/core/task/{tid}/move/{status}", dependencies=auth)
    def api_core_move(tid: str, status: str) -> dict:
        if status not in core_lifecycle.STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"status must be one of {core_lifecycle.STATUSES}")
        return _lifecycle_transition(tid, lambda c, i: core_lifecycle.move(c, i, status))

    @app.post("/api/core/task/{tid}/archive/{reason}", dependencies=auth)
    def api_core_archive(tid: str, reason: str) -> dict:
        # File a task away off the board. reason: not_relevant (won't do) | done
        # (already done, then filed away).
        if reason not in core_lifecycle.ARCHIVE_REASONS:
            raise HTTPException(
                status_code=422,
                detail=f"reason must be one of {core_lifecycle.ARCHIVE_REASONS}")
        return _lifecycle_transition(
            tid, lambda c, i: core_lifecycle.archive(c, i, reason))

    @app.post("/api/core/task/{tid}/unarchive", dependencies=auth)
    def api_core_unarchive(tid: str) -> dict:
        return _lifecycle_transition(tid, core_lifecycle.unarchive)

    # --- Team / roster (the MANUAL Worker role model) ------------------------
    # Workers are a CURATED roster onboarded BY HAND (owner/admin/member roles),
    # NOT auto-derived from contacts — the foundation for multi-user + role-based
    # approval routing. Degrades to an empty list when core.db doesn't exist yet.

    def _worker_dict(conn: Any, w: Any) -> dict:
        return {
            "id": w.id, "name": w.label,
            "kind": w.get("kind") or "human",
            "role": w.get("role") or "member",
            "active": int(w.get("active") or 0) == 1,
            "capabilities": w.get("capabilities"),
            "channel": w.get("channel"),
        }

    def _get_worker_or_404(conn: Any, wid: str) -> Any:
        w = core_store.get(conn, wid)
        if w is None or w.type != "worker":
            raise HTTPException(status_code=404, detail="unknown worker")
        return w

    @app.get("/api/core/workers", dependencies=auth)
    def api_core_workers() -> dict:
        if not _core_db_path().exists():
            return {"core_db": False, "workers": []}
        conn = _core_conn()
        try:
            workers = [_worker_dict(conn, w) for w in core_team.list_workers(conn)]
        finally:
            conn.close()
        return {"core_db": True, "workers": workers}

    @app.post("/api/core/workers", dependencies=auth)
    def api_core_add_worker(body: WorkerBody) -> dict:
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        conn = _core_conn()  # connect() creates core.db if it's missing
        try:
            try:
                w = core_team.onboard_worker(
                    conn, name, kind=body.kind, role=body.role,
                    capabilities=body.capabilities, channel=body.channel)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            worker = _worker_dict(conn, w)
        finally:
            conn.close()
        return {"core_db": True, "worker": worker}

    @app.post("/api/core/workers/{wid}/role/{role}", dependencies=auth)
    def api_core_worker_role(wid: str, role: str) -> dict:
        if not _core_db_path().exists():
            raise HTTPException(status_code=404, detail="core.db not found")
        conn = _core_conn()
        try:
            _get_worker_or_404(conn, wid)
            try:
                core_team.set_role(conn, wid, role)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            worker = _worker_dict(conn, core_store.get(conn, wid))
        finally:
            conn.close()
        return {"core_db": True, "worker": worker}

    @app.post("/api/core/workers/{wid}/active/{active}", dependencies=auth)
    def api_core_worker_active(wid: str, active: int) -> dict:
        if active not in (0, 1):
            raise HTTPException(status_code=422, detail="active must be 0|1")
        if not _core_db_path().exists():
            raise HTTPException(status_code=404, detail="core.db not found")
        conn = _core_conn()
        try:
            _get_worker_or_404(conn, wid)
            core_team.set_active(conn, wid, bool(active))
            worker = _worker_dict(conn, core_store.get(conn, wid))
        finally:
            conn.close()
        return {"core_db": True, "worker": worker}

    @app.delete("/api/core/workers/{wid}", dependencies=auth)
    def api_core_remove_worker(wid: str) -> dict:
        if not _core_db_path().exists():
            raise HTTPException(status_code=404, detail="core.db not found")
        conn = _core_conn()
        try:
            _get_worker_or_404(conn, wid)
            core_team.remove_worker(conn, wid)
        finally:
            conn.close()
        return {"core_db": True, "removed": wid}

    # --- static vendored assets ---------------------------------------------
    # CSP-safe local vendoring for the web Terminal (QoL R4 / T2): xterm.js + its
    # CSS + the fit addon are served SAME-ORIGIN from tracking_ui/static/vendor —
    # never a CDN (the dashboard forbids external hosts). This mount is a narrow
    # prefix (only /static/vendor/*, not the whole static dir) and sits behind the
    # same auth wrapper as everything else: the SPA loads with the dash_sess cookie,
    # so the browser sends it on the asset requests too. check_dir=False so a
    # checkout that has not vendored the asset 404s here instead of crashing at boot.
    _VENDOR = _STATIC / "vendor"
    app.mount("/static/vendor", StaticFiles(directory=str(_VENDOR), check_dir=False),
              name="vendor")

    # --- PWA: manifest + service worker + icons (QoL R6 / M2) ----------------
    # Installable-dashboard assets, served SAME-ORIGIN + CSP-safe (no CDN), from
    # tracking_ui/static/pwa. They live on the INNER app (not gate.py) so the
    # clean public release ships an installable PWA too. The manifest + SW are
    # explicit routes (right content-types; the SW is served from "/" so its
    # scope is the whole origin, with Service-Worker-Allowed as belt-and-braces);
    # icons ride a narrow static mount. The SW itself NEVER caches /api/* or /ws/*
    # (see sw.js), so live loop data + the terminal WebSocket stay live.
    _PWA = _STATIC / "pwa"
    app.mount("/static/pwa", StaticFiles(directory=str(_PWA), check_dir=False),
              name="pwa")

    @app.get("/manifest.webmanifest")
    def manifest() -> Any:
        f = _PWA / "manifest.webmanifest"
        if f.exists():
            return FileResponse(str(f), media_type="application/manifest+json")
        return JSONResponse({"error": "manifest missing"}, status_code=404)

    @app.get("/sw.js")
    def service_worker() -> Any:
        f = _PWA / "sw.js"
        if f.exists():
            return FileResponse(
                str(f), media_type="application/javascript",
                headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
            )
        return JSONResponse({"error": "sw missing"}, status_code=404)

    # --- SPA -----------------------------------------------------------------

    @app.get("/")
    def index() -> Any:
        if _INDEX.exists():
            return FileResponse(str(_INDEX))
        return JSONResponse({"error": "index.html missing"}, status_code=404)

    return app


# ---------------------------------------------------------------------------
# Token bootstrap + production entrypoint.
# ---------------------------------------------------------------------------


def ensure_token(secrets_path: Path) -> str:
    """Return the [tracking_ui] token, generating + persisting one if absent.

    Reads ``secrets.toml``; if ``[tracking_ui].token`` is missing a fresh
    URL-safe token is generated, appended to the file (chmod 600) and printed.
    """
    import tomllib

    text = ""
    if secrets_path.exists():
        text = secrets_path.read_text()
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            data = {}
        existing = (data.get("tracking_ui") or {}).get("token")
        if existing:
            return str(existing)

    token = secrets.token_urlsafe(32)
    block = f'\n[tracking_ui]\ntoken = "{token}"\n'
    # Append; assumes no pre-existing [tracking_ui] table (none if we got here).
    secrets_path.write_text((text.rstrip("\n") + "\n" if text else "") + block)
    try:
        os.chmod(secrets_path, 0o600)
    except OSError:
        pass
    print(f"[tracking_ui] generated bearer token and wrote it to {secrets_path}", flush=True)
    print(f"[tracking_ui] token = {token}", flush=True)
    return token


def _load_cfg(config_dir: Path):
    from bot_squad_worker.config import Config

    return Config.load(config_dir)


def main() -> None:
    import uvicorn

    # Default to the install root (the tree this file ships in) so a fresh clone
    # runs wherever it lands, rather than a fixed home dir; BOT_SQUAD_HOME still
    # overrides for a split layout.
    # R17: config via the ONE resolver; BOT_SQUAD_CONFIG / BOT_SQUAD_HOME stay
    # as deprecated aliases (explicit config wins, then a legacy home).
    from mcp_loops import paths as _paths
    legacy = os.environ.get("BOT_SQUAD_CONFIG") or (
        str(Path(os.environ["BOT_SQUAD_HOME"]) / "config")
        if os.environ.get("BOT_SQUAD_HOME") else None)
    config_dir = _paths.config_dir(legacy)
    host = os.environ.get("TRACKING_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("TRACKING_UI_PORT", "8766"))
    # Optional TLS: set both to serve HTTPS (public exposure decision = public+HTTPS).
    ssl_key = os.environ.get("TRACKING_UI_SSL_KEY") or None
    ssl_cert = os.environ.get("TRACKING_UI_SSL_CERT") or None

    token = ensure_token(config_dir / "secrets.toml")
    cfg = _load_cfg(config_dir)
    app = create_app(cfg, token)

    scheme = "https" if (ssl_key and ssl_cert) else "http"
    print(f"[tracking_ui] serving on {scheme}://{host}:{port}", flush=True)
    print(f"[tracking_ui] bearer token lives in {config_dir / 'secrets.toml'} [tracking_ui]", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info",
                ssl_keyfile=ssl_key, ssl_certfile=ssl_cert)


if __name__ == "__main__":
    main()
