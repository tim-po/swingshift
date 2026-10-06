// One connected session in the Sessions roster: status dot, plain kind, what
// it's doing now, and "Online"/"Last seen". Opening the row shows its actions
// (send a task / open its terminal / start a loop) and the technical details.
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ago, sessionActivityLabel, sessionCreatedLabel, sessionKind, sessionKindLabel, sessionSeenLabel, type Session,
} from '@loopyard/api';
import { Btn, friendlyLine } from '../../components/ui';
import { Icon } from '../../components/icons';
import { useDispatch } from './api';

export function SessionRow({ s }: { s: Session }) {
  const navigate = useNavigate();
  const dispatch = useDispatch();
  const [sendOn, setSendOn] = useState(false);
  const [text, setText] = useState('');
  const [msg, setMsg] = useState<{ ok: boolean; text: string; tip?: string } | null>(null);

  const hb = s.heartbeat ?? {};
  const live = !!hb.live;
  const kind = sessionKind(s);
  const runtime = s.runtime || 'claude';
  const made = sessionCreatedLabel(s.created);
  const o = s.origin;

  const send = () => {
    const t = text.trim();
    if (!t) return setMsg({ ok: false, text: 'Write the task first.' });
    setMsg(null);
    dispatch.mutate(
      { session: s.id, text: t },
      {
        onSuccess: (r) => {
          setSendOn(false);
          setText('');
          setMsg({ ok: true, text: `Sent. ${s.id} picks it up the next time it checks in.`, tip: r.task_id ? `Task ${r.task_id}` : undefined });
        },
        onError: (e) => setMsg({ ok: false, text: friendlyLine(e, 'send that task') }),
      },
    );
  };

  const startLoop = () =>
    navigate(`/newloop?origin=${encodeURIComponent('session:' + s.id)}&runtime=${encodeURIComponent(runtime)}`);

  return (
    <details className={'ss-row' + (live ? ' live' : '')}>
      <summary className="ss-sum">
        <span className={'ss-dot' + (live ? ' on' : '')} aria-hidden="true" />
        <span className="ss-main">
          <span className="ss-name" title={s.id}>{s.id}</span>
          <span className="ss-kind">
            <span>{sessionKindLabel(s)}</span>
            {o?.host && <span className="dotsep">on {o.host}</span>}
          </span>
          {s.doingNow && <span className="ss-doing" title={s.doingNow}>{s.doingNow}</span>}
        </span>
        <span className={'ss-seen' + (live ? ' on' : '')} title={hb.state ? `Heartbeat: ${hb.state}` : undefined}>
          {sessionSeenLabel(s, (t) => ago(t))}
        </span>
        <Icon name="chevronDown" size={14} className="ss-chev" />
      </summary>
      <div className="ss-body">
        <div className="ss-verbs">
          {kind === 'cli' ? (
            <Btn size="sm" onClick={() => navigate(`/terminal?target=${encodeURIComponent(s.id)}`)} title="Open this session's terminal in the browser">
              <Icon name="terminal" /> Open terminal
            </Btn>
          ) : (
            <Btn
              size="sm"
              disabled={!live}
              aria-expanded={sendOn}
              onClick={() => setSendOn((v) => !v)}
              title={live ? 'Give this session a task. It runs on its own computer.' : 'Offline. You can send tasks once it reconnects.'}
            >
              <Icon name="send" /> Send a task
            </Btn>
          )}
          <Btn size="sm" onClick={startLoop} title="Open the new-loop form with this session doing the work">
            <Icon name="loop" /> Start a loop here
          </Btn>
        </div>

        {kind === 'app' && sendOn && (
          <div className="ss-send">
            <textarea
              className="textarea"
              autoFocus
              rows={2}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={`What should ${s.id} do?`}
              aria-label={`Task for ${s.id}`}
            />
            <div className="ss-sendbar">
              <Btn size="sm" onClick={send} disabled={dispatch.isPending}>{dispatch.isPending ? 'Sending…' : 'Send'}</Btn>
              <Btn size="sm" variant="quiet" onClick={() => setSendOn(false)}>Cancel</Btn>
            </div>
          </div>
        )}
        {msg && <p className={'ss-msg' + (msg.ok ? ' ok' : ' err')} role="status" title={msg.tip}>{msg.text}</p>}

        <dl className="ss-details">
          <dt>Doing now</dt><dd>{s.doingNow || 'Nothing right now'}</dd>
          {s.activity && (<><dt>Tasks</dt><dd>{sessionActivityLabel(s.activity)}</dd></>)}
          <dt>Created</dt><dd>{made || 'Nothing yet'}</dd>
          <dt>Runs on</dt><dd>{o?.host || 'Not reported'}</dd>
          {o?.cwd && (<><dt>Folder</dt><dd className="mono">{o.cwd}</dd></>)}
          <dt>Id</dt><dd className="mono">{s.id}</dd>
          <dt>Type</dt>
          <dd title={kind === 'cli' ? 'Interactive tmux session on this computer: it can be opened' : 'App session: follow its activity and send it tasks'}>
            {kind === 'cli' ? 'Terminal (tmux) session' : 'App session'} · {runtime}
          </dd>
          {!!s.capabilities?.length && (<><dt>Capabilities</dt><dd>{s.capabilities.join(' · ')}</dd></>)}
        </dl>
      </div>
    </details>
  );
}
