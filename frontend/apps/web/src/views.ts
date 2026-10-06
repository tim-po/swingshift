// The route table. A view is registered when src/views/<id>/index.tsx exists
// (default-exporting its component) — discovered automatically, so adding a
// view never means editing the router. What the SIDEBAR shows is decided by
// shell/navMeta.ts (+ progressive disclosure), not here.
import type { ComponentType } from 'react';

export interface ViewDef {
  id: string;
  label: string;
  /** Extra sub-paths the view handles, e.g. ':id' for /issues/:id. */
  subpaths?: string[];
  /** Route-only alias: send /<id>/<rest> to <redirect>/<rest>. */
  redirect?: string;
}

export const VIEWS: ViewDef[] = [
  { id: 'overview', label: 'Home' },
  { id: 'loops', label: 'Loops', subpaths: [':host/:name'] },
  { id: 'plans', label: 'Plans', subpaths: [':oid', ':oid/:slug'] },
  { id: 'city', label: 'City' },
  { id: 'issues', label: 'Issues', subpaths: [':id'] },
  { id: 'machines', label: 'Machines', subpaths: [':tab'] },
  { id: 'library', label: 'Library', subpaths: [':tab', ':tab/:id'] },
  { id: 'projects', label: 'Projects' },
  { id: 'newloop', label: 'New loop' },
  { id: 'terminal', label: 'Terminal' }, // reached from Machines
  // Kept routable so old links and power-user bookmarks still land somewhere.
  { id: 'devices', label: 'Machines', redirect: '/machines/all' },
  { id: 'origins', label: 'Machines', redirect: '/machines/computers' },
  { id: 'sessions', label: 'Machines', redirect: '/machines/sessions' },
  { id: 'workspace', label: 'Plans', subpaths: [':oid', ':oid/:slug'], redirect: '/plans' },
  { id: 'hub', label: 'Plans', subpaths: [':id'] }, // legacy /hub → /plans
  { id: 'chat', label: 'Plans', subpaths: [':id'], redirect: '/plans' }, // chat now lives on each plan
  { id: 'roles', label: 'Library', redirect: '/library/agents' }, // /roles/:id → /library/agents/:id
];

const modules = import.meta.glob<{ default: ComponentType }>('./views/*/index.tsx');
export const viewLoader = (id: string) => modules[`./views/${id}/index.tsx`];
