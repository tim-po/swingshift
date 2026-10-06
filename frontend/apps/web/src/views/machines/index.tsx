// Machines — one home for where work runs. Three lenses on the same fleet:
//   All       — every connected machine + session in one roster (was Devices)
//   Computers — boxes you own that loops run on (was Origins)
//   Sessions  — live apps/terminals that dispatch and steer work (was Sessions)
//
// This wrapper owns the page title, sub and tabs. Tab views render
// <MachinesTabPage> (./TabPage.tsx) so they don't print a second title.
import { lazy, Suspense } from 'react';
import { NavLink, useParams } from 'react-router-dom';
import { Skeleton } from '../../components/ui';
import { MachinesTabContext } from './TabPage';
import './machines.css';

const Devices = lazy(() => import('../devices'));
const Origins = lazy(() => import('../origins'));
const Sessions = lazy(() => import('../sessions'));

export const MACHINE_TABS = [
  { id: 'all', label: 'All', View: Devices },
  { id: 'computers', label: 'Computers', View: Origins },
  { id: 'sessions', label: 'Sessions', View: Sessions },
] as const;

export default function MachinesView() {
  const { tab } = useParams();
  const cur = MACHINE_TABS.find((t) => t.id === tab) ?? MACHINE_TABS[0];
  return (
    <div className="page mc">
      <header className="pagehead mc-head">
        <div>
          <h1 className="h1">Machines</h1>
          <div className="sub">The computers your loops run on, and the apps connected to them.</div>
        </div>
      </header>
      <nav className="mc-tabs" aria-label="Machines views">
        {MACHINE_TABS.map((t) => (
          <NavLink key={t.id} to={`/machines/${t.id}`} className={() => 'mc-tab' + (t.id === cur.id ? ' on' : '')} aria-current={t.id === cur.id ? 'page' : undefined} end>
            {t.label}
          </NavLink>
        ))}
      </nav>
      <div className="mc-body">
        <MachinesTabContext value={true}>
          <Suspense fallback={<Skeleton lines={5} />}>
            <cur.View />
          </Suspense>
        </MachinesTabContext>
      </div>
    </div>
  );
}
