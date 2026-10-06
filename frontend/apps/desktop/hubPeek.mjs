// Read the cert fingerprint a wss:// Hub presents — the app-side twin of
// origin_client.peek_hub_cert_fingerprint. The connect form shows it next to the
// fingerprint from the join link ("matches ✓") BEFORE the single-use pairing code
// is sent anywhere; `yard origin up --hub-fingerprint` still re-checks it.
// Nothing is sent over the socket: TLS handshake, read the cert, close.
import tls from 'node:tls';
import { fingerprintState, normalizeFingerprint } from './originAgent.mjs';

/** host/port of a ws(s):// Hub URL (port default 8799 == origin_client). */
export function hubEndpoint(hubUrl) {
  let u;
  try { u = new URL(String(hubUrl || '').trim()); } catch { throw new Error('not a Hub URL'); }
  if (u.protocol !== 'wss:' && u.protocol !== 'ws:') throw new Error('the Hub URL must start with wss://');
  if (!u.hostname) throw new Error('no host in the Hub URL');
  return { host: u.hostname.replace(/^\[|\]$/g, ''), port: Number(u.port) || 8799, secure: u.protocol === 'wss:' };
}

/** → Promise<{fingerprint} | {error}>; never rejects. `connect` is injectable
 *  (tls.connect's shape) so tests drive it without a network. */
export function peekHubFingerprint(hubUrl, { connect = tls.connect, timeoutMs = 5000 } = {}) {
  return new Promise((resolve) => {
    let ep;
    try { ep = hubEndpoint(hubUrl); } catch (e) { resolve({ error: e.message }); return; }
    if (!ep.secure) { resolve({ fingerprint: null, plaintext: true }); return; }
    let done = false;
    let sock = null;
    const finish = (r) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      try { sock && sock.destroy(); } catch { /* gone */ }
      resolve(r);
    };
    const timer = setTimeout(() => finish({ error: `no answer from ${ep.host}:${ep.port}` }), timeoutMs);
    try {
      // identity is the fingerprint, compared by the caller — like the Python peek
      sock = connect({ host: ep.host, port: ep.port, servername: /^[\d.:]+$/.test(ep.host) ? undefined : ep.host, rejectUnauthorized: false }, () => {
        const cert = sock.getPeerCertificate();
        const fp = normalizeFingerprint(cert && cert.fingerprint256);
        finish(fp ? { fingerprint: fp } : { error: 'the Hub presented no certificate' });
      });
      sock.on('error', (e) => finish({ error: `cannot reach the Hub: ${e.code || e.message}` }));
    } catch (e) {
      finish({ error: `cannot reach the Hub: ${e.message}` });
    }
  });
}

/** The form's fingerprint row: peek the Hub, then classify against `expected`
 *  (see fingerprintState) → {state, seen, error?}. `peek` is injectable. */
export async function checkHub({ hub, expected } = {}, { peek = peekHubFingerprint } = {}) {
  const r = await peek(hub);
  const state = fingerprintState({ hub, expected, seen: r.fingerprint, error: r.error });
  return { state, seen: r.fingerprint || null, ...(r.error ? { error: r.error } : {}) };
}
