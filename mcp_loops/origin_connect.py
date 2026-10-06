"""origin_connect — the ONE-TIME ORIGIN-CONNECT LINK.

The owner clicks "Generate one-time link for your AI" and gets a short URL
(``<public-base>/origin-connect/<token>``). They paste only that URL to their AI.
The AI fetches it and gets a complete, self-contained instruction document
(markdown, or a runnable script with ``?format=sh``). The document connects THIS
machine as an origin: detect OS/arch → check prerequisites → install the bundle
→ run the exact ``yard origin up`` line → verify ``yard origin status``.

There is no new auth model. The link token is only a short-lived, single-use
bearer whose READS ARE IDEMPOTENT: an AI must fetch the page to read it, then
act, so repeated fetches re-hand the SAME pairing code until a device actually
pairs. On the first fetch the link mints a normal pairing
code is minted through :func:`origin_onboard.mint_join`. That is the same
single-use, owner-carrying, short-lived :class:`EnrollmentStore` code that
``yard hub pair-code`` hands out. The document embeds that code, so the link
never outlives what it authorizes.

Records live beside the Hub's enrollment store (``<hub-state>/origin_connect/``).
Minting (MCP server) and serving (dashboard) are separate processes that can both
see the Hub's ``--state-dir``. Only the SHA-256 of a token is stored, so a copy
of the directory does not hold live links. Consumption is claimed with an
``O_EXCL`` marker file, so two racing fetches cannot both get a document.

Refusals are fail-closed, and each has a distinct, stable ``code``:
``unknown_link`` (404), ``link_expired`` / ``link_consumed`` / ``link_revoked``
(410) and ``wrong_owner`` (403).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import time
from typing import Optional

from mcp_loops import origin_onboard

#: the base URL the AI reaches this host at (a CF tunnel → the gate). Falls back
#: to the mac-origin bootstrap's ``MAC_ORIGIN_URL`` (the same public host).
PUBLIC_URL_ENV = "LOOPYARD_PUBLIC_URL"
LEGACY_PUBLIC_URL_ENV = "MAC_ORIGIN_URL"

ROUTE_PREFIX = "/origin-connect/"
DEFAULT_TTL_SEC = 15 * 60   # 15-minute link lifetime (the pair-code install window below is separate)
MIN_TTL_SEC = 60
MAX_TTL_SEC = 24 * 60 * 60

#: how long the pairing code minted at fetch time stays claimable. A plain
#: ``yard hub pair-code`` lives 10 min (``PAIRING_TTL_SEC``); an AI following the
#: doc may first install tmux/git/claude + the bundle, so the link's code gets a
#: longer (bounded) window. Still single-use; revoke still kills it.
PAIR_TTL_ENV = "LOOPYARD_ORIGIN_CONNECT_PAIR_TTL"
DEFAULT_PAIR_TTL_SEC = 30 * 60
MAX_PAIR_TTL_SEC = 60 * 60

# 16 random bytes → 22 url-safe chars (128 bits): short to paste, not guessable.
_TOKEN_BYTES = 16
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")

REFUSAL_STATUS = {
    "unknown_link": 404,
    "link_expired": 410,
    "link_consumed": 410,
    "link_revoked": 410,
    "wrong_owner": 403,
}


class LinkError(ValueError):
    """A refused mint/fetch/revoke. ``code`` is stable; ``status`` is the HTTP
    status the endpoint answers with."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = REFUSAL_STATUS.get(code, 400)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def link_id(token: str) -> str:
    """The public handle of a link (UI status / revoke). A prefix of the token
    hash, so it names a link without being able to fetch it."""
    return _hash(token)[:16]


def _links_dir(state_dir: Optional[str]) -> str:
    d = os.path.join(origin_onboard.resolve_state_dir(state_dir), "origin_connect")
    os.makedirs(d, exist_ok=True)
    return d


def _rec_path(d: str, lid: str) -> str:
    return os.path.join(d, f"{lid}.json")


def _read(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write(path: str, rec: dict) -> None:
    # 0600 + atomic: a consumed record carries the live plaintext pairing code
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                       "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)  # a stale tmp keeps its old mode otherwise
            json.dump(rec, fh)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def resolve_public_url(public_url: Optional[str] = None) -> str:
    base = (public_url or os.environ.get(PUBLIC_URL_ENV)
            or os.environ.get(LEGACY_PUBLIC_URL_ENV) or "").strip().rstrip("/")
    if not base:
        raise LinkError("public_url_required",
                        f"no public base URL: pass public_url or set "
                        f"${PUBLIC_URL_ENV} (the address an AI reaches this "
                        f"host at)")
    m = re.match(r"^(https?)://([^/:]+)", base, re.I)
    if not m:
        raise LinkError("bad_public_url", f"{base!r} is not an http(s) URL")
    loopback = m.group(2) in ("127.0.0.1", "localhost", "::1")
    if m.group(1).lower() != "https" and not loopback:
        raise LinkError("insecure_public_url",
                        f"{base} is plaintext and not loopback; the link is a "
                        f"bearer secret, so it is only minted for an https URL")
    return base


def _owner(owner: Optional[str]) -> str:
    from mcp_loops.origin_proto.enrollment import DEFAULT_OWNER
    return (owner or "").strip() or DEFAULT_OWNER


def mint_link(*, owner: Optional[str] = None, label: Optional[str] = None,
              hub_url: Optional[str] = None, state_dir: Optional[str] = None,
              install_url: Optional[str] = None,
              public_url: Optional[str] = None,
              ttl_sec: Optional[float] = None,
              now: Optional[float] = None) -> dict:
    """Mint a single-use, short-lived, owner-bound connect link.

    The Hub is checked now (URL, TLS cert → fingerprint), so the owner never gets
    a link that cannot work. The pairing code itself is minted at fetch time.
    Returns ``{id, url, token, expiresAt, owner, label, hub, hubFingerprint}``.
    Raises :class:`LinkError` or :class:`origin_onboard.MintError`."""
    t = time.time() if now is None else float(now)
    ttl = DEFAULT_TTL_SEC if ttl_sec in (None, 0, "") else float(ttl_sec)
    if not (MIN_TTL_SEC <= ttl <= MAX_TTL_SEC):
        raise LinkError("bad_ttl", f"ttl_sec must be between {MIN_TTL_SEC} and "
                                   f"{MAX_TTL_SEC} seconds")
    base = resolve_public_url(public_url)
    hub, fingerprint, root = origin_onboard.resolve_hub(hub_url, state_dir)
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    lid = link_id(token)
    rec = {
        "id": lid, "tokenHash": _hash(token), "owner": _owner(owner),
        "label": (label or "").strip() or None, "hub": hub.describe(),
        "hubFingerprint": fingerprint,
        "installUrl": origin_onboard.resolve_install_url(install_url),
        "createdAt": t, "expiresAt": t + ttl,
        "consumedAt": None, "revokedAt": None, "pairCode": None,
    }
    _write(_rec_path(_links_dir(root), lid), rec)
    return {"id": lid, "url": f"{base}{ROUTE_PREFIX}{token}", "token": token,
            "expiresAt": rec["expiresAt"], "owner": rec["owner"],
            "label": rec["label"], "hub": rec["hub"],
            "hubFingerprint": fingerprint}


def mint_portal_link(*, owner: str, enrollment_id: str, document: str,
                     script: str, expires_at: float, state_dir: str,
                     public_url: str) -> dict:
    """Portal enrollment variant of the existing connect-link store.

    The portal checks the enrollment's claim/revocation on every read. No
    pairing code or shared Hub is involved when creating a user's first Hub.
    """
    base = resolve_public_url(public_url)
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    lid = link_id(token)
    rec = {"id": lid, "tokenHash": _hash(token), "owner": owner,
           "kind": "portal-enrollment", "enrollmentId": enrollment_id,
           "createdAt": time.time(), "expiresAt": expires_at,
           "revokedAt": None, "document": document, "script": script}
    _write(_rec_path(_links_dir(state_dir), lid), rec)
    return {"id": lid, "url": f"{base}{ROUTE_PREFIX}{token}", "expiresAt": expires_at}


def read_portal_link(token: str, *, state_dir: str) -> dict:
    """Repeat reads are allowed; the backing enrollment is single-use."""
    _, rec = _load(token, state_dir)
    _check(rec, now=time.time(), owner=None)
    if rec.get("kind") != "portal-enrollment":
        raise LinkError("unknown_link", "not a portal enrollment link")
    return rec


def _load(token: str, state_dir: Optional[str]) -> tuple:
    if not token or not _TOKEN_RE.match(token):
        raise LinkError("unknown_link", "this connect link is not valid")
    d = _links_dir(state_dir)
    lid = link_id(token)
    rec = _read(_rec_path(d, lid))
    # compare the full hash: the id is only a 64-bit prefix
    if rec is None or not secrets.compare_digest(rec.get("tokenHash", ""),
                                                 _hash(token)):
        raise LinkError("unknown_link", "this connect link is not valid")
    return d, rec


def _check(rec: dict, *, now: float, owner: Optional[str]) -> None:
    if owner is not None and _owner(owner) != rec.get("owner"):
        raise LinkError("wrong_owner",
                        "this connect link belongs to a different owner")
    if rec.get("revokedAt"):
        raise LinkError("link_revoked", "this connect link was revoked by its "
                                        "owner; generate a new one")
    if now > float(rec.get("expiresAt") or 0):
        raise LinkError("link_expired", "this connect link has expired; "
                                        "generate a new one")


def consume_link(token: str, *, state_dir: Optional[str] = None,
                 owner: Optional[str] = None,
                 now: Optional[float] = None) -> dict:
    """Validate a link and return the pairing code it stands for (idempotent).

    ``owner`` (optional) is a scope: when given, a link of any other owner is
    refused ``wrong_owner``. It can only narrow access, never widen it. Returns
    the :func:`origin_onboard.mint_join` result plus ``link`` (the record)."""
    t = time.time() if now is None else float(now)
    d, rec = _load(token, state_dir)
    _check(rec, now=t, owner=owner)
    # the Hub state the link was minted against (never a caller-chosen dir)
    root = os.path.dirname(d)
    # IDEMPOTENT READS: an AI must fetch the page to READ it, then act, so a
    # repeated GET must NOT 410. If we already minted the pairing code this link
    # stands for, re-hand the SAME one (re-extended) until a machine actually
    # pairs with it. The link is spent only when that code is CLAIMED (a device
    # enrolls) — or when it is revoked / expires.
    prev = rec.get("pairCode")
    if prev:
        pair = _pair_state(root, prev)
        if pair and pair.get("consumed"):
            raise LinkError("link_consumed", "this connect link already paired a "
                                             "machine; generate a new one to connect another")
        exp = _extend_pair_code(root, prev, now=t)
        install = origin_onboard.resolve_install_url(rec.get("installUrl") or "")
        # a fresh short-lived download token per read (portal-enrolled Hub)
        portal = origin_onboard.portal_download_token(root) or {}
        join = {
            "code": prev, "owner": rec["owner"], "label": rec.get("label"),
            "expiresAt": exp, "hub": rec["hub"],
            "hubFingerprint": rec.get("hubFingerprint"), "stateDir": root,
            "portalUrl": portal.get("portalUrl"),
            "downloadToken": portal.get("downloadToken"),
            "command": origin_onboard.join_command(
                rec["hub"], prev, fingerprint=rec.get("hubFingerprint"), install_url=install,
                portal_url=portal.get("portalUrl"),
                download_token=portal.get("downloadToken")),
            "joinLink": origin_onboard.join_link(
                rec["hub"], code=prev, fingerprint=rec.get("hubFingerprint"), install_url=install),
        }
        return dict(join, installUrl=rec.get("installUrl"),
                    link=_public(rec, now=t, state_dir=root))
    # FIRST fetch: mint the pairing code the link stands for.
    join = origin_onboard.mint_join(
        hub_url=rec["hub"], state_dir=root, owner=rec["owner"],
        label=rec.get("label"), install_url=rec.get("installUrl") or "",
        now=t)
    if join.get("hubFingerprint") != rec.get("hubFingerprint"):
        # the Hub cert changed since the mint: refuse rather than hand out a
        # fingerprint the owner never saw
        rec["revokedAt"] = t
        _write(_rec_path(d, rec["id"]), rec)
        raise LinkError("link_revoked", "the Hub's certificate changed since this "
                                        "link was made; generate a new one")
    join["expiresAt"] = _extend_pair_code(root, join["code"], now=t)
    rec["consumedAt"] = t   # marks 'fetched' for the owner status view
    rec["pairCode"] = join["code"]
    _write(_rec_path(d, rec["id"]), rec)
    return dict(join, installUrl=rec.get("installUrl"),
                link=_public(rec, now=t, state_dir=root))


def revoke_link(lid: str, *, owner: Optional[str] = None,
                state_dir: Optional[str] = None,
                now: Optional[float] = None) -> dict:
    """Revoke an unused link by its ``id``. An already-fetched link's pairing
    code is also expired, so a fetched-but-not-yet-joined box cannot pair."""
    t = time.time() if now is None else float(now)
    if not lid or not _ID_RE.match(lid):
        raise LinkError("unknown_link", "no such connect link")
    d = _links_dir(state_dir)
    rec = _read(_rec_path(d, lid))
    if rec is None:
        raise LinkError("unknown_link", "no such connect link")
    if owner is not None and _owner(owner) != rec.get("owner"):
        raise LinkError("wrong_owner",
                        "this connect link belongs to a different owner")
    if not rec.get("revokedAt"):
        rec["revokedAt"] = t
        _write(_rec_path(d, lid), rec)
    if rec.get("pairCode"):
        _expire_pair_code(os.path.dirname(d), rec["pairCode"], now=t)
    return _public(rec, now=t, state_dir=os.path.dirname(d))


def pair_ttl_sec() -> float:
    """The fetched pairing code's window: ``$LOOPYARD_ORIGIN_CONNECT_PAIR_TTL``
    seconds, clamped to [60, MAX_PAIR_TTL_SEC]; unset/garbage → the default."""
    try:
        v = float(os.environ.get(PAIR_TTL_ENV) or DEFAULT_PAIR_TTL_SEC)
    except ValueError:
        v = DEFAULT_PAIR_TTL_SEC
    return max(float(MIN_TTL_SEC), min(float(MAX_PAIR_TTL_SEC), v))


def _extend_pair_code(root: str, code: str, *, now: float) -> float:
    """Give the just-minted pairing code the link's install window."""
    from mcp_loops.origin_proto.enrollment import EnrollmentStore
    store = EnrollmentStore(os.path.join(root, "enroll"))
    path = store._pairing_path(code)
    prec = store._read_json(path) or {}
    prec["expiresAt"] = now + pair_ttl_sec()
    store._write_json(path, prec)
    return prec["expiresAt"]


def _expire_pair_code(root: str, code: str, *, now: float) -> None:
    from mcp_loops.origin_proto.enrollment import EnrollmentStore
    store = EnrollmentStore(os.path.join(root, "enroll"))
    path = store._pairing_path(code)
    prec = store._read_json(path)
    if prec and not prec.get("consumed"):
        prec["expiresAt"] = min(float(prec.get("expiresAt") or now), now - 1)
        store._write_json(path, prec)


def _pair_state(root: str, code: Optional[str]) -> Optional[dict]:
    if not code:
        return None
    from mcp_loops.origin_proto.enrollment import EnrollmentStore
    store = EnrollmentStore(os.path.join(root, "enroll"))
    return store._read_json(store._pairing_path(code))


def _public(rec: dict, *, now: float, state_dir: str) -> dict:
    """The owner-facing view of a link: never the token, hash or pairing code.
    ``state`` ∈ pending | fetched | connected | expired | revoked."""
    pair = _pair_state(state_dir, rec.get("pairCode"))
    device = (pair or {}).get("deviceId") if (pair or {}).get("consumed") else None
    if device:
        state = "connected"
    elif rec.get("revokedAt"):
        state = "revoked"
    elif rec.get("consumedAt"):
        exp = float((pair or {}).get("expiresAt") or 0)
        state = "fetched" if now <= exp else "expired"
    elif now > float(rec.get("expiresAt") or 0):
        state = "expired"
    else:
        state = "pending"
    return {"id": rec["id"], "owner": rec["owner"], "label": rec.get("label"),
            "hub": rec["hub"], "createdAt": rec["createdAt"],
            "expiresAt": rec["expiresAt"], "fetchedAt": rec.get("consumedAt"),
            "revokedAt": rec.get("revokedAt"), "state": state,
            "deviceId": device}


def link_status(lid: str, *, owner: Optional[str] = None,
                state_dir: Optional[str] = None,
                now: Optional[float] = None) -> dict:
    """The live state of one link, for the UI's waiting → connected poll."""
    t = time.time() if now is None else float(now)
    if not lid or not _ID_RE.match(lid):
        raise LinkError("unknown_link", "no such connect link")
    d = _links_dir(state_dir)
    rec = _read(_rec_path(d, lid))
    if rec is None:
        raise LinkError("unknown_link", "no such connect link")
    if owner is not None and _owner(owner) != rec.get("owner"):
        raise LinkError("wrong_owner",
                        "this connect link belongs to a different owner")
    return _public(rec, now=t, state_dir=os.path.dirname(d))


# ── the document the AI receives ──────────────────────────────────────────────

def _q(s: str) -> str:
    return shlex.quote(s)


def render_script(join: dict) -> str:
    """The runnable POSIX-sh connect script (``?format=sh``, and embedded
    verbatim in the markdown). Scope: THIS machine becomes an origin of the
    link owner's Hub, and nothing else. It never uses sudo and never installs
    system packages. A missing prerequisite stops the script with the exact fix
    command."""
    hub = join["hub"]
    fp = join.get("hubFingerprint") or ""
    install = join.get("installUrl") or ""
    code = join["code"]
    exp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(join["expiresAt"]))
    up = origin_onboard.join_command(hub, code, fingerprint=fp or None,
                                     box_root='"$LOOPYARD_ROOT"')
    portal = join.get("portalUrl") or ""
    if portal and join.get("downloadToken"):
        # portal mode: always run the gated installer, a fresh box installs and
        # an installed one upgrades in place (exit 4 = loops running, kept as is)
        bundle_step = f"""say "bundle: installing / upgrading from {portal}"
{origin_onboard.portal_install_command(portal, join["downloadToken"],
                                       box_root='"$LOOPYARD_ROOT"')} || {{
  rc=$?; [ "$rc" -eq 4 ] || fail "install failed (exit $rc)"
  say "bundle: loops are running, keeping the installed version (yard update later)"
}}"""
    else:
        release_install = origin_onboard.fetched_install_command(
            'curl -fsSL "$INSTALL_URL"', '--dir "$LOOPYARD_ROOT"',
            source="your Hub")
        bundle_step = f"""if [ -x "$LOOPYARD_ROOT/bin/yard" ]; then
  say "bundle: already installed at $LOOPYARD_ROOT"
elif [ -n "$INSTALL_URL" ]; then
  say "bundle: installing from $INSTALL_URL"
  {release_install} || fail "install failed (exit $?)"
else
  fail "no Loopyard bundle at $LOOPYARD_ROOT/bin/yard and this Hub has no release URL. Install the bundle (see bundle/MACOS.md or bundle/WINDOWS-WSL2.md), then run this script again."
fi"""
    return f"""#!/bin/sh
# Loopyard one-time origin connect. Connects THIS machine as an origin of the
# Hub below, and does nothing else. The pairing code works once, until {exp}.
set -eu
LOOPYARD_ROOT="${{LOOPYARD_ROOT:-${{LOOPYARD_DIR:-$HOME/loopyard}}}}"
HUB_URL={_q(hub)}
HUB_FINGERPRINT={_q(fp)}
INSTALL_URL={_q(install)}
say()  {{ printf 'origin-connect: %s\\n' "$*"; }}
fail() {{ printf 'origin-connect: ERROR: %s\\n' "$*" >&2; exit 1; }}

# 1. OS / arch
OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS" in
  Darwin|Linux) : ;;
  *) fail "unsupported OS '$OS'. On Windows, run this inside WSL2 (Ubuntu)." ;;
esac
say "machine: $OS/$ARCH"

# 2. prerequisites (tmux + git are required; the claude CLI runs the workers)
missing=""
for t in tmux git curl; do command -v "$t" >/dev/null 2>&1 || missing="$missing $t"; done
if [ -n "$missing" ]; then
  if [ "$OS" = Darwin ]; then
    say "install first:  xcode-select --install   (git)  and  brew install tmux"
  else
    say "install first:  sudo apt-get install -y tmux git curl   (or your distro's equivalent)"
  fi
  fail "missing prerequisites:$missing"
fi
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
  say "claude CLI: found (sign in once with 'claude' if you have not)"
else
  say "WARNING: claude CLI not found; the origin connects, but cannot run agents until you install it:"
  say "  curl -fsSL https://claude.ai/install.sh | bash   then sign in once with: claude"
fi

# 3. the Loopyard bundle
{bundle_step}
[ -x "$LOOPYARD_ROOT/bin/yard" ] || fail "install did not produce $LOOPYARD_ROOT/bin/yard"

# 4. pair + connect (the pairing code goes in on stdin, never on argv)
say "connecting to $HUB_URL"
{up}

# 5. verify
if "$LOOPYARD_ROOT"/bin/yard origin status | grep -q '^origin status: connected'; then
  say "CONNECTED. This machine is now a Loopyard origin."
else
  "$LOOPYARD_ROOT"/bin/yard origin status || true
  fail "origin is not connected"
fi
"""


def render_markdown(join: dict) -> str:
    """The instruction document an AI follows: ordered steps, each with the
    exact command, plus the complete script so one fetch is enough."""
    hub = join["hub"]
    fp = join.get("hubFingerprint")
    install = join.get("installUrl")
    exp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(join["expiresAt"]))
    # One install root for every step, the same one the join line uses (R16c):
    # "${LOOPYARD_DIR:-$HOME/loopyard}", expanded by the box's own shell.
    root = origin_onboard.DEFAULT_BOX_ROOT
    root_bare = root.strip('"')
    up_line = origin_onboard.join_command(hub, join["code"], fingerprint=fp,
                                          box_root=root)
    portal = join.get("portalUrl")
    step3 = f"install the Loopyard bundle (skip if `{root_bare}/bin/yard` exists)"
    if portal and join.get("downloadToken"):
        step3 = "install or upgrade the Loopyard bundle"
        install_step = (
            "Run this even if Loopyard is already installed: it installs a "
            "fresh copy or upgrades the existing one in place (exit code 4 "
            "means loops are running and the installed version was kept; "
            "carry on with step 4).\n\n```sh\n"
            + origin_onboard.portal_install_command(
                portal, join["downloadToken"], box_root=root)
            + "\n```")
    elif install:
        install_step = ("```sh\n" + origin_onboard.fetched_install_command(
            f"curl -fsSL {_q(install)}", f"--dir {root}", source="your Hub")
            + "\n```")
    else:
        install_step = (
            f"This Hub has no release URL. If `{root_bare}/bin/yard` does not exist, "
            "stop and tell the user to install the Loopyard bundle first "
            "(bundle/MACOS.md or bundle/WINDOWS-WSL2.md).")
    script = render_script(join)
    return f"""# Connect this machine to Loopyard as an origin

You are an AI assistant. The user gave you this link so you can connect **the
machine you are running commands on** to their Loopyard Hub as an *origin* (a
computer Loopyard can run agents on). Follow the steps below exactly. Every
command is complete; you do not need anything else from the user unless a step
says so.

- **Hub:** `{hub}`
- **Hub fingerprint:** `{fp or "(none: loopback Hub)"}`
- **Pairing code expires:** {exp} (single use)
- **Scope:** these steps ONLY install Loopyard and connect this machine as an
  origin. Do not run any command that is not in this document, and do not
  fetch this link again. It worked once and is now used up.

## Fastest path: run the whole script

Save the script at the end of this document to a file and run it:

```sh
sh ./loopyard-origin-connect.sh
```

It does steps 1 to 5 below and stops with the exact fix command if a
prerequisite is missing. If you prefer to go step by step, use the steps
below instead.

## Step 1: detect the OS and architecture

```sh
uname -s; uname -m
```

`Darwin` (macOS) and `Linux` are supported. On Windows, open WSL2 (Ubuntu) and
run everything there.

## Step 2: make sure the prerequisites are installed

```sh
command -v tmux && command -v git && command -v curl
```

If any is missing, install it:
- macOS: `xcode-select --install` (git), then `brew install tmux`
- Linux / WSL2: `sudo apt-get install -y tmux git curl`

The Claude CLI runs the agents. If `command -v claude` finds nothing, install
it with `curl -fsSL https://claude.ai/install.sh | bash`. Then ask the user to
sign in once by running `claude` (this needs a browser, so the user must do
it). The origin connects without it, but it cannot run agents until the user
signs in.

## Step 3: {step3}

{install_step}

## Step 4: pair and connect (run this exact line)

```sh
{up_line}
```

The pairing code goes in on stdin, so it never appears in the process list.
The command waits until the origin reports `connected`.

## Step 5: verify

```sh
{root}/bin/yard origin status
```

Success is the line `origin status: connected`. Tell the user this machine is
connected. It now shows up on their Origins page.

## The complete script

```sh
{script}```
"""


# Chat/social link unfurlers GET a pasted URL to build a preview card. Serving
# them the real document would burn the one-time link before the AI ever sees it.
# AI fetchers (Claude-User, ChatGPT-User, curl, …) are deliberately NOT listed.
_PREVIEW_UA = re.compile(
    r"slackbot|slack-imgproxy|twitterbot|facebookexternalhit|facebot|discordbot|"
    r"telegrambot|whatsapp|linkedinbot|skypeuripreview|microsoftpreview|"
    r"iframely|embedly|redditbot|pinterest|vkshare|mattermost|google-pagerenderer|"
    r"zoombot|viber|line-poker|kakaotalk-scrap|snapchat|bitlybot|outbrain",
    re.I)


def is_preview_fetch(method: str, user_agent: Optional[str]) -> bool:
    """A request that must NOT consume the link: a HEAD probe, or a known link
    unfurler. It gets :data:`PREVIEW_BODY` (the same for every token, valid or
    not, so it is no oracle for which tokens exist)."""
    if (method or "GET").upper() != "GET":
        return True
    return bool(_PREVIEW_UA.search(user_agent or ""))


PREVIEW_BODY = """# Loopyard one-time origin-connect link

This link is meant for an AI assistant (Claude, ChatGPT, …): paste it to your AI
and ask it to connect this machine. The AI fetches it and follows the
instructions. It works once and expires; a link preview does NOT use it up.
"""


def render_refusal(err: LinkError, *, as_sh: bool) -> str:
    if as_sh:
        msg = err.message.replace("'", "")
        return (f"#!/bin/sh\necho 'origin-connect: refused ({err.code}): "
                f"{msg}' >&2\nexit 1\n")
    return (f"# Loopyard connect link refused\n\n**{err.code}**: {err.message}.\n\n"
            f"Ask the user to generate a new one-time link from the Origins "
            f"page. Do not run any other commands.\n")
