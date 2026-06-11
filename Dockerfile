FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim

ENV UV_NO_MANAGED_PYTHON=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=0

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY src ./src
COPY README.md ./

RUN uv sync --frozen --no-dev \
    && groupadd --gid 10001 secondbrain \
    && useradd --uid 10001 \
        --gid 10001 \
        --create-home \
        --shell /usr/sbin/nologin \
        secondbrain \
    && mkdir -p /var/lib/second-brain \
    && chown -R secondbrain:secondbrain /var/lib/second-brain /app

COPY deploy/container-entrypoint.sh /usr/local/bin/secondbrain-entrypoint
RUN chmod 755 /usr/local/bin/secondbrain-entrypoint

USER secondbrain

ENTRYPOINT ["/usr/local/bin/secondbrain-entrypoint"]
CMD ["/app/.venv/bin/python", "-m", "secondbrain", "run"]
