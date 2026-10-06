// Shared queries for the scope + shell chrome (and the Origins/Projects views).
import { useQuery } from '@tanstack/react-query';
import { fleetApi, originsApi, projectsApi, PROJECT_UNATTR } from '@loopyard/api';
import { client } from '../api';
import { config } from '../config';

export const projects = projectsApi(client);
export const origins = originsApi(client);
const fleet = fleetApi(client);

export const useProjects = () =>
  useQuery({ queryKey: ['projects'], queryFn: projects.list, refetchInterval: config.pollMs * 6 });

export const useOrigins = () =>
  useQuery({ queryKey: ['origins'], queryFn: origins.list, refetchInterval: config.pollMs * 6 });

export const useFleet = () =>
  useQuery({ queryKey: ['fleet'], queryFn: fleet.summary, refetchInterval: config.pollMs * 2 });

/** Backend nav counts for the scope. PROJECT_UNATTR has no slug the backend can key on → disabled. */
export const useScopedCounts = (project: string) =>
  useQuery({
    queryKey: ['counts', project],
    queryFn: () => projects.counts(project),
    enabled: project !== PROJECT_UNATTR,
    refetchInterval: config.pollMs * 2,
  });

export const useDispositions = (project: string) =>
  useQuery({
    queryKey: ['dispositions', project],
    queryFn: () => projects.dispositions(project),
    enabled: project !== PROJECT_UNATTR,
    refetchInterval: config.pollMs * 6,
  });
