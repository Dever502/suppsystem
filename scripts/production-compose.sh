#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
deploy_dir=${DEPLOY_DIR:-/opt/suppsystem}
env_file=${SUPPSYSTEM_ENV_FILE:-${deploy_dir}/.env}
state_file=${DEPLOYMENT_STATE_FILE:-${deploy_dir}/deployment.env}

[ -s "$env_file" ] || {
    echo "Production environment file is missing or empty: $env_file" >&2
    exit 1
}
[ -s "$state_file" ] || {
    echo "Deployment state is missing or empty: $state_file" >&2
    exit 1
}

exec docker compose \
    --env-file "$env_file" \
    --env-file "$state_file" \
    --file "$root/compose.production.postgres.yaml" \
    "$@"
