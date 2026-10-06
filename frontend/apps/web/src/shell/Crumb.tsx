import { Link, useLocation } from 'react-router-dom';
import { VIEWS } from '../views';

/** Breadcrumb for the top bar: "Loops › redesign-build". The view name links back
 *  to its list, so a detail screen always has a way up. */
export function Crumb() {
  const { pathname } = useLocation();
  const [id, ...rest] = pathname.replace(/^\//, '').split('/').filter(Boolean);
  const view = VIEWS.find((v) => v.id === id);
  if (!view) return <span className="crumb" />;
  // /loops/:host/:name — the host is a card badge, not a crumb.
  // Machines tabs are the page's own tab bar, and a plan's title is its page heading — no raw-id crumb.
  if (id === 'machines' || id === 'plans')
    return <span className="crumb">{id === 'plans' && rest.length ? <Link to="/plans">{view.label}</Link> : <span className="crumb-cur">{view.label}</span>}</span>;
  const tail = (id === 'loops' && rest.length === 2 ? rest.slice(1) : rest).map(decodeURIComponent);
  return (
    <span className="crumb">
      {tail.length ? <Link to={'/' + view.id}>{view.label}</Link> : <span className="crumb-cur">{view.label}</span>}
      {tail.map((t, i) => (
        <span key={i}>
          <span className="crumb-sep" aria-hidden="true">›</span>
          <span className="crumb-cur mono" title={t}>{t}</span>
        </span>
      ))}
    </span>
  );
}
