// Hub-as-Workspace — an objective is a FOLDER of *statused* documents, kept honest
// by two additive-only origin agents (Sweep / Reconcile). This is the typed client
// + pure helpers over the mcp_loops.workspace store (HUB-WORKSPACE-SPEC, LOCKED).
//
// Endpoints live under /api/loops/workspace/* (tracking_ui). The client mirrors the
// server shape produced by Workspace._decorate() / get_objective() / get_doc() /
// list_suggestions(); the pure helpers (rollupStatus, statusChip, …) are the exact
// UI-side echoes of the backend rules so the view can badge optimistically and a
// vitest proves the rule without a live server.
import type { Client } from './client';
import { rejectErrorBody } from './issues';
import { honestState } from './loops';
import { inProjectScope } from './projects';
import type { LoopSummary } from './types';

// ── the doc status ladder (mirrors workspace.DOC_STATUSES) ──
export const WS_DOC_STATUSES = ['draft', 'todo', 'in progress', 'done'] as const;
export type WsStatus = (typeof WS_DOC_STATUSES)[number];

/**
 * The fixed objective id where Sweep parks its `new_objective` drafts (mirrors
 * mcp_loops.workspace.INBOX_ID). It is a real objective folder — but a *candidate*
 * one: it holds no human docs, only pending `new_objective` suggestions, and Accept
 * turns each into its own real objective. The UI surfaces it distinctly, not as a
 * peer of authored objectives.
 */
export const WS_INBOX_ID = 'sweep-inbox';

export const WS_SUG_KINDS = ['new_objective', 'new_doc', 'enrichment', 'status'] as const;
export type WsSugKind = (typeof WS_SUG_KINDS)[number];
export const WS_SUG_STATES = ['pending', 'accepted', 'declined', 'discussing'] as const;
export type WsSugState = (typeof WS_SUG_STATES)[number];

/** A commit link Reconcile attaches as evidence for a done/todo split. */
export interface WsEvidence {
  commit?: string;
  subject?: string;
  url?: string;
  path?: string;
  note?: string;
}

/** A doc's meta (no body) — one row in the objective's file tree. */
export interface WsDocMeta {
  slug: string;
  title: string;
  status: WsStatus;
  author?: string;
  created?: number;
  updated?: number;
}
/** A doc's meta + its markdown body, for the pretty-render / Edit view. */
export interface WsDoc extends WsDocMeta {
  body: string;
}

/** An objective — decorated with its derived rollup + effective (override-aware) status. */
export interface WsObjective {
  id: string;
  title: string;
  created?: number;
  updated?: number;
  status_override?: WsStatus | null;
  /** the status DERIVED from the docs (the locked rollup rule) */
  rollup: WsStatus;
  /** the headline status shown to the human — override wins over rollup */
  status: WsStatus;
  overridden: boolean;
  doc_count: number;
  /** true for the fixed Sweep-candidates inbox (see {@link WS_INBOX_ID}). */
  inbox?: boolean;
  /** 'ideahub' when this objective is an adapted Idea Hub doc (see {@link WS_IH_PREFIX}). */
  source?: 'ideahub' | string;
  /** the underlying Idea Hub doc id — keys its objective-manager thread. */
  ideahub_id?: string;
  project?: string | null;
  /** Names of the loops pointed at this objective (the durable plan↔loop link). */
  loops?: string[];
  /** Adapted Hub docs only: the derived Hub state (note/proposal/objective/done). */
  hub_state?: string;
  /** Adapted Hub docs only: the result a finished loop (or you) recorded, if any. */
  hub_result?: WsHubResult | null;
}
/** The compact result ribbon on a Hub doc — `green` only for a clean finish or "Mark done". */
export interface WsHubResult {
  loop?: string | null;
  ended?: string;
  green?: boolean;
  verdict?: string;
  summary?: string;
  /** 'you' when the owner marked it done by hand. */
  by?: string;
  ts?: number;
}
export interface WsObjectiveDetail extends WsObjective {
  docs: WsDocMeta[];
}

export interface WsSuggestion {
  id: string;
  state: WsSugState;
  kind: WsSugKind;
  title?: string;
  body?: string;
  target_slug?: string | null;
  status?: WsStatus | null;
  origin?: string;
  agent?: string;
  evidence?: WsEvidence[];
  ts?: number;
  resolved_ts?: number;
  result?: { created_slug?: string; title?: string; applied_status?: WsStatus; slug?: string; discuss_id?: string };
}

/** A bundle of suggested edits parked in the to-discuss FOLDER for a human to point a loop at. */
export interface WsDiscussItem {
  id: string;
  from_suggestion?: string;
  kind?: WsSugKind;
  title?: string;
  body?: string;
  target_slug?: string | null;
  status?: WsStatus | null;
  evidence?: WsEvidence[];
  ts?: number;
}

/** The two additive-only origin agents, launched as origin.run jobs. */
export type WsAgent = 'sweep' | 'reconcile';
export interface WsRunInput {
  agent: WsAgent;
  origin: string;
  /** Sweep: an optional path scope within the origin. Reconcile: the objective under review. */
  scope?: string;
  objective?: string;
}
export interface WsRunResponse {
  ok?: boolean;
  job?: string;
  agent?: WsAgent;
  error?: string;
}

export const workspaceApi = (c: Client) => ({
  listObjectives: () => c.get<{ objectives: WsObjective[]; error?: string }>('/api/loops/workspace/objectives'),
  getObjective: async (id: string) =>
    rejectErrorBody(await c.get<{ objective: WsObjectiveDetail; error?: string }>('/api/loops/workspace/objective/' + encodeURIComponent(id))),
  /** Create a plan; `project` (optional) files it under a project up front. */
  createObjective: async (title: string, project?: string) =>
    rejectErrorBody(
      await c.post<{ objective: WsObjective; error?: string }>('/api/loops/workspace/objective/create', project ? { title, project } : { title }),
    ),
  overrideStatus: async (id: string, status: WsStatus | null) =>
    rejectErrorBody(await c.post<{ objective: WsObjective; error?: string }>('/api/loops/workspace/objective/override', { id, status })),
  getDoc: async (oid: string, slug: string) =>
    rejectErrorBody(await c.get<{ doc: WsDoc; error?: string }>(`/api/loops/workspace/doc/${encodeURIComponent(oid)}/${encodeURIComponent(slug)}`)),
  // The human write path — the SOLE writer of doc content (additive-only spine).
  saveDoc: async (i: { oid: string; title: string; body: string; slug?: string; status?: WsStatus }) =>
    rejectErrorBody(await c.post<{ doc: WsDoc; error?: string }>('/api/loops/workspace/doc/save', i)),
  setDocStatus: async (oid: string, slug: string, status: WsStatus) =>
    rejectErrorBody(await c.post<{ doc: WsDocMeta; error?: string }>('/api/loops/workspace/doc/status', { oid, slug, status })),
  suggestions: (oid: string, state?: WsSugState) =>
    c.get<{ suggestions: WsSuggestion[]; error?: string }>(
      `/api/loops/workspace/suggestions/${encodeURIComponent(oid)}` + (state ? `?state=${encodeURIComponent(state)}` : ''),
    ),
  accept: async (oid: string, sid: string) =>
    rejectErrorBody(await c.post<{ suggestion: WsSuggestion; error?: string }>('/api/loops/workspace/suggestion/accept', { oid, sid })),
  decline: async (oid: string, sid: string) =>
    rejectErrorBody(await c.post<{ suggestion: WsSuggestion; error?: string }>('/api/loops/workspace/suggestion/decline', { oid, sid })),
  discuss: async (oid: string, sid: string) =>
    rejectErrorBody(await c.post<{ suggestion: WsSuggestion; error?: string }>('/api/loops/workspace/suggestion/discuss', { oid, sid })),
  toDiscuss: (oid: string) => c.get<{ items: WsDiscussItem[]; error?: string }>(`/api/loops/workspace/to-discuss/${encodeURIComponent(oid)}`),
  run: async (i: WsRunInput) => rejectErrorBody(await c.post<WsRunResponse>('/api/loops/workspace/run', i)),
  /**
   * Run a plan as a loop, step 1: build + save a loop from the plan and record the
   * plan↔loop link. Starts NOTHING — the caller starts the returned loop through
   * the normal loop action seam after the human confirmed.
   */
  buildLoop: async (id: string, project?: string) =>
    rejectErrorBody(
      await c.post<{ ok?: boolean; loop?: string; objective?: WsObjectiveDetail; error?: string }>(
        '/api/loops/workspace/objective/build-loop',
        project ? { id, project } : { id },
      ),
    ),
});

// ── Pure helpers (UI-side echoes of the backend rules — tested without a server) ──

const STATUS_ALIASES: Record<string, WsStatus> = {
  'in-progress': 'in progress',
  inprogress: 'in progress',
  wip: 'in progress',
  'to do': 'todo',
};
export function normalizeStatus(s: string | null | undefined): WsStatus {
  const key = String(s ?? '').trim().toLowerCase();
  if ((WS_DOC_STATUSES as readonly string[]).includes(key)) return key as WsStatus;
  return STATUS_ALIASES[key] ?? 'draft';
}

/**
 * The objective's headline status DERIVED from its docs — the LOCKED rule, echoed
 * client-side for optimistic badging:
 *   any `in progress` ⇒ in progress; any `todo` & none in-progress ⇒ todo;
 *   all `done` (≥1 doc) ⇒ done; else draft. Empty ⇒ draft.
 */
export function rollupStatus(statuses: Array<string | null | undefined>): WsStatus {
  const s = (statuses ?? []).map(normalizeStatus);
  if (!s.length) return 'draft';
  if (s.some((x) => x === 'in progress')) return 'in progress';
  if (s.some((x) => x === 'todo')) return 'todo';
  if (s.every((x) => x === 'done')) return 'done';
  return 'draft';
}

/** The effective status a human sees: a pinned override wins over the rollup. */
export function effectiveStatus(o: Pick<WsObjective, 'status_override'> & { docs?: Array<{ status?: string }> }, rolled?: WsStatus): WsStatus {
  if (o.status_override) return normalizeStatus(o.status_override);
  return rolled ?? rollupStatus((o.docs ?? []).map((d) => d.status));
}

/** Chip presentation for a doc/objective status — colour class, label, glyph. */
export function statusChip(status: string | null | undefined): { cls: string; label: string; glyph: string; key: WsStatus } {
  const key = normalizeStatus(status);
  const meta: Record<WsStatus, { cls: string; glyph: string }> = {
    draft: { cls: 'st-draft', glyph: '○' },
    todo: { cls: 'st-todo', glyph: '◔' },
    'in progress': { cls: 'st-prog', glyph: '◑' },
    done: { cls: 'st-done', glyph: '●' },
  };
  return { key, label: key, glyph: meta[key].glyph, cls: meta[key].cls };
}

/** Plain label + icon name (components/icons) for a suggestion kind. */
export function suggestionKindMeta(kind: string | null | undefined): { label: string; icon: string } {
  switch (kind) {
    case 'new_objective':
      return { label: 'New plan', icon: 'sparkle' };
    case 'new_doc':
      return { label: 'New note', icon: 'plus' };
    case 'enrichment':
      return { label: 'More detail', icon: 'edit' };
    case 'status':
      return { label: 'Status change', icon: 'check' };
    default:
      return { label: 'Suggestion', icon: 'dot' };
  }
}

/**
 * The Reconcile deliverable, made legible: fold an objective's PENDING `status`
 * proposals (the ones Reconcile emits from real commit evidence) into a
 * done/todo SPLIT for an at-a-glance read. `done` = proposed done; `todo` =
 * proposed todo; `other` = proposed in-progress/draft. Non-status and resolved
 * suggestions are ignored — those live as their own cards. Pure; tested without a
 * server. The per-item Accept/Decline/Discuss stays on the cards below the split.
 */
export interface WsReconcileSplit {
  done: WsSuggestion[];
  todo: WsSuggestion[];
  other: WsSuggestion[];
  total: number;
}
export function reconcileSplit(suggestions: Array<WsSuggestion> | null | undefined): WsReconcileSplit {
  const split: WsReconcileSplit = { done: [], todo: [], other: [], total: 0 };
  for (const s of suggestions ?? []) {
    if (s.state !== 'pending' || s.kind !== 'status') continue;
    split.total += 1;
    const st = normalizeStatus(s.status);
    if (st === 'done') split.done.push(s);
    else if (st === 'todo') split.todo.push(s);
    else split.other.push(s);
  }
  return split;
}

/** Every commit-shaped evidence row on a suggestion (has a commit hash), for the split's links. */
export function commitEvidence(s: WsSuggestion): WsEvidence[] {
  return (s.evidence ?? []).filter((e) => !!e.commit);
}

/** A short, clickable label for a Reconcile commit-evidence link. */
export function evidenceLabel(e: WsEvidence): string {
  if (e.subject) return e.subject;
  if (e.commit) return e.commit.slice(0, 12);
  if (e.path) return e.path;
  return e.note || 'evidence';
}
/** The href for a commit-evidence link, when the agent supplied one. */
export function evidenceHref(e: WsEvidence): string | undefined {
  // Only http(s): an agent-mined URL (e.g. an origin's git remote) must never
  // render as a javascript:/data: href — that would be one-click XSS in the
  // owner's dashboard session.
  return e.url && /^https?:\/\//i.test(e.url) ? e.url : undefined;
}

export const isPendingSuggestion = (s: WsSuggestion | null | undefined) => s?.state === 'pending';

/** Is this objective the fixed Sweep-candidates inbox? Trusts the server flag, falls back to the id. */
export function isInbox(o: Pick<WsObjective, 'id' | 'inbox'> | null | undefined): boolean {
  return !!o && (o.inbox === true || o.id === WS_INBOX_ID);
}

/**
 * Partition the objectives queue into authored objectives and the single Sweep
 * inbox — the list view shows them in separate surfaces (authored above, the loud
 * candidates inbox below). Pure; order of `objectives` is preserved.
 */
export function partitionObjectives(objectives: Array<WsObjective> | null | undefined): {
  objectives: WsObjective[];
  inbox: WsObjective | null;
} {
  const list = objectives ?? [];
  return { objectives: list.filter((o) => !isInbox(o)), inbox: list.find((o) => isInbox(o)) ?? null };
}

/** Count of pending `new_objective` candidate drafts in a suggestion list (the inbox badge). */
export function candidateCount(suggestions: Array<WsSuggestion> | null | undefined): number {
  return (suggestions ?? []).filter((s) => s.state === 'pending' && s.kind === 'new_objective').length;
}

/**
 * A launched origin-agent run, as the UI records it for the persistent run banner.
 * Both agents are additive-only origin.run jobs — the record is a durable, dismissible
 * acknowledgement of what was kicked off and where its suggestions will land.
 */
export interface WsRunRecord {
  id: string;
  agent: WsAgent;
  origin: string;
  originLabel?: string;
  scope?: string;
  objective?: string;
  job?: string;
  ts: number;
}

/**
 * Where a launched run's suggestions will surface: Sweep parks its candidate drafts in
 * the fixed inbox; Reconcile emits status proposals onto the objective under review
 * (falling back to the inbox if — impossibly — it ran without one). Pure; tested.
 */
export function runResultRoute(run: Pick<WsRunRecord, 'agent' | 'objective'>): string {
  if (run.agent === 'reconcile' && run.objective) return '/plans/' + encodeURIComponent(run.objective);
  return '/plans/' + encodeURIComponent(WS_INBOX_ID);
}

/** A human sentence describing a launched run, for the persistent run card. Pure; tested. */
export function describeRun(run: Pick<WsRunRecord, 'agent' | 'origin' | 'originLabel' | 'scope'>): string {
  const where = run.originLabel || run.origin;
  const scope = run.scope ? ` · scope ${run.scope}` : '';
  return run.agent === 'sweep'
    ? `Looking through ${where}${scope} for plan ideas`
    : `Checking ${where}${scope} for commits that move this plan along`;
}

// ── HUB-UNIFY: the Workspace IS the Hub ──
/** Idea Hub docs surface as objectives under this id prefix (mirrors workspace.IH_PREFIX). */
export const WS_IH_PREFIX = 'ih.';
/** The adapted objective's index doc slug — the Hub doc's markdown body (mirrors IH_INDEX_SLUG). */
export const WS_IH_INDEX_SLUG = 'index';

/** The Plans route an old `/hub[/:id]` link lands on — one doc surface, no dead links. */
export function legacyHubRoute(docId?: string | null): string {
  if (!docId) return '/plans';
  const oid = WS_IH_PREFIX + docId;
  return `/plans/${encodeURIComponent(oid)}/${WS_IH_INDEX_SLUG}`;
}

/**
 * The Idea Hub doc id whose objective-manager thread docks on this objective, or
 * null. Only adapted Hub docs carry one — the thread tools are keyed by Hub doc id.
 */
export function objectiveThreadDocId(o: Pick<WsObjective, 'id' | 'source' | 'ideahub_id'> | null | undefined): string | null {
  if (!o) return null;
  if (o.ideahub_id) return o.ideahub_id;
  if (o.id?.startsWith(WS_IH_PREFIX) && o.id.length > WS_IH_PREFIX.length) return o.id.slice(WS_IH_PREFIX.length);
  return null;
}

/**
 * What a Hub plan's recorded result says, for the rail glyph: `done` (●) ONLY
 * for a green result; a loop that ended in error/stopped records green:false and
 * reads as `ended` (✕) — never done. Null when nothing was recorded.
 */
export function planOutcome(
  o: Pick<WsObjective, 'hub_state' | 'hub_result'> | null | undefined,
): { done: boolean; glyph: string; label: string } | null {
  const r = o?.hub_result;
  if (o?.hub_state === 'done' && r?.green === true) {
    const label = r.by === 'you' ? 'Marked done by you' : `Done — ${r.loop ?? 'its loop'} finished${r.summary ? ': ' + r.summary : ''}`;
    return { done: true, glyph: '●', label };
  }
  if (r && r.green !== true) {
    return { done: false, glyph: '✕', label: `Not done — ${r.loop ?? 'its loop'} ended${r.ended ? ' (' + r.ended.replace(/_/g, ' ') + ')' : ''}` };
  }
  return null;
}

// ── Plans: the board (Idea → Planned → Running → Done) ──

export type PlanColumn = 'idea' | 'planned' | 'running' | 'done';
/** The board's columns, each mapped from one rung of the doc status ladder. */
export const PLAN_COLUMNS: ReadonlyArray<{ id: PlanColumn; label: string; status: WsStatus }> = [
  { id: 'idea', label: 'Idea', status: 'draft' },
  { id: 'planned', label: 'Planned', status: 'todo' },
  { id: 'running', label: 'Running', status: 'in progress' },
  { id: 'done', label: 'Done', status: 'done' },
];
const COLUMN_BY_STATUS: Record<WsStatus, PlanColumn> = { draft: 'idea', todo: 'planned', 'in progress': 'running', done: 'done' };

/** The plain word for a status — the one vocabulary the board and the detail share. */
export function planStatusLabel(status: string | null | undefined): string {
  const col = COLUMN_BY_STATUS[normalizeStatus(status)];
  return PLAN_COLUMNS.find((c) => c.id === col)!.label;
}

/** Loop states that mean a loop is still working on (or waiting within) its run. */
const ACTIVE_LOOP_STATES = new Set<string>(['running', 'stopping', 'waiting_owner', 'needs_owner', 'needs_input']);
export const isActiveLoop = (l: LoopSummary) => ACTIVE_LOOP_STATES.has(honestState(l));

/**
 * The loops pointed at a plan, newest first, each joined to its live summary (null
 * when the loop is no longer listed — deleted, or on a machine that's offline).
 */
export function planLoops(
  o: Pick<WsObjective, 'loops'> | null | undefined,
  loops: LoopSummary[] | null | undefined,
): Array<{ name: string; loop: LoopSummary | null }> {
  const byName = new Map<string, LoopSummary>();
  for (const l of loops ?? []) if (!byName.has(l.name) || isActiveLoop(l)) byName.set(l.name, l);
  return [...(o?.loops ?? [])].reverse().map((name) => ({ name, loop: byName.get(name) ?? null }));
}

/** The plan's currently-working loop, if any. */
export function activePlanLoop(o: Pick<WsObjective, 'loops'> | null | undefined, loops: LoopSummary[] | null | undefined): LoopSummary | null {
  return planLoops(o, loops).find((x) => x.loop && isActiveLoop(x.loop))?.loop ?? null;
}

/** Which column a plan sits in: its status — except a plan whose loop is working reads Running. */
export function planColumn(o: Pick<WsObjective, 'status' | 'loops'>, loops: LoopSummary[] | null | undefined): PlanColumn {
  const byStatus = COLUMN_BY_STATUS[normalizeStatus(o.status)];
  if (byStatus !== 'done' && activePlanLoop(o, loops)) return 'running';
  return byStatus;
}

/** Group plans into the four board columns (the Sweep inbox is never a plan). Order kept. */
export function boardColumns(
  objectives: WsObjective[] | null | undefined,
  loops: LoopSummary[] | null | undefined,
): Record<PlanColumn, WsObjective[]> {
  const out: Record<PlanColumn, WsObjective[]> = { idea: [], planned: [], running: [], done: [] };
  for (const o of objectives ?? []) if (!isInbox(o)) out[planColumn(o, loops)].push(o);
  return out;
}

/** The plans the project switcher shows: '' = all; UNATTR = plans with no project. Inbox excluded. */
export function plansInScope(objectives: WsObjective[] | null | undefined, scope: string): WsObjective[] {
  return (objectives ?? []).filter((o) => !isInbox(o) && inProjectScope(o, scope));
}

/** Plans whose title matches a free-text query (case-insensitive, every word). */
export function searchPlans(objectives: WsObjective[], query: string): WsObjective[] {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return objectives;
  return objectives.filter((o) => {
    const hay = `${o.title} ${o.project ?? ''}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

/** A short plain-text preview of a plan's markdown (for the Run-as-loop confirmation). */
export function planPreview(md: string | null | undefined, max = 360): string {
  const text = String(md ?? '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+/gm, '')
    .replace(/\[( |x|X)\]\s*/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[*_`]+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return text.length > max ? text.slice(0, max - 1).trimEnd() + '…' : text;
}
