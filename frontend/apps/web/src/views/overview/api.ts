import { useQuery } from '@tanstack/react-query';
import { onboardingApi, overviewApi } from '@loopyard/api';
import { client } from '../../api';
import { config } from '../../config';

const api = overviewApi(client);
const onboarding = onboardingApi(client);
const opts = { refetchInterval: config.pollMs };

// Own query keys: other views may cache these endpoints under richer shapes.
export const useOverviewDocs = () => useQuery({ queryKey: ['overview', 'docs'], queryFn: api.docs, ...opts });
export const useOverviewIssues = () => useQuery({ queryKey: ['overview', 'issues'], queryFn: api.issues, ...opts });
export const useOverviewOrigins = () => useQuery({ queryKey: ['overview', 'origins'], queryFn: api.origins, ...opts });
/** The getting-started checklist (engine-derived ticks). Off once hidden. */
export const useOnboarding = (enabled = true) =>
  useQuery({ queryKey: ['overview', 'onboarding'], queryFn: onboarding.get, enabled, refetchInterval: config.pollMs * 3, retry: 1 });
