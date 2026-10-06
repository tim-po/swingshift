// Home — a calm start page. Leads with what needs you, then what's running,
// then what just finished; the counts sit quietly at the bottom. A getting-
// started checklist (engine-derived, see Checklist.tsx) is the whole page on
// first run and a compact card at the top afterwards, until done or hidden.
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  CHECKLIST_HIDDEN_KEY, ago, buildOverview, checklistModel, greeting, honestState, loopProgress, reportStatusLabel, scopeName, stateLabel,
  type Checklist, type OverviewIssue, type OverviewLoop,
} from '@loopyard/api';
import { Badge, Page, QueryState, Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { Intro } from '../../components/Intro';
import { ShiftBoard } from '../../components/flipdot';
import { useLoops } from '../../api';
import { platform } from '../../platform';
import { useProjectScope } from '../../scope';
import { useOnboarding, useOverviewDocs, useOverviewIssues, useOverviewOrigins } from './api';
import { ChecklistCard, ChecklistHero } from './Checklist';
import './overview.css';

const loopHref = (d: OverviewLoop) => `/loops/${encodeURIComponent(d.host || 'local')}/${encodeURIComponent(d.name)}`;
const pl = (n: number, w: string) => `${n} ${w}${n === 1 ? '' : 's'}`;
const sentence = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1).toLowerCase() : s);

const NEEDS_CAP = 5;
const ISSUE_CAP = 3;
const RUNNING_CAP = 6;

function NewLoopBtn({ lg }: { lg?: boolean }) {
  return (
    <Link className={'btn primary' + (lg ? ' lg' : '')} to="/newloop">
      <Icon name="plus" /> New loop
    </Link>
  );
}

function Stat({ label, n, sub, to, err, pending }: { label: string; n: number; sub?: string; to: string; err?: boolean; pending?: boolean }) {
  return (
    <Link className="ov-stat" to={to} title={err ? `Couldn't load ${label.toLowerCase()} — open to try again` : `Open ${label}`}>
      <span className="ov-stat-n">{pending ? '…' : err ? '—' : n}</span>
      <span className="ov-stat-l">{label}</span>
      {sub && !err && !pending && <span className="ov-stat-sub">{sub}</span>}
    </Link>
  );
}

const WELCOME_SUB = 'Describe what you want done, and a small team of AI agents gets it done while you watch.';

// First run (no loops yet): the getting-started checklist is the page. Without
// it (still loading, hidden, or an older engine) one plain next step instead.
function Welcome({ m, pending, onHide }: { m: Checklist | null; pending: boolean; onHide: () => void }) {
  return (
    <Page title="Welcome to Swingshift" sub={WELCOME_SUB}>
      {m ? (
        <ChecklistHero m={m} onHide={onHide} />
      ) : pending ? (
        <Skeleton lines={4} />
      ) : (
        <div className="ov-first">
          <NewLoopBtn lg />
        </div>
      )}
    </Page>
  );
}

function NeedsRow({ d, state, reason }: { d: OverviewLoop; state: string; reason: string }) {
  return (
    <Link className="row ov-row" to={loopHref(d)}>
      <Badge state={state} />
      <span className="grow">
        <span className="ov-name">{d.name}</span>
        <span className="ov-line">{d.question || reason}</span>
      </span>
      <span className="end">{ago(d.updated || d.started)}</span>
    </Link>
  );
}

function IssueRow({ x }: { x: OverviewIssue }) {
  return (
    <Link className="row ov-row" to={`/issues/${encodeURIComponent(x.id)}`}>
      <span className="ov-ico" aria-hidden="true"><Icon name="issue" size={14} /></span>
      <span className="grow">
        <span className="ov-name">{x.title || 'Untitled issue'}</span>
        <span className="ov-line">Open issue{x.loop ? ` · from ${x.loop}` : ''}</span>
      </span>
    </Link>
  );
}

function RunningRow({ d }: { d: OverviewLoop }) {
  const p = loopProgress(d);
  const rep = d.last_report;
  const line = rep ? `${rep.agent}: ${reportStatusLabel(rep.status)}` : d.goal || stateLabel(d.state);
  return (
    <Link className="row ov-row" to={loopHref(d)}>
      <Badge state={honestState(d)} />
      <span className="grow">
        <span className="ov-name">{d.name}</span>
        <span className="ov-line" title={rep?.note || d.goal || undefined}>{line}</span>
      </span>
      {p !== null && (
        <span className="ov-prog" title={`Turn ${d.turns_used} of ${d.turnLimit}`}>
          <span className="ov-bar"><span style={{ width: `${Math.round(p * 100)}%` }} /></span>
          <span className="ov-prog-l">{d.turns_used}/{d.turnLimit}</span>
        </span>
      )}
      <span className="end">{ago(d.updated || d.started)}</span>
    </Link>
  );
}

function ResultRow({ d }: { d: OverviewLoop }) {
  const r = d.result ?? {};
  const green = !!r.green;
  return (
    <Link className="row ov-row" to={loopHref(d)}>
      <Badge state={green ? 'finished' : 'saved'}>{sentence(r.verdict || (green ? 'Done' : 'Result'))}</Badge>
      <span className="grow">
        <span className="ov-name">{d.name}</span>
        {r.resolution && <span className="ov-line" title={r.resolution}>{r.resolution}</span>}
      </span>
      <span className="end">{ago(d.updated || d.started)}</span>
    </Link>
  );
}

export default function OverviewView() {
  const { project: scope } = useProjectScope();
  const loops = useLoops();
  const docs = useOverviewDocs();
  const issues = useOverviewIssues();
  const origins = useOverviewOrigins();
  const [hidden, setHidden] = useState(() => platform.storage.get(CHECKLIST_HIDDEN_KEY) === '1');
  const onboarding = useOnboarding(!hidden);
  const checklist = hidden ? null : checklistModel(onboarding.data);
  const hide = () => {
    platform.storage.set(CHECKLIST_HIDDEN_KEY, '1');
    setHidden(true);
  };
  // Mirror the Loops list's archived toggle so the counts equal what it shows.
  const showArchived = platform.storage.get('loops.showArchived') === '1';

  const allLoops = (loops.data?.loops ?? []) as OverviewLoop[];
  const m = useMemo(
    () =>
      buildOverview({
        loops: allLoops,
        docs: docs.data?.docs ?? [],
        issues: issues.data?.issues ?? [],
        origins: origins.data?.origins ?? [],
        scope,
        showArchived,
      }),
    [allLoops, docs.data, issues.data, origins.data, scope, showArchived],
  );
  const name = scopeName(scope, allLoops);

  if (loops.isPending || loops.error)
    return (
      <Page title={greeting(new Date().getHours())}>
        <QueryState isPending={loops.isPending} error={loops.error} what="loops" onRetry={() => loops.refetch()} />
      </Page>
    );

  // First run: nothing has ever been started — onboard instead of showing zeros.
  if (allLoops.length === 0) return <Welcome m={checklist} pending={!hidden && onboarding.isPending} onHide={hide} />;

  const needs = m.attention.slice(0, NEEDS_CAP);
  // A crash issue about a loop already listed above would say the same thing twice.
  const needLoops = new Set(m.attention.map((a) => a.loop.name));
  const issuesLeft = m.allOpenIssues.filter((x) => !x.loop || !needLoops.has(x.loop));
  const issuesShown = issuesLeft.slice(0, ISSUE_CAP);
  const moreNeeds = m.attention.length - needs.length;
  const moreIssues = issuesLeft.length - issuesShown.length;
  const running = m.running.filter((d) => !m.attention.some((a) => a.loop === d));
  const quiet = !needs.length && !issuesShown.length && !running.length;

  // No header action: "New loop" already lives at the top of the sidebar.
  return (
    <Page
      title={greeting(new Date().getHours())}
      sub={scope ? <>Here's what's happening in <b className="ov-scope">{name}</b>.</> : "Here's what's happening across your projects."}
    >
      {checklist?.open ? (
        <ChecklistCard m={checklist} onHide={hide} />
      ) : (
        <Intro id="overview">
          Home shows what needs you, what's running and what just finished. Use the project switcher to focus on one project.
        </Intro>
      )}

      <ShiftBoard m={m} />

      {quiet && (
        <div className="ov-calm">
          <Icon name="check" />
          <span>Nothing needs you and nothing is running right now.</span>
        </div>
      )}

      {(needs.length > 0 || issuesShown.length > 0) && (
        <section className="section ov-sec">
          <h2 className="section-h">Needs you <span className="count">{m.attention.length + issuesLeft.length}</span></h2>
          <div className="rows">
            {needs.map((a) => <NeedsRow key={a.loop.host + a.loop.name} d={a.loop} state={a.state} reason={a.reason} />)}
            {moreNeeds > 0 && <Link className="row ov-more" to="/loops">{pl(moreNeeds, 'more loop')} waiting</Link>}
            {issuesShown.map((x) => <IssueRow key={x.id} x={x} />)}
            {moreIssues > 0 && <Link className="row ov-more" to="/issues">{pl(moreIssues, 'more open issue')}</Link>}
          </div>
        </section>
      )}

      {running.length > 0 && (
        <section className="section ov-sec">
          <h2 className="section-h">Running now <span className="count">{running.length}</span></h2>
          <div className="rows">
            {running.slice(0, RUNNING_CAP).map((d) => <RunningRow key={d.host + d.name} d={d} />)}
            {running.length > RUNNING_CAP && <Link className="row ov-more" to="/loops">{pl(running.length - RUNNING_CAP, 'more loop')} running</Link>}
          </div>
        </section>
      )}

      {m.results.length > 0 ? (
        <section className="section ov-sec">
          <h2 className="section-h">Recent results</h2>
          <div className="rows">{m.results.map((d) => <ResultRow key={d.host + d.name} d={d} />)}</div>
        </section>
      ) : (
        m.recent.length > 0 && (
          <section className="section ov-sec">
            <h2 className="section-h">Recently updated</h2>
            <div className="rows">
              {m.recent.map((d) => (
                <Link key={d.host + d.name} className="row ov-row" to={loopHref(d)}>
                  <Badge state={honestState(d)} />
                  <span className="grow"><span className="ov-name">{d.name}</span></span>
                  <span className="end">{ago(d.updated || d.started)}</span>
                </Link>
              ))}
            </div>
          </section>
        )
      )}

      <nav className="ov-stats" aria-label="At a glance">
        <Stat label="Loops" n={m.loops.length} to="/loops" sub={m.running.length ? `${m.running.length} running` : undefined} />
        <Stat label="Plans" n={m.docs} to="/plans" sub={m.objectives ? `${m.objectives} in progress` : undefined}
          err={!!(docs.error || docs.data?.error)} pending={docs.isPending} />
        <Stat label="Issues" n={m.issuesOpen} to="/issues" sub="open" err={!!(issues.error || issues.data?.error)} pending={issues.isPending} />
        <Stat label="Machines" n={m.origins} to="/machines" sub={`${m.reachable} online`} err={!!(origins.error || origins.data?.error)} pending={origins.isPending} />
      </nav>
    </Page>
  );
}
