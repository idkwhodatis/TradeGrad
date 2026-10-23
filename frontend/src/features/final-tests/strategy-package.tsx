import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import type { StrategyPackagePublic } from "@/client/types.gen";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/common";
import { JsonDetails, Metadata } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import { currentUserOptions } from "@/lib/api";
import {
  createStrategyPackage,
  downloadStrategyPackage,
  strategyPackageOptions,
} from "@/lib/final-api";
import { formatBytes } from "@/lib/format";
import { saveResearchDownload } from "@/lib/research-results";
import { useDataAction } from "@/lib/use-data-action";
import { useDurableAction } from "./durable-action";
import { exposureNotice, finalTrustNotice, packageSchema } from "./protocol";
import { portableFinalNotice } from "@/features/portable/final-protocol";
export const packageNotice =
  "References-only package: no raw market data and no self-contained offline runtime. Replay requires the exact referenced data snapshot and pinned execution image. It reproduces selected-strategy execution, not model re-optimization. Creating this archive does not certify successful replay or portable framework equivalence.";

export function CreateStrategyPackage({
  selectionId,
  evaluationId,
}: {
  selectionId: string;
  evaluationId: string;
}) {
  const owner = useQuery(currentUserOptions);
  return (
    <Panel title="Export reproducible references-only package">
      <p className="notice" data-testid="package-scope">
        {packageNotice}
      </p>
      {owner.isPending ? (
        <Loading />
      ) : owner.error ? (
        <ErrorState error={owner.error} retry={() => void owner.refetch()} />
      ) : (
        owner.data && (
          <PackageForm
            key={`${owner.data.id}:${selectionId}:${evaluationId}`}
            owner={owner.data.id}
            selectionId={selectionId}
            evaluationId={evaluationId}
          />
        )
      )}
    </Panel>
  );
}
function PackageForm({
  owner,
  selectionId,
  evaluationId,
}: {
  owner: string;
  selectionId: string;
  evaluationId: string;
}) {
  const navigate = useNavigate();
  const draft = useDurableAction({
    owner,
    scope: `package:${selectionId}:${evaluationId}`,
    schema: packageSchema,
    initial: {
      final_selection_id: selectionId,
      reference_evaluation_id: evaluationId,
      profile: "reference_only.v1" as const,
    },
  });
  return (
    <div className="stack mt-4">
      <p className="text-xs muted">
        Reference holdout evaluation: {evaluationId}. Identical requests return
        the same deterministic package; downloads require your authenticated
        owner session.
      </p>
      {Boolean(draft.error) && <ErrorState error={draft.error} />}
      <Button
        disabled={
          draft.busy ||
          draft.body?.final_selection_id !== selectionId ||
          draft.body?.reference_evaluation_id !== evaluationId
        }
        onClick={() =>
          void draft.run(
            (body) =>
              createStrategyPackage(body.final_selection_id, {
                reference_evaluation_id: body.reference_evaluation_id,
                profile: body.profile,
              }),
            (item) => {
              void navigate({
                to: "/strategy-packages/$packageId",
                params: { packageId: item.id },
              });
            },
          )
        }
      >
        {draft.busy
          ? "Preparing package…"
          : draft.hasPending
            ? "Recover same reference package"
            : "Create reference-only strategy package"}
      </Button>
    </div>
  );
}
export function StrategyPackagePage() {
  const { packageId } = useParams({ strict: false }) as { packageId: string };
  const item = useQuery(strategyPackageOptions(packageId));
  return item.isPending ? (
    <Loading text="Loading authenticated package metadata…" />
  ) : item.error ? (
    <ErrorState error={item.error} retry={() => void item.refetch()} />
  ) : item.data ? (
    <StrategyPackageContent item={item.data} />
  ) : (
    <p className="notice">No package is available.</p>
  );
}
export function StrategyPackageContent({
  item,
}: {
  item: StrategyPackagePublic;
}) {
  const download = useDataAction(async () => {
    const blob = await downloadStrategyPackage(item.id);
    saveResearchDownload(blob, `strategy-package-${item.id}.zip`);
  });
  return (
    <>
      <Link
        className="text-link mb-5"
        to={
          item.manifest.mode === "portable"
            ? "/portable/final-selections/$selectionId"
            : "/final-selections/$selectionId"
        }
        params={{ selectionId: item.final_selection_id }}
      >
        Frozen selection
      </Link>
      <PageHeader
        eyebrow="REFERENCE-ONLY STRATEGY PACKAGE"
        title="Deterministic strategy export"
        subtitle="Authenticated ZIP download with immutable content hashes and explicit replay prerequisites."
      />
      <div className="stack">
        <p className="notice" data-testid="package-scope">
          {packageNotice}
        </p>
        <p className="notice">
          {item.manifest.mode === "portable"
            ? portableFinalNotice
            : finalTrustNotice}
        </p>
        <p className="notice">{exposureNotice}</p>
        <Panel title="Verified archive metadata">
          <Metadata
            values={{
              "Package ID": item.id,
              "Frozen selection": item.final_selection_id,
              "Archive SHA-256": item.archive_sha256,
              "Canonical content SHA-256": item.content_sha256,
              "Request SHA-256": item.request_sha256,
              "Archive size": formatBytes(item.size_bytes),
              Created: item.created_at,
              "Exposure snapshot as of": item.exposure_as_of,
              "Raw market data": "not included",
              "Replay certification": "not established by export",
            }}
          />
          <JsonDetails
            title="Manifest, included files and declared capabilities"
            value={item.manifest}
          />
          <JsonDetails
            title="Recorded exposure snapshot"
            value={item.exposure_snapshot}
          />
          {Boolean(download.error) && <ErrorState error={download.error} />}
          <Button
            className="mt-5"
            disabled={download.pending}
            onClick={() => void download.run()}
          >
            {download.pending
              ? "Downloading…"
              : "Download authenticated strategy ZIP"}
          </Button>
        </Panel>
      </div>
    </>
  );
}
