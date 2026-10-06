// Loop City — the owner's 3D city prototype, ported as-is (dense one-line style kept on purpose).
// mountCity(root, SNAP, host) draws the city for one snapshot (see snapshot.ts for its shape) into
// `root`, which already holds CITY_MARKUP, and returns { setTheme, look, dispose }.
import * as THREE from 'three';
import { MapControls } from 'three/addons/controls/MapControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';


export function mountCity(root, SNAP, host) {
const byId = id => root.querySelector('#' + id);
const offs = [], onWin = (type, fn, opts) => { addEventListener(type, fn, opts); offs.push(() => removeEventListener(type, fn, opts)); };
let disposed = false;
const DATA = SNAP.loops, PLANS = SNAP.plans || [], LINKS = SNAP.links || [], NOW = SNAP.now;

// ─── plans and issues are city objects too (kept across rebuilds) ───
const PLANOBJ = PLANS.filter(p => !p.loops.length).map(p => ({ plan: true, n: p.t, id: p.id, p: p.p, st: p.up, stage: 'idea', team: null, budget: 24, goal: '' }));
const ISSUES = DATA.flatMap(d => (d.iss || []).map(i => ({ issue: true, id: i[0], n: i[1], status: i[2], kind: i[3], loop: d, p: d.p })));

// ─── the same rules as the canvas prototype ───
const stateOf = d => (d.s === 'waiting_owner' || d.q) ? 'need' : (d.s === 'running' || d.s === 'stopping') ? 'run' : d.s === 'error' ? 'fail' : d.s === 'stopped' ? 'stop' : d.s === 'saved' ? 'draft' : 'done';
const isStale = d => (d.s === 'running') && d.act && (NOW - d.act) > 1200;
const grade = sc => sc == null ? null : sc >= 70 ? 'good' : sc >= 40 ? 'ok' : 'bad';
// ─── what a loop made: the kind of files it delivered (git), or if it delivered nothing, produced (output folder).
// More than half of one kind decides; otherwise it's a mix; no files at all = talk ───
const FILEKINDS = { front: 'html htm css scss sass less tsx jsx vue svelte', back: 'py go rs java rb php sh sql c cpp', text: 'md txt docx rst', visual: 'png jpg jpeg svg gif webp fig pptx key pdf', data: 'json csv xlsx yml yaml toml jsonl' };
const EXT_KIND = {}; Object.entries(FILEKINDS).forEach(([k, v]) => v.split(' ').forEach(e => { EXT_KIND[e] = k; }));
const EITHER = ['ts', 'js', 'mjs', 'cjs']; // used on both sides: they join whichever side leads (frontend when alone)
const KIND_STYLE = { front: 'glass', back: 'stone', visual: 'signs', text: 'brown', data: 'store', mixed: 'standard', talk: 'deco' };
const KIND_NAME = { front: 'Frontend', back: 'Backend', visual: 'Visual', text: 'Text', data: 'Data', mixed: 'Mixed', talk: 'Talk' };
function kindOf(d) { if (d._kind) return d._kind;
  const c = {}; let amb = 0;
  for (const [x, n] of Object.entries((d.fx && d.fx.e) || {})) { if (EITHER.includes(x)) amb += n; else if (EXT_KIND[x]) c[EXT_KIND[x]] = (c[EXT_KIND[x]] || 0) + n; }
  if (amb) { const side = (c.back || 0) > (c.front || 0) ? 'back' : 'front'; c[side] = (c[side] || 0) + amb; }
  const tot = Object.values(c).reduce((a, b) => a + b, 0), ranked = Object.entries(c).sort((a, b) => b[1] - a[1]);
  const k = !tot ? 'talk' : ranked[0][1] / tot > 0.5 ? ranked[0][0] : 'mixed';
  return (d._kind = { k, tot, share: ranked.map(([a, b]) => [a, Math.round(b * 100 / tot)]), src: tot ? d.fx.src : 'none' }); }
// ─── how it went: one colour code on every building — green clean, blue good, amber mixed, red bad, orange waiting on you ───
const OUTC = { great: 0x6f9f68, good: 0x5b82b0, mixed: 0xc39a45, bad: 0xa9503e, wait: 0xcf7d3c }; // paint, not light: sage, slate blue, ochre, brick red, burnt orange
const OUTWORD = { great: 'clean', good: 'good', mixed: 'mixed', bad: 'bad', wait: 'waiting on you' };
const outcomeOf = d => stateOf(d) === 'fail' ? 'bad' : d.sc != null ? (d.sc >= 85 ? 'great' : d.sc >= 70 ? 'good' : d.sc >= 40 ? 'mixed' : 'bad') : (stateOf(d) === 'done' && d.ship === true ? 'good' : null);
const REWORK = new Set(['needs_work', 'error', 'failed', 'crashed', 'timeout', 'give_up']);
function verdictOf(d, id) { // an agent's floor: how its reports ended — clean, done after rework, still more to do, failed, or waiting
  const vs = (d.ev || []).filter(e => (e[1] === id || (e[1] || '').split('+').includes(id)) && e[2] !== 'parallel' && e[2] !== 'renudge').map(e => e[2]);
  if (!vs.length) return null; const last = punchOf(vs[vs.length - 1]);
  return last === 'fail' ? 'bad' : last === 'wait' ? 'wait' : last === 'more' ? 'mixed' : vs.some(v => REWORK.has(v)) ? 'good' : 'great'; }
const seriesOf = n => n.replace(/[-_](v?\d+|[a-z]?\d+)$/, '').split('-')[0];
const levelsOf = d => { let lv = (d.lv || []).map(l => l.map(x => ({ id: x[0], kind: x[1] }))); if (!lv.length) lv = d.w.map(id => [{ id, kind: 'worker' }]).concat(d.i.map(id => [{ id, kind: 'input' }])); if (!lv.length && d.m) lv = [[{ id: d.m, kind: 'solo' }]]; return lv; };
const hasCap = (d, lv) => !!d.m && !(lv.length === 1 && lv[0][0].kind === 'solo');
const progressOf = (d, total) => { const st = stateOf(d); if (st === 'done') return total; if (st === 'draft') return 0; const f = Math.round((d.l ? d.u / d.l : 0) * total); return (st === 'run' || st === 'need' || st === 'fail') ? Math.max(1, Math.min(total, f || 1)) : f; };
const turnsOf = (d, id) => { const p = d.pa && d.pa[id]; return p ? p[0] : null; };
const ROWH = 0.2, BASE_H = 0.62;
const hFor = (d, id) => { const t = turnsOf(d, id); return (t == null ? 2 : Math.min(16, Math.max(1, t))) * ROWH + 0.06; };
const lotSize = d => { if (d.plan) return d.stage === 'idea' || d.budget <= 12 ? [1, 1] : d.budget <= 30 ? [2, 1] : [2, 2]; const u = d.u || 0; return u <= 4 ? [1, 1] : u <= 10 ? [2, 1] : u <= 24 ? [2, 2] : [4, 2]; };
const UX = 2.7, UY = 2.7, GX = 0.35, BLOCK_W = 4, BLOCK_D = 2, PAD = 0.32, ST = 1.3, AV = 2.2;
const BW = BLOCK_W * UX - GX + PAD * 2, BD = BLOCK_D * UY - GX + PAD * 2; // a city block: 4×2 lot cells with a sidewalk rim, streets on all four sides
const HUES = [0x7aa2d6, 0xd69a7a, 0x8fc28a, 0xc9a0d6, 0xd6c27a, 0x7ac9c2, 0xd67a9a, 0xa7b0c0, 0xb8d67a, 0x9a8ad6];

// ─── grouping: the city is split into neighbourhoods; how you split it is a choice (plan, kind of work, time, status) ───
function byTime(loops) {
  const ts = loops.map(d => d.st || NOW), span = Math.max(...ts) - Math.min(...ts), weekly = span < 75 * 86400, g = new Map();
  loops.forEach(d => { const t = new Date((d.st || NOW) * 1000); let k, name;
    if (weekly) { const mon = new Date(t.getFullYear(), t.getMonth(), t.getDate() - ((t.getDay() + 6) % 7)); k = +mon; name = 'Week of ' + mon.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); }
    else { k = t.getFullYear() * 12 + t.getMonth(); name = t.toLocaleDateString(undefined, { month: 'short', year: 'numeric' }); }
    if (!g.has(k)) g.set(k, { name, k, loops: [] }); g.get(k).loops.push(d); });
  return [...g.values()].sort((a, b) => a.k - b.k);
}
function byStatus(loops) {
  const ORDER = [['need', 'Needs you'], ['run', 'Running'], ['fail', 'Failed'], ['stop', 'Stopped'], ['ship', 'Shipped'], ['done', 'Done'], ['draft', 'Drafts']], g = {};
  loops.forEach(d => { let k = isStale(d) ? 'need' : stateOf(d); if (k === 'done' && d.ship === true) k = 'ship'; (g[k] = g[k] || []).push(d); });
  return ORDER.filter(([k]) => g[k]).map(([k, name]) => ({ name, loops: g[k] }));
}
const KINDS = [['Reviews', /review|audit|critic|adversarial|usability|openreview/], ['Design & brand', /brand|design|marketing|voice|identity|visual|ux|city|rename|human|de-ai/], ['Release & ops', /release|deploy|ship|pipeline|ci-|smoke|merge|installer/], ['Research & docs', /exp-|research|feedback|direction|spec|doc|backlog|pilot|mechanism|position/]];
function byKind(loops) { // what the loop was for, read from its name
  const g = {}; loops.forEach(d => { const k = (KINDS.find(([, re]) => re.test(d.n)) || ['Builds'])[0]; (g[k] = g[k] || []).push(d); });
  return ['Builds'].concat(KINDS.map(k => k[0])).filter(k => g[k]).map(name => ({ name, loops: g[name] }));
}
// a neighbourhood per PLAN: the loops started from it or moved into it (a fact, not a guess); loops in no plan last
function byPlan(loops) {
  const g = new Map(), loose = [];
  loops.forEach(d => { if (!d.pl) { loose.push(d); return; } if (!g.has(d.pl)) g.set(d.pl, { name: d.pl, loops: [] }); g.get(d.pl).loops.push(d); });
  const out = [...g.values()]; out.forEach(x => x.t0 = Math.min(...x.loops.map(d => d.st || NOW))); out.sort((a, b) => a.t0 - b.t0);
  if (loose.length) out.push({ name: 'Not in a plan', loops: loose });
  return out;
}
const GROUPERS = { work: byPlan, kind: byKind, time: byTime, status: byStatus };

// ─── lots into blocks: oldest first, look-ahead of 8 so small lots fill the gaps ───
function packLots(loops, nSlots) {
  const pending = loops.map(d => { const s = lotSize(d); return { d, w: s[0], h: s[1] }; }).sort((a, b) => (a.d.st || 0) - (b.d.st || 0)), slots = [];
  for (let i = 0; i < nSlots; i++) {
    const occ = [[0, 0, 0, 0], [0, 0, 0, 0]], lots = [];
    for (;;) { let placed = false;
      for (let k = 0; k < Math.min(8, pending.length) && !placed; k++) { const it = pending[k];
        for (let r = 0; r + it.h <= BLOCK_D && !placed; r++) for (let c = 0; c + it.w <= BLOCK_W && !placed; c++) {
          let ok = true; for (let a = r; a < r + it.h && ok; a++) for (let b = c; b < c + it.w; b++) if (occ[a][b]) { ok = false; break; }
          if (!ok) continue; for (let a = r; a < r + it.h; a++) for (let b = c; b < c + it.w; b++) occ[a][b] = 1;
          lots.push({ d: it.d, w: it.w, h: it.h, c, r }); pending.splice(k, 1); placed = true;
        } }
      if (!placed) break; }
    slots.push({ occ, lots });
  }
  return pending.length ? null : slots;
}
function shapeFor(nb, R) { // cols × rows closest to square, fewest empty (park) blocks; R fixes the row count
  let best = null; for (let r = R || 1; r <= (R || nb); r++) { const c = Math.ceil(nb / r), s = (c * r - nb) + Math.abs(Math.log((c * BW) / (r * BD))) * 1.5; if (!best || s < best.s) best = { c, r, s }; } return best;
}
// ─── the city: neighbourhoods on shelves, one continuous street grid; avenues (with a planted median) between neighbourhoods ───
function layout(loops, mode) {
  const plans = loops.filter(d => d.plan), issues = loops.filter(d => d.issue), real = loops.filter(d => !d.plan && !d.issue);
  let groups = real.length ? GROUPERS[mode](real) : []; const spare = [];
  // a plan no loop has started yet waits in the Planned neighbourhood (or beside the loop that filed its issue)
  plans.forEach(pl => { const g = pl.home ? groups.find(gr => gr.loops.includes(pl.home)) : null;
    if (g) g.loops.push(pl); else spare.push(pl); });
  if (spare.length) groups.push({ name: 'Planned', loops: spare, plan: true });
  if (issues.length) groups.push({ name: 'Recycling', loops: [], landfill: true, issues });
  groups.forEach((g, i) => { if (g.landfill) { g.hue = 0x7fb07a; g.slots = [{ occ: [[1, 1, 1, 1], [1, 1, 1, 1]], lots: [], landfill: true }]; g.nb = 1; return; }
    g.hue = g.plan ? 0x7fa8e8 : HUES[i % HUES.length]; let nb = Math.max(1, Math.ceil(g.loops.reduce((a, d) => { const s = lotSize(d); return a + s[0] * s[1]; }, 0) / 8)), slots;
    while (!(slots = packLots(g.loops, nb))) nb++; g.slots = slots.filter(s => s.lots.length); g.nb = g.slots.length; });
  // big neighbourhoods first (they set each shelf's depth), small ones stack into columns; loose ends and plans go at the edge
  const order = groups.filter(g => !g.plan && !g.landfill && g.name !== 'Loose ends').sort((a, b) => b.nb - a.nb || (a.t0 || 0) - (b.t0 || 0)).concat(groups.filter(g => g.plan || g.landfill || g.name === 'Loose ends'));
  let best = null; // try a few city widths, keep the most compact, squarest one
  for (const k of [0.9, 1.1, 1.3, 1.5, 1.8, 2.2]) { const r = placeCity(order, Math.max(3 * (BW + ST), Math.sqrt(groups.reduce((a, g) => a + g.nb, 0) * (BW + ST) * (BD + ST)) * k)), s = r.WW * r.DD * (1 + 0.6 * Math.abs(Math.log(r.WW / r.DD)));
    if (!best || s < best.s) best = Object.assign(r, { s }); }
  return Object.assign(best, { groups });
}
function placeCity(groups, maxW) {
  const shelves = []; let cur = null;
  const unitW = c => c * BW + (c - 1) * ST;
  const tryPlace = (sh, g) => {
    const last = sh.units[sh.units.length - 1];
    if (g.nb < sh.R && last && last.stack && last.fill + g.nb <= sh.R) { last.groups.push(g); last.fill += g.nb; return true; }
    const u = g.nb < sh.R ? { stack: true, groups: [g], fill: g.nb, c: 1 } : { groups: [g], c: shapeFor(g.nb, sh.R).c };
    if (sh.units.length && sh.w + unitW(u.c) + AV > maxW) return false;
    sh.units.push(u); sh.w += unitW(u.c) + AV; return true;
  };
  for (const g of groups) { if (!cur || !tryPlace(cur, g)) { cur = { R: shapeFor(g.nb).r, units: [], w: AV }; shelves.push(cur); tryPlace(cur, g); } }
  const blocks = [], items = [], streets = [], signs = []; let y = 0, prevW = 0, WW = 0;
  const cellAt = (g, slot, x, yy) => { const b = { x, y: yy, g, slot }; blocks.push(b); if (slot) slot.lots.forEach(l => items.push({ d: l.d, w: l.w, h: l.h, x: x + PAD + l.c * UX, y: yy + PAD + l.r * UY, g })); return b; };
  shelves.forEach(sh => {
    const R = sh.R, top = y + AV, D = AV + R * BD + (R - 1) * ST, rowY = r => top + r * (BD + ST);
    streets.push({ dir: 'h', av: true, x: 0, y, w: Math.max(prevW, sh.w), d: AV });
    for (let r = 1; r < R; r++) streets.push({ dir: 'h', av: false, x: 0, y: rowY(r) - ST, w: sh.w, d: ST });
    let x = AV; streets.push({ dir: 'v', av: true, x: 0, y, w: AV, d: D + AV });
    sh.units.forEach(u => {
      if (u.stack) { let r = 0; u.groups.forEach(g => { g.slots.forEach((s, i) => { const b = cellAt(g, s, x, rowY(r + i)); if (!i) signs.push({ g, x: b.x, y: b.y }); }); r += g.nb; }); for (; r < R; r++) cellAt(null, null, x, rowY(r)); }
      else { const g = u.groups[0], cells = []; for (let r = 0; r < R; r++) for (let c = 0; c < u.c; c++) cells.push({ c, r });
        cells.sort((a, b) => Math.hypot((a.c + 0.5 - u.c / 2) * BW, (a.r + 0.5 - R / 2) * BD) - Math.hypot((b.c + 0.5 - u.c / 2) * BW, (b.r + 0.5 - R / 2) * BD)); // oldest in the middle
        cells.forEach((cl, i) => cellAt(g.slots[i] ? g : null, g.slots[i] || null, x + cl.c * (BW + ST), rowY(cl.r)));
        for (let c = 1; c < u.c; c++) streets.push({ dir: 'v', av: false, x: x + c * (BW + ST) - ST, y, w: ST, d: D + AV });
        signs.push({ g, x, y: top }); }
      x += unitW(u.c); streets.push({ dir: 'v', av: true, x, y, w: AV, d: D + AV }); x += AV;
    });
    y += D; prevW = sh.w; WW = Math.max(WW, sh.w);
  });
  streets.push({ dir: 'h', av: true, x: 0, y, w: prevW, d: AV });
  return { items, blocks, streets, signs, WW, DD: y + AV };
}

// ─── three.js scene ───
const stage = byId('stage');
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(2, devicePixelRatio)); renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
stage.appendChild(renderer.domElement);
const labels = new CSS2DRenderer(); labels.domElement.style.position = 'absolute'; labels.domElement.style.inset = '0'; labels.domElement.style.pointerEvents = 'none'; labels.domElement.style.zIndex = '1'; stage.appendChild(labels.domElement);
const scene = new THREE.Scene();
const camera = new THREE.OrthographicCamera(-10, 10, 10, -10, -500, 1000);
// map-style controls: left-drag grabs the ground, right-drag / Q·E rotates, scroll zooms toward the cursor
const controls = new MapControls(camera, renderer.domElement);
controls.enableDamping = true; controls.dampingFactor = 0.14; controls.screenSpacePanning = false; controls.zoomToCursor = true; controls.zoomSpeed = 2.6;
controls.minZoom = 0.4; controls.maxZoom = 10; controls.minPolarAngle = 0.25; controls.maxPolarAngle = 1.25; controls.panSpeed = 1.0;
const hemi = new THREE.HemisphereLight(0xffffff, 0x555555, 1.0); scene.add(hemi);
const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.castShadow = true; sun.shadow.mapSize.set(2048, 2048); sun.shadow.bias = -0.0004; scene.add(sun); scene.add(sun.target);
const composer = new EffectComposer(renderer); composer.addPass(new RenderPass(scene, camera));
const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.6, 0.5, 0.82); composer.addPass(bloom); composer.addPass(new OutputPass());

let theme = host.theme();
const PAL = {
  dark: { bg: 0x2f3033, ground: 0x3a3b3f, asphalt: 0x26272a, plate: 0x3f4044, walk: 0x55565b, lot: 0x4a4b50, lane: 0xd9d4c7, avenue: 0xc9a64a, done: 0xb4b3b0, run: 0x82abff, need: 0xff9a52, fail: 0xf06a5a, stop: 0x7a7b7f, draft: 0x55565b, lit: 0xffcf73, dark: 0x2a2b2e, lamp: 0xffe2a8, pole: 0x6d6e72, sun: 0.55, hemi: 0.55, sign: '#e8e6e0' },
  light: { bg: 0xeae6dc, ground: 0xd8d2c4, asphalt: 0x5e5a53, plate: 0xddd7ca, walk: 0xd0c9b8, lot: 0xd4cdbd, lane: 0xf7f3ea, avenue: 0xf2c14e, done: 0xcbc6ba, run: 0x5f86e0, need: 0xe8590c, fail: 0xc23a2b, stop: 0x9a958b, draft: 0xd8d2c5, lit: 0xf6e6bd, dark: 0x6b665c, lamp: 0xfff4d6, pole: 0x55524c, sun: 1.9, hemi: 1.0, sign: '#1a1814' },
};
// ─── architecture follows what a loop made (KIND_STYLE): one style per kind of work. Colour is kept for how it went (OUTC) ───
const SKINS = {
  standard: { name: 'Mixed', look: 'plain building' },
  deco: { name: 'Talk', look: 'Art Deco hall', bpal: { light: { done: 0xe3d6b8, plate: 0xe3d6b8 }, dark: { done: 0xb9ad90, plate: 0x4a4436 } } },
  glass: { name: 'Frontend', look: 'glass offices', bpal: { light: { done: 0x8fa9b4, lit: 0xcfe3ec, dark: 0x6f8797 }, dark: { done: 0x3d5560, lit: 0xd6f0fa, dark: 0x1f3a48 } } },
  stone: { name: 'Backend', look: 'concrete-grid offices', bpal: { light: { done: 0xd8d1c2, lit: 0xf3e3b8, dark: 0x2f3945 }, dark: { done: 0x8d877b, dark: 0x1b222a } } },
  signs: { name: 'Visual', look: 'signs and screens', bpal: { light: { done: 0x55585f }, dark: { done: 0x33353b } } },
  brown: { name: 'Text', look: 'brownstone', bpal: { light: { done: 0x8a5a42 }, dark: { done: 0x5a3a2c } } },
  store: { name: 'Data', look: 'storehouse', bpal: { light: { done: 0xd9d6cf }, dark: { done: 0x8a877f } } },
};
let archMode = (() => { try { return localStorage.getItem('city3d.arch') === 'plain' ? 'plain' : 'kind'; } catch (e) { return 'kind'; } })();
const skin = 'standard';
const SK = () => SKINS.standard; // the street, cars and parks are drawn the same everywhere; only buildings change style
const styleOf = d => archMode === 'plain' ? SKINS.standard : SKINS[KIND_STYLE[kindOf(d).k]];
Object.keys(PAL).forEach(k => PAL['base_' + k] = PAL[k]);
function refreshPal() { ['light', 'dark'].forEach(k => PAL[k] = Object.assign({}, PAL['base_' + k], (SK().pal || {})[k] || {})); }
refreshPal();
const TOONGRAD = (() => { const d = new Uint8Array([90, 170, 255]), tx = new THREE.DataTexture(d, 3, 1, THREE.RedFormat); tx.minFilter = tx.magFilter = THREE.NearestFilter; tx.needsUpdate = true; return tx; })();
const RBOX = new RoundedBoxGeometry(1, 1, 1, 2, 0.12);
const matCache = {};
const mat = (hex, opts = {}) => { const k = skin + hex + JSON.stringify(opts); if (matCache[k]) return matCache[k];
  const s = SK(); let m;
  if (s.lines) m = new THREE.MeshLambertMaterial({ color: new THREE.Color(hex).lerp(new THREE.Color(theme === 'dark' ? 0x133256 : 0xf4ecd6), hex === PAL[theme].need ? 0.1 : 0.6), transparent: !!opts.transparent, opacity: opts.opacity ?? 1, side: opts.side ?? THREE.FrontSide }); // flat washes; the ink lines carry the form
  else if (s.toon) m = new THREE.MeshToonMaterial({ color: hex, gradientMap: TOONGRAD, transparent: !!opts.transparent, opacity: opts.opacity ?? 1 });
  else if (s.rounded) m = new THREE.MeshStandardMaterial(Object.assign({ color: hex, roughness: 0.42, metalness: 0 }, opts, { metalness: 0 }));
  else m = new THREE.MeshStandardMaterial(Object.assign({ color: hex, roughness: 0.85, metalness: 0.05 }, opts));
  return (matCache[k] = m); };
const inkMat = () => matCache['ink' + skin + theme] || (matCache['ink' + skin + theme] = new THREE.LineBasicMaterial({ color: (SK().ink || {})[theme] ?? 0x000000, transparent: true, opacity: theme === 'dark' ? 0.9 : 0.8 }));
const BOX = new THREE.BoxGeometry(1, 1, 1), PLANE = new THREE.PlaneGeometry(1, 1), EDGES = new THREE.EdgesGeometry(BOX);
const WINMAT = new THREE.MeshBasicMaterial({ toneMapped: false }), TREE = new THREE.SphereGeometry(0.5, 8, 6);
const HIT = new THREE.MeshBasicMaterial({ visible: false }), FRAMEMAT = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.55, metalness: 0.35 });
const STEEL = 0xc0643f, DECK = 0x8e8a84;
function frameBeams(list, x, y, z, w, d, h) { // an unbuilt floor: primer-red steel columns and beams on a concrete deck
  const t = 0.045, push = (b, c) => { b.c = c; list.push(b); };
  push([x, y, z, w, d, 0.025], DECK);
  const nx = Math.max(1, Math.round(w / 0.8)), ny = Math.max(1, Math.round(d / 0.8));
  for (let i = 0; i <= nx; i++) { const px = x + (w - t) * i / nx; push([px, y, z, t, t, h], STEEL); push([px, y + d - t, z, t, t, h], STEEL); push([px, y, z + h - t, t, d, t], STEEL); }
  for (let j = 1; j < ny; j++) { const py = y + (d - t) * j / ny; push([x, py, z, t, t, h], STEEL); push([x + w - t, py, z, t, t, h], STEEL); }
  push([x, y, z + h - t, w, t, t], STEEL); push([x, y + d - t, z + h - t, w, t, t], STEEL);
}
let city = new THREE.Group(); scene.add(city);
let ANIMS = [], pickables = [], tagObjs = [], buildings = [], cars = null, cranes = [], LAYOUT = null, groupMode = 'work', NET = null;

// world: iso x → X, iso y → Z, height → Y
function box(x, y, z, w, d, h, material, cast = true, parent = city) {
  const s = SK(), big = w * h + h * d + w * d > 0.03, m = new THREE.Mesh(s.rounded && big && h > 0.06 && material.visible !== false ? RBOX : BOX, material);
  if (s.lines && big && material.visible !== false && !material.transparent) m.add(new THREE.LineSegments(EDGES, inkMat())); m.scale.set(w, h, d); m.position.set(x + w / 2, z + h / 2, y + d / 2); m.castShadow = cast; m.receiveShadow = true; parent.add(m); return m;
}
function instanced(list, material, parent = city, geo = BOX) { // list of [x,y,z,w,d,h] (or planes with rot) → one draw call
  if (!list.length) return; const im = new THREE.InstancedMesh(geo, material, list.length), m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), c = new THREE.Color();
  list.forEach((b, i) => { if (b.rot != null) { q.setFromEuler(new THREE.Euler(0, b.rot, 0)); m4.compose(new THREE.Vector3(b.x, b.y, b.z), q, new THREE.Vector3(b.w, b.h, 1)); } else { q.identity(); m4.compose(new THREE.Vector3(b[0] + b[3] / 2, b[2] + b[5] / 2, b[1] + b[4] / 2), q, new THREE.Vector3(b[3], b[5], b[4])); } im.setMatrixAt(i, m4); if (b.c != null) im.setColorAt(i, c.set(b.c)); });
  im.receiveShadow = true; parent.add(im); return im;
}
function textTexture(text, color, px = 64, bg = null) {
  const cv = document.createElement('canvas'), ctx = cv.getContext('2d'); ctx.font = `700 ${px}px Inter, sans-serif`;
  cv.width = Math.ceil(ctx.measureText(text).width) + 28; cv.height = px + 24; ctx.font = `700 ${px}px Inter, sans-serif`;
  if (bg) { ctx.fillStyle = bg; ctx.beginPath(); ctx.roundRect ? ctx.roundRect(0, 0, cv.width, cv.height, 14) : ctx.rect(0, 0, cv.width, cv.height); ctx.fill(); }
  ctx.fillStyle = color; ctx.textBaseline = 'middle'; ctx.fillText(text, 14, cv.height / 2);
  const tex = new THREE.CanvasTexture(cv); tex.anisotropy = 8; tex.colorSpace = THREE.SRGBColorSpace; return { tex, aspect: cv.width / cv.height };
}
function textPlane(text, x, y, len, size, color) { // painted flat on the ground (sub-district names)
  const { tex, aspect } = textTexture(text, color); const w = Math.min(len, size * aspect), h = w / aspect;
  const m = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false })); m.scale.set(w, h, 1);
  m.rotation.x = -Math.PI / 2; m.position.set(x + w / 2, 0.06, y); city.add(m);
}
function posterTexture(big, small, bg) { // a billboard poster: a bold headline with a small line under it
  const cv = document.createElement('canvas'); cv.width = 780; cv.height = 300; const c = cv.getContext('2d');
  c.fillStyle = bg; c.fillRect(0, 0, 780, 300); c.fillStyle = 'rgba(0,0,0,.18)'; c.fillRect(0, 226, 780, 74);
  c.fillStyle = '#1b1206'; c.textAlign = 'center'; c.textBaseline = 'middle'; let fs = 150; c.font = `800 ${fs}px Inter, sans-serif`; while (c.measureText(big).width > 720) c.font = `800 ${fs -= 6}px Inter, sans-serif`; c.fillText(big, 390, 116);
  c.fillStyle = '#fff6ea'; c.font = '700 46px Inter, sans-serif'; let s = small; while (c.measureText(s).width > 720 && s.length > 4) s = s.slice(0, -2); c.fillText(s === small ? s : s + '…', 390, 264);
  const tex = new THREE.CanvasTexture(cv); tex.anisotropy = 8; tex.colorSpace = THREE.SRGBColorSpace; return tex;
}
function signpost(text, x, y, P, hue) { // a neighbourhood sign: a pole with a label in the neighbourhood's colour (fixed screen size, always readable)
  box(x - 0.03, y - 0.03, 0.08, 0.06, 0.06, 1.3, mat(P.pole), false);
  const bg = new THREE.Color(hue).lerp(new THREE.Color(theme === 'dark' ? 0xffffff : 0x000000), theme === 'dark' ? 0.3 : 0.3);
  const el = document.createElement('div'); el.className = 'tag hood'; el.textContent = text; el.style.background = '#' + bg.getHexString(); el.style.color = theme === 'dark' ? '#1b1c1e' : '#ffffff';
  const o = new CSS2DObject(el); o.center.set(0, 1); o.position.set(x, 1.4, y); city.add(o); return o;
}

function build(product, keepCamera) {
  clearRoutes(); hoverTag.visible = false; scene.remove(city); city.traverse(o => { if (o.isCSS2DObject && o.element) o.element.remove(); if (o.geometry && o.geometry !== BOX && o.geometry !== PLANE) o.geometry.dispose(); if (o.material && o.material.map) o.material.map.dispose(); });
  city = new THREE.Group(); scene.add(city); tagObjs = []; pickables = []; buildings = []; exploded = null; cars = null; cranes = []; ANIMS = [];
  const P = PAL[theme], loops = DATA.filter(d => (d.p || 'No product') === product && !d.a);
  PLANOBJ.filter(p => (p.p || 'No product') === product).forEach(p => loops.push(p)); ISSUES.filter(i => (i.p || 'No product') === product).forEach(i => loops.push(i));
  const L = layout(loops, groupMode); LAYOUT = L;
  let seed = 17; const rnd = () => (seed = (seed * 9301 + 49297) % 233280) / 233280;
  box(-10, -10, -0.3, L.WW + 20, L.DD + 20, 0.3, mat(P.ground), false);
  const asph = mat(P.asphalt), grass = mat(theme === 'dark' ? 0x4f6451 : 0xa9c99a);
  L.streets.forEach(s => box(s.x, s.y, 0.06, s.w, s.d, 0.012, asph, false));
  // the street network: intersections are nodes, each stretch of street between two crossings is an edge (cars drive the graph)
  const nodes = new Map(), edges = [];
  const H = L.streets.filter(s => s.dir === 'h'), V = L.streets.filter(s => s.dir === 'v');
  H.forEach(h => V.forEach(v => { const cx = v.x + v.w / 2, cy = h.y + h.d / 2; if (cx < h.x - 0.01 || cx > h.x + h.w + 0.01 || cy < v.y - 0.01 || cy > v.y + v.d + 0.01) return;
    const k = cx.toFixed(2) + ',' + cy.toFixed(2); let n = nodes.get(k); if (!n) nodes.set(k, n = { x: cx, y: cy, hw: 0, vw: 0, adj: [] });
    n.hw = Math.max(n.hw, h.d); n.vw = Math.max(n.vw, v.w); (h.nodes = h.nodes || new Set()).add(n); (v.nodes = v.nodes || new Set()).add(n); }));
  NET = [...nodes.values()];
  L.streets.forEach(s => { const ns = [...(s.nodes || [])].sort((a, b) => s.dir === 'h' ? a.x - b.x : a.y - b.y);
    for (let i = 0; i + 1 < ns.length; i++) { const a = ns[i], b = ns[i + 1]; a.adj.push({ n: b, s }); b.adj.push({ n: a, s }); edges.push({ a, b, s }); } });
  const lines = [], zebra = [], medians = [], poles = [], heads = [], tops = [], trunks = [];
  const tree = (x, y, z, r) => { tops.push([x - r, y - r, z + 0.18, r * 2, r * 2, r * 1.6]); trunks.push([x - 0.02, y - 0.02, z, 0.04, 0.04, 0.22]); };
  edges.forEach(({ a, b, s }) => {
    const hz = s.dir === 'h', w = hz ? s.d : s.w, c = hz ? a.y : a.x;
    const p0 = (hz ? a.x : a.y) + (hz ? a.vw : a.hw) / 2, p1 = (hz ? b.x : b.y) - (hz ? b.vw : b.hw) / 2; if (p1 - p0 < 0.6) return;
    const rect = (u0, len, off, th, z, hgt, list) => list.push(hz ? [u0, c + off - th / 2, z, len, th, hgt] : [c + off - th / 2, u0, z, th, len, hgt]);
    if (s.av) { rect(p0 + 0.9, p1 - p0 - 1.8, 0, 0.34, 0.07, 0.06, medians); // avenues: a planted median
      for (let u = p0 + 1.4; u < p1 - 1.2; u += 1.6) tree(hz ? u : c, hz ? c : u, 0.13, 0.13 + rnd() * 0.05); }
    else for (let u = p0 + 0.7; u + 0.35 < p1 - 0.6; u += 0.7) rect(u, 0.35, 0, 0.05, 0.074, 0.004, lines); // lane dashes
    [p0 + 0.1, p1 - 0.4].forEach(u => { for (let k = 0; k < 7; k++) rect(u, 0.3, -w / 2 + 0.14 + (k + 0.25) * (w - 0.28) / 7, (w - 0.28) / 14, 0.075, 0.004, zebra); }); // zebra crossings
  });
  // blocks: a pavement tinted by neighbourhood; empty cells and unused blocks are parks
  L.blocks.forEach(b => {
    if (b.slot && b.slot.landfill) { drawLandfill(b, P, rnd); return; }
    if (!b.slot && SK().park) { SK().park({ b, P, rnd, tree }); return; }
    if (!b.slot) { box(b.x, b.y, 0.06, BW, BD, 0.05, grass, false); for (let i = 0; i < 9; i++) tree(b.x + 0.5 + rnd() * (BW - 1), b.y + 0.5 + rnd() * (BD - 1), 0.11, 0.18 + rnd() * 0.12); return; }
    const pave = new THREE.Color(P.walk).lerp(new THREE.Color(b.g.hue), theme === 'dark' ? 0.24 : 0.32).getHex();
    box(b.x, b.y, 0.06, BW, BD, 0.05, mat(pave), false);
    for (let r = 0; r < BLOCK_D; r++) for (let c = 0; c < BLOCK_W; c++) if (!b.slot.occ[r][c]) { const px = b.x + PAD + c * UX, py = b.y + PAD + r * UY;
      box(px, py, 0.11, UX - GX, UY - GX, 0.02, grass, false); [[0.6, 0.6], [1.6, 1.3], [0.9, 1.8]].forEach(([u, v]) => tree(px + u, py + v, 0.13, 0.17)); }
    for (let lx = b.x + 0.7; lx < b.x + BW - 0.3; lx += 2.6) [b.y + 0.12, b.y + BD - 0.16].forEach(ly => { poles.push([lx, ly, 0.11, 0.04, 0.04, 0.75]); heads.push([lx - 0.06, ly - 0.06, 0.86, 0.16, 0.16, 0.05]); });
  });
  // parkland round the edge of the city
  for (let i = 0; i < 220; i++) { const x = -9 + rnd() * (L.WW + 18), y = -9 + rnd() * (L.DD + 18); const inside = L.streets.some(s => x > s.x - 0.4 && x < s.x + s.w + 0.4 && y > s.y - 0.4 && y < s.y + s.d + 0.4) || L.blocks.some(b => x > b.x - 0.3 && x < b.x + BW + 0.3 && y > b.y - 0.3 && y < b.y + BD + 0.3); if (!inside) tree(x, y, 0, 0.16 + rnd() * 0.12); }
  instanced(lines, new THREE.MeshBasicMaterial({ color: P.lane }));
  instanced(zebra, new THREE.MeshBasicMaterial({ color: P.lane }));
  if (SK().median) SK().median({ medians, P, grass }); else instanced(medians, grass);
  if (SK().street) SK().street({ L, edges, nodes, P, rnd });
  instanced(poles, mat(P.pole));
  instanced(heads, new THREE.MeshBasicMaterial({ color: theme === 'dark' ? P.lamp : 0xbdb7a8, toneMapped: false }));
  const tm = instanced(tops, mat(theme === 'dark' ? 0x5b7a58 : 0x86ad78), city, TREE); if (tm) tm.castShadow = true; instanced(trunks, mat(0x7a5c3e));
  // neighbourhood signs: an upright sign at each neighbourhood's corner, in its colour. A plan's title is shortened
  // for the street: no parenthetical, cut at a word near 26 letters
  const short = t => { t = t.replace(/\s*\([^)]*\)\s*/g, ' ').replace(/\s+[—–:-]\s+.*$/, '').trim(); if (t.length <= 28) return t; const c = t.slice(0, 26); return c.slice(0, Math.max(12, c.lastIndexOf(' '))).replace(/[\s,;&+]+$/, '') + '…'; };
  L.signs.forEach(sg => { const g = sg.g, np = g.loops.filter(d => d.plan).length, nl = g.loops.length - np;
    signpost(g.landfill ? (g.issues.some(i => i.status !== 'resolved') ? 'Recycling · ' + g.issues.filter(i => i.status !== 'resolved').length + ' to sort' : 'Recycling · all clear') : g.plan ? 'Planned · ' + np : short(g.name) + ' · ' + nl + (np ? ' · ' + np + ' planned' : ''), sg.x + 0.16, sg.y + 0.16, P, g.hue); });
  // traffic: cars drive the street graph and turn at crossings; more of them when work is live
  const live = loops.filter(d => !d.plan && ['run', 'need'].includes(stateOf(d))).length, nCars = edges.length ? Math.min(90, Math.round(6 + edges.length * 0.3 + live * 6)) : 0;
  if (nCars) { const im = new THREE.InstancedMesh(BOX, new THREE.MeshStandardMaterial({ roughness: 0.5 }), nCars), list = [], CARC = (SK().car && SK().car.colors) || [0xd9534f, 0xf2c14e, 0x5f86e0, 0xe6e2d8, 0x55565b];
    for (let i = 0; i < nCars; i++) { const e = edges[Math.floor(rnd() * edges.length)], fw = rnd() < 0.5; list.push({ a: fw ? e.a : e.b, b: fw ? e.b : e.a, s: e.s, t: rnd(), v: 0.9 + rnd() * 0.7 }); im.setColorAt(i, new THREE.Color(CARC[i % 5])); }
    im.castShadow = true; city.add(im); cars = { im, list }; }
  // buildings
  L.items.forEach(it => it.d.plan ? planLot(it, P) : building(it, P));
  // light + camera framing
  const cx = L.WW / 2, cz = L.DD / 2, span = Math.max(L.WW, L.DD);
  sun.position.set(cx + span * 0.6, span * 1.1, cz + span * 0.2); sun.target.position.set(cx, 0, cz);
  const sc = sun.shadow.camera; sc.left = -span; sc.right = span; sc.top = span; sc.bottom = -span; sc.near = 0.1; sc.far = span * 4; sc.updateProjectionMatrix();
  if (!keepCamera) { controls.target.set(cx, 0, cz); frame(span); setTilt(tilt, true); }
  byId('foot').textContent = `${product} · ${loops.filter(d => !d.plan && !d.issue).length} loops in ${L.groups.length} neighbourhoods · ${nodes.size} crossings · real 3D (three.js)`;
  applyTheme(); requestRender();
}
function frame(span) { const a = stage.clientWidth / stage.clientHeight, h = span * 0.44; camera.left = -h * a; camera.right = h * a; camera.top = h; camera.bottom = -h; camera.zoom = 1; camera.updateProjectionMatrix(); }

// each building is a group of floor groups, so the floors can slide apart (exploded view)
function building(it, P) {
  const d = it.d, st = stateOf(d), S_ = styleOf(d); P = Object.assign({}, P, (S_.bpal || {})[theme] || {}); // the style's materials; colour stays for results
  const colr = S_.colorOf ? S_.colorOf(d, st, P) : P.done;
  const LW = it.w * UX - GX, LD = it.h * UY - GX, bx = it.x, by = it.y, subs = d.sub || [], SW = subs.length ? Math.min(1.3, LW * 0.3) : 0;
  let fx = bx + 0.4, fy = by + 0.24, FW = LW - 0.6 - (SW ? SW + 0.25 : 0), FD = LD - 0.66;
  if (S_.footprint) ({ fx, fy, FW, FD } = S_.footprint({ fx, fy, FW, FD, d, LW, LD, bx, by }));
  const bg = new THREE.Group(); city.add(bg);
  const B = { d, group: bg, floors: [], roof: new THREE.Group(), center: new THREE.Vector3(fx + FW / 2, 0, fy + FD / 2) }; buildings.push(B);
  box(bx, by, 0.11, LW, LD, 0.03, mat(P.lot), false, bg);
  const lv = levelsOf(d), cap = hasCap(d, lv), total = lv.length + (cap ? 1 : 0), filled = progressOf(d, total);
  const litMode = (st === 'run' || st === 'need' || d.ship === true) ? 'all' : d.ship === false ? 'none' : 'half';
  const seedBase = [...d.n].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 9973, 7);
  let z = 0.14, roof = null;
  // every floor has a steel frame (the plan) and, once the agent has worked, the finished floor (solid) over it
  const newFloor = (label, kind, turns, on) => { const g = new THREE.Group(), solid = new THREE.Group(); bg.add(g); g.add(solid); const F = { g, solid, label, kind, turns, on, z0: z, wins: [], trim: [], blocks: [], frame: [] }; B.floors.push(F); return F; };
  const hitBox = (F, x, y, w, dd, h, step, kind) => { const m = box(x, y, z, w, dd, h, HIT, false, F.g); m.userData = { loop: d, step, kind, B }; pickables.push(m); };
  const own = (F, x, y, w, dd, h, stepId, kind, on, idx) => {
    const tone = new THREE.Color(colr); if (idx % 2) tone.multiplyScalar(0.9);
    frameBeams(F.frame, x, y, z, w, dd, h);
    if (!on) { hitBox(F, x, y, w, dd, h, stepId, kind); return null; }
    const m = box(x, y, z, w, dd, h, mat(tone.getHex()), true, F.solid);
    m.userData = { loop: d, step: stepId, kind, B }; pickables.push(m);
    const ctx = { F, x, y, z, w, dd, h, idx, kind, on, d, st, P, colr, litMode, seedBase, stepId, add: (a, b, c, e, f, g2, m, cast = true) => box(a, b, c, e, f, g2, m, cast, F.solid) };
    if (on && kind !== 'manager') { const v = verdictOf(d, stepId); if (v) faces(x, y, z, w, dd, 0.012).forEach(([W, f, rot]) => { const p = f(W / 2, 0.018); F.trim.push({ x: p[0], y: p[1], z: p[2], w: W + 0.004, h: 0.036, rot, c: OUTC[v] }); }); } // a painted band under each floor: how that agent's work ended
    if (S_.facade && S_.facade(ctx) === false) return m; // a style may draw its own facade instead of the window grid
    if (on && kind !== 'manager') {
      const rows = Math.max(1, Math.round((h - 0.06) / ROWH));
      [[w, (u, v) => [x + u, z + v, y + dd + 0.005], 0], [w, (u, v) => [x + w - u, z + v, y - 0.005], Math.PI], [dd, (u, v) => [x + w + 0.005, z + v, y + dd - u], Math.PI / 2], [dd, (u, v) => [x - 0.005, z + v, y + u], -Math.PI / 2]].forEach(([W, f, rot], fi) => {
        const n = Math.max(1, Math.min(4, Math.round(W / 0.9))), mu = W / n, pier = Math.min(0.12, mu * 0.14);
        const ribbon = kind === 'input';
        for (let rw = 0; rw < rows; rw++) for (let i = 0; i < (ribbon ? 1 : n); i++) {
          const lit = litMode === 'all' ? true : litMode === 'none' ? false : ((seedBase + i * 3 + rw + fi + idx) % 3) === 0;
          const p = ribbon ? f(W / 2, 0.03 + rw * ROWH + ROWH * 0.5) : f(i * mu + mu / 2, 0.03 + rw * ROWH + ROWH * 0.5);
          F.wins.push({ x: p[0], y: p[1], z: p[2], w: ribbon ? W - 0.14 : (mu - pier * 2) * 0.5, h: ribbon ? ROWH * 0.42 : ROWH * 0.8, rot, c: lit ? (litMode === 'half' ? (SK().lines ? P.dark : 0x9c8a5c) : SK().lines && (st === 'run' || st === 'need') ? P.lamp : P.lit) : P.dark });
        }
      });
    }
    return m;
  };
  const order = []; if (cap) order.push({ cap: true, oi: 0 }); lv.forEach((l, i) => order.push({ level: l, oi: i + (cap ? 1 : 0) }));
  order.forEach((o, oiIdx) => {
    const on = o.oi < filled;
    if (o.cap) {
      const F = newFloor(d.m, 'manager · storefront', turnsOf(d, d.m), on);
      frameBeams(F.frame, fx - 0.06, fy - 0.06, z, FW + 0.12, FD + 0.12, BASE_H);
      let baseMesh = null; if (on) { const base = box(fx - 0.06, fy - 0.06, z, FW + 0.12, FD + 0.12, BASE_H, mat(new THREE.Color(colr).multiplyScalar(0.8).getHex()), true, F.solid); base.userData = { loop: d, step: d.m, kind: 'manager', B }; pickables.push(base); baseMesh = base; }
      else hitBox(F, fx - 0.06, fy - 0.06, FW + 0.12, FD + 0.12, BASE_H, d.m, 'manager');
      if (on && S_.base) S_.base({ baseMesh, seedBase, F, x: fx - 0.06, y: fy - 0.06, z, w: FW + 0.12, dd: FD + 0.12, h: BASE_H, d, st, P, colr, litMode, add: (a, b, c, e, f, g2, m, cast = true) => box(a, b, c, e, f, g2, m, cast, F.solid) });
      else if (on) { const gc = litMode === 'none' ? P.dark : litMode === 'half' ? 0x9c8a5c : P.lit;
        F.wins.push({ x: fx + FW / 2, y: z + BASE_H * 0.36, z: fy + FD + 0.07, w: FW - 0.1, h: BASE_H * 0.55, rot: 0, c: gc }); F.wins.push({ x: fx + FW + 0.07, y: z + BASE_H * 0.36, z: fy + FD / 2, w: FD - 0.1, h: BASE_H * 0.55, rot: Math.PI / 2, c: gc });
        box(fx - 0.06, fy + FD + 0.06, z + BASE_H * 0.62, FW + 0.12, 0.22, 0.04, mat(new THREE.Color(colr).multiplyScalar(0.6).getHex()), true, F.solid);
        nameSign(F.solid, nameOf(d), { font: '600 %px Inter, sans-serif', color: '#f2eadb', bg: '#2a2b2e', px: 44, pad: 12 }, fx + FW / 2, z + BASE_H * 0.86, fy + FD + 0.07, 0.2, FW * 0.85); } // a signboard over the shop
      z += BASE_H + 0.02; roof = { x: fx - 0.06, y: fy - 0.06, w: FW + 0.12, d: FD + 0.12, z }; return;
    }
    const level = o.level, n = level.length;
    if (level[0].kind === 'sublink') { const F = newFloor('sub-loops: ' + level.map(s => s.id).join(', '), 'sub-loop step', null, on); frameBeams(F.frame, fx, fy, z, FW, FD, 0.16); if (on) box(fx, fy, z, FW, FD, 0.16, mat(0x8a8d93, { metalness: 0.6, roughness: 0.4 }), true, F.solid); else hitBox(F, fx, fy, FW, FD, 0.16, level[0].id, 'sublink'); z += 0.2; return; }
    const F = newFloor(level.map(s => s.id).join(' ∥ '), n > 1 ? 'parallel · ' + n : (level[0].kind === 'input' ? 'input provider' : 'worker'), Math.max(...level.map(s => turnsOf(d, s.id) || 0)) || null, on);
    const sb = S_.setback ? S_.setback(oiIdx, order.length) : 1, LFX = fx + FW * (1 - sb) / 2, LFY = fy + FD * (1 - sb) / 2, LFW = FW * sb, LFD = FD * sb;
    const alongX = LFW >= LFD, cols = alongX ? Math.min(n, 3) : Math.ceil(n / 3), rows = alongX ? Math.ceil(n / 3) : Math.min(n, 3), gap = 0.12;
    const w = (LFW - (cols - 1) * gap) / cols, dd = (LFD - (rows - 1) * gap) / rows, hs = level.map(x => hFor(d, x.id) * (S_.hMul || 1)), h = Math.max(...hs), top = oiIdx === order.length - 1;
    for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
      const k = alongX ? r * cols + c : c * rows + r; if (k >= n) continue;
      const mx = LFX + c * (w + gap), my = LFY + r * (dd + gap);
      const hk = on ? hs[k] : h; own(F, mx, my, w, dd, hk, level[k].id, level[k].kind, on, o.oi);
      if (hk >= h - 0.001) roof = { x: mx, y: my, w, d: dd, z: z + hk };
      if (on && n > 1 && !top && hs[k] < h - 0.01) [[mx, my], [mx + w - 0.07, my], [mx + w - 0.07, my + dd - 0.07], [mx, my + dd - 0.07]].forEach(([px, py]) => box(px, py, z + hs[k], 0.07, 0.07, h - hs[k], mat(0x8a8d93, { metalness: 0.6, roughness: 0.4 }), true, F.solid));
    }
    z += h + 0.03;
    if (n > 1 && !top) { if (on) box(LFX - 0.04, LFY - 0.04, z - 0.03, LFW + 0.08, LFD + 0.08, 0.05, mat(0x9a9da3, { metalness: 0.5, roughness: 0.5 }), true, F.solid); z += 0.03; roof = { x: LFX, y: LFY, w: LFW, d: LFD, z }; }
    if (S_.setback && on && sb < 0.999) { const pb = S_.setback(oiIdx - 1, order.length); if (pb > sb) box(fx + FW * (1 - pb) / 2, fy + FD * (1 - pb) / 2, F.z0 - 0.02, FW * pb, FD * pb, 0.04, mat(S_.ledge ? S_.ledge(P) : P.plate), true, F.solid); } // a setback ledge
  });
  B.floors.forEach(F => { instanced(F.wins, WINMAT, F.solid, PLANE); instanced(F.trim, mat(0xffffff, { roughness: 0.8 }), F.solid, PLANE); const bl = instanced(F.blocks, mat(0xffffff, { roughness: 0.85 }), F.solid); if (bl) bl.castShadow = true; F.fm = instanced(F.frame, FRAMEMAT, F.g); if (F.fm) { F.fm.castShadow = true; F.fm.visible = !F.on; } });
  bg.add(B.roof); B.fp = { fx, fy, FW, FD }; B.topZ = z; B.st = d.st; B.end = d.end || d.act || d.up; B.filled = filled; B.live = st === 'run' && !isStale(d);
  subs.forEach((name, k) => { const per = (LD - 0.5) / subs.length, as = Math.min(SW, per - 0.18), ax = bx + LW - SW - 0.12, ay = by + 0.24 + k * per;
    box(ax, ay, 0.14, as, as, 0.62, mat(0x8f99a6, { metalness: 0.45, roughness: 0.45 }), true, bg); box(ax + as * 0.2, ay + as * 0.2, 0.76, as * 0.6, as * 0.6, 0.18, mat(colr), true, bg); });
  const sctx = { B, d, st, P, colr, roof, z, fx, fy, FW, FD, bx, by, LW, LD, seedBase, finished: filled >= total, total, lv };
  if (roof && S_.crown) S_.crown(sctx); else if (roof && filled >= total) roofDeco(d, roof, P, B.roof); // the plain building's roof: garden, pool, vents and tarps, or a rusty water tower
  if (d.st && lv.length && S_.runner) { const g = S_.runner(sctx); if (g) { bg.add(g); g.visible = B.live; B.crane = g; } }
  else if (d.st && lv.length) { // a tower crane: shown while the loop is building (now, or at that moment in history)
    const top = z, crane = new THREE.Group(), cx = bx + LW - 0.35, cy = by + 0.35, H = top + 1.6; crane.position.set(cx, 0, cy); bg.add(crane); crane.visible = B.live; B.crane = crane;
    const yel = mat(0xf2b01e, { metalness: 0.3, roughness: 0.5 });
    const mast = new THREE.Mesh(BOX, yel); mast.scale.set(0.12, H, 0.12); mast.position.set(0, H / 2, 0); mast.castShadow = true; crane.add(mast);
    const head = new THREE.Group(); head.position.set(0, H, 0); crane.add(head);
    const jib = new THREE.Mesh(BOX, yel); jib.scale.set(2.2, 0.08, 0.1); jib.position.set(-0.8, 0, 0); jib.castShadow = true; head.add(jib);
    const cw = new THREE.Mesh(BOX, mat(0x55565b)); cw.scale.set(0.3, 0.2, 0.2); cw.position.set(0.35, -0.1, 0); head.add(cw);
    const cab = new THREE.Mesh(BOX, mat(0xe6e2d8)); cab.scale.set(0.16, 0.14, 0.16); cab.position.set(0, -0.14, 0.12); head.add(cab);
    const cable = new THREE.Mesh(BOX, mat(0x333333)); cable.scale.set(0.015, 0.9, 0.015); cable.position.set(-1.6, -0.45, 0); head.add(cable);
    const load = new THREE.Mesh(BOX, mat(new THREE.Color(colr).getHex())); load.scale.set(0.25, 0.14, 0.25); load.position.set(-1.6, -0.95, 0); load.castShadow = true; head.add(load);
    cranes.push({ head, crane, speed: 0.18 + (seedBase % 7) * 0.02, phase: seedBase });
  }
  if (d.l) { const gh = Math.min(2.2, 0.5 + d.l / 22), used = Math.min(1, d.u / d.l); box(bx + 0.14, by + 0.14, 0.14, 0.05, 0.05, gh, mat(0x777777), false, bg); box(bx + 0.13, by + 0.13, 0.14, 0.07, 0.07, gh * used, mat(colr), false, bg); }
  // needs you (or silent): a real rooftop billboard — steel legs and bracing, a catwalk, a framed poster and floodlights
  if ((st === 'need' || isStale(d)) && S_.need) S_.need(sctx);
  else if (st === 'need' || isStale(d)) {
    const stale = isStale(d), bw = Math.max(1.4, Math.min(FW * 0.85, 3.2)), bh = bw / 2.6, lift = 0.3 + bw * 0.08, g = new THREE.Group(), steel = mat(0x4a4b4f, { metalness: 0.6, roughness: 0.45 });
    const part = (x, y, zz, w, h, dd, m, rz = 0) => { const o = new THREE.Mesh(BOX, m); o.scale.set(w, h, dd); o.position.set(x, y, zz); o.rotation.z = rz; o.castShadow = true; g.add(o); return o; };
    const poster = posterTexture(stale ? 'SILENT ' + Math.round((NOW - d.act) / 60) + ' MIN' : 'NEEDS YOU', stale ? 'no activity · check on it' : d.n, '#' + new THREE.Color(P.need).getHexString());
    part(0, lift + bh / 2, 0, bw + 0.1, bh + 0.1, 0.06, steel); // the frame
    [1, -1].forEach(side => { const f = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: poster, toneMapped: false })); f.scale.set(bw, bh, 1); f.position.set(0, lift + bh / 2, side * 0.032); if (side < 0) f.rotation.y = Math.PI; g.add(f); });
    [-bw * 0.3, bw * 0.3].forEach(x => { part(x, (lift + bh * 0.85) / 2, -0.08, 0.05, lift + bh * 0.85, 0.05, steel); part(x, lift * 0.5, -0.2, 0.04, lift, 0.04, steel); }); // legs + back struts
    const brace = Math.hypot(bw * 0.6, lift * 0.9); [1, -1].forEach(s => part(0, lift * 0.48, -0.08, brace, 0.025, 0.025, steel, s * Math.atan2(lift * 0.9, bw * 0.6))); // X-bracing
    part(0, lift - 0.02, 0.14, bw + 0.1, 0.02, 0.22, steel); part(0, lift + 0.07, 0.25, bw + 0.1, 0.015, 0.015, steel); // catwalk + rail
    const lampM = new THREE.MeshBasicMaterial({ color: 0xfff1cf, toneMapped: false });
    [-bw * 0.33, 0, bw * 0.33].forEach(x => { part(x, lift + 0.03, 0.3, 0.02, 0.02, 0.22, steel); const h = part(x, lift + 0.07, 0.4, 0.1, 0.05, 0.07, lampM); h.rotation.x = -0.6; h.castShadow = false; }); // floodlights
    g.position.set(fx + FW / 2, z, fy + FD / 2); g.rotation.y = Math.PI / 4; B.roof.add(g); B.billboard = g; B.tagZ = z + lift + bh + 0.35;
  }
}
function roofDeco(d, r, P, parent) {
  const t = outcomeOf(d), q = sub(r, 0.1, 0.1, 0.9, 0.9); if (!t) return;
  if (t === 'great') D.garden(parent, q, 3);
  else if (t === 'good') D.pool(parent, q);
  else if (t === 'mixed') { D.vents(parent, sub(r, 0.1, 0.1, 0.9, 0.5), 2); D.tarps(parent, sub(r, 0.1, 0.5, 0.9, 0.9), hashOf(d.n)); }
  else { D.vents(parent, sub(r, 0.1, 0.1, 0.6, 0.5), 2); const wx = r.x + r.w * 0.7, wy = r.y + r.d * 0.3, z = r.z; // a rusty water tower
    [[0, 0], [0.3, 0], [0.3, 0.3], [0, 0.3]].forEach(([a, b]) => box(wx + a, wy + b, z, 0.03, 0.03, 0.3, cm(RC.rust), true, parent));
    mesh(parent, CYL, cm(RC.rust), wx + 0.16, z + 0.47, wy + 0.16, 0.36, 0.34, 0.36); lean(mesh(parent, CONE, cm(RC.rust2), wx + 0.16, z + 0.72, wy + 0.16, 0.4, 0.16, 0.4), 0.12, 0.2); D.rust(parent, sub(r, 0.1, 0.55, 0.6, 0.9), hashOf(d.n)); }
}
function planLot(it, P) { // a plan's lot shows how far along it is: stakes and tape (idea) → blueprint (plan) → dug pit and digger (approved)
  const d = it.d, LW = it.w * UX - GX, LD = it.h * UY - GX, x = it.x, y = it.y, g = new THREE.Group(); city.add(g);
  box(x, y, 0.11, LW, LD, 0.03, mat(P.lot), false, g);
  const hit = box(x, y, 0.11, LW, LD, 0.1, HIT, false, g); hit.userData = { plan: d }; pickables.push(hit);
  const fx = x + 0.4, fy = y + 0.3, FW = LW - 0.8, FD = LD - 0.7, orange = mat(0xf08a24);
  if (d.stage === 'idea') {
    const pts = [[fx, fy], [fx + FW, fy], [fx + FW, fy + FD], [fx, fy + FD]];
    pts.forEach(([a, b]) => box(a - 0.025, b - 0.025, 0.14, 0.05, 0.05, 0.42, orange, true, g));
    const tape = mat(0xd9483b); pts.forEach(([a, b], i) => { const [c, e] = pts[(i + 1) % 4]; box(Math.min(a, c) - 0.01, Math.min(b, e) - 0.01, 0.46, Math.abs(c - a) + 0.02, Math.abs(e - b) + 0.02, 0.025, tape, false, g); box(Math.min(a, c) - 0.01, Math.min(b, e) - 0.01, 0.34, Math.abs(c - a) + 0.02, Math.abs(e - b) + 0.02, 0.02, mat(0xe8e2d4), false, g); });
    const { tex, aspect } = textTexture('IDEA', '#1b1206', 64, '#f2c14e'), sg = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: tex, side: THREE.DoubleSide })); sg.scale.set(0.3 * aspect, 0.3, 1); sg.position.set(fx + FW / 2, 0.62, fy + FD / 2); sg.rotation.y = Math.PI / 4; g.add(sg);
    box(fx + FW / 2 - 0.02, fy + FD / 2 - 0.02, 0.14, 0.04, 0.04, 0.34, mat(P.pole), true, g);
    return;
  }
  // blueprint: a glowing plate with a grid and the proposed floors drawn as wireframes
  const blue = theme === 'dark' ? 0x9cc2ff : 0x2f5fd0, plate = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ color: theme === 'dark' ? 0x2f6fd8 : 0x6f9be8, transparent: true, opacity: 0.4, toneMapped: false }));
  plate.scale.set(FW, FD, 1); plate.rotation.x = -Math.PI / 2; plate.position.set(fx + FW / 2, 0.15, fy + FD / 2); g.add(plate);
  const grid = []; for (let u = 0.3; u < FW; u += 0.3) grid.push([fx + u, fy, 0.152, 0.012, FD, 0.002]); for (let v = 0.3; v < FD; v += 0.3) grid.push([fx, fy + v, 0.152, FW, 0.012, 0.002]);
  instanced(grid, new THREE.MeshBasicMaterial({ color: blue, transparent: true, opacity: 0.5, toneMapped: false }), g);
  const lm = new THREE.LineBasicMaterial({ color: blue, transparent: true, opacity: d.stage === 'approved' ? 0.45 : 0.9, toneMapped: false });
  const team = d.team || [], rows = Math.max(1, Math.min(16, Math.round(d.budget / Math.max(1, team.length + 1)))), fh = rows * ROWH + 0.06;
  let z = 0.16; [BASE_H].concat(team.map(() => fh)).forEach(h => { const w = new THREE.LineSegments(EDGES, lm); w.scale.set(FW, h, FD); w.position.set(fx + FW / 2, z + h / 2, fy + FD / 2); g.add(w); z += h + 0.03; });
  if (d.stage === 'approved') { // the foundation is dug: a pit, rebar and a digger waiting
    box(fx, fy, 0.12, FW, FD, 0.03, mat(0x3b3027), false, g);
    const rebar = []; for (let u = 0.25; u < FW - 0.1; u += 0.35) for (let v = 0.25; v < FD - 0.1; v += 0.35) rebar.push([fx + u, fy + v, 0.15, 0.02, 0.02, 0.22]); instanced(rebar, mat(0xa0522d), g);
    const yel = mat(0xf2b01e, { metalness: 0.3, roughness: 0.5 }), ex = x + LW - 0.55, ey = y + LD - 0.45;
    box(ex - 0.22, ey - 0.14, 0.14, 0.44, 0.28, 0.1, mat(0x3a3a3a), true, g); box(ex - 0.18, ey - 0.12, 0.24, 0.36, 0.24, 0.14, yel, true, g); box(ex - 0.16, ey - 0.1, 0.38, 0.16, 0.2, 0.14, mat(0xe6e2d8), true, g);
    const arm = new THREE.Mesh(BOX, yel); arm.scale.set(0.5, 0.06, 0.06); arm.position.set(ex - 0.38, 0.46, ey); arm.rotation.z = -0.5; arm.castShadow = true; g.add(arm);
    const bucket = new THREE.Mesh(BOX, mat(0x55565b)); bucket.scale.set(0.12, 0.12, 0.14); bucket.position.set(ex - 0.62, 0.26, ey); g.add(bucket);
  }
}
function drawLandfill(b, P, rnd) { // the recycling centre: tidy when every issue is resolved; open issues show up as dirt piles in the yard
  const g = new THREE.Group(); city.add(g); const open = b.g.issues.filter(i => i.status !== 'resolved'), done = b.g.issues.filter(i => i.status === 'resolved');
  const concrete = theme === 'dark' ? 0x5c5e62 : 0xcfcac0; box(b.x, b.y, 0.06, BW, BD, 0.05, mat(concrete), false, g);
  // painted yard markings
  const paint = []; for (let u = 0.6; u < BW * 0.55; u += 0.9) paint.push([b.x + u, b.y + BD - 0.9, 0.112, 0.05, 0.6, 0.004]); paint.push([b.x + 0.4, b.y + BD - 1.0, 0.112, BW * 0.55, 0.05, 0.004]);
  instanced(paint, new THREE.MeshBasicMaterial({ color: theme === 'dark' ? 0xcfc9b8 : 0xffffff }), g);
  // low green fence with a gate on the street side
  const posts = [], rails = []; const x0 = b.x + 0.15, y0 = b.y + 0.15, x1 = b.x + BW - 0.15, y1 = b.y + BD - 0.15, fence = mat(0x5f8a5a, { metalness: 0.3 });
  for (let u = x0; u <= x1; u += 0.8) { posts.push([u, y0, 0.11, 0.03, 0.03, 0.34]); if (u < b.x + BW / 2 - 0.8 || u > b.x + BW / 2 + 0.8) posts.push([u, y1, 0.11, 0.03, 0.03, 0.34]); }
  for (let v = y0; v <= y1; v += 0.8) { posts.push([x0, v, 0.11, 0.03, 0.03, 0.34]); posts.push([x1, v, 0.11, 0.03, 0.03, 0.34]); }
  rails.push([x0, y0, 0.42, x1 - x0, 0.02, 0.02], [x0, y0, 0.42, 0.02, y1 - y0, 0.02], [x1, y0, 0.42, 0.02, y1 - y0, 0.02], [x0, y1, 0.42, BW / 2 - 0.95, 0.02, 0.02], [b.x + BW / 2 + 0.8, y1, 0.42, x1 - b.x - BW / 2 - 0.8, 0.02, 0.02]);
  instanced(posts, fence, g); instanced(rails, fence, g);
  // the plant: a hall with a sawtooth roof, a roll-up door and a recycling sign
  const hx = b.x + 0.5, hy = b.y + 0.45, HW = 3.4, HD = 2.0, HH = 1.3, wall = mat(theme === 'dark' ? 0x8f9a8c : 0xd9ddd2), roofM = mat(0x5f8a5a, { roughness: 0.6 });
  box(hx, hy, 0.11, HW, HD, HH, wall, true, g);
  for (let k = 0; k < 4; k++) { const tooth = new THREE.Mesh(BOX, roofM); tooth.scale.set(HW / 4, 0.05, HD * 1.02); tooth.position.set(hx + HW / 8 + k * HW / 4, 0.11 + HH + 0.18, hy + HD / 2); tooth.rotation.z = 0.45; tooth.castShadow = true; g.add(tooth);
    box(hx + (k + 1) * HW / 4 - 0.06, hy, 0.11 + HH, 0.04, HD, 0.34, new THREE.MeshBasicMaterial({ color: theme === 'dark' ? 0xffd88a : 0xbfd8ff, toneMapped: false }), false, g); }
  box(hx + HW * 0.55, hy + HD - 0.01, 0.11, 0.9, 0.04, 0.8, mat(0x6d6e72, { metalness: 0.5 }), false, g); // roll-up door
  const { tex, aspect } = textTexture('♻ RECYCLING', '#ffffff', 64, '#3f7a44'), sg = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: tex, toneMapped: false })); sg.scale.set(0.36 * aspect, 0.36, 1); sg.position.set(hx + HW * 0.28, 0.11 + HH - 0.3, hy + HD + 0.012); g.add(sg);
  // conveyor from the yard into the hall
  const cv = mat(0x3a3b3f, { roughness: 0.4 }); box(hx + HW, hy + HD * 0.4, 0.5, 1.6, 0.3, 0.05, cv, true, g); [0.2, 0.8, 1.4].forEach(u => box(hx + HW + u, hy + HD * 0.4 + 0.12, 0.11, 0.04, 0.04, 0.39, mat(P.pole), false, g));
  // sorting bins
  [0x3f6fd8, 0x4f9a4f, 0xe0b03a, 0x8a8d93].forEach((c, k) => { const bx = b.x + 0.7 + k * 0.62, by = b.y + BD - 0.75; box(bx, by, 0.11, 0.5, 0.42, 0.34, mat(c, { roughness: 0.6 }), true, g); box(bx - 0.02, by - 0.02, 0.45, 0.54, 0.46, 0.04, mat(new THREE.Color(c).multiplyScalar(0.7).getHex()), true, g); });
  // resolved issues: neat bales, stacked by the hall
  const KC = { crash: 0x9a5b3c, bug: 0x5f7896, 'follow-up': 0x7e8f6a };
  done.forEach((iss, k) => { const col = k % 3, lay = Math.floor(k / 3) % 2, row = Math.floor(k / 6), bx = b.x + BW - 2.4 + col * 0.66, by = b.y + 0.5 + row * 0.95;
    const m = box(bx, by, 0.11 + lay * 0.5, 0.6, 0.78, 0.48, mat(KC[iss.kind] || 0x8a8d93, { roughness: 0.95 }), true, g); m.userData = { issue: iss }; pickables.push(m); });
  // open issues: dirt piles dumped in the yard — they only exist while something is unresolved
  if (open.length) {
    const HEAP = new THREE.ConeGeometry(0.85, 0.85, 8), heapM = mat(theme === 'dark' ? 0x6d5a45 : 0x8c7358, { flatShading: true }), junk = [0x9a5b3c, 0x5f7896, 0xd9c9a6, 0x7e8f6a, 0x3f4044], stain = mat(theme === 'dark' ? 0x4d4236 : 0x9c8666);
    open.forEach((iss, k) => { const px = b.x + BW * 0.5 + (k % 3) * 1.8 - 0.4, py = b.y + BD - 1.5 - Math.floor(k / 3) * 1.7;
      const st = new THREE.Mesh(new THREE.CircleGeometry(1.2, 14), stain); st.rotation.x = -Math.PI / 2; st.position.set(px, 0.113, py); g.add(st);
      const h = new THREE.Mesh(HEAP, heapM); h.position.set(px, 0.53, py); h.rotation.y = k; h.castShadow = true; h.receiveShadow = true; g.add(h);
      for (let j = 0; j < 14; j++) { const a = rnd() * 6.28, r = 0.2 + rnd() * 0.9; box(px + Math.cos(a) * r - 0.08, py + Math.sin(a) * r - 0.08, r < 0.6 ? 0.14 + (0.85 - r) * 0.8 : 0.11, 0.16, 0.2, 0.12, mat(junk[j % 5]), true, g); }
      box(px - 0.015, py - 0.015, 0.9, 0.03, 0.03, 0.55, mat(P.pole), false, g); const f = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ color: P.need, side: THREE.DoubleSide, toneMapped: false })); f.scale.set(0.36, 0.22, 1); f.position.set(px + 0.18, 1.33, py); g.add(f);
      const hb = box(px - 0.9, py - 0.9, 0.11, 1.8, 1.8, 1.2, HIT, false, g); hb.userData = { issue: iss }; pickables.push(hb); });
  } else { // all clear: a planter and a bench by the gate
    box(b.x + BW / 2 - 1.6, b.y + BD - 0.7, 0.11, 0.8, 0.3, 0.18, mat(0x7a5c3e), true, g); box(b.x + BW / 2 - 1.58, b.y + BD - 0.68, 0.29, 0.76, 0.26, 0.05, mat(0x5b7a58), false, g);
    box(b.x + BW / 2 + 1.0, b.y + BD - 0.65, 0.3, 0.7, 0.2, 0.04, mat(0x9a7b55), true, g); }
  b.center = new THREE.Vector3(b.x + BW / 2, 0, b.y + BD / 2);
}
function addTag(d, x, y, z, cls, text, parent = city) {
  const el = document.createElement('div'); el.className = 'tag ' + cls; el.textContent = text;
  const o = new CSS2DObject(el); o.position.set(x, y, z); parent.add(o); tagObjs.push(o); return o;
}

// ─── exploded view: click a building and its floors slide apart, each labelled with agent · turns · status ───
let exploded = null, explodeAnim = null;
const GAP_EX = 0.55;
function explode(B) {
  if (exploded === B) return;
  collapse(true);
  exploded = B; const t0 = performance.now(); focus(B, 'building');
  B.labels = B.floors.map((F, i) => {
    const el = document.createElement('div'); el.className = 'tag floor' + (F.on ? '' : ' ghost');
    const s = F.on ? '' : ' · not reached'; el.textContent = `${F.label} — ${F.kind}${F.turns != null ? ' · ' + F.turns + ' turns' : ''}${s}`;
    const ids = F.label.replace(/^sub-loops: /, '').split(/ ∥ |, /); el.onclick = () => showFloor(B.d, ids, B);
    el.style.marginLeft = '46px'; const o = new CSS2DObject(el); o.userData.ids = ids; o.center.set(0, 0.5); o.position.set(B.center.x, F.z0 + 0.2, B.center.z); F.g.add(o); return o;
  });
  explodeAnim = { B, t0, from: 0, to: 1 }; requestRender();
}
function collapse(instant) {
  if (!exploded) return; const B = exploded; focus(null);
  (B.labels || []).forEach(o => { o.element.remove(); o.removeFromParent(); }); B.labels = null;
  if (instant) { B.lift = null; B.floors.forEach(F => F.g.position.y = 0); B.roof.position.y = 0; exploded = null; return; }
  explodeAnim = { B, t0: performance.now(), from: 1, to: 0, done: () => { exploded = null; } }; requestRender();
}
function stepExplode(t) {
  if (!explodeAnim) return false; const a = explodeAnim, k = Math.min(1, (t - a.t0) / 420), e = 1 - Math.pow(1 - k, 3), f = a.from + (a.to - a.from) * e;
  placeFloors(a.B, f);
  if (k >= 1) { const done = a.done; explodeAnim = null; if (done) done(); } return true;
}

// ─── camera: iso by default, 90° rotation steps, three tilts, fly-to ───
let tilt = 'iso', azimuth = Math.PI / 4, camAnim = null;
const TILTS = { low: 1.18, iso: Math.acos(1 / Math.sqrt(3)), high: 0.35 };
function setTilt(t, instant) {
  tilt = t; root.querySelectorAll('[data-tilt]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.tilt === t)));
  const r = 200, pol = TILTS[t], tg = controls.target;
  const to = new THREE.Vector3(tg.x + r * Math.sin(pol) * Math.sin(azimuth), tg.y + r * Math.cos(pol), tg.z + r * Math.sin(pol) * Math.cos(azimuth));
  if (instant) camera.position.copy(to); else camAnim = { from: camera.position.clone(), to, t0: performance.now() };
  requestRender();
}
function flyTo(point, zoom) { // move target (and camera with it) to a point, zoom in
  const off = camera.position.clone().sub(controls.target), aim = point.clone();
  if (stage.clientWidth > 760) { // keep the building clear of the side panel: aim a little to its right, so the building sits left of centre
    const right = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 0).setY(0).normalize(), perPx = (camera.right - camera.left) / zoom / stage.clientWidth;
    aim.add(right.multiplyScalar(175 * perPx)); }
  camAnim = { from: camera.position.clone(), to: aim.clone().add(off), tFrom: controls.target.clone(), tTo: aim, zFrom: camera.zoom, zTo: zoom, t0: performance.now() }; requestRender();
}
function rotate(dir) { const off = camera.position.clone().sub(controls.target); azimuth = Math.atan2(off.x, off.z) + dir * Math.PI / 2; setTilt(tilt, false); }
byId('rotL').onclick = () => rotate(-1); byId('rotR').onclick = () => rotate(1);
root.querySelectorAll('[data-tilt]').forEach(b => b.onclick = () => { const off = camera.position.clone().sub(controls.target); azimuth = Math.atan2(off.x, off.z); setTilt(b.dataset.tilt, false); });
// keyboard: WASD / arrows pan (MapControls), Q/E rotate, +/- zoom, Esc collapses
controls.listenToKeyEvents(window); controls.keyPanSpeed = 22;
controls.keys = { LEFT: 'KeyA', UP: 'KeyW', RIGHT: 'KeyD', BOTTOM: 'KeyS' };
onWin('keydown', e => {
  if (/INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) return;
  if (e.key === 'q' || e.key === 'Q') rotate(-1); else if (e.key === 'e' || e.key === 'E') rotate(1);
  else if (e.key === '+' || e.key === '=') { camera.zoom = Math.min(10, camera.zoom * 1.3); camera.updateProjectionMatrix(); requestRender(); }
  else if (e.key === '-') { camera.zoom = Math.max(0.4, camera.zoom / 1.3); camera.updateProjectionMatrix(); requestRender(); }
  else if (e.key === 'Escape') { if (!$('needs').hidden) toggleNeeds(false); else closeAll(); }
  else if (e.key.startsWith('Arrow')) { const s = 1.2 / camera.zoom, off = camera.position.clone().sub(controls.target); const fwd = new THREE.Vector3(-off.x, 0, -off.z).normalize(), right = new THREE.Vector3(fwd.z, 0, -fwd.x);
    const mv = e.key === 'ArrowUp' ? fwd.multiplyScalar(s) : e.key === 'ArrowDown' ? fwd.multiplyScalar(-s) : e.key === 'ArrowLeft' ? right.multiplyScalar(s) : right.multiplyScalar(-s);
    controls.target.add(mv); camera.position.add(mv); requestRender(); e.preventDefault(); }
});

// ─── theme ───
function applyTheme() {
  const P = PAL[theme]; P.bg = host.bg(theme);
  scene.background = new THREE.Color(P.bg); sun.intensity = P.sun; hemi.intensity = P.hemi; bloom.enabled = theme === 'dark' && !SK().lines; renderer.shadowMap.enabled = !SK().lines;
}
{ const ss = byId('skin'); [['kind', 'Buildings by what they made'], ['plain', 'Plain buildings']].forEach(([k, v]) => { const o = document.createElement('option'); o.value = k; o.textContent = v; ss.append(o); }); ss.value = archMode;
  ss.onchange = () => { archMode = ss.value; try { localStorage.setItem('city3d.arch', archMode); } catch (e) {} build(prodSel.value, true); applyTime(); }; }

// ─── picking + panel ───
const ray = new THREE.Raycaster(), mouse = new THREE.Vector2(); let hovered = null, downAt = null;
function pick(e) { const r = renderer.domElement.getBoundingClientRect(); mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1); ray.setFromCamera(mouse, camera); if (FV && ROOMPICK.length) { const rh = ray.intersectObjects(ROOMPICK, false)[0]; if (rh) return rh.object; } const shown = o => { for (; o; o = o.parent) if (!o.visible) return false; return true; }, hit = ray.intersectObjects(pickables, false).find(h => shown(h.object)); return hit ? hit.object : null; }
const $ = byId;
function el(tag, text, cls) { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
const hoverTag = (() => { const o = new CSS2DObject(el('div', '', 'tag')); o.visible = false; scene.add(o); return o; })();
renderer.domElement.addEventListener('pointermove', e => {
  if (e.buttons) return; const o = pick(e); if (o === hovered) return;
  if (hovered && hovered.material.emissive) hovered.material.emissive.setHex(0);
  hovered = o; renderer.domElement.style.cursor = o ? 'pointer' : 'grab';
  if (o && o.material.emissive && !o.material.transparent) { if (!o.material._own) { o.material = o.material.clone(); o.material._own = true; } o.material.emissive.setHex(theme === 'dark' ? 0x223355 : 0x223366); }
  const B = o && o.userData.B; // a name tag only for the building under the cursor
  if (o && o.userData.room) { hoverTag.element.textContent = o.userData.room.label; o.getWorldPosition(hoverTag.position); hoverTag.position.y += 0.25; hoverTag.visible = true; }
  else if (B && B !== exploded) { hoverTag.element.textContent = B.d.n; hoverTag.position.set(B.center.x, (B.tagZ || B.topZ + 0.3) + 0.2, B.center.z); hoverTag.visible = true; }
  else if (o && o.userData.plan) { const pl = o.userData.plan; hoverTag.element.textContent = STAGES.find(s => s[0] === pl.stage)[1] + ' · ' + pl.n; hoverTag.position.copy(o.position).setY(1.2); hoverTag.visible = true; }
  else if (o && o.userData.issue) { const i = o.userData.issue; hoverTag.element.textContent = (i.status === 'resolved' ? 'Recycled · ' : 'Open ' + i.kind + ' · ') + (i.n.length > 60 ? i.n.slice(0, 58) + '…' : i.n); hoverTag.position.copy(o.position).setY(1.1); hoverTag.visible = true; }
  else hoverTag.visible = false;
  requestRender();
});
renderer.domElement.addEventListener('pointerdown', e => downAt = [e.clientX, e.clientY]);
renderer.domElement.addEventListener('pointerup', e => {
  if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 4) return; const o = pick(e);
  if (o && o.userData.room) { roomAct(o.userData.room); return; }
  if (FV && !(o && o.userData.B === FV.B)) { closeFloorView(); return; } // a click outside the room flies back out
  if (o && o.userData.B) { const B = o.userData.B; if (exploded === B && o.userData.step) showFloor(B.d, [o.userData.step], B); else { explode(B); show(o.userData); } }
  else if (o && (o.userData.plan || o.userData.issue)) { collapse(false); show(o.userData); }
  else closeAll();
});
renderer.domElement.addEventListener('dblclick', e => { const o = pick(e); if (o && o.userData.B) flyTo(o.userData.B.center, Math.max(camera.zoom, 3)); });
const STL = { need: 'Needs you', run: 'Running', fail: 'Failed', stop: 'Stopped', draft: 'Draft', done: 'Done' };
const stColor = st => `var(--s-${st === 'need' ? 'need' : st === 'run' ? 'run' : st === 'fail' ? 'fail' : 'done'})`;
function closeAll() { closeFloorView(false); collapse(false); clearRoutes(); show(null); }
function panelShell() { const p = $('panel'); p.replaceChildren(); p.hidden = false; const x = el('button', '×', 'x'); x.type = 'button'; x.setAttribute('aria-label', 'Close'); x.onclick = closeAll; p.append(x); return p; }
function show(u) {
  if (u && u.issue) return showIssue(u.issue);
  if (u && u.plan) return showPlan(u.plan);
  if (!u || !u.loop) { $('panel').hidden = true; $('panel').replaceChildren(); clearRoutes(); return; }
  const p = panelShell();
  if (u.plan) { clearRoutes(); p.append(el('span', 'Planned', 'pill'), el('h2', u.plan.n), el('p', 'A plan from the idea hub that no loop has picked up yet.', 'note')); return; }
  const d = u.loop, st = stateOf(d), stale = isStale(d), B = u.B || buildings.find(x => x.d === d), row = el('div', null, 'row');
  const pill = el('span', stale ? 'Silent' : STL[st], 'pill'); pill.style.color = stColor(stale ? 'need' : st);
  row.append(el('span', d.u + '/' + d.l + ' turns'), el('span', d.sc == null ? 'no score' : 'score ' + d.sc), el('span', d.ship === true ? 'shipped' : d.ship === false ? 'not shipped' : 'no code'), el('span', d.st ? new Date(d.st * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : 'not started'));
  p.append(pill, el('h2', d.n), row);
  { const a = el('div', null, 'acts'), o = el('button', 'Open the loop ›', 'primary'); o.type = 'button'; o.onclick = () => host.openLoop(d); a.append(o); p.append(a); }
  { const kd = kindOf(d), oc = outcomeOf(d), mk = el('p', null, 'note'), sw = el('i', null, 'sw');
    mk.append(el('b', KIND_NAME[kd.k] + ' · ' + SKINS[KIND_STYLE[kd.k]].look + '. '), kd.tot ? (kd.src === 'git' ? 'Delivered ' : 'Produced ') + kd.tot + ' files: ' + kd.share.map(([a, b]) => b + '% ' + KIND_NAME[a].toLowerCase()).join(', ') + '.' : kd.src === 'loading' ? 'Still reading what it made…' : kd.src === 'unknown' ? 'What it made isn\'t visible from here, so it\'s drawn plain.' : 'No files — the work was all talk.');
    if (oc) { sw.style.background = '#' + new THREE.Color(OUTC[oc]).getHexString(); mk.append(el('br'), sw, ' Result: ' + OUTWORD[oc]); }
    p.append(mk); }
  if (st === 'need' || stale) p.append(askBox(d, stale));
  // the team, ground floor up; each row opens that agent's rounds
  const team = el('div', null, 'team'), lv = levelsOf(d), agentRow = (id, what) => { const r = el('button'); r.type = 'button'; r.append(el('span', id + ' · ' + what), el('small', (turnsOf(d, id) ?? '–') + 't ›')); r.onclick = () => { if (B && exploded !== B) explode(B); showFloor(d, [id], B); }; team.append(r); };
  if (d.m) agentRow(d.m, 'manager');
  lv.forEach(l => l.forEach(s => agentRow(s.id, (l.length > 1 ? 'parallel ' : '') + (s.kind === 'input' ? 'input' : s.kind === 'sublink' ? 'sub-loop' : 'worker'))));
  p.append(el('h3', 'Team · ground floor up'), team);
  ISSUES.filter(i => i.fixedBy === d).forEach(i => p.append(el('p', 'Fixes the issue “' + i.n + '” from the recycling yard.', 'note')));
  const built = d._fromPlan || (PLANS.find(pp => pp.loops.includes(d.n)) || {}).t; if (built) p.append(el('p', 'Built for the plan “' + built + '”.', 'note'));
  const mine = ISSUES.filter(i => i.loop === d); if (mine.length) { const r = el('div', null, 'rel'); mine.forEach(i => { const b = el('button'); b.type = 'button'; b.append(el('span', i.n.length > 70 ? i.n.slice(0, 68) + '…' : i.n), el('i', i.status === 'resolved' ? 'recycled' : 'open ' + i.kind)); b.onclick = () => showIssue(i); r.append(b); }); p.append(el('h3', 'Issues it filed · at the recycling centre'), r); }
  const rels = relationsOf(d);
  if (rels.length) { const rel = el('div', null, 'rel'); rels.forEach(r => { const b = el('button'); b.type = 'button'; b.append(el('span', r.d.n), el('i', r.why + ((r.d.p || '') !== (d.p || '') ? ' · in ' + (r.d.p || 'No product') : ''))); b.onclick = () => goTo(r.d); rel.append(b); }); p.append(el('h3', 'Lineage · routes on the map'), rel); }
  drawRoutes(d);
}
function askBox(d, stale) { // what the loop is waiting on; answering happens on the loop page
  const box_ = el('div', null, 'ask'), acts = el('div', null, 'acts');
  box_.append(el('p', stale ? 'Running, but nothing has happened for ' + Math.round((NOW - d.act) / 60) + ' minutes.' : (d.q || 'This loop is waiting on you.')));
  const go = el('button', stale ? 'Open the loop ›' : 'Answer on the loop page ›', 'primary'); go.type = 'button'; go.onclick = () => host.openLoop(d);
  acts.append(go); box_.append(acts); return box_;
}
const evClass = s => /completed|satisfied|complete|minor_only|briefed/.test(s) ? 'good' : /needs_work|timeout|error|fail/.test(s) ? 'bad' : 'mid';
function showFloor(d, ids, B) { // opens the floor view (room + time card) for the floor that holds these agents
  B = B || buildings.find(x => x.d === d); if (!B) return; if (exploded !== B) explode(B);
  const fi = B.floors.findIndex(F => idsOfFloor(F).some(id => ids.includes(id))); openFloorView(d, B, Math.max(0, fi));
}

// ─── lineage: routes along the streets to the loops this one came from or led to ───
let routeG = null, routeDots = [];
function relationsOf(d) {
  const out = [], seen = new Set([d]), add = (x, why, dir) => { if (x && !x.a && !seen.has(x)) { seen.add(x); out.push({ d: x, why, dir }); } };
  LINKS.forEach(l => { if (l[1] === d.n) add(DATA.find(x => x.n === l[0]), 'came from · ' + l[2], -1); if (l[0] === d.n) add(DATA.find(x => x.n === l[1]), 'follow-up · ' + l[2], 1); });
  const ser = DATA.filter(x => !x.a && (x.p || '') === (d.p || '') && seriesOf(x.n) === seriesOf(d.n)).sort((a, b) => (a.st || 0) - (b.st || 0)), i = ser.indexOf(d);
  if (i > 0) add(ser[i - 1], 'earlier in series', -1); if (i >= 0 && i < ser.length - 1) add(ser[i + 1], 'next in series', 1);
  return out;
}
function nearestNode(v) { let best = null, bd = Infinity; NET.forEach(n => { const k = Math.hypot(n.x - v.x, n.y - v.z); if (k < bd) { bd = k; best = n; } }); return best; }
function pathBetween(a, b) {
  const dist = new Map([[a, 0]]), prev = new Map(), open = new Set([a]);
  while (open.size) { let u = null; open.forEach(n => { if (!u || dist.get(n) < dist.get(u)) u = n; }); open.delete(u); if (u === b) break;
    u.adj.forEach(({ n }) => { const nd = dist.get(u) + Math.hypot(n.x - u.x, n.y - u.y); if (nd < (dist.get(n) ?? Infinity)) { dist.set(n, nd); prev.set(n, u); open.add(n); } }); }
  const path = [b]; while (path[0] !== a) { const q = prev.get(path[0]); if (!q) return null; path.unshift(q); } return path;
}
function clearRoutes() { if (routeG) { routeG.traverse(o => { if (o.isCSS2DObject) o.element.remove(); }); routeG.removeFromParent(); routeG = null; requestRender(); } routeDots = []; }
function addRoute(fromV, toV, col, k, label, labelPos) { // a glowing route along the streets with dots flowing from → to
  if (!NET) return; if (!routeG) { routeG = new THREE.Group(); city.add(routeG); }
  const path = pathBetween(nearestNode(fromV), nearestNode(toV)); if (!path) return;
  const off = (k % 3 - 1) * 0.16, y = 0.1 + k * 0.004, pts = [new THREE.Vector3(fromV.x, y, fromV.z)].concat(path.map(n => new THREE.Vector3(n.x + off, y, n.y + off)), [new THREE.Vector3(toV.x, y, toV.z)]);
  const m = new THREE.MeshBasicMaterial({ color: col, toneMapped: false, transparent: true, opacity: 0.85 });
  const cum = [0]; for (let i = 1; i < pts.length; i++) { const a = pts[i - 1], b = pts[i], len = a.distanceTo(b); cum.push(cum[i - 1] + len); if (len < 0.01) continue;
    const s = new THREE.Mesh(BOX, m); s.scale.set(len + 0.14, 0.02, 0.14); s.position.copy(a).lerp(b, 0.5); s.rotation.y = -Math.atan2(b.z - a.z, b.x - a.x); routeG.add(s); }
  const n = Math.max(4, Math.round(cum[cum.length - 1] / 2.5)), dots = new THREE.InstancedMesh(new THREE.SphereGeometry(0.11, 10, 8), new THREE.MeshBasicMaterial({ color: 0xffffff, toneMapped: false }), n); routeG.add(dots);
  routeDots.push({ pts, cum, dots, n });
  if (label) { const tg = new CSS2DObject(el('div', label, 'tag rel')); tg.position.copy(labelPos); routeG.add(tg); }
  requestRender();
}
function drawRoutes(d) {
  clearRoutes(); if (!NET || T != null) return; const B = buildings.find(x => x.d === d); if (!B) return;
  relationsOf(d).filter(r => buildings.some(x => x.d === r.d)).forEach((r, k) => { const B2 = buildings.find(x => x.d === r.d), from = r.dir < 0 ? B2 : B, to = r.dir < 0 ? B : B2; // flow runs from the earlier loop to the later one
    addRoute(from.center, to.center, r.why.startsWith('earlier') || r.why.startsWith('next') ? PAL[theme].run : PAL[theme].need, k, r.why, B2.center.clone().setY((B2.tagZ || B2.topZ) + 0.5)); });
}
function stepRoutes(t) {
  if (!routeDots.length) return false; const m4 = new THREE.Matrix4(), v = new THREE.Vector3();
  routeDots.forEach(r => { const L = r.cum[r.cum.length - 1]; for (let j = 0; j < r.n; j++) { const s = ((t / 1000) * 1.8 + j * L / r.n) % L; let i = 1; while (i < r.cum.length - 1 && r.cum[i] < s) i++;
      const a = r.pts[i - 1], b = r.pts[i], f = (s - r.cum[i - 1]) / Math.max(1e-6, r.cum[i] - r.cum[i - 1]); v.copy(a).lerp(b, f); v.y += 0.06; m4.makeTranslation(v.x, v.y, v.z); r.dots.setMatrixAt(j, m4); }
    r.dots.instanceMatrix.needsUpdate = true; });
  return true;
}

// ─── product switcher ───
const prodSel = $('prod'), counts = {};
DATA.filter(d => !d.a).forEach(d => { const k = d.p || 'No product'; counts[k] = (counts[k] || 0) + 1; });
Object.keys(counts).sort((a, b) => counts[b] - counts[a]).forEach(k => { const o = el('option', k + ' · ' + counts[k]); o.value = k; prodSel.append(o); });
prodSel.value = host.project && counts[host.project] ? host.project : (prodSel.options[0] ? prodSel.options[0].value : 'No product'); prodSel.hidden = !!host.project; prodSel.onchange = () => { show(null); build(prodSel.value); applyTime(); };
const grpSel = $('grp'); grpSel.onchange = () => { groupMode = grpSel.value; show(null); build(prodSel.value); applyTime(); };

// ─── needs you: one chip; it opens a short list, N jumps to the next one ───
const NEED = DATA.filter(d => stateOf(d) === 'need' || isStale(d)); let needIdx = -1;
function renderNeeds() {
  const chip = $('needChip'), list = $('needs'); chip.hidden = !NEED.length; if (!NEED.length) { list.hidden = true; return; }
  chip.textContent = NEED.length + (NEED.length === 1 ? ' needs' : ' need') + ' you ▾'; list.replaceChildren();
  NEED.forEach(d => { const r = el('button', null, 'r'); r.type = 'button'; r.append(el('b', isStale(d) ? 'silent' : 'asks'), el('strong', d.n), el('span', isStale(d) ? 'no activity for ' + Math.round((NOW - d.act) / 60) + ' min' : d.q)); r.onclick = () => { toggleNeeds(false); goTo(d); }; list.append(r); });
}
function toggleNeeds(open) { $('needs').hidden = !open; $('needChip').setAttribute('aria-expanded', String(open)); }
$('needChip').onclick = () => toggleNeeds($('needs').hidden);
function goTo(d) {
  const prod = d.p || 'No product'; if (prodSel.value !== prod) { prodSel.value = prod; build(prod); applyTime(); }
  const B = buildings.find(x => x.d === d); if (!B) return; flyTo(B.center, Math.max(camera.zoom, 2.6)); explode(B); show({ loop: d, B });
}
function nextNeed() { if (!NEED.length) return; needIdx = (needIdx + 1) % NEED.length; goTo(NEED[needIdx]); }
onWin('keydown', e => { if (e.key === 'N' && !/INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) nextNeed(); });
// ─── planning: issue (landfill) → idea (stakes) → plan (blueprint) → approved (pit) → break ground (a loop) ───
function landfillCenter() { const b = LAYOUT && LAYOUT.blocks.find(x => x.slot && x.slot.landfill); return b ? b.center : null; }
function planCenter(pl) { const it = LAYOUT && LAYOUT.items.find(x => x.d === pl); return it ? new THREE.Vector3(it.x + (it.w * UX - GX) / 2, 0, it.y + (it.h * UY - GX) / 2) : null; }
function showIssue(iss) {
  clearRoutes(); const p = panelShell(), open = iss.status !== 'resolved', pill = el('span', open ? 'Open ' + iss.kind : 'Recycled ' + iss.kind, 'pill'); pill.style.color = open ? 'var(--s-need)' : 'var(--s-done)';
  const t = +((/(\d{10})$/.exec(iss.id) || [])[1] || 0), row = el('div', null, 'row'); row.append(el('span', t ? new Date(t * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : ''), el('span', open ? 'a dirt pile in the recycling yard' : 'resolved, pressed into a bale'));
  p.append(pill, el('h2', iss.n), row);
  const by = el('div', null, 'rel'), bb = el('button'); bb.type = 'button'; bb.append(el('span', iss.loop.n), el('i', 'filed it')); bb.onclick = () => goTo(iss.loop); by.append(bb); p.append(el('h3', 'Filed by'), by);
  const acts = el('div', null, 'acts');
  if (iss.fixedBy) { const go = el('button', 'Go to the fix ›'); go.type = 'button'; go.onclick = () => goTo(iss.fixedBy); acts.append(go); }
  { const o = el('button', 'Open the issue ›', 'primary'); o.type = 'button'; o.onclick = () => host.openIssue(iss.id); acts.append(o); }
  p.append(acts);
  const B = buildings.find(x => x.d === iss.loop), lf = landfillCenter(); if (B && lf) addRoute(B.center, lf, PAL[theme].need, 0, 'filed an issue', B.center.clone().setY((B.tagZ || B.topZ) + 0.5));
}
function showPlan(pl) { // a plan no loop has picked up yet: stakes and tape on its lot
  clearRoutes(); const p = panelShell(), pill = el('span', 'Plan', 'pill'); pill.style.color = 'var(--focus)';
  p.append(pill, el('h2', pl.n), el('p', 'A plan no loop has picked up yet. Turn it into a loop from its page.', 'note'));
  const acts = el('div', null, 'acts'), o = el('button', 'Open the plan ›', 'primary'); o.type = 'button'; o.onclick = () => host.openPlan(pl.id); acts.append(o); p.append(acts);
}
// ─── floor view (after the example-loop loop's): a cut-away room drawn from the agent's real work, and its time card ───
// Every prop that changes is data: papers on the desk = reports sent, crates by the door = files it produced,
// brass plaque = last commit, rosette = the loop's score, lamp = failed / waiting, a figure at the desk = working right now.
const PUNCH = { completed: 'done', satisfied: 'done', complete: 'done', briefed: 'done', work_remaining: 'more', minor_only: 'more', needs_work: 'more', continue: 'more', wind_down: 'more', parallel: 'more', renudge: 'more', ask_owner: 'wait', input_pending: 'wait', error: 'fail', failed: 'fail', crashed: 'fail', abandoned: 'fail', timeout: 'fail' };
const punchOf = s => PUNCH[s] || 'more';
const PUNCHWORD = { done: 'Done', more: 'More to do', wait: 'Waiting on you', fail: 'Failed' };
const artifactsOf = note => [...new Set(((note || '').match(/artifact=([^\s,;]+)/g) || []).map(a => a.slice(9).replace(/[.),]+$/, '')).filter(a => /[\w-]\.[a-z0-9]{1,6}$/i.test(a) && a.length > 4))]; // the snapshot clips notes, so only whole file names count
const commitOf = note => ((note || '').match(/commit=([0-9a-f]{6,})/) || [])[1];
const saidOf = note => (note || '').replace(/(artifact|commit|tests?)=\S+/g, '').replace(/\s+/g, ' ').trim();
const idsOfFloor = F => F.label.replace(/^sub-loops: /, '').split(/ ∥ |, /);
let FV = null; // the floor we've flown into: { d, B, fi, pivot, ff }
function floorFacts(d, B, fi) {
  const F = B.floors[fi], ids = idsOfFloor(F), isMgr = ids[0] === d.m;
  const reps = (d.ev || []).filter(e => ids.some(id => e[1] === id || (e[1] || '').split('+').includes(id)) && e[2] !== 'parallel' && e[2] !== 'renudge');
  const punches = reps.map(e => punchOf(e[2])), last = reps[reps.length - 1];
  const files = [...new Set(reps.flatMap(e => artifactsOf(e[4])))], commit = [...reps].reverse().map(e => commitOf(e[4])).find(Boolean);
  const live = stateOf(d) === 'run' && !isStale(d) && last && (d.ev || []).slice(-1)[0] === last;
  const waiting = (stateOf(d) === 'need') && (isMgr || (last && punchOf(last[2]) === 'wait'));
  const failed = last && punchOf(last[2]) === 'fail';
  const directions = isMgr ? [] : (d.ev || []).filter(e => e[1] === d.m && ids.some(id => (e[4] || '').includes(id))).slice(-4);
  const kind = isMgr ? 'manager' : F.kind.startsWith('input') ? 'input' : F.kind.startsWith('parallel') ? 'parallel' : F.kind.startsWith('sub-loop') ? 'sublink' : 'worker';
  return { F, ids, isMgr, reps, punches, last, files, commit, live, waiting, failed, directions, kind, turns: ids.reduce((a, id) => a + (turnsOf(d, id) || 0), 0), retries: ids.reduce((a, id) => a + ((d.pa && d.pa[id] || [])[1] || 0), 0) };
}
function textTex(text, { color = '#211a10', bg = null, px = 48, font = '700 %px Courier Prime, monospace', pad = 12 } = {}) {
  const cv = document.createElement('canvas'), c = cv.getContext('2d'), f = font.replace('%', px); c.font = f; cv.width = Math.ceil(c.measureText(text).width) + pad * 2; cv.height = px + pad * 2;
  c.font = f; if (bg) { c.fillStyle = bg; c.fillRect(0, 0, cv.width, cv.height); } c.fillStyle = color; c.textBaseline = 'middle'; c.fillText(text, pad, cv.height / 2);
  const tx = new THREE.CanvasTexture(cv); tx.colorSpace = THREE.SRGBColorSpace; tx.anisotropy = 4; return { tx, aspect: cv.width / cv.height };
}
function cardTex(punches, limit) { // the time card pinned to the wall
  const n = Math.max(12, Math.min(24, limit || punches.length)), cv = document.createElement('canvas'); cv.width = 256; cv.height = 360; const c = cv.getContext('2d');
  c.fillStyle = '#f4ecd6'; c.fillRect(0, 0, 256, 360); c.fillStyle = '#b23a2a'; c.fillRect(0, 0, 256, 44); c.fillStyle = '#fff'; c.font = '700 22px Courier Prime, monospace'; c.fillText('TIME CARD', 14, 30);
  for (let i = 0; i < n; i++) { const x = 30 + (i % 4) * 58, y = 76 + Math.floor(i / 4) * 46, p = punches[i]; c.strokeStyle = '#43392a'; c.lineWidth = 2; c.beginPath(); c.arc(x, y, 14, 0, 7); c.stroke();
    if (!p) { c.setLineDash([3, 3]); c.stroke(); c.setLineDash([]); continue; }
    if (p === 'done') { c.fillStyle = '#211a10'; c.fill(); } else if (p === 'more') { c.fillStyle = '#211a10'; c.beginPath(); c.arc(x, y, 14, 0, Math.PI); c.fill(); }
    else if (p === 'wait') { c.fillStyle = '#e09a2a'; c.beginPath(); c.arc(x, y, 7, 0, 7); c.fill(); } else { c.strokeStyle = '#b23a2a'; c.lineWidth = 4; c.beginPath(); c.moveTo(x - 9, y - 9); c.lineTo(x + 9, y + 9); c.moveTo(x + 9, y - 9); c.lineTo(x - 9, y + 9); c.stroke(); } }
  const tx = new THREE.CanvasTexture(cv); tx.colorSpace = THREE.SRGBColorSpace; return tx;
}
// the reports board: one card per report, coloured by how it ended (the same palette as everywhere)
const PUNCHC = { done: 0x6f9f68, more: 0xc39a45, wait: 0xcf7d3c, fail: 0xa9503e }, repTexCache = {};
function repTex(n, p) { const k = n + p; if (repTexCache[k]) return repTexCache[k]; const cv = document.createElement('canvas'), c = cv.getContext('2d'); cv.width = 64; cv.height = 80;
  c.fillStyle = '#f4ecd6'; c.fillRect(0, 0, 64, 80); c.fillStyle = '#' + new THREE.Color(PUNCHC[p]).getHexString(); c.fillRect(0, 0, 64, 18);
  c.fillStyle = '#211a10'; c.font = '700 30px Courier Prime, monospace'; c.textAlign = 'center'; c.fillText(String(n), 32, 54); c.fillStyle = 'rgba(33,26,16,.35)'; c.fillRect(12, 62, 40, 2); c.fillRect(12, 68, 28, 2);
  const tx = new THREE.CanvasTexture(cv); tx.colorSpace = THREE.SRGBColorSpace; return (repTexCache[k] = tx); }
let ROOMPICK = [];
function buildRoom(d, B, fi, W = 6, D = 4.4) { // one floor's interior at the floor's own proportions, cut away on the two near sides; its things can be pointed at and clicked
  const ff = floorFacts(d, B, fi), P = PAL[theme], g = new THREE.Group(), H = 2.6, put = (x, y, z, w, dd, h, m, cast = true) => box(x, y, z, w, dd, h, m, cast, g);
  ROOMPICK = []; const hot = (o, info) => { o.userData.room = info; ROOMPICK.push(o); return o; };
  const wall = mat(theme === 'dark' ? 0x5a5b60 : P.plate), trim = mat(new THREE.Color(P.plate).multiplyScalar(0.82).getHex()), woodC = theme === 'dark' ? 0x7a5c44 : 0xc8a57e, paper = c2 => textTex(c2, { color: '#211a10', bg: '#f4ecd6', px: 28, pad: 6 });
  put(0, 0, -0.2, W, D, 0.2, trim); // slab
  const planks = []; for (let u = 0; u < W - 0.02; u += 0.4) planks.push(Object.assign([u + 0.01, 0.01, 0, Math.min(0.38, W - u - 0.02), D - 0.02, 0.02], { c: new THREE.Color(woodC).multiplyScalar(0.92 + ((u * 7) % 3) * 0.04).getHex() })); instanced(planks, FRAMEMAT, g);
  put(0, -0.12, 0, W, 0.12, H, wall); put(-0.12, -0.12, 0, 0.12, D + 0.12, H, wall); // back wall and left wall
  put(0, 0, 0, W, 0.02, 0.3, trim, false); put(0, 0, 0, 0.02, D, 0.3, trim, false); // skirting
  const winC = d.ship === true || ff.live ? P.lit : theme === 'dark' ? 0x1c2638 : 0x9fb4c8, win = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ color: winC, toneMapped: false })); win.scale.set(1.5, 1.1, 1); win.position.set(W - 1.1, 1.55, 0.01); g.add(win); // the window: lit if the work shipped
  put(W - 1.9, -0.02, 0.97, 1.6, 0.06, 0.06, trim); put(W - 1.13, -0.02, 1.0, 0.05, 0.06, 1.1, trim);
  // the reports board on the back wall: one card per report, newest last — point at one to read it, click to open it on the time card
  const bx = 0.3, cols = Math.max(3, Math.floor((W - 2.3 - bx) / 0.42)), rowsN = 3, shown = ff.reps.slice(-cols * rowsN), first = ff.reps.length - shown.length;
  put(bx - 0.06, 0, 0.88, cols * 0.42 + 0.08, 0.03, rowsN * 0.5 + 0.1, mat(theme === 'dark' ? 0x7a6448 : 0xb89a6e), false); // cork
  shown.forEach((e, j) => { const i = first + j, c = j % cols, r = Math.floor(j / cols), p = punchOf(e[2]), m = new THREE.Mesh(PLANE, new THREE.MeshStandardMaterial({ map: repTex(i + 1, p), roughness: 0.9 }));
    m.scale.set(0.34, 0.42, 1); m.position.set(bx + c * 0.42 + 0.17, 0.93 + (rowsN - 1 - r) * 0.5 + 0.25, 0.045); g.add(m); const s2 = saidOf(e[4]);
    hot(m, { type: 'report', i, label: '#' + (i + 1) + ' · ' + PUNCHWORD[p] + (s2 ? ' — ' + (s2.length > 80 ? s2.slice(0, 78) + '…' : s2) : '') }); });
  if (!ff.reps.length) { const { tx, aspect } = paper(ff.F.on ? 'NO REPORTS YET' : 'NOT BUILT YET'), m = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: tx })); m.scale.set(0.24 * aspect, 0.24, 1); m.position.set(bx + cols * 0.21, 1.6, 0.05); g.add(m); }
  // on the left wall: the time card, the last commit's plaque, the score's rosette
  const card = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: cardTex(ff.punches, d.l) })); card.scale.set(0.8, 1.12, 1); card.rotation.y = Math.PI / 2; card.position.set(0.02, 1.55, 0.8); g.add(card); hot(card, { type: 'card', label: 'Time card · ' + ff.reps.length + ' punches' });
  if (ff.commit) { const { tx, aspect } = textTex(ff.commit.slice(0, 7), { color: '#3a2a0a', bg: '#c9a24a', px: 40 }), pl = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: tx })); pl.scale.set(0.28 * aspect, 0.28, 1); pl.rotation.y = Math.PI / 2; pl.position.set(0.02, 2.1, 1.9); g.add(pl); hot(pl, { type: 'commit', label: 'Last commit ' + ff.commit.slice(0, 7) }); }
  const gr = grade(d.sc); if (gr) { const ro = new THREE.Mesh(new THREE.CircleGeometry(0.2, 24), new THREE.MeshBasicMaterial({ color: gr === 'good' ? 0x6f9f68 : gr === 'ok' ? 0xc39a45 : 0xa9503e })); ro.rotation.y = Math.PI / 2; ro.position.set(0.02, 2.1, 2.55); g.add(ro); hot(ro, { type: 'score', label: 'Loop score ' + d.sc }); }
  // furniture by role
  const desks = ff.kind === 'parallel' ? Math.min(3, ff.ids.length) : 1, desk = mat(theme === 'dark' ? 0x8a6a4e : 0xa07a56), metal = mat(0x8a8d93, { metalness: 0.4 }), screen = new THREE.MeshBasicMaterial({ color: ff.live ? P.lit : theme === 'dark' ? 0x2a3444 : 0x55606e, toneMapped: false }), dy = Math.min(1.6, D * 0.36);
  let trayAt = null;
  if (ff.kind === 'manager') { const cw = Math.min(3.4, W - 2.2); put(1.0, dy, 0, cw, 0.7, 0.9, desk); put(0.95, dy - 0.05, 0.9, cw + 0.1, 0.8, 0.06, trim); // a long counter
    const mon = new THREE.Mesh(PLANE, screen); mon.scale.set(0.6, 0.4, 1); mon.position.set(1.0 + cw / 2, 1.25, dy + 0.3); g.add(mon); trayAt = [1.2, dy + 0.15, 0.96];
  } else if (ff.kind === 'input') { const ax = Math.min(1.8, W * 0.3); // a review room: armchair, reading lamp, a lectern
    put(ax, dy, 0, 1.1, 1.0, 0.45, mat(0x6d7f96)); put(ax, dy, 0.45, 1.1, 0.25, 0.6, mat(0x5d6f86)); put(ax, dy, 0.45, 0.2, 1.0, 0.3, mat(0x5d6f86)); put(ax + 0.9, dy, 0.45, 0.2, 1.0, 0.3, mat(0x5d6f86));
    put(ax + 1.6, dy + 0.3, 0, 0.06, 0.06, 1.5, metal); const shade = new THREE.Mesh(new THREE.ConeGeometry(0.22, 0.25, 16, 1, true), new THREE.MeshBasicMaterial({ color: ff.live ? P.lit : 0xd9d4c7, side: THREE.DoubleSide, toneMapped: false })); shade.position.set(ax + 1.63, 1.55, dy + 0.33); g.add(shade);
    put(0.7, dy - 0.8, 0, 0.1, 0.1, 1.0, desk); put(0.45, dy - 1.0, 1.0, 0.6, 0.5, 0.05, desk); trayAt = [0.5, dy - 0.95, 1.05];
  } else if (ff.kind === 'sublink') { put(1.5, dy - 0.2, 0, Math.min(3, W - 2.5), 1.4, 0.2, metal);
  } else for (let k = 0; k < desks; k++) { const dw = Math.min(1.6, (W - 2.6) / desks), dx = 0.6 + k * ((W - 2.2) / desks); // a desk, chair and monitor per agent
    put(dx, dy, 0.72, dw, 0.8, 0.06, desk); [[0.05, 0.05], [dw - 0.1, 0.05], [0.05, 0.7], [dw - 0.1, 0.7]].forEach(([a, b]) => put(dx + a, dy + b, 0, 0.05, 0.05, 0.72, metal, false));
    put(dx + dw / 2 - 0.25, dy + 1.0, 0, 0.5, 0.5, 0.45, mat(0x3a3b3f)); put(dx + dw / 2 - 0.25, dy + 1.4, 0.45, 0.5, 0.1, 0.5, mat(0x3a3b3f));
    put(dx + dw / 2 - 0.3, dy + 0.1, 0.78, 0.6, 0.05, 0.38, mat(0x2a2b2e)); const mon = new THREE.Mesh(PLANE, screen); mon.scale.set(0.54, 0.32, 1); mon.position.set(dx + dw / 2, 0.97, dy + 0.16); g.add(mon);
    if (!k) trayAt = [dx + 0.08, dy + 0.45, 0.78];
    if (ff.live) { const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.16, 0.4, 4, 10), mat(0x5f86e0)); body.position.set(dx + dw / 2, 0.78, dy + 1.15); g.add(body); const head = new THREE.Mesh(new THREE.SphereGeometry(0.13, 14, 10), mat(0xe8c9a8)); head.position.set(dx + dw / 2, 1.2, dy + 1.15); g.add(head); } }
  // the in-tray: one envelope per message from the manager
  if (trayAt && ff.directions.length) { const [tx0, ty0, tz0] = trayAt, tray = put(tx0, ty0, tz0, 0.4, 0.3, 0.05, mat(0x3a3b3f)); for (let i = 0; i < Math.min(5, ff.directions.length); i++) put(tx0 + 0.03, ty0 + 0.03, tz0 + 0.05 + i * 0.02, 0.34, 0.24, 0.015, mat(0xf4f1e8), false);
    hot(tray, { type: 'letters', label: ff.directions.length + ' message' + (ff.directions.length > 1 ? 's' : '') + ' from ' + d.m }); }
  // crates by the door, one per file it produced, each labelled
  ff.files.slice(0, 6).forEach((f, i) => { const [a, c2] = [[0, 0], [0.55, 0], [1.1, 0], [0.27, 0.5], [0.82, 0.5], [0.55, 1]][i], cx = W - 1.8 + a, cy = D - 0.8, cr = put(cx, cy, c2, 0.5, 0.5, 0.48, mat(theme === 'dark' ? 0x9a7b55 : 0xc9a87a));
    const nm = f.split('/').pop(), { tx, aspect } = paper(nm.length > 16 ? nm.slice(0, 15) + '…' : nm), lb = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: tx })), lw = Math.min(0.44, 0.1 * aspect); lb.scale.set(lw, lw / aspect, 1); lb.position.set(cx + 0.25, c2 + 0.3, cy + 0.505); g.add(lb);
    hot(cr, { type: 'file', name: f, label: 'File · ' + f }); });
  if (!ff.files.length) { const { tx, aspect } = textTex('NO OUTPUTS YET', { color: theme === 'light' ? '#43392a' : '#cfc9bd', px: 36 }), lb = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: tx, transparent: true })); lb.scale.set(0.3 * aspect, 0.3, 1); lb.rotation.x = -Math.PI / 2; lb.position.set(W - 1.4, 0.03, D - 0.5); g.add(lb); }
  if (ff.failed || ff.waiting) { const lp = new THREE.Mesh(new THREE.SphereGeometry(0.14, 16, 12), new THREE.MeshBasicMaterial({ color: ff.failed ? 0xa9503e : 0xcf7d3c, toneMapped: false })); lp.position.set(0.3, 2.35, 0.3); g.add(lp); hot(lp, { type: 'status', label: ff.failed ? 'The last report failed' : 'Waiting on you' }); }
  put(W - 0.55, 0.3, 0, 0.36, 0.36, 0.4, mat(0xb8603e)); const leaf = new THREE.Mesh(new THREE.SphereGeometry(0.34, 10, 8), mat(0x5b8a52)); leaf.position.set(W - 0.37, 0.75, 0.48); g.add(leaf); // a plant
  const { tx, aspect } = textTex((ff.kind === 'manager' ? 'STOREFRONT' : ff.kind === 'input' ? 'REVIEW ROOM' : 'OFFICE FLOOR') + ' · ' + ff.ids.join(' + ').toUpperCase(), { color: '#211a10', bg: '#f4ecd6', px: 30 }), strip = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: tx }));
  strip.scale.set(0.24 * aspect, 0.24, 1); strip.position.set(0.24 * aspect / 2 + 0.1, -0.1, D + 0.005); g.add(strip);
  return { g, ff };
}
function roomAct(info) { // a click on something in the room opens it in the panel
  const p = $('panel'), go = n => { if (!n) return; n.scrollIntoView({ block: 'center', behavior: 'smooth' }); n.classList.remove('flash'); void n.offsetWidth; n.classList.add('flash'); };
  if (info.type === 'report') { const r = p.querySelector(`.tcrow[data-i="${info.i}"]`); if (r) { const t2 = r.querySelector('.tctext'); if (t2 && !t2.classList.contains('open')) { t2.classList.add('open'); const b = t2.querySelector('button.more'); if (b) { b.textContent = 'less'; b.setAttribute('aria-expanded', 'true'); } } } go(r); }
  else if (info.type === 'card') go(p.querySelector('.timecard'));
  else if (info.type === 'file') go(p.querySelector(`.chips code[data-f="${CSS.escape(info.name)}"]`) || p.querySelector('.chips'));
  else if (info.type === 'letters') go(p.querySelector('.thread'));
  else go(p.querySelector('.fvstats'));
}
// ─── focus: when a building is open, the others fade to ghosts; inside a floor they fade further ───
const GHOSTM = {};
const ghostMat = op => GHOSTM[theme + op] || (GHOSTM[theme + op] = new THREE.MeshLambertMaterial({ color: theme === 'dark' ? 0x55575c : 0xd6d1c5, transparent: true, opacity: op, depthWrite: false }));
function ghost(B, op) { if ((B.ghost ?? null) === op) return; B.ghost = op;
  B.group.traverse(o => { if (o.isCSS2DObject) { o.element.style.opacity = op == null ? '' : '0.3'; return; } if (!o.isMesh) return;
    if (op == null) { if (o.userData.m0) { o.material = o.userData.m0; o.castShadow = o.userData.cs0; delete o.userData.m0; } return; }
    if (!o.userData.m0) { o.userData.m0 = o.material; o.userData.cs0 = o.castShadow; } if (o.userData.m0.visible === false) return; o.material = ghostMat(op); o.castShadow = false; });
}
function focus(B, mode) { buildings.forEach(o => ghost(o, !B || o === B ? null : mode === 'floor' ? 0.1 : 0.3)); requestRender(); }
function fillFloorPanel(d, B, fi, ff, side, card) {
  const status = ff.waiting ? ['Waiting on you', 'need'] : ff.failed ? ['Failed', 'fail'] : ff.live ? ['On shift', 'run'] : !ff.F.on ? ['Not built yet', 'done'] : stateOf(d) === 'run' ? ['Idle', 'done'] : ['Off shift', 'done'];
  const head = el('div', null, 'fvhead'), who = el('div', ff.ids.join(' + '), 'mono'); head.append(who, el('span', '· ' + d.n, 'dim'));
  const pill = el('span', status[0], 'pill'); pill.style.color = `var(--s-${status[1] === 'done' ? 'done' : status[1]})`;
  const stats = el('div', null, 'fvstats'), stat = (k, v) => { const s = el('div'); s.append(el('small', k), el('b', v)); stats.append(s); };
  stat('Role', ff.kind === 'manager' ? 'Manager' : ff.kind === 'input' ? 'Reviewer (input)' : ff.kind === 'parallel' ? 'Workers in parallel' : ff.kind === 'sublink' ? 'Sub-loops' : 'Worker');
  stat('Turns', (ff.turns || ff.reps.length) + (d.l ? ' · loop ' + (d.u || 0) + ' of ' + d.l : ''));
  stat('Loop budget', stateOf(d) === 'run' || stateOf(d) === 'need' ? Math.max(0, (d.l || 0) - (d.u || 0)) + ' turns left' : 'used ' + (d.u || 0) + ' of ' + (d.l || '–'));
  stat('Outputs', String(ff.files.length)); stat('Commit', ff.commit ? ff.commit.slice(0, 7) : '—'); stat('Retries', String(ff.retries));
  side.append(head, pill, stats);
  if (ff.waiting && d.q) { const ab = askBox(d, false); ab.querySelectorAll('button').forEach(b => b.addEventListener('click', () => closeFloorView())); side.append(ab); }
  if (ff.last && saidOf(ff.last[4])) side.append(el('blockquote', 'Last said: “' + saidOf(ff.last[4]).slice(0, 220) + (saidOf(ff.last[4]).length > 220 ? '…' : '') + '”'));
  if (ff.files.length) { const fl = el('div', null, 'chips'); ff.files.slice(0, 8).forEach(f => { const c3 = el('code', f.split('/').pop()); c3.dataset.f = f; fl.append(c3); }); if (ff.files.length > 8) fl.append(el('code', '+' + (ff.files.length - 8))); side.append(el('h3', 'Outputs · ' + ff.files.length), fl); }
  if (ff.directions.length) { const th = el('div', null, 'thread'); ff.directions.forEach(e => { const m = el('div'); m.append(el('small', d.m + ' → ' + ff.ids.join('+') + ' · ' + new Date(e[0] * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })), el('p', saidOf(e[4]).slice(0, 260))); th.append(m); }); side.append(el('h3', 'From the manager'), th); }
  const nav = el('div', null, 'nav'), dn = el('button', '↓ Floor below'), up = el('button', 'Floor above ↑'), bk = el('button', 'Back to the building'); [dn, up, bk].forEach(b => b.type = 'button');
  dn.disabled = fi === 0; up.disabled = fi >= B.floors.length - 1; dn.onclick = () => openFloorView(d, B, fi - 1); up.onclick = () => openFloorView(d, B, fi + 1); bk.onclick = () => { closeFloorView(); show({ loop: d, B }); };
  side.append(nav); nav.append(dn, up, bk);
  // the time card: punches across the top, one row per report, newest first
  const hd = el('div', null, 'tchead'); hd.append(el('span', 'TIME CARD'), el('span', ff.reps.length + ' TURNS' + (d.l ? ' · LOOP LIMIT ' + d.l : '')));
  const meta = el('div', null, 'tcmeta'); meta.append(el('span', 'NAME'), el('b', ff.ids.join(' + ')), el('span', 'LOOP'), el('b', d.n));
  const punches = el('div', null, 'tcpunch'), slots = Math.max(ff.punches.length, Math.min(24, d.l || 12)); for (let i = 0; i < slots; i++) punches.append(el('i', null, ff.punches[i] || 'empty'));
  const rows = el('div', null, 'tcrows');
  if (!ff.reps.length) rows.append(el('p', ff.F.on ? 'No reports from this floor in the snapshot.' : 'This floor is still a steel frame: the agent hasn’t had a turn yet.', 'tcempty'));
  ff.reps.slice().reverse().forEach((e, j) => { const n = ff.reps.length - j, p = punchOf(e[2]), r = el('div', null, 'tcrow'), note = saidOf(e[4]);
    const txt = el('div', null, 'tctext'), st = el('span', PUNCHWORD[p], 'w ' + p); txt.append(st, el('span', ' ' + (note || e[2].replace(/_/g, ' ')), 'note2'));
    const meta2 = el('div', null, 'tcsub'); meta2.append(el('span', new Date(e[0] * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }))); if (commitOf(e[4])) meta2.append(el('code', 'commit ' + commitOf(e[4]).slice(0, 7))); artifactsOf(e[4]).slice(0, 2).forEach(a => meta2.append(el('code', a.split('/').pop())));
    txt.append(meta2); if (note.length > 140) { const mb = el('button', 'more', 'more'); mb.type = 'button'; mb.setAttribute('aria-expanded', 'false'); mb.onclick = () => { const o = txt.classList.toggle('open'); mb.textContent = o ? 'less' : 'more'; mb.setAttribute('aria-expanded', String(o)); }; txt.append(mb); }
    r.dataset.i = String(n - 1); r.append(el('span', String(n), 'num'), el('i', null, p), txt); rows.append(r); });
  card.append(hd, meta, punches, rows);
}
// ─── floor view: fly in. The building splits apart, the floor opens up with its room built inside, the camera flies in;
// the facts and the time card go in the side panel. ↑/↓ moves between floors, Esc flies back out ───
let TWEENS = [];
const tween = (dur, fn, done) => TWEENS.push({ t0: performance.now(), dur, fn, done });
function stepTweens(t) { if (!TWEENS.length) return false; TWEENS = TWEENS.filter(w => { const k = Math.min(1, (t - w.t0) / w.dur), e = 1 - Math.pow(1 - k, 3); w.fn(e); if (k >= 1) { if (w.done) w.done(); return false; } return true; }); return true; }
function placeFloors(B, f) { const L = B.lift; B.floors.forEach((F, i) => { F.g.position.y = i * GAP_EX * f + (L && i > L.fi ? L.v : 0); }); B.roof.position.y = B.floors.length * GAP_EX * f + (L ? L.v : 0); }
function floorHeight(B, fi) { const F = B.floors[fi], nx = B.floors[fi + 1]; return (nx ? nx.z0 : B.topZ) - F.z0; }
function enterRoom(d, B, fi) { // build the room inside the floor and lift everything above it out of the way
  const F = B.floors[fi], fp = B.fp, off = camera.position.clone().sub(controls.target), az = Math.atan2(off.x, off.z), q = ((Math.round((az - Math.PI / 4) / (Math.PI / 2)) % 4) + 4) % 4;
  const sw = (q % 2 ? fp.FD : fp.FW) + 0.12, sd = (q % 2 ? fp.FW : fp.FD) + 0.12, s = Math.min(sw, sd) / 4.2, RW = sw / s, RD = sd / s, roomH = 2.8 * s; // the room fills the floor, open sides toward the camera
  const { g, ff } = buildRoom(d, B, fi, RW, RD), pivot = new THREE.Group(); g.position.set(-RW / 2, 0, -RD / 2); pivot.add(g); pivot.scale.setScalar(s); pivot.rotation.y = q * Math.PI / 2;
  pivot.position.set(fp.fx + fp.FW / 2, F.z0 + 0.2 * s, fp.fy + fp.FD / 2); F.g.add(pivot);
  F.solid.visible = false; if (F.fm) F.fm.visible = false;
  const lift = Math.max(0, roomH * 2.4 + 0.4 - floorHeight(B, fi) - GAP_EX), from = B.lift ? B.lift.v : 0; B.lift = { fi, v: from };
  tween(520, e => { B.lift.v = from + (lift - from) * e; placeFloors(B, 1); });
  const behind = new THREE.Vector3(off.x, 0, off.z).normalize(); // hide the buildings standing between the camera and this one
  focus(B, 'floor'); buildings.forEach(o => { if (o === B) return; const v = o.center.clone().sub(B.center); o.group.visible = !(v.dot(behind) > 0.3 && v.length() < 14); });
  const mid = new THREE.Vector3(B.center.x, F.z0 + roomH * 0.7, B.center.z), span = Math.max(fp.FW, fp.FD) * 0.7 + roomH * 0.5;
  flyTo(mid, Math.min(controls.maxZoom, (camera.top - camera.bottom) / (span / 0.85)));
  return { pivot, ff };
}
function leaveRoom(keepLift) { if (!FV) return; const { B, fi, pivot } = FV, F = B.floors[fi]; pivot.removeFromParent(); F.solid.visible = F.on; if (F.fm) F.fm.visible = !F.on;
  if (!keepLift && B.lift) { const from = B.lift.v; tween(420, e => { B.lift.v = from * (1 - e); placeFloors(B, exploded === B ? 1 : 0); }, () => { B.lift = null; }); } }
function openFloorView(d, B, fi) {
  if (!B || !B.floors[fi]) return; if (exploded !== B) explode(B);
  if (FV) leaveRoom(true);
  const { pivot, ff } = enterRoom(d, B, fi); FV = { d, B, fi, pivot, ff };
  const p = panelShell(); p.classList.add('floor'); const side = el('div', null, 'fvside'), card = el('div', null, 'timecard'); p.append(side, card); fillFloorPanel(d, B, fi, ff, side, card);
  if (B.labels) B.labels.forEach((o, i) => { o.element.classList.toggle('on', i === fi); o.visible = false; }); // the panel names the floor; tags would sit on the room
}
function closeFloorView(toBuilding = true) { if (!FV) return; const { d, B } = FV; leaveRoom(false); FV = null; $('panel').classList.remove('floor');
  buildings.forEach(o => { o.group.visible = true; }); ROOMPICK = []; focus(exploded, 'building'); if (typeof applyTime === 'function') applyTime();
  if (B.labels) B.labels.forEach(o => { o.element.classList.remove('on'); o.visible = true; });
  if (toBuilding) { flyTo(B.center, 2.6); show({ loop: d, B }); } }
onWin('keydown', e => { if (!FV || /INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) return;
  if (e.key === 'Escape') { closeFloorView(); e.stopImmediatePropagation(); } else if (e.key === 'ArrowUp' && FV.fi < FV.B.floors.length - 1) { openFloorView(FV.d, FV.B, FV.fi + 1); e.preventDefault(); e.stopImmediatePropagation(); } else if (e.key === 'ArrowDown' && FV.fi > 0) { openFloorView(FV.d, FV.B, FV.fi - 1); e.preventDefault(); e.stopImmediatePropagation(); } }, true);
// ─── styles: one per kind of work. Geometry, not colour — facades, lobbies and crowns. Every style keeps the same grammar:
// workers get vertical windows and reviewers horizontal ones; a crane = building now, a billboard = needs you; colour = how it went ───
const glow = (hex, op = 1) => new THREE.MeshBasicMaterial({ color: hex, toneMapped: false, transparent: op < 1, opacity: op, depthWrite: op >= 1 });
const faces = (x, y, z, w, dd, o = 0.006) => [[w, (u, v) => [x + u, z + v, y + dd + o], 0], [w, (u, v) => [x + w - u, z + v, y - o], Math.PI], [dd, (u, v) => [x + w + o, z + v, y + dd - u], Math.PI / 2], [dd, (u, v) => [x - o, z + v, y + u], -Math.PI / 2]];
const litAt = (c, i, rw, fi) => c.litMode === 'all' ? c.P.lit : c.litMode === 'none' ? c.P.dark : ((c.seedBase + i * 3 + rw + fi + c.idx) % 3 === 0 ? c.P.lit : c.P.dark);
const mesh = (parent, geo, m, x, y, z, sx = 1, sy = 1, sz = 1, cast = true) => { const o = new THREE.Mesh(geo, m); o.scale.set(sx, sy, sz); o.position.set(x, y, z); o.castShadow = cast; parent.add(o); return o; };
const CYL = new THREE.CylinderGeometry(0.5, 0.5, 1, 14), CONE = new THREE.ConeGeometry(0.5, 1, 14), SPH = new THREE.SphereGeometry(0.5, 14, 10);
const hashOf = s => [...s].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 9973, 7);
const rowsOf = h => Math.max(1, Math.round((h - 0.06) / ROWH));
const words = n => n.split(/[-_]/).filter(w => w && !/^\d+$/.test(w));
function ribbons(c, pad = 0.12, hk = 0.42) { const rows = rowsOf(c.h); faces(c.x, c.y, c.z, c.w, c.dd).forEach(([W, f, rot], fi) => { for (let rw = 0; rw < rows; rw++) { const p = f(W / 2, 0.03 + (rw + 0.5) * ROWH); c.F.wins.push({ x: p[0], y: p[1], z: p[2], w: W - pad, h: ROWH * hk, rot, c: litAt(c, rw, rw, fi) }); } }); }
function neonTex(text, color) { // a vertical sign: glowing letters stacked top to bottom
  const ws = text.toUpperCase().split(/[^A-Z0-9]+/).filter(Boolean); let t = ws.join(' '); while (t.length > 12 && ws.length > 1) { ws.pop(); t = ws.join(' '); } const chars = [...t.slice(0, 12)], cv = document.createElement('canvas'), c = cv.getContext('2d'); cv.width = 64; cv.height = 64 * Math.max(3, chars.length) + 16;
  c.fillStyle = '#0b0c14'; c.fillRect(0, 0, cv.width, cv.height); c.strokeStyle = color; c.lineWidth = 4; c.strokeRect(3, 3, cv.width - 6, cv.height - 6);
  c.font = '700 46px Inter, sans-serif'; c.textAlign = 'center'; c.textBaseline = 'middle'; c.shadowColor = color; c.shadowBlur = 12; c.fillStyle = color; chars.forEach((ch, i) => ch !== ' ' && c.fillText(ch, 32, 40 + i * 64));
  const tx = new THREE.CanvasTexture(cv); tx.colorSpace = THREE.SRGBColorSpace; return { tx, aspect: cv.width / cv.height };
}
// Lay `text` out on one or two lines (split at the most even word break) at the largest size up to `max`px that fits
// w×h; returns [line, y offset from the middle, px] rows.
function fitLines(g, text, w, h, font, max) {
  const ws = text.split(/\s+/).filter(Boolean), opts = [[text]];
  for (let i = 1; i < ws.length; i++) opts.push([ws.slice(0, i).join(' '), ws.slice(i).join(' ')]);
  let best = null;
  for (const lines of opts) { g.font = font.replace('%', max); const widest = Math.max(...lines.map(l => g.measureText(l).width));
    const px = Math.floor(Math.min(max, max * w / widest, h / (lines.length * 1.1))); if (!best || px > best.px) best = { lines, px }; }
  const lh = best.px * 1.05; return best.lines.map((l, i) => [l, (i - (best.lines.length - 1) / 2) * lh, best.px]); }
// ad screens for the Visual style: a gradient, a few shapes and a word from the loop's name (no reds or greens — those mean results)
const ADC = [['#ff3d9a', '#ffb347'], ['#33c8ff', '#7a5cff'], ['#ffe14d', '#ff8a3d'], ['#b56bff', '#33c8ff'], ['#fff4dc', '#ff3d9a']], adCache = {};
function adTex(word, seed) { const k = word + '|' + seed; if (adCache[k]) return adCache[k];
  const [a, b] = ADC[seed % ADC.length], cv = document.createElement('canvas'), g = cv.getContext('2d'); cv.width = 256; cv.height = 160;
  const gr = g.createLinearGradient(0, 0, 256, 160); gr.addColorStop(0, a); gr.addColorStop(1, b); g.fillStyle = gr; g.fillRect(0, 0, 256, 160);
  g.fillStyle = 'rgba(255,255,255,.2)'; for (let i = 0; i < 5; i++) { g.beginPath(); g.arc((seed * 37 + i * 61) % 256, (seed * 17 + i * 43) % 160, 16 + (i * 9) % 30, 0, 7); g.fill(); }
  g.fillStyle = '#0b0c14'; g.textAlign = 'center'; g.textBaseline = 'middle'; fitLines(g, word.toUpperCase().replace(/[-_]+/g, ' '), 232, 132, '900 %px Inter, sans-serif', 46).forEach(([t, y, px]) => { g.font = '900 ' + px + 'px Inter, sans-serif'; g.fillText(t, 128, 80 + y); });
  const tx = new THREE.CanvasTexture(cv); tx.colorSpace = THREE.SRGBColorSpace; return (adCache[k] = tx); }
function tickerTex(text) { const cv = document.createElement('canvas'), g = cv.getContext('2d'); cv.width = 1024; cv.height = 32; g.fillStyle = '#0b0c14'; g.fillRect(0, 0, 1024, 32);
  g.fillStyle = '#ffe9a8'; g.font = '700 22px Inter, sans-serif'; g.textBaseline = 'middle'; const s = ('  ' + text.toUpperCase() + '  ·').repeat(8); g.fillText(s, 0, 17);
  const tx = new THREE.CanvasTexture(cv); tx.colorSpace = THREE.SRGBColorSpace; tx.wrapS = THREE.RepeatWrapping; return tx; }
const nameOf = d => { const w = words(d.n); let s = w.join(' ').toUpperCase(); while (s.length > 18 && w.length > 1) { w.pop(); s = w.join(' ').toUpperCase(); } return s.slice(0, 18); };
function nameSign(parent, text, o, x, y, z, h, maxW, rot = 0, lit = false) { // a building's name, lettered in its own style
  const { tx, aspect } = textTex(text, { color: o.color, bg: o.bg || null, px: o.px || 48, font: o.font, pad: o.pad ?? 10 }); let w = h * aspect; if (w > maxW) { w = maxW; h = w / aspect; }
  const opts = { map: tx, alphaTest: o.bg ? 0 : 0.3 }, m = new THREE.Mesh(PLANE, lit ? new THREE.MeshBasicMaterial(Object.assign(opts, { toneMapped: false })) : new THREE.MeshStandardMaterial(Object.assign(opts, { roughness: 0.85 })));
  m.scale.set(w, h, 1); m.position.set(x, y, z); m.rotation.y = rot; parent.add(m); return m; }
const lightTrim = () => theme === 'dark' ? 0xc9d2d6 : 0xf4f6f5;

// ─── how it went, as things on the roof. Each style has its own set for clean / good / mixed / bad, and the set carries the
// palette through what it is made of: plants are green, water is blue, tarps and dry grass are ochre, rust is red-brown ───
const RC = { leaf: 0x5f9e4c, leaf2: 0x78ad62, water: 0x4f8fc0, deck: 0xe6dfcf, ochre: 0xc9a045, dry: 0xb3985e, rust: 0x8a4a32, rust2: 0xa9503e, soot: 0x3a3530, grey: 0x8a8d93, copper: 0x6fa38a, white: 0xf1ede2 };
const cm = h => mat(theme === 'dark' ? new THREE.Color(h).multiplyScalar(0.8).getHex() : h);
const tierOf = s => s.finished ? outcomeOf(s.d) : null;
const sub = (r, a, b, c2, d2, dz = 0) => ({ x: r.x + r.w * a, y: r.y + r.d * b, w: r.w * (c2 - a), d: r.d * (d2 - b), z: r.z + dz });
const lean = (o, rx, rz) => { o.rotation.x = rx; o.rotation.z = rz; return o; };
const D = {
  garden(p, q, trees = 3) { box(q.x, q.y, q.z, q.w, q.d, 0.04, cm(RC.leaf2), false, p);
    [[q.x, q.y, q.w, 0.08], [q.x, q.y + q.d - 0.08, q.w, 0.08]].forEach(([a, b, w, d]) => box(a, b, q.z, w, d, 0.1, cm(RC.leaf), true, p)); // hedges
    for (let i = 0; i < trees; i++) { box(q.x + q.w * (i + 0.5) / trees - 0.02, q.y + q.d * (0.35 + (i % 2) * 0.3) - 0.02, q.z, 0.04, 0.04, 0.16, cm(0x7a5c3e), true, p); mesh(p, TREE, cm(RC.leaf), q.x + q.w * (i + 0.5) / trees, q.z + 0.28, q.y + q.d * (0.35 + (i % 2) * 0.3), 0.28, 0.3, 0.28); } },
  pool(p, q) { box(q.x, q.y, q.z, q.w, q.d, 0.04, cm(RC.deck), false, p); box(q.x + q.w * 0.12, q.y + q.d * 0.14, q.z + 0.01, q.w * 0.76, q.d * 0.54, 0.04, mat(RC.water, { roughness: 0.1, metalness: 0.1 }), false, p);
    for (let i = 0; i < 3; i++) box(q.x + q.w * (0.15 + i * 0.26), q.y + q.d * 0.76, q.z + 0.04, q.w * 0.14, q.d * 0.16, 0.03, cm(RC.white), false, p); }, // loungers
  solar(p, q) { const l = []; for (let u = 0.04; u < q.w - 0.2; u += 0.26) for (let v = 0.04; v < q.d - 0.18; v += 0.24) l.push([q.x + u, q.y + v, q.z + 0.05, 0.22, 0.2, 0.025]); if (l.length) instanced(l, mat(0x2f4a7a, { metalness: 0.5, roughness: 0.3 }), p); },
  tarps(p, q, seed) { for (let i = 0; i < 3; i++) lean(mesh(p, BOX, cm(i % 2 ? RC.ochre : RC.dry), q.x + q.w * (0.2 + i * 0.3), q.z + 0.07, q.y + q.d * (0.3 + ((seed + i) % 3) * 0.2), q.w * 0.24, 0.12, q.d * 0.28), 0.08 * (i - 1), 0.1 * ((seed + i) % 3 - 1)); },
  patches(p, q, seed) { for (let i = 0; i < 3; i++) box(q.x + q.w * (0.05 + ((seed + i * 3) % 5) * 0.16), q.y + q.d * (0.1 + i * 0.28), q.z, q.w * 0.28, q.d * 0.22, 0.012, cm(i % 2 ? RC.ochre : RC.dry), false, p); }, // tar-paper patches
  vents(p, q, n = 3) { for (let i = 0; i < n; i++) box(q.x + q.w * (0.1 + i * 0.3), q.y + q.d * 0.2, q.z, Math.min(0.26, q.w * 0.2), Math.min(0.2, q.d * 0.3), 0.14, cm(RC.grey), true, p); },
  rust(p, q, seed) { for (let i = 0; i < 4; i++) lean(mesh(p, BOX, cm(i % 2 ? RC.rust : RC.rust2), q.x + q.w * (0.15 + i * 0.22), q.z + 0.07, q.y + q.d * (0.25 + ((seed + i) % 3) * 0.22), 0.16 + (i % 2) * 0.1, 0.12, 0.14), 0.2 * ((seed + i) % 2), 0.25 * ((i % 3) - 1));
    mesh(p, CYL, cm(RC.rust), q.x + q.w * 0.8, q.z + 0.1, q.y + q.d * 0.7, 0.14, 0.2, 0.14); }, // scrap and a rusty drum
  mast(p, x, y, z, broken) { const o = mesh(p, CYL, cm(broken ? RC.rust : RC.grey), x, z + 0.35, y, 0.03, 0.7, 0.03); if (broken) lean(o, 0.5, 0.35); else mesh(p, BOX, cm(RC.grey), x, z + 0.55, y, 0.3, 0.02, 0.02); },
  streaks(p, s, n = 3) { for (let i = 0; i < n; i++) { const h = 0.3 + (i % 2) * 0.25, m = new THREE.Mesh(PLANE, cm(RC.rust)); m.scale.set(0.05, h, 1); m.position.set(s.fx + s.FW * (0.2 + i * 0.3), s.z - h / 2 - 0.02, s.fy + s.FD + 0.016); p.add(m); } }, // rust running down from the roof
  umbrellas(p, q, n = 3) { for (let i = 0; i < n; i++) { const x = q.x + q.w * (i + 0.5) / n, y = q.y + q.d * 0.5; mesh(p, CYL, cm(RC.grey), x, q.z + 0.15, y, 0.02, 0.3, 0.02); mesh(p, CONE, cm(i % 2 ? RC.leaf2 : RC.white), x, q.z + 0.33, y, 0.36, 0.1, 0.36); } },
  crates(p, q, n, mode, seed) { // neat stacks, a messy heap, or spilled
    if (mode === 'spilled') { for (let i = 0; i < Math.min(n, 8); i++) { const o = mesh(p, BOX, cm([0xc9b27a, 0xa98a6a][i % 2]), q.x + q.w * ((seed * 7 + i * 37) % 100) / 100, q.z + 0.06, q.y + q.d * ((seed * 3 + i * 53) % 100) / 100, 0.13, 0.12, 0.13); o.rotation.set(0.4 * ((i % 3) - 1), (i * 0.9) % 3, 0.5 * ((i % 2) - 0.5)); } return; }
    const l = []; for (let i = 0; i < n; i++) { const j = mode === 'messy' ? (((seed + i * 13) % 7) - 3) * 0.02 : 0, it = [q.x + (i % 3) * 0.15 + j, q.y + (Math.floor(i / 3) % 3) * 0.15 - j, q.z + Math.floor(i / 9) * 0.13, 0.13, 0.13, 0.12]; it.c = [0xc9b27a, 0xa98a6a, 0xd9c9a0][i % 3]; l.push(it); }
    if (l.length) instanced(l, mat(0xffffff), p); },
};

// ── Frontend · glass offices: full-height glass between slim white fins, planted ledges, a recessed lobby, a pergola roof garden
Object.assign(SKINS.glass, {
  facade(c) { if (!c.on || c.kind === 'manager') return; const fin = lightTrim(), rows = rowsOf(c.h), bh = (c.h - 0.04) / rows;
    faces(c.x, c.y, c.z, c.w, c.dd).forEach(([W, f, rot], fi) => {
      if (c.kind === 'input') { for (let rw = 0; rw < rows; rw++) { const p = f(W / 2, 0.02 + (rw + 0.5) * bh); c.F.wins.push({ x: p[0], y: p[1], z: p[2], w: W - 0.02, h: bh * 0.6, rot, c: litAt(c, rw, rw, fi) }); // horizontal glass bands
          const q = f(W / 2, 0.02 + rw * bh + bh * 0.9); c.F.wins.push({ x: q[0], y: q[1], z: q[2] + 0.001, w: W, h: bh * 0.2, rot, c: fin }); } return; } // with white spandrels
      const n = Math.max(2, Math.round(W / 0.22)), mu = W / n;
      for (let i = 0; i < n; i++) { const p = f(i * mu + mu / 2, c.h / 2); c.F.wins.push({ x: p[0], y: p[1], z: p[2], w: mu - 0.035, h: c.h - 0.05, rot, c: litAt(c, i, 0, fi) }); } // full-height glass
      for (let i = 0; i <= n; i++) { const p = f(i * mu, c.h / 2); c.F.wins.push({ x: p[0], y: p[1], z: p[2] + 0.001, w: 0.03, h: c.h, rot, c: fin }); } }); // slim white fins
    if (c.idx % 2 === 0) { c.add(c.x - 0.02, c.y + c.dd, c.z + c.h - 0.06, c.w + 0.04, 0.1, 0.06, mat(fin), false); // a planted ledge every other floor
      for (let u = 0.15; u < c.w - 0.05; u += 0.3) mesh(c.F.solid, SPH, mat(theme === 'dark' ? 0x3f6a45 : 0x6fa35f), c.x + u, c.z + c.h + 0.01, c.y + c.dd + 0.05, 0.2, 0.1, 0.1, false); }
    return false; },
  base(c) { if (c.baseMesh) c.baseMesh.scale.set(c.w - 0.36, c.h, c.dd - 0.36); // the tower floats over a recessed glass lobby
    c.F.wins.push({ x: c.x + c.w / 2, y: c.z + c.h * 0.45, z: c.y + c.dd - 0.175, w: c.w - 0.4, h: c.h * 0.86, rot: 0, c: c.litMode === 'none' ? c.P.dark : c.P.lit });
    c.add(c.x - 0.1, c.y - 0.1, c.z + c.h - 0.05, c.w + 0.2, c.dd + 0.2, 0.05, mat(lightTrim()), true); // a thin white canopy
    [0.18, c.w - 0.18].forEach(u => { c.add(c.x + u - 0.12, c.y + c.dd + 0.06, c.z, 0.24, 0.24, 0.1, mat(0xd9d6cf), false); mesh(c.F.solid, TREE, mat(0x5f9e4c), c.x + u, c.z + 0.3, c.y + c.dd + 0.18, 0.3, 0.36, 0.3); }); },
  crown(s) { const r = s.roof, t = tierOf(s), white = mat(lightTrim()), p = s.B.roof; // a pergola, and under it: a garden, a pool, solar panels, or a torn awning
    nameSign(p, nameOf(s.d), { font: '300 %px Inter, sans-serif', color: theme === 'dark' ? '#eef4f6' : '#23292e', px: 96, pad: 4 }, r.x + r.w / 2, r.z + 0.17, r.y + r.d + 0.03, 0.3, r.w * 0.92); // slim letters standing on the roof edge
    [[0, 0], [1, 0], [1, 1], [0, 1]].forEach(([a, b]) => box(r.x + 0.08 + a * (r.w - 0.2), r.y + 0.08 + b * (r.d - 0.2), r.z, 0.04, 0.04, 0.34, white, true, p));
    const slats = (to = 1) => { for (let u = 0.08; u < (r.w - 0.08) * to; u += 0.16) box(r.x + u, r.y + 0.08, r.z + 0.34, 0.03, r.d - 0.16, 0.03, white, false, p); };
    if (t === 'great') { slats(); D.garden(p, sub(r, 0.06, 0.06, 0.94, 0.94), 4); }
    else if (t === 'good') { slats(0.45); D.pool(p, sub(r, 0.08, 0.1, 0.92, 0.9)); }
    else if (t === 'mixed') { slats(0.45); box(r.x, r.y, r.z, r.w, r.d, 0.02, cm(0xb9b3a8), false, p); D.solar(p, sub(r, 0.05, 0.1, 0.6, 0.9, 0.02));
      [0.7, 0.85].forEach(a => { box(r.x + r.w * a, r.y + r.d * 0.2, r.z, 0.14, 0.14, 0.1, cm(RC.white), false, p); mesh(p, SPH, cm(RC.dry), r.x + r.w * a + 0.07, r.z + 0.13, r.y + r.d * 0.2 + 0.07, 0.14, 0.07, 0.14, false); }); } // dry planters
    else if (t === 'bad') { [0.2, 0.55].forEach((a, i) => lean(mesh(p, BOX, cm(RC.rust2), r.x + r.w * a, r.z + 0.24, r.y + r.d * 0.5, 0.3, 0.02, r.d * 0.4), 0.55 * (i ? 1 : -1), 0.3)); // a torn awning hanging off the frame
      [0.3, 0.7].forEach(a => { box(r.x + r.w * a, r.y + r.d * 0.75, r.z, 0.16, 0.16, 0.1, cm(RC.soot), false, p); mesh(p, SPH, cm(RC.rust), r.x + r.w * a + 0.08, r.z + 0.13, r.y + r.d * 0.75 + 0.08, 0.12, 0.05, 0.12, false); }); } // dead planters
    else slats(); },
});

// ── Backend · a contemporary office, not all glass: a warm precast concrete grid with dark glass set deep into it, sunshade
// blades on reviewers' floors, a double-height glass lobby, a louvred screen round the roof plant, the name on a metal band
Object.assign(SKINS.stone, {
  facade(c) { if (!c.on || c.kind === 'manager') return; const rows = rowsOf(c.h), bh = (c.h - 0.05) / rows, frame = c.colr, metal = theme === 'dark' ? 0x2a2c30 : 0x3d4046;
    faces(c.x, c.y, c.z, c.w, c.dd).forEach(([W, f, rot], fi) => { const street = fi === 0 || fi === 2;
      if (c.kind === 'input') { for (let rw = 0; rw < rows; rw++) { const v = 0.03 + (rw + 0.5) * bh, p = f(W / 2, v); c.F.wins.push({ x: p[0], y: p[1], z: p[2], w: W - 0.12, h: bh * 0.5, rot, c: litAt(c, rw, rw, fi) }); // a wide window band
          if (street) [0.34, 0.12].forEach(dv => { const q = f(W / 2, v + bh * dv); c.F.trim.push({ x: q[0], y: q[1], z: q[2] + 0.02, w: W, h: 0.025, rot, c: metal }); }); } // under sunshade blades
        return; }
      const n = Math.max(2, Math.round(W / 0.36)), mu = W / n; for (let i = 0; i < n; i++) { const p = f(i * mu + mu / 2, c.h / 2); c.F.wins.push({ x: p[0], y: p[1], z: p[2], w: mu - 0.12, h: c.h - 0.1, rot, c: litAt(c, i, 0, fi) }); } // a tall recessed bay
      if (fi === 0) for (let i = 0; i <= n; i++) c.F.blocks.push(Object.assign([c.x + i * mu - 0.05, c.y + c.dd - 0.01, c.z, 0.1, 0.09, c.h], { c: frame })); // the concrete grid's piers
      if (fi === 2) for (let i = 0; i <= n; i++) c.F.blocks.push(Object.assign([c.x + c.w - 0.01, c.y + c.dd - i * mu - 0.05, c.z, 0.09, 0.1, c.h], { c: frame }));
    });
    c.add(c.x - 0.05, c.y - 0.05, c.z + c.h - 0.05, c.w + 0.1, c.dd + 0.1, 0.05, mat(frame), true); // and its slab edge at every floor
    return false; },
  base(c) { if (c.baseMesh) c.baseMesh.scale.set(c.w - 0.3, c.h, c.dd - 0.3); const metal = mat(theme === 'dark' ? 0x2a2c30 : 0x3d4046), lit = c.litMode === 'none' ? c.P.dark : c.P.lit; // a recessed double-height lobby
    c.F.wins.push({ x: c.x + c.w / 2, y: c.z + c.h * 0.46, z: c.y + c.dd - 0.145, w: c.w - 0.34, h: c.h * 0.88, rot: 0, c: lit }); c.F.wins.push({ x: c.x + c.w - 0.145, y: c.z + c.h * 0.46, z: c.y + c.dd / 2, w: c.dd - 0.34, h: c.h * 0.88, rot: Math.PI / 2, c: lit });
    const n = Math.max(2, Math.round(c.w / 0.72)); for (let i = 0; i <= n; i++) c.add(c.x + i * (c.w - 0.1) / n, c.y + c.dd - 0.1, c.z, 0.1, 0.1, c.h, mat(c.colr)); // the grid comes down as columns
    c.add(c.x + c.w * 0.3, c.y + c.dd - 0.1, c.z + c.h * 0.72, c.w * 0.4, 0.4, 0.04, metal); }, // a metal entrance canopy
  crown(s) { const r = s.roof, t = tierOf(s), p = s.B.roof, metal = theme === 'dark' ? 0x2a2c30 : 0x3d4046, grey = theme === 'dark' ? 0x6f6c66 : 0x9a968c;
    box(r.x - 0.05, r.y - 0.05, r.z, r.w + 0.1, r.d + 0.1, 0.06, mat(s.colr), true, p); // the roof slab
    const sw = r.w * 0.5, sd = Math.min(0.6, r.d * 0.36), sx = r.x + r.w * 0.46, sy = r.y + 0.08; box(sx, sy, r.z + 0.06, sw, sd, 0.3, mat(grey), true, p); // a louvred screen round the plant
    for (let v = 0.05; v < 0.3; v += 0.06) { box(sx - 0.01, sy + sd, r.z + 0.06 + v, sw + 0.02, 0.02, 0.02, mat(metal), false, p); box(sx - 0.01, sy - 0.01, r.z + 0.06 + v, 0.02, sd + 0.02, 0.02, mat(metal), false, p); }
    const band = r.z - Math.min(0.44, r.z * 0.26); box(r.x, r.y + r.d, band, r.w, 0.035, r.z - band, mat(metal), false, p); box(r.x + r.w, r.y, band, 0.035, r.d, r.z - band, mat(metal), false, p); // a metal band round the top floor
    nameSign(p, nameOf(s.d), { font: '700 %px Inter, sans-serif', color: '#f1ece2', px: 96, pad: 6 }, r.x + r.w / 2, (band + r.z) / 2, r.y + r.d + 0.04, (r.z - band) * 0.72, r.w * 0.94); // with the name on it
    const q = sub(r, 0.05, 0.08 + sd / r.d + 0.04, 0.95, 0.92, 0.06);
    if (t === 'great') D.garden(p, q, 3);
    else if (t === 'good') D.pool(p, q);
    else if (t === 'mixed') { D.patches(p, q, s.seedBase); D.vents(p, sub(r, 0.05, 0.1, 0.42, 0.4, 0.06), 2); }
    else if (t === 'bad') { D.mast(p, r.x + r.w * 0.2, r.y + r.d * 0.25, r.z + 0.06, true); D.rust(p, q, s.seedBase); D.streaks(s.B.group, s); }
    s.B.tagZ = r.z + 0.6; },
});

// ── Visual · commercial buildings with signage, Times Square style: ad screens on the facades, a ticker round reviewers' floors,
// a bulb-lit marquee, a vertical blade sign down the corner and a rooftop spectacular
Object.assign(SKINS.signs, {
  facade(c) { if (!c.on || c.kind === 'manager') return; const rows = rowsOf(c.h);
    faces(c.x, c.y, c.z, c.w, c.dd).forEach(([W, f, rot], fi) => {
      if (c.kind === 'input') { for (let rw = 0; rw < rows; rw++) { const p = f(W / 2, 0.03 + (rw + 0.5) * ROWH); c.F.wins.push({ x: p[0], y: p[1], z: p[2], w: W - 0.08, h: ROWH * 0.46, rot, c: litAt(c, rw, rw, fi) }); } return; }
      const n = Math.max(2, Math.round(W / 0.2)), mu = W / n; for (let i = 0; i < n; i++) { const p = f(i * mu + mu / 2, c.h / 2); c.F.wins.push({ x: p[0], y: p[1], z: p[2], w: mu * 0.4, h: c.h - 0.08, rot, c: litAt(c, i, 0, fi) }); } });
    if (c.kind !== 'input' && (c.idx + c.seedBase) % 2 === 0 && c.h > 0.16) { // an ad screen on the street face
      const sw = c.w * 0.86, sh = Math.min(c.h - 0.05, sw * 0.62), m = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: adTex(c.stepId || c.d.n, c.seedBase + c.idx), toneMapped: false }));
      m.scale.set(sw, sh, 1); m.position.set(c.x + c.w / 2, c.z + c.h / 2, c.y + c.dd + 0.035); c.F.solid.add(m); c.add(c.x + c.w / 2 - sw / 2 - 0.025, c.y + c.dd, c.z + c.h / 2 - sh / 2 - 0.025, sw + 0.05, 0.03, sh + 0.05, mat(0x1b1c20), false); }
    if (c.kind === 'input') { const tx = tickerTex(c.d.n.replace(/[-_]/g, ' ')); // a news ticker along the two street faces
      [faces(c.x, c.y, c.z, c.w, c.dd, 0.02)[0], faces(c.x, c.y, c.z, c.w, c.dd, 0.02)[2]].forEach(([W, f, rot]) => { const t2 = tx.clone(); t2.needsUpdate = true; t2.repeat.set(W / 1.2, 1); const p = f(W / 2, c.h - 0.05), tk = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: t2, toneMapped: false })); tk.scale.set(W + 0.02, 0.07, 1); tk.position.set(p[0], p[1], p[2]); tk.rotation.y = rot; c.F.solid.add(tk); ANIMS.push({ g: tk, fn: t => { t2.offset.x = (t * 0.06 + c.seedBase * 0.01) % 1; } }); }); }
    return false; },
  base(c) { c.F.wins.push({ x: c.x + c.w / 2, y: c.z + c.h * 0.3, z: c.y + c.dd + 0.01, w: c.w - 0.1, h: c.h * 0.55, rot: 0, c: c.litMode === 'none' ? 0x3a3c48 : 0xfff1c4 }); // a lit shopfront
    const mz = c.z + c.h * 0.62; c.add(c.x - 0.05, c.y + c.dd, mz, c.w + 0.1, 0.34, 0.16, mat(0x1b1c20)); // the marquee
    for (let u = 0.04; u < c.w + 0.08; u += 0.09) [0.02, 0.14].forEach(v => c.F.wins.push({ x: c.x - 0.05 + u, y: mz + v, z: c.y + c.dd + 0.345, w: 0.035, h: 0.035, rot: 0, c: 0xfff1c4 }));
    const { tx, aspect } = textTex(nameOf(c.d), { color: '#0b0c14', bg: '#fff1c4', px: 40, font: '900 %px Inter, sans-serif', pad: 8 }), sg = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: tx, toneMapped: false }));
    const sh = 0.08; sg.scale.set(Math.min(c.w * 0.8, sh * aspect), sh, 1); sg.position.set(c.x + c.w / 2, mz + 0.08, c.y + c.dd + 0.346); c.F.solid.add(sg); },
  crown(s) { const r = s.roof, t = tierOf(s), p = s.B.roof, col = ADC[s.seedBase % ADC.length][0], { tx, aspect } = neonTex(s.d.n, col), h = Math.min(Math.max(0.8, s.z - 0.8), 3);
    const sign = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: tx, toneMapped: false, side: THREE.DoubleSide })); sign.scale.set(h * aspect, h, 1); sign.rotation.y = Math.PI / 2; sign.position.set(s.fx + s.FW + 0.14, s.z - h / 2 - 0.1, s.fy + s.FD - 0.2); s.B.group.add(sign); // a blade sign down the corner
    box(s.fx + s.FW, s.fy + s.FD - 0.22, s.z - 0.12, 0.14, 0.04, 0.03, mat(0x3a3a38), false, s.B.group);
    if (s.st === 'need' || isStale(s.d)) return; // the roof is the needs-you billboard's
    const bw = r.w * 0.9, bh = Math.min(0.7, bw * 0.42), ay = r.y + r.d * 0.35, az = r.z + 0.2 + bh / 2, frameM = t === 'bad' ? cm(RC.rust) : mat(0x3a3a38); // a rooftop spectacular
    [0.1, 0.88].forEach(k => box(r.x + r.w * k, ay - 0.08, r.z, 0.04, 0.04, 0.2 + bh, frameM, true, p));
    box(r.x + r.w / 2 - bw / 2 - 0.04, ay - 0.06, r.z + 0.16, bw + 0.08, 0.03, 0.04, frameM, true, p); box(r.x + r.w / 2 - bw / 2 - 0.04, ay - 0.06, r.z + 0.2 + bh, bw + 0.08, 0.03, 0.04, frameM, true, p);
    if (t !== 'bad') { const ad = new THREE.Mesh(PLANE, new THREE.MeshBasicMaterial({ map: adTex(nameOf(s.d), s.seedBase + 3), toneMapped: false, side: THREE.DoubleSide })); ad.scale.set(bw, bh, 1); ad.position.set(r.x + r.w / 2, az, ay); p.add(ad); }
    const front = sub(r, 0.05, 0.5, 0.95, 0.95);
    if (t === 'great') { D.umbrellas(p, front, 3); box(front.x, front.y + front.d - 0.1, front.z, front.w, 0.1, 0.12, cm(RC.leaf), true, p); } // a rooftop bar with planters
    else if (t === 'good') { mesh(p, CYL, cm(RC.water), front.x + front.w * 0.8, r.z + 0.25, front.y + front.d * 0.5, 0.34, 0.5, 0.34); mesh(p, CONE, cm(RC.water), front.x + front.w * 0.8, r.z + 0.56, front.y + front.d * 0.5, 0.38, 0.12, 0.38); } // a blue water tank
    else if (t === 'mixed') { [0.3, 0.62].forEach((k, i) => lean(mesh(p, PLANE, cm(i ? RC.ochre : RC.dry), r.x + r.w * k, az - bh * 0.1, ay + 0.012, bw * 0.24, bh * 0.5, 1, false), 0, 0.12 * (i ? 1 : -1))); // peeling paper
      for (let i = 0; i < 4; i++) box(r.x + r.w * (0.1 + i * 0.26), ay + 0.1, r.z, 0.03, 0.03, 0.2 + bh, cm(RC.grey), false, p); box(r.x + r.w * 0.1, ay + 0.1, r.z + 0.2 + bh * 0.5, r.w * 0.8, 0.03, 0.03, cm(RC.grey), false, p); } // scaffold
    else if (t === 'bad') { [0.25, 0.6].forEach((k, i) => lean(mesh(p, PLANE, cm(i ? RC.rust2 : RC.ochre), r.x + r.w * k, r.z + 0.25 + bh * 0.3, ay, 0.22, 0.3, 1, false), 0.2, 0.5 * (i ? 1 : -1))); D.rust(p, front, s.seedBase); } // torn scraps on a bare frame
    s.B.tagZ = r.z + 0.35 + bh; },
});

// ── Text · a row of townhouses: each street face splits into narrow houses in brownstone, red brick and sandstone,
// each with its own tall windows under white lintels, a bay window, a stoop to its door and a cornice at its own height
const HOUSEC = [0x7a4a36, 0x8e3f2e, 0xa58468, 0x6a4a3c, 0x9a5a3e, 0x7f6a58];
const housesOf = W => Math.max(1, Math.round(W / 0.8));
const houseCol = (seed, j, fi) => { const h = HOUSEC[(seed + j * 3 + fi * 2) % HOUSEC.length]; return theme === 'dark' ? new THREE.Color(h).multiplyScalar(0.7).getHex() : h; };
Object.assign(SKINS.brown, {
  facade(c) { if (!c.on || c.kind === 'manager') return; const rows = rowsOf(c.h), bh = (c.h - 0.05) / rows, trim = theme === 'dark' ? 0xb8ab94 : 0xefe7d6;
    faces(c.x, c.y, c.z, c.w, c.dd).forEach(([W, f, rot], fi) => { const street = fi === 0 || fi === 2, k = street ? housesOf(W) : 1, hw = W / k, g = faces(c.x, c.y, c.z, c.w, c.dd, 0.003)[fi][1];
      for (let j = 0; j < k; j++) { const u0 = j * hw;
        if (street) { const p = g(u0 + hw / 2, c.h / 2); c.F.trim.push({ x: p[0], y: p[1], z: p[2], w: hw - 0.012, h: c.h, rot, c: houseCol(c.seedBase, j, fi) }); } // this house's brick
        for (let rw = 0; rw < rows; rw++) { const v = 0.03 + (rw + 0.45) * bh;
          if (c.kind === 'input') { const p = f(u0 + hw / 2, v); c.F.wins.push({ x: p[0], y: p[1], z: p[2], w: hw - 0.2, h: bh * 0.36, rot, c: litAt(c, j, rw, fi) }); // one wide window per house
            const q = f(u0 + hw / 2, v - bh * 0.24); c.F.trim.push({ x: q[0], y: q[1], z: q[2] + 0.001, w: hw - 0.14, h: 0.025, rot, c: trim }); continue; } // over a white sill
          const nw = street ? 2 : Math.max(2, Math.round(W / 0.32)), mw = hw / nw;
          for (let i = 0; i < nw; i++) { const p = f(u0 + i * mw + mw / 2, v); c.F.wins.push({ x: p[0], y: p[1], z: p[2], w: mw * 0.42, h: bh * 0.62, rot, c: litAt(c, i + j, rw, fi) }); // tall windows
            const q = f(u0 + i * mw + mw / 2, v + bh * 0.36); c.F.trim.push({ x: q[0], y: q[1], z: q[2] + 0.001, w: mw * 0.56, h: 0.032, rot, c: trim }); } } // white lintels
      }
    });
    if (c.kind !== 'input') { const k = housesOf(c.w), hw = c.w / k; for (let j = 0; j < k; j++) if ((j + c.idx) % 2 === 0 && hw > 0.5) { const bw = hw * 0.5, bx = c.x + j * hw + (hw - bw) / 2; // bay windows on the street face
      c.F.blocks.push(Object.assign([bx, c.y + c.dd, c.z + 0.03, bw, 0.12, c.h - 0.06], { c: houseCol(c.seedBase, j, 0) })); c.F.wins.push({ x: bx + bw / 2, y: c.z + c.h / 2, z: c.y + c.dd + 0.125, w: bw - 0.1, h: c.h * 0.56, rot: 0, c: litAt(c, j, 1, 0) }); } }
    return false; },
  base(c) { const iron = mat(0x1f1d1b), lit = c.litMode === 'none' ? c.P.dark : c.P.lit, k = housesOf(c.w), hw = c.w / k, trim = theme === 'dark' ? 0xb8ab94 : 0xefe7d6, g = faces(c.x, c.y, c.z, c.w, c.dd, 0.045)[0][1];
    for (let j = 0; j < k; j++) { const p = g(j * hw + hw / 2, c.h / 2); c.F.trim.push({ x: p[0], y: p[1], z: p[2], w: hw - 0.012, h: c.h, rot: 0, c: new THREE.Color(houseCol(c.seedBase, j, 0)).multiplyScalar(0.85).getHex() }); // each house's stone ground floor
      const sx = c.x + j * hw + hw * 0.55, sw = Math.min(0.3, hw * 0.34); for (let s2 = 0; s2 < 4; s2++) c.F.blocks.push(Object.assign([sx, c.y + c.dd + 0.04 + 0.08 * (3 - s2), c.z, sw, 0.08, (s2 + 1) * c.h * 0.1], { c: new THREE.Color(houseCol(c.seedBase, j, 0)).multiplyScalar(0.8).getHex() })); // its stoop
      c.add(sx - 0.015, c.y + c.dd + 0.34, c.z, 0.015, 0.015, c.h * 0.42, iron, false); c.add(sx + sw, c.y + c.dd + 0.34, c.z, 0.015, 0.015, c.h * 0.42, iron, false); // iron railings
      c.F.wins.push({ x: sx + sw / 2, y: c.z + c.h * 0.64, z: c.y + c.dd + 0.05, w: sw * 0.7, h: c.h * 0.44, rot: 0, c: lit }); // the door
      c.F.trim.push({ x: sx + sw / 2, y: c.z + c.h * 0.9, z: c.y + c.dd + 0.05, w: sw * 0.9, h: 0.03, rot: 0, c: trim });
      c.F.wins.push({ x: c.x + j * hw + hw * 0.24, y: c.z + c.h * 0.45, z: c.y + c.dd + 0.05, w: hw * 0.2, h: c.h * 0.4, rot: 0, c: lit }); } // a garden-level window
    const mid = Math.floor(k / 2), sx = c.x + mid * hw + hw * 0.55, sw = Math.min(0.3, hw * 0.34); // the name in gold on the middle house's transom
    nameSign(c.F.solid, nameOf(c.d), { font: 'italic 400 %px Instrument Serif, Georgia, serif', color: '#e0bd66', bg: '#1d1a17', px: 56, pad: 10 }, c.x + c.w / 2, c.z + c.h * 0.97, c.y + c.dd + 0.055, 0.24, c.w * 0.7, 0, true); }, // gold lettering on a board across the houses
  crown(s) { const r = s.roof, t = tierOf(s), p = s.B.roof, dark = theme === 'dark' ? 0x2a2522 : 0x3a302a, k = housesOf(r.w), hw = r.w / k, kd = housesOf(r.d), hd = r.d / kd, cor = [];
    for (let j = 0; j < k; j++) { const up = ((s.seedBase + j) % 3) * 0.07; cor.push(Object.assign([r.x + j * hw, r.y + r.d - 0.22, r.z, hw, 0.22, up], { c: houseCol(s.seedBase, j, 0) }), Object.assign([r.x + j * hw - 0.01, r.y + r.d - 0.1, r.z + up, hw + 0.02, 0.2, 0.07], { c: dark })); } // each house's parapet and cornice at its own height
    for (let j = 0; j < kd; j++) { const up = ((s.seedBase + j + 1) % 3) * 0.07; cor.push(Object.assign([r.x + r.w - 0.22, r.y + r.d - (j + 1) * hd, r.z, 0.22, hd, up], { c: houseCol(s.seedBase, j, 2) }), Object.assign([r.x + r.w - 0.1, r.y + r.d - (j + 1) * hd - 0.01, r.z + up, 0.2, hd + 0.02, 0.07], { c: dark })); }
    instanced(cor, mat(0xffffff), p);
    for (let j = 0; j < k; j++) { const ch = mesh(p, BOX, mat(houseCol(s.seedBase, j, 0)), r.x + j * hw + hw * 0.5, r.z + 0.2, r.y + r.d * 0.18, 0.12, 0.4, 0.12); if (t === 'bad' && j === k - 1) { lean(ch, 0.5, 0.6); ch.position.y -= 0.08; } } // chimneys, one collapsed when bad
    const q = sub(r, 0.08, 0.3, 0.8, 0.72);
    if (t === 'great') { D.garden(p, q, 2); const ih = Math.min(s.z - 0.7, 1.4); [0.15, 0.55].forEach(a => { const iv = new THREE.Mesh(PLANE, cm(RC.leaf)); iv.scale.set(s.FD * 0.3, ih * (a < 0.3 ? 1 : 0.7), 1); iv.rotation.y = Math.PI / 2; iv.position.set(s.fx + s.FW + 0.016, s.z - ih * (a < 0.3 ? 0.5 : 0.35), s.fy + s.FD * (a + 0.15)); s.B.group.add(iv); }); } // a roof garden and ivy up the side
    else if (t === 'good') { box(q.x, q.y, q.z, q.w, q.d, 0.03, cm(RC.deck), false, p); [0.2, 0.4].forEach(a => mesh(p, CYL, cm(RC.water), q.x + q.w * a, q.z + 0.12, q.y + q.d * 0.5, 0.16, 0.2, 0.16)); box(q.x + q.w * 0.65, q.y + q.d * 0.3, q.z + 0.03, 0.14, 0.14, 0.06, cm(RC.white), false, p); } // a tidy deck with blue rain barrels
    else if (t === 'mixed') { D.patches(p, q, s.seedBase); [0.3, 0.6].forEach(a => D.mast(p, r.x + r.w * a, r.y + r.d * 0.5, r.z, false)); } // tar paper and TV antennas
    else if (t === 'bad') { for (let i = 0; i < 3; i++) { const fz = s.z - 0.35 - i * 0.4; if (fz < 0.9) break; box(s.fx + s.FW * 0.55, s.fy + s.FD, fz, s.FW * 0.3, 0.16, 0.03, cm(RC.rust), true, s.B.group); lean(mesh(s.B.group, BOX, cm(RC.rust2), s.fx + s.FW * 0.7, fz - 0.2, s.fy + s.FD + 0.1, s.FW * 0.26, 0.02, 0.1), 0, 0.7); } D.rust(p, q, s.seedBase); } }, // a rusty fire escape and scrap
});

// ── Data · storehouses: pale ribbed panels, slot windows, colour-coded roll-up bays under a long canopy,
// skylight strips on the roof and a stack of crates, one per file it made
Object.assign(SKINS.store, {
  facade(c) { if (!c.on || c.kind === 'manager') return; const rib = new THREE.Color(c.colr).multiplyScalar(0.88).getHex(), rows = rowsOf(c.h);
    faces(c.x, c.y, c.z, c.w, c.dd).forEach(([W, f, rot], fi) => { for (let u = 0.05; u < W; u += 0.1) { const p = f(u, c.h / 2); c.F.wins.push({ x: p[0], y: p[1], z: p[2], w: 0.02, h: c.h, rot, c: rib }); } // ribbed panels
      if (c.kind === 'input') { for (let rw = 0; rw < rows; rw++) { const p = f(W / 2, 0.03 + (rw + 0.5) * ROWH); c.F.wins.push({ x: p[0], y: p[1], z: p[2] + 0.002, w: W - 0.12, h: ROWH * 0.3, rot, c: litAt(c, rw, rw, fi) }); } return; } // long strips
      const n = Math.max(2, Math.round(W / 0.45)), mu = W / n; for (let i = 0; i < n; i++) { const p = f(i * mu + mu / 2, c.h / 2); c.F.wins.push({ x: p[0], y: p[1], z: p[2] + 0.002, w: 0.08, h: c.h - 0.1, rot, c: litAt(c, i, 0, fi) }); } }); // slot windows
    return false; },
  base(c) { const cols = [0x7d8fa3, 0xc9b27a, 0xa99a8a, 0x8a9aa6], n = Math.max(1, Math.floor(c.w / 0.5)); // roll-up bays, each its own muted colour
    for (let i = 0; i < n; i++) { const dw = (c.w - 0.2) / n - 0.08, dx = c.x + 0.1 + i * (c.w - 0.2) / n; c.add(dx, c.y + c.dd - 0.01, c.z, dw, 0.03, c.h * 0.7, mat(cols[(i + c.seedBase) % 4]));
      for (let v = 0.06; v < c.h * 0.68; v += 0.06) c.F.wins.push({ x: dx + dw / 2, y: c.z + v, z: c.y + c.dd + 0.022, w: dw, h: 0.008, rot: 0, c: 0x55565b }); }
    c.add(c.x - 0.06, c.y + c.dd + 0.02, c.z + c.h * 0.78, c.w + 0.12, 0.3, 0.035, mat(lightTrim()), true); }, // a long canopy
  crown(s) { const r = s.roof, t = tierOf(s), p = s.B.roof, n = Math.min(18, kindOf(s.d).tot), cq = sub(r, 0.62, 0.08, 0.98, 0.9);
    if (s.z > 1.2) nameSign(s.B.group, nameOf(s.d), { font: '700 %px Courier Prime, monospace', color: '#efe9dc', bg: t === 'bad' ? '#5a4238' : '#3b3e44', px: 64, pad: 14 }, s.fx + s.FW + 0.02, s.z * 0.58, s.fy + s.FD / 2, Math.min(0.7, s.z * 0.28), s.FD * 0.92, Math.PI / 2); // a painted wall sign
    if (t === 'great') { box(r.x, r.y, r.z, r.w, r.d, 0.035, cm(RC.leaf2), false, p); for (let i = 0; i < 10; i++) mesh(p, SPH, cm(RC.leaf), r.x + r.w * (0.08 + (i * 37 % 50) / 100), r.z + 0.05, r.y + r.d * (0.1 + (i * 53 % 80) / 100), 0.2, 0.08, 0.2, false); D.crates(p, cq, n, 'neat', s.seedBase); return; } // a green sedum roof
    const sky = []; for (let u = 0.12; u < r.w * 0.55; u += 0.36) sky.push([r.x + u, r.y + 0.12, r.z, 0.22, r.d - 0.24, 0.05]); instanced(sky, mat(theme === 'dark' ? 0x5f7f8f : 0xa9c6d3, { roughness: 0.2 }), p); // skylights
    if (t === 'good') { D.solar(p, sub(r, 0.05, 0.1, 0.58, 0.9, 0.04)); D.crates(p, cq, n, 'neat', s.seedBase); } // solar panels, neat crates
    else if (t === 'mixed') { D.crates(p, cq, n, 'messy', s.seedBase); D.tarps(p, cq, s.seedBase); } // tarps over a messy heap
    else if (t === 'bad') { [0, 1].forEach(i => lean(mesh(p, BOX, cm(i ? RC.rust2 : RC.rust), r.x + r.w * 0.3, r.z + 0.12, r.y + r.d * (0.3 + i * 0.4), r.w * 0.4, 0.22, r.d * 0.22), 0, 0.06 * (i ? 1 : -1))); D.crates(p, cq, n, 'spilled', s.seedBase); D.streaks(s.B.group, s, 2); } // rusted containers, spilled crates
    else D.crates(p, cq, n, 'neat', s.seedBase); },
});

// ── Talk (no files) · Art Deco: setback towers, vertical fins and gold trim, a ziggurat crown and a spire
Object.assign(SKINS.deco, {
  setback: (i, n) => { if (n <= 2) return 1; const f = i / (n - 1); return f < 0.45 ? 1 : f < 0.75 ? 0.84 : 0.68; },
  ledge: P => P.plate,
  facade(c) { if (!c.on || c.kind === 'manager') return; const gold = mat(0xc9a24a, { metalness: 0.7, roughness: 0.35 }), fin = mat(new THREE.Color(c.colr).multiplyScalar(0.85).getHex());
    if (c.kind === 'input') { ribbons(c, 0.12, 0.42); c.add(c.x - 0.02, c.y - 0.02, c.z + c.h - 0.04, c.w + 0.04, c.dd + 0.04, 0.035, gold, false); return false; }
    faces(c.x, c.y, c.z, c.w, c.dd).forEach(([W, f, rot], fi) => { const n = Math.max(2, Math.round(W / 0.3)), mu = W / n;
      for (let i = 0; i < n; i++) { const p = f(i * mu + mu / 2, c.h / 2); c.F.wins.push({ x: p[0], y: p[1], z: p[2], w: mu * 0.42, h: c.h - 0.12, rot, c: litAt(c, i, 0, fi) }); } });
    const nf = Math.max(2, Math.round(c.w / 0.3)); for (let i = 0; i <= nf; i += 2) c.add(c.x + i * c.w / nf - 0.025, c.y + c.dd - 0.01, c.z, 0.05, 0.06, c.h, fin); // fins on the street face
    const ng = Math.max(2, Math.round(c.dd / 0.3)); for (let i = 0; i <= ng; i += 2) c.add(c.x + c.w - 0.01, c.y + i * c.dd / ng - 0.025, c.z, 0.06, 0.05, c.h, fin);
    c.add(c.x - 0.02, c.y - 0.02, c.z + c.h - 0.04, c.w + 0.04, c.dd + 0.04, 0.035, gold, false); // gold band
    return false; },
  base(c) { const gold = mat(0xc9a24a, { metalness: 0.7, roughness: 0.35 }), stone = mat(new THREE.Color(c.colr).multiplyScalar(0.7).getHex());
    c.add(c.x - 0.04, c.y - 0.04, c.z, c.w + 0.08, c.dd + 0.08, 0.14, stone);
    const door = new THREE.Mesh(PLANE, glow(c.litMode === 'none' ? c.P.dark : c.P.lit)); door.scale.set(Math.min(0.7, c.w * 0.3), c.h * 0.7, 1); door.position.set(c.x + c.w / 2, c.z + c.h * 0.42, c.y + c.dd + 0.012); c.F.solid.add(door);
    c.add(c.x + c.w / 2 - Math.min(0.45, c.w * 0.2), c.y + c.dd, c.z + c.h * 0.8, Math.min(0.9, c.w * 0.4), 0.28, 0.05, gold); // canopy
    [[c.x + 0.1, c.y + c.dd + 0.15], [c.x + c.w - 0.14, c.y + c.dd + 0.15]].forEach(([a, b]) => { c.add(a, b, c.z, 0.04, 0.04, 0.5, gold); mesh(c.F.solid, SPH, glow(0xffe6a8), a + 0.02, c.z + 0.56, b + 0.02, 0.12, 0.12, 0.12, false); });
    const band = []; faces(c.x, c.y, c.z, c.w, c.dd).forEach(([W, f, rot]) => { const p = f(W / 2, c.h - 0.06); band.push({ x: p[0], y: p[1], z: p[2], w: W, h: 0.04, rot, c: 0xc9a24a }); }); c.F.wins.push(...band); },
  crown(s) { if (!s.finished) return; const r = s.roof, t = tierOf(s), p = s.B.roof, gold = mat(0xc9a24a, { metalness: 0.75, roughness: 0.3 }), stone = mat(t === 'bad' ? 0x6a645a : s.P.done), soot = cm(RC.soot);
    let z = r.z; const cx = r.x + r.w / 2, cy = r.y + r.d / 2;
    nameSign(p, nameOf(s.d), { font: '400 %px Instrument Serif, Georgia, serif', color: '#d9b04e', px: 96, pad: 4 }, cx, z + 0.11, cy + r.d * 0.39 + 0.012, 0.2, r.w * 0.74); // gold letters on the crown's first tier
    [0.78, 0.56, 0.36].forEach((k, i) => { const w = r.w * k, d = r.d * k; box(cx - w / 2, cy - d / 2, z, w, d, 0.22, t === 'bad' && i === 2 ? soot : stone, true, p);
      if (t === 'great' && i < 2) { const nw = r.w * [0.56, 0.36][i]; box(cx - w / 2, cy + nw / 2, z + 0.22, w, (d - nw) / 2, 0.08, cm(RC.leaf), false, p); box(cx + nw / 2, cy - d / 2, z + 0.22, (w - nw) / 2, d, 0.08, cm(RC.leaf), false, p); } // gardens on the setbacks
      if (t === 'great' || t === 'good') box(cx - w / 2 - 0.01, cy - d / 2 - 0.01, z + 0.18, w + 0.02, d + 0.02, 0.03, gold, false, p); z += 0.22; });
    if (t === 'bad') { lean(mesh(p, CONE, cm(RC.rust), cx, z + 0.12, cy, 0.14, 0.25, 0.14), 0.3, 0.25); D.streaks(s.B.group, s, 2); s.spireTop = z + 0.25; s.B.tagZ = z + 0.6; return; } // a broken stub of a spire
    const sh = 0.5 + Math.min(2.4, (s.d.u || 0) / 10); mesh(p, CONE, t === 'great' || t === 'good' ? gold : stone, cx, z + sh / 2, cy, 0.12, sh, 0.12);
    if (t === 'good') mesh(p, SPH, mat(RC.water, { roughness: 0.1, metalness: 0.3 }), cx, z + sh * 0.45, cy, 0.2, 0.26, 0.2); // a blue glass lantern
    if (t === 'mixed') { [[-1, -1], [1, -1], [1, 1], [-1, 1]].forEach(([a, b]) => box(cx + a * 0.12 - 0.01, cy + b * 0.12 - 0.01, z, 0.02, 0.02, sh * 0.8, cm(RC.grey), false, p)); for (let v = 0.2; v < sh * 0.8; v += 0.25) box(cx - 0.13, cy - 0.13, z + v, 0.26, 0.26, 0.015, cm(RC.ochre), false, p); } // scaffolding
    s.spireTop = z + sh; s.B.tagZ = z + sh + 0.3; },
});

window.__lc = { ISSUES, PLANOBJ, show, goTo, flyTo, kindOf, look: n => { const B = buildings.find(b => b.d.n === n); if (B) { show(null); flyTo(B.center, 3.2); } return !!B; }, get buildings() { return buildings; }, get roomPick() { return ROOMPICK; }, camera }; // debug handle for testing the prototype
$('helpBtn').onclick = () => { const h = $('help'); h.hidden = !h.hidden; $('helpBtn').setAttribute('aria-expanded', String(!h.hidden)); $('helpBtn').setAttribute('aria-pressed', String(!h.hidden)); };

// ─── time: scrub or play history; buildings appear when they started and rise floor by floor ───
const T0 = Math.min(...DATA.map(d => d.st || NOW)), T1 = NOW; let T = null, playing = false, lastTick = null, rates = null;
const endOf = d => d.end || d.act || d.up;
const activeAt = t => DATA.filter(d => d.st <= t && t < endOf(d)).length;
function playRates() { if (rates) return rates; const step = Math.max(300, (T1 - T0) / 2000); let busy = 0, idle = 0; for (let t = T0; t < T1; t += step) activeAt(t) ? busy += step : idle += step; return (rates = { active: Math.max(600, busy / 18), idle: Math.max(3600, idle / 4) }); }
function applyTime() {
  buildings.forEach(B => {
    const d = B.d; if (T == null) { B.group.visible = true; B.floors.forEach(F => { F.solid.visible = F.on; if (F.fm) F.fm.visible = !F.on; }); B.roof.visible = true; if (B.crane) B.crane.visible = B.live; return; }
    if (!d.st || d.st > T) { B.group.visible = false; return; } B.group.visible = true;
    // the whole frame goes up when the loop starts; floors are finished one by one as its rounds complete
    const end = endOf(d), ev = (d.ev || []).filter(e => e[3] === 'round'), frac = T >= end ? 1 : ev.length ? ev.filter(e => e[0] <= T).length / ev.length : (T - d.st) / Math.max(1, end - d.st);
    const n = Math.min(B.filled, Math.floor(frac * B.floors.length)); B.floors.forEach((F, i) => { F.solid.visible = i < n; if (F.fm) F.fm.visible = i >= n; });
    B.roof.visible = frac >= 1; if (B.crane) B.crane.visible = T < end;
  });
  const lbl = byId('tlabel'), built = DATA.filter(d => T == null || d.st <= T).length, act = T == null ? 0 : activeAt(T);
  lbl.textContent = T == null ? 'Now' : new Date(T * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) + (act ? ' · ' + act + ' building' : ' · ⏩');
  byId('tscrub').value = T == null ? 1000 : Math.round((T - T0) / (T1 - T0) * 1000); const pb = $('tplay'); pb.textContent = playing ? '❚❚' : '▶'; pb.title = playing ? 'Pause' : 'Play history'; $('tlive').hidden = T == null; if (T != null) clearRoutes();
  requestRender();
}
byId('tscrub').oninput = e => { playing = false; const v = +e.target.value; T = v >= 1000 ? null : T0 + (T1 - T0) * v / 1000; applyTime(); };
byId('tplay').onclick = () => { playing = !playing; if (playing && (T == null || T >= T1)) T = T0; lastTick = null; applyTime(); };
byId('tlive').onclick = () => { playing = false; T = null; applyTime(); };

// ─── render on demand: only draw when something changed or is animating ───
let needRender = true, lastFrame = null;
function requestRender() { needRender = true; }
controls.addEventListener('change', requestRender);
function resize() { const w = stage.clientWidth, h = stage.clientHeight; renderer.setSize(w, h); labels.setSize(w, h); composer.setSize(w, h); bloom.resolution.set(w, h); const a = w / h, hh = camera.top; camera.left = -hh * a; camera.right = hh * a; camera.updateProjectionMatrix(); requestRender(); }
onWin('resize', resize); const ro = new ResizeObserver(() => { if (!disposed) resize(); }); ro.observe(stage);
(document.fonts ? document.fonts.ready : Promise.resolve()).then(() => { if (disposed) return; build(prodSel.value); resize(); show(null); renderNeeds(); applyTime(); });
renderer.setAnimationLoop(t => {
  let busy = false;
  if (camAnim) { const k = Math.min(1, (t - camAnim.t0) / 480), e = 1 - Math.pow(1 - k, 3); camera.position.lerpVectors(camAnim.from, camAnim.to, e);
    if (camAnim.tTo) { controls.target.lerpVectors(camAnim.tFrom, camAnim.tTo, e); camera.zoom = camAnim.zFrom + (camAnim.zTo - camAnim.zFrom) * e; camera.updateProjectionMatrix(); }
    if (k >= 1) camAnim = null; busy = true; }
  if (stepExplode(t)) busy = true;
  if (stepTweens(t)) busy = true;
  if (stepRoutes(t)) busy = true;
  if (playing) { if (lastTick != null) { const dt = Math.min(0.5, (t - lastTick) / 1000), r = playRates(); let tt = T ?? T0;
      if (activeAt(tt)) tt += dt * r.active; else { const nx = DATA.filter(d => d.st > tt).reduce((m, d) => Math.min(m, d.st), T1); tt = Math.min(nx, tt + dt * r.idle); }
      T = tt >= T1 ? null : tt; if (T == null) playing = false; applyTime(); } lastTick = t; }
  const dt = lastFrame == null ? 0 : Math.min(0.1, (t - lastFrame) / 1000); lastFrame = t;
  if (T == null && cars) { const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), up = new THREE.Vector3(0, 1, 0), sc = new THREE.Vector3(...((SK().car && SK().car.size) || [0.32, 0.12, 0.16])); // traffic on the street graph
    cars.list.forEach((c, i) => {
      let dx = c.b.x - c.a.x, dy = c.b.y - c.a.y, len = Math.hypot(dx, dy) || 1; c.t += dt * c.v / len;
      if (c.t >= 1) { const opts = c.b.adj.filter(o => o.n !== c.a), nx = opts.length ? opts[Math.floor(Math.random() * opts.length)] : c.b.adj[0]; c.a = c.b; c.b = nx.n; c.s = nx.s; c.t = 0; dx = c.b.x - c.a.x; dy = c.b.y - c.a.y; len = Math.hypot(dx, dy) || 1; }
      const ux = dx / len, uy = dy / len, lane = c.s.av ? 0.62 : 0.3; // keep right
      q.setFromAxisAngle(up, -Math.atan2(uy, ux)); m4.compose(new THREE.Vector3(c.a.x + dx * c.t - uy * lane, 0.14, c.a.y + dy * c.t + ux * lane), q, sc); cars.im.setMatrixAt(i, m4); });
    cars.im.instanceMatrix.needsUpdate = true; busy = true; }
  if (ANIMS.length) { let any = false; ANIMS.forEach(a => { let o = a.g, vis = true; while (o) { if (!o.visible) { vis = false; break; } o = o.parent; } if (vis) { a.fn(t / 1000); any = true; } }); if (any) busy = true; }
  if (cranes.some(c => c.crane.visible)) { cranes.forEach(c => { if (c.crane.visible) c.head.rotation.y = Math.sin(t / 1000 * c.speed + c.phase) * 1.2; }); busy = true; }
  if (controls.update()) busy = true;
  if (busy || needRender) { composer.render(); labels.render(scene, camera); needRender = false; }
});

return {
  setTheme(t) { if (disposed || t === theme) return; theme = t; for (const k in matCache) delete matCache[k]; build(prodSel.value, true); applyTime(); },
  look: n => window.__lc && window.__lc.look(n),
  // Fresh facts for loops already in the city (state, turns, details that load later). Rebuilds in place and keeps
  // the camera; returns false (nothing applied) while you're inside a building, so the host retries later.
  update(loops) { if (disposed || FV || exploded) return false; loops.forEach(f => { const d = DATA.find(x => x.n === f.n && x.h === f.h); if (d) { Object.assign(d, f); if (!f._kind) delete d._kind; } }); build(prodSel.value, true); applyTime(); return true; },
  dispose() {
    disposed = true; renderer.setAnimationLoop(null); offs.forEach(f => f()); ro.disconnect(); controls.dispose();
    scene.traverse(o => { if (o.isCSS2DObject && o.element) o.element.remove(); if (o.geometry) o.geometry.dispose(); const ms = Array.isArray(o.material) ? o.material : o.material ? [o.material] : [];
      ms.forEach(m => { for (const k in m) if (m[k] && m[k].isTexture) m[k].dispose(); m.dispose(); }); });
    composer.dispose && composer.dispose(); renderer.dispose(); renderer.domElement.remove(); labels.domElement.remove();
    if (window.__lc && window.__lc.camera === camera) delete window.__lc;
  },
};
}
