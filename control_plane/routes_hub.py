"""Account-scoped directory routes; machine credentials are resolved by auth."""
import inspect
import time
from dataclasses import asdict
from starlette.responses import JSONResponse
from starlette.routing import Route
from .directory import DirectoryConflict


async def _account(request, mutation=False):
    # Explicit machine-auth seam: never interpret caller-supplied account IDs.
    machine = 'authorization' in request.headers
    resolver = request.app.state.authenticate_hub if machine else request.app.state.authenticate
    account = resolver(request)
    if inspect.isawaitable(account):
        account = await account
    account = request.app.state.store.get_account(account.id) if account else None
    if not account or account.status != 'active':
        return None, JSONResponse({'error': 'authentication required'}, status_code=401)
    if mutation and not machine and request.headers.get('origin', '').rstrip('/') != request.app.state.cfg.portal_url.rstrip('/'):
        return None, JSONResponse({'error': 'invalid origin'}, status_code=403)
    return account, None


async def hubs(request):
    account, error = await _account(request)
    if error is not None:
        return error
    store = request.app.state.store
    store.mark_stale(time.time(), request.app.state.cfg.hub_ttl)
    return JSONResponse({'hubs': [asdict(h) for h in store.hubs_for(account.id)]})


async def mutate(request):
    account, error = await _account(request, mutation=True)
    if error is not None:
        return error
    store = request.app.state.store
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError('JSON object required')
        if request.url.path == '/api/hubs/register':
            hub = store.register_hub(account.id, body['fingerprint'], body['public_url'], body['holder_origin_id'], body['state_version'])
        elif request.url.path.endswith('/heartbeat'):
            hub = store.heartbeat(request.path_params['hub_id'], body['state_version'], account_id=account.id)
        else:
            hub = store.move_hub(request.path_params['hub_id'], body['holder_origin_id'], body['public_url'], account_id=account.id, expected_version=body['state_version'])
    except DirectoryConflict as exc:
        return JSONResponse({'error': str(exc)}, status_code=409)
    except (ValueError, KeyError, TypeError):
        return JSONResponse({'error': 'invalid directory payload'}, status_code=400)
    return JSONResponse(asdict(hub))


routes = [Route('/api/hubs', hubs, methods=['GET']),
          Route('/api/hubs/register', mutate, methods=['POST']),
          Route('/api/hubs/{hub_id}/heartbeat', mutate, methods=['POST']),
          Route('/api/hubs/{hub_id}/move', mutate, methods=['POST'])]
