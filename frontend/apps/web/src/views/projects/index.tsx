import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { projectDispositionLine, projectsGatheredCount, loopsForProject, projectsOf, unattributedOf, type LoopSummary, type Project } from '@loopyard/api';
import { Btn, Empty, friendlyLine, Page, QueryState } from '../../components/ui';
import { Icon } from '../../components/icons';
import { Intro } from '../../components/Intro';
import { Modal } from '../../shell/Modal';
import { useShowAll } from '../../shell/disclosure';
import { useLoops } from '../../api';
import { projects as projectsClient, useDispositions, useProjects } from '../../scope/queries';
import { useProjectScope } from '../../scope';
import { MoreMenu } from '../issues/MoreMenu';
import { UnattributedSection } from './Unattributed';
import './projects.css';

type Flash = { ok: boolean; text: string } | null;
const SHOW_LOOPS = 3;

export default function ProjectsView() {
  const { data, error, isPending, refetch } = useProjects();
  const loops = useLoops().data?.loops ?? [];
  const qc = useQueryClient();
  const [showAll] = useShowAll();
  const [adding, setAdding] = useState(false);
  const [flash, setFlash] = useState<Flash>(null);
  const list = projectsOf(data);
  const g = data?.gathered;
  const gathered = projectsGatheredCount(g);
  // Attribution diagnostics: the catalog vs what loops are actually bound to.
  const live = loops.filter((l) => !l.archived);
  const unattributed = live.filter((l) => !(l.project || l.product)).length;
  const catalogIds = new Set(list.map((p) => p.id));
  const uncatalogued = [...live.reduce((m, l) => {
    const pid = l.project || l.product;
    if (pid && !catalogIds.has(pid)) m.set(pid, (m.get(pid) ?? 0) + 1);
    return m;
  }, new Map<string, number>())];
  const hasDiagnostics = !!g || unattributed > 0 || uncatalogued.length > 0;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['projects'] });
    qc.invalidateQueries({ queryKey: ['counts'] });
    qc.invalidateQueries({ queryKey: ['loops'] });
  };

  let body: React.ReactNode = <QueryState isPending={isPending} error={error} what="projects" onRetry={() => refetch()} />;
  if (!isPending && !error)
    body = list.length ? (
      <div className="rows pj-list">
        {list.map((p) => (
          <ProjectRow key={p.id} p={p} bound={loopsForProject(live, p.id)} onFlash={setFlash} onChanged={refresh} />
        ))}
      </div>
    ) : data?.error ? (
      <Empty tone="err" title="Couldn't load projects"><p>Something went wrong reading your projects. This is usually temporary.</p><Btn variant="primary" onClick={() => refetch()}>Try again</Btn></Empty>
    ) : (
      <Empty title="No projects yet">
        <p>A project is a codebase your loops work on. Projects appear here on their own once loops run — or add one now.</p>
        <Btn variant="primary" onClick={() => setAdding(true)}>Add project</Btn>
      </Empty>
    );

  return (
    <Page
      title="Projects"
      sub={isPending ? ' ' : `${list.length} project${list.length === 1 ? '' : 's'}`}
      actions={
        <Btn variant="primary" onClick={() => setAdding(true)}>
          <Icon name="plus" /> Add project
        </Btn>
      }
    >
      <Intro id="projects">
        Projects are the codebases your loops work on. A loop belongs to a project only when you link it — in the loop editor, or with Accept / Assign under Unattributed below. Add a project by hand with a git URL or folder path.
      </Intro>
      {flash && (
        <div className={'pj-flash' + (flash.ok ? '' : ' err')} role="status">
          <span>{flash.text}</span>
          <button type="button" className="btn quiet sm" onClick={() => setFlash(null)} aria-label="Dismiss">
            <Icon name="x" size={14} />
          </button>
        </div>
      )}
      {body}
      {!isPending && !error && <UnattributedSection loops={unattributedOf(data)} projects={list} onFlash={setFlash} onChanged={refresh} />}
      {hasDiagnostics && !isPending && (
        <details className="pj-diag" open={showAll || undefined}>
          <summary>How loops map to projects</summary>
          <ul>
            {g && (
              <li>
                {gathered ? `${gathered} project${gathered === 1 ? ' was' : 's were'} added from loops linked to them` : 'No new projects added'} from{' '}
                {g.loops_scanned ?? 0} loops.
              </li>
            )}
            {unattributed > 0 && (
              <li title="These loops only appear under Unattributed in the project switcher">
                {unattributed} loop{unattributed === 1 ? ' isn’t' : 's aren’t'} linked to any project.
              </li>
            )}
            {uncatalogued.length > 0 && (
              <li>
                {uncatalogued.length} project id{uncatalogued.length === 1 ? ' is' : 's are'} used by loops but not listed here:{' '}
                <span className="mono">{uncatalogued.map(([id, n]) => `${id} (${n})`).join(', ')}</span>
              </li>
            )}
          </ul>
        </details>
      )}
      {adding && (
        <AddProjectDialog
          onClose={() => setAdding(false)}
          onAdded={(text) => {
            setAdding(false);
            setFlash({ ok: true, text });
            refresh();
          }}
        />
      )}
    </Page>
  );
}

function AddProjectDialog({ onClose, onAdded }: { onClose(): void; onAdded(text: string): void }) {
  const [source, setSource] = useState('');
  const [err, setErr] = useState('');
  const add = useMutation({
    mutationFn: projectsClient.add,
    onSuccess: (res) => {
      const name = res.derived?.name || res.id || 'Project';
      onAdded(`Added ${name}.` + (res.warnings?.length ? ' ' + res.warnings.join('; ') : ''));
    },
    onError: (e: Error) => setErr(friendlyLine(e, 'add that project')),
  });
  const submit = () => {
    const s = source.trim();
    if (!s) return setErr('Paste a git URL or a folder path.');
    setErr('');
    add.mutate(s);
  };
  return (
    <Modal label="Add a project" onClose={onClose}>
      <form className="pj-add" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <h2 className="h2">Add a project</h2>
        <div className="field">
          <label htmlFor="pj-source">Git URL or folder path</label>
          <input
            id="pj-source"
            className="input mono"
            autoFocus
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder="git@github.com:me/my-app.git"
            spellCheck={false}
            autoCapitalize="off"
          />
          <span className="field-hint">A folder works too, e.g. <span className="mono">~/projects/my-app</span>. The name is filled in for you.</span>
        </div>
        {err && <p className="pj-err" role="alert">{err}</p>}
        <div className="pj-addbtns">
          <Btn className="btn quiet" onClick={onClose}>Cancel</Btn>
          <Btn type="submit" variant="primary" disabled={add.isPending}>{add.isPending ? 'Adding…' : 'Add project'}</Btn>
        </div>
      </form>
    </Modal>
  );
}

function ProjectRow({ p, bound, onFlash, onChanged }: { p: Project; bound: LoopSummary[]; onFlash(f: Flash): void; onChanged(): void }) {
  const navigate = useNavigate();
  const { setProject } = useProjectScope();
  const [confirming, setConfirming] = useState(false);
  const disp = useDispositions(p.id);
  const roll = projectDispositionLine(disp.data && !disp.data.error ? disp.data : undefined);
  const del = useMutation({
    mutationFn: () => projectsClient.remove(p.id),
    onSuccess: () => {
      onFlash({ ok: true, text: `Removed ${p.name || p.id}.` });
      onChanged();
    },
    onError: (e: Error) => onFlash({ ok: false, text: friendlyLine(e, 'remove that project') }),
    onSettled: () => setConfirming(false),
  });
  const openLoops = () => {
    setProject(p.id);
    navigate('/loops');
  };
  const git = p.gitRemote ? `${p.gitRemote}${p.gitBranch ? ' · ' + p.gitBranch : ''}` : p.repoDir || '';
  const name = p.name || p.id;
  const extra = bound.length - SHOW_LOOPS;

  return (
    <article className="pj-row">
      <div className="pj-top">
        <div className="pj-title">
          <span className="h3 pj-name" title={name}>{name}</span>
          {p.name && p.name !== p.id && <span className="mono pj-id" title="Project id">{p.id}</span>}
        </div>
        <div className="pj-btns">
          <button type="button" className="btn sm" onClick={openLoops} title="Show only this project's loops">Open loops</button>
          <MoreMenu label={`More actions for ${name}`} items={[{ label: 'Remove project…', danger: true, onSelect: () => setConfirming(true) }]} />
        </div>
      </div>
      {git && <div className="mono pj-git" title={git}>{git}</div>}
      {p.note && <div className="pj-note">{p.note}</div>}
      <div className="pj-loops">
        {bound.length ? (
          <>
            <span>{bound.length} loop{bound.length === 1 ? '' : 's'}:</span>
            {bound.slice(0, SHOW_LOOPS).map((l, i) => (
              <span key={l.host + l.name}>
                <Link to={`/loops/${encodeURIComponent(l.host)}/${encodeURIComponent(l.name)}`} title={`Open loop ${l.name}`}>{l.name}</Link>
                {i < Math.min(bound.length, SHOW_LOOPS) - 1 ? ',' : ''}
              </span>
            ))}
            {extra > 0 && (
              <button type="button" className="pj-morelink" onClick={openLoops}>and {extra} more</button>
            )}
          </>
        ) : (
          <span>No loops yet</span>
        )}
      </div>
      {roll && <div className="pj-roll" title="How you rated this project's finished loops">Your ratings: {roll}</div>}
      {confirming && (
        <div className="pj-confirm" role="alertdialog" aria-label={`Remove ${name}?`}>
          <span>Remove <b>{name}</b> from your projects? Its loops and files aren't touched.</span>
          <div className="pj-confirmbtns">
            <Btn className="btn quiet sm" onClick={() => setConfirming(false)}>Cancel</Btn>
            <Btn className="btn danger sm" onClick={() => del.mutate()} disabled={del.isPending}>{del.isPending ? 'Removing…' : 'Remove'}</Btn>
          </div>
        </div>
      )}
    </article>
  );
}
