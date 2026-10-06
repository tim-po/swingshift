// The live SCHEMATIC beside the draft editor: the loop's shape as the server's
// authoring.schematic sees it (the data behind loop_schematic) — who runs, in what
// order, parallel groups side by side. Redraws whenever the draft check settles.
import type { Schematic, SchematicStep } from './draft';

const ROLE_SHORT: Record<string, string> = { manager: 'manager', worker: 'worker', input_provider: 'reviewer' };

function Node({ s }: { s: SchematicStep | undefined; id: string }) {
  if (!s) return null;
  const role = s.type === 'loop' ? 'sub-loop' : ROLE_SHORT[s.role ?? ''] ?? s.role ?? 'agent';
  return (
    <div className={`nl-sn nl-sn-${s.type === 'loop' ? 'loop' : s.role ?? 'agent'}`} data-step={s.id}>
      <span className="nl-snid">{s.id}</span>
      <span className="nl-snrole">{role}</span>
    </div>
  );
}

export function DraftSchematic({ schematic, pending }: { schematic: Schematic | null | undefined; pending?: boolean }) {
  const steps = schematic?.steps ?? [];
  const byId = new Map(steps.map((s) => [s.id, s]));
  const order = schematic?.stepOrder ?? [];
  const turnLimit = schematic?.derived?.turnLimit;

  return (
    <aside className="nl-schem" aria-label="Loop schematic" aria-busy={pending || undefined}>
      <div className="nl-schemhd">
        <b>Shape of your loop</b>
        {pending && <span className="nl-schemup">updating…</span>}
      </div>
      {!schematic ? (
        <div className="nl-schemempty">The diagram appears once the draft is complete enough to run.</div>
      ) : (
        <ol className="nl-flow">
          {order.map((o, i) => (
            <li key={i} className="nl-flowrow">
              {'parallel' in o ? (
                <div className="nl-par" title="These run side by side">
                  {o.parallel.map((id) => <Node key={id} id={id} s={byId.get(id)} />)}
                </div>
              ) : (
                <Node id={o.step} s={byId.get(o.step)} />
              )}
            </li>
          ))}
        </ol>
      )}
      {schematic && (
        <div className="nl-schemnote">
          Each round runs top to bottom{typeof turnLimit === 'number' ? ` · up to ${turnLimit} turns` : ''}. The manager decides when it's done.
        </div>
      )}
    </aside>
  );
}
