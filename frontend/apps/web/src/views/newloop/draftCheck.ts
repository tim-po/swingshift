// The live check behind the draft editor: debounce the edited config, then ask the
// server's /api/loops/creator/draft-check (real loop_lint + the draft's schematic).
// Pure read — nothing is saved until the user says so.
import { useEffect, useState } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import type { LoopConfig } from '@loopyard/api';
import { client } from '../../api';
import type { DraftCheck, ScaffoldResult } from './draft';

export const DRAFT_CHECK_MS = 400;

export const draftCheck = (config: LoopConfig) => client.post<DraftCheck>('/api/loops/creator/draft-check', { config });

/** "＋ Add an agent": the real loop_creator_scaffold fills the new agent's role defaults. Never saves. */
export const scaffoldDraft = (spec: unknown) => client.post<ScaffoldResult>('/api/loops/creator/scaffold', { spec });

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function useDraftCheck(config: LoopConfig | null) {
  const key = config ? JSON.stringify(config) : '';
  const settled = useDebounced(key, DRAFT_CHECK_MS);
  return useQuery({
    queryKey: ['creator', 'draft-check', settled],
    queryFn: () => draftCheck(JSON.parse(settled) as LoopConfig),
    enabled: !!settled,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
}
