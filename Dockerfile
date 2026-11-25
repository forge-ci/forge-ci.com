# syntax=docker/dockerfile:1
# Every gate runs inside the image build, so Render cannot deploy code that
# fails formatting, lint, type checks or tests: the runtime stage copies a
# marker file out of each check stage.

# ---- frontend: prettier + tsc --strict + eslint strictTypeChecked, then build
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run check && npm run build && touch /web-checked

# ---- python runtime dependencies
FROM python:3.12-slim AS py
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

# ---- python gates: ruff format (black style) + ruff lint (isort incl.) + basedpyright + pytest
FROM py AS check
RUN uv sync --frozen
COPY backend backend
COPY agent agent
COPY tests tests
RUN uv run ruff format --check . \
 && uv run ruff check . \
 && uv run basedpyright \
 && uv run pytest \
 && touch /py-checked

# ---- runtime
FROM python:3.12-slim AS runtime
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=py /app/.venv /app/.venv
COPY backend backend
COPY --from=web /web/dist frontend/dist
COPY --from=web /web-checked /checks/web
COPY --from=check /py-checked /checks/py
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PORT=10000 \
    FORGE_STATIC_DIR=/app/frontend/dist
USER app
# Import the app with production dependencies before publishing the image.
RUN PYTHONPATH=backend python -c 'from forge_dash.app import app_factory; app_factory()'
EXPOSE 10000
# One worker on purpose: the sample store and the live fan-out are in-process.
CMD ["sh", "-c", "exec uvicorn forge_dash.app:app_factory --factory --app-dir backend --host 0.0.0.0 --port \"$PORT\" --proxy-headers --forwarded-allow-ips='*' --no-server-header"]
