import { memo } from 'react';
import { loopBadgeState, loopMetaParts, loopOwner, reportStatusLabel, type LoopSummary } from '@loopyard/api';
import { Badge } from '../../components/ui';

interface Props {
  loop: LoopSummary;
  selected: boolean;
  onPick(d: LoopSummary): void;
  /** The owner's current disposition verb for this loop (good/ok/bad), if rated. */
  disp?: string;
  /** Show the owner — only when the list spans more than one owner. */
  showOwner?: boolean;
  /** Show the project in the meta line (off when the list is already grouped by project). */
  showProject?: boolean;
}

const DISP: Record<string, string> = { good: 'rated good', ok: 'rated ok', bad: 'rated bad' };

/** One calm list row: name + status, one human meta line, the last report. */
export const LoopCard = memo(function LoopCard({ loop: d, selected, onPick, disp, showOwner, showProject }: Props) {
  const meta = loopMetaParts(d, { project: showProject });
  if (d.host !== 'local') meta.unshift(`on ${d.host}`);
  const lr = d.last_report;
  return (
    <button className={'lcard' + (selected ? ' sel' : '') + (d.archived ? ' arch' : '')} onClick={() => onPick(d)} aria-current={selected || undefined}>
      <span className="lc-top">
        <span className="lname" title={d.name}>{d.name}</span>
        <Badge state={loopBadgeState(d)} />
      </span>
      <span className="lc-meta">
        {meta.join(' · ')}
        {showOwner && <span className="ownerflag" title={`Owned by ${loopOwner(d)}`}>{loopOwner(d)}</span>}
        {disp && <span className={`dispflag ${disp}`} title={`You rated this loop ${disp}`}>{DISP[disp] ?? disp}</span>}
      </span>
      {lr && (
        <span className="lc-last" title={lr.note || undefined}>
          <span className="lc-agent">{lr.agent}</span> {reportStatusLabel(lr.status)}
        </span>
      )}
    </button>
  );
});
