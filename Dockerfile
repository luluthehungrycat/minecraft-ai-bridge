FROM python:3.13-slim

# Pinned uv (same version as CI); the image installs the exact versions in uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.13.8@sha256:d6be4539ed178df7972a67a8a5244b9663974ba9d83f0a9fab0619a585fd42d8 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first, so source edits reuse this layer. --locked fails the
# build if uv.lock is out of date with pyproject.toml.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --no-cache

# Then the bridge package itself (not editable: the image has no source mount).
COPY minecraft_ai_bridge/ minecraft_ai_bridge/
RUN uv sync --locked --no-editable --no-cache

# Default command (override via compose or CLI)
ENTRYPOINT ["minecraft-ai-bridge"]
CMD []
