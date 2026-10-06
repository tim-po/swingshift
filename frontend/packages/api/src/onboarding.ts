// Getting started — the Home checklist. The steps, their routes, button labels
// and completion all come from the engine (mcp_loops/onboarding.py, via
// GET /api/loops/onboarding → loop_onboarding_progress); the desktop guide panel
// reads the same payload from `yard onboard --json`. Nothing is copied here:
// this module only types the payload and shapes it for the page.
import type { Client } from './client';

export interface OnboardingItem {
  key: string;
  title: string;
  what?: string;
  /** Button label, e.g. "New loop". */
  action?: string;
  /** In-app route the item happens on, e.g. "/machines/computers". */
  route?: string | null;
  /** Engine-derived: true once the real thing happened (never on a click). */
  done?: boolean;
}

export interface OnboardingPlace {
  key: string;
  label: string;
  what?: string;
  route?: string | null;
}

export interface OnboardingProgress {
  done: number;
  total: number;
  next: string | null;
  complete: boolean;
}

export interface OnboardingSummary {
  welcome?: string;
  steps?: OnboardingItem[];
  explore?: OnboardingItem[];
  places?: OnboardingPlace[];
  progress?: OnboardingProgress;
  error?: string;
}

export const onboardingApi = (c: Client) => ({
  get: () => c.get<OnboardingSummary>('/api/loops/onboarding'),
});

/** A plain in-app path from the payload, else null (never a URL or script). */
export function safeRoute(r: unknown): string | null {
  return typeof r === 'string' && /^\/(?!\/)[A-Za-z0-9/_-]*$/.test(r) ? r : null;
}

export interface ChecklistItem {
  key: string;
  title: string;
  what: string;
  action: string;
  route: string | null;
  done: boolean;
}

export interface Checklist {
  steps: ChecklistItem[];
  done: number;
  total: number;
  /** All core steps done. */
  complete: boolean;
  /** The first step not done yet (its button is the primary one). */
  next: ChecklistItem | null;
  /** Optional next steps, unlocked once the core steps are done; undone only. */
  explore: ChecklistItem[];
  /** Is there anything left to show? (core not done, or explore items left) */
  open: boolean;
}

const clean = (s: OnboardingItem): ChecklistItem => ({
  key: s.key,
  title: s.title,
  what: s.what ?? '',
  action: s.action || 'Open',
  route: safeRoute(s.route),
  done: s.done === true,
});
const valid = (s: unknown): s is OnboardingItem =>
  !!s && typeof (s as OnboardingItem).key === 'string' && typeof (s as OnboardingItem).title === 'string';

/**
 * The payload → what Home renders, or null when there is nothing trustworthy to
 * show (no payload yet, an engine error, or no steps). Progress is recomputed
 * from the steps so a partial payload can't claim more than it shows.
 */
export function checklistModel(s: OnboardingSummary | null | undefined): Checklist | null {
  if (!s || s.error || !Array.isArray(s.steps)) return null;
  const steps = s.steps.filter(valid).map(clean);
  if (!steps.length) return null;
  const done = steps.filter((x) => x.done).length;
  const complete = done === steps.length;
  const explore = complete ? (s.explore ?? []).filter(valid).map(clean).filter((x) => !x.done) : [];
  return {
    steps,
    done,
    total: steps.length,
    complete,
    next: steps.find((x) => !x.done) ?? null,
    explore,
    open: !complete || explore.length > 0,
  };
}

/** Storage key for "Hide checklist" on Home. */
export const CHECKLIST_HIDDEN_KEY = 'home.checklist.hidden';
