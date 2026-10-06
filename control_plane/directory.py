"""Validation and records for the portable hub directory."""
from dataclasses import dataclass
from urllib.parse import urlsplit
from .store import Record


class DirectoryConflict(ValueError):
    """A stale writer or another account attempted to change a hub."""


@dataclass(frozen=True)
class Hub(Record):
    id: str
    account_id: str
    fingerprint: str
    public_url: str
    holder_origin_id: str
    status: str
    last_seen: float
    state_version: int


def validate_location(public_url, holder_origin_id):
    if not isinstance(holder_origin_id, str) or not holder_origin_id.strip():
        raise ValueError('holder_origin_id required')
    if not isinstance(public_url, str) or any(c.isspace() or ord(c) < 32 for c in public_url):
        raise ValueError('public_url must be an HTTPS URL')
    try:
        url = urlsplit(public_url)
        _ = url.port  # Reject malformed port numbers.
    except ValueError:
        raise ValueError('invalid public_url') from None
    if (url.scheme != 'https' or not url.hostname or url.username is not None
            or url.password is not None or url.fragment or url.query or '\\' in public_url):
        raise ValueError('public_url must be an HTTPS URL without credentials, query or fragment')


def validate_version(version):
    if type(version) is not int or not 0 <= version <= 9223372036854775807:
        raise ValueError('state_version must be a nonnegative SQLite integer')
