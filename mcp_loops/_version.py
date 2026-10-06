"""The one Loopyard version (RELEASE-PROCESS-SPEC §1.1).

``master`` carries the *next* release as ``X.Y.Z-dev``; a tag ``vX.Y.Z`` is
cut by ``scripts/release/bump.py``, the only writer of this literal. The
release workflow's coherence check reads it with ``sed`` (no import), so keep
it a single plain string assignment.
"""

__version__ = "0.1.0-dev"

# On-disk state format (R17 / §1.3.3): an integer, bumped only by a release
# that changes data/_loops, config/ or _origin/ in a way older code can't read.
STATE_VERSION = 1


def bundle_meta():
    """The install's ``BUNDLE.json`` (a tarball install) as a dict, else None
    (a source checkout). An unreadable file counts as a bundle with no fields."""
    import json
    from mcp_loops import paths
    bundle = paths.install_root() / "BUNDLE.json"
    if not bundle.is_file():
        return None
    try:
        meta = json.loads(bundle.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    return meta if isinstance(meta, dict) else {}


def version_info() -> dict:
    """``GET /api/version`` (B-3, §1.2): the running engine's stamp — from
    ``BUNDLE.json`` when present, else ``__version__`` (a source checkout) —
    plus the install/state roots the I-5 adopt guard compares (B-10)."""
    from mcp_loops import paths
    from mcp_loops.origin_proto.wire import PROTOCOL_VERSION
    meta = bundle_meta()
    src = meta if meta is not None else {}
    return {
        "version": str(src.get("version") or __version__),
        "gitSha": src.get("gitSha"),
        "target": src.get("target"),
        "protocol": PROTOCOL_VERSION,
        "stateVersion": int(src.get("stateVersion") or STATE_VERSION),
        "source": meta is None,
        "installRoot": str(paths.install_root()),
        "stateRoot": str(paths.state_root()),
    }
