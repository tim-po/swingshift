"""CP-4: the 1-button onboarding — mint a personalized bring-up, watch the
directory for the hub, hand off to the user's own dashboard (spec §7).

    portal  POST /api/onboard {label}           -> {id, command, expires_at, stub}
    machine POST /api/hubs/register  (Bearer enrollment token)   [routes_hub]
    machine POST /api/onboard/claim  (Bearer enrollment token, {fingerprint})
                                                -> hub-bound device token, once
    portal  GET  /api/onboard/{id}              -> pending | registered | online
                                                   (+ hub, handoff_url)

The enrollment token is a §6a device token with hub_id=None (it may only
register). Enrollments are durable (Store.put_enrollment); claiming atomically
trades the token for a hub-bound one and revokes it (Store.claim_enrollment),
so the bring-up is single-use; an unclaimed one dies after ENROLL_TTL like a
connect link, for register and claim alike. The machine writes the claimed
token into its hub_guard allowlist, and the handoff lands on the hub's
/_lyp/enter.

With a release store configured (cfg.release_store / LOOPYARD_RELEASE_STORE)
the bring-up is REAL (:class:`PortalBringUp`): start also mints an
account-bound, short-lived ``lyr_`` download token and the one-liner fetches
the gated install.sh from ``<portal>/releases/latest/install.sh`` with it as a
Bearer header, then hands install.sh the same token (every asset fetch is
gated and sha256-verified) plus the enrollment token for the hub bring-up.
Without a store (dev) the StubBringUp remains, so CP-4 stays testable.

Adding a further machine to an existing hub ({hub_id} in the mint body) wraps
origin_onboard.mint_join through the ``join_minter`` seam; it is off by default
because mint_join needs the hub's own state dir (it runs on the hub, not here).
With a release store the portal also mints a ``lyr_`` and hands the minter
``portal_url`` + ``download_token``, so the join line is ONE paste: the gated
install/upgrade, then ``yard origin up`` (origin only, never a local hub).

    hub     POST /api/hubs/{hub_id}/download-token  (Bearer hub-bound lyd_)
                                                -> {download_token, expires_at, portal_url}

is how a hub that serves its own connect doc (mcp_loops.origin_connect) gets
that token: origin_onboard.portal_download_token trades the device token
``yard enroll`` saved in enroll.json for an account-bound, short-lived lyr_.
"""
from __future__ import annotations

import inspect
import secrets
import shlex
import time
from dataclasses import asdict
from typing import Callable, Protocol

from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import device_token, download_token, hub_guard

ENROLL_TTL = 15 * 60  # same single-use window as origin_connect links


class BringUp(Protocol):
    """Renders the personalized one-liner a new machine runs."""

    def render(self, *, portal_url: str, enroll_token: str, label: str,
               download_token: str | None = None) -> dict: ...


class PortalBringUp:
    """The real bring-up: the portal-gated installer one-liner.

    install.sh installs the bundle and, because LOOPYARD_ENROLL_TOKEN is set,
    finishes with ``yard enroll`` (mcp_loops/yard.py): register -> claim ->
    hub_guard.provision -> ``python -m control_plane.hub_guarded`` (dashboard
    behind HubGuard + periodic heartbeat/device-hashes sync), i.e. the
    StubBringUp contract below. The desktop "Run on this Mac" download can
    carry the same two tokens.
    """

    stub = False

    def render(self, *, portal_url, enroll_token, label, download_token=None):
        if not download_token:
            raise ValueError("portal bring-up needs a download token")
        from mcp_loops.origin_onboard import fetched_install_command
        from mcp_loops.release_urls import portal_install_sh_url
        q = shlex.quote
        # No -L: the portal serves directly, a redirect must not see the token.
        fetch = f"curl -fsS -H {q('Authorization: Bearer ' + download_token)} {q(portal_install_sh_url(portal_url))}"
        env = (f"LOOPYARD_PORTAL_URL={q(portal_url)} LOOPYARD_INSTALL_TOKEN={q(download_token)} "
               f"LOOPYARD_ENROLL_TOKEN={q(enroll_token)}")
        # Download -> verify -> run, never `curl | sh`: a 401/404/truncated
        # fetch must fail loud instead of sh exiting 0 on an empty script.
        return {"kind": "cli", "stub": False, "command": fetched_install_command(fetch, "", env=env)}


class StubBringUp:
    """Stands in for Track B's installer so CP-4 is testable today.

    The real bring-up is PortalBringUp (+ ``yard enroll``); this one stays for
    dev without a release store. The contract both honour: the machine
    registers via /api/hubs/register, then claims via /api/onboard/claim with
    the enrollment token as Bearer, then provisions the returned device_token
    into its dashboard allowlist
    (hub_guard.provision(<hub state>/dashboard-devices.json, token)) and runs
    the dashboard behind hub_guard.HubGuard.
    """

    stub = True

    def render(self, *, portal_url, enroll_token, label, download_token=None):
        env = f"LOOPYARD_PORTAL_URL={shlex.quote(portal_url)} LOOPYARD_ENROLL_TOKEN={shlex.quote(enroll_token)}"
        return {"kind": "cli", "stub": True,
                "command": f"{env} sh -c 'echo \"[stub bring-up] Loopyard installer not wired yet (Track B)\"'"}


def status_of(store, ob, hub_ttl: float, now: float) -> dict:
    """Watch: pending until the machine claims, then follow its hub's liveness."""
    out = {"id": ob.id, "label": ob.label, "expires_at": ob.expires_at, "status": "pending",
           "hub": None, "handoff_url": None}
    if ob.hub_id is None:
        if ob.expires_at <= now:
            out["status"] = "expired"
        return out
    hub = store.get_hub(ob.hub_id)
    if hub is None or hub.account_id != ob.account_id:
        out["status"] = "expired"
        return out
    online = hub.status == "online" and now - hub.last_seen < hub_ttl
    out.update(status="online" if online else "registered", hub=asdict(hub),
               handoff_url=hub.public_url.rstrip("/") + hub_guard.ENTER_PATH if online else None)
    return out


# ------------------------------------------------------------------ routes
async def _portal_account(request, mutation: bool):
    app = request.app
    if mutation and request.headers.get("origin", "").rstrip("/") != app.state.cfg.portal_url.rstrip("/"):
        return None, JSONResponse({"error": "invalid origin"}, status_code=403)
    account = app.state.authenticate(request)
    if inspect.isawaitable(account):
        account = await account
    if not account:
        return None, JSONResponse({"error": "authentication required"}, status_code=401)
    return account, None


def _no_store(body, status=200):
    return JSONResponse(body, status_code=status, headers={"cache-control": "no-store"})


def with_install_preferences(bring, runtime, trusted):
    """One payload for executable downloads and copy/paste installs."""
    env = {"LOOPYARD_RUNTIME": runtime, "LOOPYARD_TRUSTED_WORKSPACE": "1" if trusted else "0"}
    command = "(export " + " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    command += "; " + bring["command"] + ")"
    permissions = "trusted-workspace (explicitly accepted; no enforced OS isolation or role restrictions)" if trusted else "role permission profiles"
    agent_prompt = (
        "Set up Loopyard on this machine for me. Work in this logged-in user's account.\n"
        f"My selected agent CLI is {runtime}; permissions: {permissions}. "
        "The agent helping with installation may be any CLI; keep my selected runtime.\n"
        "Check the OS/architecture and existing Loopyard installation first. Preserve projects, "
        "CLI logins, conversations and existing Loopyard data. Do not uninstall or reset anything. "
        "Check the selected CLI, git and tmux. Help install missing prerequisites using the "
        "platform's supported method. If authentication is needed, ask me to complete the CLI's "
        "interactive sign-in; never request, print or copy credentials from another user account.\n"
        "Run the exact personalized command below. It downloads the signed bundle, saves the "
        "selected settings on a fresh installation, enrolls the machine and starts its services. "
        "Keep signature verification enabled. Existing installations retain their saved runtime; "
        "if it differs from my selection, explain the mismatch before changing a running setup.\n"
        "This command contains private single-use enrollment credentials and expires in 15 minutes. "
        "Do not put it in source control, shared documents or diagnostic output. If expired, ask me "
        "for a fresh setup handoff from the portal. Do not invent or reuse tokens.\n\n"
        "```sh\n" + command + "\n```\n\n"
        "After installation, use ~/loopyard/bin/yard runtime show and ~/loopyard/bin/yard status --check "
        "to verify the saved provider and running services. Confirm this machine appears online "
        "in the control panel, and give me the dashboard address and any remaining action. "
        "Diagnose failures without deleting data or weakening permissions. Stop and report honestly "
        "if setup cannot finish. Do not start project work or loops as part of installation."
    )
    return {**bring, "command": command, "runtime": runtime,
            "trusted_workspace": trusted, "install_env": env, "agent_prompt": agent_prompt,
            "script": "#!/bin/sh\nset -eu\n" + command + "\n"}


async def start(request):
    account, err = await _portal_account(request, mutation=True)
    if err is not None:
        return err
    app, now = request.app, time.time()
    store, cfg = app.state.store, app.state.cfg
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _no_store({"error": "JSON object required"}, 400)
    runtime = body.get("runtime", "claude")
    trusted = body.get("trusted_workspace", False)
    if runtime not in ("claude", "codex", "cursor") or type(trusted) is not bool:
        return _no_store({"error": "invalid provider or permissions"}, 400)
    if runtime in ("codex", "cursor") and not trusted:
        return _no_store({"error": "Codex/Cursor require explicit trusted-workspace consent"}, 400)
    label = str(body.get("label") or "").strip()[:80] or "my machine"
    hub_id = body.get("hub_id")
    if hub_id is not None:
        return await _join_existing(request, account, hub_id, label, runtime, trusted)
    try:
        token_id, token = device_token.issue(store, account.id, None, f"onboard:{label}")
    except ValueError as exc:
        return _no_store({"error": str(exc)}, 400)
    ob = store.put_enrollment(secrets.token_urlsafe(12), account.id, token_id, label, now, now + ENROLL_TTL)
    extra, dl = {}, None
    if getattr(app.state.bring_up, "stub", True) is False:
        try:  # account-bound; lives no longer than the enrollment it installs for
            _, dl, dl_exp = download_token.issue(store, account.id, ttl=min(download_token.DL_TTL, ENROLL_TTL), now=now)
        except ValueError as exc:
            store.revoke_device_token(token_id)
            return _no_store({"error": str(exc)}, 400)
        extra = {"download_expires_at": dl_exp}
    bring = app.state.bring_up.render(portal_url=cfg.portal_url, enroll_token=token, label=label,
                                      download_token=dl)
    bring = with_install_preferences(bring, runtime, trusted)
    from mcp_loops import origin_connect
    link = origin_connect.mint_portal_link(
        owner=account.id, enrollment_id=ob.id, document=bring.pop("agent_prompt"),
        script=bring["script"], expires_at=ob.expires_at,
        state_dir=_connect_state(app), public_url=cfg.portal_url)
    bring["agent_setup_url"] = link["url"]
    return _no_store({"id": ob.id, "label": label, "expires_at": ob.expires_at, **extra, **bring}, 201)


def _connect_state(app):
    return app.state.store.path + ".connect"


async def serve_setup_link(request):
    """Existing origin-connect HTTP protocol, backed by portal enrollment."""
    from mcp_loops import origin_connect as oc
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
               "X-Robots-Tag": "noindex", "X-Content-Type-Options": "nosniff"}
    as_sh = request.query_params.get("format") in ("sh", "shell")
    media = "text/x-shellscript" if as_sh else "text/markdown"
    if oc.is_preview_fetch(request.method, request.headers.get("user-agent")):
        body = "#!/bin/sh\n# Preview only; fetch with GET.\nexit 1\n" if as_sh else oc.PREVIEW_BODY
        return Response(body, media_type=media, headers={**headers, "X-Loopyard-Preview": "1"})
    try:
        rec = oc.read_portal_link(request.path_params["token"], state_dir=_connect_state(request.app))
        store = request.app.state.store
        ob = store.get_enrollment(rec["enrollmentId"])
        if ob is None or ob.account_id != rec["owner"]:
            raise oc.LinkError("unknown_link", "setup enrollment not found")
        if ob.claimed_at is not None:
            raise oc.LinkError("link_consumed", "this link already enrolled a machine")
        account = store.get_account(ob.account_id)
        token = store.get_device_token(ob.token_id)
        if account is None or account.status != "active" or token is None or token.revoked:
            raise oc.LinkError("link_revoked", "setup access was revoked")
        if time.time() >= ob.expires_at:
            raise oc.LinkError("link_expired", "setup link expired")
    except oc.LinkError as exc:
        body = ("#!/bin/sh\n# Setup link refused. Generate a new link on the portal setup page.\nexit 1\n"
                if as_sh else f"# Setup link refused\n\n{exc.code}: {exc.message}. Generate a new link on the portal setup page.")
        return Response(body, status_code=exc.status, media_type=media, headers=headers)
    return Response(rec["script"] if as_sh else rec["document"], media_type=media, headers=headers)


async def _join_existing(request, account, hub_id, label, runtime="claude", trusted=False):
    """A further machine joins an existing hub as an origin (wraps mint_join)."""
    store, minter = request.app.state.store, request.app.state.join_minter
    hub = store.get_hub(hub_id) if isinstance(hub_id, str) else None
    if hub is None or hub.account_id != account.id:
        return _no_store({"error": "hub not found"}, 404)
    if minter is None:
        # TODO(Track B): the hub mints its own join code (mint_join runs against
        # the hub's state dir); the CP relays it once the hub exposes that.
        return _no_store({"error": "adding a machine to an existing hub is not wired yet"}, 501)
    portal = {}
    if request.app.state.cfg.release_store:
        try:  # the one-paste join also installs/upgrades the client
            _, dl, _ = download_token.issue(store, account.id)
        except ValueError as exc:
            return _no_store({"error": str(exc)}, 400)
        portal = {"portal_url": request.app.state.cfg.portal_url, "download_token": dl}
    try:
        minted = minter(hub_url=hub.public_url, owner=account.email, label=label, **portal)
    except Exception:
        return _no_store({"error": "the hub could not mint a join code"}, 502)
    return _no_store({"id": None, "label": label, "kind": "join", "stub": False,
                      **with_install_preferences({"command": minted["command"]}, runtime, trusted), "expires_at": minted.get("expiresAt"),
                      "hub_id": hub.id}, 201)


async def watch(request):
    account, err = await _portal_account(request, mutation=False)
    if err is not None:
        return err
    app, now = request.app, time.time()
    ob = app.state.store.get_enrollment(request.path_params["oid"])
    if ob is None or ob.account_id != account.id:
        return _no_store({"error": "onboarding not found"}, 404)
    out = status_of(app.state.store, ob, app.state.cfg.hub_ttl, now)
    if out["status"] == "expired" and ob.claimed_at is None:
        app.state.store.revoke_device_token(ob.token_id)
    return _no_store(out)


async def claim(request):
    """Machine side: trade the enrollment token for a hub-bound device token."""
    app, now = request.app, time.time()
    store = app.state.store
    hit = device_token.verify(store, device_token.bearer(request))
    ob = store.enrollment_by_token(hit[0].id) if hit and hit[0].hub_id is None else None
    if ob is None or ob.account_id != hit[1].id or ob.claimed_at is not None:
        return _no_store({"error": "invalid enrollment token"}, 401)
    if not ob.created_at <= now < ob.expires_at:
        store.revoke_device_token(ob.token_id)  # dead anyway (TTL); make it explicit
        return _no_store({"error": "enrollment expired"}, 401)
    try:
        body = await request.json()
    except ValueError:
        body = None
    fp = body.get("fingerprint") if isinstance(body, dict) else None
    hub = next((h for h in store.hubs_for(ob.account_id) if h.fingerprint == fp), None) if isinstance(fp, str) else None
    if hub is None:
        return _no_store({"error": "register the hub before claiming"}, 409)
    token = device_token.PREFIX + secrets.token_urlsafe(32)
    # One txn: insert the hub-bound token, revoke the enrollment token, mark claimed.
    done = store.claim_enrollment(ob.id, ob.account_id, hub.id, device_token.token_hash(token), now=now)
    if done is None:  # lost a race / expired in between
        return _no_store({"error": "invalid enrollment token"}, 401)
    return _no_store({"hub_id": hub.id, "device_token_id": done.device_token_id, "device_token": token}, 201)


async def hub_download_token(request):
    """Hub side: a hub-bound device token buys an account-bound lyr_ so the
    connect doc the hub serves can install/upgrade from this portal."""
    app = request.app
    if not app.state.cfg.release_store:
        return _no_store({"error": "no release store"}, 404)
    hit = device_token.verify(app.state.store, device_token.bearer(request))
    if (hit is None or hit[0].hub_id is None
            or hit[0].hub_id != request.path_params["hub_id"]):
        return _no_store({"error": "authentication required"}, 401)
    try:
        _, dl, exp = download_token.issue(app.state.store, hit[1].id)
    except ValueError as exc:
        return _no_store({"error": str(exc)}, 400)
    return _no_store({"download_token": dl, "expires_at": exp,
                      "portal_url": app.state.cfg.portal_url}, 201)


routes = [
    Route("/origin-connect/{token}", serve_setup_link, methods=["GET", "HEAD"]),
    Route("/api/onboard", start, methods=["POST"]),
    Route("/api/hubs/{hub_id}/download-token", hub_download_token, methods=["POST"]),
    Route("/api/onboard/claim", claim, methods=["POST"]),
    Route("/api/onboard/{oid}", watch, methods=["GET"]),
]


def install(app, *, bring_up: BringUp | None = None, join_minter: Callable | None = None) -> None:
    import logging
    class RedactSetupLink(logging.Filter):
        def filter(self, record):
            if isinstance(record.args, tuple) and len(record.args) == 5:
                args = list(record.args)
                if str(args[2]).startswith("/origin-connect/"):
                    args[2] = "/origin-connect/[redacted]"
                    record.args = tuple(args)
            return True
    logging.getLogger("uvicorn.access").addFilter(RedactSetupLink())
    if bring_up is None:
        bring_up = PortalBringUp() if app.state.cfg.release_store else StubBringUp()
    app.state.bring_up = bring_up
    app.state.join_minter = join_minter


def origin_onboard_minter(**kw):
    """join_minter for a portal co-located with the hub (owner dogfood box).

    The directory's ``public_url`` is the hub's guarded dashboard (https), not
    the origin Hub's wss:// address a box dials, so an http(s) URL is dropped
    and mint_join resolves this machine's own Hub (hub_record /
    $LOOPYARD_HUB_PUBLIC_URL)."""
    from mcp_loops import origin_onboard
    if str(kw.get("hub_url") or "").lower().startswith(("http://", "https://")):
        kw["hub_url"] = None
    return origin_onboard.mint_join(**kw)
