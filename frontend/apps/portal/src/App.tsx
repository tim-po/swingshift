// Portal shell (CP-5): login -> "Set up Loopyard on this machine" -> your hub ->
// handoff to the user's own dashboard. Owners also get #/invites (the allowlist).
// Hash routes (#/setup, #/hubs, #/invites) so the CP can serve the build as plain
// static files.
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import {
  addInvite,
  ApiError,
  enterUrl,
  getMe,
  issueDeviceToken,
  listHubs,
  listInvites,
  loginHref,
  logout,
  nav,
  onboardingStatus,
  revokeInvite,
  safeHubUrl,
  startOnboarding,
  type Hub,
  type Invite,
  type Me,
  type Onboarding,
  type OnboardStatus,
} from './api';

export type Route = 'setup' | 'hubs' | 'invites';
export const POLL_MS = 3000;

export function routeFromHash(hash: string): Route {
  const r = hash.replace(/^#\/?/, '');
  return r === 'setup' || r === 'invites' ? r : 'hubs';
}

function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(() => routeFromHash(window.location.hash));
  useEffect(() => {
    const on = () => setRoute(routeFromHash(window.location.hash));
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  return route;
}

export function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [failed, setFailed] = useState(false);
  const hashRoute = useHashRoute();

  useEffect(() => {
    getMe().then(setMe, () => setFailed(true));
  }, []);

  if (failed) return <Notice title="The portal is unreachable" body="Check your connection and reload the page." />;
  if (me === undefined) return <div className="page muted" aria-busy="true">Loading…</div>;
  if (me === null) return <Login />;
  // The server enforces owner-only; this just keeps non-owners off a dead screen.
  const route = hashRoute === 'invites' && !me.is_owner ? 'hubs' : hashRoute;
  return (
    <div className="page">
      <header className="top">
        <strong>Loopyard</strong>
        <nav>
          <a href="#/hubs" aria-current={route === 'hubs' ? 'page' : undefined}>Your machines</a>
          <a href="#/setup" aria-current={route === 'setup' ? 'page' : undefined}>Set up</a>
          {me.is_owner && (
            <a href="#/invites" aria-current={route === 'invites' ? 'page' : undefined}>Invites</a>
          )}
        </nav>
        <span className="muted who">{me.email}</span>
        <button className="btn ghost" onClick={() => logout().finally(() => setMe(null))}>Sign out</button>
      </header>
      {route === 'setup' ? <Setup /> : route === 'invites' ? <Invites /> : <Hubs />}
    </div>
  );
}

export function Login() {
  return (
    <div className="page narrow">
      <h1>Loopyard</h1>
      <p className="muted">Loopyard is invite-only for now. Sign in with the Google account your invite was sent to.</p>
      <a className="btn primary" href={loginHref(window.location.hash.startsWith('#/open/') ? '/' + window.location.hash : '/#/hubs')}>Sign in with Google</a>
    </div>
  );
}

function Notice({ title, body }: { title: string; body: string }) {
  return (
    <div className="page narrow" role="alert">
      <h1>{title}</h1>
      <p className="muted">{body}</p>
    </div>
  );
}

export function Hubs() {
  const [hubs, setHubs] = useState<Hub[] | null>(null);
  const [failed, setFailed] = useState(false);
  const requested = window.location.hash.startsWith('#/open/') ? window.location.hash.slice(7) : '';
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const load = () => listHubs().then((items) => {
      if (cancelled) return;
      setHubs(items);
      if (requested && !items.some((h) => h.id === requested && h.status === 'online')) timer = setTimeout(load, POLL_MS);
    }, () => { if (!cancelled) setFailed(true); });
    load();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [requested]);
  if (failed) return <p className="muted" role="alert">We couldn't load your machines. Reload to try again.</p>;
  if (hubs === null) return <p className="muted" aria-busy="true">Loading your machines…</p>;
  if (hubs.length === 0)
    return (
      <section className="card empty">
        <h2>No machines yet</h2>
        <p className="muted">Loopyard runs on your own hardware. Set it up on this machine to get started.</p>
        <a className="btn primary" href="#/setup">Set up Loopyard on this machine</a>
      </section>
    );
  return (
    <section>
      <h2>Your hub</h2>
      {requested && <p role="status">Connecting to your dashboard… You may need to trust this machine’s local HTTPS certificate in your browser.</p>}
      <ul className="hubs">
        {hubs.map((h) => (
          <HubRow key={h.id} hub={h} autoOpen={h.id === requested} />
        ))}
      </ul>
      <a className="btn" href="#/setup">Add another machine</a>
    </section>
  );
}

function HubRow({ hub, autoOpen = false }: { hub: Hub; autoOpen?: boolean }) {
  const url = safeHubUrl(hub.public_url);
  const online = hub.status === 'online';
  return (
    <li className="card hub" data-status={hub.status}>
      <span className={`dot ${online ? 'ok' : 'off'}`} aria-hidden />
      <div className="grow">
        <div>{url ? new URL(url).host : 'unknown address'}</div>
        <div className="muted small">
          {online ? 'Online' : 'Offline'} · on <span className="mono">{hub.holder_origin_id}</span>
        </div>
      </div>
      {online && url ? (
        <OpenDashboard hub={hub} autoOpen={autoOpen} />
      ) : (
        <span className="muted small">Start Loopyard on that machine to reconnect.</span>
      )}
    </li>
  );
}

/** Handoff (§6a): mint this browser's device token, then sign in on the hub. */
export function OpenDashboard({ hub, autoOpen = false }: { hub: Hub; autoOpen?: boolean }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);
  const open = async () => {
    setBusy(true);
    setErr(false);
    try {
      const { token } = await issueDeviceToken(hub.id, 'browser');
      const url = enterUrl(hub.public_url, token);
      if (!url) throw new Error('unsafe hub address');
      nav.go(url);
    } catch {
      setErr(true);
      setBusy(false);
    }
  };
  const opened = useRef(false);
  useEffect(() => {
    if (autoOpen && !opened.current) { opened.current = true; void open(); }
  }, [autoOpen]);
  return (
    <span className="handoff">
      <button className="btn primary" onClick={open} disabled={busy}>Open your dashboard</button>
      {err && <span role="alert" className="muted small"> We couldn't open your dashboard. Try again.</span>}
    </span>
  );
}

const STATUS_COPY: Record<OnboardStatus['status'], string> = {
  pending: 'Waiting for this machine to check in…',
  registered: 'Your hub registered. Waiting for it to come online…',
  online: 'Your hub is online.',
  expired: 'This setup link expired. Start again to get a fresh one.',
};

export function statusCopy(s: OnboardStatus['status']): string {
  return STATUS_COPY[s];
}

export function Setup() {
  const [label, setLabel] = useState('');
  const [runtime, setRuntime] = useState('claude');
  const [trusted, setTrusted] = useState(false);
  const [ob, setOb] = useState<Onboarding | null>(null);
  const [st, setSt] = useState<OnboardStatus | null>(null);
  const [err, setErr] = useState(false);
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const poll = useCallback((id: string) => {
    onboardingStatus(id).then(
      (s) => {
        setSt(s);
        if (s.status === 'pending' || s.status === 'registered') timer.current = setTimeout(() => poll(id), POLL_MS);
      },
      () => {
        timer.current = setTimeout(() => poll(id), POLL_MS);
      },
    );
  }, []);
  useEffect(() => () => clearTimeout(timer.current), []);

  const begin = async () => {
    if (runtime !== 'claude' && !trusted) return;
    setBusy(true);
    setErr(false);
    setSt(null);
    clearTimeout(timer.current);
    try {
      const o = await startOnboarding(label.trim() || 'my machine', runtime, trusted);
      setOb(o);
      poll(o.id);
    } catch {
      setErr(true);
    } finally {
      setBusy(false);
    }
  };

  const handoff = st?.status === 'online' && st.hub && safeHubUrl(st.handoff_url) ? st.hub : null;
  return (
    <section className="card">
      <h2>Set up Loopyard on this machine</h2>
      <p className="muted">
        Loopyard runs on your hardware. One command installs it here, starts your hub and connects it to your
        account.
      </p>
      {!ob || st?.status === 'expired' ? (
        <div className="setup-options">
          <label>Which CLI should run your agents?
            <select aria-label="Agent CLI" value={runtime} disabled={busy} onChange={(e) => { setRuntime(e.target.value); setTrusted(false); }}>
              <option value="claude">Claude</option><option value="codex">Codex</option><option value="cursor">Cursor</option>
            </select>
          </label>
          <p className="muted small">Install your selected CLI and sign in on the target machine first. Loopyard uses that account.</p>
          {runtime !== 'claude' ? <label>
            <input type="checkbox" checked={trusted} disabled={busy} onChange={(e) => setTrusted(e.target.checked)} />
            I allow agents to run with my user permissions in trusted workspaces.
            <span className="muted small"> This beta does not enforce OS isolation or role restrictions for Codex/Cursor. Use repositories you trust.</span>
          </label> : <p className="muted small">Claude uses role permission profiles by default.</p>}
          <input aria-label="Machine name" placeholder="Machine name (e.g. work laptop)" value={label}
            onChange={(e) => setLabel(e.target.value)} maxLength={80} />
          <button className="btn primary" onClick={begin} disabled={busy || (runtime !== 'claude' && !trusted)}>Set up Loopyard on this machine</button>
        </div>
      ) : (
        <>
          <p>Selected CLI: <strong>{ob.runtime || runtime}</strong>. {ob.trusted_workspace ? 'Trusted workspace permissions.' : 'Role permission profiles.'}</p>
          {ob.agent_setup_url && <>
            <h3>Set up with an agent</h3>
            <p>Give this one-time link to any coding agent on the machine you want to set up. Ask it to open the link and set up Loopyard. The instructions include your selected CLI and permissions.</p>
            <pre className="cmd mono" data-testid="agent-setup-url">{ob.agent_setup_url}</pre>
            <button className="btn primary" onClick={() => navigator.clipboard?.writeText(ob.agent_setup_url!)}>Copy one-time setup link</button>
          </>}
          <h3>Set up manually</h3>
          <p>Download the installer or copy the command below. Run the downloaded file with <code>sh ~/Downloads/install-loopyard.command</code> in Terminal.</p>
          {ob.script && <button className="btn primary" onClick={() => {
            const url = URL.createObjectURL(new Blob([ob.script!], { type: 'text/plain' }));
            const a = document.createElement('a'); a.href = url; a.download = 'install-loopyard.command'; a.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          }}>Download installer</button>}
          <p>Manual setup: copy and run this command in a terminal on the machine:</p>
          <pre className="cmd mono" data-testid="command">{ob.command}</pre>
          <button className="btn" onClick={() => navigator.clipboard?.writeText(ob.command)}>Copy command</button>
          {ob.stub && <p className="muted small">Preview: the installer isn't published yet, so this command is a placeholder.</p>}
          <p className="muted small">Use one method within 15 minutes. All methods apply the same CLI and permission choices. Share the instructions only with your trusted setup agent. Keep the command and downloaded file private, then delete the file after use. Existing installations keep their saved provider settings.</p>
        </>
      )}
      {err && <p role="alert">We couldn't start setup. Try again in a moment.</p>}
      {st && (
        <p role="status" data-status={st.status}>
          {statusCopy(st.status)}
        </p>
      )}
      {handoff && <OpenDashboard hub={handoff} />}
    </section>
  );
}

const INVITE_STATUS: Record<Invite['status'], string> = {
  open: 'Invited',
  redeemed: 'Joined',
  revoked: 'Revoked',
};

/** Owner-only: who may sign in. Add an email, or revoke an invite nobody has used yet. */
export function Invites() {
  const [invites, setInvites] = useState<Invite[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => listInvites().then(setInvites, () => setFailed(true)), []);
  useEffect(() => {
    load();
  }, [load]);

  const run = async (fn: () => Promise<unknown>, fail: (e: unknown) => string) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      await load();
      return true;
    } catch (e) {
      setErr(fail(e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const add = async (e: FormEvent) => {
    e.preventDefault();
    const ok = await run(() => addInvite(email.trim()), (x) =>
      x instanceof ApiError && x.status === 400
        ? 'Enter a valid email address that has not already joined.'
        : "We couldn't add that invite. Try again in a moment.");
    if (ok) setEmail('');
  };
  const revoke = (who: string) =>
    run(() => revokeInvite(who), (x) =>
      x instanceof ApiError && x.status === 404
        ? 'That invite was already used or revoked.'
        : "We couldn't revoke that invite. Try again in a moment.");

  if (failed) return <p className="muted" role="alert">We couldn't load invites. Reload to try again.</p>;
  if (invites === null) return <p className="muted" aria-busy="true">Loading invites…</p>;
  return (
    <section className="card">
      <h2>Invites</h2>
      <p className="muted">Only invited Google accounts can sign in. Revoking an unused invite stops it working.</p>
      <form className="row" onSubmit={add}>
        <input aria-label="Email to invite" type="email" placeholder="name@example.com" value={email}
          onChange={(e) => setEmail(e.target.value)} maxLength={254} required />
        <button className="btn primary" type="submit" disabled={busy || !email.trim()}>Invite</button>
      </form>
      {err && <p role="alert">{err}</p>}
      {invites.length === 0 ? (
        <p className="muted">No invites yet.</p>
      ) : (
        <ul className="hubs invites">
          {invites.map((i) => (
            <li key={i.email} className="card hub" data-status={i.status}>
              <div className="grow">
                <div>{i.email}</div>
                <div className="muted small">{INVITE_STATUS[i.status]}</div>
              </div>
              {i.status === 'open' && (
                <button className="btn ghost" onClick={() => revoke(i.email)} disabled={busy}
                  aria-label={`Revoke invite for ${i.email}`}>Revoke</button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
