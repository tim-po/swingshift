import { useEffect, useRef, useState } from 'react';
import { Icon } from '../components/icons';
import { resetTips, useShowAll } from './disclosure';
import { THEMES, useTheme } from './theme';

/** Sidebar footer: help + the one switch power users want ("show everything"). */
export function HelpMenu({ onShortcuts }: { onShortcuts(): void }) {
  const [open, setOpen] = useState(false);
  const [showAll, setShowAll] = useShowAll();
  const [theme, setTheme] = useTheme();
  const [flash, setFlash] = useState('');
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div className="sh-help" ref={ref}>
      <button className="sh-helpbtn" onClick={() => setOpen((o) => !o)} aria-haspopup="menu" aria-expanded={open}>
        <Icon name="help" />
        Help & settings
      </button>
      {open && (
        <div className="sh-pop sh-helppop" role="menu">
          <label className="sh-popitem sh-toggle" role="menuitemcheckbox" aria-checked={showAll}>
            <span>
              <b>Show everything</b>
              <small>Every section and advanced control, all the time.</small>
            </span>
            <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
            <span className="sh-switch" aria-hidden="true" />
          </label>
          <div className="sh-popitem sh-theme">
            <span>Theme</span>
            <div className="sh-seg" role="radiogroup" aria-label="Theme">
              {THEMES.map((t) => (
                <button key={t.id} role="radio" aria-checked={theme === t.id} onClick={() => setTheme(t.id)}>
                  {t.label}
                </button>
              ))}
            </div>
          </div>
          <div className="sh-popsep" />
          <button className="sh-popitem" role="menuitem" onClick={() => { resetTips(); setFlash('Tips will show again'); }}>
            <Icon name="info" /> {flash || 'Show page tips again'}
          </button>
          <button className="sh-popitem" role="menuitem" onClick={() => { setOpen(false); onShortcuts(); }}>
            <Icon name="sliders" /> Keyboard shortcuts <kbd>?</kbd>
          </button>
        </div>
      )}
    </div>
  );
}
