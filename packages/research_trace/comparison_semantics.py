"""Fixed observed-value comparison; identifiers and missing states stay exact."""

from __future__ import annotations

import math
from collections import Counter

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
    counters = []
    for rows in (left, right):
        counter = Counter()
        for row in rows:
            raw = canonical_bytes(row)
            if not work.spend(size=len(raw)):
                return None
            counter[raw] += 1
        counters.append(counter)
    if counters[0] == counters[1]:
        return True
    if len(left) == len(right) == 1:
        return equal_value(left[0], right[0], path=path, _work=work)
    # Tolerances are nontransitive: consuming exact pairs greedily can destroy
    # another valid perfect matching. Match the full bounded bipartite graph.
    if len(left) > DEFAULT_LIMITS.tolerant_multiset_max_size:
        return None
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

    lvalues = [row["semantic"] for row in left]
    rvalues = [row["semantic"] for row in right]
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
                    {key: value for key, value in item.items() if key != "semantic"}
                    for item in rows
                ],
                "count": len(rows),
            }
            if rows
            else None
        )

    return {
        "row_id": digest([section, event_pair_id, key]),
        "section": section,
        "event_pair_id": event_pair_id,
        "session_date": session_date,
        "instrument_id": instrument_id,
        "stage_id": stage_id,
        "status": status,
        "left_state": "observed" if left else left_state,
        "right_state": "observed" if right else right_state,
        "left": exposed(left),
        "right": exposed(right),
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
