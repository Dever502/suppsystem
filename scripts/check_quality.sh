#!/bin/sh
set -eu

uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen python scripts/check_migrations.py
uv run --frozen python scripts/check_licenses.py
uv run --frozen python scripts/check_publication.py
uv run --frozen python -m compileall -q src tests migrations scripts
sh -n scripts/backup.sh scripts/check_quality.sh scripts/deploy.sh \
    scripts/drill_production_data_path.sh scripts/production-compose.sh scripts/restore.sh \
    scripts/start.sh scripts/test_postgres.sh scripts/test_unit.sh scripts/verify.sh
git diff --check
