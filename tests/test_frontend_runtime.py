"""Exercise the shared shell entrypoint and Nginx with real HTTP/TLS servers.

Run: python3 -m unittest discover -s tests -v
Requires nginx, envsubst, and openssl; NGINX_BINARY can select a local binary.
Only fixed filesystem paths and the listen port are rebased into a temporary
directory, so tests can run without root or modifying the host's Nginx config.
"""

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
        self.wfile.write(response)

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
    def __init__(self, binary, directory, ca_certificate=None):
        self.directory = Path(directory)
        self.port = free_port()
        self.runtime = self.directory / "runtime"
        self.site = self.directory / "site"
        templates = self.directory / "templates"
        logs = self.directory / "logs"
        for path in (self.site / "assets", templates, logs):
            path.mkdir(parents=True, exist_ok=True)
        (self.site / "index.html").write_bytes(INDEX)
        (self.site / ".build_sha").write_text("test-build-sha")
        (self.site / "assets/app.abcd1234.js").write_text("window.fixture = true;")
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
            return text.replace("listen      8000;", f"listen      {self.port};").replace(
                "listen      [::]:8000;", f"listen      [::]:{self.port};"
            )

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
        self.environment.pop("BACKEND_URL", None)
        # An unrestricted envsubst would destroy these actual Nginx variables.
        self.environment.update({name: "BROKEN_NGINX_VARIABLE" for name in (
            "uri", "host", "scheme", "proxy_host", "remote_addr", "http_host",
            "http_x_forwarded_proto", "proxy_add_x_forwarded_for", "icicle_forwarded_proto",
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
        for path in ("/", "/index.html", "/example/details?tab=summary"):
            with self.subTest(path=path):
                status, headers, body = frontend.request(path)
                self.assertEqual((status, body), (200, INDEX))
                self.assertIn("no-store", headers["Cache-Control"])
        status, headers, body = frontend.request("/assets/app.abcd1234.js")
        self.assertEqual((status, body), (200, b"window.fixture = true;"))
        cache_control = ", ".join(headers.get_all("Cache-Control"))
        self.assertIn("max-age=31536000", cache_control)
        self.assertIn("public, immutable", cache_control)
        self.assertEqual(frontend.request("/assets/missing.js")[0], 404)
        self.assertEqual(frontend.request("/robots.txt")[2], b"User-agent: *")

    def test_without_backend_preserves_static_and_spa_behavior(self):
        frontend = self.frontend()
        self.assert_static_behavior(frontend)
        self.assertEqual(frontend.request("/api/example/items?x=1")[2], INDEX)

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
                "/api/", "/api?probe=1", "/api/missing.js",
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
