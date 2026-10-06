// /plans — the plans in the active project scope. A grouped list by default (Idea →
// Planned → Running → Done); the board is one click away (List | Board, remembered).
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { boardColumns, candidateCount, plansInScope, PROJECT_UNATTR, searchPlans, WS_INBOX_ID } from '@loopyard/api';
import { Btn, Empty, QueryState, Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { Intro } from '../../components/Intro';
import { useLoops } from '../../api';
import { platform } from '../../platform';
import { useProjectScope } from '../../scope';
import { useShowAll } from '../../shell/disclosure';
import { Board } from './Board';
import { NewPlanDialog } from './NewPlanDialog';
import { PlanList } from './PlanList';
import { planRoute, useInboxCandidates, useObjectives } from './queries';
import { RunLoopDialog } from './RunLoopDialog';
import { FindSuggestionsDialog, HelperRuns } from './Suggestions';

type Flash = (t: string, err?: boolean) => void;
type Layout = 'list' | 'board';

export const PLANS_VIEW_KEY = 'lyPlansView';
/** The search box earns its place once there are this many plans in view. */
export const PLAN_SEARCH_AT = 8;

function readLayout(): Layout {
  try {
    return platform.storage.get(PLANS_VIEW_KEY) === 'board' ? 'board' : 'list';
  } catch {
    return 'list';
  }
}

function LayoutToggle({ value, onChange }: { value: Layout; onChange(v: Layout): void }) {
  return (
    <div className="pl-layout" role="group" aria-label="Show plans as">
      {(['list', 'board'] as const).map((v) => (
        <button key={v} type="button" className={'pl-layoutbtn' + (value === v ? ' on' : '')} aria-pressed={value === v} onClick={() => onChange(v)}>
          {v === 'list' ? 'List' : 'Board'}
        </button>
      ))}
    </div>
  );
}

export function PlansHome({ flash }: { flash: Flash }) {
  const navigate = useNavigate();
  const objectives = useObjectives();
  const loops = useLoops().data?.loops ?? [];
  const candidates = candidateCount(useInboxCandidates().data?.suggestions);
  const { project, projectName } = useProjectScope();
  const [showAll] = useShowAll();
  const [layout, setLayoutState] = useState<Layout>(readLayout);
  const [query, setQuery] = useState('');
  const [newOpen, setNewOpen] = useState(false);
  const [findOpen, setFindOpen] = useState(false);
  const [runOid, setRunOid] = useState<string | null>(null);

  const setLayout = (v: Layout) => {
    setLayoutState(v);
    try {
      platform.storage.set(PLANS_VIEW_KEY, v);
    } catch {
      /* storage unavailable — the choice lasts this visit */
    }
  };

  const list = objectives.data?.objectives;
  const everything = plansInScope(list, '').length;
  const scoped = useMemo(() => plansInScope(list, project), [list, project]);
  const shown = useMemo(() => searchPlans(scoped, query), [scoped, query]);
  const cols = boardColumns(shown, loops);
  const total = scoped.length;
  const failed = !!objectives.error && !list;
  const scopeName = project === PROJECT_UNATTR ? 'no project' : projectName;
  const showProject = !project; // inside one project, the project goes without saying

  const newBtn = (
    <Btn variant="primary" onClick={() => setNewOpen(true)}><Icon name="plus" /> New plan</Btn>
  );

  let body: React.ReactNode;
  if (objectives.isPending) body = <Skeleton lines={4} block={120} />;
  else if (failed) body = <QueryState isPending={false} error={objectives.error} what="plans" onRetry={() => objectives.refetch()} />;
  else if (!everything)
    body = (
      <Empty title="No plans yet">
        <p>Turn an idea into a plan, then run it as a loop.</p>
        {newBtn}
      </Empty>
    );
  else if (!total)
    body = (
      <Empty title={`No plans in ${scopeName}`}>
        <p>Start one here, or switch to All projects to see the other {everything}.</p>
        {newBtn}
      </Empty>
    );
  else if (!shown.length)
    body = (
      <Empty title="No plans match">
        <Btn onClick={() => setQuery('')}>Clear search</Btn>
      </Empty>
    );
  else if (layout === 'board') body = <Board cols={cols} loops={loops} onRun={setRunOid} showProject={showProject} />;
  else body = <PlanList cols={cols} loops={loops} showProject={showProject} />;

  return (
    <div className="page pl-page">
      <header className="pagehead">
        <div>
          <h1 className="h1">Plans</h1>
          {total > 0 && (
            <div className="sub">
              {query ? `${shown.length} of ${total}` : total} plan{total === 1 ? '' : 's'}
              {project ? ` in ${scopeName}` : ''}
            </div>
          )}
        </div>
        {total > 0 && <div className="pageactions">{newBtn}</div>}
      </header>
      {total > 0 && (
        <Intro id="plans">
          A <b>plan</b> is a short doc about something you want done. When it’s ready, <b>run it as a loop</b> and watch it move to Running.
        </Intro>
      )}
      {total > 0 && (
        <div className="pl-toolbar">
          {total >= PLAN_SEARCH_AT ? (
            <label className="searchbox pl-search">
              <span className="sglyph" aria-hidden="true"><Icon name="search" size={14} /></span>
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find a plan" spellCheck={false} aria-label="Filter plans" />
              {query && (
                <button type="button" className="sclear" onClick={() => setQuery('')} aria-label="Clear search"><Icon name="x" size={14} /></button>
              )}
            </label>
          ) : (
            <span className="pl-spacer" />
          )}
          <LayoutToggle value={layout} onChange={setLayout} />
        </div>
      )}
      {body}

      {(candidates > 0 || showAll) && (
        <section className="section pl-suggested" aria-label="Suggestions">
          <h2 className="section-h">Suggestions {candidates > 0 && <span className="count">{candidates}</span>}</h2>
          <HelperRuns navigate={navigate} />
          <div className="pl-suggestedrow">
            {candidates > 0 && (
              <button type="button" className="btn" onClick={() => navigate(planRoute(WS_INBOX_ID))}>
                <Icon name="sparkle" /> {candidates} suggested plan{candidates === 1 ? '' : 's'} to review
              </button>
            )}
            {showAll && (
              <button type="button" className="btn quiet" onClick={() => setFindOpen(true)} title="Sweep a machine's files for candidate plans">
                Find plan ideas…
              </button>
            )}
          </div>
        </section>
      )}

      {newOpen && <NewPlanDialog onClose={() => setNewOpen(false)} flash={flash} />}
      {findOpen && <FindSuggestionsDialog onClose={() => setFindOpen(false)} flash={flash} />}
      {runOid && <RunLoopDialog oid={runOid} onClose={() => setRunOid(null)} flash={flash} />}
    </div>
  );
}
