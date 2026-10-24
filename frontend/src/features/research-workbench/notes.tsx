import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  ResearchNoteContent,
  WorkbenchEvaluation,
  WorkbenchSpec,
} from "@/client/types.gen";
import { ErrorState, Loading, Panel } from "@/components/common";
import { Pagination } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { stableJson } from "@/lib/data-utils";
import {
  exportNoteRevision,
  noteHistoryOptions,
  noteListOptions,
  noteRevisionOptions,
  readLatestNote,
  saveNoteDraft,
} from "@/lib/note-api";
import {
  clearNoteDraft,
  loadNoteDraft,
  noteFailureIsDefinitive,
  prepareNoteSubmission,
  storeNoteDraft,
  type NoteDraft,
} from "@/lib/note-draft";
import { captureNoteContext, noteRevisionMatches } from "@/lib/note-evidence";
import {
  noteEvidenceNotice,
  noteOperationCurrent,
  noteStatusLabels,
} from "@/lib/note-selection";
import { saveResearchDownload } from "@/lib/research-results";
import type { WorkbenchSelection } from "@/lib/workbench-selection";
import { NoteEvidenceList, NoteReadOnly, NoteTextFields } from "./note-panels";

export function ResearchNotes({
  owner,
  strategyId,
  selection,
  update,
  spec,
  leaf,
  verified,
  sourceProblem,
}: {
  owner: string;
  strategyId: string;
  selection: WorkbenchSelection;
  update: (patch: Partial<WorkbenchSelection>, replace?: boolean) => void;
  spec?: WorkbenchSpec;
  leaf?: WorkbenchEvaluation;
  verified: boolean;
  sourceProblem?: unknown;
}) {
  const queryClient = useQueryClient();
  const list = useQuery(
    noteListOptions(owner, selection.note_query, selection.note_offset),
  );
  const saved = useQuery(noteRevisionOptions(owner, selection));
  const [historyOffset, setHistoryOffset] = useState(0);
  const history = useQuery(
    noteHistoryOptions(owner, selection.note_id, historyOffset),
  );
  const [search, setSearch] = useState(selection.note_query ?? "");
  const [initial] = useState(() => {
    try {
      return {
        draft: loadNoteDraft(owner, strategyId),
        error: null as unknown,
      };
    } catch (error) {
      return { draft: null, error };
    }
  });
  const [draft, setDraft] = useState<NoteDraft | null>(initial.draft);
  const draftSelection: WorkbenchSelection = draft?.note_id
    ? {
        note_id: draft.note_id,
        note_revision: draft.base_revision ?? undefined,
        note_revision_sha256: draft.base_revision_sha256 ?? undefined,
      }
    : {};
  const draftAdmission = useQuery(noteRevisionOptions(owner, draftSelection));
  const draftVisible =
    !draft?.note_id ||
    Boolean(
      draftAdmission.data &&
      !draftAdmission.error &&
      !draftAdmission.isFetching &&
      noteRevisionMatches(draftSelection, draftAdmission.data),
    );
  const [error, setError] = useState<unknown>(initial.error);
  const [unreadable, setUnreadable] = useState(Boolean(initial.error));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const mounted = useRef(true),
    submitting = useRef(false);
  const context = stableJson({ owner, strategyId, selection });
  const latestContext = useRef(context);
  latestContext.current = context;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    setSearch(selection.note_query ?? "");
  }, [selection.note_query]);
  useEffect(() => {
    setHistoryOffset(0);
  }, [selection.note_id]);
  const current = () =>
    noteOperationCurrent(context, latestContext.current, mounted.current);
  const note =
    !saved.error &&
    !saved.isFetching &&
    saved.data &&
    noteRevisionMatches(selection, saved.data)
      ? saved.data
      : undefined;
  const responseProblem =
    saved.data && !note
      ? new Error(
          "The returned note differs from the exact saved revision. It was not displayed or replaced by latest.",
        )
      : null;
  function selectNote(
    value: {
      note_id?: string;
      id: string;
      revision: number;
      revision_sha256: string;
    },
    replace = false,
  ) {
    update(
      {
        tab: "notes",
        note_id: value.note_id ?? value.id,
        note_revision: value.revision,
        note_revision_sha256: value.revision_sha256,
        note_evidence_index: undefined,
        note_arm: undefined,
      },
      replace,
    );
  }
  function persist(value: NoteDraft) {
    storeNoteDraft(owner, strategyId, value);
    setDraft(value);
  }
  function editContent(patch: Partial<ResearchNoteContent>) {
    if (!draft || busy || draft.key) return;
    try {
      persist({ ...draft, content: { ...draft.content, ...patch } });
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }
  function discard() {
    if (busy || draft?.key) return;
    try {
      clearNoteDraft(owner, strategyId);
      setDraft(null);
      setUnreadable(false);
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }
  async function begin() {
    if (!spec || sourceProblem || draft || unreadable || submitting.current)
      return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      const content = await captureNoteContext(selection, spec, leaf, verified);
      if (!current()) return;
      persist({
        version: 1,
        note_id: null,
        base_revision: null,
        base_revision_sha256: null,
        content,
        change_note: "",
        key: null,
        fingerprint: null,
      });
      setNotice(
        "Captured exact evidence. Review the immutable references and enter your hypothesis before saving.",
      );
    } catch (failure) {
      if (current()) setError(failure);
    } finally {
      submitting.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  function edit() {
    if (!note || draft || unreadable || busy) return;
    try {
      persist({
        version: 1,
        note_id: note.note_id,
        base_revision: note.revision,
        base_revision_sha256: note.revision_sha256,
        content: structuredClone(note.content),
        change_note: "",
        key: null,
        fingerprint: null,
      });
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }
  async function save() {
    if (!draft || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    let pending: NoteDraft & { key: string };
    try {
      pending = prepareNoteSubmission(draft);
      persist(pending);
    } catch (failure) {
      submitting.current = false;
      setBusy(false);
      setError(failure);
      return;
    }
    try {
      const result = await saveNoteDraft(pending);
      if (
        (pending.note_id && result.note_id !== pending.note_id) ||
        result.revision !== (pending.base_revision ?? 0) + 1 ||
        !/^[a-f0-9]{64}$/.test(result.revision_sha256)
      )
        throw new Error(
          "The save response did not identify the expected immutable revision. Keep and retry the exact request to reconcile its outcome.",
        );
      let cleared = true;
      try {
        clearNoteDraft(owner, strategyId, pending.key);
      } catch {
        cleared = false;
      }
      await queryClient.invalidateQueries({
        queryKey: ["research-notes", owner],
      });
      await queryClient.invalidateQueries({
        queryKey: ["research-note-history", owner, result.note_id],
      });
      if (!current()) return;
      if (cleared) setDraft(null);
      setNotice(
        cleared
          ? `Saved revision ${result.revision}. Earlier revisions and their evidence remain unchanged.`
          : `Saved revision ${result.revision}, but the exact local retry could not be cleared. Retrying returns that same revision.`,
      );
      selectNote(result);
    } catch (failure) {
      if (noteFailureIsDefinitive(failure)) {
        try {
          const editable = { ...pending, key: null, fingerprint: null };
          storeNoteDraft(owner, strategyId, editable);
          if (current()) setDraft(editable);
        } catch {
          /* Keep original pending retry on storage failure. */
        }
      }
      if (current()) setError(failure);
    } finally {
      submitting.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  async function latest() {
    if (!selection.note_id || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await readLatestNote(selection.note_id);
      if (current()) {
        if (result.note_id !== selection.note_id)
          throw new Error("Returned latest note belongs to another note.");
        selectNote(result);
        setNotice(
          "Opened the latest saved revision explicitly. Any local draft retains its original base and was not rebased.",
        );
      }
    } catch (failure) {
      if (current()) setError(failure);
    } finally {
      submitting.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  async function download() {
    if (!note || submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      const blob = await exportNoteRevision(selection);
      if (current())
        saveResearchDownload(
          blob,
          `research-note-${note.note_id}-r${note.revision}.md`,
        );
    } catch (failure) {
      if (current()) setError(failure);
    } finally {
      submitting.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <div className="space-y-4">
      <Panel title="Evidence research notes">
        <p className="notice">{noteEvidenceNotice}</p>
        <p className="text-sm my-3">
          Notes and immutable revisions are saved to your account. Only an
          unsent draft or exact retry envelope is kept in this browser. Search
          covers titles, tags and written note text.
        </p>
        {sourceProblem != null && (
          <>
            <p className="notice">
              Current source context is unavailable. Your saved notes can still
              be read below; new evidence capture is disabled rather than
              silently using another source.
            </p>
            <ErrorState error={sourceProblem} />
          </>
        )}
        <Button
          onClick={() => void begin()}
          disabled={
            busy ||
            Boolean(draft) ||
            unreadable ||
            !spec ||
            Boolean(sourceProblem)
          }
        >
          Create note from current evidence
        </Button>
        <form
          aria-label="Search research notes"
          className="flex gap-3 my-4"
          onSubmit={(event) => {
            event.preventDefault();
            update({ note_query: search.trim() || undefined, note_offset: 0 });
          }}
        >
          <label className="workbench-field flex-1">
            Search titles, tags and body
            <Input
              value={search}
              maxLength={200}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <Button type="submit" variant="outline">
            Search notes
          </Button>
        </form>
        {list.isPending || list.isFetching ? (
          <Loading text="Loading saved notes…" />
        ) : list.error ? (
          <ErrorState error={list.error} />
        ) : (
          list.data && (
            <>
              <ul aria-label="Saved notes" className="space-y-2">
                {list.data.data.map((item) => (
                  <li key={item.id} className="workbench-rule">
                    <Button
                      variant="outline"
                      onClick={() => selectNote(item)}
                      disabled={busy}
                    >
                      Open note: {item.title}
                    </Button>
                    <p className="text-sm">
                      Revision {item.revision} · {noteStatusLabels[item.status]}{" "}
                      · {item.tags.join(", ") || "No tags"} ·{" "}
                      {item.evidence_count} evidence references
                    </p>
                  </li>
                ))}
              </ul>
              {!list.data.count && <p>No saved notes match this search.</p>}
              <Pagination
                offset={selection.note_offset ?? 0}
                limit={25}
                count={list.data.count}
                busy={list.isFetching}
                onPage={(offset) => update({ note_offset: offset })}
              />
            </>
          )
        )}
      </Panel>
      {selection.note_id && (
        <Panel title="Exact saved note revision">
          {saved.isPending || saved.isFetching ? (
            <Loading text="Reading exact note revision and checking evidence…" />
          ) : saved.error || responseProblem ? (
            <ErrorState error={saved.error || responseProblem} />
          ) : (
            note && (
              <>
                <NoteReadOnly
                  note={note}
                  strategyId={strategyId}
                  selection={selection}
                />
                <div className="flex flex-wrap gap-3 my-4">
                  <Button
                    onClick={edit}
                    disabled={busy || Boolean(draft) || unreadable}
                  >
                    Edit this note revision
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => void download()}
                    disabled={busy}
                  >
                    Download Markdown with references
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => void latest()}
                    disabled={busy}
                  >
                    Open latest saved note explicitly
                  </Button>
                </div>
              </>
            )
          )}
          <section aria-label="Immutable note history">
            <h3 className="font-medium">Immutable revision history</h3>
            {history.isPending || history.isFetching ? (
              <Loading text="Checking note history admission…" />
            ) : history.error ? (
              <ErrorState error={history.error} />
            ) : (
              history.data && (
                <>
                  <ul>
                    {history.data.data.map((item) => (
                      <li key={item.revision_sha256}>
                        <Button
                          variant="ghost"
                          disabled={busy}
                          onClick={() => selectNote(item)}
                        >
                          Open note revision {item.revision}
                        </Button>
                        <span className="text-xs break-all">
                          {item.revision_sha256} ·{" "}
                          {item.change_note || "No change note"}
                        </span>
                      </li>
                    ))}
                  </ul>
                  <Pagination
                    offset={historyOffset}
                    limit={25}
                    count={history.data.count}
                    busy={history.isFetching}
                    onPage={setHistoryOffset}
                  />
                </>
              )
            )}
          </section>
        </Panel>
      )}
      {draft && !draftVisible && (
        <Panel title="Local edit draft retained">
          <p className="notice">
            Copied note content is hidden until its exact base revision passes
            current admission. Your local changes remain stored and are not
            rebased.
          </p>
          {draftAdmission.error ? (
            <ErrorState error={draftAdmission.error} />
          ) : (
            <Loading text="Checking original edit revision admission…" />
          )}
          {draft.key ? (
            <Button disabled={busy} onClick={() => void save()}>
              Retry same note save
            </Button>
          ) : (
            <Button variant="outline" disabled={busy} onClick={discard}>
              Discard local note draft
            </Button>
          )}
        </Panel>
      )}
      {draft && draftVisible && (
        <Panel
          title={
            draft.note_id
              ? `Edit note from immutable revision ${draft.base_revision}`
              : "New evidence note"
          }
        >
          <p className="notice">
            {draft.note_id
              ? `Editing note ${draft.note_id} from revision ${draft.base_revision}. Saving appends a revision only if this base is still current. A conflict never overwrites or automatically rebases your draft.`
              : "New note status starts pending. Writing a positive result does not change its status."}
          </p>
          {draft.key && (
            <p role="status" className="notice">
              The save outcome is pending or uncertain. Only the same stored
              request can be retried; its text, target and evidence are locked.
            </p>
          )}
          <form
            aria-label="Research note editor"
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
          >
            <fieldset disabled={busy || Boolean(draft.key)}>
              <label className="workbench-field">
                Note title
                <Input
                  required
                  maxLength={240}
                  value={draft.content.title}
                  onChange={(event) =>
                    editContent({ title: event.target.value })
                  }
                />
              </label>
              <label className="workbench-field">
                Tags, comma-separated
                <Input
                  key={`${draft.note_id ?? "new"}:${draft.base_revision ?? 0}`}
                  defaultValue={(draft.content.tags ?? []).join(", ")}
                  maxLength={1620}
                  onChange={(event) =>
                    editContent({
                      tags: event.target.value
                        .split(",")
                        .map((value) => value.trim())
                        .filter(Boolean),
                    })
                  }
                />
              </label>
              <label className="workbench-field">
                User-maintained status
                <select
                  value={draft.content.status ?? "pending"}
                  onChange={(event) =>
                    editContent({
                      status: event.target
                        .value as ResearchNoteContent["status"],
                    })
                  }
                >
                  {Object.entries(noteStatusLabels).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <NoteTextFields
                content={draft.content}
                disabled={busy || Boolean(draft.key)}
                onChange={editContent}
              />
              <label className="workbench-field">
                Revision change note
                <textarea
                  rows={2}
                  maxLength={2000}
                  value={draft.change_note}
                  onChange={(event) => {
                    try {
                      persist({ ...draft, change_note: event.target.value });
                    } catch (failure) {
                      setError(failure);
                    }
                  }}
                />
              </label>
            </fieldset>
            <NoteEvidenceList evidence={draft.content.evidence ?? []} />
            <div className="flex flex-wrap gap-3 my-4">
              <Button type="submit" disabled={busy}>
                {busy
                  ? "Saving or checking evidence…"
                  : draft.key
                    ? "Retry same note save"
                    : "Save immutable note revision"}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={busy || Boolean(draft.key)}
                onClick={discard}
              >
                Discard local note draft
              </Button>
            </div>
          </form>
        </Panel>
      )}
      {unreadable && (
        <Button variant="outline" onClick={discard} disabled={busy}>
          Discard unreadable local note draft
        </Button>
      )}
      {notice && (
        <p role="status" className="notice">
          {notice}
        </p>
      )}
      {error != null && <ErrorState error={error} />}
    </div>
  );
}
