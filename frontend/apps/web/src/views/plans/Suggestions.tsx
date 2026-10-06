// Suggestions — a power feature, kept quiet. Helper agents (internally "Sweep" and
// "Reconcile") read a machine's files or commit history and only ever SUGGEST:
// a new plan, a new note, more detail, or a status change. The human's Accept is
// the only thing that writes. Shown on a plan only when there's something pending
// (or Show everything is on); the helpers run only from an explicit button.
import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  commitEvidence,
  describeRun,
  evidenceHref,
  evidenceLabel,
  planStatusLabel,
  reconcileSplit,
  runResultRoute,
  suggestionKindMeta,
  type WsAgent,
  type WsDiscussItem,
  type WsSuggestion,
} from '@loopyard/api';
import { Btn } from '../../components/ui';
import { Icon, type IconName } from '../../components/icons';
import { Modal } from '../../shell/Modal';
import { errText } from '../hub/flash';
import { Markdown } from './markdown';
import { useRunOrigins, ws, wsKeys } from './queries';
import { dismissRun, recordRun, useRunLog } from './runLog';

type Flash = (t: string, err?: boolean) => void;

export function SuggestionCard({ oid, sug, flash }: { oid: string; sug: WsSuggestion; flash: Flash }) {
  const qc = useQueryClient();
  const [dismissed, setDismissed] = useState(false);
  const kind = suggestionKindMeta(sug.kind);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: wsKeys.suggestions(oid) });
    qc.invalidateQueries({ queryKey: wsKeys.objective(oid) });
    qc.invalidateQueries({ queryKey: wsKeys.toDiscuss(oid) });
    qc.invalidateQueries({ queryKey: wsKeys.objectives });
  };
  const accept = useMutation({
    mutationFn: () => ws.accept(oid, sug.id),
    onSuccess: (r) => { flash(r.suggestion?.result?.created_slug ? `Accepted — added “${r.suggestion.result.title || r.suggestion.result.created_slug}”.` : 'Accepted.'); refresh(); },
    onError: (e) => flash(errText(e), true),
  });
  const decline = useMutation({
    mutationFn: () => ws.decline(oid, sug.id),
    onSuccess: () => { flash('Declined.'); refresh(); },
    onError: (e) => flash(errText(e), true),
  });
  const discuss = useMutation({
    mutationFn: () => ws.discuss(oid, sug.id),
    onSuccess: () => { flash('Saved for later.'); refresh(); },
    onError: (e) => flash(errText(e), true),
  });
  const busy = accept.isPending || decline.isPending || discuss.isPending;
  if (dismissed) return null;
  const pending = sug.state === 'pending';

  return (
    <div className="pl-sug" role="region" aria-label={kind.label}>
      <div className="pl-sughead">
        <Icon name={kind.icon as IconName} />
        <span className="pl-sugkind">{kind.label}</span>
        {sug.status && <span className="muted">→ {planStatusLabel(sug.status)}</span>}
        {(sug.agent || sug.origin) && <span className="muted pl-sugsrc" title={[sug.agent, sug.origin].filter(Boolean).join(' · ')}>from {sug.origin || 'a helper'}</span>}
        <button type="button" className="pl-x" aria-label="Hide this suggestion" onClick={() => setDismissed(true)}><Icon name="x" size={14} /></button>
      </div>
      {sug.title && <div className="pl-sugtitle">{sug.title}</div>}
      {sug.target_slug && <div className="muted pl-sugtarget">On the note <span className="mono">{sug.target_slug}</span></div>}
      {sug.body && <div className="pl-sugbody"><Markdown src={sug.body} /></div>}
      {!!sug.evidence?.length && (
        <ul className="pl-evidence" aria-label="Evidence">
          {sug.evidence.map((e, i) => {
            const href = evidenceHref(e);
            return (
              <li key={i}>
                <Icon name="commit" size={14} />
                {href ? <a href={href} target="_blank" rel="noreferrer noopener" className="mono">{evidenceLabel(e)}</a> : <span className="mono">{evidenceLabel(e)}</span>}
                {e.path && <span className="muted mono"> · {e.path}</span>}
              </li>
            );
          })}
        </ul>
      )}
      {pending ? (
        <div className="pl-sugactions">
          <Btn disabled={busy} onClick={() => accept.mutate()}>Accept</Btn>
          <button type="button" className="btn quiet" disabled={busy} onClick={() => discuss.mutate()} title="Keep it for later without applying it">Later</button>
          <button type="button" className="btn quiet" disabled={busy} onClick={() => decline.mutate()}>Decline</button>
        </div>
      ) : (
        <div className="muted pl-sugresolved">
          {sug.state === 'accepted' && 'Accepted.'}
          {sug.state === 'declined' && 'Declined.'}
          {sug.state === 'discussing' && 'Saved for later.'}
        </div>
      )}
    </div>
  );
}

/** At a glance: which notes the commit history says are done vs. still to do. */
export function ProgressSplit({ suggestions }: { suggestions: WsSuggestion[] | undefined }) {
  const split = reconcileSplit(suggestions);
  if (!split.total) return null;
  const col = (label: string, cls: string, items: WsSuggestion[]) => (
    <div className={'pl-splitcol ' + cls}>
      <div className="pl-splithead">{label} <span className="muted">{items.length}</span></div>
      {items.length ? (
        <ul className="pl-splitlist">
          {items.map((s) => (
            <li key={s.id}>
              <span className="mono">{s.target_slug || s.title || s.id}</span>
              {commitEvidence(s).map((e, i) => {
                const href = evidenceHref(e);
                const short = (e.commit || '').slice(0, 7);
                return href
                  ? <a key={i} href={href} target="_blank" rel="noreferrer noopener" className="mono pl-commit" title={e.subject || e.commit}>{short}</a>
                  : <span key={i} className="mono pl-commit" title={e.subject || evidenceLabel(e)}>{short}</span>;
              })}
            </li>
          ))}
        </ul>
      ) : <div className="muted">—</div>}
    </div>
  );
  return (
    <div className="pl-split" role="region" aria-label="Progress from commits">
      <div className="pl-splittitle">Progress from commits <span className="muted">· {split.total} suggested status{split.total === 1 ? '' : 'es'}</span></div>
      <div className="pl-splitcols">
        {col('Looks done', 'done', split.done)}
        {col('Still to do', 'todo', split.todo)}
        {split.other.length > 0 && col('In between', 'other', split.other)}
      </div>
    </div>
  );
}

export function SavedForLater({ items }: { items: WsDiscussItem[] }) {
  if (!items.length) return null;
  return (
    <div className="pl-later">
      <div className="pl-splittitle">Saved for later <span className="muted">{items.length}</span></div>
      <div className="rows">
        {items.map((it) => (
          <div key={it.id} className="row pl-laterrow">
            <span className="grow">{it.title || suggestionKindMeta(it.kind).label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Launched helper runs — a quiet, dismissible record of what's looking and where results land. */
export function HelperRuns({ navigate }: { navigate(to: string): void }) {
  const runs = useRunLog();
  if (!runs.length) return null;
  return (
    <div className="pl-runs" role="region" aria-label="Helpers at work">
      {runs.map((r) => (
        <div key={r.id} className="pl-run" role="status">
          <Icon name="refresh" />
          <span className="grow">{describeRun(r)}</span>
          <button type="button" className="btn quiet sm" onClick={() => navigate(runResultRoute(r))}>See suggestions</button>
          <button type="button" className="pl-x" aria-label="Dismiss" onClick={() => dismissRun(r.id)}><Icon name="x" size={14} /></button>
        </div>
      ))}
    </div>
  );
}

const HELPERS: { id: WsAgent; label: string; blurb: string }[] = [
  { id: 'sweep', label: 'Find plan ideas', blurb: 'Read a machine’s files and suggest new plans (skipping ones you already have).' },
  { id: 'reconcile', label: 'Check progress', blurb: 'Read a machine’s code history and suggest which notes of this plan look done.' },
];

/** Start a helper — only from this dialog's explicit button. */
export function FindSuggestionsDialog({ objectiveId, onClose, flash }: { objectiveId?: string; onClose(): void; flash: Flash }) {
  const qc = useQueryClient();
  const [agent, setAgent] = useState<WsAgent>(objectiveId ? 'reconcile' : 'sweep');
  const [origin, setOrigin] = useState('');
  const [scope, setScope] = useState('');
  const opts = useRunOrigins(true);
  const options = opts.data?.options ?? [];

  const run = useMutation({
    mutationFn: () => ws.run({ agent, origin, scope: scope || undefined, objective: agent === 'reconcile' ? objectiveId : undefined }),
    onSuccess: (r) => {
      const opt = options.find((o) => o.id === origin);
      recordRun({ agent, origin, originLabel: opt?.label || opt?.name, scope: scope || undefined, objective: agent === 'reconcile' ? objectiveId : undefined, job: r.job });
      flash('Started — suggestions will show up here.');
      if (objectiveId) qc.invalidateQueries({ queryKey: wsKeys.suggestions(objectiveId) });
      onClose();
    },
    onError: (e) => flash(errText(e), true),
  });
  const needsPlan = agent === 'reconcile' && !objectiveId;

  return (
    <Modal label="Get suggestions" onClose={onClose}>
      <div className="pl-dlg">
        <h2 className="h3">Get suggestions</h2>
        <div className="pl-helperpick" role="radiogroup" aria-label="What to look for">
          {HELPERS.filter((h) => h.id === 'sweep' || objectiveId).map((h) => (
            <button key={h.id} type="button" role="radio" aria-checked={agent === h.id} className={'pl-helper' + (agent === h.id ? ' on' : '')} onClick={() => setAgent(h.id)}>
              <span className="pl-helpername">{h.label}</span>
              <span className="muted">{h.blurb}</span>
            </button>
          ))}
        </div>
        <div className="field">
          <label className="field-label" htmlFor="pl-find-machine">Machine</label>
          <select id="pl-find-machine" className="select" value={origin} onChange={(e) => setOrigin(e.target.value)} disabled={opts.isPending}>
            <option value="">{opts.isPending ? 'Loading machines…' : 'Choose a machine…'}</option>
            {options.map((o) => (
              <option key={o.id} value={o.id} disabled={o.disabled}>{o.label || o.name || o.id}{o.reachable === false ? ' (offline)' : ''}</option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="pl-find-scope">Folder (optional)</label>
          <input id="pl-find-scope" className="input" value={scope} onChange={(e) => setScope(e.target.value)} placeholder="e.g. docs/" spellCheck={false} />
        </div>
        <div className="pl-dlgactions">
          <Btn variant="primary" disabled={!origin || needsPlan || run.isPending} onClick={() => run.mutate()}>{run.isPending ? 'Starting…' : 'Start looking'}</Btn>
          <Btn onClick={onClose}>Cancel</Btn>
        </div>
      </div>
    </Modal>
  );
}
