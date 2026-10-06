#!/usr/bin/env python3
"""Static grep-clean for the Phase-B SHIPPED file set (PHASE-B-SPEC D11, §4 B3 step 7).

    grep_clean.py --allow scripts/grep-clean.allow --shipped shipped.lst \\
        --shipped-hits hits.shipped --unshipped-files hits.unshipped.files \\
        [--tracked tracked.lst] [--root DIR]

Inputs (produced by ``phase_b_acceptance.sh`` from the just-built tarball):
  shipped.lst           one repo-relative path per line: bundle source entries
                        (+ our ``loopyard.pth``) + the clone files that run
  hits.shipped          ``path:lineno:text`` grep hits over the shipped bytes
  hits.unshipped.files  every OTHER tracked file with a hit (one path per line)

``scripts/grep-clean.allow`` rows — ``<kind> <pattern> # <one-line reason>``:
  exclude <glob>             unshipped files only. FAILS if the glob matches a
                             shipped path, or matches no tracked file (stale).
  text <path>:<line-regex>   one TEXT hit in a shipped file. FAILS if <path> is
                             not shipped, if it matches no hit (stale), or if it
                             matches a line carrying ``=``/``default=``/
                             ``os.environ`` outside a comment or docstring.

Every shipped hit must be covered by a ``text`` row and every unshipped hit
file by an ``exclude`` glob. Exit 0 = clean, 1 = violations, 2 = usage/input.
Pure stdlib; runs under the bundle's own ``runtime/bin/python3``.
"""
from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

FUNCTIONAL_RE = re.compile(r"=|os\.environ")


@dataclass
class Row:
    lineno: int
    kind: str
    pattern: str
    reason: str
    regex: re.Pattern | None = None
    path: str = ""
    used: int = 0
    matched: list = field(default_factory=list)


def _expand_braces(glob: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", glob)
    if not m:
        return [glob]
    out = []
    for alt in m.group(1).split(","):
        out += _expand_braces(glob[:m.start()] + alt + glob[m.end():])
    return out


def glob_to_regex(glob: str) -> re.Pattern:
    """``**/`` = zero or more dirs, ``**`` = anything, ``*``/``?`` stay in one
    path component, ``{a,b}`` alternation."""
    parts = []
    for g in _expand_braces(glob):
        i, rx = 0, ""
        while i < len(g):
            if g.startswith("**/", i):
                rx += "(?:.*/)?"
                i += 3
            elif g.startswith("**", i):
                rx += ".*"
                i += 2
            elif g[i] == "*":
                rx += "[^/]*"
                i += 1
            elif g[i] == "?":
                rx += "[^/]"
                i += 1
            else:
                rx += re.escape(g[i])
                i += 1
        parts.append(rx)
    return re.compile(r"\A(?:" + "|".join(parts) + r")\Z")


def parse_allow(text: str) -> tuple[list[Row], list[str]]:
    rows, errs = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, rest = line.partition(" ")
        body, sep, reason = rest.rpartition(" # ")
        if not sep or not reason.strip() or not body.strip():
            errs.append(f"allow:{n}: row needs '<kind> <pattern> # <reason>': {raw!r}")
            continue
        row = Row(n, kind, body.strip(), reason.strip())
        if kind == "exclude":
            row.regex = glob_to_regex(row.pattern)
        elif kind == "text":
            path, colon, rx = row.pattern.partition(":")
            if not colon or not rx:
                errs.append(f"allow:{n}: text row needs '<path>:<line-regex>'")
                continue
            try:
                row.path, row.regex = path, re.compile(rx)
            except re.error as e:
                errs.append(f"allow:{n}: bad regex {rx!r}: {e}")
                continue
        else:
            errs.append(f"allow:{n}: unknown kind {kind!r} (exclude|text)")
            continue
        rows.append(row)
    return rows, errs


def parse_hits(text: str) -> list[tuple[str, int, str]]:
    hits = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = re.match(r"^(.*?):(\d+):(.*)$", raw)
        if not m:
            raise ValueError(f"bad hit line (want path:lineno:text): {raw!r}")
        path = m.group(1)
        if path.startswith("./"):
            path = path[2:]
        hits.append((path, int(m.group(2)), m.group(3)))
    return hits


def _py_text_lines(src: str) -> tuple[set[int], dict[int, int]]:
    """(lines inside a bare string statement — docstrings, …;
        {line: column where a ``#`` comment starts})."""
    doc, comments = set(), {}
    try:
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None),
                                                         ast.Constant) \
                    and isinstance(node.value.value, str):
                doc.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0]] = tok.start[1]
    except (SyntaxError, tokenize.TokenError, ValueError):
        pass
    return doc, comments


def is_functional_line(path: str, lineno: int, text: str, source: str | None) -> bool:
    """True when a hit line carries ``=``/``default=``/``os.environ`` OUTSIDE a
    comment or docstring — such a line must be fixed, never allowlisted."""
    if path.endswith((".md", ".txt", ".html", ".css")):
        return False
    if path.endswith(".py") and source is not None:
        doc, comments = _py_text_lines(source)
        if lineno in doc:
            return False
        code = text[:comments[lineno]] if lineno in comments else text
        return bool(FUNCTIONAL_RE.search(code))
    stripped = text.lstrip()
    if stripped.startswith("#"):
        return False
    code = text.split(" #", 1)[0]
    return bool(FUNCTIONAL_RE.search(code))


def check(rows: list[Row], shipped: list[str], shipped_hits: list[tuple[str, int, str]],
          unshipped_files: list[str], tracked: list[str] | None,
          read_source=lambda p: None) -> list[str]:
    errs = []
    shipped_set = set(shipped)
    excludes = [r for r in rows if r.kind == "exclude"]
    texts = [r for r in rows if r.kind == "text"]

    for r in excludes:
        bad = sorted(p for p in shipped if r.regex.match(p))
        if bad:
            errs.append(f"allow:{r.lineno}: exclude {r.pattern!r} matches SHIPPED "
                        f"path(s) {bad[:5]} — an exclude can never hide shipped code")
        if tracked is not None and not any(r.regex.match(p) for p in tracked):
            errs.append(f"allow:{r.lineno}: stale exclude {r.pattern!r} matches no "
                        f"tracked file")
    for f in unshipped_files:
        hit = [r for r in excludes if r.regex.match(f)]
        if not hit:
            errs.append(f"unshipped hit file {f} is not covered by any exclude row")
        for r in hit:
            r.used += 1

    for r in texts:
        if r.path not in shipped_set:
            errs.append(f"allow:{r.lineno}: text row path {r.path!r} is not in the "
                        f"shipped set (text rows are for shipped files only)")
    for path, lineno, text in shipped_hits:
        cover = [r for r in texts if r.path == path and r.regex.search(text)]
        if not cover:
            errs.append(f"shipped hit NOT allowlisted: {path}:{lineno}: {text.strip()}")
            continue
        for r in cover:
            r.used += 1
            if is_functional_line(path, lineno, text, read_source(path)):
                errs.append(f"allow:{r.lineno}: text row matches a FUNCTIONAL line "
                            f"{path}:{lineno}: {text.strip()} — fix it, never allowlist")
    for r in texts:
        if r.path in shipped_set and not r.used:
            errs.append(f"allow:{r.lineno}: stale text row {r.pattern!r} matches no "
                        f"shipped hit")
    return errs


def _lines(p: str | None) -> list[str]:
    if not p:
        return []
    return [x.strip() for x in Path(p).read_text().splitlines() if x.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="grep_clean.py", description=__doc__.split("\n")[0])
    ap.add_argument("--allow", required=True)
    ap.add_argument("--shipped", required=True)
    ap.add_argument("--shipped-hits", required=True)
    ap.add_argument("--unshipped-files", required=True)
    ap.add_argument("--tracked", help="tracked-file list (default: `git ls-files` in --root)")
    ap.add_argument("--root", default=".", help="clone root (sources for the docstring check)")
    ap.add_argument("--unpacked", help="unpacked bundle root: shipped sources are read here first")
    a = ap.parse_args(argv)
    try:
        rows, errs = parse_allow(Path(a.allow).read_text())
        shipped = _lines(a.shipped)
        hits = parse_hits(Path(a.shipped_hits).read_text())
        unshipped = _lines(a.unshipped_files)
        if a.tracked:
            tracked = _lines(a.tracked)
        else:
            tracked = subprocess.run(["git", "-C", a.root, "ls-files"], check=True,
                                     capture_output=True, text=True).stdout.split("\n")
            tracked = [t for t in tracked if t]
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        print(f"grep_clean: bad input: {e}", file=sys.stderr)
        return 2

    def read_source(rel):
        for base in (a.unpacked, a.root):
            if base and (Path(base) / rel).is_file():
                try:
                    return (Path(base) / rel).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return None
        return None

    errs += check(rows, shipped, hits, unshipped, tracked, read_source)
    n_text = sum(1 for r in rows if r.kind == "text")
    n_excl = sum(1 for r in rows if r.kind == "exclude")
    print(f"grep_clean: shipped={len(shipped)} shipped_hits={len(hits)} "
          f"unshipped_hit_files={len(unshipped)} rows(text={n_text}, exclude={n_excl})")
    if errs:
        for e in errs:
            print(f"FAIL {e}")
        print(f"grep_clean: {len(errs)} violation(s)")
        return 1
    print("grep_clean: CLEAN (0 functional, every hit allowlisted with a reason)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
