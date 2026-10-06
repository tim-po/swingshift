"""origin_policy — P3: the per-origin CAPABILITY MANIFEST + its policy check.

Each origin advertises exactly what it permits, in a small versioned manifest
that rides the channel ``hello`` frame on every connect and is stored Hub-side
(``EnrollmentStore`` sidecar, only after the device authenticated). The SAME
:meth:`OriginManifest.check` is run twice (defence in depth):

  * Hub side, in ``ChannelServer.call_rpc`` — against the manifest the target
    advertised on its CURRENT session; a unit it did not advertise is refused
    before any frame leaves the Hub;
  * origin side, in ``ChannelClient._handle_rpc`` — against the origin's OWN
    manifest (read from its own disk), so a Hub that skipped its check (bug,
    bypass, raw frame) is still refused.

Manifest v1 (JSON)::

    {"manifestVersion": 1,
     "exec":    false,   # origin.run with an arbitrary argv   — DEFAULT OFF
     "fsRead":  false,   # fs.pull  (origin.run ["cat","--",p]) — DEFAULT OFF
     "fsWrite": false,   # fs.push  (origin.run ["tee","--",p]) — DEFAULT OFF
     "agent":   true,    # loop.start / loop.stop (ProjectAllowlist still applies)
     "rpcs":    ["loop.list","loop.get","loop.status","products.list","origin.describe"],
     "owner":   null}    # optional ORIGIN-SIDE owner pin; the Hub never trusts it

Rules (default-deny):
  * ``exec`` is off unless the origin's own manifest file says ``true`` — and even
    then the origin's ``RunAllowlist`` must admit the argv (two locks).
  * ``fsRead`` / ``fsWrite`` are their OWN flags (an origin may permit fs.read
    without exec): they admit only the exact fs_ops argv shape, no ``env``, no
    stdin for a read — and the RunAllowlist must still admit cat/tee (so any
    argv-level path rules the owner set keep binding).
  * ``origin.describe`` is always answerable (the Hub must be able to see what an
    origin is, e.g. for onboarding) — it runs nothing.
  * A Hub that receives NO manifest (a pre-P3 origin), a malformed one, or an
    unsupported ``manifestVersion`` stores :func:`hub_fallback` — describe-only.
  * ``owner`` is advisory to the Hub: the Hub derives ownership ONLY from the
    enrollment record. The origin uses it to pin which owner's units it accepts.

Pure (no asyncio, no Hub/engine imports) so both ends and the wire layer can use
it without crossing the package boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Optional

MANIFEST_VERSION = 1
MANIFEST_FILENAME = "origin-manifest.json"

# verb vocabulary (same strings as mcp_loops.audit, duplicated here so this module
# stays import-free; test_origin_policy pins them equal).
V_EXEC = "exec"
V_FS_READ = "fs.read"
V_FS_WRITE = "fs.write"
V_AGENT_START = "agent.start"
V_AGENT_STOP = "agent.stop"
RPC_PREFIX = "rpc:"

RUN_VERBS = frozenset({V_EXEC, V_FS_READ, V_FS_WRITE})

# the named read RPCs an origin may advertise (subset of wire.RPC_METHODS)
READ_RPCS = ("loop.list", "loop.get", "loop.status", "products.list",
             "origin.describe", "capabilities.probe")
ALWAYS_RPCS = frozenset({"origin.describe"})
AGENT_METHODS = {"loop.start": V_AGENT_START, "loop.stop": V_AGENT_STOP}

# typed refusal codes (mirrored as wire.E_NOT_PERMITTED / wire.E_NOT_OWNER)
E_NOT_PERMITTED = "not_permitted"
E_NOT_OWNER = "not_owner"

# manifest receipt status (Hub side)
M_OK = "ok"
M_MISSING = "missing"
M_INVALID = "invalid"
M_UNSUPPORTED = "unsupported_version"


class PolicyDenied(PermissionError):
    """A unit the manifest (or owner pin) does not permit. ``code`` is one of
    :data:`E_NOT_PERMITTED` / :data:`E_NOT_OWNER`."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def verb_for(method: str, verb: Optional[str] = None) -> str:
    """Canonical verb of a unit. ``origin.run`` may be tagged fs.read/fs.write by
    its caller; anything else is DERIVED from the method (a caller cannot relabel
    ``loop.start`` as a read)."""
    if method == "origin.run":
        return verb if verb in RUN_VERBS else V_EXEC
    if method in AGENT_METHODS:
        return AGENT_METHODS[method]
    return RPC_PREFIX + str(method)


def verb_mismatch(method: str, verb: Optional[str]) -> Optional[str]:
    """Reason string if an explicit ``verb`` tag is inconsistent with ``method``."""
    if verb is None:
        return None
    if verb != verb_for(method, verb):
        return f"verb {verb!r} is not valid for method {method!r}"
    return None


def _fs_shape(verb: str, params: dict) -> Optional[str]:
    """fs.read/fs.write admit ONLY the fs_ops argv shape — no smuggled exec."""
    argv = params.get("argv")
    prog = "cat" if verb == V_FS_READ else "tee"
    if (not isinstance(argv, (list, tuple)) or len(argv) != 3
            or argv[0] != prog or argv[1] != "--"
            or not isinstance(argv[2], str) or not argv[2]
            or argv[2].startswith("-")):
        return f"{verb} admits only argv [{prog!r}, '--', <path>]"
    if params.get("env"):
        return f"{verb} may not set env"
    if verb == V_FS_READ and params.get("stdin") is not None:
        return f"{verb} may not carry stdin"
    return None


@dataclass(frozen=True)
class OriginManifest:
    exec: bool = False
    fsRead: bool = False
    fsWrite: bool = False
    agent: bool = True
    rpcs: tuple = READ_RPCS
    owner: Optional[str] = None
    manifestVersion: int = MANIFEST_VERSION

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def default(cls) -> "OriginManifest":
        """What an origin advertises when its owner configured nothing: agent +
        reads, NO exec, NO fs."""
        return cls()

    @classmethod
    def from_dict(cls, d: Any) -> "OriginManifest":
        """Strict parse. Raises ``ValueError`` on anything malformed or on an
        unsupported version. Unknown keys are ignored (forward-compatible within
        a version); every capability flag must be a real bool (``"yes"`` ≠ true)."""
        if not isinstance(d, dict):
            raise ValueError("manifest must be a JSON object")
        v = d.get("manifestVersion")
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError("manifestVersion must be an integer")
        if v != MANIFEST_VERSION:
            raise UnsupportedManifest(f"unsupported manifestVersion {v}")
        kw: dict = {}
        for k in ("exec", "fsRead", "fsWrite", "agent"):
            if k in d:
                if not isinstance(d[k], bool):
                    raise ValueError(f"manifest.{k} must be a boolean")
                kw[k] = d[k]
        if "rpcs" in d:
            rpcs = d["rpcs"]
            if (not isinstance(rpcs, list)
                    or not all(isinstance(r, str) for r in rpcs)):
                raise ValueError("manifest.rpcs must be a list of strings")
            unknown = [r for r in rpcs if r not in READ_RPCS]
            if unknown:
                raise ValueError(f"manifest.rpcs names unknown rpcs {unknown}")
            kw["rpcs"] = tuple(sorted(set(rpcs)))
        owner = d.get("owner")
        if owner is not None and (not isinstance(owner, str) or not owner):
            raise ValueError("manifest.owner must be a non-empty string")
        kw["owner"] = owner
        return cls(**kw)

    @classmethod
    def load(cls, path: Optional[str]) -> "OriginManifest":
        """The origin's own manifest file. Missing file → :meth:`default` (no
        exec). A present-but-broken file is fail-CLOSED: exec/fs/agent all off."""
        if not path:
            return cls.default()
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return cls.default()
        except (OSError, ValueError):
            return deny_all()
        try:
            return cls.from_dict(raw)
        except ValueError:
            return deny_all()

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        os.replace(tmp, path)

    # ── serialisation ───────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = {"manifestVersion": self.manifestVersion, "exec": self.exec,
             "fsRead": self.fsRead, "fsWrite": self.fsWrite,
             "agent": self.agent, "rpcs": sorted(self.rpcs)}
        if self.owner:
            d["owner"] = self.owner
        return d

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(
            self.to_dict(), sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()

    # ── the policy check (run at Hub AND origin) ────────────────────────────
    def check(self, method: str, verb: Optional[str] = None,
              params: Optional[dict] = None) -> Optional[str]:
        """``None`` if this manifest permits the unit, else a refusal reason."""
        params = params or {}
        bad = verb_mismatch(method, verb)
        if bad:
            return bad
        v = verb_for(method, verb)
        if method == "origin.run":
            if v == V_EXEC:
                return None if self.exec else "origin did not advertise exec"
            if v == V_FS_READ and not self.fsRead:
                return "origin did not advertise fsRead"
            if v == V_FS_WRITE and not self.fsWrite:
                return "origin did not advertise fsWrite"
            return _fs_shape(v, params)
        if method in AGENT_METHODS:
            return None if self.agent else f"origin did not advertise agent ({v})"
        if method in ALWAYS_RPCS or method in self.rpcs:
            return None
        return f"origin did not advertise rpc {method!r}"

    def permits(self, method: str, verb: Optional[str] = None,
                params: Optional[dict] = None) -> bool:
        return self.check(method, verb, params) is None

    def enforce(self, method: str, verb: Optional[str] = None,
                params: Optional[dict] = None) -> None:
        reason = self.check(method, verb, params)
        if reason is not None:
            raise PolicyDenied(E_NOT_PERMITTED, reason)


class UnsupportedManifest(ValueError):
    """A manifest whose ``manifestVersion`` this build does not speak."""


def deny_all() -> OriginManifest:
    """Nothing but origin.describe (used for broken local files)."""
    return OriginManifest(exec=False, fsRead=False, fsWrite=False, agent=False,
                          rpcs=())


def hub_fallback() -> OriginManifest:
    """What the Hub enforces for an origin that sent no / a bad / an unsupported
    manifest: describe-only (fail-closed)."""
    return deny_all()


def receive(raw: Any) -> tuple:
    """Hub side: parse what arrived on ``hello.manifest``.

    Returns ``(manifest, status, detail)`` — never raises. Anything other than a
    valid v1 manifest yields :func:`hub_fallback` with the reason in ``status``."""
    if raw is None:
        return hub_fallback(), M_MISSING, "origin sent no manifest"
    try:
        return OriginManifest.from_dict(raw), M_OK, None
    except UnsupportedManifest as e:
        return hub_fallback(), M_UNSUPPORTED, str(e)
    except ValueError as e:
        return hub_fallback(), M_INVALID, str(e)


def manifest_path_for(state_dir: Optional[str]) -> Optional[str]:
    return os.path.join(state_dir, MANIFEST_FILENAME) if state_dir else None


# ── operator CLI (local only) ─────────────────────────────────────────────────
# The ONLY supported way to widen an origin's manifest is on the origin's own
# disk, by its owner: the file sits in a P4a-protected dir the Hub cannot fs.push
# into. The origin reads it at start and advertises it on every connect, so a
# change takes effect on the origin's next (re)start.
#
#   python -m mcp_loops.origin_policy show  --state-dir DIR
#   python -m mcp_loops.origin_policy set   --state-dir DIR --exec on --fs-read on
#
# ``set`` edits the CURRENT file (missing → the default). A present-but-broken
# file is never silently rebuilt: it needs ``--reset`` (start from the default).

def file_status(path: Optional[str]) -> tuple:
    """``(manifest, status, detail)`` for the origin's own file — what the origin
    will actually enforce (``missing`` → default, broken → deny-all)."""
    if not path:
        return OriginManifest.default(), M_MISSING, "no path"
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return OriginManifest.default(), M_MISSING, "no manifest file (default)"
    except (OSError, ValueError) as e:
        return deny_all(), M_INVALID, f"unreadable: {e}"
    try:
        return OriginManifest.from_dict(raw), M_OK, None
    except UnsupportedManifest as e:
        return deny_all(), M_UNSUPPORTED, str(e)
    except ValueError as e:
        return deny_all(), M_INVALID, str(e)


def apply_changes(base: OriginManifest, *, exec: Optional[bool] = None,
                  fs_read: Optional[bool] = None, fs_write: Optional[bool] = None,
                  agent: Optional[bool] = None, rpcs: Optional[list] = None,
                  owner: Optional[str] = None,
                  clear_owner: bool = False) -> OriginManifest:
    """A new manifest = ``base`` with the given fields changed, re-validated
    through :meth:`OriginManifest.from_dict` (so an unknown rpc still raises)."""
    d = base.to_dict()
    for key, val in (("exec", exec), ("fsRead", fs_read), ("fsWrite", fs_write),
                     ("agent", agent)):
        if val is not None:
            d[key] = bool(val)
    if rpcs is not None:
        d["rpcs"] = list(rpcs)
    if clear_owner:
        d.pop("owner", None)
    elif owner is not None:
        d["owner"] = owner
    return OriginManifest.from_dict(d)


def _cli(argv: Optional[list] = None) -> int:
    import argparse
    import sys

    def onoff(s: str) -> bool:
        s = s.strip().lower()
        if s in ("on", "true", "yes", "1"):
            return True
        if s in ("off", "false", "no", "0"):
            return False
        raise argparse.ArgumentTypeError("expected on|off")

    p = argparse.ArgumentParser(prog="python -m mcp_loops.origin_policy",
                                description="Show / edit this origin's capability "
                                "manifest (local file; default = no exec, no fs).")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("show", "set"):
        sp = sub.add_parser(name)
        g = sp.add_mutually_exclusive_group(required=True)
        g.add_argument("--state-dir", help="origin state dir (holds "
                       + MANIFEST_FILENAME + ")")
        g.add_argument("--path", help="explicit manifest file path")
    sp = sub.choices["set"]
    sp.add_argument("--exec", type=onoff, dest="exec_")
    sp.add_argument("--fs-read", type=onoff)
    sp.add_argument("--fs-write", type=onoff)
    sp.add_argument("--agent", type=onoff)
    sp.add_argument("--rpcs", help="comma list of read rpcs ('' = none)")
    sp.add_argument("--owner", help="origin-side owner pin")
    sp.add_argument("--clear-owner", action="store_true")
    sp.add_argument("--reset", action="store_true",
                    help="start from the default (required if the file is broken)")
    a = p.parse_args(argv)
    path = a.path or manifest_path_for(a.state_dir)
    cur, status, detail = file_status(path)
    if a.cmd == "show":
        print(json.dumps({"path": path, "status": status, "detail": detail,
                          "manifest": cur.to_dict(), "digest": cur.digest()},
                         indent=2))
        return 0
    if status in (M_INVALID, M_UNSUPPORTED) and not a.reset:
        print(f"refusing: {path} is {status} ({detail}); the origin currently "
              "enforces deny-all. Re-run with --reset to start from the default.",
              file=sys.stderr)
        return 2
    base = OriginManifest.default() if a.reset else cur
    rpcs = None
    if a.rpcs is not None:
        rpcs = [r.strip() for r in a.rpcs.split(",") if r.strip()]
    try:
        new = apply_changes(base, exec=a.exec_, fs_read=a.fs_read,
                            fs_write=a.fs_write, agent=a.agent, rpcs=rpcs,
                            owner=a.owner, clear_owner=a.clear_owner)
    except ValueError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new.save(path)
    print(json.dumps({"path": path, "previous": status,
                      "manifest": new.to_dict(), "digest": new.digest(),
                      "note": "takes effect on the origin's next (re)connect/start"},
                     indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
