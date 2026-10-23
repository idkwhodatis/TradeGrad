"""Bounded local HTTP reference evidence, not an engine/capacity benchmark.

Only login POST and an explicit GET allowlist are supported. Result-bearing GETs
can append the application's exposure records; they require an explicit opt-in.
All output is constructed from constants, validated counts and aggregate timings.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import os
import platform
import re
import socket
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID

PREFIX = "/api/v1"
UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
GET_ROUTES = re.compile(
    rf"{PREFIX}/(?:health/(?:live|ready)|research-capabilities|datasets/{UUID_PATTERN}"
    rf"|(?:research-runs|final-evaluations)(?:/{UUID_PATTERN}/evaluations)?"
    rf"|research-evaluations/{UUID_PATTERN}/results"
    rf"|final-evaluations/evaluations/{UUID_PATTERN}/result)\Z"
)
ERROR_CODES = {
    "invalid_arguments",
    "invalid_origin",
    "invalid_request",
    "missing_credentials",
    "invalid_credentials",
    "invalid_response",
    "response_too_large",
    "redirect_blocked",
    "unsupported_encoding",
    "timeout",
    "transport_error",
    "invalid_auth_response",
    "no_result",
    "invalid_result",
    "internal_error",
}


class BenchmarkError(Exception):
    """Only these fixed categories are allowed into evidence, never exception text."""

    def __init__(self, code):
        if code not in ERROR_CODES and not re.fullmatch(
            r"http_[1-5][0-9]{2}", str(code)
        ):
            code = "internal_error"
        self.code = code
        super().__init__(code)


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
    parser.add_argument("--samples", type=bounded_int(1, 100), default=20)
    parser.add_argument("--warmup", type=bounded_int(0, 10), default=2)
    parser.add_argument("--timeout-seconds", type=bounded_int(1, 10), default=3)
    parser.add_argument(
        "--body-cap-bytes",
        type=bounded_int(1024, 8 * 1024 * 1024),
        default=2 * 1024 * 1024,
    )
    parser.add_argument("--max-points", type=bounded_int(4, 1000), default=200)
    parser.add_argument("--table-limit", type=bounded_int(1, 100), default=50)
    parser.add_argument("--window-days", type=bounded_int(1, 3660), default=30)
    parser.add_argument("--discovery-runs", type=bounded_int(1, 20), default=10)
    parser.add_argument("--discovery-evaluations", type=bounded_int(1, 100), default=20)
    parser.add_argument("--candidate-probes", type=bounded_int(1, 10), default=5)
    parser.add_argument(
        "--result-kind", choices=("research", "final"), default="research"
    )
    parser.add_argument("--evaluation-id", type=UUID)
    parser.add_argument("--allow-result-access-records", action="store_true")
    parser.add_argument("--require-result", action="store_true")
    parser.add_argument("--declared-dataset-bars", type=bounded_int(1, 10**12))
    parser.add_argument("--declared-dataset-listings", type=bounded_int(1, 10**6))
    return parser.parse_args(argv)


def parse_origin(value):
    """No credentials, DNS (including localhost), proxy, path or remote endpoint."""
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or not parsed.hostname
        ):
            raise ValueError
        address = ipaddress.ip_address(parsed.hostname)
        if not address.is_loopback or (
            isinstance(address, ipaddress.IPv6Address) and address.scope_id
        ):
            raise ValueError
        port = 80 if parsed.port is None else parsed.port
        if not 1 <= port <= 65535:
            raise ValueError
        return str(address), port
    except (ValueError, TypeError):
        raise BenchmarkError("invalid_origin") from None


def identifier(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise BenchmarkError("invalid_response") from None


def count(value):
    return value if type(value) is int and 0 <= value <= 10**15 else None


def object_value(value):
    return value if isinstance(value, dict) else {}


def decode_json(body):
    def reject_constant(value):
        raise ValueError

    try:
        result = json.loads(body, parse_constant=reject_constant)
        if not isinstance(result, dict):
            raise TypeError
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BenchmarkError("invalid_response") from None


class HTTPTransport:
    """Direct HTTPConnection: no proxy, cookies, DNS or redirect implementation.

    A watchdog interrupts the connected socket throughout response headers and
    streamed bodies, retaining it when http.client detaches Connection: close.
    Deadlines are also checked around bounded parsing; this is not a real-time
    process-kill guarantee against scheduler or CPU stalls.
    """

    def __init__(
        self,
        origin,
        timeout,
        cap,
        connection_factory=http.client.HTTPConnection,
        clock=time.monotonic,
    ):
        self.host, self.port = parse_origin(origin)
        if not 1 <= timeout <= 10 or not 1024 <= cap <= 8 * 1024 * 1024:
            raise BenchmarkError("invalid_arguments")
        self.timeout, self.cap = timeout, cap
        self.connection_factory, self.clock = connection_factory, clock

    def request(self, method, path, *, token=None, body=None):
        parts = urlsplit(path)
        if (
            parts.scheme
            or parts.netloc
            or parts.fragment
            or any(ord(c) <= 32 for c in path)
            or (
                method == "GET"
                and (not GET_ROUTES.fullmatch(parts.path) or body is not None)
            )
            or (
                method == "POST"
                and (path != PREFIX + "/login/access-token" or token is not None)
            )
            or method not in {"GET", "POST"}
        ):
            raise BenchmarkError("invalid_request")
        if token is not None and (
            not isinstance(token, str)
            or len(token) > 16384
            or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token)
        ):
            raise BenchmarkError("invalid_auth_response")
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection = response = timer = connected_socket = None
        expired = threading.Event()
        timer_started = False
        deadline = self.clock() + self.timeout

        def remaining():
            value = deadline - self.clock()
            if expired.is_set() or value <= 0:
                raise BenchmarkError("timeout")
            return value

        def interrupt():
            expired.set()
            # HTTPConnection.getresponse() may clear connection.sock while its
            # HTTPResponse still owns a file object referencing this same socket.
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
            timeout = remaining()
            if connected_socket is not None:
                connected_socket.settimeout(timeout)
            connection.request(method, path, body=body, headers=headers)
            remaining()
            response = connection.getresponse()
            remaining()
            status = response.status
            if 300 <= status < 400:
                raise BenchmarkError("redirect_blocked")
            if status != 200:
                raise BenchmarkError(f"http_{status}")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise BenchmarkError("unsupported_encoding")
            length = response.getheader("Content-Length")
            if length is not None:
                if not length.isdecimal():
                    raise BenchmarkError("invalid_response")
                if int(length) > self.cap:
                    raise BenchmarkError("response_too_large")
            data = bytearray()
            while True:
                remaining()
                piece = response.read1(min(65536, self.cap + 1 - len(data)))
                remaining()
                data.extend(piece)
                if len(data) > self.cap:
                    raise BenchmarkError("response_too_large")
                if not piece:
                    break
            if length is not None and len(data) != int(length):
                raise BenchmarkError("invalid_response")
            result = decode_json(data)
            remaining()
            return result
        except BenchmarkError:
            raise
        except TimeoutError:
            raise BenchmarkError("timeout") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise BenchmarkError(
                "timeout" if expired.is_set() else "transport_error"
            ) from None
        finally:
            if timer is not None:
                timer.cancel()
                # Do not leave a watchdog operating on a later request. Every
                # connection/socket belongs to this request, even if detached.
                if timer_started:
                    timer.join(timeout=0.1)
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()


class Client:
    def __init__(self, transport):
        self.transport = transport
        self.token = None

    def login(self, environment):
        email, password = environment.get("E2E_EMAIL"), environment.get("E2E_PASSWORD")
        if not email or not password:
            raise BenchmarkError("missing_credentials")
        if any(not isinstance(v, str) or len(v) > 4096 for v in (email, password)):
            raise BenchmarkError("invalid_credentials")
        result = self.transport.request(
            "POST",
            PREFIX + "/login/access-token",
            body=urlencode({"username": email, "password": password}).encode(),
        )
        token = result.get("access_token")
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,16384}=*", token)
            or result.get("token_type") != "bearer"
        ):
            raise BenchmarkError("invalid_auth_response")
        self.token = token

    def get(self, path, **query):
        suffix = "?" + urlencode(query, doseq=True) if query else ""
        return self.transport.request("GET", PREFIX + path + suffix, token=self.token)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 6)


def summarize(values):
    return {
        "count": len(values),
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "max": round(max(values), 6) if values else None,
    }


def measure(operation, *, samples, warmup, clock=time.perf_counter):
    durations, errors, warmup_errors = [], Counter(), Counter()
    for _ in range(warmup):
        try:
            operation()
        except BenchmarkError as exc:
            warmup_errors[exc.code] += 1
    for _ in range(samples):
        start = clock()
        try:
            operation()
        except BenchmarkError as exc:
            errors[exc.code] += 1
        else:
            durations.append((clock() - start) * 1000)
    return {
        "attempts": samples,
        "successes": len(durations),
        "errors": sum(errors.values()),
        "error_categories": dict(sorted(errors.items())),
        "success_latency_ms": summarize(durations),
        "warmup": {
            "attempts": warmup,
            "errors": sum(warmup_errors.values()),
            "error_categories": dict(sorted(warmup_errors.items())),
            "excluded_from_latency": True,
        },
    }


def result_path(kind, evaluation_id):
    value = identifier(evaluation_id)
    return (
        f"/research-evaluations/{value}/results"
        if kind == "research"
        else f"/final-evaluations/evaluations/{value}/result"
    )


def page_data(value, limit):
    rows = value.get("data")
    if (
        not isinstance(rows, list)
        or len(rows) > limit
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise BenchmarkError("invalid_response")
    return rows


def validate_result(result, kind, expected_id, sections, row_limit, max_points):
    schema = (
        "tradegrad.result-query.v1"
        if kind == "research"
        else "tradegrad.final-result-query.v1"
    )
    if (
        result.get("schema_version") != schema
        or result.get("status") != "succeeded"
        or identifier(result.get("evaluation_id")) != identifier(expected_id)
    ):
        raise BenchmarkError("invalid_result")
    tables = object_value(result.get("tables"))
    if set(tables) != set(sections):
        raise BenchmarkError("invalid_result")
    for section in sections:
        table = object_value(tables[section])
        rows = table.get("rows")
        totals = [
            count(table.get(key))
            for key in ("total_rows", "filtered_rows", "returned_rows")
        ]
        bound = max_points if section in {"equity", "drawdown"} else row_limit
        if (
            not isinstance(rows, list)
            or any(x is None for x in totals)
            or not totals[0] >= totals[1] >= totals[2] == len(rows)
            or len(rows) > bound
        ):
            raise BenchmarkError("invalid_result")
    return result


def probe_result(client, args, evaluation_id):
    response = client.get(
        result_path(args.result_kind, evaluation_id),
        sections=["equity"],
        max_points=args.max_points,
        limit=args.table_limit,
    )
    validate_result(
        response,
        args.result_kind,
        evaluation_id,
        ["equity"],
        args.table_limit,
        args.max_points,
    )
    if not response["tables"]["equity"]["returned_rows"]:
        raise BenchmarkError("invalid_result")
    return response


def choose_result(client, args):
    diagnostics = {
        "runs_inspected": 0,
        "evaluations_inspected": 0,
        "candidate_probes": 0,
        "error_categories": {},
    }
    if args.evaluation_id:
        diagnostics["candidate_probes"] = 1
        return probe_result(client, args, args.evaluation_id), None, diagnostics
    collection = (
        "/research-runs" if args.result_kind == "research" else "/final-evaluations"
    )
    runs = page_data(
        client.get(collection, offset=0, limit=args.discovery_runs), args.discovery_runs
    )
    errors = Counter()
    for run in runs:
        diagnostics["runs_inspected"] += 1
        if run.get("status") != "succeeded":
            continue
        run_id = identifier(run.get("id"))
        evaluations = page_data(
            client.get(
                f"{collection}/{run_id}/evaluations",
                offset=0,
                limit=args.discovery_evaluations,
            ),
            args.discovery_evaluations,
        )
        for evaluation in evaluations:
            diagnostics["evaluations_inspected"] += 1
            if (
                evaluation.get("status") != "succeeded"
                or evaluation.get("sandbox_stopped") is not True
                or not evaluation.get("finished_at")
            ):
                continue
            diagnostics["candidate_probes"] += 1
            try:
                result = probe_result(client, args, evaluation.get("id"))
            except BenchmarkError as exc:
                errors[exc.code] += 1
            else:
                diagnostics["error_categories"] = dict(errors)
                return result, evaluation, diagnostics
            if diagnostics["candidate_probes"] >= args.candidate_probes:
                diagnostics["error_categories"] = dict(errors)
                return None, None, diagnostics
    diagnostics["error_categories"] = dict(errors)
    return None, None, diagnostics


def historical_evaluation_timing(evaluation):
    """DB registration-to-completion includes launch/publication, not engine time."""
    seconds = None
    if evaluation:
        try:
            start = datetime.fromisoformat(evaluation["created_at"])
            end = datetime.fromisoformat(evaluation["finished_at"])
            if start.tzinfo is not None and end.tzinfo is not None and end >= start:
                seconds = round((end - start).total_seconds(), 6)
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
    return {
        "status": "available" if seconds is not None else "unavailable",
        "registration_to_completion_seconds": seconds,
        "scope": "persisted_database_registration_to_completion_including_launch_and_publication",
        "independently_timed": False,
        "api_latency": False,
        "engine_only_latency": False,
    }


def host_information():
    def sysconf(name):
        try:
            value = os.sysconf(name)
            return value if value > 0 else None
        except (AttributeError, OSError, ValueError):
            return None

    pages, page_size = sysconf("SC_PHYS_PAGES"), sysconf("SC_PAGE_SIZE")
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    system, machine = platform.system(), platform.machine()
    memory_limit, cpu_quota = None, None
    try:
        value = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        memory_limit = count(int(value)) if value.isdecimal() else None
    except (OSError, ValueError):
        pass
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota.isdecimal() and period.isdecimal() and int(period) > 0:
            cpu_quota = round(int(quota) / int(period), 6)
    except (OSError, ValueError):
        pass
    return {
        "scope": "benchmark_client_not_server",
        "python": ".".join(map(str, sys.version_info[:3])),
        "platform": system if system in {"Linux", "Darwin", "Windows"} else "other",
        "architecture": machine
        if machine in {"x86_64", "AMD64", "aarch64", "arm64", "i386", "i686"}
        else "other",
        "logical_cpu_count": os.cpu_count(),
        "affinity_cpu_count": affinity,
        "physical_memory_bytes": pages * page_size if pages and page_size else None,
        "cgroup_v2_memory_limit_bytes": memory_limit,
        "cgroup_v2_cpu_quota_cores": cpu_quota,
    }


def workload_information(client, result, args):
    config = object_value(result.get("config"))
    source = object_value(config.get("source_config", config))
    dataset = object_value(source.get("dataset"))
    observed = {
        "dataset_bar_count": None,
        "dataset_listing_count": None,
        "dataset_calendar_days": None,
        "is_synthetic": result.get("is_synthetic")
        if type(result.get("is_synthetic")) is bool
        else None,
    }
    dataset_error = None
    try:
        details = client.get("/datasets/" + identifier(dataset.get("id")))
        manifest = object_value(details.get("manifest"))
        observed["dataset_bar_count"] = count(manifest.get("row_count"))
        listings = manifest.get("listings")
        mapping = manifest.get("evaluator_symbol_mapping")
        observed["dataset_listing_count"] = (
            len(listings)
            if isinstance(listings, list)
            else len(mapping)
            if isinstance(mapping, dict)
            else None
        )
        try:
            delta = (
                date.fromisoformat(details["end_date"])
                - date.fromisoformat(details["start_date"])
            ).days
            observed["dataset_calendar_days"] = count(delta)
        except (KeyError, ValueError, TypeError):
            pass
    except BenchmarkError as exc:
        dataset_error = exc.code
    return {
        "observed": observed,
        "dataset_metadata_error": dataset_error,
        "declared_not_verified": {
            "dataset_bars": args.declared_dataset_bars,
            "dataset_listings": args.declared_dataset_listings,
        },
        "representative_workload_acceptance": "operator_pending",
        "daily_frequency": "not_independently_verified",
    }


def run_benchmark(
    args, transport, environment, *, clock=time.perf_counter, host=host_information
):
    client = Client(transport)
    report = {
        "schema_version": "tradegrad.reference-performance.v1",
        "status": "complete",
        "host": host(),
        "scope": "sequential_authenticated_loopback_http_reference_not_production_capacity",
        "bounds": {
            "samples_per_endpoint": args.samples,
            "warmup_per_endpoint": args.warmup,
            "socket_timeout_seconds": args.timeout_seconds,
            "response_body_cap_bytes": args.body_cap_bytes,
            "http_concurrency": 1,
            "fresh_connection_per_request": True,
            "max_points": args.max_points,
            "table_limit": args.table_limit,
            "requested_window_calendar_days": args.window_days,
            "discovery_runs": args.discovery_runs,
            "discovery_evaluations_per_run": args.discovery_evaluations,
            "candidate_probes": args.candidate_probes,
        },
        "latency_method": "client_wall_clock_including_json_decode_and_schema_checks_successes_only_linear_interpolation_percentiles",
        "authentication_timed": False,
        "result_access_records": "explicitly_allowed"
        if args.allow_result_access_records
        else "not_allowed",
        "measurements": {},
        "result": {"status": "not_requested", "kind": args.result_kind},
        "historical_timings": {
            "evaluation": historical_evaluation_timing(None),
            "import": {
                "status": "unsupported",
                "reason": "import_created_updated_timestamps_do_not_isolate_import_execution",
            },
        },
        "unsupported_measurements": [
            "independently_timed_import",
            "independently_timed_single_evaluation",
            "concurrent_engine_capacity",
            "model_latency",
            "server_hardware_and_resource_peaks",
            "production_capacity_certification",
        ],
        "performance_threshold": None,
    }
    client.login(environment)

    def measure_endpoint(name, operation):
        report["measurements"][name] = measure(
            operation, samples=args.samples, warmup=args.warmup, clock=clock
        )

    def health(path, expected):
        result = client.get(path)
        if result.get("status") != expected or (
            expected == "ready" and result.get("database") is not True
        ):
            raise BenchmarkError("invalid_response")

    def metadata():
        if (
            client.get("/research-capabilities").get("schema_version")
            != "tradegrad.run.v1"
        ):
            raise BenchmarkError("invalid_response")

    measure_endpoint("health_live", lambda: health("/health/live", "alive"))
    measure_endpoint("health_ready", lambda: health("/health/ready", "ready"))
    measure_endpoint("metadata_research_capabilities", metadata)
    if args.allow_result_access_records:
        try:
            result, evaluation, diagnostics = choose_result(client, args)
            report["result"]["discovery"] = diagnostics
            if result is None:
                raise BenchmarkError("no_result")
            evaluation_id = identifier(result["evaluation_id"])
            window = object_value(result.get("window"))
            try:
                start, end = (
                    date.fromisoformat(window["start"]),
                    date.fromisoformat(window["end"]),
                )
                if start >= end:
                    raise ValueError
                start = max(start, end - timedelta(days=args.window_days))
            except (KeyError, TypeError, ValueError):
                raise BenchmarkError("invalid_result") from None
            report["workload"] = workload_information(client, result, args)
            report["historical_timings"]["evaluation"] = historical_evaluation_timing(
                evaluation
            )
            report["result"].update(
                status="selected",
                selection="explicit"
                if args.evaluation_id
                else "bounded_owner_scoped_discovery",
                window_calendar_days=(end - start).days,
            )

            def query(sections):
                response = client.get(
                    result_path(args.result_kind, evaluation_id),
                    sections=sections,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    max_points=args.max_points,
                    offset=0,
                    limit=args.table_limit,
                )
                validate_result(
                    response,
                    args.result_kind,
                    evaluation_id,
                    sections,
                    args.table_limit,
                    args.max_points,
                )
                return response

            for label, sections in (
                ("persisted_chart_window", ["equity", "drawdown"]),
                ("persisted_fills_page", ["fills"]),
            ):
                # One untimed observation records only aggregate table dimensions.
                observation = query(sections)
                report["result"][label] = {
                    section: {
                        key: observation["tables"][section][key]
                        for key in ("total_rows", "filtered_rows", "returned_rows")
                    }
                    for section in sections
                }
                measure_endpoint(label, lambda sections=sections: query(sections))
        except BenchmarkError as exc:
            report["result"].update(status="unavailable", error_category=exc.code)
    if any(
        item["errors"] or item["warmup"]["errors"]
        for item in report["measurements"].values()
    ):
        report["status"] = "incomplete"
    if args.require_result and report["result"]["status"] != "selected":
        report["status"] = "incomplete"
    # A permitted result query failure is visible even when it was not required.
    if report["result"]["status"] == "unavailable":
        report["status"] = "incomplete"
    return report


def main(argv=None, *, environment=None, transport_factory=HTTPTransport, output=None):
    output = output or sys.stdout
    try:
        args = parse_args(argv)
        transport = transport_factory(
            args.origin, args.timeout_seconds, args.body_cap_bytes
        )
        report = run_benchmark(
            args, transport, os.environ if environment is None else environment
        )
        code = 0 if report["status"] == "complete" else 1
    except BenchmarkError as exc:
        report, code = (
            {
                "schema_version": "tradegrad.reference-performance.v1",
                "status": "failed",
                "error_category": exc.code,
            },
            2,
        )
    except Exception:  # noqa: BLE001 - final CLI redaction boundary
        # Do not publish a traceback or free text from a server/transport/env.
        report, code = (
            {
                "schema_version": "tradegrad.reference-performance.v1",
                "status": "failed",
                "error_category": "internal_error",
            },
            2,
        )
    print(json.dumps(report, sort_keys=True, allow_nan=False), file=output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
