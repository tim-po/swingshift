"""Read-only surface over swarm state on disk — initiatives, tasks,
experts. Used by the API/UI to surface what's happening in a project
without needing tmux or running panes.

Everything here is a pure-function read of the filesystem. No side
effects, no mutations.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML-ish frontmatter (the simplified shape bot-squad uses)
    plus the body. Returns ({}, full_text) if no frontmatter.

    Handles list values like ``related_tasks: [T-0042, T-0043]`` and
    null-as-tilde (``~``). Same dialect as ``_read_session_metadata``
    in sessions.py — keep them in sync if the dialect grows.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm_block, body = m.group(1), m.group(2)
    meta: dict = {}
    for line in fm_block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[k] = [x.strip() for x in inner.split(",")] if inner else []
        elif v in ("~", "null", ""):
            meta[k] = None
        else:
            meta[k] = v
    return meta, body


def _initiatives_dir(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / slug / "initiatives"


def _backlog_dir(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / slug / "backlog"


def _experts_dir(cfg: Any, slug: str) -> Path:
    return Path(cfg.data_dir) / slug / "experts"


def list_initiatives(cfg: Any, slug: str) -> list[dict]:
    """List all initiative files under ``data/<slug>/initiatives/``.

    Each entry: ``{id, title, status, related_tasks, created_at, owner,
    path}``. Missing fields are returned as None. Extra frontmatter
    fields are passed through under ``extra``.
    """
    if slug not in cfg.projects:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"list_initiatives: unknown slug {slug!r}")
    d = _initiatives_dir(cfg, slug)
    if not d.is_dir():
        return []
    out: list[dict] = []
    known = {"id", "title", "name", "description", "status",
             "related_tasks", "created_at", "created", "owner"}
    for path in sorted(d.glob("I-*.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        meta, _ = _parse_frontmatter(text)
        extra = {k: v for k, v in meta.items() if k not in known}
        out.append({
            "id": meta.get("id") or path.stem.split("-")[0],
            "title": meta.get("title") or meta.get("name"),
            "description": meta.get("description"),
            "status": meta.get("status"),
            "related_tasks": meta.get("related_tasks") or [],
            "created_at": meta.get("created_at") or meta.get("created"),
            "owner": meta.get("owner"),
            "path": str(path),
            "extra": extra,
        })
    return out


def read_initiative(cfg: Any, slug: str, id: str) -> dict:
    """Read one initiative by ID. Returns the full body alongside parsed
    frontmatter. Raises ActionError if not found.
    """
    from bot_squad_worker.actions import ActionError
    if slug not in cfg.projects:
        raise ActionError(f"read_initiative: unknown slug {slug!r}")
    d = _initiatives_dir(cfg, slug)
    matches = sorted(d.glob(f"{id}-*.md")) if d.is_dir() else []
    if not matches:
        # Fall back: maybe the id is the full stem.
        exact = d / f"{id}.md" if d.is_dir() else None
        if exact and exact.is_file():
            matches = [exact]
    if not matches:
        raise ActionError(f"read_initiative: not found: {id}")
    path = matches[0]
    text = path.read_text()
    meta, body = _parse_frontmatter(text)
    return {
        "id": meta.get("id") or path.stem.split("-")[0],
        "frontmatter": meta,
        "body": body,
        "path": str(path),
    }


def list_tasks(cfg: Any, slug: str) -> list[dict]:
    """List all task files under ``data/<slug>/backlog/``.

    Each entry: ``{id, title, initiative_id, assigned_role, status,
    depends_on, created_at, path}``. Extra frontmatter passes through
    under ``extra``.
    """
    if slug not in cfg.projects:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"list_tasks: unknown slug {slug!r}")
    d = _backlog_dir(cfg, slug)
    if not d.is_dir():
        return []
    out: list[dict] = []
    known = {"id", "title", "initiative_id", "assigned_role", "status",
             "depends_on", "created_at"}
    for path in sorted(d.glob("T-*.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        meta, _ = _parse_frontmatter(text)
        extra = {k: v for k, v in meta.items() if k not in known}
        out.append({
            "id": meta.get("id") or path.stem.split("-")[0],
            "title": meta.get("title"),
            "initiative_id": meta.get("initiative_id"),
            "assigned_role": meta.get("assigned_role"),
            "status": meta.get("status"),
            "depends_on": meta.get("depends_on") or [],
            "created_at": meta.get("created_at"),
            "path": str(path),
            "extra": extra,
        })
    return out


def read_task(cfg: Any, slug: str, id: str) -> dict:
    """Read one task by ID. Returns full body + parsed frontmatter."""
    from bot_squad_worker.actions import ActionError
    if slug not in cfg.projects:
        raise ActionError(f"read_task: unknown slug {slug!r}")
    d = _backlog_dir(cfg, slug)
    matches = sorted(d.glob(f"{id}-*.md")) if d.is_dir() else []
    if not matches:
        exact = d / f"{id}.md" if d.is_dir() else None
        if exact and exact.is_file():
            matches = [exact]
    if not matches:
        raise ActionError(f"read_task: not found: {id}")
    path = matches[0]
    text = path.read_text()
    meta, body = _parse_frontmatter(text)
    return {
        "id": meta.get("id") or path.stem.split("-")[0],
        "frontmatter": meta,
        "body": body,
        "path": str(path),
    }


def list_experts(cfg: Any, slug: str) -> list[dict]:
    """List swarm expert directories under ``data/<slug>/experts/``.

    Each entry: ``{role, has_identity, memory_files, sleep_mark}``.
    Useful for the dashboard's expert roster view. Includes archived
    experts (under ``_archive/``) with ``archived: true``.
    """
    if slug not in cfg.projects:
        from bot_squad_worker.actions import ActionError
        raise ActionError(f"list_experts: unknown slug {slug!r}")
    d = _experts_dir(cfg, slug)
    if not d.is_dir():
        return []
    out: list[dict] = []
    for sub in sorted(d.iterdir()):
        if not sub.is_dir():
            continue
        if sub.name == "_archive":
            for arch in sorted(sub.iterdir()):
                if arch.is_dir():
                    out.append(_expert_record(arch, archived=True))
            continue
        out.append(_expert_record(sub, archived=False))
    return out


def read_expert_memory(cfg: Any, slug: str, role: str, filename: str) -> dict:
    """Read one memory file under
    ``data/<slug>/experts/<role>/memory/<filename>``.

    Filename must be a single segment (no slashes, no ``..``) ending in
    ``.md``. The resolved path is checked to stay inside the memory
    directory — defence in depth against path traversal even though the
    explicit segment check already rules it out.
    """
    from bot_squad_worker.actions import ActionError
    if slug not in cfg.projects:
        raise ActionError(f"read_expert_memory: unknown slug {slug!r}")
    if "/" in filename or ".." in filename or not filename.endswith(".md"):
        raise ActionError(f"read_expert_memory: invalid filename {filename!r}")
    memory_dir = _experts_dir(cfg, slug) / role / "memory"
    target = memory_dir / filename
    try:
        resolved = target.resolve()
        memory_resolved = memory_dir.resolve()
    except FileNotFoundError:
        raise ActionError(f"read_expert_memory: not found")
    if memory_resolved not in resolved.parents and resolved != memory_resolved:
        raise ActionError(f"read_expert_memory: invalid path")
    if not target.is_file():
        raise ActionError(f"read_expert_memory: not found")
    try:
        content = target.read_text()
    except OSError as e:
        raise ActionError(f"read_expert_memory: {e}")
    return {
        "role": role,
        "filename": filename,
        "content": content,
        "path": str(target),
    }


def _expert_record(expert_dir: Path, archived: bool) -> dict:
    identity = expert_dir / "identity.md"
    memory_dir = expert_dir / "memory"
    memory_files: list[str] = []
    if memory_dir.is_dir():
        memory_files = sorted(p.name for p in memory_dir.glob("*.md"))
    sleep_mark = expert_dir / "sleep_mark"
    mark_val: int | None = None
    if sleep_mark.is_file():
        try:
            mark_val = int(sleep_mark.read_text().strip())
        except ValueError:
            mark_val = None
    return {
        "role": expert_dir.name,
        "has_identity": identity.is_file(),
        "memory_files": memory_files,
        "sleep_mark": mark_val,
        "archived": archived,
    }
