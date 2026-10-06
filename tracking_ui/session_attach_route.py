"""The gated Session Attach channel: ``/session-attach/<token>`` (the one-time
link), ``/__attach/claim`` + ``/__attach/mcp``.

This is the public face a remote Claude/Codex session dials with the token the
owner minted on the Sessions page (:mod:`mcp_loops.session_attach`). It follows
the mac-origin pattern (:mod:`tracking_ui.mac_origin`): the routes are
gate-exempt but check a bearer secret on every request, and anything that does
not authenticate is refused. It never proxies to the raw engine port (:8771).

* ``GET /session-attach/<token>`` is the SHORT link the owner pastes to their
  AI. Fetching it consumes the single-use token, registers the session as its
  connector and answers a complete markdown instruction document (the scoped
  credential, the exact ``claude mcp add`` line, a generic/Codex config,
  ``connect_register`` and the poll loop). ``?format=sh`` answers the same as
  runnable POSIX sh. Either format is THE consuming fetch; ``HEAD`` validates
  without consuming, and a GET from a known chat/social link unfurler
  (:func:`origin_connect.is_preview_fetch`) gets a fixed preview stub — it
  neither consumes nor validates nor carries the credential. A dead token answers plain text with a
  distinct status: 404 unknown, 410 expired, 409 already used, 403 revoked or
  wrong owner.
* ``POST /__attach/claim`` with ``Authorization: Bearer sat_…`` consumes the
  single-use token, registers the session as its connector and returns the
  session credential (``sac_…``). The JSON body may carry
  ``{runtime, capabilities, meta}``.
* ``POST /__attach/mcp`` with ``Authorization: Bearer sac_…`` is a minimal MCP
  endpoint (streamable-HTTP, plain JSON responses). ``tools/list`` lists only
  the session scope. ``tools/call`` is authenticated and scope-gated
  (:func:`session_attach.authorize`) before it reaches the engine.

Refusals are HTTP 401 (bad or dead secret) or 403 (out of scope), each with a
distinct ``refused`` reason. Mounting these routes in the public gate is a
deploy step (see docs/SESSION-ATTACH.md); this module ships the code only.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from mcp_loops import connect
from mcp_loops import session_attach as sa
from mcp_loops.origin_connect import is_preview_fetch

PREFIX = "/__attach"
MAX_BODY = 1 << 20
PROTOCOL_VERSION = "2025-03-26"

#: what a link unfurler gets instead of the instructions — identical for every
#: token (valid or not), so it is no oracle for which tokens exist.
PREVIEW_MD = """# Loopyard one-time session-attach link

This link is meant for an AI session (Claude, Codex, …): paste it to your AI and
ask it to attach. The AI fetches it and follows the instructions. It works once
and expires; a link preview does NOT use it up.
"""
PREVIEW_SH = ("#!/bin/sh\necho 'loopyard: this is a one-time session-attach link "
              "preview; fetch it with GET to get the script' >&2\nexit 1\n")

#: the tool schemas a session sees — its whole world. Kept in step with
#: :data:`session_attach.SCOPED_TOOLS` (a test pins the two together).
TOOL_SCHEMAS: dict[str, dict] = {
    "connect_register": {
        "description": "Refresh this session's connector registration (runtime, capabilities, meta).",
        "inputSchema": {"type": "object", "properties": {
            "runtime": {"type": "string"},
            "capabilities": {"type": "array", "items": {"type": "string"}},
            "meta": {"type": "object"}}}},
    "connect_poll": {
        "description": "Heartbeat + claim the next task dispatched to this session (task or null).",
        "inputSchema": {"type": "object", "properties": {}}},
    "connect_return": {
        "description": "Return the result of a task this session claimed.",
        "inputSchema": {"type": "object", "required": ["task_id"], "properties": {
            "task_id": {"type": "string"}, "status": {"type": "string"},
            "summary": {"type": "string"}, "artifacts": {"type": "array"},
            "git_commit": {"type": "string"}, "output": {}, "error": {"type": "string"}}}},
    "loop_save": {
        "description": "Create or update a loop for this session's owner.",
        "inputSchema": {"type": "object", "required": ["config"], "properties": {
            "config": {"type": "object"}}}},
    "start_loop": {
        "description": "Start one of your owner's saved loops; returns its result envelope.",
        "inputSchema": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"}, "slug": {"type": "string"}}}},
    "get_loop_status": {
        "description": "Poll a loop this session started.",
        "inputSchema": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"}, "tail": {"type": "integer"}}}},
    "get_loop_result": {
        "description": "Read the result envelope of a loop this session started.",
        "inputSchema": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"}}}},
}

#: the args each engine tool accepts; anything else a caller sends is dropped.
_ALLOWED_ARGS = {
    "connect_register": {"connector_id", "runtime", "capabilities", "meta"},
    "connect_poll": {"connector_id"},
    "connect_return": {"task_id", "status", "summary", "artifacts", "git_commit",
                       "output", "error"},
    "loop_save": {"config"},
    "start_loop": {"name", "slug"},
    "get_loop_status": {"name", "tail", "owner"},
    "get_loop_result": {"name", "owner"},
}


def default_engine(tool: str) -> Callable[..., dict]:
    """The real engine tool (a plain function under ``@mcp.tool()``), imported
    lazily so the gate process pays for it only on first use."""
    from mcp_loops import server
    return getattr(server, tool)


def default_loop_owner(name: str) -> Optional[str]:
    """The resolved owner of a local loop, or None if there is no such loop."""
    import os

    from mcp_loops import schema, server
    if server._check_name(name):
        return None
    base = server._status_dir_for("local", name)
    if base is None:
        return None
    cfg = server._read_json(os.path.join(base, "config.json"))
    if cfg is None and server._read_json(os.path.join(base, "run.json")) is None:
        return None
    return schema.resolve_owner(cfg or {})


def _bearer(request: Request) -> Optional[str]:
    h = request.headers.get("authorization") or ""
    return h[7:].strip() if h.lower().startswith("bearer ") else None


def _refusal(e: sa.AttachRefused, rpc_id: Any = None) -> JSONResponse:
    body: dict = e.to_dict()
    if rpc_id is not None:
        body = {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32001, "message": e.message,
                          "data": {"refused": e.reason, "status": e.status}}}
    headers = {"WWW-Authenticate": 'Bearer realm="loopyard-attach"'} if e.status == 401 else None
    return JSONResponse(body, status_code=e.status, headers=headers)


async def _json_body(request: Request) -> Any:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise sa.AttachRefused("too_large", 403, "request body too large")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise sa.AttachRefused("bad_request", 403, "body is not JSON") from None


def build_routes(root_fn: Callable[[], str], *,
                 engine: Callable[[str], Callable[..., dict]] = default_engine,
                 loop_owner: Callable[[str], Optional[str]] = default_loop_owner,
                 owner_of_request: Optional[Callable[[Request], Optional[str]]] = None,
                 origin: Optional[str] = None,
                 public_base: Optional[str] = None) -> list[Route]:
    """The two attach routes. ``root_fn()`` is the connect root (same as the
    engine's ``_connect_root``). ``owner_of_request`` binds a claim to the owner
    this Hub face serves (None = accept the token's own owner).
    ``public_base`` is the ``…/__attach`` base the served instructions point
    at (else ``$LOOPYARD_ATTACH_PUBLIC_URL``, else the URL the link was fetched
    on)."""

    def _attach_base(request: Request) -> str:
        b = (public_base or os.environ.get(sa.PUBLIC_URL_ENV) or "").strip()
        return (b or str(request.base_url).rstrip("/") + PREFIX).rstrip("/")

    _nocache = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
                "Referrer-Policy": "no-referrer"}

    async def link(request: Request) -> Response:
        token = request.path_params.get("token") or ""
        expect_owner = owner_of_request(request) if owner_of_request else None
        as_sh = request.query_params.get("format") == "sh"
        if request.method == "GET" and is_preview_fetch(
                request.method, request.headers.get("user-agent")):
            # chat unfurlers: never consume, never validate, never emit sac_
            return Response(PREVIEW_SH if as_sh else PREVIEW_MD,
                            media_type=("text/x-shellscript" if as_sh else "text/markdown")
                            + "; charset=utf-8",
                            headers=dict(_nocache, **{"X-Loopyard-Preview": "1"}))
        try:
            if request.method != "GET":   # HEAD validates, never consumes
                sa.check(root_fn(), token, expect_owner=expect_owner,
                         expect_origin=origin)
                return Response(status_code=200, headers=_nocache)
            claimed = sa.claim(root_fn(), token, expect_owner=expect_owner,
                               expect_origin=origin,
                               meta={"attachedFrom": "session-attach-link"})
        except sa.AttachRefused as e:
            return PlainTextResponse(
                f"session-attach refused ({e.reason}): {e.message}\n",
                status_code=sa.LINK_STATUS.get(e.reason, 403), headers=_nocache)
        rec = sa._read(root_fn()).get(claimed["attachId"]) or {}
        fmt = "sh" if as_sh else "md"
        body = sa.instructions(_attach_base(request) + "/mcp", claimed, fmt=fmt,
                               runtime=rec.get("runtime") or "claude")
        media = "text/x-shellscript" if fmt == "sh" else "text/markdown"
        return Response(body, media_type=f"{media}; charset=utf-8", headers=_nocache)

    async def claim(request: Request) -> Response:
        try:
            body = await _json_body(request)
            body = body if isinstance(body, dict) else {}
            res = sa.claim(root_fn(), _bearer(request) or "",
                           expect_owner=owner_of_request(request) if owner_of_request else None,
                           expect_origin=origin,
                           runtime=body.get("runtime") if body.get("runtime") in ("claude", "codex") else None,
                           capabilities=body.get("capabilities") if isinstance(body.get("capabilities"), list) else None,
                           meta=body.get("meta") if isinstance(body.get("meta"), dict) else None)
        except sa.AttachRefused as e:
            return _refusal(e)
        return JSONResponse(res)

    def _call(sess: dict, tool: str, args: dict) -> dict:
        root = root_fn()
        clean = {k: v for k, v in (args or {}).items() if k in _ALLOWED_ARGS.get(tool, ())}
        gated = sa.authorize(sess, tool, clean,
                             task_lookup=lambda tid: connect.get_task(root, tid),
                             loop_owner=loop_owner)
        out = engine(tool)(**gated)
        if tool == "start_loop" and isinstance(out, dict) and not out.get("error"):
            sa.note_loop(root, sess["attachId"], gated["name"])
        return out

    async def mcp(request: Request) -> Response:
        rpc_id = None
        try:
            # authenticate BEFORE parsing anything the caller sent
            sess = sa.authenticate(root_fn(), _bearer(request))
            msg = await _json_body(request)
            if not isinstance(msg, dict):
                raise sa.AttachRefused("bad_request", 403, "batch requests are not supported")
            rpc_id = msg.get("id")
            method = msg.get("method")
            connect.touch_connector(root_fn(), sess["connectorId"])
            if rpc_id is None:                 # a notification: nothing to answer
                return Response(status_code=202)
            if method == "initialize":
                result: dict = {"protocolVersion": PROTOCOL_VERSION,
                                "capabilities": {"tools": {"listChanged": False}},
                                "serverInfo": {"name": "loopyard-session-attach",
                                               "version": "1"},
                                "instructions": (
                                    f"You are attached as connector {sess['connectorId']}. "
                                    "Call connect_poll to receive work, execute it, then "
                                    "connect_return the result.")}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": n, **TOOL_SCHEMAS[n]}
                                    for n in sorted(sa.SCOPED_TOOLS)]}
            elif method == "tools/call":
                p = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                out = _call(sess, str(p.get("name") or ""),
                            p.get("arguments") if isinstance(p.get("arguments"), dict) else {})
                result = {"content": [{"type": "text", "text": json.dumps(out, default=str)}],
                          "structuredContent": out,
                          "isError": bool(isinstance(out, dict) and out.get("error"))}
            else:
                return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                                     "error": {"code": -32601,
                                               "message": f"method {method!r} not supported"}})
        except sa.AttachRefused as e:
            return _refusal(e, rpc_id)
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    async def no_stream(request: Request) -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})

    return [
        Route(f"{sa.LINK_PATH}/{{token}}", link, methods=["GET", "HEAD"]),
        Route(f"{PREFIX}/claim", claim, methods=["POST"]),
        Route(f"{PREFIX}/mcp", mcp, methods=["POST"]),
        Route(f"{PREFIX}/mcp", no_stream, methods=["GET", "DELETE"]),
    ]
