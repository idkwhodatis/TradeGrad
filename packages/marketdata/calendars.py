"""Explicit exchange sessions, with no weekday fallback for absent calendars."""

from datetime import date, datetime, timedelta
from importlib.metadata import version

from .contracts import Bar, BarsRequest, Coverage, Listing


class CalendarUnavailable(ValueError):
    pass


def session_calendar(listing: Listing, start: date, end: date):
    import exchange_calendars as xc

    try:
        # Explicit bounds avoid exchange_calendars' moving default 20-year window.
        template = xc.get_calendar(listing.calendar)
        lower, upper = template.bound_min(), template.bound_max()
        if (lower is not None and start < lower.date()) or (
            upper is not None and end - timedelta(days=1) > upper.date()
        ):
            raise CalendarUnavailable(
                "Requested interval exceeds the published calendar"
            )
        padded_start, padded_end = start - timedelta(days=7), end + timedelta(days=7)
        if lower is not None:
            padded_start = max(padded_start, lower.date())
        if upper is not None:
            padded_end = min(padded_end, upper.date())
        calendar = type(template)(start=padded_start, end=padded_end)
        sessions = calendar.sessions[
            (calendar.sessions.date >= start) & (calendar.sessions.date < end)
        ]
        return calendar, sessions
    except Exception as exc:
        raise CalendarUnavailable(
            "Calendar or requested date range is unavailable"
        ) from exc


def is_provisional(calendar, session: date, as_of: datetime) -> bool:
    return calendar.session_close(session).to_pydatetime() > as_of


def coverage_for(
    listing: Listing,
    request: BarsRequest,
    bars: list[Bar],
    *,
    error_code: str | None = None,
) -> Coverage:
    observed = sorted({bar.session_date for bar in bars})
    common = {
        "listing_id": listing.listing_id,
        "requested_start": request.start,
        "requested_end": request.end,
        "calendar": listing.calendar,
        "observed_sessions": observed,
        "actual_start": min(observed) if observed else None,
        "actual_end": max(observed) + timedelta(days=1) if observed else None,
        "error_code": error_code,
    }
    try:
        calendar, index = session_calendar(listing, request.start, request.end)
    except CalendarUnavailable:
        return Coverage(
            **common,
            status="failed" if error_code else "unknown",
            missing_reason="calendar_unavailable; no synthetic business-day fallback",
        )
    sessions = [item.date() for item in index]
    before = [d for d in sessions if listing.listed_on and d < listing.listed_on]
    after = [d for d in sessions if listing.delisted_on and d >= listing.delisted_on]
    expected = [d for d in sessions if d not in before and d not in after]
    provisional = [d for d in expected if is_provisional(calendar, d, request.as_of)]
    missing = sorted(set(expected) - set(observed))
    non_sessions = sorted(set(observed) - set(sessions))
    incomplete = missing or provisional or non_sessions
    return Coverage(
        **common,
        calendar_version=version("exchange-calendars"),
        status="failed" if error_code else ("partial" if incomplete else "complete"),
        expected_sessions=expected,
        missing_sessions=missing,
        provisional_sessions=provisional,
        non_session_dates=non_sessions,
        before_listing_sessions=before,
        after_delisting_sessions=after,
        missing_reason="upstream_missing_or_suspension_unknown" if missing else None,
    )
