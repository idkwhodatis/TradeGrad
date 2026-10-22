import { useRef, useState } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { runFormSchema } from "@/lib/run-form";
import { ArrowLeft, Check, Info, LoaderCircle, Play } from "lucide-react";
import type {
  DatasetPublic,
  RunCreate,
  StrategyPublic,
} from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DemoNotice,
  NativeWindowNotice,
  ErrorState,
  Loading,
  PageHeader,
  Panel,
} from "@/components/common";
import {
  createRun,
  currentUserOptions,
  datasetsOptions,
  queryClient,
  strategiesOptions,
} from "@/lib/api";
import {
  forgetSubmission,
  loadSubmission,
  saveSubmission,
  submissionFor,
} from "@/lib/idempotency";
import { datasetOptions } from "@/lib/data-api";
import { asRecord, snapshotLabel } from "@/lib/data-utils";
import { taskLabels } from "@/lib/format";

export function NewRunPage() {
  const { dataset: preferred } = useSearch({ strict: false }) as {
    dataset?: string;
  };
  const datasets = useQuery(datasetsOptions);
  const preferredDataset = useQuery({
    ...datasetOptions(preferred || ""),
    enabled: !!preferred,
  });
  const strategies = useQuery(strategiesOptions);
  const user = useQuery(currentUserOptions);
  return (
    <>
      <PageHeader
        eyebrow="NEW EXPERIMENT"
        title="Start with a fixed seed"
        subtitle="A small, reproducible evaluation. No model setup or API keys needed."
      >
        <Link to="/runs" className="text-link">
          <ArrowLeft size={14} />
          Back to experiments
        </Link>
      </PageHeader>
      <DemoNotice />
      <NativeWindowNotice />
      <div className="mt-6">
        {datasets.isPending ||
        strategies.isPending ||
        user.isPending ||
        (!!preferred && preferredDataset.isPending) ? (
          <Loading />
        ) : datasets.error ||
          strategies.error ||
          user.error ||
          preferredDataset.error ? (
          <ErrorState
            error={
              datasets.error ||
              strategies.error ||
              user.error ||
              preferredDataset.error
            }
            retry={() => {
              void datasets.refetch();
              void strategies.refetch();
              void user.refetch();
              if (preferred) void preferredDataset.refetch();
            }}
          />
        ) : !datasets.data?.data.length ||
          !strategies.data?.data.length ||
          !user.data ? (
          <div className="notice">
            No compatible inputs are available. Ask your administrator to
            bootstrap the synthetic snapshots and unchanged seed strategies.
          </div>
        ) : (
          <RunForm
            owner={user.data.id}
            preferred={preferred}
            datasets={
              preferredDataset.data &&
              !datasets.data.data.some(
                (item) => item.id === preferredDataset.data?.id,
              )
                ? [preferredDataset.data, ...datasets.data.data]
                : datasets.data.data
            }
            strategies={strategies.data.data}
          />
        )}
      </div>
    </>
  );
}

function RunForm({
  owner,
  preferred,
  datasets,
  strategies,
}: {
  owner: string;
  preferred?: string;
  datasets: DatasetPublic[];
  strategies: StrategyPublic[];
}) {
  const navigate = useNavigate();
  const saved = useRef(preferred ? null : loadSubmission(owner));
  const first =
    datasets.find((dataset) => dataset.id === preferred) ||
    datasets.find(
      (dataset) =>
        dataset.market === "CN" && dataset.source === "synthetic_fixture",
    ) ||
    datasets.find((dataset) => dataset.market === "CN") ||
    datasets[0];
  const isBasket =
    Array.isArray(first.manifest.listings) &&
    first.manifest.listings.length > 1;
  const firstTask =
    first.market === "CN"
      ? isBasket
        ? "cn_cross"
        : "cn_time"
      : isBasket
        ? "us_cross"
        : "us_time";
  const defaults: RunCreate = saved.current?.body || {
    task: firstTask,
    dataset_version_id: first.id,
    strategy_version_id:
      strategies.find(
        (strategy) =>
          strategy.task === firstTask && !strategy.name.startsWith("E2E "),
      )?.id || "",
    start_date: first.start_date,
    end_date: first.end_date,
    profile: "corrected_explicit_config",
  };
  const {
    register,
    watch,
    setValue,
    setError,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<RunCreate>({
    resolver: zodResolver(runFormSchema),
    defaultValues: defaults,
  });
  const task = watch("task");
  const datasetId = watch("dataset_version_id");
  const selectedDataset = datasets.find((dataset) => dataset.id === datasetId);
  const compatibleDatasets = datasets.filter(
    (dataset) => dataset.market.toLowerCase() === task.slice(0, 2),
  );
  const compatibleStrategies = strategies.filter(
    (strategy) => strategy.task === task,
  );
  const [failure, setFailure] = useState<unknown>(null);
  const submitting = useRef(false);
  function selectDataset(id: string) {
    const dataset = datasets.find((item) => item.id === id);
    setValue("dataset_version_id", id);
    if (dataset) {
      setValue("start_date", dataset.start_date);
      setValue("end_date", dataset.end_date);
    }
  }
  function selectTask(value: RunCreate["task"]) {
    setValue("task", value);
    const dataset = datasets.find(
      (item) => item.market.toLowerCase() === value.slice(0, 2),
    );
    selectDataset(dataset?.id || "");
    setValue(
      "strategy_version_id",
      strategies.find(
        (item) => item.task === value && !item.name.startsWith("E2E "),
      )?.id || "",
    );
  }
  const submit = handleSubmit(async (body) => {
    if (submitting.current) return;
    if (
      !selectedDataset ||
      body.start_date < selectedDataset.start_date ||
      body.end_date > selectedDataset.end_date
    ) {
      setError("end_date", {
        message:
          "The evaluation window must stay inside the selected snapshot's coverage.",
      });
      return;
    }
    if (
      !compatibleStrategies.some((item) => item.id === body.strategy_version_id)
    ) {
      setError("strategy_version_id", {
        message: "Choose a seed that matches this market and task.",
      });
      return;
    }
    if (
      body.task.endsWith("_time") &&
      Array.isArray(selectedDataset.manifest.listings) &&
      selectedDataset.manifest.listings.length !== 1
    ) {
      setError("dataset_version_id", {
        message:
          "Time-series evaluation requires exactly one listing. Choose a basket task or a single-listing snapshot.",
      });
      return;
    }
    const compatibility = asRecord(
      selectedDataset.manifest.evaluation_compatibility,
    );
    if (compatibility.compatible === false) {
      setError("dataset_version_id", {
        message: Array.isArray(compatibility.reasons)
          ? compatibility.reasons.join(" ")
          : "This snapshot is not compatible with the fixed-seed evaluator. Inspect its metadata.",
      });
      return;
    }
    submitting.current = true;
    setFailure(null);
    try {
      const submission = submissionFor(body, saved.current);
      saved.current = submission;
      saveSubmission(owner, submission);
      const run = await createRun(body, submission.key);
      forgetSubmission(owner);
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
      await navigate({ to: "/runs/$runId", params: { runId: run.id } });
    } catch (error) {
      setFailure(error);
    } finally {
      submitting.current = false;
    }
  });
  return (
    <div className="section-grid">
      <Panel title="Evaluation configuration">
        <form onSubmit={submit} noValidate>
          <fieldset
            disabled={isSubmitting}
            className="form border-0 p-0 m-0 min-w-0"
          >
            <section>
              <div className="step-label">
                <span className="step-number">01</span>
                <h3>Choose your inputs</h3>
              </div>
              <div className="form">
                <div className="field">
                  <label htmlFor="task">Market & task</label>
                  <select
                    id="task"
                    {...register("task")}
                    onChange={(event) =>
                      selectTask(event.target.value as RunCreate["task"])
                    }
                  >
                    {Object.entries(taskLabels).map(([value, name]) => (
                      <option key={value} value={value}>
                        {name}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="dataset">Dataset snapshot</label>
                  <select
                    id="dataset"
                    {...register("dataset_version_id")}
                    onChange={(event) => selectDataset(event.target.value)}
                    aria-invalid={!!errors.dataset_version_id}
                    aria-describedby="dataset-help"
                  >
                    <option value="" disabled>
                      Select a snapshot
                    </option>
                    {compatibleDatasets.map((dataset) => (
                      <option key={dataset.id} value={dataset.id}>
                        {dataset.name} · {snapshotLabel(dataset)}
                      </option>
                    ))}
                  </select>
                  <small id="dataset-help">
                    {selectedDataset
                      ? `${selectedDataset.start_date} to ${selectedDataset.end_date}, exclusive end. Snapshot is immutable.`
                      : "No compatible dataset available."}
                  </small>
                  {selectedDataset && (
                    <p className="notice">
                      {snapshotLabel(selectedDataset)}. Price semantics and
                      coverage are fixed in the{" "}
                      <Link
                        className="text-link"
                        to="/datasets/$datasetId"
                        params={{ datasetId: selectedDataset.id }}
                      >
                        snapshot manifest
                      </Link>
                      .
                    </p>
                  )}
                  {errors.dataset_version_id && (
                    <span className="field-error">
                      {errors.dataset_version_id.message}
                    </span>
                  )}
                </div>
                <div className="field">
                  <label htmlFor="strategy">Seed strategy</label>
                  <select
                    id="strategy"
                    {...register("strategy_version_id")}
                    aria-invalid={!!errors.strategy_version_id}
                  >
                    <option value="" disabled>
                      Select a strategy
                    </option>
                    {compatibleStrategies.map((strategy) => (
                      <option key={strategy.id} value={strategy.id}>
                        {strategy.name}
                      </option>
                    ))}
                  </select>
                  <small>
                    Original native AKQuant strategy. No code generation or
                    optimization.
                  </small>
                  {errors.strategy_version_id && (
                    <span className="field-error">
                      {errors.strategy_version_id.message}
                    </span>
                  )}
                </div>
              </div>
            </section>
            <section className="border-t border-[#e8ece1] pt-6">
              <div className="step-label">
                <span className="step-number">02</span>
                <h3>Set the evaluation window</h3>
              </div>
              <div className="form-row">
                <div className="field">
                  <label htmlFor="start">Start date · inclusive</label>
                  <Input
                    type="date"
                    id="start"
                    min={selectedDataset?.start_date}
                    max={selectedDataset?.end_date}
                    {...register("start_date")}
                    aria-invalid={!!errors.start_date}
                  />
                  {errors.start_date && (
                    <span className="field-error">
                      {errors.start_date.message}
                    </span>
                  )}
                </div>
                <div className="field">
                  <label htmlFor="end">End date · exclusive</label>
                  <Input
                    type="date"
                    id="end"
                    min={selectedDataset?.start_date}
                    max={selectedDataset?.end_date}
                    {...register("end_date")}
                    aria-invalid={!!errors.end_date}
                    aria-describedby="end-help"
                  />
                  {errors.end_date && (
                    <span className="field-error">
                      {errors.end_date.message}
                    </span>
                  )}
                </div>
              </div>
              <p id="end-help" className="mt-3 text-xs muted">
                One fixed evaluation window, not a training/validation/holdout
                split.
              </p>
            </section>
            {Boolean(failure) && <ErrorState error={failure} />}
            <div className="form-footer">
              <Button
                type="button"
                variant="outline"
                disabled={isSubmitting}
                onClick={() => void navigate({ to: "/runs" })}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  isSubmitting ||
                  !compatibleDatasets.length ||
                  !compatibleStrategies.length
                }
              >
                {isSubmitting ? (
                  <>
                    <LoaderCircle className="spinning" />
                    Submitting…
                  </>
                ) : (
                  <>
                    <Play size={15} />
                    Start evaluation
                  </>
                )}
              </Button>
            </div>
          </fieldset>
        </form>
      </Panel>
      <div className="stack">
        <Panel title="What this run includes">
          <div className="space-y-4 text-xs text-[#60734e]">
            {[
              "Content-addressed dataset & seed version",
              "Explicit CN / US engine configuration",
              "Durable run, attempt, and ordered events",
              "Permission-checked result artifacts",
            ].map((text) => (
              <div className="flex gap-2.5 items-start" key={text}>
                <Check size={15} className="shrink-0 text-[#71954f]" />
                {text}
              </div>
            ))}
          </div>
          <dl className="details border-t border-[#edf0e8] pt-5">
            <div>
              <dt>Engine profile</dt>
              <dd className="!text-[10px]">corrected_explicit_config</dd>
            </div>
            <div>
              <dt>Execution mode</dt>
              <dd>Fixed seed evaluation</dd>
            </div>
            <div>
              <dt>Model calls</dt>
              <dd>None</dd>
            </div>
          </dl>
        </Panel>
        <div className="notice">
          <Info />
          <p>
            Submitting twice uses the same idempotency key. If the connection
            fails, retry the unchanged configuration to recover the same logical
            run.
          </p>
        </div>
      </div>
    </div>
  );
}
