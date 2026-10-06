import { useEffect, useState, type KeyboardEvent } from 'react';
import { ago, firstLine, reportStatusLabel, type TimelineRow } from '@loopyard/api';
import { Icon } from '../../components/icons';
import { Clamp } from './Clamp';

/** Rows shown before "Show all" (no scroll box inside the page scroll). */
export const ROWS_SHOWN = 8;

const short = (ts: number | null | undefined) => (ts ? ago(ts).replace(/ ago$/, '') : '');

/**
 * ONE feed of every turn, newest first — the agents' own notes, the scheduler log
 * and the thought-log merged (see `loopTimeline`). Click a turn to see what the
 * agent was told, what it reported and the end of its transcript.
 */
export function Timeline({ rows, filter, onClearFilter, managerRead, manager, canOpen, onTurn, files, onFiles }: {
  rows: TimelineRow[];
  /** Only this agent's turns ('' = everyone). */
  filter: string;
  onClearFilter(): void;
  /** The manager's latest read, pinned on top (finished runs; a running one shows it in the summary). */
  managerRead?: string | null;
  manager?: string;
  /** Turns open only for loops on this machine. */
  canOpen: boolean;
  onTurn(agent: string, seq: number, rep?: { status?: string | null; note?: string | null }): void;
  /** How many files the loop produced (null = not known yet). */
  files: number | null;
  onFiles(): void;
}) {
  const [all, setAll] = useState(false);
  useEffect(() => setAll(false), [filter]);
  const shownRows = filter ? rows.filter((r) => r.agent === filter) : rows;
  const shown = all ? shownRows : shownRows.slice(0, ROWS_SHOWN);
  // Pinned only when it says something the manager's own turns don't already say.
  const read = (managerRead || '').trim();
  const pinMgr = !!read && (!filter || filter === manager) && !rows.some((r) => r.isManager && r.note.startsWith(read.slice(0, 120)));
  return (
    <section className="ld-section ld-timeline" aria-label="Timeline">
      <div className="ld-tl-head">
        <h2 className="section-h">
          Timeline
          {filter && (
            <span className="ld-tl-filter">
              {' '}· {filter}
              <button type="button" className="ld-linkbtn" onClick={onClearFilter} title="Show everyone's turns">show everyone</button>
            </span>
          )}
        </h2>
        {files !== 0 && (
          <button type="button" className="ld-linkbtn ld-tl-files" onClick={onFiles} title="Browse the files this loop produced">
            {files ? `${files} file${files === 1 ? '' : 's'}` : 'Files'} <Icon name="external" size={12} />
          </button>
        )}
      </div>
      {pinMgr && (
        <div className="ld-tl-mgr">
          <span className="ld-muted">{manager ? `${manager} · ` : ''}latest read</span>
          <Clamp text={read} lines={2} label="manager's note" />
        </div>
      )}
      {shown.length ? (
        <ol className="ld-feed">
          {shown.map((r) => <Row key={r.key} r={r} canOpen={canOpen} onTurn={onTurn} />)}
        </ol>
      ) : (
        !pinMgr && <p className="ld-muted ld-tl-empty">{filter ? `No turns from ${filter} yet.` : 'No turns yet — the agents are starting up.'}</p>
      )}
      {shownRows.length > ROWS_SHOWN && (
        <button type="button" className="ld-linkbtn ld-showall" onClick={() => setAll((a) => !a)} aria-expanded={all}>
          {all ? 'Show fewer' : `Show all ${shownRows.length}`}
        </button>
      )}
    </section>
  );
}

function Row({ r, canOpen, onTurn }: { r: TimelineRow; canOpen: boolean; onTurn(agent: string, seq: number, rep?: { status?: string | null; note?: string | null }): void }) {
  if (r.kind === 'system') {
    return (
      <li className="ld-trow sys" title="What the engine did (shown with Show everything)">
        <span className="tw">{short(r.ts)}</span>
        <span className="ta">{r.label}</span>
        <span className="tn" title={r.note}>{r.note}</span>
      </li>
    );
  }
  const openable = canOpen && r.seq != null;
  const line = firstLine(r.note) || r.note;
  const open = () => openable && onTurn(r.agent, r.seq!, { status: r.status, note: r.note });
  const status = r.status ? reportStatusLabel(r.status) : '';
  return (
    <li
      className={'ld-trow' + (openable ? ' clk' : '') + (r.isManager ? ' mgr' : '')}
      title={openable ? 'Open this turn' : canOpen ? 'From an earlier run of this loop — its details were replaced by the latest run' : undefined}
      {...(openable
        ? { role: 'button', tabIndex: 0, onClick: open, onKeyDown: (k: KeyboardEvent) => (k.key === 'Enter' || k.key === ' ') && (k.preventDefault(), open()) }
        : {})}
    >
      <span className="tw">{short(r.ts)}</span>
      <span className="ta">
        <b>{r.agent}</b>
        {r.seq != null && <> · turn {r.seq}</>}
        {status && <> · {status}</>}
      </span>
      <span className="tn" title={r.note || undefined}>
        {line || <span className="ld-muted">(no note)</span>}
        {r.truncated && <span className="ld-muted"> (clipped)</span>}
      </span>
      {openable && <span className="ld-chev" aria-hidden="true"><Icon name="chevronRight" size={14} /></span>}
    </li>
  );
}
