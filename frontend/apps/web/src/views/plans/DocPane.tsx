// One note of a plan: rendered markdown to read, Edit to change it (with a live
// preview beside the editor). Saving goes through the human write path
// (ws.saveDoc — the only writer of note content).
import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { planStatusLabel, WS_DOC_STATUSES, type WsDoc, type WsStatus } from '@loopyard/api';
import { Btn } from '../../components/ui';
import { Icon } from '../../components/icons';
import { Markdown } from './markdown';
import { ws, wsKeys } from './queries';
import { errText } from '../hub/flash';

export function DocPane({
  oid,
  doc,
  flash,
  showTitle,
  titleEditable,
  showStatus,
  startEditing = false,
}: {
  oid: string;
  doc: WsDoc;
  flash(t: string, err?: boolean): void;
  /** Show the note's own title (a plan with several notes). */
  showTitle: boolean;
  /** The title can be edited (it isn't just the plan's name repeated). */
  titleEditable: boolean;
  /** Offer a per-note status (only meaningful with several notes). */
  showStatus: boolean;
  startEditing?: boolean;
}) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(startEditing);
  const [title, setTitle] = useState(doc.title);
  const [body, setBody] = useState(doc.body);

  // Adopt server state (an accepted suggestion, an assistant edit) unless mid-edit.
  useEffect(() => {
    if (!editing) {
      setTitle(doc.title);
      setBody(doc.body);
    }
  }, [doc.title, doc.body, editing]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: wsKeys.doc(oid, doc.slug) });
    qc.invalidateQueries({ queryKey: wsKeys.objective(oid) });
    qc.invalidateQueries({ queryKey: wsKeys.objectives });
  };
  const save = useMutation({
    mutationFn: () => ws.saveDoc({ oid, slug: doc.slug, title, body, status: doc.status }),
    onSuccess: () => { setEditing(false); invalidate(); flash('Saved.'); },
    onError: (e) => flash(errText(e), true),
  });
  const setStatus = useMutation({
    mutationFn: (s: WsStatus) => ws.setDocStatus(oid, doc.slug, s),
    onSuccess: () => invalidate(),
    onError: (e) => flash(errText(e), true),
  });

  return (
    <article className="pl-doc" aria-label={doc.title || doc.slug}>
      <header className="pl-dochead">
        {editing && titleEditable ? (
          <input className="input pl-doctitle-in" value={title} onChange={(e) => setTitle(e.target.value)} aria-label="Note title" />
        ) : showTitle ? (
          <h2 className="h3 pl-doctitle">{doc.title || doc.slug}</h2>
        ) : (
          <span className="pl-spacer" />
        )}
        <div className="pl-docbtns">
          {showStatus && !editing && (
            <select
              className="select pl-docstatus"
              value={doc.status}
              aria-label="Note status"
              disabled={setStatus.isPending}
              onChange={(e) => setStatus.mutate(e.target.value as WsStatus)}
            >
              {WS_DOC_STATUSES.map((s) => <option key={s} value={s}>{planStatusLabel(s)}</option>)}
            </select>
          )}
          {editing ? (
            <>
              <Btn disabled={save.isPending} onClick={() => save.mutate()}>{save.isPending ? 'Saving…' : 'Save'}</Btn>
              <button type="button" className="btn quiet" disabled={save.isPending} onClick={() => { setEditing(false); setTitle(doc.title); setBody(doc.body); }}>Cancel</button>
            </>
          ) : (
            <button type="button" className="btn quiet sm" onClick={() => setEditing(true)}><Icon name="edit" /> Edit</button>
          )}
        </div>
      </header>
      {editing ? (
        <div className="pl-editsplit">
          <textarea
            className="textarea pl-editor"
            value={body}
            onChange={(e) => setBody(e.target.value)}
            spellCheck
            aria-label="Plan text (markdown)"
            placeholder="What should happen? Goals, steps, what done looks like…"
            autoFocus={startEditing}
          />
          <div className="pl-preview" aria-label="Preview">
            <div className="pl-previewtag">Preview</div>
            <Markdown src={body} />
          </div>
        </div>
      ) : (
        <Markdown src={doc.body} />
      )}
    </article>
  );
}
