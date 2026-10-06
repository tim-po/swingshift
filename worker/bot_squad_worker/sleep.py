"""Sleep cycle — compact a running expert's recent transcript into its
markdown memory files.

The mechanism (v1):

1. Read the session md to resolve role + claude_uuid + cwd.
2. Locate Claude Code's per-session jsonl
   (``~/.claude/projects/<encoded-cwd>/<claude_uuid>.jsonl``).
3. Drain transcript from a high-water mark (``sleep_mark`` file under
   the expert dir) to current EOF.
4. Spawn a one-shot ``claude -p`` scribe in the expert's data directory,
   prompted to update ``memory/`` files based on the recent transcript
   and the existing memory.
5. Advance the sleep mark to current EOF.

The pane itself keeps running. The memory benefit lands on next pane
respawn (the SessionStart hook prints identity.md + can reference
MEMORY.md). Pane respawn is deferred to a future cut.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from bot_squad_worker import sessions as _sessions


# How many bytes of recent transcript to feed the scribe. Anything larger
# is tail-clipped — older entries are lost from this sleep but stay in
# the jsonl, so the next sleep would re-include them if we widened the
# budget. Conservative default; tune by inspecting what the scribe writes.
_MAX_TRANSCRIPT_BYTES = 200_000

# Hard cap on scribe runtime. The scribe is a single non-interactive
# claude invocation; usually finishes in 10–30s.
_SCRIBE_TIMEOUT_SEC = 300


SLEEP_PROMPT = """\
You are the SCRIBE for the {role} expert in the bot-swarm project at
slug={slug}. You are not the expert. You will write/update markdown
memory files on the expert's behalf.

The expert's memory directory is at the current working directory's
`memory/` subdirectory. The expert's identity.md is at `./identity.md`.

Your job:
1. Read `./identity.md` so you know what this expert is responsible for.
2. Read every existing file under `./memory/` so you know what's already
   documented. The index is `./memory/MEMORY.md`.
3. Read the recent transcript below.
4. Update existing memory files in place, or create new topical files
   under `./memory/`, so that an expert reading only `identity.md` +
   `MEMORY.md` + the topical files referenced from MEMORY.md could
   resume this expert's work without surprises.
5. Keep `./memory/MEMORY.md` as a thin index: one-line pointers to
   topical files. No prose in the index.
6. Prefer updating existing files over creating new ones. Don't create
   a file per turn — one file per topic.

PRESERVE in memory:
  - Specific decisions made, with the reason.
  - Gotchas / surprises / things that didn't work.
  - File paths the expert needs to remember.
  - Working agreements with peers (who owns what, conventions).
  - Pinned facts (project scope, library choices, schema shapes).

DROP from memory:
  - Conversational filler.
  - Tool-call mechanics (exact curl invocations, parameter shuffling).
  - Errors the expert recovered from cleanly.
  - The transcript itself — you are SUMMARIZING into structured memory,
    not archiving raw dialogue.

When you are done, your final assistant message must be ONLY a JSON
object on a single line, no prose around it, of the form:
  {{"ok": true, "files_touched": ["MEMORY.md", "decisions.md", ...]}}

Do not include backticks or markdown around the JSON. Just the line.

--- RECENT TRANSCRIPT ---
{transcript}
--- END TRANSCRIPT ---
"""


def _encode_cwd_for_claude_projects(cwd: str) -> str:
    """Encode a cwd path the way Claude Code names its project dirs.

    Current Claude Code (2.x) keeps the leading ``-`` from the path's
    leading slash. (Older versions stripped it; ``sessions.discover_claude_uuid``
    is wired for that older behavior and is left alone — it's only used
    by listings where mtime-fallback covers the gap.)
    """
    return cwd.replace("/", "-")


def _resolve_jsonl_path(meta: dict, user_home: Path) -> Path | None:
    """Find the Claude jsonl for a session given its md frontmatter.

    Returns None if the session md is missing claude_uuid or cwd, or if
    the file isn't on disk. Tries both the with-leading-dash encoding
    (current Claude Code) and the lstripped form (older) for resilience.
    """
    claude_uuid = meta.get("claude_uuid")
    cwd = meta.get("cwd")
    if not claude_uuid or not cwd or claude_uuid == "~" or cwd == "~":
        return None
    candidates = [
        user_home / ".claude" / "projects" / _encode_cwd_for_claude_projects(cwd) / f"{claude_uuid}.jsonl",
        user_home / ".claude" / "projects" / cwd.replace("/", "-").lstrip("-") / f"{claude_uuid}.jsonl",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _extract_transcript(jsonl_path: Path, from_offset: int) -> tuple[str, int]:
    """Read jsonl from ``from_offset`` to EOF; return a human-readable
    transcript plus the new EOF offset.

    Only ``user`` and ``assistant`` records are extracted. Within an
    assistant message, text blocks are joined plain; tool_use is reduced
    to a single line ``[tool {name}] <one-line summary>``; tool_result is
    dropped (too noisy and rarely informative for memory).
    """
    if not jsonl_path.is_file():
        return "", from_offset
    new_offset = jsonl_path.stat().st_size
    if new_offset <= from_offset:
        return "", from_offset

    with jsonl_path.open("rb") as f:
        f.seek(from_offset)
        buf = f.read(new_offset - from_offset)

    lines: list[str] = []
    for raw_line in buf.decode("utf-8", errors="replace").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            rec = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        kind = rec.get("type")
        if kind == "user":
            msg = rec.get("message", {}) or {}
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                lines.append(f"USER: {content.strip()}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            lines.append(f"USER: {text}")
        elif kind == "assistant":
            msg = rec.get("message", {}) or {}
            content = msg.get("content")
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            parts.append(text)
                    elif btype == "tool_use":
                        name = block.get("name", "tool")
                        ip = block.get("input") or {}
                        summary = _summarize_tool_use(name, ip)
                        parts.append(f"[tool {name}] {summary}")
                if parts:
                    lines.append("ASSISTANT: " + "\n".join(parts))
    transcript = "\n\n".join(lines)
    return transcript, new_offset


def _summarize_tool_use(name: str, ip: dict) -> str:
    """One-line summary of a tool_use input for transcript readability."""
    if name == "Bash":
        cmd = (ip.get("command") or "").splitlines()
        return cmd[0][:160] if cmd else ""
    if name in ("Edit", "Write", "Read"):
        return str(ip.get("file_path") or "")
    if name == "Grep":
        return f"{ip.get('pattern','')!r} in {ip.get('path','')}"[:160]
    # Generic — first 160 chars of repr.
    return repr(ip)[:160]


def _sleep_mark_path(cfg: Any, slug: str, role: str) -> Path:
    return Path(cfg.data_dir) / slug / "experts" / role / "sleep_mark"


def _read_mark(p: Path) -> int:
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip())
    except ValueError:
        return 0


def _user_home() -> Path:
    return Path(os.path.expanduser("~"))


def _run_scribe(prompt: str, cwd: Path) -> tuple[int, str, str]:
    """Run a one-shot ``claude -p`` scribe with the prompt on stdin,
    rooted at ``cwd``. Returns (returncode, stdout, stderr).

    --dangerously-skip-permissions so the scribe can Read/Write/Edit
    inside the expert's memory dir without permission prompts.
    """
    proc = subprocess.run(
        ["claude", "-p", "--dangerously-skip-permissions"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_SCRIBE_TIMEOUT_SEC,
        input=prompt,
    )
    return proc.returncode, proc.stdout, proc.stderr


def sleep_expert(cfg: Any, slug: str, sid: str) -> dict:
    """Compact an expert's recent transcript into its memory files.

    Returns ``{"ok": True, "role": ..., "compacted_bytes": int,
               "scribe_stdout_tail": str, "mark_before": int,
               "mark_after": int}``.

    Raises ActionError on:
      - unknown slug / missing session md
      - session md without role (this is bot-swarm-only; bot-squad
        sessions without a role can't be slept)
      - jsonl not findable
      - scribe non-zero exit
    """
    from bot_squad_worker.actions import ActionError

    if slug not in cfg.projects:
        raise ActionError(f"sleep_expert: unknown slug {slug!r}")

    sess_path = _sessions._session_file(Path(cfg.data_dir), slug, sid)
    meta = _sessions._read_session_metadata(sess_path)
    if meta is None:
        raise ActionError(f"sleep_expert: no session md for {sid!r}")
    role = meta.get("role")
    if not role or role == "~":
        raise ActionError(
            f"sleep_expert: session {sid!r} has no role; only bot-swarm "
            "sessions can sleep"
        )

    jsonl = _resolve_jsonl_path(meta, _user_home())
    if jsonl is None:
        raise ActionError(
            f"sleep_expert: could not resolve jsonl for {sid!r} "
            f"(uuid={meta.get('claude_uuid')!r}, cwd={meta.get('cwd')!r})"
        )

    mark_path = _sleep_mark_path(cfg, slug, role)
    mark_before = _read_mark(mark_path)

    transcript, mark_after = _extract_transcript(jsonl, mark_before)
    if not transcript:
        return {
            "ok": True,
            "role": role,
            "compacted_bytes": 0,
            "mark_before": mark_before,
            "mark_after": mark_after,
            "scribe_stdout_tail": "",
            "note": "no new transcript since last sleep",
        }

    if len(transcript.encode("utf-8")) > _MAX_TRANSCRIPT_BYTES:
        # Tail-clip: keep the most recent _MAX_TRANSCRIPT_BYTES of UTF-8.
        # Older content drops from THIS sleep but stays in the jsonl
        # past the mark, so the next sleep can re-include it.
        encoded = transcript.encode("utf-8")[-_MAX_TRANSCRIPT_BYTES:]
        transcript = encoded.decode("utf-8", errors="replace")

    expert_dir = Path(cfg.data_dir) / slug / "experts" / role
    expert_dir.mkdir(parents=True, exist_ok=True)

    prompt = SLEEP_PROMPT.format(role=role, slug=slug, transcript=transcript)

    rc, stdout, stderr = _run_scribe(prompt, cwd=expert_dir)
    if rc != 0:
        raise ActionError(
            f"sleep_expert: scribe exited with code {rc}; "
            f"stderr={stderr[-500:]!r}"
        )

    mark_path.parent.mkdir(parents=True, exist_ok=True)
    mark_path.write_text(str(mark_after))

    return {
        "ok": True,
        "role": role,
        "compacted_bytes": mark_after - mark_before,
        "mark_before": mark_before,
        "mark_after": mark_after,
        "scribe_stdout_tail": stdout[-500:],
    }
