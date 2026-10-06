// All environment-dependent knobs live here. Nothing else reads import.meta.env.
export const config = {
  apiBase: (import.meta.env.VITE_API_BASE as string | undefined) ?? '',
  /** 'browser' for the web; a packaged desktop/mobile shell can switch to 'hash'. */
  router: ((import.meta.env.VITE_ROUTER as string | undefined) ?? 'browser') as 'browser' | 'hash',
  basename: import.meta.env.BASE_URL.replace(/\/$/, ''),
  /** The release stamp baked in at build time (R1); unset in dev. */
  version: (import.meta.env.VITE_LOOPYARD_VERSION as string | undefined) || '',
  pollMs: 5000,
  /** Background refresh for data only shown as nav badges (the open view polls at pollMs). */
  navPollMs: 60000,
};
