// Thin desktop face over yard. Run starts the local stack; Join preserves the
// CLI's origin-up contract. Both display the running dashboard's /app/ over HTTP.
// Bundled code is copied outside the signed app into app-data/code/<version>,
// with LOOPYARD_HOME set to the app-data root for every child process.
// Optional development overrides: LOOPYARD_YARD, LOOPYARD_PYTHON,
// LOOPYARD_DATA_DIR. The window always uses the dashboard port reported by yard.
const { app, BrowserWindow, Menu, MenuItem, WebContentsView, ipcMain, shell, net } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

let dashboard = '';
const CONNECT = path.join(__dirname, 'connect.html');
const PRELOAD = path.join(__dirname, 'preload.js');
const GUIDE = path.join(__dirname, 'guide.html');
const GUIDE_PRELOAD = path.join(__dirname, 'guidePreload.js');
const DATA_DIR = process.env.LOOPYARD_DATA_DIR || '';
// Headless smoke: quit as soon as the first window has loaded (CI has no display).
const SMOKE = process.env.LOOPYARD_SMOKE === '1';

let win = null;
let face = null;
let prereq = null; // first-run prerequisite check + fix runner (prereqs.mjs)
let joinInstallUrl = null; // the installer URL from the last pasted join line
let currentTarget = null; // 'connect' | 'web' — which page the window is showing
let yardCmd = null; // {cmd, pre, env} — the yard command the face runs (for `yard onboard --json`)
let guide = null; // the onboarding-guide controller (guide.mjs)
let guideView = null; // its docked WebContentsView (created on first open)
let guideAutoOpened = false;
let guideLayout = null; // the window 'resize' listener that keeps the guide docked
let stateHome = ''; // the app-data state root (LOOPYARD_HOME for a bundled run)
let updates = null; // the B-9 update checker (updateCheck.mjs)
let lastView = null; // the latest origin view (Update is disabled while it is live)

function showConnect() {
  if (!win || currentTarget === 'connect') return;
  currentTarget = 'connect';
  if (guide && guide.isOpen()) guide.hide();
  win.loadFile(CONNECT);
}

function showWeb(url) {
  if (!win || !url || (currentTarget === 'web' && dashboard === url)) return;
  dashboard = url;
  currentTarget = 'web';
  win.loadURL(url);
  // The web UI loads at once; the guide docks beside it the first time.
  if (guide && !guideAutoOpened) {
    guideAutoOpened = true;
    guide.autoOpen();
  }
}

// Move the web UI to a guide step's surface (a route from the engine payload, vetted by guide.mjs).
async function navigateWeb(route) {
  if (!win || win.isDestroyed() || !dashboard) return;
  const { webTarget } = await import('./guide.mjs');
  const t = webTarget({ hosted: dashboard, route, loaded: currentTarget === 'web' });
  currentTarget = 'web';
  if (t.kind === 'url') win.loadURL(t.url);
  else win.webContents.executeJavaScript(`location.hash = ${JSON.stringify(t.hash)}`).catch(() => {});
}

// Dock/undock the guide view on the right edge (bounds from guide.mjs).
async function setGuideOpen(open) {
  if (!win || win.isDestroyed()) return;
  const { panelBounds } = await import('./guide.mjs');
  if (open && !guideView) {
    guideView = new WebContentsView({
      webPreferences: { preload: GUIDE_PRELOAD, contextIsolation: true, nodeIntegration: false },
    });
    guideView.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
    guideView.webContents.loadFile(GUIDE);
  }
  if (!guideView) return;
  if (guideLayout) win.removeListener('resize', guideLayout);
  guideLayout = null;
  if (open) {
    guideLayout = () => {
      const [width, height] = win.getContentSize();
      const b = panelBounds({ width, height }, true);
      if (b) guideView.setBounds(b);
    };
    win.contentView.addChildView(guideView);
    guideLayout();
    win.on('resize', guideLayout);
  } else {
    win.contentView.removeChildView(guideView);
  }
}

// The onboarding guide: content from `yard onboard --json` (async — never
// delays the connect screen), re-read every GUIDE_REFRESH_MS while the panel is
// open so a step ticks when the engine reports it done; state (the dismiss) in
// userData/onboarding-guide.json.
let guideTimer = null;
async function buildGuide() {
  const { loadGuide, createGuideController, GUIDE_REFRESH_MS } = await import('./guide.mjs');
  const { makeRun } = await import('./prereqSeams.mjs');
  const load = () => loadGuide({
    run: makeRun({ env: yardCmd.env }), cmd: yardCmd.cmd, pre: yardCmd.pre, dataDir: DATA_DIR || undefined,
  });
  const content = await load();
  const file = path.join(app.getPath('userData'), 'onboarding-guide.json');
  const ctl = createGuideController({
    guide: content,
    load,
    readState: () => { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; } },
    writeState: (st) => { try { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, JSON.stringify(st)); } catch { /* best effort */ } },
    navigate: (route) => { navigateWeb(route); },
    setOpen: (open) => {
      setGuideOpen(open);
      clearInterval(guideTimer);
      guideTimer = open ? setInterval(() => { ctl.refresh(); }, GUIDE_REFRESH_MS) : null;
      if (open) ctl.refresh();
    },
    onChange: (v) => { if (guideView && !guideView.webContents.isDestroyed()) guideView.webContents.send('guide:view', v); },
  });
  return ctl;
}

// Help ▸ Onboarding guide — appended to the default menu (keeps Edit/copy-paste).
function addGuideMenu() {
  const menu = Menu.getApplicationMenu();
  if (!menu) return;
  menu.append(new MenuItem({ label: 'Loopyard', submenu: [
    { label: 'Stop Loopyard', click: () => { if (face) face.stopLoopyard(); } },
    { label: 'Check for updates', click: () => { if (updates) updates.refresh(); } },
    // Enabled only while an update exists and nothing is live (onBanner).
    { id: 'update', label: 'Update Loopyard…', enabled: false, click: () => { if (updates) updates.update(); } },
  ] }));
  menu.append(new MenuItem({
    label: 'Guide',
    submenu: [{
      label: 'Onboarding guide',
      accelerator: 'CmdOrCtrl+Shift+G',
      click: () => { if (guide && currentTarget === 'web') guide.reopen(); },
    }],
  }));
  Menu.setApplicationMenu(menu);
}

// React to every engine state change: unlock the web UI once genuinely live, drop
// back to the connect screen otherwise. Also forward the raw view to whatever page
// is loaded (the connect screen renders the pill from it).
function onView(view) {
  lastView = view;
  if (updates) updates.viewChanged();
  if (win && !win.isDestroyed()) {
    win.webContents.send('origin:view', view);
    if (view.live && view.dashboardUrl) showWeb(view.dashboardUrl);
    else showConnect();
  }
}

async function buildFace() {
  const { OriginAgentFace, resolveYardCommand, guiSpawnEnv, appStateHome } = await import('./originAgent.mjs');
  const { createYardSeams } = await import('./yardSeams.mjs');
  const home = os.homedir();
  const st = appStateHome({ platform: process.platform, home, appData: app.getPath('appData'), env: process.env, user: os.userInfo().username });
  const { ensureCodeCopy } = await import('./codeCopy.mjs');
  if (!process.env.LOOPYARD_YARD || !fs.existsSync(process.env.LOOPYARD_YARD)) ensureCodeCopy({ resourcesPath: process.resourcesPath, appState: st.home, version: app.getVersion() });
  const { cmd, pre, bundled } = resolveYardCommand({
    appState: st.home, version: app.getVersion(),
    env: process.env,
    home,
    resourcesPath: process.resourcesPath,
    exists: (p) => { try { fs.accessSync(p, fs.constants.X_OK); return true; } catch { return false; } },
  });
  // The app's OWN bundle is a read-only code tree (D12): its state goes to the OS
  // app-data dir. An installed/dev CLI keeps its own state root, so the app still
  // sees an origin that CLI started.
  const loopyardHome = bundled ? st.home : '';
  stateHome = loopyardHome || process.env.LOOPYARD_HOME || st.home;
  if (bundled && st.reason) console.log(`loopyard: ${st.reason}`);
  // Join = the SAME headless `yard origin up` (code on stdin); Disconnect = `yard
  // origin down`; the connected state = `yard origin status --json`.
  const env = guiSpawnEnv(process.env, home, { loopyardHome });
  yardCmd = { cmd, pre, env };
  const seams = createYardSeams({ cmd, pre, env });
  // Status is read asynchronously so a poll never blocks the window.
  return new OriginAgentFace({
    spawn: seams.spawn, run: seams.run, readStatus: seams.readStatusAsync,
    readLocalStatus: seams.readStatusAsync, mode: 'local',
    dataDir: DATA_DIR || undefined, onChange: onView,
  });
}

// The first-run prerequisite check (tmux, git, claude + sign-in, the yard
// bundle) and its one-click fixes. The renderer names a check id; the command
// a fix runs is chosen in prereqs.mjs, never taken from the page.
async function buildPrereq() {
  const { checkPrereqs, createPrereqController } = await import('./prereqs.mjs');
  const { isExecutable, makeRun, makeSpawnLines } = await import('./prereqSeams.mjs');
  const home = os.homedir();
  // Check the same prepared command and state root that Run will use. Never
  // execute prerequisite probes from the immutable in-app code tree.
  const env = { ...yardCmd.env };
  if (!yardCmd.pre.length) env.LOOPYARD_YARD = yardCmd.cmd;
  return createPrereqController({
    check: () => checkPrereqs({
      env, home, platform: process.platform, resourcesPath: process.resourcesPath,
      exists: isExecutable, run: makeRun({ env }), installUrl: joinInstallUrl,
    }),
    spawnLines: makeSpawnLines({ env }),
    openUrl: (url) => shell.openExternal(url),
    onProgress: (p) => { if (win && !win.isDestroyed()) win.webContents.send('prereq:progress', p); },
  });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 720,
    minHeight: 480,
    title: 'Loopyard',
    backgroundColor: '#0e0d0c', // matches the app's ink background — no white flash
    autoHideMenuBar: true,
    show: !SMOKE,
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      // B-8: app.getVersion() for the connect screen, readable by the preload at once.
      additionalArguments: [`--loopyard-version=${app.getVersion()}`],
    },
  });

  // External links open in the user's browser, not inside the shell.
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // First paint is always the connect screen; the face's poll promotes us to the
  // web UI if an agent (started here or by the CLI) is already live.
  currentTarget = 'connect';
  const load = win.loadFile(CONNECT);

  if (SMOKE) {
    load
      .then(() => {
        console.log(`SMOKE_LOADED target=${CONNECT} title=${win.webContents.getTitle()}`);
        console.log('SMOKE_OK');
        app.exit(0);
      })
      .catch((err) => {
        console.error('SMOKE_FAIL', err);
        app.exit(1);
      });
  }

  return win;
}

app.whenReady().then(() => {
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
  if (SMOKE) { createWindow(); return; }
  // Handlers first, then paint, then the engine: the connect screen shows at once
  // and nothing that spawns the yard CLI sits on the paint path (boot.mjs).
  import('./boot.mjs').then(({ bootApp }) => bootApp({
    handle: (ch, fn) => ipcMain.handle(ch, fn),
    createWindow,
    build: buildEngine,
    routes: {
      // IPC: the connect screen asks; the main process owns the engine.
      'origin:connect': (d, opts) => {
        try {
          return { ok: true, pid: d.face.start(opts || {}) };
        } catch (err) {
          return { ok: false, error: String(err && err.message ? err.message : err) };
        }
      },
      'origin:run': async (d) => {
        const checks = await d.prereq.check();
        if (!checks.ready) return { ok: false, error: 'Install the missing prerequisites before starting.', prerequisites: checks };
        try { return { ok: true, pid: d.face.runLocal() }; }
        catch (err) { return { ok: false, error: String(err.message || err) }; }
      },
      'origin:stop': (d) => d.face.stopLoopyard(),
      'origin:disconnect': (d) => d.face.stop(),
      'origin:get-view': (d) => d.face.view(),
      // A pasted join link (or the fallback terminal line) → its fields for the
      // form to show + confirm (the pair code itself is never sent back to the
      // page). Its installer URL, if any, feeds the "Install Loopyard" fix.
      'origin:parse-join': (d, text) => {
        try {
          const j = d.parseJoinInput(text);
          if (j.installUrl) joinInstallUrl = j.installUrl;
          return { ok: true, hub: j.hub, hubFingerprint: j.hubFingerprint, hasCode: !!j.pairCode, installUrl: j.installUrl };
        } catch (err) {
          return { ok: false, error: String(err && err.message ? err.message : err) };
        }
      },
      // Read the cert the Hub presents and compare it with the fingerprint the
      // user was given → {state: match|mismatch|unconfirmed|plaintext|unknown, seen}.
      'origin:check-hub': (d, q) => d.checkHub(q || {}),
      'prereq:check': (d) => d.prereq.check(),
      'prereq:fix': (d, id) => d.prereq.fix(String(id || '')),
      'prereq:send': (d, id, code) => d.prereq.send(String(id || ''), String(code || '')),
      'prereq:open-link': (d, id) => d.prereq.openLink(String(id || '')),
      // The onboarding guide panel: the page sends a step KEY; guide.mjs picks the route.
      'guide:get': async (d) => (await d.guideReady).view(),
      'guide:go': async (d, key) => (await d.guideReady).go(String(key || '')),
      'guide:hide': async (d) => (await d.guideReady).hide(),
      'guide:dismiss': async (d) => (await d.guideReady).dismiss(),
      // B-9: the update banner; update:open re-checks liveness in main.
      'update:get': () => (updates ? updates.banner() : { show: false, updateEnabled: false }),
      'update:open': () => (updates ? updates.update() : { ok: false, error: 'Update check not ready.' }),
    },
  })).catch((err) => console.error('loopyard: engine start failed', err));
});

// B-9: the portal's release.json vs app.getVersion(). The banner (connect
// screen) and the menu item both follow updateBanner(); Update is disabled
// while loops are live and only ever opens the portal release page.
function onBanner(banner) {
  const item = Menu.getApplicationMenu()?.getMenuItemById?.('update');
  if (item) item.enabled = !!banner.updateEnabled;
  if (win && !win.isDestroyed()) win.webContents.send('update:banner', banner);
}

async function buildUpdates() {
  const { checkForUpdate, createUpdateController, updateAuth, enrollPath } = await import('./updateCheck.mjs');
  const readEnroll = () => {
    const file = enrollPath({ stateHome, dataDir: DATA_DIR, hubStateDir: process.env.LOOPYARD_HUB_STATE_DIR });
    try { return file ? JSON.parse(fs.readFileSync(file, 'utf8')) : null; } catch { return null; }
  };
  // The manifest is gated: the hub's lyd_ device token (enroll.json) authorizes
  // it; net.fetch also carries the app session's portal cookie as a fallback.
  const fetchImpl = net && net.fetch ? (u, o) => net.fetch(u, { ...o, credentials: 'include' }) : globalThis.fetch;
  return createUpdateController({
    check: () => {
      const { portal, headers } = updateAuth({ env: process.env, enroll: readEnroll() });
      return checkForUpdate({ portal, current: app.getVersion(), fetchImpl, headers });
    },
    openUrl: (url) => shell.openExternal(url),
    getView: () => lastView,
    onChange: onBanner,
  });
}

// Everything that touches the yard CLI, built after the window has painted.
async function buildEngine() {
  face = await buildFace();
  const { parseJoinInput } = await import('./originAgent.mjs');
  const { checkHub } = await import('./hubPeek.mjs');
  prereq = await buildPrereq();
  // guide content loads in the background; its calls wait for it on their own
  const guideReady = buildGuide().then((g) => {
    guide = g;
    if (currentTarget === 'web' && !guideAutoOpened) { guideAutoOpened = true; guide.autoOpen(); }
    return g;
  });
  addGuideMenu();
  updates = await buildUpdates();
  updates.start();
  // Watch local status: a CLI-started (or prior-session) stack
  // that is already live promotes us straight to the web UI.
  face.watch();
  return { face, prereq, parseJoinInput, checkHub, guideReady };
}

// Quit reaps only an in-flight `origin up` child; a connected origin's detached
// daemon keeps serving (as after closing the terminal that ran the CLI).
// "Disconnect" (`yard origin down`) is what takes it offline.
app.on('before-quit', () => {
  if (face) {
    try { face.dispose(); } catch { /* already gone */ }
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
