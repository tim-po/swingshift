// Shapes of /api/loops/* responses (tracking_ui/loops_panel.py is the source of truth).

export type LoopState =
  | 'running' | 'stopping' | 'waiting_owner' | 'needs_owner'
  | 'finished' | 'complete' | 'stopped' | 'error' | 'saved'
  | (string & {});

export interface LoopTeam {
  manager?: string | null;
  workers?: string[];
  inputs?: string[];
  subloops?: string[];
}

export interface LoopReport {
  ts: number;
  agent: string;
  status: string;
  note?: string;
}

export interface LoopTurns {
  main: number | null;
  winddown: number | null;
  limit: number | null;
  budgeted: number | null;
  over: number;
}

export interface LoopSummary {
  name: string;
  host: string;
  state: LoopState;
  status?: string;
  ended?: string | null;
  archived: boolean;
  started?: number | null;
  updated?: number | null;
  goal?: string | null;
  project?: string | null;
  product?: string | null;
  productName?: string | null;
  single_agent?: boolean;
  origin?: string | null;
  team?: LoopTeam;
  turnLimit?: number | null;
  /** Main-phase turns against turnLimit, capped at it — `turns_used/turnLimit` never exceeds 1. */
  turns_used?: number | null;
  /** Wind-down turns: the guaranteed tail after the budget, never counted against turnLimit. */
  winddown_turns?: number | null;
  /** Raw turn accounting; `over` = main turns the last round ran past the limit. */
  turns?: LoopTurns;
  last_report?: LoopReport | null;
  summary?: string | null;
  /** Owner id (P3 enrolled owner). Absent on a single-owner box → the local default. */
  owner?: string | null;
}

export interface LoopsListResponse {
  loops: LoopSummary[];
  hosts: string[];
  /** Echoed owner filter when the request scoped by ?owner=. */
  owner?: string | null;
  error?: string;
}
