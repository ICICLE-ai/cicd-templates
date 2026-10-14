import importlib.metadata
import importlib.util
import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

import icicle_smoke


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "ok",
            "version": os.environ.get("BUILD_VERSION") or importlib.metadata.version("icicle-smoke"),
            "build_sha": os.environ["BUILD_SHA"],
            "uid": os.getuid(),
            "python": list(sys.version_info[:2]),
            "module_path": icicle_smoke.__file__,
            "dev_dependency": importlib.util.find_spec("idna") is not None,
            "path": self.path,
        }).encode()
        self.send_response(200 if self.path == "/health" or self.path.startswith("/api/") else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def stop(_signal, _frame):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
with HTTPServer(("0.0.0.0", 8000), Handler) as server:
    server.serve_forever()
