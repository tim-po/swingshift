import { lazy, Suspense } from 'react';
import { createBrowserRouter, createHashRouter, Navigate, useLocation, type RouteObject } from 'react-router-dom';
import { config } from './config';
import { Layout } from './shell/Layout';
import { ErrorBoundary, Skeleton } from './components/ui';
import { VIEWS, viewLoader } from './views';

/** /<from>/<rest>?q → <to>/<rest>?q — keeps old links and bookmarks working. */
function Alias({ from, to }: { from: string; to: string }) {
  const { pathname, search } = useLocation();
  const rest = pathname.replace(new RegExp(`^/${from}`), '');
  return <Navigate to={to + rest + search} replace />;
}

function viewRoutes(): RouteObject[] {
  return VIEWS.flatMap((v) => {
    if (v.redirect) {
      const to = v.redirect;
      return [v.id, `${v.id}/*`].map((path) => ({ path, element: <Alias from={v.id} to={to} /> }));
    }
    const load = viewLoader(v.id);
    if (!load) return [];
    const View = lazy(load);
    // A boundary per route: one view crashing shows calm copy in place and
    // leaves the shell (nav, project switcher) alive, instead of blanking the
    // whole app. `resetKey={path}` clears a caught error when you navigate away.
    return [v.id, ...(v.subpaths ?? []).map((s) => `${v.id}/${s}`)].map((path) => ({
      path,
      element: (
        <ErrorBoundary resetKey={path}>
          <Suspense fallback={<Skeleton className="sk-page" lines={5} />}>
            <View />
          </Suspense>
        </ErrorBoundary>
      ),
    }));
  });
}

const routes: RouteObject[] = [
  {
    path: '/',
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/overview" replace /> },
      ...viewRoutes(),
      { path: '*', element: <Navigate to="/overview" replace /> },
    ],
  },
];

const make = config.router === 'hash' ? createHashRouter : createBrowserRouter;
export const router = make(routes, { basename: config.router === 'hash' ? undefined : config.basename || undefined });
