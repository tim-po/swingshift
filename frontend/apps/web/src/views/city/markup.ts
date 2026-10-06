// The city overlay (controls, panel, time strip, help), ported from the prototype.
export const CITY_MARKUP = `<div id="stage" aria-label="3D city of loops"></div>
<div class="top glass">
  <select id="prod" aria-label="Product"></select>
  <select id="grp" aria-label="Group neighbourhoods by"><option value="work">by plan</option><option value="kind">by kind of work</option><option value="time">by week started</option><option value="status">by status</option></select>
  <select id="skin" aria-label="Architecture"></select>
  <button type="button" class="chip need" id="needChip" hidden aria-expanded="false"></button>
</div>
<div class="needs glass" id="needs" hidden></div>
<aside class="panel glass" id="panel" aria-live="polite" hidden></aside>
<div class="timebar glass"><button type="button" id="tplay" title="Play history" aria-label="Play history">▶</button><input type="range" id="tscrub" min="0" max="1000" value="1000" aria-label="Scrub through time"><span id="tlabel">Now</span><button type="button" id="tlive" hidden>Now</button></div>
<div class="tools glass" role="toolbar" aria-label="View">
  <button type="button" id="rotL" title="Rotate left (Q)" aria-label="Rotate left">⟲</button><button type="button" id="rotR" title="Rotate right (E)" aria-label="Rotate right">⟳</button>
  <span class="sep"></span>
  <button type="button" data-tilt="low" aria-pressed="false" title="Street level" aria-label="Street level">▁</button><button type="button" data-tilt="iso" aria-pressed="true" title="Isometric" aria-label="Isometric">◇</button><button type="button" data-tilt="high" aria-pressed="false" title="From above" aria-label="From above">□</button>
  <span class="sep"></span>
  <button type="button" id="helpBtn" aria-expanded="false" title="How to read the city" aria-label="How to read the city">?</button>
</div>
<div class="help glass" id="help" hidden>
  <p><b>What a building is.</b> Its architecture comes from the files the loop made (delivered to git, or else produced in its output folder), by whichever kind is more than half: glass offices = frontend, concrete-grid offices = backend, signs and screens = visual (images, slides), brownstones = text, storehouses = data, plain = a mix, an Art Deco hall = talk, no files at all.</p>
  <p><b>Reading a building.</b> Base = manager; one floor per agent, one window row per turn. In every style workers have vertical windows and reviewers horizontal ones. Lit windows = shipped. Red steel frame = not built yet; a crane = building now; a billboard = needs you.</p>
  <p><b>How it went.</b> Every style has its own roof for each result, and they share a palette through what they're made of: gardens (green) when clean, pools and water (blue) when good, tarps, patches and scaffolding (ochre) when mixed, rust and scrap (red-brown) when bad. The band under each floor is that agent's result: <span class="sw" style="background:#6f9f68"></span> clean, <span class="sw" style="background:#5b82b0"></span> good after rework, <span class="sw" style="background:#c39a45"></span> mixed or unfinished, <span class="sw" style="background:#a9503e"></span> failed, <span class="sw" style="background:#cf7d3c"></span> waiting on you.</p>
  <p><b>Reading the city.</b> Each neighbourhood has its own pavement colour and a sign. Avenues with trees run between neighbourhoods. Cars drive where work is live.</p>
  <p><b>Planning.</b> Issues loops file go to the recycling centre at the edge of town. While an issue is open it's a dirt pile in the yard; once resolved it's pressed into a neat bale. Keep the yard clean. A plan no loop has picked up yet is a staked-out lot with tape; open it to turn it into a loop.</p>
  <p><b>Moving.</b> Drag to move, right-drag or Q/E to rotate, scroll to zoom. Click a building to open it, then click a floor to step inside: a cut-away room drawn from that agent's work (papers = reports, crates = files, plaque = last commit, lamp = failed or waiting) and its time card. ↑/↓ moves between floors. Shift+N jumps to the next loop that needs you, Esc closes.</p>
  <p id="foot"></p>
</div>`;
