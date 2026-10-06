"""Outbound Telegram client with debounce and SID-prefix support.

Usage::

    client = TgClient(cfg)
    sent = client.send(chat_id="404580642", text="hello", sid="S-test-p5", user="alexey")

Debounce: the same (chat_id, sid, text) combination is silently dropped for
``cooldown_sec`` seconds (default 60).  State lives as empty files in
``data/_worker/tg_debounce/``, keyed by a SHA-256 of the three components.

Empty token: if ``cfg.tg_bot_token`` is empty, ``send`` returns ``False``
without raising.  This keeps test fixtures (which set bot_token="TESTBOT:TOKEN"
or "") working without network access.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_squad_worker.config import Config

log = logging.getLogger(__name__)

# Telegram Bot API base URL.
_TG_API = "https://api.telegram.org/bot{token}/sendMessage"

# Retry budget for the flaky xray egress proxy (see _post).
_ATTEMPTS = 3
_BACKOFF_SEC = 2.0


class TgClient:
    """Outbound TG sender bound to a single bot token + data dir."""

    def __init__(self, cfg: "Config", cooldown_sec: int = 60) -> None:
        self._token: str = cfg.tg_bot_token
        self._debounce_dir: Path = cfg.data_dir / "_worker" / "tg_debounce"
        self._cooldown: int = cooldown_sec
        self._quiet_start_utc: int = getattr(cfg, "tg_quiet_hours_start_utc", 17)
        self._quiet_end_utc: int = getattr(cfg, "tg_quiet_hours_end_utc", 5)
        # api.telegram.org is NOT directly routable from this host — egress is via
        # the xray proxy. Config has carried tg_egress_proxy all along ("HTTP proxy
        # for TG bot sends, past the DPI block"); _post simply never used it, so
        # EVERY send raised ConnectError: Network is unreachable. Including the
        # oauth_refresh failure alert (jobs.py) — the page that fires when the swarm
        # is losing its auth could never have reached anyone.
        self._proxy: str = getattr(cfg, "tg_egress_proxy", "") or ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send(
        self,
        *,
        chat_id: str,
        text: str,
        sid: str = "",
        user: str = "",
        urgent: bool = False,
    ) -> bool:
        """Send ``text`` to ``chat_id``, prefixed by SID if given.

        Returns True if the message was sent, False if suppressed (empty
        token, debounce, or quiet hours).  Raises on network/API errors.

        ``urgent=True`` bypasses quiet hours AND the debounce (use for hard
        failures the stakeholder explicitly asked to be paged on; not for
        routine "needs your input" pings).

        The debounce bypass matters more than it looks: an alert REPEATS by
        nature ("node still burning", "coordinator still down"), so its text is
        identical every time — which is exactly what the debounce drops. An
        urgent page could therefore be swallowed in silence, and since
        suppression returns False rather than raising, a caller that ignores the
        return value never learned its page had vanished. Suppressing an urgent
        message is now logged at WARNING, never in silence.
        """
        if not self._token:
            log.warning("tg.send: no bot token configured — message DROPPED%s",
                        " (URGENT)" if urgent else "")
            return False

        if not urgent and _in_quiet_hours(self._quiet_start_utc, self._quiet_end_utc):
            log.info("tg.send: dropped (quiet hours — user is asleep)")
            return False

        full_text = _prefix(text, sid=sid, user=user)

        if self._debounced(chat_id=chat_id, sid=sid, text=text):
            if urgent:
                log.warning("tg.send: URGENT message matched the debounce "
                            "(same text within %ds) — sending ANYWAY; an alert "
                            "repeats by design", self._cooldown)
            else:
                log.debug("tg.send: debounced (same payload within %ds)",
                          self._cooldown)
                return False

        self._post(chat_id=chat_id, text=full_text)
        self._record(chat_id=chat_id, sid=sid, text=text)
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _debounce_path(self, chat_id: str, sid: str, text: str) -> Path:
        key = f"{chat_id}\x00{sid}\x00{text}"
        h = hashlib.sha256(key.encode()).hexdigest()
        return self._debounce_dir / h

    def _debounced(self, *, chat_id: str, sid: str, text: str) -> bool:
        p = self._debounce_path(chat_id, sid, text)
        if not p.exists():
            return False
        age = time.time() - p.stat().st_mtime
        return age < self._cooldown

    def _record(self, *, chat_id: str, sid: str, text: str) -> None:
        self._debounce_dir.mkdir(parents=True, exist_ok=True)
        p = self._debounce_path(chat_id, sid, text)
        p.touch()

    def _post(self, *, chat_id: str, text: str) -> None:
        import httpx  # lazy import — not available in all envs

        url = _TG_API.format(token=self._token)
        # The proxy is FLAKY: the same call can die on the TLS handshake and then
        # succeed a second later (observed 2026-07-13; board ad52d529). A single
        # attempt on a channel like that will silently drop real messages, so retry
        # with backoff and a fresh client each time — a wedged TLS connection must
        # not be reused. An HTTP error from Telegram itself is a real answer and is
        # NOT retried.
        last: Exception | None = None
        for attempt in range(_ATTEMPTS):
            if attempt:
                time.sleep(_BACKOFF_SEC * attempt)
            try:
                resp = httpx.post(
                    url,
                    json={"chat_id": chat_id, "text": text},
                    timeout=10,
                    proxy=self._proxy or None,
                )
            except httpx.TransportError as e:   # connect/read/TLS — retryable
                last = e
                log.warning("tg.send: transport error on attempt %d/%d via proxy %r: %s",
                            attempt + 1, _ATTEMPTS, self._proxy or "(none)", e)
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API error: {data}")
            log.info("tg.send: sent to chat %s (text len=%d)", chat_id, len(text))
            return
        raise RuntimeError(
            f"tg.send: FAILED {_ATTEMPTS}x to reach Telegram via proxy "
            f"{self._proxy or '(none — and direct egress is blocked on this host)'}; "
            f"message to chat {chat_id} was NOT delivered"
        ) from last


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _prefix(text: str, *, sid: str, user: str) -> str:
    """Add [<sid> @ <user>] or [<sid>] prefix when relevant."""
    if sid and user:
        return f"[{sid} @ {user}] {text}"
    if sid:
        return f"[{sid}] {text}"
    return text


# Quiet hours: never ping the user when they're asleep. Stakeholder is in
# UTC+5 (Tashkent). Default 17:00–05:00 UTC ≈ 22:00–10:00 local — wide enough
# to cover both early bedtime and late wake-up. Admin can override via
# system_settings.toml. Urgent=True bypasses.


def _in_quiet_hours(start_utc: int = 17, end_utc: int = 5) -> bool:
    """True if the current UTC hour falls in the stakeholder's sleep window.

    Disabled when env var ``BOT_SQUAD_DISABLE_QUIET_HOURS`` is set — used by
    the test suite, which exercises send paths without time-dependent
    skips.
    """
    import os
    if os.environ.get("BOT_SQUAD_DISABLE_QUIET_HOURS"):
        return False
    from datetime import datetime, timezone
    h = datetime.now(timezone.utc).hour
    if start_utc < end_utc:
        return start_utc <= h < end_utc
    # Wraps midnight: e.g. 17 -> 5 means 17..23 or 0..4
    return h >= start_utc or h < end_utc
