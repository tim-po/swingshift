import { agentChips, firstLine, reportStatusLabel, turnDotsOf, type LiveAgent, type RosterCard, type TeamRoom, type TurnDot } from '@loopyard/api';
import { Coachmark } from '../../components/Coachmark';
import { Clamp } from './Clamp';

export const sentence = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, ' ') : s);

/**
 * The team as compact chips (turn count, a live dot while working). Clicking one
 * filters the timeline to that agent and shows who it is right under the strip;
 * clicking it again clears the filter.
 */
export function TeamStrip({ tr, running, live, selected, onSelect, onReports, onRole }: {
  tr: TeamRoom;
  running: boolean;
  live?: LiveAgent[];
  selected: string;
  onSelect(agent: string): void;
  /** Open this agent's every-turn history. */
  onReports(agent: string): void;
  /** Open the agent's role page. */
  onRole(agent: string): void;
}) {
  const chips = agentChips(tr, { running, live });
  if (!chips.length) return null;
  const card = tr.roster.find((c) => c.agent === selected);
  return (
    <section className="ld-section ld-team" aria-label="Team">
      <div className="ld-team-row">
        <h2 className="section-h">Team</h2>
        <Coachmark id="loop.team" title="This is your team" body="Click someone to see what they did.">
          <div className="ld-chips" role="group" aria-label="Filter the timeline by agent">
            {chips.map((c) => (
              <button key={c.agent} type="button" className={'chip ld-achip' + (selected === c.agent ? ' on' : '')} aria-pressed={selected === c.agent}
                title={`${c.agent}${c.role ? ' · ' + c.role.replace(/_/g, ' ') : ''} — ${c.turns} turn${c.turns === 1 ? '' : 's'}${c.live ? ', working now' : ''}. Click to see only their turns.`}
                onClick={() => onSelect(selected === c.agent ? '' : c.agent)}>
                {c.live && <span className="ld-live-dot" aria-label="working now" />}
                <span>{c.agent}</span>
                <span className="n">{c.turns}</span>
              </button>
            ))}
          </div>
        </Coachmark>
      </div>
      {card && <AgentPanel c={card} running={running} onReports={() => onReports(card.agent)} onRole={() => onRole(card.agent)} />}
    </section>
  );
}

/** Who the picked agent is — what the old agent card showed, plus personality and goal. */
function AgentPanel({ c, running, onReports, onRole }: { c: RosterCard; running: boolean; onReports(): void; onRole(): void }) {
  // Its turns are the (now filtered) timeline right below — no need to repeat its last word here.
  return (
    <div className="ld-apanel">
      <AgentIdentity c={c} />
      {running && c.doingNow && <p className="ai-v"><span className="ld-muted">Now: </span>{c.doingNow}</p>}
      <div className="ai-links">
        <button type="button" className="ld-linkbtn" onClick={onReports}>Every turn by {c.agent}</button>
        <button type="button" className="ld-linkbtn" onClick={onRole}>Open role</button>
      </div>
    </div>
  );
}

/** Opening an agent leads with WHO it is: role + turn dots + what it owns + personality (+ its goal). */
export function AgentIdentity({ c }: { c: RosterCard }) {
  const role = c.displayRole || c.role;
  return (
    <div className="ld-agentid">
      <div className="ai-top">
        {role && <span className={'ld-arole' + (c.isManager ? ' mgr' : '')}>{sentence(role)}</span>}
        <TurnDots c={c} />
      </div>
      {c.owns && !c.goal?.trim() && <p className="ai-v">{c.owns}</p>}
      <div className="ai-k">Personality</div>
      <p className="ai-v">{c.personality?.trim() || <span className="ld-muted">No personality written for this agent.</span>}</p>
      {c.goal?.trim() && (
        <>
          <div className="ai-k">Goal on this loop</div>
          <Clamp className="ai-v" text={c.goal.trim()} lines={3} label="goal" />
        </>
      )}
    </div>
  );
}

const dotTitle = (d: TurnDot) =>
  d.current ? 'This turn — in progress' : `Turn ${d.seq} · ${reportStatusLabel(d.status || '') || '—'}${d.note ? ' — ' + firstLine(d.note) : ''}`;

/** One dot per turn, oldest → newest; the live turn pulses at the end. */
export function TurnDots({ c }: { c: RosterCard }) {
  const { dots, hidden } = turnDotsOf(c);
  const done = dots.filter((d) => !d.current).length + hidden;
  return (
    <span className="ld-adots" role="img" aria-label={`${done} turn${done === 1 ? '' : 's'} taken${dots.some((d) => d.current) ? ', one in progress' : ''}`}>
      {hidden > 0 && <span className="ld-adots-more">+{hidden}</span>}
      {dots.length === 0 ? (
        <span className="ld-adot idle" title="No turns yet" />
      ) : (
        dots.map((d) => <span key={`${d.seq}-${d.current ? 'c' : ''}`} className={`ld-adot ${d.dot}${d.current ? ' current' : ''}`} title={dotTitle(d)} />)
      )}
    </span>
  );
}
