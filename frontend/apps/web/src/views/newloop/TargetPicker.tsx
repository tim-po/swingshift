// Attach a plan or an issue (hub docs + open issues), scoped to the active project.
// The picked target pre-fills the goal (editable).
import { useMemo } from 'react';
import { PROJECT_UNATTR, targetCatalog, type CreatorTarget } from '@loopyard/api';
import { Btn } from '../../components/ui';
import { useLoops } from '../../api';
import { CreatorModal } from '../../features/editor/Modal';
import { useCreatorDocs, useCreatorIssues } from '../../features/editor/queries';

export function TargetPicker({ scope, onPick, onClose }: { scope: string; onPick(t: CreatorTarget): void; onClose(): void }) {
  const docs = useCreatorDocs();
  const issues = useCreatorIssues();
  const loops = useLoops();
  const cat = useMemo(() => {
    const byLoop = new Map((loops.data?.loops ?? []).map((l) => [l.name, l.project || l.product || null]));
    return targetCatalog(docs.data?.docs ?? [], issues.data?.issues ?? [], scope, PROJECT_UNATTR, (n) => byLoop.get(n));
  }, [docs.data, issues.data, loops.data, scope]);
  const loading = docs.isPending || issues.isPending;

  const groups: [CreatorTarget['kind'], string][] = [
    ['doc', 'Plans'],
    ['issue', 'Issues'],
  ];
  return (
    <CreatorModal title="Start from a plan or issue" onClose={onClose}>
      {loading ? (
        <p className="nl-sub">Loading plans and issues…</p>
      ) : !cat.length ? (
        <p className="nl-sub">No open plans or issues yet. Write a plan in Plans or file one in Issues — or just describe what you want done and press Start.</p>
      ) : (
        <>
          <p className="nl-sub">Its text fills in the goal — you can edit it before starting.</p>
          <div className="nl-tlist">
            {groups.map(([k, label]) => {
              const items = cat.filter((x) => x.kind === k);
              if (!items.length) return null;
              return (
                <section key={k}>
                  <h3 className="nl-tgrp">{label}</h3>
                  {items.map((x) => (
                    <button key={x.kind + x.id} type="button" className="nl-titem" onClick={() => onPick(x)} title={x.text}>
                      {x.text}
                    </button>
                  ))}
                </section>
              );
            })}
          </div>
        </>
      )}
      <div className="nl-actions">
        <Btn onClick={onClose}>{cat.length ? 'Cancel' : 'Close'}</Btn>
      </div>
    </CreatorModal>
  );
}
