"""Opt-in isolated-CI fixture workload evidence; never a production load tool.

One pinned synthetic upload, one native seed evaluation, then a pair of native
seed evaluations. Only HTTP is used; the existing workers retain Docker control.
Every emitted value is a constant, bounded count, duration or boolean.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import io
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "/api/v1"
FIXTURE_SHA = "2ad75d287f03a5186201c1d9a1d029dd15940c8d78649c8111ae38d1a2046a1b"
SEED_SHA = "09600e5e14cb293dfd0411b697d4ff95774c045845884620d4686f11485401ba"
START, END = "2024-01-02", "2024-04-27"
UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
GET_ROUTES = re.compile(
    rf"{PREFIX}/(?:data/providers|research-capabilities|strategy-versions"
    rf"|data/imports/{UUID_PATTERN}|datasets/{UUID_PATTERN}|runs/{UUID_PATTERN})\Z"
)
POST_ROUTES = re.compile(
    rf"{PREFIX}/(?:login/access-token|data/uploads|runs"
    rf"|(?:data/imports|runs)/{UUID_PATTERN}/cancel)\Z"
)
ERROR_CODES = {
    "invalid_arguments",
    "invalid_origin",
    "opt_in_required",
    "ci_required",
    "fixture_mode_required",
    "docker_required",
    "missing_credentials",
    "invalid_credentials",
    "invalid_request",
    "invalid_response",
    "response_too_large",
    "request_too_large",
    "redirect_blocked",
    "unsupported_encoding",
    "timeout",
    "transport_error",
    "invalid_token",
    "fixture_integrity",
    "seed_not_found",
    "import_failed",
    "run_failed",
    "invalid_execution_evidence",
    "concurrency_not_observed",
    "request_budget",
    "internal_error",
}
MAX_REQUESTS = 1024
CLEANUP_RESERVE_SECONDS = 30
TERMINAL_RUN = {"succeeded", "failed", "cancelled", "interrupted", "paused"}
TERMINAL_IMPORT = {"ready", "failed", "cancelled", "partial"}


class BenchmarkError(Exception):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "internal_error"
        super().__init__(self.code)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise BenchmarkError("invalid_arguments")


def bounded_int(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise ValueError
        return number

    return parse


def parse_args(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:8000")
    parser.add_argument("--allow-fixture-workload-mutations", action="store_true")
    parser.add_argument("--timeout-seconds", type=bounded_int(1, 10), default=5)
    parser.add_argument(
        "--total-timeout-seconds", type=bounded_int(60, 600), default=300
    )
    parser.add_argument(
        "--body-cap-bytes", type=bounded_int(16384, 2097152), default=1048576
    )
    parser.add_argument("--poll-milliseconds", type=bounded_int(100, 1000), default=500)
    return parser.parse_args(argv)


def parse_origin(value):
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or not address.is_loopback
            or (isinstance(address, ipaddress.IPv6Address) and address.scope_id)
        ):
            raise ValueError
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            raise ValueError
        return str(address), port
    except (ValueError, TypeError):
        raise BenchmarkError("invalid_origin") from None


def guard(args, env):
    parse_origin(args.origin)
    if not args.allow_fixture_workload_mutations:
        raise BenchmarkError("opt_in_required")
    if env.get("GITHUB_ACTIONS") != "true":
        raise BenchmarkError("ci_required")
    if env.get("DATA_PROVIDER_MODE") != "fixture":
        raise BenchmarkError("fixture_mode_required")
    if env.get("TRADEGRAD_REQUIRE_DOCKER") != "1":
        raise BenchmarkError("docker_required")
    if not env.get("E2E_EMAIL") or not env.get("E2E_PASSWORD"):
        raise BenchmarkError("missing_credentials")
    if any(
        not isinstance(env[key], str)
        or len(env[key]) > 1024
        or any(ord(c) < 32 for c in env[key])
        for key in ("E2E_EMAIL", "E2E_PASSWORD")
    ):
        raise BenchmarkError("invalid_credentials")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(UUID_PATTERN, value):
        raise BenchmarkError("invalid_response")
    return str(UUID(value))


def token_value(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 16384
        or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", value)
    ):
        raise BenchmarkError("invalid_token")
    return value


class HTTPTransport:
    """Direct literal-loopback HTTP, no DNS, proxy, redirects or ambient cookies.

    A watchdog interrupts the connected socket throughout response headers and
    streamed bodies, retaining it when http.client detaches Connection: close.
    Deadlines are also checked around bounded parsing; this is not a real-time
    process-kill guarantee against scheduler or CPU stalls.
    """

    def __init__(self, origin, cap, connection_factory=http.client.HTTPConnection):
        self.host, self.port = parse_origin(origin)
        self.cap, self.connection_factory = cap, connection_factory

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
        parsed = urlsplit(path)
        if (
            not 0 < timeout <= 10
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or any(ord(c) <= 32 for c in path)
            or (
                method == "GET"
                and (not GET_ROUTES.fullmatch(parsed.path) or body is not None)
            )
            or (method == "POST" and (not POST_ROUTES.fullmatch(path) or parsed.query))
            or method not in {"GET", "POST"}
        ):
            raise BenchmarkError("invalid_request")
        if body is not None and (not isinstance(body, bytes) or len(body) > self.cap):
            raise BenchmarkError("request_too_large")
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if token is not None:
            headers["Authorization"] = "Bearer " + token_value(token)
        if key is not None:
            headers["Idempotency-Key"] = identifier(key)
        if content_type is not None:
            if content_type not in {
                "application/json",
                "application/x-www-form-urlencoded",
            } and not re.fullmatch(
                r"multipart/form-data; boundary=tradegrad-[0-9a-f]{32}", content_type
            ):
                raise BenchmarkError("invalid_request")
            headers["Content-Type"] = content_type
        connection = response = timer = connected_socket = None
        expired = threading.Event()
        timer_started = False
        deadline = time.monotonic() + timeout

        def remaining():
            value = deadline - time.monotonic()
            if expired.is_set() or value <= 0:
                raise BenchmarkError("timeout")
            return value

        def interrupt():
            expired.set()
            # Keep the connected socket after getresponse() hands a closing
            # connection to HTTPResponse. connection.sock is then None.
            sock = connected_socket
            if sock is None and connection is not None:
                sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

        try:
            connection = self.connection_factory(
                self.host, self.port, timeout=remaining()
            )
            timer = threading.Timer(remaining(), interrupt)
            timer.daemon = True
            timer.start()
            timer_started = True
            # Before connect() returns there may be no exposed socket for the
            # watchdog to interrupt; use only the still-remaining socket budget.
            connection.timeout = remaining()
            connection.connect()
            connected_socket = connection.sock
            socket_timeout = remaining()
            if connected_socket is not None:
                connected_socket.settimeout(socket_timeout)
            connection.request(method, path, body=body, headers=headers)
            remaining()
            response = connection.getresponse()
            remaining()
            if 300 <= response.status < 400:
                raise BenchmarkError("redirect_blocked")
            if not 200 <= response.status < 300:
                raise BenchmarkError("invalid_response")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise BenchmarkError("unsupported_encoding")
            length = response.getheader("Content-Length")
            if length is not None:
                if not length.isdecimal():
                    raise BenchmarkError("invalid_response")
                if int(length) > self.cap:
                    raise BenchmarkError("response_too_large")
            content = bytearray()
            while True:
                remaining()
                chunk = response.read1(min(65536, self.cap + 1 - len(content)))
                remaining()
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > self.cap:
                    raise BenchmarkError("response_too_large")
            if length is not None and len(content) != int(length):
                raise BenchmarkError("invalid_response")
            value = json.loads(
                content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
            )
            remaining()
            if not isinstance(value, (dict, list)):
                raise BenchmarkError("invalid_response")
            return value
        except BenchmarkError:
            raise
        except TimeoutError:
            raise BenchmarkError("timeout") from None
        except (OSError, http.client.HTTPException):
            raise BenchmarkError(
                "timeout" if expired.is_set() else "transport_error"
            ) from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise BenchmarkError("invalid_response") from None
        finally:
            if timer is not None:
                timer.cancel()
                if timer_started:
                    timer.join(timeout=0.1)
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()


def load_fixture():
    path = ROOT / "frontend/tests/fixtures/research-cn.csv"
    if path.is_symlink() or path.stat().st_size > 32768:
        raise BenchmarkError("fixture_integrity")
    content = path.read_bytes()
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
    if hashlib.sha256(content).hexdigest() != FIXTURE_SHA or len(rows) != 76:
        raise BenchmarkError("fixture_integrity")
    return content


def upload_body(content):
    mapping = {
        "name": "M6 bounded synthetic workload, NOT market evidence",
        "symbol": "600000",
        "market": "CN",
        "mic": "XSHG",
        "currency": "CNY",
        "timezone": "Asia/Shanghai",
        "calendar": "XSHG",
        "start_date": START,
        "end_date": END,
        "columns": {
            "session_date": "date",
            **{
                key: key for key in ("open", "high", "low", "close", "volume", "amount")
            },
        },
        "volume_unit": "shares",
        "unknown_flags": [],
        "price_semantics": {
            "native_mode": "user_declared_synthetic",
            "split_basis": "unadjusted",
            "dividend_basis": "unadjusted",
            "note": "Synthetic engine fixture, not market prices",
        },
    }
    boundary = "tradegrad-" + uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="mapping"\r\n\r\n'.encode()
        + json.dumps(mapping, separators=(",", ":")).encode()
        + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="research-cn.csv"\r\nContent-Type: text/csv\r\n\r\n'.encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, "multipart/form-data; boundary=" + boundary


def summary(values):
    ordered = sorted(values)

    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        return round(
            ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3
        )

    return {
        "count": len(ordered),
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "max_ms": round(max(ordered), 3),
    }


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        raise BenchmarkError("invalid_execution_evidence") from None


def hardware():
    """Numeric host context only: never hostnames, environment values or paths."""
    cpus = os.cpu_count()
    try:
        memory = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError):
        memory = None
    return {
        "logical_cpus": cpus if type(cpus) is int and 0 < cpus <= 1000000 else None,
        "physical_memory_bytes": memory
        if type(memory) is int and 0 < memory <= 10**16
        else None,
        "scope": "reported_host_not_container_reservation",
    }


class Driver:
    def __init__(self, args, transport, env, clock, sleep):
        self.args, self.transport, self.env = args, transport, env
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.deadline = self.started + args.total_timeout_seconds
        self.work_deadline = self.deadline - CLEANUP_RESERVE_SECONDS
        self.token = None
        self.owned = {}
        self.unknown_mutation = False
        self.requests = 0
        self.timings = defaultdict(list)
        self.report = {
            "schema_version": "tradegrad.fixture-workloads.v1",
            "status": "failed",
            "scope": "isolated_ci_synthetic_native_seed_only",
            "capacity_claim": False,
            "live_data": False,
            "model_calls": 0,
            "workload": {
                "listings": 1,
                "daily_bars": 76,
                "import_samples": 1,
                "single_evaluation_samples": 1,
                "concurrent_runs": 2,
                "maximum_created_imports": 1,
                "maximum_created_runs": 3,
                "maximum_created_experiments": 0,
            },
            "bounds": {
                "total_seconds": args.total_timeout_seconds,
                "request_seconds": args.timeout_seconds,
                "body_bytes": args.body_cap_bytes,
                "requests": MAX_REQUESTS,
                "poll_milliseconds": args.poll_milliseconds,
                "cleanup_reserve_seconds": CLEANUP_RESERVE_SECONDS,
            },
            "created": {"imports": 0, "runs": 0},
            "hardware": hardware(),
        }

    def call(self, method, path, label, *, cleanup=False, **kwargs):
        deadline = self.deadline if cleanup else self.work_deadline
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise BenchmarkError("timeout")
        if self.requests >= MAX_REQUESTS - (0 if cleanup else 8):
            raise BenchmarkError("request_budget")
        self.requests += 1
        before = self.clock()
        try:
            return self.transport.request(
                method,
                PREFIX + path,
                token=self.token,
                timeout=min(self.args.timeout_seconds, remaining),
                **kwargs,
            )
        finally:
            self.timings[label].append(max(0, (self.clock() - before) * 1000))

    def create(self, kind, path, label, **kwargs):
        cap = 1 if kind == "imports" else 3
        if self.report["created"][kind] >= cap:
            raise BenchmarkError("request_budget")
        # Never retry a POST whose response was lost. No ID means cleanup is unknown.
        self.unknown_mutation = True
        value = self.call("POST", path, label, key=str(uuid4()), **kwargs)
        owned_id = identifier(value.get("id"))
        if owned_id in self.owned:
            raise BenchmarkError("invalid_response")
        self.owned[owned_id] = {"kind": kind, "terminal": False}
        self.report["created"][kind] += 1
        self.unknown_mutation = False
        return owned_id

    def pause(self):
        remaining = self.work_deadline - self.clock()
        if remaining <= 0:
            raise BenchmarkError("timeout")
        self.sleep(min(self.args.poll_milliseconds / 1000, remaining))

    def preflight(self):
        auth = self.call(
            "POST",
            "/login/access-token",
            "login",
            body=urlencode(
                {
                    "username": self.env["E2E_EMAIL"],
                    "password": self.env["E2E_PASSWORD"],
                }
            ).encode(),
            content_type="application/x-www-form-urlencoded",
        )
        if auth.get("token_type") != "bearer":
            raise BenchmarkError("invalid_token")
        self.token = token_value(auth.get("access_token"))
        providers = self.call("GET", "/data/providers", "fixture_preflight")
        capabilities = self.call("GET", "/research-capabilities", "fixture_preflight")
        if (
            not isinstance(providers, list)
            or not providers
            or len(providers) > 16
            or not all(
                isinstance(p, dict) and p.get("runtime_mode") == "fixture"
                for p in providers
            )
            or capabilities.get("live_model_calls") is not False
            or capabilities.get("model_providers") != ["fixture"]
            or capabilities.get("models") != ["deterministic-fixture-v1"]
        ):
            raise BenchmarkError("fixture_mode_required")
        for offset in range(0, 500, 100):
            page = self.call(
                "GET", f"/strategy-versions?offset={offset}&limit=100", "seed_metadata"
            )
            rows = page.get("data")
            if not isinstance(rows, list) or len(rows) > 100:
                raise BenchmarkError("invalid_response")
            for row in rows:
                if (
                    row.get("name") == "Original cn_time seed"
                    and row.get("task") == "cn_time"
                    and row.get("mode") == "native_akquant"
                    and row.get("parent_id") is None
                    and row.get("content_sha256") == SEED_SHA
                ):
                    return identifier(row.get("id"))
            if len(rows) < 100:
                break
        raise BenchmarkError("seed_not_found")

    def import_fixture(self, content):
        self.report["workload"]["uploaded_csv_bytes"] = len(content)
        body, content_type = upload_body(content)
        started = self.clock()
        imported = self.create(
            "imports",
            "/data/uploads",
            "upload_submission",
            body=body,
            content_type=content_type,
        )
        while True:
            detail = self.call(
                "GET", f"/data/imports/{imported}?block_limit=1", "import_poll"
            )
            if identifier(detail.get("id")) != imported:
                raise BenchmarkError("invalid_response")
            if detail.get("status") in TERMINAL_IMPORT:
                self.owned[imported]["terminal"] = True
                if detail["status"] != "ready":
                    raise BenchmarkError("import_failed")
                break
            self.pause()
        self.report["import"] = {
            "completion_wall_ms": round((self.clock() - started) * 1000, 3),
            "measurement": "upload_start_to_ready_observation_including_queue_and_poll",
        }
        dataset_id = identifier(detail.get("dataset_version_id"))
        dataset = self.call("GET", f"/datasets/{dataset_id}", "dataset_metadata")
        manifest = dataset.get("manifest", {})
        if (
            dataset.get("id") != dataset_id
            or dataset.get("is_synthetic") is not True
            or dataset.get("start_date") != START
            or dataset.get("end_date") != END
            or manifest.get("row_count") != 76
            or len(manifest.get("listings", [])) != 1
            or not re.fullmatch(r"[0-9a-f]{64}", str(dataset.get("content_sha256")))
        ):
            raise BenchmarkError("fixture_integrity")
        return dataset

    def verify_execution(self, detail, dataset, seed):
        result, attempts = detail.get("result", {}), detail.get("attempts", [])
        config = detail.get("config", {})
        if (
            detail.get("execution_mode") != "seed_evaluation"
            or detail.get("generation") != 1
            or detail.get("dataset_version_id") != dataset["id"]
            or detail.get("strategy_version_id") != seed
            or config.get("dataset_sha256") != dataset["content_sha256"]
            or config.get("strategy_sha256") != SEED_SHA
            or config.get("task") != "cn_time"
            or result.get("execution_mode") != "seed_evaluation"
            or result.get("is_synthetic") is not True
            or result.get("evaluation_mode") != "native_akquant"
            or result.get("result_trust") != "native_strategy_and_engine_share_process"
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(result.get("image_digest")))
            or not isinstance(attempts, list)
            or len(attempts) != 1
        ):
            raise BenchmarkError("invalid_execution_evidence")
        attempt = attempts[0]
        if (
            attempt.get("status") != "succeeded"
            or attempt.get("sandbox_stopped") is not True
            or attempt.get("exit_code") != 0
            or attempt.get("generation") != 1
        ):
            raise BenchmarkError("invalid_execution_evidence")
        start, end = (
            timestamp(attempt.get("started_at")),
            timestamp(attempt.get("finished_at")),
        )
        if not 0 < (end - start).total_seconds() <= self.args.total_timeout_seconds:
            raise BenchmarkError("invalid_execution_evidence")
        return start, end

    def evaluate(self, dataset, seed, size):
        body = json.dumps(
            {
                "task": "cn_time",
                "dataset_version_id": dataset["id"],
                "strategy_version_id": seed,
                "start_date": START,
                "end_date": END,
                "profile": "corrected_explicit_config",
            },
            separators=(",", ":"),
        ).encode()
        batch_started = self.clock()
        pending, completed = {}, {}
        for _ in range(size):
            started = self.clock()
            run_id = self.create(
                "runs",
                "/runs",
                "run_submission",
                body=body,
                content_type="application/json",
            )
            pending[run_id] = started
        while pending:
            for run_id, started in list(pending.items()):
                detail = self.call("GET", f"/runs/{run_id}", "run_poll")
                if identifier(detail.get("id")) != run_id:
                    raise BenchmarkError("invalid_response")
                if detail.get("status") not in TERMINAL_RUN:
                    continue
                if detail.get("status") != "succeeded":
                    raise BenchmarkError("run_failed")
                interval = self.verify_execution(detail, dataset, seed)
                self.owned[run_id]["terminal"] = True
                completed[run_id] = ((self.clock() - started) * 1000, interval)
                del pending[run_id]
            if pending:
                self.pause()
        wall = [round(value[0], 3) for value in completed.values()]
        intervals = [value[1] for value in completed.values()]
        evidence = {
            "completion_wall_ms": wall,
            "batch_wall_ms": round((self.clock() - batch_started) * 1000, 3),
            "measurement": "submission_start_to_verified_terminal_observation_including_queue_and_poll",
            "native_docker_successes": size,
            "sandbox_stopped": True,
            "persisted_attempt_duration_ms": [
                round((end - start).total_seconds() * 1000, 3)
                for start, end in intervals
            ],
        }
        if size == 2:
            overlap = max(
                0,
                (
                    min(end for _, end in intervals)
                    - max(start for start, _ in intervals)
                ).total_seconds(),
            )
            evidence.update(
                attempt_overlap_ms=round(overlap * 1000, 3),
                overlapping_attempts=overlap > 0,
            )
        return evidence

    def cleanup(self):
        pending = [
            (key, value) for key, value in self.owned.items() if not value["terminal"]
        ]
        requested = confirmed = 0
        for owned_id, record in pending:
            path = (
                "/data/imports/" if record["kind"] == "imports" else "/runs/"
            ) + owned_id
            try:
                value = self.call(
                    "POST", path + "/cancel", "owned_cleanup", cleanup=True
                )
                if value.get("id") == owned_id:
                    requested += 1
            except Exception:  # noqa: BLE001, S110 - never emit private exception text
                pass
        for owned_id, record in pending:
            path = (
                "/data/imports/" if record["kind"] == "imports" else "/runs/"
            ) + owned_id
            try:
                value = self.call("GET", path, "owned_cleanup", cleanup=True)
                terminal = (
                    TERMINAL_IMPORT if record["kind"] == "imports" else TERMINAL_RUN
                )
                stopped = record["kind"] == "imports" or all(
                    a.get("sandbox_stopped") is True for a in value.get("attempts", [])
                )
                if (
                    value.get("id") == owned_id
                    and value.get("status") in terminal
                    and stopped
                ):
                    confirmed += 1
            except Exception:  # noqa: BLE001, S110 - never emit private exception text
                pass
        return {
            "tracked_nonterminal": len(pending),
            "cancellations_accepted": requested,
            "terminal_confirmed": confirmed,
            "unknown_mutation_outcome": self.unknown_mutation,
            "cleanup_uncertain": confirmed != len(pending) or self.unknown_mutation,
            "durable_records_retained": True,
        }

    def run(self):
        try:
            seed = self.preflight()
            dataset = self.import_fixture(load_fixture())
            self.report["single_evaluation"] = self.evaluate(dataset, seed, 1)
            self.report["concurrent_pair"] = self.evaluate(dataset, seed, 2)
            if not self.report["concurrent_pair"]["overlapping_attempts"]:
                raise BenchmarkError("concurrency_not_observed")
            self.report["status"] = "succeeded"
        except BenchmarkError as exc:
            self.report["error_code"] = exc.code
        except Exception:  # noqa: BLE001 - allowlisted failure projection
            self.report["error_code"] = "internal_error"
        finally:
            self.report["cleanup"] = self.cleanup()
            self.report["control_http_latency"] = {
                label: summary(values) for label, values in self.timings.items()
            }
            self.report["total_wall_ms"] = round(
                (self.clock() - self.started) * 1000, 3
            )
            self.report["http_requests"] = self.requests
        return self.report


def run_benchmark(
    args, transport=None, env=None, clock=time.monotonic, sleep=time.sleep
):
    env = os.environ if env is None else env
    guard(args, env)
    return Driver(
        args,
        transport or HTTPTransport(args.origin, args.body_cap_bytes),
        env,
        clock,
        sleep,
    ).run()


def main(argv=None):
    try:
        result = run_benchmark(parse_args(argv))
    except BenchmarkError as exc:
        result = {
            "schema_version": "tradegrad.fixture-workloads.v1",
            "status": "failed",
            "error_code": exc.code,
        }
    except Exception:  # noqa: BLE001 - allowlisted failure projection
        result = {
            "schema_version": "tradegrad.fixture-workloads.v1",
            "status": "failed",
            "error_code": "internal_error",
        }
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
