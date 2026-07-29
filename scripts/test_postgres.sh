#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"

compose_file=compose.production.postgres.yaml
compose_project="suppsystem-pgtest-${CI_JOB_ID:-$$}"
started_postgres=false

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$started_postgres" = true ]; then
        if ! docker compose --project-name "$compose_project" --file "$compose_file" \
            down --volumes --remove-orphans >/dev/null; then
            echo "Unable to remove disposable PostgreSQL test environment" >&2
            if [ "$status" -eq 0 ]; then
                status=1
            fi
        fi
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -z "${TEST_POSTGRES_DATABASE_URL:-}" ]; then
    command -v docker >/dev/null 2>&1 || {
        echo "Docker is required when TEST_POSTGRES_DATABASE_URL is not set" >&2
        exit 1
    }
    POSTGRES_TEST_PORT=${POSTGRES_TEST_PORT:-55432}
    POSTGRES_PORT=$POSTGRES_TEST_PORT
    POSTGRES_DB=supportbot_test
    POSTGRES_ADMIN_USER=supportbot_test
    POSTGRES_VOLUME_NAME="${compose_project}_postgres_data"
    POSTGRES_ADMIN_PASSWORD=supportbot-test-only-password
    POSTGRES_MIGRATION_PASSWORD=supportbot-test-only-password
    POSTGRES_RUNTIME_PASSWORD=supportbot-test-only-password
    APP_IMAGE=supportbot-test-unused
    SUPPORTBOT_ENV_FILE=.env.example
    export POSTGRES_PORT POSTGRES_DB POSTGRES_ADMIN_USER POSTGRES_VOLUME_NAME
    export POSTGRES_ADMIN_PASSWORD POSTGRES_MIGRATION_PASSWORD
    export POSTGRES_RUNTIME_PASSWORD APP_IMAGE SUPPORTBOT_ENV_FILE
    export POSTGRES_TEST_PORT
    started_postgres=true
    docker compose --project-name "$compose_project" --file "$compose_file" \
        up --detach --wait --wait-timeout 60 postgres
    TEST_POSTGRES_DATABASE_URL="postgresql+asyncpg://supportbot_test:supportbot-test-only-password@127.0.0.1:${POSTGRES_TEST_PORT}/supportbot_test"
    export TEST_POSTGRES_DATABASE_URL
fi

export ALLOW_POSTGRES_TEST_DATABASE_CREATION=yes
unset DATABASE_URL

uv run --frozen pytest -m postgres \
    tests/test_postgres_migrations.py \
    tests/test_postgres_contracts.py \
    tests/test_retention.py \
    tests/test_postgres_roles.py \
    "$@"
