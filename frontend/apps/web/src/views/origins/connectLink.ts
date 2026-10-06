// One-time origin-connect link (tracking_ui/origin_connect.py). The owner mints a
// short single-use URL, pastes it to their AI, and the AI fetches the connect
// instructions from the link itself. This file is the typed client + pure
// view-model helpers; the panel lives in ConnectLinkPanel.tsx.
import type { Client } from '@loopyard/api';
import { client } from '../../api';

/** Server-side link lifecycle. `connected` = the fetched pair code was claimed. */
export type ConnectLinkState = 'pending' | 'fetched' | 'connected' | 'expired' | 'revoked';

/** POST /api/loops/origin-connect → the link. Never carries the bare token. */
export interface ConnectLink {
  id: string;
  url: string;
  /** Epoch seconds. */
  expiresAt: number;
  owner?: string | null;
  label?: string | null;
  hub?: string;
  hubFingerprint?: string;
}

/** GET /api/loops/origin-connect/{id} — never includes the token or pair code. */
export interface ConnectLinkStatus {
  id: string;
  owner?: string | null;
  label?: string | null;
  hub?: string;
  createdAt?: number;
  expiresAt: number;
  fetchedAt?: number | null;
  revokedAt?: number | null;
  state: ConnectLinkState;
  deviceId?: string | null;
}

export interface MintInput {
  label?: string;
  ttlSec?: number;
  owner?: string;
}

const BASE = '/api/loops/origin-connect';
const q = (owner?: string) => (owner ? `?owner=${encodeURIComponent(owner)}` : '');

export const connectLinkApi = (c: Client) => ({
  mint: (input: MintInput = {}) => c.post<ConnectLink>(BASE, input),
  status: (id: string, owner?: string) => c.get<ConnectLinkStatus>(`${BASE}/${encodeURIComponent(id)}${q(owner)}`),
  revoke: (id: string, owner?: string) => c.post<ConnectLinkStatus>(`${BASE}/${encodeURIComponent(id)}/revoke${q(owner)}`, {}),
});

export const connectLink = connectLinkApi(client);

/** Terminal states stop the status poll. */
export const isFinal = (s: ConnectLinkState) => s === 'connected' || s === 'expired' || s === 'revoked';

/** Seconds left until `expiresAt` (epoch s), floored at 0. */
export const secondsLeft = (expiresAt: number, nowMs: number) => Math.max(0, Math.floor(expiresAt - nowMs / 1000));

/** "29:59" / "1:02:03" / "0:00" countdown label. */
export function countdown(secs: number): string {
  const s = Math.max(0, Math.floor(secs));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/**
 * The state the panel shows. The server's state wins; a pending link whose
 * countdown hit zero reads `expired` locally before the next poll confirms it.
 */
export function effectiveState(state: ConnectLinkState, left: number): ConnectLinkState {
  return state === 'pending' && left <= 0 ? 'expired' : state;
}

/** Owner-facing line per state. */
export function stateLine(s: ConnectLinkState, deviceId?: string | null): string {
  switch (s) {
    case 'pending':
      return 'Waiting for your AI to open the link…';
    case 'fetched':
      return 'Your AI opened the link. Waiting for the computer to connect…';
    case 'connected':
      return deviceId ? `Connected as ${deviceId}.` : 'Connected.';
    case 'expired':
      return 'This link expired before it was used. Make a new one.';
    case 'revoked':
      return 'Link revoked. It no longer works.';
  }
}

/** The one line the owner pastes. Only the URL: the instructions live at the link. */
export const pasteHint = (url: string) => `Connect this computer to Swingshift: ${url}`;
