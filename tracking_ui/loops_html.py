"""Loopyard surface — the self-contained interactive dashboard.

Kept out of ``loops_panel.py`` so route handlers stay readable. Everything
downstream (CSS, JS, iconography) is inline / data-URI so the whole page ships
under a strict CSP with no external asset fetches.

Nav model (redesign §2): Loops · Idea Hub · Issues · Origins · Sessions, with
Project as a switcher that rescopes the rail — no standalone Runs registry, no
"+ Run" launcher (both cut in the redesign). Loops are created goal-first through
the composer (Door A target / Door B brief); a loop-of-1 is the smallest loop,
not a resurrected Run.

Identity: applies the Loopyard brand — ember on ink, phosphor for live, the
loop-on-yard glyph. Ownership is the hero; price stays out of the H1s.
"""

LOOPS_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Loopyard — own the loop</title>
<meta name="theme-color" content="#17130D">
<meta name="description" content="Loopyard — your agents, your compute, your keys. Our loops. Open harness — read the code and self-host: https://github.com/tim-po/loopyard">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' fill='%2317130D'/><rect x='12' y='19' width='40' height='26' rx='13' fill='none' stroke='%23FF6A2B' stroke-width='5' stroke-linecap='round'/><circle cx='49' cy='32' r='5' fill='%2346E0A0'/></svg>">
<!-- PWA (QoL R6 / M2): installable dashboard. Manifest + icons served locally. -->
<link rel="manifest" href="/manifest.webmanifest">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Loopyard">
<link rel="apple-touch-icon" href="/static/pwa/loopyard-192.png">
<!-- TOOLS (Phase A): web Terminal + +Loop creator use xterm.js, vendored
     same-origin under /static/vendor/xterm — no CDN (CSP-safe). -->
<link rel="stylesheet" href="/static/vendor/xterm/xterm.css">
<style>
:root{
  --ink:#17130D; --ink-2:#201A11; --ink-3:#2B2318;
  --bone:#F2ECDF; --bone-dim:#CFC5B0; --stone:#948A76;
  --ember:#FF6A2B; --ember-soft:#FFA06B; --ember-deep:#C74E1B;
  --phos:#46E0A0; --rose:#E0576B;
  --on-ember:#1a0e05;  /* the readable "text on ember" color — ember-filled buttons only */
  --line:rgba(242,236,223,0.10); --line-strong:rgba(242,236,223,0.20);
  --mono:ui-monospace,"JetBrains Mono","SF Mono",Menlo,"Cascadia Code",monospace;
  --sans:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --r-sm:8px; --r:12px; --r-lg:18px; --r-pill:999px;
}
*{box-sizing:border-box}
html,body{height:100%;background:var(--ink)}
body{margin:0;color:var(--bone);font:14px/1.5 var(--sans);letter-spacing:-.005em;-webkit-font-smoothing:antialiased}
button,input,select,textarea{font:inherit;color:inherit}
/* app shell */
.app{display:flex;height:100vh}
/* The "yard" — grounded dot-grid baseplate (from brand.css). Sits under the
   sidebar so the ownership motif isn't only a wordmark; also usable on any
   surface that wants a bit of texture — .empty picks it up below. */
.yard{background-image:radial-gradient(rgba(242,236,223,0.06) 1px, transparent 1.4px);
      background-size:26px 26px;background-position:-13px -13px}
.sidebar{width:236px;flex:none;background:var(--ink-2);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:16px 14px;
  position:relative}
/* Tiled dot-grid on its OWN composited layer (translateZ promotes it): the
   gradient is rasterized once and merely re-composited thereafter, so a repaint
   elsewhere on the page never forces Safari to re-paint this expensive tile.
   z-index:-1 keeps it behind the sidebar content, exactly as the old
   background-image did — no visual change. */
.sidebar::before{content:"";position:absolute;inset:0;z-index:-1;pointer-events:none;
  background-image:radial-gradient(rgba(242,236,223,0.045) 1px, transparent 1.4px);
  background-size:26px 26px;background-position:-13px -13px;
  transform:translateZ(0);will-change:transform}
.brand{display:flex;align-items:center;gap:10px;padding:2px 6px 14px;user-select:none;cursor:pointer;
  transition:opacity .15s ease}
.brand:hover{opacity:.92}
.brand:hover .glyph{transform:rotate(-3deg)}
.brand .glyph{width:28px;height:28px;flex:none;transition:transform .25s cubic-bezier(.2,.7,.3,1)}
.brand .wm{font-family:var(--mono);font-weight:700;font-size:17px;letter-spacing:-.02em;color:var(--bone)}
.brand .wm b{color:var(--bone)}
.brand .wm em{font-style:normal;color:var(--ember-soft)}
.newrun{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:11px 14px;margin:2px 0 16px;
  background:var(--ember);color:var(--on-ember);border:1px solid transparent;border-radius:var(--r-pill);
  font-family:var(--mono);font-size:12.5px;font-weight:700;letter-spacing:.02em;cursor:pointer;
  box-shadow:0 8px 22px -12px var(--ember-deep);transition:background .15s ease,transform .1s ease}
.newrun:hover{background:var(--ember-soft);transform:translateY(-1px)}
.newrun:active{transform:translateY(0)}
/* REDESIGN-SPEC §2 — the persistent project switcher (primary global filter). */
.projswitch{margin:14px 0 4px}
.pswrap{display:flex;align-items:center;gap:7px;width:100%;padding:8px 11px;background:var(--ink-2);
  border:1px solid var(--line);border-radius:var(--r-pill);cursor:pointer}
.pswrap:hover{border-color:var(--ember)}
.psglyph{color:var(--ember);font-size:12px}
.pswrap select{flex:1;min-width:0;background:transparent;border:none;color:var(--ink-fg,inherit);
  font-family:var(--mono);font-size:12px;font-weight:600;cursor:pointer;outline:none}
/* REDESIGN-SPEC §2/§Q1 — Loops-list filter chips (active project + single-agent). */
.loopchips{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 10px}
.lchip{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;background:var(--ink-2);
  border:1px solid var(--line);border-radius:999px;color:var(--stone);font-family:var(--mono);
  font-size:11px;font-weight:600;cursor:pointer;transition:border-color .12s ease,color .12s ease}
.lchip:hover{border-color:var(--ember);color:var(--ink-fg,inherit)}
.lchip.on{background:var(--ember);color:var(--on-ember);border-color:transparent}
.lchip.on.proj{background:transparent;color:var(--ember);border-color:var(--ember)}
.lchipn{background:rgba(0,0,0,.18);border-radius:999px;padding:0 6px;font-size:10px}
.lchip .lx{opacity:.7;font-weight:700}
.soloflag{color:var(--ember-soft,var(--ember))}
.navsec{color:var(--stone);font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.14em;font-weight:600;padding:12px 10px 5px}
.nav{display:flex;flex-direction:column;gap:2px}
.navitem{display:flex;align-items:center;gap:11px;padding:8px 11px;border-radius:9px;color:var(--bone-dim);cursor:pointer;
  font-size:13.5px;font-weight:600;user-select:none;border:1px solid transparent}
.navitem:hover{background:var(--ink-3);color:var(--bone)}
.navitem.on{background:rgba(255,106,43,.12);color:var(--ember-soft);border-color:rgba(255,106,43,.28)}
.navitem .nico{width:20px;text-align:center;font-size:14px;color:var(--stone)}
.navitem.on .nico{color:var(--ember)}
.navitem .ncnt{margin-left:auto;font-family:var(--mono);font-size:10px;font-weight:700;color:var(--stone);
  background:var(--ink-3);border:1px solid var(--line);padding:1px 7px;border-radius:999px;min-width:20px;text-align:center;letter-spacing:.02em}
.navitem:hover .ncnt{color:var(--bone-dim);border-color:var(--line-strong)}
.navitem.on .ncnt{color:var(--ember-soft);background:rgba(255,106,43,.14);border-color:rgba(255,106,43,.32)}
/* D8 — brand-styled nav tooltip (native title= is the no-CSS fallback). Shows
   on hover AND keyboard focus so the definition reaches keyboard users too. */
.navitem[data-tip]{position:relative}
.navitem[data-tip]::after{content:attr(data-tip);position:absolute;left:calc(100% + 12px);top:50%;
  transform:translateY(-50%) translateX(-5px);width:216px;background:var(--ink-3);color:var(--bone);
  border:1px solid var(--line-strong);border-radius:9px;padding:9px 11px;
  font-family:var(--sans);font-size:11.5px;font-weight:500;line-height:1.42;letter-spacing:normal;text-transform:none;
  box-shadow:0 14px 34px -14px rgba(0,0,0,.75);opacity:0;visibility:hidden;pointer-events:none;z-index:60;
  transition:opacity .14s ease,transform .14s ease}
.navitem[data-tip]:hover::after,.navitem[data-tip]:focus::after,.navitem[data-tip]:focus-visible::after{
  opacity:1;visibility:visible;transform:translateY(-50%) translateX(0)}
.favshelf{margin-top:6px;display:flex;flex-direction:column;gap:1px;max-height:210px;overflow-y:auto}
.favrow{display:flex;align-items:center;gap:8px;padding:5px 10px;border-radius:7px;font-size:12.5px;color:var(--bone-dim);cursor:pointer}
.favrow:hover{background:var(--ink-3);color:var(--bone)}
.favrow .fico{color:var(--ember);font-size:11px;width:12px;text-align:center}
.favrow .fid{font-family:var(--mono);font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.favempty{font-family:var(--mono);font-size:10.5px;color:var(--stone);padding:8px 10px 4px;font-style:italic;letter-spacing:.02em}
.navfoot{margin-top:auto;padding-top:12px;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:10px}
.toggle{display:flex;align-items:center;gap:7px;font-size:11.5px;color:var(--stone);cursor:pointer;user-select:none;padding:0 6px}
.toggle input{accent-color:var(--ember)}
.motto{font-family:var(--mono);font-size:10px;color:var(--stone);letter-spacing:.14em;text-transform:uppercase;padding:0 6px;
  cursor:default;position:relative}
.motto b{color:var(--ember)}
.motto .oneliner{display:block;font-size:9.5px;color:var(--stone);letter-spacing:.02em;text-transform:none;
  margin-top:5px;line-height:1.45;opacity:0;max-height:0;overflow:hidden;transition:opacity .2s ease,max-height .2s ease}
.motto:hover .oneliner{opacity:1;max-height:60px}
.motto .oneliner em{font-style:normal;color:var(--bone-dim)}
/* D2 — Pillar-4 credibility anchor: "read the code" is a receipt, not a
   footnote. Persistent in the sidebar navfoot (first-fold on every route),
   bone-dim lead + ember link phrases + arrow. */
.readcode{display:block;font-family:var(--mono);font-size:10.5px;line-height:1.55;letter-spacing:.02em;
  color:var(--bone-dim);text-decoration:none;padding:0 6px}
.readcode .rc-lead{color:var(--bone-dim)}
.readcode .rc-link{color:var(--ember);text-decoration:none;border-bottom:1px solid transparent;transition:border-color .15s ease}
.readcode .rc-arw{color:var(--ember);font-weight:700}
.readcode:hover .rc-link,.readcode:focus-visible .rc-link{border-bottom-color:var(--ember-soft);color:var(--ember-soft)}
.readcode:focus-visible{outline:2px solid var(--ember);outline-offset:2px;border-radius:4px}
/* main */
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{height:52px;flex:none;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;padding:0 22px;background:var(--ink)}
.crumb{display:flex;align-items:center;gap:8px;font-size:13.5px}
.crumb a{color:var(--stone);text-decoration:none;cursor:pointer;font-weight:600}.crumb a:hover{color:var(--bone-dim)}
.crumb .sep{color:var(--line-strong)}.crumb .cur{color:var(--bone);font-weight:700;font-family:var(--mono);font-size:13px}
.spacer{margin-left:auto}
.tick{color:var(--stone);font-family:var(--mono);font-size:11px;letter-spacing:.02em}
.viewport{flex:1;min-height:0;position:relative;background:var(--ink)}
.view{position:absolute;inset:0;overflow:auto}
.view.hidden{display:none}
/* generic surfaces */
.page{padding:22px 26px;max-width:1180px;margin:0 auto}
.h1{font-family:var(--sans);font-weight:700;font-size:22px;letter-spacing:-.02em;margin:0 0 4px}
.sub{color:var(--stone);font-size:12.5px}
.stone{color:var(--stone)}
.bone-dim{color:var(--bone-dim)}
.ember{color:var(--ember)}.phos{color:var(--phos)}
.kick{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--ember);font-weight:600}
.mono{font-family:var(--mono)}
hr.rule{border:0;border-top:1px solid var(--line);margin:14px 0}
/* pills / badges */
.tag{display:inline-block;font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--bone-dim);
  border:1px solid var(--line-strong);border-radius:var(--r-pill);padding:2px 8px}
/* the active-project scope tag (per-project origin usage, §2) — a quiet accent so the
   scope is legible without shouting; text-transform:none keeps the project name honest. */
.tag.scoped{margin-left:6px;color:var(--accent,#8ab4f8);border-color:var(--accent,#8ab4f8);text-transform:none;letter-spacing:0}
.pill{display:inline-block;font-family:var(--mono);font-size:10.5px;font-weight:600;padding:2px 8px;border-radius:var(--r-pill);
  background:var(--ink-3);color:var(--bone-dim);border:1px solid var(--line)}
.pill.ember{background:rgba(255,106,43,.12);color:var(--ember-soft);border-color:rgba(255,106,43,.35)}
.pill.phos{background:rgba(70,224,160,.10);color:var(--phos);border-color:rgba(70,224,160,.30)}
.pill.rose{background:rgba(224,87,107,.10);color:var(--rose);border-color:rgba(224,87,107,.30)}
.chip{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);font-size:11px;padding:3px 10px;border-radius:var(--r-pill);
  background:var(--ink-3);color:var(--bone-dim);border:1px solid var(--line);white-space:nowrap}
.chip.on{background:rgba(255,106,43,.14);color:var(--ember-soft);border-color:rgba(255,106,43,.35)}
/* Running-loop pulse. The ring is a pseudo-element that animates ONLY
   transform:scale()+opacity — compositor-driven, so it never triggers a paint
   (the old box-shadow pulse forced a full repaint every frame, and Safari
   re-rasterized the tiled dot-grid backing store each time). will-change lifts
   the ring onto its own layer; the solid dot underneath is static. */
.dot{position:relative;width:8px;height:8px;border-radius:50%;background:var(--phos);display:inline-block}
.dot::after{content:"";position:absolute;inset:0;border-radius:50%;background:rgba(70,224,160,.55);
  transform:scale(1);opacity:.55;animation:pulse 2s infinite;will-change:transform,opacity;pointer-events:none}
@keyframes pulse{0%{transform:scale(1);opacity:.55}70%,100%{transform:scale(3.5);opacity:0}}
/* cards / tables */
.card{background:linear-gradient(180deg,var(--ink-2),var(--ink));border:1px solid var(--line);border-radius:var(--r-lg);padding:18px}
.grid{display:grid;gap:14px}
.grid.two{grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
.grid.three{grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}
.table{width:100%;border-collapse:collapse;font-size:13px}
.table th{text-align:left;color:var(--stone);font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;
  font-weight:700;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
.table th.num,.table td.num{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--mono)}
.table td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
.table tr.clk{cursor:pointer}.table tr.clk:hover{background:rgba(255,106,43,.05)}
.table th.sortable{cursor:pointer;user-select:none;transition:color .15s ease;position:relative}
.table th.sortable:hover{color:var(--bone-dim)}
.table th.sortable:hover .sarrow{color:var(--bone-dim)}
.table th.sortable.on{color:var(--ember-soft)}
.table th .sarrow{margin-left:5px;color:var(--stone);font-size:9px;font-weight:600;opacity:.6;transition:color .12s ease,opacity .12s ease}
.table th.sortable.on .sarrow{color:var(--ember);opacity:1}
.aid{font-family:var(--mono);font-weight:700;color:var(--ember-soft)}
.aid.mgr{color:var(--ember)}.aid.sub{color:var(--bone-dim);font-weight:600}
.bar{display:inline-block;height:6px;border-radius:4px;background:var(--ember);vertical-align:middle;margin-right:6px;min-width:2px;opacity:.7}
.star{cursor:pointer;color:var(--stone);font-size:15px;user-select:none;transition:color .15s ease}
.star.on{color:var(--ember)}.star:hover{color:var(--ember-soft)}
/* runs page */
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 16px;align-items:center}
.filters label{font-family:var(--mono);font-size:10.5px;color:var(--stone);letter-spacing:.06em;text-transform:uppercase;margin-right:4px}
.activefilters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 14px;margin:6px 0 12px;
  border:1px solid rgba(255,106,43,.35);border-radius:var(--r);background:rgba(255,106,43,.06)}
.fchip{display:inline-flex;align-items:center;gap:0;background:var(--ink-2);border:1px solid rgba(255,106,43,.32);
  border-radius:var(--r-pill);padding:2px 3px 2px 10px;font-family:var(--mono);font-size:11.5px}
.fchip .fk{color:var(--stone);text-transform:uppercase;letter-spacing:.06em;font-size:10px;margin-right:6px}
.fchip .fv{color:var(--ember-soft);font-weight:700}
.fchip .fx{background:transparent;border:0;color:var(--stone);cursor:pointer;padding:2px 8px;font-size:14px;line-height:1;border-radius:999px}
.fchip .fx:hover{color:var(--rose)}
.select,.input{background:var(--ink-2);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:12.5px;color:var(--bone);
  font-family:var(--mono)}
.select:focus,.input:focus{outline:none;border-color:var(--ember)}
.rowcard{display:grid;grid-template-columns:1.2fr 1fr 0.7fr 0.9fr 0.7fr 0.4fr;gap:14px;align-items:center;padding:14px 18px;
  border:1px solid var(--line);border-radius:var(--r);background:var(--ink-2);margin-bottom:8px;cursor:pointer;transition:border-color .15s ease}
.rowcard:hover{border-color:var(--line-strong)}
.rowcard .rk{font-family:var(--mono);font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--stone);margin-bottom:2px}
.rowcard .rv{font-family:var(--mono);font-size:13px;color:var(--bone);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rowcard .rv.aid{color:var(--ember-soft)}
.rowcard .rv.dim{color:var(--bone-dim);font-weight:500}
/* D3 — fused feed: loop-runs carry an ember left-accent so they read as the
   primitive without leaving the one list. */
.rowcard.loopcard{border-left:3px solid var(--ember)}
/* D3 — row cross-filter: a clickable origin/project cell scopes the feed. */
.rowcard .rv.xfilter{cursor:pointer;border-bottom:1px dashed transparent;width:fit-content}
.rowcard .rv.xfilter:hover{color:var(--ember-soft);border-bottom-color:var(--ember-soft)}
/* D3 — freshness pulse: a run that lands while you're watching fades from a
   phosphor wash over 1.6s (>= the 1.5s the brief asks for). */
@keyframes runfresh{0%{background:rgba(70,224,160,.16);border-color:rgba(70,224,160,.45)}100%{background:var(--ink-2);border-color:var(--line)}}
.rowcard.fresh{animation:runfresh 1.6s ease-out}
@media (prefers-reduced-motion:reduce){.rowcard.fresh{animation:none}}
.emptyline{padding:44px 16px;text-align:center;color:var(--stone);font-size:13px;
  background-image:radial-gradient(rgba(242,236,223,0.05) 1px, transparent 1.4px);
  background-size:26px 26px;background-position:-13px -13px;
  border:1px dashed var(--line);border-radius:14px;margin:8px 0}
.emptyline .kick{display:block;margin-bottom:8px}
.emptyline b{color:var(--bone-dim)}
/* loops list + detail (two-pane) */
.two-pane{display:grid;grid-template-columns:340px 1fr;height:100%;min-height:0}
.two-pane .list{border-right:1px solid var(--line);overflow-y:auto;padding:14px;background:var(--ink)}
.two-pane .detail{overflow-y:auto;padding:22px 28px}
.lcard{background:var(--ink-2);border:1px solid var(--line);border-radius:var(--r);padding:12px 14px;margin-bottom:9px;cursor:pointer;
  transition:border-color .15s ease,background .15s ease}
.lcard:hover{border-color:var(--line-strong)}
.lcard.sel{border-color:var(--ember);background:linear-gradient(180deg,rgba(255,106,43,.06),transparent)}
.lcard.arch{opacity:.45}
/* SLICE-1B §3.1 — product group header in the loops list (additive) */
.lgrp{display:flex;align-items:baseline;gap:8px;margin:14px 0 7px;padding:0 2px;cursor:default;
  font-family:var(--mono);font-size:10.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase}
.lgrp:first-child{margin-top:2px}
.lgrp .lgrp-name{color:var(--ember-soft)}
.lgrp .lgrp-cnt{color:var(--stone);font-weight:600;font-size:10px}
.lgrp::after{content:"";flex:1;height:1px;background:var(--line);align-self:center;margin-left:2px}
.lcard .top{display:flex;align-items:center;gap:8px;margin-bottom:5px}
.host{font-family:var(--mono);font-size:9.5px;font-weight:700;padding:2px 7px;border-radius:4px;background:rgba(70,224,160,.10);
  color:var(--phos);letter-spacing:.06em;text-transform:uppercase;border:1px solid rgba(70,224,160,.28)}
.host.anneke{background:rgba(255,106,43,.10);color:var(--ember-soft);border-color:rgba(255,106,43,.28)}
.lname{font-family:var(--mono);font-weight:700;font-size:13px;color:var(--bone)}
.badge{font-family:var(--mono);font-size:9.5px;font-weight:800;letter-spacing:.04em;padding:2px 7px;border-radius:5px;text-transform:uppercase;margin-left:auto}
.b-running{background:rgba(70,224,160,.14);color:var(--phos);border:1px solid rgba(70,224,160,.32)}
.b-waiting_owner{background:rgba(255,106,43,.14);color:var(--ember-soft);border:1px solid rgba(255,106,43,.32)}
.b-needs_owner,.b-error,.b-guardian_stopped{background:rgba(224,87,107,.14);color:var(--rose);border:1px solid rgba(224,87,107,.32)}
.b-finished,.b-complete{background:rgba(70,224,160,.10);color:var(--phos);border:1px solid rgba(70,224,160,.24)}
.b-saved,.b-stopped,.b-stopping{background:var(--ink-3);color:var(--bone-dim);border:1px solid var(--line-strong)}
.meta{color:var(--stone);font-size:11.5px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-family:var(--mono)}
.team{font-size:11px;color:var(--stone);margin-top:5px}
.team b{color:var(--bone-dim);font-family:var(--mono)}
/* D-FE: project/origin binding chips (loop card + detail; nothing when unbound) */
.binds{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.bindchip{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);font-size:10.5px;font-weight:700;
  letter-spacing:.01em;padding:2px 8px;border-radius:var(--r-pill);border:1px solid var(--line-strong);
  color:var(--bone-dim);background:var(--ink-3);cursor:pointer;transition:border-color .12s,color .12s}
.bindchip:hover{border-color:var(--ember);color:var(--bone)}
.bindchip.prod{color:var(--ember-soft)}
.bindchip.orig{color:var(--phos)}
.bindchip.unbound{color:var(--stone);cursor:default;border-style:dashed}
.bindchip.unbound:hover{border-color:var(--line-strong);color:var(--stone)}
/* reverse lists on Projects/Origins: 'loops targeting this …' */
.boundloops{display:flex;gap:6px;flex-wrap:wrap;margin-top:2px}
.boundloop{font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:var(--r-pill);
  border:1px solid var(--line);background:var(--ink-3);color:var(--bone-dim);cursor:pointer}
.boundloop:hover{border-color:var(--ember);color:var(--bone)}
.boundnone{color:var(--stone);font-size:11px;font-style:italic}
/* loop detail */
.empty{color:var(--stone);text-align:center;margin-top:80px;font-family:var(--mono);font-size:12px;letter-spacing:.04em}
.dhead{display:flex;align-items:center;gap:10px;margin-bottom:6px;flex-wrap:wrap}.dhead .lname{font-size:19px}
.goal{color:var(--bone-dim);font-size:13.5px;line-height:1.55;margin:2px 0 14px;max-width:820px}
/* REDESIGN-SPEC §5.3 — the loop-detail as a TEAM ROOM: (A) goal+convergence
   header, (B) the team roster (the spine), Engine-activity demoted to a drawer. */
.trm{margin:2px 0 16px}
.trm-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
/* The single COMMITTED convergence chip. GREEN is reserved for Delivered ONLY
   (dev-1's computed rule — a stop/error can never read green, §5.4 honesty). */
.conv{font-family:var(--mono);font-size:11px;font-weight:800;letter-spacing:.03em;padding:4px 10px;border-radius:var(--r-pill);
  text-transform:uppercase;border:1px solid var(--line-strong);background:var(--ink-2);color:var(--bone)}
.conv-delivered{background:rgba(70,224,160,.16);color:var(--phos);border-color:rgba(70,224,160,.4)}
.conv-converging{background:rgba(70,160,224,.14);color:#8ec7f2;border-color:rgba(70,160,224,.36)}
.conv-aligning{background:var(--ink-2);color:var(--bone-dim);border-color:var(--line-strong)}
.conv-stalled{background:rgba(255,106,43,.12);color:var(--ember-soft);border-color:rgba(255,106,43,.36)}
.conv-needsyou{background:rgba(224,87,107,.14);color:var(--rose);border-color:rgba(224,87,107,.4)}
.trm-bar{font-family:var(--mono);font-size:11.5px;color:var(--bone-dim);letter-spacing:.02em}
.trm-bar .wd{color:var(--ember-soft)}
.mgrread{color:var(--bone-dim);font-size:12.5px;line-height:1.5;margin:0 0 12px;padding-left:10px;border-left:2px solid var(--line-strong)}
.mgrread b{color:var(--bone);font-family:var(--mono);font-size:10.5px;letter-spacing:.03em;text-transform:uppercase;font-weight:800}
.roster{display:grid;grid-template-columns:1fr;gap:8px}
.acard{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;padding:9px 12px;border:1px solid var(--line);border-radius:var(--r);
  background:var(--ink-2);cursor:pointer;transition:border-color .15s ease,transform .12s ease}
.acard:hover{border-color:var(--bone-dim);transform:translateY(-1px)}
.acard.mgr{border-color:rgba(255,106,43,.3);background:rgba(255,106,43,.05)}
.acard .adot{grid-row:1/3;align-self:start;margin-top:4px;width:9px;height:9px;border-radius:50%;background:var(--line-strong)}
.adot-active{background:var(--phos);box-shadow:0 0 0 3px rgba(70,224,160,.16)}
.adot-done{background:rgba(70,224,160,.55)}
.adot-attention{background:var(--rose);box-shadow:0 0 0 3px rgba(224,87,107,.16)}
.adot-idle{background:var(--line-strong)}
.acard .atop{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.acard .arole{font-family:var(--mono);font-size:9.5px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--ember-soft)}
.acard.mgr .arole{color:var(--ember)}
.acard .aname{font-weight:700;font-size:13px;color:var(--bone)}
.acard .anow{font-family:var(--mono);font-size:10.5px;color:var(--phos);margin-left:auto}
.acard .aowns{font-size:11.5px;color:var(--bone-dim)}
.acard .alast{font-size:11.5px;color:var(--bone-dim);margin-top:3px;line-height:1.45;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.acard .alast .lk{font-family:var(--mono);font-size:9.5px;font-weight:800;text-transform:uppercase;letter-spacing:.03em;color:var(--bone);margin-right:5px}
/* REDESIGN-SPEC §5.4 — the closing RESOLUTION CARD. When a loop ends, the TOP of
   the same pane resolves into this: (1) a COMPUTED honest verdict — green ONLY on
   a real positive signal (dev-1's `verdict.green`; a stop/error is red/amber and
   offers Respawn/Take over), (2) the resolution in one line (the crew's answer,
   not a filename), (3) the real handles + a compact proof strip, then the gated
   good/ok/bad disposition. We PAINT dev-1's verdict; we never invent a green path. */
.rescard{border:1px solid var(--line-strong);border-radius:var(--r);background:var(--ink-2);
  padding:13px 15px;margin:2px 0 16px;position:relative}
.rescard.green{border-color:rgba(70,224,160,.42);background:rgba(70,224,160,.06)}
.rescard.amber{border-color:rgba(255,106,43,.4);background:rgba(255,106,43,.05)}
.rescard.red{border-color:rgba(224,87,107,.42);background:rgba(224,87,107,.06)}
.rescard .rc-top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.rc-verdict{font-family:var(--mono);font-size:12px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;
  padding:4px 11px;border-radius:var(--r-pill);border:1px solid var(--line-strong);background:var(--ink);color:var(--bone)}
.rescard.green .rc-verdict{background:rgba(70,224,160,.18);color:var(--phos);border-color:rgba(70,224,160,.45)}
.rescard.amber .rc-verdict{background:rgba(255,106,43,.14);color:var(--ember-soft);border-color:rgba(255,106,43,.42)}
.rescard.red .rc-verdict{background:rgba(224,87,107,.16);color:var(--rose);border-color:rgba(224,87,107,.45)}
.rc-line{font-size:13.5px;line-height:1.5;color:var(--bone);margin:2px 0 4px;font-weight:600}
.rc-reason{font-size:11.5px;color:var(--bone-dim);margin-bottom:8px}
.rc-handles{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0 2px}
.rc-handle{font-family:var(--mono);font-size:11px;color:var(--bone-dim);background:var(--ink);border:1px solid var(--line);
  border-radius:var(--r-pill);padding:3px 9px;display:inline-flex;align-items:center;gap:5px}
.rc-handle .hk{color:var(--ember-soft);font-weight:800;font-size:9.5px;letter-spacing:.05em;text-transform:uppercase}
.rc-handle a{color:var(--bone);text-decoration:none}.rc-handle a:hover{color:var(--ember)}
.rc-proof{font-family:var(--mono);font-size:10.5px;color:var(--bone-dim);letter-spacing:.02em;margin-top:8px;
  display:flex;gap:12px;flex-wrap:wrap}
.rc-proof .pk{color:var(--bone)}
.rc-proof .red{color:var(--rose)}
.rc-disp{margin-top:11px;padding-top:10px;border-top:1px solid var(--line);display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.rc-disp .dq{font-family:var(--mono);font-size:10.5px;font-weight:800;letter-spacing:.03em;text-transform:uppercase;color:var(--bone-dim);margin-right:4px}
.rc-rate{font-family:var(--mono);font-size:11.5px;font-weight:700;padding:3px 11px;border-radius:var(--r-pill);
  border:1px solid var(--line-strong);background:var(--ink);color:var(--bone-dim);cursor:pointer;transition:border-color .15s,background .15s}
.rc-rate:hover{border-color:var(--bone-dim);color:var(--bone)}
.rc-rate.on-good{background:rgba(70,224,160,.16);color:var(--phos);border-color:rgba(70,224,160,.4)}
.rc-rate.on-ok{background:var(--ink-2);color:var(--bone);border-color:var(--line-strong)}
.rc-rate.on-bad{background:rgba(224,87,107,.14);color:var(--rose);border-color:rgba(224,87,107,.4)}
.rc-why{font-family:var(--sans);font-size:12px;padding:4px 9px;border-radius:8px;border:1px solid var(--line-strong);
  background:var(--ink);color:var(--bone);min-width:150px;flex:1 1 160px;max-width:340px}
.rc-nextact{font-size:11.5px;color:var(--rose);margin-top:6px;flex-basis:100%}
.rc-current{font-size:11.5px;color:var(--bone-dim);flex-basis:100%;margin-bottom:2px}
.rc-current b{color:var(--bone)}
/* the disposition verb echoed back on a Loops list card (§5.4 "shown back on loop
   cards"): a tiny how-it-landed dot, never louder than the state badge. */
.dispflag{font-family:var(--mono);font-size:9.5px;font-weight:800;letter-spacing:.03em;text-transform:uppercase;
  padding:1px 6px;border-radius:var(--r-pill);border:1px solid var(--line)}
.dispflag.good{color:var(--phos);border-color:rgba(70,224,160,.4);background:rgba(70,224,160,.1)}
.dispflag.ok{color:var(--bone-dim);border-color:var(--line-strong)}
.dispflag.bad{color:var(--rose);border-color:rgba(224,87,107,.4);background:rgba(224,87,107,.1)}
/* per-project disposition rollup line under the Loops header (§5.4 aggregate). */
.disproll{font-family:var(--mono);font-size:11px;color:var(--bone-dim);margin:2px 0 8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.disproll .rg{color:var(--phos);font-weight:800}
.disproll .rg.low{color:var(--rose)}
.engdraw{margin-top:16px}
.engdraw>summary{cursor:pointer;font-family:var(--mono);font-size:10.5px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;
  color:var(--bone-dim);list-style:none;user-select:none;padding:6px 0}
.engdraw>summary::-webkit-details-marker{display:none}
.engdraw>summary::before{content:'▸ ';color:var(--ember-soft)}
.engdraw[open]>summary::before{content:'▾ '}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
.btn{font-family:var(--mono);font-size:12px;font-weight:700;padding:7px 13px;border-radius:var(--r-pill);border:1px solid var(--line-strong);
  background:var(--ink-2);color:var(--bone);cursor:pointer;display:inline-flex;align-items:center;gap:6px;letter-spacing:.02em;
  transition:transform .12s ease,border-color .15s ease,background .15s ease}
.btn:hover{border-color:var(--bone-dim);transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn:disabled{opacity:.5;cursor:default;transform:none}
.btn.primary{background:var(--ember);border-color:transparent;color:var(--on-ember);box-shadow:0 8px 22px -12px var(--ember-deep)}
.btn.primary:hover{background:var(--ember-soft)}
.btn.stop{background:rgba(224,87,107,.14);border-color:rgba(224,87,107,.45);color:var(--rose)}
.btn.go{background:rgba(70,224,160,.14);border-color:rgba(70,224,160,.4);color:var(--phos)}
.btn.ghost{background:transparent}
/* Cross-page pivot chip — Origins → Runs, Projects → Runs, etc. Ember-tinted
   outline says "click me to travel" without shouting like .btn.primary. */
.btn.pivot{background:transparent;border-color:rgba(255,106,43,.32);color:var(--ember-soft);
  padding:5px 12px;font-size:11.5px;letter-spacing:.02em}
.btn.pivot:hover{background:rgba(255,106,43,.08);border-color:var(--ember);color:var(--ember)}
.btn.pivot .parr{color:var(--ember);font-weight:800}
.ibtn{font-family:var(--sans);font-size:13px;font-weight:700;height:34px;min-width:34px;padding:0 12px;border-radius:9px;
  border:1px solid var(--line-strong);background:var(--ink-2);color:var(--bone);cursor:pointer;
  display:inline-flex;align-items:center;justify-content:center;gap:6px}
.ibtn span{font-size:12px;font-family:var(--mono)}
.ibtn:hover{border-color:var(--bone-dim)}.ibtn:disabled{opacity:.5;cursor:default}
.ibtn.go{background:rgba(70,224,160,.14);border-color:rgba(70,224,160,.4);color:var(--phos)}
.ibtn.stop{background:rgba(224,87,107,.14);border-color:rgba(224,87,107,.45);color:var(--rose)}
/* topbar refresh — small, quiet, ember on hover. Not an .ibtn so it doesn't
   compete with the run/stop actions in the loop-detail row. */
.refreshbtn{height:30px;width:30px;padding:0;border-radius:999px;border:1px solid var(--line);background:transparent;
  color:var(--stone);cursor:pointer;font-size:14px;display:inline-flex;align-items:center;justify-content:center;
  transition:color .15s ease,border-color .15s ease,transform .35s ease}
.refreshbtn:hover{color:var(--ember);border-color:rgba(255,106,43,.4);transform:rotate(60deg)}
.refreshbtn:active{transform:rotate(180deg)}
/* Help/informational sibling to .refreshbtn — same size + shape so the topbar
   reads as a paired trio (tick / refresh / help), but no rotate: '?' shouldn't
   spin on hover. Soft ember lift instead. */
.helpbtn{height:30px;width:30px;padding:0;border-radius:999px;border:1px solid var(--line);background:transparent;
  color:var(--stone);cursor:pointer;font-size:13px;font-weight:700;font-family:var(--sans);
  display:inline-flex;align-items:center;justify-content:center;
  transition:color .15s ease,border-color .15s ease,background .15s ease}
.helpbtn:hover{color:var(--ember);border-color:rgba(255,106,43,.4);background:rgba(255,106,43,.06)}
/* ── §2/§7 — the ambient Fleet pill + its popover (rd-origins). Quiet by default,
   an amber ring when an origin running a loop drops. ── */
.fleetpill{display:inline-flex;align-items:center;gap:7px;height:30px;padding:0 12px;border-radius:999px;
  border:1px solid var(--line);background:var(--ink-2);color:var(--bone-dim);cursor:pointer;font-size:12px;
  font-family:var(--sans);transition:border-color .15s ease,color .15s ease,background .15s ease}
.fleetpill:hover{border-color:rgba(255,106,43,.35);color:var(--bone)}
.fleetpill .fpico{color:var(--stone)}
.fleetpill .fpdots{display:inline-flex;gap:2px;letter-spacing:0}
.fleetpill .fptxt{font-variant-numeric:tabular-nums;white-space:nowrap}
.fleetpill.drop{border-color:rgba(255,160,107,.55);color:var(--ember-soft)}
.fleetpill.drop .fpico{color:var(--ember)}
.fd{font-size:9px;line-height:1}
.fd.on{color:var(--phos)} .fd.off{color:var(--stone)} .fd.amber{color:var(--ember-soft)} .fd.more{color:var(--stone)}
.fleetpop{position:absolute;top:48px;right:64px;z-index:60;min-width:280px;max-width:360px;
  background:var(--ink-2);border:1px solid var(--line-strong);border-radius:var(--r-lg,12px);
  box-shadow:0 18px 44px rgba(0,0,0,.5);padding:12px 14px}
.fleetpop .fphead{font-weight:700;color:var(--bone);margin-bottom:8px;font-variant-numeric:tabular-nums}
.fleetpop .fpdrop{color:var(--ember-soft);font-size:12px;margin-bottom:8px}
.fleetpop .fprow{display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px}
.fleetpop .fpn{color:var(--bone-dim);font-family:var(--mono)}
.fleetpop .fpm{margin-left:auto;font-size:11px}
.fleetpop .fpfoot{margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}
.ilink{color:var(--ember-soft);cursor:pointer;border-bottom:1px dotted rgba(255,160,107,.4)}
.ilink:hover{color:var(--ember)}
/* ── §7 — Sessions roster cards (rd-sessions) ── */
.sessgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;margin-top:14px}
.sesscard{background:linear-gradient(180deg,var(--ink-2),var(--ink));border:1px solid var(--line);
  border-radius:var(--r-lg,12px);padding:14px 16px}
.sesscard.stale{opacity:.75}
.sesshd{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.sdot{width:9px;height:9px;border-radius:50%;flex:none}
.sdot.on{background:var(--phos);box-shadow:0 0 8px rgba(70,224,160,.5)} .sdot.off{background:var(--stone)}
.sesscard .sname{font-weight:700;color:var(--bone)}
.pill2.rt{text-transform:uppercase;letter-spacing:.04em;font-size:10px}
.shb{font-size:11px;color:var(--stone)} .shb.live{color:var(--phos)}
.sesscaps{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}
.sessrow{display:flex;gap:8px;font-size:12px;padding:3px 0;color:var(--bone-dim);align-items:baseline}
.sessrow .sk{color:var(--stone);min-width:64px;flex:none;text-transform:uppercase;font-size:10px;letter-spacing:.04em}
/* ── §7 — Overview tiles (rd-projects) ── */
.ovtiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}
.ovtile{background:var(--ink-2);border:1px solid var(--line);border-radius:var(--r-lg,12px);padding:16px 18px;cursor:pointer;
  transition:border-color .15s ease,transform .15s ease}
.ovtile:hover{border-color:rgba(255,106,43,.35);transform:translateY(-1px)}
.ovtile .ovn{font-size:26px;font-weight:800;color:var(--bone);font-variant-numeric:tabular-nums;line-height:1}
.ovtile .ovl{color:var(--bone-dim);margin-top:4px}
.ovtile .ovsub{font-size:11px;margin-top:2px}
.ovsec{margin:18px 0}
.ovsec .ovh{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--stone);margin-bottom:6px}
.ovres .ovresrow{display:flex;align-items:baseline;gap:8px;padding:5px 0;cursor:pointer;font-size:13px;border-bottom:1px solid var(--line)}
.ovloops{display:flex;flex-wrap:wrap;gap:6px}
.drverdict.green{color:var(--phos)} .drverdict.nongreen{color:var(--ember-soft)}
/* §6 — per-doc timeline agent badge: a loop rewrite is visibly distinct from a human edit */
.dhbadge{display:inline-flex;align-items:center;gap:4px;font-size:11px;padding:1px 7px;border-radius:999px;
  border:1px solid var(--line);font-family:var(--mono);white-space:nowrap}
.dhbadge.loop{color:var(--ember-soft);border-color:rgba(255,160,107,.35);background:rgba(255,106,43,.08)}
.dhbadge.you{color:var(--stone)}
/* Physical-key keycap for shortcut docs. Rounded ink-3 pill with a hairline
   inset so it reads like an actual key, not just bold text. */
.kbd{display:inline-block;font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.02em;
  padding:2px 8px;border-radius:6px;background:var(--ink-3);color:var(--bone);
  border:1px solid var(--line-strong);box-shadow:0 1px 0 rgba(0,0,0,0.4), inset 0 -1px 0 rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.04);
  line-height:1.4;min-width:18px;text-align:center;vertical-align:baseline}
/* Page-level search box — Loopyard-tinted, with an inline glyph. Distinct from
   the column-filter row so search reads as a first-class action. */
.searchbox{display:inline-flex;align-items:center;gap:0;background:var(--ink-2);border:1px solid var(--line);border-radius:999px;
  padding:0 4px 0 12px;transition:border-color .15s ease}
.searchbox:focus-within{border-color:var(--ember)}
.searchbox .sglyph{color:var(--stone);font-size:12px;margin-right:6px;user-select:none}
.searchbox input{background:transparent;border:0;outline:none;color:var(--bone);font-family:var(--mono);font-size:12.5px;
  padding:7px 0;min-width:180px;letter-spacing:.02em}
.searchbox input::placeholder{color:var(--stone)}
.searchbox .sclear{background:transparent;border:0;color:var(--stone);cursor:pointer;padding:3px 8px;font-size:14px;line-height:1;border-radius:999px}
.searchbox .sclear:hover{color:var(--rose)}
.searchmeta{font-family:var(--mono);font-size:10.5px;color:var(--stone);letter-spacing:.02em;margin-left:8px}
.flash{position:fixed;left:50%;transform:translateX(-50%);bottom:22px;max-width:70vw;padding:11px 18px;border-radius:12px;
  font-size:13px;z-index:60;box-shadow:0 12px 40px rgba(0,0,0,.5);white-space:pre-wrap;font-family:var(--mono)}
.flash.ok{background:rgba(70,224,160,.12);border:1px solid rgba(70,224,160,.5);color:var(--phos)}
.flash.err{background:rgba(224,87,107,.12);border:1px solid rgba(224,87,107,.5);color:var(--rose);font-size:12px}
.banner{border-radius:var(--r);padding:12px 14px;margin-bottom:14px;font-size:13px;line-height:1.55}
.banner b{font-family:var(--mono);letter-spacing:.02em}
.banner.q{background:rgba(255,106,43,.10);border:1px solid rgba(255,106,43,.35);color:var(--ember-soft)}
.banner.a{background:rgba(224,87,107,.10);border:1px solid rgba(224,87,107,.35);color:var(--rose)}
.banner.live{background:rgba(70,224,160,.08);border:1px solid rgba(70,224,160,.32);display:flex;align-items:center;gap:12px;flex-wrap:wrap;color:var(--bone-dim)}
.banner.live b{color:var(--phos)}
.runchip{font-family:var(--mono);font-size:11px;font-weight:700;background:rgba(70,224,160,.10);color:var(--phos);
  padding:3px 10px;border-radius:var(--r-pill);border:1px solid rgba(70,224,160,.28)}
.pend{background:rgba(255,106,43,.08);border:1px solid rgba(255,106,43,.25);border-radius:var(--r);padding:11px 14px;margin-bottom:14px;font-size:12.5px}
.pend .pi{color:var(--ember-soft);margin-top:3px}
.pend b{color:var(--ember)}
.compose{display:flex;gap:8px;margin-top:8px}
.compose textarea{flex:1;background:var(--ink);border:1px solid var(--line-strong);border-radius:9px;padding:9px 11px;font-size:13px;resize:vertical;min-height:40px;font-family:var(--sans);color:var(--bone)}
.compose textarea:focus{outline:none;border-color:var(--ember)}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.stat{background:var(--ink-2);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:100px}
.stat .k{color:var(--stone);font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.08em;font-weight:600}
.stat .v{font-size:18px;font-weight:700;margin-top:2px;font-family:var(--mono);color:var(--bone)}
.logh{font-family:var(--mono);font-size:11px;color:var(--stone);text-transform:uppercase;letter-spacing:.1em;margin:14px 0 8px;
  display:flex;align-items:center;gap:8px;font-weight:600}
.log{background:var(--ink);border:1px solid var(--line);border-radius:var(--r);padding:6px}
.row{display:flex;gap:10px;padding:6px 10px;border-bottom:1px solid var(--line);align-items:baseline;font-family:var(--mono);font-size:12.5px}
.row:last-child{border-bottom:none}
.row.turn{cursor:pointer;border-radius:6px}.row.turn:hover{background:rgba(255,106,43,.05)}
.row.mach{background:rgba(255,106,43,.04)}
.slgroup{margin:3px 0 3px 8px;border-left:2px solid rgba(255,106,43,.32);background:rgba(255,106,43,.03);border-radius:0 8px 8px 0}
.slhead{display:flex;gap:9px;align-items:center;padding:6px 10px;cursor:pointer;user-select:none}
.slhead:hover{background:rgba(255,106,43,.06)}
.sltri{color:var(--ember);font-size:11px;width:12px;text-align:center}
.slmeta{color:var(--stone);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono)}
.slbody{padding:2px 0 4px}
.ts{color:var(--stone);font-size:11px;white-space:nowrap}
.ag{font-weight:700;min-width:96px;font-family:var(--mono)}
.ag.manager{color:var(--ember)}.ag.worker{color:var(--ember-soft)}.ag.input_provider{color:#F0B47A}
.ag.aglink,.runchip.aglink{cursor:pointer;border-bottom:1px dashed transparent;transition:border-color .12s ease,color .12s ease}
.ag.aglink:hover,.runchip.aglink:hover{border-bottom-color:var(--ember);color:var(--ember-soft)}
.ag.service,.ag.group,.ag.loop{color:var(--ember-soft)}
.ag.nested{color:var(--bone-dim)}
.st{font-family:var(--mono);font-weight:700;font-size:10.5px;padding:1px 7px;border-radius:4px;white-space:nowrap}
.st-completed,.st-satisfied,.st-continue,.st-complete{background:rgba(70,224,160,.14);color:var(--phos)}
.st-work_remaining,.st-needs_work,.st-minor_only{background:rgba(255,106,43,.14);color:var(--ember-soft)}
.st-timeout,.st-give_up,.st-ask_owner,.st-wind_down{background:rgba(224,87,107,.14);color:var(--rose)}
.st-retired,.st-briefed,.st-parallel,.st-serialized,.st-subloop_start,.st-subloop_complete,.st-subloop_finished{background:rgba(255,106,43,.1);color:var(--ember-soft)}
/* Q1 guardian-recovery events: a distinct 🛡 row so every attempt is visible */
.row.mach.guardian{background:rgba(224,87,107,.06);border-left:2px solid rgba(224,87,107,.32)}
.ag.guardian{color:var(--rose);font-weight:700}
.gtarget{font-family:var(--mono);font-size:11px;color:var(--stone)}
.st-guardian{background:rgba(224,87,107,.14);color:var(--rose)}
.st-g-give_up{background:rgba(224,87,107,.22);color:var(--rose);font-weight:800}
.note{color:var(--bone);opacity:.86}.ago{color:var(--stone)}
.chev{color:var(--stone);font-size:10px}
.parbox{margin-left:20px;border-left:2px solid rgba(70,224,160,.32);padding:4px 8px;background:rgba(70,224,160,.03)}
/* origins page */
.ocard{background:var(--ink-2);border:1px solid var(--line);border-radius:var(--r-lg);padding:18px 20px;display:flex;flex-direction:column;gap:8px}
.ocard.local{border-color:rgba(70,224,160,.32)}
.ocard .obound{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:2px;padding-top:8px;border-top:1px solid var(--line)}
.ocard .oh{display:flex;align-items:center;gap:10px}
.ocard .oname{font-family:var(--mono);font-weight:700;font-size:15px;color:var(--bone);letter-spacing:-.01em}
.ocard .oid{font-family:var(--mono);font-size:11px;color:var(--stone)}
.ocard .opath{font-family:var(--mono);font-size:11px;color:var(--stone);word-break:break-all}
.ocard .ometa{display:flex;gap:8px;flex-wrap:wrap}
.health{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);font-size:10.5px;font-weight:700;
  padding:3px 9px;border-radius:var(--r-pill);letter-spacing:.02em;text-transform:uppercase}
.health.fresh{background:rgba(70,224,160,.12);color:var(--phos);border:1px solid rgba(70,224,160,.3)}
.health.stale{background:rgba(255,106,43,.12);color:var(--ember-soft);border:1px solid rgba(255,106,43,.3)}
.health.cold,.health.empty{background:var(--ink-3);color:var(--stone);border:1px solid var(--line-strong)}
/* D5 — health-pill three-state (live / stale / off), Portainer-style. An
   unreachable origin doesn't vanish: it stays in the grid, dimmed, with the
   .off pill and a one-line cause. */
.health-pill{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10.5px;font-weight:700;
  letter-spacing:.04em;text-transform:uppercase;padding:3px 10px;border-radius:var(--r-pill)}
.health-pill::before{content:"";width:7px;height:7px;border-radius:50%}
.health-pill.live{background:rgba(70,224,160,.12);color:var(--phos);border:1px solid rgba(70,224,160,.3)}
.health-pill.live::before{background:var(--phos);box-shadow:0 0 6px var(--phos)}
.health-pill.stale{background:rgba(255,106,43,.12);color:var(--ember-soft);border:1px solid rgba(255,106,43,.3)}
.health-pill.stale::before{background:var(--ember-soft)}
.health-pill.off{background:var(--ink-3);color:var(--stone);border:1px solid var(--line-strong)}
.health-pill.off::before{background:var(--stone)}
/* D6 — a box added via `yard connect` but not yet synced: pending, not off. */
.health-pill.pending{background:rgba(255,160,107,.10);color:var(--ember-soft);border:1px solid rgba(255,160,107,.3)}
.health-pill.pending::before{background:var(--ember-soft)}
.ocard.off{opacity:.62}
.ocard.pending{border-left:3px solid var(--ember-soft)}
.ocard .ocause{font-family:var(--mono);font-size:11px;color:var(--ember-soft)}
.ocard .ocause.pending{color:var(--bone-dim)}
/* D6 — copy-paste command box (the ONLY thing the Add-origin modal offers). */
.cmdbox{display:flex;align-items:center;gap:10px;margin:12px 0 2px;padding:11px 14px;background:var(--ink);
  border:1px solid var(--line-strong);border-radius:var(--r);}
.cmdbox code{font-family:var(--mono);font-size:13.5px;color:var(--phos);flex:1;overflow-x:auto;white-space:nowrap}
.cmdbox .btn{flex:none}
/* projects page */
.prow{display:flex;align-items:center;gap:14px;padding:12px 16px;border:1px solid var(--line);border-radius:var(--r);background:var(--ink-2);margin-bottom:8px;flex-wrap:wrap}
.prow .pbound{flex-basis:100%;display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  margin-top:2px;padding-top:8px;border-top:1px solid var(--line)}
.prow .pid{font-family:var(--mono);font-weight:700;color:var(--ember-soft);font-size:14px}
.prow .pmeta{color:var(--bone-dim);font-size:12.5px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono)}
.pform{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:8px 0 14px}
.pform label{display:flex;flex-direction:column;gap:5px;font-family:var(--mono);font-size:10px;color:var(--stone);
  text-transform:uppercase;letter-spacing:.08em;font-weight:700}
.pform input,.pform select,.mform input,.mform select,.mform textarea{background:var(--ink);border:1px solid var(--line-strong);border-radius:9px;
  color:var(--bone);font-family:var(--mono);font-size:13px;padding:8px 11px}
.pform input:focus,.pform select:focus,.mform input:focus,.mform select:focus{outline:none;border-color:var(--ember)}
.pform .full{grid-column:1/-1}
.mform{display:flex;flex-direction:column;gap:12px;margin:12px 0}
.mform label{display:flex;flex-direction:column;gap:5px;font-family:var(--mono);font-size:10px;color:var(--stone);
  text-transform:uppercase;letter-spacing:.08em;font-weight:700}
.hint{font-size:11.5px;color:var(--stone);margin:2px 0 10px;line-height:1.5}
.kv{display:flex;gap:8px;font-size:12.5px;padding:3px 0;font-family:var(--mono)}.kv .kk{color:var(--stone);min-width:96px;text-transform:uppercase;font-size:10.5px;letter-spacing:.08em}
.warnpill{font-family:var(--mono);font-size:10.5px;color:var(--ember-soft)}
/* agents / registry */
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:14px}
.tab{padding:9px 16px;cursor:pointer;font-family:var(--mono);font-size:12px;font-weight:700;color:var(--stone);
  border-bottom:2px solid transparent;margin-bottom:-1px;letter-spacing:.04em;text-transform:uppercase}
.tab:hover{color:var(--bone-dim)}.tab.on{color:var(--bone);border-bottom-color:var(--ember)}
.tab .cnt{font-size:10.5px;color:var(--stone);font-weight:600;margin-left:6px}
.pill2{display:inline-block;font-family:var(--mono);font-size:10.5px;font-weight:700;padding:1px 8px;border-radius:var(--r-pill);
  background:rgba(255,106,43,.10);color:var(--ember-soft);margin:0 3px 3px 0;border:1px solid rgba(255,106,43,.25)}
.chips{display:flex;flex-wrap:wrap;gap:3px;max-width:340px}
.anote{color:var(--bone-dim);font-size:12px;margin:2px 0 14px;line-height:1.55}
.backlink{color:var(--stone);cursor:pointer;font-size:12.5px;display:inline-flex;align-items:center;gap:5px;margin-bottom:14px;font-family:var(--mono);letter-spacing:.02em}
.backlink:hover{color:var(--bone-dim)}
/* Issues view (Q3) — terminal-failure surface. Rose-toned to read as "fault",
   distinct from the ember accent used for active/healthy state. */
.crumbrow{margin-bottom:10px}
.ichip{display:inline-flex;align-items:center;font-family:var(--mono);font-size:10.5px;font-weight:700;padding:2px 9px;border-radius:var(--r-pill);
  color:var(--bone-dim);background:var(--ink-3);border:1px solid var(--line);text-transform:lowercase;letter-spacing:.02em}
.ichip.gup{color:#FF8A8A;background:rgba(255,90,90,.12);border-color:rgba(255,90,90,.32)}
.ichip.err{color:#FFC46B;background:rgba(255,150,60,.12);border-color:rgba(255,150,60,.30)}
.imeta{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 4px}
.attlist{display:flex;flex-direction:column;gap:6px}
.attrow{display:flex;align-items:baseline;gap:10px;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--ink-2)}
.attrow .attag{color:var(--bone-dim);font-size:12px}
.attrow .attnote{color:var(--stone);font-size:12.5px;flex:1;min-width:0;word-break:break-word}
.attrow .attts{color:var(--stone);font-size:10.5px;margin-left:auto;white-space:nowrap}
.logbox{background:var(--ink);border:1px solid var(--line);border-radius:9px;padding:11px 13px;margin:0 0 4px;
  font-family:var(--mono);font-size:11.5px;line-height:1.55;color:var(--bone-dim);white-space:pre-wrap;word-break:break-word;
  max-height:340px;overflow:auto}
.logbox.err{color:#FF9E9E;border-color:rgba(255,90,90,.28)}
.tri{color:var(--stone);font-size:10px;width:11px;display:inline-block;transition:transform .12s}
.tri.open{transform:rotate(90deg)}
.linkbtn{color:var(--ember-soft);cursor:pointer;font-size:12.5px;font-weight:700;text-decoration:none;font-family:var(--mono);letter-spacing:.02em}
.linkbtn:hover{color:var(--ember)}
.xpbox{padding:12px 16px 14px;background:rgba(255,106,43,.04);border-bottom:1px solid var(--line)}
.xpbox .stats{gap:12px;margin-bottom:12px}.xpbox .stat{min-width:78px;padding:6px 12px}.xpbox .stat .v{font-size:15px}
.xpact{display:flex;gap:12px;margin-top:10px;align-items:center}
/* agent identity card */
.idcard{background:linear-gradient(180deg,var(--ink-2),var(--ink));border:1px solid var(--line);border-radius:var(--r-lg);padding:16px 20px;margin:6px 0 4px}
.idhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.idver{margin-left:auto;color:var(--stone);font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.04em}
.idnote{font-family:var(--mono);font-size:10px;color:var(--stone);font-style:italic;letter-spacing:.02em}
.v2tag{font-family:var(--mono);font-size:9.5px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;padding:2px 8px;border-radius:var(--r-pill);
  background:rgba(255,106,43,.12);color:var(--ember-soft);border:1px solid rgba(255,106,43,.30)}
.vmodel{font-family:var(--mono);font-size:11px;font-weight:700;padding:2px 9px;border-radius:var(--r-pill);background:var(--ink-3);color:var(--bone);
  border:1px solid var(--line-strong);white-space:nowrap}
.vmodel.sm{font-size:10px;padding:1px 8px}
.vmodel.vinherit{color:var(--stone)}
.vrole{font-family:var(--mono);font-size:9.5px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;padding:2px 8px;border-radius:5px;
  background:var(--ink-3);color:var(--stone);border:1px solid var(--line-strong)}
.vk{color:var(--stone);font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:5px;font-weight:700}
.vpre{background:var(--ink);border:1px solid var(--line);border-radius:9px;padding:11px 13px;font-family:var(--sans);font-size:13px;line-height:1.55;
  white-space:pre-wrap;color:var(--bone-dim);max-height:220px;overflow:auto}
.vtl{display:flex;flex-direction:column;gap:5px}
.vrow{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:9px 12px;border:1px solid var(--line);border-radius:9px;
  background:var(--ink-2);cursor:pointer;user-select:none}
.vrow:hover{border-color:var(--line-strong)}
.vrow.exp{border-color:var(--line-strong);background:var(--ink-3)}
.vrow.ishead{border-left:3px solid var(--ember)}
.vtag{font-family:var(--mono);font-size:11.5px;font-weight:700;color:var(--stone)}
.vtag.head{color:var(--ember)}
.vsrc{font-size:12px;color:var(--bone-dim)}
.vchg{font-family:var(--mono);font-size:10px;font-weight:700;padding:1px 7px;border-radius:var(--r-pill);background:var(--ink-3);color:var(--stone);border:1px solid var(--line-strong)}
.vwhen{margin-left:auto;font-size:10.5px;color:var(--stone);font-family:var(--mono)}
.vbody{margin:-2px 2px 2px;padding:11px 13px;border:1px solid var(--line);border-top:none;border-radius:0 0 9px 9px;background:var(--ink)}
.vnote{font-size:12.5px;color:var(--bone-dim);font-style:italic;margin-bottom:8px}
/* modal (slide-over) */
.ovl{position:fixed;inset:0;background:rgba(9,7,4,.6);z-index:50;display:flex;justify-content:flex-end;animation:fade .15s ease}
@keyframes fade{from{opacity:0}to{opacity:1}}
.modal{position:relative;background:var(--ink-2);border-left:1px solid var(--line);width:min(780px,94vw);height:100%;
  padding:24px 30px;overflow-y:auto;box-shadow:-24px 0 60px rgba(0,0,0,.55);animation:slidein .22s cubic-bezier(.2,.7,.3,1)}
@keyframes slidein{from{transform:translateX(56px);opacity:.3}to{transform:translateX(0);opacity:1}}
.modal h2{margin:0 4px 4px 0;font-family:var(--sans);font-size:19px;font-weight:700;letter-spacing:-.02em;padding-right:36px;color:var(--bone);line-height:1.2}
.modal h3{margin:0 0 4px;font-family:var(--sans);font-size:15px;font-weight:700;padding-right:36px}
.modal .x{position:absolute;top:14px;right:14px;cursor:pointer;color:var(--stone);width:32px;height:32px;
  display:inline-flex;align-items:center;justify-content:center;border-radius:999px;font-size:22px;line-height:1;z-index:2;
  transition:color .15s ease,background .15s ease,border-color .15s ease;border:1px solid transparent}
.modal .x:hover{color:var(--bone);background:var(--ink-3);border-color:var(--line-strong)}
.pre{background:var(--ink);border:1px solid var(--line);border-radius:10px;padding:12px;font-family:var(--mono);font-size:12px;
  white-space:pre-wrap;max-height:340px;overflow:auto;color:var(--bone-dim);line-height:1.55}
.ov{background:rgba(255,106,43,.06);border:1px solid rgba(255,106,43,.25);border-radius:10px;padding:12px 14px;margin:10px 0;font-size:13px;line-height:1.6;color:var(--bone-dim)}
/* SLICE-1B §3.2 — an errored run renders HONESTLY: a calm failure block that
   LEADS the result (never a green "finished ✅"). Rose-keyed, plain reason + a
   what-to-do-next line. Only shown when the run actually errored. */
.result-fail{background:rgba(224,87,107,.07);border:1px solid rgba(224,87,107,.30);border-radius:10px;padding:12px 14px;margin:8px 0 4px}
.result-fail .rf-h{font-family:var(--mono);font-size:12.5px;font-weight:800;letter-spacing:.02em;color:var(--rose)}
.result-fail .rf-why{margin-top:6px;font-size:13px;line-height:1.55;color:var(--bone-dim);white-space:pre-wrap;word-break:break-word}
.result-fail .rf-next{margin-top:8px;font-size:11.5px;line-height:1.5;color:var(--stone)}
/* editor */
.edform{display:flex;flex-direction:column;gap:6px;margin-top:6px}
/* D-FE: New-Team bindings block (Project + Origin) atop the editor */
.edbind{margin-top:8px;padding:12px 14px;border:1px solid var(--line);border-radius:var(--r);background:var(--ink-2)}
.edbindrow{display:flex;gap:12px;flex-wrap:wrap;margin-top:6px}
.edbl{display:flex;flex-direction:column;gap:4px;flex:1;min-width:180px;color:var(--stone);
  font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.edbl .edin{text-transform:none;letter-spacing:0}
.edl{color:var(--stone);font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-top:10px}
.edagenthead{display:flex;align-items:center;text-transform:none;letter-spacing:0;font-size:12.5px;color:var(--bone);font-family:var(--sans)}
.edin{background:var(--ink);border:1px solid var(--line-strong);border-radius:9px;color:var(--bone);font-size:13px;padding:8px 11px;resize:vertical;font-family:var(--sans)}
.edin:focus{outline:none;border-color:var(--ember)}.edin.mono{font-family:var(--mono)}
.edin[readonly]{opacity:.6}
.regpick{width:auto;padding:5px 9px;font-size:12px;font-family:var(--mono)}
.edagent{border:1px solid var(--line);border-radius:11px;padding:10px;margin-top:8px;display:flex;flex-direction:column;gap:6px;background:var(--ink-3)}
.edarow{display:flex;gap:8px;align-items:center}
.edarow .edin{flex:1}.edrole{flex:none;width:130px}
.edmv{display:flex;gap:9px;color:var(--stone);font-size:14px}
.edmv span{cursor:pointer;padding:2px 4px;border-radius:5px}.edmv span:hover{background:var(--ink-2);color:var(--bone)}
.edmv .rm:hover{color:var(--rose)}
.edfoot{display:flex;gap:8px;align-items:center;margin-top:20px;padding-top:14px;border-top:1px solid var(--line)}
.edsub{border:1px solid rgba(255,106,43,.32);border-radius:11px;margin-top:8px;background:rgba(255,106,43,.04)}
.edsub>.edarow{padding:9px 10px}
.edsubbody{padding:2px 12px 12px;border-top:1px solid rgba(255,106,43,.18)}
.edsubtoggle{cursor:pointer;color:var(--ember-soft);font-weight:800;user-select:none;white-space:nowrap;font-size:13px;font-family:var(--mono)}
.edpar{cursor:pointer;color:var(--stone);border:1px solid var(--line-strong);border-radius:6px;padding:2px 8px;font-weight:800;user-select:none;font-size:12px;font-family:var(--mono)}
.edpar.on{color:var(--phos);border-color:rgba(70,224,160,.4);background:rgba(70,224,160,.12)}
.edpar-sp{width:28px;flex:none}
.edmin{width:64px;flex:none;text-align:center}
.ednode{display:flex;flex-direction:column;gap:6px}
/* files list */
.fgrp{font-family:var(--mono);font-weight:700;font-size:12px;margin:12px 0 6px;color:var(--bone);text-transform:uppercase;letter-spacing:.06em}
.filelist{display:flex;flex-direction:column;gap:2px;max-height:52vh;overflow-y:auto}
.frow{display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:8px;border:1px solid transparent}
.frow:hover{background:var(--ink-3);border-color:var(--line)}
.frow .fmain{flex:1;display:flex;align-items:center;gap:9px;text-decoration:none;color:var(--bone);overflow:hidden}
.frow .fi{width:18px;text-align:center}
.frow .fp{font-family:var(--mono);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.frow .fsz{color:var(--stone);font-family:var(--mono);font-size:11px;white-space:nowrap}
.frow .dl{color:var(--stone);text-decoration:none;padding:2px 7px;border-radius:5px;font-size:13px}
.frow .dl:hover{color:var(--bone);background:var(--ink-3)}
.regrow{display:flex;align-items:center;gap:10px;padding:9px 6px;border-bottom:1px solid var(--line)}
.regrow .rid{font-family:var(--mono);font-weight:700;color:var(--ember-soft);font-size:13px}
.regrow .rn{color:var(--bone-dim);font-size:12px;flex:1;font-family:var(--mono)}
/* overflow menu */
.ovf{position:relative}
.ovfmenu{display:none;position:absolute;right:0;top:38px;background:var(--ink-2);border:1px solid var(--line-strong);border-radius:11px;
  padding:5px;min-width:200px;box-shadow:0 16px 42px rgba(0,0,0,.55);z-index:30}
.ovfmenu.open{display:block}
.ovfi{padding:9px 12px;border-radius:7px;cursor:pointer;font-size:13px;white-space:nowrap;color:var(--bone-dim)}
.ovfi:hover{background:var(--ink-3);color:var(--bone)}
/* new-loop button in a page */
.pagehead{display:flex;align-items:center;gap:14px;margin-bottom:6px}
.pagehead .h1{margin:0}
.pagehead .spacer{flex:1}
.pageintro{color:var(--bone-dim);font-size:13px;margin:0 0 18px;max-width:640px}
.pageintro b{color:var(--bone)}
/* THE HUB — objectives queue + issue log (SLICE-1-SPEC §4.5 / BRIEF-ADDENDUM) */
.hubcols{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start}
@media (max-width:760px){.hubcols{grid-template-columns:1fr}}
.hubcol{background:var(--ink-2);border:1px solid var(--line);border-radius:12px;padding:14px 16px 16px}
.hubcolhead{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
  color:var(--bone);margin-bottom:10px;display:flex;align-items:baseline;gap:8px}
.hubcolhead .cnt{color:var(--stone);font-weight:600;font-size:10px}
.hubadd{display:flex;gap:8px;margin-bottom:10px}
.hubin{flex:1;background:var(--ink);border:1px solid var(--line);border-radius:8px;padding:8px 10px;
  color:var(--bone);font-family:var(--sans);font-size:13px}
.hubin:focus{outline:none;border-color:var(--ember)}
.hublist{display:flex;flex-direction:column;gap:6px}
.hubrow{display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--ink);border:1px solid var(--line);border-radius:8px}
.hubrow.done .hubtext{text-decoration:line-through;color:var(--stone)}
.hubrow.fail{cursor:pointer}
.hubrow.fail:hover{border-color:var(--line-strong)}
.hubchk{background:none;border:none;color:var(--phos);font-size:14px;cursor:pointer;line-height:1;padding:2px}
.hubrow.done .hubchk{color:var(--stone)}
.hubtext{flex:1;font-size:13px;color:var(--bone);word-break:break-word}
.hubago{font-family:var(--mono);font-size:10px;color:var(--stone)}
.hubx{background:none;border:none;color:var(--stone);font-size:16px;cursor:pointer;line-height:1;padding:0 2px;opacity:.5}
.hubx:hover{opacity:1;color:var(--ember-soft)}
.hubempty{padding:16px 10px;color:var(--stone);font-size:12.5px;text-align:center;
  border:1px dashed var(--line);border-radius:8px}
.hubsub{font-family:var(--mono);font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
  color:var(--stone);margin:16px 0 8px;display:flex;align-items:baseline;gap:8px}
.hubsub .cnt{color:var(--ember-soft)}
.ichip.ok{color:var(--phos);background:rgba(70,224,160,.12);border-color:rgba(70,224,160,.30)}
.clk{cursor:pointer}
.btn.full{width:100%;justify-content:center}
/* REDESIGN-SPEC §3 (rd-issues) — the reactive INBOX stream */
.isslist{display:flex;flex-direction:column;gap:10px;margin-top:6px}
.isscard{background:var(--ink-2);border:1px solid var(--line);border-radius:12px;padding:11px 13px}
.isscard:hover{border-color:var(--line-strong)}
.isshead{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.issttl{flex:1;font-size:13.5px;color:var(--bone);font-weight:600;min-width:120px}
.ikind{display:inline-flex;font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.04em;
  text-transform:uppercase;padding:2px 8px;border-radius:var(--r-pill);color:var(--bone-dim);
  background:var(--ink);border:1px solid var(--line)}
.ikind.k-crash{color:#FF8A8A;background:rgba(255,90,90,.10);border-color:rgba(255,90,90,.30)}
.ikind.k-blocked{color:#FFC46B;background:rgba(255,150,60,.10);border-color:rgba(255,150,60,.28)}
.ikind.k-idea-overflow{color:var(--phos);background:rgba(70,224,160,.08);border-color:rgba(70,224,160,.24)}
.issmeta{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:7px}
.issacts{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:9px}
.issfileform{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0 14px;
  padding:12px;background:var(--ink-2);border:1px solid var(--line);border-radius:12px}
/* REDESIGN-SPEC §3/§6 (rd-ideahub) — the two-pane living-document workspace */
.hubtwo{display:grid;grid-template-columns:280px 1fr;gap:18px;align-items:start}
@media (max-width:820px){.hubtwo{grid-template-columns:1fr}}
.hubrail{position:sticky;top:8px}
.docindex{display:flex;flex-direction:column;gap:4px;margin-top:10px}
.docrow{display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--line);
  border-radius:8px;cursor:pointer;background:var(--ink)}
.docrow:hover{border-color:var(--line-strong)}
.docrow.on{border-color:var(--ember);background:rgba(255,106,43,.05)}
.dglyph{font-size:15px;color:var(--phos);flex:none;width:16px;text-align:center}
.dglyph.big{font-size:22px}
.dttl{flex:1;font-size:13px;color:var(--bone);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dloop{font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:.05em;color:var(--phos);
  background:rgba(70,224,160,.12);border:1px solid rgba(70,224,160,.30);border-radius:var(--r-pill);padding:1px 6px}
.dres{color:var(--phos);font-size:12px}
.hubwork{min-width:0}
.docpane{background:var(--ink-2);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.docpanehead{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.doctitle{flex:1;background:transparent;border:none;border-bottom:1px solid var(--line);
  color:var(--bone);font-family:var(--sans);font-size:17px;font-weight:700;padding:4px 2px}
.doctitle:focus{outline:none;border-bottom-color:var(--ember)}
.docbody{width:100%;min-height:280px;resize:vertical;background:var(--ink);border:1px solid var(--line);
  border-radius:10px;padding:12px 14px;color:var(--bone);font-family:var(--sans);font-size:13.5px;line-height:1.6}
.docbody:focus{outline:none;border-color:var(--ember)}
.docbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:11px}
.dtoggle{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--bone-dim);cursor:pointer}
.dsub{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
  color:var(--stone);margin:16px 0 7px}
.dchips{display:flex;gap:7px;flex-wrap:wrap}
.dhist{display:flex;flex-direction:column;gap:4px}
.dhrow{display:flex;align-items:center;gap:9px;font-size:11.5px;color:var(--bone-dim)}
.docresult{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:9px 12px;border-radius:10px;margin-bottom:11px}
.docresult.green{background:rgba(70,224,160,.10);border:1px solid rgba(70,224,160,.32)}
.docresult.nongreen{background:rgba(255,150,60,.08);border:1px solid rgba(255,150,60,.28)}
.drverdict{font-family:var(--mono);font-size:12px;font-weight:700}
.docresult.green .drverdict{color:var(--phos)}
.docresult.nongreen .drverdict{color:#FFC46B}
.drline{flex:1;font-size:12.5px;color:var(--bone-dim);min-width:120px}
/* §8 (Q2) — the objective-manager THREAD docked in the doc right pane */
.docthread{margin-top:18px;border-top:1px solid var(--line);padding-top:6px}
.thsub{display:flex;align-items:baseline;gap:8px}
.thhelp,.thhint{font-size:10.5px;text-transform:none;letter-spacing:0;font-weight:400}
.thbyline{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:2px 0 10px}
.thstate{font-family:var(--mono);font-size:11px;font-weight:700;padding:1px 8px;border-radius:var(--r-pill);border:1px solid var(--line)}
.thstate.st-unatt{color:var(--stone)}
.thstate.st-off{color:#FFC46B;border-color:rgba(255,150,60,.32)}
.thstate.st-await{color:var(--ember-soft);border-color:rgba(255,106,43,.3)}
.thstate.st-ready{color:var(--phos);border-color:rgba(70,224,160,.32)}
.thon{font-size:12px;color:var(--bone-dim)}
.thsel{background:var(--ink);border:1px solid var(--line);color:var(--bone);border-radius:8px;padding:3px 8px;font-family:var(--mono);font-size:11.5px;max-width:220px}
.thmsgs{display:flex;flex-direction:column;gap:8px;max-height:340px;overflow-y:auto;padding:2px}
.thempty{font-size:12.5px;color:var(--stone);padding:10px 4px}
.thmsg{border:1px solid var(--line);border-radius:10px;padding:8px 11px;font-size:13px;line-height:1.5}
.thmsg.you{background:var(--ink);align-self:flex-end;max-width:86%}
.thmsg.agent{background:linear-gradient(180deg,var(--ink-2),var(--ink));border-color:var(--line-strong);max-width:92%}
.thmsg.agent.failed{border-color:rgba(255,150,60,.35)}
.thwho{display:block;font-family:var(--mono);font-size:9.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--stone);margin-bottom:3px}
.thmsg.agent .thwho{color:var(--ember-soft)}
.thtext{color:var(--bone);white-space:pre-wrap}
.thundis{margin-left:7px;font-family:var(--mono);font-size:10px;color:#FFC46B}
.thpending{color:var(--ember-soft);font-size:12.5px}
.thdots i{display:inline-block;width:4px;height:4px;margin:0 1px;border-radius:50%;background:var(--ember-soft);opacity:.4;animation:thpulse 1.2s infinite}
.thdots i:nth-child(2){animation-delay:.2s}.thdots i:nth-child(3){animation-delay:.4s}
@keyframes thpulse{0%,60%,100%{opacity:.3}30%{opacity:1}}
.thacts{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.thact{font-family:var(--mono);font-size:10.5px;padding:1px 8px;border-radius:var(--r-pill);background:rgba(70,224,160,.10);border:1px solid rgba(70,224,160,.28);color:var(--phos)}
.thcompose{margin-top:10px}
.thinput{width:100%;resize:vertical;min-height:44px;background:var(--ink);border:1px solid var(--line);border-radius:10px;padding:9px 11px;color:var(--bone);font-family:var(--sans);font-size:13px;line-height:1.5}
.thinput:focus{outline:none;border-color:var(--ember)}
.thinput:disabled{opacity:.55}
.thbar{display:flex;align-items:center;gap:10px;margin-top:7px;flex-wrap:wrap}
.thbar .thhint{flex:1;min-width:140px}
/* §rd-sessions — capability-honest switch verbs on a session card */
.sessverbs{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px;padding-top:9px;border-top:1px solid var(--line)}
.sessfollow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px;font-size:12px}
.sessfollow .sk{color:var(--stone);text-transform:uppercase;font-size:10px;letter-spacing:.04em}
.sesssend{margin-top:9px}
.sinput{width:100%;resize:vertical;background:var(--ink);border:1px solid var(--line);border-radius:9px;padding:8px 10px;color:var(--bone);font-family:var(--sans);font-size:12.5px;line-height:1.5}
.sinput:focus{outline:none;border-color:var(--ember)}
.sesssendbar{display:flex;align-items:center;gap:9px;margin-top:6px;flex-wrap:wrap}
.boundloop.more{border-style:dashed;color:var(--ember-soft)}
/* saved-templates shelf (in the Loops list) */
.savedshelf{margin:0 0 12px;padding:10px 4px 6px;background:rgba(255,106,43,0.04);border:1px solid rgba(255,106,43,0.18);border-radius:12px}
.savedshelf .kick{display:block;padding:0 8px 6px;color:var(--ember)}
.savedrow{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px}
.savedrow:hover{background:rgba(255,106,43,0.06)}
.savedrow .rid{font-family:var(--mono);font-weight:700;color:var(--ember-soft);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:none;max-width:140px}
.savedrow .rn{color:var(--bone-dim);font-size:11.5px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono)}
.savedrow .btn{padding:4px 10px;font-size:11px}
/* Empty-state helper for lists that share the same brand tone */
.empty .kick{display:block;margin-bottom:8px}
.empty .lede{color:var(--bone-dim);font-size:13px;line-height:1.55;max-width:280px;margin:0 auto 8px}
/* Reduced-motion respect: the pulse dot, slide-over modal, hover lifts, and
   sortable-arrow flips all disappear when the OS asks for calm. Everything
   still functions — the state changes just land instantly. */
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:.001ms !important;animation-iteration-count:1 !important;
    transition-duration:.001ms !important;scroll-behavior:auto !important}
  .dot::after{animation:none;opacity:0}
  .modal{animation:none}
  .ovl{animation:none}
  .newrun:hover,.btn:hover,.rowcard:hover,.lcard:hover{transform:none}
}
/* ── M1 (QoL R6): responsive / mobile ───────────────────────────────────────
   The desktop shell is untouched above the 760px breakpoint. Below it the
   fixed 236px sidebar becomes an off-canvas drawer reached by a hamburger in
   the topbar (with a dimming scrim), the loops list+detail two-pane stacks
   vertically, and wide content (tables/logs/code) scrolls INSIDE its own box
   so the page itself never scrolls sideways. The toggle + scrim are
   display:none until the query switches them on — zero change on desktop. */
.navtoggle{display:none;flex:none;width:40px;height:40px;padding:0;border-radius:10px;
  border:1px solid var(--line-strong);background:var(--ink-2);color:var(--bone);
  cursor:pointer;align-items:center;justify-content:center;font-size:18px;line-height:1}
.navtoggle:hover{border-color:var(--bone-dim)}
.navtoggle:focus-visible{outline:2px solid var(--ember);outline-offset:2px}
.navscrim{display:none;position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.55);
  opacity:0;transition:opacity .2s ease}
@media (max-width:760px){
  .app{position:relative}
  .sidebar{position:fixed;top:0;left:0;bottom:0;z-index:70;width:80vw;max-width:296px;
    transform:translateX(-100%);transition:transform .22s cubic-bezier(.2,.7,.3,1);
    box-shadow:0 0 46px -8px rgba(0,0,0,.8);will-change:transform}
  .app.nav-open .sidebar{transform:none}
  .navscrim{display:block;visibility:hidden}
  .app.nav-open .navscrim{visibility:visible;opacity:1}
  .navtoggle{display:inline-flex}
  .topbar{padding:0 12px;gap:10px;height:50px}
  .crumb{min-width:0;overflow:hidden;font-size:12.5px}
  .crumb .cur{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:52vw}
  .tick{display:none}                 /* free the narrow topbar; non-critical ticker */
  .two-pane{display:block;height:auto;min-height:100%}
  .two-pane .list{border-right:0;border-bottom:1px solid var(--line);max-height:44vh;padding:12px}
  .two-pane .detail{padding:16px 14px}
  .page{padding:16px 14px}
  .h1{font-size:19px}
  .navitem{padding:11px 12px}         /* ≥40px-tall tap targets */
  .refreshbtn,.helpbtn{height:38px;width:38px}
  /* wide content scrolls inside its own container — never the page */
  .view{overflow-x:hidden}
  .view table{display:block;max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
  .view pre{max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
}
/* ── TOOLS: web Terminal + +Loop creator (Phase A) ──────────────────────────
   Ported from the standalone SPA (static/index.html) into the workspace shell.
   Adapted from that page's light palette onto the Loopyard dark tokens; the
   xterm surfaces keep their own near-black background either way. xterm.js is
   vendored same-origin (no CDN). */
.toolwrap{display:flex;flex-direction:column;height:100%;min-height:0;padding:18px 22px}
.termbar{display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap}
.termstat{display:inline-flex;align-items:center;gap:7px;font-weight:600;font-size:12.5px}
.termstat .d{width:9px;height:9px;border-radius:50%;background:var(--stone);transition:background .15s}
.termstat.on .d{background:var(--phos)}
.termstat.off .d{background:var(--ember-deep)}
.termstat.wait .d{background:var(--ember-soft)}
.termhint{color:var(--stone);font-size:12px;margin-left:auto}
#termwrap{flex:1;min-height:0;border:1px solid var(--line);border-radius:var(--r);
          background:#0b0f0d;padding:8px 10px;overflow:hidden}
#term{width:100%;height:100%}
/* +loop split view: config editor (left) + seeded creator terminal (right) */
.nlsplit{display:grid;grid-template-columns:1fr 1fr;gap:14px;flex:1;min-height:0}
.nlpane{display:flex;flex-direction:column;min-height:0;border:1px solid var(--line);
        border-radius:var(--r);background:var(--ink-2);overflow:hidden}
.nlhd{display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.nlhd .t{font-weight:700;font-size:13px}
.nlhd select{padding:6px 9px;border:1px solid var(--line-strong);border-radius:var(--r-sm);font:inherit;font-size:12px;
             background:var(--ink-3);color:var(--bone);cursor:pointer}
#nlEditor{flex:1;min-height:0;resize:none;border:0;padding:12px 14px;background:#0b0f0d;color:#e6ede9;
          font-family:var(--mono);font-size:12.5px;line-height:1.5}
#nlEditor:focus{outline:none}
.nlmsg{padding:8px 12px;font-size:12px;border-top:1px solid var(--line);white-space:pre-wrap;max-height:150px;overflow:auto}
.nlmsg.ok{color:#0b1f14;background:var(--phos)}
.nlmsg.bad{color:#fff;background:var(--rose)}
.nlmsg.info{color:var(--stone)}
.nlbar{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--line);flex-wrap:wrap}
.btn.sm{padding:6px 12px;font-size:12px}
/* I2b — describe-your-goal → suggested (editable, rule-checked) team shape */
.nlsuggest{padding:11px 12px;border-bottom:1px solid var(--line);background:var(--ink-3)}
.nlsughd{display:block;font-size:11px;font-weight:700;letter-spacing:.02em;color:var(--bone-dim);margin-bottom:7px}
.nlsugrow{display:flex;gap:8px;flex-wrap:wrap}
.nlsugrow input{flex:1;min-width:180px;padding:7px 10px;border:1px solid var(--line-strong);
  border-radius:var(--r-sm);font:inherit;font-size:12.5px;background:var(--ink-2);color:var(--bone)}
.nlsugrow input:focus{outline:none;border-color:var(--ember)}
.nlsugout{margin-top:9px;font-size:12px}
.nlsugout .sugtop{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.nlsugroles{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:5px}
.nlsugroles li{display:flex;gap:8px;align-items:baseline;line-height:1.4}
.nlsugroles .rid{font-family:var(--mono);font-size:11.5px;color:var(--ember-soft);min-width:74px;font-weight:700}
.nlsugroles .rwhy{color:var(--stone);font-size:11.5px}
.sugok{color:#0b1f14;background:var(--phos);border-radius:999px;padding:1px 9px;font-size:11px;font-weight:700}
.sugwarn{color:#fff;background:var(--rose);border-radius:999px;padding:1px 9px;font-size:11px;font-weight:700}
.sugnote{color:var(--stone);font-size:11px;margin-top:7px;font-style:italic}
#nltermwrap{flex:1;min-height:0;background:#0b0f0d;padding:8px 10px;overflow:hidden}
#nlterm{width:100%;height:100%}
/* ── §5.5 rd-create — the calm, goal-first COMPOSER (one creator, two doors, zero
   JSON on the default path). Replaces the JSON-editor + shell split as the front
   door; the config editor + briefing terminal now live behind "Advanced". ───── */
.composer{max-width:720px;margin:0 auto;padding:26px 6px 40px;overflow-y:auto;min-height:0;flex:1}
.cmphd{margin-bottom:18px}
.cmpq{font-size:26px;font-weight:800;letter-spacing:-.01em;margin:0 0 6px;color:var(--bone)}
.cmpsub{color:var(--stone);font-size:13.5px;line-height:1.5}
/* Door B honest-shell note — quiet, factual; says what awaits the Creator backend. */
.cmpnote{display:flex;gap:9px;align-items:flex-start;margin-top:12px;padding:9px 12px;
  border:1px dashed var(--line-strong);border-radius:10px;background:var(--ink-2);
  color:var(--stone);font-size:12px;line-height:1.5}
.cmpnote .ck{flex:none;font-family:var(--mono);font-size:10px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--bone-dim);padding-top:1px}
.cmpnote b{color:var(--bone);font-weight:600}
/* REDESIGN-SPEC §5.5 — the native briefing chat (Door B + per-loop Brief/Debrief). */
.briefsub{color:var(--stone);font-size:12px;line-height:1.5;margin:0 0 12px}
.briefbody{display:flex;flex-direction:column;gap:12px}
.bchat{display:flex;flex-direction:column;gap:8px;max-height:230px;overflow:auto}
.bmsg{display:flex;gap:8px;align-items:baseline;line-height:1.45;font-size:12.5px}
.bmsg .bwho{flex:none;font-family:var(--mono);font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;
  padding:2px 6px;border-radius:6px;min-width:52px;text-align:center}
.bmsg.creator .bwho{background:rgba(255,106,43,.14);color:var(--ember-soft)}
.bmsg.you .bwho{background:var(--ink-2);color:var(--stone)}
.bmsg .btxt{color:var(--bone)}
.bmsg.creator .btxt{color:var(--bone-dim)}
.bprev{border:1px solid var(--line-strong);border-radius:10px;background:var(--ink-2);padding:9px 12px}
.bprevhd{font-size:11px;color:var(--stone);margin-bottom:6px}
.bprevhd b{color:var(--ember-soft)}
.bprevroles{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:5px}
.bprevroles li{display:flex;gap:8px;align-items:baseline;line-height:1.4}
.bprevroles .rid{font-family:var(--mono);font-size:11.5px;color:var(--ember-soft);min-width:74px;font-weight:700}
.bprevroles .rwhy{color:var(--stone);font-size:11.5px}
.bform{display:flex;flex-direction:column;gap:9px}
.bq{display:flex;flex-direction:column;gap:4px}
.bq .bql{font-size:12.5px;color:var(--bone)}
.bq .bqi{padding:8px 10px;border:1px solid var(--line-strong);border-radius:8px;background:var(--ink-1);
  color:var(--bone);font:inherit;font-size:13px}
.bq .bqi:focus{outline:none;border-color:var(--ember-soft)}
.bacts{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:2px}
.briefmsg{margin-top:10px;font-size:12px;min-height:1em}
.briefmsg.info{color:var(--stone)}
.briefmsg.ok{color:var(--ember-soft)}
.briefmsg.bad{color:#ff6b6b}
.cmpchips{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.cmpchip{display:inline-flex;align-items:center;gap:7px;padding:6px 11px;border:1px solid var(--line-strong);
  border-radius:999px;background:var(--ink-2);font-size:12px;color:var(--bone)}
.cmpchip .ck{color:var(--stone);font-size:11px;font-family:var(--mono);letter-spacing:.04em;text-transform:uppercase}
.cmpchip select{border:0;background:transparent;color:var(--bone);font:inherit;font-size:12px;cursor:pointer;max-width:220px}
.cmpchip select:focus{outline:none}
.cmptarget{cursor:pointer;border-style:dashed;color:var(--ember-soft);font-weight:700}
.cmptarget:hover{background:var(--ember-soft);color:#0b1f14}
.cmptargets{display:flex;flex-direction:column;gap:6px;margin-bottom:12px}
.cmptargets:empty{display:none}
.cmptargtag{display:flex;align-items:center;gap:9px;padding:8px 11px;border:1px solid var(--line);
  border-left:3px solid var(--ember);border-radius:var(--r-sm);background:var(--ink-2);font-size:12.5px}
.cmptargtag .tk{font-family:var(--mono);font-size:10.5px;color:var(--ember-soft);text-transform:uppercase;letter-spacing:.05em}
.cmptargtag .tt{flex:1;color:var(--bone)}
.cmptargtag .tx{cursor:pointer;color:var(--stone);border:0;background:none;font-size:16px;line-height:1}
.cmptargtag .tx:hover{color:var(--rose)}
.cmpgoal{width:100%;min-height:112px;resize:vertical;padding:14px 16px;border:1px solid var(--line-strong);
  border-radius:var(--r);background:var(--ink-2);color:var(--bone);font:inherit;font-size:15px;line-height:1.55}
.cmpgoal:focus{outline:none;border-color:var(--ember)}
.cmpactions{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-top:12px}
.btn.lg{padding:11px 20px;font-size:14px;font-weight:700}
.cmphint{margin-left:auto;color:var(--stone);font-size:11px}
.cmpadv{margin-top:22px;border-top:1px solid var(--line);padding-top:14px}
.cmpadv summary{cursor:pointer;color:var(--stone);font-size:12px;font-weight:700;letter-spacing:.02em;list-style:none}
.cmpadv summary::-webkit-details-marker{display:none}
.cmpadv summary::before{content:'▸ ';color:var(--ember-soft)}
.cmpadv[open] summary::before{content:'▾ '}
.nladvbody{display:flex;flex-direction:column;gap:0;margin-top:12px;border:1px solid var(--line);
  border-radius:var(--r);background:var(--ink-2);overflow:hidden}
.nladvbody #nlEditor{min-height:220px}
.nltermhd{display:flex;align-items:center;gap:10px;padding:9px 12px;border-top:1px solid var(--line);
  border-bottom:1px solid var(--line);flex-wrap:wrap;background:var(--ink-3)}
.nltermhd .t{font-weight:700;font-size:12.5px}
.nltermhd select{padding:6px 9px;border:1px solid var(--line-strong);border-radius:var(--r-sm);font:inherit;
  font-size:12px;background:var(--ink-3);color:var(--bone);cursor:pointer}
.nladvbody #nltermwrap{min-height:280px}
@media (max-width:760px){
  .toolwrap{padding:14px}
  .nlsplit{display:block;overflow-y:auto}      /* +loop: side-by-side → stacked */
  .nlpane{min-height:320px;margin-bottom:14px}
  .nlpane:last-child{margin-bottom:0}
  .composer{padding:16px 4px 32px}
  .cmpq{font-size:22px}
}
</style></head><body>
<div class="app">
  <!-- M1: dimming backdrop for the mobile sidebar drawer (tap to close). -->
  <div class="navscrim" onclick="closeNav()" aria-hidden="true"></div>
  <nav class="sidebar" id="sidebar">
    <div class="brand" onclick="navGo('loops')" role="link" tabindex="0"
      onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();navGo('loops')}"
      title="Loopyard — back to Loops" aria-label="Loopyard home (Loops)">
      <svg class="glyph" viewBox="0 0 64 64" aria-hidden="true">
        <rect x="12" y="19" width="40" height="26" rx="13" fill="none" stroke="#FF6A2B" stroke-width="5" stroke-linecap="round"/>
        <circle cx="49" cy="32" r="5" fill="#46E0A0"/>
        <line x1="18" y1="55" x2="46" y2="55" stroke="#FF6A2B" stroke-width="3.5" stroke-linecap="round" opacity="0.42"/>
      </svg>
      <span class="wm"><b>loop</b><em>yard</em></span>
    </div>
    <!-- Project switcher — the primary global filter (REDESIGN-SPEC §2). Picking a
         project rigidly re-scopes the Loops list + nav counts; "All projects" is the
         explicit fleet meta-view. Rendered by renderProjectSwitcher(). -->
    <div class="projswitch" id="projSwitch"></div>
    <!-- One create front door: ＋ Loop (REDESIGN-SPEC §2 — the legacy standalone-run launcher is retired). -->
    <button class="newrun" onclick="navGo('newloop')" title="Stand up a new team — the one create front door"
      aria-label="Create a new loop">＋ Loop</button>
    <div class="nav" id="nav"></div>
    <div class="navsec" id="favsec" style="display:none">★ Favorites</div>
    <div class="favshelf" id="favshelf"></div>
    <div class="navfoot">
      <label class="toggle"><input type="checkbox" id="showArch" onchange="setShowArch(this.checked)"> show archived</label>
      <div class="motto" title="Your agents. Your compute. Your keys. Our loops.">
        own <b>the loop.</b>
        <span class="oneliner"><em>Your agents. Your compute. Your keys.</em> Our loops.</span>
      </div>
      <a class="readcode" href="https://github.com/tim-po/loopyard" target="_blank" rel="noopener"
         title="Loopyard is an open harness — read the source and self-host it."><span class="rc-lead">Runs in your shell.</span> <span class="rc-link">Read the code</span> · <span class="rc-link">Self-host the harness</span> <span class="rc-arw">→</span></a>
    </div>
  </nav>
  <div class="main">
    <header class="topbar">
      <button class="navtoggle" onclick="toggleNav()" aria-label="Open navigation menu"
        aria-expanded="false" aria-controls="sidebar">☰</button>
      <span class="crumb" id="crumb"></span>
      <span class="spacer"></span>
      <!-- REDESIGN-SPEC §2 — the Fleet pill: compute felt ambiently in the top bar
           of every screen. Honest ◉ Fleet ●●○ N · M reachable; click for a popover.
           An origin running a loop that drops turns a dot amber (rd-origins). -->
      <button class="fleetpill" id="fleetPill" onclick="toggleFleetPop(event)"
        title="Your compute fleet — click for details" aria-label="Fleet status" aria-haspopup="true" aria-expanded="false">
        <span class="fpico" aria-hidden="true">◉</span><span class="fpdots" id="fleetDots" aria-hidden="true"></span><span class="fptxt" id="fleetTxt">Fleet</span></button>
      <div class="fleetpop hidden" id="fleetPop" role="dialog" aria-label="Fleet detail"></div>
      <span class="tick" id="tick" role="status" aria-live="polite">Reaching your box…</span>
      <button class="refreshbtn" onclick="refreshAll()" title="Refresh everything now" aria-label="Refresh data now">↻</button>
      <button class="helpbtn" onclick="showShortcutsHelp()" title="Keyboard shortcuts (press ?)" aria-label="Show keyboard shortcuts" style="margin-left:4px">?</button>
    </header>
    <div class="viewport">
      <div class="view hidden" id="v-overview"></div>
      <div class="view" id="v-loops"></div>
      <div class="view hidden" id="v-agents"></div>
      <div class="view hidden" id="v-sessions"></div>
      <div class="view hidden" id="v-origins"></div>
      <div class="view hidden" id="v-projects"></div>
      <div class="view hidden" id="v-hub"></div>
      <div class="view hidden" id="v-issues"></div>
      <!-- TOOLS (Phase A): static markup — render() only toggles .hidden here;
           the PTYs are driven by navGo/popstate/boot, NEVER by render(). -->
      <div class="view hidden" id="v-terminal">
        <div class="toolwrap">
          <!-- Web Terminal: a live shell on THIS box (local origin) over the
               gate-authed PTY-over-WebSocket. Auth is the dash_sess cookie the
               browser sends on the WS handshake — no token in the URL. -->
          <div class="termbar">
            <span class="termstat off" id="termStat"><span class="d"></span><span id="termStatTxt">Disconnected</span></span>
            <button class="btn ghost sm" id="termReconnect" style="display:none">Reconnect</button>
            <span class="termhint">Local origin · a shell on this box · leaving this page ends the session</span>
          </div>
          <div id="termwrap"><div id="term"></div></div>
        </div>
      </div>
      <div class="view hidden" id="v-newloop">
        <!-- §5.5 rd-create — one calm, goal-first COMPOSER, two doors:
             Door A = attach a target (＋ Target: hub docs + issues) → born pointed;
             Door B = describe the goal → Preview/Start team. A project chip sets an
             EXPLICIT projectId at create (fixes attribution). The JSON config +
             briefing terminal live behind "Advanced" — never the front door. -->
        <div class="composer">
          <div class="cmphd">
            <h1 class="cmpq">What do you want done?</h1>
            <div class="cmpsub">Describe a goal, or point the loop at a target. A team assembles — edit it, then start.
              Everything is a loop, from one agent up.</div>
          </div>
          <div class="cmpchips">
            <span class="cmpchip" title="The project this loop targets — set explicitly here so attribution is honest, never guessed from a git remote">
              <span class="ck">project</span>
              <select id="nlProject" aria-label="Project this loop targets"></select>
            </span>
            <span class="cmpchip" title="The origin (your compute) this loop will run on">
              <span class="ck">runs on</span>
              <select id="nlOrigin" aria-label="Origin"><option value="local">local · this box</option></select>
            </span>
            <button class="cmpchip cmptarget" id="nlAddTarget" title="Point this loop at a hub doc or an issue (Door A)">&#65291; Target</button>
          </div>
          <div id="nlTargets" class="cmptargets" aria-live="polite"></div>
          <textarea id="nlGoal" class="cmpgoal" spellcheck="false" aria-label="What do you want done?"
                    placeholder="e.g. Build and ship the checkout API, add tests, fix the bugs"
                    onkeydown="if((event.metaKey||event.ctrlKey)&&event.key==='Enter'){event.preventDefault();nlStart(true);}"></textarea>
          <div class="cmpactions">
            <button class="btn primary lg" id="nlStart" title="Assemble the team and start the loop">&#9654; Start loop</button>
            <button class="btn ghost" id="nlBrief" title="Door B — a live briefing chat: a creator asks a question or two while your team preview builds">&#9998; Brief the team</button>
            <button class="btn ghost" id="nlSuggest" title="Preview the editable team this goal would assemble">Preview team</button>
            <button class="btn ghost" id="nlAskOne" title="A loop of one — a single agent that decides when it's done">Ask one agent</button>
            <span class="cmphint">⌘/Ctrl + Enter to start</span>
          </div>
          <div class="nlmsg info" id="nlMsg">Describe a goal or attach a target, then Start.</div>
          <div id="nlSuggestOut" class="nlsugout hidden" aria-live="polite"></div>
          <!-- Door B — LIVE (REDESIGN-SPEC §5.5). The native in-browser briefing chat is
               wired to dev-1's daemon-free Creator (loop_creator_brief): "Brief the team"
               opens a real conversation — a creator asks a question or two while your team
               preview builds = the config — and starts it through the same validate→save→
               start seam. "Preview team" stays as the one-shot derive; Advanced keeps the
               JSON + PTY for power users. No shell up front, and no faked chat. -->
          <div class="cmpnote" id="nlBriefNote">
            <span class="ck">briefing</span>
            <span><b>Brief the team</b> opens a live briefing chat — a creator asks a clarifying
            question or two while an editable team preview builds, then you start it. Prefer one
            shot? <b>Preview team</b> derives the team immediately; <b>Advanced</b> keeps the raw
            config + briefing terminal for power users.</span>
          </div>
          <!-- Advanced — the config JSON + the briefing terminal, for power users /
               self-hosters. NOT the default path (no JSON, no shell up front). -->
          <details class="cmpadv" id="nlAdvanced" ontoggle="nlAdvancedToggle(this)">
            <summary>Advanced — view config &amp; briefing terminal</summary>
            <div class="nladvbody">
              <div class="nlhd">
                <span class="t">Loop config (JSON)</span>
                <span style="margin-left:auto"></span>
                <button class="btn sm ghost" id="nlApply">&#9664; Apply from terminal</button>
                <button class="btn sm ghost" id="nlValidate">Validate</button>
                <button class="btn sm" id="nlSave" disabled>Save</button>
                <button class="btn sm primary" id="nlSaveRun" disabled title="Save this team and start it running now">&#9654; Save &amp; run</button>
              </div>
              <textarea id="nlEditor" spellcheck="false" aria-label="Loop config editor"
                        placeholder="The composer fills this in for you. You can also converse with the briefing terminal below — when it emits a ```json config, click &ldquo;&#9664; Apply from terminal&rdquo;, then Validate + Save. Or paste / hand-edit config JSON here."></textarea>
              <div class="nltermhd">
                <span class="t">Briefing terminal</span>
                <span class="termstat off" id="nlTermStat"><span class="d"></span><span id="nlTermStatTxt">Disconnected</span></span>
                <span style="margin-left:auto"></span>
                <label class="termhint" style="margin:0">Runtime</label>
                <select id="nlRuntime" aria-label="Creator runtime"><option value="claude">claude</option><option value="codex">codex</option><option value="cursor">cursor</option></select>
                <button class="btn sm ghost" id="nlTermReconnect" style="display:none">Reconnect</button>
              </div>
              <div id="nltermwrap"><div id="nlterm"></div></div>
            </div>
          </details>
        </div>
      </div>
    </div>
  </div>
</div>
<div id="modalRoot"></div>
<script>
// ── shared state ──────────────────────────────────────────────────────────────
let view='loops';                      // loops | agents | origins | projects | issues | hub (landing = Loops; /runs is gone)
let sel=null, selHost=null, detail=null;
let loops=[], hosts=['local'], origins=[], projects=[];
let issues=[];                          // §3 (rd-issues) — the project-scoped reactive INBOX stream (new model)
let issueSel=null, issueDetail=null;    // crash drill-in: selected id + fetched forensic record
let issueViewAll=false;                 // Issues filter: open-work vs the whole stream (incl. resolved/dismissed)
let issueFileOpen=false;                // the ＋ File-an-issue inline form toggle
let hubDocs=[];                         // §6 (rd-ideahub) — the two-pane Hub RAIL: index of living docs (new model)
let hubDocSel=null, hubDocDetail=null;  // Hub editor: selected doc id + its full record (body/result/history)
let hubDocDirty=false;                  // unsaved-edits guard in the doc editor
let hubThread=null;                     // §8 (Q2) — the objective-manager thread docked to the open doc
let hubThreadAttaching=false;           // the attach/detach control is mid-flight
let projectsGathered=null;             // {created, loops_scanned} from the last auto-gather on Projects-page load
let regAgents=[], regSavedAgents=[], regSavedLoops=[];
let sessionsRoster=[], sessionsLive=0, sessionsLoaded=false;  // §7 (rd-sessions) — the living-collaborators roster read-model
let sessFollowOpen={};                  // §rd-sessions "Follow" — per-session read-only activity expanded
let sessSendOpen={};                    // §rd-sessions "Send" — per-session inline compose box open
let fleetSummary=null;                  // §2 (rd-origins) — the Fleet pill data (N·M reachable + drop signal)
let originPickerOpts=[];                // §7 (rd-origins) — the ONE shared health-aware picker source
// REDESIGN-SPEC §2 — the project switcher is the primary GLOBAL filter. `null`
// means the "All projects" fleet meta-view; a project id rigidly scopes the Loops
// list + nav counts. Persisted so the last-picked project survives a reload.
let activeProject=(function(){try{return localStorage.getItem('lyActiveProject')||null}catch(e){return null}})();
// REDESIGN-SPEC §2/§3 — the honest "Unattributed" scope: loops the backend
// (schema.resolve_project) could bind to no confident project. Its sentinel id can
// never collide with a real project id (those start with an alphanumeric, per the
// backend _NAME_RE). Picking it scopes the app to the unattributed bucket.
const UNATTR='__unattributed__';
// A loop/issue matches the active scope: "" (null) = all projects, UNATTR = no
// project binding, else its `project`/`product` binding equals the active id.
function inScope(d){return !activeProject
  ?true:(activeProject===UNATTR?!(d&&(d.project||d.product)):(d&&(d.project||d.product))===activeProject);}
// REDESIGN-SPEC §2 — the project filter re-scopes the WHOLE app, not just Loops
// (uxui: Hub/Origins stayed global). A loop-failure/issue record carries no project
// of its own, so map it through its loop to the confident project the loops feed
// already resolved (schema.resolve_project) — the honest loop→project link
// (§rd-issues). Falls back to the record's own project/product when present.
function loopProjectOf(name){const l=(loops||[]).find(x=>x&&x.name===name);return l?(l.project||l.product||null):null;}
function issueInScope(x){if(!activeProject)return true;
  const pid=(x&&(x.project||x.product))||loopProjectOf(x&&x.loop);
  return activeProject===UNATTR?!pid:pid===activeProject;}
// §6 (rd-ideahub) — a Hub doc carries its OWN project (§2 scoping), unlike an
// issue which maps through its loop. UNATTR = a doc with no project (the honest
// cross-project bucket); "" = all projects.
function docInScope(d){if(!activeProject)return true;const pid=d&&d.project;
  return activeProject===UNATTR?!pid:pid===activeProject;}
// The scopes the switcher offers, sourced from what loops are ACTUALLY bound to
// (schema.resolve_project on the backend, §3), unioned with the projects catalog,
// plus an honest "Unattributed" scope when any loop carries no confident project.
// Each carries its live loop count so the switcher discriminates REAL projects with
// non-zero counts — not a dead catalog of buckets nothing points at.
function projectScopes(){
  // Mirror visibleLoops()'s archived rule so the switcher's counts equal what the
  // Loops list actually shows (uxui: BotSwarm read 9 in the switcher vs 8 in the
  // list — the switcher was counting an archived loop the list hides). Honour the
  // same "show archived" toggle so the two never disagree again.
  const sa=(document.getElementById('showArch')||{}).checked;
  const map=new Map();                                   // id -> {id,name,count}
  (projects||[]).forEach(p=>{if(p&&p.id)map.set(p.id,{id:p.id,name:p.name||p.id,count:0});});
  let unattr=0;
  (loops||[]).forEach(d=>{
    if(!d)return;
    if(!(sa||!d.archived))return;                        // archived → excluded from switcher counts
    const pid=d.project||d.product;
    if(!pid){unattr++;return;}
    const cur=map.get(pid)||{id:pid,name:d.productName||pid,count:0};
    cur.count++;map.set(pid,cur);
  });
  const list=[...map.values()].sort((a,b)=>(b.count-a.count)||String(a.name).localeCompare(String(b.name)));
  if(unattr)list.push({id:UNATTR,name:'Unattributed',count:unattr});
  return list;
}
// REDESIGN-SPEC §Q1 — the loop-of-1 is the smallest unit; this chip filters the
// Loops list to single-agent loops (keyed off dev-1's `single_agent` field).
let loopSingleFilter=false;
// D4 — empty states never lie: per-collection load error. A blank list is only
// honestly "empty" when its loader succeeded; if the box/daemon couldn't be
// reached the empty state must name THAT instead of implying no data exists.
let loadErr={loops:'',agents:'',origins:'',projects:'',issues:'',hub:'',fleet:'',sessions:''};
let curAgent=null, agentDetail=null, agentRec=null, agentVers=null;
let collapsedSubs=new Set();
let busy=false;
let expandedAgents=new Set(), expandedVers=new Set();
let agentFilter='';                    // case-insensitive substring filter for the Agents table (persists across re-renders)
let loopFilter='';                     // same, but for the Loops sidebar list
let agentSort={col:'turns',dir:-1};    // click a column header on the Agents table to sort; dir=1 asc, -1 desc
const RUNNING=new Set(['running','stopping','waiting_owner','needs_owner']);
const FINISHED=new Set(['finished','complete','stopped','error','needs_owner']);
const KNOWN_MODELS=['claude-opus-4-8','claude-sonnet-5','claude-haiku-4-5-20251001','claude-fable-5'];
const ICONS={overview:'⌂',loops:'⟲',hub:'◈',agents:'★',roles:'★',sessions:'✦',origins:'◉',projects:'▦',issues:'⚠',terminal:'❯',newloop:'＋'};
// SLICE-1B §3.2 / design principle 5 — HONEST terminal status. The engine's raw
// `state` marks any run that ENDED as 'finished'/'complete' (the loop stopped),
// while the run's `ended==='error'` (mirrored into the backend's external
// `status`) records whether it actually SUCCEEDED. A run that errored must never
// read as a green "finished ✅": surface it as the failure it was. Additive —
// every non-errored state passes straight through unchanged, so live / saved /
// genuinely-completed loops are untouched (no loops-screen regression).
const runErrored=d=>!!d&&(d.ended==='error'||d.status==='error');
const honestState=d=>runErrored(d)?'error':((d&&d.state)||'saved');
const firstLine=s=>{if(!s)return'';for(const ln of(''+s).split('\n')){const t=ln.trim().replace(/^#+\s*/,'');if(t)return t}return''};
// §3.2 — the ONE honest failure-reason path. firstLine() happily surfaces the
// stale "✅ loop finished — stopped by owner" headline that a run's summary /
// finish_report still carries; inside a failure block (or a couldn't-run toast)
// that green-check reads as a fake-success — the exact §3.2 defect. failReason()
// instead SKIPS any ✅/green-check/"finished"/"complete"/"stopped by owner"
// headline line, PREFERS the genuine `error:` detail line, and only falls back
// to a plain honest sentence (never a ✅/"finished" line) when none exists. Used
// by BOTH the errored result block and the start-failed toast so there is one
// reason path, not two. Additive: the success path never calls this.
const _greenLine=t=>/✅|✔|☑|\bfinished\b|\bcomplete(d)?\b|stopped by owner/i.test(t);
const failReason=d=>{
  // Scan BOTH the full finish_report AND the one-line summary together — NOT
  // `summary||finish_report`. `_loop_summary_line` prefers the finish-report's
  // FIRST head line, which for an errored run is the stale "✅ finished — stopped
  // by owner" green headline; letting that truthy one-liner shadow the full
  // finish_report hides the REAL `error:` detail that lives on a deeper line.
  // finish_report goes first so the step-2 fallback prefers its fuller text.
  const src=[(d&&d.finish_report)||'',(d&&d.summary)||''].join('\n');
  const lines=(''+src).split('\n')
    .map(l=>l.trim().replace(/^#+\s*/,'')).filter(Boolean);
  const err=lines.find(l=>/error/i.test(l)&&!_greenLine(l));   // 1) the real error detail
  if(err)return err;
  const real=lines.find(l=>!_greenLine(l));                    // 2) first non-green substantive line
  if(real)return real;
  return 'ended in an error — no result produced.';            // 3) never a ✅/"finished" line
};

// ── utils ──────────────────────────────────────────────────────────────────────
const esc=s=>(s==null?'':(''+s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const ago=t=>{if(!t)return'';const s=Math.max(0,Date.now()/1000-t);if(s<60)return Math.floor(s)+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago'};
const dur=t=>{if(!t)return'';const s=Math.max(0,Date.now()/1000-t);if(s<60)return Math.floor(s)+'s';if(s<3600)return Math.floor(s/60)+'m'+Math.floor(s%60)+'s';return Math.floor(s/3600)+'h'};
const hhmm=t=>{if(!t)return'';return new Date(t*1000).toTimeString().slice(0,8)};
const mins=v=>v==null?'—':(v>=60?(v/60).toFixed(1)+'h':(+v).toFixed(1)+'m');
const roleOf=(d,a)=>{const t=d.team||{};if(t.manager===a)return'manager';if((t.workers||[]).includes(a))return'worker';if((t.inputs||[]).includes(a))return'input_provider';if((t.subloops||[]).includes(a))return'loop';if(a==='__service')return'service';return''};
async function j(u){const r=await fetch(u,{headers:{'Accept':'application/json'}});if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}
async function post(u,body){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify(body||{})});return r.json()}
function flash(msg,isErr){const f=document.createElement('div');f.className='flash '+(isErr?'err':'ok');f.textContent=msg;document.body.appendChild(f);setTimeout(()=>f.remove(),isErr?7000:3500)}
// D4 — the honest "can't see it" empty state: names the exact reason a list is
// blank (the box didn't answer) instead of implying nothing exists.
function emptyErr(kick,err){return `<div class="emptyline"><span class="kick">${esc(kick)}</span>Couldn't reach the box — ${esc(err)}.<br>The list stays empty until this box answers again.</div>`}

// ── nav + favorites ────────────────────────────────────────────────────────────
// D8 — tooltips define the three nouns that aren't self-evident on first
// contact (Loops/Origins/Projects). Runs & Agents deliberately get none.
// SLICE-1B / BRIEF item 5 — the top nav is aligned to the four designed surfaces
// (Home · Project/hub · Loops & Results · Origins) with the building blocks as
// depth below. `issues` and `terminal` are NO LONGER top-level peers: `issues`
// folds into the hub issue log (the hub surfaces loop-terminal-failures too), and
// `terminal` demotes under Origins as a connected-session (builder-origins-run
// places it). Their entries STAY in this array — the views remain reachable as
// depth (the issue drill-in, the demoted terminal PTY) and byId()/label lookups
// keep working — they are simply not rendered as top-level items by renderNav().
const NAV=[
  {id:'overview',label:'Overview',tip:'This project at a glance — loops in flight, the Idea Hub snapshot, open issues, its origins, and the latest honest results.'},
  {id:'loops',label:'Loops',tip:'A crew of agents that plans, hands off, and reviews until the work is done.'},
  {id:'hub',label:'Idea Hub',tip:'A per-project workspace of living documents — a note thickens into a proposal and becomes an objective the moment you point a loop at it.'},
  // rd-sessions rename: the registry of role TEMPLATES is "Roles" now; "sessions"
  // (the live collaborators) owns the former "Agents" word — ending the collision.
  {id:'agents',label:'Roles',tip:'Building blocks — the registry of role templates a loop can be assembled from.'},
  {id:'sessions',label:'Sessions',tip:'The living collaborators — connected apps/terminals that create loops and steer objectives, each on a real origin.'},
  {id:'origins',label:'Origins',tip:'Boxes you own that loops actually run on.'},
  {id:'projects',label:'Projects',tip:'Repos and output roots a loop can point at.'},
  {id:'issues',label:'Issues',tip:'A project-scoped inbox of faults — filed by loops as they work and by you; drained by pointing a loop at one. A crash is just one kind.'},
  {id:'terminal',label:'Terminal',tip:'A live shell on this box — PTY over WebSocket, authed by your session cookie. Closing the page ends the session.'},
  {id:'newloop',label:'＋ Loop',tip:'Stand up a new team: converse with a live creator agent, then Validate + Save the config against the real schema.'}];
// REDESIGN-SPEC §2 — dev-1's per-project nav-count aggregate (loop_project_counts)
// for the ACTIVE scope, or null → the honest client-side derivation. Its unique win
// is the Idea Hub count: the client can't see per-project hub DOCS, only the
// fleet-wide objectives+issues rollup, so a scoped answer replaces that with THIS
// project's doc count. Origins/projects stay client-side (compute is fleet-wide by
// design, never a contradictory 0). We keep loops/issues client-side too so the
// nav count always equals its own list (visibleLoops()/issueInScope) — a scoped
// backend number that disagreed with the list it sits beside would be the exact
// §3 honesty seam we're closing.
let scopedCounts=null;
async function loadScopedCounts(){
  // "Unattributed" has no project slug the backend can key on (it fails the route
  // guard and would mean "whole fleet"), so keep the client-side counts there —
  // mirrors loadProjectDispositions()'s honest handling of the same sentinel.
  if(activeProject===UNATTR){scopedCounts=null;return;}
  const proj=activeProject||'';
  try{const r=await j('/api/loops/counts?project='+encodeURIComponent(proj));
    scopedCounts=(r&&r.ok!==false&&!r.error)?r:null;}
  catch(e){scopedCounts=null;}
}
// Nav counts are SCOPED to the active project (REDESIGN-SPEC §2): the Loops count
// reflects visibleLoops() (already project-filtered), and Issues counts only those
// carrying the active project id. Idea Hub rescopes off dev-1's scoped backend when
// it has answered; origins/agents/projects are fleet-wide compute, read honestly.
function counts(){
  const vis=visibleLoops();
  const iss=activeProject?issues.filter(x=>x&&issueInScope(x)):issues;
  // Idea Hub: prefer dev-1's project-scoped doc count when the backend answered for
  // THIS scope; else the client-side count of docs in the active scope (§2), so the
  // nav count always equals the rail the two-pane Hub actually renders.
  const hubN=activeProject?hubDocs.filter(docInScope).length:hubDocs.length;
  const hub=(scopedCounts&&scopedCounts.ideahub!=null)?scopedCounts.ideahub:hubN;
  return {loops:vis.length||'',hub:hub||'',agents:regAgents.length||'',sessions:(sessionsRoster||[]).length||'',origins:originBoxes().length||'',projects:projects.length||'',issues:iss.length||''};
}
function _navItem(n,c){
  const tipAttrs=n.tip?` title="${esc(n.tip)}" data-tip="${esc(n.tip)}"`:'';
  const aria=`${esc(n.label)}${n.tip?'. '+esc(n.tip):''}${c[n.id]?', '+c[n.id]:''}`;
  return `<a class="navitem ${view===n.id?'on':''}" onclick="navGo('${n.id}')"${tipAttrs}
      role="link" tabindex="0" aria-current="${view===n.id?'page':'false'}" aria-label="${aria}">
    <span class="nico" aria-hidden="true">${ICONS[n.id]}</span>${esc(n.label)}${c[n.id]?`<span class="ncnt">${c[n.id]}</span>`:''}</a>`;
}
// BRIEF item 5 — the top nav is the four designed surfaces (Home · Loops &
// Results · Project/Hub · Origins), building blocks as depth below. `issues`
// (folded into the hub) and `terminal` (demoted under Origins) are no longer
// rendered as top-level peers; their views stay reachable as depth. This is an
// information-architecture alignment, not a deletion — Runs/Agents/Projects all
// remain reachable so the loops screen never regresses (preservation contract).
function renderNav(){const el=document.getElementById('nav');if(!el)return;const c=counts();
  const byId=id=>NAV.find(n=>n.id===id);
  // REDESIGN-SPEC §2/§8 rail, scoped to the active project:
  //   ⌂ Overview · ⟲ Loops · ◈ Idea Hub · ⚠ Issues · ◉ Origins · ✦ Sessions
  //   — then Building blocks: ★ Roles (the former "Agents" registry) + ▦ Projects.
  el.innerHTML=
    '<div class="navsec">HOME</div>'
    +_navItem(byId('newloop'),c)
    +_navItem(byId('overview'),c)
    +'<div class="navsec">LOOPS & RESULTS</div>'
    +_navItem(byId('loops'),c)
    +'<div class="navsec">TARGETS</div>'
    +_navItem(byId('hub'),c)+_navItem(byId('issues'),c)
    +'<div class="navsec">COMPUTE</div>'
    +_navItem(byId('origins'),c)+_navItem(byId('sessions'),c)
    +'<div class="navsec">BUILDING BLOCKS</div>'
    +_navItem(byId('agents'),c)+_navItem(byId('projects'),c);
  renderFavShelf();
  renderProjectSwitcher();
}
// REDESIGN-SPEC §2 — the persistent project switcher, the app's primary global
// filter. It lists every known project plus an explicit "All projects" fleet
// meta-view (value ""). Picking one rigidly re-scopes the Loops list and the nav
// counts (see visibleLoops()/counts()). The current pick persists in localStorage
// so it survives a reload. Rendered fleet-wide even with zero projects so the
// control is always discoverable (it then reads "All projects" only).
function renderProjectSwitcher(){
  const el=document.getElementById('projSwitch');if(!el)return;
  const scopes=projectScopes();
  // If the active scope no longer exists (project deleted, or the last loop bound
  // to it went away), fall back to the fleet view honestly rather than showing a
  // stale phantom scope. Guard BEFORE building opts so the right one reads selected.
  if(activeProject&&!scopes.some(s=>s.id===activeProject))activeProject=null;
  const opts=['<option value=""'+(!activeProject?' selected':'')+'>All projects</option>']
    .concat(scopes.map(s=>`<option value="${esc(s.id)}"${activeProject===s.id?' selected':''}>${esc(s.name)}${s.count?' ('+s.count+')':''}</option>`));
  el.innerHTML=`<label class="pswrap" title="Filter the whole app to one project (REDESIGN-SPEC §2)">
    <span class="psglyph" aria-hidden="true">▦</span>
    <select id="psSelect" aria-label="Active project — scopes Loops and nav counts" onchange="setActiveProject(this.value)">${opts.join('')}</select>
  </label>`;
}
function setActiveProject(pid){
  activeProject=pid||null;
  try{if(activeProject)localStorage.setItem('lyActiveProject',activeProject);else localStorage.removeItem('lyActiveProject');}catch(e){}
  loadProjectDispositions();   // §5.4 — re-scope the per-project rollup + card verbs
  loadScopedCounts().then(()=>renderNav());   // §2 — rescope the Idea Hub nav count in lockstep
  render();
  paintTick();   // uxui#1/#2 — move the top-bar scope count in lockstep, no stale contradiction
}
// ── §5.4 disposition, fed back ────────────────────────────────────────────────
// dev-1's loop_project_dispositions aggregate for the ACTIVE project scope (or the
// whole fleet at "All projects"). `dispByLoop` maps a loop name → its CURRENT
// good/ok/bad verb so a Loops card can echo how its owner said it landed;
// `dispAgg` holds the honest rollup ("4 good / 1 bad · 80%"). goodRate is null
// until something is rated — we never paint a fake 0%.
let dispByLoop={},dispAgg=null;
async function loadProjectDispositions(){
  // the "Unattributed" scope has no project slug the aggregate can key on (and the
  // sentinel fails the route's name guard); read the whole fleet and let the card
  // verbs still show, but skip the scoped rollup line.
  const scoped=activeProject&&activeProject!==UNATTR;
  // uxui LIVE FAILURE #2 — on "All projects" the old code built
  // /api/loops/projects//dispositions (empty projectId ⇒ a double-slash Starlette
  // 404 ⇒ "is not valid JSON" toast). Request the project-less fleet route instead;
  // a real scope keeps the {project} path. Never an empty path segment.
  const url=scoped
    ? '/api/loops/projects/'+encodeURIComponent(activeProject)+'/dispositions'
    : '/api/loops/dispositions';
  try{const r=await j(url);
    if(!r||r.error||r.ok===false){dispByLoop={};dispAgg=null;return;}
    const map={};(r.loops||[]).forEach(l=>{if(l&&l.loop&&l.verb)map[l.loop]=l.verb;});
    dispByLoop=map;
    // only surface the rollup line for a real project scope (the fleet-wide number
    // is noise on the "All projects" view); the card verbs come from the map either way.
    dispAgg=(scoped&&r.rated)?r:null;
  }catch(e){dispByLoop={};dispAgg=null;}
}
// The tiny how-it-landed flag echoed on a Loops card (§5.4 "shown back on loop cards").
function dispFlagHtml(name){const v=dispByLoop[name];return v?`<span class="dispflag ${v}" title="you rated this loop ${esc(v)}">${esc(v)}</span>`:'';}
// The per-project disposition rollup line under the Loops header.
function dispRollHtml(){
  if(!dispAgg||!dispAgg.rated)return'';
  const gr=dispAgg.goodRate;const pct=gr!=null?Math.round(gr*100):null;
  return `<div class="disproll"><span>how loops landed here:</span>
    <span>${dispAgg.good} good · ${dispAgg.ok_count} ok · ${dispAgg.bad} bad</span>
    ${pct!=null?`<span class="rg${pct<50?' low':''}">${pct}% good</span>`:''}
    <span class="stone">${dispAgg.rated}/${dispAgg.total} rated</span></div>`;
}
function renderFavShelf(){
  const favs=(regSavedAgents||[]).filter(a=>a.favorite);
  const sec=document.getElementById('favsec'),shelf=document.getElementById('favshelf');
  if(!sec||!shelf)return;
  if(!favs.length&&!regSavedAgents.length){sec.style.display='none';shelf.innerHTML='';return}
  sec.style.display='';
  if(!favs.length){shelf.innerHTML='<div class="favempty">★ star an agent to pin it here</div>';return}
  shelf.innerHTML=favs.map(a=>`<div class="favrow" onclick="openAgent('${esc(a.id)}')" title="${esc(a.note||'')}">
    <span class="fico">★</span><span class="fid">${esc(a.id)}</span></div>`).join('');
}
function navGo(id){view=id;curAgent=null;if(id!=='loops')sel=null;if(id!=='issues'){issueSel=null;issueDetail=null}_writeUrl('/'+id);render();syncToolPanes()}
function crumbHtml(){
  if(view==='loops'&&sel)return `<a onclick="navGo('loops')">Loops</a><span class="sep">/</span><span class="cur">${esc(sel)}</span>`;
  if(view==='agents'&&curAgent)return `<a onclick="navGo('agents')">Roles</a><span class="sep">/</span><span class="cur">${esc(curAgent)}</span>`;
  if(view==='issues'&&issueSel)return `<a onclick="navGo('issues')">Issues</a><span class="sep">/</span><span class="cur">${esc(issueSel)}</span>`;
  const label=(NAV.find(n=>n.id===view)||{}).label||view;
  return `<span class="cur">${esc(label)}</span>`;
}
function render(){
  renderNav();
  // NB: 'terminal' and 'newloop' are in the visibility list but deliberately NOT
  // in the render() dispatch below — their PTYs are owned by navGo/popstate/boot
  // via syncToolPanes(). render() fires on every 5s/7s poll; touching the PTY
  // here would respawn it and exhaust the backend's MAX_TERMINALS.
  for(const id of ['overview','loops','hub','agents','sessions','origins','projects','issues','terminal','newloop'])
    document.getElementById('v-'+id).classList.toggle('hidden',view!==id);
  document.getElementById('crumb').innerHTML=crumbHtml();
  if(view==='overview')renderOverview();
  else if(view==='loops')renderLoops();
  else if(view==='hub')renderHub();
  else if(view==='agents')renderAgents();
  else if(view==='sessions')renderSessions();
  else if(view==='origins')renderOrigins();
  else if(view==='projects')renderProjects();
  else if(view==='issues')renderIssues();
}
// URL grammar (D1 — real paths, served by the SPA-fallback routes so a hard
// refresh or a pasted URL lands on the right view; NOT #hashes):
//   /loops[/<name>]  (the landing)
//   /agents[/<id>]
//   /origins  |  /projects  (legacy /products still resolves to the Projects view)
// REDESIGN-SPEC §2/§5.1 — the standalone `/runs` surface is gone: an old
// `/runs` deep link (with or without a query) now resolves to Loops, the new home
// of the by-origin/agent/project value (as filters). No query state is parsed.
// Push (or replace) a real path. pushState does NOT fire popstate, so every caller
// still drives its own render() as before — the listener below only handles the
// browser Back/Forward buttons.
function _writeUrl(path,{replace}={}){
  if((location.pathname+location.search)===path)return;
  if(replace)history.replaceState(null,'',path);else history.pushState(null,'',path);
}
function applyUrl(){
  const path=(location.pathname||'/').replace(/\/+$/,'')||'/';
  let m;
  if((m=path.match(/^\/agents\/(.+)$/))){view='agents';curAgent=decodeURIComponent(m[1])}
  else if((m=path.match(/^\/loops\/(.+)$/))){view='loops';sel=decodeURIComponent(m[1]);selHost='local'}
  else if((m=path.match(/^\/issues\/(.+)$/))){view='issues';issueSel=decodeURIComponent(m[1])}
  else if(path==='/runs'){view='loops';curAgent=null}   // retired surface → Loops
  // /roles is the rename alias for the Agents registry (rd-sessions) — same view.
  else if((m=path.match(/^\/(overview|loops|hub|agents|roles|sessions|origins|projects|products|issues|terminal|newloop)$/))){view=(m[1]==='products'?'projects':(m[1]==='roles'?'agents':m[1]));curAgent=null;if(view!=='loops')sel=null;if(view!=='issues'){issueSel=null;issueDetail=null}}
  else{view='loops'}
}
// Back-compat: an old bookmarked #/… hash → replace it into a real path once,
// on boot, so the pushState router (and any later refresh) resolves it.
function _migrateLegacyHash(){
  const raw=(location.hash||'').replace(/^#/,'');
  if(!raw||raw.charAt(0)!=='/')return;
  history.replaceState(null,'',raw);
}
window.addEventListener('popstate',()=>{applyUrl();render();syncToolPanes();
  if(view==='agents'&&curAgent&&!agentDetail)openAgent(curAgent);
  if(view==='loops'&&sel)pick(sel,selHost||'local');
  if(view==='issues'&&issueSel&&!issueDetail)openIssue(issueSel);
});


// ── Loops view (list + detail) ─────────────────────────────────────────────────
// The Loops list, honestly scoped: archived toggle, name filter, the active
// project (REDESIGN-SPEC §2 — matches the loop's explicit `project`/`product`
// binding), and the single-agent chip (REDESIGN-SPEC §Q1 — dev-1's `single_agent`
// field). Every filter is AND-ed; an unset filter is a no-op.
function visibleLoops(){const sa=(document.getElementById('showArch')||{}).checked;
  const q=(loopFilter||'').toLowerCase().trim();
  return loops.filter(d=>(sa||!d.archived)
    &&(!q||(d.name||'').toLowerCase().includes(q))
    &&inScope(d)
    &&(!loopSingleFilter||!!d.single_agent))}
// A stable identity for a loop card: host + name. Used as the reconcile key so
// the 5s tick can patch cards in place instead of rebuilding the whole list.
function lcardKey(d){return (d.host||'local')+'␟'+(d.name||'');}
function lcardClass(d){return 'lcard'+((sel===d.name&&selHost===d.host)?' sel':'')+(d.archived?' arch':'');}
// D-FE (read side): a loop's project + origin BINDINGS as small chips. Project
// pivots to that Projects page; origin pivots to the Origins view. Renders nothing
// when a loop carries neither (every legacy loop, until the creator flow binds
// them) — purely additive, no empty chrome on existing cards. Reads the canonical
// `project` binding with a `product` fallback (Round A rename, both are emitted).
function bindChips(d){
  const chips=[];
  const proj=d.project||d.product;
  if(proj)chips.push(`<span class="bindchip prod" title="Project this loop targets — open Projects"
    onclick="event.stopPropagation();navGo('projects')">▦ ${esc(proj)}</span>`);
  if(d.origin)chips.push(`<span class="bindchip orig" title="Origin this loop targets — open Origins"
    onclick="event.stopPropagation();navGo('origins')">◉ ${esc(d.origin)}</span>`);
  return chips.length?`<div class="binds">${chips.join('')}</div>`:'';
}
// Reverse lookups for the catalog reverse-lists: which loops bind to a project /
// origin. Reads the same in-memory `loops` feed (already loaded) — no new fetch.
// Matches on the canonical `project` binding with a `product` fallback.
function loopsForProject(pid){return (loops||[]).filter(l=>l&&(l.project||l.product)===pid)}
function loopsForOrigin(oid){return (loops||[]).filter(l=>l&&(l.origin||l.host)===oid)}
// REDESIGN-SPEC §rd-origins / §8 nav ("◉ Origins 2") — the honest ORIGIN count is
// BOXES only (local + mirrors). A connected-session is targetable compute whose
// home is the Sessions surface, NOT an origin (Origin ≠ session); counting it
// inflated the persistent count to a dishonest "3 origins" when only two real
// boxes exist (uxui). Sessions are surfaced separately so the number never lies.
const isSessionOrigin=o=>o&&(o.kind||'').toLowerCase()==='connected-session';
const isLocalOrigin=o=>o&&((o.kind||'').toLowerCase()==='local'||o.id==='local');
function originBoxes(){return (origins||[]).filter(o=>o&&!isSessionOrigin(o))}
function originSessions(){return (origins||[]).filter(isSessionOrigin)}
function reachableBoxes(){return originBoxes().filter(o=>o.reachable!==undefined?o.reachable:isLocalOrigin(o))}
// The honest, never-inflated origin summary shown in the top-bar + Origins header:
// N boxes · K reachable (· S sessions) — the §2 fleet-pill honesty in one line.
function originCountLabel(){
  const b=originBoxes().length,r=reachableBoxes().length,s=originSessions().length;
  return `${b} origin${b===1?'':'s'} · ${r} reachable`+(s?` · ${s} session${s===1?'':'s'}`:'');
}
function boundLoopsHtml(list){
  if(!list.length)return '<span class="boundnone">no loops bound yet</span>';
  return `<div class="boundloops">${list.map(l=>`<span class="boundloop"
    onclick="jumpToLoop('${esc(l.name)}')" title="Open loop ${esc(l.name)}">⟲ ${esc(l.name)}</span>`).join('')}</div>`;
}
// §rd-origins — a chip-wall-safe variant: preview a few bound loops, then collapse
// the rest to a "+N more" that pivots to the scoped Loops list (never dumps 60+).
function boundLoopsCapped(list,oid,cap){
  if(!list.length)return '<span class="boundnone">no loops bound yet</span>';
  cap=cap||6;
  const head=list.slice(0,cap).map(l=>`<span class="boundloop"
    onclick="event.stopPropagation();jumpToLoop('${esc(l.name)}')" title="Open loop ${esc(l.name)}">⟲ ${esc(l.name)}</span>`).join('');
  const rest=list.length-cap;
  const more=rest>0?`<span class="boundloop more" onclick="event.stopPropagation();scopeLoopsTo('origin','${esc(oid)}')" title="View all loops on this origin in the Loops list">+${rest} more →</span>`:'';
  return `<div class="boundloops">${head}${more}</div>`;
}
function lcardInner(d){const st=d.state||'saved';const t=d.team||{};
  // REDESIGN-SPEC §Q1 — a loop-of-1 reads as one primitive, not "0W · 0I"; the
  // solo tag says so plainly (keyed off dev-1's `single_agent` field).
  const nteam=d.single_agent?'<span class="soloflag" title="A loop of one — a single-agent loop">◐ loop-of-1</span>'
    :((t.workers||[]).length+'W · '+(t.inputs||[]).length+'I'+(t.manager?' · 1M':''));
  const turns=d.turns_used!=null?('turn '+d.turns_used+(d.turnLimit?'/'+d.turnLimit:'')):'';
  const hc=d.host==='local'?'host':'host anneke';
  const hst=honestState(d);
  return `<div class="top"><span class="${hc}">${esc(d.host)}</span><span class="lname">${esc(d.name)}</span><span class="badge b-${hst}">${hst.replace('_',' ')}</span>${dispFlagHtml(d.name)}</div>
    <div class="meta"><span>${nteam}</span>${turns?`<span>${turns}</span>`:''}<span class="ago">${ago(d.updated||d.started)}</span></div>
    ${bindChips(d)}
    ${d.last_report?`<div class="team">last: <b>${esc(d.last_report.agent)}</b> → ${esc(d.last_report.status)}</div>`:''}`;}
function lcard(d){
  return `<div class="${lcardClass(d)}" data-key="${esc(lcardKey(d))}" onclick="pick('${esc(d.name)}','${esc(d.host)}')">${lcardInner(d)}</div>`;}
// SLICE-1B §3.1 — the owner's daily screen, grouped by product. Pure presentation
// over the already-loaded `loops` feed; the per-loop product is `d.project||d.product`
// (the same binding bindChips/loopsForProject already read). Attributed groups keep
// their first-seen order; the "Unattributed" bucket (git-only attribution returns
// no product) always sorts LAST. No new fetch, no data change — additive only.
function groupLoopsByProduct(vis){
  const map=new Map(),order=[];
  (vis||[]).forEach(d=>{
    const pid=(d.project||d.product)||'';
    const key=pid||'__unattr__';
    // `name` is the humanized header label the backend derived from the product
    // (e.g. "Bot Swarm" for the bot-swarm remote); fall back to the pid slug.
    if(!map.has(key)){map.set(key,{pid:pid,name:(d.productName||pid||''),loops:[]});if(key!=='__unattr__')order.push(key);}
    map.get(key).loops.push(d);
  });
  const groups=order.map(k=>map.get(k));
  if(map.has('__unattr__'))groups.push(map.get('__unattr__'));
  return groups;
}
function groupHdrInner(g){
  return `<span class="lgrp-name">${g.pid?('▦ '+esc(g.name||g.pid)):'Unattributed'}</span><span class="lgrp-cnt">${g.loops.length}</span>`;
}
// The ordered stream of render items the card container holds: group headers
// interleaved with their loop cards. Grouping engages as soon as ANY loop
// carries a real product (SLICE-1B §3.1) — so the owner's daily screen visibly
// reads as grouped (clear product headers, "Unattributed" bucket last) even
// when the honest git signal converges every loop onto ONE product. Only when
// NOTHING is attributed (zero products — a single all-"Unattributed" bucket adds
// no information) does it stay the flat list it has always been.
// Card items carry their loop `d`; header items don't (no pick/onclick). Each
// item has a stable `key` so reconcileCards can patch it in place across ticks.
function loopRenderItems(vis){
  const groups=groupLoopsByProduct(vis);
  const attributed=groups.filter(g=>g.pid).length;
  // When a specific project scope is active, the switcher + the scope chip already
  // name the group; a per-group header just repeats it (uxui: redundant header when
  // scoped). Fall back to the flat card list — grouping is only informative in the
  // "All projects" meta-view, where more than one group actually appears.
  if(attributed===0||activeProject){
    return (vis||[]).map(d=>({kind:'card',key:lcardKey(d),cls:lcardClass(d),inner:lcardInner(d),d:d}));
  }
  const items=[];
  groups.forEach(g=>{
    items.push({kind:'hdr',key:'__grp__'+(g.pid||'__unattr__'),cls:'lgrp',inner:groupHdrInner(g)});
    g.loops.forEach(d=>items.push({kind:'card',key:lcardKey(d),cls:lcardClass(d),inner:lcardInner(d),d:d}));
  });
  return items;
}
function loopItemHtml(it){
  if(it.kind==='hdr')return `<div class="lgrp" data-key="${esc(it.key)}">${it.inner}</div>`;
  return `<div class="${it.cls}" data-key="${esc(it.key)}" onclick="pick('${esc(it.d.name)}','${esc(it.d.host)}')">${it.inner}</div>`;
}
function loopsHeadText(vis,q,totalUnfiltered){
  return `${vis.length}${q?'/'+totalUnfiltered:''} loop${vis.length===1?'':'s'} · ${hosts.length} origin${hosts.length===1?'':'s'}`;}
// Keyed, in-place reconcile of the .lcard children of `container` against `vis`.
// Unchanged cards keep their exact DOM node (no innerHTML write); only changed
// cards are patched, new ones created, gone ones removed — so scroll position
// and unrelated layout survive a 5s tick. Returns a small op-count for tests.
function reconcileCards(container,vis){
  // `want` is the ordered render stream — group headers (kind:'hdr') interleaved
  // with loop cards (kind:'card') when grouping is active, or a flat card list
  // otherwise. Headers reconcile through the same keyed path as cards; they just
  // carry no `d` and get no pick() handler. Everything the flat list did before
  // (in-place patch, scroll preservation, op-count) is preserved.
  const want=loopRenderItems(vis);
  const wantKeys=new Set(want.map(w=>w.key));
  const have=new Map();
  Array.from(container.children).forEach(ch=>{const k=ch.getAttribute&&ch.getAttribute('data-key');if(k!=null)have.set(k,ch);});
  const ops={created:0,patched:0,removed:0,kept:0};
  have.forEach((el,k)=>{if(!wantKeys.has(k)){el.remove();ops.removed++;}});
  let prev=null;
  want.forEach(w=>{
    let el=have.get(w.key);
    if(!el){
      el=container.ownerDocument.createElement('div');
      el.setAttribute('data-key',w.key);
      el.className=w.cls;
      if(w.kind!=='hdr')el.onclick=(function(name,host){return function(){pick(name,host);};})(w.d.name,w.d.host);
      el.innerHTML=w.inner;
      el._inner=w.inner;el._cls=w.cls;
      ops.created++;
    }else{
      let touched=false;
      if(el._cls!==w.cls){el.className=w.cls;el._cls=w.cls;touched=true;}
      if(el._inner!==w.inner){el.innerHTML=w.inner;el._inner=w.inner;touched=true;}
      touched?ops.patched++:ops.kept++;
    }
    const ref=prev?prev.nextSibling:container.firstChild;
    if(ref!==el)container.insertBefore(el,ref);
    prev=el;
  });
  return ops;
}
let _loopsChromeSig=null;
function renderLoops(){
  const el=document.getElementById('v-loops');
  if(!el)return;
  const vis=visibleLoops();
  const q=(loopFilter||'').toLowerCase().trim();
  const totalUnfiltered=loops.filter(d=>(document.getElementById('showArch')||{}).checked||!d.archived).length;
  const savedLoops=(regSavedLoops||[]);
  const savedShelf=savedLoops.length?`<div class="savedshelf">
    <span class="kick">★ saved templates <span class="stone" style="letter-spacing:0;text-transform:none;font-weight:500">— clone one to start</span></span>
    ${savedLoops.map(l=>`<div class="savedrow">
      <span class="rid" title="${esc(l.id)}">${esc(l.id)}</span><span class="rn">${esc(l.note||'')}</span>
      <button class="btn ghost" onclick="cloneLoop('${esc(l.id)}')" title="Clone into a new runnable loop">Clone…</button>
    </div>`).join('')}</div>`:'';
  const cardsMode=vis.length>0;
  const emptyKind=cardsMode?'':(loadErr.loops?('err:'+loadErr.loops):(q?'nomatch':(loopSingleFilter||activeProject?'noscope':'none')));
  // REDESIGN-SPEC §Q1 — how many single-agent (loop-of-1) loops match the OTHER
  // filters (archived/name/project), so the chip can show a count and hide when
  // there are none. Deliberately ignores loopSingleFilter itself.
  const scopeBase=loops.filter(d=>((document.getElementById('showArch')||{}).checked||!d.archived)
    &&(!q||(d.name||'').toLowerCase().includes(q))
    &&inScope(d));
  const singleCount=scopeBase.filter(d=>!!d.single_agent).length;
  // The "chrome" is everything OUTSIDE the card list (search bar, saved shelf,
  // empty state, detail-pane presence). When it is unchanged we take the cheap
  // incremental path; when it changes we rebuild the shell once. The active
  // project + single-agent chip are chrome (they change the header + which cards
  // are eligible), so they participate in the signature.
  // the per-project disposition rollup (§5.4) is chrome (a header line); fold a
  // small signature of it in so a fresh rating rebuilds the header, not just cards.
  const dispSig=dispAgg?(dispAgg.rated+'/'+dispAgg.good+'/'+dispAgg.ok_count+'/'+dispAgg.bad):'';
  const chromeSig=JSON.stringify([totalUnfiltered>=6,q,q?vis.length:0,totalUnfiltered,savedShelf,emptyKind,!!sel,hosts.length,activeProject||'',loopSingleFilter,singleCount,dispSig]);
  const cardsBox=document.getElementById('lp_cards');
  if(cardsBox&&cardsMode&&_loopsChromeSig===chromeSig){
    // ── incremental: patch only changed cards, preserve scroll ──
    const list=document.getElementById('lp_list');
    const sy=list?list.scrollTop:0;const wy=(window.scrollY||window.pageYOffset||0);
    const cnt=document.getElementById('lp_count');
    if(cnt)cnt.textContent=loopsHeadText(vis,q,totalUnfiltered);
    reconcileCards(cardsBox,vis);
    if(list)list.scrollTop=sy;try{window.scrollTo(0,wy);}catch(e){}
    if(sel&&detail)renderDetail(detail);
    return;
  }
  // ── full rebuild of the shell (first paint, or chrome changed) ──
  const emptyNone=`<div class="empty"><span class="kick">the yard is quiet</span>
    <span class="lede">No loops here yet — author one and give the crew somewhere to run.</span>
    <button class="btn primary" style="margin-top:6px" onclick="openEditor()">＋ New loop</button></div>`;
  const emptyNoMatch=`<div class="empty"><span class="kick">no loops match "${esc(loopFilter)}"</span>
    <span class="lede">${totalUnfiltered} in the yard — try a different fragment.</span>
    <button class="btn ghost" style="margin-top:6px" onclick="setLoopFilter('')">clear filter</button></div>`;
  // Honest empty state when the active project and/or the single-agent chip hide
  // everything (REDESIGN-SPEC §2/§Q1) — say which scope is in effect, offer to lift it.
  const scopeName=activeProject?(activeProject===UNATTR?'Unattributed':((projectScopes().find(s=>s.id===activeProject)||{}).name||activeProject)):'';
  const emptyNoScope=`<div class="empty"><span class="kick">no loops in this scope</span>
    <span class="lede">${totalUnfiltered} loop${totalUnfiltered===1?'':'s'} in the yard, but none match ${activeProject?`project <b>${esc(scopeName)}</b>`:''}${activeProject&&loopSingleFilter?' + ':''}${loopSingleFilter?'the <b>single-agent</b> filter':''}.</span>
    <div style="margin-top:8px">${activeProject?`<button class="btn ghost" onclick="setActiveProject('')">all projects</button> `:''}${loopSingleFilter?`<button class="btn ghost" onclick="toggleSingleFilter()">clear single-agent</button>`:''}</div></div>`;
  const items=loopRenderItems(vis);
  const listHtml=vis.length?items.map(loopItemHtml).join(''):(loadErr.loops?emptyErr('loops list unavailable',loadErr.loops):(q?emptyNoMatch:((loopSingleFilter||activeProject)?emptyNoScope:emptyNone)));
  const detailHtml=sel?'':`<div class="empty"><div class="kick" style="margin-bottom:10px">the loop is the crew</div>
    A crew of agents that plans, hands off, and reviews until the work is done — pick one to watch it work, or author a fresh one.<br>
    <button class="btn primary" style="margin-top:14px" onclick="openEditor()">＋ New loop</button></div>`;
  // Search only surfaces when the list is long enough to warrant it; below the
  // threshold, eye-scanning is faster than filtering.
  const searchBar=totalUnfiltered>=6?`<div style="margin:8px 0 10px">
    <label class="searchbox" for="lp_search">
      <span class="sglyph" aria-hidden="true">⌕</span>
      <input id="lp_search" placeholder="find a loop by name fragment…" value="${esc(loopFilter)}"
        oninput="setLoopFilter(this.value)" spellcheck="false" aria-label="Filter loops by name">
      ${q?`<button class="sclear" type="button" onclick="setLoopFilter('')" title="Clear" aria-label="Clear loop search">×</button>`:''}
    </label>
    ${q?`<span class="searchmeta">${vis.length} of ${totalUnfiltered}</span>`:''}
  </div>`:'';
  // Filter chips: the single-agent (loop-of-1) chip surfaces whenever any single-
  // agent loop exists in the current scope (REDESIGN-SPEC §Q1); the active-project
  // chip echoes the global switcher and offers a one-click clear (REDESIGN-SPEC §2).
  const filterChips=(singleCount||loopSingleFilter||activeProject)?`<div class="loopchips">
    ${activeProject?`<button class="lchip proj on" onclick="setActiveProject('')" title="Clear the project scope — show all projects">▦ ${esc(scopeName)} <span class="lx">×</span></button>`:''}
    ${(singleCount||loopSingleFilter)?`<button class="lchip ${loopSingleFilter?'on':''}" onclick="toggleSingleFilter()" aria-pressed="${loopSingleFilter?'true':'false'}" title="Show only loop-of-1 (single-agent) loops">◐ single-agent${singleCount?` <span class="lchipn">${singleCount}</span>`:''}</button>`:''}
  </div>`:'';
  el.innerHTML=`<div class="two-pane">
    <div class="list" id="lp_list">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <span class="kick" id="lp_count">${loopsHeadText(vis,q,totalUnfiltered)}</span>
        <button class="ibtn go" onclick="openEditor()" title="Author a new loop">＋</button>
      </div>
      ${searchBar}
      ${filterChips}
      ${dispRollHtml()}
      ${savedShelf}
      <div id="lp_cards">${listHtml}</div>
    </div>
    <div class="detail" id="loopDetail">${detailHtml}</div>
  </div>`;
  _loopsChromeSig=chromeSig;
  // Stamp the freshly-parsed cards with their content signature so the NEXT
  // tick can diff them without rebuilding (they came from innerHTML, so the
  // _inner/_cls props aren't set yet). Order matches vis (listHtml = vis.map).
  if(cardsMode){
    const kids=document.getElementById('lp_cards').children;
    for(let i=0;i<items.length&&i<kids.length;i++){kids[i]._inner=items[i].inner;kids[i]._cls=items[i].cls;}
  }
  if(sel&&detail)renderDetail(detail);
}
function setLoopFilter(v){
  loopFilter=v||'';
  renderLoops();
  const box=document.getElementById('lp_search');
  if(box){box.focus();const n=box.value.length;try{box.setSelectionRange(n,n)}catch(e){}}
}
// REDESIGN-SPEC §Q1 — toggle the loop-of-1 (single-agent) filter on the Loops list.
function toggleSingleFilter(){loopSingleFilter=!loopSingleFilter;renderLoops();}

// ── Agents view (Favorites shelf + full table + agent detail) ──────────────────
function renderAgents(){
  const el=document.getElementById('v-agents');
  if(curAgent){el.innerHTML='<div class="page">'+agentBody()+'</div>';return}
  const favs=(regSavedAgents||[]).filter(a=>a.favorite);
  const savedById={};for(const a of regSavedAgents||[])savedById[a.id]=a;
  const maxg=Math.max(1,...regAgents.map(a=>a.avg_gap_min||0));
  const q=(agentFilter||'').toLowerCase().trim();
  const preMatches=q?regAgents.filter(a=>(a.agent||'').toLowerCase().includes(q)):regAgents.slice();
  // Sort key access — agent id is the only string field; the rest are numeric.
  // Missing values sort as 0 for numbers / '' for the id.
  const sortKey=a=>{const c=agentSort.col;
    if(c==='agent')return (a.agent||'').toLowerCase();
    if(c==='avg')return a.avg_gap_min||0;
    return a[c]||0;
  };
  const matches=preMatches.sort((a,b)=>{const ka=sortKey(a),kb=sortKey(b);
    if(ka===kb)return 0; return (ka<kb?-1:1)*agentSort.dir;
  });
  const rows=matches.map(a=>{
    const s=savedById[a.agent];
    const mgr=/manager|orchestrat|coordinat/i.test(a.agent);
    const w=Math.round(64*(a.avg_gap_min||0)/maxg);
    const open=expandedAgents.has(a.agent);
    const isFav=!!(s&&s.favorite);
    const isSaved=!!s;
    let tr=`<tr class="clk ${open?'exp':''}" onclick="toggleAgentRow('${esc(a.agent)}')">
      <td><span class="tri ${open?'open':''}">▸</span> <span class="aid ${mgr?'mgr':''}">${esc(a.agent)}</span>${isSaved?'<span class="v2tag" style="margin-left:8px">saved</span>':''}</td>
      <td class="num">${a.loop_count}</td><td class="num">${a.turns}</td>
      <td class="num"><span class="bar" style="width:${w}px"></span>${mins(a.avg_gap_min)}</td>
      <td class="num">${a.continues||0}</td>
      <td class="num">${a.retries||0}</td>
      <td><span class="star ${isFav?'on':''}" role="button" tabindex="0"
        aria-pressed="${isFav?'true':'false'}" aria-label="${isFav?'Unfavorite':'Favorite'} ${esc(a.agent)}"
        onclick="event.stopPropagation();toggleFav('${esc(a.agent)}',${isFav?'false':'true'})" title="${isFav?'unfavorite':'favorite this agent'}">★</span></td>
    </tr>`;
    if(open){
      const loopchips=(a.loops||[]).map(l=>`<span class="pill2" style="cursor:pointer" onclick="event.stopPropagation();jumpToLoop('${esc(l)}')">${esc(l)}</span>`).join('');
      const st=(k,lbl)=>`<div class="stat"><div class="k">${lbl}</div><div class="v">${a[k]||0}</div></div>`;
      tr+=`<tr class="exp"><td class="xp" colspan="7"><div class="xpbox" onclick="event.stopPropagation()">
        <div class="stats">${st('turns','turns')}${st('loop_count','loops')}${st('continues','continues')}${st('retries','retries')}
          <div class="stat"><div class="k">avg turn</div><div class="v">${mins(a.avg_gap_min)}</div></div></div>
        <div class="chips" style="max-width:none">${statusChips(a.statuses)}</div>
        <div style="margin:10px 0 4px;color:var(--stone);font-size:11px;font-family:var(--mono);letter-spacing:.06em;text-transform:uppercase">Appears in</div>
        <div class="chips" style="max-width:none">${loopchips}</div>
        <div class="xpact">
          <span class="star ${isFav?'on':''}" onclick="toggleFav('${esc(a.agent)}',${isFav?'false':'true'})">${isFav?'★ favorited':'☆ favorite'}</span>
          <a class="linkbtn" onclick="openAgent('${esc(a.agent)}')">open detail →</a>
        </div></div></td></tr>`;
    }
    return tr}).join('');
  const emptyTable=regAgents.length
    ? (q?`<div class="emptyline"><span class="kick">no agents match "${esc(agentFilter)}"</span>
        ${regAgents.length} tracked; try a different fragment.
        <button class="btn ghost" style="margin-top:12px" onclick="setAgentFilter('')">clear</button></div>`
      :'')
    :(loadErr.agents?emptyErr('agents unavailable',loadErr.agents)
      :'<div class="emptyline"><span class="kick">no agent activity yet</span>Run a loop or a standalone run and the crew shows up here.</div>');
  const sortableTh=(col,label,cls)=>{const on=agentSort.col===col;
    const arrow=on?(agentSort.dir===1?' ▲':' ▼'):'';
    return `<th class="${cls||''} sortable ${on?'on':''}" onclick="setAgentSort('${col}')"
      role="button" tabindex="0" aria-sort="${on?(agentSort.dir===1?'ascending':'descending'):'none'}"
      title="Sort by ${label}">${label}<span class="sarrow">${arrow||' ↕'}</span></th>`};
  const table=matches.length?`<table class="table"><thead><tr>
    ${sortableTh('agent','agent','')}${sortableTh('loop_count','loops','num')}${sortableTh('turns','turns','num')}
    ${sortableTh('avg','avg turn ⌀','num')}${sortableTh('continues','continues','num')}${sortableTh('retries','retries','num')}
    <th>fav</th></tr></thead><tbody>${rows}</tbody></table>`:emptyTable;
  const favBlock=favs.length?`<div style="margin:8px 0 20px">
    <div class="logh" style="margin:0 0 8px">★ favorites <span class="stone">— your pinned crew</span></div>
    <div class="grid three">${favs.map(a=>`<div class="card" style="padding:14px 16px;cursor:pointer" onclick="openAgent('${esc(a.id)}')">
      <div style="display:flex;align-items:center;gap:8px"><span class="star on" onclick="event.stopPropagation();toggleFav('${esc(a.id)}',false)">★</span>
        <span class="aid" style="font-size:14px">${esc(a.id)}</span></div>
      ${a.model?`<div class="mono stone" style="font-size:11px;margin-top:6px">${esc(a.model)}</div>`:''}
      ${a.note?`<div class="bone-dim" style="font-size:12px;margin-top:6px">${esc(a.note)}</div>`:''}
    </div>`).join('')}</div></div>`:'';
  const searchBar=regAgents.length>=6?`<div style="display:flex;align-items:center;gap:0;margin:8px 0 12px">
    <label class="searchbox" for="ag_search">
      <span class="sglyph" aria-hidden="true">⌕</span>
      <input id="ag_search" placeholder="find an agent by id fragment…" value="${esc(agentFilter)}"
        oninput="setAgentFilter(this.value)" spellcheck="false" aria-label="Filter agents by id">
      ${q?`<button class="sclear" type="button" onclick="setAgentFilter('')" title="Clear" aria-label="Clear search">×</button>`:''}
    </label>
    ${q?`<span class="searchmeta">${matches.length} of ${regAgents.length}</span>`:''}
  </div>`:'';
  el.innerHTML=`<div class="page">
    <div class="pagehead"><h1 class="h1">Roles</h1><span class="tag">${regAgents.length} tracked</span>
      <span class="spacer"></span></div>
    <p class="pageintro">The <b>role templates</b> a loop can be assembled from — the building blocks, not the live collaborators (those are <a class="ilink" onclick="navGo('sessions')">Sessions</a>). Every role has a detail view; star the ones you run often and they pin to the Favorites shelf — one click to open, one click to fire a standalone run.</p>
    ${favBlock}
    ${searchBar}
    <div class="logh" style="margin-top:14px">all agents <span class="stone">— click a row to expand · click the id to open the full detail</span></div>
    ${table}</div>`;
}
function toggleAgentRow(a){if(expandedAgents.has(a))expandedAgents.delete(a);else expandedAgents.add(a);renderAgents()}
function setAgentFilter(v){
  agentFilter=v||'';
  renderAgents();
  // renderAgents() re-innerHTMLs the view, so we restore focus + caret on the
  // search input; without this every keystroke would drop focus.
  const box=document.getElementById('ag_search');
  if(box){box.focus();const n=box.value.length;try{box.setSelectionRange(n,n)}catch(e){}}
}
// Clicking the same column flips direction; a new column starts numeric-desc
// (highest first) which is what an owner scanning "who's doing the most" wants.
// The agent-id column defaults to ascending — alphabetical read.
function setAgentSort(col){
  if(agentSort.col===col)agentSort.dir=-agentSort.dir;
  else agentSort={col,dir:col==='agent'?1:-1};
  renderAgents();
}
function statusChips(s){return Object.entries(s||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<span class="pill2">${esc(k)} ${v}</span>`).join('')}
async function toggleFav(agent,fav){
  const res=await post('/api/loops/agent/favorite',{id:agent,favorite:!!fav});
  if(res&&res.error)return flash(res.error,true);
  flash((fav?'★ pinned ':'☆ unpinned ')+agent,false);
  // reflect in local caches (favorite flag on registry entry)
  const s=(regSavedAgents||[]).find(x=>x.id===agent);
  if(s)s.favorite=!!fav; else if(fav){(regSavedAgents=regSavedAgents||[]).push({id:agent,note:'',favorite:true})}
  render();
}
function jumpToLoop(n){view='loops';sel=n;selHost='local';_writeUrl('/loops/'+encodeURIComponent(n));render();pick(n,'local')}

// ── Origins view ───────────────────────────────────────────────────────────────
// Render the honest per-CLI chip row for one origin. Never fabricates authed:true
// — 'unknown' and false both render as neutral/off; only real positive evidence
// paints the phos "authed" chip. Applied is ALWAYS labelled as "recorded" today
// (recorded ≠ applied at spawn — see suggestions/MODEL-PASSTHROUGH-daemon.md).
function _capChip(c){
  const authed=c.authed===true?'authed':(c.authed===false?'no auth':'auth unknown');
  const cls=c.authed===true?'phos':(c.authed===false?'':'ember');
  const where=c.remote?' · checked on that origin':'';
  return `<span class="pill ${cls}" title="${esc((c.note||'')+where)}">${esc(c.cli)} · ${authed} · recorded</span>`;
}
function _capsRowHtml(o, isLocal){
  // D5 — render from the capabilities EMBEDDED in the origins list (probed on
  // every /api/loops/origins), falling back to the lazily-fetched record so the
  // launcher's model gate still has a source. Never fabricates a chip.
  const emb=o&&o.capabilities;
  const rec=(emb!==undefined)
    ? {ok:o.capabilitiesOk!==false,cliCapabilities:emb||[],reason:o.capabilitiesReason}
    : capsByOrigin[o&&o.id];
  if(!rec){return `<div class="ometa" style="margin-top:2px"><span class="chip">${isLocal?'probing local CLIs…':'capabilities unprobed'}</span></div>`}
  const caps=rec.cliCapabilities||[];
  if(!caps.length){
    // A remote origin is probed ON that origin (capabilities.probe over the
    // origin channel); an unreachable one is honestly "not checked" (reason in
    // the tooltip), never "none found" — never fabricate chips.
    if(rec.ok===false){
      const why=rec.reason||'origin could not be asked which CLIs it has';
      return `<div class="ometa" style="margin-top:2px"><span class="chip" title="${esc(why)}">CLIs not checked</span></div>`;
    }
    return `<div class="ometa" style="margin-top:2px"><span class="chip" title="${esc(rec.reason||'')}">${isLocal?'no CLIs on PATH':'no CLIs on that origin'}</span></div>`;
  }
  return `<div class="ometa" style="margin-top:2px">${caps.map(_capChip).join('')}</div>`;
}
// §3.4 — a connected session declares FREE-LABEL capabilities (e.g. "browser",
// "interactive"), not the probe-shaped {cli,authed} dicts _capChip renders. Show
// them as plain chips; when it declares none, say so honestly (no fabrication).
function _sessionCapsHtml(o){
  const caps=(o&&o.sessionCaps)||[];
  if(!caps.length){return '<div class="ometa" style="margin-top:2px"><span class="chip" title="this session declared no capability labels">no capabilities declared</span></div>';}
  return `<div class="ometa" style="margin-top:2px">${caps.map(c=>`<span class="pill phos" title="declared by the connected session">${esc(c)}</span>`).join('')}</div>`;
}
// Per-project origin-USAGE aggregate (REDESIGN-SPEC §2 — "per-project origin usage
// re-scopes instantly"). The per-card counts already re-scope (below); this is the
// page-level headline so the scope is FELT on the Origins page, not only buried in a
// chip: when a project is active it reads "▦ <project> · N loops on K origins",
// computed from the same in-scope loop feed. Fleet view (no scope) returns '' — the
// honest fleet count label stands alone. No new fetch — pure re-composition.
function originUsageScopedHtml(){
  if(!activeProject)return'';
  const scoped=(loops||[]).filter(inScope);
  const boxes=new Set(scoped.map(l=>(l&&(l.origin||l.host))||'').filter(Boolean));
  const n=scoped.length,k=boxes.size;
  const nm=activeProject===UNATTR?'Unattributed'
    :((projectScopes().find(s=>s.id===activeProject)||{}).name||activeProject);
  return `<span class="tag scoped" title="Scoped to the active project (REDESIGN-SPEC §2): Loops, Idea Hub, Issues and origin usage all re-scope in lockstep.">▦ ${esc(nm)} · ${n} loop${n===1?'':'s'} on ${k} origin${k===1?'':'s'}</span>`;
}
function renderOrigins(){
  const el=document.getElementById('v-origins');
  const list=(origins||[]).length?origins.map(o=>{
    const isLocal=(o.kind||'').toLowerCase()==='local'||o.id==='local';
    // §3.4 — a connected tmux/app session surfaced as an origin (the demoted
    // terminal's home): a live executor with a heartbeat + declared capabilities.
    const isSession=(o.kind||'').toLowerCase()==='connected-session';
    const reachable=o.reachable!==undefined?o.reachable:isLocal;
    const stale=!!o.stale;
    // A box just added with `yard connect` but not yet synced (D6): honestly
    // PENDING, never "unreachable" — the engine surfaces health:"pending" with
    // connectedAt/address so the front door doesn't lie about a fresh add.
    const isPending=(o.health||'')==='pending';
    // D5 — health pill. Local is always live (we ARE the box); a mirror is live
    // when its sync is fresh, pending when connected-but-unsynced, stale when
    // seen-but-old, off when there's no evidence of sync at all.
    const pill=isLocal||reachable?'live':(isPending?'pending':(stale?'stale':'off'));
    // An off/stale/pending origin stays in the grid with the exact cause.
    const cause=isSession
      ? (reachable?'':`session idle · last seen ${o.last_seen?ago(o.last_seen):'unknown'} (no heartbeat &gt;90s)`)
      : (isPending
          ? `connected via yard · waiting for first sync${o.connectedAt?' · added '+ago(o.connectedAt):''}${o.address?' · '+o.address:''}`
          : ((!isLocal&&!reachable)
              ? (o.last_seen?`unreachable · last seen ${ago(o.last_seen)}`:'unreachable · no sync ever seen from this box')
              : ''));
    return `<div class="ocard ${isLocal?'local':''} ${isPending?'pending':((!isLocal&&!reachable)?'off':'')}">
      <div class="oh"><span class="oname">${esc(o.name||o.id)}</span>
        <span class="oid mono">${esc(o.id||'')}</span>
        <span class="spacer"></span>
        <span class="health-pill ${pill}" title="${esc(cause||(isLocal?'this dashboard\'s own box':'sync is fresh'))}">${pill}</span></div>
      <div class="ometa"><span class="chip">${esc(o.kind||'mirror')}</span>
        <span class="chip ${reachable?'on':''}" title="${isSession?(reachable?'heartbeat within 90s':'no heartbeat for &gt;90s'):(reachable?'mirror mtime is within FRESH_SEC':(isPending?'connected via yard, awaiting first sync':(stale?'seen once, no recent sync':'no evidence of sync')))}">${isSession?(reachable?'live session':'idle'):(reachable?'reachable':(isPending?'pending':(stale?'stale':'unreachable')))}</span>
        ${isSession?(o.runtime?`<span class="chip">${esc(o.runtime)}</span>`:''):(function(){
          // Per-project origin USAGE re-scopes with the switcher (REDESIGN-SPEC §2):
          // when a project is active show how many of ITS loops target this box,
          // not the box's fleet-wide total. Fleet view keeps the honest total.
          const n=activeProject?loopsForOrigin(o.id||'').filter(inScope).length:(o.loops||0);
          return `<span class="chip"${activeProject?' title="loops on this origin in the active project"':''}>${n} loop${n===1?'':'s'}${activeProject?' here':''}</span>`;})()}
        ${o.last_seen?`<span class="chip">seen ${ago(o.last_seen)}</span>`:''}</div>
      ${cause?`<div class="ocause ${isPending?'pending':''}">${isPending?'◔':'⚠'} ${esc(cause)}</div>`:''}
      ${isSession?_sessionCapsHtml(o):_capsRowHtml(o, isLocal)}
      <div class="opath mono">${esc(o.path||(isSession?('connected session · '+(o.id||'')):''))}</div>
      ${isLocal?'<div class="hint" style="margin:2px 0 0">The origin this dashboard runs on. Standalone runs prepared here land in this box\'s output tree.</div>':''}
      ${isSession?'<div class="hint" style="margin:2px 0 0">A live connected session — a running app/terminal that can execute dispatched tasks. Visible here as an origin; deeper act-on lands in a later slice.</div>':''}
      ${(function(){const b=loopsForOrigin(o.id||'').filter(inScope);
        // §rd-origins gap: KILL the chip-wall on a busy box (local can bind 60+).
        // Mirror the session card — a summary line + "View loops" (the pivot below);
        // show a few chips as a preview, collapse the rest to a "+N more" that pivots.
        return `<div class="obound"><span class="kick" style="font-size:10px">${b.length} loop${b.length===1?'':'s'} targeting this origin${activeProject?' · in scope':''}</span>${boundLoopsCapped(b,o.id||'',6)}</div>`;})()}
      <div style="display:flex;gap:8px;margin-top:6px">
        <button class="btn pivot" onclick="scopeLoopsTo('origin','${esc(o.id||'')}')" title="Open the Loops list"><span class="parr">▶</span> ${o.loops?'View loops on '+esc(o.id):'View loops'}</button>
      </div>
    </div>`}).join(''):(loadErr.origins?emptyErr('origins unavailable',loadErr.origins)
      :'<div class="emptyline"><span class="kick">this box didn\'t answer</span>Even the local origin is missing — the loops daemon isn\'t reporting. Nothing is hidden; there\'s just nothing to mirror yet.</div>');
  el.innerHTML=`<div class="page">
    <div class="pagehead"><h1 class="h1">Origins</h1><span class="tag" title="Boxes only (local + mirrors); connected sessions live on the Sessions surface (REDESIGN-SPEC §rd-origins)">${originCountLabel()}</span>${originUsageScopedHtml()}<span class="spacer"></span>
      <button class="btn primary" onclick="openAddOrigin()" title="Add a box you own — from the box, not here">＋ Add origin</button></div>
    <p class="pageintro">Origins are where work runs. <b>Local</b> is this dashboard's host (always here as the working default); the rest are fleet mirrors we can already read, plus any <b>connected sessions</b> — live apps/terminals that can execute dispatched tasks.
      Health is honest: mirror freshness from status.jsonl mtimes, a session's live/idle from its heartbeat (&gt;90s → idle).
      Per-origin CLI chips show which authed subscription CLIs are recorded on that box — never guessed.</p>
    <div class="grid two">${list}</div></div>`;
  // Kick off the capability probes in the background so chips paint as they resolve.
  // Only origins missing from the cache are fetched; re-visits are instant.
  // Connected sessions carry their declared caps inline (sessionCaps) — never
  // probe them via the mirror-caps endpoint (they have no _loops_<host> dir).
  const missing=(origins||[]).filter(o=>o&&o.id&&(o.kind||'').toLowerCase()!=='connected-session'&&!capsByOrigin[o.id]).map(o=>o.id);
  if(missing.length){Promise.all(missing.map(id=>loadOriginCaps(id))).then(()=>{if(view==='origins')renderOrigins()})}
}
// REDESIGN-SPEC §2/§Q1 — Origins/Projects cards pivot to the Loops list (the new
// home of the by-origin/project value that the deleted /runs feed used to carry).
// A project pivot also sets the global project switcher so the whole app re-scopes;
// an origin pivot just opens Loops (there is no per-origin Loops filter yet).
function scopeLoopsTo(axis,value){
  if((axis==='product'||axis==='project')&&value)setActiveProject(value);
  navGo('loops');
  flash(axis==='origin'?'Opened Loops':('Scoped to project '+value),false);
}

// ── Projects view ──────────────────────────────────────────────────────────────
// (Round A rename of the former Products catalog. The client hits the canonical
// /api/loops/projects* endpoints; the legacy /products* routes stay live.)
function renderProjects(){
  const el=document.getElementById('v-projects');
  const list=(projects||[]).length?projects.map(p=>{
    const git=p.gitRemote?`${esc(p.gitRemote)}${p.gitBranch?' · '+esc(p.gitBranch):''}`:'<span class="stone">no git remote</span>';
    // D-FE reverse list: the loops that target this project (read from configs).
    const bound=loopsForProject(p.id);
    const boundLine=`<div class="pbound"><span class="kick" style="font-size:10px">${bound.length} loop${bound.length===1?'':'s'} targeting</span>${boundLoopsHtml(bound)}</div>`;
    return `<div class="prow"><span class="pid">${esc(p.id)}</span>
      <span class="pmeta">${esc(p.name||'')}${p.name?' — ':''}${git}${p.note?' · '+esc(p.note):''}</span>
      <button class="btn pivot" onclick="scopeLoopsTo('product','${esc(p.id)}')" title="Scope the app to this project and open its Loops"><span class="parr">▶</span> Loops</button>
      <button class="btn stop" onclick="delProject('${esc(p.id)}')">Delete</button>
      ${boundLine}</div>`
  }).join(''):(loadErr.projects?emptyErr('projects unavailable',loadErr.projects)
    :'<div class="emptyline"><span class="kick">no projects yet</span>A project is a portable target (git repo + output root) any loop or standalone run can point at.</div>');
  // Auto-gather runs on every Projects-page load: this chip reports what the last
  // pass just materialized from loops we'd already run — zero manual entry.
  const g=projectsGathered;
  const gatherChip=g?`<span class="tag" title="Projects auto-gathered from loops already run on this box">${g.created?'▦ auto-gathered '+g.created+' from loops':'✓ all '+g.loops_scanned+' loops attributed'}</span>`:'';
  el.innerHTML=`<div class="page">
    <div class="pagehead"><h1 class="h1">Projects</h1><span class="tag">${projects.length} in catalog</span>${gatherChip}</div>
    <p class="pageintro">Projects are your codebases — the ground the loops run on. They're <b>auto-gathered</b> from loops you've already run,
      so this catalog fills itself in. To add one by hand, just paste a git URL or a directory path — the id and name are derived for you.</p>
    ${list}
    <div class="logh" style="margin-top:22px">add a project</div>
    <div class="hint">Paste a git repo URL (<code>git@github.com:me/my-app.git</code>) or a directory path (<code>/home/me/projects/my-app</code>). That's the whole input — id &amp; name are derived.</div>
    <div class="pform">
      <label class="full">git URL or path <input id="pf_source" placeholder="git@github.com:me/my-app.git  —or—  /home/me/projects/my-app" spellcheck="false" onkeydown="if(event.key==='Enter')addProject()"></label>
      <div class="full"><button class="btn primary" onclick="addProject()">Add project</button></div>
    </div></div>`;
}
// ── Product hub (capabilities) — NOT surfaced in Loopyard v1 ────────────────
// The per-project capability pages / hub are intentionally left out of the v1
// self-host build (see BRIEF: scope-OUT). The backend routes (/api/loops/
// capabilities and /capability/<id>/page) stay dormant but are not mounted in
// the UI: no nav entry, no per-project button, no openCaps/capMount client code.
// Removing the surface here keeps the dashboard free of dead links/iframes.
// ── Issues view (REDESIGN-SPEC §3 rd-issues — the reactive INBOX, not a crash
// diary) ─────────────────────────────────────────────────────────────────────
// The ONE project-scoped stream of faults loops FILL (file-and-keep-going) and
// loops DRAIN (point a loop → it resolves). A crash is just the
// `kind=crash, source=engine` slice — one kind, not the definition of Issues.
async function loadIssues(){
  try{const r=await j('/api/loops/issues');issues=(r&&r.issues)||[];loadErr.issues=(r&&r.error)||''}
  catch(e){issues=[];loadErr.issues=''+e}
}
async function openIssue(id){
  view='issues';  // the crash drill-in (forensics) is reachable from a crash card
  issueSel=id;issueDetail=null;_writeUrl('/issues/'+encodeURIComponent(id));render();  // paint the loading shell
  try{
    const r=await j('/api/loops/issue/'+encodeURIComponent(id));
    if(r&&r.error){flash(r.error,true);issueSel=null;issueDetail=null;_writeUrl('/issues');render();return}
    issueDetail=r;
  }catch(e){flash('issue unavailable — '+e,true);issueSel=null;issueDetail=null;_writeUrl('/issues');render();return}
  if(view==='issues')render();
}
// The lifecycle status chip (§3.3): open / snoozed / resolving / resolved /
// dismissed — NOT the raw engine `ended`. resolved/dismissed read as done.
const _ISTATUS_CLS={open:'',snoozed:'gup',resolving:'',resolved:'ok',dismissed:'',crash:'err'};
function _issueStatusChip(status){
  const s=(status||'open').toLowerCase();
  return `<span class="ichip ${_ISTATUS_CLS[s]||''}">${esc(s)}</span>`;
}
function _issueSourceLabel(src){
  // Honest source (dev-1's normalize_read derives it): engine / you / loop:<name>.
  // A genuinely absent source reads "unattributed" — never silently faked as "you".
  const s=(src||'').trim();
  if(!s)return 'unattributed';
  if(s==='engine')return 'engine';
  if(s==='you')return 'you';
  if(s.indexOf('loop:')===0)return 'loop '+s.slice(5);
  return esc(s);
}
// §3.3 — the legal triage actions offered per status (mirrors the backend state
// machine; the backend still enforces it, so an illegal click gets an honest toast).
function _issueActions(it){
  const s=(it.status||'open').toLowerCase();
  const a=[];
  if(s==='open'||s==='snoozed'){a.push(['point','▶ Point a loop','primary']);}
  if(s==='open'){a.push(['snoozed','Snooze','ghost']);}
  if(s==='snoozed'){a.push(['open','Resume','ghost']);}
  if(s==='open'||s==='snoozed'||s==='resolving'){a.push(['resolved','Resolve','ghost']);a.push(['dismissed','Dismiss','ghost']);}
  if(s==='resolving'){a.push(['open','Stop','ghost']);}
  if(s==='resolved'||s==='dismissed'){a.push(['open','Reopen','ghost']);}
  return a;
}
function _issueCard(it){
  const isCrash=(it.kind==='crash');
  const proj=it.project||'Unattributed';
  const sevHigh=(it.severity==='high');
  const openLink=isCrash?`<a class="backlink" style="margin-left:auto;font-size:12px" onclick="event.stopPropagation();openIssue('${esc(it.id)}')" title="Full crash context — guardian attempts, transcript, log slice">full context ›</a>`:'';
  const acts=_issueActions(it).map(([st,label,cls])=>
    st==='point'
      ? `<button class="btn ${cls} sm" onclick="issuePointLoop('${esc(it.id)}')">${label}</button>`
      : `<button class="btn ${cls} sm" onclick="issueTriage('${esc(it.id)}','${esc(st)}')">${esc(label)}</button>`
  ).join('');
  return `<div class="isscard">
    <div class="isshead">
      <span class="ikind k-${esc(it.kind||'bug')}" title="kind">${esc(it.kind||'bug')}</span>
      ${_issueStatusChip(it.status)}
      ${sevHigh?'<span class="ichip err" title="severity">high</span>':''}
      <span class="issttl">${esc(it.title||'(untitled issue)')}</span>
      ${openLink}
    </div>
    <div class="issmeta">
      <span class="chip" title="source">${_issueSourceLabel(it.source)}</span>
      ${!activeProject?`<span class="chip mono" title="project">▦ ${esc(proj)}</span>`:''}
      ${it.loop?`<span class="chip mono" title="filed by / about loop">${esc(it.loop)}</span>`:''}
      ${it.ts?`<span class="chip">${ago(it.ts)}</span>`:''}
    </div>
    <div class="issacts">${acts}</div>
  </div>`;
}
function renderIssues(){
  const el=document.getElementById('v-issues');
  if(!el)return;
  // Drill-in: one crash's full forensic context (guardian attempts + transcript + log).
  if(issueSel){el.innerHTML=_issueDetailHtml();return}
  const OPEN_WORK=new Set(['open','snoozed','resolving']);
  const scoped=(issues||[]).filter(issueInScope);
  const shown=issueViewAll?scoped:scoped.filter(x=>OPEN_WORK.has((x.status||'open').toLowerCase()));
  const openN=scoped.filter(x=>OPEN_WORK.has((x.status||'open').toLowerCase())).length;
  const cards=shown.length?shown.map(_issueCard).join('')
    :(loadErr.issues?emptyErr('issues unavailable',loadErr.issues)
      :`<div class="emptyline"><span class="kick">inbox clear</span>No ${issueViewAll?'':'open '}issues${activeProject?' for this project':''}. Loops file here while they work (a crash is just one <b>kind</b>); file your own with <b>＋ File an issue</b>, then <b>▶ Point a loop</b> to drain it.</div>`);
  const scopeName=activeProject?(activeProject===UNATTR?'Unattributed':((projectScopes().find(s=>s.id===activeProject)||{}).name||activeProject)):'';
  const fileForm=issueFileOpen?`<div class="issfileform">
      <input id="iss_file_title" class="hubin" placeholder="What's the fault? (one line)" spellcheck="false"
        onkeydown="if(event.key==='Enter')issueFileSubmit()" aria-label="Issue title">
      <select id="iss_file_kind" class="hubin" aria-label="Issue kind" style="max-width:150px">
        <option value="bug">bug</option><option value="follow-up">follow-up</option>
        <option value="blocked">blocked</option><option value="idea-overflow">idea-overflow</option>
        <option value="crash">crash</option></select>
      <button class="btn primary sm" onclick="issueFileSubmit()">File</button>
      <button class="btn ghost sm" onclick="issueFileOpen=false;renderIssues()">Cancel</button>
    </div>`:'';
  el.innerHTML=`<div class="page">
    <div class="pagehead"><h1 class="h1">Issues</h1>
      <span class="tag">${openN} open${issueViewAll&&shown.length!==openN?' · '+shown.length+' total':''}</span>
      ${scopeName?`<span class="tag mono" title="Scoped to the active project (§2)">▦ ${esc(scopeName)}</span>`:''}
      <span class="spacer"></span>
      <button class="btn ghost sm" onclick="issueViewAll=!issueViewAll;renderIssues()">${issueViewAll?'Open work':'Show all'}</button>
      <button class="btn primary sm" onclick="issueFileOpen=!issueFileOpen;renderIssues();var t=document.getElementById('iss_file_title');if(t)t.focus()">＋ File an issue</button>
    </div>
    <p class="pageintro">A project-scoped <b>inbox</b> of faults — filed by loops while they work and by you, drained by
      <b>pointing a loop</b> at one. A loop crash is just the <code>crash</code> kind. Triage inline: point a loop, snooze, resolve or dismiss.</p>
    ${fileForm}
    <div class="isslist">${cards}</div></div>`;
}
async function issueTriage(id,status){
  const r=await post('/api/loops/issues/triage',{id,status});
  if(r&&r.error){flash(r.error+(r.legal?' (legal: '+r.legal.join(', ')+')':''),true);return;}
  await loadIssues();if(view==='issues')renderIssues();
}
async function issueFileSubmit(){
  const t=document.getElementById('iss_file_title');const title=t?t.value.trim():'';
  if(!title){flash('give the issue a one-line title',true);return;}
  const kindSel=document.getElementById('iss_file_kind');const kind=kindSel?kindSel.value:'bug';
  // File into the ACTIVE project (§3 — project is required; unscoped/UNATTR lands
  // as the honest "Unattributed", never a fake bucket).
  const project=(activeProject&&activeProject!==UNATTR)?activeProject:'';
  const r=await post('/api/loops/issues/file',{title,kind,project,source:'you'});
  if(r&&r.error){flash(r.error,true);return;}
  issueFileOpen=false;
  await loadIssues();if(view==='issues')renderIssues();
  flash('Filed.');
}
// ▶ Point a loop at this issue (§3 unlock gesture). No dedicated issue→loop
// builder tool exists yet, so this is honestly born through the REAL composer:
// it seeds the goal + project chip from the issue and moves the issue to
// `resolving` (the model's "a loop is pointed at this" state — reversible via
// Reopen if you abandon the compose). A crash pre-seeds a recovery goal.
function issuePointLoop(id){
  const it=(issues||[]).find(x=>x&&x.id===id);if(!it){flash('issue not found',true);return;}
  navGo('newloop');
  const gi=document.getElementById('nlGoal');
  if(gi){const goal=(it.kind==='crash')
      ? ('Recover from: '+(it.title||it.loop||it.id))
      : ('Resolve issue: '+(it.title||it.id));
    gi.value=goal+((it.body&&(''+it.body).trim())?'\n\n'+(''+it.body).trim():'');}
  const ps=document.getElementById('nlProject');
  if(ps&&it.project&&it.project!=='Unattributed'){try{ps.value=it.project;}catch(e){}}
  nlValidConfig=null;try{nlSaveState(false);}catch(e){}
  post('/api/loops/issues/triage',{id:it.id,status:'resolving',note:'pointed a loop (composer)'})
    .then(()=>loadIssues());   // illegal from a terminal state → left as-is (honest no-op)
  flash('Composer seeded from the issue — ▶ Start to point the loop at it.');
}
function _issueDetailHtml(){
  const d=issueDetail;
  const back=`<div class="crumbrow"><a class="backlink" onclick="navGo('issues')">← Issues</a></div>`;
  if(!d)return `<div class="page">${back}<div class="emptyline"><span class="kick">loading…</span>Opening issue ${esc(issueSel)}.</div></div>`;
  const it=d.issue||{};
  const atts=(it.guardian_attempts||[]);
  const attHtml=atts.length?atts.map(a=>`<div class="attrow">
      <span class="ichip ${a.status==='give_up'?'gup':''}">${esc(a.status||'—')}</span>
      <span class="mono attag">${esc(a.agent||'—')}</span>
      <span class="attnote">${esc(a.note||'')}</span>
      <span class="mono attts">${a.ts?ago(a.ts):''}</span>
    </div>`).join(''):'<div class="hint">No guardian attempts recorded — the run errored outright.</div>';
  const tHtml=d.transcript_exists
    ? `<pre class="logbox">${esc(d.transcript_tail||'')}</pre>`
    : `<div class="hint">No transcript captured for the failing turn${it.transcript_path?' at '+esc(it.transcript_path):''}.</div>`;
  const logHtml=(it.log_slice&&it.log_slice.trim())
    ? `<pre class="logbox">${esc(it.log_slice)}</pre>`
    : '<div class="hint">No engine-log slice recorded.</div>';
  const errHtml=it.error?`<div class="logh">error</div><pre class="logbox err">${esc(it.error)}</pre>`:'';
  return `<div class="page">${back}
    <div class="pagehead"><h1 class="h1">${esc(it.title||it.loop||'issue')}</h1>${_issueStatusChip(it.status)}
      ${it.ended?`<span class="ichip err" title="engine end-state">${esc(it.ended)}</span>`:''}
      <span class="spacer"></span><span class="tag mono">${esc(it.id||issueSel)}</span></div>
    <div class="imeta">
      <span class="chip">agent <b>${esc(it.failing_agent||'—')}</b></span>
      <span class="chip">phase <b>${esc(it.failing_phase||'—')}</b></span>
      <span class="chip">status <b>${esc(it.failing_status||'—')}</b></span>
      ${it.ts?`<span class="chip">${ago(it.ts)}</span>`:''}
      ${it.run?`<span class="chip mono" title="run key">${esc(''+it.run)}</span>`:''}
    </div>
    ${errHtml}
    <div class="logh">guardian recovery attempts <span class="cnt">${atts.length}</span></div>
    <div class="attlist">${attHtml}</div>
    <div class="logh">failing turn transcript ${d.transcript_exists?'':'<span class="cnt">none</span>'}</div>
    ${tHtml}
    <div class="logh">engine log slice</div>
    ${logHtml}</div>`;
}
// ── THE IDEA HUB — targets (a): a two-pane living-document workspace ──────────
// REDESIGN-SPEC §3 (rd-ideahub) + §6: NOT a queue of strings — a per-project
// workspace where a note thickens into a proposal and, the moment a loop is
// pointed at it, that same doc BECOMES an objective. Left rail = an index of docs
// (derived note→proposal→objective glyph + ●LOOP badge); right pane = a real
// editor. The core gesture — write, then ▶ Point a loop — calls dev-1's
// loop_doc_build_loop (build → promote → invite-rewrite in one call). When the
// loop finishes its Resolution folds back as a RESULT ribbon (never a green lie).
async function loadHub(){
  try{const r=await j('/api/loops/docs');hubDocs=(r&&r.docs)||[];loadErr.hub=(r&&r.error)||''}
  catch(e){hubDocs=[];loadErr.hub=''+e}
}
function _docRailRow(d){
  const on=(hubDocSel===d.id)?' on':'';
  const badge=(d.loop_count>0)?`<span class="dloop" title="${d.loop_count} loop${d.loop_count===1?'':'s'} pointed here">●LOOP</span>`:'';
  const res=d.has_result?'<span class="dres" title="a loop result folded back">✓</span>':'';
  return `<div class="docrow${on}" onclick="hubOpenDoc('${esc(d.id)}')" title="${esc(d.state||'note')}">
    <span class="dglyph" aria-hidden="true">${esc(d.glyph||'·')}</span>
    <span class="dttl">${esc(d.title||'(untitled)')}</span>
    ${badge}${res}
  </div>`;
}
function _docResultRibbon(rez){
  if(!rez)return '';
  // §rd-results / §2.5 — render green ONLY when the computed verdict says so; a
  // crash/abandon folds a non-green ribbon that can never read as success.
  const green=!!rez.green;
  const commit=rez.commit?`<span class="chip mono" title="commit">${esc((''+rez.commit).slice(0,12))}</span>`:'';
  return `<div class="docresult ${green?'green':'nongreen'}">
    <span class="drverdict">${green?'⬤':'○'} ${esc(rez.verdict||'RESULT')}</span>
    <span class="drline">${esc(rez.resolution||'')}</span>
    ${rez.loop?`<span class="chip mono">${esc(rez.loop)}</span>`:''}${commit}
  </div>`;
}
function _docEditorHtml(){
  const d=hubDocDetail;
  if(!d||!d.doc){return `<div class="docpane"><div class="emptyline"><span class="kick">loading…</span>Opening the doc.</div></div>`;}
  const doc=d.doc;
  const state=doc.state||'note';
  const loops=(doc.loops||[]);
  const canRewrite=!!doc.loop_rewrite;
  const ready=!!doc.ready;
  const loopChips=loops.length?loops.map(n=>`<span class="chip mono clk" onclick="jumpToLoop('${esc(n)}')" title="open loop">⟲ ${esc(n)}</span>`).join(''):'';
  // §rd-ideahub — a loop's rewrite lands inline with an AGENT BADGE (distinct from
  // a human 'you' edit); the per-doc timeline reads honestly who touched what.
  const hist=(doc.history||[]).slice(-6).reverse().map(h=>{
    const actor=h.actor||'you';
    const byLoop=(actor!=='you')||!!h.loop;
    const badge=byLoop?`<span class="dhbadge loop" title="rewritten by a loop agent">⟲ ${esc(h.loop||actor)}</span>`
      :`<span class="dhbadge you" title="your edit">✎ you</span>`;
    return `<div class="dhrow">${badge}
      <span class="stone">${(h.changed||[]).join(', ')||'edit'}</span>
      <span class="hubago">${h.ts?ago(h.ts):''}</span></div>`;}).join('');
  return `<div class="docpane">
    <div class="docpanehead">
      <span class="dglyph big" title="derived state">${esc(doc.glyph||'·')}</span>
      <input id="hub_doc_title" class="doctitle" value="${esc(doc.title||'')}" spellcheck="false"
        oninput="hubDocDirty=true" aria-label="Doc title">
      <span class="tag">${esc(state)}</span>
      ${loops.length?`<span class="dloop" title="${loops.length} loop${loops.length===1?'':'s'} pointed here">●LOOP</span>`:''}
    </div>
    ${_docResultRibbon(doc.result)}
    <textarea id="hub_doc_body" class="docbody" spellcheck="false" placeholder="Write the note… it thickens into a proposal, and becomes an objective the moment you point a loop at it."
      oninput="hubDocDirty=true" aria-label="Doc body">${esc(doc.body||'')}</textarea>
    <div class="docbar">
      <button class="btn primary sm" onclick="hubSaveDoc()">Save</button>
      <button class="btn primary sm" onclick="hubPointLoop('${esc(doc.id)}')" title="Build a loop from this doc, pointed at it — promotes it to an objective (§rd-ideahub core gesture)">▶ Point a loop</button>
      <label class="dtoggle" title="A considered design a loop could take"><input type="checkbox" ${ready?'checked':''} onchange="hubToggleFlag('ready',this.checked)"> ready</label>
      <label class="dtoggle" title="Read-only canvas vs loop-may-rewrite (§2.4)"><input type="checkbox" ${canRewrite?'checked':''} onchange="hubToggleFlag('loop_rewrite',this.checked)"> loop may rewrite</label>
      <span class="spacer"></span>
      <span class="stone mono">${esc(doc.id)}</span>
    </div>
    ${loopChips?`<div class="dsub">pointed loops</div><div class="dchips">${loopChips}</div>`:''}
    ${hist?`<div class="dsub">timeline</div><div class="dhist">${hist}</div>`:''}
    ${_docThreadHtml()}
  </div>`;
}
// ── §8 (Q2) — the OBJECTIVE-MANAGER THREAD, docked to the Hub doc right pane ──
// A durable, doc-anchored conversation backed by an ATTACHED SESSION (Q2: your
// compute, your keys). It reads the live doc, and — from the one conversation —
// rewrites it in place (landing inline with the ⟲ agent badge + timeline above),
// files an issue, and points a loop. Honesty is unfakeable: an agent turn is a
// PENDING placeholder until the backing session actually returns; with no live
// session it shows the honest unattached/offline state and NEVER a canned reply.
const _THREAD_STATE={unattached:{cls:'st-unatt',label:'unattached'},offline:{cls:'st-off',label:'offline'},
  awaiting:{cls:'st-await',label:'working…'},ready:{cls:'st-ready',label:'ready'}};
function _threadActionChip(a){
  // an action the session REPORTED it took this turn (rewrote the doc / filed an
  // issue / pointed a loop) — folded from its real returned envelope, never faked.
  const t=(a&&(a.type||a.kind||a.action))||'';const label=(a&&(a.label||a.summary||a.text))||t||'action';
  const ic=t.indexOf('doc')>=0?'✎':(t.indexOf('issue')>=0?'⚠':(t.indexOf('loop')>=0?'⟲':'•'));
  return `<span class="thact" title="${esc(t||'action')}">${ic} ${esc(label)}</span>`;}
function _threadMsgHtml(m){
  if(!m)return'';
  if(m.role==='you')return `<div class="thmsg you"><span class="thwho">you</span><span class="thtext">${esc(m.text||'')}</span>${m.dispatched===false?'<span class="thundis" title="kept but not dispatched — attach a live session to run it">held</span>':''}</div>`;
  // agent turn: pending (real task in flight, no text yet) vs a folded real reply.
  const pending=(m.status==='pending');
  const failed=(m.status==='failed');
  const acts=(m.actions||[]).map(_threadActionChip).join('');
  return `<div class="thmsg agent${failed?' failed':''}"><span class="thwho">◈ objective-manager</span>
    ${pending?'<span class="thpending">working… <span class="thdots"><i></i><i></i><i></i></span></span>'
      :`<span class="thtext">${esc(m.text||(failed?'(the session returned a failure)':''))}</span>`}
    ${acts?`<div class="thacts">${acts}</div>`:''}</div>`;}
function _threadSessionOptions(cur){
  // Live app sessions are the honest backing choices (they execute dispatched work).
  const live=(sessionsRoster||[]).filter(s=>s&&s.heartbeat&&s.heartbeat.live);
  const opts=live.map(s=>`<option value="${esc(s.id)}" ${cur===s.id?'selected':''}>${esc(s.id)} · ${esc(s.runtime||'claude')}</option>`).join('');
  return `<option value="">— attach a session —</option>${opts}`;}
function _docThreadHtml(){
  const d=hubDocDetail&&hubDocDetail.doc;if(!d)return'';
  const th=hubThread;
  const st=(th&&th.session_state)||{state:'unattached',can_send:false,reason:'Loading the objective-manager thread…'};
  const meta=_THREAD_STATE[st.state]||_THREAD_STATE.unattached;
  const msgs=((th&&th.thread&&th.thread.messages)||[]);
  const session=st.session||'';
  const canSend=!!st.can_send;
  const body=msgs.length?msgs.map(_threadMsgHtml).join('')
    :`<div class="thempty">No messages yet. ${session?'Send one to steer this objective — the session reads the doc and can rewrite it, file an issue, or point a loop.':'Attach a live session, then steer this objective in conversation.'}</div>`;
  const attaching=hubThreadAttaching;
  // the "running on <session> ·" byline + switcher (a live-session picker).
  const byline=`<div class="thbyline">
    <span class="thstate ${meta.cls}" title="${esc(st.reason||'')}">● ${meta.label}</span>
    ${session?`<span class="thon">running on <b class="mono">${esc(session)}</b></span>`:'<span class="thon stone">no session attached</span>'}
    <span class="spacer"></span>
    <select id="hub_thread_sess" class="thsel" ${attaching?'disabled':''} aria-label="Backing session">${_threadSessionOptions(session)}</select>
    <button class="btn ghost sm" ${attaching?'disabled':''} onclick="hubThreadAttach('${esc(d.id)}')" title="Back this thread with the selected live session (Q2: your compute)">${session?'Switch':'Attach'}</button>
    ${session?`<button class="btn ghost sm" ${attaching?'disabled':''} onclick="hubThreadDetach('${esc(d.id)}')" title="Detach — back to the honest unattached state">Detach</button>`:''}
  </div>`;
  const composer=`<div class="thcompose">
    <textarea id="hub_thread_input" class="thinput" rows="2" ${canSend?'':'disabled'}
      placeholder="${canSend?'Message the objective-manager — steer this objective…':(st.reason||'Attach a live session to steer this objective.')}"></textarea>
    <div class="thbar">
      <button class="btn primary sm" ${canSend?'':'disabled'} onclick="hubThreadSend('${esc(d.id)}')">Send</button>
      <span class="thhint stone">${canSend?'The backing session reads the doc and may rewrite it, file an issue, or point a loop — its reply folds back here.':esc(st.reason||'')}</span>
    </div></div>`;
  return `<div class="docthread" aria-label="Objective-manager thread">
    <div class="dsub thsub">◈ objective-manager <span class="thhelp stone">— a durable thread docked to this doc, backed by a session</span></div>
    ${byline}
    <div class="thmsgs">${body}</div>
    ${composer}
  </div>`;
}
function renderHub(){
  const el=document.getElementById('v-hub');
  if(!el)return;
  // Preserve in-progress editing across a re-render (30s tick / action refresh).
  const prevBody=(document.getElementById('hub_doc_body')||{}).value;
  const prevTitle=(document.getElementById('hub_doc_title')||{}).value;
  const prevThreadIn=(document.getElementById('hub_thread_input')||{}).value;
  const docs=(hubDocs||[]).filter(docInScope);
  const scopeName=activeProject?(activeProject===UNATTR?'Unattributed':((projectScopes().find(s=>s.id===activeProject)||{}).name||activeProject)):'';
  const rail=docs.length?docs.map(_docRailRow).join('')
    :`<div class="hubempty">No docs${activeProject?' for this project':''} yet — <b>＋ New doc</b> and start typing.</div>`;
  const errLine=loadErr.hub?`<div class="hint" style="color:var(--ember-soft)">hub read error — ${esc(loadErr.hub)}</div>`:'';
  const editor=(hubDocSel&&hubDocDetail)?_docEditorHtml()
    :`<div class="docpane"><div class="emptyline"><span class="kick">the workspace</span>Pick a doc on the left, or <b>＋ New doc</b> to capture a thought. Write it, then <b>▶ Point a loop</b> — that gesture promotes it to an objective and lets the loop rewrite it in place.</div></div>`;
  el.innerHTML=`<div class="page hubpage">
    <div class="pagehead"><h1 class="h1">Idea Hub</h1><span class="tag">${docs.length} doc${docs.length===1?'':'s'}</span>
      ${scopeName?`<span class="tag mono" title="Scoped to the active project (§2)">▦ ${esc(scopeName)}</span>`:''}</div>
    <p class="pageintro">Living documents, per project. A rough <b>note</b> thickens into a <b>proposal</b>; the moment you
      <b>point a loop</b> at one it becomes an <b>objective</b> — and the loop reads and rewrites it in place, folding its result back when it finishes.</p>
    ${errLine}
    <div class="hubtwo">
      <aside class="hubrail">
        <button class="btn primary sm full" onclick="hubNewDoc()">＋ New doc</button>
        <div class="docindex">${rail}</div>
      </aside>
      <div class="hubwork">${editor}</div>
    </div>
  </div>`;
  // Restore in-flight edits (same doc still open) so a background tick never clobbers typing.
  if(hubDocSel&&hubDocDetail){
    const bb=document.getElementById('hub_doc_body');if(bb&&prevBody!=null&&hubDocDirty)bb.value=prevBody;
    const tt=document.getElementById('hub_doc_title');if(tt&&prevTitle!=null&&prevTitle&&hubDocDirty)tt.value=prevTitle;
    // keep any in-progress thread message across a poll re-render.
    const ti=document.getElementById('hub_thread_input');if(ti&&prevThreadIn)ti.value=prevThreadIn;
  }
}
async function hubNewDoc(){
  // Zero-friction capture (§2.2): create into the active project, open it, focus body.
  const project=(activeProject&&activeProject!==UNATTR)?activeProject:'';
  const r=await post('/api/loops/docs/create',{project,title:'Untitled',body:''});
  if(r&&r.error){flash(r.error,true);return;}
  const doc=r&&r.doc;if(!doc){flash('could not create doc',true);return;}
  await loadHub();
  hubDocSel=doc.id;hubDocDetail={doc};hubDocDirty=false;hubThread=null;hubThreadAttaching=false;
  if(view==='hub')renderHub();
  const t=document.getElementById('hub_doc_title');if(t){t.focus();t.select&&t.select();}
  loadThread(doc.id).then(()=>{if(view==='hub'&&hubDocSel===doc.id)renderHub();});
}
async function hubOpenDoc(id){
  hubDocSel=id;hubDocDirty=false;hubThread=null;hubThreadAttaching=false;
  // paint the shell, then fetch the full record (body/result/history).
  hubDocDetail=null;if(view==='hub')renderHub();
  try{const r=await j('/api/loops/doc/'+encodeURIComponent(id));
    if(r&&r.error){flash(r.error,true);hubDocSel=null;if(view==='hub')renderHub();return;}
    hubDocDetail=r;}
  catch(e){flash('doc unavailable — '+e,true);hubDocSel=null;if(view==='hub')renderHub();return;}
  if(view==='hub')renderHub();
  // §8 — load the docked objective-manager thread (+ ensure the session roster is
  // available so the switcher offers real live sessions), then repaint.
  if(!sessionsLoaded)loadSessions();
  await loadThread(id);
  if(view==='hub'&&hubDocSel===id){renderHub();if(_threadPending())_pollThreadSoon(id);}
}
async function hubSaveDoc(){
  if(!hubDocSel)return;
  const title=(document.getElementById('hub_doc_title')||{}).value||'';
  const body=(document.getElementById('hub_doc_body')||{}).value||'';
  const r=await post('/api/loops/docs/update',{id:hubDocSel,title,body});
  if(r&&r.error){flash(r.error,true);return;}
  hubDocDirty=false;
  hubDocDetail=r&&r.doc?r:hubDocDetail;
  await loadHub();if(view==='hub')renderHub();
  flash('Saved.');
}
async function hubToggleFlag(flag,on){
  if(!hubDocSel)return;
  const payload={id:hubDocSel};payload[flag]=!!on;
  const r=await post('/api/loops/docs/update',payload);
  if(r&&r.error){flash(r.error,true);return;}
  hubDocDetail=r&&r.doc?r:hubDocDetail;
  await loadHub();if(view==='hub')renderHub();
}
// THE CORE GESTURE — ▶ point a loop at this doc. Build → promote → invite-rewrite
// in one real call (dev-1's loop_doc_build_loop): the doc flips to an objective
// (◔ + ●LOOP), a real loop is born pointed at it, and the loop is invited to
// rewrite the body. We save any pending edits first so the loop is built from the
// text you see. Then hand off to the loop detail to Start it (§7 lifecycle).
async function hubPointLoop(id){
  if(!id)return;
  if(hubDocDirty){await hubSaveDoc();}
  const r=await post('/api/loops/docs/point',{id});
  if(r&&r.error){flash(r.error,true);return;}
  if(!(r&&r.ok&&r.loop)){flash('could not build a loop from this doc',true);return;}
  await loadHub();
  // refresh the open doc so the ●LOOP badge + objective glyph + pointed-loop chip show
  try{const g=await j('/api/loops/doc/'+encodeURIComponent(id));if(!g.error)hubDocDetail=g;}catch(e){}
  if(view==='hub')renderHub();
  flash('Promoted to objective — built loop "'+r.loop+'" pointed at this doc. Opening it to Start…');
  jumpToLoop(r.loop);   // hand off to the proven loop detail; Start lives there (§7)
}

// ── §8 (Q2): the objective-manager THREAD — load / attach / send / fold ──
// Loaded when a doc opens; the reply folds back from the backing session's REAL
// returned envelope (dev-1's loop_thread_get folds terminal tasks), so a light
// poll while a turn is pending is all the client does — it never invents a reply.
async function loadThread(id){
  if(!id){hubThread=null;return;}
  try{const r=await j('/api/loops/thread/'+encodeURIComponent(id));
    hubThread=(r&&!r.error)?r:null;}
  catch(e){hubThread=null;}
}
function _threadPending(){
  const ms=(hubThread&&hubThread.thread&&hubThread.thread.messages)||[];
  return ms.some(m=>m&&m.role==='agent'&&m.status==='pending');
}
let _threadPollT=null;
function _pollThreadSoon(id){
  // fold a pending agent turn when its backing session returns — poll a few times,
  // stop as soon as nothing is pending (honest: we only re-read, never fabricate).
  if(_threadPollT){clearTimeout(_threadPollT);_threadPollT=null;}
  let n=0;const tick=async()=>{
    if(view!=='hub'||hubDocSel!==id){return;}
    const wasPending=_threadPending();
    await loadThread(id);
    // a turn just folded (pending→done): the session may have rewritten the doc,
    // filed an issue or pointed a loop — refresh the doc so an inline edit + the
    // ⟲ agent-badge timeline show, and the rail glyph/badge update.
    if(wasPending&&!_threadPending()){
      try{const r=await j('/api/loops/doc/'+encodeURIComponent(id));if(r&&!r.error&&hubDocSel===id){hubDocDetail=r;}}catch(e){}
      loadHub();loadIssues();
    }
    if(view==='hub'&&hubDocSel===id)renderHub();
    if(_threadPending()&&n++<20){_threadPollT=setTimeout(tick,3000);}
  };
  _threadPollT=setTimeout(tick,2500);
}
async function hubThreadAttach(id){
  const sel=document.getElementById('hub_thread_sess');
  const session=sel?(sel.value||''):'';
  if(!session){flash('pick a live session to attach',true);return;}
  hubThreadAttaching=true;if(view==='hub')renderHub();
  const r=await post('/api/loops/thread/attach',{id,session});
  hubThreadAttaching=false;
  if(r&&r.error){flash(r.error,true);if(view==='hub')renderHub();return;}
  hubThread=r&&!r.error?r:hubThread;
  if(view==='hub')renderHub();
  const stt=(r&&r.session_state&&r.session_state.state)||'';
  flash(stt==='offline'?('Attached '+session+' — but it reads offline; it can run once it reconnects.'):('Attached — thread now runs on '+session+'.'));
}
async function hubThreadDetach(id){
  hubThreadAttaching=true;if(view==='hub')renderHub();
  const r=await post('/api/loops/thread/attach',{id,session:''});
  hubThreadAttaching=false;
  if(r&&r.error){flash(r.error,true);if(view==='hub')renderHub();return;}
  hubThread=r&&!r.error?r:hubThread;
  if(view==='hub')renderHub();
  flash('Detached — back to the honest unattached state (no faked replies).');
}
async function hubThreadSend(id){
  const t=document.getElementById('hub_thread_input');const text=t?(t.value||'').trim():'';
  if(!text){flash('type a message to steer the objective',true);return;}
  const r=await post('/api/loops/thread/post',{id,text});
  if(r&&r.error){flash(r.error,true);return;}
  hubThread=r&&!r.error?r:hubThread;
  if(view==='hub')renderHub();
  if(r&&r.dispatched){
    flash('Sent — '+((hubThread&&hubThread.session_state&&hubThread.session_state.session)||'the session')+' is working; its reply folds back here.');
    _pollThreadSoon(id);
  }else{
    // no live session: the words are kept honestly, nothing faked.
    flash('Kept your message — attach a live session to actually run it.',true);
  }
}

// ══ §7 — compute felt ambiently: the Fleet pill, the Sessions roster, Overview ══
// REDESIGN-SPEC §2/§3 (rd-origins / rd-sessions). All three read dev-1's fresh
// read-models (loop_fleet_summary / loop_sessions_list) and render REAL data or an
// honest empty — never a faked row or an inflated count.

// ── The Fleet pill (top bar, every screen) ─────────────────────────────────────
let _fleetDropSeen=new Set();           // origins we've already toasted as dropped
async function loadFleet(){
  try{const r=await j('/api/loops/fleet');fleetSummary=(r&&!r.error)?r:(r||null);loadErr.fleet=(r&&r.error)||''}
  catch(e){fleetSummary=null;loadErr.fleet=''+e}
}
function renderFleetPill(){
  const pill=document.getElementById('fleetPill');if(!pill)return;
  const f=fleetSummary;
  const dotsEl=document.getElementById('fleetDots'),txtEl=document.getElementById('fleetTxt');
  if(!f){if(dotsEl)dotsEl.textContent='';if(txtEl)txtEl.textContent='Fleet';pill.classList.remove('drop');return;}
  // Honest dots: one glyph per origin — ● reachable, ○ unreachable (amber when it
  // still owns a running loop — the drop signal). Cap the glyph run so a big fleet
  // never overflows the bar; the count text stays exact.
  const items=(f.items||[]).slice(0,8);
  const dots=items.map(i=>{
    const cls=i.dot==='amber'?'fd amber':(i.reachable?'fd on':'fd off');
    return `<span class="${cls}">${i.reachable?'●':'○'}</span>`;
  }).join('')+((f.items||[]).length>8?'<span class="fd more">…</span>':'');
  if(dotsEl)dotsEl.innerHTML=dots;
  if(txtEl)txtEl.textContent='Fleet · '+(f.label||((f.origins||0)+' · '+(f.reachable||0)+' reachable'));
  const drop=(f.dropCount||0)>0;
  pill.classList.toggle('drop',drop);
  // Quiet toast the FIRST time an origin running a loop drops (rd-origins §2).
  (f.dropped||[]).forEach(d=>{const k=d.id||d.name;if(k&&!_fleetDropSeen.has(k)){_fleetDropSeen.add(k);
    flash('◉ '+(d.name||k)+' dropped while running '+d.loops+' loop'+(d.loops===1?'':'s')+' — check the fleet',true);}});
  if(!_fleetPopHidden())renderFleetPop();
}
function _fleetPopHidden(){const p=document.getElementById('fleetPop');return !p||p.classList.contains('hidden');}
function renderFleetPop(){
  const p=document.getElementById('fleetPop');if(!p)return;
  const f=fleetSummary;
  if(!f){p.innerHTML='<div class="fprow stone">fleet unavailable'+(loadErr.fleet?' — '+esc(loadErr.fleet):'')+'</div>';return;}
  const rows=(f.items||[]).map(i=>{
    const cls=i.dot==='amber'?'fd amber':(i.reachable?'fd on':'fd off');
    const meta=[i.reachable?'reachable':'unreachable',i.health?esc(i.health):'',i.loops?(i.loops+' loop'+(i.loops===1?'':'s')):''].filter(Boolean).join(' · ');
    return `<div class="fprow"><span class="${cls}">${i.reachable?'●':'○'}</span>
      <span class="fpn">${esc(i.name||i.id)}</span><span class="fpm stone">${meta}</span></div>`;
  }).join('')||'<div class="fprow stone">no origins yet</div>';
  const dropLine=(f.dropCount||0)>0?`<div class="fpdrop">⚠ ${f.dropCount} origin${f.dropCount===1?'':'s'} dropped while running a loop</div>`:'';
  p.innerHTML=`<div class="fphead">${esc(f.label||'')}</div>${dropLine}${rows}
    <div class="fpfoot"><a class="ilink" onclick="navGo('origins');toggleFleetPop()">Open Origins →</a></div>`;
}
function toggleFleetPop(e){if(e&&e.stopPropagation)e.stopPropagation();
  const p=document.getElementById('fleetPop'),pill=document.getElementById('fleetPill');if(!p)return;
  const willShow=p.classList.contains('hidden');
  p.classList.toggle('hidden',!willShow);
  if(pill)pill.setAttribute('aria-expanded',willShow?'true':'false');
  if(willShow)renderFleetPop();
}
// close the popover on an outside click (once wired at boot)
document.addEventListener('click',(e)=>{const p=document.getElementById('fleetPop');
  if(!p||p.classList.contains('hidden'))return;
  if(e.target.closest('#fleetPop')||e.target.closest('#fleetPill'))return;
  p.classList.add('hidden');const pill=document.getElementById('fleetPill');if(pill)pill.setAttribute('aria-expanded','false');});

// ── The Sessions roster (rd-sessions §3/§7) — living collaborators, one card each.
async function loadSessions(){
  try{const r=await j('/api/loops/sessions');sessionsRoster=(r&&r.sessions)||[];sessionsLive=(r&&r.live)||0;loadErr.sessions=(r&&r.error)||''}
  catch(e){sessionsRoster=[];sessionsLive=0;loadErr.sessions=''+e}
  finally{sessionsLoaded=true;}
}
// §rd-sessions capability-honest switch verbs. An interactive/terminal session on
// the LOCAL box is a CLI/tmux session we can OPEN (attach a web terminal to its
// live tmux — honest: tmux itself reports if it isn't there). Everything else is
// an app session we FOLLOW (read-only) + SEND (a real dispatched task) — never a
// faked takeover. "Start a loop from here" is offered for both (composer pre-filled
// with the session as compute + its runtime).
function _sessKind(s){
  const caps=(s.capabilities||[]).map(c=>(''+c).toLowerCase());
  const interactive=caps.includes('interactive')||caps.includes('terminal')||caps.includes('tmux');
  const o=s.origin||{};const host=(o.host||'').toLowerCase();
  const local=!host||host==='local'||host==='localhost';
  return (interactive&&local)?'cli':'app';
}
function _sessCard(s){
  const hb=s.heartbeat||{};
  const live=!!hb.live;
  const cap=(s.capabilities||[]).map(c=>`<span class="pill2">${esc(c)}</span>`).join('');
  const o=s.origin||null;
  const originLine=o?`<span class="mono">${esc(o.host||'—')}</span>${o.cwd?`<span class="stone"> · ${esc(o.cwd)}</span>`:''}`:'<span class="stone">— origin not reported</span>';
  const cr=s.created||{};
  const madeParts=[cr.loops?cr.loops+' loop'+(cr.loops===1?'':'s'):'',cr.issues?cr.issues+' issue'+(cr.issues===1?'':'s'):'',cr.objectives?cr.objectives+' objective'+(cr.objectives===1?'':'s'):''].filter(Boolean);
  const made=madeParts.length?madeParts.join(' · '):'nothing yet';
  const idle=hb.idleSeconds!=null?ago(Date.now()/1000-hb.idleSeconds):'';
  const kind=_sessKind(s);
  const jid=esc(s.id);
  // capability-honest verbs (§rd-sessions): only offer what this session really supports.
  let verbs='';
  if(kind==='cli'){
    verbs=`<button class="btn ghost sm" onclick="openSessionTerminal('${jid}')" title="Attach a web terminal to this session's live tmux (local origin)"><span class="parr">❯</span> Open</button>`;
  }else{
    const followOn=!!sessFollowOpen[s.id];
    verbs=`<button class="btn ghost sm${followOn?' on':''}" onclick="sessToggleFollow('${jid}')" title="Watch this session's live activity — read-only, no takeover">${followOn?'▾ Following':'Follow'}</button>`
      +`<button class="btn ghost sm" ${live?'':'disabled'} onclick="sessToggleSend('${jid}')" title="${live?'Dispatch a real task to this session (it runs it on its own compute)':'Session offline — cannot dispatch until it reconnects'}">Send</button>`;
  }
  verbs+=`<button class="btn primary sm" onclick="startLoopFromSession('${jid}','${esc(s.runtime||'claude')}')" title="Open the composer with this session as compute, its runtime pre-filled"><span class="parr">＋</span> Start a loop from here</button>`;
  // Follow (read-only) — the session's real activity, from already-loaded roster data.
  const act=s.activity||{};
  const followPanel=(kind==='app'&&sessFollowOpen[s.id])?`<div class="sessfollow">
      <span class="sk">activity</span>
      <span class="chip">${act.claimed||0} claimed</span>
      <span class="chip ok">${act.returned||0} returned</span>
      <span class="chip ${act.failed?'err':''}">${act.failed||0} failed</span>
      <span class="stone">read-only — no takeover</span></div>`:'';
  // Send — an inline compose box that dispatches a REAL task (honest, no faked reply).
  const sendPanel=(kind==='app'&&sessSendOpen[s.id])?`<div class="sesssend">
      <textarea id="sess_send_${esc(s.id)}" class="sinput" rows="2" placeholder="A task to dispatch to ${esc(s.id)} — it claims and runs this on its own compute."></textarea>
      <div class="sesssendbar">
        <button class="btn primary sm" onclick="sessDoSend('${jid}')">Dispatch</button>
        <button class="btn ghost sm" onclick="sessToggleSend('${jid}')">Cancel</button>
        <span class="stone">no fake takeover — the session picks it up when it next polls</span>
      </div></div>`:'';
  return `<div class="sesscard ${live?'live':'stale'}">
    <div class="sesshd"><span class="sdot ${live?'on':'off'}"></span>
      <span class="sname mono">${esc(s.id)}</span>
      <span class="pill2 rt">${esc(s.runtime||'claude')}</span>
      <span class="pill2" title="capability-honest session kind">${kind==='cli'?'cli/tmux':'app'}</span>
      <span class="spacer"></span>
      <span class="shb ${live?'live':'stale'}" title="honest heartbeat">${live?'live':'stale'}${idle?' · '+idle:''}</span></div>
    ${cap?`<div class="sesscaps">${cap}</div>`:''}
    <div class="sessrow"><span class="sk">runs on</span> ${originLine}</div>
    <div class="sessrow"><span class="sk">doing now</span> ${s.doingNow?esc(s.doingNow):'<span class="stone">idle — no claimed task</span>'}</div>
    <div class="sessrow"><span class="sk">created</span> <span class="${madeParts.length?'':'stone'}">${esc(made)}</span></div>
    ${followPanel}${sendPanel}
    <div class="sessverbs">${verbs}</div>
  </div>`;
}
function sessToggleFollow(id){sessFollowOpen[id]=!sessFollowOpen[id];if(view==='sessions')renderSessions();}
function sessToggleSend(id){sessSendOpen[id]=!sessSendOpen[id];if(view==='sessions')renderSessions();
  if(sessSendOpen[id]){setTimeout(()=>{const t=document.getElementById('sess_send_'+id);if(t)t.focus();},0);}}
async function sessDoSend(id){
  const t=document.getElementById('sess_send_'+id);const text=t?(t.value||'').trim():'';
  if(!text){flash('type a task to dispatch',true);return;}
  const r=await post('/api/loops/session/dispatch',{session:id,text});
  if(r&&r.error){flash(r.error,true);return;}
  if(!(r&&(r.ok||r.task_id))){flash('could not dispatch to '+id,true);return;}
  sessSendOpen[id]=false;
  flash('Dispatched task '+((r.task_id||'').slice(0,8))+' to '+id+' — it runs on its own compute.');
  await loadSessions();if(view==='sessions')renderSessions();
}
// "Start a loop from here" — the composer, pre-filled with this session as compute
// (origin = session:<id>) and its runtime. Honest: the session must be a live,
// reachable option in the shared picker for the origin to stick.
function startLoopFromSession(id,runtime){
  nlSeedOrigin='session:'+id;nlSeedRuntime=runtime||'claude';
  navGo('newloop');
  flash('Composer opened — compute pre-filled to session '+id+'. Describe the goal, then Start.');
}
function renderSessions(){
  const el=document.getElementById('v-sessions');if(!el)return;
  const list=sessionsRoster||[];
  const errLine=loadErr.sessions?`<div class="hint" style="color:var(--ember-soft)">sessions read error — ${esc(loadErr.sessions)}</div>`:'';
  const body=list.length?`<div class="sessgrid">${list.map(_sessCard).join('')}</div>`
    :(!sessionsLoaded?`<div class="empty"><div class="emptyline"><span class="kick">loading…</span>Reading the roster from your compute.</div></div>`
    :`<div class="empty"><div class="emptyline"><span class="kick">no sessions yet</span>
        Connect an app or CLI session — it appears here as a living collaborator that can create loops and steer objectives, each on a real origin.</div></div>`);
  el.innerHTML=`<div class="page">
    <div class="pagehead"><h1 class="h1">Sessions</h1><span class="tag">${list.length} session${list.length===1?'':'s'}${sessionsLive?' · '+sessionsLive+' live':''}</span></div>
    <p class="pageintro">The <b>living collaborators</b> — connected apps and terminals that execute dispatched work on <b>your own compute</b>. Each card is honest: identity, runtime, which origin it runs on (host · cwd), its real heartbeat, what it's doing now, and what it has created. And each card <b>acts</b>, capability-honest: <b>Open</b> a live terminal on a CLI session, <b>Follow + Send</b> to an app session, or <b>start a loop</b> from any of them — a real dispatch to its own compute, never a faked takeover.</p>
    ${errLine}${body}</div>`;
}

// ── Overview (rd-projects §3) — this project at a glance. Pure re-composition over
// already-loaded state (loops/hub/issues/origins), so it never adds a fetch and can
// never contradict the surfaces it summarizes (§2 honesty).
function renderOverview(){
  const el=document.getElementById('v-overview');if(!el)return;
  const scopeName=activeProject?(activeProject===UNATTR?'Unattributed':((projectScopes().find(s=>s.id===activeProject)||{}).name||activeProject)):'All projects';
  const vis=visibleLoops();
  const running=vis.filter(d=>RUNNING.has(d.state));
  const docs=(hubDocs||[]).filter(docInScope);
  const objectives=docs.filter(d=>(d.loop_count||0)>0||d.state==='objective');
  const iss=(activeProject?issues.filter(x=>x&&issueInScope(x)):issues);
  const openIss=iss.filter(x=>{const st=(x&&x.status)||'open';return st!=='resolved'&&st!=='dismissed';});
  // latest honest results: the most recently updated resolved/ended loops in scope.
  const ended=vis.filter(d=>!RUNNING.has(d.state)&&d.result).slice(0,4);
  const tile=(label,n,go,sub)=>`<div class="ovtile" onclick="navGo('${go}')" role="link" tabindex="0" title="Open ${esc(label)}">
      <div class="ovn">${n}</div><div class="ovl">${esc(label)}</div>${sub?`<div class="ovsub stone">${esc(sub)}</div>`:''}</div>`;
  const runList=running.length?`<div class="ovloops">${running.slice(0,6).map(d=>`<span class="boundloop" onclick="event.stopPropagation();jumpToLoop('${esc(d.name)}')">⟲ ${esc(d.name)}</span>`).join('')}</div>`
    :'<div class="stone" style="margin-top:6px">nothing in flight</div>';
  const resList=ended.length?`<div class="ovres">${ended.map(d=>{const rz=d.result||{};const g=!!rz.green;
      return `<div class="ovresrow" onclick="jumpToLoop('${esc(d.name)}')"><span class="drverdict ${g?'green':'nongreen'}">${g?'⬤':'○'} ${esc(rz.verdict||'RESULT')}</span> <span class="mono">${esc(d.name)}</span> <span class="stone">${esc(rz.resolution||'')}</span></div>`;}).join('')}</div>`
    :'<div class="stone" style="margin-top:6px">no results folded back yet</div>';
  el.innerHTML=`<div class="page">
    <div class="pagehead"><h1 class="h1">Overview</h1><span class="tag mono">▦ ${esc(scopeName)}</span></div>
    <p class="pageintro">This project at a glance — everything scoped to <b>${esc(scopeName)}</b>. Pick another in the switcher to re-scope in lockstep (§2).</p>
    <div class="ovtiles">
      ${tile('Loops',vis.length,'loops',running.length+' in flight')}
      ${tile('Idea Hub',docs.length,'hub',objectives.length+' objective'+(objectives.length===1?'':'s'))}
      ${tile('Issues',openIss.length,'issues',iss.length+' total')}
      ${tile('Origins',originBoxes().length,'origins',reachableBoxes().length+' reachable')}
    </div>
    <div class="ovsec"><div class="ovh">In flight</div>${runList}</div>
    <div class="ovsec"><div class="ovh">Latest results</div>${resList}</div>
  </div>`;
}
async function addProject(){
  const el=document.getElementById('pf_source');
  const source=el?el.value.trim():'';
  if(!source)return flash('paste a git URL or a directory path',true);
  const res=await post('/api/loops/projects/add',{source});
  if(res&&res.error)return flash(res.error+(res.errors?': '+res.errors.join('; '):''),true);
  const d=res.derived||{};
  flash('added '+(d.name?d.name+' ('+res.id+')':res.id)+((res.warnings&&res.warnings.length)?' — '+res.warnings.join('; '):''),false);
  await loadProjects();render();
}
async function delProject(id){if(!confirm('Delete project "'+id+'"?'))return;
  const res=await post('/api/loops/projects/delete',{id});
  if(res&&res.error)return flash(res.error,true);
  flash('deleted '+id,false);await loadProjects();render();
}

// ── Loop detail (right pane of Loops view) ─────────────────────────────────────
function toggleSub(key){if(collapsedSubs.has(key))collapsedSubs.delete(key);else collapsedSubs.add(key);if(detail)renderDetail(detail)}
function ctrlBtns(d){const st=d.state||'saved';if(d.host!=='local')return '<span class="stone">remote loop — read-only from this host</span>';
  let b='<div class="controls">';
  if(RUNNING.has(st))b+=`<button class="ibtn stop" title="Stop this loop" onclick="act('stop')" ${busy?'disabled':''}>⏹<span>Stop</span></button>`;
  else b+=`<button class="ibtn go" title="Start this loop" onclick="act('start')" ${busy?'disabled':''}>▶<span>Run</span></button>`;
  b+=`<button class="ibtn" title="Brief a FRESH manager session (it reads the whole loop first); then Run adopts it" onclick="briefLoop()" ${busy?'disabled':''}>✎<span>Brief</span></button>`;
  b+=`<button class="ibtn" title="Debrief — resume the SAME manager (a live briefing, or the one that ran a previous round, with its whole context); then Run re-adopts it" onclick="debriefLoop()" ${busy?'disabled':''}>↩<span>Debrief</span></button>`;
  b+=`<button class="ibtn" title="Browse output files" onclick="openFiles()">📁</button>`;
  if(FINISHED.has(st))b+=`<button class="ibtn" title="Analyze — telemetry + overview" onclick="analyze()">📊</button>`;
  b+=`<div class="ovf"><button class="ibtn" title="More actions" onclick="toggleOvf(event)">⋯</button>
      <div class="ovfmenu" id="ovfmenu">
        <div class="ovfi" onclick="closeOvf();openEditor('${esc(d.name)}')">✎ Edit config</div>
        <div class="ovfi" onclick="closeOvf();act('${d.archived?'unarchive':'archive'}')">🗂 ${d.archived?'Unarchive':'Archive'}</div>
        <div class="ovfi" onclick="closeOvf();saveToRegistry()">⭐ Save loop to registry</div>
      </div></div>`;
  return b+'</div>'}
function toggleOvf(ev){ev.stopPropagation();const m=document.getElementById('ovfmenu');if(m)m.classList.toggle('open')}
function closeOvf(){const m=document.getElementById('ovfmenu');if(m)m.classList.remove('open')}
document.addEventListener('click',()=>closeOvf());

function buildLog(rows, subAgents, subNames){
  subAgents=subAgents||{}; subNames=subNames||{};
  const root={children:[]};
  const openBySub={}, lastBySub={};
  function newSub(subId, ev){
    const node={sub:true, subId, name:(subNames[subId]||subId), start:ev||null, end:null, children:[]};
    root.children.push(node); openBySub[subId]=node; lastBySub[subId]=node; return node;
  }
  for(const e of rows){
    const st=e.status||'';
    if(e.kind==='machinery'&&st==='subloop_start'){ newSub(e.agent, e); continue; }
    if(e.kind==='machinery'&&st.indexOf('subloop_')===0){
      const n=openBySub[e.agent]||lastBySub[e.agent];
      if(n){ n.end=e; openBySub[e.agent]=null; } else root.children.push({leaf:e});
      continue; }
    if(e.kind==='machinery'&&st==='parallel'){
      const sid0=subAgents[(e.agent||'').split('+')[0]];
      if(sid0){ (openBySub[sid0]||lastBySub[sid0]||newSub(sid0,null)).children.push({leaf:e}); }
      else root.children.push({leaf:e});
      continue; }
    const sid=subAgents[e.agent];
    if(sid){ (openBySub[sid]||lastBySub[sid]||newSub(sid,null)).children.push({leaf:e}); }
    else root.children.push({leaf:e});
  }
  return root.children;
}
const subKey=n=>(n.name||'')+'|'+((n.start&&n.start.ts)||'');
// The agent label in a log row becomes a link to that agent's cross-loop
// detail — one click from "who did this turn" to "how has this agent done
// across every loop". stopPropagation so it doesn't also fire openTurn on the
// same click. Not linkable for the synthetic parallel/service agents which
// aren't in the registry.
function _agentLabel(e,role){
  const linkable=e.agent&&role!=='group'&&role!=='service'&&e.kind!=='machinery';
  const cls=`ag ${role||'nested'}${linkable?' aglink':''}`;
  const attrs=linkable?` onclick="event.stopPropagation();openAgent('${esc(e.agent)}')" title="Open ${esc(e.agent)}"`:'';
  return `<span class="${cls}"${attrs}>${esc(e.agent)}</span>`;
}
function leafRow(d,e){
  const role=roleOf(d,e.agent);const st=e.status||'';
  if(e.kind==='machinery'&&st==='parallel'){
    const ids=(e.agent||'').split('+');
    return `<div class="row mach"><span class="ts">${hhmm(e.ts)}</span><span class="ag group">parallel</span>
      <span class="st st-parallel">group</span><span class="note">${ids.length} concurrent</span></div>
      <div class="parbox">${ids.map(x=>`<span class="runchip aglink" onclick="openAgent('${esc(x)}')" title="Open ${esc(x)}">${esc(x)}</span>`).join('')}</div>`}
  if(e.kind==='machinery'&&e.phase==='guardian'){
    // Q1: guardian recovery attempts (re-nudge/service/extend/give_up) render as
    // their own turn under a 🛡 guardian marker, tagged with the wedged agent.
    return `<div class="row mach guardian"><span class="ts">${hhmm(e.ts)}</span>
      <span class="ag guardian">🛡 guardian</span><span class="gtarget">${esc(e.agent||'')}</span>
      <span class="st st-guardian st-g-${esc(st)}">${esc(st)}</span><span class="note">${esc(e.note||'')}</span></div>`}
  if(e.kind==='machinery'){
    return `<div class="row mach"><span class="ts">${hhmm(e.ts)}</span>${_agentLabel(e,role||'service')}
      <span class="st st-${esc(st)}">${esc(st)}</span><span class="note">${esc(e.note||'')}</span></div>`}
  const canOpen=e.seq&&d.host==='local';
  return `<div class="row turn" ${canOpen?`onclick="openTurn('${esc(e.agent)}',${e.seq})"`:''}>
    <span class="ts">${hhmm(e.ts)}</span>${_agentLabel(e,role)}
    <span class="st st-${esc(st)}">${esc(st)}</span><span class="note">${esc(e.note||'')}</span>
    ${canOpen?'<span class="chev">▸ open</span>':''}</div>`}
function renderNodes(d,nodes,depth){let html='';
  for(const n of nodes){
    if(n.leaf){html+=leafRow(d,n.leaf);continue}
    const key=subKey(n);const open=!collapsedSubs.has(key);
    const leaves=n.children.filter(c=>c.leaf&&c.leaf.agent&&c.leaf.kind!=='machinery');
    const nAgents=new Set(leaves.map(c=>c.leaf.agent)).size;
    const nTurns=leaves.filter(c=>c.leaf.seq).length;
    const endSt=n.end?(n.end.status||'').replace('subloop_',''):'running';
    const endNote=n.end?(n.end.note||''):'…';
    const cls=n.end?('st-'+esc(n.end.status)):'st-subloop_start';
    html+=`<div class="slgroup"><div class="slhead" onclick="toggleSub('${esc(key)}')">
        <span class="sltri">${open?'▾':'▸'}</span>
        <span class="ag loop">⟲ ${esc(n.name)}</span>
        <span class="st ${cls}">sub-loop · ${esc(endSt)}</span>
        <span class="slmeta">${nAgents} agent${nAgents===1?'':'s'} · ${nTurns} turn${nTurns===1?'':'s'}${endNote?(' · '+esc(endNote)):''}</span></div>
      ${open?`<div class="slbody">${renderNodes(d,n.children,depth+1)||'<div class="row"><span class="note">no turns captured for this sub-loop</span></div>'}</div>`:''}</div>`;
  }
  return html}
function logRows(d){const tree=buildLog(d.recent||[], d.sub_agents, d.sub_names);
  return renderNodes(d,tree,0)||'<div class="row"><span class="note">no reports yet — agents cold-booting</span></div>'}

// §4.1 — the readable result LEADS WITH THE ANSWER, not the receipt.
// Render order: curated deliverable (previewed) -> the crew's answer (last
// substantive NON-manager note) -> honest output-file list (transcripts +
// receipt demoted) -> git-commit pointer -> the finish-report receipt as a
// one-line status with the full text one disclosure deeper. Honors a declared
// artifact= as the curated override. Builds ON the prior block (still renders
// whenever there is anything to show, incl. an old finish-report-only run).
function resultBlock(d){
  const rf=d.result_files||{};const cur=rf.curated;const files=rf.files||[];
  const answer=d.answer_note;const gp=d.git_pointer;const errored=runErrored(d);
  if(!cur&&!answer&&!files.length&&!gp&&!d.finish_report&&!d.summary&&!errored)return '';
  const fileUrl=p=>'/api/loops/'+encodeURIComponent(d.name)+'/file?path='+encodeURIComponent(p);
  let h=`<div class="logh" style="margin-top:16px">result <span class="stone">— what this loop produced · <a href="#" onclick="openFiles();return false">📁 output files</a></span></div>`;
  // 0) HONEST FAILURE (§3.2). An errored run LEADS with a calm, plain statement of
  // what happened + the real reason — never the green answer-leads framing, never a
  // "✅ finished" lie. The success blocks (1,2) are skipped; any partial artifacts
  // and the demoted receipt still render below, so nothing is hidden.
  if(errored){
    const reason=failReason(d);   // §3.2: the REAL error detail, never a ✅/"finished" headline
    h+=`<div class="result-fail">
      <div class="rf-h">⚠ This run didn't finish — it ended in an error</div>
      <div class="rf-why">${esc(reason)}</div>
      <div class="rf-next">Nothing was produced. Check the loop's origin has a running loop-runner, then start it again — or open the turn-by-turn log below for the full trace.</div></div>`;
  }
  // 1) the curated deliverable, previewed and LEADING (success path only)
  if(!errored&&cur){
    h+=`<div class="logh" style="margin-top:6px">📄 <a href="${fileUrl(cur.path)}" target="_blank" rel="noopener">${esc(cur.path)}</a>${cur.override?' <span class="stone">· declared deliverable</span>':''}</div>
      <div class="pre result">${esc(cur.preview)}${cur.truncated?'\n…':''}</div>`;
  }
  // 2) the ANSWER — last substantive non-manager agent note (success path only)
  if(!errored&&answer){
    h+=`<div class="logh" style="margin-top:10px">the crew's answer</div><div class="pre result">${esc(answer)}</div>`;
  }else if(!errored&&!cur){
    h+=`<div class="pre result stone">No agent note or deliverable captured yet — open the output files or the turn-by-turn log below.</div>`;
  }
  // 3) honest output-file list (transcripts + receipt demoted out)
  if(files.length){
    const shown=files.slice(0,8);
    h+=`<div class="logh" style="margin-top:10px">output files <span class="stone">${files.length}</span></div>
      <div class="filelist">`+shown.map(f=>`<div class="frow"><a class="fmain" href="${fileUrl(f.path)}" target="_blank" rel="noopener"><span class="fp">${esc(f.path)}</span></a></div>`).join('')+`</div>`;
    if(files.length>shown.length)h+=`<div class="stone"><a href="#" onclick="openFiles();return false">…and ${files.length-shown.length} more</a></div>`;
  }
  // 4) git-commit pointer when a note names one
  if(gp){
    h+=`<div class="stone" style="margin-top:8px">↪ committed${gp.branch?' to <code>'+esc(gp.branch)+'</code>':''} @ <code>${esc(gp.commit)}</code></div>`;
  }
  // 5) the finish-report receipt, DEMOTED to a one-line status (full text ▸).
  // §3.2 (CLASS fix): this status line is the THIRD render site that surfaced the
  // raw finish_report FIRST line — for an errored run that first line is the stale
  // "✅ loop finished — stopped by owner" green headline, an unguarded fake-success.
  // Route the errored case through the SAME failReason() guard the headline/reason
  // line use, so NO ✅/"finished" string can render on any errored-run surface. The
  // success/stopped-cleanly path keeps its exact prior text (additive, byte-stable).
  if(d.finish_report){
    const line=(errored?failReason(d):(d.finish_report.split('\n').map(x=>x.trim()).find(x=>x)||'')).slice(0,120);
    h+=`<div class="stone" style="margin-top:8px">status: ${esc(line)} · <a href="#" onclick="showReceipt();return false">full receipt ▸</a></div>`;
  }
  // 6) invisible disposition (§4.2–4.4): inline ship/keep taps (behavior) + the
  // three quiet good/ok/bad buttons (explicit) BELOW the result — never a modal,
  // never demanded. Only when the loop actually PRODUCED something (re-run is not
  // a button here: it is captured at the loop_start engine seam for every origin).
  // §5.4 — when the closing Resolution card is showing (the engine serves dev-1's
  // resolution and the verdict has resolved), IT owns the gated good/ok/bad rating;
  // this legacy inline row would be a second, ungated disposition surface, so it
  // stands down. It stays as the graceful fallback for an older/remote engine that
  // doesn't serve the resolution card.
  const resOwns=(typeof teamRoom!=='undefined')&&teamRoom&&teamRoom.resolution&&teamRoom.resolution.verdict&&teamRoom.resolution.verdict.resolved;
  if(!resOwns&&(cur||answer||files.length||gp||d.finish_report||errored)){
    // Better-UX #5: good/ok/bad is the ONLY manual rating — ship/keep are retired
    // (removed/deleted are recorded automatically by the engine, never a click).
    h+=`<div class="disprow" style="margin-top:14px;display:flex;gap:6px;align-items:center;flex-wrap:wrap">
      <button class="btn" style="padding:2px 8px;font-size:11px;opacity:.7" onclick="disp('good','explicit')">good</button>
      <button class="btn" style="padding:2px 8px;font-size:11px;opacity:.7" onclick="disp('ok','explicit')">ok</button>
      <button class="btn" style="padding:2px 8px;font-size:11px;opacity:.7" onclick="disp('bad','explicit')">bad</button>
      <span id="dispmsg" class="stone" style="margin-left:6px"></span></div>`;
  }
  return h;
}
async function disp(verb,source){if(busy)return;busy=true;
  const m=document.getElementById('dispmsg');if(m)m.textContent='…';
  try{const res=await post('/api/loops/'+encodeURIComponent(sel)+'/disposition',{verb,source});
    if(res&&res.ok){if(m)m.textContent='✓ '+verb+' recorded';}
    else{if(m)m.textContent=(res&&res.error)||'could not record';}
  }catch(e){if(m)m.textContent='failed: '+e}
  busy=false;}
function showReceipt(){if(!detail)return;
  modal(`<h2>Finish-report — ${esc(detail.name)}</h2>
    <div class="hint">The engine's end-of-run receipt (the owner status ping), demoted below the result. This is the machinery record — not the deliverable.</div>
    <div class="pre">${esc(detail.finish_report||'(none)')}</div>`);}
// ── §5.3 the loop-detail as a TEAM ROOM ───────────────────────────────────────
// dev-1's loop_team_room payload, fetched alongside the detail. null → the engine
// doesn't serve the tool (an older build, e.g. a remote mirror) and renderDetail
// falls back to the legacy stat tiles, so the loop-detail never half-breaks.
let teamRoom=null;
// uxui#4 (§5.3) — a roster card's "owns:" must read the agent's PER-LOOP
// responsibility (its charter in THIS loop's config), not the generic role
// template. The team-room payload's `owns` is the personality string ("A strong
// full-stack developer"); the real per-loop responsibility is each step's `goal`
// in the loop config. We fetch the config alongside the team room and overlay the
// goal-derived responsibility onto each roster entry. Config missing (older
// mirror) → we keep the backend `owns` honestly rather than blanking it.
function respFromGoal(goal){
  const g=(goal||'').trim();if(!g)return'';
  // first sentence — the concise responsibility; cap so a long charter stays a
  // one-liner. Split on a sentence end followed by a space (keeps "e.g." intact
  // rarely enough to not matter here).
  let s=g.split(/(?<=[.!?])\s+/)[0]||g;
  if(s.length>110)s=s.slice(0,108).replace(/\s+\S*$/,'')+'…';
  return s;
}
async function fetchTeamRoom(n,h){
  try{const r=await j('/api/loops/'+encodeURIComponent(n)+'/teamroom?host='+encodeURIComponent(h||'local'));
    const tr=(r&&!r.error&&Array.isArray(r.roster))?r:null;
    if(tr){
      // Overlay per-loop responsibilities from the config's per-agent goals.
      try{const c=await j('/api/loops/'+encodeURIComponent(n)+'/config');
        const steps=(c&&c.config&&c.config.steps)||{};
        tr.roster.forEach(a=>{const st=a&&a.agent?steps[a.agent]:null;
          const resp=st?respFromGoal(st.goal):'';
          if(resp)a.owns=resp;});   // real per-loop charter; else keep backend owns
      }catch(e){/* config unavailable → keep the backend owns, never blank it */}
    }
    teamRoom=tr;}
  catch(e){teamRoom=null;}
}
// The single COMMITTED convergence chip. dev-1 computes the value honestly (green
// = Delivered ONLY; a stop/error can never read green); we only paint it.
const CONV_CLASS={Delivered:'conv-delivered',Converging:'conv-converging',Aligning:'conv-aligning',Stalled:'conv-stalled','Needs you':'conv-needsyou'};
function convChipHtml(c){if(!c||!c.value)return'';
  return `<span class="conv ${CONV_CLASS[c.value]||'conv-aligning'}" title="${esc(c.reason||'')}">${esc(c.value)}</span>`;}
// Never-blank turn bar — dev-1 guarantees `used` is numeric on a running loop
// (the blank-TURNS fix is at the data source), so we render 0, never a blank.
function turnBarHtml(t){if(!t)return'';
  const lim=(t.limit!=null)?('/'+t.limit):'';
  const wd=t.running?` · <span class="wd">wind-down in ${t.winddown_in!=null?t.winddown_in:'—'}</span>`:'';
  return `<span class="trm-bar">turn ${t.used!=null?t.used:0}${lim}${wd}</span>`;}
// One roster card. The backend orders the roster manager-first; each card carries
// the role, one-line responsibility ("owns: …"), a live status dot, what it's
// doing now, and its last report. The WHOLE card opens the agent's REPORT HISTORY
// (§3.B — a report history, not a prompt dump).
function agentCardHtml(c){
  const dot='adot-'+((c.statusDot)||'idle');
  const role=(c.displayRole||c.role||'').toUpperCase();
  const now=c.doingNow?`<span class="anow">${esc(c.doingNow)}</span>`:'';
  const lr=c.lastReport||null;
  const last=lr?`<div class="alast"><span class="lk">${esc(lr.status||'report')}</span>${esc(firstLine(lr.note)||lr.note||'')}</div>`:'';
  return `<div class="acard ${c.isManager?'mgr':''}" onclick="openAgentReports('${esc(c.agent)}')" title="Open ${esc(c.agent)}'s report history">
    <span class="adot ${dot}"></span>
    <div class="atop"><span class="arole">${esc(role)}</span><span class="aname">${esc(c.agent)}</span>${now}</div>
    <div>${c.owns?`<span class="aowns">owns: ${esc(c.owns)}</span>`:''}${last}</div>
  </div>`;}
// The three-zone team room: (A) the convergence chip + never-blank turn bar +
// manager's one-line read; (B) the roster (the spine). (C result + steer stays
// below, kept from the existing detail.)
function teamRoomHtml(tr){
  const mgr=tr.managerRead?`<div class="mgrread"><b>manager reads</b> ${esc(firstLine(tr.managerRead)||tr.managerRead)}</div>`:'';
  const cards=(tr.roster||[]).map(agentCardHtml).join('');
  return `<div class="trm">
    <div class="trm-head">${convChipHtml(tr.convergence)}${turnBarHtml(tr.turn)}</div>
    ${mgr}
    <div class="roster">${cards}</div>
  </div>`;}
// ── §5.4 the closing RESOLUTION CARD ──────────────────────────────────────────
// dev-1's loop_resolution payload rides inside loop_team_room (`tr.resolution`) so
// the loop-detail carries the closing card without a second round-trip. We render
// EXACTLY what its COMPUTED verdict says — `verdict.green` is the ONLY green gate,
// so a guardian-stop/error can never paint green here (it lands red/amber with a
// Respawn/Take-over action). No second green path in the frontend.
const RES_TONE=v=>v&&v.green?'green':(v&&(v.value==='FAILED'||v.value==='ABANDONED—needs you')?'red':'amber');
function resolutionCardHtml(rc){
  if(!rc||!rc.verdict||!rc.verdict.resolved)return'';   // no resolution yet → the team room speaks
  const v=rc.verdict,tone=RES_TONE(v);
  // (1) the verdict badge — colored ONLY to v.green — + the one owner action owed.
  const act=(!v.green&&v.ownerAction)?`<button class="btn ${v.value==='FAILED'||v.value==='ABANDONED—needs you'?'stop':'ghost'}" style="padding:3px 12px;font-size:11.5px" onclick="ownerAct('${esc(v.ownerAction.kind)}')">${esc(v.ownerAction.label||v.ownerAction.kind)}</button>`
    :(v.green&&v.ownerAction&&v.ownerAction.kind!=='watch'?`<button class="btn go" style="padding:3px 12px;font-size:11.5px" onclick="ownerAct('${esc(v.ownerAction.kind)}')">${esc(v.ownerAction.label||v.ownerAction.kind)}</button>`:'');
  // (2) the resolution in one line — the crew's real-world answer, led first.
  const line=rc.resolution?`<div class="rc-line">${esc(rc.resolution)}</div>`:'';
  const reason=(v.reason&&v.reason!==rc.resolution)?`<div class="rc-reason">${esc(v.reason)}</div>`:'';
  // (3) the real handles — commit (the true git handle), the deliverable, receipt.
  const h=rc.handles||{};const hd=[];
  const furl=p=>'/api/loops/'+encodeURIComponent(rc.name||sel||'')+'/file?path='+encodeURIComponent(p);
  if(h.commitShort)hd.push(`<span class="rc-handle" title="${esc(h.commit||'')} — the commit this loop landed"><span class="hk">commit</span><code>${esc(h.commitShort)}</code></span>`);
  // FRONTEND HALF of the [object Object] fix. dev-1 fixed the DATA (handles.deliverable
  // is now a STRING path, no longer the raw {kind,path,exists} dict). This guard is the
  // belt-and-suspenders render half: paint the deliverable ONLY when it is a real string
  // handle. A stray object (an older/replayed payload, any future drift) now renders as
  // honest ABSENCE — never the literal "[object Object]" — and the file drawer still
  // carries every artifact via the "full receipt" affordance below.
  const deliv=(typeof h.deliverable==='string')?h.deliverable:'';
  if(deliv)hd.push(`<span class="rc-handle"><span class="hk">deliverable</span><a href="${furl(deliv)}" target="_blank" rel="noopener">${esc(shortPath(deliv))}</a></span>`);
  const nMore=((h.artifacts||[]).length);
  if(nMore>1||((h.artifacts||[]).length&&!deliv))hd.push(`<span class="rc-handle"><a href="#" onclick="openFiles();return false">full receipt · ${nMore} file${nMore===1?'':'s'} ▸</a></span>`);
  const handles=hd.length?`<div class="rc-handles">${hd.join('')}</div>`:'';
  // the compact proof strip — tests / turns / duration / signals, honest tokens.
  const p=rc.proof||{};const pf=[];
  if(p.tests)pf.push(`<span><span class="pk">tests</span> <span class="${looksRed(p.tests)?'red':''}">${esc(p.tests)}</span></span>`);
  if(p.turns!=null)pf.push(`<span><span class="pk">turns</span> ${esc(p.turns)}${p.winddown!=null?'+'+esc(p.winddown)+'wd':''}</span>`);
  if(p.seconds!=null)pf.push(`<span><span class="pk">ran</span> ${fmtSecs(p.seconds)}</span>`);
  const proof=pf.length?`<div class="rc-proof">${pf.join('')}</div>`:'';
  return `<div class="rescard ${tone}">
    <div class="rc-top"><span class="rc-verdict">${esc(v.value)}</span>${act}</div>
    ${line}${reason}${handles}${proof}
    ${dispositionHtml(rc)}
  </div>`;}
// The disposition control — one question "How did this land?" — GATED on
// rc.disposition.offered (dev-1 offers it ONLY when there's an outcome to judge; a
// crash can't be rated). Pre-fills from the current single-replaceable verdict.
function dispositionHtml(rc){
  const dd=rc.disposition||{};if(!dd.offered)return'';
  const cur=dd.current||null,curv=cur&&cur.verb;
  const chip=(verb,label)=>`<button class="rc-rate${curv===verb?' on-'+verb:''}" onclick="rateLoop('${verb}')">${label}</button>`;
  const already=cur?`<div class="rc-current">you rated this <b>${esc(cur.verb)}</b>${cur.note?' · '+esc(cur.note):''} · change below</div>`:'';
  const next=(cur&&cur.verb==='bad'&&cur.nextAction)?`<div class="rc-nextact">↪ ${esc(cur.nextAction)}</div>`:'';
  return `<div class="rc-disp">
    ${already}
    <span class="dq">how did this land?</span>
    ${chip('good','good')}${chip('ok','ok')}${chip('bad','bad')}
    <input class="rc-why" id="rcWhy" placeholder="why? (optional)" value="${esc((cur&&cur.note)||'')}">
    <span id="rcMsg" class="stone" style="font-size:11px"></span>
    ${next}
  </div>`;}
// Rate the loop with the "why" note the card carries — reuses the disposition POST
// (source="explicit"); on success re-fetch the card so the pre-fill + aggregate
// reflect the new single-replaceable verdict.
async function rateLoop(verb){if(busy||!sel)return;busy=true;
  const m=document.getElementById('rcMsg');if(m)m.textContent='…';
  const why=(document.getElementById('rcWhy')||{}).value||'';
  try{const res=await post('/api/loops/'+encodeURIComponent(sel)+'/disposition',{verb,source:'explicit',note:why.trim()});
    if(res&&res.ok){if(m)m.textContent='✓ '+verb+' recorded';busy=false;
      await fetchTeamRoom(sel,selHost||'local');if(detail)renderDetail(detail);
      loadProjectDispositions();return;}
    if(m)m.textContent=(res&&res.error)||'could not record';
  }catch(e){if(m)m.textContent='failed: '+e}
  busy=false;}
// The one owner action the card offers (Respawn / Take over / Review & merge).
// Respawn/take-over route through the existing start/reply seams honestly; merge
// /review just opens the files receipt — we never fake a git operation here.
async function ownerAct(kind){
  if(kind==='respawn'){if(confirm('Respawn this loop from its saved config?'))act('start');return;}
  if(kind==='take_over'){pick(sel,selHost);flash('take over — answer the manager below',false);return;}
  openFiles();}
// A path handle squeezed to its tail so a long deliverable path stays one chip.
function shortPath(p){if(!p)return'';const s=''+p;return s.length>42?'…'+s.slice(-40):s;}
// A raw seconds count → human duration (dur() takes a start-timestamp, not a span).
function fmtSecs(s){s=Math.max(0,Math.floor(+s||0));if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m'+(s%60)+'s';return Math.floor(s/3600)+'h'+Math.floor((s%3600)/60)+'m';}
// mirror dev-1's tests-red rule for the proof strip's red tint (paint only).
function looksRed(t){if(!t)return false;const s=(''+t).toLowerCase();
  if(s.indexOf('red')>=0||s.indexOf('fail')>=0)return true;
  const m=s.match(/(\d+)\s*\/\s*(\d+)/);return m?(+m[2]>0&&+m[1]<+m[2]):false;}
// Click an agent card → its report history (newest first). Each row opens the
// agent-turn card via the existing openTurn() (report first; framed prompt +
// transcript behind that tool's toggle).
async function openAgentReports(agent){if(!sel)return;flash('loading report history…',false);
  try{const r=await j('/api/loops/'+encodeURIComponent(sel)+'/agent-reports?agent='+encodeURIComponent(agent)+'&host='+encodeURIComponent(selHost||'local'));
    if(r.error)return flash(r.error,true);
    const reps=(r.reports||[]);
    const rows=reps.length?reps.map(rp=>`<div class="regrow clk" onclick="openTurn('${esc(agent)}',${rp.seq})" title="Open this turn's agent-turn card">
        <span class="rid">turn ${rp.seq} · ${esc(rp.status||'—')}</span>
        <span class="rn">${esc(firstLine(rp.note)||rp.note||'')}</span>
        <span class="hubago">${rp.ts?ago(rp.ts):''}</span></div>`).join('')
      :'<div class="stone">No reports yet — this agent has not taken a turn.</div>';
    modal(`<h2>${esc(agent)} · report history</h2>
      <div class="hint">Newest first — every end-of-turn report this agent posted. Click one to open its agent-turn card (report first; framed prompt + transcript behind a toggle).</div>
      <div class="reghist">${rows}</div>`);
  }catch(e){flash('report history unavailable — '+e,true)}}
function renderDetail(d){detail=d;
  const el=document.getElementById('loopDetail');if(!el)return;
  if(!d||d.error){el.innerHTML='<div class="empty">'+esc(d&&d.error||'error')+'</div>';return}
  const st=d.state||'saved';let banners='';
  if(st==='waiting_owner'&&d.question)banners+=`<div class="banner q"><b>⏸ The crew wants your call</b><br>${esc(d.question)}
    ${d.host==='local'?`<div class="compose"><textarea id="replyBox" placeholder="Answer the manager — one line is plenty…"></textarea>
      <button class="btn primary" onclick="act('reply')">Send reply</button></div>`:''}</div>`;
  if(st==='needs_owner'&&d.guardian_alert)banners+=`<div class="banner a"><b>⚠ Guardian flagged this one — your call</b><br>${esc(d.guardian_alert)}</div>`;
  const running=d.live||[];
  if(running.length)banners+=`<div class="banner live"><span class="dot"></span><b>◐ Loop running</b>
    <span class="stone mono" style="font-size:11px">${running.length} agent${running.length===1?'':'s'} in flight</span>
    ${running.map(r=>`<span class="runchip">${esc(r.agent)}${r.phase?' · '+esc(r.phase):''}${r.since?' · '+dur(r.since):''}</span>`).join('')}</div>`;
  const pend=d.input_pending||[];
  if(pend.length)banners+=`<div class="pend"><b>📨 ${pend.length} steering note(s) queued</b> — the manager reads them at the top of its next turn.
    ${pend.map(p=>`<div class="pi">• ${esc(p.text)}</div>`).join('')}</div>`;
  const stat=(k,v)=>`<div class="stat"><div class="k">${k}</div><div class="v">${v==null?'—':esc(v)}</div></div>`;
  const canInput=d.host==='local'&&(RUNNING.has(st)||st==='saved');
  // REDESIGN-SPEC §5.3 — the detail IS a team room when the engine serves dev-1's
  // loop_team_room for THIS loop: the roster (manager first) + committed
  // convergence chip + never-blank turn bar + manager read REPLACE the `0W 8I 1M`
  // stat tiles, and the scheduler's turn-by-turn log demotes to a collapsed
  // "Engine activity" drawer. When the tool isn't served (older/remote engine) we
  // fall back to the legacy stat tiles + open log so the detail never half-breaks.
  const hasTR=teamRoom&&teamRoom.name===d.name&&Array.isArray(teamRoom.roster);
  // §5.4 — when the loop has ENTERED a terminal state, the TOP of the same pane
  // resolves into the closing card (dev-1's computed verdict rides in the team-room
  // payload as `resolution`). It leads the pane; the team room reads below it.
  const resCard=hasTR?resolutionCardHtml(teamRoom.resolution):'';
  const body=hasTR
    ? `${resCard}${teamRoomHtml(teamRoom)}
    ${resultBlock(d)}
    <details class="engdraw">
      <summary>Engine activity — turn-by-turn log (${(d.recent||[]).length})</summary>
      <div class="logh" style="margin-top:4px"><span class="stone">the scheduler's per-turn trace — guardian re-nudges, parallel groups. Click a turn to read its framed prompt.</span></div>
      <div class="log">${logRows(d)}</div>
    </details>`
    : `<div class="stats">
      ${stat('turns', d.turns_used!=null?(d.turns_used+(d.turnLimit?' / '+d.turnLimit:'')):null)}
      ${stat('wind-down', d.winddown_turns)}
      ${stat('team', ((d.team.workers||[]).length)+'W '+((d.team.inputs||[]).length)+'I 1M')}
      ${stat('ended', d.ended)}${stat('retired', (d.retired||[]).length||null)}
    </div>
    ${resultBlock(d)}
    <div class="logh">turn-by-turn log (${(d.recent||[]).length}) <span class="stone">— click a turn to read its framed prompt</span></div>
    <div class="log">${logRows(d)}</div>`;
  el.innerHTML=`
    <div class="dhead"><span class="${d.host==='local'?'host':'host anneke'}">${esc(d.host)}</span><span class="lname">${esc(d.name)}</span>
      <span class="badge b-${honestState(d)}">${honestState(d).replace('_',' ')}</span>
      <span class="sub" style="margin-left:6px;font-family:var(--mono);font-size:11px">${d.slug?('slug '+esc(d.slug)):''} · ${ago(d.updated||d.started)}</span></div>
    <div class="goal">${esc(d.goal||'')}${(d.goal||'').length>=219?'…':''}</div>
    ${bindChips(d)}
    ${ctrlBtns(d)}
    ${banners}
    ${body}
    ${canInput?`<div class="logh" style="margin-top:16px">steer this loop</div>
      <div class="compose"><textarea id="inputBox" placeholder="Drop a note the manager reads at the top of its next turn…"></textarea>
        <button class="btn primary" onclick="act('input')">Send</button></div>`:''}`;
}

// OWNER RESTORE (2026-09-22): ✎ Brief / ↩ Debrief a running loop spawn the REAL
// tmux adopt-the-manager session (loop_brief_session/loop_debrief_session), then show
// the attach command — the pre-redesign behaviour. Rationale (owner): a brief that
// spins up an in-process chat INSTEAD of the real daemon session hides whether the
// substrate is healthy; the tmux brief opening is itself the smoke-test that the whole
// stack is up. The §5.5 native chat is NOT deleted — it still powers the +Loop
// composer's create-by-chat (openBriefChat with a goal). The /api/loops/{name}/brief,
// /debrief routes are now registered in loops_dashboard.py (they had only lived in the
// coordinator app, which is why they 404'd before).
async function briefLoop(){if(!sel||busy)return;busy=true;flash('spawning a fresh briefing session…',false);
  try{const res=await post('/api/loops/'+encodeURIComponent(sel)+'/brief',{});
    if(res&&res.error){flash(res.error,true)}
    else{modal(`<h2>Brief the manager — ${esc(sel)}</h2>
      <div class="hint">A FRESH session preloaded with this loop's full context is now running on the loop's host. Attach in a terminal (or TOOLS ▸ Terminal), reshape the goal + crew, then hit Run — this same session becomes the manager (nothing resets).</div>
      <div class="logh" style="margin-top:12px">attach command (copy — run on the loop's host)</div>
      <pre class="ov" style="user-select:all;white-space:pre-wrap">${esc(res.attach||'')}</pre>`);}
  }catch(e){flash('brief failed: '+e,true)}
  busy=false;}
async function debriefLoop(){if(!sel||busy)return;busy=true;flash('resuming the manager…',false);
  try{const res=await post('/api/loops/'+encodeURIComponent(sel)+'/debrief',{});
    if(res&&res.error){flash(res.error,true)}
    else{const how=res.resumed?'resurrected with its whole prior context (from its saved transcript)':'the live briefing session';
      modal(`<h2>Debrief the manager — ${esc(sel)}</h2>
      <div class="hint">Resumed ${how}. Attach in a terminal (or TOOLS ▸ Terminal) to continue the conversation, then hit Run — it re-adopts this same manager, so the last round carries over.</div>
      <div class="logh" style="margin-top:12px">attach command (copy — run on the loop's host)</div>
      <pre class="ov" style="user-select:all;white-space:pre-wrap">${esc(res.attach||'')}</pre>`);}
  }catch(e){flash('debrief failed: '+e,true)}
  busy=false;}

async function act(action){if(busy)return;busy=true;
  const body={action,host:selHost||'local'};   // start/stop route to the loop's origin
  if(action==='input'){const t=(document.getElementById('inputBox')||{}).value||'';if(!t.trim()){busy=false;return flash('nothing to send',true)}body.text=t}
  if(action==='reply'){const t=(document.getElementById('replyBox')||{}).value||'';if(!t.trim()){busy=false;return flash('nothing to send',true)}body.reply=t}
  try{const res=await post('/api/loops/'+encodeURIComponent(sel)+'/action',body);
    if(res&&res.error)flash(res.error,true);
    else flash(action+': ok'+(res&&res.state?(' → '+res.state):(res&&res.pending!=null?(' ('+res.pending+' queued)'):'')),false);
  }catch(e){flash('request failed: '+e,true)}
  busy=false;await tickDetail();await tickList();}

async function analyze(){flash('running analysis…',false);
  try{const res=await post('/api/loops/'+encodeURIComponent(sel)+'/action',{action:'analyze'});
    if(res&&res.error)return flash(res.error,true);
    showAnalysis(res);
  }catch(e){flash('analyze failed: '+e,true)}}
function showAnalysis(r){const t=r.telemetry||{};const pa=t.per_agent||{};
  const rows=Object.entries(pa).map(([a,v])=>`<div class="regrow"><span class="rid">${esc(a)}</span><span class="rn">${v.turns} turn(s) · ${esc(Object.keys(v.statuses||{}).join(', '))}</span></div>`).join('');
  modal(`<h2>Analysis — ${esc(r.name)}</h2><div class="hint">on-demand telemetry + heuristic overview</div>
    <div class="ov">${esc(r.ai_overview||'')}</div>
    <div class="stats">
      ${['turns_used','winddown_turns','distinct_agents','parallel_groups','subloops','wall_clock_min','report_count'].map(k=>`<div class="stat"><div class="k">${k.replace(/_/g,' ')}</div><div class="v">${t[k]==null?'—':esc(t[k])}</div></div>`).join('')}
    </div>
    <div class="logh">per-agent</div>${rows||'<span class="stone">no per-agent data</span>'}
    ${t.turn_gap_min&&t.turn_gap_min.n?`<div class="logh" style="margin-top:12px">turn gaps (min)</div><div class="stone">n=${t.turn_gap_min.n} · min ${t.turn_gap_min.min} · median ${t.turn_gap_min.median} · mean ${t.turn_gap_min.mean} · max ${t.turn_gap_min.max}</div>`:''}
    <div class="stone" style="margin-top:12px">${esc(t.note||'')}</div>`);
}

async function openTurn(agent,seq){flash('loading turn…',false);
  try{const r=await j('/api/loops/'+encodeURIComponent(sel)+'/turn?agent='+encodeURIComponent(agent)+'&seq='+seq+'&host='+encodeURIComponent(selHost||'local'));
    if(r.error)return flash(r.error,true);
    const rep=r.report?(`status: ${r.report.status}\nnote: ${r.report.note||''}`):'(no matching report)';
    modal(`<h2>${esc(agent)} · turn ${seq}</h2>
      <div class="logh">end-of-turn report</div><div class="pre">${esc(rep)}</div>
      <div class="logh" style="margin-top:12px">framed prompt <span class="stone">— what the agent was told</span></div><div class="pre">${esc(r.prompt||'(none)')}</div>
      ${r.transcript_tail?`<div class="logh" style="margin-top:12px">transcript tail</div><div class="pre">${esc(r.transcript_tail)}</div>`:''}`);
  }catch(e){flash('turn load failed: '+e,true)}}

async function openFiles(){
  if(!sel)return; flash('loading files…',false);
  let r; try{r=await j('/api/loops/'+encodeURIComponent(sel)+'/files')}catch(e){return flash('files unavailable',true)}
  if(r.error)return flash(r.error,true);
  const groups=r.artifacts||[];
  const icon=k=>({html:'🌐',svg:'🖼',image:'🖼',text:'📄',other:'📎'}[k]||'📎');
  const url=p=>'/api/loops/'+encodeURIComponent(sel)+'/file?path='+encodeURIComponent(p);
  const dl=p=>url(p)+'&download=1';
  const sz=n=>n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(1)+' MB';
  let html='<h3>📁 Files — '+esc(sel)+'</h3>';
  if(!groups.length||!r.count){html+='<div class="stone">No output files yet — run the loop; its result (finish-report + transcripts) lands here in <code>_output/'+esc(sel)+'/</code> when it finishes.</div>';return modal(html)}
  html+='<div style="margin:6px 0 10px"><a class="btn go" href="/api/loops/'+encodeURIComponent(sel)+'/download">⬇ Download all (.zip)</a></div>';
  for(const g of groups){
    html+='<div class="fgrp">'+esc(g.label||'workspace')+' <span class="stone">'+esc(g.dir)+'</span></div>';
    html+='<div class="filelist">'+(g.files||[]).map(f=>
      `<div class="frow"><a class="fmain" href="${url(f.path)}" target="_blank" rel="noopener"><span class="fi">${icon(f.kind)}</span><span class="fp">${esc(f.path)}</span></a><span class="fsz">${sz(f.size)}</span><a class="dl" href="${dl(f.path)}" title="Download ${esc(f.path)}">⬇</a></div>`
    ).join('')+'</div>';
  }
  if(r.capped)html+='<div class="stone" style="margin-top:8px">…list capped at '+r.count+' files.</div>';
  html+='<div class="stone" style="margin-top:8px">HTML/images open rendered in a new tab; text opens as plain text.</div>';
  modal(html);
}

// ── Agent detail (opens inside Agents view) ────────────────────────────────────
const VSRC={init:'created',edit:'persona edit',distill:'goal re-distill',migrated:'imported (v1)'};
function fmtWhen(ts){if(!ts)return '';try{return new Date(ts*1000).toISOString().slice(0,16).replace('T',' ')+'Z'}catch(e){return ''}}
function verChanged(c){return (c||[]).map(f=>`<span class="vchg">${f==='genericGoal'?'goal':esc(f)}</span>`).join('')}
function modelBadge(m,sm){return m?`<span class="vmodel${sm?' sm':''}">◈ ${esc(m)}</span>`
  :(sm?'':`<span class="vmodel vinherit">◈ inherits model</span>`)}
async function openAgent(id){curAgent=id;agentDetail=null;agentRec=null;agentVers=null;expandedVers=new Set();
  view='agents';_writeUrl('/agents/'+encodeURIComponent(id));render();
  const P=[
    j('/api/loops/agent?id='+encodeURIComponent(id)).then(r=>{agentDetail=r}).catch(e=>{agentDetail={error:''+e}}),
    j('/api/loops/agent/record?id='+encodeURIComponent(id)).then(r=>{agentRec=r}).catch(()=>{agentRec={error:'unavailable'}}),
    j('/api/loops/agent/versions?id='+encodeURIComponent(id)).then(r=>{agentVers=r}).catch(()=>{agentVers=null}),
  ];
  await Promise.all(P);
  if(view==='agents'&&curAgent===id)render();}
function closeAgent(){curAgent=null;agentDetail=null;agentRec=null;agentVers=null;_writeUrl('/agents');render();}
function toggleVer(n){if(expandedVers.has(n))expandedVers.delete(n);else expandedVers.add(n);render()}
function agentIdentity(){
  const r=agentRec; if(!r||r.error||!Array.isArray(r.versions)||!r.versions.length)return '';
  const versions=r.versions;
  const head=versions.find(v=>v.version===r.head)||versions[versions.length-1];
  if(!head)return '';
  const role=r.defaultRole?`<span class="vrole">${esc(r.defaultRole)}</span>`:'';
  const tl=(agentVers&&Array.isArray(agentVers.versions)&&agentVers.versions.length)
    ? agentVers.versions
    : versions.map(v=>({version:v.version,source:v.source,note:v.note,createdAt:v.createdAt,model:v.model,changed:[],isHead:v.version===r.head}));
  const rows=tl.slice().reverse().map(v=>{
    const full=versions.find(x=>x.version===v.version)||{};
    const open=expandedVers.has(v.version);
    const src=VSRC[v.source]||esc(v.source||'edit');
    let row=`<div class="vrow ${open?'exp':''} ${v.isHead?'ishead':''}" onclick="toggleVer(${v.version})">
      <span class="tri ${open?'open':''}">▸</span>
      <span class="vtag ${v.isHead?'head':''}">v${v.version}${v.isHead?' · head':''}</span>
      <span class="vsrc">${src}</span>${verChanged(v.changed)}${modelBadge(v.model,true)}
      <span class="vwhen">${fmtWhen(v.createdAt)}</span></div>`;
    if(open)row+=`<div class="vbody" onclick="event.stopPropagation()">
      ${v.note?`<div class="vnote">"${esc(v.note)}"</div>`:''}
      <div class="vk">persona</div><div class="vpre">${esc(full.persona||'—')}</div>
      <div class="vk" style="margin-top:8px">generic goal</div><div class="vpre">${esc(full.genericGoal||'—')}</div></div>`;
    return row}).join('');
  const hasModel=!!(head.model||r.model);
  return `<div class="idcard">
      <div class="idhead">${modelBadge(head.model||r.model)}${role}
        ${hasModel?'<span class="idnote">recorded · not yet applied at spawn</span>':''}
        <span class="idver">${versions.length} version${versions.length!==1?'s':''}</span></div>
      <div class="vk">persona <span class="stone">— fixed identity</span></div>
      <div class="vpre">${esc(head.persona||'—')}</div>
      <div class="vk" style="margin-top:10px">generic goal <span class="stone">— loop-agnostic; distilled from past loop goals</span></div>
      <div class="vpre">${esc(head.genericGoal||'—')}</div></div>
    <div class="logh" style="margin-top:16px">version timeline
      <span class="stone">— every persona edit / goal re-distill forks an immutable version; loops pin the exact one they ran</span></div>
    <div class="vtl">${rows}</div>`;
}
function agentBody(){const d=agentDetail;const idb=agentIdentity();const isV2=agentRec&&!agentRec.error&&Array.isArray(agentRec.versions);
  const back=`<div class="backlink" onclick="closeAgent()">← all agents</div>`;
  const mgrH=/manager|orchestrat|coordinat/i.test(curAgent||'');
  const savedById={};for(const a of regSavedAgents||[])savedById[a.id]=a;
  const isFav=!!(savedById[curAgent]&&savedById[curAgent].favorite);
  const favBtn=`<span class="star ${isFav?'on':''}" style="font-size:20px" onclick="toggleFav('${esc(curAgent||'')}',${isFav?'false':'true'})" title="${isFav?'unfavorite':'favorite'}">★</span>`;
  const dhead=`<div class="dhead">${favBtn}<span class="aid ${mgrH?'mgr':''}" style="font-size:19px">${esc(curAgent)}</span>${isV2?'<span class="v2tag">registry · v2</span>':''}</div>`;
  if(!d)return `${back}${dhead}${idb}<div class="empty">loading activity…</div>`;
  if(d.error)return `${back}${dhead}${idb}<div class="anote" style="margin-top:14px">activity unavailable: ${esc(d.error)}</div>`;
  const t=d.totals||{};
  const stat=(k,v)=>`<div class="stat"><div class="k">${k}</div><div class="v">${v==null?'—':esc(v)}</div></div>`;
  const perLoop=(d.per_loop||[]).map(p=>`<tr class="clk" onclick="jumpToLoop('${esc(p.loop)}')">
    <td><span class="aid">${esc(p.loop)}</span></td><td class="num">${p.turns}</td>
    <td class="num">${p.continues||0}</td><td class="num">${p.retries||0}</td><td class="num">${mins(p.avg_gap_min)}</td></tr>`).join('');
  const turns=(d.turns||[]).map(r=>`<tr class="clk" onclick="openTurnFor('${esc(r.loop)}','${esc(d.agent)}',${r.seq})">
    <td class="stone mono">${esc(r.loop)}</td><td class="num stone">#${r.seq}</td>
    <td><span class="st st-${esc(r.status)}">${esc(r.status)}</span></td>
    <td class="num stone">${r.gap_min==null?'—':mins(r.gap_min)}</td>
    <td class="note">${esc((r.note||'').slice(0,120))}</td></tr>`).join('');
  return `${back}${dhead}${idb}
    <div class="logh" style="margin-top:16px">activity</div>
    <div class="anote">turns across every loop this agent ran in · click a turn to read its framed prompt</div>
    <div class="stats">${stat('total turns',t.turns)}${stat('loops',t.loops)}${stat('continues',t.continues)}${stat('retries',t.retries)}${stat('avg turn ⌀',mins(t.avg_gap_min))}</div>
    <div class="chips" style="max-width:none;margin-bottom:14px">${statusChips(t.statuses)}</div>
    <div class="logh">per-loop</div>
    <table class="table"><thead><tr><th>loop</th><th class="num">turns</th><th class="num">continues</th><th class="num">retries</th><th class="num">avg ⌀</th></tr></thead><tbody>${perLoop}</tbody></table>
    <div class="logh" style="margin-top:18px">every turn (${(d.turns||[]).length})</div>
    <table class="table"><thead><tr><th>loop</th><th class="num">#</th><th>status</th><th class="num">since prev</th><th>note</th></tr></thead><tbody>${turns}</tbody></table>`;
}
async function openTurnFor(loop,agent,seq){flash('loading turn…',false);
  try{const r=await j('/api/loops/'+encodeURIComponent(loop)+'/turn?agent='+encodeURIComponent(agent)+'&seq='+seq);
    if(r.error)return flash(r.error,true);
    const rep=r.report?(`status: ${r.report.status}\nnote: ${r.report.note||''}`):'(no matching report)';
    modal(`<h2>${esc(agent)} · ${esc(loop)} · turn ${seq}</h2>
      <div class="logh">end-of-turn report</div><div class="pre">${esc(rep)}</div>
      <div class="logh" style="margin-top:12px">framed prompt</div><div class="pre">${esc(r.prompt||'(none)')}</div>
      ${r.transcript_tail?`<div class="logh" style="margin-top:12px">transcript tail</div><div class="pre">${esc(r.transcript_tail)}</div>`:''}`);
  }catch(e){flash('turn load failed: '+e,true)}}

// ── standalone-run launcher: REMOVED (REDESIGN-SPEC §5.1 / rd-create §3.1).
// The legacy standalone-run launcher is retired — everything is a loop now; the
// fast one-agent job is the composer's "Ask one agent" loop-of-1 (§Q1). The
// /api/loops/standalone BACKEND route stays (dev-1 owns it) for direct MCP
// callers; only the UI launcher + its helpers are gone.

// ── clone / save-loop-to-registry ──────────────────────────────────────────────
async function cloneLoop(fromId){
  const nn=prompt('New loop name for the clone of "'+fromId+'":');
  if(!nn)return;
  const res=await post('/api/loops/clone',{from_id:fromId,new_name:nn});
  if(res&&res.error)return flash(res.error,true);
  flash('cloned → '+res.name,false);
  await tickList();
  view='loops';sel=res.name;selHost='local';
  _writeUrl('/loops/'+encodeURIComponent(res.name));render();pick(res.name,'local');
}
async function saveToRegistry(){if(!detail)return;const note=prompt('Save loop "'+sel+'" to the registry — optional note:');if(note===null)return;
  const save=await post('/api/loops/'+encodeURIComponent(sel)+'/action',{action:'save_registry',note});
  if(save&&save.error)flash(save.error,true);else{flash('saved "'+sel+'" to registry',false);loadSaved().then(render)}}

// ── in-dashboard loop editor — recursive (agents + sub-loops + parallel groups) ──
let ed=null, edReg=[], edEditing=false, edCollapsed=new Set(), _uidc=0;
const uid=()=>'n'+(++_uidc);
function blankNode(){return {uid:uid(),name:'',goal:'',turnLimit:30,steps:[],_budget:{},projectId:'',origin:''}}
function toNode(c){const steps=c.steps||{};const seq=[],seen=new Set();
  const push=(id,par)=>{if(steps[id]&&!seen.has(id)){seq.push({id,par});seen.add(id)}};
  (c.stepOrder||Object.keys(steps)).forEach(e=>{if(typeof e==='string')push(e,false);else if(Array.isArray(e))e.forEach((id,i)=>push(id,i>0))});
  Object.keys(steps).forEach(id=>push(id,false));
  const arr=seq.map(({id,par})=>{const s=steps[id]||{};
    if(s.type==='loop')return {uid:uid(),kind:'loop',id,par,loop:toNode(s.loop||{})};
    return {uid:uid(),kind:'agent',id,par,role:s.role||'worker',personality:s.personality||'',goal:s.goal||'',maxTurnMinutes:s.maxTurnMinutes,_raw:s}});
  return {uid:uid(),name:c.name||'',goal:c.goal||'',turnLimit:(c.budget&&c.budget.turnLimit)||30,steps:arr,_budget:c.budget||{}}}
function fromNode(node){const steps={},order=[];
  for(const st of node.steps){const id=(st.id||'').trim();if(!id)continue;
    if(st.kind==='loop')steps[id]={type:'loop',loop:fromNode(st.loop)};
    else{const base=Object.assign({},st._raw||{});delete base.type;
      const a=Object.assign(base,{type:'agent',role:st.role,personality:st.personality,goal:st.goal});
      if(st.maxTurnMinutes)a.maxTurnMinutes=parseInt(st.maxTurnMinutes)||undefined; else delete a.maxTurnMinutes;
      steps[id]=a}
    if(st.par&&order.length){const last=order[order.length-1];
      if(Array.isArray(last))last.push(id);else order[order.length-1]=[last,id]}
    else order.push(id)}
  return {name:(node.name||'').trim(),goal:node.goal,budget:Object.assign({},node._budget||{},{turnLimit:parseInt(node.turnLimit)||30}),steps,stepOrder:order}}
function findNode(uid,node){node=node||ed;if(!node)return null;if(node.uid===uid)return node;
  for(const st of node.steps)if(st.kind==='loop'){const f=findNode(uid,st.loop);if(f)return f}return null}
function syncTree(node){if(!node)return;const g=id=>{const e=document.getElementById(id);return e?e.value:undefined};let v;
  if((v=g('f_'+node.uid+'_name'))!==undefined)node.name=v;
  if((v=g('f_'+node.uid+'_goal'))!==undefined)node.goal=v;
  if((v=g('f_'+node.uid+'_turn'))!==undefined)node.turnLimit=parseInt(v)||node.turnLimit;
  node.steps.forEach(st=>{
    if((v=g('f_'+st.uid+'_id'))!==undefined)st.id=v;
    if(st.kind==='agent'){
      if((v=g('f_'+st.uid+'_role'))!==undefined)st.role=v;
      if((v=g('f_'+st.uid+'_pers'))!==undefined)st.personality=v;
      if((v=g('f_'+st.uid+'_goal'))!==undefined)st.goal=v;
      if((v=g('f_'+st.uid+'_min'))!==undefined)st.maxTurnMinutes=v?parseInt(v):undefined;
    }else syncTree(st.loop)})}
function edToggle(u){if(edCollapsed.has(u))edCollapsed.delete(u);else edCollapsed.add(u);renderEditor()}
function addAgent(nodeUid){syncTree(ed);const n=findNode(nodeUid);if(!n)return;
  const hasMgr=n.steps.some(s=>s.kind==='agent'&&s.role==='manager');
  n.steps.push({uid:uid(),kind:'agent',id:'',par:false,role:hasMgr?'worker':'manager',personality:'',goal:''});renderEditor()}
function addSub(nodeUid){syncTree(ed);const n=findNode(nodeUid);if(!n)return;
  const sub=blankNode();sub.steps.push({uid:uid(),kind:'agent',id:'',par:false,role:'manager',personality:'',goal:''});
  n.steps.push({uid:uid(),kind:'loop',id:'',par:false,loop:sub});renderEditor()}
function addFromReg(nodeUid,regId){syncTree(ed);const n=findNode(nodeUid),r=edReg.find(x=>x.id===regId);if(!n||!r)return;
  n.steps.push({uid:uid(),kind:'agent',id:r.id,par:false,role:r.role,personality:r.personality,goal:r.goal});renderEditor()}
function rmStep(nodeUid,i){syncTree(ed);const n=findNode(nodeUid);if(n){n.steps.splice(i,1);renderEditor()}}
function moveStep(nodeUid,i,d){syncTree(ed);const n=findNode(nodeUid);if(!n)return;const k=i+d;if(k<0||k>=n.steps.length)return;
  const t=n.steps[i];n.steps[i]=n.steps[k];n.steps[k]=t;renderEditor()}
function togglePar(nodeUid,i){syncTree(ed);const n=findNode(nodeUid);if(n&&n.steps[i]){n.steps[i].par=!n.steps[i].par;renderEditor()}}
const roleOpt=v=>['manager','worker','input_provider'].map(r=>`<option value="${r}" ${v===r?'selected':''}>${r.replace('_',' ')}</option>`).join('');
function parCell(node,st,i){return i>0
  ?`<span class="edpar ${st.par?'on':''}" onclick="togglePar('${node.uid}',${i})" title="run in parallel with the step above">∥</span>`
  :`<span class="edpar-sp"></span>`}
function mvCell(node,i){return `<span class="edmv"><span onclick="moveStep('${node.uid}',${i},-1)" title="up">↑</span><span onclick="moveStep('${node.uid}',${i},1)" title="down">↓</span><span class="rm" onclick="rmStep('${node.uid}',${i})" title="remove">✕</span></span>`}
function renderAgentStep(node,st,i){return `<div class="edagent">
    <div class="edarow">${parCell(node,st,i)}
      <input class="edin mono" id="f_${st.uid}_id" placeholder="agent id" value="${esc(st.id)}">
      <select class="edin edrole" id="f_${st.uid}_role">${roleOpt(st.role)}</select>
      <input class="edin edmin" id="f_${st.uid}_min" type="number" min="1" placeholder="min" value="${st.maxTurnMinutes||''}" title="max minutes per turn (optional)">
      ${mvCell(node,i)}</div>
    <textarea class="edin" id="f_${st.uid}_pers" rows="2" placeholder="persona — fixed identity: who this agent is / how it thinks">${esc(st.personality)}</textarea>
    <textarea class="edin" id="f_${st.uid}_goal" rows="2" placeholder="goal for this loop">${esc(st.goal)}</textarea>
  </div>`}
function renderSubStep(node,st,i){const open=!edCollapsed.has(st.loop.uid);
  return `<div class="edsub">
    <div class="edarow">${parCell(node,st,i)}
      <span class="edsubtoggle" onclick="edToggle('${st.loop.uid}')">${open?'▾':'▸'} ⟲ sub-loop</span>
      <input class="edin mono" id="f_${st.uid}_id" placeholder="step id" value="${esc(st.id)}" title="step id — the key for this sub-loop in the parent">
      ${mvCell(node,i)}</div>
    ${open?`<div class="edsubbody">${renderNode(st.loop,false)}</div>`:''}
  </div>`}
function renderNode(node,isTop){
  const regOpts=(edReg||[]).map(a=>`<option value="${esc(a.id)}">${esc(a.id)} · ${esc(a.role)}</option>`).join('');
  const steps=node.steps.map((st,i)=>st.kind==='loop'?renderSubStep(node,st,i):renderAgentStep(node,st,i)).join('')
    ||'<div class="stone" style="margin:6px 0">empty — add an agent or sub-loop.</div>';
  return `<div class="ednode">
    <label class="edl">${isTop?'name':'sub-loop name'}</label>
    <input class="edin mono" id="f_${node.uid}_name" placeholder="${isTop?'my-loop-01':'sub-loop-name'}" value="${esc(node.name)}" ${isTop&&edEditing?'readonly':''}>
    <label class="edl">${isTop?'loop goal ':'goal '}<span class="stone" style="text-transform:none;letter-spacing:0">— the north star the manager holds</span></label>
    <textarea class="edin" id="f_${node.uid}_goal" rows="${isTop?3:2}" placeholder="What should this loop achieve?">${esc(node.goal)}</textarea>
    <label class="edl">turn limit</label>
    <input class="edin" id="f_${node.uid}_turn" type="number" min="1" value="${node.turnLimit}" style="width:120px">
    <div class="edl edagenthead">agents &amp; sub-loops <span class="stone" style="text-transform:none;letter-spacing:0">— order = one round · ∥ = parallel with the step above</span>
      <span class="spacer" style="flex:1"></span>
      ${regOpts?`<select class="edin regpick" data-node="${node.uid}"><option value="">＋ from registry…</option>${regOpts}</select>`:''}
      <button class="ibtn" onclick="addAgent('${node.uid}')" title="add a blank agent">＋ agent</button>
      <button class="ibtn" onclick="addSub('${node.uid}')" title="add a nested sub-loop">＋ sub-loop</button></div>
    ${steps}
  </div>`}
// D-FE (write side): the New-Team front door — confirm a Project + Origin the
// crew targets before composing it. Both OPTIONAL (a loop with neither still
// validates, per owner decision); populated from the already-loaded catalogs. A
// stored origin the current list doesn't know (e.g. a host_class) stays
// selectable so editing never drops it. Writes the canonical `projectId`; the
// schema still emits `productId` in lock-step so old readers are unaffected.
function renderBindings(){
  const prods=(projects||[]),origs=(origins||[]);
  const pOpts=['<option value="">(no project — unlinked)</option>']
    .concat(prods.map(p=>`<option value="${esc(p.id)}" ${ed.projectId===p.id?'selected':''}>${esc(p.id)}${p.name&&p.name!==p.id?' — '+esc(p.name):''}</option>`)).join('');
  const oOpts=['<option value="">(any origin — decide at run)</option>']
    .concat(origs.map(o=>`<option value="${esc(o.id)}" ${ed.origin===o.id?'selected':''}>${esc(o.id)}${o.name&&o.name!==o.id?' — '+esc(o.name):''}</option>`)).join('');
  const extraO=(ed.origin&&!origs.some(o=>o.id===ed.origin))?`<option value="${esc(ed.origin)}" selected>${esc(ed.origin)} (custom)</option>`:'';
  return `<div class="edbind">
    <div class="edl">team bindings <span class="stone" style="text-transform:none;letter-spacing:0">— the Project this crew targets and the Origin that runs it (both optional)</span></div>
    <div class="edbindrow">
      <label class="edbl">▦ Project
        <select class="edin" id="ed_projectId" ${prods.length?'':'disabled'}>${pOpts}</select></label>
      <label class="edbl">◉ Origin
        <select class="edin" id="ed_origin">${oOpts}${extraO}</select></label>
    </div>
    ${prods.length?'':'<div class="stone" style="font-size:11px;margin-top:3px">No projects yet — add one on the Projects page to bind this team to a repo.</div>'}
    <div class="nlmsg info" id="ed_bindnote" style="display:none;border:0;padding:6px 0;margin-top:4px"></div>
  </div>`;
}
function renderEditor(){if(!ed)return;
  modal(`<div class="kick" style="margin-bottom:6px">${edEditing?'⟲ Reshape the crew':'⟲ Compose a crew'}</div>
    <h2>${edEditing?'Edit loop':'New loop'}</h2>
    <div class="hint">${edEditing?'edit':'author'} the whole config — agents, nested sub-loops, and parallel groups. A loop is a crew; roles + phases + a shared bus, baked in.</div>
    ${renderBindings()}
    <div class="edform">${renderNode(ed,true)}</div>
    <div class="edfoot">
      <button class="btn ghost" onclick="closeModal()">Cancel</button><span style="flex:1"></span>
      <button class="btn" onclick="saveLoop(false)">Save</button>
      <button class="btn primary" onclick="saveLoop(true)">▶ Save &amp; run</button>
    </div>`);
  document.querySelectorAll('.regpick').forEach(sel=>{sel.onchange=()=>{if(sel.value)addFromReg(sel.dataset.node,sel.value)}});
}
function openEditor(name){edCollapsed=new Set();
  j('/api/loops/agents').then(r=>{edReg=r.agents||[];if(ed)renderEditor()}).catch(()=>{edReg=[]});
  if(name){edEditing=true;ed=blankNode();
    j('/api/loops/'+encodeURIComponent(name)+'/config').then(r=>{
      if(r.error){flash(r.error,true);return}
      const c=r.config||{};ed=toNode(c);
      // carry existing bindings onto the root node (accept either origin spelling)
      ed.projectId=c.projectId||c.productId||'';ed.origin=c.origin||c.host_class||'';
      renderEditor()});
  }else{edEditing=false;ed=blankNode();ed.steps.push({uid:uid(),kind:'agent',id:'',par:false,role:'manager',personality:'',goal:''})}
  renderEditor();
}
// Runtime capability probe: does the REAL schema RETAIN projectId (i.e. has the
// Phase C/D-schema landed)? We validate a minimal probe config through the same
// gate the save uses and check whether the normalized config keeps the binding.
// Cached. This keeps the New-Team binding forward-compatible + HONEST: it binds
// for real the moment the schema supports it, and says so plainly until then —
// no client-side re-implementation of the schema, no silent dropped keys.
let _bindSupport=null;
async function schemaBindingsSupported(){
  if(_bindSupport!==null)return _bindSupport;
  const probe={name:'__bindprobe__',goal:'probe',
    steps:{m:{type:'agent',role:'manager',personality:'p',goal:'g'}},stepOrder:['m'],
    projectId:'__p__',origin:'__o__'};
  try{const r=await post('/api/loops/creator/validate',{config:probe});
    // Schema keeps projectId + productId in lock-step; accept either spelling in
    // the normalized echo so the probe survives the rename in both directions.
    _bindSupport=!!(r&&r.ok&&r.config&&(r.config.projectId==='__p__'||r.config.productId==='__p__'));
  }catch(e){_bindSupport=false}
  return _bindSupport;
}
function _readBindings(){
  const p=(document.getElementById('ed_projectId')||{}).value||'';
  const o=(document.getElementById('ed_origin')||{}).value||'';
  ed.projectId=p;ed.origin=o;return {p,o};
}
async function saveLoop(run){syncTree(ed);const cfg=fromNode(ed);
  if(!cfg.name)return flash('name required',true);
  if(!cfg.stepOrder.length)return flash('add at least one agent (with an id)',true);
  // Bind Project/Origin if chosen AND the live schema will keep them; otherwise
  // save the team unbound and tell the owner the binding lands with the schema.
  const {p,o}=_readBindings();let bindDeferred=false;
  if(p||o){
    if(await schemaBindingsSupported()){
      if(p)cfg.projectId=p;
      if(o)cfg.origin=o;
    }else{bindDeferred=true;}
  }
  const res=await post('/api/loops/save',{config:cfg});
  if(res&&res.error)return flash(res.error+(res.errors&&res.errors.length?': '+res.errors.join('; '):''),true);
  const bindMsg=bindDeferred?' · project/origin binding activates when the registry schema update ships (saved unbound for now)'
    :((p||o)?' · bound '+[p?('▦ '+p):'',o?('◉ '+o):''].filter(Boolean).join(' '):'');
  flash('saved loop "'+res.name+'"'+(res.warnings&&res.warnings.length?' · '+res.warnings.length+' warning(s)':'')+bindMsg,false);
  if(run){const r2=await post('/api/loops/'+encodeURIComponent(res.name)+'/action',{action:'start'});
    if(r2&&r2.error)flash('saved, but start failed: '+r2.error,true);else flash('▶ running "'+res.name+'"',false)}
  closeModal();await tickList();
  view='loops';sel=res.name;selHost='local';_writeUrl('/loops/'+encodeURIComponent(res.name));render();pick(res.name,'local');}

function modal(html){document.getElementById('modalRoot').innerHTML=`<div class="ovl" onclick="if(event.target===this)closeModal()">
  <div class="modal"><span class="x" onclick="closeModal()">×</span>${html}</div></div>`}
function closeModal(){document.getElementById('modalRoot').innerHTML=''}
// D6 — "Add origin" is a MIRROR, not a vault. The web NEVER takes a key: it
// shows the one host-side command to run. `yard connect <host>` on the box
// writes the origin; this list polls and the new card appears within a cycle.
// There is deliberately no input field here — the box is the source of truth.
function openAddOrigin(){
  modal(`<div class="kick" style="margin-bottom:6px">◉ Add an origin</div>
    <h2 style="margin:.15em 0 .1em">Add it on the box — not here</h2>
    <div class="hint">Origins are added host-side. Loopyard never asks for a key, token, or password — your credentials stay on the box. Run this on the machine you want to add:</div>
    <div class="cmdbox"><code id="yardcmd">yard connect &lt;host&gt;</code>
      <button class="btn ghost" onclick="copyCmd('yard connect <host>',this)" title="Copy to clipboard" aria-label="Copy the yard connect command">Copy</button></div>
    <div class="hint" style="margin-top:10px">Swap <code>&lt;host&gt;</code> for a name for the box (e.g. <code>yard connect anneke</code>); add <code>--address&nbsp;user@host</code> if it isn't reachable yet. Once it runs, the origin appears here within a few seconds — no web-side secret entry, ever.</div>
    <div style="display:flex;gap:8px;margin-top:12px">
      <button class="btn primary" onclick="refreshOriginsNow(this)" title="Re-read origins now — the new box appears as soon as it has connected">Refresh origins now</button>
      <button class="btn ghost" onclick="closeModal()">Close</button>
    </div>`);
}
// §3.3 — complete the add gesture WITHOUT a full page reload: after the user runs
// the host command, one tap re-reads the origins list and re-renders, so the new
// origin surfaces on demand (the 30s poll would get it too — this just makes the
// gesture end where the user is looking). Honest: it shows what the box reports.
async function refreshOriginsNow(btn){
  if(btn){var o=btn.textContent;btn.textContent='Refreshing…';btn.disabled=true;}
  await loadOrigins();
  if(btn){btn.textContent=o;btn.disabled=false;}
  const n=(origins||[]).length;
  flash(n+' origin'+(n===1?'':'s')+' visible',false);
  if(view==='origins')renderOrigins();
}
function copyCmd(text,btn){
  const done=()=>{if(btn){const o=btn.textContent;btn.textContent='Copied ✓';setTimeout(()=>{btn.textContent=o},1600)}flash('command copied',false)};
  try{
    if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(done,()=>_copyFallback(text,done))}
    else{_copyFallback(text,done)}
  }catch(e){_copyFallback(text,done)}
}
function _copyFallback(text,done){
  try{const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';
    document.body.appendChild(ta);ta.focus();ta.select();document.execCommand('copy');ta.remove();done()}
  catch(e){flash('copy failed — select the command and copy it manually',true)}
}

// ── data pumps ─────────────────────────────────────────────────────────────────
async function pick(n,h){sel=n;selHost=h;teamRoom=null;
  if(view==='loops'){renderLoops();
    try{detail=await j('/api/loops/'+encodeURIComponent(n)+'?host='+encodeURIComponent(h||'local')+'&tail=150');}catch(e){}
    await fetchTeamRoom(n,h);   // §5.3 — the team-room re-composition (null → legacy stats)
    if(sel===n&&detail)renderDetail(detail);}
}
// Ambient tab title: put the running-loop count in the browser tab so an owner
// watching a long run can glance at their tab bar. Falls back to plain
// "Loopyard" when nothing is running or the fetch fails. Anti-flicker: only
// updated when the number actually changes.
let _lastTabTitle='';
function _tabTitle(nRunning){
  const t=nRunning>0?`▶ ${nRunning} · Loopyard`:'Loopyard — own the loop';
  if(t===_lastTabTitle)return;
  _lastTabTitle=t;document.title=t;
}
// The top-bar scope label — computed from in-memory state so it can be repainted
// WITHOUT a refetch (uxui#1/#2: switching the project must move the top-bar count
// in lockstep with the rail + body, never leave a stale "59 loops" contradicting
// the rescoped "8"). `visibleLoops()` already honors the active project.
function tickScopeText(){
  return view==='loops'?`${visibleLoops().length} loop${visibleLoops().length===1?'':'s'} · ${hosts.length} origin${hosts.length===1?'':'s'}`
    :view==='agents'?`${regAgents.length} role${regAgents.length===1?'':'s'}${curAgent?' · '+curAgent:''}`
    :view==='sessions'?`${(sessionsRoster||[]).length} session${(sessionsRoster||[]).length===1?'':'s'}`
    :view==='origins'?originCountLabel()
    :view==='projects'?`${projects.length} project${projects.length===1?'':'s'}`
    :'';
}
function paintTick(){const el=document.getElementById('tick');if(!el)return;
  const scope=tickScopeText();
  el.textContent=(scope?scope+' · ':'')+(hosts||['local']).join(', ')+' · '+new Date().toTimeString().slice(0,8);}
async function tickList(){try{const r=await j('/api/loops');loops=r.loops||[];hosts=r.hosts||['local'];loadErr.loops=(r&&r.error)||'';
  const nRunning=loops.filter(d=>RUNNING.has(d.state)).length;
  _tabTitle(nRunning);
  paintTick();
  if(view==='loops')renderLoops();else renderNav();
  }catch(e){loadErr.loops=''+e;document.getElementById('tick').textContent='refresh failed';_tabTitle(0);if(view==='loops')renderLoops()}}
async function tickDetail(){if(view==='loops'&&sel){const n=sel,h=selHost||'local';
  try{detail=await j('/api/loops/'+encodeURIComponent(n)+'?host='+encodeURIComponent(h)+'&tail=150');}catch(e){}
  await fetchTeamRoom(n,h);   // §5.3 — refresh the roster/convergence/turn-bar each tick
  if(sel===n&&detail)renderDetail(detail);}}

async function loadOrigins(){try{const r=await j('/api/loops/origins');origins=(r&&r.origins)||[];loadErr.origins=(r&&r.error)||''}catch(e){origins=[];loadErr.origins=''+e}}
// Per-origin authed-CLI capability cache — keyed by origin id, value is the
// loop_origin_capabilities response ({ok, cliCapabilities:[{cli,authed,applied,note}], reason?}).
// Fetch is lazy (only when the Origins view or the launcher needs it) and best-effort:
// a fetch failure leaves the entry undefined so the UI can render an honest "unprobed" chip.
let capsByOrigin={};
async function loadOriginCaps(id){
  if(!id)return null;
  if(capsByOrigin[id])return capsByOrigin[id];
  try{const r=await j('/api/loops/origin/'+encodeURIComponent(id)+'/capabilities');
    capsByOrigin[id]=r||{ok:false,cliCapabilities:[]};
    return capsByOrigin[id];
  }catch(e){capsByOrigin[id]={ok:false,cliCapabilities:[],reason:'probe failed: '+e.message};return capsByOrigin[id]}
}
// Fetch caps for every visible origin (parallel), then re-render. Used by Origins page
// so the honest per-CLI chips paint after the first frame instead of blocking it.
async function loadAllOriginCaps(){
  const ids=(origins||[]).map(o=>o.id).filter(Boolean);
  await Promise.all(ids.map(id=>loadOriginCaps(id)));
  if(view==='origins')renderOrigins();
}
// Map a model id → the CLI that would run it. Today claude-* → claude; codex-* → codex.
// Unknown prefixes return '' so the launcher treats them as always-allowed (honest: we
// only gate when we KNOW which CLI a model belongs to). MUST stay in lockstep with
// mcp_loops.origins_probe.model_cli — the server enforces the SAME rule, and if the two
// disagree the launcher would either lie ("you can run this") or nag ("we don't
// support this yet"). Any change here needs a matching server-side change.
function _modelCli(m){if(!m)return '';const s=(''+m).toLowerCase();if(s.startsWith('claude'))return 'claude';if(s.startsWith('codex'))return 'codex';return ''}
// Given an origin's capability list, return the set of CLIs with authed===true.
// Kept as a convenience for chip rendering ONLY — the launcher gate goes through
// _canRunOnOrigin below so it matches origins_probe.can_run's contract exactly.
function _authedClis(caps){const out=new Set();(caps&&caps.cliCapabilities||[]).forEach(c=>{if(c.authed===true)out.add(c.cli)});return out}
// Client twin of origins_probe.can_run — same rule, same honesty invariants. Returns
// {canRun,reason,cli}. Kept small and pure so the two sides can be diffed by eye.
// * Model with no known CLI mapping → allowed (we don't invent gates).
// * Capability record ok:false → allowed with reason (remote not-wired etc.).
// * CLI authed:true → allowed. authed:"unknown" → allowed w/ caveat. authed:false → refused.
// * CLI record missing from the probe list → refused ("no probe reported").
function _canRunOnOrigin(rec,model){
  const cli=_modelCli(model); if(!cli)return {canRun:true,reason:'',cli:null};
  if(!rec)return {canRun:true,reason:'no capability record — model gate skipped',cli};
  if(rec.ok===false)return {canRun:true,reason:rec.reason||'',cli};
  const caps=rec.cliCapabilities||[];
  const entry=caps.find(c=>c&&c.cli===cli);
  if(!entry)return {canRun:false,reason:'origin did not report a probe for '+cli,cli};
  if(entry.authed===true)return {canRun:true,reason:'',cli};
  if(entry.authed==='unknown')return {canRun:true,reason:'authed for '+cli+' is unknown on this origin',cli};
  return {canRun:false,reason:'origin is not authed for '+cli+' — '+(entry.note||''),cli};
}
async function loadProjects(){try{const r=await j('/api/loops/projects');projects=(r&&(r.projects||r.products))||[];projectsGathered=(r&&r.gathered)||null;loadErr.projects=(r&&r.error)||''}catch(e){projects=[];projectsGathered=null;loadErr.projects=''+e}}
async function loadAnalytics(){try{const r=await j('/api/loops/analytics');regAgents=(r&&r.agents)||[];loadErr.agents=(r&&r.error)||''}catch(e){regAgents=[];loadErr.agents=''+e}}
async function loadSaved(){try{const r=await j('/api/loops/registry');regSavedAgents=(r&&r.agents)||[];regSavedLoops=(r&&r.loops)||[]}catch(e){regSavedAgents=[];regSavedLoops=[]}}

// Rehydrate everything except the currently-open loop detail; called on the slow
// background poll (below) and on demand by the topbar refresh button. Skipped
// while a modal is open so the user's edits don't get yanked out from under them.
async function refreshAll(){
  if(document.getElementById('modalRoot').innerHTML)return;
  await Promise.all([tickList(),loadOrigins(),loadProjects(),loadAnalytics(),loadSaved()]);
  render();
}
// ── Keyboard shortcuts ────────────────────────────────────────────────────────
// A g-prefixed nav scheme (gr/gl/ga/go/gp), '/' to focus the visible search,
// 'n' to open the launcher, '?' for a cheat sheet. Text-input focus is a hard
// bypass so typing never triggers a shortcut. Chord state ('g' pressed)
// auto-expires after 900ms so a stray g never leaves the app in a weird mode.
let _gChord=false, _gChordT=0;
const _KEY_NAV={l:'loops',h:'hub',a:'agents',o:'origins',p:'projects'};
function _inTextField(t){if(!t)return false;const tag=(t.tagName||'').toLowerCase();
  return tag==='input'||tag==='textarea'||tag==='select'||t.isContentEditable}
function showShortcutsHelp(){
  modal(`<div class="kick" style="margin-bottom:6px">⌘ Keys</div>
    <h2>Keyboard shortcuts</h2>
    <div class="hint">Fast paths for owners who live in this dashboard. Text inputs still swallow every keystroke; these fire only when nothing else has focus.</div>
    <table class="table" style="margin-top:10px"><tbody>
      <tr><td><span class="kbd">g</span> <span class="stone" style="font-size:11px">then</span> <span class="kbd">l</span></td><td>Loops</td></tr>
      <tr><td><span class="kbd">g</span> <span class="stone" style="font-size:11px">then</span> <span class="kbd">a</span></td><td>Agents</td></tr>
      <tr><td><span class="kbd">g</span> <span class="stone" style="font-size:11px">then</span> <span class="kbd">o</span></td><td>Origins</td></tr>
      <tr><td><span class="kbd">g</span> <span class="stone" style="font-size:11px">then</span> <span class="kbd">p</span></td><td>Projects</td></tr>
      <tr><td><span class="kbd">g</span> <span class="stone" style="font-size:11px">then</span> <span class="kbd">t</span></td><td>Terminal</td></tr>
      <tr><td><span class="kbd">/</span></td><td>Focus the visible search box (Loops or Agents)</td></tr>
      <tr><td><span class="kbd">n</span></td><td>Open the <b>＋ Loop</b> creator</td></tr>
      <tr><td><span class="kbd">r</span></td><td>Refresh everything now</td></tr>
      <tr><td><span class="kbd">?</span></td><td>This help</td></tr>
      <tr><td><span class="kbd">Esc</span></td><td>Close a modal / cancel a <span class="kbd">g</span>-chord</td></tr>
    </tbody></table>`);
}
function _focusVisibleSearch(){
  const el=document.getElementById('lp_search')||document.getElementById('ag_search');
  if(el){el.focus();try{const n=el.value.length;el.setSelectionRange(n,n)}catch(e){};return true}
  return false;
}
document.addEventListener('keydown',(ev)=>{
  // Never steal a native cmd/ctrl/alt shortcut, and never fire while typing.
  if(ev.metaKey||ev.ctrlKey||ev.altKey)return;
  if(ev.key==='Escape'){
    // Close a modal first; failing that just clear the g-chord.
    if(document.getElementById('modalRoot').innerHTML){closeModal();ev.preventDefault();return}
    _gChord=false;return;
  }
  if(_inTextField(ev.target))return;
  const now=Date.now();
  // Expire a stale g-chord so pressing g and then leaving the tab open doesn't
  // trap the next real 'r' or 'l' keystroke in nav-mode forever.
  if(_gChord&&now-_gChordT>900)_gChord=false;
  if(_gChord){
    const dest=_KEY_NAV[ev.key.toLowerCase()];
    _gChord=false;
    if(dest){ev.preventDefault();navGo(dest);return}
    return;
  }
  const k=ev.key.toLowerCase();
  if(k==='g'){_gChord=true;_gChordT=now;ev.preventDefault();return}
  if(k==='/'){if(_focusVisibleSearch())ev.preventDefault();return}
  if(k==='n'){ev.preventDefault();navGo('newloop');return}
  if(k==='r'){ev.preventDefault();refreshAll();return}
  if(k==='?'||(k==='/'&&ev.shiftKey)){ev.preventDefault();showShortcutsHelp();return}
});

// showArchived is a per-owner preference — persist it so the checkbox state
// survives reloads and hash-nav (which re-innerHTMLs the sidebar). Wrapped so
// storage-denied browsers degrade to no-persist rather than throwing.
const SHOW_ARCH_KEY='loopyard.showArch';
function _loadShowArch(){try{return localStorage.getItem(SHOW_ARCH_KEY)==='1'}catch(e){return false}}
function _saveShowArch(v){try{localStorage.setItem(SHOW_ARCH_KEY,v?'1':'0')}catch(e){}}
function setShowArch(checked){_saveShowArch(!!checked);render()}

// ══ TOOLS: web Terminal + +Loop creator (Phase A) ═══════════════════════════
// Ported from the standalone SPA (static/index.html) into the workspace. Wire
// contract with the backend (docs/TERMINAL.md): a gate-authed PTY over
// ws(s)://<host>/ws/terminal — auth is the SAME dash_sess cookie the browser
// sends on the handshake (NO token in the URL). client→server BINARY = raw
// stdin, TEXT = {"type":"resize",cols,rows}; server→client BINARY = raw stdout,
// TEXT = {"type":"exit"|"error",…}. Leaving the page closes the socket, which is
// the backend's teardown signal (no orphaned shells).
//
// PTY LIFECYCLE CONTRACT (critical): start/stop happen ONLY on real navigation —
// via syncToolPanes() called from navGo / popstate / boot — NEVER from render(),
// which fires on the 5s/7s poll and would respawn PTYs, exhausting the backend's
// MAX_TERMINALS. syncToolPanes acts only on an actual view transition, so a poll
// that re-renders (or re-nav to the same tool) leaves the live PTY untouched.
let _toolView=null;                 // which tool pane (terminal|newloop) currently owns a PTY
function syncToolPanes(){
  const want=(view==='terminal'||view==='newloop')?view:null;
  if(want===_toolView)return;       // no transition → leave any live PTY alone
  if(_toolView==='terminal'){stopTerminal();termAttach=null;}  // disarm any Open attach
  else if(_toolView==='newloop')stopNewLoop();
  _toolView=want;
  if(want==='terminal')startTerminal();
  else if(want==='newloop')startNewLoop();
}
const TERM_ENC=new TextEncoder();
let term=null,termFit=null,termWS=null,termLibP=null,termRO=null;
// §rd-sessions "Open": armed with {target} → the next terminal connect attaches to
// that live local tmux session instead of a fresh shell. Cleared on leaving.
let termAttach=null;
function openSessionTerminal(sid){if(!sid)return;termAttach={target:sid};navGo('terminal');}
function loadTermLib(){
  if(termLibP)return termLibP;
  termLibP=new Promise((res,rej)=>{
    if(window.Terminal&&window.FitAddon){res();return;}
    let loaded=0;const need=2;
    const done=()=>{if(++loaded>=need)res();};
    const add=(src)=>{const s=document.createElement("script");s.src=src;
      s.onload=done;s.onerror=()=>rej(new Error("terminal library failed to load"));
      document.head.appendChild(s);};
    // same-origin, CSP-safe (no CDN). Both UMD; load order is independent.
    add("/static/vendor/xterm/xterm.js");
    add("/static/vendor/xterm/addon-fit.js");
  });
  return termLibP;
}
function setTermStat(state,txt){
  const el=document.getElementById("termStat");if(!el)return;
  el.classList.remove("on","off","wait");el.classList.add(state);
  document.getElementById("termStatTxt").textContent=txt;
  const rc=document.getElementById("termReconnect");
  if(rc)rc.style.display=(state==="off")?"":"none";
}
function fitTerm(){if(!term||!termFit)return;try{termFit.fit();sendTermResize();}catch(e){}}
function sendTermResize(){
  if(termWS&&termWS.readyState===1&&term){
    try{termWS.send(JSON.stringify({type:"resize",cols:term.cols,rows:term.rows}));}catch(e){}
  }
}
async function startTerminal(){
  const wrap=document.getElementById("termwrap");if(!wrap)return;
  // Honest termbar hint: say plainly when we're attaching to a named session.
  const hint=document.querySelector('#v-terminal .termhint');
  if(hint)hint.textContent=(termAttach&&termAttach.target)
    ?('Attaching to live tmux session '+termAttach.target+' · local origin · leaving ends the view')
    :'Local origin · a shell on this box · leaving this page ends the session';
  setTermStat("wait","Connecting…");
  try{await loadTermLib();}
  catch(e){setTermStat("off","Terminal library failed to load");return;}
  if(view!=="terminal")return;        // navigated away while the lib loaded
  if(term){try{term.dispose();}catch(e){}term=null;}
  const FitAddon=(window.FitAddon&&window.FitAddon.FitAddon)||window.FitAddon;
  term=new window.Terminal({
    cursorBlink:true,fontSize:13,scrollback:5000,
    fontFamily:'ui-monospace,"JetBrains Mono","SF Mono",Menlo,Consolas,monospace',
    theme:{background:"#0b0f0d",foreground:"#e6ede9",cursor:"#46e0a0",selectionBackground:"#2f9e7755"},
  });
  termFit=new FitAddon();term.loadAddon(termFit);
  const host=document.getElementById("term");host.innerHTML="";term.open(host);
  // keystrokes → raw stdin (binary). Bound once; reads the live socket each time
  // so Reconnect can swap termWS underneath without re-binding.
  term.onData(d=>{if(termWS&&termWS.readyState===1)termWS.send(TERM_ENC.encode(d));});
  requestAnimationFrame(fitTerm);
  if(window.ResizeObserver){termRO=new ResizeObserver(()=>fitTerm());termRO.observe(wrap);}
  const rc=document.getElementById("termReconnect");
  if(rc)rc.onclick=()=>{if(!termWS||termWS.readyState>1)connectTermWS();};
  connectTermWS();
}
function connectTermWS(){
  const proto=location.protocol==="https:"?"wss:":"ws:";
  // §rd-sessions "Open" — when a session id is armed, attach the web terminal to
  // that live LOCAL tmux session (honest: tmux itself reports if it isn't there).
  const q=termAttach&&termAttach.target
    ?("?session=attach&origin=local&target="+encodeURIComponent(termAttach.target)):"";
  let ws;
  try{ws=new WebSocket(proto+"//"+location.host+"/ws/terminal"+q);}
  catch(e){setTermStat("off","Disconnected");return;}
  ws.binaryType="arraybuffer";termWS=ws;setTermStat("wait","Connecting…");
  let opened=false;
  ws.onopen=()=>{opened=true;setTermStat("on","Connected");sendTermResize();if(term)term.focus();};
  ws.onmessage=(ev)=>{
    if(typeof ev.data==="string"){
      let m;try{m=JSON.parse(ev.data);}catch(_){return;}
      if(m.type==="exit"&&term)
        term.write("\r\n\x1b[90m[session ended"+(m.code!=null?" ("+m.code+")":"")+"]\x1b[0m\r\n");
      else if(m.type==="error"&&term)
        term.write("\r\n\x1b[31m["+String(m.message||"error")+"]\x1b[0m\r\n");
      return;
    }
    if(term)term.write(new Uint8Array(ev.data));   // raw stdout
  };
  ws.onerror=()=>{};
  ws.onclose=()=>{
    if(termWS!==ws)return;              // superseded by a reconnect
    termWS=null;
    // never-opened close == handshake refused (bad/absent cookie) → auth hint.
    setTermStat("off",opened?"Disconnected":"Refused — reload to sign in");
  };
}
function stopTerminal(){
  if(termRO){try{termRO.disconnect();}catch(e){}termRO=null;}
  if(termWS){try{termWS.close();}catch(e){}termWS=null;}   // → backend tears down the PTY
  if(term){try{term.dispose();}catch(e){}term=null;}
}
window.addEventListener("beforeunload",()=>{if(termWS){try{termWS.close();}catch(e){}}});

// ── + Loop: split view = config editor (left) + seeded creator terminal (right) ─
// RIGHT is the SAME gate-authed /ws/terminal, but ?session=creator&origin=local so
// the PTY autostarts the interactive loop-creator (seeded with the C1 context).
// Default runtime = claude (owner decision: never codex-autostart). LEFT edits the
// config JSON and VALIDATEs/SAVEs through the REAL endpoints (no client re-impl).
let nlterm=null,nlfit=null,nlWS=null,nlRO=null,nlBuf="",nlValidConfig=null;
// §5.5 — targets attached via Door A (＋ Target: hub docs + issues). The target
// IS the goal (rd-create §3.3): attaching one pre-fills the goal box (editable).
let nlTargets=[];
// §rd-sessions "start a loop from here" — a one-shot seed applied on the next
// composer entry (origin pre-filled to the session as compute + its runtime),
// then cleared so a plain +Loop is never silently pinned to a session.
let nlSeedOrigin=null,nlSeedRuntime=null;
function setNlStat(state,txt){
  const el=document.getElementById("nlTermStat");if(!el)return;
  el.classList.remove("on","off","wait");el.classList.add(state);
  document.getElementById("nlTermStatTxt").textContent=txt;
  const rc=document.getElementById("nlTermReconnect");
  if(rc)rc.style.display=(state==="off")?"":"none";
}
function nlSetMsg(kind,txt){
  const el=document.getElementById("nlMsg");if(!el)return;
  el.className="nlmsg "+kind;el.textContent=txt;
}
function nlFit(){if(!nlterm||!nlfit)return;try{nlfit.fit();nlSendResize();}catch(e){}}
function nlSendResize(){
  if(nlWS&&nlWS.readyState===1&&nlterm){
    try{nlWS.send(JSON.stringify({type:"resize",cols:nlterm.cols,rows:nlterm.rows}));}catch(e){}
  }
}
// §5.5 — entering the composer is CALM: no shell, no JSON up front. We only
// populate the ambient chips (project + origin) and reset the surface. The
// briefing terminal (a claude/codex PTY) is started LAZILY, and only when the
// user opens "Advanced" (nlAdvancedToggle) — the shell is never on the default
// path (rd-create §3.2).
function startNewLoop(){
  nlValidConfig=null;nlSaveState(false);nlTargets=[];
  nlPopulateProjectChip();nlPopulateOriginChip();renderNlTargets();
  // Apply a pending "start a loop from here" seed (session runtime), then repaint.
  if(nlSeedRuntime){const rs=document.getElementById('nlRuntime');if(rs){try{rs.value=nlSeedRuntime;}catch(e){}}}
  // refresh the shared health-aware picker source, then repaint the origin chip so
  // the composer offers the full reachable fleet + (session) compute, not just local.
  loadOriginPicker().then(()=>{if(view==='newloop'){nlPopulateOriginChip();nlApplyOriginSeed();}});
  const ed=document.getElementById("nlEditor");if(ed)ed.value="";
  nlSetMsg("info","Describe a goal or attach a target, then Start.");
  const out=document.getElementById("nlSuggestOut");if(out){out.classList.add("hidden");out.innerHTML="";}
  // If we returned with Advanced already expanded, bring its briefing PTY back up.
  const adv=document.getElementById("nlAdvanced");if(adv&&adv.open)startCreatorTerm();
}
// Lazily bring up the seeded creator PTY — called the first time the Advanced
// disclosure is opened, never on plain composer entry.
async function startCreatorTerm(){
  if(nlterm||nlWS)return;              // already up
  // surface which context the creator is seeded with (transparency for self-hosters)
  try{
    const p=await j("/api/loops/creator/prompt");
    const docs=(p&&p.context_docs)||[];
    nlSetMsg("info","Briefing terminal seeded with context: "+(docs.length?docs.join(", "):"(default preprompt)")+
             ". Converse below; Apply its ```json config into the editor, then Validate + Save.");
  }catch(e){}
  const wrap=document.getElementById("nltermwrap");if(!wrap)return;
  setNlStat("wait","Connecting…");
  try{await loadTermLib();}
  catch(e){setNlStat("off","Terminal library failed to load");return;}
  if(view!=="newloop")return;          // navigated away while the lib loaded
  if(nlterm){try{nlterm.dispose();}catch(e){}nlterm=null;}
  const FitAddon=(window.FitAddon&&window.FitAddon.FitAddon)||window.FitAddon;
  nlterm=new window.Terminal({
    cursorBlink:true,fontSize:13,scrollback:6000,
    fontFamily:'ui-monospace,"JetBrains Mono","SF Mono",Menlo,Consolas,monospace',
    theme:{background:"#0b0f0d",foreground:"#e6ede9",cursor:"#46e0a0",selectionBackground:"#2f9e7755"},
  });
  nlfit=new FitAddon();nlterm.loadAddon(nlfit);
  const host=document.getElementById("nlterm");host.innerHTML="";nlterm.open(host);
  nlterm.onData(d=>{if(nlWS&&nlWS.readyState===1)nlWS.send(TERM_ENC.encode(d));});
  requestAnimationFrame(nlFit);
  if(window.ResizeObserver){nlRO=new ResizeObserver(()=>nlFit());nlRO.observe(wrap);}
  const rc=document.getElementById("nlTermReconnect");
  if(rc)rc.onclick=()=>{if(!nlWS||nlWS.readyState>1)connectNlWS();};
  connectNlWS();
}
function connectNlWS(){
  const proto=location.protocol==="https:"?"wss:":"ws:";
  const rt=(document.getElementById("nlRuntime")||{}).value||"claude";
  nlBuf="";
  let ws;
  // creator session on the LOCAL origin — same gate-authed /ws/terminal, no token.
  try{ws=new WebSocket(proto+"//"+location.host+"/ws/terminal?session=creator&origin=local&runtime="+encodeURIComponent(rt));}
  catch(e){setNlStat("off","Disconnected");return;}
  ws.binaryType="arraybuffer";nlWS=ws;setNlStat("wait","Connecting…");
  let opened=false;
  ws.onopen=()=>{opened=true;setNlStat("on","Connected");nlSendResize();if(nlterm)nlterm.focus();};
  ws.onmessage=(ev)=>{
    if(typeof ev.data==="string"){
      let m;try{m=JSON.parse(ev.data);}catch(_){return;}
      if(m.type==="exit"&&nlterm)
        nlterm.write("\r\n\x1b[90m[session ended"+(m.code!=null?" ("+m.code+")":"")+"]\x1b[0m\r\n");
      else if(m.type==="error"&&nlterm)
        nlterm.write("\r\n\x1b[31m["+String(m.message||"error")+"]\x1b[0m\r\n");
      return;
    }
    const u=new Uint8Array(ev.data);
    if(nlterm)nlterm.write(u);
    // accumulate decoded stdout so Apply can lift the creator's config block
    try{nlBuf+=new TextDecoder().decode(u);if(nlBuf.length>200000)nlBuf=nlBuf.slice(-200000);}catch(e){}
  };
  ws.onerror=()=>{};
  ws.onclose=()=>{
    if(nlWS!==ws)return;
    nlWS=null;
    setNlStat("off",opened?"Disconnected":"Refused — reload to sign in");
  };
}
function stopNewLoop(){
  if(nlRO){try{nlRO.disconnect();}catch(e){}nlRO=null;}
  if(nlWS){try{nlWS.close();}catch(e){}nlWS=null;}   // → backend tears down the PTY
  if(nlterm){try{nlterm.dispose();}catch(e){}nlterm=null;}
}
// Opening "Advanced" brings up the briefing PTY (Door B live shell); closing it
// tears the PTY down — so a claude/codex session never runs on the default path.
function nlAdvancedToggle(el){
  if(el&&el.open)startCreatorTerm();
  else stopNewLoop();
}

// ── §5.5 composer: ambient chips (project + origin) ──────────────────────────
// The PROJECT chip sets an EXPLICIT, editable projectId at create — the mechanism
// that fixes attribution (a loop born here carries its project, never reverse-
// derived from a git remote). Defaults to the active-project lens; "" = the honest
// Unattributed (no fake bucket). (rd-projects PO Direction 2 / §5.2.)
function nlPopulateProjectChip(){
  const sel=document.getElementById("nlProject");if(!sel)return;
  const P=projects||[];
  const cur=sel.value||((activeProject&&activeProject!==UNATTR)?activeProject:"");
  let opts='<option value=""'+(cur?"":" selected")+'>Unattributed — bind later</option>';
  opts+=P.map(p=>`<option value="${esc(p.id)}" ${cur===p.id?'selected':''}>${esc(p.name||p.id)}</option>`).join("");
  sel.innerHTML=opts;
  if(cur&&P.some(p=>p.id===cur))sel.value=cur;
}
// One shared, health-aware origin picker — the FULL reachable fleet, not just
// `local` (rd-origins: the creator offered only local — fix to the fleet). Consumes
// dev-1's ONE picker source (loop_origin_picker): local first, reachable mirrors/
// live origins next, unreachable ones shown but DISABLED with an honest reason, and
// connected sessions as `(session)` compute. Falls back to the raw origins list if
// the picker source hasn't answered yet, so the composer always works.
async function loadOriginPicker(){
  try{const r=await j('/api/loops/origin-picker');originPickerOpts=(r&&r.options)||[];}
  catch(e){originPickerOpts=[];}
}
// Apply (once) a pending session-compute seed to the origin chip. Honest: only
// sticks if that session is a live, non-disabled option in the shared picker; if
// it isn't reachable we leave the default and clear the seed rather than pin a lie.
function nlApplyOriginSeed(){
  if(!nlSeedOrigin)return;
  const sel=document.getElementById("nlOrigin");
  const seed=nlSeedOrigin;nlSeedOrigin=null;nlSeedRuntime=null;
  if(!sel)return;
  const opt=Array.prototype.find.call(sel.options||[],o=>o.value===seed&&!o.disabled);
  if(opt){sel.value=seed;}
  else{flash('Session '+seed.replace('session:','')+' is not a reachable compute option right now — pick an origin.',true);}
}
function nlPopulateOriginChip(){
  const sel=document.getElementById("nlOrigin");if(!sel)return;
  const cur=sel.value||"local";
  const P=originPickerOpts||[];
  if(P.length){
    // The shared source already orders local→reachable→unreachable→sessions and
    // carries `disabled` + `note` — render it honestly, never silently drop a box.
    sel.innerHTML=P.map(o=>{const id=o.id||o.name;const lbl=o.label||o.name||id;
      const note=o.disabled&&o.note?` — ${o.note}`:'';
      return `<option value="${esc(id)}" ${cur===id?'selected':''} ${o.disabled?'disabled':''}>${esc(lbl)}${esc(note)}</option>`;}).join("");
    if(P.some(o=>(o.id||o.name)===cur&&!o.disabled))sel.value=cur;
    return;
  }
  // fallback: the raw origins list until the picker source lands.
  const O=origins||[];if(!O.length)return;
  let opts="";const seen={};
  for(const o of O){const id=o.id||o.name;if(!id||seen[id])continue;seen[id]=1;
    const isLocal=(o.kind||'').toLowerCase()==='local'||id==='local';
    opts+=`<option value="${esc(id)}" ${cur===id?'selected':''}>${esc(o.name||id)}${isLocal?' · this box':''}</option>`;}
  if(!seen['local'])opts='<option value="local"'+(cur==='local'?' selected':'')+'>local · this box</option>'+opts;
  sel.innerHTML=opts;
}

// ── §5.5 Door A: point at a target (hub docs + issues) ───────────────────────
// The catalog the picker offers: open Idea Hub objectives + hub issues + the
// folded-in loop-failure crashes — scoped to the active project where the record
// carries one (rd-issues: a fault follows the project its loop targets).
function _nlTargetCatalog(){
  // §5.5 Door A — the picker offers this project's Idea Hub docs + open Issues
  // (the new §6 models), scoped to the active project so a target follows its
  // project (rd-issues/rd-ideahub).
  const list=[];
  for(const d of (hubDocs||[]).filter(docInScope))
    list.push({kind:'doc',id:d.id,text:(d.glyph||'·')+' '+(d.title||d.id)});
  const OPEN_WORK=new Set(['open','snoozed','resolving']);
  for(const it of (issues||[]).filter(issueInScope))
    if(OPEN_WORK.has((it.status||'open').toLowerCase()))
      list.push({kind:'issue',id:it.id,text:(it.kind==='crash'?'⚠ ':'')+(it.title||it.id)});
  return list;
}
function renderNlTargets(){
  const el=document.getElementById("nlTargets");if(!el)return;
  el.innerHTML=(nlTargets||[]).map((t,i)=>`<div class="cmptargtag"><span class="tk">${esc(t.kind)}</span>
    <span class="tt">${esc(t.text)}</span>
    <button class="tx" title="remove target" aria-label="remove target" onclick="nlRemoveTarget(${i})">×</button></div>`).join("");
}
function nlRemoveTarget(i){nlTargets.splice(i,1);renderNlTargets();nlValidConfig=null;nlSaveState(false);}
function nlOpenTargetPicker(){
  const cat=_nlTargetCatalog();
  if(!cat.length){modal(`<div class="kick" style="margin-bottom:6px">Door A · point</div>
    <h2>&#65291; Point a loop at a target</h2>
    <div class="hint">No open hub docs or issues to point at yet. Write one in the Idea Hub or file one in Issues, then attach it here — or just describe a goal and Start.</div>
    <div style="margin-top:14px"><button class="btn ghost" onclick="closeModal()">Close</button></div>`);return;}
  const groups=[['doc','Idea Hub docs'],['issue','Issues']];
  let html=`<div class="kick" style="margin-bottom:6px">Door A · point</div>
    <h2>&#65291; Point a loop at a target</h2>
    <div class="hint">Pick a hub doc or an issue — the loop is born pointed at it, and its text pre-fills the goal (editable).</div>
    <div style="margin-top:12px;max-height:52vh;overflow:auto">`;
  for(const [g,label] of groups){
    const items=cat.filter(x=>x.kind===g);if(!items.length)continue;
    html+=`<div class="logh">${label}</div>`;
    html+=items.map(x=>`<div class="clk" style="padding:9px 11px;border:1px solid var(--line);border-radius:6px;margin:5px 0;cursor:pointer"
      onclick="nlAttachTarget('${esc(x.kind)}','${esc(String(x.id))}')">${esc(x.text)}</div>`).join("");
  }
  html+=`</div><div style="margin-top:12px"><button class="btn ghost" onclick="closeModal()">Cancel</button></div>`;
  modal(html);
}
function nlAttachTarget(kind,id){
  const t=_nlTargetCatalog().find(x=>x.kind===kind&&String(x.id)===String(id));
  if(!t){flash('target not found',true);return;}
  if(!nlTargets.some(x=>x.kind===t.kind&&String(x.id)===String(t.id))){
    nlTargets.push(t);renderNlTargets();
    const gi=document.getElementById("nlGoal");
    if(gi){const line=(t.kind==='crash'?'Recover: ':'Point a loop at: ')+t.text;
      gi.value=(gi.value||"").trim()?gi.value.trim()+"\n"+line:line;}
    nlValidConfig=null;nlSaveState(false);
    nlSetMsg("info","Attached a "+t.kind+" — the goal is pre-filled from it (editable). ▶ Start when ready.");
  }
  closeModal();
}

// ── §5.5 the composer's one-shot START (Door A + Door B converge here) ────────
// The goal box is authoritative — targets fold into it on attach. The PROJECT chip
// binds an explicit projectId; "" stays honestly Unattributed (never a fake bucket).
function nlComposedGoal(){const gi=document.getElementById("nlGoal");return (gi&&gi.value||"").trim();}
function nlInjectProject(cfg){
  if(!cfg||typeof cfg!=="object")return cfg;
  const sel=document.getElementById("nlProject");const pid=((sel&&sel.value)||"").trim();
  if(pid){cfg.projectId=pid;}else{delete cfg.projectId;delete cfg.productId;}
  return cfg;
}
function nlDeriveName(goal){
  const s=(goal||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,40).replace(/-+$/,'');
  return 'ask-'+(s||'one');
}
// Validate a composed config through the REAL schema/linter endpoint (never a
// client re-impl) and light the save controls on success.
async function nlValidateConfig(cfg){
  let res;try{res=await post("/api/loops/creator/validate",{config:cfg});}
  catch(e){nlSetMsg("bad","Couldn't build a valid team: "+esc(e.message));return false;}
  if(res&&res.ok){
    nlValidConfig=nlInjectProject(res.config);   // the server keeps projectId; re-assert to be sure
    const ed=document.getElementById("nlEditor");if(ed)try{ed.value=JSON.stringify(nlValidConfig,null,2);}catch(e){}
    nlSaveState(true);return true;
  }
  nlValidConfig=null;nlSaveState(false);
  const errs=(res&&(res.errors||(res.error?[res.error]:null)||(res.raw?[res.raw]:null)))||["invalid config"];
  nlSetMsg("bad","Couldn't build a valid team: "+errs.filter(Boolean).join("; "));return false;
}
// ▶ Start loop: if the power user already validated a config in Advanced, start
// THAT (project-bound); otherwise assemble the team from the goal (suggest →
// inject project → validate) and ignite. Reuses nlSave's proven save+start seam.
async function nlStart(run){
  if(nlValidConfig){
    nlInjectProject(nlValidConfig);
    const ed=document.getElementById("nlEditor");if(ed)try{ed.value=JSON.stringify(nlValidConfig,null,2);}catch(e){}
    return nlSave(run!==false);
  }
  const goal=nlComposedGoal();
  if(!goal){nlSetMsg("bad","Describe a goal, or attach a target with ＋ Target.");const gi=document.getElementById("nlGoal");if(gi)gi.focus();return;}
  nlSetMsg("info","Assembling the team…");
  let res;try{res=await post("/api/loops/creator/suggest",{goal});}
  catch(e){nlSetMsg("bad","Couldn't assemble a team: "+esc(e.message));return;}
  if(!res||!res.ok){
    const errs=(res&&(res.errors||[res.error]))||["could not assemble a team"];
    nlSetMsg("bad","Couldn't assemble a team: "+errs.filter(Boolean).join("; "));return;
  }
  const cfg=nlInjectProject(Object.assign({},res.config));
  if(!(await nlValidateConfig(cfg)))return;
  return nlSave(run!==false);
}
// "Ask one agent" — the loop of one (rd-create §3.5 / §Q1): a single autonomous
// agent that decides when it's done. Built as a one-step config, project-bound,
// through the SAME validate + save+start seam as any loop (no resurrected "Run").
async function nlAskOne(){
  const goal=nlComposedGoal();
  if(!goal){nlSetMsg("bad","Describe what the one agent should do, or attach a target.");const gi=document.getElementById("nlGoal");if(gi)gi.focus();return;}
  nlSetMsg("info","Standing up a loop of one…");
  const cfg=nlInjectProject({
    name:nlDeriveName(goal),goal:goal,
    steps:{solo:{type:"agent",role:"manager",
      personality:"A pragmatic solo agent who drives the goal to done and decides when the work is complete.",
      goal:goal}},
    stepOrder:["solo"]});
  if(!(await nlValidateConfig(cfg)))return;
  return nlSave(true);
}
// ── REDESIGN-SPEC §5.5 Door B — the NATIVE in-browser briefing chat ───────────
// One surface, three entries: the composer's "Brief the team" (type a goal), a
// running loop's ✎ Brief (seeds the loop's saved goal, phase 'brief'), and its
// ↩ Debrief (phase 'debrief'). Talks to dev-1's daemon-free loop_creator_brief:
// the Creator asks 1-2 MATERIAL questions while a LIVE team preview builds = the
// config; answer, and the questions narrow + the preview refines. On done (or any
// time a preview exists) the config is startable through the SAME validate→save→
// start seam as the composer — no resurrected "Run", no shell, works on the
// daemon-less isolated stack (this is what replaces the /brief,/debrief 404 shell).
let briefState=null;
function briefSetMsg(kind,txt){const m=document.getElementById('briefMsg');if(m){m.className='briefmsg '+(kind||'info');m.textContent=txt||'';}}
async function openBriefChat(opts){
  opts=opts||{};
  briefState={name:opts.name||'',goal:opts.goal||'',phase:opts.phase||'brief',
              projectId:opts.projectId||'',answers:{},turn:null,log:[]};
  const bs=briefState;
  const title=bs.phase==='debrief'?'Debrief the manager':(bs.name?'Brief the manager':'Brief a team');
  modal(`<div class="kick" style="margin-bottom:4px">✎ ${esc(title)}${bs.name?' — '+esc(bs.name):''}</div>
    <div class="briefsub">${bs.phase==='debrief'
      ?'Capture or redirect the last run — reshape the team, then Run re-adopts it.'
      :(bs.name?'A live conversation that reshapes this loop’s goal + crew. Run re-adopts the result.'
              :'Answer a question or two and a team preview builds live — start it when it looks right.')}</div>
    <div id="briefBody" class="briefbody" aria-live="polite"></div>
    <div id="briefMsg" class="briefmsg info"></div>`);
  await briefTurn();
}
async function briefTurn(){
  const bs=briefState;if(!bs)return;
  briefSetMsg('info','Thinking…');
  const payload={phase:bs.phase,answers:bs.answers};
  if(bs.name)payload.name=bs.name; else payload.goal=bs.goal;
  if(bs.projectId)payload.projectId=bs.projectId;
  let r;try{r=await post('/api/loops/creator/brief',payload);}
  catch(e){briefSetMsg('bad','Briefing failed: '+esc(e.message)+' — you can still build a team from Advanced in the composer.');return;}
  if(!r||r.ok===false){briefSetMsg('bad','Briefing failed: '+esc((r&&r.error)||'no response from the creator'));return;}
  bs.turn=r;if(r.goal)bs.goal=r.goal;
  // record the creator's turn in the transcript (its note is the plain-language line)
  bs.log.push({who:'creator',text:r.note||(r.done?'Looks complete — review the team and start.':'Answer these and the team preview updates.')});
  renderBriefChat();
  briefSetMsg('info','');
}
function briefPreviewHtml(){
  const bs=briefState;const p=bs&&bs.turn&&bs.turn.preview;
  if(!p||!p.config)return'';
  const roles=(p.roles||[]).map(r=>
    `<li><span class="rid">${esc(r.id||'')}</span><span class="rwhy">${esc(r.why||r.role||'')}</span></li>`).join('');
  const solo=bs.turn.single_agent;
  const n=(p.roles||[]).length;
  const head=solo?'loop of one':(n?n+'-agent team':'team preview');
  return `<div class="bprev"><div class="bprevhd">live team preview · <b>${esc(head)}</b></div>
    ${roles?`<ul class="bprevroles">${roles}</ul>`:'<div class="hint">assembling…</div>'}</div>`;
}
function renderBriefChat(){
  const bs=briefState;const body=document.getElementById('briefBody');if(!body||!bs)return;
  const r=bs.turn||{};
  const log=bs.log.map(e=>`<div class="bmsg ${e.who}"><span class="bwho">${e.who==='creator'?'creator':'you'}</span><span class="btxt">${esc(e.text)}</span></div>`).join('');
  const qs=(r.questions||[]);
  const form=qs.map(q=>`<label class="bq"><span class="bql">${esc(q.question||q.key)}</span>
    <input class="bqi" id="bq_${esc(q.key)}" data-k="${esc(q.key)}" autocomplete="off"
       placeholder="${esc(q.why?('— '+q.why):'your answer')}"
       onkeydown="if(event.key==='Enter'){event.preventDefault();briefSend();}"></label>`).join('');
  const hasPrev=!!(r.preview&&r.preview.config);
  const startLabel=bs.name?(bs.phase==='debrief'?'✓ Save & re-adopt':'✓ Save changes'):'▶ Start loop';
  body.innerHTML=
    `<div class="bchat">${log}</div>`
    +briefPreviewHtml()
    +`<div class="bform">${form}</div>`
    +`<div class="bacts">`
      +(qs.length?`<button class="btn primary" onclick="briefSend()">${bs.name?'Send':'Send answers'}</button>`:'')
      +(hasPrev?`<button class="btn ${qs.length?'ghost':'primary lg'}" onclick="briefStartFromChat()" title="${bs.name?'Save this reshaped team; Run re-adopts it':'Assemble and start this team now'}">${startLabel}</button>`:'')
      +`<button class="btn ghost" onclick="closeModal()">Close</button>`
    +`</div>`;
  const first=body.querySelector('.bqi');if(first)first.focus();
}
function briefSend(){
  const bs=briefState;if(!bs)return;
  const inputs=document.querySelectorAll('#briefBody .bqi');
  const parts=[];
  inputs.forEach(el=>{const k=el.getAttribute('data-k');const v=(el.value||'').trim();if(k&&v){bs.answers[k]=v;parts.push(v);}});
  if(!parts.length){briefSetMsg('bad','Type an answer (or hit Close / Start to go with the current team).');return;}
  bs.log.push({who:'you',text:parts.join(' · ')});
  briefTurn();
}
async function briefStartFromChat(){
  const bs=briefState;
  const cfg0=bs&&bs.turn&&bs.turn.preview&&bs.turn.preview.config;
  if(!cfg0){briefSetMsg('bad','No team preview to start yet — answer the goal first.');return;}
  // Door B (new loop): bind the composer's project chip. Name-seeded brief/debrief:
  // respect the loop's OWN saved project (the preview already carries it).
  let cfg=Object.assign({},cfg0);
  if(!bs.name)cfg=nlInjectProject(cfg);
  briefSetMsg('info',bs.name?'Saving the reshaped team…':'Assembling & starting…');
  let vr;try{vr=await post('/api/loops/creator/validate',{config:cfg});}
  catch(e){briefSetMsg('bad','Couldn’t build a valid team: '+esc(e.message));return;}
  if(!vr||!vr.ok){const errs=(vr&&(vr.errors||(vr.error?[vr.error]:null)||(vr.raw?[vr.raw]:null)))||['invalid config'];briefSetMsg('bad','Couldn’t build a valid team: '+errs.filter(Boolean).join('; '));return;}
  const valid=vr.config;
  let sr;try{sr=await post('/api/loops/save',{config:valid});}
  catch(e){briefSetMsg('bad','Save failed: '+esc(e.message));return;}
  if(!(sr&&sr.ok)){const errs=(sr&&(sr.errors||[sr.error]))||['save failed'];briefSetMsg('bad','Save failed: '+errs.filter(Boolean).join('; '));return;}
  const nm=sr.name||valid.name||bs.name||'';
  if(bs.name){
    // reshaping an EXISTING loop — saving the reshaped config is the whole gesture;
    // Run re-adopts it (SAME surface for a not-yet-started and a live loop, §5.5).
    briefSetMsg('ok','Saved “'+esc(nm)+'” ✓ — hit Run and it re-adopts this reshaped team.');
    try{if(view==='loops'&&sel===nm){await tickDetail();}}catch(e){}
    return;
  }
  // Door B new loop — ignite through the composer's proven save+start seam, then
  // land on the running loop (never a fake success — a start error is surfaced).
  let ar;try{ar=await post('/api/loops/'+encodeURIComponent(nm)+'/action',{action:'start'});}catch(e){ar={error:e.message};}
  if(ar&&ar.error){briefSetMsg('bad','Saved “'+esc(nm)+'”, but couldn’t start it: '+esc(ar.error)+' — open it from Loops to run when a runner is attached.');return;}
  briefSetMsg('ok','▶ Running “'+esc(nm)+'” — opening it…');
  closeModal();
  try{view='loops';sel=nm;selHost='local';_writeUrl('/loops/'+encodeURIComponent(nm));await tickList();render();await pick(nm,'local');}catch(e){}
}
function nlExtractConfig(text){
  // the LAST fenced ```json … ``` block that contains an object; else first { … last }.
  const fence=/```(?:json)?\s*([\s\S]*?)```/gi;let m,last=null;
  while((m=fence.exec(text))!==null){if(m[1]&&m[1].indexOf("{")!==-1)last=m[1];}
  if(last)return last.trim();
  const a=text.indexOf("{"),b=text.lastIndexOf("}");
  return (a!==-1&&b>a)?text.slice(a,b+1):null;
}
function nlApplyFromTerminal(){
  const cfg=nlExtractConfig(nlBuf);
  const ed=document.getElementById("nlEditor");
  if(!cfg){nlSetMsg("bad","No ```json config block in the creator output yet — let it emit one first.");return;}
  let out=cfg;try{out=JSON.stringify(JSON.parse(cfg),null,2);}catch(e){}
  ed.value=out;nlValidConfig=null;nlSaveState(false);
  nlSetMsg("info","Applied the creator's config into the editor. Validate to check it against the real schema.");
}
// I2b — describe a goal → a rule-checked, EDITABLE suggested team. The shape is
// DERIVED from the goal (server-side loop_creator_suggest), not a fixed template:
// we drop the scaffolded config into the editor for the user to reshape, then the
// same Validate + Save gate applies. Rule-clean is shown as a green ✓ chip.
async function nlSuggest(){
  const gi=document.getElementById("nlGoal");const goal=(gi&&gi.value||"").trim();
  const out=document.getElementById("nlSuggestOut");
  if(!goal){nlSetMsg("bad","Describe your goal first — the team is derived from it.");if(gi)gi.focus();return;}
  nlSetMsg("info","Suggesting a team shape from your goal…");
  let res;try{res=await post("/api/loops/creator/suggest",{goal});}
  catch(e){nlSetMsg("bad","Suggest failed: "+esc(e.message));return;}
  if(!res||!res.ok){
    const errs=(res&&(res.errors||[res.error]))||["could not suggest a team"];
    nlSetMsg("bad","Suggest: "+errs.filter(Boolean).join("; "));return;
  }
  // render the editable role suggestion + rule-check status
  const clean=!(res.lint&&res.lint.length);
  const chip=clean?'<span class="sugok">rule-checked ✓</span>'
                  :'<span class="sugwarn">'+((res.lint||[]).length)+' lint note(s)</span>';
  const roles=(res.roles||[]).map(r=>
    `<li><span class="rid">${esc(r.id)}</span><span class="rwhy">${esc(r.why||r.role||"")}</span></li>`).join("");
  if(out){
    out.classList.remove("hidden");
    out.innerHTML=`<div class="sugtop"><b>Suggested team</b> ${chip}`
      +(res.signals&&res.signals.length?` <span class="hint" style="font-size:11px">from: ${esc(res.signals.join(", "))}</span>`:"")
      +`</div><ul class="nlsugroles">${roles}</ul>`
      +(res.note?`<div class="sugnote">${esc(res.note)}</div>`:"");
  }
  // Drop the project-bound scaffolded config into the (Advanced) editor so a power
  // user can reshape it; the composer's ▶ Start loop assembles + ignites directly.
  const previewCfg=nlInjectProject(Object.assign({},res.config));
  try{document.getElementById("nlEditor").value=JSON.stringify(previewCfg,null,2);}catch(e){}
  nlValidConfig=null;nlSaveState(false);
  nlSetMsg("ok","Previewed an editable team ✓ — hit ▶ Start loop, or open Advanced to reshape roles first.");
}
async function nlValidate(){
  const ed=document.getElementById("nlEditor");const text=(ed.value||"").trim();
  if(!text){nlSetMsg("bad","The editor is empty.");return;}
  nlSetMsg("info","Validating against the real schema + linter…");
  // When the editor holds a JSON object — the common case right after "Suggest
  // team" or a pasted config — post {config:parsed} (the path the server handles
  // cleanly), mirroring saveLoop(). A JSON STRING sent as {text} gets coerced to a
  // dict by the MCP layer and rejected, which used to leave Save permanently
  // disabled. Free-form prose (a creator transcript wrapping a ```json fence)
  // still goes as {text} so the server extracts the fenced block.
  let body;
  try{const parsed=JSON.parse(text);
      body=(parsed&&typeof parsed==="object"&&!Array.isArray(parsed))?{config:parsed}:{text};}
  catch(e){body={text};}
  let res;try{res=await post("/api/loops/creator/validate",body);}
  catch(e){nlSetMsg("bad","Validate failed: "+esc(e.message));return;}
  if(res&&res.ok){
    try{ed.value=JSON.stringify(res.config,null,2);}catch(e){}
    nlValidConfig=res.config;
    const warns=(res.warnings||[]).concat((res.lint&&res.lint.warnings)||[]);
    nlSetMsg("ok","Valid ✓ "+(warns.length?("— warnings: "+warns.join("; ")):"— ready to Save."));
    nlSaveState(true);
  }else{
    nlValidConfig=null;nlSaveState(false);
    // Surface a reason no matter the failure shape: {errors:[]} | {error} | {raw}
    // (a tool-boundary trace) — never the old bare "Invalid:" with nothing after.
    const errs=(res&&(res.errors||(res.error?[res.error]:null)||(res.raw?[res.raw]:null)))||["invalid config"];
    nlSetMsg("bad","Invalid: "+errs.filter(Boolean).join("; "));
  }
}
// Toggle BOTH creator save controls (plain Save + Save & run) in lockstep so a
// validated config lights them together and an edit dims them together.
function nlSaveState(on){
  const s=document.getElementById("nlSave");   if(s)s.disabled=!on;
  const r=document.getElementById("nlSaveRun");if(r)r.disabled=!on;
}
// §3.2 — the conversational ＋Loop now IGNITES, not just saves. `run` reuses the
// SAME `/api/loops/<name>/action {action:'start'}` the editor's saveLoop(true)
// fires and proves works (no new engine path). On a no-runner box the start
// fails with a clear message — we surface it CALMLY and still land on the saved
// loop, never a fake success.
async function nlSave(run){
  if(!nlValidConfig){nlSetMsg("bad","Validate a config first.");return;}
  nlSetMsg("info",run?"Saving & starting…":"Saving…");
  let res;try{res=await post("/api/loops/save",{config:nlValidConfig});}
  catch(e){nlSetMsg("bad","Save failed: "+esc(e.message));return;}
  if(!(res&&res.ok)){
    const errs=(res&&(res.errors||[res.error]))||["save failed"];
    nlSetMsg("bad","Save failed: "+errs.filter(Boolean).join("; "));
    return;
  }
  const nm=res.name||nlValidConfig.name||"";
  if(!run){
    nlSetMsg("ok","Saved loop “"+esc(nm)+"” ✓ — open it from Loops to run it.");
    return;
  }
  let r2;try{r2=await post("/api/loops/"+encodeURIComponent(nm)+"/action",{action:"start"});}
  catch(e){r2={error:e.message};}
  if(r2&&r2.error){
    // Degrade gracefully (principle 5): the team IS saved; only ignition failed
    // (typically "no runner attached" on a box with no worker). Say so plainly.
    nlSetMsg("bad","Saved “"+esc(nm)+"”, but couldn't start it: "+esc(r2.error)+" — open it from Loops to run when a runner is attached.");
    return;
  }
  nlSetMsg("ok","▶ Running “"+esc(nm)+"” — opening it…");
  // Route the user to the now-running loop, mirroring saveLoop's landing.
  try{view="loops";sel=nm;selHost="local";_writeUrl("/loops/"+encodeURIComponent(nm));
    await tickList();render();await pick(nm,"local");}catch(e){}
  // §3.2 RECONCILE — the start action returned ok, but a run can still die the
  // instant it starts (no runner reachable, engine ConnectError). pick() has just
  // loaded the real detail; if it already ERRORED, say so plainly RIGHT NOW rather
  // than leave a green "▶ Running…" that silently dies (the exact bug this fixes).
  // If it's genuinely running/finishing, the live view + 5s refresh carry it.
  try{if(runErrored(detail)){
    const why=failReason(detail)||"the run ended before it could start";
    flash("“"+nm+"” couldn't run — "+why+". Check the origin has a running loop-runner, then start it again.",true);
  }}catch(e){}
}
function bindNewLoop(){
  // Composer front door
  const st=document.getElementById("nlStart");   if(st)st.onclick=()=>nlStart(true);
  const sg=document.getElementById("nlSuggest");  if(sg)sg.onclick=nlSuggest;
  const ao=document.getElementById("nlAskOne");   if(ao)ao.onclick=nlAskOne;
  // Door B — the live briefing chat, seeded with the composed goal + project chip.
  const br=document.getElementById("nlBrief");    if(br)br.onclick=()=>{
    const goal=nlComposedGoal();
    if(!goal){nlSetMsg("bad","Describe a goal first — the briefing chat builds a team from it.");const gi=document.getElementById("nlGoal");if(gi)gi.focus();return;}
    const pj=document.getElementById("nlProject");const pid=((pj&&pj.value)||"").trim();
    openBriefChat({goal:goal,projectId:pid,phase:"brief"});
  };
  const at=document.getElementById("nlAddTarget");if(at)at.onclick=nlOpenTargetPicker;
  const pj=document.getElementById("nlProject");  if(pj)pj.onchange=()=>{
    if(nlValidConfig){nlInjectProject(nlValidConfig);
      const ed=document.getElementById("nlEditor");if(ed)try{ed.value=JSON.stringify(nlValidConfig,null,2);}catch(e){}}
  };
  // Advanced (config JSON + briefing terminal)
  const a=document.getElementById("nlApply");   if(a)a.onclick=nlApplyFromTerminal;
  const v=document.getElementById("nlValidate");if(v)v.onclick=nlValidate;
  const s=document.getElementById("nlSave");    if(s)s.onclick=()=>nlSave(false);
  const sr=document.getElementById("nlSaveRun");if(sr)sr.onclick=()=>nlSave(true);
  const ed=document.getElementById("nlEditor"); if(ed)ed.oninput=()=>{
    nlValidConfig=null;nlSaveState(false);
  };
  const rt=document.getElementById("nlRuntime");if(rt)rt.onchange=()=>{
    if(view==="newloop"&&(nlterm||nlWS)){stopNewLoop();startCreatorTerm();}   // relaunch the briefing PTY on the chosen runtime
  };
}
bindNewLoop();
window.addEventListener("beforeunload",()=>{if(nlWS){try{nlWS.close();}catch(e){}}});

async function bootstrap(){
  _migrateLegacyHash();
  applyUrl();
  // Rehydrate the show-archived preference BEFORE render() so the first paint
  // matches the last session; otherwise the checkbox flickers unchecked then
  // gets ticked on the next mutation.
  const showArch=_loadShowArch();
  await Promise.all([tickList(),loadOrigins(),loadProjects(),loadAnalytics(),loadSaved(),loadIssues(),loadHub(),loadProjectDispositions(),loadScopedCounts()]);
  const sa=document.getElementById('showArch');if(sa)sa.checked=showArch;
  render();
  // §7 (rd-origins/rd-sessions) — the Fleet pill, Sessions roster and Origin picker
  // are AMBIENT: they must never gate first paint (they proxy MCP calls that can be
  // slow). Load them in the background, then repaint just their surfaces.
  Promise.all([loadFleet(),loadSessions(),loadOriginPicker()]).then(()=>{
    renderFleetPill();
    if(view==='sessions')renderSessions();
    if(view==='overview')renderOverview();
    if(view==='newloop')nlPopulateOriginChip();
  });
  syncToolPanes();   // deep-link/refresh onto /terminal or /newloop starts its PTY (boot-only)
  if(view==='loops'&&sel)pick(sel,selHost||'local');
  if(view==='agents'&&curAgent)openAgent(curAgent);
  if(view==='issues'&&issueSel)openIssue(issueSel);
  // Fast pump — loops list + open loop detail track live state at ~5s.
  setInterval(tickList,5000);
  setInterval(()=>{if(!document.getElementById('modalRoot').innerHTML){tickDetail()}},7000);
  // Slow pump — analytics / registry / origins / projects refresh at 30s so the
  // Agents table, Favorites shelf, and origin/project pickers stay honest as
  // new runs land, without hammering mcp-loops.
  setInterval(()=>{if(document.getElementById('modalRoot').innerHTML)return;
    Promise.all([loadAnalytics(),loadSaved(),loadOrigins(),loadProjects(),loadIssues(),loadHub(),loadProjectDispositions(),loadScopedCounts(),loadFleet(),loadSessions()]).then(()=>{renderFleetPill();if(view==='agents'&&!curAgent)renderAgents();else if(view==='sessions')renderSessions();else if(view==='overview')renderOverview();else if(view==='origins')renderOrigins();else if(view==='projects')renderProjects();else if(view==='issues'&&!issueSel)renderIssues();else if(view==='hub')renderHub();else if(view==='loops')renderLoops();else renderNav()})},30000);
}
// ── M1 (QoL R6): mobile sidebar drawer ──────────────────────────────────────
// Toggle an off-canvas .sidebar on phones. Desktop never calls these (the
// hamburger is display:none >760px). Closing on any nav choice + Escape + a
// scrim tap keeps the drawer from trapping focus or hiding the content.
function _navApp(){return document.querySelector('.app')}
function _syncNavToggle(){var b=document.querySelector('.navtoggle');
  if(b)b.setAttribute('aria-expanded',_navApp().classList.contains('nav-open')?'true':'false')}
function toggleNav(){_navApp().classList.toggle('nav-open');_syncNavToggle()}
function closeNav(){_navApp().classList.remove('nav-open');_syncNavToggle()}
(function(){
  var side=document.getElementById('sidebar');
  if(side)side.addEventListener('click',function(e){
    if(e.target.closest('.navitem,.brand,.favrow,.newrun'))closeNav();});
  document.addEventListener('keydown',function(e){if(e.key==='Escape')closeNav();});
})();
// ── PWA (QoL R6 / M2): register the service worker ──────────────────────────
// App-shell + static-asset caching only; the SW never caches /api/* or /ws/*,
// so live loop data + the terminal WebSocket stay live. Fails soft on any error.
if('serviceWorker' in navigator){
  window.addEventListener('load',function(){
    navigator.serviceWorker.register('/sw.js').catch(function(){});
  });
}
bootstrap();
</script></body></html>"""
