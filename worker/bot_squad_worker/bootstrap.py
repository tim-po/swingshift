"""Project repo bootstrap — drop a swarm-aware ``.claude/settings.json``
into a project's repo_path so Claude Code fires this install's hooks
when a pane starts there.

Idempotent: if a settings.json already exists in the repo's .claude/
directory we leave it alone and report ``skipped=True`` — the operator
might have customised it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _install_root(cfg: Any) -> Path:
    # config_dir on the real Config; data_dir.parent on test stubs.
    # Either way the install root is one level above data/.
    return Path(cfg.data_dir).parent


def _settings_payload(install_root: Path) -> dict:
    """Build the .claude/settings.json content for a swarm project."""
    hooks = install_root / "scripts" / "hooks"
    return {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command",
                            "command": str(hooks / "session_start.sh")}]}
            ],
            "UserPromptSubmit": [
                {"hooks": [{"type": "command",
                            "command": str(hooks / "user_prompt_submit.sh")}]}
            ],
            "Stop": [
                {"hooks": [{"type": "command",
                            "command": str(hooks / "stop.sh")}]}
            ],
        },
        "permissions": {
            "allow": ["Read", "Glob", "Grep", "Edit", "Write", "Bash"],
        },
    }


def bootstrap_project_repo(cfg: Any, slug: str) -> dict:
    """Wire ``<repo>/.claude/settings.json`` so this install's hooks fire
    when Claude Code starts in the repo. Creates ``<repo>`` and
    ``<repo>/.claude`` if they don't yet exist.

    Always merges swarm hooks into the file. If the file is absent,
    writes a fresh one with hooks + permissions. If it already exists
    we preserve its other top-level keys (e.g. mcp servers, custom
    permissions) and only overwrite the swarm-managed entries:
      - hooks.SessionStart, hooks.UserPromptSubmit, hooks.Stop
      - permissions.allow (we add the entries we need; existing entries
        are kept)

    A pre-existing file is backed up to ``settings.json.pre-swarm.bak``
    once (no double-backup on re-runs).

    Returns ``{ok, path, repo_path, action}`` where action is one of
    ``written`` (fresh file), ``merged`` (existing keys preserved,
    swarm keys overwritten), or ``unchanged`` (file already had our
    hook paths).
    """
    from bot_squad_worker.actions import ActionError

    project = cfg.projects.get(slug)
    if project is None:
        raise ActionError(f"bootstrap_project_repo: unknown slug {slug!r}")

    repo_path = Path(project.repo_path)
    repo_path.mkdir(parents=True, exist_ok=True)

    claude_dir = repo_path / ".claude"
    claude_dir.mkdir(exist_ok=True)

    settings_path = claude_dir / "settings.json"
    desired = _settings_payload(_install_root(cfg))

    # Fresh write.
    if not settings_path.exists():
        settings_path.write_text(json.dumps(desired, indent=2) + "\n")
        return {
            "ok": True,
            "path": str(settings_path),
            "repo_path": str(repo_path),
            "action": "written",
        }

    # Merge: load existing JSON, overwrite swarm-managed keys.
    try:
        existing = json.loads(settings_path.read_text())
        if not isinstance(existing, dict):
            existing = {}
    except (json.JSONDecodeError, OSError):
        # Corrupt settings.json — back up and start clean. Better than
        # silently leaving the repo in a broken state.
        bak = claude_dir / "settings.json.pre-swarm.bak"
        if not bak.exists():
            settings_path.replace(bak)
        settings_path.write_text(json.dumps(desired, indent=2) + "\n")
        return {
            "ok": True,
            "path": str(settings_path),
            "repo_path": str(repo_path),
            "action": "written",
            "note": "previous settings.json was unparseable; backed up",
        }

    # Snapshot for "unchanged?" check.
    before = json.dumps(existing, sort_keys=True)

    # Stash a one-time backup of the pre-swarm file.
    bak = claude_dir / "settings.json.pre-swarm.bak"
    if not bak.exists():
        bak.write_text(json.dumps(existing, indent=2) + "\n")

    # Overwrite swarm-managed hook entries.
    hooks = existing.setdefault("hooks", {}) if isinstance(existing.get("hooks"), dict) else None
    if hooks is None:
        existing["hooks"] = {}
        hooks = existing["hooks"]
    for event, val in desired["hooks"].items():
        hooks[event] = val

    # Merge our required permission allows in, preserving any extras.
    perms = existing.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        existing["permissions"] = perms
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []
    for tool in desired["permissions"]["allow"]:
        if tool not in allow:
            allow.append(tool)
    perms["allow"] = allow

    after = json.dumps(existing, sort_keys=True)
    if before == after:
        return {
            "ok": True,
            "path": str(settings_path),
            "repo_path": str(repo_path),
            "action": "unchanged",
        }

    settings_path.write_text(json.dumps(existing, indent=2) + "\n")
    return {
        "ok": True,
        "path": str(settings_path),
        "repo_path": str(repo_path),
        "action": "merged",
        "backup": str(bak) if bak.exists() else None,
    }
