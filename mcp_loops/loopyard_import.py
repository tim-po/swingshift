"""Markdown → structured items importer for the first-party hub capabilities
(Round B2).

The dogfood ``loopyard/`` folder ships human-authored markdown — ``objectives.md``
and ``known-issues.md`` — at the loopyard top level (siblings of a capability's
``loopyard/<dataDir>/`` data dir). A capability that stores structured JSON must
still SHOW that legacy content without anyone re-authoring it by hand, so this
module parses the shared ``## section`` + ``- **title** — detail (`status`)``
convention both files follow into the same normalized item shape the capability
pages render.

Pure + deterministic: text in, list of item dicts out. No filesystem, no server.
"""

from __future__ import annotations

import re
from typing import Any

# One bullet: "- **Title** — detail…" (em/en dash or hyphen after the bold title,
# all optional). The bold title may itself contain `code` backticks.
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
_BOLD_RE = re.compile(r"^\*\*(.+?)\*\*\s*(?:[—–-]\s*)?(.*)$")
_SECTION_RE = re.compile(r"^\s*#{1,6}\s+(.*\S)\s*$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")

# Status vocabularies, per the two dogfood files' legends.
_OBJECTIVE_STATUSES = {"shipped", "active", "next", "idea", "deferred"}
_ISSUE_STATUSES = {"open", "fixed", "wontfix"}

# Section-name keyword → objective status, used only when a bullet omits an
# explicit (`status`) token (most carry one; this is the graceful fallback).
_SECTION_STATUS = [
    ("shipped", "shipped"),
    ("active", "active"),
    ("next", "next"),
    ("deferred", "deferred"),
    ("big idea", "idea"),
    ("idea", "idea"),
]


def _slug(text: str) -> str:
    """A stable, path-/id-safe slug from a title (backticks + punctuation out)."""
    s = re.sub(r"`", "", text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "item"


def _statuses_in(text: str, vocab: set[str]) -> list[str]:
    """The backticked tokens in ``text`` that are known status words (in order)."""
    return [t.strip().lower() for t in _BACKTICK_RE.findall(text)
            if t.strip().lower() in vocab]


def _clean_detail(rest: str) -> str:
    """Drop the trailing status marker(s) so the detail reads cleanly:
    ``**`fixed`**`` and ``(`open`, S)`` / ``(`shipped`)`` are removed; other
    backticked spans (URLs, code) are kept."""
    d = re.sub(r"\s*\*\*`[^`]+`\*\*", "", rest)          # bold-wrapped status
    d = re.sub(r"\s*\(`[^`]+`(?:,[^)]*)?\)", "", d)      # (`status`) or (`status`, SEV)
    return d.strip().strip(".").strip()


def _severity(content: str) -> str:
    """Best-effort severity for an issue bullet: the ``, SEV`` tail after a
    status token (``(`open`, S)`` → ``S``), or a ``(P0)``-style marker anywhere."""
    m = re.search(r"`(?:open|fixed|wontfix)`(?:,\s*([^)]+))?\)", content)
    if m and m.group(1):
        return m.group(1).strip()
    m = re.search(r"\((P\d)\)", content)
    return m.group(1) if m else ""


def _parse(text: str, *, kind: str) -> list[dict[str, Any]]:
    vocab = _ISSUE_STATUSES if kind == "issue" else _OBJECTIVE_STATUSES
    section = ""
    section_status = ""
    seen: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        sec = _SECTION_RE.match(raw)
        if sec:
            section = sec.group(1).strip()
            low = section.lower()
            section_status = ""
            if kind != "issue":
                for key, st in _SECTION_STATUS:
                    if key in low:
                        section_status = st
                        break
            continue
        b = _BULLET_RE.match(raw)
        if not b:
            continue
        content = b.group(1)
        bold = _BOLD_RE.match(content)
        if bold:
            title, rest = bold.group(1).strip(), bold.group(2).strip()
        else:
            title, rest = content.strip(), ""

        found = _statuses_in(content, vocab)
        status = found[-1] if found else (section_status if kind != "issue" else "open")

        # unique id within this parse
        base = _slug(title)
        n = seen.get(base, 0) + 1
        seen[base] = n
        iid = base if n == 1 else f"{base}-{n}"

        item: dict[str, Any] = {
            "id": iid,
            "source": "imported",
            "title": title,
            "detail": _clean_detail(rest),
            "status": status,
            "section": section,
        }
        if kind == "issue":
            item["severity"] = _severity(content)
        else:
            item["priority"] = ""
        out.append(item)
    return out


def parse_objectives_md(text: str) -> list[dict[str, Any]]:
    """Parse ``objectives.md`` → objective items ``{id, source, title, detail,
    status, priority, section}``."""
    return _parse(text, kind="objective")


def parse_issues_md(text: str) -> list[dict[str, Any]]:
    """Parse ``known-issues.md`` → issue items ``{id, source, title, detail,
    status, severity, section}``."""
    return _parse(text, kind="issue")


def parse(text: str, *, kind: str) -> list[dict[str, Any]]:
    """Dispatch on capability ``importKind`` (``objective`` | ``issue``)."""
    return parse_issues_md(text) if kind == "issue" else parse_objectives_md(text)
