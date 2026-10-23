"""Pure fake transport contracts; no sockets, databases, Docker or engines."""

import copy
import http.client as http_client
import io
import json
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from script import benchmark_v1 as reference_bench
from script import benchmark_v1_workloads as bench

PRIVATE = "PRIVATE_SOURCE_ACCOUNT_CONFIG_TRACE_ERROR"


def arguments(*extra):
    return bench.parse_args(["--allow-fixture-workload-mutations", *extra])


@pytest.fixture
def environment():
    return {
        "GITHUB_ACTIONS": "true",
        "DATA_PROVIDER_MODE": "fixture",
        "TRADEGRAD_REQUIRE_DOCKER": "1",
        "E2E_EMAIL": "fixture@example.invalid",
        "E2E_PASSWORD": secrets.token_urlsafe(32),
    }


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeTransport:
    def __init__(self, env, clock):
        self.env, self.clock = env, clock
        self.token = secrets.token_urlsafe(32)
        self.seed, self.dataset, self.imported = (str(uuid4()) for _ in range(3))
        self.calls, self.runs, self.polls = [], [], {}
        self.live = False
        self.models = ["fixture"]
        self.overlap = True
        self.hang = False
        self.fail_run = False
        self.lost_post = False
        self.invalid_image = False
        self.cleanup_complete = False
        self.dataset_rows = 76

    def detail(self, run):
        index = self.runs.index(run)
        start = datetime(2024, 1, 2, tzinfo=UTC) + timedelta(seconds=20 * index)
        if index == 2 and self.overlap:
            start -= timedelta(seconds=15)
        return {
            "id": run,
            "status": "succeeded",
            "execution_mode": "seed_evaluation",
            "generation": 1,
            "dataset_version_id": self.dataset,
            "strategy_version_id": self.seed,
            "config": {
                "dataset_sha256": "a" * 64,
                "strategy_sha256": bench.SEED_SHA,
                "task": "cn_time",
                "source": PRIVATE,
            },
            "result": {
                "execution_mode": "seed_evaluation",
                "is_synthetic": True,
                "evaluation_mode": "native_akquant",
                "result_trust": "native_strategy_and_engine_share_process",
                "image_digest": None if self.invalid_image else "sha256:" + "b" * 64,
                "trace": PRIVATE,
            },
            "attempts": [
                {
                    "id": str(uuid4()),
                    "status": "succeeded",
                    "generation": 1,
                    "sandbox_stopped": True,
                    "exit_code": 0,
                    "started_at": start.isoformat(),
                    "finished_at": (start + timedelta(seconds=10)).isoformat(),
                    "error": PRIVATE,
                }
            ],
            "private": PRIVATE,
        }

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
        parsed = urlsplit(path)
        route = parsed.path.removeprefix(bench.PREFIX)
        if route == "/login/access-token":
            assert token is None
            credentials_match = parse_qs(body.decode()) == {
                "username": [self.env["E2E_EMAIL"]],
                "password": [self.env["E2E_PASSWORD"]],
            }
            assert credentials_match
            return {
                "access_token": self.token,
                "token_type": "bearer",
                "private": PRIVATE,
            }
        token_matches = secrets.compare_digest(token, self.token)
        assert token_matches
        if route == "/data/providers":
            return [
                {
                    "id": "akshare",
                    "runtime_mode": "live" if self.live else "fixture",
                    "private": PRIVATE,
                }
            ]
        if route == "/research-capabilities":
            return {
                "live_model_calls": False,
                "model_providers": self.models,
                "models": ["deterministic-fixture-v1"],
                "private": PRIVATE,
            }
        if route == "/strategy-versions":
            return {
                "data": [
                    {
                        "id": self.seed,
                        "name": "Original cn_time seed",
                        "task": "cn_time",
                        "mode": "native_akquant",
                        "parent_id": None,
                        "content_sha256": bench.SEED_SHA,
                    }
                ],
                "private": PRIVATE,
            }
        if route == "/data/uploads":
            assert bench.load_fixture() in body
            assert 'name="mapping"' in body.decode()
            assert content_type.startswith("multipart/form-data; boundary=")
            return {"id": self.imported, "status": "requested", "private": PRIVATE}
        if route == "/data/imports/" + self.imported:
            return {
                "id": self.imported,
                "status": "ready",
                "dataset_version_id": self.dataset,
                "private": PRIVATE,
            }
        if route == "/datasets/" + self.dataset:
            return {
                "id": self.dataset,
                "is_synthetic": True,
                "start_date": bench.START,
                "end_date": bench.END,
                "content_sha256": "a" * 64,
                "manifest": {
                    "row_count": self.dataset_rows,
                    "listings": [{"source": PRIVATE}],
                },
            }
        if route == "/runs":
            assert method == "POST" and content_type == "application/json"
            assert json.loads(body) == {
                "task": "cn_time",
                "dataset_version_id": self.dataset,
                "strategy_version_id": self.seed,
                "start_date": bench.START,
                "end_date": bench.END,
                "profile": "corrected_explicit_config",
            }
            if self.lost_post:
                raise bench.BenchmarkError("transport_error")
            run = str(uuid4())
            self.runs.append(run)
            return {"id": run, "status": "queued", "private": PRIVATE}
        if route.endswith("/cancel"):
            assert method == "POST"
            owned = route.split("/")[-2]
            assert owned == self.imported or owned in self.runs
            return {"id": owned, "status": "cancel_requested", "private": PRIVATE}
        if route.startswith("/runs/"):
            run = route.split("/")[-1]
            assert run in self.runs
            self.polls[run] = self.polls.get(run, 0) + 1
            if self.cleanup_complete and self.fail_run:
                return {
                    "id": run,
                    "status": "cancelled",
                    "attempts": [{"sandbox_stopped": True}],
                }
            if self.fail_run:
                return {
                    "id": run,
                    "status": "failed",
                    "error": PRIVATE,
                    "attempts": [{"sandbox_stopped": False}],
                }
            if self.hang or self.polls[run] == 1:
                return {"id": run, "status": "running", "error": PRIVATE}
            return self.detail(run)
        raise AssertionError("Unexpected fake route")


def run(env, transport=None, *extra):
    clock = transport.clock if transport else Clock()
    transport = transport or FakeTransport(env, clock)
    return bench.run_benchmark(
        arguments(*extra), transport, env, clock, clock.sleep
    ), transport


def test_full_bounded_workload_and_private_allowlist(environment):
    report, transport = run(environment)
    assert report["status"] == "succeeded"
    assert report["created"] == {"imports": 1, "runs": 3}
    assert report["workload"]["maximum_created_experiments"] == 0
    assert report["single_evaluation"]["native_docker_successes"] == 1
    assert report["concurrent_pair"]["native_docker_successes"] == 2
    assert report["concurrent_pair"]["attempt_overlap_ms"] == 5000
    assert len(report["concurrent_pair"]["completion_wall_ms"]) == 2
    assert report["cleanup"]["cleanup_uncertain"] is False
    assert report["control_http_latency"]["run_submission"]["count"] == 3
    assert all(
        item[1].split("?")[0] != bench.PREFIX + "/research-runs"
        for item in transport.calls
    )
    keys = [key for _, _, key in transport.calls if key]
    assert len(keys) == len(set(keys)) == 4
    encoded = json.dumps(report)
    for secret in (
        PRIVATE,
        environment["E2E_EMAIL"],
        environment["E2E_PASSWORD"],
        transport.token,
        transport.seed,
        transport.dataset,
        transport.imported,
        *transport.runs,
    ):
        contains_forbidden = secret in encoded
        assert not contains_forbidden


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("GITHUB_ACTIONS", "false", "ci_required"),
        ("DATA_PROVIDER_MODE", "live", "fixture_mode_required"),
        ("TRADEGRAD_REQUIRE_DOCKER", "0", "docker_required"),
        ("E2E_PASSWORD", "", "missing_credentials"),
        ("E2E_PASSWORD", "\r\n", "invalid_credentials"),
    ],
)
def test_guards_fail_before_any_http(environment, field, value, error):
    environment[field] = value
    transport = FakeTransport(environment, Clock())
    with pytest.raises(bench.BenchmarkError, match="^" + error + "$"):
        run(environment, transport)
    assert transport.calls == []


def test_opt_in_is_not_implicit(environment):
    transport = FakeTransport(environment, Clock())
    with pytest.raises(bench.BenchmarkError, match="^opt_in_required$"):
        bench.run_benchmark(bench.parse_args([]), transport, environment)
    assert transport.calls == []


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8000",
        "http://example.invalid",
        "https://127.0.0.1",
        "http://127.0.0.1/path",
        "http://127.0.0.1?secret",
        pytest.param(
            f"http://user:{secrets.token_urlsafe(32)}@127.0.0.1", id="userinfo"
        ),
        "http://0.0.0.0",
        "http://[::]",
        "http://[::1%eth0]",
        "http://127.0.0.1:0",
    ],
)
def test_non_literal_loopback_origins_are_rejected(origin):
    with pytest.raises(bench.BenchmarkError, match="^invalid_origin$"):
        bench.parse_origin(origin)


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8000", "http://[::1]:8000"])
def test_literal_loopback_is_allowed(origin):
    assert bench.parse_origin(origin)[1] == 8000


@pytest.mark.parametrize(
    "option,value",
    [
        ("--total-timeout-seconds", "59"),
        ("--total-timeout-seconds", "601"),
        ("--timeout-seconds", "11"),
        ("--body-cap-bytes", "2097153"),
        ("--poll-milliseconds", "0"),
        ("--samples", "100"),
        ("--origin-secret", PRIVATE),
    ],
)
def test_parser_bounds_and_error_redaction(option, value):
    with pytest.raises(bench.BenchmarkError, match="^invalid_arguments$"):
        arguments(option, value)


@pytest.mark.parametrize("setting", ["live", "models"])
def test_server_modes_are_verified_before_mutations(environment, setting):
    transport = FakeTransport(environment, Clock())
    setattr(transport, setting, True if setting == "live" else ["paid-model"])
    report, _ = run(environment, transport)
    assert report["error_code"] == "fixture_mode_required"
    assert report["created"] == {"imports": 0, "runs": 0}
    assert [path for method, path, _ in transport.calls if method == "POST"] == [
        bench.PREFIX + "/login/access-token"
    ]


def test_two_completed_serial_attempts_do_not_pass_concurrency(environment):
    transport = FakeTransport(environment, Clock())
    transport.overlap = False
    report, _ = run(environment, transport)
    assert report["status"] == "failed"
    assert report["error_code"] == "concurrency_not_observed"
    assert report["concurrent_pair"]["attempt_overlap_ms"] == 0
    assert report["created"]["runs"] == 3


def test_failure_cancels_only_returned_ids_and_does_not_claim_cleanup(environment):
    transport = FakeTransport(environment, Clock())
    transport.fail_run = True
    report, _ = run(environment, transport)
    assert report["error_code"] == "run_failed"
    cancels = [
        path
        for method, path, _ in transport.calls
        if method == "POST" and path.endswith("/cancel")
    ]
    assert cancels == [bench.PREFIX + "/runs/" + transport.runs[0] + "/cancel"]
    assert report["cleanup"]["cancellations_accepted"] == 1
    assert report["cleanup"]["terminal_confirmed"] == 0
    assert report["cleanup"]["cleanup_uncertain"] is True


def test_lost_post_is_not_retried_or_broadly_cleaned_up(environment):
    transport = FakeTransport(environment, Clock())
    transport.lost_post = True
    report, _ = run(environment, transport)
    assert report["error_code"] == "transport_error"
    assert report["cleanup"]["unknown_mutation_outcome"] is True
    assert report["cleanup"]["cleanup_uncertain"] is True
    assert sum(path == bench.PREFIX + "/runs" for _, path, _ in transport.calls) == 1
    assert not any(path.endswith("/cancel") for _, path, _ in transport.calls)


def test_deadline_and_request_budget_remain_bounded(environment):
    transport = FakeTransport(environment, Clock())
    transport.hang = True
    report, _ = run(environment, transport, "--total-timeout-seconds", "60")
    assert report["error_code"] == "timeout"
    assert report["total_wall_ms"] <= 60000
    assert report["http_requests"] <= bench.MAX_REQUESTS
    assert report["created"]["runs"] == 1
    assert report["cleanup"]["cancellations_accepted"] == 1


def test_actual_native_image_evidence_is_required(environment):
    transport = FakeTransport(environment, Clock())
    transport.invalid_image = True
    report, _ = run(environment, transport)
    assert report["error_code"] == "invalid_execution_evidence"
    assert "single_evaluation" not in report


def test_published_data_size_must_match_pinned_input(environment):
    transport = FakeTransport(environment, Clock())
    transport.dataset_rows = 75
    report, _ = run(environment, transport)
    assert report["error_code"] == "fixture_integrity"
    assert report["created"]["runs"] == 0


def test_fixture_is_exact_existing_synthetic_file():
    assert len(bench.load_fixture().decode().splitlines()) == 77


class Response:
    def __init__(self, body=b"{}", status=200, headers=None):
        self.stream, self.status, self.headers = io.BytesIO(body), status, headers or {}

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def close(self):
        self.stream.close()

    def read1(self, size):
        return self.stream.read(size)


class Connection:
    def __init__(self, response):
        self.response, self.sock, self.calls, self.closed = response, None, [], False

    def connect(self):
        pass

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def http(response, cap=16384):
    connection = Connection(response)
    return bench.HTTPTransport(
        "http://127.0.0.1:8000", cap, lambda *args, **kwargs: connection
    ), connection


@pytest.mark.parametrize(
    "response,error",
    [
        (Response(status=302), "redirect_blocked"),
        (Response(body=b"x" * 16385), "response_too_large"),
        (Response(headers={"Content-Length": "16385"}), "response_too_large"),
        (Response(headers={"Content-Length": "100"}), "invalid_response"),
        (Response(headers={"Content-Length": "1"}), "invalid_response"),
        (Response(headers={"Content-Length": "-1"}), "invalid_response"),
        (Response(headers={"Content-Encoding": "gzip"}), "unsupported_encoding"),
        (Response(body=b'{"secret":NaN}'), "invalid_response"),
        (Response(body=b'"secret"'), "invalid_response"),
        (Response(body=b"not-json"), "invalid_response"),
    ],
)
def test_transport_rejects_unsafe_responses_without_sockets(response, error):
    transport, connection = http(copy.deepcopy(response))
    with pytest.raises(bench.BenchmarkError, match="^" + error + "$"):
        transport.request("GET", bench.PREFIX + "/data/providers", timeout=1)
    assert connection.closed


@pytest.mark.parametrize(
    "method,path",
    [
        ("DELETE", "/runs"),
        ("POST", "/data/imports"),
        ("POST", "/strategy-versions"),
        ("POST", "/research-runs"),
        ("GET", "//example.invalid"),
        ("POST", "/runs?outside=scope"),
        ("GET", "/runs"),
    ],
)
def test_transport_route_allowlist_cannot_invoke_other_mutations(method, path):
    transport, connection = http(Response())
    with pytest.raises(bench.BenchmarkError, match="^invalid_request$"):
        transport.request(method, bench.PREFIX + path, timeout=1)
    assert not connection.calls


def test_transport_caps_request_body_before_connecting():
    transport, connection = http(Response())
    with pytest.raises(bench.BenchmarkError, match="^request_too_large$"):
        transport.request(
            "POST", bench.PREFIX + "/data/uploads", timeout=1, body=b"x" * 16385
        )
    assert not connection.calls


def test_main_suppresses_raw_unexpected_errors(monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(bench, "run_benchmark", fail)
    assert bench.main([]) == 1
    value = capsys.readouterr()
    assert PRIVATE not in value.out + value.err
    assert json.loads(value.out)["error_code"] == "internal_error"


def test_absolute_request_deadline_interrupts_before_sending(monkeypatch):
    class ExpiredTimer:
        def __init__(self, timeout, callback):
            self.callback = callback

        def start(self):
            self.callback()

        def cancel(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(bench.threading, "Timer", ExpiredTimer)
    transport, connection = http(Response())
    with pytest.raises(bench.BenchmarkError, match="^timeout$"):
        transport.request("GET", bench.PREFIX + "/data/providers", timeout=1)
    assert connection.calls == []
    assert connection.closed


def test_request_count_has_a_separate_hard_cap(environment, monkeypatch):
    monkeypatch.setattr(bench, "MAX_REQUESTS", 20)
    transport = FakeTransport(environment, Clock())
    transport.hang = True
    report, _ = run(environment, transport)
    assert report["error_code"] == "request_budget"
    assert report["http_requests"] <= 20
    assert report["cleanup"]["cancellations_accepted"] == 1


def test_fixture_hash_failure_stops_before_upload(environment, monkeypatch):
    def invalid_fixture():
        raise bench.BenchmarkError("fixture_integrity")

    monkeypatch.setattr(bench, "load_fixture", invalid_fixture)
    report, transport = run(environment)
    assert report["error_code"] == "fixture_integrity"
    assert report["created"] == {"imports": 0, "runs": 0}
    assert not any(
        path == bench.PREFIX + "/data/uploads" for _, path, _ in transport.calls
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("sandbox_stopped", False),
        ("exit_code", 1),
        ("status", "running"),
        ("generation", 2),
        ("finished_at", "not-a-timestamp"),
        ("started_at", "2024-01-02T00:00:00"),
    ],
)
def test_native_evidence_cannot_pass_with_incomplete_attempt(environment, field, value):
    transport = FakeTransport(environment, Clock())
    transport.runs.append(str(uuid4()))
    detail = transport.detail(transport.runs[0])
    detail["attempts"][0][field] = value
    driver = bench.Driver(
        arguments(), transport, environment, transport.clock, transport.clock.sleep
    )
    with pytest.raises(bench.BenchmarkError, match="^invalid_execution_evidence$"):
        driver.verify_execution(
            detail,
            {"id": transport.dataset, "content_sha256": "a" * 64},
            transport.seed,
        )


class FakeWatchdog:
    """Manual deadline trigger; no timer thread, sleep or socket is used."""

    def __init__(self, interval, function):
        self.interval, self.function = interval, function
        self.cancelled = self.joined = self.started = False

    def start(self):
        self.started = True

    def fire(self):
        assert self.started and not self.cancelled
        self.function()

    def cancel(self):
        self.cancelled = True

    def join(self, timeout=None):
        assert self.cancelled and timeout == 0.1
        self.joined = True


def parser_transport(monkeypatch, module, *, phase=None, raw=None):
    """Use the real HTTP parser/Connection: close path with only in-memory I/O."""
    timers = []
    clock = [0.0]
    raw = raw or (
        b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
    )
    header_end = raw.index(b"\r\n\r\n") + 4

    def timer_factory(interval, function):
        timer = FakeWatchdog(interval, function)
        timers.append(timer)
        return timer

    def interrupt():
        timers[0].fire()
        assert sock.shutdowns == [module.socket.SHUT_RDWR]
        raise OSError(PRIVATE)

    class Stream(io.BytesIO):
        def readline(self, size=-1):
            if phase == "headers" and self.tell() < header_end:
                interrupt()
            if phase == "chunk_framing" and self.tell() >= header_end:
                assert connection.sock is None
                interrupt()
            return super().readline(size)

        def read1(self, size=-1):
            if phase == "body":
                assert connection.sock is None
                interrupt()
            value = super().read1(size)
            if phase == "body_clock":
                clock[0] = 4.0
            return value

    class Socket:
        def __init__(self):
            self.stream = Stream(raw)
            self.shutdowns, self.timeouts = [], []
            self.closes = self.sends = 0

        def makefile(self, mode):
            assert mode == "rb"
            return self.stream

        def sendall(self, value):
            self.sends += 1
            if phase == "send":
                interrupt()

        def settimeout(self, value):
            self.timeouts.append(value)

        def shutdown(self, how):
            self.shutdowns.append(how)

        def close(self):
            # A socket with makefile() references keeps the stream alive, just
            # like the real Connection: close handoff (without a real socket).
            self.closes += 1

    sock = Socket()

    class Connection(http_client.HTTPConnection):
        def connect(self):
            self.sock = sock
            if phase == "connect":
                timers[0].fire()

    connection = Connection("127.0.0.1")
    monkeypatch.setattr(module.threading, "Timer", timer_factory)
    factory = lambda *args, **kwargs: connection
    if module is reference_bench:
        transport = module.HTTPTransport(
            "http://127.0.0.1", 3, 16384, factory, clock=lambda: clock[0]
        )
        call = lambda: transport.request("GET", "/api/v1/health/live")
    else:
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
        transport = module.HTTPTransport("http://127.0.0.1", 16384, factory)
        call = lambda: transport.request("GET", "/api/v1/data/providers", timeout=3)
    return call, connection, sock, timers, clock


@pytest.mark.parametrize("module", [reference_bench, bench])
@pytest.mark.parametrize(
    "phase", ["connect", "send", "headers", "body", "chunk_framing"]
)
def test_absolute_watchdog_keeps_real_parser_socket_after_detach(
    monkeypatch, module, phase
):
    call, connection, sock, timers, _ = parser_transport(
        monkeypatch, module, phase=phase
    )
    with pytest.raises(module.BenchmarkError, match="^timeout$"):
        call()
    assert sock.shutdowns == [module.socket.SHUT_RDWR]
    assert timers[0].cancelled and timers[0].joined
    assert connection.sock is None
    if phase == "connect":
        assert sock.sends == 0
    if phase in {"body", "chunk_framing"}:
        assert sock.stream.closed


@pytest.mark.parametrize("module", [reference_bench, bench])
def test_deadline_is_checked_after_body_read_not_just_before(monkeypatch, module):
    call, _, sock, timers, _ = parser_transport(monkeypatch, module, phase="body_clock")
    with pytest.raises(module.BenchmarkError, match="^timeout$"):
        call()
    assert sock.stream.closed
    assert timers[0].cancelled and timers[0].joined


@pytest.mark.parametrize("module", [reference_bench, bench])
def test_deadline_is_checked_after_bounded_json_decode(monkeypatch, module):
    call, _, sock, timers, clock = parser_transport(monkeypatch, module)
    original = module.json.loads

    def delayed_decode(*args, **kwargs):
        value = original(*args, **kwargs)
        clock[0] = 4.0
        return value

    monkeypatch.setattr(module.json, "loads", delayed_decode)
    with pytest.raises(module.BenchmarkError, match="^timeout$"):
        call()
    assert sock.stream.closed
    assert timers[0].cancelled and timers[0].joined


@pytest.mark.parametrize("module", [reference_bench, bench])
def test_real_http_parser_rejects_premature_eof_without_sockets(monkeypatch, module):
    call, _, sock, timers, _ = parser_transport(
        monkeypatch,
        module,
        raw=b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 100\r\n\r\n{}",
    )
    with pytest.raises(module.BenchmarkError, match="^invalid_response$"):
        call()
    assert sock.stream.closed
    assert timers[0].cancelled and timers[0].joined


@pytest.mark.parametrize("module", [reference_bench, bench])
def test_success_closes_detached_response_and_disarms_watchdog(monkeypatch, module):
    call, connection, sock, timers, _ = parser_transport(monkeypatch, module)
    assert call() == {}
    assert sock.stream.closed and connection.sock is None
    assert sock.shutdowns == []
    assert timers[0].cancelled and timers[0].joined
