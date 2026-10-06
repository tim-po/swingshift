// The always-on Hub (mcp_loops/hub_record.py via /api/loops/origin-hub): which
// machine new origins join, its public URL + cert fingerprint, and whether it is
// up. The Origins page shows it and switches it; one-time connect links are
// minted against it, so the owner never types a hub URL. Typed client + pure
// view-model helpers; the card + useHub live in HubCard.tsx.
import type { Client } from '@loopyard/api';
import { client } from '../../api';

export type HubMode = 'self' | 'remote';

/** GET /api/loops/origin-hub — the record plus a live TCP probe. */
export interface HubStatus {
  mode: HubMode;
  originId?: string | null;
  host?: string | null;
  publicUrl?: string | null;
  fingerprint?: string | null;
  /** A box could pair against it now: a URL is known (+ a fingerprint for wss). */
  configured: boolean;
  /** Why it is not `configured` (owner-facing, written by the backend). */
  reason?: string;
  live?: boolean;
  checkedAt?: number;
  updatedAt?: number | null;
}

/** POST /api/loops/origin-hub — switch which machine is the Hub. */
export type HubSwitch =
  | { mode: 'self' }
  | { mode: 'remote'; publicUrl: string; fingerprint?: string; originId?: string; host?: string };

const BASE = '/api/loops/origin-hub';

export const hubApi = (c: Client) => ({
  status: () => c.get<HubStatus>(BASE),
  set: (input: HubSwitch) => c.post<HubStatus>(BASE, input),
});

export const hub = hubApi(client);

export const HUB_QUERY_KEY = ['origin-hub'] as const;
export const HUB_POLL_MS = 30_000;

// ── Pure view-model helpers ──

export type HubPill = 'live' | 'pending' | 'off';

/** live = pairable and answering; pending = not pairable yet; off = pairable but down. */
export function hubPill(h: HubStatus): HubPill {
  if (!h.configured) return 'pending';
  return h.live === false ? 'off' : 'live';
}

/** One owner-facing line for the Hub's state. */
export function hubLine(h: HubStatus): string {
  switch (hubPill(h)) {
    case 'live':
      return h.mode === 'self' ? 'Running on this machine. New origins join it.' : `New origins join ${hubName(h)}.`;
    case 'off':
      return h.mode === 'self'
        ? "This machine is the Hub, but it isn't answering right now. New origins can't join until it's back."
        : `${hubName(h)} isn't answering right now. New origins can't join until it's back.`;
    case 'pending':
      return h.reason ? `Not ready yet: ${h.reason}.` : 'Not ready yet.';
  }
}

/** "this machine (vps-1)" / the remote origin's name. */
export function hubName(h: HubStatus): string {
  if (h.mode === 'self') return h.host ? `this machine (${h.host})` : 'this machine';
  return h.host || h.originId || 'another machine';
}

/** "ab:cd:ef:01…9a:bc": enough to eyeball against the Hub's boot line. */
export function shortFingerprint(fp?: string | null): string {
  if (!fp) return '';
  const parts = fp.split(':');
  return parts.length > 6 ? `${parts.slice(0, 4).join(':')}…${parts.slice(-2).join(':')}` : fp;
}

/** Links are minted where the Hub runs; a box pointed at another Hub can't mint. */
export const canMintHere = (h?: HubStatus | null) => !h || h.mode === 'self';

const FP_RE = /^[0-9a-f]{2}(:[0-9a-f]{2}){31}$/;

/**
 * Client-side check for "another origin is the Hub", mirroring hub_record.set_hub
 * so the owner gets the reason before a round trip. Returns an error line or null.
 */
export function remoteHubError(url: string, fingerprint: string): string | null {
  const u = url.trim();
  const fp = fingerprint.trim().toLowerCase();
  if (!u) return "Enter the Hub's address, e.g. wss://hub.example.com/hub.";
  if (!/^wss?:\/\//i.test(u)) return 'The address starts with wss:// (or ws:// for a Hub on this same machine).';
  if (fp && !FP_RE.test(fp)) return 'The fingerprint is 32 pairs of hex separated by colons, as the Hub prints it at start.';
  if (/^wss:\/\//i.test(u) && !fp) return "A wss:// Hub needs its fingerprint: new machines check it so they can't be pointed at an impostor.";
  return null;
}
