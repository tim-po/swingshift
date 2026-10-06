"""Control-plane configuration; importing this module has no side effects."""
from dataclasses import dataclass, field
import os

@dataclass
class Config:
    portal_url: str = "https://portal.example.invalid"
    db_path: str = "control-plane.sqlite3"
    owner_emails: set[str] = field(default_factory=set)
    google_client_id: str = ""
    google_client_secret: str = ""
    session_secret: str = ""
    hub_ttl: float = 90.0
    release_store: str = ""

    @classmethod
    def from_env(cls):
        return cls(
            portal_url=os.environ.get("LOOPYARD_PORTAL_URL", "https://portal.example.invalid").rstrip("/"),
            release_store=os.environ.get("LOOPYARD_RELEASE_STORE", ""),
            db_path=os.environ.get("CP_DB_PATH", "control-plane.sqlite3"),
            owner_emails={e.strip().lower() for e in os.environ.get("CP_OWNER_EMAILS", "").split(",") if e.strip()},
            google_client_id=os.environ.get("CP_GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.environ.get("CP_GOOGLE_CLIENT_SECRET", ""),
            session_secret=os.environ.get("CP_SESSION_SECRET", ""),
        )
