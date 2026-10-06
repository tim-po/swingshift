import { memo } from 'react';
import { Link } from 'react-router-dom';
import { ago, issueActions, issueFromLabel, issueKindLabel, issueStatus, issueStatusBadge, type Issue } from '@loopyard/api';
import { Badge } from '../../components/ui';
import { MoreMenu } from './MoreMenu';

/** The issue's lifecycle status as a Badge — same word on the list and the detail page. */
export function StatusChip({ status }: { status?: string }) {
  const b = issueStatusBadge(status);
  return <Badge state={b.state}>{b.label}</Badge>;
}

interface Props {
  it: Issue;
  busy: boolean;
  onAction(it: Issue, status: string): void;
}

/** One calm row: title, one meta line, status/severity only when they matter.
 *  "Fix with a loop" + ⋯ triage appear on hover/focus (always on touch). */
export const IssueCard = memo(function IssueCard({ it, busy, onAction }: Props) {
  const title = it.title || it.loop || 'Untitled issue';
  const href = '/issues/' + encodeURIComponent(it.id);
  const status = issueStatus(it);
  const done = status === 'resolved' || status === 'dismissed';
  const actions = issueActions(it);
  const primary = actions.find((a) => a.primary);
  const from = issueFromLabel(it.source);
  const meta = [
    issueKindLabel(it.kind),
    from && `from ${from}`,
    it.project && it.project !== 'Unattributed' ? it.project : '',
    it.ts ? ago(it.ts) : '',
  ].filter(Boolean);

  return (
    <article className={'is-row' + (done ? ' done' : '')}>
      <div className="is-main">
        <div className="is-titleline">
          <Link className="is-title" to={href} title={title}>{title}</Link>
          {it.severity === 'high' && <Badge state="error">High priority</Badge>}
          {status !== 'open' && <StatusChip status={it.status} />}
        </div>
        <div className="is-meta" title={it.loop ? `About loop ${it.loop}` : undefined}>
          {meta.map((m, i) => <span key={i} className={i ? 'dotsep' : undefined}>{m}</span>)}
        </div>
      </div>
      <div className="is-acts">
        {primary && (
          <button type="button" className="btn sm" disabled={busy} onClick={() => onAction(it, primary.status)} title="Start a new loop seeded with this issue">
            {primary.label}
          </button>
        )}
        <MoreMenu
          label="More actions for this issue"
          disabled={busy}
          items={actions.filter((a) => !a.primary).map((a) => ({ label: a.label, onSelect: () => onAction(it, a.status) }))}
        />
      </div>
    </article>
  );
});
