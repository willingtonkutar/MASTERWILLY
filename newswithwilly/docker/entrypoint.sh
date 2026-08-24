#!/bin/sh
set -eu

cd /app

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    echo "Applying database migrations..."
    alembic upgrade head
fi

echo "Starting NewsWithWilly: $*"
exec python -m src.main "$@"
