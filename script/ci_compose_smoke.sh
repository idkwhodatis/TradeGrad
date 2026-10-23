#!/usr/bin/env bash
# HOSTED-ONLY: real Compose/PG/queue/Docker/browser. Never run in restricted local QA.
set -euo pipefail
[ "${GITHUB_ACTIONS:-}" = true ] || { echo 'Hosted disposable job required' >&2; exit 1; }
: "${COMPOSE_PROJECT_NAME:?Run python -m script.ci_compose_environment in the preceding workflow step}"
: "${DOCKER_CLI_IMAGE:?Resolve the official docker CLI repository digest first}"
: "${TRADEGRAD_STATE_ROOT:?}"
: "${TRADEGRAD_PYTHON_IMAGE:?Resolve official app/frontend base digests in a preceding step}"
: "${TRADEGRAD_NODE_IMAGE:?}"
: "${TRADEGRAD_NGINX_IMAGE:?}"
compose=(docker --host "unix://${TRADEGRAD_DOCKER_SOCKET:?}" compose -f infra/compose.full.yml)
cleanup() {
  outcome=$?
  trap - EXIT
  python -m script.ci_compose_cleanup || outcome=1
  exit "$outcome"
}
trap cleanup EXIT
python -m script.compose_runtime prepare
# Never emit `compose config` with resolved credentials; quiet validation only.
"${compose[@]}" config --quiet
"${compose[@]}" --profile images build native-evaluator portable-engine portable-policy
"${compose[@]}" build api data-worker research-worker frontend
python -m script.compose_image_evidence
"${compose[@]}" up -d --wait --wait-timeout 120 database
"${compose[@]}" run --rm --no-deps migrate
BOOTSTRAP_EMAIL="$E2E_EMAIL" BOOTSTRAP_PASSWORD="$E2E_PASSWORD" BOOTSTRAP_SYNTHETIC=1 BOOTSTRAP_E2E=1 BOOTSTRAP_SUPERUSER=0 \
  "${compose[@]}" run --rm --no-deps bootstrap >/dev/null
BOOTSTRAP_EMAIL="$E2E_OTHER_EMAIL" BOOTSTRAP_PASSWORD="$E2E_OTHER_PASSWORD" BOOTSTRAP_SYNTHETIC=1 BOOTSTRAP_E2E=0 BOOTSTRAP_SUPERUSER=0 \
  "${compose[@]}" run --rm --no-deps bootstrap >/dev/null
BOOTSTRAP_EMAIL="$E2E_ADMIN_EMAIL" BOOTSTRAP_PASSWORD="$E2E_ADMIN_PASSWORD" BOOTSTRAP_SUPERUSER=1 BOOTSTRAP_SYNTHETIC=0 BOOTSTRAP_E2E=0 \
  "${compose[@]}" run --rm --no-deps bootstrap >/dev/null
"${compose[@]}" up -d --wait --wait-timeout 180
python -m script.compose_acceptance
python -m script.ci_compose_logging > var/ci-evidence/v1-service-log-policy.json
# Full existing suite: real import, native/portable optimization, cancellation,
# frozen final evaluation, ownership and artifact/browser regression coverage.
TRADEGRAD_REQUIRE_BROWSER=1 python -m script.browser_acceptance
python -m script.compose_acceptance --record "$TRADEGRAD_STATE_ROOT/restart-proof.json"
"${compose[@]}" stop --timeout 45
"${compose[@]}" up -d --wait --wait-timeout 180
python -m script.compose_acceptance --verify "$TRADEGRAD_STATE_ROOT/restart-proof.json"
