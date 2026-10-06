// Shared presentational building blocks. Use these before inventing new ones;
// if a view needs a variant, extend here rather than copying CSS.
import { Component, type ErrorInfo, type ReactNode } from 'react';
import { stateLabel } from '@loopyard/api';
import './ui.css';

export function Kick({ children, tone }: { children: ReactNode; tone?: 'rose' | 'phos' | 'stone' }) {
  return <span className={'kick' + (tone ? ' ' + tone : '')}>{children}</span>;
}

/** State badge: `.b-<state>` colours (running, finished, error, saved, …). */
export function Badge({ state, children }: { state: string; children?: ReactNode }) {
  // Colour keys off the raw state; the visible text is always the friendly label.
  return <span className={`badge b-${state}`}>{children ?? stateLabel(state)}</span>;
}

export function Pill({ tone, children, title }: { tone?: 'ember' | 'phos' | 'rose'; children: ReactNode; title?: string }) {
  return <span className={'pill' + (tone ? ' ' + tone : '')} title={title}>{children}</span>;
}

export function Chip({ on, onClick, children, title }: { on?: boolean; onClick?: () => void; children: ReactNode; title?: string }) {
  return onClick ? (
    <button type="button" className={'chip' + (on ? ' on' : '')} onClick={onClick} aria-pressed={on} title={title}>{children}</button>
  ) : (
    <span className={'chip' + (on ? ' on' : '')} title={title}>{children}</span>
  );
}

export function Btn({ variant = 'ghost', size, className, children, ...rest }: { variant?: 'ghost' | 'primary' | 'danger' | 'quiet'; size?: 'sm' | 'lg' } & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const cls = ['btn', variant, size, className].filter(Boolean).join(' ');
  return <button type="button" className={cls} {...rest}>{children}</button>;
}

export function Empty({ title, children, tone }: { title: ReactNode; children?: ReactNode; tone?: 'err' }) {
  return (
    <div className={'empty' + (tone ? ' ' + tone : '')}>
      <span className="kick">{title}</span>
      {children}
    </div>
  );
}

/**
 * Turn any thrown error into calm, human copy — never a raw traceback, JSON
 * blob, or `HTTP 500` shown to a beta tester. `what` is the thing we tried to
 * load ("loops", "devices"), phrased so it slots into a sentence.
 */
export function friendlyError(error: unknown, what: string): { title: string; detail: string } {
  const status = (error as { status?: number } | null)?.status;
  const raw = (error as { message?: string } | null)?.message ?? '';
  const looksOffline = /failed to fetch|networkerror|load failed|fetch/i.test(raw) || (error instanceof TypeError);

  if (status === 401 || status === 403 || /log(ged)? ?in|sign|unauth|session/i.test(raw)) {
    return { title: 'Please sign in again', detail: `Your session ended. Sign in and your ${what} will be right here.` };
  }
  if (looksOffline && status === undefined) {
    return { title: "Can't reach the hub", detail: `Check your connection — we'll load your ${what} the moment you're back.` };
  }
  if (typeof status === 'number' && status >= 500) {
    return { title: 'Something went wrong', detail: `The hub had a hiccup loading your ${what}. This is usually temporary — give it another try.` };
  }
  if (status === 404) {
    return { title: 'Nothing here yet', detail: `We couldn't find your ${what}. It may not be set up yet.` };
  }
  return { title: `Couldn't load ${what}`, detail: 'Give it another try in a moment.' };
}

/**
 * One calm line for a failed *action* (start, save, brief) — never the raw
 * `(e as Error).message`. `action` is a verb phrase that slots after "Couldn't".
 */
export function friendlyLine(error: unknown, action: string): string {
  const status = (error as { status?: number } | null)?.status;
  const raw = (error as { message?: string } | null)?.message ?? '';
  const looksOffline = /failed to fetch|networkerror|load failed|fetch/i.test(raw) || error instanceof TypeError;
  if (status === 401 || status === 403) return `Couldn't ${action} — please sign in again.`;
  if (looksOffline && status === undefined) return `Couldn't ${action} — check your connection and try again.`;
  return `Couldn't ${action} — try again in a moment.`;
}

/** Standard loading / error / empty handling for a query result. */
export function QueryState({
  isPending,
  error,
  what,
  onRetry,
}: {
  isPending: boolean;
  error: unknown;
  what: string;
  /** Wire this to react-query's `refetch` so the error state can recover in place. */
  onRetry?: () => void;
}) {
  if (isPending) return <Skeleton className="sk-page" lines={5} />;
  if (error) {
    const { title, detail } = friendlyError(error, what);
    // Deliberately never render the raw error message: beta testers see calm
    // copy + a way to recover, never a traceback, `HTTP 500`, or a JSON blob.
    return (
      <Empty tone="err" title={title}>
        <p>{detail}</p>
        {onRetry && <Btn variant="primary" onClick={onRetry}>Try again</Btn>}
      </Empty>
    );
  }
  return null;
}

/**
 * Last-line guard against a blank white screen. If any child throws while
 * rendering — e.g. the backend hands back a shape we didn't expect — we catch
 * it and show calm copy with a way to recover, never React's crashed tree or a
 * raw stack. `page` wraps the fallback in the standard column so it sits right
 * inside the shell; the top-level boundary uses `page={false}` to centre on a
 * bare screen. `resetKey` lets a parent clear the error when the user navigates
 * or the underlying data changes (change the key → the boundary re-renders its
 * children fresh).
 */
type ErrorBoundaryProps = { children: ReactNode; page?: boolean; resetKey?: unknown };
type ErrorBoundaryState = { error: unknown };

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { error };
  }

  componentDidUpdate(prev: ErrorBoundaryProps) {
    // Recover automatically when the parent signals a fresh context (route change).
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    // Keep the real detail in the dev console for us; the tester never sees it.
    if (import.meta.env?.DEV) console.error('A view crashed:', error, info?.componentStack);
  }

  private reset = () => this.setState({ error: null });

  private reload = () => {
    if (typeof window !== 'undefined') window.location.reload();
  };

  render() {
    if (!this.state.error) return this.props.children;
    // Deliberately no error.message / stack: beta testers get reassurance + a
    // way out, never a traceback or a dead white page.
    const fallback = (
      <Empty tone="err" title="This view hit a snag">
        <p>Something on this page didn't load right. Your work is safe — this is usually temporary.</p>
        <div className="eb-actions">
          <Btn variant="primary" onClick={this.reset}>Try again</Btn>
          <Btn onClick={this.reload}>Reload the app</Btn>
        </div>
      </Empty>
    );
    return this.props.page === false ? fallback : <div className="page">{fallback}</div>;
  }
}

export function Card({ children, className, ...rest }: { children: ReactNode } & React.HTMLAttributes<HTMLDivElement>) {
  return <div className={'card' + (className ? ' ' + className : '')} {...rest}>{children}</div>;
}

/** Page wrapper for single-column views: title + sub + body. */
export function Page({ title, sub, actions, children }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="page">
      <header className="pagehead">
        <div>
          <h1 className="h1">{title}</h1>
          {sub && <div className="sub">{sub}</div>}
        </div>
        {actions && <div className="pageactions">{actions}</div>}
      </header>
      {children}
    </div>
  );
}

/** Grey placeholder blocks shaped like the content that is loading. */
export function Skeleton({ lines = 3, block, className }: { lines?: number; block?: number; className?: string }) {
  return (
    <div className={'sk' + (className ? ' ' + className : '')} aria-busy="true" aria-label="Loading">
      {block ? <div className="sk-block" style={{ height: block }} /> : null}
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="sk-line" style={{ width: `${[92, 78, 85, 60, 70][i % 5]}%` }} />
      ))}
    </div>
  );
}
