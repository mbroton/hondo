FROM node:24-alpine AS dashboard
WORKDIR /client
COPY apps/client/package.json apps/client/package-lock.json ./
RUN npm ci
COPY apps/client ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS server
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY apps/server/pyproject.toml apps/server/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY apps/server ./
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm
RUN useradd --create-home hondo
COPY --from=server /app/.venv /app/.venv
COPY --from=dashboard /client/dist /app/dashboard
ENV PATH="/app/.venv/bin:$PATH" HONDO_API__DASHBOARD_DIR=/app/dashboard
USER hondo
EXPOSE 8000
ENTRYPOINT ["hondo"]
CMD ["serve"]
