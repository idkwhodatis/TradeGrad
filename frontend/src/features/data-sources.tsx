import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Database, Search } from "lucide-react";
import type {
  Capability,
  ImportCreate,
  ProviderPublic,
  ProviderConnectionUpdate,
  ResolveCreate,
} from "@/client/types.gen";
import * as sdk from "@/client/sdk.gen";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/common";
import {
  DataNotice,
  Field,
  JsonDetails,
  Metadata,
} from "@/components/data-common";
import { DataTable } from "@/components/data-table";
import { currentUserOptions, queryClient } from "@/lib/api";
import {
  importOptions,
  listingsOptions,
  providersOptions,
  universesOptions,
} from "@/lib/data-api";
import {
  asRecord,
  dateWindowError,
  parseSymbols,
  universeLabels,
} from "@/lib/data-utils";
import { useDataAction } from "@/lib/use-data-action";
import {
  providerSubmission,
  hasPendingProviderSubmission,
  clearProviderSubmission,
  connectionAdmissionRejected,
} from "@/lib/provider-submission";

async function submitProviderJob<
  T extends { expected_connection_revision?: number | null },
>(
  owner: string,
  operation: string,
  body: T,
  submit: (body: T, key: string) => Promise<{ id: string }>,
) {
  const frozen = providerSubmission(owner, operation, body);
  try {
    const result = await submit(frozen.body, frozen.key);
    clearProviderSubmission(owner, operation);
    return result;
  } catch (error) {
    if (connectionAdmissionRejected(error)) {
      clearProviderSubmission(owner, operation);
      await queryClient.invalidateQueries({ queryKey: ["data-providers"] });
    }
    throw error;
  }
}

export function DataSourcesPage() {
  const providers = useQuery(providersOptions);
  const user = useQuery(currentUserOptions);
  return (
    <>
      <PageHeader
        title="Data sources"
        subtitle="Resolve a security, inspect its venue, then publish an immutable daily snapshot."
      >
        <Button asChild variant="outline">
          <Link to="/imports">Import history</Link>
        </Button>
      </PageHeader>
      <DataNotice />
      <div className="mt-6">
        {providers.isPending || user.isPending ? (
          <Loading />
        ) : providers.error || user.error ? (
          <ErrorState
            error={providers.error || user.error}
            retry={() => {
              void providers.refetch();
              void user.refetch();
            }}
          />
        ) : user.data && providers.data ? (
          <>
            <div className="catalog mb-6">
              {providers.data.map((provider) => (
                <ProviderCard
                  key={provider.id}
                  provider={provider}
                  owner={user.data.id}
                />
              ))}
            </div>
            <SourceForm providers={providers.data} owner={user.data.id} />
          </>
        ) : null}
      </div>
    </>
  );
}
function ProviderCard({
  provider,
  owner,
}: {
  provider: ProviderPublic;
  owner: string;
}) {
  return (
    <article
      className="panel panel-body"
      aria-label={`${provider.name} provider`}
    >
      <div className="flex items-center justify-between gap-3">
        <h2>{provider.name}</h2>
        <Badge variant="outline">
          {provider.runtime_mode === "fixture"
            ? "Fixture transport · not live"
            : "Live transport"}
        </Badge>
      </div>
      <p className="text-xs muted mt-2">
        {provider.upstream} · {provider.markets.join(", ")} · daily bars
      </p>
      {provider.fixture_notice && (
        <p className="notice mt-4">{provider.fixture_notice}</p>
      )}
      <p className="text-xs muted mt-4">
        Configured transport is not verification evidence. Capabilities below
        record their own tested scope.
      </p>
      <DataTable<Capability>
        label={`${provider.name} capabilities`}
        data={provider.capabilities}
        columns={[
          { accessorKey: "feature", header: "Feature" },
          { accessorKey: "status", header: "Status" },
          { accessorKey: "evidence", header: "Evidence" },
        ]}
      />
      <JsonDetails
        title="Inspect endpoints, versions and verification scope"
        value={provider.capabilities}
      />
      {provider.connection && (
        <ProviderConnectionSettings provider={provider} />
      )}
      {provider.id !== "portable_fixture" && (
        <ProviderConnectionTest provider={provider} owner={owner} />
      )}
    </article>
  );
}
function ProviderConnectionSettings({
  provider,
}: {
  provider: ProviderPublic;
}) {
  const config = provider.connection!;
  const [editingRevision, setEditingRevision] = useState(config.revision);
  const [enabled, setEnabled] = useState(config.enabled);
  const [isDefault, setDefault] = useState(config.is_default);
  const [mode, setMode] = useState<
    ProviderConnectionUpdate["default_price_mode"]
  >(config.default_price_mode);
  const action = useDataAction(async () => {
    try {
      const saved = (
        await sdk.updateProviderConnection({
          path: { provider: provider.id },
          body: {
            expected_revision: editingRevision,
            enabled,
            is_default: isDefault,
            default_price_mode: mode,
          },
        })
      ).data;
      setEditingRevision(saved.revision);
    } finally {
      // Refetch on conflicts as well; never silently overwrite another tab's edit.
      await queryClient.invalidateQueries({ queryKey: ["data-providers"] });
    }
  });
  return (
    <section className="mt-5" aria-label={`${provider.id} connection settings`}>
      <h3>My provider connection</h3>
      <p className="notice mt-3">
        Public access · public_no_auth. No credentials are required or stored.
        Model secret references remain separate in Settings. Disabling blocks
        new jobs, not already frozen jobs.
      </p>
      <p
        className="text-xs muted mt-3"
        data-testid={`${provider.id}-connection-revision`}
      >
        Revision {config.revision}
        {config.revision === 0
          ? " · Unsaved defaults; persisted on first save or use"
          : " · Saved immutable configuration"}
        .{" "}
        {config.is_default
          ? `Default for ${provider.markets.join(", ")}`
          : "Not the market default"}
      </p>
      <JsonDetails
        title="Inspect connection identity and hash"
        value={config}
      />
      <form
        className="form mt-3"
        onSubmit={(event) => {
          event.preventDefault();
          void action.run();
        }}
      >
        <label className="data-check">
          <input
            type="checkbox"
            aria-label={`${provider.name} enabled`}
            checked={enabled}
            disabled={action.pending}
            onChange={(event) => {
              setEnabled(event.target.checked);
              if (!event.target.checked) setDefault(false);
            }}
          />
          Enabled for new jobs
        </label>
        <label className="data-check">
          <input
            type="checkbox"
            aria-label={`${provider.name} market default`}
            checked={isDefault}
            disabled={action.pending || !enabled}
            onChange={(event) => setDefault(event.target.checked)}
          />
          Default for {provider.markets.join(", ")}
        </label>
        <Field
          label="Default price mode"
          id={`${provider.id}-default-price-mode`}
        >
          <select
            id={`${provider.id}-default-price-mode`}
            value={mode}
            disabled={action.pending}
            onChange={(event) =>
              setMode(
                event.target
                  .value as ProviderConnectionUpdate["default_price_mode"],
              )
            }
          >
            {[...new Set(["provider_native", ...provider.price_modes])]
              .filter((value) => value !== "repair")
              .map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
          </select>
        </Field>
        {editingRevision !== config.revision && (
          <p className="notice" role="alert">
            Connection changed since this form opened. Discard unsaved changes
            to load the latest settings before saving.
          </p>
        )}
        <Button
          type="submit"
          variant="outline"
          disabled={action.pending || editingRevision !== config.revision}
        >
          {action.pending ? "Saving connection…" : "Save connection settings"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          disabled={action.pending}
          onClick={() => {
            setEditingRevision(config.revision);
            setEnabled(config.enabled);
            setDefault(config.is_default);
            setMode(config.default_price_mode);
            action.resetError();
          }}
        >
          Discard unsaved changes
        </Button>
        {Boolean(action.error) && <ErrorState error={action.error} />}
      </form>
    </section>
  );
}

function ProviderConnectionTest({
  provider,
  owner,
}: {
  provider: ProviderPublic;
  owner: string;
}) {
  const navigate = useNavigate();
  const [symbol, setSymbol] = useState(
    provider.id === "akshare" ? "600000" : "AAPL",
  );
  const [start, setStart] = useState("2024-01-02");
  const [end, setEnd] = useState("2024-01-06");
  const probeBody = {
    symbol,
    start_date: start,
    end_date: end,
    expected_connection_revision: provider.connection?.revision,
  };
  const recoverProbe = hasPendingProviderSubmission(
    owner,
    `provider-test:${provider.id}`,
    probeBody,
  );
  const action = useDataAction(async () => {
    const body = probeBody;
    const scope = `provider-test:${provider.id}`;
    const result = await submitProviderJob(
      owner,
      scope,
      body,
      async (frozen, key) =>
        (
          await sdk.testDataProvider({
            path: { provider: provider.id },
            body: frozen,
            headers: { "Idempotency-Key": key },
          })
        ).data,
    );
    await queryClient.invalidateQueries({ queryKey: ["data-imports"] });
    await queryClient.invalidateQueries({ queryKey: ["data-providers"] });
    await navigate({
      to: "/imports/$importId",
      params: { importId: result.id },
    });
  });
  const receipt = provider.latest_test;
  return (
    <section className="mt-5" aria-label={`${provider.id} connection test`}>
      <h3>Bounded public connection test</h3>
      <p className="text-xs muted mt-2">
        No account or new credentials. Tests one symbol for at most 14 closed
        calendar days. No data-use permission or whole-provider verification is
        established.
      </p>
      {receipt ? (
        <>
          <p
            className="notice mt-3"
            data-testid={`provider-test-${provider.id}`}
          >
            Last completed test: {receipt.outcome} · {receipt.evidence} ·{" "}
            {receipt.checked_at}. Scope: {receipt.symbol} [{receipt.start_date},{" "}
            {receipt.end_date}).
          </p>
          {provider.connection &&
            receipt.provider_connection?.revision !==
              provider.connection.revision && (
              <p className="notice mt-3">
                This test predates the current connection revision. It does not
                verify these settings.
              </p>
            )}
          <JsonDetails
            title="Inspect scoped connection receipt"
            value={receipt}
          />
          <Link
            className="text-link"
            to="/imports/$importId"
            params={{ importId: receipt.import_id }}
          >
            Open connection test record
          </Link>
        </>
      ) : (
        <p className="notice mt-3">
          No connection test receipt. Live connectivity is unknown.
        </p>
      )}
      <form
        className="form mt-3"
        onSubmit={(event) => {
          event.preventDefault();
          void action.run();
        }}
      >
        <Field label="Test symbol" id={`${provider.id}-test-symbol`}>
          <select
            id={`${provider.id}-test-symbol`}
            value={symbol}
            onChange={(event) => setSymbol(event.target.value)}
            disabled={action.pending}
          >
            {(provider.id === "akshare"
              ? ["600000", "000001"]
              : ["AAPL", "MSFT"]
            ).map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </Field>
        <Field label="Test start · inclusive" id={`${provider.id}-test-start`}>
          <Input
            id={`${provider.id}-test-start`}
            type="date"
            required
            value={start}
            onChange={(event) => setStart(event.target.value)}
            disabled={action.pending}
          />
        </Field>
        <Field label="Test end · exclusive" id={`${provider.id}-test-end`}>
          <Input
            id={`${provider.id}-test-end`}
            type="date"
            required
            value={end}
            onChange={(event) => setEnd(event.target.value)}
            disabled={action.pending}
          />
        </Field>
        <Button
          type="submit"
          variant="outline"
          disabled={
            action.pending ||
            (provider.connection?.enabled === false && !recoverProbe)
          }
        >
          {action.pending
            ? "Submitting test…"
            : recoverProbe
              ? "Recover connection test"
              : "Test connection"}
        </Button>
        {Boolean(action.error) && <ErrorState error={action.error} />}
      </form>
    </section>
  );
}

function SourceForm({
  providers,
  owner,
}: {
  providers: ProviderPublic[];
  owner: string;
}) {
  const navigate = useNavigate();
  const storageKey = `tradegrad.source-form.${owner}`;
  const draft = useRef(
    (() => {
      try {
        return asRecord(JSON.parse(sessionStorage.getItem(storageKey) || "{}"));
      } catch {
        return {};
      }
    })(),
  ).current;
  const initialProvider =
    providers.find((item) => item.id === draft.providerId) ||
    providers.find(
      (item) => item.connection?.is_default && item.connection.enabled,
    ) ||
    providers.find((item) => item.connection?.enabled !== false) ||
    providers[0];
  const [providerId, setProviderId] = useState<
    NonNullable<ResolveCreate["provider"]>
  >(initialProvider?.id || "akshare");
  const provider = providers.find((item) => item.id === providerId);
  const [market, setMarket] = useState<ResolveCreate["market"]>(
    (draft.market === "CN" || draft.market === "US") &&
      initialProvider?.markets.includes(draft.market)
      ? draft.market
      : initialProvider?.markets.includes("CN")
        ? "CN"
        : "US",
  );
  const [symbols, setSymbols] = useState(
    typeof draft.symbols === "string" ? draft.symbols : "",
  );
  const [jobId, setJobId] = useState(
    typeof draft.jobId === "string" ? draft.jobId : "",
  );
  const [selected, setSelected] = useState<string[]>(
    Array.isArray(draft.selected)
      ? draft.selected.filter((id): id is string => typeof id === "string")
      : [],
  );
  const [name, setName] = useState(
    typeof draft.name === "string" ? draft.name : "Market data snapshot",
  );
  const [start, setStart] = useState(
    typeof draft.start === "string" ? draft.start : "2024-01-02",
  );
  const [end, setEnd] = useState(
    typeof draft.end === "string" ? draft.end : "2024-07-01",
  );
  const [priceMode, setPriceMode] = useState<
    NonNullable<ImportCreate["price_mode"]> | ""
  >(
    (initialProvider?.price_modes.includes(String(draft.priceMode))
      ? draft.priceMode
      : initialProvider?.id === "portable_fixture"
        ? "raw"
        : "") as NonNullable<ImportCreate["price_mode"]> | "",
  );
  const [universe, setUniverse] = useState(
    typeof draft.universe === "string" ? draft.universe : "",
  );
  const [validation, setValidation] = useState("");
  useEffect(() => {
    sessionStorage.setItem(
      storageKey,
      JSON.stringify({
        providerId,
        market,
        symbols,
        jobId,
        selected,
        name,
        start,
        end,
        priceMode,
        universe,
      }),
    );
  }, [
    storageKey,
    providerId,
    market,
    symbols,
    jobId,
    selected,
    name,
    start,
    end,
    priceMode,
    universe,
  ]);
  const resolution = useQuery({ ...importOptions(jobId), enabled: !!jobId });
  const listings = useQuery({
    ...listingsOptions(0, resolution.data?.listing_ids),
    enabled: Boolean(resolution.data?.listing_ids.length),
  });
  const universes = useQuery(universesOptions());
  useEffect(() => {
    if (
      resolution.data?.status === "ready" ||
      resolution.data?.status === "partial"
    )
      void queryClient.invalidateQueries({ queryKey: ["data-listings"] });
  }, [resolution.data?.status]);
  const resolved =
    listings.data?.data.filter((listing) =>
      resolution.data?.listing_ids.includes(listing.listing_id),
    ) || [];
  const resolveBody: ResolveCreate = {
    provider: providerId,
    expected_connection_revision: provider?.connection?.revision,
    symbols: parseSymbols(symbols),
    market,
  };
  const importBody: ImportCreate = {
    provider: providerId,
    expected_connection_revision: provider?.connection?.revision,
    name,
    market,
    listing_ids: selected,
    start_date: start,
    end_date: end,
    price_mode: priceMode || null,
    universe_version_id: universe || null,
  };
  const recoverResolve = hasPendingProviderSubmission(
    owner,
    "resolve",
    resolveBody,
  );
  const recoverImport = hasPendingProviderSubmission(
    owner,
    "import",
    importBody,
  );
  const resolve = useDataAction(async () => {
    if (provider?.connection?.enabled === false && !recoverResolve) {
      setValidation(
        "This provider is disabled. Enable it or explicitly select another provider.",
      );
      return;
    }
    const requested = parseSymbols(symbols);
    if (!requested.length || requested.length > 100) {
      setValidation("Enter between 1 and 100 vendor symbols.");
      return;
    }
    setValidation("");
    setSelected([]);
    const body = resolveBody;
    const result = await submitProviderJob(
      owner,
      "resolve",
      body,
      async (frozen, key) =>
        (
          await sdk.resolveDataInstruments({
            body: frozen,
            headers: { "Idempotency-Key": key },
          })
        ).data,
    );
    await queryClient.invalidateQueries({ queryKey: ["data-providers"] });
    setJobId(result.id);
  });
  const create = useDataAction(async () => {
    if (provider?.connection?.enabled === false && !recoverImport) {
      setValidation(
        "This provider is disabled. Enable it or explicitly select another provider.",
      );
      return;
    }
    const issue = dateWindowError(start, end);
    if (issue || !selected.length) {
      setValidation(issue || "Select at least one resolved listing.");
      return;
    }
    setValidation("");
    const body = importBody;
    const result = await submitProviderJob(
      owner,
      "import",
      body,
      async (frozen, key) =>
        (
          await sdk.createDataImport({
            body: frozen,
            headers: { "Idempotency-Key": key },
          })
        ).data,
    );
    await queryClient.invalidateQueries({ queryKey: ["data-imports"] });
    await queryClient.invalidateQueries({ queryKey: ["data-providers"] });
    await navigate({
      to: "/imports/$importId",
      params: { importId: result.id },
    });
  });
  return (
    <div className="data-grid">
      <Panel title="Resolve securities and select listings">
        <form
          className="form"
          onSubmit={(event) => {
            event.preventDefault();
            void resolve.run();
          }}
        >
          <Field label="Provider" id="provider">
            <select
              id="provider"
              value={providerId}
              onChange={(event) => {
                setProviderId(
                  event.target.value as NonNullable<ResolveCreate["provider"]>,
                );
                setMarket(
                  providers
                    .find((item) => item.id === event.target.value)
                    ?.markets.includes("CN")
                    ? "CN"
                    : "US",
                );
                setUniverse("");
                setJobId("");
                setSelected([]);
                setPriceMode(
                  event.target.value === "portable_fixture" ? "raw" : "",
                );
              }}
              disabled={resolve.pending || create.pending}
            >
              {providers.map((item) => (
                <option
                  value={item.id}
                  key={item.id}
                  disabled={item.connection?.enabled === false}
                >
                  {item.name}
                  {item.connection?.enabled === false
                    ? " · disabled"
                    : item.connection?.is_default
                      ? " · market default"
                      : ""}
                </option>
              ))}
            </select>
          </Field>
          {provider && provider.markets.length > 1 && (
            <Field
              label="Market"
              id="market"
              help="Synthetic reference data covers separate CN and US fixtures, not live prices."
            >
              <select
                id="market"
                value={market}
                disabled={resolve.pending || create.pending}
                onChange={(event) => {
                  setMarket(event.target.value as ResolveCreate["market"]);
                  setJobId("");
                  setSelected([]);
                  setUniverse("");
                }}
              >
                {provider.markets.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field
            label="Vendor symbols"
            id="symbols"
            help="Comma or space separated. Tickers are not global identities; confirm each exchange listing below."
          >
            <Input
              id="symbols"
              value={symbols}
              onChange={(event) => setSymbols(event.target.value)}
              placeholder={market === "CN" ? "600000, 000001" : "AAPL, MSFT"}
              disabled={resolve.pending || create.pending}
            />
          </Field>
          {provider?.connection?.enabled === false && (
            <p role="alert" className="notice error">
              This provider is disabled. Existing selections are retained;
              enable it or choose another provider explicitly.
            </p>
          )}
          <Button
            type="submit"
            disabled={
              resolve.pending ||
              create.pending ||
              (provider?.connection?.enabled === false && !recoverResolve)
            }
          >
            <Search />
            {resolve.pending
              ? "Requesting resolution…"
              : recoverResolve
                ? "Recover security resolution"
                : "Resolve securities"}
          </Button>
        </form>
        {validation && !selected.length && (
          <p role="alert" className="notice error mt-4">
            {validation}
          </p>
        )}
        {Boolean(resolve.error) && (
          <div className="mt-4">
            <ErrorState
              error={resolve.error}
              retry={() => void resolve.run()}
            />
          </div>
        )}
        {jobId && (
          <section aria-label="Security resolution" className="mt-5">
            {resolution.error ? (
              <ErrorState
                error={resolution.error}
                retry={() => void resolution.refetch()}
              />
            ) : !resolution.data ||
              !["ready", "partial", "failed", "cancelled"].includes(
                resolution.data.status,
              ) ? (
              <Loading text="Resolving securities in the data worker…" />
            ) : (
              <>
                <p className="text-xs muted">
                  Resolution: {resolution.data.status}. {resolved.length}{" "}
                  listing(s) returned.
                </p>
                {resolution.data.error && (
                  <p className="notice error mt-3" role="alert">
                    {resolution.data.error}
                  </p>
                )}
                {resolution.data.warnings.length > 0 && (
                  <JsonDetails
                    title="Resolution warnings and unresolved securities"
                    value={resolution.data.warnings}
                  />
                )}
                {resolved.map((listing) => (
                  <label className="data-check" key={listing.listing_id}>
                    <input
                      type="checkbox"
                      aria-label={`Select ${listing.symbol} on ${listing.mic}`}
                      disabled={resolve.pending || create.pending}
                      checked={selected.includes(listing.listing_id)}
                      onChange={(event) =>
                        setSelected(
                          event.target.checked
                            ? [...selected, listing.listing_id]
                            : selected.filter(
                                (id) => id !== listing.listing_id,
                              ),
                        )
                      }
                    />
                    <span>
                      <strong>
                        {listing.symbol} · {listing.mic}
                      </strong>{" "}
                      {listing.name}
                      <small>
                        {listing.asset_type} · {listing.currency} ·{" "}
                        {listing.timezone} · {listing.calendar}
                        {listing.calendar_note && (
                          <> · {listing.calendar_note}</>
                        )}
                      </small>
                      <small>
                        Listing {listing.listing_id} · {listing.identity_status}
                      </small>
                      <small>
                        Listed {listing.listed_on || "unknown"} · delisted{" "}
                        {listing.delisted_on || "unknown"}
                      </small>
                    </span>
                  </label>
                ))}
                <Link
                  className="text-link mt-4"
                  to="/imports/$importId"
                  params={{ importId: jobId }}
                >
                  Open durable resolution record
                </Link>
              </>
            )}
            {listings.error && (
              <ErrorState
                error={listings.error}
                retry={() => void listings.refetch()}
              />
            )}
          </section>
        )}
      </Panel>
      <Panel title="Publish a snapshot">
        <form
          className="form"
          onSubmit={(event) => {
            event.preventDefault();
            void create.run();
          }}
        >
          <Field label="Snapshot name" id="snapshot-name">
            <Input
              id="snapshot-name"
              value={name}
              maxLength={255}
              required
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <div className="form-row">
            <Field label="Import start · inclusive" id="import-start">
              <Input
                id="import-start"
                type="date"
                value={start}
                onChange={(event) => setStart(event.target.value)}
                required
              />
            </Field>
            <Field label="Import end · exclusive" id="import-end">
              <Input
                id="import-end"
                type="date"
                value={end}
                onChange={(event) => setEnd(event.target.value)}
                required
              />
            </Field>
          </div>
          <Field label="Price mode" id="price-mode">
            <select
              id="price-mode"
              value={priceMode}
              onChange={(event) =>
                setPriceMode(
                  event.target.value as
                    NonNullable<ImportCreate["price_mode"]> | "",
                )
              }
            >
              {provider?.connection && (
                <option value="">
                  Connection default ({provider.connection.default_price_mode})
                </option>
              )}
              {provider?.price_modes
                .filter((mode) => mode !== "repair")
                .map((mode) => (
                  <option key={mode} value={mode}>
                    {mode}
                  </option>
                ))}
            </select>
          </Field>
          <p className="notice">
            Provider-native does not mean raw. Yahoo OHLC with auto_adjust=False
            is split-adjusted; OpenBB Nasdaq split/dividend basis remains
            unknown unless evidenced. No silent source fallback or
            forward-filled tradable bars.
          </p>
          <Field label="Universe version (optional)" id="import-universe">
            <select
              id="import-universe"
              value={universe}
              onChange={(event) => setUniverse(event.target.value)}
            >
              <option value="">No universe attached</option>
              {universes.data?.data.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name} · {universeLabels[item.state] || item.state}
                </option>
              ))}
            </select>
          </Field>
          <Metadata
            values={{
              "Selected listings": selected.length,
              "Date convention": "[start, end)",
              Publication: "All blocks validated",
            }}
          />
          {validation && (
            <p role="alert" className="notice error">
              {validation}
            </p>
          )}
          {Boolean(create.error) && <ErrorState error={create.error} />}
          <Button
            type="submit"
            disabled={
              create.pending ||
              resolve.pending ||
              !selected.length ||
              (provider?.connection?.enabled === false && !recoverImport)
            }
          >
            <Database />
            {create.pending
              ? "Submitting…"
              : recoverImport
                ? "Recover snapshot import"
                : "Import selected listings"}
          </Button>
          <small className="muted">
            Repeated clicks are guarded. Retry unchanged inputs after a network
            error to recover the same submission.
          </small>
        </form>
      </Panel>
    </div>
  );
}
