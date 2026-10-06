// The in-dashboard loop editor — recursive: agents + nested sub-loops + parallel
// groups, plus the team bindings (Project + Origin). Opened from
// /newloop?edit=<name> (reshape a saved loop) or /newloop?editor=new (compose).
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  EDITOR_ROLES, bindingsLost, creatorErrors, editorBindings, editorSuggestion, edBlankAgent, edBlankNode, edFromNode, edMoveStep, edToNode,
  edUidGen, edUpdateNode, editorPrecheck, projectsOf, type EdAgentStep, type EdNode, type EdStep, type LoopConfig,
  type RegistryAgent,
} from '@loopyard/api';
import { Btn, Empty, friendlyError, friendlyLine, Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { useProjects } from '../../scope';
import { useOriginPickerOptions } from '../../views/origins/OriginPicker';
import { creator, loopPath, useLoopConfig, useRegistryAgents, useSave, useStart } from './queries';
import type { Msg } from './ignite';
import './editor.css';

interface Ops {
  reg: RegistryAgent[];
  collapsed: Set<string>;
  toggle(uid: string): void;
  patch(nodeUid: string, fn: (n: EdNode) => EdNode): void;
  readonlyName: boolean;
}

export function LoopEditor({ name, onCancel }: { name?: string; onCancel?: () => void }) {
  const editing = !!name;
  const uid = useRef(edUidGen()).current;
  const navigate = useNavigate();
  const cfgQ = useLoopConfig(name ?? '');
  const regQ = useRegistryAgents();
  const [root, setRoot] = useState<EdNode | null>(() => {
    if (editing) return null;
    const n = edBlankNode(uid);
    n.steps.push(edBlankAgent(uid, 'manager'));
    return n;
  });
  const [projectId, setProjectId] = useState('');
  const [origin, setOrigin] = useState('');
  const [suggested, setSuggested] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [msg, setMsg] = useState<Msg | null>(null);
  const [busy, setBusy] = useState(false);
  const save = useSave();
  const start = useStart();

  // Seed once from the saved config (never clobber the user's edits on refetch).
  useEffect(() => {
    if (!editing || root || !cfgQ.data) return;
    if (cfgQ.data.error) return;
    const c: LoopConfig = cfgQ.data.config ?? {};
    setRoot(edToNode(c, uid));
    // The explicit binding only — the same one the Loops list shows. A guessed
    // project is offered as a suggestion, never pre-selected (else Save binds it).
    const b = editorBindings(cfgQ.data);
    setProjectId(b.projectId);
    setOrigin(b.origin);
    setSuggested(editorSuggestion(cfgQ.data));
  }, [editing, root, cfgQ.data, uid]);

  const ops: Ops = {
    reg: regQ.data?.agents ?? [],
    collapsed,
    toggle: (u) =>
      setCollapsed((s) => {
        const n = new Set(s);
        if (n.has(u)) n.delete(u);
        else n.add(u);
        return n;
      }),
    patch: (nodeUid, fn) => setRoot((r) => (r ? edUpdateNode(r, nodeUid, fn) : r)),
    readonlyName: editing,
  };

  async function onSave(run: boolean) {
    if (!root) return;
    const cfg = edFromNode(root);
    const pre = editorPrecheck(cfg);
    if (pre) return setMsg({ kind: 'bad', text: pre });
    setBusy(true);
    try {
      // Always write the binding — the schema retains projectId/origin. (A
      // validate-probe used to gate this and silently saved loops unbound.)
      if (projectId) cfg.projectId = projectId;
      if (origin) cfg.origin = origin;
      let res;
      try {
        res = await save.mutateAsync(cfg);
      } catch (e) {
        return setMsg({ kind: 'bad', text: friendlyLine(e, 'save your loop') });
      }
      if (!res || res.error || res.ok === false) return setMsg({ kind: 'bad', text: creatorErrors(res, 'save failed') });
      const nm = res.name || cfg.name || '';
      // Read the saved loop back: if the binding didn't stick, say so — never
      // report a save as bound when it isn't.
      if (projectId || origin) {
        const back = await creator.config(nm).catch(() => null);
        const lost = bindingsLost(back, { projectId, origin });
        if (lost.length)
          return setMsg({ kind: 'bad', text: `Saved “${nm}”, but the ${lost.join(' and ')} didn't stick — the loop is still unbound. Try saving again.` });
      }
      const bindMsg =
        projectId || origin
          ? ' · ' + [projectId && 'project ' + projectId, origin && 'runs on ' + origin].filter(Boolean).join(' · ')
          : '';
      const warn = res.warnings?.length ? ` · ${res.warnings.length} warning${res.warnings.length === 1 ? '' : 's'}` : '';
      setMsg({ kind: 'ok', text: `Saved “${nm}”${warn}${bindMsg}` });
      if (run) {
        let err: string | undefined;
        try {
          err = (await start.mutateAsync(nm))?.error;
        } catch {
          err = "couldn't reach a runner";
        }
        if (err) return setMsg({ kind: 'bad', text: `Saved “${nm}”, but couldn't start it — open it from Loops to run when a runner is attached.` });
      }
      navigate(loopPath(nm));
    } finally {
      setBusy(false);
    }
  }

  const cancel = onCancel ?? (() => navigate(-1));

  let body: React.ReactNode;
  if (editing && cfgQ.isPending) body = <div className="sk-cards">{[0, 1, 2].map((i) => <Skeleton key={i} block={72} lines={0} />)}</div>;
  else if (editing && (cfgQ.error || cfgQ.data?.error)) {
    // Never surface the raw cfgQ.error.message / server error string — calm copy + retry.
    const fe = friendlyError(cfgQ.error, "this loop's setup");
    body = (
      <Empty tone="err" title={fe.title}>
        <p>{fe.detail}</p>
        <Btn variant="primary" onClick={() => cfgQ.refetch()}>Try again</Btn>
      </Empty>
    );
  }
  else if (root)
    body = (
      <>
        <Bindings projectId={projectId} setProjectId={setProjectId} origin={origin} setOrigin={setOrigin} suggested={suggested} />
        <div className="ce-form">
          <NodeForm node={root} top ops={ops} />
        </div>
      </>
    );

  return (
    <div className="ce-page">
      <h1 className="h1 ce-h1">{editing ? 'Edit loop' : 'Build a team by hand'}</h1>
      <p className="ce-hint">
        {editing ? <><span className="mono">{name}</span> · </> : null}
        The goal, the agents in the order they take turns, and where it runs.
      </p>
      {body}
      {msg && (
        <div className={`ce-msg ${msg.kind}`} role="status">
          {msg.text}
        </div>
      )}
      <div className="ce-foot">
        <Btn onClick={cancel}>Cancel</Btn>
        <span className="ce-grow" />
        <Btn disabled={!root || busy} onClick={() => void onSave(false)}>
          Save
        </Btn>
        <Btn variant="primary" disabled={!root || busy} onClick={() => void onSave(true)}>
          Save &amp; run
        </Btn>
      </div>
    </div>
  );
}

function Bindings(p: { projectId: string; setProjectId(v: string): void; origin: string; setOrigin(v: string): void; suggested?: string }) {
  const prods = projectsOf(useProjects().data);
  const origs = useOriginPickerOptions();
  const known = origs.some((o) => o.id === p.origin);
  return (
    <div className="ce-bind">
      <div className="ce-bindrow">
        <label className="ce-bl" title="The project this team works on (optional)">
          Project
          <select className="ce-in" value={p.projectId} disabled={!prods.length && !p.projectId} onChange={(e) => p.setProjectId(e.target.value)} aria-label="Project">
            <option value="">No project</option>
            {prods.map((x) => (
              <option key={x.id} value={x.id}>
                {x.id}
                {x.name && x.name !== x.id ? ' — ' + x.name : ''}
              </option>
            ))}
            {p.projectId && !prods.some((x) => x.id === p.projectId) && <option value={p.projectId}>{p.projectId}</option>}
          </select>
        </label>
        <label className="ce-bl" title="The machine that runs this team (optional)">
          Machine
          <select className="ce-in" value={p.origin} onChange={(e) => p.setOrigin(e.target.value)} aria-label="Machine">
            <option value="">Any machine — decide when it runs</option>
            {origs.map((o) => (
              <option key={o.id} value={o.id}>
                {o.id}
                {o.name && o.name !== o.id ? ' — ' + o.name : ''}
              </option>
            ))}
            {p.origin && !known && <option value={p.origin}>{p.origin}</option>}
          </select>
        </label>
      </div>
      {p.suggested && !p.projectId && (
        <div className="ce-note ce-small">
          Suggested project: <span className="mono">{p.suggested}</span>{' '}
          <button type="button" className="btn quiet sm" onClick={() => p.setProjectId(p.suggested!)}>
            Use it
          </button>
        </div>
      )}
      {!prods.length && !p.projectId && !p.suggested && <div className="ce-note ce-small">No projects yet — add one under Projects to link this team to a repo.</div>}
    </div>
  );
}

function NodeForm({ node, top, ops }: { node: EdNode; top?: boolean; ops: Ops }) {
  const set = (fn: (n: EdNode) => EdNode) => ops.patch(node.uid, fn);
  const setSteps = (fn: (s: EdStep[]) => EdStep[]) => set((n) => ({ ...n, steps: fn(n.steps) }));
  const uid = useMemo(() => edUidGen(node.uid + '_'), [node.uid]);
  const add = (st: EdStep) => setSteps((s) => [...s, st]);
  const hasMgr = node.steps.some((s) => s.kind === 'agent' && s.role === 'manager');

  const addSub = () => {
    const sub = edBlankNode(uid);
    sub.steps.push(edBlankAgent(uid, 'manager'));
    add({ uid: uid(), kind: 'loop', id: '', par: false, loop: sub });
  };
  const addFromReg = (id: string) => {
    const r = ops.reg.find((x) => x.id === id);
    if (r) add({ ...edBlankAgent(uid, r.role), id: r.id, personality: r.personality ?? '', goal: r.goal ?? '' });
  };
  const idp = `ce_${node.uid}`;


  return (
    <div className="ce-node">
      <div className="ce-row2">
        <div className="field ce-grow">
          <label className="field-label" htmlFor={idp + '_name'}>{top ? 'Name' : 'Sub-loop name'}</label>
          <input
            id={idp + '_name'}
            className="ce-in mono"
            placeholder={top ? 'my-loop-01' : 'sub-loop-name'}
            value={node.name}
            readOnly={top && ops.readonlyName}
            title={top && ops.readonlyName ? 'A saved loop keeps its name' : undefined}
            onChange={(e) => set((n) => ({ ...n, name: e.target.value }))}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor={idp + '_turn'} title="The most turns the whole team takes before it wraps up">Turn limit</label>
          <input
            id={idp + '_turn'}
            className="ce-in ce-turn"
            type="number"
            min={1}
            value={node.turnLimit}
            onChange={(e) => set((n) => ({ ...n, turnLimit: parseInt(e.target.value) || 0 }))}
            onBlur={() => set((n) => ({ ...n, turnLimit: n.turnLimit || 30 }))}
          />
        </div>
      </div>
      <div className="field">
        <label className="field-label" htmlFor={idp + '_goal'}>{top ? 'Goal' : 'Sub-loop goal'}</label>
        <textarea
          id={idp + '_goal'}
          className="ce-in"
          rows={top ? 3 : 2}
          placeholder="What should this loop achieve?"
          value={node.goal}
          onChange={(e) => set((n) => ({ ...n, goal: e.target.value }))}
        />
        <span className="field-hint">What the manager keeps the whole team working toward.</span>
      </div>
      <div className="ce-agenthead">
        <span className="ce-agenth" title="Agents take turns top to bottom, one round at a time. Mark a step Parallel to run it alongside the step above.">
          Team <span className="ce-note">· in turn order</span>
        </span>
        <span className="ce-grow" />
        <span className="ce-adds">
          {ops.reg.length > 0 && (
            <select className="ce-in ce-regpick" value="" aria-label="Add an agent from the registry" onChange={(e) => e.target.value && addFromReg(e.target.value)}>
              <option value="">Add a saved agent…</option>
              {ops.reg.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.id} · {a.role.replace('_', ' ')}
                </option>
              ))}
            </select>
          )}
          <button type="button" className="btn sm" onClick={() => add(edBlankAgent(uid, hasMgr ? 'worker' : 'manager'))} title="Add a blank agent">
            <Icon name="plus" size={14} /> Agent
          </button>
          <button type="button" className="btn sm" onClick={addSub} title="Add a nested sub-loop (a team inside the team)">
            <Icon name="plus" size={14} /> Sub-loop
          </button>
        </span>
      </div>
      {node.steps.length === 0 && <div className="ce-note ce-empty">No agents yet — add one.</div>}
      {node.steps.map((st, i) => {
        const patchStep = (fn: (s: EdStep) => EdStep) => setSteps((s) => s.map((x, k) => (k === i ? fn(x) : x)));
        const row = (
          <StepRow
            i={i}
            step={st}
            onPar={() => patchStep((s) => ({ ...s, par: !s.par }))}
            onMove={(d) => setSteps((s) => edMoveStep(s, i, d))}
            onRemove={() => setSteps((s) => s.filter((_, k) => k !== i))}
            onId={(v) => patchStep((s) => ({ ...s, id: v }))}
            collapsed={st.kind === 'loop' && ops.collapsed.has(st.loop.uid)}
            onToggle={st.kind === 'loop' ? () => ops.toggle(st.loop.uid) : undefined}
            onAgent={st.kind === 'agent' ? (p) => patchStep((s) => ({ ...s, ...p }) as EdStep) : undefined}
          />
        );
        const fid = `${idp}_${st.uid}`;
        if (st.kind === 'agent')
          return (
            <div key={st.uid} className="ce-agent">
              {row}
              <div className="field">
                <label className="field-label" htmlFor={fid + '_persona'}>Personality</label>
                <textarea
                  id={fid + '_persona'}
                  className="ce-in"
                  rows={2}
                  placeholder="Who this agent is and how it thinks — stays the same on every loop"
                  value={st.personality}
                  onChange={(e) => patchStep((s) => ({ ...s, personality: e.target.value }))}
                />
              </div>
              <div className="field">
                <label className="field-label" htmlFor={fid + '_goal'}>Goal on this loop</label>
                <textarea
                  id={fid + '_goal'}
                  className="ce-in"
                  rows={2}
                  placeholder="What this agent is responsible for here"
                  value={st.goal}
                  onChange={(e) => patchStep((s) => ({ ...s, goal: e.target.value }))}
                />
              </div>
            </div>
          );
        return (
          <div key={st.uid} className="ce-sub">
            {row}
            {!ops.collapsed.has(st.loop.uid) && (
              <div className="ce-subbody">
                <NodeForm node={st.loop} ops={ops} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function StepRow(p: {
  i: number;
  step: EdStep;
  collapsed: boolean;
  onPar(): void;
  onMove(d: number): void;
  onRemove(): void;
  onId(v: string): void;
  onToggle?: () => void;
  onAgent?: (patch: Partial<EdAgentStep>) => void;
}) {
  const st = p.step;
  const idLabel = st.kind === 'loop' ? 'Step id' : 'Agent id';
  return (
    <div className="ce-arow">
      {st.kind === 'loop' && (
        <button type="button" className="ce-subtoggle" onClick={p.onToggle} aria-expanded={!p.collapsed} title={p.collapsed ? 'Expand' : 'Collapse'}>
          <Icon name={p.collapsed ? 'chevronRight' : 'chevronDown'} size={14} />
          <Icon name="loop" size={14} /> Sub-loop
        </button>
      )}
      <label className="field ce-idf">
        <span className="field-label">{idLabel}</span>
        <input
          className="ce-in mono ce-id"
          placeholder={st.kind === 'loop' ? 'step-id' : 'agent-id'}
          aria-label={st.kind === 'loop' ? 'step id' : 'agent id'}
          title={st.kind === 'loop' ? 'The key for this sub-loop in the parent' : st.id || undefined}
          value={st.id}
          onChange={(e) => p.onId(e.target.value)}
        />
      </label>
      {st.kind === 'agent' && p.onAgent && (
        <>
          <label className="field">
            <span className="field-label">Role</span>
            <select className="ce-in ce-role" aria-label="role" value={st.role} onChange={(e) => p.onAgent?.({ role: e.target.value })}>
              {EDITOR_ROLES.map((r) => (
                <option key={r} value={r}>
                  {r === 'input_provider' ? 'reviewer' : r.replace('_', ' ')}
                </option>
              ))}
              {!EDITOR_ROLES.includes(st.role as (typeof EDITOR_ROLES)[number]) && <option value={st.role}>{st.role}</option>}
            </select>
          </label>
          <label className="field">
            <span className="field-label" title="Optional — how long one turn may take before it times out">Max minutes per turn</span>
            <input
              className="ce-in ce-min"
              type="number"
              min={1}
              placeholder="—"
              aria-label="max minutes per turn"
              value={st.maxTurnMinutes ?? ''}
              onChange={(e) => p.onAgent?.({ maxTurnMinutes: e.target.value ? parseInt(e.target.value) || undefined : undefined })}
            />
          </label>
        </>
      )}
      <span className="ce-mv">
        {p.i > 0 && (
          <button type="button" className={'ce-par' + (st.par ? ' on' : '')} onClick={p.onPar} aria-pressed={st.par}
            title="Run at the same time as the step above">
            Parallel
          </button>
        )}
        <button type="button" onClick={() => p.onMove(-1)} title="Move up" aria-label="move up"><Icon name="chevronUp" size={14} /></button>
        <button type="button" onClick={() => p.onMove(1)} title="Move down" aria-label="move down"><Icon name="chevronDown" size={14} /></button>
        <button type="button" className="rm" onClick={p.onRemove} title="Remove" aria-label="remove step"><Icon name="x" size={14} /></button>
      </span>
    </div>
  );
}
