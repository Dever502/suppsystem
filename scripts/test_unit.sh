#!/bin/sh
set -eu

workers=${PYTEST_WORKERS:-auto}
shard_count=${PYTEST_SHARD_COUNT:-1}
shard_index=${PYTEST_SHARD_INDEX:-0}

if [ "$shard_count" -eq 1 ]; then
    uv run --frozen pytest -n "$workers" --dist load -m "not postgres" \
        --cov=resolvate --cov-report=term-missing --cov-fail-under=75 "$@"
    exit 0
fi

test_files=$(uv run --frozen python scripts/select_pytest_shard.py \
    --count "$shard_count" --index "$shard_index")
[ -n "$test_files" ] || {
    echo "Selected pytest shard is empty" >&2
    exit 1
}

# Paths are generated exclusively from repository-owned tests/test_*.py files.
# Intentional word splitting passes each path to pytest as a separate argument.
# shellcheck disable=SC2086
uv run --frozen pytest -n "$workers" --dist load -m "not postgres" $test_files \
    --cov=resolvate --cov-report= --cov-fail-under=0 "$@"
