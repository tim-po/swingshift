// Theme — Time card (light) or Night (dark). "system" follows the OS; the other two
// pin <html data-theme>, which tokens.css reads. State is per-browser (platform.storage).
import { useSyncExternalStore } from 'react';
import { platform } from '../platform';

export type Theme = 'system' | 'light' | 'dark';
export const THEMES: { id: Theme; label: string }[] = [
  { id: 'system', label: 'System' },
  { id: 'light', label: 'Time card' },
  { id: 'dark', label: 'Night' },
];

const KEY = 'ui.theme';
const listeners = new Set<() => void>();
const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => listeners.delete(l);
};

export function getTheme(): Theme {
  const v = platform.storage.get(KEY);
  return v === 'light' || v === 'dark' ? v : 'system';
}

/** Put the stored theme on <html>. Called once before first render so there's no flash. */
export function applyTheme(t: Theme = getTheme()) {
  const root = document.documentElement;
  if (t === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', t);
}

export function setTheme(t: Theme) {
  platform.storage.set(KEY, t);
  applyTheme(t);
  listeners.forEach((l) => l());
}

export function useTheme(): [Theme, (t: Theme) => void] {
  return [useSyncExternalStore(subscribe, getTheme, getTheme), setTheme];
}
