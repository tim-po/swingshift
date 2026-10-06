// The GUI face over the SAME origin-agent engine (HUB-FABRIC §6.3).
//
// The Python engine (`mcp_loops/origin_client.py`) publishes a live `status.json`
// as it moves through its state machine; `yard origin status --json` prints it and
// the desktop/GUI app polls it. This module is the JS half of that ONE contract:
// the shape of a status payload + a pure mapper to a GUI view-model (a connect
// screen while dialing, a "Connected ✓" badge + the web UI once up). No engine is
// re-implemented here — the GUI is a thin face over the CLI-first agent.

/** The states `origin_client.serve` moves through — keep in lock-step with the
 *  STATE_* constants in mcp_loops/origin_client.py. */
export type OriginRunState =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'stopped'
  | 'error'
  | 'unknown';

/** A status.json payload as the daemon writes it (and `origin status --json`
 *  prints it). `stale` is stamped by the reader when the file's `ts` is too old —
 *  a hard-killed daemon must not read as a live `connected`. */
export interface OriginStatus {
  state: OriginRunState | string;
  hub?: string;
  originId?: string;
  deviceId?: string | null;
  workerPid?: number | null;
  reason?: string;
  error?: string;
  ts?: number;
  stale?: boolean;
}

/** What the GUI should show for a given engine status: a phase, a human badge, a
 *  tone for the pill, and whether the connected web UI should be shown vs. the
 *  connect/login screen. `spin` drives a progress affordance while dialing. */
export interface OriginStatusView {
  /** Coarse phase the UI branches on: the connect screen vs. the live app. */
  phase: 'connect' | 'connecting' | 'connected';
  label: string;
  detail: string;
  tone: 'idle' | 'phos' | 'amber' | 'ember';
  spin: boolean;
  /** True only when the origin is genuinely live — the "✓" + show-the-web-UI gate. */
  live: boolean;
  /** True when a stale/killed daemon means the shown state can't be trusted. */
  stale: boolean;
}

const norm = (s: string | undefined): OriginRunState => {
  switch (s) {
    case 'idle':
    case 'connecting':
    case 'connected':
    case 'reconnecting':
    case 'stopped':
    case 'error':
      return s;
    default:
      return 'unknown';
  }
};

/** Map an engine status (or `null`/absent, i.e. no daemon has ever run) to the
 *  GUI view-model. A STALE `connected`/`reconnecting` is demoted — the daemon
 *  likely died, so the GUI drops back to the connect screen rather than showing a
 *  false "✓". */
export function originStatusView(status: OriginStatus | null | undefined): OriginStatusView {
  if (!status) {
    return { phase: 'connect', label: 'Not connected', detail: 'Sign in to a Hub to bring this Mac up as an origin.', tone: 'idle', spin: false, live: false, stale: false };
  }
  const state = norm(status.state);
  const stale = !!status.stale;
  const hub = status.hub ? ` · ${status.hub}` : '';

  // A stale live-ish state can't be trusted: the daemon that wrote it is likely
  // gone. Fall back to the connect screen, honestly labelled.
  if (stale && (state === 'connected' || state === 'reconnecting' || state === 'connecting')) {
    return { phase: 'connect', label: 'Disconnected', detail: `The origin agent is no longer running${hub}. Reconnect to bring it back up.`, tone: 'amber', spin: false, live: false, stale: true };
  }

  switch (state) {
    case 'connected':
      return { phase: 'connected', label: 'Connected ✓', detail: `Serving as an origin${hub}.`, tone: 'phos', spin: false, live: true, stale: false };
    case 'connecting':
      return { phase: 'connecting', label: 'Connecting…', detail: `Dialing the Hub${hub}.`, tone: 'amber', spin: true, live: false, stale: false };
    case 'reconnecting':
      return { phase: 'connecting', label: 'Reconnecting…', detail: `Connection dropped — retrying${hub}.`, tone: 'amber', spin: true, live: false, stale: false };
    case 'error':
      return { phase: 'connect', label: 'Connection failed', detail: status.error || status.reason || 'The origin agent stopped with an error.', tone: 'ember', spin: false, live: false, stale };
    case 'stopped':
      return { phase: 'connect', label: 'Stopped', detail: `The origin agent stopped${status.reason ? ` (${status.reason})` : ''}. Reconnect to bring it back up.`, tone: 'idle', spin: false, live: false, stale };
    case 'idle':
      return { phase: 'connect', label: 'Not connected', detail: 'Sign in to a Hub to bring this Mac up as an origin.', tone: 'idle', spin: false, live: false, stale };
    default:
      return { phase: 'connect', label: 'Unknown', detail: 'The origin agent reported an unrecognized state.', tone: 'amber', spin: false, live: false, stale };
  }
}
