// Workstreams: a loop's workstream is its PLAN. The Loops ledger and the city both
// group by the plan a loop was started from or moved into (doc.loops — a fact the
// engine keeps), never by guessing from names. Names only ever SUGGEST a plan for
// loops that aren't in one yet; the owner accepts, renames or ignores.
import type { HubDocRow } from './hub';
import type { OverviewLoop } from './overview';

/** A loop's status as the ledger shows it: one punch per loop. */
export type LedgerStatus = 'need' | 'run' | 'done' | 'part' | 'fail' | 'stop' | 'draft';

export function ledgerStatus(d: OverviewLoop): LedgerStatus {
  if (d.state === 'needs_owner' || d.state === 'waiting_owner' || d.state === 'guardian_stopped' || d.question) return 'need';
  if (d.state === 'running' || d.state === 'stopping') return 'run';
  const v = (d.result?.verdict || '').toUpperCase();
  if (d.state === 'error' || d.ended === 'error' || d.status === 'error' || v.startsWith('FAILED')) return 'fail';
  if (d.state === 'saved') return 'draft';
  if (d.state === 'stopped' || v.startsWith('ABANDONED')) return 'stop';
  if (v.startsWith('PARTIAL')) return 'part';
  return 'done';
}

export const LEDGER_STATUS: Record<LedgerStatus, string> = {
  need: 'Needs you', run: 'Running', done: 'Resolved', part: 'Partial', fail: 'Failed', stop: 'Stopped', draft: 'Draft',
};

export interface PlanGroup {
  /** The plan's doc id; null for the loops that aren't in a plan. */
  id: string | null;
  title: string;
  project?: string | null;
  loops: OverviewLoop[];
  /** Latest activity across its loops (for ordering). */
  last: number;
}

export const NOT_IN_A_PLAN = 'Not in a plan';

/** loop name → the plan it's in (the first plan that lists it, if an old loop is in several). */
export function planIndex(docs: HubDocRow[]): Map<string, HubDocRow> {
  const m = new Map<string, HubDocRow>();
  for (const doc of docs) for (const n of doc.loops ?? []) if (!m.has(n)) m.set(n, doc);
  return m;
}

const lastOf = (loops: OverviewLoop[]) => Math.max(0, ...loops.map((d) => d.updated ?? d.started ?? 0));

/** Plans with the loops shown, most recently active first; the loops in no plan come last. */
export function groupByPlan(loops: OverviewLoop[], docs: HubDocRow[]): PlanGroup[] {
  const idx = planIndex(docs);
  const byId = new Map<string, PlanGroup>();
  const loose: OverviewLoop[] = [];
  for (const d of loops) {
    const doc = idx.get(d.name);
    if (!doc) {
      loose.push(d);
      continue;
    }
    const g = byId.get(doc.id) ?? { id: doc.id, title: doc.title || 'Untitled plan', project: doc.project ?? null, loops: [], last: 0 };
    g.loops.push(d);
    byId.set(doc.id, g);
  }
  const out = [...byId.values()].map((g) => ({ ...g, last: lastOf(g.loops) })).sort((a, b) => b.last - a.last);
  if (loose.length) out.push({ id: null, title: NOT_IN_A_PLAN, loops: loose, last: lastOf(loose) });
  return out;
}

// Words that name what a loop does, not what it's about — never a series on their own.
const GENERIC = new Set(['build', 'review', 'remediate', 'refine', 'docs', 'loop', 'loops', 'the', 'and', 'fix', 'fixes', 'apply', 'min', 'minimal', 'refresh', 'launch', 'all', 'design', 'research', 'test', 'tests', 'v1', 'v2', 'v3']);

export interface PlanSuggestion {
  /** A title to start from, e.g. "Clearall". */
  title: string;
  loops: OverviewLoop[];
}

/** Loops not in a plan that share the first word of their name (a series like
 *  clearall-*), at least `min` of them: offered as "make this a plan". Nothing is
 *  grouped until the owner accepts. */
export function suggestPlans(unplanned: OverviewLoop[], min = 3, projects: Iterable<string> = []): PlanSuggestion[] {
  const by = new Map<string, OverviewLoop[]>();
  // A name that starts with a project's name ("loopyard-marketing-v2") names its series by the next word.
  const proj = new Set([...projects, ...unplanned.map((d) => d.project || d.product || '')].map((p) => p.toLowerCase()).filter(Boolean));
  for (const d of unplanned) {
    const words = d.name.toLowerCase().split(/[-_]+/);
    const first = proj.has(words[0]) && words.length > 1 ? words[1] : words[0];
    if (!first || first.length < 3 || GENERIC.has(first) || /^\d+$/.test(first)) continue;
    by.set(first, [...(by.get(first) ?? []), d]);
  }
  return [...by]
    .filter(([, ls]) => ls.length >= min)
    .map(([w, ls]) => ({ title: w[0].toUpperCase() + w.slice(1), loops: ls }))
    .sort((a, b) => b.loops.length - a.loops.length);
}

/** The project a new plan made from these loops belongs to: theirs, when they agree. */
export function commonProject(loops: OverviewLoop[]): string {
  const ps = new Set(loops.map((d) => d.project || d.product || ''));
  return ps.size === 1 ? [...ps][0] : '';
}
