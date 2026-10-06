// PTY-over-WebSocket (/ws/terminal) wire contract. Pure — the socket itself
// lives in the app. client→server BINARY = raw stdin, TEXT = {"type":"resize",
// cols,rows}; server→client BINARY = raw stdout, TEXT = {"type":"hello"|"exit"|"error"}.

export type TermSession =
  | { session: 'shell' }
  | { session: 'creator'; runtime: 'claude' | 'codex' | string }
  | { session: 'attach'; target: string };

export type TermStatus = 'off' | 'wait' | 'on';

/**
 * Build the ws(s) URL. `apiBase` '' = same origin (derive from `pageUrl`);
 * an absolute http(s) base (native shell) maps to ws(s) on that host.
 */
export function terminalWsUrl(apiBase: string, pageUrl: string, s: TermSession = { session: 'shell' }): string {
  const base = new URL(apiBase || '/', pageUrl);
  const proto = base.protocol === 'https:' ? 'wss:' : 'ws:';
  const path = base.pathname.replace(/\/+$/, '') + '/ws/terminal';
  const q = new URLSearchParams();
  if (s.session === 'creator') {
    q.set('session', 'creator');
    q.set('origin', 'local');
    q.set('runtime', s.runtime || 'claude');
  } else if (s.session === 'attach') {
    q.set('session', 'attach');
    q.set('origin', 'local');
    q.set('target', s.target);
  }
  const qs = q.toString();
  return `${proto}//${base.host}${path}${qs ? '?' + qs : ''}`;
}

export const resizeMessage = (cols: number, rows: number) => JSON.stringify({ type: 'resize', cols, rows });

/** A server TEXT frame → ANSI line to write into the terminal (or null to ignore). */
export function controlLine(data: string): string | null {
  let m: { type?: string; code?: number | null; message?: string };
  try {
    m = JSON.parse(data);
  } catch {
    return null;
  }
  if (m.type === 'exit') return `\r\n\x1b[90m[session ended${m.code != null ? ` (${m.code})` : ''}]\x1b[0m\r\n`;
  if (m.type === 'error') return `\r\n\x1b[31m[${String(m.message || 'error')}]\x1b[0m\r\n`;
  return null;
}

/** Status text after a close: a never-opened close == handshake refused (auth). */
export const closedText = (opened: boolean) => (opened ? 'Disconnected' : 'Refused — reload to sign in');

/** Keep a rolling tail of decoded stdout (for "Apply from terminal"). */
export function appendTail(buf: string, chunk: string, max = 200_000): string {
  const s = buf + chunk;
  return s.length > max ? s.slice(-max) : s;
}
