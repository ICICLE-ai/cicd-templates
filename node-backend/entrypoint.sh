#!/usr/bin/env sh
set -eu
# TODO: replace with your service's entrypoint
# The final long-running command MUST start with `exec` so your process
# runs as PID 1 and receives SIGTERM on shutdown.
exec node dist/main.js
