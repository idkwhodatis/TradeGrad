import type { UploadMapping } from "@/client/types.gen";

export function portableUploadDeclaration(
  mapping: UploadMapping,
  enabled: boolean,
  sourceReference: string,
  evidenceNote: string,
): UploadMapping["portable_declaration"] {
  if (!enabled) return null;
  return {
    schema_version: "marketdata.portable-upload-declaration.v1",
    kind: "user_declared_raw_no_actions",
    acknowledged_unverified: true,
    symbol: mapping.symbol,
    mic: mapping.mic,
    start: mapping.start_date,
    end: mapping.end_date,
    source_reference: sourceReference.trim(),
    evidence_note: evidenceNote.trim(),
  };
}

export function portableUploadAssumptionError(
  mapping: UploadMapping,
): string | null {
  const claim = mapping.portable_declaration;
  if (!claim) return null;
  const price = mapping.price_semantics;
  if (
    !claim.source_reference.trim() ||
    claim.source_reference.length > 512 ||
    !claim.evidence_note.trim() ||
    claim.evidence_note.length > 2000 ||
    /[\x00-\x1f\x7f]/.test(claim.source_reference + claim.evidence_note) ||
    claim.acknowledged_unverified !== true ||
    claim.symbol !== mapping.symbol ||
    claim.mic !== mapping.mic ||
    claim.start !== mapping.start_date ||
    claim.end !== mapping.end_date ||
    !["raw", "user_declared_raw"].includes(price.native_mode || "") ||
    price.split_basis !== "unadjusted" ||
    price.dividend_basis !== "unadjusted" ||
    price.adjustment_as_of != null ||
    price.algorithm_version != null ||
    price.actions_snapshot_id != null ||
    mapping.volume_unit !== "shares" ||
    !["USD", "CNY"].includes(mapping.currency) ||
    mapping.unknown_flags?.length
  ) {
    return "Portable research assumptions require bounded source and evidence notes, exact identity/date coverage, explicitly raw/unadjusted OHLC, share volume and known currency. No semantics are changed automatically.";
  }
  return null;
}
