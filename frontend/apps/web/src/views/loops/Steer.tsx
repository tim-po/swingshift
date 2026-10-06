import type { SteerInfo } from '@loopyard/api';
import { Icon } from '../../components/icons';

const STEP: Record<string, string> = {
  stopping: 'Finishing the current turn, then pausing — nothing is cut off mid-turn.',
  handoff: 'Paused. Getting the manager ready for you…',
  ready: 'Talk to the manager: give it the new direction, then restart. The team picks up from the same turn with your context.',
};
const STEPS = ['stopping', 'handoff', 'ready'] as const;
const STEP_LABEL: Record<(typeof STEPS)[number], string> = { stopping: 'Pause', handoff: 'Hand-off', ready: 'Talk and restart' };

/**
 * Steer mirrors Brief: a gentle stop → the manager's attach → the owner talks to it
 * → restart FROM THE SAME POINT. This panel is the whole flow's surface.
 */
export function SteerPanel({ steer, busy, onResume }: { steer: SteerInfo; busy: boolean; onResume(): void }) {
  const ph = steer.phase || '';
  if (ph === 'error') {
    return (
      <div className="ld-banner steer" role="status">
        <b>Steering didn't reach the manager</b>
        <p>The loop is paused safely. Try Debrief under More actions. If this provider cannot restore the chat, use Brief for a new conversation based on the saved loop files.</p>
        {steer.error && <p>{steer.error}</p>}
      </div>
    );
  }
  if (!STEP[ph]) return null;
  const at = STEPS.indexOf(ph as (typeof STEPS)[number]);
  return (
    <div className="ld-banner steer" role="status" aria-live="polite">
      <b>Steering{ph === 'ready' ? ' — your turn' : '…'}</b>
      <ol className="ld-steps" aria-label="Steer progress">
        {STEPS.map((s, i) => (
          <li key={s} className={s === ph ? 'on' : at > i ? 'done' : ''}>{STEP_LABEL[s]}</li>
        ))}
      </ol>
      <p>{STEP[ph]}</p>
      {ph === 'ready' && steer.conversation?.startsWith('new session') && (
        <p>This is a new manager conversation using saved loop context. The previous chat was not restored.</p>
      )}
      {ph === 'ready' && (
        <>
          <div className="ld-field-label">Attach command — run it on the loop's machine</div>
          <pre className="ld-pre ld-copy">{steer.attach || 'No attach command was returned — use Debrief.'}</pre>
          <div className="ld-mactions">
            <button className="btn primary" disabled={busy} onClick={onResume}>
              <Icon name="play" /> Restart from turn {steer.turnsUsed ?? '—'}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
