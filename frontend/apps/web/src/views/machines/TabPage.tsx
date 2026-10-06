// How a view renders inside the Machines tabs without a second page title.
//
// The Machines wrapper (./index.tsx) owns the page: the "Machines" heading, its
// one-line sub and the tab strip. It wraps the active tab in
// <MachinesTabContext value={true}>. A tab view renders <MachinesTabPage> where it
// used to render <Page>:
//
//   • inside Machines → no <h1>, no second `.page` frame; the view's `sub`
//     (counts, filters) and `actions` (its primary button) become a slim toolbar
//     under the tabs.
//   • reached standalone (tests, a direct import) → exactly the old <Page>.
//
// Every Machines tab (All / Computers / Sessions) renders this instead of <Page>.
import { createContext, useContext, type ReactNode } from 'react';
import { Page } from '../../components/ui';
import './machines.css';

export const MachinesTabContext = createContext(false);

/** True when rendered as a tab inside the Machines page. */
export const useInMachinesTab = () => useContext(MachinesTabContext);

export function MachinesTabPage({ title, sub, actions, children }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  const embedded = useInMachinesTab();
  if (!embedded) return <Page title={title} sub={sub} actions={actions}>{children}</Page>;
  return (
    <div className="mc-panel">
      {(sub || actions) && (
        <div className="mc-toolbar">
          <div className="mc-toolbar-sub">{sub}</div>
          {actions && <div className="pageactions">{actions}</div>}
        </div>
      )}
      {children}
    </div>
  );
}
