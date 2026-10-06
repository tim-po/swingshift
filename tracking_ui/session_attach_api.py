"""Dashboard REST for the Sessions page's **Attach a session** panel — the
owner-side half of the one-time session-attach link (:mod:`mcp_loops.session_attach`).

  POST /api/loops/sessions/attach-link      mint → {url, expiresAt, attachId, connectorId, ttl}
  GET  /api/loops/sessions/attaches         the owner's attaches + live/attached state
  POST /api/loops/sessions/attach/revoke    revoke a pending link or an attached session

These are dashboard (owner) routes — they sit behind the dashboard's own
perimeter like every other ``/api/loops/*`` route. The AI-facing half (the link
itself and the scoped ``/__attach/mcp`` endpoint) is
:mod:`tracking_ui.session_attach_route`, which authenticates its own secrets.

The link is minted against ``$LOOPYARD_ATTACH_PUBLIC_URL`` when set, else the
host this dashboard was reached on (``<base>/__attach``) — never raw :8771."""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_loops import connect
from mcp_loops import session_attach as sa

_CAP = 128


def _root() -> str:
    from mcp_loops import server
    return server._connect_root()


def attach_base(request: Request) -> str:
    """The ``…/__attach`` base a minted link (and the instructions it serves)
    points at."""
    b = (os.environ.get(sa.PUBLIC_URL_ENV) or "").strip()
    return (b or str(request.base_url).rstrip("/") + "/__attach").rstrip("/")


def _owner(v: object) -> Optional[str]:
    s = str(v or "").strip()
    return s[:_CAP] or None


async def _body(request: Request) -> dict:
    try:
        b = json.loads((await request.body()) or b"{}")
    except ValueError:
        return {}
    return b if isinstance(b, dict) else {}


def _rows(root: str, owner: Optional[str], now: float) -> list[dict]:
    live = {c.get("id") or c.get("connectorId"): bool(c.get("live"))
            for c in connect.list_connectors(root, now=now)}
    out = []
    for r in sa.list_attaches(root, owner=owner, now=now):
        r = {k: v for k, v in r.items() if k != "loops"} | {"loops": list(r.get("loops") or [])}
        r["live"] = r["state"] == sa.CONSUMED and live.get(r["connectorId"], False)
        r["attached"] = r["state"] == sa.CONSUMED
        out.append(r)
    return out


async def attach_link_api(request: Request) -> JSONResponse:
    """Mint a one-time attach link. Body: ``{ttl?, label?, runtime?, owner?}``."""
    b = await _body(request)
    try:
        ttl = int(b.get("ttl") or 0) or None
    except (TypeError, ValueError):
        return JSONResponse({"error": "ttl must be seconds"}, status_code=400)
    runtime = b.get("runtime") if b.get("runtime") in ("claude", "codex") else "claude"
    m = sa.mint(_root(), owner=_owner(b.get("owner")), ttl=ttl,
                label=(str(b.get("label") or "")[:_CAP] or None),
                runtime=runtime, base_url=attach_base(request))
    return JSONResponse({k: m[k] for k in ("url", "expiresAt", "attachId", "connectorId",
                                           "owner", "ttl", "runtime", "publicUrlConfigured")},
                        headers={"Cache-Control": "no-store"})


async def attaches_api(request: Request) -> JSONResponse:
    """The owner's attaches (newest first; no secrets) with ``state``,
    ``secondsLeft``, ``attached`` and ``live`` (its connector heartbeating)."""
    now = time.time()
    owner = _owner(request.query_params.get("owner"))
    try:
        rows = _rows(_root(), owner, now)
    except Exception as e:  # noqa: BLE001 — honest empty panel, never a 500
        return JSONResponse({"attaches": [], "now": now, "error": f"attaches unavailable: {e}"})
    return JSONResponse({"attaches": rows, "now": now}, headers={"Cache-Control": "no-store"})


async def attach_revoke_api(request: Request) -> JSONResponse:
    """Revoke by ``attachId`` or ``connectorId`` (body). Only the minting owner
    may revoke; the session's credential stops working at once."""
    b = await _body(request)
    attach_id = str(b.get("attachId") or "").strip() or None
    connector_id = str(b.get("connectorId") or "").strip() or None
    if not (attach_id or connector_id):
        return JSONResponse({"error": "attachId or connectorId required"}, status_code=400)
    res = sa.revoke(_root(), owner=_owner(b.get("owner")), attach_id=attach_id,
                    connector_id=connector_id)
    return JSONResponse(res, status_code=int(res.get("status") or 200) if res.get("error") else 200)
