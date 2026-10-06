// The board — the secondary Plans view (List | Board). Four columns (Idea →
// Planned → Running → Done) mapped from each plan's status; a plan whose loop is
// working shows under Running with its live turn. Each card: title, one quiet
// meta line, one status-appropriate action.
import { useNavigate } from 'react-router-dom';
import { activePlanLoop, ago, honestState, PLAN_COLUMNS, planLoops, turnsLabel, type PlanColumn, type WsObjective } from '@loopyard/api';
import { Badge, Btn } from '../../components/ui';
import { Icon } from '../../components/icons';
import { notes, type LoopList } from './PlanList';
import { planRoute } from './queries';

function PlanCard({ o, col, loops, onRun, showProject }: { o: WsObjective; col: PlanColumn; loops: LoopList; onRun(oid: string): void; showProject: boolean }) {
  const navigate = useNavigate();
  const active = activePlanLoop(o, loops);
  const last = planLoops(o, loops).find((x) => x.loop)?.loop ?? null;
  const updated = ago(o.updated);
  const meta = [showProject && o.project, notes(o.doc_count), updated && `updated ${updated}`].filter(Boolean).join(' · ');

  let action: React.ReactNode = null;
  if (active) {
    action = (
      <span className="pl-cardstate">
        <Badge state={honestState(active)} />
        {turnsLabel(active) && <span className="muted">{turnsLabel(active)}</span>}
      </span>
    );
  } else if (col === 'planned') {
    action = (
      <Btn className="btn sm" onClick={() => onRun(o.id)} aria-label={`Run “${o.title}” as a loop`}>
        <Icon name="play" /> Run loop
      </Btn>
    );
  } else if (col === 'done') {
    action = (
      <span className="pl-cardstate ok">
        <Icon name="check" size={14} /> {last ? (honestState(last) === 'error' ? 'loop needs a look' : 'loop finished') : 'done'}
      </span>
    );
  } else if (last) {
    action = <span className="pl-cardstate muted">Last loop: <Badge state={honestState(last)} /></span>;
  }

  return (
    <li className="pl-card">
      <button type="button" className="pl-cardmain" onClick={() => navigate(planRoute(o.id))}>
        <span className="pl-cardtitle">{o.title}</span>
        <span className="pl-cardmeta">{meta}</span>
      </button>
      {action && <div className="pl-cardaction">{action}</div>}
    </li>
  );
}

export function Board({ cols, loops, onRun, showProject }: {
  cols: Record<PlanColumn, WsObjective[]>;
  loops: LoopList;
  onRun(oid: string): void;
  showProject: boolean;
}) {
  return (
    <div className="pl-board">
      {PLAN_COLUMNS.map((c) => (
        <section key={c.id} className={'pl-col pl-col-' + c.id} aria-label={c.label}>
          <h2 className="pl-colhead">{c.label} <span className="count">{cols[c.id].length}</span></h2>
          {cols[c.id].length ? (
            <ul className="pl-cards">
              {cols[c.id].map((o) => <PlanCard key={o.id} o={o} col={c.id} loops={loops} onRun={onRun} showProject={showProject} />)}
            </ul>
          ) : (
            <p className="pl-colempty">Nothing here</p>
          )}
        </section>
      ))}
    </div>
  );
}
