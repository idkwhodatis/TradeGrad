import type {
  ResearchNoteContent,
  ResearchNoteRevision,
} from "@/client/types.gen";
import { JsonDetails, Metadata } from "@/components/data-common";
import type { WorkbenchSelection } from "@/lib/workbench-selection";
import { noteTextFields, type NoteEvidence } from "@/lib/note-evidence";
import {
  noteArmResultHref,
  noteHistoricalHref,
  noteEvidenceNotice,
  noteStatusLabels,
  safeNoteEvidenceHref,
} from "@/lib/note-selection";

export function NoteEvidenceList({
  evidence,
  resolutions,
  note,
  strategyId,
}: {
  evidence: NoteEvidence[];
  resolutions?: ResearchNoteRevision["evidence"];
  note?: ResearchNoteRevision;
  strategyId?: string;
}) {
  return (
    <section aria-label="Typed immutable evidence" className="space-y-4">
      <h3 className="font-medium">Typed immutable evidence</h3>
      {!evidence.length && (
        <p className="notice">
          No evidence attached. The note is a human hypothesis, not a recorded
          result.
        </p>
      )}
      {evidence.map((ref, index) => {
        const matches = resolutions?.filter((item) => item.index === index);
        const resolved = matches?.length === 1 ? matches[0] : undefined;
        const href =
          resolved?.state === "available"
            ? safeNoteEvidenceHref(resolved.href)
            : null;
        return (
          <article
            key={index}
            className="workbench-rule"
            data-testid={`note-evidence-${index}`}
          >
            <h4 className="font-medium">
              {index + 1}. {ref.label || ref.kind} · {ref.kind}
            </h4>
            <p className="text-sm">
              {resolutions
                ? `Evidence state: ${resolved?.state ?? "unavailable"}`
                : "Awaiting server admission on save. Pins below are the captured request, not proof of availability."}
            </p>
            {resolved?.reason && <p className="notice">{resolved.reason}</p>}
            {resolutions && !resolved && (
              <p role="alert" className="notice error">
                The server did not resolve this exact reference uniquely. No
                replacement link is offered.
              </p>
            )}
            {href ? (
              <a className="text-link" href={href}>
                Open exact {ref.kind} evidence
              </a>
            ) : (
              resolutions && (
                <p className="text-sm">
                  Exact evidence link unavailable. No newer source, attempt or
                  result was substituted.
                </p>
              )
            )}
            {ref.kind === "controlled" && (
              <ul aria-label="Saved controlled arms" className="space-y-2 my-3">
                {ref.arms.map((arm) => (
                  <li key={arm.ordinal} className="text-sm">
                    Arm {arm.ordinal + 1}: {arm.status} · generation{" "}
                    {arm.generation}
                    <br />
                    Run {arm.run_id} · stopped physical attempt{" "}
                    {arm.execution_attempt_id}
                    <br />
                    {note && strategyId && (
                      <>
                        <a
                          className="text-link"
                          href={
                            noteHistoricalHref(
                              strategyId,
                              note,
                              index,
                              arm.ordinal,
                            ) ?? undefined
                          }
                        >
                          Inspect saved arm {arm.ordinal + 1} from note revision{" "}
                          {note.revision}
                        </a>
                        <br />
                      </>
                    )}
                    {arm.result
                      ? `Evaluation ${arm.result.evaluation_id} · result SHA-256 ${arm.result.result_manifest_sha256}`
                      : "No successful result. This failed or stopped arm is not zero profit and is not a later retry."}
                  </li>
                ))}
              </ul>
            )}
            <JsonDetails
              title={`Immutable ${ref.kind} reference ${index + 1}`}
              value={ref}
            />
            {resolved?.bindings?.length ? (
              <JsonDetails
                title={`Admitted source provenance ${index + 1}`}
                value={resolved.bindings}
              />
            ) : null}
          </article>
        );
      })}
    </section>
  );
}
export function NoteReadOnly({
  note,
  strategyId,
  selection,
}: {
  note: ResearchNoteRevision;
  strategyId?: string;
  selection?: WorkbenchSelection;
}) {
  const restore = safeNoteEvidenceHref(note.restore_href);
  return (
    <article
      aria-label="Saved research note"
      data-testid="saved-research-note"
      className="space-y-4"
    >
      <h3 className="text-xl font-semibold">{note.content.title}</h3>
      <Metadata
        values={{
          "Note ID": note.note_id,
          "Immutable revision": note.revision,
          "Revision SHA-256": note.revision_sha256,
          "Saved at": note.created_at,
          "User-maintained status":
            noteStatusLabels[note.content.status ?? "pending"],
          Tags: (note.content.tags ?? []).join(", ") || "No tags",
          "Change note": note.change_note || "Not supplied",
          "Retained artifact pins": note.retained_pin_count,
          "Status provenance": note.status_basis,
        }}
      />
      <p className="notice">{noteEvidenceNotice}</p>
      <p className="text-sm">
        Referenced artifacts are retained by this immutable note revision.
        Explicit cleanup can break a reference; the old reference remains
        visible rather than pointing to a newer run.
      </p>
      {restore ? (
        <a className="text-link" href={restore}>
          Restore full saved research selection
        </a>
      ) : (
        <p role="status" className="notice">
          Full selection restoration is unavailable because its exact evidence
          could not be admitted. The saved note and broken-reference states
          remain visible.
        </p>
      )}
      {selection?.note_evidence_index !== undefined && (
        <HistoricalControlArm note={note} selection={selection} />
      )}
      {noteTextFields.map(([field, label]) => (
        <section key={field} aria-label={`Saved note ${label}`}>
          <h4 className="font-medium">{label}</h4>
          <p className="whitespace-pre-wrap break-words text-sm">
            {note.content[field] || "Not recorded in this note."}
          </p>
        </section>
      ))}
      <NoteEvidenceList
        evidence={note.content.evidence ?? []}
        resolutions={note.evidence}
        note={note}
        strategyId={strategyId ?? note.content.restore?.strategy_version_id}
      />
      {note.content.restore && (
        <JsonDetails
          title="Full immutable saved selection"
          value={note.content.restore}
        />
      )}
    </article>
  );
}
export function NoteTextFields({
  content,
  disabled,
  onChange,
}: {
  content: ResearchNoteContent;
  disabled: boolean;
  onChange: (patch: Partial<ResearchNoteContent>) => void;
}) {
  return (
    <>
      {noteTextFields.map(([field, label]) => (
        <label className="workbench-field" key={field}>
          {label}
          <textarea
            value={content[field] ?? ""}
            disabled={disabled}
            maxLength={field === "body" ? 32000 : 8000}
            rows={field === "body" ? 5 : 3}
            onChange={(event) => onChange({ [field]: event.target.value })}
          />
        </label>
      ))}
    </>
  );
}

export function HistoricalControlArm({
  note,
  selection,
}: {
  note: ResearchNoteRevision;
  selection: WorkbenchSelection;
}) {
  const index = selection.note_evidence_index;
  const ref = index === undefined ? undefined : note.content.evidence?.[index];
  const arm =
    ref?.kind === "controlled" && selection.note_arm !== undefined
      ? ref.arms.find((item) => item.ordinal === selection.note_arm)
      : undefined;
  if (!ref || ref.kind !== "controlled" || !arm)
    return (
      <p role="alert" className="notice error">
        The exact historical control arm is absent from this note revision. No
        other reference, arm or latest run was substituted.
      </p>
    );
  const resolutions = note.evidence.filter((item) => item.index === index);
  const resolved = resolutions.length === 1 ? resolutions[0] : undefined;
  const href = resolved?.state === "available" ? noteArmResultHref(arm) : null;
  return (
    <section
      aria-label="Historical controlled arm evidence"
      className="workbench-rule"
      data-testid="historical-control-arm"
    >
      <h3 className="font-medium">
        Saved control arm {arm.ordinal + 1} · note revision {note.revision}
      </h3>
      <p className="notice">
        This is the captured immutable control outcome, not current run status.
        A later retry is not loaded or substituted. A failed arm has no
        successful result and is never treated as zero profit.
      </p>
      <Metadata
        values={{
          "Controlled experiment": ref.controlled_experiment_id,
          "Request SHA-256": ref.request_sha256,
          "Common protocol SHA-256": ref.protocol_sha256,
          "Saved arm status": arm.status,
          "Saved generation": arm.generation,
          Run: arm.run_id,
          "Stopped physical attempt": arm.execution_attempt_id,
          "Evidence admission": resolved?.state ?? "unavailable",
          "Admission limitation":
            resolved?.reason ??
            (resolved?.state === "available"
              ? "No admission limitation reported"
              : "Admission reason unavailable; no successful admission asserted"),
          "Physical Evaluation":
            arm.result?.evaluation_id ?? "No successful result",
          "Result manifest SHA-256":
            arm.result?.result_manifest_sha256 ?? "No successful result",
        }}
      />
      {href ? (
        <a className="text-link" href={href}>
          Open this saved arm’s exact successful result
        </a>
      ) : (
        <p className="text-sm">
          No admitted successful result link is available for this saved arm.
          Its recorded status and immutable identifiers remain visible.
        </p>
      )}
      <JsonDetails title="Exact historical arm reference" value={arm} />
      {resolved?.bindings?.length ? (
        <JsonDetails
          title="Re-admitted immutable execution bindings"
          value={resolved.bindings}
        />
      ) : null}
    </section>
  );
}
