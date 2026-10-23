"""Scoped probe receipts. A successful fixture never verifies a live capability."""

import re
from datetime import UTC, datetime


def build_probe_receipt(
    *, import_id, provider, request, provenance, warnings, complete
):
    versions = {}
    for source in provenance:
        versions.update(source.get("package_versions", {}))
    evidence = {source.get("evidence") for source in provenance}
    live = evidence == {"live"}
    fixture = evidence == {"fixture"}
    known_versions = bool(versions) and all(
        value not in {"not-installed", "unknown", ""} for value in versions.values()
    )
    errors = sorted(
        {
            w["code"]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,79}", w["code"])
            else "provider_test_failed"
            for w in warnings
            if w.get("severity") == "error" and isinstance(w.get("code"), str)
        }
    )[:20]
    verified = complete and not errors and live and known_versions
    outcome = (
        "verified"
        if verified
        else "fixture_passed"
        if complete and fixture and not errors
        else "failed"
        if errors
        else "incomplete"
    )
    receipt = {
        "schema_version": "provider-test.v1",
        "import_id": str(import_id),
        "provider": provider,
        "outcome": outcome,
        "capability_status": "verified"
        if verified
        else "documented"
        if outcome == "fixture_passed"
        else "unknown",
        "evidence": "live" if live else "fixture" if fixture else "none",
        "symbol": request["symbols"][0],
        "start_date": request["start_date"],
        "end_date": request["end_date"],
        "checked_at": datetime.now(UTC).isoformat(),
        "endpoints": sorted(
            {source["endpoint"] for source in provenance if source.get("endpoint")}
        ),
        "package_versions": versions,
        "error_codes": errors,
        "note": "Only this public symbol, daily endpoint, package combination and closed date interval were tested. No global provider, credentials, data-use rights or current availability claim. Fixture results never verify live connectivity.",
    }

    if request.get("_provider_connection") is not None:
        receipt["provider_connection"] = request["_provider_connection"]
        receipt["price_mode"] = request["price_mode"]
    return receipt
