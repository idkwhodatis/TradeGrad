"""Hosted exact-Compose readiness and durable-result restart acceptance.

Real HTTP only. No mocked queue/results, account creation, model calls or Docker.
The existing Playwright suite creates the synthetic results this check verifies.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from script.compose_bounds import alarm_deadline, read_capped
from script.compose_browser_launcher import BROWSER_SECONDS, browser_environment

JSON_BYTES = 2 * 1024 * 1024
TOTAL_SECONDS = 180


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise HTTPError(request.full_url, code, "Redirect refused", headers, fp)


class Client:
    def __init__(self):
        self.origin = os.environ["E2E_API_URL"]
        uri = urlsplit(self.origin)
        if (
            uri.scheme != "http"
            or uri.hostname != "127.0.0.1"
            or uri.path not in {"", "/"}
            or uri.username is not None
            or uri.password is not None
            or uri.query
            or uri.fragment
        ):
            raise ValueError("Only the explicit loopback Compose endpoint is accepted")
        self.token = None
        self.deadline = time.monotonic() + TOTAL_SECONDS
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, data=None):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        with (
            alarm_deadline(min(self.deadline, time.monotonic() + 10)),
            self.opener.open(
                Request(self.origin + "/api/v1" + path, data=data, headers=headers),
                timeout=10,
            ) as response,
        ):
            return json.loads(read_capped(response, JSON_BYTES))

    def login(self):
        value = self.request(
            "/login/access-token",
            urlencode(
                {
                    "username": os.environ["E2E_EMAIL"],
                    "password": os.environ["E2E_PASSWORD"],
                }
            ).encode(),
        )
        self.token = value["access_token"]

    def ready(self):
        if self.request("/health/ready")["status"] != "ready":
            raise ValueError("Migrated API is not ready")
        self.login()
        status = self.request("/status")
        if status["status"] != "ready" or not all(
            status["workers"].get(role) == "healthy"
            for role in (
                "research",
                "dispatcher",
                "reconciler",
                "data",
                "data_dispatcher",
            )
        ):
            raise ValueError("Independent worker role heartbeats are not ready")

    def verify_artifact_bytes(self, artifact):
        expected_size = artifact["size_bytes"]
        if type(expected_size) is not int or not 0 <= expected_size <= 50_000_000:
            raise ValueError("Artifact exceeds acceptance byte budget")
        request = Request(
            self.origin + "/api/v1/artifacts/" + artifact["id"] + "/download",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        digest, size = hashlib.sha256(), 0
        with (
            alarm_deadline(min(self.deadline, time.monotonic() + 30)),
            self.opener.open(request, timeout=10) as response,
        ):
            while chunk := response.read(65536):
                size += len(chunk)
                if size > expected_size:
                    raise ValueError("Artifact size differs from immutable record")
                digest.update(chunk)
        if size != expected_size or digest.hexdigest() != artifact["sha256"]:
            raise ValueError("Artifact bytes differ from immutable record")

    def artifact_identity(self, run_id):
        run = self.request(f"/runs/{run_id}")
        if run["status"] != "succeeded":
            raise ValueError("Stored successful run is no longer readable")
        page = self.request(f"/runs/{run_id}/artifacts?limit=100")
        artifacts = page["data"]
        if page["count"] != len(artifacts):
            raise ValueError("Choose a bounded seed run for restart acceptance")
        if not artifacts:
            raise ValueError(
                "A real completed evaluation with durable artifacts is required"
            )
        if (
            len(artifacts) > 100
            or any(
                type(item["size_bytes"]) is not int
                or not 0 <= item["size_bytes"] <= 50_000_000
                for item in artifacts
            )
            or sum(item["size_bytes"] for item in artifacts) > 64 * 1024 * 1024
        ):
            raise ValueError("Combined restart artifact budget exceeded")
        for artifact in artifacts:
            self.verify_artifact_bytes(artifact)
        return {
            "run_id": run_id,
            "artifacts": sorted(
                (
                    {key: item[key] for key in ("id", "name", "sha256", "size_bytes")}
                    for item in artifacts
                ),
                key=lambda item: item["id"],
            ),
        }


def run_browser(node, root, source):
    """Outer deadline is failure, never substitute evidence for child containment."""
    for name in ("E2E_BASE_URL", "E2E_API_URL"):
        uri = urlsplit(source.get(name, ""))
        if (
            uri.scheme != "http"
            or uri.hostname != "127.0.0.1"
            or uri.path not in {"", "/"}
            or uri.username is not None
            or uri.password is not None
            or uri.query
            or uri.fragment
            or uri.port is None
        ):
            raise ValueError("Browser requires explicit loopback origins and ports")
    environment = browser_environment(source)
    environment.update(GITHUB_ACTIONS="true", TRADEGRAD_REQUIRE_COMPOSE_BROWSER="1")
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(root / "script/compose_browser_launcher.py"),
            node,
            str(root),
            str(os.getpid()),
        ],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    descriptor = None
    try:
        descriptor = os.pidfd_open(process.pid)
        result = process.wait(timeout=BROWSER_SECONDS + 55)
    except BaseException:  # noqa: BLE001 - uncertain launch/wait must request owned cleanup
        try:
            if descriptor is not None:
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                # The Popen child has not been reaped. It still owns this PID;
                # failure to establish a pidfd never permits successful acceptance.
                process.terminate()
            process.wait(timeout=30)
        except BaseException:  # noqa: BLE001 - preserve uncertainty, not raw child output
            raise RuntimeError("Owned browser cleanup remains unconfirmed") from None
        raise RuntimeError(
            "Browser exceeded its outer bound or launch became uncertain"
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if result != 0:
        raise RuntimeError("Browser workflow or owned-descendant cleanup failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--browser", action="store_true")
    group.add_argument("--record", type=Path)
    group.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.browser:
            if os.environ.get("GITHUB_ACTIONS") != "true":
                raise ValueError(
                    "Browser launcher is restricted to the hosted fixture job"
                )
            node = shutil.which("node")
            if not node:
                raise ValueError("The matched Node runtime is required")
            root = Path(__file__).resolve().parents[1]
            run_browser(node, root, os.environ)
            print("Hosted browser workflow and owned-descendant cleanup passed")
            return
        client = Client()
        deadline = time.monotonic() + 120
        while True:
            try:
                client.ready()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(5)
        if args.record:
            runs = client.request("/runs?limit=100")["data"]
            run = next(
                item
                for item in runs
                if item["status"] == "succeeded"
                and item.get("config", {}).get("schema_version") is None
            )
            value = client.artifact_identity(run["id"])
            fd = os.open(args.record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as destination:
                json.dump(value, destination, sort_keys=True)
        if args.verify:
            with args.verify.open("rb") as proof:
                value = json.loads(read_capped(proof, JSON_BYTES))
            if value != client.artifact_identity(value["run_id"]):
                raise ValueError("Cold restart changed immutable artifact identities")
        print(
            "Exact-Compose HTTP/worker readiness and requested durability checks passed"
        )
    except Exception:  # noqa: BLE001 - redact all private HTTP/configuration failures
        raise SystemExit(
            "Exact-Compose acceptance failed; no credentials or response bodies emitted"
        ) from None


if __name__ == "__main__":
    main()
