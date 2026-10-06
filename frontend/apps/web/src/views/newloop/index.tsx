// ＋Loop — one calm, goal-first composer (default), plus the other creator doors
// reached by query:
//   ?edit=<name>                       the loop editor on a saved loop
//   ?editor=new                        the loop editor, blank (compose a crew by hand)
//   ?clone=<registry id>               clone → then the editor on the clone
//   ?brief=<name>&phase=brief|debrief  the briefing chat seeded by a saved loop
//   ?origin=<id>&runtime=<rt>          composer with origin / creator runtime preselected
//   router state {goal, project}       composer prefilled (Issues' "Point a loop")
import { useNavigate, useSearchParams } from 'react-router-dom';
import { BriefChat } from '../../features/editor/BriefChat';
import { LoopEditor } from '../../features/editor/LoopEditor';
import { loopPath } from '../../features/editor/queries';
import { CloneForm } from './CloneForm';
import { Composer } from './Composer';
import './newloop.css';

export default function NewLoopView() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const edit = params.get('edit');
  const clone = params.get('clone');
  const brief = params.get('brief');
  if (edit) return <LoopEditor key={'e:' + edit} name={edit} />;
  if (params.get('editor') != null) return <LoopEditor key="new" />;
  if (clone) return <CloneForm key={'c:' + clone} from={clone} />;
  if (brief) {
    const phase = params.get('phase') === 'debrief' ? 'debrief' : 'brief';
    return <BriefChat key={`b:${brief}:${phase}`} name={brief} phase={phase} onClose={() => navigate(loopPath(brief))} />;
  }
  return <Composer />;
}
