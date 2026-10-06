"""Loop preflight doctor — "why will this loop silently fail?", answered BEFORE start.

Static schema validation lives in :mod:`mcp_loops.schema`; the doctor goes beyond
it with the two things an author cannot see from the config file alone:

* **schedule preview** — how the budget actually spends: rounds until
  ``turnLimit``, the guaranteed wind-down tail, and a wall-clock UPPER BOUND from
  each agent's ``maxTurnMinutes`` (the substrate's per-turn timeout), so "40
  turns" stops being an abstraction.
* **environment preflight** — the runtime dependencies the runner assumes and
  today fails on silently: the worker daemon socket, the worker venv python, a
  writable loops data dir, and whether the loop name already has state on disk.

Pure functions over (config, paths) — no network, no daemon calls — so tests run
anywhere and the CLI is safe to point at anything::

    python -m mcp_loops.doctor path/to/config.json          # human report, exit 0/1
    python -m mcp_loops.doctor path/to/config.json --json   # machine-readable
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from mcp_loops import paths
from mcp_loops.schema import validate_config, summarize

# R6: state paths come from ``paths`` (LOOPS_DATA_DIR > LOOPYARD_HOME > install).
# ``_HOME`` is only the legacy/test override (the deprecated BOT_SQUAD_HOME, or
# ``preflight_env(home=)``) under which the old ``<home>/data/...`` layout holds.
_HOME = paths.legacy_home_override("doctor")


# ── schedule preview ─────────────────────────────────────────────────────────
def schedule_preview(norm_cfg: dict) -> dict:
    """Derive how the turn budget spends from a *normalized* config.

    Returns rounds/turns arithmetic plus a wall-clock upper bound in minutes
    (every scheduled turn hitting its ``maxTurnMinutes`` timeout — the worst
    legal case, useful as a "never longer than" promise to the owner).
    """
    steps = norm_cfg.get("steps", {})
    order = norm_cfg.get("stepOrder", [])
    budget = norm_cfg.get("budget", {})
    turn_limit = budget.get("turnLimit", 0)
    summ = summarize(norm_cfg)

    def _minutes(sid: str) -> int:
        s = steps.get(sid, {})
        if s.get("type") == "loop":  # a sub-loop turn: bound by ITS own preview
            return schedule_preview(s.get("loop", {})).get("maxWallClockMinutes", 0)
        return s.get("maxTurnMinutes", 30)

    flat: list[str] = []
    for ref in order:
        flat.extend(ref if isinstance(ref, list) else [ref])
    turns_per_round = len(flat)
    round_minutes = sum(_minutes(sid) for sid in flat)

    if turns_per_round:
        # a round in flight finishes: ceil(turnLimit / per-round) full rounds
        full_rounds = -(-turn_limit // turns_per_round)
        main_turns = full_rounds * turns_per_round
        main_minutes = full_rounds * round_minutes
    else:
        full_rounds = main_turns = main_minutes = 0

    workers = summ["workers"]
    worker_minutes = sum(_minutes(sid) for sid in workers)
    mgr = summ["manager"]
    mgr_minutes = _minutes(mgr) if mgr else 0
    wind_minutes = 2 * worker_minutes + summ["windDownManagerTurns"] * mgr_minutes

    return {
        "turnsPerRound": turns_per_round,
        "roundsUntilLimit": full_rounds,
        "mainPhaseTurns": main_turns,
        "windDownTurns": summ["windDownTotal"],
        "totalTurns": main_turns + summ["windDownTotal"],
        "maxWallClockMinutes": main_minutes + wind_minutes,
    }


# ── environment preflight ────────────────────────────────────────────────────
def preflight_env(norm_cfg: dict, *, home: Path | str | None = None) -> list[dict]:
    """Check the runtime assumptions the headless substrate makes.

    Returns a list of ``{check, ok, detail}`` — never raises. ``home`` overrides
    the install root for tests.
    """
    root = Path(home) if home else _HOME
    checks: list[dict] = []

    def add(check: str, ok: bool, detail: str) -> None:
        checks.append({"check": check, "ok": ok, "detail": detail})

    if root is not None:
        loops_dir = root / "data" / "_loops"
        sock = root / "data" / "_sock" / "worker.sock"
        py = root / "worker" / ".venv" / "bin" / "python"
    else:
        loops_dir = Path(paths.resolve_data_dir())
        sock = Path(paths.sock_dir()) / "worker.sock"
        py = paths.install_root() / "worker" / ".venv" / "bin" / "python"
    # R20: a bound runner (runner.json) names the real socket + interpreter.
    from mcp_loops import runner_registry
    rb = runner_registry.read(str(loops_dir))
    if rb:
        if rb.get("sock"):
            sock = Path(rb["sock"])
        if rb.get("python"):
            py = Path(rb["python"])

    if not sock.exists():
        add("worker daemon socket", False, f"{sock} does not exist — is the worker daemon running?")
    else:
        is_sock = stat.S_ISSOCK(sock.stat().st_mode)
        add("worker daemon socket", is_sock,
            str(sock) if is_sock else f"{sock} exists but is not a socket")

    add("worker venv python", py.is_file() and os.access(py, os.X_OK), str(py))

    writable = loops_dir.is_dir() and os.access(loops_dir, os.W_OK)
    add("loops data dir writable", writable, str(loops_dir))

    name = norm_cfg.get("name", "")
    if name:
        existing = loops_dir / name
        if (existing / "run.json").exists():
            state = None
            try:
                state = json.loads((existing / "run.json").read_text()).get("state")
            except (OSError, json.JSONDecodeError):
                pass
            live = state in ("running", "waiting_owner", "needs_owner", "stopping")
            add("loop name free", not live,
                f"data/_loops/{name} already has state ({state or 'unreadable'})"
                + (" — starting would collide with a live run" if live
                   else " — prior run; starting resumes/overwrites its dir"))
        else:
            add("loop name free", True, f"data/_loops/{name} has no prior run state")

    if norm_cfg.get("substrate") == "autogen":
        add("substrate", False, "substrate 'autogen' is not implemented yet — use 'headless'")

    return checks


# ── the doctor ───────────────────────────────────────────────────────────────
def doctor(raw_cfg, *, home: Path | str | None = None) -> dict:
    """Full preflight: schema + schedule preview + environment. Never raises."""
    v = validate_config(raw_cfg)
    out = {
        "ok": v["ok"],
        "errors": list(v["errors"]),
        "warnings": list(v["warnings"]),
        "schedule": None,
        "env": [],
    }
    if v["ok"]:
        out["schedule"] = schedule_preview(v["config"])
        out["env"] = preflight_env(v["config"], home=home)
        out["ok"] = all(c["ok"] for c in out["env"])
    return out


def format_report(name: str, rep: dict) -> str:
    lines = [f"── loop doctor: {name} ──"]
    for e in rep["errors"]:
        lines.append(f"  ✗ ERROR   {e}")
    for w in rep["warnings"]:
        lines.append(f"  ⚠ warning {w}")
    sched = rep.get("schedule")
    if sched:
        lines.append(
            f"  ◷ schedule {sched['turnsPerRound']} turns/round × "
            f"{sched['roundsUntilLimit']} rounds + {sched['windDownTurns']} wind-down "
            f"= ≤{sched['totalTurns']} turns, ≤{sched['maxWallClockMinutes']} min wall-clock")
    for c in rep.get("env", []):
        mark = "✓" if c["ok"] else "✗"
        lines.append(f"  {mark} {c['check']}: {c['detail']}")
    lines.append("  → " + ("READY to start" if rep["ok"] else "NOT ready — fix the ✗ lines above"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        print("usage: python -m mcp_loops.doctor <config.json> [--json]", file=sys.stderr)
        return 2
    try:
        raw = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read config: {exc}", file=sys.stderr)
        return 2
    rep = doctor(raw)
    if as_json:
        print(json.dumps(rep, indent=2))
    else:
        print(format_report(raw.get("name", paths[0]) if isinstance(raw, dict) else paths[0], rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
