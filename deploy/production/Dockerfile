# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded AS uv

FROM python:3.14.2-slim-bookworm@sha256:e87711ef5c86aaeaa7031718a69db79d334d94c545c709583f651b8185870941 AS build

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable --extra postgres --extra server

FROM python:3.14.2-slim-bookworm@sha256:e87711ef5c86aaeaa7031718a69db79d334d94c545c709583f651b8185870941 AS runtime

RUN groupadd --gid 10001 cli-consumption \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app cli-consumption
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8765

ENTRYPOINT ["cli-consumption"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765"]
