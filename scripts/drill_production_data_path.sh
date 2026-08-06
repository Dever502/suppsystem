#!/bin/sh
set -eu

usage() {
    echo "Usage: CONFIRM_PRODUCTION_DATA_PATH_DRILL=isolated $0 BASELINE_IMAGE CANDIDATE_IMAGE REPORT" >&2
    exit 2
}

[ "$#" -eq 3 ] || usage
[ "${CONFIRM_PRODUCTION_DATA_PATH_DRILL:-}" = "isolated" ] || {
    echo "Refusing destructive drill without CONFIRM_PRODUCTION_DATA_PATH_DRILL=isolated" >&2
    exit 2
}
[ "${CONFIRM_SCHEMA_COMPATIBLE_ROLLBACK:-}" = "yes" ] || {
    echo "Set CONFIRM_SCHEMA_COMPATIBLE_ROLLBACK=yes after reviewing candidate migrations" >&2
    exit 2
}

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
deploy_dir=${DEPLOY_DIR:-/opt/resolvate-drill}
env_file=${RESOLVATE_ENV_FILE:-${deploy_dir}/.env}
state_file=${DEPLOYMENT_STATE_FILE:-${deploy_dir}/deployment.env}
marker=${deploy_dir}/.resolvate-drill-environment
baseline_image=$1
candidate_image=$2
report=$3
backup=${report}.postgres.dump

[ -f "$marker" ] && [ "$(sed -n '1p' "$marker")" = "isolated" ] || {
    echo "Drill marker is missing: create $marker containing exactly 'isolated'" >&2
    exit 2
}
[ -s "$env_file" ] || {
    echo "Drill environment file is missing: $env_file" >&2
    exit 1
}
mkdir -p "$(dirname "$report")"
: > "$report"

record() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" | tee -a "$report"
}

compose() {
    DEPLOY_DIR=$deploy_dir \
    RESOLVATE_ENV_FILE=$env_file \
    DEPLOYMENT_STATE_FILE=$state_file \
        sh "$root/scripts/production-compose.sh" "$@"
}

deploy() {
    DEPLOY_DIR=$deploy_dir \
    RESOLVATE_ENV_FILE=$env_file \
    DEPLOYMENT_STATE_FILE=$state_file \
        sh "$root/scripts/deploy.sh" "$@"
}

data_operation() {
    PRODUCTION_DEPLOYMENT=yes \
    DEPLOY_DIR=$deploy_dir \
    RESOLVATE_ENV_FILE=$env_file \
    DEPLOYMENT_STATE_FILE=$state_file \
        "$@"
}

fingerprint() {
    compose exec -T postgres sh -eu -c \
        'PGPASSWORD="$POSTGRES_RUNTIME_PASSWORD" psql --username="$POSTGRES_RUNTIME_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align --command="SELECT (SELECT version_num FROM alembic_version) || chr(58) || (SELECT count(*) FROM users) || chr(58) || (SELECT count(*) FROM tickets) || chr(58) || (SELECT count(*) FROM ticket_messages) || chr(58) || (SELECT count(*) FROM delivery_outbox) || chr(58) || (SELECT count(*) FROM operator_actions)"'
}

if [ ! -s "$state_file" ]; then
    record "clean deploy baseline"
    deploy deploy "$baseline_image"
else
    current_image=$(sed -n 's/^APP_IMAGE=//p' "$state_file")
    [ "$current_image" = "$baseline_image" ] || {
        echo "Existing drill deployment does not use the requested baseline image" >&2
        exit 1
    }
    deploy preflight
fi

record "stop baseline and capture control data"
compose stop resolvate
before=$(fingerprint)
data_operation sh "$root/scripts/backup.sh" postgres "$backup"
compose up --detach --wait resolvate

record "deploy candidate and wait for container health"
deploy deploy "$candidate_image"

record "rollback to immutable baseline"
deploy rollback

record "inject restore failure after application stop"
set +e
CONFIRM_RESTORE=yes \
CONFIRM_RESTORE_FAILURE_INJECTION=yes \
RESOLVATE_RESTORE_FAILURE_INJECTION=after_stop \
PRODUCTION_DEPLOYMENT=yes \
DEPLOY_DIR=$deploy_dir \
RESOLVATE_ENV_FILE=$env_file \
DEPLOYMENT_STATE_FILE=$state_file \
    sh "$root/scripts/restore.sh" postgres "$backup" >> "$report" 2>&1
failure_status=$?
set -e
[ "$failure_status" -eq 97 ] || {
    echo "Restore failure injection returned unexpected status $failure_status" >&2
    exit 1
}
running=$(compose ps --status running --services resolvate)
[ -z "$running" ] || {
    echo "Resolvate unexpectedly restarted after failed restore" >&2
    exit 1
}

record "restore valid backup and wait for health"
CONFIRM_RESTORE=yes \
PRODUCTION_DEPLOYMENT=yes \
DEPLOY_DIR=$deploy_dir \
RESOLVATE_ENV_FILE=$env_file \
DEPLOYMENT_STATE_FILE=$state_file \
    sh "$root/scripts/restore.sh" postgres "$backup"

record "stop application and verify restored control data"
compose stop resolvate
after=$(fingerprint)
if [ "$before" != "$after" ]; then
    echo "Restored control fingerprint differs from backup fingerprint" >&2
    echo "before=$before" >&2
    echo "after=$after" >&2
    exit 1
fi
compose up --detach --wait resolvate

record "production data path drill passed"
