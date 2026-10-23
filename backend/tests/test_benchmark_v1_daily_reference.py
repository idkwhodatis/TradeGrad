"""Pure/fake HTTP reference contracts. No sockets, DB, Docker, models or engines."""

import ast
import copy
import csv
import hashlib
import io
import json
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from script import benchmark_v1_daily_reference as bench
from script import benchmark_v1_workloads as smoke
from script.generate_daily_reference_fixture import generate

PRIVATE = "PRIVATE_ACCOUNT_SOURCE_TRACE_RESPONSE_EXCEPTION"


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


@pytest.fixture
def env():
    return {
        "GITHUB_ACTIONS": "true",
        "DATA_PROVIDER_MODE": "fixture",
        "TRADEGRAD_REQUIRE_DOCKER": "1",
        "E2E_EMAIL": "fixture@example.invalid",
        "E2E_PASSWORD": secrets.token_urlsafe(32),
    }


def arguments(*extra):
    return bench.parse_args(["--allow-representative-workload-mutations", *extra])


class FakeTransport:
    def __init__(self, env, clock):
        self.env, self.clock = env, clock
        self.token = secrets.token_urlsafe(32)
        self.seed, self.dataset, self.imported, self.universe, self.listing = [
            str(uuid4()) for _ in range(5)
        ]
        self.runs, self.calls, self.bodies, self.polls = [], [], [], {}
        self.attempts, self.evaluations = {}, {}
        self.config = None
        self.live = False
        self.overlap = True
        self.engine_overlap = True
        self.hang = False
        self.lost_post = False
        self.model_calls = False
        self.mutate = lambda route, value: value
        self.sessions = [
            row["date"]
            for row in csv.DictReader(io.StringIO(bench.load_fixture().decode()))
        ]

    def dataset_value(self):
        return {
            "id": self.dataset,
            "is_synthetic": True,
            "start_date": bench.START,
            "end_date": bench.END,
            "content_sha256": "a" * 64,
            "manifest": {
                "row_count": bench.ROWS,
                "listings": [{"listing_id": self.listing}],
                "upload_source": {
                    "sha256": bench.FIXTURE_SHA,
                    "size_bytes": bench.CSV_BYTES,
                },
                "artifact_files": {"source.csv": bench.FIXTURE_SHA},
                "evaluation_compatibility": {"compatible": True},
            },
            "private": PRIVATE,
        }

    def interval(self, run):
        index = self.runs.index(run)
        offset = index * 100
        if index == 2 and self.overlap:
            offset -= 90
        start = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(seconds=offset)
        return start, start + timedelta(seconds=80)

    def detail(self, run):
        start, end = self.interval(run)
        return {
            "id": run,
            "status": "succeeded",
            "execution_mode": "optimization",
            "generation": 1,
            "dataset_version_id": self.dataset,
            "strategy_version_id": self.seed,
            "config": self.config,
            "research_image_digest": "sha256:" + "b" * 64,
            "result": {
                "execution_mode": "optimization",
                "is_synthetic": True,
                "evaluation_mode": "native_akquant",
                "result_trust": "native_strategy_and_engine_share_process",
                "holdout_executed": False,
                "image_digest": "sha256:" + "b" * 64,
                "private": PRIVATE,
            },
            "attempts": [
                {
                    "id": self.attempts[run],
                    "status": "succeeded",
                    "sandbox_stopped": True,
                    "exit_code": 0,
                    "generation": 1,
                    "started_at": start.isoformat(),
                    "finished_at": end.isoformat(),
                }
            ],
            "private": PRIVATE,
        }

    def evaluation_rows(self, run):
        start, _ = self.interval(run)
        short = not self.engine_overlap
        if short and self.runs.index(run) == 2:
            start += timedelta(seconds=35)
        rows = []
        for index, window in enumerate(bench.EXPECTED_WINDOWS):
            opened = start + timedelta(seconds=1 + index * (3 if short else 12))
            rows.append(
                {
                    "id": self.evaluations[run][index],
                    "run_id": run,
                    "attempt_id": self.attempts[run],
                    "generation": 1,
                    "status": "succeeded",
                    "code_sha256": bench.SEED_SHA,
                    "split": window["split"],
                    "window": window,
                    "window_sha256": bench.digest(bench.json_bytes(window)),
                    "sandbox_stopped": True,
                    "image_digest": "sha256:" + "b" * 64,
                    "payload_sha256": "c" * 64,
                    "created_at": opened.isoformat(),
                    "finished_at": (
                        opened + timedelta(seconds=2 if short else 10)
                    ).isoformat(),
                    "private": PRIVATE,
                }
            )
        return rows

    def request(
        self,
        method,
        path,
        *,
        timeout,
        token=None,
        body=None,
        content_type=None,
        key=None,
    ):
        assert 0 < timeout <= 10
        self.clock.sleep(0.01)
        self.calls.append((method, path, key))
        value = self.response(method, path, token, body, content_type)
        return self.mutate(urlsplit(path).path.removeprefix(bench.PREFIX), value)

    def response(self, method, path, token, body, content_type):
        route = urlsplit(path).path.removeprefix(bench.PREFIX)
        if route == "/login/access-token":
            assert token is None
            assert parse_qs(body.decode()) == {
                "username": [self.env["E2E_EMAIL"]],
                "password": [self.env["E2E_PASSWORD"]],
            }
            return {"access_token": self.token, "token_type": "bearer"}
        assert secrets.compare_digest(token, self.token)
        if route == "/data/providers":
            return [{"runtime_mode": "live" if self.live else "fixture"}]
        if route == "/research-capabilities":
            return {
                "live_model_calls": False,
                "model_providers": ["fixture"],
                "models": ["deterministic-fixture-v1"],
                "environment_digest": bench.environment_digest(bench.ROOT),
            }
        if route == "/strategy-versions":
            return {
                "data": [
                    {
                        "id": self.seed,
                        "task": "cn_time",
                        "name": "Original cn_time seed",
                        "mode": "native_akquant",
                        "parent_id": None,
                        "content_sha256": bench.SEED_SHA,
                    }
                ]
            }
        if route == "/data/uploads":
            assert bench.load_fixture() in body
            assert content_type.startswith("multipart/form-data; boundary=")
            return {"id": self.imported}
        if route == "/data/imports/" + self.imported:
            return {
                "id": self.imported,
                "status": "ready",
                "dataset_version_id": self.dataset,
            }
        if route == "/datasets/" + self.dataset:
            return self.dataset_value()
        if route == f"/datasets/{self.dataset}/coverage":
            return {
                "dataset_version_id": self.dataset,
                "universe": {"members": [{"listing_id": self.listing}]},
                "coverage": [
                    {
                        "listing_id": self.listing,
                        "status": "complete",
                        "calendar_version": "4.13.2",
                        "expected_sessions": self.sessions,
                        "observed_sessions": self.sessions,
                        "missing_sessions": [],
                        "non_session_dates": [],
                        "provisional_sessions": [],
                    }
                ],
            }
        if route == "/data/universes":
            posted = json.loads(body)
            assert posted["state"] == "user_fixed" and posted["members"] == [
                {"listing_id": self.listing}
            ]
            return posted | {"id": self.universe, "content_sha256": "e" * 64}
        if route == "/research-runs/preflight":
            self.config = json.loads(body)
            config = bench.RunConfig.model_validate(self.config)
            assert (
                config.optimizer.max_iterations
                == config.budgets.max_model_calls
                == config.budgets.max_tokens
                == 0
            )
            return {
                "valid": True,
                "is_synthetic": True,
                "holdout_execution": False,
                "config_sha256": config.sha256,
                "evaluation_windows": list(bench.EXPECTED_WINDOWS),
            }
        if route == "/research-runs":
            assert method == "POST" and json.loads(body) == self.config
            self.bodies.append(body)
            if self.lost_post:
                raise bench.BenchmarkError("transport_error")
            run = str(uuid4())
            self.runs.append(run)
            self.attempts[run] = str(uuid4())
            self.evaluations[run] = [str(uuid4()) for _ in range(5)]
            return {"id": run, "status": "queued"}
        if route.endswith("/cancel"):
            owned = route.split("/")[-2]
            assert owned == self.imported or owned in self.runs
            return {"id": owned, "status": "cancel_requested"}
        if route.startswith("/research-runs/"):
            run = route.split("/")[2]
            assert run in self.runs
            if route.endswith("/evaluations"):
                rows = self.evaluation_rows(run)
                return {"data": rows, "count": len(rows)}
            if route.endswith("/model-calls"):
                return {
                    "data": [{"private": PRIVATE}] if self.model_calls else [],
                    "count": int(self.model_calls),
                }
            self.polls[run] = self.polls.get(run, 0) + 1
            if self.hang or self.polls[run] == 1:
                return {"id": run, "status": "running", "private": PRIVATE}
            return self.detail(run)
        if route.startswith("/research-evaluations/"):
            eid = route.split("/")[2]
            run = self.runs[0]
            assert eid == self.evaluations[run][-1]
            return {
                "schema_version": "tradegrad.result-query.v1",
                "evaluation_id": eid,
                "run_id": run,
                "code_sha256": bench.SEED_SHA,
                "config_sha256": bench.RunConfig.model_validate(self.config).sha256,
                "window": bench.EXPECTED_WINDOWS[-1],
                "split": "validation",
                "status": "succeeded",
                "is_synthetic": True,
                "result_trust": "native_strategy_and_engine_share_process",
                "start": bench.CHART_START,
                "end": bench.CHART_END,
                "config": self.config,
                "artifact_sha256": "f" * 64,
                "tables": {
                    section: {
                        "total_rows": 240,
                        "filtered_rows": 60,
                        "returned_rows": 2,
                        "rows": [[PRIVATE, 1], [PRIVATE, 2]],
                        "sampling": "min_max",
                    }
                    for section in ("equity", "drawdown")
                },
                "private": PRIVATE,
            }
        raise AssertionError("Unexpected fake route")


def run(env, fake=None, *extra):
    clock = fake.clock if fake else Clock()
    fake = fake or FakeTransport(env, clock)
    return bench.run_benchmark(arguments(*extra), fake, env, clock, clock.sleep), fake


def test_five_dimensions_exact_new_pipeline_and_redaction(env):
    report, fake = run(env)
    assert report["status"] == "succeeded", report.get("error_code")
    assert report["created"] == {"imports": 1, "universes": 1, "runs": 3}
    assert report["metadata_api"]["count"] == report["windowed_chart"]["count"] == 20
    assert report["metadata_api"]["warmup_excluded"] == 2
    assert report["single_evaluation"]["samples"] == 1
    assert report["single_evaluation"]["persisted_lifecycle_ms"] == 10000
    assert report["standalone_configured_run"]["physical_engine_evaluations"] == 5
    assert report["concurrent_runs"]["physical_engine_evaluations"] == 10
    assert report["concurrent_runs"]["attempt_overlap_ms"] > 0
    assert report["concurrent_runs"]["physical_evaluation_overlap_ms"] > 0
    assert report["model_calls"] == 0 and report["performance_threshold"] is None
    assert (
        report["capacity_claim"] is False
        and report["user_agreed_production_profile"] is False
    )
    assert report["cleanup"]["cleanup_uncertain"] is False
    assert len(set(fake.bodies)) == 1
    keys = [key for _, _, key in fake.calls if key]
    assert len(keys) == len(set(keys)) == 5
    encoded = json.dumps(report)
    for secret in (
        PRIVATE,
        env["E2E_EMAIL"],
        env["E2E_PASSWORD"],
        fake.token,
        fake.seed,
        fake.dataset,
        fake.imported,
        fake.universe,
        fake.listing,
        *fake.runs,
    ):
        assert secret not in encoded
    assert all(
        bench.GET_ROUTES.fullmatch(urlsplit(path).path)
        for method, path, _ in fake.calls
        if method == "GET"
    )
    assert all(
        bench.POST_ROUTES.fullmatch(urlsplit(path).path)
        for method, path, _ in fake.calls
        if method == "POST"
    )


def test_frozen_sessions_ohlcv_generator_and_old_smoke_unchanged():
    content = bench.load_fixture()
    rows = list(csv.DictReader(io.StringIO(content.decode())))
    assert generate([row["date"] for row in rows]) == content
    assert len({row["date"] for row in rows}) == 1456
    assert {row["date"][:4] for row in rows} == {
        str(year) for year in range(2019, 2025)
    }
    assert all(
        float(row["low"])
        <= float(row["open"])
        <= float(row["close"])
        <= float(row["high"])
        for row in rows
    )
    profile = json.loads((bench.ROOT / bench.PROFILE_PATH).read_text())
    assert (
        hashlib.sha256(
            ("\n".join(row["date"] for row in rows) + "\n").encode()
        ).hexdigest()
        == profile["session_dates_sha256"]
    )
    assert len(list(csv.DictReader(io.StringIO(smoke.load_fixture().decode())))) == 76


@pytest.mark.parametrize(
    "key,value,error",
    [
        ("GITHUB_ACTIONS", "false", "ci_required"),
        ("DATA_PROVIDER_MODE", "live", "fixture_mode_required"),
        ("TRADEGRAD_REQUIRE_DOCKER", "0", "docker_required"),
        ("E2E_PASSWORD", "", "missing_credentials"),
    ],
)
def test_hosted_guard_before_http(env, key, value, error):
    env[key] = value
    fake = FakeTransport(env, Clock())
    with pytest.raises(bench.BenchmarkError, match=error):
        run(env, fake)
    assert fake.calls == []


def test_explicit_opt_in_and_no_live_model_provider(env):
    with pytest.raises(bench.BenchmarkError, match="opt_in_required"):
        bench.run_benchmark(bench.parse_args([]), env=env)
    fake = FakeTransport(env, Clock())
    fake.live = True
    report, _ = run(env, fake)
    assert report["error_code"] == "fixture_mode_required"
    assert not any(
        method == "POST" and "/login/" not in path for method, path, _ in fake.calls
    )


@pytest.mark.parametrize(
    "case", ["serial", "attempt_only", "model_call", "lost_post", "timeout"]
)
def test_fail_closed_and_bounded_cleanup(env, case):
    fake = FakeTransport(env, Clock())
    if case == "serial":
        fake.overlap = False
    if case == "attempt_only":
        fake.engine_overlap = False
    if case == "model_call":
        fake.model_calls = True
    if case == "lost_post":
        fake.lost_post = True
    if case == "timeout":
        fake.hang = True
    total = 60 if case == "timeout" else 600
    report, _ = run(env, fake, "--total-timeout-seconds", str(total))
    expected = {
        "serial": "concurrency_not_observed",
        "attempt_only": "concurrency_not_observed",
        "model_call": "invalid_execution_evidence",
        "lost_post": "transport_error",
        "timeout": "timeout",
    }[case]
    assert report["status"] == "failed" and report["error_code"] == expected
    assert report["http_requests"] <= bench.MAX_REQUESTS
    assert report["total_wall_ms"] <= total * 1000
    assert PRIVATE not in json.dumps(report)
    if case == "attempt_only":
        assert report["concurrent_runs"]["attempt_overlap_ms"] > 0
        assert report["concurrent_runs"]["physical_evaluation_overlap_ms"] == 0
    if case == "lost_post":
        assert report["cleanup"]["unknown_mutation_outcome"] is True
        assert len(fake.bodies) == 1
    if case == "timeout":
        assert report["cleanup"]["cleanup_uncertain"] is True
        assert all(
            path.split("/")[-2] in fake.runs
            for method, path, _ in fake.calls
            if path.endswith("/cancel")
        )


@pytest.mark.parametrize(
    "kind",
    [
        "dataset",
        "upload",
        "coverage",
        "preflight",
        "config",
        "image",
        "children",
        "chart",
        "artifact",
    ],
)
def test_binding_and_physical_evidence_mismatches_fail(env, kind):
    fake = FakeTransport(env, Clock())
    calls = 0

    def mutate(route, value):
        nonlocal calls
        value = copy.deepcopy(value)
        if kind == "dataset" and route == f"/datasets/{fake.dataset}":
            value["manifest"]["row_count"] = 76
        if kind == "upload" and route == f"/datasets/{fake.dataset}":
            value["manifest"]["upload_source"]["sha256"] = "0" * 64
        if kind == "coverage" and route.endswith("/coverage"):
            value["coverage"][0]["calendar_version"] = "unverified"
        if kind == "preflight" and route == "/research-runs/preflight":
            value["config_sha256"] = "0" * 64
        if (
            kind == "config"
            and route.startswith("/research-runs/")
            and value.get("status") == "succeeded"
        ):
            value["config"]["optimizer"]["max_iterations"] = 1
        if kind == "image" and route.endswith("/evaluations"):
            value["data"][0]["image_digest"] = "unknown"
        if kind == "children" and route.endswith("/evaluations"):
            value["data"][0]["sandbox_stopped"] = False
        if route.startswith("/research-evaluations/"):
            calls += 1
            if kind == "chart":
                value["evaluation_id"] = str(uuid4())
            if kind == "artifact" and calls > 1:
                value["artifact_sha256"] = "0" * 64
        return value

    fake.mutate = mutate
    report, _ = run(env, fake)
    assert report["status"] == "failed"
    assert report["error_code"] in {"fixture_integrity", "invalid_execution_evidence"}
    assert report["created"]["runs"] <= 1


@pytest.mark.parametrize(
    "path",
    [
        "/runs",
        "/data/imports",
        "/strategy-versions",
        "/final-evaluations",
        "/portable-research-runs",
        "/research-runs/x/resume",
    ],
)
def test_no_extra_mutating_routes(path):
    assert not bench.POST_ROUTES.fullmatch(bench.PREFIX + path)


def test_source_does_not_launch_services_or_execute_candidates():
    source = (bench.ROOT / "script/benchmark_v1_daily_reference.py").read_text()
    tree = ast.parse(source)
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    imports |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not imports.intersection(
        {"subprocess", "sqlalchemy", "docker", "akquant", "openai", "psycopg"}
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"exec", "eval", "compile", "__import__"}
        for node in ast.walk(tree)
    )


def test_code_identity_is_content_hash_not_untrusted_label(env):
    env["GITHUB_SHA"] = PRIVATE
    identity = bench.code_identity(env)
    assert identity["ci_commit"] is None
    assert identity["source_file_count"] > 100
    assert len(identity["source_tree_sha256"]) == 64
    assert PRIVATE not in json.dumps(identity)
    env["GITHUB_SHA"] = "a" * 40
    assert (
        bench.code_identity(env)["source_tree_sha256"] == identity["source_tree_sha256"]
    )


def test_sanitized_main_error(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ValueError(PRIVATE)

    monkeypatch.setattr(bench, "run_benchmark", fail)
    assert bench.main([]) == 1
    output = capsys.readouterr().out
    assert (
        PRIVATE not in output and json.loads(output)["error_code"] == "internal_error"
    )


@pytest.mark.parametrize(
    "phase", ["connect", "send", "headers", "body", "chunk_framing", "body_clock"]
)
def test_transport_absolute_deadlines_with_inert_http_parser(monkeypatch, phase):
    from backend.tests.test_benchmark_v1_workloads import parser_transport

    call, connection, sock, timers, _ = parser_transport(
        monkeypatch, bench, phase=phase
    )
    with pytest.raises(bench.BenchmarkError, match="^timeout$"):
        call()
    assert timers[0].cancelled and timers[0].joined
    assert connection.sock is None
    if phase == "connect":
        assert sock.sends == 0


@pytest.mark.parametrize(
    "body,status,headers,error",
    [
        (b"{}", 302, {}, "redirect_blocked"),
        (b"x" * 16385, 200, {}, "response_too_large"),
        (b"{}", 200, {"Content-Length": "16385"}, "response_too_large"),
        (b"{}", 200, {"Content-Length": "100"}, "invalid_response"),
        (b"{}", 200, {"Content-Encoding": "gzip"}, "unsupported_encoding"),
        (b'{"value":NaN}', 200, {}, "invalid_response"),
        (b'"private"', 200, {}, "invalid_response"),
    ],
)
def test_transport_response_cap_and_redaction_without_sockets(
    body, status, headers, error
):
    from backend.tests.test_benchmark_v1_workloads import Connection, Response

    connection = Connection(Response(body, status, headers))
    transport = bench.HTTPTransport(
        "http://127.0.0.1", 16384, lambda *args, **kwargs: connection
    )
    with pytest.raises(bench.BenchmarkError, match="^" + error + "$"):
        transport.request("GET", "/api/v1/data/providers", timeout=1)
    assert connection.closed


@pytest.mark.parametrize(
    "method,path",
    [
        ("DELETE", "/research-runs"),
        ("POST", "/runs"),
        ("POST", "/data/imports"),
        ("POST", "/strategy-versions"),
        ("GET", "//example.invalid"),
        ("POST", "/research-runs?outside=scope"),
        ("GET", "/research-runs"),
    ],
)
def test_transport_routes_before_connecting(method, path):
    from backend.tests.test_benchmark_v1_workloads import Connection, Response

    connection = Connection(Response())
    transport = bench.HTTPTransport(
        "http://127.0.0.1", 16384, lambda *args, **kwargs: connection
    )
    with pytest.raises(bench.BenchmarkError, match="^invalid_request$"):
        transport.request(method, bench.PREFIX + path, timeout=1)
    assert connection.calls == []


def test_request_cap_and_cleanup_reserve(env, monkeypatch):
    fake = FakeTransport(env, Clock())
    fake.hang = True
    monkeypatch.setattr(bench, "MAX_REQUESTS", 50)
    report, _ = run(env, fake)
    assert report["error_code"] == "request_budget"
    assert report["http_requests"] <= 50
    assert report["cleanup"]["cancellations_accepted"] == 1


@pytest.mark.parametrize(
    "option,value",
    [
        ("--total-timeout-seconds", "601"),
        ("--timeout-seconds", "11"),
        ("--body-cap-bytes", "131071"),
        ("--samples", "100"),
        ("--profile", "larger"),
        ("--poll-milliseconds", "0"),
    ],
)
def test_profile_and_safety_bounds_cannot_expand(option, value):
    with pytest.raises(bench.BenchmarkError, match="^invalid_arguments$"):
        arguments(option, value)


def test_missing_or_modified_fixture_stops_before_http(env, monkeypatch):
    fake = FakeTransport(env, Clock())
    monkeypatch.setattr(bench, "FIXTURE_SHA", "0" * 64)
    report, _ = run(env, fake)
    assert report["error_code"] == "fixture_integrity"
    assert fake.calls == []


def test_success_closes_inert_response(monkeypatch):
    from backend.tests.test_benchmark_v1_workloads import parser_transport

    call, connection, sock, timers, _ = parser_transport(monkeypatch, bench)
    assert call() == {}
    assert sock.stream.closed and connection.sock is None
    assert timers[0].cancelled and timers[0].joined


def test_upload_and_universe_bodies_use_production_public_contracts():
    from backend.app.data_schemas import UniverseCreate, UploadMapping

    body, _ = bench.upload_body(bench.load_fixture())
    mapping = json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n--", 1)[0])
    accepted = UploadMapping.model_validate(mapping)
    assert accepted.start_date.isoformat() == bench.START
    assert accepted.end_date.isoformat() == bench.END
    assert accepted.price_semantics.native_mode == "user_declared_synthetic"
    value = UniverseCreate.model_validate(
        {
            "name": "Six-year synthetic daily reference v1",
            "state": "user_fixed",
            "as_of": datetime.now(UTC).isoformat(),
            "members": [{"listing_id": str(uuid4())}],
            "evidence": [],
        }
    )
    assert value.state == "user_fixed"


def test_cleanup_never_confirms_live_child_sandbox(env):
    fake = FakeTransport(env, Clock())

    def live_child(route, value):
        value = copy.deepcopy(value)
        if route.endswith("/evaluations"):
            value["data"][0]["sandbox_stopped"] = False
        return value

    fake.mutate = live_child
    report, _ = run(env, fake)
    assert report["error_code"] == "invalid_execution_evidence"
    assert report["cleanup"]["cancellations_accepted"] == 1
    assert report["cleanup"]["terminal_confirmed"] == 0
    assert report["cleanup"]["cleanup_uncertain"] is True


def test_frozen_old_baseline_sources_remain_unchanged():
    expected = {
        "script/benchmark_v1.py": "f55449c1cfdbf2e9dd1edb75fc3453a4dd7db1e26325a670fbb2e72cb188efad",
        "script/benchmark_v1_workloads.py": "d2853b3f790791c6425e68970b854f23018f788621fecaa7cbd3621667f001ef",
    }
    for relative, checksum in expected.items():
        assert bench.digest((bench.ROOT / relative).read_bytes()) == checksum


def test_server_build_input_mismatch_rejects_before_mutation(env):
    fake = FakeTransport(env, Clock())

    def mismatch(route, value):
        if route == "/research-capabilities":
            return value | {"environment_digest": "0" * 64}
        return value

    fake.mutate = mismatch
    report, _ = run(env, fake)
    assert report["error_code"] == "invalid_execution_evidence"
    assert report["created"] == {"imports": 0, "universes": 0, "runs": 0}


def test_request_body_cap_rejects_before_connecting():
    from backend.tests.test_benchmark_v1_workloads import Connection, Response

    connection = Connection(Response())
    transport = bench.HTTPTransport(
        "http://127.0.0.1", 16384, lambda *args, **kwargs: connection
    )
    with pytest.raises(bench.BenchmarkError, match="^request_too_large$"):
        transport.request(
            "POST", bench.PREFIX + "/data/uploads", timeout=1, body=b"x" * 16385
        )
    assert connection.calls == []


@pytest.mark.parametrize("value", ["2025-01-01T00:00:00", "not-a-date", None])
def test_physical_timestamp_requires_timezone(value):
    with pytest.raises(bench.BenchmarkError, match="^invalid_execution_evidence$"):
        bench.interval(value, "2025-01-02T00:00:00+00:00", 240)
