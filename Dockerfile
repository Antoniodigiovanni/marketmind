# marketmind — production image. Runs the MCP server (default CMD) or any
# one-off job via `docker compose run --rm marketmind <command>`, reusing
# the same image and dependency install for both.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependencies before the rest of the source so this layer only rebuilds
# when pyproject.toml/uv.lock actually change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

RUN useradd --system --create-home --home-dir /app marketmind \
    && chown -R marketmind:marketmind /app
USER marketmind

CMD ["uv", "run", "--frozen", "--no-sync", "python", "-m", "server.mcp"]
