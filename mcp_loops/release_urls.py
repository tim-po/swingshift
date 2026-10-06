"""One URL rule for release assets (RELEASE-PROCESS-SPEC §5 B-1, I-1).

GitHub Releases serve a pinned asset at ``…/releases/download/v<V>/<f>`` and
the newest at ``…/releases/latest/download/<f>``; a single ``<base>/<V>/<f>``
template 404s on ``latest``. A base ending in ``/releases`` therefore gets the
GitHub layout; any other base (a ``file://`` dir, a plain static host) keeps
the flat ``<base>/<V>/<f>`` layout unchanged.

Portal mode (the invite-gated control-plane release store): a portal base
serves ``<portal>/releases/latest/<f>`` and ``<portal>/releases/v<V>/<f>``,
each GET carrying an ``Authorization: Bearer lyr_…`` download token (never a
query parameter). :func:`portal_release_urls` is that rule.

``install.sh`` carries shell twins of :func:`release_urls` and
:func:`portal_release_urls`; the pin tests run both over :data:`CASES` /
:data:`PORTAL_CASES` and require byte-equal output. Change them together.
"""

from __future__ import annotations


def release_urls(base: str, version: str) -> str:
    """The directory URL the release files of ``version`` live under (no
    trailing slash). ``version`` is ``latest``, ``X.Y.Z…`` or ``vX.Y.Z…``."""
    base = base.rstrip("/")
    if base.endswith("/releases"):
        if version == "latest":
            return f"{base}/latest/download"
        return f"{base}/download/v{version.removeprefix('v')}"
    return f"{base}/{version}"


def release_url(base: str, version: str, name: str) -> str:
    """``<release_urls(base, version)>/<name>``."""
    return f"{release_urls(base, version)}/{name}"


def install_sh_url(base: str) -> str:
    """Where the newest ``install.sh`` lives: GitHub's ``latest/download`` for a
    ``/releases`` base, else ``<base>/install.sh`` (the flat host's root)."""
    b = base.rstrip("/")
    return release_url(b, "latest", "install.sh") if b.endswith("/releases") else f"{b}/install.sh"


def portal_release_urls(portal: str, version: str) -> str:
    """The portal directory URL of ``version``'s assets (no trailing slash):
    ``<portal>/releases/latest`` or ``<portal>/releases/v<X.Y.Z…>``."""
    portal = portal.rstrip("/")
    if version == "latest":
        return f"{portal}/releases/latest"
    return f"{portal}/releases/v{version.removeprefix('v')}"


def portal_release_url(portal: str, version: str, name: str) -> str:
    """``<portal_release_urls(portal, version)>/<name>``."""
    return f"{portal_release_urls(portal, version)}/{name}"


def portal_install_sh_url(portal: str) -> str:
    """The gated newest ``install.sh`` on a portal."""
    return portal_release_url(portal, "latest", "install.sh")


GH = "https://github.com/tim-po/loopyard-releases/releases"

# (base, version, release_urls(base, version)) — shared with the install.sh twin.
CASES: tuple[tuple[str, str, str], ...] = (
    (GH, "latest", f"{GH}/latest/download"),
    (GH + "/", "latest", f"{GH}/latest/download"),
    (GH, "0.1.0", f"{GH}/download/v0.1.0"),
    (GH, "v0.1.0", f"{GH}/download/v0.1.0"),
    (GH + "/", "v0.1.0", f"{GH}/download/v0.1.0"),
    (GH, "0.2.0-rc.1", f"{GH}/download/v0.2.0-rc.1"),
    (GH, "0.1.0-dev+g0123456789ab", f"{GH}/download/v0.1.0-dev+g0123456789ab"),
    ("file:///tmp/rel", "latest", "file:///tmp/rel/latest"),
    ("file:///tmp/rel/", "0.1.0", "file:///tmp/rel/0.1.0"),
    ("file:///tmp/rel", "v0.1.0", "file:///tmp/rel/v0.1.0"),
    ("https://dl.example.com/loopyard", "latest", "https://dl.example.com/loopyard/latest"),
    ("https://dl.example.com/releases-mirror", "0.1.0",
     "https://dl.example.com/releases-mirror/0.1.0"),
)

PORTAL = "https://portal.loopyard.example"

# (portal, version, portal_release_urls(portal, version)) — shared with the twin.
PORTAL_CASES: tuple[tuple[str, str, str], ...] = (
    (PORTAL, "latest", f"{PORTAL}/releases/latest"),
    (PORTAL + "/", "latest", f"{PORTAL}/releases/latest"),
    (PORTAL + "///", "latest", f"{PORTAL}/releases/latest"),
    (PORTAL, "0.1.0", f"{PORTAL}/releases/v0.1.0"),
    (PORTAL, "v0.1.0", f"{PORTAL}/releases/v0.1.0"),
    (PORTAL, "0.2.0-rc.1", f"{PORTAL}/releases/v0.2.0-rc.1"),
    ("http://127.0.0.1:18431", "1.2.3", "http://127.0.0.1:18431/releases/v1.2.3"),
    ("https://cp.example.com/portal/", "latest", "https://cp.example.com/portal/releases/latest"),
)
