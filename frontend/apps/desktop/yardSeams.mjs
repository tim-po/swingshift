// The REAL process seams the desktop face runs on — plain node (no Electron), so
// they are exercised against the actual `yard` CLI in originAgent.cli.test.mjs.
// main.js only picks the command + env and hands these to OriginAgentFace.
import { spawn as nodeSpawn, spawnSync, execFile } from 'node:child_process';

// Keep at most this much of a failing verb's stderr (its reason + log tail).
const STDERR_CAP = 16 * 1024;

/** Build {spawn, run, readStatus} over `<cmd> <pre…> <argv…>`. */
export function createYardSeams({ cmd, pre = [], env = process.env, statusTimeoutMs = 8000, runTimeoutMs = 30000 } = {}) {
  if (!cmd) throw new Error('createYardSeams needs the yard command');
  return {
    // `yard origin up …`: stdin carries the pair code (`--pair-code -`), stdout is
    // dropped, stderr is collected for the one-line failure reason.
    spawn(argv, stdin) {
      const child = nodeSpawn(cmd, [...pre, ...argv], { stdio: ['pipe', 'ignore', 'pipe'], env });
      let err = '';
      const waiters = [];
      let done = null;
      const finish = (code) => {
        if (done) return;
        done = { code, err };
        for (const cb of waiters.splice(0)) cb(code, err);
      };
      child.stderr.on('data', (b) => { if (err.length < STDERR_CAP) err += b.toString('utf8'); });
      child.stdin.on('error', () => { /* exited before reading stdin */ });
      child.stdin.end(stdin || '');
      // 'error' = could not spawn at all (ENOENT: yard not installed) → 'close'
      // may never fire, so finish here with the reason.
      child.on('error', (e) => { err += `cannot run ${cmd}: ${e && e.message ? e.message : e}`; finish(127); });
      child.on('close', (code, signal) => finish(code == null ? (signal ? 143 : 1) : code));
      return {
        pid: child.pid,
        kill: (sig) => child.kill(sig),
        once: (ev, cb) => {
          if (ev !== 'exit') return;
          if (done) cb(done.code, done.err); else waiters.push(cb);
        },
      };
    },
    // Short verbs run to completion (`yard origin down`).
    run(argv) {
      const r = spawnSync(cmd, [...pre, ...argv], { encoding: 'utf8', timeout: runTimeoutMs, env });
      return { code: r.status == null ? 1 : r.status, stderr: r.stderr || String(r.error || '') };
    },
    // `yard origin status --json` → the parsed payload, or null for "no daemon".
    readStatus(argv) {
      const r = spawnSync(cmd, [...pre, ...argv], { encoding: 'utf8', timeout: statusTimeoutMs, env });
      return r.status === 0 ? parseStatus(r.stdout) : null;
    },
    // The same read without blocking the caller: the app's poll uses this, so a
    // slow CLI start never freezes the window (a sync read stalls Electron's main
    // process for as long as python takes to start).
    readStatusAsync(argv) {
      return new Promise((resolve) => {
        execFile(cmd, [...pre, ...argv], { encoding: 'utf8', timeout: statusTimeoutMs, env }, (err, stdout) => {
          resolve(err ? null : parseStatus(stdout));
        });
      });
    },
  };
}

/** Parse `yard origin status --json` stdout → the payload, or null (no daemon). */
export function parseStatus(stdout) {
  if (!stdout) return null;
  try {
    const s = JSON.parse(String(stdout).trim());
    return s && (Array.isArray(s.services) || (s.state && s.state !== 'unknown')) ? s : null;
  } catch {
    return null;
  }
}
