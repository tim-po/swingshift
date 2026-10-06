// /api/loops/sessions + /api/loops/session/dispatch — the connected-session roster.
import type { Client } from './client';

export interface SessionOrigin {
  host?: string | null;
  cwd?: string | null;
  origin?: string | null;
}

export interface SessionHeartbeat {
  live?: boolean;
  state?: string;
  lastSeen?: number | null;
  idleSeconds?: number | null;
}

export interface SessionCounts {
  loops?: number;
  issues?: number;
  objectives?: number;
}

export interface SessionActivity {
  claimed?: number;
  returned?: number;
  failed?: number;
}

export interface Session {
  id: string;
  runtime?: string | null;
  capabilities?: string[];
  origin?: SessionOrigin | null;
  heartbeat?: SessionHeartbeat | null;
  doingNow?: string | null;
  created?: SessionCounts | null;
  activity?: SessionActivity | null;
}

export interface SessionsResponse {
  sessions: Session[];
  count?: number;
  live?: number;
  error?: string;
}

export interface SessionDispatchResponse {
  ok?: boolean;
  task_id?: string;
  error?: string;
}

export const sessionsApi = (c: Client) => ({
  list: () => c.get<SessionsResponse>('/api/loops/sessions'),
  dispatch: (session: string, text: string) =>
    c.post<SessionDispatchResponse>('/api/loops/session/dispatch', { session, text }),
});

// ── Pure helpers ──

export type SessionKind = 'cli' | 'app';

/**
 * Capability-honest kind: an interactive/terminal session on the LOCAL box is a
 * CLI/tmux session we can OPEN; anything else is an app session we FOLLOW + SEND.
 */
export function sessionKind(s: Session): SessionKind {
  const caps = (s.capabilities ?? []).map((c) => String(c).toLowerCase());
  const interactive = caps.includes('interactive') || caps.includes('terminal') || caps.includes('tmux');
  const host = (s.origin?.host ?? '').toLowerCase();
  const local = !host || host === 'local' || host === 'localhost';
  return interactive && local ? 'cli' : 'app';
}

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`;

/** "2 loops · 1 issue" — or '' when the session created nothing. */
export function sessionCreatedLabel(c: SessionCounts | null | undefined): string {
  const x = c ?? {};
  return [
    x.loops ? plural(x.loops, 'loop') : '',
    x.issues ? plural(x.issues, 'issue') : '',
    x.objectives ? plural(x.objectives, 'plan') : '',
  ]
    .filter(Boolean)
    .join(' · ');
}

// ── Plain-words roster (the Machines → Sessions tab) ──

/** "claude" → "Claude", "gpt" → "GPT", "chatgpt" → "ChatGPT". */
export function runtimeName(runtime?: string | null): string {
  const r = (runtime || 'claude').trim();
  if (r.toLowerCase() === 'chatgpt') return 'ChatGPT';
  if (/^gpt/i.test(r)) return r.toUpperCase();
  return r[0].toUpperCase() + r.slice(1);
}

/** "Claude in a terminal" / "Claude app" — what this session is, in plain words. */
export function sessionKindLabel(s: Session): string {
  const name = runtimeName(s.runtime);
  return sessionKind(s) === 'cli' ? `${name} in a terminal` : `${name} app`;
}

/** "Online", "Last seen 5m ago" or "Offline" — strictly from the heartbeat. */
export function sessionSeenLabel(s: Session, agoFn: (t: number) => string, nowSec = Date.now() / 1000): string {
  const hb = s.heartbeat ?? {};
  if (hb.live) return 'Online';
  const last = hb.lastSeen ?? (hb.idleSeconds != null ? nowSec - hb.idleSeconds : null);
  return last ? `Last seen ${agoFn(last)}` : 'Offline';
}

/** "2 sessions · 1 online" — the tab summary. */
export function sessionCountLabel(sessions: Session[]): string {
  const live = sessions.filter((s) => s.heartbeat?.live).length;
  return `${plural(sessions.length, 'session')} · ${live} online`;
}

/** "3 picked up · 2 finished · 1 failed" — the tasks this session has handled. */
export function sessionActivityLabel(a: SessionActivity | null | undefined): string {
  const x = a ?? {};
  return `${x.claimed ?? 0} picked up · ${x.returned ?? 0} finished · ${x.failed ?? 0} failed`;
}
