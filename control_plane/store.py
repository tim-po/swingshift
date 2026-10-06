"""SQLite persistence. Invite consumption and account creation share a write lock."""
from contextlib import contextmanager
from dataclasses import dataclass, asdict
import re
import math
import sqlite3
import threading
import time
from uuid import uuid4

class Record:
    def __getitem__(self, name):
        return getattr(self, name)
    def get(self, name, default=None):
        return getattr(self, name, default)
    def to_dict(self):
        return asdict(self)

@dataclass(frozen=True)
class Account(Record):
    id: str
    google_sub: str
    email: str
    display_name: str
    created_at: float
    status: str

@dataclass(frozen=True)
class Invite(Record):
    email: str
    issued_by: str
    issued_at: float
    redeemed_by: str | None
    redeemed_at: float | None
    status: str

@dataclass(frozen=True)
class DeviceToken(Record):
    id: str
    account_id: str
    hub_id: str | None
    device_label: str
    token_hash: str
    revoked: int


@dataclass(frozen=True)
class Enrollment(Record):
    id: str
    account_id: str
    token_id: str
    label: str
    created_at: float
    expires_at: float
    hub_id: str | None
    claimed_at: float | None
    device_token_id: str | None


def normalize_email(email):
    email = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("invalid email")
    return email

class Store:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript('''
        CREATE TABLE IF NOT EXISTS account (
            id TEXT PRIMARY KEY, google_sub TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL, display_name TEXT NOT NULL, created_at REAL NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','disabled')));
        CREATE TABLE IF NOT EXISTS invite (
            email TEXT PRIMARY KEY, issued_by TEXT NOT NULL, issued_at REAL NOT NULL,
            redeemed_by TEXT REFERENCES account(id), redeemed_at REAL,
            status TEXT NOT NULL CHECK(status IN ('open','redeemed','revoked')));
        CREATE TABLE IF NOT EXISTS session_generation (
            account_id TEXT PRIMARY KEY REFERENCES account(id), generation INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS revoked_session (
            sid TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES account(id));
        CREATE TABLE IF NOT EXISTS hub (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES account(id),
            fingerprint TEXT UNIQUE NOT NULL, public_url TEXT NOT NULL,
            holder_origin_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('online','offline')),
            last_seen REAL NOT NULL, state_version INTEGER NOT NULL CHECK(state_version >= 0));
        CREATE TABLE IF NOT EXISTS device_token (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES account(id),
            hub_id TEXT REFERENCES hub(id), device_label TEXT NOT NULL,
            token_hash TEXT UNIQUE NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS download_token (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES account(id),
            token_hash TEXT UNIQUE NOT NULL, created_at REAL NOT NULL,
            expires_at REAL NOT NULL, uses INTEGER NOT NULL DEFAULT 0,
            revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS enrollment (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES account(id),
            token_id TEXT UNIQUE NOT NULL REFERENCES device_token(id), label TEXT NOT NULL,
            created_at REAL NOT NULL, expires_at REAL NOT NULL CHECK(expires_at > created_at),
            hub_id TEXT REFERENCES hub(id), claimed_at REAL,
            device_token_id TEXT REFERENCES device_token(id));
        ''')

    def close(self):
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def _one(self, sql, args, kind):
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
            return kind(**dict(row)) if row else None

    def get_account(self, id):
        return self._one("SELECT * FROM account WHERE id=?", (id,), Account)

    def account_by_google_sub(self, sub):
        return self._one("SELECT * FROM account WHERE google_sub=?", (sub,), Account)

    def list_accounts(self):
        with self._lock:
            return [Account(**dict(r)) for r in self._db.execute("SELECT * FROM account ORDER BY created_at,id")]

    def list_invites(self):
        with self._lock:
            return [Invite(**dict(r)) for r in self._db.execute("SELECT * FROM invite ORDER BY email")]

    def mint_invite(self, email, issued_by):
        email = normalize_email(email)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM invite WHERE email=?", (email,)).fetchone()
            if row and row['status'] == 'redeemed':
                raise ValueError("invite already redeemed")
            if not row or row['status'] == 'revoked':
                db.execute("INSERT INTO invite VALUES (?,?,?,NULL,NULL,'open') ON CONFLICT(email) DO UPDATE SET issued_by=excluded.issued_by,issued_at=excluded.issued_at,status='open',redeemed_by=NULL,redeemed_at=NULL", (email, issued_by, time.time()))
            return Invite(**dict(db.execute("SELECT * FROM invite WHERE email=?", (email,)).fetchone()))

    def revoke_invite(self, email):
        with self._transaction() as db:
            return bool(db.execute("UPDATE invite SET status='revoked' WHERE email=? AND status='open'", (normalize_email(email),)).rowcount)

    def create_account_if_invited(self, google_sub, email, display_name):
        if not google_sub or not isinstance(google_sub, str):
            raise ValueError("google subject required")
        email = normalize_email(email)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM account WHERE google_sub=?", (google_sub,)).fetchone()
            if row:
                return Account(**dict(row))
            invite = db.execute("SELECT * FROM invite WHERE email=? AND status='open'", (email,)).fetchone()
            if not invite:
                return None
            account = Account(uuid4().hex, google_sub, email, display_name, time.time(), 'active')
            db.execute("INSERT INTO account VALUES (?,?,?,?,?,?)", tuple(asdict(account).values()))
            db.execute("UPDATE invite SET status='redeemed',redeemed_by=?,redeemed_at=? WHERE email=?", (account.id, time.time(), email))
            return account

    def revoke_session(self, account_id, sid):
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO revoked_session VALUES (?,?)", (sid, account_id))

    def session_revoked(self, sid):
        with self._lock:
            return self._db.execute("SELECT 1 FROM revoked_session WHERE sid=?", (sid,)).fetchone() is not None

    def generation(self, account_id):
        with self._lock:
            row = self._db.execute("SELECT generation FROM session_generation WHERE account_id=?", (account_id,)).fetchone()
            return row[0] if row else 0

    def bump_generation(self, account_id):
        with self._transaction() as db:
            db.execute("INSERT INTO session_generation VALUES (?,1) ON CONFLICT(account_id) DO UPDATE SET generation=generation+1", (account_id,))
            return db.execute("SELECT generation FROM session_generation WHERE account_id=?", (account_id,)).fetchone()[0]

    def put_device_token(self, account_id, hub_id, device_label, token_hash):
        if not re.fullmatch(r'[0-9a-f]{64}', token_hash):
            raise ValueError("expected sha256 hex digest")
        with self._transaction() as db:
            if not db.execute("SELECT 1 FROM account WHERE id=? AND status='active'", (account_id,)).fetchone():
                raise ValueError("active account required")
            if hub_id is not None and not db.execute("SELECT 1 FROM hub WHERE id=? AND account_id=?", (hub_id, account_id)).fetchone():
                raise ValueError("hub does not belong to account")
            id = uuid4().hex
            db.execute("INSERT INTO device_token VALUES (?,?,?,?,?,0)", (id, account_id, hub_id, device_label, token_hash))
            return id

    def device_token_by_hash(self, h):
        return self._one("SELECT * FROM device_token WHERE token_hash=?", (h,), DeviceToken)

    def get_device_token(self, id):
        return self._one("SELECT * FROM device_token WHERE id=?", (id,), DeviceToken)

    def list_device_tokens(self, account_id):
        """Include revoked tokens so callers can display/manage device history."""
        with self._lock:
            return [DeviceToken(**dict(r)) for r in self._db.execute(
                "SELECT * FROM device_token WHERE account_id=? ORDER BY id", (account_id,))]

    def put_enrollment(self, id, account_id, token_id, label, created_at, expires_at):
        """Persist an enrollment bound to an active, unbound device credential."""
        if not isinstance(id, str) or not id or not isinstance(label, str) or not label.strip():
            raise ValueError("enrollment id and label required")
        if not math.isfinite(created_at) or not math.isfinite(expires_at) or expires_at <= created_at:
            raise ValueError("finite timestamps and positive enrollment lifetime required")
        with self._transaction() as db:
            token = db.execute("SELECT d.* FROM device_token d JOIN account a ON a.id=d.account_id "
                               "WHERE d.id=? AND d.account_id=? AND a.status='active' "
                               "AND d.revoked=0 AND d.hub_id IS NULL", (token_id, account_id)).fetchone()
            if token is None:
                raise ValueError("active unbound enrollment token required")
            db.execute("INSERT INTO enrollment VALUES (?,?,?,?,?,?,NULL,NULL,NULL)",
                       (id, account_id, token_id, label, created_at, expires_at))
            return Enrollment(id, account_id, token_id, label, created_at, expires_at, None, None, None)

    def get_enrollment(self, id):
        return self._one("SELECT * FROM enrollment WHERE id=?", (id,), Enrollment)

    def enrollment_by_token(self, token_id):
        return self._one("SELECT * FROM enrollment WHERE token_id=?", (token_id,), Enrollment)

    def claim_enrollment(self, id, account_id, hub_id, token_hash, *, now=None):
        """Consume enrollment and issue its hub-bound replacement in one transaction.

        Caller authenticates the enrollment bearer and supplies its account.
        Returns Enrollment with device_token_id, or None on invalid/expired/replayed
        claims. Only the SHA256 of the replacement credential is passed here.
        """
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("finite timestamp required")
        if not isinstance(token_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', token_hash):
            raise ValueError("expected sha256 hex digest")
        with self._transaction() as db:
            row = db.execute("SELECT e.* FROM enrollment e JOIN account a ON a.id=e.account_id "
                             "JOIN device_token d ON d.id=e.token_id "
                             "WHERE e.id=? AND e.account_id=? AND e.claimed_at IS NULL "
                             "AND e.created_at<=? AND e.expires_at>? AND a.status='active' "
                             "AND d.revoked=0 AND d.hub_id IS NULL",
                             (id, account_id, now, now)).fetchone()
            if row is None or not db.execute("SELECT 1 FROM hub WHERE id=? AND account_id=?",
                                            (hub_id, account_id)).fetchone():
                return None
            replacement_id = uuid4().hex
            db.execute("INSERT INTO device_token VALUES (?,?,?,?,?,0)",
                       (replacement_id, account_id, hub_id, row['label'], token_hash))
            db.execute("UPDATE device_token SET revoked=1 WHERE id=?", (row['token_id'],))
            db.execute("UPDATE enrollment SET hub_id=?,claimed_at=?,device_token_id=? WHERE id=?",
                       (hub_id, now, replacement_id, id))
            return Enrollment(**dict(db.execute("SELECT * FROM enrollment WHERE id=?", (id,)).fetchone()))

    def prune_device_tokens(self, account_id, hub_id, device_label, keep):
        """Revoke all but the newest ``keep`` live tokens for (account, hub, label).

        Bounds tokens that accumulate per handoff (the portal mints a fresh
        'browser' token each time). A hub's own claimed credential is never
        pruned, even if its enrollment label collides. Returns the count revoked.
        """
        if not isinstance(keep, int) or keep < 1:
            raise ValueError("keep must be a positive int")
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id FROM device_token WHERE account_id=? AND hub_id=? AND device_label=? "
                "AND revoked=0 AND id NOT IN (SELECT device_token_id FROM enrollment "
                "WHERE device_token_id IS NOT NULL) ORDER BY rowid DESC",
                (account_id, hub_id, device_label)).fetchall()
            stale = [r[0] for r in rows[keep:]]
            db.executemany("UPDATE device_token SET revoked=1 WHERE id=?", [(i,) for i in stale])
            return len(stale)

    def revoke_device_token(self, id):
        with self._transaction() as db:
            return bool(db.execute("UPDATE device_token SET revoked=1 WHERE id=? AND revoked=0", (id,)).rowcount)

    def register_hub(self, account_id, fingerprint, public_url, holder_origin_id, state_version):
        from .directory import Hub, DirectoryConflict, validate_location, validate_version
        validate_location(public_url, holder_origin_id)
        validate_version(state_version)
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError('fingerprint required')
        with self._transaction() as db:
            if not db.execute("SELECT 1 FROM account WHERE id=? AND status='active'", (account_id,)).fetchone():
                raise DirectoryConflict('active account required')
            row = db.execute('SELECT * FROM hub WHERE fingerprint=?', (fingerprint,)).fetchone()
            if row:
                if row['account_id'] != account_id:
                    raise DirectoryConflict('fingerprint already registered')
                if state_version < row['state_version']:
                    raise DirectoryConflict('stale state_version')
                if state_version == row['state_version'] and (public_url != row['public_url'] or holder_origin_id != row['holder_origin_id']):
                    raise DirectoryConflict('location changes require a newer state_version')
                id = row['id']
                db.execute("UPDATE hub SET public_url=?,holder_origin_id=?,state_version=?,last_seen=?,status='online' WHERE id=?",
                           (public_url, holder_origin_id, state_version, time.time(), id))
            else:
                id = uuid4().hex
                db.execute("INSERT INTO hub VALUES (?,?,?,?,?,'online',?,?)",
                           (id, account_id, fingerprint, public_url, holder_origin_id, time.time(), state_version))
            return Hub(**dict(db.execute('SELECT * FROM hub WHERE id=?', (id,)).fetchone()))

    def get_hub(self, id):
        from .directory import Hub
        return self._one('SELECT * FROM hub WHERE id=?', (id,), Hub)

    def hubs_for(self, account_id):
        from .directory import Hub
        with self._lock:
            return [Hub(**dict(r)) for r in self._db.execute('SELECT * FROM hub WHERE account_id=? ORDER BY id', (account_id,))]

    def heartbeat(self, hub_id, state_version, *, account_id=None):
        """Refresh only the current holder generation; optional account guard for routes."""
        from .directory import Hub, DirectoryConflict, validate_version
        validate_version(state_version)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM hub WHERE id=?', (hub_id,)).fetchone()
            if row is None:
                row = db.execute('SELECT * FROM hub WHERE fingerprint=?', (hub_id,)).fetchone()
            if row is None or (account_id is not None and row['account_id'] != account_id):
                raise DirectoryConflict('hub not found')
            if not db.execute("SELECT 1 FROM account WHERE id=? AND status='active'", (row['account_id'],)).fetchone():
                raise DirectoryConflict('active account required')
            if row['state_version'] != state_version:
                raise DirectoryConflict('heartbeat requires current state_version')
            db.execute("UPDATE hub SET status='online',last_seen=? WHERE id=?", (time.time(), row['id']))
            return Hub(**dict(db.execute('SELECT * FROM hub WHERE id=?', (row['id'],)).fetchone()))

    def mark_stale(self, now, ttl):
        import math
        if not math.isfinite(now) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError('finite timestamp and positive ttl required')
        with self._transaction() as db:
            return db.execute("UPDATE hub SET status='offline' WHERE status='online' AND last_seen<=?", (now - ttl,)).rowcount

    def move_hub(self, hub_id, new_holder_origin_id, new_public_url, *, account_id=None, expected_version=None):
        """Change address and holder in one transaction, preserving id and fingerprint."""
        from .directory import Hub, DirectoryConflict, validate_location, validate_version
        validate_location(new_public_url, new_holder_origin_id)
        if expected_version is not None:
            validate_version(expected_version)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM hub WHERE id=?', (hub_id,)).fetchone()
            if row is None or (account_id is not None and row['account_id'] != account_id):
                raise DirectoryConflict('hub not found')
            if not db.execute("SELECT 1 FROM account WHERE id=? AND status='active'", (row['account_id'],)).fetchone():
                raise DirectoryConflict('active account required')
            if expected_version is not None and expected_version != row['state_version']:
                raise DirectoryConflict('stale state_version')
            validate_version(row['state_version'] + 1)
            # A requested move is offline until its new holder phones home.
            db.execute("UPDATE hub SET holder_origin_id=?,public_url=?,state_version=state_version+1,status='offline' WHERE id=?",
                       (new_holder_origin_id, new_public_url, hub_id))
            return Hub(**dict(db.execute('SELECT * FROM hub WHERE id=?', (hub_id,)).fetchone()))
