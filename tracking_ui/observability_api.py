"""REST proxies for the Run observability & trust tools (dual goodness score,
thought-log). Kept in its own module so the loops_panel route table stays
untouched; each handler forwards to the MCP loops server through the panel's
shared ``_mcp_call`` seam (patched by tests)."""
from __future__ import annotations

from starlette.responses import JSONResponse

from tracking_ui import loops_panel as P


async def loop_goodness_api(request):
    """GET /api/loops/{name}/goodness[?refresh=1] → ``loop_goodness``: the
    owner good/ok/bad rating and the independent analyst score as two SEPARATE
    fields (never blended), each with an honest ``pending`` flag."""
    name = request.path_params.get("name", "")
    if not P._NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    args = {"name": name}
    if request.query_params.get("refresh") in ("1", "true"):
        args["refresh"] = True
    return JSONResponse(await P._mcp_call("loop_goodness", args))



async def loop_quality_api(request):
    """GET /api/loops/quality[?days=30&project=&compute=5&host=] → ``loop_quality``:
    the Library rollup — per-loop and per-agent owner rating BESIDE analyst
    score. Read-only apart from caching analyst scores for at most ``compute``
    finished-but-unscored runs."""
    qp = request.query_params
    args: dict = {}
    for key in ("days", "compute"):
        raw = qp.get(key)
        if raw not in (None, ""):
            try:
                args[key] = max(0, min(3650, int(raw)))
            except ValueError:
                return JSONResponse({"error": f"bad {key}"}, status_code=400)
    project = qp.get("project", "")
    if project:
        # "__unattributed__" is the switcher's no-project scope id
        if project != "__unattributed__" and not P._NAME_RE.match(project):
            return JSONResponse({"error": "bad project"}, status_code=400)
        args["project"] = project
    host = qp.get("host", "local")
    if host and host != "local":
        if not P._NAME_RE.match(host):
            return JSONResponse({"error": "bad host"}, status_code=400)
        args["origin"] = host
    return JSONResponse(await P._mcp_call("loop_quality", args))

async def loop_thoughtlog_api(request):
    """GET /api/loops/{name}/thoughtlog[?agent=&limit=] → ``loop_thoughtlog``:
    the capped one-line-per-turn reasoning gists, oldest-first."""
    name = request.path_params.get("name", "")
    if not P._NAME_RE.match(name or ""):
        return JSONResponse({"error": "bad name"}, status_code=400)
    args: dict = {"name": name}
    agent = request.query_params.get("agent") or ""
    if agent:
        args["agent"] = agent
    lim = request.query_params.get("limit")
    if lim:
        try:
            args["limit"] = max(1, min(int(lim), 500))
        except ValueError:
            return JSONResponse({"error": "bad limit"}, status_code=400)
    return JSONResponse(await P._mcp_call("loop_thoughtlog", args))
