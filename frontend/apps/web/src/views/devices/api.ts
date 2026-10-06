import { useQuery } from '@tanstack/react-query';
import { devicesApi } from '@loopyard/api';
import { client } from '../../api';
import { config } from '../../config';

const api = devicesApi(client);

/** The unified device roster. Optional owner scopes the read (Phase 5 seam). */
export const useDevices = (owner?: string, poll = config.pollMs) =>
  useQuery({
    queryKey: ['devices', owner ?? null],
    queryFn: () => api.list(owner),
    refetchInterval: poll,
  });
