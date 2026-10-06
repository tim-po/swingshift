// Pure helpers for the registry-v2 version timeline + goal distiller.

export type DiffOp = 'same' | 'add' | 'del';
export interface DiffPart {
  op: DiffOp;
  text: string;
}

const words = (s: string | null | undefined) => (s ?? '').split(/\s+/).filter(Boolean);

/** Word-level diff (LCS) of two texts, adjacent same-op runs merged. Whitespace
 *  is normalised: a part's words are joined by single spaces and parts are meant
 *  to be rendered space-separated, so a removed word never glues onto an added
 *  one ("TurnImplement"). */
export function wordDiff(a: string | null | undefined, b: string | null | undefined): DiffPart[] {
  const x = words(a), y = words(b);
  const n = x.length, m = y.length;
  const L: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--) L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out: DiffPart[] = [];
  const push = (op: DiffOp, text: string) => {
    const last = out[out.length - 1];
    if (last && last.op === op) last.text += ' ' + text;
    else out.push({ op, text });
  };
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { push('same', x[i]); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) push('del', x[i++]);
    else push('add', y[j++]);
  }
  while (i < n) push('del', x[i++]);
  while (j < m) push('add', y[j++]);
  return out;
}

/** Share of words the two texts keep in common (0..1, vs the longer side). */
export function diffOverlap(parts: DiffPart[]): number {
  const n = (keep: (op: DiffOp) => boolean) =>
    parts.filter((p) => keep(p.op)).reduce((k, p) => k + words(p.text).length, 0);
  const longer = Math.max(n((op) => op !== 'add'), n((op) => op !== 'del'));
  return longer ? n((op) => op === 'same') / longer : 1;
}

/** Below this overlap an interleaved diff is word soup — show Before / After. */
export const STACK_BELOW = 0.4;
export const diffMode = (parts: DiffPart[]): 'inline' | 'stacked' =>
  parts.some((p) => p.op === 'same') && diffOverlap(parts) >= STACK_BELOW ? 'inline'
    : parts.length > 1 ? 'stacked' : 'inline';

export const FIELD_LABEL: Record<string, string> = { persona: 'Persona', genericGoal: 'Goal', model: 'Model' };

/** The note the distiller's accept route stores (tracking_ui DISTILL_NOTE). */
export const DISTILL_NOTE = 'distilled: loop-agnostic goal';

/** Plain-language source label; an "edit" that only moved the goal is a goal edit. */
export function sourceLabel(source: string | undefined, changed: string[] | undefined, note?: string | null): string {
  if (source === 'distill' || note === DISTILL_NOTE) return 'goal made reusable';
  if (source === 'auto') return 'first seen in a loop';
  if (source === 'init') return 'created';
  if (source === 'migrated') return 'imported (v1)';
  const c = changed ?? [];
  if (c.length === 1 && c[0] === 'genericGoal') return 'goal edit';
  if (c.length === 1 && c[0] === 'model') return 'model change';
  return 'persona edit';
}

/** Calm, plain-language reason the distiller offers what it offers. */
export function distillReason(basis: string | undefined): string {
  if (basis === 'trimmed') return 'We left out the parts that only make sense in one loop (file paths, links, task lists), so any loop can reuse this agent.';
  if (basis === 'role-default') return "This agent hasn't recorded a goal of its own yet, so here's the standard loop-agnostic goal for its role.";
  return 'This goal already reads as reusable across loops.';
}
