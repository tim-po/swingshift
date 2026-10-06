import { useCallback, useEffect, useMemo, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { buildOverview, filterLoops, inProjectScope, isOpenWork, isSessionOrigin, issueInScope, partitionObjectives, projectsOf } from '@loopyard/api';
import { useObjectives } from '../views/workspace/queries';
import { useIssues } from '../views/issues/queries';
import { useSessions } from '../views/sessions/api';
import { useLoops } from '../api';
import { config } from '../config';
import { useOrigins, useProjects, useProjectScope } from '../scope';
import { Icon } from '../components/icons';
import { ShiftSign } from '../components/flipdot';
import { Brand } from './Brand';
import { FavShelf, useRegistryAgents } from './FavShelf';
import { FleetPill } from './FleetPill';
import { HelpMenu } from './HelpMenu';
import { NAV, NAV_TIPS, type NavEntry } from './navMeta';
import { ProjectSwitcher } from './ProjectSwitcher';
import { Crumb } from './Crumb';
import { SyncStatus } from './SyncStatus';
import { ShortcutsHelp } from './ShortcutsHelp';
import { useShortcuts } from './useShortcuts';
import { VersionFoot, VersionSkew } from './Version';
import { useSeen, useShowAll } from './disclosure';
import './shell.css';

/** The top-bar flip-dot sign, fed by the same model as Home. Home has the full board, so it hides there. */
function TopSign() {
  const { project } = useProjectScope();
  const loops = useLoops().data?.loops;
  const { pathname } = useLocation();
  const m = useMemo(
    () => buildOverview({ loops: (loops ?? []) as Parameters<typeof buildOverview>[0]['loops'], docs: [], issues: [], origins: [], scope: project }),
    [loops, project],
  );
  if (!loops || pathname === '/' || pathname.startsWith('/overview')) return null;
  return <ShiftSign m={m} />;
}

interface NavState {
  counts: Record<string, number>;
  /** Has something in it → earns a place in the sidebar. */
  relevant: Record<string, boolean>;
  loaded: boolean;
}

/** Nav counts — each derived from the SAME query + filter its view renders, so a
 *  badge can never disagree with the page behind it. */
function useNavState(): NavState {
  const { project } = useProjectScope();
  const loops = useLoops().data?.loops;
  const objectives = useObjectives(config.navPollMs).data?.objectives;
  const issues = useIssues(config.navPollMs).data?.issues;
  const sessions = useSessions(config.navPollMs).data?.sessions;
  const origins = useOrigins().data?.origins;
  const projects = useProjects().data;
  const registry = useRegistryAgents().data;
  return useMemo(() => {
    const counts: Record<string, number> = {};
    if (loops) counts.loops = filterLoops(loops, {}).filter((d) => inProjectScope(d, project)).length;
    if (objectives) counts.plans = partitionObjectives(objectives).objectives.filter((o) => inProjectScope(o, project)).length;
    if (issues && loops) counts.issues = issues.filter((x) => isOpenWork(x) && issueInScope(x, project, loops)).length;
    const boxes = origins ? origins.filter((o) => !isSessionOrigin(o)).length : 0;
    const machines = boxes + (sessions?.length ?? 0);
    if (origins || sessions) counts.machines = machines;
    if (registry) counts.library = registry.length;
    if (projects) counts.projects = projectsOf(projects).length;
    return {
      counts,
      relevant: {
        issues: (counts.issues ?? 0) > 0,
        // One machine (this box) is the default — the page earns its place at two.
        machines: machines > 1,
        library: (registry?.length ?? 0) > 0,
        projects: (counts.projects ?? 0) > 0,
      },
      loaded: !!(loops && issues && origins && projects),
    };
  }, [loops, objectives, issues, sessions, origins, projects, registry, project]);
}

export function Layout() {
  const [navOpen, setNavOpen] = useState(false);
  const [help, setHelp] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const qc = useQueryClient();
  const [manual, setManual] = useState(false);
  const [showAll] = useShowAll();
  const { seen, markSeen, initialized, init } = useSeen();
  const { counts, relevant, loaded } = useNavState();
  const close = useCallback(() => setNavOpen(false), []);
  const refresh = useCallback(() => {
    setManual(true);
    void qc.invalidateQueries().finally(() => setManual(false));
  }, [qc]);

  useShortcuts({ go: (id) => navigate('/' + id), refresh, help: () => setHelp(true) });

  const here = pathname.split('/')[1] ?? '';
  const shown = (n: NavEntry) => n.core || showAll || relevant[n.id] || here === n.id;
  const primary = NAV.filter(shown);
  const tucked = NAV.filter((n) => !shown(n));

  // First load ever: whatever is already visible counts as seen (no dot storm).
  useEffect(() => {
    if (loaded && !initialized) init(primary.map((n) => n.id));
  }, [loaded, initialized]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (here) markSeen(here);
  }, [here, markSeen]);

  const navItem = (n: NavEntry, quiet = false) => {
    const c = counts[n.id];
    const fresh = initialized && !quiet && !n.core && !seen(n.id);
    return (
      <NavLink
        key={n.id}
        to={'/' + n.id}
        className={({ isActive }) => 'navitem' + (isActive ? ' on' : '') + (quiet ? ' quiet' : '')}
        onClick={close}
        data-tip={n.tip}
        aria-label={`${n.label}. ${n.tip}${c ? ', ' + c : ''}`}
      >
        <Icon name={n.icon} className="nico" />
        <span className="sh-nlabel">{n.label}</span>
        {fresh ? <span className="newdot" title="New — you haven't opened this yet" /> : !!c && !quiet && <span className="sh-ncnt">{c}</span>}
      </NavLink>
    );
  };

  return (
    // The city is a full-bleed map: the sidebar folds to a strip of icons there (desktop).
    <div className={'app' + (navOpen ? ' nav-open' : '') + (here === 'city' ? ' rail' : '')}>
      <div className="navscrim" onClick={close} aria-hidden="true" />
      <nav className="sidebar" id="sidebar" aria-label="Main">
        <div className="sh-top">
          <Brand />
        </div>
        <ProjectSwitcher />
        <button className="sh-newloop" onClick={() => { close(); navigate('/newloop'); }} title={NAV_TIPS.newloop}>
          <Icon name="plus" />
          New loop
        </button>
        <div className="nav">{primary.map((n) => navItem(n))}</div>
        {tucked.length > 0 && (
          <div className="sh-more">
            <button className="sh-morebtn" onClick={() => setMoreOpen((o) => !o)} aria-expanded={moreOpen}>
              <Icon name={moreOpen ? 'chevronDown' : 'chevronRight'} size={14} />
              More
            </button>
            {moreOpen && <div className="nav">{tucked.map((n) => navItem(n, true))}</div>}
          </div>
        )}
        <FavShelf onNavigate={close} />
        <div className="sh-foot">
          <HelpMenu onShortcuts={() => setHelp(true)} />
          <VersionFoot ui={config.version} />
        </div>
      </nav>
      <div className="main">
        <header className="topbar">
          <button className="navtoggle sh-iconbtn" onClick={() => setNavOpen((o) => !o)} aria-label="Open navigation" aria-expanded={navOpen} aria-controls="sidebar">
            <Icon name="menu" />
          </button>
          <Crumb />
          <span className="spacer" />
          <TopSign />
          <SyncStatus manual={manual} />
          <FleetPill />
          <button className="sh-iconbtn" onClick={refresh} title="Refresh now (r)" aria-label="Refresh data now">
            <Icon name="refresh" />
          </button>
        </header>
        <VersionSkew />
        <main className="viewport">
          <Outlet />
        </main>
      </div>
      {help && <ShortcutsHelp onClose={() => setHelp(false)} />}
    </div>
  );
}
