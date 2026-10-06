"""HeadlessSubstrate — runs loop agents as worker-daemon Claude sessions.

Maps the engine's four-call :class:`~mcp_loops.runner.Substrate` contract onto the
worker daemon (Unix socket at ``<data>/_sock/worker.sock``):

  run_turn      → for an INTERACTIVE role (manager / adopted): spawn_session
                  (once, long-lived) OR inject_input the turn prompt. For a
                  HEADLESS role (worker / input_provider): one-shot ``claude -p``
                  subprocess — do the slice, run the report command, EXIT — with
                  no persistent REPL to reap. Either way the outcome is the next
                  end-of-turn status line in status.jsonl (written by
                  ``mcp_loops.report``); no screen-scraping.
  brief_manager → spawn + prime the manager with the north-star goal.
  compact       → sleep_expert (compact-before-next-turn), gated by frequency.
  shutdown      → suspend every LIVE session (only the manager/service persist;
                  headless workers already exited, so there is nothing to reap).

Manager-only-tmux (2026-09-20): before this, EVERY agent was a long-lived tmux
REPL that had to be reaped and otherwise piled up as orphans in the shared
session the owner attaches to. Now only the manager (which the owner briefs and
steers) keeps a live session; the disposable "do a slice and report" roles run
headless and leave nothing behind.

The completion signal is deliberately pane-independent: an agent ends its turn by
running the reporter command (which the substrate states explicitly at the top of
every prompt), appending a JSON line the substrate reads. No screen-scraping.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from mcp_loops import runtimes
from mcp_loops import paths
from mcp_loops import report
from mcp_loops import role_profiles
from mcp_loops import sandbox
from mcp_loops import turn_identity
from mcp_loops.report import status_log
from mcp_loops.runner import OwnerChannel, TurnOutcome

# Fallback binding for when NO runner is attached (the server passes the
# attached runner's sock/repo/python explicitly — see runner_registry). Derived
# from the install location so a fresh clone points at its OWN daemon, not a
# ``~/bot-swarm`` it doesn't own. On the original box these resolve to the same
# paths as before, so the swarm daemon is untouched.
DEFAULT_SOCK = Path(paths.sock_dir()) / "worker.sock"
DEFAULT_REPO = str(paths.install_root())
# R5: the interpreter running the engine (a bundle's runtime/bin/python3, the
# dev box's unit python) — not a ``worker/.venv`` that need not exist.
DEFAULT_PY = sys.executable

# Session liveness. A sid is a tmux pane address (S-<user>-<win>-p<pane>): when
# the agent's claude exits (window closes) or the tmux server restarts, every
# daemon verb on it fails with one of these. That is a STALE sid, not a wedged
# agent — the substrate re-resolves it (fresh session + re-deliver the turn)
# instead of letting the guardian escalate it to give_up.
_DEAD_SESSION_MARKERS = ("no live pane", "unknown sid", "no such session",
                         "can't find pane", "can't find window", "can't find session",
                         "no server running")
# Internal outcome of _wait_status when the pane vanished mid-turn; never
# returned to the runner (run_turn respawns, or maps it to "timeout").
DEAD_SESSION = "dead_session"
# Internal outcome of _wait_status when the turn's prompt never landed: the pane
# still sits idle at the empty-input composer after the landing window (tmux
# driver dropped the paste, or typed it but the Enter was swallowed). Never
# returned to the runner (the pointer is re-delivered, or it maps to "timeout").
UNLANDED = "unlanded"


class LoopStopped(RuntimeError):
    """Raised by the interactive delivery path once shutdown() began: a stopping
    loop must never (re)spawn or inject into a session (loopyard-bug-1790562425)."""

# Pane-tail markers for the landing check (engine-side copy: the engine and the
# worker package only talk over the socket). BUSY = a turn is in flight; READY =
# the REPL footer/composer is up. "Idle at the composer" = READY and not BUSY.
_PANE_BUSY_MARKERS = ("esc to interrupt", "Esc to interrupt")
_PANE_READY_MARKERS = ("for shortcuts", "bypass permissions", "shift+tab to cycle",
                       'Try "', "Ask Codex")


def _is_dead_session_error(e: BaseException) -> bool:
    msg = str(e).lower()
    return any(m in msg for m in _DEAD_SESSION_MARKERS)

# Headless single-shot plumbing. Worker + input_provider agents run as one-shot
# ``claude -p`` turns — do the slice, run the report command, EXIT — instead of a
# long-lived tmux REPL that has to be reaped and otherwise piles up as orphans in
# the shared session. Only the MANAGER (which the owner may attach to and steer)
# stays a persistent interactive session. Mirrors the daemon's own headless
# pattern (librarian/tg_pipelines) but is defined HERE so the engine stays
# decoupled from the worker package — the two otherwise talk only over the socket.
def _resolve_claude_bin() -> str:
    """R4: ``$LOOPS_CLAUDE_BIN`` > ``claude`` on PATH > ``~/.local/bin/claude``."""
    return (os.environ.get("LOOPS_CLAUDE_BIN") or shutil.which("claude")
            or os.path.expanduser("~/.local/bin/claude"))


_CLAUDE_BIN = _resolve_claude_bin()


# ── runtime CLI + model at spawn (gap #2) ────────────────────────────────────
# The CLIs a loop agent may run — the SAME allowlist the daemon enforces
# (bot_squad_worker.sessions.SUPPORTED_RUNTIMES) and schema.py validates. The
# engine checks it again at substrate construction so an unknown runtime fails
# the start with a clear error instead of silently launching a bare `claude`.
SPAWN_RUNTIMES = runtimes.SUPPORTED_RUNTIMES
PASSTHROUGH_ENV = "LOOPS_MODEL_PASSTHROUGH"
# A model id is handed to the CLI as ``--model <id>``: keep it to an id charset
# and never let it start with '-' (it must not smuggle a second flag).
_MODEL_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      "0123456789._:/[]@-")


def model_passthrough_enabled(env: Optional[dict] = None) -> bool:
    """Whether the chosen runtime CLI + model reach the spawned agent. ON by
    default — the daemon accepts ``model``/``runtime`` on spawn_session (CAP-1)
    and headless turns build their own argv — so a picked CLI/model is APPLIED,
    not merely recorded. ``LOOPS_MODEL_PASSTHROUGH=0`` (or false/off/no) is the
    explicit kill switch back to the historical bare-`claude` request."""
    raw = (os.environ if env is None else env).get(PASSTHROUGH_ENV)
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("0", "false", "off", "no")


def validate_runtime(runtime) -> str:
    """Normalise a runtime id against :data:`SPAWN_RUNTIMES`; ''/None ⇒ claude.
    Raises ``ValueError`` naming the allowlist for anything else."""
    if runtime is None or not str(runtime).strip():
        return "claude"
    rt = str(runtime).strip().lower()
    if rt not in SPAWN_RUNTIMES:
        raise ValueError(f"unsupported runtime {runtime!r} — expected one of "
                         f"{list(SPAWN_RUNTIMES)}")
    return rt


def validate_model(model) -> Optional[str]:
    """A model id safe to pass as ``--model <id>``; ''/None ⇒ None (CLI default)."""
    if model is None or not str(model).strip():
        return None
    m = str(model).strip()
    if m.startswith("-") or len(m) > 128 or not set(m) <= _MODEL_OK:
        raise ValueError(f"invalid model id {model!r}")
    return m


def _resolve_codex_bin() -> str:
    """``$LOOPS_CODEX_BIN`` > ``codex`` on PATH > bare ``codex``."""
    return os.environ.get("LOOPS_CODEX_BIN") or shutil.which("codex") or "codex"
# Phase-B knobs that opt _slug_cwd into paths.config_dir(); all unset (the live
# shape, which sets only the pre-existing LOOPYARD_INSTALL) ⇒ legacy <repo>/config.
_SLUG_CWD_KNOBS = (paths.ENV_CONFIG_DIR, paths.ENV_HOME, "LOOPS_DEFAULT_PROJECT")
# Roles that run headless single-shot (everything the owner never attaches to).
DEFAULT_HEADLESS_ROLES = frozenset({"worker", "input_provider"})
# When a headless turn EXITS without writing the report line (crash / non-compliant
# agent), synthesise the conservative "not done" status for its role so the loop
# keeps driving rather than falsely marking the work complete.
_HEADLESS_NOREPORT = {"worker": "work_remaining", "input_provider": "needs_work"}


def _claude_env() -> dict:
    """Env for the headless claude subprocess: inherit the server's env but strip
    the egress-proxy vars (Anthropic is reached directly), mirroring the daemon."""
    env = dict(os.environ)
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
              "HTTP_PROXY", "http_proxy"):
        env.pop(k, None)
    return env


def cage_write_paths(loop: str, cwd: Optional[str], data_dir: Optional[str] = None,
                     log=None) -> list:
    """What a caged agent of ``loop`` may write besides the sandbox base paths:
    its cwd (the loop worktree, or the legacy slug checkout; ``None`` for a
    brief manager), that worktree's git dirs (never the common hooks/config),
    the loop's output dir, and ONLY the two append logs the report command
    writes in the status dir — config.json / run.json / brief_session.json /
    steer.json there are engine-trusted and stay read-only to agents.

    Raises :class:`sandbox.CageExposesGitDir` when ``cwd`` holds a real ``.git/``
    directory (a legacy no-worktree loop on a shared checkout): Landlock cannot
    carve ``.git/hooks`` out of a writable cwd, so the cage refuses to start."""
    sdir = os.path.join(data_dir or paths.resolve_data_dir(), loop)
    odir = os.path.join(report.output_base(), loop)
    logs = [os.path.join(sdir, f) for f in sandbox.AGENT_STATUS_FILES]
    try:  # Landlock skips paths that don't exist; the agent can't create them later
        os.makedirs(odir, exist_ok=True)
        os.makedirs(sdir, exist_ok=True)
        for f in logs:
            with open(f, "a", encoding="utf-8"):
                pass
    except OSError as e:
        if log:
            log(f"[headless] cage: cannot pre-create {odir} / status logs: {e}")
    if cwd:  # fail closed: never grant a checkout whose real .git/ lies beneath it
        gd = sandbox.exposed_git_dir(cwd)
        if gd:
            raise sandbox.CageExposesGitDir(
                f"refusing to cage loop {loop!r} with cwd {cwd}: its real git dir {gd} "
                f"(hooks/config) would be agent-writable — run the loop in an "
                f"engine-provisioned worktree (loop workspace) instead")
    head = [cwd, *sandbox.git_write_paths(cwd)] if cwd else []
    return [*head, odir, *logs]


class HeadlessSubstrate:
    def __init__(self, slug: str, loop_name: str, *,
                 sock_path: Path | str = DEFAULT_SOCK,
                 repo: str = DEFAULT_REPO,
                 python: str = DEFAULT_PY,
                 base_window: int = 90,
                 turn_timeout: float = 1200.0,
                 cold_timeout: float = 420.0,
                 # spawn blocks on the daemon delivering + SUBMITTING the first
                 # prompt; a slow-booting CLI composer can take ~2min to honor
                 # Enter (see sessions._deliver_prompt MODE 2), so the client must
                 # wait that out rather than time out mid-deliver and fail the spawn.
                 spawn_timeout: float = 200.0,
                 poll_interval: float = 4.0,
                 compact_enabled: bool = True,
                 compact_every: int = 4,
                 python_flags: tuple = (),
                 owner: str = "coord",
                 agent_timeouts: Optional[dict] = None,
                 agent_models: Optional[dict] = None,
                 agent_runtimes: Optional[dict] = None,
                 model_passthrough: bool = False,
                 loops_data_dir: Optional[str] = None,
                 # Loop-Workspaces (Phase 1): the engine-provisioned ephemeral
                 # worktree this loop builds in (an absolute path). When set it is
                 # the cwd for EVERY session spawn — threaded as the `workspace`
                 # param into the daemon's spawn_session (interactive roles) AND
                 # used directly as the headless subprocess cwd — DETACHED from any
                 # per-loop projects.toml slug. None ⇒ byte-for-byte legacy: the
                 # slug's repo_path is used and no `workspace` key is ever sent (so
                 # a daemon that predates the param is untouched).
                 workspace: Optional[str] = None,
                 # Session ADOPTION: {agent_id: sid} whose LIVE session was created
                 # OUTSIDE the loop (an owner briefed a plain session that now BECOMES
                 # the manager). Pre-seeded so turns inject into it; never respawned.
                 adopt_sessions: Optional[dict] = None,
                 # Manager-only-tmux pivot: these roles run HEADLESS single-shot
                 # (one `claude -p` per turn, no persistent REPL). Everything not
                 # listed here (manager, service) keeps a live interactive session.
                 headless_roles=DEFAULT_HEADLESS_ROLES,
                 claude_bin: Optional[str] = None,
                 codex_bin: Optional[str] = None,
                 # injectable seam for tests: (agent_id, cmd, prompt, cwd, timeout)
                 # -> (returncode|None, stdout, stderr, timed_out). Defaults to a
                 # real subprocess.run so tests never spawn a real claude.
                 run_headless_fn=None,
                 # Q2 observability: persist each agent turn's raw pane transcript
                 # to _output/<loop>/<agent>-<turn>.txt so a failed/hung turn is
                 # inspectable WITHOUT the 10MB engine log. capture_fn is an
                 # injectable seam (agent_id -> raw text) — defaults to pulling the
                 # pane scrollback over the daemon's `capture_pane` action; tests
                 # inject a fake, and a future richer source (claude jsonl) can drop
                 # in here without touching the call sites.
                 transcript_enabled: bool = True,
                 transcript_max_bytes: int = 512_000,
                 capture_fn=None,
                 # Loop cage (mcp_loops.sandbox): the Policy planned for THIS loop at
                 # start. Headless turns run wrapped (slice scope + exec stage);
                 # interactive spawns send it as the `sandbox` param; shutdown()
                 # stops the slice (Step 5). None ⇒ today's argv/request exactly.
                 sandbox_policy=None,
                 # Session liveness: while waiting on an interactive turn, probe the
                 # agent's pane every N seconds (daemon `capture_pane` reports a
                 # vanished pane as missing). A dead pane is respawned and the turn's
                 # prompt re-delivered, at most max_respawns times per turn.
                 liveness_interval: float = 30.0,
                 max_respawns: int = 3,
                 # Prompt landing: if a turn's pointer has not visibly started a
                 # turn (pane still idle at the composer, no report) within
                 # landing_timeout seconds, re-deliver it — at most max_redelivers
                 # times — then fail the turn fast to the guardian instead of
                 # silently burning the whole turn budget on a dropped prompt.
                 landing_timeout: float = 120.0,
                 max_redelivers: int = 2,
                 log=print):
        self.slug = slug
        self.loop = loop_name
        self.sock = Path(sock_path)
        self.repo = repo
        self.python = python
        # P1c (split-brain fix): the LOOPS_DATA_DIR the SERVER resolved, sent to
        # the worker daemon so the spawned agent's `mcp_loops.report` writes its
        # end-of-turn status into the SAME data dir the server tails — regardless
        # of what LOOPS_DATA_DIR the daemon process itself was started under. Set
        # explicitly so a fresh install with its own data dir never silently
        # splits its writes back under the install's own data dir. None ⇒ omit (the daemon
        # keeps its own env default — the historical behaviour).
        self.loops_data_dir = (os.path.abspath(loops_data_dir)
                               if loops_data_dir else None)
        # Engine-provisioned ephemeral worktree for this loop (see param docs).
        self.workspace = os.path.abspath(workspace) if workspace else None
        self.turn_timeout = turn_timeout
        self.cold_timeout = cold_timeout
        # per-agent turn budget in SECONDS (from config maxTurnMinutes); the
        # guardian can extend an entry mid-loop for a slow-but-productive agent.
        self._agent_timeouts: dict[str, float] = {k: float(v) for k, v in (agent_timeouts or {}).items()}
        # per-agent MODEL id (from config/registry). Applied at spawn when
        # model_passthrough is ON (the default, see model_passthrough_enabled):
        # sent to the daemon as `model` for interactive panes and passed as
        # `--model` on the headless argv. OFF ⇒ both are byte-for-byte historical.
        # Validated HERE (allowlist + id charset) so a bad value fails the loop
        # start with a clear error, never a silent fallback to a bare `claude`.
        self._agent_models: dict[str, str] = {}
        for k, v in (agent_models or {}).items():
            try:
                m = validate_model(v)
            except ValueError as e:
                raise ValueError(f"agent {k!r}: {e}") from None
            if m:
                self._agent_models[k] = m
        # per-agent RUNTIME id (CAP-1 multi-CLI: "claude"|"codex"). Applied under
        # the same gate as `model`: the daemon's `runtime` param for interactive
        # panes, the headless argv's CLI for one-shot turns.
        self._agent_runtimes: dict[str, str] = {}
        for k, v in (agent_runtimes or {}).items():
            try:
                self._agent_runtimes[k] = validate_runtime(v)
            except ValueError as e:
                raise ValueError(f"agent {k!r}: {e}") from None
        # Fail invalid host routing before any session is spawned.
        runtimes.selection()
        self.model_passthrough = bool(model_passthrough)
        self.spawn_timeout = spawn_timeout
        self.poll = poll_interval
        self.liveness_interval = liveness_interval
        self.max_respawns = max(0, int(max_respawns))
        self.landing_timeout = float(landing_timeout)
        self.max_redelivers = max(0, int(max_redelivers))
        # agent_id -> did this turn's pointer visibly land? True (daemon verified
        # submit / pane seen busy / report arrived), False (seen idle at the
        # composer past the landing window), absent = unknown.
        self._landed: dict[str, bool] = {}
        self.compact_enabled = compact_enabled
        self.compact_every = max(1, compact_every)
        # R31: interpreter isolation for the agent-facing report command; only
        # "-I" is honoured, and only when the bound runner.json carries it.
        self.python_flags = tuple(python_flags or ())
        self.owner = owner
        self.log = log
        self.transcript_enabled = bool(transcript_enabled)
        self.transcript_max_bytes = max(0, int(transcript_max_bytes))
        self._capture_fn = capture_fn or self._capture_pane_text
        # ── headless single-shot config ──
        self._headless_roles = frozenset(headless_roles or ())
        self._claude_bin = claude_bin or _CLAUDE_BIN
        self._codex_bin = codex_bin           # None ⇒ resolved at first codex turn
        self._run_headless_fn = run_headless_fn or self._subprocess_headless
        self._headless_output: dict[str, str] = {}   # agent_id -> last stdout (transcript source)
        self._cwd_cache: Optional[str] = None        # slug repo dir, resolved once
        # Track every live `claude -p` subprocess so shutdown()/stop can REAP it
        # (and its child browser/tool processes) instead of orphaning it — the
        # headless analogue of suspending a tmux session. Without this, a stopped
        # or re-batched loop leaves its workers running and load piles up until the
        # box is starved. Each proc runs in its OWN process group so we kill the
        # whole tree (claude + any headless-Chromium/node children).
        self._procs: dict = {}                       # agent_id -> subprocess.Popen
        self._proc_lock = threading.Lock()
        self._stopped = False                        # set by shutdown(); blocks new spawns
        self._cage = sandbox_policy
        self._cage_fs: Optional[sandbox.Policy] = None   # + this loop's write paths
        self._cage_ready = False                     # slice created + limits read back
        self._cage_lock = threading.Lock()
        self.cage_teardown: Optional[dict] = None    # last Step-5 teardown result

        self.status_path = status_log(loop_name)
        self.sids: dict[str, str] = {}         # agent_id -> sid
        self._turns: dict[str, int] = {}       # agent_id -> turns run (for compact gating)
        self._consumed: dict[str, int] = {}    # agent_id -> status lines already attributed
        self._roles: dict[str, str] = {}       # agent_id -> role (for re-nudge command)
        self._next_window = base_window
        self._spawn_lock = threading.Lock()    # window allocation under parallel groups
        # ADOPTION pre-seed: bind these agent_ids to their existing sessions so
        # run_turn/brief_manager inject into the live session instead of spawning.
        self._adopted: set[str] = set()
        # adopted agent_ids whose sid has been registered with the daemon's role
        # registry (see _register_adopted) — done once per adopted sid.
        self._role_registered: set[str] = set()
        for _aid, _sid in (adopt_sessions or {}).items():
            if _sid:
                self.sids[_aid] = str(_sid)
                self._adopted.add(_aid)

    def _timeout_for(self, agent_id: str, first: bool = False) -> float:
        """This agent's turn budget (seconds): its configured maxTurnMinutes (or
        the global default), and never less than cold_timeout on its first turn."""
        base = self._agent_timeouts.get(agent_id, self.turn_timeout)
        return max(self.cold_timeout, base) if first else base

    def extend_agent_timeout(self, agent_id: str, minutes: float) -> bool:
        """Guardian hook: grant a slow-but-productive agent a longer turn budget
        for the rest of the loop (called when the service agent says EXTEND=<min>)."""
        try:
            self._agent_timeouts[agent_id] = float(minutes) * 60.0
            self.log(f"[headless] extended {agent_id} turn budget → {minutes}m")
            return True
        except Exception:  # noqa: BLE001
            return False

    # ── socket ──
    def _call(self, name: str, params: dict, timeout: float = 20.0) -> dict:
        tr = httpx.HTTPTransport(uds=str(self.sock))
        with httpx.Client(transport=tr, base_url="http://w", timeout=timeout) as c:
            r = c.post(f"/actions/{name}", json=params)
            if r.status_code != 200:
                detail = r.text
                try:
                    detail = r.json().get("detail", r.text)
                except Exception:  # noqa: BLE001 — error body may not be JSON (e.g. bare 500)
                    pass
                raise RuntimeError(f"{name} HTTP {r.status_code}: {detail}")
            try:
                return r.json() if r.content else {}
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"{name}: non-JSON 200 body: {r.text[:200]}") from e

    # ── prompt framing: the authoritative reporter command ──
    _VOCAB = {
        "worker": "completed | work_remaining",
        "input_provider": "satisfied | minor_only | needs_work",
        "manager": "continue | ask_owner | wind_down | complete",
        "service": "retry | give_up",
    }

    def _report_prefix(self) -> str:
        """``cd <repo> && <python> [-I] -m mcp_loops.report`` — paths shell-quoted
        (R29; unchanged for space-free paths), ``-I`` only when bound (R31)."""
        iso = " -I" if "-I" in self.python_flags else ""
        return (f'cd {shlex.quote(str(self.repo))} && '
                f'{shlex.quote(str(self.python))}{iso} -m mcp_loops.report')

    def _report_banner(self, agent_id: str, role: str) -> str:
        vocab = self._VOCAB.get(role, "completed")
        cmd = (f'{self._report_prefix()} '
               f'{self.loop} {agent_id} <STATUS> "<one-line note>"')
        # Durable guardrail: loop agents must NOT spawn their own sub-agents — the
        # ENGINE dispatches every other agent + sub-loop. A manager that "runs the
        # departments" via the Task tool blocks its own turn and bypasses the loop.
        no_spawn = (
            "Do this turn's work YOURSELF in THIS session. Do NOT use the Task tool "
            "or spawn/launch any sub-agents — the loop engine already runs every other "
            "agent and sub-loop for you, automatically, when your turn ends.\n")
        mgr_note = (
            "You are the manager, NOT an orchestrator of other sessions: you do not "
            "start, run, or wait on the workers, inputs, or sub-loops — the engine does "
            "that. Your turn is only to think, set direction in your note, and report. "
            if role == "manager" else "")
        return (
            "=== LOOP TURN PROTOCOL (authoritative) ===\n"
            f"You are agent '{agent_id}' (role: {role}) in loop '{self.loop}'.\n"
            + no_spawn + mgr_note +
            "Do the work described below. When — and only when — this turn is done, "
            "END your turn by running EXACTLY this shell command, substituting one "
            f"STATUS from [{vocab}] and a short note:\n  {cmd}\n"
            "That report is how the orchestrator learns your turn ended and what to do "
            "next. Nothing after it will be read this turn.\n"
            "==========================================\n")

    def _frame(self, agent_id: str, role: str, prompt: str) -> str:
        return self._report_banner(agent_id, role) + "\n" + prompt

    # ── delivery: write the full prompt to a file, hand the agent a ONE-LINE
    # pointer to it. inject_input submits one Enter PER LINE, so a multi-line
    # prompt would be fragmented into separate REPL messages; a single-line
    # pointer is submitted intact and the agent reads the real prompt from disk.
    def _begin_turn(self, agent_id: str) -> int:
        """Shared per-turn bookkeeping for BOTH the interactive and headless paths:
        baseline the status cursor past any pre-existing lines before this agent's
        first turn (e.g. a re-run of a same-named loop), then bump + return the
        1-based turn index used for prompt/transcript filenames."""
        self._consumed.setdefault(agent_id, len(self._agent_lines(agent_id)))
        ix = self._turns.get(agent_id, 0) + 1
        self._turns[agent_id] = ix
        # H7: record (runId, agent, turn) so the agent's report row is stamped
        # with THIS per-run index — the one the prompt file is named with.
        turn_identity.note_turn(os.path.dirname(self.status_path), agent_id, ix)
        return ix

    def _deliver(self, agent_id: str, role: str, prompt: str) -> str:
        ix = self._begin_turn(agent_id)
        d = os.path.join(os.path.dirname(self.status_path), "prompts")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{agent_id}-{ix:03d}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self._frame(agent_id, role, prompt))
        return (f"Read the file {path} in full, then follow it as your instructions "
                f"for THIS turn (it tells you the exact command to end the turn). "
                f"Do nothing else until you have read it.")

    # ── headless single-shot turns (workers / input_providers) ──
    def _is_headless(self, agent_id: str, role: str) -> bool:
        """True if this agent runs as a one-shot `claude -p` subprocess. An ADOPTED
        agent is the owner-briefed manager and is ALWAYS interactive, whatever its
        role string, so it is never routed here."""
        return role in self._headless_roles and agent_id not in self._adopted

    def _slug_cwd(self) -> str:
        """Working dir for a headless turn: the slug's ``repo_path`` from
        ``projects.toml``, falling back to the engine repo. Resolved once and
        cached. Which ``projects.toml``: when a Phase-B knob
        (``_SLUG_CWD_KNOBS``) is set, ``paths.config_dir()`` (R14: the SAME
        resolver the engine and the daemon's spawn_session use); with every
        knob unset, the legacy ``<repo>/config`` — byte-identical to 1dda54c
        (D7; the live engine sets only LOOPYARD_INSTALL, and moving its
        projectId-less cwd is a B5 owner cut-over, not a B0 change).

        Loop-Workspaces: when the engine provisioned an ephemeral worktree for
        this loop, THAT is the cwd — it takes precedence over the slug's
        projects.toml repo_path, so a headless worker builds in the loop's own
        worktree rather than the shared project checkout. Unset ⇒ legacy path."""
        if self.workspace:
            return self.workspace
        if self._cwd_cache:
            return self._cwd_cache
        cwd = self.repo
        try:
            import tomllib
            if any(os.environ.get(k) for k in _SLUG_CWD_KNOBS):
                p = paths.config_dir() / "projects.toml"
            else:
                p = Path(self.repo) / "config" / "projects.toml"
            if p.exists():
                data = tomllib.loads(p.read_text("utf-8"))
                rp = ((data.get("projects") or {}).get(self.slug) or {}).get("repo_path")
                if rp and os.path.isdir(rp):
                    cwd = rp
        except Exception as e:  # noqa: BLE001 — bad/absent toml → fall back to repo
            self.log(f"[headless] slug cwd resolve failed ({self.slug}): {e}")
        self._cwd_cache = cwd
        return cwd

    def _kill_proc(self, proc) -> None:
        """SIGKILL a headless subprocess AND its whole process group (claude plus
        any child headless-Chromium/node it spawned), so nothing is left running."""
        import os
        import signal
        for attempt in (
            lambda: os.killpg(os.getpgid(proc.pid), signal.SIGKILL),
            lambda: proc.kill(),
        ):
            try:
                attempt()
                return
            except Exception:  # noqa: BLE001 — already-dead / no pgid → try the next form
                continue

    def _reap_procs(self) -> None:
        """Stop signal + kill every live headless subprocess. Called by shutdown()
        so a stopped/finished loop never leaves `claude -p` workers (or their child
        browsers) churning in the background and starving the box."""
        self._stopped = True
        with self._proc_lock:
            procs = list(self._procs.items())
        for agent_id, proc in procs:
            try:
                if proc.poll() is None:
                    self._kill_proc(proc)
                    self.log(f"[headless] reaped in-flight worker {agent_id}")
            except Exception:  # noqa: BLE001 — best-effort
                pass

    def _subprocess_headless(self, agent_id: str, cmd: list, prompt: str,
                             cwd: str, timeout: float):
        """Default headless runner: one `claude -p` subprocess, prompt on stdin,
        proxy-stripped env with LOOPS_DATA_DIR pinned so the agent's report writes
        into the status log THIS substrate tails. Runs in its OWN process group and
        is REGISTERED in self._procs so shutdown() can kill the whole tree mid-turn
        (a stop must not leave the worker — or its child browser — running). Returns
        (returncode|None, stdout, stderr, timed_out)."""
        import subprocess
        if self._stopped:                            # loop already told to stop — don't spawn
            return (None, "", "loop stopped before turn start", True)
        env = _claude_env() if self.spawn_cli(agent_id)[0] == "claude" else dict(os.environ)
        if self.loops_data_dir:
            env["LOOPS_DATA_DIR"] = self.loops_data_dir
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env, text=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)              # own process group → kill the whole tree
        except Exception as e:  # noqa: BLE001 — a spawn failure is a failed turn, not a crash
            self.log(f"[headless] subprocess {agent_id} spawn error: {e}")
            return (1, "", str(e), False)
        with self._proc_lock:
            self._procs[agent_id] = proc
        # If a stop landed between the check and the register, kill immediately.
        if self._stopped:
            self._kill_proc(proc)
        try:
            out, err = proc.communicate(input=prompt, timeout=timeout)
            # A shutdown() kill makes communicate() return with a non-zero/None code;
            # that surfaces as "no report" → the engine synthesises a turn outcome.
            return (proc.returncode, out or "", err or "", False)
        except subprocess.TimeoutExpired:
            self._kill_proc(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                out, err = "", ""
            return (None, out or "", err or "", True)
        except Exception as e:  # noqa: BLE001
            self._kill_proc(proc)
            self.log(f"[headless] subprocess {agent_id} error: {e}")
            return (1, "", str(e), False)
        finally:
            with self._proc_lock:
                self._procs.pop(agent_id, None)

    def _role_caps_profile(self, role: str, cwd: str) -> Optional[dict]:
        """This role's ``--settings`` profile, or None when the role is uncapped
        (worker / unknown / absent) or ``LOOPS_ROLE_CAPS=0``."""
        return role_profiles.build_profile(
            role, repo=str(self.repo), python=str(self.python),
            python_flags=self.python_flags, cwd=cwd,
            notes_dir=report.output_dir(self.loop))

    def _role_caps_settings(self, role: str, cwd: str) -> Optional[str]:
        """Write + return this role's ``--settings`` file (None ⇒ uncapped)."""
        profile = self._role_caps_profile(role, cwd)
        if profile is None:
            return None
        return role_profiles.write_settings(profile, role_profiles.settings_path(
            os.path.dirname(self.status_path), role))

    def _permission_args(self, role: str, cwd: str) -> list:
        path = self._role_caps_settings(role, cwd)
        if path is None:
            return ["--dangerously-skip-permissions"]         # golden (pre-role-caps)
        return role_profiles.cli_args(path)

    def _ensure_cage(self) -> None:
        """Make sure the loop slice exists with its limits read back — on EVERY
        spawn, not once: if the slice was stopped from outside (a parent's
        descendant teardown, an orphan sweep, an operator), ``systemd-run
        --slice`` would silently re-create it with NO limits. Raises otherwise."""
        if self._cage is None or not self._cage.slice:
            return
        with self._cage_lock:
            got = sandbox.ensure_slice(self._cage)
            if not self._cage_ready:
                self.log(f"[headless] cage {self._cage.slice} ready {got}")
            self._cage_ready = True

    def cage_write_paths(self) -> list:
        return cage_write_paths(self.loop, self._slug_cwd(), self.loops_data_dir,
                                log=self.log)

    def _cage_policy(self) -> Optional["sandbox.Policy"]:
        """The planned cage plus this loop's write paths (computed once)."""
        if self._cage is None or not self._cage.fs_confine:
            return self._cage
        if self._cage_fs is None:
            self._cage_fs = sandbox.with_write_paths(self._cage, self.cage_write_paths())
            if not self.workspace:
                self.log(f"[headless] cage: NO loop workspace — the legacy slug checkout "
                         f"{self._slug_cwd()} is writable by this loop's agents")
        return self._cage_fs

    def _cage_argv(self, agent_id: str, cmd: list) -> list:
        """``cmd`` inside the loop cage; unchanged when isolation is off."""
        if self._cage is None:
            return cmd
        if self._stopped:        # after teardown a scope would re-create the slice unlimited
            raise RuntimeError("loop stopped — cage torn down")
        self._ensure_cage()
        return sandbox.wrap_argv(cmd, self._cage_policy(), agent=agent_id,
                                 nonce=self._cage_nonce(agent_id))

    def _cage_nonce(self, agent_id: str) -> str:
        """Scope-unit suffix, unique per turn (a seam for goldens)."""
        return f"t{self._turns.get(agent_id, 0)}{os.urandom(3).hex()}"

    def teardown_cage(self) -> Optional[dict]:
        """Step 5 barrier: stop the loop slice (kills every process still in it,
        setsid/double-fork escapees included). Idempotent; a no-op when off."""
        if self._cage is None or not self._cage.slice:
            return None
        try:
            res = sandbox.teardown(self._cage)
        except Exception as e:  # noqa: BLE001 — teardown must not raise into stop
            res = {"slice": self._cage.slice, "stopped": False, "error": str(e)}
        self._cage_ready = False
        self.cage_teardown = res
        self.log(f"[headless] cage teardown {res}")
        return res

    def spawn_cli(self, agent_id: str) -> tuple:
        """``(runtime, model|None)`` this agent's session is launched with —
        the recorded choice when passthrough is on, else ``("claude", None)``."""
        if not self.model_passthrough:
            return ("claude", None)
        runtime, model = runtimes.selection(self._agent_runtimes.get(agent_id),
                                            self._agent_models.get(agent_id))
        return runtime, validate_model(model)

    def _headless_cli_argv(self, agent_id: str, role: str, cwd: str) -> list:
        """The one-shot argv (prompt on stdin) for this agent's runtime + model.

        * claude → ``claude -p --output-format text <perms> [--model <id>]``
        * codex  → ``codex exec --dangerously-bypass-approvals-and-sandbox
          --skip-git-repo-check [--model <id>] -`` (``-`` ⇒ prompt from stdin).
          codex has no role-caps equivalent, so a capped role is refused (fail
          closed, never silently uncapped) — the daemon's rule, mirrored.
        Passthrough off ⇒ exactly the historical claude argv."""
        runtime, model = self.spawn_cli(agent_id)
        if runtime != "claude":
            if self._role_caps_profile(role, cwd) is not None:
                raise RuntimeError(f"role caps are not supported for runtime {runtime!r}")
            executable = None
            if runtime == "codex":
                if self._codex_bin is None:
                    self._codex_bin = _resolve_codex_bin()
                executable = self._codex_bin
            return runtimes.argv(runtime, model, headless=True, executable=executable)
        cmd = [self._claude_bin, "-p", "--output-format", "text"]
        try:
            cmd += self._permission_args(role, cwd)
        except Exception as e:  # noqa: BLE001 — surfaced as a failed turn by the caller
            raise RuntimeError(f"role-caps settings unavailable: {e}") from e
        if model:
            cmd += ["--model", model]
        return cmd

    def _run_headless(self, agent_id: str, role: str, framed_prompt: str,
                      timeout: float) -> TurnOutcome:
        """Run ONE headless turn and return its outcome. The framed prompt tells the
        agent to end by running the report command (→ status.jsonl); once the
        process exits we consume that line. If it exited WITHOUT reporting, we
        synthesise a conservative outcome so the loop never hangs on a silent turn."""
        cwd = self._slug_cwd()
        try:
            cmd = self._headless_cli_argv(agent_id, role, cwd)
        except Exception as e:  # noqa: BLE001 — never fall OPEN to bypass
            self.log(f"[headless] {agent_id} cli argv failed: {e}")
            return TurnOutcome(_HEADLESS_NOREPORT.get(role, "work_remaining"),
                               str(e)[:200])
        try:
            cmd = self._cage_argv(agent_id, cmd)
        except Exception as e:  # noqa: BLE001 — never fall OPEN to an uncaged turn
            self.log(f"[headless] {agent_id} loop cage unavailable: {e}")
            return TurnOutcome(_HEADLESS_NOREPORT.get(role, "work_remaining"),
                               f"loop cage unavailable: {e}"[:200])
        self.log(f"[headless] one-shot {agent_id} (role {role}) cwd={cwd} "
                 f"budget={timeout:.0f}s")
        rc, out, err, timed_out = self._run_headless_fn(
            agent_id, cmd, framed_prompt, cwd, timeout)
        self._headless_output[agent_id] = out or err or ""
        # The report line is written mid-run (the agent's final command completes
        # before `claude -p` exits), so it is already on disk — a few poll cycles
        # of grace only cover flush latency. A timed-out turn was killed: no report.
        grace = 0.0 if timed_out else self.poll * 3
        outcome = self._wait_status(agent_id, grace)
        if outcome.status != "timeout":
            return outcome                                  # agent reported cleanly
        if timed_out:
            return TurnOutcome("timeout",
                               f"headless {agent_id} exceeded {timeout:.0f}s turn budget")
        tail = (err or out or "").strip().splitlines()
        note = tail[-1][:200] if tail else "exited without reporting status"
        status = _HEADLESS_NOREPORT.get(role, "work_remaining")
        self.log(f"[headless] {agent_id} exited rc={rc} without a report → {status}")
        return TurnOutcome(status, note)

    # ── status tailing ──
    def _read_status(self) -> list[dict]:
        try:
            with open(self.status_path, encoding="utf-8") as fh:
                out = []
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                return out
        except FileNotFoundError:
            return []

    def _agent_lines(self, agent_id: str) -> list[dict]:
        return [e for e in self._read_status() if e.get("agent") == agent_id]

    def _wait_status(self, agent_id: str, timeout: float,
                     liveness: bool = False) -> TurnOutcome:
        """Consume this agent's NEXT unattributed status line (index-based).

        Reports are consumed one per turn, in order, so a late report from a
        slow turn is never double-matched to a later turn — it's simply picked
        up by the next wait. A timeout does NOT advance the cursor.

        ``liveness``: also probe the agent's pane every ``liveness_interval``s and
        return ``DEAD_SESSION`` as soon as it is gone (claude exited / tmux server
        restarted) instead of burning the whole turn budget on a pane that can
        never report. It also watches the prompt LAND: until the pointer is known
        to have landed, probe the pane (finer cadence) for a running turn; if it
        still sits IDLE at the composer once ``landing_timeout`` has passed,
        return ``UNLANDED`` so the caller re-delivers the prompt instead of
        waiting out the whole budget.
        """
        start = time.time()
        deadline = start + timeout
        landing_deadline = start + self.landing_timeout
        landing_step = max(self.poll, min(self.liveness_interval, 10.0))
        next_probe = start + (landing_step if liveness else self.liveness_interval)
        while time.time() < deadline:
            lines = self._agent_lines(agent_id)
            idx = self._consumed.get(agent_id, 0)
            if len(lines) > idx:
                e = lines[idx]
                self._consumed[agent_id] = idx + 1
                self._landed[agent_id] = True
                return TurnOutcome(e.get("status", "continue"), e.get("note", ""))
            watching = liveness and self._landed.get(agent_id) is not True
            if liveness and time.time() >= next_probe:
                state = self._pane_state(agent_id)
                if state == "missing":
                    self.log(f"[headless] {agent_id} pane is gone mid-turn "
                             f"({self.sids.get(agent_id)})")
                    return TurnOutcome(DEAD_SESSION, "session pane vanished mid-turn")
                if state == "busy":
                    self._landed[agent_id] = True
                elif (watching and state == "idle"
                      and time.time() >= landing_deadline):
                    self._landed[agent_id] = False
                    self.log(f"[headless] {agent_id} prompt never landed "
                             f"({self.sids.get(agent_id)} idle at the composer "
                             f"after {self.landing_timeout:.0f}s)")
                    return TurnOutcome(UNLANDED, "prompt never landed "
                                                 "(pane idle at the composer)")
                next_probe = time.time() + (landing_step if self._landed.get(agent_id)
                                            is not True else self.liveness_interval)
            time.sleep(self.poll)
        self.log(f"[headless] TIMEOUT waiting for {agent_id} status ({timeout:.0f}s)")
        return TurnOutcome("timeout", "no status reported before timeout")

    def has_pending_report(self, agent_id: str) -> bool:
        """Guardian hook: a report from this agent landed but no turn consumed it
        yet (typically it arrived late, while recovery was under way)."""
        return len(self._agent_lines(agent_id)) > self._consumed.get(agent_id, 0)

    def take_pending_report(self, agent_id: str) -> Optional[TurnOutcome]:
        """Consume that pending report as the stuck turn's outcome (non-blocking)."""
        lines = self._agent_lines(agent_id)
        idx = self._consumed.get(agent_id, 0)
        if len(lines) <= idx:
            return None
        self._consumed[agent_id] = idx + 1
        e = lines[idx]
        return TurnOutcome(e.get("status", "continue"), e.get("note", ""))

    # ── Q2: transcript persistence ──
    def _capture_pane_text(self, agent_id: str) -> str:
        """Default capture source: pull this agent's pane scrollback over the
        daemon's `capture_pane` action. Best-effort — returns '' if the agent
        has no live session, the daemon predates the action, or the socket call
        fails, so persistence NEVER raises into a turn."""
        # Headless agents have no pane — their transcript IS the subprocess stdout.
        if agent_id in self._headless_output:
            return self._headless_output.get(agent_id, "")
        sid = self.sids.get(agent_id)
        if not sid:
            return ""
        try:
            res = self._call("capture_pane", {"sid": sid, "max_lines": 5000},
                             timeout=30.0)
        except Exception as e:  # noqa: BLE001 — capture is best-effort
            self.log(f"[headless] capture_pane {agent_id} failed: {e}")
            return ""
        return res.get("text", "") if isinstance(res, dict) else ""

    def _persist_transcript(self, agent_id: str, role: str, phase: str,
                            outcome: TurnOutcome) -> Optional[str]:
        """Write this agent turn's raw transcript to
        ``_output/<loop>/<agent>-<turn>.txt`` with a metadata header, a size cap,
        and a filesystem-safe name. Called at the END of every turn — including a
        wedged/timeout turn (whose pane is still alive, so its PARTIAL output is
        still captured). Fully fail-soft: any error is logged, never raised."""
        if not self.transcript_enabled:
            return None
        turn = self._turns.get(agent_id, 0)
        try:
            body = self._capture_fn(agent_id) or ""
            header = (
                f"# loop={self.loop} agent={agent_id} role={role} turn={turn}\n"
                f"# phase={phase} status={outcome.status} "
                f"note={outcome.notes!r}\n"
                f"# captured_at={time.time():.0f} sid={self.sids.get(agent_id, '-')}\n"
                f"{'-' * 72}\n")
            # Cap to the last N bytes of the transcript (the tail is where a wedge
            # shows), keeping the header intact so the file is always self-describing.
            cap = self.transcript_max_bytes
            if cap and len(body.encode("utf-8", "replace")) > cap:
                raw = body.encode("utf-8", "replace")[-cap:]
                body = ("[…transcript truncated to last "
                        f"{cap} bytes…]\n" + raw.decode("utf-8", "replace"))
            path = report.transcript_path(self.loop, agent_id, turn)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(header + body)
            os.replace(tmp, path)
            return path
        except Exception as e:  # noqa: BLE001 — persistence must not fail a turn
            self.log(f"[headless] persist transcript {agent_id} failed: {e}")
            return None

    # ── session liveness: stale-sid re-resolution ──
    def _pane_state(self, agent_id: str) -> Optional[str]:
        """Probe this agent's pane: "missing" (definitely gone), "busy" (a turn is
        running), "idle" (REPL up at the composer, nothing running), "live"
        (alive, state unclear), or None when unknown (no sid / probe failed / a
        daemon predating `missing`). Only "missing" ever triggers a respawn and
        only "idle" a re-delivery — anything unclear keeps waiting."""
        sid = self.sids.get(agent_id)
        if not sid:
            return None
        try:
            res = self._call("capture_pane", {"sid": sid, "max_lines": 40}, timeout=30.0)
        except Exception as e:  # noqa: BLE001 — a probe never fails a turn
            return "missing" if _is_dead_session_error(e) else None
        if not isinstance(res, dict):
            return None
        if res.get("missing"):
            return "missing"
        # Only the visible tail: finished-turn lines ("✻ Cogitated…") and older
        # footers in the scrollback say nothing about NOW.
        tail = "\n".join([ln for ln in str(res.get("text", "")).splitlines()
                          if ln.strip()][-12:])
        if any(m in tail for m in _PANE_BUSY_MARKERS):
            return "busy"
        if any(m in tail for m in _PANE_READY_MARKERS):
            return "idle"
        return "live"

    def _forget_session(self, agent_id: str, why: str) -> None:
        """Drop a stale sid so the next delivery spawns a fresh session. An
        adopted session that died can't be re-adopted — it is respawned like any
        other interactive agent rather than parking the loop."""
        sid = self.sids.pop(agent_id, None)
        self._adopted.discard(agent_id)
        self.log(f"[headless] {agent_id} session {sid} is gone ({why}) → will respawn")

    def _send(self, agent_id: str, role: str, pointer: str) -> bool:
        """Deliver a turn pointer to this agent's session: spawn on first use (or
        after its sid went stale), else inject. An inject that fails because the
        pane is gone re-resolves by spawning a FRESH session with the same pointer
        as its initial prompt. Returns True if a (re)spawn happened (cold boot).
        Once shutdown() began nothing is injected or spawned: raises LoopStopped."""
        if self._stopped:
            raise LoopStopped(f"loop stopped — not delivering to {agent_id}")
        self._landed.pop(agent_id, None)
        if agent_id in self.sids:
            try:
                res = self._call("inject_input", {
                    "sid": self.sids[agent_id], "text": pointer,
                    "wait_for_idle": True, "idle_timeout_sec": 45,
                }, timeout=90.0)
                # The daemon verifies a turn started ("esc to interrupt") before
                # returning submitted=True; False/absent ⇒ the landing watch decides.
                if isinstance(res, dict) and res.get("submitted") is True:
                    self._landed[agent_id] = True
                return False
            except Exception as e:  # noqa: BLE001
                if not _is_dead_session_error(e):
                    raise
                self._forget_session(agent_id, str(e))
        self._spawn(agent_id, role, pointer)
        return True

    def _deliver_and_wait(self, agent_id: str, role: str, pointer: str,
                          first: bool = False) -> TurnOutcome:
        """Send the pointer, then wait for the report — respawning the session and
        re-delivering the SAME pointer (same turn, same prompt file) whenever the
        pane dies under the turn, up to max_respawns times. A turn is never lost
        (or escalated to the guardian) merely because its sid went stale."""
        respawns = redelivers = 0
        while True:
            # A stop mid-turn archives the pane → the liveness probe reads it as
            # DEAD_SESSION; re-spawning then would orphan a fresh session the
            # stop was meant to reap (loopyard-bug-1790562425). Abort the turn.
            if self._stopped:
                return TurnOutcome("timeout", "loop stopped mid-turn (no respawn)")
            try:
                cold = self._send(agent_id, role, pointer)
            except LoopStopped as e:
                return TurnOutcome("timeout", str(e))
            if cold and not first:
                respawns += 1
            outcome = self._wait_status(agent_id, self._timeout_for(agent_id, first or cold),
                                        liveness=True)
            first = False
            if outcome.status == UNLANDED:
                # Dropped prompt: the pane is alive but idle at the composer. Re-type
                # the SAME pointer (a doubled, unsubmitted copy in the box is
                # harmless); once the budget is spent, fail the turn NOW so the
                # guardian starts recovery in ~landing_timeout, not the full budget.
                if redelivers >= self.max_redelivers:
                    return TurnOutcome("timeout", f"prompt never landed after "
                                                  f"{redelivers + 1} deliveries")
                redelivers += 1
                self.log(f"[headless] re-delivering {agent_id}'s prompt "
                         f"({redelivers}/{self.max_redelivers})")
                continue
            if outcome.status != DEAD_SESSION:
                return outcome
            self._forget_session(agent_id, outcome.notes)
            if respawns >= self.max_respawns:
                return TurnOutcome("timeout", f"session died {respawns + 1}x this turn "
                                              "(respawn budget spent)")

    # ── Substrate contract ──
    def _spawn(self, agent_id: str, role: str, initial_prompt: str) -> str:
        if self._stopped:                            # like _subprocess_headless: no spawn after stop
            raise LoopStopped(f"loop stopped — not spawning {agent_id}")
        with self._spawn_lock:
            window = self._next_window
            self._next_window += 1
        params = {
            "slug": self.slug, "window": str(window),
            "initial_prompt": initial_prompt, "role": role, "owner": self.owner,
        }
        # P1c: hand the daemon the exact data dir the server reads, so the agent
        # reports into it (not the daemon's own default). Omitted when unset so a
        # daemon that predates the param is never sent an unexpected key.
        if self.loops_data_dir:
            params["loops_data_dir"] = self.loops_data_dir
        # Loop-Workspaces: hand the daemon the engine-provisioned worktree as the
        # session cwd, detached from the slug's projects.toml repo_path. Omitted
        # when unset so a legacy loop (no workspace) and a daemon that predates the
        # param are byte-for-byte unchanged — exactly like `loops_data_dir`.
        if self.workspace:
            params["workspace"] = self.workspace
        # multi-CLI passthrough gate: only when ON do we add `model`/`runtime` to
        # the spawn params. OFF (default) ⇒ neither key ⇒ the request is exactly
        # what it was before this change (claude, host-default model). A `runtime`
        # of "claude" is elided even when ON — it is the daemon default, so sending
        # it would needlessly diverge the request from the historical one.
        runtime, model = self.spawn_cli(agent_id)
        if model:
            params["model"] = model
        if runtime != "claude":
            params["runtime"] = runtime
        # Role caps: a manager / input_provider pane gets its permission profile
        # (the daemon writes it + runs `claude --permission-mode dontAsk
        # --settings`). Uncapped roles / LOOPS_ROLE_CAPS=0 ⇒ no key ⇒ the request
        # is byte-for-byte today's. A profile error raises: never spawn uncapped.
        caps = self._role_caps_profile(role, self._slug_cwd())
        if caps is not None:
            params["role_caps"] = caps
        # Loop cage: the daemon wraps the pane CLI in the loop slice + exec stage
        # (and re-verifies the slice limits). Off ⇒ no key ⇒ today's request.
        if self._cage is not None:
            params["sandbox"] = sandbox.to_spec(self._cage_policy())
        res = self._call("spawn_session", params, timeout=self.spawn_timeout)
        sid = res.get("sid")
        if not res.get("ok") or not sid:
            raise RuntimeError(f"spawn_session for {agent_id} returned {res}")
        if self._stopped:
            # shutdown() landed while spawn_session was in flight: its sid sweep
            # never saw this session — reap it here rather than orphan it.
            self._reap_session(agent_id, sid)
            raise LoopStopped(f"loop stopped during spawn of {agent_id}")
        self.sids[agent_id] = sid
        self._roles[agent_id] = role
        self.log(f"[headless] spawned {agent_id} → {sid} (window {window})")
        return sid

    def _register_adopted(self, agent_id: str, role: str) -> None:
        """Make an ADOPTED session a first-class agent to the daemon: stamp its
        role + this loop into the daemon's session registry before we drive it.

        A briefed / Debrief-resurrected manager was not spawned by THIS run, and
        can reach the daemon ROLELESS — every role-gated op (sleep_expert,
        respawn_expert) then answers ``session has no role`` and the manager is
        never treated like a spawned one past turn 1 (loopyard-bug-1790190791).
        Best-effort: a daemon that predates the action is logged once; a dead
        pane is left to the normal inject → respawn path."""
        if agent_id not in self._adopted or agent_id in self._role_registered:
            return
        sid = self.sids.get(agent_id)
        if not sid:
            return
        self._role_registered.add(agent_id)
        try:
            self._call("register_session_role", {"slug": self.slug, "sid": sid,
                                                 "role": role, "loop": self.loop})
            self.log(f"[headless] registered adopted {agent_id} ({sid}) as role={role}")
        except Exception as e:  # noqa: BLE001 — registration must not block the turn
            self.log(f"[headless] register role for adopted {agent_id} ({sid}) failed: {e}")

    def brief_manager(self, agent_id: str, goal: str, owner_ask: OwnerChannel) -> None:
        self._roles.setdefault(agent_id, "manager")
        self._register_adopted(agent_id, "manager")
        if agent_id in self._adopted:
            # ADOPTED: the owner already briefed this live session; it BECOMES the
            # manager in-place. Inject the handoff into the existing sid — never
            # spawn a fresh one — so the whole owner<->session dialogue IS the
            # manager's live context (a continuation, not a copy or a summary).
            brief = (
                f"You have been working with the owner in THIS session to shape loop "
                f"'{self.loop}', and you are now its MANAGER -- the same conversation "
                f"continues, nothing resets.\n\n## THE GOAL (hold this every turn)\n{goal}\n\n"
                "The crew is forming under you. First manager turn: confirm you're "
                "driving it per what you and the owner just worked out, then end the "
                "turn by reporting status `continue`.")
            pointer = self._deliver(agent_id, "manager", brief)
        else:
            brief = (
                f"You are the MANAGER and north-star holder of loop '{self.loop}'.\n\n"
                f"## THE GOAL (hold this in full, every turn)\n{goal}\n\n"
                "This is your briefing turn. Acknowledge you understand the goal and how "
                "you'll drive it, then end the turn by reporting status `continue`.")
            pointer = self._deliver(agent_id, "manager", brief)
        outcome = self._deliver_and_wait(agent_id, "manager", pointer, first=True)
        self._persist_transcript(agent_id, "manager", "brief", outcome)

    def run_turn(self, agent_id: str, role: str, prompt: str, *,
                 context_cap: int, phase: str, kind: str = "") -> TurnOutcome:
        self._roles.setdefault(agent_id, role)
        if self._is_headless(agent_id, role):
            # One-shot `claude -p`: no persistent session, so no pointer file / inject
            # and nothing to reap. The full framed prompt goes straight to stdin.
            ix = self._begin_turn(agent_id)
            framed = self._frame(agent_id, role, prompt)
            outcome = self._run_headless(
                agent_id, role, framed, self._timeout_for(agent_id, first=(ix == 1)))
            self._persist_transcript(agent_id, role, phase, outcome)
            return outcome
        # ── interactive path (manager / adopted / service): persistent session ──
        self._register_adopted(agent_id, role)
        pointer = self._deliver(agent_id, role, prompt)
        # first turn primes via initial_prompt; a stale sid is respawned + the
        # pointer re-delivered. Per-agent turn budget (config maxTurnMinutes;
        # cold-boot floor on a first/respawned turn).
        outcome = self._deliver_and_wait(agent_id, role, pointer,
                                         first=agent_id not in self.sids)
        # Q2: persist the turn's transcript (partial on timeout — pane still alive).
        self._persist_transcript(agent_id, role, phase, outcome)
        return outcome

    def compact(self, agent_id: str) -> None:
        # The scribe reads Claude transcripts; other CLIs manage their own context.
        if self.spawn_cli(agent_id)[0] != "claude":
            return
        if not self.compact_enabled or agent_id not in self.sids:
            return
        if self._turns.get(agent_id, 0) % self.compact_every != 0:
            return                                        # gate: only every N turns
        try:
            self._call("sleep_expert", {"slug": self.slug, "sid": self.sids[agent_id]},
                       timeout=200.0)
            self.log(f"[headless] compacted {agent_id}")
        except Exception as e:  # noqa: BLE001 — compaction is best-effort
            self.log(f"[headless] compact {agent_id} skipped: {e}")
            if "not available in" in str(e):
                # R30: a user-worker refuses sleep_expert (coordinator_only);
                # stop asking for the rest of the run so it logs once.
                self.compact_enabled = False

    def _reap_session(self, agent_id: str, sid: str) -> None:
        try:
            self._call("suspend_session", {"slug": self.slug, "sid": sid})
            self._call("archive_session", {"slug": self.slug, "sid": sid})
            self.log(f"[headless] suspended+archived {agent_id} ({sid})")
        except Exception as e:  # noqa: BLE001
            self.log(f"[headless] cleanup {agent_id} failed: {e}")

    def shutdown(self) -> None:
        # FIRST reap every in-flight headless worker (claude -p + its child browsers)
        # so a stop/finish never leaves them running and starving the box — the
        # headless analogue of suspending a tmux session. Also unblocks any thread
        # parked in communicate() so the run loop can honour should_stop promptly.
        self._reap_procs()
        for agent_id, sid in list(self.sids.items()):
            self._reap_session(agent_id, sid)
        # Step 5: sessions are suspended and the branch is already in git — now
        # stop the loop slice so nothing the agents left behind keeps running.
        self.teardown_cage()

    # ── guardian hooks (recovery) ──
    def guardian_renudge(self, agent_id: str) -> bool:
        """Cheap recovery: remind a stuck agent to run its report command. One
        line → one clean inject. Returns False if the agent has no live session."""
        sid = self.sids.get(agent_id)
        if not sid:
            return False
        role = self._roles.get(agent_id, "worker")
        cmd = (f'{self._report_prefix()} '
               f'{self.loop} {agent_id} <{self._VOCAB.get(role, "completed")}> "<note>"')
        nudge = ("You have not reported your status for this turn yet. If your work "
                 "is done, END THE TURN NOW by running exactly: " + cmd)
        if self._landed.get(agent_id) is False:
            # The turn's prompt never landed: "report now" would end a turn whose
            # work never started. The runner's retry re-delivers the turn's prompt
            # (landing-watched again), so there is nothing to nudge — just retry.
            self.log(f"[headless] {agent_id}'s prompt never landed → retry re-delivers")
            return True
        try:
            self._call("inject_input", {"sid": sid, "text": nudge,
                                        "wait_for_idle": True, "idle_timeout_sec": 30},
                       timeout=90.0)
            self.log(f"[headless] re-nudged {agent_id}")
            return True
        except Exception as e:  # noqa: BLE001
            if _is_dead_session_error(e):
                # Stale sid, not a wedge: forget it so the guardian's retry of the
                # step spawns a fresh session and re-delivers the turn's prompt.
                self._forget_session(agent_id, str(e))
                return True
            self.log(f"[headless] re-nudge {agent_id} failed: {e}")
            return False

    def guardian_service(self, problem: dict, attempt: int, max_attempts: int) -> TurnOutcome:
        """Escalated recovery: a service agent diagnoses the stuck turn and
        decides whether another retry is worth it. It runs as its own long-lived
        session ('__service') and reports status retry|give_up."""
        svc = "__service"
        agent = problem.get("agent", "?")
        stuck_sid = self.sids.get(agent, "?")
        brief = (
            f"You are the SERVICE agent (recovery) for loop '{self.loop}'.\n"
            f"Problem: agent '{agent}' (role {problem.get('role')}, session {stuck_sid}) "
            f"did not report within its turn budget in phase {problem.get('phase')} — "
            f"attempt {attempt}/{max_attempts}. A re-nudge was already sent.\n"
            "Diagnose: is it genuinely WEDGED, or slow-but-productive (mid-build, "
            "watching CI, etc. — just needs more time)? END your turn with the report "
            "command, choosing:\n"
            " - alive + doing valuable work, needs more time → report `retry` AND put "
            "`EXTEND=<minutes>` in your note (e.g. EXTEND=60): the loop grants that agent "
            "a longer per-turn budget so it stops timing out.\n"
            " - just needs one more normal attempt → report `retry` (no EXTEND).\n"
            " - genuinely wedged beyond recovery → report `give_up`.")
        first = svc not in self.sids
        pointer = self._deliver(svc, "service", brief)
        outcome = self._deliver_and_wait(svc, "service", pointer, first=first)
        self._persist_transcript(svc, "service", "guardian", outcome)
        return outcome
