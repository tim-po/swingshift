// The one place that knows how to reach the Loopyard API. Platform-neutral:
// no window/document access, so a future desktop (Tauri/Electron) or mobile
// (Capacitor/Expo) shell can reuse it by passing its own baseUrl + auth.

export interface Auth {
  /** Extra headers per request (e.g. a bearer token in a native shell). */
  headers(): Record<string, string>;
  /** fetch credentials mode; the web app rides the dash_sess cookie. */
  credentials: RequestCredentials;
}

export const cookieAuth: Auth = { headers: () => ({}), credentials: 'include' };

export interface ClientOptions {
  /** '' = same origin. Must not end with '/'. */
  baseUrl: string;
  auth?: Auth;
  fetch?: typeof fetch;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/** The human message for an `{error, legal?}` body — the server's words, plus the
 *  legal targets when an illegal triage move lists them. */
export function errorBodyMessage(body: unknown): string | undefined {
  const e = body as { error?: string; legal?: string[] } | undefined;
  if (!e || typeof e !== 'object' || !e.error) return undefined;
  return e.error + (Array.isArray(e.legal) && e.legal.length ? ` (legal: ${e.legal.join(', ')})` : '');
}

export function createClient({ baseUrl, auth = cookieAuth, fetch: f = globalThis.fetch }: ClientOptions) {
  const base = baseUrl.replace(/\/+$/, '');

  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const res = await f(base + path, {
      ...init,
      credentials: auth.credentials,
      headers: { Accept: 'application/json', ...auth.headers(), ...(init.headers ?? {}) },
    });
    const text = await res.text();
    let body: unknown = undefined;
    try {
      body = text ? JSON.parse(text) : undefined;
    } catch {
      // A non-JSON body is almost always the gate's login page (session expired).
      throw new ApiError(res.status, path, res.ok ? 'expected JSON — are you logged in?' : `HTTP ${res.status}`);
    }
    if (!res.ok) {
      // Failed writes answer 4xx/502 with the same {error} body they used to send as 200.
      const msg = errorBodyMessage(body) ?? `HTTP ${res.status}`;
      throw new ApiError(res.status, path, msg);
    }
    return body as T;
  }

  return {
    get: <T>(path: string) => request<T>(path),
    post: <T>(path: string, data: unknown) =>
      request<T>(path, { method: 'POST', body: JSON.stringify(data), headers: { 'Content-Type': 'application/json' } }),
  };
}

export type Client = ReturnType<typeof createClient>;
