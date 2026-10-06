// /library            → Agents tab
// /library/agents     → Agents tab
// /library/loops      → Loops tab
// /library/agents/:id → one agent (quality, identity, versions, usage)
// Old /roles and /roles/:id links redirect here (see src/views.ts).
import { Navigate, useParams } from 'react-router-dom';
import { AgentDetail } from './AgentDetail';
import { Library } from './Library';
import './library.css';

export default function LibraryView() {
  const { tab, id } = useParams();
  if (tab === 'agents' && id) return <AgentDetail key={id} id={id} />;
  if (!tab || tab === 'agents') return <Library tab="agents" />;
  if (tab === 'loops') return <Library tab="loops" />;
  // /library/<bare agent id> (hand-typed or an old deep link) → its agent page
  return <Navigate to={`/library/agents/${encodeURIComponent(tab)}`} replace />;
}
