FROM python:3.14-alpine3.23 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --all-extras --no-install-project --no-dev

COPY pyproject.toml LICENSE README.md ./
COPY src/ ./src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --all-extras --no-dev


FROM python:3.14-alpine3.23
LABEL org.opencontainers.image.description="CLI tool for syncing content to Open WebUI Knowledge Bases" \
      org.opencontainers.image.source="https://github.com/open-webui/oikb" \
      org.opencontainers.image.vendor="Open WebUI Inc." \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache ca-certificates \
    && addgroup -g 1000 appuser \
    && adduser -D -u 1000 -G appuser appuser

COPY --from=builder --chown=appuser:appuser /app /app

WORKDIR /app

USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

ENTRYPOINT ["oikb"]
