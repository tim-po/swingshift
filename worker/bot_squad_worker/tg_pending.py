"""Confirm-first pending store for gated (OUTWARD) Telegram actions (Phase 2).

When an authorized user requests an OUTWARD/irreversible action (e.g. ``/deploy``)
``tg_authz.authorize`` returns ``needs_confirm``. Instead of executing, we stash a
PENDING record here and reply asking the user to confirm. The action runs only on
their explicit confirmation (matched against this record).

State model (load-bearing):
  - VOLATILE: records live under ``data/_worker/tg_pending/`` — NOT git memory.
  - Keyed on ``(chat_id, from_id)``: a confirmation is bound to the RAISER. A
    different user's "yes" in the same chat must NOT confirm someone else's
    pending (that cross-user binding is enforced at the call site, which checks
    ``raised_by`` against the confirmer; the per-(chat,from_id) key already keeps
    each user's pending separate).
  - TTL 300s (design §"Defaults" #5). ``get_active`` returns None once expired.
  - ``put`` SUPERSEDES any existing pending for that (chat,from_id) — a fresh
    gated request always replaces the stale one (single outstanding confirm per
    user per chat).

This module is pure I/O over an injectable ``data_dir`` (tests pass a tmp dir);
no network, no subprocess.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Pending TTL in seconds (design §"Defaults" #5: pending TTL 5 min).
TTL_SECONDS = 300


@dataclass(frozen=True)
class Pending:
    """One outstanding confirm-first request.

    - ``token``: opaque id; lets ``/confirm <token>`` disambiguate explicitly.
    - ``action``: the authz action name (e.g. "deploy").
    - ``params``: the dispatch params to run verbatim on confirmation.
    - ``raised_by``: the from_id that raised it (the authority binding).
    - ``role``: the raiser's role at raise time (audit only).
    - ``created_at`` / ``expires_at``: epoch seconds.
    """
    token: str
    action: str
    params: dict[str, Any]
    raised_by: int
    role: str
    created_at: float
    expires_at: float

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return now >= self.expires_at


def _dir(data_dir: Path) -> Path:
    return Path(data_dir) / "_worker" / "tg_pending"


def _path(data_dir: Path, chat_id: Any, from_id: Any) -> Path:
    # Sanitize both ids for the filename (chat_id can be a negative group id).
    safe_chat = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(chat_id))
    safe_from = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(from_id))
    return _dir(data_dir) / f"{safe_chat}-{safe_from}.json"


def put(data_dir: Path, chat_id: Any, from_id: int, action: str,
        params: dict[str, Any], role: str, *,
        now: Optional[float] = None, token: Optional[str] = None) -> Pending:
    """Write (supersede) the pending record for ``(chat_id, from_id)``.

    Returns the stored ``Pending``. Any prior pending for the same key is
    overwritten — there is at most one outstanding confirm per user per chat.
    """
    now = time.time() if now is None else now
    token = token or secrets.token_urlsafe(8)
    rec = Pending(
        token=token,
        action=action,
        params=dict(params),
        raised_by=int(from_id),
        role=role,
        created_at=now,
        expires_at=now + TTL_SECONDS,
    )
    p = _path(data_dir, chat_id, from_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "token": rec.token,
        "action": rec.action,
        "params": rec.params,
        "raised_by": rec.raised_by,
        "role": rec.role,
        "created_at": rec.created_at,
        "expires_at": rec.expires_at,
    }))
    return rec


def get_active(data_dir: Path, chat_id: Any, from_id: int,
               now: Optional[float] = None) -> Optional[Pending]:
    """Return the active (non-expired) pending for ``(chat_id, from_id)``, else None.

    An expired record returns None (the caller should reply "expired, re-issue").
    A corrupt/unreadable record also returns None (fail closed — never confirm a
    record we can't trust).
    """
    p = _path(data_dir, chat_id, from_id)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        rec = Pending(
            token=d["token"],
            action=d["action"],
            params=dict(d.get("params") or {}),
            raised_by=int(d["raised_by"]),
            role=d.get("role", ""),
            created_at=float(d["created_at"]),
            expires_at=float(d["expires_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if rec.is_expired(now):
        return None
    return rec


def clear(data_dir: Path, chat_id: Any, from_id: int) -> None:
    """Delete the pending record for ``(chat_id, from_id)`` (idempotent)."""
    p = _path(data_dir, chat_id, from_id)
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
