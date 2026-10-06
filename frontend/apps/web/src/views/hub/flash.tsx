// A tiny per-view toast (the legacy `flash(msg, isErr)`), used by Hub + Issues.
import { useCallback, useEffect, useRef, useState } from 'react';
import './flash.css';

export function useFlash() {
  const [msg, setMsg] = useState<{ text: string; err: boolean } | null>(null);
  const t = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const flash = useCallback((text: string, err = false) => {
    setMsg({ text, err });
    clearTimeout(t.current);
    t.current = setTimeout(() => setMsg(null), err ? 6000 : 3500);
  }, []);
  useEffect(() => () => clearTimeout(t.current), []);
  const node = msg ? (
    <div className={'hflash' + (msg.err ? ' err' : '')} role={msg.err ? 'alert' : 'status'} onClick={() => setMsg(null)}>
      {msg.text}
    </div>
  ) : null;
  return { flash, node };
}

export const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
