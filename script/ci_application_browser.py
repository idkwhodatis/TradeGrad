"""Hosted-only owner of the ordinary process-stack V1 acceptance boundary.

Fixture: FULL existing Chromium suite, reference/workload performance, operations.
Live: explicitly scoped @provider-flow only, with no fixture or model fallback.
Every launched service, Vite, CLI and detached Chromium descendant belongs to a
single bounded Linux subreaper. No inherited DB/model settings reach Node.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
from xml.etree import ElementTree as ET

from script.ci_browser_diagnostics import failure_sites
from script.ci_process_owner import ProcessOwner
from script.ci_test_environment import SECRET_FIELDS
from script.compose_bounds import alarm_deadline, read_capped
from script.compose_browser_launcher import (
    BROWSER_SECONDS,
    browser_command,
    browser_environment,
)
from script.research_browser_acceptance import require_research_cases
from script.summarize_ci_junit import summarize

API = "http://127.0.0.1:8000"
FRONTEND = "http://127.0.0.1:5173"
TOTAL_SECONDS = 3600
DOCKER_SECONDS = 90
MAX_REPORT = 20_000_000
SERVICE_KEYS = (
    "DATABASE_URL",
    "SECRET_KEY",
    "STORAGE_ROOT",
    "TRADEGRAD_RUNNER_STATE_DIR",
    "EVALUATOR_IMAGE",
    "PORTABLE_ENGINE_IMAGE",
    "PORTABLE_POLICY_IMAGE",
    "PORTABLE_ENVIRONMENT_DIGEST",
)
MANAGED = ("evaluator-v1", "portable-evaluator-v1")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise HTTPError(request.full_url, code, "Redirect refused", headers, fp)


def acceptance_stage(source):
    stage = source.get("TRADEGRAD_APPLICATION_STAGE", "completion")
    if stage not in {"m5", "m6", "completion"}:
        raise ValueError("Unknown exact-stage acceptance profile")
    return stage


def diagnostic_target(source):
    value = source.get("TRADEGRAD_BROWSER_DIAGNOSTIC", "")
    if value not in {"", "native-final"}:
        raise ValueError("Unknown browser diagnostic profile")
    if value and (
        source.get("DATA_PROVIDER_MODE") != "fixture"
        or acceptance_stage(source) != "m5"
    ):
        raise ValueError(
            "Native final diagnostic is fixture-only and not stage acceptance"
        )
    return value


def validate_source(source, root):
    if (
        source.get("GITHUB_ACTIONS") != "true"
        or source.get("TRADEGRAD_REQUIRE_APPLICATION_BROWSER") != "1"
    ):
        raise ValueError("Explicit disposable hosted application gate required")
    stage = acceptance_stage(source)
    diagnostic_target(source)
    mode = source.get("DATA_PROVIDER_MODE")
    if mode not in {"fixture", "live"} or source.get("E2E_LIVE_PROVIDERS") != (
        "1" if mode == "live" else "0"
    ):
        raise ValueError("Provider evidence modes must agree explicitly")
    # Match the existing job-local generator, not an arbitrary production DSN.
    if any(
        not re.fullmatch(r"[a-f0-9]{64}", source.get(key, "")) for key in SECRET_FIELDS
    ):
        raise ValueError("Generated disposable credentials are required")
    expected = (
        "postgresql+psycopg://tradegrad_ci:"
        + source["POSTGRES_PASSWORD"]
        + "@127.0.0.1:5432/tradegrad_ci"
    )
    if (
        source.get("DATABASE_URL") != expected
        or source.get("TEST_DATABASE_URL") != expected
    ):
        raise ValueError("Only the generated disposable PostgreSQL target is accepted")
    for key, expected_path in (
        ("GITHUB_WORKSPACE", root),
        ("STORAGE_ROOT", root / "var/storage"),
        ("TRADEGRAD_RUNNER_STATE_DIR", root / "var/runner"),
    ):
        path = Path(source.get(key, ""))
        if path != expected_path or any(
            part.is_symlink() for part in (path, *path.parents)
        ):
            raise ValueError("Disposable hosted path identity mismatch")
    if any(
        (root / "frontend" / name).exists()
        for name in (".env", ".env.local", ".env.development", ".env.development.local")
    ):
        raise ValueError("Vite may not load ambient environment files")
    if any(
        not source.get(key)
        for key in ("PATH", "HOME", "E2E_EMAIL", "E2E_OTHER_EMAIL")
        + (("E2E_ADMIN_EMAIL",) if stage == "completion" else ())
    ):
        raise ValueError("Disposable hosted runtime and accounts are required")
    return mode


def base_environment(source):
    return {
        "PATH": source["PATH"],
        "HOME": source["HOME"],
        "LANG": "C.UTF-8",
        "CI": "true",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }


def service_environment(source, mode):
    result = base_environment(source)
    result.update({key: source[key] for key in SERVICE_KEYS if key in source})
    result.update(
        DATA_PROVIDER_MODE=mode,
        MODEL_PROFILES_ENABLED="false",
        MODEL_PROFILES="[]",
        MODEL_SYNTHETIC_TEST_MODE="false",
        WORKER_CONCURRENCY="1",
        DATA_WORKER_CONCURRENCY="1",
        HEARTBEAT_SECONDS="1",
        LEASE_SECONDS="20",
        TRADEGRAD_DOCKER_HOST="unix:///var/run/docker.sock",
    )
    return result


def node_environment(source, mode):
    result = browser_environment(source)
    result.update(
        CI="true",
        E2E_API_URL=API,
        E2E_BASE_URL=FRONTEND,
        E2E_LIVE_PROVIDERS="1" if mode == "live" else "0",
        E2E_MODEL_PROFILES="0",
        TRADEGRAD_REQUIRE_SETTINGS_E2E=(
            "1" if acceptance_stage(source) == "completion" else "0"
        ),
    )
    return result


def http_environment(source, mode):
    result = base_environment(source)
    result.update({key: source[key] for key in ("E2E_EMAIL", "E2E_PASSWORD")})
    result.update(
        E2E_API_URL=API,
        GITHUB_ACTIONS="true",
        DATA_PROVIDER_MODE=mode,
        TRADEGRAD_REQUIRE_DOCKER="1",
        TRADEGRAD_REQUIRE_OPERATIONS_INTEGRATION="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    return result


def commands(root, node):
    api, research, data = (
        sys.executable,
        root / ".venv-research/bin/python",
        root / ".venv-data/bin/python",
    )
    return [
        [
            api,
            "-m",
            "uvicorn",
            "backend.app.asgi:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        [api, "-m", "workers.research", "dispatcher"],
        [str(research), "-m", "workers.research", "worker"],
        [api, "-m", "workers.research", "reconciler"],
        [str(data), "-m", "workers.data", "dispatcher"],
        [str(data), "-m", "workers.data", "worker"],
        [
            node,
            str(root / "frontend/node_modules/vite/bin/vite.js"),
            "--host",
            "127.0.0.1",
            "--port",
            "5173",
            "--strictPort",
        ],
    ]


def wait_ready(owner):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    deadline = min(owner.deadline, time.monotonic() + 120)
    while time.monotonic() < deadline:
        owner.check()
        try:
            for url in (API + "/api/v1/health/ready", FRONTEND):
                with (
                    alarm_deadline(min(deadline, time.monotonic() + 3)),
                    opener.open(url, timeout=2) as response,
                ):
                    if response.status != 200:
                        raise ValueError("Readiness response was not successful")
                    body = read_capped(response, 131072)
                    if (
                        url.startswith(API)
                        and json.loads(body).get("status") != "ready"
                    ):
                        raise ValueError("API migration is not ready")
            owner.check()
            return
        except (OSError, ValueError, TimeoutError):
            time.sleep(0.25)
    raise TimeoutError("Explicit API and owned Vite did not become ready")


def export_junit(
    source, destination, *, live=False, diagnostic_root=None, require_research=False
):
    """Raw private reports never enter uploaded paths. Missing/empty is failure."""
    if (
        source.is_symlink()
        or not source.is_file()
        or source.stat().st_size > MAX_REPORT
    ):
        raise ValueError("Required bounded JUnit evidence missing")
    content = source.read_bytes()
    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise ValueError("Unsupported JUnit declarations")
    report = ET.fromstring(content)
    cases = list(report.iter("testcase"))
    passed = [
        case
        for case in cases
        if not any(
            case.find(tag) is not None for tag in ("skipped", "failure", "error")
        )
    ]
    failed = any(
        case.find(tag) is not None for case in cases for tag in ("failure", "error")
    )
    # Even failure evidence is useful, but its free text is never public.
    summarize(source, destination)
    if diagnostic_root is not None:
        print(
            "BROWSER_SOURCE_DIAGNOSTICS_V1:"
            + json.dumps(failure_sites(source, diagnostic_root), sort_keys=True)
        )
    if (
        not passed
        or failed
        or next(report.iter("error"), None) is not None
        or next(report.iter("failure"), None) is not None
    ):
        raise ValueError("Actual passing test cases are required")
    if require_research:
        require_research_cases(cases)
    if live and (
        len(passed) < 3
        or len(passed) != len(cases)
        or any("@provider-flow" not in case.get("name", "") for case in passed)
    ):
        raise ValueError("Live evidence must execute all three scoped provider flows")


def benchmark_evidence(content, destination, schema, status):
    value = json.loads(content)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != schema
        or value.get("status") != status
    ):
        raise ValueError("Bounded benchmark did not report successful execution")
    # These two existing benchmark programs emit only reviewed outcome/count/
    # timing projections, not response bodies or arbitrary diagnostic strings.
    destination.write_text(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")


def exercise(owner, root, source, private, node, mode):
    services = service_environment(source, mode)
    program = commands(root, node)
    for command in program[:-1]:
        owner.check()
        owner.spawn(command, cwd=root, env=services, service=True)
    # Vite is its own explicitly tracked root, with no login, DB or model data.
    vite = base_environment(source)
    vite.update(API_PROXY_TARGET=API, VITE_DEV_HOST="127.0.0.1")
    owner.spawn(program[-1], cwd=root / "frontend", env=vite, service=True)
    wait_ready(owner)
    environment = node_environment(source, mode)
    report = private / "browser.xml"
    environment["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] = str(report)
    command = browser_command(node, root) + [
        "--reporter=junit",
        "--output",
        str(private / "browser-output"),
    ]
    if diagnostic_target(source):
        # Fixed repository file, never an arbitrary user-supplied selector.
        command[3] = "tests/e2e/final-test-workflow.spec.ts"
    if mode == "live":
        command += ["--grep", "@provider-flow"]
    try:
        owner.run(
            command,
            cwd=root / "frontend",
            env=environment,
            seconds=BROWSER_SECONDS + 15,
        )
    finally:
        export_junit(
            report,
            root / "frontend/test-results/junit.xml",
            live=mode == "live",
            diagnostic_root=root,
            require_research=mode == "fixture"
            and acceptance_stage(source) == "completion",
        )
    if mode == "live" or acceptance_stage(source) == "m5":
        return
    environment = http_environment(source, mode)
    evidence = root / "var/ci-evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    result = owner.run(
        [
            sys.executable,
            "-m",
            "script.benchmark_v1",
            "--origin",
            API,
            "--allow-result-access-records",
            "--require-result",
            "--samples",
            "20",
            "--warmup",
            "2",
            "--timeout-seconds",
            "3",
            "--max-points",
            "200",
            "--table-limit",
            "50",
            "--window-days",
            "30",
        ],
        cwd=root,
        env=environment,
        seconds=600,
        maximum=1_048_576,
    )
    benchmark_evidence(
        result,
        evidence / "m6-reference-performance.json",
        "tradegrad.reference-performance.v1",
        "complete",
    )
    # A real second owned worker proves attempt overlap, not merely queued work.
    owner.spawn(program[2], cwd=root, env=services, service=True)
    result = owner.run(
        [
            sys.executable,
            "-m",
            "script.benchmark_v1_workloads",
            "--origin",
            API,
            "--allow-fixture-workload-mutations",
            "--timeout-seconds",
            "5",
            "--total-timeout-seconds",
            "300",
        ],
        cwd=root,
        env=environment,
        seconds=330,
        maximum=1_048_576,
    )
    benchmark_evidence(
        result,
        evidence / "m6-workload-performance.json",
        "tradegrad.fixture-workloads.v1",
        "succeeded",
    )
    if acceptance_stage(source) == "m6":
        return
    result = owner.run(
        [
            sys.executable,
            "-m",
            "script.benchmark_v1_daily_reference",
            "--origin",
            API,
            "--allow-representative-workload-mutations",
            "--timeout-seconds",
            "5",
            "--total-timeout-seconds",
            "600",
        ],
        cwd=root,
        env=environment,
        seconds=630,
        maximum=1_048_576,
    )
    benchmark_evidence(
        result,
        evidence / "v1-daily-reference-performance.json",
        "tradegrad.daily-reference-performance.v1",
        "succeeded",
    )
    report = private / "operations.xml"
    try:
        owner.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/system/test_operations_pipeline.py",
                "-q",
                "--junitxml=" + str(report),
            ],
            cwd=root,
            env=environment,
            seconds=75,
        )
    finally:
        export_junit(report, root / "v1-operations-application-junit.xml")


def cleanup_containers(owner, root, source, private):
    """Only exact full IDs with this job's storage bind + managed label qualify."""
    deadline = time.monotonic() + DOCKER_SECONDS
    config = private / "docker-config"
    config.mkdir(mode=0o700)
    (config / "config.json").write_text('{"auths":{}}')
    prefix = [
        "docker",
        "--config",
        str(config),
        "--host",
        "unix:///var/run/docker.sock",
    ]
    environment = base_environment(source)

    def command(*arguments):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Container cleanup exceeded its total bound")
        return owner.run(
            [*prefix, *arguments],
            cwd=root,
            env=environment,
            seconds=min(10, remaining),
            maximum=262144,
            cleanup=True,
        )

    def inventory():
        owned = []
        total = 0
        for profile in MANAGED:
            ids = (
                command(
                    "ps",
                    "-aq",
                    "--no-trunc",
                    "--filter",
                    "label=io.tradegrad.managed=" + profile,
                )
                .decode()
                .split()
            )
            total += len(ids)
            if (
                total > 256
                or len(set(ids)) != len(ids)
                or any(not re.fullmatch(r"[a-f0-9]{64}", identity) for identity in ids)
            ):
                raise ValueError("Uncertain bounded container identity")
            for identity in ids:
                rows = json.loads(command("inspect", identity))
                if (
                    not isinstance(rows, list)
                    or len(rows) != 1
                    or rows[0].get("Id") != identity
                    or rows[0]
                    .get("Config", {})
                    .get("Labels", {})
                    .get("io.tradegrad.managed")
                    != profile
                ):
                    raise ValueError("Exact managed container identity changed")
                mounts = rows[0].get("Mounts")
                if not isinstance(mounts, list) or len(mounts) > 16:
                    raise ValueError("Uncertain container mount ownership")
                for mount in mounts:
                    path = Path(mount.get("Source", ""))
                    if (
                        mount.get("Type") == "bind"
                        and path.is_absolute()
                        and ".." not in path.parts
                        and path.is_relative_to(root / "var/storage")
                    ):
                        owned.append(identity)
                        break
        return owned

    for identity in inventory():
        command("rm", "--force", identity)
    if inventory():
        raise RuntimeError("Owned container absence is not confirmed")


def run(source, root):
    mode = validate_source(source, root)
    node = shutil.which("node", path=source["PATH"])
    if not node:
        raise ValueError("Installed matched Node is required")
    owner = ProcessOwner(time.monotonic() + TOTAL_SECONDS)
    owner.contain()
    passed = False
    with tempfile.TemporaryDirectory(prefix="tradegrad-application-") as directory:
        private = Path(directory)
        try:
            exercise(owner, root, source, private, node, mode)
            owner.check()
            passed = True
        except BaseException:  # noqa: BLE001 - private failures still require owned cleanup
            passed = False
        finally:
            cleanup_confirmed = False
            try:
                # Workers must be confirmed stopped BEFORE inventory/removal:
                # otherwise they could still create new evaluator containers.
                if not owner.cleanup():
                    passed = False
                else:
                    cleanup_containers(owner, root, source, private)
                    cleanup_confirmed = True
            except BaseException:  # noqa: BLE001 - private failures still require owned cleanup
                passed = False
            finally:
                try:
                    if not owner.cleanup():
                        passed = False
                        cleanup_confirmed = False
                except BaseException:  # noqa: BLE001 - private failures still require owned cleanup
                    passed = False
                    cleanup_confirmed = False
            print(
                "OWNED_BROWSER_CLEANUP_V1:"
                + ("confirmed" if cleanup_confirmed else "unconfirmed")
            )
    return 0 if passed and not owner.interrupted else 1


def main():
    # New files, including private diagnostics, are owner-only on the runner.
    os.umask(0o077)
    try:
        result = run(os.environ, Path(__file__).resolve().parents[1])
    except BaseException:  # noqa: BLE001 - private failures still require owned cleanup
        result = 1
    if result:
        print(
            "Hosted application acceptance failed or owned cleanup is unconfirmed",
            file=sys.stderr,
        )
    else:
        mode = (
            "live @provider-flow only (NOT full fixture acceptance)"
            if os.environ["DATA_PROVIDER_MODE"] == "live"
            else "full fixture Chromium (" + acceptance_stage(os.environ) + " stage)"
        )
        if diagnostic_target(os.environ):
            mode = "native-final Chromium DIAGNOSTIC ONLY (NOT full stage acceptance)"
        print(
            (
                "Hosted diagnostic passed: "
                if diagnostic_target(os.environ)
                else "Hosted acceptance passed: "
            )
            + mode
            + "; owned process/container absence confirmed"
        )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
