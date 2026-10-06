// The ONE save(+start) seam every creator door converges on: validated config →
// /api/loops/save → optionally /action start → land on the loop. A start that
// fails is surfaced calmly — the team IS saved; only ignition failed.
import { useNavigate } from 'react-router-dom';
import { creatorErrors, type LoopConfig } from '@loopyard/api';
import { friendlyLine } from '../../components/ui';
import { loopPath, useSave, useStart } from './queries';

export type MsgKind = 'info' | 'ok' | 'bad';
export interface Msg {
  kind: MsgKind;
  text: string;
}

export function useIgnite() {
  const save = useSave();
  const start = useStart();
  const navigate = useNavigate();

  return async function ignite(cfg: LoopConfig, run: boolean, say: (m: Msg) => void): Promise<string | null> {
    say({ kind: 'info', text: run ? 'Saving & starting…' : 'Saving…' });
    let res;
    try {
      res = await save.mutateAsync(cfg);
    } catch (e) {
      say({ kind: 'bad', text: friendlyLine(e, 'save your loop') });
      return null;
    }
    if (!res?.ok) {
      say({ kind: 'bad', text: 'Save failed: ' + creatorErrors(res, 'save failed') });
      return null;
    }
    const nm = res.name || cfg.name || '';
    if (!run) {
      say({ kind: 'ok', text: `Saved loop “${nm}” ✓ — open it from Loops to run it.` });
      return nm;
    }
    let err: string | undefined;
    try {
      err = (await start.mutateAsync(nm))?.error;
    } catch {
      err = "couldn't reach a runner";
    }
    if (err) {
      say({ kind: 'bad', text: `Saved “${nm}”, but couldn't start it: ${err} — open it from Loops to run when a runner is attached.` });
      return nm;
    }
    say({ kind: 'ok', text: `▶ Running “${nm}” — opening it…` });
    navigate(loopPath(nm));
    return nm;
  };
}
