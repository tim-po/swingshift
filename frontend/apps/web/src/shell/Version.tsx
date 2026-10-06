import { useQuery } from '@tanstack/react-query';
import { client } from '../api';
import { config } from '../config';

/** GET /api/version (B-3) — the running engine's stamp. */
export interface EngineVersion {
  version: string;
  gitSha?: string | null;
  target?: string | null;
  protocol?: number;
  stateVersion?: number;
}

/** "UI v A ≠ engine v B — reload" when this SPA's baked stamp differs from the
 *  engine's (a tab cached across an upgrade). An unstamped dev build never nags. */
export function skewMessage(ui: string, engine: string | undefined): string | null {
  if (!ui || !engine || ui === engine) return null;
  return `UI v${ui} ≠ engine v${engine} — reload`;
}

export function SkewBanner({ ui, engine, onReload = () => window.location.reload() }: {
  ui: string;
  engine: string | undefined;
  onReload?: () => void;
}) {
  const msg = skewMessage(ui, engine);
  if (!msg) return null;
  return (
    <div className="sh-skew" role="status">
      <span>{msg}</span>
      <button className="sh-skewbtn" onClick={onReload}>Reload</button>
    </div>
  );
}

/** Sidebar footer line: the UI's own stamp, or "dev" for an unstamped build. */
export function VersionFoot({ ui }: { ui: string }) {
  return <div className="sh-version" title="Loopyard version">{ui ? `v${ui}` : 'dev'}</div>;
}

export const useEngineVersion = () =>
  useQuery({
    queryKey: ['version'],
    queryFn: () => client.get<EngineVersion>('/api/version'),
    refetchInterval: config.navPollMs,
    retry: false,
  });

export function VersionSkew() {
  return <SkewBanner ui={config.version} engine={useEngineVersion().data?.version} />;
}
