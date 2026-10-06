// Door B — the native in-browser briefing chat. One surface, three entries: the
// composer's "Brief the team" (a typed goal), and a saved loop's Brief /
// ↩ Debrief (seeded by `name`). The creator asks 1-2 material questions while a
// live team preview (= the config) builds; start/save goes through the same
// validate → save → start seam as every other door.
import { useEffect, useRef, useState } from 'react';
import { creatorErrors, injectProject, type BriefTurn } from '@loopyard/api';
import { Btn, friendlyLine } from '../../components/ui';
import { CreatorModal } from './Modal';
import { useIgnite, type Msg } from './ignite';
import { useBrief, useInvalidateLoops, useValidate } from './queries';
import './brief.css';

export interface BriefChatProps {
  goal?: string;
  /** Seed from a saved loop (Brief / Debrief) instead of a typed goal. */
  name?: string;
  phase?: 'brief' | 'debrief';
  projectId?: string;
  onClose(): void;
}

interface LogEntry {
  who: 'creator' | 'you';
  text: string;
}

export function BriefChat({ goal: goal0 = '', name = '', phase = 'brief', projectId = '', onClose }: BriefChatProps) {
  const brief = useBrief();
  const validate = useValidate();
  const ignite = useIgnite();
  const invalidate = useInvalidateLoops();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [turn, setTurn] = useState<BriefTurn | null>(null);
  const [goal, setGoal] = useState(goal0);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [msg, setMsg] = useState<Msg>({ kind: 'info', text: '' });
  const [busy, setBusy] = useState(false);
  const firstQ = useRef<HTMLInputElement>(null);

  async function runTurn(ans: Record<string, string>) {
    setMsg({ kind: 'info', text: 'Thinking…' });
    let r: BriefTurn;
    try {
      r = await brief.mutateAsync({ phase, answers: ans, ...(name ? { name } : { goal }), ...(projectId ? { projectId } : {}) });
    } catch {
      setMsg({ kind: 'bad', text: "Couldn't reach the creator — try again, or use Draft a team instead." });
      return;
    }
    if (!r || r.ok === false) {
      setMsg({ kind: 'bad', text: "The briefing didn't go through: " + ((r && r.error) || 'no answer from the creator') });
      return;
    }
    setTurn(r);
    if (r.goal) setGoal(r.goal);
    setDraft({});
    setLog((l) => [
      ...l,
      { who: 'creator', text: r.note || (r.done ? 'Looks complete — review the team and start.' : 'Answer these and the team preview updates.') },
    ]);
    setMsg({ kind: 'info', text: '' });
  }

  // The opening turn. (Ref-guarded so StrictMode's double effect doesn't ask twice.)
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void runTurn({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => firstQ.current?.focus(), [turn]);

  function send() {
    const qs = turn?.questions ?? [];
    const parts: string[] = [];
    const next = { ...answers };
    for (const q of qs) {
      const v = (draft[q.key] || '').trim();
      if (v) {
        next[q.key] = v;
        parts.push(v);
      }
    }
    if (!parts.length) return setMsg({ kind: 'bad', text: 'Type an answer (or hit Close / Start to go with the current team).' });
    setAnswers(next);
    setLog((l) => [...l, { who: 'you', text: parts.join(' · ') }]);
    void runTurn(next);
  }

  async function startFromChat() {
    const cfg0 = turn?.preview?.config;
    if (!cfg0) return setMsg({ kind: 'bad', text: 'No team preview to start yet — answer the goal first.' });
    // A new loop binds the composer's project chip; a name-seeded brief keeps the loop's own.
    const cfg = name ? { ...cfg0 } : injectProject(cfg0, projectId);
    setBusy(true);
    try {
      setMsg({ kind: 'info', text: name ? 'Saving the reshaped team…' : 'Assembling & starting…' });
      let vr;
      try {
        vr = await validate.mutateAsync({ config: cfg });
      } catch (e) {
        return setMsg({ kind: 'bad', text: friendlyLine(e, 'build a valid team') });
      }
      if (!vr?.ok || !vr.config) return setMsg({ kind: 'bad', text: 'Couldn’t build a valid team: ' + creatorErrors(vr, 'invalid config') });
      const nm = await ignite(vr.config, !name, (m) => setMsg(m));
      if (nm && name) {
        // reshaping an EXISTING loop — saving is the whole gesture; Run re-adopts it.
        setMsg({ kind: 'ok', text: `Saved “${nm}” — press Run and it picks up this reshaped team.` });
        invalidate();
      }
    } finally {
      setBusy(false);
    }
  }

  const title = phase === 'debrief' ? 'Debrief the manager' : name ? 'Brief the manager' : 'Brief a team';
  const sub =
    phase === 'debrief'
      ? 'Capture or redirect the last run — reshape the team, then Run re-adopts it.'
      : name
        ? 'A live conversation that reshapes this loop’s goal + crew. Run re-adopts the result.'
        : 'Answer a question or two and a team preview builds live — start it when it looks right.';
  const qs = turn?.questions ?? [];
  const prev = turn?.preview;
  const hasPrev = !!prev?.config;
  const roles = prev?.roles ?? [];
  const head = turn?.single_agent ? 'one agent' : roles.length ? `${roles.length}-agent team` : 'team preview';
  const startLabel = name ? (phase === 'debrief' ? 'Save & re-adopt' : 'Save changes') : 'Start loop';

  return (
    <CreatorModal title={title} kick={name ? <span className="mono">{name}</span> : undefined} onClose={onClose}>
      <p className="bc-sub">{sub}</p>
      <div className="bc-body" aria-live="polite">
        {log.length > 0 && (
          <div className="bc-chat">
            {log.map((e, i) => (
              <div key={i} className={`bc-msg ${e.who}`}>
                <span className="bc-who">{e.who === 'you' ? 'You' : 'Creator'}</span>
                <span className="bc-txt">{e.text}</span>
              </div>
            ))}
          </div>
        )}
        {hasPrev && (
          <div className="bc-prev">
            <div className="bc-prevhd">
              Team preview · <b>{head}</b>
            </div>
            {roles.length ? (
              <ul className="bc-roles">
                {roles.map((r, i) => (
                  <li key={r.id + i}>
                    <span className="bc-rid" title={r.id}>{r.id}</span>
                    <span className="bc-why">{r.why || r.role || ''}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="bc-sub">assembling…</div>
            )}
          </div>
        )}
        {qs.length > 0 && (
          <form
            className="bc-form"
            onSubmit={(e) => {
              e.preventDefault();
              send();
            }}
          >
            {qs.map((q, i) => (
              <label key={q.key} className="bc-q">
                <span className="bc-ql">{q.question || q.key}</span>
                <input
                  ref={i === 0 ? firstQ : undefined}
                  className="bc-qi"
                  autoComplete="off"
                  placeholder={q.why ? '— ' + q.why : 'your answer'}
                  value={draft[q.key] ?? ''}
                  onChange={(e) => setDraft((d) => ({ ...d, [q.key]: e.target.value }))}
                />
              </label>
            ))}
            <button type="submit" hidden />
          </form>
        )}
        <div className="bc-acts">
          {qs.length > 0 && (
            <Btn variant="primary" disabled={brief.isPending || busy} onClick={send}>
              {name ? 'Send' : 'Send answers'}
            </Btn>
          )}
          {hasPrev && (
            <Btn
              variant={qs.length ? 'ghost' : 'primary'}
              disabled={busy}
              onClick={() => void startFromChat()}
              title={name ? 'Save this reshaped team; Run re-adopts it' : 'Assemble and start this team now'}
            >
              {startLabel}
            </Btn>
          )}
          <Btn onClick={onClose}>Close</Btn>
        </div>
      </div>
      {msg.text && (
        <div className={`bc-msgline ${msg.kind}`} role="status">
          {msg.text}
        </div>
      )}
    </CreatorModal>
  );
}
