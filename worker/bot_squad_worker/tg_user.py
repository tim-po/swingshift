"""Personal-account Telegram ingest worker.

Long-lived MTProto (Telethon) client. Subscribes to all incoming messages on
the logged-in user's account and writes them as rows in a SQLite db at
``data/_tg/messages.db``. On startup, catches up history per dialog using a
per-chat cursor so messages received while the worker was down are not lost.

Run with:
    python -m bot_squad_worker.tg_user --config /path/to/config

Requires a prior one-time login via ``tg_user_cli login`` (Telethon session
file at ``data/_tg/telethon.session``).

This module is intentionally separate from ``tg_listener.py`` / ``tg.py``
which speak the Bot API and serve the swarm's own outbound notifications.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
import time
import tomllib
from pathlib import Path
from typing import Optional

from bot_squad_worker import tg_pipelines, tg_tracking, tracking_store
from bot_squad_worker.config import Config


log = logging.getLogger("tg_user")

# Forum-topic id of the "Features" topic on the configured Telegram bot. the owner's messages in
# this topic are OWNED by the business-analyst (BA) service (its own getUpdates
# loop) — the bridge must NOT also route them to coord_inbox, or coord-core would
# double-respond. See _forum_thread_id + the general-bot branch of _handle_bot_inbox.
FEATURES_THREAD_ID = 625308


def _forum_thread_id(msg) -> Optional[int]:
    """The forum-topic (message-thread) id of a Telethon message, or None.

    For forum topics (MTProto), a message carries a MessageReplyHeader with
    ``forum_topic=True``; the topic id is ``reply_to_top_id`` when present (a
    reply nested inside the topic) else ``reply_to_msg_id`` (the topic root).
    We gate on ``forum_topic`` so a PLAIN reply in the non-forum main chat (which
    sets reply_to_msg_id but NOT forum_topic) is never mistaken for a topic.
    Fully fail-soft: any missing attr / error -> None."""
    try:
        rt = getattr(msg, "reply_to", None)
        if rt is None or not getattr(rt, "forum_topic", False):
            return None
        top = getattr(rt, "reply_to_top_id", None)
        if top:
            return int(top)
        rid = getattr(rt, "reply_to_msg_id", None)
        return int(rid) if rid else None
    except Exception:  # noqa: BLE001 — the bridge must keep running
        return None


# Send-as-Tim: how often the bridge drains the outgoing queue. The bridge owns
# the only live Telethon session, so all sends route through here.
_OUTGOING_POLL_SEC = 5.0

# Auto-draft: how often the bridge drains the draft queue and sets per-chat
# drafts. Same single-session constraint as the outgoing sender.
_DRAFT_POLL_SEC = 5.0


# Real-time tracking: when a message lands in a hot-list chat we wait this long
# for the burst to settle, then extract the whole new window ONCE. Re-armed on
# every new message in the chat, so a rapid back-and-forth collapses into a
# single LLM pass instead of one per message (COST GUARD + politeness).
_REALTIME_DEBOUNCE_SEC = 15.0


class RealtimeDebouncer:
    """Coalesce per-chat real-time extraction OFF the Telethon event loop.

    ``schedule(proj, chat_id, title, username)`` (re)arms a short debounce timer
    for ``(slug, chat_id)``; when it fires, the post-debounce work runs ONCE in a
    thread executor — never on the event loop — because the extraction's
    ``claude`` subprocess can block for up to ~120s and ingest must keep flowing.

    The scheduling (this class) is kept deliberately thin and separate from the
    actual work (``tg_pipelines.process_realtime_chat``, a sync, unit-testable
    function), so the burst-coalescing logic can be tested with a fake loop and
    the extraction logic tested directly.
    """

    def __init__(
        self,
        cfg: Config,
        loop=None,
        debounce_sec: float = _REALTIME_DEBOUNCE_SEC,
        runner=None,
    ) -> None:
        self._cfg = cfg
        self._loop = loop  # None → resolve the running loop lazily at use time.
        self._debounce = float(debounce_sec)
        self._runner = runner or tg_pipelines.process_realtime_chat
        # key = (slug, chat_id)
        self._timers: dict = {}
        self._pending: dict = {}

    def _get_loop(self):
        return self._loop or asyncio.get_event_loop()

    def schedule(
        self,
        proj,
        chat_id: int,
        chat_title: Optional[str] = None,
        username: Optional[str] = None,
    ) -> None:
        """(Re)arm the debounce timer for this chat; coalesces rapid messages."""
        key = (getattr(proj, "slug", "") or "", int(chat_id))
        self._pending[key] = (proj, chat_id, chat_title, username)
        old = self._timers.get(key)
        if old is not None:
            old.cancel()
        self._timers[key] = self._get_loop().call_later(
            self._debounce, self._fire, key
        )

    def _fire(self, key) -> None:
        """Debounce window elapsed → hand the chat's work to a thread executor."""
        self._timers.pop(key, None)
        pending = self._pending.pop(key, None)
        if pending is None:
            return
        proj, chat_id, chat_title, username = pending
        # run_in_executor returns a Future scheduled on a worker thread; we do
        # NOT await it (fire-and-forget) so the event loop is never blocked.
        self._get_loop().run_in_executor(
            None, self._run, proj, chat_id, chat_title, username
        )

    def _run(self, proj, chat_id, chat_title, username) -> None:
        """Executor body. Guarded: one failure must not kill the bridge."""
        try:
            self._runner(self._cfg, proj, chat_id, chat_title, username)
        except Exception:  # noqa: BLE001
            log.exception("realtime extraction failed for chat %s", chat_id)


def _schedule_realtime(cfg: Config, debouncer: "RealtimeDebouncer", event, chat_entity) -> None:
    """If this incoming message is in a hot-list chat, arm its debounce timer.

    Iterates projects and schedules for each whose EFFECTIVE config (DB-first;
    see ``tg_tracking.get_effective_tracking``) is enabled and lists this chat
    as real-time (in practice just ``swarmdev``). Reading the effective config
    here is what lets a UI scope change arm/disarm a chat with no restart.
    Cheap and non-LLM: a read-only config lookup + scope checks + a timer
    (re)arm. Errors are caught by the caller.
    """
    chat_id = int(event.chat_id) if getattr(event, "chat_id", None) is not None else None
    if chat_id is None:
        return
    title = _chat_title(chat_entity)
    username = getattr(chat_entity, "username", None)
    for proj in getattr(cfg, "projects", {}).values():
        eff = tg_tracking.effective_proj(cfg, proj)
        if not eff.tg_track_enabled:
            continue
        if tg_tracking.is_realtime(eff, chat_id, title, username):
            # Pass the original proj: the debounced runner re-resolves the
            # effective config itself, and the proj identity is what callers
            # (and tests) key the schedule on.
            debouncer.schedule(proj, chat_id, title, username)


# ---------------------------------------------------------------------------
# @claude auto-reply trigger.
#
# In ANY chat (independent of tracking scope), when Tim or the other person
# types "@claude", the swarm replies AUTOMATICALLY (no approval), sent AS Tim
# via the same outgoing queue the bridge already drains. Two modes:
#   * "@claude <question>" (or bare "@claude")  → ONE auto-reply.
#   * "@claude help [for N min(utes)]"          → open a per-chat AUTO-HELP
#     WINDOW: for N minutes, NEW incoming messages get auto-replied (the LLM
#     decides if a reply is warranted) without further @claude.
# Every auto-reply carries an AI disclaimer + a guaranteed leading "🤖 " marker.
# ---------------------------------------------------------------------------

# Case-insensitive @claude with a trailing word boundary (so "@claudette" never
# matches). Used for detection AND to split off the rest of the command.
_CLAUDE_TRIGGER_RE = re.compile(r"(?i)@claude\b")

# Default auto-help window when "@claude help" has no explicit duration.
_DEFAULT_HELP_MINUTES = 5

# LOOP/ABUSE GUARDS (process-local; the window itself is durable in tracking.db).
# Min seconds between auto-replies in one chat, and a hard cap on auto-replies
# per auto-help window so a chatty window can't fan out unbounded.
_AUTOREPLY_MIN_INTERVAL_SEC = 8.0
_AUTOREPLY_WINDOW_CAP = 20

# Per-chat guard state: {chat_id: {"last_ts": float, "window_until": int,
# "window_count": int}}. Reset helper exists for tests.
_autoreply_guard: dict = {}


def _reset_autoreply_guards() -> None:
    """Clear the in-memory per-chat auto-reply guard state (test seam)."""
    _autoreply_guard.clear()


def _rate_limited(chat_id: int, now: float,
                  min_interval: float = _AUTOREPLY_MIN_INTERVAL_SEC) -> bool:
    """True if this chat auto-replied less than ``min_interval`` seconds ago."""
    g = _autoreply_guard.get(int(chat_id))
    if not g:
        return False
    return (now - float(g.get("last_ts", 0.0))) < min_interval


def _note_autoreply(chat_id: int, now: float) -> None:
    """Record that this chat just auto-replied (for the rate limiter)."""
    _autoreply_guard.setdefault(int(chat_id), {})["last_ts"] = float(now)


def _window_reply_allowed(chat_id: int, until_ts: int) -> bool:
    """True if this window still has budget. (Re)sets the counter on a new window."""
    g = _autoreply_guard.setdefault(int(chat_id), {})
    if g.get("window_until") != int(until_ts):
        g["window_until"] = int(until_ts)
        g["window_count"] = 0
    return int(g.get("window_count", 0)) < _AUTOREPLY_WINDOW_CAP


def _note_window_reply(chat_id: int, until_ts: int) -> None:
    """Count one auto-reply against this window's cap."""
    g = _autoreply_guard.setdefault(int(chat_id), {})
    if g.get("window_until") != int(until_ts):
        g["window_until"] = int(until_ts)
        g["window_count"] = 0
    g["window_count"] = int(g.get("window_count", 0)) + 1


def _parse_claude_trigger(text: str) -> dict:
    """Parse an @claude command into a mode dict.

    * "help [for] [N] min(ute)(s)" / "help N min" / bare "help" after @claude
      → ``{"mode": "window", "minutes": N or 5}``.
    * anything else → ``{"mode": "oneshot", "question": <text after @claude>}``.

    If there is no @claude at all, treats the whole text as a one-shot question
    (callers gate on detection separately, so this is just a safe default).
    """
    m = _CLAUDE_TRIGGER_RE.search(text or "")
    after = (text[m.end():] if m else (text or "")).strip()
    hm = re.match(r"(?i)^help\b(.*)$", after)
    if hm:
        dm = re.search(r"(?i)(\d+)\s*(?:m\b|mins?\b|minutes?\b)", hm.group(1))
        minutes = int(dm.group(1)) if dm else _DEFAULT_HELP_MINUTES
        return {"mode": "window", "minutes": minutes}
    return {"mode": "oneshot", "question": after}


def _auto_help_confirmation(minutes: int) -> str:
    """Bilingual confirmation announcing the auto-help window (with 🤖 marker)."""
    return (
        f"\U0001F916 AI auto-help enabled for {minutes} min — replies in this "
        f"chat are now automated AI (Claude) on Tim's behalf. / "
        f"Автоответы ИИ включены на {minutes} мин — отвечает Claude (ИИ) от "
        f"имени Тима."
    )


def _default_slug(cfg: Config) -> str:
    """Bookkeeping slug for enqueued auto-replies (feature is scope-independent).

    The outgoing sender drains by status regardless of slug, so any stable value
    works; prefer the first configured project's slug, else a constant.
    """
    for slug in (getattr(cfg, "projects", None) or {}):
        return slug
    return "auto"


async def _safe_get_chat(event):
    try:
        return await event.get_chat()
    except Exception:  # noqa: BLE001 — Telethon raises a wide variety
        return None


def _trigger_sender_name(event) -> str:
    """Best-effort label of who invoked @claude (for ``enabled_by``)."""
    if getattr(getattr(event, "message", None), "out", False):
        return "Tim"
    sid = getattr(event, "sender_id", None)
    return str(sid) if sid is not None else "?"


def _auto_reply_and_enqueue(
    cfg: Config,
    chat_id: int,
    chat_title: Optional[str],
    trigger_text: str,
    reply_to_msg_id: Optional[int],
) -> None:
    """Executor body: generate an auto-reply and enqueue it. Fully guarded.

    Runs OFF the event loop (the ``claude`` subprocess can block ~120s). Opens
    its own read-only messages.db conn for context (tolerates a missing file —
    e.g. in tests that monkeypatch ``auto_reply``) and its own tracking.db conn
    for the enqueue. One failure must never kill the bridge.
    """
    try:
        mconn = None
        msgs_path = Path(str(cfg.tg_user_db_path))
        if msgs_path.exists():
            mconn = sqlite3.connect(f"file:{msgs_path}?mode=ro", uri=True)
            mconn.row_factory = sqlite3.Row
        try:
            text = tg_pipelines.auto_reply(
                cfg, int(chat_id), chat_title, trigger_text, mconn
            )
        finally:
            if mconn is not None:
                mconn.close()
        if not text:
            return
        tconn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
        try:
            tracking_store.enqueue_outgoing(
                tconn, _default_slug(cfg), int(chat_id), text,
                reply_to_msg_id=reply_to_msg_id,
            )
        finally:
            tconn.close()
    except Exception:  # noqa: BLE001 — the bridge must keep running
        log.exception("auto-reply generation failed for chat %s", chat_id)


async def _dispatch_auto_reply(
    cfg: Config,
    chat_id: int,
    chat_title: Optional[str],
    trigger_text: str,
    reply_to_msg_id: Optional[int],
) -> bool:
    """Rate-limit + offload one auto-reply to a thread executor.

    Returns True if an auto-reply was dispatched (so callers can count it against
    a window cap), False if it was rate-limited. The blocking LLM work runs in an
    executor and IS awaited — this keeps the event loop free (ingest keeps
    flowing) while making the path deterministic for tests.
    """
    now = time.time()
    if _rate_limited(chat_id, now):
        log.info("auto-reply rate-limited for chat %s", chat_id)
        return False
    _note_autoreply(chat_id, now)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, _auto_reply_and_enqueue,
        cfg, int(chat_id), chat_title, trigger_text, reply_to_msg_id,
    )
    return True


async def _handle_claude_trigger(cfg: Config, client, event) -> bool:
    """If this message contains @claude, act on it; return True iff handled.

    Called from BOTH the incoming and outgoing handlers. Detection is the only
    thing the outgoing path does (it never records to messages.db / schedules
    realtime). LOOP GUARD: never reacts to our own AI replies (text starting with
    the "🤖 " marker).
    """
    msg = getattr(event, "message", None)
    text = getattr(msg, "message", "") or ""
    if not text or text.startswith("\U0001F916"):
        return False
    if not _CLAUDE_TRIGGER_RE.search(text):
        return False
    chat_id = int(event.chat_id) if getattr(event, "chat_id", None) is not None else None
    if chat_id is None:
        return False
    # PROMPT-INJECTION GATE: @claude only fires in chats Tim has allowlisted
    # (DB-first, default empty ⇒ off everywhere). Do NOTHING when not enabled —
    # no reply, no window. Returns True so callers don't fall through to the
    # auto-help follow-up for this (gated) trigger message.
    if not tg_tracking.is_claude_enabled(cfg, chat_id):
        return True

    title = _chat_title(await _safe_get_chat(event))
    parsed = _parse_claude_trigger(text)

    if parsed["mode"] == "window":
        minutes = int(parsed["minutes"])
        until = int(time.time()) + minutes * 60
        tconn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
        try:
            tracking_store.set_auto_help(
                tconn, chat_id, until, _trigger_sender_name(event)
            )
            tracking_store.enqueue_outgoing(
                tconn, _default_slug(cfg), chat_id,
                _auto_help_confirmation(minutes),
                reply_to_msg_id=getattr(msg, "id", None),
            )
        finally:
            tconn.close()
        # (Re)arm the in-memory window counter for the fresh window.
        _window_reply_allowed(chat_id, until)
        log.info("auto-help window opened for chat %s (%d min)", chat_id, minutes)
        return True

    # one-shot: reply to THIS message + recent context.
    await _dispatch_auto_reply(
        cfg, chat_id, title, parsed.get("question") or text,
        getattr(msg, "id", None),
    )
    return True


async def _maybe_auto_help_followup(cfg: Config, client, event) -> None:
    """During an active auto-help window, auto-reply to a NEW incoming message.

    Incoming-only (the owner's own messages don't get auto-replied during a window).
    Skips our own AI replies (🤖 marker) and any message that itself carries an
    @claude trigger (that path is handled by ``_handle_claude_trigger``). Honors
    the per-window cap and the per-chat rate limit.
    """
    msg = getattr(event, "message", None)
    text = getattr(msg, "message", "") or ""
    if not text or text.startswith("\U0001F916"):
        return
    if _CLAUDE_TRIGGER_RE.search(text):
        return
    chat_id = int(event.chat_id) if getattr(event, "chat_id", None) is not None else None
    if chat_id is None:
        return
    # PROMPT-INJECTION GATE: auto-help windows only apply in allowlisted chats.
    if not tg_tracking.is_claude_enabled(cfg, chat_id):
        return

    tconn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    try:
        until = tracking_store.get_auto_help_until(tconn, chat_id)
    finally:
        tconn.close()
    if until <= int(time.time()):
        return  # no active window
    if not _window_reply_allowed(chat_id, until):
        log.info("auto-help window cap reached for chat %s", chat_id)
        return

    title = _chat_title(await _safe_get_chat(event))
    dispatched = await _dispatch_auto_reply(
        cfg, chat_id, title, text, getattr(msg, "id", None)
    )
    if dispatched:
        _note_window_reply(chat_id, until)


# ---------------------------------------------------------------------------
# /swarm-answer — draft a reply into Tim's input field (any chat, manual send).
#
# Telegram inline bots can't see chat context, so this uses Tim's OWN user
# session: when he sends "/swarm-answer" (or "/sa") in any chat, the bridge
# DELETES that trigger message, generates a context-aware reply in HIS voice
# (no AI disclaimer, since he sends it himself) and SETS THE CHAT DRAFT so the
# text lands in his input box for review/edit. NOTHING is auto-sent.
# ---------------------------------------------------------------------------

# Case-insensitive trigger at the start. Forgiving about the exact name so the
# things one would naturally type all work: /swarm-answer, /swarm-reply,
# /swarmanswer, /swarm answer, /swarm_reply, and the short /sa. Word boundary so
# "/sausage" never matches.
_SWARM_ANSWER_RE = re.compile(r"(?i)^/(sa|swarm[-_ ]?(answer|reply))\b")


def _draft_reply_blocking(cfg: Config, chat_id: int, chat_title: Optional[str]) -> Optional[str]:
    """Executor body: open messages.db read-only and generate a draft reply.

    Runs OFF the event loop (``draft_reply``'s ``claude`` subprocess can block
    ~120s). Tolerates a missing messages.db (context is best-effort). One failure
    must never kill the bridge.
    """
    mconn = None
    try:
        msgs_path = Path(str(cfg.tg_user_db_path))
        if msgs_path.exists():
            mconn = sqlite3.connect(f"file:{msgs_path}?mode=ro", uri=True)
            mconn.row_factory = sqlite3.Row
        return tg_pipelines.draft_reply(cfg, int(chat_id), chat_title, mconn)
    except Exception:  # noqa: BLE001 — the bridge must keep running
        log.exception("swarm-answer: draft generation failed for chat %s", chat_id)
        return None
    finally:
        if mconn is not None:
            mconn.close()


async def _handle_swarm_answer(cfg: Config, client, event) -> bool:
    """If Tim's outgoing message is /swarm-answer (or /sa), draft a reply; True iff handled.

    Deletes the trigger message, generates a context-aware reply in Tim's voice
    (no 🤖 marker), and SaveDrafts it into his input box for review/manual send.
    Works in ANY chat (no allowlist — Tim invokes, reviews and sends himself).
    """
    msg = getattr(event, "message", None)
    text = getattr(msg, "message", "") or ""
    # Diagnostic: log any outgoing slash-command so a mistyped trigger is visible.
    if text.startswith("/"):
        log.info("outgoing command seen: %r", text[:40])
    if not _SWARM_ANSWER_RE.search(text):
        return False
    chat_id = int(event.chat_id) if getattr(event, "chat_id", None) is not None else None
    if chat_id is None:
        return False
    log.info("swarm-answer: triggered in chat %s — drafting", chat_id)

    chat = await _safe_get_chat(event)
    # DELETE the trigger so the bare "/swarm-answer" command never lingers.
    try:
        await client.delete_messages(chat, [getattr(msg, "id", None)])
    except Exception:  # noqa: BLE001 — Telethon raises a wide variety
        log.exception("swarm-answer: failed to delete trigger in chat %s", chat_id)

    title = _chat_title(chat)
    loop = asyncio.get_running_loop()
    reply = await loop.run_in_executor(
        None, _draft_reply_blocking, cfg, int(chat_id), title
    )
    if not reply:
        return True  # nothing to draft → no-op (trigger already removed)

    # SET THE CHAT DRAFT so it lands in Tim's input box for review/edit.
    try:
        from telethon.tl import functions
        await client(functions.messages.SaveDraftRequest(peer=chat, message=reply))
        log.info("swarm-answer: draft set in chat %s (len=%d)", chat_id, len(reply))
    except Exception:  # noqa: BLE001 — Telethon raises a wide variety
        log.exception("swarm-answer: SaveDraft failed for chat %s", chat_id)
    return True


def _coord_inbox_path(cfg) -> Path:
    """Where the owner's messages TO THE SWARM BOT land for the coordinator to poll —
    a jsonl sibling of the message store."""
    return Path(cfg.tg_user_db_path).parent / "coord_inbox.jsonl"


def _extract_bot_document_text(data: bytes, name: str, mime: str) -> str:
    """Extract TEXT (text-based PDFs + .txt) from a document sent to the general
    swarm bot, reusing coord_life.uxhelpers.extract_document_text. ROOT (the
    bot-swarm repo root) is added to sys.path so the shared extractor is
    importable from the worker package. Fail-soft: "" on any error."""
    try:
        import sys
        root = str(Path(__file__).resolve().parents[2])  # …/bot-swarm
        if root not in sys.path:
            sys.path.insert(0, root)
        from coord_life.uxhelpers import extract_document_text
        return extract_document_text(data, name, mime)
    except Exception:  # noqa: BLE001 — the bridge must keep running
        log.exception("bot document extraction failed")
        return ""


async def _read_bot_document(client, msg) -> tuple[str, str]:
    """Download a document message's bytes (Telethon ``download_media``) and
    extract its text. Returns ``(file_name, extracted_text)``; extracted_text is
    "" on failure (caller writes a placeholder so nothing is silently dropped)."""
    name, mime = "file", ""
    f = getattr(msg, "file", None)
    if f is not None:
        name = getattr(f, "name", None) or "file"
        mime = getattr(f, "mime_type", None) or ""
    try:
        data = await client.download_media(msg, file=bytes)
    except Exception:  # noqa: BLE001
        log.exception("bot document download failed for msg %s",
                      getattr(msg, "id", "?"))
        return name, ""
    if not data:
        return name, ""
    return name, _extract_bot_document_text(bytes(data), name, mime)


def _premium_voice_path(cfg) -> Path:
    """SHARED file where Tim's voice DMs to NON-general swarm bots (life, future)
    land after Telegram's BUILT-IN (Premium) transcription — a jsonl sibling of
    the message store, polled by those bots' coordinators (e.g. coord-life)."""
    return Path(cfg.tg_user_db_path).parent / "premium_voice.jsonl"


def _swarm_bots(cfg) -> list[dict]:
    """The set of the swarm's OWN Telegram bots whose voice DMs from Tim should
    use Telegram's BUILT-IN (Premium) transcription instead of local Whisper.

    Each entry: ``{"label": str, "bot_id": int, "general": bool}``. The GENERAL
    (swarmdev) bot keeps its first-class coord_inbox.jsonl path; every OTHER bot
    routes voice into the shared premium_voice.jsonl. Extend by appending here
    (or wiring a new secrets entry) — the matcher below is list-driven."""
    bots: list[dict] = []
    # GENERAL / swarmdev bot — MUST stay first-class (coord_inbox path unchanged).
    try:
        token = cfg.bot_token_for("swarmdev") or ""
        bid = int(token.split(":")[0]) if ":" in token else 0
        if bid:
            bots.append({"label": "swarmdev", "bot_id": bid, "general": True})
    except Exception:  # noqa: BLE001
        pass
    # LIFE bot — token in secrets.toml [lifetrack].bot_token (bot_id before ':').
    try:
        sec = tomllib.loads((Path(cfg.config_dir) / "secrets.toml").read_text())
        lt = (sec.get("lifetrack", {}) or {}).get("bot_token", "") or ""
        bid = int(lt.split(":")[0]) if ":" in lt else 0
        if bid:
            bots.append({"label": "life", "bot_id": bid, "general": False})
    except Exception:  # noqa: BLE001
        pass
    return bots


def _match_swarm_bot(cfg, chat_id: int) -> Optional[dict]:
    """Return the swarm-bot entry whose bot_id == chat_id, or None (no-op)."""
    if not chat_id:
        return None
    for b in _swarm_bots(cfg):
        if b.get("bot_id") == chat_id:
            return b
    return None


def _bot_id_of(token: str) -> int:
    """The numeric user-id a bot token belongs to (the part before ':'), or 0.

    The id prefix of a token is NOT the secret — the secret is the hash after the
    colon — so this leaks nothing. Returns 0 for a malformed/empty token."""
    tok = (token or "")
    head = tok.split(":", 1)[0] if ":" in tok else ""
    return int(head) if head.isdigit() else 0


def _own_bot_ids(cfg) -> set[int]:
    """User-ids of ALL our own Telegram bots, DERIVED from config tokens.

    A bot DM's ``chat_id`` EQUALS the bot's user-id, so this set is exactly the
    chat_ids that are conversations between Tim and one of our coordinators
    (plancheck / umem / diss / genui / …). It is derived from the global token
    plus EVERY per-project token (``telegram_bots``), never hand-listed — the day
    a new coordinator bot's token lands in config it is covered with no code
    change (DERIVE, NEVER ENUMERATE). Tolerant of a stub cfg missing either
    attribute (returns whatever it can derive)."""
    ids: set[int] = set()
    b = _bot_id_of(getattr(cfg, "tg_bot_token", "") or "")
    if b:
        ids.add(b)
    for tok in (getattr(cfg, "telegram_bots", {}) or {}).values():
        b = _bot_id_of(tok)
        if b:
            ids.add(b)
    return ids


async def _handle_bot_inbox(cfg, client, event) -> bool:
    """the owner's messages to a SWARM BOT are meant for that bot's coordinator (NOT
    swarm-answer / @claude). Transcribe voice with Telegram's BUILT-IN
    transcription (his Premium account) so voice reaches the coordinator with
    good quality — no local Whisper. Returns True (short-circuit) when the
    message was addressed to ANY swarm bot.

    Routing:
      * GENERAL (swarmdev) bot  -> coord_inbox.jsonl  (behavior UNCHANGED).
      * OTHER swarm bot (life…) -> premium_voice.jsonl, VOICE only (text already
        reaches those bots via their own getUpdates loop)."""
    chat_id = int(getattr(event, "chat_id", 0) or 0)
    bot = _match_swarm_bot(cfg, chat_id)
    if bot is None:
        return False
    msg = getattr(event, "message", None)
    if msg is None:
        return True

    if bot.get("general"):
        # === GENERAL / swarmdev bot — coord_inbox.jsonl =======================
        # FEATURES-TOPIC GUARD: the BA service owns the Features topic via its own
        # getUpdates loop. The Telethon bridge is thread-blind, so WITHOUT this it
        # would ALSO write Features messages to coord_inbox and coord-core would
        # double-respond. Detect the topic id and, when it's the Features topic,
        # route the message NOWHERE (skip the write); still short-circuit (return
        # True) so no other handler picks it up.
        thread_id = _forum_thread_id(msg)
        if thread_id == FEATURES_THREAD_ID:
            log.info("bot inbox: msg %s in Features topic (%s) — skipping "
                     "coord_inbox (owned by BA)", getattr(msg, "id", "?"), thread_id)
            return True
        text = (getattr(msg, "message", "") or "").strip()
        is_voice = False
        is_doc = False
        had_audio = bool(getattr(msg, "voice", None) or getattr(msg, "video_note", None)
                         or getattr(msg, "audio", None))
        had_doc = bool(getattr(msg, "document", None))
        if not text and had_audio:
            vt = await _transcribe_if_voice(client, msg)
            if vt:
                text, is_voice = vt, True
            else:  # transcription didn't resolve — don't silently drop the voice
                text, is_voice = "[голосовое — не удалось расшифровать, пришли ещё раз]", True
        elif not text and had_doc:
            # DOCUMENT: extract TEXT (text-based PDFs + .txt) so it reaches the
            # coordinator like any other message. Never silently drop it.
            is_doc = True
            name, extracted = await _read_bot_document(client, msg)
            text = (f"📄 {name}:\n{extracted}" if extracted
                    else "[couldn't read document]")
        log.info("bot inbox from Tim: msg %s voice=%s doc=%s chars=%d",
                 getattr(msg, "id", "?"), is_voice, is_doc, len(text))
        if text:
            rec = {"ts": int(msg.date.timestamp()) if msg.date else 0,
                   "msg_id": int(msg.id), "text": text, "voice": is_voice,
                   "doc": is_doc, "thread_id": thread_id}
            try:
                with open(_coord_inbox_path(cfg), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001
                log.exception("coord inbox write failed")
        return True

    # === OTHER swarm bot — PREMIUM-transcribe VOICE into the shared file ======
    had_audio = bool(getattr(msg, "voice", None) or getattr(msg, "video_note", None)
                     or getattr(msg, "audio", None))
    if not had_audio:
        return True  # matched a swarm bot; text is handled by that bot's own loop
    try:
        vt = await _transcribe_if_voice(client, msg)
        # Keep the failure marker as the transcript so the coordinator can surface
        # a retry (coord-life treats it as a miss and falls back to Whisper).
        transcript = vt or "[голосовое — не удалось расшифровать, пришли ещё раз]"
        rec = {"ts": int(msg.date.timestamp()) if msg.date else 0,
               "bot_id": int(bot["bot_id"]), "msg_id": int(msg.id),
               "transcript": transcript}
        with open(_premium_voice_path(cfg), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log.info("premium voice for %s bot: msg %s chars=%d",
                 bot.get("label"), getattr(msg, "id", "?"), len(transcript))
    except Exception:  # noqa: BLE001
        log.exception("premium voice write failed")
    return True


async def _clear_pending_reply_on_own_message(cfg, event) -> None:
    """When Tim answers a chat HIMSELF, clear any PENDING suggested reply there so
    a stale draft doesn't linger. Works for ALL chats (not just realtime). Skips
    our own 🤖 AI replies, and skips when the outgoing text IS the suggested draft
    (that's a send-as-Tim send — the sender marks it 'sent', don't override).

    ``suggested_replies`` lives in the TRACKING db, not the bridge's messages.db —
    so this opens its OWN tracking conn (the file's idiom; see _outgoing_sender /
    _handle_swarm_answer). Passing it the messages.db conn threw 'no such table:
    suggested_replies' on EVERY outgoing event (58217419), which the per-step
    try/except turned into a traceback flood rather than a crash."""
    msg = getattr(event, "message", None)
    text = (getattr(msg, "message", "") or "").strip()
    if not text or text.startswith("\U0001F916"):
        return
    chat_id = getattr(event, "chat_id", None)
    if chat_id is None:
        return
    tconn = None
    try:
        tconn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
        pending = tconn.execute(
            "SELECT draft_text FROM suggested_replies "
            "WHERE chat_id = ? AND status = 'pending'", (int(chat_id),),
        ).fetchall()
        if not pending:
            return
        if any((r[0] or "").strip() == text for r in pending):
            return   # this IS the suggested draft going out — leave it to 'sent'
        n = tracking_store.clear_pending_reply(tconn, int(chat_id))
        if n:
            log.info("cleared %d pending reply(ies) in chat %s (Tim answered himself)",
                     n, int(chat_id))
    except Exception:  # noqa: BLE001
        log.exception("clear_pending_reply failed for chat %s", chat_id)
    finally:
        if tconn is not None:
            tconn.close()


async def _record_outgoing_realtime(
    cfg: Config, conn: sqlite3.Connection, client,
    debouncer: "RealtimeDebouncer", event,
) -> None:
    """Record Tim's OWN message + (re)arm realtime extraction — REALTIME chats only.

    Keeps messages.db's ``_last_outgoing_msg_id`` current so the next realtime
    pass's ``generate_reply`` sees Tim as the latest sender (no unanswered
    incoming ⇒ a hanging suggested reply is CLEARED) and so his own messages feed
    the todos/meetings extractor ("check my messages"). Skips our own 🤖 AI
    replies (loop guard). NON-realtime chats are untouched (don't bloat the db
    with all outgoing everywhere). Caller already filtered command/trigger msgs.
    """
    msg = getattr(event, "message", None)
    text = getattr(msg, "message", "") or ""
    if text.startswith("\U0001F916"):
        return  # our own AI reply — never record/extract as a normal message
    chat_id = int(event.chat_id) if getattr(event, "chat_id", None) is not None else None
    if chat_id is None:
        return
    try:
        chat_entity = await event.get_chat()
    except Exception:  # noqa: BLE001 — Telethon raises a wide variety
        chat_entity = None
    title = _chat_title(chat_entity)
    username = getattr(chat_entity, "username", None)
    # RECORD the owner's own words to a COORDINATOR BOT DM (plancheck / umem / diss /
    # genui / …) before the realtime loop. Those chats are NOT realtime, so the
    # loop below skips every one of them, and the general/life first-class paths
    # short-circuit earlier in _handle_bot_inbox — so without this, Tim's SIDE of
    # every coordinator conversation is absent from messages.db while the bot's
    # side is fully mirrored (measured 2026-07-23: the plancheck chat held 67 rows
    # and 0 were from Tim). Record-only: no realtime extraction/debounce (a
    # coordinator DM has no meetings/todos to mine), and scoped to OUR OWN bots
    # (derived from config), so it does not "bloat the db with all outgoing
    # everywhere" — the exact concern that made the realtime-only rule. We reach
    # here only AFTER the command handlers (_handle_bot_inbox / swarm-answer /
    # @claude) have short-circuited, so a /command is never recorded as a message.
    if chat_id in _own_bot_ids(cfg):
        await _record_message(conn, client, msg, chat_entity=chat_entity,
                              transcribe=False)
        return
    for proj in getattr(cfg, "projects", {}).values():
        eff = tg_tracking.effective_proj(cfg, proj)
        if not eff.tg_track_enabled:
            continue
        if not tg_tracking.is_realtime(eff, chat_id, title, username):
            continue
        # Record once (idempotent on (chat_id, msg_id)) + arm the debouncer. Pass
        # the original proj (the debounced runner re-resolves the effective cfg).
        await _record_message(conn, client, msg, chat_entity=chat_entity,
                              transcribe=True)
        debouncer.schedule(proj, chat_id, title, username)
        return


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id      INTEGER NOT NULL,
    msg_id       INTEGER NOT NULL,
    ts           INTEGER NOT NULL,        -- unix seconds, UTC
    chat_title   TEXT,
    sender_id    INTEGER,
    sender_name  TEXT,
    text         TEXT,
    raw_json     TEXT,
    project      TEXT,                    -- nullable; filled by classifier later
    status       TEXT NOT NULL DEFAULT 'new',
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages (ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages (chat_id, ts DESC);

CREATE TABLE IF NOT EXISTS chats (
    chat_id      INTEGER PRIMARY KEY,
    title        TEXT,
    kind         TEXT,                    -- 'user' | 'group' | 'channel'
    last_msg_id  INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL DEFAULT 0
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def _chat_kind(entity) -> str:
    cls = type(entity).__name__
    if cls == "User":
        return "user"
    if cls == "Chat":
        return "group"
    if cls == "Channel":
        return "channel" if getattr(entity, "broadcast", False) else "group"
    return "unknown"


def _chat_title(entity) -> str:
    if entity is None:
        return ""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    full = f"{first} {last}".strip()
    if full:
        return full
    username = getattr(entity, "username", None)
    return f"@{username}" if username else str(getattr(entity, "id", ""))


def _sender_name(sender) -> str:
    if sender is None:
        return ""
    return _chat_title(sender)


def _serialize_message(msg) -> dict:
    """Compact dict-of-primitives snapshot of a Telethon Message for raw_json."""
    return {
        "id": msg.id,
        "date": msg.date.isoformat() if msg.date else None,
        "out": bool(getattr(msg, "out", False)),
        "fwd": bool(getattr(msg, "fwd_from", None)),
        "reply_to_msg_id": getattr(msg, "reply_to_msg_id", None),
        "has_media": bool(getattr(msg, "media", None)),
        "media_type": type(msg.media).__name__ if getattr(msg, "media", None) else None,
    }


async def _transcribe_if_voice(client, msg) -> str:
    """Telegram's BUILT-IN transcription for a voice / round-video / audio message
    (needs Premium on the logged-in account — Tim's has it). Returns the transcript
    text, or '' for a non-voice message or ANY failure (fail-soft — never raises
    into ingest). Telegram transcribes asynchronously, so we poll a few times until
    the result is no longer ``pending``."""
    is_voice = bool(getattr(msg, "voice", None) or getattr(msg, "video_note", None)
                    or getattr(msg, "audio", None))
    if not is_voice:
        return ""
    try:
        from telethon.tl import functions
        try:
            peer = await msg.get_input_chat()
        except Exception:  # noqa: BLE001
            peer = await client.get_input_entity(msg.chat_id)
        text, tries = "", 0
        for _ in range(12):   # transcribes async + retry transient errors (~24s)
            res = await client(functions.messages.TranscribeAudioRequest(
                peer=peer, msg_id=int(msg.id)))
            text = (getattr(res, "text", "") or "").strip()
            pending = getattr(res, "pending", False)
            errored = text.lower().startswith("error")   # transient Telegram error
            if text and not pending and not errored:
                log.info("voice transcript OK (msg %s, %d chars)", msg.id, len(text))
                return text
            tries += 1
            await asyncio.sleep(2.0)
        log.warning("voice transcript unresolved (msg %s, tries %d, last=%r)",
                    getattr(msg, "id", "?"), tries, text[:60])
        return "" if (not text or text.lower().startswith("error")) else text
    except Exception:  # noqa: BLE001
        log.exception("voice transcription failed for msg %s",
                      getattr(msg, "id", "?"))
        return ""


async def _record_message(
    conn: sqlite3.Connection,
    client,
    msg,
    chat_entity=None,
    transcribe: bool = False,
) -> None:
    """Insert one message; upsert its chat row. Idempotent on (chat_id, msg_id).

    ``transcribe=True`` (realtime path only) runs Telegram's built-in transcription
    on voice/round-video messages and stores the transcript as the message text, so
    voice flows into the SAME extraction pipeline as text."""
    if chat_entity is None:
        try:
            chat_entity = await msg.get_chat()
        except Exception:  # noqa: BLE001 — Telethon raises a wide variety
            chat_entity = None
    try:
        sender = await msg.get_sender()
    except Exception:  # noqa: BLE001
        sender = None

    chat_id = int(msg.chat_id) if msg.chat_id is not None else 0
    ts = int(msg.date.timestamp()) if msg.date else 0
    title = _chat_title(chat_entity)
    sender_id = int(getattr(sender, "id", 0) or 0)
    sender_nm = _sender_name(sender)
    text = msg.message or ""
    if transcribe and not text:
        vt = await _transcribe_if_voice(client, msg)
        if vt:
            text = "🎤 " + vt   # marker: this text came from a voice transcript
    raw = json.dumps(_serialize_message(msg))

    conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (chat_id, msg_id, ts, chat_title, sender_id, sender_name, text, raw_json, project, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'new')
        """,
        (chat_id, int(msg.id), ts, title, sender_id, sender_nm, text, raw),
    )
    conn.execute(
        """
        INSERT INTO chats (chat_id, title, kind, last_msg_id, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title       = COALESCE(excluded.title, chats.title),
            kind        = COALESCE(excluded.kind, chats.kind),
            last_msg_id = MAX(chats.last_msg_id, excluded.last_msg_id),
            updated_at  = MAX(chats.updated_at, excluded.updated_at)
        """,
        (chat_id, title, _chat_kind(chat_entity), int(msg.id), ts),
    )


async def _catch_up(conn: sqlite3.Connection, client, per_dialog_limit: int = 100) -> int:
    """Pull recent history for every dialog past our stored ``last_msg_id``.

    Bounded by ``per_dialog_limit`` so a never-before-synced account doesn't
    pull millions of messages on first boot. Returns total messages inserted.
    """
    total = 0
    async for dialog in client.iter_dialogs():
        chat_id = int(dialog.id)
        row = conn.execute(
            "SELECT last_msg_id FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        last_seen = int(row[0]) if row else 0
        # Cheap skip: iter_dialogs already gives us the dialog's newest message
        # id — if it isn't past what we've stored, DON'T make a GetHistory call
        # at all. On a restart almost every dialog is up to date, so this avoids
        # ~one API call per dialog and the resulting Telegram flood-waits.
        top = getattr(getattr(dialog, "message", None), "id", None) \
            or int(getattr(dialog, "top_message", 0) or 0)
        if top and top <= last_seen:
            continue
        inserted = 0
        async for msg in client.iter_messages(dialog.entity, limit=per_dialog_limit):
            if msg.id <= last_seen:
                break
            await _record_message(conn, client, msg, chat_entity=dialog.entity)
            inserted += 1
        if inserted:
            log.info("catchup: chat=%s (%s) +%d msgs", chat_id, dialog.name, inserted)
            total += inserted
            await asyncio.sleep(0.5)  # gentle pacing between real history fetches
    return total


# ---------------------------------------------------------------------------
# Send-as-Tim: drain the outgoing queue through the bridge's live session.
# ---------------------------------------------------------------------------


async def _send_one(client, msg) -> tuple[bool, Optional[str]]:
    """Send ONE queued outgoing row via the live Telethon client.

    Resolves the target entity by chat_id, then sends ``text`` (as a reply when
    ``reply_to_msg_id`` is set). Returns ``(ok, error)``: ``(True, None)`` on
    success, ``(False, "<reason>")`` on any exception. Pure of DB writes so tests
    can drive it with a fake client; the caller records the result.
    """
    try:
        chat_id = int(msg["chat_id"])
        reply_to = msg["reply_to_msg_id"]
        entity = await client.get_entity(chat_id)
        await client.send_message(
            entity, msg["text"], reply_to=reply_to or None
        )
        return True, None
    except Exception as e:  # noqa: BLE001 — Telethon raises a wide variety
        return False, str(e)[:300]


async def _outgoing_sender(cfg: Config, client, stop: "asyncio.Event") -> None:
    """Background task: poll tracking.db for queued sends and send them.

    Opens its OWN tracking.db connection (the bridge's messages.db conn is for a
    different file). Every ~5s: a cheap queued-count check first (sleep when the
    queue is empty); when non-empty, drains it FIFO. Each send is guarded so one
    bad message marks itself failed and never stalls the loop. Honors ``stop``
    like the rest of run() for a clean shutdown.
    """
    conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    log.info("outgoing sender started (queue: %s)", cfg.tg_tracking_db_path)
    try:
        while not stop.is_set():
            try:
                pending = tracking_store.count_outgoing(conn, "queued")
                if pending:
                    for msg in tracking_store.list_outgoing(conn, status="queued"):
                        if stop.is_set():
                            break
                        ok, err = await _send_one(client, msg)
                        if ok:
                            tracking_store.mark_outgoing_sent(conn, msg["id"])
                            log.info(
                                "sent outgoing id=%s chat=%s", msg["id"], msg["chat_id"]
                            )
                        else:
                            tracking_store.mark_outgoing_failed(conn, msg["id"], err or "")
                            log.warning(
                                "outgoing send failed id=%s chat=%s: %s",
                                msg["id"], msg["chat_id"], err,
                            )
            except Exception:  # noqa: BLE001 — loop must never die
                log.exception("outgoing sender iteration failed")
            # Sleep, but wake early on shutdown.
            try:
                await asyncio.wait_for(stop.wait(), timeout=_OUTGOING_POLL_SEC)
            except asyncio.TimeoutError:
                pass
    finally:
        conn.close()
        log.info("outgoing sender stopped")


# ---------------------------------------------------------------------------
# Auto-draft: drain the draft queue through the bridge's live session and set
# per-chat drafts. Same single-session constraint as send-as-Tim — the periodic
# tick (worker) / realtime path (bridge thread) enqueue rows; only this task,
# on the event loop with the live client, can call SaveDraftRequest.
# ---------------------------------------------------------------------------


async def _read_current_draft(client, chat_id: int) -> str:
    """Best-effort read of a chat's CURRENT Telegram draft text ('' when none).

    Used by the clobber-guard to tell our own draft from text Tim typed. Raises
    on a hard failure so the caller marks the row failed rather than blindly
    overwriting. Resolves the chat's input peer and reads its dialog's draft via
    GetPeerDialogsRequest.
    """
    from telethon.tl import functions, types

    input_entity = await client.get_input_entity(int(chat_id))
    res = await client(
        functions.messages.GetPeerDialogsRequest(
            peers=[types.InputDialogPeer(peer=input_entity)]
        )
    )
    dialogs = getattr(res, "dialogs", None) or []
    if not dialogs:
        return ""
    draft = getattr(dialogs[0], "draft", None)
    return getattr(draft, "message", "") or ""


async def _set_one_draft(
    client, chat_id: int, text: str, current_draft: str,
    last_set_text: Optional[str] = None,
) -> tuple[bool, bool, Optional[str]]:
    """Set ONE chat's draft via the live client, honoring the CLOBBER-GUARD.

    Returns ``(set_ok, skipped, error)``:
      * ``(False, True, None)``  — the chat's current draft is NON-EMPTY and not
        what WE last wrote (``last_set_text``), i.e. Tim typed/edited something →
        SKIP, never clobber his text. No SaveDraft call is made.
      * ``(True, False, None)``  — draft set (empty ``text`` CLEARS the draft).
      * ``(False, False, err)``  — an exception (e.g. entity resolution / network).

    Pure of DB writes so tests drive it with a fake client; the caller records
    the result. ``current_draft`` is read by the caller and passed in.
    """
    try:
        cur = current_draft or ""
        last = last_set_text or ""
        # Clobber-guard: a non-empty draft that isn't our last-written text means
        # Tim has his own content in the box — leave it untouched.
        if cur and cur != last:
            return False, True, None
        from telethon.tl import functions

        entity = await client.get_entity(int(chat_id))
        await client(
            functions.messages.SaveDraftRequest(peer=entity, message=text or "")
        )
        return True, False, None
    except Exception as e:  # noqa: BLE001 — Telethon raises a wide variety
        return False, False, str(e)[:300]


async def _draft_setter(cfg: Config, client, stop: "asyncio.Event") -> None:
    """Background task: poll tracking.db for queued drafts and set them.

    Mirrors ``_outgoing_sender``: own tracking.db connection, ~5s poll, each chat
    guarded so one bad chat can't stall the loop, honors ``stop``. For each
    queued row it reads the chat's CURRENT draft (clobber-guard input), then
    sets/clears the draft via ``_set_one_draft``. A SKIP (the owner's own text present)
    is marked ``set`` WITHOUT changing ``last_set_text`` so it isn't reprocessed
    every poll yet his text is never overwritten.
    """
    conn = tracking_store.init_tracking_db(cfg.tg_tracking_db_path)
    log.info("draft setter started (queue: %s)", cfg.tg_tracking_db_path)
    try:
        while not stop.is_set():
            try:
                for d in tracking_store.list_queued_drafts(conn):
                    if stop.is_set():
                        break
                    chat_id = int(d["chat_id"])
                    text = d["text"] or ""
                    last_set = d["last_set_text"]
                    try:
                        current = await _read_current_draft(client, chat_id)
                    except Exception as e:  # noqa: BLE001 — guard per chat
                        tracking_store.mark_draft_failed(conn, chat_id, str(e))
                        log.warning("draft read failed chat=%s: %s", chat_id, e)
                        continue
                    ok, skipped, err = await _set_one_draft(
                        client, chat_id, text, current, last_set
                    )
                    if ok:
                        tracking_store.mark_draft_set(conn, chat_id, text)
                        log.info("draft set chat=%s (len=%d)", chat_id, len(text))
                    elif skipped:
                        # Don't clobber Tim's text; stop reprocessing but keep the
                        # recorded last_set_text unchanged.
                        tracking_store.mark_draft_set(conn, chat_id, last_set or "")
                        log.info("draft skipped (foreign text) chat=%s", chat_id)
                    else:
                        tracking_store.mark_draft_failed(conn, chat_id, err or "")
                        log.warning("draft set failed chat=%s: %s", chat_id, err)
            except Exception:  # noqa: BLE001 — loop must never die
                log.exception("draft setter iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=_DRAFT_POLL_SEC)
            except asyncio.TimeoutError:
                pass
    finally:
        conn.close()
        log.info("draft setter stopped")


async def run(cfg: Config) -> int:
    if not cfg.tg_user_api_id or not cfg.tg_user_api_hash:
        log.warning("tg_user disabled: api_id/api_hash empty in secrets.toml [telegram_user]")
        return 0

    from telethon import TelegramClient, events

    from bot_squad_worker.tg_proxy import telethon_proxy_from_env

    cfg.tg_user_dir.mkdir(parents=True, exist_ok=True)
    conn = open_db(cfg.tg_user_db_path)
    log.info("opened db: %s", cfg.tg_user_db_path)

    client = TelegramClient(
        str(cfg.tg_user_session_path),
        cfg.tg_user_api_id,
        cfg.tg_user_api_hash,
        proxy=telethon_proxy_from_env(),
    )
    await client.connect()
    if not await client.is_user_authorized():
        log.error(
            "telethon session not authorized — run `python -m bot_squad_worker.tg_user_cli login` first"
        )
        await client.disconnect()
        return 2

    me = await client.get_me()
    log.info("logged in as %s (id=%s)", _sender_name(me), getattr(me, "id", "?"))

    # Real-time tracking hook: coalesces per-chat extraction off the event loop.
    debouncer = RealtimeDebouncer(cfg)

    @client.on(events.NewMessage(incoming=True))
    async def _on_new(event) -> None:
        try:
            chat_entity = await event.get_chat()
        except Exception:  # noqa: BLE001 — Telethon raises a wide variety
            chat_entity = None
        try:
            await _record_message(conn, client, event.message, chat_entity=chat_entity,
                                  transcribe=True)
        except Exception:  # noqa: BLE001
            log.exception("failed to record incoming message id=%s", event.message.id)
            return
        # After recording, (re)arm real-time extraction for hot-list chats. This
        # only schedules a debounce timer — the (blocking) LLM work runs later in
        # a thread executor — so ingest is never blocked. Never let it crash the
        # handler.
        try:
            _schedule_realtime(cfg, debouncer, event, chat_entity)
        except Exception:  # noqa: BLE001
            log.exception("realtime scheduling failed")
        # @claude auto-reply (scope-INDEPENDENT). If this message invoked @claude,
        # handle it; otherwise, if an auto-help window is active for the chat,
        # auto-reply to this message. Both run the LLM in an executor (never block
        # ingest) and never crash the handler.
        try:
            handled = await _handle_claude_trigger(cfg, client, event)
        except Exception:  # noqa: BLE001
            log.exception("claude trigger (incoming) failed")
            handled = False
        if not handled:
            try:
                await _maybe_auto_help_followup(cfg, client, event)
            except Exception:  # noqa: BLE001
                log.exception("auto-help follow-up failed")

    @client.on(events.NewMessage(outgoing=True))
    async def _on_outgoing(event) -> None:
        # ORDER MATTERS. A COMMAND/TRIGGER message must NOT also be recorded or
        # extracted as a normal message:
        #   1) /swarm-answer (or /sa) — draft a reply into Tim's input box. Runs
        #      FIRST and short-circuits (it deletes the trigger).
        #   2) @claude — summon the swarm (gated to the allowlist). Handled here.
        #   3) otherwise, if this is a REALTIME-tracked chat, record the owner's own
        #      message + (re)arm realtime extraction so a hanging suggested reply
        #      clears live and his own action items get picked up.
        # the owner's messages to the swarm bot are for the coordinator (coord_inbox).
        # Voice → Telegram's BUILT-IN transcription, retrying through transient
        # "Error during transcription" responses.
        try:
            if await _handle_bot_inbox(cfg, client, event):
                return
        except Exception:  # noqa: BLE001
            log.exception("bot inbox (outgoing) failed")
        try:
            if await _handle_swarm_answer(cfg, client, event):
                return
        except Exception:  # noqa: BLE001
            log.exception("swarm-answer (outgoing) failed")
        try:
            handled = await _handle_claude_trigger(cfg, client, event)
        except Exception:  # noqa: BLE001
            log.exception("claude trigger (outgoing) failed")
            handled = True  # be conservative: don't record a half-handled trigger
        if handled:
            return
        # ALL chats (not just realtime): if Tim answered in his own words, drop the
        # stale suggested draft for that chat.
        try:
            await _clear_pending_reply_on_own_message(cfg, event)
        except Exception:  # noqa: BLE001
            log.exception("clear-pending-reply-on-own-message failed")
        try:
            await _record_outgoing_realtime(cfg, conn, client, debouncer, event)
        except Exception:  # noqa: BLE001
            log.exception("outgoing realtime record/schedule failed")

    stop = asyncio.Event()

    def _signal_stop(*_: object) -> None:
        log.info("shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _signal_stop())

    # Send-as-Tim: start the outgoing-queue drainer on THIS event loop (the only
    # process with the live session) BEFORE catch-up, so sends work immediately
    # even while history backfills. Shares the stop-event for clean shutdown.
    sender_task = asyncio.create_task(_outgoing_sender(cfg, client, stop))
    # Auto-draft: drain the draft queue and set per-chat drafts on THIS loop (the
    # only process with the live session). Started alongside the sender; shares
    # the stop-event for clean shutdown.
    draft_task = asyncio.create_task(_draft_setter(cfg, client, stop))

    # Catch up history in the BACKGROUND so a restart doesn't block sends / live
    # ingest (and the gentle, skip-up-to-date catch-up no longer hammers Telegram).
    async def _catch_up_bg() -> None:
        try:
            inserted = await _catch_up(conn, client)
            log.info("catchup complete: +%d msgs", inserted)
        except Exception:  # noqa: BLE001
            log.exception("catch-up failed; continuing live")

    catchup_task = asyncio.create_task(_catch_up_bg())

    log.info("tg_user listening for new messages")
    # Race our stop-event against the client disconnecting. client.disconnected is
    # a Future (NOT a coroutine) — pass it through ensure_future, not create_task.
    disconnected = asyncio.ensure_future(client.disconnected)
    stopper = asyncio.create_task(stop.wait())
    await asyncio.wait({disconnected, stopper}, return_when=asyncio.FIRST_COMPLETED)

    # Tear down background tasks (they also watch stop, but set it in case we
    # exited via disconnect rather than a signal).
    stop.set()
    for t in (sender_task, draft_task, catchup_task):
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    await client.disconnect()
    conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="bot-squad-worker.tg_user")
    parser.add_argument(
        "--config",
        default=os.environ.get("BOT_SQUAD_CONFIG", "/home/www/bot-squad/config"),
        help="path to bot-swarm config dir",
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "info"))
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    cfg = Config.load(Path(args.config))
    return asyncio.run(run(cfg))


if __name__ == "__main__":
    raise SystemExit(main())
