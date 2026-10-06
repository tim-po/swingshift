// /api/loops/devices — the ONE unified device list any frontend renders.
// A device is a box (an origin/mirror), a connected session, or the dial-out
// origin-agent — folded into a single record with an `owner`, so per-user
// scoping is a later filter, not a reshape. The frontend routes to a device
// THROUGH the hub's channel; it never dials the device directly.
import type { Client } from './client';

/** The three shapes a device can take in the unified registry. */
export type DeviceKind = 'box' | 'session' | 'origin';

export interface Device {
  id: string;
  /** Owner id — defaults to the single pilot owner until multi-tenant lands. */
  owner: string;
  kind: DeviceKind;
  name: string;
  /** Free-form capability labels (CLI names for a box, declared caps for a session). */
  capabilities: string[];
  /**
   * Human address WITHOUT reachability promise — a box is reached through the
   * hub, never dialled directly, so this is a label ("dial-out · via hub",
   * a mirror path, a session host), not a routable endpoint.
   */
  address_free?: string | null;
  last_seen?: number | null;
  /** True when the device has a live channel / recent heartbeat right now. */
  live: boolean;
  /** Opaque handle for the hub's outbound channel to this device, when one is held. */
  channel_ref?: string | null;
}

export interface DevicesResponse {
  devices: Device[];
  /** Echoed owner filter when the request scoped by ?owner=. */
  owner?: string | null;
  count?: number;
  live?: number;
  error?: string;
}

export const devicesApi = (c: Client) => ({
  /** The unified device list. Pass an owner to scope (Phase 5 seam). */
  list: (owner?: string) =>
    c.get<DevicesResponse>('/api/loops/devices' + (owner ? `?owner=${encodeURIComponent(owner)}` : '')),
});

// ── Pure view-model helpers (no I/O, unit-testable) ──

export const isBox = (d: Device) => d.kind === 'box';
export const isSessionDevice = (d: Device) => d.kind === 'session';
export const isOriginAgent = (d: Device) => d.kind === 'origin';

/** Chip label for a device's kind, in the owner's words. */
export function deviceKindLabel(d: Device): string {
  switch (d.kind) {
    case 'box':
      return 'box';
    case 'session':
      return 'session';
    case 'origin':
      return 'dial-out device';
  }
}

export type DevicePill = 'live' | 'idle';

/** Honest liveness pill — `live` strictly from the record, never inferred. */
export function devicePill(d: Device): DevicePill {
  return d.live ? 'live' : 'idle';
}

/**
 * One-line explanation of a device's state, honest about why it's idle. `agoFn`
 * renders a timestamp as a relative string so this stays pure/testable.
 */
export function deviceCause(d: Device, agoFn: (t: number) => string): string {
  if (d.live) return '';
  if (d.last_seen) return `idle · last seen ${agoFn(d.last_seen)}`;
  return 'idle · no channel seen yet';
}

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`;

/** Never-inflated roster summary: "3 devices · 2 live · 1 box · 1 session". */
export function deviceCountLabel(devices: Device[]): string {
  const total = devices.length;
  const live = devices.filter((d) => d.live).length;
  const byKind = (['box', 'session', 'origin'] as DeviceKind[])
    .map((k) => ({ k, n: devices.filter((d) => d.kind === k).length }))
    .filter((x) => x.n > 0)
    .map((x) => plural(x.n, x.k === 'origin' ? 'dial-out device' : x.k))
    .join(' · ');
  return `${plural(total, 'device')} · ${live} live` + (byKind ? ` · ${byKind}` : '');
}

/** Stable ordering: live first, then boxes/origins before sessions, then by name. */
export function sortDevices(devices: Device[]): Device[] {
  const rank: Record<DeviceKind, number> = { box: 0, origin: 1, session: 2 };
  return [...devices].sort(
    (a, b) =>
      Number(b.live) - Number(a.live) ||
      rank[a.kind] - rank[b.kind] ||
      (a.name || a.id).localeCompare(b.name || b.id),
  );
}

/** Distinct owners present, for the Phase 5 owner filter. */
export function deviceOwners(devices: Device[]): string[] {
  return [...new Set(devices.map((d) => d.owner).filter(Boolean))].sort();
}

// ── Plain-words roster (the Machines → All tab) ──
// The helpers above keep the protocol vocabulary (box / dial-out / idle) for
// tooltips and power users; these are what a newcomer reads.

/** Host part of a session's address label ("Tims-MacBook · /Users/tim/proj" → "Tims-MacBook"). */
function sessionHost(d: Device): string {
  return (d.address_free ?? '').split('·')[0].trim();
}

/** What kind of machine this is, in plain words. */
export function machineKindLabel(d: Device): string {
  switch (d.kind) {
    case 'origin':
      return d.id === 'origin:local' || d.name === 'local' ? 'This computer' : 'Computer';
    case 'box':
      return 'Remote computer';
    case 'session': {
      const host = sessionHost(d);
      return host ? `Session on ${host}` : 'Session';
    }
  }
}

/** "Online", "Last seen 20d ago" or "Not connected yet" — strictly from the record. */
export function machineSeenLabel(d: Device, agoFn: (t: number) => string): string {
  if (d.live) return 'Online';
  if (d.last_seen) return `Last seen ${agoFn(d.last_seen)}`;
  return 'Not connected yet';
}

/** "3 machines · 1 online" — the page summary. */
export function machineCountLabel(devices: Device[]): string {
  const live = devices.filter((d) => d.live).length;
  return `${plural(devices.length, 'machine')} · ${live} online`;
}

/** Capabilities as quiet text ("claude · codex"), '' when none declared. */
export const machineCapsLabel = (d: Device) => d.capabilities.join(' · ');
