// /api/loops/fleet — the compute fleet summary behind the top-bar pill.
import type { Client } from './client';

export interface FleetItem {
  id: string;
  name?: string;
  kind?: string;
  health?: string;
  reachable: boolean;
  loops?: number;
  dot?: 'amber' | string;
}

export interface FleetSummary {
  origins?: number;
  reachable?: number;
  label?: string;
  dots?: string;
  dropped?: { id: string; name?: string; health?: string; loops?: number }[];
  dropCount?: number;
  items?: FleetItem[];
  error?: string;
}

export const fleetApi = (c: Client) => ({
  summary: () => c.get<FleetSummary>('/api/loops/fleet'),
});

export type FleetDot = 'on' | 'off' | 'amber';
export const fleetDot = (i: FleetItem): FleetDot => (i.dot === 'amber' ? 'amber' : i.reachable ? 'on' : 'off');

export function fleetLabel(f: FleetSummary | undefined): string {
  if (!f) return 'Fleet';
  return 'Fleet · ' + (f.label || `${f.origins || 0} · ${f.reachable || 0} reachable`);
}

export function fleetItemMeta(i: FleetItem): string {
  return [i.reachable ? 'reachable' : 'unreachable', i.health || '', i.loops ? `${i.loops} loop${i.loops === 1 ? '' : 's'}` : '']
    .filter(Boolean)
    .join(' · ');
}
