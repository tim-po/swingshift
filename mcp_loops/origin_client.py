"""origin_client — P2.5: the CLI-first, cross-platform bring-up of a machine as a
full origin (dial + enroll + worker), headless on Linux, Windows, and macOS.

HUB-FABRIC §6.2 decided the target UX::

    loopyard origin up --hub <url>

→ generate/load the device keypair, pin the Hub cert delivered at pairing, dial
the Hub over an **mTLS-bound** channel, and (optionally) bring the worker up — one
command, no GUI, on any of the three OSes. The GUI apps (§6.3) are thin wrappers
that shell out to / supervise this SAME engine; there is no second code path.

This module is the ENGINE behind that verb. It is deliberately separate from the
in-process :class:`~mcp_loops.origin_service` bring-up (which stands up a LOCAL
hub + agent on loopback for the VPS): the origin *client* dials a **remote** Hub it
does not host, so its trust model is the mTLS pin (§5.1(4)) — the origin presents
its device cert and REFUSES a Hub that presents a wrong/unpinned cert. That gate
is fail-closed for any non-loopback Hub (a real remote origin MUST pin, tls.py
L169-171); only a physically-loopback Hub may be dialed without a pin (dev).

What exists and is reused here (nothing re-implemented):
  * identity  — :class:`~mcp_loops.origin_proto.identity.DeviceIdentity`
                (``load_or_create`` → ONE stable keypair across restarts);
  * TLS       — :func:`~mcp_loops.origin_proto.tls.mint_device_cert` +
                :func:`~mcp_loops.origin_proto.tls.client_ssl_context`
                (present device cert, pin the Hub cert);
  * channel   — :class:`~mcp_loops.origin_proto.channel.ChannelClient`
                (dial, authenticate by key, serve capability-scoped RPCs);
  * executor  — :class:`~mcp_loops.origin_proto.agent_core.OriginAgentExecutor`
                (the RPC executor incl. ``origin.run`` — P1);
  * detach    — :func:`~mcp_loops.detach.spawn_detached`
                (cross-platform: POSIX ``setsid`` / Windows process-group, §6.2).

The pairing-CLAIM over a remote transport (issue a code on the Hub, claim it from
the box) is now wired: :meth:`OriginClient.enroll` dials the Hub ONCE over a TOFU
context (via :func:`channel.pair_with_hub` → the pre-auth ``pair`` frame), presents
this box's PUBLIC key + the code, and the Hub consumes the code, enrolls the key,
and hands back the device_id + its own cert to PIN (§5.1(4)(b)). The box confirms
the returned Hub-cert fingerprint against the value shown next to the code (the
out-of-band trust root) before pinning — closing the trust-on-first-use MITM
window. After that one claim the box owns a stable identity + a pinned Hub cert and
every subsequent dial is full mTLS. ``up --pair-code -`` runs this claim then
brings the origin up in one command.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

# ── the live origin state machine (what a GUI face renders, §6.3) ──────────────

# The states the serve loop moves through. A GUI (or `yard origin status`) renders
# these: "Connecting…" → "Connected ✓" → (on a drop) "Reconnecting…" → "Stopped".
STATE_IDLE = "idle"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"

# A status.json older than this (wall-clock seconds) is treated as STALE by a
# reader: the daemon that wrote it was likely hard-killed without a clean STOPPED,
# so "connected" can no longer be trusted. Honest staleness, not a false green.
STATUS_STALE_AFTER_S = 30.0
# A live daemon re-publishes its CURRENT state at least this often (well inside
# STATUS_STALE_AFTER_S), so a healthy origin never reads stale between transitions.
STATUS_REFRESH_S = 10.0

# ── the Hub endpoint the origin dials ─────────────────────────────────────────

# schemes we accept for --hub; wss/https ⇒ TLS (the real remote posture), ws/http
# ⇒ plaintext (loopback dev only). A bare host:port defaults to secure.
_SECURE_SCHEMES = {"wss", "https"}
_PLAIN_SCHEMES = {"ws", "http"}
_DEFAULT_PORT = 8799  # the origin-hub high port (channel default), not the MCP one


def _is_loopback_host(host: str) -> bool:
    """True iff ``host`` is a loopback address/name — the ONLY case where dialing
    a Hub without a pinned cert is permitted (§6.2). Unknown/odd hosts fail toward
    'not loopback' so the mTLS gate stays fail-closed."""
    if host in ("localhost", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class HubEndpoint:
    """A parsed ``--hub`` target: where to dial and whether TLS is required."""
    host: str
    port: int
    secure: bool
    raw: str
    #: a URL path (e.g. ``/hub``) marks a Hub reached THROUGH an HTTPS front
    #: door (the dashboard gate's ``/hub`` tunnel) rather than dialed directly.
    #: Empty for the direct ``wss://host:port`` form.
    path: str = ""

    @property
    def loopback(self) -> bool:
        return _is_loopback_host(self.host)

    @property
    def tunneled(self) -> bool:
        return bool(self.path)

    def describe(self) -> str:
        scheme = "wss" if self.secure else "ws"
        if self.path:
            default = 443 if self.secure else 80
            port = "" if self.port == default else f":{self.port}"
            return f"{scheme}://{self.host}{port}{self.path}"
        return f"{scheme}://{self.host}:{self.port}"


class OriginUpError(RuntimeError):
    """A bring-up refusal with a stable ``code`` (mirrors EnrollmentError's shape
    so the CLI can map it to a clear message + non-zero exit)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_hub_url(url: str) -> HubEndpoint:
    """Parse a ``--hub`` value into a :class:`HubEndpoint`. Accepts
    ``wss://host:port`` / ``ws://host:port`` (and the https/http aliases) or a bare
    ``host:port`` / ``host`` (defaults to secure + the origin-hub port). Raises
    :class:`OriginUpError` on an empty/unparseable value."""
    if not url or not url.strip():
        raise OriginUpError("bad_hub_url", "--hub requires a value (e.g. wss://host:port)")
    raw = url.strip()
    # urlparse needs a scheme to populate .hostname/.port; add one for bare forms.
    parsed = urlparse(raw if "://" in raw else f"wss://{raw}")
    scheme = parsed.scheme.lower()
    if scheme in _SECURE_SCHEMES:
        secure = True
    elif scheme in _PLAIN_SCHEMES:
        secure = False
    else:
        raise OriginUpError("bad_hub_url", f"unsupported hub scheme {scheme!r}")
    host = parsed.hostname
    if not host:
        raise OriginUpError("bad_hub_url", f"no host in --hub {url!r}")
    path = (parsed.path or "").rstrip("/")
    if (parsed.query or parsed.fragment or parsed.params
            or (path and (not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", path)
                          or any(seg in (".", "..")
                                 for seg in path.split("/"))))):
        raise OriginUpError("bad_hub_url", f"bad path in --hub {url!r}")
    # a pathed URL is an HTTPS front door, so it defaults to the web port
    port = parsed.port or ((443 if secure else 80) if path else _DEFAULT_PORT)
    return HubEndpoint(host=host, port=int(port), secure=secure, raw=raw,
                       path=path)


def probe_hub(hub: HubEndpoint, *, timeout: float = 5.0) -> Optional[str]:
    """TCP reachability of ``hub`` BEFORE any pairing/dial: None when something
    accepts on host:port, else a one-line reason. Lets ``origin up`` fail loudly
    with ``hub_unreachable`` instead of spawning a daemon that reconnects forever."""
    import socket
    try:
        with socket.create_connection((hub.host, hub.port), timeout=timeout):
            return None
    except OSError as e:
        return str(e.strerror or e)


def peek_hub_cert_fingerprint(hub: HubEndpoint, *,
                              timeout: float = 5.0) -> Optional[str]:
    """The fingerprint of the cert a ``wss://`` Hub presents in the TLS
    handshake, read WITHOUT sending anything (None if no cert came back). Same
    shape as :func:`tls.cert_fingerprint` (SHA-256 of the DER). Lets ``enroll``
    refuse a wrong Hub BEFORE the single-use code crosses the wire; the
    post-answer check still guards what gets pinned. Raises OSError / ssl.SSLError
    on a transport failure."""
    import socket
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False   # identity is the fingerprint, checked by the caller
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((hub.host, hub.port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw) as tls_sock:
            der = tls_sock.getpeercert(binary_form=True)
    return _der_fingerprint(der)


async def peek_tunneled_hub_cert_fingerprint(
        hub: HubEndpoint, *, tunnel_path: str, tunnel_ssl: Any = None,
        timeout: float = 5.0) -> Optional[str]:
    """:func:`peek_hub_cert_fingerprint` for a Hub behind a ``/hub`` tunnel: the
    fingerprint of the cert on the INNER (Hub) TLS handshake, not the front
    door's."""
    from mcp_loops.origin_proto import hub_tunnel
    der = await hub_tunnel.peek_cert_der(hub.host, hub.port, tunnel_path,
                                         outer_ssl=tunnel_ssl, timeout=timeout)
    return _der_fingerprint(der)


def _der_fingerprint(der: Optional[bytes]) -> Optional[str]:
    import hashlib
    if not der:
        return None
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


# ── the machine-readable status surface (one engine, N faces) ─────────────────

class OriginStatusFile:
    """The live status the serve daemon publishes and any *face* reads — the thin
    contract between the ONE engine and its N faces (§6.3). The `origin up` daemon
    writes each state transition here atomically; `yard origin status` and the GUI
    poll it, so a GUI never needs to embed the engine to show "Connected ✓". A
    hard-killed daemon leaves a stale file — :meth:`read` stamps that honestly."""

    def __init__(self, path: str):
        self.path = path

    def write(self, status: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(status, fh)
        os.replace(tmp, self.path)

    def read(self, *, now: Optional[float] = None) -> Optional[dict]:
        """The last-published status (with a computed ``stale`` flag), or None if
        no daemon has ever written one. ``stale`` is True when the file's ``ts`` is
        older than :data:`STATUS_STALE_AFTER_S` — the writer likely died without a
        clean STOPPED, so a lingering ``connected`` must not read as live."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                status = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(status, dict):
            return None
        if now is None:
            import time
            now = time.time()
        ts = status.get("ts")
        try:
            age = now - float(ts)
        except (TypeError, ValueError):
            age = None
        status["stale"] = age is None or age > STATUS_STALE_AFTER_S
        return status


# ── configuration for a bring-up ──────────────────────────────────────────────

@dataclass
class OriginUpConfig:
    """Everything ``loopyard origin up`` needs. ``state_dir`` holds the device
    keypair (``device.json``, 0600) + the pinned Hub cert (``hub-cert.pem``) + the
    agent's pidfile/log — the box keeps ONE identity across restarts."""
    hub: HubEndpoint
    state_dir: str
    origin_id: str = "local"
    label: Optional[str] = None
    start_worker: bool = True
    hub_cert_path: Optional[str] = None  # explicit pin; else <state_dir>/hub-cert.pem
    worker_module: str = "bot_squad_worker"
    extra_run_allowlist: list[str] = field(default_factory=list)
    # Project ids the box owner opts into remote dispatch on THIS bring-up
    # (`origin up --allow-project ID`); added to <state_dir>/project-allowlist.json.
    allow_projects: list[str] = field(default_factory=list)


# The origin's dispatchable-Project allowlist (§6.4) lives in its OWN state dir —
# P4a-protected, so the Hub can never widen it over origin.run / fs.push. Only the
# box owner edits it (`yard origin allow-project/deny-project`). No file = deny.
PROJECT_ALLOWLIST_FILE = "project-allowlist.json"


def project_allowlist_path(state_dir: str) -> str:
    return os.path.join(state_dir, PROJECT_ALLOWLIST_FILE)


def open_project_allowlist(state_dir: str):
    """The owner-side handle on ``<state_dir>/project-allowlist.json`` (created
    0700/0600 on first write). Used by the yard CLI verbs."""
    from mcp_loops.origin_proto.agent_core import ProjectAllowlist
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    return ProjectAllowlist(project_allowlist_path(state_dir))


def _live_project_allowlist(path: str):
    """A :class:`ProjectAllowlist` over ``path`` that re-reads the file whenever
    it changes, so `yard origin allow-project`/`deny-project` take effect on a
    RUNNING origin without a restart. Fail-CLOSED: a missing or unreadable file
    means nothing is dispatchable."""
    from mcp_loops.origin_proto.agent_core import ProjectAllowlist

    class _Live(ProjectAllowlist):
        _stamp = None

        def _refresh(self) -> None:
            try:
                st = os.stat(self.path)
                stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
            except OSError:
                stamp = None
            if stamp == self._stamp:
                return
            self._stamp = stamp
            self._allow = set()
            if stamp is not None:
                self._load()

        def allowed(self) -> list[str]:
            self._refresh()
            return super().allowed()

        @property
        def wildcard(self) -> bool:
            self._refresh()
            return self.WILDCARD in self._allow

        def is_allowed(self, project_id) -> bool:
            self._refresh()
            return super().is_allowed(project_id)

    return _Live(path)


class OriginClient:
    """The bring-up engine. Construct with an :class:`OriginUpConfig`; call
    :meth:`plan` for the dry-run description or :meth:`connect` to dial. All the
    security-critical material (keypair, device cert, Hub-cert pin) is assembled
    here so the CLI verb and the GUI wrapper share exactly ONE path."""

    def __init__(self, config: OriginUpConfig):
        self.config = config
        self._identity = None  # lazy: created on first need (idempotent)
        self._device_material = None  # cached device cert (minted once, stable)

    # ── identity + trust material ─────────────────────────────────────────────
    def _device_path(self) -> str:
        return os.path.join(self.config.state_dir, "device.json")

    def identity(self, *, create: bool = True):
        """Load (or, when ``create``, load-or-create) the box's stable device
        identity. The private key stays 0600 under ``state_dir`` and NEVER leaves
        the box (only the public key is enrolled)."""
        if self._identity is not None:
            return self._identity
        from mcp_loops.origin_proto import identity as _id
        path = self._device_path()
        if not create and not os.path.exists(path):
            return None
        os.makedirs(self.config.state_dir, mode=0o700, exist_ok=True)
        self._identity = _id.DeviceIdentity.load_or_create(
            path, label=self.config.label or self.config.origin_id)
        return self._identity

    def pinned_hub_cert_path(self) -> str:
        return self.config.hub_cert_path or os.path.join(
            self.config.state_dir, "hub-cert.pem")

    def pinned_hub_cert(self) -> Optional[bytes]:
        """The Hub cert this origin pins (delivered at pairing), or None if not yet
        present. A non-loopback dial with no pin is refused (§6.2)."""
        path = self.pinned_hub_cert_path()
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        return data or None

    def tls_mode(self) -> str:
        """How this dial is secured: ``"mtls-pinned"`` (secure + a pinned Hub cert
        — the real remote posture), ``"plaintext-loopback"`` (loopback dev, no
        TLS), or ``"blocked"`` (secure/non-loopback but no pin → refused)."""
        hub = self.config.hub
        if hub.tunneled:   # the Hub's TLS always runs inside a /hub tunnel
            return "mtls-pinned" if self.pinned_hub_cert() is not None else "blocked"
        if not hub.secure:
            return "plaintext-loopback" if hub.loopback else "blocked"
        if self.pinned_hub_cert() is not None:
            return "mtls-pinned"
        return "plaintext-loopback" if hub.loopback else "blocked"

    def device_cert_material(self):
        """This origin's TLS material (self-signed cert whose public key IS the
        device key), minted ONCE and cached so the cert it PRESENTS is stable — the
        Hub pins exactly this cert for mTLS (§5.1(4)). ``cert_pem`` is public (safe
        to enroll/pin); the key never leaves the box."""
        if self._device_material is None:
            from mcp_loops.origin_proto import tls as _tls
            self._device_material = _tls.mint_device_cert(self.identity())
        return self._device_material

    def build_client_ssl_context(self) -> Any:
        """Assemble the origin's client TLS context: present THIS device's cert
        and pin the Hub's. Returns None for a plaintext-loopback dial. Raises
        :class:`OriginUpError` (``mtls_required``) when the Hub is non-loopback but
        no cert is pinned — the fail-closed §5.1(4)/§6.2 gate: a real remote origin
        MUST pin, and MUST refuse an unpinned/​wrong Hub."""
        from mcp_loops.origin_proto import tls as _tls
        mode = self.tls_mode()
        if mode == "blocked":
            raise OriginUpError(
                "mtls_required",
                f"refusing to dial non-loopback Hub {self.config.hub.describe()} "
                f"without a pinned cert — place the Hub cert at "
                f"{self.pinned_hub_cert_path()} (delivered at pairing)")
        if mode == "plaintext-loopback":
            return None
        return _tls.client_ssl_context(
            self.device_cert_material(), server_cert_pem=self.pinned_hub_cert())

    def tunnel_kwargs(self) -> dict:
        """Dial kwargs for a Hub behind a front door's ``/hub`` byte tunnel: the
        tunnel path + the front door's CA-verified TLS (None for a ``ws://`` front
        door). Empty for a directly dialed Hub."""
        hub = self.config.hub
        if not hub.tunneled:
            return {}
        import ssl
        return {"tunnel_path": hub.path,
                "tunnel_ssl": ssl.create_default_context() if hub.secure else None}

    # ── enrollment: the remote pairing-claim (§6.2) ───────────────────────────
    def pairing_ssl_context(self) -> Any:
        """The TOFU dial context for the ONE pre-auth pairing connection: encrypt +
        present this device's cert, but do NOT pin the Hub yet — the box has no Hub
        cert until pairing delivers it (§5.1(4)(b)). Returns None for a plaintext
        loopback Hub (dev). A non-loopback plaintext Hub is refused (a pairing
        credential must not cross the network in the clear)."""
        hub = self.config.hub
        # a tunneled Hub always pairs over its own TLS inside the tunnel; only a
        # loopback front door may carry that tunnel over plain ws://
        if not hub.secure and not (hub.tunneled and hub.loopback):
            if hub.loopback:
                return None
            raise OriginUpError(
                "insecure_pairing",
                f"refusing to pair with non-loopback Hub {hub.describe()} over "
                f"plaintext — use wss:// so the pairing claim is encrypted")
        from mcp_loops.origin_proto import tls as _tls
        # server_cert_pem=None ⇒ CERT_NONE (trust-on-first-use): we accept the
        # Hub's cert this once and PIN what it returns; the fingerprint check below
        # is what actually authenticates it.
        # No client cert on this one dial: a binding Hub asks for one but trusts
        # only ENROLLED certs, so presenting ours before it is enrolled would fail
        # the handshake. The cert rides the claim instead (see enroll).
        return _tls.client_ssl_context(None, server_cert_pem=None)

    def pairing_label(self) -> Optional[str]:
        """The label this box claims with: an explicit ``--label``, else a non-
        default origin id, else the hostname — never the shared ``local`` default
        (every box would register indistinguishably). The Hub prefers the label
        the owner minted the code with."""
        if self.config.label:
            return self.config.label
        if self.config.origin_id and self.config.origin_id != "local":
            return self.config.origin_id
        import socket
        return socket.gethostname() or None

    async def enroll(self, code: str, *,
                     expected_hub_fingerprint: Optional[str] = None) -> dict:
        """Claim a pairing ``code`` on the Hub over the wire — the remote half of
        enrollment (§6.2). Dials once (TOFU), presents this box's PUBLIC key, and:

          * confirms the Hub-derived device_id matches THIS box's key (a self-
            certifying id, so a mismatch means we reached the wrong box's record);
          * requires ``expected_hub_fingerprint`` (the value shown next to the
            code, the out-of-band trust root) for any NON-loopback Hub — refused
            before dialing without it; fingerprint-less TOFU is loopback/dev only;
          * validates the returned Hub cert is a self-signed Ed25519 cert whose
            fingerprint matches the expected one — anything else is REFUSED,
            closing the TOFU MITM window — then PINS it to ``hub-cert.pem``.

        Returns ``{deviceId, fingerprint, hubCertPinned, hubCertFingerprint}``.
        Raises :class:`OriginUpError` on any refusal — the caller never proceeds to
        an unpaired dial. The private key never leaves the box; only the public key
        and label are sent."""
        from mcp_loops.origin_proto import channel as _channel
        from mcp_loops.origin_proto import identity as _id
        from mcp_loops.origin_proto import tls as _tls
        ident = self.identity()
        # a tunneled Hub is never "loopback" for trust: the front door is the peer
        loopback = self.config.hub.loopback and not self.config.hub.tunneled
        try:   # non-loopback ⇒ fingerprint mandatory, refused BEFORE any dial
            _tls.require_pairing_fingerprint(
                loopback=loopback, expected_fingerprint=expected_hub_fingerprint)
        except _tls.PairingTrustError as e:
            raise OriginUpError(e.code, str(e)) from e
        # S-1 c: the logical originId lives beside the keypair, minted once and
        # never derived from the key, so a re-pair with a new key keeps it. A
        # present-but-malformed file refuses the pair rather than forking identity.
        from mcp_loops.sync import ids as _sync_ids
        try:
            origin_id = _sync_ids.local_origin_id(self.config.state_dir)
        except (OSError, ValueError) as e:
            raise OriginUpError("origin_id_unreadable",
                                f"cannot read this box's originId: {e}") from e
        ssl_ctx = self.pairing_ssl_context()
        if ssl_ctx is not None and expected_hub_fingerprint:
            # refuse a wrong Hub BEFORE the code is spent: an on-path impostor
            # never sees it, and the real Hub never enrolls a box that then
            # refuses to pin (the post-answer gate below stays as the pin guard).
            try:
                import asyncio
                if self.config.hub.tunneled:
                    seen = await peek_tunneled_hub_cert_fingerprint(
                        self.config.hub, **self.tunnel_kwargs())
                else:
                    seen = await asyncio.get_running_loop().run_in_executor(
                        None, peek_hub_cert_fingerprint, self.config.hub)
            except OSError as e:   # ssl.SSLError is an OSError
                raise OriginUpError(
                    "hub_unreachable",
                    f"cannot reach the Hub at {self.config.hub.describe()} to pair: "
                    f"{getattr(e, 'strerror', None) or e}") from e
            if seen is None or seen.lower() != expected_hub_fingerprint.strip().lower():
                raise OriginUpError(
                    "hub_fingerprint_mismatch",
                    f"Hub cert fingerprint {seen} does not match the expected "
                    f"{expected_hub_fingerprint.strip()} — possible MITM, refusing "
                    f"(the pairing code was NOT sent)")
        try:
            answer = await _channel.pair_with_hub(
                self.config.hub.host, self.config.hub.port,
                code=code, pubkey_b64=ident.public_key_b64,
                label=self.pairing_label(),
                cert_b64=_id.b64e(self.device_cert_material().cert_pem),
                origin_id=origin_id,
                ssl_context=ssl_ctx, **self.tunnel_kwargs())
        except _channel.ChannelDenied as e:
            raise OriginUpError("pairing_failed",
                                f"Hub refused the pairing claim: {e}") from e
        except _channel.ChannelError as e:
            raise OriginUpError("pairing_transport",
                                f"pairing transport failed: {e}") from e
        except OSError as e:   # refused / unroutable / TLS handshake — not a traceback
            raise OriginUpError(
                "hub_unreachable",
                f"cannot reach the Hub at {self.config.hub.describe()} to pair: "
                f"{e.strerror or e}") from e
        # the id is derived from OUR key on the Hub; if it does not match ours, we
        # are looking at the wrong record — refuse rather than pin a stranger.
        if answer.get("deviceId") != ident.device_id:
            raise OriginUpError(
                "device_id_mismatch",
                f"Hub enrolled device_id {answer.get('deviceId')!r} but this box is "
                f"{ident.device_id!r} — refusing")
        pinned = False
        hub_cert_b64 = answer.get("hubCert")
        try:
            hub_cert_pem = _id.b64d(hub_cert_b64) if hub_cert_b64 else None
        except Exception:   # noqa: BLE001 — undecodable ⇒ same as an invalid cert
            hub_cert_pem = b"\x00"
        try:   # self-signed Ed25519 + fingerprint gate BEFORE anything is pinned
            hub_fp = _tls.verify_pairing_hub_cert(
                hub_cert_pem, loopback=loopback,
                expected_fingerprint=expected_hub_fingerprint)
        except _tls.PairingTrustError as e:
            raise OriginUpError(e.code, str(e)) from e
        if hub_cert_pem:
            os.makedirs(self.config.state_dir, mode=0o700, exist_ok=True)
            path = self.pinned_hub_cert_path()
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(hub_cert_pem)
            os.replace(tmp, path)
            pinned = True
        return {
            "deviceId": answer.get("deviceId"),
            "fingerprint": answer.get("fingerprint"),
            "hubCertPinned": pinned,
            "hubCertFingerprint": hub_fp,
        }

    # ── the worker command (execution half of "an origin") ────────────────────
    def worker_command(self, python: Optional[str] = None) -> list[str]:
        """The argv that brings the worker daemon up (execution: it spawns the
        agent/CLI processes a dispatched turn runs). Reachability is the channel;
        this is the other half that makes the box actually RUN work (§6.1).

        Two shapes, one for each way this agent runs:

          * **From a real interpreter** → ``[python, "-m", <worker_module>]`` — the
            classic module launch.
          * **From a frozen one-file bundle** (§6.5) → ``[exe, "origin",
            "run-worker"]``. Inside a PyInstaller bundle ``sys.executable`` IS the
            frozen exe, so ``-m bot_squad_worker`` cannot resolve a module. The
            worker rides *inside* the bundle (collected as a hidden import) and is
            reached through the exe's own ``origin run-worker`` subcommand instead —
            the BUNDLE_WORKER_NOTE constraint, closed. An explicit ``python`` still
            wins (a test / a caller pointing at a specific interpreter).
        """
        import sys
        if python is None and getattr(sys, "frozen", False):
            return [sys.executable, "origin", "run-worker"]
        # R1: hand the worker OUR config dir. Its own default is a foreign
        # absolute path on a clean box/bundle (boots DEGRADED without this).
        from mcp_loops import paths
        return [python or sys.executable, "-m", self.config.worker_module,
                "--config", str(paths.config_dir())]

    def worker_env(self) -> dict:
        """R1: the env the detached worker starts with — ours, in ``user-worker``
        mode (never the coordinator role)."""
        return {**os.environ, "BOT_SQUAD_MODE": "user-worker"}

    # ── worker supervision: spawn/reuse/stop OUR OWN detached worker ────────────
    def worker_log_path(self) -> str:
        return os.path.join(self.config.state_dir, "worker.log")

    def worker_pidfile_path(self) -> str:
        return os.path.join(self.config.state_dir, "worker.pid")

    def status_path(self) -> str:
        """Where the serve daemon publishes its live state (§6.3) — the file a GUI
        or ``yard origin status`` polls."""
        return os.path.join(self.config.state_dir, "status.json")

    def status_file(self) -> "OriginStatusFile":
        return OriginStatusFile(self.status_path())

    def _read_worker_pid(self) -> Optional[int]:
        """The pid recorded in our worker pidfile, or None. Only pids WE wrote are
        ever signalled — the origin never touches a process it did not start."""
        try:
            with open(self.worker_pidfile_path(), encoding="utf-8") as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """True iff ``pid`` is a live process (POSIX ``kill(pid, 0)``; on Windows
        the absence of a cheap probe means we conservatively report alive so a
        stale pidfile does not trigger a duplicate spawn)."""
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but not ours to signal
        except (AttributeError, OSError):
            return True  # no os.kill (Windows) — assume alive, don't double-spawn
        return True

    def start_worker_daemon(self, *, spawn=None) -> Optional[int]:
        """Bring THIS box's worker daemon up detached (survives the launching
        shell/console on POSIX + Windows via :func:`detach.spawn_detached`), so the
        origin actually RUNS dispatched work (§6.1). Idempotent: if our pidfile
        names a live worker we reuse it rather than launching a second. Returns the
        worker pid (or None when ``start_worker`` is off). ``spawn`` is injectable
        so a test supervises the lifecycle without launching a real worker."""
        if not self.config.start_worker:
            return None
        existing = self._read_worker_pid()
        if existing is not None and self._pid_alive(existing):
            return existing  # already up — do not double-spawn
        from mcp_loops import detach
        os.makedirs(self.config.state_dir, mode=0o700, exist_ok=True)
        if spawn is None:
            pid = detach.spawn_detached(self.worker_command(),
                                        log_path=self.worker_log_path(),
                                        env=self.worker_env())
        else:
            pid = spawn(self.worker_command(), log_path=self.worker_log_path())
        tmp = self.worker_pidfile_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(str(pid) + "\n")
        os.replace(tmp, self.worker_pidfile_path())
        return pid

    def stop_worker_daemon(self, *, sig=None) -> bool:
        """Signal the worker WE started (the pid in our pidfile) to stop, then
        forget the pidfile. Never signals a pid we did not record (kill-only-our-own
        discipline). Returns True iff a live worker was signalled. ``sig`` defaults
        to SIGTERM where available."""
        pid = self._read_worker_pid()
        signalled = False
        if pid is not None and self._pid_alive(pid):
            import signal as _signal
            _sig = sig if sig is not None else getattr(_signal, "SIGTERM", 15)
            try:
                os.kill(pid, _sig)
                signalled = True
            except (OSError, AttributeError):
                signalled = False
        try:
            os.remove(self.worker_pidfile_path())
        except OSError:
            pass
        return signalled

    # ── the executor: assemble the RPC executor from this origin's policy ───────
    def build_executor(self, *, engine=None, audit=None):
        """Assemble the capability-scoped :class:`OriginAgentExecutor` this origin
        serves inbound RPCs through. The ``origin.run`` exec gate is DEFAULT-DENY;
        ``extra_run_allowlist`` opts specific program basenames (or the ``"*"``
        wildcard) in (§5.1(2)). The Project gate (§6.4) is loaded from
        ``<state_dir>/project-allowlist.json`` and re-read when it changes. ``engine=None`` lazily binds the box's live loop
        engine on first mutating RPC. Kept here so the CLI verb, the GUI wrapper,
        and the smoke proof all serve through exactly ONE executor build."""
        from mcp_loops.origin_proto.agent_core import (
            OriginAgentExecutor, RunAllowlist)
        from mcp_loops import origin_policy as _policy
        # §6.4 Project gate: the owner's persisted opt-ins (DEFAULT-DENY — no file,
        # no remote loop.start), plus any `--allow-project` given on this bring-up.
        # A write failure raises: the owner asked for an opt-in this box can't
        # record, so fail the bring-up loudly rather than serve without it.
        if self.config.allow_projects:
            seed = open_project_allowlist(self.config.state_dir)
            for pid in self.config.allow_projects:
                seed.allow_project(pid)
        allowlist = _live_project_allowlist(
            project_allowlist_path(self.config.state_dir))
        run_allowlist = RunAllowlist(allow=list(self.config.extra_run_allowlist)) \
            if self.config.extra_run_allowlist else None
        # `--allow-run PROG` is the box owner's explicit exec opt-in, so with no
        # manifest file of their own the P3 manifest advertises exec (still gated
        # to exactly those programs). An owner-written manifest always wins.
        manifest = None
        if run_allowlist is not None and not os.path.exists(
                _policy.manifest_path_for(self.config.state_dir)):
            manifest = _policy.OriginManifest(exec=True)
        return OriginAgentExecutor(
            self.config.origin_id, engine=engine, allowlist=allowlist,
            run_allowlist=run_allowlist, audit=audit,
            protected_dirs=[self.config.state_dir], manifest=manifest)

    # ── the dry-run description (no side effects beyond an idempotent keypair) ─
    def plan(self, *, create_identity: bool = False) -> dict:
        """Describe what ``up`` will do, WITHOUT dialing. Honest about the TLS gate
        and the not-yet-wired remote pairing-claim seam. ``create_identity=False``
        leaves the box untouched (reports the existing device_id or that one will
        be generated); ``True`` materializes the keypair so the plan can show the
        real device_id to enroll."""
        from mcp_loops import detach
        ident = self.identity(create=create_identity)
        device_id = ident.device_id if ident is not None else "(generated on `up`)"
        mode = self.tls_mode()
        steps: list[str] = [
            f"identity: {'load' if os.path.exists(self._device_path()) else 'generate'} "
            f"device keypair under {self.config.state_dir}",
            f"dial Hub {self.config.hub.describe()} ({mode})",
        ]
        if self.config.start_worker:
            steps.append("start worker daemon (detached)")
        return {
            "hub": self.config.hub.describe(),
            "hubLoopback": self.config.hub.loopback,
            "deviceId": device_id,
            "originId": self.config.origin_id,
            "tlsMode": mode,
            "pinnedHubCert": self.pinned_hub_cert() is not None,
            "pinnedHubCertPath": self.pinned_hub_cert_path(),
            "detachKind": detach.platform_detach_kind(),
            "startWorker": self.config.start_worker,
            "workerCommand": self.worker_command(),
            "steps": steps,
            # remote pairing-claim transport is now wired (OriginClient.enroll →
            # channel.pair_with_hub → the pre-auth `pair` frame).
            "pairingClaimWired": True,
            "notes": [
                "run `origin up --pair-code -` (the code is read from stdin or "
                "$LOOPYARD_PAIR_CODE, never argv; a non-loopback Hub also needs "
                "--hub-fingerprint) to "
                "claim a pairing code on the Hub: it enrolls this box's public key "
                "and delivers the Hub cert to pin (§6.2). Without a pair-code the "
                "box must already be enrolled and its Hub cert already pinned.",
            ],
        }

    # ── the real dial (used by --foreground serve AND the smoke proof) ────────
    def build_channel_client(self, *, executor, ledger=None,
                             capabilities_fn=None, **kw):
        """Construct the :class:`ChannelClient` that dials the Hub with this
        origin's identity + mTLS context. The caller owns running it
        (``run_forever``) and tearing it down — kept separate so the smoke test and
        the foreground service share exactly this wiring."""
        from mcp_loops.origin_proto.channel import ChannelClient
        ssl_context = self.build_client_ssl_context()
        return ChannelClient(
            self.identity(), self.config.hub.host, self.config.hub.port,
            origin_id=self.config.origin_id, executor=executor,
            ledger=ledger, capabilities_fn=capabilities_fn,
            ssl_context=ssl_context, **self.tunnel_kwargs(), **kw)

    # ── the long-lived FOREGROUND serve loop (§6.2 — the daemon behind `up`) ────
    def _status(self, state: str, *, worker_pid: Optional[int], **extra) -> dict:
        """Assemble a status payload for :data:`on_state` — the shape a GUI or
        ``yard origin status`` renders. ``ts`` lets a reader flag staleness."""
        import time
        dev = self.identity(create=False)
        return {
            "state": state,
            "hub": self.config.hub.describe(),
            "originId": self.config.origin_id,
            "deviceId": dev.device_id if dev is not None else None,
            "workerPid": worker_pid,
            "ts": time.time(),
            **extra,
        }

    async def _watch_connection(self, client, emit, *, interval: float = 0.1,
                                refresh: float = STATUS_REFRESH_S, clock=None):
        """Poll the channel's ``connected`` flag and emit a state transition on each
        edge: first up ⇒ CONNECTED, a subsequent drop ⇒ RECONNECTING (the reconnect
        backoff is already running underneath), a re-up ⇒ CONNECTED again. Between
        edges the CURRENT state is re-emitted every ``refresh`` seconds (a fresh
        ``ts``), so a reader never mistakes a healthy long-lived daemon for a dead
        one (:data:`STATUS_STALE_AFTER_S`). Cancelled on teardown so the final
        STOPPED/ERROR is always the last word."""
        import asyncio
        import time
        clock = clock or time.monotonic
        prev = False
        state = STATE_CONNECTING  # serve() emitted it just before starting us
        last = clock()
        try:
            while True:
                now = bool(client.connected)
                if now and not prev:
                    state = STATE_CONNECTED
                elif prev and not now:
                    state = STATE_RECONNECTING
                if now != prev or clock() - last >= refresh:
                    emit(state)
                    last = clock()
                prev = now
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    async def serve(self, *, executor=None, ledger=None, capabilities_fn=None,
                    max_reconnects: Optional[int] = None,
                    shutdown: Any = None, on_ready=None, on_state=None,
                    spawn_worker=None, engine=None, audit=None) -> dict:
        """Bring the origin up as a live daemon and KEEP it up: assemble the RPC
        executor, (optionally) supervise a detached worker, dial the Hub, and
        ``run_forever`` — reconnecting with backoff across drops until ``shutdown``
        is set, ``max_reconnects`` is exhausted, or the key is revoked
        (``ChannelDenied``). This is the one code path the CLI verb and the GUI
        wrapper both drive (§6.3) — there is no second daemon.

        ALWAYS tears down a worker it started and closes the channel on exit —
        including on cancellation, so Ctrl-C / SIGTERM leaves nothing dangling.
        ``on_ready(client)`` (sync or async) fires once the client object exists
        (before the dial completes) so a test/GUI can await ``wait_connected`` and
        drive an RPC. ``on_state(status)`` (optional) fires on every state
        transition (CONNECTING → CONNECTED → RECONNECTING → STOPPED/ERROR) so a face
        renders live state without embedding the engine — this is what powers
        ``yard origin status`` + the GUI (§6.3). ``shutdown`` is an
        :class:`asyncio.Event`; ``spawn_worker`` injects the detached-spawn for
        tests. Returns ``{workerPid, reason}``."""
        import asyncio
        if executor is None:
            executor = self.build_executor(engine=engine, audit=audit)
        worker_pid = self.start_worker_daemon(spawn=spawn_worker)

        def emit(state: str, **extra):
            if on_state is None:
                return
            try:
                on_state(self._status(state, worker_pid=worker_pid, **extra))
            except Exception:  # noqa: BLE001 — a broken face must never kill the daemon
                pass

        client = self.build_channel_client(
            executor=executor, ledger=ledger, capabilities_fn=capabilities_fn)
        # gap #3: a loop the Hub dispatches here streams its progress back over
        # THIS channel (the origin tails its own engine; no rsync mirror needed).
        if getattr(executor, "tail_client", False) is None:
            executor.tail_client = client
        emit(STATE_CONNECTING)
        run_task = asyncio.ensure_future(
            client.run_forever(max_reconnects=max_reconnects))
        watch_task = (asyncio.ensure_future(self._watch_connection(client, emit))
                      if on_state is not None else None)
        reason = "run_forever_returned"
        err: Optional[BaseException] = None
        try:
            if on_ready is not None:
                res = on_ready(client)
                if asyncio.iscoroutine(res):
                    await res
            if shutdown is not None:
                stop_task = asyncio.ensure_future(shutdown.wait())
                done, _ = await asyncio.wait(
                    {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                stop_task.cancel()
                if run_task in done:
                    # surface a ChannelDenied (revoked key) rather than swallow it
                    run_task.result()
                else:
                    reason = "shutdown_requested"
            else:
                await run_task
        except Exception as e:  # noqa: BLE001 — surface as ERROR then re-raise
            err = e
            raise
        finally:
            if watch_task is not None:
                watch_task.cancel()
                try:
                    await watch_task
                except (asyncio.CancelledError, Exception):
                    pass
            if not run_task.done():
                client.stop()
                run_task.cancel()
                try:
                    await run_task
                except (asyncio.CancelledError, Exception):
                    pass
            if worker_pid is not None:
                self.stop_worker_daemon()
            if err is not None:
                emit(STATE_ERROR, reason="exception", error=str(err))
            else:
                emit(STATE_STOPPED, reason=reason)
        return {"workerPid": worker_pid, "reason": reason}

    def run_foreground(self, *, max_reconnects: Optional[int] = None,
                       publish_status: bool = True) -> dict:
        """Synchronous entry point for `yard origin up` (no `--dry-run`): run
        :meth:`serve` under ``asyncio.run`` with a graceful SIGINT/SIGTERM stop
        where the platform supports signal handlers (POSIX); on Windows a Ctrl-C
        surfaces as ``KeyboardInterrupt`` and we shut down the same way. Blocks
        until the daemon exits, then returns serve's result. When
        ``publish_status`` (default) the daemon publishes each state transition to
        ``status.json`` so a separate face (`yard origin status`, the GUI) renders
        live state without embedding the engine (§6.3)."""
        import asyncio
        import signal

        on_state = self.status_file().write if publish_status else None

        async def _main() -> dict:
            shutdown = asyncio.Event()
            loop = asyncio.get_event_loop()
            for name in ("SIGINT", "SIGTERM"):
                sig = getattr(signal, name, None)
                if sig is None:
                    continue
                try:
                    loop.add_signal_handler(sig, shutdown.set)
                except (NotImplementedError, RuntimeError):
                    pass  # Windows / non-main-thread — fall back to KeyboardInterrupt
            return await self.serve(shutdown=shutdown, on_state=on_state,
                                    max_reconnects=max_reconnects)

        try:
            return asyncio.run(_main())
        except KeyboardInterrupt:
            if on_state is not None:
                # a Ctrl-C that surfaced as KeyboardInterrupt (Windows / no signal
                # handler) still leaves an honest STOPPED, not a lingering CONNECTED.
                try:
                    on_state(self._status(STATE_STOPPED,
                                          worker_pid=None, reason="keyboard_interrupt"))
                except Exception:  # noqa: BLE001
                    pass
            return {"workerPid": None, "reason": "keyboard_interrupt"}


# ── the GUI face: a thin supervisor over the SAME serve engine (§6.3) ──────────

class OriginSupervisor:
    """Runs the SAME :meth:`OriginClient.serve` engine in a background thread and
    exposes an observable :meth:`snapshot` + :meth:`start`/:meth:`stop`, so a GUI
    (the macOS/Windows app, §6.3) drives ONE engine with NO second daemon and NO
    terminal. It mirrors every state transition to both an in-memory snapshot (for
    an embedded GUI) and, when given a :class:`OriginStatusFile`, to ``status.json``
    (for a shell-out face like ``yard origin status``). The engine is unchanged;
    this is purely a face over it."""

    def __init__(self, client: "OriginClient", *,
                 status_file: Optional["OriginStatusFile"] = None,
                 spawn_worker=None):
        import threading
        self._client = client
        self._status_file = status_file
        self._spawn_worker = spawn_worker
        self._lock = threading.Lock()
        self._ready = threading.Event()  # set once the loop + shutdown event exist
        self._status: dict = {"state": STATE_IDLE, "hub": client.config.hub.describe(),
                              "originId": client.config.origin_id}
        self._thread = None
        self._loop = None
        self._shutdown = None
        self.result: Optional[dict] = None

    def snapshot(self) -> dict:
        """The current status (a copy — safe to read from the GUI thread)."""
        with self._lock:
            return dict(self._status)

    def state(self) -> str:
        return self.snapshot().get("state", STATE_IDLE)

    def _on_state(self, status: dict) -> None:
        with self._lock:
            self._status = status
        if self._status_file is not None:
            try:
                self._status_file.write(status)
            except Exception:  # noqa: BLE001 — a face write must never kill serve
                pass

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Bring the origin up on a background thread. Idempotent: a second call
        while already running is a no-op."""
        import threading
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self.result = None
            self._thread = threading.Thread(
                target=self._run, name="origin-supervisor", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def _main():
            self._shutdown = asyncio.Event()
            self._ready.set()
            return await self._client.serve(
                shutdown=self._shutdown, on_state=self._on_state,
                spawn_worker=self._spawn_worker)

        try:
            self.result = loop.run_until_complete(_main())
        except Exception as e:  # noqa: BLE001 — serve already emitted ERROR
            self.result = {"reason": "exception", "error": str(e)}
        finally:
            self._ready.set()  # unblock a stop() that raced start()
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self, *, timeout: float = 10.0) -> Optional[dict]:
        """Ask the engine to shut down (set its shutdown event, thread-safe) and
        join the worker thread. Returns serve's result. Safe to call when not
        running."""
        thread = self._thread
        if thread is None:
            return self.result
        self._ready.wait(timeout=5.0)
        loop, ev = self._loop, self._shutdown
        if loop is not None and ev is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass  # loop already finished
        thread.join(timeout=timeout)
        return self.result

    def wait_state(self, target: str, *, timeout: float = 5.0,
                   poll: float = 0.02) -> bool:
        """Block until the snapshot reaches ``target`` (or any of a set), up to
        ``timeout``. Returns True on reach, False on timeout — a GUI/test hook to
        await "Connected ✓" without reaching into the engine."""
        import time
        targets = {target} if isinstance(target, str) else set(target)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.state() in targets:
                return True
            time.sleep(poll)
        return self.state() in targets
