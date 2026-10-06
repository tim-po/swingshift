import { useEffect, useState } from 'react';
import { useLoops } from '../api';

/** Quiet freshness line: "updated 12s ago" from the loops feed; "syncing…" only
 *  for a refresh the user asked for; "offline" when the feed is failing. */
export function SyncStatus({ manual }: { manual: boolean }) {
  const { dataUpdatedAt, error, isFetching } = useLoops();
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 5000);
    return () => clearInterval(t);
  }, []);

  let cls = 'tick';
  let text: string;
  if (manual && isFetching) {
    cls += ' live';
    text = 'syncing…';
  } else if (error) {
    cls += ' err';
    text = 'offline — retrying';
  } else if (!dataUpdatedAt) {
    cls += ' live';
    text = 'connecting…';
  } else {
    const s = Math.max(0, Math.round((Date.now() - dataUpdatedAt) / 1000));
    // Fresh data is the normal case — say nothing. Only speak up when it's old.
    if (s < 60) return <span className="tick" role="status" aria-label="Up to date" />;
    text = `updated ${Math.floor(s / 60)}m ago`;
    cls += ' stale';
  }
  return (
    <span className={cls} role="status" title={dataUpdatedAt ? `Loops feed last refreshed ${new Date(dataUpdatedAt).toLocaleTimeString()}` : undefined}>
      {text}
    </span>
  );
}
