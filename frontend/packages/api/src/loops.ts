import type { Client } from './client';
import type { LoopState, LoopSummary, LoopsListResponse } from './types';

export const loopsApi = (c: Client) => ({
  /** The loop list. Pass an owner to scope the read server-side; omit for the unscoped (master) request. */
  list: (owner?: string) =>
    c.get<LoopsListResponse>('/api/loops' + (owner ? `?owner=${encodeURIComponent(owner)}` : '')),
});

// ── Pure view-model helpers (shared across platforms, unit-tested) ──

/** A loop's detail route on its ORIGIN. Records that name a loop (issues, Hub doc
 *  pointers, analytics rows) carry the origin; absent → the honest default `local`. */
export const loopHref = (name: string, origin?: string | null) =>
  `/loops/${encodeURIComponent(origin || 'local')}/${encodeURIComponent(name)}`;

/** A run that ENDED in error must never read as a green "finished". */
export const isErrored = (d: LoopSummary) => d.ended === 'error' || d.status === 'error';
export const honestState = (d: LoopSummary): LoopState => (isErrored(d) ? 'error' : d.state || 'saved');

export const loopKey = (d: LoopSummary) => `${d.host || 'local'}␟${d.name}`;
export const loopProject = (d: LoopSummary) => d.project || d.product || '';

/** The single local default owner (mirrors mcp_loops DEFAULT_OWNER / LOOPYARD_PILOT_OWNER). */
export const DEFAULT_OWNER = 'local';
/** A loop without a persisted owner belongs to the local default owner. */
export const loopOwner = (d: LoopSummary) => d.owner || DEFAULT_OWNER;
/** Distinct owners across a loop list, sorted — the owner picker shows only when this has >1 entry. */
export function loopOwners(loops: LoopSummary[]): string[] {
  return [...new Set(loops.map(loopOwner))].sort();
}

const plural = (n: number, one: string, many = one + 's') => `${n} ${n === 1 ? one : many}`;

/**
 * The team in plain words — `2 workers · 1 reviewer`, never `2W · 1I · 1M`. The
 * manager is implied (every team has one); a loop of one says so. Empty → ''.
 */
export function teamLabel(d: LoopSummary): string {
  if (d.single_agent) return 'solo agent';
  const t = d.team ?? {};
  const parts: string[] = [];
  const w = t.workers?.length ?? 0;
  const i = t.inputs?.length ?? 0;
  const s = t.subloops?.length ?? 0;
  if (w) parts.push(plural(w, 'worker'));
  if (i) parts.push(plural(i, 'reviewer'));
  if (s) parts.push(plural(s, 'sub-loop'));
  if (!parts.length && t.manager) parts.push('manager only');
  return parts.join(' · ');
}

/** `Turn 4 of 28` (or `Turn 4`); '' before the first turn is counted. */
export function turnsLabel(d: Pick<LoopSummary, 'turns_used' | 'turnLimit'>): string {
  if (d.turns_used == null) return '';
  return `Turn ${d.turns_used}${d.turnLimit ? ' of ' + d.turnLimit : ''}`;
}

/** The loop list's one human meta line: team · when (· project, when asked). */
export function loopMetaParts(d: LoopSummary, opts: { project?: boolean; now?: number } = {}): string[] {
  const out = [teamLabel(d), ago(d.updated || d.started, opts.now)];
  const proj = opts.project ? loopProject(d) : '';
  if (proj) out.push(d.productName || proj);
  return out.filter(Boolean);
}

/** De-slug an unknown raw state/status into plain words as a safe fallback. */
const deSlug = (s: string) => s.replace(/_/g, ' ').trim();

/**
 * Plain, reassuring words for a loop's state — never the raw slug (`waiting_owner`,
 * `guardian_stopped`) a beta tester shouldn't have to decode. Unknown states fall
 * back to a de-slugged form so nothing ever renders with an underscore.
 */
const STATE_LABELS: Record<string, string> = {
  running: 'running',
  stopping: 'wrapping up',
  stopped: 'stopped',
  waiting_owner: 'waiting for you',
  needs_owner: 'needs you',
  needs_input: 'needs you',
  finished: 'finished',
  complete: 'complete',
  error: 'needs a look',
  guardian_stopped: 'paused for safety',
  saved: 'draft',
};
export const stateLabel = (state: string): string => STATE_LABELS[state] ?? deSlug(state);

/**
 * Plain words for an agent's turn/report status — keeps internal protocol tokens
 * (`work_remaining`, `abandon`, `minor_only`) from leaking to a tester.
 */
const REPORT_LABELS: Record<string, string> = {
  completed: 'done',
  complete: 'done',
  satisfied: 'satisfied',
  continue: 'continuing',
  work_remaining: 'more to do',
  needs_work: 'needs more work',
  minor_only: 'minor items left',
  partial: 'partly done',
  blocked: 'blocked',
  stop: 'stopped',
  stopped: 'stopped',
  abandon: 'stepped back',
  abandoned: 'stepped back',
  error: 'hit a snag',
};
export const reportStatusLabel = (status: string): string => REPORT_LABELS[status] ?? deSlug(status);

export function ago(t: number | null | undefined, now = Date.now() / 1000): string {
  if (!t) return '';
  const s = Math.max(0, now - t);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export interface LoopFilter {
  query?: string;
  showArchived?: boolean;
  singleOnly?: boolean;
  /** Owner id to scope to; empty/absent = every owner. */
  owner?: string;
}

export function filterLoops(loops: LoopSummary[], f: LoopFilter): LoopSummary[] {
  const q = (f.query ?? '').toLowerCase().trim();
  return loops.filter(
    (d) =>
      (f.showArchived || !d.archived) &&
      (!q || d.name.toLowerCase().includes(q)) &&
      (!f.singleOnly || !!d.single_agent) &&
      (!f.owner || loopOwner(d) === f.owner),
  );
}

export interface LoopGroup {
  project: string; // '' = Unattributed
  label: string;
  loops: LoopSummary[];
}

/** Group by project in first-seen order; Unattributed always last. */
export function groupByProject(loops: LoopSummary[]): LoopGroup[] {
  const map = new Map<string, LoopGroup>();
  for (const d of loops) {
    const p = loopProject(d);
    let g = map.get(p);
    if (!g) {
      g = { project: p, label: p ? d.productName || p : 'Unattributed', loops: [] };
      map.set(p, g);
    }
    g.loops.push(d);
  }
  const groups = [...map.values()];
  return [...groups.filter((g) => g.project), ...groups.filter((g) => !g.project)];
}
