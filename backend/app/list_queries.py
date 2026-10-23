"""Allowlisted SQL expressions for owned listing and candidate pages."""

from sqlalchemy import func, select

from backend.app.models import Candidate, Listing, OwnerListing
from backend.app.services import DomainError

CANDIDATE_SORTS = {
    name: getattr(Candidate, name)
    for name in (
        "created_at",
        "candidate_key",
        "generation",
        "iteration",
        "repair_index",
        "kind",
    )
}
LISTING_SORTS = {
    name: getattr(Listing, name)
    for name in (
        "symbol",
        "mic",
        "currency",
        "created_at",
    )
}


def _order(query, allowed, name, direction, identifier):
    if name not in allowed or direction not in {"asc", "desc"}:
        raise DomainError(
            "invalid_list_query", "Unsupported list sort field or direction", 422
        )
    column = allowed[name]
    return query.order_by(
        column.desc() if direction == "desc" else column.asc(), identifier.asc()
    )


def _search(value, maximum=128):
    if value is not None and (not isinstance(value, str) or len(value) > maximum):
        raise DomainError("invalid_list_query", "List filter exceeds its bounds", 422)
    return value.strip() if value else None


def candidate_query(
    run_id,
    *,
    sort_by="created_at",
    sort_direction="asc",
    search=None,
    kind=None,
    min_iteration=None,
):
    search = _search(search)
    if kind not in {None, "seed", "generated", "repair"} or (
        min_iteration is not None
        and (type(min_iteration) is not int or not 0 <= min_iteration <= 100000)
    ):
        raise DomainError("invalid_list_query", "Unsupported candidate filter", 422)
    query = select(Candidate).where(Candidate.run_id == run_id)
    if search:
        query = query.where(Candidate.candidate_key.icontains(search, autoescape=True))
    if kind is not None:
        query = query.where(Candidate.kind == kind)
    if min_iteration is not None:
        query = query.where(Candidate.iteration >= min_iteration)
    return _order(query, CANDIDATE_SORTS, sort_by, sort_direction, Candidate.id)


def listing_query(
    owner_id,
    *,
    listing_ids=None,
    sort_by="symbol",
    sort_direction="asc",
    search=None,
    mic=None,
    currency=None,
):
    search, mic, currency = _search(search), _search(mic, 4), _search(currency, 16)
    query = (
        select(Listing)
        .join(OwnerListing, OwnerListing.listing_id == Listing.id)
        .where(OwnerListing.owner_id == owner_id)
    )
    if listing_ids:
        query = query.where(Listing.id.in_(listing_ids))
    if search:
        query = query.where(Listing.symbol.icontains(search, autoescape=True))
    if mic:
        query = query.where(Listing.mic == mic.upper())
    if currency:
        query = query.where(Listing.currency == currency.upper())
    return _order(query, LISTING_SORTS, sort_by, sort_direction, Listing.id)


def query_page(session, query, offset, limit):
    if (
        type(offset) is not int
        or not 0 <= offset <= 100000
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise DomainError("invalid_list_query", "List page exceeds bounds", 422)
    return {
        "data": session.scalars(query.offset(offset).limit(limit)).all(),
        "count": session.scalar(
            select(func.count()).select_from(query.order_by(None).subquery())
        ),
    }
