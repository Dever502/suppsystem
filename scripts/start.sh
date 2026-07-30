#!/bin/sh
set -eu

usage() {
    echo "Usage: $0 sqlite|postgres [REGISTRY_IMAGE]" >&2
    exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
mode=$1
case "$mode" in
    sqlite)
        manifest=compose.production.sqlite.yaml
        ;;
    postgres)
        manifest=compose.production.postgres.yaml
        ;;
    *)
        usage
        ;;
esac

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
env_file=${SUPPSYSTEM_ENV_FILE:-${root}/.env}
[ -s "$env_file" ] || {
    echo "Configuration file is missing or empty: $env_file" >&2
    echo "Create it with: cp .env.example .env" >&2
    exit 1
}
env_dir=$(CDPATH= cd -- "$(dirname "$env_file")" && pwd)
env_file=${env_dir}/$(basename "$env_file")

command -v docker >/dev/null 2>&1 || {
    echo "Docker is not installed or not available in PATH" >&2
    exit 1
}
docker compose version >/dev/null 2>&1 || {
    echo "Docker Compose v2 is not available" >&2
    exit 1
}

default_image=ghcr.io/dever502/suppsystem:v2.0.0
requested_image=${2:-${APP_IMAGE:-$default_image}}
docker pull "$requested_image"
resolved_image=$(docker image inspect --format '{{index .RepoDigests 0}}' "$requested_image")
case "$resolved_image" in
    *@sha256:*) ;;
    *)
        echo "Registry image did not resolve to an immutable digest: $requested_image" >&2
        exit 1
        ;;
esac

APP_IMAGE=$resolved_image
SUPPSYSTEM_ENV_FILE=$env_file
export APP_IMAGE SUPPSYSTEM_ENV_FILE

compose() {
    docker compose \
        --project-directory "$root" \
        --env-file "$env_file" \
        --file "$root/$manifest" \
        "$@"
}

rendered=
cleanup() {
    if [ -n "$rendered" ]; then
        rm -f "$rendered"
    fi
}
trap cleanup EXIT HUP INT TERM
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$mode" = postgres ]; then
    umask 077
    rendered=$(mktemp "${TMPDIR:-/tmp}/suppsystem-compose.XXXXXX.json")
    compose config --format json > "$rendered"
    docker run --rm -i "$APP_IMAGE" python -m suppsystem.production < "$rendered"
else
    compose config --quiet
fi

compose pull
compose up --detach --wait
compose ps
echo "Suppsystem started with $mode using immutable image $APP_IMAGE"
