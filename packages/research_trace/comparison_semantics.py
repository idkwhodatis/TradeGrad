"""Fixed observed-value comparison; identifiers and missing states stay exact."""

from __future__ import annotations

import math
import sys
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from .comparison_contracts import DEFAULT_LIMITS, FIELD_TOLERANCES
from .contracts import canonical_bytes


def observed_value(record):
    # Provenance/code anchors are retained in output, but are not a change to the
    # already computed observation. This is not generic recursive ID stripping.
    return {
        key: value
        for key, value in record.items()
        if key not in {"source", "source_ref"}
    }


@lru_cache(maxsize=128)
def _short_provenance_size(value):
    return len(canonical_bytes(value))


def observation_payload_size(record, semantic_size):
    """Reconstruct exact payload size from its provenance-free canonical bytes."""
    removed = size = 0
    for key in ("source", "source_ref"):
        if key not in record:
            continue
        value = record[key]
        removed += 1
        value_size = (
            _short_provenance_size(value)
            if value is None or (type(value) is str and len(value) <= 200)
            else len(canonical_bytes(value))
        )
        size += len(key) + 3 + value_size  # quoted ASCII key plus colon
    remaining = len(record) - removed
    return semantic_size + size + max(len(record) - 1, 0) - max(remaining - 1, 0)


@dataclass(frozen=True, slots=True)
class CanonicalObservation:
    """One semantic representation, replacing the copied observation dictionary.

    These are exact canonical bytes of JSON-decoded source values with only
    source/source_ref omitted. Retain bytes instead of another dictionary, not
    an extra raw payload or cache. The source reference points to the record
    already retained for output, preserving its original traversal order when
    non-exact tolerance matching needs the shallow semantic dictionary again.
    """

    raw: bytes
    record: dict


@dataclass(frozen=True, slots=True)
class SourceObservation:
    """A canonical source string instead of a copied semantic dictionary.

    The string is the original, already-admitted SQL payload, verified equal to
    its strict ASCII canonical encoding, so character counts are byte counts.
    The record is the existing decoded source,
    never another copy. Allocation admission includes the enclosing compact
    observation, and any reuse-index allocation is charged separately.
    """

    raw: str
    record: dict
    semantic_size: int


@dataclass(frozen=True, slots=True)
class TraceObservation:
    ordinal: int
    record: dict
    semantic: object

    def __getitem__(self, key):
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class TraceInputRow:
    stage_id: str
    ordinal: int
    payload: dict
    semantic: object

    def __getitem__(self, key):
        return self.semantic if key == "_semantic" else getattr(self, key)


@dataclass(frozen=True, slots=True)
class SourceReuse:
    stage_id: str
    observations: list


class SharedJSONRecord(dict):
    """Private producer witness: both sides are the same decoded JSON evidence."""

    __slots__ = ("source",)


_ITEM_DICT_SIZE = sys.getsizeof({"ordinal": 0, "record": None, "semantic": None})
_TRACE_ITEM_SIZE = sys.getsizeof(TraceObservation(0, {}, None))
_TRACE_INPUT_SIZE = sys.getsizeof(TraceInputRow("", 0, {}, None))
_CANONICAL_WRAPPER_SIZE = sys.getsizeof(CanonicalObservation(b"", {}))
_BYTES_SIZE = sys.getsizeof(b"")
_SOURCE_WRAPPER_SIZE = sys.getsizeof(SourceObservation("", {}, 0))
# Optimization eligibility only, never an input/budget limit. Larger sources
# take the unchanged representation; do not materialize a second large source.
_MAX_SOURCE_REUSE_BYTES = 4096
_SMALL_DICT_SIZES = tuple(
    sys.getsizeof({str(index): None for index in range(count)}) for count in range(33)
)


def source_semantic_size(record, payload_size):
    # observation_payload_size adds only exact provenance members/separators.
    return payload_size - observation_payload_size(record, 0)


def source_retention_possible(raw, semantic, row=None):
    """Reject unaffordable sources before any full canonical source allocation."""
    return (
        len(raw) <= _MAX_SOURCE_REUSE_BYTES
        and raw.isascii()
        and (
            row is None
            or sys.getsizeof(row) - _TRACE_INPUT_SIZE
            >= _ITEM_DICT_SIZE - _TRACE_ITEM_SIZE
        )
        and _SOURCE_WRAPPER_SIZE + sys.getsizeof(raw) + sys.getsizeof(len(raw))
        <= _ITEM_DICT_SIZE - _TRACE_ITEM_SIZE + sys.getsizeof(semantic)
    )


def source_retention_saving(value, semantic):
    """Actual retained allocation saved versus the previous representation."""
    previous_semantic_size = min(
        sys.getsizeof(semantic),
        _CANONICAL_WRAPPER_SIZE + _BYTES_SIZE + value.semantic_size,
    )
    return (
        _ITEM_DICT_SIZE
        + previous_semantic_size
        - _TRACE_ITEM_SIZE
        - sys.getsizeof(value)
        - sys.getsizeof(value.raw)
        - sys.getsizeof(value.semantic_size)
    )


def trace_retention_saving(observation):
    value = observation.semantic
    if isinstance(value, SourceObservation):
        count = (
            len(value.record)
            - ("source" in value.record)
            - ("source_ref" in value.record)
        )
        # JSON-decoded keys are plain strings. A fresh comprehension's compact
        # dictionary allocation depends only on its insertion count; derive the
        # small sizes on this interpreter, rather than hard-code a CPython ABI.
        previous_dict_size = (
            _SMALL_DICT_SIZES[count]
            if count < len(_SMALL_DICT_SIZES)
            else sys.getsizeof(observed_value(value.record))
        )
        return (
            _ITEM_DICT_SIZE
            + min(
                previous_dict_size,
                _CANONICAL_WRAPPER_SIZE + _BYTES_SIZE + value.semantic_size,
            )
            - _TRACE_ITEM_SIZE
            - sys.getsizeof(value)
            - sys.getsizeof(value.raw)
            - sys.getsizeof(value.semantic_size)
        )
    # The ordinary representation is unchanged; only the item container shrank.
    return _ITEM_DICT_SIZE - _TRACE_ITEM_SIZE


def release_source_observation(observation):
    """Restore the original semantic representation before duplicate matching.

    Counter matching can retain one canonical byte key per distinct value. Do
    not keep an additional raw source string beside those keys for a whole
    duplicate group. Conversion releases each small source before the next;
    the right side then also takes the ordinary, independently decoded path.
    """
    if not isinstance(observation.semantic, SourceObservation):
        return observation
    semantic = observed_value(observation.record)
    encoded = CanonicalObservation(canonical_bytes(semantic), observation.record)
    if sys.getsizeof(encoded) + sys.getsizeof(encoded.raw) <= sys.getsizeof(semantic):
        semantic = encoded
    return TraceObservation(observation.ordinal, observation.record, semantic)


def _canonical_observation(value):
    if isinstance(value, SourceObservation):
        return canonical_bytes(observed_value(value.record))
    return (
        value.raw if isinstance(value, CanonicalObservation) else canonical_bytes(value)
    )


def _decoded_observation(value):
    return (
        observed_value(value.record)
        if isinstance(value, (CanonicalObservation, SourceObservation))
        else value
    )


# Only these schema-defined collections are unordered. In particular arbitrary
# observation arrays, source event tuples and predicate operands stay positional.
_GRAPH_MULTISET_PATHS = {
    ("order_graph",),
    ("order_graph", "[]", "children"),
    ("order_graph", "[]", "explicit_fills"),
    ("order_graph", "[]", "children", "[]", "fills"),
}


class _MatchingWork:
    def __init__(self, control, graph_semantics=False):
        self.control = control
        self.graph_semantics = graph_semantics
        self.nodes = DEFAULT_LIMITS.semantic_matching_nodes
        self.bytes = DEFAULT_LIMITS.semantic_matching_bytes

    def spend(self, *, size=0):
        if self.control is not None and self.nodes % 256 == 0:
            self.control.check()
        self.nodes -= 1
        self.bytes -= size
        return self.nodes >= 0 and self.bytes >= 0


def _all_known(values):
    unknown = False
    for value in values:
        if value is False:
            return False
        unknown |= value is None
    return None if unknown else True


def equal_value(
    left, right, field=None, *, path=(), control=None, graph_semantics=False, _work=None
):
    """Schema-aware equality, with None for bounded but unresolved multisets."""
    work = _work if _work is not None else _MatchingWork(control, graph_semantics)
    if not work.spend():
        return None
    if type(left) in (float, int) and type(right) in (float, int):
        if type(left) is int and type(right) is int:
            return left == right
        tolerance = FIELD_TOLERANCES.get(field)
        return (
            math.isclose(
                left,
                right,
                rel_tol=tolerance["relative"],
                abs_tol=tolerance["absolute"],
            )
            if tolerance
            else left == right
        )
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return False
        return _all_known(
            equal_value(left[key], right[key], key, path=(*path, key), _work=work)
            for key in left
        )
    if isinstance(left, list):
        if len(left) != len(right):
            return False
        if work.graph_semantics and path in _GRAPH_MULTISET_PATHS:
            return multiset_equal(left, right, path=(*path, "[]"), _work=work)
        return _all_known(
            equal_value(a, b, field, path=(*path, "[]"), _work=work)
            for a, b in zip(left, right, strict=True)
        )
    return left == right


def multiset_equal(
    left, right, *, path=(), control=None, graph_semantics=False, _work=None
):
    """Return True/False/None without fabricating row-to-row identity.

    Exact canonical buckets handle arbitrary multiplicity cheaply. Bounded
    numerical-tolerance graphs are matched for existence only; output remains a
    group with every original ordinal, never an arbitrary mapping. Larger
    non-exact duplicate groups are explicitly ambiguous, not truncated.
    """
    work = _work if _work is not None else _MatchingWork(control, graph_semantics)
    if not work.spend():
        return None
    if len(left) != len(right):
        return False
    if len(left) == 1:
        # Preserve the exact same canonical byte and work charges as the general
        # path, without allocating two Counters for the common singleton group.
        if (
            isinstance(left[0], SourceObservation)
            and isinstance(right[0], SourceObservation)
            and left[0].raw == right[0].raw
        ):
            # Full canonical source identity is stronger than semantic identity.
            # Charge precisely the original two semantic encodings, with the
            # same short-circuit order and cancellation checks, before returning.
            if not work.spend(size=left[0].semantic_size):
                return None
            if not work.spend(size=right[0].semantic_size):
                return None
            return True
        raw_left = _canonical_observation(left[0])
        if not work.spend(size=len(raw_left)):
            return None
        raw_right = _canonical_observation(right[0])
        if not work.spend(size=len(raw_right)):
            return None
        if raw_left == raw_right:
            return True
        return equal_value(
            _decoded_observation(left[0]),
            _decoded_observation(right[0]),
            path=path,
            _work=work,
        )
    counters = []
    for rows in (left, right):
        counter = Counter()
        for row in rows:
            raw = _canonical_observation(row)
            if not work.spend(size=len(raw)):
                return None
            counter[raw] += 1
        counters.append(counter)
    if counters[0] == counters[1]:
        return True
    # Tolerances are nontransitive: consuming exact pairs greedily can destroy
    # another valid perfect matching. Match the full bounded bipartite graph.
    if len(left) > DEFAULT_LIMITS.tolerant_multiset_max_size:
        return None
    left = [_decoded_observation(value) for value in left]
    right = [_decoded_observation(value) for value in right]
    matrix = [
        [equal_value(row, other, path=path, _work=work) for other in right]
        for row in left
    ]

    class WorkExhausted(Exception):
        pass

    def perfect_match(*, allow_unknown):
        if not work.spend():
            return None
        edges = [
            [
                index
                for index, same in enumerate(row)
                if same is True or (allow_unknown and same is None)
            ]
            for row in matrix
        ]
        matched = {}

        def augment(index, visited):
            for other in edges[index]:
                if not work.spend():
                    raise WorkExhausted
                if other in visited:
                    continue
                visited.add(other)
                if other not in matched or augment(matched[other], visited):
                    matched[other] = index
                    return True
            return False

        try:
            return all(augment(index, set()) for index in range(len(left)))
        except WorkExhausted:
            return None

    known = perfect_match(allow_unknown=False)
    if known is True or known is None:
        return known
    # An unknown nested graph must not become a false observed difference.
    return False if perfect_match(allow_unknown=True) is False else None


def group_record(
    section,
    key,
    left,
    right,
    *,
    left_state,
    right_state,
    event_pair_id=None,
    session_date=None,
    stage_id=None,
    instrument_id=None,
    basis="semantic_multiset",
    reason=None,
    control=None,
    graph_semantics=False,
):
    from .contracts import digest

    lvalues = [
        row.semantic if isinstance(row, TraceObservation) else row["semantic"]
        for row in left
    ]
    rvalues = [
        row.semantic if isinstance(row, TraceObservation) else row["semantic"]
        for row in right
    ]
    same = (
        multiset_equal(
            lvalues, rvalues, control=control, graph_semantics=graph_semantics
        )
        if left and right
        else None
    )
    ambiguous = len(left) > 1 or len(right) > 1 or bool(left and right and same is None)
    status = (
        ("equal" if same is True else "ambiguous" if same is None else "changed")
        if left and right
        else "left_only"
        if left
        else "right_only"
        if right
        else "unavailable"
    )

    def exposed(rows):
        return (
            {
                "items": [
                    {"ordinal": item.ordinal, "record": item.record}
                    if isinstance(item, TraceObservation)
                    else {
                        key: value for key, value in item.items() if key != "semantic"
                    }
                    for item in rows
                ],
                "count": len(rows),
            }
            if rows
            else None
        )

    shared = (
        len(left) == len(right) == 1
        and isinstance(left[0], TraceObservation)
        and isinstance(right[0], TraceObservation)
        and isinstance(left[0].semantic, SourceObservation)
        and left[0].semantic is right[0].semantic
        and left[0].record is right[0].record
        and type(left[0].ordinal) is type(right[0].ordinal)
        and left[0].ordinal == right[0].ordinal
    )
    left_body = exposed(left)
    result = {
        "row_id": digest([section, event_pair_id, key]),
        "section": section,
        "event_pair_id": event_pair_id,
        "session_date": session_date,
        "instrument_id": instrument_id,
        "stage_id": stage_id,
        "status": status,
        "left_state": "observed" if left else left_state,
        "right_state": "observed" if right else right_state,
        "left": left_body,
        "right": left_body if shared else exposed(right),
        "match_basis": basis,
        "ambiguous": ambiguous,
        "missing_reason": reason
        or (
            "Bounded semantic matching is unresolved; every original ordinal is retained and no arbitrary association is asserted."
            if left and right and same is None
            else "Duplicate semantic identities retain all ordinals as a multiset; no arbitrary row-to-row association is asserted."
            if ambiguous
            else None
        ),
    }
    if shared:
        result = SharedJSONRecord(result)
        result.source = left[0].semantic
    return result
