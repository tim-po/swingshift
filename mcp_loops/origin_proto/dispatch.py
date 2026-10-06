"""dispatch — G2.2: route loop_start through the ENROLLED origin, not the hardcode.

The owner's north star: even the local VPS is an origin like any other — a loop
runs on its BOUND origin's OWN engine, reached over the uniform origin protocol,
never a hardcoded ``origins.LOCAL_ORIGIN_ID`` special-case. This module is the
in-process bridge that makes ``server.loop_start`` do exactly that for the local
origin, while staying additive + reversible:

  * :class:`LocalOriginService` stands up an in-process hub
    (:class:`~mcp_loops.origin_proto.control_plane.OriginHub`) plus a local
    origin-agent (:class:`~mcp_loops.origin_proto.agent_core.OriginAgentExecutor`
    over a :class:`~mcp_loops.origin_proto.channel.ChannelClient`) on a background
    asyncio loop, enrolls the box as origin ``local``, and dials the hub. It is
    the SAME protocol a remote origin uses — no shortcut.
  * :meth:`LocalOriginService.dispatch_start` (sync, thread-safe) sends a
    capability-scoped ``loop.start`` over the live channel with a FRESH
    client-generated dispatch-id (§7). The origin-agent's executor runs the loop
    on the box's own engine; a network redelivery of that dispatch-id re-attaches
    via the channel's :class:`DispatchLedger` instead of double-running.

Execution ALWAYS stays on the origin: the injected ``engine`` is the box's own
loop engine (the real server passes an adapter that calls its in-process launch
helper directly; tests pass a fake). The hub only ever sends a scoped RPC.

Back-compat / safety: the real ``loop_start`` installs a service only when the
origin-agent is actually up; with none installed it keeps the old in-process
path verbatim. A dispatch that errors fails OPEN to the local path so a hub/agent
hiccup never wedges a start. Nothing here binds to a network port beyond the
loopback high port the hub picks, and the private key is a generated throwaway on
tests / the box's own device key in production.
"""

from __future__ import annotations

import asyncio
import base64
import threading
import uuid
from typing import Any, Callable, Optional

from mcp_loops.origin_proto import identity
from mcp_loops.origin_proto.agent_core import (
    CapabilityReporter, OriginAgentExecutor, ProductAllowlist, RunAllowlist)
from mcp_loops.origin_proto.channel import ChannelClient, DispatchLedger
from mcp_loops.origin_proto.control_plane import OriginHub
from mcp_loops.origin_proto.enrollment import EnrollmentStore


class DispatchUnavailable(RuntimeError):
    """The local origin service is not live (no enrolled+connected agent), so a
    caller that requires routing should fall back to the local path."""


class LocalOriginService:
    """An in-process hub + local origin-agent, run on a background asyncio loop.

    Construct with the box's :class:`EnrollmentStore`, its
    :class:`DeviceIdentity`, and an ``engine`` (the object the origin-agent's
    executor drives — the box's own loop engine). :meth:`start` brings the hub up
    on a loopback high port, enrolls the identity, and dials it as origin
    ``origin_id``. :meth:`dispatch_start` / :meth:`dispatch_stop` are the sync,
    thread-safe entry points ``loop_start`` / ``loop_stop`` call.
    """

    def __init__(self, store: EnrollmentStore, ident: "identity.DeviceIdentity",
                 *, engine: Any, origin_id: str = "local",
                 allowlist: Optional[ProductAllowlist] = None,
                 host: str = "127.0.0.1", port: int = 0,
                 ledger_path: Optional[str] = None,
                 rpc_timeout: float = 30.0,
                 heartbeat_interval: float = 15.0,
                 status_reader: Optional[Callable[[str], list]] = None,
                 run_reader: Optional[Callable[[str], dict]] = None,
                 run_allowlist: Optional["RunAllowlist"] = None,
                 manifest: Any = None,
                 tail_interval: float = 1.0,
                 hub_ssl_context: Any = None,
                 client_ssl_context: Any = None,
                 require_tls_binding: bool = False):
        self.store = store
        # §5.1(4): optional TLS for the embedded Hub (a Hub that serves REMOTE
        # origins presents its cert + verifies theirs) and the matching client
        # context for this box's own agent. None = plaintext loopback (unchanged).
        self._hub_ssl = hub_ssl_context
        self._client_ssl = client_ssl_context
        self._require_tls_binding = require_tls_binding
        self.identity = ident
        self.engine = engine
        self.origin_id = origin_id
        # G2.4 — when a status reader is wired, a dispatched loop.start spawns a
        # background StatusTailer that streams that loop's live turn/state events
        # to the hub (so the consolidated view reflects the origin's progress).
        # Default None keeps the service a pure dispatcher (tests / back-compat).
        self._status_reader = status_reader
        self._run_reader = run_reader
        self._tail_interval = max(0.05, float(tail_interval))
        self._tail_tasks: list = []
        # default-allow the wildcard for the LOCAL origin: the box dispatching to
        # itself is not a remote-authority decision — the origin-side allowlist
        # exists to gate OTHER planes, and the local plane is the same trust
        # domain. A deployment can pass a narrower allowlist to restrict it.
        self.allowlist = allowlist or ProductAllowlist(allow=[ProductAllowlist.WILDCARD])
        # §5/§P3: arbitrary exec is OPT-IN per origin. Default None → the executor's
        # own fail-closed deny-all RunAllowlist (origin.run refused until the origin
        # owner opts specific programs in on its OWN disk). The Hub can never widen
        # it — it only lives here, on the origin side of the channel.
        self.run_allowlist = run_allowlist
        # P3 capability manifest (origin_policy.OriginManifest). None → the
        # executor loads origin-manifest.json beside a file-backed run allowlist,
        # else the default — NO exec, NO fs (two locks: manifest + RunAllowlist).
        self.manifest = manifest
        self.host = host
        self.port = port
        self._ledger_path = ledger_path
        self._rpc_timeout = rpc_timeout
        self._heartbeat_interval = heartbeat_interval
        self.device_id = ident.device_id

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._hub: Optional[OriginHub] = None
        self._client: Optional[ChannelClient] = None
        self._client_task: Optional[asyncio.Task] = None
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._stopped = False

    # ── lifecycle (sync façade over a background loop) ────────────────────────
    def start(self, *, timeout: float = 10.0) -> "LocalOriginService":
        """Bring the hub + agent up and block until the agent is connected (or
        raise). Idempotent-ish: a second call on a live service is a no-op."""
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run_loop, name=f"origin-dispatch-{self.origin_id}",
            daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise DispatchUnavailable(
                f"local origin service did not become ready in {timeout}s")
        if self._start_error is not None:
            raise self._start_error
        return self

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_start())
        except BaseException as e:  # noqa: BLE001 — surface to start()
            self._start_error = e
            self._ready.set()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()

    async def _async_start(self) -> None:
        # S-0: never poll this box's own origin for its inventory — its loops
        # are the local data dir the read path already scans.
        self._hub = OriginHub(self.store, ssl_context=self._hub_ssl,
                              require_tls_binding=self._require_tls_binding,
                              inventory=True,
                              inventory_exclude=(self.device_id,))
        self.port = await self._hub.start(self.host, self.port)
        # enroll THIS box's device key (pubkey only on the plane) through the real
        # pairing-code flow, so the local origin authenticates exactly like a
        # remote one would.
        if self.store.get_device(self.device_id) is None:
            code = self.store.issue_pairing_code(now=_t(), label=self.origin_id)["code"]
            self.store.claim_pairing_code(code, self.identity.public_key_b64,
                                          now=_t())
        reporter = CapabilityReporter(
            self.origin_id, engine=self.engine, allowlist=self.allowlist)
        executor = OriginAgentExecutor(
            self.origin_id, engine=self.engine, allowlist=self.allowlist,
            reporter=reporter, run_allowlist=self.run_allowlist,
            manifest=self.manifest)
        ledger = DispatchLedger(self._ledger_path)
        self._client = ChannelClient(
            self.identity, self.host, self.port, origin_id=self.origin_id,
            executor=executor, ledger=ledger,
            capabilities_fn=lambda: reporter.heartbeat_extras(),
            heartbeat_interval=self._heartbeat_interval,
            backoff_initial=0.05, backoff_max=1.0,
            **({"ssl_context": self._client_ssl} if self._client_ssl else {}))
        self._client_task = asyncio.ensure_future(self._client.run_forever())
        await self._client.wait_connected(timeout=5.0)
        # let the hub register the live session + push origin.describe caps so the
        # registry knows this origin's logical id before the first dispatch.
        for _ in range(100):
            if self._hub.server.is_live(self.device_id):
                break
            await asyncio.sleep(0.01)
        await self._prime_capabilities()

    async def _prime_capabilities(self) -> None:
        """Ask the freshly-connected origin to describe itself so the registry
        records its logical origin id + capabilities (drives the Origins view /
        first_enrolled_origin resolution)."""
        try:
            desc = await self._hub.router.call(self.device_id, "origin.describe")
            caps = dict(desc.get("capabilities", {}))
            caps.setdefault("origin", self.origin_id)
            self._hub.registry.set_capabilities(self.device_id, caps)
        except Exception:  # noqa: BLE001 — describe is best-effort priming
            pass

    def stop(self, *, timeout: float = 5.0) -> None:
        if self._loop is None or self._stopped:
            self._stopped = True
            return
        self._stopped = True

        async def _shutdown():
            for task in self._tail_tasks:          # G2.4 — stop live tailers first
                task.cancel()
            self._tail_tasks = []
            if self._client is not None:
                self._client.stop()
            if self._client_task is not None:
                self._client_task.cancel()
                try:
                    await self._client_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if self._hub is not None:
                await self._hub.stop()

        fut = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
        try:
            fut.result(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ── liveness ──────────────────────────────────────────────────────────────
    def is_live(self) -> bool:
        """True iff the local origin-agent is enrolled AND holds a live channel
        to the hub (so a dispatch will actually reach an engine)."""
        if self._hub is None or self._stopped:
            return False
        try:
            return bool(self._hub.server.is_live(self.device_id))
        except Exception:  # noqa: BLE001
            return False

    def target_origin_id(self) -> str:
        return self.origin_id

    # ── the sync dispatch entry points (called from loop_start/loop_stop) ─────
    def dispatch_start(self, name: str, slug: str = "") -> dict:
        """Route a ``loop.start`` to the enrolled local origin over the channel
        with a FRESH dispatch-id (each user intent is its own dispatch; network
        redelivery of THAT id re-attaches via the ledger). Returns the origin's
        engine result, annotated so the caller can SEE it routed via the protocol
        (``routed``/``dispatchId``/``origin``) rather than the old hardcode."""
        if not self.is_live():
            raise DispatchUnavailable("local origin agent is not connected")
        did = str(uuid.uuid4())
        res = self._submit(self._hub.router.start(
            self.device_id, name, slug=slug or None, dispatch_id=did))
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out.setdefault("name", name)
        out["routed"] = True
        out["dispatchId"] = did
        out["origin"] = self.origin_id
        # G2.4 — start streaming this loop's live progress to the hub. Skip a
        # re-attached redelivery (the run — and its tailer — already exist) AND a
        # start that returned an error (no run.json will ever appear, so the tailer
        # would poll forever — a leaked task).
        if not out.get("error") and not out.get("_reattached"):
            self._spawn_tailer(name)
        return out

    def dispatch_start_remote(self, name: str, origin: str, *, slug: str = "",
                              config: Optional[dict] = None) -> dict:
        """Gap #3 — start ``name`` on a REMOTE connected ``origin``'s OWN engine
        over the channel (fresh dispatch-id; the origin's ledger re-attaches a
        redelivery). ``config`` ships the Hub's saved loop config so an origin
        that lacks the loop prepares it first (its own copy wins; its §6.4
        Project gate + the manifest's ``agent`` verb still apply). No Hub-side
        tailer: the remote origin streams its own progress back
        (``OriginAgentExecutor.tail_client``). Raises
        :class:`DispatchUnavailable` for an unknown / not-connected origin — the
        caller must NOT fall back to running it locally."""
        device_id = self.resolve_device(origin)
        if device_id == self.device_id:
            raise DispatchUnavailable(
                f"origin {origin!r} is this box; use dispatch_start")
        did = str(uuid.uuid4())
        res = self._submit(self._hub.router.start(
            device_id, name, slug=slug or None, dispatch_id=did, config=config))
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out.setdefault("name", name)
        out["routed"] = True
        out["dispatchId"] = did
        out["origin"] = origin
        out["deviceId"] = device_id
        return out

    def dispatch_status_remote(self, name: str, origin: str, *,
                               tail: int = 10) -> dict:
        """Read ``name``'s status straight off a REMOTE origin's engine over the
        channel (read-only ``loop.status``) — the Hub's view of a loop that runs
        there. Raises :class:`DispatchUnavailable` when not connected."""
        device_id = self.resolve_device(origin)
        res = self._submit(self._hub.router.call(
            device_id, "loop.status", {"name": name, "tail": tail}))
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out["origin"] = origin
        out["via"] = "origin-channel"
        return out

    def dispatch_stop_remote(self, name: str, origin: str) -> dict:
        """Stop ``name`` on a REMOTE connected ``origin``'s OWN engine over the
        channel (``loop.stop`` — de-escalation, never manifest-gated). Raises
        :class:`DispatchUnavailable` when not connected; never stops locally."""
        device_id = self.resolve_device(origin)
        if device_id == self.device_id:
            raise DispatchUnavailable(
                f"origin {origin!r} is this box; use dispatch_stop")
        res = self._submit(self._hub.router.stop(device_id, name))
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out.setdefault("name", name)
        out.update(routed=True, origin=origin, deviceId=device_id,
                   via="origin-channel")
        return out

    def remote_events(self, origin: str) -> list:
        """The live events a connected ``origin`` pushed to this Hub (the
        consolidator's per-origin stream) — read on the Hub's own loop."""
        if self._hub is None or self._loop is None or self._stopped:
            return []

        async def _ev() -> list:
            return list(self._hub.consolidator.events_for(origin))

        try:
            return self._submit(_ev())
        except Exception:  # noqa: BLE001
            return []

    def dispatch_stop(self, name: str) -> dict:
        if not self.is_live():
            raise DispatchUnavailable("local origin agent is not connected")
        res = self._submit(self._hub.router.stop(self.device_id, name))
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out.setdefault("name", name)
        out["routed"] = True
        out["origin"] = self.origin_id
        return out

    # ── P2: cross-origin exec + fs compositions (agent-native, over the hub) ──
    def resolve_device(self, origin: Optional[str] = None) -> str:
        """Map an ``origin`` id/label to a LIVE device id ON THIS HUB. Empty / the
        local ids resolve to THIS box's own device; any other value is matched
        against the registry's connected devices by deviceId or label. Raises
        :class:`DispatchUnavailable` with a CLEAR reason when the origin is unknown
        or enrolled-but-not-live (the tool surfaces that verbatim, §8-P2)."""
        if self._hub is None or self._stopped:
            raise DispatchUnavailable("origin agent is not connected")
        if not origin or origin in (self.origin_id, "local", "LOCAL"):
            return self.device_id
        # matches deviceId, label, or the logical origin id reported in
        # origin.describe — shared with the hub_serve control socket.
        from mcp_loops.origin_proto.hub_control import resolve_live_device
        return resolve_live_device(self._hub, origin)

    def policy_view(self, origin: Optional[str] = None) -> dict:
        """P3: what the Hub will ENFORCE for ``origin`` — read-only.

        Unlike :meth:`resolve_device` an enrolled-but-offline origin resolves too
        (its last stored manifest is shown, ``live: False``). The source of truth
        is the live session's manifest when connected, else the post-auth
        ``EnrollmentStore`` sidecar, else the describe-only fallback (``status:
        missing``). ``owner`` is the ENROLLED owner — never the manifest's claim.
        ``allowedVerbs`` is the flat list of units the Hub would let through."""
        from mcp_loops.origin_proto import hub_control
        if self._hub is None or self._stopped:
            raise DispatchUnavailable("origin agent is not connected")
        if not origin or origin in (self.origin_id, "local", "LOCAL"):
            did = self.device_id
        else:
            did = None
            # connected-this-process (registry) first, then any ENROLLED device
            for entry in list(self._hub.registry.list()) + self.store.list_devices():
                if origin in (entry.get("deviceId"), entry.get("label")):
                    did = entry.get("deviceId")
                    break
            if did is None:
                raise DispatchUnavailable(f"origin {origin!r} is not an enrolled origin")
        return hub_control.policy_view(self._hub, did, origin or self.origin_id)

    def dispatch_run(self, argv: Any, *, origin: Optional[str] = None,
                     stdin: Optional[str] = None, cwd: Optional[str] = None,
                     env: Optional[dict] = None, timeout: Optional[float] = None,
                     cap_token: Optional[str] = None,
                     dispatch_id: Optional[str] = None) -> dict:
        """Route ``origin.run`` to a live origin over the channel and return its
        result envelope (annotated ``routed``/``origin``). The origin's own
        fail-closed ``RunAllowlist`` + the §5.1(4) mTLS gate still apply — an
        un-permitted verb or an unbound non-loopback origin comes back as a typed
        error, surfaced rather than swallowed."""
        device_id = self.resolve_device(origin)
        block = None if timeout is None else float(timeout) + self._rpc_timeout + 5.0
        res = self._submit(self._hub.router.run(
            device_id, list(argv), stdin=stdin, cwd=cwd, env=env,
            timeout=timeout, cap_token=cap_token, dispatch_id=dispatch_id),
            block_timeout=block)
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out["routed"] = True
        out["origin"] = origin or self.origin_id
        return out

    def dispatch_fs_pull(self, remote_path: str, *, origin: Optional[str] = None,
                         timeout: Optional[float] = None,
                         cap_token: Optional[str] = None) -> dict:
        """Pull ``remote_path`` from a live origin (B→A) as a composition over
        ``origin.run`` (:mod:`fs_ops`). Returns the fs_ops envelope with ``data``
        base64-encoded for the JSON tool boundary."""
        from mcp_loops.origin_proto import fs_ops
        device_id = self.resolve_device(origin)
        block = None if timeout is None else float(timeout) + self._rpc_timeout + 5.0
        res = self._submit(fs_ops.fs_pull(
            self._hub.router, device_id, remote_path, timeout=timeout,
            cap_token=cap_token), block_timeout=block)
        out = dict(res)
        data = out.pop("data", b"")
        out["dataB64"] = base64.b64encode(data).decode("ascii")
        out["origin"] = origin or self.origin_id
        return out

    def dispatch_fs_push(self, remote_path: str, data: Any, *,
                         origin: Optional[str] = None,
                         timeout: Optional[float] = None,
                         cap_token: Optional[str] = None) -> dict:
        """Push ``data`` to ``remote_path`` on a live origin (A→B) as a composition
        over ``origin.run`` (:mod:`fs_ops`), with ``tee``-echo integrity check."""
        from mcp_loops.origin_proto import fs_ops
        device_id = self.resolve_device(origin)
        block = None if timeout is None else float(timeout) + self._rpc_timeout + 5.0
        res = self._submit(fs_ops.fs_push(
            self._hub.router, device_id, remote_path, data, timeout=timeout,
            cap_token=cap_token), block_timeout=block)
        out = dict(res)
        out["origin"] = origin or self.origin_id
        return out

    def dispatch_onboarding_check(self, required: Any, *,
                                  origin: Optional[str] = None,
                                  timeout: Optional[float] = None) -> dict:
        """Run the LIVE §6.4 onboarding check against a connected origin: probe its
        real, fresh auth-aware capability picture over the channel and diff it
        against ``required`` (the CLIs a set of loops needs), returning the readiness
        verdict + concrete actions. The Hub audits the check (``A_ONBOARDING``)."""
        device_id = self.resolve_device(origin)
        block = None if timeout is None else float(timeout) + self._rpc_timeout + 5.0
        res = self._submit(
            self._hub.router.onboarding_check(device_id, required),
            block_timeout=block)
        out = dict(res) if isinstance(res, dict) else {"result": res}
        out.setdefault("origin", origin or self.origin_id)
        return out

    def dispatch_capabilities_probe(self, origin: Optional[str] = None, *,
                                    timeout: Optional[float] = None) -> dict:
        """Ask a LIVE origin for its own subscription-CLI probe over the channel
        (``capabilities.probe`` — read-only, manifest-gated at the Hub AND the
        origin). Returns the origin's ``{ok, cliCapabilities, lastProbed}``
        annotated with ``origin``/``deviceId``/``via``. Raises
        :class:`DispatchUnavailable` for an unknown / not-connected origin and the
        channel's typed errors (e.g. a manifest that doesn't advertise the rpc)
        so the caller can surface the reason verbatim."""
        device_id = self.resolve_device(origin)
        block = timeout if timeout is not None else self._rpc_timeout + 5.0
        res = self._submit(self._hub.router.call(device_id, "capabilities.probe"),
                           block_timeout=block)
        out = dict(res) if isinstance(res, dict) else {"ok": False, "result": res}
        out["origin"] = origin or self.origin_id
        out["deviceId"] = device_id
        out["via"] = "origin-channel"
        return out

    # ── live status tailing (G2.4 — a running loop's progress → the hub) ──────
    def _spawn_tailer(self, name: str) -> None:
        """Start a background :class:`StatusTailer` for ``name`` on the hub's event
        loop (best-effort). No-op when no status reader is wired or the service is
        down. The task streams the loop's turn/state events until the run is
        terminal, then flushes a last poll and exits."""
        if self._status_reader is None or self._loop is None or self._stopped:
            return

        async def _tail() -> None:
            from mcp_loops.origin_proto.agent_core import StatusTailer
            tailer = StatusTailer(
                self.origin_id, name,
                read_status=lambda: self._status_reader(name),
                read_run=(lambda: self._run_reader(name)) if self._run_reader else None)
            while not self._stopped:
                try:
                    await tailer.poll(self._client)
                except Exception:  # noqa: BLE001 — a tail hiccup never kills the run
                    pass
                if tailer.done():
                    break
                await asyncio.sleep(self._tail_interval)
            try:
                await tailer.poll(self._client)   # final flush of the terminal state
            except Exception:  # noqa: BLE001
                pass

        def _schedule() -> None:
            self._tail_tasks.append(self._loop.create_task(_tail()))

        self._loop.call_soon_threadsafe(_schedule)

    # ── live consolidation snapshot (G2.1 — the dashboard read path) ──────────
    def consolidated_snapshot(self, *, mirror_root: Optional[str] = None) -> dict:
        """A plain-data snapshot of the LIVE consolidation for the dashboard read
        path (G2.1): ``{origins: [...], loops: [...]}`` computed ON the hub's own
        event loop so the registry + consolidator are read WITHOUT a cross-thread
        race with the ingest that mutates them.

        ``origins`` are :func:`control_plane.unified_origins` (a live-enrolled
        origin SUPERSEDES its rsync mirror, health from heartbeats); ``loops`` are
        every connected origin's loops — its cached ``loop.list`` inventory with
        the event stream folded over it (:func:`control_plane.live_loops`, S-0)
        — the caller fuses these OVER
        its own mirror scan (a live ``(origin, name)`` supersedes the mirror row).
        Returns empty lists when the hub isn't up, so the caller cleanly falls
        back to the pure-mirror view."""
        from mcp_loops.origin_proto import control_plane
        if self._hub is None or self._stopped or self._loop is None:
            return {"origins": [], "loops": []}

        async def _snap() -> dict:
            return control_plane.hub_snapshot(self._hub, mirror_root=mirror_root)

        try:
            return self._submit(_snap())
        except Exception:  # noqa: BLE001 — a snapshot hiccup falls back to mirror
            return {"origins": [], "loops": []}

    def _submit(self, coro, *, block_timeout: Optional[float] = None) -> Any:
        """Run a channel coroutine on the background loop and block for its
        result (the MCP tool thread is synchronous). ``block_timeout`` overrides
        the default wait for a long ``origin.run`` whose own budget exceeds the
        RPC timeout — the caller passes ``run timeout + margin`` so the origin's
        timeout wins the race instead of this waiter giving up early."""
        if self._loop is None:
            raise DispatchUnavailable("dispatch loop is not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        wait = block_timeout if block_timeout is not None else self._rpc_timeout + 5.0
        return fut.result(timeout=wait)


def _t() -> float:
    # a tiny indirection so the module has ONE time source (kept import-cheap; the
    # channel/registry use time.time directly for liveness).
    import time
    return time.time()
