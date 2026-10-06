// Library quality: per-loop and per-agent rollups of two SEPARATE signals —
// the owner's rating (good / ok / bad) and the analyst's score (0–100). They
// are shown side by side and never averaged together.
import type { Client } from './client';

export type QualityVerdict = 'good' | 'ok' | 'bad';

export interface QualityRun {
  runStarted: number | null;
  score: number | null;
  grade: QualityVerdict | null;
  owner: QualityVerdict | null;
}

export interface QualityLoop {
  name: string;
  origin?: string | null;
  project: string | null;
  state: string;
  archived: boolean;
  startedTs: number | null;
  lastTs: number | null;
  goal: string;
  agents: string[];
  turns: number;
  retries: number;
  perAgent?: Record<string, { turns: number; retries: number }>;
  pins?: Record<string, number[]>;
  owner: { verb: QualityVerdict; note?: string | null; at?: number | null } | null;
  analyst: { score: number; grade: QualityVerdict; verdict?: string; rationale?: string; at?: number } | null;
  analystPending: boolean;
  analystReason?: string;
  runs: QualityRun[];
  needsAttention: boolean;
  attentionReason?: string;
}

export interface QualityPoint {
  ts: number | null;
  score: number;
  loop: string;
}

export interface QualityAgent {
  agent: string;
  loops: string[];
  loopCount: number;
  turns: number;
  retries: number;
  retryRate: number;
  avgScore: number | null;
  scored: number;
  owner: { good: number; ok: number; bad: number };
  trend: 'up' | 'down' | 'flat' | null;
  trendDelta: number | null;
  history: QualityPoint[];
  best: { loop: string; origin?: string | null; score: number } | null;
  worst: { loop: string; origin?: string | null; score: number } | null;
  saved: boolean;
  favorite: boolean;
  versions: number[];
  lastTs: number | null;
  needsAttention: boolean;
  attentionReason?: string;
}

export interface QualityResponse {
  ok?: boolean;
  error?: string;
  days?: number;
  since?: number | null;
  computed?: number;
  pending?: number;
  loops: QualityLoop[];
  agents: QualityAgent[];
}

export interface QualityQuery {
  days?: number;
  project?: string;
  compute?: number;
}

export function qualityPath(q: QualityQuery = {}): string {
  const p = new URLSearchParams();
  if (q.days != null) p.set('days', String(q.days));
  if (q.project) p.set('project', q.project);
  if (q.compute != null) p.set('compute', String(q.compute));
  const s = p.toString();
  return '/api/loops/quality' + (s ? `?${s}` : '');
}

export const qualityApi = (c: Client) => ({
  quality: (q: QualityQuery = {}) => c.get<QualityResponse>(qualityPath(q)),
});

// ── Pure helpers ──

export const PERIODS = [
  { days: 7, label: 'Last 7 days' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
  { days: 0, label: 'All time' },
] as const;

export const periodLabel = (days: number): string => PERIODS.find((p) => p.days === days)?.label ?? `Last ${days} days`;

export type LibrarySort = 'quality' | 'usage' | 'recent';
export const LIBRARY_SORTS: { id: LibrarySort; label: string; title: string }[] = [
  { id: 'quality', label: 'Quality', title: 'Highest analyst score first, then your ratings' },
  { id: 'usage', label: 'Most used', title: 'Most loops first' },
  { id: 'recent', label: 'Recent', title: 'Most recently active first' },
];

/** Share of rated runs the owner called good; null until something is rated. */
export function goodRate(o: { good: number; ok: number; bad: number }): number | null {
  const n = o.good + o.ok + o.bad;
  return n ? o.good / n : null;
}

const byNum = (a: number | null | undefined, b: number | null | undefined) => (b ?? -1) - (a ?? -1);

export function sortAgents(rows: QualityAgent[], sort: LibrarySort): QualityAgent[] {
  const out = rows.slice();
  out.sort((a, b) => {
    let d = 0;
    if (sort === 'quality') d = byNum(a.avgScore, b.avgScore) || byNum(goodRate(a.owner), goodRate(b.owner));
    else if (sort === 'usage') d = b.loopCount - a.loopCount || b.turns - a.turns;
    else d = byNum(a.lastTs, b.lastTs);
    return d || a.agent.localeCompare(b.agent);
  });
  return out;
}

const OWNER_RANK: Record<string, number> = { good: 3, ok: 2, bad: 1 };

export function sortLoops(rows: QualityLoop[], sort: LibrarySort): QualityLoop[] {
  const out = rows.slice();
  out.sort((a, b) => {
    let d = 0;
    if (sort === 'quality') d = byNum(a.analyst?.score, b.analyst?.score) || byNum(OWNER_RANK[a.owner?.verb ?? ''], OWNER_RANK[b.owner?.verb ?? '']);
    else if (sort === 'usage') d = b.turns - a.turns || b.agents.length - a.agents.length;
    else d = byNum(a.lastTs, b.lastTs);
    return d || a.name.localeCompare(b.name);
  });
  return out;
}

/** A score as a word: good ≥70, ok ≥40, else bad (the analyst's own cut-offs). */
export const scoreGrade = (s: number): QualityVerdict => (s >= 70 ? 'good' : s >= 40 ? 'ok' : 'bad');

export const VERDICT_LABEL: Record<QualityVerdict, string> = { good: 'Good', ok: 'Okay', bad: 'Bad' };

/** "9 good · 1 bad" — zero buckets dropped; '' when nothing is rated. */
export function ownerTally(o: { good: number; ok: number; bad: number }): string {
  return [o.good && `${o.good} good`, o.ok && `${o.ok} okay`, o.bad && `${o.bad} bad`].filter(Boolean).join(' · ');
}

/** Bar heights (0–1) for a score series, capped to the newest `max` points. */
export function sparkBars(points: { score: number | null }[], max = 24): { h: number; grade: QualityVerdict | null; score: number | null }[] {
  return points.slice(-max).map((p) =>
    p.score == null ? { h: 0, grade: null, score: null } : { h: Math.max(0.06, Math.min(1, p.score / 100)), grade: scoreGrade(p.score), score: p.score },
  );
}

/** The loops an agent was in, as their quality rows, best latest score first. */
export function loopsForAgent(loops: QualityLoop[], agent: string): QualityLoop[] {
  return sortLoops(loops.filter((l) => l.agents.includes(agent)), 'quality');
}
