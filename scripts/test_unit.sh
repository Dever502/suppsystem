#!/bin/sh
set -eu

workers=${PYTEST_WORKERS:-auto}

uv run --frozen pytest -n "$workers" --dist load -m "not postgres" \
    --cov=suppsystem --cov-report=term-missing --cov-fail-under=75 "$@"
