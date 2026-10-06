"""Listen for Telegram updates and route replies into sessions."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

SID_RE = re.compile(r"\[(S-[A-Za-z0-9_-]+?-p\d+)")

# --- Conversational coordinator bridge -------------------------------------
# Plain (non-command, non-reply) messages from an allowlisted chat are routed
# to a headless Claude "coordinator brain" running --resume on a fixed session
# id, so Tim and Ed share one persistent context. The reply is posted back to
# the chat. This is what makes "just write to the bot and it answers" work;
# the swarm's native model is command-only (/sessions, /say, replies).
_CLAUDE_BIN = "claude"
# Chat turns are conversational, not agentic: no repo exploration (keeps replies
# ~5-15s instead of minutes). For real work, use /say into a swarm session.
#
# Per-project: the system prompt is generated from the owning project so each
# bot speaks as that project's coordinator. 'computation' gets a prompt
# equivalent to the historical hardcoded one (see _COMPUTATION_PROMPT below).
_COMPUTATION_PROMPT = (
    "You are the coordinator for Tim & Ed's 'computation' project (building a LAN "
    "for decentralised LLM inference), reachable in their Telegram group. Reply "
    "concisely and conversationally (1-6 sentences). Do NOT read files, run "
    "commands, or use tools unless the user explicitly asks you to perform an "
    "action — otherwise just answer directly."
)


def _chat_system_prompt(proj) -> str:
    """Project-generic conversational system prompt.

    'computation' keeps its historical wording verbatim; every other project
    gets an equivalent prompt with its display name filled in. Same safety
    posture for all: conversational, 1-6 sentences, no tool use unless asked.
    """
    if proj is not None and getattr(proj, "slug", None) == "computation":
        return _COMPUTATION_PROMPT
    name = getattr(proj, "display_name", None) or getattr(proj, "slug", None) or "this"
    return (
        f"You are the coordinator for the '{name}' project, reachable over "
        "Telegram. Reply concisely and conversationally (1-6 sentences). Do NOT "
        "read files, run commands, or use tools unless the user explicitly asks "
        "you to perform an action — otherwise just answer directly."
    )
_CHAT_DISALLOWED_TOOLS = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "NotebookEdit",
]
_CHAT_TIMEOUT = 150  # seconds; a conversational turn should take ~5-15s


def _claude_env() -> dict:
    """Env for the claude subprocess WITHOUT proxy vars.

    The worker routes its own egress through the Xray/WARP proxy so it can reach
    the DPI-blocked Telegram API. But Anthropic is reachable on the direct route
    and the proxy is a slow bottleneck (claude turns took ~2min through it vs ~5s
    direct). So strip the proxy for claude — it talks to Anthropic directly.
    """
    env = dict(os.environ)
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
              "HTTP_PROXY", "http_proxy"):
        env.pop(k, None)
    return env
# Stable per-(project, chat) session id (valid UUID). Keyed by both slug and
# chat so each project's bot keeps its own brain even when two projects share a
# chat (e.g. a DM). Derived deterministically below.
_CHAT_SESSION_NS = "c0a70000-0000-4000-8000-0000000000"
_CHAT_LOCKS: dict[str, threading.Lock] = {}


def _chat_session_id(slug: str, chat_id: str) -> str:
    # Stable valid-UUID session id per (project, chat). Uses hashlib (NOT builtin
    # hash(), which is salted per-process and would break continuity across
    # restarts). For the historical 'computation' chat we preserve the legacy
    # single-arg derivation so its existing --resume session id is unchanged.
    import hashlib
    key = chat_id if slug == "computation" else f"{slug}:{chat_id}"
    suffix = hashlib.sha1(key.encode()).hexdigest()[:2]
    return _CHAT_SESSION_NS + suffix


def _brain_cwd(slug: str) -> str:
    """Isolated working dir for the headless brain's `claude -p` turns.

    The brain is tool-less (no Bash/Read/Write), so its cwd is cosmetic — but
    Claude Code keys its per-project session store on the cwd. Running the brain
    in the project's real repo_path made its deterministic `c0a70000-*` resume
    sessions land in the SAME store a human's interactive `claude` uses there,
    causing "Session ID ... already in use" collisions (2026-06-26 incident).
    A dedicated per-slug dir keeps the brain's sessions fully isolated.
    """
    import os
    from pathlib import Path
    # Install-relative, not a hardcoded ~/bot-swarm: the worker home is the repo
    # root two levels above this package (…/worker/bot_squad_worker/), overridable
    # via BOT_SQUAD_HOME — mirrors tg_user.py / xray_resolve.py / the tracking_ui
    # app home resolution, so a relocated or repackaged install works and a fresh
    # clone doesn't ship a dead absolute path.
    home = Path(os.environ.get(
        "BOT_SQUAD_HOME", str(Path(__file__).resolve().parents[2])))
    base = home / "data" / "_brain_cwd"
    d = base / (slug or "default")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return str(base)
    return str(d)


def _run_coordinator_turn(cfg, token: str, chat_id: str, cwd: str, sid: str,
                          prompt: str, system_prompt: str) -> None:
    """Run one headless Claude turn and post the reply. Runs in a thread."""
    lock = _CHAT_LOCKS.setdefault(sid, threading.Lock())
    with lock:  # serialize turns per (project,chat) so --resume isn't concurrent
        # Keep the 'typing…' indicator alive for the whole turn (it expires ~5s).
        done = threading.Event()

        def _heartbeat():
            while not done.wait(4):
                _send_typing(cfg, token, chat_id)

        threading.Thread(target=_heartbeat, daemon=True).start()
        cmd_base = [_CLAUDE_BIN, "-p", "--output-format", "text",
                    "--system-prompt", system_prompt,
                    "--disallowed-tools", *_CHAT_DISALLOWED_TOOLS,
                    "--dangerously-skip-permissions"]
        try:
            cenv = _claude_env()
            r = subprocess.run(cmd_base + ["--resume", sid], cwd=cwd, env=cenv,
                               capture_output=True, text=True, timeout=_CHAT_TIMEOUT,
                               input=prompt)
            if r.returncode != 0:  # session doesn't exist yet -> create it
                r = subprocess.run(cmd_base + ["--session-id", sid], cwd=cwd, env=cenv,
                                   capture_output=True, text=True, timeout=_CHAT_TIMEOUT,
                                   input=prompt)
            out = (r.stdout or r.stderr or "(no output)").strip()
        except subprocess.TimeoutExpired:
            out = "Coordinator timed out."
        except Exception as e:  # noqa: BLE001
            out = f"Coordinator error: {e}"
        finally:
            done.set()
        log.info("tg chat: posting reply to chat %s (len=%d)", chat_id, len(out))
        _notify(cfg, token, chat_id, out[:3900])


def _handle_chat(cfg, proj, chat_id: str, msg: dict) -> dict:
    """Route a plain message to the headless coordinator brain for ``proj``.

    ``proj`` is the project that owns the bot which received this update; its
    repo_path is the brain cwd and its name shapes the system prompt.
    """
    if (msg.get("from") or {}).get("is_bot"):
        return {"ok": True, "action": "skip", "reason": "from bot"}
    text = (msg.get("text") or "").strip()
    if not text:
        return {"ok": True, "action": "skip", "reason": "no text"}
    # Strip a leading @botname mention if present.
    text = re.sub(r"^@\w+\s+", "", text)
    slug = getattr(proj, "slug", "") if proj is not None else ""
    cwd = _brain_cwd(slug)  # isolated store — never collide with interactive claude (see _brain_cwd)
    token = cfg.bot_token_for(slug) if slug else cfg.tg_bot_token
    system_prompt = _chat_system_prompt(proj)
    sender = (msg.get("from") or {}).get("first_name") or "teammate"
    sid = _chat_session_id(slug, chat_id)
    prompt = f"[Telegram message from {sender}]: {text}"
    # Instant ack — a headless coordinator turn takes ~20-40s; without this the
    # user thinks the bot is dead and gives up before the reply lands.
    _send_typing(cfg, token, chat_id)
    log.info("tg chat: dispatching turn for %s chat %s (sid %s)", slug, chat_id, sid)
    threading.Thread(
        target=_run_coordinator_turn,
        args=(cfg, token, chat_id, cwd, sid, prompt, system_prompt),
        daemon=True,
    ).start()
    return {"ok": True, "action": "chat", "sid": sid}


# ===========================================================================
# Conversational COORDINATOR BRAIN (tg_authz projects only).
#
# Replaces the tool-less _handle_chat for tg_authz=True projects (swarmdev).
# Tim DMs in natural language; the brain converses AND acts on his behalf via a
# STRUCTURED-PROPOSAL mechanism (not MCP — simpler, robust, testable):
#
#   - The brain runs `claude -p --resume` (per project,chat) with Bash/Edit/
#     Write/WebFetch/WebSearch DISALLOWED. It is instructed to converse, and
#     when it decides to act, append ONE fenced ```swarm-action {json}``` block.
#   - The listener parses at most ONE such block, re-derives the Principal from
#     the message ENVELOPE (NOT the LLM output), and runs the SAME authz broker
#     (tg_authz.authorize) used by the slash layer:
#       * allow + internal action  → A.dispatch(...) now, then ONE follow-up
#         brain turn feeding the result back so it reports naturally.
#       * needs_confirm (OUTWARD)  → write a tg_pending record, do NOT execute;
#         the brain's NL reply asks Tim to confirm; Tim's "yes" hits the existing
#         Phase-2 awaiting-confirm branch → executes.
#       * deny                     → relay the reason in the reply.
#   - The action block is STRIPPED from what Tim sees; Tim gets the NL text + a
#     short "(spun up X / checked Y)" note.
#   - Cap: 1 action + 1 follow-up per inbound message (bounds cost + loops).
#
# Authority is ALWAYS code-enforced at the authorize() boundary against the
# envelope Principal — the LLM is never trusted to self-authorize.
# ===========================================================================

# Brain action name -> (authz action name, dispatch action name). The authz
# layer (tg_authz._ACTION_CAP) is keyed on logical names (status/sessions/spawn/
# inject/deploy); A.dispatch is keyed on the registry names (status_summary/
# spawn_session/...). This table maps the names the brain emits to both. Any
# action NOT in this table is rejected (fail closed) before authorize even runs.
_BRAIN_ACTIONS: dict[str, tuple[str, str]] = {
    "status_summary": ("status", "status_summary"),
    "list_sessions": ("status", "list_sessions"),
    "spawn_session": ("spawn", "spawn_session"),
    "inject_input": ("inject", "inject_input"),
    "peer_send": ("dispatch", "peer_send"),
    "deploy": ("deploy", "deploy"),
}

_ACTION_BLOCK_RE = re.compile(r"```swarm-action\s*\n(.*?)\n```", re.DOTALL)


def _parse_action_block(text: str) -> tuple[Optional[dict], str]:
    """Extract at most ONE ```swarm-action``` block from brain output.

    Returns ``(action_or_None, visible_text)`` where ``visible_text`` has the
    block STRIPPED (what Tim sees). ``action`` is ``{"action": str, "params":
    dict}`` or None when there is no block / it is malformed (→ pure
    conversation). Only the FIRST block is honored; a second is left in the
    stripped text as harmless prose (never parsed → never executed).
    """
    m = _ACTION_BLOCK_RE.search(text)
    if not m:
        return None, text.strip()
    visible = (text[:m.start()] + text[m.end():]).strip()
    raw = m.group(1).strip()
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None, visible  # malformed JSON → treat as pure conversation
    if not isinstance(obj, dict) or not isinstance(obj.get("action"), str):
        return None, visible
    params = obj.get("params")
    if not isinstance(params, dict):
        params = {}
    return {"action": obj["action"], "params": params}, visible


_BRAIN_DISALLOWED_TOOLS = [
    "Bash", "Edit", "Write", "WebFetch", "WebSearch",
    "Read", "Glob", "Grep", "Task", "NotebookEdit",
]
_BRAIN_TIMEOUT = 180  # seconds; a brain turn may call status + reason over it

# Action names the brain may use, with their exact params, injected into the
# system prompt so it emits dispatch-ready JSON (slug-keyed, not "swarm"-keyed).
_BRAIN_ACTION_SPEC = (
    "Actions and their params (ALWAYS use \"slug\":\"{slug}\"):\n"
    "- status_summary {{\"slug\":\"{slug}\"}} — live swarm state\n"
    "- list_sessions {{\"slug\":\"{slug}\"}} — list sessions\n"
    "- spawn_session {{\"slug\":\"{slug}\",\"window\":\"<short-label>\","
    "\"role\":\"<role>\"}} — spin up a worker\n"
    "- inject_input {{\"sid\":\"<S-...>\",\"text\":\"<message>\"}} — steer a worker\n"
    "- peer_send {{...}} — message between workers\n"
    "- deploy {{\"slug\":\"{slug}\",\"target\":\"<target>\"}} — OUTWARD/irreversible"
)


def _brain_context_block(swarm_state: str, project_state: str,
                         coord_context: str = "") -> str:
    """The brain's SEEDED CONTEXT — live swarm state, project state, and the
    cross-instance blackboard (what Tim discussed with the terminal coordinator).

    This rides STDIN with the user message, NOT argv (971d1a75). It used to be
    baked into the --system-prompt, and --system-prompt is a command-line argument:
    /proc/<pid>/cmdline is world-readable and this host has other local users, so
    every one of Tim's cross-instance discussions was readable with a plain `ps`.
    The payload fix (ec71b804) could not cover it — `claude -p` has exactly one
    stdin and the user message already owned it — and this CLI (2.1.207) has no
    --system-prompt-file to move it to.

    The way out is that it never needed to be a system prompt at all. What must be
    a system prompt is the INSTRUCTIONS (below); this is DATA, and data belongs
    with the message. So the split is: static instructions -> argv (they leak
    nothing), live context -> stdin (private). No CLI capability required.
    """
    cross = ""
    if coord_context:
        cross = (
            "=== Recent cross-instance context (you + the terminal coordinator "
            "share this) ===\n"
            f"{coord_context}\n"
        )
    return (
        "=== LIVE SWARM STATE (swarmdev) ===\n"
        f"{swarm_state}\n"
        "=== PROJECT STATE (claude-memory/projects/swarmdev/current-state.md) ===\n"
        f"{project_state}\n"
        f"{cross}"
        "Shared memory index: ~/claude-memory/manifest.md\n\n"
    )


def _brain_system_prompt(slug: str, coord_sid: Optional[str] = None,
                         chat_id: Optional[str] = None) -> str:
    """STATIC instructions for the coordinator brain — safe to pass in argv.

    Deliberately carries NO live state: the swarm/project/blackboard blocks moved
    to _brain_context_block() and ride stdin, because this string becomes a
    world-readable command-line argument (971d1a75). Keep it that way — anything
    derived from the owner's messages, memory, or transcripts must NOT be added here.

    ``coord_sid`` (a session uuid) and ``chat_id`` are identifiers, not content,
    and the relay instruction is useless without them.
    """
    spec = _BRAIN_ACTION_SPEC.format(slug=slug)
    if coord_sid:
        relay = (
            "=== REACHING THE LIVE TERMINAL COORDINATOR ===\n"
            f"You CAN reach the live terminal-coordinator session directly: it is "
            f"session {coord_sid} (the working session that builds/operates the "
            "swarm). When Tim asks to talk to the coordinator, or wants something "
            "only the live working session would know or that needs real action "
            "beyond your own tools, RELAY to it with an inject_input action:\n"
            '```swarm-action\n{"action":"inject_input","params":{"sid":"'
            + coord_sid + '","text":"[TG relay from Tim — reply to him directly via '
            "Telegram chat " + (chat_id or "?") + ']: <Tim\'s message>"}}\n```\n'
            "Then tell Tim you've relayed it and the coordinator will reply here "
            "shortly. Do NOT claim you can't reach the coordinator — you can, via "
            "this relay.\n\n"
        )
    else:
        relay = (
            "=== TERMINAL COORDINATOR ===\n"
            "The live terminal-coordinator session isn't currently active, so you "
            "can't relay to it right now; leave a note in shared memory and tell "
            "Tim it'll be picked up on the next sync.\n\n"
        )
    return (
        "You are the swarmdev coordinator, consistent with Tim's terminal "
        "coordinator via shared memory. Tim (admin) is talking to you over "
        "Telegram. Act on his behalf; only confirm before outward/irreversible "
        "actions (deploy/send/spend).\n\n"
        "Converse naturally and concisely (Telegram DM, 1-8 sentences). When you "
        "decide to ACT on the swarm, append EXACTLY ONE fenced block at the very "
        "END of your reply:\n"
        "```swarm-action\n{\"action\":\"<name>\",\"params\":{...}}\n```\n"
        f"{spec}\n"
        "ALWAYS emit the block when you intend an action — even outward ones. For "
        "OUTWARD actions (deploy/send/spend), ALSO ask Tim to confirm in your "
        "natural-language text (the system holds it pending his 'yes'). At most "
        "ONE block per reply; omit it entirely to just converse.\n\n"
        f"{relay}"
        "The live swarm state, project state and cross-instance context arrive "
        "with Tim's message, not here."
    )


# Cap the cross-instance context block injected into the prompt (chars).
_COORD_CONTEXT_MAX = 3000
_COORD_CONTEXT_N = 25


def _seed_coord_context(cfg, slug: str) -> str:
    """Compact recent cross-instance blackboard for ``slug`` (never raises).

    Reads the last ~25 entries from the shared coord-context blackboard and
    renders them as compact ``[instance/speaker] text`` lines, capped to
    ~3KB (oldest lines dropped first so the freshest context survives).
    """
    try:
        from bot_squad_worker import coord_context as CC
        entries = CC.read_recent(cfg.data_dir, slug, n=_COORD_CONTEXT_N)
    except Exception:  # noqa: BLE001
        return ""
    if not entries:
        return ""
    lines = []
    for e in entries:
        inst = e.get("instance", "?")
        spk = e.get("speaker", "?")
        txt = (e.get("text") or "").strip().replace("\n", " ")
        lines.append(f"[{inst}/{spk}] {txt}")
    block = "\n".join(lines)
    if len(block) > _COORD_CONTEXT_MAX:
        # Keep the freshest (tail) within the cap.
        block = "…\n" + block[-_COORD_CONTEXT_MAX:]
    return block


_MEMORY_ROOT = Path.home() / "claude-memory"
_PROJECT_STATE_MAX = 6000  # chars; trim current-state.md so the prompt stays small


def _seed_swarm_state(slug: str) -> str:
    """Compact live swarm state via status_summary (best-effort, never raises)."""
    try:
        from bot_squad_worker import actions as A
        res = A.dispatch("status_summary", {"slug": slug})
        return json.dumps(res, default=str)[:2000]
    except Exception as e:  # noqa: BLE001
        return f"(status unavailable: {e})"


def _coordinator_sid(slug: str) -> Optional[str]:
    """SID of the live terminal-coordinator session for ``slug`` (or None).

    Lets the TG brain RELAY a message to the working coordinator session via
    inject_input — that's the live channel between the bot and the terminal
    coordinator (they otherwise only sync through shared memory).
    """
    try:
        from bot_squad_worker import actions as A
        res = A.dispatch("list_sessions", {"slug": slug})
        for s in (res or {}).get("sessions", []):
            if s.get("window") == "coordinator" and s.get("status") == "active":
                return s.get("sid")
    except Exception:  # noqa: BLE001
        pass
    return None


def _seed_project_state() -> str:
    """Contents of current-state.md, trimmed. Read-only; never raises."""
    p = _MEMORY_ROOT / "projects" / "swarmdev" / "current-state.md"
    try:
        txt = p.read_text()
    except OSError:
        return "(current-state.md unavailable)"
    if len(txt) > _PROJECT_STATE_MAX:
        txt = txt[:_PROJECT_STATE_MAX] + "\n…(trimmed)"
    return txt


def _claude_brain_turn(cwd: str, sid: str, prompt: str, system_prompt: str) -> str:
    """Run ONE headless brain turn (--resume, fallback --session-id). Returns
    raw stdout text. Never raises — returns an error string on failure."""
    cmd_base = [_CLAUDE_BIN, "-p", "--output-format", "text",
                "--system-prompt", system_prompt,
                "--disallowed-tools", *_BRAIN_DISALLOWED_TOOLS,
                "--dangerously-skip-permissions"]
    cenv = _claude_env()
    try:
        r = subprocess.run(cmd_base + ["--resume", sid], cwd=cwd, env=cenv,
                           capture_output=True, text=True, timeout=_BRAIN_TIMEOUT,
                           input=prompt)
        if r.returncode != 0:  # session doesn't exist yet -> create it
            r = subprocess.run(cmd_base + ["--session-id", sid], cwd=cwd, env=cenv,
                               capture_output=True, text=True, timeout=_BRAIN_TIMEOUT,
                               input=prompt)
        return (r.stdout or r.stderr or "(no output)").strip()
    except subprocess.TimeoutExpired:
        return "Coordinator timed out."
    except Exception as e:  # noqa: BLE001
        return f"Coordinator error: {e}"


def _run_brain_turn(cfg, proj, token: str, chat_id: str, cwd: str, sid: str,
                    msg: dict, prompt: str, system_prompt: str,
                    inbound_text: str = "") -> None:
    """Full conversational-brain turn for a tg_authz project. Runs in a thread.

    Flow: brain turn → parse ONE action block → re-derive Principal from the
    ENVELOPE → authorize → {dispatch+follow-up | pending | deny} → post the
    natural-language reply (block stripped) + a short action note → append the
    inbound (Tim) + reply (coord) to the shared coord-context blackboard.
    """
    from bot_squad_worker import actions as A
    from bot_squad_worker import tg_authz as Z
    from bot_squad_worker import tg_pending as P

    lock = _CHAT_LOCKS.setdefault(sid, threading.Lock())
    with lock:  # serialize turns per (project,chat) so --resume isn't concurrent
        done = threading.Event()

        def _heartbeat():
            while not done.wait(4):
                _send_typing(cfg, token, chat_id)

        threading.Thread(target=_heartbeat, daemon=True).start()
        visible = ""
        try:
            raw = _claude_brain_turn(cwd, sid, prompt, system_prompt)
            action, visible = _parse_action_block(raw)
            note = ""

            if action is not None:
                note = _handle_brain_action(
                    cfg, proj, token, chat_id, cwd, sid, msg, system_prompt,
                    action,
                )
                # _handle_brain_action may REPLACE visible (follow-up turn) by
                # returning a 2-tuple; normalize.
                if isinstance(note, tuple):
                    visible, note = note
        finally:
            done.set()

        body = visible if visible else "(done)"
        if note:
            body = f"{body}\n{note}"
        log.info("tg brain: posting reply to chat %s (len=%d)", chat_id, len(body))
        _notify(cfg, token, chat_id, body[:3900])

        # Share this exchange on the cross-instance blackboard (best-effort).
        # ``visible`` is the NL reply with the swarm-action block already
        # STRIPPED — we never leak the raw action block into shared memory.
        _append_coord_context(cfg, proj, inbound_text, visible)


def _append_coord_context(cfg, proj, inbound_text: str, reply_text: str) -> None:
    """Append the inbound (Tim) + reply (coord) to the shared blackboard.

    instance="tg-brain". Never raises (coord_context swallows IO; we guard the
    rest). Skips empties so we don't write blank lines."""
    try:
        from bot_squad_worker import coord_context as CC
        slug = getattr(proj, "slug", "") or ""
        if inbound_text:
            CC.append(cfg.data_dir, slug, "tg-brain", "tim", inbound_text)
        if reply_text:
            CC.append(cfg.data_dir, slug, "tg-brain", "coord", reply_text)
    except Exception:  # noqa: BLE001 — shared memory must never break a turn
        pass


def _handle_brain_action(cfg, proj, token, chat_id, cwd, sid, msg,
                         system_prompt, action):
    """Authorize + execute ONE brain-proposed action. Returns either a short
    note string, or a ``(new_visible, note)`` tuple when a follow-up brain turn
    reworded the reply around the action result.

    Authority is re-derived from the ENVELOPE here — the LLM's output is only a
    PROPOSAL; this function is the deterministic broker boundary.
    """
    from bot_squad_worker import actions as A
    from bot_squad_worker import tg_authz as Z
    from bot_squad_worker import tg_pending as P

    name = action.get("action", "")
    params = dict(action.get("params") or {})
    mapping = _BRAIN_ACTIONS.get(name)
    if mapping is None:
        # Unknown/unsupported proposal: fail closed, do nothing, say so quietly.
        log.warning("tg brain: unknown action proposed: %r", name)
        return "(ignored an unsupported action request)"

    authz_action, dispatch_action = mapping
    principal = Z.resolve_identity(cfg, msg, proj)

    # Build the authorize() params. status/spawn/deploy carry a slug for the
    # project-scope check; inject carries the target session's owner.
    authz_params: dict = {}
    if "slug" in params:
        authz_params["slug"] = params["slug"]
    if authz_action == "inject":
        authz_params["owner"] = _owner_for_sid(
            cfg, getattr(proj, "slug", ""), params.get("sid", ""))

    d = Z.authorize(principal, authz_action, authz_params, data_dir=cfg.data_dir)

    if d.denied:
        return f"(couldn't: {d.reason})"

    if d.needs_confirm:
        # OUTWARD action: stash a pending; do NOT execute. The brain's NL reply
        # already asked Tim to confirm; his "yes" hits the Phase-2 branch.
        dispatch_params = _finalize_outward_params(
            dispatch_action, params, principal)
        rec = P.put(cfg.data_dir, chat_id, principal.from_id, dispatch_action,
                    dispatch_params, principal.role)
        log.info("tg brain: pending %s for %s (token=%s)",
                 dispatch_action, principal.from_id, rec.token)
        return f"(awaiting your confirmation — reply 'yes', expires 5 min)"

    # allow + INTERNAL action → execute now, then ONE follow-up brain turn to
    # report the result naturally. Cap: exactly 1 action + 1 follow-up.
    try:
        result = A.dispatch(dispatch_action, params)
    except A.ActionError as e:
        return f"(action {dispatch_action} failed: {e})"
    except Exception as e:  # noqa: BLE001
        return f"(action {dispatch_action} error: {e})"

    follow_prompt = (
        f"[system] You ran the action `{dispatch_action}` on Tim's behalf. "
        f"Result (JSON):\n{json.dumps(result, default=str)[:2500]}\n"
        "Report this to Tim naturally and concisely in plain language. Do NOT "
        "emit another swarm-action block."
    )
    reworded = _claude_brain_turn(cwd, sid, follow_prompt, system_prompt)
    follow_action, follow_visible = _parse_action_block(reworded)
    # The follow-up should not act; if it tried, ignore the block (already
    # stripped from follow_visible) — we never dispatch a follow-up action.
    note = f"({_action_note(dispatch_action, params)})"
    return (follow_visible or "(done)", note)


def _finalize_outward_params(dispatch_action: str, params: dict,
                             principal) -> dict:
    """Complete the dispatch params for an OUTWARD action before stashing it as
    pending, mirroring the slash /deploy path so confirmation runs verbatim."""
    p = dict(params)
    if dispatch_action == "deploy":
        p.setdefault("target", p.get("target", ""))
        p["reason"] = f"telegram coordinator brain by {principal.from_id}"
        p["requested_by"] = f"tg:{principal.from_id}"
    return p


def _action_note(dispatch_action: str, params: dict) -> str:
    """Short human note appended after an executed internal action."""
    if dispatch_action == "status_summary":
        return "checked swarm status"
    if dispatch_action == "list_sessions":
        return "listed sessions"
    if dispatch_action == "spawn_session":
        return f"spun up {params.get('role') or params.get('window') or 'a worker'}"
    if dispatch_action == "inject_input":
        return f"steered {params.get('sid', 'a worker')}"
    if dispatch_action == "peer_send":
        return "relayed a peer message"
    return f"ran {dispatch_action}"


def _handle_brain_chat(cfg, proj, chat_id: str, msg: dict) -> dict:
    """Route a plain message to the CONVERSATIONAL COORDINATOR BRAIN (tg_authz).

    Mirrors _handle_chat's threading/ack but uses the acting brain pipeline.
    """
    if (msg.get("from") or {}).get("is_bot"):
        return {"ok": True, "action": "skip", "reason": "from bot"}
    text = (msg.get("text") or "").strip()
    if not text:
        return {"ok": True, "action": "skip", "reason": "no text"}
    text = re.sub(r"^@\w+\s+", "", text)
    slug = getattr(proj, "slug", "") if proj is not None else ""
    cwd = _brain_cwd(slug)  # isolated store — never collide with interactive claude (see _brain_cwd)
    token = _token_for_proj(cfg, proj)
    sender = (msg.get("from") or {}).get("first_name") or "Tim"
    sid = _chat_session_id(slug, chat_id)
    # Seed the STDIN payload — not the system prompt — with live swarm state, project
    # state and the cross-instance blackboard (what Tim discussed with the terminal
    # coord). The system prompt is a world-readable argv element (971d1a75), and this
    # is Tim's conversation content; it is data, so it travels with the message.
    prompt = (_brain_context_block(_seed_swarm_state(slug), _seed_project_state(),
                                   _seed_coord_context(cfg, slug))
              + f"[Telegram message from {sender}]: {text}")
    system_prompt = _brain_system_prompt(
        slug, coord_sid=_coordinator_sid(slug), chat_id=str(chat_id))
    _send_typing(cfg, token, chat_id)
    log.info("tg brain: dispatching turn for %s chat %s (sid %s)", slug, chat_id, sid)
    threading.Thread(
        target=_run_brain_turn,
        args=(cfg, proj, token, chat_id, cwd, sid, msg, prompt, system_prompt, text),
        daemon=True,
    ).start()
    return {"ok": True, "action": "chat", "sid": sid}


def _send_typing(cfg, token: str, chat_id: str) -> None:
    """Show a Telegram 'typing…' indicator so the user knows we're working."""
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/sendChatAction"
    try:
        httpx.post(url, data={"chat_id": chat_id, "action": "typing"}, timeout=10)
    except httpx.HTTPError:
        pass


def _last_update_id_path(cfg, slug: str | None = None) -> Path:
    """Per-project getUpdates offset file.

    Projects on the global token (computation) keep the legacy single filename
    ``tg_last_update_id`` so their existing offset survives this refactor — no
    migration, no risk of re-delivering old updates. Projects with their own bot
    token get ``tg_last_update_id-<slug>``.
    """
    base = cfg.data_dir / "_worker"
    if slug and cfg.telegram_bots.get(slug):
        return base / f"tg_last_update_id-{slug}"
    return base / "tg_last_update_id"


def _read_last_update_id(cfg, slug: str | None = None) -> int:
    p = _last_update_id_path(cfg, slug)
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return 0


def _write_last_update_id(cfg, update_id: int, slug: str | None = None) -> None:
    p = _last_update_id_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(update_id))


def poll_updates(cfg, last_update_id: int, timeout: int = 25,
                 token: str | None = None) -> list[dict]:
    """Long-poll TG getUpdates. Returns empty on no-token / network errors.

    ``token`` selects the bot to poll; defaults to the global token so existing
    single-bot callers keep working.
    """
    token = token if token is not None else cfg.tg_bot_token
    if not token:
        return []
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {
        "offset": last_update_id + 1,
        "timeout": timeout,
        "allowed_updates": ["message"],
    }
    try:
        r = httpx.get(url, params=params, timeout=timeout + 5)
        r.raise_for_status()
        return r.json().get("result", [])
    except (httpx.HTTPError, ValueError):
        return []


def extract_reply_target(message: dict) -> Optional[tuple[str, str]]:
    """Extract (sid, text) if this is a reply to a worker notification."""
    reply_to = message.get("reply_to_message")
    if not reply_to:
        return None
    quoted = reply_to.get("text") or ""
    m = SID_RE.match(quoted)
    if not m:
        return None
    return (m.group(1), message.get("text", "").strip())


def extract_slash_command(message: dict) -> Optional[tuple[str, str]]:
    """Extract (cmd, args_str) for a recognized slash command.

    Recognizes the legacy commands (sessions/say/help) plus the Phase-1 directing
    commands (status/spawn) and the Phase-2 confirm-first commands (deploy/confirm).
    status/spawn/deploy/confirm are only ACTED on for tg_authz=True projects
    (gated in _handle_slash); for tg_authz=False projects the existing
    _handle_slash falls through to its ``unknown_cmd`` branch exactly as before
    (it never had these handlers), so behavior there is unchanged.
    """
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split(None, 1)
    cmd = parts[0].lstrip("/").split("@")[0]   # strip @botname if present
    args = parts[1] if len(parts) > 1 else ""
    if cmd not in {"sessions", "say", "help", "status", "spawn", "deploy", "confirm"}:
        return None
    return (cmd, args)


def _is_dm(msg: dict) -> bool:
    """True if this update is a 1:1 chat (DM) with the bot.

    Telegram chat.type is 'private' for DMs; 'group'/'supergroup'/'channel'
    otherwise. DMs are always answered; in groups we only see @mentions /
    replies / commands because group-privacy mode is on (getUpdates filters
    them for us), so no extra mention-parsing is needed here.
    """
    return (msg.get("chat", {}) or {}).get("type") == "private"


def handle_update(cfg, update: dict, proj=None) -> dict:
    """Dispatch one update for the project ``proj`` that owns the receiving bot.

    Routing is by owning bot (``proj``), not a global chat→project map. We still
    authorize: act only on DMs (always) or chats in this project's allowlist
    (its ``tg_chat``). ``proj=None`` preserves the legacy single-bot behavior
    (authorize against every project's tg_chat) for any caller that doesn't
    route by bot.
    """
    msg = update.get("message")
    if not msg:
        return {"ok": True, "action": "skip", "reason": "no message"}

    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))

    # Authorize. DMs are always allowed (the bot answers any direct message).
    # Group/supergroup chatter is only authorized for chats this bot's project
    # lists in tg_chat — and privacy mode already ensures we only receive
    # @mentions/replies/commands in groups, so we don't answer un-addressed
    # group chatter.
    if not _is_dm(msg):
        if proj is not None:
            allowed_chats = {str(proj.tg_chat)} - {"0"}
        else:
            allowed_chats = {str(p.tg_chat) for p in cfg.projects.values()} - {"0"}
        if chat_id not in allowed_chats:
            return {"ok": True, "action": "skip",
                    "reason": f"chat {chat_id} not allowlisted"}

    slash = extract_slash_command(msg)
    if slash:
        return _handle_slash(cfg, proj, chat_id, *slash, msg=msg)

    reply = extract_reply_target(msg)
    if reply:
        return _handle_reply(cfg, proj, chat_id, *reply)

    # Per-from.id rate limit BEFORE the expensive brain spawn. ONLY engaged for
    # tg_authz=True projects — tg_authz=False projects (computation) skip this
    # entirely and reach _handle_chat byte-for-byte as before. Over-limit →
    # cheap canned reply, NO LLM turn (the biggest cost lever).
    # Awaiting-confirm branch (Phase 2). ONLY for tg_authz projects, and placed
    # BEFORE _handle_chat so a plain "yes" is caught as a confirmation of a
    # pending gated action — NOT forwarded to the (attacker-controllable) brain.
    # A different user's "yes" cannot confirm someone else's pending: the pending
    # store is keyed on (chat_id, from_id), so we look up THIS sender's pending,
    # and the confirm helper additionally binds raised_by == confirmer (or admin).
    if _authz_on(proj):
        confirm = _maybe_handle_confirmation(cfg, proj, chat_id, msg)
        if confirm is not None:
            return confirm

    if _authz_on(proj):
        from bot_squad_worker import tg_authz as Z
        from_id = _from_id(msg)
        if not Z.check_turn_rate(cfg.data_dir, from_id):
            _notify(cfg, _token_for_proj(cfg, proj), chat_id,
                    "rate limit reached — try again later")
            return {"ok": True, "action": "rate_limited", "from_id": from_id}

    # Plain message -> coordinator brain. tg_authz projects (swarmdev) get the
    # ACTING conversational brain (parse→authorize→dispatch); everyone else
    # (computation, tg_authz=False) keeps the legacy tool-less _handle_chat
    # byte-for-byte.
    if _authz_on(proj):
        return _handle_brain_chat(cfg, proj, chat_id, msg)
    return _handle_chat(cfg, proj, chat_id, msg)


def _handle_reply(cfg, proj, chat_id: str, sid: str, text: str) -> dict:
    """Find the pane by SID and inject the text."""
    from bot_squad_worker import actions as A
    try:
        result = A.dispatch("inject_input", {"sid": sid, "text": text})
        return {"ok": True, "action": "inject", "sid": sid, "result": result}
    except A.ActionError as e:
        _notify(cfg, _token_for_proj(cfg, proj), chat_id,
                f"❌ session {sid} not active — message dropped")
        return {"ok": False, "action": "inject_failed", "sid": sid, "error": str(e)}


def _token_for_proj(cfg, proj) -> str:
    """Resolve the bot token to reply on for ``proj`` (global if proj=None)."""
    if proj is None:
        return cfg.tg_bot_token
    return cfg.bot_token_for(getattr(proj, "slug", "")) or cfg.tg_bot_token


def _authz_on(proj) -> bool:
    """True iff this project opts into per-user authz (tg_authz=True).

    proj=None (legacy single-bot mode) and tg_authz=False (computation) both
    return False → the new authz/rate-limit/command logic is entirely skipped.
    """
    return bool(getattr(proj, "tg_authz", False))


def _from_id(msg: dict) -> int:
    """TG sender id from the message envelope (0 if absent/malformed)."""
    try:
        return int((msg.get("from") or {}).get("id"))
    except (TypeError, ValueError):
        return 0


def _handle_slash(cfg, proj, chat_id: str, cmd: str, args: str, *,
                  msg: dict | None = None) -> dict:
    """Implement /sessions, /say, /help (+ /status, /spawn when tg_authz).

    For tg_authz=False projects (computation) this is byte-for-byte the legacy
    handler: no identity resolution, no authorize gating, and /status & /spawn
    are simply unrecognized (fall through to ``unknown_cmd``), exactly as before
    this feature existed. ONLY when ``proj.tg_authz`` is True does the per-user
    authz path engage.
    """
    if _authz_on(proj):
        return _handle_slash_authz(cfg, proj, chat_id, cmd, args, msg or {})

    from bot_squad_worker import actions as A, sessions as S
    token = _token_for_proj(cfg, proj)
    if cmd == "sessions":
        # List sessions for the owning project (all projects in legacy mode).
        slugs = [proj.slug] if proj is not None else list(cfg.projects)
        rows: list[str] = []
        for slug in slugs:
            for row in S.list_sessions(cfg, slug):
                rows.append(f"  {row['sid']}  win={row['window']}  status={row['status']}")
        body = "Active sessions:\n" + ("\n".join(rows) if rows else "  (none)")
        _notify(cfg, token, chat_id, body)
        return {"ok": True, "action": "sessions", "count": len(rows)}

    if cmd == "say":
        # /say <sid> <text>
        parts = args.split(None, 1)
        if len(parts) < 2:
            _notify(cfg, token, chat_id, "Usage: /say <sid> <text>")
            return {"ok": False, "action": "say_usage"}
        sid, text = parts[0], parts[1]
        try:
            result = A.dispatch("inject_input", {"sid": sid, "text": text})
            return {"ok": True, "action": "say", "sid": sid, "result": result}
        except A.ActionError as e:
            _notify(cfg, token, chat_id, f"❌ /say failed: {e}")
            return {"ok": False, "action": "say_failed", "error": str(e)}

    if cmd == "help":
        _notify(cfg, token, chat_id,
                "Reply to a notification to inject text into the session.\n"
                "/sessions — list active sessions\n"
                "/say <sid> <text> — direct inject without reply-quoting")
        return {"ok": True, "action": "help"}

    return {"ok": False, "action": "unknown_cmd"}


def _handle_slash_authz(cfg, proj, chat_id: str, cmd: str, args: str,
                        msg: dict) -> dict:
    """Per-user-authorized slash handling — engaged ONLY for tg_authz projects.

    PIVOT 2026-06-25: the conversational coordinator brain is now THE interface.
    The user-facing SLASH CONTROL COMMANDS (/spawn, /deploy, /say, /status,
    /sessions) are NEUTRALIZED here — the brain handles those intents from
    natural conversation. They reply with a one-line pointer to "just ask me".
    Only ``/confirm`` (machinery for the brain's outward proposals) and ``/help``
    remain functional. The Phase-2 awaiting-confirm branch in handle_update is
    untouched, so the brain's confirm-first flow still works.

    Resolves the Principal from the message envelope (NOT chat text), then calls
    tg_authz.authorize() before any A.dispatch (only reached by /confirm now).
    """
    from bot_squad_worker import actions as A, sessions as S
    from bot_squad_worker import tg_authz as Z
    from bot_squad_worker import tg_pending as P
    token = _token_for_proj(cfg, proj)
    principal = Z.resolve_identity(cfg, msg, proj)
    slug = getattr(proj, "slug", "")

    def _denied(decision: "Z.Decision") -> dict:
        _notify(cfg, token, chat_id, f"not authorized: {decision.reason}")
        return {"ok": False, "action": "denied", "cmd": cmd,
                "reason": decision.reason, "from_id": principal.from_id}

    # Neutralized control commands → point the user at conversation. The brain
    # does spawn/deploy/say/status from natural language now.
    if cmd in ("sessions", "status", "say", "spawn", "deploy"):
        _notify(cfg, token, chat_id,
                "Just talk to me normally — tell me what you want and I'll "
                "handle the swarm for you (spin up workers, check status, "
                "deploy, etc.). No commands needed.")
        return {"ok": True, "action": "neutralized_cmd", "cmd": cmd}

    if cmd == "confirm":
        # /confirm [token] — explicit confirmation of THIS sender's pending
        # (the brain's outward proposals). RETAINED machinery.
        token_arg = (args.split() or [""])[0]
        return _execute_confirmation(cfg, proj, chat_id, principal, token=token_arg)

    if cmd == "help":
        _notify(cfg, token, chat_id,
                "I'm the swarmdev coordinator — just message me in plain "
                "English. I'll converse and act on the swarm for you (spin up "
                "workers, check status, deploy with a confirm). "
                "/confirm — confirm a pending outward action.")
        return {"ok": True, "action": "help"}

    return {"ok": False, "action": "unknown_cmd"}


def _handle_slash_authz_legacy(cfg, proj, chat_id: str, cmd: str, args: str,
                               msg: dict) -> dict:
    """RETIRED Phase-1 slash command handlers (kept for reference, not routed).

    These implemented /status, /say, /spawn, /deploy as user-typed commands.
    The PIVOT replaced this UX with the conversational brain; _handle_slash_authz
    no longer calls this. Preserved so the wiring history is legible and tests
    that exercise the broker directly can still reach it if needed.
    """
    from bot_squad_worker import actions as A, sessions as S
    from bot_squad_worker import tg_authz as Z
    from bot_squad_worker import tg_pending as P
    token = _token_for_proj(cfg, proj)
    principal = Z.resolve_identity(cfg, msg, proj)
    slug = getattr(proj, "slug", "")

    def _denied(decision: "Z.Decision") -> dict:
        _notify(cfg, token, chat_id, f"not authorized: {decision.reason}")
        return {"ok": False, "action": "denied", "cmd": cmd,
                "reason": decision.reason, "from_id": principal.from_id}

    if cmd in ("sessions", "status"):
        # /status [slug] — read state for a project; defaults to this project.
        # /sessions stays an alias (read-only) for back-compat.
        target = (args.split() or [slug])[0] if cmd == "status" else slug
        d = Z.authorize(principal, "status", {"slug": target}, data_dir=cfg.data_dir)
        if not d.allowed:
            return _denied(d)
        rows: list[str] = []
        for row in S.list_sessions(cfg, target):
            rows.append(f"  {row['sid']}  win={row['window']}  status={row['status']}")
        body = (f"[{target}] sessions:\n"
                + ("\n".join(rows) if rows else "  (none)"))
        _notify(cfg, token, chat_id, body)
        return {"ok": True, "action": "status", "slug": target, "count": len(rows)}

    if cmd == "say":
        parts = args.split(None, 1)
        if len(parts) < 2:
            _notify(cfg, token, chat_id, "Usage: /say <sid> <text>")
            return {"ok": False, "action": "say_usage"}
        sid, text = parts[0], parts[1]
        # inject — pass owner so the ownership check can scope 'member' injects.
        owner = _owner_for_sid(cfg, slug, sid)
        d = Z.authorize(principal, "inject", {"owner": owner}, data_dir=cfg.data_dir)
        if not d.allowed:
            return _denied(d)
        try:
            result = A.dispatch("inject_input", {"sid": sid, "text": text})
            return {"ok": True, "action": "say", "sid": sid, "result": result}
        except A.ActionError as e:
            _notify(cfg, token, chat_id, f"❌ /say failed: {e}")
            return {"ok": False, "action": "say_failed", "error": str(e)}

    if cmd == "spawn":
        # /spawn <slug> <role>
        parts = args.split()
        if len(parts) < 2:
            _notify(cfg, token, chat_id, "Usage: /spawn <slug> <role>")
            return {"ok": False, "action": "spawn_usage"}
        target, role = parts[0], parts[1]
        d = Z.authorize(principal, "spawn", {"slug": target}, data_dir=cfg.data_dir)
        if not d.allowed:
            return _denied(d)
        try:
            # owner = the directing principal's from.id (design: dispatch sets
            # owner=principal). window defaults to the role for a readable label.
            result = A.dispatch("spawn_session", {
                "slug": target, "window": role, "role": role,
                "owner": str(principal.from_id),
            })
            sid = result.get("sid") if isinstance(result, dict) else None
            _notify(cfg, token, chat_id, f"spawned {role} in {target}: {sid}")
            return {"ok": True, "action": "spawn", "slug": target,
                    "role": role, "sid": sid}
        except A.ActionError as e:
            _notify(cfg, token, chat_id, f"❌ /spawn failed: {e}")
            return {"ok": False, "action": "spawn_failed", "error": str(e)}

    if cmd == "deploy":
        # /deploy <slug> <target> — first OUTWARD (confirm-first) action.
        parts = args.split()
        if len(parts) < 2:
            _notify(cfg, token, chat_id, "Usage: /deploy <slug> <target>")
            return {"ok": False, "action": "deploy_usage"}
        target_slug, deploy_target = parts[0], parts[1]
        # authorize() returns needs_confirm for OUTWARD actions (even for admin),
        # or deny if the principal lacks dispatch / isn't in project scope.
        d = Z.authorize(principal, "deploy", {"slug": target_slug},
                        data_dir=cfg.data_dir)
        if d.denied:
            return _denied(d)
        if not d.needs_confirm:
            # Defensive: deploy is always confirm-first. If that ever changes,
            # fail closed rather than silently executing unconfirmed.
            _notify(cfg, token, chat_id, "deploy is not available")
            return {"ok": False, "action": "deploy_unavailable"}
        # Stash a pending record + ask to confirm. Do NOT execute yet. Build the
        # full dispatch params now so confirmation just runs them verbatim.
        deploy_params = {
            "slug": target_slug,
            "target": deploy_target,
            "reason": f"telegram /deploy by {principal.from_id}",
            "requested_by": f"tg:{principal.from_id}",
        }
        rec = P.put(cfg.data_dir, chat_id, principal.from_id, "deploy",
                    deploy_params, principal.role)
        _notify(cfg, token, chat_id,
                f"Confirm: deploy {target_slug}→{deploy_target}? reply 'yes' "
                f"(or /confirm {rec.token}). Expires in 5 min.")
        return {"ok": True, "action": "needs_confirm", "gated_action": "deploy",
                "slug": target_slug, "target": deploy_target, "token": rec.token,
                "from_id": principal.from_id}

    if cmd == "confirm":
        # /confirm [token] — explicit confirmation of THIS sender's pending.
        token_arg = (args.split() or [""])[0]
        return _execute_confirmation(cfg, proj, chat_id, principal, token=token_arg)

    if cmd == "help":
        _notify(cfg, token, chat_id,
                "/status [slug] — list sessions for a project\n"
                "/sessions — list active sessions\n"
                "/say <sid> <text> — inject text into a session\n"
                "/spawn <slug> <role> — spawn a swarm expert\n"
                "/deploy <slug> <target> — deploy (asks you to confirm first)\n"
                "/confirm [token] — confirm a pending gated action")
        return {"ok": True, "action": "help"}

    return {"ok": False, "action": "unknown_cmd"}


_AFFIRMATIVE = {"yes", "y", "confirm", "ok", "okay", "yep"}
_NEGATIVE = {"no", "n", "cancel", "nope", "abort"}


def _maybe_handle_confirmation(cfg, proj, chat_id: str, msg: dict):
    """If this sender has an active pending AND the message is a plain yes/no,
    handle it as a confirmation. Returns a result dict, or None to fall through.

    Engaged ONLY for tg_authz projects (caller gates on _authz_on). Placed before
    the brain so a plain "yes" confirms a gated action instead of being sent to
    the LLM. Slash-form confirmation (/confirm <token>) is handled separately via
    the slash path; here we only intercept bare yes/no/cancel.
    """
    from bot_squad_worker import tg_pending as P
    from bot_squad_worker import tg_authz as Z

    from_id = _from_id(msg)
    text = (msg.get("text") or "").strip().lower()
    # A bare slash command is not a yes/no; let the slash router own it. (Slash
    # commands are already handled before handle_update reaches this point, but
    # guard anyway.)
    if text.startswith("/"):
        return None
    word = text.split()[0] if text else ""

    # If there's no pending file at all for this sender, a bare yes/no is just
    # chatter → let it reach the brain. (An EXPIRED file still routes here so we
    # can reply "expired, re-issue" rather than silently sending it to the LLM.)
    has_file = _has_pending_file(cfg, chat_id, from_id)

    if word in _AFFIRMATIVE:
        if not has_file:
            return None
        principal = Z.resolve_identity(cfg, msg, proj)
        return _execute_confirmation(cfg, proj, chat_id, principal)

    if word in _NEGATIVE:
        if not has_file:
            return None
        token = _token_for_proj(cfg, proj)
        P.clear(cfg.data_dir, chat_id, from_id)
        _notify(cfg, token, chat_id, "cancelled")
        return {"ok": True, "action": "confirm_cancelled", "from_id": from_id}

    # Not a yes/no → not a confirmation; let it reach the brain.
    return None


def _has_pending_file(cfg, chat_id, from_id) -> bool:
    """True if a pending record file exists for (chat,from_id), even if expired.

    Lets us tell "user said yes with NO pending" (chat the brain) apart from
    "user said yes but the pending EXPIRED" (reply 'expired, re-issue')."""
    from bot_squad_worker import tg_pending as P
    return P._path(cfg.data_dir, chat_id, from_id).exists()


def _execute_confirmation(cfg, proj, chat_id: str, principal, *, token: str = ""):
    """Verify + execute the pending gated action for ``principal`` in this chat.

    Checks, in order (fail closed):
      1. capability: principal.can_confirm_gated (director/admin tier).
      2. a pending exists for THIS (chat_id, principal.from_id) — the store key
         already binds the pending to the raiser, so a different user's confirm
         simply finds no pending of their own.
      3. not expired (get_active returns None if expired).
      4. raised_by == confirmer (or confirmer is admin) — explicit cross-user bind.
      5. optional token match when /confirm <token> was used.
    On success → A.dispatch(action, params) → reply outcome → clear pending.
    """
    from bot_squad_worker import actions as A
    from bot_squad_worker import tg_pending as P
    tok = _token_for_proj(cfg, proj)
    from_id = principal.from_id

    if not principal.can_confirm_gated:
        _notify(cfg, tok, chat_id, "not authorized to confirm gated actions")
        return {"ok": False, "action": "confirm_denied",
                "reason": "no_confirm_capability", "from_id": from_id}

    pending = P.get_active(cfg.data_dir, chat_id, from_id)
    if pending is None:
        # Either nothing pending, or it expired. Distinguish for a useful reply.
        if _has_pending_file(cfg, chat_id, from_id):
            P.clear(cfg.data_dir, chat_id, from_id)
            _notify(cfg, tok, chat_id, "confirmation expired — re-issue the request")
            return {"ok": False, "action": "confirm_expired", "from_id": from_id}
        _notify(cfg, tok, chat_id, "nothing to confirm")
        return {"ok": False, "action": "confirm_none", "from_id": from_id}

    # Cross-user binding: only the raiser (or an admin) may confirm. Because the
    # store is keyed on (chat,from_id) this is normally guaranteed, but we assert
    # it explicitly so the invariant is enforced in code, not just by the key.
    if pending.raised_by != from_id and not principal.is_admin:
        _notify(cfg, tok, chat_id, "only the user who raised it can confirm")
        return {"ok": False, "action": "confirm_wrong_user", "from_id": from_id}

    if token and token != pending.token:
        _notify(cfg, tok, chat_id, "confirm token mismatch — re-issue the request")
        return {"ok": False, "action": "confirm_bad_token", "from_id": from_id}

    # Execute, then clear (clear regardless of dispatch outcome — single shot).
    try:
        result = A.dispatch(pending.action, pending.params)
        P.clear(cfg.data_dir, chat_id, from_id)
        _notify(cfg, tok, chat_id,
                f"✅ {pending.action} confirmed and dispatched: {result}")
        return {"ok": True, "action": "confirmed", "gated_action": pending.action,
                "result": result, "from_id": from_id}
    except A.ActionError as e:
        P.clear(cfg.data_dir, chat_id, from_id)
        _notify(cfg, tok, chat_id, f"❌ {pending.action} failed: {e}")
        return {"ok": False, "action": "confirm_failed",
                "gated_action": pending.action, "error": str(e), "from_id": from_id}


def _owner_for_sid(cfg, slug: str, sid: str):
    """Best-effort lookup of a session's owner for the ownership check.

    Returns the owner (from session metadata) or None if it can't be resolved —
    in which case authorize's ownership check is skipped (only matters for the
    'member' tier; director/admin bypass it anyway). Never raises.
    """
    try:
        from bot_squad_worker import sessions as S
        for row in S.list_sessions(cfg, slug):
            if row.get("sid") == sid:
                return row.get("owner")
    except Exception:  # noqa: BLE001
        pass
    return None


def _notify(cfg, token: str, chat_id: str, message: str) -> None:
    """Lightweight outbound message -- bypass debounce, no SID prefix."""
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        httpx.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
    except httpx.HTTPError:
        pass


def _tick_project(cfg, proj) -> dict:
    """Poll + process one project's bot, with its own persisted offset."""
    token = cfg.bot_token_for(proj.slug)
    last_id = _read_last_update_id(cfg, proj.slug)
    updates = poll_updates(cfg, last_id, timeout=25, token=token)
    handled = 0
    max_id = last_id
    for update in updates:
        uid = update.get("update_id", 0)
        max_id = max(max_id, uid)
        try:
            handle_update(cfg, update, proj=proj)
            handled += 1
        except Exception:
            # Swallow so one bad update doesn't poison the offset for the rest.
            continue
    if max_id > last_id:
        _write_last_update_id(cfg, max_id, proj.slug)
    return {"slug": proj.slug, "polled": len(updates),
            "handled": handled, "max_update_id": max_id}


def tick(cfg) -> dict:
    """One poll-and-process cycle across every ENABLED project's bot.

    Called by the APScheduler job every 30s. Iterates the projects whose
    ``tg_enabled`` is True (and which have a resolvable token), polling each
    bot's getUpdates with its own offset file. Disabled projects are never
    polled. 'computation' (global token, enabled) keeps its legacy offset file
    and behavior.
    """
    per_project = [_tick_project(cfg, p) for p in cfg.enabled_tg_projects()]
    polled = sum(r["polled"] for r in per_project)
    handled = sum(r["handled"] for r in per_project)
    max_id = max((r["max_update_id"] for r in per_project), default=0)
    return {"ok": True, "polled": polled, "handled": handled,
            "max_update_id": max_id, "projects": per_project}
