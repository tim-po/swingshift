// The city's snapshot: the compact per-loop record the ported scene reads, built from
// the app's own API. Pure, so it's tested without a browser. Nothing here decides a
// building's look — the scene classifies files and scores itself (UI layer, not engine).
import { planIndex } from '@loopyard/api';
import type { DeliveredResponse, FilesResponse, Issue, LoopConfigResponse, OverviewLoop, QualityLoop, TeamRoom } from '@loopyard/api';

/** One loop, in the scene's short field names. */
export interface CityLoop {
  n: string; // name
  h: string; // host
  s: string; // state, in the scene's vocabulary
  a: boolean; // archived
  p: string; // project
  m: string | null; // manager
  w: string[]; // workers
  i: string[]; // inputs (reviewers)
  sub: string[]; // sub-loops
  u: number; // turns used
  l: number; // turn limit
  q: string | null; // what it's asking you
  st: number | null; // started
  up: number | null; // updated
  end: number | null; // finished
  act: number | null; // last activity
  sc: number | null; // analyst score
  ship: boolean | null; // left a commit
  pa: Record<string, [number, number]>; // per agent: [turns, retries]
  iss: [string, string, string, string][]; // issues it filed: [id, title, status, kind]
  pl?: string; // the plan it's in (its workstream): the city's neighbourhood
  // Filled in once the loop's details load:
  ev?: [number, string, string, 'round', string][]; // reports: [ts, agent, status, 'round', note]
  lv?: [string, string][][]; // floors ground-up; a level with several agents is a parallel group
  fx?: { src: 'git' | 'output' | 'none'; e: Record<string, number> }; // files it delivered (git), else produced, by extension
  /** Until the files load the building is drawn plain rather than guessed. */
  _kind?: { k: string; tot: number; share: [string, number][]; src: string };
}

export interface CityPlan { id: string; t: string; p: string; s: string; loops: string[]; up: number | null }
export interface CitySnap { loops: CityLoop[]; plans: CityPlan[]; links: [string, string, string, string][]; now: number }

/** A plan row as /api/loops/docs returns it. */
export interface CityDoc { id: string; title?: string | null; project?: string | null; state?: string | null; loops?: string[] | null; updated?: number | null; ts?: number | null }

const PENDING_KIND = { k: 'mixed', tot: 0, share: [] as [string, number][], src: 'loading' };
const UNKNOWN_KIND = { k: 'mixed', tot: 0, share: [] as [string, number][], src: 'unknown' };

/** The scene's states: waiting_owner = needs you, running, error, stopped, saved, finished. */
function sceneState(d: OverviewLoop): string {
  if (d.ended === 'error' || d.status === 'error' || d.state === 'error') return 'error';
  if (d.state === 'needs_owner' || d.state === 'waiting_owner' || d.state === 'guardian_stopped' || d.question) return 'waiting_owner';
  if (d.state === 'running' || d.state === 'stopping') return d.state;
  if (d.state === 'stopped' || d.state === 'saved') return d.state;
  return 'finished';
}

export function cityLoop(d: OverviewLoop, q: QualityLoop | undefined, issues: Issue[]): CityLoop {
  const s = sceneState(d);
  const live = s === 'running' || s === 'stopping' || s === 'waiting_owner';
  const pa: CityLoop['pa'] = {};
  for (const [agent, v] of Object.entries(q?.perAgent ?? {})) pa[agent] = [v.turns, v.retries];
  return {
    n: d.name,
    h: d.host || 'local',
    s,
    a: !!d.archived,
    p: d.project || d.product || '',
    m: d.team?.manager ?? null,
    w: d.team?.workers ?? [],
    i: d.team?.inputs ?? [],
    sub: d.team?.subloops ?? [],
    u: d.turns_used ?? 0,
    l: d.turnLimit ?? 0,
    q: d.question || (d.state === 'guardian_stopped' ? 'Paused for safety — it needs a look before it carries on.' : null),
    st: d.started ?? null,
    up: d.updated ?? null,
    end: live ? null : d.updated ?? null,
    act: d.last_report?.ts ?? d.updated ?? null,
    sc: q?.analyst && !q.analystPending ? q.analyst.score : null,
    ship: d.result?.commit ? true : null,
    pa,
    iss: issues.filter((x) => x.loop === d.name).map((x) => [x.id, x.title || 'Untitled issue', x.status || 'open', x.kind || 'issue']),
    _kind: PENDING_KIND,
  };
}

/** The engine's own per-turn transcripts (agent-001.txt) are not the loop's work. */
const TRANSCRIPT = /-\d{3}\.txt$/;
const extOf = (path: string) => {
  const b = path.split('/').pop()!.toLowerCase().replace(/^\.+/, '');
  const i = b.lastIndexOf('.');
  return i > 0 ? b.slice(i + 1) : '';
};

/** Files the loop produced in its output folder, by extension. */
export function producedFx(r: FilesResponse | null | undefined): Record<string, number> {
  const e: Record<string, number> = {};
  for (const g of r?.artifacts ?? [])
    for (const f of g.files ?? []) {
      if (TRANSCRIPT.test(f.path)) continue;
      const x = extOf(f.path);
      if (x) e[x] = (e[x] ?? 0) + 1;
    }
  return e;
}

/** What a loop made: files delivered in git first, else files it produced; null when neither can be seen from here. */
export function filesFx(dv: DeliveredResponse | null | undefined, files: FilesResponse | null | undefined): CityLoop['fx'] | null {
  if (dv?.known && dv.files > 0) return { src: 'git', e: dv.delivered };
  const e = producedFx(files);
  if (Object.keys(e).length) return { src: 'output', e };
  if (dv?.known) return { src: 'none', e: {} };
  return null;
}

/** Floors from the saved config: stepOrder minus the manager; a nested list is a parallel group. */
export function levelsFrom(cfg: LoopConfigResponse | null | undefined, manager: string | null): CityLoop['lv'] {
  const c = cfg?.config ?? {};
  const kind = (id: string) => {
    const r = c.steps?.[id]?.role;
    return r === 'input_provider' || r === 'input' ? 'input' : r === 'subloop' || r === 'sublink' ? 'sublink' : 'worker';
  };
  return (c.stepOrder ?? [])
    .map((x) => (Array.isArray(x) ? x : [x]).filter((id) => id && id !== manager).map((id) => [id, kind(id)] as [string, string]))
    .filter((l) => l.length);
}

/** Every agent's reports, oldest first, from the team room's turn dots. */
export function reportsFrom(tr: TeamRoom | null | undefined): CityLoop['ev'] {
  const out: NonNullable<CityLoop['ev']> = [];
  for (const card of tr?.roster ?? [])
    for (const dot of card.turnDots?.dots ?? [])
      if (dot.ts && dot.status) out.push([dot.ts, card.agent, dot.status, 'round', dot.note ?? '']);
  return out.sort((a, b) => a[0] - b[0]);
}

export interface LoopDetails { cfg?: LoopConfigResponse | null; tr?: TeamRoom | null; files?: FilesResponse | null; delivered?: DeliveredResponse | null }

/** Fold a loop's loaded details into its record. */
export function withDetails(x: CityLoop, det: LoopDetails | undefined): CityLoop {
  if (!det) return x;
  const out: CityLoop = { ...x };
  if (det.cfg) out.lv = levelsFrom(det.cfg, x.m);
  if (det.tr) out.ev = reportsFrom(det.tr);
  if (det.files !== undefined || det.delivered !== undefined) {
    const fx = filesFx(det.delivered, det.files);
    if (fx) {
      out.fx = fx;
      delete out._kind;
    } else out._kind = UNKNOWN_KIND; // its work isn't visible from here: drawn plain, never guessed as talk
  }
  return out;
}

export function buildSnap(i: {
  loops: OverviewLoop[];
  quality: QualityLoop[];
  issues: Issue[];
  docs: CityDoc[];
  details: Map<string, LoopDetails>;
  now: number;
}): CitySnap {
  const q = new Map(i.quality.map((x) => [`${x.origin || 'local'}/${x.name}`, x]));
  const inPlan = planIndex(i.docs.map((d) => ({ id: d.id, title: d.title ?? undefined, loops: d.loops ?? [] })));
  return {
    loops: i.loops.map((d) => {
      const c = withDetails(cityLoop(d, q.get(`${d.host || 'local'}/${d.name}`), i.issues), i.details.get(detailKey(d)));
      const plan = inPlan.get(d.name);
      return plan ? { ...c, pl: plan.title || 'Untitled plan' } : c;
    }),
    plans: i.docs.map((d) => ({ id: d.id, t: d.title || d.id, p: d.project || '', s: d.state || 'note', loops: d.loops ?? [], up: d.updated ?? d.ts ?? null })),
    links: [],
    now: i.now,
  };
}

export const detailKey = (d: Pick<OverviewLoop, 'host' | 'name'>) => `${d.host || 'local'}/${d.name}`;

/** Changes that should redraw the city: what each building shows, not timestamps ticking. */
export function snapSignature(s: CitySnap): string {
  return JSON.stringify(s.loops.map((d) => [d.n, d.h, d.s, d.u, d.l, d.sc, d.q, d.pl ?? '', d.w.length, d.i.length, d.iss.length, d.ev?.length ?? -1, d.lv?.length ?? -1, d.fx ? Object.keys(d.fx.e).length : -1, d._kind?.src ?? '']));
}
