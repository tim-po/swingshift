import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { fleetDot, fleetItemMeta } from '@loopyard/api';
import { useFleet } from '../scope';

const MAX_DOTS = 8;

/** Compute felt ambiently: ◉ Fleet ●●○ N · M reachable, click for detail. */
export function FleetPill() {
  const { data: f, error } = useFleet();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const items = f?.items ?? [];
  const drop = (f?.dropCount ?? 0) > 0;
  const online = items.filter((i) => i.reachable).length;
  const label = items.length ? `${online} of ${items.length} machine${items.length === 1 ? '' : 's'} online` : 'No machines yet';

  return (
    <div className="sh-fleet" ref={ref}>
      <button
        className={'sh-fleetpill' + (drop ? ' drop' : '')}
        onClick={() => setOpen((o) => !o)}
        title={`${label} — click for details`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`Machines: ${label}`}
      >
        <span className="sh-fpdots" aria-hidden="true">
          {items.slice(0, MAX_DOTS).map((i) => (
            <span key={i.id} className={'sh-fd ' + fleetDot(i)} />
          ))}
        </span>
        <span className="sh-fptxt">{label}</span>
        {items.length > 0 && <span className="sh-fpmini" aria-hidden="true">{online}/{items.length}</span>}
      </button>
      {open && (
        <div className="sh-pop sh-fleetpop" role="dialog" aria-label="Machines">
          {!f ? (
            <div className="sh-fprow stone">{error ? "Couldn't reach your machines — trying again" : 'No machines connected yet'}</div>
          ) : (
            <>
              <div className="sh-fphead">{label}</div>
              {drop && (
                <div className="sh-fpdrop">
                  {f.dropCount} machine{f.dropCount === 1 ? '' : 's'} dropped while running a loop
                  {f.dropped?.length ? ': ' + f.dropped.map((d) => d.name || d.id).join(', ') : ''}
                </div>
              )}
              {items.length ? (
                items.map((i) => (
                  <div key={i.id} className="sh-fprow">
                    <span className={'sh-fd ' + fleetDot(i)} />
                    <span className="sh-fpn" title={i.name || i.id}>{i.name || i.id}</span>
                    <span className="sh-fpm">{fleetItemMeta(i)}</span>
                  </div>
                ))
              ) : (
                <div className="sh-fprow stone">No machines connected yet</div>
              )}
            </>
          )}
          <div className="sh-fpfoot">
            <Link to="/machines" onClick={() => setOpen(false)}>Manage machines →</Link>
          </div>
        </div>
      )}
    </div>
  );
}
