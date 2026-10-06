// Runtime loader for the vendored xterm.js (UMD) served by the Loopyard server at
// /static/vendor/xterm/ — same-origin, CSP-safe, no npm dep. Loads once.
import { config } from '../../config';

/** The slice of the xterm API we use (xterm 5.5 UMD → window.Terminal). */
export interface XTerminal {
  cols: number;
  rows: number;
  open(el: HTMLElement): void;
  write(data: string | Uint8Array): void;
  onData(cb: (d: string) => void): { dispose(): void };
  loadAddon(a: unknown): void;
  focus(): void;
  dispose(): void;
}
export interface XFitAddon {
  fit(): void;
}
export interface XtermLib {
  Terminal: new (opts: Record<string, unknown>) => XTerminal;
  FitAddon: new () => XFitAddon;
}

const VENDOR = `${config.apiBase}/static/vendor/xterm`;
let libP: Promise<XtermLib> | null = null;

type XWin = Window & { Terminal?: XtermLib['Terminal']; FitAddon?: XtermLib['FitAddon'] | { FitAddon: XtermLib['FitAddon'] } };

function fromWindow(): XtermLib | null {
  const w = window as XWin;
  if (!w.Terminal || !w.FitAddon) return null;
  const fa = w.FitAddon as { FitAddon?: XtermLib['FitAddon'] };
  return { Terminal: w.Terminal, FitAddon: fa.FitAddon ?? (w.FitAddon as XtermLib['FitAddon']) };
}

export function loadXterm(): Promise<XtermLib> {
  if (libP) return libP;
  libP = new Promise<XtermLib>((resolve, reject) => {
    const ready = fromWindow();
    if (ready) return resolve(ready);
    if (!document.querySelector('link[data-xterm]')) {
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = `${VENDOR}/xterm.css`;
      css.dataset.xterm = '1';
      document.head.appendChild(css);
    }
    let loaded = 0;
    const done = () => {
      if (++loaded < 2) return;
      const lib = fromWindow();
      if (lib) resolve(lib);
      else reject(new Error('terminal library failed to load'));
    };
    for (const f of ['xterm.js', 'addon-fit.js']) {
      const s = document.createElement('script');
      s.src = `${VENDOR}/${f}`;
      s.onload = done;
      s.onerror = () => reject(new Error('terminal library failed to load'));
      document.head.appendChild(s);
    }
  }).catch((e: unknown) => {
    libP = null; // allow a retry on the next mount
    throw e;
  });
  return libP;
}

/** xterm needs concrete colours: read them from the design tokens. */
export function xtermTheme(): Record<string, string> {
  const cs = getComputedStyle(document.documentElement);
  const v = (n: string) => cs.getPropertyValue(n).trim();
  const phos = v('--phos');
  return {
    background: v('--ink'),
    foreground: v('--bone'),
    cursor: phos,
    cursorAccent: v('--ink'),
    selectionBackground: /^#[0-9a-f]{6}$/i.test(phos) ? phos + '55' : phos,
  };
}
