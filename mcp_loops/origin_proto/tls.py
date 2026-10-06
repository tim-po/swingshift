"""tls — G2.5: wss/mTLS transport hardening, keyed to the device keypair.

Round-1 dialed/served the origin channel over PLAINTEXT loopback (:mod:`wsio`
note): fine for the in-process dogfood, but a REMOTE origin (the Mac, later)
must dial the hub over an untrusted network. This module adds the missing
transport crypto WITHOUT changing the auth model:

  * the channel's identity + authentication stay the Ed25519 device-key
    challenge/response (:mod:`channel`) — replay-proof, no bearer token in a URL,
    ever. TLS wraps that; it does not replace it.
  * TLS is *keyed to the same device keypair*: each side presents a self-signed
    X.509 certificate whose public key IS its Ed25519 device key and whose
    subject/SAN encodes its ``device_id``. No second PKI, no CA to run — the
    device identity that already authenticates the channel also authenticates the
    tunnel. (OpenSSL 1.1.1+/3.x sign+verify Ed25519 certs natively.)
  * mutual TLS: the origin pins the hub's cert (encrypted + hub-authenticated)
    and PRESENTS its own device cert; the hub binds that presented cert back to
    the device key proven in the app-layer ``auth`` frame
    (:func:`peer_pubkey_b64` + the channel's binding check) so a TLS session can
    never be replayed under a different app identity.

Trust model (self-signed, self-issued): there is no CA chain to walk, so trust
is by EXACT cert (a pinned bundle), the same trust-on-first-use posture the
enrollment store already takes for pubkeys (§6.1). :func:`server_ssl_context`
takes the exact client certs it will admit; :func:`client_ssl_context` takes the
exact server cert it will pin. A cert's public key is cross-checked against the
device pubkey at every layer, so a valid-but-wrong-key cert is refused.

Pure crypto + ``ssl`` context construction. No sockets here — the contexts are
handed to :mod:`wsio` (``asyncio.open_connection`` / ``start_server`` take
``ssl=`` straight through). Backed by ``cryptography`` (already a dependency).
"""

from __future__ import annotations

import atexit
import datetime
import os
import ssl
import tempfile
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from cryptography.x509.oid import NameOID

from mcp_loops.origin_proto import identity as ident_mod

# Long-lived self-signed certs: the device keypair is the durable identity, so
# the cert is just a transport envelope for it. A fixed, clock-free validity
# window keeps cert minting deterministic (no now()-dependent test flake) and
# wide enough that a long-offline origin's cert has not "expired" on return —
# revocation is the EnrollmentStore's denylist job, not X.509 expiry (§6.3).
_NOT_BEFORE = datetime.datetime(2020, 1, 1)
_NOT_AFTER = datetime.datetime(2099, 1, 1)

# temp cert/key files we materialize for ssl.load_cert_chain (which wants paths,
# not memory); cleaned at exit so a throwaway harness leaves nothing behind.
_TEMP_PATHS: list[str] = []


def _cleanup_temp() -> None:
    for p in _TEMP_PATHS:
        try:
            os.unlink(p)
        except OSError:
            pass


atexit.register(_cleanup_temp)


class CertMaterial:
    """A device's TLS material: the self-signed cert PEM + private key PEM, both
    derived from its Ed25519 device key. ``cert_pem`` is public (safe to pin /
    enroll); ``key_pem`` never leaves the box (same rule as the raw device key)."""

    __slots__ = ("cert_pem", "key_pem", "pubkey_b64", "device_id")

    def __init__(self, cert_pem: bytes, key_pem: bytes, pubkey_b64: str,
                 device_id: str):
        self.cert_pem = cert_pem
        self.key_pem = key_pem
        self.pubkey_b64 = pubkey_b64
        self.device_id = device_id


def _device_serial(pubkey_b64: str) -> int:
    """A stable, positive, ≤159-bit X.509 serial derived from the device key."""
    import hashlib
    return (int.from_bytes(hashlib.sha256(pubkey_b64.encode("ascii")).digest()[:19],
                           "big") >> 1) or 1


def mint_device_cert(ident: "ident_mod.DeviceIdentity") -> CertMaterial:
    """Mint a self-signed X.509 cert whose public key IS the device's Ed25519 key
    and whose CN + DNS SAN are the ``device_id``. The cert is signed by the device
    private key itself (self-issued) — so possessing a valid cert for a device_id
    proves control of that device key, exactly like the channel's ``auth`` sig."""
    sk: Ed25519PrivateKey = ident._sk  # the origin's own key, on its own box
    device_id = ident.device_id
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(sk.public_key())
        # serial derived from the key (not random) + fixed validity + Ed25519's
        # deterministic signature ⇒ the SAME cert bytes on every mint, so the cert
        # a binding Hub trusted at pairing still matches after a daemon restart.
        .serial_number(_device_serial(ident.public_key_b64))
        .not_valid_before(_NOT_BEFORE)
        .not_valid_after(_NOT_AFTER)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(device_id)]),
                       critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        # Ed25519 certs are signed with algorithm=None (the key type fixes it).
        .sign(private_key=sk, algorithm=None)
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = sk.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    return CertMaterial(cert_pem, key_pem, ident.public_key_b64, device_id)


def _materialize_key(key_pem: bytes) -> str:
    """Write a private key to a 0600 temp file ssl can load by path (never
    briefly group/world-readable: fchmod before the bytes land)."""
    fd, path = tempfile.mkstemp(suffix=".key.pem")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key_pem)
    _TEMP_PATHS.append(path)
    return path


def _materialize_cert(cert_pem: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".crt.pem")
    with os.fdopen(fd, "wb") as fh:
        fh.write(cert_pem)
    _TEMP_PATHS.append(path)
    return path


def server_ssl_context(material: CertMaterial, *,
                       client_certs_pem: Optional[list[bytes]] = None,
                       request_client_cert: bool = False
                       ) -> ssl.SSLContext:
    """A TLS *server* context for the hub, presenting its device cert. When
    ``client_certs_pem`` is given, require + verify a client cert against exactly
    those enrolled device certs (mTLS): OpenSSL admits only a presented cert that
    matches one in the pinned bundle. When omitted, TLS is server-authenticated
    only (still encrypted); the device-key binding then rests on the app-layer
    ``auth`` frame plus :func:`peer_pubkey_b64` if a cert was presented.

    ``request_client_cert`` (P2.5, a standalone Hub with ``--require-tls-binding``)
    asks every dialer for a cert (``CERT_OPTIONAL``) and trusts exactly the device
    certs later added with :func:`trust_client_cert` (delivered in the pairing
    claim) — a pre-pairing dial presents none and still connects; an enrolled box
    presents its pinned cert, so the channel can bind the tunnel to its key."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.check_hostname = False
    cpath = _materialize_cert(material.cert_pem)
    kpath = _materialize_key(material.key_pem)
    ctx.load_cert_chain(cpath, kpath)
    if client_certs_pem:
        # self-signed self-issued client certs ARE their own CA; pinning the
        # exact enrolled certs as trusted anchors is the whole trust store.
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=b"".join(client_certs_pem).decode("ascii"))
    elif request_client_cert:
        ctx.verify_mode = ssl.CERT_OPTIONAL
    else:
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def trust_client_cert(ctx: ssl.SSLContext, cert_pem: bytes) -> None:
    """Add one enrolled device's self-signed cert to a live server context's trust
    store (every later handshake sees it). Only meaningful on a context that asks
    for client certs (``CERT_OPTIONAL``/``CERT_REQUIRED``)."""
    ctx.load_verify_locations(cadata=cert_pem.decode("ascii"))


def client_ssl_context(material: Optional[CertMaterial] = None, *,
                       server_cert_pem: Optional[bytes] = None
                       ) -> ssl.SSLContext:
    """A TLS *client* context for the origin dialing OUT. Pins the hub's exact
    cert when ``server_cert_pem`` is given (encrypted + hub-authenticated); with
    no pin it still encrypts but does not authenticate the server (loopback dev
    only — a real remote origin MUST pin). When ``material`` is given the origin
    PRESENTS its device cert for mTLS so the hub can bind the tunnel to the
    device key."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False  # trust is by pinned cert, not by hostname/SAN
    if server_cert_pem is not None:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=server_cert_pem.decode("ascii"))
    else:
        ctx.verify_mode = ssl.CERT_NONE
    if material is not None:
        cpath = _materialize_cert(material.cert_pem)
        kpath = _materialize_key(material.key_pem)
        ctx.load_cert_chain(cpath, kpath)
    return ctx


def pubkey_b64_from_cert_pem(cert_pem: bytes) -> Optional[str]:
    """The Ed25519 public key (base64, the device pubkey format) carried by a
    cert PEM, or None if it isn't an Ed25519 cert. Lets the enrollment/binding
    layer cross-check a presented cert against the enrolled device pubkey."""
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except ValueError:
        return None
    pk = cert.public_key()
    if not isinstance(pk, Ed25519PublicKey):
        return None
    return ident_mod.b64e(pk.public_bytes_raw())


def cert_fingerprint(cert_pem: bytes) -> str:
    """A human-verifiable fingerprint of a cert (SHA-256 of its DER, hex,
    colon-grouped) — the value shown alongside a pairing code so a box can confirm
    the Hub cert it is handed at pairing is the one the user expects, closing the
    trust-on-first-use MITM window (§5.1(4)(b)/§5.4). Same shape as
    :func:`identity.fingerprint` so both read the same in a UI."""
    import hashlib
    cert = x509.load_pem_x509_certificate(cert_pem)
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def peer_pubkey_b64(ssl_object) -> Optional[str]:
    """Extract the peer's device pubkey (base64) from the TLS peer certificate of
    a live ``ssl.SSLObject``/``SSLSocket``, or None when no cert was presented.
    The channel calls this post-handshake to BIND the encrypted tunnel to the
    device key the peer proves in the app-layer ``auth`` frame."""
    if ssl_object is None:
        return None
    try:
        der = ssl_object.getpeercert(binary_form=True)
    except (ValueError, AttributeError):
        return None
    if not der:
        return None
    try:
        cert = x509.load_der_x509_certificate(der)
    except ValueError:
        return None
    pk = cert.public_key()
    if not isinstance(pk, Ed25519PublicKey):
        return None
    return ident_mod.b64e(pk.public_bytes_raw())


def is_self_signed_ed25519(cert_pem: bytes) -> bool:
    """True iff ``cert_pem`` is a well-formed Ed25519 cert whose own key signs it
    (self-signed + self-issued). A tampered cert, or one whose signature was not
    made by the key it carries, is rejected — so a forged 'device_id but not the
    device key' cert never passes."""
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except ValueError:
        return False
    pk = cert.public_key()
    if not isinstance(pk, Ed25519PublicKey):
        return False
    try:
        pk.verify(cert.signature, cert.tbs_certificate_bytes)
        return True
    except Exception:  # noqa: BLE001 — any verify failure = not self-signed
        return False


class PairingTrustError(ValueError):
    """A pairing-time Hub-cert trust refusal. ``code`` is a stable, typed reason
    (``hub_fingerprint_required`` / ``hub_fingerprint_mismatch`` /
    ``hub_cert_invalid`` / ``hub_cert_missing``) the caller surfaces verbatim."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _norm_fp(fp: str) -> str:
    return fp.strip().lower()


def require_pairing_fingerprint(*, loopback: bool,
                                expected_fingerprint: Optional[str]) -> None:
    """The pre-auth pairing dial is CERT_NONE, so for any NON-loopback Hub the
    out-of-band fingerprint is the ONLY thing authenticating the cert we are about
    to pin — without it an on-path MITM pins its own cert. Refuse BEFORE dialing
    (no pairing claim crosses the wire). Fingerprint-less TOFU stays loopback/dev
    only."""
    if loopback:
        return
    if not (expected_fingerprint and expected_fingerprint.strip()):
        raise PairingTrustError(
            "hub_fingerprint_required",
            "pairing with a non-loopback Hub requires --hub-fingerprint (the value "
            "shown next to the pairing code) — refusing trust-on-first-use")


def verify_pairing_hub_cert(cert_pem: Optional[bytes], *, loopback: bool,
                            expected_fingerprint: Optional[str]) -> Optional[str]:
    """Gate a Hub cert handed back at pairing BEFORE it is pinned. Returns its
    fingerprint (or None when no cert was delivered on loopback). Raises
    :class:`PairingTrustError` when:

      * a non-loopback pairing has no expected fingerprint, or delivered no cert
        (nothing to authenticate → nothing may be pinned);
      * the cert is not a well-formed, self-signed Ed25519 cert (the only shape a
        Hub mints — anything else is never written to ``hub-cert.pem``);
      * the fingerprint does not match the expected one (possible MITM)."""
    require_pairing_fingerprint(loopback=loopback,
                                expected_fingerprint=expected_fingerprint)
    if not cert_pem:
        if loopback:
            return None
        raise PairingTrustError(
            "hub_cert_missing",
            "the Hub did not deliver its cert at pairing — nothing to pin, refusing")
    if not is_self_signed_ed25519(cert_pem):
        raise PairingTrustError(
            "hub_cert_invalid",
            "the Hub cert delivered at pairing is not a self-signed Ed25519 cert — "
            "refusing to pin it")
    fp = cert_fingerprint(cert_pem)
    if expected_fingerprint and _norm_fp(fp) != _norm_fp(expected_fingerprint):
        raise PairingTrustError(
            "hub_fingerprint_mismatch",
            f"Hub cert fingerprint {fp} does not match the expected "
            f"{expected_fingerprint.strip()} — possible MITM, refusing")
    return fp
