import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import type { UploadMapping, PriceSemantics } from "@/client/types.gen";
import * as sdk from "@/client/sdk.gen";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorState, PageHeader, Panel } from "@/components/common";
import { DataNotice, Field, JsonDetails } from "@/components/data-common";
import { currentUserOptions } from "@/lib/api";
import {
  clearDataSubmission,
  dataSubmissionKey,
  dateWindowError,
} from "@/lib/data-utils";
import { useDataAction } from "@/lib/use-data-action";
import {
  portableUploadDeclaration,
  portableUploadAssumptionError,
} from "@/lib/portable-upload";

const requiredColumns = [
  "session_date",
  "open",
  "high",
  "low",
  "close",
  "volume",
];
export function UploadDatasetPage() {
  const navigate = useNavigate();
  const user = useQuery(currentUserOptions);
  const [file, setFile] = useState<File | null>(null);
  const [validation, setValidation] = useState("");
  const [declarePortable, setDeclarePortable] = useState(false);
  const [sourceReference, setSourceReference] = useState("");
  const [evidenceNote, setEvidenceNote] = useState("");
  const [mapping, setMapping] = useState<UploadMapping>({
    name: "Uploaded data snapshot",
    symbol: "",
    market: "US",
    mic: "XNAS",
    currency: "USD",
    timezone: "America/New_York",
    calendar: "XNYS",
    start_date: "2024-01-02",
    end_date: "2024-05-01",
    date_format: "%Y-%m-%d",
    columns: {
      session_date: "Date",
      open: "Open",
      high: "High",
      low: "Low",
      close: "Close",
      volume: "Volume",
    },
    volume_unit: "unknown",
    price_semantics: {
      native_mode: "unknown",
      split_basis: "unknown",
      dividend_basis: "unknown",
    },
    unknown_flags: ["volume_unit_unknown"],
  });
  function field<K extends keyof UploadMapping>(
    name: K,
    value: UploadMapping[K],
  ) {
    setMapping((current) => ({ ...current, [name]: value }));
  }
  function prices<K extends keyof PriceSemantics>(
    name: K,
    value: PriceSemantics[K],
  ) {
    setMapping((current) => ({
      ...current,
      price_semantics: { ...current.price_semantics, [name]: value },
    }));
  }
  const exactMapping: UploadMapping = {
    ...mapping,
    portable_declaration: portableUploadDeclaration(
      mapping,
      declarePortable,
      sourceReference,
      evidenceNote,
    ),
    currency:
      mapping.currency.trim().toLowerCase() === "unknown"
        ? "unknown"
        : mapping.currency.trim().toUpperCase(),
    unknown_flags: [
      ...(mapping.volume_unit === "unknown" ? ["volume_unit_unknown"] : []),
      ...(mapping.currency.trim().toLowerCase() === "unknown"
        ? ["currency_unknown"]
        : []),
    ],
  };
  const upload = useDataAction(async () => {
    const issue = dateWindowError(mapping.start_date, mapping.end_date);
    if (!file || !user.data || issue) {
      setValidation(
        issue ||
          "Choose a CSV or Parquet file and wait for your session to load.",
      );
      return;
    }
    if (
      !/\.(csv|parquet)$/i.test(file.name) ||
      file.size === 0 ||
      file.size > 10_000_000
    ) {
      setValidation(
        "Choose a nonempty CSV or Parquet file no larger than 10 MB.",
      );
      return;
    }
    if (
      new Set(Object.values(mapping.columns)).size !==
      Object.keys(mapping.columns).length
    ) {
      setValidation("Each normalized field needs a distinct source column.");
      return;
    }
    const assumptionIssue = portableUploadAssumptionError(exactMapping);
    if (assumptionIssue) {
      setValidation(assumptionIssue);
      return;
    }
    setValidation("");
    const sha = Array.from(
      new Uint8Array(
        await crypto.subtle.digest("SHA-256", await file.arrayBuffer()),
      ),
    )
      .map((value) => value.toString(16).padStart(2, "0"))
      .join("");
    const body = exactMapping;
    const result = (
      await sdk.uploadDataFile({
        body: { file, mapping: JSON.stringify(body) },
        headers: {
          "Idempotency-Key": dataSubmissionKey(user.data.id, "upload", {
            mapping: body,
            sha,
            format: file.name.split(".").at(-1)?.toLowerCase(),
          }),
        },
      })
    ).data;
    clearDataSubmission(user.data.id, "upload");
    await navigate({
      to: "/imports/$importId",
      params: { importId: result.id },
    });
  });
  return (
    <>
      <PageHeader
        title="Upload a data snapshot"
        subtitle="CSV and Parquet follow the same immutable validation and publication path."
      >
        <Link className="text-link" to="/datasets">
          Cancel upload
        </Link>
      </PageHeader>
      <DataNotice />
      <form
        className="stack mt-6"
        onSubmit={(event) => {
          event.preventDefault();
          void upload.run();
        }}
      >
        <fieldset
          disabled={upload.pending}
          className="stack border-0 p-0 m-0 min-w-0"
        >
          <Panel title="File and security identity">
            <div className="form">
              <Field
                label="CSV or Parquet file"
                id="upload-file"
                help="At most 10 MB in this browser form. No file is sent until you submit."
              >
                <Input
                  type="file"
                  accept=".csv,.parquet"
                  id="upload-file"
                  required
                  onChange={(event) => setFile(event.target.files?.[0] || null)}
                />
              </Field>
              <Field label="Upload snapshot name" id="upload-name">
                <Input
                  id="upload-name"
                  value={mapping.name}
                  required
                  maxLength={255}
                  onChange={(event) => field("name", event.target.value)}
                />
              </Field>
              <div className="form-row">
                <Field label="Upload symbol" id="upload-symbol">
                  <Input
                    id="upload-symbol"
                    value={mapping.symbol}
                    required
                    maxLength={40}
                    onChange={(event) => field("symbol", event.target.value)}
                  />
                </Field>
                <Field label="Upload market" id="upload-market">
                  <select
                    id="upload-market"
                    value={mapping.market}
                    onChange={(event) =>
                      field(
                        "market",
                        event.target.value as UploadMapping["market"],
                      )
                    }
                  >
                    <option value="US">United States</option>
                    <option value="CN">China</option>
                  </select>
                </Field>
              </div>
              <div className="form-row">
                {(
                  [
                    ["mic", "Exchange MIC"],
                    ["currency", "Currency"],
                    ["timezone", "IANA timezone"],
                    ["calendar", "Trading calendar"],
                  ] as const
                ).map(([key, label]) => (
                  <Field key={key} label={label} id={`upload-${key}`}>
                    <Input
                      id={`upload-${key}`}
                      value={mapping[key]}
                      required
                      onChange={(event) => field(key, event.target.value)}
                    />
                  </Field>
                ))}
              </div>
            </div>
          </Panel>
          <Panel title="Map columns and the date window">
            <div className="form">
              <p className="text-xs muted">
                Enter exact source column names. Invalid dates, absent columns,
                duplicates and OHLC inconsistencies are errors, not silently
                repaired bars.
              </p>
              <div className="form-row">
                {[...requiredColumns, "adj_close", "amount"].map((column) => (
                  <Field
                    label={`Source column for ${column}${requiredColumns.includes(column) ? "" : " (optional)"}`}
                    key={column}
                    id={`column-${column}`}
                  >
                    <Input
                      id={`column-${column}`}
                      required={requiredColumns.includes(column)}
                      value={mapping.columns[column] || ""}
                      onChange={(event) => {
                        const columns = { ...mapping.columns };
                        if (event.target.value)
                          columns[column] = event.target.value;
                        else delete columns[column];
                        field("columns", columns);
                      }}
                    />
                  </Field>
                ))}
              </div>
              <div className="form-row">
                <Field label="Upload start · inclusive" id="upload-start">
                  <Input
                    type="date"
                    id="upload-start"
                    value={mapping.start_date}
                    required
                    onChange={(event) =>
                      field("start_date", event.target.value)
                    }
                  />
                </Field>
                <Field label="Upload end · exclusive" id="upload-end">
                  <Input
                    type="date"
                    id="upload-end"
                    value={mapping.end_date}
                    required
                    onChange={(event) => field("end_date", event.target.value)}
                  />
                </Field>
              </div>
              <Field
                label="Source date format"
                id="upload-format"
                help="Explicit Python date format, e.g. %Y-%m-%d. Sessions are exchange-local dates."
              >
                <Input
                  id="upload-format"
                  value={mapping.date_format}
                  required
                  onChange={(event) => field("date_format", event.target.value)}
                />
              </Field>
            </div>
          </Panel>
          <Panel title="Units and price semantics">
            <div className="form">
              <Field label="Volume unit" id="upload-unit">
                <select
                  id="upload-unit"
                  value={mapping.volume_unit}
                  onChange={(event) =>
                    field(
                      "volume_unit",
                      event.target.value as UploadMapping["volume_unit"],
                    )
                  }
                >
                  {["unknown", "shares", "lots", "contracts"].map((unit) => (
                    <option key={unit} value={unit}>
                      {unit}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Native price mode" id="upload-native">
                <Input
                  id="upload-native"
                  value={mapping.price_semantics.native_mode}
                  onChange={(event) =>
                    prices("native_mode", event.target.value || "unknown")
                  }
                />
              </Field>
              <div className="form-row">
                {(
                  [
                    ["split_basis", "Upload split basis"],
                    ["dividend_basis", "Upload dividend basis"],
                  ] as const
                ).map(([key, label]) => (
                  <Field key={key} label={label} id={`upload-${key}`}>
                    <select
                      id={`upload-${key}`}
                      value={mapping.price_semantics[key]}
                      onChange={(event) =>
                        prices(
                          key,
                          event.target.value as PriceSemantics[typeof key],
                        )
                      }
                    >
                      {["unknown", "unadjusted", "adjusted"].map((value) => (
                        <option key={value} value={value}>
                          {value}
                        </option>
                      ))}
                    </select>
                  </Field>
                ))}
              </div>
              <p className="notice">
                Use unknown for an unknown currency. Unknown is a stored value,
                not an assumption of raw prices or shares. Upload provenance is
                user-supplied and not independently verified. Corporate actions
                are not fabricated.
              </p>
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  checked={declarePortable}
                  onChange={(event) => setDeclarePortable(event.target.checked)}
                />
                <span>
                  Enable portable research using my unverified raw/no-actions
                  declaration
                </span>
              </label>
              {declarePortable && (
                <>
                  <p className="notice">
                    I declare that the uploaded OHLC is raw and unadjusted and
                    that no corporate actions occurred for{" "}
                    {mapping.symbol || "this symbol"} on {mapping.mic}, from{" "}
                    {mapping.start_date} inclusive to {mapping.end_date}{" "}
                    exclusive. This includes warm-up sessions. I understand that
                    this is my research assumption, not independently verified
                    action coverage or provider, runtime or cross-framework
                    certification. References are retained, not fetched or
                    verified. Known actions and unknown/adjusted semantics are
                    rejected. The original bytes and declaration are immutable.
                  </p>
                  <Field
                    label="Declaration source reference"
                    id="upload-source-reference"
                    help="A public source/document identifier, without passwords or private access tokens."
                  >
                    <Input
                      id="upload-source-reference"
                      value={sourceReference}
                      required
                      maxLength={512}
                      onChange={(event) =>
                        setSourceReference(event.target.value)
                      }
                    />
                  </Field>
                  <Field
                    label="Basis for raw/no-actions assumption"
                    id="upload-evidence-note"
                    help="Describe what you reviewed for this exact security and date range. This note is not independent proof."
                  >
                    <Input
                      id="upload-evidence-note"
                      value={evidenceNote}
                      required
                      maxLength={2000}
                      onChange={(event) => setEvidenceNote(event.target.value)}
                    />
                  </Field>
                </>
              )}
              <JsonDetails
                title="Review exact mapping before upload"
                value={exactMapping}
              />
            </div>
          </Panel>
          {validation && (
            <p role="alert" className="notice error">
              {validation}
            </p>
          )}
          {Boolean(upload.error) && <ErrorState error={upload.error} />}
          {user.error && <ErrorState error={user.error} />}
          <div className="form-footer">
            <Button
              type="submit"
              disabled={upload.pending || !file || !user.data}
            >
              <Upload />
              {upload.pending ? "Uploading…" : "Validate and import file"}
            </Button>
          </div>
        </fieldset>
      </form>
    </>
  );
}
