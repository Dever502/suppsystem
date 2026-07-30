#!/bin/sh
set -eu

usage() {
    echo "Usage: CONFIRM_RESTORE=yes $0 sqlite|postgres INPUT" >&2
    exit 2
}

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)

compose() {
    if [ "${PRODUCTION_DEPLOYMENT:-}" = "yes" ]; then
        sh "$root/scripts/production-compose.sh" "$@"
    else
        docker compose "$@"
    fi
}

[ "$#" -eq 2 ] || usage
[ "${CONFIRM_RESTORE:-}" = "yes" ] || {
    echo "Restore is destructive; set CONFIRM_RESTORE=yes" >&2
    exit 2
}
backend=$1
input=$2
[ -s "$input" ] || {
    echo "Backup does not exist or is empty: $input" >&2
    exit 1
}

case "$backend" in
    sqlite)
        compose run --rm -T --no-deps suppsystem \
            python -m suppsystem.operations sqlite-validate < "$input"
        ;;
    postgres)
        compose exec -T postgres pg_restore --list < "$input" >/dev/null
        ;;
    *)
        usage
        ;;
esac

application_stopped=false
restore_exit() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$application_stopped" = true ] && [ "$status" -ne 0 ]; then
        echo "Restore failed; suppsystem remains stopped for manual verification" >&2
    fi
    exit "$status"
}
trap restore_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

compose stop suppsystem
application_stopped=true

if [ "${SUPPSYSTEM_RESTORE_FAILURE_INJECTION:-}" = "after_stop" ]; then
    [ "${CONFIRM_RESTORE_FAILURE_INJECTION:-}" = "yes" ] || {
        echo "Restore failure injection requires CONFIRM_RESTORE_FAILURE_INJECTION=yes" >&2
        exit 2
    }
    echo "Injected restore failure after application stop" >&2
    exit 97
fi

case "$backend" in
    sqlite)
        compose run --rm -T --no-deps suppsystem \
            python -m suppsystem.operations sqlite-restore < "$input"
        ;;
    postgres)
        compose exec -T postgres sh -eu -c \
            'PGPASSWORD="$POSTGRES_MIGRATION_PASSWORD" pg_restore --clean --if-exists --exit-on-error --no-owner --no-acl --username="$POSTGRES_MIGRATION_USER" --dbname="$POSTGRES_DB"' \
            < "$input"
        ;;
esac

if [ "$backend" = "postgres" ]; then
    # The restored archive may be older than the running image. Remove the successful
    # one-shot container so Compose must execute migrations again before suppsystem.
    compose rm --force --stop postgres-migrate
fi
compose up --detach --wait suppsystem
application_stopped=false
trap - EXIT HUP INT TERM
echo "Restore completed; application is healthy"
