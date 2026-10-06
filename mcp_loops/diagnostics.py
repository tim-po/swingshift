"""``yard diagnostics`` — one tarball a user can attach to a bug report
(LAUNCH-CHECKLIST 6.2): log tails, versions, service state and the config
files, with every secret redacted.

Two layers keep secrets out:

  1. **Allowlist, not a sweep.** Only named files go in: the service logs
     under ``<data>/_logs`` + ``<data>/_run`` + the origin log, the
     ``services.json`` / runner registry, and ``*.toml`` / ``*.json`` from the
     config dir. Key material (``*.pem``, the origin keypair, ``secret.txt``
     cookie secrets, claude credentials) is never read at all.
  2. **Every line is redacted** before it is written: the value of any
     ``key = value`` / ``"key": value`` / ``KEY=value`` whose key names a
     secret, ``Authorization``/``Cookie`` headers, URL userinfo passwords, and
     token-shaped strings (``sk-…``, ``ghp_…``, JWTs, long mixed base64/hex
     runs; a 40-char git sha is kept).
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Callable, Optional

from mcp_loops import paths, runner_registry

REDACTED = "<redacted>"
LOG_TAIL_BYTES = 512 * 1024

_SECRET_KEY = r"[\w.-]*(?:secret|token|passw(?:or)?d|passwd|api[_-]?key|private[_-]?key|credential|cookie|session[_-]?key|auth)[\w.-]*"
_RULES: list[tuple[re.Pattern, str]] = [
    # "key": "value"  (JSON)
    (re.compile(rf'("{_SECRET_KEY}"\s*:\s*)("(?:[^"\\]|\\.)*"|[^,}}\s]+)', re.I),
     rf'\1"{REDACTED}"'),
    # key = value / KEY=value / key: value  (TOML, env, logs)
    (re.compile(rf"(\b{_SECRET_KEY}\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)", re.I),
     rf"\1{REDACTED}"),
    (re.compile(r"(\b(?:authorization|proxy-authorization)\s*:\s*)(.+)", re.I),
     rf"\1{REDACTED}"),
    (re.compile(r"(\b(?:set-)?cookie\s*:\s*)(.+)", re.I), rf"\1{REDACTED}"),
    (re.compile(r"(\bbearer\s+)[\w.~+/=-]+", re.I), rf"\1{REDACTED}"),
    (re.compile(r"(://[^\s:/@]+:)[^\s/@]+@"), rf"\1{REDACTED}@"),
    (re.compile(r"\b(?:sk|pk|rk)-[\w-]{16,}"), REDACTED),
    (re.compile(r"\b(?:gh[pousr]|github_pat)_\w{20,}"), REDACTED),
    (re.compile(r"\bxox[abpr]-[\w-]{10,}"), REDACTED),
    (re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), REDACTED),   # JWT
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
                re.S), REDACTED),
]
_LONG_RUN = re.compile(r"(?<![\w+-])[\w+-]{32,}={0,2}(?![\w+=-])")


def _long_run(m: re.Match) -> str:
    """High-entropy runs only: mixed-case+digit (base64) or bare hex. Loop
    names / paths (lowercase + digits + '-') and a 40-char git sha survive."""
    s = m.group(0).rstrip("=")
    mixed = (re.search(r"[a-z]", s) and re.search(r"[A-Z]", s)
             and re.search(r"\d", s))
    hexy = re.fullmatch(r"[0-9a-fA-F]+", s) and len(s) != 40
    return REDACTED if mixed or hexy else s


def redact(text: str) -> str:
    """Scrub secrets from ``text`` (config or log content)."""
    for pat, repl in _RULES:
        text = pat.sub(repl, text)
    return _LONG_RUN.sub(_long_run, text)


def _tail(path: str, limit: int = LOG_TAIL_BYTES) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - limit))
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    if size > limit:
        text = f"[… first {size - limit} bytes omitted …]\n" + text.split("\n", 1)[-1]
    return text


def _cmd_version(argv: list[str], run: Callable) -> str:
    try:
        out = run(argv, capture_output=True, text=True, timeout=15,
                  stdin=subprocess.DEVNULL)
        return ((out.stdout or "") + (out.stderr or "")).strip().splitlines()[0] \
            if (out.stdout or out.stderr) else f"rc={out.returncode}"
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        return f"unavailable ({type(exc).__name__})"


_SKIP_CONFIG = re.compile(r"(\.pem|\.key|secret\.txt|credentials?\.json)$", re.I)


def collect(*, data_dir: Optional[str] = None, out_dir: Optional[str] = None,
            now: Optional[float] = None, run: Callable = subprocess.run,
            which: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """Write the tarball and return its path."""
    import shutil
    which = which or shutil.which
    now = time.time() if now is None else now
    droot = paths.resolve_data_dir(data_dir)
    data_parent = os.path.dirname(droot)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    out_dir = os.path.abspath(out_dir or os.path.join(data_parent, "_diag"))
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"loopyard-diagnostics-{stamp}.tar.gz")
    prefix = f"loopyard-diagnostics-{stamp}"
    manifest: dict = {"created": now, "files": [], "skipped": []}

    def add(name: str, text: str) -> None:
        blob = redact(text).encode("utf-8")
        info = tarfile.TarInfo(f"{prefix}/{name}")
        info.size, info.mtime, info.mode = len(blob), int(now), 0o600
        tar.addfile(info, io.BytesIO(blob))
        manifest["files"].append(name)

    # D9 order, without importing the engine: env > PATH > ~/.local/bin
    claude = (os.environ.get("LOOPS_CLAUDE_BIN") or which("claude")
              or os.path.expanduser("~/.local/bin/claude"))
    bundle = paths.install_root() / "BUNDLE.json"
    versions = {
        "loopyard": getattr(__import__("mcp_loops"), "__version__", None) or "dev",
        "bundle": _read_json_file(str(bundle)),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "claude": _cmd_version([claude, "--version"], run),
        "tmux": _cmd_version(["tmux", "-V"], run) if which("tmux") else "not found",
        "git": _cmd_version(["git", "--version"], run) if which("git") else "not found",
        "installRoot": str(paths.install_root()),
        "stateRoot": str(paths.state_root()),
        "dataDir": droot,
        "configDir": str(paths.config_dir()),
    }
    old_umask = os.umask(0o077)
    try:
        with tarfile.open(out, "w:gz") as tar:
            add("versions.json", json.dumps(versions, indent=2, default=str))
            for sub in ("_logs", "_run"):
                d = os.path.join(data_parent, sub)
                for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                    p = os.path.join(d, name)
                    if not os.path.isfile(p):
                        continue
                    if name.endswith(".log") or name in ("services.json",
                                                         "start.json"):
                        add(f"{sub.strip('_')}/{name}", _tail(p))
            origin_log = os.path.join(droot, "_origin", "origin.log")
            if os.path.isfile(origin_log):
                add("logs/origin.log", _tail(origin_log))
            runner = runner_registry.marker_path(data_dir)
            if os.path.isfile(runner):
                add("run/runner.json", _tail(runner))
            cfg = str(paths.config_dir())
            for name in sorted(os.listdir(cfg)) if os.path.isdir(cfg) else []:
                p = os.path.join(cfg, name)
                if not os.path.isfile(p):
                    continue
                if _SKIP_CONFIG.search(name) or not name.endswith((".toml", ".json")):
                    manifest["skipped"].append(f"config/{name}")
                    continue
                add(f"config/{name}", _tail(p))
            add("MANIFEST.json", json.dumps(manifest, indent=2))
    finally:
        os.umask(old_umask)
    os.chmod(out, 0o600)
    return out


def _read_json_file(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
