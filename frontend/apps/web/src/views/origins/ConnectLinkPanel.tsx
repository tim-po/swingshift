// "Generate one-time link for your AI": mint a short single-use URL, show it with
// a paste hint + expiry countdown, poll until the origin connects, allow revoke.
// The link always points at the current Hub (hub.ts), so the owner never types a
// hub URL; when another machine is the Hub, links are made there instead. There is
// no hub-URL error path: a failed mint is one calm, retryable line.
import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Btn, friendlyLine } from '../../components/ui';
import { Icon } from '../../components/icons';
import {
  connectLink, countdown, effectiveState, isFinal, pasteHint, secondsLeft, stateLine,
  type ConnectLink, type ConnectLinkState,
} from './connectLink';
import { canMintHere, hubName, type HubStatus } from './hub';

export const STATUS_POLL_MS = 3000;

export function ConnectLinkPanel({ pollMs = STATUS_POLL_MS, hub }: { pollMs?: number; hub?: HubStatus | null }) {
  const qc = useQueryClient();
  const [link, setLink] = useState<ConnectLink | null>(null);
  const [copied, setCopied] = useState(false);
  const now = useNow(link ? 1000 : 0);

  const mint = useMutation({ mutationFn: () => connectLink.mint(), onSuccess: (l) => setLink(l) });
  const status = useQuery({
    queryKey: ['origin-connect', link?.id],
    queryFn: () => connectLink.status(link!.id),
    enabled: !!link,
    refetchInterval: (q) => (q.state.data && isFinal(q.state.data.state) ? false : pollMs),
  });
  const revoke = useMutation({
    mutationFn: () => connectLink.revoke(link!.id),
    onSuccess: (s) => qc.setQueryData(['origin-connect', link?.id], s),
  });

  const server: ConnectLinkState = status.data?.state ?? 'pending';
  const left = link ? secondsLeft(status.data?.expiresAt ?? link.expiresAt, now) : 0;
  const state = effectiveState(server, left);

  // The new computer shows up in the list as soon as its pair code is claimed.
  useEffect(() => {
    if (state === 'connected') {
      void qc.invalidateQueries({ queryKey: ['origins'] });
      void qc.invalidateQueries({ queryKey: ['devices'] });
    }
  }, [state, qc]);

  const copy = async () => {
    if (!link) return;
    try {
      await navigator.clipboard.writeText(pasteHint(link.url));
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard denied — the message stays selectable */
    }
  };
  const again = () => {
    setLink(null);
    mint.reset();
    revoke.reset();
  };

  if (!link && !canMintHere(hub)) {
    return (
      <div className="og-cl" data-state="remote-hub">
        <p className="og-hint">
          New machines join the Hub on <b>{hubName(hub!)}</b>. Make the one-time link from that machine's Swingshift, or switch the Hub
          back to this machine on the Origins page.
        </p>
      </div>
    );
  }

  if (!link) {
    return (
      <div className="og-cl">
        <ol className="og-steps">
          <li>Make a one-time link.</li>
          <li>On the computer you want to add, paste it to your AI (Claude, ChatGPT…).</li>
          <li>Your AI sets it up. It connects the machine to your Hub and it shows up in the list.</li>
        </ol>
        {mint.error && (
          <p className="og-cause" role="alert">
            {friendlyLine(mint.error, 'make the link')}
          </p>
        )}
        <div className="og-cl-actions">
          <Btn variant="primary" onClick={() => mint.mutate()} disabled={mint.isPending}>
            {mint.isPending ? 'Generating…' : 'Generate one-time link for your AI'}
          </Btn>
        </div>
        <p className="og-note">The link works once and never contains a password or key.</p>
      </div>
    );
  }

  const live = state === 'pending' || state === 'fetched';
  return (
    <div className={'og-cl ' + state} data-state={state}>
      {live && <p className="og-cl-lead">Copy this and paste it to your AI on the computer you want to add:</p>}
      <div className="og-cmd">
        <code className="og-cl-msg">
          {pasteHint('')}
          <span className="og-cl-url">{link.url}</span>
        </code>
        <Btn size="sm" onClick={copy} disabled={!live} aria-label="Copy the one-time link">
          <Icon name={copied ? 'check' : 'copy'} /> {copied ? 'Copied' : 'Copy'}
        </Btn>
      </div>
      <div className="og-cl-row">
        <span className={'og-cl-state ' + state} role="status" aria-live="polite">
          {state === 'connected' ? <Icon name="check" /> : live ? <span className="og-cl-pulse" aria-hidden /> : null}
          {stateLine(state, status.data?.deviceId)}
        </span>
        {live && (
          <span className="og-cl-exp" title={new Date(link.expiresAt * 1000).toLocaleString()}>
            Expires in {countdown(left)}
          </span>
        )}
      </div>
      {live && <p className="og-note">Works once. Paste it only into your AI chat: some chat apps open links to preview them, which can use it up.</p>}
      <div className="og-cl-actions">
        {live ? (
          <Btn variant="quiet" onClick={() => revoke.mutate()} disabled={revoke.isPending} title="Stop this link from working, including any pairing it already started">
            {revoke.isPending ? 'Revoking…' : 'Revoke link'}
          </Btn>
        ) : (
          <Btn onClick={again}>{state === 'connected' ? 'Connect another' : 'Generate a new link'}</Btn>
        )}
      </div>
    </div>
  );
}

/** Wall clock that re-renders every `ms` (0 = frozen). */
function useNow(ms: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!ms) return;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}
