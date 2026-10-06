import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  DEFAULT_AGENT_SORT, filterSortAgents, isAdHoc, loopHref, mins, nextAgentSort, reportStatusLabel, savedAgentNote, statusEntries,
  type AgentSort, type AgentSortCol, type AgentStats, type SavedAgent,
} from '@loopyard/api';
import { Btn, Empty, QueryState, Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { useShowAll } from '../../shell/disclosure';
import { useAnalytics, useRegistry, useSaveAgent } from './api';
import { Star } from './Star';

const COLS: { col: AgentSortCol; label: string; title: string; num?: boolean }[] = [
  { col: 'agent', label: 'Agent', title: 'Agent name' },
  { col: 'loop_count', label: 'Loops', title: 'How many loops this agent worked in', num: true },
  { col: 'turns', label: 'Turns', title: 'Total turns taken', num: true },
  { col: 'avg', label: 'Avg. turn', title: 'Average time between turns', num: true },
  { col: 'continues', label: 'Kept going', title: 'Turns that ended with “more to do”', num: true },
  { col: 'retries', label: 'Retries', title: 'Turns that had to be retried', num: true },
];

const SAVED_CAP = 6;
export const agentHref = (id: string) => `/library/agents/${encodeURIComponent(id)}`;

export function Expanded({ a }: { a: AgentStats }) {
  const loops = a.loops ?? [];
  return (
    <div className="rl-xp">
      <p className="rl-xpline">
        {statusEntries(a.statuses).map(([k, v]) => `${reportStatusLabel(k)} ${v}`).join(' · ') || 'No turn reports yet'}
      </p>
      {loops.length > 0 && (
        <p className="rl-xpline">
          Worked in{' '}
          {loops.slice(0, 8).map((l, i) => (
            <span key={l}>
              {i > 0 && ', '}
              <Link to={loopHref(l, a.loop_origins?.[l])}>{l}</Link>
            </span>
          ))}
          {loops.length > 8 && ` and ${loops.length - 8} more`}
        </p>
      )}
      <Link className="btn sm" to={agentHref(a.agent)}>Open agent <Icon name="chevronRight" size={14} /></Link>
    </div>
  );
}

/** "Ad-hoc" flag + one-click "Save to library" for an agent that ran but has no
 *  registry record (loopyard-follow-up-1790089838). Lifts its step-def from
 *  `save_from` via loop_save_agent; status-only agents have nothing to lift. */
export function AdHoc({ a }: { a: AgentStats }) {
  const save = useSaveAgent();
  const from = a.save_from;
  return (
    <>
      <span className="rl-savedtag rl-adhoc" title="Ran in a loop but isn't in your library yet, so it can't be opened as a template">Ad-hoc</span>
      <button
        type="button"
        className="btn quiet sm rl-savebtn"
        disabled={!from || save.isPending}
        title={
          !from ? 'No saved step to copy — this agent only appears in turn reports'
            : save.error ? `Couldn't save — ${save.error.message}. Try again`
              : `Save to library from ${from}`
        }
        onClick={(e) => {
          e.stopPropagation();
          if (from) save.mutate({ id: a.agent, fromLoop: from });
        }}
      >
        {save.isPending ? 'Saving…' : save.error ? 'Retry save' : 'Save to library'}
      </button>
    </>
  );
}

function SavedCard({ a, stats }: { a: SavedAgent; stats?: AgentStats }) {
  const meta = [
    a.model,
    stats ? `${stats.loop_count} loop${stats.loop_count === 1 ? '' : 's'}` : 'Not used yet',
    a.versions && a.versions > 1 ? `${a.versions} versions` : '',
  ].filter(Boolean);
  return (
    <div className="card rl-saved">
      <div className="rl-savedhd">
        <Link className="h3 rl-sname" to={agentHref(a.id)} title={a.id}>{a.id}</Link>
        <Star id={a.id} on={!!a.favorite} />
      </div>
      {a.note && <p className="rl-snote">{savedAgentNote(a.note)}</p>}
      <div className="meta">{meta.map((m, i) => <span key={i} className={i ? 'dotsep' : undefined}>{m}</span>)}</div>
    </div>
  );
}

/** The Library's secondary "Usage" lens: saved agents + every agent's turns,
 *  retries and kept-going counts (the former Roles page, all time). */
export function Usage() {
  const analytics = useAnalytics();
  const registry = useRegistry();
  const [showAll] = useShowAll();
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState<AgentSort>(DEFAULT_AGENT_SORT);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [usageOpen, setUsageOpen] = useState(false);
  const [allSaved, setAllSaved] = useState(false);

  const agents = analytics.data?.agents ?? [];
  const saved = registry.data?.agents ?? [];
  const statsById = useMemo(() => new Map<string, AgentStats>(agents.map((a) => [a.agent, a])), [agents]);
  const savedById = useMemo(() => new Map<string, SavedAgent>(saved.map((a) => [a.id, a])), [saved]);
  // Favourites first, then the most-used saved agents.
  const savedSorted = useMemo(
    () => [...saved].sort((a, b) => Number(!!b.favorite) - Number(!!a.favorite) || (statsById.get(b.id)?.loop_count ?? 0) - (statsById.get(a.id)?.loop_count ?? 0) || (statsById.get(b.id)?.turns ?? 0) - (statsById.get(a.id)?.turns ?? 0) || a.id.localeCompare(b.id)),
    [saved, statsById],
  );
  // Lead with a handful — every agent that ever ran is auto-saved, so the full
  // shelf can run to hundreds. Favorites always show.
  const favCount = saved.filter((a) => a.favorite).length;
  const savedCap = Math.max(SAVED_CAP, favCount);
  const savedShown = allSaved || showAll ? savedSorted : savedSorted.slice(0, savedCap);
  const rows = useMemo(() => filterSortAgents(agents, query, sort), [agents, query, sort]);
  const adHocCount = useMemo(() => agents.filter((a) => !savedById.has(a.agent) && isAdHoc(a)).length, [agents, savedById]);
  const maxg = Math.max(1, ...agents.map((a) => a.avg_gap_min || 0));
  // The usage table is secondary: open when asked, when Show everything is on,
  // or when there are no saved agents to lead with.
  const tableOpen = usageOpen || showAll || (!registry.isPending && saved.length === 0);
  const toggle = (id: string) =>
    setOpen((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });

  let table: React.ReactNode;
  if (analytics.isPending || analytics.error) table = <QueryState isPending={analytics.isPending} error={analytics.error} what="agents" onRetry={() => analytics.refetch()} />;
  else if (!agents.length)
    table = analytics.data?.error
      ? <Empty tone="err" title="Couldn't load agents"><p>Something went wrong reading agent activity. This is usually temporary.</p><Btn variant="primary" onClick={() => analytics.refetch()}>Try again</Btn></Empty>
      : <Empty title="No agents have run yet"><p>Start a loop and the agents that work on it show up here.</p></Empty>;
  else if (!rows.length)
    table = (
      <Empty title={`No agents match “${query}”`}>
        <Btn onClick={() => setQuery('')}>Clear search</Btn>
      </Empty>
    );
  else
    table = (
      <>
        <div className="rl-sortbar" role="group" aria-label="Sort agents">
          <span className="rl-stone">Sort by</span>
          {COLS.map((c) => {
            const on = sort.col === c.col;
            return (
              <button key={c.col} type="button" className={'chip' + (on ? ' on' : '')} aria-pressed={on} onClick={() => setSort((s) => nextAgentSort(s, c.col))}>
                {c.label}{on ? (sort.dir === 1 ? ' ↑' : ' ↓') : ''}
              </button>
            );
          })}
        </div>
        <table className="rl-table">
          <thead>
            <tr>
              {COLS.map((c) => {
                const on = sort.col === c.col;
                return (
                  <th key={c.col} className={c.num ? 'num' : undefined} aria-sort={on ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'}>
                    <button type="button" className={'rl-th' + (on ? ' on' : '')} onClick={() => setSort((s) => nextAgentSort(s, c.col))} title={c.title}>
                      {c.label}<span className="rl-arrow">{on ? (sort.dir === 1 ? ' ↑' : ' ↓') : ''}</span>
                    </button>
                  </th>
                );
              })}
              <th><span className="rl-vh">Favorite</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => {
              const s = savedById.get(a.agent);
              return (
                <RowPair key={a.agent} a={a} saved={!!s} fav={!!s?.favorite} open={open.has(a.agent)} onToggle={() => toggle(a.agent)} barW={Math.round((48 * (a.avg_gap_min || 0)) / maxg)} />
              );
            })}
          </tbody>
        </table>
        <div className="rows rl-cards">
          {rows.map((a) => {
            const s = savedById.get(a.agent);
            const isOpen = open.has(a.agent);
            return (
              <div key={a.agent} className="rl-card">
                <div className="rl-cardhd">
                  <button type="button" className="rl-cardtoggle" aria-expanded={isOpen} onClick={() => toggle(a.agent)}>
                    <Icon name={isOpen ? 'chevronDown' : 'chevronRight'} size={14} className="rl-tri" />
                    <span className="rl-aid" title={a.agent}>{a.agent}</span>
                  </button>
                  {!s && isAdHoc(a) && <AdHoc a={a} />}
                  <Star id={a.agent} on={!!s?.favorite} />
                </div>
                <div className="meta">
                  <span>{a.loop_count} loops</span><span className="dotsep">{a.turns} turns</span><span className="dotsep">{mins(a.avg_gap_min)} per turn</span>
                </div>
                {isOpen && <Expanded a={a} />}
              </div>
            );
          })}
        </div>
      </>
    );

  return (
    <>

      {registry.isPending ? (
        <Skeleton block={96} lines={0} />
      ) : savedSorted.length > 0 ? (
        <section className="rl-sec">
          <h2 className="section-h">
            {favCount ? 'Saved agents — favorites first' : 'Saved agents'}
            <span className="count">{savedShown.length} of {savedSorted.length}</span>
          </h2>
          <div className="rl-savedgrid">
            {savedShown.map((a) => <SavedCard key={a.id} a={a} stats={statsById.get(a.id)} />)}
          </div>
          {savedSorted.length > savedCap && (
            <button type="button" className="btn quiet sm rl-showall" onClick={() => setAllSaved((v) => !v)}>
              {allSaved ? 'Show fewer' : `Show all ${savedSorted.length} saved agents`}
            </button>
          )}
        </section>
      ) : null}

      <section className="section rl-sec">
        <h2 className="section-h">
          {saved.length ? 'All agents that have run' : 'Agents that have run'}
          {!analytics.isPending && agents.length > 0 && <span className="count">{agents.length}</span>}
          {adHocCount > 0 && <span className="count" title="Ran but not in your library yet">{adHocCount} ad-hoc</span>}
          {saved.length > 0 && !showAll && (
            <button type="button" className="btn quiet sm spacer" aria-expanded={tableOpen} onClick={() => setUsageOpen((o) => !o)}>
              {tableOpen ? 'Hide' : 'Show'} <Icon name={tableOpen ? 'chevronDown' : 'chevronRight'} size={14} />
            </button>
          )}
        </h2>
        {tableOpen ? (
          <>
            {agents.length >= 6 && (
              <div className="rl-search">
                <label className="searchbox">
                  <Icon name="search" className="sglyph" />
                  <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find an agent" spellCheck={false} aria-label="Filter agents by id" />
                  {query && <button type="button" className="sclear" onClick={() => setQuery('')} aria-label="Clear search"><Icon name="x" size={14} /></button>}
                </label>
                {query && <span className="rl-stone">{rows.length} of {agents.length}</span>}
              </div>
            )}
            {table}
          </>
        ) : (
          <p className="rl-stone rl-collapsed">Usage for every agent that has taken a turn — loops, turns, retries.</p>
        )}
      </section>
    </>
  );
}

function RowPair({ a, saved, fav, open, onToggle, barW }: { a: AgentStats; saved: boolean; fav: boolean; open: boolean; onToggle(): void; barW: number }) {
  return (
    <>
      <tr className={'rl-row' + (open ? ' exp' : '')} onClick={onToggle}>
        <td>
          <button type="button" className="rl-rowbtn" aria-expanded={open} onClick={(e) => { e.stopPropagation(); onToggle(); }}>
            <Icon name={open ? 'chevronDown' : 'chevronRight'} size={14} className="rl-tri" />
            <span className="rl-aid" title={a.agent}>{a.agent}</span>
            {saved && <span className="rl-savedtag" title="Saved as a reusable agent">Saved</span>}
          </button>
          {!saved && isAdHoc(a) && <AdHoc a={a} />}
        </td>
        <td className="num">{a.loop_count}</td>
        <td className="num">{a.turns}</td>
        <td className="num"><span className="rl-bar" style={{ width: barW }} />{mins(a.avg_gap_min)}</td>
        <td className="num">{a.continues || 0}</td>
        <td className="num">{a.retries || 0}</td>
        <td className="rl-favcell"><Star id={a.agent} on={fav} /></td>
      </tr>
      {open && (
        <tr className="rl-xprow">
          <td colSpan={7}><Expanded a={a} /></td>
        </tr>
      )}
    </>
  );
}
