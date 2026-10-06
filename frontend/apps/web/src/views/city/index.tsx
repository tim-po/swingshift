// City — the project as a city: every loop a building, every agent a floor, every turn a
// window row. The scene is the owner's Loop City prototype ported as-is (scene.js); this
// view feeds it the app's data, follows the app's theme and links out to the real pages.
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { useNavigate } from 'react-router-dom';
import type { OverviewLoop } from '@loopyard/api';
import { Empty, QueryState } from '../../components/ui';
import { useLoops } from '../../api';
import { useProjectScope } from '../../scope';
import { useCityDocs, useCityIssues, useCityQuality, useLoopDetails } from './api';
import { CITY_MARKUP } from './markup';
import { mountCity, type CityHandle } from './scene.js';
import { buildSnap, snapSignature, type CitySnap } from './snapshot';
import './city.css';

type Theme = 'light' | 'dark';

// The effective theme: a pinned <html data-theme>, else the OS.
const dark = () => typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches;
const readTheme = (): Theme => {
  const t = document.documentElement.getAttribute('data-theme');
  return t === 'light' || t === 'dark' ? t : dark() ? 'dark' : 'light';
};
function subscribeTheme(cb: () => void) {
  const mq = matchMedia('(prefers-color-scheme: dark)');
  const mo = new MutationObserver(cb);
  mq.addEventListener('change', cb);
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  return () => {
    mq.removeEventListener('change', cb);
    mo.disconnect();
  };
}
const useTheme = () => useSyncExternalStore(subscribeTheme, readTheme, () => 'light' as Theme);

/** The page colour of the current theme, so the sky is the same card as the app. */
function pageColor(el: HTMLElement): number {
  const hex = getComputedStyle(el).getPropertyValue('--bg').trim().replace('#', '');
  return /^[0-9a-f]{6}$/i.test(hex) ? parseInt(hex, 16) : 0xece2c6;
}

function CityScene({ snap, project }: { snap: CitySnap; project: string | null }) {
  const root = useRef<HTMLDivElement>(null);
  const city = useRef<CityHandle | null>(null);
  const navigate = useNavigate();
  const theme = useTheme();
  const sig = snapSignature(snap);
  const applied = useRef(sig);
  const latest = useRef(snap);
  latest.current = snap;

  // One scene per visit and project; later facts are applied in place (below).
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    el.innerHTML = CITY_MARKUP;
    const handle = mountCity(el, latest.current, {
      theme: readTheme,
      bg: () => pageColor(el),
      project,
      openLoop: (d) => navigate(`/loops/${encodeURIComponent(d.h || 'local')}/${encodeURIComponent(d.n)}`),
      openIssue: (id) => navigate(`/issues/${encodeURIComponent(id)}`),
      openPlan: (id) => navigate(`/plans/${encodeURIComponent(id)}`),
    });
    city.current = handle;
    applied.current = snapSignature(latest.current);
    return () => {
      handle.dispose();
      city.current = null;
      el.innerHTML = '';
    };
  }, [project, navigate]);

  useEffect(() => city.current?.setTheme(theme), [theme]);

  // New facts (turns, states, details as they load): redraw in place. The scene refuses while
  // you're inside a building, so keep trying quietly until you step out.
  useEffect(() => {
    if (sig === applied.current) return;
    const tryApply = () => {
      if (city.current?.update(latest.current.loops)) {
        applied.current = sig;
        return true;
      }
      return false;
    };
    if (tryApply()) return;
    const t = setInterval(() => tryApply() && clearInterval(t), 2000);
    return () => clearInterval(t);
  }, [sig]);

  return <div className="lc" ref={root} />;
}

export default function CityView() {
  const { project } = useProjectScope();
  const loops = useLoops();
  const quality = useCityQuality();
  const issues = useCityIssues();
  const docs = useCityDocs();
  const [now] = useState(() => Date.now() / 1000);

  const all = useMemo(() => (loops.data?.loops ?? []) as OverviewLoop[], [loops.data]);
  // Details load for the loops the city can show: not archived, in the project in scope.
  const shown = useMemo(() => all.filter((d) => !d.archived && (!project || (d.project || d.product) === project)), [all, project]);
  const details = useLoopDetails(shown);

  const settled = !loops.isPending && !quality.isPending && !issues.isPending && !docs.isPending;
  const snap = useMemo(
    () =>
      buildSnap({
        loops: all,
        quality: quality.data?.loops ?? [],
        issues: (issues.data?.issues ?? []) as Parameters<typeof buildSnap>[0]['issues'],
        docs: docs.data?.docs ?? [],
        details,
        now,
      }),
    [all, quality.data, issues.data, docs.data, details, now],
  );

  if (loops.isPending || loops.error)
    return <QueryState isPending={loops.isPending} error={loops.error} what="loops" onRetry={() => loops.refetch()} />;
  if (!shown.length)
    return <Empty title="No city yet">Every loop you run becomes a building here. Start one and watch the first floors go up.</Empty>;
  if (!settled) return <QueryState isPending error={null} what="the city" />;
  return <CityScene key={project ?? '*'} snap={snap} project={project} />;
}
