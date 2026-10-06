// Creator / editor queries + mutations. Keys are namespaced under 'creator' so
// they never share a cache entry with another view's differently-shaped data.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { creatorApi } from '@loopyard/api';
import { client } from '../../api';
import { config } from '../../config';

export const creator = creatorApi(client);

export const useRegistryAgents = () =>
  useQuery({ queryKey: ['creator', 'agents'], queryFn: creator.agents, staleTime: 30_000 });

/** A loop's saved config — fetched once for editing (no polling under the user's edits). */
export const useLoopConfig = (name: string) =>
  useQuery({ queryKey: ['creator', 'config', name], queryFn: () => creator.config(name), enabled: !!name, staleTime: Infinity });

export const useCreatorDocs = () =>
  useQuery({ queryKey: ['creator', 'docs'], queryFn: creator.docs, refetchInterval: config.pollMs * 6 });

export const useCreatorIssues = () =>
  useQuery({ queryKey: ['creator', 'issues'], queryFn: creator.issues, refetchInterval: config.pollMs * 6 });

/** Seeded-context summary for the briefing terminal (fetched when Advanced opens). */
export const useCreatorPrompt = (enabled: boolean) =>
  useQuery({ queryKey: ['creator', 'prompt'], queryFn: creator.prompt, enabled, staleTime: 60_000 });

/** Invalidate what a save/clone/start changes. */
export function useInvalidateLoops() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ['loops'] });
    void qc.invalidateQueries({ queryKey: ['creator', 'config'] });
  };
}

export const useValidate = () => useMutation({ mutationFn: creator.validate });
export const useSuggest = () => useMutation({ mutationFn: (goal: string) => creator.suggest(goal) });
export const useBrief = () => useMutation({ mutationFn: creator.brief });

export function useSave() {
  const inv = useInvalidateLoops();
  return useMutation({ mutationFn: creator.save, onSuccess: inv });
}
export function useStart() {
  const inv = useInvalidateLoops();
  return useMutation({ mutationFn: creator.start, onSuccess: inv });
}
export function useClone() {
  const inv = useInvalidateLoops();
  return useMutation({ mutationFn: (v: { from: string; name: string }) => creator.clone(v.from, v.name), onSuccess: inv });
}

/** Loop detail route for a (local) loop. */
export const loopPath = (name: string, host = 'local') => `/loops/${encodeURIComponent(host)}/${encodeURIComponent(name)}`;
