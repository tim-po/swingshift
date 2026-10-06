// /api/loops/{name}/* calls + types + pure helpers for the loop DETAIL pane.
// Shapes follow tracking_ui/loops_panel.py (the source of truth).
import type { Client } from './client';
import { honestState } from './loops';
import type { LoopSummary } from './types';

// ── Types ────────────────────────────────────────────────────────────────────

/** One row of the scheduler's turn-by-turn log (`recent`). */
export interface LoopEvent {
  ts: number;
  loop?: string;
  agent: string;
  status?: string;
  note?: string | null;
  seq?: number | null;
  turn?: number | null;
  kind?: string | null; // 'machinery' for engine rows
  phase?: string | null; // 'guardian' for recovery attempts
  valid?: boolean;
}

export interface LiveAgent {
  agent: string;
  role?: string;
  phase?: string;
  kind?: string;
  since?: number | null;
}

export interface PendingInput {
  text: string;
  ts?: number;
}

export interface ResultFile {
  path: string;
  size?: number;
  kind?: string;
}

export interface ResultFiles {
  curated?: { path: string; preview: string; truncated?: boolean; override?: boolean } | null;
  files?: ResultFile[];
  count?: number;
}

export interface LoopDetail extends LoopSummary {
  slug?: string | null;
  kind?: string | null;
  parent?: string | null;
  question?: string | null;
  guardian_alert?: string | null;
  winddown_turns?: number | null;
  retired?: string[];
  recent?: LoopEvent[];
  finish_report?: string | null;
  answer_note?: string | null;
  git_pointer?: { commit: string; branch?: string | null } | null;
  result_files?: ResultFiles | null;
  live?: LiveAgent[];
  live_updated?: number | null;
  input_pending?: PendingInput[];
  sub_agents?: Record<string, string>;
  sub_names?: Record<string, string>;
  error?: string;
}

export interface Convergence {
  value: 'Delivered' | 'Converging' | 'Aligning' | 'Stalled' | 'Needs you' | (string & {});
  reason?: string;
}

export interface TurnBar {
  used?: number | null;
  limit?: number | null;
  winddown_in?: number | null;
  winddown_turns?: number | null;
  in_winddown?: boolean;
  running?: boolean;
}

/**
 * Better-UX #1 — the ONE loop state (the old convergence chip folded in).
 * phase config → config · running → aligning|converging|stalled|needs-assistance ·
 * finished → delivered|error|stopped.
 */
export type LoopStatusValue =
  | 'config' | 'aligning' | 'converging' | 'stalled' | 'needs-assistance' | 'delivered' | 'error' | 'stopped';
export interface LoopStatus {
  value: LoopStatusValue | (string & {});
  phase?: 'config' | 'running' | 'finished' | (string & {});
  reason?: string;
}

/** Better-UX #2 — one dot per turn (oldest → newest); a trailing `current` dot while live. */
export interface TurnDot {
  seq: number;
  turn?: number | null;
  status?: string | null;
  dot: 'ok' | 'done' | 'attention' | 'active' | (string & {});
  current?: boolean;
  /** Better-UX #6 — the agent's OWN note for that turn (its steering note). */
  note?: string | null;
  ts?: number | null;
}

export interface RosterCard {
  agent: string;
  /** Better-UX #7 — shown when the agent is opened. */
  personality?: string | null;
  goal?: string | null;
  turnDots?: { dots: TurnDot[]; hidden?: number } | null;
  role?: string;
  displayRole?: string;
  isManager?: boolean;
  owns?: string | null;
  statusDot?: 'active' | 'done' | 'attention' | 'idle' | (string & {});
  doingNow?: string | null;
  lastReport?: { status?: string; note?: string | null; ts?: number } | null;
}

export interface Verdict {
  value: string;
  green?: boolean;
  resolved?: boolean;
  running?: boolean;
  reason?: string;
  canRate?: boolean;
  ownerAction?: { kind: string; label?: string; commit?: string } | null;
}

export interface DispositionCurrent {
  verb: 'good' | 'ok' | 'bad' | (string & {});
  note?: string | null;
  nextAction?: string | null;
}

/** Better-UX #3 — one entry of the `<output>/deliverables/` folder (or an `artifact=`-marked file). */
export interface DeliverableItem {
  name: string;
  rel?: string;
  path: string;
  exists?: boolean;
  isDir?: boolean;
  size?: number | null;
  location?: 'folder' | 'output' | 'external' | (string & {});
  marked?: boolean;
  markedBy?: string[];
  seq?: number | null;
  status?: 'deleted' | null | (string & {});
}
export interface DeliverablesFolder {
  folder: string;
  exists?: boolean;
  items: DeliverableItem[];
  results?: number;
  missing?: number;
  hidden?: number;
}

/** Better-UX #5 — the automatic negative status (never a click). */
export interface AutoStatus {
  value: 'removed' | 'deleted' | (string & {});
  reason?: string;
}

export interface Resolution {
  name?: string;
  project?: string | null;
  status?: string;
  verdict?: Verdict | null;
  resolution?: string | null;
  handles?: {
    commit?: string | null;
    commitShort?: string | null;
    /** Should be a string path; older payloads carried an object — treat as absent. */
    deliverable?: unknown;
    artifacts?: { kind: string; path: string; exists?: boolean }[];
    deliverables?: DeliverablesFolder | null;
  } | null;
  proof?: { tests?: string | null; turns?: number | null; winddown?: number | null; seconds?: number | null; signals?: string[] } | null;
  disposition?: { offered?: boolean; current?: DispositionCurrent | null; autoStatus?: AutoStatus | null } | null;
  error?: string;
}

export interface TeamRoom {
  name: string;
  goal?: string;
  project?: string | null;
  single_agent?: boolean;
  /** The single loop STATE — render this, not `convergence` (kept for back-compat). */
  state?: LoopStatus | null;
  convergence?: Convergence | null;
  turn?: TurnBar | null;
  managerRead?: string | null;
  roster: RosterCard[];
  run?: { state?: string; steer?: SteerInfo | null };
  origin?: string;
  resolution?: Resolution | null;
  error?: string;
}

export interface LoopConfigResponse {
  name: string;
  origin?: string;
  config?: { steps?: Record<string, { goal?: string; role?: string } | undefined>; stepOrder?: (string | string[])[] } & Record<string, unknown>;
  error?: string;
}

export interface AgentReport {
  seq: number;
  status?: string;
  note?: string | null;
  ts?: number;
}
export interface AgentReportsResponse {
  name: string;
  agent: string;
  reports: AgentReport[];
  error?: string;
}

export interface TurnDetail {
  report?: { status?: string; note?: string | null } | null;
  prompt?: string | null;
  transcript_tail?: string | null;
  error?: string;
}

export interface FileGroup {
  dir: string;
  label?: string;
  files: ResultFile[];
}
export interface FilesResponse {
  name: string;
  artifacts: FileGroup[];
  count: number;
  capped?: boolean;
  error?: string;
}

/** `known` false: nothing of the loop resolves in this repo (its work lives elsewhere), so no count is a guess. */
export interface DeliveredResponse {
  name: string;
  known: boolean;
  src: 'branch' | 'commit' | 'none';
  delivered: Record<string, number>;
  files: number;
  error?: string;
}

export interface DispositionsResponse {
  ok?: boolean;
  project?: string | null;
  good: number;
  ok_count: number;
  bad: number;
  rated: number;
  total: number;
  goodRate?: number | null;
  loops: { loop: string; verb: string; note?: string | null }[];
  error?: string;
}

/** Better-UX #6 — where a Steer is (mirrors Brief: gentle stop → manager hand-off → restart). */
export interface SteerInfo {
  conversation?: string;
  phase?: 'stopping' | 'handoff' | 'ready' | 'restarted' | 'error' | (string & {});
  attach?: string;
  sid?: string;
  resumed?: boolean;
  turnsUsed?: number;
  error?: string;
  requestedAt?: number;
  restartedAt?: number;
}
export interface SteerStatus {
  name?: string;
  active?: boolean;
  steer?: SteerInfo | null;
  error?: string;
}

export type LoopAction =
  | 'start' | 'stop' | 'reply' | 'archive' | 'unarchive' | 'analyze' | 'save_registry'
  | 'steer' | 'steer_resume';
export interface ActionBody {
  action: LoopAction;
  host?: string;
  text?: string;
  reply?: string;
  note?: string;
}
export interface ActionResult {
  ok?: boolean;
  error?: string;
  state?: string;
  pending?: number;
  [k: string]: unknown;
}

/** The result of spawning/resuming a real tmux briefing session (adopt-the-manager). */
export interface BriefResult {
  ok?: boolean;
  error?: string;
  sid?: string;
  attach?: string;
  slug?: string;
  window?: string;
  resumed?: boolean;
  [k: string]: unknown;
}

export interface AnalysisResult extends ActionResult {
  name?: string;
  ai_overview?: string;
  telemetry?: {
    per_agent?: Record<string, { turns: number; statuses?: Record<string, number> }>;
    turn_gap_min?: { n: number; min: number; median: number; mean: number; max: number };
    note?: string;
    [k: string]: unknown;
  };
}

/** good/ok/bad is the ONLY manual rating — ship/keep are retired (removed/deleted are automatic). */
export type DispositionVerb = 'good' | 'ok' | 'bad';
export interface DispositionBody {
  verb: DispositionVerb;
  source: 'behavior' | 'explicit';
  note?: string;
}

/** Run observability — the owner rating and the independent loop-analyst score.
 * Two SEPARATE fields, each with its own honest `pending` flag; never blended. */
export type GoodnessGrade = 'good' | 'ok' | 'bad';
export interface GoodnessOwner {
  verb: GoodnessGrade | (string & {});
  note?: string | null;
  at?: number | null;
  source?: string | null;
}
export interface GoodnessAnalyst {
  grade: GoodnessGrade | (string & {});
  score: number;
  rationale?: string | null;
  factors?: { factor: string; delta: number }[];
  verdict?: string | null;
  analyst?: string | null;
  at?: number | null;
  runStarted?: unknown;
}
export interface Goodness {
  name: string;
  runStarted?: unknown;
  owner: GoodnessOwner | null;
  analyst: GoodnessAnalyst | null;
  pending: { owner: boolean; analyst: boolean };
  ownerRateable?: boolean;
  table?: { owner: string; analyst: string };
  /** Why the analyst score is absent (e.g. the run is still live). */
  analystReason?: string | null;
  error?: string;
}

/** Run observability — one capped reasoning gist per agent turn (never full I/O). */
export interface ThoughtEntry {
  ts: number;
  loop?: string;
  agent: string;
  turn: number;
  status?: string;
  gist: string;
  source?: 'gist' | 'note' | (string & {});
  truncated?: boolean;
}
export interface ThoughtlogResponse {
  name: string;
  cap: number;
  count: number;
  agents: Record<string, number>;
  entries: ThoughtEntry[];
  error?: string;
}

// ── API ──────────────────────────────────────────────────────────────────────

const seg = encodeURIComponent;
const q = (o: Record<string, string | number>) =>
  Object.entries(o).map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join('&');

/**
 * Phase 5 owner scope: append `owner=` to a per-loop GET so the backend refuses (403)
 * a cross-owner read. No owner → the URL is byte-identical to the unscoped (master) one.
 */
export const withOwner = (url: string, owner?: string) =>
  owner ? `${url}${url.includes('?') ? '&' : '?'}${q({ owner })}` : url;

export const loopDetailApi = (c: Client) => ({
  detail: (name: string, host = 'local', tail = 150, owner?: string) =>
    c.get<LoopDetail>(withOwner(`/api/loops/${seg(name)}?${q({ host, tail })}`, owner)),
  teamRoom: (name: string, host = 'local', owner?: string) =>
    c.get<TeamRoom>(withOwner(`/api/loops/${seg(name)}/teamroom?${q({ host })}`, owner)),
  config: (name: string, owner?: string) => c.get<LoopConfigResponse>(withOwner(`/api/loops/${seg(name)}/config`, owner)),
  resolution: (name: string, host = 'local', owner?: string) =>
    c.get<Resolution>(withOwner(`/api/loops/${seg(name)}/resolution?${q({ host })}`, owner)),
  agentReports: (name: string, agent: string, host = 'local', owner?: string) =>
    c.get<AgentReportsResponse>(withOwner(`/api/loops/${seg(name)}/agent-reports?${q({ agent, host })}`, owner)),
  turn: (name: string, agent: string, seq: number, host = 'local', owner?: string) =>
    c.get<TurnDetail>(withOwner(`/api/loops/${seg(name)}/turn?${q({ agent, seq, host })}`, owner)),
  files: (name: string, owner?: string) => c.get<FilesResponse>(withOwner(`/api/loops/${seg(name)}/files`, owner)),
  /** Extension counts of the files the loop delivered in git (the city's building style). */
  delivered: (name: string, owner?: string) => c.get<DeliveredResponse>(withOwner(`/api/loops/${seg(name)}/delivered`, owner)),
  /** Fleet-wide when `project` is empty (never builds `/projects//dispositions`). */
  dispositions: (project?: string | null) =>
    c.get<DispositionsResponse>(project ? `/api/loops/projects/${seg(project)}/dispositions` : '/api/loops/dispositions'),
  action: (name: string, body: ActionBody) => c.post<ActionResult>(`/api/loops/${seg(name)}/action`, body),
  /**
   * Spawn the REAL tmux adopt-the-manager briefing session (loop_brief_session) and
   * return its attach command. This is the pre-redesign per-loop Brief behaviour,
   * restored by owner request: unlike the daemon-free native chat, spawning the
   * session is itself the smoke-test that the daemon/tmux substrate is healthy.
   */
  brief: (name: string) => c.post<BriefResult>(`/api/loops/${seg(name)}/brief`, {}),
  /** Debrief — resume the SAME manager (live briefing, or the one a prior run adopted). */
  debrief: (name: string) => c.post<BriefResult>(`/api/loops/${seg(name)}/debrief`, {}),
  analyze: (name: string) => c.post<AnalysisResult>(`/api/loops/${seg(name)}/action`, { action: 'analyze' }),
  /** Poll a Steer's phase until `ready`, then show the attach (as Brief does). */
  steerStatus: (name: string) => c.post<SteerStatus>(`/api/loops/${seg(name)}/action`, { action: 'steer_status' }),
  disposition: (name: string, body: DispositionBody) => c.post<ActionResult>(`/api/loops/${seg(name)}/disposition`, body),
  /** Owner rating + analyst score, side by side (never blended). `refresh` re-runs the analyst pass. */
  goodness: (name: string, refresh = false, owner?: string) =>
    c.get<Goodness>(withOwner(`/api/loops/${seg(name)}/goodness${refresh ? '?refresh=1' : ''}`, owner)),
  /** The capped one-line-per-turn thought-log, oldest-first. */
  thoughtlog: (name: string, opts: { agent?: string; limit?: number; owner?: string } = {}) => {
    const p: Record<string, string | number> = {};
    if (opts.agent) p.agent = opts.agent;
    if (opts.limit) p.limit = opts.limit;
    if (opts.owner) p.owner = opts.owner;
    const qs = q(p);
    return c.get<ThoughtlogResponse>(`/api/loops/${seg(name)}/thoughtlog${qs ? '?' + qs : ''}`);
  },
});

/** Plain links (opened in a new tab / downloaded), so they need the API base. */
export const fileUrl = (base: string, loop: string, path: string, download = false, owner?: string) =>
  withOwner(`${base.replace(/\/+$/, '')}/api/loops/${seg(loop)}/file?path=${seg(path)}${download ? '&download=1' : ''}`, owner);
export const downloadAllUrl = (base: string, loop: string, owner?: string) =>
  withOwner(`${base.replace(/\/+$/, '')}/api/loops/${seg(loop)}/download`, owner);

// ── Pure view-model helpers ──────────────────────────────────────────────────

export const RUNNING_STATES = new Set(['running', 'stopping', 'waiting_owner', 'needs_owner']);
export const FINISHED_STATES = new Set(['finished', 'complete', 'stopped', 'error', 'needs_owner']);

export type Role = 'manager' | 'worker' | 'input_provider' | 'loop' | 'service' | '';
export function roleOf(d: Pick<LoopSummary, 'team'>, agent: string): Role {
  const t = d.team ?? {};
  if (t.manager === agent) return 'manager';
  if (t.workers?.includes(agent)) return 'worker';
  if (t.inputs?.includes(agent)) return 'input_provider';
  if (t.subloops?.includes(agent)) return 'loop';
  if (agent === '__service') return 'service';
  return '';
}

/** First non-blank line, with markdown heading hashes stripped. */
export function firstLine(s: string | null | undefined): string {
  if (!s) return '';
  for (const ln of String(s).split('\n')) {
    const t = ln.trim().replace(/^#+\s*/, '');
    if (t) return t;
  }
  return '';
}

const greenLine = (t: string) => /✅|✔|☑|\bfinished\b|\bcomplete(d)?\b|stopped by owner/i.test(t);

/** §3.2 — the ONE honest failure reason: never a ✅/"finished" headline. */
export function failReason(d: Pick<LoopDetail, 'finish_report' | 'summary'>): string {
  const lines = [d.finish_report || '', d.summary || '']
    .join('\n')
    .split('\n')
    .map((l) => l.trim().replace(/^#+\s*/, ''))
    .filter(Boolean);
  return (
    lines.find((l) => /error/i.test(l) && !greenLine(l)) ??
    lines.find((l) => !greenLine(l)) ??
    'ended in an error — no result produced.'
  );
}

/** A roster card's per-loop responsibility: the first sentence of its step goal, capped. */
export function respFromGoal(goal: string | null | undefined): string {
  const g = (goal || '').trim();
  if (!g) return '';
  let s = g.split(/(?<=[.!?])\s+/)[0] || g;
  if (s.length > 110) s = s.slice(0, 108).replace(/\s+\S*$/, '') + '…';
  return s;
}

/** Overlay per-loop charters from the config onto the roster; keep backend `owns` otherwise. */
export function withResponsibilities(tr: TeamRoom, cfg: LoopConfigResponse | null | undefined): TeamRoom {
  const steps = cfg?.config?.steps ?? {};
  return {
    ...tr,
    roster: tr.roster.map((a) => {
      const resp = respFromGoal(steps[a.agent]?.goal);
      return resp ? { ...a, owns: resp } : a;
    }),
  };
}

const LOOP_STATUS_LABEL: Record<string, string> = {
  config: 'config',
  aligning: 'aligning',
  converging: 'converging',
  stalled: 'stalled',
  'needs-assistance': 'needs you',
  delivered: 'delivered',
  error: 'needs a look',
  stopped: 'stopped',
};
/** Legacy chip value → the new single-state value (older engines serve only `convergence`). */
const CONV_TO_STATUS: Record<string, LoopStatusValue> = {
  Delivered: 'delivered',
  Converging: 'converging',
  Aligning: 'aligning',
  Stalled: 'stalled',
  'Needs you': 'needs-assistance',
};

/** The ONE loop state for the header. Prefers the engine's `state`; derives from the chip on older payloads. */
export function loopStatusOf(tr: TeamRoom | null | undefined): LoopStatus | null {
  if (!tr) return null;
  if (tr.state?.value) return tr.state;
  const c = tr.convergence;
  const run = tr.run?.state;
  if (!run || run === 'saved') return { value: 'config', phase: 'config', reason: 'not started' };
  const v = c?.value ? CONV_TO_STATUS[c.value] : undefined;
  return v ? { value: v, phase: v === 'delivered' ? 'finished' : 'running', reason: c?.reason } : null;
}

/**
 * Is the loop actually running right now? The single STATE decides (its `phase`);
 * with no team room, fall back to the run state. Leftover `live` rows or a stale
 * `turn.running` on a finished loop must never paint a second "running" status.
 */
export function loopIsRunning(status: LoopStatus | null | undefined, runState?: string | null): boolean {
  if (status) return status.phase === 'running';
  return RUNNING_STATES.has(runState || '');
}

export const loopStatusLabel = (s: LoopStatus) => LOOP_STATUS_LABEL[s.value] ?? String(s.value).replace(/[-_]/g, ' ');

/** CSS tone: calm by default — only error reads red; stalled/needs-you read amber. */
export function loopStatusTone(s: LoopStatus): 'green' | 'live' | 'amber' | 'red' | 'muted' {
  switch (s.value) {
    case 'delivered':
      return 'green';
    case 'aligning':
    case 'converging':
      return 'live';
    case 'stalled':
    case 'needs-assistance':
      return 'amber';
    case 'error':
      return 'red';
    default:
      return 'muted';
  }
}

/**
 * ONE status vocabulary for the list AND the detail header. The badge word always
 * comes from the run state (`stateLabel`), so the same loop never reads "finished"
 * in the list and "delivered" on its page. The team-room STATE only refines it where
 * the run state can't tell: a running loop that is asking for you reads "needs you".
 * Returns the state key for `<Badge state>` (colour class + label).
 */
export function loopBadgeState(d: LoopSummary, status?: LoopStatus | null): string {
  const st = honestState(d);
  if (st === 'error') return st;
  if (status?.value === 'needs-assistance' && status.phase === 'running') {
    return st === 'waiting_owner' || st === 'needs_owner' ? st : 'needs_owner';
  }
  if (status?.value === 'error' && status.phase === 'finished') return 'error';
  return st;
}

const LIVE_PHASE: Record<string, string> = {
  aligning: 'getting aligned',
  converging: 'converging',
  stalled: 'stalled',
  'needs-assistance': 'waiting for you',
};
/** A running loop's sub-phase in plain words ('' when not running / unknown). */
export const livePhaseLabel = (s: LoopStatus | null | undefined): string =>
  s && s.phase === 'running' ? LIVE_PHASE[s.value] ?? '' : '';

/**
 * The goal to show in the header. The list payload clips the goal at 220 chars
 * (mid-word); prefer the team room's full goal, else end the clipped one on a word
 * boundary with an ellipsis instead of cutting a word in half.
 */
export function goalText(full: string | null | undefined, clipped: string | null | undefined, clipAt = 219): string {
  const f = (full || '').trim();
  if (f) return f;
  const c = (clipped || '').trim();
  if (c.length < clipAt) return c;
  const cut = c.replace(/\s+\S*$/, '');
  return (cut.length > clipAt * 0.6 ? cut : c).replace(/[\s,;:.–—-]+$/, '') + '…';
}

/** An agent's per-turn dots; a pre-array engine yields one dot from `statusDot`. */
export function turnDotsOf(c: RosterCard): { dots: TurnDot[]; hidden: number } {
  const td = c.turnDots;
  if (td && Array.isArray(td.dots)) return { dots: td.dots, hidden: td.hidden ?? 0 };
  const d = c.statusDot && c.statusDot !== 'idle' ? c.statusDot : '';
  return { dots: d ? [{ seq: 0, dot: d, current: d === 'active' }] : [], hidden: 0 };
}

export const STEER_WAITING = new Set(['stopping', 'handoff', 'ready']);

/**
 * Each agent's finished-turn count in the CURRENT run, from the team room's dots
 * (their `seq` is the per-run turn index — the one the prompt files are keyed on).
 * Agents without a dot array are left out.
 */
export function agentTurnCounts(tr: TeamRoom | null | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  for (const c of tr?.roster ?? []) {
    const dots = c.turnDots?.dots;
    if (!Array.isArray(dots)) continue;
    const done = dots.filter((d) => !d.current);
    out[c.agent] = done.length ? Math.max(...done.map((d) => d.seq)) : 0;
  }
  return out;
}

/**
 * The turn index to open for each engine-log row — or null when the row can't be
 * opened.
 *
 * Why: the log's `seq` is counted over the WHOLE status history (every run of this
 * loop), but the engine restarts each agent's counter on every run and names the
 * prompt file `prompts/<agent>-NNN.txt` with the per-run index. After a Rerun the
 * two drift apart (`app_builder` seq 2 → no `app_builder-002.txt`), while the team
 * room's dots — already per-run — open fine. So re-base each row onto the run:
 *   - rows from before this run started can't be opened (their prompts were reused);
 *   - with the team room's per-run counts: offset = newest seq − per-run count;
 *   - else, when the tail reaches back past the run start (so every in-run row is
 *     present): offset = the agent's first in-run seq − 1;
 *   - else keep the served seq (best effort).
 */
export function runTurnSeqs(
  rows: LoopEvent[],
  opts: { started?: number | null; agentTurns?: Record<string, number> } = {},
): (number | null)[] {
  const started = typeof opts.started === 'number' && opts.started > 0 ? opts.started : null;
  const inRun = (e: LoopEvent) => started == null || typeof e.ts !== 'number' || e.ts >= started;
  const isTurn = (e: LoopEvent) => e.kind !== 'machinery' && !!e.agent && typeof e.seq === 'number' && e.seq > 0;
  const reachesStart = started != null && rows.some((e) => typeof e.ts === 'number' && e.ts < started);
  const first: Record<string, number> = {};
  const last: Record<string, number> = {};
  for (const e of rows) {
    if (!isTurn(e) || !inRun(e)) continue;
    first[e.agent] ??= e.seq!;
    last[e.agent] = Math.max(last[e.agent] ?? 0, e.seq!);
  }
  const offset = (agent: string): number => {
    const n = opts.agentTurns?.[agent];
    if (typeof n === 'number' && n > 0 && last[agent] != null) return Math.max(0, last[agent] - n);
    if (reachesStart && first[agent] != null) return first[agent] - 1;
    return 0;
  };
  return rows.map((e) => {
    if (!isTurn(e)) return null;
    if (!inRun(e)) return null;
    const s = e.seq! - offset(e.agent);
    return s > 0 ? s : null;
  });
}

/** The deliverables folder, or null when the engine serves none. */
export function deliverablesOf(r: Resolution | null | undefined): DeliverablesFolder | null {
  const dl = r?.handles?.deliverables;
  return dl && typeof dl.folder === 'string' && Array.isArray(dl.items) ? dl : null;
}

const CONV_CLASS: Record<string, string> = {
  Delivered: 'delivered',
  Converging: 'converging',
  Aligning: 'aligning',
  Stalled: 'stalled',
  'Needs you': 'needsyou',
};
export const convTone = (c: Convergence | null | undefined) => (c?.value && CONV_CLASS[c.value]) || 'aligning';

/** `verdict.green` is the ONLY green gate. */
export function resolutionTone(v: Verdict | null | undefined): 'green' | 'red' | 'amber' {
  if (v?.green) return 'green';
  return v && (v.value === 'FAILED' || v.value === 'ABANDONED—needs you') ? 'red' : 'amber';
}

/** Tests read red on "red"/"fail" or a partial pass count like 3/5. */
export function looksRed(t: string | null | undefined): boolean {
  if (!t) return false;
  const s = String(t).toLowerCase();
  if (s.includes('red') || s.includes('fail')) return true;
  const m = s.match(/(\d+)\s*\/\s*(\d+)/);
  return m ? +m[2] > 0 && +m[1] < +m[2] : false;
}

export function fmtSecs(s: number | null | undefined): string {
  const n = Math.max(0, Math.floor(Number(s) || 0));
  if (n < 60) return `${n}s`;
  if (n < 3600) return `${Math.floor(n / 60)}m${n % 60}s`;
  return `${Math.floor(n / 3600)}h${Math.floor((n % 3600) / 60)}m`;
}

/** Elapsed time since a unix timestamp, compact. */
export function since(t: number | null | undefined, now = Date.now() / 1000): string {
  if (!t) return '';
  const s = Math.max(0, now - t);
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${Math.floor(s % 60)}s`;
  return `${Math.floor(s / 3600)}h`;
}

export const shortPath = (p: string | null | undefined) => {
  const s = p ? String(p) : '';
  return s.length > 42 ? '…' + s.slice(-40) : s;
};

export const fmtSize = (n: number | null | undefined) => {
  const b = Number(n) || 0;
  return b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`;
};

/** The deliverable handle, only when it is a real string path (never "[object Object]"). */
export const deliverablePath = (r: Resolution | null | undefined) =>
  typeof r?.handles?.deliverable === 'string' ? (r.handles.deliverable as string) : '';

/** A loop name → its current disposition verb. */
export function dispositionMap(r: DispositionsResponse | null | undefined): Record<string, string> {
  const m: Record<string, string> = {};
  if (!r || r.error || r.ok === false) return m;
  for (const l of r.loops ?? []) if (l?.loop && l.verb) m[l.loop] = l.verb;
  return m;
}

export interface DispRollup {
  good: number;
  ok: number;
  bad: number;
  rated: number;
  total: number;
  /** 0–100 good rate, null when the backend gave none. */
  pct: number | null;
}

/** The per-project "how loops landed here" rollup; null when nothing is rated. */
export function dispositionRollup(r: DispositionsResponse | null | undefined): DispRollup | null {
  if (!r || r.error || r.ok === false || !r.rated) return null;
  const gr = r.goodRate;
  return { good: r.good ?? 0, ok: r.ok_count ?? 0, bad: r.bad ?? 0, rated: r.rated, total: r.total ?? 0, pct: gr != null ? Math.round(gr * 100) : null };
}

/** Does the resolution card own the good/ok/bad rating (so the inline row stands down)? */
export const resolutionOwnsRating = (tr: TeamRoom | null | undefined) => !!tr?.resolution?.verdict?.resolved;

/** Tone for a good/ok/bad grade (owner verb or analyst grade) — pending stays muted. */
export function gradeTone(g: string | null | undefined): 'green' | 'amber' | 'red' | 'muted' {
  if (g === 'good') return 'green';
  if (g === 'ok') return 'amber';
  if (g === 'bad') return 'red';
  return 'muted';
}

// ── The one timeline: every per-turn record merged into a single feed ──────────

/** One row of the loop-detail timeline — a turn (openable when `seq` is set) or an engine note. */
export interface TimelineRow {
  key: string;
  kind: 'turn' | 'system';
  agent: string;
  /** The per-run turn index to open (prompt files are keyed on it); null = can't be opened. */
  seq: number | null;
  status?: string | null;
  note: string;
  ts?: number | null;
  isManager?: boolean;
  /** The kept note is a capped gist (the full one wasn't available). */
  truncated?: boolean;
  /** System rows: a short label for what the engine did (parallel, guardian, sub-loop, …). */
  label?: string;
}

/** The longer of two notes (a turn's full report beats its capped gist). */
const richer = (a: string, b: string) => ((b || '').trim().length > (a || '').trim().length ? b.trim() : (a || '').trim());

/** Rows from different sources describe the same turn when the same agent logged them within this many seconds. */
const SAME_TURN_SECS = 5;

function systemRow(e: LoopEvent, i: number, subNames: Record<string, string>): TimelineRow {
  const st = e.status || '';
  const base = { key: `sys-${i}-${e.ts}`, kind: 'system' as const, agent: e.agent || '', seq: null, status: st, ts: e.ts };
  if (st === 'parallel') {
    const ids = (e.agent || '').split('+').filter(Boolean);
    return { ...base, label: 'parallel', note: `${ids.join(', ')} at once` };
  }
  if (e.phase === 'guardian') return { ...base, label: 'guardian', note: `${e.agent}: ${st}${e.note ? ' — ' + e.note : ''}` };
  if (st.startsWith('subloop_')) {
    return { ...base, label: 'sub-loop', note: `${subNames[e.agent] || e.agent} ${st.replace('subloop_', '')}${e.note ? ' — ' + e.note : ''}` };
  }
  return { ...base, label: e.agent || 'engine', note: `${st}${e.note ? ' — ' + e.note : ''}` };
}

/**
 * ONE feed of turns, newest first. It merges the three per-turn records the engine
 * keeps — the team room's per-agent notes (per-run `seq`, authoritative), the
 * scheduler log (`recent`, re-based onto this run via `runTurnSeqs`) and the capped
 * thought-log gists — deduped by agent + time (or agent + turn), keeping the richer
 * note. Engine-only rows (parallel groups, guardian nudges, sub-loop starts) are
 * included only when `system` is on.
 */
export function loopTimeline(input: {
  tr?: TeamRoom | null;
  recent?: LoopEvent[] | null;
  started?: number | null;
  thoughts?: ThoughtlogResponse | null;
  manager?: string | null;
  subNames?: Record<string, string>;
  system?: boolean;
}): TimelineRow[] {
  const { tr, started, thoughts, system } = input;
  const recent = input.recent ?? [];
  const managers = new Set<string>((tr?.roster ?? []).filter((c) => c.isManager).map((c) => c.agent));
  if (input.manager) managers.add(input.manager);
  const rows: TimelineRow[] = [];
  const byKey = new Map<string, TimelineRow>();
  const turnKey = (agent: string, seq: number) => `${agent}#${seq}`;
  const findByTime = (agent: string, ts: number | null | undefined) =>
    typeof ts === 'number'
      ? rows.find((r) => r.kind === 'turn' && r.agent === agent && typeof r.ts === 'number' && Math.abs(r.ts - ts) <= SAME_TURN_SECS)
      : undefined;
  const merge = (row: Omit<TimelineRow, 'key' | 'kind'>) => {
    const hit = findByTime(row.agent, row.ts) ?? (row.seq != null ? byKey.get(turnKey(row.agent, row.seq)) : undefined);
    if (hit) {
      const note = richer(hit.note, row.note);
      if (note !== hit.note) {
        hit.note = note;
        hit.truncated = row.truncated;
      }
      hit.status ||= row.status;
      hit.ts ??= row.ts;
      if (hit.seq == null && row.seq != null) {
        hit.seq = row.seq;
        byKey.set(turnKey(hit.agent, hit.seq), hit);
      }
      return;
    }
    const r: TimelineRow = {
      ...row, note: (row.note || '').trim(), kind: 'turn', key: `${row.agent}-${row.seq ?? 'x'}-${row.ts ?? rows.length}`, isManager: managers.has(row.agent),
    };
    rows.push(r);
    if (r.seq != null && !byKey.has(turnKey(r.agent, r.seq))) byKey.set(turnKey(r.agent, r.seq), r);
  };

  // 1. The team room's per-run notes — their seq is exactly what the turn modal opens.
  for (const c of tr?.roster ?? []) {
    for (const d of c.turnDots?.dots ?? []) {
      if (d.current || !(d.seq > 0)) continue;
      merge({ agent: c.agent, seq: d.seq, status: d.status, note: d.note || '', ts: d.ts });
    }
  }
  // 2. The scheduler log, re-based onto this run.
  const seqs = runTurnSeqs(recent, { started, agentTurns: tr ? agentTurnCounts(tr) : undefined });
  recent.forEach((e, i) => {
    if (e.kind === 'machinery') {
      if (system) rows.push(systemRow(e, i, input.subNames ?? {}));
      return;
    }
    if (!e.agent) return;
    merge({ agent: e.agent, seq: seqs[i], status: e.status, note: e.note || '', ts: e.ts });
  });
  // 3. Thought-log gists: their `turn` counts the whole history, so they match by time only.
  if (thoughts && !thoughts.error) {
    for (const t of thoughts.entries ?? []) merge({ agent: t.agent, seq: null, status: t.status, note: t.gist || '', ts: t.ts, truncated: !!t.truncated });
  }
  return rows.sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0) || (b.seq ?? 0) - (a.seq ?? 0));
}

/** A team chip: who, their role, how many turns they took, and whether they're working now. */
export interface AgentChip {
  agent: string;
  role: string;
  isManager: boolean;
  turns: number;
  live: boolean;
}

export function agentChips(tr: TeamRoom | null | undefined, opts: { running?: boolean; live?: LiveAgent[] } = {}): AgentChip[] {
  const liveSet = new Set((opts.live ?? []).map((l) => l.agent));
  return (tr?.roster ?? []).map((c) => {
    const { dots, hidden } = turnDotsOf(c);
    const done = dots.filter((d) => !d.current && d.seq > 0).length + hidden;
    const live = !!opts.running && (dots.some((d) => d.current) || liveSet.has(c.agent) || !!c.doingNow);
    return { agent: c.agent, role: c.displayRole || c.role || '', isManager: !!c.isManager, turns: done, live };
  });
}

/**
 * The 1–3 deliverables worth showing on the summary: files an agent marked as a
 * result that still exist, then any other live file in the folder. `more` counts
 * the rest (they're one click away in Files).
 */
export function topDeliverables(rc: Resolution | null | undefined, n = 3): { items: DeliverableItem[]; more: number } {
  const dl = deliverablesOf(rc);
  if (!dl) return { items: [], more: 0 };
  const live = dl.items.filter((i) => i.status !== 'deleted' && i.exists !== false && !i.isDir);
  const ranked = [...live.filter((i) => i.marked), ...live.filter((i) => !i.marked)];
  const items = ranked.slice(0, n);
  return { items, more: Math.max(0, live.length + (dl.hidden ?? 0) - items.length) };
}

/** The finished run's verdict in plain words (never the engine's SHOUTED value). */
export function verdictLabel(v: Verdict | null | undefined): { label: string; tone: 'green' | 'amber' | 'red' } | null {
  if (!v || v.running) return null;
  const tone = resolutionTone(v);
  if (tone === 'green') return { label: 'Resolved', tone };
  if (tone === 'red') return { label: "Didn't resolve", tone };
  return { label: v.resolved ? 'Resolved, with open items' : 'Not resolved', tone };
}
