"""identity — G4: the device keypair (Ed25519).

Every origin has exactly one **device identity**: an Ed25519 keypair generated
LOCALLY the first time it enrolls. The private key never leaves the box (§3, §5)
— it is written to a file with 0600 perms (the CLI/daemon analogue of Keychain /
Credential Manager / secret-service; the real OS keystore is a Phase-2 native-app
concern). The control plane only ever sees the PUBLIC key and a human label.

The key does three jobs:
  * identifies the device (its ``device_id`` is derived from the public key, so
    it is self-certifying — you cannot claim a device_id you can't sign for);
  * authenticates the channel by signing a fresh server challenge nonce
    (:mod:`mcp_loops.origin_proto.channel`) — replay-proof, no bearer token;
  * gives a human-verifiable ``fingerprint`` for TOFU confirmation at enrollment
    (§6.1 network-MITM-at-enrollment).

Pure crypto + a tiny file keystore. No sockets, no network, no secrets sent
anywhere. Backed by ``cryptography`` (Ed25519), which is already a dependency.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

# ── base64 helpers (url-safe-less standard b64, no newlines) ──────────────────
def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def public_key_from_b64(pubkey_b64: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(b64d(pubkey_b64))


def fingerprint(pubkey_b64: str) -> str:
    """A short, human-verifiable fingerprint of a public key for TOFU: the
    SHA-256 of the raw key bytes, hex, colon-grouped in pairs (like an SSH
    fingerprint). Shown at enrollment so a user can confirm the device (§6.1)."""
    digest = hashlib.sha256(b64d(pubkey_b64)).hexdigest()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def device_id_from_pubkey(pubkey_b64: str) -> str:
    """A self-certifying device id: the first 16 hex chars of SHA-256(pubkey).
    Because it is derived from the public key, a device cannot present a
    device_id it cannot sign for — the id and the key are bound at the source,
    so the plane never has to trust a self-asserted id."""
    return "dev_" + hashlib.sha256(b64d(pubkey_b64)).hexdigest()[:16]


def verify(pubkey_b64: str, sig_b64: str, message: bytes) -> bool:
    """True iff ``sig_b64`` is a valid Ed25519 signature of ``message`` under
    ``pubkey_b64``. Never raises — a malformed key/sig is simply False, so the
    channel's auth check is a clean boolean."""
    try:
        public_key_from_b64(pubkey_b64).verify(b64d(sig_b64), message)
        return True
    except (InvalidSignature, ValueError, Exception):  # noqa: BLE001
        return False


class DeviceIdentity:
    """An origin's device keypair. Hold the private key in memory + on disk with
    strict perms; expose only sign()/public material. The private key is never
    serialized to the wire and never sent to the plane."""

    def __init__(self, private_key: Ed25519PrivateKey, *,
                 label: Optional[str] = None):
        self._sk = private_key
        self._pk = private_key.public_key()
        self.label = label

    # ── construction ─────────────────────────────────────────────────────────
    @classmethod
    def generate(cls, *, label: Optional[str] = None) -> "DeviceIdentity":
        """Generate a brand-new device keypair. Called once, on the box, at
        ``yard connect`` time."""
        return cls(Ed25519PrivateKey.generate(), label=label)

    @classmethod
    def load(cls, path: str) -> "DeviceIdentity":
        """Load a device identity from its keystore file. Refuses a
        world/group-readable private key file (a leaked key is the whole
        ballgame — §2 bearer-token lesson)."""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        mode = os.stat(path).st_mode & 0o077
        if mode:
            raise PermissionError(
                f"device key {path} is group/world-accessible "
                f"(mode ...{oct(os.stat(path).st_mode)[-3:]}); refusing to load")
        sk = Ed25519PrivateKey.from_private_bytes(b64d(data["privateKey"]))
        return cls(sk, label=data.get("label"))

    @classmethod
    def load_or_create(cls, path: str, *,
                       label: Optional[str] = None) -> "DeviceIdentity":
        """Load the device identity at ``path`` if present, else generate one and
        persist it. Idempotent: a box keeps ONE stable identity across restarts,
        so its device_id (and thus its enrollment) survives a reboot."""
        if os.path.exists(path):
            return cls.load(path)
        ident = cls.generate(label=label)
        ident.save(path)
        return ident

    # ── persistence (0600, private key stays local) ──────────────────────────
    def save(self, path: str) -> None:
        """Write the keystore file with 0600 perms — owner-only. The parent dir
        is created 0700. This is the file-keystore stand-in for the OS keystore
        the native apps will use (§9)."""
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, mode=0o700, exist_ok=True)
        raw = self._sk.private_bytes_raw()
        payload = {
            "version": 1,
            "deviceId": self.device_id,
            "privateKey": b64e(raw),
            "publicKey": self.public_key_b64,
            "label": self.label,
        }
        # write via a 0600 temp then atomic replace, so the key is never briefly
        # world-readable on disk between create and chmod.
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, path)
        os.chmod(path, 0o600)

    # ── public material (safe to send to the plane) ──────────────────────────
    @property
    def public_key_b64(self) -> str:
        return b64e(self._pk.public_bytes_raw())

    @property
    def device_id(self) -> str:
        return device_id_from_pubkey(self.public_key_b64)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_key_b64)

    # ── signing (proves identity on the channel) ─────────────────────────────
    def sign(self, message: bytes) -> str:
        """Sign ``message`` (the server challenge nonce) and return the base64
        signature the channel puts in its ``auth`` frame."""
        return b64e(self._sk.sign(message))
