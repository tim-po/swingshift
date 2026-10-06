// /api/loops/projects* — the project catalog, per-project nav counts and
// dispositions, plus the project-scope helpers the global switcher uses.
import type { Client } from './client';
import type { LoopSummary } from './types';

export interface Project {
  id: string;
  name?: string;
  gitRemote?: string | null;
  gitBranch?: string | null;
  subPath?: string;
  repoDir?: string;
  outputRoot?: string;
  origin?: string | null;
  aka?: string[];
  connected?: boolean;
  note?: string;
  saved?: number;
}

export interface ProjectsResponse {
  projects?: Project[];
  /** legacy key the backend still emits */
  products?: Project[];
  gathered?: { created?: string[] | number; loops_scanned?: number } | null;
  /** Live loops with no explicit project, each with its guessed project (never assumed). */
  unattributed?: UnattributedLoop[];
  error?: string;
}

export interface UnattributedLoop {
  name: string;
  suggestedProject?: string | null;
}

export interface ProjectAssignResult {
  ok?: boolean;
  project?: string;
  assigned?: string[];
  errors?: { name: string; error?: string }[];
  error?: string;
}

export interface ProjectAddResult {
  ok?: boolean;
  id?: string;
  derived?: { name?: string };
  warnings?: string[];
  error?: string;
  errors?: string[];
}

export interface ProjectCounts {
  ok?: boolean;
  project?: string | null;
  loops?: number;
  ideahub?: number;
  issues?: number;
  origins?: number;
  origins_reachable?: number;
  sessions?: number;
  error?: string;
}

export interface ProjectDispositions {
  ok?: boolean;
  project?: string | null;
  good: number;
  ok_count: number;
  bad: number;
  rated: number;
  total: number;
  goodRate: number | null;
  loops: { loop: string; verb: 'good' | 'ok' | 'bad'; note?: string | null }[];
  error?: string;
}

/** Sentinel scope for loops bound to no confident project. Never collides with a real id. */
export const PROJECT_UNATTR = '__unattributed__';
/** Short alias (imported by other views). */
export const UNATTR = PROJECT_UNATTR;

/** MCP-proxied routes answer 200 with {error}; surface that as a thrown error. */
function orThrow<T extends { error?: string; errors?: string[] }>(r: T): T {
  if (r && r.error) throw new Error(r.error + (r.errors?.length ? ': ' + r.errors.join('; ') : ''));
  return r;
}

export const projectsApi = (c: Client) => ({
  list: () => c.get<ProjectsResponse>('/api/loops/projects'),
  add: async (source: string) => orThrow(await c.post<ProjectAddResult>('/api/loops/projects/add', { source })),
  save: async (project: Project) => orThrow(await c.post<{ ok?: boolean; error?: string }>('/api/loops/projects/save', { project })),
  /** Bind loops to a project (bulk assign, or accepting one loop's suggestion). */
  /** Per-loop failures come back in `errors` (the rest still assigned); only a whole-call error throws. */
  assign: async (project: string, loops: string[]) => {
    const r = await c.post<ProjectAssignResult>('/api/loops/projects/assign', { project, loops });
    if (r?.error) throw new Error(r.error);
    return r;
  },
  remove: async (id: string) => orThrow(await c.post<{ ok?: boolean; error?: string }>('/api/loops/projects/delete', { id })),
  counts: (project: string) => c.get<ProjectCounts>('/api/loops/counts?project=' + encodeURIComponent(project)),
  /** '' = whole fleet (never an empty path segment → 404). */
  dispositions: (project: string) =>
    c.get<ProjectDispositions>(project ? `/api/loops/projects/${encodeURIComponent(project)}/dispositions` : '/api/loops/dispositions'),
});

// ── Pure helpers ──

export const projectsOf = (r: ProjectsResponse | undefined): Project[] => r?.projects ?? r?.products ?? [];

export const unattributedOf = (r: ProjectsResponse | undefined): UnattributedLoop[] => r?.unattributed ?? [];

/** "Assigned 3 loops to p" / "… — 1 failed: x (why)". */
export function assignLine(r: ProjectAssignResult): string {
  const n = r.assigned?.length ?? 0;
  const head = `Assigned ${n} loop${n === 1 ? '' : 's'} to ${r.project}`;
  const errs = r.errors ?? [];
  return errs.length ? `${head} — ${errs.length} failed: ${errs.map((e) => e.name + (e.error ? ` (${e.error})` : '')).join(', ')}` : head + '.';
}

export function projectsGatheredCount(g: ProjectsResponse['gathered']): number {
  if (!g) return 0;
  return Array.isArray(g.created) ? g.created.length : g.created || 0;
}

/** Does a loop fall inside the scope? '' = all projects; PROJECT_UNATTR = no project binding. */
export function inProjectScope(d: { project?: string | null; product?: string | null }, scope: string): boolean {
  if (!scope) return true;
  const pid = d.project || d.product || '';
  return scope === PROJECT_UNATTR ? !pid : pid === scope;
}

export interface ProjectScope {
  id: string;
  name: string;
  count: number;
}

/**
 * The scopes the switcher offers: the catalog unioned with what loops are actually
 * bound to, each with its live (non-archived) loop count, busiest first, plus an
 * honest "Unattributed" scope when any loop carries no project.
 */
export function projectScopes(projects: Project[], loops: LoopSummary[], showArchived = false): ProjectScope[] {
  const map = new Map<string, ProjectScope>();
  for (const p of projects) if (p?.id) map.set(p.id, { id: p.id, name: p.name || p.id, count: 0 });
  let unattr = 0;
  for (const d of loops) {
    if (!showArchived && d.archived) continue;
    const pid = d.project || d.product;
    if (!pid) {
      unattr++;
      continue;
    }
    const cur = map.get(pid) ?? { id: pid, name: d.productName || pid, count: 0 };
    cur.count++;
    map.set(pid, cur);
  }
  const list = [...map.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  if (unattr) list.push({ id: PROJECT_UNATTR, name: 'Unattributed', count: unattr });
  return list;
}

export const loopsForProject = (loops: LoopSummary[], pid: string) => loops.filter((l) => (l.project || l.product) === pid);

export function projectDispositionLine(d: ProjectDispositions | undefined): string {
  if (!d || !d.rated) return '';
  const pct = d.goodRate != null ? ` · ${Math.round(d.goodRate * 100)}% good` : '';
  return `${d.good} good · ${d.ok_count} ok · ${d.bad} bad${pct} · ${d.rated}/${d.total} rated`;
}

// ── App-wide project scoping (the switcher re-scopes every surface in lockstep) ──

/** A raw project id vs the active scope: '' = all, PROJECT_UNATTR = no project. */
export function projectIdInScope(pid: string | null | undefined, scope: string): boolean {
  if (!scope) return true;
  return scope === PROJECT_UNATTR ? !pid : pid === scope;
}

/** Hub docs carry their own project. */
export const docInScope = (d: { project?: string | null }, scope: string) => projectIdInScope(d.project, scope);

/** Issues map through their loop when they carry no project of their own. */
export function issueInScope(
  x: { project?: string | null; product?: string | null; loop?: string | null },
  scope: string,
  loops: LoopSummary[],
): boolean {
  if (!scope) return true;
  const own = x.project && x.project !== 'Unattributed' ? x.project : x.product;
  const viaLoop = x.loop ? loops.find((l) => l.name === x.loop) : undefined;
  return projectIdInScope(own || (viaLoop ? viaLoop.project || viaLoop.product : null), scope);
}

/** The project a new record should be filed into for the active scope ('' when not a real project). */
export const scopeFileProject = (scope: string) => (scope && scope !== PROJECT_UNATTR ? scope : '');
