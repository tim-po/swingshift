import type { TermStatus } from '@loopyard/api';
import './xterm.css';

/** Connection dot + label (on / wait / off). */
export function TermStat({ status, text }: { status: TermStatus; text: string }) {
  return (
    <span className={`xt-stat ${status}`} role="status" aria-live="polite">
      <span className="xt-dot" aria-hidden="true" />
      <span>{text}</span>
    </span>
  );
}

/** The surface xterm mounts into; size it with the parent (flex/min-height). */
export function TermSurface({ hostRef, className }: { hostRef: React.RefObject<HTMLDivElement | null>; className?: string }) {
  return (
    <div className={'xt-wrap' + (className ? ' ' + className : '')}>
      <div className="xt-host" ref={hostRef} />
    </div>
  );
}
