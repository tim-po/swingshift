"""Products — first-class registry entries that loops and standalone runs TARGET.

A PRODUCT is the thing an agent works ON: a repo/dir + its git coordinates + an
output root. One generic loop ("run my reviewer") can point at ANY product, and
a standalone run is just ``agent × product × model``. Products are the moat's
foundation: a shared library of loops/agents only works if what they target is
**portable** — so this model bakes in NO host-absolute paths. It stores the
git remote/branch (portable source of truth) + *relative* ``repoDir`` /
``outputRoot``; the host-specific absolute paths are resolved at RUN TIME on the
user's own box (see :func:`resolve_paths`), never persisted into a shareable
config.

Record shape (``schema`` 2)::

    {
      "id": "weave",
      "kind": "product",
      "schema": 2,
      "name": "Weave",
      "gitRemote": "git@github.com:acme/weave.git",  # canonical identity (nullable
                                                     #   ONLY while local-only)
      "gitBranch": "main",
      "subPath": "",             # RELATIVE in-repo path for a monorepo sub-project;
                                 #   empty = repo root. Owns the in-repo identity.
      "outputRoot": "weave",     # RELATIVE to the host's output base (portable)
      "repoDir": "weave",        # DEMOTED to a host-resolution hint (was identity)
      "origin": null,            # optional Origin/host this checkout lives on
      "aka": [],                 # alias id list (rename / merge history)
      "note": "…",
      "saved": 1789000000.0
    }

Schema 1 (``gitRemote``/``gitBranch``/``repoDir``/``outputRoot``/``note``) still
loads LOSSLESSLY — :func:`migrate_product` upcasts it on read, filling the
schema-2 additions with empty defaults and stamping ``schema=2``; no field is
dropped. ``gitRemote`` is the CANONICAL identity in schema 2 (``repoDir`` is only
a host hint), which is what :func:`merge_suggestions` matches on.

This module is pure: only string/path arithmetic (``os.path`` is not filesystem
I/O), no reads or writes. The server owns persistence and injects ``now`` and the
host-specific roots.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

PRODUCT_SCHEMA_VERSION = 2

# Fields that MUST stay relative so a product config is portable across hosts.
PORTABLE_PATH_FIELDS: tuple[str, ...] = ("repoDir", "outputRoot")

DEFAULT_BRANCH = "main"


def is_portable_relpath(p: Any) -> bool:
    """True iff ``p`` is a non-empty RELATIVE path safe to share across hosts:
    not absolute, not ``~``-anchored, and with no ``..`` traversal segment."""
    if not isinstance(p, str) or not p.strip():
        return False
    q = p.strip()
    if q.startswith("~") or os.path.isabs(q):
        return False
    segments = q.replace("\\", "/").split("/")
    return ".." not in segments


def _slugify_relpath(p: str) -> str:
    return p.strip().replace("\\", "/").strip("/")


# ── deriving a product from ONE string (git URL or directory path) ────────────
# The whole point: a product is NOTHING more than a git repo URL OR a path to a
# directory. Everything else (id, display name, portable repoDir) is DERIVED,
# deterministically, from that single input — no hand-typed metadata.

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# scheme://…  (https, http, ssh, git, file, ftp…): a URL by its scheme.
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
# scp-style git remote: user@host:owner/repo(.git) — the classic git@github form.
_SCP_URL_RE = re.compile(r"^[^\s/@]+@[^\s/@:]+:.+")


def slug(text: Any) -> str:
    """Deterministic id-slug: lowercase, ``[a-z0-9-]`` only, runs of anything
    else collapsed to a single ``-`` and trimmed off the ends. ``"My_App v2"`` →
    ``"my-app-v2"``. Returns ``""`` when nothing slug-worthy remains."""
    return _SLUG_RE.sub("-", str(text or "").strip().lower()).strip("-")


def humanize(basename: Any) -> str:
    """Human display name from a repo/dir basename: ``-``/``_`` → space,
    whitespace collapsed, title-cased. ``"weave-core"`` → ``"Weave Core"``."""
    s = re.sub(r"[\s_\-]+", " ", str(basename or "")).strip()
    return s.title() if s else ""


def looks_like_git_url(source: Any) -> bool:
    """True iff ``source`` is a git URL (has a ``scheme://`` or is scp-style
    ``user@host:path``) rather than a filesystem path. Deterministic — no I/O."""
    if not isinstance(source, str):
        return False
    s = source.strip()
    return bool(_URL_SCHEME_RE.match(s) or _SCP_URL_RE.match(s))


def _repo_basename(text: str) -> str:
    """Last path segment of a git URL or path, with a trailing ``.git`` and any
    trailing slashes stripped. Handles both ``/`` and scp ``:`` separators."""
    t = text.strip().replace("\\", "/").rstrip("/")
    if t.endswith(".git"):
        t = t[:-4]
    t = t.rstrip("/")
    # split on both path-sep and the scp colon so git@host:owner/repo → repo
    seg = re.split(r"[/:]", t)[-1] if t else ""
    return seg


def derive_product_from_source(source: Any) -> dict:
    """Derive a product from ONE string — the entire input for a trivial add.

    A git URL (``git@host:owner/repo.git``, ``https://host/owner/repo(.git)``,
    ``ssh://…``) → ``{id, name, gitRemote, gitBranch, repoDir}`` (remote kept
    verbatim as the portable source of truth). A directory path (``/abs/products/
    weave`` or ``weave``) → ``{id, name, repoDir}`` from its basename. Returns
    ``{}`` when ``source`` is empty/unusable. Pure string arithmetic — no I/O."""
    if not isinstance(source, str) or not source.strip():
        return {}
    s = source.strip()
    base = _repo_basename(s)
    pid = slug(base)
    if not pid:
        return {}
    if looks_like_git_url(s):
        return {"id": pid, "name": humanize(base), "gitRemote": s,
                "gitBranch": DEFAULT_BRANCH, "repoDir": pid}
    return {"id": pid, "name": humanize(base), "repoDir": pid}


def _autoderive(raw: dict) -> dict:
    """Return a copy of ``raw`` with ``id``/``name`` (and the source coordinates)
    filled in from a single ``source`` string, or from an existing
    ``gitRemote``/``repoDir``, whenever they're missing. Hand-typed fields always
    win; derivation only ever FILLS blanks. This is what lets a save arrive with
    nothing but ``{"source": "<git url or path>"}``."""
    out = dict(raw)

    def _blank(key: str) -> bool:
        v = out.get(key)
        return not (isinstance(v, str) and v.strip())

    # 1) an explicit single `source` string is the richest signal
    src = out.pop("source", None)
    derived = derive_product_from_source(src) if isinstance(src, str) else {}
    for k in ("gitRemote", "gitBranch", "repoDir"):
        if derived.get(k) and _blank(k):
            out[k] = derived[k]

    # 2) still missing id/name? derive from whatever source coordinate we have
    if _blank("id") or _blank("name"):
        base = derived
        if not base:
            if not _blank("gitRemote") and looks_like_git_url(out["gitRemote"]):
                base = derive_product_from_source(out["gitRemote"])
            elif not _blank("repoDir"):
                bn = _repo_basename(out["repoDir"])
                base = {"id": slug(bn), "name": humanize(bn)} if slug(bn) else {}
            elif not _blank("gitRemote"):
                bn = _repo_basename(out["gitRemote"])
                base = {"id": slug(bn), "name": humanize(bn)} if slug(bn) else {}
        if base:
            if _blank("id") and base.get("id"):
                out["id"] = base["id"]
            if _blank("name") and base.get("name"):
                out["name"] = base["name"]
    return out


def validate_product(raw: Any, *, strict: bool = False) -> dict:
    """Validate + normalize a product. Returns ``{ok, errors, warnings, product}``.

    Accepts a single ``source`` string (a git URL or a directory path) and/or an
    explicit ``gitRemote``/``repoDir``; ``id`` and ``name`` are AUTO-DERIVED from
    those when omitted (see :func:`derive_product_from_source`), so a trivial add
    can pass nothing but ``{"source": "<git url or path>"}``. Still requires, in
    the end, an ``id`` + ``name`` and at least one source coordinate (``gitRemote``
    OR ``repoDir``). ``repoDir``/``outputRoot`` must be portable relative paths —
    an absolute or ``~``/``..`` path is an ERROR (it would break the shared
    library). ``outputRoot`` defaults to ``id``; ``gitBranch`` to ``main``.
    """
    errors: list[str] = []
    warnings: list[str] = []

    def fail(msg: str) -> None:
        if strict:
            raise ValueError(msg)
        errors.append(msg)

    if not isinstance(raw, dict):
        fail(f"product must be an object, got {type(raw).__name__}")
        return {"ok": False, "errors": errors, "warnings": warnings, "product": {}}

    # AUTO-DERIVE: fill missing id/name (+ source coords) from a single `source`
    # string or an existing gitRemote/repoDir, so a save no longer needs any
    # hand-typed metadata. Explicit fields always win.
    raw = _autoderive(raw)

    prod: dict[str, Any] = {"kind": "product", "schema": PRODUCT_SCHEMA_VERSION}

    pid = raw.get("id")
    if not isinstance(pid, str) or not pid.strip():
        fail("`id` is required and must be a non-empty string")
        pid = pid if isinstance(pid, str) else ""
    prod["id"] = pid.strip()

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        fail("`name` is required and must be a non-empty string")
        name = name if isinstance(name, str) else ""
    prod["name"] = name.strip()

    remote = raw.get("gitRemote")
    repo_dir = raw.get("repoDir")
    if not (isinstance(remote, str) and remote.strip()) and \
       not (isinstance(repo_dir, str) and repo_dir.strip()):
        fail("provide a source: at least one of `gitRemote` or `repoDir`")

    prod["gitRemote"] = remote.strip() if isinstance(remote, str) and remote.strip() else None
    branch = raw.get("gitBranch")
    prod["gitBranch"] = branch.strip() if isinstance(branch, str) and branch.strip() \
        else DEFAULT_BRANCH

    # repoDir: optional, but if present must be portable
    if isinstance(repo_dir, str) and repo_dir.strip():
        if not is_portable_relpath(repo_dir):
            fail("`repoDir` must be a RELATIVE portable path (no absolute / ~ / .. — "
                 "host paths resolve at run time)")
            prod["repoDir"] = None
        else:
            prod["repoDir"] = _slugify_relpath(repo_dir)
    else:
        prod["repoDir"] = _slugify_relpath(pid) if pid else None

    # outputRoot: default to id; must be portable
    out_root = raw.get("outputRoot")
    if isinstance(out_root, str) and out_root.strip():
        if not is_portable_relpath(out_root):
            fail("`outputRoot` must be a RELATIVE portable path (no absolute / ~ / ..)")
            prod["outputRoot"] = _slugify_relpath(pid) if pid else None
        else:
            prod["outputRoot"] = _slugify_relpath(out_root)
    else:
        prod["outputRoot"] = _slugify_relpath(pid) if pid else None

    # subPath (schema 2): optional RELATIVE in-repo path for a monorepo sub-project.
    # Empty = repo root. It now OWNS the in-repo identity role (repoDir is only a
    # host-resolution hint); must be portable, like every shareable path.
    sub = raw.get("subPath")
    if isinstance(sub, str) and sub.strip():
        if not is_portable_relpath(sub):
            fail("`subPath` must be a RELATIVE portable in-repo path (no absolute / ~ / ..)")
            prod["subPath"] = ""
        else:
            prod["subPath"] = _slugify_relpath(sub)
    else:
        prod["subPath"] = ""

    # origin (schema 2): optional Origin/host id this checkout lives on (nullable).
    origin = raw.get("origin")
    prod["origin"] = origin.strip() if isinstance(origin, str) and origin.strip() else None

    # aka (schema 2): alias id list (from a rename or a merge), deduped, order kept.
    aka: list[str] = []
    aka_in = raw.get("aka")
    if isinstance(aka_in, list):
        for a in aka_in:
            if isinstance(a, str) and a.strip() and a.strip() not in aka:
                aka.append(a.strip())
    prod["aka"] = aka

    prod["note"] = raw.get("note", "") if isinstance(raw.get("note"), str) else ""

    ok = not errors
    return {"ok": ok, "errors": errors, "warnings": warnings, "product": prod}


def resolve_paths(product: dict, *, products_root: str, output_base: str) -> dict:
    """RUN-TIME resolution of a product's portable relatives against host-specific
    roots. Returns absolute ``{repoPath, outputPath}`` — computed on the user's
    box, never stored back into the (portable) product config. A schema-2
    ``subPath`` (a monorepo sub-project) is appended INSIDE the checkout, so
    ``repoPath`` points at the sub-project the loop actually works on."""
    repo_rel = product.get("repoDir") or product.get("id") or ""
    out_rel = product.get("outputRoot") or product.get("id") or ""
    repo_path = os.path.join(products_root, repo_rel) if repo_rel else products_root
    sub = product.get("subPath")
    if isinstance(sub, str) and sub.strip() and is_portable_relpath(sub):
        repo_path = os.path.join(repo_path, _slugify_relpath(sub))
    return {
        "repoPath": repo_path,
        "outputPath": os.path.join(output_base, out_rel) if out_rel else output_base,
    }


# ── schema-2 upcast (READ path) ───────────────────────────────────────────────

def migrate_product(raw: Any) -> dict:
    """Upcast a STORED product record to the current schema on READ, LOSSLESSLY.

    Schema-1 records load unchanged — this only FILLS the schema-2 additions
    (``subPath``, ``origin``, ``aka``), normalizes ``gitRemote``/``gitBranch``,
    and stamps ``schema=2``. Every other field (``id``, ``name``, ``repoDir``,
    ``outputRoot``, ``note``, ``saved``, and any server breadcrumb like
    ``gathered``) is PRESERVED — ``repoDir`` is simply DEMOTED in meaning to a
    host-resolution hint. Tolerant (never raises): this runs on every registry
    read, so a malformed record round-trips as best it can. Pure — no I/O."""
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)                      # preserve everything → no data loss
    out["kind"] = "product"
    out["schema"] = PRODUCT_SCHEMA_VERSION
    rem = out.get("gitRemote")
    out["gitRemote"] = rem.strip() if isinstance(rem, str) and rem.strip() else None
    br = out.get("gitBranch")
    out["gitBranch"] = br.strip() if isinstance(br, str) and br.strip() else DEFAULT_BRANCH
    sub = out.get("subPath")
    out["subPath"] = (_slugify_relpath(sub)
                      if isinstance(sub, str) and sub.strip() and is_portable_relpath(sub)
                      else "")
    org = out.get("origin")
    out["origin"] = org.strip() if isinstance(org, str) and org.strip() else None
    aka: list[str] = []
    aka_in = out.get("aka")
    if isinstance(aka_in, list):
        for a in aka_in:
            if isinstance(a, str) and a.strip() and a.strip() not in aka:
                aka.append(a.strip())
    out["aka"] = aka
    if not (isinstance(out.get("outputRoot"), str) and out["outputRoot"].strip()):
        pid = out.get("id")
        if isinstance(pid, str) and pid.strip():
            out["outputRoot"] = _slugify_relpath(pid)
    return out


# ── git-remote identity + merge SUGGESTIONS (advisory only) ───────────────────

def normalize_git_remote(remote: Any) -> str:
    """Canonical IDENTITY key for a git remote, for MATCHING two products to the
    same upstream. Lowercases the host, drops any userinfo, strips a trailing
    ``.git`` and slashes, and canonicalizes scp-style ``user@host:owner/repo`` to
    ``host/owner/repo``. Returns ``""`` for a blank/None remote. Pure — no I/O.
    An identity key, NOT a fetchable URL (scheme + auth are intentionally lost)."""
    if not isinstance(remote, str) or not remote.strip():
        return ""
    s = remote.strip()
    # scp-style: user@host:owner/repo(.git) → https://host/owner/repo (temporarily)
    m = re.match(r"^[^\s/@]+@([^\s/@:]+):(.+)$", s)
    if m and "://" not in s:
        s = f"https://{m.group(1)}/{m.group(2)}"
    # scheme://[user@]host/path → host/path  (drop scheme + userinfo)
    m2 = re.match(r"^[a-zA-Z][\w+.\-]*://(?:[^/@]+@)?([^/]+)/(.*)$", s)
    if m2:
        s = f"{m2.group(1).lower()}/{m2.group(2)}"
    else:
        s = s.lower()
    if s.endswith(".git"):
        s = s[:-4]
    return s.rstrip("/")


def merge_suggestions(product_list: Any, *, common_dirs: Any = None) -> list[dict]:
    """ADVISORY ONLY — group products that likely denote the SAME upstream so the
    Products view can SUGGEST a merge. This NEVER collapses, renames, or deletes
    anything: it just returns groups of ids.

    Two products are 'same' iff their normalized ``gitRemote`` matches (see
    :func:`normalize_git_remote`) OR their resolved checkouts share a git
    common-dir — the caller passes ``common_dirs={id: common_dir}`` from a
    filesystem probe (kept OUT of this pure function; ``None`` = remote-only).

    Returns ``[{"key", "reason", "ids": [...]}]``, one entry per group of >=2
    products, ids in first-seen order. Single products (including EVERY no-git
    folder product — they have no identity to match) are never grouped. Pure."""
    items = [p for p in (product_list or []) if isinstance(p, dict) and p.get("id")]
    parent: dict[str, str] = {p["id"]: p["id"] for p in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 1) union by normalized remote identity
    by_remote: dict[str, str] = {}      # remote-key → first product id
    remote_key: dict[str, str] = {}     # product id → its remote-key ("" = no git)
    for p in items:
        key = normalize_git_remote(p.get("gitRemote"))
        remote_key[p["id"]] = key
        if not key:
            continue                     # no-git folder product: never a match
        if key in by_remote:
            union(by_remote[key], p["id"])
        else:
            by_remote[key] = p["id"]

    # 2) union by shared git common-dir (from the caller's filesystem probe)
    if isinstance(common_dirs, dict):
        by_cdir: dict[str, str] = {}
        for p in items:
            cd = common_dirs.get(p["id"])
            if not (isinstance(cd, str) and cd.strip()):
                continue
            cd = cd.strip()
            if cd in by_cdir:
                union(by_cdir[cd], p["id"])
            else:
                by_cdir[cd] = p["id"]

    groups: dict[str, list[str]] = {}
    for p in items:                      # first-seen order preserved
        groups.setdefault(find(p["id"]), []).append(p["id"])

    out: list[dict] = []
    for root, ids in groups.items():
        if len(ids) < 2:
            continue
        key = remote_key.get(root) or ""
        reason = "shared git remote" if key else "shared git checkout"
        out.append({"key": key or root, "reason": reason, "ids": ids})
    return out
