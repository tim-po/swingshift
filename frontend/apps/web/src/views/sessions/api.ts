import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { sessionsApi } from '@loopyard/api';
import { client } from '../../api';
import { config } from '../../config';

const api = sessionsApi(client);
const KEY = ['sessions'];

export const useSessions = (poll = config.pollMs) => useQuery({ queryKey: KEY, queryFn: api.list, refetchInterval: poll });

export function useDispatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ session, text }: { session: string; text: string }) => {
      const r = await api.dispatch(session, text);
      if (r.error) throw new Error(r.error);
      if (!(r.ok || r.task_id)) throw new Error(`could not dispatch to ${session}`);
      return r;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}

// ── One-time session-attach link (owner side of tracking_ui/session_attach_api.py) ──

export type AttachState = 'pending' | 'consumed' | 'revoked' | 'expired';

export interface AttachLink {
  url: string;
  expiresAt: number;
  attachId: string;
  connectorId: string;
  ttl?: number;
  runtime?: string;
  publicUrlConfigured?: boolean;
}

export interface Attach {
  attachId: string;
  connectorId: string;
  state: AttachState;
  secondsLeft: number;
  expiresAt: number;
  createdAt?: number;
  label?: string | null;
  runtime?: string;
  attached: boolean;
  live: boolean;
}

export interface AttachesResponse {
  attaches: Attach[];
  now?: number;
  error?: string;
}

const ATTACH_KEY = ['sessions', 'attaches'];

export const attachApi = {
  mint: (body: { ttl?: number; label?: string; runtime?: string } = {}) =>
    client.post<AttachLink>('/api/loops/sessions/attach-link', body),
  list: () => client.get<AttachesResponse>('/api/loops/sessions/attaches'),
  revoke: (attachId: string) => client.post<{ ok?: boolean; error?: string }>('/api/loops/sessions/attach/revoke', { attachId }),
};

/** Polls fast while a link is waiting for its session, so 'attached ✓' flips promptly. */
export const useAttaches = (waiting: boolean) =>
  useQuery({ queryKey: ATTACH_KEY, queryFn: attachApi.list, refetchInterval: waiting ? 2000 : config.pollMs });

export function useMintAttachLink() {
  const qc = useQueryClient();
  return useMutation({ mutationFn: attachApi.mint, onSuccess: () => qc.invalidateQueries({ queryKey: ATTACH_KEY }) });
}

export function useRevokeAttach() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (attachId: string) => {
      const r = await attachApi.revoke(attachId);
      if (r.error) throw new Error(r.error);
      return r;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: KEY }),
  });
}
