// Thin seam over platform features. Views call these, never the browser APIs
// directly, so a desktop/mobile shell can swap in native implementations.
export interface Platform {
  openExternal(url: string): void;
  storage: { get(key: string): string | null; set(key: string, value: string): void };
}

export const platform: Platform = {
  openExternal: (url) => void window.open(url, '_blank', 'noopener'),
  storage: {
    get: (k) => {
      try {
        return localStorage.getItem(k);
      } catch {
        return null;
      }
    },
    set: (k, v) => {
      try {
        localStorage.setItem(k, v);
      } catch {
        /* private mode — ignore */
      }
    },
  },
};
