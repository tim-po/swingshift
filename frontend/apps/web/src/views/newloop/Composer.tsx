// The New loop page: ONE big prompt box + Start. Everything else is secondary:
//  - quiet selectors under the box (project — only once one exists — and machine),
//    plus "attach a plan or issue" (the goal is pre-filled from it);
//  - "More ways to start": brief the team (chat), draft a team (editable), one agent;
//  - "Advanced": the raw config editor, and a briefing terminal that starts ONLY on
//    an explicit button — opening the disclosure never launches an agent.
import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import {
  PROJECT_UNATTR, appendTail, creatorErrors, creatorWarnings, extractConfig, goalWithTarget, injectProject, projectsOf, soloConfig,
  validateBody, type CreatorTarget, type LoopConfig, type SuggestResponse,
} from '@loopyard/api';
import { Btn, friendlyLine } from '../../components/ui';
import { Icon } from '../../components/icons';
import { Coachmark } from '../../components/Coachmark';
import { useProjectScope, useProjects } from '../../scope';
import { origins } from '../../scope/queries';
import { useOriginPickerOptions } from '../origins/OriginPicker';
import { BriefChat } from '../../features/editor/BriefChat';
import { useIgnite, type Msg } from '../../features/editor/ignite';
import { useCreatorPrompt, useSuggest, useValidate } from '../../features/editor/queries';
import { BriefingTerminal, RUNTIMES } from './BriefingTerminal';
import { addScaffolded, applyDraft, draftFromConfig, scaffoldSpec, type Draft } from './draft';
import { scaffoldDraft, useDraftCheck } from './draftCheck';
import { DraftEditor } from './DraftEditor';
import { DraftSchematic } from './DraftSchematic';
import { TargetPicker } from './TargetPicker';

/** Issues' "Point a loop" (and any other door) can hand the composer a seed via router state. */
interface SeedState {
  goal?: string;
  project?: string;
}

const pretty = (c: unknown) => {
  try {
    return JSON.stringify(c, null, 2);
  } catch {
    return '';
  }
};

export function Composer() {
  const location = useLocation();
  const [params] = useSearchParams();
  const seed = (location.state ?? {}) as SeedState;
  const scope = useProjectScope();
  const projects = projectsOf(useProjects().data);
  const originOpts = useOriginPickerOptions();
  const picker = useQuery({ queryKey: ['origin-picker'], queryFn: origins.picker });

  const [goal, setGoal] = useState(() => (typeof seed.goal === 'string' ? seed.goal : ''));
  const [projectId, setProjectId] = useState(() =>
    typeof seed.project === 'string' ? seed.project : scope.project && scope.project !== PROJECT_UNATTR ? scope.project : '',
  );
  const [origin, setOrigin] = useState('local');
  const [targets, setTargets] = useState<CreatorTarget[]>([]);
  const [msg, setMsg] = useState<Msg>({ kind: 'info', text: '' });
  const [valid, setValid] = useState<LoopConfig | null>(null);
  const [editorText, setEditorText] = useState('');
  const [suggestion, setSuggestion] = useState<SuggestResponse | null>(null);
  // The editable draft: the suggested config (base) + what the tester edits on top.
  const [draftBase, setDraftBase] = useState<LoopConfig | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [advanced, setAdvanced] = useState(false);
  // The briefing terminal launches a loop-creator agent on the server, so it only
  // mounts after an explicit "Start briefing terminal" — never on opening Advanced.
  const [termOn, setTermOn] = useState(false);
  const [runtime, setRuntime] = useState(() => {
    const r = params.get('runtime');
    return r && (RUNTIMES as readonly string[]).includes(r) ? r : 'claude';
  });
  const [picking, setPicking] = useState(false);
  const [briefing, setBriefing] = useState(false);
  const [busy, setBusy] = useState(false);
  const termBuf = useRef('');
  const goalRef = useRef<HTMLTextAreaElement>(null);

  const suggest = useSuggest();
  const validate = useValidate();
  const ignite = useIgnite();
  const prompt = useCreatorPrompt(termOn);
  const draftConfig = useMemo(
    () => (draftBase && draft ? injectProject(applyDraft(draftBase, draft), projectId) : null),
    [draftBase, draft, projectId],
  );
  const check = useDraftCheck(draftConfig);

  // "Start a loop from here" (Sessions): a one-shot origin seed — only sticks if
  // that session is a live, non-disabled picker option; else say so plainly.
  const originSeed = useRef(params.get('origin'));
  useEffect(() => {
    const s = originSeed.current;
    if (!s || !picker.isFetched) return;
    originSeed.current = null;
    const opt = originOpts.find((o) => o.id === s && !o.disabled);
    if (opt) setOrigin(s);
    else setMsg({ kind: 'bad', text: `Session ${s.replace('session:', '')} isn't reachable right now — pick another machine.` });
  }, [originOpts, picker.isFetched]);

  // Surface which context the briefing terminal is seeded with (once it's started).
  useEffect(() => {
    if (!termOn || !prompt.data) return;
    const docs = prompt.data.context_docs ?? [];
    setMsg({
      kind: 'info',
      text:
        'The briefing terminal has read ' +
        (docs.length ? docs.join(', ') : 'the default brief') +
        '. When it writes a config, use "Apply from terminal", then Validate and Save.',
    });
  }, [termOn, prompt.data]);

  const invalidate = () => setValid(null);
  const focusGoal = () => goalRef.current?.focus();
  const bad = (text: string) => setMsg({ kind: 'bad', text });

  // Keep a validated config's project binding in lock-step with the chip.
  function changeProject(pid: string) {
    setProjectId(pid);
    if (valid) {
      const next = injectProject(valid, pid);
      setValid(next);
      setEditorText(pretty(next));
    }
  }

  /** Validate through the REAL schema endpoint; on success hold + show the config. */
  async function validateConfig(cfg: LoopConfig): Promise<LoopConfig | null> {
    let res;
    try {
      res = await validate.mutateAsync({ config: cfg });
    } catch (e) {
      bad(friendlyLine(e, 'build a valid team'));
      return null;
    }
    if (res?.ok && res.config) {
      const v = injectProject(res.config, projectId); // the server keeps projectId; re-assert
      setValid(v);
      setEditorText(pretty(v));
      return v;
    }
    setValid(null);
    bad("Couldn't build a valid team: " + creatorErrors(res, 'invalid config'));
    return null;
  }

  async function guarded(fn: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  }

  /** ▶ Start loop: a config validated in Advanced wins; else assemble from the goal. */
  const start = () =>
    guarded(async () => {
      if (valid) {
        const v = injectProject(valid, projectId);
        setEditorText(pretty(v));
        return ignite(v, true, setMsg);
      }
      if (draftConfig) {
        const v = await validateConfig(draftConfig);
        if (v) await ignite(v, true, setMsg);
        return;
      }
      const g = goal.trim();
      if (!g) {
        bad('Describe what you want done first — or attach a plan or issue.');
        return focusGoal();
      }
      setMsg({ kind: 'info', text: 'Assembling the team…' });
      let res;
      try {
        res = await suggest.mutateAsync(g);
      } catch (e) {
        return bad(friendlyLine(e, 'assemble a team'));
      }
      if (!res?.ok || !res.config) return bad("Couldn't assemble a team: " + creatorErrors(res, 'could not assemble a team'));
      const v = await validateConfig(injectProject(res.config, projectId));
      if (v) await ignite(v, true, setMsg);
    });

  /** "Ask one agent" — the loop of one, through the same validate + save+start seam. */
  const askOne = () =>
    guarded(async () => {
      const g = goal.trim();
      if (!g) {
        bad('Describe what the agent should do first.');
        return focusGoal();
      }
      setMsg({ kind: 'info', text: 'Setting up a single agent…' });
      const v = await validateConfig(injectProject(soloConfig(g), projectId));
      if (v) await ignite(v, true, setMsg);
    });

  /** Draft a team: the real creator (loop_creator_suggest) derives an EDITABLE team
   *  from the goal; the draft editor + live lint + schematic take it from there. */
  const preview = () =>
    guarded(async () => {
      const g = goal.trim();
      if (!g) {
        bad('Describe your goal first — the team is derived from it.');
        return focusGoal();
      }
      setMsg({ kind: 'info', text: 'Drafting a team from your goal…' });
      let res;
      try {
        res = await suggest.mutateAsync(g);
      } catch (e) {
        return bad(friendlyLine(e, 'suggest a team'));
      }
      if (!res?.ok) return bad("Couldn't suggest a team: " + creatorErrors(res, 'could not suggest a team'));
      setSuggestion(res);
      const base = res.config ?? {};
      setDraftBase(base);
      setDraft(draftFromConfig(base));
      setEditorText(pretty(injectProject(base, projectId)));
      setValid(null);
      setMsg({ kind: 'ok', text: 'Drafted a team from your goal — edit anything below; suggestions appear as you type.' });
    });

  const brief = () => {
    if (!goal.trim()) {
      bad('Describe a goal first — the briefing chat builds a team from it.');
      return focusGoal();
    }
    setBriefing(true);
  };

  function attach(t: CreatorTarget) {
    setPicking(false);
    if (targets.some((x) => x.kind === t.kind && x.id === t.id)) return;
    setTargets((ts) => [...ts, t]);
    setGoal((g) => goalWithTarget(g, t));
    invalidate();
    setMsg({ kind: 'info', text: `Attached a ${t.kind === 'doc' ? 'plan' : t.kind} — the goal is filled in from it; edit it, then Start.` });
  }

  // ── Advanced ──
  function applyFromTerminal() {
    const cfg = extractConfig(termBuf.current);
    if (!cfg) return bad('The terminal hasn\'t written a config yet — ask it for one first.');
    let out = cfg;
    try {
      out = pretty(JSON.parse(cfg));
    } catch {
      /* keep raw */
    }
    setEditorText(out);
    invalidate();
    setMsg({ kind: 'info', text: 'Applied the terminal\'s config. Validate to check it.' });
  }

  const validateText = () =>
    guarded(async () => {
      const text = editorText.trim();
      if (!text) return bad('The editor is empty.');
      setMsg({ kind: 'info', text: 'Checking the config…' });
      let res;
      try {
        res = await validate.mutateAsync(validateBody(text));
      } catch (e) {
        return bad(friendlyLine(e, 'check that config'));
      }
      if (res?.ok && res.config) {
        setEditorText(pretty(res.config));
        setValid(res.config);
        const w = creatorWarnings(res);
        setMsg({ kind: 'ok', text: 'Looks valid' + (w.length ? ' — warnings: ' + w.join('; ') : ' — ready to save.') });
      } else {
        setValid(null);
        bad('Invalid: ' + creatorErrors(res, 'invalid config'));
      }
    });

  function editDraft(d: Draft) {
    setDraft(d);
    if (draftBase) setEditorText(pretty(injectProject(applyDraft(draftBase, d), projectId)));
    invalidate();
  }

  /** ＋ Add an agent: the real creator scaffold (loop_creator_scaffold) gives the new
   *  agent its role defaults; everything already in the draft is kept as edited. */
  const addAgent = (role: string) =>
    guarded(async () => {
      if (!draft || !draftBase) return;
      let res;
      try {
        res = await scaffoldDraft(scaffoldSpec(draft, role));
      } catch (e) {
        return bad(friendlyLine(e, 'add an agent'));
      }
      if (!res?.ok || !res.config) return bad("Couldn't add an agent: " + creatorErrors(res, 'please try again'));
      const next = addScaffolded(draftBase, draft, res.config);
      const added = next.draft.agents.slice(draft.agents.length).map((a) => a.id);
      setDraftBase(next.base);
      setDraft(next.draft);
      setEditorText(pretty(injectProject(applyDraft(next.base, next.draft), projectId)));
      invalidate();
      setMsg({ kind: 'ok', text: added.length ? `Added ${added.join(', ')} — it starts with sensible defaults; make it yours below.` : 'Your team already has that covered.' });
    });

  /** Save (or save + start) the edited draft — through the same validate + ignite seam. */
  const saveDraft = (run: boolean) =>
    guarded(async () => {
      if (!draftConfig) return;
      const v = await validateConfig(draftConfig);
      if (v) await ignite(v, run, setMsg);
    });

  const saveValid = (run: boolean) =>
    guarded(async () => {
      if (!valid) return bad('Validate a config first.');
      await ignite(valid, run, setMsg);
    });

  const machineLabel = (o: (typeof originOpts)[number]) =>
    (o.id === 'local' ? 'This machine' : o.label || o.name || o.id) + (o.disabled && o.note ? ` — ${o.note}` : '');

  return (
    <div className={draft ? 'nl-composer nl-wide' : 'nl-composer'}>
      <header className="nl-hd">
        <Coachmark
          id="newloop.prompt"
          side="bottom"
          title="Describe the outcome"
          body="A sentence or two is enough — Swingshift puts together a small team of agents for it. You can watch and step in any time."
        >
          <h1 className="nl-q">What do you want done?</h1>
        </Coachmark>
      </header>
      {/* The first-visit explanation is the newloop.prompt tip above (Coachmark),
          so the page doesn't say the same thing twice. */}

      {targets.length > 0 && (
        <div className="nl-targets" aria-live="polite">
          {targets.map((t, i) => (
            <div key={t.kind + t.id} className="nl-targ">
              <span className="nl-tk">{t.kind === 'doc' ? 'Plan' : 'Issue'}</span>
              <span className="nl-tt" title={t.text}>{t.text}</span>
              <button
                type="button"
                className="nl-tx"
                title="Remove"
                aria-label="remove target"
                onClick={() => {
                  setTargets((ts) => ts.filter((_, k) => k !== i));
                  invalidate();
                }}
              >
                <Icon name="x" size={14} />
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="nl-box">
        <textarea
          ref={goalRef}
          className="nl-goal"
          spellCheck={false}
          aria-label="What do you want done?"
          placeholder="e.g. Build the checkout API, add tests, and fix whatever they find"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
              e.preventDefault();
              void start();
            }
          }}
        />
        <div className="nl-boxbar">
          {projects.length > 0 && (
            <label className="nl-sel" title="The project this loop works on">
              <Icon name="project" size={14} />
              <select value={projectId} onChange={(e) => changeProject(e.target.value)} aria-label="Project this loop targets">
                <option value="">No project</option>
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>{p.name || p.id}</option>
                ))}
                {projectId && !projects.some((p) => p.id === projectId) && <option value={projectId}>{projectId}</option>}
              </select>
            </label>
          )}
          {originOpts.length > 0 && (
            <label className="nl-sel" title="The machine this loop runs on">
              <Icon name="machine" size={14} />
              <select value={origin} onChange={(e) => setOrigin(e.target.value)} aria-label="Machine">
                {originOpts.map((o) => (
                  <option key={o.id} value={o.id} disabled={!!o.disabled}>{machineLabel(o)}</option>
                ))}
              </select>
            </label>
          )}
          <button type="button" className="nl-sel nl-attach" onClick={() => setPicking(true)} title="Start from a plan or an issue — its text fills in the goal">
            <Icon name="link" size={14} /> Attach
          </button>
          <span className="nl-grow" />
          <span className="nl-hint">⌘/Ctrl + Enter</span>
          <button type="button" className="btn primary lg nl-lg" disabled={busy} onClick={() => void start()} title="Put a team together and start">
            Start loop
          </button>
        </div>
      </div>

      <div className={`nl-msg ${msg.kind}`} role="status" aria-live="polite">
        {msg.text}
      </div>

      {draft && (
        <div className="nl-drafting">
          <div className="nl-draftmain">
            {suggestion?.signals?.length ? (
              <div className="nl-sugfrom">Drafted from your goal · {suggestion.signals.join(', ')}</div>
            ) : null}
            <DraftEditor draft={draft} onChange={editDraft} check={check.data} checking={check.isFetching} onAdd={(r) => void addAgent(r)} adding={busy} />
            <div className="nl-draftact">
              <Btn variant="primary" disabled={busy} onClick={() => void saveDraft(true)} title="Save this team and start it now">
                Save &amp; start
              </Btn>
              <Btn disabled={busy} onClick={() => void saveDraft(false)} title="Save it to Loops without starting">
                Save draft
              </Btn>
              <span className="nl-draftnext">Nothing runs until you start it, and you can stop it any time.</span>
            </div>
          </div>
          <DraftSchematic schematic={check.data?.schematic} pending={check.isFetching} />
        </div>
      )}

      <div className="nl-more" role="group" aria-label="More ways to start">
        <Coachmark
          id="newloop.more"
          side="top"
          title="More ways to start"
          body="Want a say in the team before it starts? Brief it, draft and edit it, or hand the job to one agent."
        >
          <span className="nl-more-k">More ways to start</span>
        </Coachmark>
        <button type="button" className="nl-way" disabled={busy} onClick={brief} title="Answer a question or two while a team preview builds, then start">
          <Icon name="chat" size={14} /> Brief the team
        </button>
        <button type="button" className="nl-way" disabled={busy} onClick={() => void preview()} title="Draft an editable team from your goal — nothing is saved until you say so">
          <Icon name="sliders" size={14} /> Draft a team
        </button>
        <button type="button" className="nl-way" disabled={busy} onClick={() => void askOne()} title="A single agent that decides when it's done">
          <Icon name="role" size={14} /> Ask one agent
        </button>
        <Link className="nl-way" to="/newloop?editor=new" title="Compose the team by hand in the full editor">
          <Icon name="edit" size={14} /> Build by hand
        </Link>
      </div>

      <details className="nl-adv" open={advanced} onToggle={(e) => setAdvanced((e.target as HTMLDetailsElement).open)}>
        <summary>Advanced</summary>
        <div className="nl-advbody">
          <div className="nl-advhd">
            <span className="nl-t">Loop config</span>
            <span className="nl-grow" />
            {termOn && (
              <button type="button" className="btn sm quiet" onClick={applyFromTerminal}>
                Apply from terminal
              </button>
            )}
            <button type="button" className="btn sm" onClick={() => void validateText()} disabled={busy}>
              Validate
            </button>
            <button type="button" className="btn sm" onClick={() => void saveValid(false)} disabled={!valid || busy}>
              Save
            </button>
            <button type="button" className="btn sm" onClick={() => void saveValid(true)} disabled={!valid || busy} title="Save this team and start it now">
              Save &amp; run
            </button>
          </div>
          <textarea
            className="nl-editor"
            spellCheck={false}
            aria-label="Loop config editor"
            value={editorText}
            onChange={(e) => {
              setEditorText(e.target.value);
              invalidate();
            }}
            placeholder="The config (JSON) appears here as you draft a team. You can also paste or hand-edit one, then Validate and Save."
          />
          {termOn ? (
            <>
              <BriefingTerminal
                runtime={runtime}
                setRuntime={setRuntime}
                onOutput={(c) => {
                  termBuf.current = appendTail(termBuf.current, c);
                }}
              />
              <div className="nl-termfoot">
                <button type="button" className="btn sm quiet" onClick={() => setTermOn(false)} title="Close the terminal (the creator agent is stopped)">
                  Close terminal
                </button>
              </div>
            </>
          ) : (
            <div className="nl-termoff">
              <div className="nl-termoff-text">
                <b>Briefing terminal</b>
                <span>Talk to a loop-creator agent in a terminal; it writes a config you can apply above. Starting it launches an agent on this machine.</span>
              </div>
              <label className="nl-rt">
                Runtime
                <select className="select" value={runtime} onChange={(e) => setRuntime(e.target.value)} aria-label="Creator runtime">
                  {RUNTIMES.map((r) => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
              </label>
              <button type="button" className="btn sm" onClick={() => setTermOn(true)}>
                <Icon name="terminal" size={14} /> Start briefing terminal
              </button>
            </div>
          )}
        </div>
      </details>

      {picking && <TargetPicker scope={scope.project} onPick={attach} onClose={() => setPicking(false)} />}
      {briefing && <BriefChat goal={goal.trim()} projectId={projectId} phase="brief" onClose={() => setBriefing(false)} />}
    </div>
  );
}
