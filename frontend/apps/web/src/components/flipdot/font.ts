// A 5×7 dot-matrix face for flip-dot boards, and the layout helpers that turn
// text into a bitmap. Each glyph is seven hex rows, five bits wide (MSB = left).
const SRC: Record<string, string> = {
  A: '0E11111F111111', B: '1E11111E11111E', C: '0E11101010110E', D: '1E11111111111E',
  E: '1F10101E10101F', F: '1F10101E101010', G: '0E11101711110F', H: '1111111F111111',
  I: '0E04040404040E', J: '0702020202120C', K: '11121418141211', L: '1010101010101F',
  M: '111B1515111111', N: '11111915131111', O: '0E11111111110E', P: '1E11111E101010',
  Q: '0E11111115120D', R: '1E11111E141211', S: '0F10100E01011E', T: '1F040404040404',
  U: '1111111111110E', V: '11111111110A04', W: '1111111515150A', X: '11110A040A1111',
  Y: '1111110A040404', Z: '1F01020408101F',
  '0': '0E11131519110E', '1': '040C040404040E', '2': '0E11010204081F', '3': '1F02040201110E',
  '4': '02060A121F0202', '5': '1F101E0101110E', '6': '0608101E11110E', '7': '1F010204080808',
  '8': '0E11110E11110E', '9': '0E11110F01020C',
  ' ': '00000000000000', '·': '00000004000000', '/': '01010204081010', '-': '0000000E000000',
  ':': '00000400000400', '!': '04040404040004', '?': '0E110102040004', '.': '00000000000004',
  _: '0000000000001F',
};
const FONT: Record<string, number[]> = Object.fromEntries(
  Object.entries(SRC).map(([k, s]) => [k, Array.from({ length: 7 }, (_, i) => parseInt(s.slice(i * 2, i * 2 + 2), 16))]),
);
// Punctuation that sits in the middle column is drawn one dot wide.
const NARROW: Record<string, true> = { '·': true, ':': true, '!': true, '.': true };

export const GLYPH_H = 7;
const glyph = (ch: string) => (FONT[ch] ? ch : '?');
const glyphW = (ch: string) => (ch === ' ' ? 3 : NARROW[ch] ? 1 : 5);

/** Width in dots of `text` as the board would draw it (1 dot between glyphs). */
export function measure(text: string): number {
  const t = text.toUpperCase();
  let w = 0;
  for (let i = 0; i < t.length; i++) w += glyphW(glyph(t[i])) + (i < t.length - 1 ? 1 : 0);
  return w;
}

/** The longest prefix of `text` that fits in `w` dots. */
export function clip(text: string, w: number): string {
  let t = text;
  while (t && measure(t) > w) t = t.slice(0, -1);
  return t;
}

export interface DotText {
  text: string;
  x: number;
  y: number;
  /** Aligns inside [x, x + w). */
  align?: 'left' | 'right' | 'center';
  w?: number;
  /** Red discs for this text (a row that needs you). */
  alert?: boolean;
}

/** Lay text out on a cols×rows grid of discs: 0 = dark face, 1 = bright face, 2 = red face. */
export function compose(cols: number, rows: number, items: DotText[]): Uint8Array {
  const bits = new Uint8Array(cols * rows);
  for (const it of items) {
    const t = it.text.toUpperCase();
    const tw = measure(t);
    let x = it.x;
    if (it.align === 'right' && it.w != null) x = it.x + it.w - tw;
    else if (it.align === 'center' && it.w != null) x = it.x + Math.floor((it.w - tw) / 2);
    for (const raw of t) {
      const ch = glyph(raw);
      const g = FONT[ch];
      const w = glyphW(ch);
      const off = NARROW[ch] ? 2 : 0;
      for (let r = 0; r < GLYPH_H; r++)
        for (let c = 0; c < w; c++) {
          if (!((g[r] >> (4 - (c + off))) & 1)) continue;
          const px = x + c;
          const py = it.y + r;
          if (px >= 0 && px < cols && py >= 0 && py < rows) bits[py * cols + px] = it.alert ? 2 : 1;
        }
      x += w + 1;
    }
  }
  return bits;
}
