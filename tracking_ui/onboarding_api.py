"""GET /api/loops/onboarding — the web app's getting-started checklist (Home).

Read-only proxy onto the engine's ``loop_onboarding_progress`` tool, which returns
the SAME payload ``yard onboard --json`` gives the desktop guide panel (steps +
explore items + places, each with its web route and an engine-derived ``done``).
One source of truth: mcp_loops/onboarding.py. The route is a literal prefix, so it
must be registered BEFORE the ``/api/loops/{name}`` catch-all.
"""
from __future__ import annotations

from starlette.responses import JSONResponse

from tracking_ui import loops_panel


async def onboarding_api(request):
    """Fail-soft: an engine error comes back as ``{"error": …}`` with 200 so the
    Home page can quietly fall back to its plain first-run view."""
    res = await loops_panel._mcp_call("loop_onboarding_progress", {})
    if not isinstance(res, dict):
        res = {"error": "bad response from the engine"}
    return JSONResponse(res)
