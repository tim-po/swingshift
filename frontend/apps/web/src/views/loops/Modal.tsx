import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from '../../components/icons';

/**
 * The loop-detail dialog: click-outside / Esc / close button.
 *
 * Rendered into <body> through a portal: the detail pane is itself a scroll
 * container, and a `position: fixed` overlay nested in it could be clipped or
 * carried along by an ancestor's scroll/transform — the "content slides out of its
 * frame and the page shows through" bug. The dialog is a fixed-height column: the
 * header stays put and ONLY the body scrolls, so long content never escapes it.
 */
export function Modal({ title, hint, onClose, children, wide }: { title: ReactNode; hint?: ReactNode; onClose(): void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    ref.current?.focus();
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && close.current();
    document.addEventListener('keydown', esc);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', esc);
      document.body.style.overflow = prev;
    };
  }, []);
  const dialog = (
    <div className="ld-ovl" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={'ld-modal' + (wide ? ' wide' : '')} role="dialog" aria-modal="true" aria-label={typeof title === 'string' ? title : undefined} tabIndex={-1} ref={ref}>
        <header className="ld-mhead">
          <h2>{title}</h2>
          <button className="ld-mclose" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </header>
        <div className="ld-mbody">
          {hint && <div className="ld-hint">{hint}</div>}
          {children}
        </div>
      </div>
    </div>
  );
  return typeof document === 'undefined' ? dialog : createPortal(dialog, document.body);
}
