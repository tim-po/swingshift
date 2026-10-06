import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';
import { ISSUE_KINDS, isOpenWork, issueKindLabel, issuePointGoal, type Issue } from '@loopyard/api';
import { Btn, Chip, Empty, friendlyError, friendlyLine, Page, Skeleton } from '../../components/ui';
import { Icon } from '../../components/icons';
import { IssueCard } from './IssueCard';
import { IssueDetail } from './IssueDetail';
import { issueKeys, issues, useIssues } from './queries';
import { useProjectScope } from '../../scope';
import { useLoops } from '../../api';
import { issueInScope, scopeFileProject } from '@loopyard/api';
import { useToast } from './toast';
import './issues.css';
import { Intro } from '../../components/Intro';

export default function IssuesView() {
  const { id } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { toast, node: toastNode } = useToast();
  const { data, error, isPending, refetch } = useIssues();
  const [viewAll, setViewAll] = useState(false);
  const [fileOpen, setFileOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [kind, setKind] = useState<string>('bug');

  const { project: scope } = useProjectScope();
  const loopsQ = useLoops();
  const everything = data?.issues ?? [];
  const all = useMemo(() => everything.filter((x) => issueInScope(x, scope, loopsQ.data?.loops ?? [])), [everything, scope, loopsQ.data]);
  const openN = useMemo(() => all.filter(isOpenWork).length, [all]);
  const closedN = all.length - openN;
  const shown = useMemo(() => (viewAll ? all : all.filter(isOpenWork)), [all, viewAll]);
  const refresh = () => qc.invalidateQueries({ queryKey: issueKeys.list });

  const triage = useMutation({
    mutationFn: (t: { id: string; status: string; note?: string }) => issues.triage(t),
    onSuccess: refresh,
    onError: (e) => toast(friendlyLine(e, 'update that issue'), true),
  });
  const file = useMutation({
    mutationFn: () => issues.file({ title: title.trim(), kind, project: scopeFileProject(scope) }),
    onSuccess: () => { setFileOpen(false); setTitle(''); refresh(); toast('Issue filed.'); },
    onError: (e) => toast(friendlyLine(e, 'file the issue'), true),
  });

  const submitFile = () => {
    if (!title.trim()) return toast('Give the issue a one-line title first.', true);
    file.mutate();
  };

  // Fix with a loop: seed the real composer from the issue and move it to
  // `resolving` (reversible via Stop). Nothing starts until the user launches
  // the loop from the composer.
  const onAction = (it: Issue, status: string) => {
    if (status !== 'point') return triage.mutate({ id: it.id, status });
    triage.mutate({ id: it.id, status: 'resolving', note: 'pointed a loop (composer)' }, { onError: () => undefined });
    navigate('/newloop', { state: { goal: issuePointGoal(it), project: it.project && it.project !== 'Unattributed' ? it.project : '' } });
  };

  if (id)
    return (
      <div className="page is-detail">
        <IssueDetail id={id} />
        {toastNode}
      </div>
    );

  let list: React.ReactNode;
  if (isPending) list = <div className="sk-cards">{[0, 1, 2].map((i) => <Skeleton key={i} block={56} lines={0} />)}</div>;
  else if (error || (!shown.length && data?.error)) {
    const fe = friendlyError(error, 'issues');
    list = <Empty tone="err" title={fe.title}><p>{fe.detail}</p><Btn variant="primary" onClick={() => refetch()}>Try again</Btn></Empty>;
  } else if (!shown.length)
    list = (
      <Empty title={viewAll ? 'No issues yet' : 'Nothing open'}>
        <p>
          {viewAll
            ? 'When a loop runs into a problem it shows up here. You can file your own too.'
            : 'All clear. New problems from your loops will show up here.'}
        </p>
        {!viewAll && closedN > 0 && <Btn onClick={() => setViewAll(true)}>Show {closedN} closed</Btn>}
      </Empty>
    );
  else
    list = (
      <div className="rows is-list">
        {shown.map((it) => <IssueCard key={it.id} it={it} busy={triage.isPending && triage.variables?.id === it.id} onAction={onAction} />)}
      </div>
    );

  return (
    <Page
      title="Issues"
      sub={isPending ? ' ' : openN ? `${openN} open` : 'Nothing open'}
      actions={
        <Btn variant="primary" onClick={() => setFileOpen((o) => !o)} aria-expanded={fileOpen}>
          <Icon name="plus" /> File an issue
        </Btn>
      }
    >
      <Intro id="issues">
        Problems your loops ran into, plus anything you file yourself. Pick one and choose <b>Fix with a loop</b> to hand it to a new loop.
      </Intro>
      {fileOpen && (
        <form className="is-file" onSubmit={(e) => { e.preventDefault(); submitFile(); }}>
          <div className="field is-file-title">
            <label htmlFor="is-file-title">What's wrong?</label>
            <input id="is-file-title" className="input" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} placeholder="One line — e.g. Login page shows a blank screen" />
          </div>
          <div className="field is-file-kind">
            <label htmlFor="is-file-kind">Type</label>
            <select id="is-file-kind" className="select" value={kind} onChange={(e) => setKind(e.target.value)}>
              {ISSUE_KINDS.map((k) => <option key={k} value={k}>{issueKindLabel(k)}</option>)}
            </select>
          </div>
          <div className="is-filebtns">
            <button type="submit" className="btn primary" disabled={file.isPending}>{file.isPending ? 'Filing…' : 'File issue'}</button>
            <Btn className="btn quiet" onClick={() => setFileOpen(false)}>Cancel</Btn>
          </div>
        </form>
      )}
      {(closedN > 0 || viewAll) && !isPending && (
        <div className="is-filter" role="group" aria-label="Which issues to show">
          <Chip on={!viewAll} onClick={() => setViewAll(false)}>Open · {openN}</Chip>
          <Chip on={viewAll} onClick={() => setViewAll(true)}>All · {all.length}</Chip>
        </div>
      )}
      {list}
      {toastNode}
    </Page>
  );
}
