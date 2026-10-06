// The Advanced briefing terminal: the gate-authed /ws/terminal with
// ?session=creator, so the PTY autostarts a seeded loop-creator agent. The
// composer mounts it ONLY after an explicit "Start briefing terminal" click;
// unmounting closes the socket (the backend reaps the PTY).
import { useMemo } from 'react';
import type { TermSession } from '@loopyard/api';
import { TermStat, TermSurface } from '../../features/xterm/Pty';
import { usePty } from '../../features/xterm/usePty';

export const RUNTIMES = ['claude', 'codex', 'cursor'] as const;

export function BriefingTerminal({ runtime, setRuntime, onOutput }: { runtime: string; setRuntime(r: string): void; onOutput(chunk: string): void }) {
  const session = useMemo<TermSession>(() => ({ session: 'creator', runtime }), [runtime]);
  const { hostRef, status, text, reconnect } = usePty(session, { onOutput, scrollback: 6000 });
  return (
    <>
      <div className="nl-termhd">
        <span className="nl-t">Briefing terminal</span>
        <TermStat status={status} text={text} />
        <span className="nl-grow" />
        <label className="nl-rt">
          Runtime
          <select className="select" value={runtime} onChange={(e) => setRuntime(e.target.value)} aria-label="Creator runtime">
            {RUNTIMES.map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        </label>
        {status === 'off' && (
          <button type="button" className="btn sm" onClick={reconnect}>
            Reconnect
          </button>
        )}
      </div>
      <TermSurface hostRef={hostRef} className="nl-term" />
    </>
  );
}
