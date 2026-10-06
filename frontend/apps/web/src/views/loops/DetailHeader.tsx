import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ago, FINISHED_STATES, loopBadgeState, loopProject, RUNNING_STATES, stateLabel, STEER_WAITING, type LoopDetail, type LoopStatus } from '@loopyard/api';
import { Icon, type IconName } from '../../components/icons';
import { Clamp } from './Clamp';

export interface HeaderActions {
  act(action: 'start' | 'stop' | 'archive' | 'unarchive'): void;
  brief(phase: 'brief' | 'debrief'): void;
  /** Better-UX #6 — gentle stop → manager hand-off → restart from the same point. */
  steer(): void;
  /** Steer step 3 — restart from the same turn with the steered manager. */
  restart(): void;
  files(): void;
  analyze(): void;
  editConfig(): void;
  saveToRegistry(): void;
}

/**
 * Name + ONE status badge (the same word the list uses — `loopBadgeState`), the
 * primary action, and everything else one click away in ⋯. The team-room STATE
 * stays on the badge as `data-state` + tooltip; its running sub-phase is shown in
 * the progress line, never as a second status.
 */
export function DetailHeader({ d, status, goal, steerPhase = '', busy, on }: {
  d: LoopDetail;
  status?: LoopStatus | null;
  /** The goal to show (full when known — see `goalText`). */
  goal?: string;
  steerPhase?: string;
  busy: boolean;
  on: HeaderActions;
}) {
  const key = loopBadgeState(d, status);
  const proj = loopProject(d);
  const when = ago(d.updated || d.started);
  const remote = d.host !== 'local';
  return (
    <header className="ld-header">
      <div className="ld-head">
        <div className="ld-titlewrap">
          <h1 className="ld-name" title={d.slug ? `${d.name} · reference ${d.slug}` : d.name}>{d.name}</h1>
          <span className={`badge b-${key} ld-state`} data-state={status?.value || key} title={status?.reason || undefined}>
            {stateLabel(key)}
          </span>
        </div>
        <Controls d={d} steerPhase={steerPhase} busy={busy} on={on} />
      </div>
      <div className="ld-meta">
        {when && <span>Updated {when}</span>}
        {proj && (
          <Link to="/projects" title="Open Projects">{d.productName || proj}</Link>
        )}
        {remote && <span title="The machine this loop runs on">on {d.host}</span>}
        {d.origin && d.origin !== d.host && d.origin !== 'local' && <span title="The machine this loop is set to run on">runs on {d.origin}</span>}
      </div>
      {goal && <Clamp className="ld-goal" lines={3} text={goal} label="goal" />}
      {remote && <p className="ld-remote">Read-only here — this loop runs on {d.host}.</p>}
    </header>
  );
}

interface MenuItem {
  label: string;
  icon: IconName;
  title?: string;
  disabled?: boolean;
  onSelect(): void;
}

function Controls({ d, steerPhase, busy, on }: { d: LoopDetail; steerPhase: string; busy: boolean; on: HeaderActions }) {
  if (d.host !== 'local') return null;
  const st = d.state || 'saved';
  const finished = FINISHED_STATES.has(st);
  // Mid-Steer the loop is paused, not finished: the play button RESTARTS from the
  // same turn (once the manager hand-off is ready) instead of a fresh "Rerun".
  const steering = STEER_WAITING.has(steerPhase);
  const running = RUNNING_STATES.has(st);
  const items: MenuItem[] = [
    { label: 'Brief the manager', icon: 'brief', disabled: busy, onSelect: () => on.brief('brief'),
      title: 'Start a fresh manager session that reads the whole loop first; Run then adopts it' },
    { label: 'Debrief the manager', icon: 'undo', disabled: busy, onSelect: () => on.brief('debrief'),
      title: 'Resume the same manager with its whole context; Run then re-adopts it' },
    ...(finished ? [{ label: 'Analyze this run', icon: 'chart' as const, onSelect: on.analyze, title: 'Numbers and an overview of the finished run' }] : []),
    { label: 'Edit setup', icon: 'edit', onSelect: on.editConfig, title: 'Change the goal, team, project or machine' },
    { label: d.archived ? 'Unarchive' : 'Archive', icon: 'archive', disabled: busy, onSelect: () => on.act(d.archived ? 'unarchive' : 'archive') },
    { label: 'Save to registry', icon: 'star', onSelect: on.saveToRegistry, title: 'Keep this loop as a reusable template' },
  ];
  return (
    <div className="ld-controls">
      {running ? (
        <>
          {st !== 'stopping' && !steering && (
            <button className="btn" disabled={busy} onClick={on.steer}
              title="Finish the current turn and pause, talk to the manager, then restart from the same point">
              <Icon name="steer" /><span>Steer</span>
            </button>
          )}
          <button className="btn danger" title="Stop this loop" disabled={busy} onClick={() => on.act('stop')}>
            <Icon name="stop" /><span>Stop</span>
          </button>
        </>
      ) : steering ? (
        <button className="btn primary" disabled={busy || steerPhase !== 'ready'} onClick={on.restart}
          title={steerPhase === 'ready'
            ? 'Restart from the same turn — the team picks up with the new context you gave the manager'
            : 'Waiting for the manager to be ready — restart unlocks then'}>
          <Icon name="play" /><span>Restart</span>
        </button>
      ) : (
        <button className="btn primary"
          title={finished ? 'Run this loop again — a fresh round with the same team' : 'Start this loop'}
          disabled={busy} onClick={() => on.act('start')}>
          <Icon name="play" /><span>{finished ? 'Rerun' : 'Run'}</span>
        </button>
      )}
      <button className="btn" title="Browse the files this loop produced" onClick={on.files}>
        <Icon name="files" /><span>Files</span>
      </button>
      <OverflowMenu items={items} />
    </div>
  );
}

/** ⋯ — the secondary actions. Esc / click-away close; arrow keys move. */
function OverflowMenu({ items }: { items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', esc);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', esc);
    };
  }, [open]);
  const move = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const btns = [...(ref.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? [])];
    const i = btns.indexOf(document.activeElement as HTMLButtonElement);
    btns[(i + (e.key === 'ArrowDown' ? 1 : -1) + btns.length) % btns.length]?.focus();
  };
  return (
    <div className="ld-ovf" ref={ref} onKeyDown={move}>
      <button className="btn ld-more" title="More actions" aria-label="More actions" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((m) => !m)}>
        <Icon name="more" />
      </button>
      {open && (
        <div className="ld-ovfmenu" role="menu">
          {items.map((it) => (
            <button key={it.label} role="menuitem" disabled={it.disabled} title={it.title}
              onClick={() => {
                setOpen(false);
                it.onSelect();
              }}>
              <Icon name={it.icon} />
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
