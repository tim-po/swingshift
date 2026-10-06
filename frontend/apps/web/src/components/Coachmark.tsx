// In-app contextual tip: a small bubble pointing at a real control the first
// time a user reaches it ("This is your team", "Rate the result here").
//
//   <Coachmark id="loop.rate" title="Rate the result" body="Your rating trains …">
//     <RatingButtons />
//   </Coachmark>
//
// Rules: shown once per id (dismiss = never again); only ONE coachmark is visible
// at a time app-wide (first unseen one mounted wins, the rest wait their turn);
// never shown while "Show everything" is on (power users know the app); "Show
// page tips again" in Help resets them along with the Intros.
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { platform } from '../platform';
import { useShowAll } from '../shell/disclosure';
import { Icon } from './icons';
import './coachmark.css';

const KEY = (id: string) => `tip.coach.${id}`;
let active: string | null = null;
const queue: string[] = [];
const subs = new Set<() => void>();
const emit = () => subs.forEach((f) => f());
const sub = (f: () => void) => {
  subs.add(f);
  return () => subs.delete(f);
};

function claim(id: string) {
  if (active === id || queue.includes(id)) return;
  if (!active) active = id;
  else queue.push(id);
  emit();
}
function release(id: string) {
  const qi = queue.indexOf(id);
  if (qi >= 0) queue.splice(qi, 1);
  if (active === id) active = queue.shift() ?? null;
  emit();
}

export function Coachmark({ id, title, body, side = 'bottom', children }: {
  id: string;
  title: string;
  body: ReactNode;
  side?: 'top' | 'bottom' | 'right';
  children: ReactNode;
}) {
  const [showAll] = useShowAll();
  const [done, setDone] = useState(() => platform.storage.get(KEY(id)) === 'done');
  const cur = useSyncExternalStore(sub, () => active, () => active);
  const eligible = !done && !showAll;
  const bubble = useRef<HTMLSpanElement>(null);
  const [flip, setFlip] = useState(false);
  const shown = eligible && cur === id;
  // Keep the bubble on screen: anchor it to the right edge when it would overflow.
  useLayoutEffect(() => {
    if (!shown || !bubble.current) return;
    const r = bubble.current.getBoundingClientRect();
    if (r.right > window.innerWidth - 8) setFlip(true);
  }, [shown]);

  useEffect(() => {
    if (!eligible) return;
    claim(id);
    return () => release(id);
  }, [eligible, id]);

  const dismiss = () => {
    platform.storage.set(KEY(id), 'done');
    setDone(true);
    release(id);
  };

  return (
    <span className="cm-anchor">
      {children}
      {shown && (
        <span ref={bubble} className={'cm-bubble cm-' + side + (flip ? ' cm-flip' : '')} role="dialog" aria-label={title}>
          <span className="cm-title">{title}</span>
          <span className="cm-body">{body}</span>
          <button type="button" className="cm-ok" onClick={dismiss}>
            Got it
          </button>
          <button type="button" className="cm-x" aria-label="Dismiss tip" onClick={dismiss}>
            <Icon name="x" size={12} />
          </button>
        </span>
      )}
    </span>
  );
}
