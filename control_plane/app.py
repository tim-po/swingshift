"""Application composition; never starts a listener or changes the live gate."""
from starlette.applications import Starlette
from .routes_admin import routes as admin_routes
from .routes_hub import routes as hub_routes
from .releases import routes as release_routes


def build_app(store, cfg, *, authenticate=None, authenticate_hub=None, extra_routes=()):
    app = Starlette(routes=[*admin_routes, *hub_routes, *release_routes, *extra_routes])
    app.state.store = store
    app.state.cfg = cfg
    # The auth builder may inject the session resolver and OAuth routes. Until
    # attached, administrative routes fail closed rather than trust headers.
    app.state.authenticate = authenticate or (lambda request: None)
    app.state.authenticate_hub = authenticate_hub or (lambda request: None)
    return app
