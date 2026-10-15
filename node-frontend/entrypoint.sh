#!/usr/bin/env sh
set -eu

# New pods supply PORT; omission preserves existing containers on port 8000.
# Reject privileged ports, leading zeros, and configuration injection before
# rendering. Bound the digit count before shell arithmetic can overflow.
PORT=${PORT-8000}
case "$PORT" in
    ''|*[!0-9]*|0*)
        echo "ERROR: PORT must be an integer from 1024 to 65535." >&2
        exit 1 ;;
esac
if [ "${#PORT}" -gt 5 ] || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
    echo "ERROR: PORT must be an integer from 1024 to 65535." >&2
    exit 1
fi
export PORT

fail() {
    echo "ERROR: BACKEND_URL must be an http(s)://host[:port] origin (no path, query, credentials, or whitespace)." >&2
    exit 1
}

BACKEND_URL=${BACKEND_URL:-}
if [ -n "$BACKEND_URL" ]; then
    # Accept one trailing slash, but never put a URI into proxy_pass: nginx
    # would otherwise replace the /api prefix. Reject config/variable injection.
    BACKEND_URL=${BACKEND_URL%/}
    case "$BACKEND_URL" in
        *[[:space:]]*) fail ;;
    esac
    printf '%s\n' "$BACKEND_URL" | LC_ALL=C grep -Eq \
        '^https?://([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?$' \
        || fail
    authority=${BACKEND_URL#*://}
    case "$authority" in
        \[*\]) ;; # IPv6 literal without a port.
        *:*)
            port=${authority##*:}
            [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || fail
            ;;
    esac
    export BACKEND_URL
fi

# Keep nginx's existing limits unless a service explicitly opts into larger
# uploads, slower upstreams, streaming responses, or WebSocket upgrades.
validate_integer() {
    name=$1 value=$2 maximum=$3
    case "$value" in
        ''|*[!0-9]*|0*)
            echo "ERROR: $name must be an integer from 1 to $maximum." >&2
            exit 1 ;;
    esac
    # Bound the digit count before shell arithmetic/comparison (no overflow).
    if [ "${#value}" -gt 4 ] || [ "$value" -gt "$maximum" ]; then
        echo "ERROR: $name must be an integer from 1 to $maximum." >&2
        exit 1
    fi
}

BACKEND_MAX_BODY_SIZE_MB=${BACKEND_MAX_BODY_SIZE_MB-1}
BACKEND_CONNECT_TIMEOUT_SECONDS=${BACKEND_CONNECT_TIMEOUT_SECONDS-60}
BACKEND_READ_TIMEOUT_SECONDS=${BACKEND_READ_TIMEOUT_SECONDS-60}
BACKEND_SEND_TIMEOUT_SECONDS=${BACKEND_SEND_TIMEOUT_SECONDS-60}
BACKEND_STREAMING=${BACKEND_STREAMING-off}
BACKEND_WEBSOCKETS=${BACKEND_WEBSOCKETS-off}
validate_integer BACKEND_MAX_BODY_SIZE_MB "$BACKEND_MAX_BODY_SIZE_MB" 1024
validate_integer BACKEND_CONNECT_TIMEOUT_SECONDS "$BACKEND_CONNECT_TIMEOUT_SECONDS" 75
validate_integer BACKEND_READ_TIMEOUT_SECONDS "$BACKEND_READ_TIMEOUT_SECONDS" 3600
validate_integer BACKEND_SEND_TIMEOUT_SECONDS "$BACKEND_SEND_TIMEOUT_SECONDS" 3600
for name in BACKEND_STREAMING BACKEND_WEBSOCKETS; do
    case "$name" in
        BACKEND_STREAMING) value=$BACKEND_STREAMING ;;
        BACKEND_WEBSOCKETS) value=$BACKEND_WEBSOCKETS ;;
    esac
    case "$value" in
        on|off) ;;
        *) echo "ERROR: $name must be on or off." >&2; exit 1 ;;
    esac
done
case "$BACKEND_STREAMING" in
    on) BACKEND_PROXY_BUFFERING=off ;;
    off) BACKEND_PROXY_BUFFERING=on ;;
esac
export BACKEND_MAX_BODY_SIZE_MB BACKEND_CONNECT_TIMEOUT_SECONDS
export BACKEND_READ_TIMEOUT_SECONDS BACKEND_SEND_TIMEOUT_SECONDS
export BACKEND_PROXY_BUFFERING BACKEND_WEBSOCKETS

# The image creates this directory for uid 999. Recreate it if /tmp is mounted
# as an empty writable volume; no writes to /etc/nginx are needed at runtime.
mkdir -p /tmp/nginx
envsubst '${PORT} ${BACKEND_WEBSOCKETS}' \
    < /etc/nginx/templates/nginx.conf > /tmp/nginx/nginx.conf
# Always replace the include, including after a restart with proxying disabled.
: > /tmp/nginx/api.conf
if [ -n "$BACKEND_URL" ]; then
    envsubst '${BACKEND_URL} ${BACKEND_MAX_BODY_SIZE_MB} ${BACKEND_CONNECT_TIMEOUT_SECONDS} ${BACKEND_READ_TIMEOUT_SECONDS} ${BACKEND_SEND_TIMEOUT_SECONDS} ${BACKEND_PROXY_BUFFERING}' \
        < /etc/nginx/templates/nginx-api.conf.template > /tmp/nginx/api.conf
fi

nginx -t -c /tmp/nginx/nginx.conf
# Replace the shell so nginx is PID 1 and receives container stop signals.
exec nginx -c /tmp/nginx/nginx.conf -g 'daemon off;' "$@"
