"""CLI runtime registry and host routing policy. No credentials are managed here."""
from __future__ import annotations
import os
import json
import shutil
from dataclasses import dataclass

@dataclass(frozen=True)
class Runtime:
    binary: str
    interactive: tuple[str, ...]
    headless: tuple[str, ...]
    stdin_suffix: tuple[str, ...] = ()

RUNTIMES = {
    "claude": Runtime("claude", ("--dangerously-skip-permissions",),
                      ("-p", "--output-format", "text", "--dangerously-skip-permissions")),
    "codex": Runtime("codex", ("--dangerously-bypass-approvals-and-sandbox", "-c", "check_for_update_on_startup=false"),
                     ("exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"), ("-",)),
    "cursor": Runtime("cursor-agent", ("--force", "--trust", "--approve-mcps"),
                      ("--print", "--output-format", "text", "--force", "--trust", "--approve-mcps")),
}
SUPPORTED_RUNTIMES = tuple(RUNTIMES)

def normalize(runtime=None):
    value = str(runtime or "claude").strip().lower() or "claude"
    if value not in RUNTIMES:
        raise ValueError(f"unsupported runtime {runtime!r} — expected one of {list(RUNTIMES)}")
    return value

def selection(runtime=None, model=None):
    """Override is explicit, never an automatic replay after a failed turn.

    Switching providers drops the old provider's model; an optional
    LOOPS_<RUNTIME>_MODEL selects the replacement provider's model.
    """
    from mcp_loops import runtime_policy
    env = {**runtime_policy.environment(), **os.environ}
    requested = normalize(runtime)
    override = env.get("LOOPS_RUNTIME_OVERRIDE", "").strip()
    configured = normalize(runtime or env.get("LOOPS_DEFAULT_RUNTIME", "claude"))
    try:
        remap = json.loads(env.get("LOOPS_RUNTIME_REMAP", "{}"))
    except ValueError as exc:
        raise ValueError("LOOPS_RUNTIME_REMAP must be a JSON object") from exc
    if not isinstance(remap, dict):
        raise ValueError("LOOPS_RUNTIME_REMAP must be a JSON object")
    for source, target in remap.items():
        normalize(source)
        if not isinstance(target, str) or not target.strip():
            raise ValueError("LOOPS_RUNTIME_REMAP targets must name a runtime")
        normalize(target)
    effective = normalize(override or remap.get(configured, configured))
    if effective != requested:
        model = None
    model = model or env.get(f"LOOPS_{effective.upper()}_MODEL") or None
    return effective, model

def binary(runtime):
    rt = normalize(runtime)
    name = RUNTIMES[rt].binary
    return os.environ.get(f"LOOPS_{rt.upper()}_BIN") or shutil.which(name) or name

def argv(runtime, model=None, *, headless=False, executable=None):
    rt = normalize(runtime)
    spec = RUNTIMES[rt]
    cmd = [executable or binary(rt), *(spec.headless if headless else spec.interactive)]
    if model and str(model).strip():
        cmd += ["--model", str(model).strip()]
    if headless:
        cmd += spec.stdin_suffix
    return cmd
