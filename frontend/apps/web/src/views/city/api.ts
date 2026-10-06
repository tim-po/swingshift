import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { loopDetailApi, overviewApi, qualityApi, type OverviewLoop } from '@loopyard/api';
import { client } from '../../api';
import { config } from '../../config';
import { detailKey, type CityDoc, type LoopDetails } from './snapshot';

const overview = overviewApi(client);
const quality = qualityApi(client);
const detail = loopDetailApi(client);
const slow = { refetchInterval: config.pollMs * 6 };

// Own query keys: other views cache these endpoints under their own shapes.
export const useCityQuality = () => useQuery({ queryKey: ['city', 'quality'], queryFn: () => quality.quality({ days: 3650 }), ...slow });
export const useCityIssues = () => useQuery({ queryKey: ['city', 'issues'], queryFn: overview.issues, ...slow });
export const useCityDocs = () =>
  useQuery({ queryKey: ['city', 'docs'], queryFn: async () => (await overview.docs()) as unknown as { docs: CityDoc[] }, ...slow });

// ── Per-loop details (floors, reports, files): one loop at a time in the background, a few at once.
// A finished loop's details never change, so they're kept for the session; live ones refresh.
const CONCURRENCY = 3;
const LIVE_TTL = 120_000;
const cache = new Map<string, { det: LoopDetails; at: number; final: boolean }>();
const inflight = new Set<string>();
const LIVE = new Set(['running', 'stopping', 'waiting_owner', 'needs_owner']);

async function load(d: OverviewLoop): Promise<LoopDetails> {
  const host = d.host || 'local';
  const local = host === 'local';
  const soft = <T,>(p: Promise<T>) => p.catch(() => null);
  // Config and files are read on this computer only; a remote loop still gets its reports (and is drawn plain).
  const [cfg, tr, files, delivered] = await Promise.all([
    local ? soft(detail.config(d.name)) : Promise.resolve(null),
    soft(detail.teamRoom(d.name, host)),
    local ? soft(detail.files(d.name)) : Promise.resolve(null),
    local ? soft(detail.delivered(d.name)) : Promise.resolve(null),
  ]);
  return { cfg, tr, files, delivered };
}

/** Details for `loops` as they arrive; the map's identity changes after each batch. */
export function useLoopDetails(loops: OverviewLoop[]): Map<string, LoopDetails> {
  const [, bump] = useState(0);
  const want = loops.map((d) => d.host + '/' + d.name + ':' + d.state).join('|');
  useEffect(() => {
    let alive = true;
    const now = Date.now();
    const todo = loops.filter((d) => {
      const c = cache.get(detailKey(d));
      return !inflight.has(detailKey(d)) && (!c || (!c.final && now - c.at > LIVE_TTL) || (c.final && LIVE.has(d.state)));
    });
    let next = 0;
    const worker = async () => {
      while (alive && next < todo.length) {
        const d = todo[next++];
        const k = detailKey(d);
        inflight.add(k);
        try {
          const det = await load(d);
          cache.set(k, { det, at: Date.now(), final: !LIVE.has(d.state) });
        } finally {
          inflight.delete(k);
        }
        if (alive && (next % 6 === 0 || next === todo.length)) bump((n) => n + 1);
      }
    };
    for (let i = 0; i < CONCURRENCY; i++) void worker();
    return () => {
      alive = false;
    };
    // `want` captures the loops and their states; the array itself changes on every poll.
  }, [want]);
  const out = new Map<string, LoopDetails>();
  for (const d of loops) {
    const c = cache.get(detailKey(d));
    if (c) out.set(detailKey(d), c.det);
  }
  return out;
}
