import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { qualityApi, rolesApi, type RegistryResponse } from '@loopyard/api';
import { client } from '../../api';
import { config } from '../../config';
import { platform } from '../../platform';

export const api = rolesApi(client);
export const quality = qualityApi(client);
const REG = ['roles', 'registry'];
const q = encodeURIComponent;

// ── registry v2: goal distiller + per-version diff (roles/registry routes) ──
export interface DistillOffer {
  id: string;
  version?: number;
  current: string;
  suggested: string;
  basis: 'clean' | 'trimmed' | 'role-default';
  changed: boolean;
  findings?: { level: string; rule: string; message: string }[];
  error?: string;
}
export interface FieldDiff { from?: string | null; to?: string | null; changed: boolean }
export interface AgentDiff {
  id: string;
  versionA: number;
  versionB: number;
  fields: Record<string, FieldDiff>;
  textDiff?: Record<string, string[]>;
  error?: string;
}
export const registryV2 = {
  distill: (id: string) => client.get<DistillOffer>(`/api/loops/agent/distill?id=${q(id)}`),
  acceptDistill: (id: string, goal: string) =>
    client.post<{ ok?: boolean; version?: number; created?: boolean; error?: string }>('/api/loops/agent/distill/accept', { id, goal }),
  diff: (id: string, a: number, b: number) => client.get<AgentDiff>(`/api/loops/agent/diff?id=${q(id)}&a=${a}&b=${b}`),
};

// ── Library quality (owner rating beside analyst score) ──
const PERIOD_KEY = 'library.days';
/** The Library's period (days; 0 = all time), remembered per browser. */
export function usePeriod(): [number, (d: number) => void] {
  const [days, setDays] = useState(() => {
    const n = Number(platform.storage.get(PERIOD_KEY));
    return Number.isFinite(n) && platform.storage.get(PERIOD_KEY) ? n : 30;
  });
  return [days, (d: number) => { platform.storage.set(PERIOD_KEY, String(d)); setDays(d); }];
}
/** Scores up to 5 finished-but-unscored runs per read (server-capped); the rest arrive on later polls. */
export const useQuality = (days: number, project: string) =>
  useQuery({
    queryKey: ['library', 'quality', days, project],
    queryFn: () => quality.quality({ days, project, compute: 5 }),
    refetchInterval: config.pollMs * 6,
  });

export const useAnalytics = (poll = config.pollMs * 6) =>
  useQuery({ queryKey: ["roles", "analytics"], queryFn: api.analytics, refetchInterval: poll });
export const useRegistry = () => useQuery({ queryKey: REG, queryFn: api.registry });
export const useAgentDetail = (id: string) => useQuery({ queryKey: ['roles', 'agent', id], queryFn: () => api.detail(id) });
export const useAgentRecord = (id: string) => useQuery({ queryKey: ['roles', 'record', id], queryFn: () => api.record(id), retry: false });
export const useAgentVersions = (id: string) => useQuery({ queryKey: ['roles', 'versions', id], queryFn: () => api.versions(id), retry: false });
export const useDistill = (id: string, enabled = true) =>
  useQuery({ queryKey: ['roles', 'distill', id], queryFn: () => registryV2.distill(id), enabled, retry: false });
export const useVersionDiff = (id: string, a: number, b: number, enabled = true) =>
  useQuery({ queryKey: ['roles', 'diff', id, a, b], queryFn: () => registryV2.diff(id, a, b), enabled: enabled && a >= 1 && b > a, retry: false, staleTime: Infinity });

/** Accept a distilled goal — forks a new immutable version, then re-reads the record + timeline. */
export function useAcceptDistill(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (goal: string) => {
      const r = await registryV2.acceptDistill(id, goal);
      if (r?.error) throw new Error(r.error);
      return r;
    },
    onSuccess: () => {
      for (const k of ['record', 'versions', 'distill']) qc.invalidateQueries({ queryKey: ['roles', k, id] });
      qc.invalidateQueries({ queryKey: REG });
    },
  });
}
export const useTurn = (t: { loop: string; agent: string; seq: number } | null) =>
  useQuery({
    queryKey: ['roles', 'turn', t?.loop, t?.agent, t?.seq],
    queryFn: () => api.turn(t!.loop, t!.agent, t!.seq),
    enabled: !!t,
  });

/** Favorite toggle — optimistic on the registry cache, then re-read. */
/** One-click "Save to library" for an ad-hoc agent — the existing loop_save_agent path, then re-reads registry + analytics. */
export function useSaveAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, fromLoop }: { id: string; fromLoop: string }) => {
      const r = await api.saveAgent(id, fromLoop);
      if (!r || r.error || !r.ok) throw new Error(r?.error || "Couldn't save agent");
      return r;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: REG });
      qc.invalidateQueries({ queryKey: ['roles', 'analytics'] });
    },
  });
}

export function useFavorite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, favorite }: { id: string; favorite: boolean }) => {
      const r = await api.favorite(id, favorite);
      if (r?.error) throw new Error(r.error);
      return r;
    },
    onMutate: async ({ id, favorite }) => {
      await qc.cancelQueries({ queryKey: REG });
      const prev = qc.getQueryData<RegistryResponse>(REG);
      if (prev) {
        const has = prev.agents.some((a) => a.id === id);
        const agents = has
          ? prev.agents.map((a) => (a.id === id ? { ...a, favorite } : a))
          : favorite
            ? [...prev.agents, { id, note: '', favorite: true }]
            : prev.agents;
        qc.setQueryData<RegistryResponse>(REG, { ...prev, agents });
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => ctx?.prev && qc.setQueryData(REG, ctx.prev),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: REG });
      qc.invalidateQueries({ queryKey: ['library', 'quality'] });
    },
  });
}
