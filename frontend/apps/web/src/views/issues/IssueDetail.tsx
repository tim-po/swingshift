import { Link } from 'react-router-dom';
import { ago, issueFromLabel, issueKindLabel, loopHref, reportStatusLabel } from '@loopyard/api';
import { Badge, Btn, Empty, friendlyError, Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { StatusChip } from './IssueCard';
import { useIssue } from './queries';

export function IssueDetail({ id }: { id: string }) {
  const { data, error, isPending, refetch } = useIssue(id);
  const back = (
    <Link className="is-back" to="/issues">
      <Icon name="chevronLeft" size={14} /> Issues
    </Link>
  );
  if (isPending) return <>{back}<Skeleton block={80} lines={5} /></>;
  if (error || !data) {
    const fe = friendlyError(error, 'this issue');
    return <>{back}<Empty tone="err" title={fe.title}><p>{fe.detail}</p><Btn variant="primary" onClick={() => refetch()}>Try again</Btn></Empty></>;
  }
  const it = data.issue ?? { id };
  const atts = it.guardian_attempts ?? [];
  const from = issueFromLabel(it.source);
  const where = [
    it.failing_agent && `agent ${it.failing_agent}`,
    it.failing_phase && `phase ${it.failing_phase}`,
    it.failing_status && `last said “${reportStatusLabel(it.failing_status)}”`,
  ].filter(Boolean);
  return (
    <>
      {back}
      <header className="is-dhead">
        <h1 className="h1">{it.title || it.loop || 'Issue'}</h1>
        <div className="is-dmeta">
          <StatusChip status={it.status} />
          {it.severity === 'high' && <Badge state="error">High priority</Badge>}
          <span>{issueKindLabel(it.kind)}</span>
          {from && <span className="dotsep">from {from}</span>}
          {it.loop && (
            <span className="dotsep">
              loop <Link className="mono" to={loopHref(it.loop, it.origin)}>{it.loop}</Link>
            </span>
          )}
          {it.ts ? <span className="dotsep">{ago(it.ts)}</span> : null}
        </div>
      </header>
      {it.body && <p className="is-body">{it.body}</p>}
      {where.length > 0 && <p className="is-where">Where it stopped: {where.join(" · ")}</p>}
      {it.error && (
        <section className="section">
          <h2 className="section-h">Error</h2>
          <pre className="is-log err">{it.error}</pre>
        </section>
      )}
      {(atts.length > 0 || it.kind === 'crash') && (
        <section className="section">
          <h2 className="section-h" title="Swingshift's automatic recovery (the guardian) tries to get a stuck loop going again">
            Recovery attempts <span className="count">{atts.length}</span>
          </h2>
          {atts.length ? (
            <div className="rows">
              {atts.map((a, i) => (
                <div key={i} className="is-att">
                  <Badge state={a.status === 'give_up' ? 'error' : 'saved'}>{a.status === 'give_up' ? 'Gave up' : reportStatusLabel(a.status || 'tried')}</Badge>
                  <span className="mono is-atag">{a.agent || '—'}</span>
                  <span className="is-anote">{a.note}</span>
                  <span className="is-ats">{ago(a.ts)}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="is-hint">No automatic recovery was tried — the loop stopped straight away.</p>
          )}
        </section>
      )}
      <details className="is-more-ctx" open={it.kind === 'crash' || undefined}>
        <summary>Transcript and log</summary>
        <h2 className="section-h">Last turn transcript</h2>
        {data.transcript_exists ? (
          <pre className="is-log">{data.transcript_tail}</pre>
        ) : (
          <p className="is-hint" title={it.transcript_path || undefined}>No transcript was saved for this turn.</p>
        )}
        <h2 className="section-h">Log excerpt</h2>
        {it.log_slice?.trim() ? <pre className="is-log">{it.log_slice}</pre> : <p className="is-hint">No log excerpt was recorded.</p>}
        <p className="is-hint mono" title="Issue id">{it.id || id}{it.run ? ` · run ${String(it.run)}` : ''}</p>
      </details>
    </>
  );
}
