// What the flip-dot boards say: the Home shift board (a departures board of
// loops on shift) and the top-bar sign. Pure, so it's tested without a canvas.
import type { OverviewModel, OverviewLoop } from '@loopyard/api';
import { clip, measure, type DotText } from './font';

export interface ShiftRow {
  name: string;
  turn: string;
  status: string;
  /** Stuck on the owner. */
  needs: boolean;
}

const STATUS: Record<string, string> = {
  waiting_owner: 'NEEDS YOU',
  needs_owner: 'NEEDS YOU',
  guardian_stopped: 'PAUSED',
  error: 'ERROR',
  running: 'RUNNING',
  stopping: 'STOPPING',
};

export const SHIFT_MAX = 5;

const turnOf = (d: OverviewLoop) =>
  d.turns_used == null ? '' : d.turnLimit ? `${d.turns_used}/${d.turnLimit}` : String(d.turns_used);

/** Loops that need you first, then the rest of what's running; at most SHIFT_MAX. */
export function shiftRows(m: Pick<OverviewModel, 'attention' | 'running'>): ShiftRow[] {
  const out: ShiftRow[] = m.attention.map((a) => ({ name: a.loop.name, turn: turnOf(a.loop), status: STATUS[a.state] ?? a.state.toUpperCase(), needs: true }));
  const seen = new Set(m.attention.map((a) => a.loop));
  for (const d of m.running) if (!seen.has(d)) out.push({ name: d.name, turn: turnOf(d), status: STATUS[d.state] ?? d.state.toUpperCase(), needs: false });
  return out.slice(0, SHIFT_MAX);
}

// Board geometry in discs: name · turn · status, one 7-dot line every 10 rows.
export const SB = { cols: 236, nameX: 2, nameW: 125, sep1: 130, turnX: 134, turnW: 41, sep2: 179, statX: 183, statW: 51 };
export const shiftBoardRows = (n: number) => Math.max(1, n) * 10 + 1;

export function shiftItems(rows: ShiftRow[]): DotText[] {
  return rows.flatMap((r, i) => {
    const y = 2 + i * 10;
    const alert = r.needs;
    return [
      { text: clip(r.name, SB.nameW), x: SB.nameX, y, alert },
      { text: '·', x: SB.sep1, y, alert },
      { text: r.turn, x: SB.turnX, y, align: 'right' as const, w: SB.turnW, alert },
      { text: '·', x: SB.sep2, y, alert },
      { text: clip(r.status, SB.statW), x: SB.statX, y, alert },
    ];
  });
}

export const shiftLabel = (rows: ShiftRow[]) =>
  'On shift: ' + rows.map((r) => `${r.name}, ${r.turn ? `turn ${r.turn}, ` : ''}${r.status.toLowerCase()}`).join('; ') + '.';

/** The top-bar sign: what needs you, else what's running, else nothing to show. */
export function signText(m: Pick<OverviewModel, 'attention' | 'running'>): { text: string; alert: boolean } | null {
  if (m.attention.length) return { text: `${m.attention.length} NEED${m.attention.length === 1 ? 'S' : ''} YOU`, alert: true };
  if (m.running.length) return { text: `${m.running.length} RUNNING`, alert: false };
  return null;
}

/** A fixed width, so a count ticking over flips a few discs instead of resizing the sign. */
export const SIGN_COLS = measure('99 NEEDS YOU') + 4;
