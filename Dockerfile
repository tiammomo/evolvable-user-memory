FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable \
    && groupadd --gid 10001 emf \
    && useradd --uid 10001 --gid emf --home-dir /app --no-create-home \
        --shell /usr/sbin/nologin emf

USER 10001:10001

EXPOSE 33009 38089

CMD ["evolvable-memory"]
