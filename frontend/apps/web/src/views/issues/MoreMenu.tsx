// A small "⋯" menu for secondary row actions (Issues triage, Projects delete).
// Lives here until it earns a place in components/ui — import it from views
// that need one rather than copying it.
import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../components/icons';
import './moremenu.css';

export interface MoreItem {
  label: string;
  onSelect(): void;
  danger?: boolean;
  disabled?: boolean;
}

export function MoreMenu({ items, label = 'More actions', disabled }: { items: MoreItem[]; label?: string; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
  if (!items.length) return null;
  return (
    <div className={'mm' + (open ? ' open' : '')} ref={ref}>
      <button
        type="button"
        className="btn quiet sm mm-btn"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen((o) => !o);
        }}
      >
        <Icon name="more" />
      </button>
      {open && (
        <div className="mm-pop" role="menu">
          {items.map((it) => (
            <button
              key={it.label}
              type="button"
              role="menuitem"
              className={'mm-item' + (it.danger ? ' danger' : '')}
              disabled={it.disabled}
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                setOpen(false);
                it.onSelect();
              }}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
