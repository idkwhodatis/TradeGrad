"""Bounded resource projection and workflow contracts, no Docker or PG substitute."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from script import ci_postgres_resources as resources

ROOT = Path(__file__).resolve().parents[2]
IDENTITY = "a" * 64 + "|/tradegrad-ci-postgres|postgres|running|false|0\n"
FILESYSTEM = "4096|524288|500000|500000\n"
POSTGRES = "83886080|10000000|268435456|83886080|60|1|1|1\n"


def observation(outputs=(IDENTITY, FILESYSTEM, POSTGRES)):
    calls = []
    values = iter(outputs)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(returncode=0, stdout=value)

    return resources.collect(run=run), calls


def test_projects_actual_numeric_capacity_and_durability_without_raw_text():
    report, calls = observation()
    assert resources.expected_configuration(report)
    assert report["filesystem_used_bytes"] == (524288 - 500000) * 4096
    assert report["wal_bytes"] == 80 * 1024**2
    assert report["database_bytes"] == 10000000
    assert len(calls) == 3
    assert all(call[1]["timeout"] == 15 for call in calls)
    assert all(call[1]["stderr"] == subprocess.DEVNULL for call in calls)
    assert all(call[0][2] == "a" * 64 for call in calls[1:])
    assert "--no-psqlrc" in calls[2][0]
    assert "--no-password" in calls[2][0]
    output = json.dumps(report)
    assert (
        "/var" not in output and "tradegrad_ci" not in output and "SELECT" not in output
    )


@pytest.mark.parametrize(
    "identity",
    [
        IDENTITY.replace("/tradegrad-ci-postgres", "/other"),
        IDENTITY.replace("|postgres|", "|unrelated|"),
        IDENTITY + "private diagnostic",
        "sensitive arbitrary output",
        "x" * 1025,
        subprocess.TimeoutExpired("private diagnostic", 15),
        OSError("private diagnostic"),
    ],
)
def test_identity_failure_never_probes_or_echoes_unrelated_container(identity):
    report, calls = observation((identity,))
    assert len(calls) == 1
    assert report["state"] == "unavailable"
    assert not resources.expected_configuration(report)
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize(
    "raw",
    [
        "1|2|3|4\n",
        "4096|10|20|5\n",
        "0|10|5|5\n",
        "1|18446744073709551616|0|0\n",
        "4096|524288|500000|500000\nprivate diagnostic",
        "private diagnostic",
    ],
)
def test_invalid_filesystem_is_null_but_independent_pg_probe_is_retained(raw):
    report, _ = observation((IDENTITY, raw, POSTGRES))
    assert report["filesystem_total_bytes"] is None
    assert report["wal_bytes"] == 80 * 1024**2
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize(
    "raw",
    [
        "83886080|10000000|268435456|83886080|60|1|1|2\n",
        "-1|2|3|4|5|1|1|1\n",
        "private database rows or error",
        subprocess.TimeoutExpired("private database rows", 15),
    ],
)
def test_failed_sql_probe_retains_filesystem_without_diagnostic_content(raw):
    report, _ = observation((IDENTITY, FILESYSTEM, raw))
    assert report["filesystem_total_bytes"] == 2 * 1024**3
    assert all(report[field] is None for field in resources.PG_FIELDS)
    assert not resources.expected_configuration(report)
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize("field", resources.PG_FIELDS[-3:])
def test_each_actual_durability_setting_is_required(field):
    report, _ = observation()
    report[field] = 0
    assert not resources.expected_configuration(report)


def test_exit_state_remains_observable_when_server_and_mount_are_gone():
    report, _ = observation(
        (
            IDENTITY.replace("running|false|0", "exited|true|137"),
            OSError("private"),
            OSError("private"),
        )
    )
    assert report["state"] == "exited"
    assert report["oom_killed"] is True and report["exit_code"] == 137
    assert report["filesystem_total_bytes"] is None
    assert not resources.expected_configuration(report)


def test_ci_only_cli_records_only_fixed_projected_fields(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    report, _ = observation()
    monkeypatch.setattr(resources, "collect", lambda: report)
    assert resources.main(["--phase", "provisioned", "--require-config"]) == 0
    body = json.loads(
        (tmp_path / "var/ci-evidence/postgres-resources-provisioned.json").read_text()
    )
    assert set(body) == set(report) | {"format", "phase"}
    assert json.loads(capsys.readouterr().out) == body
    report["fsync_enabled"] = 0
    assert resources.main(["--phase", "provisioned", "--require-config"]) == 1
    # Post-failure observation does not replace the actual failing test outcome.
    assert resources.main(["--phase", "before-removal"]) == 0
    monkeypatch.delenv("GITHUB_ACTIONS")
    with pytest.raises(SystemExit, match="only for an ephemeral"):
        resources.main(["--phase", "provisioned"])


def test_disposable_workflow_resource_limits_do_not_weaken_production_durability():
    workflow = (ROOT / ".github/workflows/v1-validation.yml").read_text()
    assert workflow.count("--tmpfs /var/lib/postgresql:rw,size=2g,nosuid") == 2
    assert (
        workflow.count(
            "-c max_wal_size=256MB -c min_wal_size=80MB -c checkpoint_timeout=60s"
        )
        == 2
    )
    assert workflow.count("--phase provisioned --require-config") == 2
    assert workflow.count("--phase before-removal") == 2
    assert "--phase after-integration" in workflow
    assert workflow.count("--publish 127.0.0.1:5432:5432") == 2
    # The image's initialization server is socket-only. Wait for the final TCP
    # listener before probing the actual configured database/settings.
    assert (
        workflow.count("pg_isready -h 127.0.0.1 -U tradegrad_ci -d tradegrad_ci") == 2
    )
    assert (
        workflow.count("--env POSTGRES_USER --env POSTGRES_PASSWORD --env POSTGRES_DB")
        == 2
    )
    for source in [
        workflow,
        *(path.read_text() for path in (ROOT / "infra").glob("compose*.yml")),
    ]:
        for setting in ("fsync", "full_page_writes", "synchronous_commit"):
            assert f"{setting}=off" not in source and f"{setting}: off" not in source
        assert "POSTGRES_HOST_AUTH_METHOD=trust" not in source
    # The resource tuning and diagnostic are CI-only, never application settings
    # or either deployed/full-Compose PostgreSQL service's startup command.
    for path in (ROOT / "infra").glob("compose*.yml"):
        source = path.read_text()
        assert "ci_postgres_resources" not in source
        assert "max_wal_size" not in source and "checkpoint_timeout" not in source
        assert "postgres:18.6" in source


@pytest.mark.parametrize("operation", ["mkdir", "write_text"])
def test_evidence_write_failure_never_emits_private_exception(
    tmp_path, monkeypatch, capsys, operation
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    report, _ = observation()
    monkeypatch.setattr(resources, "collect", lambda: report)

    def fail(*args, **kwargs):
        raise OSError("private-path-and-diagnostic")

    monkeypatch.setattr(Path, operation, fail)
    assert resources.main(["--phase", "provisioned", "--require-config"]) == 1
    captured = capsys.readouterr()
    assert captured.err == "" and "private" not in captured.out
    body = json.loads(captured.out)
    assert body["evidence_status"] == "write_failed"
    assert body["state"] == "running" and body["fsync_enabled"] == 1
