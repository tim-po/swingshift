"""Owner-scoped REST reads (owner-scoping Phase 5). Kept in its own module so
``loops_panel`` stays untouched: the routes in ``loops_dashboard`` are re-pointed
here and each handler wraps the panel's existing read, adding ONLY the tenant
``owner`` seam.

``owner`` means ONE thing product-wide — the P3 enrolled-owner id, resolved by
:func:`mcp_loops.schema.resolve_owner` (a record without an owner belongs to the
single local default owner). No auth, login or tenancy: ``?owner=`` is a filter,
not an access control. Unset ``?owner=`` on a single-owner box returns bytes
identical to the pre-owner endpoints."""
from __future__ import annotations

import asyncio

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from mcp_loops import devices as _devices
from mcp_loops import schema as _schema
from tracking_ui import loops_panel as P

_OWNER_CAP = 128


def _owner_param(request):
    """``(owner|None, error_response|None)`` from ``?owner=``."""
    owner = (request.query_params.get("owner") or "").strip()
    if len(owner) > _OWNER_CAP:
        return None, JSONResponse({"error": "bad owner"}, status_code=400)
    return (owner or None), None


def _loop_owner(roots: dict, row: dict) -> str:
    root = roots.get(row.get("host"))
    cfg = P._read_json(root / row["name"] / "config.json") if root and row.get("name") else None
    return _schema.resolve_owner(cfg)


async def loops_list_api(request):
    """GET /api/loops[?owner=] — the panel's loop list, each row resolved to its
    tenant owner (config ``owner``, else the local default). ``?owner=`` keeps
    only that owner's loops and echoes ``owner``. Unset → rows carry ``owner``
    only when a non-default owner exists (else byte-identical to master)."""
    owner, bad = _owner_param(request)
    if bad is not None:
        return bad
    # H5 step-1: the engine's fan-out list (loop_list_all) is the membership +
    # freshness source — it alone sees CONNECTED origins' live loops (G2.1 / S-0)
    engine = await _engine_list()
    # every loop's run/status/config is read from disk — off the event loop
    # (loopyard-bug-1790562060)
    body = await run_in_threadpool(_loops_list_body, owner, engine)
    return JSONResponse(body)


_ENGINE_TIMEOUT = 5.0
# the live-overlay fields a connected origin's row carries (loop_list_all)
_LIVE_KEYS = ("deviceId", "fetchedAt", "stale")


async def _engine_list():
    """``loop_list_all(include_archived=True)`` rows, or ``None`` when the engine
    can't answer (down / error / timeout) — the caller then serves the file scan
    alone. Archived rows are asked for because the file scan lists them too."""
    try:
        res = await asyncio.wait_for(
            P._mcp_call("loop_list_all", {"include_archived": True}), _ENGINE_TIMEOUT)
    except Exception:  # noqa: BLE001 — a slow/broken engine must never sink the list
        return None
    if not isinstance(res, dict) or res.get("error") or not isinstance(res.get("loops"), list):
        return None
    return res


def _merge_engine(loops: list[dict], roots: dict, engine: dict) -> list[str]:
    """H5 step-1: fuse ``loop_list_all`` over the file-scan rows, in place.

    The file scan keeps its rich rows (goal, team, turns, …) for every loop it can
    read; the engine answer adds what only it can see. Keyed on (origin, name) —
    a mirror origin's id IS the file scan's ``host`` label, and a Hub-saved loop
    dispatched to a remote origin is keyed on that origin (as loop_list_all does).
    * a ``source:"live"`` engine row over a file row overlays the fresher state /
      updated (+ deviceId/stale) and tags the row ``source``/``origin``;
    * a live engine row with NO file row (a connected origin's loop that was
      never rsync-mirrored here) is appended as a thin row, ``host`` = its origin;
    * a mirror engine row changes nothing — mirrors are the file scan's own
      domain (it reads the same dirs), so bytes stay stable and an engine with
      a different data dir can't duplicate rows.
    Returns the origin ids the engine saw that the file scan has no source for."""
    index: dict[tuple, dict] = {}
    for row in loops:
        index.setdefault((row.get("host"), row.get("name")), row)
        if row.get("host") == "local" and row.get("name"):
            root = roots.get("local")
            disp = P._dispatched_origin(root / row["name"]) if root else None
            if disp:
                index[(disp, row["name"])] = row
    extra: list[str] = []
    for er in engine.get("loops") or []:
        name, origin = er.get("name"), er.get("origin")
        if not name or not origin:
            continue
        row = index.get((origin, name))
        if er.get("source") != "live":
            continue
        if row is None:
            row = {"name": name, "host": origin, "origin": origin,
                   "state": er.get("state") or "saved",
                   "updated": er.get("updated"),
                   "archived": bool(er.get("archived")),
                   "single_agent": er.get("single_agent"),
                   "project": er.get("project"),
                   "owner": _schema.resolve_owner(er),
                   "source": "live"}
            row.update({k: er[k] for k in _LIVE_KEYS if k in er})
            loops.append(row)
            index[(origin, name)] = row
            if origin not in roots and origin not in extra:
                extra.append(origin)
        else:
            row["state"] = er.get("state") or row.get("state")
            if er.get("updated") is not None:
                row["updated"] = er["updated"]
            if er.get("owner"):
                row["owner"] = er["owner"]
            row["origin"], row["source"] = origin, "live"
            row.update({k: er[k] for k in _LIVE_KEYS if k in er})
    loops.sort(key=lambda x: x.get("updated") or x.get("started") or 0, reverse=True)
    return extra


def _loops_list_body(owner, engine=None) -> dict:
    """The /api/loops body: the file scan (local + ``_loops_<host>`` mirrors),
    fused with the engine's ``loop_list_all`` when it answered (H5 step-1) so a
    connected origin's live loops show. ``engine=None`` is the explicit fallback:
    the pure file scan, byte-identical to the pre-H5 body."""
    sources = P._sources()
    roots = dict(sources)
    loops = P.list_loops()
    for row in loops:
        row["owner"] = _loop_owner(roots, row)
    hosts = [h for h, _ in sources]
    if engine:
        hosts += _merge_engine(loops, roots, engine)
    loops = _schema.owner_scope(loops, owner)
    body: dict = {"loops": loops, "hosts": hosts}
    if owner:
        body["owner"] = owner
    return body


async def devices_list_api(request):
    """GET /api/loops/devices[?owner=] — the unified Device Registry (same reads
    and fail-soft contract as the panel handler it replaces), with each device's
    owner taken from its own record (P3 enrolled owner, else the local default)
    so ``?owner=`` is a real filter."""
    owner, bad = _owner_param(request)
    if bad is not None:
        return bad
    origins_res = await P._mcp_call("loop_origin_list", {})
    origins = origins_res.get("origins") if isinstance(origins_res, dict) else None
    sessions_res = await P._mcp_call("loop_sessions_list", {})
    err = None
    if isinstance(origins_res, dict) and origins_res.get("error"):
        err = origins_res.get("error")
    elif not isinstance(origins, list):
        err = "origins unavailable"
    elif isinstance(sessions_res, dict) and sessions_res.get("error"):
        err = sessions_res.get("error")
    return JSONResponse(_devices.devices_response(
        origins if isinstance(origins, list) else [],
        sessions_res if isinstance(sessions_res, dict) else {},
        owner_filter=owner, error=err))


def _cross_owner(request):
    """Slice 3: a 403 refusal when ``?owner=`` is set and the named loop (on
    ``?host=``, default local) belongs to another owner; else None. An unknown
    loop/host falls through to the panel's own not-found answer."""
    owner, bad = _owner_param(request)
    if bad is not None:
        return bad
    if not owner:
        return None
    name = request.path_params.get("name", "")
    roots = dict(P._sources())
    root = roots.get(request.query_params.get("host", "local"))
    if root is None or not name or "/" in name or name.startswith("."):
        return None
    cfg = P._read_json(root / name / "config.json")
    refused = _schema.owner_refusal(name, cfg, owner)
    return JSONResponse(refused, status_code=403) if refused else None


async def loop_detail_api(request):
    """GET /api/loops/{name}[?owner=] — the panel's loop detail, refused (403,
    no data) for a cross-owner read; unset ``?owner=`` is the panel handler."""
    return _cross_owner(request) or await P.loop_detail_api(request)


async def loop_config_api(request):
    """GET /api/loops/{name}/config[?owner=] — same cross-owner guard."""
    return _cross_owner(request) or await P.loop_config_api(request)


def guarded(panel_handler):
    """The same cross-owner guard around any other ``/api/loops/{name}/*`` GET
    reader (teamroom, resolution, goodness, thought-log, agent-reports, turn,
    files, file, download) — any module's handler, so ``observability_api`` is
    wrapped, not edited: refused → 403 no data; else the unchanged handler."""
    async def handler(request):
        return _cross_owner(request) or await panel_handler(request)
    handler.__name__ = getattr(panel_handler, "__name__", "handler")
    handler.__wrapped__ = panel_handler
    handler.__doc__ = (f"Owner-guarded ({handler.__name__}): ?owner= cross-owner "
                       "reads are refused 403; otherwise the panel handler.")
    return handler



async def thread_get_api(request):
    """GET /api/loops/thread/{id}[?owner=] — unset owner is the panel handler;
    with ``?owner=`` the MCP tool applies the thread guard and a refusal is a
    403 (nothing created or folded)."""
    owner, bad = _owner_param(request)
    if bad is not None:
        return bad
    if not owner:
        return await P.thread_get_api(request)
    did = (request.path_params.get("id") or "").strip("/")
    if not P._doc_id_ok(did):
        return JSONResponse({"error": "bad doc id"}, status_code=400)
    res = await P._mcp_call("loop_thread_get", {"doc_id": did, "owner": owner})
    status = 403 if isinstance(res, dict) and res.get("refused") else 200
    return JSONResponse(res, status_code=status)


async def loop_agent_api(request):
    """GET /api/loops/agent?id=<agent>[&owner=] — an agent's turns across loops,
    scoped to one owner's loops when ``?owner=`` is set (else the panel handler)."""
    owner, bad = _owner_param(request)
    if bad is not None:
        return bad
    if not owner:
        return await P.loop_agent_api(request)
    agent = request.query_params.get("id", "")
    if not P._NAME_RE.match(agent or ""):
        return JSONResponse({"error": "bad agent id"}, status_code=400)
    return JSONResponse(await P._mcp_call(
        "loop_agent_detail", {"agent_id": agent, "owner": owner}))
