"""chat_api.py — LIVE delivery for the objective-manager thread (agent chat).

The thread itself is Loopyard-native: :mod:`mcp_loops.threads` stores it, and
``loop_thread_post`` dispatches each agent turn as a connect task to the attached
session. The reply folds in only from that session's REAL returned envelope. This
module adds no model logic. It pushes the thread's *visible changes* to the
browser as they happen, so a pending turn goes queued → working → folded without
the user refreshing:

* ``GET /api/loops/chat/events/{id}`` — Server-Sent Events. It emits one
  ``event: thread`` (the full ``loop_thread_get`` view, with ``rev``) at open
  and again whenever ``rev`` changes. ``: ping`` comments keep proxies from
  idling the stream out. After ``SSE_MAX_S`` it sends ``event: bye`` and ends;
  EventSource reconnects on its own (``retry:``).
* ``GET /api/loops/chat/wait/{id}?rev=<rev>&timeout=<s>`` — the long-poll
  fallback for clients or proxies that can't hold an SSE stream. It returns
  as soon as the thread's ``rev`` differs from the one given, or returns the
  unchanged view with ``changed: false`` at the timeout.

* ``GET /api/loops/chat/threads?project=<id>`` — the Chat index: one row per
  Hub doc in a REAL Loopyard project, with its thread summary, plus the real
  project registry (proxies ``loop_thread_list``). A pure read.

What the browser sees is always a fresh read of the store: no interim text, no
typing simulation. Streaming here means *state transitions*: the connect
protocol has no partial-result channel, so reply text arrives whole when the
session returns. If the loops server is unreachable, that is reported as an
``error`` event or body. It is never masked.

Both handlers read ``loops_panel._mcp_call`` at call time, so tests stub it, and
tests also stub ``_sleep`` so they never wait on the wall clock.
"""
from __future__ import annotations

import asyncio
import json
import time

from starlette.responses import JSONResponse, StreamingResponse

from tracking_ui import loops_panel

POLL_S = 1.0          # re-read cadence while a turn is in flight
IDLE_POLL_S = 3.0     # re-read cadence when idle (catches attach/offline changes)
PING_S = 15.0         # SSE keep-alive comment interval
SSE_MAX_S = 300.0     # one stream's lifetime; EventSource reconnects after
WAIT_MAX_S = 25.0     # long-poll ceiling (below common proxy idle timeouts)
RETRY_MS = 1000       # EventSource reconnect hint

_sleep = asyncio.sleep
_clock = time.monotonic


async def _read(did: str) -> dict:
    res = await loops_panel._mcp_call("loop_thread_get", {"doc_id": did})
    return res if isinstance(res, dict) else {"error": "thread unavailable"}


def _in_flight(view: dict) -> bool:
    return ((view.get("session_state") or {}).get("state") == "awaiting")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _disconnected(request) -> bool:
    probe = getattr(request, "is_disconnected", None)
    if probe is None:
        return False
    try:
        return bool(await probe())
    except Exception:  # noqa: BLE001 — a broken probe means the client is gone
        return True


async def thread_events_api(request):
    """SSE stream of the objective-manager thread for a Hub doc. See the module
    docstring for the event contract."""
    did = (request.path_params.get("id") or "").strip("/")
    if not loops_panel._doc_id_ok(did):
        return JSONResponse({"error": "bad doc id"}, status_code=400)

    async def gen():
        yield f"retry: {RETRY_MS}\n\n"
        start = last_ping = _clock()
        last_rev = None
        last_err = None
        view: dict = {}
        while True:
            view = await _read(did)
            if view.get("error"):
                if view["error"] != last_err:
                    last_err = view["error"]
                    yield _sse("error", {"doc_id": did, "error": view["error"]})
            else:
                last_err = None
                if view.get("rev") != last_rev:
                    last_rev = view.get("rev")
                    yield _sse("thread", view)
                    last_ping = _clock()
            now = _clock()
            if now - start >= SSE_MAX_S:
                yield _sse("bye", {"doc_id": did, "rev": last_rev})
                return
            if now - last_ping >= PING_S:
                yield ": ping\n\n"
                last_ping = now
            await _sleep(POLL_S if _in_flight(view) else IDLE_POLL_S)
            if await _disconnected(request):
                return

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


async def thread_wait_api(request):
    """Long-poll: return the thread view as soon as its ``rev`` differs from
    ``?rev=``. With no ``rev``, return right away. At ``?timeout=`` (capped at
    ``WAIT_MAX_S``), return the unchanged view with ``changed: false``."""
    did = (request.path_params.get("id") or "").strip("/")
    if not loops_panel._doc_id_ok(did):
        return JSONResponse({"error": "bad doc id"}, status_code=400)
    since = (request.query_params.get("rev") or "").strip()
    try:
        timeout = float(request.query_params.get("timeout") or WAIT_MAX_S)
    except ValueError:
        timeout = WAIT_MAX_S
    timeout = max(0.0, min(timeout, WAIT_MAX_S))
    deadline = _clock() + timeout
    while True:
        view = await _read(did)
        if view.get("error"):
            return JSONResponse(view)
        if not since or view.get("rev") != since:
            return JSONResponse({**view, "changed": True})
        left = deadline - _clock()
        if left <= 0 or await _disconnected(request):
            return JSONResponse({**view, "changed": False})
        await _sleep(min(left, POLL_S if _in_flight(view) else IDLE_POLL_S))


async def thread_list_api(request):
    """The Chat index for a project (empty ⇒ all projects). It proxies
    ``loop_thread_list``: ``{ok, project, project_known, projects, threads, count}``.
    There is no default project. An unknown one reads ``project_known: false``
    with zero rows, never a silent fallback."""
    project = (request.query_params.get("project") or "").strip()
    if len(project) > 200 or any(c in project for c in "/\\\0"):
        return JSONResponse({"error": "bad project"}, status_code=400)
    res = await loops_panel._mcp_call("loop_thread_list", {"project": project})
    return JSONResponse(res if isinstance(res, dict) else {"error": "threads unavailable"})
