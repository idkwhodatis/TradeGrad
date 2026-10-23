"""Provider settings and admission against migrated, real PostgreSQL.

Collection is safe without a server. Execution uses the existing real API and
fresh-schema fixtures; it never substitutes SQLite, mocks a session, invokes a
provider SDK, or starts workers. Run execution in the hosted PostgreSQL gate.
"""

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.app.models import DataImport, DataOutbox, ProviderConnection

pytestmark = pytest.mark.postgres

CATALOG = "/api/v1/data/providers"
CONNECTIONS = "/api/v1/data/provider-connections"
PUBLIC_FIELDS = {
    "schema_version",
    "connection_id",
    "provider",
    "revision",
    "enabled",
    "is_default",
    "default_price_mode",
    "auth_mode",
    "config_sha256",
    "created_at",
}


def catalog(api, headers):
    response = api.get(CATALOG, headers=headers)
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()}


def connection(api, headers, provider):
    return catalog(api, headers)[provider]["connection"]


def update_body(current, **changes):
    return {
        "expected_revision": current["revision"],
        "enabled": current["enabled"],
        "is_default": current["is_default"],
        "default_price_mode": current["default_price_mode"],
        **changes,
    }


def save(api, headers, provider, **changes):
    current = connection(api, headers, provider)
    response = api.post(
        f"{CONNECTIONS}/{provider}",
        headers=headers,
        json=update_body(current, **changes),
    )
    assert response.status_code == 200, response.text
    return response.json()


def revision(api, headers, provider, number):
    return api.get(f"{CONNECTIONS}/{provider}/revisions/{number}", headers=headers)


def admission_request(kind, *, provider="yfinance", market="US"):
    symbol = "600000" if market == "CN" else "AAPL"
    if kind == "probe":
        return f"{CATALOG}/{provider}/test", {
            "symbol": symbol,
            "start_date": "2024-01-02",
            "end_date": "2024-01-06",
        }
    body = {"market": market, "symbols": [symbol]}
    if provider is not None:
        body["provider"] = provider
    if kind == "import":
        body.update(start_date="2024-01-02", end_date="2024-01-06")
    return f"/api/v1/data/{'imports' if kind == 'import' else 'resolve'}", body


def submit(api, headers, path, body, *, key=None):
    return api.post(
        path,
        headers={**headers, "Idempotency-Key": key or uuid4().hex},
        json=body,
    )


def counts(engine):
    with Session(engine) as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (DataImport, DataOutbox)
        )


def frozen_snapshot(value):
    return {key: item for key, item in value.items() if key != "created_at"}


def assert_snapshot(value, *, provider, persisted, public=True):
    assert set(value) == (PUBLIC_FIELDS if public else PUBLIC_FIELDS - {"created_at"})
    assert value["schema_version"] == "provider-connection.v1"
    assert str(UUID(value["connection_id"])) == value["connection_id"]
    assert value["provider"] == provider
    assert value["auth_mode"] == "public_no_auth"
    assert type(value["enabled"]) is type(value["is_default"]) is bool
    assert re.fullmatch(r"[0-9a-f]{64}", value["config_sha256"])
    hashed = {
        key: item
        for key, item in value.items()
        if key not in {"created_at", "config_sha256"}
    }
    expected_hash = hashlib.sha256(
        json.dumps(
            hashed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    assert value["config_sha256"] == expected_hash
    if persisted:
        assert value["revision"] >= 1
        if public:
            assert value["created_at"] is not None
    else:
        assert value["revision"] == 0
        if public:
            assert value["created_at"] is None


def test_catalog_has_stable_private_owner_defaults_without_creating_jobs(
    api, owner_headers, other_headers, migrated_engine
):
    first = catalog(api, owner_headers)
    repeated = catalog(api, owner_headers)
    foreign = catalog(api, other_headers)
    assert first == repeated
    for provider, is_default in (
        ("akshare", True),
        ("yfinance", True),
        ("openbb", False),
    ):
        item = first[provider]["connection"]
        assert_snapshot(item, provider=provider, persisted=False)
        assert item["enabled"] is True
        assert item["is_default"] is is_default
        assert item["default_price_mode"] == "provider_native"
        assert item["connection_id"] != foreign[provider]["connection"]["connection_id"]
        assert revision(api, owner_headers, provider, 1).status_code == 404
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ProviderConnection)) == 0
    assert counts(migrated_engine) == (0, 0)


def test_fixture_catalog_has_no_editable_connection(api, owner_headers):
    api.app.state.settings.data_provider_mode = "fixture"
    assert catalog(api, owner_headers)["portable_fixture"]["connection"] is None


def test_save_is_versioned_noop_is_stable_and_old_revision_remains_readable(
    api, owner_headers, migrated_engine
):
    original = connection(api, owner_headers, "akshare")
    first = save(api, owner_headers, "akshare", default_price_mode="qfq")
    assert_snapshot(first, provider="akshare", persisted=True)
    assert first["revision"] == 1
    assert first["connection_id"] == original["connection_id"]
    assert first["config_sha256"] != original["config_sha256"]
    unchanged = save(api, owner_headers, "akshare")
    assert unchanged == first
    second = save(api, owner_headers, "akshare", default_price_mode="hfq")
    assert second["revision"] == 2
    assert second["connection_id"] == first["connection_id"]
    assert second["config_sha256"] != first["config_sha256"]
    assert revision(api, owner_headers, "akshare", 1).json() == first
    assert revision(api, owner_headers, "akshare", 2).json() == second
    assert connection(api, owner_headers, "akshare") == second
    assert counts(migrated_engine) == (0, 0)


def test_history_and_updates_are_scoped_to_authenticated_owner(
    api, owner_headers, other_headers
):
    saved = save(api, owner_headers, "akshare", default_price_mode="qfq")
    assert revision(api, other_headers, "akshare", saved["revision"]).status_code == 404
    other_saved = save(api, other_headers, "akshare", default_price_mode="raw")
    assert other_saved["revision"] == saved["revision"] == 1
    assert other_saved["connection_id"] != saved["connection_id"]
    assert revision(api, other_headers, "akshare", 1).json() == other_saved
    assert revision(api, owner_headers, "akshare", 1).json() == saved
    assert connection(api, owner_headers, "akshare") == saved


def test_connection_read_and_write_require_authentication(api, owner_headers):
    current = connection(api, owner_headers, "akshare")
    assert api.get(CATALOG).status_code == 401
    assert (
        api.post(f"{CONNECTIONS}/akshare", json=update_body(current)).status_code == 401
    )
    assert api.get(f"{CONNECTIONS}/akshare/revisions/1").status_code == 401


@pytest.mark.parametrize("provider", ["portable_fixture", "unknown-provider"])
def test_only_supported_live_connections_can_be_configured(
    api, owner_headers, provider, migrated_engine
):
    initial = catalog(api, owner_headers)
    response = api.post(
        f"{CONNECTIONS}/{provider}",
        headers=owner_headers,
        json=update_body(initial["yfinance"]["connection"]),
    )
    assert response.status_code in {404, 422}, response.text
    assert catalog(api, owner_headers) == initial
    assert counts(migrated_engine) == (0, 0)


@pytest.mark.parametrize("number", [0, -1, 999])
def test_missing_or_nonpersisted_history_is_not_exposed(api, owner_headers, number):
    save(api, owner_headers, "akshare", default_price_mode="qfq")
    assert revision(api, owner_headers, "akshare", number).status_code in {404, 422}


@pytest.mark.parametrize(
    "changes",
    [
        {"api_key": "synthetic-rejected-value"},
        {"credential": "synthetic-rejected-value"},
        {"token": "synthetic-rejected-value"},
        {"password": "synthetic-rejected-value"},
        {"endpoint": "https://example.invalid/data"},
        {"base_url": "http://127.0.0.1:9/private"},
        {"owner_id": "00000000-0000-0000-0000-000000000001"},
        {"connection_id": "00000000-0000-0000-0000-000000000001"},
        {"auth_mode": "api_key"},
        {"config_sha256": "0" * 64},
        {"revision": 100},
        {"enabled": False, "is_default": True},
        {"default_price_mode": "repair"},
        {"default_price_mode": "raw"},
        {"default_price_mode": "qfq"},
        {"default_price_mode": "hfq"},
        {"expected_revision": -1},
    ],
)
def test_invalid_settings_cannot_persist_secrets_or_partial_changes(
    api, owner_headers, migrated_engine, changes
):
    before = catalog(api, owner_headers)
    response = api.post(
        f"{CONNECTIONS}/yfinance",
        headers=owner_headers,
        json=update_body(before["yfinance"]["connection"], **changes),
    )
    assert response.status_code == 422, response.text
    assert catalog(api, owner_headers) == before
    assert revision(api, owner_headers, "yfinance", 1).status_code == 404
    assert counts(migrated_engine) == (0, 0)


def test_stale_update_is_conflict_even_if_values_match_current(api, owner_headers):
    first = save(api, owner_headers, "akshare", default_price_mode="qfq")
    response = api.post(
        f"{CONNECTIONS}/akshare",
        headers=owner_headers,
        json=update_body(first, expected_revision=0),
    )
    assert response.status_code == 409, response.text
    assert connection(api, owner_headers, "akshare") == first
    assert revision(api, owner_headers, "akshare", 2).status_code == 404


def test_default_promotion_atomically_demotes_only_the_same_market(
    api, owner_headers, other_headers
):
    before = catalog(api, owner_headers)
    promoted = save(api, owner_headers, "openbb", is_default=True)
    after = catalog(api, owner_headers)
    demoted = after["yfinance"]["connection"]
    assert_snapshot(promoted, provider="openbb", persisted=True)
    assert_snapshot(demoted, provider="yfinance", persisted=True)
    assert promoted["is_default"] is True
    assert demoted["is_default"] is False
    assert demoted["connection_id"] == before["yfinance"]["connection"]["connection_id"]
    assert after["akshare"]["connection"] == before["akshare"]["connection"]
    assert catalog(api, other_headers)["yfinance"]["connection"]["is_default"] is True
    restored = save(api, owner_headers, "yfinance", is_default=True)
    after_restore = catalog(api, owner_headers)
    assert restored["revision"] == demoted["revision"] + 1
    assert after_restore["openbb"]["connection"]["is_default"] is False
    assert after_restore["openbb"]["connection"]["revision"] == promoted["revision"] + 1
    assert (
        revision(api, owner_headers, "openbb", promoted["revision"]).json() == promoted
    )
    assert (
        revision(api, owner_headers, "yfinance", demoted["revision"]).json() == demoted
    )


def test_parallel_edits_with_same_revision_cannot_overwrite_winner(api, owner_headers):
    initial = connection(api, owner_headers, "akshare")
    barrier = Barrier(2)

    def edit(mode):
        barrier.wait(timeout=10)
        return api.post(
            f"{CONNECTIONS}/akshare",
            headers=owner_headers,
            json=update_body(initial, default_price_mode=mode),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(edit, ["qfq", "hfq"]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(
        response.json() for response in responses if response.status_code == 200
    )
    assert winner["revision"] == 1
    assert connection(api, owner_headers, "akshare") == winner
    assert revision(api, owner_headers, "akshare", 1).json() == winner
    assert revision(api, owner_headers, "akshare", 2).status_code == 404


def test_parallel_market_default_promotions_leave_exactly_one_current_default(
    api, owner_headers, migrated_engine, owner
):
    initial = {
        "yfinance": save(api, owner_headers, "yfinance", is_default=False),
        "openbb": save(api, owner_headers, "openbb", is_default=False),
    }
    barrier = Barrier(2)

    def promote(provider):
        barrier.wait(timeout=10)
        return api.post(
            f"{CONNECTIONS}/{provider}",
            headers=owner_headers,
            json=update_body(initial[provider], is_default=True),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(promote, ["yfinance", "openbb"]))
    assert [response.status_code for response in responses] == [200, 200]
    current = catalog(api, owner_headers)
    assert sum(current[p]["connection"]["is_default"] for p in initial) == 1
    assert current["akshare"]["connection"]["revision"] == 0
    assert sorted(current[p]["connection"]["revision"] for p in initial) == [2, 3]
    with Session(migrated_engine) as session:
        rows = session.scalars(
            select(ProviderConnection).where(
                ProviderConnection.owner_id == owner.user.id
            )
        ).all()
        # Two initial revisions, two promotions, and one immutable demotion.
        assert len(rows) == 5


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE provider_connections SET config = config",
        "DELETE FROM provider_connections",
    ],
    ids=["update", "delete"],
)
def test_postgresql_trigger_prevents_mutating_historical_revisions(
    api, owner_headers, migrated_engine, statement
):
    first = save(api, owner_headers, "akshare", default_price_mode="qfq")
    current = save(api, owner_headers, "akshare", default_price_mode="hfq")
    with (
        pytest.raises(DBAPIError, match="immutable") as raised,
        migrated_engine.begin() as database,
    ):
        database.execute(
            text(
                statement
                + " WHERE connection_id = :connection_id AND revision = :revision"
            ),
            {
                "connection_id": UUID(first["connection_id"]),
                "revision": first["revision"],
            },
        )
    assert raised.value.orig.sqlstate == "P0001"
    assert revision(api, owner_headers, "akshare", first["revision"]).json() == first
    assert connection(api, owner_headers, "akshare") == current


@pytest.mark.parametrize("kind", ["import", "resolve"])
@pytest.mark.parametrize("market,provider", [("CN", "akshare"), ("US", "yfinance")])
def test_omitted_provider_uses_market_default_and_freezes_persisted_revision(
    api, owner_headers, owner, migrated_engine, kind, market, provider
):
    path, body = admission_request(kind, provider=None, market=market)
    response = submit(api, owner_headers, path, body)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["kind"] == kind
    assert job["provider"] == job["request"]["provider"] == provider
    frozen = job["request"]["_provider_connection"]
    assert_snapshot(frozen, provider=provider, persisted=True, public=False)
    assert frozen["enabled"] is True
    assert frozen["is_default"] is True
    assert job["request"]["_execution"]["worker_contract"] == "data-import.v3"
    saved = revision(api, owner_headers, provider, frozen["revision"])
    assert saved.status_code == 200, saved.text
    assert frozen_snapshot(saved.json()) == frozen
    with Session(migrated_engine) as session:
        record = session.get(DataImport, UUID(job["id"]))
        assert record.owner_id == owner.user.id
        assert record.request["_provider_connection"] == frozen
        events = session.scalars(
            select(DataOutbox).where(DataOutbox.import_id == record.id)
        ).all()
        assert len(events) == 1
        assert events[0].generation == 1
        assert events[0].dispatched_at is None


@pytest.mark.parametrize("selection", ["omitted", "null", "explicit"])
def test_import_price_defaults_are_frozen_without_overriding_explicit_choice(
    api, owner_headers, selection
):
    saved = save(api, owner_headers, "akshare", default_price_mode="qfq")
    path, body = admission_request("import", provider="akshare", market="CN")
    if selection != "omitted":
        body["price_mode"] = None if selection == "null" else "raw"
    response = submit(api, owner_headers, path, body)
    assert response.status_code == 202, response.text
    request = response.json()["request"]
    assert request["price_mode"] == ("raw" if selection == "explicit" else "qfq")
    assert request["_provider_connection"] == frozen_snapshot(saved)


@pytest.mark.parametrize("kind", ["import", "resolve", "probe"])
def test_disabled_connection_blocks_new_admission_without_partial_queue_state(
    api, owner_headers, migrated_engine, kind
):
    disabled = save(api, owner_headers, "yfinance", enabled=False, is_default=False)
    assert disabled["enabled"] is False
    path, body = admission_request(kind)
    response = submit(api, owner_headers, path, body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "provider_connection_disabled"
    assert counts(migrated_engine) == (0, 0)
    assert connection(api, owner_headers, "yfinance") == disabled


@pytest.mark.parametrize("kind", ["import", "resolve"])
def test_no_enabled_market_default_does_not_silently_fallback(
    api, owner_headers, migrated_engine, kind
):
    save(api, owner_headers, "yfinance", enabled=False, is_default=False)
    assert connection(api, owner_headers, "openbb")["enabled"] is True
    path, body = admission_request(kind, provider=None)
    response = submit(api, owner_headers, path, body)
    assert response.status_code == 422, response.text
    assert counts(migrated_engine) == (0, 0)
    assert connection(api, owner_headers, "openbb")["revision"] == 0


@pytest.mark.parametrize("kind", ["import", "resolve", "probe"])
def test_stale_expected_revision_rejects_admission_before_creating_job(
    api, owner_headers, migrated_engine, kind
):
    first = save(api, owner_headers, "yfinance", is_default=False)
    second = save(api, owner_headers, "yfinance", is_default=True)
    path, body = admission_request(kind)
    body["expected_connection_revision"] = first["revision"]
    rejected = submit(api, owner_headers, path, body)
    assert rejected.status_code == 409, rejected.text
    assert counts(migrated_engine) == (0, 0)
    body["expected_connection_revision"] = second["revision"]
    accepted = submit(api, owner_headers, path, body)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["request"]["_provider_connection"] == frozen_snapshot(second)
    assert counts(migrated_engine) == (1, 1)


@pytest.mark.parametrize("kind", ["import", "resolve"])
@pytest.mark.parametrize(
    "provider,market", [("akshare", "US"), ("yfinance", "CN"), ("openbb", "CN")]
)
def test_provider_market_mismatch_is_rejected_without_queue_admission(
    api, owner_headers, migrated_engine, kind, provider, market
):
    before = catalog(api, owner_headers)
    path, body = admission_request(kind, provider=provider, market=market)
    response = submit(api, owner_headers, path, body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "unsupported_market"
    assert counts(migrated_engine) == (0, 0)
    assert catalog(api, owner_headers) == before


@pytest.mark.parametrize("kind", ["import", "resolve", "probe"])
def test_quota_failure_rolls_back_automatic_connection_revision_and_outbox(
    api, owner_headers, owner, migrated_engine, kind
):
    from backend.app.models import UserWorkspaceSettings

    with Session(migrated_engine) as session:
        session.add(UserWorkspaceSettings(owner_id=owner.user.id, max_active_imports=0))
        session.commit()
    before = catalog(api, owner_headers)
    path, body = admission_request(kind)
    response = submit(api, owner_headers, path, body)
    assert response.status_code == 429, response.text
    assert response.json()["code"] == "import_quota"
    assert catalog(api, owner_headers) == before
    assert counts(migrated_engine) == (0, 0)
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ProviderConnection)) == 0


def test_parallel_replays_materialize_one_connection_revision_job_and_outbox(
    api, owner_headers, owner, migrated_engine
):
    path, body = admission_request("import", provider=None)
    key, barrier = uuid4().hex, Barrier(4)

    def admit(_):
        barrier.wait(timeout=10)
        return submit(api, owner_headers, path, body, key=key)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(admit, range(4)))
    assert all(response.status_code in {200, 202} for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    assert len({response.json()["id"] for response in responses}) == 1
    assert all(
        response.json()["request"]["_provider_connection"]["revision"] == 1
        for response in responses
    )
    assert counts(migrated_engine) == (1, 1)
    with Session(migrated_engine) as session:
        rows = session.scalars(
            select(ProviderConnection).where(
                ProviderConnection.owner_id == owner.user.id
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].provider == "yfinance"
        assert rows[0].revision == 1


@pytest.mark.parametrize("kind", ["import", "resolve", "probe"])
@pytest.mark.parametrize(
    "injected",
    [
        {"_provider_connection": {"revision": 999, "enabled": True}},
        {"_execution": {"worker_contract": "data-import.v1"}},
        {"endpoint": "https://example.invalid/adapter"},
        {"credential": "synthetic-rejected-value"},
    ],
    ids=["forged-pin", "contract-downgrade", "arbitrary-endpoint", "credential"],
)
def test_admission_cannot_supply_internal_binding_execution_or_credentials(
    api, owner_headers, migrated_engine, kind, injected
):
    path, body = admission_request(kind)
    response = submit(api, owner_headers, path, {**body, **injected})
    assert response.status_code == 422, response.text
    assert counts(migrated_engine) == (0, 0)
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ProviderConnection)) == 0


@pytest.mark.parametrize("kind", ["import", "resolve", "probe"])
def test_idempotent_replay_survives_connection_edit_disable_and_stale_revision(
    api, owner_headers, other_headers, migrated_engine, kind
):
    saved = save(api, owner_headers, "yfinance", is_default=False)
    path, body = admission_request(kind)
    body["expected_connection_revision"] = saved["revision"]
    key = uuid4().hex
    original = submit(api, owner_headers, path, body, key=key)
    assert original.status_code == 202, original.text
    job = original.json()
    with Session(migrated_engine) as session:
        record = session.get(DataImport, UUID(job["id"]))
        original_request, original_hash = (
            deepcopy(record.request),
            record.request_sha256,
        )
    save(api, owner_headers, "yfinance", enabled=False)
    replay = submit(api, owner_headers, path, body, key=key)
    assert replay.status_code in {200, 202}, replay.text
    assert replay.json()["id"] == job["id"]
    assert replay.json()["request"] == original_request
    conflict_body = (
        {**body, "symbol": "MSFT"} if kind == "probe" else {**body, "symbols": ["MSFT"]}
    )
    conflict = submit(api, owner_headers, path, conflict_body, key=key)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "idempotency_conflict"
    with Session(migrated_engine) as session:
        record = session.get(DataImport, UUID(job["id"]))
        assert record.request_sha256 == original_hash
        assert record.request == original_request
    assert counts(migrated_engine) == (1, 1)
    assert (
        api.get(f"/api/v1/data/imports/{job['id']}", headers=other_headers).status_code
        == 404
    )


@pytest.mark.parametrize("kind", ["import", "resolve"])
def test_replayed_implicit_default_is_original_but_new_job_uses_new_default(
    api, owner_headers, migrated_engine, kind
):
    path, body = admission_request(kind, provider=None)
    key = uuid4().hex
    original = submit(api, owner_headers, path, body, key=key)
    assert original.status_code == 202, original.text
    old_job = original.json()
    assert old_job["provider"] == "yfinance"
    save(api, owner_headers, "openbb", is_default=True)
    save(api, owner_headers, "yfinance", enabled=False)
    replay = submit(api, owner_headers, path, body, key=key)
    assert replay.status_code in {200, 202}, replay.text
    assert replay.json()["id"] == old_job["id"]
    assert replay.json()["request"] == old_job["request"]
    fresh = submit(api, owner_headers, path, body)
    assert fresh.status_code == 202, fresh.text
    assert fresh.json()["provider"] == "openbb"
    assert fresh.json()["request"]["_provider_connection"] == frozen_snapshot(
        connection(api, owner_headers, "openbb")
    )
    assert counts(migrated_engine) == (2, 2)


def test_retry_preserves_frozen_request_and_hash_after_connection_changes(
    api, owner_headers, migrated_engine
):
    saved = save(api, owner_headers, "akshare", default_price_mode="qfq")
    path, body = admission_request("import", provider="akshare", market="CN")
    original = submit(api, owner_headers, path, body)
    assert original.status_code == 202, original.text
    job = original.json()
    with Session(migrated_engine) as session:
        record = session.get(DataImport, UUID(job["id"]))
        original_request, original_hash = (
            deepcopy(record.request),
            record.request_sha256,
        )
    cancelled = api.post(f"{path}/{job['id']}/cancel", headers=owner_headers)
    assert cancelled.status_code == 202, cancelled.text
    save(
        api,
        owner_headers,
        "akshare",
        enabled=False,
        is_default=False,
        default_price_mode="hfq",
    )
    retry = api.post(f"{path}/{job['id']}/retry", headers=owner_headers)
    assert retry.status_code in {200, 202}, retry.text
    assert retry.json()["id"] == job["id"]
    assert retry.json()["generation"] == 2
    assert retry.json()["request"] == original_request
    assert retry.json()["request"]["_provider_connection"] == frozen_snapshot(saved)
    assert retry.json()["request"]["price_mode"] == "qfq"
    with Session(migrated_engine) as session:
        record = session.get(DataImport, UUID(job["id"]))
        assert record.request_sha256 == original_hash
        assert record.request == original_request
        assert session.scalars(
            select(DataOutbox.generation)
            .where(DataOutbox.import_id == record.id)
            .order_by(DataOutbox.generation)
        ).all() == [1, 2]


@pytest.mark.parametrize("worker_contract", ["data-import.v1", "data-import.v2"])
def test_legacy_replay_and_retry_keep_original_request_hash_without_new_binding(
    api, owner_headers, owner, migrated_engine, worker_contract
):
    from backend.app.data_schemas import ImportCreate

    path, body = admission_request("import")
    key, job_id = uuid4().hex, uuid4()
    legacy_input = ImportCreate(**body).model_dump(mode="json")
    legacy_input.pop("expected_connection_revision", None)
    legacy_input["price_mode"] = "provider_native"
    old_hash = hashlib.sha256(
        json.dumps(
            {"kind": "import", "request": legacy_input, "parent_version_id": None},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    old_request = {
        **legacy_input,
        "_execution": {
            "worker_contract": worker_contract,
            "provider_mode": "live",
            "block_days": 31,
            "schema_version": "marketdata.v1",
            "transform_version": "normalize.v1",
            "fixture_version": None,
        },
    }
    # Persist an actual pre-006-shaped job, rather than mocking a legacy lookup.
    with Session(migrated_engine) as session:
        session.add(
            DataImport(
                id=job_id,
                owner_id=owner.user.id,
                kind="import",
                provider="yfinance",
                idempotency_key=key,
                request_sha256=old_hash,
                request=old_request,
                status="cancelled",
                generation=1,
            )
        )
        session.flush()
        session.add(DataOutbox(import_id=job_id, generation=1))
        session.commit()
    save(api, owner_headers, "yfinance", enabled=False, is_default=False)
    replay = submit(api, owner_headers, path, body, key=key)
    assert replay.status_code in {200, 202}, replay.text
    assert replay.json()["id"] == str(job_id)
    assert replay.json()["request"] == old_request
    retry = api.post(f"{path}/{job_id}/retry", headers=owner_headers)
    assert retry.status_code in {200, 202}, retry.text
    assert retry.json()["generation"] == 2
    assert retry.json()["request"] == old_request
    assert "_provider_connection" not in retry.json()["request"]
    with Session(migrated_engine) as session:
        record = session.get(DataImport, job_id)
        assert record.request_sha256 == old_hash
        assert record.request == old_request
    assert counts(migrated_engine) == (1, 2)
