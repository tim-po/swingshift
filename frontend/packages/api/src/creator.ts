// ＋Loop creator + loop editor: /api/loops/creator/*, save, clone, standalone,
// agents registry, a loop's config. Types + calls + pure helpers (no DOM).
import type { Client } from './client';

/** A loop config as the schema accepts it. Loose on purpose: the server is the gate. */
export interface LoopConfig {
  name?: string;
  goal?: string;
  budget?: { turnLimit?: number; [k: string]: unknown };
  steps?: Record<string, StepConfig>;
  stepOrder?: (string | string[])[];
  projectId?: string;
  productId?: string;
  origin?: string;
  host_class?: string;
  [k: string]: unknown;
}

export type StepConfig =
  | { type: 'loop'; loop: LoopConfig; [k: string]: unknown }
  | {
      type?: 'agent';
      role?: string;
      personality?: string;
      goal?: string;
      maxTurnMinutes?: number;
      [k: string]: unknown;
    };

/** Anything the MCP proxy can answer with on failure. */
export interface Failable {
  ok?: boolean;
  error?: string;
  errors?: string[];
  raw?: string;
}

export interface CreatorPromptResponse extends Failable {
  prompt?: string;
  open_questions?: string[];
  context_docs?: string[];
}

export interface ValidateResponse extends Failable {
  config?: LoopConfig;
  warnings?: string[];
  lint?: { warnings?: string[] } | unknown[];
  source?: string;
}

export interface SuggestedRole {
  id: string;
  role?: string;
  why?: string;
}

export interface SuggestResponse extends Failable {
  editable?: boolean;
  goal?: string;
  signals?: string[];
  roles?: SuggestedRole[];
  config?: LoopConfig;
  warnings?: string[];
  lint?: unknown[];
  note?: string;
}

export interface BriefQuestion {
  key: string;
  question?: string;
  why?: string;
}

export interface BriefRequest {
  phase: 'brief' | 'debrief';
  answers: Record<string, string>;
  goal?: string;
  name?: string;
  projectId?: string;
  target?: string;
  just_one?: boolean;
}

export interface BriefTurn extends Failable {
  goal?: string;
  note?: string;
  done?: boolean;
  single_agent?: boolean;
  questions?: BriefQuestion[];
  preview?: { config?: LoopConfig; roles?: SuggestedRole[] };
}

export interface SaveResponse extends Failable {
  name?: string;
  warnings?: string[];
}

export interface StandaloneRequest {
  agent_id: string;
  product_id: string;
  model?: string;
  version?: number;
  origin?: string;
}

export interface RegistryAgent {
  id: string;
  role: string;
  personality?: string;
  goal?: string;
  note?: string;
}

export interface EditorConfigResponse extends Failable {
  name?: string;
  /** The mirror the config was READ from (e.g. 'local') — not the loop's machine binding. */
  origin?: string;
  config?: LoopConfig;
  /** The loop's project — its explicit binding only; null = Unattributed. */
  project?: string | null;
  /** A guess (goal target / git remote) for an unbound loop — offered, never assumed. */
  suggested_project?: string | null;
}

const trimmed = (v: unknown) => (typeof v === 'string' ? v.trim() : '');

/**
 * The project + machine a saved loop is bound to, for the editor's selects. The
 * project is ONLY the explicit `projectId` / `productId` in the config — a guessed
 * project is never pre-selected, so saving an unbound loop can't silently bind it
 * (see `editorSuggestion`). The response's top-level `origin` is only the mirror
 * it was read from, so the machine comes from the config alone.
 */
export function editorBindings(res: EditorConfigResponse | null | undefined): { projectId: string; origin: string } {
  const c = res?.config ?? {};
  return {
    projectId: trimmed(c.projectId) || trimmed(c.productId),
    origin: trimmed(c.origin) || trimmed(c.host_class),
  };
}

/** The suggested project for an unbound loop ('' when bound or nothing to suggest). */
export function editorSuggestion(res: EditorConfigResponse | null | undefined): string {
  return editorBindings(res).projectId ? '' : trimmed(res?.suggested_project);
}

export interface CreatorDoc {
  id: string;
  title?: string;
  glyph?: string;
  project?: string | null;
}

export interface CreatorIssue {
  id: string;
  title?: string;
  status?: string;
  kind?: string;
  project?: string | null;
  product?: string | null;
  loop?: string | null;
}

const enc = encodeURIComponent;

export const creatorApi = (c: Client) => ({
  prompt: () => c.get<CreatorPromptResponse>('/api/loops/creator/prompt'),
  validate: (body: { config: LoopConfig } | { text: string }) => c.post<ValidateResponse>('/api/loops/creator/validate', body),
  suggest: (goal: string, name?: string) => c.post<SuggestResponse>('/api/loops/creator/suggest', name ? { goal, name } : { goal }),
  brief: (req: BriefRequest) => c.post<BriefTurn>('/api/loops/creator/brief', req),
  save: (config: LoopConfig) => c.post<SaveResponse>('/api/loops/save', { config }),
  clone: (from_id: string, new_name: string) => c.post<SaveResponse>('/api/loops/clone', { from_id, new_name }),
  standalone: (req: StandaloneRequest) => c.post<Failable & Record<string, unknown>>('/api/loops/standalone', req),
  start: (name: string) => c.post<Failable>(`/api/loops/${enc(name)}/action`, { action: 'start' }),
  agents: () => c.get<{ agents?: RegistryAgent[] } & Failable>('/api/loops/agents'),
  config: (name: string) => c.get<EditorConfigResponse>(`/api/loops/${enc(name)}/config`),
  docs: () => c.get<{ docs?: CreatorDoc[] } & Failable>('/api/loops/docs'),
  issues: () => c.get<{ issues?: CreatorIssue[] } & Failable>('/api/loops/issues'),
});

// ── Pure helpers ────────────────────────────────────────────────────────────

/** Surface a reason no matter the failure shape: {errors[]} | {error} | {raw}. */
export function creatorErrors(res: Failable | null | undefined, fallback: string): string {
  const errs = (res && (res.errors?.length ? res.errors : res.error ? [res.error] : res.raw ? [res.raw] : null)) || [fallback];
  const s = errs.filter(Boolean).join('; ');
  return s || fallback;
}

/** Warnings from validate: top-level + lint.warnings. */
export function creatorWarnings(res: ValidateResponse): string[] {
  const lint = res.lint && !Array.isArray(res.lint) ? res.lint.warnings ?? [] : [];
  return [...(res.warnings ?? []), ...lint];
}

/** Bind (or honestly unbind) the chosen project. Returns a NEW object. */
export function injectProject(cfg: LoopConfig, projectId: string): LoopConfig {
  const out = { ...cfg };
  const pid = projectId.trim();
  if (pid) out.projectId = pid;
  else {
    delete out.projectId;
    delete out.productId;
  }
  return out;
}

export function deriveName(goal: string): string {
  const s = goal
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
    .replace(/-+$/, '');
  return 'ask-' + (s || 'one');
}

/** "Ask one agent" — the loop of one. */
export function soloConfig(goal: string): LoopConfig {
  return {
    name: deriveName(goal),
    goal,
    steps: {
      solo: {
        type: 'agent',
        role: 'manager',
        personality: 'A pragmatic solo agent who drives the goal to done and decides when the work is complete.',
        goal,
      },
    },
    stepOrder: ['solo'],
  };
}

/** The LAST fenced ```json block containing an object; else first `{` … last `}`. */
export function extractConfig(text: string): string | null {
  const fence = /```(?:json)?\s*([\s\S]*?)```/gi;
  let m: RegExpExecArray | null;
  let last: string | null = null;
  while ((m = fence.exec(text)) !== null) if (m[1] && m[1].includes('{')) last = m[1];
  if (last) return last.trim();
  const a = text.indexOf('{');
  const b = text.lastIndexOf('}');
  return a !== -1 && b > a ? text.slice(a, b + 1) : null;
}

/** Validate body for raw editor text: a JSON object goes as {config}, prose as {text}. */
export function validateBody(text: string): { config: LoopConfig } | { text: string } {
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return { config: parsed as LoopConfig };
  } catch {
    /* prose */
  }
  return { text };
}

// ── Door A: targets ──

export interface CreatorTarget {
  kind: 'doc' | 'issue';
  id: string;
  text: string;
}

const OPEN_WORK = new Set(['open', 'snoozed', 'resolving']);

/**
 * The Door A picker catalog: hub docs + OPEN issues, scoped to the active project
 * ('' = all; `unattr` = the no-project bucket). An issue maps to its project via
 * its own record, else through its loop (`loopProject`).
 */
export function targetCatalog(
  docs: CreatorDoc[],
  issues: CreatorIssue[],
  scope = '',
  unattr = '__unattributed__',
  loopProject: (loop: string) => string | null | undefined = () => null,
): CreatorTarget[] {
  const inScope = (pid: string | null | undefined) => !scope || (scope === unattr ? !pid : pid === scope);
  const list: CreatorTarget[] = docs
    .filter((d) => inScope(d.project))
    .map((d) => ({ kind: 'doc', id: d.id, text: `${d.glyph || '·'} ${d.title || d.id}` }));
  for (const it of issues)
    if (OPEN_WORK.has((it.status || 'open').toLowerCase()) && inScope(it.project || it.product || (it.loop ? loopProject(it.loop) : null)))
      list.push({ kind: 'issue', id: it.id, text: (it.kind === 'crash' ? '⚠ ' : '') + (it.title || it.id) });
  return list;
}

/** Fold an attached target into the goal box (the target IS the goal). */
export function goalWithTarget(goal: string, t: CreatorTarget): string {
  const line = 'Point a loop at: ' + t.text;
  return goal.trim() ? goal.trim() + '\n' + line : line;
}

// ── Loop editor tree model (agents + nested sub-loops + parallel groups) ──

export interface EdAgentStep {
  uid: string;
  kind: 'agent';
  id: string;
  par: boolean;
  role: string;
  personality: string;
  goal: string;
  maxTurnMinutes?: number;
  _raw?: Record<string, unknown>;
}
export interface EdLoopStep {
  uid: string;
  kind: 'loop';
  id: string;
  par: boolean;
  loop: EdNode;
}
export type EdStep = EdAgentStep | EdLoopStep;
export interface EdNode {
  uid: string;
  name: string;
  goal: string;
  turnLimit: number;
  steps: EdStep[];
  _budget: Record<string, unknown>;
}

export const EDITOR_ROLES = ['manager', 'worker', 'input_provider'] as const;

export function edUidGen(prefix = 'n') {
  let n = 0;
  return () => `${prefix}${++n}`;
}

export function edBlankNode(uid: () => string): EdNode {
  return { uid: uid(), name: '', goal: '', turnLimit: 30, steps: [], _budget: {} };
}

export function edBlankAgent(uid: () => string, role = 'worker'): EdAgentStep {
  return { uid: uid(), kind: 'agent', id: '', par: false, role, personality: '', goal: '' };
}

export function edToNode(c: LoopConfig, uid: () => string): EdNode {
  const steps = c.steps ?? {};
  const seq: { id: string; par: boolean }[] = [];
  const seen = new Set<string>();
  const push = (id: string, par: boolean) => {
    if (steps[id] && !seen.has(id)) {
      seq.push({ id, par });
      seen.add(id);
    }
  };
  (c.stepOrder ?? Object.keys(steps)).forEach((e) => {
    if (typeof e === 'string') push(e, false);
    else if (Array.isArray(e)) e.forEach((id, i) => push(id, i > 0));
  });
  Object.keys(steps).forEach((id) => push(id, false));
  const arr: EdStep[] = seq.map(({ id, par }) => {
    const s = steps[id] ?? {};
    if (s.type === 'loop') return { uid: uid(), kind: 'loop', id, par, loop: edToNode((s.loop as LoopConfig) ?? {}, uid) };
    const a = s as Exclude<StepConfig, { type: 'loop' }>;
    return {
      uid: uid(),
      kind: 'agent',
      id,
      par,
      role: a.role || 'worker',
      personality: a.personality || '',
      goal: a.goal || '',
      maxTurnMinutes: a.maxTurnMinutes,
      _raw: a as Record<string, unknown>,
    };
  });
  return { uid: uid(), name: c.name || '', goal: c.goal || '', turnLimit: c.budget?.turnLimit || 30, steps: arr, _budget: c.budget ?? {} };
}

export function edFromNode(node: EdNode): LoopConfig {
  const steps: Record<string, StepConfig> = {};
  const order: (string | string[])[] = [];
  for (const st of node.steps) {
    const id = st.id.trim();
    if (!id) continue;
    if (st.kind === 'loop') steps[id] = { type: 'loop', loop: edFromNode(st.loop) };
    else {
      const base: Record<string, unknown> = { ...(st._raw ?? {}) };
      delete base.type;
      const a: Record<string, unknown> = { ...base, type: 'agent', role: st.role, personality: st.personality, goal: st.goal };
      if (st.maxTurnMinutes) a.maxTurnMinutes = Math.trunc(Number(st.maxTurnMinutes)) || undefined;
      else delete a.maxTurnMinutes;
      steps[id] = a as StepConfig;
    }
    if (st.par && order.length) {
      const last = order[order.length - 1];
      if (Array.isArray(last)) last.push(id);
      else order[order.length - 1] = [last, id];
    } else order.push(id);
  }
  return {
    name: node.name.trim(),
    goal: node.goal,
    budget: { ...node._budget, turnLimit: Math.trunc(Number(node.turnLimit)) || 30 },
    steps,
    stepOrder: order,
  };
}

/** Immutable update of the node with `uid` anywhere in the tree. */
export function edUpdateNode(root: EdNode, uid: string, fn: (n: EdNode) => EdNode): EdNode {
  if (root.uid === uid) return fn(root);
  let changed = false;
  const steps = root.steps.map((st) => {
    if (st.kind !== 'loop') return st;
    const loop = edUpdateNode(st.loop, uid, fn);
    if (loop === st.loop) return st;
    changed = true;
    return { ...st, loop };
  });
  return changed ? { ...root, steps } : root;
}

export function edMoveStep(steps: EdStep[], i: number, d: number): EdStep[] {
  const k = i + d;
  if (k < 0 || k >= steps.length) return steps;
  const out = steps.slice();
  [out[i], out[k]] = [out[k], out[i]];
  return out;
}

/** Client-side pre-save checks the legacy editor did; returns an error or null. */
export function editorPrecheck(cfg: LoopConfig): string | null {
  if (!cfg.name) return 'name required';
  if (!cfg.stepOrder?.length) return 'add at least one agent (with an id)';
  return null;
}

/** Which requested bindings are MISSING from a loop's saved config (read back
 * after a save)? [] when everything stuck, or when the read-back failed (no
 * evidence either way). */
export function bindingsLost(
  back: EditorConfigResponse | null | undefined,
  want: { projectId?: string; origin?: string },
): ('project' | 'machine')[] {
  if (!back || back.error || !back.config) return [];
  const c = back.config;
  const lost: ('project' | 'machine')[] = [];
  if (want.projectId && c.projectId !== want.projectId && c.productId !== want.projectId) lost.push('project');
  if (want.origin && c.origin !== want.origin) lost.push('machine');
  return lost;
}
