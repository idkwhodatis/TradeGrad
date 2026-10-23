"""Pure command/identity checks; no Docker, socket or PostgreSQL invocation."""

from types import SimpleNamespace

import pytest

from script.ci_pg_client import command, main


def environment():
    import secrets

    return {
        "GITHUB_ACTIONS": "true",
        "TRADEGRAD_CI_POSTGRES_ID": "a" * 64,
        "PGHOST": "127.0.0.1",
        "PGPORT": "5432",
        "PGDATABASE": "tradegrad_backup_test_" + "b" * 32,
        "PGUSER": "tradegrad_ci",
        "PGPASSWORD": secrets.token_hex(32),
    }


def test_credentials_are_only_passed_by_environment_name():
    env = environment()
    argv = command("pg_dump", ["--format=custom", "--no-owner"], env)
    assert argv[:3] == ["docker", "exec", "-i"]
    contains_credential = env["PGPASSWORD"] in argv
    assert not contains_credential
    assert "PGPASSWORD" in argv
    assert argv[-3:] == ["pg_dump", "--format=custom", "--no-owner"]


@pytest.mark.parametrize("tool", ["sh", "psql", "other"])
def test_only_two_backup_tools(tool):
    with pytest.raises(ValueError):
        command(tool, [], environment())


@pytest.mark.parametrize(
    "arguments",
    [
        ["--file=/host/dump"],
        ["-f/tmp/dump"],
        ["--create"],
        ["--clean"],
        ["postgresql://example"],
        ["--password"],
        ["x" * 257],
        ["x"] * 31,
    ],
)
def test_rejects_file_and_unbounded_or_credential_arguments(arguments):
    with pytest.raises(ValueError):
        command("pg_dump", arguments, environment())


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_ACTIONS", "false"),
        ("TRADEGRAD_CI_POSTGRES_ID", "postgres"),
        ("PGHOST", "remote"),
        ("PGPORT", "5433"),
        ("PGDATABASE", "tradegrad_ci"),
        ("PGUSER", "another"),
        ("PGPASSWORD", ""),
    ],
)
def test_rejects_unrelated_targets(key, value):
    env = environment()
    env[key] = value
    with pytest.raises(ValueError):
        command("pg_restore", [], env)


@pytest.mark.parametrize("tool", ["pg_dump", "pg_restore"])
def test_version_needs_recorded_container_but_no_database(tool):
    env = environment()
    for name in list(env):
        if name.startswith("PG"):
            del env[name]
    assert command(tool, ["--version"], env)[-2:] == [tool, "--version"]


@pytest.mark.parametrize(
    "identity",
    [
        b"/other postgres\n",
        b"/tradegrad-ci-postgres other\n",
        b"",
        b"bad\n",
    ],
)
def test_wrong_service_identity_never_executes(identity, capsys):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=identity)

    assert main(["pg_dump", "--version"], environment=environment(), run=run) == 1
    assert len(calls) == 1
    assert capsys.readouterr().err == "Disposable PostgreSQL client failed\n"


def test_valid_identity_streams_inherited_stdio_without_dump_capture():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=b"/tradegrad-ci-postgres postgres\n"
        )

    executions = []

    def execute(program, argv, env):
        executions.append((program, argv, env))
        return 0

    assert (
        main(
            ["pg_dump", "--format=custom"],
            environment=environment(),
            run=run,
            execute=execute,
        )
        == 0
    )
    assert len(calls) == 1 and len(executions) == 1
    assert executions[0][0] == "docker"
    assert calls[0][1]["timeout"] == 10


def test_restore_database_must_match_generated_environment_target():
    env = environment()
    args = [
        "--exit-on-error",
        "--single-transaction",
        "--no-password",
        "--dbname=" + env["PGDATABASE"],
    ]
    assert command("pg_restore", args, env)[-4:] == args
    with pytest.raises(ValueError):
        command("pg_restore", ["--dbname=unrelated"], env)


@pytest.mark.parametrize(
    "key,value",
    [("PGPASSFILE", "/other"), ("PGOPTIONS", "-c session_preload_libraries=other")],
)
def test_unexpected_client_configuration_rejects(key, value):
    env = environment()
    env[key] = value
    with pytest.raises(ValueError):
        command("pg_dump", ["--format=custom"], env)
