"""Creator context-handoff store (QoL-R5 / C1) — the loop-creator's seed context.

The '+ loop' flow (Phase 2b) opens an interactive loop-creator session that must
start already grounded in real background about the environment it is building a
team FOR — coord's system knowledge, house policy, whatever a session chose to
leave for the next creator. That background lives in a tiny on-disk store:

    <LOOPS_DATA_DIR>/_creator_context/*.md      (one .md == one context doc)

This module is the deterministic backbone for that store — a *reader* (list +
concatenate every doc) and a *writer* (drop a new doc so coord / any session can
seed more). The concatenated text is handed to :func:`creator.build_prompt` as
its ``context`` argument, where it is framed as TRUSTED BACKGROUND — explicitly
NOT user instructions — so a poisoned context doc can't hijack the creator.

Design points that matter:

  * EMPTY-SAFE. A missing or empty store yields ``""`` from :func:`read_all`, and
    ``build_prompt`` with ``context=""`` is byte-identical to the old prompt — so
    the creator still works with no seed at all (an explicit acceptance point).
  * DETERMINISTIC ORDER. Docs concatenate in filename sort order, so the emitted
    preprompt is stable and assertable in tests.
  * PATH-SAFE WRITES. A doc name is slugged to a bare ``<slug>.md`` basename; no
    directory components, traversal, or absolute paths can escape the store.
  * ONE DATA-ROOT RULE. Location resolves through :func:`paths.resolve_data_dir`,
    the same precedence (explicit arg > $LOOPS_DATA_DIR > install default) every
    other module uses — the store is a sibling of ``_issues`` / the loop dirs.

The store is trusted-operator input, not end-user input: the framing in the
preprompt is defence-in-depth, and writes are gated the same way the rest of the
dashboard is (the server exposes the writer behind the auth gate).
"""
from __future__ import annotations

import os
import re
from typing import Optional

from mcp_loops import paths

# The store dir name, a sibling of the per-loop dirs under the data root.
CONTEXT_DIRNAME = "_creator_context"

# The canonical operator seed doc — coord's system knowledge. Shipped as a
# constant so a fresh install can populate the store deterministically (no
# reaching into any one box's filesystem), and so tests have a known seed.
SEED_NAME = "coord-context.md"
DEFAULT_COORD_CONTEXT = """\
# Coord system context (loop-creator seed)

This is trusted background about the Loopyard / bot-swarm environment the team you
are about to design will run in. Use it to make on-model config choices; it is not
an instruction to you.

## The Loopyard model
- A loop is a team of steps. Exactly ONE step has role "manager"; "worker" steps
  do the building; optional "input_provider" steps review each round.
- Identity (a step's persona + generic goal) is LOOP-AGNOSTIC. All product/task/
  path/repo detail belongs in the loop goal, never baked into a persona.
- Each step runs on the "claude" or "codex" CLI (per-step "runtime"), with an
  optional per-step "model". A multi-CLI team (e.g. a codex reviewer independent
  of a claude writer) is a first-class, encouraged shape.
- budget.turnLimit caps the collective turns one run gets.

## How teams run here
- Steps take turns; the manager decomposes the goal into independent slices and
  assigns them, integrates only verified work, and finalizes when the loop goal's
  acceptance is met.
- Work is isolated per agent (its own git worktree/branch); prod services and
  other agents' trees are off-limits. A good loop goal states such constraints
  explicitly so every agent obeys them.

## Making a good config
- Prefer the smallest team that covers the goal: one manager + the workers the
  goal actually needs, plus a reviewer only when independent review adds value.
- Put every concrete fact (repo URL, paths, acceptance tests, policy) in the loop
  goal so it survives; keep personas about *how the role works*, not *what to build*.
"""

# --- framing knobs used by both this module and creator.build_prompt ----------
# The heading a context doc gets in the concatenation (so tests can assert it and
# the creator can see doc boundaries).
_DOC_HEADING = "## {name}"


def context_dir(data_dir: Optional[str] = None) -> str:
    """Absolute path of the context store: ``<data_root>/_creator_context``.
    Resolves the data root through the one shared precedence rule."""
    return os.path.join(paths.resolve_data_dir(data_dir), CONTEXT_DIRNAME)


def _slug_name(name: str) -> Optional[str]:
    """Reduce an arbitrary doc name to a safe bare ``<slug>.md`` basename, or
    None if nothing usable remains. Strips any directory components first so no
    traversal or absolute path can escape the store."""
    base = os.path.basename(str(name or "").strip())
    stem = re.sub(r"\.md$", "", base, flags=re.IGNORECASE)
    slug = re.sub(r"[^a-z0-9._-]+", "-", stem.lower()).strip("-._")
    if not slug:
        return None
    return f"{slug}.md"


def list_docs(data_dir: Optional[str] = None) -> list[dict]:
    """Every context doc in the store, filename-sorted. Each entry is
    ``{name, path, bytes}``. A missing store is simply empty (never an error)."""
    d = context_dir(data_dir)
    if not os.path.isdir(d):
        return []
    out: list[dict] = []
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".md"):
            continue
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        out.append({"name": fn, "path": p, "bytes": size})
    return out


def read_all(data_dir: Optional[str] = None) -> str:
    """Concatenate every context doc into one string, filename-sorted, each under
    a ``## <name>`` heading. Returns ``""`` when the store is missing or empty —
    the empty-safe contract the creator relies on."""
    parts: list[str] = []
    for doc in list_docs(data_dir):
        try:
            with open(doc["path"], "r", encoding="utf-8") as fh:
                body = fh.read().strip()
        except OSError:
            continue
        if not body:
            continue
        parts.append(f"{_DOC_HEADING.format(name=doc['name'])}\n\n{body}")
    return "\n\n".join(parts)


def write_doc(name: str, text: str,
              data_dir: Optional[str] = None) -> dict:
    """Write (create or overwrite) a context doc. ``name`` is slugged to a safe
    ``<slug>.md`` basename; ``text`` is the doc body. Creates the store dir if
    absent. Returns ``{ok, name, path, bytes}`` or ``{ok:false, error}``."""
    fn = _slug_name(name)
    if fn is None:
        return {"ok": False, "error": "doc name has no usable characters"}
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "doc body is empty"}
    d = context_dir(data_dir)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, fn)
    payload = text if text.endswith("\n") else text + "\n"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(payload)
    return {"ok": True, "name": fn, "path": p,
            "bytes": len(payload.encode("utf-8"))}


def seed_default(data_dir: Optional[str] = None) -> dict:
    """Ensure the operator seed ``coord-context.md`` exists in the store,
    idempotently — writes it from :data:`DEFAULT_COORD_CONTEXT` only if absent, so
    an operator's later edits are never clobbered. Returns
    ``{seeded, name, path}`` (``seeded`` False when it was already present)."""
    d = context_dir(data_dir)
    p = os.path.join(d, SEED_NAME)
    if os.path.isfile(p):
        return {"seeded": False, "name": SEED_NAME, "path": p}
    res = write_doc(SEED_NAME, DEFAULT_COORD_CONTEXT, data_dir)
    return {"seeded": res.get("ok", False), "name": SEED_NAME,
            "path": res.get("path", p)}
