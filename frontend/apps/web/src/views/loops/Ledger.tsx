// The Loops ledger: what's on shift, then every loop grouped by its PLAN (its
// workstream) as a scannable table. One click opens a loop; while one is open the
// ledger narrows into a list beside it (`compact`) with the same search and filters.
import { memo, useEffect, useMemo, useRef, useState, type DragEvent } from 'react';
import { Link } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import {
  ago, commonProject, groupByPlan, LEDGER_STATUS, ledgerStatus, loopKey, loopOwner, planIndex, suggestPlans,
  type HubDocRow, type LedgerStatus, type OverviewLoop, type PlanGroup,
} from '@loopyard/api';
import { Icon } from '../../components/icons';
import { platform } from '../../platform';
import { planWrites } from './ledgerData';

export type GroupMode = 'plan' | 'project' | 'none';
export type SortMode = 'recent' | 'score' | 'name';
type Filter = 'all' | LedgerStatus;

const FILTERS: Filter[] = ['all', 'need', 'run', 'done', 'part', 'fail', 'stop', 'draft'];
const LIVE = new Set<LedgerStatus>(['need', 'run']);
const OPEN_KEY = (k: string) => `loops.open.${k}`;

export interface LedgerProps {
  loops: OverviewLoop[];
  docs: HubDocRow[];
  scores: Map<string, number>;
  selected?: OverviewLoop;
  compact: boolean;
  onPick(d: OverviewLoop): void;
  query: string;
  setQuery(q: string): void;
  /** Filters the page already had (solo agents, archived, owner, your ratings) render here. */
  extraTools?: React.ReactNode;
  disp: Record<string, string>;
  showOwner: boolean;
}

const pctOf = (d: OverviewLoop) => (d.turnLimit ? Math.min(100, Math.round((100 * (d.turns_used ?? 0)) / d.turnLimit)) : 0);
const teamOf = (d: OverviewLoop) => `${d.team?.workers?.length ?? 0}w · ${d.team?.inputs?.length ?? 0}r`;
const shortAgo = (t?: number | null) => ago(t).replace(' ago', '');

export function Ledger(p: LedgerProps) {
  const { loops, docs, scores, selected, compact, onPick, query, setQuery } = p;
  const qc = useQueryClient();
  const [filter, setFilter] = useState<Filter>('all');
  const [group, setGroup] = useState<GroupMode>(() => (platform.storage.get('loops.group') as GroupMode) || 'plan');
  const [sort, setSort] = useState<SortMode>(() => (platform.storage.get('loops.sort') as SortMode) || 'recent');
  const [, bump] = useState(0);
  const [cur, setCur] = useState(-1);
  const [busy, setBusy] = useState('');
  const [dragOver, setDragOver] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);

  const q = query.trim().toLowerCase();
  // Search covers the loop's name and its team.
  const base = useMemo(
    () => loops.filter((d) => !q || d.name.toLowerCase().includes(q) || [d.team?.manager, ...(d.team?.workers ?? []), ...(d.team?.inputs ?? [])].some((a) => a?.toLowerCase().includes(q))),
    [loops, q],
  );
  const shown = useMemo(() => base.filter((d) => filter === 'all' || ledgerStatus(d) === filter), [base, filter]);
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: base.length };
    for (const d of base) c[ledgerStatus(d)] = (c[ledgerStatus(d)] ?? 0) + 1;
    return c;
  }, [base]);
  const plans = useMemo(() => planIndex(docs), [docs]);

  const groups: PlanGroup[] = useMemo(() => {
    const order = (ls: OverviewLoop[]) =>
      [...ls].sort(
        sort === 'name' ? (a, b) => a.name.localeCompare(b.name)
          : sort === 'score' ? (a, b) => (scores.get(loopKey(b)) ?? -1) - (scores.get(loopKey(a)) ?? -1)
          : (a, b) => (b.updated ?? 0) - (a.updated ?? 0),
      );
    let gs: PlanGroup[];
    if (group === 'none') gs = [{ id: '*', title: 'All loops', loops: shown, last: 0 }];
    else if (group === 'project') {
      const m = new Map<string, OverviewLoop[]>();
      for (const d of shown) m.set(d.project || d.product || '', [...(m.get(d.project || d.product || '') ?? []), d]);
      gs = [...m].map(([k, ls]) => ({ id: 'project:' + k, title: k || 'No project', loops: ls, last: Math.max(...ls.map((d) => d.updated ?? 0)) }))
        .sort((a, b) => b.last - a.last);
    } else gs = groupByPlan(shown, docs);
    return gs.map((g) => ({ ...g, loops: order(g.loops) }));
  }, [shown, docs, group, sort, scores]);

  const keyOf = (g: PlanGroup) => `${group}:${g.id ?? 'none'}`;
  // Recent groups and anything live start open; the rest remember how you left them.
  const isOpen = (g: PlanGroup, i: number) => {
    if (q || group === 'none') return true;
    if (selected && g.loops.includes(selected)) return true;
    if (groups.length === 1) return true;
    const v = platform.storage.get(OPEN_KEY(keyOf(g)));
    if (v) return v === '1';
    return (i < 3 && g.id !== null) || g.loops.some((d) => LIVE.has(ledgerStatus(d)));
  };
  const toggle = (g: PlanGroup, i: number) => {
    platform.storage.set(OPEN_KEY(keyOf(g)), isOpen(g, i) ? '0' : '1');
    bump((n) => n + 1);
  };
  const flat = groups.flatMap((g, i) => (isOpen(g, i) ? g.loops : []));
  // Loops in no plan (all of them, not just this view's filter) and the series among them worth a plan.
  const loose = useMemo(() => { const idx = planIndex(docs); return loops.filter((d) => !idx.has(d.name)); }, [loops, docs]);
  const sugg = useMemo(() => (group === 'plan' && !compact && !q ? suggestPlans(loose, 3, loops.map((d) => d.project || d.product || '')) : []), [loose, loops, group, compact, q]);

  // j/k move, Enter opens, from anywhere on the page but a text field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (/INPUT|SELECT|TEXTAREA/.test((e.target as HTMLElement).tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key !== 'j' && e.key !== 'k' && e.key !== 'Enter') return;
      const at = cur >= 0 ? cur : selected ? flat.indexOf(selected) : -1;
      if (e.key === 'Enter') {
        if (flat[at]) onPick(flat[at]);
        return;
      }
      const next = Math.max(0, Math.min(flat.length - 1, at + (e.key === 'j' ? 1 : -1)));
      setCur(next);
      if (compact && flat[next]) onPick(flat[next]); // with a loop open, moving opens the next one
      root.current?.querySelector(`[data-row="${next}"]`)?.scrollIntoView({ block: 'nearest' });
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [flat, cur, compact, selected, onPick]);

  const move = async (names: string[], to: string, label: string) => {
    setBusy(label);
    try {
      await planWrites.move(names, to);
      await qc.invalidateQueries({ queryKey: planWrites.docsKey });
    } finally {
      setBusy('');
    }
  };
  const makePlan = async (title: string, ls: OverviewLoop[]) => {
    setBusy(`Making “${title}”…`);
    try {
      const made = await planWrites.create({ project: commonProject(ls), title });
      await planWrites.move(ls.map((d) => d.name), made.doc.id);
      await qc.invalidateQueries({ queryKey: planWrites.docsKey });
    } finally {
      setBusy('');
    }
  };

  // Drag a row onto a plan's header to move it there (desktop).
  const onDrop = (g: PlanGroup) => (e: DragEvent) => {
    e.preventDefault();
    setDragOver(null);
    const name = e.dataTransfer.getData('text/x-loop');
    if (name && g.id !== undefined && group === 'plan') void move([name], g.id ?? '', `Moving ${name}…`);
  };
  const dropProps = (g: PlanGroup) =>
    group === 'plan'
      ? {
          onDragOver: (e: DragEvent) => {
            if (e.dataTransfer.types.includes('text/x-loop')) {
              e.preventDefault();
              setDragOver(keyOf(g));
            }
          },
          onDragLeave: () => setDragOver(null),
          onDrop: onDrop(g),
        }
      : {};

  const live = loops.filter((d) => LIVE.has(ledgerStatus(d))).sort((a, b) => (ledgerStatus(a) === 'need' ? -1 : 0) - (ledgerStatus(b) === 'need' ? -1 : 0));
  let row = 0;

  return (
    <div className={'lg' + (compact ? ' compact' : '')} ref={root}>
      <div className="lg-head">
        {compact && <Link className="lg-back" to="/loops">‹ All loops</Link>}
        {!compact && <h1 className="h1">Loops</h1>}
        {!compact && <span className="lg-count">{q || filter !== 'all' ? `${shown.length} of ${loops.length}` : loops.length}</span>}
        <span className="spacer" />
        <label className="searchbox lg-search">
          <span className="sglyph" aria-hidden="true"><Icon name="search" size={14} /></span>
          <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find a loop, an agent…" spellCheck={false} aria-label="Find a loop" />
        </label>
        {!compact && <Link className="btn primary" to="/newloop"><Icon name="plus" /> New loop</Link>}
      </div>

      {!compact && live.length > 0 && (
        <section className="lg-live" aria-label="On shift">
          <h2 className="section-h">On shift <span className="count">{live.length}</span></h2>
          <div className="lg-cards">
            {live.map((d) => {
              const st = ledgerStatus(d);
              return (
                <button key={loopKey(d)} type="button" className={'lg-card' + (st === 'need' ? ' need' : '')} onClick={() => onPick(d)}>
                  <span className="lg-ct"><span className={'pch ' + st} /> <b>{d.name}</b></span>
                  {st === 'need' && <span className="lg-cq">{d.question || 'Waiting on you'}</span>}
                  <span className="lg-bar"><span style={{ width: pctOf(d) + '%' }} /></span>
                  <span className="lg-cm">
                    turn {d.turns_used ?? 0}/{d.turnLimit || '∞'} · {teamOf(d)}
                    {d.last_report?.agent ? ` · ${d.last_report.agent}` : ''}
                  </span>
                </button>
              );
            })}
          </div>
        </section>
      )}

      <div className="lg-tools">
        <div className="lg-chips" role="group" aria-label="Show">
          {FILTERS.filter((f) => f === 'all' || counts[f] || filter === f).map((f) => (
            <button key={f} type="button" className={'lchip' + (filter === f ? ' on' : '')} aria-pressed={filter === f} onClick={() => setFilter(f)}>
              {f === 'all' ? 'All' : LEDGER_STATUS[f]} <span className="lchipn">{counts[f] ?? 0}</span>
            </button>
          ))}
        </div>
        {compact && p.extraTools}
        {!compact && (
          <>
            <span className="spacer" />
            {p.extraTools}
            <label className="lg-sel">Group
              <select value={group} onChange={(e) => { setGroup(e.target.value as GroupMode); platform.storage.set('loops.group', e.target.value); }}>
                <option value="plan">by plan</option><option value="project">by project</option><option value="none">no groups</option>
              </select>
            </label>
            <label className="lg-sel">Sort
              <select value={sort} onChange={(e) => { setSort(e.target.value as SortMode); platform.storage.set('loops.sort', e.target.value); }}>
                <option value="recent">recent first</option><option value="score">score</option><option value="name">name</option>
              </select>
            </label>
          </>
        )}
      </div>
      {busy && <p className="lg-busy" role="status">{busy}</p>}

      {sugg.length > 0 && (
        <section className="lg-sugg" aria-label="Sort loops into plans">
          <p><b>{loose.length} loops aren't in a plan yet.</b> A plan is a workstream: its loops group together here and in the city. These look like one each:</p>
          <div className="lg-sgs">
            {sugg.slice(0, 8).map((s) => (
              <span key={s.title} className="lg-sg">
                <b>{s.title}</b> <span className="lchipn">{s.loops.length}</span>
                <button type="button" className="btn sm" disabled={!!busy} title={s.loops.map((d) => d.name).join(', ')} onClick={() => void makePlan(s.title, s.loops)}>Make it a plan</button>
              </span>
            ))}
          </div>
          <p className="lg-sgn">Or drag any loop onto a plan, or use its ⋯ menu.</p>
        </section>
      )}

      <div className="lg-groups">
        {!shown.length && <p className="lg-none">{q ? `No loop matches “${query}”.` : 'Nothing in this view.'}</p>}
        {groups.map((g, gi) => {
          const open = isOpen(g, gi);
          const k = keyOf(g);
          return (
            <section key={k} className={'lg-grp' + (open ? ' open' : '') + (dragOver === k ? ' drop' : '') + (g.id === null ? ' loose' : '')} {...dropProps(g)}>
              <div className="lg-gh">
                <button type="button" className="lg-gt" onClick={() => toggle(g, gi)} aria-expanded={open}>
                  <span className="lg-chev" aria-hidden="true">›</span>
                  <span className="lg-gn">{g.title}</span>
                  <span className="lg-gc">{g.loops.length}</span>
                  {!compact && <Punches loops={g.loops} />}
                  {!compact && <span className="lg-when">{shortAgo(g.last)}</span>}
                </button>
                {group === 'plan' && g.id && !compact && (
                  <Link className="lg-plan" to={`/plans/${encodeURIComponent(g.id)}`} title="Open the plan"><Icon name="plan" size={14} /></Link>
                )}
              </div>
              {open && (
                <div className="lg-rows" role="list">
                  {g.loops.map((d) => {
                    const i = row++;
                    return (
                      <Row key={loopKey(d)} d={d} i={i} cur={i === cur} sel={d === selected} compact={compact} onPick={onPick}
                        score={scores.get(loopKey(d))} plan={plans.get(d.name)} docs={docs} disp={d.host === 'local' ? p.disp[d.name] : undefined}
                        showOwner={p.showOwner} showProject={group !== 'project'} draggable={group === 'plan'}
                        onMove={(to, label) => void move([d.name], to, label)} onNewPlan={(title) => void makePlan(title, [d])} />
                    );
                  })}
                </div>
              )}
            </section>
          );
        })}
      </div>
      {!compact && <p className="lg-keys">Keys: <b>/</b> find · <b>j</b>/<b>k</b> move · <b>Enter</b> open. Drag a loop onto a plan to move it there.</p>}
    </div>
  );
}

/** A plan's track record: one time-card punch per loop, oldest first. */
function Punches({ loops }: { loops: OverviewLoop[] }) {
  const ls = [...loops].sort((a, b) => (a.started ?? 0) - (b.started ?? 0)).slice(-40);
  return (
    <span className="lg-punches" aria-hidden="true">
      {ls.map((d) => <span key={loopKey(d)} className={'pch ' + ledgerStatus(d)} title={`${d.name} · ${LEDGER_STATUS[ledgerStatus(d)]}`} />)}
    </span>
  );
}

interface RowProps {
  d: OverviewLoop;
  i: number;
  cur: boolean;
  sel: boolean;
  compact: boolean;
  onPick(d: OverviewLoop): void;
  score?: number;
  plan?: HubDocRow;
  docs: HubDocRow[];
  disp?: string;
  showOwner: boolean;
  showProject: boolean;
  draggable: boolean;
  onMove(to: string, label: string): void;
  onNewPlan(title: string): void;
}

const Row = memo(function Row({ d, i, cur, sel, compact, onPick, score, plan, docs, disp, showOwner, showProject, draggable, onMove, onNewPlan }: RowProps) {
  const st = ledgerStatus(d);
  const [menu, setMenu] = useState(false);
  return (
    <div className={'lg-row' + (cur ? ' cur' : '') + (sel ? ' sel' : '') + (d.archived ? ' arch' : '')} role="listitem" data-row={i}
      draggable={draggable && !compact} onDragStart={(e) => { e.dataTransfer.setData('text/x-loop', d.name); e.dataTransfer.effectAllowed = 'move'; }}>
      <button type="button" className="lg-open" onClick={() => onPick(d)} aria-current={sel || undefined} title={d.name}>
        <span className={'pch ' + st} aria-label={LEDGER_STATUS[st]} />
        <span className="lg-nm">
          {d.name}
          {!compact && showProject && (d.project || d.product) && <small>{d.project || d.product}</small>}
          {!compact && d.host !== 'local' && <small>on {d.host}</small>}
        </span>
        {!compact && (
          <>
            <span className={'lg-res ' + st}>{LEDGER_STATUS[st]}{score != null && <i>{score}/100</i>}</span>
            <span className="lg-turns"><span className="lg-bar"><span style={{ width: pctOf(d) + '%' }} /></span>{d.turns_used ?? 0}/{d.turnLimit || '∞'}</span>
            <span className="lg-team">
              {teamOf(d)}
              {showOwner && <span className="ownerflag" title={`Owned by ${loopOwner(d)}`}>{loopOwner(d)}</span>}
              {disp && <span className={`dispflag ${disp}`} title={`You rated this loop ${disp}`}>{disp}</span>}
            </span>
          </>
        )}
        <span className="lg-ago">{shortAgo(d.updated ?? d.started)}</span>
      </button>
      {!compact && (
        <span className="lg-more">
          <button type="button" className="sh-iconbtn" aria-label={`Move ${d.name} to a plan`} aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
            <Icon name="more" size={16} />
          </button>
          {menu && <MoveMenu d={d} plan={plan} docs={docs} onClose={() => setMenu(false)} onMove={onMove} onNewPlan={onNewPlan} />}
        </span>
      )}
    </div>
  );
});

function MoveMenu({ d, plan, docs, onClose, onMove, onNewPlan }: { d: OverviewLoop; plan?: HubDocRow; docs: HubDocRow[]; onClose(): void; onMove(to: string, label: string): void; onNewPlan(title: string): void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [title, setTitle] = useState('');
  useEffect(() => {
    const down = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && onClose();
    const key = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('mousedown', down);
    document.addEventListener('keydown', key);
    return () => {
      document.removeEventListener('mousedown', down);
      document.removeEventListener('keydown', key);
    };
  }, [onClose]);
  const proj = d.project || d.product || '';
  const others = docs
    .filter((x) => x.id !== plan?.id && (!proj || !x.project || x.project === proj))
    .sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0))
    .slice(0, 30);
  return (
    <div className="sh-pop lg-menu" role="menu" ref={ref}>
      <div className="lg-mh">Move to a plan</div>
      <div className="lg-ml">
        {others.map((x) => (
          <button key={x.id} type="button" role="menuitem" className="sh-popitem" onClick={() => { onClose(); onMove(x.id, `Moving ${d.name}…`); }}>
            <Icon name="plan" size={14} /> {x.title || 'Untitled plan'}
          </button>
        ))}
        {!others.length && <p className="lg-mn">No other plans yet.</p>}
      </div>
      <form className="lg-mnew" onSubmit={(e) => { e.preventDefault(); if (title.trim()) { onClose(); onNewPlan(title.trim()); } }}>
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="New plan…" aria-label="New plan title" />
        <button type="submit" className="btn sm" disabled={!title.trim()}>Create</button>
      </form>
      {plan && (
        <button type="button" role="menuitem" className="sh-popitem" onClick={() => { onClose(); onMove('', `Taking ${d.name} out of “${plan.title}”…`); }}>
          <Icon name="x" size={14} /> Take out of “{plan.title}”
        </button>
      )}
    </div>
  );
}
