"""Agent Registry v2 data model — pure functions, no I/O.

Decided agent model (loop north star): an AGENT *is* identity only —
``persona`` (fixed) + a ``genericGoal`` (loop-agnostic) + a preferred ``model``.
Everything operational (the role it plays in a given loop, its per-loop goal,
its schedule) comes from the LOOP, not the agent. Any change to identity forks a
new **immutable** version (v1, v2, …); ``head`` is the current version. A loop
step can PIN an exact version (``agentRef``) so a run records which version it
actually executed.

Record shape (``schema`` 2)::

    {
      "id": "critic",
      "kind": "agent",
      "schema": 2,
      "head": 2,                         # current version number
      "defaultRole": "input_provider",   # operational HINT, not part of identity
      "model": "claude-sonnet-5",        # head model (mirror of versions[-1].model)
      "note": "reusable critic",
      "saved": 1789000000.0,             # last-write ts
      "versions": [
        {"version": 1, "persona": "…", "genericGoal": "…", "model": None,
         "note": "init", "source": "init", "createdAt": 1789000000.0},
        {"version": 2, "persona": "…", "genericGoal": "…", "model": "claude-sonnet-5",
         "note": "sharper", "source": "edit", "createdAt": 1789000100.0}
      ],
      "step": {                          # BACK-COMPAT mirror of head as a loop step-def
        "role": "input_provider", "personality": "…", "goal": "…",
        "model": "claude-sonnet-5"
      }
    }

``model`` is ``None`` → "inherit" (use the loop/substrate default). :data:`KNOWN_MODELS`
is advisory (a UI dropdown); an unknown id is stored as-is (forward-compat) so this
layer never blocks a newer model the harness gains later.

This module is deliberately I/O-free: the server owns reading/writing JSON and
injects ``now`` (the runtime forbids ``time`` inside pure code paths that must be
deterministic under replay). Callers pass ``now=time.time()``.
"""

from __future__ import annotations

import difflib
from typing import Any, Callable, Optional

from mcp_loops.schema import WORKER

# ── schema + models ──────────────────────────────────────────────────────────
AGENT_SCHEMA_VERSION = 2

# Advisory list for a UI dropdown — NOT a hard allow-list (see normalize_model).
KNOWN_MODELS: tuple[str, ...] = (
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
    "claude-fable-5",
)
# Identity fields that, when changed, fork a new immutable version.
IDENTITY_FIELDS: tuple[str, ...] = ("persona", "genericGoal", "model")

# sentinel: "argument not supplied" (distinct from an explicit None = inherit)
_UNSET = object()

# When an agent is auto-materialized from a bare id (no step-def yet available),
# we still want its detail view to work. These placeholders make that honest:
# they say "we haven't recorded a real persona yet" rather than inventing one.
AUTO_PERSONA_PLACEHOLDER = "(persona not yet recorded — auto-materialized from a loop)"
AUTO_GENERIC_GOAL_PLACEHOLDER = "(generic goal not yet recorded — auto-materialized from a loop)"


def normalize_model(model: Any) -> Optional[str]:
    """None/"" → None (inherit). A non-empty string is trimmed and returned as-is
    (unknown ids allowed for forward-compat). Anything else raises ValueError."""
    if model is None:
        return None
    if not isinstance(model, str):
        raise ValueError(f"model must be a string or None, got {type(model).__name__}")
    m = model.strip()
    return m or None


# ── construction ───────────────────────────────────────────────────────────────
def _version(n: int, persona: str, generic_goal: str, model: Optional[str],
             note: str, source: str, now: float) -> dict:
    return {
        "version": n,
        "persona": persona,
        "genericGoal": generic_goal,
        "model": normalize_model(model),
        "note": note or "",
        "source": source,
        "createdAt": now,
    }


def new_agent_record(agent_id: str, persona: str, generic_goal: str, *,
                     model: Any = None, default_role: str = WORKER,
                     note: str = "", now: float) -> dict:
    """Build a fresh v2 record at version 1."""
    if not isinstance(persona, str) or not persona.strip():
        raise ValueError("persona (fixed identity) is required and must be non-empty")
    if not isinstance(generic_goal, str) or not generic_goal.strip():
        raise ValueError("genericGoal is required and must be non-empty")
    v1 = _version(1, persona.strip(), generic_goal.strip(), model,
                  note, "init", now)
    rec = {
        "id": agent_id,
        "kind": "agent",
        "schema": AGENT_SCHEMA_VERSION,
        "head": 1,
        "defaultRole": default_role if default_role else WORKER,
        "note": note or "",
        "saved": now,
        "versions": [v1],
    }
    rec["model"] = v1["model"]
    rec["step"] = _step_mirror(rec)
    return rec


def auto_agent_record_from_step(agent_id: str, step: Optional[dict], *,
                                 now: float) -> dict:
    """Build a v2 registry record from a loop step-def (or a bare id) — the
    entry point for AUTO-materializing an agent seen in any loop so its detail
    view always works.

    If ``step`` carries a real ``personality`` + ``goal``, they become v1's
    persona + genericGoal. If the step is missing or empty, the placeholders
    are used — HONEST about what was actually recorded, so the UI can prompt
    for a proper edit later. Source is ``"auto"`` (distinguishable from
    ``"init"`` / ``"migrated"`` / ``"edit"``)."""
    step = step if isinstance(step, dict) else {}
    persona = step.get("personality")
    goal = step.get("goal")
    role = step.get("role") or WORKER
    model = step.get("model")
    if not (isinstance(persona, str) and persona.strip()):
        persona = AUTO_PERSONA_PLACEHOLDER
    if not (isinstance(goal, str) and goal.strip()):
        goal = AUTO_GENERIC_GOAL_PLACEHOLDER
    rec = new_agent_record(
        agent_id, persona, goal, model=model, default_role=role,
        note="", now=now)
    rec["versions"][0]["source"] = "auto"
    rec["favorite"] = False
    return rec


def is_placeholder_identity(rec: dict) -> bool:
    """True iff the record's head persona OR generic goal is still the
    auto-materialize placeholder — i.e. it was seeded from a bare id and hasn't
    been curated yet. Used by the UI to show "needs review" affordance."""
    try:
        hv = head_version(rec)
    except ValueError:
        return True
    return (hv.get("persona") == AUTO_PERSONA_PLACEHOLDER or
            hv.get("genericGoal") == AUTO_GENERIC_GOAL_PLACEHOLDER)


def set_favorite(rec: dict, favorite: bool) -> dict:
    """Toggle the favorite flag on a registry record IN PLACE (idempotent).
    Favorites are a curated shelf; every agent already has a detail view — the
    flag never gates functionality."""
    rec["favorite"] = bool(favorite)
    return rec


def _step_mirror(rec: dict) -> dict:
    """A by-value loop step-def reflecting the HEAD version — kept on the record
    so pre-v2 consumers (and the runner's ``step['personality']`` read path) keep
    working unchanged."""
    hv = head_version(rec)
    step: dict[str, Any] = {
        "role": rec.get("defaultRole", WORKER),
        "personality": hv["persona"],
        "goal": hv["genericGoal"],
    }
    if hv.get("model"):
        step["model"] = hv["model"]
    return step


# ── access ─────────────────────────────────────────────────────────────────────
def head_version(rec: dict) -> dict:
    """The current (head) version dict. Raises if the record has no versions."""
    versions = rec.get("versions") or []
    if not versions:
        raise ValueError(f"agent {rec.get('id')!r} has no versions")
    head = rec.get("head", versions[-1]["version"])
    return agent_version(rec, head) or versions[-1]


def agent_version(rec: dict, version: Optional[int] = None) -> Optional[dict]:
    """Resolve the immutable identity dict at ``version`` (default: head).
    Returns None if that version does not exist."""
    versions = rec.get("versions") or []
    if not versions:
        return None
    if version is None:
        version = rec.get("head", versions[-1]["version"])
    for v in versions:
        if v.get("version") == version:
            return v
    return None


# ── forking a new immutable version ─────────────────────────────────────────────
def _identity_tuple(v: dict) -> tuple:
    return tuple(v.get(f) for f in IDENTITY_FIELDS)


def fork_agent(rec: dict, *, persona: Any = _UNSET, generic_goal: Any = _UNSET,
               model: Any = _UNSET, note: str = "", source: str = "edit",
               now: float) -> tuple[dict, bool]:
    """Apply identity overrides onto the head version and, IF anything actually
    changed, append a new immutable version (bumping ``head`` and the step mirror).

    Returns ``(record, changed)``. When nothing changed, the record is returned
    untouched with ``changed=False`` — identity is content-addressed, so a no-op
    edit never inflates the version timeline.
    """
    hv = head_version(rec)
    new_persona = hv["persona"] if persona is _UNSET else str(persona).strip()
    new_goal = hv["genericGoal"] if generic_goal is _UNSET else str(generic_goal).strip()
    new_model = hv["model"] if model is _UNSET else normalize_model(model)
    if not new_persona:
        raise ValueError("persona cannot be emptied")
    if not new_goal:
        raise ValueError("genericGoal cannot be emptied")

    candidate = {"persona": new_persona, "genericGoal": new_goal, "model": new_model}
    if _identity_tuple(candidate) == _identity_tuple(hv):
        return rec, False

    next_n = max((v["version"] for v in rec["versions"]), default=0) + 1
    rec["versions"].append(
        _version(next_n, new_persona, new_goal, new_model, note, source, now))
    rec["head"] = next_n
    rec["model"] = new_model
    rec["saved"] = now
    if note:
        rec["note"] = note
    rec["step"] = _step_mirror(rec)
    return rec, True


# ── migration (legacy {step} shape → v2) ─────────────────────────────────────────
def migrate_agent_record(rec: dict, *, now: float) -> dict:
    """Upgrade a registry agent record to schema 2. Idempotent: a v2 record is
    returned with its step mirror refreshed; a legacy ``{step:{role,personality,
    goal}}`` record is lifted into a single immutable v1.

    Never mutates the input — returns a new dict (so a migrate-on-read never
    silently rewrites disk unless the caller persists it).
    """
    if not isinstance(rec, dict):
        raise ValueError("agent record must be a dict")

    if rec.get("schema") == AGENT_SCHEMA_VERSION and rec.get("versions"):
        out = dict(rec)
        out["versions"] = [dict(v) for v in rec["versions"]]
        out["step"] = _step_mirror(out)
        out.setdefault("model", head_version(out)["model"])
        return out

    step = rec.get("step") or {}
    if not isinstance(step, dict):
        step = {}
    persona = step.get("personality") or ""
    generic_goal = step.get("goal") or ""
    saved = rec.get("saved", now)
    migrated = new_agent_record(
        rec.get("id", "agent"),
        persona or "(persona not recorded)",
        generic_goal or "(generic goal not recorded)",
        model=step.get("model"),
        default_role=step.get("role", WORKER),
        note=rec.get("note", ""),
        now=saved,
    )
    # keep provenance honest: this v1 came from a pre-v2 save, not a fresh author
    migrated["versions"][0]["source"] = "migrated"
    migrated["saved"] = saved
    migrated["step"] = _step_mirror(migrated)
    return migrated


# ── version-pin: resolve a loop step that references a registry agent ────────────
def resolve_step_ref(step: dict, load_agent: Callable[[str], Optional[dict]], *,
                     now: float) -> dict:
    """If ``step`` carries an ``agentRef`` ({id, version?}), resolve it against the
    registry into a fully by-value step (personality/goal/model filled from the
    pinned immutable version) and stamp ``agentPin`` recording exactly which
    version was frozen in. Steps with no ``agentRef`` are returned unchanged.

    The runner keeps reading ``step['personality']`` / ``step['goal']`` — this is
    the bridge that lets a loop reuse a registry agent AND record its version,
    without the runner learning about the registry at all.
    """
    ref = step.get("agentRef")
    if not ref:
        return step
    if not isinstance(ref, dict) or not ref.get("id"):
        raise ValueError("agentRef must be an object with an `id`")
    agent_id = ref["id"]
    raw = load_agent(agent_id)
    if raw is None:
        raise ValueError(f"agentRef → unknown registry agent {agent_id!r}")
    rec = migrate_agent_record(raw, now=now)
    version = ref.get("version")
    v = agent_version(rec, version)
    if v is None:
        raise ValueError(
            f"agent {agent_id!r} has no version {version!r} "
            f"(head is {rec.get('head')})")

    out = {k: val for k, val in step.items() if k != "agentRef"}
    out.setdefault("role", rec.get("defaultRole", WORKER))
    out["personality"] = v["persona"]
    out["goal"] = v["genericGoal"]
    if v.get("model"):
        out["model"] = v["model"]
    out["agentPin"] = {"id": agent_id, "version": v["version"], "model": v.get("model")}
    return out


# ── read helpers for the (fast-follow) version timeline + diff ───────────────────
def version_timeline(rec: dict) -> list[dict]:
    """Compact per-version entries with the identity fields that changed vs the
    previous version — the data behind an agent page's version timeline."""
    out: list[dict] = []
    prev: Optional[dict] = None
    for v in rec.get("versions") or []:
        changed = [f for f in IDENTITY_FIELDS
                   if prev is None or v.get(f) != prev.get(f)]
        if prev is None:
            changed = ["persona", "genericGoal"]  # v1 is the baseline
        out.append({
            "version": v["version"],
            "source": v.get("source", "edit"),
            "note": v.get("note", ""),
            "createdAt": v.get("createdAt"),
            "model": v.get("model"),
            "changed": changed,
            "isHead": v["version"] == rec.get("head"),
        })
        prev = v
    return out


def diff_versions(rec: dict, a: int, b: int) -> dict:
    """Field-wise identity diff between versions ``a`` and ``b``. Returns
    ``{field: {"from":…, "to":…, "changed":bool}}`` for the identity fields."""
    va = agent_version(rec, a)
    vb = agent_version(rec, b)
    if va is None or vb is None:
        raise ValueError(f"cannot diff — missing version {a if va is None else b}")
    return {
        f: {"from": va.get(f), "to": vb.get(f), "changed": va.get(f) != vb.get(f)}
        for f in IDENTITY_FIELDS
    }


def _unified(a_text: Optional[str], b_text: Optional[str], label: str) -> list[str]:
    """Stdlib line-level unified diff of two text fields (empty list if equal)."""
    a_lines = (a_text or "").splitlines()
    b_lines = (b_text or "").splitlines()
    return list(difflib.unified_diff(
        a_lines, b_lines, fromfile=f"{label}@v?a", tofile=f"{label}@v?b", lineterm=""))


def diff_versions_detailed(rec: dict, a: int, b: int, *,
                           role: Optional[str] = None) -> dict:
    """Structured diff for role/persona/genericGoal/model between two versions,
    plus a stdlib line-level unified diff for the text fields (persona,
    genericGoal). Returns ``{versionA, versionB, fields:{field:{from,to,changed}},
    textDiff:{persona:[…], genericGoal:[…]}}``.

    NOTE role is NOT part of versioned identity in this model — it comes from the
    LOOP (``defaultRole`` is only a hint). So role's ``from``/``to`` are both the
    record's default role and ``changed`` is always False; it is reported for
    spec parity / UI symmetry, not because role forks a version.
    """
    va = agent_version(rec, a)
    vb = agent_version(rec, b)
    if va is None or vb is None:
        raise ValueError(f"cannot diff — missing version {a if va is None else b}")
    role_val = role if role is not None else rec.get("defaultRole", WORKER)
    fields: dict[str, dict] = {
        f: {"from": va.get(f), "to": vb.get(f), "changed": va.get(f) != vb.get(f)}
        for f in IDENTITY_FIELDS
    }
    fields["role"] = {"from": role_val, "to": role_val, "changed": False}
    return {
        "versionA": a,
        "versionB": b,
        "fields": fields,
        "textDiff": {
            "persona": _unified(va.get("persona"), vb.get("persona"), "persona"),
            "genericGoal": _unified(va.get("genericGoal"), vb.get("genericGoal"),
                                    "genericGoal"),
        },
    }
