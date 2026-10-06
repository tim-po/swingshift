"""The **capability mechanism** (Round B1, #6 — the platform primitive).

A *capability* is a small, git-shaped package that contributes any of:

* typed **structure** — the entity-kind(s) it manages (e.g. ``note``);
* a **page** — a DECLARATIVE UI descriptor the dashboard shell renders;
* **settings** — a lean schema;
* an **attached loop** — its agent-native behaviour (seed + run a loop, see the
  result).

The split the platform honours: the **UI is declarative** (rendered by the
dashboard shell, not shipped as executable JS-with-privileges); the **backend +
attached loop** run on the origin/engine; the **data** lives under the Project's
``loopyard/<capability>/`` (see :mod:`mcp_loops.loopyard`).

This module is PURE: it parses a manifest, discovers bundled capabilities from a
known dir, and builds the attached-loop *config* — no server, no git, no loop
engine. The server/dashboard layer (``server.py`` tools + ``loops_panel``
handlers) resolves the Project checkout, calls the ``/loopyard`` manager for
data, and drives ``loop_save``/``loop_start`` for the attached loop.

NO SANDBOX: bundled, first-party capabilities are trusted. Sandboxing is a
later shared-marketplace concern and is deliberately not built here.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_loops.projects import slug

# A capability folder carries its manifest under this fixed name.
MANIFEST_FILE = "manifest.json"

# id/data-dir charset — a plain slug, so it is safe as a path fragment and a
# loop-name fragment (mirrors the loop/name validation elsewhere).
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Page kinds the dashboard shell knows how to render declaratively.
PAGE_KINDS = {"html"}


class CapabilityError(ValueError):
    """A manifest is malformed / a capability op is invalid. Raised so the
    caller can surface a clean 400 rather than mounting a broken section."""


@dataclass
class Capability:
    """A parsed, normalized capability manifest (+ where it lives on disk)."""

    id: str
    name: str
    version: str = "1"
    entity_kinds: list[str] = field(default_factory=list)
    # declarative page descriptor, e.g. {"kind":"html","asset":"page.html"}
    page: dict[str, Any] = field(default_factory=dict)
    # optional attached loop: {"title":..., "config": <loop cfg>, "seedFrom": <dir>}
    attached_loop: dict[str, Any] | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    # loopyard/<data_dir>/ is where this capability's project data lives.
    data_dir: str = ""
    root: Path | None = None  # folder on disk; None for an in-memory manifest.

    def as_dict(self) -> dict[str, Any]:
        """Wire form for the dashboard — no on-disk ``root`` leaks out, and a
        ``hasAttachedLoop`` flag saves the client a null-check."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "entityKinds": list(self.entity_kinds),
            "page": dict(self.page),
            "dataDir": self.data_dir,
            "settings": dict(self.settings),
            "hasAttachedLoop": self.attached_loop is not None,
            "attachedLoop": (
                {"title": (self.attached_loop or {}).get("title", "Run")}
                if self.attached_loop is not None else None
            ),
        }

    def page_asset_path(self) -> Path:
        """Absolute path to the declarative page asset inside the cap folder."""
        if self.root is None:
            raise CapabilityError(f"capability {self.id!r} has no on-disk root")
        asset = self.page.get("asset")
        if not asset:
            raise CapabilityError(f"capability {self.id!r} page has no asset")
        # asset is a manifest-controlled (trusted) relative name; still refuse an
        # obvious escape so a bad manifest can't read outside its own folder.
        p = (self.root / asset).resolve()
        if self.root.resolve() not in p.parents:
            raise CapabilityError(f"page asset escapes capability folder: {asset!r}")
        return p

    def read_page(self) -> str:
        """The declarative page markup the dashboard shell renders."""
        return self.page_asset_path().read_text(encoding="utf-8")


def parse_manifest(obj: Any, *, root: str | Path | None = None) -> Capability:
    """Validate + normalize a manifest object into a :class:`Capability`.

    Raises :class:`CapabilityError` on any structural problem. Keep this the ONE
    place manifest shape is enforced.
    """
    if not isinstance(obj, dict):
        raise CapabilityError(f"manifest must be an object, got {type(obj).__name__}")

    cid = obj.get("id")
    if not isinstance(cid, str) or not _ID_RE.match(cid):
        raise CapabilityError(f"manifest `id` must be a slug, got {cid!r}")

    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CapabilityError(f"capability {cid!r}: `name` is required")

    version = str(obj.get("version", "1"))

    entity_kinds = obj.get("entityKinds", [])
    if not isinstance(entity_kinds, list) or not all(isinstance(k, str) for k in entity_kinds):
        raise CapabilityError(f"capability {cid!r}: `entityKinds` must be a list of strings")

    page = obj.get("page", {})
    if not isinstance(page, dict) or not page:
        raise CapabilityError(f"capability {cid!r}: `page` is required (declarative descriptor)")
    kind = page.get("kind")
    if kind not in PAGE_KINDS:
        raise CapabilityError(
            f"capability {cid!r}: page.kind must be one of {sorted(PAGE_KINDS)}, got {kind!r}")

    attached = obj.get("attachedLoop")
    if attached is not None:
        if not isinstance(attached, dict):
            raise CapabilityError(f"capability {cid!r}: `attachedLoop` must be an object")
        if "config" not in attached or not isinstance(attached["config"], dict):
            raise CapabilityError(
                f"capability {cid!r}: `attachedLoop.config` (an inline loop config) is required")

    settings = obj.get("settings", {})
    if not isinstance(settings, dict):
        raise CapabilityError(f"capability {cid!r}: `settings` must be an object")

    data_dir = obj.get("dataDir") or cid
    if not isinstance(data_dir, str) or not _ID_RE.match(data_dir):
        raise CapabilityError(f"capability {cid!r}: `dataDir` must be a slug, got {data_dir!r}")

    return Capability(
        id=cid,
        name=name.strip(),
        version=version,
        entity_kinds=entity_kinds,
        page=page,
        attached_loop=attached,
        settings=settings,
        data_dir=data_dir,
        root=Path(root) if root is not None else None,
    )


def load_capability(folder: str | Path) -> Capability:
    """Load one capability from a folder containing ``manifest.json``."""
    folder = Path(folder)
    mpath = folder / MANIFEST_FILE
    if not mpath.exists():
        raise CapabilityError(f"no {MANIFEST_FILE} in {folder}")
    try:
        obj = json.loads(mpath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CapabilityError(f"{mpath}: invalid JSON: {e}") from e
    return parse_manifest(obj, root=folder)


def bundled_dir() -> Path:
    """The known dir the dashboard discovers bundled capabilities from.

    Ships inside the package (``mcp_loops/capabilities_bundled``) so discovery is
    real: dropping a folder in or out changes what mounts, with no registry.
    """
    return Path(__file__).resolve().parent / "capabilities_bundled"


def discover(bundled: str | Path | None = None) -> list[Capability]:
    """Discover valid bundled capabilities, sorted by id.

    Malformed folders are skipped (see :func:`discover_with_errors` for why one
    was dropped). Discovery is directory-driven: add/remove a folder → the set
    changes. That IS the mechanism (ACCEPT #6).
    """
    caps, _ = discover_with_errors(bundled)
    return caps


def discover_with_errors(
    bundled: str | Path | None = None,
) -> tuple[list[Capability], list[dict[str, str]]]:
    """Like :func:`discover` but also returns ``[{id/folder, error}]`` for every
    folder that failed to load — so a bad manifest is visible, not silent."""
    root = Path(bundled) if bundled is not None else bundled_dir()
    caps: list[Capability] = []
    errors: list[dict[str, str]] = []
    if not root.exists():
        return caps, errors
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / MANIFEST_FILE).exists():
            continue
        try:
            caps.append(load_capability(child))
        except CapabilityError as e:
            errors.append({"folder": child.name, "error": str(e)})
    caps.sort(key=lambda c: c.id)
    return caps, errors


def get(cap_id: str, bundled: str | Path | None = None) -> Capability:
    """Discover + return one capability by id, or raise :class:`CapabilityError`."""
    for c in discover(bundled):
        if c.id == cap_id:
            return c
    raise CapabilityError(f"no such capability: {cap_id!r}")


# ── attached loop ────────────────────────────────────────────────────────────
def attached_loop_name(cap: Capability, project_id: str) -> str:
    """Deterministic, per-(capability, project) loop name — so re-triggering
    reuses one loop record instead of littering the registry."""
    return f"cap-{slug(cap.id)}-{slug(project_id)}"


def build_attached_loop(
    cap: Capability,
    project_id: str,
    seed_summary: str = "",
) -> dict[str, Any]:
    """Build the loop config to hand to ``loop_save`` for this capability's
    attached loop, seeded with the project + a short data summary.

    The manifest's inline ``config`` carries GENERIC, role-based agent goals; all
    per-run specifics (which project, what data) are appended to the TOP-LEVEL
    goal here — exactly the reusable-team rule the linter enforces. Pure: returns
    a config dict; it does not save or start anything.
    """
    if cap.attached_loop is None:
        raise CapabilityError(f"capability {cap.id!r} has no attached loop")
    cfg = copy.deepcopy(cap.attached_loop["config"])
    cfg["name"] = attached_loop_name(cap, project_id)
    base_goal = str(cfg.get("goal", "")).strip()
    context = f"\n\nProject: {project_id}. Capability: {cap.id} (data under loopyard/{cap.data_dir}/)."
    if seed_summary.strip():
        context += f"\nCurrent data:\n{seed_summary.strip()}"
    cfg["goal"] = base_goal + context
    return cfg
