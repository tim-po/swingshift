// Machines → Sessions: AI apps and terminals connected to Loopyard. A flat
// roster like the All tab; "Connect a session" opens the one-time-link dialog.
import { useMemo, useState } from 'react';
import { sessionCountLabel } from '@loopyard/api';
import { Btn, Empty, QueryState } from '../../components/ui';
import { Icon } from '../../components/icons';
import { Intro } from '../../components/Intro';
import { Modal } from '../../shell/Modal';
import { MachinesTabPage } from '../machines/TabPage';
import { useSessions } from './api';
import { SessionRow } from './SessionRow';
import { AttachPanel } from './AttachPanel';
import './sessions.css';

export default function SessionsView() {
  const { data, error, isPending, refetch } = useSessions();
  const [connecting, setConnecting] = useState(false);
  // Online first, then by name.
  const list = useMemo(
    () => [...(data?.sessions ?? [])].sort((a, b) => Number(!!b.heartbeat?.live) - Number(!!a.heartbeat?.live) || a.id.localeCompare(b.id)),
    [data],
  );
  const connect = () => setConnecting(true);

  return (
    <MachinesTabPage
      title="Sessions"
      sub={list.length || (!isPending && !error) ? <span>{sessionCountLabel(list)}</span> : undefined}
      actions={
        <Btn variant="primary" onClick={connect}>
          <Icon name="plus" /> Connect a session
        </Btn>
      }
    >
      <Intro id="sessions">
        Sessions are AI apps and terminals, like Claude, connected to Swingshift. Send one a task, or start a loop that it works on.
      </Intro>
      {data?.error && list.length > 0 && <p className="ss-warn">Some sessions couldn't be read just now. Showing the rest.</p>}
      {list.length ? (
        <div className="rows ss-list">{list.map((s) => <SessionRow key={s.id} s={s} />)}</div>
      ) : isPending || error ? (
        <QueryState isPending={isPending} error={error} what="sessions" onRetry={() => refetch()} />
      ) : (
        <Empty title="No sessions yet">
          <p>Connect your AI (Claude, ChatGPT…) and it shows up here, ready to take tasks and run loops.</p>
          <Btn variant="primary" onClick={connect}>Connect a session</Btn>
        </Empty>
      )}
      {connecting && (
        <Modal label="Connect a session" onClose={() => setConnecting(false)}>
          <h2 className="h2 ss-mh">Connect a session</h2>
          <p className="ss-lead">Your AI connects itself from a one-time link. Nothing is shared until you make one.</p>
          <AttachPanel />
          <div className="ss-mactions">
            <Btn onClick={() => setConnecting(false)}>Done</Btn>
          </div>
        </Modal>
      )}
    </MachinesTabPage>
  );
}
