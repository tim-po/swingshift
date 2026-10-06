// The REAL process seams for the prerequisite check (prereqs.mjs) — plain node,
// no Electron. main.js hands these to checkPrereqs / createPrereqController.
import { accessSync, constants } from 'node:fs';
import { execFile, spawn } from 'node:child_process';

/** True when `p` is an executable file. */
export function isExecutable(p) {
  try { accessSync(p, constants.X_OK); return true; } catch { return false; }
}

/** Run a short probe to completion → {code, stdout, stderr}; never rejects. */
export function makeRun({ env = process.env, timeoutMs = 10000 } = {}) {
  return (cmd, args) => new Promise((resolve) => {
    execFile(cmd, args, { env, timeout: timeoutMs, encoding: 'utf8' }, (err, stdout, stderr) => {
      const code = err ? (typeof err.code === 'number' ? err.code : 1) : 0;
      resolve({ code, stdout: stdout || '', stderr: stderr || '' });
    });
  });
}

/** A per-stream line assembler: push(chunk) emits each COMPLETE non-empty line
 *  (so a sign-in URL split across two pipe chunks still arrives whole); the
 *  unterminated tail is held until its newline, until `idleMs` of silence (a
 *  prompt like "Paste code here if prompted >" has no newline and the child
 *  then waits), or until flush() — on stdin write and on exit. */
export function makeLineBuffer(onLine, { idleMs = 250, setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  let tail = '';
  let timer = null;
  const emit = (l) => { const t = l.trim(); if (t) onLine(t); };
  const flush = () => {
    if (timer) { clearTimer(timer); timer = null; }
    const t = tail; tail = '';
    emit(t);
  };
  const push = (chunk) => {
    const parts = (tail + String(chunk)).split(/\r\n|[\r\n]/);
    tail = parts.pop();
    parts.forEach(emit);
    if (timer) { clearTimer(timer); timer = null; }
    if (tail.trim()) timer = setTimer(flush, idleMs);
  };
  return { push, flush };
}

/** Run a fix, calling onLine for each non-empty output line → Promise<exit code>.
 *  stdin is closed (a fix that wants a TTY fails fast instead of hanging) unless
 *  `interactive`: then it is a pipe and onStdin(write) receives the writer — the
 *  in-app code field for `claude auth login`'s "Paste code here" step. */
export function makeSpawnLines({ env = process.env, idleMs = 250 } = {}) {
  return (cmd, args, onLine = () => {}, { interactive = false, onStdin = () => {} } = {}) => new Promise((resolve) => {
    let child;
    const out = makeLineBuffer(onLine, { idleMs });
    const err = makeLineBuffer(onLine, { idleMs });
    const flushAll = () => { out.flush(); err.flush(); };
    try {
      child = spawn(cmd, args, { env, stdio: [interactive ? 'pipe' : 'ignore', 'pipe', 'pipe'] });
      if (interactive) {
        child.stdin.on('error', () => {});
        // Answering ends the prompt line: emit it before any output the answer causes.
        onStdin((s) => { flushAll(); if (child.stdin.writable) child.stdin.write(s); });
      }
    } catch (e) {
      onLine(`cannot run ${cmd}: ${e && e.message ? e.message : e}`);
      resolve(127);
      return;
    }
    child.stdout.setEncoding('utf8'); // a multi-byte char split across chunks decodes whole
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', out.push);
    child.stderr.on('data', err.push);
    let done = false;
    child.on('error', (e) => { if (!done) { done = true; flushAll(); onLine(`cannot run ${cmd}: ${e.message}`); resolve(127); } });
    child.on('close', (code, sig) => { if (!done) { done = true; flushAll(); resolve(code == null ? (sig ? 143 : 1) : code); } });
  });
}
