"""Owner-only allowlist administration. Authentication is supplied by the app."""
import inspect
from dataclasses import asdict
from starlette.responses import JSONResponse
from starlette.routing import Route
from .invites import is_owner
from .store import normalize_email

async def _owner(request):
    account = request.app.state.authenticate(request)
    if inspect.isawaitable(account):
        account = await account
    if not account:
        return None, JSONResponse({'error': 'authentication required'}, status_code=401)
    # Re-read persisted status; a previously authenticated account can be disabled.
    account = request.app.state.store.get_account(account.id)
    if not is_owner(account, request.app.state.cfg):
        return None, JSONResponse({'error': 'owner required'}, status_code=403)
    return account, None

def _cross_origin(request):
    # Cookie-authenticated mutations require the configured portal Origin.
    if request.headers.get('origin', '').rstrip('/') != request.app.state.cfg.portal_url.rstrip('/'):
        return JSONResponse({'error': 'invalid origin'}, status_code=403)
    return None

async def _email(request):
    try:
        body = await request.json()
    except ValueError:
        # Never echo the JSON parser's message back to the client.
        raise ValueError('invalid JSON body') from None
    if not isinstance(body, dict) or not isinstance(body.get('email'), str):
        raise ValueError('email required')
    return body['email']

async def invites(request):
    account, error = await _owner(request)
    if error is not None:
        return error
    store = request.app.state.store
    if request.method == 'GET':
        return JSONResponse({'invites': [asdict(i) for i in store.list_invites()]})
    if (error := _cross_origin(request)) is not None:
        return error
    try:
        invite = store.mint_invite(await _email(request), account.id)
    except ValueError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    return JSONResponse(asdict(invite), status_code=201)

async def revoke_invite(request):
    _, error = await _owner(request)
    if error is None:
        error = _cross_origin(request)
    if error is not None:
        return error
    try:
        email = normalize_email(await _email(request))
    except ValueError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    # Only an open invite can be revoked; a redeemed one belongs to an account.
    if not request.app.state.store.revoke_invite(email):
        return JSONResponse({'error': 'no open invite'}, status_code=404)
    return JSONResponse({'email': email, 'status': 'revoked'})

async def accounts(request):
    _, error = await _owner(request)
    if error is not None:
        return error
    return JSONResponse({'accounts': [asdict(a) for a in request.app.state.store.list_accounts()]})

routes = [Route('/api/admin/invites', invites, methods=['GET', 'POST']),
          Route('/api/admin/invites/revoke', revoke_invite, methods=['POST']),
          Route('/api/admin/accounts', accounts, methods=['GET'])]
