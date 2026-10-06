import { useEffect, useState } from 'react';
import { Btn, friendlyLine } from '../../components/ui';
import { Icon } from '../../components/icons';
import { useAttaches, useMintAttachLink, useRevokeAttach, type Attach, type AttachLink } from './api';

/** "29:58" — whole seconds left, never negative. */
export function mmss(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** Seconds until `expiresAt` (epoch seconds), re-rendering every second while positive. */
function useSecondsLeft(expiresAt: number | undefined): number {
  const left = () => (expiresAt ? expiresAt - Date.now() / 1000 : 0);
  const [s, setS] = useState(left);
  useEffect(() => {
    setS(left());
    if (!expiresAt) return;
    const t = setInterval(() => setS(left()), 1000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expiresAt]);
  return s;
}

/**
 * "Connect a session": make a one-time link the owner pastes to their AI. The AI
 * opens it, gets its own instructions, and connects itself as a session. Minting
 * creates a real credential on the server, so it happens only on the button
 * click — never on mount. Shows the link, its countdown, and flips to
 * "Connected" once the server reports the link used.
 */
export function AttachPanel() {
  const mint = useMintAttachLink();
  const revoke = useRevokeAttach();
  const [link, setLink] = useState<AttachLink | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [copied, setCopied] = useState(false);

  const { data } = useAttaches(!!link);
  const rows = data?.attaches ?? [];
  const row = link ? rows.find((r) => r.attachId === link.attachId) : undefined;
  const left = useSecondsLeft(link?.expiresAt);
  const attached = rows.filter((r) => r.attached && r.state === 'consumed');

  // The link's own state: the server's word once it reports it; locally expired once the clock runs out.
  const state = row?.state ?? (left <= 0 ? 'expired' : 'pending');
  const waiting = state === 'pending' && left > 0;

  const generate = () => {
    setMsg(null);
    setCopied(false);
    mint.mutate(
      {},
      {
        onSuccess: (r) => setLink(r),
        onError: (e) => setMsg({ ok: false, text: friendlyLine(e, 'make a connect link') }),
      },
    );
  };

  const copy = () => {
    if (!link) return;
    navigator.clipboard?.writeText(link.url).then(() => setCopied(true), () => setCopied(false));
  };

  const doRevoke = (a: Pick<Attach, 'attachId' | 'connectorId'>) =>
    revoke.mutate(a.attachId, {
      onSuccess: () => setMsg({ ok: true, text: `Revoked ${a.connectorId}. It can't connect any more.` }),
      onError: (e) => setMsg({ ok: false, text: friendlyLine(e, 'revoke that session') }),
    });

  return (
    <section className="ss-attach" aria-label="Connect a session">
      {!link && (
        <ol className="ss-steps">
          <li>Make a one-time link.</li>
          <li>Paste it to your AI (Claude, ChatGPT…) or a terminal session running one.</li>
          <li>It connects itself and shows up in the list.</li>
        </ol>
      )}

      {link && (
        <div className={'ss-link ' + state}>
          <div className="ss-linkrow">
            <input className="input mono" readOnly value={link.url} aria-label="One-time attach link" onFocus={(e) => e.currentTarget.select()} />
            <Btn size="sm" onClick={copy} disabled={!waiting}>
              <Icon name={copied ? 'check' : 'copy'} /> {copied ? 'Copied' : 'Copy'}
            </Btn>
          </div>
          <p className="ss-hint">Paste this link to your AI (Claude, ChatGPT…). It reads its own instructions and connects itself.</p>
          <div className="ss-linkstate" role="status">
            {state === 'consumed' ? (
              <>
                <span className="ss-ok"><Icon name="check" /> Connected</span>
                <span className="mono">{link.connectorId}</span>
                <span className="ss-quiet">{row?.live ? "It's online in the list." : 'Waiting for it to check in.'}</span>
              </>
            ) : state === 'revoked' ? (
              <span className="ss-quiet">Link revoked. It no longer works.</span>
            ) : state === 'expired' ? (
              <span className="ss-quiet">This link expired. Make a new one.</span>
            ) : (
              <>
                <span className="ss-wait"><span className="ss-pulse" aria-hidden /> Waiting for your AI to open the link…</span>
                <span className="ss-exp" title="The link works once and stops after this">Expires in {mmss(left)}</span>
              </>
            )}
          </div>
          {link.publicUrlConfigured === false && (
            <p className="ss-note">This link uses this dashboard's own address, so your AI must be able to reach it.</p>
          )}
        </div>
      )}

      <div className="ss-attach-actions">
        <Btn variant="primary" onClick={generate} disabled={mint.isPending || waiting}>
          {mint.isPending ? 'Generating…' : link ? 'Generate a new link' : 'Generate one-time link'}
        </Btn>
        {link && waiting && (
          <Btn variant="quiet" onClick={() => doRevoke(link)} disabled={revoke.isPending} title="Stop this link from working">Revoke link</Btn>
        )}
      </div>

      {msg && <p className={'ss-msg' + (msg.ok ? ' ok' : ' err')} role="status">{msg.text}</p>}

      {!!attached.length && (
        <div className="ss-attached-wrap">
          <span className="ss-k">Connected by link</span>
          <ul className="ss-attached" aria-label="Sessions attached by link">
            {attached.map((a) => (
              <li key={a.attachId}>
                <span className={'ss-dot' + (a.live ? ' on' : '')} aria-hidden="true" />
                <span className="mono">{a.connectorId}</span>
                {a.label && <span className="ss-quiet">{a.label}</span>}
                <span className="ss-quiet">{a.live ? 'Online' : 'Offline'}</span>
                <Btn size="sm" variant="quiet" onClick={() => doRevoke(a)} disabled={revoke.isPending} title="Cut this session off: its access stops working at once">
                  Revoke
                </Btn>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
