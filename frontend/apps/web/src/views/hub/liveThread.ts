// Live delivery of the objective-manager thread (tracking_ui/chat_api.py).
//
// Primary: an EventSource on /api/loops/chat/events/{id} — the server pushes the
// full thread view whenever its `rev` changes (queued → working → reply folded,
// attach/offline flips). Fallback: long-poll /api/loops/chat/wait/{id}?rev= for
// browsers/proxies that can't hold a stream. Either way every view is a fresh read
// of the store — this module never synthesizes text or state.
import { parseThreadEvent, type ThreadResponse } from '@loopyard/api';

/** connecting = opening/reconnecting; sse / poll = live via that transport;
 *  down = the dashboard or loops server can't be reached right now. */
export type LiveMode = 'connecting' | 'sse' | 'poll' | 'down';

type ESLike = {
  readyState: number;
  addEventListener(type: string, fn: (e: Event) => void): void;
  close(): void;
};
export type ESCtor = new (url: string, init?: { withCredentials?: boolean }) => ESLike;

export interface SubscribeOpts {
  url: string;
  withCredentials?: boolean;
  onView(v: ThreadResponse): void;
  onMode(m: LiveMode, why?: string): void;
  /** Long-poll: resolve with the view once its rev differs from `rev`. */
  wait(rev: string): Promise<ThreadResponse>;
  EventSource?: ESCtor | undefined;
  sleep?: (ms: number) => Promise<void>;
}

/** Native errors in a row (no `thread` event between) before we give up on SSE. */
export const SSE_MAX_FAILS = 3;
const CLOSED = 2;

export function subscribeThread(o: SubscribeOpts): () => void {
  const sleep = o.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  let stopped = false;
  let es: ESLike | null = null;

  const poll = async () => {
    let rev = '';
    while (!stopped) {
      try {
        const v = await o.wait(rev);
        if (stopped) return;
        o.onMode('poll');
        if (v.changed !== false || v.rev !== rev) o.onView(v);
        // an old server without `rev` (or one that ignores it) would answer
        // instantly — pace it instead of hot-looping
        if (!v.rev || (rev && v.rev === rev && v.changed !== false)) await sleep(3000);
        rev = v.rev || '';
      } catch (e) {
        if (stopped) return;
        o.onMode('down', e instanceof Error ? e.message : String(e));
        await sleep(5000);
      }
    }
  };

  if (!o.EventSource) {
    o.onMode('connecting');
    void poll();
  } else {
    o.onMode('connecting');
    let fails = 0;
    es = new o.EventSource(o.url, o.withCredentials ? { withCredentials: true } : undefined);
    es.addEventListener('open', () => {
      if (!stopped) o.onMode('sse');
    });
    es.addEventListener('thread', (e) => {
      const v = parseThreadEvent((e as MessageEvent).data);
      if (!v || stopped) return;
      fails = 0;
      o.onMode('sse');
      o.onView(v);
    });
    // `error` is both the server's named event (loops server unreachable — carries
    // data; the stream stays open) and EventSource's native transport error.
    es.addEventListener('error', (e) => {
      if (stopped) return;
      const data = (e as MessageEvent).data;
      if (typeof data === 'string' && data) {
        let why = 'thread unavailable';
        try {
          why = (JSON.parse(data) as { error?: string }).error || why;
        } catch {
          /* keep the generic reason */
        }
        o.onMode('down', why);
        return;
      }
      fails += 1;
      if (es && (es.readyState === CLOSED || fails >= SSE_MAX_FAILS)) {
        es.close();
        es = null;
        void poll();
      } else {
        o.onMode('connecting');
      }
    });
  }

  return () => {
    stopped = true;
    es?.close();
    es = null;
  };
}
