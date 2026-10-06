"""Standalone runs — the activation front door: one agent × one product × a model.

The product_owner's "2-minute aha": pick an agent (a reviewer, a changelog-writer),
pick a product (my repo), pick a model → get a rendered RESULT. That single flow
exercises the whole value chain — registry + product-as-target + model selection +
output contract — without authoring a loop.

This module is pure (string/spec builders, no I/O): the server resolves the agent
version + product host paths, creates the output folder, and persists what these
functions return. ``now`` is injected.

MODEL HONESTY (product_owner ruling / quality_critic finding): a run RECORDS the
model it selected, but until the worker daemon accepts a per-session model (its
``spawn_session`` currently REJECTS unknown params — see
suggestions/MODEL-PASSTHROUGH-daemon.md) the model is *not applied at spawn*. So
``modelApplied`` is DERIVED from the shared ``LOOPS_MODEL_PASSTHROUGH`` gate (∧ a
resolved model) — the same gate the loop path reads — NOT a hardcoded constant:
gate off ⇒ ``modelApplied: False`` + a pending note (today's behavior, no silent
no-op picker); gate on ⇒ it flips to True automatically, no edit here. This is the
structural honesty ``originApplied`` already has right beside it.
"""

from __future__ import annotations

from typing import Optional

from mcp_loops import agents

# Why a selected model is not yet applied end-to-end (surfaced in spec + result).
MODEL_PENDING_REASON = (
    "recorded — not yet applied (worker-daemon spawn_session does not yet accept "
    "a per-session model; see suggestions/MODEL-PASSTHROUGH-daemon.md)")


def select_model(explicit: object, agent_head_model: Optional[str]) -> Optional[str]:
    """Resolve the model a standalone run WOULD use: an explicit choice wins,
    else the agent's own preferred (head) model, else None = inherit the host
    default. Validated/normalized via agents.normalize_model."""
    if explicit is not None and str(explicit).strip():
        return agents.normalize_model(explicit)
    return agents.normalize_model(agent_head_model)


def build_run_spec(agent_rec: dict, version: Optional[int], product: dict,
                   model: Optional[str], *, repo_path: str, output_dir: str,
                   now: float, origin: Optional[str] = "local",
                   model_passthrough: bool = False) -> dict:
    """Assemble the immutable spec for a standalone run from an agent version,
    a product, and a selected model. Pure — no I/O.

    A run = **agent × origin × product × model** (loopyard-core north star §3).
    ``origin`` is the mirror the run belongs to; it defaults to ``"local"`` (the
    ONLY origin runs actually execute on today — cross-origin dispatch is a
    later increment). Recorded on the spec so the surface can tag/filter runs
    by origin without inferring from paths.

    ``modelApplied`` is DERIVED (not a hardcoded constant): it is true only when
    the shared ``LOOPS_MODEL_PASSTHROUGH`` gate is on AND a model was resolved.
    OFF-by-default keeps today's behavior (False + pending note); the flag then
    tells the truth automatically the day the gate flips, with no edit here.
    """
    v = agents.agent_version(agent_rec, version)
    if v is None:
        raise ValueError(
            f"agent {agent_rec.get('id')!r} has no version {version!r} "
            f"(head is {agent_rec.get('head')})")
    # DERIVED honesty: applied only when the shared LOOPS_MODEL_PASSTHROUGH gate
    # is on AND a model was resolved — matches the loop path's contract (headless
    # _spawn adds `model` only under the gate). Conservative: gate ∧ model.
    model_applied = bool(model_passthrough and model)
    return {
        "kind": "standalone_run",
        "agent": {
            "id": agent_rec.get("id"),
            "version": v["version"],
            "role": agent_rec.get("defaultRole", "worker"),
            "persona": v["persona"],
            "genericGoal": v["genericGoal"],
        },
        "origin": origin,
        "product": {
            "id": product.get("id"),
            "name": product.get("name"),
            "gitRemote": product.get("gitRemote"),
            "gitBranch": product.get("gitBranch"),
            "repoPath": repo_path,
        },
        "model": model,
        "modelApplied": model_applied,
        "modelNote": None if model_applied else MODEL_PENDING_REASON,
        "outputDir": output_dir,
        "createdAt": now,
    }


def frame_standalone_prompt(spec: dict) -> str:
    """The one-shot prompt handed to the agent for a standalone run: its fixed
    persona + generic goal, aimed at the product's checkout, told where to write
    results. The report banner is added by the substrate at dispatch time."""
    a = spec["agent"]
    p = spec["product"]
    branch = f" (branch {p['gitBranch']})" if p.get("gitBranch") else ""
    return "\n\n".join([
        f"# STANDALONE RUN: {a['id']} → {p.get('name') or p.get('id')}",
        f"### your persona\n{a['persona']}",
        f"### your generic goal\n{a['genericGoal']}",
        f"### the product\nWork against the checkout at: {p['repoPath']}{branch}\n"
        f"git remote: {p.get('gitRemote') or '(local dir)'}",
        f"### output contract\nWrite your curated result files into: {spec['outputDir']}\n"
        "Leave a short markdown summary of what you produced (a 'PR description' for "
        "your work: what changed, links to the files, and an honest note on quality).",
    ])


def render_result_markdown(spec: dict, *, summary: str = "",
                           file_links: Optional[list[str]] = None,
                           goodness: Optional[dict] = None) -> str:
    """The manager/agent-authored RESULT, framed as a PR-description for the
    agent's work (product_owner §4). Rendered in the web app. Ships as a stub the
    run fills in; the dual goodness note (owner + analyst) is reserved in-layout
    for Pillar 4."""
    a = spec["agent"]
    p = spec["product"]
    model_line = spec.get("model") or "inherited (host default)"
    origin = spec.get("origin") or "local"
    lines = [
        f"# {a['id']} → {p.get('name') or p.get('id')}",
        "",
        f"*Standalone run · agent `{a['id']}@v{a['version']}` · "
        f"origin `{origin}` · product `{p.get('id')}` · model `{model_line}`*",
        "",
        f"> **Model:** {model_line} — {spec.get('modelNote', '')}",
        "",
        "## What the agent produced",
        summary or "_(the run writes its summary here)_",
        "",
        "## Files",
    ]
    for link in (file_links or []):
        lines.append(f"- {link}")
    if not file_links:
        lines.append(f"_(curated files land in `{spec['outputDir']}`)_")
    lines += [
        "",
        "## Goodness",
        "| Owner rating | Analyst score |",
        "| --- | --- |",
        (f"| {goodness.get('owner', '—')} | {goodness.get('analyst', '—')} |"
         if goodness else "| _pending_ | _pending_ |"),
        "",
        "_Dual goodness (owner rating + independent analyst) lands with Pillar 4; "
        "the honesty is the feature._",
    ]
    return "\n".join(lines)


_GOODNESS_HEAD = "| Owner rating | Analyst score |"


def fill_goodness_table(markdown: str, goodness: dict) -> str:
    """Fill the reserved Goodness table of an already-rendered result markdown
    with ``{owner, analyst}`` cells (see :func:`mcp_loops.goodness.table_cells`).
    The two stay separate columns — never blended. Only the data row right after
    the header/separator is replaced; a markdown without the table is returned
    unchanged. Idempotent."""
    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == _GOODNESS_HEAD and i + 2 < len(lines) \
                and lines[i + 2].lstrip().startswith("|"):
            lines[i + 2] = (f"| {goodness.get('owner', '—')} | "
                            f"{goodness.get('analyst', '—')} |")
            break
    return "\n".join(lines)
