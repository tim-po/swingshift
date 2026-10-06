// The list — the default Plans view (Notion-database style). Rows grouped by
// status (Idea / Planned / Running / Done), each group collapsible, empty groups
// hidden. A row: title, project, notes, its loop's live state, when it changed.
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { activePlanLoop, ago, honestState, PLAN_COLUMNS, planLoops, planOutcome, turnsLabel, type PlanColumn, type WsObjective } from '@loopyard/api';
import { Badge } from '../../components/ui';
import { Icon } from '../../components/icons';
import type { useLoops } from '../../api';
import { planRoute } from './queries';

export type LoopList = NonNullable<ReturnType<typeof useLoops>['data']>['loops'];

export const notes = (n: number) => `${n} note${n === 1 ? '' : 's'}`;

function LoopState({ o, loops }: { o: WsObjective; loops: LoopList }) {
  const active = activePlanLoop(o, loops);
  if (active) {
    const turn = turnsLabel(active);
    return (
      <span className="pl-rowloop">
        <Badge state={honestState(active)} />
        {turn && <span className="muted">{turn}</span>}
      </span>
    );
  }
  const last = planLoops(o, loops).find((x) => x.loop)?.loop;
  if (!last) return null;
  return (
    <span className="pl-rowloop" title="Its most recent loop">
      <Badge state={honestState(last)} />
    </span>
  );
}

function PlanRow({ o, loops, showProject }: { o: WsObjective; loops: LoopList; showProject: boolean }) {
  const navigate = useNavigate();
  const updated = ago(o.updated);
  const meta = [showProject && o.project, notes(o.doc_count)].filter(Boolean).join(' · ');
  const outcome = planOutcome(o);
  return (
    <button type="button" className="row pl-row" onClick={() => navigate(planRoute(o.id))}>
      {outcome && (
        <span className={'pl-outcome' + (outcome.done ? ' done' : ' ended')} title={outcome.label} aria-label={outcome.label}>
          {outcome.glyph}
        </span>
      )}
      <span className="grow pl-rowtitle">{o.title}</span>
      <span className="pl-rowmeta">{meta}</span>
      <LoopState o={o} loops={loops} />
      <span className="end pl-rowwhen">{updated}</span>
    </button>
  );
}

export function PlanList({ cols, loops, showProject }: { cols: Record<PlanColumn, WsObjective[]>; loops: LoopList; showProject: boolean }) {
  const [closed, setClosed] = useState<Partial<Record<PlanColumn, boolean>>>({});
  return (
    <div className="pl-list">
      {PLAN_COLUMNS.filter((c) => cols[c.id].length > 0).map((c) => {
        const open = !closed[c.id];
        const id = 'pl-group-' + c.id;
        return (
          <section key={c.id} className="pl-group" aria-label={c.label}>
            <h2 className="pl-grouphead">
              <button type="button" className="pl-grouptoggle" aria-expanded={open} aria-controls={id}
                onClick={() => setClosed((s) => ({ ...s, [c.id]: open }))}>
                <Icon name={open ? 'chevronDown' : 'chevronRight'} size={14} />
                {c.label} <span className="count">{cols[c.id].length}</span>
              </button>
            </h2>
            {open && (
              <div className="rows" id={id}>
                {cols[c.id].map((o) => <PlanRow key={o.id} o={o} loops={loops} showProject={showProject} />)}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
