// The editable DRAFT behind "Draft a team": the suggested config reduced to what a
// beta tester actually edits (the loop goal + each agent's role/persona/goal),
// merged back onto the full config for the live check, save and start. Pure — no
// DOM, no fetch — so the wiring is testable on its own.
import type { Failable, LoopConfig, StepConfig } from '@loopyard/api';

export interface DraftAgent {
  id: string;
  role: string;
  personality: string;
  goal: string;
}

export interface Draft {
  goal: string;
  agents: DraftAgent[];
}

/** One loop_lint finding: {level, rule, where:"goal"|"steps.<id>.<field>", message}. */
export interface LintFinding {
  level?: string;
  rule?: string;
  where?: string;
  message?: string;
}

export interface SchematicStep {
  id: string;
  type?: 'agent' | 'loop';
  role?: string | null;
  goal?: string;
  runtime?: string;
  loop?: Schematic;
}

export type SchematicOrder = { step: string } | { parallel: string[] };

/** The shape mcp_loops.authoring.schematic returns (what loop_schematic renders). */
export interface Schematic {
  name?: string | null;
  steps?: SchematicStep[];
  stepOrder?: SchematicOrder[];
  derived?: Record<string, unknown>;
}

/** POST /api/loops/creator/draft-check → real loop_lint findings + the draft's schematic. */
export interface DraftCheck extends Failable {
  warnings?: string[];
  lints?: LintFinding[];
  schematic?: Schematic | null;
  /** true when the lint tool couldn't be reached — not a problem with the draft. */
  offline?: boolean;
}

export const ROLES = ['manager', 'worker', 'input_provider'] as const;
export const ROLE_LABEL: Record<string, string> = {
  manager: 'Manager — steers + decides when done',
  worker: 'Worker — does the work',
  input_provider: 'Reviewer — gives feedback',
};

const isAgent = (s: StepConfig | undefined): s is Exclude<StepConfig, { type: 'loop' }> => !!s && s.type !== 'loop';
const str = (v: unknown) => (typeof v === 'string' ? v : '');

/** Step ids in run order (parallel groups flattened), then any not in stepOrder. */
function orderedIds(cfg: LoopConfig): string[] {
  const seen: string[] = [];
  for (const ref of cfg.stepOrder ?? []) for (const id of Array.isArray(ref) ? ref : [ref]) if (!seen.includes(id)) seen.push(id);
  for (const id of Object.keys(cfg.steps ?? {})) if (!seen.includes(id)) seen.push(id);
  return seen;
}

export function draftFromConfig(cfg: LoopConfig): Draft {
  const steps = cfg.steps ?? {};
  const agents: DraftAgent[] = [];
  for (const id of orderedIds(cfg)) {
    const s = steps[id];
    if (!isAgent(s)) continue;
    agents.push({ id, role: str(s.role) || 'worker', personality: str(s.personality), goal: str(s.goal) });
  }
  return { goal: str(cfg.goal), agents };
}

/** Merge the edited draft back onto the full config: edited fields win, removed
 *  agents leave steps + stepOrder, sub-loop steps and every other field survive. */
export function applyDraft(base: LoopConfig, d: Draft): LoopConfig {
  const baseSteps = base.steps ?? {};
  const keep = new Set(d.agents.map((a) => a.id));
  const steps: Record<string, StepConfig> = {};
  for (const [id, s] of Object.entries(baseSteps)) if (!isAgent(s)) steps[id] = s;
  for (const a of d.agents) {
    const prev = baseSteps[a.id];
    steps[a.id] = { ...(isAgent(prev) ? prev : { type: 'agent' }), role: a.role, personality: a.personality, goal: a.goal };
  }
  const alive = (id: string) => keep.has(id) || (id in steps && !isAgent(steps[id]));
  const order: (string | string[])[] = [];
  for (const ref of base.stepOrder ?? []) {
    if (Array.isArray(ref)) {
      const g = ref.filter(alive);
      if (g.length > 1) order.push(g);
      else if (g.length === 1) order.push(g[0]);
    } else if (alive(ref)) order.push(ref);
  }
  const placed = new Set(order.flat());
  for (const a of d.agents) if (!placed.has(a.id)) order.push(a.id);
  return { ...base, goal: d.goal, steps, stepOrder: order };
}

export function updateAgent(d: Draft, id: string, patch: Partial<Omit<DraftAgent, 'id'>>): Draft {
  return { ...d, agents: d.agents.map((a) => (a.id === id ? { ...a, ...patch } : a)) };
}

export function removeAgent(d: Draft, id: string): Draft {
  return { ...d, agents: d.agents.filter((a) => a.id !== id) };
}

/** The roles "＋ Add an agent" offers (a team keeps exactly one manager). */
export const ADDABLE_ROLES = ['worker', 'input_provider'] as const;

/** POST /api/loops/creator/scaffold → the real loop_creator_scaffold's result. */
export interface ScaffoldResult extends Failable {
  config?: LoopConfig;
  warnings?: string[];
}

/** The team spec loop_creator_scaffold takes: the draft as it stands + one new role. */
export function scaffoldSpec(d: Draft, role: string) {
  return {
    name: 'draft',
    goal: d.goal.trim() || 'Draft loop',
    roles: [...d.agents.map((a) => ({ id: a.id, role: a.role, personality: a.personality, goal: a.goal })), { role }],
  };
}

/** Fold the agent(s) the scaffold added onto the base config + the draft; the
 *  tester's existing agents and edits are left exactly as they were. */
export function addScaffolded(base: LoopConfig, d: Draft, scaffolded: LoopConfig): { base: LoopConfig; draft: Draft } {
  const have = new Set(d.agents.map((a) => a.id));
  const steps = { ...(base.steps ?? {}) };
  const added: DraftAgent[] = [];
  for (const [id, s] of Object.entries(scaffolded.steps ?? {})) {
    if (have.has(id) || !isAgent(s) || (id in steps && !isAgent(steps[id]))) continue;
    steps[id] = s;
    added.push({ id, role: str(s.role) || 'worker', personality: str(s.personality), goal: str(s.goal) });
  }
  return { base: { ...base, steps }, draft: { ...d, agents: [...d.agents, ...added] } };
}

/** Findings anchored at `where` (e.g. "goal", "steps.builder.goal") or beneath it. */
export function findingsAt(lints: LintFinding[] | undefined, where: string): LintFinding[] {
  return (lints ?? []).filter((f) => f.where === where || (!!f.where && f.where.startsWith(where + '.')));
}

/** Findings no field in the draft editor owns — shown once, above the team. */
export function looseFindings(lints: LintFinding[] | undefined, d: Draft): LintFinding[] {
  const owned = ['goal', ...d.agents.flatMap((a) => [`steps.${a.id}.goal`, `steps.${a.id}.personality`])];
  return (lints ?? []).filter((f) => !f.where || !owned.some((w) => f.where === w || f.where!.startsWith(w + '.')));
}

// Calm, human titles for the authoring rules — a hint, never a scolding.
const RULE_HINT: Record<string, string> = {
  'no-todos-in-identity': 'Reads like a to-do list',
  'product-detail-in-loop-goal': 'Specific paths or products belong in the loop goal',
  'generic-goal': 'Keep this reusable across loops',
};

export function findingTitle(f: LintFinding): string {
  return (f.rule && RULE_HINT[f.rule]) || 'A small suggestion';
}
