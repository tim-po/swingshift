import { useEffect, useRef } from 'react';
import { KEY_NAV } from './navMeta';

const inTextField = (t: EventTarget | null) => {
  const el = t as HTMLElement | null;
  if (!el) return false;
  const tag = (el.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
};

/** g-chord nav, '/' search focus, n = new loop, r = refresh, ? = help. */
export function useShortcuts(h: { go(id: string): void; refresh(): void; help(): void }) {
  const ref = useRef(h);
  ref.current = h;
  useEffect(() => {
    let chordAt = 0;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
      if (ev.key === 'Escape') {
        chordAt = 0;
        return;
      }
      if (inTextField(ev.target) || document.querySelector('[aria-modal="true"]')) return;
      const now = Date.now();
      if (chordAt && now - chordAt <= 900) {
        chordAt = 0;
        const dest = KEY_NAV[ev.key.toLowerCase()];
        if (dest) {
          ev.preventDefault();
          ref.current.go(dest);
        }
        return;
      }
      chordAt = 0;
      const k = ev.key;
      if (k === '?') {
        ev.preventDefault();
        ref.current.help();
      } else if (k === 'g') {
        chordAt = now;
        ev.preventDefault();
      } else if (k === '/') {
        const input = document.querySelector<HTMLInputElement>('.viewport input[type="search"], .viewport input:not([type]), .viewport input[type="text"]');
        if (input) {
          ev.preventDefault();
          input.focus();
        }
      } else if (k === 'n') {
        ev.preventDefault();
        ref.current.go('newloop');
      } else if (k === 'r') {
        ev.preventDefault();
        ref.current.refresh();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);
}
