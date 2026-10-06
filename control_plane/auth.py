"""CP-1: Google sign-in (authorization code + PKCE) behind the email-invite gate.

Flow: GET /login -> Google -> GET /oauth/callback -> invite gate -> lyp_sess.
The session cookie reuses the dashgate's HMAC codec (tracking_ui.session_cookie,
"<uid>.<exp>.<sid>.<hmac>") with uid = account.id. A session is valid only while
the account is active, its sid is not revoked and the sid's generation equals
the account's current generation (log-out-everywhere). Every failure is closed.

Google is reached only through the TokenVerifier seam; tests inject a fake.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from tracking_ui import session_cookie

SESSION_COOKIE = "lyp_sess"
OAUTH_COOKIE = "lyp_oauth"
SESSION_TTL = 7 * 24 * 3600
OAUTH_TTL = 600
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


class TokenVerifier(Protocol):
    """Exchanges an authorization code (with its PKCE verifier) for id_token claims."""

    def exchange(self, code: str, code_verifier: str, redirect_uri: str) -> dict: ...


class GoogleTokenVerifier:
    """Real exchange against Google's token endpoint.

    The id_token arrives directly from Google over server-validated TLS in
    exchange for our client secret, so per OIDC Core 3.1.3.7 the TLS channel
    authenticates the issuer; iss/aud/exp/nonce are still checked by
    validate_claims().
    """

    def __init__(self, client_id: str, client_secret: str, token_url: str = GOOGLE_TOKEN_URL):
        self.client_id, self.client_secret, self.token_url = client_id, client_secret, token_url

    def exchange(self, code, code_verifier, redirect_uri):
        data = urllib.parse.urlencode({
            "code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "authorization_code",
        }).encode()
        with urllib.request.urlopen(urllib.request.Request(self.token_url, data=data), timeout=10) as r:
            id_token = json.load(r)["id_token"]
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str
    name: str


def validate_claims(claims: dict, client_id: str, nonce: str, now: float | None = None) -> Identity:
    now = time.time() if now is None else now
    if not isinstance(claims, dict):
        raise AuthError("malformed id_token")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise AuthError("wrong issuer")
    aud = claims.get("aud")
    if not client_id or (aud != client_id and not (isinstance(aud, list) and client_id in aud)):
        raise AuthError("wrong audience")
    try:
        if float(claims.get("exp", 0)) < now:
            raise AuthError("id_token expired")
    except (TypeError, ValueError):
        raise AuthError("malformed exp") from None
    if not nonce or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise AuthError("nonce mismatch")
    if claims.get("email_verified") not in (True, "true"):
        raise AuthError("email not verified")
    sub, email = claims.get("sub"), claims.get("email")
    if not isinstance(sub, str) or not sub or not isinstance(email, str) or not email:
        raise AuthError("missing subject or email")
    return Identity(sub, email.strip().lower(), str(claims.get("name") or email))


# ------------------------------------------------------------------ helpers
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, _b64(hashlib.sha256(verifier.encode()).digest())


def _seal(obj: dict, secret: str) -> str:
    body = _b64(json.dumps(obj, separators=(",", ":")).encode())
    mac = hmac.new(secret.encode(), b"oauth." + body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def _unseal(value: str, secret: str) -> dict | None:
    try:
        body, mac = value.split(".")
    except (ValueError, AttributeError):
        return None
    good = hmac.new(secret.encode(), b"oauth." + body.encode(), hashlib.sha256).hexdigest()
    if not secret or not hmac.compare_digest(good, mac):
        return None
    try:
        obj = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and obj.get("exp", 0) >= time.time() else None


def _safe_next(value) -> str:
    # Only same-site absolute paths; "//host" and "/\\host" are open redirects.
    if not isinstance(value, str) or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    return value


def _cookie_kw(cfg) -> dict:
    return {"httponly": True, "secure": True, "samesite": "lax", "path": "/"}


def issue_session(store, account, secret: str, now: float | None = None) -> str:
    exp = int((time.time() if now is None else now) + SESSION_TTL)
    sid = f"{store.generation(account.id)}~{secrets.token_urlsafe(16)}"
    return session_cookie.sign(account.id, exp, sid, secret)


def resolve_session(store, value: str, secret: str):
    """(account, sid) for a live session cookie, else None."""
    parsed = session_cookie.parse(value or "", secret)
    if parsed is None:
        return None
    account_id, _exp, sid = parsed
    gen, sep, _rand = sid.partition("~")
    if not sep or not gen.isdigit():
        return None
    account = store.get_account(account_id)
    if account is None or account.status != "active":
        return None
    if store.session_revoked(sid) or int(gen) != store.generation(account.id):
        return None
    return account, sid


def make_authenticator(store, cfg):
    """build_app(authenticate=...) resolver: request -> active Account | None."""
    def authenticate(request):
        hit = resolve_session(store, request.cookies.get(SESSION_COOKIE, ""), cfg.session_secret)
        return hit[0] if hit else None
    return authenticate


def _same_origin(request) -> bool:
    return request.headers.get("origin", "").rstrip("/") == request.app.state.cfg.portal_url.rstrip("/")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>{html.escape(title)}</h1>{body}</body>", status_code=status,
        headers={"cache-control": "no-store"})


# ------------------------------------------------------------------ routes
async def login(request):
    cfg = request.app.state.cfg
    if not cfg.session_secret or not cfg.google_client_id:
        return _page("Sign-in unavailable", "<p>The portal is not configured.</p>", 503)
    verifier, challenge = pkce_pair()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    params = {
        "client_id": cfg.google_client_id, "redirect_uri": f"{cfg.portal_url}/oauth/callback",
        "response_type": "code", "scope": "openid email profile", "state": state,
        "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    resp = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}", status_code=302)
    sealed = _seal({"state": state, "verifier": verifier, "nonce": nonce,
                    "next": _safe_next(request.query_params.get("next")),
                    "exp": int(time.time()) + OAUTH_TTL}, cfg.session_secret)
    resp.set_cookie(OAUTH_COOKIE, sealed, max_age=OAUTH_TTL, **_cookie_kw(cfg))
    return resp


def _denied(reason: str, status: int = 400) -> Response:
    resp = _page("Sign-in failed", f"<p>{html.escape(reason)}</p><p><a href='/login'>Try again</a></p>", status)
    resp.delete_cookie(OAUTH_COOKIE, path="/")
    return resp


async def callback(request):
    app, cfg = request.app, request.app.state.cfg
    pending = _unseal(request.cookies.get(OAUTH_COOKIE, ""), cfg.session_secret)
    state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
    if pending is None or not state or not hmac.compare_digest(str(pending.get("state", "")), state):
        return _denied("This sign-in link is invalid or expired.")
    if request.query_params.get("error") or not code:
        return _denied("Google sign-in was cancelled.")
    try:
        claims = app.state.token_verifier.exchange(code, pending["verifier"], f"{cfg.portal_url}/oauth/callback")
        ident = validate_claims(claims, cfg.google_client_id, pending["nonce"])
    except Exception:  # any exchange/claims failure is a refusal, never a session
        return _denied("Google could not confirm your identity.", 401)
    store = app.state.store
    try:
        account = store.create_account_if_invited(ident.sub, ident.email, ident.name)
    except ValueError:
        account = None
    if account is None:
        resp = _page("Not invited yet",
                     f"<p>Loopyard is invite-only. <b>{html.escape(ident.email)}</b> has no invite.</p>"
                     "<p>Ask the person who told you about Loopyard to invite this address, then sign in again.</p>",
                     403)
        resp.delete_cookie(OAUTH_COOKIE, path="/")
        return resp
    if account.status != "active":
        return _denied("This account is disabled.", 403)
    resp = RedirectResponse(pending.get("next") or "/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, issue_session(store, account, cfg.session_secret),
                    max_age=SESSION_TTL, **_cookie_kw(cfg))
    resp.delete_cookie(OAUTH_COOKIE, path="/")
    return resp


async def logout(request):
    if not _same_origin(request):
        return JSONResponse({"error": "invalid origin"}, status_code=403)
    store, cfg = request.app.state.store, request.app.state.cfg
    hit = resolve_session(store, request.cookies.get(SESSION_COOKIE, ""), cfg.session_secret)
    if hit:
        account, sid = hit
        store.revoke_session(account.id, sid)
        if request.query_params.get("everywhere") == "1":
            store.bump_generation(account.id)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


async def me(request):
    account = make_authenticator(request.app.state.store, request.app.state.cfg)(request)
    if account is None:
        return JSONResponse({"error": "authentication required"}, status_code=401,
                            headers={"cache-control": "no-store"})
    from .invites import is_owner
    return JSONResponse({"id": account.id, "email": account.email, "display_name": account.display_name,
                         "is_owner": is_owner(account, request.app.state.cfg)},
                        headers={"cache-control": "no-store"})


routes = [
    Route("/login", login, methods=["GET"]),
    Route("/oauth/callback", callback, methods=["GET"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/api/me", me, methods=["GET"]),
]


def bootstrap_owner_invites(store, cfg) -> None:
    """Owners are invite-gated like everyone else; seed an open invite once."""
    existing = {i.email for i in store.list_invites()}
    for email in sorted(cfg.owner_emails):
        email = email.strip().lower()
        if email and email not in existing:
            store.mint_invite(email, "bootstrap")


def build_portal_app(store, cfg, verifier: TokenVerifier | None = None, *, authenticate_hub=None,
                     extra_routes=(), bring_up=None, join_minter=None, portal_dist: str | None = None):
    """Compose the backend app with auth, device-token and onboarding routes.

    ``portal_dist`` (default $CP_PORTAL_DIST) is the built frontend/apps/portal;
    when set it is served at / after every API route.
    """
    import os
    from .app import build_app
    from . import device_token, onboard
    if verifier is None:
        verifier = GoogleTokenVerifier(cfg.google_client_id, cfg.google_client_secret)
    bootstrap_owner_invites(store, cfg)
    tail = []
    portal_dist = portal_dist if portal_dist is not None else os.environ.get("CP_PORTAL_DIST", "")
    if portal_dist:
        from starlette.routing import Mount
        from starlette.staticfiles import StaticFiles
        tail.append(Mount("/", StaticFiles(directory=portal_dist, html=True), name="portal"))
    app = build_app(store, cfg, authenticate=make_authenticator(store, cfg),
                    authenticate_hub=authenticate_hub or device_token.make_hub_authenticator(store),
                    extra_routes=[*routes, *device_token.routes, *onboard.routes, *extra_routes, *tail])
    app.state.token_verifier = verifier
    onboard.install(app, bring_up=bring_up, join_minter=join_minter)
    return app
