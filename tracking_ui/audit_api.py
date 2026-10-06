"""REST proxy for the Hub's append-only dispatch audit (P3 slice 4). Kept in
its own module so the loops_panel route table stays untouched; the handler
forwards to the MCP loops server's ``origin_audit`` tool through the panel's
shared ``_mcp_call`` seam (patched by tests). Read-only — no write route."""
from __future__ import annotations

from starlette.responses import JSONResponse

from tracking_ui import loops_panel as P

_STR_FILTERS = ("origin", "owner", "verb")
_FILTER_CAP = 200


async def origin_audit_api(request):
    """GET /api/loops/audit[?limit=&origin=&owner=&verb=&decision=&since=] →
    ``origin_audit``: the last ``limit`` matching records (allowed + denied)
    plus a whole-log summary."""
    q = request.query_params
    args: dict = {}
    for key in _STR_FILTERS:
        val = q.get(key) or ""
        if val:
            if len(val) > _FILTER_CAP:
                return JSONResponse({"error": f"bad {key}"}, status_code=400)
            args[key] = val
    dec = q.get("decision") or ""
    if dec:
        if dec not in ("allow", "deny"):
            return JSONResponse({"error": "bad decision"}, status_code=400)
        args["decision"] = dec
    lim = q.get("limit")
    if lim:
        try:
            args["limit"] = max(1, min(int(lim), 1000))
        except ValueError:
            return JSONResponse({"error": "bad limit"}, status_code=400)
    since = q.get("since")
    if since:
        try:
            args["since"] = max(0.0, float(since))
        except ValueError:
            return JSONResponse({"error": "bad since"}, status_code=400)
    return JSONResponse(await P._mcp_call("origin_audit", args))
