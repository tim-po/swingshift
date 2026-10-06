// The onboarding-guide panel's logic (deliverable 3) — pure, no Electron.
//
// Once this machine is connected the app loads the web UI at once and docks the
// onboarding guide beside it: the welcome + the getting-started checklist whose
// buttons move the web UI to the surface each step happens on. EVERYTHING it
// shows — the steps, their routes and button labels, the explore items, the key
// places — comes from `yard onboard --json` (mcp_loops/onboarding.py, the one
// source of truth; the web app's Home checklist reads the same payload through
// GET /api/loops/onboarding). This module keeps no copy of those tables: it only
// vets what the engine sends, lays the panel out and remembers the dismiss.
//
// A step is ticked ONLY when the engine says it is done (`steps[].done: true`),
// never because the user clicked Go. main.js re-reads the payload while the
// panel is open, so ticks follow the real state. The agent's own greeting
// (persona + the prompt its CLI session runs) is shown too.

/** The yard verb the panel's content comes from. */
export const GUIDE_ARGV = Object.freeze(['onboard', '--json']);

// Used only when the yard CLI is too old for `onboard --json`: a short welcome,
// no routes (so no buttons) — the panel says the engine needs an update.
export const FALLBACK_GUIDE = Object.freeze({
  source: 'fallback',
  agent: { id: 'loop_onboarding', name: 'Loopyard guide' },
  welcome: 'Welcome to Loopyard. Describe what you want done, review the team it gets, and start it.',
  steps: [],
  explore: [],
  places: [],
});

const str = (v) => (typeof v === 'string' ? v : '');

/** A route from the payload, or null unless it is a plain in-app path
 *  ("/machines/computers") — never a URL, protocol-relative path or script. */
export function safeRoute(r) {
  return typeof r === 'string' && /^\/(?!\/)[A-Za-z0-9/_-]*$/.test(r) ? r : null;
}

/** A payload step's completion as the ENGINE reports it: true / false, or null
 *  when the engine does not say (older engine) — never inferred from clicks.
 *  Reads `done: boolean`, or `state`/`status` of 'done' | 'todo' | 'pending'. */
export function stepDone(s) {
  if (typeof s.done === 'boolean') return s.done;
  const v = str(s.state) || str(s.status);
  if (v === 'done' || v === 'complete' || v === 'completed') return true;
  if (v === 'todo' || v === 'pending' || v === 'open') return false;
  return null;
}

const item = (s) => ({ key: s.key, title: s.title, what: str(s.what), action: str(s.action), route: safeRoute(s.route), done: stepDone(s) });
const items = (list) => (Array.isArray(list) ? list.filter((s) => s && str(s.key) && str(s.title)).map(item) : []);

/** `yard onboard --json` stdout → the panel's guide, or null if it is not one. */
export function parseGuide(stdout) {
  let o;
  try { o = JSON.parse(String(stdout || '').trim()); } catch { return null; }
  if (!o || typeof o !== 'object' || !str(o.welcome) || !Array.isArray(o.steps)) return null;
  const steps = items(o.steps);
  if (!steps.length) return null;
  const a = o.agent && typeof o.agent === 'object' ? o.agent : {};
  const places = (Array.isArray(o.places) ? o.places : [])
    .filter((p) => p && str(p.key) && str(p.label))
    .map((p) => ({ key: p.key, label: p.label, what: str(p.what), route: safeRoute(p.route) }));
  return {
    source: 'agent',
    agent: { id: str(a.id) || 'loop_onboarding', name: 'Loopyard guide', persona: str(a.persona) },
    welcome: o.welcome,
    // the prompt a CLI session runs the guide with (loop_onboarding_agent)
    prompt: str(o.prompt),
    steps,
    explore: items(o.explore),
    places,
  };
}

/** Run `yard onboard --json` (async; never rejects) → the guide, else the fallback. */
export async function loadGuide({ run, cmd, pre = [], dataDir } = {}) {
  if (!run || !cmd) return FALLBACK_GUIDE;
  const argv = [...pre, ...GUIDE_ARGV];
  if (dataDir) argv.push('--data-dir', dataDir);
  try {
    const r = await run(cmd, argv);
    return (r && r.code === 0 && parseGuide(r.stdout)) || FALLBACK_GUIDE;
  } catch {
    return FALLBACK_GUIDE;
  }
}

/** The web-UI route a step / explore / place key opens in `guide`, or null
 *  (unknown keys, and keys whose route is missing or unsafe → null). */
export function routeFor(guide, key) {
  if (!guide || typeof key !== 'string' || !key) return null;
  for (const list of [guide.steps, guide.explore, guide.places]) {
    const hit = (list || []).find((x) => x.key === key);
    if (hit) return safeRoute(hit.route);
  }
  return null;
}

/** How to show the web UI at `route`. The bundled build uses the hash router
 *  (VITE_ROUTER=hash): already loaded → just set location.hash (no reload);
 *  else load the file with that hash. A hosted /app uses path routes under its
 *  base URL. */
export function webTarget({ hosted = '', route = '/', loaded = false } = {}) {
  const r = route && route.startsWith('/') ? route : '/';
  if (hosted) return { kind: 'url', url: hosted.split('#')[0].replace(/\/+$/, '') + r };
  return loaded ? { kind: 'hash', hash: r } : { kind: 'file', hash: r };
}

/** The docked panel's bounds on the right edge; null when closed. */
export function panelBounds({ width, height }, open, panelWidth = 360) {
  if (!open || !(width > 0) || !(height > 0)) return null;
  const w = Math.max(280, Math.min(panelWidth, Math.floor(width * 0.45)));
  return { x: Math.max(0, width - w), y: 0, width: Math.min(w, width), height };
}

// Only the dismiss is remembered; a `done` list written by an older app (ticks
// on click) is ignored — completion comes from the engine on every read.
function cleanState(s) {
  return { dismissed: !!(s && typeof s === 'object' && s.dismissed === true) };
}

/** How often main.js re-reads the payload while the panel is open (ms). */
export const GUIDE_REFRESH_MS = 8000;

/**
 * The panel controller main.js drives. `readState()`/`writeState(s)` persist
 * {dismissed} (a JSON file in the app's userData); `load()` re-reads the guide
 * (loadGuide) so ticks follow the engine; `navigate(route)` moves the web UI;
 * `setOpen(bool)` shows/hides the docked view; `onChange` pushes the new view
 * to the panel page.
 */
export function createGuideController({ guide, load, readState, writeState, navigate, setOpen, onChange = () => {} } = {}) {
  let state = cleanState(readState ? readState() : null);
  let open = false;
  let g = guide || FALLBACK_GUIDE;
  const persist = () => { if (writeState) writeState(state); };
  const emit = () => { const v = view(); onChange(v); return v; };

  function view() {
    const steps = (g.steps || []).map((s) => ({ ...s, done: s.done === true }));
    const tracked = (g.steps || []).some((s) => typeof s.done === 'boolean');
    const done = steps.filter((s) => s.done).length;
    return {
      source: g.source,
      agent: g.agent,
      welcome: g.welcome,
      prompt: g.prompt || '',
      // does the engine report completion? (false → the panel says it can't tick)
      tracked,
      steps,
      progress: { done, total: steps.length, complete: tracked && steps.length > 0 && done === steps.length },
      explore: (g.explore || []).map((s) => ({ ...s, done: s.done === true })),
      places: (g.places || []).map((p) => ({ ...p })),
      open,
      dismissed: state.dismissed,
    };
  }

  function show(v) {
    open = !!v;
    if (setOpen) setOpen(open);
  }

  let inflight = null;
  return {
    view,
    isOpen: () => open,
    // Re-read the payload; a failed read keeps the last good guide (a fallback
    // never replaces real agent content). → the view.
    async refresh() {
      if (!load) return view();
      if (!inflight) {
        inflight = (async () => {
          try {
            const next = await load();
            if (next && (next.source === 'agent' || g.source !== 'agent')) g = next;
          } catch { /* keep the last good guide */ }
        })().finally(() => { inflight = null; });
      }
      await inflight;
      return emit();
    },
    // First time connected: open unless the user said "don't show again".
    autoOpen() {
      if (!state.dismissed) show(true);
      return emit();
    },
    // A step/place button: only a key the ENGINE sent, with a safe route, moves
    // the web UI (the page sends a key, never a route or URL). It does NOT tick
    // the step — the engine does.
    go(key) {
      const route = routeFor(g, String(key || ''));
      if (!route) return { ok: false, error: `unknown guide step: ${key}` };
      navigate(route);
      return { ok: true, route, view: emit() };
    },
    hide() { show(false); return emit(); },
    reopen() { show(true); return emit(); },
    // "Don't show on start": remembered; the Help menu still reopens it.
    dismiss() {
      state = { ...state, dismissed: true };
      persist();
      show(false);
      return emit();
    },
  };
}
