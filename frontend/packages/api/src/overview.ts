// Overview = a pure re-composition over loops / Idea Hub docs / issues / origins,
// scoped to one project. Minimal shapes here so this module stays independent of
// the other areas' richer types.
import type { Client } from './client';
import type { LoopSummary } from './types';

/** Sentinel scope id for "no project binding" (never collides with a real id). */
export const UNATTRIBUTED = '__unattributed__';

export const OVERVIEW_RUNNING = new Set(['running', 'stopping', 'waiting_owner', 'needs_owner']);

/** The compact result a finished loop's /api/loops row carries (null while live). */
export interface LoopResult {
  green?: boolean;
  verdict?: string | null;
  resolution?: string | null;
  ended?: string | null;
  commit?: string | null;
  turns_used?: number | null;
  winddown_turns?: number | null;
}
export type OverviewLoop = LoopSummary & { result?: LoopResult | null; question?: string | null };

export interface OverviewDoc {
  id: string;
  project?: string | null;
  state?: string;
  loop_count?: number;
}
export interface OverviewIssue {
  title?: string | null;
  id: string;
  status?: string | null;
  project?: string | null;
  product?: string | null;
  loop?: string | null;
}
export interface OverviewOrigin {
  id: string;
  kind?: string | null;
  reachable?: boolean;
}

export const overviewApi = (c: Client) => ({
  docs: () => c.get<{ docs: OverviewDoc[]; error?: string }>('/api/loops/docs'),
  issues: () => c.get<{ issues: OverviewIssue[]; error?: string }>('/api/loops/issues'),
  origins: () => c.get<{ origins: OverviewOrigin[]; error?: string }>('/api/loops/origins'),
});

// ── Pure helpers ──

/** Does a record bound to project `pid` fall in scope? ''/undefined scope = all. */
export function pidInScope(pid: string | null | undefined, scope: string | null | undefined): boolean {
  if (!scope) return true;
  return scope === UNATTRIBUTED ? !pid : pid === scope;
}

const isSessionOrigin = (o: OverviewOrigin) => (o.kind ?? '').toLowerCase() === 'connected-session';
const isLocalOrigin = (o: OverviewOrigin) => (o.kind ?? '').toLowerCase() === 'local' || o.id === 'local';

export interface OverviewInput {
  loops: OverviewLoop[];
  docs: OverviewDoc[];
  issues: OverviewIssue[];
  origins: OverviewOrigin[];
  scope?: string | null;
  showArchived?: boolean;
}

export interface OverviewModel {
  loops: OverviewLoop[];
  running: OverviewLoop[];
  docs: number;
  objectives: number;
  issuesTotal: number;
  issuesOpen: number;
  origins: number;
  reachable: number;
  results: OverviewLoop[];
  /** Loops that are stuck on you: asking a question, errored, or guardian-stopped. */
  attention: AttentionItem[];
  /** The most recently touched loops in scope, newest first. */
  recent: OverviewLoop[];
  openIssues: OverviewIssue[];
  /** Every open issue in scope (openIssues is the first five). */
  allOpenIssues: OverviewIssue[];
}

export interface AttentionItem {
  loop: OverviewLoop;
  /** Effective state (an errored end counts as `error`) — keys the Badge colour. */
  state: string;
  /** Why it needs you, in the owner's words. */
  reason: string;
}

const ATTENTION: [string, string][] = [
  ['waiting_owner', 'Waiting on your answer'],
  ['needs_owner', 'Needs you'],
  ['guardian_stopped', 'Paused for safety'],
  ['error', 'Ended with an error'],
];

/** Loops stuck on the owner, most urgent first. */
export function attentionItems(loops: OverviewLoop[]): AttentionItem[] {
  const out: AttentionItem[] = [];
  for (const [state, reason] of ATTENTION) {
    for (const d of loops) {
      const st = d.ended === 'error' || d.status === 'error' ? 'error' : d.state;
      if (st === state) out.push({ loop: d, state, reason: d.question ? 'Asked you a question' : reason });
    }
  }
  return out;
}

/** Time-of-day greeting for the Home header. */
export function greeting(hour: number): string {
  if (hour < 5) return 'Good evening';
  if (hour < 12) return 'Good morning';
  if (hour < 18) return 'Good afternoon';
  return 'Good evening';
}

/** 0..1 progress through a loop's turn budget, or null when it has no limit. */
export function loopProgress(d: Pick<OverviewLoop, 'turns_used' | 'turnLimit'>): number | null {
  if (!d.turnLimit || d.turns_used == null) return null;
  return Math.max(0, Math.min(1, d.turns_used / d.turnLimit));
}

export function buildOverview(i: OverviewInput): OverviewModel {
  const scope = i.scope ?? '';
  const loopPid = (d: OverviewLoop) => d.project || d.product || null;
  const loops = i.loops.filter((d) => (i.showArchived || !d.archived) && pidInScope(loopPid(d), scope));
  const running = loops.filter((d) => OVERVIEW_RUNNING.has(d.state));
  const docs = i.docs.filter((d) => pidInScope(d.project, scope));
  const objectives = docs.filter((d) => (d.loop_count ?? 0) > 0 || d.state === 'objective');
  // An issue maps to its project through its loop when it carries none itself.
  const loopProj = new Map(i.loops.map((l) => [l.name, loopPid(l)]));
  const iss = i.issues.filter((x) => pidInScope(x.project || x.product || (x.loop ? loopProj.get(x.loop) : null), scope));
  const open = iss.filter((x) => {
    const st = x.status || 'open';
    return st !== 'resolved' && st !== 'dismissed';
  });
  const boxes = i.origins.filter((o) => !isSessionOrigin(o));
  const reachable = boxes.filter((o) => (o.reachable !== undefined ? o.reachable : isLocalOrigin(o)));
  const results = loops.filter((d) => !OVERVIEW_RUNNING.has(d.state) && d.result).slice(0, 4);
  const attention = attentionItems(loops);
  const recent = [...loops].sort((a, b) => (b.updated ?? b.started ?? 0) - (a.updated ?? a.started ?? 0)).slice(0, 6);
  return {
    loops,
    running,
    docs: docs.length,
    objectives: objectives.length,
    issuesTotal: iss.length,
    issuesOpen: open.length,
    origins: boxes.length,
    reachable: reachable.length,
    results,
    attention,
    recent,
    openIssues: open.slice(0, 5),
    allOpenIssues: open,
  };
}

/** Display name for a scope: catalog/loop name when known, else the id. */
export function scopeName(scope: string | null | undefined, loops: LoopSummary[]): string {
  if (!scope) return 'All projects';
  if (scope === UNATTRIBUTED) return 'Unattributed';
  return loops.find((l) => (l.project || l.product) === scope)?.productName || scope;
}
