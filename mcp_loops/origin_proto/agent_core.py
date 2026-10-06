"""agent_core — G2: the shared origin-agent core.

The origin-agent core is the piece that runs ON an origin box (the VPS included)
and turns the box's OWN loop engine into a first-class origin behind the uniform
protocol. It wraps the EXISTING worker daemon + loop engine — it does NOT invent
a second executor — reusing :class:`mcp_loops.client.LoopsMCP` to reach the local
engine at ``127.0.0.1:8771``. On top of that transport it adds the four things
§4.2/§5/§6.4/§7 name:

  * enrollment/identity — the device keypair
    (:class:`mcp_loops.origin_proto.identity.DeviceIdentity`); the private key is
    generated locally and NEVER leaves the box. (Owned by G4; the agent just
    holds the identity it was enrolled with.)
  * capability reporter — :class:`CapabilityReporter`: an HONEST report of which
    subscription CLIs are on PATH (+ versions), cores, RAM, 1-min load, harness
    protocol version, and the Products this origin can actually reach. This is
    what ``origin.describe`` and the heartbeat's ``busy``/``load`` carry.
  * capability-scoped RPC executor — :class:`OriginAgentExecutor`: the FULL RPC
    surface (read AND the mutating loop.start / loop.stop), bridged to the live
    engine. There is deliberately NO shell method (that retires the mac-origin
    URL-token remote-shell debt, §2). MUTATION is gated by the origin-side
    Product allowlist below.
  * event pusher — the channel already streams pushed run/turn/status/result
    events (replacing the rsync mirror); :func:`engine_events_to_push` adapts an
    engine status line into a wire event so a poller/tailer can feed
    :meth:`ChannelClient.push_event`.

── the origin-side Project allowlist (§6.4) ─────────────────────────────────────
The control plane can only START what the ORIGIN opted in. :class:`ProjectAllowlist`
(pre-rename alias: ``ProductAllowlist``) is that gate, and it lives on the origin
(not the plane). It is DEFAULT-DENY: a loop is remotely dispatchable only if its
bound Project (``config.projectId``, or the ``config.productId`` alias) is on the
allowlist — or the origin opted into the ``"*"`` wildcard. An UNBOUND loop
(no projectId) is dispatchable only under the wildcard. Read RPCs are never gated
(listing/among status is not "starting work"); loop.stop is never gated (halting
work is a de-escalation the origin always permits). This keeps execution on the
origin AND keeps the origin — not the plane — the authority over what may run.

Everything here is transport-agnostic: :class:`OriginAgentExecutor` is exactly the
``executor(method, params) -> awaitable[dict]`` callable
:class:`mcp_loops.origin_proto.channel.ChannelClient` expects, so wiring an origin
is ``ChannelClient(identity, host, port, origin_id=…, executor=OriginAgentExecutor(…))``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from mcp_loops import origin_policy as _policy
from mcp_loops.origin_proto import wire

# CLIs an origin may have. PATH-presence is the honest floor; a version probe
# (below) is best-effort and fail-soft — we never claim a CLI we cannot see, and
# never block the agent on a slow/hanging probe.
#
# §6.4 GAP-2 — the candidate set is WIDENED past the two subscription harnesses
# (claude/codex) toward the CLIs a user's loops actually invoke: the VCS/host
# pair (git, gh), language toolchains (node), and the common cloud CLIs (docker,
# aws, gcloud). This is the honest DEFAULT floor, not the last word: it is a
# hardcoded tuple only as a fallback — :func:`detect_clis` takes an explicit
# ``candidates=`` so the onboarding loop can probe EXACTLY the CLIs a given
# user's loops call (driven by loop requirements, not this tuple), per §6.4.
CLI_CANDIDATES = (
    "claude", "codex", "gh", "git", "node", "docker", "aws", "gcloud",
)

# the read-only slice of the allowlist (never gated) vs the mutating slice
_READ_METHODS = frozenset({
    "loop.list", "loop.get", "loop.status", "products.list", "origin.describe",
    "capabilities.probe",
})
# loop.start is the only method that starts NEW work → the only one the Product
# allowlist gates. loop.stop halts work (de-escalation) and is always permitted.
_START_METHODS = frozenset({"loop.start"})
_STOP_METHODS = frozenset({"loop.stop"})


# ── capability probing (honest, fail-soft) ───────────────────────────────────
def _cli_version(name: str, *, timeout: float = 2.0) -> Optional[str]:
    """Best-effort ``<cli> --version``. Fail-soft: a missing/slow/odd CLI yields
    ``None`` rather than raising or hanging the agent. This is the origin probing
    its OWN tools on its OWN box — not remote shell over the channel."""
    exe = shutil.which(name)
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True,
            timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    line = (out.stdout or out.stderr or "").strip().splitlines()
    return line[0].strip() if line else None


# ── per-CLI auth-state probe (§6.4 GAP-1) ─────────────────────────────────────
# The capability report knows what is PRESENT; GAP-1 is knowing what is AUTHED.
# There is no universal "am I logged in?" call, so each CLI needs its own probe.
# An AuthProbe is the NON-INTERACTIVE, side-effect-free status command for one
# CLI (a read — `gh auth status`, never `gh auth login`). Absent an entry, the
# report degrades HONESTLY to "unknown" ("present — verify login"), never a
# fabricated auth-state — exactly the honesty §6.4 demands.
class AuthProbe:
    """How to ask ONE CLI whether it is logged in, non-interactively.

    ``argv`` is the status command. ``need_stdout`` covers the CLIs whose
    status command exits 0 even when signed out but prints nothing when it is
    (e.g. ``gcloud auth list`` filtered to active accounts): with it set, authed
    requires exit 0 AND non-empty stdout. Kept tiny + declarative so widening the
    registry is data, not code."""

    def __init__(self, argv, *, need_stdout: bool = False):
        self.argv = tuple(str(a) for a in argv)
        self.need_stdout = need_stdout

    def interpret(self, returncode: int, stdout: Optional[str]) -> bool:
        if returncode != 0:
            return False
        if self.need_stdout and not (stdout or "").strip():
            return False
        return True


# The registry. Conservative on purpose — only probes that are non-interactive,
# read-only, and whose exit code (or stdout, for need_stdout) truthfully reflects
# login state. A CLI absent here reports authState "unknown" and the onboarding
# diff degrades it to "present — verify login", never asserting an auth-state the
# box cannot observe. Widening this is the sanctioned way to teach the fabric a
# new CLI's auth check (mirrors CLI_CANDIDATES being data, not logic).
AUTH_PROBES: dict[str, "AuthProbe"] = {
    "gh": AuthProbe(("gh", "auth", "status")),
    "aws": AuthProbe(("aws", "sts", "get-caller-identity")),
    "gcloud": AuthProbe(
        ("gcloud", "auth", "list", "--filter=status:ACTIVE",
         "--format=value(account)"),
        need_stdout=True),
}


def _cli_auth_state(name: str, *, timeout: float = 6.0) -> str:
    """Best-effort login-state probe for ONE CLI (§6.4 GAP-1). Returns
    ``"authed"`` | ``"unauthed"`` | ``"unknown"``. ``"unknown"`` when no probe is
    registered for this CLI, or the probe hangs/errors — an HONEST degrade to
    "present, verify login", never a fabricated auth-state. Runs the CLI's OWN
    status command on the box (non-interactive, read-only), fail-soft under a
    timeout so a hung `gh`/`aws` never blocks the describe path."""
    probe = AUTH_PROBES.get(name)
    if probe is None:
        return "unknown"
    exe = shutil.which(probe.argv[0])
    if not exe:
        return "unknown"
    try:
        out = subprocess.run(
            [exe, *probe.argv[1:]], capture_output=True, text=True,
            timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return "authed" if probe.interpret(out.returncode, out.stdout) else "unauthed"


def detect_clis(*, probe_version: bool = True, probe_auth: bool = False,
                candidates: Optional[tuple] = None) -> list[dict]:
    """Which CLIs are actually on PATH, with best-effort versions and (opt-in)
    auth-state. Honest by construction: PATH presence is the floor; ``authState``
    is only added when ``probe_auth`` is set, and is ``"authed"``/``"unauthed"``
    only where a probe (§6.4 GAP-1) can observe it — otherwise ``"unknown"`` (a CLI
    the fabric cannot yet ask), never a guess.

    ``candidates`` overrides :data:`CLI_CANDIDATES` so the onboarding loop can
    probe exactly the CLIs a given user's loops invoke (§6.4 GAP-2) instead of the
    hardcoded default. ``probe_auth`` defaults OFF so the hot describe/heartbeat
    path stays a cheap PATH+version scan — auth probes (which may hit the network,
    e.g. `aws sts`) are opt-in for the onboarding diff, not every heartbeat."""
    out: list[dict] = []
    for name in (candidates if candidates is not None else CLI_CANDIDATES):
        path = shutil.which(name)
        if not path:
            continue
        rec = {"name": name, "path": path}
        if probe_version:
            rec["version"] = _cli_version(name)
        if probe_auth:
            state = _cli_auth_state(name)
            rec["authState"] = state
            if state in ("authed", "unauthed"):
                rec["authed"] = (state == "authed")  # convenience bool where known
        out.append(rec)
    return out


# ── onboarding diff (§6.4): capability report → "action needed" items ──────────
def onboarding_actions(describe: dict, required) -> list[dict]:
    """Diff what a set of loops NEED (``required`` — an iterable of CLI names)
    against an ``origin.describe`` payload, returning concrete "action needed on
    this origin" items (§6.4). Each required CLI resolves to at most one item:

      * **absent**   — not on the origin at all → *install it* (severity ``blocker``).
      * **unauthed** — present but the auth probe says signed out → *log in*
        (severity ``action``). The headline case: "`gh` is present but
        unauthenticated."
      * **verify**   — present but auth-state is ``"unknown"`` (no probe covers it,
        or the probe degraded) → *"present — verify it's logged in"* (severity
        ``verify``). The HONEST degrade §6.4 demands — never asserts an
        auth-state the box cannot observe.

    A required CLI that is present AND ``authed`` yields NO item (nothing to do).
    Each item names both remediation routes §6.4 gives the user: do it manually,
    or ask the origin agent to run it (``origin.run`` exists to make the second
    route real). PURE + deterministic — the items are a function of
    (describe, required) with no I/O, so the onboarding loop and its test agree."""
    caps = describe.get("capabilities", {}) if isinstance(describe, dict) else {}
    clis = caps.get("clis", []) if isinstance(caps, dict) else []
    by_name: dict[str, dict] = {}
    for c in clis:
        if isinstance(c, dict) and c.get("name"):
            by_name[str(c["name"])] = c

    actions: list[dict] = []
    for name in required:
        name = str(name)
        rec = by_name.get(name)
        if rec is None:
            actions.append({
                "cli": name, "state": "absent", "severity": "blocker",
                "action": f"install {name}",
                "message": (f"{name!r} is not installed on this origin — install "
                            f"it, or ask the origin agent to install it for you."),
            })
            continue
        auth = rec.get("authState", "unknown")
        if auth == "authed":
            continue  # present + authed → nothing to do
        if auth == "unauthed":
            actions.append({
                "cli": name, "state": "unauthed", "severity": "action",
                "action": f"authenticate {name}",
                "message": (f"{name!r} is installed but not authenticated — run "
                            f"its login, or ask the origin agent to run it for you."),
            })
        else:  # "unknown" (or any unobservable state) → honest degrade
            actions.append({
                "cli": name, "state": "verify", "severity": "verify",
                "action": f"verify {name} login",
                "message": (f"{name!r} is present — verify it's logged in "
                            f"(no auth probe covers it yet)."),
            })
    return actions


def cpu_cores() -> Optional[int]:
    return os.cpu_count()


def load_avg() -> Optional[float]:
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        return None


def total_ram_gb() -> Optional[float]:
    """Physical RAM in GiB, or None where unavailable (no hard psutil dep — we
    read the POSIX sysconf pages, matching the repo's dependency-light style)."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError):
        return None
    if pages < 0 or page_size < 0:
        return None
    return round(pages * page_size / (1024 ** 3), 1)


# ── origin-side Project allowlist (§6.4) ──────────────────────────────────────
class ProjectAllowlist:
    """The origin's decision about which Projects are remotely dispatchable.

    (Round A renamed the noun Product → Project; ``ProductAllowlist`` remains as a
    back-compat alias of this class, and ``allow_product``/``deny_product`` alias
    the ``*_project`` methods, so pre-rename callers are unaffected.)

    DEFAULT-DENY: nothing may be started remotely until the origin opts a Project
    in by id, or opts into the ``"*"`` wildcard (everything, including unbound
    loops). This lives on the ORIGIN — the plane cannot widen it; it can only
    start what the origin already allowed. File-backed (single writer per box;
    atomic replace): a missing/unreadable file reads as empty (deny), and a
    failed write RAISES so a revoke can never silently not happen.
    """

    WILDCARD = "*"

    def __init__(self, path: Optional[str] = None,
                 *, allow: Optional[list[str]] = None):
        self.path = path
        self._allow: set[str] = set(allow or [])
        if path and not allow:
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        items = data.get("allow") if isinstance(data, dict) else data
        if isinstance(items, list):
            self._allow = {str(x) for x in items}

    def _persist(self) -> None:
        """Write the decision to disk. Raises ``OSError`` on failure (the .tmp is
        removed): a revoke the disk did not take must never read as done."""
        if not self.path:
            return
        # 0600 from creation (never a world-readable window) + atomic replace, so
        # a reader sees the old or the new decision, never a torn file.
        tmp = self.path + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)  # a stale .tmp keeps its old mode otherwise
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"allow": sorted(self._allow)}, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _mutate(self, change) -> None:
        # All-or-nothing: if the write fails, memory goes back to what disk holds.
        before = set(self._allow)
        change(self._allow)
        try:
            self._persist()
        except OSError:
            self._allow = before
            raise

    def allow_project(self, project_id: str) -> None:
        """Opt ``project_id`` in. Raises ``OSError`` if it could not be saved."""
        self._mutate(lambda s: s.add(str(project_id)))

    def deny_project(self, project_id: str) -> None:
        """Take ``project_id`` out. Raises ``OSError`` if it could not be saved
        (the id then stays allowed, in memory and on disk)."""
        self._mutate(lambda s: s.discard(str(project_id)))

    # back-compat aliases (pre-rename spelling).
    allow_product = allow_project
    deny_product = deny_project

    def allowed(self) -> list[str]:
        return sorted(self._allow)

    @property
    def wildcard(self) -> bool:
        return self.WILDCARD in self._allow

    def is_allowed(self, project_id: Optional[str]) -> bool:
        """True iff a loop bound to ``project_id`` may be started remotely. The
        wildcard admits everything (including an unbound loop); otherwise an
        unbound loop (``project_id`` None/empty) is refused, and a bound loop is
        admitted only when its exact Project id was opted in."""
        if self.wildcard:
            return True
        if not project_id:
            return False
        return project_id in self._allow


# Back-compat alias: the class was ``ProductAllowlist`` before the noun rename.
ProductAllowlist = ProjectAllowlist


class ProjectNotAllowed(PermissionError):
    """A remote loop.start named a loop whose bound Project is not on the
    origin-side allowlist. Fail-CLOSED: the origin refuses to start work it did
    not opt into, and the plane cannot override it (§6.4)."""


# Back-compat alias (pre-rename name); same exception object, so callers that
# ``except ProductNotAllowed`` still catch what the gate raises.
ProductNotAllowed = ProjectNotAllowed


# ── origin.run: the exec gate + primitive (HUB-FABRIC §2, §5.1(2), §5.4) ───────
# Env vars origin.run's `env` param may NEVER overwrite: the origin process/engine
# relies on them, so a merged env whitelists new vars but preserves these (§5.4
# env-clobber guard). LOOPYARD_* are the origin service's own vars.
_PROTECTED_ENV = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "TMPDIR",
    "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
})
_PROTECTED_ENV_PREFIX = ("LOOPYARD_",)

# Env vars origin.run's `env` param may NEVER ADD: loader / interpreter / tool
# hooks that turn "run an allowed program" into "run arbitrary code" (an allowed
# `git` + GIT_SSH_COMMAND, any dynamically-linked binary + LD_PRELOAD, `bash` +
# BASH_ENV, `node` + NODE_OPTIONS=--require …). Hardening P2a: the caller-supplied
# env is an ADD-only whitelist, and these injection-class keys are dropped (the
# origin's own base env is the owner's and is inherited unchanged).
_INJECTION_ENV = frozenset({
    "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PROMPT_COMMAND", "PS4", "IFS",
    "CDPATH", "GLOBIGNORE",
    "NODE_OPTIONS", "NODE_PATH",
    "PERLLIB", "PERLDB_OPTS", "RUBYOPT", "RUBYLIB",
    "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH",
    "GCONV_PATH", "HOSTALIASES", "LOCPATH", "NLSPATH", "RESOLV_HOST_CONF",
    "EDITOR", "VISUAL", "PAGER", "MANPAGER", "BROWSER", "LESSOPEN",
    "LESSCLOSE", "SSH_ASKPASS", "SUDO_ASKPASS", "GIT_ASKPASS",
})
_INJECTION_ENV_PREFIX = (
    "LD_", "DYLD_", "PYTHON", "PERL5", "GIT_", "MALLOC_", "BASH_FUNC_",
)

# Default bound on simultaneous in-flight origin.run executions per origin. A run
# beyond this is refused with a typed RunBusy (§5.4 concurrency cap) rather than
# queued unboundedly — an unbounded exec pool is a self-inflicted fork bomb. This
# is also the dedicated run-pool's max_workers so exec load never starves the fast
# read RPCs (origin.describe/status) on the shared default pool.
DEFAULT_RUN_CONCURRENCY = 4


def _match_arg_pattern(pattern: tuple, tokens: list) -> bool:
    """Positionally match one argv-tail ``pattern`` against ``tokens`` (``argv[1:]``).
    A literal token matches itself; ``"*"`` matches any single token; a trailing
    ``"**"`` matches all remaining tokens (including none). Fail-closed: an empty
    pattern matches only an empty tail, and a ``"**"`` mid-pattern is treated as the
    terminal catch-all (so it never silently under-matches)."""
    pi = 0
    ti = 0
    while pi < len(pattern):
        pat = pattern[pi]
        if pat == "**":
            return True  # matches every remaining token (including none)
        if ti >= len(tokens):
            return False
        if pat != "*" and pat != tokens[ti]:
            return False
        pi += 1
        ti += 1
    return ti == len(tokens)


def _deny_token_hit(pat: str, tok: str) -> bool:
    """One deny-pattern token vs one argv token (P2b). ``"*"`` hits any token; a
    literal hits itself, and a literal ``--flag`` also hits the ``--flag=value``
    spelling so a deny can't be dodged by gluing the value on."""
    if pat == "*" or pat == tok:
        return True
    return pat.startswith("-") and tok.startswith(pat + "=")


def _match_deny_pattern(pattern: tuple, tokens: list) -> bool:
    """Match a ``deny_args`` pattern ANYWHERE in ``tokens`` (P2b). Deny is the
    fail-closed side, so it must not be positional: the pattern's tokens hit when
    they occur IN ORDER anywhere in the tail, with any tokens interleaved — so
    ``push --force`` denies ``git -c x=y push --force`` and ``git push origin
    --force`` alike. A ``"**"`` accepts the rest. An empty pattern matches only an
    empty tail (unchanged). Greedy earliest-hit is exact for ordered-subsequence
    matching, so this never under-matches."""
    if not pattern:
        return not tokens
    ti = 0
    for pat in pattern:
        if pat == "**":
            return True
        while ti < len(tokens) and not _deny_token_hit(pat, tokens[ti]):
            ti += 1
        if ti >= len(tokens):
            return False
        ti += 1
    return True


# P2c — program identity. The allowlist governs ``argv[0]``'s basename, which on
# its own is spoofable: ``./git`` / ``/tmp/x/git`` (an fs.push'd binary NAMED git)
# or ``/bin/busybox rm`` (a multi-call binary dispatching a denied applet) all
# slip past a name-only check. Identity is therefore resolved: cwd-relative
# argv[0] is refused, an absolute argv[0] must be the SAME file the origin's own
# PATH resolves for that name, and a multi-call dispatcher is gated on its applet
# too. Realpath identity is NOT used for deny (on multi-call boxes ``echo`` and
# ``rm`` can share one binary) — argv[0]'s name is what those binaries dispatch on.
_MULTICALL_DISPATCHERS = frozenset({"busybox", "toybox", "coreutils"})


def _has_sep(s: str) -> bool:
    return os.sep in s or bool(os.altsep and os.altsep in s)


def _which_abs(name: str, path: Optional[str] = None) -> Optional[str]:
    """``shutil.which`` over the ABSOLUTE entries of PATH only — a relative or
    empty PATH entry (``.``/``::``) would resolve against the run's cwd, i.e. a
    file the caller may have pushed."""
    raw = os.environ.get("PATH", "") if path is None else path
    dirs = [d for d in raw.split(os.pathsep) if d and os.path.isabs(d)]
    if not dirs:
        return None
    return shutil.which(name, path=os.pathsep.join(dirs))


def resolve_program(argv0: str, path: Optional[str] = None) -> Optional[str]:
    """The realpath of the executable ``argv0`` names, or None when it cannot be
    resolved. Raises :class:`ValueError` for a cwd-relative argv[0] (``./git``,
    ``bin/git``) — never resolved against a caller-chosen cwd (P2c)."""
    argv0 = str(argv0)
    if _has_sep(argv0):
        if not os.path.isabs(argv0):
            raise ValueError(f"cwd-relative program path {argv0!r} is refused")
        return os.path.realpath(argv0) if os.path.isfile(argv0) else None
    found = _which_abs(argv0, path)
    return os.path.realpath(found) if found else None


# P4a — the origin's OWN state dir (device keypair, run-allowlist.json, enroll/,
# the dispatch ledger) is off-limits to origin.run. An ``fs.push`` is
# ``tee -- <path>``, so without this an owner-allowed ``tee`` could overwrite
# run-allowlist.json and widen the origin's own exec gate from the Hub side — the
# one thing the gate promises the Hub can never do. Checked on the ORIGIN (only
# it can realpath its own filesystem, symlinks included) for EVERY program: a
# per-verb write list was bypassed twice (``cp -tDIR``, then ``find -exec cp`` /
# ``find -delete`` / ``git -C`` / ``mktemp -p``), so the check judges PATHS, not
# verbs — and a read of the device key is refused along with the writes.


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives (Windows) → not within
        return False


def _path_candidates(tok: str, end_of_opts: bool) -> list:
    """Every string in one argv token that a program might open as a path: the
    token itself, a ``--opt=VALUE`` / ``key=VALUE`` value, every suffix of a short
    option (the glued value of ``-tDIR``, ``-oFILE``, ``-at/dir`` — which letter
    takes a value is per-program, so all are judged), and each ``:``-separated
    part (``host:/path``, ``PATH``-style lists)."""
    cands = [tok]
    if not end_of_opts and tok.startswith("--"):
        cands = [tok.split("=", 1)[1]] if "=" in tok else []
    elif not end_of_opts and tok.startswith("-") and len(tok) > 1:
        cands = [tok[i:] for i in range(1, len(tok))]
    elif "=" in tok:
        cands = [tok, tok.split("=", 1)[1]]
    out = []
    for c in cands:
        out.append(c)
        for sep in {":", os.pathsep}:
            if sep in c:
                out.extend(c.split(sep))
    return [c for c in out if c]


# P4a (ancestor rule) — an operand that CONTAINS the state dir reaches it just
# as surely as one inside it: ``cp -r stage/. <root>``, ``rm -rf <root>``,
# ``mv <root> …``, ``tar -xf x.tar -C <root>`` and ``find <root> -exec cp …``
# all rewrote or deleted run-allowlist.json (tester-004). So an operand / option
# value / explicit cwd that is an ANCESTOR of the state dir is refused too —
# except for this small hard-coded set that can only READ what it is pointed at
# and never descends into file CONTENTS recursively. ``find`` is NOT here
# (-exec/-delete), nor ``rg`` (recursive by default, would read the device key),
# nor ``git``: a repo's own config (core.fsmonitor, core.pager, diff.external,
# textconv, hooks) makes even ``git status`` run arbitrary commands (tester-005 K).
_READ_ONLY_PROGRAMS = frozenset({"ls", "cat", "head", "tail", "wc", "stat",
                                 "du", "file"})
_GREP_RECURSIVE = frozenset({"-r", "-R", "--recursive",
                             "--dereference-recursive"})


def _read_only_run(argv: list) -> bool:
    """True iff ``argv`` is one of the read-only forms that may name an ANCESTOR
    of the state dir (never a path inside it — that is refused for every
    program). Fail closed: anything unrecognised is not read-only."""
    prog = os.path.basename(str(argv[0]))
    tail = [str(a) for a in argv[1:]]
    if prog in _READ_ONLY_PROGRAMS:
        return True
    if prog == "grep":
        for t in tail:
            if t == "--":
                break
            if t in _GREP_RECURSIVE or t.startswith("--directories") or (
                    t.startswith("-d") and not t.startswith("--")):
                return False
            if t.startswith("-") and not t.startswith("--") and (
                    "r" in t[1:] or "R" in t[1:]):
                return False
        return True
    return False


# P4a (link rule) — the kernel resolves a RELATIVE symlink target against the
# LINK's directory, not the run's cwd: ``ln -s ../mirror/_origin <t>/d/lnk`` run
# from elsewhere looks harmless against the cwd but plants a link to the state
# dir, which ``find -L <t>/d -exec cp …`` / ``tar -xf x.tar -C <t>/d`` then write
# through (tester-005 H1/H3/J). For a link-making run every operand is also
# resolved against every place the link could be created (each operand / option
# value, and its parent dir) — over-approximate, fail closed.
_LINK_PROGRAMS = frozenset({"ln", "link"})


def _makes_link(argv: list) -> bool:
    prog = os.path.basename(str(argv[0]))
    if prog in _LINK_PROGRAMS:
        return True
    if prog != "cp":
        return False
    for t in (str(a) for a in argv[1:]):
        if t == "--":
            break
        if t == "--symbolic-link" or (
                t.startswith("-") and not t.startswith("--") and "s" in t[1:]):
            return True
    return False


def _link_dirs(tail: list, base: str) -> list:
    """Every directory a link-making run could create its link in: each path
    candidate of each token (a DIR operand / ``-t DIR``) and its parent (a
    ``<dir>/<name>`` link path), realpath'd as the kernel will walk them."""
    dirs = []
    end_of_opts = False
    for tok in tail:
        if not end_of_opts and tok == "--":
            end_of_opts = True
            continue
        for cand in _path_candidates(tok, end_of_opts):
            p = os.path.join(base, os.path.expanduser(cand))
            for d in (p, os.path.dirname(p)):
                d = os.path.realpath(d)
                if d not in dirs:
                    dirs.append(d)
    return dirs


def _contains_protected(cand_real: str, cand_abs: str, roots_real: list,
                        roots_abs: list) -> bool:
    """True iff the candidate path is an ANCESTOR (or the dir itself) of a
    protected root — judged on both the realpath and the lexical abspath, so a
    symlinked parent (``~/data -> /mnt/data``) cannot hide the containment."""
    return (any(_within(r, cand_real) for r in roots_real)
            or any(_within(r, cand_abs) for r in roots_abs))


def _roots(protected_dirs) -> tuple:
    dirs = [d for d in (protected_dirs or []) if d]
    return ([os.path.realpath(d) for d in dirs],
            [os.path.normpath(os.path.abspath(d)) for d in dirs])


def neutral_cwd(argv: Any, protected_dirs) -> Optional[str]:
    """The cwd to run ``argv`` in when the caller named none (P4a ancestor
    rule): if the origin's OWN cwd is an ancestor of its state dir, a cwd-only
    writer (``tar -xf x.tar``, ``cp -r stage/_origin .``) would land in it — so a
    non-read-only run is moved to ``tempfile.gettempdir()``. None = keep the
    inherited cwd."""
    if not protected_dirs or not isinstance(argv, (list, tuple)) or not argv:
        return None
    if _read_only_run(list(argv)):
        return None
    roots_real, roots_abs = _roots(protected_dirs)
    here = os.getcwd()
    if _contains_protected(os.path.realpath(here),
                           os.path.normpath(os.path.abspath(here)),
                           roots_real, roots_abs):
        return tempfile.gettempdir()
    return None


def protected_write_target(argv: Any, protected_dirs, *,
                           cwd: Optional[str] = None,
                           env: Optional[dict] = None) -> Optional[str]:
    """The first thing about a run that reaches one of ``protected_dirs`` (the
    origin's own state dir), else None (P4a). Fails closed for EVERY program —
    there is no write-verb list: the run's effective ``cwd``, every ``argv``
    operand (``argv[0]`` excluded; relative operands resolve against the cwd,
    ``..`` and symlinks resolved), ``--opt=VALUE`` and ``key=VALUE`` values,
    glued short-option values (``-tDIR``) and the values of caller-supplied
    ``env`` are all judged.

    Two rules:
    - INTO: anything that resolves inside the state dir is refused for every
      program — reads (``cat <state>/device.json``) included.
    - ANCESTOR: an operand/option value or an EXPLICIT ``cwd`` that CONTAINS the
      state dir (``cp -r x/. <root>``, ``rm -rf <root>``, ``tar -C <root>``,
      ``find <root> -exec …``) is refused unless the run is one of the
      hard-coded read-only forms (:func:`_read_only_run`: ls/cat/head/tail/wc/
      stat/du/file, non-recursive grep — never ``git``, whose repo config can
      run commands).
      Env values are judged by the INTO rule only (``HOME=~`` is ordinary). The
      IMPLICIT cwd is not an operand; :func:`neutral_cwd` moves a non-read-only
      run out of an ancestor cwd instead.
    - LINK: for ``ln``/``link``/``cp -s|--symbolic-link`` every relative operand
      is ALSO resolved against each directory the link could land in (an
      operand, or its parent) — the kernel resolves a symlink target against
      the link's dir, not the cwd — and INTO/ANCESTOR apply to the result.
    Over-refusing a harmless token is the fail-closed side (``find ~`` is
    refused when the state dir lives under ``~``).

    Known gap this CANNOT close: a program that computes a path the argv never
    spells out — an allowlisted interpreter or shell (``sh -c 'tee "$D/x"'``,
    ``python -c``, ``perl -e``, ``awk``/``find -exec sh -c``), or a path
    assembled at runtime from pieces. The argv never names the state dir, so no
    argv check can see it. Keeping interpreters off an origin's run allowlist is
    the owner's job; allowing one grants the Hub everything the origin user can
    do, this check included.

    DOCUMENTED RESIDUALS (bounded by the loop manager, not reworked — an argv
    check cannot see them):
    - a symlink planted by other means than ``ln``/``cp -s`` — an archive
      carrying a symlink member (``tar -x``, ``cp -a``/``rsync -a`` of a tree
      holding one), an ``fs.push``'d link — then a recursive or
      link-following writer (``find -L … -exec``, ``tar -x`` into that dir,
      ``cp -r`` through it) walks into the state dir;
    - an allowlisted interpreter or multi-program runner (``sh -c``,
      ``python -c``, ``perl -e``, ``make``, ``xargs``, and ``git`` — a repo
      config's core.fsmonitor / core.pager / diff.external / textconv / hooks
      run arbitrary commands);
    - a path assembled at runtime from pieces the argv never spells out.
    Mitigation: owners keep interpreters, git, and recursive/link-following
    writers off an origin's run allowlist (or deny them with a RunRule)."""
    if not protected_dirs or not isinstance(argv, (list, tuple)) or not argv:
        return None
    roots_real, roots_abs = _roots(protected_dirs)
    if not roots_real:
        return None
    read_only = _read_only_run(list(argv))
    base_raw = cwd or os.getcwd()
    base = os.path.realpath(base_raw)
    if any(_within(base, r) for r in roots_real):
        return cwd or base
    if cwd and not read_only and _contains_protected(
            base, os.path.normpath(os.path.abspath(cwd)), roots_real, roots_abs):
        return cwd
    lex_base = os.path.normpath(os.path.abspath(base_raw))
    judged = []  # (token, end_of_opts, ancestor_rule)
    end_of_opts = False
    for tok in [str(a) for a in argv][1:]:
        if not end_of_opts and tok == "--":
            end_of_opts = True
            continue
        judged.append((tok, end_of_opts, not read_only))
    if _makes_link(list(argv)):
        tail = [str(a) for a in argv][1:]
        for ldir in _link_dirs(tail, base):
            eoo = False
            for tok in tail:
                if not eoo and tok == "--":
                    eoo = True
                    continue
                for cand in _path_candidates(tok, eoo):
                    if os.path.isabs(os.path.expanduser(cand)):
                        continue  # absolute: judged below, no link-dir effect
                    joined = os.path.join(ldir, cand)
                    real = os.path.realpath(joined)
                    if any(_within(real, r) for r in roots_real) or \
                            _contains_protected(real, os.path.normpath(joined),
                                                roots_real, roots_abs):
                        return tok
    if isinstance(env, dict):  # ``TMPDIR=<state>`` steers mktemp & co
        judged += [(f"{k}={v}", False, False) for k, v in env.items()]
    for tok, eoo, ancestor in judged:
        for cand in _path_candidates(tok, eoo):
            joined = os.path.join(base, os.path.expanduser(cand))
            real = os.path.realpath(joined)
            if any(_within(real, r) for r in roots_real):
                return tok
            if ancestor and _contains_protected(
                    real, os.path.normpath(os.path.join(
                        lex_base, os.path.expanduser(cand))),
                    roots_real, roots_abs):
                return tok
    return None


class RunRule:
    """An argv-level policy rule for one program (§5.3 P3 refinement). It NARROWS
    the coarse opt-in-to-exec gate so an origin can allow ``git`` but ONLY its
    read-only subcommands, or HARD-deny ``rm``/``shutdown`` even when the box
    wildcard-allows exec.

    - ``program`` — ``argv[0]``'s basename this rule governs (``"*"`` governs every
      program, useful for a fleet-wide deny).
    - ``deny=True`` — HARD block: the program never runs, even under the wildcard.
      This is the §5.4 wildcard-blast-radius guard — opt into exec broadly, then
      subtract the destructive few.
    - ``allow_args`` — if set, ``argv[1:]`` must MATCH one of these token patterns;
      a program with any ``allow_args`` rule is refused when nothing matches.
    - ``deny_args`` — ``argv[1:]`` CONTAINING any of these patterns (its tokens in
      order, anywhere in the tail — see :func:`_match_deny_pattern`) is refused, and
      a deny is checked BEFORE allow so it always wins.

    A pattern is a token tuple (a bare string is split on whitespace): a literal
    matches that token, ``"*"`` matches any one token, and a trailing ``"**"``
    matches all remaining tokens. Fail-closed by construction."""

    def __init__(self, program: str, *, deny: bool = False,
                 allow_args: Optional[list] = None,
                 deny_args: Optional[list] = None):
        self.program = str(program)
        self.deny = bool(deny)
        self.allow_args = [self._norm(p) for p in (allow_args or [])]
        self.deny_args = [self._norm(p) for p in (deny_args or [])]

    @staticmethod
    def _norm(pattern: Any) -> tuple:
        if isinstance(pattern, str):
            return tuple(pattern.split())
        if isinstance(pattern, (list, tuple)):
            return tuple(str(t) for t in pattern)
        return (str(pattern),)

    def governs(self, program: str) -> bool:
        return self.program == program or self.program == RunAllowlist.WILDCARD

    def denies(self, tail: list) -> bool:
        if self.deny:
            return True
        return any(_match_deny_pattern(p, tail) for p in self.deny_args)

    def allows(self, tail: list) -> bool:
        return any(_match_arg_pattern(p, tail) for p in self.allow_args)

    def to_dict(self) -> dict:
        d: dict = {"program": self.program}
        if self.deny:
            d["deny"] = True
        if self.allow_args:
            d["allowArgs"] = [" ".join(p) for p in self.allow_args]
        if self.deny_args:
            d["denyArgs"] = [" ".join(p) for p in self.deny_args]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RunRule":
        return cls(d.get("program", RunAllowlist.WILDCARD),
                   deny=bool(d.get("deny")),
                   allow_args=list(d.get("allowArgs", [])),
                   deny_args=list(d.get("denyArgs", [])))


class RunAllowlist:
    """The origin's opt-in-to-exec gate for ``origin.run`` (§5.1(2), fail-closed).

    Modeled on :class:`ProjectAllowlist`: DEFAULT-DENY. An origin that has not
    opted into exec refuses every ``origin.run``. The origin opts in by adding the
    ``"*"`` wildcard (allow ANY program — the P1 coarse "I permit exec on this
    box" switch) and/or specific program basenames (``argv[0]``'s basename).

    P3 (§5.3) refines this coarse gate with argv-level :class:`RunRule` policy:
    per-program rules can narrow a program to specific subcommands (``allow_args``)
    or hard-deny destructive programs/flags (``deny``/``deny_args``) — and a DENY
    wins even under the wildcard, so an origin can safely allow-all-and-subtract
    (the §5.4 blast-radius guard). This lives on the ORIGIN's own disk; the Hub
    cannot widen it — it can only run what the origin already allowed. File-backed
    (0600, atomic replace): a missing/unreadable file reads as empty (deny), and
    a failed write RAISES so revoking a program can never silently not happen.
    """

    WILDCARD = "*"

    def __init__(self, path: Optional[str] = None,
                 *, allow: Optional[list[str]] = None,
                 rules: Optional[list[RunRule]] = None):
        self.path = path
        self._allow: set[str] = set(allow or [])
        self._rules: list[RunRule] = list(rules or [])
        if path and not allow and not rules:
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        items = data.get("allow") if isinstance(data, dict) else data
        if isinstance(items, list):
            self._allow = {str(x) for x in items}
        raw_rules = data.get("rules") if isinstance(data, dict) else None
        if isinstance(raw_rules, list):
            self._rules = [RunRule.from_dict(r) for r in raw_rules
                           if isinstance(r, dict)]

    def _persist(self) -> None:
        """Write the policy to disk. Raises ``OSError`` on failure (the .tmp is
        removed): a revoke the disk did not take must never read as done."""
        if not self.path:
            return
        # 0600 from creation + atomic replace — same as ProjectAllowlist._persist.
        tmp = self.path + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)  # a stale .tmp keeps its old mode otherwise
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"allow": sorted(self._allow),
                           "rules": [r.to_dict() for r in self._rules]},
                          fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _mutate(self, change) -> None:
        # All-or-nothing: if the write fails, memory goes back to what disk holds.
        allow, rules = set(self._allow), list(self._rules)
        change()
        try:
            self._persist()
        except OSError:
            self._allow, self._rules = allow, rules
            raise

    def allow_program(self, program: str) -> None:
        """Opt ``program`` in. Raises ``OSError`` if it could not be saved."""
        self._mutate(lambda: self._allow.add(str(program)))

    def deny_program(self, program: str) -> None:
        """Take ``program`` out. Raises ``OSError`` if it could not be saved (the
        program then stays allowed, in memory and on disk)."""
        self._mutate(lambda: self._allow.discard(str(program)))

    def add_rule(self, rule: RunRule) -> None:
        """Add an argv-level policy rule (§5.3). A hard-deny rule takes effect even
        under the wildcard, so this is how an origin subtracts destructive programs
        from a broad opt-in. Raises ``OSError`` if it could not be saved."""
        self._mutate(lambda: self._rules.append(rule))

    def rules(self) -> list[RunRule]:
        return list(self._rules)

    def allowed(self) -> list[str]:
        return sorted(self._allow)

    @property
    def wildcard(self) -> bool:
        return self.WILDCARD in self._allow

    def is_allowed(self, argv: Any) -> bool:
        """True iff this origin permits running ``argv``. Fail-closed and layered:

        0. malformed/empty argv → deny.
        1. any governing rule that DENIES (``deny`` or a ``deny_args`` match) → deny,
           overriding even the coarse wildcard (§5.4 blast-radius guard).
        2. if the program has ``allow_args`` rules → allow ONLY when the argv tail
           matches one (argv-level narrowing).
        3. otherwise fall back to the coarse gate: wildcard, or the program's
           basename explicitly opted in."""
        if not isinstance(argv, (list, tuple)) or not argv:
            return False
        argv0 = str(argv[0])
        program = os.path.basename(argv0)
        if not program:
            return False
        tail = [str(a) for a in argv[1:]]
        # (0b) P2c identity: refuse cwd-relative argv[0]. An absolute argv[0] only
        # inherits its basename's opt-in/rules when it is the SAME file PATH
        # resolves for that name; otherwise (``/tmp/x/git``) the name confers no
        # privilege — a by-name DENY still bites, but only the wildcard (or an
        # exact-path opt-in) can admit it.
        if _has_sep(argv0):
            try:
                real = resolve_program(argv0)
            except ValueError:
                return False
            if real is None or real != resolve_program(program):
                if any(r.denies(tail) for r in self._rules if r.governs(program)):
                    return False
                return self._gate(argv0, tail)
        # (0c) a multi-call dispatcher (``busybox rm``) is ALSO gated on its applet,
        # so a deny/allow on ``rm`` cannot be dodged through the dispatcher.
        if program in _MULTICALL_DISPATCHERS and len(argv) > 1:
            applet = str(argv[1])
            if _has_sep(applet) or not self._gate(applet, [str(a) for a in argv[2:]]):
                return False
        return self._gate(program, tail)

    def _gate(self, program: str, tail: list) -> bool:
        """Layers (1)–(3) of :meth:`is_allowed` for one resolved program name."""
        governing = [r for r in self._rules if r.governs(program)]
        # (1) DENY wins — even under the coarse wildcard.
        for rule in governing:
            if rule.denies(tail):
                return False
        # (2) argv-level narrowing: a program constrained by allow_args must match.
        constrained = [r for r in governing if r.allow_args]
        if constrained:
            return any(r.allows(tail) for r in constrained)
        # (3) coarse opt-in gate.
        if self.wildcard:
            return True
        return program in self._allow

    def describe(self) -> dict:
        """The run policy this origin ADVERTISES (§3 — each origin advertises what
        it permits). Surfaced in ``origin.describe`` so the Hub/owner can see the
        exec gate without being able to widen it."""
        programs = sorted(p for p in self._allow if p != self.WILDCARD)
        return {
            "execEnabled": bool(self._allow) or bool(self._rules),
            "wildcard": self.wildcard,
            "programs": programs,
            "rules": [r.to_dict() for r in self._rules],
        }


class RunNotAllowed(PermissionError):
    """An ``origin.run`` named a program this origin did not opt into. Fail-CLOSED:
    the origin refuses to exec what it did not permit, and the Hub cannot override
    it (§5.1(2)). The channel returns this as a typed ``rpc_result`` error, keeping
    the channel up."""


class RunBusy(RuntimeError):
    """An ``origin.run`` arrived while this origin was already at its exec
    concurrency cap (§5.4). Refused with a typed error rather than queued
    unboundedly; the caller may retry once a slot frees."""


def merge_env(base: dict, overrides: Optional[dict]) -> dict:
    """Whitelist-merge ``overrides`` into a COPY of ``base`` (§5.4 env-clobber
    guard): new vars are added, but the protected vars the origin/engine relies on
    (:data:`_PROTECTED_ENV` + ``LOOPYARD_*``) are never overwritten, and
    loader/interpreter injection vars (LD_PRELOAD, GIT_SSH_COMMAND, BASH_ENV,
    NODE_OPTIONS, PYTHON*, …) are never added (P2a). Never a wholesale replace —
    a run cannot strip ``PATH`` out from under the engine."""
    env = dict(base)
    for k, v in (overrides or {}).items():
        key = str(k)
        if not _env_key_addable(key):
            continue
        env[key] = str(v)
    return env


def _env_key_addable(key: str) -> bool:
    """True iff a caller-supplied ``env`` key may be merged (P2a). Refuses the
    protected vars, loader/interpreter injection vars (:data:`_INJECTION_ENV` +
    prefixes, case-insensitive — Windows env is case-insensitive), and any name
    that is not a plain identifier (``=``/NUL/whitespace smuggling)."""
    if not key or not (key[0].isalpha() or key[0] == "_"):
        return False
    if not all(c.isascii() and (c.isalnum() or c == "_") for c in key):
        return False
    up = key.upper()
    if up in _PROTECTED_ENV or up.startswith(_PROTECTED_ENV_PREFIX):
        return False
    if up in _INJECTION_ENV or up.startswith(_INJECTION_ENV_PREFIX):
        return False
    return True


def _b64(data: bytes) -> str:
    return base64.b64encode(data or b"").decode("ascii")


def _pinned_executable(argv: list, env: dict) -> Optional[str]:
    """P2c: the absolute executable to spawn for ``argv`` — a bare name is pinned
    to its ABSOLUTE-PATH-only resolution (so the child's exec lookup can never
    fall through a relative PATH entry into the run's cwd); argv[0] itself is kept
    (multi-call binaries dispatch on it). A cwd-relative argv[0] is refused
    outright (defence in depth behind the allowlist gate); an unresolvable bare
    name raises :class:`FileNotFoundError` (→ exit 127) rather than letting the
    spawn search PATH itself."""
    argv0 = argv[0]
    if _has_sep(argv0):
        if not os.path.isabs(argv0):
            raise ValueError(f"origin.run refuses cwd-relative program {argv0!r}")
        return None
    exe = _which_abs(argv0, (env or {}).get("PATH", os.defpath))
    if exe is None:
        raise FileNotFoundError(f"program {argv0!r} not found on the origin's PATH")
    return exe


# P4c: the one-shot origin.run captures stdout+stderr into memory, so the cap
# must bite DURING capture — a post-hoc ``len(stdout) > max_bytes`` (the old
# fs.pull check) only fires after the origin already buffered the whole thing and
# shipped it over the channel. ``maxBytes`` in the params narrows this default.
DEFAULT_RUN_MAX_OUTPUT = 64 * 1024 * 1024  # 64 MiB, stdout + stderr combined


def _run_max_output(params: dict) -> int:
    raw = params.get("maxBytes")
    if raw is None:
        return DEFAULT_RUN_MAX_OUTPUT
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"origin.run maxBytes must be an integer, got {raw!r}")
    if cap < 0:
        raise ValueError("origin.run maxBytes must be >= 0")
    return min(cap, DEFAULT_RUN_MAX_OUTPUT)


def exec_run(params: dict, *, base_env: Optional[dict] = None,
             read_size: int = 65536) -> dict:
    """The synchronous body of ``origin.run`` (§2.2): spawn ``params['argv']`` as a
    subprocess (argv list, NEVER ``shell=True`` — no shell-string injection),
    enforce ``timeout``, merge ``env`` (whitelist), feed ``stdin``, and return
    ``{exitCode, stdout:<b64>, stderr:<b64>, durationMs, timedOut?,
    outputLimitExceeded?}``. Output is captured with a running byte counter
    (P4c): once stdout+stderr pass ``maxBytes`` (default
    :data:`DEFAULT_RUN_MAX_OUTPUT`) the child is killed, capture stops at the
    ceiling, and the envelope says ``outputLimitExceeded`` (exitCode -1) — memory
    is bounded by the cap, never by what the program chose to print. Runs ON the
    origin's own box; called off-thread on the dedicated run-pool so exec never
    blocks the channel loop nor starves read RPCs. Pure + injectable (``base_env``)
    so it is testable without a live channel."""
    argv = params.get("argv")
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("origin.run requires a non-empty argv list")
    argv = [str(a) for a in argv]
    cwd = params.get("cwd") or None
    timeout = params.get("timeout")
    if timeout is not None:
        timeout = float(timeout)
    max_output = _run_max_output(params)
    env = merge_env(base_env if base_env is not None else os.environ,
                    params.get("env"))
    stdin_b64 = params.get("stdin")
    stdin_bytes = base64.b64decode(stdin_b64) if stdin_b64 else None

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=(subprocess.PIPE if stdin_bytes is not None
                   else subprocess.DEVNULL),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=env, shell=False, bufsize=0,
            executable=_pinned_executable(argv, env))
    except FileNotFoundError as e:
        # a program not on the box is a run FAILURE, not a channel error — report
        # it as an exec result (exit 127, the shell convention) so the caller sees
        # "command not found" instead of the channel returning an internal error.
        return {"exitCode": 127, "stdout": _b64(b""),
                "stderr": _b64(f"{e}\n".encode()),
                "durationMs": int((time.monotonic() - started) * 1000)}

    bufs: dict = {"out": [], "err": []}
    lock = threading.Lock()
    state = {"total": 0, "over": False}

    def _pump(pipe, fd: str) -> None:
        try:
            for chunk in iter(lambda: pipe.read(read_size), b""):
                with lock:
                    room = max_output - state["total"]
                    if len(chunk) > room:
                        chunk = chunk[:room]
                        state["over"] = True
                    state["total"] += len(chunk)
                    if chunk:
                        bufs[fd].append(chunk)
                    over = state["over"]
                if over:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    break
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _feed() -> None:
        # stdin is fed off-thread so a child echoing it (``tee``) can never
        # deadlock against a full stdout pipe we have not drained yet.
        try:
            proc.stdin.write(stdin_bytes)
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=_pump, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=_pump, args=(proc.stderr, "err"), daemon=True)]
    if stdin_bytes is not None:
        threads.append(threading.Thread(target=_feed, daemon=True))
    for t in threads:
        t.start()

    timed_out = False
    try:
        exit_code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
        exit_code = -1
    for t in threads:
        t.join(timeout=5.0)
    result = {
        "exitCode": exit_code,
        "stdout": _b64(b"".join(bufs["out"])),
        "stderr": _b64(b"".join(bufs["err"])),
        "durationMs": int((time.monotonic() - started) * 1000),
    }
    if timed_out:
        result["timedOut"] = True
    if state["over"]:
        result["exitCode"] = -1
        result["outputLimitExceeded"] = True
    return result


def exec_run_streamed(params: dict, on_chunk: Callable[[str, str], None], *,
                      base_env: Optional[dict] = None,
                      read_size: int = 65536) -> dict:
    """The STREAMED body of ``origin.run`` (§2.4 seam 1): spawn ``params['argv']``
    with :class:`subprocess.Popen`, and as output arrives call
    ``on_chunk(fd, b64)`` — ``fd`` is ``"out"``/``"err"``, ``b64`` a base64 slice
    of that pipe — so a minutes-long run's output flows back as ``run.chunk`` events
    instead of buffering into one giant reply. Returns the TERMINAL envelope
    ``{exitCode, durationMs, timedOut?, outputLimitExceeded?}`` only
    (stdout/stderr already streamed). The same ``maxBytes`` ceiling as the
    one-shot path (P4c) is enforced on what gets STREAMED: past it the last
    chunk is truncated, the child is killed, and nothing more is forwarded.

    Two reader threads drain the pipes (cross-platform, unlike ``select`` on
    Windows pipes) — this runs on the dedicated run-pool thread, so blocking here
    never touches the channel's event loop. ``on_chunk`` MUST NOT raise (the caller
    wraps it best-effort): a lost telemetry chunk must never abort the run."""
    argv = params.get("argv")
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("origin.run requires a non-empty argv list")
    argv = [str(a) for a in argv]
    cwd = params.get("cwd") or None
    timeout = params.get("timeout")
    if timeout is not None:
        timeout = float(timeout)
    max_output = _run_max_output(params)
    env = merge_env(base_env if base_env is not None else os.environ,
                    params.get("env"))
    stdin_b64 = params.get("stdin")
    stdin_bytes = base64.b64decode(stdin_b64) if stdin_b64 else None

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=(subprocess.PIPE if stdin_bytes is not None
                   else subprocess.DEVNULL),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=env, shell=False, bufsize=0,
            executable=_pinned_executable(argv, env))
    except FileNotFoundError as e:
        # missing program → exit 127 (shell convention), reported as a run result
        # (streamed as a single stderr chunk) rather than a channel error.
        on_chunk("err", _b64(f"{e}\n".encode()))
        return {"exitCode": 127,
                "durationMs": int((time.monotonic() - started) * 1000)}

    if stdin_bytes is not None:
        try:
            proc.stdin.write(stdin_bytes)
            proc.stdin.close()
        except OSError:
            pass

    lock = threading.Lock()
    state = {"total": 0, "over": False}

    def _pump(pipe, fd: str) -> None:
        try:
            for chunk in iter(lambda: pipe.read(read_size), b""):
                with lock:
                    if state["over"]:
                        break
                    room = max_output - state["total"]
                    if len(chunk) > room:
                        chunk = chunk[:room]
                        state["over"] = True
                    state["total"] += len(chunk)
                    over = state["over"]
                    # forwarded under the lock so the two pumps can never
                    # jointly stream past the ceiling
                    if chunk:
                        on_chunk(fd, _b64(chunk))
                if over:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    break
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    t_out = threading.Thread(target=_pump, args=(proc.stdout, "out"), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc.stderr, "err"), daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        exit_code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        exit_code = proc.wait()
    t_out.join(timeout=5.0)
    t_err.join(timeout=5.0)
    result = {"exitCode": exit_code,
              "durationMs": int((time.monotonic() - started) * 1000)}
    if timed_out:
        result["exitCode"] = -1
        result["timedOut"] = True
    if state["over"]:
        result["exitCode"] = -1
        result["outputLimitExceeded"] = True
    return result


# ── capability reporter ───────────────────────────────────────────────────────
class CapabilityReporter:
    """Builds the honest capability picture for ``origin.describe`` and the
    heartbeat. Injectable pieces (``clis``, the engine) keep it fast + testable:
    tests pass ``clis=[]`` to skip subprocess version probes, and a fake engine
    to stand in for the live loop engine."""

    def __init__(self, origin_id: str = "local", *, engine: Any = None,
                 allowlist: Optional[ProductAllowlist] = None,
                 run_allowlist: Optional["RunAllowlist"] = None,
                 clis: Optional[list[dict]] = None,
                 probe_auth: bool = False):
        self.origin_id = origin_id
        self._engine = engine
        self.allowlist = allowlist
        # §3: the origin advertises its exec gate (run policy) in describe so the
        # Hub/owner can SEE what it permits — while never being able to widen it.
        self.run_allowlist = run_allowlist
        self._clis = clis  # None → probe lazily; [] → explicitly none (tests)
        # §6.4 GAP-1: when set, the lazy CLI probe also reports authState. OFF by
        # default — the hot describe/heartbeat path stays a cheap PATH+version
        # scan; the onboarding diff opts in when it needs auth-state.
        self._probe_auth = probe_auth

    def _reachable_products(self) -> list[dict]:
        """Products this origin can actually reach, from the LIVE engine (not a
        static file). Degrade honestly when the engine build has no products
        tool."""
        if self._engine is None:
            return []
        try:
            res = self._engine.call("loop_product_list", {})
        except Exception:  # noqa: BLE001 — a missing tool must not sink describe
            return []
        products = res.get("products", []) if isinstance(res, dict) else []
        out = []
        for p in products:
            if not isinstance(p, dict):
                continue
            pid = p.get("id") or p.get("projectId") or p.get("productId") or p.get("name")
            rec = {"id": pid, "name": p.get("name", pid)}
            if self.allowlist is not None:
                rec["dispatchable"] = self.allowlist.is_allowed(pid)
            out.append(rec)
        return out

    def capabilities(self, *, probe_auth: Optional[bool] = None,
                     candidates: Optional[tuple] = None) -> dict:
        """The capability block: CLIs (+versions), cores, RAM, load, harness.

        ``probe_auth``/``candidates`` override the construction-time defaults for a
        SINGLE call — the seam the Hub's live onboarding check (§6.4) uses to ask
        for a fresh, auth-aware CLI picture on demand (GAP-1/GAP-2) without turning
        auth probes on for the hot heartbeat path. When ``clis`` was injected
        (tests) that fixed list is honored as-is, ignoring the overrides."""
        want_auth = self._probe_auth if probe_auth is None else bool(probe_auth)
        clis = (self._clis if self._clis is not None
                else detect_clis(probe_auth=want_auth, candidates=candidates))
        caps = {
            "clis": clis,
            "cores": cpu_cores(),
            "ramGb": total_ram_gb(),
            "load": load_avg(),
            "harness": {"protocol": wire.PROTOCOL_VERSION},
        }
        if self.allowlist is not None:
            allowed = self.allowlist.allowed()
            caps["dispatchableProjects"] = allowed          # canonical
            caps["dispatchableProducts"] = allowed          # back-compat alias
        if self.run_allowlist is not None:
            caps["runPolicy"] = self.run_allowlist.describe()
        policy = getattr(self, "policy", None)
        if policy is not None:
            caps["manifest"] = policy.to_dict()  # P3: what this origin permits
        return caps

    def describe(self, *, probe_auth: Optional[bool] = None,
                candidates: Optional[tuple] = None) -> dict:
        """The full ``origin.describe`` payload (§13 RPC surface). ``probe_auth`` /
        ``candidates`` are per-call overrides (see :meth:`capabilities`) — the Hub's
        live onboarding check passes ``probe_auth=True`` + the loops' required CLIs
        so the returned ``authState`` is freshly observed, not a stale heartbeat."""
        return {
            "origin": self.origin_id,
            "capabilities": self.capabilities(probe_auth=probe_auth,
                                              candidates=candidates),
            "harness": {"protocol": wire.PROTOCOL_VERSION},
            "products": self._reachable_products(),
        }

    def heartbeat_extras(self, *, busy: Optional[bool] = None) -> dict:
        """The optional load/busy the heartbeat carries. ``busy`` is supplied by
        the agent (it knows if a dispatch is in-flight); ``load`` is measured."""
        extras: dict = {"load": load_avg()}
        if busy is not None:
            extras["busy"] = busy
        return extras


# ── the capability-scoped RPC executor (the ChannelClient's executor) ─────────
class OriginAgentExecutor:
    """The FULL capability-scoped RPC executor, bridging the fixed method
    allowlist to the box's OWN live loop engine over :class:`LoopsMCP`. It is the
    ``executor(method, params)`` the :class:`ChannelClient` invokes for every
    inbound RPC.

    Unlike the round-1 read-only dogfood bridge, this serves the MUTATING RPCs
    too — but ``loop.start`` is gated by the origin-side :class:`ProductAllowlist`
    (§6.4): the loop's bound Product must be opted in, or the start is refused
    with :class:`ProductNotAllowed` (which the channel returns as a typed
    ``rpc_result`` error, keeping the channel up). Execution ALWAYS stays here on
    the origin; the plane only ever sent a scoped RPC, never work to copy.

    The engine client is synchronous, so calls run off-thread (``run_in_executor``)
    and never block the channel's event loop. ``engine`` is injectable so tests
    exercise the full gate without a live 8771.
    """

    def __init__(self, origin_id: str = "local", *, engine: Any = None,
                 allowlist: Optional[ProductAllowlist] = None,
                 reporter: Optional[CapabilityReporter] = None,
                 run_allowlist: Optional[RunAllowlist] = None,
                 run_concurrency: int = DEFAULT_RUN_CONCURRENCY,
                 audit: Optional[Callable[..., None]] = None,
                 protected_dirs: Optional[list[str]] = None,
                 manifest: Optional["_policy.OriginManifest"] = None,
                 manifest_path: Optional[str] = None):
        self.origin_id = origin_id
        self._engine = engine  # lazily constructed LoopsMCP if None
        self.allowlist = allowlist or ProductAllowlist()
        # origin.run gate + isolation (§5.1(2), §5.4). DEFAULT-DENY: with no
        # run_allowlist opted in, every origin.run is refused. Exec runs on a
        # DEDICATED bounded pool (not the shared default that serves reads), so a
        # long/streamed run can never starve origin.describe/status.
        self.run_allowlist = run_allowlist or RunAllowlist()
        # P4a: the origin's own state dir is off-limits to fs writes over
        # origin.run. Always includes the dir holding the file-backed run
        # allowlist (the file a write would widen the gate through).
        dirs = list(protected_dirs or [])
        if self.run_allowlist.path:
            dirs.append(os.path.dirname(os.path.abspath(self.run_allowlist.path)))
        self.protected_dirs = sorted({os.path.abspath(d) for d in dirs if d})
        # P3 capability manifest — the origin's OWN statement of what it permits.
        # Explicit object > explicit path > ``origin-manifest.json`` beside the
        # run allowlist (else in the first protected/state dir). Those dirs are
        # P4a-protected, so the Hub can never rewrite the manifest over fs.push.
        # No file → the default: NO exec, NO fs.
        if manifest_path is None:
            base = (os.path.dirname(os.path.abspath(self.run_allowlist.path))
                    if self.run_allowlist.path
                    else (self.protected_dirs[0] if self.protected_dirs else None))
            manifest_path = _policy.manifest_path_for(base)
        self.manifest_path = manifest_path
        self.policy = manifest or _policy.OriginManifest.load(manifest_path)
        # The reporter advertises the run policy in describe (§3), so build it AFTER
        # the run gate and hand it the same allowlist object.
        self.reporter = reporter or CapabilityReporter(
            origin_id, engine=engine, allowlist=self.allowlist,
            run_allowlist=self.run_allowlist)
        self.reporter.policy = self.policy
        self.run_concurrency = max(1, int(run_concurrency))
        self._run_pool = ThreadPoolExecutor(
            max_workers=self.run_concurrency,
            thread_name_prefix=f"origin-run-{origin_id}")
        self._run_inflight = 0
        self._audit = audit
        # capabilities.probe seam (``now -> list[dict]``); None → origins_probe's
        # real PATH + credential-file probe on this box.
        self.cli_probe: Optional[Callable[[float], list]] = None
        # Gap #3 — a REMOTE origin streams a dispatched loop's progress back to the
        # Hub itself: when ``tail_client`` (this origin's live ChannelClient) is
        # set, every successful ``loop.start`` spawns a background tail that
        # pushes the loop's turn/state/result events off this box's OWN engine.
        # None (the default, and the in-process LocalOriginService, which tails
        # Hub-side) keeps loop.start a pure launch.
        self.tail_client: Any = None
        self.tail_interval: float = 1.0
        self._tail_tasks: set = set()

    def _probe_clis(self) -> dict:
        """``capabilities.probe``: this origin's honest per-CLI auth picture, the
        SAME record shape the Hub's local probe returns, so the Hub surfaces a
        remote origin exactly like its own. ``cli_probe`` (``now -> list``) is a
        test seam; default is :func:`mcp_loops.origins_probe.probe_local_clis`."""
        now = time.time()
        probe = self.cli_probe
        if probe is None:
            from mcp_loops import origins_probe
            probe = origins_probe.probe_local_clis
        return {"ok": True, "origin": self.origin_id,
                "cliCapabilities": list(probe(now)), "lastProbed": now}

    # engine is created lazily so importing this module never requires a live
    # 8771 (mirrors the dogfood bridge's lazy import).
    def _engine_client(self):
        if self._engine is None:
            from mcp_loops.client import LoopsMCP
            self._engine = LoopsMCP()
        return self._engine

    def manifest(self) -> dict:
        """The manifest this origin advertises in ``hello`` (ChannelClient default)."""
        return self.policy.to_dict()

    async def __call__(self, method: str, params: dict) -> dict:
        if method not in wire.RPC_METHODS:
            # defence in depth — the channel already validates the allowlist, but
            # the executor refuses anything off-list too (never a shell method).
            raise ValueError(f"method {method!r} is not on the capability allowlist")
        if method == "origin.run":
            return await self._run_exec(params)
        res = await asyncio.get_event_loop().run_in_executor(
            None, self._call_sync, method, params)
        if (method == "loop.start" and self.tail_client is not None
                and isinstance(res, dict) and not res.get("error")
                and not res.get("_reattached")):
            self._spawn_tail(params["name"])
        return res

    def _spawn_tail(self, name: str) -> None:
        """Stream ``name``'s progress to the Hub over :attr:`tail_client` until
        the run is terminal (gap #3 — the origin's engine stays the source of
        truth; the events only MIRROR it). Best-effort, never raises."""
        task = asyncio.ensure_future(tail_engine_loop(
            self.tail_client, self.origin_id, self._engine_client(), name,
            interval=self.tail_interval))
        self._tail_tasks.add(task)
        task.add_done_callback(self._tail_tasks.discard)

    # ── origin.run: gate → dedicated pool → audit (HUB-FABRIC §2, §5) ──────────
    async def _run_exec(self, params: dict) -> dict:
        """Handle ``origin.run`` off the SHARED read pool: fail-closed exec gate,
        bounded-concurrency rejection, execution on the dedicated run-pool, and an
        audit line with the REAL argv. Read RPCs stay on the default pool, so an
        exec surge never starves a status/describe call (§5.4)."""
        argv = params.get("argv")
        # (0) P3 manifest gate — the unit's verb must be one this origin
        # advertised (exec off by default; fs.read/fs.write are their own
        # capabilities, shape-locked to the fs_ops argv). Untagged ⇒ exec.
        verb = _policy.verb_for("origin.run", params.get("_verb"))
        reason = self.policy.check("origin.run", verb, params)
        if reason is not None:
            raise RunNotAllowed(
                f"origin {self.origin_id!r} refused origin.run ({verb}): {reason}")
        # (1) exec gate — fail-closed. An origin that did not opt into exec (or
        # opted into a program this argv isn't) refuses, channel stays up. This
        # is the SECOND lock for every verb: fs.read/fs.write also need cat/tee
        # admitted here, so argv-level rules an owner put on them still bind.
        if not self.run_allowlist.is_allowed(argv):
            prog = (os.path.basename(str(argv[0]))
                    if isinstance(argv, (list, tuple)) and argv else "<empty>")
            raise RunNotAllowed(
                f"origin {self.origin_id!r} refused origin.run for {prog!r}: not "
                f"permitted by its exec allowlist / argv policy. The origin must "
                f"opt the program in (and any argv-level rule must admit these "
                f"arguments) before the Hub may run it (§5.1(2), §5.3)")
        # (1b) P4a — never a path into the origin's own state dir, for ANY
        # allowed program (the Hub must not be able to rewrite the gate itself).
        # The implicit cwd is not an operand, but if the origin's own cwd
        # contains its state dir a non-read-only run is moved to a neutral dir.
        if not params.get("cwd"):
            moved = neutral_cwd(argv, self.protected_dirs)
            if moved is not None:
                params = {**params, "cwd": moved}
        hit = protected_write_target(argv, self.protected_dirs,
                                     cwd=params.get("cwd") or None,
                                     env=params.get("env"))
        if hit is not None:
            raise RunNotAllowed(
                f"origin {self.origin_id!r} refused origin.run: {hit!r} resolves "
                f"into the origin's own state dir, which is off-limits to "
                f"origin.run (hardening P4a)")
        # (2) concurrency cap — typed busy rejection, never an unbounded queue.
        if self._run_inflight >= self.run_concurrency:
            raise RunBusy(
                f"origin {self.origin_id!r} is at its origin.run concurrency cap "
                f"({self.run_concurrency}); retry once a slot frees (§5.4)")
        # (3) streaming vs one-shot. A streamed run (§2.4) needs a channel push
        # callback (`_push`, injected by ChannelClient) to emit run.chunk/run.end
        # mid-run; without one it falls back to buffering the one-shot envelope.
        stream = bool(params.get("stream"))
        push = params.get("_push")
        self._run_inflight += 1
        try:
            loop = asyncio.get_event_loop()
            if stream and callable(push):
                result = await loop.run_in_executor(
                    self._run_pool, self._exec_streamed, params, push)
            else:
                result = await loop.run_in_executor(
                    self._run_pool, exec_run, params)
        finally:
            self._run_inflight -= 1
        # (4) audit the REAL argv (§5.1(3)) — fail-soft, never raises into the run.
        self._audit_run(params, result)
        return result

    def _exec_streamed(self, params: dict, push: Callable[[str, dict], Any]) -> dict:
        """Run the streamed exec on the run-pool thread, forwarding each output
        slice to the channel push callback as a ``run.chunk`` and closing with a
        ``run.end`` (§2.4 seam 1). ``push(kind, data)`` marshals across the
        thread→event-loop boundary (ChannelClient owns that); we keep every push
        best-effort so a dropped telemetry frame never aborts the exec — the
        terminal envelope still returns and resolves the caller's rpc_result."""
        def on_chunk(fd: str, b64: str) -> None:
            try:
                push("run.chunk", {"fd": fd, "b64": b64})
            except Exception:  # noqa: BLE001 — telemetry is best-effort (§7)
                pass

        result = exec_run_streamed(params, on_chunk)
        try:
            push("run.end", {"exitCode": result.get("exitCode"),
                             "durationMs": result.get("durationMs")})
        except Exception:  # noqa: BLE001
            pass
        return result

    def _audit_run(self, params: dict, result: dict) -> None:
        if self._audit is None:
            return
        argv = params.get("argv")
        try:
            self._audit(
                "origin.run", now=time.time(),
                deviceId=self.origin_id,
                argv=list(argv) if isinstance(argv, (list, tuple)) else argv,
                cwd=params.get("cwd"),
                dispatchId=params.get("_dispatchId"),
                exitCode=result.get("exitCode"))
        except Exception:  # noqa: BLE001 — a lost audit line beats a crashed run
            pass

    # ── project resolution + gate ─────────────────────────────────────────────
    def _resolve_project(self, name: str) -> Optional[str]:
        """The Project a loop is bound to (``config.projectId``, or the pre-rename
        ``config.productId`` alias), or None when the loop is unlinked. Read
        straight from the live engine's config."""
        got = self._engine_client().loop_get(name)
        if isinstance(got, dict) and "error" in got:
            raise ValueError(f"unknown loop {name!r}: {got['error']}")
        config = got.get("config", {}) if isinstance(got, dict) else {}
        pid = config.get("projectId") or config.get("productId")
        return pid if pid else None

    # back-compat alias (pre-rename method name).
    _resolve_product = _resolve_project

    def _guard_start(self, name: str) -> Optional[str]:
        """Refuse a remote start whose bound Project the origin didn't opt in
        (§6.4). Returns the resolved project id on success (for the result)."""
        project_id = self._resolve_project(name)
        if not self.allowlist.is_allowed(project_id):
            raise ProjectNotAllowed(
                f"loop {name!r} (project {project_id or 'unlinked'!r}) is not on "
                f"this origin's dispatchable-Project allowlist; the origin must "
                f"opt it in before the plane may start it (§6.4)")
        return project_id

    def _prepare(self, name: str, config: Any) -> Optional[str]:
        """Save a Hub-shipped loop ``config`` on this origin's engine when the loop
        is absent. Returns ``"shipped"`` (saved now), ``"existing"`` (the origin
        already has the loop — its copy wins, the shipped one is ignored) or None
        (nothing shipped). Raises :class:`ProjectNotAllowed` when the shipped
        config's Project is not opted in, ValueError on a malformed/invalid one."""
        if config is None:
            return None
        if not isinstance(config, dict) or config.get("name") != name:
            raise ValueError(f"shipped config for {name!r} must be an object "
                             f"whose name is {name!r}")
        engine = self._engine_client()
        got = engine.loop_get(name)
        if isinstance(got, dict) and "error" not in got:
            return "existing"
        pid = config.get("projectId") or config.get("productId")
        if not self.allowlist.is_allowed(pid):
            raise ProjectNotAllowed(
                f"loop {name!r} (project {pid or 'unlinked'!r}) is not on this "
                f"origin's dispatchable-Project allowlist; the origin must opt it "
                f"in before the plane may prepare + start it (§6.4)")
        saved = engine.loop_save(config)
        if not isinstance(saved, dict) or saved.get("error"):
            detail = (saved or {}).get("errors") or (saved or {}).get("error")
            raise ValueError(f"origin could not save shipped config for "
                             f"{name!r}: {detail}")
        return "shipped"

    # ── the synchronous engine bridge (run off-thread) ────────────────────────
    def _call_sync(self, method: str, params: dict) -> dict:
        engine = self._engine_client()
        if method == "loop.list":
            return engine.loop_list()
        if method == "loop.get":
            return engine.loop_get(params["name"])
        if method == "loop.status":
            return engine.loop_status(params["name"], tail=params.get("tail"))
        if method == "products.list":
            try:
                return engine.call("loop_product_list", {})
            except Exception:  # noqa: BLE001 — honest degrade on older engines
                return {"products": [],
                        "note": "products.list unavailable on this engine"}
        if method == "origin.describe":
            # §6.4: the Hub may ask for a fresh auth-aware picture (probeAuth) over
            # exactly the CLIs a set of loops needs (candidates) — the live
            # onboarding check's seam. Absent params keep the cheap default probe.
            cands = params.get("candidates")
            cands = tuple(cands) if isinstance(cands, (list, tuple)) else None
            return self.reporter.describe(
                probe_auth=params.get("probeAuth"), candidates=cands)
        if method == "capabilities.probe":
            # READ-ONLY: the origin answers with its OWN subscription-CLI probe
            # (PATH + credential-file evidence on THIS box). No argv, no exec —
            # the manifest rpcs gate (Hub + origin) already admitted it.
            return self._probe_clis()
        if method == "loop.start":
            # Gap #3 — PREPARE: a Hub dispatching a loop this origin has never
            # seen ships its saved config. It is saved here only when the loop is
            # ABSENT (the origin's own copy always wins — never overwritten) and
            # only after the SHIPPED config's Project passes this origin's §6.4
            # allowlist, so preparing can't smuggle past the gate below.
            prepared = self._prepare(params["name"], params.get("config"))
            # GATE: the origin decides what's dispatchable (§6.4) BEFORE launch.
            project_id = self._guard_start(params["name"])
            # forward the dispatch-id (channel injected it as `_dispatchId`) so the
            # engine's loop_start runs LOCALLY keyed on it rather than re-routing
            # back through the hub — the seam that lets the origin-agent reach the
            # engine over the MCP socket without an infinite dispatch loop (G2.2).
            # Fall back for older engines whose loop_start predates the kwarg.
            did = params.get("_dispatchId")
            try:
                res = engine.loop_start(params["name"], slug=params.get("slug"),
                                        dispatch_id=did)
            except TypeError:
                res = engine.loop_start(params["name"], slug=params.get("slug"))
            if isinstance(res, dict):
                res.setdefault("projectId", project_id)     # canonical
                res.setdefault("productId", project_id)      # back-compat alias
                if prepared:
                    res.setdefault("prepared", prepared)
            return res
        if method == "loop.stop":
            # halting work is a de-escalation the origin always permits.
            return engine.loop_stop(params["name"])
        raise ValueError(f"unhandled method {method!r}")


# ── event-push adaptation (origin → hub; replaces the rsync mirror) ───────────
def engine_status_to_event(origin_id: str, line: dict) -> Optional[dict]:
    """Map one engine status/run/turn record (e.g. a ``status.jsonl`` line) to a
    wire event kind so a tailer can feed :meth:`ChannelClient.push_event`.
    Returns ``(kind, data)`` or None when the line isn't a pushable event. This
    is the adapter that lets pushed events REPLACE mirror-mtime freshness."""
    if not isinstance(line, dict):
        return None
    kind = line.get("event") or line.get("kind")
    mapping = {
        "run": "run", "run_start": "run", "run_end": "run",
        "turn": "turn", "turn_end": "turn",
        "status": "status", "result": "result",
    }
    wire_kind = mapping.get(kind)
    if wire_kind is not None and wire_kind in wire.EVENT_KINDS:
        return {"kind": wire_kind, "data": {k: v for k, v in line.items()
                                            if k not in ("event", "kind")}}
    # Fallback for the REAL engine status.jsonl schema (mcp_loops.report): a
    # per-turn agent report has NO explicit event/kind — it carries
    # ``{loop, agent, status, note, turn, ...}``. Map it to a ``turn`` event so a
    # connected origin's live turns REPLACE mirror-mtime freshness. Machinery
    # lines (``kind=="machinery"``) are explicitly skipped — they are engine
    # bookkeeping, not run progress.
    if kind == "machinery":
        return None
    # ``turn`` is often null on a real report line (mcp_loops.report writes the
    # engine's turn only when it knows it), so an agent-attributed report line
    # counts too — requiring a turn number dropped every real report (gap #3).
    if line.get("loop") and line.get("status") and (
            line.get("turn") is not None or line.get("agent")):
        return {"kind": "turn", "data": {
            "loop": line.get("loop"), "turn": line.get("turn"),
            "status": line.get("status"), "agent": line.get("agent"),
            "note": line.get("note")}}
    return None


async def push_engine_events(client, origin_id: str, lines: list[dict]) -> int:
    """Push a batch of engine status lines to the hub as wire events. Best-effort
    (§7 at-least-once): returns how many were pushed; a not-connected channel
    raises out of :meth:`push_event` so the caller can buffer + flush on
    reconnect. Kept tiny + injectable so a real tailer or a test can drive it."""
    pushed = 0
    for line in lines:
        ev = engine_status_to_event(origin_id, line)
        if ev is None:
            continue
        await client.push_event(ev["kind"], ev["data"])
        pushed += 1
    return pushed


async def tail_engine_loop(client, origin_id: str, engine: Any, name: str, *,
                           interval: float = 1.0, tail: int = 10_000,
                           max_polls: Optional[int] = None) -> int:
    """Gap #3 — the ORIGIN-side driver of :class:`StatusTailer`: poll this box's
    own engine (``engine.loop_status(name, tail=)`` — off the event loop, the
    engine client is synchronous) and push the loop's new turn lines + state
    changes to the Hub until the run is terminal, then flush once more. Works
    for the in-process engine and the LoopsMCP socket client alike. A read or
    push hiccup is swallowed (never kills the channel). Returns events pushed."""
    snap: dict = {}

    def _read() -> None:
        try:
            got = engine.loop_status(name, tail=tail)
        except Exception:  # noqa: BLE001 — a read hiccup retries next poll
            return
        if isinstance(got, dict) and "error" not in got:
            snap.clear()
            snap.update(got)

    tailer = StatusTailer(origin_id, name,
                          read_status=lambda: list(snap.get("recent") or []),
                          read_run=lambda: dict(snap.get("run") or {}))
    loop = asyncio.get_event_loop()
    pushed = polls = 0
    while True:
        await loop.run_in_executor(None, _read)
        try:
            pushed += await tailer.poll(client)
        except Exception:  # noqa: BLE001 — channel down: retry next poll
            pass
        polls += 1
        if tailer.done() or (max_polls is not None and polls >= max_polls):
            break
        await asyncio.sleep(interval)
    return pushed


class StatusTailer:
    """G2.4 — stream a running loop's live progress to the hub as wire events so a
    CONNECTED origin's turn-by-turn progress shows in the consolidated view
    (superseding rsync-mirror mtime). Bound to ONE loop; :meth:`poll` is
    incremental + idempotent:

      * per-turn status lines are pushed ONCE — a cursor tracks how many of the
        loop's ordered status lines have been sent, so a redelivery/re-poll never
        double-pushes (the consolidator also dedupes on seq, belt + suspenders);
      * a run-STATE change is pushed once (a last-state memo) as a ``run`` event,
        and a terminal state also emits a ``result`` event so the live view
        settles to done.

    The origin's engine stays the source of truth; these events only MIRROR it.
    ``read_status()`` returns the loop's full ordered status-line list (the tailer
    slices off what it already sent); ``read_run()`` (optional) returns the loop's
    run.json so state transitions are surfaced without an explicit engine event."""

    _TERMINAL = frozenset({"finished", "complete", "stopped", "error"})

    def __init__(self, origin_id: str, loop: str, *,
                 read_status: Callable[[], list], read_run: Optional[Callable[[], dict]] = None):
        self.origin_id = origin_id
        self.loop = loop
        self._read_status = read_status
        self._read_run = read_run
        self._cursor = 0
        self._last_state: Optional[str] = None

    async def poll(self, client) -> int:
        """Push any NEW status lines + a changed run-state for this loop. Returns
        how many wire events were pushed this call."""
        pushed = 0
        lines = list(self._read_status() or [])
        new = lines[self._cursor:]
        if new:
            pushed += await push_engine_events(client, self.origin_id, new)
            self._cursor = len(lines)
        if self._read_run is not None:
            run = self._read_run() or {}
            state = run.get("state")
            if state and state != self._last_state:
                self._last_state = state
                await client.push_event("run", {"loop": self.loop, "state": state})
                pushed += 1
                if state in self._TERMINAL:
                    await client.push_event(
                        "result", {"loop": self.loop, "state": state})
                    pushed += 1
        return pushed

    def done(self) -> bool:
        """True once the loop's run reached a terminal state (a driver stops
        polling). False when there is no run reader (driver decides otherwise)."""
        if self._read_run is None:
            return False
        return (self._read_run() or {}).get("state") in self._TERMINAL
