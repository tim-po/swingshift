// Progressive disclosure — the app starts small and grows as the user does.
//
//  • A section appears in the sidebar once it has something in it (your first
//    issue, a second machine, a saved role). Until then it waits under "More".
//  • A newly-appeared section wears a dot until it's opened once.
//  • Views gate power-user controls on `useShowAll()` (or tuck them in a ⋯ menu).
//  • "Show everything" (help menu) turns all of this off for power users.
//
// State is per-browser (platform.storage) and deliberately tiny.
import { useSyncExternalStore } from 'react';
import { platform } from '../platform';

const SHOW_ALL = 'ui.showAll';
const SEEN = (id: string) => `nav.seen.${id}`;
const INIT = 'nav.init';

const listeners = new Set<() => void>();
let version = 0;
const bump = () => {
  version++;
  listeners.forEach((l) => l());
};
const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => listeners.delete(l);
};
const snap = () => version;

/** Re-render when any disclosure flag changes. */
function useDisclosureVersion() {
  return useSyncExternalStore(subscribe, snap, snap);
}

export function useShowAll(): [boolean, (v: boolean) => void] {
  useDisclosureVersion();
  return [platform.storage.get(SHOW_ALL) === '1', setShowAll];
}

export function setShowAll(v: boolean) {
  platform.storage.set(SHOW_ALL, v ? '1' : '0');
  bump();
}

export function useSeen(): { seen(id: string): boolean; markSeen(id: string): void; initialized: boolean; init(ids: string[]): void } {
  useDisclosureVersion();
  return {
    seen: (id) => platform.storage.get(SEEN(id)) === '1',
    markSeen,
    initialized: platform.storage.get(INIT) === '1',
    // First ever load: everything already visible counts as seen, so a returning
    // owner isn't greeted by a sidebar full of dots.
    init: (ids) => {
      ids.forEach((id) => platform.storage.set(SEEN(id), '1'));
      platform.storage.set(INIT, '1');
      bump();
    },
  };
}

export function markSeen(id: string) {
  if (platform.storage.get(SEEN(id)) === '1') return;
  platform.storage.set(SEEN(id), '1');
  bump();
}

/** Clear every dismissed tip (`tip.<view>` keys) so the Intros show again. */
export function resetTips() {
  try {
    Object.keys(localStorage)
      .filter((k) => k.startsWith('tip.'))
      .forEach((k) => localStorage.removeItem(k));
  } catch {
    /* storage unavailable */
  }
  bump();
}
