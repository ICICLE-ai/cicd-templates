#!/usr/bin/env sh
set -eu
# TODO: replace with your service's entrypoint
exec uvicorn my_service.main:app --host 0.0.0.0 --port "${PORT:-8000}"
