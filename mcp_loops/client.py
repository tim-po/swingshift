"""LoopsMCP — synchronous client for the mcp-loops server.

The coordinator runners are ordinary Python loops (not Claude sessions), so
they can't use MCP tools natively. ``LoopsMCP`` wraps the streamable-HTTP MCP
endpoint (http://127.0.0.1:8771/mcp) behind small SYNC methods that mirror the
8 server tools:

    MCP = LoopsMCP()
    MCP.loop_save(config_dict)
    MCP.loop_start("dash-polish")

Session lifecycle: one short-lived ClientSession PER CALL (open → initialize →
call_tool → close), following the same short-lived-session client pattern the
other MCP servers on the box use. No connection state to babysit — a
restarted server is simply picked up on the next call. Each method runs its
own ``asyncio.run`` so callers stay fully synchronous.

Error model — two distinct layers, on purpose:
  * transport/session failure (server down, connect refused, timeout, protocol
    error, tool-level MCP isError) → raises ``LoopsMCPError``. Callers log +
    skip/retry; a runner loop must never crash on this.
  * tool-DOMAIN failure (invalid config, unknown loop, not waiting) → returned
    as the tool's own ``{"error": ...}`` dict, exactly as the server produced
    it — the caller decides what it means.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Optional

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

DEFAULT_URL = "http://127.0.0.1:8771/mcp"
ENV_URL = "MCP_LOOPS_URL"
# connect/write timeout + read timeout: tools are local file/thread work; keep
# read generous so a slow loop_start spawn never trips us.
CONNECT_TIMEOUT = 30.0
READ_TIMEOUT = 120.0

# B2: the one message every unset-URL path speaks. A fresh install is NOT on the
# host's :8771 — hitting it silently means talking to whatever owns that port
# (the host), which leaks host paths into a pilot's errors.
URL_UNSET_HINT = (
    f"{ENV_URL} not set — run scripts/loopyard-quickstart.sh, then copy-paste "
    "the printed exports (MCP_LOOPS_URL / LOOPS_DATA_DIR / LOOPYARD_INSTALL)."
)


def url_unset_hint() -> str:
    """:data:`URL_UNSET_HINT`, but in a tarball (bundle) install — BUNDLE.json at
    the install root — name its own ``<root>/bin/yard start``: a bundle has no
    scripts/loopyard-quickstart.sh (friction, clearall-v1-launch-build)."""
    import shlex
    from mcp_loops import paths
    root = paths.install_root()
    if (root / "BUNDLE.json").is_file():
        yard = shlex.quote(str(root / "bin" / "yard"))
        return (f"{ENV_URL} not set — run `{yard} start`, then copy-paste the "
                "`export MCP_LOOPS_URL=… / LOOPS_DATA_DIR=…` lines it prints.")
    return URL_UNSET_HINT


def default_url() -> str:
    """The endpoint a client hits when none is passed explicitly. A fresh user's
    install is NOT on the host's :8771 — honour ``$MCP_LOOPS_URL`` so a pilot box
    reaches ITS OWN server instead of silently talking to the host install."""
    return os.environ.get(ENV_URL) or DEFAULT_URL


class LoopsMCPError(RuntimeError):
    """The mcp-loops server is unreachable or the MCP call itself failed
    (transport/session/protocol level — NOT a tool-domain {"error": ...})."""


class LoopsURLUnset(LoopsMCPError):
    """B2: neither an explicit URL nor ``$MCP_LOOPS_URL`` was set, so the only
    endpoint left is the host's :8771 default — which a fresh install must NOT
    silently hit. Raised only when the caller asks to ``require_env`` (the CLI);
    the message is :data:`URL_UNSET_HINT`."""


_WARNED_DEFAULT = False


def _warn_default_once() -> None:
    """Say ONCE, on stderr, that we fell back to the :8771 default because
    ``$MCP_LOOPS_URL`` is unset — naming the URL actually used. Belt to the CLI's
    hard error: a library caller (e.g. the connector) at least sees it isn't
    reaching its own server. Once-per-process so we never spam a poll loop."""
    global _WARNED_DEFAULT
    if _WARNED_DEFAULT:
        return
    _WARNED_DEFAULT = True
    sys.stderr.write(
        f"mcp_loops: {ENV_URL} unset — defaulting to {DEFAULT_URL}. A fresh "
        "install is NOT on the host's :8771; run scripts/loopyard-quickstart.sh "
        "and export the printed MCP_LOOPS_URL to reach your own server.\n")


def result_dict(res: Any, tool: str) -> dict:
    """CallToolResult -> plain dict.

    FastMCP tools annotated ``-> dict`` return their dict as
    ``structuredContent``; fall back to parsing the first text content as
    JSON. An MCP-level isError becomes LoopsMCPError.
    """
    if getattr(res, "isError", False):
        detail = ""
        for c in getattr(res, "content", None) or []:
            detail = getattr(c, "text", "") or detail
        raise LoopsMCPError(f"mcp-loops tool {tool} errored: {detail or res}")
    sc = getattr(res, "structuredContent", None)
    if isinstance(sc, dict):
        return sc
    for c in getattr(res, "content", None) or []:
        text = getattr(c, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return {"text": text}
    return {}


class LoopsMCP:
    """Sync wrapper over the 8 mcp-loops tools."""

    def __init__(self, url: Optional[str] = None, *, require_env: bool = False):
        # explicit arg > $MCP_LOOPS_URL > localhost:8771. Resolved at construction
        # so every call on this instance targets one stable endpoint. B2: when we
        # fall through to the :8771 default (no explicit url, env unset) either
        # REFUSE (``require_env`` — the CLI) or warn loudly on stderr, so a fresh
        # install never silently talks to the host's server.
        env = os.environ.get(ENV_URL)
        if url:
            self.url, self.url_defaulted = url, False
        elif env:
            self.url, self.url_defaulted = env, False
        else:
            if require_env:
                raise LoopsURLUnset(url_unset_hint())
            self.url, self.url_defaulted = DEFAULT_URL, True
            _warn_default_once()

    # --- plumbing -------------------------------------------------------------
    def _call(self, tool: str, args: dict) -> dict:
        async def _go():
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
                    follow_redirects=True) as hc:
                async with streamable_http_client(
                        self.url, http_client=hc) as (read, write, _):
                    async with ClientSession(read, write) as s:
                        await s.initialize()
                        return await s.call_tool(tool, args)
        try:
            res = asyncio.run(_go())
        except LoopsMCPError:
            raise
        except Exception as e:  # noqa: BLE001 — surface ONE clear exception type
            raise LoopsMCPError(
                f"mcp-loops {tool} failed ({self.url}): "
                f"{type(e).__name__}: {e}") from e
        return result_dict(res, tool)

    def call(self, tool: str, args: Optional[dict] = None) -> dict:
        """Call ANY server-registered tool by name. This is the generic shim
        path: unlike the typed methods below, it forwards whatever tool name the
        server exposes — so the doc-blessed public names (start_loop,
        get_loop_status, cancel_loop, get_loop_result) work without a bespoke
        wrapper each. Raises ``LoopsMCPError`` on transport failure; a
        tool-DOMAIN error comes back as the tool's own ``{"error": ...}`` dict."""
        return self._call(tool, args or {})

    @staticmethod
    def _args(**kw) -> dict:
        """Drop None values so server-side defaults apply."""
        return {k: v for k, v in kw.items() if v is not None}

    # --- config tools ---------------------------------------------------------
    def loop_save(self, config: dict) -> dict:
        """loop_save (validate + persist normalized config) -> {ok, name, path,
        warnings} or {"error", errors}."""
        return self._call("loop_save", {"config": config})

    def loop_render(self, name: str) -> dict:
        """loop_render -> {ok, json_path, html_path}."""
        return self._call("loop_render", {"name": name})

    def loop_list(self) -> dict:
        """loop_list -> {loops: [{name, state, updated}], count}."""
        return self._call("loop_list", {})

    def loop_get(self, name: str) -> dict:
        """loop_get -> {name, config, run} or {"error": ...}."""
        return self._call("loop_get", {"name": name})

    # --- run tools ------------------------------------------------------------
    def loop_start(self, name: str, slug: Optional[str] = None,
                   dispatch_id: Optional[str] = None) -> dict:
        """loop_start (background LoopRunner on HeadlessSubstrate)
        -> {ok, name, state} or {"error": ...}.

        ``dispatch_id`` (set by the origin-agent when a hub routed this start over
        the channel) tells the engine to run the loop LOCALLY keyed on that id,
        rather than re-routing it back through the hub (G2.2 idempotent dispatch)."""
        return self._call("loop_start", self._args(
            name=name, slug=slug, dispatch_id=dispatch_id))

    def loop_status(self, name: str, tail: Optional[int] = None) -> dict:
        """loop_status -> {name, run, recent, waiting_question}."""
        return self._call("loop_status", self._args(name=name, tail=tail))

    def loop_stop(self, name: str) -> dict:
        """loop_stop -> {ok, name} or {"error": ...}."""
        return self._call("loop_stop", {"name": name})

    def loop_reply(self, name: str, reply: str) -> dict:
        """loop_reply (deliver an owner reply to a parked ask_owner)
        -> {ok, name} or {"error": ...}."""
        return self._call("loop_reply", {"name": name, "reply": reply})

    # --- app-connect OUTBOUND (connector side, CAP-2 §2b) ---------------------
    def connect_register(self, connector_id: str, runtime: Optional[str] = None,
                         capabilities: Optional[list] = None,
                         meta: Optional[dict] = None,
                         secret: Optional[str] = None) -> dict:
        """connect_register -> {ok, connector, secret?}. Register this session as
        a connector that can execute dispatched loop tasks. A new id is issued
        ``secret`` (once); re-registering an existing id must present it."""
        return self._call("connect_register", self._args(
            connector_id=connector_id, runtime=runtime,
            capabilities=capabilities, meta=meta, secret=secret))

    def connect_poll(self, connector_id: str, secret: Optional[str] = None) -> dict:
        """connect_poll -> {connector, task}. Heartbeat + claim the next task
        this connector can run (task is None when the queue is empty)."""
        return self._call("connect_poll", self._args(connector_id=connector_id,
                                                      secret=secret))

    def connect_return(self, task_id: str, status: Optional[str] = None,
                       summary: Optional[str] = None, artifacts: Optional[list] = None,
                       git_commit: Optional[str] = None, output: Any = None,
                       connector_id: Optional[str] = None,
                       secret: Optional[str] = None) -> dict:
        """connect_return -> {ok, task}. Return the result envelope for a task
        ``connector_id`` claimed (presenting its ``secret``) and mark it
        terminal."""
        return self._call("connect_return", self._args(
            task_id=task_id, status=status, summary=summary,
            artifacts=artifacts, git_commit=git_commit, output=output,
            connector_id=connector_id, secret=secret))
