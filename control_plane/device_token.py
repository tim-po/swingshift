"""§6a device-bound tokens: issued by the portal, accepted by the user's hub.

A token is "lyd_" + 256 random bits, shown to the device exactly once; only its
sha256 hex is persisted (Store.put_device_token). The same credential serves:
  * the hub dashboard, offline — hub_guard.HubGuard over a local hash allowlist,
    no CP round trip (the hub syncs hashes via /api/hubs/{id}/device-hashes);
  * the stack's phone-home to the CP directory — make_hub_authenticator() as
    build_app(authenticate_hub=...). A hub-bound token may act only on its hub
    (same id, same fingerprint); an unbound token is the account's enrollment
    credential and may register a new hub;
  * the desktop update check — releases.py lets it read release.json (the
    manifest only; assets stay behind a session or lyr_ download token).
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import secrets
import time
from typing import Iterable

from starlette.responses import JSONResponse
from starlette.routing import Route

PREFIX = "lyd_"
MAX_PER_LABEL = 10  # live hub-bound tokens kept per (hub, device_label); older are revoked


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(store, account_id: str, hub_id: str | None, device_label: str) -> tuple[str, str]:
    """(token_id, plaintext). Raises ValueError for inactive accounts / foreign hubs."""
    label = (device_label or "").strip()[:80]
    if not label:
        raise ValueError("device label required")
    token = PREFIX + secrets.token_urlsafe(32)
    token_id = store.put_device_token(account_id, hub_id, label, token_hash(token))
    if hub_id is not None:  # each portal handoff mints one; keep the set bounded
        store.prune_device_tokens(account_id, hub_id, label, MAX_PER_LABEL)
    return token_id, token


def verify(store, token: str):
    """(DeviceToken, Account) for a live token of an active account, else None."""
    if not isinstance(token, str) or not token.startswith(PREFIX) or len(token) > 200:
        return None
    row = store.device_token_by_hash(token_hash(token))
    if row is None or row.revoked:
        return None
    account = store.get_account(row.account_id)
    if account is None or account.status != "active":
        return None
    if row.hub_id is not None:
        hub = store.get_hub(row.hub_id)
        if hub is None or hub.account_id != account.id:
            return None
    return row, account


def hub_accepts(token: str, allowed_hashes: Iterable[str]) -> bool:
    """Offline check for the hub dashboard against its locally provisioned hashes."""
    if not isinstance(token, str) or not token.startswith(PREFIX):
        return False
    h = token_hash(token)
    ok = False
    for allowed in allowed_hashes:  # no early exit: constant work per list
        ok |= hmac.compare_digest(h, str(allowed))
    return ok


def bearer(request) -> str:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def make_hub_authenticator(store):
    """build_app(authenticate_hub=...): Bearer device token -> Account | None."""
    async def authenticate_hub(request):
        hit = verify(store, bearer(request))
        if hit is None:
            return None
        row, account = hit
        if row.hub_id is None:
            # Enrollment credential: may register, not steer existing hubs. One
            # minted by onboarding dies with its enrollment (TTL / once claimed).
            ob = store.enrollment_by_token(row.id)
            if ob is not None and (ob.claimed_at is not None or not ob.created_at <= time.time() < ob.expires_at):
                return None
            return account if request.url.path == "/api/hubs/register" else None
        target = request.path_params.get("hub_id")
        if target is not None:
            return account if target == row.hub_id else None
        if request.url.path == "/api/hubs/register":
            try:
                body = await request.json()  # Starlette caches the body for the endpoint
            except ValueError:
                return None
            hub = store.get_hub(row.hub_id)
            fp = body.get("fingerprint") if isinstance(body, dict) else None
            return account if hub and isinstance(fp, str) and hmac.compare_digest(fp, hub.fingerprint) else None
        return account  # read-only listing of the account's own hubs
    return authenticate_hub


async def issue_route(request):
    """POST /api/devices {device_label, hub_id?} -> plaintext token, once."""
    app = request.app
    if request.headers.get("origin", "").rstrip("/") != app.state.cfg.portal_url.rstrip("/"):
        return JSONResponse({"error": "invalid origin"}, status_code=403)
    account = app.state.authenticate(request)
    if inspect.isawaitable(account):
        account = await account
    if not account:
        return JSONResponse({"error": "authentication required"}, status_code=401)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("JSON object required")
        hub_id = body.get("hub_id")
        if hub_id is not None and not isinstance(hub_id, str):
            raise ValueError("hub_id must be a string")
        token_id, token = issue(app.state.store, account.id, hub_id, str(body.get("device_label", "")))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"id": token_id, "token": token, "hub_id": hub_id},
                        status_code=201, headers={"cache-control": "no-store"})


async def hub_hashes_route(request):
    """GET /api/hubs/{hub_id}/device-hashes (Bearer: that hub's own device token).

    The hub's allowlist sync (hub_guard.cp_refresher): hashes of every live
    token bound to this hub, so portal-minted browser tokens appear and revoked
    ones drop out. Hashes only; plaintext never leaves the device it was shown to.
    """
    store = request.app.state.store
    hit = verify(store, bearer(request))
    hub_id = request.path_params["hub_id"]
    if hit is None or hit[0].hub_id is None or not hmac.compare_digest(hit[0].hub_id, hub_id):
        return JSONResponse({"error": "authentication required"}, status_code=401)
    hashes = sorted(t.token_hash for t in store.list_device_tokens(hit[1].id)
                    if t.hub_id == hub_id and not t.revoked)
    return JSONResponse({"hub_id": hub_id, "hashes": hashes}, headers={"cache-control": "no-store"})


routes = [Route("/api/devices", issue_route, methods=["POST"]),
          Route("/api/hubs/{hub_id}/device-hashes", hub_hashes_route, methods=["GET"])]
