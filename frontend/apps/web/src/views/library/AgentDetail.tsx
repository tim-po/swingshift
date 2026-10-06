import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  fmtWhen, loopHref, loopsForAgent, mins, periodLabel, reportStatusLabel, statusEntries, versionTimeline,
  type AgentRecord, type AgentVersionsResponse,
} from '@loopyard/api';
import { Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { useShowAll } from '../../shell/disclosure';
import { useAgentDetail, useAgentRecord, useAgentVersions, usePeriod, useQuality, useRegistry, useTurn } from './api';
import { useProjectScope } from '../../scope';
import { AgentQuality } from './Quality';
import { Star } from './Star';
import { DistillOffer, VersionDiff } from './RegistryV2';
import { sourceLabel } from './diff';

const PER_LOOP_CAP = 10;
const TURN_CAP = 20;

const FIELD_WORD: Record<string, string> = { genericGoal: 'goal', persona: 'persona', model: 'model' };

function Identity({ id, rec, vers }: { id: string; rec?: AgentRecord; vers?: AgentVersionsResponse }) {
  const [showAll] = useShowAll();
  const [open, setOpen] = useState<Set<number>>(new Set());
  const versions = rec && !rec.error && Array.isArray(rec.versions) ? rec.versions : [];
  if (!versions.length) return null;
  const head = versions.find((v) => v.version === rec!.head) ?? versions[versions.length - 1];
  const model = head.model || rec!.model;
  const toggle = (n: number) => setOpen((s) => { const x = new Set(s); if (x.has(n)) x.delete(n); else x.add(n); return x; });
  const timeline = versionTimeline(rec, vers);
  return (
    <>
      <div className="meta rl-idmeta">
        <span title={model ? 'Recorded for this agent; loops currently use their own model setting' : undefined}>
          {model ? <>Model <span className="mono">{model}</span></> : "Uses the loop's model"}
        </span>
        {rec!.defaultRole && <span className="dotsep">Usually the {rec!.defaultRole}</span>}
      </div>
      <section className="section rl-idsec">
        <h2 className="section-h">Persona</h2>
        <div className="rl-text">{head.persona || <span className="rl-stone">No persona written yet.</span>}</div>
        <h2 className="section-h rl-gap">Goal</h2>
        <div className="rl-text">{head.genericGoal || <span className="rl-stone">No goal written yet.</span>}</div>
        <DistillOffer id={id} />
      </section>
      <details className="rl-history" open={showAll || undefined}>
        <summary>
          Version history <span className="rl-stone">{versions.length}</span>
        </summary>
        <p className="rl-stone rl-note">Every edit is kept as a new version. Loops remember the exact version they ran.</p>
        <div className="rows rl-vtl">
          {timeline.map((v) => {
            const full = versions.find((x) => x.version === v.version);
            const isOpen = open.has(v.version);
            return (
              <div key={v.version} className="rl-vitem">
                <button type="button" className="rl-vrow" aria-expanded={isOpen} onClick={() => toggle(v.version)}>
                  <Icon name={isOpen ? 'chevronDown' : 'chevronRight'} size={14} className="rl-tri" />
                  <span className="rl-vtag">Version {v.version}</span>
                  {v.isHead && <span className="rl-cur">Current</span>}
                  <span className="rl-vsrc">
                    {sourceLabel(v.source, v.changed, v.note)}
                    {(v.changed ?? []).length > 0 && ` · changed ${(v.changed ?? []).map((f) => FIELD_WORD[f] ?? f).join(', ')}`}
                    {v.model && ` · ${v.model}`}
                  </span>
                  <span className="rl-vwhen">{fmtWhen(v.createdAt)}</span>
                </button>
                {isOpen && (
                  <div className="rl-vbody">
                    {v.note && <div className="rl-vnote">“{v.note}”</div>}
                    <VersionDiff id={id} v={v.version} />
                    <div className="rl-dfield">Persona</div><div className="rl-pre">{full?.persona || '—'}</div>
                    <div className="rl-dfield">Goal</div><div className="rl-pre">{full?.genericGoal || '—'}</div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </details>
    </>
  );
}

type TurnRef = { loop: string; agent: string; seq: number };

function TurnModal({ t, onClose }: { t: TurnRef; onClose(): void }) {
  const { data, error, isPending } = useTurn(t);
  const err = error || data?.error;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="rl-modalbg" onClick={onClose}>
      <div className="rl-modal" role="dialog" aria-modal="true" aria-label={`${t.agent} turn ${t.seq}`} onClick={(e) => e.stopPropagation()}>
        <div className="rl-modalhd">
          <h2 className="h3">Turn {t.seq} <span className="rl-stone">in <span className="mono">{t.loop}</span></span></h2>
          <button type="button" className="btn quiet sm" autoFocus onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        {isPending ? <Skeleton lines={4} /> : err ? <p className="rl-errtxt">Couldn't load this turn just now — try reopening it.</p> : (
          <>
            <h3 className="section-h">What it reported</h3>
            {data?.report ? (
              <p className="rl-report"><b>{reportStatusLabel(data.report.status)}</b>{data.report.note ? ` — ${data.report.note}` : ''}</p>
            ) : <p className="rl-stone">No report for this turn.</p>}
            <h3 className="section-h rl-gap" title="The full prompt the agent received for this turn">What it was asked</h3>
            <pre className="rl-pre">{data?.prompt || '(none)'}</pre>
            {data?.transcript_tail && <><h3 className="section-h rl-gap">End of the transcript</h3><pre className="rl-pre">{data.transcript_tail}</pre></>}
          </>
        )}
      </div>
    </div>
  );
}

export function AgentDetail({ id }: { id: string }) {
  const { project } = useProjectScope();
  const [days] = usePeriod();
  const q = useQuality(days, project);
  const qa = q.data?.agents?.find((a) => a.agent === id);
  const qLoops = useMemo(() => loopsForAgent(q.data?.loops ?? [], id), [q.data?.loops, id]);
  const detail = useAgentDetail(id);
  const rec = useAgentRecord(id);
  const vers = useAgentVersions(id);
  const registry = useRegistry();
  const [turn, setTurn] = useState<TurnRef | null>(null);
  const [allLoops, setAllLoops] = useState(false);
  const [allTurns, setAllTurns] = useState(false);
  const isFav = !!registry.data?.agents.find((a) => a.id === id)?.favorite;
  const d = detail.data;
  const derr = detail.error || d?.error;
  const t = d?.totals ?? {};
  // Busiest loops first, capped — the tail is a long flat list nobody reads.
  const perLoop = useMemo(() => {
    const rows = [...(d?.per_loop ?? [])].sort((a, b) => (b.turns ?? 0) - (a.turns ?? 0));
    return allLoops ? rows : rows.slice(0, PER_LOOP_CAP);
  }, [d?.per_loop, allLoops]);
  const turnRows = useMemo(() => {
    const rows = d?.turns ?? [];
    return allTurns ? rows : rows.slice(-TURN_CAP).reverse();
  }, [d?.turns, allTurns]);
  const stat = (k: string, v: string | number | null | undefined, title?: string) => (
    <div className="rl-stat" title={title}><div className="v">{v == null ? '—' : v}</div><div className="k">{k}</div></div>
  );

  return (
    <div className="page rl-detail">
      <Link className="rl-back" to="/library"><Icon name="chevronLeft" size={14} /> Library</Link>
      <header className="rl-dhead">
        <h1 className="h1">{id}</h1>
        <Star id={id} on={isFav} label />
      </header>
      {q.isPending ? <Skeleton lines={3} /> : qa ? (
        <AgentQuality a={qa} loops={qLoops} />
      ) : (
        <p className="rl-stone rl-note">
          {q.error || q.data?.error ? "Couldn't load quality just now." : `No runs in ${periodLabel(days).toLowerCase()}${project ? ' for this project' : ''} — change the period on the Library page to see older ones.`}
        </p>
      )}
      <Identity id={id} rec={rec.data} vers={vers.data} />
      {detail.isPending ? <Skeleton lines={4} /> : derr ? <p className="rl-errtxt">Couldn't load this agent's activity just now — <button type="button" className="rl-retry" onClick={() => detail.refetch()}>try again</button>.</p> : (
        <>
          <section className="section">
            <h2 className="section-h" title="All time, every project">Usage</h2>
            <div className="rl-stats">
              {stat('turns', t.turns)}{stat('loops', t.loops)}{stat('avg. turn', mins(t.avg_gap_min), 'Average time between turns')}
              {stat('kept going', t.continues, 'Turns that ended with “more to do”')}{stat('retries', t.retries, 'Turns that had to be retried')}
            </div>
            {statusEntries(t.statuses).length > 0 && (
              <p className="rl-stone rl-note">{statusEntries(t.statuses).map(([k, v]) => `${reportStatusLabel(k)} ${v}`).join(' · ')}</p>
            )}
          </section>

          {perLoop.length > 0 && (
            <section className="section">
              <h2 className="section-h">Turns per loop <span className="count">{d?.per_loop?.length}</span></h2>
              <div className="rl-scroll">
                <table className="rl-table rl-keep">
                  <thead><tr><th>Loop</th><th className="num">Turns</th><th className="num">Kept going</th><th className="num">Retries</th><th className="num">Avg. turn</th></tr></thead>
                  <tbody>
                    {perLoop.map((p) => (
                      <tr key={p.loop}>
                        <td><Link className="rl-cell" to={loopHref(p.loop, p.origin)} title={p.loop}>{p.loop}</Link></td>
                        <td className="num">{p.turns}</td><td className="num">{p.continues || 0}</td><td className="num">{p.retries || 0}</td><td className="num">{mins(p.avg_gap_min)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {(d?.per_loop?.length ?? 0) > PER_LOOP_CAP && (
                <button type="button" className="btn quiet sm rl-showall" onClick={() => setAllLoops(!allLoops)}>
                  {allLoops ? 'Show top 10' : `Show all ${d?.per_loop?.length} loops`}
                </button>
              )}
            </section>
          )}

          {turnRows.length > 0 && (
            <section className="section">
              <h2 className="section-h">Recent turns <span className="count">{(d?.turns ?? []).length}</span></h2>
              <div className="rows rl-turns">
                {turnRows.map((r) => (
                  <button key={`${r.loop}#${r.seq}`} type="button" className="rl-turn" onClick={() => setTurn({ loop: r.loop, agent: d!.agent || id, seq: r.seq })}>
                    <span className="rl-tloop" title={r.loop}>{r.loop} <span className="rl-stone">#{r.seq}</span></span>
                    <span className={`rl-st st-${r.status}`} title={r.status}>{reportStatusLabel(r.status)}</span>
                    <span className="rl-tnote" title={r.note}>{r.note}</span>
                    <span className="rl-stone rl-tgap">{r.gap_min == null ? '' : mins(r.gap_min)}</span>
                  </button>
                ))}
              </div>
              {(d?.turns?.length ?? 0) > TURN_CAP && (
                <button type="button" className="btn quiet sm rl-showall" onClick={() => setAllTurns(!allTurns)}>
                  {allTurns ? `Show latest ${TURN_CAP}` : `Show all ${d?.turns?.length} turns`}
                </button>
              )}
            </section>
          )}
        </>
      )}
      {turn && <TurnModal t={turn} onClose={() => setTurn(null)} />}
    </div>
  );
}
