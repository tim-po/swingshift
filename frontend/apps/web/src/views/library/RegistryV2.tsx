import { useState } from 'react';
import { Btn, friendlyLine } from '../../components/ui';
import { useAcceptDistill, useDistill, useVersionDiff } from './api';
import { FIELD_LABEL, diffMode, distillReason, wordDiff, type DiffPart } from './diff';

export function DiffText({ parts }: { parts: DiffPart[] }) {
  return (
    <div className="rl-pre rl-diff">
      {parts.map((p, i) => (
        <span key={i}>
          {i > 0 && ' '}
          {p.op === 'add' ? <ins className="rl-dadd">{p.text}</ins>
            : p.op === 'del' ? <del className="rl-ddel">{p.text}</del>
              : p.text}
        </span>
      ))}
    </div>
  );
}

/** Inline word diff when the texts mostly agree; calm stacked Before / After
 *  when they share little (an interleaved diff would be word soup). */
export function ChangeView({ from, to }: { from: string; to: string }) {
  const parts = wordDiff(from, to);
  if (diffMode(parts) === 'inline') return <DiffText parts={parts} />;
  return (
    <div className="rl-stack">
      <div className="rl-dfield">Before</div><div className="rl-pre rl-before">{from || '—'}</div>
      <div className="rl-dfield">After</div><div className="rl-pre rl-after">{to || '—'}</div>
    </div>
  );
}

/** Offer a loop-agnostic form of the head generic goal. Nothing is stored
 *  until the human accepts; "not now" just hides the offer. */
export function DistillOffer({ id }: { id: string }) {
  const offer = useDistill(id);
  const accept = useAcceptDistill(id);
  const [dismissed, setDismissed] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const o = offer.data;
  if (accept.isSuccess && accept.data?.version) {
    return <div className="rl-distill done" role="status">Saved — the reusable goal is now version {accept.data.version}. Earlier versions are kept in the history below.</div>;
  }
  if (dismissed || !o || o.error || !o.changed) return null;
  const goal = draft ?? o.suggested;
  return (
    <section className="rl-distill" aria-label="Suggested reusable goal">
      <h3 className="section-h">Suggested reusable goal <span className="count">Optional — nothing changes unless you accept</span></h3>
      <p className="rl-dwhy">{distillReason(o.basis)}</p>
      <ChangeView from={o.current} to={goal} />
      <textarea className="rl-dedit" aria-label="Edit the suggested goal" value={goal} rows={3} onChange={(e) => setDraft(e.target.value)} />
      <div className="rl-dact">
        <Btn variant="primary" disabled={accept.isPending || !goal.trim()} onClick={() => accept.mutate(goal)}>
          {accept.isPending ? 'Saving…' : 'Use this goal'}
        </Btn>
        <Btn onClick={() => setDismissed(true)}>Not now</Btn>
        {accept.error && <span className="rl-errtxt">{friendlyLine(accept.error, 'save the goal')}</span>}
      </div>
    </section>
  );
}

/** What changed in version ``v`` vs the one before it — word-level, per field. */
export function VersionDiff({ id, v }: { id: string; v: number }) {
  const q = useVersionDiff(id, v - 1, v, v > 1);
  if (v <= 1) return <div className="rl-stone rl-dnote">First version — nothing to compare yet</div>;
  if (q.isPending) return <div className="rl-stone rl-dnote">Loading changes…</div>;
  if (q.error || q.data?.error) return <div className="rl-stone rl-dnote">Couldn't load the changes for this version just now.</div>;
  const rows = Object.keys(FIELD_LABEL).filter((f) => q.data!.fields?.[f]?.changed);
  if (!rows.length) return <div className="rl-stone rl-dnote">No change from version {v - 1}</div>;
  return (
    <div className="rl-vdiff" aria-label={`Changes from version ${v - 1} to ${v}`}>
      <div className="rl-dfield rl-dh">Changes from version {v - 1}</div>
      {rows.map((f) => {
        const d = q.data!.fields[f];
        return (
          <div key={f}>
            <div className="rl-dfield">{FIELD_LABEL[f]}</div>
            <ChangeView from={d.from ?? (f === 'model' ? 'inherits model' : '')} to={d.to ?? (f === 'model' ? 'inherits model' : '')} />
          </div>
        );
      })}
    </div>
  );
}
