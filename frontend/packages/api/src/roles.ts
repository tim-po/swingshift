// Roles (legacy "Agents"): fleet analytics per agent, the saved registry, and the
// per-agent drill-in (activity, v2 record, version timeline, a turn).
import type { Client } from './client';

export type StatusCounts = Record<string, number>;

export interface AgentStats {
  agent: string;
  turns: number;
  retries?: number;
  continues?: number;
  loops?: string[];
  /** loop name → origin for each of `loops` (links /loops/<origin>/<name>). */
  loop_origins?: Record<string, string>;
  loop_count: number;
  statuses?: StatusCounts;
  avg_gap_min?: number | null;
  last_ts?: number | null;
  saved?: boolean;
  /** Ran but was never materialized into the registry — can't be opened as a template yet. */
  ad_hoc?: boolean;
  /** Newest loop whose config carries this agent's step-def ("Save to library" lifts it from here); null = nothing to lift. */
  save_from?: string | null;
}

export interface AnalyticsResponse {
  agents: AgentStats[];
  totals?: Record<string, number>;
  note?: string;
  error?: string;
}

export interface SavedAgent {
  id: string;
  note?: string;
  favorite?: boolean;
  model?: string | null;
  defaultRole?: string | null;
  head?: number;
  versions?: number;
}

export interface RegistryResponse {
  agents: SavedAgent[];
  loops: { id: string; note?: string }[];
  error?: string;
}

export interface AgentLoopRow {
  loop: string;
  origin?: string | null;
  turns: number;
  retries?: number;
  continues?: number;
  avg_gap_min?: number | null;
}

export interface AgentTurnRow {
  loop: string;
  seq: number;
  status: string;
  note?: string;
  ts?: number;
  gap_min?: number | null;
}

export interface AgentDetail {
  agent: string;
  per_loop?: AgentLoopRow[];
  turns?: AgentTurnRow[];
  totals?: { turns?: number; loops?: number; retries?: number; continues?: number; avg_gap_min?: number | null; statuses?: StatusCounts };
  error?: string;
}

export interface AgentVersionFull {
  version: number;
  persona?: string;
  genericGoal?: string;
  model?: string | null;
  source?: string;
  note?: string;
  createdAt?: number;
}

export interface AgentRecord {
  id: string;
  head?: number;
  defaultRole?: string | null;
  model?: string | null;
  favorite?: boolean;
  versions?: AgentVersionFull[];
  error?: string;
}

export interface AgentVersionEntry {
  version: number;
  source?: string;
  note?: string;
  createdAt?: number;
  model?: string | null;
  changed?: string[];
  isHead?: boolean;
}

export interface AgentVersionsResponse {
  id: string;
  head?: number;
  versions: AgentVersionEntry[];
  error?: string;
}

export interface AgentTurnDetail {
  report?: { status: string; note?: string } | null;
  prompt?: string | null;
  transcript_tail?: string | null;
  error?: string;
}

const q = encodeURIComponent;

export const rolesApi = (c: Client) => ({
  analytics: () => c.get<AnalyticsResponse>('/api/loops/analytics'),
  registry: () => c.get<RegistryResponse>('/api/loops/registry'),
  detail: (id: string) => c.get<AgentDetail>(`/api/loops/agent?id=${q(id)}`),
  record: (id: string) => c.get<AgentRecord>(`/api/loops/agent/record?id=${q(id)}`),
  versions: (id: string) => c.get<AgentVersionsResponse>(`/api/loops/agent/versions?id=${q(id)}`),
  turn: (loop: string, agent: string, seq: number) =>
    c.get<AgentTurnDetail>(`/api/loops/${q(loop)}/turn?agent=${q(agent)}&seq=${seq}`),
  favorite: (id: string, favorite: boolean) =>
    c.post<{ ok?: boolean; error?: string }>('/api/loops/agent/favorite', { id, favorite }),
  /** Save a ran-but-unregistered agent to the library by lifting its step-def from `fromLoop` (loop_save_agent). */
  saveAgent: (id: string, fromLoop: string) =>
    c.post<{ ok?: boolean; id?: string; error?: string }>(`/api/loops/${q(fromLoop)}/action`, { action: 'save_agent', agent: id }),
});

/** True when an analytics row ran without a registry record (older feeds: `saved === false`). */
export const isAdHoc = (a: Pick<AgentStats, 'ad_hoc' | 'saved'>): boolean => a.ad_hoc ?? a.saved === false;

// ── Pure helpers ──

/** Minutes → "12.3m" / "1.4h"; null → "—". */
export const mins = (v: number | null | undefined): string =>
  v == null ? '—' : v >= 60 ? `${(v / 60).toFixed(1)}h` : `${(+v).toFixed(1)}m`;

export const isManagerish = (id: string) => /manager|orchestrat|coordinat/i.test(id);

export type AgentSortCol = 'agent' | 'loop_count' | 'turns' | 'avg' | 'continues' | 'retries';
export interface AgentSort {
  col: AgentSortCol;
  dir: 1 | -1;
}
export const DEFAULT_AGENT_SORT: AgentSort = { col: 'turns', dir: -1 };

/** Same column flips; a new column starts desc (agent id starts asc). */
export function nextAgentSort(cur: AgentSort, col: AgentSortCol): AgentSort {
  if (cur.col === col) return { col, dir: cur.dir === 1 ? -1 : 1 };
  return { col, dir: col === 'agent' ? 1 : -1 };
}

export function filterSortAgents(agents: AgentStats[], query: string, sort: AgentSort): AgentStats[] {
  const needle = query.toLowerCase().trim();
  const key = (a: AgentStats): string | number => {
    if (sort.col === 'agent') return (a.agent || '').toLowerCase();
    if (sort.col === 'avg') return a.avg_gap_min ?? 0;
    return a[sort.col] ?? 0;
  };
  return agents
    .filter((a) => !needle || (a.agent || '').toLowerCase().includes(needle))
    .sort((a, b) => {
      const ka = key(a),
        kb = key(b);
      return ka === kb ? 0 : (ka < kb ? -1 : 1) * sort.dir;
    });
}

/** Status histogram entries, most frequent first. */
export const statusEntries = (s: StatusCounts | undefined) => Object.entries(s ?? {}).sort((a, b) => b[1] - a[1]);

export const VERSION_SOURCE: Record<string, string> = {
  init: 'created',
  edit: 'persona edit',
  distill: 'goal re-distill',
  migrated: 'imported (v1)',
};

export function fmtWhen(ts: number | null | undefined): string {
  if (!ts) return '';
  try {
    return new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') + 'Z';
  } catch {
    return '';
  }
}

/** Version timeline, newest first; falls back to the record's versions. */
export function versionTimeline(rec: AgentRecord | undefined, vers: AgentVersionsResponse | undefined): AgentVersionEntry[] {
  const tl: AgentVersionEntry[] = vers?.versions?.length
    ? vers.versions
    : (rec?.versions ?? []).map((v) => ({ ...v, changed: [], isHead: v.version === rec?.head }));
  return tl.slice().reverse();
}

/** A saved agent's note in plain words — the registry's auto-save note
 *  ("auto-materialized from loop 'x'") reads as "First used in x". */
export function savedAgentNote(note: string | null | undefined): string {
  const n = (note ?? '').trim();
  const m = /^auto-materiali[sz]ed from loop ['"]?([^'"]+)['"]?$/i.exec(n);
  return m ? `First used in ${m[1]}` : n;
}
