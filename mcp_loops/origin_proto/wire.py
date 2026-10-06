"""wire — G1: the typed RPC + event wire protocol for the origin channel.

This is the *schema* layer: it defines every frame that crosses the channel, the
protocol-version handshake, the capability-scoped RPC surface (a FIXED allowlist
— never an arbitrary-shell capability), the event kinds, the dispatch-id
idempotency key, and pure `validate_*` conformance checks. It is deliberately
transport-free (no sockets, no asyncio, no crypto) so the wire contract can be
tested in isolation and reused verbatim by both the origin agent and the hub.

Design refs: docs/ORIGINS-PRODUCTION-DESIGN.md §4.3 (transport & protocol),
§6.5 (capability-scoped control, not shell), §7 (dispatch-id idempotency),
§13 (RPC-surface scoping: start minimal — loop.*, products.list, origin.*).

── Frames ──────────────────────────────────────────────────────────────────
Every message on the channel is a JSON object with a ``t`` (type) tag. The
handshake frames come first; after ``welcome`` the channel is bidirectional.

  origin → hub (dial + handshake):
    {t:"hello",     v:<int protocol>, minV:<int>, deviceId, pubkey, label?,
                    manifest?:{manifestVersion, exec, fsRead, fsWrite, agent, rpcs}}
  hub → origin:
    {t:"challenge", nonce:<b64 random>, v:<int negotiated>}         # replay-proof
  origin → hub:
    {t:"auth",      deviceId, sig:<b64 Ed25519(nonce)>}
  hub → origin:
    {t:"welcome",   v:<int negotiated>, sessionId, serverV:<int>, owner?}
    {t:"error",     code, message}                                  # handshake OR steady-state

  hub → origin (steady state — capability-scoped RPC ONLY):
    {t:"rpc", id:<str>, method:<allowlisted>, params:{...},
             dispatchId?:<uuid — REQUIRED on loop.start>,
             owner?:<who the unit is for>, verb?:<exec|fs.read|fs.write|...>}
  origin → hub:
    {t:"rpc_result", id:<str>, ok:<bool>, result?:{...}, error?:{...}}

  origin → hub (steady state — pushed telemetry, replaces the rsync mirror):
    {t:"event", kind:<allowlisted>, seq:<int>, ts:<float>, origin, data:{...}}
    {t:"heartbeat", ts:<float>, load?, busy?}

No frame ever carries a bearer token, a private key, or shell. Identity is the
device keypair proven in ``auth``; authority is the fixed method allowlist.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# ── protocol version ─────────────────────────────────────────────────────────
# Bump PROTOCOL_VERSION on any wire-incompatible change. MIN_SUPPORTED is the
# oldest peer version this build will still speak (auto-update + long-offline
# origins mean the plane must talk to old and new harnesses — §4.3). The
# handshake negotiates min(hello.v, serverV) and REJECTS if that drops below
# either side's floor, rather than silently assuming a version.
PROTOCOL_VERSION = 1
MIN_SUPPORTED = 1

# ── frame type tags ──────────────────────────────────────────────────────────
T_HELLO = "hello"
T_CHALLENGE = "challenge"
T_AUTH = "auth"
T_WELCOME = "welcome"
T_ERROR = "error"
T_RPC = "rpc"
T_RPC_RESULT = "rpc_result"
T_EVENT = "event"
T_HEARTBEAT = "heartbeat"
# ── the pre-auth pairing handshake (§6.2 remote pairing-claim transport) ───────
# A box that is NOT yet enrolled cannot complete the device-key hello→auth→welcome
# handshake (the plane has no record of its key). `pair` is the ONE pre-auth frame
# it may send instead: it carries a short-lived pairing code (the out-of-band trust
# root, issued on the Hub) + the box's PUBLIC key. The Hub claims the code, records
# the pubkey, and answers `paired` with the device_id + the HUB's own cert so the
# box can PIN it (§5.1(4)(b): the Hub cert MUST be delivered at pairing time). No
# secret is ever transmitted; the private key never leaves the box.
T_PAIR = "pair"
T_PAIRED = "paired"

FRAME_TYPES = frozenset({
    T_HELLO, T_CHALLENGE, T_AUTH, T_WELCOME, T_ERROR,
    T_RPC, T_RPC_RESULT, T_EVENT, T_HEARTBEAT,
    T_PAIR, T_PAIRED,
})

# ── the capability-scoped RPC surface (§6.5, §13) ────────────────────────────
# A FIXED allowlist. There is deliberately NO "shell"/"exec"/"eval" method —
# that retires the mac-origin URL-token remote-shell debt (§2). Expansion is a
# code change + review, never a runtime string. Start minimal: loop.*,
# products.list, origin.describe.
RPC_METHODS = frozenset({
    "loop.list",     # -> {loops:[...]}
    "loop.get",      # {name} -> {config, run}
    "loop.start",    # {name, slug?} + dispatchId -> {ok, name, state}
    "loop.stop",     # {name} -> {ok, name}
    "loop.status",   # {name, tail?} -> {run, recent, ...}
    "products.list", # -> {products:[...]}
    "origin.describe",  # -> {origin, capabilities, harness, products}
    # capabilities.probe — READ-ONLY: the origin runs its OWN subscription-CLI
    # probe (origins_probe.probe_local_clis: PATH + credential-file evidence) and
    # answers {ok, cliCapabilities:[{cli, authed, applied, note, lastProbed}],
    # lastProbed}. No argv, no exec; gated by the origin's advertised manifest
    # rpcs like any other read RPC.
    "capabilities.probe",
    # origin.run — the ONE general exec primitive (HUB-FABRIC §2). Runs argv (NOT
    # a shell string) on the origin's own box, capability-gated per origin
    # (fail-closed RunAllowlist), audited, dispatch-id idempotent. This is the
    # sanctioned "code change + review" expansion of the fixed allowlist — never a
    # runtime shell string. fs.pull/fs.push are COMPOSITIONS of it, not new methods.
    "origin.run",    # {argv:[...], stdin?, cwd?, env?, timeout?, stream?} + dispatchId
                     #   -> {exitCode, stdout:<b64>, stderr:<b64>, durationMs}
})

# loop.start and origin.run are the methods that mutate by launching work, so they
# REQUIRE a dispatch-id idempotency key (§7): the same id keys the run, so a
# redelivery re-attaches instead of double-running. For origin.run this is
# critical — an exec primitive must never double-execute on redelivery.
RPC_METHODS_REQUIRING_DISPATCH_ID = frozenset({"loop.start", "origin.run"})

# ── event kinds (pushed origin → hub; replaces rsync mirror freshness) ───────
EVENT_KINDS = frozenset({
    "heartbeat",   # liveness + load (also its own frame T_HEARTBEAT; kept here
                   # so an event-stream consumer can treat it uniformly)
    "run",         # a run started / ended
    "turn",        # a turn completed
    "status",      # an agent status report (mirrors status.jsonl lines)
    "result",      # a loop's terminal result envelope
    # origin.run streaming (HUB-FABRIC §2.1/§2.4): a long/streamed exec pushes its
    # output as run.chunk events (ordered by the frame seq counter, correlated to
    # the caller by data.dispatchId) and closes with run.end. "stream" is a
    # parameter of the ONE origin.run primitive, not a second verb.
    "run.chunk",   # {dispatchId, fd:"out"|"err", b64} — a slice of exec output
    "run.end",     # {dispatchId, exitCode, durationMs} — terminal, closes a stream
})

# ── error codes ──────────────────────────────────────────────────────────────
E_VERSION_SKEW = "version_skew"
E_UNKNOWN_DEVICE = "unknown_device"
E_BAD_SIGNATURE = "bad_signature"
E_REVOKED = "revoked"
E_UNKNOWN_METHOD = "unknown_method"
E_BAD_REQUEST = "bad_request"
E_MISSING_DISPATCH_ID = "missing_dispatch_id"
# §5.1(4): a non-loopback origin.run was refused because its channel is not
# mTLS device-bound (plaintext / CERT_NONE / unbound) — the Hub-side half of the
# bidirectional transport hard gate.
E_TLS_REQUIRED = "tls_required"
# §6.2: a pre-auth `pair` claim was refused (bad / expired / already-consumed
# pairing code, or a malformed public key). The box shows this to the user and
# exits non-zero; it never falls through to an unpaired dial.
E_PAIRING_FAILED = "pairing_failed"
# P6: the caller's owner does not own the target device (or the device is
# unknown to the store) — the Hub refuses before any frame is sent.
E_NOT_OWNER = "not_owner"
# P3: the unit's verb/rpc is not in the target origin's capability manifest —
# refused by the Hub before send, and re-refused by the origin on receipt.
E_NOT_PERMITTED = "not_permitted"
E_INTERNAL = "internal"

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class ProtocolError(ValueError):
    """A frame violates the wire schema. Carries a stable ``code`` so the peer
    can answer with a typed ``error`` frame rather than a naked disconnect."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ── version negotiation (§4.3 handshake) ─────────────────────────────────────
def negotiate_version(peer_v: int, peer_min: int,
                      *, my_v: int = PROTOCOL_VERSION,
                      my_min: int = MIN_SUPPORTED) -> int:
    """Return the highest protocol version BOTH sides can speak, or raise
    ``ProtocolError(E_VERSION_SKEW)`` when the ranges don't overlap. Negotiated,
    never assumed — a long-offline origin on an old version is either spoken to
    on that version or cleanly refused, not silently mis-framed."""
    if not isinstance(peer_v, int) or not isinstance(peer_min, int):
        raise ProtocolError(E_BAD_REQUEST, "hello v/minV must be integers")
    hi = min(peer_v, my_v)
    lo = max(peer_min, my_min)
    if hi < lo:
        raise ProtocolError(
            E_VERSION_SKEW,
            f"no common protocol version (peer {peer_min}..{peer_v}, "
            f"me {my_min}..{my_v})")
    return hi


# ── frame builders (canonical shapes; keeps producers honest) ────────────────
def hello(device_id: str, pubkey_b64: str, *, label: Optional[str] = None,
          v: int = PROTOCOL_VERSION, min_v: int = MIN_SUPPORTED,
          manifest: Optional[dict] = None) -> dict:
    f = {"t": T_HELLO, "v": v, "minV": min_v,
         "deviceId": device_id, "pubkey": pubkey_b64}
    if label:
        f["label"] = label
    if manifest is not None:
        # P3 capability manifest — what this origin permits. The Hub stores it
        # only AFTER auth succeeds (a pre-auth peer cannot plant one).
        f["manifest"] = manifest
    return f


def pair(code: str, pubkey_b64: str, *, label: Optional[str] = None,
         cert_b64: Optional[str] = None, origin_id: Optional[str] = None,
         v: int = PROTOCOL_VERSION, min_v: int = MIN_SUPPORTED) -> dict:
    """The pre-auth pairing claim (box → Hub). Carries the out-of-band pairing
    code + the box's PUBLIC key (never a secret) + a human label + (optionally)
    the box's self-signed device cert (public, base64 PEM) so a binding Hub can
    trust exactly that cert for the box's later mTLS dials. ``origin_id`` (S-1 c,
    optional + additive so v1 Hubs ignore it) is the box's logical originId."""
    f = {"t": T_PAIR, "v": v, "minV": min_v, "code": code, "pubkey": pubkey_b64}
    if origin_id:
        f["originId"] = origin_id
    if label:
        f["label"] = label
    if cert_b64:
        f["cert"] = cert_b64
    return f


def paired(device_id: str, fingerprint: str, *, hub_cert_b64: Optional[str] = None,
           v: int = PROTOCOL_VERSION) -> dict:
    """The Hub's answer to a successful pairing claim. Returns the enrolled
    device_id + the key fingerprint (TOFU confirmation) and — critically — the
    Hub's own cert (base64 PEM) so the box can PIN it for every subsequent mTLS
    dial (§5.1(4)(b))."""
    f = {"t": T_PAIRED, "v": v, "deviceId": device_id, "fingerprint": fingerprint}
    if hub_cert_b64 is not None:
        f["hubCert"] = hub_cert_b64
    return f


def challenge(nonce_b64: str, v: int) -> dict:
    return {"t": T_CHALLENGE, "nonce": nonce_b64, "v": v}


def auth(device_id: str, sig_b64: str) -> dict:
    return {"t": T_AUTH, "deviceId": device_id, "sig": sig_b64}


def welcome(session_id: str, v: int, *, server_v: int = PROTOCOL_VERSION,
            owner: Optional[str] = None) -> dict:
    f = {"t": T_WELCOME, "v": v, "sessionId": session_id, "serverV": server_v}
    if owner is not None:
        f["owner"] = owner  # P3: the enrolled owner — the origin pins it per session
    return f


def error(code: str, message: str) -> dict:
    return {"t": T_ERROR, "code": code, "message": message}


def rpc(rpc_id: str, method: str, params: Optional[dict] = None,
        *, dispatch_id: Optional[str] = None, owner: Optional[str] = None,
        verb: Optional[str] = None) -> dict:
    f = {"t": T_RPC, "id": rpc_id, "method": method, "params": params or {}}
    if dispatch_id is not None:
        f["dispatchId"] = dispatch_id
    if owner is not None:
        f["owner"] = owner   # P3: the owner the Hub authorised this unit for
    if verb is not None:
        f["verb"] = verb     # P3: the unit's verb (origin.run: exec|fs.read|fs.write)
    return f


def rpc_result(rpc_id: str, *, ok: bool, result: Any = None,
               err: Optional[dict] = None) -> dict:
    f = {"t": T_RPC_RESULT, "id": rpc_id, "ok": ok}
    if ok:
        f["result"] = result if result is not None else {}
    else:
        f["error"] = err or {"code": E_INTERNAL, "message": "unspecified"}
    return f


def event(kind: str, seq: int, ts: float, origin: str,
          data: Optional[dict] = None) -> dict:
    return {"t": T_EVENT, "kind": kind, "seq": seq, "ts": ts,
            "origin": origin, "data": data or {}}


def heartbeat(ts: float, *, load: Optional[float] = None,
              busy: Optional[bool] = None) -> dict:
    f = {"t": T_HEARTBEAT, "ts": ts}
    if load is not None:
        f["load"] = load
    if busy is not None:
        f["busy"] = busy
    return f


# ── conformance validators (pure; the test bar's protocol-conformance suite) ──
def _require(cond: bool, code: str, message: str) -> None:
    if not cond:
        raise ProtocolError(code, message)


def validate_frame(frame: Any) -> str:
    """Validate ANY frame against the wire schema and return its type tag.
    Raises ``ProtocolError`` on the first violation. This is the single entry
    point the conformance suite and both channel endpoints run every inbound
    frame through — one schema, no drift."""
    _require(isinstance(frame, dict), E_BAD_REQUEST, "frame must be a JSON object")
    t = frame.get("t")
    _require(isinstance(t, str), E_BAD_REQUEST, "frame missing string 't'")
    _require(t in FRAME_TYPES, E_BAD_REQUEST, f"unknown frame type {t!r}")

    if t == T_HELLO:
        _require(isinstance(frame.get("v"), int), E_BAD_REQUEST, "hello.v int")
        _require(isinstance(frame.get("minV"), int), E_BAD_REQUEST, "hello.minV int")
        _require(bool(frame.get("deviceId")), E_BAD_REQUEST, "hello.deviceId required")
        _require(bool(frame.get("pubkey")), E_BAD_REQUEST, "hello.pubkey required")
        # the manifest's CONTENT is judged by origin_policy (a bad one degrades to
        # describe-only, not a refused handshake); the wire only guards the type.
        _require(frame.get("manifest") is None or isinstance(frame["manifest"], dict),
                 E_BAD_REQUEST, "hello.manifest must be an object")
    elif t == T_PAIR:
        _require(isinstance(frame.get("v"), int), E_BAD_REQUEST, "pair.v int")
        _require(isinstance(frame.get("minV"), int), E_BAD_REQUEST, "pair.minV int")
        _require(bool(frame.get("code")), E_BAD_REQUEST, "pair.code required")
        _require(bool(frame.get("pubkey")), E_BAD_REQUEST, "pair.pubkey required")
        _require(frame.get("originId") is None or isinstance(frame["originId"], str),
                 E_BAD_REQUEST, "pair.originId must be a string")
    elif t == T_PAIRED:
        _require(isinstance(frame.get("v"), int), E_BAD_REQUEST, "paired.v int")
        _require(bool(frame.get("deviceId")), E_BAD_REQUEST, "paired.deviceId required")
        _require(bool(frame.get("fingerprint")), E_BAD_REQUEST,
                 "paired.fingerprint required")
    elif t == T_CHALLENGE:
        _require(bool(frame.get("nonce")), E_BAD_REQUEST, "challenge.nonce required")
        _require(isinstance(frame.get("v"), int), E_BAD_REQUEST, "challenge.v int")
    elif t == T_AUTH:
        _require(bool(frame.get("deviceId")), E_BAD_REQUEST, "auth.deviceId required")
        _require(bool(frame.get("sig")), E_BAD_REQUEST, "auth.sig required")
    elif t == T_WELCOME:
        _require(isinstance(frame.get("v"), int), E_BAD_REQUEST, "welcome.v int")
        _require(bool(frame.get("sessionId")), E_BAD_REQUEST, "welcome.sessionId required")
    elif t == T_ERROR:
        _require(bool(frame.get("code")), E_BAD_REQUEST, "error.code required")
    elif t == T_RPC:
        validate_rpc_request(frame)
    elif t == T_RPC_RESULT:
        _require(bool(frame.get("id")), E_BAD_REQUEST, "rpc_result.id required")
        _require(isinstance(frame.get("ok"), bool), E_BAD_REQUEST, "rpc_result.ok bool")
    elif t == T_EVENT:
        validate_event(frame)
    elif t == T_HEARTBEAT:
        _require(isinstance(frame.get("ts"), (int, float)), E_BAD_REQUEST,
                 "heartbeat.ts number")
    return t


def validate_rpc_request(frame: dict) -> None:
    """Validate an ``rpc`` frame: known-allowlisted method (NEVER shell), a
    string id, an object params, and a well-formed dispatch-id on the methods
    that require one (§7). Raises ``ProtocolError`` with a typed code."""
    _require(isinstance(frame, dict), E_BAD_REQUEST, "rpc frame must be object")
    _require(bool(frame.get("id")) and isinstance(frame["id"], str),
             E_BAD_REQUEST, "rpc.id must be a non-empty string")
    method = frame.get("method")
    _require(isinstance(method, str), E_BAD_REQUEST, "rpc.method must be a string")
    _require(method in RPC_METHODS, E_UNKNOWN_METHOD,
             f"method {method!r} is not on the capability allowlist")
    params = frame.get("params", {})
    _require(isinstance(params, dict), E_BAD_REQUEST, "rpc.params must be an object")
    for k in ("owner", "verb"):
        _require(frame.get(k) is None or (isinstance(frame[k], str) and frame[k]),
                 E_BAD_REQUEST, f"rpc.{k} must be a non-empty string")
    if method in RPC_METHODS_REQUIRING_DISPATCH_ID:
        did = frame.get("dispatchId")
        _require(did is not None, E_MISSING_DISPATCH_ID,
                 f"{method} requires a client-generated dispatchId (§7)")
        _require(is_dispatch_id(did), E_BAD_REQUEST,
                 "dispatchId must be a UUID string")


def validate_event(frame: dict) -> None:
    """Validate an ``event`` frame: known kind, integer seq, numeric ts,
    origin id, object data. Ordered + idempotent consumption is the plane's job;
    this only guards the shape."""
    _require(isinstance(frame, dict), E_BAD_REQUEST, "event frame must be object")
    kind = frame.get("kind")
    _require(kind in EVENT_KINDS, E_BAD_REQUEST, f"unknown event kind {kind!r}")
    _require(isinstance(frame.get("seq"), int), E_BAD_REQUEST, "event.seq int")
    _require(isinstance(frame.get("ts"), (int, float)), E_BAD_REQUEST, "event.ts number")
    _require(bool(frame.get("origin")), E_BAD_REQUEST, "event.origin required")
    _require(isinstance(frame.get("data", {}), dict), E_BAD_REQUEST, "event.data object")


def is_dispatch_id(value: Any) -> bool:
    """True iff ``value`` is a UUID-shaped dispatch-id string (§7). The client
    generates it; the origin keys the run on it and dedupes redelivery."""
    return isinstance(value, str) and bool(_UUID_RE.match(value))
