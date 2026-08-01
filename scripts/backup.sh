#!/bin/sh
set -eu

usage() {
    echo "Usage: $0 sqlite|postgres DATABASE_OUTPUT [MEDIA_OUTPUT]" >&2
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

[ "$#" -ge 2 ] && [ "$#" -le 3 ] || usage
backend=$1
output=$2
media_output=${3:-}
output_dir=$(dirname "$output")
mkdir -p "$output_dir"
if [ -n "$media_output" ]; then
    mkdir -p "$(dirname "$media_output")"
fi
umask 077
temporary="${output}.tmp.$$"
media_temporary=
application_stopped=false
if [ -n "$media_output" ]; then
    media_temporary="${media_output}.tmp.$$"
fi
cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    rm -f "$temporary"
    if [ -n "$media_temporary" ]; then
        rm -f "$media_temporary"
    fi
    if [ "$application_stopped" = true ]; then
        compose up --detach --wait suppsystem || status=$?
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -n "$media_output" ]; then
    compose stop suppsystem
    application_stopped=true
fi

case "$backend" in
    sqlite)
        if [ -n "$media_output" ]; then
            compose run --rm -T --no-deps suppsystem \
                python -m suppsystem.operations sqlite-backup > "$temporary"
        else
            compose exec -T suppsystem python -m suppsystem.operations sqlite-backup > "$temporary"
        fi
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

if [ -n "$media_output" ]; then
    compose run --rm -T --no-deps suppsystem \
        python -m suppsystem.media_archive export > "$media_temporary"
    compose run --rm -T --no-deps suppsystem \
        python -m suppsystem.media_archive validate < "$media_temporary"
fi

[ -s "$temporary" ] || {
    echo "Backup is empty; refusing to publish it" >&2
    exit 1
}
mv "$temporary" "$output"
if [ -n "$media_output" ]; then
    mv "$media_temporary" "$media_output"
    compose up --detach --wait suppsystem
    application_stopped=false
fi
trap - EXIT HUP INT TERM
echo "Backup written to $output"
if [ -n "$media_output" ]; then
    echo "Media backup written to $media_output"
fi
