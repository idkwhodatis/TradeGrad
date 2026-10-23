import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useForm, type FieldPath } from "react-hook-form";
import { ArrowLeft, Check, LoaderCircle, Play } from "lucide-react";
import type { ResearchCapabilities } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/common";
import { JsonDetails, Metadata } from "@/components/data-common";
import { currentUserOptions, queryClient } from "@/lib/api";
import { asRecord, snapshotLabel, universeLabels } from "@/lib/data-utils";
import { taskLabels } from "@/lib/format";
import {
  matchingUniverses,
  profileDescriptions,
  researchDefaults,
  researchFormSchema,
  researchModelProfileFor,
  researchModelProfiles,
  researchSelectionIssues,
  researchStepFor,
  suggestWindows,
  versionRef,
  type ResearchCatalog,
  type ResearchFormValues,
  type ResearchIssue,
} from "@/lib/research-form";
import {
  clearAcceptedResearchDraft,
  researchCompletionIsCurrent,
  hasStoredResearchDraft,
  loadResearchDraft,
  matchingPreflight,
  preflightResearch,
  researchCapabilitiesOptions,
  researchCatalogOptions,
  researchFingerprint,
  researchSubmissionFor,
  saveResearchDraft,
  singleFlight,
  submitResearch,
  type ResearchPreflight,
} from "@/lib/research-submit";

const steps = [
  "Inputs & profile",
  "Windows & history",
  "Engine & optimization",
  "Review & preflight",
];
type Completion = { onCreated?: (id: string) => void };

function ModelProfileNotice({
  profile,
}: {
  profile: ReturnType<typeof researchModelProfileFor>;
}) {
  if (!profile) return null;
  return (
    <p className="notice" data-testid="research-selected-model-notice">
      {profile.synthetic
        ? profile.config.provider === "fixture"
          ? "The deterministic fixture does not call a paid model. Model responses and usage are synthetic, not model-quality or cost evidence."
          : "This operator profile is marked synthetic. It uses an operator-managed endpoint, but responses and usage are not paid-model quality or cost evidence."
        : "This operator-managed profile can make paid online model calls after submission. Strategy code, task instructions and research feedback are sent to the configured provider. Call, token and runtime limits are not a currency spending cap; no verified price estimate is available."}
    </p>
  );
}

export function NewExperimentPage({ onCreated }: Completion = {}) {
  const { dataset: preferred } = useSearch({ strict: false }) as {
    dataset?: string;
  };
  const catalog = useQuery(researchCatalogOptions);
  const capabilities = useQuery(researchCapabilitiesOptions);
  const user = useQuery(currentUserOptions);
  const availableModels = capabilities.data
    ? researchModelProfiles(capabilities.data)
    : [];
  const hasOperatorModels = availableModels.some(
    (profile) => profile.config.provider !== "fixture",
  );
  const retry = () => {
    void catalog.refetch();
    void capabilities.refetch();
    void user.refetch();
  };
  let defaults: ResearchFormValues | null = null;
  let defaultsError: unknown;
  if (catalog.data && capabilities.data) {
    try {
      defaults = researchDefaults(capabilities.data, catalog.data, preferred);
    } catch (error) {
      defaultsError = error;
    }
  }
  return (
    <>
      <PageHeader
        eyebrow="NEW EXPERIMENT"
        title="Configure a research experiment"
        subtitle="Pin exact inputs, declare the protocol, then validate before allocating a run."
      >
        <Link to="/runs" className="text-link">
          <ArrowLeft size={14} />
          Back to experiments
        </Link>
      </PageHeader>
      {capabilities.data && (
        <p
          className="notice"
          data-testid={
            hasOperatorModels
              ? "research-model-notice"
              : "research-fixture-notice"
          }
        >
          {hasOperatorModels
            ? availableModels.some((profile) => !profile.synthetic)
              ? "Choose only operator-enabled model profiles available to your account. The deterministic synthetic fixture remains the default. Non-synthetic operator profiles can make paid online calls; credentials and endpoints stay on the server."
              : "Choose only operator-enabled synthetic model profiles available to your account. The deterministic fixture remains the default. No live model calls are enabled; credentials and endpoints stay on the server."
            : "This deployment runs deterministic fixture-model optimization only. Model responses and token counts are synthetic, not paid-model quality or cost evidence."}{" "}
          No live trading, portable execution, or final holdout evaluation in
          this wizard.
        </p>
      )}
      <div className="mt-6">
        {catalog.isPending || capabilities.isPending || user.isPending ? (
          <Loading text="Loading published versions and deployment capabilities…" />
        ) : catalog.error ||
          capabilities.error ||
          user.error ||
          defaultsError ? (
          <ErrorState
            error={
              catalog.error || capabilities.error || user.error || defaultsError
            }
            retry={retry}
          />
        ) : catalog.data && capabilities.data && user.data && defaults ? (
          <ExperimentWizard
            key={`${user.data.id}:${preferred || "draft"}`}
            owner={user.data.id}
            catalog={catalog.data}
            capabilities={capabilities.data}
            defaults={defaults}
            preferred={preferred}
            onCreated={onCreated}
          />
        ) : null}
      </div>
    </>
  );
}

function ExperimentWizard({
  owner,
  catalog,
  capabilities,
  defaults,
  preferred,
  onCreated,
}: {
  owner: string;
  catalog: ResearchCatalog;
  capabilities: ResearchCapabilities;
  defaults: ResearchFormValues;
  preferred?: string;
} & Completion) {
  const navigate = useNavigate();
  const previous = useRef(loadResearchDraft(owner));
  const rejectedDraft = useRef(
    !previous.current && hasStoredResearchDraft(owner),
  );
  const restored = useRef(preferred ? null : previous.current);
  const submission = useRef(previous.current?.submission || null);
  const [step, setStep] = useState(restored.current?.step || 0);
  const [preflight, setPreflight] = useState<ResearchPreflight | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const [busy, setBusy] = useState<"preflight" | "submit" | null>(null);
  const [issues, setIssues] = useState<ResearchIssue[]>([]);
  const [storageWarning, setStorageWarning] = useState(false);
  const [accepted, setAccepted] = useState("");
  const active = useRef(true);
  const runOnce = useRef(singleFlight<void>());
  const {
    register,
    watch,
    getValues,
    setValue,
    reset,
    setError,
    clearErrors,
    getFieldState,
    formState,
  } = useForm<ResearchFormValues>({
    defaultValues: restored.current?.body || defaults,
  });
  const body = watch();
  const modelProfiles = researchModelProfiles(capabilities);
  const selectedModelProfile = researchModelProfileFor(
    body.model,
    capabilities,
  );
  const modelProfileHash =
    body.model.provider === "fixture"
      ? "None: deterministic fixture"
      : body.model.profile_sha256;
  const modelOutputLimit =
    body.model.provider === "fixture"
      ? "Not applicable: deterministic fixture"
      : body.model.max_output_tokens;
  const dataset = catalog.datasets.find((row) => row.id === body.dataset.id);
  const universe = catalog.universes.find((row) => row.id === body.universe.id);
  const seed = catalog.strategies.find(
    (row) => row.id === body.seed_strategy.id,
  );
  const sourceCompat = body.profile === "source_compat";
  const compatibleDatasets = catalog.datasets.filter(
    (row) => row.market === body.market_profile.market,
  );
  const compatibleUniverses = matchingUniverses(dataset, catalog.universes);
  const compatibleSeeds = catalog.strategies.filter(
    (row) => row.task === body.task && row.mode === "native_akquant",
  );
  const persist = (value: ResearchFormValues, position = step) =>
    saveResearchDraft(owner, {
      version: 1,
      body: value,
      step: position,
      submission: submission.current,
    });
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  useEffect(() => {
    setPreflight(null);
  }, [capabilities]);
  useEffect(() => {
    const subscription = watch((value) => {
      setPreflight(null);
      setFailure(null);
      setIssues([]);
      clearErrors();
      setStorageWarning(
        !saveResearchDraft(owner, {
          version: 1,
          body: value as ResearchFormValues,
          step,
          submission: submission.current,
        }),
      );
    });
    return () => subscription.unsubscribe();
  }, [watch, owner, step, clearErrors]);
  useEffect(() => {
    if (issues.length) document.getElementById(issues[0].path)?.focus();
  }, [issues, step]);
  function move(next: number) {
    setStep(next);
    setStorageWarning(!persist(getValues(), next));
  }
  function chooseDataset(id: string) {
    const selected = catalog.datasets.find((row) => row.id === id);
    setValue("dataset", versionRef(selected));
    setValue(
      "universe",
      versionRef(matchingUniverses(selected, catalog.universes)[0]),
    );
    if (selected)
      setValue(
        "windows",
        suggestWindows(selected, getValues("warmup.sessions")),
      );
  }
  function chooseProfile(
    task: ResearchFormValues["task"],
    profile: ResearchFormValues["profile"],
  ) {
    const selected = capabilities.profile_defaults.find(
      (row) => row.task === task && row.profile === profile,
    );
    if (!selected) return;
    const taskChanged = task !== body.task;
    const nextDataset = taskChanged
      ? catalog.datasets.find(
          (row) =>
            row.market.toLowerCase() === task.slice(0, 2) &&
            (!task.endsWith("_time") ||
              (Array.isArray(row.manifest.listings) &&
                row.manifest.listings.length === 1)),
        )
      : dataset;
    reset({
      ...getValues(),
      task,
      profile,
      market_profile: structuredClone(
        selected.market_profile,
      ) as ResearchFormValues["market_profile"],
      warmup: structuredClone(selected.warmup) as ResearchFormValues["warmup"],
      ...(taskChanged
        ? {
            dataset: versionRef(nextDataset),
            universe: versionRef(
              matchingUniverses(nextDataset, catalog.universes)[0],
            ),
            seed_strategy: versionRef(
              catalog.strategies.find((row) => row.task === task),
            ),
            windows: nextDataset
              ? suggestWindows(nextDataset, selected.warmup.sessions)
              : defaults.windows,
          }
        : {}),
    });
  }
  function validate(onlyStep?: number): ResearchFormValues | null {
    clearErrors();
    const value = getValues();
    const parsed = researchFormSchema.safeParse(value);
    const all: ResearchIssue[] = parsed.success
      ? researchSelectionIssues(parsed.data, catalog, capabilities)
      : parsed.error.issues.map((issue) => ({
          path: issue.path.join("."),
          message: issue.message,
        }));
    const shown =
      onlyStep === undefined
        ? all
        : all.filter((issue) => researchStepFor(issue.path) === onlyStep);
    setIssues(shown);
    shown.forEach((issue, index) =>
      setError(
        issue.path as FieldPath<ResearchFormValues>,
        { type: "validate", message: issue.message },
        { shouldFocus: index === 0 },
      ),
    );
    if (shown.length) {
      if (onlyStep === undefined) move(researchStepFor(shown[0].path));
      return null;
    }
    return value;
  }
  function checkPreflight() {
    return runOnce.current(async () => {
      const value = validate();
      if (!value) return;
      setBusy("preflight");
      setFailure(null);
      setPreflight(null);
      const fingerprint = researchFingerprint(value);
      try {
        const result = await preflightResearch(value);
        if (active.current && fingerprint === researchFingerprint(getValues()))
          setPreflight({ fingerprint, result });
      } catch (error) {
        if (active.current) setFailure(error);
      } finally {
        if (active.current) setBusy(null);
      }
    });
  }
  function submit() {
    return runOnce.current(async () => {
      const value = validate();
      if (!value || !matchingPreflight(value, preflight)) return;
      setBusy("submit");
      setFailure(null);
      try {
        const pending = researchSubmissionFor(value, submission.current);
        submission.current = pending;
        if (!persist(value))
          throw new Error(
            "This browser cannot save the retry key. Enable tab storage before submitting; no request was sent.",
          );
        const run = await submitResearch(pending.body, pending.key);
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
        void queryClient.invalidateQueries({ queryKey: ["research-runs"] });
        if (
          researchCompletionIsCurrent(pending, {
            active: active.current,
            body: getValues(),
            submission: submission.current,
          })
        ) {
          clearAcceptedResearchDraft(owner, pending);
          submission.current = null;
          setAccepted(run.id);
          if (onCreated) onCreated(run.id);
          else
            void navigate({
              to: "/research-runs/$runId",
              params: { runId: run.id },
            });
        }
      } catch (error) {
        if (active.current) setFailure(error);
      } finally {
        if (active.current) setBusy(null);
      }
    });
  }
  function attrs(name: FieldPath<ResearchFormValues>) {
    return {
      id: name,
      "aria-invalid": !!getFieldState(name, formState).error,
      "aria-describedby": `${name}-help ${name}-error`,
    };
  }
  function field(
    name: FieldPath<ResearchFormValues>,
    label: string,
    input: ReactNode,
    help?: string,
  ) {
    const error = getFieldState(name, formState).error?.message;
    return (
      <div className="field" key={name}>
        <label htmlFor={name}>{label}</label>
        {input}
        {help && <small id={`${name}-help`}>{help}</small>}
        {error && (
          <span className="field-error" id={`${name}-error`}>
            {error}
          </span>
        )}
      </div>
    );
  }
  function number(
    name: FieldPath<ResearchFormValues>,
    label: string,
    min: number,
    max: number,
    help?: string,
    increment = 1,
    readOnly = false,
  ) {
    return field(
      name,
      label,
      <Input
        type="number"
        min={min}
        max={max}
        step={increment}
        readOnly={readOnly}
        {...register(name, { valueAsNumber: true })}
        {...attrs(name)}
      />,
      help,
    );
  }
  function windowFields(
    name: "train" | "validation" | "holdout",
    title: string,
  ) {
    return (
      <section className="form" key={name}>
        <h3>{title}</h3>
        <div className="form-row">
          {(["start", "end"] as const).map((edge) => {
            const path = `windows.${name}.${edge}` as const;
            return field(
              path,
              `${title} ${edge} · ${edge === "start" ? "inclusive" : "exclusive"}`,
              <Input
                type="date"
                min={dataset?.start_date}
                max={dataset?.end_date}
                {...register(path)}
                {...attrs(path)}
              />,
            );
          })}
        </div>
      </section>
    );
  }
  if (accepted)
    return (
      <Panel title="Experiment accepted">
        <p>The run was accepted. Open its persisted record below.</p>
        <a
          className="text-link"
          href={`/research-runs/${encodeURIComponent(accepted)}`}
        >
          Open research run
        </a>
      </Panel>
    );
  return (
    <div className="stack">
      {rejectedDraft.current && (
        <p className="notice" data-testid="research-draft-unrestored">
          A saved draft could not be restored under the current configuration
          schema. It has not been translated or resubmitted. Check your
          experiments for a previously accepted run before creating another;
          editing this form replaces the old draft.
        </p>
      )}
      {restored.current && (
        <p className="notice">
          Restored this signed-in owner’s draft. Run server preflight again
          before submitting.
        </p>
      )}
      {!selectedModelProfile && (
        <p className="notice" data-testid="research-model-unavailable">
          The saved model profile is no longer available with this exact
          configuration. Choose an available profile in Engine &amp;
          optimization and run preflight again. No profile is substituted
          automatically.
        </p>
      )}
      {submission.current && (
        <p className="notice" data-testid="research-retry-notice">
          A previous submission may have been accepted. Retrying unchanged
          configuration uses the same key, including after reload. Editing
          creates a different submission; inspect the experiments list before
          starting another.
        </p>
      )}
      {storageWarning && (
        <p className="notice">
          The draft could not be saved. Complete valid fields and enable tab
          storage before submitting.
        </p>
      )}
      <nav aria-label="Experiment steps" className="flex flex-wrap gap-2">
        {steps.map((title, index) => (
          <Button
            key={title}
            type="button"
            variant={step === index ? "default" : "outline"}
            size="sm"
            disabled={!!busy || index > step}
            aria-current={step === index ? "step" : undefined}
            onClick={() => move(index)}
          >
            {index + 1}. {title}
          </Button>
        ))}
      </nav>
      <Panel title={`${step + 1}. ${steps[step]}`}>
        <form
          className="form"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            if (step < 3) {
              if (validate(step)) move(step + 1);
            } else void submit();
          }}
        >
          <fieldset disabled={!!busy} className="form border-0 p-0 m-0 min-w-0">
            {step === 0 && (
              <>
                {field(
                  "task",
                  "Market & task",
                  <select
                    {...register("task")}
                    {...attrs("task")}
                    onChange={(event) =>
                      chooseProfile(
                        event.target.value as ResearchFormValues["task"],
                        body.profile,
                      )
                    }
                  >
                    {(capabilities.tasks || []).map((task) => (
                      <option key={task} value={task}>
                        {taskLabels[task]}
                      </option>
                    ))}
                  </select>,
                )}
                {field(
                  "profile",
                  "Execution profile",
                  <select
                    {...register("profile")}
                    {...attrs("profile")}
                    onChange={(event) =>
                      chooseProfile(
                        body.task,
                        event.target.value as ResearchFormValues["profile"],
                      )
                    }
                  >
                    <option value="corrected_explicit_config">
                      Corrected explicit configuration · recommended
                    </option>
                    <option value="source_compat">
                      Historical source compatibility
                    </option>
                  </select>,
                  profileDescriptions[body.profile],
                )}
                {field(
                  "dataset.id",
                  "Published dataset version",
                  <select
                    {...register("dataset.id")}
                    value={body.dataset.id}
                    {...attrs("dataset.id")}
                    onChange={(event) => chooseDataset(event.target.value)}
                  >
                    <option value="">Select a published snapshot</option>
                    {compatibleDatasets.map((row) => (
                      <option key={row.id} value={row.id}>
                        {row.name} · {snapshotLabel(row)} · {row.id}
                      </option>
                    ))}
                  </select>,
                  dataset
                    ? `[${dataset.start_date}, ${dataset.end_date}) · SHA-256 ${dataset.content_sha256}`
                    : "No matching published snapshot. Import, validate and publish one in Datasets.",
                )}
                {dataset && (
                  <>
                    <Metadata
                      values={{
                        "Source classification": snapshotLabel(dataset),
                        "Price basis":
                          dataset.manifest.price_basis ||
                          dataset.manifest.price_semantics ||
                          "Unknown; inspect manifest",
                        "Volume units":
                          asRecord(dataset.manifest.units).volume ||
                          "See normalized snapshot metadata",
                        "Universe evidence":
                          asRecord(dataset.manifest.universe).state ||
                          dataset.manifest.universe_status ||
                          "Unknown",
                      }}
                    />
                    <JsonDetails
                      title="Inspect immutable dataset semantics and coverage"
                      value={dataset.manifest}
                    />
                  </>
                )}
                {field(
                  "universe.id",
                  "Exact universe version",
                  <select
                    {...register("universe.id")}
                    value={body.universe.id}
                    {...attrs("universe.id")}
                    onChange={(event) =>
                      setValue(
                        "universe",
                        versionRef(
                          catalog.universes.find(
                            (row) => row.id === event.target.value,
                          ),
                        ),
                      )
                    }
                  >
                    <option value="">Select the matching universe</option>
                    {compatibleUniverses.map((row) => (
                      <option key={row.id} value={row.id}>
                        {row.name} · {universeLabels[row.state] || row.state} ·{" "}
                        {row.id}
                      </option>
                    ))}
                  </select>,
                  universe
                    ? `SHA-256 ${universe.content_sha256}. A fixed/current basket is not proof of historical PIT membership.`
                    : "The exact listing set and any pinned universe hash must match. Create a matching immutable universe if none is listed.",
                )}
                {field(
                  "seed_strategy.id",
                  "Seed strategy version",
                  <select
                    {...register("seed_strategy.id")}
                    value={body.seed_strategy.id}
                    {...attrs("seed_strategy.id")}
                    onChange={(event) =>
                      setValue(
                        "seed_strategy",
                        versionRef(
                          catalog.strategies.find(
                            (row) => row.id === event.target.value,
                          ),
                        ),
                      )
                    }
                  >
                    <option value="">Select a compatible seed</option>
                    {compatibleSeeds.map((row) => (
                      <option key={row.id} value={row.id}>
                        {row.name} · {row.id}
                      </option>
                    ))}
                  </select>,
                  seed
                    ? `native_akquant · SHA-256 ${seed.content_sha256}. Portable mode is unavailable.`
                    : "Choose a native AKQuant seed matching the task.",
                )}
              </>
            )}
            {step === 1 && (
              <>
                <p className="notice">
                  Every date range is half-open [start, end), using exchange
                  session dates. Suggestions are not coverage certification.
                  Training runs separately by calendar year; validation runs as
                  one window. Native arbitrary Python shares the engine process,
                  so this protocol is not proof against look-ahead or ledger
                  tampering.
                </p>
                {windowFields("train", "Training")}
                {windowFields("validation", "Validation")}
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={body.windows.holdout !== null}
                    onChange={(event) => {
                      if (!event.target.checked)
                        setValue("windows.holdout", null);
                      else {
                        const validation = getValues("windows.validation");
                        const midpoint =
                          (Date.parse(validation.start) +
                            Date.parse(validation.end)) /
                          2;
                        const middle = Number.isFinite(midpoint)
                          ? new Date(midpoint).toISOString().slice(0, 10)
                          : "";
                        setValue("windows.validation.end", middle);
                        setValue("windows.holdout", {
                          start: middle,
                          end: validation.end,
                        });
                      }
                    }}
                  />
                  Reserve a final holdout interval (metadata only)
                </label>
                {body.windows.holdout && windowFields("holdout", "Holdout")}
                <p className="muted text-xs">
                  Holdout is never evaluated or used to choose candidates here.
                  Reserving dates does not certify they were never seen or used
                  elsewhere, and does not reset experiment-family exposure.
                </p>
                {number(
                  "warmup.sessions",
                  "Warm-up sessions per listing",
                  sourceCompat ? 0 : body.task.endsWith("_time") ? 20 : 3,
                  252,
                  sourceCompat
                    ? "Source compatibility fixes zero pre-start history."
                    : "Previous available sessions, not calendar days. Server preflight checks actual rows for each independent yearly training and validation window.",
                  1,
                  sourceCompat,
                )}
                <Metadata
                  values={{
                    "History policy": body.warmup.history_policy,
                    "Lookback contract": body.warmup.lookback_contract,
                    "Engine history depth": body.market_profile.history_depth,
                  }}
                />
              </>
            )}
            {step === 2 && (
              <>
                <p className="notice">
                  Fees are fractional rates: 0.0005 means 0.05%, not 5%. These
                  are historical demonstration assumptions, not current broker
                  terms. Market volume units and order lot sizes are different
                  contracts.
                </p>
                <Metadata
                  values={{
                    "Market / currency": `${body.market_profile.market} / ${body.market_profile.currency}`,
                    Timezone:
                      body.market_profile.timezone ??
                      "Not passed: source engine default",
                    "Order lot size":
                      body.market_profile.lot_size ??
                      "Not passed: source engine default",
                    Calendar: body.market_profile.calendar,
                    "Calendar evidence": body.market_profile.calendar_note,
                    "T+1 constraint": body.market_profile.t_plus_one,
                    "Account / short selling":
                      body.market_profile.risk_config ||
                      "Engine default; CN shorting not certified",
                    "Decision timing": body.market_profile.decision_timing,
                    "History depth": body.market_profile.history_depth,
                    Assumptions: body.market_profile.assumptions,
                    Benchmark: "None; benchmark computation unavailable",
                  }}
                />
                {sourceCompat && (
                  <p className="notice">
                    Source compatibility locks capital, fees, slippage and
                    market rules. Select corrected configuration to adjust
                    implemented cost parameters.
                  </p>
                )}
                <div className="form-row">
                  {number(
                    "market_profile.initial_cash",
                    `Initial capital · ${body.market_profile.currency}`,
                    0.01,
                    1e12,
                    undefined,
                    0.01,
                    sourceCompat,
                  )}
                  {number(
                    "market_profile.commission_rate",
                    "Commission · fraction",
                    0,
                    0.1,
                    "0.0005 = 0.05%",
                    0.0001,
                    sourceCompat,
                  )}
                </div>
                <div className="form-row">
                  {number(
                    "market_profile.stamp_tax_rate",
                    "Stamp tax · fraction",
                    0,
                    0.1,
                    undefined,
                    0.0001,
                    sourceCompat,
                  )}
                  {number(
                    "market_profile.slippage.value",
                    "Slippage · fraction (percent model)",
                    0,
                    0.1,
                    undefined,
                    0.0001,
                    sourceCompat,
                  )}
                </div>
                <h3>Optimizer and sampling</h3>
                <p className="muted text-xs">
                  trade_tgd only. Maximum iterations 0 means a configured
                  seed-only research run, not legacy /runs/new.
                </p>
                <div className="form-row">
                  {number(
                    "optimizer.max_iterations",
                    "Maximum iterations",
                    0,
                    1000,
                  )}
                  {number("optimizer.sample_size", "Sample size", 1, 100)}
                </div>
                {field(
                  "optimizer.sample_method",
                  "Sampling method",
                  <select
                    {...register("optimizer.sample_method")}
                    {...attrs("optimizer.sample_method")}
                  >
                    {(capabilities.sample_methods || []).map((method) => (
                      <option value={method} key={method}>
                        {method.replaceAll("_", " ")}
                      </option>
                    ))}
                  </select>,
                )}
                <div className="form-row">
                  {number(
                    "optimizer.large_revision_prob",
                    "Large revision probability",
                    0,
                    1,
                    "Probability from 0 to 1.",
                    0.01,
                  )}
                  {number(
                    "optimizer.worst_ratio",
                    "Worst-window sampling ratio",
                    0.001,
                    1,
                    "Fraction greater than 0 and at most 1.",
                    0.01,
                  )}
                </div>
                <div className="form-row">
                  {number("optimizer.memory_window", "Memory window", 1, 1000)}
                  {number("optimizer.seed", "Random seed", 0, 2 ** 32 - 1)}
                </div>
                <h3>Objective · fixed implemented definition</h3>
                <p className="muted text-xs">
                  Source reward uses annualized return reference 20 percentage
                  points and drawdown reference 15 percentage points, AR weight
                  0.5 and frequency sharpness 1. Source metric rounding occurs
                  before reward. These are fixed, not silently redefined for
                  display.
                </p>
                <JsonDetails
                  title="Objective, metric and frequency definitions"
                  value={body.objective}
                />
                <h3>Model and budgets</h3>
                {field(
                  "model.profile_id",
                  "Model profile",
                  <select
                    value={selectedModelProfile?.id || ""}
                    {...attrs("model.profile_id")}
                    onChange={(event) => {
                      const selected = modelProfiles.find(
                        (profile) => profile.id === event.target.value,
                      );
                      if (selected)
                        setValue("model", structuredClone(selected.config));
                    }}
                  >
                    <option value="">Choose an available model profile</option>
                    {modelProfiles.map((profile) => (
                      <option key={profile.id} value={profile.id}>
                        {profile.label} ·{" "}
                        {profile.synthetic
                          ? profile.config.provider === "fixture"
                            ? "fixture / synthetic"
                            : "operator / synthetic"
                          : "operator / paid calls"}{" "}
                        · {profile.config.provider} / {profile.config.model}
                      </option>
                    ))}
                  </select>,
                  "Exact owner-available server profiles only. Provider, model, temperature and output limit are pinned together; no browser credentials enter configuration.",
                )}
                <Metadata
                  values={{
                    Provider: body.model.provider,
                    Model: body.model.model,
                    Temperature: body.model.temperature,
                    "Maximum output tokens per call": modelOutputLimit,
                    "Model profile SHA-256": modelProfileHash,
                    "Request timeout · seconds":
                      selectedModelProfile?.request_timeout_seconds ??
                      "Unavailable",
                    "Admitted strategy modes":
                      selectedModelProfile?.strategy_modes.join(", ") ??
                      "Unavailable",
                    "Credential reference":
                      "None: no browser credentials enter configuration",
                    "Prompt version": body.prompt_version,
                  }}
                />
                <ModelProfileNotice profile={selectedModelProfile} />
                <div className="form-row">
                  {number(
                    "budgets.max_model_calls",
                    "Maximum model calls",
                    0,
                    10000,
                  )}
                  {number("budgets.max_tokens", "Maximum tokens", 0, 100000000)}
                </div>
                <div className="form-row">
                  {number(
                    "budgets.max_runtime_seconds",
                    "Maximum runtime · seconds",
                    1,
                    86400,
                  )}
                  {number(
                    "budgets.max_repairs",
                    "Maximum repair attempts",
                    0,
                    20,
                  )}
                </div>
                {field(
                  "environment_digest",
                  "Deployment environment digest",
                  <Input
                    value={body.environment_digest}
                    readOnly
                    {...attrs("environment_digest")}
                  />,
                  "Authoritative server capability digest, not a user-entered image name or credential.",
                )}
                {body.environment_digest !==
                  capabilities.environment_digest && (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() =>
                      setValue(
                        "environment_digest",
                        capabilities.environment_digest,
                      )
                    }
                  >
                    Use current deployment digest and invalidate preflight
                  </Button>
                )}
              </>
            )}
            {step === 3 && (
              <>
                <Metadata
                  values={{
                    Task: taskLabels[body.task],
                    Profile: body.profile,
                    Dataset: `${dataset?.name || "Missing"} · ${body.dataset.id}`,
                    "Dataset SHA-256": body.dataset.sha256,
                    Universe: `${universe?.name || "Missing"} · ${body.universe.id}`,
                    "Universe SHA-256": body.universe.sha256,
                    "Seed / mode": `${seed?.name || "Missing"} · native_akquant · ${body.seed_strategy.id}`,
                    "Seed SHA-256": body.seed_strategy.sha256,
                    "Training [start, end)": `${body.windows.train.start} → ${body.windows.train.end}`,
                    "Validation [start, end)": `${body.windows.validation.start} → ${body.windows.validation.end}`,
                    "Holdout [start, end)": body.windows.holdout
                      ? `${body.windows.holdout.start} → ${body.windows.holdout.end} (reserved only)`
                      : "Not reserved",
                    "Warm-up sessions": body.warmup.sessions,
                    "Maximum iterations": body.optimizer.max_iterations,
                    "Model profile":
                      selectedModelProfile?.label || "Unavailable or changed",
                    "Model provider / model": `${body.model.provider} / ${body.model.model}`,
                    "Model profile SHA-256": modelProfileHash,
                    "Model temperature": body.model.temperature,
                    "Maximum output tokens per call": modelOutputLimit,
                    "Initial capital": `${body.market_profile.initial_cash} ${body.market_profile.currency}`,
                    "Environment digest": body.environment_digest,
                  }}
                />
                <ModelProfileNotice profile={selectedModelProfile} />
                <p className="notice">
                  Preflight verifies exact owned references, actual available
                  history, fields, coverage and snapshot semantics. It allocates
                  no run and calls no model. Final submission repeats
                  validation; quotas and deployment state can change.
                </p>
                <JsonDetails
                  title="Review every immutable RunConfig field"
                  value={body}
                />
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => void checkPreflight()}
                  disabled={!!busy}
                >
                  {busy === "preflight" ? (
                    <LoaderCircle className="spinning" />
                  ) : (
                    <Check size={16} />
                  )}
                  Run server preflight
                </Button>
                {matchingPreflight(body, preflight) && preflight && (
                  <div
                    className="form"
                    role="status"
                    data-testid="research-preflight-success"
                  >
                    <p className="notice">
                      Preflight passed for this exact configuration. Config
                      SHA-256: {preflight.result.config_sha256}. Dataset
                      classification:{" "}
                      {preflight.result.is_synthetic
                        ? "synthetic / fixture"
                        : "provider data; inspect snapshot provenance"}
                      .
                    </p>
                    {preflight.result.warnings.map((warning, index) => (
                      <p className="notice" key={index}>
                        {warning}
                      </p>
                    ))}
                    <JsonDetails
                      title="Actual independent evaluation windows"
                      value={preflight.result.evaluation_windows}
                    />
                  </div>
                )}
              </>
            )}
            {issues.length > 0 && (
              <div className="notice error" role="alert">
                <div>
                  <p>Correct these fields before continuing:</p>
                  <ul>
                    {issues.map((issue, index) => (
                      <li key={`${issue.path}-${index}`}>
                        <a href={`#${issue.path}`}>{issue.message}</a>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            )}
            {Boolean(failure) && <ErrorState error={failure} />}
            <div className="form-footer">
              <div className="flex gap-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    persist(getValues());
                    void navigate({ to: "/runs" });
                  }}
                >
                  Cancel · keep draft
                </Button>
                {step > 0 && (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => move(step - 1)}
                  >
                    Back
                  </Button>
                )}
              </div>
              {step < 3 ? (
                <Button type="submit">Continue</Button>
              ) : (
                <Button
                  type="submit"
                  disabled={
                    !!busy ||
                    !selectedModelProfile ||
                    !matchingPreflight(body, preflight)
                  }
                >
                  {busy === "submit" ? (
                    <LoaderCircle className="spinning" />
                  ) : (
                    <Play size={15} />
                  )}
                  Submit experiment
                </Button>
              )}
            </div>
          </fieldset>
        </form>
      </Panel>
      <p className="muted text-xs">
        The separate legacy{" "}
        <Link className="text-link" to="/runs/new">
          fixed-seed evaluator
        </Link>{" "}
        does not configure optimization. This wizard saves owner-scoped inputs
        in this browser tab, never API credentials.
      </p>
    </div>
  );
}
