FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=2.1.1 \
    POETRY_HOME=/opt/poetry \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PATH="/opt/poetry/bin:$PATH"

RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && curl -sSL https://install.python-poetry.org | python3 - \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Keep dependency installation in its own layer for faster rebuilds.
COPY pyproject.toml README.md ./
RUN poetry install --only main --no-root --no-ansi

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker/entrypoint.sh /usr/local/bin/newswithwilly-entrypoint
RUN chmod +x /usr/local/bin/newswithwilly-entrypoint \
    && mkdir -p /app/logs /app/data \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app

USER app

ENTRYPOINT ["/usr/local/bin/newswithwilly-entrypoint"]
CMD ["run"]
