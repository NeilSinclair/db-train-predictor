# The collector image. Station reference data and the database live in
# /app/data, which is mounted from the host (see compose.yaml), so the image
# itself holds only code.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.9 /uv /bin/uv

# Use the image's Python rather than downloading one, compile bytecode for a
# faster start, and print logs straight away.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# A fixed UID and GID, so the host's data folder can be owned by them.
RUN groupadd --system --gid 10001 collector \
    && useradd --system --uid 10001 --gid 10001 --no-create-home collector \
    && mkdir -p /app/data \
    && chown collector /app/data
USER collector

ENV PATH="/app/.venv/bin:$PATH"
CMD ["db-ml-collector"]
