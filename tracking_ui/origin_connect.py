"""HTTP face of the one-time origin-connect link (:mod:`mcp_loops.origin_connect`).

Like the mac-origin bootstrap, ``GET /origin-connect/<token>`` is gate-exempt
and checks its token: the token IS the bearer, so an AI with no session can
fetch it. Unlike mac-origin, the token is minted, single-use and short-lived.
The endpoint returns a connect document (markdown, or ``?format=sh`` for a
runnable script), not a remote-shell channel. A refusal is fail-closed and
carries a distinct code and status (see ``REFUSAL_STATUS``).

The owner-side endpoints (mint / status / revoke) live under ``/api/loops/`` and
therefore stay behind the gate like every other dashboard API.

Env: ``LOOPYARD_ORIGIN_CONNECT_OWNER`` scopes the public endpoint to one owner.
A link minted for any other owner is refused ``wrong_owner``.
"""
from __future__ import annotations

import os

from starlette.responses import JSONResponse, Response

from mcp_loops import origin_connect as OC
from mcp_loops import hub_record, origin_onboard

OWNER_SCOPE_ENV = "LOOPYARD_ORIGIN_CONNECT_OWNER"
_NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex",
             "Referrer-Policy": "no-referrer"}
_OWNER_CAP = 128


def _scope_owner(request):
    """The owner this endpoint serves: the host scope, narrowed by ``?owner=``.
    Either one can only cause a refusal; neither grants access."""
    env = (os.environ.get(OWNER_SCOPE_ENV) or "").strip() or None
    q = (request.query_params.get("owner") or "").strip()[:_OWNER_CAP] or None
    if env and q and env != q:
        return "\x00"  # contradictory scopes: matches no owner
    return env or q


async def serve_connect(request):
    """GET /origin-connect/{token}[?format=sh] — consume the link, return the doc."""
    token = request.path_params.get("token", "")
    as_sh = (request.query_params.get("format") or "").lower() in ("sh", "shell")
    media = "text/x-shellscript" if as_sh else "text/markdown; charset=utf-8"
    if OC.is_preview_fetch(request.method, request.headers.get("user-agent")):
        # HEAD probes + chat unfurlers: never consume, never validate
        body = ("#!/bin/sh\necho 'loopyard: this is a one-time connect link "
                "preview; fetch it with GET to get the script' >&2\nexit 1\n"
                if as_sh else OC.PREVIEW_BODY)
        return Response(body, media_type=media,
                        headers=dict(_NO_STORE, **{"X-Loopyard-Preview": "1"}))
    try:
        join = OC.consume_link(token, owner=_scope_owner(request))
    except OC.LinkError as e:
        return Response(OC.render_refusal(e, as_sh=as_sh), status_code=e.status,
                        media_type=media,
                        headers=dict(_NO_STORE, **{"X-Loopyard-Refusal": e.code}))
    except origin_onboard.MintError as e:
        err = OC.LinkError("hub_unavailable", e.message)
        return Response(OC.render_refusal(err, as_sh=as_sh), status_code=503,
                        media_type=media,
                        headers=dict(_NO_STORE, **{"X-Loopyard-Refusal": e.code}))
    body = OC.render_script(join) if as_sh else OC.render_markdown(join)
    return Response(body, media_type=media, headers=_NO_STORE)


def _err(e) -> JSONResponse:
    status = getattr(e, "status", 400)
    return JSONResponse({"error": e.message, "code": e.code}, status_code=status)


def _api_owner(request, body=None):
    o = ((body or {}).get("owner") if body else None) \
        or request.query_params.get("owner") or ""
    return o.strip()[:_OWNER_CAP] or None


async def mint_api(request):
    """POST /api/loops/origin-connect {label?, ttlSec?, owner?} → the link.
    The public URL is taken from the request's own origin when
    ``$LOOPYARD_PUBLIC_URL`` is unset."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        res = OC.mint_link(
            owner=_api_owner(request, body), label=body.get("label"),
            hub_url=body.get("hubUrl") or None,
            ttl_sec=body.get("ttlSec"),
            public_url=(os.environ.get(OC.PUBLIC_URL_ENV)
                        or os.environ.get(OC.LEGACY_PUBLIC_URL_ENV)
                        or f"{request.url.scheme}://{request.url.netloc}"))
    except (OC.LinkError, origin_onboard.MintError) as e:
        return _err(e)
    res.pop("token", None)   # the url already carries it; no second copy
    return JSONResponse(res, headers=_NO_STORE)


async def status_api(request):
    """GET /api/loops/origin-connect/{id}[?owner=] → {state, deviceId, …}."""
    try:
        return JSONResponse(OC.link_status(request.path_params.get("id", ""),
                                           owner=_api_owner(request)),
                            headers=_NO_STORE)
    except OC.LinkError as e:
        return _err(e)


async def revoke_api(request):
    """POST /api/loops/origin-connect/{id}/revoke[?owner=]."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    try:
        return JSONResponse(OC.revoke_link(
            request.path_params.get("id", ""),
            owner=_api_owner(request, body if isinstance(body, dict) else None)),
            headers=_NO_STORE)
    except OC.LinkError as e:
        return _err(e)


# ── the current Hub (mcp_loops.hub_record): shown + switched on Origins ──────

async def hub_status_api(request):
    """GET /api/loops/origin-hub → the current Hub {mode, originId, host,
    publicUrl, fingerprint, configured, live, …}."""
    import asyncio
    return JSONResponse(await asyncio.to_thread(hub_record.status),
                        headers=_NO_STORE)


async def hub_set_api(request):
    """POST /api/loops/origin-hub {mode: self|remote, publicUrl?, fingerprint?,
    originId?, host?} → switch which machine is the Hub; returns the status."""
    import asyncio
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        hub_record.set_hub(mode=str(body.get("mode") or ""),
                           public_url=body.get("publicUrl"),
                           fingerprint=body.get("fingerprint"),
                           origin_id=body.get("originId"),
                           host=body.get("host"))
    except hub_record.HubRecordError as e:
        return _err(e)
    return JSONResponse(await asyncio.to_thread(hub_record.status),
                        headers=_NO_STORE)
