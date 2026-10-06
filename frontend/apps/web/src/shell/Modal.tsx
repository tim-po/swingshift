// Minimal accessible modal: scrim click / Esc closes. Shared by the shell's
// shortcuts help and the Origins "Add origin" dialog.
import { useEffect, type ReactNode } from 'react';

export function Modal({ label, onClose, children }: { label: string; onClose(): void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="sh-modalscrim" onClick={onClose}>
      <div className="sh-modal" role="dialog" aria-modal="true" aria-label={label} onClick={(e) => e.stopPropagation()}>
        <button className="sh-modalx" onClick={onClose} aria-label="Close">×</button>
        {children}
      </div>
    </div>
  );
}
