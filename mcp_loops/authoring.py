"""Loop-authoring aids (Pillar 2 backend): the creation RULES as a linter, and a
SCHEMATIC that lays out every field of a loop config for the detail page.

The decided agent model splits *identity* from *operation*: an agent's persona +
generic goal are loop-agnostic identity; per-loop, product, and task detail live
in the LOOP goal. The RULES below encode that so authoring (by hand, by the
in-dashboard editor, or by the loop-creator agent) stays on-model. They are
*advisory* — findings, not hard failures — because they are heuristic; the
structural invariants (exactly one manager, valid stepOrder, …) stay in
:func:`schema.validate_config`. This module never rejects a config; it explains
how to make it better.

Pure module (no I/O): the server validates + persists; these functions only read
a config dict and return findings / a reshaped view.
"""

from __future__ import annotations

import re
from typing import Any

from mcp_loops.schema import summarize

# ── the encoded creation rules (also surfaced to the UI / loop-creator) ────────
RULES: list[dict] = [
    {"id": "no-todos-in-identity",
     "summary": "Agent persona/goal must not contain todos, checklists, or "
                "step-by-step tasks — those belong in the loop goal."},
    {"id": "generic-goal",
     "summary": "An agent's goal must be loop-agnostic (reference 'the loop "
                "goal' generically), so any loop can reuse the agent."},
    {"id": "product-detail-in-loop-goal",
     "summary": "Product/feature/path specifics belong in the loop goal, not in "
                "an agent's persona or goal."},
]

# checklist / task-list / step-enumeration markers (todos leaking into identity)
_TODO_PATTERNS = [
    re.compile(r"(?im)^\s*[-*]\s*\[[ xX]\]"),        # - [ ] / * [x]
    re.compile(r"(?m)\b(TODO|FIXME|TBD)\b"),          # explicit todo markers
    re.compile(r"(?im)^\s*step\s*\d+\b"),             # "Step 1 ..."
    re.compile(r"(?im)^\s*\d+[.)]\s+\S"),             # "1. do x" / "2) do y"
]
# loop-specific leakage: filesystem paths, source files, urls, repo-ish tokens
_PRODUCT_DETAIL_PATTERNS = [
    re.compile(r"[\w./-]+\.(py|js|ts|md|json|yaml|yml|tsx|go|rs)\b"),  # a filename
    re.compile(r"https?://\S+"),                                      # a URL
    re.compile(r"(?<!\w)/\w[\w/-]{3,}"),                              # an abs-ish path
]
# a goal that never gestures at the loop goal is more likely to be loop-specific
_GENERIC_HINT = re.compile(r"(?i)\bloop(?:'s)?\s+goal\b|\bnorth[- ]star\b|\bthe goal\b")


def _finding(level: str, rule: str, where: str, message: str) -> dict:
    return {"level": level, "rule": rule, "where": where, "message": message}


def _lint_identity(where_prefix: str, personality: str, goal: str) -> list[dict]:
    out: list[dict] = []
    for field, text in (("personality", personality), ("goal", goal)):
        where = f"{where_prefix}.{field}"
        if any(p.search(text or "") for p in _TODO_PATTERNS):
            out.append(_finding(
                "warn", "no-todos-in-identity", where,
                f"{field} reads like a todo/checklist — move task steps to the "
                "loop goal; keep identity durable and loop-agnostic."))
        hits = [m.group(0) for p in _PRODUCT_DETAIL_PATTERNS
                for m in p.finditer(text or "")]
        if hits:
            out.append(_finding(
                "warn", "product-detail-in-loop-goal", where,
                f"{field} names product/path specifics ({', '.join(sorted(set(hits))[:3])}) "
                "— those belong in the loop goal so the agent stays reusable."))
    # a worker/input goal that never references the loop goal may be too specific
    if goal and not _GENERIC_HINT.search(goal) and len(goal) > 120:
        out.append(_finding(
            "info", "generic-goal", f"{where_prefix}.goal",
            "goal is long and never references 'the loop goal' — check it is "
            "loop-agnostic (generic identity), not this loop's task list."))
    return out


def lint_config(cfg: Any, *, _prefix: str = "steps") -> list[dict]:
    """Advisory findings against the authoring RULES. Recurses into sub-loops.
    Returns a list of ``{level, rule, where, message}`` (empty = clean)."""
    if not isinstance(cfg, dict):
        return []
    findings: list[dict] = []
    steps = cfg.get("steps")
    if not isinstance(steps, dict):
        return findings
    for sid, sdef in steps.items():
        if not isinstance(sdef, dict):
            continue
        if sdef.get("type") == "loop" and isinstance(sdef.get("loop"), dict):
            findings.extend(lint_config(sdef["loop"], _prefix=f"{_prefix}.{sid}.loop.steps"))
        elif sdef.get("type", "agent") == "agent":
            findings.extend(_lint_identity(
                f"{_prefix}.{sid}",
                sdef.get("personality", "") or "",
                sdef.get("goal", "") or ""))
    return findings


# ── schematic: every field of the loop JSON, labeled + sectioned for the UI ────
def _field(key: str, label: str, value: Any) -> dict:
    return {"key": key, "label": label, "value": value}


def schematic(cfg: dict) -> dict:
    """A labeled, sectioned view of every field of a (normalized) loop config —
    the data behind the loop detail page's SCHEMATIC view. Pure reshape; assumes
    ``cfg`` came from :func:`schema.validate_config`."""
    caps = cfg.get("contextCaps", {})
    budget = cfg.get("budget", {})
    hil = cfg.get("humanInLoop", {})
    sections = [
        {"title": "Identity", "fields": [
            _field("name", "Name", cfg.get("name")),
            _field("goal", "North-star goal", cfg.get("goal")),
            _field("substrate", "Substrate", cfg.get("substrate")),
        ]},
        {"title": "Context caps", "fields": [
            _field("contextCaps.default", "Default cap", caps.get("default")),
            _field("contextCaps.manager", "Manager cap", caps.get("manager")),
        ]},
        {"title": "Budget", "fields": [
            _field("budget.turnLimit", "Turn limit", budget.get("turnLimit")),
            _field("budget.managerFinalizeSteps", "Manager finalize steps",
                   budget.get("managerFinalizeSteps")),
            _field("budget.minorOnlyExtraSteps", "Minor-only extra steps",
                   budget.get("minorOnlyExtraSteps")),
            _field("budget.minorSkipThreshold", "Minor skip threshold",
                   budget.get("minorSkipThreshold")),
        ]},
        {"title": "Human in loop", "fields": [
            _field("humanInLoop.owner", "Owner", hil.get("owner")),
            _field("humanInLoop.via", "Via", hil.get("via")),
        ]},
    ]

    steps_out = []
    for sid, sdef in (cfg.get("steps") or {}).items():
        if sdef.get("type") == "loop":
            steps_out.append({"id": sid, "type": "loop",
                              "loop": schematic(sdef.get("loop") or {})})
            continue
        entry = {
            "id": sid, "type": "agent",
            "role": sdef.get("role"),
            "model": sdef.get("model"),          # None → inherit host default
            "maxRepeatTurns": sdef.get("maxRepeatTurns"),
            "maxTurnMinutes": sdef.get("maxTurnMinutes"),
            "personality": sdef.get("personality", ""),
            "goal": sdef.get("goal", ""),
        }
        if sdef.get("agentPin"):
            entry["agentPin"] = sdef["agentPin"]   # which registry version was frozen
        if sdef.get("tools"):
            entry["tools"] = sdef["tools"]
        if sdef.get("runtime"):
            entry["runtime"] = sdef["runtime"]     # CAP-1: which CLI this step runs on
        if sdef.get("host_class"):
            entry["host_class"] = sdef["host_class"]  # A9: which host/venue class
        steps_out.append(entry)

    # stepOrder with parallel groups marked
    order_out = []
    for ref in cfg.get("stepOrder", []):
        if isinstance(ref, list):
            order_out.append({"parallel": ref})
        else:
            order_out.append({"step": ref})

    return {
        "name": cfg.get("name"),
        "sections": sections,
        "steps": steps_out,
        "stepOrder": order_out,
        "derived": summarize(cfg) if cfg.get("steps") else {},
    }
