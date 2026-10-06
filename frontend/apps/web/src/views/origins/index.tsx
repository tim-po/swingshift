// Machines → Computers: the computers loops run on (internally: origins).
// A flat roster like the All tab — status dot, plain kind, "Online"/"Last seen",
// and the technical details (ids, paths, sign-in state) behind each row.
// Connected sessions are origins too, but they live on the Sessions tab.
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQueries } from '@tanstack/react-query';
import {
  ago, cliName, cliSignInLabel, computerCountLabel, computerKindLabel, computerOnline, computerOrigins, computerSeenLabel,
  inProjectScope, isLocalOrigin, loopsForOrigin, originCaps, originCountLabel, originHealth,
  type LoopSummary, type Origin, type OriginCapsRecord,
} from '@loopyard/api';
import { Btn, Empty, QueryState } from '../../components/ui';
import { Icon } from '../../components/icons';
import { Intro } from '../../components/Intro';
import { useLoops } from '../../api';
import { origins as originsClient, useOrigins } from '../../scope/queries';
import { useProjectScope } from '../../scope';
import { useShowAll } from '../../shell/disclosure';
import { Modal } from '../../shell/Modal';
import { MachinesTabPage } from '../machines/TabPage';
import { capsEmptyLabel, capsSourceTip, capsToolsSummary } from './caps';
import { ConnectLinkPanel } from './ConnectLinkPanel';
import { HubCard, useHub } from './HubCard';
import './origins.css';

export { OriginPicker, useOriginPickerOptions } from './OriginPicker';

/** Loops listed per computer before "and N more". */
export const LOOPS_SHOWN = 3;

export default function OriginsView() {
  const { data, error, isPending, refetch } = useOrigins();
  const loops = useLoops().data?.loops ?? [];
  const { project, projectName } = useProjectScope();
  const [showAll] = useShowAll();
  const [adding, setAdding] = useState(false);
  const all = data?.origins ?? [];
  const list = computerOrigins(all);
  const sessions = all.length - list.length;

  // Read-only capability probe, only for computers whose list entry carries none.
  const missing = list.filter((o) => o.capabilities === undefined);
  const probes = useQueries({
    queries: missing.map((o) => ({
      queryKey: ['origin-caps', o.id],
      queryFn: () => originsClient.capabilities(o.id).catch((e: Error): OriginCapsRecord => ({ ok: false, cliCapabilities: [], reason: 'probe failed: ' + e.message })),
      staleTime: Infinity,
    })),
  });
  const probed = Object.fromEntries(missing.map((o, i) => [o.id, probes[i]?.data]));

  // Same rule as the Loops list: archived loops are hidden, so they are not counted here either.
  const scoped = loops.filter((l) => !l.archived && inProjectScope(l, project));
  const connect = () => setAdding(true);

  let body: React.ReactNode = <QueryState isPending={isPending} error={error} what="computers" onRetry={() => refetch()} />;
  if (!isPending && !error) {
    body = list.length ? (
      <div className="rows og-list">
        {list.map((o) => (
          <ComputerRow key={o.id} o={o} caps={originCaps(o, probed[o.id])} bound={loopsForOrigin(scoped, o.id)} scoped={!!project} showAll={showAll} />
        ))}
      </div>
    ) : data?.error ? (
      <Empty tone="err" title="Couldn't load computers"><p>Something went wrong reading the list. This is usually temporary.</p><Btn variant="primary" onClick={() => refetch()}>Try again</Btn></Empty>
    ) : (
      <Empty title="No computers yet">
        <p>This computer isn't reporting yet. Check that Swingshift is running here, or connect another computer.</p>
        <Btn variant="primary" onClick={connect}>Connect a computer</Btn>
      </Empty>
    );
  }

  return (
    <MachinesTabPage
      title="Computers"
      sub={
        !isPending && !error ? (
          <>
            <span title={showAll ? originCountLabel(all) : undefined}>{computerCountLabel(all)}</span>
            {project && (
              <span className="dotsep" title="Loops in the active project">
                {scoped.length} loop{scoped.length === 1 ? '' : 's'} in {projectName}
              </span>
            )}
          </>
        ) : undefined
      }
      actions={
        <Btn variant="primary" onClick={connect}>
          <Icon name="plus" /> Connect a computer
        </Btn>
      }
    >
      <Intro id="computers">
        The computers your loops run on. <b>This computer</b> is the one running this dashboard; connect more to run loops elsewhere.
      </Intro>
      <HubCard origins={list} onConnect={() => setAdding(true)} />
      {body}
      {sessions > 0 && (
        <p className="og-foot">
          AI apps and terminals connected to Swingshift are under <Link to="/machines/sessions">Sessions</Link>.
        </p>
      )}
      {adding && <ConnectModal onClose={() => setAdding(false)} />}
    </MachinesTabPage>
  );
}

function ComputerRow({ o, caps, bound, scoped, showAll }: { o: Origin; caps?: OriginCapsRecord; bound: LoopSummary[]; scoped: boolean; showAll: boolean }) {
  const online = computerOnline(o);
  const pending = !online && (o.health || '') === 'pending';
  const h = originHealth(o, (t) => ago(t));
  const kind = computerKindLabel(o);
  const tools = capsToolsSummary(caps?.cliCapabilities);
  const local = isLocalOrigin(o);
  const empty = caps && capsEmptyLabel(caps, local);
  const name = o.name || o.id;
  const n = bound.length;
  const loopsWord = `${n} loop${n === 1 ? '' : 's'}${scoped ? ' in this project' : ''}`;

  return (
    <details className={'og-row og-card' + (online ? ' live' : '') + (o.isHub ? ' hub' : '')}>
      <summary className="og-sum">
        <span className={'og-dot' + (online ? ' on' : pending ? ' wait' : '')} aria-hidden="true" />
        <span className="og-main">
          <span className="og-name" title={o.id}>
            {name}
            {showAll && o.id !== name && <span className="og-id mono">{o.id}</span>}
          </span>
          <span className="og-kind">
            <span title={kind.tip}>{kind.text}</span>
            {tools ? <span className="dotsep" title={capsSourceTip(caps, local)}>{tools}</span>
              : caps && !caps.ok && !local && <span className="dotsep" title={empty?.tip}>AI tools not checked</span>}
            <span className="dotsep">{loopsWord}</span>
          </span>
        </span>
        {o.isHub && <span className="og-hubtag" title="New origins join this machine's Hub">◈ Hub</span>}
        <span className={'og-seen' + (online ? ' on' : '')} title={h.cause || h.reachTip}>{computerSeenLabel(o, (t) => ago(t))}</span>
        <Icon name="chevronDown" size={14} className="og-chev" />
      </summary>
      <div className="og-body">
        <div className="og-loops">
          <span className="og-k">Loops</span>
          {n ? (
            <ul>
              {bound.slice(0, LOOPS_SHOWN).map((l) => (
                <li key={l.host + l.name}>
                  <Link to={`/loops/${encodeURIComponent(l.host)}/${encodeURIComponent(l.name)}`} title={`Open ${l.name}`}>{l.name}</Link>
                </li>
              ))}
              {n > LOOPS_SHOWN && (
                <li>
                  <Link className="og-more" to="/loops">and {n - LOOPS_SHOWN} more</Link>
                </li>
              )}
            </ul>
          ) : (
            <span className="og-none">No loops on this computer yet.</span>
          )}
        </div>
        <dl className="og-details">
          <dt>Type</dt><dd>{kind.tip}</dd>
          <dt>Id</dt><dd className="mono">{o.id}</dd>
          {o.path && (<><dt>Folder</dt><dd className="mono">{o.path}</dd></>)}
          {o.address && (<><dt>Address</dt><dd className="mono">{o.address}</dd></>)}
          <dt>AI tools</dt>
          <dd>
            {!caps ? 'Checking…'
              : caps.cliCapabilities.length ? caps.cliCapabilities.map((c, i) => (
                <span key={c.cli} className={i ? 'dotsep' : undefined} title={[c.note, c.remote && 'checked on that computer'].filter(Boolean).join(' · ') || undefined}>{cliName(c.cli)} ({cliSignInLabel(c)})</span>
              ))
              : <span title={empty?.tip || undefined}>{empty?.text}</span>}
          </dd>
          {o.last_seen ? (<><dt>Last sync</dt><dd>{ago(o.last_seen)}</dd></>) : null}
          {h.cause && (<><dt>Status</dt><dd>{h.cause}</dd></>)}
        </dl>
      </div>
    </details>
  );
}

const CMD = 'yard connect <name>';

function ConnectModal({ onClose }: { onClose(): void }) {
  const hub = useHub();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(CMD);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard denied — the command stays selectable */
    }
  };
  return (
    <Modal label="Connect a computer" onClose={onClose}>
      <h2 className="h2 og-mh">Connect a computer</h2>
      <p className="og-lead">Your AI can do the setup for you. Nothing is shared until you make a link.</p>
      <ConnectLinkPanel hub={hub.data} />
      <details className="og-how">
        <summary>Prefer a terminal?</summary>
        <p>Run this on the computer you want to add, with a name for it:</p>
        <div className="og-cmd">
          <code>{CMD}</code>
          <Btn size="sm" onClick={copy} aria-label="Copy the yard connect command">
            <Icon name={copied ? 'check' : 'copy'} /> {copied ? 'Copied' : 'Copy'}
          </Btn>
        </div>
        <p>
          If Swingshift can't reach it yet, add <code className="mono">--address user@host</code>. It shows up in the list a few seconds after it
          connects. You never type a password or key here.
        </p>
      </details>
      <div className="og-mactions">
        <Btn onClick={onClose}>Done</Btn>
      </div>
    </Modal>
  );
}
