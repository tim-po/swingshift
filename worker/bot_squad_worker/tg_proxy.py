"""Proxy resolution for the personal-account Telethon (MTProto) client.

Telethon speaks raw MTProto over TCP. Unlike httpx it does **not** honor the
``HTTPS_PROXY`` / ``ALL_PROXY`` environment variables. On hosts where Telegram
is DPI-blocked on the default route (e.g. Almaty/KZ) the only way out is the
local Xray HTTP proxy — so the client must be handed an explicit ``proxy=``
tuple built from python-socks.

This module reads the same proxy env the rest of the worker uses (set by
``bin/worker-tg-proxy-exec.sh`` / ``bin/tg-bridge-proxy-exec.sh``) and converts
``http://HOST:PORT`` into the python-socks tuple Telethon expects. On hosts
with no proxy env set it returns ``None`` so the client connects directly.
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlparse


log = logging.getLogger("tg_user")

# Proxy env vars, in precedence order (first one set wins). ALL_PROXY is the
# broadest so it leads; we check both upper- and lower-case spellings since
# different tools export different casings.
_PROXY_ENV_VARS = (
    "ALL_PROXY",
    "all_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
)


def telethon_proxy_from_env() -> Optional[tuple]:
    """Build a Telethon ``proxy=`` tuple from the worker's proxy env.

    Returns ``(python_socks.ProxyType.HTTP, host, port)`` for the first
    ``ALL_PROXY/HTTPS_PROXY/HTTP_PROXY`` (upper or lower case) that is set and
    parseable as ``http://HOST:PORT``. Returns ``None`` when no proxy env is
    set (direct connection — correct for non-blocked hosts) or when the value
    cannot be parsed into a host+port.
    """
    raw = ""
    for name in _PROXY_ENV_VARS:
        val = os.environ.get(name)
        if val:
            raw = val.strip()
            break

    if not raw:
        log.info("tg-proxy: no proxy env set — Telethon will connect directly")
        return None

    # urlparse needs a scheme to populate hostname/port; default to http://.
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        log.warning("tg-proxy: could not parse proxy %r — connecting directly", raw)
        return None

    from python_socks import ProxyType

    log.info("tg-proxy: routing Telethon (MTProto) via HTTP proxy %s:%d", host, port)
    return (ProxyType.HTTP, host, int(port))
