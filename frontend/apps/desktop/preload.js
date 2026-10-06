// The ONLY bridge between the trusted connect screen (connect.html) and the main
// process that supervises the origin agent. contextIsolation stays on and the page
// gets NO node integration — it can only call these narrow, audited channels.
// The main process owns every real side-effect (spawning `yard origin up`, polling
// `yard origin status`); the renderer just asks.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('loopyardOrigin', {
  // Bring this Mac up as an origin — main spawns the SAME headless `yard origin up`
  // (pair code on its stdin). opts: { joinLine } or { hub, hubFingerprint, pairCode }.
  // Resolves to { ok, pid } or { ok:false, error }.
  connect: (opts) => ipcRenderer.invoke('origin:connect', opts),
  // "Run on this Mac": main re-checks the prerequisites, then runs `yard start`
  // from the per-version code copy with the app-data LOOPYARD_HOME.
  // Resolves to { ok, pid } or { ok:false, error, prerequisites? }.
  run: () => ipcRenderer.invoke('origin:run'),
  // "Stop Loopyard": main stops the local stack it started (`yard down`).
  stop: () => ipcRenderer.invoke('origin:stop'),
  // Take the origin offline — main runs the SAME `yard origin down`.
  disconnect: () => ipcRenderer.invoke('origin:disconnect'),
  // The current derived view (phase / live / state) — for first paint.
  getView: () => ipcRenderer.invoke('origin:get-view'),
  // A pasted join link (or terminal join line) → {ok, hub, hubFingerprint, hasCode, installUrl} for the
  // form to show and confirm. Parsing only; nothing is run.
  parseJoin: (text) => ipcRenderer.invoke('origin:parse-join', text),
  // {hub, expected} → {state, seen}: the cert the Hub presents vs the fingerprint
  // the user was given (match / mismatch / unconfirmed / plaintext / unknown).
  checkHub: (q) => ipcRenderer.invoke('origin:check-hub', q),
  // Subscribe to live view changes (connecting → connected → …). Returns an
  // unsubscribe fn.
  onView: (cb) => {
    const handler = (_e, view) => cb(view);
    ipcRenderer.on('origin:view', handler);
    return () => ipcRenderer.removeListener('origin:view', handler);
  },
});

// First-run prerequisite check (tmux, git, Claude Code + sign-in, the Loopyard
// engine). `fix(id)` names a check; main picks the command (prereqs.mjs).
contextBridge.exposeInMainWorld('loopyardSetup', {
  check: () => ipcRenderer.invoke('prereq:check'),
  fix: (id) => ipcRenderer.invoke('prereq:fix', id),
  // "Sign in to Claude": the code from the sign-in page → that running
  // `claude auth login`'s stdin (main cleans it to one line). → {ok, error?}
  sendCode: (id, code) => ipcRenderer.invoke('prereq:send', id, code),
  // Open the sign-in URL that running fix printed (main keeps the URL). → {ok}
  openLink: (id) => ipcRenderer.invoke('prereq:open-link', id),
  onProgress: (cb) => {
    const handler = (_e, p) => cb(p);
    ipcRenderer.on('prereq:progress', handler);
    return () => ipcRenderer.removeListener('prereq:progress', handler);
  },
});

// B-8/B-9: the app's own version (app.getVersion(), passed on the preload's argv
// so it paints before the engine is up) and the portal update banner.
const versionArg = (process.argv || []).find((a) => a.startsWith('--loopyard-version='));
contextBridge.exposeInMainWorld('loopyardApp', {
  version: versionArg ? versionArg.slice('--loopyard-version='.length) : '',
  // → {show, text, updateEnabled, disabledReason, latest, current}
  getUpdate: () => ipcRenderer.invoke('update:get'),
  // Opens the portal release page; main refuses while loops are live. → {ok, error?}
  update: () => ipcRenderer.invoke('update:open'),
  onUpdate: (cb) => {
    const handler = (_e, banner) => cb(banner);
    ipcRenderer.on('update:banner', handler);
    return () => ipcRenderer.removeListener('update:banner', handler);
  },
});
