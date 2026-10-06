"""Install-local provider preferences. Stores no credentials or executable paths."""
import json
import os
from pathlib import Path
import shlex
import tempfile
import subprocess

from mcp_loops import paths


def policy_path():
    return paths.config_dir() / "runtime.json"


def load():
    try:
        policy = json.loads(policy_path().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {policy_path()}: {exc}") from exc
    if (not isinstance(policy, dict) or policy.get("version") != 1
            or policy.get("runtime") not in ("claude", "codex", "cursor")
            or policy.get("permissions") not in ("role-profiles", "trusted-workspace")):
        raise ValueError(f"Invalid provider configuration in {policy_path()}")
    if policy["runtime"] != "claude" and policy["permissions"] != "trusted-workspace":
        raise ValueError("Codex/Cursor require explicit trusted-workspace mode in this beta")
    return policy


def save(runtime, trusted=False):
    if runtime not in ("claude", "codex", "cursor"):
        raise ValueError("Choose claude, codex, or cursor")
    if runtime != "claude" and not trusted:
        raise ValueError("Codex/Cursor role restrictions are not enforced in this beta. "
                         "For your own trusted repository, use --trusted-workspace explicitly.")
    policy = {"version": 1, "runtime": runtime,
              "permissions": "trusted-workspace" if trusted else "role-profiles"}
    path = policy_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".runtime-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(policy, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return policy


def environment():
    policy = load()
    if not policy:
        return {}
    rt = policy["runtime"]
    env = {"LOOPS_DEFAULT_RUNTIME": rt,
            "LOOPS_RUNTIME_REMAP": json.dumps({"claude": rt} if rt != "claude" else {}),
            "LOOPS_ROLE_CAPS": "0" if policy["permissions"] == "trusted-workspace" else "1"}
    if policy["permissions"] == "trusted-workspace":
        # The Linux cage currently assumes Claude state and denies AF_UNIX.
        # Codex needs both writable provider state and its local app-server socket.
        # Opt out only under the operator's explicit trusted-workspace choice.
        env["LOOPS_ISOLATION"] = "off"
    return env


def cursor_adapter(python, binary):
    """Use the install interpreter (bundles do not need system Python)."""
    path = paths.config_dir() / "provider-bin" / "cursor-refresh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nexec {shlex.quote(str(python))} -I -m "
                    'mcp_loops.cursor_auth "$@"\n')
    path.chmod(0o700)
    return {"LOOPS_CURSOR_BIN": str(path), "LOOPS_CURSOR_REAL_BIN": str(binary)}


def spawn_environment(python, which, policy_env=None):
    """The same provider environment for engine, worker and browser terminals."""
    policy_env = environment() if policy_env is None else policy_env
    result = {k: os.environ.get(k, v) for k, v in policy_env.items()}
    if policy_env:
        cursor = which("cursor-agent")
        if cursor and not os.environ.get("LOOPS_CURSOR_BIN"):
            result.update(cursor_adapter(python, cursor))
    return result


def check_login(runtime, binary, run=subprocess.run):
    args = [binary, *({"claude": ["auth", "status"], "codex": ["login", "status"],
                      "cursor": ["status"]}[runtime])]
    try:
        result = run(args, stdin=subprocess.DEVNULL, capture_output=True,
                     text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{runtime} login check could not complete; run the CLI login and retry") from exc
    output = ((result.stdout or "") + (result.stderr or "")).lower()
    if result.returncode or any(s in output for s in ("not logged in", "not authenticated", '"loggedin": false')):
        command = {"claude": "claude auth login", "codex": "codex login",
                   "cursor": "cursor-agent login"}[runtime]
        raise ValueError(f"{runtime} is not logged in; run `{command}`, then `yard start` again")
