"""Loopyard onboarding (BRIEF §4) — the first-run guide a NEW user runs first.

The loop-creator (``creator.py``) turns a description into a valid config; this
module is the layer ABOVE it — the friendly guide that GREETS a brand-new user,
EXPLAINS what a Loopyard team is, and walks them through describing what they
want done, reviewing the suggested team, starting it, and rating the result.

It is also the ONE source of truth for the getting-started walkthrough every
surface shows: the web app's Home checklist (``GET /api/loops/onboarding`` →
``loop_onboarding_progress``), the desktop guide panel (``yard onboard --json``)
and the agent prompt all render :data:`STEPS` / :data:`EXPLORE` /
:data:`PLACES` from here — no surface keeps its own copy.

Two shapes, one story:
  * A portable AGENT IDENTITY (persona + generic goal) — like ``loop_creator`` —
    a user can run on their connected CLI to be walked through it conversationally
    (:func:`onboarding_agent_record`, :func:`build_onboarding_prompt`).
  * A deterministic DRIVER (:func:`onboard_first_team`) that does the mechanical
    end-to-end — suggest a team from the goal, then hand back a validated config
    ready to save + start — so the "run your first team" path works hands-free
    (the New loop page and ``yard onboard`` both lean on it).

Mostly pure: the server wires the save/start tools + registry persistence. The
only I/O is the read-only progress helpers (:func:`read_loop_facts`,
:func:`gather_facts`), which look at the local data dir. The identity passes
``authoring.RULES``.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from mcp_loops import authoring, creator, envelope, schema

# ── the guide's identity (loop-agnostic; passes authoring.RULES) ───────────────
ONBOARDING_PERSONA = (
    "A warm, encouraging onboarding guide for someone opening Loopyard for the "
    "very first time. You explain, in plain language, what a self-aligning team "
    "of agents is and why it works, then help the person stand up their own first "
    "team and watch it run. You show rather than lecture and you keep the first "
    "few minutes effortless. You know the Loopyard model cold: a team is one "
    "manager plus workers (and an optional reviewer) that plan, hand off, and "
    "review against one shared goal until the work is done."
)
ONBOARDING_GENERIC_GOAL = (
    "Welcome a new user and explain what a self-aligning team is in plain terms, "
    "then guide them from describing the goal, through shaping the suggested "
    "team, to running it — so they finish with a real team of their own design "
    "already working. Ask only what you need and point them to where the run "
    "unfolds."
)

# What a Loopyard team IS — the one paragraph the guide leads with. Kept as a
# named constant so the CLI, the web app, the desktop guide and the agent prompt
# all say the same thing. Plain words (frontend/DESIGN.md §2).
WELCOME = (
    "Welcome to Loopyard. You describe what you want done in plain words, and "
    "Loopyard puts together a small team of AI agents for it: a manager that "
    "keeps the goal, a few workers that do the work, and often a reviewer that "
    "checks it. They plan, hand off and check each other, turn after turn, until "
    "the job is done — on a machine you own, with your own keys. You watch, "
    "answer if they ask, and rate the result."
)

# The getting-started steps. ``route`` is the web-UI path the step happens on (a
# view in frontend/apps/web/src/views.ts — test_onboarding_surfaces.py checks it;
# None would mean read-only); ``action`` labels its button. Keys are stable ids
# clients may persist. Completion (``done``) is derived from real data — see
# :func:`step_progress`.
STEPS: list[dict] = [
    {"key": "machine",
     "route": "/machines/computers",
     "title": "Connect a machine",
     "action": "Open machines",
     "what": "Loops run on a computer you own. The one running Loopyard counts "
             "already — you can add another from Machines with a connect link.",
     "via": "Machines · yard up · connect link"},
    {"key": "describe",
     "route": "/newloop",
     "title": "Describe what you want done",
     "action": "New loop",
     "what": "Write the outcome in a sentence or two — e.g. 'build a URL shortener "
             "with tests'. Loopyard drafts a team for it.",
     "via": "New loop · loop_creator_suggest"},
    {"key": "run",
     "route": "/newloop",
     "title": "Review the team and start",
     "action": "Start a loop",
     "what": "See who is on the team and why, change anything you like, then "
             "start. Nothing runs until you do, and you can stop it any time.",
     "via": "loop_save → start_loop"},
    {"key": "result",
     "route": "/loops",
     "title": "See the result and rate it",
     "action": "Open loops",
     "what": "Watch turns land live and answer if the team asks. When it "
             "finishes, open the result — what was made and where — and rate it "
             "good, ok or bad so Loopyard learns what works for you.",
     "via": "Loops · finish-report · loop_disposition_record"},
]

# Optional next steps, shown quietly once the core steps are done (clients
# unlock them when ``progress.complete``). Same shape as STEPS.
EXPLORE: list[dict] = [
    {"key": "plan",
     "route": "/plans",
     "title": "Write your first plan",
     "action": "Open plans",
     "what": "A plan is a living doc for a bigger goal — point loops at it as "
             "the work grows."},
    {"key": "second_machine",
     "route": "/machines/computers",
     "title": "Connect a second computer",
     "action": "Open machines",
     "what": "Run loops on another machine you own — a connect link sets it up "
             "in a minute."},
    {"key": "favorite",
     "route": "/library",
     "title": "Star a favorite agent",
     "action": "Browse agents",
     "what": "Star the agents you like so they are first in line when you build "
             "a team."},
]

# The key web-UI surfaces the guide points a new user at besides the steps.
PLACES: list[dict] = [
    {"key": "place:home", "label": "Home", "route": "/overview",
     "what": "What needs you, what is running, and your getting-started list."},
    {"key": "place:newloop", "label": "New loop", "route": "/newloop",
     "what": "Describe what you want done and get a team for it."},
    {"key": "place:loops", "label": "Loops", "route": "/loops",
     "what": "Watch your loops run, turn by turn, and open their results."},
    {"key": "place:plans", "label": "Plans", "route": "/plans",
     "what": "Living docs for bigger goals that loops work towards."},
    {"key": "place:machines", "label": "Machines", "route": "/machines/computers",
     "what": "The computers your loops run on — this one is one of them."},
    {"key": "place:sessions", "label": "Sessions", "route": "/machines/sessions",
     "what": "Live agent sessions you can open or start a loop from."},
]


def onboarding_agent_record(now: float = 0.0) -> dict:
    """The onboarding guide as an Agent-Registry record (persona + generic goal +
    default role). ``model``/``runtime`` stay unset so it runs on whichever CLI
    the user connected (claude or codex). Mirrors
    :func:`creator.creator_agent_record`."""
    from mcp_loops import agents
    return agents.new_agent_record(
        "loop_onboarding", ONBOARDING_PERSONA, ONBOARDING_GENERIC_GOAL,
        model=None, default_role=schema.WORKER,
        note="BRIEF §4 first-run onboarding guide", now=now)


def build_onboarding_prompt(runtime: str = "") -> str:
    """Assemble the guide prompt a connected CLI session runs to walk a NEW user
    from greeting to a running first team. It leads with the plain-language
    explanation, then drives the same getting-started steps the app shows using
    the real tools — never inventing facts, asking only what it needs."""
    rt = (runtime or "your connected CLI").strip()
    lines = [
        "You are the Loopyard onboarding guide. A brand-new user just started you "
        f"on {rt}. Your job: get them to their first running team in a few "
        "effortless minutes, and make them feel it.",
        "",
        "Open by greeting them warmly and explaining, in plain language, what a "
        "Loopyard team is — you may use this framing verbatim:",
        f"  “{WELCOME}”",
        "",
        "Then walk them through the same getting-started steps the app's Home "
        "page shows, conversationally and one move at a time:",
    ]
    for s in STEPS:
        lines.append(f"  • {s['title']} ({s['route']}) — {s['what']}  (via {s['via']})")
    lines += [
        "",
        "Rules of the walk: use plain words (machine, team, agent, plan, result). "
        "Ask only what actually changes the outcome; when you have their goal, "
        "call loop_creator_suggest to propose a team and show the roles + why, "
        "invite edits, then loop_save the shaped config and start_loop it. Point "
        "them at the Loops view (/loops) to watch it, and when it finishes ask "
        "them to rate the result good, ok or bad (loop_disposition_record). Tell "
        "them the goal is theirs to change any time. Celebrate the first result.",
    ]
    return "\n".join(lines)


def onboard_first_team(goal: str, name: str = "") -> dict:
    """Deterministic 'create your first team' driver: derive a rule-checked team
    from ``goal`` (via :func:`creator.suggest_team`) and hand back a validated,
    schema-normalized config ready to save + start, plus the human-readable plan.

    Returns ``{ok, welcome, goal, suggestion, config, next, steps}`` where
    ``suggestion`` carries the editable roles + why, ``config`` is save-ready, and
    ``next`` names the follow-on tools (loop_save → start_loop). On a blank goal
    returns ``{ok: False, errors}``. Pure: the server tool performs the actual
    save/start; this shapes what to save."""
    sugg = creator.suggest_team(goal, name=name)
    if not sugg.get("ok"):
        return {"ok": False, "errors": sugg.get("errors", ["could not build a team"])}
    cfg = sugg.get("config") or {}
    return {
        "ok": True,
        "welcome": WELCOME,
        "goal": sugg.get("goal"),
        "suggestion": {"roles": sugg.get("roles", []),
                       "signals": sugg.get("signals", []),
                       "lint": sugg.get("lint", []),
                       "note": sugg.get("note", "")},
        "config": cfg,
        "next": {"save": {"tool": "loop_save", "args": {"config": cfg}},
                 "run": {"tool": "start_loop",
                         "args": {"name": cfg.get("name")}}},
        "steps": STEPS,
    }


# ── completion state: which steps the user has ACTUALLY done ──────────────────
# Derived from the local data dir (the same ``data/_loops`` dirs loop_list reads,
# plus its ``_ideahub`` / ``_registry`` siblings), so every surface ticks a step
# when the engine sees it happen — never on a click:
#   machine  ⇐ a reachable machine (this one counts) or any loop that ran
#   describe ⇐ a saved loop (config.json)
#   run      ⇐ a started run (run.json past 'saved')
#   result   ⇐ a rating (good/ok/bad in a loop's dispositions.jsonl)
_RESULT_STATES = envelope.RUN_TERMINAL | {"completed", "early_finish", "done"}
_RATING_VERBS = frozenset({"good", "ok", "bad"})


def _rated(base: str) -> bool:
    """Has the owner rated any run of the loop at ``base`` (good/ok/bad)?"""
    try:
        with open(os.path.join(base, "dispositions.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("verb") in _RATING_VERBS:
                    return True
    except OSError:
        pass
    return False


def read_loop_facts(root: Optional[str]) -> list[dict]:
    """One ``{name, started, ended, rated}`` row per saved loop under ``root`` (a
    ``data/_loops`` dir). Unreadable/missing dirs and private ``_*`` dirs are
    skipped, so a fresh install yields ``[]``."""
    facts: list[dict] = []
    if not root or not os.path.isdir(root):
        return facts
    for entry in sorted(os.listdir(root)):
        if entry.startswith("_") or entry.startswith("."):
            continue
        base = os.path.join(root, entry)
        if not os.path.isfile(os.path.join(base, "config.json")):
            continue
        try:
            with open(os.path.join(base, "run.json"), encoding="utf-8") as f:
                run = json.load(f)
        except (OSError, ValueError):
            run = {}
        run = run if isinstance(run, dict) else {}
        state = str(run.get("state") or "saved")
        facts.append({
            "name": entry,
            "started": bool(run.get("started")) or state not in ("saved", ""),
            "ended": state in _RESULT_STATES or bool(run.get("finish_report"))
                     or run.get("result") is not None,
            "rated": _rated(base),
        })
    return facts


def _count_machines(root: Optional[str]) -> int:
    """Reachable machines visible from ``root`` (this one + fresh mirrors)."""
    from mcp_loops import origins
    if not root:
        return 0
    return sum(1 for o in origins.list_origins(root, now=time.time())
               if o.get("reachable"))


def _count_plans(root: Optional[str]) -> int:
    """Plans (Idea Hub docs) under ``<root>/_ideahub`` — one ``<id>.json`` each."""
    d = os.path.join(root or "", "_ideahub")
    if not root or not os.path.isdir(d):
        return 0
    return sum(1 for f in os.listdir(d) if f.endswith(".json"))


def _count_favorites(root: Optional[str]) -> int:
    """Starred agents in ``<root>/_registry/agents``."""
    d = os.path.join(root or "", "_registry", "agents")
    if not root or not os.path.isdir(d):
        return 0
    n = 0
    for f in os.listdir(d):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        n += 1 if isinstance(rec, dict) and rec.get("favorite") else 0
    return n


def gather_facts(root: Optional[str], *, machines: Optional[int] = None) -> dict:
    """Everything progress needs, read fail-soft from the data dir ``root``:
    ``{loops, machines, plans, favorites}``. ``machines`` overrides the local
    count (the server passes its live fleet view). A read that fails counts as
    nothing — a store error never sinks the checklist."""
    def safe(fn, default):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return default
    return {
        "loops": safe(lambda: read_loop_facts(root), []),
        "machines": machines if isinstance(machines, int)
        else safe(lambda: _count_machines(root), 0),
        "plans": safe(lambda: _count_plans(root), 0),
        "favorites": safe(lambda: _count_favorites(root), 0),
    }


def step_progress(facts: list[dict], machines: int = 0) -> dict[str, bool]:
    """``{step key: done}`` for every STEPS entry. Pure. ``facts`` are
    :func:`read_loop_facts` rows; ``machines`` the reachable-machine count. A
    later loop fact implies the earlier loop steps (a rating implies a run, a run
    implies a saved loop and a machine it ran on)."""
    rated = any(f.get("rated") for f in facts)
    started = rated or any(f.get("started") or f.get("ended") for f in facts)
    saved = started or bool(facts)
    evidence = {"machine": machines > 0 or started, "describe": saved,
                "run": started, "result": rated}
    return {s["key"]: evidence[s["key"]] for s in STEPS}


def explore_progress(*, machines: int = 0, plans: int = 0,
                     favorites: int = 0) -> dict[str, bool]:
    """``{explore key: done}`` for every EXPLORE entry. Pure."""
    evidence = {"plan": plans > 0, "second_machine": machines >= 2,
                "favorite": favorites > 0}
    return {s["key"]: evidence[s["key"]] for s in EXPLORE}


def onboarding_summary(progress: Optional[dict[str, bool]] = None,
                       explore: Optional[dict[str, bool]] = None) -> dict:
    """The identity + welcome + step script the first-run entries surface. Used by
    the ``loop_onboarding_agent`` tool and ``yard onboard``. With ``progress``
    (:func:`step_progress`) each step carries ``done`` and the payload a
    ``progress`` block; with ``explore`` (:func:`explore_progress`) each explore
    item carries ``done``. Without them the lists are the bare script."""
    steps = STEPS if progress is None else [
        {**s, "done": bool(progress.get(s["key"]))} for s in STEPS]
    more = EXPLORE if explore is None else [
        {**s, "done": bool(explore.get(s["key"]))} for s in EXPLORE]
    out = {
        "agent": {"id": "loop_onboarding", "persona": ONBOARDING_PERSONA,
                  "genericGoal": ONBOARDING_GENERIC_GOAL,
                  "defaultRole": schema.WORKER, "runtimes": creator.VALID_RUNTIMES},
        "welcome": WELCOME,
        "steps": steps,
        "explore": more,
        "places": PLACES,
        "rules": authoring.RULES,
    }
    if progress is not None:
        n = sum(1 for s in steps if s["done"])
        nxt = next((s["key"] for s in steps if not s["done"]), None)
        out["progress"] = {"done": n, "total": len(steps), "next": nxt,
                           "complete": nxt is None}
    return out


def progress_payload(root: Optional[str], *, machines: Optional[int] = None) -> dict:
    """The getting-started payload every app surface reads — ``yard onboard
    --json`` (desktop guide) and ``loop_onboarding_progress`` (web Home checklist)
    both return exactly this: the summary with engine-derived ``done`` on every
    step and explore item + ``progress``, the prompt a CLI session runs the guide
    with, and no authoring rules (no surface renders them)."""
    f = gather_facts(root, machines=machines)
    o = onboarding_summary(
        step_progress(f["loops"], f["machines"]),
        explore_progress(machines=f["machines"], plans=f["plans"],
                         favorites=f["favorites"]))
    out = {k: v for k, v in o.items() if k != "rules"}
    out["prompt"] = build_onboarding_prompt()
    return out


def identity_lint() -> list[dict]:
    """Lint the guide's own identity against authoring.RULES (should be empty).
    Wraps the persona/goal in a one-step config and reuses the real linter — the
    same bar the creator identity meets."""
    probe: dict[str, Any] = {"name": "onboarding", "goal": "the loop goal",
                             "steps": {"guide": {"type": "agent", "role": schema.WORKER,
                                                 "personality": ONBOARDING_PERSONA,
                                                 "goal": ONBOARDING_GENERIC_GOAL}}}
    return authoring.lint_config(probe)
