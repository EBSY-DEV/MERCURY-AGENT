FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# System deps (curl needed for the Claude CLI installer)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (cached layer — rebuilds only when requirements change),
# then Chromium + its system libraries in the same layer cleanup pass.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Non-root user; owns app data and the shared browser install
RUN useradd --create-home --uid 1000 harvey \
    && mkdir -p /app/data \
    && chown -R harvey:harvey /app /ms-playwright

# Copy project (config/prompts are typically bind-mounted over these at runtime)
COPY --chown=harvey:harvey harvey/ harvey/
COPY --chown=harvey:harvey prompts/ prompts/
COPY --chown=harvey:harvey skills/ skills/
COPY --chown=harvey:harvey harvey.yaml .

USER harvey
ENV PATH="/home/harvey/.local/bin:${PATH}"

# Claude Code CLI, installed as the runtime user so ~/.local/bin is correct.
# `|| true` keeps the build working offline; the CLI is required at runtime.
RUN curl -fsSL https://claude.ai/install.sh | sh || true

# Healthy = the heartbeat loop has touched the database recently
# (2h window tolerates long heartbeat intervals and quiet-hour idling).
HEALTHCHECK --interval=5m --timeout=10s --start-period=3m --retries=3 \
    CMD python -c "import os,sys,time; p='/app/data/harvey.db'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 7200 else 1)"

CMD ["python", "-m", "harvey.main"]
