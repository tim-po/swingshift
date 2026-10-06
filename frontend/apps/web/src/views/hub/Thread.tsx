// "Ask about this plan" — a durable conversation docked to a plan, answered by a
// session the user attaches (their own compute). Replies only ever come from the
// session's real returned turn; nothing here invents one. Mounted by the Plans
// side panel; nothing is attached or sent until the user clicks.
import { Link } from 'react-router-dom';
import { useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  emptyTurnText,
  idleLabel,
  pendingTurnLabel,
  threadActionChip,
  threadPending,
  threadSessionChoices,
  threadStateMeta,
  type ThreadMessage,
  type ThreadResponse,
} from '@loopyard/api';
import { Btn } from '../../components/ui';
import { errText } from './flash';
import type { LiveMode } from './liveThread';
import { hub, hubKeys, useHubSessions, useThread, useThreadLive } from './queries';
import './hub.css';

const LIVE_META: Record<LiveMode, { cls: string; label: string; tip: string }> = {
  sse: { cls: 'lv-on', label: 'live', tip: 'Replies appear here the moment the session returns them.' },
  poll: { cls: 'lv-on', label: 'live · poll', tip: 'Live (via polling) — replies still appear as soon as they land.' },
  connecting: { cls: 'lv-wait', label: 'connecting…', tip: 'Connecting to this conversation.' },
  down: { cls: 'lv-down', label: 'not live', tip: 'Can’t reach the conversation right now — showing the last copy; it retries on its own.' },
};

function Msg({ m, session }: { m: ThreadMessage; session: string }) {
  if (m.role === 'you')
    return (
      <div className="hb-msg you">
        <span className="hb-who">You</span>
        <span className="hb-text">{m.text}</span>
        {m.dispatched === false && <span className="hb-held" title="Kept, but not sent — attach a live session to send it">not sent</span>}
      </div>
    );
  const pending = m.status === 'pending';
  const failed = m.status === 'failed';
  const canceled = m.status === 'canceled';
  const acts = (m.actions ?? []).map(threadActionChip);
  const pl = pending ? pendingTurnLabel(m, session) : null;
  return (
    <div className={'hb-msg agent' + (failed ? ' failed' : '') + (canceled ? ' canceled' : '')} aria-busy={pending || undefined}>
      <span className="hb-who">Assistant</span>
      {pl ? (
        <span className={'hb-pending ' + pl.cls} role="status">
          {pl.text} <span className="hb-dots"><i /><i /><i /></span>
        </span>
      ) : m.text ? (
        <span className="hb-text">{m.text}</span>
      ) : (
        <span className="hb-text hb-none">{emptyTurnText(m.status)}</span>
      )}
      {acts.length > 0 && (
        <div className="hb-acts">
          {acts.map((a, i) => <span key={i} className="hb-act" title={a.type}>{a.label}</span>)}
        </div>
      )}
    </div>
  );
}

export function Thread({ docId, flash }: { docId: string; flash(t: string, err?: boolean): void }) {
  const qc = useQueryClient();
  const live = useThreadLive(docId);
  const pushed = live.mode === 'sse' || live.mode === 'poll';
  const { data: th } = useThread(docId, pushed);
  // Old servers don't put the session roster on the thread payload — fall back.
  const { data: sess } = useHubSessions(!!th && !Array.isArray(th.sessions));
  const [input, setInput] = useState('');
  const [pick, setPick] = useState('');

  // A turn just landed (pending → done): the session may have edited the plan,
  // filed an issue or pointed a loop — refresh what it could have touched.
  const wasPending = useRef(false);
  const pendingNow = threadPending(th);
  useEffect(() => {
    if (wasPending.current && !pendingNow) {
      qc.invalidateQueries({ queryKey: hubKeys.doc(docId) });
      qc.invalidateQueries({ queryKey: hubKeys.docs });
      qc.invalidateQueries({ queryKey: ['issues'] });
      qc.invalidateQueries({ queryKey: ['ws'] }); // the plan + its notes
    }
    wasPending.current = pendingNow;
  }, [pendingNow, docId, qc]);

  const st = th?.session_state ?? { state: 'unattached', can_send: false, reason: 'Loading the conversation…' };
  const meta = threadStateMeta(st.state);
  const session = st.session || '';
  const canSend = !!st.can_send;
  const choices = threadSessionChoices(th, sess?.sessions);
  const lm = LIVE_META[live.mode];
  const liveChoices = choices.filter((o) => o.live);
  // One live session → preselect it (attaching still needs the click).
  const only = liveChoices.length === 1 ? liveChoices[0].id : '';
  useEffect(() => setPick(session || only), [session, only]);
  const noneLive = !session && !liveChoices.length;

  const setThread = (r: ThreadResponse) => qc.setQueryData(hubKeys.thread(docId), r);
  const attach = useMutation({
    mutationFn: (s: string) => hub.attach(docId, s),
    onSuccess: (r, s) => {
      setThread(r);
      const n = r.canceled_tasks?.length ?? 0;
      const dropped = n ? ` ${n} queued turn${n > 1 ? 's' : ''} the old session never picked up ${n > 1 ? 'were' : 'was'} canceled.` : '';
      if (!s) flash('Detached — no session is answering now.' + dropped);
      else flash((r.session_state?.state === 'offline' ? `Attached ${s} — it’s offline, so it will answer once it reconnects.` : `Attached — the conversation now runs on ${s}.`) + dropped);
    },
    onError: (e) => flash(errText(e), true),
  });
  const send = useMutation({
    mutationFn: (text: string) => hub.post(docId, text),
    onSuccess: (r) => {
      setThread(r);
      setInput('');
      if (r.dispatched) flash(`Sent — ${r.session_state?.session || 'the session'} will reply here.`);
      else flash('Kept your message — attach a live session to send it.', true);
    },
    onError: (e) => flash(errText(e), true),
  });

  const doSend = () => {
    const text = input.trim();
    if (!text) return flash('Type a question first.', true);
    send.mutate(text);
  };
  const doAttach = () => {
    if (!pick) return flash('Pick a live session first.', true);
    if (pick === session) return flash(`Already answered by ${session}.`);
    attach.mutate(pick);
  };
  const busy = attach.isPending;
  const msgs = th?.thread?.messages ?? [];

  return (
    <section className="hb-thread" aria-label="Ask about this plan">
      <div className="hb-byline">
        <span className={'hb-state ' + meta.cls} title={st.reason || ''}>{session ? meta.label : 'no session'}</span>
        <span className={'hb-live ' + lm.cls} title={live.mode === 'down' && live.why ? `${lm.tip} (${live.why})` : lm.tip} data-live={live.mode}>
          {lm.label}
        </span>
      </div>
      {noneLive ? (
        <p className="hb-noconn">
          To ask questions, connect Claude or Codex on one of your machines as a session.{' '}
          <Link to="/machines/sessions">How to connect →</Link>
        </p>
      ) : (
      <div className="hb-attach">
        <select className="select hb-sel" value={pick} disabled={busy} onChange={(e) => setPick(e.target.value)} aria-label="Backing session"
          title="The session (your own Claude or Codex) that reads this plan and answers">
          <option value="">Choose a session…</option>
          {choices.map((o) => (
            <option key={o.id} value={o.id} disabled={!o.live && !o.attached}>
              {(o.label && o.label !== o.id ? `${o.label} (${o.id})` : o.id) +
                ' · ' +
                (o.live ? o.runtime || 'claude' : o.registered === false ? 'not registered' : ['offline', idleLabel(o.idleSeconds)].filter(Boolean).join(' '))}
            </option>
          ))}
        </select>
        <Btn disabled={busy} onClick={doAttach}>{session ? 'Switch' : 'Use this session'}</Btn>
        {session && <Btn variant="quiet" disabled={busy} onClick={() => attach.mutate('')}>Detach</Btn>}
      </div>
      )}
      <div className="hb-msgs">
        {msgs.length ? (
          msgs.map((m, i) => <Msg key={m.id || i} m={m} session={session} />)
        ) : (
          <p className="hb-thempty">
            {session
              ? 'Ask anything about this plan. The assistant reads it and can edit it, file an issue, or start a loop.'
              : 'Pick the session that should answer — it reads this plan and can edit it, file an issue, or start a loop.'}
          </p>
        )}
      </div>
      <div className="hb-compose">
        <textarea
          className="textarea hb-thin"
          rows={3}
          value={input}
          disabled={!canSend}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) doSend(); }}
          placeholder={canSend ? 'Ask about this plan…' : 'Choose a session above to ask.'}
          aria-label="Ask about this plan"
        />
        <div className="hb-bar">
          <Btn disabled={!canSend || send.isPending} onClick={doSend} title="⌘/Ctrl + Enter">Send</Btn>
        </div>
      </div>
    </section>
  );
}
