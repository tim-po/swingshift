import { useMemo, useState } from 'react';
import {
  ago, deviceCause, deviceKindLabel, deviceOwners, machineCapsLabel, machineCountLabel, machineKindLabel,
  machineSeenLabel, sortDevices, type Device,
} from '@loopyard/api';
import { Btn, Empty, QueryState } from '../../components/ui';
import { Icon } from '../../components/icons';
import { Intro } from '../../components/Intro';
import { Modal } from '../../shell/Modal';
import { MachinesTabPage } from '../machines/TabPage';
import { ConnectLinkPanel } from '../origins/ConnectLinkPanel';
import { AttachPanel } from '../sessions/AttachPanel';
import '../origins/origins.css';
import '../sessions/sessions.css';
import { useDevices } from './api';
import './devices.css';

const ALL_OWNERS = '';

export default function DevicesView() {
  const [ownerPick, setOwner] = useState<string>(ALL_OWNERS);
  const [enrolling, setEnrolling] = useState(false);
  // The unscoped read feeds the owner picker; picking an owner switches to the
  // server-scoped ?owner= read. The client-side filter stays as a guard so the
  // scope holds even against a backend that ignores ?owner=.
  const unscoped = useDevices();
  const owners = useMemo(() => deviceOwners(unscoped.data?.devices ?? []), [unscoped.data]);
  const owner = owners.length > 1 && owners.includes(ownerPick) ? ownerPick : ALL_OWNERS;
  const scoped = useDevices(owner || undefined);
  const { data, error, isPending, refetch } = owner ? scoped : unscoped;
  const list = useMemo(() => {
    const all = data?.devices ?? [];
    return sortDevices(owner === ALL_OWNERS ? all : all.filter((d) => d.owner === owner));
  }, [data, owner]);

  let body: React.ReactNode = <QueryState isPending={isPending} error={error} what="machines" onRetry={() => refetch()} />;
  if (!isPending && !error) {
    body = list.length ? (
      <div className="rows dv-list">
        {list.map((d) => <MachineRow key={d.id} d={d} />)}
      </div>
    ) : data?.error ? (
      <Empty tone="err" title="Couldn't load machines"><p>Something went wrong reading the list. This is usually temporary.</p><Btn variant="primary" onClick={() => refetch()}>Try again</Btn></Empty>
    ) : (
      <Empty title="No machines yet">
        <p>Connect the computer you want loops to run on. It shows up here a few seconds later.</p>
        <Btn variant="primary" onClick={() => setEnrolling(true)}>Connect a machine</Btn>
      </Empty>
    );
  }

  return (
    <MachinesTabPage
      title="Machines"
      sub={
        <>
          {!isPending && !error && <span>{machineCountLabel(list)}</span>}
          {owners.length > 1 && (
            <label className="dv-owner" title="Show only one owner's machines">
              Owner
              <select className="select" value={owner} onChange={(e) => setOwner(e.target.value)} aria-label="Filter devices by owner">
                <option value={ALL_OWNERS}>Everyone</option>
                {owners.map((o) => (
                  <option key={o} value={o}>{o}</option>
                ))}
              </select>
            </label>
          )}
        </>
      }
      actions={
        <Btn variant="primary" onClick={() => setEnrolling(true)}>
          <Icon name="plus" /> Connect a machine
        </Btn>
      }
    >
      <Intro id="devices">
        Every computer and app connected to Swingshift, in one list. A machine is <b>online</b> while it's connected; open a row for its technical details.
      </Intro>
      {body}
      {enrolling && <EnrollModal onClose={() => setEnrolling(false)} />}
    </MachinesTabPage>
  );
}

function MachineRow({ d }: { d: Device }) {
  const caps = machineCapsLabel(d);
  const seen = machineSeenLabel(d, (t) => ago(t));
  return (
    <details className={'dv-row' + (d.live ? ' live' : '')}>
      <summary className="dv-sum">
        <span className={'dv-dot' + (d.live ? ' on' : '')} aria-hidden="true" />
        <span className="dv-main">
          <span className="dv-name" title={d.name || d.id}>{d.name || d.id}</span>
          <span className="dv-kind">
            {machineKindLabel(d)}
            {caps && <span className="dotsep" title="What this machine can run">{caps}</span>}
          </span>
        </span>
        <span className={'dv-seen' + (d.live ? ' on' : '')} title={deviceCause(d, (t) => ago(t)) || 'Connected right now'}>{seen}</span>
        <Icon name="chevronDown" size={14} className="dv-chev" />
      </summary>
      <dl className="dv-details">
        <dt>Type</dt><dd>{deviceKindLabel(d)}</dd>
        <dt>Id</dt><dd className="mono">{d.id}</dd>
        {d.address_free && (<><dt>Address</dt><dd className="mono" title="A label, not a network address — Swingshift reaches it through the hub">{d.address_free}</dd></>)}
        <dt>Owner</dt><dd>{d.owner}</dd>
        <dt>Capabilities</dt><dd>{caps || 'None declared'}</dd>
        {d.channel_ref && (<><dt>Channel</dt><dd className="mono">{d.channel_ref}</dd></>)}
      </dl>
    </details>
  );
}

/** The same terminal command the Computers tab shows. */
export const CONNECT_CMD = 'yard connect <name>';

type Kind = 'computer' | 'session';

const KINDS: Array<{ id: Kind; icon: string; title: string; hint: string }> = [
  { id: 'computer', icon: 'machine', title: 'A computer', hint: 'Runs loops. Your laptop, a server, a spare box.' },
  { id: 'session', icon: 'chat', title: 'An AI app or terminal', hint: 'Claude, ChatGPT or a terminal session running one. Takes tasks.' },
];

// Connect a machine: pick what you're adding, then the same flow its tab uses
// (Computers → one-time link; Sessions → attach link). Choosing never mints a
// link — only the panel's own Generate button does.
function EnrollModal({ onClose }: { onClose(): void }) {
  const [kind, setKind] = useState<Kind | null>(null);
  return (
    <Modal label="Connect a machine" onClose={onClose}>
      <h2 className="h2 dv-mh">{kind === 'computer' ? 'Connect a computer' : kind === 'session' ? 'Connect an AI app or terminal' : 'Connect a machine'}</h2>
      {!kind ? (
        <>
          <p className="dv-lead">What are you adding?</p>
          <div className="dv-kinds">
            {KINDS.map((k) => (
              <button key={k.id} type="button" className="dv-kind-opt" onClick={() => setKind(k.id)}>
                <Icon name={k.icon} size={18} />
                <span className="dv-kind-text">
                  <span className="dv-kind-title">{k.title}</span>
                  <span className="dv-kind-hint">{k.hint}</span>
                </span>
                <Icon name="chevronRight" size={14} className="dv-kind-chev" />
              </button>
            ))}
          </div>
        </>
      ) : kind === 'computer' ? (
        <>
          <p className="dv-lead">Your AI can do the setup for you. Nothing is shared until you make a link.</p>
          <ConnectLinkPanel />
          <TerminalHint />
        </>
      ) : (
        <>
          <p className="dv-lead">Your AI connects itself from a one-time link. Nothing is shared until you make one.</p>
          <AttachPanel />
        </>
      )}
      <div className="dv-actions">
        {kind && <Btn variant="quiet" onClick={() => setKind(null)}><Icon name="chevronLeft" /> Back</Btn>}
        <Btn onClick={onClose}>Done</Btn>
      </div>
    </Modal>
  );
}

function TerminalHint() {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(CONNECT_CMD);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard denied — the command stays selectable */
    }
  };
  return (
    <details className="dv-how">
      <summary>Prefer a terminal?</summary>
      <p>Run this on the computer you want to add, with a name for it:</p>
      <div className="dv-cmd">
        <code>{CONNECT_CMD}</code>
        <button type="button" className="btn sm" onClick={copy} aria-label="Copy the yard connect command">
          <Icon name={copied ? 'check' : 'copy'} /> {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <p>
        If Swingshift can't reach it yet, add <code className="mono">--address user@host</code>. It shows up in the list a few seconds after it
        connects. You never type a password or key here.
      </p>
    </details>
  );
}
