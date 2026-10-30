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

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HANDPICKED_CONFIG=/config/config.yml
VOLUME /config
# Unraid's nobody:users, so files in appdata stay editable over SMB.
USER 99:100
# Exec form, so handpicked is PID 1 and receives SIGTERM.
CMD ["handpicked", "serve"]
