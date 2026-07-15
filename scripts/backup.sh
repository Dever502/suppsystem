#!/bin/sh
set -eu

usage() {
    echo "Usage: $0 sqlite|postgres OUTPUT" >&2
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
backend=$1
output=$2
output_dir=$(dirname "$output")
mkdir -p "$output_dir"
umask 077
temporary="${output}.tmp.$$"
cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    rm -f "$temporary"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

case "$backend" in
    sqlite)
        compose exec -T supportbot python -m supportbot.operations sqlite-backup > "$temporary"
        ;;
    postgres)
        compose exec -T postgres sh -eu -c \
            'PGPASSWORD="$POSTGRES_RUNTIME_PASSWORD" pg_dump --format=custom --no-owner --no-acl --username="$POSTGRES_RUNTIME_USER" --dbname="$POSTGRES_DB"' \
            > "$temporary"
        compose exec -T postgres pg_restore --list < "$temporary" >/dev/null
        ;;
    *)
        usage
        ;;
esac

[ -s "$temporary" ] || {
    echo "Backup is empty; refusing to publish it" >&2
    exit 1
}
mv "$temporary" "$output"
trap - EXIT HUP INT TERM
echo "Backup written to $output"
