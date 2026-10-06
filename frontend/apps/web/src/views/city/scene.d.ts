import type { CityLoop, CitySnap } from './snapshot';

/** What the city needs from the app around it. */
export interface CityHost {
  /** The app's effective theme right now. */
  theme(): 'light' | 'dark';
  /** The page colour for a theme, as a hex number, so the sky matches the card. */
  bg(theme: 'light' | 'dark'): number;
  /** The app's project scope; null = all projects (the city shows its own product switcher). */
  project: string | null;
  openLoop(d: CityLoop): void;
  openIssue(id: string): void;
  openPlan(id: string): void;
}

export interface CityHandle {
  setTheme(t: 'light' | 'dark'): void;
  /** Fly to a building by loop name; false when it isn't in the city shown. */
  look(name: string): boolean;
  /** Apply fresh facts to loops already drawn; false while you're inside a building (try again later). */
  update(loops: CityLoop[]): boolean;
  dispose(): void;
}

export function mountCity(root: HTMLElement, snap: CitySnap, host: CityHost): CityHandle;
