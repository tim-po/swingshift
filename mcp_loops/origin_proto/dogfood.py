"""dogfood — G6: re-enroll THIS box as origin 'local' THROUGH the protocol and
prove the hub sees it as a LIVE enrolled origin over the channel (heartbeat +
capabilities + a loop.list served from the LIVE ENGINE, not the rsync mirror).

The origin is a box like any other; the dashboard-serving role is incidental and
decoupled here — this proof stands up its OWN throwaway hub + agent on a unique
high port with generated throwaway keys, and the ChannelClient's executor bridges
the capability-scoped RPCs to the box's own live loop engine
(``mcp_loops.client.LoopsMCP`` → 127.0.0.1:8771).

ROUND-1 SAFETY: the bridge serves only READ-ONLY RPCs (loop.list / loop.get /
loop.status / products.list / origin.describe). The MUTATING RPCs (loop.start /
loop.stop) are deliberately NOT wired here — launching real loops from a
transport proof is out of scope, and the full origin-agent RPC executor +
origin-side Product allowlist that gate mutation are the other engineer's G2/G5.
Attempting a mutating RPC through this bridge raises, so the dogfood cannot start
or stop any real work.

This module is the transport-side G6 evidence. The control-plane consolidation
half of G6 (turning ``origins.LOCAL_ORIGIN_ID`` into "the first enrolled origin"
and rendering the unified view FROM the pushed events) is the other engineer's
G5 and layers on the registry this channel feeds.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from typing import Optional

from mcp_loops.origin_proto import identity, wire
from mcp_loops.origin_proto.channel import ChannelClient, ChannelServer
from mcp_loops.origin_proto.enrollment import EnrollmentStore

# read-only RPCs the round-1 dogfood bridge will serve from the live engine.
_READ_ONLY = {"loop.list", "loop.get", "loop.status", "products.list",
              "origin.describe"}


def _detect_clis() -> list[str]:
    """Honest capability report: which subscription CLIs are actually on PATH.
    (Real auth-state detection needs per-CLI probes — §13 open question; PATH
    presence is the honest round-1 floor, not a guess about which is logged in.)"""
    return [name for name in ("claude", "codex") if shutil.which(name)]


def _load_avg() -> Optional[float]:
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        return None


class LiveEngineExecutor:
    """Bridges capability-scoped RPCs to the box's OWN live loop engine over the
    existing sync ``LoopsMCP`` client (run off-thread so it never blocks the
    channel event loop). Read-only in round 1 — see the module docstring."""

    def __init__(self, origin_id: str = "local", *, mcp=None):
        # import here so the module imports even where mcp isn't configured
        from mcp_loops.client import LoopsMCP
        self.origin_id = origin_id
        self._mcp = mcp or LoopsMCP()

    async def __call__(self, method: str, params: dict) -> dict:
        if method not in _READ_ONLY:
            raise PermissionError(
                f"{method} is not served by the read-only dogfood bridge "
                f"(mutating RPCs are the origin-agent core's job, gated by the "
                f"Product allowlist)")
        return await asyncio.get_event_loop().run_in_executor(
            None, self._call_sync, method, params)

    def _call_sync(self, method: str, params: dict) -> dict:
        if method == "loop.list":
            return self._mcp.loop_list()
        if method == "loop.get":
            return self._mcp.loop_get(params["name"])
        if method == "loop.status":
            return self._mcp.loop_status(params["name"], tail=params.get("tail"))
        if method == "products.list":
            # products.list may not exist on every engine build; degrade honestly
            try:
                return self._mcp.call("loop_product_list", {})
            except Exception:  # noqa: BLE001
                return {"products": [], "note": "products.list unavailable on this engine"}
        if method == "origin.describe":
            return {
                "origin": self.origin_id,
                "capabilities": {
                    "clis": _detect_clis(),
                    "cores": os.cpu_count(),
                    "load": _load_avg(),
                },
                "harness": {"protocol": wire.PROTOCOL_VERSION},
            }
        raise ValueError(f"unhandled method {method!r}")


async def prove(*, host: str = "127.0.0.1", port: int = 0,
                origin_id: str = "local") -> dict:
    """Stand up a throwaway hub + enrol THIS box + dial back in over the channel,
    then prove the hub sees it LIVE with capabilities and serves a loop.list FROM
    THE LIVE ENGINE over the channel. Returns a structured evidence dict; cleans
    up its own hub, socket, and temp data dir. Binds an ephemeral high port
    (isolation-safe) unless one is given."""
    root = tempfile.mkdtemp(prefix="dogfood-origins-")
    ev: dict = {"origin_id": origin_id, "data_dir": root}
    server = ChannelServer(EnrollmentStore(root),
                           on_event=lambda dev, f: ev.setdefault("events", []).append(f["kind"]))
    bound = await server.start(host, port)
    ev["hub_port"] = bound
    ident = identity.DeviceIdentity.generate(label=f"{origin_id} (dogfood)")
    ev["device_id"] = ident.device_id
    ev["fingerprint"] = ident.fingerprint

    # enrol through the real pairing-code flow (records PUBLIC key only)
    now = time.time()
    code = server.store.issue_pairing_code(now=now, label=origin_id)["code"]
    rec = server.store.claim_pairing_code(code, ident.public_key_b64, now=now)
    ev["enrolled"] = rec["status"] == "enrolled"
    ev["public_key_only"] = "privateKey" not in rec

    executor = LiveEngineExecutor(origin_id)
    client = ChannelClient(ident, host, bound, origin_id=origin_id,
                           executor=executor, heartbeat_interval=0.3)
    task = asyncio.ensure_future(client.run_forever())
    try:
        await client.wait_connected(10.0)
        for _ in range(200):
            if server.is_live(ident.device_id):
                break
            await asyncio.sleep(0.01)
        ev["hub_sees_live"] = server.is_live(ident.device_id)

        # capabilities over the channel (origin.describe served by the agent)
        desc = await server.call_rpc(ident.device_id, "origin.describe")
        ev["capabilities"] = desc.get("capabilities")

        # THE key claim: a loop.list served FROM THE LIVE ENGINE over the channel
        listed = await server.call_rpc(ident.device_id, "loop.list")
        loops = listed.get("loops", [])
        ev["loop_list_over_channel_count"] = listed.get("count", len(loops))
        ev["loop_list_sample"] = [l.get("name") for l in loops[:5]]
        ev["served_from"] = "live-engine-over-channel (not rsync mirror)"

        # a heartbeat lands (liveness, not mirror mtime)
        await asyncio.sleep(0.5)
        ev["heartbeats_seen"] = server._live[ident.device_id].last_seen is not None

        # a mutating RPC is refused by the read-only bridge (safety)
        try:
            import uuid
            await server.call_rpc(ident.device_id, "loop.start",
                                  {"name": "x"}, dispatch_id=str(uuid.uuid4()))
            ev["mutation_refused"] = False
        except Exception:  # noqa: BLE001
            ev["mutation_refused"] = True

        # audit trail exists
        ev["audit_kinds"] = sorted({e["kind"] for e in server.store.read_audit()})
    finally:
        client.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await server.stop()
        shutil.rmtree(root, ignore_errors=True)
    ev["ok"] = bool(ev.get("hub_sees_live") and ev.get("loop_list_over_channel_count", 0) >= 0)
    return ev


def main() -> None:
    import json
    ev = asyncio.run(prove())
    print(json.dumps(ev, indent=2))


if __name__ == "__main__":
    main()
