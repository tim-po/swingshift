"""Isolated fixture: real portal routes, enrollment store and TLS OriginHub.

Control files are private to the hub container; no test routes added to portal.
"""
import asyncio
import json
import os
from pathlib import Path
import threading
import time

import uvicorn
from starlette.testclient import TestClient
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from control_plane import auth, onboard, download_token
from control_plane.config import Config
from control_plane.store import Store
from mcp_loops import hub_serve, origin_onboard

state = Path('/state')
state.mkdir(exist_ok=True)
portal = 'http://127.0.0.1:18931'
hub_root = str(state / 'hub')
hub = hub_serve.build_hub(state_dir=hub_root, tls=True)
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
port = asyncio.run_coroutine_threadsafe(hub.start('127.0.0.1', 18932), loop).result(15)
os.environ[origin_onboard.HUB_STATE_ENV] = hub_root
os.environ[origin_onboard.HUB_PUBLIC_URL_ENV] = f'wss://127.0.0.1:{port}'
os.environ['LOOPS_DATA_DIR'] = str(state / 'data' / '_loops')
store = Store(state / 'portal.db')
cfg = Config(portal_url=portal, google_client_id='e2e', session_secret='isolated-e2e',
             release_store='/releases')
store.mint_invite('e2e@example.com', 'owner')
account = store.create_account_if_invited('e2e-google', 'e2e@example.com', 'E2E')
directory_hub = store.register_hub(account.id, 'SHA256:e2e', 'https://hub.e2e.invalid', 'e2e', 0)
app = auth.build_portal_app(store, cfg, verifier=object(),
                          join_minter=onboard.origin_onboard_minter)


async def installer_fault(request, call_next):
    fault = state / 'installer-fault'
    if request.url.path.endswith('/install.sh') and fault.exists():
        mode = fault.read_text().strip()
        if mode in ('401', '404'):
            return Response('injected download failure', status_code=int(mode))
        if mode == 'truncated':
            # A successful HTTP transfer can still contain an incomplete script.
            # Cut at a complete shell statement, so sh -n alone cannot catch it.
            installer = Path('/releases/v1.0.0/install.sh').read_bytes()
            return Response(installer.split(b'\n', 1)[0] + b'\n')
    return await call_next(request)

app.add_middleware(BaseHTTPMiddleware, dispatch=installer_fault)
client = TestClient(app, base_url=portal)
client.cookies.set(auth.SESSION_COOKIE, auth.issue_session(store, account, cfg.session_secret))
r = client.post('/api/onboard', json={'label': 'docker-origin', 'hub_id': directory_hub.id},
                headers={'origin': portal})
assert r.status_code == 201, r.text
(state / 'connect.sh').write_text(r.json()['command'] + '\n')
(state / 'connect.sh').chmod(0o600)
# Private test credential for the real CLI stdin path; never put it on argv.
_, update_token, _ = download_token.issue(store, account.id)
(state / 'update-token').write_text(update_token + '\n')
(state / 'update-token').chmod(0o600)


def snapshots():
    while True:
        async def snapshot():
            return {'devices': hub.store.list_devices(), 'origins': hub.registry.list()}
        value = asyncio.run_coroutine_threadsafe(snapshot(), loop).result(10)
        temporary = state / 'status.tmp'
        temporary.write_text(json.dumps(value))
        temporary.replace(state / 'status.json')
        request = state / 'dispatch-request'
        if request.exists():
            request.unlink()
            async def dispatch():
                origins = hub.registry.list()
                assert len(origins) == 1, origins
                return await hub.router.run(origins[0]['deviceId'],
                                            ['echo', 'docker-e2e-dispatch'],
                                            owner=account.email, timeout=10)
            try:
                result = asyncio.run_coroutine_threadsafe(dispatch(), loop).result(30)
            except Exception as exc:
                result = {'error': str(exc)}
            temporary = state / 'dispatch-result.tmp'
            temporary.write_text(json.dumps(result))
            temporary.replace(state / 'dispatch-result.json')
        time.sleep(.25)

threading.Thread(target=snapshots, daemon=True).start()
uvicorn.run(app, host='127.0.0.1', port=18931, log_level='warning', access_log=False)
