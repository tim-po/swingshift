// Getting started — a Notion-style checklist on Home. The steps, their routes,
// button labels and ticks come from the engine (GET /api/loops/onboarding); a
// step ticks only when the real thing happened. Two sizes: the hero of a
// brand-new Home, and a compact card at the top of the normal dashboard. Once
// the core steps are done, the optional "explore" items take their place,
// quieter. "Hide checklist" is remembered.
import { Link } from 'react-router-dom';
import type { Checklist as Model, ChecklistItem } from '@loopyard/api';
import { Icon } from '../../components/icons';
import { Coachmark } from '../../components/Coachmark';

function Tick({ done, n }: { done: boolean; n?: number }) {
  return (
    <span className={'ck-tick' + (done ? ' done' : '')} aria-hidden="true">
      {done ? <Icon name="check" size={12} /> : n ?? ''}
    </span>
  );
}

// The next step gets the stronger button; later steps stay quiet links.
function Action({ s, look = 'quiet', lg }: { s: ChecklistItem; look?: 'primary' | 'default' | 'quiet'; lg?: boolean }) {
  if (!s.route || s.done) return null;
  const cls = 'btn' + (look === 'default' ? '' : ' ' + look) + (lg ? '' : ' sm');
  return <Link className={cls} to={s.route}>{s.action}</Link>;
}

function Progress({ done, total }: { done: number; total: number }) {
  return (
    <span className="ck-prog" title={`${done} of ${total} steps done`}>
      <span className="ck-bar" aria-hidden="true"><span style={{ width: `${Math.round((done / total) * 100)}%` }} /></span>
      <span className="ck-count">{done} of {total}</span>
    </span>
  );
}

/** The first-run hero: numbered steps with their explanation; the next step's button is the page's primary action. */
export function ChecklistHero({ m, onHide }: { m: Model; onHide: () => void }) {
  return (
    <section className="ck ck-hero" aria-label="Getting started">
      <div className="ck-hd">
        {/* no tip here: on first run the checklist IS the page */}
        <h2 className="section-h">Getting started</h2>
        <Progress done={m.done} total={m.total} />
        <button type="button" className="btn quiet sm ck-hide" onClick={onHide}>Hide checklist</button>
      </div>
      <ol className="ck-steps">
        {m.steps.map((s, i) => (
          <li key={s.key} className={'ck-step' + (s.done ? ' done' : '') + (m.next === s ? ' next' : '')}>
            <Tick done={s.done} n={i + 1} />
            <div className="ck-body">
              <div className="h3">{s.title}</div>
              {s.what && <p>{s.what}</p>}
              <Action s={s} look={m.next === s ? 'primary' : 'quiet'} lg={m.next === s} />
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

/** The compact card at the top of the normal Home; explore items once the core steps are done. */
export function ChecklistCard({ m, onHide }: { m: Model; onHide: () => void }) {
  const items = m.complete ? m.explore : m.steps;
  return (
    <section className={'ck ck-card' + (m.complete ? ' ck-explore' : '')} aria-label={m.complete ? 'Next, when you are ready' : 'Getting started'}>
      <div className="ck-hd">
        <Coachmark id="home.checklist" title="Your getting-started list" body="Each step opens the right page, and ticks itself once you've actually done it.">
          <h2 className="section-h">{m.complete ? "You're set up — next, when you're ready" : 'Getting started'}</h2>
        </Coachmark>
        {!m.complete && <Progress done={m.done} total={m.total} />}
        <button type="button" className="btn quiet sm ck-hide" onClick={onHide}>Hide checklist</button>
      </div>
      <ul className="ck-rows">
        {items.map((s) => (
          <li key={s.key} className={'ck-row' + (s.done ? ' done' : '')}>
            <Tick done={s.done} />
            <span className="ck-title" title={s.what || undefined}>{s.title}</span>
            <Action s={s} look={m.next === s ? 'default' : 'quiet'} />
          </li>
        ))}
      </ul>
    </section>
  );
}
