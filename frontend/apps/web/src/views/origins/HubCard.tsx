// The Hub, first-class on Origins: which machine new origins join (host, public
// URL, fingerprint, live/down), a "Connect an origin" action that always works
// against it, and a switch between "this machine is the Hub" and another
// origin's Hub. The choice persists server-side (hub_record).
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { isLocalOrigin, isSessionOrigin, type Origin } from '@loopyard/api';
import { Btn, friendlyLine } from '../../components/ui';
import {
  HUB_POLL_MS, HUB_QUERY_KEY, canMintHere, hub, hubLine, hubName, hubPill, remoteHubError, shortFingerprint,
  type HubStatus, type HubSwitch,
} from './hub';

export const useHub = (pollMs = HUB_POLL_MS) =>
  useQuery({ queryKey: HUB_QUERY_KEY, queryFn: () => hub.status(), refetchInterval: pollMs || false });

export function HubCard({ origins, onConnect, pollMs }: { origins: Origin[]; onConnect(): void; pollMs?: number }) {
  const q = useHub(pollMs);
  const [switching, setSwitching] = useState(false);

  if (q.isPending) {
    return (
      <section className="og-hub pending" aria-label="Hub">
        <div className="og-head"><span className="kick">◈ Hub</span><span className="og-health pending">checking</span></div>
      </section>
    );
  }
  if (q.error || !q.data) {
    return (
      <section className="og-hub off" aria-label="Hub">
        <div className="og-head"><span className="kick">◈ Hub</span></div>
        <p className="og-cause" role="alert">{friendlyLine(q.error, 'read the Hub')}</p>
        <div className="og-actions"><Btn onClick={() => q.refetch()}>Try again</Btn></div>
      </section>
    );
  }

  const h = q.data;
  const pill = hubPill(h);
  return (
    <section className={'og-hub ' + pill} aria-label="Hub" data-mode={h.mode}>
      <div className="og-head">
        <span className="kick">◈ Hub</span>
        <span className="og-name og-hub-name" title={h.originId || undefined}>{hubName(h)}</span>
        <span className={'og-health ' + pill} title={h.checkedAt ? `checked ${new Date(h.checkedAt * 1000).toLocaleTimeString()}` : undefined}>
          {pill === 'live' ? 'live' : pill === 'off' ? 'down' : 'not ready'}
        </span>
      </div>
      <p className={'og-hint' + (pill === 'live' ? '' : ' og-hub-warn')}>{hubLine(h)}</p>
      <dl className="og-hub-facts">
        <dt>Address</dt>
        <dd className="mono og-hub-url">{h.publicUrl || '—'}</dd>
        <dt>Fingerprint</dt>
        <dd className="mono" title={h.fingerprint || undefined}>{shortFingerprint(h.fingerprint) || '—'}</dd>
      </dl>
      {switching ? (
        <HubSwitchForm current={h} origins={origins} onDone={() => setSwitching(false)} />
      ) : (
        <div className="og-actions">
          {canMintHere(h) && (
            <Btn variant="primary" onClick={onConnect} title="Make a one-time link your AI uses to connect a machine to this Hub">
              ＋ Generate a connect link
            </Btn>
          )}
          <Btn onClick={() => setSwitching(true)} title="Choose which machine is the Hub">Switch Hub</Btn>
        </div>
      )}
    </section>
  );
}

const OTHER = '__other__';

function HubSwitchForm({ current, origins, onDone }: { current: HubStatus; origins: Origin[]; onDone(): void }) {
  const qc = useQueryClient();
  const remotes = origins.filter((o) => !isLocalOrigin(o) && !isSessionOrigin(o));
  const [mode, setMode] = useState<'self' | 'remote'>(current.mode);
  const [originId, setOriginId] = useState(current.mode === 'remote' ? current.originId || OTHER : remotes[0]?.id || OTHER);
  const [url, setUrl] = useState(current.mode === 'remote' ? current.publicUrl || '' : '');
  const [fp, setFp] = useState(current.mode === 'remote' ? current.fingerprint || '' : '');

  const invalid = mode === 'remote' ? remoteHubError(url, fp) : null;
  const save = useMutation({
    mutationFn: (input: HubSwitch) => hub.set(input),
    onSuccess: (s) => {
      qc.setQueryData(HUB_QUERY_KEY, s);
      void qc.invalidateQueries({ queryKey: ['origins'] });
      onDone();
    },
  });
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (invalid) return;
    if (mode === 'self') return save.mutate({ mode: 'self' });
    const o = remotes.find((r) => r.id === originId);
    save.mutate({
      mode: 'remote', publicUrl: url.trim(), fingerprint: fp.trim().toLowerCase() || undefined,
      originId: o?.id, host: o ? o.name || o.id : undefined,
    });
  };

  return (
    <form className="og-hub-switch" onSubmit={submit} aria-label="Switch Hub">
      <label className="og-hub-opt">
        <input type="radio" name="hub-mode" value="self" checked={mode === 'self'} onChange={() => setMode('self')} />
        <span><b>This machine is the Hub</b> — new origins join here{current.mode === 'self' && current.host ? ` (${current.host})` : ''}.</span>
      </label>
      <label className="og-hub-opt">
        <input type="radio" name="hub-mode" value="remote" checked={mode === 'remote'} onChange={() => setMode('remote')} />
        <span><b>Another machine is the Hub</b> — new origins join it instead.</span>
      </label>
      {mode === 'remote' && (
        <div className="og-hub-fields">
          <label>
            <span className="kick">Machine</span>
            <select className="og-picker" value={originId} onChange={(e) => setOriginId(e.target.value)} aria-label="Hub machine">
              {remotes.map((o) => <option key={o.id} value={o.id}>{o.name || o.id}</option>)}
              <option value={OTHER}>Another machine…</option>
            </select>
          </label>
          <label>
            <span className="kick">Address</span>
            <input className="og-picker" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="wss://hub.example.com/hub" aria-label="Hub address" />
          </label>
          <label>
            <span className="kick">Fingerprint</span>
            <input className="og-picker mono" value={fp} onChange={(e) => setFp(e.target.value)} placeholder="ab:cd:…" aria-label="Hub fingerprint" />
          </label>
          <p className="og-hint">The Hub prints both when it starts (<code>HUB_LISTENING … fingerprint=…</code>). Links to connect new machines are then made on that machine.</p>
        </div>
      )}
      {(invalid && (url || fp)) && <p className="og-cause" role="alert">{invalid}</p>}
      {save.error && <p className="og-cause" role="alert">{friendlyLine(save.error, 'switch the Hub')}</p>}
      <div className="og-actions">
        <Btn variant="primary" type="submit" disabled={!!invalid || save.isPending}>{save.isPending ? 'Saving…' : 'Save'}</Btn>
        <Btn type="button" onClick={onDone}>Cancel</Btn>
      </div>
    </form>
  );
}
