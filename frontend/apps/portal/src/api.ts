// Thin client for the control plane's portal API (control_plane/*.py).
// Every call is same-origin with credentials so the lyp_sess cookie rides along
// and the browser's Origin header satisfies the CP's CSRF check.

declare const __PORTAL_URL__: string;

/** LOOPYARD_PORTAL_URL at build time; falls back to wherever we're served from. */
export function portalUrl(): string {
  const baked = typeof __PORTAL_URL__ === 'string' ? __PORTAL_URL__ : '';
  return baked || window.location.origin;
}

export type Me = { id: string; email: string; display_name: string; is_owner: boolean };

export type Hub = {
  id: string;
  fingerprint: string;
  public_url: string;
  holder_origin_id: string;
  status: 'online' | 'offline';
  last_seen: number;
  state_version: number;
};

export type Onboarding = {
  id: string;
  label: string;
  expires_at: number;
  kind: string;
  command: string;
  stub: boolean;
  script?: string;
  agent_setup_url?: string;
  runtime?: string;
  trusted_workspace?: boolean;
};

export type OnboardStatus = {
  id: string;
  status: 'pending' | 'registered' | 'online' | 'expired';
  hub: Hub | null;
  handoff_url: string | null;
};

export type Invite = {
  email: string;
  issued_by: string;
  issued_at: number;
  redeemed_by: string | null;
  redeemed_at: number | null;
  status: 'open' | 'redeemed' | 'revoked';
};

export class ApiError extends Error {
  constructor(public status: number) {
    super(`request failed (${status})`);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { accept: 'application/json', ...(init?.body ? { 'content-type': 'application/json' } : {}) },
  });
  if (!res.ok) throw new ApiError(res.status);
  return (await res.json()) as T;
}

/** null when signed out (401); any other failure throws. */
export async function getMe(): Promise<Me | null> {
  try {
    return await call<Me>('/api/me');
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

export const listHubs = () => call<{ hubs: Hub[] }>('/api/hubs').then((r) => r.hubs);

export const startOnboarding = (label: string, runtime: string, trusted_workspace: boolean) =>
  call<Onboarding>('/api/onboard', { method: 'POST', body: JSON.stringify({ label, runtime, trusted_workspace }) });

export const onboardingStatus = (id: string) => call<OnboardStatus>(`/api/onboard/${encodeURIComponent(id)}`);

/** §6a: a device token for THIS browser, bound to the hub; shown once, never stored here. */
export const issueDeviceToken = (hubId: string, label: string) =>
  call<{ id: string; token: string; hub_id: string }>('/api/devices', {
    method: 'POST',
    body: JSON.stringify({ hub_id: hubId, device_label: label }),
  });

/**
 * The hub's sign-in exchange (control_plane/hub_guard.py). The token rides in the
 * URL fragment, which browsers never send to a server, so it stays out of logs and
 * Referer; the hub's page POSTs it and swaps it for an httponly cookie.
 */
export function enterUrl(hubUrl: string, token: string): string | null {
  const base = safeHubUrl(hubUrl);
  if (!base) return null;
  return `${base.replace(/\/$/, '')}/_lyp/enter#t=${encodeURIComponent(token)}`;
}

/** Indirection so tests can observe the handoff without jsdom navigation. */
export const nav = { go: (url: string) => window.location.assign(url) };

/** Owner-only allowlist admin (control_plane/routes_admin.py). */
export const listInvites = () => call<{ invites: Invite[] }>('/api/admin/invites').then((r) => r.invites);

export const addInvite = (email: string) =>
  call<Invite>('/api/admin/invites', { method: 'POST', body: JSON.stringify({ email }) });

export const revokeInvite = (email: string) =>
  call<{ email: string; status: 'revoked' }>('/api/admin/invites/revoke', {
    method: 'POST',
    body: JSON.stringify({ email }),
  });

export const logout = () => call<{ ok: boolean }>('/logout', { method: 'POST' });

/** Google sign-in starts server-side; come back to `next` (a portal path). */
export const loginHref = (next = '/') => `${portalUrl()}/login?next=${encodeURIComponent(next)}`;

/** Only hand off to an https hub address; anything else renders no link. */
export function safeHubUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === 'https:' && !u.username && !u.password ? u.href : null;
  } catch {
    return null;
  }
}
