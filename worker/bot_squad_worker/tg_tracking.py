"""Chat-tracking scope resolution (chat-tracking system, increment 1).

PURE, unit-testable scope logic over a project's ``tg_tracking`` config (parsed
in config.py). No I/O. Decides, for a given chat, whether the tracking
pipelines should process it at all (``is_tracked``) and whether it is on the
real-time hot list vs. the periodic list (``is_realtime``).

Two modes (design §"Scope = 2 modes"):
- ``include`` — track ONLY chats in ``tg_track_chats``.
- ``exclude`` — track ALL chats EXCEPT those in ``tg_track_chats``.

Plus a ``tg_track_realtime`` sub-list: chats processed on message arrival;
everything else in scope is processed periodically.

DB persistence (todos/meetings/replies/watermarks) lives in ``tracking_store``;
this module stays free of side effects so the pipelines can call it per message.

An entry in a list matches a chat if it equals the int ``chat_id``, OR
(case-insensitively) equals the ``chat_title`` or the ``@username``. ``@`` is
optional on username entries. Numeric *string* entries also match ``chat_id``,
so an operator who quotes an id in TOML still gets the intended chat.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

from bot_squad_worker import tracking_store


def _chat_matches(
    entry: int | str,
    chat_id: Optional[int],
    title: Optional[str],
    username: Optional[str],
) -> bool:
    """True iff a single config list entry identifies this chat.

    ``entry`` is stored as given by config (int chat_id or string title/handle).
    Matching is by int chat_id, or case-insensitive chat_title / @username.
    """
    # Integer entry → strict chat_id equality. (bool is excluded: it is an int
    # subclass but never a real chat id.)
    if isinstance(entry, bool):
        return False
    if isinstance(entry, int):
        return chat_id is not None and entry == chat_id

    s = str(entry).strip()
    if not s:
        return False

    # Numeric string (optionally signed) → also treat as a chat_id.
    sign_stripped = s[1:] if s[:1] in "+-" else s
    if sign_stripped.isdigit() and chat_id is not None:
        try:
            if int(s) == chat_id:
                return True
        except ValueError:  # pragma: no cover - isdigit() guards this
            pass

    low = s.lower()
    # Title match (case-insensitive, whitespace-trimmed).
    if title and low == str(title).strip().lower():
        return True
    # Username match — tolerate a leading '@' on either side.
    if username:
        u = str(username).strip().lstrip("@").lower()
        if u and low.lstrip("@") == u:
            return True
    return False


def _any_match(
    entries: Iterable[int | str],
    chat_id: Optional[int],
    title: Optional[str],
    username: Optional[str],
) -> bool:
    return any(_chat_matches(e, chat_id, title, username) for e in entries)


def is_tracked(
    proj: Any,
    chat_id: Optional[int],
    chat_title: Optional[str] = None,
    username: Optional[str] = None,
) -> bool:
    """Should the tracking pipelines process this chat for ``proj``?

    - include mode → chat must match ``tg_track_chats``.
    - exclude mode → chat must NOT match ``tg_track_chats``.

    DEFAULT config (exclude + empty list) → everything is tracked.
    """
    mode = str(getattr(proj, "tg_track_mode", "exclude") or "exclude").lower()
    listed = _any_match(
        getattr(proj, "tg_track_chats", ()) or (), chat_id, chat_title, username
    )
    if mode == "include":
        return listed
    # Any non-"include" value (incl. the default "exclude") is exclude semantics.
    return not listed


def is_realtime(
    proj: Any,
    chat_id: Optional[int],
    chat_title: Optional[str] = None,
    username: Optional[str] = None,
) -> bool:
    """True iff the chat is tracked AND on the real-time hot list.

    Realtime is always a subset of tracked: a chat outside scope is never
    real-time even if it appears in ``tg_track_realtime``.
    """
    if not is_tracked(proj, chat_id, chat_title, username):
        return False
    return _any_match(
        getattr(proj, "tg_track_realtime", ()) or (), chat_id, chat_title, username
    )


# ---------------------------------------------------------------------------
# Effective config — DB-first (live, no restart) with file-config fallback.
#
# The management UI (inc. 3) writes a ``tracking_config`` row to tracking.db.
# When present that row OVERRIDES the project's file config (tg_track_*); when
# absent the file config is the default. The pipelines + bridge resolve the
# EFFECTIVE config through here so a UI change applies on the next periodic run
# / next message, with no worker restart.
# ---------------------------------------------------------------------------


class _EffProj:
    """Lightweight proj-shaped view of the effective config.

    Carries exactly the attributes the pure matchers (``is_tracked`` /
    ``is_realtime``) and the pipelines read, so callers can reuse the existing
    matchers unchanged by passing one of these.
    """

    __slots__ = (
        "slug", "tg_track_enabled", "tg_track_mode",
        "tg_track_chats", "tg_track_realtime", "tg_track_claude",
        "tg_track_auto_draft",
    )

    def __init__(self, slug, enabled, mode, chats, realtime, claude=(),
                 auto_draft=()):
        self.slug = slug
        self.tg_track_enabled = bool(enabled)
        self.tg_track_mode = mode
        self.tg_track_chats = tuple(chats)
        self.tg_track_realtime = tuple(realtime)
        # @claude allowlist (prompt-injection gate). Default empty ⇒ off.
        self.tg_track_claude = tuple(claude or ())
        # Auto-draft allowlist (per-chat draft pre-fill). Default empty ⇒ off.
        self.tg_track_auto_draft = tuple(auto_draft or ())


def _loads_list(raw: Optional[str]) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    return list(v) if isinstance(v, (list, tuple)) else []


def _read_config_row(db_path, slug: str):
    """Read a ``tracking_config`` row read-only; None on any problem.

    Defensive by design: a missing db/table (the UI never wrote one), a path of
    None, or any sqlite error all fall through to None so the caller uses the
    file config. Opened read-only so a per-message scope check never writes WAL.
    """
    if not db_path or not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return tracking_store.get_config_row(conn, slug)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _proj_file_config(proj: Any) -> dict:
    """The file-config view of a proj-like object (the fallback default)."""
    if proj is None:
        return {"enabled": False, "mode": "exclude", "chats": [],
                "realtime": [], "claude": [], "auto_draft": []}
    return {
        "enabled": bool(getattr(proj, "tg_track_enabled", False)),
        "mode": str(getattr(proj, "tg_track_mode", "exclude") or "exclude"),
        "chats": list(getattr(proj, "tg_track_chats", ()) or ()),
        "realtime": list(getattr(proj, "tg_track_realtime", ()) or ()),
        # @claude allowlist has NO file-config source — it is UI/DB-only, so the
        # file fallback is always empty (off).
        "claude": list(getattr(proj, "tg_track_claude", ()) or ()),
        # Auto-draft allowlist is likewise UI/DB-only; file fallback empty (off).
        "auto_draft": list(getattr(proj, "tg_track_auto_draft", ()) or ()),
    }


def _read_row(cfg: Any, slug: str, conn: Optional[sqlite3.Connection]):
    if conn is not None:
        try:
            return tracking_store.get_config_row(conn, slug)
        except Exception:  # noqa: BLE001
            return None
    return _read_config_row(getattr(cfg, "tg_tracking_db_path", None), slug)


def _row_to_dict(row) -> dict:
    return {
        "enabled": bool(row["enabled"]),
        "mode": (row["mode"] or "exclude"),
        "chats": _loads_list(row["chats_json"]),
        "realtime": _loads_list(row["realtime_json"]),
        "claude": _loads_list(row["claude_json"]),
        "auto_draft": _loads_list(row["auto_draft_json"]),
    }


def get_effective_tracking(
    cfg: Any, slug: str, conn: Optional[sqlite3.Connection] = None
) -> dict:
    """Resolve the effective tracking config for ``slug`` (DB-first).

    Returns ``{enabled, mode, chats, realtime}``. Prefers the live
    ``tracking_config`` DB row; falls back to ``cfg.projects[slug]``'s file
    config (``tg_track_*``) when no row exists. Pass an open ``conn`` (e.g. the
    tick's tracking.db handle) to avoid re-opening; otherwise the row is read
    read-only from ``cfg.tg_tracking_db_path``. (When you already hold the
    proj object, prefer ``effective_proj`` so the fallback is that proj.)
    """
    row = _read_row(cfg, slug, conn)
    if row is not None:
        return _row_to_dict(row)
    return _proj_file_config((getattr(cfg, "projects", {}) or {}).get(slug))


def effective_proj(
    cfg: Any, proj: Any, conn: Optional[sqlite3.Connection] = None
) -> _EffProj:
    """Build a proj-shaped view of ``proj``'s EFFECTIVE config (DB-first).

    Prefers the live ``tracking_config`` DB row; falls back to THIS ``proj``'s
    own file config when no row exists (so callers that hold a proj — the tick,
    the bridge — get the right default even when it is not in ``cfg.projects``).
    The returned object is interchangeable with a real ``Project`` for the
    matchers + pipelines (it exposes ``slug`` + ``tg_track_*``).
    """
    slug = getattr(proj, "slug", "") or ""
    row = _read_row(cfg, slug, conn)
    eff = _row_to_dict(row) if row is not None else _proj_file_config(proj)
    return _EffProj(
        slug, eff["enabled"], eff["mode"], eff["chats"], eff["realtime"],
        eff.get("claude") or [], eff.get("auto_draft") or [],
    )


def is_tracked_effective(
    cfg: Any,
    slug: str,
    chat_id: Optional[int],
    chat_title: Optional[str] = None,
    username: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """``is_tracked`` against the EFFECTIVE (DB-first) config for ``slug``."""
    eff = get_effective_tracking(cfg, slug, conn=conn)
    p = _EffProj(slug, eff["enabled"], eff["mode"], eff["chats"],
                 eff["realtime"], eff.get("claude") or [])
    return is_tracked(p, chat_id, chat_title, username)


def is_realtime_effective(
    cfg: Any,
    slug: str,
    chat_id: Optional[int],
    chat_title: Optional[str] = None,
    username: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """``is_realtime`` against the EFFECTIVE (DB-first) config for ``slug``."""
    eff = get_effective_tracking(cfg, slug, conn=conn)
    p = _EffProj(slug, eff["enabled"], eff["mode"], eff["chats"],
                 eff["realtime"], eff.get("claude") or [])
    return is_realtime(p, chat_id, chat_title, username)


def is_claude_enabled(
    cfg: Any,
    chat_id: Optional[int],
    chat_title: Optional[str] = None,
    username: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """True iff ``chat_id`` is on the @claude ALLOWLIST of any ENABLED project.

    Prompt-injection safety gate: @claude (one-shot, auto-help windows + the
    follow-up auto-replies) only fires in chats Tim has explicitly allowlisted
    from the UI. Resolved against the EFFECTIVE (DB-first) config, so a UI change
    takes effect with no restart. DEFAULT (no DB row / empty list) ⇒ False, i.e.
    @claude is OFF everywhere until Tim adds chats.

    Entry matching reuses the same convention as the tracking lists (int
    chat_id, or case-insensitive title / @username).
    """
    for slug in (getattr(cfg, "projects", None) or {}):
        eff = get_effective_tracking(cfg, slug, conn=conn)
        if not eff.get("enabled"):
            continue
        if _any_match(eff.get("claude") or (), chat_id, chat_title, username):
            return True
    return False


def is_auto_draft_enabled(
    cfg: Any,
    chat_id: Optional[int],
    chat_title: Optional[str] = None,
    username: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """True iff ``chat_id`` is on the AUTO-DRAFT allowlist of any ENABLED project.

    When True, whenever the system has a suggested reply for the chat the bridge
    sets it as that chat's Telegram DRAFT (it lands in Tim's input box — nothing
    is sent). Resolved against the EFFECTIVE (DB-first) config so a UI toggle
    takes effect with no restart. DEFAULT (no DB row / empty list) ⇒ False, i.e.
    auto-draft is OFF everywhere until Tim toggles chats on. Entry matching reuses
    the tracking-list convention (int chat_id, or case-insensitive title /
    @username). Independent of tracking scope and the @claude allowlist.
    """
    for slug in (getattr(cfg, "projects", None) or {}):
        eff = get_effective_tracking(cfg, slug, conn=conn)
        if not eff.get("enabled"):
            continue
        if _any_match(eff.get("auto_draft") or (), chat_id, chat_title, username):
            return True
    return False
