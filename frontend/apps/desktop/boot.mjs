// The app's start-up ORDER (deliverable 4, fast time-to-UI), kept out of
// main.js so it is unit-tested without Electron:
//   1. every IPC channel is registered FIRST. A handler awaits the engine, so a
//      page that calls before the engine is ready waits instead of failing with
//      "No handler registered".
//   2. the window is created (the connect screen paints at once).
//   3. only THEN is the engine built (the yard command resolved, the status
//      poll, prereq controller, guide content), off the paint path.
// A build failure reaches every waiting call as {ok:false, error}, never a hang.

/**
 * @param {object} p
 * @param {(channel: string, fn: Function) => void} p.handle   ipcMain.handle
 * @param {Record<string, (deps: object, ...args: any[]) => any>} p.routes
 * @param {() => void} p.createWindow
 * @param {() => Promise<object>} p.build   resolves to the deps routes receive
 * @returns {Promise<object>} the deps (rejects if the build failed)
 */
export function bootApp({ handle, routes, createWindow, build }) {
  let resolve;
  let reject;
  const ready = new Promise((res, rej) => { resolve = res; reject = rej; });
  ready.catch(() => { /* surfaced per call below */ });
  for (const [channel, fn] of Object.entries(routes)) {
    handle(channel, async (_e, ...args) => {
      let deps;
      try {
        deps = await ready;
      } catch (err) {
        return { ok: false, error: `Loopyard could not start: ${err && err.message ? err.message : err}` };
      }
      return fn(deps, ...args);
    });
  }
  createWindow();
  Promise.resolve().then(build).then(resolve, reject);
  return ready;
}
