// /plans/:oid[/:slug] — one plan: its doc to read/edit, a status, the primary
// "Run as loop" (behind a confirmation), the loops pointed at it, and — collapsed
// by default — "Ask about this plan" in a side panel. Suggestions stay tucked away
// unless something is pending or Show everything is on.
import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  ago,
  honestState,
  objectiveThreadDocId,
  planLoops,
  planOutcome,
  planStatusLabel,
  turnsLabel,
  WS_DOC_STATUSES,
  WS_IH_INDEX_SLUG,
  type WsObjectiveDetail,
  type WsStatus,
} from '@loopyard/api';
import { Badge, Btn, Empty, friendlyError, Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { useLoops } from '../../api';
import { useShowAll } from '../../shell/disclosure';
import { errText } from '../hub/flash';
import { hub } from '../hub/queries';
import { Thread } from '../hub/Thread';
import { DocPane } from './DocPane';
import { loopRoute, planRoute, useDoc, useSuggestions, useToDiscuss, ws, wsKeys } from './queries';
import { RunLoopDialog } from './RunLoopDialog';
import { FindSuggestionsDialog, HelperRuns, ProgressSplit, SavedForLater, SuggestionCard } from './Suggestions';

type Flash = (t: string, err?: boolean) => void;

/** One status control for the plan. A one-note plan sets that note's status; a
 *  multi-note plan pins the plan's status (with a way back to "from its notes"). */
function StatusControl({ plan, flash }: { plan: WsObjectiveDetail; flash: Flash }) {
  const qc = useQueryClient();
  const single = plan.docs.length === 1 ? plan.docs[0] : null;
  const set = useMutation({
    mutationFn: async (v: string) => {
      if (v === 'auto') return ws.overrideStatus(plan.id, null);
      const s = v as WsStatus;
      if (single) {
        await ws.setDocStatus(plan.id, single.slug, s);
        if (plan.overridden) await ws.overrideStatus(plan.id, null);
        return;
      }
      return ws.overrideStatus(plan.id, s);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: wsKeys.objective(plan.id) });
      qc.invalidateQueries({ queryKey: wsKeys.objectives });
      if (single) qc.invalidateQueries({ queryKey: wsKeys.doc(plan.id, single.slug) });
    },
    onError: (e) => flash(errText(e), true),
  });
  return (
    <span className="pl-status" title={plan.overridden ? `Set by you (its notes say ${planStatusLabel(plan.rollup)})` : 'From its notes'}>
      <select className="select" value={plan.status} disabled={set.isPending} onChange={(e) => set.mutate(e.target.value)} aria-label="Plan status">
        {WS_DOC_STATUSES.map((s) => <option key={s} value={s}>{planStatusLabel(s)}</option>)}
        {plan.overridden && !single && <option value="auto">Use its notes ({planStatusLabel(plan.rollup)})</option>}
      </select>
    </span>
  );
}

/** Hub plans only: the owner's explicit result — Mark done (a green result) or
 *  Reopen (clears it). A loop can never do this; only a finished-green loop or you. */
function DoneControl({ plan, docId, flash }: { plan: WsObjectiveDetail; docId: string; flash: Flash }) {
  const qc = useQueryClient();
  const outcome = planOutcome(plan);
  const done = !!outcome?.done;
  const set = useMutation({
    mutationFn: () => hub.update({ id: docId, result: done ? 'clear' : 'set' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: wsKeys.objective(plan.id) });
      qc.invalidateQueries({ queryKey: wsKeys.objectives });
      flash(done ? 'Reopened' : 'Marked done');
    },
    onError: (e) => flash(errText(e), true),
  });
  return (
    <>
      {outcome && (
        <span className={'pl-outcome' + (done ? ' done' : ' ended')} title={outcome.label}>
          {outcome.glyph} <span className="muted">{outcome.label}</span>
        </span>
      )}
      <button type="button" className="btn quiet" disabled={set.isPending} onClick={() => set.mutate()}
        title={done ? 'Clear the recorded result — the plan is open again' : 'Record this plan as done'}>
        <Icon name={done ? 'refresh' : 'check'} /> {done ? 'Reopen' : 'Mark done'}
      </button>
    </>
  );
}

function LoopsOnPlan({ plan }: { plan: WsObjectiveDetail }) {
  const navigate = useNavigate();
  const loops = useLoops().data?.loops;
  const rows = planLoops(plan, loops);
  return (
    <section className="section" aria-label="Loops on this plan">
      <h2 className="section-h">Loops on this plan {rows.length > 0 && <span className="count">{rows.length}</span>}</h2>
      {rows.length ? (
        <div className="rows">
          {rows.map(({ name, loop }) =>
            loop ? (
              <button key={name} type="button" className="row" onClick={() => navigate(loopRoute(loop.name, loop.host))}>
                <Badge state={honestState(loop)} />
                <span className="mono pl-loopname">{loop.name}</span>
                <span className="grow muted pl-loopresult" title={loop.summary || undefined}>
                  {[turnsLabel(loop), loop.summary].filter(Boolean).join(' · ')}
                </span>
                <span className="end">{ago(loop.updated ?? loop.started)}</span>
              </button>
            ) : (
              <div key={name} className="row pl-loopgone" title="This loop isn’t listed right now — it may have been deleted, or its machine is offline.">
                <Badge state="saved">not listed</Badge>
                <span className="mono pl-loopname">{name}</span>
              </div>
            ),
          )}
        </div>
      ) : (
        <p className="muted pl-quiet">No loops yet. <b>Run as loop</b> starts one and links it here.</p>
      )}
    </section>
  );
}

function Suggestions({ plan, flash }: { plan: WsObjectiveDetail; flash: Flash }) {
  const navigate = useNavigate();
  const [showAll] = useShowAll();
  const [findOpen, setFindOpen] = useState(false);
  const sugs = useSuggestions(plan.id).data?.suggestions;
  const later = useToDiscuss(plan.id).data?.items ?? [];
  const pending = (sugs ?? []).filter((s) => s.state === 'pending');
  if (!pending.length && !later.length && !showAll) return null;
  return (
    <section className="section pl-suggestions" aria-label="Suggestions">
      <h2 className="section-h">
        Suggestions {pending.length > 0 && <span className="count">{pending.length}</span>}
        {showAll && (
          <button type="button" className="btn quiet sm spacer" onClick={() => setFindOpen(true)} title="Run a helper (Reconcile) over a machine's commits">
            Get suggestions…
          </button>
        )}
      </h2>
      <HelperRuns navigate={navigate} />
      <ProgressSplit suggestions={sugs} />
      {pending.map((s) => <SuggestionCard key={s.id} oid={plan.id} sug={s} flash={flash} />)}
      {!pending.length && <p className="muted pl-quiet">No suggestions right now.</p>}
      <SavedForLater items={later} />
      {findOpen && <FindSuggestionsDialog objectiveId={plan.id} onClose={() => setFindOpen(false)} flash={flash} />}
    </section>
  );
}

export function PlanDetail({ plan, slug, flash }: { plan: WsObjectiveDetail; slug?: string; flash: Flash }) {
  const navigate = useNavigate();
  const location = useLocation();
  const qc = useQueryClient();
  const [runOpen, setRunOpen] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);

  const docs = plan.docs;
  const current = docs.find((d) => d.slug === slug) ?? docs.find((d) => d.slug === WS_IH_INDEX_SLUG) ?? docs[0];
  const doc = useDoc(plan.id, current?.slug);
  const threadDoc = objectiveThreadDocId(plan);
  const fromHub = plan.source === 'ideahub';
  const multi = docs.length > 1;
  const startEditing = !!(location.state as { edit?: boolean } | null)?.edit;

  const addNote = useMutation({
    mutationFn: (first: boolean) => ws.saveDoc({ oid: plan.id, title: first ? plan.title : 'New note', body: '', status: first ? 'draft' : undefined }),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: wsKeys.objective(plan.id) });
      qc.invalidateQueries({ queryKey: wsKeys.objectives });
      if (r.doc?.slug) navigate(planRoute(plan.id, r.doc.slug), { state: { edit: true } });
    },
    onError: (e) => flash(errText(e), true),
  });

  const meta = [
    plan.project ? `Project ${plan.project}` : null,
    multi ? `${docs.length} notes` : null,
    plan.updated ? `updated ${ago(plan.updated)}` : null,
  ].filter(Boolean);

  return (
    <div className={'page pl-page pl-detail' + (chatOpen ? ' with-panel' : '')}>
      <button type="button" className="btn quiet sm pl-back" onClick={() => navigate('/plans')}>
        <Icon name="chevronLeft" /> All plans
      </button>
      <header className="pagehead pl-head">
        <div className="pl-headtext">
          <h1 className="h1">{plan.title}</h1>
          {meta.length > 0 && <div className="sub">{meta.join(' · ')}</div>}
        </div>
        <div className="pageactions">
          <StatusControl plan={plan} flash={flash} />
          {fromHub && threadDoc && <DoneControl plan={plan} docId={threadDoc} flash={flash} />}
          {threadDoc && (
            <button type="button" className={'btn' + (chatOpen ? '' : ' quiet')} aria-expanded={chatOpen} aria-controls="pl-askpanel" onClick={() => setChatOpen((v) => !v)}>
              <Icon name="chat" /> Ask about this plan
            </button>
          )}
          <Btn variant="primary" onClick={() => setRunOpen(true)}><Icon name="play" /> Run as loop</Btn>
        </div>
      </header>

      <div className="pl-detailbody">
        <div className="pl-main">
          {!current ? (
            <Empty title="This plan is empty">
              <p>Write down what you want done — a loop will work from it.</p>
              <Btn disabled={addNote.isPending} onClick={() => addNote.mutate(true)}><Icon name="edit" /> Write the plan</Btn>
            </Empty>
          ) : doc.isPending ? (
            <Skeleton block={160} lines={3} />
          ) : doc.error || !doc.data?.doc ? (
            <Empty tone="err" title={doc.error ? friendlyError(doc.error, 'this note').title : 'Note not found'}>
              <p>{doc.error ? friendlyError(doc.error, 'this note').detail : 'It may have been moved or deleted.'}</p>
              <Btn onClick={() => doc.refetch()}>Try again</Btn>
            </Empty>
          ) : (
            <DocPane
              key={current.slug}
              oid={plan.id}
              doc={doc.data.doc}
              flash={flash}
              showTitle={multi}
              titleEditable={multi || fromHub}
              showStatus={multi}
              startEditing={startEditing && current.slug === slug}
            />
          )}

          {current && (
            <section className="pl-notes" aria-label="Notes in this plan">
              {multi && (
                <ul className="pl-notelist">
                  {docs.map((d) => (
                    <li key={d.slug}>
                      <button
                        type="button"
                        className={'pl-note' + (d.slug === current.slug ? ' on' : '')}
                        aria-current={d.slug === current.slug || undefined}
                        onClick={() => navigate(planRoute(plan.id, d.slug))}
                      >
                        <Icon name="files" size={14} />
                        <span className="grow">{d.title || d.slug}</span>
                        <span className="muted">{planStatusLabel(d.status)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <button type="button" className="btn quiet sm" disabled={addNote.isPending} onClick={() => addNote.mutate(false)}>
                <Icon name="plus" /> Add a note
              </button>
            </section>
          )}

          <LoopsOnPlan plan={plan} />
          <Suggestions plan={plan} flash={flash} />
        </div>

        {threadDoc && chatOpen && (
          <aside id="pl-askpanel" className="pl-panel" aria-label="Ask about this plan panel">
            <div className="pl-panelhead">
              <h2 className="h3">Ask about this plan</h2>
              <button type="button" className="pl-x" aria-label="Close" onClick={() => setChatOpen(false)}><Icon name="x" /></button>
            </div>
            <Thread key={threadDoc} docId={threadDoc} flash={flash} />
          </aside>
        )}
      </div>

      {runOpen && <RunLoopDialog oid={plan.id} onClose={() => setRunOpen(false)} flash={flash} />}
    </div>
  );
}

/** Loading / error shell around a plan. */
export function PlanDetailState({ isPending, error, retry }: { isPending: boolean; error: unknown; retry(): void }) {
  const navigate = useNavigate();
  if (isPending) return <div className="page pl-page"><Skeleton block={240} lines={3} /></div>;
  const fe = error ? friendlyError(error, 'this plan') : null;
  return (
    <div className="page pl-page">
      <Empty tone="err" title={fe ? fe.title : 'Plan not found'}>
        <p>{fe ? fe.detail : 'It may have been moved or deleted.'}</p>
        <div className="eb-actions">
          {fe && <Btn variant="primary" onClick={retry}>Try again</Btn>}
          <Btn onClick={() => navigate('/plans')}>All plans</Btn>
        </div>
      </Empty>
    </div>
  );
}
