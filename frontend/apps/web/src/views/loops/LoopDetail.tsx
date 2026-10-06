import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  goalText, loopIsRunning, loopStatusOf, loopTimeline, STEER_WAITING, turnsLabel,
  type DispositionBody, type LoopAction, type LoopDetail as LD, type LoopSummary,
} from '@loopyard/api';
import { Banners } from './Banners';
import { DetailHeader } from './DetailHeader';
import { DetailModal, type ModalState, type TurnReport } from './DetailModals';
import { friendlyLine, Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { useShowAll } from '../../shell/disclosure';
import {
  useAnalyze, useBriefLoop, useDisposition, useFiles, useGoodness, useLoopAction, useLoopDetail, useRescore, useSteerStatus, useTeamRoom, useThoughtlog,
} from './hooks';
import { SteerPanel } from './Steer';
import { RunSummary } from './Summary';
import { TeamStrip } from './Team';
import { Timeline } from './Timeline';
import './loopDetail.css';

/** A toast. `sticky` = an in-progress note that stays until the work settles. */
type Flash = { msg: string; err: boolean; sticky?: boolean } | null;

const DONE: Partial<Record<LoopAction, string>> = {
  start: 'Started',
  stop: 'Stopping — the current turn finishes first',
  archive: 'Archived',
  unarchive: 'Back from the archive',
  reply: 'Reply sent to the manager',
  steer: 'Pausing so you can steer…',
};

/**
 * The loop page: header → one summary card (live progress, or the result + your
 * rating) → the team as chips → ONE timeline of every turn. Everything else is a
 * click away (⋯, Files, a turn, an agent).
 */
export function LoopDetail({ loop, onClose }: { loop: LoopSummary; onClose(): void }) {
  const { name, host } = loop;
  const navigate = useNavigate();
  const det = useLoopDetail(name, host);
  const trq = useTeamRoom(name, host);
  const action = useLoopAction(name, host);
  const briefLoop = useBriefLoop(name);
  const analyze = useAnalyze(name);
  const disposition = useDisposition(name, host);
  const [modal, setModal] = useState<ModalState | null>(null);
  const [flash, setFlash] = useState<Flash>(null);
  // Set when THIS pane asked for a Steer — keeps polling + shows a hand-off error.
  const [steering, setSteering] = useState(false);
  // The team chip that filters the timeline ('' = everyone).
  const [agent, setAgent] = useState('');

  useEffect(() => {
    if (!flash || flash.sticky) return;
    const t = setTimeout(() => setFlash(null), flash.err ? 7000 : 3500);
    return () => clearTimeout(t);
  }, [flash]);
  // A different loop → drop any open modal / toast / filter.
  useEffect(() => {
    setModal(null);
    setSteering(false);
    setFlash(null);
    setAgent('');
  }, [name, host]);

  // Until the full detail lands, render what the list already knows.
  const d: LD = det.data && !det.data.error ? det.data : loop;
  const tr = trq.data && trq.data.name === d.name ? trq.data : null;
  const busy = action.isPending || briefLoop.isPending;
  const trSteer = tr?.run?.steer ?? null;
  const steerQ = useSteerStatus(name, host === 'local' && (steering || STEER_WAITING.has(trSteer?.phase || '')));
  const steer = steerQ.data?.steer?.phase ? steerQ.data.steer : trSteer;
  const steerPhase = steer?.phase || '';
  useEffect(() => {
    if (steerPhase === 'restarted') setSteering(false);
  }, [steerPhase]);
  const say = (msg: string, err = false) => setFlash({ msg, err });
  const working = (msg: string) => setFlash({ msg, err: false, sticky: true });

  // OWNER RESTORE: the per-loop Brief/Debrief spawn the REAL tmux adopt-the-manager
  // session (loop_brief_session/loop_debrief_session) and show its attach command —
  // spawning the session is itself the owner's health smoke-test of the substrate.
  // The native chat still powers the composer and /newloop?brief=<name>.
  const doBrief = async (phase: 'brief' | 'debrief') => {
    working(phase === 'debrief' ? 'Resuming the manager…' : 'Starting a briefing session…');
    try {
      const res = await briefLoop.mutateAsync(phase);
      if (res?.error) return say(res.error, true);
      setFlash(null);
      setModal({ kind: 'brief', phase, attach: (res?.attach as string) || '', resumed: !!res?.resumed });
    } catch (e) {
      say(friendlyLine(e, phase === 'debrief' ? 'resume the manager' : 'start the briefing'), true);
    }
  };

  const act = async (a: LoopAction, extra: { reply?: string; note?: string } = {}): Promise<boolean> => {
    try {
      const res = await action.mutateAsync({ action: a, ...extra });
      if (res?.error) {
        say(res.error, true);
        return false;
      }
      const pending = res?.pending != null ? ` (${res.pending} queued)` : '';
      if (a !== 'save_registry') say((DONE[a] ?? 'Done') + pending);
      return true;
    } catch (e) {
      say(friendlyLine(e, 'do that'), true);
      return false;
    }
  };

  const dispose = async (body: DispositionBody): Promise<string> => {
    try {
      const res = await disposition.mutateAsync(body);
      return res?.ok ? 'Saved' : res?.error || "Couldn't save that";
    } catch (e) {
      return friendlyLine(e, 'record that');
    }
  };

  const runAnalysis = async () => {
    working('Analyzing the run…');
    try {
      const res = await analyze.mutateAsync();
      if (res?.error) return say(res.error, true);
      setFlash(null); // the result is on screen — the "analyzing" toast has done its job
      setModal({ kind: 'analysis', result: res });
    } catch (e) {
      say(friendlyLine(e, 'run analysis'), true);
    }
  };

  // Steer mirrors Brief: gentle stop → manager hand-off → restart.
  const askSteer = () =>
    setModal({
      kind: 'confirm',
      text: 'Steer this loop?',
      body: 'The current turn finishes, then the loop pauses and hands you its manager. Tell it the new direction, then restart — the team picks up from the same turn.',
      yes: 'Pause & steer',
      onYes: async () => {
        if (await act('steer')) setSteering(true);
      },
    });
  const resumeSteer = async () => {
    try {
      const res = await action.mutateAsync({ action: 'steer_resume' });
      if (res?.error) return say("Couldn't restart yet — give the manager a moment, then try again.", true);
      setSteering(false);
      say(`Restarted from turn ${res?.resumedFromTurn ?? steer?.turnsUsed ?? '—'} with your new direction`);
    } catch (e) {
      say(friendlyLine(e, 'restart the loop'), true);
    }
  };

  const openRole = (id: string) => navigate(`/library/agents/${encodeURIComponent(id)}`);
  const openTurn = (a: string, seq: number, rep?: TurnReport) => setModal({ kind: 'turn', agent: a, seq, rep });
  const openFiles = () => setModal({ kind: 'files' });

  const status = loopStatusOf(tr);
  const running = loopIsRunning(status, d.state);
  // Run observability (goodness + thought-log) and the file list are served by the local engine only.
  const observe = host === 'local';
  const goodness = useGoodness(name, observe);
  const rescore = useRescore(name);
  const thoughts = useThoughtlog(name, observe);
  const files = useFiles(name, observe);
  const fileCount = files.data && !files.data.error ? files.data.count ?? null : null;
  const [showAll] = useShowAll();
  const tl = observe ? thoughts.data : null;
  const rows = useMemo(
    () => loopTimeline({ tr, recent: d.recent, started: d.started, thoughts: tl, manager: d.team?.manager, subNames: d.sub_names, system: showAll }),
    [tr, d.recent, d.started, tl, d.team?.manager, d.sub_names, showAll],
  );
  const manager = tr?.roster.find((c) => c.isManager)?.agent ?? d.team?.manager ?? undefined;
  const showSteer = d.host === 'local' && steer && (STEER_WAITING.has(steerPhase) || (steering && steerPhase === 'error'));
  const rate = (verb: 'good' | 'ok' | 'bad', note: string) => dispose({ verb, source: 'explicit', ...(note ? { note } : {}) });

  return (
    <article className="ldetail ld">
      <button className="back" onClick={onClose}><Icon name="chevronLeft" size={14} /> All loops</button>
      <DetailHeader
        d={d}
        status={status}
        goal={goalText(tr?.goal, d.goal)}
        steerPhase={d.host === 'local' ? steerPhase : ''}
        busy={busy}
        on={{
          act: (a) => void act(a),
          brief: (phase) => void doBrief(phase),
          steer: askSteer,
          restart: () => void resumeSteer(),
          files: openFiles,
          analyze: () => void runAnalysis(),
          editConfig: () => navigate(`/newloop?edit=${encodeURIComponent(name)}`),
          saveToRegistry: () => setModal({ kind: 'save' }),
        }}
      />
      {det.error && (
        <p className="ld-err">
          Couldn't refresh — showing the latest we have. <button type="button" className="ld-linkbtn" onClick={() => det.refetch()}>Try again</button>
        </p>
      )}
      {det.data?.error && <p className="ld-err">Couldn't refresh just now — showing the latest we have.</p>}
      <Banners d={d} busy={busy} reply={(reply) => act('reply', { reply })} />
      {showSteer && steer && <SteerPanel steer={steer} busy={busy} onResume={() => void resumeSteer()} />}

      {!det.data || trq.isPending ? (
        <div className="ld-sk">
          <Skeleton block={96} lines={0} />
          <Skeleton lines={4} />
          <Skeleton block={64} lines={0} />
        </div>
      ) : (
        <>
          <RunSummary
            d={d}
            tr={tr}
            status={status}
            running={running}
            goodness={observe ? goodness.data : null}
            rate={rate}
            rescore={observe ? () => rescore.mutate() : undefined}
            rescoring={rescore.isPending}
            openFiles={openFiles}
          />
          {!tr && (turnsLabel(d) || d.ended || !!d.retired?.length) && (
            <dl className="ld-kv">
              {turnsLabel(d) && (<div><dt>Progress</dt><dd>{turnsLabel(d)}</dd></div>)}
              {d.ended && (<div><dt>Ended</dt><dd>{d.ended}</dd></div>)}
              {!!d.retired?.length && (<div><dt>Retired agents</dt><dd>{d.retired.length}</dd></div>)}
            </dl>
          )}
          {tr && (
            <TeamStrip
              tr={tr}
              running={running}
              live={d.live}
              selected={agent}
              onSelect={setAgent}
              onReports={(a) => setModal({ kind: 'reports', agent: a, card: tr.roster.find((c) => c.agent === a) })}
              onRole={openRole}
            />
          )}
          <Timeline
            rows={rows}
            filter={agent}
            onClearFilter={() => setAgent('')}
            managerRead={running ? null : tr?.managerRead}
            manager={manager}
            canOpen={observe}
            onTurn={openTurn}
            files={observe ? fileCount : 0}
            onFiles={openFiles}
          />
        </>
      )}

      {modal && (
        <DetailModal
          m={modal}
          loop={name}
          host={host}
          close={() => setModal(null)}
          open={setModal}
          saveToRegistry={async (note) => {
            const ok = await act('save_registry', { note });
            if (ok) say(`Saved “${name}” to the registry`);
            return ok;
          }}
        />
      )}
      {flash && (
        <div className={'ld-flash' + (flash.err ? ' err' : '') + (flash.sticky ? ' busy' : '')} role="status">
          {flash.msg}
          {!flash.sticky && (
            <button type="button" className="ld-flash-x" aria-label="Dismiss" onClick={() => setFlash(null)}><Icon name="x" size={14} /></button>
          )}
        </div>
      )}
    </article>
  );
}
