# Loopyard frontend

The dashboard UI: a Vite + React + TypeScript app that talks to the `/api/loops/*`
HTTP API served by `tracking_ui/loops_dashboard.py`.

```
frontend/
  packages/api/   typed API client + pure view-model helpers (no DOM — reusable by
                  a future desktop/mobile shell)
  apps/web/       the web app: one folder per view under src/views/<id>/
```

## Develop

```bash
cd frontend
npm ci
npm run dev        # http://127.0.0.1:5173, proxies /api, /ws, /static
```

The dev proxy target is `LOOPYARD_PROXY_TARGET` (default `http://127.0.0.1:8795`,
the dashboard on the VPS). See `apps/web/.env.example`.

**From a Mac against the VPS:** run the dev server on the VPS and tunnel it:

```bash
ssh -N -L 5173:localhost:5173 <vps>
```

then open http://localhost:5173. Edits hot-reload. Everything you click acts on
live data.

## Check

```bash
npm run typecheck && npm test && npm run build
```

## Deploy

The dashboard serves `apps/web/dist` under `/app/`, next to the legacy SPA at `/`.

```bash
cd frontend && npm ci && npm run build
systemctl --user restart bot-swarm-loopyard-dashboard
```

- `LOOPYARD_WEB_APP=0` hides `/app` (rollback).
- `LOOPYARD_WEB_APP_DEFAULT=1` makes `/` land on the new app.
- Without a build, `/app` is simply absent — the legacy dashboard is unaffected.

## Adding a view

Create `apps/web/src/views/<id>/index.tsx` with a default-exported component and
add an entry to `VIEWS` in `apps/web/src/views.ts`; routing picks it up. Put its
API calls and pure helpers in `packages/api/src/<area>.ts` with tests, reuse
`src/components/ui.tsx`, and read the global project scope via `useProjectScope()`.
