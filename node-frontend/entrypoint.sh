#!/usr/bin/env sh
set -eu

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

# The image creates this directory for uid 999. Recreate it if /tmp is mounted
# as an empty writable volume; no writes to /etc/nginx are needed at runtime.
mkdir -p /tmp/nginx
cp /etc/nginx/templates/nginx.conf /tmp/nginx/nginx.conf
# Always replace the include, including after a restart with proxying disabled.
: > /tmp/nginx/api.conf
if [ -n "$BACKEND_URL" ]; then
    envsubst '${BACKEND_URL}' \
        < /etc/nginx/templates/nginx-api.conf.template > /tmp/nginx/api.conf
fi

nginx -t -c /tmp/nginx/nginx.conf
# Replace the shell so nginx is PID 1 and receives container stop signals.
exec nginx -c /tmp/nginx/nginx.conf -g 'daemon off;' "$@"
