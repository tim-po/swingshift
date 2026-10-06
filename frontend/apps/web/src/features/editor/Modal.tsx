import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from '../../components/icons';
import './editor.css';

/**
 * Overlay dialog for the creator (target picker, briefing chat): click-outside /
 * Esc / close button. Portalled to <body>; a fixed frame whose body scrolls, so
 * long content never slides out of it.
 */
export function CreatorModal({ kick, title, onClose, children }: { kick?: ReactNode; title?: ReactNode; onClose(): void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    ref.current?.focus();
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && close.current();
    document.addEventListener('keydown', esc);
    return () => document.removeEventListener('keydown', esc);
  }, []);
  const dialog = (
    <div className="ce-ovl" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="ce-modal" role="dialog" aria-modal="true" tabIndex={-1} ref={ref}>
        <div className="ce-mhead">
          <div className="ce-mtitles">
            {kick && <div className="ce-mkick">{kick}</div>}
            {title && <h2 className="ce-mtitle">{title}</h2>}
          </div>
          <button className="ce-x" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        <div className="ce-mbody">{children}</div>
      </div>
    </div>
  );
  return typeof document === 'undefined' ? dialog : createPortal(dialog, document.body);
}
