// /api/loops/issues* — the project-scoped inbox stream, drill-in, filing, triage.
import { errorBodyMessage, type Client } from './client';

export type IssueStatus = 'open' | 'snoozed' | 'resolving' | 'resolved' | 'dismissed';
export const ISSUE_KINDS = ['bug', 'follow-up', 'blocked', 'idea-overflow', 'crash'] as const;

export interface Issue {
  id: string;
  ts?: number | null;
  status?: string;
  project?: string;
  kind?: string;
  source?: string;
  severity?: string;
  title?: string;
  body?: string;
  loop?: string | null;
  /** The origin (host) `loop` lives on — links go to /loops/<origin>/<loop>. */
  origin?: string | null;
  run?: number | string | null;
  ended?: string | null;
  failing_agent?: string | null;
  failing_phase?: string | null;
  failing_status?: string | null;
  transcript_path?: string | null;
}
export interface IssuesListResponse {
  issues: Issue[];
  count: number;
  counts?: Record<string, number>;
  error?: string;
}
export interface GuardianAttempt {
  ts?: number | null;
  agent?: string;
  status?: string;
  note?: string;
}
export interface IssueDetail extends Issue {
  guardian_attempts?: GuardianAttempt[];
  log_slice?: string;
  error?: string;
}
export interface IssueDetailResponse {
  issue: IssueDetail;
  transcript_exists?: boolean;
  transcript_tail?: string;
}
export interface IssueFileInput {
  title: string;
  kind: string;
  project?: string;
  body?: string;
}
export interface IssueTriageInput {
  id: string;
  status: string;
  note?: string;
}

/** Some handlers still answer 200 with `{error}` (failed writes now use 4xx, which
 *  the client throws with the same message); surface either as a thrown Error. */
export function rejectErrorBody<T>(r: T): T {
  const msg = errorBodyMessage(r);
  if (msg) throw new Error(msg);
  return r;
}

export const issuesApi = (c: Client) => ({
  list: () => c.get<IssuesListResponse>('/api/loops/issues'),
  get: async (id: string) => rejectErrorBody(await c.get<IssueDetailResponse & { error?: string }>('/api/loops/issue/' + encodeURIComponent(id))),
  file: async (i: IssueFileInput) =>
    rejectErrorBody(await c.post<{ ok?: boolean; issue?: Issue; error?: string }>('/api/loops/issues/file', { ...i, source: 'you' })),
  triage: async (t: IssueTriageInput) =>
    rejectErrorBody(await c.post<{ ok?: boolean; error?: string; legal?: string[] }>('/api/loops/issues/triage', t)),
});

// ── Pure helpers ──

export const OPEN_WORK = new Set(['open', 'snoozed', 'resolving']);
export const issueStatus = (it: Pick<Issue, 'status'>) => (it.status || 'open').toLowerCase();
export const isOpenWork = (it: Pick<Issue, 'status'>) => OPEN_WORK.has(issueStatus(it));

/** Chip tone per lifecycle status (NOT the raw engine `ended`). */
export function issueStatusTone(status: string | undefined): '' | 'ok' | 'gup' | 'err' {
  const s = (status || 'open').toLowerCase();
  return s === 'resolved' ? 'ok' : s === 'snoozed' ? 'gup' : s === 'crash' ? 'err' : '';
}

/** Honest source: engine / you / loop <name>; absent reads "unattributed". */
export function issueSourceLabel(src: string | undefined | null): string {
  const s = (src || '').trim();
  if (!s) return 'unattributed';
  if (s.startsWith('loop:')) return 'loop ' + s.slice(5);
  return s;
}

const KIND_LABELS: Record<string, string> = {
  bug: 'Bug',
  'follow-up': 'Follow-up',
  blocked: 'Blocked',
  'idea-overflow': 'Extra idea',
  crash: 'Crash',
};
/** Sentence-case name for an issue kind ("idea-overflow" → "Extra idea"). */
export const issueKindLabel = (kind: string | undefined | null): string => {
  const k = (kind || 'bug').toLowerCase();
  return KIND_LABELS[k] ?? k.charAt(0).toUpperCase() + k.slice(1).replace(/[-_]/g, ' ');
};

/** Who filed it, in plain words: "Loopyard", "you", "loop nightly-triage". */
export function issueFromLabel(src: string | undefined | null): string {
  const s = (src || '').trim();
  if (s === 'engine') return 'Loopyard';
  if (!s) return '';
  return issueSourceLabel(s);
}

/**
 * The one status vocabulary for an issue — list row and detail page say the same
 * word. `state` keys the shared Badge colour (`.b-<state>`).
 */
export function issueStatusBadge(status: string | undefined): { state: string; label: string } {
  switch ((status || 'open').toLowerCase()) {
    case 'snoozed':
      return { state: 'saved', label: 'Snoozed' };
    case 'resolving':
      return { state: 'running', label: 'Being fixed' };
    case 'resolved':
      return { state: 'finished', label: 'Resolved' };
    case 'dismissed':
      return { state: 'stopped', label: 'Dismissed' };
    default:
      return { state: 'waiting_owner', label: 'Open' };
  }
}

export type IssueAction ={ status: string; label: string; primary?: boolean };

/** Legal triage actions per status (mirrors the backend state machine). `point` = Point a loop. */
export function issueActions(it: Pick<Issue, 'status'>): IssueAction[] {
  const s = issueStatus(it);
  const a: IssueAction[] = [];
  if (s === 'open' || s === 'snoozed') a.push({ status: 'point', label: 'Fix with a loop', primary: true });
  if (s === 'open') a.push({ status: 'snoozed', label: 'Snooze' });
  if (s === 'snoozed') a.push({ status: 'open', label: 'Resume' });
  if (s === 'open' || s === 'snoozed' || s === 'resolving') a.push({ status: 'resolved', label: 'Resolve' }, { status: 'dismissed', label: 'Dismiss' });
  if (s === 'resolving') a.push({ status: 'open', label: 'Stop' });
  if (s === 'resolved' || s === 'dismissed') a.push({ status: 'open', label: 'Reopen' });
  return a;
}

/** The goal a "Point a loop" composer is seeded with. */
export function issuePointGoal(it: Pick<Issue, 'kind' | 'title' | 'loop' | 'id' | 'body'>): string {
  const goal = it.kind === 'crash' ? 'Recover from: ' + (it.title || it.loop || it.id) : 'Resolve issue: ' + (it.title || it.id);
  const body = (it.body ?? '').toString().trim();
  return body ? goal + '\n\n' + body : goal;
}
