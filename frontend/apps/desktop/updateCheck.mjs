// B-9 desktop update check. The portal serves the gated release manifest at
// <portal>/releases/latest/release.json ({version, created_at, targets, assets};
// control_plane/releases.py). The app compares it with app.getVersion() and shows
// an update banner. Update only opens the portal's release page (D2: the app
// tree is replaced by the user, never patched in place), and it stays DISABLED
// while loops are live — replacing the app under a running stack would orphan it.
// Pure helpers + one controller with injected seams; main.js owns electron.

export const UPDATE_CHECK_MS = 6 * 60 * 60 * 1000;
const SEMVER = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/;

/** The portal origin: $LOOPYARD_PORTAL_URL, else the `portal_url` that `yard enroll`
 *  saved in <hub state>/enroll.json. HTTPS only (http allowed for 127.0.0.1 dev). */
export function resolvePortal({ env = {}, enroll = null } = {}) {
  const raw = env.LOOPYARD_PORTAL_URL || (enroll && typeof enroll === 'object' ? enroll.portal_url : '') || '';
  let u;
  try { u = new URL(String(raw)); } catch { return ''; }
  const local = u.protocol === 'http:' && (u.hostname === '127.0.0.1' || u.hostname === 'localhost');
  if ((u.protocol !== 'https:' && !local) || u.username || u.password || u.search || u.hash) return '';
  return u.origin + u.pathname.replace(/\/+$/, '');
}

/** <state home>/data/_hub/enroll.json — where `yard enroll` keeps the portal link
 *  ($LOOPYARD_HUB_STATE_DIR overrides the hub state dir, as in yard.py). */
export function enrollPath({ stateHome = '', dataDir = '', hubStateDir = '' } = {}) {
  if (hubStateDir) return `${String(hubStateDir).replace(/\/+$/, '')}/enroll.json`;
  const data = dataDir || (stateHome ? `${String(stateHome).replace(/\/+$/, '')}/data` : '');
  return data ? `${data.replace(/\/+$/, '')}/_hub/enroll.json` : '';
}

const DEVICE_TOKEN = /^lyd_[A-Za-z0-9_-]{1,196}$/;

/** The request the update check makes: the portal plus the hub's lyd_ device
 *  token as `Authorization: Bearer lyd_…` (the portal lets a live device token
 *  read release.json only). The token is sent ONLY to the portal it was issued
 *  by — an env override pointing elsewhere gets no Authorization header. */
export function updateAuth({ env = {}, enroll = null } = {}) {
  const portal = resolvePortal({ env, enroll });
  const token = enroll && typeof enroll === 'object' ? enroll.device_token : '';
  if (!portal || typeof token !== 'string' || !DEVICE_TOKEN.test(token)) return { portal, headers: {} };
  if (resolvePortal({ enroll }) !== portal) return { portal, headers: {} };
  return { portal, headers: { Authorization: `Bearer ${token}` } };
}

export const manifestUrl = (portal) => `${portal}/releases/latest/release.json`;
export const releasePage = (portal) => `${portal}/#/setup`;

function parse(v) {
  const m = SEMVER.exec(String(v || '').trim());
  return m ? { n: [+m[1], +m[2], +m[3]], pre: m[4] || '' } : null;
}

/** semver order: -1 / 0 / 1; null when either side is not a version. */
export function compareVersions(a, b) {
  const x = parse(a), y = parse(b);
  if (!x || !y) return null;
  for (let i = 0; i < 3; i++) if (x.n[i] !== y.n[i]) return x.n[i] < y.n[i] ? -1 : 1;
  if (x.pre === y.pre) return 0;
  if (!x.pre) return 1; // a release outranks its pre-releases
  if (!y.pre) return -1;
  return x.pre < y.pre ? -1 : 1;
}

/** One check → {state, current, latest?, releaseUrl?, error?}.
 *  state: 'current' | 'available' | 'signin' (portal wants a session) | 'unavailable'. */
export async function checkForUpdate({ portal, current, fetchImpl, headers = {} }) {
  if (!portal) return { state: 'unavailable', current, error: 'no portal configured' };
  const base = { current, releaseUrl: releasePage(portal) };
  let res;
  try {
    res = await fetchImpl(manifestUrl(portal), { headers: { Accept: 'application/json', ...headers }, redirect: 'error' });
  } catch (err) {
    return { ...base, state: 'unavailable', error: String(err && err.message ? err.message : err) };
  }
  if (res.status === 401 || res.status === 403) return { ...base, state: 'signin' };
  if (!res.ok) return { ...base, state: 'unavailable', error: `portal answered ${res.status}` };
  let manifest;
  try { manifest = await res.json(); } catch { return { ...base, state: 'unavailable', error: 'invalid release.json' }; }
  const latest = manifest && typeof manifest.version === 'string' ? manifest.version.replace(/^v/, '') : '';
  const order = compareVersions(current, latest);
  if (order === null) return { ...base, state: 'unavailable', error: 'invalid release version' };
  return { ...base, state: order < 0 ? 'available' : 'current', latest };
}

/** What the banner shows, given the last check and the live origin view.
 *  Update is enabled only when an update exists AND nothing is live. */
export function updateBanner(check, view) {
  if (!check || check.state !== 'available') return { show: false, updateEnabled: false };
  const live = !!(view && (view.live || view.phase === 'connecting'));
  return {
    show: true,
    latest: check.latest,
    current: check.current,
    text: `Loopyard ${check.latest} is available (you have ${check.current}).`,
    updateEnabled: !live,
    disabledReason: live ? 'Stop Loopyard to update — loops are live.' : '',
    releaseUrl: check.releaseUrl,
  };
}

/** Periodic checker. `update()` refuses while live (the renderer's disabled
 *  button is not the only guard) and otherwise opens the release page. */
export function createUpdateController({ check, openUrl, getView, onChange, setTimer = setInterval, clearTimer = clearInterval, every = UPDATE_CHECK_MS }) {
  let last = null, timer = null;
  const banner = () => updateBanner(last, getView());
  const run = async () => {
    try { last = await check(); } catch (err) { last = { state: 'unavailable', error: String(err) }; }
    onChange(banner());
    return last;
  };
  return {
    start() { if (!timer) timer = setTimer(run, every); return run(); },
    stop() { if (timer) clearTimer(timer); timer = null; },
    refresh: run,
    banner,
    update() {
      const b = banner();
      if (!b.show) return { ok: false, error: 'No update available.' };
      if (!b.updateEnabled) return { ok: false, error: b.disabledReason };
      openUrl(b.releaseUrl);
      return { ok: true };
    },
    viewChanged() { onChange(banner()); },
  };
}
