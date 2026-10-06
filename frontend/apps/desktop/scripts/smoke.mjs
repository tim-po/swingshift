// Headless acceptance for the Electron shell — honest about the no-macOS build host.
// Proves, without packaging a .app:
//   1. main.js is syntactically valid (node --check).
//   2. the electron-builder mac config is present + well-formed (appId, mac target).
//   2d. the origin bundle hook (§1.9): dist:mac → stage-origin.mjs, mac.extraResources,
//      one repo-root dist/, the {x64, arm64} arch map, a real stage-origin run on
//      two fake tarballs, and resolveYardCommand over the R27 cases.
//   3. R18: no renderer/ is built, shipped, or loaded — the window loads the running
//      yard's http://127.0.0.1:<dash-port>/app/ in both modes.
//   4. if electron is installed, `electron .` launches under xvfb, loads the app
//      entry, and reports SMOKE_OK — else this step is SKIPPED, loudly, not faked.
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync,
} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const DIR = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(DIR, '..');
const fail = (m) => { console.error('SMOKE_FAIL ' + m); process.exit(1); };
const ok = (m) => console.log('  ✓ ' + m);

// 1. main.js parses.
try {
  execFileSync(process.execPath, ['--check', path.join(ROOT, 'main.js')]);
  ok('main.js: node --check clean');
} catch (e) {
  fail('main.js failed node --check: ' + e.message);
}

// 2. electron-builder mac config is well-formed.
const yml = path.join(ROOT, 'electron-builder.yml');
if (!existsSync(yml)) fail('electron-builder.yml missing');
const cfg = readFileSync(yml, 'utf8');
for (const need of ['appId:', 'mac:', 'target:', 'arch:']) {
  if (!cfg.includes(need)) fail(`electron-builder.yml missing "${need}"`);
}
ok('electron-builder.yml: appId + mac target + arch present');

// 2b. the origin-agent FACE wiring is present + parses (§6.3). The GUI is a thin
// face over the SAME `yard origin up` engine — prove its three files exist and are
// syntactically sound, so packaging never ships a broken connect screen.
for (const f of ['preload.js', 'originAgent.mjs', 'codeCopy.mjs', 'yardSeams.mjs', 'prereqs.mjs', 'prereqSeams.mjs', 'hubPeek.mjs', 'guide.mjs', 'boot.mjs', 'updateCheck.mjs', 'guidePreload.js']) {
  const p = path.join(ROOT, f);
  if (!existsSync(p)) fail(`${f} missing — the GUI origin-agent face is not wired`);
  try {
    execFileSync(process.execPath, ['--check', p]);
  } catch (e) {
    fail(`${f} failed node --check: ${e.message}`);
  }
}
ok('origin-agent face: preload.js + originAgent.mjs + prereqs.mjs present + node --check clean');

const connectHtml = path.join(ROOT, 'connect.html');
if (!existsSync(connectHtml)) fail('connect.html missing — no connect screen to join as an origin');
const connect = readFileSync(connectHtml, 'utf8');
for (const need of ['loopyardOrigin', 'loopyardSetup', 'Run on this Mac', 'Or join a Hub', 'Connect', 'Hub URL', 'Join link', 'checkHub', 'id="newer"']) {
  if (!connect.includes(need)) fail(`connect.html missing "${need}"`);
}
// main.js must actually wire the preload + connect screen + the shared verb.
const mainJs = readFileSync(path.join(ROOT, 'main.js'), 'utf8');
for (const need of ['preload', 'connect.html', 'origin:connect', 'origin:run', 'origin:stop', 'Stop Loopyard', 'OriginAgentFace', 'resourcesPath', 'appStateHome', 'prereq:check', 'prereq:fix', 'origin:check-hub']) {
  if (!mainJs.includes(need)) fail(`main.js does not wire "${need}" — GUI face incomplete`);
}
ok('first screen: connect.html (Run on this Mac + Join a Hub + R17 newer-version) wired into main.js via preload');

// 2c. the packaged app must SHIP the face files — electron-builder `files:` is an
// allow-list, so a file missing here is absent from the .app (G20).
for (const f of ['main.js', 'preload.js', 'originAgent.mjs', 'codeCopy.mjs', 'yardSeams.mjs', 'prereqs.mjs', 'prereqSeams.mjs', 'hubPeek.mjs', 'guide.mjs', 'boot.mjs', 'updateCheck.mjs', 'guidePreload.js', 'connect.html', 'guide.html']) {
  if (!new RegExp(`^\\s*-\\s*${f.replace('.', '\\.')}\\s*$`, 'm').test(cfg)) {
    fail(`electron-builder.yml files: does not ship ${f} — the .app would lack the join face`);
  }
}
ok('electron-builder files: ships main.js + preload.js + originAgent.mjs + codeCopy.mjs + yardSeams.mjs + prereqs.mjs + prereqSeams.mjs + hubPeek.mjs + guide.mjs + boot.mjs + updateCheck.mjs + guidePreload.js + connect.html + guide.html');

// 2d. the origin bundle rides inside the .app (PHASE-B-SPEC §1.9 item 1, B4(b)):
// dist:mac stages it, electron-builder copies it to Resources/loopyard, and both
// name the SAME repo-root dist/. Then stage-origin.mjs runs for real against two
// tiny fake tarballs, and resolveYardCommand is checked over the R27 cases.
{
  const pkg = JSON.parse(readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
  if (!/node scripts\/stage-origin\.mjs && electron-builder --mac/.test(pkg.scripts['dist:mac'] || '')) {
    fail('package.json dist:mac does not run scripts/stage-origin.mjs before electron-builder');
  }
  // the `mac:` block = from `mac:` to the next top-level key
  const mac = cfg.slice(cfg.search(/^mac:/m));
  const nl = mac.indexOf('\n');
  const next = mac.slice(nl + 1).search(/^[^\s#]/m);
  const macBlock = next < 0 ? mac : mac.slice(0, nl + 1 + next);
  const m = /^\s+extraResources:\s*\n\s+-\s*from:\s*(\S+)\s*\n\s+to:\s*(\S+)\s*$/m.exec(macBlock);
  if (!m) fail('electron-builder.yml mac.extraResources {from, to} missing — the .app would not ship the origin');
  const [, from, to] = m;
  if (to !== 'loopyard') fail(`mac.extraResources to: "${to}" (want loopyard → Resources/loopyard/bin/yard)`);
  if (!from.endsWith('/origin-mac-${arch}')) fail(`mac.extraResources from: "${from}" does not end in /origin-mac-\${arch}`);
  const so = await import(pathToFileURL(path.join(DIR, 'stage-origin.mjs')).href);
  const ymlDist = path.resolve(ROOT, from.slice(0, -'/origin-mac-${arch}'.length));
  if (ymlDist !== so.DIST) fail(`dist mismatch: electron-builder from → ${ymlDist}, stage-origin DIST → ${so.DIST}`);
  if (so.DIST !== path.resolve(ROOT, '../../../dist')) fail(`stage-origin DIST ${so.DIST} is not the repo-root dist/`);
  if (JSON.stringify(so.ARCH_MAP) !== JSON.stringify({ x64: 'x86_64', arm64: 'arm64' })) {
    fail(`stage-origin ARCH_MAP ${JSON.stringify(so.ARCH_MAP)} != {x64: x86_64, arm64: arm64}`);
  }
  const archs = /arch:\s*\[([^\]]*)\]/.exec(macBlock);
  const want = archs ? archs[1].split(',').map((s) => s.trim()).sort() : [];
  if (JSON.stringify(want) !== JSON.stringify(Object.keys(so.ARCH_MAP).sort())) {
    fail(`mac target arch [${want}] not covered exactly by ARCH_MAP keys [${Object.keys(so.ARCH_MAP)}]`);
  }
  ok(`extraResources: ${from} → Resources/${to}; one dist/ (${so.DIST}); arch map {x64: x86_64, arm64: arm64}`);

  // stage-origin against two tiny fake tarballs (loopyard/bin/yard + BUNDLE.json).
  const tmp = mkdtempSync(path.join(os.tmpdir(), 'stage-origin-smoke-'));
  try {
    const dist = path.join(tmp, 'dist');
    mkdirSync(dist);
    for (const arch of Object.keys(so.ARCH_MAP)) {
      const src = path.join(tmp, `src-${arch}`, 'loopyard');
      mkdirSync(path.join(src, 'bin'), { recursive: true });
      writeFileSync(path.join(src, 'bin', 'yard'), '#!/bin/sh\necho fake yard\n', { mode: 0o755 });
      writeFileSync(path.join(src, 'BUNDLE.json'), JSON.stringify({ target: so.slugFor(arch) }));
      const tgz = path.join(dist, so.tarballName(arch));
      execFileSync('tar', ['-czf', tgz, '-C', path.dirname(src), 'loopyard']);
      const hex = createHash('sha256').update(readFileSync(tgz)).digest('hex');
      writeFileSync(`${tgz}.sha256`, `${hex}  ${path.basename(tgz)}\n`);
    }
    so.stageOrigin({ dist });
    for (const arch of Object.keys(so.ARCH_MAP)) {
      const out = path.join(dist, `origin-mac-${arch}`);
      const yard = path.join(out, 'bin', 'yard');
      if (!existsSync(yard)) fail(`stage-origin: ${yard} missing`);
      if (!(statSync(yard).mode & 0o111)) fail(`stage-origin: ${yard} lost its exec bit`);
      if (existsSync(path.join(out, 'loopyard'))) fail(`stage-origin: ${out}/loopyard exists — top dir not stripped`);
      const b = JSON.parse(readFileSync(path.join(out, 'BUNDLE.json'), 'utf8'));
      if (b.target !== `macos-${so.ARCH_MAP[arch]}`) fail(`stage-origin: ${out} holds ${b.target}, not macos-${so.ARCH_MAP[arch]}`);
    }
    // a tampered .sha256 must fail the build, never stage
    const bad = path.join(dist, `${so.tarballName('x64')}.sha256`);
    writeFileSync(bad, `${'0'.repeat(64)}  x\n`);
    let threw = '';
    try { so.stageOrigin({ dist, arches: ['x64'] }); } catch (e) { threw = e.message; }
    if (!/sha256 [0-9a-f]{64} != 0{64}/.test(threw)) fail(`stage-origin accepted a tampered .sha256 (${threw || 'no error'})`);
    rmSync(path.join(dist, so.tarballName('arm64')));
    threw = '';
    try { so.stageOrigin({ dist, arches: ['arm64'] }); } catch (e) { threw = e.message; }
    if (!threw.includes('origin_bundle build --target macos-arm64')) fail(`missing tarball error does not name the build command (${threw})`);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
  ok('stage-origin: 2 fake tarballs → dist/origin-mac-{x64,arm64}/bin/yard (exec, no loopyard/ level); bad sha + missing tarball refused');

  // R27 over the three spec cases (+ the bundled default is set directly).
  const { resolveYardCommand } = await import(pathToFileURL(path.join(ROOT, 'originAgent.mjs')).href);
  const RES = '/Applications/My Apps/Loopyard.app/Contents/Resources';
  const spaced = '/Users/Tim Po/loopyard/bin/yard';
  const r1 = resolveYardCommand({ env: { LOOPYARD_YARD: spaced }, resourcesPath: RES, exists: (p) => p === spaced });
  const r2 = resolveYardCommand({ env: {}, resourcesPath: RES, exists: (p) => p === `${RES}/loopyard/bin/yard` });
  const r3 = resolveYardCommand({ env: { LOOPYARD_YARD: '/v/bin/python -m mcp_loops.yard' }, exists: (p) => p === '/v/bin/python' });
  if (r1.cmd !== spaced || r1.pre.length) fail(`R27 (i) path with a space was split: ${JSON.stringify(r1)}`);
  if (r2.cmd !== `${RES}/loopyard/bin/yard` || !r2.bundled) fail(`R27 (ii) bundled default not used: ${JSON.stringify(r2)}`);
  if (r3.cmd !== '/v/bin/python' || r3.pre.join(' ') !== '-m mcp_loops.yard') fail(`R27 (iii) dev string not split: ${JSON.stringify(r3)}`);
  ok('R27 yardCommand: spaced file path unsplit; bundled default direct; dev string split');
}

// 3. R18: the app never builds, ships or loads its own renderer/ copy of the web UI
// (a hash-routed file:// build blanked the window). Both modes load the running
// yard's dashboard over HTTP; the port comes from `yard status --json`.
{
  const pkg = JSON.parse(readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
  for (const [name, cmd] of Object.entries(pkg.scripts || {})) {
    if (/build:web|renderer/.test(name + ' ' + cmd)) fail(`package.json script "${name}" still builds the renderer: ${cmd}`);
  }
  if (/^\s*-\s*renderer\b/m.test(cfg)) fail('electron-builder.yml files: still ships renderer/');
  if (/loadFile\([^)]*renderer|renderer[\/\\]index\.html/.test(mainJs)) fail('main.js still loads renderer/index.html');
  if (!/win\.loadURL\(/.test(mainJs)) fail('main.js never loadURLs the dashboard');
  const { dashboardUrl } = await import(pathToFileURL(path.join(ROOT, 'originAgent.mjs')).href);
  const u = dashboardUrl({ live: true, dashPort: 18811 });
  if (u !== 'http://127.0.0.1:18811/app/') fail(`dashboard URL from yard status is ${u}, want http://127.0.0.1:18811/app/`);
  if (dashboardUrl({ live: false, dashPort: 18811 })) fail('a not-live yard status still yields a dashboard URL');
  ok('R18: no renderer/ built, shipped or loaded; window loads http://127.0.0.1:<dash-port>/app/');
}

// 4. Real headless launch — only if electron is installed.
let electronBin = null;
try {
  electronBin = execFileSync(process.execPath, ['-e', "process.stdout.write(require('electron'))"],
    { cwd: ROOT, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
} catch { /* electron not installed / not downloaded on this host */ }

if (electronBin && existsSync(electronBin)) {
  const useXvfb = spawnSync('which', ['xvfb-run']).status === 0;
  // --no-sandbox: a rootless CI/container can't give chrome-sandbox the root:4755
  // it demands; the flag is standard for headless CI and a no-op on a real desktop.
  const argv = ['--no-sandbox', '.'];
  const [cmd, pre] = useXvfb ? ['xvfb-run', ['-a', electronBin]] : [electronBin, []];
  const r = spawnSync(cmd, [...pre, ...argv], {
    cwd: ROOT,
    env: { ...process.env, LOOPYARD_SMOKE: '1' },
    timeout: 60000,
    encoding: 'utf8',
  });
  const out = (r.stdout || '') + (r.stderr || '');
  if (out.includes('SMOKE_OK')) {
    ok('electron launch: window loaded the app entry headlessly' + (useXvfb ? ' (xvfb)' : ''));
  } else if (/cannot open shared object file|error while loading shared libraries|libgtk-3|libgbm|libnss3/.test(out)) {
    // Electron is present but the host lacks the GUI system libraries (libgtk-3, …)
    // — a build-host limitation, NOT a project defect. The spec explicitly permits
    // the node-check path here; report a LOUD skip, never a fake pass or a false fail.
    console.log('  ⚠ electron present but host lacks GUI libs (libgtk-3, …) — launch step SKIPPED (owner runs the real .app on macOS).');
  } else {
    fail('electron launched but did not report SMOKE_OK:\n' + out.slice(-800));
  }
} else {
  console.log('  ⚠ electron not installed on this host — launch step SKIPPED (owner runs it on macOS).');
}

console.log('SMOKE_OK');
