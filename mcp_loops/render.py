"""Render a loop config as a self-contained HTML block-scheme.

The owner reviews a loop two ways in Telegram: the raw JSON file, and this
prettified flow diagram (sent as an .html document via ``tg_send_document``).
Output is a single standalone HTML string — inline CSS, theme-aware (light/dark),
no external assets — so it renders anywhere and prints cleanly.

Public API::

    html = render_html(config)            # config may be raw or normalized
    render_to_file(config, "/path.html")  # convenience
"""

from __future__ import annotations

import html as _html
from typing import Any

from mcp_loops.schema import (
    INPUT_PROVIDER,
    MANAGER,
    WORKER,
    summarize,
    validate_config,
)

# role → (label, accent-var-suffix)
_ROLE_META = {
    MANAGER: ("MANAGER", "mgr"),
    WORKER: ("WORKER", "wrk"),
    INPUT_PROVIDER: ("INPUT / CRITIC", "inp"),
}


def render_html(config: Any, *, title: str | None = None) -> str:
    """Return a standalone HTML block-scheme for ``config`` (raw or normalized)."""
    res = validate_config(config)
    cfg = res["config"]
    s = summarize(cfg)
    name = cfg.get("name") or "(unnamed loop)"
    doc_title = title or f"Loop · {name}"

    invalid_banner = ""
    if not res["ok"]:
        items = "".join(f"<li>{_esc(e)}</li>" for e in res["errors"])
        invalid_banner = (
            '<div class="banner err"><b>⚠ config has errors — this is a draft view</b>'
            f'<ul>{items}</ul></div>'
        )
    warn_banner = ""
    if res["warnings"]:
        items = "".join(f"<li>{_esc(w)}</li>" for w in res["warnings"])
        warn_banner = f'<div class="banner warn"><b>notes</b><ul>{items}</ul></div>'

    body = f"""
{_header(cfg, s)}
{invalid_banner}
{warn_banner}
{_legend()}
{_flow(cfg, s)}
{_rules_note(cfg, s)}
"""
    return _PAGE.replace("__TITLE__", _esc(doc_title)).replace("__BODY__", body)


def render_to_file(config: Any, path: str, *, title: str | None = None) -> str:
    html_str = render_html(config, title=title)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_str)
    return path


# ── sections ────────────────────────────────────────────────────────────────
def _header(cfg: dict, s: dict) -> str:
    caps = cfg.get("contextCaps", {})
    chips = [
        _chip("substrate", cfg.get("substrate", "headless")),
        _chip("turn limit", f"{s['turnLimit']} turns"),
        _chip("ctx cap", f"{_k(caps.get('default'))} · mgr {_k(caps.get('manager'))}"),
        _chip("wind-down", f"{s['windDownWorkerTurns']} worker + {s['windDownManagerTurns']} mgr"),
        _chip("team", f"{s['n_workers']}W · {s['n_inputs']}I · 1M"),
    ]
    return f"""
<header>
  <div class="eyebrow">LOOP</div>
  <h1>{_esc(cfg.get('name') or '(unnamed)')}</h1>
  <blockquote class="goal"><span class="star">★</span> {_esc(cfg.get('goal') or '')}</blockquote>
  <div class="chips">{''.join(chips)}</div>
</header>"""


def _legend() -> str:
    dots = "".join(
        f'<span class="lg"><i class="dot {suf}"></i>{lbl}</span>'
        for lbl, suf in (("Manager", "mgr"), ("Worker", "wrk"),
                         ("Input / Critic", "inp"), ("Sub-loop", "loop"))
    )
    return f'<div class="legend">{dots}</div>'


def _flow(cfg: dict, s: dict) -> str:
    parts: list[str] = ['<div class="flow">']

    # briefing gate
    parts.append(_gate("① BRIEFING", "Owner ↔ Manager — multi-message; manager confirms it holds the goal before any worker runs"))
    parts.append(_arrow())

    # the repeating round
    parts.append('<div class="round"><div class="round-tag">ROUND — repeats until turn limit spent or manager ends early</div>')
    order = cfg.get("stepOrder", [])
    steps = cfg.get("steps", {})
    if not order:
        parts.append('<div class="empty">stepOrder is empty</div>')
    for i, sid in enumerate(order):
        parts.append(_step_block(sid, steps.get(sid, {})))
        if i < len(order) - 1:
            parts.append(_arrow())
    parts.append('</div>')  # round

    parts.append(_arrow("loop back ↺ / or ↓ when done"))

    # wind-down
    parts.append(_gate(
        "② WIND-DOWN (guaranteed)",
        f"{s['windDownWorkerTurns']} worker-only turns "
        f"({s['n_workers']}×2) → manager finalize ×{s['windDownManagerTurns']} "
        "(triage → consolidate → final report). No input agents run here."))
    parts.append('</div>')  # flow
    return "".join(parts)


def _step_block(sid: str, sdef: dict) -> str:
    stype = sdef.get("type", "agent")
    if stype == "loop":
        inner = sdef.get("loop", {})
        s2 = summarize(inner)
        chain = " → ".join(_esc(x) for x in inner.get("stepOrder", [])) or "—"
        return f"""
<div class="block loop">
  <div class="bhead"><span class="badge loop">SUB-LOOP</span><span class="bid">{_esc(sid)}</span></div>
  <div class="bname">{_esc(inner.get('name') or 'nested loop')}</div>
  <div class="bgoal">{_esc(_snip(inner.get('goal', ''), 140))}</div>
  <div class="subchain">{chain}</div>
  <div class="submeta">{s2['n_workers']}W · {s2['n_inputs']}I · 1M · {s2['turnLimit']} turns</div>
</div>"""

    role = sdef.get("role", WORKER)
    label, suf = _ROLE_META.get(role, ("AGENT", "wrk"))
    repeat = sdef.get("maxRepeatTurns", 1)
    repeat_badge = f'<span class="rep">↻×{repeat}</span>' if repeat and repeat > 1 else ""
    tools = sdef.get("tools") or []
    tools_html = ""
    if tools:
        tools_html = '<div class="tools">' + "".join(
            f'<span class="tool">{_esc(str(t))}</span>' for t in tools) + '</div>'
    return f"""
<div class="block {suf}">
  <div class="bhead"><span class="badge {suf}">{label}</span><span class="bid">{_esc(sid)}</span>{repeat_badge}</div>
  <div class="bpers">{_esc(_snip(sdef.get('personality', ''), 160))}</div>
  <div class="bgoal"><b>goal:</b> {_esc(_snip(sdef.get('goal', ''), 160))}</div>
  {tools_html}
</div>"""


def _rules_note(cfg: dict, s: dict) -> str:
    b = cfg.get("budget", {})
    pct = int(round(b.get("minorSkipThreshold", 0.3) * 100))
    return f"""
<section class="rules">
  <h2>how the engine drives this</h2>
  <ul>
    <li><b>Every turn ends with a status.</b> Worker → <code>completed</code>/<code>work_remaining</code>;
        input → <code>satisfied</code>/<code>minor_only</code>/<code>needs_work</code>;
        manager → <code>continue</code>/<code>ask_owner</code>/<code>wind_down</code>/<code>complete</code>.</li>
    <li><b>Repeat-in-place.</b> An agent may repeat its own turn up to <code>maxRepeatTurns</code>
        (worker 3, others 1) while its status says "not done" — a worker finishes its todos before critics run.</li>
    <li><b>Input retirement.</b> <code>satisfied</code> → dropped immediately;
        <code>minor_only</code> → {b.get('minorOnlyExtraSteps', 2)} more runs then dropped;
        with &lt;{pct}% of the turn limit left, all <code>minor_only</code> agents are dropped at once.</li>
    <li><b>Early finish.</b> When no input agent is still <code>needs_work</code>, the manager may go straight to wind-down.</li>
    <li><b>Owner asks.</b> A manager <code>ask_owner</code> pauses the loop and pings the owner in Telegram (off-budget).</li>
    <li><b>Budget.</b> {s['turnLimit']} shared main-phase turns (a round in flight finishes past the limit),
        then a guaranteed wind-down of {s['windDownTotal']} turns.</li>
  </ul>
</section>"""


# ── little builders ───────────────────────────────────────────────────────────
def _gate(tag: str, text: str) -> str:
    return f'<div class="gate"><span class="gtag">{_esc(tag)}</span><span class="gtext">{_esc(text)}</span></div>'


def _arrow(label: str = "") -> str:
    lab = f'<span class="alab">{_esc(label)}</span>' if label else ""
    return f'<div class="arrow">↓{lab}</div>'


def _chip(k: str, v: str) -> str:
    return f'<span class="chip"><span class="ck">{_esc(k)}</span>{_esc(v)}</span>'


def _snip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _k(n: Any) -> str:
    try:
        return f"{int(n) // 1000}k"
    except Exception:
        return str(n)


def _esc(s: Any) -> str:
    return _html.escape(str(s if s is not None else ""))


# ── page shell (theme-aware, self-contained) ─────────────────────────────────
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --bg:#f6f7f9; --card:#fff; --ink:#1a1d21; --sub:#5b6470; --line:#e2e6ea;
  --mgr:#7c5cff; --wrk:#0ea5a5; --inp:#e8863b; --loop:#3b82f6;
  --mgr-bg:#f1edff; --wrk-bg:#e6f7f7; --inp-bg:#fdefe3; --loop-bg:#e8f1ff;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0e1116; --card:#171b21; --ink:#e6e9ee; --sub:#9aa4b2; --line:#262c35;
  --mgr-bg:#211a3d; --wrk-bg:#0f2b2b; --inp-bg:#2e2013; --loop-bg:#122238;
}}
:root[data-theme=dark]{--bg:#0e1116;--card:#171b21;--ink:#e6e9ee;--sub:#9aa4b2;--line:#262c35;
  --mgr-bg:#211a3d;--wrk-bg:#0f2b2b;--inp-bg:#2e2013;--loop-bg:#122238;}
:root[data-theme=light]{--bg:#f6f7f9;--card:#fff;--ink:#1a1d21;--sub:#5b6470;--line:#e2e6ea;
  --mgr-bg:#f1edff;--wrk-bg:#e6f7f7;--inp-bg:#fdefe3;--loop-bg:#e8f1ff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  padding:28px 16px;}
.wrap{max-width:760px;margin:0 auto}
header{margin-bottom:14px}
.eyebrow{font-size:11px;letter-spacing:.18em;color:var(--sub);font-weight:700}
h1{margin:.1em 0 .35em;font-size:26px;letter-spacing:-.01em}
.goal{margin:0 0 14px;padding:10px 14px;background:var(--card);border:1px solid var(--line);
  border-left:3px solid var(--mgr);border-radius:8px;color:var(--ink);font-size:14.5px}
.goal .star{color:var(--mgr);font-weight:700;margin-right:4px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--line);
  border-radius:999px;padding:4px 11px;font-size:12.5px;color:var(--ink)}
.chip .ck{color:var(--sub);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.legend{display:flex;flex-wrap:wrap;gap:16px;margin:8px 2px 18px;color:var(--sub);font-size:12.5px}
.lg{display:inline-flex;align-items:center;gap:6px}
.dot{width:10px;height:10px;border-radius:3px;display:inline-block}
.dot.mgr{background:var(--mgr)}.dot.wrk{background:var(--wrk)}.dot.inp{background:var(--inp)}.dot.loop{background:var(--loop)}
.banner{border-radius:8px;padding:10px 14px;margin:0 0 14px;font-size:13px}
.banner ul{margin:6px 0 0;padding-left:18px}
.banner.err{background:#fdecec;border:1px solid #f5b5b5;color:#8a1f1f}
.banner.warn{background:#fff8e6;border:1px solid #f0dca0;color:#6b520e}
@media (prefers-color-scheme:dark){.banner.err{background:#2a1414;border-color:#5a2020;color:#f0b4b4}
  .banner.warn{background:#241f10;border-color:#4a3f18;color:#e8d38a}}
.flow{display:flex;flex-direction:column;align-items:stretch}
.gate{background:var(--card);border:1px dashed var(--line);border-radius:10px;padding:10px 14px;
  display:flex;flex-direction:column;gap:2px}
.gtag{font-size:11px;font-weight:800;letter-spacing:.06em;color:var(--sub)}
.gtext{font-size:13px}
.arrow{text-align:center;color:var(--sub);font-size:17px;padding:3px 0;display:flex;
  align-items:center;justify-content:center;gap:8px}
.arrow .alab{font-size:11px;letter-spacing:.03em}
.round{border:1px solid var(--line);border-radius:12px;padding:12px;margin:2px 0;background:rgba(127,127,127,.04)}
.round-tag{font-size:11px;font-weight:700;letter-spacing:.05em;color:var(--sub);margin-bottom:8px;text-align:center}
.block{background:var(--card);border:1px solid var(--line);border-left-width:4px;border-radius:10px;
  padding:10px 13px;margin:0 auto;max-width:560px;width:100%}
.block.mgr{border-left-color:var(--mgr);background:var(--mgr-bg)}
.block.wrk{border-left-color:var(--wrk);background:var(--wrk-bg)}
.block.inp{border-left-color:var(--inp);background:var(--inp-bg)}
.block.loop{border-left-color:var(--loop);background:var(--loop-bg)}
.bhead{display:flex;align-items:center;gap:8px;margin-bottom:5px}
.badge{font-size:10px;font-weight:800;letter-spacing:.06em;padding:2px 7px;border-radius:5px;color:#fff}
.badge.mgr{background:var(--mgr)}.badge.wrk{background:var(--wrk)}.badge.inp{background:var(--inp)}.badge.loop{background:var(--loop)}
.bid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:700;font-size:13.5px}
.rep{margin-left:auto;font-size:11px;color:var(--sub);font-weight:700}
.bpers{font-size:13px;color:var(--ink);opacity:.92}
.bgoal{font-size:12.5px;color:var(--sub);margin-top:3px}
.bname{font-weight:700;font-size:14px;margin-bottom:2px}
.subchain{font-family:ui-monospace,monospace;font-size:12px;color:var(--sub);margin-top:5px}
.submeta{font-size:11px;color:var(--sub);margin-top:3px}
.tools{margin-top:6px;display:flex;gap:5px;flex-wrap:wrap}
.tool{font-size:10.5px;background:rgba(127,127,127,.16);border-radius:4px;padding:1px 6px;color:var(--ink)}
.empty{color:var(--sub);text-align:center;padding:10px}
.rules{margin-top:22px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px}
.rules h2{font-size:14px;margin:0 0 8px}
.rules ul{margin:0;padding-left:18px}
.rules li{margin:5px 0;font-size:13px;color:var(--ink)}
.rules code{background:rgba(127,127,127,.16);border-radius:4px;padding:1px 5px;font-size:12px}
</style></head>
<body><div class="wrap">__BODY__</div></body></html>"""
