import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { WorkbenchSpec } from "@/client/types.gen";
import { ErrorState, Panel } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Pagination } from "@/components/data-common";
import { queryClient } from "@/lib/api";
import {
  saveSpecRevision,
  specRevisionsOptions,
  type SourceResource,
} from "@/lib/workbench-api";
import {
  forgetDescriptionDraft,
  loadDescriptionDraft,
  newDescriptionDraft,
  prepareDescriptionSubmission,
  revisionFailureIsDefinitive,
  storeDescriptionDraft,
  type DescriptionDraft,
} from "@/lib/workbench-draft";

export function DescriptionRevisions({
  owner,
  source,
  spec,
  onSelect,
}: {
  owner: string;
  source: SourceResource;
  spec: WorkbenchSpec;
  onSelect: (value: WorkbenchSpec) => void;
}) {
  const [offset, setOffset] = useState(0);
  const revisions = useQuery(specRevisionsOptions(source, offset));
  const [initial] = useState(() => {
    try {
      return {
        draft: loadDescriptionDraft(owner, source),
        error: null as unknown,
      };
    } catch (error) {
      return { draft: null, error };
    }
  });
  const [draft, setDraft] = useState<DescriptionDraft | null>(initial.draft);
  const [error, setError] = useState<unknown>(initial.error);
  const [unreadable, setUnreadable] = useState(Boolean(initial.error));
  const [editing, setEditing] = useState(Boolean(initial.draft));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const submitting = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const choices = revisions.data?.data || [];
  const displayedChoices = choices.some(
    (value) => value.spec_sha256 === spec.spec_sha256,
  )
    ? choices
    : [spec, ...choices];
  function persist(value: DescriptionDraft) {
    storeDescriptionDraft(owner, source, value);
    setDraft(value);
  }
  function begin() {
    try {
      if (!draft) persist(newDescriptionDraft(spec));
      setEditing(true);
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }
  function cancel() {
    if (busy || draft?.key) return;
    try {
      forgetDescriptionDraft(owner, source);
      setDraft(null);
      setEditing(false);
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }
  async function save() {
    if (
      !draft ||
      submitting.current ||
      draft.body.code_sha256 !== spec.code_sha256
    )
      return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    let pending: DescriptionDraft & { key: string };
    try {
      pending = prepareDescriptionSubmission(draft, spec);
    } catch (failure) {
      submitting.current = false;
      setBusy(false);
      setError(failure);
      return;
    }
    try {
      persist(pending); // A write is never sent unless the exact retry is durably stored.
      const result = await saveSpecRevision(source, pending.body, pending.key);
      setDraft(null);
      setEditing(false);
      setNotice(
        `Description revision ${result.revision} saved. Strategy code and parameters are unchanged.`,
      );
      try {
        forgetDescriptionDraft(owner, source);
      } catch {
        setNotice(
          `Description revision ${result.revision} saved, but its retry draft could not be cleared. Retrying returns the same revision.`,
        );
      }
      await queryClient.invalidateQueries({
        queryKey: ["workbench-revisions", source.kind, source.id],
      });
      if (mounted.current) onSelect(result);
    } catch (failure) {
      if (revisionFailureIsDefinitive(failure)) {
        try {
          persist({ ...pending, key: null });
        } catch {
          /* Keep the exact saved submission if storage becomes unavailable. */
        }
      }
      setError(failure);
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <Panel title="Versioned human description">
      <p className="text-sm">
        Description revision {spec.revision} · {spec.origin} · {spec.created_at}
        . This prose is not executable strategy code and never overrides runtime
        records.
      </p>
      <p className="text-xs break-all mt-2">
        Description SHA-256: {spec.spec_sha256}
      </p>
      {spec.change_note && (
        <p className="text-sm mt-2">Change note: {spec.change_note}</p>
      )}
      <label className="workbench-field">
        Description revision
        <select
          value={spec.spec_sha256}
          disabled={busy}
          onChange={(event) => {
            const value = displayedChoices.find(
              (item) => item.spec_sha256 === event.target.value,
            );
            if (value) onSelect(value);
          }}
        >
          {displayedChoices.map((value) => (
            <option key={value.spec_sha256} value={value.spec_sha256}>
              Revision {value.revision} · {value.origin} · {value.spec_sha256}
            </option>
          ))}
        </select>
      </label>
      {revisions.error && <ErrorState error={revisions.error} />}
      {revisions.data && (
        <Pagination
          offset={offset}
          limit={25}
          count={revisions.data.count}
          busy={revisions.isFetching}
          onPage={setOffset}
        />
      )}
      {!editing && !unreadable && (
        <Button variant="outline" onClick={begin}>
          Edit description
        </Button>
      )}
      {unreadable && (
        <Button
          variant="outline"
          onClick={() => {
            try {
              forgetDescriptionDraft(owner, source);
              setUnreadable(false);
              setError(null);
            } catch (failure) {
              setError(failure);
            }
          }}
        >
          Clear unreadable local description draft
        </Button>
      )}
      {editing && draft && (
        <form
          aria-label="Edit human description"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <p className="notice my-3">
            Editing prose from revision {draft.body.base_revision}. Rule IDs,
            source anchors, parameters, stage mappings and code remain
            immutable. A newer saved revision causes a conflict rather than
            silently overwriting it.
          </p>
          {draft.key && (
            <p className="notice">
              A save is pending or its outcome is uncertain. Retry the same
              stored request; do not create a replacement revision.
            </p>
          )}
          {draft.body.code_sha256 !== spec.code_sha256 && (
            <p role="alert" className="notice error">
              Draft code hash differs from the selected source. It cannot be
              submitted here.
            </p>
          )}
          {draft.body.rules.map((rule, index) => (
            <fieldset
              key={rule.rule_id}
              className="workbench-rule mt-3"
              disabled={busy || Boolean(draft.key)}
            >
              <legend className="text-sm">{rule.rule_id}</legend>
              <label className="workbench-field">
                Rule title: {rule.rule_id}
                <Input
                  value={rule.title}
                  required
                  maxLength={200}
                  onChange={(event) => {
                    try {
                      persist({
                        ...draft,
                        body: {
                          ...draft.body,
                          rules: draft.body.rules.map((item, position) =>
                            position === index
                              ? { ...item, title: event.target.value }
                              : item,
                          ),
                        },
                      });
                    } catch (failure) {
                      setError(failure);
                    }
                  }}
                />
              </label>
              <label className="workbench-field">
                Rule description: {rule.rule_id}
                <textarea
                  value={rule.description}
                  required
                  maxLength={4000}
                  rows={3}
                  onChange={(event) => {
                    try {
                      persist({
                        ...draft,
                        body: {
                          ...draft.body,
                          rules: draft.body.rules.map((item, position) =>
                            position === index
                              ? { ...item, description: event.target.value }
                              : item,
                          ),
                        },
                      });
                    } catch (failure) {
                      setError(failure);
                    }
                  }}
                />
              </label>
            </fieldset>
          ))}
          <label className="workbench-field">
            Revision change note
            <textarea
              value={draft.body.change_note || ""}
              disabled={busy || Boolean(draft.key)}
              maxLength={2000}
              rows={2}
              onChange={(event) => {
                try {
                  persist({
                    ...draft,
                    body: { ...draft.body, change_note: event.target.value },
                  });
                } catch (failure) {
                  setError(failure);
                }
              }}
            />
          </label>
          <div className="flex flex-wrap gap-3">
            <Button
              type="submit"
              disabled={busy || draft.body.code_sha256 !== spec.code_sha256}
            >
              {busy
                ? "Saving revision…"
                : draft.key
                  ? "Retry same description revision"
                  : "Save description revision"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy || Boolean(draft.key)}
              onClick={cancel}
            >
              Cancel description edit
            </Button>
          </div>
        </form>
      )}
      {notice && (
        <p role="status" className="notice mt-3">
          {notice}
        </p>
      )}
      {error != null && <ErrorState error={error} />}
    </Panel>
  );
}
