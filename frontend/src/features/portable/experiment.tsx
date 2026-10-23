import { BenchmarkSelector } from "@/components/benchmark-selector";
import { benchmarkSelectionIssue } from "@/lib/benchmarks";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type {
  PortableDefaultRequest,
  PortableStrategyPublic,
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
import { asRecord, snapshotLabel, universeLabels } from "@/lib/data-utils";
import {
  researchModelProfiles,
  researchModelProfileFor,
} from "@/lib/research-form";
import {
  createPortableStrategy,
  defaultPortableConfig,
  portableCatalogOptions,
  portableRunsOptions,
  preflightPortable,
  refreshPortable,
  submitPortable,
  PORTABLE_PAGE_SIZE,
  type PortableCatalog,
} from "@/lib/portable-api";
import {
  loadPortableDraft,
  portablePreflightMatches,
  savePortableDraft,
  usePortableDraft,
  type PortablePreflight,
} from "./durable";
import {
  portableDataNotice,
  portableFingerprint,
  portableFormSchema,
  portableIssues,
  portableNotice,
  portableRequest,
  portableUniverses,
  type PortableForm,
} from "./protocol";

export function PortableExperimentPage() {
  const user = useQuery(currentUserOptions);
  return (
    <>
      <PageHeader
        eyebrow="PORTABLE RESEARCH"
        title="Configure portable research"
        subtitle="A distinct policy protocol with pinned inputs, explicit state and a separate execution ledger."
      />
      <p className="notice" data-testid="portable-trust">
        {portableNotice}
      </p>
      {user.isPending ? (
        <Loading text="Verifying workspace owner…" />
      ) : user.error ? (
        <ErrorState error={user.error} retry={() => void user.refetch()} />
      ) : user.data ? (
        <PortableWorkspace key={user.data.id} owner={user.data.id} />
      ) : null}
    </>
  );
}
function PortableWorkspace({ owner }: { owner: string }) {
  const catalog = useQuery(portableCatalogOptions(owner));
  const [initial, setInitial] = useState<PortableForm | null>(
    () => loadPortableDraft(owner)?.body ?? null,
  );
  const [offset, setOffset] = useState(0);
  const runs = useQuery(portableRunsOptions(owner, offset));
  return (
    <div className="stack mt-6">
      <p className="notice">{portableDataNotice}</p>
      <p className="notice">
        For the synthetic path, explicitly import the synthetic portable_fixture
        source in raw mode. It supports CN 600000/000001 and US AAPL/MSFT within
        [2024-01-02, 2025-01-01). Its no-actions evidence is true by
        construction, not evidence about real securities. Publish a matching
        user-fixed universe version before continuing.
      </p>
      {catalog.isPending ? (
        <Loading text="Loading immutable portable inputs…" />
      ) : catalog.error ? (
        <ErrorState
          error={catalog.error}
          retry={() => void catalog.refetch()}
        />
      ) : catalog.data ? (
        initial ? (
          <PortableConfigForm
            key={`${initial.seed_strategy.id}:${initial.environment_digest}`}
            owner={owner}
            initial={initial}
            catalog={catalog.data}
            onChangeInputs={() => setInitial(null)}
          />
        ) : (
          <PortableInputPicker
            owner={owner}
            catalog={catalog.data}
            onConfigured={(body) => {
              savePortableDraft(owner, { version: 1, body, pending: null });
              setInitial(body);
            }}
          />
        )
      ) : null}
      <Panel title="Your portable runs">
        {runs.isPending ? (
          <Loading text="Loading portable runs…" />
        ) : runs.error ? (
          <ErrorState error={runs.error} retry={() => void runs.refetch()} />
        ) : runs.data ? (
          <>
            <div className="stack">
              {runs.data.data.map((run) => (
                <div
                  className="flex items-center justify-between gap-4"
                  key={run.id}
                >
                  <Link
                    to="/portable/runs/$runId"
                    params={{ runId: run.id }}
                    className="text-link"
                  >
                    {run.config.strategy_kind} · {run.config.task} · {run.id}
                  </Link>
                  <Status status={run.status} />
                </div>
              ))}
              {!runs.data.data.length && (
                <p className="empty">
                  No portable runs yet. Native runs remain separate.
                </p>
              )}
            </div>
            <Pagination
              offset={offset}
              limit={PORTABLE_PAGE_SIZE}
              count={runs.data.count}
              busy={runs.isFetching}
              onPage={setOffset}
            />
          </>
        ) : null}
      </Panel>
    </div>
  );
}
const refFor = (item: { id: string; content_sha256: string }) => ({
  id: item.id,
  sha256: item.content_sha256,
});
function initialWindows(start: string, end: string): PortableForm["windows"] {
  const first = Date.parse(start),
    days = Math.floor((Date.parse(end) - first) / 86400000);
  const day = (offset: number) =>
    new Date(first + offset * 86400000).toISOString().slice(0, 10);
  const reserve = Math.min(14, Math.max(1, Math.floor(days / 4)));
  return {
    train: {
      start: day(reserve),
      end: day(Math.max(reserve + 1, Math.floor(days * 0.7))),
    },
    validation: {
      start: day(Math.max(reserve + 1, Math.floor(days * 0.7))),
      end,
    },
    holdout: null,
  };
}
export function PortableInputPicker({
  owner,
  catalog,
  onConfigured,
}: {
  owner: string;
  catalog: PortableCatalog;
  onConfigured: (body: PortableForm) => void;
}) {
  const [market, setMarket] = useState<"CN" | "US">("CN"),
    [kind, setKind] = useState<"portfolio" | "event">("portfolio");
  const [datasetId, setDatasetId] = useState(""),
    [universeId, setUniverseId] = useState(""),
    [seedId, setSeedId] = useState("");
  const [executionMode, setExecutionMode] = useState<
    "optimization" | "seed_evaluation"
  >("optimization");
  const [created, setCreated] = useState<PortableStrategyPublic | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [uncertainCreate, setUncertainCreate] = useState(false);
  const active = useRef(true),
    lock = useRef(false);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  const dataset = catalog.datasets.find(
    (item) => item.id === datasetId && item.market === market,
  );
  const universes = portableUniverses(dataset, catalog.universes),
    universe = universes.find((item) => item.id === universeId);
  const task = `${market.toLowerCase()}_${kind === "portfolio" ? "cross" : "time"}`;
  const strategies = [
    ...catalog.strategies,
    ...(created && !catalog.strategies.some((item) => item.id === created.id)
      ? [created]
      : []),
  ].filter(
    (item) =>
      item.task === task &&
      asRecord(item.manifest.universe).snapshot_sha256 ===
        universe?.content_sha256,
  );
  const seed = strategies.find((item) => item.id === seedId),
    template = catalog.templates.find((item) => item.strategy_kind === kind);
  async function perform(action: "seed" | "defaults") {
    if (lock.current || !dataset || !universe || !active.current) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    try {
      if (action === "seed") {
        const item = await createPortableStrategy({
          name: `${market} portable ${kind === "portfolio" ? "momentum" : "trend"} reference`,
          strategy_kind: kind,
          dataset: refFor(dataset),
          universe: refFor(universe),
          initial_cash: 1000000,
        });
        if (active.current) {
          setCreated(item);
          setSeedId(item.id);
          setUncertainCreate(false);
          void refreshPortable(owner);
        }
      } else if (seed) {
        const request: PortableDefaultRequest = {
          dataset: refFor(dataset),
          universe: refFor(universe),
          seed_strategy: refFor(seed),
          windows: initialWindows(dataset.start_date, dataset.end_date),
          execution_mode: executionMode,
        };
        const result = await defaultPortableConfig(request);
        const parsed = portableFormSchema.safeParse(result);
        if (!parsed.success)
          throw new Error(
            "The server returned unsupported portable defaults. Reload the catalog; no configuration was accepted.",
          );
        if (active.current) onConfigured(parsed.data);
      }
    } catch (failure) {
      if (active.current) {
        setError(failure);
        if (action === "seed") setUncertainCreate(true);
      }
    } finally {
      lock.current = false;
      if (active.current) setBusy(false);
    }
  }
  return (
    <Panel title="1 · Explicit portable inputs">
      <fieldset disabled={busy} className="form-grid">
        <Field id="portable-market" label="Market">
          <select
            id="portable-market"
            value={market}
            onChange={(e) => {
              setMarket(e.target.value as typeof market);
              setDatasetId("");
              setUniverseId("");
              setSeedId("");
            }}
          >
            <option value="CN">China · CNY · XSHG</option>
            <option value="US">United States · USD · XNYS</option>
          </select>
        </Field>
        <Field id="portable-kind" label="Portable template">
          <select
            id="portable-kind"
            value={kind}
            onChange={(e) => {
              setKind(e.target.value as typeof kind);
              setSeedId("");
            }}
          >
            <option value="portfolio">
              PortfolioPolicy · momentum portfolio
            </option>
            <option value="event">EventPolicy · trend event policy</option>
          </select>
        </Field>
        <Field id="portable-dataset" label="Published dataset version">
          <select
            id="portable-dataset"
            value={datasetId}
            onChange={(e) => {
              setDatasetId(e.target.value);
              setUniverseId("");
              setSeedId("");
            }}
          >
            <option value="">Choose an immutable snapshot</option>
            {catalog.datasets
              .filter((item) => item.market === market)
              .map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name} · {snapshotLabel(item)} · {item.id}
                </option>
              ))}
          </select>
        </Field>
        <Field id="portable-universe" label="Exact universe version">
          <select
            id="portable-universe"
            value={universeId}
            onChange={(e) => {
              setUniverseId(e.target.value);
              setSeedId("");
            }}
          >
            <option value="">Choose matching pinned membership</option>
            {universes.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} · {universeLabels[item.state]} · {item.id}
              </option>
            ))}
          </select>
        </Field>
        <Field id="portable-seed" label="Portable seed version">
          <select
            id="portable-seed"
            value={seedId}
            onChange={(e) => setSeedId(e.target.value)}
          >
            <option value="">Choose a published portable seed</option>
            {strategies.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} · {item.id}
              </option>
            ))}
          </select>
        </Field>
        <Field id="portable-execution" label="Research execution">
          <select
            id="portable-execution"
            value={executionMode}
            onChange={(e) =>
              setExecutionMode(e.target.value as typeof executionMode)
            }
          >
            <option value="optimization">Bounded model optimization</option>
            <option value="seed_evaluation">
              Seed-only evaluation · no optimization
            </option>
          </select>
        </Field>
      </fieldset>
      <p className="notice mt-4">
        No native strategy is converted automatically. Create a new reviewed
        portable reference version for the selected dataset, universe and
        market. Portfolio decisions await the complete snapshot; event decisions
        emit explicit intents. Daily decisions occur after close, with strictly
        later NextOpen execution.
      </p>
      {!catalog.datasets.some((item) => item.market === market) && (
        <p className="notice mt-4">
          No published dataset is available for this market. Import the
          explicitly synthetic portable raw/no-actions fixture through Data
          first. Live-provider coverage is not inferred.
        </p>
      )}
      {uncertainCreate && (
        <p className="notice mt-4">
          Template creation may have completed even though its response was
          lost. Reload the catalog and check published versions before creating
          another. Template creation is not automatically retried.
        </p>
      )}
      {Boolean(error) && <ErrorState error={error} />}
      <div className="actions mt-4">
        <Button
          variant="outline"
          disabled={busy || !dataset || !universe || uncertainCreate}
          onClick={() => void perform("seed")}
        >
          Create reference template version
        </Button>
        {uncertainCreate && (
          <Button
            variant="outline"
            disabled={busy}
            onClick={async () => {
              if (lock.current) return;
              lock.current = true;
              setBusy(true);
              try {
                await refreshPortable(owner);
                if (active.current) setUncertainCreate(false);
              } finally {
                lock.current = false;
                if (active.current) setBusy(false);
              }
            }}
          >
            Reload published versions
          </Button>
        )}
        <Button
          disabled={busy || !seed || !dataset || !universe}
          onClick={() => void perform("defaults")}
        >
          {busy ? "Loading…" : "Load pinned portable configuration"}
        </Button>
      </div>
      {dataset && (
        <Metadata
          values={{
            "Dataset ID": dataset.id,
            "Dataset SHA-256": dataset.content_sha256,
            "Dataset evidence": snapshotLabel(dataset),
            "Universe ID": universe?.id,
            "Universe SHA-256": universe?.content_sha256,
            "Seed code SHA-256": seed?.content_sha256,
          }}
        />
      )}
      {template && (
        <JsonDetails
          title="Reviewed portable template and interface limits"
          value={template}
        />
      )}
      <Link to="/datasets" className="text-link mt-4">
        Inspect published data and provenance
      </Link>
    </Panel>
  );
}

export function PortableConfigForm({
  owner,
  initial,
  catalog,
  onChangeInputs,
}: {
  owner: string;
  initial: PortableForm;
  catalog: PortableCatalog;
  onChangeInputs: () => void;
}) {
  const navigate = useNavigate(),
    draft = usePortableDraft(owner, initial),
    body = draft.body;
  const [preflight, setPreflight] = useState<PortablePreflight | null>(null),
    [checking, setChecking] = useState(false),
    [failure, setFailure] = useState<unknown>(null),
    [accepted, setAccepted] = useState("");
  const active = useRef(true),
    lock = useRef(false),
    current = useRef(body),
    controller = useRef<AbortController | null>(null);
  current.current = body;
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      controller.current?.abort();
    };
  }, []);
  const errors = portableIssues(body);
  const modelProfiles = researchModelProfiles(catalog.capabilities, "portable");
  const selectedModelProfile = researchModelProfileFor(
    body.model,
    catalog.capabilities,
    "portable",
  );
  if (!selectedModelProfile)
    errors.push(
      "The exact model profile is unavailable or changed for this owner. Reload and choose a currently admitted portable profile.",
    );
  const dataset = catalog.datasets.find((item) => item.id === body.dataset.id),
    universe = catalog.universes.find((item) => item.id === body.universe.id),
    seed = catalog.strategies.find((item) => item.id === body.seed_strategy.id);
  if (!dataset || dataset.content_sha256 !== body.dataset.sha256)
    errors.push(
      "The exact dataset version is unavailable. Reload the catalog or change pinned inputs.",
    );
  if (
    !universe ||
    universe.content_sha256 !== body.universe.sha256 ||
    !portableUniverses(dataset, catalog.universes).some(
      (item) => item.id === universe.id,
    )
  )
    errors.push(
      "The exact universe version or dataset membership no longer matches.",
    );
  if (
    !seed ||
    seed.content_sha256 !== body.seed_strategy.sha256 ||
    seed.task !== body.task
  )
    errors.push(
      "The exact portable seed version is unavailable for this task.",
    );
  const benchmarkError = benchmarkSelectionIssue(
    body.benchmark,
    catalog.datasets,
    body.task.slice(0, 2).toUpperCase(),
  );
  if (benchmarkError) errors.push(benchmarkError);
  function edit(next: PortableForm) {
    controller.current?.abort();
    setPreflight(null);
    setFailure(null);
    draft.edit(next);
  }
  async function check() {
    if (lock.current || errors.length || !active.current) return;
    lock.current = true;
    setChecking(true);
    setFailure(null);
    setPreflight(null);
    const checked = structuredClone(body),
      fingerprint = portableFingerprint(checked);
    controller.current = new AbortController();
    try {
      const result = await preflightPortable(
        portableRequest(checked),
        controller.current.signal,
      );
      if (
        active.current &&
        portableFingerprint(current.current) === fingerprint
      )
        setPreflight({ fingerprint, result });
    } catch (error) {
      if (
        active.current &&
        portableFingerprint(current.current) === fingerprint &&
        !controller.current.signal.aborted
      )
        setFailure(error);
    } finally {
      lock.current = false;
      if (active.current) setChecking(false);
    }
  }
  const canSubmit =
    portablePreflightMatches(body, preflight) &&
    !errors.length &&
    !checking &&
    !accepted;
  function numeric(
    group: "optimizer" | "budgets" | "warmup",
    name: string,
    title: string,
    min: number,
    max: number,
    step = 1,
  ) {
    const groupValue = body[group];
    const value = Number(groupValue[name as keyof typeof groupValue]);
    return (
      <Field
        key={`${group}.${name}`}
        id={`portable-${group}-${name}`}
        label={title}
      >
        <Input
          id={`portable-${group}-${name}`}
          type="number"
          min={min}
          max={max}
          step={step}
          value={Number.isFinite(value) ? value : ""}
          onChange={(event) => {
            const number = event.target.valueAsNumber;
            if (!Number.isFinite(number)) return;
            const next = structuredClone(body);
            Object.assign(next[group], {
              [name]: Math.min(
                max,
                Math.max(min, step === 1 ? Math.round(number) : number),
              ),
            });
            edit(next);
          }}
        />
      </Field>
    );
  }
  return (
    <div className="stack">
      <Panel
        title="2 · Exact windows and immutable evidence"
        action={
          <Button
            variant="outline"
            disabled={draft.busy || checking}
            onClick={onChangeInputs}
          >
            Change pinned inputs
          </Button>
        }
      >
        <Metadata
          values={{
            "Policy contract": body.strategy_kind,
            "Task / market": body.task,
            "Dataset version": body.dataset.id,
            "Dataset SHA-256": body.dataset.sha256,
            "Universe version": body.universe.id,
            "Universe SHA-256": body.universe.sha256,
            "Seed version": body.seed_strategy.id,
            "Seed code SHA-256": body.seed_strategy.sha256,
            "Seed manifest SHA-256": body.seed_manifest.sha256,
            "Seed parameters SHA-256": body.seed_parameters.sha256,
            "Deployment environment SHA-256": body.environment_digest,
          }}
        />
        <p className="notice mt-4">
          All windows are [start, end). Calendar-date suggestions are not
          session certification. Warm-up updates state without orders and must
          precede every evaluation window. Holdout dates are metadata only and
          are never executed or used as optimization feedback here.
        </p>
        <fieldset
          disabled={draft.busy || checking || Boolean(accepted)}
          className="form-grid mt-4"
        >
          {(["train", "validation", "holdout"] as const).map((name) => {
            const window = body.windows[name];
            return (
              <div key={name} className="stack">
                <label>
                  {name !== "train" && (
                    <input
                      type="checkbox"
                      checked={Boolean(window)}
                      onChange={(event) => {
                        const next = structuredClone(body);
                        next.windows[name] = event.target.checked
                          ? { start: "", end: "" }
                          : null;
                        edit(next);
                      }}
                    />
                  )}{" "}
                  {name === "holdout"
                    ? "Metadata-only holdout"
                    : name === "train"
                      ? "Training window"
                      : "Validation window"}
                </label>
                {window &&
                  (["start", "end"] as const).map((edge) => (
                    <Field
                      key={edge}
                      id={`portable-${name}-${edge}`}
                      label={`${name} ${edge} · ${edge === "start" ? "inclusive" : "exclusive"}`}
                    >
                      <Input
                        id={`portable-${name}-${edge}`}
                        type="date"
                        value={window[edge]}
                        onChange={(event) => {
                          const next = structuredClone(body);
                          next.windows[name]![edge] = event.target.value;
                          edit(next);
                        }}
                      />
                    </Field>
                  ))}
              </div>
            );
          })}
          {numeric(
            "warmup",
            "sessions",
            "Warm-up exchange sessions per listing",
            1,
            252,
          )}
        </fieldset>
      </Panel>
      <Panel title="Optional immutable benchmark">
        <BenchmarkSelector
          value={body.benchmark}
          datasets={catalog.datasets}
          market={body.task.slice(0, 2).toUpperCase()}
          disabled={checking || draft.busy}
          onChange={(benchmark) =>
            edit({ ...body, benchmark: benchmark as PortableForm["benchmark"] })
          }
        />
      </Panel>
      <Panel title="3 · Model profile, optimizer and bounded budgets">
        <p className="notice">
          Fixture is the default and never makes network calls. Operator
          profiles must be enabled and assigned by the deployment operator. No
          browser credentials enter configuration. This portable objective is
          total_return_pct − max_drawdown_pct, not upstream-paper protocol
          equivalence.
        </p>
        {body.model.provider !== "fixture" && (
          <p className="notice" data-testid="portable-model-notice">
            {selectedModelProfile?.synthetic
              ? "Synthetic operator HTTP profile, not live-model or quality evidence."
              : "Submitting with this operator profile authorizes model requests within the selected call, token and time limits; the provider may bill them."}{" "}
            Unknown usage consumes a conservative token reservation. Cost is
            unavailable, not zero. Failed or uncertain requests are not
            automatically retried. Cancellation waits for confirmed local
            model-process and policy/engine stops; it cannot undo provider
            processing or charges.
          </p>
        )}
        <fieldset
          disabled={draft.busy || checking || Boolean(accepted)}
          className="form-grid mt-4"
        >
          <Field id="portable-model-profile" label="Model profile">
            <select
              id="portable-model-profile"
              value={selectedModelProfile?.id ?? ""}
              onChange={(event) => {
                const selected = modelProfiles.find(
                  (profile) => profile.id === event.target.value,
                );
                if (selected)
                  edit({ ...body, model: structuredClone(selected.config) });
              }}
            >
              <option value="">Choose an admitted profile</option>
              {modelProfiles.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.label} ·{" "}
                  {profile.synthetic ? "synthetic" : "operator / may bill"} ·{" "}
                  {profile.config.provider} / {profile.config.model}
                </option>
              ))}
            </select>
          </Field>
          <Field id="portable-mode" label="Execution mode">
            <select
              id="portable-mode"
              value={body.execution_mode}
              onChange={(event) => {
                const next = structuredClone(body);
                next.execution_mode = event.target
                  .value as typeof body.execution_mode;
                if (next.execution_mode === "seed_evaluation")
                  next.optimizer.max_iterations = 0;
                edit(next);
              }}
            >
              <option value="optimization">Bounded optimization</option>
              <option value="seed_evaluation">Seed evaluation</option>
            </select>
          </Field>
          {numeric(
            "optimizer",
            "max_iterations",
            "Maximum iterations",
            0,
            1000,
          )}
          {numeric("optimizer", "sample_size", "Candidate sample size", 1, 100)}
          <Field id="portable-sample-method" label="Sampling method">
            <select
              id="portable-sample-method"
              value={body.optimizer.sample_method}
              onChange={(event) =>
                edit({
                  ...body,
                  optimizer: {
                    ...body.optimizer,
                    sample_method: event.target
                      .value as PortableForm["optimizer"]["sample_method"],
                  },
                })
              }
            >
              {["power_law", "uniform", "top_k"].map((method) => (
                <option key={method}>{method}</option>
              ))}
            </select>
          </Field>
          {numeric(
            "optimizer",
            "large_revision_prob",
            "Large revision probability",
            0,
            1,
            0.01,
          )}
          {numeric(
            "optimizer",
            "memory_window",
            "Optimizer memory window",
            1,
            1000,
          )}
          {numeric(
            "optimizer",
            "seed",
            "Optimizer random seed",
            0,
            2 ** 32 - 1,
          )}
          {numeric(
            "optimizer",
            "worst_ratio",
            "Worst-candidate ratio",
            0.01,
            1,
            0.01,
          )}
          {numeric(
            "budgets",
            "max_model_calls",
            "Maximum model calls",
            0,
            10000,
          )}
          {numeric(
            "budgets",
            "max_tokens",
            "Maximum charged or reserved tokens",
            0,
            100000000,
          )}
          {numeric(
            "budgets",
            "max_runtime_seconds",
            "Maximum runtime · seconds",
            1,
            86400,
          )}
          {numeric("budgets", "max_repairs", "Maximum repair attempts", 0, 20)}
        </fieldset>
        <JsonDetails
          title="Pinned engine, timing, state and model contracts"
          value={{
            engine_profile: body.engine_profile,
            initial_state: body.initial_state,
            model: body.model,
            objective: body.objective,
            prompt_version: body.prompt_version,
          }}
        />
      </Panel>
      <Panel title="4 · Strict portable preflight and durable submission">
        {errors.length > 0 && (
          <ul className="notice error" role="alert">
            {errors.map((error, index) => (
              <li key={index}>{error}</li>
            ))}
          </ul>
        )}
        {draft.storageWarning && (
          <p className="notice error">
            Tab storage is unavailable or this draft is invalid. Submission is
            blocked unless the exact body and retry key can be saved.
          </p>
        )}
        {draft.hasPending && (
          <p className="notice">
            A submission may already have been accepted. Recheck these unchanged
            inputs and retry with the original immutable body and key; do not
            create a replacement merely because a response was lost.
          </p>
        )}
        {Boolean(failure || draft.error) && (
          <ErrorState error={failure || draft.error} />
        )}
        {preflight && portablePreflightMatches(body, preflight) && (
          <div className="mt-4" data-testid="portable-preflight">
            <p className="notice">
              Portable preflight passed for this exact configuration. Runtime
              certification remains unverified.
            </p>
            <Metadata
              values={{
                "Config SHA-256": preflight.result.config_sha256,
                "Data evidence": preflight.result.is_synthetic
                  ? "Explicit synthetic fixture, not investment evidence"
                  : "Non-synthetic; inspect provenance",
                "Holdout execution": preflight.result.holdout_execution,
              }}
            />
            <JsonDetails
              title="Exact admitted training and validation windows"
              value={preflight.result.evaluation_windows}
            />
            {preflight.result.warnings.map((warning, index) => (
              <p className="notice mt-3" key={index}>
                {warning}
              </p>
            ))}
          </div>
        )}
        <div className="actions mt-4">
          <Button
            variant="outline"
            disabled={
              checking ||
              draft.busy ||
              Boolean(accepted) ||
              errors.length > 0 ||
              !portableFormSchema.safeParse(body).success
            }
            onClick={() => void check()}
          >
            {checking
              ? "Checking raw/action/calendar evidence…"
              : "Run strict portable preflight"}
          </Button>
          <Button
            disabled={!canSubmit || draft.busy}
            onClick={() => {
              if (!canSubmit) return;
              void draft.submit(
                (value, key) => submitPortable(portableRequest(value), key),
                (run) => {
                  setAccepted(run.id);
                  void refreshPortable(owner);
                  void navigate({
                    to: "/portable/runs/$runId",
                    params: { runId: run.id },
                  });
                },
              );
            }}
          >
            {draft.busy
              ? "Submitting…"
              : draft.hasPending
                ? "Retry same portable submission"
                : "Submit portable experiment"}
          </Button>
        </div>
        {accepted && (
          <Link
            to="/portable/runs/$runId"
            params={{ runId: accepted }}
            className="text-link"
          >
            Open accepted portable run
          </Link>
        )}
        <JsonDetails
          title="Review every immutable portable configuration field"
          value={body}
        />
      </Panel>
    </div>
  );
}
