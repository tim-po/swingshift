import { NavLink } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { client } from '../api';
import { config } from '../config';
import { Icon } from '../components/icons';

interface SavedAgent { id: string; note?: string; favorite?: boolean }

export const useRegistryAgents = () =>
  useQuery({
    queryKey: ['shell', 'registry'],
    queryFn: () => client.get<{ agents?: SavedAgent[] }>('/api/loops/registry'),
    refetchInterval: config.pollMs * 6,
    select: (r) => r.agents ?? [],
  });

/** ★ Favorites: starred role templates pinned in the sidebar. */
export function FavShelf({ onNavigate }: { onNavigate(): void }) {
  const { data: agents = [] } = useRegistryAgents();
  const favs = agents.filter((a) => a.favorite);
  // Appears once you've starred something — the empty shelf taught nothing.
  if (!favs.length) return null;
  return (
    <div className="sh-favs">
      <div className="sh-navsec">Favorites</div>
      {favs.length ? (
        <div className="sh-favshelf">
          {favs.map((a) => (
            <NavLink key={a.id} to={`/library/agents/${encodeURIComponent(a.id)}`} className="sh-favrow" title={a.note || a.id} onClick={onNavigate}>
              <Icon name="star" size={14} className="sh-fico" />
              <span className="sh-fid">{a.id}</span>
            </NavLink>
          ))}
        </div>
      ) : (
        <div className="sh-favempty">★ star an agent to pin it here</div>
      )}
    </div>
  );
}
