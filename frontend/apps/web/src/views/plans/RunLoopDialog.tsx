// Run a plan as a loop — ALWAYS behind this confirmation. Opening it only reads
// the plan; nothing is built or started until "Start loop". Then: build + save a
// loop from the plan and link it (ws.buildLoop), and start it through the normal
// loop action seam. A start that fails leaves the saved, linked loop in place.
import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { planPreview, PROJECT_UNATTR } from '@loopyard/api';
import { Btn } from '../../components/ui';
import { Icon } from '../../components/icons';
import { Modal } from '../../shell/Modal';
import { useProjectScope } from '../../scope';
import { errText } from '../hub/flash';
import { startLoop, useDoc, useObjective, ws, wsKeys } from './queries';

export function RunLoopDialog({ oid, onClose, flash, onStarted }: {
  oid: string;
  onClose(): void;
  flash(t: string, err?: boolean): void;
  /** Called with the loop's name once it's saved + linked (started or not). */
  onStarted?(loop: string): void;
}) {
  const qc = useQueryClient();
  const detail = useObjective(oid).data?.objective;
  const docs = detail?.docs ?? [];
  const first = docs.find((d) => d.slug === 'index') ?? docs[0];
  const doc = useDoc(oid, first?.slug).data?.doc;
  const scope = useProjectScope();
  const fromHub = detail?.source === 'ideahub';
  const projects = useMemo(() => scope.projects.filter((p) => p.id !== PROJECT_UNATTR), [scope.projects]);
  const defaultProject = detail?.project || (scope.project && scope.project !== PROJECT_UNATTR ? scope.project : '');
  const [picked, setPicked] = useState<string | null>(null);
  const project = picked ?? defaultProject;
  const projectName = (id: string) => projects.find((p) => p.id === id)?.name || id;

  const preview = planPreview(doc?.body);
  const more = docs.length > 1 ? docs.length - 1 : 0;

  const run = useMutation({
    mutationFn: async () => {
      const built = await ws.buildLoop(oid, fromHub ? undefined : project || undefined);
      const name = built.loop;
      if (!name) throw new Error(built.error || 'Couldn’t make a loop from this plan.');
      let err: string | undefined;
      try {
        err = (await startLoop(name))?.error;
      } catch {
        err = 'couldn’t reach a runner';
      }
      return { name, err };
    },
    onSuccess: ({ name, err }) => {
      qc.invalidateQueries({ queryKey: ['loops'] });
      qc.invalidateQueries({ queryKey: wsKeys.objective(oid) });
      qc.invalidateQueries({ queryKey: wsKeys.objectives });
      if (err) flash(`Saved the loop “${name}” on this plan, but it couldn’t start: ${err}. Open it from Loops to try again.`, true);
      else flash(`Started “${name}”. It’s listed under Loops on this plan.`);
      onStarted?.(name);
      onClose();
    },
    onError: (e) => flash(errText(e), true),
  });

  return (
    <Modal label="Run as loop" onClose={onClose}>
      <div className="pl-dlg">
        <h2 className="h3">Run “{detail?.title ?? 'this plan'}” as a loop</h2>
        <p className="sub">A small team works through the plan until it’s done: a manager, a builder and a reviewer, up to 12 turns.</p>

        <div className="field">
          <span className="field-label">Goal</span>
          <div className="pl-goal">
            <b>{detail?.title}</b>
            {preview ? <span> — {preview}</span> : <span className="muted"> — the plan has no text yet; the loop works from its title.</span>}
            {more > 0 && <div className="muted pl-goalmore">Plus {more} more note{more === 1 ? '' : 's'} from this plan.</div>}
          </div>
        </div>

        <div className="field">
          <label className="field-label" htmlFor="pl-run-project">Project</label>
          {fromHub || !projects.length ? (
            <div className="pl-dlgval" id="pl-run-project">{project ? projectName(project) : 'No project'}</div>
          ) : (
            <select id="pl-run-project" className="select" value={project} onChange={(e) => setPicked(e.target.value)}>
              <option value="">No project</option>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          )}
        </div>

        <div className="field">
          <span className="field-label">Machine</span>
          <div className="pl-dlgval" title="Starts on the runner connected to this hub (yard up). Change machines from the loop page.">
            <Icon name="machine" /> This computer’s runner
          </div>
        </div>

        <div className="pl-dlgactions">
          <Btn variant="primary" disabled={run.isPending || !detail} onClick={() => run.mutate()}>
            <Icon name="play" /> {run.isPending ? 'Starting…' : 'Start loop'}
          </Btn>
          <Btn disabled={run.isPending} onClick={onClose}>Cancel</Btn>
        </div>
      </div>
    </Modal>
  );
}
