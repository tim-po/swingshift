import { useQuery } from '@tanstack/react-query';
import { createClient, loopsApi } from '@loopyard/api';
import { config } from './config';

export const client = createClient({ baseUrl: config.apiBase });
const loops = loopsApi(client);

/**
 * The loop list. No owner = the unscoped master request under the shared ['loops'] key
 * (every existing caller); an owner scopes the read server-side (Phase 5 seam).
 */
export const useLoops = (owner?: string) =>
  useQuery({
    queryKey: owner ? ['loops', { owner }] : ['loops'],
    queryFn: () => loops.list(owner),
    refetchInterval: config.pollMs,
  });
