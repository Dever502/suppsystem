FROM ghcr.io/astral-sh/uv:0.11.27@sha256:4d01caf3b22dfd11003455e2e68153da08c4ee1fa54fdbd166c6282d22693419 AS uv

FROM python:3.12.13-alpine3.24@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df AS runtime

ARG BUILD_DATE=unknown
ARG VCS_REF=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/app/data

WORKDIR /app

RUN addgroup -S app && adduser -S -D -H -G app app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app alembic.ini ./
COPY --chown=app:app src ./src
COPY --chown=app:app migrations ./migrations

RUN uv sync --frozen --no-dev --no-editable && \
    python -c "import suppsystem; assert suppsystem.__version__ == '3.0.0'" && \
    mkdir /app/data && \
    chown app:app /app/data

USER app

LABEL org.opencontainers.image.title="suppsystem" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/Dever502/suppsystem" \
      org.opencontainers.image.version="3.0.0" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.revision="$VCS_REF"

CMD ["suppsystem"]
