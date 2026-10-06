"""Build an immutable local portal release; never uploads anything.

Run: python -m scripts.release.publish_local --store DIR --version X.Y.Z
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from control_plane.releases import VERSION, release_path

DEFAULT_TARGETS = ('linux-x86_64', 'linux-aarch64', 'macos-arm64')
INSTALL_END = '# loopyard-install-end'   # install.sh's last line (connect-line completeness check)
_URL_CHARS = re.compile(r'[A-Za-z0-9._~:/%+=,-]+')


def portal_default_url(url):
    """The portal URL to bake into install.sh's PORTAL_DEFAULT_URL: https://,
    or http:// only for 127.0.0.1/localhost (install.sh still demands
    LOOPYARD_ALLOW_INSECURE=1 for that at run time); a bare origin/path with no
    userinfo, query, fragment or /releases suffix, and only characters that are
    inert inside the single-quoted shell literal."""
    from urllib.parse import urlsplit
    url = (url or '').strip().rstrip('/')
    if not _URL_CHARS.fullmatch(url):
        raise ValueError('invalid --portal-url: unexpected characters')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' and not (
            parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')):
        raise ValueError('--portal-url must be https:// (http:// only for 127.0.0.1/localhost)')
    try:
        parsed.port
    except ValueError:
        raise ValueError('invalid --portal-url: bad port') from None
    if not parsed.hostname or '@' in parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError('invalid --portal-url')
    if parsed.path.endswith('/releases'):
        raise ValueError('--portal-url is the portal base URL, without /releases')
    return url


def bake_installer(installer, public_key, portal_url=None):
    """install.sh text with the release key (and portal URL) pinned."""
    installer, count = re.subn(r"^RELEASE_PUBKEY='[^'\n]*'$",
                               "RELEASE_PUBKEY='" + public_key + "'",
                               installer, flags=re.MULTILINE)
    if count != 1:
        raise ValueError('install.sh must contain exactly one release public key pin')
    installer, count = re.subn(r"^PORTAL_DEFAULT_URL='[^'\n]*'$",
                               "PORTAL_DEFAULT_URL='" + (portal_url or '') + "'",
                               installer, flags=re.MULTILINE)
    if count != 1:
        raise ValueError('install.sh must contain exactly one PORTAL_DEFAULT_URL pin')
    # exactly the connect-line gate: last byte a newline, last line the sentinel
    if not installer.endswith('\n' + INSTALL_END + '\n'):
        raise ValueError('install.sh must end with ' + INSTALL_END + ' as its newline-terminated last line')
    return installer


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def publish(store, version, *, targets=DEFAULT_TARGETS, repo=None, builder=None,
            web_dist=None, signing_key=None, portal_url=None):
    from mcp_loops.origin_bundle import (build, target_for, signing_public_key,
                                         sign_artifact, verify_artifact)
    if not VERSION.fullmatch(version):
        raise ValueError('invalid version')
    version = version.removeprefix('v')
    targets = sorted(set(targets))
    if not targets:
        raise ValueError('targets required')
    for target in targets:
        target_for(target)
    if portal_url:
        portal_url = portal_default_url(portal_url)
    signing_key = signing_key or os.environ.get('LOOPYARD_RELEASE_SIGNING_KEY')
    if not signing_key:
        raise ValueError('LOOPYARD_RELEASE_SIGNING_KEY or --signing-key is required; '
                         'production keys must be supplied by the release owner')
    public_key = signing_public_key(signing_key)
    root = Path(store).resolve()
    root.mkdir(parents=True, exist_ok=True)
    repo = Path(repo or Path(__file__).resolve().parents[2])
    # A release MUST ship the real /app UI (the same dashboard used daily), not
    # the legacy fallback. Default to the repo's built dist; refuse a real build
    # with none rather than silently ship a lesser product.
    if builder is None:
        if web_dist is None:
            cand = repo / 'frontend' / 'apps' / 'web' / 'dist'
            web_dist = str(cand) if (cand / 'index.html').is_file() else None
        if not web_dist:
            raise ValueError('no web dist to ship: build frontend/apps/web '
                             '(npm run build) or pass --web-dist=DIR; a release '
                             'must include the /app UI')
    destination = root / ('v' + version)
    if destination.exists():
        if destination.is_symlink():
            raise ValueError('symlink release')
        manifest = json.loads(release_path(root, version, 'release.json').read_text())
        if manifest['version'] != version or manifest['targets'] != targets:
            raise ValueError('existing release differs')
        required = {'install.sh'}
        for target in targets:
            name = f'loopyard-origin-{target}.tar.gz'
            required.update((name, name + '.sha256', name + '.sig'))
        if not required <= manifest['assets'].keys():
            raise ValueError('existing manifest is incomplete')
        for name, metadata in manifest['assets'].items():
            asset = release_path(root, version, name)
            if digest(asset) != metadata['sha256'] or asset.stat().st_size != metadata['size']:
                raise ValueError('existing release is corrupt')
        expected_pin = "RELEASE_PUBKEY='" + public_key + "'"
        published = (destination / 'install.sh').read_text().splitlines()
        if expected_pin not in published:
            raise ValueError('existing release signing key differs')
        if "PORTAL_DEFAULT_URL='" + (portal_url or '') + "'" not in published:
            raise ValueError('existing release portal URL differs')
        for target in targets:
            if not verify_artifact(destination / f'loopyard-origin-{target}.tar.gz', public_key):
                raise ValueError('existing release signature is invalid')
        # Check the checksum manifest as well, including release.json itself.
        checksums = {}
        for line in release_path(root, version, 'SHA256SUMS').read_text().splitlines():
            expected, name = line.split('  ', 1)
            if name in checksums:
                raise ValueError('duplicate checksum asset')
            checksums[name] = expected
        if checksums.keys() != manifest['assets'].keys() | {'release.json'}:
            raise ValueError('existing checksums are incomplete')
        for name, expected in checksums.items():
            if digest(release_path(root, version, name)) != expected:
                raise ValueError('existing checksums are corrupt')
    else:
        with tempfile.TemporaryDirectory(prefix='.publish-', dir=root) as temporary:
            stage = Path(temporary)
            for target in targets:
                (builder or build)(target, stage, version=version, repo=repo,
                                   web_dist=web_dist, signing_key=signing_key)
                # Bundle naming is stable across the local/CI publishers.
                name = f'loopyard-origin-{target}.tar.gz'
                asset = stage / name
                sign_artifact(asset, signing_key)
                (stage / (name + '.sha256')).write_text(f'{digest(asset)}  {name}\n')
            installer = bake_installer((repo / 'install.sh').read_text(), public_key, portal_url)
            (stage / 'install.sh').write_text(installer)
            assets = {p.name: {'sha256': digest(p), 'size': p.stat().st_size}
                      for p in sorted(stage.iterdir()) if p.is_file()}
            manifest = {'version': version, 'created_at': int(time.time()),
                        'targets': targets, 'assets': assets}
            (stage / 'release.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
            (stage / 'SHA256SUMS').write_text(''.join(
                f'{digest(p)}  {p.name}\n' for p in sorted(stage.iterdir()) if p.is_file()))
            stage.rename(destination)
    fd, pointer = tempfile.mkstemp(prefix='.latest-', dir=root)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write('v' + version + '\n')
        os.replace(pointer, root / 'latest')
    finally:
        if os.path.exists(pointer):
            os.unlink(pointer)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', default=os.environ.get('LOOPYARD_RELEASE_STORE'))
    parser.add_argument('--version', required=True)
    parser.add_argument('--signing-key', default=os.environ.get('LOOPYARD_RELEASE_SIGNING_KEY'),
                        help='owner-provided Ed25519 private key, or .pub with ssh-agent')
    parser.add_argument('--target', action='append')
    parser.add_argument('--web-dist', default=os.environ.get('LOOPYARD_WEB_DIST'),
                        help='built frontend/apps/web dist/ to serve at /app/ '
                             '(default: <repo>/frontend/apps/web/dist)')
    parser.add_argument('--portal-url',
                        help='portal base URL baked into install.sh as PORTAL_DEFAULT_URL '
                             '(https://; http only for 127.0.0.1/localhost)')
    args = parser.parse_args()
    if not args.store:
        parser.error('--store or LOOPYARD_RELEASE_STORE is required')
    print(publish(args.store, args.version, targets=args.target or DEFAULT_TARGETS,
                  web_dist=args.web_dist, signing_key=args.signing_key,
                  portal_url=args.portal_url))


if __name__ == '__main__':
    main()
