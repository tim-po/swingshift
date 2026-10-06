// A flip-dot board: dark matte hardware, round discs that flip one by one in a
// left→right sweep when — and only when — the text on them changes. Drawn on a
// canvas; colours come from the --board / --dot-* tokens so both themes work.
// The canvas is decorative: `label` is the text screen readers get.
import { useEffect, useMemo, useRef, type ReactNode } from 'react';
import { compose, type DotText } from './font';
import './flipdot.css';

const FLIP_MS = 150;
const reduced = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

interface Colors { board: string; off: string; rim: string; on: string; alert: string }

class Engine {
  ctx: CanvasRenderingContext2D;
  to: Uint8Array;
  from: Uint8Array;
  start: Float64Array;
  live = new Set<number>();
  p = 0;
  colors: Colors = { board: '#000', off: '#111', rim: 'transparent', on: '#fff', alert: '#f00' };

  constructor(public canvas: HTMLCanvasElement, public cols: number, public rows: number, public sweep: number) {
    this.ctx = canvas.getContext('2d')!;
    const n = cols * rows;
    this.to = new Uint8Array(n);
    this.from = new Uint8Array(n);
    this.start = new Float64Array(n).fill(-1);
  }

  readColors(alert: boolean) {
    const cs = getComputedStyle(this.canvas);
    const v = (k: string) => cs.getPropertyValue(k).trim();
    this.colors = { board: v('--board'), off: v('--dot-off'), rim: v('--dot-rim'), on: v(alert ? '--dot-alert' : '--dot-on'), alert: v('--dot-alert') };
  }

  /** Size the canvas for a disc pitch in CSS px, in whole device pixels. Rounding may step it down one
   *  to stay inside `limit`, but never below `floor` — past that the frame scrolls instead. Returns whether it changed. */
  size(css: number, limit = Infinity, floor = 0): boolean {
    const dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
    let p = Math.max(2, Math.round(css * dpr));
    if (p > 2 && (this.cols * p) / dpr > limit && p - 1 >= floor * dpr) p--;
    if (p === this.p) return false;
    this.p = p;
    this.canvas.width = this.cols * p;
    this.canvas.height = this.rows * p;
    this.canvas.style.width = `${(this.cols * p) / dpr}px`;
    this.canvas.style.height = `${(this.rows * p) / dpr}px`;
    // Plate text on the frame lines up with disc columns through this.
    this.canvas.parentElement?.style.setProperty('--fd-pitch', `${p / dpr}px`);
    return true;
  }

  /** Paint one disc; true while it is still mid-flip. */
  cell(i: number, now: number): boolean {
    const { ctx: c, p, cols, colors } = this;
    const x = i % cols;
    const y = (i / cols) | 0;
    let face = this.to[i];
    let sx = 1;
    let busy = false;
    const s = this.start[i];
    if (s >= 0) {
      const t = (now - s) / FLIP_MS;
      if (t < 1) {
        busy = true;
        if (t < 0) face = this.from[i];
        else {
          sx = Math.abs(Math.cos(Math.PI * t));
          face = t < 0.5 ? this.from[i] : this.to[i];
        }
      } else this.start[i] = -1;
    }
    c.fillStyle = colors.board;
    c.fillRect(x * p, y * p, p, p);
    const r = p * 0.42;
    c.beginPath();
    c.ellipse(x * p + p / 2, y * p + p / 2, Math.max(r * sx, 0.6), r, 0, 0, Math.PI * 2);
    c.fillStyle = face === 2 ? colors.alert : face ? colors.on : colors.off;
    c.fill();
    if (!face && sx > 0.3) {
      c.lineWidth = Math.max(1, p * 0.06);
      c.strokeStyle = colors.rim;
      c.stroke();
    }
    return busy;
  }

  drawAll() {
    if (!this.p) return;
    const now = performance.now();
    for (let i = 0; i < this.to.length; i++) this.cell(i, now);
  }

  step(now: number): boolean {
    for (const i of this.live) if (!this.cell(i, now)) this.live.delete(i);
    return this.live.size > 0;
  }

  set(bits: Uint8Array) {
    const now = performance.now();
    const instant = reduced();
    let moving = false;
    for (let i = 0; i < bits.length; i++) {
      if (bits[i] === this.to[i]) continue;
      if (instant) {
        this.to[i] = bits[i];
        this.start[i] = -1;
        this.live.delete(i);
        if (this.p) this.cell(i, now);
        continue;
      }
      const x = i % this.cols;
      const y = (i / this.cols) | 0;
      this.from[i] = this.to[i];
      this.to[i] = bits[i];
      // Column sweep, a hair of per-row lag, and a little per-disc jitter like real hardware.
      this.start[i] = now + (x / this.cols) * this.sweep + y * 1.5 + (((i * 2654435761) >>> 0) % 9);
      this.live.add(i);
      moving = true;
    }
    if (moving) animate(this);
  }
}

// One animation frame loop for every board on the page; idle when nothing flips.
const animating = new Set<Engine>();
let rafOn = false;
function frame(now: number) {
  for (const b of animating) if (!b.step(now)) animating.delete(b);
  if (animating.size) requestAnimationFrame(frame);
  else rafOn = false;
}
function animate(b: Engine) {
  animating.add(b);
  if (!rafOn) {
    rafOn = true;
    requestAnimationFrame(frame);
  }
}

export interface DotBoardProps {
  cols: number;
  rows: number;
  items: DotText[];
  /** What the board says, for screen readers. */
  label: string;
  /** Red discs: a separate alert board, never a recolour of the same one. */
  alert?: boolean;
  /** Fixed disc pitch in CSS px; otherwise the board fits its container between min and max. */
  pitch?: number;
  min?: number;
  max?: number;
  /** Duration of the left→right sweep in ms. */
  sweep?: number;
  className?: string;
  /** Printed on the frame above the discs; position children with calc(var(--fd-pitch) * column). */
  head?: ReactNode;
}

export function DotBoard({ cols, rows, items, label, alert = false, pitch, min = 2, max = 5, sweep = 700, className, head }: DotBoardProps) {
  const wrap = useRef<HTMLDivElement>(null);
  const frameEl = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const eng = useRef<Engine | null>(null);
  const key = JSON.stringify(items);
  const bits = useMemo(() => compose(cols, rows, items), [cols, rows, key]);
  const latest = useRef(bits);
  latest.current = bits;

  // Build the engine, keep it sized to the container and repaint on theme changes.
  useEffect(() => {
    const cv = canvas.current;
    const box = wrap.current;
    const fr = frameEl.current;
    if (!cv || !box || !fr || !cv.getContext) return;
    let e: Engine;
    try {
      e = new Engine(cv, cols, rows, sweep);
    } catch {
      return; // no 2D canvas (e.g. a test DOM): the text label still stands in
    }
    if (!e.ctx) return;
    eng.current = e;
    e.readColors(alert);
    const fit = () => {
      const fs = getComputedStyle(fr);
      const chrome = ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth'].reduce((a, k) => a + parseFloat(fs[k as 'paddingLeft']), 0);
      const avail = box.clientWidth - chrome;
      const css = pitch ?? Math.max(min, Math.min(max, avail / cols));
      if (e.size(css, pitch == null ? avail : Infinity, min)) e.drawAll();
    };
    fit();
    e.set(latest.current);
    const ro = pitch == null && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(fit) : null;
    ro?.observe(box);
    const repaint = () => {
      e.readColors(alert);
      e.drawAll();
    };
    const mq = matchMedia('(prefers-color-scheme: dark)');
    mq.addEventListener('change', repaint);
    const mo = new MutationObserver(repaint);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => {
      ro?.disconnect();
      mo.disconnect();
      mq.removeEventListener('change', repaint);
      animating.delete(e);
      eng.current = null;
    };
  }, [cols, rows, alert, pitch, min, max, sweep]);

  // Flip to the new text. A new engine starts blank, so its first set flips in like a board powering up.
  useEffect(() => {
    eng.current?.set(bits);
  }, [bits]);

  return (
    <div className={'fd-wrap' + (className ? ' ' + className : '')} ref={wrap}>
      <div className="fd" ref={frameEl}>
        {head && <div className="fd-head" aria-hidden="true">{head}</div>}
        <canvas ref={canvas} aria-hidden="true" />
      </div>
      <span className="fd-sr">{label}</span>
    </div>
  );
}
