"""Invite-gated local release store. No redirect to an anonymous origin."""
import inspect
import re
import time
from pathlib import Path
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
from . import device_token, download_token
from .device_token import bearer

VERSION = re.compile(r'v?\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?')
ASSET = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
# The one asset a device token (lyd_) may read: the desktop app's update check
# carries the hub's device credential, not a portal session. Assets stay gated.
METADATA = 'release.json'
HEADERS = {'Cache-Control': 'private, no-store', 'Vary': 'Authorization, Cookie',
           'X-Content-Type-Options': 'nosniff'}


def release_path(root, version, asset):
    root = Path(root).resolve(strict=True)
    if version == 'latest':
        pointer = root / 'latest'
        if pointer.is_symlink():
            raise ValueError('symlink pointer')
        version = pointer.read_text().strip()
    if not VERSION.fullmatch(version) or not ASSET.fullmatch(asset):
        raise ValueError('invalid asset path')
    version = version if version.startswith('v') else 'v' + version
    directory = root / version
    path = directory / asset
    # Reject symlinks entirely, including a version directory redirect.
    if directory.is_symlink() or path.is_symlink():
        raise ValueError('symlink asset')
    resolved = path.resolve(strict=True)
    if resolved.parent != directory or not resolved.is_file():
        raise ValueError('not a release asset')
    return resolved


def device_reader(store, token, account_id=None):
    """Active Account for a live lyd_ device token, else None (read-only use)."""
    hit = device_token.verify(store, token)
    if hit is None:
        return None
    row, account = hit
    if row.hub_id is None:  # an onboarding enrollment token dies with its enrollment
        ob = store.enrollment_by_token(row.id)
        if ob is not None and (ob.claimed_at is not None or not ob.created_at <= time.time() < ob.expires_at):
            return None
    if account_id is not None and account_id != account.id:
        return None
    return account


async def download(request):
    app = request.app
    root = app.state.cfg.release_store
    if not root:
        return JSONResponse({'error': 'not found'}, 404, headers=HEADERS)
    account = app.state.authenticate(request)
    if inspect.isawaitable(account):
        account = await account
    token = bearer(request)
    if token.startswith(device_token.PREFIX):
        account = (device_reader(app.state.store, token, account.id if account else None)
                   if request.path_params['asset'] == METADATA else None)
    elif token:
        account = download_token.verify(app.state.store, token,
                                       account_id=account.id if account else None)
    if not account or account.status != 'active':
        return JSONResponse({'error': 'authentication required'}, 401, headers=HEADERS)
    try:
        path = release_path(root, request.path_params['version'], request.path_params['asset'])
    except (OSError, ValueError):
        return JSONResponse({'error': 'not found'}, 404, headers=HEADERS)
    return FileResponse(path, headers=HEADERS, filename=path.name)


routes = [Route('/releases/{version}/{asset:path}', download, methods=['GET', 'HEAD'])]
