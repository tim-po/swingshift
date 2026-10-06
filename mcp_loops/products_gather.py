"""Auto-gather — deterministically attribute the loops we've ALREADY run to
products, and surface the products missing from the registry so the caller can
materialize them with ZERO manual entry.

This module is PURE (no filesystem access): the server reads each loop's config
and the existing product list and hands them in; here it's plain string/path
arithmetic, fully unit-testable and idempotent.

Attribution is STRICTLY BY GIT (2026-09): a product comes ONLY from an explicit
project binding or a real git remote — never from an arbitrary folder/path name.
Attribution signal per loop, strongest first:

  1. ``config.projectId``   — an explicit project id the loop already targets
                              (``config.productId`` accepted as a back-compat alias).
  2. ``config.gitRemote``   — a git URL → derive the product from it.

Everything else is UNATTRIBUTED (no product). The former name-derived signals — a
repoDir basename, an absolute cwd basename, or a folder path scraped from the goal —
invented a near-duplicate pseudo-project per loop and polluted the Projects surface,
so they are removed. A loop with no explicit project and no git remote simply has no
product, which is honest.
Every match collapses to ``{id, name, repoDir[, gitRemote, gitBranch]}``; a loop
is "attributed" iff its product id is already registered, else it is "discovered"
and wants materializing.
"""

from __future__ import annotations

import os
import re
from typing import Any

from mcp_loops.products import (
    derive_product_from_source,
    humanize,
    is_portable_relpath,
    slug,
)

# A REAL absolute path embedded in goal text — anchored to a known filesystem
# root so prose fragments like "$5/month", "AI/LLM", "HTML/CSS/JS" are NOT
# mistaken for paths (that produced junk products). Must be ~/… or a real root
# (/home, /Users, …) followed by at least one more segment.
_ABS_PATH_RE = re.compile(
    r"(?:~|/(?:home|Users|srv|opt|var|tmp|mnt|root|data|app|workspace|work|project|projects))"
    r"/[\w.\-]+(?:/[\w.\-]+)*"
)


# A product is a git repo or a ROOT FOLDER a loop runs in — never a file. These
# extensions mark a path as a file (e.g. a brief.md / RESULT.md scraped from a
# goal), so it is rejected rather than turned into a bogus product.
_FILE_EXTS = frozenset((
    "md", "txt", "rst", "py", "js", "ts", "tsx", "jsx", "json", "toml", "yaml",
    "yml", "sh", "bash", "html", "htm", "css", "scss", "png", "jpg", "jpeg",
    "gif", "svg", "webp", "pdf", "log", "csv", "tsv", "cfg", "ini", "lock",
    "xml", "rs", "go", "java", "rb", "c", "h", "hpp", "cpp", "sql", "env",
    "tgz", "gz", "zip", "bundle", "patch", "diff",
))


def _looks_like_file(path: str) -> bool:
    """True iff the path's basename ends in a known file extension — i.e. it is a
    file (brief.md), not a git repo or a root folder."""
    base = os.path.basename(str(path).replace("\\", "/").rstrip("/"))
    if "." not in base:
        return False
    return base.rsplit(".", 1)[-1].lower() in _FILE_EXTS


def _from_path_basename(path: str) -> dict:
    """Derive a product from a DIRECTORY path by its basename (id/name/repoDir).
    Returns ``{}`` for a file path — products come only from git repos + folders."""
    if _looks_like_file(path):
        return {}
    base = os.path.basename(str(path).replace("\\", "/").rstrip("/"))
    pid = slug(base)
    return {"id": pid, "name": humanize(base), "repoDir": pid} if pid else {}


def derive_for_loop(loop: dict) -> tuple[dict, str]:
    """Return ``(product_dict, signal)`` for one loop. ``signal`` names which
    rule fired — for transparency in the gather report. Never raises."""
    if not isinstance(loop, dict):
        return {}, "none"
    cfg = loop.get("config") if isinstance(loop.get("config"), dict) else loop
    name = loop.get("name") or cfg.get("name") or ""

    # 1) explicit project id already declared on the loop. `projectId` is the
    #    canonical binding (Round A rename); `productId` is the back-compat alias.
    #    The signal names whichever spelling the config actually carried.
    for _key in ("projectId", "productId"):
        pid = cfg.get(_key)
        if isinstance(pid, str) and slug(pid):
            s = slug(pid)
            return {"id": s, "name": humanize(pid), "repoDir": s}, _key

    # EXPLICIT ONLY (owner decision, loopyard-bug-1790177434): a loop belongs to
    # a project ONLY via an explicit projectId. A git remote (or a goal-named
    # target dir) is a GUESS — it is demoted to :func:`suggest_for_loop` and never
    # attributes, so the catalog never materializes an inferred project and the
    # surface never renders a guess as a real binding. No explicit id → none.
    return {}, "none"


def suggest_for_loop(loop: dict) -> tuple[str | None, str]:
    """Return ``(suggested_project_id, signal)`` for an UNBOUND loop — the demoted
    inference (loopyard-bug-1790177434). ``(None, "none")`` when the loop already
    carries an explicit binding (nothing to suggest) or no signal fires.

    Signals, strongest first: the single unambiguous target dir its goal names
    (:func:`target_project_from_goal`), else a real ``gitRemote`` on the config.
    Only ever a SUGGESTION — the user accepts it by writing ``projectId``. Pure,
    never raises."""
    if not isinstance(loop, dict):
        return None, "none"
    cfg = loop.get("config") if isinstance(loop.get("config"), dict) else loop
    for _key in ("projectId", "productId"):
        pid = cfg.get(_key)
        if isinstance(pid, str) and pid.strip():
            return None, "none"
    name = loop.get("name") or cfg.get("name") or None
    target = target_project_from_goal(cfg.get("goal"), loop_name=name)
    if target:
        return target, "goal"
    remote = cfg.get("gitRemote")
    if isinstance(remote, str) and remote.strip():
        d = derive_product_from_source(remote)
        if d.get("id"):
            return d["id"], "gitRemote"
    return None, "none"


# ── confident goal-target attribution (redesign spec §3, build-order §5.2) ──────
# The redesign needs the Loops switcher to discriminate REAL projects, but 0/60 of
# the live corpus carries an explicit `projectId` and the checkout git-remote /
# loop-slug both collapse the whole fleet into one dead bucket (`bot-swarm` /
# `swarmdev`). So attribution falls back to the ONE discriminating signal the data
# actually carries: the real TARGET a loop names in its goal. This is high-PRECISION
# by construction — a project is derived ONLY when the goal names exactly ONE
# distinct top-level project directory (a goal that names several targets, or none,
# stays honestly Unattributed). It is READ-TIME + PURE (no filesystem, no mutation),
# never the removed near-duplicate basename scrape (which invented a pseudo-project
# per loop from a *deeper* path segment): here we key on the top-level project ROOT,
# so `.../bot-swarm-brand/landing.html` and `.../bot-swarm-brand/voice.md` both map
# to the single project `swingshift-brand`.

def _project_root_of(path: str) -> str | None:
    """The top-level PROJECT directory a real absolute path lives under — the repo
    root, not the deep file. ``/path/to/swingshift/landing.html`` →
    ``swingshift-brand``; ``~/weave/core`` → ``weave``. ``None`` when the path is
    too shallow to name a project (e.g. ``/workspace`` alone)."""
    p = str(path or "").replace("\\", "/")
    if p.startswith("~"):
        segs = [s for s in p.split("/") if s and s != "~"]
        return segs[0] if segs else None
    segs = [s for s in p.split("/") if s]
    if not segs:
        return None
    # /home/<user>/<PROJECT>/… and /Users/<user>/<PROJECT>/… nest one level deeper
    # (the account name) than single-tenant roots like /srv/<PROJECT> or /opt/<X>.
    if segs[0] in ("home", "Users"):
        return segs[2] if len(segs) >= 3 else None
    return segs[1] if len(segs) >= 2 else None


def target_project_from_goal(goal: Any, *, loop_name: Any = None) -> str | None:
    """The CONFIDENT project id a loop targets, read off its goal text, or ``None``.

    Returns a project id (a :func:`slug`) ONLY when the goal names exactly ONE
    distinct top-level project directory — an unambiguous target. Zero named
    targets, or two-or-more distinct ones, return ``None`` (honest "Unattributed").
    A path whose project root is the loop's own name (its state/output dir) is
    excluded so a loop never attributes to itself. Never derives from a git remote
    or the loop slug (redesign §3); never raises."""
    if not isinstance(goal, str) or not goal:
        return None
    self_slug = slug(loop_name) if loop_name else None
    candidates: set[str] = set()
    for match in _ABS_PATH_RE.findall(goal):
        root = _project_root_of(match)
        if not root:
            continue
        sid = slug(root)
        if not sid or sid == self_slug:
            continue
        candidates.add(sid)
    return next(iter(candidates)) if len(candidates) == 1 else None


def attribute_loops(loops: list[dict], existing_products: list[dict]) -> dict:
    """Attribute every loop to a product. Returns::

        {
          "attribution":  {loop_name: product_id, ...},
          "discovered":   [product_dict, ...],   # NOT yet in the registry
          "unattributed": [loop_name, ...],      # loops whose product is missing
          "signals":      {loop_name: signal, ...},
          "suggestions":  {loop_name: suggested_project_id, ...},  # unbound only
        }

    Idempotent: when every loop's product already exists, ``discovered`` is empty.
    Discovered products are deduped by id and carry a ``_signal`` breadcrumb."""
    existing_ids = {
        p.get("id") for p in (existing_products or [])
        if isinstance(p, dict) and isinstance(p.get("id"), str) and p.get("id")
    }
    attribution: dict[str, str] = {}
    signals: dict[str, str] = {}
    discovered: dict[str, dict] = {}
    unattributed: list[str] = []
    suggestions: dict[str, str] = {}

    for loop in (loops or []):
        if not isinstance(loop, dict):
            continue
        name = loop.get("name") or (loop.get("config") or {}).get("name")
        if not isinstance(name, str) or not name:
            continue
        cand, signal = derive_for_loop(loop)
        pid = cand.get("id")
        if not pid:
            sug, _sig = suggest_for_loop(loop)
            if sug:
                suggestions[name] = sug
            continue
        attribution[name] = pid
        signals[name] = signal
        if pid not in existing_ids:
            unattributed.append(name)
            if pid not in discovered:
                discovered[pid] = {**cand, "_signal": signal}

    return {
        "attribution": attribution,
        "discovered": list(discovered.values()),
        "unattributed": unattributed,
        "signals": signals,
        "suggestions": suggestions,
    }
