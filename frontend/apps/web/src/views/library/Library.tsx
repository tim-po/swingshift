// Library — agents AND loops, each judged by two separate signals: your rating
// and the analyst's score. Agents tab leads; Loops tab is one click away; the
// old usage table lives on as a collapsed "Usage" section.
import { useMemo, useState } from 'react';
import { Link, NavLink } from 'react-router-dom';
import {
  ago, LIBRARY_SORTS, loopHref, PERIODS, periodLabel, sortAgents, sortLoops,
  type LibrarySort, type QualityAgent, type QualityLoop,
} from '@loopyard/api';
import { Btn, Empty, QueryState, Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { Intro } from '../../components/Intro';
import { useShowAll } from '../../shell/disclosure';
import { useProjectScope } from '../../scope';
import { usePeriod, useQuality } from './api';
import { AnalystScore, Attention, OwnerRating, RunHistory } from './Quality';
import { agentHref, Usage } from './Usage';

export type LibraryTab = 'agents' | 'loops';
const CAP = 25;

function AgentRow({ a }: { a: QualityAgent }) {
  return (
    <Link to={agentHref(a.agent)} className="row lib-row">
      <span className="grow lib-name" title={a.agent}>
        <span className="rl-aid">{a.agent}</span>
        {a.favorite && <Icon name="star" size={14} fill="currentColor" className="lib-fav" aria-label="Favorite" />}
        {a.needsAttention && <Attention reason={a.attentionReason} />}
      </span>
      <AnalystScore score={a.avgScore} trend={a.trend} delta={a.trendDelta} />
      <OwnerRating tally={a.owner} />
      <span className="lib-n" title={`${a.turns} turns`}>{a.loopCount} loop{a.loopCount === 1 ? '' : 's'}</span>
    </Link>
  );
}

function LoopRow({ l, open, onToggle }: { l: QualityLoop; open: boolean; onToggle(): void }) {
  return (
    <div className={'lib-loop' + (open ? ' open' : '')}>
      <button type="button" className="row lib-row" aria-expanded={open} onClick={onToggle}>
        <Icon name={open ? 'chevronDown' : 'chevronRight'} size={14} className="rl-tri" />
        <span className="grow lib-name" title={l.goal || l.name}>
          <span className="mono lib-ell">{l.name}</span>
          {l.needsAttention && <Attention reason={l.attentionReason} />}
        </span>
        <AnalystScore score={l.analyst?.score} pending={l.analystPending} reason={l.analystReason} />
        <OwnerRating verb={l.owner?.verb} />
        <span className="lib-n">{l.lastTs ? ago(l.lastTs) : ''}</span>
      </button>
      {open && (
        <div className="lib-xp">
          {l.goal && <p className="lib-goal">{l.goal}</p>}
          <RunHistory loop={l} />
          {l.owner?.note && <p className="lib-small">Your note: “{l.owner.note}”</p>}
          {l.analyst?.rationale && <p className="lib-small lib-quiet" title="How the analyst arrived at its score">{l.analyst.rationale}</p>}
          <p className="meta">
            {l.agents.length > 0 && (
              <span>
                Agents:{' '}
                {l.agents.map((a, i) => (
                  <span key={a}>{i > 0 && ', '}<Link to={agentHref(a)}>{a}</Link></span>
                ))}
              </span>
            )}
          </p>
          <Link className="btn sm" to={loopHref(l.name, l.origin)}>Open loop <Icon name="chevronRight" size={14} /></Link>
        </div>
      )}
    </div>
  );
}

function useSorted<T>(rows: T[], sort: LibrarySort, fn: (r: T[], s: LibrarySort) => T[], query: string, key: (r: T) => string) {
  return useMemo(() => {
    const n = query.trim().toLowerCase();
    return fn(n ? rows.filter((r) => key(r).toLowerCase().includes(n)) : rows, sort);
  }, [rows, sort, fn, query, key]);
}
const agentKey = (a: QualityAgent) => a.agent;
const loopKey = (l: QualityLoop) => l.name;

export function Library({ tab }: { tab: LibraryTab }) {
  const { project, projectName } = useProjectScope();
  const [days, setDays] = usePeriod();
  const [sort, setSort] = useState<LibrarySort>('quality');
  const [query, setQuery] = useState('');
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [usageOpen, setUsageOpen] = useState(false);
  const [showAll] = useShowAll();
  const q = useQuality(days, project);

  const agents = useSorted(q.data?.agents ?? [], sort, sortAgents, query, agentKey);
  const loops = useSorted(q.data?.loops ?? [], sort, sortLoops, query, loopKey);
  const total = tab === 'agents' ? q.data?.agents?.length ?? 0 : q.data?.loops?.length ?? 0;
  const shown = tab === 'agents' ? agents.length : loops.length;
  const cap = all || showAll ? Infinity : CAP;
  const what = tab === 'agents' ? 'agents' : 'loops';
  const scope = `${periodLabel(days).toLowerCase()}${project ? ` in ${projectName || 'this project'}` : ''}`;

  let body: React.ReactNode;
  if (q.isPending) body = <Skeleton lines={6} />;
  else if (q.error) body = <QueryState isPending={false} error={q.error} what="the library" onRetry={() => q.refetch()} />;
  else if (q.data?.error)
    body = (
      <Empty tone="err" title="Couldn't load the library">
        <p>Something went wrong reading quality. This is usually temporary.</p>
        <Btn variant="primary" onClick={() => q.refetch()}>Try again</Btn>
      </Empty>
    );
  else if (!total)
    body = (
      <Empty title={`No ${what} ran in the ${scope}`}>
        {days ? <Btn onClick={() => setDays(0)}>Show all time</Btn> : <p>Start a loop and its agents show up here.</p>}
      </Empty>
    );
  else if (!shown)
    body = (
      <Empty title={`No ${what} match “${query}”`}>
        <Btn onClick={() => setQuery('')}>Clear search</Btn>
      </Empty>
    );
  else
    body = (
      <>
        <div className="rows lib-list">
          {tab === 'agents'
            ? agents.slice(0, cap).map((a) => <AgentRow key={a.agent} a={a} />)
            : loops.slice(0, cap).map((l) => <LoopRow key={l.name} l={l} open={open === l.name} onToggle={() => setOpen((o) => (o === l.name ? null : l.name))} />)}
        </div>
        {shown > CAP && !showAll && (
          <button type="button" className="btn quiet sm rl-showall" onClick={() => setAll((v) => !v)}>
            {all ? 'Show fewer' : `Show all ${shown} ${what}`}
          </button>
        )}
        {(q.data?.pending ?? 0) > 0 && (
          <p className="lib-quiet lib-small" title="The analyst scores a few finished runs at a time">
            {q.data!.pending} finished run{q.data!.pending === 1 ? ' is' : 's are'} still waiting for a score — they fill in on their own.
          </p>
        )}
      </>
    );

  return (
    <div className="page lib">
      <header className="pagehead">
        <div>
          <h1 className="h1">Library</h1>
          <div className="sub">Your agents and loops, and how well they’re doing.</div>
        </div>
      </header>
      <nav className="lib-tabs" aria-label="Library views">
        {(['agents', 'loops'] as const).map((t) => (
          <NavLink key={t} to={`/library/${t}`} className={() => 'lib-tab' + (t === tab ? ' on' : '')} aria-current={t === tab ? 'page' : undefined}>
            {t === 'agents' ? 'Agents' : 'Loops'}
            {q.data && <span className="lib-tabn">{t === 'agents' ? q.data.agents?.length ?? 0 : q.data.loops?.length ?? 0}</span>}
          </NavLink>
        ))}
      </nav>
      <Intro id="library">
        Quality combines two separate signals — your rating and the analyst’s score — they’re never averaged together.
      </Intro>
      <div className="lib-bar">
        <label className="lib-ctl">
          <span className="lib-quiet">Sort</span>
          <select className="select lib-select" value={sort} onChange={(e) => setSort(e.target.value as LibrarySort)} aria-label="Sort by">
            {LIBRARY_SORTS.map((s) => <option key={s.id} value={s.id} title={s.title}>{s.label}</option>)}
          </select>
        </label>
        {total >= 6 && (
          <label className="searchbox lib-search">
            <Icon name="search" className="sglyph" />
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={tab === 'agents' ? 'Find an agent' : 'Find a loop'} spellCheck={false} aria-label={`Filter ${what}`} />
            {query && <button type="button" className="sclear" onClick={() => setQuery('')} aria-label="Clear search"><Icon name="x" size={14} /></button>}
          </label>
        )}
        <select className="select lib-select lib-period" value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Period">
          {PERIODS.map((p) => <option key={p.days} value={p.days}>{p.label}</option>)}
        </select>
      </div>
      {body}

      {tab === 'agents' && (
        <section className="section rl-sec">
          <h2 className="section-h">
            Usage
            {!showAll && (
              <button type="button" className="btn quiet sm spacer" aria-expanded={usageOpen} onClick={() => setUsageOpen((o) => !o)}>
                {usageOpen ? 'Hide' : 'Show'} <Icon name={usageOpen ? 'chevronDown' : 'chevronRight'} size={14} />
              </button>
            )}
          </h2>
          {usageOpen || showAll ? <Usage /> : <p className="rl-stone rl-collapsed">Saved agents, and every agent’s turns, retries and kept-going counts — all time.</p>}
        </section>
      )}
    </div>
  );
}
