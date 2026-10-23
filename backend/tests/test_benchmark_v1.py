"""Pure parser/transport/projection contracts: no sockets, DB, Docker or engine."""

import copy
import io
import json
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest

from script import benchmark_v1 as bench

EVALUATION = "11111111-1111-1111-1111-111111111111"
RUN = "22222222-2222-2222-2222-222222222222"
DATASET = "33333333-3333-3333-3333-333333333333"
PRIVATE = "PRIVATE_SOURCE_USER_AND_ERROR_MARKER"
ENVIRONMENT = {
    "E2E_EMAIL": "private-owner@example.invalid",
    "E2E_PASSWORD": secrets.token_urlsafe(32),
}
TOKEN = secrets.token_urlsafe(32)


def args(*extra):
    return bench.parse_args(["--samples", "3", "--warmup", "1", *extra])


def table(rows=2):
    return {
        "total_rows": 10,
        "filtered_rows": rows,
        "returned_rows": rows,
        "rows": [[PRIVATE, n] for n in range(rows)],
    }


def query_result(kind="research", sections=("equity",)):
    return {
        "schema_version": "tradegrad.result-query.v1"
        if kind == "research"
        else "tradegrad.final-result-query.v1",
        "status": "succeeded",
        "evaluation_id": EVALUATION,
        "run_id": RUN,
        "window": {"start": "2024-01-01", "end": "2024-03-01"},
        "config": {"dataset": {"id": DATASET}, "strategy_source": PRIVATE},
        "is_synthetic": True,
        "tables": {section: table() for section in sections},
        "warnings": [PRIVATE],
        "identity": ENVIRONMENT["E2E_EMAIL"],
    }


class FakeTransport:
    def __init__(
        self, *, empty=False, kind="research", missing_metadata=False, failure=None
    ):
        self.calls = []
        self.empty, self.kind = empty, kind
        self.missing_metadata, self.failure = missing_metadata, failure

    def request(self, method, path, *, token=None, body=None):
        self.calls.append((method, path, token, body))
        parts = urlsplit(path)
        route, query = parts.path.removeprefix(bench.PREFIX), parse_qs(parts.query)
        if method == "POST":
            assert route == "/login/access-token"
            assert token is None
            credentials_match = parse_qs(body.decode()) == {
                "username": [ENVIRONMENT["E2E_EMAIL"]],
                "password": [ENVIRONMENT["E2E_PASSWORD"]],
            }
            assert credentials_match
            return {"access_token": TOKEN, "token_type": "bearer", "private": PRIVATE}
        token_matches = secrets.compare_digest(token, TOKEN)
        assert token_matches
        if route == "/health/live":
            return {"status": "alive"}
        if route == "/health/ready":
            return {"status": "ready", "database": True}
        if route == "/research-capabilities":
            return {"schema_version": "tradegrad.run.v1", "private": PRIVATE}
        if route == "/datasets/" + DATASET:
            if self.missing_metadata:
                return {"manifest": {}}
            return {
                "start_date": "2023-12-01",
                "end_date": "2024-03-01",
                "name": PRIVATE,
                "manifest": {"row_count": 222, "listings": [{"name": PRIVATE}]},
            }
        if route in ("/research-runs", "/final-evaluations"):
            return {
                "data": []
                if self.empty
                else [
                    {"id": RUN, "status": "succeeded", "config": {"source": PRIVATE}}
                ],
                "count": 0 if self.empty else 1,
            }
        if route.endswith("/evaluations"):
            return {
                "data": [
                    {
                        "id": EVALUATION,
                        "status": "succeeded",
                        "sandbox_stopped": True,
                        "created_at": "2024-01-01T00:00:00Z",
                        "finished_at": "2024-01-01T00:00:02.500000Z",
                        "error": PRIVATE,
                    }
                ],
                "count": 1,
            }
        if route == bench.result_path(self.kind, EVALUATION):
            if self.failure:
                raise bench.BenchmarkError(self.failure)
            return query_result(self.kind, query["sections"])
        raise AssertionError("Unexpected fake route")


def run(transport=None, *extra):
    return bench.run_benchmark(
        args(*extra),
        transport or FakeTransport(),
        ENVIRONMENT,
        host=lambda: {"scope": "test_only"},
    )


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("http://127.0.0.1:8000", ("127.0.0.1", 8000)),
        ("http://127.2.3.4/", ("127.2.3.4", 80)),
        ("http://[::1]:8080", ("::1", 8080)),
    ],
)
def test_literal_loopback_origins(origin, expected):
    assert bench.parse_origin(origin) == expected


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:8000",
        "http://localhost:8000",
        "http://example.invalid",
        "http://192.168.0.1",
        "http://0.0.0.0",
        "http://[::]",
        "http://[::1%eth0]",
        pytest.param(
            f"http://user:{secrets.token_urlsafe(32)}@127.0.0.1", id="userinfo"
        ),
        "http://127.0.0.1/api/v1",
        "http://127.0.0.1?x=1",
        "http://127.0.0.1#fragment",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1\n",
        "file:///tmp/no",
        "//127.0.0.1",
        "http://2130706433",
    ],
)
def test_origin_rejected_without_network(origin):
    with pytest.raises(bench.BenchmarkError, match="^invalid_origin$"):
        bench.parse_origin(origin)


@pytest.mark.parametrize(
    "option,value",
    [
        ("--samples", "0"),
        ("--samples", "101"),
        ("--warmup", "11"),
        ("--timeout-seconds", "0"),
        ("--timeout-seconds", "11"),
        ("--timeout-seconds", "nan"),
        ("--max-points", "3"),
        ("--max-points", "1001"),
        ("--table-limit", "101"),
        ("--body-cap-bytes", "999999999"),
        ("--candidate-probes", "11"),
        ("--discovery-runs", "21"),
        ("--discovery-evaluations", "101"),
        ("--evaluation-id", PRIVATE),
        ("--result-kind", PRIVATE),
        ("--window-days", "0"),
    ],
)
def test_parser_caps_and_errors_are_redacted(option, value):
    with pytest.raises(bench.BenchmarkError, match="^invalid_arguments$"):
        args(option, value)


def test_percentiles_are_linear_interpolation_and_singleton_is_defined():
    assert bench.summarize([10, 20, 30, 40]) == {
        "count": 4,
        "p50": 25,
        "p95": 38.5,
        "max": 40,
    }
    assert bench.summarize([3]) == {"count": 1, "p50": 3, "p95": 3, "max": 3}
    assert bench.summarize([]) == {"count": 0, "p50": None, "p95": None, "max": None}


def test_warmup_excluded_and_errors_not_counted_as_success_latency():
    outcomes = iter(
        [bench.BenchmarkError("timeout"), None, bench.BenchmarkError("http_503"), None]
    )
    ticks = iter([1, 1.010, 2, 3, 3.030])

    def operation():
        error = next(outcomes)
        if error:
            raise error

    result = bench.measure(operation, samples=3, warmup=1, clock=lambda: next(ticks))
    assert result["successes"] == 2
    assert result["error_categories"] == {"http_503": 1}
    assert result["warmup"]["error_categories"] == {"timeout": 1}
    assert result["success_latency_ms"] == {"count": 2, "p50": 20, "p95": 29, "max": 30}


class FakeResponse:
    def __init__(self, body=b'{"ok":true}', *, status=200, headers=None, chunks=None):
        self.status, self.headers = status, headers or {}
        self.stream = io.BytesIO(body)
        self.chunks = iter(chunks) if chunks is not None else None
        self.read_sizes = []

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def close(self):
        self.stream.close()

    def read1(self, size):
        self.read_sizes.append(size)
        return (
            next(self.chunks, b"")
            if self.chunks is not None
            else self.stream.read(size)
        )


class FakeConnection:
    def __init__(self, response=None, failure=None):
        self.response, self.failure = response or FakeResponse(), failure
        self.calls, self.closed = [], False
        self.sock = None

    def connect(self):
        pass

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.failure:
            raise self.failure

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def adapter(connection, *, clock=lambda: 0):
    constructions = []

    def factory(*positional, **kw):
        constructions.append((positional, kw))
        return connection

    transport = bench.HTTPTransport(
        "http://127.0.0.1:8000", 3, 1024, connection_factory=factory, clock=clock
    )
    transport.constructions = constructions
    return transport


def test_real_transport_adapter_uses_bounded_reads_no_proxy_and_closes():
    connection = FakeConnection()
    transport = adapter(connection)
    assert transport.request("GET", "/api/v1/health/live", token=TOKEN) == {"ok": True}
    assert transport.constructions == [(("127.0.0.1", 8000), {"timeout": 3})]
    assert connection.closed
    assert connection.response.read_sizes == [1025, 1014]
    headers = connection.calls[0][1]["headers"]
    assert headers["Accept-Encoding"] == "identity"
    assert headers["Connection"] == "close"
    token_matches = secrets.compare_digest(headers["Authorization"], "Bearer " + TOKEN)
    assert token_matches


@pytest.mark.parametrize(
    "status", [301, 302, 303, 307, 308, 401, 403, 404, 409, 429, 500, 503]
)
def test_http_error_and_redirect_are_never_read_or_followed(status):
    response = FakeResponse(
        body=PRIVATE.encode(),
        status=status,
        headers={"Location": "http://evil.invalid/"},
    )
    connection = FakeConnection(response)
    expected = "redirect_blocked" if status < 400 else f"http_{status}"
    with pytest.raises(bench.BenchmarkError, match=f"^{expected}$"):
        adapter(connection).request(
            "POST",
            "/api/v1/login/access-token",
            body=secrets.token_urlsafe(32).encode(),
        )
    assert len(connection.calls) == 1
    assert response.read_sizes == []
    assert connection.closed


@pytest.mark.parametrize(
    "response,error",
    [
        (FakeResponse(headers={"Content-Length": "1025"}), "response_too_large"),
        (FakeResponse(body=b"x" * 1025), "response_too_large"),
        (FakeResponse(headers={"Content-Encoding": "gzip"}), "unsupported_encoding"),
        (FakeResponse(headers={"Content-Length": "-1"}), "invalid_response"),
        (FakeResponse(headers={"Content-Length": "1"}), "invalid_response"),
        (FakeResponse(body=b"not-json"), "invalid_response"),
        (FakeResponse(body=b"[]"), "invalid_response"),
        (FakeResponse(body=b'{"x":NaN}'), "invalid_response"),
        (FakeResponse(body=b"\xff"), "invalid_response"),
    ],
)
def test_response_bounds_and_decoding(response, error):
    connection = FakeConnection(response)
    with pytest.raises(bench.BenchmarkError, match=f"^{error}$"):
        adapter(connection).request("GET", "/api/v1/health/live")
    assert connection.closed


@pytest.mark.parametrize(
    "failure,error",
    [(TimeoutError(PRIVATE), "timeout"), (OSError(PRIVATE), "transport_error")],
)
def test_transport_timeout_and_errors_discard_free_text(failure, error):
    connection = FakeConnection(failure=failure)
    with pytest.raises(bench.BenchmarkError, match=f"^{error}$"):
        adapter(connection).request("GET", "/api/v1/health/live")
    assert connection.closed


def test_response_body_deadline_stops_drip():
    clock = [0.0]

    class DripResponse(FakeResponse):
        def read1(self, size):
            clock[0] += 4
            return super().read1(size)

    response = DripResponse(chunks=[b"{", b'"ok":true}'])
    connection = FakeConnection(response)
    with pytest.raises(bench.BenchmarkError, match="^timeout$"):
        adapter(connection, clock=lambda: clock[0]).request(
            "GET", "/api/v1/health/live"
        )
    assert response.read_sizes == [1025]
    assert connection.closed


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/research-runs"),
        ("DELETE", "/api/v1/research-runs"),
        ("GET", "http://evil.invalid/api/v1/health/live"),
        ("GET", "//evil.invalid/api/v1/health/live"),
        ("GET", "/api/v1/candidates/" + EVALUATION),
        ("GET", "/api/v1/health/live\nInjected: x"),
    ],
)
def test_transport_rejects_non_allowlisted_operations_before_connection(method, path):
    transport = adapter(FakeConnection())
    with pytest.raises(bench.BenchmarkError, match="^invalid_request$"):
        transport.request(method, path)
    assert not transport.constructions


def test_metadata_only_never_reads_results_without_opt_in():
    transport = FakeTransport()
    report = run(transport)
    assert report["status"] == "complete"
    assert report["result"]["status"] == "not_requested"
    assert len(transport.calls) == 13
    assert {name for name in report["measurements"]} == {
        "health_live",
        "health_ready",
        "metadata_research_capabilities",
    }
    required = run(FakeTransport(), "--require-result")
    assert required["status"] == "incomplete"


@pytest.mark.parametrize("kind", ["research", "final"])
def test_discovery_and_windowed_table_measurements_are_distinct_and_bounded(kind):
    transport = FakeTransport(kind=kind)
    report = run(
        transport,
        "--allow-result-access-records",
        "--require-result",
        "--result-kind",
        kind,
    )
    assert report["status"] == "complete"
    assert report["result"]["status"] == "selected"
    assert report["result"]["window_calendar_days"] == 30
    assert report["workload"]["observed"] == {
        "dataset_bar_count": 222,
        "dataset_listing_count": 1,
        "dataset_calendar_days": 91,
        "is_synthetic": True,
    }
    assert (
        report["historical_timings"]["evaluation"]["registration_to_completion_seconds"]
        == 2.5
    )
    assert report["historical_timings"]["evaluation"]["independently_timed"] is False
    assert report["historical_timings"]["import"]["status"] == "unsupported"
    assert report["bounds"]["http_concurrency"] == 1
    assert report["performance_threshold"] is None
    assert len(report["measurements"]) == 5
    result_calls = [
        parse_qs(urlsplit(call[1]).query)
        for call in transport.calls
        if urlsplit(call[1]).path == bench.PREFIX + bench.result_path(kind, EVALUATION)
    ]
    assert (
        len(result_calls) == 11
    )  # probe, 2 untimed observations, 2 * (warmup + samples)
    for call in result_calls[1:]:
        assert call["start"] == ["2024-01-31"]
        assert call["end"] == ["2024-03-01"]
        assert call["limit"] == ["50"] and call["max_points"] == ["200"]
    encoded = json.dumps(report)
    for forbidden in (
        PRIVATE,
        TOKEN,
        EVALUATION,
        RUN,
        DATASET,
        *ENVIRONMENT.values(),
        "http://",
        "https://",
    ):
        contains_forbidden = forbidden in encoded
        assert not contains_forbidden


def test_explicit_result_does_not_discover_or_invent_historical_timing():
    transport = FakeTransport()
    report = run(
        transport, "--allow-result-access-records", "--evaluation-id", EVALUATION
    )
    assert report["result"]["selection"] == "explicit"
    assert report["historical_timings"]["evaluation"]["status"] == "unavailable"
    assert not any(
        call[1].split("?")[0] in ("/api/v1/research-runs", "/api/v1/final-evaluations")
        for call in transport.calls
    )


def test_empty_results_fail_required_gate_without_fabricated_measurements():
    report = run(
        FakeTransport(empty=True), "--allow-result-access-records", "--require-result"
    )
    assert report["status"] == "incomplete"
    assert report["result"]["error_category"] == "no_result"
    assert "persisted_chart_window" not in report["measurements"]
    assert report["result"]["discovery"]["runs_inspected"] == 0


def test_result_probe_http_errors_and_candidate_cap_are_visible():
    transport = FakeTransport(failure="http_409")
    report = run(
        transport,
        "--allow-result-access-records",
        "--candidate-probes",
        "1",
        "--require-result",
    )
    assert report["status"] == "incomplete"
    assert report["result"]["discovery"]["error_categories"] == {"http_409": 1}
    assert report["result"]["discovery"]["candidate_probes"] == 1


def test_unknown_dimensions_are_null_not_guessed_or_replaced_with_declarations():
    report = run(
        FakeTransport(missing_metadata=True),
        "--allow-result-access-records",
        "--declared-dataset-bars",
        "400",
        "--declared-dataset-listings",
        "2",
    )
    workload = report["workload"]
    assert workload["observed"]["dataset_bar_count"] is None
    assert workload["observed"]["dataset_listing_count"] is None
    assert workload["observed"]["dataset_calendar_days"] is None
    assert workload["declared_not_verified"] == {
        "dataset_bars": 400,
        "dataset_listings": 2,
    }
    assert workload["representative_workload_acceptance"] == "operator_pending"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda result: result.update(status="failed"),
        lambda result: result.update(evaluation_id=RUN),
        lambda result: result.update(schema_version="unexpected"),
        lambda result: result["tables"]["equity"].update(returned_rows=True),
        lambda result: result["tables"]["equity"].update(returned_rows=3),
        lambda result: result["tables"]["equity"].update(total_rows=0),
        lambda result: result["tables"]["equity"].update(
            rows=[[1]] * 201, returned_rows=201, filtered_rows=201, total_rows=201
        ),
    ],
)
def test_invalid_result_shapes_are_not_latency_successes(mutation):
    result = query_result()
    mutation(result)
    with pytest.raises(bench.BenchmarkError):
        bench.validate_result(result, "research", EVALUATION, ["equity"], 50, 200)


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"created_at": "invalid", "finished_at": PRIVATE},
        {"created_at": "2024-01-01", "finished_at": "2024-01-02"},
        {"created_at": "2024-01-02T00:00:00Z", "finished_at": "2024-01-01T00:00:00Z"},
    ],
)
def test_historical_interval_requires_two_ordered_aware_timestamps(record):
    assert bench.historical_evaluation_timing(record)["status"] == "unavailable"


def test_cli_errors_and_raw_server_data_are_not_serialized(capsys):
    output = io.StringIO()
    code = bench.main(["--unknown", PRIVATE], environment=ENVIRONMENT, output=output)
    assert code == 2
    assert json.loads(output.getvalue())["error_category"] == "invalid_arguments"
    assert PRIVATE not in output.getvalue()
    assert not capsys.readouterr().err
    output = io.StringIO()
    code = bench.main(
        [
            "--samples",
            "1",
            "--warmup",
            "0",
            "--allow-result-access-records",
            "--require-result",
        ],
        environment=ENVIRONMENT,
        transport_factory=lambda *args: FakeTransport(),
        output=output,
    )
    assert code == 0
    for forbidden in (
        PRIVATE,
        TOKEN,
        EVALUATION,
        RUN,
        DATASET,
        *ENVIRONMENT.values(),
        "http://",
    ):
        contains_forbidden = forbidden in output.getvalue()
        assert not contains_forbidden


def test_cli_missing_auth_does_not_make_request_or_echo_environment():
    transport = FakeTransport()
    output = io.StringIO()
    assert (
        bench.main(
            [],
            environment={"E2E_EMAIL": PRIVATE},
            transport_factory=lambda *args: transport,
            output=output,
        )
        == 2
    )
    assert json.loads(output.getvalue())["error_category"] == "missing_credentials"
    assert not transport.calls
    assert PRIVATE not in output.getvalue()


def test_cli_unexpected_exception_redacts_message_and_traceback():
    output = io.StringIO()

    def broken(*args):
        raise RuntimeError(PRIVATE)

    assert (
        bench.main([], environment=ENVIRONMENT, transport_factory=broken, output=output)
        == 2
    )
    assert json.loads(output.getvalue())["error_category"] == "internal_error"
    assert PRIVATE not in output.getvalue()


def test_actual_research_projection_contract_without_network_or_engine():
    from backend.app.research_views import result_view
    from backend.tests.research_view_fixtures import synthetic_result

    result = synthetic_result()
    projection = result_view(
        result, sections=["equity", "drawdown", "fills"], max_points=4, limit=2
    ).model_dump(mode="json")
    assert (
        bench.validate_result(
            projection,
            "research",
            result.evaluation.id,
            ["equity", "drawdown", "fills"],
            2,
            4,
        )
        is projection
    )
    projection_copy = copy.deepcopy(projection)
    projection_copy["tables"]["equity"]["rows"] = []
    with pytest.raises(bench.BenchmarkError):
        bench.validate_result(
            projection_copy,
            "research",
            result.evaluation.id,
            ["equity", "drawdown", "fills"],
            2,
            4,
        )


def test_actual_final_projection_contract_without_network_or_engine():
    from backend.app.final_evaluation_views import final_result_view
    from backend.tests.final_result_fixtures import synthetic_final_result

    result = synthetic_final_result()
    projection = final_result_view(
        result,
        exposure_snapshot={},
        sections=["equity", "fills"],
        max_points=4,
        limit=2,
    ).model_dump(mode="json")
    assert (
        bench.validate_result(
            projection, "final", result.evaluation.id, ["equity", "fills"], 2, 4
        )
        is projection
    )


def test_host_information_missing_resources_and_unknown_platform_are_not_invented(
    monkeypatch,
):
    def unavailable(*args):
        raise OSError(PRIVATE)

    monkeypatch.setattr(bench.os, "sysconf", unavailable)
    monkeypatch.setattr(bench.os, "sched_getaffinity", unavailable)
    monkeypatch.setattr(bench.os, "cpu_count", lambda: None)
    monkeypatch.setattr(bench.platform, "system", lambda: PRIVATE)
    monkeypatch.setattr(bench.platform, "machine", lambda: PRIVATE)
    monkeypatch.setattr(bench.Path, "read_text", unavailable)
    result = bench.host_information()
    assert result["platform"] == "other"
    assert result["architecture"] == "other"
    for key in (
        "logical_cpu_count",
        "affinity_cpu_count",
        "physical_memory_bytes",
        "cgroup_v2_memory_limit_bytes",
        "cgroup_v2_cpu_quota_cores",
    ):
        assert result[key] is None
    assert PRIVATE not in json.dumps(result)


def test_cli_auth_failure_and_result_incompleteness_have_distinct_exit_codes():
    class AuthFailure(FakeTransport):
        def request(self, *args, **kwargs):
            raise bench.BenchmarkError("http_401")

    output = io.StringIO()
    assert (
        bench.main(
            [],
            environment=ENVIRONMENT,
            transport_factory=lambda *args: AuthFailure(),
            output=output,
        )
        == 2
    )
    assert json.loads(output.getvalue())["error_category"] == "http_401"
    output = io.StringIO()
    assert (
        bench.main(
            [
                "--samples",
                "1",
                "--warmup",
                "0",
                "--allow-result-access-records",
                "--require-result",
            ],
            environment=ENVIRONMENT,
            transport_factory=lambda *args: FakeTransport(empty=True),
            output=output,
        )
        == 1
    )
    assert json.loads(output.getvalue())["result"]["error_category"] == "no_result"


def test_empty_equity_probe_is_not_accepted_as_representative_success():
    class EmptyEquity(FakeTransport):
        def request(self, *args, **kwargs):
            response = super().request(*args, **kwargs)
            if "tables" in response:
                response["tables"]["equity"] = table(rows=0)
            return response

    report = run(EmptyEquity(), "--allow-result-access-records", "--require-result")
    assert report["status"] == "incomplete"
    assert report["result"]["discovery"]["error_categories"] == {"invalid_result": 1}
    assert "persisted_chart_window" not in report["measurements"]


def test_failure_during_measured_samples_is_not_a_success_or_complete_gate():
    class FlakyHealth(FakeTransport):
        def request(self, method, path, **kwargs):
            if path == "/api/v1/health/ready":
                raise bench.BenchmarkError("timeout")
            return super().request(method, path, **kwargs)

    report = run(FlakyHealth(), "--allow-result-access-records", "--require-result")
    measurement = report["measurements"]["health_ready"]
    assert report["status"] == "incomplete"
    assert report["result"]["status"] == "selected"
    assert measurement["errors"] == 3 and measurement["successes"] == 0
    assert measurement["warmup"]["errors"] == 1
    assert measurement["success_latency_ms"]["p95"] is None


@pytest.mark.parametrize("token", ["", "\r\nInjected: x", "x" * 16385])
def test_bad_token_cannot_be_forwarded_or_printed(token):
    transport = adapter(FakeConnection())
    with pytest.raises(bench.BenchmarkError, match="^invalid_auth_response$"):
        transport.request("GET", "/api/v1/health/live", token=token)
    assert not transport.constructions
