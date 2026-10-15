"""Exercise the shared shell entrypoint and Nginx with real HTTP/TLS servers.

Run: python3 -m unittest discover -s tests -v
Requires nginx, envsubst, and openssl; NGINX_BINARY can select a local binary.
Only fixed filesystem paths are rebased into a temporary directory. The real
PORT setting selects a free port without modifying the host's Nginx config.
"""

import base64
import hashlib
import concurrent.futures
import contextlib
import http.client
import http.server
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "node-frontend"
INDEX = b"<!doctype html><title>Frontend fixture</title>"
UNSET = object()


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class EchoHandler(http.server.BaseHTTPRequestHandler):
    def handle_request(self):
        if self.path.startswith("/api/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", "11")
            if self.path.endswith("?accel=no"):
                self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"first\n")
            self.wfile.flush()
            self.server.request_started.set()
            self.server.release_response.wait(timeout=5)
            self.wfile.write(b"last\n")
            return
        if self.path == "/api/websocket" and self.headers.get("Upgrade", "").lower() == "websocket":
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            accept = hashlib.sha1((self.headers["Sec-WebSocket-Key"] +
                                  "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            self.send_header("Sec-WebSocket-Accept", base64.b64encode(accept).decode())
            self.end_headers()
            self.wfile.flush()
            self.connection.settimeout(3)
            # An empty masked client frame; answer with a text frame through the tunnel.
            if self.rfile.read(6) == b"\x81\x80mask":
                self.wfile.write(b"\x81\x02OK")
            return
        if self.path == "/api/slow":
            self.server.request_started.set()
            self.server.release_response.wait(timeout=5)
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size).decode()
        status = 418 if self.path.startswith("/api/failure") else 200
        if self.path == "/api/redirect":
            status = 302
        response = json.dumps({
            "path": self.path,
            "method": self.command,
            "body": body,
            "headers": dict(self.headers),
        }).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "session=backend-cookie; HttpOnly; Path=/")
        self.send_header("X-Backend", "echo")
        if self.path == "/api/redirect":
            scheme = "https" if isinstance(self.connection, ssl.SSLSocket) else "http"
            self.send_header("Location", f"{scheme}://{self.headers['Host']}/api/example/items")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        try:
            self.wfile.write(response)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when a timeout test has already closed the proxy connection.

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def upstream(tls_context=None, request_started=None, release_response=None):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    server.request_started = request_started
    server.release_response = release_response
    if tls_context:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class Frontend:
    def __init__(self, binary, directory, ca_certificate=None, options=None, port=None):
        self.directory = Path(directory)
        self.port = free_port() if port is None else port
        self.runtime = self.directory / "runtime"
        self.site = self.directory / "site"
        templates = self.directory / "templates"
        logs = self.directory / "logs"
        for path in (self.site / "assets", templates, logs):
            path.mkdir(parents=True, exist_ok=True)
        (self.site / "index.html").write_bytes(INDEX)
        (self.site / ".build_sha").write_text("test-build-sha")
        (self.site / "assets/app.abcd1234.js").write_text("window.fixture = true;")
        for asset in ("app-Bx5kJ7_p.js", "theme.12345678.css", "logo.svg", "translations.json",
                      "app-short7.js", "app-allletters.js"):
            (self.site / "assets" / asset).write_text(asset)
        (self.site / "robots.txt").write_text("User-agent: *")
        mime_types = self.directory / "mime.types"
        mime_types.write_text("types { text/html html; text/plain txt; application/javascript js; }")
        replacements = {
            "/etc/nginx/templates": str(templates),
            "/etc/nginx/mime.types": str(mime_types),
            "/var/log/nginx": str(logs),
            "/tmp/nginx": str(self.runtime),
            "/app/site": str(self.site),
        }
        if ca_certificate:
            replacements["/etc/ssl/certs/ca-certificates.crt"] = str(ca_certificate)

        def rebase(text):
            for source, destination in replacements.items():
                text = text.replace(source, destination)
            return text

        for name in ("nginx.conf", "nginx-api.conf.template"):
            # nginx.conf remains the authoritative source name in the repo;
            # the Dockerfile places it in the template directory at build time.
            (templates / name).write_text(rebase((FRONTEND / name).read_text()))
        self.entrypoint = self.directory / "entrypoint.sh"
        self.entrypoint.write_text(rebase((FRONTEND / "entrypoint.sh").read_text()))
        bin_directory = self.directory / "bin"
        bin_directory.mkdir()
        (bin_directory / "nginx").symlink_to(binary)
        self.environment = dict(os.environ, PATH=f"{bin_directory}:{os.environ['PATH']}")
        for name in ("BACKEND_URL", "BACKEND_MAX_BODY_SIZE_MB", "BACKEND_CONNECT_TIMEOUT_SECONDS",
                     "BACKEND_READ_TIMEOUT_SECONDS", "BACKEND_SEND_TIMEOUT_SECONDS",
                     "BACKEND_STREAMING", "BACKEND_WEBSOCKETS"):
            self.environment.pop(name, None)
        self.environment["PORT"] = str(self.port)
        self.environment.update(options or {})
        # An unrestricted envsubst would destroy these actual Nginx variables.
        self.environment.update({name: "BROKEN_NGINX_VARIABLE" for name in (
            "uri", "host", "scheme", "proxy_host", "remote_addr", "http_host",
            "http_x_forwarded_proto", "proxy_add_x_forwarded_for", "icicle_forwarded_proto",
            "http_upgrade", "icicle_asset_cache", "icicle_upstream_upgrade",
            "icicle_upstream_connection",
        )})
        self.process = None
        self.output = None

    def start(self, backend=UNSET):
        environment = self.environment.copy()
        if backend is not UNSET:
            environment["BACKEND_URL"] = backend
        self.output = (self.directory / "startup.log").open("a+")
        self.process = subprocess.Popen(
            ["sh", str(self.entrypoint)], env=environment,
            stdout=self.output, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"Nginx startup failed:\n{self.diagnostics()}")
            try:
                if self.request("/healthcheck")[0] == 200:
                    return self
            except (ConnectionError, OSError):
                pass
            time.sleep(0.05)
        raise AssertionError(f"Nginx did not become ready:\n{self.diagnostics()}")

    def diagnostics(self):
        return (self.directory / "startup.log").read_text()

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGQUIT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                raise AssertionError("Nginx did not shut down gracefully on SIGQUIT")
        if self.output:
            self.output.close()


class FrontendPortTests(unittest.TestCase):
    """Exercise the real entrypoint without binding privileged or fixed ports."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="frontend-port-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        # The server is a sentinel: invalid PORT values must fail before it runs.
        # Real Nginx listening and container defaults are covered below/in images.
        nginx = directory / "nginx-stub"
        nginx.write_text("#!/bin/sh\nexit 0\n")
        nginx.chmod(0o755)
        self.frontend = Frontend(nginx, directory, port=5000)

    def run_entrypoint(self, port=UNSET):
        environment = self.frontend.environment.copy()
        if port is UNSET:
            environment.pop("PORT")
        else:
            environment["PORT"] = port
        return subprocess.run(
            ["sh", str(self.frontend.entrypoint)], env=environment,
            capture_output=True, text=True, timeout=5, check=False,
        )

    def test_invalid_ports_fail_before_rendering_or_starting(self):
        for port in ("", "0", "-1", "01", "05000", "65536", "999999999999999999999",
                     "1.5", "5000/tcp", "+5000", " 5000", "5000\n", "5000; return 200;"):
            with self.subTest(port=port):
                result = self.run_entrypoint(port)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("PORT", result.stderr)
                self.assertFalse(self.frontend.runtime.exists())

    @unittest.skipUnless(shutil.which("envsubst"), "Port rendering tests need envsubst")
    def test_default_override_and_valid_boundaries_render_both_listeners(self):
        for supplied, expected in ((UNSET, 5000), ("8000", 8000), ("1", 1), ("65535", 65535)):
            with self.subTest(port=supplied):
                result = self.run_entrypoint(supplied)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                config = (self.frontend.runtime / "nginx.conf").read_text()
                self.assertRegex(config, rf"listen\s+{expected};")
                self.assertRegex(config, rf"listen\s+\[::\]:{expected};")
                self.assertIn("$uri", config)
                self.assertNotIn("BROKEN_NGINX_VARIABLE", config)


class FrontendRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nginx = shutil.which(os.environ.get("NGINX_BINARY", "nginx"))
        missing = [name for name in ("envsubst", "openssl") if not shutil.which(name)]
        if not cls.nginx:
            missing.append("nginx (or set NGINX_BINARY)")
        if missing:
            raise RuntimeError("Install test prerequisites: " + ", ".join(missing))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="frontend-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def frontend(self, backend=UNSET, **kwargs):
        frontend = Frontend(self.nginx, self.directory, **kwargs)
        self.addCleanup(frontend.stop)
        return frontend.start(backend)

    def assert_static_behavior(self, frontend):
        status, headers, body = frontend.request("/healthcheck")
        self.assertEqual((status, body), (200, b"test-build-sha"))
        self.assertIn("no-store", headers["Cache-Control"])
        for path in ("/", "/index.html", "/example/details?tab=summary", "/example/missing.js"):
            with self.subTest(path=path):
                status, headers, body = frontend.request(path)
                self.assertEqual((status, body), (200, INDEX))
                self.assertIn("no-store", headers["Cache-Control"])
        status, headers, body = frontend.request("/assets/app.abcd1234.js")
        self.assertEqual((status, body), (200, b"window.fixture = true;"))
        cache_control = ", ".join(headers.get_all("Cache-Control"))
        self.assertIn("max-age=31536000", cache_control)
        self.assertIn("public", cache_control)
        self.assertIn("immutable", cache_control)
        for asset in ("app-Bx5kJ7_p.js", "theme.12345678.css"):
            status, headers, body = frontend.request(f"/assets/{asset}?v=current")
            self.assertEqual((status, body), (200, asset.encode()))
            self.assertIn("immutable", headers["Cache-Control"])
        for asset in ("logo.svg", "translations.json", "app-short7.js", "app-allletters.js"):
            status, headers, body = frontend.request(f"/assets/{asset}")
            self.assertEqual((status, body), (200, asset.encode()))
            self.assertEqual(headers["Cache-Control"], "no-cache")
            self.assertIn("ETag", headers)
            status, _, _ = frontend.request(f"/assets/{asset}", headers={"If-None-Match": headers["ETag"]})
            self.assertEqual(status, 304)
        self.assertEqual(frontend.request("/assets/missing.js")[0], 404)
        self.assertEqual(frontend.request("/assets/missing.abcd1234.js")[0], 404)
        self.assertEqual(frontend.request("/robots.txt")[2], b"User-agent: *")

    def test_without_backend_preserves_static_and_spa_behavior(self):
        frontend = self.frontend()
        self.assert_static_behavior(frontend)
        self.assertEqual(frontend.request("/api/example/items?x=1")[2], INDEX)

    def test_explicit_port_preserves_previous_8000_listener(self):
        frontend = self.frontend(port=8000)
        self.assert_static_behavior(frontend)

    def test_empty_backend_preserves_existing_api_fallback(self):
        frontend = self.frontend("")
        self.assertEqual(frontend.request("/api/example/items")[2], INDEX)

    def test_missing_build_sha_returns_empty_uncached_liveness_response(self):
        frontend = self.frontend()
        (frontend.site / ".build_sha").unlink()
        status, headers, body = frontend.request("/healthcheck")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["Content-Length"], "0")
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertEqual(frontend.request("/example/details")[2], INDEX)

    def test_proxy_preserves_full_path_query_and_static_behavior(self):
        with upstream() as port:
            frontend = self.frontend(f"http://127.0.0.1:{port}")
            self.assert_static_behavior(frontend)
            for path in (
                "/api/example/items",
                "/api/example/items?next=%2Fexample%3Fx%3D1&tag=one&tag=two&empty=",
                "/api/encoded%2Fsegment?q=a+b%20c",
                "/api/", "/api?probe=1", "/api/missing.js", "/api/assets/app.abcd1234.js",
            ):
                with self.subTest(path=path):
                    status, _, body = frontend.request(path)
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["path"], path)
            self.assertEqual(frontend.request("/apiary")[2], INDEX)

    def test_proxy_forwards_methods_bodies_credentials_and_response(self):
        with upstream() as port:
            frontend = self.frontend(f"http://127.0.0.1:{port}/")
            headers = {
                "Host": "frontend.example.test",
                "Authorization": "Bearer test-token",
                "Cookie": "session=browser-cookie",
                "Content-Type": "application/json",
                "X-Request-ID": "test-request",
                "X-Forwarded-For": "192.0.2.10",
            }
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(method=method):
                    # Force body buffering so the configured writable temp
                    # directory is exercised, as well as ordinary forwarding.
                    payload = json.dumps({"item": "test", "data": "x" * 20000})
                    status, response_headers, body = frontend.request(
                        "/api/example/items?source=frontend", method, payload, headers,
                    )
                    self.assertEqual(status, 200)
                    echo = json.loads(body)
                    self.assertEqual(echo["method"], method)
                    self.assertEqual(echo["body"], payload)
                    forwarded = {key.lower(): value for key, value in echo["headers"].items()}
                    self.assertEqual(forwarded["host"], f"127.0.0.1:{port}")
                    for header in ("Authorization", "Cookie", "Content-Type", "X-Request-ID"):
                        self.assertEqual(forwarded[header.lower()], headers[header])
                    self.assertEqual(forwarded["x-forwarded-for"], "192.0.2.10, 127.0.0.1")
                    self.assertEqual(forwarded["x-real-ip"], "127.0.0.1")
                    self.assertEqual(forwarded["x-forwarded-host"], "frontend.example.test")
                    self.assertEqual(forwarded["x-forwarded-proto"], "http")
                    self.assertEqual(response_headers["X-Backend"], "echo")
                    self.assertIn("session=backend-cookie", response_headers["Set-Cookie"])
            self.assertEqual(frontend.request("/api/failure")[0], 418)

    def test_default_upload_limit_and_opt_in_larger_uploads(self):
        with upstream() as port:
            frontend = self.frontend(f"http://127.0.0.1:{port}")
            generated = (frontend.runtime / "api.conf").read_text()
            for directive in ("client_max_body_size 1m;", "proxy_connect_timeout 60s;",
                              "proxy_read_timeout 60s;", "proxy_send_timeout 60s;", "proxy_buffering on;"):
                self.assertIn(directive, generated)
            boundary = "x" * (1024 * 1024)
            self.assertEqual(frontend.request("/api/upload", "POST", boundary)[0], 200)
            self.assertEqual(frontend.request("/api/upload", "POST", boundary + "x")[0], 413)
            frontend.stop()
            frontend.environment["BACKEND_MAX_BODY_SIZE_MB"] = "2"
            frontend.start(f"http://127.0.0.1:{port}")
            status, _, body = frontend.request("/api/upload", "POST", boundary + "x")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["body"], boundary + "x")
            self.assertEqual(frontend.request("/api/upload", "POST", boundary * 2 + "x")[0], 413)

    def test_configurable_upstream_timeout(self):
        started, release = threading.Event(), threading.Event()
        with upstream(request_started=started, release_response=release) as port:
            frontend = self.frontend(f"http://127.0.0.1:{port}", options={
                "BACKEND_CONNECT_TIMEOUT_SECONDS": "2", "BACKEND_READ_TIMEOUT_SECONDS": "1",
                "BACKEND_SEND_TIMEOUT_SECONDS": "3",
            })
            try:
                start = time.monotonic()
                self.assertEqual(frontend.request("/api/slow")[0], 504)
                self.assertTrue(started.is_set())
                self.assertLess(time.monotonic() - start, 2.5)
                config = (frontend.runtime / "api.conf").read_text()
                self.assertIn("proxy_connect_timeout 2s;", config)
                self.assertIn("proxy_send_timeout 3s;", config)
            finally:
                release.set()

    def test_streaming_opt_in_and_upstream_buffering_header(self):
        started, release = threading.Event(), threading.Event()
        with upstream(request_started=started, release_response=release) as port:
            frontend = self.frontend(f"http://127.0.0.1:{port}")
            for mode, path, should_stream in (("off", "/api/stream", False),
                                              ("on", "/api/stream", True),
                                              ("off", "/api/stream?accel=no", True)):
                with self.subTest(mode=mode, path=path):
                    frontend.stop()
                    frontend.environment["BACKEND_STREAMING"] = mode
                    frontend.start(f"http://127.0.0.1:{port}")
                    started.clear()
                    release.clear()
                    connection = http.client.HTTPConnection("127.0.0.1", frontend.port, timeout=3)
                    def first_chunk():
                        connection.request("GET", path)
                        response = connection.getresponse()
                        self.assertIsNone(response.headers.get("X-Accel-Buffering"))
                        return response, response.read(6)
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(first_chunk)
                        try:
                            self.assertTrue(started.wait(timeout=3))
                            if should_stream:
                                response, chunk = pending.result(timeout=1)
                                self.assertEqual(chunk, b"first\n")
                            else:
                                with self.assertRaises(concurrent.futures.TimeoutError):
                                    pending.result(timeout=0.2)
                        finally:
                            release.set()
                        response, chunk = pending.result(timeout=3)
                        self.assertEqual(chunk + response.read(), b"first\nlast\n")
                        connection.close()

    def test_websocket_upgrade_is_opt_in_and_restricted_to_websocket(self):
        with upstream() as port:
            frontend = self.frontend(f"http://127.0.0.1:{port}")
            for enabled in ("off", "on"):
                frontend.stop()
                frontend.environment["BACKEND_WEBSOCKETS"] = enabled
                frontend.start(f"http://127.0.0.1:{port}")
                for upgrade in (None, "h2c", "websocket, h2c", "websocket" if enabled == "off" else None):
                    with self.subTest(enabled=enabled, upgrade=upgrade):
                        headers = {"Connection": "Upgrade"}
                        if upgrade:
                            headers["Upgrade"] = upgrade
                        status, _, body = frontend.request("/api/check", headers=headers)
                        forwarded = {key.lower(): value for key, value in json.loads(body)["headers"].items()}
                        self.assertEqual(status, 200)
                        self.assertNotIn("upgrade", forwarded)
                        self.assertNotIn("connection", forwarded)
            with socket.create_connection(("127.0.0.1", frontend.port), timeout=3) as connection:
                connection.sendall((
                    "GET /api/websocket HTTP/1.1\r\nHost: localhost\r\n"
                    "Connection: Upgrade\r\nUpgrade: WebSocket\r\n"
                    "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
                ).encode())
                handshake = b""
                while b"\r\n\r\n" not in handshake:
                    handshake += connection.recv(4096)
                self.assertIn(b"101 Switching Protocols", handshake)
                self.assertIn(b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", handshake)
                connection.sendall(b"\x81\x80mask")
                self.assertEqual(connection.recv(4), b"\x81\x02OK")

    def test_invalid_runtime_options_fail_before_serving(self):
        frontend = Frontend(self.nginx, self.directory)
        bounds = {"BACKEND_MAX_BODY_SIZE_MB": 1024, "BACKEND_CONNECT_TIMEOUT_SECONDS": 75,
                  "BACKEND_READ_TIMEOUT_SECONDS": 3600, "BACKEND_SEND_TIMEOUT_SECONDS": 3600}
        invalid = [(name, value) for name, maximum in bounds.items()
                   for value in ("", "0", "-1", "01", "1.5", "1s", str(maximum + 1),
                                 "999999999999999999999", "1; return 200;")]
        invalid += [(name, value) for name in ("BACKEND_STREAMING", "BACKEND_WEBSOCKETS")
                    for value in ("", "true", "ON", "off; return 200;")]
        for name, value in invalid:
            with self.subTest(name=name, value=value):
                result = subprocess.run(
                    ["sh", str(frontend.entrypoint)], env=dict(frontend.environment, **{name: value}),
                    capture_output=True, text=True, timeout=5, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertFalse(frontend.runtime.exists())

    def test_forwarded_scheme_preserves_ingress_tls_and_rejects_invalid_values(self):
        with upstream() as port:
            frontend = self.frontend(f"http://127.0.0.1:{port}")
            for supplied, expected in (("https", "https"), ("http", "http"),
                                       ("garbage", "http"), ("https,http", "http")):
                with self.subTest(forwarded_proto=supplied):
                    _, _, body = frontend.request(
                        "/api/check", headers={"X-Forwarded-Proto": supplied},
                    )
                    self.assertEqual(json.loads(body)["headers"]["X-Forwarded-Proto"], expected)

    def test_upstream_redirect_keeps_browser_frontend_origin_behind_tls_ingress(self):
        with upstream() as port:
            frontend = self.frontend(f"http://127.0.0.1:{port}")
            status, headers, _ = frontend.request("/api/redirect", headers={
                "Host": "frontend.example.test", "X-Forwarded-Proto": "https",
            })
            self.assertEqual(status, 302)
            self.assertEqual(headers["Location"], "/api/example/items")

    def test_restart_with_new_backend_or_no_backend_needs_no_rebuild(self):
        with upstream() as first, upstream() as second:
            frontend = self.frontend(f"http://127.0.0.1:{first}")
            first_headers = json.loads(frontend.request("/api/check")[2])["headers"]
            self.assertEqual(first_headers["Host"], f"127.0.0.1:{first}")
            frontend.stop()
            frontend.start(f"http://127.0.0.1:{second}")
            second_headers = json.loads(frontend.request("/api/check")[2])["headers"]
            self.assertEqual(second_headers["Host"], f"127.0.0.1:{second}")
            frontend.stop()
            frontend.start()
            self.assertEqual(frontend.request("/api/check")[2], INDEX)

    def test_invalid_backend_urls_fail_before_serving(self):
        frontend = Frontend(self.nginx, self.directory)
        for backend in (
            "backend:3000", "ftp://backend:3000", "http://", "http://backend/api",
            "http://backend//", "http://backend?query=1", "http://backend#fragment",
            "http://user:password@backend", "http://backend;", "http://backend/$uri",
            "http://backend\n}", "http://backend:abc", "http://backend:0",
            "http://backend:65536",
        ):
            with self.subTest(backend=backend):
                result = subprocess.run(
                    ["sh", str(frontend.entrypoint)],
                    env=dict(frontend.environment, BACKEND_URL=backend),
                    capture_output=True, text=True, timeout=5, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BACKEND_URL", result.stderr)

    def test_invalid_generated_config_fails_nginx_validation(self):
        frontend = Frontend(self.nginx, self.directory)
        template = self.directory / "templates/nginx.conf"
        with template.open("a") as config:
            config.write("\ninvalid_test_directive;\n")
        result = subprocess.run(
            ["sh", str(frontend.entrypoint)], env=frontend.environment,
            capture_output=True, text=True, timeout=5, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid_test_directive", result.stderr)

    def test_entrypoint_execs_nginx_and_quits_gracefully(self):
        frontend = self.frontend()
        self.assertEqual(int((frontend.runtime / "nginx.pid").read_text()), frontend.process.pid)
        frontend.stop()
        self.assertEqual(frontend.process.returncode, 0)
        self.assertFalse((frontend.runtime / "nginx.pid").exists())

    def test_graceful_shutdown_completes_in_flight_api_request(self):
        started, release = threading.Event(), threading.Event()
        with upstream(request_started=started, release_response=release) as port:
            frontend = self.frontend(f"http://127.0.0.1:{port}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(frontend.request, "/api/slow")
                try:
                    self.assertTrue(started.wait(timeout=3), "Upstream request did not start")
                    frontend.process.send_signal(signal.SIGQUIT)
                    time.sleep(0.1)
                    self.assertIsNone(frontend.process.poll(), "Nginx dropped the active request")
                finally:
                    release.set()
                status, _, body = pending.result(timeout=5)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["path"], "/api/slow")
            self.assertEqual(frontend.process.wait(timeout=5), 0)

    def test_https_sni_and_certificate_verification(self):
        certificate = self.directory / "localhost.crt"
        key = self.directory / "localhost.key"
        config = self.directory / "openssl.cnf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n"
            "basicConstraints=critical,CA:TRUE\n"
        )
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(key), "-out", str(certificate), "-config", str(config),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        server_names = []
        context.set_servername_callback(lambda _socket, name, _context: server_names.append(name))
        with upstream(context) as port:
            frontend = self.frontend(f"https://localhost:{port}", ca_certificate=certificate)
            status, _, body = frontend.request("/api/example/items?tls=1")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["path"], "/api/example/items?tls=1")
            self.assertIn("localhost", server_names)
            status, headers, _ = frontend.request("/api/redirect", headers={
                "Host": "frontend.example.test", "X-Forwarded-Proto": "https",
            })
            self.assertEqual(status, 302)
            self.assertEqual(headers["Location"], "/api/example/items")
            frontend.stop()
            frontend.start(f"https://127.0.0.1:{port}")
            self.assertEqual(frontend.request("/api/example/items")[0], 502)


if __name__ == "__main__":
    unittest.main()
