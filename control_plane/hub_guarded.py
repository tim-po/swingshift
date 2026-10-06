"""§6a cutover: serve the real hub dashboard behind :class:`hub_guard.HubGuard`.

`yard enroll` (mcp_loops/yard.py) runs register -> claim -> provision, writes
``<hub state>/enroll.json`` (0600: portal_url, hub_id, device_token,
fingerprint, public_url, state_version) and launches, as a recorded service::

    python -m control_plane.hub_guarded --state-dir D --port P \\
        [--host 127.0.0.1] [--tls-cert C --tls-key K]

which serves ``tracking_ui.loops_dashboard.app`` wrapped by HubGuard over
``D/dashboard-devices.json`` and runs :class:`PhoneHome` beside it. Every
``sync_interval`` seconds (default 30, inside the portal's 90 s hub TTL) it:

* heartbeats ``POST /api/hubs/{id}/heartbeat`` so the portal shows ONLINE;
* pulls ``GET /api/hubs/{id}/device-hashes`` into ``Allowlist.sync``, so a
  portal-side revocation reaches the hub within one interval even if nobody
  ever misses at /_lyp/enter.

Viewing stays offline: an unreachable portal keeps the cached allowlist. A
401/403 on device-hashes means the portal no longer honours THIS hub's own
credential (device revoked / account disabled), so the allowlist is emptied —
fail closed rather than keep admitting on a credential the owner killed.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .hub_guard import Allowlist, HubGuard

ALLOWLIST = "dashboard-devices.json"
ENROLLMENT = "enroll.json"
SYNC_INTERVAL = 30.0
_FIELDS = ("portal_url", "hub_id", "device_token")


class CredentialRejected(Exception):
    """The portal answered 401/403 to this hub's own device token."""


def load_enrollment(state_dir) -> dict:
    """enroll.json, refusing anything but a private regular file with the fields."""
    path = Path(state_dir) / ENROLLMENT
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise ValueError(f"{path} must be a 0600 regular file")
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict) or any(not isinstance(doc.get(k), str) or not doc[k] for k in _FIELDS):
        raise ValueError(f"{path} is missing portal_url/hub_id/device_token")
    if type(doc.get("state_version")) is not int or doc["state_version"] < 0:
        raise ValueError(f"{path} has no valid state_version")
    _check_portal(doc["portal_url"])
    return doc


def _check_portal(url: str) -> None:
    p = urlsplit(url)
    loopback_dev = (os.environ.get("LOOPYARD_ALLOW_INSECURE") == "1" and p.scheme == "http"
                    and p.hostname in ("localhost", "127.0.0.1"))
    if (p.scheme != "https" and not loopback_dev) or p.username or p.password or p.query or p.fragment:
        raise ValueError("portal URL must be HTTPS without credentials/query/fragment")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # the bearer only ever goes to the portal itself
        return None


def http_transport(timeout: float = 10.0) -> Callable:
    """(method, url, token, body|None) -> parsed JSON; raises CredentialRejected on 401/403."""
    opener = urllib.request.build_opener(_NoRedirect)

    def call(method, url, token, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            "authorization": f"Bearer {token}", "accept": "application/json",
            "user-agent": "Loopyard/0.1",
            **({"content-type": "application/json"} if data is not None else {})})
        try:
            with opener.open(req, timeout=timeout) as resp:  # noqa: S310 (https portal, checked)
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise CredentialRejected(exc.code) from None
            raise
    return call


class PhoneHome:
    """Periodic heartbeat + device-hashes sync for one enrolled hub."""

    def __init__(self, allowlist: Allowlist, *, portal_url: str, hub_id: str, hub_token: str,
                 state_version: int, interval: float = SYNC_INTERVAL, transport: Callable | None = None):
        _check_portal(portal_url)
        self.allowlist, self.interval = allowlist, float(interval)
        self.hub_id, self._token, self.state_version = hub_id, hub_token, state_version
        self._base = f"{portal_url.rstrip('/')}/api/hubs/{hub_id}"
        self._call = transport or http_transport()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict = {}

    def refresh(self):
        """HubGuard's enter-miss refresh= and the periodic pull: the CP's active hashes."""
        return self._call("GET", self._base + "/device-hashes", self._token)["hashes"]

    def tick(self) -> dict:
        """One heartbeat + sync. Never raises: the dashboard must outlive portal outages."""
        out = {"heartbeat": False, "synced": False, "rejected": False}
        try:
            self._call("POST", self._base + "/heartbeat", self._token, {"state_version": self.state_version})
            out["heartbeat"] = True
        except CredentialRejected:
            out["rejected"] = True
        except Exception:  # noqa: BLE001  (offline / 409: keep serving; next tick retries)
            pass
        try:
            hashes = self.refresh()
            if not isinstance(hashes, list):
                raise ValueError("device-hashes: list expected")
            self.allowlist.sync(hashes)
            out["synced"] = True
        except CredentialRejected:
            self.allowlist.sync([])  # our own device was revoked on the portal: admit nobody
            out["rejected"] = out["synced"] = True
        except Exception:  # noqa: BLE001  (CP unreachable: cached allowlist stays authoritative)
            pass
        self.last = out
        return out

    def _run(self):
        while True:
            self.tick()
            if self._stop.wait(self.interval):
                return

    def start(self) -> "PhoneHome":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hub-phone-home", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


def guarded_app(dashboard_app, state_dir, *, portal_url: str, hub_id: str, hub_token: str,
                state_version: int = 0, sync_interval: float = SYNC_INTERVAL,
                transport: Callable | None = None, secure_cookie: bool = True) -> HubGuard:
    """HubGuard(dashboard_app) over state_dir's allowlist; ``.phone_home`` is not yet started."""
    allowlist = Allowlist(Path(state_dir) / ALLOWLIST)
    phone = PhoneHome(allowlist, portal_url=portal_url, hub_id=hub_id, hub_token=hub_token,
                      state_version=state_version, interval=sync_interval, transport=transport)
    from urllib.parse import quote
    handoff = portal_url.rstrip("/") + "/#/open/" + quote(hub_id, safe="")
    guard = HubGuard(dashboard_app, allowlist, refresh=phone.refresh, portal_url=handoff,
                     secure_cookie=secure_cookie)
    guard.phone_home = phone
    return guard


def from_state(dashboard_app, state_dir, **kw) -> HubGuard:
    doc = load_enrollment(state_dir)
    return guarded_app(dashboard_app, state_dir, portal_url=doc["portal_url"], hub_id=doc["hub_id"],
                       hub_token=doc["device_token"], state_version=doc["state_version"], **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m control_plane.hub_guarded",
                                 description="Serve the hub dashboard behind the device-token guard.")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--tls-cert")
    ap.add_argument("--tls-key")
    ap.add_argument("--sync-interval", type=float, default=SYNC_INTERVAL)
    a = ap.parse_args(argv)
    if bool(a.tls_cert) != bool(a.tls_key):
        ap.error("--tls-cert and --tls-key go together")
    if not 0 < a.sync_interval <= 60:
        ap.error("--sync-interval must be in (0, 60] to stay ONLINE on the portal")
    try:
        doc = load_enrollment(a.state_dir)
    except (OSError, ValueError) as exc:
        print(f"hub_guarded: not enrolled ({type(exc).__name__}: {exc}); run `yard enroll`", file=sys.stderr)
        return 3
    import uvicorn
    from tracking_ui.loops_dashboard import app as dashboard

    guard = guarded_app(dashboard, a.state_dir, portal_url=doc["portal_url"], hub_id=doc["hub_id"],
                        hub_token=doc["device_token"], state_version=doc["state_version"],
                        sync_interval=a.sync_interval, secure_cookie=bool(a.tls_cert))
    guard.phone_home.start()
    print(f"hub_guarded: dashboard behind device-token guard on "
          f"{'https' if a.tls_cert else 'http'}://{a.host}:{a.port} (hub {doc['hub_id']})", flush=True)
    try:
        uvicorn.run(guard, host=a.host, port=a.port, log_level="warning",
                    ssl_certfile=a.tls_cert, ssl_keyfile=a.tls_key)
    finally:
        guard.phone_home.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
