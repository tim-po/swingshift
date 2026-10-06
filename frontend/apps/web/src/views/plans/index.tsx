// Plans — replaces the old Hub + Chat. /plans lists the plans (grouped Idea →
// Planned → Running → Done, or as a board); /plans/:oid[/:slug] is one plan with "Run as loop", the loops
// pointed at it, and "Ask about this plan" in a collapsible side panel.
import { useParams } from 'react-router-dom';
import { isInbox, WS_INBOX_ID } from '@loopyard/api';
import { useFlash } from '../hub/flash';
import { PlansHome } from './PlansHome';
import { PlanDetail, PlanDetailState } from './PlanDetail';
import { useObjective } from './queries';
import { SuggestedPlans } from './SuggestedPlans';
import './plans.css';

function PlanRoute({ oid, slug, flash }: { oid: string; slug?: string; flash(t: string, err?: boolean): void }) {
  const q = useObjective(oid);
  const plan = q.data?.objective;
  if (!plan) return <PlanDetailState isPending={q.isPending} error={q.error} retry={() => q.refetch()} />;
  if (isInbox(plan)) return <SuggestedPlans flash={flash} />;
  return <PlanDetail plan={plan} slug={slug} flash={flash} />;
}

export default function PlansView() {
  const { oid, slug } = useParams();
  const { flash, node: toast } = useFlash();
  return (
    <>
      {!oid ? (
        <PlansHome flash={flash} />
      ) : oid === WS_INBOX_ID ? (
        <SuggestedPlans flash={flash} />
      ) : (
        <PlanRoute key={oid} oid={oid} slug={slug} flash={flash} />
      )}
      {toast}
    </>
  );
}
