// The desktop GUI's origin-agent FACE — a thin supervisor over the SAME CLI-first
// engine (HUB-FABRIC §6.3, P2.5). There is NO second daemon and NO re-implemented
// engine here: "Join as origin" SHELLS OUT to the identical headless
// `yard origin up` a terminal user pastes (yard start → pair once → detached
// daemon → wait connected → exit 0), "Disconnect" runs `yard origin down`, and
// the connected state is POLLED from the identical `yard origin status --json`.
// One engine, N faces — the Mac app is just another face.
//
// Contract with the CLI (mcp_loops/yard.py, P2.5 T2/T3):
//   • the pairing code NEVER goes on argv (yard refuses it — `ps` leaks argv).
//     The face passes `--pair-code -` and writes the code to the child's STDIN.
//   • `origin up` is headless: it RETURNS once the detached daemon is connected
//     (rc 0), or fails with one clear stderr line (rc 2 refused / 3 bring-up).
//     So the `up` child is short-lived; the daemon it leaves outlives the app.
//   • `origin up` is idempotent: re-clicking Connect on a live box is a no-op.
//
// Every side-effect (spawning, running `down`, reading status, clock, timer) is
// INJECTED, so the whole lifecycle is driven deterministically in a unit test
// with zero processes and no display. Electron's main.js supplies the real seams.
// Keep the phase decision in lock-step with `originStatusView` in packages/api.

// A status.json whose `ts` is older than this is treated as STALE — matches
// STATUS_STALE_AFTER_S in mcp_loops/origin_client.py. `yard origin status` already
// stamps `stale`; we recompute only as a defensive backstop.
export const STATUS_STALE_AFTER_S = 30.0;

const stateArgs = ({ dataDir } = {}) => dataDir ? ['--data-dir', dataDir] : [];
export function buildStartArgv(opts = {}) {
  const argv = [...stateArgs(opts), 'start'];
  if (opts.port != null) argv.push('--port', String(opts.port));
  if (opts.dashPort != null) argv.push('--dash-port', String(opts.dashPort));
  return argv;
}
export const buildYardStatusArgv = (opts = {}) => [...stateArgs(opts), 'status', '--json'];
export const buildStopArgv = (opts = {}) => [...stateArgs(opts), 'down'];

/** Build the argv for "Join as origin" — the EXACT headless `yard origin up` the
 *  Hub's minted one-liner runs. A pairing code is signalled as `--pair-code -`
 *  (the value is fed on stdin by the caller — see `pairCodeStdin`). `dataDir`,
 *  when set, is a GLOBAL `--data-dir` flag emitted first. */
export function buildOriginUpArgv(opts = {}) {
  const { hub, pairCode, hubFingerprint, label, originId, dataDir, noWorker, wait } = opts;
  if (!hub || !String(hub).trim()) {
    throw new Error('a Hub URL is required to connect');
  }
  const argv = [];
  if (dataDir) argv.push('--data-dir', dataDir);
  argv.push('origin', 'up', '--hub', String(hub).trim());
  if (pairCode && String(pairCode).trim()) argv.push('--pair-code', '-');
  if (hubFingerprint && String(hubFingerprint).trim()) {
    argv.push('--hub-fingerprint', String(hubFingerprint).trim());
  }
  if (label) argv.push('--label', String(label));
  if (originId) argv.push('--origin-id', String(originId));
  if (noWorker) argv.push('--no-worker');
  if (wait != null) argv.push('--wait', String(wait));
  return argv;
}

/** The stdin payload for `origin up` — the pairing code on one line, or null
 *  when there is none (already-paired box: the CLI skips the claim). */
export function pairCodeStdin(opts = {}) {
  const code = opts.pairCode == null ? '' : String(opts.pairCode).trim();
  return code ? code + '\n' : null;
}

/** Split a POSIX-shell line into words — single/double quotes and backslash
 *  escapes only (enough for the line `yard hub pair-code` prints, which quotes
 *  with shlex.quote). Never evaluates anything. */
export function shellWords(line) {
  const words = [];
  let cur = null;
  let i = 0;
  const s = String(line || '');
  while (i < s.length) {
    const c = s[i];
    if (c === "'") {
      const j = s.indexOf("'", i + 1);
      if (j < 0) throw new Error('unterminated single quote');
      cur = (cur || '') + s.slice(i + 1, j);
      i = j + 1;
    } else if (c === '"') {
      let j = i + 1;
      let buf = '';
      while (j < s.length && s[j] !== '"') {
        if (s[j] === '\\' && j + 1 < s.length) { buf += s[j + 1]; j += 2; } else { buf += s[j]; j += 1; }
      }
      if (j >= s.length) throw new Error('unterminated double quote');
      cur = (cur || '') + buf;
      i = j + 1;
    } else if (c === '\\' && i + 1 < s.length) {
      cur = (cur || '') + s[i + 1];
      i += 2;
    } else if (/\s/.test(c)) {
      if (cur !== null) { words.push(cur); cur = null; }
      i += 1;
    } else if (c === '|' || c === ';' || c === '&') {
      if (cur !== null) { words.push(cur); cur = null; }
      words.push(c);
      i += 1;
    } else {
      cur = (cur || '') + c;
      i += 1;
    }
  }
  if (cur !== null) words.push(cur);
  return words;
}

/** Parse the join line the Hub mints (`yard hub pair-code` / MCP
 *  `origin_pair_code`), e.g.
 *    [curl -fsSL URL/install.sh | sh && ] printf '%s\n' CODE |
 *      ~/loopyard/bin/yard origin up --hub wss://H:P --hub-fingerprint FP --pair-code -
 *  → {hub, hubFingerprint, pairCode, installUrl}. The desktop EXTRACTS these three fields
 *  and runs its own `yard origin up`; it never executes the pasted text. Throws
 *  when the text is not such a line. */
export function parseJoinLine(text) {
  const w = shellWords(String(text || '').trim());
  const up = w.findIndex((t, k) => t === 'origin' && w[k + 1] === 'up');
  if (up < 0) throw new Error('not a Loopyard join line (no `yard origin up`)');
  const out = { hub: null, hubFingerprint: null, pairCode: null, installUrl: null };
  // `curl -fsSL URL/install.sh | sh && …` — the installer the prereq check offers
  // when the yard bundle is missing (validated again before it is ever run).
  for (let k = 0; k < up; k += 1) {
    if (/^https:\/\/\S+\/install\.sh$/.test(w[k])) { out.installUrl = w[k]; break; }
  }
  for (let k = up + 2; k < w.length && !['|', ';', '&'].includes(w[k]); k += 1) {
    if (w[k] === '--hub') out.hub = w[k + 1] || null;
    if (w[k] === '--hub-fingerprint') out.hubFingerprint = w[k + 1] || null;
  }
  const pf = w.lastIndexOf('printf', up);
  if (pf >= 0 && w[pf + 2] && w[pf + 3] === '|') out.pairCode = w[pf + 2];
  if (!out.hub) throw new Error('join line has no --hub URL');
  return out;
}

/** The scheme of the single hub-minted join link the app accepts:
 *    loopyard://join?hub=<wss URL>&fp=<hub cert fingerprint>&code=<pair code>[&install=<https …/install.sh>]
 *  (all values URL-encoded). One copy-paste carries every field the form needs,
 *  so the user never sees or assembles a CLI command. */
export const JOIN_LINK_PREFIX = 'loopyard://join';

/** Parse a join link → {hub, hubFingerprint, pairCode, installUrl}. Throws when
 *  the text is not a `loopyard://join?…` link or has no hub. */
export function parseJoinLink(text) {
  const t = String(text || '').trim();
  if (!t.toLowerCase().startsWith(JOIN_LINK_PREFIX)) throw new Error('not a Loopyard join link');
  let u;
  try {
    // WHATWG URL does not parse a custom scheme's query reliably everywhere;
    // re-root it on a dummy https origin to read the params.
    u = new URL('https://join.invalid/' + t.slice(JOIN_LINK_PREFIX.length).replace(/^\/?/, ''));
  } catch {
    throw new Error('join link is malformed');
  }
  const q = (k) => {
    const v = u.searchParams.get(k);
    return v && v.trim() ? v.trim() : null;
  };
  const out = { hub: q('hub'), hubFingerprint: q('fp'), pairCode: q('code'), installUrl: null };
  const inst = q('install');
  if (inst && /^https:\/\/\S+\/install\.sh$/.test(inst)) out.installUrl = inst;
  if (!out.hub) throw new Error('join link has no hub');
  if (!/^wss?:\/\//i.test(out.hub)) throw new Error('join link hub must be a ws:// or wss:// URL');
  return out;
}

/** Build the join link for these fields (the inverse of parseJoinLink) — what a
 *  Hub-side minter emits. */
export function buildJoinLink({ hub, hubFingerprint, pairCode, installUrl } = {}) {
  if (!hub) throw new Error('a hub is required');
  const q = new URLSearchParams();
  q.set('hub', hub);
  if (hubFingerprint) q.set('fp', hubFingerprint);
  if (pairCode) q.set('code', pairCode);
  if (installUrl) q.set('install', installUrl);
  return `${JOIN_LINK_PREFIX}?${q.toString()}`;
}

/** Whatever the user pasted into the join field — the one-click link, or (the
 *  fallback) the terminal line `yard hub pair-code` prints. Same result shape. */
export function parseJoinInput(text) {
  const t = String(text || '').trim();
  if (t.toLowerCase().startsWith(JOIN_LINK_PREFIX)) return parseJoinLink(t);
  return parseJoinLine(t);
}

/** A cert fingerprint in the Hub's shape: lowercase hex, colon-grouped (what
 *  `yard hub fingerprint` prints). Accepts upper case, no colons, or a
 *  `SHA256:` prefix. Returns '' for anything that is not 32 bytes of hex. */
export function normalizeFingerprint(fp) {
  const hex = String(fp || '').trim().replace(/^sha-?256:/i, '').replace(/[:\s]/g, '').toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(hex)) return '';
  return hex.match(/../g).join(':');
}

/** Compare the fingerprint the user was given (join link / form) with the one
 *  the Hub actually presents → the form's trust state:
 *    'match'     — both present and equal: safe to pair;
 *    'mismatch'  — both present, different: REFUSE (wrong Hub or MITM);
 *    'unconfirmed' — the Hub presented one but none was given: the user must
 *                  confirm it (trust on first use) before Connect;
 *    'plaintext' — a ws:// Hub (loopback only): no cert to check;
 *    'unknown'   — could not reach the Hub to read its cert. */
export function fingerprintState({ hub, expected, seen, error } = {}) {
  if (hub && /^ws:\/\//i.test(String(hub).trim())) return 'plaintext';
  const s = normalizeFingerprint(seen);
  if (error || !s) return 'unknown';
  const e = normalizeFingerprint(expected);
  if (!String(expected || '').trim()) return 'unconfirmed';
  return e === s ? 'match' : 'mismatch';
}

/** Build the argv for the status poll — the EXACT `yard origin status --json`. */
export function buildStatusArgv(opts = {}) {
  const argv = [];
  if (opts.dataDir) argv.push('--data-dir', opts.dataDir);
  argv.push('origin', 'status', '--json');
  return argv;
}

/** Build the argv for "Disconnect" — the EXACT `yard origin down` (stops the
 *  detached daemon by its recorded pid; engine + worker stay up). */
export function buildOriginDownArgv(opts = {}) {
  const argv = [];
  if (opts.dataDir) argv.push('--data-dir', opts.dataDir);
  argv.push('origin', 'down');
  return argv;
}

/** The one-line error a failed `origin up` printed. yard writes the reason as the
 *  first stderr line (`yard: …` / `yard start: …` / `yard origin up: …`), then an
 *  optional log tail.
 *  Credential-shaped tokens are never echoed by yard, so this is safe to show. */
export function summarizeUpFailure(stderr, code, verb = 'up') {
  const lines = String(stderr || '').split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const PREFIX = /^yard(?: [a-z-]+){0,2}:\s*/; // `yard:` `yard start:` `yard origin up:`
  const first = lines.find((l) => PREFIX.test(l)) || lines[0];
  if (first) return first.replace(PREFIX, '');
  return `yard origin ${verb} exited with code ${code}`;
}

/** The origin bundle staged into the .app (PHASE-B-SPEC §1.9, electron-builder
 *  mac.extraResources → <resourcesPath>/loopyard). */
export function bundledYardPath(resourcesPath) {
  return resourcesPath ? `${String(resourcesPath).replace(/\/+$/, '')}/loopyard/bin/yard` : '';
}

/** How to invoke the yard CLI from a GUI (macOS apps launched from Finder get a
 *  minimal PATH, so don't rely on `yard` being on it). Spec R27 order:
 *    1. $LOOPYARD_YARD naming an EXISTING file — ONE path, never whitespace-split
 *       (a path with spaces such as "/Users/Tim Po/loopyard/bin/yard" survives);
 *    2. the per-version appState/code/<version>/bin/yard copy (bundled: true),
 *       then the bundle inside the app, <resourcesPath>/loopyard/bin/yard — set
 *       directly, never via an env string (`bundled: true`);
 *    3. $LOOPYARD_YARD otherwise: split ONLY when its first word is an existing
 *       file (a dev value like "/v/bin/python -m mcp_loops.yard"); anything else
 *       stays one path, as before;
 *    4. $LOOPYARD_PYTHON — `<python> -m mcp_loops.yard` (dev checkouts);
 *    5. <home>/loopyard/bin/yard — where install.sh puts it by default;
 *    6. `yard` on PATH. */
export function resolveYardCommand({ env = {}, home = '', exists = () => false, resourcesPath = '', appState = '', version = '' } = {}) {
  const pinned = env.LOOPYARD_YARD;
  if (pinned && exists(pinned)) return { cmd: pinned, pre: [], bundled: false };
  const copied = appState && version ? `${appState}/code/${version}/bin/yard` : '';
  if (copied && exists(copied)) return { cmd: copied, pre: [], bundled: true };
  const bundled = bundledYardPath(resourcesPath);
  if (bundled && exists(bundled)) return { cmd: bundled, pre: [], bundled: true };
  if (pinned) {
    const words = pinned.trim().split(/\s+/);
    if (words.length > 1 && exists(words[0])) return { cmd: words[0], pre: words.slice(1), bundled: false };
    return { cmd: pinned, pre: [], bundled: false };
  }
  if (env.LOOPYARD_PYTHON) return { cmd: env.LOOPYARD_PYTHON, pre: ['-m', 'mcp_loops.yard'], bundled: false };
  if (home) {
    const installed = `${home.replace(/\/+$/, '')}/loopyard/bin/yard`;
    if (exists(installed)) return { cmd: installed, pre: [], bundled: false };
  }
  return { cmd: 'yard', pre: [], bundled: false };
}

// AF_UNIX sun_path limit on macOS (loopyard-quickstart.sh:55-66); `yard start`'s
// preflight stays the authority and fails loud either way.
export const SOCK_PATH_MAX = 104;

/** The state root (LOOPYARD_HOME) for yard children spawned from the APP'S OWN
 *  bundle (spec §1.9, D12): the bundled code tree is read-only, so state goes to
 *  the OS app-data dir, never inside the app.
 *    macOS: <appData>/Loopyard  (~/Library/Application Support/Loopyard)
 *    Linux: ${XDG_DATA_HOME:-~/.local/share}/loopyard
 *  Falls back to ~/.loopyard when the worker socket path would reach the AF_UNIX
 *  limit. A LOOPYARD_HOME already in the env wins. Returns {home, reason} where
 *  `reason` is a one-line log for the fallback, or {home: ''} when n/a (Windows). */
export function appStateHome({ platform = '', home = '', appData = '', env = {}, user = '' } = {}) {
  if (env.LOOPYARD_HOME) return { home: env.LOOPYARD_HOME, reason: '' };
  const h = String(home || '').replace(/\/+$/, '');
  let root = '';
  if (platform === 'darwin' && appData) root = `${String(appData).replace(/\/+$/, '')}/Loopyard`;
  else if (platform === 'linux' && h) root = `${env.XDG_DATA_HOME || `${h}/.local/share`}/loopyard`;
  if (!root) return { home: '', reason: '' };
  const sock = `${root}/data/_sock/user-${user}.sock`;
  const len = new TextEncoder().encode(sock).length;
  if (len >= SOCK_PATH_MAX && h) {
    return { home: `${h}/.loopyard`, reason: `worker socket ${len} chars ≥ ${SOCK_PATH_MAX} under ${root}; using LOOPYARD_HOME=${h}/.loopyard` };
  }
  return { home: root, reason: '' };
}

/** The env for spawned yard processes: the GUI's env + the usual CLI bins
 *  (`~/.local/bin` is where the claude installer puts `claude`, which the
 *  `yard start` preflight needs; Homebrew prefixes for git). `loopyardHome`, when
 *  given (the app runs its own bundle), becomes the children's LOOPYARD_HOME. */
export function guiSpawnEnv(env = {}, home = '', { loopyardHome = '' } = {}) {
  const extra = [
    home ? `${home.replace(/\/+$/, '')}/.local/bin` : null,
    home ? `${home.replace(/\/+$/, '')}/loopyard/bin` : null,
    '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin',
  ].filter(Boolean);
  const have = String(env.PATH || '').split(':').filter(Boolean);
  const path = [...have, ...extra.filter((p) => !have.includes(p))].join(':');
  const out = { ...env, PATH: path };
  if (loopyardHome) out.LOOPYARD_HOME = loopyardHome;
  return out;
}

export const RELEASE_URL = 'https://github.com/tim-po/loopyard-releases/releases/latest';
export function dashboardUrl(status) {
  const svc = status?.services?.find(s => s.name === 'dashboard');
  const port = Number(status?.dashPort ?? svc?.port);
  const live = typeof status?.live === 'boolean' ? status.live : svc?.state === 'running';
  return live && Number.isInteger(port) && port > 0 && port <= 65535
    ? `http://127.0.0.1:${port}/app/` : '';
}

const LIVE_ISH = new Set(['connected', 'reconnecting', 'connecting']);

/** Recompute `stale` from a status payload's `ts`. */
export function stampStale(status, now) {
  if (!status || typeof status !== 'object') return status;
  const ts = Number(status.ts);
  const age = Number.isFinite(ts) ? now - ts : NaN;
  const stale = !Number.isFinite(age) || age > STATUS_STALE_AFTER_S;
  return { ...status, stale };
}

/** Reduce a status payload to the coarse GUI decision. The safety-critical rule:
 *  a STALE `connected` is DEMOTED to the connect screen so a dead daemon never
 *  reads as a false "✓". `null`/absent status means no daemon has ever run. */
export function derivePhase(status) {
  if (!status || typeof status !== 'object') {
    return { phase: 'connect', state: 'unknown', live: false, stale: false };
  }
  const state = String(status.state || 'unknown');
  const stale = !!status.stale;
  if (stale && LIVE_ISH.has(state)) {
    return { phase: 'connect', state, live: false, stale: true };
  }
  switch (state) {
    case 'connected':
      return { phase: 'connected', state, live: true, stale: false };
    case 'connecting':
    case 'reconnecting':
      return { phase: 'connecting', state, live: false, stale: false };
    default: // idle | stopped | error | unknown
      return { phase: 'connect', state, live: false, stale };
  }
}

/** One line of "who am I on the Hub" for a LIVE view — read verbatim from the
 *  same `yard origin status --json` payload (deviceId, workerPid, hub), never
 *  inferred. `yard origin status` falls back to the worker `yard start` spawned,
 *  so a composed join reports its worker here too. Returns '' when not live. */
export function describeConnection(status, live) {
  if (!live || !status || typeof status !== 'object') return '';
  const parts = [];
  if (status.deviceId) parts.push(`device ${status.deviceId}`);
  parts.push(status.workerPid ? `worker pid ${status.workerPid}` : 'no worker running');
  if (status.hub) parts.push(`hub ${status.hub}`);
  return parts.join(' · ');
}

const DEFAULT_POLL_MS = 1500;
// While a join is in flight the face polls faster, so the connect screen's
// progress (and the switch to the web UI) follows the daemon closely.
const DEFAULT_JOIN_POLL_MS = 500;

/** The visible stages of a join, for the connect screen's progress strip.
 *  Derived ONLY from what the face knows: an `up` child in flight, and the
 *  daemon's own status.json state. `origin up` prints nothing while it runs.
 *  Returns null when no join is under way (nothing to show). */
export const JOIN_STAGES = [
  { key: 'start', label: 'Starting Loopyard on this computer' },
  { key: 'dial', label: 'Pairing with the Hub' },
  { key: 'ui', label: 'Opening your workspace' },
];
export function joinProgress({ joining = false, state = '', live = false, startedAt = null, now = null } = {}) {
  const dialing = state === 'connecting' || state === 'reconnecting';
  if (!joining && !dialing && !live) return null;
  if (!joining && !startedAt) return null; // a daemon reconnecting on its own: no join strip
  const at = live ? 2 : dialing ? 1 : 0;
  const stages = JOIN_STAGES.map((st, i) => ({ ...st, state: i < at ? 'done' : i === at ? 'active' : 'todo' }));
  const elapsed = startedAt != null && now != null ? Math.max(0, Math.round(now - startedAt)) : null;
  return { stages, active: JOIN_STAGES[at].key, elapsed };
}

/**
 * The GUI face. Drives the join lifecycle over injected seams:
 *   - `spawn(argv, stdin) -> child` : start `yard origin up …`, writing `stdin`
 *        (the pair code line, or null) to it. child: {pid, kill(), once('exit',
 *        (code, stderr) => …)} — main.js collects stderr and passes it on exit.
 *   - `run(argv) -> {code, stderr}`  : run a short verb to completion (`origin down`).
 *   - `readStatus(argv) -> status`   : parsed `origin status --json` (or null).
 *   - `now()`, `setTimer/clearTimer`, `onChange(view)`, `dataDir`.
 *
 * While the `up` child is in flight the face shows `connecting` (state
 * `joining`) — the daemon's status.json does not exist yet during `yard start` +
 * pairing, and the face must not flash the connect form. A non-zero exit lands
 * on the connect screen with `error` = the CLI's own one-line reason (bad pair
 * code, unreachable Hub, missing claude, …). A zero exit hands over to the
 * status poll: the daemon is detached and outlives both the child and the app.
 */
export class OriginAgentFace {
  // Legacy callers default to joined. The desktop passes mode: 'local' and a
  // readLocalStatus seam; Join then polls both status surfaces.
  constructor({ spawn, run, readStatus, readLocalStatus, mode = 'joined', onChange, now, pollMs, joinPollMs, setTimer, clearTimer, dataDir } = {}) {
    if (typeof spawn !== 'function') throw new Error('OriginAgentFace needs a spawn(argv, stdin) seam');
    if (typeof readStatus !== 'function') throw new Error('OriginAgentFace needs a readStatus() seam');
    this._spawn = spawn;
    this._run = typeof run === 'function' ? run : null;
    this._readStatus = readStatus;
    this._readLocalStatus = readLocalStatus || readStatus;
    this._hasLocalStatus = !!readLocalStatus;
    this._onChange = typeof onChange === 'function' ? onChange : () => {};
    this._now = typeof now === 'function' ? now : () => Date.now() / 1000;
    this._pollMs = pollMs || DEFAULT_POLL_MS;
    this._joinPollMs = joinPollMs || DEFAULT_JOIN_POLL_MS;
    this._mode = mode;
    this._newerVersion = false;
    this._seq = 0; // orders async status reads: an older answer never overwrites a newer one
    this._joinStartedAt = null;
    this._disposed = false;
    this._setTimer = setTimer || ((fn, ms) => setTimeout(fn, ms));
    this._clearTimer = clearTimer || ((h) => clearTimeout(h));
    this._dataDir = dataDir || undefined;
    this._child = null;
    this._timer = null;
    this._error = null;
    this._view = { phase: 'connect', state: 'idle', live: false, stale: false, status: null, error: null, detail: '', progress: null };
  }

  /** The current derived view (a copy) — what the renderer branches on. */
  view() {
    return { ...this._view };
  }

  /** True while a `yard origin up` child is in flight. */
  joining() {
    return this._child != null;
  }

  _emit(next) {
    const changed = next.phase !== this._view.phase
      || next.state !== this._view.state
      || next.live !== this._view.live
      || next.stale !== this._view.stale
      || next.error !== this._view.error
      || next.dashboardUrl !== this._view.dashboardUrl
      || next.detail !== this._view.detail
      || JSON.stringify(next.progress) !== JSON.stringify(this._view.progress);
    this._view = next;
    if (changed) {
      try {
        this._onChange(this.view());
      } catch { /* a broken face callback must never kill the poll loop */ }
    }
  }

  /** Read status once (via `yard origin status --json`), derive + emit the view.
   *  `readStatus` may be synchronous (returns the view) or async (returns a
   *  Promise of the view — the app's seam, so a poll never blocks the window). */
  poll() {
    const seq = ++this._seq;
    let status = null;
    try {
      const local = this._mode === 'local' || this._hasLocalStatus ? this._readLocalStatus(buildYardStatusArgv({dataDir: this._dataDir})) : null;
      if (this._mode === 'joined') {
        const joined = this._readStatus(buildStatusArgv({ dataDir: this._dataDir }));
        const combine = (l, j) => j ? { ...j, services: l?.services, live: l?.live, dashPort: l?.dashPort } : null;
        status = local?.then || joined?.then ? Promise.all([local, joined]).then(([l, j]) => combine(l, j)) : combine(local, joined);
      } else status = local;
    } catch {
      status = null; // an unreadable status reads as "no daemon", honestly
    }
    if (status && typeof status.then === 'function') {
      return status.then((s) => s, () => null).then((s) => {
        if (seq === this._seq) this._apply(s); // superseded by a newer read: drop it
        return this.view();
      });
    }
    this._apply(status);
    return this.view();
  }

  _apply(status) {
    const url = dashboardUrl(status);
    if (status && (!status.services || this._mode === 'joined')) status = stampStale(status, this._now());
    let d = derivePhase(status);
    if (this._mode === 'local' && status && (status.services || typeof status.live === 'boolean')) d = {phase: url ? 'connected' : 'connect', state: url ? 'running' : 'stopped', live: !!url, stale: false};
    const daemonState = d.stale ? '' : d.state; // what the daemon itself reports
    if (d.live && !this._newerVersion) this._error = null; // a genuine connect clears an old failure
    if (this._child && !d.live) {
      // `up` is still running (yard start / pairing / waiting for the dial).
      d = { phase: 'connecting', state: 'joining', live: false, stale: false };
    }
    const progress = joinProgress({
      joining: !!this._child, state: daemonState, live: d.live,
      startedAt: this._joinStartedAt, now: this._now(),
    });
    if (!this._child && (d.live || d.phase === 'connect')) this._joinStartedAt = null; // the join is over
    if (this._newerVersion && !this._child) d = { ...d, phase: 'newer-version', live: false };
    this._emit({ ...d, mode: this._mode === 'joined' ? 'hub' : 'local', dashboardUrl: url, releaseUrl: RELEASE_URL, status: status || null, error: this._error, detail: describeConnection(status, d.live), progress });
  }

  /** Begin polling (idempotent). The app calls this at launch so a daemon
   *  brought up by the CLI (or a previous app session) is picked up. */
  watch() {
    this.poll();
    if (!this._timer) this._scheduleNext();
    return this.view();
  }

  /** Poll cadence: fast while a join is in flight or the daemon is still
   *  dialling, the idle rate otherwise. */
  pollInterval() {
    const v = this._view;
    return this._child || v.state === 'connecting' || v.state === 'reconnecting' ? this._joinPollMs : this._pollMs;
  }

  _scheduleNext() {
    if (this._disposed) return;
    this._timer = this._setTimer(() => {
      this._timer = null;
      const r = this.poll();
      // an async read: the next tick waits for this one, so reads never pile up
      if (r && typeof r.then === 'function') r.then(() => { if (!this._timer) this._scheduleNext(); });
      else this._scheduleNext();
    }, this.pollInterval());
  }

  /** Re-arm the poll timer at the CURRENT cadence (after a join starts). */
  _rearm() {
    if (this._disposed) return;
    if (this._timer) this._clearTimer(this._timer);
    this._timer = null;
    this._scheduleNext();
  }

  /** "Join as origin": spawn the headless `yard origin up …` with the pair code
   *  on stdin. `opts.joinLine` (the Hub's minted paste line) may stand in for
   *  hub/hubFingerprint/pairCode. Idempotent while a join is in flight. Returns
   *  the child's pid. Throws only on a bad connect form (no Hub URL / bad line). */
  start(opts = {}) {
    if (this._child) return this._child.pid;
    if (opts.joinLine && String(opts.joinLine).trim()) {
      // the owner pasted the Hub's join link (or its minted terminal line): its
      // fields fill the form (explicit fields still win), the pasted text itself
      // is never executed.
      const parsed = parseJoinInput(opts.joinLine);
      const pick = (k) => (opts[k] && String(opts[k]).trim() ? opts[k] : parsed[k]);
      opts = { ...opts, hub: pick('hub'), hubFingerprint: pick('hubFingerprint'), pairCode: pick('pairCode') };
    }
    const argv = buildOriginUpArgv({ ...opts, dataDir: this._dataDir });
    this._mode = 'joined';
    return this._launch(argv, pairCodeStdin(opts));
  }

  runLocal(opts = {}) {
    if (this._child) return this._child.pid;
    this._mode = 'local';
    return this._launch(buildStartArgv({...opts, dataDir: this._dataDir}), null);
  }

  _launch(argv, stdin) {
    this._newerVersion = false;
    this._error = null;
    this._joinStartedAt = this._now();
    const child = this._spawn(argv, stdin);
    this._child = child || null;
    if (child && typeof child.once === 'function') {
      child.once('exit', (code, stderr) => {
        if (this._child !== child) return; // superseded (stop() already reaped it)
        this._child = null;
        if (code !== 0 && code !== null && code !== undefined) {
          this._error = summarizeUpFailure(stderr, code);
          this._newerVersion = code === 2 && /written by.*(?:newer|Loopyard)|state.*newer/i.test(String(stderr));
          // Desktop IPC carries structured errors; legacy Join-only consumers
          // keep their string contract (and their unchanged tests).
          if (this._mode === 'local' || this._hasLocalStatus) {
            const writtenBy = String(stderr).match(/written by Loopyard v(\d+\.\d+\.\d+[^\s;)]*)/i)?.[1];
            this._error = this._newerVersion
              ? { kind: 'newer-state', writtenBy: writtenBy || null, releaseUrl: RELEASE_URL, message: this._error }
              : { kind: 'start-failed', message: this._error };
          }
        }
        this.poll();
      });
    }
    this.poll(); // reflect "joining…" immediately, before the first timer tick
    this._rearm(); // switch to the fast join cadence now, not after the idle wait
    return child ? child.pid : null;
  }

  /** "Disconnect": abort an in-flight join (kill ONLY that child, never a broad
   *  kill), then run `yard origin down` — the SAME verb the CLI uses to stop the
   *  detached daemon. Returns {ok, error?}. Polling continues (a later CLI
   *  `origin up` is reflected). */
  stop() {
    const child = this._child;
    this._child = null;
    if (child && typeof child.kill === 'function') {
      try { child.kill('SIGTERM'); } catch { /* already gone */ }
    }
    let res = { ok: true };
    if (this._run) {
      try {
        const r = this._run(buildOriginDownArgv({ dataDir: this._dataDir })) || {};
        if (r.code !== 0) {
          res = { ok: false, error: summarizeUpFailure(r.stderr, r.code, 'down') };
        }
      } catch (err) {
        res = { ok: false, error: String(err && err.message ? err.message : err) };
      }
    }
    this._error = res.ok ? null : res.error;
    this.poll();
    return res;
  }

  stopLoopyard() {
    const child = this._child;
    this._child = null;
    if (child && typeof child.kill === 'function') {
      try { child.kill('SIGTERM'); } catch { /* already exited */ }
    }
    let result = { ok: true };
    try {
      // yard down stops all tracked services, including a joined origin daemon.
      const r = this._run ? this._run(buildStopArgv({dataDir: this._dataDir})) : {code: 0};
      if (r.code !== 0) result = {ok: false, error: summarizeUpFailure(r.stderr, r.code, 'down')};
    } catch (error) { result = {ok: false, error: String(error.message || error)}; }
    this._error = result.ok ? null : result.error;
    this._mode = 'local';
    this.poll();
    return result;
  }

  /** App quit: reap ONLY an in-flight `up` child and stop polling. The detached
   *  daemon keeps serving — exactly as when a terminal that ran `origin up`
   *  closes. Use Disconnect (`origin down`) to take the origin offline. */
  dispose() {
    this._disposed = true;
    if (this._timer) {
      this._clearTimer(this._timer);
      this._timer = null;
    }
    const child = this._child;
    this._child = null;
    if (child && typeof child.kill === 'function') {
      try { child.kill('SIGTERM'); } catch { /* already gone */ }
    }
  }
}
