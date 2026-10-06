import { useState } from 'react';
import { ago, downloadAllUrl, fileUrl, firstLine, fmtSize, reportStatusLabel, type AnalysisResult, type RosterCard } from '@loopyard/api';
import { QueryState } from '../../components/ui';
import { Icon, type IconName } from '../../components/icons';
import { config } from '../../config';
import { useAgentReports, useFiles, useTurn } from './hooks';
import { useLoopOwnerScope } from './ownerScope';
import { Modal } from './Modal';
import { AgentIdentity, sentence } from './Team';

/** What the row that opened a turn already knows — its own report. */
export interface TurnReport {
  status?: string | null;
  note?: string | null;
}

export type ModalState =
  | { kind: 'turn'; agent: string; seq: number; rep?: TurnReport }
  | { kind: 'reports'; agent: string; card?: RosterCard }
  | { kind: 'files' }
  | { kind: 'analysis'; result: AnalysisResult }
  | { kind: 'save' }
  | { kind: 'brief'; phase: 'brief' | 'debrief'; attach: string; resumed?: boolean }
  | { kind: 'confirm'; text: string; body?: string; yes: string; onYes(): void | Promise<void> };

interface Props {
  m: ModalState;
  loop: string;
  host: string;
  close(): void;
  open(m: ModalState): void;
  saveToRegistry(note: string): Promise<boolean>;
}

export function DetailModal({ m, loop, host, close, open, saveToRegistry }: Props) {
  switch (m.kind) {
    case 'turn':
      return <TurnModal loop={loop} host={host} agent={m.agent} seq={m.seq} rep={m.rep} close={close} />;
    case 'reports':
      return <ReportsModal loop={loop} host={host} agent={m.agent} card={m.card} close={close} openTurn={(seq, rep) => open({ kind: 'turn', agent: m.agent, seq, rep })} />;
    case 'files':
      return <FilesModal loop={loop} close={close} />;
    case 'analysis':
      return <AnalysisModal r={m.result} loop={loop} close={close} />;
    case 'save':
      return <SaveModal loop={loop} close={close} save={saveToRegistry} />;
    case 'brief':
      return (
        <Modal title={m.phase === 'debrief' ? 'Debrief the manager' : 'Brief the manager'} onClose={close}
          hint={m.phase === 'debrief'
            ? `The same manager is back${m.resumed ? ' with everything it knew from the last round' : ''}. Attach to it in a terminal, talk it through, then press Run — it picks up where it left off.`
            : "A fresh manager session that has read this loop is running. Attach to it in a terminal, reshape the goal and team, then press Run — this session becomes the manager."}>
          <div className="ld-field-label">Attach command — run it on the loop's machine</div>
          <pre className="ld-pre ld-copy">{m.attach || 'No attach command was returned.'}</pre>
        </Modal>
      );
    case 'confirm':
      return (
        <Modal title={m.text} onClose={close}>
          {m.body && <p className="ld-mtext">{m.body}</p>}
          <div className="ld-mactions">
            <button className="btn quiet" onClick={close}>Cancel</button>
            <button className="btn primary" onClick={() => { close(); void m.onYes(); }}>{m.yes}</button>
          </div>
        </Modal>
      );
  }
}

/** The engine found no framed prompt for this turn (one-shot workers get theirs on stdin). */
const noPrompt = (err: string | undefined) => !!err && /no framed prompt/i.test(err);

function TurnModal({ loop, host, agent, seq, rep, close }: { loop: string; host: string; agent: string; seq: number; rep?: TurnReport; close(): void }) {
  const q = useTurn(loop, { agent, seq }, host);
  const r = q.data;
  // The row's own report is the truth for the row that was clicked; the server's is
  // matched by index over the whole history and can belong to an earlier run.
  const report = rep && (rep.note || rep.status) ? rep : r?.report ?? null;
  const st = report?.status;
  const reportBlock = (
    <>
      <h3 className="ld-subh">What it reported</h3>
      {report ? (
        <div className="ld-report">
          {st && <span className="ld-report-st">{sentence(reportStatusLabel(st))}</span>}
          <p>{report.note || 'No note.'}</p>
        </div>
      ) : (
        <p className="ld-muted">No report for this turn.</p>
      )}
    </>
  );
  return (
    <Modal title={`${agent} · turn ${seq}`} onClose={close} wide>
      <QueryState isPending={q.isPending} error={q.error} what="turn" onRetry={() => q.refetch()} />
      {r?.error && (
        <>
          {reportBlock}
          <p className="ld-muted">
            {noPrompt(r.error)
              ? "The exact instructions for this turn weren't kept — this agent runs as a one-shot, and only saved prompts can be shown."
              : "Couldn't load the rest of this turn just now."}
          </p>
        </>
      )}
      {r && !r.error && (
        <>
          {reportBlock}
          <h3 className="ld-subh" title="The framed prompt the engine handed the agent">What it was told</h3>
          <pre className="ld-pre">{r.prompt || '(none)'}</pre>
          {r.transcript_tail && (
            <>
              <h3 className="ld-subh">End of its transcript</h3>
              <pre className="ld-pre">{r.transcript_tail}</pre>
            </>
          )}
        </>
      )}
    </Modal>
  );
}

function ReportsModal({ loop, host, agent, card, close, openTurn }: { loop: string; host: string; agent: string; card?: RosterCard; close(): void; openTurn(seq: number, rep?: TurnReport): void }) {
  const q = useAgentReports(loop, agent, host);
  const reps = q.data?.reports ?? [];
  return (
    <Modal title={agent} onClose={close}>
      {card && <AgentIdentity c={card} />}
      <h3 className="ld-subh">Turn by turn <span className="ld-muted">— newest first</span></h3>
      <QueryState isPending={q.isPending} error={q.error} what="report history" onRetry={() => q.refetch()} />
      {q.data?.error && <p className="ld-err">Couldn't load this history just now — try reopening it.</p>}
      {q.data && !q.data.error && (
        reps.length ? (
          <div className="ld-reghist">
            {reps.map((rp) => (
              <button key={rp.seq} className="ld-regrow" onClick={() => openTurn(rp.seq, { status: rp.status, note: rp.note })} title="Open this turn">
                <span className="rid">Turn {rp.seq}{rp.status ? ` · ${reportStatusLabel(rp.status)}` : ''}</span>
                <span className="rn">{firstLine(rp.note) || rp.note || ''}</span>
                <span className="ld-ago">{rp.ts ? ago(rp.ts) : ''}</span>
              </button>
            ))}
          </div>
        ) : (
          <p className="ld-muted">No turns yet.</p>
        )
      )}
    </Modal>
  );
}

const FILE_ICON: Record<string, IconName> = { html: 'globe', svg: 'image', image: 'image', text: 'files' };

function FilesModal({ loop, close }: { loop: string; close(): void }) {
  const q = useFiles(loop, true);
  const owner = useLoopOwnerScope();
  const r = q.data;
  const url = (p: string, dl = false) => fileUrl(config.apiBase, loop, p, dl, owner);
  return (
    <Modal title="Files" onClose={close} wide>
      <QueryState isPending={q.isPending} error={q.error} what="files" onRetry={() => q.refetch()} />
      {r?.error && <p className="ld-err">Couldn't load files just now — try reopening this.</p>}
      {r && !r.error && (!r.artifacts?.length || !r.count ? (
        <p className="ld-muted">
          No files yet. When the loop finishes, its report and transcripts land here (in <code>_output/{loop}/</code>).
        </p>
      ) : (
        <>
          <div className="ld-mtoolbar">
            <span className="ld-muted">{r.count} file{r.count === 1 ? '' : 's'} · web pages and images open in a new tab</span>
            <a className="btn sm" href={downloadAllUrl(config.apiBase, loop, owner)}><Icon name="download" /> Download all</a>
          </div>
          {r.artifacts.map((g) => (
            <div key={g.dir} className="ld-fgroup">
              <h3 className="ld-subh">
                {g.label ? sentence(g.label) : 'Workspace'} <span className="ld-muted mono" title={g.dir}>{g.dir}</span>
              </h3>
              <div className="ld-filelist">
                {g.files.map((f) => (
                  <div key={f.path} className="ld-frow">
                    <a className="fmain" href={url(f.path)} target="_blank" rel="noopener" title={f.path}>
                      <Icon name={FILE_ICON[f.kind || ''] || 'files'} />
                      <span className="fp mono">{f.path}</span>
                    </a>
                    <span className="fsz">{fmtSize(f.size)}</span>
                    <a className="dl" href={url(f.path, true)} title={`Download ${f.path}`} aria-label={`Download ${f.path}`}><Icon name="download" /></a>
                  </div>
                ))}
              </div>
            </div>
          ))}
          {r.capped && <p className="ld-muted">Showing the first {r.count} files.</p>}
        </>
      ))}
    </Modal>
  );
}

const ANALYSIS_KEYS: [string, string][] = [
  ['turns_used', 'Turns'],
  ['wall_clock_min', 'Minutes'],
  ['distinct_agents', 'Agents'],
  ['report_count', 'Reports'],
  ['parallel_groups', 'Parallel groups'],
  ['subloops', 'Sub-loops'],
  ['winddown_turns', 'Wrap-up turns'],
];

/** The overview as paragraphs (it arrives as one long string with blank-line breaks). */
const paragraphs = (s: string) => s.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);

function AnalysisModal({ r, loop, close }: { r: AnalysisResult; loop: string; close(): void }) {
  const t = r.telemetry ?? {};
  const pa = Object.entries(t.per_agent ?? {});
  const g = t.turn_gap_min;
  const stats = ANALYSIS_KEYS.map(([k, label]) => [label, t[k]] as const).filter(([, v]) => v != null && v !== '');
  return (
    <Modal title={`Analysis · ${r.name || loop}`} onClose={close} wide>
      {stats.length > 0 && (
        <dl className="ld-kv">
          {stats.map(([label, v]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{String(v)}</dd>
            </div>
          ))}
        </dl>
      )}
      {r.ai_overview && (
        <>
          <h3 className="ld-subh">Overview</h3>
          <div className="ld-prose">
            {paragraphs(r.ai_overview).map((p, i) => <p key={i}>{p}</p>)}
          </div>
        </>
      )}
      <h3 className="ld-subh">By agent</h3>
      {pa.length ? (
        <div className="rows ld-rows">
          {pa.map(([a, v]) => (
            <div key={a} className="row static">
              <span className="grow">{a}</span>
              <span className="end">
                {v.turns} turn{v.turns === 1 ? '' : 's'}
                {Object.keys(v.statuses ?? {}).length ? ' · ' + Object.keys(v.statuses ?? {}).map(reportStatusLabel).join(', ') : ''}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <p className="ld-muted">No per-agent numbers.</p>
      )}
      {g?.n ? (
        <p className="ld-muted" title={`n=${g.n} · min ${g.min} · mean ${g.mean} · max ${g.max}`}>
          Time between turns: typically {g.median} min (from {g.min} to {g.max}).
        </p>
      ) : null}
      {t.note && <p className="ld-muted">{t.note}</p>}
    </Modal>
  );
}

function SaveModal({ loop, close, save }: { loop: string; close(): void; save(note: string): Promise<boolean> }) {
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  return (
    <Modal title="Save to registry" onClose={close}>
      <p className="ld-mtext">Keep <b>{loop}</b> as a reusable loop you can clone later.</p>
      <label className="field">
        <span className="field-label">Note (optional)</span>
        <input className="input" value={note} onChange={(e) => setNote(e.target.value)} autoFocus />
      </label>
      <div className="ld-mactions">
        <button className="btn quiet" onClick={close}>Cancel</button>
        <button
          className="btn primary"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            if (await save(note)) close();
            setBusy(false);
          }}
        >
          Save
        </button>
      </div>
    </Modal>
  );
}
