// Quality pieces shared by the Library list and the agent page. Two signals,
// always side by side: the analyst's score and your rating — never blended.
import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ago, fmtWhen, loopHref, ownerTally, sparkBars, VERDICT_LABEL,
  type QualityAgent, type QualityLoop, type QualityVerdict,
} from '@loopyard/api';
import { Icon } from '../../components/icons';


/** Tiny bar sparkline — one bar per scored run, oldest → newest. */
export function ScoreBars({ points, label }: { points: { score: number | null; ts?: number | null; runStarted?: number | null; loop?: string }[]; label: string }) {
  const bars = sparkBars(points);
  const src = points.slice(-bars.length);
  if (!bars.length) return null;
  return (
    <span className="lib-spark" role="img" aria-label={label}>
      {bars.map((b, i) => {
        const p = src[i];
        const when = fmtWhen(p.ts ?? p.runStarted ?? null);
        return (
          <span
            key={i}
            className={'lib-sbar' + (b.grade ? ` g-${b.grade}` : ' g-none')}
            style={{ height: `${Math.round(b.h * 100)}%` }}
            title={[b.score == null ? 'Not scored' : `Analyst ${b.score}`, p.loop, when].filter(Boolean).join(' · ')}
          />
        );
      })}
    </span>
  );
}

/** "Analyst 88 ↑" — the score, coloured by grade, with a trend arrow. */
export function AnalystScore({ score, trend, delta, pending, reason }: { score: number | null | undefined; trend?: QualityAgent['trend']; delta?: number | null; pending?: boolean; reason?: string }) {
  if (score == null) {
    return (
      <span className="lib-an lib-quiet" title={pending ? 'Being scored — check back shortly' : reason ? `Not scored: ${reason}` : 'Not scored yet'}>
        Analyst —
      </span>
    );
  }
  const g: QualityVerdict = score >= 70 ? 'good' : score >= 40 ? 'ok' : 'bad';
  const tip = `The analyst's score out of 100 (good from 70, okay from 40)` + (delta != null && trend ? ` · recent runs ${delta > 0 ? '+' : ''}${delta} vs earlier` : '');
  return (
    <span className="lib-an" title={tip}>
      Analyst <b className={`lib-score g-${g}`}>{Math.round(score)}</b>
      {trend === 'up' && <Icon name="chevronUp" size={14} className="lib-up" />}
      {trend === 'down' && <Icon name="chevronDown" size={14} className="lib-down" />}
    </span>
  );
}

/** "You 9 good · 1 bad" / "You: good" / "Not rated". */
export function OwnerRating({ tally, verb }: { tally?: { good: number; ok: number; bad: number }; verb?: QualityVerdict | null }) {
  const text = tally ? ownerTally(tally) : verb ? VERDICT_LABEL[verb].toLowerCase() : '';
  return (
    <span className={'lib-you' + (text ? '' : ' lib-quiet')} title="Your own rating of each run">
      {text ? <>You <span className={verb ? `g-${verb}` : undefined}>{text}</span></> : 'Not rated by you'}
    </span>
  );
}

export function Attention({ reason }: { reason?: string }) {
  return (
    <span className="badge b-attention" title={reason}>
      Needs attention
    </span>
  );
}

/** One loop's runs: each run's analyst score and your rating. */
export function RunHistory({ loop }: { loop: QualityLoop }) {
  const runs = loop.runs.slice().reverse();
  if (!runs.length) return <p className="lib-quiet lib-small">No runs have been scored or rated yet.</p>;
  return (
    <div className="lib-runs">
      <ScoreBars points={loop.runs} label={`Analyst score across ${loop.runs.length} runs`} />
      <ul className="lib-runlist">
        {runs.slice(0, 8).map((r, i) => (
          <li key={i}>
            <span className="lib-quiet">{r.runStarted ? ago(r.runStarted) : 'Earlier run'}</span>
            <AnalystScore score={r.score} />
            <OwnerRating verb={r.owner} />
          </li>
        ))}
      </ul>
      {runs.length > 8 && <p className="lib-quiet lib-small">and {runs.length - 8} earlier runs</p>}
    </div>
  );
}

/** Agent page header block: both signals, the score history, best/worst. */
export function AgentQuality({ a, loops }: { a: QualityAgent; loops: QualityLoop[] }) {
  const [all, setAll] = useState(false);
  return (
    <section className="section lib-aq">
      <h2 className="section-h">Quality</h2>
      <div className="lib-aqgrid">
        <div className="lib-aqcell">
          <div className="lib-k">Analyst score <span className="lib-quiet">· average of {a.scored} run{a.scored === 1 ? '' : 's'}</span></div>
          <div className="lib-v"><AnalystScore score={a.avgScore} trend={a.trend} delta={a.trendDelta} /></div>
          <ScoreBars points={a.history} label={`Score history across ${a.history.length} runs`} />
        </div>
        <div className="lib-aqcell">
          <div className="lib-k">Your rating</div>
          <div className="lib-v"><OwnerRating tally={a.owner} /></div>
          {a.needsAttention && <Attention reason={a.attentionReason} />}
        </div>
      </div>
      <p className="meta lib-bw">
        {a.best && <span>Best run: <Link to={loopHref(a.best.loop, a.best.origin)}>{a.best.loop}</Link> ({Math.round(a.best.score)})</span>}
        {a.worst && <span className="dotsep">Worst: <Link to={loopHref(a.worst.loop, a.worst.origin)}>{a.worst.loop}</Link> ({Math.round(a.worst.score)})</span>}
        {a.retries > 0 && <span className="dotsep" title="Turns that had to be retried">{Math.round(a.retryRate * 100)}% retried</span>}
        {a.versions.length > 0 && <span className="dotsep" title="Saved versions frozen into these loops">Ran version{a.versions.length > 1 ? 's' : ''} {a.versions.join(', ')}</span>}
      </p>
      {loops.length > 0 && (
        <>
          <h3 className="section-h rl-gap">Loops it was in <span className="count">{loops.length}</span></h3>
          <div className="rows">
            {(all ? loops : loops.slice(0, 10)).map((l) => (
              <Link key={l.name} to={loopHref(l.name, l.origin)} className="row lib-row">
                <span className="grow lib-name" title={l.name}>
                  <span className="mono lib-ell">{l.name}</span>
                  {l.needsAttention && <Attention reason={l.attentionReason} />}
                </span>
                <AnalystScore score={l.analyst?.score} pending={l.analystPending} reason={l.analystReason} />
                <OwnerRating verb={l.owner?.verb} />
              </Link>
            ))}
          </div>
          {loops.length > 10 && (
            <button type="button" className="btn quiet sm rl-showall" onClick={() => setAll((v) => !v)}>
              {all ? 'Show top 10' : `Show all ${loops.length} loops`}
            </button>
          )}
        </>
      )}
    </section>
  );
}
