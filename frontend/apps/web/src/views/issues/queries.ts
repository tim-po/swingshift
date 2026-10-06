import { useQuery } from '@tanstack/react-query';
import { issuesApi } from '@loopyard/api';
import { client } from '../../api';
import { config } from '../../config';

export const issues = issuesApi(client);
export const issueKeys = { list: ['issues'] as const, one: (id: string) => ['issues', 'one', id] as const };

export const useIssues = (poll = config.pollMs) => useQuery({ queryKey: issueKeys.list, queryFn: issues.list, refetchInterval: poll });
export const useIssue = (id: string | undefined) =>
  useQuery({ queryKey: issueKeys.one(id ?? ''), queryFn: () => issues.get(id!), enabled: !!id, retry: false });
