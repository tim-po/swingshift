// Shell-only nav metadata layered over the route table.
import type { IconName } from '../components/icons';

export interface NavEntry {
  id: string;
  label: string;
  icon: IconName;
  /** One plain sentence, shown on hover. No jargon. */
  tip: string;
  /** Core entries are always in the sidebar; the rest appear once relevant. */
  core?: boolean;
}

/** The sidebar, top to bottom. Everything else is reachable from a page. */
export const NAV: NavEntry[] = [
  { id: 'overview', label: 'Home', icon: 'home', core: true, tip: 'What needs you, what’s running, and what just finished.' },
  { id: 'loops', label: 'Loops', icon: 'loop', core: true, tip: 'Teams of AI agents working on a goal until it’s done.' },
  { id: 'plans', label: 'Plans', icon: 'plan', core: true, tip: 'Ideas and goals you want built. Turn any plan into a loop.' },
  { id: 'city', label: 'City', icon: 'city', core: true, tip: 'Your project as a city: every loop is a building, every agent a floor.' },
  { id: 'issues', label: 'Issues', icon: 'issue', tip: 'Problems found by your loops or by you. Point a loop at one to fix it.' },
  { id: 'machines', label: 'Machines', icon: 'machine', tip: 'The computers and live sessions your loops run on.' },
  { id: 'library', label: 'Library', icon: 'role', tip: 'Your agents and loops, and how well they’re doing — your rating beside the analyst’s score.' },
  { id: 'projects', label: 'Projects', icon: 'project', tip: 'The code repos your loops work in.' },
];

export const NAV_TIPS: Record<string, string> = {
  ...Object.fromEntries(NAV.map((n) => [n.id, n.tip])),
  newloop: 'Describe what you want done — a team assembles and gets to work.',
};

/** g-chord destinations. Old letters (h, c, o, s, d) keep working. */
export const KEY_NAV: Record<string, string> = {
  o: 'overview', l: 'loops', p: 'plans', y: 'city', h: 'plans', c: 'plans', i: 'issues',
  m: 'machines', s: 'machines', d: 'machines', a: 'library', r: 'library', j: 'projects',
};
