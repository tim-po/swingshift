"""Telegram owner-channel for the loop runner (part c).

:class:`TelegramOwnerChannel` satisfies the engine's
:data:`~mcp_loops.runner.OwnerChannel` contract — ``owner(question) -> reply`` —
over the existing mcp-telegram stack: the question goes out with ``tg_send``,
and the channel then BLOCKS on ``tg_get_updates`` until the owner answers in
that chat. Each call is one exchange; the runner calls it again for every
follow-up (the manager's multi-message briefing and mid-run ``ask_owner``
pauses), so multi-turn back-and-forth is just repeated calls on the same
channel — the update offset carries across calls.

Transport is injectable: production passes nothing and gets a
:class:`mcp_telegram.client.TgMCP` bound to ``bot``; tests pass a stub with the
same three methods (``send`` / ``get_updates`` / ``send_document``) — no live
network anywhere in the tests. Transport failures (TgMCPError — server down,
restart) are retried until ``reply_timeout``; a timeout returns a marked
string rather than raising, so a runner never dies waiting on a human.

:func:`send_loop_scheme` ships a loop's JSON config + freshly rendered HTML
block-scheme (render.py) to the owner as Telegram documents.
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable, Optional

from mcp_loops.render import render_to_file

TIMEOUT_REPLY = "(owner did not reply in time — proceed on your best judgment)"


class TelegramOwnerChannel:
    """Blocking owner ↔ manager relay over one Telegram chat.

    ``tg`` needs ``send(chat_id, text)``, ``get_updates(offset=, timeout=)``
    and ``send_document(chat_id, path, caption=)`` — exactly TgMCP's surface.
    ``clock``/``sleep`` are injectable so timeout tests run instantly.
    """

    def __init__(self, bot: str, chat_id, *, loop_name: str = "",
                 tg=None, reply_timeout: float = 3600.0,
                 poll_seconds: int = 25, retry_pause: float = 5.0,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        if tg is None:
            from mcp_loops import telegram_optional  # deferred: tests stay stub-only
            tg = telegram_optional.tg_client(bot)
            if tg is None:
                raise RuntimeError("TelegramOwnerChannel needs mcp_telegram, "
                                   "which is not installed (owner Telegram disabled)")
        self.tg = tg
        self.chat_id = str(chat_id)
        self.loop_name = loop_name
        self.reply_timeout = reply_timeout
        self.poll_seconds = poll_seconds
        self.retry_pause = retry_pause
        self.clock = clock
        self.sleep = sleep
        self._offset = 0
        self._primed = False

    # ── plumbing ──
    def _advance(self, updates: list[dict]) -> None:
        for u in updates:
            self._offset = max(self._offset, int(u.get("update_id", 0)) + 1)

    def _drain_stale(self) -> None:
        """Baseline the offset past anything the owner sent BEFORE the first
        question — an old message must never be consumed as a reply."""
        try:
            res = self.tg.get_updates(offset=self._offset, timeout=0)
            self._advance(res.get("updates", []))
        except Exception:  # noqa: BLE001 — transport down: baseline stays 0
            pass
        self._primed = True

    def _reply_text(self, update: dict) -> Optional[str]:
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id", "")) != self.chat_id:
            return None
        text = (msg.get("text") or "").strip()
        return text or None

    # ── OwnerChannel contract ──
    def __call__(self, question: str) -> str:
        if not self._primed:
            self._drain_stale()
        prefix = f"[loop {self.loop_name}] " if self.loop_name else ""
        self.tg.send(self.chat_id, prefix + question)
        deadline = self.clock() + self.reply_timeout
        while self.clock() < deadline:
            try:
                res = self.tg.get_updates(offset=self._offset,
                                          timeout=self.poll_seconds)
            except Exception:  # noqa: BLE001 — TgMCPError etc: retry until deadline
                self.sleep(self.retry_pause)
                continue
            updates = res.get("updates", [])
            self._advance(updates)
            for u in updates:
                text = self._reply_text(u)
                if text is not None:
                    return text
            if not updates:
                self.sleep(0)  # zero-length poll (stubbed tg): yield, no spin
        return TIMEOUT_REPLY


def send_loop_scheme(tg, chat_id, config_path: str, *,
                     html_path: Optional[str] = None) -> dict:
    """Send a loop's JSON config + rendered HTML block-scheme as documents.

    Renders the HTML fresh from the JSON (render.py) — the scheme the owner
    sees always matches the file they got. Returns
    {ok, json_path, html_path} or {"error": ...}; a failed send is reported,
    never raised (the caller is usually mid-turn).
    """
    try:
        with open(config_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        out_html = html_path or os.path.splitext(config_path)[0] + ".html"
        render_to_file(raw, out_html)
        name = raw.get("name") if isinstance(raw, dict) else None
        caption = f"loop config: {name}" if name else "loop config"
        r1 = tg.send_document(str(chat_id), config_path, caption=caption)
        r2 = tg.send_document(str(chat_id), out_html,
                              caption=caption + " — block-scheme")
        ok = bool(r1.get("ok")) and bool(r2.get("ok"))
        return {"ok": ok, "json_path": config_path, "html_path": out_html}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
