FROM ghcr.io/astral-sh/uv:0.11.27@sha256:4d01caf3b22dfd11003455e2e68153da08c4ee1fa54fdbd166c6282d22693419 AS uv

FROM python:3.12.13-alpine3.24@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && \
    /app/.venv/bin/python -c "import resolvate; assert resolvate.__version__ == '3.5.0'"

FROM python:3.12.13-alpine3.24@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df AS runtime

ARG BUILD_DATE=unknown
ARG VCS_REF=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/app/data

WORKDIR /app

RUN addgroup -S app && \
    adduser -S -D -H -G app app && \
    mkdir /app/data && \
    chown app:app /app/data

COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini LICENSE ./
COPY migrations ./migrations

USER app

LABEL org.opencontainers.image.title="Resolvate" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/Dever502/resolvate" \
      org.opencontainers.image.version="3.5.0" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.revision="$VCS_REF"

CMD ["resolvate"]
