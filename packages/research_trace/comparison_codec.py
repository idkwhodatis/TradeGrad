"""Versioned lossless physical rows, independent of comparison semantics.

The public ComparisonRecord and its canonical bytes do not change. Only known
top-level defaults and values already present in the same Parquet row are
omitted. In particular neither arbitrary observation dictionaries nor either
side's evidence is simplified, merged, or interpreted by this codec.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache

from .comparison_contracts import ComparisonError, ComparisonRecord
from .contracts import canonical_bytes

PAYLOAD_CODEC = "tradegrad.comparison-row.v2"
PAYLOAD_ACCOUNTING_VERSION = "encoded_payload_and_published_artifacts.v2"
PROJECTED_FIELDS = (
    "section",
    "event_pair_id",
    "session_date",
    "instrument_id",
    "stage_id",
    "status",
)
# Frozen codec defaults, not dynamically derived from a future public model.
_DEFAULTS = {"ambiguous": False, "associations": [], "missing_reason": None}
_REQUIRED = {"row_id", "left", "right", "left_state", "right_state", "match_basis"}
_PREFIX = b'["tradegrad.comparison-row.v2",'
_KEY_BYTES = {key: len(canonical_bytes(key)) + 2 for key in PROJECTED_FIELDS}
_DEFAULT_BYTES = {
    key: len(canonical_bytes(key)) + len(canonical_bytes(value)) + 2
    for key, value in _DEFAULTS.items()
}


@lru_cache(maxsize=4096)
def _short_scalar_size(value):
    return len(canonical_bytes(value))


def _scalar_size(value):
    # Repeated event, date, instrument and status labels dominate normal rows.
    # Cache only short fixed-schema scalars, with a strict key-count/length cap;
    # never retain arbitrary evidence or unbounded projected strings.
    if value is None or len(value) <= 200:
        return _short_scalar_size(value)
    return len(canonical_bytes(value))


def encode_record(record):
    """Return stored bytes and the exact expanded canonical byte count.

    Partitioning top-level fields lets us count the full canonical JSON without
    serializing every nested observation twice. JSON object field ordering has
    no effect on length. All nested content still uses canonical_bytes, including
    its rejection of nonfinite values, never a serializer that maps NaN to null.
    """
    # ComparisonRecord preserves nonfinite constants inside Any, allowing the
    # strict canonical encoder to reject them instead of silently writing null.
    body = record.model_dump(mode="json")
    omitted_bytes = sum(
        _KEY_BYTES[key] + _scalar_size(body.pop(key)) for key in PROJECTED_FIELDS
    )
    for key, default in _DEFAULTS.items():
        if body[key] == default:
            body.pop(key)
            omitted_bytes += _DEFAULT_BYTES[key]
    raw = canonical_bytes(body)
    expanded_bytes = len(raw) + omitted_bytes
    return _PREFIX + raw + b"]", expanded_bytes


def _scalar_size_sql(field):
    """Exact ensure_ascii JSON scalar length in bounded DuckDB, before fetch.

    DuckDB to_json already escapes controls/quotes but retains Unicode. Replace
    each non-ASCII UTF-8 character by six ASCII bytes, or twelve for an astral
    character's surrogate pair. All field identifiers are fixed by this module.
    """
    non_ascii = f"regexp_replace({field}, '[\\x00-\\x7E]', '', 'g')"
    astral = f"regexp_replace({field}, '[\\x00-\\x{{FFFF}}]', '', 'g')"
    return (
        f"CASE WHEN {field} IS NULL THEN 4 ELSE "
        f"octet_length(encode(to_json({field})))"
        f" + 6 * length({non_ascii}) - octet_length(encode({non_ascii}))"
        f" + 6 * length({astral}) END"
    )


def expanded_size_sql(section):
    """Authoritative full public size, using only fixed-schema SQL projections.

    New payloads use their canonical stored core and frozen omitted fields.
    Legacy payloads already contain the full record. Declared metadata is never
    sufficient to admit a page: the reader compares it with this physical size.
    """
    additions = []
    for field in PROJECTED_FIELDS:
        scalar = (
            str(len(canonical_bytes(section)))
            if field == "section"
            else _scalar_size_sql(field)
        )
        additions.append(f"({len(canonical_bytes(field)) + 2} + {scalar})")
    for field, value in _DEFAULTS.items():
        extra = len(canonical_bytes(field)) + len(canonical_bytes(value)) + 2
        additions.append(
            f"CASE WHEN json_exists(payload, '$[1].{field}') THEN 0 ELSE {extra} END"
        )
    return (
        "CASE WHEN starts_with(payload, '{') THEN octet_length(encode(payload)) ELSE "
        f"octet_length(encode(payload)) - {len(_PREFIX) + 1} + "
        + " + ".join(additions)
        + " END"
    )


def unsafe_numeric_sql():
    """Reject exponent spellings that can expand beyond the raw-core count.

    All genuine writer JSON numbers pass. Admit fixed-point and scientific
    spellings only in Python's respective representation ranges. Within each
    range its shortest round-trip spelling cannot add significant digits; a
    rounding carry only shortens the mantissa. Cross-range noncanonical forms
    (e.g. 1e15 or 1000000000000000128.0) could grow and are rejected. Strip JSON
    strings first so arbitrary evidence text is unaffected.
    """
    strings = r"\"(?:[^\"\\]|\\.)*\""
    number = r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
    scientific = (
        r"-?[1-9](?:\.[0-9]+)?e(?:\+(?:1[6-9]|[2-9][0-9]|[12][0-9]{2}|30[0-8])"
        r"|-(?:0[5-9]|[1-9][0-9]|[12][0-9]{2}|3[01][0-9]|32[0-4]))"
    )
    fixed = r"-?(?:[1-9][0-9]{0,15}\.[0-9]+|0\.0|0\.0{0,3}[1-9][0-9]*)"
    return (
        f"len(list_filter(regexp_extract_all(regexp_replace(payload, '{strings}', '', 'g'), '{number}'), "
        f"number -> (contains(number, '.') OR contains(number, 'e') OR contains(number, 'E')) AND NOT regexp_full_match(number, '(?:{fixed}|{scientific})'))) > 0"
    )


def _unique_object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ComparisonError()
        output[key] = value
    return output


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ComparisonError()
    return result


def decode_record(payload, projected, expanded_bytes):
    """Decode an old full JSON object or the explicitly versioned physical row.

    The caller preflights expanded_bytes before loading the page. Rechecking it
    against the reconstructed public record also rejects corrupt size metadata.
    No public model/manifest version changes or historical hash rewrites occur.
    """
    if (
        not payload.isascii()
        or (b"\x7f" if isinstance(payload, bytes) else "\x7f") in payload
    ):
        # Both historical canonical JSON and this codec are ASCII escaped.
        # Noncanonical UTF-8 must not amplify past the preflight payload count.
        raise ComparisonError()
    value = json.loads(
        payload,
        object_pairs_hook=_unique_object,
        parse_float=_finite_float,
        parse_constant=lambda _: (_ for _ in ()).throw(ComparisonError()),
    )
    if isinstance(value, dict):
        # Historical payloads already contain every public field. Do not fill
        # them from a new codec or reinterpret their exact evidence.
        if value.keys() != (_REQUIRED | _DEFAULTS.keys() | set(PROJECTED_FIELDS)):
            raise ComparisonError()
        record = ComparisonRecord.model_validate(value, strict=True)
    elif (
        isinstance(value, list)
        and len(value) == 2
        and value[0] == PAYLOAD_CODEC
        and isinstance(value[1], dict)
        and type(expanded_bytes) is int
        and expanded_bytes > 0
    ):
        body = value[1]
        if not _REQUIRED <= body.keys() or body.keys() - (_REQUIRED | _DEFAULTS.keys()):
            raise ComparisonError()
        if set(projected) != set(PROJECTED_FIELDS):
            raise ComparisonError()
        record = ComparisonRecord.model_validate(
            {**_DEFAULTS, **body, **projected}, strict=True
        )
    else:
        raise ComparisonError()
    raw_size = len(canonical_bytes(record))
    if expanded_bytes is not None and (
        type(expanded_bytes) is not int or expanded_bytes != raw_size
    ):
        raise ComparisonError()
    if any(getattr(record, key) != value for key, value in projected.items()):
        raise ComparisonError()
    return record, raw_size
