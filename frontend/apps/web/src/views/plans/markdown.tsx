// A compact, dependency-free "pretty markdown" renderer for the doc read-view.
// House style keeps the web app dep-light (see apps/web/package.json — no markdown
// lib), so this is a small, SAFE block tokenizer + inline formatter that emits real
// React elements (never dangerouslySetInnerHTML — user/agent markdown is untrusted).
// Covers what objective docs use: headings, paragraphs, fenced + inline code, bullet
// and ordered lists (incl. GFM task-list checkboxes), GFM pipe tables, blockquotes,
// rules, and inline **bold** / *em* / `code` / links.
import type { ReactNode } from 'react';

/** One item in a bullet/ordered list — text plus an optional GFM task-list checkbox state. */
export interface MdListItem {
  text: string;
  /** null ⇒ plain bullet; true/false ⇒ a `- [x]` / `- [ ]` task-list checkbox. */
  checked: boolean | null;
}

export type MdBlock =
  | { type: 'heading'; level: number; text: string }
  | { type: 'para'; text: string }
  | { type: 'code'; lang?: string; text: string }
  | { type: 'list'; ordered: boolean; items: MdListItem[] }
  | { type: 'table'; header: string[]; align: Array<'left' | 'right' | 'center' | null>; rows: string[][] }
  | { type: 'quote'; text: string }
  | { type: 'hr' };

/** Split a GFM table row `| a | b |` into trimmed cells, tolerating optional edge pipes and escaped `\|`. */
function splitTableRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const cells: string[] = [];
  let cur = '';
  for (let k = 0; k < s.length; k++) {
    if (s[k] === '\\' && s[k + 1] === '|') {
      cur += '|';
      k++;
    } else if (s[k] === '|') {
      cells.push(cur.trim());
      cur = '';
    } else {
      cur += s[k];
    }
  }
  cells.push(cur.trim());
  return cells.map((c) => c.trim());
}

/** A GFM alignment delimiter row: every cell is `---`, `:--`, `--:` or `:-:` (≥1 dash). */
function tableDelim(line: string): Array<'left' | 'right' | 'center' | null> | null {
  const cells = splitTableRow(line);
  if (!cells.length) return null;
  const align: Array<'left' | 'right' | 'center' | null> = [];
  for (const c of cells) {
    if (!/^:?-+:?$/.test(c)) return null;
    const left = c.startsWith(':');
    const right = c.endsWith(':');
    align.push(left && right ? 'center' : right ? 'right' : left ? 'left' : null);
  }
  return align;
}

/** Split markdown source into a flat list of blocks. Pure + total — testable. */
export function mdBlocks(src: string): MdBlock[] {
  const lines = String(src ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks: MdBlock[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    // fenced code
    const fence = line.match(/^```\s*(\S+)?\s*$/);
    if (fence) {
      const lang = fence[1];
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++; // closing fence
      blocks.push({ type: 'code', lang, text: buf.join('\n') });
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      blocks.push({ type: 'hr' });
      i++;
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      blocks.push({ type: 'heading', level: h[1].length, text: h[2].trim() });
      i++;
      continue;
    }
    // GFM pipe table — a header row followed by a `---|:--:` alignment row
    if (line.includes('|') && i + 1 < lines.length) {
      const align = tableDelim(lines[i + 1]);
      if (align) {
        const header = splitTableRow(line);
        const rows: string[][] = [];
        i += 2; // consume header + delimiter
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
          rows.push(splitTableRow(lines[i]));
          i++;
        }
        blocks.push({ type: 'table', header, align, rows });
        continue;
      }
    }
    // list (bullet or ordered) — consume the contiguous run, marking task-list checkboxes
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items: MdListItem[] = [];
      while (i < lines.length) {
        const cur = lines[i];
        if (/^\s*([-*+]|\d+[.)])\s+/.test(cur)) {
          const raw = cur.replace(/^\s*([-*+]|\d+[.)])\s+/, '');
          const box = raw.match(/^\[([ xX])\]\s+(.*)$/);
          items.push(box ? { text: box[2], checked: box[1] !== ' ' } : { text: raw, checked: null });
        } else if (cur.trim() && !/^\s*(#{1,6}\s|```|~~~|>|\||(-{3,}|\*{3,}|_{3,})\s*$)/.test(cur)) {
          // Lazy continuation: a wrapped line belongs to the item above it.
          items[items.length - 1].text += ' ' + cur.trim();
        } else break;
        i++;
      }
      blocks.push({ type: 'list', ordered, items });
      continue;
    }
    // blockquote — consume contiguous "> " lines
    if (/^\s*>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      blocks.push({ type: 'quote', text: buf.join(' ') });
      continue;
    }
    // paragraph — gather until a blank line or a block starter
    const buf: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,6}\s|```|\s*([-*+]|\d+[.)])\s+|\s*>\s?)/.test(lines[i]) &&
      !/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(lines[i])
    ) {
      buf.push(lines[i]);
      i++;
    }
    blocks.push({ type: 'para', text: buf.join(' ') });
  }
  return blocks;
}

/** Inline formatter: bold, italic, inline code, and safe links. Returns React nodes. */
export function mdInline(text: string, keyPrefix = 'i'): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*([^*]+)\*\*|__([^_]+)__|\*([^*]+)\*|_([^_]+)_|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let n = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const key = `${keyPrefix}-${n++}`;
    if (m[2] || m[3]) out.push(<strong key={key}>{m[2] || m[3]}</strong>);
    else if (m[4] || m[5]) out.push(<em key={key}>{m[4] || m[5]}</em>);
    else if (m[6]) out.push(<code key={key}>{m[6]}</code>);
    else if (m[7] && m[8]) {
      const href = m[8];
      const safe = /^(https?:|mailto:|\/)/i.test(href) ? href : undefined;
      out.push(
        safe ? (
          <a key={key} href={safe} target="_blank" rel="noreferrer noopener">
            {m[7]}
          </a>
        ) : (
          m[7]
        ),
      );
    }
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** Pretty rendered-markdown read view. */
export function Markdown({ src }: { src: string }) {
  const blocks = mdBlocks(src);
  if (!blocks.length) return <p className="md-empty">Nothing written yet.</p>;
  return (
    <div className="md">
      {blocks.map((b, i) => {
        const key = `b-${i}`;
        switch (b.type) {
          case 'heading': {
            const kids = mdInline(b.text, key);
            const lvl = Math.min(Math.max(b.level, 1), 4);
            if (lvl === 1) return <h1 key={key}>{kids}</h1>;
            if (lvl === 2) return <h2 key={key}>{kids}</h2>;
            if (lvl === 3) return <h3 key={key}>{kids}</h3>;
            return <h4 key={key}>{kids}</h4>;
          }
          case 'code':
            return (
              <pre key={key} className="md-code" data-lang={b.lang}>
                <code>{b.text}</code>
              </pre>
            );
          case 'list': {
            const hasTasks = b.items.some((it) => it.checked !== null);
            const renderItem = (it: MdListItem, j: number) =>
              it.checked === null ? (
                <li key={j}>{mdInline(it.text, `${key}-${j}`)}</li>
              ) : (
                <li key={j} className={'md-task' + (it.checked ? ' done' : '')}>
                  <input type="checkbox" checked={it.checked} disabled readOnly aria-label={it.checked ? 'done' : 'not done'} />
                  <span>{mdInline(it.text, `${key}-${j}`)}</span>
                </li>
              );
            const cls = hasTasks ? 'md-tasklist' : undefined;
            return b.ordered ? (
              <ol key={key} className={cls}>{b.items.map(renderItem)}</ol>
            ) : (
              <ul key={key} className={cls}>{b.items.map(renderItem)}</ul>
            );
          }
          case 'table': {
            const cols = b.header.length;
            const cell = (align: 'left' | 'right' | 'center' | null): { style?: { textAlign: 'left' | 'right' | 'center' } } =>
              align ? { style: { textAlign: align } } : {};
            return (
              <div key={key} className="md-tablewrap">
                <table className="md-table">
                  <thead>
                    <tr>
                      {b.header.map((h, j) => (
                        <th key={j} {...cell(b.align[j] ?? null)}>{mdInline(h, `${key}-h-${j}`)}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {b.rows.map((r, ri) => (
                      <tr key={ri}>
                        {Array.from({ length: cols }, (_, ci) => (
                          <td key={ci} {...cell(b.align[ci] ?? null)}>{mdInline(r[ci] ?? '', `${key}-${ri}-${ci}`)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          }
          case 'quote':
            return <blockquote key={key}>{mdInline(b.text, key)}</blockquote>;
          case 'hr':
            return <hr key={key} />;
          default:
            return <p key={key}>{mdInline(b.text, key)}</p>;
        }
      })}
    </div>
  );
}
