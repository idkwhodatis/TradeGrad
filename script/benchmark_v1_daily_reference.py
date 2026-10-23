"""Finite synthetic six-year reference through production HTTP and native workers.

Hosted-only mutations; offline tests supply fake HTTP. No service, engine, SDK,
model or subprocess is launched by this driver. Historical smoke is unchanged.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import io
import itertools
import json
import os
import re
import socket
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from packages.tradegrad_adapter.config import (
    BudgetConfig,
    MarketConfig,
    RunConfig,
    VersionRef,
    WarmupConfig,
    WindowSet,
    evaluation_windows,
)
from packages.tradegrad_adapter.environment import environment_digest
from script.benchmark_v1_workloads import (
    CLEANUP_RESERVE_SECONDS,
    MAX_REQUESTS,
    PREFIX,
    SEED_SHA,
    TERMINAL_IMPORT,
    TERMINAL_RUN,
    UUID_PATTERN,
    BenchmarkError,
    SafeParser,
    bounded_int,
    hardware,
    identifier,
    parse_origin,
    summary,
    timestamp,
    token_value,
)
from script.benchmark_v1_workloads import (
    guard as smoke_guard,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE_ID = "cn-one-listing-six-year-v1"
PROFILE_PATH = "tests/performance/fixtures/cn-daily-six-year-v1.json"
FIXTURE_PATH = "tests/performance/fixtures/cn-daily-six-year-v1.csv"
FIXTURE_SHA = "bae7a1e660c4db02b60fd731754de3a1b66964cee70a05850c021503f54980f3"
PROFILE_SHA = "04109420fa962d25550335f477327221d1b3b9c22c5a557610a893c4c2e4b1c5"
SESSION_SHA = "4a361e8bd0651456511a0c919c8c63e6ae5c656258928fb021f46d636178b512"
ROWS, CSV_BYTES = 1456, 80119
START, END = "2019-01-01", "2025-01-01"
EXPECTED_WINDOWS = tuple(
    {"split": "train", "start": f"{year}-01-01", "end": f"{year + 1}-01-01"}
    for year in range(2020, 2024)
) + ({"split": "validation", "start": "2024-01-01", "end": "2025-01-01"},)
CHART_START, CHART_END = "2024-01-01", "2024-04-01"
MAX_POINTS, SAMPLES, WARMUP = 200, 20, 2
SCHEMA = "tradegrad.daily-reference-performance.v1"
GET_ROUTES = re.compile(
    rf"{PREFIX}/(?:data/providers|research-capabilities|strategy-versions"
    rf"|data/imports/{UUID_PATTERN}|datasets/{UUID_PATTERN}(?:/coverage)?"
    rf"|research-runs/{UUID_PATTERN}(?:/(?:evaluations|model-calls))?"
    rf"|research-evaluations/{UUID_PATTERN}/results)\Z"
)
POST_ROUTES = re.compile(
    rf"{PREFIX}/(?:login/access-token|data/uploads|data/universes|research-runs"
    rf"|research-runs/preflight|(?:data/imports|research-runs)/{UUID_PATTERN}/cancel)\Z"
)


def parse_args(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--allow-representative-workload-mutations", action="store_true"
    )
    parser.add_argument("--timeout-seconds", type=bounded_int(1, 10), default=5)
    parser.add_argument(
        "--total-timeout-seconds", type=bounded_int(60, 600), default=600
    )
    parser.add_argument(
        "--body-cap-bytes", type=bounded_int(131072, 2097152), default=1048576
    )
    parser.add_argument("--poll-milliseconds", type=bounded_int(100, 1000), default=500)
    return parser.parse_args(argv)


def guard(args, env):
    # Reuse the existing hosted/fixture/Docker/credentials guard without mutating args.
    values = vars(args) | {
        "allow_fixture_workload_mutations": args.allow_representative_workload_mutations
    }
    smoke_guard(argparse.Namespace(**values), env)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise BenchmarkError("invalid_execution_evidence")
    return value


def json_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def local_file(relative, cap):
    path = ROOT / relative
    if path.is_symlink() or not path.is_file() or path.stat().st_size > cap:
        raise BenchmarkError("fixture_integrity")
    content = path.read_bytes()
    if len(content) > cap:
        raise BenchmarkError("fixture_integrity")
    return content


def load_fixture():
    content = local_file(FIXTURE_PATH, CSV_BYTES)
    profile = local_file(PROFILE_PATH, 4096)
    if digest(content) != FIXTURE_SHA or digest(profile) != PROFILE_SHA:
        raise BenchmarkError("fixture_integrity")
    rows = list(csv.DictReader(io.StringIO(content.decode())))
    if len(rows) != ROWS or len(content) != CSV_BYTES:
        raise BenchmarkError("fixture_integrity")
    if digest(local_file("seed_strategies/cn_time.py", 65536)) != SEED_SHA:
        raise BenchmarkError("fixture_integrity")
    return content


def code_identity(env):
    """Bounded, deterministic content hash; never run git or emit filenames/data."""
    paths = set()
    for directory in (
        "backend/app",
        "backend/alembic",
        "packages",
        "workers",
        "src",
        "seed_strategies",
        "script",
        "runtimes",
    ):
        for path in (ROOT / directory).rglob("*"):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and (
                    path.suffix in {".py", ".lock", ".in"} or path.name == "Dockerfile"
                )
            ):
                paths.add(path.relative_to(ROOT).as_posix())
                if len(paths) > 512:
                    raise BenchmarkError("fixture_integrity")
    paths.update(
        {".dockerignore", "backend/requirements.lock", PROFILE_PATH, FIXTURE_PATH}
    )
    entries, size = [], 0
    for relative in sorted(paths):
        content = local_file(relative, 2097152)
        size += len(content)
        if size > 16777216:
            raise BenchmarkError("fixture_integrity")
        entries.append([relative, digest(content)])
    revision = env.get("GITHUB_SHA", "")
    return {
        "source_tree_sha256": digest(json_bytes(entries)),
        "source_file_count": len(entries),
        "source_bytes": size,
        "source_hash_definition": "sorted_relative_path_and_sha256_pairs.v1",
        "ci_commit": revision if re.fullmatch(r"[0-9a-f]{40}", revision) else None,
        "ci_commit_is_label_not_tree_verification": True,
        "profile_sha256": PROFILE_SHA,
        "uploaded_csv_sha256": FIXTURE_SHA,
        "seed_code_sha256": SEED_SHA,
    }


def host_context():
    result = hardware()
    try:
        result["affinity_cpus"] = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        result["affinity_cpus"] = None
    # These are client cgroup observations, not assertions about worker containers.
    for key, path in (
        ("client_cgroup_memory_limit_bytes", "/sys/fs/cgroup/memory.max"),
    ):
        try:
            value = Path(path).read_text()[:100].strip()
            result[key] = (
                int(value) if value.isdecimal() and 0 < int(value) <= 10**16 else None
            )
        except (OSError, ValueError):
            result[key] = None
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text()[:100].split()
        result["client_cgroup_cpu_quota_cores"] = (
            round(int(quota) / int(period), 3)
            if quota.isdecimal()
            and period.isdecimal()
            and 0 < int(period) <= 10**9
            and 0 < int(quota) <= 10**12
            else None
        )
    except (OSError, ValueError):
        result["client_cgroup_cpu_quota_cores"] = None
    result["worker_container_limits_verified"] = False
    return result


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


def upload_body(content):
    mapping = {
        "name": "Six-year daily reference v1, SYNTHETIC, NOT market evidence",
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
            "note": "Deterministic synthetic engineering reference, not market prices",
        },
    }
    boundary = "tradegrad-" + uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="mapping"\r\n\r\n'.encode()
        + json_bytes(mapping)
        + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="cn-daily-six-year-v1.csv"\r\nContent-Type: text/csv\r\n\r\n'.encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, "multipart/form-data; boundary=" + boundary


def config_for(dataset, universe, seed, environment):
    # Production immutable contract supplies all defaults and canonical hashing.
    # No returned arbitrary model, profile, execution budget or source is replayed.
    return RunConfig(
        execution_mode="optimization",
        task="cn_time",
        profile="corrected_explicit_config",
        dataset=VersionRef(
            id=identifier(dataset["id"]), sha256=sha(dataset["content_sha256"])
        ),
        universe=VersionRef(
            id=identifier(universe["id"]), sha256=sha(universe["content_sha256"])
        ),
        seed_strategy=VersionRef(id=identifier(seed), sha256=SEED_SHA),
        windows=WindowSet(
            train={"start": "2020-01-01", "end": "2024-01-01"},
            validation={"start": "2024-01-01", "end": END},
        ),
        market_profile=MarketConfig.for_market("CN"),
        warmup=WarmupConfig(sessions=20),
        optimizer={"max_iterations": 0},
        budgets=BudgetConfig(
            max_model_calls=0, max_tokens=0, max_runtime_seconds=240, max_repairs=0
        ),
        environment_digest=sha(environment),
    )


def interval(start, end, bound):
    value = timestamp(start), timestamp(end)
    duration = (value[1] - value[0]).total_seconds()
    if not 0 < duration <= bound:
        raise BenchmarkError("invalid_execution_evidence")
    return value


def duration_ms(value):
    return round((value[1] - value[0]).total_seconds() * 1000, 3)


def overlap_ms(first, second):
    return round(
        max(0, (min(first[1], second[1]) - max(first[0], second[0])).total_seconds())
        * 1000,
        3,
    )


class Driver:
    def __init__(self, args, transport, env, clock, sleep):
        self.args, self.transport, self.env = args, transport, env
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.deadline = self.started + args.total_timeout_seconds
        self.work_deadline = self.deadline - CLEANUP_RESERVE_SECONDS
        self.token, self.config = None, None
        self.owned, self.timings = {}, defaultdict(list)
        self.unknown_mutation, self.requests = False, 0
        self.report = {
            "schema_version": SCHEMA,
            "profile_id": PROFILE_ID,
            "status": "failed",
            "scope": "isolated_ci_synthetic_native_configured_seed",
            "capacity_claim": False,
            "user_agreed_production_profile": False,
            "live_data": False,
            "model_calls": None,
            "performance_threshold": None,
            "hardware": host_context(),
            "workload": {
                "listings": 1,
                "daily_bars": ROWS,
                "calendar_years": 6,
                "uploaded_csv_bytes": CSV_BYTES,
                "import_samples": 1,
                "standalone_configured_runs": 1,
                "concurrent_runs": 2,
                "physical_evaluations_per_run": 5,
                "maximum_physical_evaluations": 15,
                "maximum_created_imports": 1,
                "maximum_created_universes": 1,
                "maximum_created_runs": 3,
                "maximum_created_strategies": 0,
                "maximum_created_experiments": 0,
                "model_call_budget": 0,
                "engine_seconds_per_run_cap": 240,
            },
            "bounds": {
                "total_seconds": args.total_timeout_seconds,
                "request_seconds": args.timeout_seconds,
                "body_bytes": args.body_cap_bytes,
                "requests": MAX_REQUESTS,
                "poll_milliseconds": args.poll_milliseconds,
                "cleanup_reserve_seconds": CLEANUP_RESERVE_SECONDS,
                "metadata_samples": SAMPLES,
                "chart_samples": SAMPLES,
                "warmup_samples_each": WARMUP,
                "chart_max_points_per_series": MAX_POINTS,
                "chart_window_days": 91,
            },
            "created": {"imports": 0, "universes": 0, "runs": 0},
        }

    def call(self, method, path, label, *, cleanup=False, **kwargs):
        deadline = self.deadline if cleanup else self.work_deadline
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise BenchmarkError("timeout")
        # Six requests for cancelling and confirming three runs plus at most
        # eighteen child-evaluation cleanup reads; never spend the cleanup reserve.
        if self.requests >= MAX_REQUESTS - (0 if cleanup else 32):
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
        cap = {"imports": 1, "universes": 1, "runs": 3}[kind]
        if self.report["created"][kind] >= cap:
            raise BenchmarkError("request_budget")
        # Never retry an uncertain POST. Even synchronous universe writes track
        # unknown outcomes rather than discovering or deleting unrelated records.
        self.unknown_mutation = True
        value = self.call("POST", path, label, key=str(uuid4()), **kwargs)
        owned_id = identifier(value.get("id"))
        if owned_id in self.owned:
            raise BenchmarkError("invalid_response")
        self.owned[owned_id] = {"kind": kind, "terminal": kind == "universes"}
        self.report["created"][kind] += 1
        self.unknown_mutation = False
        return value

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
            or not 1 <= len(providers) <= 16
            or not all(
                isinstance(p, dict) and p.get("runtime_mode") == "fixture"
                for p in providers
            )
            or capabilities.get("live_model_calls") is not False
            or capabilities.get("model_providers") != ["fixture"]
            or capabilities.get("models") != ["deterministic-fixture-v1"]
        ):
            raise BenchmarkError("fixture_mode_required")
        self.environment = sha(capabilities.get("environment_digest"))
        if self.environment != environment_digest(ROOT):
            raise BenchmarkError("invalid_execution_evidence")
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

    def verify_dataset(self, dataset, dataset_id):
        manifest = dataset.get("manifest", {})
        source = manifest.get("upload_source", {})
        if (
            dataset.get("id") != dataset_id
            or dataset.get("is_synthetic") is not True
            or dataset.get("start_date") != START
            or dataset.get("end_date") != END
            or manifest.get("row_count") != ROWS
            or len(manifest.get("listings", [])) != 1
            or source.get("sha256") != FIXTURE_SHA
            or source.get("size_bytes") != CSV_BYTES
            or manifest.get("artifact_files", {}).get("source.csv") != FIXTURE_SHA
            or manifest.get("evaluation_compatibility", {}).get("compatible")
            is not True
        ):
            raise BenchmarkError("fixture_integrity")
        sha(dataset.get("content_sha256"))
        return dataset

    def import_fixture(self, content):
        body, content_type = upload_body(content)
        started = self.clock()
        imported = self.create(
            "imports",
            "/data/uploads",
            "upload_submission",
            body=body,
            content_type=content_type,
        )["id"]
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
            "samples": 1,
            "completion_wall_ms": round((self.clock() - started) * 1000, 3),
            "measurement": "upload_start_to_ready_observation_including_queue_and_poll",
        }
        dataset_id = identifier(detail.get("dataset_version_id"))
        dataset = self.verify_dataset(
            self.call("GET", f"/datasets/{dataset_id}", "dataset_verification"),
            dataset_id,
        )
        self.report["identity"]["dataset_snapshot_sha256"] = dataset["content_sha256"]
        return dataset

    def publish_universe(self, dataset):
        coverage = self.call(
            "GET", f"/datasets/{dataset['id']}/coverage", "coverage_verification"
        )
        members = coverage.get("universe", {}).get("members")
        rows = coverage.get("coverage")
        listing_id = identifier(dataset["manifest"]["listings"][0].get("listing_id"))
        if (
            coverage.get("dataset_version_id") != dataset["id"]
            or not isinstance(members, list)
            or len(members) != 1
            or members[0].get("listing_id") != listing_id
            or not isinstance(rows, list)
            or len(rows) != 1
            or rows[0].get("status") != "complete"
            or rows[0].get("calendar_version") != "4.13.2"
            or rows[0].get("listing_id") != listing_id
            or len(rows[0].get("expected_sessions", [])) != ROWS
            or rows[0].get("observed_sessions") != rows[0].get("expected_sessions")
            or digest(("\n".join(rows[0]["expected_sessions"]) + "\n").encode())
            != SESSION_SHA
            or any(
                rows[0].get(key) != []
                for key in (
                    "missing_sessions",
                    "non_session_dates",
                    "provisional_sessions",
                )
            )
        ):
            raise BenchmarkError("fixture_integrity")
        # Construct the declared fixed universe, never replay arbitrary source fields.
        body = {
            "name": "Six-year synthetic daily reference v1",
            "state": "user_fixed",
            "as_of": datetime.now(UTC).isoformat(),
            "members": [{"listing_id": listing_id}],
            "evidence": [],
        }
        value = self.create(
            "universes",
            "/data/universes",
            "universe_submission",
            body=json_bytes(body),
            content_type="application/json",
        )
        if value.get("state") != "user_fixed" or [
            m.get("listing_id") for m in value.get("members", [])
        ] != [listing_id]:
            raise BenchmarkError("fixture_integrity")
        sha(value.get("content_sha256"))
        self.report["identity"]["universe_sha256"] = value["content_sha256"]
        return value

    def configure(self, dataset, universe, seed):
        self.config = config_for(dataset, universe, seed, self.environment)
        if evaluation_windows(self.config) != EXPECTED_WINDOWS:
            raise BenchmarkError("fixture_integrity")
        self.body = self.config.canonical_bytes()
        prepared = self.call(
            "POST",
            "/research-runs/preflight",
            "research_preflight",
            body=self.body,
            content_type="application/json",
        )
        if (
            prepared.get("valid") is not True
            or prepared.get("is_synthetic") is not True
            or prepared.get("holdout_execution") is not False
            or prepared.get("config_sha256") != self.config.sha256
            or prepared.get("evaluation_windows") != list(EXPECTED_WINDOWS)
        ):
            raise BenchmarkError("invalid_execution_evidence")
        self.report["identity"].update(
            config_sha256=self.config.sha256, environment_sha256=self.environment
        )

    def page(self, run_id, section, label, *, cleanup=False):
        value = self.call(
            "GET",
            f"/research-runs/{run_id}/{section}?offset=0&limit=16",
            label,
            cleanup=cleanup,
        )
        rows = value.get("data")
        if (
            not isinstance(rows, list)
            or len(rows) > 16
            or value.get("count") != len(rows)
        ):
            raise BenchmarkError("invalid_execution_evidence")
        return rows

    def verify_execution(self, detail):
        result, attempts, config = (
            detail.get("result") or {},
            detail.get("attempts", []),
            detail.get("config"),
        )
        try:
            exact_config = (
                RunConfig.model_validate(config).canonical_bytes() == self.body
            )
        except (ValueError, TypeError):
            exact_config = False
        if (
            not exact_config
            or detail.get("execution_mode") != "optimization"
            or detail.get("generation") != 1
            or detail.get("dataset_version_id") != self.config.dataset.id
            or detail.get("strategy_version_id") != self.config.seed_strategy.id
            or result.get("execution_mode") != "optimization"
            or result.get("is_synthetic") is not True
            or result.get("evaluation_mode") != "native_akquant"
            or result.get("result_trust") != "native_strategy_and_engine_share_process"
            or result.get("holdout_executed") is not False
            or not isinstance(attempts, list)
            or len(attempts) != 1
        ):
            raise BenchmarkError("invalid_execution_evidence")
        image = result.get("image_digest")
        if not isinstance(image, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", image
        ):
            raise BenchmarkError("invalid_execution_evidence")
        if detail.get("research_image_digest") != image:
            raise BenchmarkError("invalid_execution_evidence")
        attempt = attempts[0]
        attempt_id = identifier(attempt.get("id"))
        if (
            attempt.get("status") != "succeeded"
            or attempt.get("sandbox_stopped") is not True
            or attempt.get("exit_code") != 0
            or attempt.get("generation") != 1
        ):
            raise BenchmarkError("invalid_execution_evidence")
        outer = interval(
            attempt.get("started_at"),
            attempt.get("finished_at"),
            self.args.total_timeout_seconds,
        )
        run_id = identifier(detail.get("id"))
        evaluations = self.page(run_id, "evaluations", "evaluation_verification")
        if len(evaluations) != 5:
            raise BenchmarkError("invalid_execution_evidence")
        by_window, seen = {}, set()
        for item in evaluations:
            eid = identifier(item.get("id"))
            window = item.get("window")
            key = json_bytes(window)
            if (
                eid in seen
                or key in by_window
                or window not in EXPECTED_WINDOWS
                or item.get("run_id") != run_id
                or item.get("attempt_id") != attempt_id
                or item.get("generation") != 1
                or item.get("status") != "succeeded"
                or item.get("code_sha256") != SEED_SHA
                or item.get("split") != window["split"]
                or item.get("sandbox_stopped") is not True
                or item.get("image_digest") != image
                or item.get("window_sha256") != digest(json_bytes(window))
            ):
                raise BenchmarkError("invalid_execution_evidence")
            sha(item.get("payload_sha256"))
            child = interval(item.get("created_at"), item.get("finished_at"), 240)
            if child[0] < outer[0] or child[1] > outer[1]:
                raise BenchmarkError("invalid_execution_evidence")
            by_window[key] = (item, child)
            seen.add(eid)
        models = self.page(run_id, "model-calls", "model_call_verification")
        if models:
            raise BenchmarkError("invalid_execution_evidence")
        ordered = [by_window[json_bytes(window)] for window in EXPECTED_WINDOWS]
        for first, second in itertools.pairwise(ordered):
            if first[1][1] > second[1][0]:
                raise BenchmarkError("invalid_execution_evidence")
        if self.report["identity"].get("evaluator_image_digest", image) != image:
            raise BenchmarkError("invalid_execution_evidence")
        self.report["identity"]["evaluator_image_digest"] = image
        return {"run_id": run_id, "attempt": outer, "evaluations": ordered}

    def evaluate(self, size):
        batch_started = self.clock()
        pending, completed = {}, []
        for _ in range(size):
            started = self.clock()
            run_id = self.create(
                "runs",
                "/research-runs",
                "run_submission",
                body=self.body,
                content_type="application/json",
            )["id"]
            pending[run_id] = started
        while pending:
            for run_id, started in list(pending.items()):
                detail = self.call("GET", f"/research-runs/{run_id}", "run_poll")
                if identifier(detail.get("id")) != run_id:
                    raise BenchmarkError("invalid_response")
                if detail.get("status") not in TERMINAL_RUN:
                    continue
                if detail.get("status") != "succeeded":
                    raise BenchmarkError("run_failed")
                evidence = self.verify_execution(detail)
                self.owned[run_id]["terminal"] = True
                evidence["wall_ms"] = round((self.clock() - started) * 1000, 3)
                completed.append(evidence)
                del pending[run_id]
            if pending:
                self.pause()
        report = {
            "configured_runs": size,
            "physical_engine_evaluations": size * 5,
            "completion_wall_ms": [row["wall_ms"] for row in completed],
            "batch_wall_ms": round((self.clock() - batch_started) * 1000, 3),
            "measurement": "configured_run_submission_to_verified_terminal_including_queue_five_evaluations_and_poll",
            "persisted_attempt_duration_ms": [
                duration_ms(row["attempt"]) for row in completed
            ],
            "persisted_physical_evaluation_lifecycle_ms": [
                [duration_ms(item[1]) for item in row["evaluations"]]
                for row in completed
            ],
            "physical_evaluation_measurement": "newly_created_server_evaluation_registration_to_persisted_completion_including_staging_docker_and_publication_not_cpu_time",
            "physical_evaluation_year_order": [2020, 2021, 2022, 2023, 2024],
            "physical_evaluation_split_order": [
                "train",
                "train",
                "train",
                "train",
                "validation",
            ],
            "native_docker_successes": size * 5,
            "sandbox_stopped": True,
            "same_exact_config_dataset_strategy": True,
            "verified_model_calls": 0,
        }
        if size == 2:
            first, second = completed
            attempt_overlap = overlap_ms(first["attempt"], second["attempt"])
            engine_overlaps = [
                overlap_ms(a[1], b[1])
                for a in first["evaluations"]
                for b in second["evaluations"]
            ]
            report.update(
                attempt_overlap_ms=attempt_overlap,
                physical_evaluation_overlap_ms=round(sum(engine_overlaps), 3),
                physical_evaluation_overlapping_pairs=sum(
                    value > 0 for value in engine_overlaps
                ),
                overlap_scope="persisted_actual_attempt_and_native_docker_evaluation_lifecycles_not_simultaneous_cpu_instructions",
            )
            self.report["concurrent_runs"] = report
            if attempt_overlap <= 0 or not any(value > 0 for value in engine_overlaps):
                raise BenchmarkError("concurrency_not_observed")
        else:
            self.report["standalone_configured_run"] = report
            chosen, timing = completed[0]["evaluations"][-1]
            self.report["single_evaluation"] = {
                "samples": 1,
                "scored_calendar_year": 2024,
                "persisted_lifecycle_ms": duration_ms(timing),
                "measurement": report["physical_evaluation_measurement"],
                "model_calls": 0,
                "split": "validation",
                "warmup_sessions": 20,
                "evaluation_payload_sha256": chosen["payload_sha256"],
                "selection": "2024_physical_evaluation_from_new_standalone_configured_run",
            }
        return completed

    def verify_chart(self, value, selected):
        evaluation = selected["evaluations"][-1][0]
        if (
            value.get("schema_version") != "tradegrad.result-query.v1"
            or value.get("evaluation_id") != evaluation["id"]
            or value.get("run_id") != selected["run_id"]
            or value.get("code_sha256") != SEED_SHA
            or value.get("config_sha256") != self.config.sha256
            or value.get("window") != EXPECTED_WINDOWS[-1]
            or value.get("split") != "validation"
            or value.get("status") != "succeeded"
            or value.get("is_synthetic") is not True
            or value.get("result_trust") != "native_strategy_and_engine_share_process"
            or value.get("start") != CHART_START
            or value.get("end") != CHART_END
        ):
            raise BenchmarkError("invalid_execution_evidence")
        try:
            if (
                RunConfig.model_validate(value.get("config")).canonical_bytes()
                != self.body
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise BenchmarkError("invalid_execution_evidence") from None
        artifact = sha(value.get("artifact_sha256"))
        if self.report["identity"].get("chart_artifact_sha256", artifact) != artifact:
            raise BenchmarkError("invalid_execution_evidence")
        self.report["identity"]["chart_artifact_sha256"] = artifact
        tables = value.get("tables")
        if not isinstance(tables, dict) or set(tables) != {"equity", "drawdown"}:
            raise BenchmarkError("invalid_execution_evidence")
        counts = {}
        for name, table in tables.items():
            fields = [
                table.get(key)
                for key in ("total_rows", "filtered_rows", "returned_rows")
            ]
            if (
                not all(type(n) is int for n in fields)
                or not 0 < fields[2] <= MAX_POINTS
                or not fields[2] <= fields[1] <= fields[0] <= ROWS
                or not isinstance(table.get("rows"), list)
                or len(table["rows"]) != fields[2]
                or table.get("sampling") not in {"none", "min_max"}
            ):
                raise BenchmarkError("invalid_execution_evidence")
            counts[name] = {
                "total_rows": fields[0],
                "filtered_rows": fields[1],
                "returned_rows": fields[2],
            }
        return counts

    def measure_queries(self, dataset, selected):
        evaluation = selected["evaluations"][-1][0]
        query = (
            urlencode(
                {
                    "start": CHART_START,
                    "end": CHART_END,
                    "max_points": MAX_POINTS,
                    "offset": 0,
                    "limit": 50,
                }
            )
            + "&sections=equity&sections=drawdown"
        )
        chart_path = f"/research-evaluations/{evaluation['id']}/results?{query}"
        expected_counts = None
        for index in range(WARMUP + SAMPLES):
            suffix = "warmup" if index < WARMUP else "sample"
            value = self.call("GET", f"/datasets/{dataset['id']}", "metadata_" + suffix)
            self.verify_dataset(value, dataset["id"])
            if value.get("content_sha256") != dataset["content_sha256"]:
                raise BenchmarkError("fixture_integrity")
            value = self.call("GET", chart_path, "chart_" + suffix)
            counts = self.verify_chart(value, selected)
            if expected_counts is not None and counts != expected_counts:
                raise BenchmarkError("invalid_execution_evidence")
            expected_counts = counts
        self.report["metadata_api"] = summary(self.timings["metadata_sample"]) | {
            "measurement": "sequential_authenticated_immutable_dataset_metadata_http_no_model_time",
            "warmup_excluded": WARMUP,
        }
        self.report["windowed_chart"] = summary(self.timings["chart_sample"]) | {
            "measurement": "persisted_hash_verified_result_query_http_including_access_ledger_no_engine_rerun",
            "warmup_excluded": WARMUP,
            "window_days": 91,
            "tables": expected_counts,
            "same_exact_new_dataset_and_standalone_2024_evaluation": True,
            "stable_persisted_artifact_hash_across_reads": True,
        }

    def cleanup(self):
        pending = [
            (key, value) for key, value in self.owned.items() if not value["terminal"]
        ]
        requested = confirmed = 0
        for owned_id, record in pending:
            path = (
                "/data/imports/" if record["kind"] == "imports" else "/research-runs/"
            ) + owned_id
            try:
                value = self.call(
                    "POST", path + "/cancel", "owned_cleanup", cleanup=True
                )
                if value.get("id") == owned_id:
                    requested += 1
            except Exception:  # noqa: BLE001, S110 - private exception projection forbidden
                pass
        for owned_id, record in pending:
            path = (
                "/data/imports/" if record["kind"] == "imports" else "/research-runs/"
            ) + owned_id
            try:
                value = self.call("GET", path, "owned_cleanup", cleanup=True)
                terminal = (
                    TERMINAL_IMPORT if record["kind"] == "imports" else TERMINAL_RUN
                )
                stopped = True
                if record["kind"] == "runs":
                    attempts = value.get("attempts")
                    children = self.page(
                        owned_id, "evaluations", "owned_cleanup", cleanup=True
                    )
                    stopped = (
                        isinstance(attempts, list)
                        and all(a.get("sandbox_stopped") is True for a in attempts)
                        and all(
                            e.get("sandbox_stopped") is True
                            and e.get("finished_at") is not None
                            for e in children
                        )
                    )
                if (
                    value.get("id") == owned_id
                    and value.get("status") in terminal
                    and stopped
                ):
                    confirmed += 1
            except Exception:  # noqa: BLE001, S110 - never emit private responses
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
            content = load_fixture()
            self.report["identity"] = code_identity(self.env)
            seed = self.preflight()
            dataset = self.import_fixture(content)
            self.configure(dataset, self.publish_universe(dataset), seed)
            single = self.evaluate(1)[0]
            self.measure_queries(dataset, single)
            self.evaluate(2)
            self.report["model_calls"] = 0
            self.report["status"] = "succeeded"
        except BenchmarkError as error:
            self.report["error_code"] = error.code
        except Exception:  # noqa: BLE001 - fixed category, no private exceptions
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
    except BenchmarkError as error:
        result = {
            "schema_version": SCHEMA,
            "status": "failed",
            "error_code": error.code,
        }
    except Exception:  # noqa: BLE001 - fixed category, no private exceptions
        result = {
            "schema_version": SCHEMA,
            "status": "failed",
            "error_code": "internal_error",
        }
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
