#!/bin/sh
set -eu

usage() {
    echo "Usage: $0 preflight | deploy IMAGE_REFERENCE | rollback" >&2
    exit 2
}

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
deploy_dir=${DEPLOY_DIR:-/opt/supportbot}
env_file=${SUPPORTBOT_ENV_FILE:-${deploy_dir}/.env}
state_file=${DEPLOYMENT_STATE_FILE:-${deploy_dir}/deployment.env}
rollback_file=${ROLLBACK_STATE_FILE:-${deploy_dir}/rollback.env}
temporary_state=
secondary_state=
lock_dir=

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ -n "$temporary_state" ]; then
        rm -f "$temporary_state"
    fi
    if [ -n "$secondary_state" ]; then
        rm -f "$secondary_state"
    fi
    if [ -n "$lock_dir" ]; then
        rmdir "$lock_dir" 2>/dev/null || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[ "$#" -ge 1 ] || usage
[ -s "$env_file" ] || {
    echo "Production environment file is missing or empty: $env_file" >&2
    exit 1
}
mkdir -p "$deploy_dir"
candidate_lock=${deploy_dir}/.deployment-lock
if ! mkdir "$candidate_lock"; then
    echo "Another deployment operation is active; lock exists: $candidate_lock" >&2
    exit 1
fi
lock_dir=$candidate_lock

compose_with_state() {
    candidate_state=$1
    shift
    docker compose \
        --env-file "$env_file" \
        --env-file "$candidate_state" \
        --file "$root/compose.production.postgres.yaml" \
        "$@"
}

state_image() {
    sed -n 's/^APP_IMAGE=//p' "$1"
}

write_state() {
    output=$1
    image=$2
    umask 077
    {
        printf 'APP_IMAGE=%s\n' "$image"
        printf 'SUPPORTBOT_ENV_FILE=%s\n' "$env_file"
    } > "$output"
}

preflight() {
    candidate_state=$1
    image=$(state_image "$candidate_state")
    [ -n "$image" ] || {
        echo "Deployment state does not contain APP_IMAGE" >&2
        exit 1
    }
    rendered=$(mktemp "${TMPDIR:-/tmp}/supportbot-compose.XXXXXX.json")
    if ! compose_with_state "$candidate_state" config --format json > "$rendered"; then
        rm -f "$rendered"
        return 1
    fi
    if ! compose_with_state "$candidate_state" pull; then
        rm -f "$rendered"
        return 1
    fi
    if ! docker run --rm -i "$image" python -m supportbot.production < "$rendered"; then
        rm -f "$rendered"
        return 1
    fi
    rm -f "$rendered"
}

activate() {
    candidate_state=$1
    preflight "$candidate_state"
    compose_with_state "$candidate_state" up --detach --wait
}

case "$1" in
    preflight)
        [ "$#" -eq 1 ] || usage
        [ -s "$state_file" ] || {
            echo "Deployment state is missing or empty: $state_file" >&2
            exit 1
        }
        preflight "$state_file"
        ;;
    deploy)
        [ "$#" -eq 2 ] || usage
        requested_image=$2
        docker pull "$requested_image"
        resolved_image=$(docker image inspect --format '{{index .RepoDigests 0}}' "$requested_image")
        case "$resolved_image" in
            *@sha256:*) ;;
            *)
                echo "Registry image did not resolve to an immutable digest: $requested_image" >&2
                exit 1
                ;;
        esac
        candidate=$(mktemp "${deploy_dir}/deployment.XXXXXX")
        temporary_state=$candidate
        write_state "$candidate" "$resolved_image"
        if [ -s "$state_file" ]; then
            cp -p "$state_file" "$rollback_file"
        fi
        activate "$candidate"
        mv "$candidate" "$state_file"
        temporary_state=
        echo "Deployment completed with immutable image $resolved_image"
        ;;
    rollback)
        [ "$#" -eq 1 ] || usage
        [ -s "$rollback_file" ] || {
            echo "No verified rollback image is recorded" >&2
            exit 1
        }
        candidate=$(mktemp "${deploy_dir}/rollback.XXXXXX")
        temporary_state=$candidate
        cp -p "$rollback_file" "$candidate"
        replacement_rollback=$(mktemp "${deploy_dir}/rollback-next.XXXXXX")
        secondary_state=$replacement_rollback
        cp -p "$state_file" "$replacement_rollback"
        activate "$candidate"
        mv "$candidate" "$state_file"
        temporary_state=
        mv "$replacement_rollback" "$rollback_file"
        secondary_state=
        echo "Rollback completed with immutable image $(state_image "$state_file")"
        ;;
    *)
        usage
        ;;
esac
