import { useState, type ReactNode } from 'react';
import { platform } from '../platform';
import { Icon } from './icons';

/** A view's one-paragraph explainer for newcomers. Shown until dismissed, then
 *  never again (per view) — regulars get the content, first-timers get the why.
 *  "Show all tips" in the help menu clears the dismissals. */
export function Intro({ id, className, children }: { id: string; className?: string; children: ReactNode }) {
  const key = `tip.${id}`;
  const [hidden, setHidden] = useState(() => platform.storage.get(key) === 'hidden');
  if (hidden) return null;
  return (
    <div className={'intro' + (className ? ' ' + className : '')} role="note">
      <span className="intro-ico"><Icon name="info" /></span>
      <p className="intro-text">{children}</p>
      <button
        type="button"
        className="intro-x"
        aria-label="Got it — hide this tip"
        title="Got it"
        onClick={() => {
          platform.storage.set(key, 'hidden');
          setHidden(true);
        }}
      >
        <Icon name="x" size={14} />
      </button>
    </div>
  );
}
