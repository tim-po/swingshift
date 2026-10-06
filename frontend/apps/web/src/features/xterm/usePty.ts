// A live PTY over /ws/terminal rendered with xterm. The socket's lifetime IS the
// component's: unmount closes it, which is the backend's teardown signal (no
// orphaned shells). Polling re-renders never touch it — only `session` changes do.
import { useCallback, useEffect, useRef, useState } from 'react';
import { closedText, controlLine, resizeMessage, terminalWsUrl, type TermSession, type TermStatus } from '@loopyard/api';
import { config } from '../../config';
import { loadXterm, xtermTheme, type XFitAddon, type XTerminal } from './loadXterm';

const ENC = new TextEncoder();

export interface PtyState {
  hostRef: React.RefObject<HTMLDivElement | null>;
  status: TermStatus;
  text: string;
  reconnect: () => void;
}

export function usePty(session: TermSession, opts: { onOutput?: (chunk: string) => void; scrollback?: number } = {}): PtyState {
  const hostRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<TermStatus>('wait');
  const [text, setText] = useState('Connecting…');
  const connectRef = useRef<() => void>(() => {});
  const wsRef = useRef<WebSocket | null>(null);
  const onOutput = useRef(opts.onOutput);
  onOutput.current = opts.onOutput;
  const url = terminalWsUrl(config.apiBase, window.location.href, session);
  const scrollback = opts.scrollback ?? 5000;

  useEffect(() => {
    let cancelled = false;
    let term: XTerminal | null = null;
    let fit: XFitAddon | null = null;
    let ro: ResizeObserver | null = null;
    const dec = new TextDecoder();
    const set = (s: TermStatus, t: string) => {
      if (!cancelled) {
        setStatus(s);
        setText(t);
      }
    };
    const sendResize = () => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN && term) ws.send(resizeMessage(term.cols, term.rows));
    };
    const refit = () => {
      try {
        fit?.fit();
        sendResize();
      } catch {
        /* hidden / zero-size host */
      }
    };

    const connect = () => {
      if (cancelled) return;
      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch {
        set('off', 'Disconnected');
        return;
      }
      ws.binaryType = 'arraybuffer';
      wsRef.current = ws;
      set('wait', 'Connecting…');
      let opened = false;
      ws.onopen = () => {
        opened = true;
        set('on', 'Connected');
        sendResize();
        term?.focus();
      };
      ws.onmessage = (ev) => {
        if (typeof ev.data === 'string') {
          const line = controlLine(ev.data);
          if (line) term?.write(line);
          return;
        }
        const u = new Uint8Array(ev.data as ArrayBuffer);
        term?.write(u);
        onOutput.current?.(dec.decode(u, { stream: true }));
      };
      ws.onclose = () => {
        if (wsRef.current !== ws) return; // superseded by a reconnect
        wsRef.current = null;
        set('off', closedText(opened));
      };
    };
    connectRef.current = connect;

    set('wait', 'Connecting…');
    loadXterm()
      .then((lib) => {
        const host = hostRef.current;
        if (cancelled || !host) return;
        term = new lib.Terminal({
          cursorBlink: true,
          fontSize: 13,
          scrollback,
          fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--mono').trim() || 'monospace',
          theme: xtermTheme(),
        });
        fit = new lib.FitAddon();
        term.loadAddon(fit);
        host.replaceChildren();
        term.open(host);
        term.onData((d) => {
          const ws = wsRef.current;
          if (ws && ws.readyState === WebSocket.OPEN) ws.send(ENC.encode(d));
        });
        requestAnimationFrame(refit);
        if ('ResizeObserver' in window) {
          ro = new ResizeObserver(refit);
          ro.observe(host);
        }
        connect();
      })
      .catch(() => set('off', 'Terminal library failed to load'));

    return () => {
      cancelled = true;
      ro?.disconnect();
      const ws = wsRef.current;
      wsRef.current = null;
      try {
        ws?.close();
      } catch {
        /* already closed */
      }
      term?.dispose();
      connectRef.current = () => {};
    };
  }, [url, scrollback]);

  // Tab close / reload → close the socket so the backend reaps the PTY.
  useEffect(() => {
    const bye = () => wsRef.current?.close();
    window.addEventListener('beforeunload', bye);
    return () => window.removeEventListener('beforeunload', bye);
  }, []);

  const reconnect = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || ws.readyState > WebSocket.OPEN) connectRef.current();
  }, []);

  return { hostRef, status, text, reconnect };
}
