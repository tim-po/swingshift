"""origin_onboard — the Hub-side half of one-command onboarding (HUB-FABRIC P2.5).

The owner mints a pairing code on the Hub and gets back the exact line to paste
on the new box. There is no new auth model: the code is the existing single-use,
short-lived :meth:`EnrollmentStore.issue_pairing_code` record (P6: it carries the
issuing owner). The line also carries the Hub cert fingerprint, the out-of-band
trust root ``origin up --hub-fingerprint`` verifies before pinning (§5.1(4)(b)).

The store is file-based, so minting works from any process that can see the
running Hub's ``--state-dir``. The Hub does not have to be restarted.

Surfaces: ``yard hub pair-code`` (CLI) and the ``origin_pair_code`` MCP tool.
Both call :func:`mint_join`.

Besides the terminal line, a mint also returns a ``loopyard://join?…`` link
(:func:`join_link`). The desktop app parses it into its pairing form (hub URL,
fingerprint, code), so a new user never has to handle a CLI command.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from typing import Optional
from urllib.parse import parse_qs, urlencode

#: where the engine's own Hub state lives when none is named (== hub_serve's).
HUB_STATE_ENV = "LOOPYARD_HUB_STATE_DIR"
#: the URL a remote box dials (the Hub's public wss:// address).
HUB_PUBLIC_URL_ENV = "LOOPYARD_HUB_PUBLIC_URL"
#: where the box fetches install.sh (optional; omitted = join line only).
RELEASE_URL_ENV = "LOOPYARD_RELEASE_URL"
#: the invite-gated portal release store (install.sh portal mode). A join in
#: portal mode also carries an account-bound ``lyr_`` download token.
PORTAL_URL_ENV = "LOOPYARD_PORTAL_URL"
#: the portal enrollment ``yard enroll`` saved in the Hub's state dir
#: (``{portal_url, hub_id, device_token, ...}``); its hub-bound ``lyd_`` token
#: buys the download token a portal-mode join carries.
ENROLL_FILE = "enroll.json"
#: the portal route a hub-bound device token mints a ``lyr_`` at
#: (control_plane.onboard.hub_download_token).
HUB_DOWNLOAD_TOKEN_PATH = "/api/hubs/{hub_id}/download-token"

#: the box root as a shell expression the box expands (R16c): the box's own
#: ``$LOOPYARD_DIR`` when set, else ``~/loopyard`` (== install.sh's default), so
#: a box installed elsewhere joins with that install instead of a second one.
DEFAULT_BOX_ROOT = '"${LOOPYARD_DIR:-$HOME/loopyard}"'

#: the scheme of the one-paste join link (== the desktop app's JOIN_LINK_PREFIX
#: in frontend/apps/desktop/originAgent.mjs).
JOIN_LINK_PREFIX = "loopyard://join"
_INSTALL_URL_RE = re.compile(r"^https://\S+/install\.sh$")
_DOWNLOAD_TOKEN_RE = re.compile(r"^lyr_[A-Za-z0-9_-]{1,196}$")
_PORTAL_URL_RE = re.compile(r"^https?://[^\s/?#@]+(/[^\s?#]*)?$")


class MintError(ValueError):
    """A refusal to mint, with a stable ``code`` the CLI/MCP surface verbatim."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_state_dir(state_dir: Optional[str] = None) -> str:
    from mcp_loops import hub_serve
    return (state_dir or os.environ.get(HUB_STATE_ENV)
            or hub_serve.default_state_dir())


def _hub_cert_path(state_dir: str) -> str:
    from mcp_loops import hub_serve
    return os.path.join(state_dir, "_identity", hub_serve.HUB_CERT_FILENAME)


#: install.sh's last line. A downloaded copy runs only when this is still its
#: last line, so a body cut at a statement boundary (which passes ``sh -n``,
#: and which curl cannot catch without a Content-Length) is refused too.
INSTALL_SENTINEL = "# loopyard-install-end"

#: what a failed/garbled install.sh download prints (nothing ran, nothing staged).
_FETCH_FAILED_MSG = ("loopyard: could not download install.sh (the link may have "
                     "expired, or the network failed). Nothing was installed. "
                     "Re-copy the command from {source} and run it again.")
_FETCH_BAD_MSG = ("loopyard: the downloaded install.sh is incomplete or corrupt. "
                  "Nothing was installed. Re-copy the command from {source} and "
                  "run it again.")


def fetched_install_command(fetch: str, args: str, *, env: str = "",
                            source: str = "the portal") -> str:
    """Download install.sh, verify it, THEN run it; never ``curl | sh``.

    ``curl … | sh`` swallows a failed fetch: an expired token/401/404 gives sh
    an empty stdin, sh exits 0 and whatever is chained after the install runs
    as if it had worked. Here ``fetch`` (a ``curl -fsS …`` without an output
    option) writes to a mktemp file; a non-zero curl (HTTP error, network, a
    body shorter than its Content-Length) aborts with an actionable message.
    The file must then be non-empty, start with ``#!``, end in a newline,
    end with the :data:`INSTALL_SENTINEL` line and pass ``sh -n``, else it is
    refused (a truncated body). Only then
    ``env sh <file> args`` runs; its exit code is passed through, so a
    caller's ``|| [ $? -eq 4 ]`` still sees install.sh's own 4. The tempfile
    is removed on every path.

    A subshell, so the ``exit``s never close the user's own terminal and the
    tempfile variable does not leak into it."""
    q = shlex.quote
    bad = f"rm -f \"$f\"; echo {q(_FETCH_BAD_MSG.format(source=source))} >&2; exit 1"
    run = f"{env} sh" if env else "sh"
    return ("( f=\"$(mktemp \"${TMPDIR:-/tmp}/loopyard-install.XXXXXX\")\" || exit 1; "
            f"{fetch} -o \"$f\" || {{ rm -f \"$f\"; "
            f"echo {q(_FETCH_FAILED_MSG.format(source=source))} >&2; exit 1; }}; "
            "if [ -s \"$f\" ] && [ -z \"$(tail -c 1 \"$f\")\" ] "
            "&& head -n 1 \"$f\" | grep -q '^#!' "
            f"&& [ \"$(tail -n 1 \"$f\")\" = {q(INSTALL_SENTINEL)} ] "
            "&& sh -n \"$f\" 2>/dev/null; "
            f"then :; else {bad}; fi; "
            f"{run} \"$f\"{' ' + args if args else ''}; rc=$?; rm -f \"$f\"; exit $rc )")


def portal_install_command(portal_url: str, download_token: str, *,
                           box_root: str = DEFAULT_BOX_ROOT) -> str:
    """The portal-mode install/upgrade half of a join line: fetch the gated
    ``install.sh`` with the ``lyr_`` download token as a Bearer header (no
    ``-L``: a redirect must not see the token) and run it with
    ``LOOPYARD_PORTAL_URL`` + ``LOOPYARD_INSTALL_TOKEN`` set, so every asset
    fetch is gated and verified and nothing has to be passed by hand.

    No ``--keep-existing``: re-running on an installed box is an in-place
    upgrade (already current exits 0). No ``LOOPYARD_ENROLL_TOKEN``, so
    install.sh never runs ``yard enroll`` (which would bring up a local Hub)."""
    from mcp_loops.release_urls import portal_install_sh_url
    portal = (portal_url or "").strip()
    if not _PORTAL_URL_RE.match(portal):
        raise ValueError("portal URL must be an http(s):// URL")
    if not _DOWNLOAD_TOKEN_RE.match(download_token or ""):
        raise ValueError("a portal join needs a lyr_ download token")
    q = shlex.quote
    fetch = (f"curl -fsS -H {q('Authorization: Bearer ' + download_token)} "
             f"{q(portal_install_sh_url(portal))}")
    env = (f"LOOPYARD_PORTAL_URL={q(portal.rstrip('/'))} "
           f"LOOPYARD_INSTALL_TOKEN={q(download_token)}")
    return fetched_install_command(fetch, f"--dir {box_root.rstrip('/')}", env=env)


def join_command(hub_url: str, code: str, *, fingerprint: Optional[str],
                 install_url: Optional[str] = None,
                 box_root: str = DEFAULT_BOX_ROOT,
                 portal_url: Optional[str] = None,
                 download_token: Optional[str] = None) -> str:
    """The line the owner pastes on the new box. The code goes in on stdin
    (``--pair-code -``), never on argv (P5). ``printf`` is a shell builtin, so the
    code never shows up in ``ps``.

    ``box_root`` is pasted verbatim (a shell expression, see
    :data:`DEFAULT_BOX_ROOT`) as both the ``install.sh --dir`` and the ``yard``
    path.

    Portal mode (``portal_url`` + ``download_token``, wins over
    ``install_url``): :func:`portal_install_command`, then the join. The install
    installs a fresh box or upgrades an existing one in place; its exit 4
    (loops still running, nothing swapped) does not block the join, any other
    failure does. The join is ``yard origin up`` only: it dials the Hub and
    never starts a local one.

    Release-URL mode (``install_url``): the install runs with
    ``--keep-existing`` (R16a): a box that already has a bundle keeps it,
    whatever its version, and the join proceeds."""
    yard = f"{box_root.rstrip('/')}/bin/yard"
    join = [yard, "origin", "up", "--hub", hub_url]
    if fingerprint:
        join += ["--hub-fingerprint", fingerprint]
    join += ["--pair-code", "-"]
    # the root has to stay unquoted here so the box's shell expands it
    join_s = " ".join(shlex.quote(t) if i else t for i, t in enumerate(join))
    line = f"printf '%s\\n' {shlex.quote(code)} | {join_s}"
    if portal_url or download_token:
        install = portal_install_command(portal_url or "", download_token or "",
                                         box_root=box_root)
        return f"{{ {install} || [ $? -eq 4 ]; }} && {line}"
    if install_url:
        install = fetched_install_command(
            f"curl -fsSL {shlex.quote(install_url)}",
            f"--keep-existing --dir {box_root.rstrip('/')}", source="your Hub")
        line = f"{install} && {line}"
    return line


def portal_download_token(state_dir: Optional[str] = None, *,
                          post=None) -> Optional[dict]:
    """``{portalUrl, downloadToken, expiresAt}`` for a portal-mode join, or
    ``None`` when this Hub is not portal-enrolled (no ``enroll.json``) or the
    portal refuses/is unreachable (the join then falls back to the release URL).

    The Hub trades its hub-bound ``lyd_`` device token (saved by ``yard
    enroll``) for a short-lived, account-bound ``lyr_`` at
    ``POST <portal>/api/hubs/<hub_id>/download-token``. ``post(url, bearer)``
    is the HTTP seam (returns the decoded JSON body)."""
    import json
    path = os.path.join(resolve_state_dir(state_dir), ENROLL_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        portal = str(cfg["portal_url"]).rstrip("/")
        hub_id, device = str(cfg["hub_id"]), str(cfg["device_token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (not _PORTAL_URL_RE.match(portal) or not device.startswith("lyd_")
            or not re.match(r"^[A-Za-z0-9_-]{1,128}$", hub_id)):
        return None
    url = portal + HUB_DOWNLOAD_TOKEN_PATH.format(hub_id=hub_id)
    try:
        out = (post or _post_bearer)(url, device)
        tok = out["download_token"]
    except Exception:  # noqa: BLE001 — never block a join on the portal
        return None
    if not isinstance(tok, str) or not _DOWNLOAD_TOKEN_RE.match(tok):
        return None
    return {"portalUrl": portal, "downloadToken": tok,
            "expiresAt": out.get("expires_at")}


def _post_bearer(url: str, bearer: str, timeout: float = 10.0) -> dict:
    import json
    import urllib.request
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "User-Agent": "Loopyard/0.1",
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json"})
    # no redirects are followed with the device token (urllib would): refuse them
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def join_link(hub_url: str, *, code: Optional[str] = None,
              fingerprint: Optional[str] = None,
              install_url: Optional[str] = None) -> str:
    """The one-paste join link for the desktop app:
    ``loopyard://join?hub=<url>[&fp=<fingerprint>][&code=<code>][&install=<url>]``,
    with every value form-encoded. This is the same shape the app's
    ``buildJoinLink`` emits and its ``parseJoinLink`` reads. The app only extracts
    the fields; it never executes anything carried in the link."""
    if not hub_url:
        raise ValueError("a hub URL is required")
    q = [("hub", hub_url)]
    if fingerprint:
        q.append(("fp", fingerprint))
    if code:
        q.append(("code", code))
    if install_url:
        q.append(("install", install_url))
    return f"{JOIN_LINK_PREFIX}?{urlencode(q)}"


def parse_join_link(text: str) -> dict:
    """Inverse of :func:`join_link`, with the same rules as the desktop app's
    ``parseJoinLink``. Returns ``{hub, hubFingerprint, pairCode, installUrl}``.
    ``installUrl`` is dropped unless it is an ``https://…/install.sh`` URL.
    Raises ``ValueError`` when the text is not a join link or has no ws(s):// hub."""
    t = (text or "").strip()
    if not t.lower().startswith(JOIN_LINK_PREFIX):
        raise ValueError("not a Loopyard join link")
    rest = t[len(JOIN_LINK_PREFIX):]
    if rest.startswith("/"):
        rest = rest[1:]
    query = rest[1:] if rest.startswith("?") else rest.partition("?")[2]
    params = parse_qs(query, keep_blank_values=True)

    def q(k: str) -> Optional[str]:
        v = (params.get(k) or [""])[0].strip()
        return v or None

    out = {"hub": q("hub"), "hubFingerprint": q("fp"), "pairCode": q("code"),
           "installUrl": None}
    inst = q("install")
    if inst and _INSTALL_URL_RE.match(inst):
        out["installUrl"] = inst
    if not out["hub"]:
        raise ValueError("join link has no hub")
    if not re.match(r"^wss?://", out["hub"], re.I):
        raise ValueError("join link hub must be a ws:// or wss:// URL")
    return out


def resolve_hub(hub_url: Optional[str] = None,
                state_dir: Optional[str] = None) -> tuple:
    """``(hub, fingerprint, root)`` for a mint: the parsed Hub URL, the Hub cert
    fingerprint (``None`` for a loopback ``ws://`` Hub) and the resolved state
    dir. Raises :class:`MintError` with the same codes :func:`mint_join`
    documents. Split out so the one-time connect link can validate a Hub at
    link-mint time, before any pairing code exists."""
    from mcp_loops import origin_client
    from mcp_loops.origin_proto import tls as _tls

    url = (hub_url or "").strip()
    if not url:
        # the always-on Hub (mcp_loops.hub_record): this machine's
        # $LOOPYARD_HUB_PUBLIC_URL, unless the owner pointed the Hub elsewhere
        from mcp_loops import hub_record
        cur = hub_record.current_hub(state_dir)
        if cur["mode"] == hub_record.MODE_REMOTE:
            raise MintError("hub_is_remote",
                            f"the Hub is {cur.get('host') or cur.get('publicUrl')}"
                            f", not this machine: pairing codes are minted on "
                            f"the Hub, so generate the link there")
        url = (cur.get("publicUrl") or os.environ.get(HUB_PUBLIC_URL_ENV)
               or "").strip()
    if not url:
        raise MintError("hub_url_required",
                        f"pass --hub-url wss://HOST:PORT (or set "
                        f"${HUB_PUBLIC_URL_ENV}): the address the new box dials")
    try:
        hub = origin_client.parse_hub_url(url)
    except origin_client.OriginUpError as e:
        raise MintError(e.code, e.message) from e
    if not hub.secure and not hub.loopback:
        raise MintError("insecure_hub_url",
                        f"{hub.describe()} is plaintext and not loopback; a box "
                        f"refuses to pair over it. Serve the Hub with --tls and "
                        f"use wss://")
    root = resolve_state_dir(state_dir)
    fingerprint = None
    if hub.secure or hub.tunneled:   # a /hub tunnel always carries the Hub's TLS
        cert_path = _hub_cert_path(root)
        try:
            with open(cert_path, "rb") as fh:
                cert_pem = fh.read()
        except OSError:
            raise MintError("hub_not_tls",
                            f"no Hub cert at {cert_path}: start the Hub with "
                            f"`python -m mcp_loops.hub_serve --tls --state-dir "
                            f"{root}` first") from None
        fingerprint = _tls.cert_fingerprint(cert_pem)
    return hub, fingerprint, root


def is_release_version(version: str) -> bool:
    """A published release (``X.Y.Z``, ``X.Y.Z-rc.N``), not a dev build:
    ``X.Y.Z-dev`` or an untagged ``…+g<sha12>`` (R2) has no release assets."""
    v = (version or "").strip()
    return bool(v) and "+" not in v and "-dev" not in v


def hub_version() -> str:
    """The version of the code minting the line (the Hub's own install):
    ``BUNDLE.json.version``, else ``__version__`` for a source checkout."""
    from mcp_loops import _version
    meta = _version.bundle_meta()
    return str((meta or {}).get("version") or _version.__version__)


def resolve_install_url(install_url: Optional[str] = None, *,
                        version: Optional[str] = None) -> Optional[str]:
    """The install.sh URL a join fetches: the argument, else
    ``$LOOPYARD_RELEASE_URL``. A full ``…/install.sh`` URL is used as given. A
    GitHub ``…/releases`` base is pinned to the Hub's release (D5):
    ``<base>/download/v<version>/install.sh``; a dev Hub (``-dev`` / ``+g``)
    has no such asset and gets ``<base>/latest/download/install.sh``. Any other
    base keeps ``<base>/install.sh``. ``version`` defaults to :func:`hub_version`."""
    from mcp_loops.release_urls import install_sh_url, release_url
    install = install_url if install_url is not None \
        else os.environ.get(RELEASE_URL_ENV)
    if not install or install.endswith(".sh"):
        return install or None
    base = install.rstrip("/")
    if base.endswith("/releases"):
        v = version if version is not None else hub_version()
        if is_release_version(v):
            return release_url(base, v, "install.sh")
    return install_sh_url(base)


def mint_join(*, hub_url: Optional[str] = None, state_dir: Optional[str] = None,
              owner: Optional[str] = None, label: Optional[str] = None,
              install_url: Optional[str] = None,
              box_root: str = DEFAULT_BOX_ROOT,
              portal_url: Optional[str] = None,
              download_token: Optional[str] = None,
              portal: Optional[bool] = None,
              now: Optional[float] = None) -> dict:
    """Mint a pairing code in the Hub's store and return the paste line.

    The line is portal-mode (:func:`join_command`) when ``portal_url`` +
    ``download_token`` are given, else — unless ``portal=False`` — when this
    Hub is portal-enrolled and the portal hands it a download token
    (:func:`portal_download_token`). Otherwise it uses the release URL.

    Refuses (``MintError``) rather than hand out a line that cannot work:

    * ``hub_url_required``: no URL (argument or ``$LOOPYARD_HUB_PUBLIC_URL``);
    * ``insecure_hub_url``: a non-loopback ``ws://`` URL, which the box refuses to
      pair over;
    * ``hub_not_tls``: a ``wss://`` URL, but no ``--tls`` Hub has run over this
      state dir yet, so there is no cert and no fingerprint to hand out.
    """
    from mcp_loops.origin_proto.enrollment import EnrollmentStore

    hub, fingerprint, root = resolve_hub(hub_url, state_dir)
    store = EnrollmentStore(os.path.join(root, "enroll"))
    rec = store.issue_pairing_code(now=now if now is not None else time.time(),
                                   label=label, owner=owner)
    install = resolve_install_url(install_url)
    if not (portal_url and download_token) and portal is not False:
        got = portal_download_token(root)
        if got:
            portal_url, download_token = got["portalUrl"], got["downloadToken"]
    if not (portal_url and download_token):
        portal_url = download_token = None
    return {
        "code": rec["code"],
        "owner": rec["owner"],
        "label": rec["label"],
        "expiresAt": rec["expiresAt"],
        "hub": hub.describe(),
        "hubFingerprint": fingerprint,
        "stateDir": root,
        "portalUrl": portal_url,
        "downloadToken": download_token,
        "command": join_command(hub.describe(), rec["code"],
                                fingerprint=fingerprint, install_url=install,
                                box_root=box_root, portal_url=portal_url,
                                download_token=download_token),
        "joinLink": join_link(hub.describe(), code=rec["code"],
                              fingerprint=fingerprint, install_url=install),
    }
