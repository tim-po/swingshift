"""§6a on the hub: admit only this account's device tokens to the hub dashboard.

Runs on the USER'S hub, not the control plane. Wrap any dashboard ASGI app:

    app = HubGuard(dashboard_app, Allowlist(state_dir / "dashboard-devices.json"),
                   refresh=cp_refresher(portal_url, hub_id, hub_token))

* Allowlist — a hub-local 0600 JSON file of sha256 token hashes (+ revoked
  hashes). The onboarding claim provisions the hub's own token into it
  (``Allowlist.add``); ``sync`` replaces the active set with the CP's view so
  revocations propagate. Viewing never calls the CP: a missing, corrupt or
  loosely-permissioned file admits nobody (fail closed).
* A request is admitted with ``Authorization: Bearer lyd_…`` or the httponly
  ``lyh_dev`` cookie, which the one-time ``/_lyp/enter`` exchange sets. The
  token reaches /_lyp/enter in a URL fragment (read by the page's script, then
  POSTed) or a pasted form field — never a query string, so never in access
  logs or Referer.
* ``refresh`` (optional) is consulted only when /_lyp/enter sees an unknown
  token — e.g. a browser token the portal minted moments ago — rate-limited.

Cutover: control_plane/hub_guarded.py wraps the real hub dashboard
(tracking_ui.loops_dashboard) in HubGuard and runs a periodic heartbeat +
device-hashes ``sync`` (``python -m control_plane.hub_guarded``, launched by
``yard enroll``), so revocation propagates without an enter-miss.
"""
from __future__ import annotations

import asyncio
import html
import inspect
import json
import os
import secrets
import stat
import threading
import time
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs

from . import device_token

ENTER_PATH = "/_lyp/enter"
COOKIE = "lyh_dev"
COOKIE_TTL = 30 * 24 * 3600
MAX_REVOKED = 256  # tombstones kept; older ones are pruned (sync is authoritative)
_HEX = frozenset("0123456789abcdef")


def _is_hash(h) -> bool:
    return isinstance(h, str) and len(h) == 64 and set(h) <= _HEX


class Allowlist:
    """Hub-local device-token hashes, reloaded when the file changes."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp = None
        self._active: frozenset[str] = frozenset()
        self._revoked: frozenset[str] = frozenset()
        self._revoked_seq: tuple[str, ...] = ()

    # ---------------------------------------------------------------- read
    def _load(self) -> tuple[frozenset, frozenset]:
        try:
            st = os.stat(self.path)
        except OSError:
            return frozenset(), frozenset()
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode)
        with self._lock:
            if stamp == self._stamp:
                return self._active, self._revoked
            active, revoked, seq = frozenset(), frozenset(), ()
            if stat.S_ISREG(st.st_mode) and not st.st_mode & 0o077:
                try:
                    doc = json.loads(self.path.read_text())
                    seq = tuple(h for h in doc.get("revoked", []) if _is_hash(h))
                    revoked = frozenset(seq)
                    active = frozenset(h for h in doc.get("hashes", []) if _is_hash(h)) - revoked
                except (OSError, ValueError, AttributeError, TypeError):
                    active, revoked, seq = frozenset(), frozenset(), ()
            self._stamp, self._active, self._revoked, self._revoked_seq = stamp, active, revoked, seq
            return active, revoked

    def hashes(self) -> frozenset[str]:
        return self._load()[0]

    def accepts(self, token: str) -> bool:
        return bool(token) and device_token.hub_accepts(token, self.hashes())

    # --------------------------------------------------------------- write
    def _write(self, active: Iterable[str], revoked: Iterable[str]) -> None:
        """``revoked`` is oldest-first; only the newest MAX_REVOKED tombstones are kept.

        Dropping a tombstone never re-admits anything: admission is ``hashes``
        only, and every sync replaces ``hashes`` with the CP's live set.
        """
        active = set(active)
        revoked = [h for h in dict.fromkeys(revoked) if h not in active][-MAX_REVOKED:]
        doc = {"version": 1, "hashes": sorted(active - set(revoked)), "revoked": revoked}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(doc, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def add(self, token: str) -> None:
        """Provision a plaintext device token (the claim step on the machine)."""
        self.add_hash(device_token.token_hash(token))

    def _seq(self) -> tuple[frozenset, list]:
        active, _ = self._load()
        return active, list(self._revoked_seq)

    def add_hash(self, h: str) -> None:
        if not _is_hash(h):
            raise ValueError("sha256 hex digest required")
        active, seq = self._seq()
        self._write(active | {h}, seq)

    def revoke_hash(self, h: str) -> None:
        active, seq = self._seq()
        self._write(active - {h}, [*seq, h])

    def sync(self, hashes: Iterable[str]) -> None:
        """Adopt the CP's active set; anything dropped from it becomes revoked."""
        new = {h for h in hashes if _is_hash(h)}
        active, seq = self._seq()
        if new == active:
            return  # periodic sync: don't rewrite (and re-stamp) an unchanged file
        self._write(new, [*seq, *sorted(active - new)])


def provision(allowlist_path, device_token_plaintext: str) -> None:
    """What the bring-up does with the claim response's ``device_token``."""
    Allowlist(allowlist_path).add(device_token_plaintext)


def cp_refresher(portal_url: str, hub_id: str, hub_token: str, timeout: float = 5.0) -> Callable:
    """refresh= for HubGuard: fetch this hub's active hashes from the CP."""
    url = f"{portal_url.rstrip('/')}/api/hubs/{hub_id}/device-hashes"

    def refresh():
        req = urllib.request.Request(url, headers={"authorization": f"Bearer {hub_token}",
                                                   "accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https portal)
            return json.loads(resp.read())["hashes"]
    return refresh


# ------------------------------------------------------------------ ASGI
def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _cookie(headers, name) -> str:
    jar = SimpleCookie()
    try:
        jar.load(headers.get("cookie", ""))
    except Exception:
        return ""
    return jar[name].value if name in jar else ""


def _bearer(headers) -> str:
    scheme, _, value = headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


async def _respond(send, status: int, body: bytes, ctype: str, extra=()) -> None:
    hdrs = [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"), *extra]
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": body})


_ENTER_JS = """
(function(){var m=/[#&]t=([^&]+)/.exec(location.hash);if(!m){var p=document.getElementById('portal-open');if(p)location.replace(p.href);return;}
history.replaceState(null,'',location.pathname);
var s=document.getElementById('s');s.textContent='Signing you in\\u2026';
fetch(location.pathname,{method:'POST',credentials:'same-origin',
headers:{'content-type':'application/json'},body:JSON.stringify({token:decodeURIComponent(m[1])})})
.then(function(r){if(r.ok){location.replace('/');}else{s.textContent=
'This link is not valid for this hub. Open your dashboard again from the portal, or paste this device\\u2019s token below.';}})
.catch(function(){s.textContent='Could not reach your hub. Try again.';});})();
"""


def _enter_page(nonce: str, portal_url: str, message: str = "") -> bytes:
    portal = (f'<p><a id=portal-open href="{html.escape(portal_url)}">Open your dashboard from the Loopyard portal</a></p>'
              if portal_url else "")
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Loopyard · sign in to this hub</title>
<style>body{{font:15px system-ui;max-width:28rem;margin:4rem auto;padding:0 1rem}}input{{width:100%}}</style>
<h1>Sign in to your hub</h1><p id=s role=status>{html.escape(message)}</p>{portal}
<form method=post><label>Device token<input name=token type=password autocomplete=off required></label>
<button>Sign in</button></form><script nonce="{nonce}">{_ENTER_JS}</script>""".encode()


class HubGuard:
    """ASGI middleware: every HTTP/WebSocket request needs a live device token."""

    def __init__(self, app, allowlist: Allowlist, *, refresh: Callable | None = None,
                 refresh_interval: float = 10.0, portal_url: str = "", secure_cookie: bool = True,
                 clock: Callable[[], float] = time.monotonic):
        self.app, self.allowlist, self.refresh = app, allowlist, refresh
        self.refresh_interval, self.portal_url = refresh_interval, portal_url
        self.secure_cookie, self._clock = secure_cookie, clock
        self._last_refresh = float("-inf")

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = _headers(scope)
        if kind == "http" and scope["path"] == ENTER_PATH:
            return await self._enter(scope, receive, send, headers)
        token = _bearer(headers) or _cookie(headers, COOKIE)
        if self.allowlist.accepts(token):
            return await self.app(scope, receive, send)
        if kind == "websocket":
            return await send({"type": "websocket.close", "code": 4401})
        if "text/html" in headers.get("accept", ""):
            nonce = secrets.token_urlsafe(12)
            return await _respond(send, 401, _enter_page(nonce, self.portal_url), "text/html; charset=utf-8",
                                  [(b"content-security-policy", _csp(nonce))])
        await _respond(send, 401, b'{"error":"device token required"}', "application/json")

    async def _maybe_refresh(self) -> None:
        now = self._clock()
        if self.refresh is None or now - self._last_refresh < self.refresh_interval:
            return
        self._last_refresh = now
        try:
            if inspect.iscoroutinefunction(self.refresh):
                got = await self.refresh()
            else:
                got = await asyncio.to_thread(self.refresh)  # cp_refresher blocks on urllib
            if got is not None:
                await asyncio.to_thread(self.allowlist.sync, list(got))
        except Exception:
            pass  # CP unreachable: keep the cached allowlist (offline-capable)

    async def _enter(self, scope, receive, send, headers):
        method = scope["method"]
        if method in ("GET", "HEAD"):
            nonce = secrets.token_urlsafe(12)
            return await _respond(send, 200, _enter_page(nonce, self.portal_url), "text/html; charset=utf-8",
                                  [(b"content-security-policy", _csp(nonce))])
        if method != "POST":
            return await _respond(send, 405, b'{"error":"method not allowed"}', "application/json")
        if not _same_origin(scope, headers):
            return await _respond(send, 403, b'{"error":"invalid origin"}', "application/json")
        body = await _read_body(receive, limit=4096)
        is_json = headers.get("content-type", "").startswith("application/json")
        token = _token_from(body, is_json)
        ok = self.allowlist.accepts(token)
        if not ok and token.startswith(device_token.PREFIX):
            await self._maybe_refresh()
            ok = self.allowlist.accepts(token)
        if not ok:
            if is_json:
                return await _respond(send, 401, b'{"error":"invalid device token"}', "application/json")
            nonce = secrets.token_urlsafe(12)
            return await _respond(send, 401, _enter_page(nonce, self.portal_url, "That token is not valid for this hub."),
                                  "text/html; charset=utf-8", [(b"content-security-policy", _csp(nonce))])
        attrs = f"{COOKIE}={token}; Path=/; Max-Age={COOKIE_TTL}; HttpOnly; SameSite=Lax"
        if self.secure_cookie:
            attrs += "; Secure"
        cookie = (b"set-cookie", attrs.encode())
        if is_json:
            return await _respond(send, 200, b'{"ok":true}', "application/json", [cookie])
        await send({"type": "http.response.start", "status": 303,
                    "headers": [(b"location", b"/"), (b"cache-control", b"no-store"), cookie,
                                (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})


def _csp(nonce: str) -> bytes:
    return (f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
            f"connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'").encode()


def _same_origin(scope, headers) -> bool:
    origin = headers.get("origin", "")
    host = headers.get("host", "")
    if not origin or not host:
        return False
    return origin.split("://", 1)[-1].rstrip("/") == host


async def _read_body(receive, limit: int) -> bytes:
    chunks, size = [], 0
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            break
        part = msg.get("body", b"")
        size += len(part)
        if size > limit:
            return b""
        chunks.append(part)
        if not msg.get("more_body"):
            break
    return b"".join(chunks)


def _token_from(body: bytes, is_json: bool) -> str:
    try:
        if is_json:
            doc = json.loads(body or b"{}")
            tok = doc.get("token") if isinstance(doc, dict) else None
        else:
            tok = (parse_qs(body.decode("utf-8", "replace")).get("token") or [None])[0]
    except ValueError:
        return ""
    tok = tok.strip() if isinstance(tok, str) else ""
    # Cookie-safe alphabet only (token_urlsafe), so the Set-Cookie value can't be injected.
    return tok if len(tok) <= 200 and all(c.isalnum() or c in "-_" for c in tok) else ""
