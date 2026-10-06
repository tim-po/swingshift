import { useEffect, useState, type ReactNode } from 'react';
import {
  failReason, fileUrl, gradeTone, isErrored, livePhaseLabel, looksRed, since, topDeliverables, turnsLabel, verdictLabel,
  type Goodness, type LoopDetail, type LoopStatus, type Resolution, type TeamRoom,
} from '@loopyard/api';
import { Coachmark } from '../../components/Coachmark';
import { Icon } from '../../components/icons';
import { config } from '../../config';
import { Clamp } from './Clamp';
import { useLoopOwnerScope } from './ownerScope';

type Rate = (verb: 'good' | 'ok' | 'bad', note: string) => Promise<string>;

/**
 * The ONE summary card at the top of the page.
 * Running → live progress (turn N of M, who's working, when it wraps up, the
 * manager's latest word). Finished → verdict · analyst score · tests · commit, the
 * key results, and your own rating. Your rating and the analyst's score stay two
 * separate values — never averaged or combined.
 */
export function RunSummary({ d, tr, status, running, goodness, rate, rescore, rescoring, openFiles }: {
  d: LoopDetail;
  tr: TeamRoom | null;
  status: LoopStatus | null;
  running: boolean;
  /** Only for loops on this machine (the analyst runs locally). */
  goodness?: Goodness | null;
  rate: Rate;
  rescore?: () => void;
  rescoring?: boolean;
  openFiles(): void;
}) {
  if (running) return <Live d={d} tr={tr} status={status} />;
  if (status?.phase === 'config') return null;
  return <Finished d={d} rc={tr?.resolution} goodness={goodness} rate={rate} rescore={rescore} rescoring={rescoring} openFiles={openFiles} />;
}

/** Live progress: "Turn 4 of 28 · converging", a bar, who is working, when it wraps up. */
function Live({ d, tr, status }: { d: LoopDetail; tr: TeamRoom | null; status: LoopStatus | null }) {
  const t = tr?.turn;
  const used = t?.used ?? d.turns_used;
  const limit = t?.limit ?? d.turnLimit;
  const turn = turnsLabel({ turns_used: used, turnLimit: limit }) || 'Starting up';
  const phase = livePhaseLabel(status);
  const wd = t?.running ? t.winddown_in : null;
  const pct = used != null && limit ? Math.min(100, Math.round((used / limit) * 100)) : null;
  const live = d.live ?? [];
  const doing = live.length
    ? live.map((r) => ({ agent: r.agent, what: [r.phase, r.since ? since(r.since) : ''].filter(Boolean).join(' · ') }))
    : (tr?.roster ?? []).filter((c) => c.doingNow).map((c) => ({ agent: c.agent, what: c.doingNow || '' }));
  return (
    <section className="ld-sum live" aria-label="Progress">
      <div className="ld-sum-line">
        <span className="ld-dot" aria-hidden="true" />
        <b>{turn}</b>
        {phase && <span>· {phase}</span>}
        {t?.in_winddown ? (
          <span className="ld-muted" title="The manager is wrapping the run up (wind-down)">· wrapping up now</span>
        ) : wd != null ? (
          <span className="ld-muted" title="After this many turns the manager starts wrapping up (wind-down)">
            · wraps up in {wd} turn{wd === 1 ? '' : 's'}
          </span>
        ) : null}
      </div>
      {pct != null && (
        <div className="ld-bar" role="progressbar" aria-valuemin={0} aria-valuemax={limit ?? undefined} aria-valuenow={used ?? undefined} aria-label={turn}>
          <span style={{ width: `${pct}%` }} />
        </div>
      )}
      {doing.length > 0 && (
        <div className="ld-sum-doing">
          <span className="ld-muted">{doing.length} agent{doing.length === 1 ? '' : 's'} working</span>
          {doing.map((x, i) => (
            <span key={x.agent + i} className="ld-sum-who" title={x.what ? `${x.agent} — ${x.what}` : x.agent}>
              <b>{x.agent}</b>{x.what ? ' · ' + x.what : ''}
            </span>
          ))}
        </div>
      )}
      {tr?.managerRead && (
        <div className="ld-sum-mgr">
          <span className="ld-muted">Latest from the manager</span>
          <Clamp text={tr.managerRead} lines={2} label="manager's note" />
        </div>
      )}
    </section>
  );
}

function Finished({ d, rc, goodness, rate, rescore, rescoring, openFiles }: {
  d: LoopDetail;
  rc: Resolution | null | undefined;
  goodness?: Goodness | null;
  rate: Rate;
  rescore?: () => void;
  rescoring?: boolean;
  openFiles(): void;
}) {
  const errored = isErrored(d);
  const verdict = errored ? null : verdictLabel(rc?.verdict);
  const g = goodness && !goodness.error ? goodness : null;
  const analyst = g?.analyst ?? null;
  const tests = rc?.proof?.tests;
  const commit = rc?.handles?.commitShort;
  const rateable = canRateLoop(rc);
  const auto = rc?.disposition?.autoStatus;
  const top = topDeliverables(rc);
  if (!errored && !verdict && !g && !rateable && !auto?.value && !commit && !top.items.length) return null;

  const facts: ReactNode[] = [];
  if (g) {
    facts.push(
      <span key="an" className="ld-sum-analyst" data-side="analyst"
        title={analyst ? `${analyst.rationale || ''}${analyst.analyst ? ` (${analyst.analyst})` : ''} — kept separate from your rating, never averaged` : g.analystReason || 'An automatic review of the finished run'}>
        {analyst ? (
          <>analyst <span className={'ld-grade t-' + gradeTone(analyst.grade)}>{analyst.score}/100</span></>
        ) : (
          <span className="ld-muted">analyst: not scored yet</span>
        )}
        {rescore && (
          <button type="button" className="ld-linkbtn ld-sum-rescore" disabled={rescoring} onClick={rescore}>
            {rescoring ? 'Scoring…' : analyst ? 'Re-score' : 'Score now'}
          </button>
        )}
      </span>,
    );
  }
  if (tests) facts.push(<span key="t" className={'ld-sum-tests' + (looksRed(tests) ? ' ld-bad' : '')} title={tests}>tests {tests}</span>);
  if (commit) facts.push(<span key="c" title={rc?.handles?.commit || undefined}>commit <code className="mono">{commit}</code></span>);

  return (
    <section className={'ld-sum' + (errored ? ' bad' : '')} aria-label="Result">
      {errored ? (
        <div className="ld-sum-fail" role="note">
          <div className="ld-sum-line"><Icon name="x" className="ld-bad" /><b className="ld-bad">This run didn't finish — it ended with an error</b></div>
          <p className="rf-why">{failReason(d)}</p>
          <p className="rf-next ld-muted">Check that the machine running it is online, then run it again. The timeline below has the full trace.</p>
        </div>
      ) : null}
      {(verdict || facts.length > 0) && (
        <div className="ld-sum-line ld-sum-facts">
          {verdict && (
            <span className={'ld-verdict t-' + verdict.tone} title={rc?.verdict?.reason || undefined}>
              <Icon name={verdict.tone === 'green' ? 'check' : 'info'} size={14} />
              {verdict.label}
            </span>
          )}
          {facts}
        </div>
      )}
      {top.items.length > 0 && <KeyResults loop={d.name} rc={rc} openFiles={openFiles} />}
      {rateable && (
        <RateControl rc={rc!} rate={rate} />
      )}
      {auto?.value && (
        <div className="ld-rc-auto" title={auto.reason || undefined}>
          <span className="ak">{auto.value}</span>
          {auto.reason ? ` ${auto.reason}` : ''}
          <span className="ld-muted"> · set automatically</span>
        </div>
      )}
    </section>
  );
}

/** The 1–3 results worth opening straight away; everything else is in Files. */
function KeyResults({ loop, rc, openFiles }: { loop: string; rc: Resolution | null | undefined; openFiles(): void }) {
  const owner = useLoopOwnerScope();
  const { items, more } = topDeliverables(rc);
  return (
    <ul className="ld-keyres" aria-label="Key results">
      {items.map((it) => (
        <li key={it.path}>
          <Icon name="files" size={14} className="ld-muted" />
          {it.location === 'external' ? (
            <span title={it.path}>{it.rel || it.name}</span>
          ) : (
            <a href={fileUrl(config.apiBase, loop, it.path, false, owner)} target="_blank" rel="noopener" title={it.path}>{it.rel || it.name}</a>
          )}
          {!!it.markedBy?.length && <span className="ld-muted" title="Marked as a result by">· {it.markedBy.join(', ')}</span>}
        </li>
      ))}
      {more > 0 && (
        <li>
          <button type="button" className="ld-linkbtn" onClick={openFiles}>+{more} more in Files</button>
        </li>
      )}
    </ul>
  );
}

/**
 * The engine's `offered` gate only covers positive terminals; a loop that ended in
 * error or was stopped is still the owner's to judge, and an existing rating must
 * never silently vanish. So: offered, OR any finished (not running) loop. Never
 * while the loop is still running — that is premature.
 */
export function canRateLoop(rc: Resolution | null | undefined): boolean {
  if (!rc) return false;
  if (rc.disposition?.offered) return true;
  const v = rc.verdict;
  if (!v || v.running) return false;
  return !!v.resolved || !!rc.disposition?.current;
}

const RATE_LABEL = { good: 'Good', ok: 'OK', bad: 'Bad' } as const;

/** "How did it go?" — good / ok / bad + an optional why; pre-fills the current rating. */
function RateControl({ rc, rate }: { rc: Resolution; rate: Rate }) {
  const cur = rc.disposition?.current ?? null;
  const [why, setWhy] = useState(cur?.note || '');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => setWhy(cur?.note || ''), [cur?.note]);
  const go = async (verb: 'good' | 'ok' | 'bad') => {
    setBusy(true);
    setMsg('…');
    setMsg(await rate(verb, why.trim()));
    setBusy(false);
  };
  return (
    <div className="ld-rate" data-side="owner" aria-label="Rate this loop">
      <span className="dq">How did it go?</span>
      <Coachmark id="loop.rate" title="Rate the result" body="Your rating and the analyst's score build each agent's track record.">
        <span className="ld-rc-seg" role="group" aria-label="Your rating">
          {(['good', 'ok', 'bad'] as const).map((verb) => (
            <button key={verb} type="button" className={'ld-rc-rate' + (cur?.verb === verb ? ' on-' + verb : '')} aria-pressed={cur?.verb === verb} disabled={busy} onClick={() => void go(verb)}>
              {RATE_LABEL[verb]}
            </button>
          ))}
        </span>
      </Coachmark>
      <input className="ld-rc-why input" value={why} onChange={(e) => setWhy(e.target.value)} placeholder="Why? (optional)" aria-label="Why? (optional)" />
      {msg && <span className="ld-muted ld-rc-msg" aria-live="polite">{msg}</span>}
      {cur?.verb === 'bad' && cur.nextAction && <div className="ld-rc-nextact">Next: {cur.nextAction}</div>}
    </div>
  );
}
