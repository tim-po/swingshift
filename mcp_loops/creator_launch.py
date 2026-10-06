"""Interactive loop-creator session launcher (QoL-R5 / C2).

The '+ loop' split view's RIGHT pane is the Phase-2a web terminal (a PTY over a
gate-authed WebSocket). Instead of a bare login shell, that PTY runs THIS module,
which turns the terminal into an interactive loop-creator session:

  1. It builds the loop-creator preprompt via :func:`creator.build_prompt`, seeded
     with the C1 context store (``creator_context.read_all``) — so the OPENING
     screen of the session VISIBLY contains the trusted-background context. This
     is the C2 acceptance point: the seed is on-screen before the user types.
  2. It writes the preprompt to a per-session temp file and prints the exact
     command to start a real creator CLI (claude / codex) from it.
  3. It then hands the terminal to an interactive session:
       * default (safe): an interactive ``$SHELL`` so the operator can start the
         CLI, paste, or converse — the prompt is already on screen + on disk;
       * with ``LOOPYARD_CREATOR_AUTOSTART=1`` and the runtime CLI on PATH: it
         EXECs that CLI seeded with the preprompt (the full interactive creator).
     ``LOOPYARD_CREATOR_NOEXEC=1`` forces the shell fallback (used by the live
     demo / tests so verification never spends model tokens).

Runs inside the PTY the web terminal already tears down per-session (own pgroup,
concurrency cap, idle reap) — this module adds NO privilege and no network of its
own; it only assembles text and execs an interactive process in the same session.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

from mcp_loops import creator, creator_context

VALID_RUNTIMES = creator.VALID_RUNTIMES  # ("claude", "codex")

# ANSI dim/bold for the banner — the terminal is xterm-256color, and these are
# the same escapes the rest of the web terminal already emits.
_DIM = "\x1b[90m"
_BOLD = "\x1b[1m"
_EMBER = "\x1b[38;5;208m"
_RST = "\x1b[0m"


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def build_seeded_prompt() -> str:
    """The loop-creator preprompt seeded with the C1 context store. Empty
    profile/instructions ⇒ the generic creator preprompt (asks the material
    questions first); an empty store still yields a working default (C1)."""
    context = creator_context.read_all()
    return creator.build_prompt("", "", context=context)


def _cli_command(runtime: str, prompt_path: str) -> list[str] | None:
    """The argv to start the chosen creator CLI seeded with the preprompt file,
    or None if that runtime CLI isn't on PATH. Both claude and codex take an
    initial prompt as a positional arg; we pass the file's contents."""
    from mcp_loops import runtimes
    runtime, model = runtimes.selection(runtime)
    exe = shutil.which(runtimes.binary(runtime))
    if not exe:
        return None
    # Read the prompt back at exec time via the shell? No — pass the text
    # directly so there is no shell-quoting hazard. The caller reads the file.
    try:
        with open(prompt_path, "r", encoding="utf-8") as fh:
            prompt = fh.read()
    except OSError:
        return None
    if runtime == "cursor":
        return runtimes.argv(runtime, model, executable=exe) + [prompt]
    if runtime == "codex":
        # codex on this host needs the sandbox bypass (bwrap fails); see the
        # coordinator's known-limits note. Keep it interactive.
        return runtimes.argv(runtime, model, executable=exe) + [prompt]
    return [exe, prompt]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mcp_loops.creator_launch", add_help=True)
    ap.add_argument("--runtime", default=os.environ.get("LOOPYARD_CREATOR_RUNTIME",
                                                        "claude"))
    args = ap.parse_args(argv)
    runtime = args.runtime.strip().lower()
    if runtime not in VALID_RUNTIMES:
        runtime = "claude"

    prompt = build_seeded_prompt()
    docs = creator_context.list_docs()
    fd, prompt_path = tempfile.mkstemp(prefix="creator-prompt-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(prompt)

    # ── the opening screen: banner + the seeded preprompt (contains the seed) ──
    seeded = ", ".join(d["name"] for d in docs) or "(none — default preprompt)"
    sys.stdout.write(
        f"{_EMBER}{_BOLD}Loopyard loop-creator{_RST} — interactive session "
        f"(runtime: {runtime})\n"
        f"{_DIM}Seed context docs: {seeded}\n"
        f"The preprompt below is what the creator starts from. Trusted background "
        f"is framed as NOT-instructions.{_RST}\n"
        f"{_DIM}{'─' * 72}{_RST}\n")
    sys.stdout.write(prompt.rstrip("\n") + "\n")
    sys.stdout.write(f"{_DIM}{'─' * 72}{_RST}\n")
    sys.stdout.write(
        f"{_DIM}Preprompt saved to {prompt_path}\n"
        f"Start the creator CLI with it, e.g.:  {runtime} \"$(cat {prompt_path})\"\n"
        f"When it emits a ```json config, click {_RST}{_BOLD}Apply → editor{_RST}"
        f"{_DIM} on the left, then Validate + Save.{_RST}\n\n")
    sys.stdout.flush()

    # ── hand off to an interactive session ────────────────────────────────────
    noexec = _truthy(os.environ.get("LOOPYARD_CREATOR_NOEXEC"))
    autostart = _truthy(os.environ.get("LOOPYARD_CREATOR_AUTOSTART"))
    cli = None if noexec else (_cli_command(runtime, prompt_path) if autostart else None)
    if cli is not None:
        sys.stdout.write(f"{_DIM}Starting {runtime}…{_RST}\n")
        sys.stdout.flush()
        os.execvp(cli[0], cli)                    # replaces this process in the PTY
        return 0                                  # unreachable if exec succeeds

    # Safe default: an interactive shell in the same PTY. The operator converses
    # by starting the CLI (command shown above) or pastes into their own tools.
    shell = os.environ.get("SHELL") or "/bin/bash"
    os.execvp(shell, [shell, "-i"])
    return 0


if __name__ == "__main__":            # pragma: no cover — exercised via the PTY
    raise SystemExit(main())
