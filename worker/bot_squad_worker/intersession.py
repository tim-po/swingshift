"""Cross-session message bus.

Ports the cctv-backend ``ops/intersession.sh`` pattern into a worker action
surface. Three primitives:

- ``send`` appends a message line to the recipient's inbox log; recipients
  can be a literal SID or a role keyword (``teamlead`` / ``dev`` / ``all``).
- ``inbox_read`` drains lines since the high-water mark.
- ``inbox_wait`` long-polls until the inbox file grows past the mark or a
  timeout elapses. Designed to be invoked from Claude Code with
  ``run_in_background: true`` so the harness's task-notification fires when
  this returns — that's the no-polling push primitive.

File layout under ``data/<slug>/_chat/``:
    inbox-<sid>.log    append-only, one message per line
    seen-<sid>         single int (byte offset), read high-water mark
    heartbeat-<sid>    mtime touched while wait/read is running
"""
from __future__ import annotations

import logging
import os
import stat
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger("bot-squad-worker")


class PeerSendTooLarge(Exception):
    """Raised by `send` when the text exceeds `_MAX_TEXT_LEN`. Surfaced to
    the action layer as an ActionError so the caller gets a clean 4xx instead
    of silent truncation."""

# T-0002: prior value (4000) silently truncated 99% of real coord dispatches.
# Bumped to 64 KB — covers every realistic peer_send payload. Anything over
# `_WARN_TEXT_LEN` triggers a warning log; anything over `_MAX_TEXT_LEN`
# raises so the caller gets a 4xx error instead of silent data loss.
_WARN_TEXT_LEN = 16 * 1024
_MAX_TEXT_LEN = 64 * 1024
_MAX_WAIT_TIMEOUT = 1800
_HEARTBEAT_INTERVAL = 10.0

# T-0008: inbox log rotation. Rotation triggers when (a) the inbox file is
# bigger than this AND (b) the seen offset equals file size (everything
# drained, no in-flight unread). Rotated files keep `inbox-<sid>.log.N`
# numeric suffix; oldest beyond `_INBOX_KEEP` rotations are pruned.
_INBOX_ROTATE_BYTES = 1 * 1024 * 1024
_INBOX_KEEP = 4


def _chat_dir(cfg: Any, slug: str) -> Path:
    """Return data/<slug>/_chat, creating it 770 if missing."""
    d = Path(cfg.data_dir) / slug / "_chat"
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(stat.S_IRWXU | stat.S_IRWXG)  # 770
        except OSError:
            pass
    return d


def _sanitize(text: str) -> str:
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    n = len(text)
    if n > _MAX_TEXT_LEN:
        # T-0002: fail loudly instead of silent truncation.
        raise PeerSendTooLarge(
            f"peer_send text exceeds {_MAX_TEXT_LEN} bytes (got {n}). "
            f"Split the dispatch into multiple shorter messages."
        )
    if n > _WARN_TEXT_LEN:
        _log.warning(
            "peer_send text is %d bytes (> %d-byte warn threshold). "
            "Consider splitting; the protocol's hard cap is %d.",
            n, _WARN_TEXT_LEN, _MAX_TEXT_LEN,
        )
    return text


def _list_session_sids(cfg: Any, slug: str) -> list[tuple[str, dict]]:
    """Parse session md frontmatter for every session in data/<slug>/sessions/.

    Returns (sid, meta) tuples. Meta values are raw strings (no type coercion).
    """
    sess_dir = Path(cfg.data_dir) / slug / "sessions"
    if not sess_dir.exists():
        return []
    out: list[tuple[str, dict]] = []
    for md in sorted(sess_dir.glob("*.md")):
        text = md.read_text()
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        meta: dict[str, str] = {}
        for line in parts[1].strip().splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
        sid = meta.get("sid", md.stem)
        out.append((sid, meta))
    return out


def _resolve_recipients(cfg: Any, slug: str, to: str) -> list[str]:
    """Map a recipient spec to a list of SIDs.

    Recipient kinds (resolved in this order):
      - ``teamlead`` / ``dev`` / ``all``: bot-squad legacy role keywords.
      - SID literal: anything starting with ``S-`` (the SID format).
      - Role keyword: anything else is treated as a swarm role name
        (e.g. ``coordinator``, ``planner``, ``backend-expert``) and matches
        every session whose ``role:`` frontmatter equals it. An empty list
        means no live session embodies that role — the caller (``send``)
        falls back to the holding inbox.
    """
    if to in {"teamlead", "dev", "all"}:
        sids: list[str] = []
        for sid, meta in _list_session_sids(cfg, slug):
            tid = meta.get("task_id", "") or ""
            is_dev = bool(tid) and tid != "~"
            if to == "all":
                sids.append(sid)
            elif to == "teamlead" and not is_dev:
                sids.append(sid)
            elif to == "dev" and is_dev:
                sids.append(sid)
        return sids
    if to.startswith("S-"):
        return [to]
    # Treat as swarm role keyword: broadcast to every session with role: <to>.
    sids = []
    for sid, meta in _list_session_sids(cfg, slug):
        if (meta.get("role") or "") == to:
            sids.append(sid)
    return sids


def _is_role_keyword(to: str) -> bool:
    """True iff ``to`` is shaped like a swarm role keyword (not a SID,
    not a legacy keyword).
    """
    if to in {"teamlead", "dev", "all"}:
        return False
    if to.startswith("S-"):
        return False
    return bool(to)


def _holding_path(cfg: Any, slug: str, role: str) -> Path:
    return _chat_dir(cfg, slug) / f"holding-{role}.log"


def _role_for_sid(cfg: Any, slug: str, sid: str) -> str | None:
    """Read this session's md and return its ``role:`` field, or None."""
    md = Path(cfg.data_dir) / slug / "sessions" / f"{sid}.md"
    if not md.exists():
        return None
    text = md.read_text()
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        if k.strip() == "role":
            v = v.strip()
            return v if v and v != "~" else None
    return None


def drain_unread_to_holding(cfg: Any, slug: str, sid: str, role: str) -> int:
    """Move any UNREAD mail (bytes past ``seen-<sid>``) from inbox-<sid>
    into holding-<role>.log. Used when a pane is respawned: the new pane
    has a fresh SID, so unread mail in the old SID's inbox would be
    orphaned. Draining into the role's holding inbox routes it to
    whichever same-role pane reads next.

    Returns the number of message lines drained.
    """
    inbox = _inbox_path(cfg, slug, sid)
    seen = _seen_path(cfg, slug, sid)
    if not inbox.exists():
        return 0
    try:
        offset = int(seen.read_text().strip()) if seen.exists() else 0
    except (FileNotFoundError, ValueError):
        offset = 0
    try:
        size = inbox.stat().st_size
    except FileNotFoundError:
        return 0
    if size <= offset:
        return 0
    with inbox.open("rb") as f:
        f.seek(offset)
        unread = f.read(size - offset)
    if not unread:
        return 0
    holding = _holding_path(cfg, slug, role)
    with holding.open("ab") as f:
        f.write(unread)
    seen.write_text(str(size))
    return unread.count(b"\n")


def _drain_one_claim(claim: Path, inbox: Path) -> None:
    """Append a claimed holding file's content into ``inbox`` then remove the
    claim. Commit ordering (each step durably fsync'd):

        1. append the claim's bytes to the inbox
        2. truncate the claim to zero bytes  <-- the commit point
        3. unlink the (now-empty) claim

    A crash before step 2 leaves a non-empty claim: recovery re-appends — the
    only window where a batch can be delivered twice, and it is bounded to the
    single claimed batch (the original read-append-truncate bug re-delivered on
    EVERY subsequent holding read, unbounded). A crash between step 2 and 3
    leaves an EMPTY claim: recovery appends nothing, so no double-deliver — the
    common case. Callers must only ever route a given claim to ONE inbox.
    """
    try:
        buf = claim.read_bytes()
    except OSError:
        return
    if buf:
        with inbox.open("ab") as f:
            f.write(buf)
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                pass
        # Commit point: empty the claim so a crash after this can't re-deliver.
        try:
            with claim.open("wb") as cf:
                cf.flush()
                os.fsync(cf.fileno())
        except OSError:
            pass
    try:
        claim.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _drain_holding(cfg: Any, slug: str, role: str, sid: str) -> None:
    """Atomically claim any pending holding-<role>.log content and append it
    into this session's inbox.

    S2: instead of read-append-truncate (which raced two same-role drains and
    double-delivered on a crash between append and truncate), this claims the
    holding file via ``os.rename`` into a per-drain claim file. ``rename`` is
    atomic on a single filesystem, so exactly one concurrent drain wins the
    claim; losers see no holding file and no-op. The winner then appends from
    the claim and unlinks it. A crash after the rename but before the unlink
    leaves a stale claim that the NEXT same-sid drain recovers (re-appends and
    unlinks) — no lines are lost, and because a given claim suffix is unique
    per (sid, time) it is only ever drained into one inbox, so no double-deliver
    across distinct sessions.
    """
    holding = _holding_path(cfg, slug, role)
    inbox = _inbox_path(cfg, slug, sid)

    # Recover any stale claim left by a crashed prior drain of THIS sid. The
    # claim suffix embeds the sid, so this only reclaims our own orphans —
    # never another live session's in-flight claim.
    claim_glob = f"{holding.name}.{sid}.*.claim"
    for stale in sorted(holding.parent.glob(claim_glob)):
        _drain_one_claim(stale, inbox)

    if not holding.exists():
        return
    # Unique claim name: holding-<role>.log.<sid>.<ns>.claim. The nanosecond
    # stamp keeps back-to-back drains by the same sid from colliding.
    claim = holding.with_name(f"{holding.name}.{sid}.{time.monotonic_ns()}.claim")
    try:
        os.rename(holding, claim)
    except FileNotFoundError:
        # Another concurrent drain claimed it first — nothing for us.
        return
    except OSError:
        return
    _drain_one_claim(claim, inbox)
    # Preserve the legacy contract: after a drain the holding file exists but
    # is empty (callers/tests observe ``holding.read_bytes() == b""``). Don't
    # clobber a file a concurrent ``send`` may have just recreated with new
    # mail — only create it if it's currently absent.
    if not holding.exists():
        try:
            holding.touch()
        except OSError:
            pass


def _inbox_path(cfg: Any, slug: str, sid: str) -> Path:
    return _chat_dir(cfg, slug) / f"inbox-{sid}.log"


def _seen_path(cfg: Any, slug: str, sid: str) -> Path:
    return _chat_dir(cfg, slug) / f"seen-{sid}"


def _heartbeat_path(cfg: Any, slug: str, sid: str) -> Path:
    return _chat_dir(cfg, slug) / f"heartbeat-{sid}"


def _touch(p: Path) -> None:
    try:
        p.touch(exist_ok=True)
    except OSError:
        pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def send(cfg: Any, slug: str, from_sid: str, to: str, text: str) -> dict:
    """Append a message line to recipient inboxes.

    For a swarm role keyword with zero live sessions, writes to the role's
    holding inbox at ``_chat/holding-<role>.log``; the first session of
    that role to spawn will drain it on its next ``inbox_read``.

    Returns ``{"ok": True, "delivered_to": [sid, ...], "held_for": <role>?}``.
    """
    sanitized = _sanitize(text)
    recipients = _resolve_recipients(cfg, slug, to)
    line = f"{_now_iso()}\t[from {from_sid}]\t{sanitized}\n"
    delivered: list[str] = []
    for sid in recipients:
        inbox = _inbox_path(cfg, slug, sid)
        with inbox.open("ab") as f:
            f.write(line.encode("utf-8"))
        delivered.append(sid)
    if not delivered and _is_role_keyword(to):
        holding = _holding_path(cfg, slug, to)
        with holding.open("ab") as f:
            f.write(line.encode("utf-8"))
        return {"ok": True, "delivered_to": [], "held_for": to}
    return {"ok": True, "delivered_to": delivered}


def _maybe_rotate_inbox(inbox: Path, seen: Path) -> bool:
    """T-0008: rotate `inbox-<sid>.log` when it exceeds `_INBOX_ROTATE_BYTES`
    AND the seen offset is at end-of-file (no unread mail to lose).

    Rotation pattern: shift `.log.N` → `.log.N+1`; current `.log` → `.log.1`;
    create a fresh empty `.log`; reset seen to 0. Prune rotations beyond
    `_INBOX_KEEP`. Returns True iff rotation happened.
    """
    try:
        size = inbox.stat().st_size
    except FileNotFoundError:
        return False
    if size < _INBOX_ROTATE_BYTES:
        return False
    try:
        offset = int(seen.read_text().strip())
    except (FileNotFoundError, ValueError):
        offset = 0
    if offset < size:
        # Unread mail; don't rotate (would dissociate seen offset from file).
        return False
    # Shift existing rotations: .log.3 -> .log.4, .log.2 -> .log.3, ...
    for n in range(_INBOX_KEEP, 0, -1):
        src = inbox.with_suffix(inbox.suffix + f".{n}")
        if src.exists():
            if n >= _INBOX_KEEP:
                src.unlink()
            else:
                src.rename(inbox.with_suffix(inbox.suffix + f".{n+1}"))
    inbox.rename(inbox.with_suffix(inbox.suffix + ".1"))
    inbox.touch()
    seen.write_text("0")
    return True


def inbox_read(cfg: Any, slug: str, sid: str) -> dict:
    """Drain inbox lines since the seen-<sid> byte offset.

    Before reading, drains any pending holding-<role>.log into this
    session's inbox if the session has a role. That covers messages
    addressed to the role while no session of that role was live.

    After draining, opportunistically rotates the inbox file if it exceeds
    the size threshold and is fully drained (T-0008).
    """
    _touch(_heartbeat_path(cfg, slug, sid))
    role = _role_for_sid(cfg, slug, sid)
    if role:
        _drain_holding(cfg, slug, role, sid)
    inbox = _inbox_path(cfg, slug, sid)
    seen = _seen_path(cfg, slug, sid)
    if not inbox.exists():
        # Touch a zero-byte inbox so subsequent waits have something to watch.
        inbox.touch()
    try:
        offset = int(seen.read_text().strip())
    except (FileNotFoundError, ValueError):
        offset = 0
    size = inbox.stat().st_size
    messages: list[str] = []
    if size > offset:
        with inbox.open("rb") as f:
            f.seek(offset)
            buf = f.read(size - offset)
        text = buf.decode("utf-8", errors="replace")
        # Split on real newlines, drop the trailing empty from the final \n.
        messages = [m for m in text.split("\n") if m]
        seen.write_text(str(size))
    # T-0008: best-effort rotation. Now that seen offset == size, this can
    # roll over an oversized inbox without losing in-flight mail.
    try:
        _maybe_rotate_inbox(inbox, seen)
    except OSError as e:
        _log.warning("inbox rotation failed for %s: %s", inbox, e)
    return {"ok": True, "messages": messages, "count": len(messages)}


def inbox_wait(cfg: Any, slug: str, sid: str, timeout: float) -> dict:
    """Long-poll for inbox growth.

    Returns ``{"ok": True, "ready": bool, "elapsed_sec": float}``. ``ready``
    True means "you have new mail, call ``inbox_read``"; False means the
    timeout expired without new mail.
    """
    timeout = max(0.0, min(float(timeout), float(_MAX_WAIT_TIMEOUT)))
    inbox = _inbox_path(cfg, slug, sid)
    seen = _seen_path(cfg, slug, sid)
    hb = _heartbeat_path(cfg, slug, sid)
    if not inbox.exists():
        inbox.touch()
    try:
        offset = int(seen.read_text().strip())
    except (FileNotFoundError, ValueError):
        offset = 0

    start = time.monotonic()
    deadline = start + timeout
    last_hb = 0.0
    # Plain stat() poll (1s) — keeps the implementation portable. Linux
    # inotify would only buy us sub-second wake latency, and the message
    # bus's latency budget is "human-perceptible turn", not sub-second.
    poll_interval = 1.0
    while True:
        now = time.monotonic()
        if now - last_hb >= _HEARTBEAT_INTERVAL:
            _touch(hb)
            last_hb = now
        try:
            size = inbox.stat().st_size
        except FileNotFoundError:
            size = 0
        if size > offset:
            return {"ok": True, "ready": True, "elapsed_sec": now - start}
        if now >= deadline:
            return {"ok": True, "ready": False, "elapsed_sec": now - start}
        sleep_for = min(poll_interval, deadline - now)
        if sleep_for > 0:
            time.sleep(sleep_for)
