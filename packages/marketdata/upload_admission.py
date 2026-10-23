"""Bind optional user declarations to exact immutable uploads, never certify them."""

import hashlib
import re

from .contracts import (
    BarsRequest,
    ProviderResult,
    UploadMapping,
    validate_portable_upload,
)
from .normalize import canonical_json

DECLARATION_FILE = "portable-upload-declaration.json"
DECLARATION_VERSION = "marketdata.portable-upload-evidence.v1"
EVIDENCE = "user_declaration_unverified"


def declared_upload_evidence(
    request: BarsRequest, result: ProviderResult
) -> dict | None:
    declared = [
        p
        for p in result.provenance
        if p.effective_params.get("portable_declaration") is not None
    ]
    if not declared:
        return None
    if len(declared) != 1 or len(result.provenance) != 1 or len(request.listings) != 1:
        raise ValueError(
            "Portable upload declaration requires one exact upload/listing"
        )
    source = declared[0]
    listing = request.listings[0]
    if (
        (source.connector, source.provider, source.endpoint, source.evidence)
        != ("upload", "user", "uploaded_table", "uploaded")
        or source.requested_params
        != {
            "listing_id": listing.listing_id,
            "start": str(request.start),
            "end": str(request.end),
        }
        or result.actions
    ):
        raise ValueError(
            "Portable upload declaration has unsupported provenance or actions"
        )
    params = dict(source.effective_params)
    original = params.pop("upload_source", None)
    mapping = UploadMapping.model_validate(params)
    validate_portable_upload(mapping, start=request.start, end=request.end)
    if (
        mapping.symbol != listing.symbol
        or mapping.mic != listing.mic
        or mapping.currency != listing.currency
        or mapping.timezone != listing.timezone
        or mapping.calendar != listing.calendar
        or mapping.price_semantics != result.price_semantics
        or not isinstance(original, dict)
        or set(original) != {"artifact", "sha256", "size_bytes"}
        or not re.fullmatch(r"source\.(csv|parquet)", str(original.get("artifact")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(original.get("sha256")))
        or type(original.get("size_bytes")) is not int
        or not 0 < original["size_bytes"] <= 10_000_000
        or len(result.raw) != 1
        or result.raw[0].content != original
        or result.raw[0].provenance_id != source.provenance_id
        or result.raw[0].listing_id != listing.listing_id
    ):
        raise ValueError(
            "Portable declaration does not bind the exact original upload and mapping"
        )
    return {
        "schema_version": DECLARATION_VERSION,
        "data_evidence": EVIDENCE,
        "listing_id": listing.listing_id,
        "start": str(request.start),
        "end": str(request.end),
        "declaration": mapping.portable_declaration.model_dump(mode="json"),
        "source": original,
        "provenance_id": source.provenance_id,
        "price_semantics": result.price_semantics.model_dump(mode="json"),
    }


def declared_action_coverage(evidence: dict) -> dict:
    return {
        "schema_version": "marketdata.corporate-action-coverage.v2",
        "start": evidence["start"],
        "end": evidence["end"],
        "listings": [
            {
                "listing_id": evidence["listing_id"],
                "status": "declared_none",
                "provenance_ids": [evidence["provenance_id"]],
                "evidence": EVIDENCE,
            }
        ],
        "is_synthetic": False,
        "generator_version": None,
        "declaration": {
            "artifact": DECLARATION_FILE,
            "sha256": hashlib.sha256(canonical_json(evidence)).hexdigest(),
        },
    }
