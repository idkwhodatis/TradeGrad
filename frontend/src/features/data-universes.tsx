import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type {
  ListingPublic,
  UniverseCreate,
  UniverseMember,
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
  Pagination,
} from "@/components/data-common";
import { queryClient } from "@/lib/api";
import { listingsOptions, universesOptions } from "@/lib/data-api";
import { universeEvidenceError, universeLabels } from "@/lib/data-utils";
import { useDataAction } from "@/lib/use-data-action";

export function UniversesPage() {
  const [offset, setOffset] = useState(0);
  const [listingOffset, setListingOffset] = useState(0);
  const universes = useQuery(universesOptions(offset));
  const listings = useQuery(listingsOptions(listingOffset));
  const [selected, setSelected] = useState<ListingPublic[]>([]);
  const [members, setMembers] = useState<Record<string, UniverseMember>>({});
  const [name, setName] = useState("");
  const [state, setState] =
    useState<NonNullable<UniverseCreate["state"]>>("user_fixed");
  const [asOf, setAsOf] = useState(new Date().toISOString().slice(0, 16));
  const [evidence, setEvidence] = useState("");
  const [note, setNote] = useState("");
  const [validation, setValidation] = useState("");
  const [created, setCreated] = useState("");
  function updateMember(id: string, patch: Partial<UniverseMember>) {
    setMembers((current) => ({
      ...current,
      [id]: { ...current[id], listing_id: id, ...patch },
    }));
  }
  const create = useDataAction(async () => {
    if (!selected.length || !name.trim() || !asOf) {
      setValidation(
        "Give this universe a name, an as-of time and at least one resolved listing.",
      );
      return;
    }
    const data = selected.map(
      (listing) =>
        members[listing.listing_id] || { listing_id: listing.listing_id },
    );
    const evidenceError = universeEvidenceError(state, evidence, note);
    if (evidenceError) {
      setValidation(evidenceError);
      return;
    }
    if (
      state === "historical_membership" &&
      data.some((member) => !member.valid_from)
    ) {
      setValidation(
        "Historical membership needs an evidence reference and each member's valid-from date.",
      );
      return;
    }
    if (
      data.some(
        (member) =>
          member.valid_to &&
          (!member.valid_from || member.valid_to <= member.valid_from),
      )
    ) {
      setValidation(
        "Each member's valid-to date must follow its valid-from date.",
      );
      return;
    }
    setValidation("");
    const body: UniverseCreate = {
      name: name.trim(),
      state,
      as_of: `${asOf}:00Z`,
      members: data,
      evidence: evidence.trim()
        ? [
            {
              reference: evidence.trim(),
              note,
              verification: "user_supplied_not_independently_verified",
            },
          ]
        : [],
    };
    const result = (await sdk.createDataUniverse({ body })).data;
    setCreated(`${result.name} · ${result.id}`);
    setSelected([]);
    setName("");
    await queryClient.invalidateQueries({ queryKey: ["data-universes"] });
  });
  return (
    <>
      <PageHeader
        title="Universe versions"
        subtitle="Fixed baskets, current lists and historical intervals are different evidence claims."
      >
        <Button variant="outline" asChild>
          <Link to="/data-sources">Resolve more securities</Link>
        </Button>
      </PageHeader>
      <DataNotice />
      <div className="data-grid mt-6">
        <Panel title="Create an immutable universe">
          <form
            className="form"
            onSubmit={(event) => {
              event.preventDefault();
              void create.run();
            }}
          >
            <fieldset
              disabled={create.pending}
              className="form border-0 p-0 m-0 min-w-0"
            >
              <Field label="Universe name" id="universe-name">
                <Input
                  id="universe-name"
                  value={name}
                  required
                  maxLength={255}
                  onChange={(event) => setName(event.target.value)}
                />
              </Field>
              <Field label="Membership evidence type" id="universe-state">
                <select
                  id="universe-state"
                  value={state}
                  onChange={(event) =>
                    setState(
                      event.target.value as NonNullable<
                        UniverseCreate["state"]
                      >,
                    )
                  }
                >
                  {[
                    "user_fixed",
                    "current_snapshot",
                    "historical_membership",
                  ].map((value) => (
                    <option value={value} key={value}>
                      {universeLabels[value]}
                    </option>
                  ))}
                  <option value="point_in_time_verified" disabled>
                    Verified PIT requires independently verified evidence
                    (unavailable for manual creation)
                  </option>
                </select>
              </Field>
              <Field label="Universe as of · UTC" id="universe-as-of">
                <Input
                  type="datetime-local"
                  id="universe-as-of"
                  value={asOf}
                  required
                  onChange={(event) => setAsOf(event.target.value)}
                />
              </Field>
              <p className="notice">
                {state === "current_snapshot"
                  ? "Today's members cannot be used as historical completeness. Current snapshots cannot be applied before their as-of date."
                  : state === "historical_membership"
                    ? "Historical intervals do not prove when membership information was available. User-supplied references are retained, not promoted to verified PIT."
                    : "A user-fixed basket is an explicit research choice, not evidence of a historical investable universe."}
              </p>
              <section aria-label="Universe listing selection">
                <h3>Choose resolved listings</h3>
                {listings.isPending ? (
                  <Loading />
                ) : listings.error ? (
                  <ErrorState
                    error={listings.error}
                    retry={() => void listings.refetch()}
                  />
                ) : (
                  <>
                    {listings.data?.data.map((listing) => (
                      <label className="data-check" key={listing.listing_id}>
                        <input
                          type="checkbox"
                          aria-label={`Include ${listing.symbol} on ${listing.mic}`}
                          checked={selected.some(
                            (item) => item.listing_id === listing.listing_id,
                          )}
                          onChange={(event) =>
                            setSelected(
                              event.target.checked
                                ? [...selected, listing]
                                : selected.filter(
                                    (item) =>
                                      item.listing_id !== listing.listing_id,
                                  ),
                            )
                          }
                        />
                        <span>
                          {listing.symbol} · {listing.mic}
                          <small>
                            {listing.currency} · {listing.timezone}
                          </small>
                        </span>
                      </label>
                    ))}
                    <Pagination
                      offset={listingOffset}
                      limit={100}
                      count={listings.data?.count || 0}
                      busy={listings.isFetching}
                      onPage={setListingOffset}
                    />
                  </>
                )}
              </section>
              {selected.map((listing) => (
                <section
                  key={listing.listing_id}
                  aria-label={`${listing.symbol} membership dates`}
                >
                  <h3>
                    {listing.symbol} · {listing.mic}
                  </h3>
                  <div className="form-row mt-3">
                    <Field
                      label={`${listing.symbol} valid from`}
                      id={`from-${listing.listing_id}`}
                    >
                      <Input
                        type="date"
                        id={`from-${listing.listing_id}`}
                        required={state === "historical_membership"}
                        value={members[listing.listing_id]?.valid_from || ""}
                        onChange={(event) =>
                          updateMember(listing.listing_id, {
                            valid_from: event.target.value || null,
                          })
                        }
                      />
                    </Field>
                    <Field
                      label={`${listing.symbol} valid to · exclusive`}
                      id={`to-${listing.listing_id}`}
                    >
                      <Input
                        type="date"
                        id={`to-${listing.listing_id}`}
                        value={members[listing.listing_id]?.valid_to || ""}
                        onChange={(event) =>
                          updateMember(listing.listing_id, {
                            valid_to: event.target.value || null,
                          })
                        }
                      />
                    </Field>
                  </div>
                  <Field
                    label={`${listing.symbol} information known at · UTC (optional)`}
                    id={`known-${listing.listing_id}`}
                  >
                    <Input
                      type="datetime-local"
                      id={`known-${listing.listing_id}`}
                      onChange={(event) =>
                        updateMember(listing.listing_id, {
                          known_at: event.target.value
                            ? `${event.target.value}:00Z`
                            : null,
                        })
                      }
                    />
                  </Field>
                </section>
              ))}
              <Field
                label="Membership evidence reference"
                id="universe-evidence"
                help="Required for current and historical membership. URL, document or announcement reference plus note must fit within 16 KiB serialized. Stored as user evidence, never independently verified."
              >
                <Input
                  id="universe-evidence"
                  value={evidence}
                  required={state !== "user_fixed"}
                  maxLength={16_000}
                  onChange={(event) => setEvidence(event.target.value)}
                />
              </Field>
              <Field label="Evidence note" id="universe-note">
                <textarea
                  id="universe-note"
                  value={note}
                  maxLength={16_000}
                  onChange={(event) => setNote(event.target.value)}
                />
              </Field>
              {validation && (
                <p role="alert" className="notice error">
                  {validation}
                </p>
              )}
              {Boolean(create.error) && <ErrorState error={create.error} />}
              <Button
                type="submit"
                disabled={create.pending || !selected.length}
              >
                {create.pending ? "Saving…" : "Publish universe version"}
              </Button>
              {created && (
                <p role="status" className="notice">
                  Published {created}
                </p>
              )}
            </fieldset>
          </form>
        </Panel>
        <div>
          <Panel title="Published universe versions">
            {universes.isPending ? (
              <Loading />
            ) : universes.error ? (
              <ErrorState
                error={universes.error}
                retry={() => void universes.refetch()}
              />
            ) : (
              <div className="stack">
                {universes.data?.data.map((universe) => (
                  <article
                    key={universe.id}
                    className="border-b border-border pb-5"
                  >
                    <h3>{universe.name}</h3>
                    <Badge variant="outline" className="mt-3">
                      {universeLabels[universe.state] || universe.state}
                    </Badge>
                    <Metadata
                      values={{
                        "As of": universe.as_of,
                        Members: universe.members.length,
                        Version: universe.id,
                      }}
                    />
                    <JsonDetails
                      title={`Membership intervals for ${universe.name}`}
                      value={universe.members}
                    />
                    <JsonDetails
                      title={`Evidence for ${universe.name}`}
                      value={universe.evidence}
                    />
                    <p className="hash mt-4">
                      SHA-256 {universe.content_sha256}
                    </p>
                  </article>
                ))}
                {!universes.data?.count && (
                  <p className="text-xs muted">
                    No published universes yet. Resolve securities first, then
                    create a deliberate basket.
                  </p>
                )}
              </div>
            )}
          </Panel>
          <Pagination
            offset={offset}
            limit={50}
            count={universes.data?.count || 0}
            busy={universes.isFetching}
            onPage={setOffset}
          />
        </div>
      </div>
    </>
  );
}
