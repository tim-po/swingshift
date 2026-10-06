"""Optional owner-Telegram client (Phase-B D8 / R16).

``mcp_telegram`` is bot-swarm code, not a Loopyard dependency: it reaches the
dev box's venv through ``bot-swarm.pth`` and is never bundled. Every engine-side
Telegram call goes through :func:`tg_client`, which returns ``None`` — and logs
ONE line per process — when the module is absent, instead of failing (and
logging) on every owner ping.
"""

from __future__ import annotations

import importlib.util
import threading
from typing import Any, Optional

DISABLED_MSG = "[loops] owner Telegram disabled: mcp_telegram not installed"
SKIPPED = "mcp_telegram not installed"

_warned = False
_lock = threading.Lock()


def available() -> bool:
    """True when ``mcp_telegram`` is importable; logs the disabled line once."""
    global _warned
    try:
        found = importlib.util.find_spec("mcp_telegram") is not None
    except (ImportError, ValueError):
        found = False
    if not found:
        with _lock:
            if not _warned:
                _warned = True
                print(DISABLED_MSG)
    return found


def tg_client(bot: str) -> Optional[Any]:
    """A ``TgMCP`` for ``bot``, or ``None`` when ``mcp_telegram`` is absent."""
    if not available():
        return None
    from mcp_telegram.client import TgMCP
    return TgMCP(bot=bot)
