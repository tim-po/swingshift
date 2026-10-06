"""Q3 observability — terminal loop failures become durable, structured ISSUES.

When a run ends in a genuine failure (the guardian gave up, or the run errored),
the context evaporates the moment the tmux panes die and the next run overwrites
``run.json``. This module turns that terminal failure into a structured issue on
disk: one file per issue under ``<data>/_issues/<id>.json`` plus an append-only
``index.jsonl`` the dashboard lists from.

The WRITER sits behind a tiny adapter — :class:`IssueSink` — so a future GitHub
backend (``gh issue create``) can drop in without touching the terminal-fail
hook. ``LocalIssueSink`` is the default; :class:`GhIssueSink` implements the same
three-method contract (``file_issue`` / ``list_issues`` / ``get_issue``) over the
``gh`` CLI and is OPT-IN only (:func:`select_issue_sink` — ``LOOPS_ISSUE_SINK=gh``
plus an explicit ``LOOPS_GH_ISSUE_REPO``).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from typing import Callable, Optional, Protocol

from mcp_loops import origins, report

# The run outcomes that warrant an issue: the guardian exhausted recovery, or the
# run threw. A user stop ("stopped"), budget exhaustion ("turn_limit") and the
# healthy ends ("complete"/"early_finish") are NOT failures.
TERMINAL_FAILURES = frozenset({"guardian_stopped", "error"})


def is_terminal_failure(ended: str) -> bool:
    return ended in TERMINAL_FAILURES


# ── the reactive-stream data model (REDESIGN-SPEC §3 rd-issues / §6) ─────────
# The redesign turns "Issues" from an engine crash diary into a project-scoped
# reactive stream that loops FILL (file-and-keep-going) and loops DRAIN (point a
# loop at one → it resolves). Four fields define the shape the UI reflects:
#
#   project  — the project it belongs to, or None when unconfident (the surface
#              LABELS that "Unattributed"; the label is never stored, so it can't
#              read as a real project id). Never laundered into one bucket.
#   source   — WHO raised it: ``you`` (a human), ``loop:<name>`` (a loop set it
#              down mid-work), or ``engine`` (a crash the guardian detected).
#   kind     — WHAT it is: crash / bug / follow-up / blocked / idea-overflow.
#   status   — a real lifecycle, not just open/closed.
KINDS = ("crash", "bug", "follow-up", "blocked", "idea-overflow")
SEVERITIES = ("low", "normal", "high")

# The status lifecycle and its legal transitions (the triage state machine).
# ``resolving`` is the "a loop is pointed at this" state; a terminal state
# (resolved/dismissed) can still be reopened → open. No transition ever fakes a
# resolution: only an explicit triage or a loop's real result moves a card.
STATUSES = ("open", "snoozed", "resolving", "resolved", "dismissed")
STATUS_TRANSITIONS = {
    "open":      {"snoozed", "resolving", "resolved", "dismissed"},
    "snoozed":   {"open", "resolving", "resolved", "dismissed"},
    "resolving": {"open", "resolved", "dismissed"},
    "resolved":  {"open"},          # reopen only
    "dismissed": {"open"},          # reopen only
}


def normalize_source(source: Any, *, loop: Optional[str] = None) -> str:
    """Coerce a source into the ``you`` / ``loop:<name>`` / ``engine`` vocab.
    A bare ``loop`` name (with no explicit ``loop:`` prefix) plus a loop hint
    yields ``loop:<name>``; anything unrecognised falls back to ``you`` so a
    filed issue always carries an honest, well-formed provenance."""
    s = (source or "").strip() if isinstance(source, str) else ""
    if s == "engine":
        return "engine"
    if s.startswith("loop:") and len(s) > len("loop:"):
        return s
    if s == "loop" and loop:
        return f"loop:{loop}"
    if s and s != "you" and loop and s == loop:
        return f"loop:{loop}"
    return "you"


def normalize_read(row: dict) -> dict:
    """Repair the reactive-stream fields on a row/record READ from the store,
    deriving HONEST values for **legacy crash rows** filed before the stream model
    added ``kind``/``source``/``title`` (those older rows carry only the forensic
    ``loop``/``failing_*``/``ended`` keys, so the stream would otherwise show a
    ``(untitled)`` card with a null source — the exact gap uxui flagged).

    A row is a crash iff it names a ``loop`` and any of ``failing_agent`` /
    ``failing_status`` / ``ended`` / ``run`` — a provable fact about the record, so
    the derivation invents nothing:

    * ``kind``     → ``"crash"``   (that is literally what these records are);
    * ``source``   → ``"engine"``  (a crash is engine-detected — the honest source);
    * ``severity`` → ``"high"``    (matches :func:`build_issue`);
    * ``title``    → ``"<loop> crashed (<ended>)"`` — the real event, not a guess.

    Only ABSENT/empty fields are filled; a well-formed filed issue passes through
    untouched. Pure: returns a new dict, never mutates the input, never raises."""
    if not isinstance(row, dict):
        return row
    loop = row.get("loop")
    is_crash = bool(loop) and any(
        row.get(k) for k in ("failing_agent", "failing_status", "ended", "run"))
    if not is_crash:
        return row
    out = {**row}
    if not out.get("kind"):
        out["kind"] = "crash"
    if not out.get("source"):
        out["source"] = "engine"
    if not out.get("severity"):
        out["severity"] = "high"
    if not out.get("title"):
        ended = out.get("ended") or out.get("failing_status") or "?"
        out["title"] = f"{loop} crashed ({ended})"
    return out


# ── loop ORIGIN on issue rows (loopyard-bug-1790089839) ──────────────────────
# An issue names its loop by NAME only; the UI needs the loop's origin (host) too
# or a loop on a remote origin opens ``/loops/local/<name>`` — the wrong detail.
# Backfill-free: a row that doesn't carry ``origin`` gets it DERIVED at read time
# from where the loop's data actually lives (local first, then each origin
# mirror), defaulting to ``local`` when the loop isn't visible anywhere.
LOCAL_ORIGIN = origins.LOCAL_ORIGIN_ID


def loop_origin(loop: Optional[str], *, data_dir: Optional[str] = None) -> str:
    """The origin id a loop NAME lives on: ``local`` when this box has its
    ``config.json``, else the first origin mirror (``_loops_<host>``) that does,
    else ``local`` (the honest default — never raises)."""
    if not isinstance(loop, str) or not loop or "/" in loop or loop.startswith((".", "_")):
        return LOCAL_ORIGIN
    try:
        local = data_dir or os.path.dirname(report.status_dir("_"))
        for oid, _kind, path in origins._mirror_dirs(local):
            if os.path.isfile(os.path.join(path, loop, "config.json")):
                return oid
    except Exception:  # noqa: BLE001 — origin derivation must never break a read
        pass
    return LOCAL_ORIGIN


def with_origin(row: dict, resolve: Callable[[str], str] = loop_origin) -> dict:
    """``row`` plus a read-time ``origin`` for its ``loop`` when the stored
    record doesn't carry one. A row with no loop is returned untouched; an
    explicit stored origin always wins. Pure: returns a new dict."""
    if not isinstance(row, dict) or not row.get("loop") or row.get("origin"):
        return row
    return {**row, "origin": resolve(row["loop"])}


def _cached(resolve: Callable[[str], str]) -> Callable[[str], str]:
    memo: dict = {}

    def get(loop: str) -> str:
        if loop not in memo:
            memo[loop] = resolve(loop)
        return memo[loop]
    return get


def build_inbox_issue(project: str, title: str, *, body: str = "",
                      kind: str = "bug", source: str = "you",
                      severity: str = "normal", loop: Optional[str] = None,
                      origin: Optional[str] = None,
                      now: Optional[float] = None) -> dict:
    """The pure payload for a user- or loop-FILED issue (the reactive-stream
    inversion §3.2: filing is a normal, healthy act, not only a crash). Validates
    the enums fail-soft — an unknown ``kind``/``severity`` snaps to a safe default
    rather than raising, so a loop's one-line ``file_issue`` gesture can never fail
    on a typo. An empty ``project`` is stored as ``None`` — homeless but honest
    (the UI labels it "Unattributed"), never dumped into a real bucket."""
    now = time.time() if now is None else now
    k = kind if kind in KINDS else "bug"
    sev = severity if severity in SEVERITIES else "normal"
    proj = project.strip() if isinstance(project, str) and project.strip() else None
    return {
        "project": proj,
        "title": (title or "").strip() or "(untitled issue)",
        "body": body or "",
        "kind": k,
        "severity": sev,
        "source": normalize_source(source, loop=loop),
        "loop": loop,
        "origin": (origin.strip() if isinstance(origin, str) and origin.strip() and loop
                   else None),
        "ts": now,
    }


def _sanitize(text: str) -> str:
    """Filename-safe segment (``[A-Za-z0-9._-]``); can never traverse a path."""
    out = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(text))
    return out.strip("._") or "issue"


# ── issue payload (pure — takes already-gathered pieces, does no I/O) ────────
def build_issue(loop: str, run, result, *, log_slice: str = "",
                project: Optional[str] = None,
                now: Optional[float] = None) -> dict:
    """Assemble the structured issue for a terminal-failed run from its result.

    Captures {loop, run, failing agent/phase/status, guardian attempts, transcript
    path, mcp_loops.log slice, timestamp}. The failing agent is read from the
    guardian's give-up event (Q1) when present, else the last un-recovered
    timeout/error turn. The transcript path is resolved from Q2's persisted files.
    """
    now = time.time() if now is None else now
    events = [e if isinstance(e, dict) else vars(e)
              for e in (getattr(result, "events", None) or [])]

    guardian_attempts = [
        {"ts": e.get("ts"), "agent": e.get("agent"),
         "status": e.get("status"), "note": e.get("note")}
        for e in events if e.get("phase") == "guardian"]

    failing = None
    for e in reversed(events):                       # prefer the guardian give-up target
        if e.get("phase") == "guardian" and e.get("status") == "give_up":
            failing = {"agent": e.get("agent"), "phase": "guardian", "status": "give_up"}
            break
    if failing is None:                              # else the last unrecovered fail turn
        for e in reversed(events):
            if e.get("phase") != "guardian" and e.get("status") in ("timeout", "error"):
                failing = {"agent": e.get("agent"), "phase": e.get("phase"),
                           "status": e.get("status")}
                break
    if failing is None:
        failing = {"agent": None, "phase": None, "status": getattr(result, "ended", None)}

    agent = failing.get("agent")
    transcript = report.latest_transcript(loop, agent) if agent else None
    # A crash is the ONE issue the engine files (§3.4): kind=crash, source=engine.
    # It carries a project (resolved by the caller from the loop's config, else
    # None — shown as "Unattributed") so it lands in the same project-scoped stream as
    # loop-/user-filed issues — the forensic detail (below) rides along.
    return {
        "project": (project.strip() if isinstance(project, str) and project.strip()
                    else None),
        "kind": "crash",
        "source": "engine",
        "severity": "high",
        "title": f"{loop} crashed ({getattr(result, 'ended', '?')})",
        "loop": loop,
        "run": run,
        "ended": getattr(result, "ended", None),
        "failing_agent": failing.get("agent"),
        "failing_phase": failing.get("phase"),
        "failing_status": failing.get("status"),
        "guardian_attempts": guardian_attempts,
        "transcript_path": transcript,
        "log_slice": log_slice,
        "error": getattr(result, "error", None),
        "ts": now,
    }


def read_log_slice(loop: str, *, path: Optional[str] = None,
                   tail_bytes: int = 262144, max_lines: int = 200) -> str:
    """The *relevant* tail of the engine log for one loop: read only the last
    ``tail_bytes`` (the log is multi-MB), keep the lines mentioning the loop, cap
    to ``max_lines``. Fail-soft — a missing/unreadable log yields ``""``."""
    path = path or report.engine_log_path()
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            data = fh.read().decode("utf-8", "replace")
    except (FileNotFoundError, OSError):
        return ""
    lines = [ln for ln in data.splitlines() if loop in ln]
    if not lines:                                    # fall back to the raw tail
        lines = data.splitlines()
    return "\n".join(lines[-max_lines:])


# ── sink adapter (the seam a future GhIssueSink drops into) ──────────────────
class IssueSink(Protocol):
    def file_issue(self, issue: dict) -> Optional[dict]: ...
    def list_issues(self) -> list: ...
    def get_issue(self, issue_id: str) -> Optional[dict]: ...


class NullIssueSink:
    """Default no-op sink (pure-logic tests / unguarded use write nothing)."""

    def file_issue(self, issue: dict) -> Optional[dict]:
        return None

    def set_status(self, issue_id: str, status: str, *, note: str = "",
                   actor: str = "you") -> Optional[dict]:
        return None

    def list_issues(self) -> list:
        return []

    def get_issue(self, issue_id: str) -> Optional[dict]:
        return None


class LocalIssueSink:
    """Local-first sink: ``<base>/<id>.json`` (one file per issue, full context)
    plus an append-only ``<base>/index.jsonl`` the dashboard lists from.

    Fail-soft: a failed write is logged and swallowed so issue-filing can NEVER
    crash run teardown. Serialized so concurrent terminal failures don't interleave
    an index line or collide on an id."""

    def __init__(self, base_dir: Optional[str] = None,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda m: None):
        self.base = base_dir or report.issues_dir()
        self.clock = clock
        self.log = log
        self._lock = threading.Lock()
        # name → origin resolver for rows that don't store one (swappable in tests)
        self.resolve_origin: Callable[[str], str] = loop_origin

    def _new_id(self, issue: dict) -> str:
        stamp = int(issue.get("ts") or self.clock())
        # crash issues key off loop+failing_agent (historical); a filed inbox
        # issue keys off project+kind so its id reads honestly too.
        lead = issue.get("loop") or issue.get("project") or "issue"
        tag = issue.get("failing_agent") or issue.get("kind") or "loop"
        base = _sanitize(f"{lead}-{tag}-{stamp}")
        cand, n = base, 1
        while os.path.exists(os.path.join(self.base, cand + ".json")):
            n += 1
            cand = f"{base}-{n}"
        return cand

    def file_issue(self, issue: dict) -> Optional[dict]:
        try:
            with self._lock:
                os.makedirs(self.base, exist_ok=True)
                iid = self._new_id(issue)
                created = issue.get("ts") or self.clock()
                record = {"id": iid, "status": "open", "created": created, **issue}
                path = os.path.join(self.base, iid + ".json")
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(record, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, path)                # atomic: a reader never sees a partial file
                # Index carries the reactive-stream fields the stream cards read
                # (project/kind/source/severity/title) PLUS the crash forensics
                # keys (loop/run/ended/failing_*), so one index row serves both a
                # filed issue and a crash without a second store.
                idx = {"id": iid, "ts": created, "status": "open",
                       "project": issue.get("project"),
                       "kind": issue.get("kind", "crash"),
                       "source": issue.get("source"),
                       "severity": issue.get("severity"),
                       "title": issue.get("title"),
                       "loop": issue.get("loop"), "origin": issue.get("origin"),
                       "run": issue.get("run"),
                       "ended": issue.get("ended"),
                       "failing_agent": issue.get("failing_agent"),
                       "failing_phase": issue.get("failing_phase"),
                       "failing_status": issue.get("failing_status"),
                       "transcript_path": issue.get("transcript_path"),
                       "path": path}
                with open(os.path.join(self.base, "index.jsonl"), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(idx, ensure_ascii=False) + "\n")
                return record
        except Exception as e:  # noqa: BLE001 — issue-filing must never crash teardown
            self.log(f"[issues] file_issue failed: {type(e).__name__}: {e}")
            return None

    def set_status(self, issue_id: str, status: str, *, note: str = "",
                   actor: str = "you") -> Optional[dict]:
        """Triage: move ONE issue along the lifecycle (§3.3), enforcing the
        :data:`STATUS_TRANSITIONS` state machine so a card can never jump to an
        illegal state or fake a resolution. Rewrites the per-issue file's status
        atomically, appends a timeline entry to its ``triage`` list, and appends a
        status-update row to ``index.jsonl`` (append-only durable; :meth:`list_issues`
        folds to the latest). Returns the updated record, or ``None`` on an unknown
        issue / illegal transition (fail-soft, never raises into a caller)."""
        try:
            with self._lock:
                if status not in STATUSES:
                    self.log(f"[issues] set_status: unknown status {status!r}")
                    return None
                rec = self.get_issue(issue_id)
                if rec is None:
                    return None
                cur = rec.get("status", "open")
                if status != cur and status not in STATUS_TRANSITIONS.get(cur, set()):
                    self.log(f"[issues] set_status: illegal {cur!r}→{status!r}")
                    return None
                now = self.clock()
                rec["status"] = status
                rec.setdefault("triage", []).append(
                    {"ts": now, "from": cur, "to": status, "actor": actor, "note": note})
                path = os.path.join(self.base, _sanitize(rec["id"]) + ".json")
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
                with open(os.path.join(self.base, "index.jsonl"), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(
                        {"id": rec["id"], "ts": now, "status": status,
                         "_update": True}, ensure_ascii=False) + "\n")
                return rec
        except Exception as e:  # noqa: BLE001 — triage must never crash a request thread
            self.log(f"[issues] set_status failed: {type(e).__name__}: {e}")
            return None

    # -- read side (the dashboard reads through these; unit-tested here) --
    def list_issues(self) -> list:
        """The CURRENT view of every issue: index rows folded by id so a later
        status-update row (from :meth:`set_status`) supersedes the ``open`` row it
        was filed with, while first-seen order (newest last, append order) is
        preserved. Malformed lines are skipped; a missing index → ``[]`` (never a
        crash). Crash-only data (no updates) folds to itself, so legacy callers see
        exactly what they did before."""
        rows: dict = {}
        order: list = []
        try:
            with open(os.path.join(self.base, "index.jsonl"), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    iid = row.get("id")
                    if iid is None:
                        continue
                    if iid not in rows:
                        rows[iid] = row
                        order.append(iid)
                    else:
                        # a status-update row carries only {id, ts, status}: merge
                        # the mutable field onto the original row, keep its details.
                        rows[iid] = {**rows[iid], **{k: v for k, v in row.items()
                                                     if v is not None or k not in rows[iid]}}
        except OSError:            # missing index / unreadable base → empty, never a crash
            return []
        # Repair legacy crash rows to honest title/source/kind on the way out, so the
        # stream never renders a null-sourced ``(untitled)`` card (§ Issues data-
        # correctness). Derives only provable crash facts; well-formed rows untouched.
        # Carry the loop's ORIGIN (derived at read time when the row lacks one) so
        # the UI links /loops/<origin>/<name>, not always /loops/local/<name>.
        resolve = _cached(self.resolve_origin)
        return [with_origin(normalize_read(rows[i]), resolve) for i in order]

    def get_issue(self, issue_id: str) -> Optional[dict]:
        try:
            with open(os.path.join(self.base, _sanitize(issue_id) + ".json"),
                      encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            return None
        return with_origin(normalize_read(rec), self.resolve_origin)


# ── GitHub sink (OPT-IN: LOOPS_ISSUE_SINK=gh + LOOPS_GH_ISSUE_REPO=owner/repo) ─
GH_BODY_CAP = 60000          # GitHub rejects bodies > 65536 chars; leave headroom
GH_LOG_CAP = 8000            # the log slice is the biggest field — cap it first
GH_TIMEOUT = 30
_GH_URL_RE = re.compile(r"https://\S+/issues/(\d+)")


def gh_issue_body(issue: dict) -> str:
    """Markdown body for a GitHub issue — the same forensic fields the local file
    carries, rendered for a human, hard-capped under GitHub's body limit. Pure."""
    rows = [(k, issue.get(k)) for k in (
        "project", "kind", "source", "severity", "loop", "run", "ended",
        "failing_agent", "failing_phase", "failing_status", "transcript_path",
        "error") if issue.get(k) not in (None, "")]
    parts = ["| field | value |", "|---|---|"]
    parts += [f"| {k} | `{str(v).replace('|', '/')}` |" for k, v in rows]
    body = issue.get("body")
    if body:
        parts += ["", str(body)]
    attempts = issue.get("guardian_attempts") or []
    if attempts:
        parts += ["", "**Guardian attempts**", ""]
        parts += [f"- {a.get('status')} · {a.get('agent')} · {a.get('note') or ''}"
                  for a in attempts[:20]]
    log = issue.get("log_slice") or ""
    if log:
        if len(log) > GH_LOG_CAP:
            log = "…" + log[-GH_LOG_CAP:]
        parts += ["", "<details><summary>mcp_loops.log slice</summary>", "",
                  "```", log.replace("```", "ˋˋˋ"), "```", "</details>"]
    parts += ["", "_Filed by loopyard (GhIssueSink)._"]
    out = "\n".join(parts)
    return out if len(out) <= GH_BODY_CAP else out[:GH_BODY_CAP - 1] + "…"


class GhIssueSink:
    """GitHub-backed sink via the ``gh`` CLI. OPT-IN only — never the default.

    * ``file_issue`` runs ``gh issue create --repo R --title T --body-file -``
      (body on stdin, so no argv-length or shell-quoting hazard; argv is a list,
      never a shell string), parses the issue URL/number from stdout, and ALSO
      mirrors the record into a :class:`LocalIssueSink` stamped with
      ``gh_url``/``gh_number`` — so the dashboard, triage and every local reader
      keep working unchanged. If ``gh`` fails, the issue still lands locally with
      ``gh_error`` set: a GitHub outage can never lose a failure record.
    * ``list_issues`` / ``get_issue`` read GitHub (``gh issue list/view --json``)
      and map rows to the local shape (``id = gh-<number>``); a gh failure falls
      back to the local mirror, never raises.
    * ``set_status`` delegates to the local mirror (triage stays local).

    ``runner`` is the subprocess seam — tests inject a stub so a real GitHub issue
    is NEVER created."""

    def __init__(self, repo: str, *, labels: tuple = (),
                 local: Optional["LocalIssueSink"] = None,
                 runner: Callable = subprocess.run, gh: str = "gh",
                 log: Callable[[str], None] = lambda m: None):
        if not repo or "/" not in repo:
            raise ValueError(f"GhIssueSink needs an explicit owner/repo, got {repo!r}")
        self.repo = repo
        self.labels = tuple(l for l in labels if l)
        self.local = local if local is not None else LocalIssueSink(log=log)
        self.runner = runner
        self.gh = gh
        self.log = log

    def _run(self, args: list, *, stdin: Optional[str] = None) -> Optional[str]:
        """Run ``gh <args>``; stdout on success, ``None`` on any failure (logged)."""
        try:
            cp = self.runner([self.gh, *args], input=stdin, capture_output=True,
                             text=True, timeout=GH_TIMEOUT)
        except Exception as e:  # noqa: BLE001 — missing gh / timeout → fail-soft
            self.log(f"[issues] gh {args[:2]} failed: {type(e).__name__}: {e}")
            return None
        if getattr(cp, "returncode", 1) != 0:
            self.log(f"[issues] gh {args[:2]} rc={cp.returncode}: "
                     f"{(cp.stderr or '').strip()[:300]}")
            return None
        return cp.stdout or ""

    def file_issue(self, issue: dict) -> Optional[dict]:
        title = (issue.get("title") or f"{issue.get('loop') or 'loop'} issue")[:250]
        args = ["issue", "create", "--repo", self.repo, "--title", title,
                "--body-file", "-"]
        for lab in self.labels:
            args += ["--label", lab]
        out = self._run(args, stdin=gh_issue_body(issue))
        m = _GH_URL_RE.search(out or "")
        extra = ({"gh_url": m.group(0), "gh_number": int(m.group(1)),
                  "gh_repo": self.repo} if m else
                 {"gh_error": "gh issue create failed" if out is None
                  else f"no issue url in gh output: {out.strip()[:200]}",
                  "gh_repo": self.repo})
        return self.local.file_issue({**issue, **extra})

    def set_status(self, issue_id: str, status: str, *, note: str = "",
                   actor: str = "you") -> Optional[dict]:
        return self.local.set_status(issue_id, status, note=note, actor=actor)

    @staticmethod
    def _from_gh(row: dict) -> dict:
        state = str(row.get("state") or "").lower()
        return {"id": f"gh-{row.get('number')}", "gh_number": row.get("number"),
                "gh_url": row.get("url"), "title": row.get("title"),
                "status": "resolved" if state == "closed" else "open",
                "ts": row.get("createdAt"), "body": row.get("body"),
                "kind": "bug", "source": "engine", "project": None,
                "labels": [l.get("name") for l in (row.get("labels") or [])
                           if isinstance(l, dict)]}

    def list_issues(self) -> list:
        args = ["issue", "list", "--repo", self.repo, "--state", "all",
                "--limit", "100", "--json", "number,title,state,url,createdAt,labels"]
        for lab in self.labels:
            args += ["--label", lab]
        out = self._run(args)
        if out is None:
            return self.local.list_issues()
        try:
            return [self._from_gh(r) for r in json.loads(out or "[]")
                    if isinstance(r, dict)]
        except ValueError:
            return self.local.list_issues()

    def get_issue(self, issue_id: str) -> Optional[dict]:
        sid = str(issue_id)
        num = sid[3:] if sid.startswith("gh-") else sid
        if not num.isdigit():
            return self.local.get_issue(sid)            # a local-mirror id
        out = self._run(["issue", "view", num, "--repo", self.repo, "--json",
                         "number,title,state,url,createdAt,labels,body"])
        if out is None:
            return None
        try:
            row = json.loads(out)
        except ValueError:
            return None
        return self._from_gh(row) if isinstance(row, dict) else None


def select_issue_sink(env: Optional[dict] = None, *,
                      log: Callable[[str], None] = lambda m: None,
                      runner: Callable = subprocess.run) -> "IssueSink":
    """The active sink. DEFAULT is :class:`LocalIssueSink`. GitHub is OPT-IN only:
    ``LOOPS_ISSUE_SINK=gh`` AND an explicit ``LOOPS_GH_ISSUE_REPO=owner/repo``
    (optional ``LOOPS_GH_ISSUE_LABELS=a,b``). ``gh`` selected without a valid repo
    → logged, falls back to local (never guesses a repo)."""
    env = os.environ if env is None else env
    choice = (env.get("LOOPS_ISSUE_SINK") or "local").strip().lower()
    if choice == "gh":
        repo = (env.get("LOOPS_GH_ISSUE_REPO") or "").strip()
        labels = tuple(x.strip() for x in (env.get("LOOPS_GH_ISSUE_LABELS") or "").split(","))
        try:
            return GhIssueSink(repo, labels=labels, runner=runner, log=log,
                               local=LocalIssueSink(log=log))
        except ValueError as e:
            log(f"[issues] LOOPS_ISSUE_SINK=gh ignored: {e}; using local sink")
    return LocalIssueSink(log=log)
