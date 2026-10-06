// Retired: the old single-doc Idea Hub page. Every Idea Hub doc is a plan now
// (id `ih.<doc id>`, its body the `index` note). Old `/hub[/:id]` links
// (bookmarks, the `g h` habit) land on the same doc in Plans.
import { Navigate, useParams } from 'react-router-dom';
import { legacyHubRoute } from '@loopyard/api';

export default function HubRedirect() {
  const { id } = useParams();
  return <Navigate to={legacyHubRoute(id)} replace />;
}
