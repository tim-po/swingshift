// /plans/sweep-inbox — plan ideas a helper found on a machine, waiting for you.
// Accept turns one into a real plan; nothing is created until you click.
import { useNavigate } from 'react-router-dom';
import { WS_INBOX_ID } from '@loopyard/api';
import { Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { useSuggestions } from './queries';
import { SuggestionCard } from './Suggestions';

export function SuggestedPlans({ flash }: { flash(t: string, err?: boolean): void }) {
  const navigate = useNavigate();
  const sugs = useSuggestions(WS_INBOX_ID);
  const pending = (sugs.data?.suggestions ?? []).filter((s) => s.state === 'pending');
  return (
    <div className="page pl-page pl-inbox">
      <button type="button" className="btn quiet sm pl-back" onClick={() => navigate('/plans')}>
        <Icon name="chevronLeft" /> All plans
      </button>
      <header className="pagehead">
        <div>
          <h1 className="h1">Suggested plans</h1>
          <div className="sub">Ideas found in your machines’ files. Accept one to make it a plan.</div>
        </div>
      </header>
      {sugs.isPending ? (
        <Skeleton lines={3} />
      ) : pending.length ? (
        <div className="pl-sugs">{pending.map((s) => <SuggestionCard key={s.id} oid={WS_INBOX_ID} sug={s} flash={flash} />)}</div>
      ) : (
        <p className="muted pl-quiet">Nothing to review right now.</p>
      )}
    </div>
  );
}
