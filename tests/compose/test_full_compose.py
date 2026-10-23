"""Pure source/configuration checks; these are NOT Docker/Compose acceptance."""

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import yaml

from script import ci_compose_cleanup, compose_image_evidence
from script import compose_runtime as runtime
from script.compose_acceptance import Client, browser_environment

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "infra/compose.full.yml").read_text())
SERVICES = COMPOSE["services"]
APP_ROLES = {
    "api",
    "research-dispatcher",
    "research-worker",
    "evaluator-reconciler",
    "data-worker",
    "data-dispatcher",
    "migrate",
    "bootstrap",
}
CANDIDATES = {"native-evaluator", "portable-engine", "portable-policy"}


class ComposeBoundaryTests(unittest.TestCase):
    def test_full_roles_are_independent_and_not_the_legacy_database_file(self):
        self.assertEqual(
            set(SERVICES), APP_ROLES | CANDIDATES | {"frontend", "database"}
        )
        original = yaml.safe_load((ROOT / "infra/compose.yml").read_text())
        self.assertEqual(set(original["services"]), {"database"})
        self.assertEqual(SERVICES["bootstrap"]["profiles"], ["provision"])
        for name in CANDIDATES:
            self.assertEqual(SERVICES[name]["profiles"], ["images"])

    def test_all_running_services_use_explicit_bounded_operational_log_rotation(self):
        expected = {
            "driver": "json-file",
            "options": {
                "max-size": "${TRADEGRAD_SERVICE_LOG_MAX_SIZE:-10m}",
                "max-file": "${TRADEGRAD_SERVICE_LOG_MAX_FILES:-3}",
            },
        }
        for name in APP_ROLES | {"frontend", "database"}:
            self.assertEqual(SERVICES[name]["logging"], expected)
        self.assertEqual(
            runtime.validate_service_logging({}), {"max-size": "10m", "max-file": "3"}
        )
        self.assertEqual(
            runtime.validate_service_logging(
                {
                    "TRADEGRAD_SERVICE_LOG_MAX_SIZE": "64k",
                    "TRADEGRAD_SERVICE_LOG_MAX_FILES": "1",
                }
            ),
            {"max-size": "64k", "max-file": "1"},
        )
        for key, value in (
            ("TRADEGRAD_SERVICE_LOG_MAX_SIZE", "-1"),
            ("TRADEGRAD_SERVICE_LOG_MAX_SIZE", "0m"),
            ("TRADEGRAD_SERVICE_LOG_MAX_SIZE", "63k"),
            ("TRADEGRAD_SERVICE_LOG_MAX_SIZE", "101m"),
            ("TRADEGRAD_SERVICE_LOG_MAX_SIZE", "1g"),
            ("TRADEGRAD_SERVICE_LOG_MAX_FILES", "0"),
            ("TRADEGRAD_SERVICE_LOG_MAX_FILES", "21"),
            ("TRADEGRAD_SERVICE_LOG_MAX_FILES", "01"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                runtime.validate_service_logging({key: value})

    def test_only_trusted_supervisors_have_the_socket(self):
        owners = set()
        for name, service in SERVICES.items():
            for mount in service.get("volumes", []):
                if (
                    isinstance(mount, dict)
                    and mount["target"] == "/var/run/docker.sock"
                ):
                    owners.add(name)
                    self.assertFalse(mount["bind"]["create_host_path"])
        self.assertEqual(owners, runtime.SUPERVISORS)
        for name, service in SERVICES.items():
            self.assertNotIn("privileged", service)
            self.assertNotIn("pid", service)
            self.assertNotIn("devices", service)
            self.assertNotEqual(service.get("network_mode"), "host")

    def test_candidates_never_receive_config_network_socket_or_storage(self):
        for name in CANDIDATES:
            service = SERVICES[name]
            self.assertEqual(service["network_mode"], "none")
            self.assertTrue(service["read_only"])
            self.assertEqual(service["cap_drop"], ["ALL"])
            for field in (
                "volumes",
                "environment",
                "env_file",
                "secrets",
                "ports",
                "group_add",
            ):
                self.assertNotIn(field, service)
            digest = service["build"]["args"]["TRADEGRAD_ENVIRONMENT_DIGEST"]
            self.assertIn("ENVIRONMENT_DIGEST:?", digest)

    def test_durable_binds_have_identical_host_container_paths(self):
        for name in APP_ROLES:
            service = SERVICES[name]
            self.assertIn("TRADEGRAD_UID:?", service["user"])
            self.assertTrue(service["read_only"])
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertEqual(len(service["tmpfs"]), 1)
            self.assertIn("size=268435456", service["tmpfs"][0])
            for mount in service["volumes"]:
                if mount["target"] == "/var/run/docker.sock":
                    continue
                # Error text on required interpolation is non-semantic.
                import re

                normalize = lambda value: re.sub(
                    r"\$\{(\w+)\:?[^}]*\}", r"${\1}", value
                )
                self.assertEqual(normalize(mount["source"]), normalize(mount["target"]))
                self.assertFalse(mount["bind"]["create_host_path"])
        self.assertIn("postgres-data", COMPOSE["volumes"])

    def test_only_frontend_is_published_and_sse_is_unbuffered(self):
        published = {name for name, service in SERVICES.items() if service.get("ports")}
        self.assertEqual(published, {"frontend"})
        self.assertTrue(SERVICES["frontend"]["ports"][0].startswith("127.0.0.1:"))
        config = (ROOT / "infra/nginx.conf").read_text()
        for contract in (
            "proxy_buffering off",
            "proxy_cache off",
            "proxy_read_timeout 3600s",
            "access_log off",
            "try_files $uri $uri/ /index.html",
        ):
            self.assertIn(contract, config)

    def test_model_registry_only_reaches_api_and_research_without_keys(self):
        owners = set()
        for name, service in SERVICES.items():
            environment = service.get("environment", {})
            if "MODEL_PROFILES" in environment:
                owners.add(name)
                self.assertEqual(
                    environment["MODEL_PROFILES_ENABLED"],
                    "${MODEL_PROFILES_ENABLED:-false}",
                )
            self.assertNotIn("MODEL_SYNTHETIC_TEST_MODE", environment)
            self.assertNotIn("env_file", service)
            self.assertFalse(any("API_KEY" in key for key in environment))
        self.assertEqual(owners, {"api", "research-worker"})

    def test_migration_and_readiness_gate_normal_services(self):
        self.assertEqual(SERVICES["migrate"]["command"], ["migrate"])
        self.assertEqual(SERVICES["migrate"]["restart"], "no")
        for name in APP_ROLES - {"migrate", "bootstrap"}:
            self.assertEqual(
                SERVICES[name]["depends_on"]["migrate"]["condition"],
                "service_completed_successfully",
            )
            self.assertIn("healthcheck", SERVICES[name])
        self.assertEqual(
            SERVICES["frontend"]["depends_on"]["api"]["condition"], "service_healthy"
        )

    def test_image_targets_keep_installed_dependency_closures_separate(self):
        source = (ROOT / "infra/application.Dockerfile").read_text()
        for role, lock in (
            ("api", "backend"),
            ("data", "runtimes/data"),
            ("research", "runtimes/research"),
        ):
            part = source.split(f"FROM source AS {role}\n")[1].split("\nFROM ")[0]
            self.assertIn(f"--require-hashes -r {lock}/requirements.lock", part)
            self.assertIn(f"validate_runtime_split --runtime {role}", part)
            self.assertEqual("COPY --from=docker-cli" in part, role == "research")
        self.assertNotIn("COPY . .", source)
        for filename in ("application", "frontend"):
            lines = (
                (ROOT / f"infra/{filename}.Dockerfile.dockerignore")
                .read_text()
                .splitlines()
            )
            self.assertIn("**", lines)
            self.assertNotIn("!**", lines)
            self.assertFalse(any(line.startswith(("!var", "!.env")) for line in lines))

    def test_hosted_script_runs_exact_compose_real_browser_and_restart(self):
        source = (ROOT / "script/ci_compose_smoke.sh").read_text()
        for text in (
            "GITHUB_ACTIONS",
            "config --quiet",
            "--profile images build",
            "run --rm --no-deps migrate",
            "python -m script.browser_acceptance",
            "--record",
            "--verify",
            "script.ci_compose_cleanup",
        ):
            self.assertIn(text, source)
        self.assertNotIn("--privileged", source)
        self.assertNotIn("MODEL_SYNTHETIC_TEST_MODE", source)
        self.assertNotIn("docker.sock:/", source)


class RuntimePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = self.root / "storage"
        self.storage.mkdir(mode=0o700)
        self.environment = {
            "TRADEGRAD_STATE_ROOT": str(self.root),
            "STORAGE_ROOT": str(self.storage),
            "TRADEGRAD_UID": str(os.getuid()),
            "TRADEGRAD_GID": str(os.getgid()),
            # Generated per-test URI, never an actual database credential.
            "DATABASE_URL": "postgresql+psycopg://tradegrad:"
            + os.urandom(16).hex()
            + "@database:5432/tradegrad",
        }

    def test_host_and_container_identity_validate_without_daemon_or_network(self):
        with patch.dict(os.environ, self.environment, clear=True):
            runtime.validate_application()

    def test_root_and_mismatched_identity_are_rejected(self):
        with patch.dict(os.environ, self.environment, clear=True):
            with patch("os.getuid", return_value=0), self.assertRaises(ValueError):
                runtime.validate_identity()
            with (
                patch.dict(os.environ, {"TRADEGRAD_UID": "65531"}),
                self.assertRaises(ValueError),
            ):
                runtime.validate_identity()

    def test_symlink_parent_traversal_relative_and_mount_delimiters_fail(self):
        for path in (
            "relative",
            "/",
            str(self.root) + "/../x",
            str(self.root) + ",x",
            str(self.root) + "\nx",
            str(self.root) + "/",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                runtime.state_root({"TRADEGRAD_STATE_ROOT": path})
        link = self.root / "alias"
        link.symlink_to(self.storage, target_is_directory=True)
        with self.assertRaises(ValueError):
            runtime.state_root({"TRADEGRAD_STATE_ROOT": str(link)})

    def test_wrong_storage_path_or_permissions_fail_without_repair(self):
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.dict(os.environ, {"STORAGE_ROOT": "/different/storage"}),
                self.assertRaises(ValueError),
            ):
                runtime.validate_application()
            self.storage.chmod(0o755)
            with self.assertRaises(ValueError):
                runtime.validate_application()
            self.assertEqual(self.storage.stat().st_mode & 0o777, 0o755)

    def test_external_database_is_not_silently_used(self):
        with patch.dict(os.environ, self.environment, clear=True):
            for uri in (
                "postgresql://tradegrad@database/tradegrad",
                "sqlite:///db",
                self.environment["DATABASE_URL"].replace("@database", "@127.0.0.1"),
            ):
                with (
                    patch.dict(os.environ, {"DATABASE_URL": uri}),
                    self.assertRaises(ValueError),
                ):
                    runtime.validate_application()

    def test_unpinned_cli_or_stale_environment_fails(self):
        for image in (
            "docker:cli",
            "untrusted@sha256:" + "a" * 64,
            "docker@sha256:" + "a" * 64,
        ):
            with self.subTest(image=image), self.assertRaises(ValueError):
                runtime.validate_build({"DOCKER_CLI_IMAGE": image})

    def test_role_failure_does_not_emit_supplied_credentials(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sys.stderr") as error,
            self.assertRaises(SystemExit),
        ):
            runtime.main(["run", "api"])
        self.assertNotIn(
            "DATABASE_URL", "".join(str(call) for call in error.write.call_args_list)
        )

    def test_browser_environment_never_inherits_worker_or_database_credentials(self):
        result = browser_environment(
            {
                "PATH": "/usr/bin",
                "HOME": "/tmp",
                "CI": "true",
                "DATABASE_URL": os.urandom(16).hex(),
                "SECRET_KEY": os.urandom(16).hex(),
                "MODEL_PROFILES": "private registry",
                "PROVIDER_API_KEY": os.urandom(16).hex(),
                "E2E_PASSWORD": os.urandom(16).hex(),
            }
        )
        self.assertIn("E2E_PASSWORD", result)
        self.assertFalse(
            set(result)
            & {"DATABASE_URL", "SECRET_KEY", "MODEL_PROFILES", "PROVIDER_API_KEY"}
        )
        self.assertEqual(result["E2E_MODEL_PROFILES"], "0")

    def test_administrator_bootstrap_is_only_an_explicit_request(self):
        password = os.urandom(16).hex()
        env = {
            "BOOTSTRAP_EMAIL": "generated@example.com",
            "BOOTSTRAP_PASSWORD": password,
        }
        for requested in ("0", "1"):
            with (
                patch.dict(
                    os.environ, {**env, "BOOTSTRAP_SUPERUSER": requested}, clear=True
                ),
                patch.object(runtime, "validate_application"),
                patch("os.execvp") as execute,
            ):
                runtime.main(["bootstrap"])
            arguments = execute.call_args.args[1]
            self.assertEqual("--superuser" in arguments, requested == "1")
            self.assertNotIn(password, arguments)

    def test_public_image_evidence_drops_environment_and_unrelated_labels(self):
        secret = os.urandom(16).hex()
        raw = [
            {
                "Id": "sha256:" + "a" * 64,
                "RepoDigests": [],
                "Os": "linux",
                "Architecture": "amd64",
                "Config": {
                    "Env": ["PRIVATE=" + secret],
                    "Labels": {
                        "unrelated-private": secret,
                        "io.tradegrad.environment_digest": "b" * 64,
                    },
                },
            }
        ]
        with patch.object(
            compose_image_evidence, "docker", return_value=json.dumps(raw)
        ):
            evidence = compose_image_evidence.inspect("local/reviewed:fixture")
        self.assertNotIn(secret, json.dumps(evidence))
        self.assertEqual(evidence["environment_digest"], "b" * 64)

    def test_acceptance_rejects_nonloopback_before_http(self):
        with (
            patch.dict(os.environ, {"E2E_API_URL": "https://example.com"}),
            self.assertRaises(ValueError),
        ):
            Client()

    def test_hosted_cleanup_is_bound_to_project_and_exact_storage(self):
        suffix = os.urandom(8).hex()
        root = self.root / "var" / ("compose-" + suffix)
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_WORKSPACE": str(self.root),
            "COMPOSE_PROJECT_NAME": "tg-ci-" + suffix,
            "TRADEGRAD_STATE_ROOT": str(root),
        }
        for owned in (False, True):
            removed = set()

            def fake_command(*args, owned=owned, removed=removed, **kwargs):
                if args[:3] == ("ps", "-aq", "--filter"):
                    return (
                        "a" * 12
                        if args[-1].endswith("=evaluator-v1") and not removed
                        else ""
                    )
                if args[0] == "rm":
                    removed.add(args[-1])
                if args[0] == "inspect":
                    source = root / ("storage" if owned else "storage-other") / "input"
                    return json.dumps(
                        [
                            {
                                "Id": "a" * 64,
                                "Config": {
                                    "Labels": {"io.tradegrad.managed": "evaluator-v1"}
                                },
                                "Mounts": [{"Type": "bind", "Source": str(source)}],
                            }
                        ]
                    )
                return ""

            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    ci_compose_cleanup, "command", side_effect=fake_command
                ) as command,
                patch("builtins.print"),
            ):
                ci_compose_cleanup.main()
            removals = [call for call in command.call_args_list if call.args[0] == "rm"]
            self.assertEqual(len(removals), int(owned))

    def test_cleanup_rejects_unowned_project_before_docker(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(ci_compose_cleanup, "command") as command,
            self.assertRaises(SystemExit),
        ):
            ci_compose_cleanup.main()
        command.assert_not_called()

    def test_restart_verifies_actual_downloaded_bytes_with_no_network(self):
        content = os.urandom(37)
        with patch.dict(os.environ, {"E2E_API_URL": "http://127.0.0.1:8080"}):
            client = Client()
        artifact = {
            "id": "one",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        with (
            patch.object(client.opener, "open", return_value=io.BytesIO(content)),
            patch(
                "script.compose_acceptance.alarm_deadline", return_value=nullcontext()
            ),
        ):
            client.verify_artifact_bytes(artifact)
        with (
            patch.object(
                client.opener,
                "open",
                return_value=io.BytesIO(content + b"x"),
            ),
            patch(
                "script.compose_acceptance.alarm_deadline", return_value=nullcontext()
            ),
            self.assertRaises(ValueError),
        ):
            client.verify_artifact_bytes(artifact)
        with (
            patch.object(
                client.opener,
                "open",
                return_value=io.BytesIO(b"x" * len(content)),
            ),
            patch(
                "script.compose_acceptance.alarm_deadline", return_value=nullcontext()
            ),
            self.assertRaises(ValueError),
        ):
            client.verify_artifact_bytes(artifact)

    def test_durable_result_identity_is_bounded_metadata_only(self):
        with patch.dict(os.environ, {"E2E_API_URL": "http://127.0.0.1:8080"}):
            client = Client()
        artifact = {
            "id": "one",
            "name": "metrics.json",
            "sha256": "a" * 64,
            "size_bytes": 12,
            "secret": os.urandom(16).hex(),
        }
        with (
            patch.object(
                client,
                "request",
                side_effect=[{"status": "succeeded"}, {"data": [artifact], "count": 1}],
            ),
            patch.object(client, "verify_artifact_bytes"),
        ):
            value = client.artifact_identity("run")
        self.assertNotIn("secret", json.dumps(value))
        self.assertEqual(value["artifacts"][0]["sha256"], artifact["sha256"])


if __name__ == "__main__":
    unittest.main()


def test_hosted_compose_is_independent_after_baseline_and_keeps_bounded_evidence():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/v1-validation.yml").read_text()
    )
    job = workflow["jobs"]["compose"]
    assert job["needs"] == "baseline"
    assert workflow["jobs"]["application"]["needs"] == "baseline"
    assert "continue-on-error" not in job
    assert "always()" not in job["if"]
    assert job["timeout-minutes"] == 75
    assert workflow["permissions"] == {"contents": "read"}
    steps = job["steps"]
    # This job rebuilds its own stack; it consumes no application-job artifact
    # or output that could be missing or unsafe after an unrelated failure.
    assert all(
        not step.get("uses", "").startswith("actions/download-artifact@")
        for step in steps
    )
    assert "needs.application" not in json.dumps(job)
    assert all("continue-on-error" not in step for step in steps)
    commands = "\n".join(step.get("run", "") for step in steps)
    assert (
        "--require-hashes -r backend/requirements.lock -r tests/compose/requirements.lock"
        in commands
    )
    assert "python -m script.ci_compose_environment" in commands
    assert "python -m script.compose_image_evidence --resolve-bases" in commands
    assert "bash script/ci_compose_smoke.sh" in commands
    uploads = [
        step["with"]
        for step in steps
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    assert uploads[0]["path"].splitlines() == [
        "var/ci-evidence/*.xml",
        "var/ci-evidence/compose-images.json",
        "var/ci-evidence/v1-service-log-policy.json",
    ]
    assert "secrets." not in json.dumps(job)
    assert compose_image_evidence.BASES["DOCKER_CLI_IMAGE"] == "docker:27-cli"


def test_compose_yaml_parser_dependency_is_test_only_and_hash_locked():
    lock = (ROOT / "tests/compose/requirements.lock").read_text()
    assert "PyYAML==6.0.3 --hash=sha256:" in lock
    for location in ("backend", "runtimes/research", "runtimes/data"):
        assert "PyYAML" not in (ROOT / location / "requirements.lock").read_text()
