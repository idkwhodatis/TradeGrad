"""SDK-free V1 venue metadata and explicit calendar/proxy compatibility.

exchange-calendars 4.13.2 implements XNAS as an alias of XNYS. Both US
providers persist XNYS while preserving the actual MIC. Shenzhen intentionally
uses the declared XSHG mainland regular-equity schedule proxy, never weekdays.
"""

_VENUES = {
    "XNAS": ("XNYS", {"XNYS", "XNAS"}, "USD", "America/New_York"),
    "XNYS": ("XNYS", {"XNYS", "XNAS"}, "USD", "America/New_York"),
    "XASE": ("XNYS", {"XNYS", "XNAS"}, "USD", "America/New_York"),
    "ARCX": ("XNYS", {"XNYS", "XNAS"}, "USD", "America/New_York"),
    "BATS": ("XNYS", {"XNYS", "XNAS"}, "USD", "America/New_York"),
    "IEXG": ("XNYS", {"XNYS", "XNAS"}, "USD", "America/New_York"),
    "XSHG": ("XSHG", {"XSHG"}, "CNY", "Asia/Shanghai"),
    "XSHE": ("XSHG", {"XSHG"}, "CNY", "Asia/Shanghai"),
}


def canonical_calendar(
    mic: str, calendar: str, *, currency: str | None = None, timezone: str | None = None
) -> str:
    if mic not in _VENUES:
        raise ValueError("Unsupported V1 listing venue")
    canonical, accepted, expected_currency, expected_timezone = _VENUES[mic]
    if calendar not in accepted:
        raise ValueError("Calendar does not match the listing venue or declared proxy")
    if currency not in {None, "unknown", expected_currency}:
        raise ValueError("Currency does not match the listing venue")
    if timezone not in {None, expected_timezone}:
        raise ValueError("Timezone does not match the listing venue")
    return canonical


def validate_listing_calendar(mic: str, calendar: str) -> str:
    return canonical_calendar(mic, calendar)
