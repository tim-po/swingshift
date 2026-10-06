// A tiny per-view toast. Issues owns its copy so it doesn't depend on the
// Hub view (being replaced by Plans). Messages are always calm copy — callers
// pass friendlyLine(...), never a raw error.
import { useCallback, useEffect, useRef, useState } from 'react';

export function useToast() {
  const [msg, setMsg] = useState<{ text: string; err: boolean } | null>(null);
  const t = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const toast = useCallback((text: string, err = false) => {
    setMsg({ text, err });
    clearTimeout(t.current);
    t.current = setTimeout(() => setMsg(null), err ? 6000 : 3500);
  }, []);
  useEffect(() => () => clearTimeout(t.current), []);
  const node = msg ? (
    <div className={'is-toast' + (msg.err ? ' err' : '')} role={msg.err ? 'alert' : 'status'} onClick={() => setMsg(null)}>
      {msg.text}
    </div>
  ) : null;
  return { toast, node };
}
