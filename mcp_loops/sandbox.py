"""Loop cage — Tier-A isolation for every loop agent session (both spawn paths).

Design: ``docs/LOOP-ISOLATION-DESIGN.md`` + ``docs/ISOLATION-BUILD-PLAN.md``;
host audit that shaped THIS module: ``_output/isolation-build/capability.md``.

What one caged session looks like (outermost first)::

    systemd-run --user --scope --quiet --collect \\
        --slice=<prefix>_<loop>_<h8>.slice --unit=<slice-stem>_<agent>_<nonce> --
      [unshare box …]                               # Step 3, only when the probe says so
      <python> <this file> exec --unix=deny --landlock=on --fs=on --rw=… --net-deny=22 --
        <the agent argv, unchanged>

* **Step 1 (cgroup):** the per-loop cgroup is a *transient slice* created over
  D-Bus (``StartTransientUnit``) with ``MemoryMax``/``MemorySwapMax``/
  ``CPUQuota``/``TasksMax`` — non-blocking, systemd-owned, limits read back from
  cgroupfs before use. Each session joins it as a ``--scope`` that is exec'd BY
  the pane shell / the headless ``Popen`` child, so ``spawn_session`` returns and
  ``inject_input``/capture/killpg behave exactly as before; nothing in the
  runtime blocks on it. Stopping the slice kills every process of the loop
  (Step 5 teardown barrier).
* **Step 0 (no docker / no escalation):** ``setpriv --clear-groups`` is EPERM
  unprivileged on this host and every ``systemd --user`` child inherits gid 989,
  so the group cannot be dropped. Instead the exec stage sets
  ``PR_SET_NO_NEW_PRIVS`` and a seccomp filter that fails ``socket(AF_UNIX, …)``
  with EACCES (``socketpair`` stays allowed). That makes ``/run/docker.sock``,
  the user systemd bus (every prod service is a ``--user`` unit), the host tmux
  ``default`` socket and the worker UDS unreachable. ``io_uring_setup`` and the
  x32 ABI get ENOSYS (they would sidestep the filter), foreign arches are killed.
* **Kill guarantee without a PID ns:** Landlock (ABI ≥ 6) ``SCOPE_SIGNAL`` +
  ``SCOPE_ABSTRACT_UNIX_SOCKET`` — a caged ``pkill -f`` cannot signal anything
  outside the cage (EPERM); its own children are unaffected.
* **FS write confinement (Landlock, ABI ≥ 1):** writes are allowed only beneath
  :func:`base_write_paths` (/tmp, /var/tmp, /dev, this cwd's
  ``~/.claude/projects/<cwd>`` + ``~/.claude/history.jsonl``) and the
  engine-computed ``--rw`` list (the loop worktree, its ``.git/worktrees/<n>`` +
  object store + ``refs/heads/loop/`` only, the loop's output dir, and the two
  report logs in its status dir). ``~/.ssh``, shell rc files, ``~/.config``,
  ``~/.claude.json`` / ``settings*.json`` / ``shell-snapshots`` (all read or
  executed by UNcaged sessions), other checkouts and the engine's run.json /
  config.json are EACCES. Reads stay open. Tool caches go to
  :func:`cage_cache_dir`.
* **TCP connect deny (Landlock, ABI ≥ 4):** ``connect()`` to ports in
  ``net_deny`` (default 22 — a caged ``ssh 127.0.0.1`` would be a fresh uncaged
  login) is EACCES; ``LOOPS_ISOLATION_NET_DENY`` adds ports.
* **Config may only tighten:** a loop config can raise ``mode`` to strict and
  lower limits; ``mode: off``, ``unixSockets: allow`` and limits above
  :data:`DEFAULTS` are ignored (loudly) unless the operator sets
  ``LOOPS_ISOLATION_CONFIG_LOOSEN=1`` — caged agents can reach loop_save.
* **Step 3 (namespaces):** capability-probed. When ``unshare --user
  --map-root-user --pid --mount`` works, the box (PID ns + private /proc + a
  masked mount view) is prefixed. When it does not (this host: AppArmor
  ``apparmor_restrict_unprivileged_userns=1``) ``strict`` refuses the loop and
  ``best-effort`` runs without it and says so in :attr:`Policy.status_lines`.

Knobs: ``LOOPS_ISOLATION`` = ``off`` | ``best-effort`` (default) | ``strict``
(env wins over loop config ``isolation.mode``). ``off`` ⇒ :func:`wrap_argv`
returns the argv untouched (golden). ``LOOPS_ISOLATION_PREFIX`` (unit prefix,
default ``lyloop``; tests use their own) and ``LOOPS_ISOLATION_RESERVE_MEM``
(runtime RAM reserve, default 2G).

Honest limits (Tier A, this host): READS are not confined (``~/.ssh/id_*``,
``~/.claude/.credentials.json``, other checkouts are readable → exfiltration
over the open egress); host ``/proc`` stays readable; disk space is uncapped;
egress is open except the denied ports (Step 4 is out of scope), and loopback
services on other ports (e.g. :8771 loop MCP, which managers need) stay
reachable; UDP is not covered by Landlock net; /tmp is shared with the host.
A legacy loop without a workspace gets its slug checkout writable (logged).
Single-UID — not a multi-tenant boundary.

This file is stdlib-only on purpose: the worker daemon runs the exec stage as
``<python> /abs/path/sandbox.py exec …`` without importing the package.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import platform
import re
import shlex
import struct
import subprocess
import sys
from dataclasses import asdict, dataclass, field, replace
from typing import Callable, Iterable, Mapping, Optional

SANDBOX_PY = os.path.abspath(__file__)

MODE_ENV = "LOOPS_ISOLATION"
PREFIX_ENV = "LOOPS_ISOLATION_PREFIX"
RESERVE_MEM_ENV = "LOOPS_ISOLATION_RESERVE_MEM"
# Operator-only escape hatch: a loop CONFIG may only tighten the cage (mode
# best-effort→strict, lower limits). Loosening keys in config (mode off,
# unixSockets allow, limits above the defaults) are ignored unless this is 1.
LOOSEN_ENV = "LOOPS_ISOLATION_CONFIG_LOOSEN"
NET_DENY_ENV = "LOOPS_ISOLATION_NET_DENY"
DEFAULT_NET_DENY = (22,)               # host sshd: `ssh 127.0.0.1` = a fresh uncaged login
MODES = ("off", "best-effort", "strict")
DEFAULT_MODE = "best-effort"
DEFAULT_PREFIX = "lyloop"
DEFAULT_RESERVE_MEM = "2G"
RESERVE_CPUS = 1                      # cores the runtime always keeps
UNIX_POLICIES = ("deny", "allow")
DEFAULTS = {"memoryMax": "4G", "cpuQuota": "300%", "tasksMax": 1024,
            "unixSockets": "deny"}
_ISO_KEYS = frozenset({"mode", "memoryMax", "cpuQuota", "tasksMax", "unixSockets"})
_OFF_WORDS = frozenset({"off", "0", "false", "no", "none"})


class IsolationRefused(RuntimeError):
    """``strict`` isolation requested but a required primitive is unavailable."""


# ── config ──────────────────────────────────────────────────────────────────
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?\s*$", re.I)
_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def parse_size(v) -> int:
    """``"4G"``/``"512M"``/``268435456`` → bytes (binary units)."""
    if isinstance(v, bool):
        raise ValueError(f"bad size {v!r}")
    if isinstance(v, int):
        if v <= 0:
            raise ValueError(f"size must be > 0, got {v}")
        return v
    m = _SIZE.match(str(v))
    if not m:
        raise ValueError(f"bad size {v!r} (want e.g. 4G, 512M)")
    n = int(float(m.group(1)) * _UNITS[m.group(2).upper()])
    if n <= 0:
        raise ValueError(f"size must be > 0, got {v!r}")
    return n


def parse_cpu(v) -> int:
    """``"300%"``/``300`` → percent of one CPU."""
    if isinstance(v, bool):
        raise ValueError(f"bad cpuQuota {v!r}")
    s = str(v).strip().rstrip("%")
    try:
        n = int(float(s))
    except ValueError:
        raise ValueError(f"bad cpuQuota {v!r} (want e.g. 300%)") from None
    if n <= 0:
        raise ValueError(f"cpuQuota must be > 0, got {v!r}")
    return n


def validate_isolation(raw) -> tuple[dict, list]:
    """Normalize a loop config ``isolation`` block → ``(norm, errors)``.

    Only keys the author set are retained (defaults are applied at plan time, so
    a config's normalized form doesn't churn when a default moves)."""
    errs: list = []
    if raw is None:
        return {}, errs
    if not isinstance(raw, dict):
        return {}, ["`isolation` must be an object"]
    out: dict = {}
    for k in sorted(set(raw) - _ISO_KEYS):
        errs.append(f"`isolation.{k}` is not a known key (known: {sorted(_ISO_KEYS)})")
    if "mode" in raw:
        m = str(raw["mode"]).strip().lower()
        if m in _OFF_WORDS:
            m = "off"
        if m not in MODES:
            errs.append(f"`isolation.mode` must be one of {list(MODES)}, got {raw['mode']!r}")
        else:
            out["mode"] = m
    for key, parse in (("memoryMax", parse_size), ("cpuQuota", parse_cpu)):
        if key in raw:
            try:
                parse(raw[key])
                out[key] = raw[key]
            except ValueError as e:
                errs.append(f"`isolation.{key}`: {e}")
    if "tasksMax" in raw:
        t = raw["tasksMax"]
        if isinstance(t, bool) or not isinstance(t, int) or t < 16:
            errs.append(f"`isolation.tasksMax` must be an integer >= 16, got {t!r}")
        else:
            out["tasksMax"] = t
    if "unixSockets" in raw:
        u = str(raw["unixSockets"]).strip().lower()
        if u not in UNIX_POLICIES:
            errs.append(f"`isolation.unixSockets` must be deny|allow, got {raw['unixSockets']!r}")
        else:
            out["unixSockets"] = u
    return out, errs


_RANK = {"off": 0, "best-effort": 1, "strict": 2}


def _loosen_allowed(env: Mapping[str, str]) -> bool:
    return (env.get(LOOSEN_ENV) or "").strip() == "1"


def resolve_mode(iso: Optional[Mapping] = None,
                 env: Optional[Mapping[str, str]] = None) -> str:
    """The operator env ``LOOPS_ISOLATION`` sets the floor (default best-effort;
    ``off`` is the operator kill-switch and wins outright). A loop config's
    ``isolation.mode`` may only TIGHTEN it (best-effort → strict); a config
    ``off`` is ignored unless ``LOOPS_ISOLATION_CONFIG_LOOSEN=1``. An
    unrecognised env value raises (a typo must not silently pick a mode)."""
    env = os.environ if env is None else env
    raw = env.get(MODE_ENV)
    base = DEFAULT_MODE
    if raw is not None and raw.strip():
        v = raw.strip().lower()
        v = "off" if v in _OFF_WORDS else v
        if v not in MODES:
            raise ValueError(f"{MODE_ENV}={raw!r} is not one of {list(MODES)}")
        if v == "off":
            return "off"
        base = v
    cfg = (iso or {}).get("mode")
    if cfg not in _RANK:
        return base
    if _RANK[cfg] > _RANK[base] or _loosen_allowed(env):
        return cfg
    return base


def effective_isolation(iso: Optional[Mapping] = None,
                        env: Optional[Mapping[str, str]] = None) -> tuple[dict, list]:
    """Strip every config key that would LOOSEN the cage (unless the operator
    set ``LOOPS_ISOLATION_CONFIG_LOOSEN=1``) → ``(iso, notes)``. A caged agent can
    reach loop_save/start_loop, so a config must never be a way out."""
    env = os.environ if env is None else env
    iso = dict(iso or {})
    if _loosen_allowed(env):
        return iso, ([f"{LOOSEN_ENV}=1 — loop config may loosen the cage"]
                     if iso else [])
    notes: list = []
    m = iso.get("mode")
    if m in _RANK and _RANK[m] < _RANK[DEFAULT_MODE]:
        notes.append(f"config isolation.mode={m} IGNORED (a config may only tighten; "
                     f"the operator sets {MODE_ENV})")
        iso.pop("mode")
    if iso.get("unixSockets") == "allow":
        notes.append("config isolation.unixSockets=allow IGNORED (a config may only "
                     "tighten) — AF_UNIX stays denied")
        iso.pop("unixSockets")
    for key, parse in (("memoryMax", parse_size), ("cpuQuota", parse_cpu)):
        if key in iso and parse(iso[key]) > parse(DEFAULTS[key]):
            notes.append(f"config isolation.{key}={iso[key]} above the default "
                         f"{DEFAULTS[key]} IGNORED (a config may only tighten)")
            iso.pop(key)
    if "tasksMax" in iso and int(iso["tasksMax"]) > int(DEFAULTS["tasksMax"]):
        notes.append(f"config isolation.tasksMax={iso['tasksMax']} above the default "
                     f"{DEFAULTS['tasksMax']} IGNORED (a config may only tighten)")
        iso.pop("tasksMax")
    return iso, notes


# ── limits ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Limits:
    memory_max: int          # bytes
    cpu_quota_pct: int       # % of one CPU
    tasks_max: int

    def describe(self) -> str:
        return (f"MemoryMax={self.memory_max >> 20}M CPUQuota={self.cpu_quota_pct}% "
                f"TasksMax={self.tasks_max} MemorySwapMax=0")


def _mem_total() -> int:
    """Physical RAM in bytes. ``/proc/meminfo`` is Linux-only (B-0: every macOS
    loop start died on it), so fall back to sysconf, then ``sysctl hw.memsize``."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        n = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        if n > 0:
            return n
    except (ValueError, OSError, AttributeError):
        pass
    try:
        r = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                           text=True, timeout=5)
        return int(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise OSError(f"MemTotal not found: {e}") from e


def resolve_limits(iso: Optional[Mapping] = None, *, mem_total: Optional[int] = None,
                   ncpu: Optional[int] = None,
                   env: Optional[Mapping[str, str]] = None) -> tuple[Limits, list]:
    """Config (or defaults) clamped so the runtime keeps a reserve: memory ≤
    MemTotal − reserve, CPU ≤ (ncpu − 1) × 100 %. Returns ``(limits, notes)``."""
    iso = iso or {}
    env = os.environ if env is None else env
    mem_total = _mem_total() if mem_total is None else mem_total
    ncpu = (os.cpu_count() or 1) if ncpu is None else ncpu
    reserve = parse_size(env.get(RESERVE_MEM_ENV) or DEFAULT_RESERVE_MEM)
    notes: list = []
    mem = parse_size(iso.get("memoryMax", DEFAULTS["memoryMax"]))
    mem_cap = max(mem_total - reserve, 256 << 20)
    if mem > mem_cap:
        notes.append(f"memoryMax clamped {mem >> 20}M → {mem_cap >> 20}M "
                     f"(host {mem_total >> 20}M − reserve {reserve >> 20}M)")
        mem = mem_cap
    cpu = parse_cpu(iso.get("cpuQuota", DEFAULTS["cpuQuota"]))
    cpu_cap = max(ncpu - RESERVE_CPUS, 1) * 100
    if cpu > cpu_cap:
        notes.append(f"cpuQuota clamped {cpu}% → {cpu_cap}% ({ncpu} CPUs − {RESERVE_CPUS} reserved)")
        cpu = cpu_cap
    tasks = int(iso.get("tasksMax", DEFAULTS["tasksMax"]))
    return Limits(mem, cpu, tasks), notes


# ── capability probe ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Capabilities:
    systemd_user: bool
    systemd_reason: str
    seccomp: bool
    seccomp_reason: str
    landlock_abi: int
    userns_box: bool
    userns_reason: str


_ARCH = {  # machine → (AUDIT_ARCH, __NR_socket, __NR_io_uring_setup, x32 bit?)
    "x86_64": (0xC000003E, 41, 425, True),
    "aarch64": (0xC00000B7, 198, 425, False),
}
_probe_cache: Optional[Capabilities] = None
Runner = Callable[..., subprocess.CompletedProcess]


def _libc():
    lib = ctypes.CDLL(None, use_errno=True)
    lib.syscall.restype = ctypes.c_long
    return lib


def landlock_abi() -> int:
    try:
        v = _libc().syscall(444, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
        return int(v) if v > 0 else 0
    except Exception:  # noqa: BLE001
        return 0


def _last_line(s: str) -> str:
    lines = [ln.strip() for ln in (s or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def probe_capabilities(run: Runner = subprocess.run, *, refresh: bool = False) -> Capabilities:
    """Probe this host once (cached). ``run`` is injectable for tests."""
    global _probe_cache
    if _probe_cache is not None and not refresh and run is subprocess.run:
        return _probe_cache
    kw = dict(capture_output=True, text=True, timeout=10)
    # systemd --user + delegated controllers
    try:
        r = run(["systemctl", "--user", "is-system-running"], **kw)
        state = (r.stdout or "").strip()
        sd_ok = state in ("running", "degraded", "starting")
        sd_reason = f"systemctl --user is-system-running = {state or _last_line(r.stderr)!r}"
    except Exception as e:  # noqa: BLE001
        sd_ok, sd_reason = False, f"systemctl --user unavailable: {e}"
    mach = platform.machine()
    sc_ok = mach in _ARCH and os.path.exists("/proc/self/status")
    sc_reason = "" if sc_ok else f"no seccomp filter for arch {mach!r}"
    # userns box: the exact unshare flags the box uses
    try:
        r = run(["unshare", "--user", "--map-root-user", "--pid", "--fork",
                 "--mount", "--mount-proc", "true"], **kw)
        ns_ok = r.returncode == 0
        ns_reason = "" if ns_ok else (_last_line(r.stderr) or f"unshare rc={r.returncode}")
    except Exception as e:  # noqa: BLE001
        ns_ok, ns_reason = False, f"unshare unavailable: {e}"
    if not ns_ok:
        try:
            with open("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") as fh:
                if fh.read().strip() == "1":
                    ns_reason += " (kernel.apparmor_restrict_unprivileged_userns=1)"
        except OSError:
            pass
    caps = Capabilities(sd_ok, sd_reason, sc_ok, sc_reason, landlock_abi(), ns_ok, ns_reason)
    if run is subprocess.run:
        _probe_cache = caps
    return caps


# ── policy ──────────────────────────────────────────────────────────────────
def _safe(s: str, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:n] or "x"


def slice_stem(loop: str, prefix: Optional[str] = None,
               env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    prefix = _safe(prefix or env.get(PREFIX_ENV) or DEFAULT_PREFIX, 24)
    h8 = hashlib.sha1(loop.encode()).hexdigest()[:8]
    # no '-' anywhere: systemd treats '-' in a slice name as nesting
    return f"{prefix}_{_safe(loop)}_{h8}"


@dataclass(frozen=True)
class Policy:
    mode: str                      # best-effort | strict
    loop: str
    slice: str                     # "<stem>.slice" ('' ⇒ no cgroup)
    limits: Optional[Limits]
    unix_sockets: str              # deny | allow
    landlock: bool
    namespaces: bool
    status_lines: tuple = field(default_factory=tuple)
    # Landlock FS: writes allowed ONLY beneath base_write_paths() + these
    # (workspace, its git dirs, loop status/output dirs). Reads stay open.
    fs_confine: bool = False
    write_paths: tuple = field(default_factory=tuple)
    # Landlock NET (ABI ≥ 4): TCP connect() to these ports is denied.
    net_deny: tuple = field(default_factory=tuple)

    @property
    def stem(self) -> str:
        return self.slice[:-len(".slice")] if self.slice else ""


def host_unsupported(system: Optional[str] = None) -> str:
    """Why this host has no cage layers at all ("" ⇒ it may have some). Every
    layer (systemd slice, seccomp, Landlock, userns) is Linux-only; on macOS /
    Windows the probe would also issue a Linux syscall number via ctypes."""
    system = system or platform.system()
    return "" if system == "Linux" else f"no cage layers on {system or 'this OS'}"


def plan(loop: str, iso: Optional[Mapping] = None, *,
         env: Optional[Mapping[str, str]] = None,
         caps: Optional[Capabilities] = None,
         mem_total: Optional[int] = None, ncpu: Optional[int] = None,
         prefix: Optional[str] = None) -> Optional[Policy]:
    """Decide how ``loop``'s sessions are caged. ``None`` ⇒ isolation off
    (callers must then produce today's exact argv). Raises
    :class:`IsolationRefused` in ``strict`` mode when any layer is missing."""
    env = os.environ if env is None else env
    mode = resolve_mode(iso, env)
    if mode == "off":
        return None
    unsupported = host_unsupported()
    if unsupported:
        # Before resolve_limits / probe_capabilities: neither is portable.
        if mode == "strict":
            raise IsolationRefused(
                f"LOOPS_ISOLATION=strict refuses loop {loop!r}: {unsupported}")
        print(f"[isolation] {loop}: isolation off: {unsupported}", file=sys.stderr)
        return None
    iso, cfg_notes = effective_isolation(iso, env)
    caps = caps or probe_capabilities()
    limits, notes = resolve_limits(iso, mem_total=mem_total, ncpu=ncpu, env=env)
    notes = cfg_notes + notes
    net_deny = net_deny_ports(env)
    unix = iso.get("unixSockets", DEFAULTS["unixSockets"])
    missing = []
    if not caps.systemd_user:
        missing.append(f"cgroup slice unavailable: {caps.systemd_reason}")
    if not caps.seccomp:
        missing.append(f"seccomp unavailable: {caps.seccomp_reason}")
    if caps.landlock_abi < 6:
        missing.append(f"landlock signal scope unavailable: ABI {caps.landlock_abi} < 6")
    if caps.landlock_abi < 1:
        missing.append("filesystem write confinement unavailable: no Landlock")
    if caps.landlock_abi < 4 and net_deny:
        missing.append(f"landlock net unavailable (ABI {caps.landlock_abi} < 4): "
                       f"TCP connect to {list(net_deny)} NOT denied")
    if not caps.userns_box:
        missing.append(f"namespaces unavailable: {caps.userns_reason}")
    if mode == "strict":
        if unix == "allow":
            missing.append("isolation.unixSockets=allow re-opens docker.sock/systemd bus")
        if missing:
            raise IsolationRefused(
                f"LOOPS_ISOLATION=strict refuses loop {loop!r}: " + "; ".join(missing))
    stem = slice_stem(loop, prefix, env)
    lines = [f"isolation: {mode} — slice {stem}.slice ({limits.describe()})"
             if caps.systemd_user else f"isolation: {mode} — NO cgroup"]
    lines += [f"isolation: {n}" for n in notes]
    if unix == "allow":
        lines.append("isolation: WARNING unixSockets=allow — docker.sock, the user "
                     "systemd bus and the host tmux socket are REACHABLE from this loop")
    lines += [f"isolation: {m}" for m in missing]
    fs = caps.landlock_abi >= 1
    if fs:
        lines.append("isolation: landlock FS — writes confined to the workspace, its git "
                     "dirs, the loop status/output dirs, /tmp, /dev and claude session "
                     "state (~/.ssh, shell rc files, ~/.config, other checkouts: EACCES)")
    if caps.landlock_abi >= 4 and net_deny:
        lines.append(f"isolation: landlock net — TCP connect denied to ports {list(net_deny)}")
    if not caps.userns_box:
        lines.append("isolation: running Steps 0+1 + Landlock only (no PID/mount/net "
                     "namespaces)")
    return Policy(mode=mode, loop=loop,
                  slice=f"{stem}.slice" if caps.systemd_user else "",
                  limits=limits if caps.systemd_user else None,
                  unix_sockets=unix, landlock=caps.landlock_abi >= 6,
                  namespaces=caps.userns_box, status_lines=tuple(lines),
                  fs_confine=fs,
                  net_deny=net_deny if caps.landlock_abi >= 4 else ())


def net_deny_ports(env: Optional[Mapping[str, str]] = None) -> tuple:
    """Ports caged agents may not TCP-connect to: ``LOOPS_ISOLATION_NET_DENY``
    (comma list; the operator may only ADD to the default ``22``)."""
    env = os.environ if env is None else env
    ports = set(DEFAULT_NET_DENY)
    for tok in (env.get(NET_DENY_ENV) or "").replace(" ", "").split(","):
        if tok:
            n = int(tok)
            if not 1 <= n <= 65535:
                raise ValueError(f"{NET_DENY_ENV}: port {n} out of range")
            ports.add(n)
    return tuple(sorted(ports))


def with_write_paths(p: Optional[Policy], paths: Iterable[str]) -> Optional[Policy]:
    """The planned policy plus extra writable paths (absolute, de-duplicated,
    order-stable). ``None`` stays ``None``."""
    if p is None:
        return None
    out = list(p.write_paths)
    for x in paths:
        if not x:
            continue
        x = os.path.abspath(x)
        if x not in out:
            out.append(x)
    return replace(p, write_paths=tuple(out))


class CageExposesGitDir(IsolationRefused):
    """A cage write root would contain a repo's real ``.git/`` directory."""


def exposed_git_dir(cwd: str, run: Runner = subprocess.run) -> Optional[str]:
    """The repo's real git dir when it lies AT/UNDER ``cwd`` (a shared checkout
    with a ``.git/`` DIRECTORY, not a linked worktree's ``.git`` gitfile), else
    ``None``. Landlock grants are allow-only and recursive, so granting such a
    cwd makes ``.git/hooks`` + ``.git/config`` writable — a hook then runs
    UNCAGED on the next commit by anyone in that checkout (cage escape)."""
    root = os.path.realpath(cwd)
    found = []
    dotgit = os.path.join(cwd, ".git")
    if os.path.isdir(dotgit):
        found.append(dotgit)
    try:
        r = run(["git", "-C", cwd, "rev-parse", "--absolute-git-dir", "--git-common-dir"],
                capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            for d in (r.stdout or "").split("\n")[:2]:
                if d.strip():
                    found.append(os.path.join(cwd, d.strip()))
    except Exception:  # noqa: BLE001 — git absent: the .git/ dir check still stands
        pass
    for d in found:
        rd = os.path.realpath(d)
        if rd == root or rd.startswith(root + os.sep):
            # a linked worktree's own gitdir lives in the COMMON dir, never here
            return d
    return None


def git_write_paths(workspace: str, run: Runner = subprocess.run) -> list:
    """What a commit in a linked worktree writes OUTSIDE it: its own gitdir
    (``.git/worktrees/<n>``), the shared object store, and the ref + reflog
    DIRECTORY of the checked-out branch only (``refs/heads/loop/``) — never the
    common ``config``/``hooks``/``refs/heads/master`` of the (prod) checkout."""
    try:
        r = run(["git", "-C", workspace, "rev-parse", "--absolute-git-dir",
                 "--git-common-dir", "--symbolic-full-name", "HEAD"],
                capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return []
    parts = (r.stdout or "").split("\n")
    if r.returncode != 0 or len(parts) < 3:
        return []
    gitdir, common, ref = parts[0].strip(), parts[1].strip(), parts[2].strip()
    common = os.path.normpath(os.path.join(workspace, common)) if common else ""
    out = [gitdir]
    if common and common != gitdir:
        out.append(os.path.join(common, "objects"))
        if ref.startswith("refs/heads/"):
            rdir = os.path.dirname(ref)
            out += [os.path.join(common, rdir), os.path.join(common, "logs", rdir)]
    return out


def to_spec(p: Optional[Policy]) -> Optional[dict]:
    """JSON-safe form for the ``spawn_session`` ``sandbox`` param / session md."""
    if p is None:
        return None
    d = asdict(p)
    d["status_lines"] = list(p.status_lines)
    d["write_paths"] = list(p.write_paths)
    d["net_deny"] = list(p.net_deny)
    return d


def from_spec(d: Optional[Mapping]) -> Optional[Policy]:
    """Inverse of :func:`to_spec`; validates so a bad spec fails closed."""
    if d is None:
        return None
    if not isinstance(d, Mapping):
        raise ValueError("sandbox spec must be an object")
    known = {"mode", "loop", "slice", "limits", "unix_sockets", "landlock",
             "namespaces", "status_lines", "fs_confine", "write_paths", "net_deny"}
    extra = set(d) - known
    if extra:
        raise ValueError(f"sandbox spec: unknown keys {sorted(extra)}")
    if d.get("mode") not in ("best-effort", "strict"):
        raise ValueError(f"sandbox spec: bad mode {d.get('mode')!r}")
    sl = str(d.get("slice") or "")
    if sl and not re.fullmatch(r"[A-Za-z0-9_]+\.slice", sl):
        raise ValueError(f"sandbox spec: bad slice {sl!r}")
    if d.get("unix_sockets") not in UNIX_POLICIES:
        raise ValueError(f"sandbox spec: bad unix_sockets {d.get('unix_sockets')!r}")
    lim = d.get("limits")
    limits = Limits(int(lim["memory_max"]), int(lim["cpu_quota_pct"]),
                    int(lim["tasks_max"])) if lim else None
    if sl and limits is None:
        raise ValueError("sandbox spec: slice without limits")
    wp = tuple(d.get("write_paths") or ())
    for x in wp:
        if not isinstance(x, str) or not os.path.isabs(x) or "\n" in x or "\0" in x:
            raise ValueError(f"sandbox spec: bad write path {x!r}")
    nd = tuple(d.get("net_deny") or ())
    for n in nd:
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 65535:
            raise ValueError(f"sandbox spec: bad net_deny port {n!r}")
    return Policy(mode=d["mode"], loop=str(d.get("loop") or ""), slice=sl,
                  limits=limits, unix_sockets=d["unix_sockets"],
                  landlock=bool(d.get("landlock")), namespaces=bool(d.get("namespaces")),
                  status_lines=tuple(d.get("status_lines") or ()),
                  fs_confine=bool(d.get("fs_confine")), write_paths=wp, net_deny=nd)


# ── argv ────────────────────────────────────────────────────────────────────
def scope_unit(p: Policy, agent: str, nonce: str) -> str:
    return f"{p.stem}_{_safe(agent, 24)}_{_safe(nonce, 16)}"


def box_argv(python: str, uid: Optional[int] = None, gid: Optional[int] = None) -> list:
    """Step 3 box (only used when the probe says userns works): root-in-userns
    builds a PID ns + private /proc + masked mount view, then a nested userns
    maps the caller's uid back so the agent is not 'root' (claude refuses
    bypass mode as root)."""
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    return ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--kill-child",
            "--mount", "--mount-proc", "--propagation", "private",
            python, SANDBOX_PY, "boxinit", "--",
            "unshare", "--user", f"--map-user={uid}", f"--map-group={gid}", "--"]


def cage_prefix(p: Optional[Policy], *, agent: str, nonce: str,
                python: Optional[str] = None) -> list:
    """Everything that goes in front of the agent argv (``[]`` when off)."""
    if p is None:
        return []
    python = python or sys.executable
    pre: list = []
    if p.slice:
        pre += ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                f"--slice={p.slice}", f"--unit={scope_unit(p, agent, nonce)}", "--"]
    if p.namespaces:
        pre += box_argv(python)
    pre += [python, SANDBOX_PY, "exec", f"--unix={p.unix_sockets}",
            f"--landlock={'on' if p.landlock else 'off'}"]
    if p.fs_confine:
        pre += ["--fs=on"] + [f"--rw={x}" for x in p.write_paths]
    if p.net_deny:
        pre.append("--net-deny=" + ",".join(str(n) for n in p.net_deny))
    return pre + ["--"]


def wrap_argv(argv: Iterable[str], p: Optional[Policy], *, agent: str, nonce: str,
              python: Optional[str] = None) -> list:
    """The caged argv; ``p is None`` ⇒ ``list(argv)`` exactly (golden)."""
    return cage_prefix(p, agent=agent, nonce=nonce, python=python) + list(argv)


def wrap_shell(cmd: str, p: Optional[Policy], *, agent: str, nonce: str,
               python: Optional[str] = None) -> str:
    """Shell-string form for the tmux pane (``bash -lc``). ``cmd`` must be a
    simple command (words only); leading ``VAR=val`` assignments stay in front
    of the prefix so they reach the agent through the scope + exec stage."""
    if p is None:
        return cmd
    return f"{shlex.join(cage_prefix(p, agent=agent, nonce=nonce, python=python))} {cmd}"


# ── slice lifecycle (Step 1 / Step 5) ───────────────────────────────────────
def _usec_per_sec(pct: int) -> int:
    return pct * 10_000


def ensure_slice(p: Policy, run: Runner = subprocess.run) -> dict:
    """Create (or re-limit) the loop's transient slice and READ BACK the limits
    from cgroupfs. Raises RuntimeError if they don't match — a cage whose limits
    are not verified must never be used."""
    if not p.slice or p.limits is None:
        raise RuntimeError("policy has no cgroup slice")
    lim = p.limits
    props = ["MemoryMax", "t", str(lim.memory_max), "MemorySwapMax", "t", "0",
             "TasksMax", "t", str(lim.tasks_max),
             "CPUQuotaPerSecUSec", "t", str(_usec_per_sec(lim.cpu_quota_pct))]
    kw = dict(capture_output=True, text=True, timeout=20)
    state = run(["systemctl", "--user", "is-active", p.slice], **kw).stdout.strip()
    if state != "active":
        r = run(["busctl", "--user", "call", "org.freedesktop.systemd1",
                 "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                 "StartTransientUnit", "ssa(sv)a(sa(sv))", p.slice, "fail",
                 str(len(props) // 3 + 1), *props,
                 "Description", "s", f"loop cage {p.loop}", "0"], **kw)
        if r.returncode != 0:
            raise RuntimeError(f"cannot create {p.slice}: {_last_line(r.stderr)}")
    else:
        r = run(["busctl", "--user", "call", "org.freedesktop.systemd1",
                 "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                 "SetUnitProperties", "sba(sv)", p.slice, "true",
                 str(len(props) // 3), *props], **kw)
        if r.returncode != 0:
            raise RuntimeError(f"cannot re-limit {p.slice}: {_last_line(r.stderr)}")
    got = read_limits(p.slice, run)
    want = {"memory.max": str(lim.memory_max), "memory.swap.max": "0",
            "pids.max": str(lim.tasks_max),
            "cpu.max": f"{_usec_per_sec(lim.cpu_quota_pct) // 10} 100000"}
    bad = {k: (got.get(k), v) for k, v in want.items() if got.get(k) != v}
    if bad:
        raise RuntimeError(f"{p.slice} limits not applied: {bad}")
    return got


_CAGE_SLICE = re.compile(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_[0-9a-f]{8}\.slice")


def is_cage_slice(sl: str, loop: Optional[str] = None) -> bool:
    """Only ``<prefix>_<loop>_<sha1[:8]>.slice`` names (with ``loop`` given: the
    hash must be THAT loop's) — ``app.slice`` & co. can never be stopped by
    a teardown fed from run.json (which caged agents can write)."""
    if not isinstance(sl, str) or not _CAGE_SLICE.fullmatch(sl) or "_" not in sl:
        return False
    if loop is not None:
        stem = sl[:-len(".slice")]
        h8 = hashlib.sha1(loop.encode()).hexdigest()[:8]
        return stem.endswith(f"_{_safe(loop)}_{h8}")
    return True


def cgroup_dir(unit: str, run: Runner = subprocess.run) -> str:
    r = run(["systemctl", "--user", "show", "-p", "ControlGroup", "--value", unit],
            capture_output=True, text=True, timeout=10)
    cg = (r.stdout or "").strip()
    return f"/sys/fs/cgroup{cg}" if cg else ""


def read_limits(unit: str, run: Runner = subprocess.run) -> dict:
    d = cgroup_dir(unit, run)
    out = {}
    for f in ("memory.max", "memory.swap.max", "pids.max", "cpu.max"):
        try:
            with open(os.path.join(d, f)) as fh:
                out[f] = fh.read().strip()
        except OSError:
            out[f] = None
    return out


def teardown(p_or_slice, run: Runner = subprocess.run) -> dict:
    """Step 5 barrier: stop the loop slice — systemd SIGTERMs then SIGKILLs every
    process in it (double-forked / setsid escapees included). Call AFTER the
    runtime has collected the branch. Returns ``{slice, stopped, remaining}``."""
    sl = p_or_slice.slice if isinstance(p_or_slice, Policy) else str(p_or_slice)
    if not sl:
        return {"slice": "", "stopped": False, "remaining": 0}
    if not is_cage_slice(sl):
        raise ValueError(f"refusing to stop {sl!r}: not a loop cage slice")
    d = cgroup_dir(sl, run)
    r = run(["systemctl", "--user", "stop", sl], capture_output=True, text=True, timeout=120)
    remaining = 0
    if d and os.path.isdir(d):
        try:
            with open(os.path.join(d, "cgroup.procs")) as fh:
                remaining = len(fh.read().split())
        except OSError:
            pass
    return {"slice": sl, "stopped": r.returncode == 0, "remaining": remaining}


# ── exec stage (runs INSIDE the scope, just before the agent) ───────────────
PR_SET_NO_NEW_PRIVS, PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 38, 22, 2
_RET_KILL_PROCESS, _RET_ALLOW, _RET_ERRNO = 0x80000000, 0x7FFF0000, 0x00050000
_LD_W_ABS, _JEQ_K, _JGE_K, _RET_K = 0x20, 0x15, 0x35, 0x06
AF_UNIX = 1
LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET, LANDLOCK_SCOPE_SIGNAL = 1, 2
# landlock_ruleset_attr.handled_access_fs bits (write side only: reads and
# execute stay unrestricted; this is a write-confinement layer).
_FS_WRITE_FILE, _FS_TRUNCATE = 1 << 1, 1 << 14
_FS_DIR_WRITE = (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7) | (1 << 8) | (1 << 9) \
    | (1 << 10) | (1 << 11) | (1 << 12)          # REMOVE_*/MAKE_*
_FS_REFER = 1 << 13
_NET_CONNECT_TCP = 1 << 1
_RULE_PATH_BENEATH, _RULE_NET_PORT = 1, 2
# Claude Code state a caged agent may write under ~/.claude: ONLY its own
# per-cwd project dir (transcripts, and that cwd's project memory) + the
# append-only prompt history. Everything else there is read or EXECUTED by
# UNcaged sessions — settings*.json (hooks), shell-snapshots/*.sh (sourced before
# every Bash call), session-env/ (sourced env), other projects' memory/ (loaded
# into context), skills/plugins/agents/commands — so writing it is an escape.
CLAUDE_STATE_FILES = ("history.jsonl",)
# The only files a caged agent may write in its loop's status dir (report.py).
AGENT_STATUS_FILES = ("status.jsonl", "thoughtlog.jsonl")


def claude_project_dir(cwd: str, home: Optional[str] = None) -> str:
    """``~/.claude/projects/<cwd with every non-alnum → '-'>`` (claude's rule)."""
    home = home or os.path.expanduser("~")
    return os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))


def cage_cache_dir(uid: Optional[int] = None) -> str:
    """Where caged tools cache (XDG/npm/uv/pip): NOT ~/.cache or ~/.npm, whose
    contents uncaged tools execute."""
    return f"/tmp/lyloop-cache-{os.getuid() if uid is None else uid}"


def base_write_paths(home: Optional[str] = None, cwd: Optional[str] = None) -> list:
    """Always-writable roots for every caged agent. ``~/.claude.json`` is
    deliberately NOT here: it carries user-scope ``mcpServers`` commands and
    per-project settings that uncaged sessions execute (claude runs fine with
    it read-only — verified headless + interactive on 2.1.283)."""
    home = home or os.path.expanduser("~")
    cwd = cwd or os.getcwd()
    return (["/tmp", "/var/tmp", "/dev", claude_project_dir(cwd, home)]
            + [os.path.join(home, ".claude", f) for f in CLAUDE_STATE_FILES])


def _fs_masks(abi: int) -> tuple[int, int, int]:
    """→ (handled, dir_allowed, file_allowed) for this Landlock ABI."""
    file_w = _FS_WRITE_FILE | (_FS_TRUNCATE if abi >= 3 else 0)
    dir_w = file_w | _FS_DIR_WRITE | (_FS_REFER if abi >= 2 else 0)
    return dir_w, dir_w, file_w


def _landlock_rules(libc, fd: int, abi: int, rw: list, net_deny: list) -> list:
    """Add PATH_BENEATH write rules + NET_PORT connect rules; returns skipped paths."""
    _, dir_w, file_w = _fs_masks(abi)
    skipped = []
    for pth in rw:
        try:
            pfd = os.open(pth, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            skipped.append(pth)
            continue
        try:
            acc = dir_w if os.path.isdir(pth) else file_w
            attr = ctypes.create_string_buffer(struct.pack("=Qi", acc, pfd))
            if libc.syscall(445, ctypes.c_int(fd), ctypes.c_int(_RULE_PATH_BENEATH),
                            attr, ctypes.c_uint32(0)) != 0:
                raise OSError(ctypes.get_errno(), f"landlock_add_rule({pth})")
        finally:
            os.close(pfd)
    if net_deny:
        deny = set(net_deny)
        buf = ctypes.create_string_buffer(16)
        for port in range(0, 65536):
            if port in deny:
                continue
            struct.pack_into("=QQ", buf, 0, _NET_CONNECT_TCP, port)
            if libc.syscall(445, ctypes.c_int(fd), ctypes.c_int(_RULE_NET_PORT),
                            buf, ctypes.c_uint32(0)) != 0:
                raise OSError(ctypes.get_errno(), f"landlock_add_rule(port {port})")
    return skipped


def _ins(code: int, k: int, jt: int = 0, jf: int = 0) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def seccomp_program(deny_unix: bool, machine: Optional[str] = None) -> bytes:
    """Classic-BPF seccomp filter (see module doc). Pure → golden-testable."""
    arch, nr_socket, nr_uring, x32 = _ARCH[machine or platform.machine()]
    p = [_ins(_LD_W_ABS, 4), _ins(_JEQ_K, arch, 1, 0), _ins(_RET_K, _RET_KILL_PROCESS),
         _ins(_LD_W_ABS, 0)]
    if x32:
        p += [_ins(_JGE_K, 0x40000000, 0, 1), _ins(_RET_K, _RET_ERRNO | errno.ENOSYS)]
    p += [_ins(_JEQ_K, nr_uring, 0, 1), _ins(_RET_K, _RET_ERRNO | errno.ENOSYS)]
    if deny_unix:
        p += [_ins(_JEQ_K, nr_socket, 0, 3), _ins(_LD_W_ABS, 16),
              _ins(_JEQ_K, AF_UNIX, 0, 1), _ins(_RET_K, _RET_ERRNO | errno.EACCES)]
    p += [_ins(_RET_K, _RET_ALLOW)]
    return b"".join(p)


def _apply(deny_unix: bool, landlock: bool, rw: Optional[list] = None,
           net_deny: Optional[list] = None) -> list:
    """NNP → one Landlock domain (signal/abstract-unix scope, FS write
    allowlist when ``rw`` is not None, TCP connect denylist) → seccomp.
    Returns the rw paths skipped because they don't exist."""
    libc = _libc()
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS)")
    skipped: list = []
    if landlock or rw is not None or net_deny:
        abi = landlock_abi()
        if abi < 1:
            raise OSError(errno.ENOSYS, "landlock unavailable")
        if net_deny and abi < 4:
            raise OSError(errno.ENOSYS, f"landlock net needs ABI 4 (have {abi})")
        if landlock and abi < 6:
            raise OSError(errno.ENOSYS, f"landlock scope needs ABI 6 (have {abi})")
        handled_fs = _fs_masks(abi)[0] if rw is not None else 0
        handled_net = _NET_CONNECT_TCP if net_deny else 0
        scoped = (LANDLOCK_SCOPE_SIGNAL | LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET) if landlock else 0
        size = 24 if scoped else (16 if handled_net else 8)
        attr = ctypes.create_string_buffer(struct.pack("QQQ", handled_fs, handled_net,
                                                       scoped)[:size], size)
        fd = libc.syscall(444, attr, ctypes.c_size_t(size), ctypes.c_uint32(0))
        if fd < 0:
            raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
        try:
            skipped = _landlock_rules(libc, fd, abi, list(rw or ()), list(net_deny or ()))
            if libc.syscall(446, ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
                raise OSError(ctypes.get_errno(), "landlock_restrict_self")
        finally:
            os.close(fd)
    prog = seccomp_program(deny_unix)
    buf = ctypes.create_string_buffer(prog, len(prog))

    class _Fprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    fprog = _Fprog(len(prog) // 8, ctypes.addressof(buf))
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(SECCOMP)")
    return skipped


_CACHE_ENV = {"XDG_CACHE_HOME": "", "npm_config_cache": "npm", "UV_CACHE_DIR": "uv",
              "PIP_CACHE_DIR": "pip"}


def _prepare_fs_cage() -> None:
    """Before Landlock: create this cwd's claude project dir (mkdir under
    ~/.claude/projects is denied afterwards) and point tool caches at
    :func:`cage_cache_dir`. Best-effort."""
    try:
        os.makedirs(claude_project_dir(os.getcwd()), exist_ok=True)
    except OSError:
        pass
    root = cage_cache_dir()
    for var, sub in _CACHE_ENV.items():
        os.environ[var] = os.path.join(root, sub) if sub else root
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
    except OSError:
        pass


# Step-3 mount masks: paths hidden inside the namespaced box.
def mask_paths(uid: Optional[int] = None, home: Optional[str] = None) -> list:
    uid = os.getuid() if uid is None else uid
    home = home or os.path.expanduser("~")
    return ["/run/docker.sock", "/var/run/docker.sock", f"/run/user/{uid}",
            f"/tmp/tmux-{uid}", os.path.join(home, ".ssh")]


def _boxinit(rest: list) -> int:
    """Root-in-userns step of the Step-3 box: mask sensitive paths, then exec."""
    libc = _libc()
    MS_BIND, MS_RDONLY = 4096, 1
    seen = set()
    for pth in mask_paths(uid=int(os.environ.get("LOOPS_BOX_UID", os.getuid())),
                          home=os.environ.get("HOME")):
        real = os.path.realpath(pth)
        if real in seen or not os.path.lexists(real):
            continue
        seen.add(real)
        if os.path.isdir(real):
            rc = libc.mount(b"tmpfs", real.encode(), b"tmpfs", 0, b"mode=0700,size=1m")
        else:
            rc = libc.mount(b"/dev/null", real.encode(), None, MS_BIND | MS_RDONLY, None)
        if rc != 0:
            print(f"loop-sandbox: mask {real} failed errno={ctypes.get_errno()}",
                  file=sys.stderr)
            return 126
    os.execvp(rest[0], rest)
    return 127


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("exec", "boxinit", "probe"):
        print("usage: sandbox.py exec [--unix=deny|allow] [--landlock=on|off] "
              "[--fs=on --rw=PATH…] [--net-deny=P,…] -- CMD… | "
              "boxinit -- CMD… | probe", file=sys.stderr)
        return 2
    verb, argv = argv[0], argv[1:]
    if verb == "probe":
        print(json.dumps(asdict(probe_capabilities()), indent=2))
        return 0
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        print(f"loop-sandbox: {verb}: missing '-- CMD'", file=sys.stderr)
        return 2
    i = argv.index("--")
    opts, rest = argv[:i], argv[i + 1:]
    if verb == "boxinit":
        return _boxinit(rest)
    unix, landlock, fs, rw, net_deny = "deny", True, False, [], []
    for o in opts:
        if o in ("--unix=deny", "--unix=allow"):
            unix = o.split("=", 1)[1]
        elif o in ("--landlock=on", "--landlock=off"):
            landlock = o.endswith("on")
        elif o == "--fs=on":
            fs = True
        elif o.startswith("--rw=") and os.path.isabs(o[5:]):
            rw.append(o[5:])
        elif o.startswith("--net-deny="):
            try:
                net_deny = [int(x) for x in o.split("=", 1)[1].split(",") if x]
            except ValueError:
                print(f"loop-sandbox: bad {o!r}", file=sys.stderr)
                return 2
        else:
            print(f"loop-sandbox: unknown option {o!r}", file=sys.stderr)
            return 2
    try:
        # No inherited descriptor may smuggle a pre-connected unix socket in.
        os.closerange(3, os.sysconf("SC_OPEN_MAX") if hasattr(os, "sysconf") else 4096)
        if fs:
            _prepare_fs_cage()
        _apply(deny_unix=(unix == "deny"), landlock=landlock,
               rw=(base_write_paths() + rw) if fs else None, net_deny=net_deny)
    except Exception as e:  # noqa: BLE001 — fail CLOSED: never run the agent uncaged
        print(f"loop-sandbox: cannot apply cage ({e}); refusing to start {rest[0]!r}",
              file=sys.stderr)
        return 126
    try:
        os.execvp(rest[0], rest)
    except OSError as e:
        print(f"loop-sandbox: exec {rest[0]!r} failed: {e}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
