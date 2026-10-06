// First-run PREREQUISITE check for the desktop app — the in-UI replacement for
// "brew install tmux / install claude + log in / run install.sh" in a terminal.
//
// Pure face over injected seams (like originAgent.mjs): `exists(path)` and
// `run(cmd, args) -> Promise<{code, stdout, stderr}>` are supplied by main.js
// (prereqSeams.mjs), so every detection state and the EXACT command each fix
// runs are unit-tested with no processes.
//
// What is checked mirrors `yard start`'s own preflight (mcp_loops/yard.py):
// tmux + git on PATH, a claude CLI ($LOOPS_CLAUDE_BIN > PATH > ~/.local/bin),
// plus two things yard cannot see from inside: whether claude is LOGGED IN
// (`claude auth status --json`) and whether the yard bundle itself is present.
//
// Safety rule: the renderer only ever names a check id; the command a fix runs is
// chosen HERE from constants + resolved absolute paths, never from page input.
import { resolveYardCommand } from './originAgent.mjs';

export const CLAUDE_INSTALL_URL = 'https://claude.ai/install.sh';
export const HOMEBREW_URL = 'https://brew.sh';

/** Resolve an executable on a colon PATH (first hit), or null. */
export function whichIn(name, pathStr, exists) {
  if (!name) return null;
  if (name.includes('/')) return exists(name) ? name : null;
  for (const dir of String(pathStr || '').split(':').filter(Boolean)) {
    const p = `${dir.replace(/\/+$/, '')}/${name}`;
    if (exists(p)) return p;
  }
  return null;
}

/** The claude CLI yard would pick (D9: $LOOPS_CLAUDE_BIN > PATH > ~/.local/bin/claude). */
export function resolveClaude({ env = {}, home = '', exists = () => false } = {}) {
  if (env.LOOPS_CLAUDE_BIN && exists(env.LOOPS_CLAUDE_BIN)) return env.LOOPS_CLAUDE_BIN;
  const onPath = whichIn('claude', env.PATH, exists);
  if (onPath) return onPath;
  const local = home ? `${home.replace(/\/+$/, '')}/.local/bin/claude` : '';
  return local && exists(local) ? local : null;
}

/** Read `claude auth status --json` → {loggedIn, email?} or {loggedIn:null} when
 *  the answer is unreadable (old CLI, timeout) — never guessed as logged in.
 *  The real CLI (2.1.283) prints the JSON on stdout and exits 1 when signed out,
 *  so the exit code is ignored; the `--text` form ("Not logged in. …") is read
 *  too, for the fallback probe on a CLI that rejects `--json`. */
export function parseAuthStatus(res) {
  if (!res || !res.stdout) return { loggedIn: null };
  const out = String(res.stdout).trim();
  try {
    const j = JSON.parse(out);
    if (typeof j.loggedIn !== 'boolean') return { loggedIn: null };
    return { loggedIn: j.loggedIn, email: j.loggedIn && j.email ? String(j.email) : '' };
  } catch {
    if (/^not logged in\b/i.test(out)) return { loggedIn: false, email: '' };
    if (/^logged in\b/i.test(out)) return { loggedIn: true, email: '' };
    return { loggedIn: null };
  }
}

// The probes, in order: the documented JSON form, then the bare verb (JSON by
// default on current CLIs, text on some) for a CLI that does not know `--json`.
export const AUTH_STATUS_PROBES = Object.freeze([
  Object.freeze(['auth', 'status', '--json']),
  Object.freeze(['auth', 'status']),
]);

/** Ask `claude` whether it is signed in, falling back to the next probe while the
 *  answer is unreadable. Never rejects. */
export async function probeAuth(claude, run) {
  for (const args of AUTH_STATUS_PROBES) {
    let r;
    try { r = parseAuthStatus(await run(claude, [...args])); } catch { r = { loggedIn: null }; }
    if (r.loggedIn !== null) return r;
  }
  return { loggedIn: null };
}

// `claude auth login` with a pipe for stdin prints the sign-in URL and then
// "Paste code here if prompted >" (checked against the real CLI 2.1.283). The
// app shows a code field when it sees that prompt and a link for the URL.
export const CODE_PROMPT_RE = /paste code here/i;
const SIGNIN_HOSTS = /^https:\/\/(?:[a-z0-9-]+\.)*(?:claude\.com|claude\.ai|anthropic\.com)(?:[/?#]|$)/i;

/** The sign-in URL in a line of `claude auth login` output, or null. Only an
 *  https URL on an Anthropic/Claude host is ever returned (the app opens it). */
export function signinUrlIn(line) {
  const m = String(line || '').match(/https:\/\/\S+/);
  return m && SIGNIN_HOSTS.test(m[0]) ? m[0] : null;
}

/** A pasted sign-in code → the one line written to the CLI's stdin, or null.
 *  One line, printable, bounded — never a way to feed the CLI anything else. */
export function cleanCode(text) {
  const s = String(text == null ? '' : text).trim();
  if (!s || s.length > 512 || /[\x00-\x1f\x7f\s]/.test(s)) return null;
  return s;
}

/** Single-quote for /bin/sh (only ever applied to a validated https URL). */
function shq(s) {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

/** Only a plain https URL ending in /install.sh may feed the installer. */
export function safeInstallUrl(url) {
  const s = String(url || '').trim();
  if (!/^https:\/\/[A-Za-z0-9.-]+(?::\d+)?(\/[A-Za-z0-9._~%/-]*)?\/install\.sh$/.test(s)) return null;
  return s;
}

/** The fix for a check id, as {kind, label, cmd, args} for `run`, {kind:'open',
 *  url} for a browser link, or {kind:'info', text} when the app cannot do it.
 *  ctx: {platform, brew, claude, installUrl}. */
export function fixFor(id, ctx = {}) {
  const { platform = '', brew = null, claude = null, installUrl = null } = ctx;
  switch (id) {
    case 'tmux':
      if (brew) return { kind: 'run', label: 'Install tmux', cmd: brew, args: ['install', 'tmux'] };
      if (platform === 'darwin') return { kind: 'open', label: 'Get Homebrew first', url: HOMEBREW_URL };
      return { kind: 'info', text: 'Install tmux with your package manager (e.g. sudo apt install tmux).' };
    case 'git':
      if (platform === 'darwin') {
        return { kind: 'run', label: 'Install developer tools', cmd: '/usr/bin/xcode-select', args: ['--install'] };
      }
      if (brew) return { kind: 'run', label: 'Install git', cmd: brew, args: ['install', 'git'] };
      return { kind: 'info', text: 'Install git with your package manager.' };
    case 'claude':
      return {
        kind: 'run', label: 'Install Claude Code', cmd: '/bin/bash',
        args: ['-c', `curl -fsSL ${shq(CLAUDE_INSTALL_URL)} | bash`],
      };
    case 'claudeLogin':
      if (!claude) return { kind: 'info', text: 'Install Claude Code first.' };
      // interactive: stdin is a pipe so the "Paste code here" step can be
      // answered from the app's code field (prereqs send()).
      return { kind: 'run', label: 'Sign in to Claude', cmd: claude, args: ['auth', 'login'], interactive: true };
    case 'yard': {
      const url = safeInstallUrl(installUrl);
      if (url) return { kind: 'run', label: 'Install Loopyard', cmd: '/bin/sh', args: ['-c', `curl -fsSL ${shq(url)} | sh`] };
      return { kind: 'info', text: 'Paste the join link from your Hub below — it carries the Loopyard installer.' };
    }
    default:
      return null;
  }
}

/** Run every check. Returns {items, ready, blocking}: `ready` = nothing blocks
 *  Connect (login is advisory — the UI loads without it, loops need it). */
export async function checkPrereqs({ env = {}, home = '', platform = '', resourcesPath = '', exists = () => false, run, installUrl = null } = {}) {
  const PATH = env.PATH || '';
  const brew = whichIn('brew', PATH, exists);
  const tmux = whichIn('tmux', PATH, exists);
  const git = whichIn('git', PATH, exists);
  const claude = resolveClaude({ env, home, exists });
  const ctx = { platform, brew, claude, installUrl };
  const item = (id, label, ok, detail, blocking = true) => ({
    id, label, ok, blocking, detail, fix: ok ? null : fixFor(id, ctx),
  });

  let login = { loggedIn: null };
  if (claude && typeof run === 'function') {
    login = await probeAuth(claude, run);
  }

  const y = resolveYardCommand({ env, home, exists, resourcesPath });
  const yardPath = y.pre.length ? y.cmd : whichIn(y.cmd, PATH, exists);
  const yardOk = y.bundled || (!!yardPath && exists(yardPath));

  const items = [
    item('tmux', 'tmux', !!tmux, tmux ? tmux : 'not found — agents run in tmux'),
    item('git', 'git', !!git, git ? git : 'not found'),
    item('claude', 'Claude Code', !!claude, claude ? claude : 'not installed'),
    item('claudeLogin', 'Claude sign-in', login.loggedIn === true,
      login.loggedIn === true ? `signed in${login.email ? ` as ${login.email}` : ''}`
        : login.loggedIn === false ? 'not signed in' : claude ? 'could not read sign-in state' : 'needs Claude Code',
      false),
    item('yard', 'Loopyard engine', yardOk,
      y.bundled ? 'bundled with the app' : yardOk ? yardPath : 'not installed'),
  ];
  const blocking = items.filter((i) => !i.ok && i.blocking).map((i) => i.id);
  return { items, ready: blocking.length === 0, blocking };
}

/** The fix runner main.js exposes over IPC. `check()` re-runs checkPrereqs;
 *  `spawnLines(cmd, args, onLine, {interactive, onStdin}) -> Promise<code>` runs
 *  one fix streaming its output (an interactive fix gets a stdin pipe, handed
 *  back through onStdin(write)); `openUrl(url)` opens the browser. One fix per
 *  id at a time; every fix ends with a fresh check so the UI shows the real
 *  state, not a hopeful one. */
export function createPrereqController({ check, spawnLines, openUrl = () => {}, onProgress = () => {} } = {}) {
  const busy = new Set();
  const stdin = new Map(); // id -> write(line) of a running interactive fix
  const links = new Map(); // id -> the sign-in URL that fix printed
  return {
    check: () => check(),
    busy: (id) => busy.has(id),
    async fix(id) {
      if (busy.has(id)) return { ok: false, error: 'already running' };
      const before = await check();
      const it = before.items.find((i) => i.id === id);
      if (!it) return { ok: false, error: `unknown check: ${id}`, report: before };
      if (it.ok || !it.fix) return { ok: it.ok, report: before };
      const fix = it.fix;
      if (fix.kind === 'open') {
        openUrl(fix.url);
        return { ok: true, opened: fix.url, report: before };
      }
      if (fix.kind !== 'run') return { ok: false, error: fix.text, report: before };
      busy.add(id);
      links.delete(id);
      let code;
      const onLine = (line) => {
        const p = { id, running: true, line };
        if (fix.interactive) {
          const url = signinUrlIn(line);
          if (url) { links.set(id, url); p.link = true; }
          if (CODE_PROMPT_RE.test(line)) p.needsCode = true;
        }
        onProgress(p);
      };
      const opts = fix.interactive
        ? { interactive: true, onStdin: (write) => { stdin.set(id, write); } }
        : { interactive: false };
      try {
        onProgress({ id, running: true, line: `${fix.label}…` });
        code = await spawnLines(fix.cmd, fix.args, onLine, opts);
      } catch (err) {
        code = 127;
        onProgress({ id, running: true, line: String(err && err.message ? err.message : err) });
      } finally {
        busy.delete(id);
        stdin.delete(id);
        links.delete(id);
      }
      const report = await check();
      onProgress({ id, running: false, code });
      return code === 0 ? { ok: true, code, report } : { ok: false, code, error: `${fix.label} failed (exit ${code})`, report };
    },
    // The code field: one cleaned line to a RUNNING interactive fix's stdin.
    send(id, text) {
      const write = stdin.get(id);
      if (!write) return { ok: false, error: 'nothing is waiting for a code' };
      const line = cleanCode(text);
      if (!line) return { ok: false, error: 'that does not look like a sign-in code' };
      try { write(`${line}\n`); } catch (err) { return { ok: false, error: String(err && err.message ? err.message : err) }; }
      onProgress({ id, running: true, line: 'Code sent — finishing sign-in…' });
      return { ok: true };
    },
    // "Open the sign-in page": only the URL the running fix itself printed.
    openLink(id) {
      const url = links.get(id);
      if (!url) return { ok: false, error: 'no sign-in page to open' };
      openUrl(url);
      return { ok: true };
    },
  };
}
