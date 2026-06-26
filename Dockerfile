# kops — read-only kubectl MCP server.
#
# This image exists so registries (e.g. Glama) can build the server, start it,
# and exercise MCP introspection (initialize + tools/list) without a cluster:
# tools are registered statically and kubectl is only invoked at tool-CALL time,
# so the server starts and introspects fine with no kubeconfig present.
#
# For ACTUAL use, the container needs `kubectl` on PATH and a kubeconfig mounted
# (e.g. -v ~/.kube:/root/.kube:ro); kubectl is intentionally not baked in to keep
# the image small and version-neutral.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Install dependencies against the locked set first (better layer caching),
# then the project itself.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Run the stdio MCP server.
ENTRYPOINT ["uv", "run", "--no-dev", "kops"]
