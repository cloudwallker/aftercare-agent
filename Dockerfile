FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.9.8 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
RUN uv sync --locked --no-dev

FROM base AS production
CMD ["uv", "run", "--no-sync", "alembic", "upgrade", "head"]

FROM base AS tests
RUN uv sync --locked
COPY tests ./tests
CMD ["uv", "run", "--no-sync", "pytest", "tests/unit", "tests/integration", "tests/e2e", "-q"]
