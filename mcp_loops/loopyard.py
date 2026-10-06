"""The ``/loopyard/`` project-state manager (Round B1, #5 — the core substrate).

A **Project** (a connected git repo, or a dir on an origin) may carry a
top-level ``/loopyard/`` directory *inside its checkout*, holding project-scoped
state: loop configs, references, and — the reason this module exists —
**capability DATA** (see :mod:`mcp_loops.capabilities`). Core ships a *simple*
manager: resolve a Project to its checkout, then list/read/write files under its
``loopyard/`` dir with safe path handling (no escaping the dir).

Layering (matches the house style — pure model + thin server wiring):

* This module is **pure filesystem** over an explicit *base* directory. It does
  NOT know how to resolve a Project → checkout (that lives in ``server.py``,
  which already owns ``_resolve_product_checkout``). Keeping it base-relative
  makes every operation unit-testable against a ``tmp_path`` with no registry,
  no git, and no server import.
* The server/dashboard layer resolves ``project_id → checkout`` and calls
  :func:`loopyard_dir` + the ``*_file`` / :func:`list_tree` helpers below.

Safety is the whole point: :func:`safe_join` refuses any relative path that
would resolve outside the loopyard dir — ``..`` traversal, an absolute path, or
a symlink that points out. A capability writing ``loopyard/<cap>/x.json`` must
round-trip; nothing may ever touch a byte outside ``loopyard/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# The single conventional dir name, in one place.
LOOPYARD_DIRNAME = "loopyard"


class LoopyardPathError(ValueError):
    """A requested relative path escapes (or cannot be contained in) the
    ``loopyard/`` dir. Raised instead of silently clamping so callers surface a
    clean 400 rather than reading/writing the wrong file."""


def loopyard_dir(checkout: str | os.PathLike[str], *, create: bool = False) -> Path:
    """The ``loopyard/`` directory *inside* a project's checkout.

    ``checkout`` is the resolved on-disk repo dir (from the server's project
    resolver). With ``create=True`` the dir is created (parents too) if absent —
    the spec says "create it if absent". Returns the :class:`Path` regardless.
    """
    base = Path(checkout).resolve() / LOOPYARD_DIRNAME
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return base


def safe_join(base: str | os.PathLike[str], relpath: str) -> Path:
    """Join ``relpath`` under ``base`` and guarantee the result stays inside it.

    Rejects absolute paths and any ``..`` traversal that would land outside
    ``base``. Uses real-path containment (``os.path.realpath``) so a symlink
    inside the tree that points out is caught too. ``relpath`` of ``""`` / ``.``
    resolves to ``base`` itself (used to list the root).
    """
    rel = (relpath or "").strip()
    # Normalise separators but forbid an absolute path outright — an absolute
    # relpath is never a request we should honour under a base dir.
    if rel.startswith("/") or rel.startswith("\\") or (os.path.isabs(rel)):
        raise LoopyardPathError(f"absolute path not allowed: {relpath!r}")

    base_real = Path(os.path.realpath(base))
    # Resolve the candidate against the base, then realpath it so any symlink
    # component is expanded before the containment check.
    candidate = Path(os.path.realpath(base_real / rel))

    if candidate != base_real and base_real not in candidate.parents:
        raise LoopyardPathError(f"path escapes loopyard dir: {relpath!r}")
    return candidate


def _entry(base: Path, p: Path) -> dict[str, Any]:
    """One tree-entry descriptor (name, relpath, type, size) for listings."""
    is_dir = p.is_dir()
    return {
        "name": p.name,
        "path": os.path.relpath(p, base) if p != base else "",
        "type": "dir" if is_dir else "file",
        "size": (p.stat().st_size if p.is_file() else None),
    }


def list_tree(checkout: str | os.PathLike[str], subpath: str = "") -> list[dict[str, Any]]:
    """List the immediate children of ``loopyard/<subpath>`` (one level).

    Returns ``[]`` when the loopyard dir (or the subpath) does not exist yet —
    an absent ``loopyard/`` is an empty project-state, not an error. Entries are
    sorted dirs-first then name, each a :func:`_entry` descriptor with a
    base-relative ``path`` suitable for a follow-up read.
    """
    base = loopyard_dir(checkout)
    if not base.exists():
        return []
    target = safe_join(base, subpath)
    if not target.exists() or not target.is_dir():
        return []
    entries = [_entry(base, c) for c in target.iterdir()]
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return entries


def read_file(checkout: str | os.PathLike[str], relpath: str) -> str:
    """Read a text file under ``loopyard/``. Raises :class:`FileNotFoundError`
    if absent, :class:`LoopyardPathError` on an escaping path."""
    base = loopyard_dir(checkout)
    target = safe_join(base, relpath)
    if target == base:
        raise LoopyardPathError("cannot read the loopyard dir itself as a file")
    return target.read_text(encoding="utf-8")


def write_file(
    checkout: str | os.PathLike[str],
    relpath: str,
    content: str,
) -> dict[str, Any]:
    """Write a text file under ``loopyard/``, creating the dir (+ parents) as
    needed. Returns the written entry descriptor. Refuses an escaping path or a
    ``relpath`` that names the dir root."""
    base = loopyard_dir(checkout, create=True)
    target = safe_join(base, relpath)
    if target == base:
        raise LoopyardPathError("write target must be a file path, not the dir root")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return _entry(base, target)


# ── JSON convenience (capabilities store JSON records under loopyard/<cap>/) ──
def read_json(checkout: str | os.PathLike[str], relpath: str) -> Any:
    """Read + parse a JSON file under ``loopyard/``."""
    return json.loads(read_file(checkout, relpath))


def write_json(
    checkout: str | os.PathLike[str],
    relpath: str,
    obj: Any,
) -> dict[str, Any]:
    """Serialise ``obj`` as pretty JSON and write it under ``loopyard/``."""
    return write_file(checkout, relpath, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def delete_file(checkout: str | os.PathLike[str], relpath: str) -> bool:
    """Delete a file under ``loopyard/``. Returns ``True`` if a file was
    removed, ``False`` if it did not exist. Refuses to remove a directory or an
    escaping path (keeps the manager append/edit-shaped, not a tree bomb)."""
    base = loopyard_dir(checkout)
    target = safe_join(base, relpath)
    if target == base or target.is_dir():
        raise LoopyardPathError("delete target must be a file, not a directory")
    if not target.exists():
        return False
    target.unlink()
    return True
