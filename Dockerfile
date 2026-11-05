FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM docker:29-cli AS docker-cli

FROM python:3.11-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    LLM_CACHE_DIR=/data/cache/llm \
    SANDBOX=docker \
    SANDBOX_IMAGE=autograder-improved-sandbox:2.0.0 \
    LLM_DISABLED=true \
    CODACY_CLI=/root/.local/bin/codacy-cli
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY pyproject.toml requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements-lock.txt
COPY sandbox/ sandbox/
RUN bash sandbox/codacy_setup.sh
COPY backend/autograder/ backend/autograder/
RUN pip install --no-cache-dir --no-deps --no-build-isolation -e .
COPY --from=frontend /build/dist/ frontend/dist/
COPY docker/entrypoint.sh /usr/local/bin/autograder-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/autograder-entrypoint \
    && chmod +x /usr/local/bin/autograder-entrypoint
EXPOSE 8020
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8020/api/health', timeout=3)"
ENTRYPOINT ["/usr/local/bin/autograder-entrypoint"]
