import { friendlyError, Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { dispositionMap, dispositionRollup, filterLoops, inProjectScope, loopOwner, loopOwners, PROJECT_UNATTR, type DispRollup, type LoopSummary, type OverviewLoop } from '@loopyard/api';
import { useLoops } from '../../api';
import { platform } from '../../platform';
import { useProjectScope } from '../../scope';
import { Ledger } from './Ledger';
import { useLedgerDocs, useLoopScores } from './ledgerData';
import { LoopDetail } from './LoopDetail';
import { useDispositions } from './hooks';
import { LoopOwnerScope } from './ownerScope';
import './loops.css';
import './ledger.css';

const ARCH_KEY = 'loops.showArchived';
const ALL_OWNERS = '';

export default function LoopsView() {
  const [ownerPick, setOwner] = useState<string>(ALL_OWNERS);
  // The unscoped read feeds the owner picker (and is the whole view on a single-owner box);
  // picking an owner switches the list to the server-scoped ?owner= read.
  const unscoped = useLoops();
  const owners = useMemo(() => loopOwners(unscoped.data?.loops ?? []), [unscoped.data]);
  const multiOwner = owners.length > 1;
  const owner = multiOwner && owners.includes(ownerPick) ? ownerPick : ALL_OWNERS;
  const scoped = useLoops(owner || undefined);
  const { data, error, isPending, refetch } = owner ? scoped : unscoped;
  const { host, name } = useParams();
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [singleOnly, setSingleOnly] = useState(false);
  const [showArchived, setShowArchived] = useState(platform.storage.get(ARCH_KEY) === '1');

  const { project } = useProjectScope(); // global project switcher (shell owner)
  // Unattributed has no slug the aggregate can key on → fleet verbs, no rollup line.
  const dispScope = project && project !== PROJECT_UNATTR ? project : '';
  const disps = useDispositions(dispScope);
  const dispByLoop = useMemo(() => dispositionMap(disps.data), [disps.data]);
  const dispRoll = dispScope ? dispositionRollup(disps.data) : null;
  // Client-side owner filter too, so the scope holds even against a backend that ignores ?owner=.
  const all = useMemo(
    () => (data?.loops ?? []).filter((d) => inProjectScope(d, project) && (!owner || loopOwner(d) === owner)),
    [data, project, owner],
  );
  const visible = useMemo(() => filterLoops(all, { query, showArchived, singleOnly }), [all, query, showArchived, singleOnly]);
  const unfilteredCount = useMemo(() => filterLoops(all, { showArchived }).length, [all, showArchived]);
  // Plans are the loops' workstreams (the ledger groups by them); scores are the analyst's, read-only.
  const docs = useLedgerDocs();
  const scores = useLoopScores();
  const archivedCount = useMemo(() => all.filter((d) => d.archived).length, [all]);
  const singles = useMemo(() => filterLoops(all, { showArchived, query }).filter((d) => d.single_agent).length, [all, showArchived, query]);
  const selected = data?.loops.find((d) => d.host === host && d.name === name);
  // A filter shows only when it would change something (or is already on).
  const showSingle = singleOnly || (singles > 0 && singles < unfilteredCount);
  const showArchToggle = showArchived || archivedCount > 0;

  const hasFilters = showSingle || showArchToggle || multiOwner;
  const pick = (d: LoopSummary) => navigate(`/loops/${encodeURIComponent(d.host)}/${encodeURIComponent(d.name)}`);
  const toggleArchived = () => {
    const on = !showArchived;
    setShowArchived(on);
    platform.storage.set(ARCH_KEY, on ? '1' : '0');
  };

  // First run: no loops anywhere → one centred welcome, not an empty ledger.
  if (!isPending && !error && data && !data.loops?.length)
    return (
      <div className="lp-first">
        <div className="empty lp-welcome">
          <span className="lp-welcome-ico" aria-hidden="true"><Icon name="loop" size={22} /></span>
          <span className="kick">No loops yet</span>
          <p>A <b>loop</b> is a small team of AI agents working on one goal until it's done. Describe what you want and Swingshift puts the team together — you watch and can step in any time.</p>
          <button className="btn primary" onClick={() => navigate('/newloop')}>
            <Icon name="plus" /> Create your first loop
          </button>
          <button className="btn quiet sm" onClick={() => navigate('/overview')}>See the getting-started steps</button>
        </div>
      </div>
    );
  if (isPending) return <div className="lg"><Skeleton lines={8} /></div>;
  if (error) {
    const fe = friendlyError(error, 'loops');
    return (
      <div className="empty err">
        <span className="kick">{fe.title}</span>
        <p>{fe.detail}</p>
        <button className="btn primary" onClick={() => refetch()}>Try again</button>
      </div>
    );
  }

  // The filters the page always had, beside the ledger's own.
  const tools = hasFilters || dispRoll ? (
    <>
      {showSingle && (
        <button type="button" className={'lchip' + (singleOnly ? ' on' : '')} aria-pressed={singleOnly} onClick={() => setSingleOnly((s) => !s)}
          title="Only loops with a single agent">
          Solo agents <span className="lchipn">{singles}</span>
        </button>
      )}
      {showArchToggle && (
        <button type="button" className={'lchip' + (showArchived ? ' on' : '')} aria-pressed={showArchived} onClick={toggleArchived}>
          Archived <span className="lchipn">{archivedCount}</span>
        </button>
      )}
      {multiOwner && (
        <label className="lchip owner">
          Owner
          <select value={owner} onChange={(e) => setOwner(e.target.value)} aria-label="Filter loops by owner">
            <option value={ALL_OWNERS}>all</option>
            {owners.map((o) => (
              <option key={o} value={o}>{o}</option>
            ))}
          </select>
        </label>
      )}
      {dispRoll && <DispRoll r={dispRoll} />}
    </>
  ) : null;

  const ledger = (compact: boolean) => (
    <Ledger loops={visible as OverviewLoop[]} docs={docs.data?.docs ?? []} scores={scores} selected={selected as OverviewLoop | undefined}
      compact={compact} onPick={pick} query={query} setQuery={setQuery} extraTools={tools} disp={dispByLoop} showOwner={multiOwner} />
  );

  if (!selected) return <div className="lg-page">{ledger(false)}</div>;
  return (
    <div className="lg-split">
      <aside className="lg-side" aria-label="Loops">{ledger(true)}</aside>
      <div className="detail">
        <LoopOwnerScope.Provider value={owner}>
          <LoopDetail loop={selected} onClose={() => navigate('/loops')} />
        </LoopOwnerScope.Provider>
      </div>
    </div>
  );
}

/** The per-project "how loops landed here" rollup (only once something is rated). */
function DispRoll({ r }: { r: DispRollup }) {
  return (
    <div className="disproll" title={`${r.rated} of ${r.total} loops rated`}>
      Your ratings: {r.good} good · {r.ok} ok · {r.bad} bad
      {r.pct != null && <span className={'rg' + (r.pct < 50 ? ' low' : '')}> · {r.pct}% good</span>}
    </div>
  );
}
