// The persistent project switcher's state — the app's primary global filter.
// '' = "All projects" (fleet meta-view); PROJECT_UNATTR = loops bound to no project.
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import { projectScopes, projectsOf, type ProjectScope } from '@loopyard/api';
import { useLoops } from '../api';
import { platform } from '../platform';
import { useProjects } from './queries';

const KEY = 'lyActiveProject';

interface ScopeValue {
  /** Active scope id: '' = all projects. */
  project: string;
  setProject(id: string): void;
  /** Switchable scopes with live loop counts. */
  projects: ProjectScope[];
  /** Display name of the active scope ('' for all). */
  projectName: string;
}

const Ctx = createContext<ScopeValue | null>(null);

export function ProjectScopeProvider({ children }: { children: ReactNode }) {
  const [stored, setStored] = useState(() => platform.storage.get(KEY) || '');
  const loops = useLoops();
  const cat = useProjects();
  const scopes = useMemo(() => projectScopes(projectsOf(cat.data), loops.data?.loops ?? []), [cat.data, loops.data]);

  // A scope that no longer exists (project deleted, last loop gone) falls back to
  // the fleet view — but only once both feeds have answered, never on a cold load.
  const loaded = !!loops.data && !!cat.data;
  const project = stored && loaded && !scopes.some((s) => s.id === stored) ? '' : stored;

  const setProject = useCallback((id: string) => {
    setStored(id);
    platform.storage.set(KEY, id);
  }, []);

  const value = useMemo<ScopeValue>(
    () => ({ project, setProject, projects: scopes, projectName: scopes.find((s) => s.id === project)?.name ?? project }),
    [project, setProject, scopes],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useProjectScope(): ScopeValue {
  const v = useContext(Ctx);
  if (!v) throw new Error('useProjectScope must be used inside <ProjectScopeProvider>');
  return v;
}
