import { useState } from 'react';
import type { LoopDetail } from '@loopyard/api';

/** A textarea + send button. Clears itself only once the send succeeded. */
export function Compose({ placeholder, label, busy, onSend }: { placeholder: string; label: string; busy: boolean; onSend(text: string): Promise<boolean> }) {
  const [text, setText] = useState('');
  const send = async () => {
    if (!text.trim()) return;
    if (await onSend(text)) setText('');
  };
  return (
    <div className="ld-compose">
      <textarea
        className="textarea"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={placeholder}
        aria-label={placeholder}
        onKeyDown={(e) => (e.metaKey || e.ctrlKey) && e.key === 'Enter' && void send()}
      />
      <button className="btn primary" disabled={busy || !text.trim()} onClick={() => void send()}>{label}</button>
    </div>
  );
}

/** Things that need the owner now: a question from the manager, or a safety pause. */
export function Banners({ d, busy, reply }: { d: LoopDetail; busy: boolean; reply(text: string): Promise<boolean> }) {
  const st = d.state || 'saved';
  return (
    <>
      {st === 'waiting_owner' && d.question && (
        <div className="ld-banner q" role="status">
          <b>The team needs your call</b>
          <p>{d.question}</p>
          {d.host === 'local' && <Compose placeholder="Answer the manager — one line is plenty" label="Send reply" busy={busy} onSend={reply} />}
        </div>
      )}
      {st === 'needs_owner' && d.guardian_alert && (
        <div className="ld-banner a" role="status">
          <b title="The guardian paused this loop">Paused for safety — your call</b>
          <p>{d.guardian_alert}</p>
        </div>
      )}
    </>
  );
}
