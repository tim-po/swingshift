import { useState } from 'react';

/** Roughly how many characters fit on one line of the detail column. */
const CHARS_PER_LINE = 100;

/**
 * Long text clamped to a few lines with "Show more" — never cut mid-word with a
 * hard "…". The toggle only appears when the text is likely to overflow.
 */
export function Clamp({ text, lines = 3, className, label = 'text' }: { text: string; lines?: number; className?: string; label?: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > lines * CHARS_PER_LINE || text.split('\n').filter((l) => l.trim()).length > lines;
  return (
    <div className={'ld-clamp' + (className ? ' ' + className : '')}>
      <p className={'ld-clamp-text' + (long && !open ? ' clamped' : '')} style={long && !open ? { WebkitLineClamp: lines } : undefined}>
        {text}
      </p>
      {long && (
        <button type="button" className="ld-linkbtn" aria-expanded={open} title={open ? `Collapse the ${label}` : `Show the whole ${label}`}
          onClick={() => setOpen((o) => !o)}>
          {open ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  );
}
