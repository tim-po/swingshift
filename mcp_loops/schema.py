"""Loop config schema: validation, default-normalization, and derived summary.

A loop config is a plain dict (round-trips to the JSON file the owner reviews).
:func:`validate_config` checks it, fills role-based defaults, and returns a
*normalized* copy the runner can execute without re-deriving anything.

Shape (authoring form — defaults may be omitted)::

    {
      "name": "dash-polish",                 # referenceable handle (autogen-ish name)
      "goal": "North-star the manager holds in full ...",
      "substrate": "headless",               # headless (now) | autogen (later)
      "contextCaps": {"default": 200000, "manager": 400000},
      "budget": {                            # everything is a collective turn budget
        "turnLimit": 60,                     #   main-phase turns shared by all agents
        "managerFinalizeSteps": 4,           #   manager turns granted in wind-down
        "minorOnlyExtraSteps": 2,            #   extra runs a `minor_only` agent gets
        "minorSkipThreshold": 0.30           #   <this frac of turnLimit left → drop minors
      },
      "humanInLoop": {"owner": "tim", "via": "manager"},
      "steps": {                             # id -> step definition (agent OR loop)
        "boss":      {"type":"agent","role":"manager","personality":"...","goal":"..."},
        "builder":   {"type":"agent","role":"worker","personality":"...","goal":"..."},
        "ui_critic": {"type":"agent","role":"input_provider","personality":"...","goal":"..."},
        "subaudit":  {"type":"loop","loop": { ...a full nested loop config... }}
      },
      "stepOrder": ["boss","builder","ui_critic"]   # one round; ids may repeat
    }

Budget model (see runner): the main phase runs rounds (one full ``stepOrder``
pass) until ``turnLimit`` collective turns are spent — a round in flight is
allowed to finish. Then a guaranteed **wind-down** tail runs beyond the limit:
``2 * (#workers)`` worker-only turns plus ``managerFinalizeSteps`` manager turns.
Per-agent ``maxRepeatTurns`` lets an agent repeat its own turn back-to-back
(worker default 3, others 1) before the loop advances to the next id.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from mcp_loops.devices import DEFAULT_OWNER
from mcp_loops.runtimes import SUPPORTED_RUNTIMES


class LoopConfigError(ValueError):
    """Raised by :func:`validate_config` when ``strict=True`` and config is invalid."""


# A host-class / venue label (A9) tags which machine class a step runs on. Keep
# it to a safe, path/name-fragment-free charset (same shape as a slug/runtime).
_HOST_CLASS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A tenant owner id (the P3 enrolled-owner identity: "local", "alice",
# "alice@example.com"). No whitespace / path separators; bounded.
_OWNER_RE = re.compile(r"^[^\s/\\]{1,128}$")


# ── roles ──────────────────────────────────────────────────────────────────
MANAGER = "manager"
WORKER = "worker"
INPUT_PROVIDER = "input_provider"
ROLES = frozenset({MANAGER, WORKER, INPUT_PROVIDER})

# ── the status vocab every agent turn ends with (the engine's spine) ────────
# The runner reads these to decide repeat / retirement / phase transitions.
STATUS_VOCAB: dict[str, tuple[str, ...]] = {
    WORKER: ("completed", "work_remaining"),
    INPUT_PROVIDER: ("satisfied", "minor_only", "needs_work"),
    MANAGER: ("continue", "ask_owner", "wind_down", "complete"),
}
# Which statuses mean "run this same agent again" (subject to maxRepeatTurns):
REPEAT_STATUS: dict[str, frozenset[str]] = {
    WORKER: frozenset({"work_remaining"}),
    INPUT_PROVIDER: frozenset(),          # inputs don't self-repeat by default
    MANAGER: frozenset(),
}

# ── defaults ────────────────────────────────────────────────────────────────
DEFAULT_SUBSTRATE = "headless"
SUBSTRATES = frozenset({"headless", "autogen"})

DEFAULT_CONTEXT_CAP = 200_000
DEFAULT_MANAGER_CAP = 400_000

DEFAULT_TURN_LIMIT = 40
DEFAULT_MANAGER_FINALIZE_STEPS = 4          # advised in design; = 2 + one pass either side
DEFAULT_MINOR_ONLY_EXTRA = 2
DEFAULT_MINOR_SKIP_THRESHOLD = 0.30

REPEAT_DEFAULTS = {WORKER: 3, MANAGER: 1, INPUT_PROVIDER: 1}

# Estimated MAX turn duration (minutes) per role — the substrate waits this long
# for an agent's end-of-turn report before treating the turn as timed out. Set
# generously for heavy work (build + CI); the guardian's service agent can EXTEND
# it mid-loop when an agent is slow-but-productive. Authors override per agent.
TURN_MINUTES_DEFAULTS = {WORKER: 45, MANAGER: 20, INPUT_PROVIDER: 30}

MAX_LOOP_DEPTH = 5                          # guard against runaway inline nesting


# ── public API ───────────────────────────────────────────────────────────────
def validate_config(raw: Any, *, strict: bool = False, _depth: int = 0) -> dict:
    """Validate a loop config and return ``{ok, errors, warnings, config}``.

    ``config`` is a normalized deep copy with every default filled in (role-based
    ``maxRepeatTurns``, context caps, budget knobs) so the runner needs no
    further derivation. Nested ``loop`` steps are validated recursively; their
    errors bubble up path-qualified (e.g. ``steps.subaudit.loop: ...``).

    With ``strict=True`` a first error raises :class:`LoopConfigError` instead of
    being collected.
    """
    errors: list[str] = []
    warnings: list[str] = []

    def fail(msg: str) -> None:
        if strict:
            raise LoopConfigError(msg)
        errors.append(msg)

    if not isinstance(raw, dict):
        fail(f"config must be an object, got {type(raw).__name__}")
        return _result(False, errors, warnings, {})

    cfg: dict[str, Any] = {}

    # -- name --
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        fail("`name` is required and must be a non-empty string")
        name = name if isinstance(name, str) else ""
    cfg["name"] = name.strip()

    # -- goal (the north star) --
    goal = raw.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        fail("`goal` is required (the manager's north star) and must be non-empty")
        goal = goal if isinstance(goal, str) else ""
    cfg["goal"] = goal.strip()

    # -- substrate --
    substrate = raw.get("substrate", DEFAULT_SUBSTRATE)
    if substrate not in SUBSTRATES:
        fail(f"`substrate` must be one of {sorted(SUBSTRATES)}, got {substrate!r}")
        substrate = DEFAULT_SUBSTRATE
    cfg["substrate"] = substrate

    # -- context caps --
    caps_in = raw.get("contextCaps") or {}
    if not isinstance(caps_in, dict):
        fail("`contextCaps` must be an object")
        caps_in = {}
    cfg["contextCaps"] = {
        "default": _pos_int(caps_in.get("default"), DEFAULT_CONTEXT_CAP, "contextCaps.default", fail),
        "manager": _pos_int(caps_in.get("manager"), DEFAULT_MANAGER_CAP, "contextCaps.manager", fail),
    }

    # -- budget --
    budget_in = raw.get("budget") or {}
    if not isinstance(budget_in, dict):
        fail("`budget` must be an object")
        budget_in = {}
    turn_limit = _pos_int(budget_in.get("turnLimit"), DEFAULT_TURN_LIMIT, "budget.turnLimit", fail)
    threshold = budget_in.get("minorSkipThreshold", DEFAULT_MINOR_SKIP_THRESHOLD)
    if not isinstance(threshold, (int, float)) or not (0 <= threshold <= 1):
        fail("`budget.minorSkipThreshold` must be a number in [0, 1]")
        threshold = DEFAULT_MINOR_SKIP_THRESHOLD
    cfg["budget"] = {
        "turnLimit": turn_limit,
        "managerFinalizeSteps": _pos_int(
            budget_in.get("managerFinalizeSteps"), DEFAULT_MANAGER_FINALIZE_STEPS,
            "budget.managerFinalizeSteps", fail),
        "minorOnlyExtraSteps": _pos_int(
            budget_in.get("minorOnlyExtraSteps"), DEFAULT_MINOR_ONLY_EXTRA,
            "budget.minorOnlyExtraSteps", fail, allow_zero=True),
        "minorSkipThreshold": float(threshold),
    }

    # -- humanInLoop --
    hil = raw.get("humanInLoop") or {}
    if not isinstance(hil, dict):
        fail("`humanInLoop` must be an object")
        hil = {}
    cfg["humanInLoop"] = {
        "owner": hil.get("owner") or "owner",
        "via": hil.get("via") or MANAGER,
    }

    # -- team bindings (Phase D) — OPTIONAL Project + Origin a loop targets --
    # Advisory-with-warning, NEVER hard-required: all 38 existing configs lack
    # these and must keep validating during the live pilot. When present they are
    # RETAINED in the normalized config so the creator's New-Team write journey
    # binds for real.
    #   projectId — canonical id of the connected git repo a loop targets = BARE
    #               repo-name slug (owner decision). `productId` is the BACK-COMPAT
    #               ALIAS from before the noun rename (Round A: Product → Project);
    #               it is accepted on read and normalizes to `projectId`, and BOTH
    #               keys are written into the normalized config so the 40+ existing
    #               `productId` configs AND every old reader (agent_core §6.4,
    #               products_gather) keep resolving with zero change.
    #   origin    — target Origin/host_class; accepts either spelling (engineer_a
    #               writes "origin"), canonicalized here to `origin`.
    pid = raw.get("projectId")
    # which spelling the caller used — so the error message names THEIR field.
    pid_field = "projectId"
    if pid is None:
        pid = raw.get("productId")
        pid_field = "productId"
    if pid is not None:
        if not isinstance(pid, str) or not pid.strip():
            fail(f"`{pid_field}` must be a non-empty string when set")
        elif not _HOST_CLASS_RE.match(pid.strip()):
            fail(f"`{pid_field}` {pid!r} must be a slug (letters, digits, . _ -; "
                 "no path separators)")
        else:
            # canonical + alias kept in lock-step for zero-breakage.
            cfg["projectId"] = pid.strip()
            cfg["productId"] = pid.strip()
    origin = raw.get("origin")
    if origin is None:
        origin = raw.get("host_class")
    if origin is not None:
        if not isinstance(origin, str) or not origin.strip():
            fail("`origin` must be a non-empty string when set")
        elif not _HOST_CLASS_RE.match(origin.strip()):
            fail(f"`origin` {origin!r} must be letters, digits, . _ - "
                 "(no path separators)")
        else:
            cfg["origin"] = origin.strip()
    #   slug — the worker-daemon PROJECT slug this loop runs its agents under (its
    #          repo_path in projects.toml IS the agents' working dir). OPTIONAL and
    #          strictly additive: when present it PINS the loop to that slug, which
    #          the dispatch treats as an explicit slug so it overrides the
    #          service-wide LOOPS_SLUG default; when absent the loop resolves EXACTLY
    #          as before (LOOPS_SLUG > runner default > projectId). Lets one loop
    #          target a dedicated worktree slug without disturbing every other loop.
    slug = raw.get("slug")
    if slug is not None:
        if not isinstance(slug, str) or not slug.strip():
            fail("`slug` must be a non-empty string when set")
        elif not _HOST_CLASS_RE.match(slug.strip()):
            fail(f"`slug` {slug!r} must be a slug (letters, digits, . _ -; "
                 "no path separators)")
        else:
            cfg["slug"] = slug.strip()
    #   owner — the TENANT owner (P3 enrolled-owner id) this loop belongs to. NOT
    #          ``humanInLoop.owner`` (that is the HIL display name). OPTIONAL and
    #          strictly additive: kept only when present, so an owner-less config
    #          normalizes byte-identically and resolves to DEFAULT_OWNER at read
    #          time (:func:`resolve_owner`) — no migration.
    own = raw.get("owner")
    if own is not None:
        if not isinstance(own, str) or not _OWNER_RE.match(own.strip()):
            fail("`owner` must be a non-empty owner id (no whitespace or path "
                 "separators, max 128 chars) when set")
        else:
            cfg["owner"] = own.strip()
    # Only nudge at the top level — nested sub-loops don't carry team bindings.
    if _depth == 0 and "projectId" not in cfg:
        warnings.append(
            "loop is `unlinked` — no projectId; it shows under Unattributed (a "
            "goal-path/git guess is only a suggestion). Bind a Project to make the "
            "team target real.")

    # -- setup (optional one-time, idempotent pre-run scaffold) --
    # A loop MAY declare a SETUP the engine runs on the EXECUTING origin BEFORE the
    # first turn, so a flagship example is hands-free on a fresh clone (no manual
    # repo prep — A1). Shape:
    #   {"ensure": "<install-relative dir>",   # skip the script if it already exists
    #    "script": "<install-relative script>"} # run to create it (idempotent)
    # BOTH paths MUST be install-relative — no absolute path, no `..` component —
    # so a saved config can never point the engine at an arbitrary host script or
    # scaffold outside the install. Enforced here; the runtime (server.py
    # _run_loop_setup) re-checks the resolved path stays under the install root.
    setup_in = raw.get("setup")
    if setup_in is not None:
        if not isinstance(setup_in, dict):
            fail("`setup` must be an object when set")
        else:
            norm_setup: dict[str, Any] = {}
            for key in ("script", "ensure"):
                val = setup_in.get(key)
                if val is None:
                    continue
                if not isinstance(val, str) or not val.strip():
                    fail(f"`setup.{key}` must be a non-empty string when set")
                    continue
                v = val.strip()
                parts = v.replace("\\", "/").split("/")
                if v.startswith("/") or ":" in parts[0] or ".." in parts:
                    fail(f"`setup.{key}` must be an install-relative path "
                         "(no absolute path, no `..`)")
                    continue
                norm_setup[key] = v
            if norm_setup:
                cfg["setup"] = norm_setup

    # -- briefing model (how direction reaches agents) --
    # baseline: every agent's prompt carries the full loop goal (legacy).
    # briefing: the manager writes a fresh per-round briefing; workers/inputs get
    #           that + a generic role frame instead of the raw goal.
    # board:    the manager maintains a persistent board.md (per-agent directives +
    #           running record); workers/inputs act off the board, not the raw goal.
    bmode = raw.get("briefingMode", "baseline")
    if bmode not in ("baseline", "briefing", "board"):
        fail(f"`briefingMode` must be baseline|briefing|board, got {bmode!r}")
        bmode = "baseline"
    cfg["briefingMode"] = bmode
    # briefingStart (E1) — WHEN/HOW the loop-start briefing happens (a SEPARATE
    # axis from briefingMode, which is about the per-round prompt channel):
    #   auto        — the substrate auto-briefs the manager (canned owner↔manager
    #                 handshake); the owner does not actually participate. DEFAULT
    #                 = today's behavior, so existing configs are unchanged.
    #   interactive — a REAL owner↔manager exchange BEFORE the main phase: the
    #                 manager may ask the owner clarifying questions (ask_owner)
    #                 and the owner answers (loop_reply) before the first worker
    #                 turn. Off the turn budget.
    #   none        — SKIP briefing entirely; straight into the main phase.
    bstart = raw.get("briefingStart", "auto")
    if bstart not in ("interactive", "auto", "none"):
        fail(f"`briefingStart` must be interactive|auto|none, got {bstart!r}")
        bstart = "auto"
    cfg["briefingStart"] = bstart
    # inputMode — baseline: input providers judge from reasoning. evidence: they are
    # directed to actively TEST the product + RESEARCH their domain and cite evidence.
    # DEFAULT = evidence: the 3-way briefing experiment (2026-09) showed the evidence
    # variant (evidence critics + baseline briefing) beat board/briefing decisively —
    # it shipped the only working v1.1 build. Evidence critics are the gold standard.
    imode = raw.get("inputMode", "evidence")
    if imode not in ("baseline", "evidence"):
        fail(f"`inputMode` must be baseline|evidence, got {imode!r}")
        imode = "evidence"
    cfg["inputMode"] = imode

    # -- isolation (loop cage, mcp_loops.sandbox) — OPTIONAL, additive --
    # Retained only when present so every existing config normalizes unchanged;
    # an absent block means the sandbox defaults (see sandbox.DEFAULTS).
    if "isolation" in raw:
        from mcp_loops import sandbox as _sandbox
        iso, iso_errs = _sandbox.validate_isolation(raw.get("isolation"))
        for e in iso_errs:
            fail(e)
        cfg["isolation"] = iso

    # -- steps --
    steps_in = raw.get("steps")
    cfg["steps"] = {}
    manager_ids: list[str] = []
    worker_ids: list[str] = []
    if not isinstance(steps_in, dict) or not steps_in:
        fail("`steps` is required and must be a non-empty object of id -> step")
        steps_in = {}
    for sid, sdef in steps_in.items():
        norm = _validate_step(sid, sdef, fail, warnings, _depth)
        cfg["steps"][sid] = norm
        if norm.get("type") == "agent":
            if norm.get("role") == MANAGER:
                manager_ids.append(sid)
            elif norm.get("role") == WORKER:
                worker_ids.append(sid)

    # exactly one manager (across agent steps; nested loops carry their own)
    if len(manager_ids) == 0:
        fail("a loop must have exactly one `manager` agent; found none")
    elif len(manager_ids) > 1:
        fail(f"a loop must have exactly one `manager` agent; found {len(manager_ids)}: {manager_ids}")
    if not worker_ids:
        warnings.append("no `worker` agents — wind-down grants 0 worker turns")

    # -- stepOrder -- (an entry is a step id, OR a list of ids = a PARALLEL group
    # that MAY run concurrently; the manager decides per round via a SERIAL= note)
    order = raw.get("stepOrder")
    if not isinstance(order, list) or not order:
        fail("`stepOrder` is required and must be a non-empty list of step ids / groups")
        order = []
    norm_order: list = []
    flat_ids: list[str] = []
    for i, ref in enumerate(order):
        if isinstance(ref, str):
            if ref not in cfg["steps"]:
                fail(f"stepOrder[{i}] references unknown step id {ref!r}")
            norm_order.append(ref)
            flat_ids.append(ref)
        elif isinstance(ref, list):
            if not ref:
                fail(f"stepOrder[{i}] is an empty parallel group")
            for r2 in ref:
                if not isinstance(r2, str) or r2 not in cfg["steps"]:
                    fail(f"stepOrder[{i}] parallel group has unknown/invalid id {r2!r}")
            norm_order.append([r for r in ref])
            flat_ids.extend(x for x in ref if isinstance(x, str))
        else:
            fail(f"stepOrder[{i}] must be a step id (string) or a parallel group (list)")
    cfg["stepOrder"] = norm_order

    # manager should actually be scheduled somewhere (north star must run)
    if manager_ids and manager_ids[0] not in flat_ids:
        warnings.append(
            f"manager {manager_ids[0]!r} is not in stepOrder — it will only run in "
            "briefing + wind-down, never mid-round")

    ok = not errors
    return _result(ok, errors, warnings, cfg)


def summarize(cfg: dict) -> dict:
    """Derive the numbers the renderer/runner display from a *normalized* config.

    Assumes ``cfg`` came from :func:`validate_config` (defaults present).
    """
    steps = cfg.get("steps", {})
    agents = [(sid, s) for sid, s in steps.items() if s.get("type") == "agent"]
    workers = [sid for sid, s in agents if s.get("role") == WORKER]
    inputs = [sid for sid, s in agents if s.get("role") == INPUT_PROVIDER]
    managers = [sid for sid, s in agents if s.get("role") == MANAGER]
    subloops = [sid for sid, s in steps.items() if s.get("type") == "loop"]

    n_workers = len(workers)
    finalize = cfg.get("budget", {}).get("managerFinalizeSteps", DEFAULT_MANAGER_FINALIZE_STEPS)
    wind_worker_turns = 2 * n_workers
    return {
        "manager": managers[0] if managers else None,
        "workers": workers,
        "inputs": inputs,
        "subloops": subloops,
        "n_workers": n_workers,
        "n_inputs": len(inputs),
        "turnLimit": cfg.get("budget", {}).get("turnLimit"),
        "windDownWorkerTurns": wind_worker_turns,
        "windDownManagerTurns": finalize,
        "windDownTotal": wind_worker_turns + finalize,
        "rounds_per_pass": len(cfg.get("stepOrder", [])),
    }


def count_agents(cfg: Any) -> int:
    """Count the top-level AGENT steps in a config (nested ``loop`` steps carry
    their own agents and are NOT counted). Tolerant of a raw, un-normalized
    config so callers can pass whatever ``config.json`` holds. Never raises."""
    steps = (cfg or {}).get("steps") if isinstance(cfg, dict) else None
    if not isinstance(steps, dict):
        return 0
    return sum(1 for s in steps.values()
               if isinstance(s, dict) and s.get("type", "agent") == "agent")


def is_single_agent(cfg: Any) -> bool:
    """True iff this loop is a **loop-of-1** — exactly one top-level agent step.
    The smallest unit of the product; the ``single-agent`` filter on the Loops
    list keys off this (redesign §Q1: one primitive, no resurrected "Run")."""
    return count_agents(cfg) == 1


def resolve_owner(rec: Any) -> str:
    """The tenant owner of a loop config / run record / device record. ONE rule
    product-wide — identical to the P3 ``enrollment.device_owner``: a record with
    no owner belongs to the single local default owner (``DEFAULT_OWNER``), never
    to 'anyone'. Read-time default only; nothing is rewritten."""
    if not isinstance(rec, dict):
        return DEFAULT_OWNER
    return str(rec.get("owner") or DEFAULT_OWNER)


def owner_scope(rows: list, owner_filter: Optional[str] = None) -> list:
    """Apply the owner seam to list rows that each carry a resolved ``owner``.

    * ``owner_filter`` set → keep only that owner's rows (``owner`` kept on each).
    * unset, and every row resolves to ``DEFAULT_OWNER`` → the ``owner`` key is
      DROPPED so a single-owner box is byte-identical to the pre-owner payload.
    * unset, any non-default owner present → rows keep ``owner`` so a surface can
      show/filter it.
    Mutates and returns ``rows``' dicts in a new list."""
    if owner_filter:
        return [r for r in rows if r.get("owner") == owner_filter]
    if all(r.get("owner") == DEFAULT_OWNER for r in rows):
        for r in rows:
            r.pop("owner", None)
    return list(rows)


def owner_refusal(name: str, rec: Any, owner: Optional[str]) -> Optional[dict]:
    """Slice 3 read-by-name guard. ``owner`` unset → None (unchanged read). A
    record that doesn't exist (``rec`` None) → None too, so the caller's own
    not-found path answers. Otherwise, when ``rec`` resolves to a DIFFERENT owner,
    return the honest refusal ``{error, refused:"cross_owner", name, owner}`` —
    it names only the caller's owner, never the record's owner or any data."""
    owner = (owner or "").strip()
    if not owner or not isinstance(rec, dict) or resolve_owner(rec) == owner:
        return None
    return {"error": f"loop {name!r} is not readable by owner {owner!r} "
                     f"(cross-owner read refused)",
            "refused": "cross_owner", "name": name, "owner": owner}


def project_id(cfg: Any) -> Optional[str]:
    """The EXPLICIT project a loop targets — its ``projectId`` (``productId`` is
    the back-compat alias from the Product → Project rename), or ``None`` when the
    loop carries no explicit binding.

    Attribution is set-at-create and read straight off the config — NEVER derived
    from a git remote or a folder name here. A ``None`` return is honest: the
    surface renders it as **"Unattributed"** rather than inventing a bucket."""
    if not isinstance(cfg, dict):
        return None
    for key in ("projectId", "productId"):
        val = cfg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def resolve_project(cfg: Any, *, loop_name: Optional[str] = None) -> Optional[str]:
    """The AUTHORITATIVE project a loop belongs to — the value the Loops switcher
    scopes by — or ``None`` for an honest **"Unattributed"**.

    EXPLICIT ONLY (owner decision, loopyard-bug-1790177434): the loop's
    ``projectId``/``productId`` (:func:`project_id`) and nothing else. Inference
    (the goal's target dir, a git remote) is NEVER asserted as a binding — it is
    demoted to :func:`suggest_project`, which the surface offers as "suggested:
    <p> — accept?". ``loop_name`` is accepted for call-site compatibility. Pure,
    read-time, never raises."""
    return project_id(cfg)


def suggest_project(cfg: Any, *, loop_name: Optional[str] = None) -> Optional[str]:
    """The SUGGESTED project for an UNBOUND loop (``None`` when the loop is
    explicitly bound or nothing is inferable): the single unambiguous target dir
    its goal names, else its ``gitRemote``. Never an attribution — accepting it
    writes a real ``projectId`` via ``loop_save``. Pure, never raises."""
    if not isinstance(cfg, dict) or project_id(cfg):
        return None
    if loop_name is None:
        nm = cfg.get("name")
        loop_name = nm if isinstance(nm, str) else None
    # Deferred import: schema is a low-level module imported everywhere; keep its
    # import graph clean and pull the attribution helper in only when needed.
    from mcp_loops import products_gather
    pid, _signal = products_gather.suggest_for_loop({"name": loop_name, "config": cfg})
    return pid


# ── internals ─────────────────────────────────────────────────────────────────
def _validate_step(sid: str, sdef: Any, fail, warnings: list[str], depth: int) -> dict:
    if not isinstance(sdef, dict):
        fail(f"steps.{sid} must be an object")
        return {"type": "agent", "role": WORKER, "_invalid": True}

    stype = sdef.get("type", "agent")
    if stype not in ("agent", "loop"):
        fail(f"steps.{sid}.type must be 'agent' or 'loop', got {stype!r}")
        stype = "agent"

    if stype == "loop":
        if "ref" in sdef:
            fail(f"steps.{sid}: sub-loops inline a full `loop` config (no `ref` support)")
        inner = sdef.get("loop")
        if depth + 1 >= MAX_LOOP_DEPTH:
            fail(f"steps.{sid}: sub-loop nesting exceeds MAX_LOOP_DEPTH={MAX_LOOP_DEPTH}")
            return {"type": "loop", "loop": {}}
        sub = validate_config(inner, _depth=depth + 1)
        for e in sub["errors"]:
            fail(f"steps.{sid}.loop: {e}")
        for w in sub["warnings"]:
            warnings.append(f"steps.{sid}.loop: {w}")
        return {"type": "loop", "loop": sub["config"]}

    # agent step
    role = sdef.get("role")
    if role not in ROLES:
        fail(f"steps.{sid}.role must be one of {sorted(ROLES)}, got {role!r}")
        role = WORKER
    personality = sdef.get("personality")
    if not isinstance(personality, str) or not personality.strip():
        fail(f"steps.{sid}.personality is required (prompt part 1) and must be non-empty")
        personality = personality if isinstance(personality, str) else ""
    goal = sdef.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        fail(f"steps.{sid}.goal is required (prompt part 2) and must be non-empty")
        goal = goal if isinstance(goal, str) else ""

    default_repeat = REPEAT_DEFAULTS.get(role, 1)
    repeat = sdef.get("maxRepeatTurns", default_repeat)
    if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1:
        fail(f"steps.{sid}.maxRepeatTurns must be an integer >= 1")
        repeat = default_repeat

    default_minutes = TURN_MINUTES_DEFAULTS.get(role, 30)
    minutes = sdef.get("maxTurnMinutes", default_minutes)
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
        fail(f"steps.{sid}.maxTurnMinutes must be an integer >= 1 (minutes)")
        minutes = default_minutes

    norm = {
        "type": "agent",
        "role": role,
        "personality": personality.strip(),
        "goal": goal.strip(),
        "maxRepeatTurns": repeat,
        "maxTurnMinutes": minutes,
    }
    # optional free-form capability hints for input providers / workers
    if isinstance(sdef.get("tools"), list):
        norm["tools"] = list(sdef["tools"])
    # optional per-agent MODEL selection (Agent Registry v2). Kept by-value on
    # the step; not yet passed to the substrate (model-passthrough is a separate
    # increment) — recording it here is the foundation for that.
    model = sdef.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            fail(f"steps.{sid}.model must be a non-empty string when set")
        else:
            norm["model"] = model.strip()
    # optional per-step RUNTIME (CAP-1 multi-CLI): which coding CLI this step's
    # agent runs on — "claude" (default) or "codex". Recorded by-value on the step
    # and applied at spawn under the same passthrough gate as `model` (headless
    # _spawn). Unset ⇒ claude, exactly as before.
    runtime = sdef.get("runtime")
    if runtime is not None:
        if not isinstance(runtime, str) or runtime.strip().lower() not in SUPPORTED_RUNTIMES:
            fail(f"steps.{sid}.runtime must be one of {list(SUPPORTED_RUNTIMES)} when set")
        else:
            norm["runtime"] = runtime.strip().lower()
    # optional per-step HOST_CLASS / venue (A9): a label for WHICH host/venue class
    # this step's agent runs on (e.g. "mac-local", "agent-vps") when a team spans
    # machines. ``venue`` is accepted as an alias. Recorded by-value and ADVISORY
    # — the scheduler doesn't route on it yet — so a profile's host_classes are
    # preserved on the step instead of silently dropped (see creator A8/A9).
    host_class = sdef.get("host_class")
    if host_class is None:
        host_class = sdef.get("venue")
    if host_class is not None:
        if not isinstance(host_class, str) or not host_class.strip():
            fail(f"steps.{sid}.host_class must be a non-empty string when set")
        elif not _HOST_CLASS_RE.match(host_class.strip()):
            fail(f"steps.{sid}.host_class {host_class!r} must be letters, digits, "
                 "and . _ - only")
        else:
            norm["host_class"] = host_class.strip()
    # optional version PIN: which immutable registry agent version this step was
    # frozen from (set by agents.resolve_step_ref). Opaque provenance — preserved
    # through normalization so a run records exactly which version it executed.
    pin = sdef.get("agentPin")
    if isinstance(pin, dict) and pin.get("id"):
        norm["agentPin"] = {"id": pin.get("id"), "version": pin.get("version"),
                            "model": pin.get("model")}
    return norm


def _pos_int(val, default, path, fail, *, allow_zero: bool = False) -> int:
    if val is None:
        return default
    if isinstance(val, bool) or not isinstance(val, int):
        fail(f"`{path}` must be an integer")
        return default
    if val < 0 or (val == 0 and not allow_zero):
        fail(f"`{path}` must be a {'non-negative' if allow_zero else 'positive'} integer")
        return default
    return val


def _result(ok: bool, errors: list[str], warnings: list[str], cfg: dict) -> dict:
    return {"ok": ok, "errors": errors, "warnings": warnings, "config": cfg}
