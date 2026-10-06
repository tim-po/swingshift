// /api/loops/docs* + /thread* — the Idea Hub living docs and the docked
// objective-manager thread.
import type { Client } from './client';
import { rejectErrorBody } from './issues';
import { PROJECT_UNATTR } from './projects';

export interface HubDocRow {
  id: string;
  project?: string;
  title?: string;
  state?: string;
  glyph?: string;
  loops?: string[];
  /** loop name → origin for each pointed loop (links /loops/<origin>/<name>). */
  loop_origins?: Record<string, string>;
  loop_count?: number;
  loop_rewrite?: boolean;
  has_result?: boolean;
  /** true only when the recorded result is green (the doc derives `done`, ●). */
  result_green?: boolean;
  updated?: number;
}
export interface DocResult {
  green?: boolean;
  verdict?: string;
  resolution?: string;
  loop?: string;
  commit?: string;
  ended?: string;
  summary?: string;
  finishReport?: string;
  by?: string;
  ts?: number;
}
export interface DocHistoryEntry {
  ts?: number;
  actor?: string;
  loop?: string;
  changed?: string[];
}
export interface HubDoc extends HubDocRow {
  body?: string;
  ready?: boolean;
  result?: DocResult | null;
  history?: DocHistoryEntry[];
  created?: number;
}
export interface DocsListResponse {
  docs: HubDocRow[];
  count: number;
  counts?: Record<string, number>;
  error?: string;
}
export interface DocResponse {
  ok?: boolean;
  doc: HubDoc;
}
export interface ThreadAction {
  type?: string;
  kind?: string;
  action?: string;
  label?: string;
  summary?: string;
  text?: string;
}
export interface ThreadMessage {
  role: 'you' | 'agent' | string;
  text?: string;
  status?: string;
  dispatched?: boolean;
  actions?: ThreadAction[];
  ts?: number;
  id?: string;
  task_id?: string;
  /** Read-time progress of a PENDING agent turn, from the real connect task:
   *  queued = dispatched, not yet claimed; working = the session claimed it. */
  task_status?: 'queued' | 'working' | 'unknown' | string;
  claimed_by?: string | null;
  claimed_at?: number | null;
}
export type ThreadStateName = 'unattached' | 'offline' | 'awaiting' | 'ready';
export interface SessionState {
  state: ThreadStateName | string;
  session?: string | null;
  can_send?: boolean;
  reason?: string;
}
export interface ThreadResponse {
  ok?: boolean;
  doc_id: string;
  thread: { doc_id: string; session?: string | null; messages: ThreadMessage[] };
  doc?: HubDoc;
  session_state: SessionState;
  dispatched?: boolean;
  task_id?: string;
  /** The session switcher's choices: every registered connector, real liveness. */
  sessions?: ThreadSessionOption[];
  /** Change revision: differs whenever anything the pane paints changed. */
  rev?: string;
  /** Turns still queued for the old session, canceled by a switch/detach. */
  canceled_tasks?: string[];
  /** Long-poll only: whether `rev` moved before the timeout. */
  changed?: boolean;
}
export interface ThreadSessionOption {
  id: string;
  runtime?: string | null;
  label?: string;
  live: boolean;
  idleSeconds?: number | null;
  capabilities?: string[];
  attached?: boolean;
  /** false = the thread points at an id no longer registered as a connector. */
  registered?: boolean;
}
/** One row of the Chat index (GET /api/loops/chat/threads). */
export interface ChatThreadRow {
  doc_id: string;
  title: string;
  project?: string | null;
  doc_state?: string | null;
  glyph?: string | null;
  has_thread: boolean;
  session?: string | null;
  state?: ThreadStateName | string;
  can_send?: boolean;
  message_count: number;
  pending: number;
  last?: { id?: string; role?: string; status?: string; ts?: number; text: string } | null;
  updated?: number | null;
}
export interface ChatIndexResponse {
  ok?: boolean;
  project?: string | null;
  project_known?: boolean;
  projects: { id: string; name: string }[];
  threads: ChatThreadRow[];
  count: number;
  error?: string;
}
/** The slice of /api/loops/sessions the thread's session picker needs. */
export interface HubSessionRow {
  id: string;
  runtime?: string;
  heartbeat?: { live?: boolean };
}
export interface DocUpdateInput {
  id: string;
  title?: string;
  body?: string;
  ready?: boolean;
  loop_rewrite?: boolean;
  /** Owner only: 'set' = Mark done (a green result), 'clear' = Reopen. */
  result?: 'set' | 'clear';
}

export const hubApi = (c: Client) => ({
  list: () => c.get<DocsListResponse>('/api/loops/docs'),
  get: async (id: string) => rejectErrorBody(await c.get<DocResponse>('/api/loops/doc/' + encodeURIComponent(id))),
  create: async (i: { project?: string; title?: string; body?: string }) => rejectErrorBody(await c.post<DocResponse>('/api/loops/docs/create', i)),
  update: async (i: DocUpdateInput) => rejectErrorBody(await c.post<DocResponse>('/api/loops/docs/update', i)),
  point: async (id: string) => rejectErrorBody(await c.post<{ ok?: boolean; loop?: string; doc?: HubDoc }>('/api/loops/docs/point', { id })),
  /** Move loops into a plan (their workstream; a loop is in at most one). `to` '' takes them out of every plan. */
  move: async (loops: string[], to: string) =>
    rejectErrorBody(await c.post<{ ok?: boolean; doc?: HubDoc | null; moved?: string[]; left?: string[] }>('/api/loops/docs/move', { loops, to })),
  thread: async (id: string) => rejectErrorBody(await c.get<ThreadResponse>('/api/loops/thread/' + encodeURIComponent(id))),
  attach: async (id: string, session: string) => rejectErrorBody(await c.post<ThreadResponse>('/api/loops/thread/attach', { id, session })),
  post: async (id: string, text: string) => rejectErrorBody(await c.post<ThreadResponse>('/api/loops/thread/post', { id, text })),
  sessions: () => c.get<{ sessions: HubSessionRow[] }>('/api/loops/sessions'),
  /** The Chat index for a REAL project ('' = every project) — no default project. */
  chatThreads: async (project: string) =>
    rejectErrorBody(await c.get<ChatIndexResponse>('/api/loops/chat/threads' + (project ? '?project=' + encodeURIComponent(project) : ''))),
  /** Long-poll fallback: resolves as soon as the thread's rev differs from `rev`. */
  wait: async (id: string, rev: string, timeout = 25) =>
    rejectErrorBody(
      await c.get<ThreadResponse>(
        '/api/loops/chat/wait/' + encodeURIComponent(id) + '?timeout=' + timeout + (rev ? '&rev=' + encodeURIComponent(rev) : ''),
      ),
    ),
});

/** SSE stream path for a thread (served by tracking_ui/chat_api.py). */
export const chatEventsPath = (id: string) => '/api/loops/chat/events/' + encodeURIComponent(id);

// ── Pure helpers ──

export const threadPending = (t: ThreadResponse | null | undefined) =>
  (t?.thread?.messages ?? []).some((m) => m?.role === 'agent' && m.status === 'pending');

export const HUB_THREAD_STATE: Record<string, { cls: string; label: string }> = {
  unattached: { cls: 'st-unatt', label: 'unattached' },
  offline: { cls: 'st-off', label: 'offline' },
  awaiting: { cls: 'st-await', label: 'working…' },
  ready: { cls: 'st-ready', label: 'ready' },
};
export const threadStateMeta = (s: string | undefined) => HUB_THREAD_STATE[s ?? ''] ?? HUB_THREAD_STATE.unattached;

/** Icon + label for an action the session reported it took this turn. */
export function threadActionChip(a: ThreadAction | null | undefined): { icon: string; label: string; type: string } {
  const t = a?.type || a?.kind || a?.action || '';
  const label = a?.label || a?.summary || a?.text || t || 'action';
  const icon = t.includes('doc') ? '✎' : t.includes('issue') ? '⚠' : t.includes('loop') ? '⟲' : '•';
  return { icon, label, type: t || 'action' };
}

/** Timeline row: newest-first, last 6; a non-"you" actor or a loop reads as a loop rewrite. */
export function docTimeline(h: DocHistoryEntry[] | undefined) {
  return (h ?? []).slice(-6).reverse().map((e) => {
    const actor = e.actor || 'you';
    const byLoop = actor !== 'you' || !!e.loop;
    return { byLoop, who: byLoop ? e.loop || actor : 'you', changed: (e.changed ?? []).join(', ') || 'edit', ts: e.ts };
  });
}

/** Parse one SSE `thread` event body; anything that isn't a thread view is null. */
export function parseThreadEvent(data: unknown): ThreadResponse | null {
  if (typeof data !== 'string' || !data) return null;
  try {
    const v = JSON.parse(data) as ThreadResponse;
    return v && typeof v === 'object' && v.thread && v.session_state && !(v as { error?: string }).error ? v : null;
  } catch {
    return null;
  }
}

/** Honest copy for a PENDING agent turn — only what the connect task really says. */
export function pendingTurnLabel(m: ThreadMessage, session?: string | null): { cls: string; text: string } {
  if (m.task_status === 'working') return { cls: 'working', text: `${m.claimed_by || session || 'the session'} is working on it` };
  if (m.task_status === 'queued') return { cls: 'queued', text: `queued — waiting for ${session || 'the session'} to pick it up` };
  return { cls: 'unknown', text: 'waiting for the session’s reply' };
}

/** A settled agent turn with no text: say why, never invent a reply. */
export function emptyTurnText(status: string | undefined): string {
  if (status === 'failed') return '(the session returned a failure)';
  if (status === 'canceled') return '(canceled before the session picked it up — no reply)';
  return '(the session returned no text)';
}

/** Idle age for a switcher option, e.g. "idle 4m". */
export function idleLabel(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return '';
  if (s < 90) return `idle ${Math.max(0, Math.round(s))}s`;
  if (s < 5400) return `idle ${Math.round(s / 60)}m`;
  if (s < 172800) return `idle ${Math.round(s / 3600)}h`;
  return `idle ${Math.round(s / 86400)}d`;
}

/** The switcher's choices: the thread payload's own roster when the server sends one,
 *  else the legacy /api/loops/sessions live rows (attached-but-gone kept visible). */
export function threadSessionChoices(
  th: ThreadResponse | null | undefined,
  legacy: HubSessionRow[] | undefined,
): ThreadSessionOption[] {
  const attached = th?.session_state?.session || th?.thread?.session || '';
  if (Array.isArray(th?.sessions)) return th!.sessions!.filter((o) => o && o.id);
  const live: ThreadSessionOption[] = hubLiveSessions(legacy).map((s) => ({ id: s.id, runtime: s.runtime, live: true, attached: s.id === attached, registered: true }));
  if (attached && !live.some((o) => o.id === attached)) live.push({ id: attached, live: false, attached: true, registered: false });
  return live;
}

/** The Chat index query for the active project scope. '' = every project; the
 *  "no project" scope reads every thread and keeps only unattributed docs. There
 *  is no default project — an unknown one comes back `project_known: false`. */
export function chatScopeQuery(scope: string): { project: string; keep(r: ChatThreadRow): boolean } {
  if (scope === PROJECT_UNATTR) return { project: '', keep: (r) => !r.project };
  return { project: scope, keep: () => true };
}

/** Badge for a Chat index row — only what the thread summary really says. */
export function chatRowBadge(r: ChatThreadRow): { cls: string; label: string } {
  if (!r.has_thread) return { cls: 'st-unatt', label: 'no thread yet' };
  if (r.pending > 0) return { cls: 'st-await', label: r.pending > 1 ? `${r.pending} replies pending` : 'reply pending' };
  return threadStateMeta(r.state);
}

export const hubLiveSessions = (s: HubSessionRow[] | undefined) => (s ?? []).filter((x) => x?.heartbeat?.live);
