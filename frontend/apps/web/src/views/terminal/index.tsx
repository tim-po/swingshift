// Web Terminal: a live shell on THIS box (local origin) over the gate-authed
// PTY-over-WebSocket. `?target=<tmux session>` (or ?attach=) attaches to a live local session
// instead (the Sessions view's "Open" verb). Leaving the page ends the session.
import { useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { TermSession } from '@loopyard/api';
import { Btn } from '../../components/ui';
import { TermStat, TermSurface } from '../../features/xterm/Pty';
import { usePty } from '../../features/xterm/usePty';
import './terminal.css';

export default function TerminalView() {
  const [params] = useSearchParams();
  // `?target=` (Sessions' Open) or `?attach=` — attach to that live local tmux session.
  const attach = params.get('target') || params.get('attach') || '';
  const session = useMemo<TermSession>(() => (attach ? { session: 'attach', target: attach } : { session: 'shell' }), [attach]);
  const { hostRef, status, text, reconnect } = usePty(session);

  return (
    <div className="tm-page">
      <div className="tm-bar">
        <TermStat status={status} text={text} />
        {status === 'off' && (
          <Btn className="btn ghost tm-sm" onClick={reconnect}>
            Reconnect
          </Btn>
        )}
        <span className="tm-hint" title={attach ? `tmux session ${attach}` : undefined}>
          {attach
            ? `Attaching to live tmux session ${attach} · local origin · leaving ends the view`
            : 'Local origin · a shell on this box · leaving this page ends the session'}
        </span>
      </div>
      <TermSurface hostRef={hostRef} className="tm-surface" />
    </div>
  );
}
