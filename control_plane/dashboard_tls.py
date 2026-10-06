"""Browser TLS for the local dashboard, separate from the pinned Hub identity."""
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID


def ensure(state_dir, hostname):
    """Keep a matching, valid P-256 certificate; migrate old Ed25519 web certs.

    Chromium cannot negotiate the Hub's Ed25519 identity certificate. The Hub
    still uses that identity for pairing; browser HTTPS gets its own stable key.
    This is self-signed: local users must trust it, or front it with trusted TLS.
    """
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = state / 'dashboard-cert.pem', state / 'dashboard-key.pem'
    try:
        host = x509.IPAddress(ipaddress.ip_address(hostname))
    except ValueError:
        host = x509.DNSName(hostname.encode('idna').decode('ascii'))
    names = [host, x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]
    names = list(dict.fromkeys(names))
    now = datetime.now(timezone.utc)
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        valid = (isinstance(key, ec.EllipticCurvePrivateKey)
                 and isinstance(key.curve, ec.SECP256R1)
                 and key.public_key().public_numbers() == cert.public_key().public_numbers()
                 and cert.not_valid_before_utc <= now < cert.not_valid_after_utc - timedelta(days=1)
                 and host in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
        if valid:
            key_path.chmod(0o600)
            cert_path.chmod(0o600)
            return cert_path, key_path
    except (OSError, ValueError, TypeError, AttributeError, x509.ExtensionNotFound):
        pass
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Swingshift local dashboard')])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    for path, data in ((key_path, key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                       (cert_path, cert.public_bytes(serialization.Encoding.PEM))):
        fd, temporary = tempfile.mkstemp(prefix='.dashboard-tls-', dir=state)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return cert_path, key_path
