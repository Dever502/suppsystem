#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"

compose_file=compose.production.postgres.yaml
compose_project="suppsystem-pgtest-${CI_JOB_ID:-$$}"
started_postgres=false
postgres_container=""

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$started_postgres" = true ]; then
        if [ -n "$postgres_container" ]; then
            docker rm --force "$postgres_container" >/dev/null 2>&1 || true
        fi
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
    POSTGRES_DB=suppsystem_test
    POSTGRES_VOLUME_NAME="${compose_project}_postgres_data"
    POSTGRES_ADMIN_PASSWORD=suppsystem-test-only-password
    POSTGRES_MIGRATION_PASSWORD=suppsystem-test-only-password
    POSTGRES_RUNTIME_PASSWORD=suppsystem-test-only-password
    APP_IMAGE=suppsystem-test-unused
    SUPPSYSTEM_ENV_FILE=.env.example
    export POSTGRES_PORT POSTGRES_DB POSTGRES_VOLUME_NAME
    export POSTGRES_ADMIN_PASSWORD POSTGRES_MIGRATION_PASSWORD
    export POSTGRES_RUNTIME_PASSWORD APP_IMAGE SUPPSYSTEM_ENV_FILE
    export POSTGRES_TEST_PORT
    started_postgres=true
    postgres_container="${compose_project}-postgres-test"
    docker compose --project-name "$compose_project" --file "$compose_file" \
        run --detach --service-ports --name "$postgres_container" \
        --env POSTGRES_DB=suppsystem_test \
        --env POSTGRES_USER=suppsystem_test \
        --env POSTGRES_PASSWORD=suppsystem-test-only-password \
        postgres >/dev/null

    postgres_ready=false
    attempt=0
    while [ "$attempt" -lt 60 ]; do
        if docker exec "$postgres_container" \
            pg_isready --username suppsystem_test --dbname suppsystem_test >/dev/null 2>&1; then
            postgres_ready=true
            break
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    if [ "$postgres_ready" != true ]; then
        docker logs "$postgres_container" >&2
        echo "Disposable PostgreSQL test environment did not become ready" >&2
        exit 1
    fi

    TEST_POSTGRES_DATABASE_URL="postgresql+asyncpg://suppsystem_test:suppsystem-test-only-password@127.0.0.1:${POSTGRES_TEST_PORT}/suppsystem_test"
    export TEST_POSTGRES_DATABASE_URL
fi

export ALLOW_POSTGRES_TEST_DATABASE_CREATION=yes
unset DATABASE_URL

workers=${POSTGRES_PYTEST_WORKERS:-4}

uv run --frozen pytest -n "$workers" --dist load -m postgres \
    tests/test_postgres_migrations.py \
    tests/test_postgres_contracts.py \
    tests/test_retention.py \
    "$@"

# Role provisioning changes cluster-wide principals and must remain serial.
uv run --frozen pytest -m postgres tests/test_postgres_roles.py "$@"
