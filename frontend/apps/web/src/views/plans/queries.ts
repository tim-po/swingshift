// Plans data: the workspace store (a plan = an objective folder of statused notes),
// its suggestions, and the Run-as-loop mutation (build + link, then start).
import { useQuery } from '@tanstack/react-query';
import { creatorApi, originsApi, workspaceApi, WS_INBOX_ID } from '@loopyard/api';
import { client } from '../../api';
import { config } from '../../config';

export const ws = workspaceApi(client);
const origins = originsApi(client);
const creator = creatorApi(client);

export const wsKeys = {
  objectives: ['ws', 'objectives'] as const,
  objective: (id: string) => ['ws', 'objective', id] as const,
  doc: (oid: string, slug: string) => ['ws', 'doc', oid, slug] as const,
  suggestions: (oid: string) => ['ws', 'suggestions', oid] as const,
  toDiscuss: (oid: string) => ['ws', 'to-discuss', oid] as const,
  origins: ['ws', 'origins'] as const,
};

export const useObjectives = (poll = config.pollMs) =>
  useQuery({ queryKey: wsKeys.objectives, queryFn: ws.listObjectives, refetchInterval: poll });

/** One plan. Polls gently so a loop pointed at it elsewhere shows up here. */
export const useObjective = (id: string | undefined) =>
  useQuery({ queryKey: wsKeys.objective(id ?? ''), queryFn: () => ws.getObjective(id!), enabled: !!id, refetchInterval: config.pollMs * 2 });

export const useDoc = (oid: string | undefined, slug: string | undefined) =>
  useQuery({ queryKey: wsKeys.doc(oid ?? '', slug ?? ''),queryFn: () => ws.getDoc(oid!, slug!), enabled: !!oid && !!slug });

/** Suggestions poll on a slower cadence — agent jobs land asynchronously. */
export const useSuggestions = (oid: string | undefined) =>
  useQuery({ queryKey: wsKeys.suggestions(oid ?? ''), queryFn: () => ws.suggestions(oid!), enabled: !!oid, refetchInterval: 8000 });

/** Pending plan ideas an agent found (the fixed inbox). Shares its key with useSuggestions. */
export const useInboxCandidates = () =>
  useQuery({ queryKey: wsKeys.suggestions(WS_INBOX_ID), queryFn: () => ws.suggestions(WS_INBOX_ID), refetchInterval: 8000 });

export const useToDiscuss = (oid: string | undefined) =>
  useQuery({ queryKey: wsKeys.toDiscuss(oid ?? ''), queryFn: () => ws.toDiscuss(oid!), enabled: !!oid });

export const useRunOrigins = (enabled: boolean) =>
  useQuery({ queryKey: wsKeys.origins, queryFn: origins.picker, enabled });

/** Start a saved loop through the normal loop action seam. */
export const startLoop = (name: string) => creator.start(name);

/** The loop detail route for a loop on this hub. */
export const loopRoute = (name: string, host = 'local') => `/loops/${encodeURIComponent(host)}/${encodeURIComponent(name)}`;

export const planRoute = (oid: string, slug?: string) =>
  '/plans/' + encodeURIComponent(oid) + (slug ? '/' + encodeURIComponent(slug) : '');
