import { Icon } from '../../components/icons';
import { useFavorite } from './api';

/** The favorite star — a real button, one tap to pin/unpin. */
export function Star({ id, on, label, big }: { id: string; on: boolean; label?: boolean; big?: boolean }) {
  const fav = useFavorite();
  return (
    <button
      type="button"
      className={'rl-star' + (on ? ' on' : '') + (big ? ' big' : '') + (label ? ' lbl btn sm' : '')}
      aria-pressed={on}
      aria-label={`${on ? 'Unfavorite' : 'Favorite'} ${id}`}
      title={fav.error ? "Couldn't update — try again" : on ? 'Unfavorite' : 'Favorite this agent'}
      disabled={fav.isPending}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        fav.mutate({ id, favorite: !on });
      }}
    >
      <Icon name="star" size={big ? 20 : 16} fill={on ? 'currentColor' : 'none'} />
      {label && (on ? 'Favorited' : 'Favorite')}
    </button>
  );
}
