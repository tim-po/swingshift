// The ONLY bridge the onboarding-guide panel (guide.html) gets — a separate,
// narrower preload than the connect screen's: it cannot connect, disconnect or
// run fixes. It sends a step/place KEY; main maps it to a web-UI route.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('loopyardGuide', {
  // The panel's view: {agent{id,name,persona}, welcome, prompt, tracked,
  // steps[{key,title,what,route,done}], places, open, dismissed, source} — `done`
  // is what the engine reports, re-read while the panel is open.
  get: () => ipcRenderer.invoke('guide:get'),
  // Move the web UI to the surface for `key` (it does not tick the step). → {ok, route, view}
  go: (key) => ipcRenderer.invoke('guide:go', key),
  hide: () => ipcRenderer.invoke('guide:hide'),
  // "Don't show on start" — remembered; Help ▸ Onboarding guide still reopens it.
  dismiss: () => ipcRenderer.invoke('guide:dismiss'),
  onView: (cb) => {
    const handler = (_e, v) => cb(v);
    ipcRenderer.on('guide:view', handler);
    return () => ipcRenderer.removeListener('guide:view', handler);
  },
});
