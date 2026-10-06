// Unattributed loops — no explicit project. A loop's guessed project (from its goal
// or git remote) is shown as a suggestion only; nothing is linked until the owner
// accepts it or bulk-assigns (loopyard-bug-1790177434, loopyard-follow-up-1790089835).
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { assignLine, type Project, type UnattributedLoop } from '@loopyard/api';
import { Btn, friendlyLine } from '../../components/ui';
import { projects as projectsClient } from '../../scope/queries';

type Flash = { ok: boolean; text: string };

export function UnattributedSection({ loops, projects, onFlash, onChanged }: {
  loops: UnattributedLoop[];
  projects: Project[];
  onFlash(f: Flash): void;
  onChanged(): void;
}) {
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [target, setTarget] = useState('');
  const assign = useMutation({
    mutationFn: ({ project, names }: { project: string; names: string[] }) => projectsClient.assign(project, names),
    onSuccess: (res) => {
      onFlash({ ok: !res.errors?.length, text: assignLine(res) });
      setPicked(new Set());
      onChanged();
    },
    onError: (e: Error) => onFlash({ ok: false, text: friendlyLine(e, 'assign that project') }),
  });
  if (!loops.length) return null;

  const toggle = (name: string) =>
    setPicked((s) => {
      const n = new Set(s);
      if (n.has(name)) n.delete(name);
      else n.add(name);
      return n;
    });
  const allOn = picked.size === loops.length;
  // The catalog plus any suggested ids not in it yet, so a bulk assign can target either.
  const options = [...new Set([...projects.map((p) => p.id), ...loops.map((l) => l.suggestedProject || '').filter(Boolean)])].sort();

  return (
    <section className="pj-un" aria-label="Unattributed loops">
      <h2 className="section-h">Unattributed <span className="count">{loops.length}</span></h2>
      <p className="pj-unnote">
        These loops aren't linked to a project. Suggestions are guesses from a loop's goal or repo — nothing is linked until you accept one.
      </p>
      <div className="pj-unbar">
        <label className="pj-uncheck">
          <input type="checkbox" checked={allOn} onChange={() => setPicked(allOn ? new Set() : new Set(loops.map((l) => l.name)))} aria-label="Select all unattributed loops" />
          {picked.size ? `${picked.size} selected` : 'Select all'}
        </label>
        {picked.size > 0 && (
          <>
            <select className="input pj-unsel" value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Project to assign">
              <option value="">Choose a project…</option>
              {options.map((id) => (
                <option key={id} value={id}>{id}</option>
              ))}
            </select>
            <Btn variant="primary" size="sm" disabled={!target || assign.isPending} onClick={() => assign.mutate({ project: target, names: [...picked] })}>
              {assign.isPending ? 'Assigning…' : `Assign ${picked.size}`}
            </Btn>
          </>
        )}
      </div>
      <div className="rows">
        {loops.map((l) => (
          <div key={l.name} className="pj-unrow">
            <label className="pj-uncheck">
              <input type="checkbox" checked={picked.has(l.name)} onChange={() => toggle(l.name)} aria-label={`Select ${l.name}`} />
            </label>
            <Link className="pj-unname" to={`/loops/local/${encodeURIComponent(l.name)}`} title={`Open loop ${l.name}`}>{l.name}</Link>
            {l.suggestedProject && (
              <span className="pj-unsug">
                suggested: <span className="mono">{l.suggestedProject}</span>
                <Btn size="sm" disabled={assign.isPending} onClick={() => assign.mutate({ project: l.suggestedProject!, names: [l.name] })} title={`Link ${l.name} to ${l.suggestedProject}`}>
                  Accept
                </Btn>
              </span>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
