// + New plan: name it (filed under the project in view), then land on the plan ready to write.
import { useMemo, useState } from 'react';
import { PROJECT_UNATTR } from '@loopyard/api';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { Btn } from '../../components/ui';
import { useProjectScope } from '../../scope';
import { Modal } from '../../shell/Modal';
import { errText } from '../hub/flash';
import { planRoute, ws, wsKeys } from './queries';

export function NewPlanDialog({ onClose, flash }: { onClose(): void; flash(t: string, err?: boolean): void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const scope = useProjectScope();
  const projects = useMemo(() => scope.projects.filter((p) => p.id !== PROJECT_UNATTR), [scope.projects]);
  const [title, setTitle] = useState('');
  const [project, setProject] = useState(scope.project && scope.project !== PROJECT_UNATTR ? scope.project : '');
  const create = useMutation({
    mutationFn: async (t: string) => {
      const oid = (await ws.createObjective(t, project || undefined)).objective?.id;
      if (!oid) throw new Error('Couldn’t create the plan.');
      // every plan starts with one note to write in (status: Idea)
      const slug = (await ws.saveDoc({ oid, title: t, body: '', status: 'draft' })).doc?.slug;
      return { oid, slug };
    },
    onSuccess: ({ oid, slug }) => {
      qc.invalidateQueries({ queryKey: wsKeys.objectives });
      onClose();
      navigate(planRoute(oid, slug), { state: { edit: true } });
    },
    onError: (e) => flash(errText(e), true),
  });
  const submit = () => {
    const t = title.trim();
    if (t) create.mutate(t);
  };
  return (
    <Modal label="New plan" onClose={onClose}>
      <form className="pl-dlg" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <h2 className="h3">New plan</h2>
        <div className="field">
          <label className="field-label" htmlFor="pl-new-title">What’s the plan?</label>
          <input id="pl-new-title" className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Dark mode" autoFocus />
        </div>
        {projects.length > 0 && (
          <div className="field">
            <label className="field-label" htmlFor="pl-new-project">Project</label>
            <select id="pl-new-project" className="select" value={project} onChange={(e) => setProject(e.target.value)}>
              <option value="">No project</option>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </div>
        )}
        <div className="pl-dlgactions">
          <button type="submit" className="btn primary" disabled={!title.trim() || create.isPending}>{create.isPending ? 'Creating…' : 'Create plan'}</button>
          <Btn onClick={onClose}>Cancel</Btn>
        </div>
      </form>
    </Modal>
  );
}
