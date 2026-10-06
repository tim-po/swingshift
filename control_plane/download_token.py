"""Account-bound, hashed, bounded-use release credentials, separate from devices."""
import math
import secrets
import time
from .device_token import token_hash

PREFIX = 'lyr_'
DL_TTL = 15 * 60
MAX_USES = 64


def issue(store, account_id, *, ttl=DL_TTL, now=None):
    now = time.time() if now is None else now
    if not math.isfinite(ttl) or not 0 < ttl <= DL_TTL or not math.isfinite(now):
        raise ValueError('invalid download token lifetime')
    token_id, token = secrets.token_urlsafe(16), PREFIX + secrets.token_urlsafe(32)
    with store._transaction() as db:
        account = store.get_account(account_id)
        if account is None or account.status != 'active':
            raise ValueError('inactive account')
        db.execute('INSERT INTO download_token VALUES (?,?,?,?,?,?,0)',
                   (token_id, account_id, token_hash(token), now, now + ttl, 0))
    return token_id, token, now + ttl


def verify(store, token, *, now=None, account_id=None):
    """Consume one request atomically; optional account binding must match."""
    if not isinstance(token, str) or not token.startswith(PREFIX) or len(token) > 200:
        return None
    now = time.time() if now is None else now
    with store._transaction() as db:
        row = db.execute('SELECT * FROM download_token WHERE token_hash=?',
                         (token_hash(token),)).fetchone()
        if (row is None or row['revoked'] or row['uses'] >= MAX_USES
                or not row['created_at'] <= now < row['expires_at']
                or (account_id is not None and account_id != row['account_id'])):
            return None
        account = store.get_account(row['account_id'])
        if account is None or account.status != 'active':
            return None
        db.execute('UPDATE download_token SET uses=uses+1 WHERE id=?', (row['id'],))
        return account


def revoke(store, token_id):
    with store._transaction() as db:
        db.execute('UPDATE download_token SET revoked=1 WHERE id=?', (token_id,))
