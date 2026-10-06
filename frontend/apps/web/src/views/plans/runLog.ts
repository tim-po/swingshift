// A tiny module-level store for LAUNCHED origin-agent runs. A launch is fire-and-
// forget on the Hub Fabric (origin.run), so the transient toast that acknowledges it
// vanishes in seconds — but the human needs a LOUD, PERSISTENT, DISMISSABLE record of
// what is in flight and where its suggestions will land. The store survives route
// changes and view remounts (it lives outside React), and is read via a
// useSyncExternalStore hook so any mounted RunBanner stays live.
import { useSyncExternalStore } from 'react';
import type { WsRunRecord } from '@loopyard/api';

const MAX = 12; // keep the banner bounded — oldest launches fall off the tail
let runs: WsRunRecord[] = [];
let seq = 0;
const subs = new Set<() => void>();
const emit = () => subs.forEach((cb) => cb());

/** Record a launched run. Newest first; returns the stored record (with its id/ts). */
export function recordRun(r: Omit<WsRunRecord, 'id' | 'ts'> & { id?: string; ts?: number }): WsRunRecord {
  const entry: WsRunRecord = { ...r, id: r.id ?? `run_${Date.now()}_${seq++}`, ts: r.ts ?? Date.now() };
  runs = [entry, ...runs].slice(0, MAX);
  emit();
  return entry;
}

/** Dismiss a single run card (the human's × click). */
export function dismissRun(id: string): void {
  const next = runs.filter((r) => r.id !== id);
  if (next.length !== runs.length) {
    runs = next;
    emit();
  }
}

/** Clear the whole run log (used by tests; no UI affordance yet). */
export function clearRuns(): void {
  runs = [];
  emit();
}

/** Live view of the run log for a mounted component. */
export function useRunLog(): WsRunRecord[] {
  return useSyncExternalStore(
    (cb) => {
      subs.add(cb);
      return () => subs.delete(cb);
    },
    () => runs,
    () => runs,
  );
}
