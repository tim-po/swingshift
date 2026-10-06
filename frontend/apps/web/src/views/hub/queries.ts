import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { chatEventsPath, hubApi, threadPending } from '@loopyard/api';
import { client } from '../../api';
import { config } from '../../config';
import { subscribeThread, type ESCtor, type LiveMode } from './liveThread';

export const hub = hubApi(client);

export const hubKeys = {
  docs: ['hub', 'docs'] as const,
  doc: (id: string) => ['hub', 'doc', id] as const,
  thread: (id: string) => ['hub', 'thread', id] as const,
  sessions: ['hub', 'sessions'] as const,
};

export const useDocs = (poll = config.pollMs) => useQuery({ queryKey: hubKeys.docs, queryFn: hub.list, refetchInterval: poll });

export const useDoc = (id: string | undefined) =>
  useQuery({ queryKey: hubKeys.doc(id ?? ''), queryFn: () => hub.get(id!), enabled: !!id });

/** The initial read. While a live push is active it doesn't poll; otherwise it
 *  re-reads only while an agent turn is pending — never fabricates a reply. */
export const useThread = (id: string | undefined, pushed = false) =>
  useQuery({
    queryKey: hubKeys.thread(id ?? ''),
    queryFn: () => hub.thread(id!),
    enabled: !!id,
    refetchInterval: (q) => (!pushed && threadPending(q.state.data) ? 3000 : false),
  });

/** Subscribe the thread to live delivery (SSE, long-poll fallback); every pushed
 *  view lands in the same query cache `useThread` reads. Returns the transport state. */
export function useThreadLive(id: string | undefined): { mode: LiveMode; why?: string } {
  const qc = useQueryClient();
  const [st, setSt] = useState<{ mode: LiveMode; why?: string }>({ mode: 'connecting' });
  useEffect(() => {
    if (!id) return;
    setSt({ mode: 'connecting' });
    return subscribeThread({
      url: config.apiBase + chatEventsPath(id),
      withCredentials: !!config.apiBase,
      EventSource: (globalThis as { EventSource?: ESCtor }).EventSource,
      wait: (rev) => hub.wait(id, rev),
      onView: (v) => qc.setQueryData(hubKeys.thread(id), v),
      onMode: (mode, why) => setSt((p) => (p.mode === mode && p.why === why ? p : { mode, why })),
    });
  }, [id, qc]);
  return st;
}

export const useHubSessions = (enabled: boolean) =>
  useQuery({ queryKey: hubKeys.sessions, queryFn: hub.sessions, enabled, refetchInterval: 30000 });
