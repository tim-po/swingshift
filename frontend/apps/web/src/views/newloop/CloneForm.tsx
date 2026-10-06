// Clone a registry loop into a new runnable loop, then open it in the editor.
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { creatorErrors } from '@loopyard/api';
import { Btn } from '../../components/ui';
import { useClone } from '../../features/editor/queries';

export function CloneForm({ from }: { from: string }) {
  const [name, setName] = useState('');
  const [err, setErr] = useState('');
  const clone = useClone();
  const navigate = useNavigate();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const nn = name.trim();
    if (!nn) return setErr('Give the copy a name.');
    setErr('');
    try {
      const res = await clone.mutateAsync({ from, name: nn });
      if (!res || res.error || res.ok === false) return setErr(creatorErrors(res, 'clone failed'));
      navigate(`/newloop?edit=${encodeURIComponent(res.name || nn)}`, { replace: true });
    } catch {
      setErr("Couldn't clone that loop — try again in a moment.");
    }
  }

  return (
    <form className="nl-clone" onSubmit={submit}>
      <h1 className="h1 nl-cloneh">Copy “{from}”</h1>
      <p className="nl-sub">Make a runnable copy — then reshape it in the editor before you run it.</p>
      <label className="nl-lbl" htmlFor="nl-clonename">Name for the copy</label>
      <input
        id="nl-clonename"
        className="input mono"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="my-loop-02"
        autoFocus
        spellCheck={false}
      />
      {err && <div className="nl-msg bad" role="alert">{err}</div>}
      <div className="nl-actions">
        <Btn onClick={() => navigate(-1)}>Cancel</Btn>
        <button type="submit" className="btn primary" disabled={clone.isPending}>
          {clone.isPending ? 'Copying…' : 'Copy & edit'}
        </button>
      </div>
    </form>
  );
}
