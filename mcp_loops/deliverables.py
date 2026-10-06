"""The loop's DELIVERABLES FOLDER (Better-UX #3) + the automatic negative
status that replaces the old ship/keep clicks (Better-UX #5).

A loop's declared deliverables are no longer ONE picked path — they are a
FOLDER inside the loop's output folder::

    <output_dir>/deliverables/

Every file an agent writes into that folder is a result, and agents can mark
MULTIPLE files as results with repeated ``artifact=<path>`` markers in their
end-of-turn notes (a marked path may live anywhere; relative paths resolve
against the output folder, the deliverables folder, the loops data root, the
install root and the loop's build workspace — see ``search_roots``). The folder
view is the union of both, one entry per file, with who marked it.

ship/keep are gone as clicks. What's left is an AUTOMATIC negative status the
engine derives, never a user button:

* ``removed`` — the owner archived the loop (it's out of their way);
* ``deleted`` — every file the loop marked as a result is gone from disk.

Pure reads of the filesystem; no writes; never raises.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

FOLDER = "deliverables"
MAX_ITEMS = 200                 # a runaway folder never floods the payload

AUTO_REMOVED = "removed"
AUTO_DELETED = "deleted"
AUTO_STATUSES = (AUTO_REMOVED, AUTO_DELETED)

_ARTIFACT_RE = re.compile(r"\bartifact=(\S+)", re.IGNORECASE)


def folder_path(output_dir: Optional[str]) -> Optional[str]:
    """``<output_dir>/deliverables`` (or ``None`` without an output folder)."""
    return os.path.join(output_dir, FOLDER) if output_dir else None


# ``artifact=none`` / ``artifact=n/a`` … mean "no artifact", never a phantom file
_NO_ARTIFACT = frozenset({"none", "n/a", "na", "null", "nil", "-", "--", "tbd",
                          "pending", "<path>", "path"})
_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")    # mcp_loops/x.py:45 -> the file


def _clean(raw: str) -> Optional[str]:
    """Strip prose punctuation/quotes an agent glued onto the marker; ``None``
    for a no-artifact sentinel."""
    raw = raw.strip().strip("`'\"").rstrip(".,;:)]`'\"").lstrip("([")
    # a brace is prose only when unbalanced (``(see {x.md})``); a balanced
    # ``a/{x,y}.md`` is a shell brace list — keep it for marker_paths()
    while raw.endswith("}") and raw.count("}") > raw.count("{"):
        raw = raw[:-1].rstrip(".,;:)]`'\"")
    while raw.startswith("{") and raw.count("{") > raw.count("}"):
        raw = raw[1:].lstrip("([")
    if raw.startswith("{") and raw.endswith("}") and "," not in raw:
        raw = raw[1:-1]                               # ``{x.md}`` wraps one path
    if not raw or raw.lower() in _NO_ARTIFACT:
        return None
    return raw


_BRACE_RE = re.compile(r"\{([^{}]*,[^{}]*)\}")
MAX_EXPAND = 64


def _expand_braces(raw: str) -> list[str]:
    """Shell brace lists an agent used to mark several files in one marker
    (``shots/{1-offer,2-after}.png``) -> one path per file, left to right.
    Nested / multiple groups expand too; capped at ``MAX_EXPAND``."""
    out, todo = [], [raw]
    while todo and len(out) < MAX_EXPAND:
        cur = todo.pop(0)
        m = _BRACE_RE.search(cur)
        if not m:
            out.append(cur)
            continue
        todo[:0] = [cur[:m.start()] + alt + cur[m.end():]
                    for alt in m.group(1).split(",")]
    return out


def marker_paths(raw: str) -> list[str]:
    """The path(s) one ``artifact=`` marker names: cleaned of prose, brace
    lists expanded, empty for a no-artifact sentinel."""
    cleaned = _clean(raw)
    if cleaned is None:
        return []
    return [p for p in _expand_braces(cleaned) if p and p.lower() not in _NO_ARTIFACT]


def search_roots(output_dir: Optional[str]) -> list[str]:
    """Where a RELATIVE ``artifact=`` path may be rooted, most specific first.

    Agents write markers relative to whatever they had in mind: the output
    folder (``summary.md``), the deliverables folder, the loops DATA root
    (``_output/<loop>/x.md``), the install root (``data/_loops/_output/…``) or
    the loop's build workspace (``mcp_loops/x.py``). The data root is derived
    from the output folder's own layout (``<data>/_output/<loop>``) — no config
    read, so an off-layout output folder just gets the first two roots."""
    if not output_dir:
        return []
    out = os.path.normpath(output_dir)
    roots = [out, os.path.join(out, FOLDER)]
    parent = os.path.dirname(out)
    if os.path.basename(parent) == "_output":
        data = os.path.dirname(parent)
        roots += [data,
                  os.path.join(data, "loop-workspaces", os.path.basename(out)),
                  os.path.dirname(os.path.dirname(data))]   # <install>/data/_loops
    return roots


def _resolve(raw: str, output_dir: Optional[str]) -> str:
    """The real path a marker points at: the first root it EXISTS under
    (also trying it without a trailing ``:<line>``); if it exists nowhere, the
    output-folder-relative path (so a truly gone file honestly reads deleted)."""
    if raw.startswith(("...", "…")) and output_dir:
        # an elided prefix (``.../<loop>/x.md``): drop it, and the loop's own
        # folder name if the tail starts with it
        tail = raw.lstrip(".…").lstrip("/")
        head, _, rest = tail.partition("/")
        raw = rest if rest and head == os.path.basename(os.path.normpath(output_dir)) \
            else tail
    variants = [raw]
    bare = _LINE_SUFFIX_RE.sub("", raw)
    if bare != raw:
        variants.append(bare)
    if os.path.isabs(raw) or not output_dir:
        for v in variants:
            if os.path.exists(v):
                return v
        return bare
    for root in search_roots(output_dir):
        for v in variants:
            cand = os.path.join(root, v)
            if os.path.exists(cand):
                return cand
    return os.path.join(output_dir, bare)


def _key(path: str) -> str:
    """De-dup key: the real on-disk path (symlinks / ``a/../b`` / the same file
    marked relative AND absolute collapse to one row)."""
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.normpath(path)


def marked_results(reports: list[dict], output_dir: Optional[str]) -> list[dict]:
    """Every ``artifact=`` a report marked, in first-marked order, de-duplicated
    by resolved path: ``[{path, markedBy:[agent], seq}]``. ``seq`` is the
    marking agent's 1-based report number (matches loop_turn_detail)."""
    out: dict[str, dict] = {}
    per_agent: dict[str, int] = {}
    for e in reports or []:
        if not isinstance(e, dict):
            continue
        agent = e.get("agent") if isinstance(e.get("agent"), str) else None
        if agent:
            per_agent[agent] = per_agent.get(agent, 0) + 1
        note = e.get("note")
        if not isinstance(note, str):
            continue
        raws = [r for m in _ARTIFACT_RE.finditer(note) for r in marker_paths(m.group(1))]
        for raw in raws:
            path = os.path.normpath(_resolve(raw, output_dir))
            item = out.setdefault(_key(path), {"path": path, "markedBy": [],
                                         "seq": per_agent.get(agent) if agent else None})
            if agent and agent not in item["markedBy"]:
                item["markedBy"].append(agent)
    return list(out.values())


def _entry(path: str, output_dir: Optional[str], folder: Optional[str]) -> dict:
    exists = os.path.exists(path)
    in_folder = bool(folder) and (path == folder or path.startswith(folder + os.sep))
    in_output = bool(output_dir) and path.startswith(output_dir.rstrip(os.sep) + os.sep)
    rel = (os.path.relpath(path, folder) if in_folder
           else os.path.relpath(path, output_dir) if in_output else path)
    size = None
    if exists and os.path.isfile(path):
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
    return {"name": os.path.basename(path) or path, "rel": rel, "path": path,
            "exists": exists, "isDir": exists and os.path.isdir(path),
            "size": size,
            "location": "folder" if in_folder else "output" if in_output else "external",
            "markedBy": [], "marked": False,
            # an automatic, per-file negative status — never a user click
            "status": None if exists else AUTO_DELETED}


def build_folder(output_dir: Optional[str], reports: Optional[list[dict]] = None
                 ) -> dict[str, Any]:
    """The deliverables folder view::

        {folder, exists, items:[{name, rel, path, exists, isDir, size,
                                 location: folder|output|external,
                                 marked, markedBy:[agent], seq, status}],
         results, missing, hidden}

    ``items`` = every file under ``<output>/deliverables/`` (sorted) + every
    ``artifact=``-marked path, merged by path. ``results`` counts the items that
    still exist; ``missing`` the marked ones that are gone (``status:"deleted"``).
    Never raises."""
    try:
        folder = folder_path(output_dir)
        items: dict[str, dict] = {}
        if folder and os.path.isdir(folder):
            for root, dirs, files in os.walk(folder):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for f in sorted(files):
                    if f.startswith("."):
                        continue
                    p = os.path.normpath(os.path.join(root, f))
                    items[_key(p)] = _entry(p, output_dir, folder)
        for m in marked_results(reports or [], output_dir):
            k = _key(m["path"])
            it = items.get(k) or _entry(m["path"], output_dir, folder)
            it["marked"] = True
            it["markedBy"] = m["markedBy"]
            it["seq"] = m["seq"]
            items[k] = it
        ordered = sorted(items.values(),
                         key=lambda i: (i["location"] != "folder", i["rel"]))
        hidden = max(0, len(ordered) - MAX_ITEMS)
        ordered = ordered[:MAX_ITEMS]
        return {"folder": folder,
                "exists": bool(folder) and os.path.isdir(folder),
                "items": ordered,
                "results": sum(1 for i in ordered if i["exists"]),
                "missing": sum(1 for i in ordered if not i["exists"]),
                "hidden": hidden}
    except Exception:  # noqa: BLE001 — a view must never break the card
        return {"folder": folder_path(output_dir), "exists": False, "items": [],
                "results": 0, "missing": 0, "hidden": 0}


def auto_status(*, archived: bool, folder: Optional[dict]) -> Optional[dict]:
    """The AUTOMATIC negative status (replaces the ship/keep clicks)::

        {value: removed|deleted, reason}  |  None

    ``removed`` when the owner archived the loop; ``deleted`` when the loop
    marked results and every one of them is gone. ``None`` otherwise — the
    absence of a negative status is NOT a positive rating (good/ok/bad is)."""
    if archived:
        return {"value": AUTO_REMOVED, "reason": "you archived this loop"}
    items = (folder or {}).get("items") or []
    marked = [i for i in items if i.get("marked") or i.get("location") == "folder"]
    if marked and not any(i.get("exists") for i in marked):
        return {"value": AUTO_DELETED,
                "reason": "every result this loop produced has been deleted"}
    return None
