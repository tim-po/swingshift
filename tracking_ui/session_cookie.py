"""Pure HMAC session-cookie codec shared by the dashgate and the control plane.

Cookie value: "<uid>.<exp>.<sid>.<hmac-sha256-hex>". Verification is
constant-time and checks expiry only; uid pinning and sid revocation are the
caller's policy (gate.py pins Tim's uid, control_plane.auth checks its store).
Stdlib only, no side effects on import.
"""
from __future__ import annotations

import hashlib
import hmac
import time


def _mac(uid: str, exp: int, sid: str, secret: str) -> str:
    return hmac.new(secret.encode(), f"{uid}.{exp}.{sid}".encode(), hashlib.sha256).hexdigest()


def sign(uid: str, exp: int, sid: str, secret: str) -> str:
    uid, sid = str(uid), str(sid)
    if not uid or not sid or "." in uid or "." in sid:
        raise ValueError("uid and sid must be non-empty and dot-free")
    if not secret:
        raise ValueError("secret required")
    return f"{uid}.{int(exp)}.{sid}.{_mac(uid, int(exp), sid, secret)}"


def parse(value: str, secret: str, now: float | None = None) -> tuple[str, int, str] | None:
    """(uid, exp, sid) of a correctly-signed, unexpired cookie, else None."""
    if not secret:
        return None
    try:
        uid, exp_s, sid, mac = value.split(".")
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return None
    if not uid or not sid or exp < (time.time() if now is None else now):
        return None
    if not hmac.compare_digest(_mac(uid, exp, sid, secret), mac):
        return None
    return uid, exp, sid
