// /api/loops/origins* — the boxes loops run on, their CLI capabilities, and the
// one shared health-aware origin picker source.
import type { Client } from './client';
import type { LoopSummary } from './types';

export interface OriginCliCapability {
  cli: string;
  authed: boolean | 'unknown';
  applied?: boolean;
  note?: string;
  lastProbed?: number;
  /** Probed on the remote computer itself (capabilities.probe over the origin channel). */
  remote?: boolean;
}

export interface Origin {
  id: string;
  name?: string;
  path?: string | null;
  kind?: string; // 'local' | 'mirror' | 'connected-session'
  loops?: number;
  last_seen?: number | null;
  health?: string; // 'pending' | 'stale' | 'cold' | …
  reachable?: boolean;
  stale?: boolean;
  connectedAt?: number | null;
  address?: string | null;
  capabilities?: OriginCliCapability[];
  capabilitiesOk?: boolean;
  capabilitiesReason?: string;
  /** 'origin-channel' when a remote computer answered the probe itself. */
  capabilitiesVia?: string;
  sessionCaps?: string[];
  runtime?: string;
  /** This origin is the current Hub (loop_origin_list, from hub_record). */
  isHub?: boolean;
}

export interface OriginsResponse {
  origins: Origin[];
  error?: string;
}

export interface OriginCapsRecord {
  ok: boolean;
  cliCapabilities: OriginCliCapability[];
  reason?: string;
  via?: string;
}

export interface OriginPickerOption {
  id: string;
  name?: string;
  kind?: string;
  reachable?: boolean;
  health?: string | null;
  disabled?: boolean;
  label?: string;
  note?: string | null;
  runtime?: string;
}

export const originsApi = (c: Client) => ({
  list: () => c.get<OriginsResponse>('/api/loops/origins'),
  capabilities: (id: string) => c.get<OriginCapsRecord>(`/api/loops/origin/${encodeURIComponent(id)}/capabilities`),
  picker: () => c.get<{ options: OriginPickerOption[] }>('/api/loops/origin-picker'),
});

// ── Pure view-model helpers ──

export const isSessionOrigin = (o: Origin) => (o.kind || '').toLowerCase() === 'connected-session';
export const isLocalOrigin = (o: Origin) => (o.kind || '').toLowerCase() === 'local' || o.id === 'local';
export const originReachable = (o: Origin) => (o.reachable !== undefined ? o.reachable : isLocalOrigin(o));

/** Honest, never-inflated summary: boxes only; sessions counted separately. */
export function originCountLabel(origins: Origin[]): string {
  const boxes = origins.filter((o) => !isSessionOrigin(o));
  const b = boxes.length;
  const r = boxes.filter(originReachable).length;
  const s = origins.length - b;
  return `${b} origin${b === 1 ? '' : 's'} · ${r} reachable` + (s ? ` · ${s} session${s === 1 ? '' : 's'}` : '');
}

export type OriginPill = 'live' | 'pending' | 'stale' | 'off';

export interface OriginHealth {
  pill: OriginPill;
  /** Exact cause for an off/stale/pending origin; '' when healthy. */
  cause: string;
  /** Reachability chip label + its explanation. */
  reachLabel: string;
  reachTip: string;
}

export function originHealth(o: Origin, agoFn: (t: number) => string): OriginHealth {
  const local = isLocalOrigin(o);
  const session = isSessionOrigin(o);
  const reachable = originReachable(o);
  const stale = !!o.stale;
  const pending = (o.health || '') === 'pending';
  const pill: OriginPill = local || reachable ? 'live' : pending ? 'pending' : stale ? 'stale' : 'off';
  let cause = '';
  if (session) {
    if (!reachable) cause = `session idle · last seen ${o.last_seen ? agoFn(o.last_seen) : 'unknown'} (no heartbeat >90s)`;
  } else if (pending) {
    cause = `connected via yard · waiting for first sync${o.connectedAt ? ' · added ' + agoFn(o.connectedAt) : ''}${o.address ? ' · ' + o.address : ''}`;
  } else if (!local && !reachable) {
    cause = o.last_seen ? `unreachable · last seen ${agoFn(o.last_seen)}` : 'unreachable · no sync ever seen from this box';
  }
  const reachLabel = session
    ? reachable ? 'live session' : 'idle'
    : reachable ? 'reachable' : pending ? 'pending' : stale ? 'stale' : 'unreachable';
  const reachTip = session
    ? reachable ? 'heartbeat within 90s' : 'no heartbeat for >90s'
    : reachable ? 'mirror mtime is within FRESH_SEC'
    : pending ? 'connected via yard, awaiting first sync'
    : stale ? 'seen once, no recent sync' : 'no evidence of sync';
  return { pill, cause, reachLabel, reachTip };
}

/** Capabilities embedded in the origins list win; else the lazily-probed record. */
export function originCaps(o: Origin, probed?: OriginCapsRecord): OriginCapsRecord | undefined {
  if (o.capabilities !== undefined)
    return { ok: o.capabilitiesOk !== false, cliCapabilities: o.capabilities || [], reason: o.capabilitiesReason, via: o.capabilitiesVia };
  return probed;
}

export function originCapLabel(c: OriginCliCapability): { text: string; tone?: 'phos' | 'ember' } {
  if (c.authed === true) return { text: `${c.cli} · authed · recorded`, tone: 'phos' };
  if (c.authed === false) return { text: `${c.cli} · no auth · recorded` };
  return { text: `${c.cli} · auth unknown · recorded`, tone: 'ember' };
}

export const loopsForOrigin = (loops: LoopSummary[], oid: string) => loops.filter((l) => (l.origin || l.host) === oid);

// ── Plain-words roster (the Machines → Computers tab) ──
// The helpers above keep the protocol vocabulary (origin / mirror / reachable)
// for tooltips and power users; these are what a newcomer reads.

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`;

/** Real computers only — connected sessions live on the Sessions tab. */
export const computerOrigins = (origins: Origin[]) => origins.filter((o) => !isSessionOrigin(o));

/** Online = this dashboard's own computer, or a remote one syncing right now. */
export const computerOnline = (o: Origin) => isLocalOrigin(o) || originReachable(o);

/** "2 computers · 1 online" — sessions are never counted. */
export function computerCountLabel(origins: Origin[]): string {
  const list = computerOrigins(origins);
  return `${plural(list.length, 'computer')} · ${list.filter(computerOnline).length} online`;
}

/** What kind of computer this is, in plain words, plus the precise term for a tooltip. */
export function computerKindLabel(o: Origin): { text: string; tip: string } {
  if (isLocalOrigin(o)) return { text: 'This computer', tip: 'Local origin: the computer running this dashboard' };
  if ((o.health || '') === 'pending') return { text: 'Remote computer', tip: 'Connected with yard, waiting for its first sync' };
  return { text: 'Remote computer', tip: `${o.kind || 'mirror'} origin: its loops sync to this dashboard` };
}

/** "Online", "Connecting…", "Last seen 20d ago" or "Not reachable" — strictly from the record. */
export function computerSeenLabel(o: Origin, agoFn: (t: number) => string): string {
  if (computerOnline(o)) return 'Online';
  if ((o.health || '') === 'pending') return 'Connecting…';
  if (o.last_seen) return `Last seen ${agoFn(o.last_seen)}`;
  return 'Not reachable';
}

/** A CLI's name as a person says it: "claude" → "Claude". */
export const cliName = (cli: string) => (cli ? cli[0].toUpperCase() + cli.slice(1) : cli);

/** Whether a recorded CLI is signed in, in plain words. */
export function cliSignInLabel(c: OriginCliCapability): string {
  if (c.authed === true) return 'signed in';
  if (c.authed === false) return 'not signed in';
  return 'sign-in unknown';
}
