"""hub_serve — M3: the Hub façade (``OriginHub``) as its OWN runnable process.

Phase A / **M3** (HUB-ENGINE-SPLIT-SPEC §4 M3, §5 Phase A). Through M0–M2 the
Hub lived *inside* the engine's process — :class:`LocalOriginService`
(``origin_proto.dispatch``) embeds an :class:`OriginHub` on a loopback high port
and dials it in-process. M3 packages the SAME ``OriginHub`` façade (ChannelServer
+ OriginRegistry + EventConsolidator + DispatchRouter, ``control_plane.py:503``)
as a **standalone daemon** an origin dials over the shared ``origin_wire``
:class:`ChannelClient`. Nothing about the Hub's logic changes — this module only
gives it a ``main()`` + a ``serve_forever`` lifecycle so it can run on its own box
while each engine stays a pure per-origin loop authority.

**Standalone guarantee (§3) is UNCHANGED and is the whole point.** This daemon is
opt-in: the engine dials a Hub ONLY when

  * ``LOOPYARD_ORIGIN_AGENT`` is set → the in-process **loopback** Hub the VPS's
    own ``local`` origin uses (``origin_service.start`` / ``LocalOriginService``,
    the M0–M2 path — unchanged), OR
  * ``LOOPYARD_ORIGIN_HUB_URL`` is set → an **external** Hub daemon like this one,
    dialed via :mod:`mcp_loops.origin_client` (``loopyard origin up --hub <url>``).

Default deploy sets **neither** → Hub OFF → ``loop_start`` fails OPEN to
``_loop_start_local`` and the engine runs loops byte-for-byte as the pre-split
path (§3). :func:`mcp_loops.origin_service.hub_mode` is the single, tested place
that decision lives.

TLS (§5.1(4)): a loopback dev Hub runs plaintext (the default, exactly as
``OriginHub`` and the in-process ``LocalOriginService`` do). A Hub that serves a
real remote origin turns the mTLS hard gate on by minting a device cert for its
own identity (``--tls`` / passing an identity) and requiring the tunnel be bound
to an enrolled device key — the same ``tls.server_ssl_context`` +
``require_tls_binding`` the ``OriginHub`` already threads through.

This module carries NO engine policy and holds NO origin secrets — it only stands
up the broker. It is re-exported as :mod:`hub.serve` (the logical Hub package,
§1b); it must never be imported by ``origin_wire`` (the M1 boundary check in
``test_package_boundaries`` enforces that direction).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from typing import Callable, Optional

from mcp_loops.origin_proto import identity as _identity
from mcp_loops.origin_proto.control_plane import OriginHub
from mcp_loops.origin_proto.enrollment import EnrollmentStore

# Where an external Hub keeps its enrollment store + (optional) device identity.
# A `_hub` dir mirrors origin_service's `_origin` convention (prefixed `_` so it
# is never mistaken for a loop) but names the ROLE this box plays.
DEFAULT_STATE_DIRNAME = "_hub"


def default_state_dir(data_dir: Optional[str] = None) -> str:
    """The Hub's own state dir. Resolves under ``LOOPS_DATA_DIR`` (via the engine's
    ``paths`` resolver) so a co-located dev Hub and engine don't collide, with a
    plain ``./_hub`` fallback if the engine package isn't importable (a Hub-only
    bundle)."""
    if data_dir:
        return os.path.join(data_dir, DEFAULT_STATE_DIRNAME)
    try:
        from mcp_loops import paths
        return os.path.join(paths.resolve_data_dir(), DEFAULT_STATE_DIRNAME)
    except Exception:  # noqa: BLE001 — Hub-only install: no engine data dir
        return os.path.abspath(DEFAULT_STATE_DIRNAME)


HUB_CERT_FILENAME = "hub-cert.pem"


def load_tls_material(state_dir: str):
    """Mint (once) this Hub's device cert and PERSIST it, so the Hub presents the
    SAME cert — and the same fingerprint — across restarts (§5.1(4)). A box pins
    the exact cert it was handed at pairing, and ``mint_device_cert`` uses a random
    serial, so re-minting on every start would break every pinned origin. The
    stored cert is reused only while it still carries this identity's key."""
    from mcp_loops.origin_proto import tls
    root = os.path.join(state_dir, "_identity")
    os.makedirs(root, mode=0o700, exist_ok=True)
    ident = _identity.DeviceIdentity.load_or_create(
        os.path.join(root, "device.json"), label="hub")
    material = tls.mint_device_cert(ident)
    cert_path = os.path.join(root, HUB_CERT_FILENAME)
    try:
        with open(cert_path, "rb") as fh:
            stored = fh.read()
    except OSError:
        stored = None
    if (stored and tls.is_self_signed_ed25519(stored)
            and tls.pubkey_b64_from_cert_pem(stored) == ident.public_key_b64):
        material.cert_pem = stored
    else:
        tmp = cert_path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(material.cert_pem)
        os.replace(tmp, cert_path)
    return material


def _build_tls_context(state_dir: str):
    """A server ssl_context presenting this Hub's persisted cert."""
    from mcp_loops.origin_proto import tls
    return tls.server_ssl_context(load_tls_material(state_dir))


def hub_fingerprint(state_dir: str) -> str:
    """The fingerprint of the cert a ``--tls`` Hub over ``state_dir`` presents —
    the out-of-band value a joining box passes as ``--hub-fingerprint``."""
    from mcp_loops.origin_proto import tls
    return tls.cert_fingerprint(load_tls_material(state_dir).cert_pem)


def build_hub(*, state_dir: Optional[str] = None, data_dir: Optional[str] = None,
              tls: bool = False, require_tls_binding: bool = False,
              peers_remote: bool = False) -> OriginHub:
    """Construct (but do not start) a standalone :class:`OriginHub` with a
    persistent :class:`EnrollmentStore` under ``state_dir``. Separated from
    :func:`serve` so tests can build against a tmp dir and drive ``start``/``stop``
    directly. ``tls=True`` presents this Hub's own minted cert AND delivers it in
    the pairing answer (P2.5 G5 — without it a non-loopback box has nothing to
    verify and pin, so every remote join is refused); ``require_tls_binding``
    additionally hard-gates every non-loopback dial to a device-key-bound tunnel
    (§5.1(4)). ``peers_remote`` drops the loopback exemption: required when the
    Hub is published through the dashboard gate's ``/hub`` tunnel, where every
    remote origin arrives from 127.0.0.1."""
    from mcp_loops.origin_proto import tls as _tls
    root = state_dir or default_state_dir(data_dir)
    os.makedirs(root, mode=0o700, exist_ok=True)
    store = EnrollmentStore(os.path.join(root, "enroll"))
    ssl_context = hub_cert_pem = None
    if tls:
        material = load_tls_material(root)
        # --require-tls-binding: ask each dialer for its device cert and trust
        # the enrolled ones (P2.5 — before this the Hub never requested one, so
        # every remote origin was refused)
        ssl_context = _tls.server_ssl_context(
            material, request_client_cert=require_tls_binding)
        hub_cert_pem = material.cert_pem
    return OriginHub(store, ssl_context=ssl_context,
                     require_tls_binding=require_tls_binding,
                     hub_cert_pem=hub_cert_pem,
                     loopback_exempt=not peers_remote,
                     # S-0 (§7.3): poll connected origins' loop.list
                     inventory=True)


async def serve_forever(*, host: str = "127.0.0.1", port: int = 0,
                        state_dir: Optional[str] = None,
                        data_dir: Optional[str] = None,
                        tls: bool = False, require_tls_binding: bool = False,
                        peers_remote: bool = False,
                        control_sock: Optional[str] = None,
                        ready: "Optional[asyncio.Future]" = None,
                        on_start: Optional[Callable[["OriginHub", int], None]] = None,
                        install_signals: bool = True) -> None:
    """Bring a standalone Hub up on ``host:port`` and run until signalled.

    Resolves the actual listening port (``port=0`` picks a free one), fulfils the
    optional ``ready`` future with it (so a supervisor/test can learn the port),
    invokes the optional ``on_start(hub, port)`` hook with the live façade (a
    supervisor handle — the enrollment store, registry, and router the daemon
    serves), prints a machine-readable ``HUB_LISTENING host=… port=…`` line, then
    blocks on an asyncio stop event that ``SIGINT``/``SIGTERM`` set. On stop it
    tears the ChannelServer down cleanly (kill-only-our-own listener — no
    ``pkill``)."""
    hub = build_hub(state_dir=state_dir, data_dir=data_dir, tls=tls,
                    require_tls_binding=require_tls_binding,
                    peers_remote=peers_remote)
    bound_port = await hub.start(host, port)
    # the engine ↔ Hub bridge (origin_proto.hub_control): a 0600 same-uid UDS
    # the engine's HubServeDispatch uses to reach origins connected HERE.
    control = None
    if control_sock:
        from mcp_loops.origin_proto import hub_control
        control = await hub_control.serve_control(hub, control_sock)
    if ready is not None and not ready.done():
        ready.set_result(bound_port)
    if on_start is not None:
        on_start(hub, bound_port)
    fp = ""
    if tls:   # the out-of-band value a joining box passes as --hub-fingerprint
        fp = f" fingerprint={hub_fingerprint(state_dir or default_state_dir(data_dir))}"
    ctl = f" control={control_sock}" if control is not None else ""
    print(f"HUB_LISTENING host={host} port={bound_port}{fp}{ctl}", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if install_signals:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # non-POSIX / no main loop
                pass
    try:
        await stop.wait()
    finally:
        if control is not None:
            control.close()
            try:
                os.unlink(control_sock)
            except OSError:
                pass
        await hub.stop()


def run_foreground(*, host: str = "127.0.0.1", port: int = 0,
                   state_dir: Optional[str] = None,
                   data_dir: Optional[str] = None,
                   tls: bool = False, require_tls_binding: bool = False,
                   peers_remote: bool = False,
                   control_sock: Optional[str] = None) -> None:
    """Run :func:`serve_forever` under ``asyncio.run`` — the process entry point.
    Returns cleanly on ``SIGINT``/``SIGTERM`` (or ``KeyboardInterrupt`` on
    platforms without signal handlers)."""
    try:
        asyncio.run(serve_forever(
            host=host, port=port, state_dir=state_dir, data_dir=data_dir,
            tls=tls, require_tls_binding=require_tls_binding,
            peers_remote=peers_remote, control_sock=control_sock))
    except KeyboardInterrupt:  # pragma: no cover — interactive Ctrl-C
        pass


def main(argv: Optional[list] = None) -> int:
    """``python -m mcp_loops.hub_serve`` — stand up the Hub daemon (§4 M3)."""
    ap = argparse.ArgumentParser(
        prog="mcp_loops.hub_serve",
        description="Run the Loopyard Hub (OriginHub) as a standalone daemon.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1 — loopback dev Hub)")
    ap.add_argument("--port", type=int, default=0,
                    help="bind port (0 = pick a free port, printed on startup)")
    ap.add_argument("--state-dir", default=None,
                    help="enrollment/identity store dir (default: <data>/_hub)")
    ap.add_argument("--tls", action="store_true",
                    help="present this Hub's minted device cert (encrypt the wire)")
    ap.add_argument("--require-tls-binding", action="store_true",
                    help="hard-gate non-loopback dials to a device-key-bound tunnel")
    ap.add_argument("--peers-remote", action="store_true",
                    help="treat every peer as non-loopback (set when published "
                         "through the dashboard's /hub tunnel, where all peers "
                         "arrive from 127.0.0.1)")
    ap.add_argument("--control-sock", default=None,
                    help="serve the engine bridge on this Unix socket (0600, "
                         "same uid) so the engine can dispatch to origins "
                         "connected to this Hub (LOOPYARD_HUB_CONTROL_SOCK)")
    args = ap.parse_args(argv)
    run_foreground(host=args.host, port=args.port, state_dir=args.state_dir,
                   tls=args.tls, require_tls_binding=args.require_tls_binding,
                   peers_remote=args.peers_remote,
                   control_sock=args.control_sock)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
