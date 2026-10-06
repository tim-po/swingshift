"""First-party hub capabilities — shared backend helpers (Round B2).

The bundled capabilities Objectives (C1) and Known-Issues (C4) need a little more
than the generic ``loopyard/<dataDir>/`` JSON round-trip:

* **Objectives → Build** — seed a real loop *config* bound to the Project from an
  objective, so intent becomes a team (:func:`build_loop_from_objective`). The
  ``/newloop`` creator can't be URL-prefilled, so ``capability_build_loop`` saves
  this config as a draft and the dashboard opens it in the prefilled editor.
* **Known-Issues union** — show the curated per-Project issues alongside the
  existing auto-filed loop-failure issues (``data/_issues``) for THIS Project.
  Auto-filed issue records carry no ``projectId`` — only the ``loop`` name — so
  the join is ``issue.loop → loop config → projectId``
  (:func:`filter_autofiled`).

Everything here is PURE (no filesystem, no server import): the server layer
injects the readers/resolvers and wires ``loop_save``. Same house style as
:mod:`mcp_loops.capabilities` / :mod:`mcp_loops.loopyard`.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from mcp_loops.projects import slug


# ── C1 Objectives: seed a loop from an objective (intent → team) ──────────────
def build_loop_from_objective(
    project_id: str,
    title: str,
    detail: str = "",
    *,
    cap_id: str = "objectives",
) -> dict[str, Any]:
    """Build a valid, reusable-team loop config seeded from an objective and
    bound to the Project.

    The team is GENERIC (role-based agent goals); the objective — the only
    per-run specific — goes in the TOP-LEVEL goal, exactly the reusable-team rule
    the linter enforces. Bound via the canonical ``projectId`` (``loop_save``
    mirrors ``productId`` for back-compat). Deterministic name per
    (project, objective) so re-building updates one draft instead of littering
    the registry. Pure: returns the config; the caller saves it.
    """
    title = (title or "").strip()
    body = title
    if (detail or "").strip():
        body += "\n\n" + detail.strip()
    goal = (
        body
        + f"\n\nThis loop was seeded (via the {cap_id} capability's → Build) from an "
        f"objective of Project '{project_id}'. Build the objective described above "
        "end-to-end and bind the work to that Project; the manager gates a one-turn "
        "wind-down once the deliverable is real and verified by evidence."
    )
    name = f"build-{slug(project_id)}-{slug(title) or 'objective'}"[:60].rstrip("-")
    return {
        "name": name,
        "goal": goal,
        "projectId": project_id,
        "budget": {"turnLimit": 12},
        "steps": {
            "manager": {
                "role": "manager",
                "personality": "A tight orchestrator who drives the loop to its stated "
                "goal and gates a one-turn wind-down once the deliverable is real.",
                "goal": "Drive the loop to the goal it was given; verify the builder "
                "produced the deliverable with evidence before finishing.",
            },
            "builder": {
                "role": "worker",
                "personality": "A senior engineer who ships real, minimal, working "
                "changes rather than plans or prose.",
                "goal": "Build the deliverable the loop goal describes; produce real, "
                "verifiable output, not a plan.",
            },
            "reviewer": {
                "role": "input_provider",
                "personality": "A reviewer who checks the deliverable against the goal "
                "with evidence, never eyeballing.",
                "goal": "Verify the builder's deliverable against the loop goal; report "
                "concrete gaps as inputs.",
            },
        },
        "stepOrder": ["manager", "builder", "reviewer"],
    }


# ── stored capability records → normalized items ─────────────────────────────
def normalize_stored_item(name: str, record: dict, rel_path: str) -> dict[str, Any]:
    """A JSON record written by a capability page → the unified item shape the
    page renders. Passes the record's own fields through, stamps ``source`` +
    provenance, and defaults the id to the filename stem."""
    stem = name[:-5] if name.endswith(".json") else name
    item: dict[str, Any] = dict(record)
    item.setdefault("id", record.get("id") or stem)
    item["source"] = "stored"
    item["name"] = name
    item["path"] = rel_path
    item.setdefault("title", record.get("title") or stem)
    item.setdefault("detail", record.get("detail", ""))
    item.setdefault("status", record.get("status", ""))
    return item


# ── C4 Known-Issues: auto-filed loop-failure issues, filtered to a Project ────
def issue_row_to_item(row: dict) -> dict[str, Any]:
    """One ``index.jsonl`` auto-filed issue row → the unified item shape, tagged
    ``source="autofiled"`` with its loop/issue provenance so the page can link to
    the existing issue detail view."""
    loop = row.get("loop") or "?"
    agent = row.get("failing_agent") or "loop"
    ended = row.get("ended") or "failure"
    fstatus = row.get("failing_status")
    phase = row.get("failing_phase")
    detail = f"Auto-filed loop failure — {ended}"
    if agent and agent != "loop":
        detail += f" in agent '{agent}'"
    if phase:
        detail += f" ({phase}{'/' + fstatus if fstatus else ''})"
    return {
        "id": row.get("id") or f"{loop}-{agent}",
        "source": "autofiled",
        "title": f"Loop failure: {loop}",
        "detail": detail,
        "status": row.get("status", "open"),
        "severity": "auto",
        "loop": loop,
        "issueId": row.get("id"),
        "ts": row.get("ts"),
    }


def filter_autofiled(
    rows: list[dict],
    project_id: str,
    project_of: Callable[[str], Optional[str]],
) -> list[dict[str, Any]]:
    """Keep the auto-filed issue rows whose loop is bound to ``project_id`` and
    return them as normalized items, newest first.

    ``project_of(loop_name)`` resolves a loop → its bound projectId (the server
    reads the loop config; tests inject a dict). Rows with no loop, an
    unresolvable loop, or a different project are dropped.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        loop = row.get("loop")
        if not loop:
            continue
        if project_of(loop) != project_id:
            continue
        out.append(issue_row_to_item(row))
    out.sort(key=lambda it: (it.get("ts") or 0), reverse=True)
    return out
