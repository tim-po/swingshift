"""enrollment — G4: control-plane enrollment, the device registry, revocation,
and the audit log.

This replaces ``curl …/<token> | bash`` and the URL-bearer-token remote shell
(§2, §5). The flow, with NO secret material ever transmitted to the origin:

  1. The user asks the plane to enroll a device → the plane mints a short-lived
     **pairing code** (:meth:`EnrollmentStore.issue_pairing_code`) shown in the
     browser/CLI. It authorizes ONE enrollment and expires.
  2. On the box, the origin agent generates a device keypair
     (:class:`~mcp_loops.origin_proto.identity.DeviceIdentity`) and calls
     :meth:`EnrollmentStore.claim_pairing_code` with the code + its PUBLIC key +
     a human label. The plane records the public key + label + device_id and
     returns the device_id + a key fingerprint for TOFU confirmation.
  3. Thereafter the channel is authenticated by the keypair (the plane looks the
     device up by :meth:`get_device` and verifies its challenge signature).

Revocation (§6.3) must TEAR DOWN, not merely invalidate: :meth:`revoke` adds the
device to a denylist checked on every reconnect + per-RPC AND fires an injected
``on_revoke`` callback so the live channel server can drop the socket
server-side. Every enroll / revoke / dispatch is written to an append-only audit
log (§6.5).

File-backed + fail-soft, matching the connect/registry conventions already in
this repo (single writer per box; atomic replace; no cross-process lock). The
store holds ONLY public keys + metadata — never a private key, never a secret.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from typing import Any, Callable, Optional

from mcp_loops.origin_proto import identity

# device status vocabulary
ENROLLED = "enrolled"
REVOKED = "revoked"

# P6: the owner a device belongs to when none is threaded — the single pilot
# owner of a one-tenant hub. Same env + default as ``mcp_loops.devices.
# DEFAULT_OWNER`` (the Hub read-model); defined here because this module is
# origin_wire and may not import the Hub (test_package_boundaries). A test pins
# the two equal.
DEFAULT_OWNER = os.environ.get("LOOPYARD_PILOT_OWNER", "local")

# pairing code lifetime (seconds) — short-lived (§5). A code authorizes a single
# enrollment and is consumed on claim; it also expires so an unused code is not a
# standing credential.
PAIRING_TTL_SEC = 10 * 60

# audit event kinds
A_ENROLL = "enroll"
A_REVOKE = "revoke"
A_DISPATCH = "dispatch"          # transport-level: an RPC frame left the Hub (method+dispatchId)
A_RUN = "run"                    # exec-level: an origin.run completed — REAL argv + outcome (§5.1(3))
A_RUN_DENIED = "run_denied"      # exec-level: an origin.run was REFUSED — fail-closed refusals are audited too
A_RUN_REATTACHED = "run_reattached"  # exec-level: a reused dispatchId hit the origin ledger — NOTHING ran (P3a)
A_RUN_UNCONFIRMED = "run_unconfirmed"  # exec-level: transport dropped/timed out — outcome UNKNOWN, may have run (P3b)
A_AUTH_OK = "auth_ok"
A_AUTH_DENIED = "auth_denied"
A_MANIFEST = "manifest"          # P3: an authenticated origin's capability manifest was stored (status + digest)
A_ONBOARDING = "onboarding"      # readiness-level: a live capability diff ran (§6.4) — required CLIs vs the origin's real auth-state


def device_owner(rec: dict) -> str:
    """The owner of a device record (P6). Records enrolled before owner-threading
    carry none — they were minted by a single-tenant hub, so they belong to that
    hub's pilot owner (``DEFAULT_OWNER``), never to 'anyone'."""
    return str(rec.get("owner") or DEFAULT_OWNER)


class EnrollmentError(RuntimeError):
    """An enrollment operation was refused (bad/expired/consumed pairing code,
    duplicate enrollment, unknown device). Carries a stable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _canon_origin_id(value: Any) -> Optional[str]:
    """A logical originId (S-1 c) in canonical ``str(uuid)`` form, else None."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _now(now: Optional[float]) -> float:
    # Time is injected (the callers pass time.time()); this module never reads the
    # clock itself, so it stays pure/testable and chaos tests can pin time.
    if now is None:
        raise ValueError("enrollment requires an explicit `now` timestamp")
    return float(now)


class EnrollmentStore:
    """The control-plane's device registry, pairing codes, denylist, and audit
    log — all under one root dir. This is the identity half of the control
    plane; the registry/router half (health, dispatch routing) is the other
    engineer's G5 and layers on top of :meth:`get_device` / :meth:`is_revoked`.

    Layout under ``root``::

        devices/<device_id>.json   {deviceId, owner, publicKey, label, status,
                                    enrolledAt, revokedAt, fingerprint}
        pairing/<code>.json        {code, owner, createdAt, expiresAt, label,
                                    consumed}
        manifests/<device_id>.json {deviceId, manifest, status, detail, digest,
                                    receivedAt} — P3 capability manifest, written
                                    only after the device authenticated
        audit.jsonl                append-only enroll/revoke/dispatch/run/auth log
                                   (run + run_denied carry the REAL argv, §5.1(3))
    """

    def __init__(self, root: str, *,
                 on_revoke: Optional[Callable[[str], None]] = None):
        self.root = os.path.abspath(root)
        self.devices_dir = os.path.join(self.root, "devices")
        self.pairing_dir = os.path.join(self.root, "pairing")
        self.manifests_dir = os.path.join(self.root, "manifests")
        self.certs_dir = os.path.join(self.root, "certs")
        self.audit_path = os.path.join(self.root, "audit.jsonl")
        # Fired on revoke so the live ChannelServer can tear the socket down
        # (§6.3). Optional — the store denylists regardless; the callback is the
        # active-teardown half.
        self._on_revoke = on_revoke
        os.makedirs(self.devices_dir, exist_ok=True)
        os.makedirs(self.pairing_dir, exist_ok=True)

    # ── low-level fs (atomic, fail-soft reads) ───────────────────────────────
    def _write_json(self, path: str, data: dict) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)

    def _read_json(self, path: str) -> Optional[dict]:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _device_path(self, device_id: str) -> str:
        # device_id is self-certifying (identity.device_id_from_pubkey) but still
        # guard the filename against traversal.
        safe = os.path.basename(device_id)
        return os.path.join(self.devices_dir, f"{safe}.json")

    def _pairing_path(self, code: str) -> str:
        return os.path.join(self.pairing_dir, f"{os.path.basename(code)}.json")

    # ── audit log (§6.5) ─────────────────────────────────────────────────────
    def audit(self, kind: str, *, now: float, **fields: Any) -> None:
        """Append one audit line. Fail-soft: an audit write must never raise into
        the enrollment/dispatch path (losing a log line beats crashing a
        dispatch), but every enroll/revoke/dispatch attempts it."""
        rec = {"ts": _now(now), "kind": kind, **fields}
        try:
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def read_audit(self) -> list[dict]:
        out: list[dict] = []
        try:
            with open(self.audit_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return out

    # ── pairing codes (§5 step 1) ────────────────────────────────────────────
    def issue_pairing_code(self, *, now: float,
                           label: Optional[str] = None,
                           owner: Optional[str] = None) -> dict:
        """Mint a short-lived, single-use pairing code. Returned to the user in
        the browser/CLI; NOT a channel credential — it only authorizes recording
        a public key, and expires. The code carries the ISSUING owner (P6), so the
        device it enrolls belongs to whoever minted the code — never to whoever
        happens to present it."""
        t = _now(now)
        # 8 url-safe chars ≈ 48 bits — ample for a 10-minute single-use code.
        code = secrets.token_urlsafe(6)[:8]
        rec = {"code": code, "owner": owner or DEFAULT_OWNER, "createdAt": t,
               "expiresAt": t + PAIRING_TTL_SEC, "label": label,
               "consumed": False}
        self._write_json(self._pairing_path(code), rec)
        return rec

    def claim_pairing_code(self, code: str, pubkey_b64: str, *, now: float,
                           label: Optional[str] = None,
                           origin_id: Optional[str] = None) -> dict:
        """Consume a pairing code and enroll the device that presents its PUBLIC
        key. Raises :class:`EnrollmentError` on a bad/expired/consumed code.
        Records only public material — no secret is transmitted or stored (§5).
        Returns the device record (with its TOFU fingerprint)."""
        t = _now(now)
        path = self._pairing_path(code)
        rec = self._read_json(path)
        if rec is None:
            raise EnrollmentError("bad_pairing_code", "unknown pairing code")
        if rec.get("consumed"):
            raise EnrollmentError("pairing_consumed", "pairing code already used")
        if t > float(rec.get("expiresAt", 0)):
            raise EnrollmentError("pairing_expired", "pairing code expired")
        # the label the OWNER minted the code with wins (P2.5: two boxes must not
        # both register as the claimant's default); the claim's label fills in.
        eff_label = rec.get("label") or label
        # P6: the owner rides the code, not the claim — a claimant cannot choose it.
        device = self.enroll(pubkey_b64, label=eff_label, now=t,
                             owner=rec.get("owner") or DEFAULT_OWNER,
                             origin_id=origin_id)
        rec["consumed"] = True
        rec["consumedAt"] = t
        rec["deviceId"] = device["deviceId"]
        self._write_json(path, rec)
        return device

    # ── enrollment / registry ────────────────────────────────────────────────
    def enroll(self, pubkey_b64: str, *, now: float,
               label: Optional[str] = None,
               owner: Optional[str] = None,
               origin_id: Optional[str] = None) -> dict:
        """Record a device's PUBLIC key + label + ``owner`` (P6). The device_id is
        derived from the key (self-certifying), so re-enrolling the same key is
        idempotent — it re-activates a revoked record rather than duplicating it
        (a box that keeps its keypair keeps its identity). Re-enrolling a key that
        another owner already holds is REFUSED (``owner_conflict``) — a second
        owner's pairing code can never take over someone else's device. Audited.

        ``origin_id`` (S-1 c) is the box's LOGICAL originId — a UUIDv4 minted once
        per device state dir, independent of the key, so re-enrolling with a NEW
        key records the SAME originId. Stored canonical; a malformed/absent value
        is recorded as ``None`` (a pre-S-1 client still enrolls — G-3), and a
        re-enroll of the same key without one keeps the prior value. The ``enroll``
        audit carries ``publicKey`` + ``originId`` (spec §9 S-1 c)."""
        t = _now(now)
        try:
            pubkey = identity.b64d(pubkey_b64)
        except Exception as e:  # noqa: BLE001
            raise EnrollmentError("bad_pubkey", f"invalid public key: {e}") from e
        if len(pubkey) != 32:
            raise EnrollmentError("bad_pubkey", "Ed25519 public key must be 32 bytes")
        device_id = identity.device_id_from_pubkey(pubkey_b64)
        eff_owner = owner or DEFAULT_OWNER
        prior = self.get_device(device_id)
        if prior is not None and device_owner(prior) != eff_owner:
            self.audit(A_AUTH_DENIED, now=t, deviceId=device_id,
                       reason="owner_conflict", owner=eff_owner)
            raise EnrollmentError("owner_conflict",
                                  "device is enrolled to a different owner")
        eff_origin = _canon_origin_id(origin_id)
        if eff_origin is None and prior is not None:
            eff_origin = _canon_origin_id(prior.get("originId"))
        rec = {
            "deviceId": device_id,
            "owner": eff_owner,
            "publicKey": pubkey_b64,
            "label": label,
            "status": ENROLLED,
            "enrolledAt": t,
            "revokedAt": None,
            "fingerprint": identity.fingerprint(pubkey_b64),
            "originId": eff_origin,
        }
        self._write_json(self._device_path(device_id), rec)
        self.audit(A_ENROLL, now=t, deviceId=device_id, label=label,
                   owner=eff_owner, fingerprint=rec["fingerprint"],
                   publicKey=pubkey_b64, originId=eff_origin)
        return rec

    def get_device(self, device_id: str) -> Optional[dict]:
        return self._read_json(self._device_path(device_id))

    # ── P3 capability manifest sidecar ───────────────────────────────────────
    def set_manifest(self, device_id: str, manifest: dict, *, now: float,
                     status: str, detail: Optional[str] = None,
                     digest: Optional[str] = None) -> dict:
        """Store the manifest an AUTHENTICATED device advertised (the caller —
        ChannelServer — only calls this after ``authenticate`` succeeded).
        ``manifest`` is what the Hub will ENFORCE (the fallback when the origin's
        was missing/invalid); ``status`` says which. Audited."""
        t = _now(now)
        if self.get_device(device_id) is None:
            raise EnrollmentError("unknown_device", "no such device")
        os.makedirs(self.manifests_dir, exist_ok=True)
        rec = {"deviceId": device_id, "manifest": manifest, "status": status,
               "detail": detail, "digest": digest, "receivedAt": t}
        safe = os.path.basename(device_id)
        self._write_json(os.path.join(self.manifests_dir, f"{safe}.json"), rec)
        self.audit(A_MANIFEST, now=t, deviceId=device_id, status=status,
                   digest=digest)
        return rec

    def get_manifest(self, device_id: str) -> Optional[dict]:
        safe = os.path.basename(device_id)
        return self._read_json(os.path.join(self.manifests_dir, f"{safe}.json"))

    # ── device TLS cert sidecar (P2.5: mTLS binding on a standalone Hub) ──────
    def set_device_cert(self, device_id: str, cert_pem: bytes) -> None:
        """Record the self-signed cert an enrolled device delivered in its pairing
        claim. Refused unless the cert carries exactly the enrolled public key —
        the cert is only ever a TLS-layer carrier of the key we already trust."""
        from mcp_loops.origin_proto import tls as _tls
        rec = self.get_device(device_id)
        if rec is None:
            raise EnrollmentError("unknown_device", "no such device")
        if _tls.pubkey_b64_from_cert_pem(cert_pem) != rec.get("publicKey"):
            raise EnrollmentError("bad_cert",
                                  "device cert does not carry the enrolled key")
        os.makedirs(self.certs_dir, exist_ok=True)
        path = os.path.join(self.certs_dir, f"{os.path.basename(device_id)}.pem")
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(cert_pem)
        os.replace(tmp, path)

    def get_device_cert(self, device_id: str) -> Optional[bytes]:
        path = os.path.join(self.certs_dir, f"{os.path.basename(device_id)}.pem")
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def enrolled_device_certs(self) -> list[bytes]:
        """Certs of every enrolled (not revoked) device that delivered one."""
        out: list[bytes] = []
        for rec in self.list_devices():
            if rec.get("status") != ENROLLED:
                continue
            pem = self.get_device_cert(rec.get("deviceId") or "")
            if pem:
                out.append(pem)
        return out

    def list_devices(self) -> list[dict]:
        out: list[dict] = []
        try:
            names = sorted(os.listdir(self.devices_dir))
        except OSError:
            return out
        for name in names:
            if not name.endswith(".json") or name.endswith(".tmp"):
                continue
            rec = self._read_json(os.path.join(self.devices_dir, name))
            if rec is not None:
                out.append(rec)
        return out

    def is_revoked(self, device_id: str) -> bool:
        """The denylist check run on EVERY reconnect + per-RPC (§6.3). An unknown
        device is treated as revoked (fail-closed): if we have no record, we do
        not admit it."""
        rec = self.get_device(device_id)
        if rec is None:
            return True
        return rec.get("status") != ENROLLED

    def authenticate(self, device_id: str, nonce: bytes, sig_b64: str, *,
                     now: float) -> dict:
        """The plane-side of the channel handshake: look the device up, refuse if
        revoked/unknown (fail-closed), and verify the Ed25519 signature over the
        challenge nonce. Returns the device record on success; raises
        :class:`EnrollmentError` otherwise. Audited both ways."""
        t = _now(now)
        rec = self.get_device(device_id)
        if rec is None:
            self.audit(A_AUTH_DENIED, now=t, deviceId=device_id, reason="unknown")
            raise EnrollmentError("unknown_device", "device is not enrolled")
        if rec.get("status") != ENROLLED:
            self.audit(A_AUTH_DENIED, now=t, deviceId=device_id, reason="revoked")
            raise EnrollmentError("revoked", "device credential is revoked")
        if not identity.verify(rec["publicKey"], sig_b64, nonce):
            self.audit(A_AUTH_DENIED, now=t, deviceId=device_id, reason="bad_signature")
            raise EnrollmentError("bad_signature", "challenge signature invalid")
        self.audit(A_AUTH_OK, now=t, deviceId=device_id)
        return rec

    def revoke(self, device_id: str, *, now: float,
               reason: Optional[str] = None) -> dict:
        """Revoke a device. This (i) denylists the key — checked on every
        reconnect + per-RPC via :meth:`is_revoked`/:meth:`authenticate` — AND
        (ii) fires ``on_revoke`` so the live ChannelServer TEARS DOWN the open
        socket server-side, not merely invalidating for next time (§6.3). If the
        origin is asleep there is simply no live socket to cut; the next
        reconnect is refused. Audited."""
        t = _now(now)
        rec = self.get_device(device_id)
        if rec is None:
            raise EnrollmentError("unknown_device", "no such device")
        rec["status"] = REVOKED
        rec["revokedAt"] = t
        rec["revokeReason"] = reason
        self._write_json(self._device_path(device_id), rec)
        self.audit(A_REVOKE, now=t, deviceId=device_id, reason=reason)
        # active teardown — the half that makes "revoke" real, not just "next
        # time". Fail-soft: a teardown-hook error must not leave the record
        # un-revoked (the denylist already stands).
        if self._on_revoke is not None:
            try:
                self._on_revoke(device_id)
            except Exception:  # noqa: BLE001
                pass
        return rec
