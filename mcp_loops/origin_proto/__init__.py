"""origin_proto — the Origins Phase-0 SPINE: a production-grade, uniform origin
protocol and secure channel (docs/ORIGINS-PRODUCTION-DESIGN.md §3-§8).

The control plane (hub/dashboard) is SEPARATE from origins (boxes that run their
own loop engine). Every origin — the VPS included — connects to the hub through
the SAME protocol; the hub NEVER dispatches work by copying it, it sends a
capability-scoped RPC to the origin whose OWN engine runs it locally. The hub
holds no model keys and no origin secrets (§3 ownership boundary).

This package is the transport + protocol + enrollment/identity spine that both
the origin-agent core and the control-plane registry/router sit on top of:

  wire       — G1: the typed RPC + event wire protocol, version handshake, the
               capability-scoped RPC allowlist (NO arbitrary shell), the event
               kinds, dispatch-id idempotency keys, and conformance validators.
  identity   — G4: the device keypair (Ed25519). The private key is generated
               locally and NEVER leaves the box; the plane records only the
               public key + a human label.
  enrollment — G4/G5-edge: control-plane enrollment (pairing code / device flow),
               the device registry (public keys + labels), revocation (denylist
               checked on every reconnect + per-RPC) and the audit log.
  wsio       — the raw asyncio+wsproto WebSocket primitive (persistent, outbound).
  hub_tunnel — dial a Hub through a front door's /hub byte tunnel (the Hub's own
               pinned TLS runs inside it, end to end).
  channel    — G3: the secure channel. The origin DIALS OUT and holds a
               long-lived connection (no inbound ports). Authenticated by the
               device keypair via a replay-proof challenge/response handshake;
               protocol-version handshake; reconnect with exponential backoff;
               at-least-once delivery with dispatch-id idempotency so a reconnect
               RE-ATTACHES to an in-flight run instead of double-running.
  agent_core — G2: the origin-agent core. The FULL capability-scoped RPC executor
               bridged to the box's OWN live engine (mcp_loops.client.LoopsMCP),
               the capability reporter (CLIs+versions, cores, RAM, load, reachable
               Products), and the origin-side Product ALLOWLIST that gates which
               Products the plane may start (§6.4). This is the origin end.
  control_plane — G5: the hub side. The origin registry (live devices +
               capabilities + heartbeat-derived health), the event consolidator
               (per-origin namespace, unified view rendered FROM events), the
               dispatch router (scoped loop.start to the owning origin), and the
               fusion that lets a live-enrolled origin SUPERSEDE its rsync mirror.

Round-1 scope is the hardened spine dogfooded LOCALLY (loopback, no cross-machine
networking yet). The rsync-mirror aggregation (mcp_loops.origins) STAYS as the
fallback for not-yet-enrolled origins; a live-enrolled origin supersedes its
mirror.
"""

from __future__ import annotations

from mcp_loops.origin_proto import wire

PROTOCOL_VERSION = wire.PROTOCOL_VERSION

__all__ = ["wire", "PROTOCOL_VERSION"]
