"""hub_record — the ONE record of which machine is the current Hub.

The Hub is always set up. It is not a per-link setting. This module stores
which origin plays the Hub, the public URL a new box dials, and the Hub cert
fingerprint. The Origins page shows the record, and a switch writes it. Minting
a one-time connect link (:mod:`mcp_loops.origin_connect`) reads it as the
default ``hub_url``.

Two modes:

* ``self``: THIS machine is the Hub. Its URL is ``$LOOPYARD_HUB_PUBLIC_URL``
  (e.g. ``wss://dashboard.nolimlabs.uk/hub``, the gate's ``/hub`` tunnel), and
  its fingerprint is read from the running Hub's persisted cert
  (``<hub-state>/_identity/hub-cert.pem``). A record-less box is ``self``.
* ``remote``: another origin's Hub. The owner gives its ``wss://`` URL and
  fingerprint, which are stored as given. The fingerprint is the out-of-band
  trust root a joining box pins, so a ``wss://`` remote Hub without one is
  refused.

The record lives beside the Hub's enrollment store (``<hub-state>/current.json``),
so the MCP server and the dashboard, which both see the Hub's state dir, agree.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

from mcp_loops import origin_onboard

RECORD_FILENAME = "current.json"
MODE_SELF = "self"
MODE_REMOTE = "remote"
_FP_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){31}$")
_ORIGIN_CAP = 128


class HubRecordError(ValueError):
    """A refused switch, with a stable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = 400


def _path(state_dir: Optional[str]) -> str:
    return os.path.join(origin_onboard.resolve_state_dir(state_dir),
                        RECORD_FILENAME)


def _read_raw(state_dir: Optional[str]) -> Optional[dict]:
    try:
        with open(_path(state_dir), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def local_fingerprint(state_dir: Optional[str] = None) -> Optional[str]:
    """The fingerprint of the cert THIS box's ``--tls`` Hub presents, read from
    the cert it persisted. ``None`` when no ``--tls`` Hub has run here."""
    from mcp_loops.origin_proto import tls as _tls
    root = origin_onboard.resolve_state_dir(state_dir)
    try:
        with open(origin_onboard._hub_cert_path(root), "rb") as fh:
            return _tls.cert_fingerprint(fh.read())
    except OSError:
        return None


def _host() -> str:
    import socket
    return socket.gethostname()


def _check_url(url: str) -> str:
    """Parse + canonicalize a Hub URL, with the same refusals a mint applies."""
    from mcp_loops import origin_client
    try:
        hub = origin_client.parse_hub_url(url)
    except origin_client.OriginUpError as e:
        raise HubRecordError(e.code, e.message) from e
    if not hub.secure and not hub.loopback:
        raise HubRecordError("insecure_hub_url",
                             f"{hub.describe()} is plaintext and not loopback; "
                             f"a box refuses to pair over it. Use wss://")
    return hub.describe()


def current_hub(state_dir: Optional[str] = None) -> dict:
    """The current Hub, resolved for display and for minting.

    Returns ``{mode, originId, host, publicUrl, fingerprint, configured,
    updatedAt, reason?}``. ``configured`` is true when a box could pair against
    it now: a URL is known, and a ``wss://`` Hub has a fingerprint."""
    rec = _read_raw(state_dir) or {}
    mode = rec.get("mode") if rec.get("mode") in (MODE_SELF, MODE_REMOTE) \
        else MODE_SELF
    if mode == MODE_REMOTE:
        out = {"mode": MODE_REMOTE, "originId": rec.get("originId"),
               "host": rec.get("host"), "publicUrl": rec.get("publicUrl"),
               "fingerprint": rec.get("fingerprint")}
    else:
        env_url = (os.environ.get(origin_onboard.HUB_PUBLIC_URL_ENV) or "").strip()
        url = None
        if env_url:
            try:
                url = _check_url(env_url)
            except HubRecordError:
                url = None
        out = {"mode": MODE_SELF, "originId": "local", "host": _host(),
               "publicUrl": url, "fingerprint": local_fingerprint(state_dir)}
        if env_url and url is None:
            out["reason"] = (f"${origin_onboard.HUB_PUBLIC_URL_ENV}={env_url!r} "
                             f"is not a usable Hub URL")
    url = out["publicUrl"]
    secure = bool(url) and url.lower().startswith("wss://")
    out["configured"] = bool(url) and (bool(out["fingerprint"]) or not secure)
    if not out["configured"] and "reason" not in out:
        if not url:
            out["reason"] = (f"no public Hub URL: set "
                             f"${origin_onboard.HUB_PUBLIC_URL_ENV} on this "
                             f"machine" if mode == MODE_SELF else
                             "the remote Hub record has no URL")
        else:
            out["reason"] = ("no Hub certificate yet: start the Hub with --tls"
                             if mode == MODE_SELF else
                             "the remote Hub record has no fingerprint")
    out["updatedAt"] = rec.get("updatedAt")
    return out


def set_hub(*, mode: str, public_url: Optional[str] = None,
            fingerprint: Optional[str] = None, origin_id: Optional[str] = None,
            host: Optional[str] = None, state_dir: Optional[str] = None,
            now: Optional[float] = None) -> dict:
    """Switch the current Hub. ``mode='self'`` points it back at this machine
    (URL + fingerprint come from the env and the running Hub). ``mode='remote'``
    points it at another origin's Hub and needs its ``public_url`` and, for
    ``wss://``, its ``fingerprint``. Returns :func:`current_hub`."""
    t = time.time() if now is None else float(now)
    if mode == MODE_SELF:
        rec = {"mode": MODE_SELF, "updatedAt": t}
    elif mode == MODE_REMOTE:
        url = _check_url((public_url or "").strip())
        fp = (fingerprint or "").strip().lower() or None
        if fp is not None and not _FP_RE.match(fp):
            raise HubRecordError("bad_fingerprint",
                                 "fingerprint must be a SHA-256 as 32 "
                                 "colon-separated hex pairs")
        if url.startswith("wss://") and not fp:
            raise HubRecordError("fingerprint_required",
                                 "a wss:// Hub needs its certificate "
                                 "fingerprint (the value its host prints as "
                                 "HUB_LISTENING … fingerprint=…)")
        oid = (origin_id or "").strip()[:_ORIGIN_CAP] or None
        rec = {"mode": MODE_REMOTE, "publicUrl": url, "fingerprint": fp,
               "originId": oid,
               "host": (host or "").strip()[:_ORIGIN_CAP] or oid,
               "updatedAt": t}
    else:
        raise HubRecordError("bad_mode", "mode must be 'self' or 'remote'")
    path = _path(state_dir)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.replace(tmp, path)
    return current_hub(state_dir)


def default_hub_url(state_dir: Optional[str] = None) -> Optional[str]:
    """The Hub URL a mint uses when the caller names none: the current Hub's."""
    return current_hub(state_dir).get("publicUrl")


#: where THIS machine's Hub listens (the Hub service). The dashboard gate's
#: /hub tunnel reads the same variable, so the probe checks what the tunnel dials.
LOCAL_HUB_ENV = "LOOPYARD_HUB_UPSTREAM"
LOCAL_HUB_DEFAULT = ("127.0.0.1", 8779)


def _local_listen() -> tuple:
    raw = (os.environ.get(LOCAL_HUB_ENV) or "").strip()
    host, _, port = raw.rpartition(":")
    try:
        return (host.strip("[]") or LOCAL_HUB_DEFAULT[0]), int(port)
    except ValueError:
        return LOCAL_HUB_DEFAULT


def probe(rec: dict, *, timeout: float = 2.0) -> dict:
    """``rec`` plus ``live`` (something accepts on the Hub's port: the local
    listener for ``self``, the public host for ``remote``) and ``checkedAt``."""
    import socket
    if rec.get("mode") == MODE_REMOTE:
        from mcp_loops import origin_client
        try:
            hub = origin_client.parse_hub_url(rec.get("publicUrl") or "")
            target = (hub.host, hub.port)
        except origin_client.OriginUpError:
            target = None
    else:
        target = _local_listen()
    live = False
    if target is not None:
        try:
            with socket.create_connection(target, timeout=timeout):
                live = True
        except OSError:
            live = False
    return dict(rec, live=live, checkedAt=time.time())


def status(state_dir: Optional[str] = None, *, live: bool = True) -> dict:
    """The current Hub for the UI / ``origin_hub_status``: the record, plus the
    live probe unless ``live=False``."""
    cur = current_hub(state_dir)
    return probe(cur) if live else cur
