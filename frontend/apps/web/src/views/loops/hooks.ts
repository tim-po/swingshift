// Queries + mutations for the loop detail pane. POSTs mutate live data.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { loopDetailApi, withResponsibilities, type ActionBody, type DispositionBody, type TeamRoom } from '@loopyard/api';
import { client } from '../../api';
import { config } from '../../config';
import { ownerKey, useLoopOwnerScope } from './ownerScope';

export const detailApi = loopDetailApi(client);

const keys = {
  detail: (host: string, name: string) => ['loop', host, name] as const,
  teamRoom: (host: string, name: string) => ['loop-teamroom', host, name] as const,
};

export function useLoopDetail(name: string, host: string) {
  const owner = useLoopOwnerScope();
  return useQuery({ queryKey: [...keys.detail(host, name), ...ownerKey(owner)], queryFn: () => detailApi.detail(name, host, 150, owner), refetchInterval: config.pollMs });
}

/** The team room, with per-loop charters overlaid from the config. null → engine doesn't serve it. */
export function useTeamRoom(name: string, host: string) {
  const owner = useLoopOwnerScope();
  return useQuery({
    queryKey: [...keys.teamRoom(host, name), ...ownerKey(owner)],
    refetchInterval: config.pollMs,
    queryFn: async (): Promise<TeamRoom | null> => {
      let tr: TeamRoom;
      try {
        tr = await detailApi.teamRoom(name, host, owner);
      } catch {
        return null; // older/remote engine → fall back to the legacy stat tiles
      }
      if (!tr || tr.error || !Array.isArray(tr.roster)) return null;
      try {
        return withResponsibilities(tr, await detailApi.config(name, owner));
      } catch {
        return tr; // config unavailable → keep the backend `owns`, never blank it
      }
    },
  });
}

/** '' = the whole fleet (never builds `/projects//dispositions`). */
export const useDispositions = (project = '') =>
  useQuery({ queryKey: ['dispositions', project], queryFn: () => detailApi.dispositions(project), refetchInterval: config.pollMs * 6 });

export function useAgentReports(name: string, agent: string | null, host: string) {
  const owner = useLoopOwnerScope();
  return useQuery({ queryKey: ['loop-agent-reports', host, name, agent, ...ownerKey(owner)], queryFn: () => detailApi.agentReports(name, agent!, host, owner), enabled: !!agent });
}

export function useTurn(name: string, turn: { agent: string; seq: number } | null, host: string) {
  const owner = useLoopOwnerScope();
  return useQuery({ queryKey: ['loop-turn', host, name, turn?.agent, turn?.seq, ...ownerKey(owner)], queryFn: () => detailApi.turn(name, turn!.agent, turn!.seq, host, owner), enabled: !!turn });
}

export function useFiles(name: string, enabled: boolean) {
  const owner = useLoopOwnerScope();
  return useQuery({ queryKey: ['loop-files', name, ...ownerKey(owner)], queryFn: () => detailApi.files(name, owner), enabled });
}

/** The REAL tmux adopt-the-manager briefing session (owner health smoke-test) — NOT the native chat. */
export const useBriefLoop = (name: string) =>
  useMutation({
    mutationFn: (phase: 'brief' | 'debrief') => (phase === 'debrief' ? detailApi.debrief(name) : detailApi.brief(name)),
  });

function useInvalidateLoop(name: string, host: string) {
  const qc = useQueryClient();
  return () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: keys.detail(host, name) }),
      qc.invalidateQueries({ queryKey: keys.teamRoom(host, name) }),
      qc.invalidateQueries({ queryKey: ['loops'] }),
      qc.invalidateQueries({ queryKey: ['dispositions'] }),
      qc.invalidateQueries({ queryKey: ['loop-goodness', name] }),
    ]);
}

export function useLoopAction(name: string, host: string) {
  const invalidate = useInvalidateLoop(name, host);
  return useMutation({
    mutationFn: (body: Omit<ActionBody, 'host'>) => detailApi.action(name, { ...body, host }),
    onSettled: invalidate,
  });
}

export const useAnalyze = (name: string) => useMutation({ mutationFn: () => detailApi.analyze(name) });

export function useDisposition(name: string, host: string) {
  const invalidate = useInvalidateLoop(name, host);
  return useMutation({ mutationFn: (body: DispositionBody) => detailApi.disposition(name, body), onSettled: invalidate });
}

/** Better-UX #6 — poll a Steer's phase (stopping → handoff → ready) while one is in flight. */
export const useSteerStatus = (name: string, enabled: boolean) =>
  useQuery({ queryKey: ['loop-steer', name], queryFn: () => detailApi.steerStatus(name), enabled, refetchInterval: enabled ? 2000 : false });

/** Run observability — owner rating + analyst score, as two separate fields (never blended). */
export function useGoodness(name: string, enabled: boolean) {
  const owner = useLoopOwnerScope();
  return useQuery({ queryKey: ['loop-goodness', name, ...ownerKey(owner)], queryFn: () => detailApi.goodness(name, false, owner), enabled, refetchInterval: enabled ? config.pollMs * 3 : false });
}

/** Re-run the analyst pass over the finished run; the fresh payload replaces the cached one. */
export function useRescore(name: string) {
  const qc = useQueryClient();
  const owner = useLoopOwnerScope();
  return useMutation({
    mutationFn: () => detailApi.goodness(name, true, owner),
    onSuccess: (g) => qc.setQueryData(['loop-goodness', name, ...ownerKey(owner)], g),
  });
}

/** Run observability — the capped one-line-per-turn thought-log. */
export function useThoughtlog(name: string, enabled: boolean) {
  const owner = useLoopOwnerScope();
  return useQuery({ queryKey: ['loop-thoughtlog', name, ...ownerKey(owner)], queryFn: () => detailApi.thoughtlog(name, { limit: 200, owner }), enabled, refetchInterval: enabled ? config.pollMs : false });
}
