FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Dependencies first, so a code change reuses this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

ENV PYTHONUNBUFFERED=1 \
    HANDPICKED_CONFIG=/config/config.yml
# Starts as root to fix /config's owner, then runs as PUID:PGID. The venv is
# not on PATH, so `docker exec ... handpicked` goes through this too.
COPY --chmod=755 docker/handpicked.sh /usr/local/bin/handpicked
VOLUME /config
# Exec form, and the wrapper execs, so handpicked is PID 1 and receives SIGTERM.
CMD ["handpicked", "serve"]
