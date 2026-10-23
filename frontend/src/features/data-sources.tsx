import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Database, Search } from "lucide-react";
import type {
  Capability,
  ImportCreate,
  ProviderPublic,
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
  clearDataSubmission,
  dataSubmissionKey,
  dateWindowError,
  parseSymbols,
  universeLabels,
} from "@/lib/data-utils";
import { useDataAction } from "@/lib/use-data-action";

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
                <ProviderCard key={provider.id} provider={provider} />
              ))}
            </div>
            <SourceForm providers={providers.data} owner={user.data.id} />
          </>
        ) : null}
      </div>
    </>
  );
}
function ProviderCard({ provider }: { provider: ProviderPublic }) {
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
    </article>
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
    providers.find((item) => item.id === draft.providerId) || providers[0];
  const [providerId, setProviderId] = useState<ResolveCreate["provider"]>(
    initialProvider?.id || "akshare",
  );
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
  const [priceMode, setPriceMode] = useState<ImportCreate["price_mode"]>(
    (initialProvider?.price_modes.includes(String(draft.priceMode))
      ? draft.priceMode
      : initialProvider?.price_modes[0] ||
        "provider_native") as ImportCreate["price_mode"],
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
  const resolve = useDataAction(async () => {
    const requested = parseSymbols(symbols);
    if (!requested.length || requested.length > 100) {
      setValidation("Enter between 1 and 100 vendor symbols.");
      return;
    }
    setValidation("");
    setSelected([]);
    const body: ResolveCreate = {
      provider: providerId,
      symbols: requested,
      market,
    };
    const result = (
      await sdk.resolveDataInstruments({
        body,
        headers: {
          "Idempotency-Key": dataSubmissionKey(owner, "resolve", body),
        },
      })
    ).data;
    clearDataSubmission(owner, "resolve");
    setJobId(result.id);
  });
  const create = useDataAction(async () => {
    const issue = dateWindowError(start, end);
    if (issue || !selected.length) {
      setValidation(issue || "Select at least one resolved listing.");
      return;
    }
    setValidation("");
    const body: ImportCreate = {
      provider: providerId,
      name,
      market,
      listing_ids: selected,
      start_date: start,
      end_date: end,
      price_mode: priceMode,
      universe_version_id: universe || null,
    };
    const result = (
      await sdk.createDataImport({
        body,
        headers: {
          "Idempotency-Key": dataSubmissionKey(owner, "import", body),
        },
      })
    ).data;
    clearDataSubmission(owner, "import");
    await queryClient.invalidateQueries({ queryKey: ["data-imports"] });
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
                setProviderId(event.target.value as ResolveCreate["provider"]);
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
                  (providers.find((item) => item.id === event.target.value)
                    ?.price_modes[0] ||
                    "provider_native") as ImportCreate["price_mode"],
                );
              }}
              disabled={resolve.pending || create.pending}
            >
              {providers.map((item) => (
                <option value={item.id} key={item.id}>
                  {item.name}
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
          <Button type="submit" disabled={resolve.pending || create.pending}>
            <Search />
            {resolve.pending ? "Requesting resolution…" : "Resolve securities"}
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
                setPriceMode(event.target.value as ImportCreate["price_mode"])
              }
            >
              {provider?.price_modes.map((mode) => (
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
            disabled={create.pending || resolve.pending || !selected.length}
          >
            <Database />
            {create.pending ? "Submitting…" : "Import selected listings"}
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
