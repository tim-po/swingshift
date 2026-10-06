// The editable draft team + its INLINE lint: each loop_lint finding sits right under
// the field it's about, in calm words — a hint, never a blocker. A clean draft shows
// no findings at all, just a quiet "looks good".
import { useState } from 'react';
import { Icon } from '../../components/icons';
import { ADDABLE_ROLES, ROLES, ROLE_LABEL, findingTitle, findingsAt, looseFindings, removeAgent, updateAgent, type Draft, type DraftCheck, type LintFinding } from './draft';

function Findings({ items }: { items: LintFinding[] }) {
  if (!items.length) return null;
  return (
    <ul className="nl-lint" aria-label="Suggestions">
      {items.map((f, i) => (
        <li key={(f.rule ?? '') + (f.where ?? '') + i} className={`nl-lintit ${f.level === 'info' ? 'info' : 'warn'}`} data-rule={f.rule}>
          <b>{findingTitle(f)}</b>
          {f.message && <span> — {f.message}</span>}
        </li>
      ))}
    </ul>
  );
}

interface Props {
  draft: Draft;
  onChange: (d: Draft) => void;
  check: DraftCheck | undefined;
  checking: boolean;
  /** "＋ Add an agent" — the Composer asks the real scaffold for the new agent's defaults. */
  onAdd?: (role: string) => void;
  adding?: boolean;
}

export function DraftEditor({ draft, onChange, check, checking, onAdd, adding }: Props) {
  const [addRole, setAddRole] = useState<string>(ADDABLE_ROLES[0]);
  const lints = check?.lints;
  const errors = check?.errors ?? [];
  const clean = !!check && !check.offline && check.ok !== false && !errors.length && !(lints ?? []).length;

  return (
    <section className="nl-draft" aria-label="Draft team">
      <div className="nl-drafthd">
        <b>Your draft team</b>
        <span className="nl-grow" />
        <span className="nl-draftst" role="status" aria-live="polite">
          {checking ? 'Checking…' : clean ? 'Looks good' : lints?.length ? `${lints.length} suggestion${lints.length === 1 ? '' : 's'}` : ''}
        </span>
      </div>

      {check?.offline && (
        <div className="nl-drafterr" role="note">
          Suggestions are taking a short break — keep editing; you can still save.
        </div>
      )}
      {errors.length > 0 && (
        <div className="nl-drafterr" role="note">
          Not quite ready to run yet — {errors.join('; ')}
        </div>
      )}
      <Findings items={looseFindings(lints, draft)} />

      <label className="nl-field">
        <span className="nl-flabel">Loop goal — what the whole team is working toward</span>
        <textarea
          className="nl-fgoal"
          aria-label="Loop goal"
          value={draft.goal}
          onChange={(e) => onChange({ ...draft, goal: e.target.value })}
        />
        <Findings items={findingsAt(lints, 'goal')} />
      </label>

      <ul className="nl-agents">
        {draft.agents.map((a) => (
          <li key={a.id} className="nl-agent" data-agent={a.id}>
            <div className="nl-agenthd">
              <span className="nl-rid">{a.id}</span>
              <select
                aria-label={`${a.id} role`}
                value={a.role}
                onChange={(e) => onChange(updateAgent(draft, a.id, { role: e.target.value }))}
              >
                {ROLES.map((r) => (
                  <option key={r} value={r}>{ROLE_LABEL[r]}</option>
                ))}
                {!(ROLES as readonly string[]).includes(a.role) && <option value={a.role}>{a.role}</option>}
              </select>
              <span className="nl-grow" />
              {a.role !== 'manager' && (
                <button type="button" className="nl-tx" aria-label={`remove ${a.id}`} title="Remove this agent from the draft" onClick={() => onChange(removeAgent(draft, a.id))}>
                  <Icon name="x" size={14} />
                </button>
              )}
            </div>
            <label className="nl-field">
              <span className="nl-flabel">Who they are</span>
              <textarea
                aria-label={`${a.id} personality`}
                value={a.personality}
                onChange={(e) => onChange(updateAgent(draft, a.id, { personality: e.target.value }))}
              />
              <Findings items={findingsAt(lints, `steps.${a.id}.personality`)} />
            </label>
            <label className="nl-field">
              <span className="nl-flabel">Their job on any loop</span>
              <textarea
                aria-label={`${a.id} goal`}
                value={a.goal}
                onChange={(e) => onChange(updateAgent(draft, a.id, { goal: e.target.value }))}
              />
              <Findings items={findingsAt(lints, `steps.${a.id}.goal`)} />
            </label>
          </li>
        ))}
      </ul>

      {onAdd && (
        <div className="nl-addagent">
          <select aria-label="new agent role" value={addRole} onChange={(e) => setAddRole(e.target.value)} disabled={adding}>
            {ADDABLE_ROLES.map((r) => (
              <option key={r} value={r}>{ROLE_LABEL[r]}</option>
            ))}
          </select>
          <button type="button" className="nl-add" disabled={adding} onClick={() => onAdd(addRole)} title="Add another agent — it starts with sensible defaults you can edit">
            {!adding && <Icon name="plus" size={14} />}
            {adding ? 'Adding…' : 'Add an agent'}
          </button>
        </div>
      )}
    </section>
  );
}
