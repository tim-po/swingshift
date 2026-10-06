import { useProjectScope } from '../scope';
import { Icon } from '../components/icons';
import { useShowAll } from './disclosure';

/** The persistent project switcher — the app's primary global filter. */
export function ProjectSwitcher() {
  const { project, setProject, projects } = useProjectScope();
  const [showAll] = useShowAll();
  // One project (or none) → nothing to switch between; the filter appears with the second.
  if (projects.length < 2 && !project && !showAll) return null;
  return (
    <label className="sh-psw" title="Filter the whole app to one project">
      <Icon name="project" className="sh-psglyph" />
      <select value={project} onChange={(e) => setProject(e.target.value)} aria-label="Show one project, or all">
        <option value="">All projects</option>
        {projects.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name}{s.count ? ` (${s.count})` : ''}
          </option>
        ))}
      </select>
    </label>
  );
}
