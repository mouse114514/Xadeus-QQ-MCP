# =============================================================================
# Xadeus-QQ-MCP — Dockerfile for Glama safety & quality checks
# =============================================================================
# Build:  docker build -t xadeus-qq-mcp .
# Run:    docker run --rm xadeus-qq-mcp --help
# =============================================================================
# The server expects a running NapCatQQ instance.  Without one it logs a
# warning and still starts (MCP tools register).
# =============================================================================

FROM python:3.11-slim

WORKDIR /app

# System deps: Playwright browser runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install the package from local source
COPY . .
RUN pip install --no-cache-dir .

# Install Playwright Chromium browser (needed for screenshot_chat tool)
RUN python -m playwright install chromium --with-deps 2>/dev/null || true
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

# Default config (override via env or CLI args)
ENV QQ_OVERRIDE=3838379219

EXPOSE 3000 3001

ENTRYPOINT ["qq-agent-mcp"]
CMD ["--napcat-host", "127.0.0.1", "--napcat-port", "3000", "--ws-port", "3001", "--log-level", "info"]
