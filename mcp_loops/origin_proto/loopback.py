"""loopback — an in-process fake-origin harness (the §8 testing strategy).

Stands up the WHOLE spine on localhost with generated throwaway keys and no real
hardware: an EnrollmentStore, a ChannelServer on an ephemeral high port, a
DeviceIdentity enrolled through a pairing code, and a ChannelClient dialing back
in with a pluggable RPC executor. Used by the conformance / loopback / chaos
tests and by the by-hand proof script, so both exercise the SAME enroll →
channel → dispatch path a real origin would.

Nothing here touches production data, real secrets, or the network beyond
127.0.0.1 (isolation rules): the port is ephemeral (bind 0), the keys are
generated per-harness, and the data dir is caller-provided (a tmp dir).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from mcp_loops.origin_proto import identity, wire
from mcp_loops.origin_proto.channel import ChannelClient, ChannelServer, DispatchLedger
from mcp_loops.origin_proto.enrollment import EnrollmentStore


class FakeEngine:
    """A tiny stand-in for the origin's local loop engine, so the transport can
    be exercised without the real worker daemon. Counts loop.start calls PER
    dispatch-id so a test can prove idempotency (no double-run). The real origin
    agent (the other engineer's G2) replaces this with a LoopsMCP executor to the
    live engine at 127.0.0.1:8771."""

    def __init__(self, origin_id: str = "local"):
        self.origin_id = origin_id
        self.loops: dict[str, dict] = {
            "demo": {"name": "demo", "state": "idle"},
        }
        self.start_calls: list[str] = []  # dispatch-ids actually executed
        self.started: dict[str, dict] = {}

    async def execute(self, method: str, params: dict) -> dict:
        if method == "loop.list":
            return {"loops": [{"name": n, "state": v["state"]}
                              for n, v in sorted(self.loops.items())]}
        if method == "loop.get":
            name = params.get("name")
            loop = self.loops.get(name)
            if loop is None:
                raise ValueError(f"unknown loop {name!r}")
            return {"config": {"name": name}, "run": {"state": loop["state"]}}
        if method == "loop.start":
            name = params.get("name")
            if name not in self.loops:
                raise ValueError(f"unknown loop {name!r}")
            # record the EXECUTION (the ledger guarantees this runs once per id)
            self.loops[name]["state"] = "running"
            run = {"ok": True, "name": name, "state": "running"}
            self.started[name] = run
            return run
        if method == "loop.stop":
            name = params.get("name")
            if name in self.loops:
                self.loops[name]["state"] = "idle"
            return {"ok": True, "name": name}
        if method == "loop.status":
            name = params.get("name")
            loop = self.loops.get(name, {})
            return {"run": {"state": loop.get("state", "unknown")}, "recent": []}
        if method == "products.list":
            return {"products": [{"id": "self", "kind": "git"}]}
        if method == "origin.describe":
            return {"origin": self.origin_id,
                    "capabilities": {"clis": ["claude"], "cores": 2},
                    "harness": {"protocol": wire.PROTOCOL_VERSION},
                    "products": ["self"]}
        raise ValueError(f"unhandled method {method!r}")


class Harness:
    """Everything wired together. ``await start()`` then use ``.server`` to
    dispatch and ``.engine`` to inspect. ``await stop()`` to tear down."""

    def __init__(self, root: str, *, origin_id: str = "local",
                 executor: Optional[Callable[[str, dict], Awaitable[dict]]] = None,
                 now_fn: Callable[[], float] = time.time,
                 backoff_initial: float = 0.05, backoff_max: float = 0.5,
                 server_ssl: Any = None, client_ssl: Any = None,
                 require_tls_binding: bool = False):
        self.root = root
        self.origin_id = origin_id
        self.now_fn = now_fn
        self.engine = FakeEngine(origin_id)
        self._executor = executor or self.engine.execute
        self.events: list[dict] = []  # events the hub ingested (per §7 sink)
        self.store = EnrollmentStore(root, on_revoke=None)
        # G2.5: optional TLS. Default None keeps the plaintext-loopback path the
        # in-process dogfood + existing tests use; a TLS test passes contexts from
        # mcp_loops.origin_proto.tls to exercise the encrypted network hop.
        self._client_ssl = client_ssl
        self.server = ChannelServer(
            self.store, on_event=lambda dev, frame: self.events.append(frame),
            ssl_context=server_ssl, require_tls_binding=require_tls_binding,
            now_fn=now_fn)
        self.identity = identity.DeviceIdentity.generate(label="loopback-origin")
        self.device_id = self.identity.device_id
        self.client: Optional[ChannelClient] = None
        self.port: Optional[int] = None
        self._client_task: Optional[asyncio.Task] = None
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max

    def enroll(self) -> dict:
        """Enroll the device through a pairing code (the real §5 flow), recording
        only its PUBLIC key. Returns the device record."""
        now = self.now_fn()
        code = self.store.issue_pairing_code(now=now, label="loopback")["code"]
        return self.store.claim_pairing_code(
            code, self.identity.public_key_b64, now=now)

    async def start(self, *, run_forever: bool = True,
                    ledger: Optional[DispatchLedger] = None) -> "Harness":
        self.port = await self.server.start("127.0.0.1", 0)
        self.enroll()
        self.client = ChannelClient(
            self.identity, "127.0.0.1", self.port, origin_id=self.origin_id,
            executor=self._executor, ledger=ledger,
            ssl_context=self._client_ssl,
            backoff_initial=self._backoff_initial,
            backoff_max=self._backoff_max)
        if run_forever:
            self._client_task = asyncio.ensure_future(self.client.run_forever())
            await self.client.wait_connected(timeout=5.0)
            # give the server a beat to register the live session
            for _ in range(50):
                if self.server.is_live(self.device_id):
                    break
                await asyncio.sleep(0.01)
        return self

    async def stop(self) -> None:
        if self.client is not None:
            self.client.stop()
        if self._client_task is not None:
            self._client_task.cancel()
            try:
                await self._client_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.server.stop()

    async def __aenter__(self) -> "Harness":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()
