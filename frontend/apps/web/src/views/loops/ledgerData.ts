// What the ledger reads besides the loops: plans (the loops' workstreams) and the
// analyst's scores. Its own module so a view test can stand it in with fixtures.
import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { loopKey, qualityApi, type LoopSummary } from '@loopyard/api';
import { client } from '../../api';
import { config } from '../../config';
import { hub, hubKeys, useDocs } from '../hub/queries';

const quality = qualityApi(client);

export const useLedgerDocs = () => useDocs(config.pollMs * 2);

/** loopKey → analyst score, for the loops that have one (read-only; never computes). */
export function useLoopScores(): Map<string, number> {
  const q = useQuery({ queryKey: ['loops', 'scores'], queryFn: () => quality.quality({ days: 3650, compute: 0 }), refetchInterval: config.pollMs * 12 });
  return useMemo(() => {
    const m = new Map<string, number>();
    for (const l of q.data?.loops ?? []) if (l.analyst && !l.analystPending) m.set(loopKey({ host: l.origin || 'local', name: l.name } as LoopSummary), l.analyst.score);
    return m;
  }, [q.data]);
}

/** Moving loops between plans, and making a plan: the ledger's only writes. */
export const planWrites = {
  docsKey: hubKeys.docs,
  move: (loops: string[], to: string) => hub.move(loops, to),
  create: (i: { project?: string; title: string }) => hub.create(i),
};
