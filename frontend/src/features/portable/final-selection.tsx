import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type {
  PortableFinalEvaluationCreate,
  PortableFinalEvaluationPreflight,
  FinalSelectionPublic,
} from "@/client/types.gen";
import {
  ErrorState,
  Loading,
  PageHeader,
  Panel,
  Status,
} from "@/components/common";
import {
  Field,
  JsonDetails,
  Metadata,
  Pagination,
} from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { currentUserOptions } from "@/lib/api";
import { stableJson } from "@/lib/data-utils";
import {
  PORTABLE_FINAL_PAGE_SIZE,
  portableFinalSelectionOptions,
  portableFinalJobsOptions,
  preflightPortableFinal,
  portableExposureOptions,
  portableSplitOptions,
  submitPortableFinal,
  portableFrozenSourceOptions,
  refreshPortableFinal,
} from "@/lib/portable-final-api";
import { shortId } from "@/lib/format";
import { ExposurePanel, SplitPanel } from "@/features/final-tests/exposure";
import { portableRunOptions } from "@/lib/portable-api";
import { useDurableAction } from "@/features/final-tests/durable-action";
import {
  finalTestSchema,
  portableFinalNotice,
  portableSelectionMatches,
  portableFinalPreflightMatches,
} from "./final-protocol";

export function PortableFinalSelectionPage() {
  const { selectionId } = useParams({ strict: false }) as {
    selectionId: string;
  };
  const owner = useQuery(currentUserOptions);
  return owner.isPending ? (
    <Loading text="Verifying frozen selection owner…" />
  ) : owner.error ? (
    <ErrorState error={owner.error} retry={() => void owner.refetch()} />
  ) : owner.data ? (
    <PortableSelectionLoader
      key={`${owner.data.id}:${selectionId}`}
      owner={owner.data.id}
      selectionId={selectionId}
    />
  ) : null;
}
function PortableSelectionLoader({
  owner,
  selectionId,
}: {
  owner: string;
  selectionId: string;
}) {
  const selection = useQuery(portableFinalSelectionOptions(owner, selectionId));
  if (selection.isPending)
    return <Loading text="Loading frozen portable selection…" />;
  if (selection.error)
    return (
      <ErrorState
        error={selection.error}
        retry={() => void selection.refetch()}
      />
    );
  if (!selection.data)
    return <p className="notice">No frozen selection is available.</p>;
  if (!portableSelectionMatches(selection.data))
    return (
      <ErrorState
        error={
          new Error(
            "This selection does not contain a frozen portable optimization source. Native selections cannot use portable final execution.",
          )
        }
      />
    );
  return <PortableSelectionContent owner={owner} selection={selection.data} />;
}
export function PortableSelectionContent({
  owner,
  selection,
}: {
  owner: string;
  selection: FinalSelectionPublic;
}) {
  const split = useQuery(
    portableSplitOptions(owner, selection.split_version_id),
  );
  const source = useQuery(
    portableFrozenSourceOptions(owner, selection.strategy_version_id),
  );
  const exposure = useQuery(
    portableExposureOptions(owner, selection.split_version_id),
  );
  const sourceRun = useQuery(
    portableRunOptions(owner, selection.source_run_id),
  );
  const [offset, setOffset] = useState(0);
  const jobs = useQuery(portableFinalJobsOptions(owner, selection.id, offset));
  return (
    <>
      <Link
        className="text-link mb-5"
        to="/portable/runs/$runId"
        params={{ runId: selection.source_run_id }}
      >
        Source research run
      </Link>
      <PageHeader
        eyebrow={`FROZEN SELECTION ${shortId(selection.id)}`}
        title="Frozen portable candidate, separate final test"
        subtitle="Immutable source, selection evidence and holdout protocol. Reading this page does not submit a test."
      />
      <p className="notice" data-testid="portable-final-trust">
        {portableFinalNotice}
      </p>
      <div className="stack mt-6">
        <ExposurePanel
          snapshot={selection.exposure_snapshot}
          title="Exposure recorded when the candidate was frozen"
        />
        <Panel title="Exact frozen candidate">
          <Metadata
            values={{
              "Selection ID": selection.id,
              "Source research run": selection.source_run_id,
              "Source candidate": selection.source_candidate_id,
              "Source checkpoint": selection.source_checkpoint_id,
              "Frozen strategy version": selection.strategy_version_id,
              "Code SHA-256": selection.source_code_sha256,
              "Source config SHA-256": selection.source_config_sha256,
              "Checkpoint SHA-256": selection.source_checkpoint_sha256,
              "Frozen at": selection.created_at,
            }}
          />
          {sourceRun.isPending ? (
            <Loading text="Reading original dual-image identities…" />
          ) : sourceRun.error ? (
            <ErrorState
              error={sourceRun.error}
              retry={() => void sourceRun.refetch()}
            />
          ) : (
            sourceRun.data && (
              <Metadata
                values={{
                  "Source trusted engine image ID":
                    sourceRun.data.research_image_digest,
                  "Source isolated policy image ID":
                    sourceRun.data.portable_policy_image_digest,
                  "Source environment SHA-256":
                    sourceRun.data.config.environment_digest,
                  "Raw dataset reference": sourceRun.data.config.dataset,
                }}
              />
            )
          )}
          <p className="notice mt-4">
            Current cash-daily raw-price profile only. Synthetic fixtures are
            not market or investment evidence. Final preflight re-verifies both
            original execution image identities and the pinned environment; no
            profile conversion is offered.
          </p>
          {source.isPending ? (
            <Loading text="Loading frozen source code…" />
          ) : source.error ? (
            <ErrorState
              error={source.error}
              retry={() => void source.refetch()}
            />
          ) : (
            source.data &&
            (source.data.mode !== "portable" ||
            source.data.content_sha256 !== selection.source_code_sha256 ? (
              <ErrorState
                error={
                  new Error(
                    "Frozen portable source identity differs. No source is displayed.",
                  )
                }
              />
            ) : (
              <details className="mt-4">
                <summary className="text-link">
                  Read exact frozen source code
                </summary>
                <pre className="preview" tabIndex={0}>
                  {source.data.code}
                </pre>
              </details>
            ))
          )}
          <JsonDetails
            title="Original training/validation selection evidence (not final-test feedback)"
            value={selection.selection_evidence}
          />
        </Panel>
        {split.isPending ? (
          <Loading text="Loading pinned split…" />
        ) : split.error ? (
          <ErrorState error={split.error} retry={() => void split.refetch()} />
        ) : (
          split.data && <SplitPanel split={split.data} />
        )}
        {exposure.isPending ? (
          <Loading text="Checking current tracked exposure…" />
        ) : exposure.error ? (
          <ErrorState
            error={exposure.error}
            retry={() => void exposure.refetch()}
          />
        ) : (
          exposure.data && (
            <ExposurePanel
              snapshot={exposure.data}
              title="Current owner-wide platform-recorded exposure"
            />
          )
        )}
        <Panel title="Explicit final holdout submission">
          <PortableFinalTestForm
            key={`${owner}:${selection.id}`}
            owner={owner}
            selection={selection}
          />
        </Panel>
        <Panel title="Persisted final-test jobs">
          {jobs.isPending ? (
            <Loading />
          ) : jobs.error ? (
            <ErrorState error={jobs.error} retry={() => void jobs.refetch()} />
          ) : (
            jobs.data && (
              <>
                {!jobs.data.data.length ? (
                  <p className="notice">
                    No final test has been submitted for this selection.
                  </p>
                ) : (
                  <ul className="stack">
                    {jobs.data.data.map((job) => (
                      <li className="flex items-center gap-4" key={job.id}>
                        <Link
                          to="/portable/final-tests/$finalTestId"
                          params={{ finalTestId: job.id }}
                          className="text-link"
                        >
                          Final test {shortId(job.id)}
                        </Link>
                        <Status status={job.status} />
                        <span className="text-xs muted">
                          Generation {job.generation} · {job.created_at}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
                <Pagination
                  offset={offset}
                  limit={PORTABLE_FINAL_PAGE_SIZE}
                  count={jobs.data.count}
                  busy={jobs.isFetching}
                  onPage={setOffset}
                />
              </>
            )
          )}
        </Panel>
      </div>
    </>
  );
}
export function PortableFinalTestForm({
  owner,
  selection,
}: {
  owner: string;
  selection: FinalSelectionPublic;
}) {
  const selectionId = selection.id;
  const navigate = useNavigate();
  const draft = useDurableAction<PortableFinalEvaluationCreate>({
    owner,
    scope: `portable-test:${selectionId}`,
    schema: finalTestSchema,
    initial: { selection_id: selectionId, max_runtime_seconds: 300 },
  });
  const [confirmed, setConfirmed] = useState(false);
  const [checking, setChecking] = useState(false);
  const checkingRef = useRef(false);
  const active = useRef(true);
  const bodyRef = useRef(draft.body);
  bodyRef.current = draft.body;
  const [preflight, setPreflight] = useState<{
    fingerprint: string;
    result: PortableFinalEvaluationPreflight;
  } | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  const valid =
    portableSelectionMatches(selection) &&
    finalTestSchema.safeParse(draft.body).success &&
    draft.body?.selection_id === selectionId;
  const checked = Boolean(
    preflight?.result.eligible &&
    preflight.fingerprint === stableJson(draft.body),
  );
  async function check() {
    if (!valid || !draft.body || checkingRef.current) return;
    const body = structuredClone(draft.body);
    const fingerprint = stableJson(body);
    checkingRef.current = true;
    setChecking(true);
    setFailure(null);
    setPreflight(null);
    try {
      const result = await preflightPortableFinal(body);
      if (
        !portableFinalPreflightMatches(result, selection) ||
        result.config.max_runtime_seconds !== body.max_runtime_seconds
      )
        throw new Error(
          "Final preflight returned a different frozen source, holdout or environment. Submission is blocked.",
        );
      if (active.current && stableJson(bodyRef.current) === fingerprint)
        setPreflight({ fingerprint, result });
    } catch (error) {
      if (active.current) setFailure(error);
    } finally {
      checkingRef.current = false;
      if (active.current) setChecking(false);
    }
  }
  return (
    <div className="stack">
      <p className="notice">
        Portable final testing is a separate holdout job with no optimizer or
        model calls. It cannot change the frozen candidate or feed a score back
        into candidate selection. It records additional platform exposure. No
        pause or optimizer resume is supported.
      </p>
      <Field
        id="portable-final-runtime"
        label="Maximum portable final-test runtime · seconds"
        help="Only the execution timeout can be chosen here; frozen code, dates, data and environment cannot be edited."
      >
        <Input
          id="portable-final-runtime"
          type="number"
          min={1}
          max={3600}
          step={1}
          value={draft.body?.max_runtime_seconds ?? ""}
          disabled={draft.busy || checking}
          onChange={(event) => {
            setConfirmed(false);
            setPreflight(null);
            setFailure(null);
            draft.edit({
              selection_id: selectionId,
              max_runtime_seconds: Number(event.target.value),
            });
          }}
        />
      </Field>
      <label className="flex items-start gap-3">
        <input
          type="checkbox"
          checked={confirmed}
          disabled={draft.busy}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        <span>
          I confirm running the frozen candidate on the declared holdout.
          Historical/external use remains unknown; this is not independent
          performance certification.
        </span>
      </label>
      {draft.hasPending && (
        <p className="notice" data-testid="portable-final-pending">
          A previous submission response is unconfirmed. Retry sends its
          original immutable body and key, even if that job has already started.
          It does not allocate a new test.
        </p>
      )}
      {draft.storageWarning && (
        <p className="notice error" role="alert">
          Tab storage is unavailable. No new submission can be sent without
          saving its retry key.
        </p>
      )}
      {Boolean(failure || draft.error) && (
        <ErrorState error={failure || draft.error} />
      )}
      {preflight && preflight.fingerprint === stableJson(draft.body) && (
        <>
          <p className="notice" role="status">
            {preflight.result.eligible
              ? "Portable final preflight passed"
              : "Portable final preflight blocked"}
            . Remaining quota: {preflight.result.quota_remaining}.
          </p>
          {preflight.result.warnings.map((warning, index) => (
            <p className="notice" key={index}>
              {warning}
            </p>
          ))}
          <JsonDetails
            title="Exact final execution configuration"
            value={preflight.result.config}
          />
          <ExposurePanel
            snapshot={preflight.result.exposure_snapshot}
            title="Exposure checked by final preflight"
          />
        </>
      )}
      <div className="actions">
        <Button
          variant="outline"
          disabled={!valid || checking || draft.busy}
          onClick={() => void check()}
        >
          {checking ? "Checking…" : "Run portable final preflight"}
        </Button>
        <Button
          disabled={
            !valid ||
            !confirmed ||
            (!checked && !draft.hasPending) ||
            checking ||
            draft.busy
          }
          onClick={() =>
            void draft.run(submitPortableFinal, (job) => {
              void refreshPortableFinal(owner);
              void navigate({
                to: "/portable/final-tests/$finalTestId",
                params: { finalTestId: job.id },
              });
            })
          }
        >
          {draft.busy
            ? "Submitting…"
            : draft.hasPending
              ? "Retry same portable final submission"
              : "Submit portable final holdout test"}
        </Button>
      </div>
    </div>
  );
}
