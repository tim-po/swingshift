"""Loop scaffolding — start a new loop from a template in 30 seconds.

Templates are complete, valid loop configs (see ``mcp_loops/templates/*.json``)
with two placeholder tokens — ``__NAME__`` and ``__GOAL__`` — plus a top-level
``description`` shown in ``--list`` and stripped from the emitted config. The
scaffolder substitutes the tokens, validates the result, runs the preflight
:mod:`~mcp_loops.doctor` on it, and writes the config the owner then reviews
and hands to ``loop_save``/``loop_start``::

    python -m mcp_loops.new --list
    python -m mcp_loops.new build-critic --name dash-polish \
        --goal "Make the dashboard great" [-o config.json] [--turns 30]

Authoring rule for templates: agent personalities/goals must be self-standing
and reference "the loop goal" generically, so any goal drops in cleanly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp_loops.doctor import doctor, format_report
from mcp_loops.schema import validate_config

TEMPLATES_DIR = Path(__file__).parent / "templates"


def list_templates() -> list[dict]:
    """[{name, description, agents}] for every bundled template, sorted by name."""
    out = []
    for p in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "name": p.stem,
            "description": raw.get("description", ""),
            "agents": len(raw.get("steps", {})),
        })
    return out


def scaffold(template: str, *, name: str, goal: str, turn_limit: int | None = None) -> dict:
    """Instantiate ``template`` and return ``{ok, errors, config, doctor}``.

    ``config`` is the authoring-form dict (tokens substituted, ``description``
    stripped) ready to write to disk; ``doctor`` is the full preflight result.
    """
    path = TEMPLATES_DIR / f"{template}.json"
    if not path.is_file():
        known = ", ".join(t["name"] for t in list_templates()) or "none found"
        return {"ok": False, "errors": [f"unknown template {template!r} (available: {known})"],
                "config": None, "doctor": None}
    if not name.strip() or not goal.strip():
        return {"ok": False, "errors": ["--name and --goal are both required and must be non-empty"],
                "config": None, "doctor": None}

    text = path.read_text(encoding="utf-8")
    text = text.replace("__NAME__", json.dumps(name.strip())[1:-1])
    text = text.replace("__GOAL__", json.dumps(goal.strip())[1:-1])
    cfg = json.loads(text)
    cfg.pop("description", None)
    if turn_limit is not None:
        cfg.setdefault("budget", {})["turnLimit"] = turn_limit

    v = validate_config(cfg)
    if not v["ok"]:
        return {"ok": False, "errors": [f"template produced invalid config: {e}" for e in v["errors"]],
                "config": cfg, "doctor": None}
    return {"ok": True, "errors": [], "config": cfg, "doctor": doctor(cfg)}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or "--help" in argv:
        print(__doc__.strip().splitlines()[0]
              + "\nusage: python -m mcp_loops.new --list | <template> --name N --goal G"
                " [-o out.json] [--turns K]", file=sys.stderr)
        return 2
    if "--list" in argv:
        for t in list_templates():
            print(f"{t['name']:<14} {t['agents']} agents — {t['description']}")
        return 0

    template, name, goal, out_path, turns = argv[0], "", "", None, None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--name" and i + 1 < len(argv):
            name = argv[i + 1]; i += 2
        elif a == "--goal" and i + 1 < len(argv):
            goal = argv[i + 1]; i += 2
        elif a in ("-o", "--out") and i + 1 < len(argv):
            out_path = argv[i + 1]; i += 2
        elif a == "--turns" and i + 1 < len(argv):
            try:
                turns = int(argv[i + 1])
            except ValueError:
                print("--turns must be an integer", file=sys.stderr)
                return 2
            i += 2
        else:
            print(f"unknown argument {a!r}", file=sys.stderr)
            return 2

    res = scaffold(template, name=name, goal=goal, turn_limit=turns)
    if not res["ok"]:
        for e in res["errors"]:
            print(f"✗ {e}", file=sys.stderr)
        return 1

    dest = Path(out_path) if out_path else Path(f"{name.strip()}.json")
    dest.write_text(json.dumps(res["config"], indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"✓ wrote {dest} (template {template!r})")
    print(format_report(name, res["doctor"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
