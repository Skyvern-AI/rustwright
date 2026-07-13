from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen
import zipfile
import zlib


ONE_PIXEL_GIF_BYTES = bytes.fromhex(
    "47494638396101000100800000000000ffffff2c00000000010001000002024401003b"
)


def data_url(html: str) -> str:
    return f"data:text/html;charset=utf-8,{quote(html)}"


def unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve_forever_fast(server: ThreadingHTTPServer) -> None:
    server.serve_forever(poll_interval=0.01)


@contextmanager
def remote_debugging_chromium(browser_type):
    executable = browser_type.executable_path
    assert executable, "Chromium executable path is required for remote CDP parity checks"
    with tempfile.TemporaryDirectory(prefix="rustwright-cdp-", ignore_cleanup_errors=True) as directory:
        profile_dir = Path(directory) / "profile"
        profile_dir.mkdir()
        process = subprocess.Popen(
            [
                executable,
                "--remote-debugging-port=0",
                f"--user-data-dir={profile_dir}",
                "--headless=new",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        port_file = profile_dir / "DevToolsActivePort"
        endpoint = None
        deadline = time.time() + 10.0
        try:
            while time.time() < deadline:
                if process.poll() is not None:
                    stderr = process.stderr.read() if process.stderr else ""
                    raise AssertionError(f"remote Chromium exited before CDP endpoint was ready: {stderr}")
                if port_file.exists():
                    lines = port_file.read_text(encoding="utf-8").splitlines()
                    if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
                        endpoint = f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
                        break
                time.sleep(0.05)
            if endpoint is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                stderr = process.stderr.read() if process.stderr else ""
                raise AssertionError(f"remote Chromium did not expose a CDP endpoint: {stderr}")
            yield endpoint, process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def assert_near(value: int | float, expected: int | float, tolerance: int | float = 50) -> None:
    assert abs(float(value) - float(expected)) <= float(tolerance), f"{value!r} is not within {tolerance!r} of {expected!r}"


def png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\x89PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def png_pixel(data: bytes, x: int, y: int) -> tuple[int, ...]:
    width, height = png_size(data)
    assert 0 <= x < width
    assert 0 <= y < height
    bit_depth = data[24]
    color_type = data[25]
    assert bit_depth == 8
    channels = {2: 3, 6: 4}[color_type]
    chunks = []
    offset = 8
    while offset < len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        if kind == b"IDAT":
            chunks.append(data[offset + 8 : offset + 8 + length])
        offset += 12 + length
    raw = zlib.decompress(b"".join(chunks))
    stride = width * channels
    previous = bytearray(stride)
    cursor = 0
    rows = []
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 0xFF
            elif filter_type == 2:
                row[index] = (row[index] + up) & 0xFF
            elif filter_type == 3:
                row[index] = (row[index] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                predictor = left + up - upper_left
                pa = abs(predictor - left)
                pb = abs(predictor - up)
                pc = abs(predictor - upper_left)
                row[index] = (row[index] + (left if pa <= pb and pa <= pc else up if pb <= pc else upper_left)) & 0xFF
        rows.append(bytes(row))
        previous = row
    start = x * channels
    return tuple(rows[y][start : start + channels])


@contextmanager
def slow_body_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/delayed-script.js":
                time.sleep(0.2)
                body = b"window.__delayedScriptTag = 'loaded';"
                self.send_response(200, "OK")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/delayed-style.css":
                time.sleep(0.2)
                body = b"#box { background-color: rgb(4, 5, 6); }"
                self.send_response(200, "OK")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "text/css")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/slow-image-page"):
                image_path = f"/slow-image-goto.gif?{self.path.split('?', 1)[1]}" if "?" in self.path else "/slow-image-goto.gif"
                body = f"<title>Slow Image</title><img id='slow' src='{image_path}'>".encode("utf-8")
                self.send_response(200, "OK")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/networkidle-page":
                body = b"""
                <title>Network Idle</title>
                <script>
                fetch('/slow-fetch').then(response => response.text()).then(() => {
                  document.body.dataset.fetchDone = 'yes';
                });
                </script>
                """
                self.send_response(200, "OK")
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/slow-fetch":
                time.sleep(0.25)
                body = b"done"
                self.send_response(200, "OK")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path == "/slow-fetch-timeout":
                time.sleep(1.5)
                body = b"done"
                self.send_response(200, "OK")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.path.startswith("/slow-image"):
                time.sleep(0.5 if "domcontentloaded" in self.path else 0.25)
                body = ONE_PIXEL_GIF_BYTES
                self.send_response(200, "OK")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "image/gif")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            body = b"<title>Slow Body</title><script>window.__slowBodyParsed = true</script>"
            self.send_response(200, "OK")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.flush()
                time.sleep(0.25)
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=_serve_forever_fast, args=(server,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@contextmanager
def cdp_discovery_status_server(status: int = 500):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = f"status {status}".encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=_serve_forever_fast, args=(server,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@contextmanager
def cdp_websocket_status_server(status: int = 502, reason: str = "Bad Gateway"):
    body = b"<html><head><title>502 Bad Gateway</title></head></html>"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(status, reason)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=_serve_forever_fast, args=(server,), daemon=True)
    thread.start()
    try:
        yield f"ws://127.0.0.1:{server.server_port}/devtools/browser/bad", body.decode("utf-8")
    finally:
        server.shutdown()
        thread.join(timeout=2)


@contextmanager
def https_case_server():
    with tempfile.TemporaryDirectory(prefix="rustwright-https-case-") as tmp:
        cert_path = Path(tmp) / "cert.pem"
        key_path = Path(tmp) / "key.pem"
        proc = subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=IP:127.0.0.1,DNS:localhost",
            ],
            text=True,
            capture_output=True,
            timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"openssl could not create a self-signed certificate: {proc.stderr or proc.stdout}")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                body = json.dumps({"secure": True, "path": self.path}).encode("utf-8")
                self.send_response(200, "OK")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=_serve_forever_fast, args=(server,), daemon=True)
        thread.start()
        try:
            yield f"https://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=2)


@contextmanager
def mtls_case_server():
    with tempfile.TemporaryDirectory(prefix="rustwright-mtls-case-") as tmp:
        root = Path(tmp)
        ca_cert = root / "ca.pem"
        ca_key = root / "ca-key.pem"
        server_cert = root / "server.pem"
        server_key = root / "server-key.pem"
        server_csr = root / "server.csr"
        client_cert = root / "client.pem"
        client_key = root / "client-key.pem"
        client_csr = root / "client.csr"
        commands = [
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(ca_key),
                "-out",
                str(ca_cert),
                "-days",
                "1",
                "-subj",
                "/CN=Rustwright Test CA",
            ],
            [
                "openssl",
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(server_key),
                "-out",
                str(server_csr),
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=IP:127.0.0.1,DNS:localhost",
            ],
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(server_csr),
                "-CA",
                str(ca_cert),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(server_cert),
                "-days",
                "1",
                "-copy_extensions",
                "copy",
            ],
            [
                "openssl",
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(client_key),
                "-out",
                str(client_csr),
                "-subj",
                "/CN=Rustwright Client",
            ],
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(client_csr),
                "-CA",
                str(ca_cert),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(client_cert),
                "-days",
                "1",
            ],
        ]
        for command in commands:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=10)
            if proc.returncode != 0:
                raise RuntimeError(f"openssl could not create mTLS certificates: {proc.stderr or proc.stdout}")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                peer = self.connection.getpeercert()
                subject = []
                for group in peer.get("subject", []) if peer else []:
                    subject.extend([f"{key}={value}" for key, value in group])
                body = json.dumps({"client_subject": subject, "path": self.path}).encode("utf-8")
                self.send_response(200, "OK")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(server_cert), keyfile=str(server_key))
        context.load_verify_locations(cafile=str(ca_cert))
        context.verify_mode = ssl.CERT_REQUIRED
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=_serve_forever_fast, args=(server,), daemon=True)
        thread.start()
        try:
            origin = f"https://127.0.0.1:{server.server_port}"
            yield {
                "origin": origin,
                "url": f"{origin}/secure",
                "client_cert": str(client_cert),
                "client_key": str(client_key),
            }
        finally:
            server.shutdown()
            thread.join(timeout=2)


@contextmanager
def http_proxy_case_server():
    seen: list[dict[str, str | None]] = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _record_and_send_json(self, entry):
            seen.append(entry)
            body = json.dumps({"proxied": True, **entry}).encode("utf-8")
            self.send_response(200, "OK")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            entry = {"url": self.path, "host": self.headers.get("Host")}
            self._record_and_send_json(entry)

        def do_CONNECT(self):
            self.connection.settimeout(5)
            self.send_response(200, "Connection Established")
            self.end_headers()
            request_line = self.rfile.readline(65536).decode("iso-8859-1").strip()
            headers: dict[str, str] = {}
            while True:
                line = self.rfile.readline(65536).decode("iso-8859-1")
                if line in ("\r\n", "\n", ""):
                    break
                name, _, value = line.partition(":")
                headers[name.lower()] = value.strip()
            parts = request_line.split(" ", 2)
            if len(parts) != 3:
                return
            _method, path, _ = parts
            host = headers.get("host", self.path.rsplit(":", 1)[0])
            url = path if path.startswith(("http://", "https://")) else f"http://{host}{path}"
            entry = {"url": url, "host": host}
            seen.append(entry)
            body = json.dumps({"proxied": True, **entry}).encode("utf-8")
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )
            self.connection.sendall(response)
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=_serve_forever_fast, args=(server,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        thread.join(timeout=2)


def proxy_seen_for_host(seen: list[dict[str, str | None]], host: str) -> list[dict[str, str | None]]:
    return [entry for entry in seen if entry.get("host") == host]


def proxy_seen_for_url(seen: list[dict[str, str | None]], url: str) -> list[dict[str, str | None]]:
    return [entry for entry in seen if entry.get("url") == url]


@contextmanager
def authenticated_http_proxy_case_server(username: str = "user", password: str = "pass"):
    expected = "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    seen: list[dict[str, str | None]] = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            entry = {
                "url": self.path,
                "host": self.headers.get("Host"),
                "proxy_authorization": self.headers.get("Proxy-Authorization"),
            }
            seen.append(entry)
            if entry["proxy_authorization"] != expected:
                body = b"proxy auth required"
                self.send_response(407, "Proxy Authentication Required")
                self.send_header("Proxy-Authenticate", 'Basic realm="rustwright-proxy"')
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            body = json.dumps({"proxied": True, **entry}).encode("utf-8")
            self.send_response(200, "OK")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=_serve_forever_fast, args=(server,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen, expected
    finally:
        server.shutdown()
        thread.join(timeout=2)


@contextmanager
def header_case_server():
    class HeaderCaseServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            _, exc, _ = sys.exc_info()
            if isinstance(exc, BrokenPipeError):
                return
            super().handle_error(request, client_address)

    class Handler(BaseHTTPRequestHandler):
        auth_challenge_count = 0

        def log_message(self, *_):
            pass

        def _send_json(self, payload, *, status=200, reason="OK", extra_headers=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status, reason)
            self.send_header("Content-Type", "application/json")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_text_body(self):
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            return self.rfile.read(content_length).decode("utf-8") if content_length else ""

        def _send_method_echo(self):
            parsed = urlparse(self.path)
            if parsed.path != "/method":
                self.send_error(404)
                return
            self._send_json(
                {
                    "method": self.command,
                    "content_type": self.headers.get("Content-Type"),
                    "body": self._read_text_body(),
                }
            )

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/set-cookies":
                body = b"cookies set"
                self.send_response(200, "OK")
                self.send_header("Set-Cookie", "first=one; Path=/")
                self.send_header("Set-Cookie", "second=two; Path=/")
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/cookie-echo":
                self._send_json({"cookie": self.headers.get("Cookie", "")})
                return
            if parsed.path == "/query":
                self._send_json({"path": parsed.path, "query": parse_qs(parsed.query)})
                return
            if parsed.path == "/echo-headers":
                self._send_json(
                    {
                        "x-route-header": self.headers.get("X-Route-Header"),
                        "x-route-fetch": self.headers.get("X-Route-Fetch"),
                        "x-extra": self.headers.get("X-Extra"),
                        "x-context": self.headers.get("X-Context"),
                        "x-page": self.headers.get("X-Page"),
                        "x-shared": self.headers.get("X-Shared"),
                        "x-after": self.headers.get("X-After"),
                        "referer": self.headers.get("Referer"),
                        "user-agent": self.headers.get("User-Agent"),
                        "accept-language": self.headers.get("Accept-Language"),
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                return
            if parsed.path == "/basic-auth-challenge":
                type(self).auth_challenge_count += 1
                expected = "Basic " + base64.b64encode(b"user:pass").decode("ascii")
                if self.headers.get("Authorization") != expected:
                    self.send_response(401, "Unauthorized")
                    self.send_header("WWW-Authenticate", 'Basic realm="rustwright-parity"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._send_json(
                    {
                        "authorization": self.headers.get("Authorization"),
                        "attempts": type(self).auth_challenge_count,
                    }
                )
                return
            if parsed.path == "/redirect-one":
                self.send_response(302, "Found")
                self.send_header("Location", "/headers")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/redirect-hop-one":
                self.send_response(302, "Found")
                self.send_header("Location", "/redirect-hop-two")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/redirect-hop-two":
                self.send_response(302, "Found")
                self.send_header("Location", "/headers")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed.path == "/headers":
                body = b'{"ok":true}'
                self.send_response(200, "OK")
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Mixed-Case", "ResponseValue")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/slow-headers":
                time.sleep(0.1)
                self._send_json({"ok": True, "slow": True})
                return
            if parsed.path == "/fetch-json-page":
                body = b"""
                <button id="fetch-json" onclick="fetch('/headers').then(response => response.json()).then(data => {
                  document.body.dataset.fetchOk = String(data.ok);
                })">Fetch JSON</button>
                """
                self.send_response(200, "OK")
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/download":
                body = b"download-body"
                self.send_response(200, "OK")
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Disposition", 'attachment; filename="report.txt"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/download-slow":
                body = b"slow-download-body"
                self.send_response(200, "OK")
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Disposition", 'attachment; filename="slow-report.txt"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[:4])
                self.wfile.flush()
                time.sleep(0.3)
                self.wfile.write(body[4:])
                return
            if parsed.path == "/download-large-slow":
                total_bytes = 2 * 1024 * 1024
                chunk = b"x" * (128 * 1024)
                self.send_response(200, "OK")
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="large-report.bin"')
                self.send_header("Content-Length", str(total_bytes))
                self.end_headers()
                written = 0
                while written < total_bytes:
                    to_write = min(len(chunk), total_bytes - written)
                    self.wfile.write(chunk[:to_write])
                    self.wfile.flush()
                    written += to_write
                    time.sleep(0.05)
                return
            if parsed.path == "/protected-download":
                cookie = self.headers.get("Cookie", "")
                if "first=one" not in cookie or "second=two" not in cookie:
                    self._send_json({"cookie": cookie}, status=403, reason="Forbidden")
                    return
                body = b"protected-download-body"
                self.send_response(200, "OK")
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Disposition", 'attachment; filename="protected.txt"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/frame-page":
                body = b"""
                <iframe name="child" src="/frame-child"></iframe>
                """
                self.send_response(200, "OK")
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/frame-child":
                body = b"""
                <button id="fetch" onclick="fetch('/headers').then(response => response.json()).then(data => {
                  document.body.dataset.fetchOk = String(data.ok);
                })">Fetch in frame</button>
                """
                self.send_response(200, "OK")
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/frame-child-auto":
                body = b"""
                <script>
                fetch('/headers').then(response => response.json()).then(data => {
                  document.body.dataset.fetchOk = String(data.ok);
                });
                </script>
                <main>auto frame fetch</main>
                """
                self.send_response(200, "OK")
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/csp":
                body = b"<main>CSP page</main>"
                self.send_response(200, "OK")
                self.send_header("Content-Security-Policy", "script-src 'self'")
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/scripted":
                body = b"""
                <body>
                  <main id="probe">Initial</main>
                  <script>
                  document.body.dataset.script = 'ran';
                  document.getElementById('probe').textContent = 'Ran';
                  </script>
                </body>
                """
                self.send_response(200, "OK")
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/bad":
                body = b"bad"
                self.send_response(500, "Internal Server Error")
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = b"""
            <button id="go" onclick="fetch('/headers', {
              headers: { 'X-Client-Mixed': 'ReqValue' }
            })">Go</button>
            """
            self.send_response(200, "OK")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/echo":
                content_length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(content_length).decode("utf-8")
                self._send_json(
                    {
                        "content_type": self.headers.get("Content-Type"),
                        "x_test": self.headers.get("X-Test-Header"),
                        "body": raw_body,
                    },
                    status=202,
                    reason="Accepted",
                )
                return
            if parsed.path == "/multipart":
                content_length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(content_length).decode("utf-8", errors="replace")
                self._send_json(
                    {
                        "content_type": self.headers.get("Content-Type"),
                        "body": raw_body,
                    }
                )
                return
            self.send_error(404)

        def do_HEAD(self):
            parsed = urlparse(self.path)
            if parsed.path == "/method":
                self.send_response(204, "No Content")
                self.send_header("X-Method", "HEAD")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_error(404)

        def do_PUT(self):
            self._send_method_echo()

        def do_PATCH(self):
            self._send_method_echo()

        def do_DELETE(self):
            self._send_method_echo()

    server = HeaderCaseServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=_serve_forever_fast, args=(server,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@contextmanager
def service_worker_case_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/service-worker-page":
                body = b"""
                <script>
                window.__registrationPromise = navigator.serviceWorker.register('/sw.js')
                  .then(() => navigator.serviceWorker.ready)
                  .then(() => { document.body.dataset.sw = 'ready'; });
                </script>
                <body>service worker page</body>
                """
                self.send_response(200, "OK")
                self.send_header("Content-Type", "text/html")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/sw.js":
                body = b"""
                self.__parityServiceWorkerValue = 73;
                self.addEventListener('install', event => {
                  self.skipWaiting();
                });
                self.addEventListener('activate', event => {
                  event.waitUntil(self.clients.claim());
                });
                self.addEventListener('fetch', event => {
                  if (new URL(event.request.url).pathname === '/sw-controlled') {
                    event.respondWith(new Response(JSON.stringify({ source: 'service-worker' }), {
                      status: 203,
                      headers: { 'content-type': 'application/json', 'x-sw': 'yes' },
                    }));
                  }
                });
                """
                self.send_response(200, "OK")
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Service-Worker-Allowed", "/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = b"missing"
            self.send_response(404, "Not Found")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=_serve_forever_fast, args=(server,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@contextmanager
def websocket_echo_server():
    stop = threading.Event()
    ready = threading.Event()
    connections = []
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def read_exact(conn, size):
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def read_ws_message(conn):
        header = read_exact(conn, 2)
        if header is None:
            return None
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            extended = read_exact(conn, 2)
            if extended is None:
                return None
            length = int.from_bytes(extended, "big")
        elif length == 127:
            extended = read_exact(conn, 8)
            if extended is None:
                return None
            length = int.from_bytes(extended, "big")
        mask = read_exact(conn, 4) if masked else b""
        payload = read_exact(conn, length)
        if payload is None:
            return None
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 8:
            return None
        return payload.decode("utf-8")

    def send_ws_text(conn, message):
        payload = message.encode("utf-8")
        if len(payload) > 125:
            raise ValueError("test websocket payload is too large")
        conn.sendall(bytes([0x81, len(payload)]) + payload)

    def handle_connection(conn):
        with conn:
            conn.settimeout(2)
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk
            text = request.decode("latin1")
            path = text.split(" ", 2)[1]
            key = ""
            for line in text.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
                    break
            accept = base64.b64encode(hashlib.sha1((key + magic).encode("ascii")).digest()).decode("ascii")
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            connections.append(("path", path))
            while not stop.is_set():
                try:
                    message = read_ws_message(conn)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if message is None:
                    return
                connections.append(("message", path, message))
                send_ws_text(conn, f"echo:{message}")

    def run_server():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen()
            server.settimeout(0.1)
            connections.append(("port", server.getsockname()[1]))
            ready.set()
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=handle_connection, args=(conn,), daemon=True).start()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    ready.wait(timeout=2)
    port_items = [item for item in connections if item[0] == "port"]
    if not port_items:
        raise RuntimeError("websocket parity server did not start")
    try:
        yield f"ws://127.0.0.1:{port_items[0][1]}/socket", connections
    finally:
        stop.set()
        try:
            with socket.create_connection(("127.0.0.1", port_items[0][1]), timeout=1):
                pass
        except OSError:
            pass
        thread.join(timeout=2)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def goto_and_title(page):
    page.goto(data_url("<title>Bench</title><main>ready</main>"))
    assert page.title() == "Bench"


@case
def set_content_and_read_text(page):
    page.set_content("<section><h1>Dashboard</h1><p id='status'>Ready</p></section>")
    assert page.text_content("#status") == "Ready"


@case
def evaluate_json(page):
    page.set_content("<div></div>")
    assert page.evaluate("() => ({sum: 1 + 2, ok: true})") == {"sum": 3, "ok": True}
    nan_value = page.evaluate("() => Number.NaN")
    assert isinstance(nan_value, float) and math.isnan(nan_value)
    assert page.evaluate("() => Infinity") == float("inf")
    assert page.evaluate("() => -Infinity") == float("-inf")
    neg_zero = page.evaluate("() => -0")
    assert neg_zero == 0 and math.copysign(1, neg_zero) == -1
    values = page.evaluate(
        "() => ({ nan: Number.NaN, inf: Infinity, negInf: -Infinity, negZero: -0, nested: [Number.NaN, -0] })"
    )
    assert math.isnan(values["nan"])
    assert values["inf"] == float("inf")
    assert values["negInf"] == float("-inf")
    assert values["negZero"] == 0 and math.copysign(1, values["negZero"]) == -1
    assert math.isnan(values["nested"][0])
    assert values["nested"][1] == 0 and math.copysign(1, values["nested"][1]) == -1
    serialized = page.evaluate(
        """() => ({
        date: new Date('2020-01-02T03:04:05.678Z'),
        regex: /abc/gi,
        url: new URL('https://example.com/a?b=1'),
        big: 42n,
        error: new TypeError('boom'),
        symbol: Symbol('ignored'),
        array: [new Date('2021-02-03T04:05:06Z'), /x/m, -3n, new Error('nested')]
        })"""
    )
    error_value = serialized["error"]
    nested_error = serialized["array"][3]
    assert serialized == {
        "date": datetime(2020, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc),
        "regex": {"r": {"p": "abc", "f": "gi"}},
        "url": urlparse("https://example.com/a?b=1"),
        "big": 42,
        "error": error_value,
        "symbol": None,
        "array": [
            datetime(2021, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
            {"r": {"p": "x", "f": "m"}},
            -3,
            nested_error,
        ],
    }
    assert str(error_value) == "boom"
    assert getattr(error_value, "name", None) == "TypeError"
    assert getattr(error_value, "message", None) == "boom"
    assert "TypeError: boom" in getattr(error_value, "stack", "")
    assert str(nested_error) == "nested"


@case
def click_button(page):
    page.set_content("<button id='go' onclick=\"document.body.dataset.clicked='yes'\">Go</button>")
    page.locator("#go").click(trial=True)
    page.click("#go", trial=True)
    page.locator("#go").dblclick(trial=True)
    assert page.evaluate("document.body.dataset.clicked || null") is None
    page.click("#go")
    assert page.evaluate("document.body.dataset.clicked") == "yes"


@case
def fill_input(page):
    page.set_content(
        """
        <input id='email'>
        <input id='hidden-email' style='display:none' value='hidden'>
        <input id='disabled-email' disabled value='disabled'>
        <input id='readonly-email' readonly value='readonly'>
        <input id='checkbox-email' type='checkbox' value='old'>
        <input id='number-code' type='number'>
        <input id='date-code' type='date'>
        <select id='plan'><option value='basic'>Basic</option><option value='pro' selected>Pro</option></select>
        <button id='button-email' value='button-value'>Button</button>
        <div id='editable-email' contenteditable>editable</div>
        <div id='plain-email'>plain</div>
        """
    )

    def expect_error(callback, *substrings):
        try:
            callback()
        except Exception as exc:
            text = str(exc)
            for substring in substrings:
                assert substring in text
        else:
            raise AssertionError(f"expected error containing {substrings!r}")

    page.fill("#email", "user@example.com")
    assert page.evaluate("document.querySelector('#email').value") == "user@example.com"
    expect_error(
        lambda: page.locator("#plain-email").fill("normal plain", timeout=500),
        "not an <input>",
        "role allowing",
    )
    expect_error(lambda: page.locator("#plan").fill("normal select", timeout=500), "[contenteditable] element")
    expect_error(lambda: page.locator("#checkbox-email").fill("checked", timeout=500), 'Input of type "checkbox"')
    expect_error(lambda: page.locator("#checkbox-email").clear(timeout=500), 'Input of type "checkbox"')
    expect_error(lambda: page.locator("#number-code").fill("abc", timeout=500), "Cannot type text into input[type=number]")
    expect_error(lambda: page.locator("#date-code").fill("1", timeout=500), "Malformed value")
    assert page.locator("#plan").input_value() == "pro"
    expect_error(lambda: page.locator("#plain-email").input_value(timeout=500), "Node is not an <input>")
    expect_error(lambda: page.locator("#button-email").input_value(timeout=500), "Node is not an <input>")
    page.locator("#email").clear(force=True)
    assert page.evaluate("document.querySelector('#email').value") == ""
    page.locator("#email").fill("forced@example.com", force=True)
    assert page.evaluate("document.querySelector('#email').value") == "forced@example.com"
    page.locator("#hidden-email").fill("forced-hidden", force=True)
    assert page.evaluate("document.querySelector('#hidden-email').value") == "hidden"
    page.fill("#hidden-email", "page-forced-hidden", force=True)
    assert page.evaluate("document.querySelector('#hidden-email').value") == "hidden"
    page.locator("#disabled-email").fill("forced-disabled", force=True)
    assert page.evaluate("document.querySelector('#disabled-email').value") == "disabled"
    page.locator("#readonly-email").fill("forced-readonly", force=True)
    assert page.evaluate("document.querySelector('#readonly-email').value") == "readonly"
    page.locator("#editable-email").fill("forced editable", force=True)
    assert page.text_content("#editable-email") == "forced editable"
    expect_error(lambda: page.locator("#plain-email").fill("forced plain", force=True), "[contenteditable] element")
    expect_error(lambda: page.locator("#checkbox-email").fill("forced checkbox", force=True), 'Input of type "checkbox"')


@case
def type_input(page):
    page.set_content("<input id='message'>")
    page.type("#message", "hello")
    assert page.evaluate("document.querySelector('#message').value") == "hello"


@case
def locator_count(page):
    page.set_content("<ul>" + "".join(f"<li>Item {i}</li>" for i in range(25)) + "</ul>")
    assert page.locator("li").count() == 25


@case
def locator_nth_text(page):
    page.set_content("<ul><li>first</li><li>second</li><li>third</li></ul>")
    assert page.locator("li").nth(2).inner_text() == "third"


@case
def role_locator(page):
    page.set_content("<button aria-label='Save record'>Save</button>")
    assert page.get_by_role("button", name="Save").is_visible()


@case
def text_locator(page):
    page.set_content("<article><p>Quarterly revenue report</p></article>")
    assert page.get_by_text("revenue").is_visible()


@case
def wait_for_selector(page):
    page.set_content("<main id='root'><div id='hidden' style='display:none'>Hidden</div><div id='gone'>Gone</div><iframe srcdoc='<span id=\"frame-hidden\" style=\"display:none\">Hidden</span>'></iframe></main>")
    page.evaluate(
        """() => setTimeout(() => {
        const node = document.createElement('div');
        node.id = 'done';
        node.textContent = 'Done';
        document.querySelector('#root').appendChild(node);
        document.querySelector('#gone').remove();
        }, 20)"""
    )
    assert page.wait_for_selector("#done", timeout=2_000).text_content() == "Done"
    assert page.wait_for_selector("#hidden", state="hidden", timeout=500) is None
    assert page.wait_for_selector("#missing", state="hidden", timeout=500) is None
    assert page.wait_for_selector("#missing", state="detached", timeout=500) is None
    assert page.locator("#hidden").wait_for(state="hidden", timeout=500) is None
    assert page.locator("#missing").wait_for(state="detached", timeout=500) is None
    root = page.query_selector("#root")
    assert root.wait_for_selector("#hidden", state="hidden", timeout=500) is None
    assert root.wait_for_selector("#gone", state="detached", timeout=500) is None
    assert root.wait_for_selector("#missing", state="hidden", timeout=500) is None
    frame = page.frames[1]
    assert frame.wait_for_selector("#frame-hidden", state="hidden", timeout=500) is None

    def expect_state_error(callback, method):
        try:
            callback()
        except Exception as exc:
            assert f"{method}: state: expected one of (attached|detached|visible|hidden)" in str(exc)
        else:
            raise AssertionError(f"{method} invalid state unexpectedly succeeded")

    expect_state_error(lambda: page.wait_for_selector("#done", state="enabled", timeout=500), "Page.wait_for_selector")
    expect_state_error(lambda: page.locator("#done").wait_for(state="enabled", timeout=500), "Locator.wait_for")
    expect_state_error(lambda: root.wait_for_selector("#hidden", state="enabled", timeout=500), "ElementHandle.wait_for_selector")
    expect_state_error(lambda: frame.wait_for_selector("#frame-hidden", state="enabled", timeout=500), "Frame.wait_for_selector")


@case
def wait_for_selector_timeout_messages_match_playwright(page):
    page.set_content("<main id='root'><div>Ready</div></main>")
    root = page.query_selector("#root")
    assert root is not None

    checks = [
        (lambda: page.wait_for_selector("#missing", timeout=120), "Page.wait_for_selector: Timeout 120ms exceeded."),
        (lambda: page.main_frame.wait_for_selector("#missing", timeout=120), "Frame.wait_for_selector: Timeout 120ms exceeded."),
        (lambda: page.locator("#missing").wait_for(timeout=120), "Locator.wait_for: Timeout 120ms exceeded."),
        (lambda: root.wait_for_selector("#missing", timeout=120), "ElementHandle.wait_for_selector: Timeout 120ms exceeded."),
    ]
    for operation, expected in checks:
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
        else:
            raise AssertionError(f"wait unexpectedly succeeded for {expected}")


@case
def wait_for_selector_strict_violations(page):
    page.set_content(
        """
        <main id='root'>
          <button style='display:none'>A</button>
          <button style='display:none'>B</button>
          <iframe srcdoc='<span style="display:none">One</span><span style="display:none">Two</span>'></iframe>
        </main>
        """
    )
    root = page.query_selector("#root")
    frame = page.frames[1]

    def expect_strict_error(callback):
        try:
            callback()
        except Exception as exc:
            assert "strict mode violation" in str(exc)
        else:
            raise AssertionError("strict wait unexpectedly succeeded")

    for state in ("attached", "visible", "hidden", "detached"):
        expect_strict_error(lambda state=state: page.wait_for_selector("button", state=state, strict=True, timeout=500))
        expect_strict_error(lambda state=state: frame.wait_for_selector("span", state=state, strict=True, timeout=500))
        expect_strict_error(lambda state=state: page.locator("button").wait_for(state=state, timeout=500))
        expect_strict_error(lambda state=state: root.wait_for_selector("button", state=state, strict=True, timeout=500))

    assert root.wait_for_selector("button", state="attached", timeout=500).text_content() == "A"
    assert page.locator("button").first.wait_for(state="attached", timeout=500) is None


@case
def screenshot(page):
    page.set_content("<h1>Screenshot</h1>")
    assert page.screenshot().startswith(b"\x89PNG")


@case
def webvoyager_checkout_workflow(page):
    page.set_content(
        """
        <main>
          <label>Search catalog <input id="query" placeholder="Search catalog"></label>
          <label>Category
            <select id="category">
              <option value="all">All</option>
              <option value="travel">Travel</option>
              <option value="office">Office</option>
            </select>
          </label>
          <label>Maximum price <input id="max-price" type="number" value="999"></label>
          <button id="apply">Apply filters</button>
          <section id="results" aria-label="Results"></section>
          <aside>
            <output id="cart-count" aria-label="Cart count">0</output>
            <output id="cart-total" aria-label="Cart total">$0</output>
          </aside>
          <label>Email <input id="email" type="email"></label>
          <button id="place-order">Place order</button>
          <strong id="confirmation"></strong>
        </main>
        <script>
        const products = [
          { name: 'Noise cancelling headphones', category: 'travel', price: 129 },
          { name: 'Travel adapter', category: 'travel', price: 29 },
          { name: 'Desk lamp', category: 'office', price: 64 },
          { name: 'Notebook set', category: 'office', price: 18 }
        ];
        const cart = [];
        function render() {
          const query = document.querySelector('#query').value.toLowerCase();
          const category = document.querySelector('#category').value;
          const max = Number(document.querySelector('#max-price').value || 999);
          const results = products.filter(product =>
            product.name.toLowerCase().includes(query) &&
            (category === 'all' || product.category === category) &&
            product.price <= max
          );
          document.querySelector('#results').innerHTML = results.map(product => `
            <article data-testid="result">
              <h2>${product.name}</h2>
              <p>$${product.price}</p>
              <button aria-label="Add ${product.name}" data-name="${product.name}">Add</button>
            </article>
          `).join('');
        }
        document.querySelector('#apply').addEventListener('click', render);
        document.querySelector('#results').addEventListener('click', event => {
          const button = event.target.closest('button[data-name]');
          if (!button) return;
          const product = products.find(item => item.name === button.dataset.name);
          cart.push(product);
          document.querySelector('#cart-count').textContent = String(cart.length);
          document.querySelector('#cart-total').textContent = '$' + cart.reduce((total, item) => total + item.price, 0);
        });
        document.querySelector('#place-order').addEventListener('click', () => {
          document.querySelector('#confirmation').textContent =
            `Confirmed ${cart.length} items for ${document.querySelector('#email').value}`;
        });
        render();
        </script>
        """
    )
    page.mouse.move(500, 500)
    page.evaluate("window.events = []")

    page.get_by_placeholder("Search catalog").fill("travel")
    page.get_by_role("button", name="Apply filters").click()
    assert page.locator("[data-testid='result']").count() == 1
    page.get_by_role("button", name="Add Travel adapter").click()

    page.get_by_placeholder("Search catalog").fill("noise")
    page.get_by_label("Category").select_option("travel")
    page.get_by_label("Maximum price").fill("150")
    page.get_by_role("button", name="Apply filters").click()
    assert page.locator("[data-testid='result']").count() == 1
    page.get_by_role("button", name="Add Noise cancelling headphones").click()

    page.get_by_label("Email").fill("ada@example.com")
    page.get_by_role("button", name="Place order").click()
    assert page.locator("#cart-count").text_content() == "2"
    assert page.locator("#cart-total").text_content() == "$158"
    assert page.locator("#confirmation").text_content() == "Confirmed 2 items for ada@example.com"


@case
def mind2web_table_triage_workflow(page):
    page.set_content(
        """
        <main>
          <label>Status
            <select id="status">
              <option value="all">All</option>
              <option value="open">Open</option>
              <option value="closed">Closed</option>
            </select>
          </label>
          <label>Owner <input id="owner"></label>
          <label><input id="urgent" type="checkbox"> Urgent only</label>
          <button id="run">Run triage</button>
          <table>
            <thead><tr><th>Ticket</th><th>Owner</th><th>Status</th><th>Priority</th><th></th></tr></thead>
            <tbody></tbody>
          </table>
          <div id="toast" role="status"></div>
        </main>
        <script>
        const tickets = [
          { title: 'Invoice export', owner: 'Sam', status: 'open', priority: 'P0' },
          { title: 'Login copy', owner: 'Rae', status: 'open', priority: 'P2' },
          { title: 'Billing retry', owner: 'Sam', status: 'closed', priority: 'P1' },
          { title: 'Webhook audit', owner: 'Sam', status: 'open', priority: 'P1' }
        ];
        function render() {
          const status = document.querySelector('#status').value;
          const owner = document.querySelector('#owner').value.toLowerCase();
          const urgent = document.querySelector('#urgent').checked;
          const rows = tickets.filter(ticket =>
            (status === 'all' || ticket.status === status) &&
            (!owner || ticket.owner.toLowerCase().includes(owner)) &&
            (!urgent || ticket.priority === 'P0')
          );
          document.querySelector('tbody').innerHTML = rows.map(ticket => `
            <tr>
              <td>${ticket.title}</td><td>${ticket.owner}</td><td>${ticket.status}</td><td>${ticket.priority}</td>
              <td><button aria-label="Assign ${ticket.title}" data-title="${ticket.title}">Assign</button></td>
            </tr>
          `).join('');
        }
        document.querySelector('#run').addEventListener('click', render);
        document.querySelector('tbody').addEventListener('click', event => {
          const button = event.target.closest('button[data-title]');
          if (button) document.querySelector('#toast').textContent = `Assigned ${button.dataset.title}`;
        });
        render();
        </script>
        """
    )

    page.get_by_label("Status").select_option("open")
    page.get_by_label("Owner").fill("Sam")
    page.get_by_label("Urgent only").check()
    page.get_by_role("button", name="Run triage").click()
    assert page.locator("tbody tr").count() == 1
    assert "Invoice export" in page.locator("tbody tr").first.inner_text()
    page.get_by_role("button", name="Assign Invoice export").click()
    assert page.get_by_role("status").text_content() == "Assigned Invoice export"


@case
def research_navigation_workflow(page):
    detail_url = data_url(
        """
        <title>Alpine Detail</title>
        <article>
          <h1>Alpine expansion report</h1>
          <dl><dt>Revenue</dt><dd>$4.2M</dd><dt>Risk</dt><dd>Low</dd></dl>
        </article>
        """
    )
    page.goto(
        data_url(
            f"""
            <title>Research Home</title>
            <main>
              <input aria-label="Research query" value="alpine">
              <a href="{escape(detail_url, quote=True)}">Open Alpine report</a>
              <button id="save" onclick="document.body.dataset.saved='alpine'">Save result</button>
            </main>
            """
        )
    )

    assert page.get_by_label("Research query").input_value() == "alpine"
    detail_href = page.get_by_role("link", name="Open Alpine report").get_attribute("href")
    page.goto(detail_href)
    page.wait_for_load_state()
    assert page.title() == "Alpine Detail"
    assert "Revenue" in page.locator("article").inner_text()
    page.go_back()
    page.wait_for_load_state()
    page.get_by_role("button", name="Save result").click()
    assert page.evaluate("document.body.dataset.saved") == "alpine"


BENCHMARK_CASES = list(CASES)


@case
def screenshot_type_quality_and_path_extension(page):
    with tempfile.TemporaryDirectory() as tmpdir:
        jpg_path = Path(tmpdir) / "screen.jpg"
        page.set_content(
            """
            <style>
            body { margin: 0; background: rgb(255, 0, 0); }
            #box { width: 80px; height: 60px; background: rgb(0, 0, 255); }
            </style>
            <div id="box"></div>
            """
        )

        jpeg = page.screenshot(type="jpeg")
        low_quality = page.screenshot(type="jpeg", quality=40)
        inferred = page.screenshot(path=str(jpg_path))
        assert jpeg.startswith(b"\xff\xd8")
        assert low_quality.startswith(b"\xff\xd8")
        assert len(low_quality) <= len(jpeg)
        assert inferred.startswith(b"\xff\xd8")
        assert jpg_path.read_bytes().startswith(b"\xff\xd8")

        try:
            page.screenshot(type="png", quality=40)
        except Exception as exc:
            assert str(exc).splitlines()[0] == (
                "Page.screenshot: options.quality is unsupported for the png screenshots"
            )
        else:
            raise AssertionError("png screenshot quality unexpectedly succeeded")


@case
def screenshot_scale_and_clip(page):
    browser = page.context.browser
    context = browser.new_context(viewport={"width": 40, "height": 30}, device_scale_factor=2)
    scaled_page = context.new_page()
    try:
        scaled_page.set_content(
            "<style>body{margin:0}</style><div style='width:40px;height:30px;background:red'></div>"
        )

        assert png_size(scaled_page.screenshot()) == (80, 60)
        assert png_size(scaled_page.screenshot(scale="css")) == (40, 30)
        assert png_size(scaled_page.screenshot(scale="device")) == (80, 60)
        assert png_size(scaled_page.screenshot(clip={"x": 0, "y": 0, "width": 10, "height": 8})) == (20, 16)
        assert png_size(
            scaled_page.screenshot(clip={"x": 0, "y": 0, "width": 10, "height": 8}, scale="css")
        ) == (10, 8)
        assert png_size(scaled_page.locator("div").screenshot()) == (80, 60)
        assert png_size(scaled_page.locator("div").screenshot(scale="css")) == (40, 30)

        try:
            scaled_page.screenshot(scale="bad")
        except Exception as exc:
            assert str(exc).splitlines()[0] == "Page.screenshot: scale: expected one of (css|device)"
        else:
            raise AssertionError("invalid scale option unexpectedly succeeded")
    finally:
        context.close()


@case
def screenshot_omit_background(page):
    page.set_viewport_size({"width": 20, "height": 20})
    page.set_content("<style>html, body { margin: 0; width: 20px; height: 20px; }</style>")

    plain = page.screenshot()
    omitted = page.screenshot(omit_background=True)
    restored = page.screenshot()
    jpeg = page.screenshot(type="jpeg", omit_background=True)

    assert plain.startswith(b"\x89PNG")
    assert omitted.startswith(b"\x89PNG")
    assert jpeg.startswith(b"\xff\xd8")
    assert plain[25] == 2
    assert omitted[25] == 6
    assert restored[25] == 2


@case
def screenshot_style_option_is_temporary(page):
    page.set_viewport_size({"width": 20, "height": 20})
    page.set_content(
        """
        <style>
        body { margin: 0; background: rgb(255, 0, 0); }
        #box { width: 20px; height: 20px; background: rgb(255, 0, 0); }
        </style>
        <div id="box"></div>
        """
    )

    plain_page = page.screenshot()
    styled_page = page.screenshot(style="body, #box { background: rgb(0, 0, 255) !important; }")
    plain_locator = page.locator("#box").screenshot()
    styled_locator = page.locator("#box").screenshot(style="#box { background: rgb(0, 255, 0) !important; }")

    assert styled_page != plain_page
    assert styled_locator != plain_locator
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 0, 0)"
    assert page.locator("#box").evaluate("(el) => getComputedStyle(el).backgroundColor") == "rgb(255, 0, 0)"


@case
def screenshot_mask_and_mask_color(page):
    page.set_viewport_size({"width": 30, "height": 30})
    page.set_content(
        """
        <style>
        body { margin: 0; background: rgb(255, 255, 255); }
        #box { position: absolute; left: 0; top: 0; width: 30px; height: 30px; background: rgb(255, 255, 255); }
        #secret { position: absolute; left: 5px; top: 5px; width: 10px; height: 10px; background: rgb(255, 0, 0); }
        </style>
        <div id="box"><div id="secret"></div></div>
        """
    )
    secret = page.locator("#secret")

    plain = page.screenshot()
    default_masked = page.screenshot(mask=[secret])
    green_masked = page.screenshot(mask=[secret], mask_color="rgb(0, 255, 0)")
    locator_masked = page.locator("#box").screenshot(mask=[secret], mask_color="rgb(0, 0, 255)")

    assert png_pixel(plain, 10, 10)[:3] == (255, 0, 0)
    assert png_pixel(default_masked, 10, 10)[:3] == (255, 0, 255)
    assert png_pixel(green_masked, 10, 10)[:3] == (0, 255, 0)
    assert png_pixel(locator_masked, 10, 10)[:3] == (0, 0, 255)
    assert secret.evaluate("(el) => getComputedStyle(el).backgroundColor") == "rgb(255, 0, 0)"


@case
def screenshot_animation_and_caret_options(page):
    page.set_viewport_size({"width": 40, "height": 40})
    page.set_content(
        """
        <style>
        body { margin: 0; }
        #box {
          width: 40px;
          height: 40px;
          background: rgb(255, 0, 0);
          animation: color 10s linear forwards;
        }
        @keyframes color {
          from { background: rgb(255, 0, 0); }
          to { background: rgb(0, 0, 255); }
        }
        </style>
        <div id="box"></div>
        <input id="field" value="focused">
        """
    )
    page.wait_for_timeout(100)

    allowed = png_pixel(page.screenshot(animations="allow"), 20, 20)[:3]
    disabled = png_pixel(page.screenshot(animations="disabled", caret="hide"), 20, 20)[:3]

    assert allowed[0] > allowed[2]
    assert disabled[2] > disabled[0]
    try:
        page.screenshot(animations="bogus")
    except Exception as exc:
        assert str(exc).splitlines()[0] == "Page.screenshot: animations: expected one of (disabled|allow)"
    else:
        raise AssertionError("invalid animations option unexpectedly succeeded")
    try:
        page.screenshot(caret="bogus")
    except Exception as exc:
        assert str(exc).splitlines()[0] == "Page.screenshot: caret: expected one of (hide|initial)"
    else:
        raise AssertionError("invalid caret option unexpectedly succeeded")


@case
def locator_screenshot_animation_disabled_matches_playwright(page):
    page.set_viewport_size({"width": 50, "height": 50})
    page.set_content(
        """
        <style>
        body { margin: 0; }
        #box {
          width: 40px;
          height: 40px;
          background: rgb(255, 0, 0);
          animation: color 10s linear forwards;
        }
        @keyframes color {
          from { background: rgb(255, 0, 0); }
          to { background: rgb(0, 0, 255); }
        }
        </style>
        <div id="box"></div>
        """
    )
    page.wait_for_timeout(100)

    locator = page.locator("#box")
    allowed = png_pixel(locator.screenshot(timeout=3_000, animations="allow"), 20, 20)[:3]
    disabled = png_pixel(locator.screenshot(timeout=3_000, animations="disabled"), 20, 20)[:3]

    assert allowed[0] > allowed[2]
    assert disabled[2] > disabled[0]


@case
def screenshot_option_validation_matches_playwright(page):
    page.set_content("<style>body{margin:0}</style><div style='width:20px;height:20px;background:red'></div>")

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("screenshot option unexpectedly accepted invalid value")

    invalid_cases = [
        (
            lambda: page.screenshot(full_page="bad"),
            "Page.screenshot: full_page: expected boolean, got string",
        ),
        (
            lambda: page.screenshot(full_page=1),
            "Page.screenshot: full_page: expected boolean, got number",
        ),
        (
            lambda: page.screenshot(omit_background="bad"),
            "Page.screenshot: omit_background: expected boolean, got string",
        ),
        (
            lambda: page.screenshot(omit_background=1),
            "Page.screenshot: omit_background: expected boolean, got number",
        ),
        (
            lambda: page.screenshot(timeout="bad"),
            "Page.screenshot: timeout: expected float, got string",
        ),
        (
            lambda: page.screenshot(quality=True),
            "Page.screenshot: quality: expected integer, got boolean",
        ),
        (
            lambda: page.screenshot(type="jpeg", quality=40.5),
            "Page.screenshot: quality: expected integer, got float 40.5",
        ),
        (
            lambda: page.screenshot(style=123),
            "Page.screenshot: style: expected string, got number",
        ),
        (
            lambda: page.screenshot(mask_color=123),
            "Page.screenshot: mask_color: expected string, got number",
        ),
        (
            lambda: page.screenshot(clip="bad"),
            "Page.screenshot: clip: expected object, got string",
        ),
        (
            lambda: page.screenshot(clip={"x": 0, "y": 0, "width": 10}),
            "Page.screenshot: clip.height: expected float, got undefined",
        ),
        (
            lambda: page.screenshot(clip={"x": "bad", "y": 0, "width": 10, "height": 10}),
            "Page.screenshot: clip.x: expected float, got string",
        ),
        (
            lambda: page.screenshot(path=123),
            "expected str, bytes or os.PathLike object, not int",
        ),
    ]
    for operation, expected in invalid_cases:
        expect_error(operation, expected)


@case
def pdf_pathlike_writes_file_matches_playwright(page):
    with tempfile.TemporaryDirectory(prefix="rustwright-pdf-pathlike-") as directory:
        target = Path(directory) / "artifact.pdf"
        page.set_content("<main>PDF pathlike</main>")

        data = page.pdf(path=target)

        assert data.startswith(b"%PDF")
        assert target.read_bytes() == data


@case
def pdf_option_validation_matches_playwright(page):
    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("PDF option unexpectedly accepted invalid value")

    invalid_cases = [
        (
            lambda: page.pdf(print_background="bad"),
            "Page.pdf: print_background: expected boolean, got string",
        ),
        (
            lambda: page.pdf(landscape="bad"),
            "Page.pdf: landscape: expected boolean, got string",
        ),
        (
            lambda: page.pdf(display_header_footer="bad"),
            "Page.pdf: display_header_footer: expected boolean, got string",
        ),
        (
            lambda: page.pdf(prefer_css_page_size="bad"),
            "Page.pdf: prefer_css_page_size: expected boolean, got string",
        ),
        (
            lambda: page.pdf(tagged="bad"),
            "Page.pdf: tagged: expected boolean, got string",
        ),
        (
            lambda: page.pdf(outline="bad"),
            "Page.pdf: outline: expected boolean, got string",
        ),
        (
            lambda: page.pdf(scale="bad"),
            "Page.pdf: scale: expected float, got string",
        ),
        (
            lambda: page.pdf(margin="bad"),
            "Page.pdf: margin: expected object, got string",
        ),
        (
            lambda: page.pdf(margin={"top": True}),
            "Page.pdf: margin.top: expected string, got boolean",
        ),
        (
            lambda: page.pdf(width=True),
            "Page.pdf: width: expected string, got boolean",
        ),
        (
            lambda: page.pdf(height=100),
            "Page.pdf: height: expected string, got number",
        ),
        (
            lambda: page.pdf(format=123),
            "Page.pdf: format: expected string, got number",
        ),
        (
            lambda: page.pdf(header_template=123),
            "Page.pdf: header_template: expected string, got number",
        ),
        (
            lambda: page.pdf(footer_template=123),
            "Page.pdf: footer_template: expected string, got number",
        ),
        (
            lambda: page.pdf(page_ranges=123),
            "Page.pdf: page_ranges: expected string, got number",
        ),
    ]
    for operation, expected in invalid_cases:
        expect_error(operation, expected)


@case
def role_locator_implicit_roles_and_names(page):
    page.set_content(
        """
        <header id="banner">Header</header>
        <footer id="contentinfo">Footer</footer>
        <aside id="complementary">Aside</aside>
        <article id="article">Article<header id="article-header">Article Header</header><footer id="article-footer">Article Footer</footer></article>
        <form id="signup" aria-label="Signup"><input></form>
        <form id="unnamed-form"><input></form>
        <section id="featured" aria-label="Featured">Featured<header id="section-header">Section Header</header><footer id="section-footer">Section Footer</footer></section>
        <section id="unnamed-section">Unnamed</section>
        <hr id="separator">
        <details id="details"><summary>More</summary><p>Body</p></details>
        <fieldset id="group"><legend>Choice Group</legend><input></fieldset>
        <dialog id="dialog" open>Dialog</dialog>
        <blockquote id="role-blockquote">Quote body</blockquote>
        <code id="role-code">print()</code>
        <del id="role-deletion">old</del>
        <em id="role-emphasis">important</em>
        <ins id="role-insertion">new</ins>
        <p id="role-paragraph">Paragraph role</p>
        <search id="role-search-landmark">Search landmark</search>
        <strong id="role-strong">Strong text</strong>
        <sub id="role-subscript">2</sub>
        <sup id="role-superscript">3</sup>
        <time id="role-time">10:00</time>
        <div id="explicit-multi" role="button link">Multi Role</div>
        <div id="explicit-fallback" role="unknown button">Fallback Role</div>
        <div id="explicit-none-button" role="none button">None Button</div>
        <div id="explicit-presentation-button" role="presentation button">Presentation Button</div>
        <div id="explicit-uppercase" role="BUTTON">Uppercase Role</div>
        <img id="logo" alt="Logo">
        <ul id="items"><li id="item">One</li></ul>
        <table id="grid"><caption id="role-caption">Grid Caption</caption><tr id="row"><th id="col-heading" scope="col">Col</th><th id="row-heading" scope="row">Row</th><td id="cell">Cell</td></tr></table>
        <input id="search" type="search" aria-label="Find">
        <input id="text" type="text" aria-label="Name">
        <input id="textbox-value-only" type="text" value="Name Value">
        <input id="textbox-placeholder" type="text" value="Alice" placeholder="Nickname">
        <input id="textbox-title-placeholder" type="text" value="Alice" placeholder="Nickname" title="Profile Name">
        <input id="quantity" type="number" aria-label="Qty">
        <input id="volume" type="range" aria-label="Volume">
        <progress id="progress" value="1" max="2" aria-label="Loading"></progress>
        <meter id="meter" value="0.5">half</meter>
        <output id="status">7</output>
        <select id="choice" aria-label="Choice"><option id="native-option">One</option></select>
        <select id="many" multiple aria-label="Many"><option id="many-option">Many One</option></select>
        <nav id="nav" aria-label="Primary"></nav>
        <main id="main"></main>
        <div role="main" id="role-main-scope"><header id="role-main-header">Role Main Header</header><footer id="role-main-footer">Role Main Footer</footer></div>
        <div role="article" id="role-article-scope"><header id="role-article-header">Role Article Header</header><footer id="role-article-footer">Role Article Footer</footer></div>
        <div role="region" aria-label="Scoped Region" id="role-region-scope"><header id="role-region-header">Role Region Header</header><footer id="role-region-footer">Role Region Footer</footer></div>
        <input id="image-input-alt" type="image" alt="Image Submit">
        <input id="image-input-value" type="image" value="Image Value">
        <input id="image-input-empty" type="image">
        <button id="image-button"><img alt="Search"></button>
        <button id="hidden-name">Visible<span aria-hidden="true"> Hidden</span></button>
        """
    )

    assert page.get_by_role("banner").get_attribute("id") == "banner"
    assert page.get_by_role("banner").count() == 1
    assert page.get_by_role("banner", name="Article Header").count() == 0
    assert page.get_by_role("banner", name="Section Header").count() == 0
    assert page.get_by_role("banner", name="Role Main Header").count() == 0
    assert page.get_by_role("banner", name="Role Article Header").count() == 0
    assert page.get_by_role("banner", name="Role Region Header").count() == 0
    assert page.get_by_role("contentinfo").get_attribute("id") == "contentinfo"
    assert page.get_by_role("contentinfo").count() == 1
    assert page.get_by_role("contentinfo", name="Article Footer").count() == 0
    assert page.get_by_role("contentinfo", name="Section Footer").count() == 0
    assert page.get_by_role("contentinfo", name="Role Main Footer").count() == 0
    assert page.get_by_role("contentinfo", name="Role Article Footer").count() == 0
    assert page.get_by_role("contentinfo", name="Role Region Footer").count() == 0
    assert page.get_by_role("complementary").get_attribute("id") == "complementary"
    assert set(page.get_by_role("article").evaluate_all("(els) => els.map(el => el.id)")) == {"article", "role-article-scope"}
    assert page.get_by_role("form", name="Signup").get_attribute("id") == "signup"
    assert page.get_by_role("form").count() == 1
    assert page.get_by_role("region", name="Featured").get_attribute("id") == "featured"
    assert set(page.get_by_role("region").evaluate_all("(els) => els.map(el => el.id)")) == {"featured", "role-region-scope"}
    assert page.get_by_role("separator").get_attribute("id") == "separator"
    assert page.get_by_role("group").count() == 2
    assert page.get_by_role("group", name="Choice Group").get_attribute("id") == "group"
    assert page.get_by_role("group", name="More").count() == 0
    assert page.get_by_role("dialog").get_attribute("id") == "dialog"
    assert page.get_by_role("blockquote").get_attribute("id") == "role-blockquote"
    assert page.get_by_role("caption").get_attribute("id") == "role-caption"
    assert page.get_by_role("code").get_attribute("id") == "role-code"
    assert page.get_by_role("deletion").get_attribute("id") == "role-deletion"
    assert page.get_by_role("emphasis").get_attribute("id") == "role-emphasis"
    assert page.get_by_role("insertion").get_attribute("id") == "role-insertion"
    assert "role-paragraph" in page.get_by_role("paragraph").evaluate_all("(els) => els.map(el => el.id)")
    assert page.get_by_role("search").get_attribute("id") == "role-search-landmark"
    assert page.get_by_role("strong").get_attribute("id") == "role-strong"
    assert page.get_by_role("subscript").get_attribute("id") == "role-subscript"
    assert page.get_by_role("superscript").get_attribute("id") == "role-superscript"
    assert page.get_by_role("time").get_attribute("id") == "role-time"
    assert page.get_by_role("button", name="Multi Role").get_attribute("id") == "explicit-multi"
    assert page.get_by_role("link", name="Multi Role").count() == 0
    assert page.get_by_role("button", name="Fallback Role").get_attribute("id") == "explicit-fallback"
    assert page.get_by_role("button", name="None Button").count() == 0
    assert "explicit-none-button" in page.get_by_role("none").evaluate_all("(els) => els.map(el => el.id)")
    assert page.get_by_role("button", name="Presentation Button").count() == 0
    assert "explicit-presentation-button" in page.get_by_role("presentation").evaluate_all("(els) => els.map(el => el.id)")
    assert page.get_by_role("button", name="Uppercase Role").count() == 0
    assert page.get_by_role("img", name="Logo").get_attribute("id") == "logo"
    assert page.get_by_role("list").get_attribute("id") == "items"
    assert page.get_by_role("listitem").get_attribute("id") == "item"
    assert page.get_by_role("table").get_attribute("id") == "grid"
    assert page.get_by_role("row").get_attribute("id") == "row"
    assert page.get_by_role("columnheader").get_attribute("id") == "col-heading"
    assert page.get_by_role("rowheader").get_attribute("id") == "row-heading"
    assert page.get_by_role("cell").get_attribute("id") == "cell"
    assert page.get_by_role("searchbox", name="Find").get_attribute("id") == "search"
    assert page.get_by_role("textbox", name="Find").count() == 0
    assert page.get_by_role("textbox", name="Name", exact=True).get_attribute("id") == "text"
    assert page.get_by_role("textbox", name="Name Value").count() == 0
    assert page.get_by_role("textbox", name="Profile Name").get_attribute("id") == "textbox-title-placeholder"
    assert page.get_by_role("textbox", name="Nickname", exact=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "textbox-placeholder"
    ]
    assert page.get_by_role("spinbutton", name="Qty").get_attribute("id") == "quantity"
    assert page.get_by_role("slider", name="Volume").get_attribute("id") == "volume"
    assert page.get_by_role("progressbar", name="Loading").get_attribute("id") == "progress"
    assert page.get_by_role("meter").get_attribute("id") == "meter"
    assert page.get_by_role("status").get_attribute("id") == "status"
    assert page.get_by_role("combobox", name="Choice").get_attribute("id") == "choice"
    assert page.get_by_role("listbox", name="Many").get_attribute("id") == "many"
    assert page.get_by_role("option", name="One", exact=True).get_attribute("id") == "native-option"
    assert page.get_by_role("option", name="Many One").get_attribute("id") == "many-option"
    assert page.get_by_role("navigation", name="Primary").get_attribute("id") == "nav"
    assert set(page.get_by_role("main").evaluate_all("(els) => els.map(el => el.id)")) == {"main", "role-main-scope"}
    assert page.get_by_role("button", name="Image Submit").get_attribute("id") == "image-input-alt"
    assert page.get_by_role("button", name="Image Value").count() == 0
    assert set(page.get_by_role("button", name="Submit", exact=True).evaluate_all("(els) => els.map(el => el.id)")) == {
        "image-input-empty",
        "image-input-value",
    }
    assert page.get_by_role("button", name="Search").get_attribute("id") == "image-button"
    assert page.get_by_role("button", name="Hidden").count() == 0


@case
def file_input_button_role_matches_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <input id="file" type="file">
        <label for="label-file">Upload Avatar</label><input id="label-file" type="file">
        <input id="file-title" type="file" title="Title Upload">
        <input id="file-aria" type="file" aria-label="ARIA Upload">
        <input id="hidden-file" type="file" style="display:none">
        <input id="submit" type="submit" value="Submitter">
        """
    )

    assert page.get_by_role("button").evaluate_all("(els) => els.map(el => el.id)") == [
        "file",
        "label-file",
        "file-title",
        "file-aria",
        "submit",
    ]
    assert page.get_by_role("button", include_hidden=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "file",
        "label-file",
        "file-title",
        "file-aria",
        "hidden-file",
        "submit",
    ]
    assert page.get_by_role("button", name="Choose File").evaluate_all("(els) => els.map(el => el.id)") == [
        "file",
        "file-title",
    ]
    assert page.locator('role=button[name="Choose File"]').evaluate_all("(els) => els.map(el => el.id)") == [
        "file",
        "file-title",
    ]
    assert page.get_by_role("button", name="Title Upload").count() == 0
    assert page.get_by_role("button", name="Upload Avatar").get_attribute("id") == "label-file"
    assert page.get_by_role("button", name="ARIA Upload").get_attribute("id") == "file-aria"
    assert page.get_by_role("button", name="Submitter").get_attribute("id") == "submit"
    expect(page.locator("#file")).to_have_role("button")
    expect(page.locator("#file")).to_have_accessible_name("Choose File")


@case
def input_role_defaults_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Inputs">
          <input id="color" type="color" aria-label="Color">
          <input id="date" type="date" aria-label="Date">
          <input id="datetime" type="datetime-local" aria-label="Date Time">
          <input id="month" type="month" aria-label="Month">
          <input id="time" type="time" aria-label="Time">
          <input id="week" type="week" aria-label="Week">
          <input id="submit-empty" type="submit">
          <input id="reset-empty" type="reset">
          <input id="button-empty" type="button">
          <input id="submit-value" type="submit" value="Send">
          <input id="reset-value" type="reset" value="Clear">
        </main>
        """
    )

    assert page.get_by_role("textbox").evaluate_all("(els) => els.map(el => el.id)") == [
        "color",
        "date",
        "datetime",
        "month",
        "time",
        "week",
    ]
    assert page.get_by_role("button", name="Submit").evaluate_all("(els) => els.map(el => el.id)") == ["submit-empty"]
    assert page.get_by_role("button", name="Reset").evaluate_all("(els) => els.map(el => el.id)") == ["reset-empty"]
    assert page.get_by_role("button", name="Send").evaluate_all("(els) => els.map(el => el.id)") == ["submit-value"]
    assert page.get_by_role("button", name="Clear").evaluate_all("(els) => els.map(el => el.id)") == ["reset-value"]
    expect(page.locator("#color")).to_have_role("textbox")
    expect(page.locator("#submit-empty")).to_have_accessible_name("Submit")
    expect(page.locator("#reset-empty")).to_have_accessible_name("Reset")
    assert page.aria_snapshot() == (
        '- main "Inputs":\n'
        '  - textbox "Color": "#000000"\n'
        '  - textbox "Date"\n'
        '  - textbox "Date Time"\n'
        '  - textbox "Month"\n'
        '  - textbox "Time"\n'
        '  - textbox "Week"\n'
        '  - button "Submit"\n'
        '  - button "Reset"\n'
        '  - button\n'
        '  - button "Send"\n'
        '  - button "Clear"'
    )


@case
def context_record_video_artifact(page):
    with tempfile.TemporaryDirectory() as directory:
        context = page.context.browser.new_context(
            record_video_dir=directory,
            record_video_size={"width": 320, "height": 240},
            viewport={"width": 320, "height": 240},
        )
        recorded_page = context.new_page()
        try:
            video = recorded_page.video
            assert video is not None
            for index, color in enumerate(["red", "green", "blue"], start=1):
                recorded_page.set_content(
                    f"<body style='margin:0;background:{color}'><main>frame {index}</main></body>"
                )
                recorded_page.wait_for_timeout(120)
            recorded_page.close()
            path = Path(video.path())
            assert path.exists()
            data = path.read_bytes()
            assert len(data) > 0
            if path.suffix == ".webm":
                assert data.startswith(b"\x1a\x45\xdf\xa3")
            else:
                assert data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG")
        finally:
            context.close()


@case
def video_delete_lifecycle_and_save_as_validation_matches_playwright(page):
    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("video operation unexpectedly succeeded")

    with tempfile.TemporaryDirectory() as directory:
        context = page.context.browser.new_context(
            record_video_dir=directory,
            record_video_size={"width": 160, "height": 120},
            viewport={"width": 160, "height": 120},
        )
        recorded_page = context.new_page()
        try:
            video = recorded_page.video
            assert video is not None
            recorded_page.set_content("<body style='margin:0;background:purple'><main>video</main></body>")
            recorded_page.wait_for_timeout(120)

            preclose_path = Path(video.path())
            assert preclose_path.parent == Path(directory)
            expect_error(
                lambda: video.save_as(Path(directory) / "preclose.webm"),
                "Page is not yet closed. Close the page prior to calling save_as",
            )

            recorded_page.close()

            video_path = Path(video.path())
            assert video_path.exists()
            copy_path = Path(directory) / f"copy{video_path.suffix}"
            video.save_as(copy_path)
            assert copy_path.read_bytes() == video_path.read_bytes()
            expect_error(
                lambda: video.save_as(123),
                "expected str, bytes or os.PathLike object, not int",
            )

            video.delete()
            assert not video_path.exists()
            assert Path(video.path()) == video_path
            expect_error(
                lambda: video.save_as(Path(directory) / f"after-delete{video_path.suffix}"),
                "Video.save_as: Target page, context or browser has been closed",
            )
            expect_error(
                lambda: video.delete(),
                "Video.delete: Target page, context or browser has been closed",
            )
        finally:
            context.close()


@case
def record_video_dir_path_validation_matches_playwright(page, playwright):
    browser = page.context.browser
    expected_context_error = (
        "argument should be a str or an os.PathLike object where __fspath__ returns a str, not 'int'"
    )
    expected_page_error = f"Browser.new_page: {expected_context_error}"

    def expect_type_error(operation, expected):
        try:
            operation()
        except TypeError as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("record_video_dir unexpectedly accepted a non-path value")

    expect_type_error(lambda: browser.new_context(record_video_dir=123), expected_context_error)
    expect_type_error(lambda: browser.new_page(record_video_dir=123), expected_page_error)

    with tempfile.TemporaryDirectory() as directory:
        profile = Path(directory) / "profile"
        expect_type_error(
            lambda: playwright.chromium.launch_persistent_context(
                str(profile),
                headless=True,
                record_video_dir=123,
            ),
            expected_context_error,
        )


@case
def record_video_size_without_dir_is_ignored_like_playwright(page, playwright):
    browser = page.context.browser
    invalid_size = {"width": "100", "height": 100}

    context = browser.new_context(record_video_size=invalid_size)
    try:
        context_page = context.new_page()
        assert context_page.video is None
    finally:
        context.close()

    owned_page = browser.new_page(record_video_size=invalid_size)
    try:
        assert owned_page.video is None
    finally:
        owned_page.close()

    with tempfile.TemporaryDirectory() as directory:
        profile = Path(directory) / "profile"
        persistent_context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=True,
            record_video_size=invalid_size,
        )
        try:
            assert persistent_context.pages
            assert persistent_context.pages[0].video is None
        finally:
            persistent_context.close()


@case
def tracing_unexpected_keyword_arguments_match_playwright(page):
    tracing = page.context.tracing

    def expect_type_error(operation, expected):
        try:
            operation()
        except TypeError as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("tracing operation unexpectedly accepted an unknown keyword argument")

    invalid_cases = [
        (
            lambda: tracing.start(unknown_option=True),
            "Tracing.start() got an unexpected keyword argument 'unknown_option'",
        ),
        (
            lambda: tracing.start_chunk(unknown_option=True),
            "Tracing.start_chunk() got an unexpected keyword argument 'unknown_option'",
        ),
        (
            lambda: tracing.stop_chunk(unknown_option=True),
            "Tracing.stop_chunk() got an unexpected keyword argument 'unknown_option'",
        ),
        (
            lambda: tracing.stop(unknown_option=True),
            "Tracing.stop() got an unexpected keyword argument 'unknown_option'",
        ),
        (
            lambda: tracing.group("group", unknown_option=True),
            "Tracing.group() got an unexpected keyword argument 'unknown_option'",
        ),
        (
            lambda: tracing.group_end(unknown_option=True),
            "Tracing.group_end() got an unexpected keyword argument 'unknown_option'",
        ),
    ]
    for operation, expected in invalid_cases:
        expect_type_error(operation, expected)


@case
def tracing_sources_records_stack_metadata(page):
    with tempfile.TemporaryDirectory() as directory:
        trace_path = Path(directory) / "trace-sources.zip"
        page.context.tracing.start(screenshots=True, snapshots=True, sources=True, title="Source Trace")

        def run_traced_actions() -> None:
            page.set_content("<title>Trace Sources</title><button>Trace</button>")
            page.click("button")

        run_traced_actions()
        page.context.tracing.stop(path=trace_path)

        assert zipfile.is_zipfile(trace_path)
        with zipfile.ZipFile(trace_path) as archive:
            names = set(archive.namelist())
            assert "trace.trace" in names
            assert "trace.stacks" in names
            stacks = json.loads(archive.read("trace.stacks").decode("utf-8"))
            assert stacks["files"]
            assert stacks["stacks"]
            assert any(str(file).endswith(".py") for file in stacks["files"])
            assert any(entry[1] for entry in stacks["stacks"])


@case
def download_path_save_cancel_and_delete(page):
    with tempfile.TemporaryDirectory() as directory:
        copy_path = Path(directory) / "copy.txt"
        with header_case_server() as base_url:
            page.set_content(f"<a id='download' href='{base_url}/download'>Download</a>")
            with page.expect_download() as download_info:
                page.click("#download")

        download = download_info.value
        downloaded_path = download.path()
        assert isinstance(downloaded_path, Path)
        download.save_as(str(copy_path))

        assert download.url.endswith("/download")
        assert download.suggested_filename == "report.txt"
        assert download.failure() is None
        assert downloaded_path.read_bytes() == b"download-body"
        assert copy_path.read_bytes() == b"download-body"

        download.cancel()
        assert download.failure() is None
        assert downloaded_path.exists()

        download.delete()
        assert not downloaded_path.exists()


@case
def download_delete_invalidates_artifact_and_save_as_path_validation_matches_playwright(page):
    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("download operation unexpectedly succeeded")

    with tempfile.TemporaryDirectory() as directory:
        copy_path = Path(directory) / "copy-pathlike.txt"
        after_delete_path = Path(directory) / "after-delete.txt"
        with header_case_server() as base_url:
            page.set_content(f"<a id='download' href='{base_url}/download'>Download</a>")
            with page.expect_download() as download_info:
                page.click("#download")

        download = download_info.value
        downloaded_path = Path(download.path())
        download.save_as(copy_path)
        assert copy_path.read_bytes() == b"download-body"

        expect_error(
            lambda: download.save_as(123),
            "expected str, bytes or os.PathLike object, not int",
        )

        download.delete()
        assert not downloaded_path.exists()
        expect_error(
            lambda: download.path(),
            "Download.path: Target page, context or browser has been closed",
        )
        expect_error(
            lambda: download.save_as(str(after_delete_path)),
            "Download.save_as: Target page, context or browser has been closed",
        )
        expect_error(
            lambda: download.failure(),
            "Download.failure: Target page, context or browser has been closed",
        )
        expect_error(
            lambda: download.cancel(),
            "Download.cancel: Target page, context or browser has been closed",
        )
        expect_error(
            lambda: download.delete(),
            "Download.delete: Target page, context or browser has been closed",
        )


@case
def download_predicate_and_wait_for_event_helpers(page):
    with tempfile.TemporaryDirectory() as directory:
        predicate_copy = Path(directory) / "predicate-copy.txt"
        waited_copy = Path(directory) / "waited-copy.txt"

        with header_case_server() as base_url:
            page.set_content(
                f"""
                <a id="predicate-download" href="{base_url}/download">Predicate download</a>
                <a id="wait-download" href="{base_url}/download">Wait download</a>
                """
            )

            with page.expect_download(lambda download: download.suggested_filename == "report.txt") as download_info:
                page.click("#predicate-download")

            predicate_download = download_info.value
            predicate_download.save_as(str(predicate_copy))

            page.evaluate("() => setTimeout(() => document.querySelector('#wait-download').click(), 20)")
            waited_download = page.wait_for_event(
                "download",
                lambda download: download.url.endswith("/download"),
                timeout=3_000,
            )
            waited_download.save_as(str(waited_copy))

        assert predicate_download.url.endswith("/download")
        assert waited_download.suggested_filename == "report.txt"
        assert predicate_copy.read_bytes() == b"download-body"
        assert waited_copy.read_bytes() == b"download-body"


@case
def overlapping_download_waiters_preserve_page_metadata(page):
    with header_case_server() as base_url:
        browser = page.context.browser
        assert browser is not None
        context = browser.new_context()
        try:
            download_page = context.new_page()
            other_page = context.new_page()
            download_page.set_content(f"<a id='slow' href='{base_url}/download-slow'>Slow</a>")
            other_page.set_content(f"<a id='fast' href='{base_url}/download'>Fast</a>")

            with download_page.expect_download(
                lambda download: download.url.endswith("/download-slow"),
                timeout=5_000,
            ) as download_info:
                download_page.evaluate("() => document.querySelector('#slow').click()")
                time.sleep(0.05)
                other_page.evaluate("() => document.querySelector('#fast').click()")

            download = download_info.value
            downloaded_path = Path(download.path())

            assert download.url.endswith("/download-slow")
            assert download.suggested_filename == "slow-report.txt"
            assert downloaded_path.read_bytes() == b"slow-download-body"
        finally:
            context.close()


@case
def persistent_downloads_path_exposes_crdownload_during_native_download(page, playwright):
    with tempfile.TemporaryDirectory() as directory, header_case_server() as base_url:
        root = Path(directory)
        downloads_dir = root / "downloads"
        context = playwright.chromium.launch_persistent_context(
            str(root / "profile"),
            headless=True,
            accept_downloads=True,
            downloads_path=str(downloads_dir),
        )
        try:
            download_page = context.pages[0]
            download_page.set_content(f"<a id='large' href='{base_url}/download-large-slow'>Large</a>")

            saw_partial = False
            observed_files = []
            with download_page.expect_download(timeout=10_000) as download_info:
                download_page.evaluate("() => document.querySelector('#large').click()")
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    files = sorted(path.name for path in downloads_dir.iterdir()) if downloads_dir.exists() else []
                    observed_files.append(files)
                    if any(name.endswith(".crdownload") for name in files):
                        saw_partial = True
                        break
                    time.sleep(0.02)

            download = download_info.value
            downloaded_path = Path(download.path())

            assert saw_partial, f"expected in-flight .crdownload file, observed {observed_files[-10:]}"
            assert download.suggested_filename == "large-report.bin"
            assert downloaded_path.parent == downloads_dir
            assert downloaded_path.stat().st_size == 2 * 1024 * 1024
            assert not list(downloads_dir.glob("*.crdownload"))
        finally:
            context.close()


def skyvern_browser_cdp_download_behavior_allow_writes_suggested_file(page):
    with tempfile.TemporaryDirectory() as directory, header_case_server() as base_url:
        downloads_dir = Path(directory) / "browser-cdp-downloads"
        downloads_dir.mkdir()
        browser = page.context.browser
        assert browser is not None

        session = browser.new_browser_cdp_session()
        session.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(downloads_dir),
                "eventsEnabled": True,
            },
        )
        session.detach()

        context = browser.new_context()
        try:
            download_page = context.new_page()
            download_page.set_content(f"<a id='large' href='{base_url}/download-large-slow'>Large</a>")
            download_page.evaluate("() => document.querySelector('#large').click()")

            saw_partial = False
            observed_files = []
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                files = sorted(path.name for path in downloads_dir.iterdir())
                observed_files.append(files)
                if any(name.endswith(".crdownload") for name in files):
                    saw_partial = True
                if "large-report.bin" in files:
                    break
                time.sleep(0.05)

            final_path = downloads_dir / "large-report.bin"
            assert saw_partial, f"expected suggested-name .crdownload file, observed {observed_files[-10:]}"
            assert final_path.exists(), f"expected suggested-name final file, observed {observed_files[-10:]}"
            assert final_path.stat().st_size == 2 * 1024 * 1024
            assert not list(downloads_dir.glob("*.crdownload"))
        finally:
            context.close()


def skyvern_browser_cdp_download_progress_events(page):
    browser = page.context.browser
    assert browser is not None

    with tempfile.TemporaryDirectory() as directory, header_case_server() as base_url:
        downloads_dir = Path(directory) / "browser-cdp-progress"
        downloads_dir.mkdir()
        session = browser.new_browser_cdp_session()
        will_begin_events = []
        progress_events = []
        context = browser.new_context()
        try:
            download_page = context.new_page()
            marker_url = data_url(f"<title>download-progress-{time.time_ns()}</title>")
            download_page.goto(marker_url)
            targets = session.send("Target.getTargets").get("targetInfos") or []
            matching_targets = [
                target
                for target in targets
                if target.get("type") == "page" and target.get("url") == marker_url
            ]
            assert matching_targets, targets
            context_id = matching_targets[0].get("browserContextId")
            assert context_id
            session.on("Browser.downloadWillBegin", lambda event: will_begin_events.append(event))
            session.on("Browser.downloadProgress", lambda event: progress_events.append(event))
            session.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "browserContextId": context_id,
                    "downloadPath": str(downloads_dir),
                    "eventsEnabled": True,
                },
            )

            download_page.set_content(f"<a id='download' href='{base_url}/download'>Download</a>")
            download_page.evaluate("() => document.querySelector('#download').click()")

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if any(event.get("state") == "completed" for event in progress_events):
                    break
                time.sleep(0.05)

            assert will_begin_events
            assert progress_events
            begin_event = will_begin_events[0]
            completed = [event for event in progress_events if event.get("state") == "completed"]
            assert completed, f"expected completed downloadProgress event, got {progress_events}"
            completed_event = completed[-1]
            assert completed_event["guid"] == begin_event["guid"]
            assert begin_event["url"].endswith("/download")
            assert begin_event["suggestedFilename"] == "report.txt"
            assert completed_event["receivedBytes"] >= len(b"download-body")
            assert completed_event["totalBytes"] >= len(b"download-body")
        finally:
            try:
                session.detach()
            finally:
                context.close()


def skyvern_browser_cdp_download_monitor_deny_event_and_context_request(page):
    with header_case_server() as base_url:
        browser = page.context.browser
        assert browser is not None
        context = browser.new_context()
        session = browser.new_browser_cdp_session()
        events = []
        try:
            download_page = context.new_page()
            download_page.goto(f"{base_url}/set-cookies")
            session.on("Browser.downloadWillBegin", lambda event: events.append(event))
            session.send(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "deny",
                    "eventsEnabled": True,
                },
            )

            download_page.set_content(f"<a id='protected' href='{base_url}/protected-download'>Protected</a>")
            download_page.evaluate("() => document.querySelector('#protected').click()")

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not events:
                time.sleep(0.05)

            assert events, "expected Browser.downloadWillBegin for denied browser-native download"
            event = events[0]
            assert event["url"].endswith("/protected-download")
            assert event["suggestedFilename"] == "protected.txt"
            response = context.request.get(event["url"])
            try:
                assert response.status == 200
                assert response.body() == b"protected-download-body"
            finally:
                response.dispose()
        finally:
            try:
                session.detach()
            finally:
                context.close()


@case
def page_generic_download_and_filechooser_event_helpers(page):
    with tempfile.TemporaryDirectory() as directory:
        upload = Path(directory) / "generic-chooser.txt"
        upload.write_text("generic chosen", encoding="utf-8")
        download_copy = Path(directory) / "generic-download-copy.txt"

        with header_case_server() as base_url:
            page.set_content(
                f"""
                <a id="download" href="{base_url}/download">Download</a>
                <input id="file" type="file" multiple>
                <script>
                document.querySelector('#file').addEventListener('change', event => {{
                  document.body.dataset.chosen = event.target.files[0].name;
                }});
                </script>
                """
            )

            with page.expect_event("download", lambda download: download.suggested_filename == "report.txt") as download_info:
                page.click("#download")

            download = download_info.value
            download.save_as(str(download_copy))

            with page.expect_event("filechooser", lambda chooser: chooser.is_multiple()) as chooser_info:
                page.click("#file")

            chooser = chooser_info.value
            assert chooser.page is page
            assert chooser.is_multiple() is True
            assert chooser.element.get_attribute("id") == "file"
            chooser.set_files(str(upload))

        assert download.url.endswith("/download")
        assert download.failure() is None
        assert download_copy.read_bytes() == b"download-body"
        assert page.evaluate("document.body.dataset.chosen") == "generic-chooser.txt"
        assert page.evaluate("async () => await document.querySelector('#file').files[0].text()") == "generic chosen"


@case
def file_chooser_set_files_payload_and_timeout_validation_matches_playwright(page):
    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("FileChooser.set_files unexpectedly accepted invalid option")

    page.set_content(
        """
        <input id="file" type="file">
        <script>
        document.querySelector('#file').addEventListener('change', async event => {
          const file = event.target.files[0];
          document.body.dataset.filePayload = file
            ? `${file.name}:${file.type}:${await file.text()}`
            : 'none';
        });
        </script>
        """
    )

    with page.expect_file_chooser() as chooser_info:
        page.click("#file")
    chooser = chooser_info.value

    expect_error(
        lambda: chooser.set_files([], timeout="bad"),
        "FileChooser.set_files: timeout: expected float, got string",
    )
    chooser.set_files({"name": "payload.txt", "mimeType": "text/plain", "buffer": b"payload-body"})

    handle = page.wait_for_function(
        "() => document.body.dataset.filePayload === 'payload.txt:text/plain:payload-body'",
        timeout=3_000,
    )
    try:
        assert handle.json_value() is True
    finally:
        handle.dispose()


@case
def file_chooser_payload_preserves_custom_mime_type_matches_playwright(page):
    page.set_content(
        """
        <input id="file" type="file">
        <script>
        document.querySelector('#file').addEventListener('change', async event => {
          const file = event.target.files[0];
          document.body.dataset.customPayload = file
            ? `${file.name}:${file.type}:${await file.text()}`
            : 'none';
        });
        </script>
        """
    )

    with page.expect_file_chooser() as chooser_info:
        page.click("#file")

    chooser_info.value.set_files(
        {
            "name": "skyvern-upload.bin",
            "mimeType": "application/x-skyvern-upload",
            "buffer": b"skyvern-body",
        }
    )
    handle = page.wait_for_function(
        "() => document.body.dataset.customPayload === 'skyvern-upload.bin:application/x-skyvern-upload:skyvern-body'",
        timeout=3_000,
    )
    try:
        assert handle.json_value() is True
    finally:
        handle.dispose()


@case
def file_chooser_element_resolves_exact_input_matches_playwright(page):
    page.set_content(
        """
        <input id="first" type="file">
        <input id="second" type="file" multiple>
        """
    )

    with page.expect_file_chooser() as chooser_info:
        page.click("#second")

    chooser = chooser_info.value
    assert chooser.is_multiple() is True
    assert chooser.element.get_attribute("id") == "second"

    with page.expect_file_chooser() as first_info:
        page.click("#first")

    first = first_info.value
    assert first.is_multiple() is False
    assert first.element.get_attribute("id") == "first"


@case
def single_file_inputs_reject_multiple_files_matches_playwright(page):
    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("single-file input unexpectedly accepted multiple files")

    with tempfile.TemporaryDirectory() as directory:
        first_path = Path(directory) / "first.txt"
        second_path = Path(directory) / "second.txt"
        first_path.write_text("first", encoding="utf-8")
        second_path.write_text("second", encoding="utf-8")
        paths = [str(first_path), str(second_path)]

        page.set_content(
            """
            <input id="chooser" type="file">
            <input id="locator" type="file">
            <script>
            for (const input of document.querySelectorAll('input[type=file]')) {
              input.addEventListener('change', event => {
                document.body.dataset[event.target.id] =
                  Array.from(event.target.files).map(file => file.name).join(',');
              });
            }
            </script>
            """
        )

        with page.expect_file_chooser() as chooser_info:
            page.click("#chooser")

        expect_error(
            lambda: chooser_info.value.set_files(paths),
            "FileChooser.set_files: Error: Non-multiple file input can only accept single file",
        )
        assert page.evaluate("document.body.dataset.chooser") is None

        expect_error(
            lambda: page.locator("#locator").set_input_files(paths),
            "Locator.set_input_files: Error: Non-multiple file input can only accept single file",
        )
        assert page.evaluate("document.body.dataset.locator") is None


@case
def file_input_set_input_files_bypasses_actionability_like_playwright(page):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        page_path = root / "page-disabled.txt"
        locator_path = root / "locator-hidden-disabled.txt"
        element_path = root / "element-invisible-disabled.txt"
        page_path.write_text("page disabled", encoding="utf-8")
        locator_path.write_text("locator hidden disabled", encoding="utf-8")
        element_path.write_text("element invisible disabled", encoding="utf-8")

        page.set_content(
            """
            <input id="page-disabled" type="file" disabled>
            <input id="locator-hidden-disabled" type="file" disabled style="display:none">
            <input id="element-invisible-disabled" type="file" disabled style="visibility:hidden">
            <script>
            for (const input of document.querySelectorAll('input[type=file]')) {
              input.addEventListener('change', event => {
                const file = event.target.files[0];
                document.body.setAttribute(`data-${event.target.id}`, file ? file.name : 'none');
              });
            }
            </script>
            """
        )

        page.set_input_files("#page-disabled", str(page_path))
        page.locator("#locator-hidden-disabled").set_input_files(str(locator_path))
        element = page.query_selector("#element-invisible-disabled")
        assert element is not None
        try:
            element.set_input_files(str(element_path))
        finally:
            element.dispose()

    assert page.evaluate("document.body.getAttribute('data-page-disabled')") == "page-disabled.txt"
    assert page.evaluate("document.body.getAttribute('data-locator-hidden-disabled')") == "locator-hidden-disabled.txt"
    assert page.evaluate("document.body.getAttribute('data-element-invisible-disabled')") == "element-invisible-disabled.txt"
    assert page.evaluate("Array.from(document.querySelector('#page-disabled').files).map(file => file.name)") == [
        "page-disabled.txt"
    ]


@case
def context_record_har_artifact(page):
    with tempfile.TemporaryDirectory() as directory:
        har_path = Path(directory) / "record.har"
        with header_case_server() as base_url:
            context = page.context.browser.new_context(record_har_path=str(har_path))
            recorded_page = context.new_page()
            try:
                response = recorded_page.goto(f"{base_url}/headers")
                assert response.status == 200
            finally:
                context.close()
        har = json.loads(har_path.read_text(encoding="utf-8"))
        entries = har["log"]["entries"]
        assert any(
            entry["request"]["url"].endswith("/headers") and entry["response"]["status"] == 200
            for entry in entries
        )


@case
def context_record_har_option_validation_matches_playwright(page, playwright):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("record_har option unexpectedly accepted invalid value")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        har_path = str(root / "record.har")

        expect_error(
            lambda: browser.new_context(record_har_path=har_path, record_har_content="bad"),
            "Browser.new_context: options.content: expected one of (embed|attach|omit)",
        )
        expect_error(
            lambda: browser.new_context(record_har_path=har_path, record_har_mode="bad"),
            "Browser.new_context: options.mode: expected one of (full|minimal)",
        )
        expect_error(
            lambda: browser.new_page(record_har_path=har_path, record_har_content="bad"),
            "Browser.new_page: options.content: expected one of (embed|attach|omit)",
        )
        expect_error(
            lambda: browser.new_page(record_har_path=har_path, record_har_mode="bad"),
            "Browser.new_page: options.mode: expected one of (full|minimal)",
        )
        expect_error(
            lambda: playwright.chromium.launch_persistent_context(
                str(root / "profile-content"),
                headless=True,
                record_har_path=har_path,
                record_har_content="bad",
            ),
            "BrowserType.launch_persistent_context: options.content: expected one of (embed|attach|omit)",
        )
        expect_error(
            lambda: playwright.chromium.launch_persistent_context(
                str(root / "profile-mode"),
                headless=True,
                record_har_path=har_path,
                record_har_mode="bad",
            ),
            "BrowserType.launch_persistent_context: options.mode: expected one of (full|minimal)",
        )

        for target in (
            browser.new_context(record_har_content="bad", record_har_mode="bad"),
            browser.new_page(record_har_content="bad", record_har_mode="bad"),
        ):
            target.close()


@case
def context_record_har_path_scalar_coercion_matches_playwright(page, playwright):
    browser = page.context.browser

    def exercise(operation):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = os.getcwd()
            os.chdir(root)
            try:
                target = operation(root)
                try:
                    page_or_context = target
                    if hasattr(page_or_context, "new_page"):
                        page_or_context.new_page().set_content("<main>har</main>")
                    else:
                        page_or_context.set_content("<main>har</main>")
                finally:
                    target.close()
                assert (root / "123").exists()
                assert not (root / "False").exists()
            finally:
                os.chdir(previous)

    exercise(lambda root: browser.new_context(record_har_path=123))
    exercise(lambda root: browser.new_page(record_har_path=123))
    exercise(
        lambda root: playwright.chromium.launch_persistent_context(
            str(root / "profile"),
            headless=True,
            record_har_path=123,
        )
    )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        previous = os.getcwd()
        os.chdir(root)
        try:
            for target in (
                browser.new_context(record_har_path=False, record_har_content="bad", record_har_mode="bad"),
                browser.new_page(record_har_path=False, record_har_content="bad", record_har_mode="bad"),
            ):
                target.close()
            assert not (root / "False").exists()
        finally:
            os.chdir(previous)


@case
def persistent_context_initial_page_matches_playwright(page, playwright):
    with tempfile.TemporaryDirectory() as directory:
        context = playwright.chromium.launch_persistent_context(str(Path(directory) / "profile"), headless=True)
        try:
            assert len(context.pages) == 1
            persistent_page = context.pages[0]
            assert persistent_page.url == "about:blank"
            persistent_page.set_content("<title>Persistent Context</title>")
            assert persistent_page.title() == "Persistent Context"
        finally:
            context.close()


@case
def persistent_context_skyvern_artifact_options_match_playwright(page, playwright):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        downloads_dir = root / "downloads"
        har_path = root / "session.har"
        with header_case_server() as base_url:
            context = playwright.chromium.launch_persistent_context(
                str(root / "profile"),
                headless=True,
                accept_downloads=True,
                base_url=base_url,
                downloads_path=str(downloads_dir),
                record_har_path=str(har_path),
                record_har_url_filter="**/headers",
                record_har_content="omit",
            )
            closed = False
            try:
                persistent_browser = context.browser
                assert persistent_browser is not None
                assert context in persistent_browser.contexts
                assert len(context.pages) == 1
                persistent_page = context.pages[0]
                assert persistent_page.context is context

                response = persistent_page.goto("/headers")
                assert response is not None
                assert response.ok
                persistent_page.wait_for_load_state("load")

                persistent_page.set_content(f"<a id='download' href='{base_url}/download'>Download</a>")
                with persistent_page.expect_download() as download_info:
                    persistent_page.click("#download")
                downloaded_path = Path(download_info.value.path())
                assert downloaded_path.parent == downloads_dir
                assert downloaded_path.read_bytes() == b"download-body"
                context.close()
                closed = True
                assert not persistent_browser.is_connected()
                assert persistent_browser.contexts == []
            finally:
                if not closed:
                    context.close()

        har = json.loads(har_path.read_text(encoding="utf-8"))
        assert any(entry["request"]["url"].endswith("/headers") for entry in har["log"]["entries"])


@case
def persistent_context_record_video_artifact_matches_playwright(page, playwright):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        video_dir = root / "videos"
        context = playwright.chromium.launch_persistent_context(
            str(root / "profile"),
            headless=True,
            record_video_dir=str(video_dir),
            record_video_size={"width": 160, "height": 120},
            viewport={"width": 160, "height": 120},
        )
        closed = False
        try:
            assert len(context.pages) == 1
            persistent_page = context.pages[0]
            video = persistent_page.video
            assert video is not None
            for color in ("red", "green"):
                persistent_page.set_content(
                    f"<body style='margin:0;background:{color}'><main>{color}</main></body>"
                )
                persistent_page.wait_for_timeout(120)

            context.close()
            closed = True
            path = Path(video.path())
            assert path.exists()
            data = path.read_bytes()
            assert len(data) > 0
            if path.suffix == ".webm":
                assert data.startswith(b"\x1a\x45\xdf\xa3")
            else:
                assert data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG")
        finally:
            if not closed:
                context.close()


@case
def persistent_context_extra_http_headers_apply_to_pages(page, playwright):
    with tempfile.TemporaryDirectory() as directory:
        with header_case_server() as base_url:
            context = playwright.chromium.launch_persistent_context(
                str(Path(directory) / "profile"),
                headless=True,
                base_url=base_url,
                extra_http_headers={"X-Extra": "from-persistent-context", "X-Shared": "context"},
            )
            try:
                assert context.pages[0].goto("/echo-headers").json()["x-extra"] == "from-persistent-context"

                new_page = context.new_page()
                try:
                    payload = new_page.goto("/echo-headers").json()
                    assert payload["x-extra"] == "from-persistent-context"
                    assert payload["x-shared"] == "context"

                    new_page.set_extra_http_headers({"X-Page": "page", "X-Shared": "page"})
                    merged = new_page.goto("/echo-headers").json()
                    assert merged["x-extra"] == "from-persistent-context"
                    assert merged["x-page"] == "page"
                    assert merged["x-shared"] == "page"
                finally:
                    new_page.close()
            finally:
                context.close()


@case
def persistent_context_environment_options_apply_to_initial_and_new_pages(page, playwright):
    with tempfile.TemporaryDirectory() as directory:
        with header_case_server() as base_url:
            context = playwright.chromium.launch_persistent_context(
                str(Path(directory) / "profile"),
                headless=True,
                base_url=base_url,
                locale="fr-FR",
                timezone_id="America/Los_Angeles",
                color_scheme="dark",
                reduced_motion="reduce",
                forced_colors="active",
                contrast="more",
                viewport={"width": 320, "height": 240},
                screen={"width": 360, "height": 260},
                device_scale_factor=2,
                has_touch=True,
            )
            try:
                for context_page in (context.pages[0], context.new_page()):
                    try:
                        response = context_page.goto("/echo-headers")
                        assert response.json()["accept-language"] == "fr-FR"
                        assert context_page.evaluate("Intl.DateTimeFormat().resolvedOptions().locale") == "fr-FR"
                        assert (
                            context_page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
                            == "America/Los_Angeles"
                        )
                        assert context_page.evaluate("navigator.language") == "fr-FR"
                        assert context_page.evaluate("navigator.languages") == ["fr-FR"]
                        assert context_page.viewport_size == {"width": 320, "height": 240}
                        assert context_page.evaluate("({ width: screen.width, height: screen.height })") == {
                            "width": 360,
                            "height": 260,
                        }
                        assert_near(context_page.evaluate("window.devicePixelRatio"), 2, 0.01)
                        assert context_page.evaluate("navigator.maxTouchPoints") > 0
                        assert context_page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches") is True
                        assert context_page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches") is True
                        assert context_page.evaluate("matchMedia('(forced-colors: active)').matches") is True
                        assert context_page.evaluate("matchMedia('(prefers-contrast: more)').matches") is True
                    finally:
                        if context_page is not context.pages[0]:
                            context_page.close()
            finally:
                context.close()


@case
def persistent_context_proxy_applies_to_initial_and_new_pages(page, playwright):
    with tempfile.TemporaryDirectory() as directory:
        with http_proxy_case_server() as (proxy_url, proxy_seen):
            context = playwright.chromium.launch_persistent_context(
                str(Path(directory) / "profile"),
                headless=True,
                proxy={"server": proxy_url},
            )
            try:
                initial_response = context.pages[0].goto("http://persistent-proxy.invalid/initial")
                assert initial_response.json() == {
                    "proxied": True,
                    "url": "http://persistent-proxy.invalid/initial",
                    "host": "persistent-proxy.invalid",
                }

                new_page = context.new_page()
                try:
                    new_response = new_page.goto("http://persistent-proxy.invalid/new")
                    assert new_response.json() == {
                        "proxied": True,
                        "url": "http://persistent-proxy.invalid/new",
                        "host": "persistent-proxy.invalid",
                    }
                finally:
                    new_page.close()
            finally:
                context.close()

    assert (
        proxy_seen_for_url(proxy_seen, "http://persistent-proxy.invalid/initial")
        + proxy_seen_for_url(proxy_seen, "http://persistent-proxy.invalid/new")
    ) == [
        {"url": "http://persistent-proxy.invalid/initial", "host": "persistent-proxy.invalid"},
        {"url": "http://persistent-proxy.invalid/new", "host": "persistent-proxy.invalid"},
    ]


@case
def persistent_context_init_script_applies_to_initial_and_new_pages(page, playwright):
    with tempfile.TemporaryDirectory() as directory:
        context = playwright.chromium.launch_persistent_context(
            str(Path(directory) / "profile"),
            headless=True,
        )
        try:
            context.add_init_script("window.__persistentContextInit = 'context-init'")

            initial_page = context.pages[0]
            initial_page.goto(data_url("<main>Initial</main>"))
            assert initial_page.evaluate("window.__persistentContextInit") == "context-init"

            new_page = context.new_page()
            try:
                new_page.goto(data_url("<main>New</main>"))
                assert new_page.evaluate("window.__persistentContextInit") == "context-init"
            finally:
                new_page.close()
        finally:
            context.close()


@case
def context_record_har_omit_content_mode(page):
    with tempfile.TemporaryDirectory() as directory:
        har_path = Path(directory) / "omit.har"
        with header_case_server() as base_url:
            context = page.context.browser.new_context(
                record_har_path=str(har_path),
                record_har_url_filter="**/headers",
                record_har_content="omit",
            )
            recorded_page = context.new_page()
            try:
                response = recorded_page.goto(f"{base_url}/headers")
                response.finished()
            finally:
                context.close()

        har = json.loads(har_path.read_text(encoding="utf-8"))
        content = har["log"]["entries"][0]["response"]["content"]
        assert content["size"] > 0
        assert content["mimeType"] == "application/json"
        assert "text" not in content
        assert "_file" not in content


@case
def context_record_har_minimal_mode(page):
    with tempfile.TemporaryDirectory() as directory:
        har_path = Path(directory) / "minimal.har"
        with header_case_server() as base_url:
            context = page.context.browser.new_context(
                record_har_path=str(har_path),
                record_har_url_filter="**/headers",
                record_har_content="embed",
                record_har_mode="minimal",
            )
            recorded_page = context.new_page()
            try:
                response = recorded_page.goto(f"{base_url}/headers")
                response.finished()
            finally:
                context.close()

        har = json.loads(har_path.read_text(encoding="utf-8"))
        assert "pages" not in har["log"]
        entry = har["log"]["entries"][0]
        assert entry["request"]["headersSize"] == -1
        assert entry["response"]["headersSize"] == -1
        assert entry["response"]["bodySize"] == -1
        assert entry["response"]["content"]["size"] == -1
        assert json.loads(entry["response"]["content"]["text"]) == {"ok": True}


@case
def context_record_har_attach_zip_and_replay(page):
    with tempfile.TemporaryDirectory() as directory:
        har_path = Path(directory) / "attached.zip"
        with header_case_server() as base_url:
            context = page.context.browser.new_context(
                record_har_path=str(har_path),
                record_har_url_filter="**/headers",
                record_har_content="attach",
            )
            recorded_page = context.new_page()
            try:
                response = recorded_page.goto(f"{base_url}/headers")
                response.finished()
            finally:
                context.close()

            assert zipfile.is_zipfile(har_path)
            with zipfile.ZipFile(har_path) as archive:
                names = set(archive.namelist())
                assert "har.har" in names
                har = json.loads(archive.read("har.har").decode("utf-8"))
                content = har["log"]["entries"][0]["response"]["content"]
                attached_file = content["_file"]
                assert attached_file in names
                assert archive.read(attached_file) == b'{"ok":true}'
                assert "text" not in content

            replay_context = page.context.browser.new_context()
            try:
                replay_context.route_from_har(str(har_path), url="**/headers")
                replay_page = replay_context.new_page()
                response = replay_page.goto(f"{base_url}/headers")
                assert response.json() == {"ok": True}
            finally:
                replay_context.close()


@case
def route_from_har_update_minimal_mode(page):
    with tempfile.TemporaryDirectory() as directory:
        har_path = Path(directory) / "update-minimal.har"
        with header_case_server() as base_url:
            context = page.context.browser.new_context()
            try:
                context.route_from_har(
                    str(har_path),
                    url="**/headers",
                    update=True,
                    update_content="embed",
                    update_mode="minimal",
                )
                recorded_page = context.new_page()
                response = recorded_page.goto(f"{base_url}/headers")
                response.finished()
            finally:
                context.close()

        har = json.loads(har_path.read_text(encoding="utf-8"))
        assert "pages" not in har["log"]
        entry = har["log"]["entries"][0]
        assert entry["response"]["bodySize"] == -1
        assert entry["response"]["content"]["size"] == -1
        assert json.loads(entry["response"]["content"]["text"]) == {"ok": True}


@case
def route_from_har_option_edges_match_playwright(page):
    browser = page.context.browser

    def write_empty_har(path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "log": {
                        "version": "1.2",
                        "creator": {"name": "parity", "version": "1.0"},
                        "entries": [],
                    }
                }
            ),
            encoding="utf-8",
        )

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("route_from_har unexpectedly accepted invalid update option")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        empty_har = root / "empty.har"
        write_empty_har(empty_har)

        expect_error(
            lambda: page.route_from_har(str(empty_har), update=True, update_content="bad"),
            "Page.route_from_har: options.content: expected one of (embed|attach|omit)",
        )
        expect_error(
            lambda: page.route_from_har(str(empty_har), update=True, update_mode="bad"),
            "Page.route_from_har: options.mode: expected one of (full|minimal)",
        )

        context = browser.new_context()
        try:
            expect_error(
                lambda: context.route_from_har(str(empty_har), update=True, update_content="bad"),
                "BrowserContext.route_from_har: options.content: expected one of (embed|attach|omit)",
            )
            expect_error(
                lambda: context.route_from_har(str(empty_har), update=True, update_mode="bad"),
                "BrowserContext.route_from_har: options.mode: expected one of (full|minimal)",
            )
        finally:
            context.close()

        with header_case_server() as base_url:
            fallback_page = browser.new_page()
            try:
                fallback_page.route_from_har(str(empty_har), not_found="unknown-mode")
                assert fallback_page.goto(f"{base_url}/headers").json() == {"ok": True}
            finally:
                fallback_page.close()

            fallback_context = browser.new_context()
            try:
                fallback_context.route_from_har(str(empty_har), not_found=123)
                fallback_context_page = fallback_context.new_page()
                assert fallback_context_page.goto(f"{base_url}/headers").json() == {"ok": True}
            finally:
                fallback_context.close()

            omit_har = root / "omit-update.har"
            omit_page = browser.new_page()
            try:
                omit_page.route_from_har(str(omit_har), url="**/headers", update=True, update_content="omit")
                omit_page.goto(f"{base_url}/headers")
            finally:
                omit_page.close()

        omit_content = json.loads(omit_har.read_text(encoding="utf-8"))["log"]["entries"][0]["response"]["content"]
        assert omit_content["mimeType"] == "application/json"
        assert "text" not in omit_content
        assert "_file" not in omit_content


@case
def route_from_har_replay_path_errors_match_playwright(page):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("route_from_har unexpectedly accepted missing HAR path")

    path_values = ("missing.har", [], None, 1, -1, 1.5)
    for raw_path in path_values:
        expect_error(
            lambda raw_path=raw_path: page.route_from_har(raw_path),
            f"Page.route_from_har: ENOENT: no such file or directory, open '{raw_path}'",
        )

    context = browser.new_context()
    try:
        for raw_path in path_values:
            expect_error(
                lambda raw_path=raw_path: context.route_from_har(raw_path),
                f"BrowserContext.route_from_har: ENOENT: no such file or directory, open '{raw_path}'",
            )
    finally:
        context.close()


@case
def route_from_har_update_path_scalar_coercion_matches_playwright(page):
    browser = page.context.browser

    def assert_har_recorded(path: Path) -> None:
        har = json.loads(path.read_text(encoding="utf-8"))
        entries = har["log"]["entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["response"]["content"]["size"] == -1
        assert json.loads(entry["response"]["content"]["text"]) == {"ok": True}

    def exercise(scope: str, raw_path: Any) -> None:
        with tempfile.TemporaryDirectory() as directory, header_case_server() as base_url:
            root = Path(directory)
            previous = os.getcwd()
            os.chdir(root)
            context = browser.new_context()
            closed = False
            try:
                routed_page = context.new_page()
                if scope == "context":
                    context.route_from_har(
                        raw_path,
                        url="**/headers",
                        update=True,
                        update_content="embed",
                        update_mode="minimal",
                    )
                else:
                    routed_page.route_from_har(
                        raw_path,
                        url="**/headers",
                        update=True,
                        update_content="embed",
                        update_mode="minimal",
                    )
                response = routed_page.goto(f"{base_url}/headers")
                response.finished()
                context.close()
                closed = True
            finally:
                if not closed:
                    try:
                        context.close()
                    except Exception:
                        pass
                os.chdir(previous)
            assert_har_recorded(root / str(raw_path))

    for scope in ("context", "page"):
        for raw_path in (123, False, 0):
            exercise(scope, raw_path)


@case
def route_from_har_replays_exact_request(page):
    body = "<title>HAR Replay</title><main>from har</main>"
    with tempfile.TemporaryDirectory() as directory:
        har_path = Path(directory) / "replay.har"
        har_path.write_text(
            json.dumps(
                {
                    "log": {
                        "version": "1.2",
                        "creator": {"name": "parity", "version": "1.0"},
                        "entries": [
                            {
                                "startedDateTime": "2026-01-01T00:00:00.000Z",
                                "time": 0,
                                "request": {
                                    "method": "GET",
                                    "url": "http://example.test/har-replay",
                                    "httpVersion": "HTTP/1.1",
                                    "cookies": [],
                                    "headers": [],
                                    "queryString": [],
                                    "headersSize": -1,
                                    "bodySize": 0,
                                },
                                "response": {
                                    "status": 200,
                                    "statusText": "OK",
                                    "httpVersion": "HTTP/1.1",
                                    "cookies": [],
                                    "headers": [{"name": "Content-Type", "value": "text/html"}],
                                    "content": {
                                        "size": len(body),
                                        "mimeType": "text/html",
                                        "text": body,
                                    },
                                    "redirectURL": "",
                                    "headersSize": -1,
                                    "bodySize": len(body),
                                },
                                "cache": {},
                                "timings": {"send": 0, "wait": 0, "receive": 0},
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        page.route_from_har(str(har_path))
        response = page.goto("http://example.test/har-replay")
        assert response.status == 200
        assert page.title() == "HAR Replay"
        assert page.locator("main").inner_text() == "from har"


@case
def websocket_event_and_frame_roundtrip(page):
    with websocket_echo_server() as (ws_url, connections):
        with page.expect_websocket(lambda websocket: websocket.url == f"{ws_url}?parity") as websocket_info:
            page.evaluate("(url) => { window.__paritySocket = new WebSocket(url + '?parity'); }", ws_url)

        websocket = websocket_info.value
        handle = page.wait_for_function("() => window.__paritySocket.readyState === WebSocket.OPEN", timeout=3_000)
        handle.dispose()

        with websocket.expect_event("framereceived") as received_info:
            with websocket.expect_event("framesent") as sent_info:
                page.evaluate("() => window.__paritySocket.send('client-ping')")

        assert websocket.url == f"{ws_url}?parity"
        assert not websocket.is_closed()
        assert sent_info.value == "client-ping"
        assert received_info.value == "echo:client-ping"
        assert any(item == ("message", "/socket?parity", "client-ping") for item in connections)

        with page.expect_websocket(lambda closing_websocket: closing_websocket.url == f"{ws_url}?closing") as closing_info:
            page.evaluate("(url) => { window.__closingParitySocket = new WebSocket(url + '?closing'); }", ws_url)
        closing_websocket = closing_info.value
        closing_handle = page.wait_for_function("() => window.__closingParitySocket.readyState === WebSocket.OPEN", timeout=3_000)
        closing_handle.dispose()
        try:
            with closing_websocket.expect_event("framereceived", timeout=3_000):
                page.evaluate("() => window.__closingParitySocket.close()")
        except Exception as exc:
            assert str(exc).splitlines()[0] == "Socket closed"
        else:
            raise AssertionError("websocket non-close waiter did not reject when socket closed")
        assert closing_websocket.is_closed()

        with websocket.expect_event("close") as close_info:
            page.evaluate("() => window.__paritySocket.close()")
        assert close_info.value is websocket
        try:
            websocket.wait_for_event("close", timeout=10)
        except Exception as exc:
            assert exc.__class__.__name__ == "TimeoutError"
            assert str(exc).splitlines()[0] == 'Timeout 10ms exceeded while waiting for event "close"'
        else:
            raise AssertionError("closed websocket close event unexpectedly replayed")


@case
def websocket_unknown_event_waiters_timeout_like_playwright(page):
    def expect_timeout(operation):
        try:
            operation()
        except Exception as exc:
            assert exc.__class__.__name__ == "TimeoutError"
            assert str(exc).splitlines()[0] == 'Timeout 5ms exceeded while waiting for event "unknown-event"'
            return
        raise AssertionError("unknown websocket event waiter unexpectedly resolved")

    with websocket_echo_server() as (ws_url, _):
        with page.expect_websocket(lambda websocket: websocket.url == f"{ws_url}?unknown-event") as websocket_info:
            page.evaluate("(url) => { window.__unknownEventSocket = new WebSocket(url + '?unknown-event'); }", ws_url)

        websocket = websocket_info.value
        page.wait_for_function("() => window.__unknownEventSocket.readyState === WebSocket.OPEN", timeout=3_000).dispose()

        expect_timeout(lambda: websocket.wait_for_event("unknown-event", timeout=5))

        def expect_context_timeout():
            with websocket.expect_event("unknown-event", timeout=5):
                pass

        expect_timeout(expect_context_timeout)
        page.evaluate("() => window.__unknownEventSocket.close()")


@case
def page_wait_for_websocket_and_worker_events(page):
    with websocket_echo_server() as (ws_url, connections):
        page.evaluate(
            "(url) => setTimeout(() => { window.__waitSocket = new WebSocket(url + '?direct-wait'); }, 20)",
            ws_url,
        )
        websocket = page.wait_for_event(
            "websocket",
            lambda item: item.url == f"{ws_url}?direct-wait",
            timeout=3_000,
        )
        assert websocket.url == f"{ws_url}?direct-wait"
        page.wait_for_function("() => window.__waitSocket.readyState === WebSocket.OPEN", timeout=3_000).dispose()

        with websocket.expect_event("framesent") as sent_info:
            with websocket.expect_event("framereceived") as received_info:
                page.evaluate("() => window.__waitSocket.send('direct-wait')")
        assert sent_info.value == "direct-wait"
        assert received_info.value == "echo:direct-wait"
        assert any(item == ("message", "/socket?direct-wait", "direct-wait") for item in connections)

    page.evaluate(
        """() => setTimeout(() => {
        const source = `self.ready = true; setInterval(() => {}, 1000);`;
        const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
        window.__waitWorker = new Worker(url);
        }, 20)"""
    )
    worker = page.wait_for_event("worker", lambda item: item.url.startswith("blob:"), timeout=3_000)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if worker.evaluate("() => self.ready === true"):
            break
        time.sleep(0.05)
    assert worker.evaluate("() => self.ready === true") is True

    with worker.expect_event("close") as close_info:
        page.evaluate("() => window.__waitWorker.terminate()")
    assert close_info.value is worker


@case
def route_web_socket_mocks_browser_socket(page):
    received = []

    def handler(route):
        assert route.url == "ws://example.test/parity-mock"

        def on_message(message):
            received.append(message)
            route.send(f"server:{message}")

        route.on_message(on_message)

    context = page.context.browser.new_context()
    try:
        context.route_web_socket("**/parity-mock", handler)
        routed_page = context.new_page()
        routed_page.set_content("<main>route websocket parity</main>")
        routed_page.evaluate(
            """() => new Promise((resolve) => {
            const ws = new WebSocket("ws://example.test/parity-mock");
            window.__routeMessages = [];
            ws.onopen = () => ws.send("client-ready");
            ws.onmessage = event => {
              window.__routeMessages.push(event.data);
              if (event.data === "server:client-ready") resolve();
            };
            ws.onerror = resolve;
            setTimeout(resolve, 1000);
            })"""
        )

        assert routed_page.evaluate("() => window.__routeMessages") == ["server:client-ready"]
        assert received == ["client-ready"]
    finally:
        context.close()


@case
def route_web_socket_connect_to_server_auto_forwarding(page):
    with websocket_echo_server() as (ws_url, connections):
        page.route_web_socket("**/socket?auto-forward", lambda route: route.connect_to_server())
        page.set_content("<main>route websocket auto forwarding parity</main>")
        messages = page.evaluate(
            f"""() => new Promise((resolve) => {{
            const ws = new WebSocket({json.dumps(ws_url + '?auto-forward')});
            const messages = [];
            ws.onmessage = event => {{
              messages.push(event.data);
              resolve(messages);
            }};
            ws.onopen = () => ws.send("auto-ping");
            ws.onerror = () => resolve(messages);
            setTimeout(() => resolve(messages), 1000);
            }})"""
        )

        assert messages == ["echo:auto-ping"]
        assert any(item == ("message", "/socket?auto-forward", "auto-ping") for item in connections)


@case
def route_web_socket_blob_payload_and_validation(page):
    received = []

    def handler(route):
        def on_message(message):
            received.append(message)
            if isinstance(message, (bytes, bytearray)) and bytes(message) == bytes([7, 6, 5]):
                route.send(bytes([3, 4]))

        route.on_message(on_message)

    context = page.context.browser.new_context()
    try:
        context.route_web_socket("**/parity-binary", handler)
        routed_page = context.new_page()
        routed_page.set_content("<main>route websocket binary parity</main>")
        result = routed_page.evaluate(
            """async () => {
            const result = { messages: [] };
            try {
              new WebSocket("ws://example.test/parity-binary", ["chat", "chat"]);
              result.duplicateProtocol = "none";
            } catch (error) {
              result.duplicateProtocol = error.name;
            }
            const ws = new WebSocket("ws://example.test/parity-binary", "chat");
            ws.binaryType = "arraybuffer";
            await new Promise(resolve => ws.onopen = resolve);
            try {
              ws.close(1001);
              result.invalidClose = "none";
            } catch (error) {
              result.invalidClose = error.name;
            }
            const messagePromise = new Promise(resolve => {
              ws.onmessage = event => {
                result.messages.push(Array.from(new Uint8Array(event.data)));
                resolve();
              };
            });
            ws.send(new Blob([new Uint8Array([7, 6, 5]).buffer]));
            await messagePromise;
            const closePromise = new Promise(resolve => {
              ws.onclose = event => {
                result.close = [event.code, event.reason];
                resolve();
              };
            });
            ws.close(3001, "done");
            await closePromise;
            return result;
            }"""
        )

        assert result == {
            "messages": [[3, 4]],
            "duplicateProtocol": "none",
            "invalidClose": "Error",
            "close": [3001, "done"],
        }
        assert any(isinstance(message, (bytes, bytearray)) and bytes(message) == bytes([7, 6, 5]) for message in received)
    finally:
        context.close()


@case
def page_console_messages_history_and_clear(page):
    page.set_content("<main>console history</main>")
    page.evaluate("() => { console.log('first history'); console.info('second history'); }")

    deadline = time.monotonic() + 3
    messages = []
    while time.monotonic() < deadline:
        messages = page.console_messages()
        if len(messages) >= 2:
            break
        time.sleep(0.05)
    assert [message.text for message in messages[-2:]] == ["first history", "second history"]
    assert [message.type for message in messages[-2:]] == ["log", "info"]
    assert all(message.page is page for message in messages[-2:])

    page.clear_console_messages()
    assert page.console_messages() == []


@case
def page_wait_for_event_console_does_not_replay_history(page):
    buffered_text = "buffered parity console"
    future_text = "future parity console"

    page.evaluate("(text) => console.log(text)", buffered_text)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if buffered_text in [message.text for message in page.console_messages(filter="all")]:
            break
        time.sleep(0.05)

    try:
        page.wait_for_event("console", lambda message: message.text == buffered_text, timeout=100)
    except Exception as exc:
        assert str(exc).splitlines()[0] == 'Timeout 100ms exceeded while waiting for event "console"'
    else:
        raise AssertionError("page.wait_for_event('console') replayed an old console message")

    with page.expect_event("console", lambda item: item.text == future_text, timeout=3_000) as info:
        page.evaluate("(text) => setTimeout(() => console.log(text), 20)", future_text)
    message = info.value
    assert message.text == future_text
    assert message.page is page


@case
def console_message_args_are_jshandles_for_skyvern_exfiltration(page):
    marker = "__skyvern_exfil__"
    payload = {"kind": "event", "count": 3, "nested": {"ok": True}}

    with page.expect_console_message(lambda message: message.text.startswith(marker), timeout=3_000) as info:
        page.evaluate(
            """([marker, payload]) => {
            console.log(marker, payload, [1, 2, 3]);
            }""",
            [marker, payload],
        )

    message = info.value
    assert len(message.args) == 3
    assert all(hasattr(arg, "json_value") for arg in message.args)
    assert [arg.json_value() for arg in message.args] == [marker, payload, [1, 2, 3]]
    assert message.page is page


@case
def context_console_message_captures_immediate_popup_console(page):
    message_text = "popup console parity"
    page.set_content(
        f"""
        <button id="open" onclick="
          const popup = window.open('about:blank');
          popup.document.write(`<script>console.log('popup console parity')</script>`);
          popup.document.close();
        ">Open</button>
        """
    )

    with page.context.expect_console_message(lambda message: message.text == message_text, timeout=3_000) as info:
        page.click("#open")

    message = info.value
    assert message.text == message_text
    assert message.page is not None
    assert message.page.context is page.context
    assert message.page.opener() is page


@case
def page_console_messages_since_navigation_filter(page):
    before = "console before navigation filter"
    after = "console after navigation filter"
    after_set_content = "console after set content filter"
    page.set_content("<main>console filter</main>")
    page.evaluate("(text) => console.log(text)", before)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if before in [message.text for message in page.console_messages(filter="all")]:
            break
        time.sleep(0.05)

    page.goto(data_url(f"<script>console.log({json.dumps(after)})</script><main>after</main>"))
    deadline = time.monotonic() + 3
    current_texts = []
    while time.monotonic() < deadline:
        current_texts = [message.text for message in page.console_messages()]
        if after in current_texts:
            break
        time.sleep(0.05)

    all_texts = [message.text for message in page.console_messages(filter="all")]
    since_texts = [message.text for message in page.console_messages(filter="since-navigation")]
    assert before in all_texts
    assert after in all_texts
    assert before not in current_texts
    assert after in current_texts
    assert current_texts == since_texts

    page.set_content(f"<script>console.log({json.dumps(after_set_content)})</script><main>set</main>")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        current_texts = [message.text for message in page.console_messages()]
        if after_set_content in current_texts:
            break
        time.sleep(0.05)

    assert before not in current_texts
    assert after in current_texts
    assert after_set_content in current_texts


@case
def page_requests_history_records_recent_requests(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        assert page.evaluate("() => fetch('/headers').then(response => response.text())") == '{"ok":true}'

        deadline = time.monotonic() + 3
        requests = []
        while time.monotonic() < deadline:
            requests = page.requests()
            if any(request.url == f"{base_url}/headers" for request in requests):
                break
            time.sleep(0.05)
        assert any(request.url.rstrip("/") == base_url and request.method == "GET" for request in requests)
        assert any(request.url == f"{base_url}/headers" and request.method == "GET" for request in requests)


@case
def page_requests_history_includes_expect_response_request(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        with page.expect_response(lambda response: response.url.endswith("/headers"), timeout=3_000):
            page.evaluate("() => fetch('/headers')")

        requests = page.requests()
        assert any(request.url.rstrip("/") == base_url and request.method == "GET" for request in requests)
        assert any(request.url == f"{base_url}/headers" and request.method == "GET" for request in requests)


@case
def page_requests_history_is_bounded_to_recent_requests(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        for index in range(105):
            page.goto(f"{base_url}/query?bounded={index}", wait_until="commit")

        deadline = time.monotonic() + 3
        requests = []
        recent_urls = []
        while time.monotonic() < deadline:
            requests = page.requests()
            recent_urls = [request.url for request in requests]
            if any(url == f"{base_url}/query?bounded=104" for url in recent_urls):
                break
            time.sleep(0.05)

        assert 0 < len(requests) <= 100
        assert f"{base_url}/query?bounded=104" in recent_urls
        assert f"{base_url}/query?bounded=0" not in recent_urls
        assert base_url.rstrip("/") not in {url.rstrip("/") for url in recent_urls}


@case
def page_once_and_remove_listener_for_console(page):
    seen = []
    page.once("console", lambda message: seen.append(message.text))
    page.evaluate("() => { console.log('once first'); console.log('once second'); }")

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not seen:
        time.sleep(0.05)

    assert seen == ["once first"]

    removed = []

    def removed_handler(message):
        removed.append(message.text)

    page.on("console", removed_handler)
    page.remove_listener("console", removed_handler)
    page.evaluate("() => console.log('removed listener')")
    time.sleep(0.1)

    assert removed == []


@case
def page_pause_returns_in_headless_mode(page):
    started = time.monotonic()
    page.pause()
    assert time.monotonic() - started < 0.5


@case
def page_crash_event_for_chromium_target(page):
    context = page.context.browser.new_context()
    crashed_page = context.new_page()
    seen = []

    try:
        crashed_page.on("crash", lambda event_page: seen.append(event_page is crashed_page))
        with crashed_page.expect_event("crash", timeout=7_000) as crash_info:
            try:
                crashed_page.goto("chrome://crash", timeout=5_000)
            except Exception:
                pass

        assert crash_info.value is crashed_page
        assert seen == [True]
        try:
            crashed_page.wait_for_event("crash", timeout=10)
        except Exception as exc:
            assert exc.__class__.__name__ == "TimeoutError"
            assert str(exc).splitlines()[0] == 'Timeout 10ms exceeded while waiting for event "crash"'
        else:
            raise AssertionError("already-fired crash event unexpectedly replayed")
    finally:
        context.close()


@case
def page_event_waiters_reject_on_page_crash(page):
    context = page.context.browser.new_context()
    crashed_page = context.new_page()
    result: dict[str, tuple[str, str]] = {}

    try:
        try:
            with crashed_page.expect_event("console", timeout=7_000):
                try:
                    crashed_page.goto("chrome://crash", timeout=5_000)
                except Exception as exc:
                    result["goto"] = (exc.__class__.__name__, str(exc).splitlines()[0])
        except Exception as exc:
            result["waiter"] = (exc.__class__.__name__, str(exc).splitlines()[0])
        else:
            result["waiter"] = ("resolved", "")
        assert result.get("goto") is not None
        assert result["goto"][0] == "Error"
        assert result["goto"][1] == "Page crashed" or "net::ERR_ABORTED" in result["goto"][1]
        assert result.get("waiter") == ("Error", "Page crashed")
    finally:
        context.close()


@case
def page_errors_history_and_clear(page):
    page.set_content("<main>page error history</main>")
    page.evaluate("() => setTimeout(() => { throw new Error('parity page boom'); }, 0)")

    deadline = time.monotonic() + 3
    errors = []
    while time.monotonic() < deadline:
        errors = page.page_errors()
        if errors:
            break
        time.sleep(0.05)

    assert [str(error) for error in errors[-1:]] == ["parity page boom"]
    page.clear_page_errors()
    assert page.page_errors() == []


@case
def page_errors_since_navigation_filter(page):
    before = "page error before navigation filter"
    after = "page error after navigation filter"
    after_set_content = "page error after set content filter"
    page.set_content("<main>page error filter</main>")
    page.evaluate("(text) => setTimeout(() => { throw new Error(text); }, 0)", before)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if before in [str(error) for error in page.page_errors(filter="all")]:
            break
        time.sleep(0.05)

    page.goto(
        data_url(
            f"<script>setTimeout(() => {{ throw new Error({json.dumps(after)}); }}, 0)</script><main>after</main>"
        )
    )
    deadline = time.monotonic() + 3
    current_errors = []
    while time.monotonic() < deadline:
        current_errors = [str(error) for error in page.page_errors()]
        if after in current_errors:
            break
        time.sleep(0.05)

    all_errors = [str(error) for error in page.page_errors(filter="all")]
    since_errors = [str(error) for error in page.page_errors(filter="since-navigation")]
    assert before in all_errors
    assert after in all_errors
    assert before not in current_errors
    assert after in current_errors
    assert current_errors == since_errors

    page.set_content(
        f"<script>setTimeout(() => {{ throw new Error({json.dumps(after_set_content)}); }}, 0)</script><main>set</main>"
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        current_errors = [str(error) for error in page.page_errors()]
        if after_set_content in current_errors:
            break
        time.sleep(0.05)

    assert before not in current_errors
    assert after in current_errors
    assert after_set_content in current_errors


@case
def request_gc_collects_unreferenced_objects(page):
    page.set_content("<main>gc parity</main>")
    page.evaluate(
        """() => {
        window.__parityWeak = new WeakRef({ payload: new Array(1000).fill('x') });
        window.__parityCollected = () => window.__parityWeak.deref() === undefined;
        }"""
    )

    for _ in range(3):
        page.request_gc()
        if page.evaluate("() => window.__parityCollected()"):
            break

    assert page.evaluate("() => window.__parityCollected()")


@case
def page_clock_controls_date_and_timers(page):
    page.set_content("<main>clock parity</main>")

    page.clock.install(time="2020-01-01T00:00:00Z")
    assert_near(page.evaluate("() => Date.now()"), 1577836800000)
    called_date = page.evaluate(
        """() => ({
        noArgs: Date.parse(Date()),
        withArgs: Date.parse(Date(1999, 0, 1)),
        constructed: new Date().getTime(),
        now: Date.now(),
        })"""
    )
    assert_near(called_date["noArgs"], 1577836800000)
    assert_near(called_date["withArgs"], 1577836800000)
    assert_near(called_date["constructed"], 1577836800000)
    assert_near(called_date["now"], 1577836800000)
    date_shape = page.evaluate(
        """() => ({
        name: Date.name,
        length: Date.length,
        isFake: Date.isFake,
        toStringValue: Date.toString(),
        toStringName: Date.toString.name,
        ownNames: Object.getOwnPropertyNames(Date).sort(),
        instance: new Date() instanceof Date,
        constructorIsDate: new Date().constructor === Date,
        })"""
    )
    assert date_shape == {
        "name": "ClockDate",
        "length": 7,
        "isFake": True,
        "toStringValue": "function Date() { [native code] }",
        "toStringName": "",
        "ownNames": ["UTC", "isFake", "length", "name", "now", "parse", "prototype", "toString"],
        "instance": True,
        "constructorIsDate": False,
    }
    perf_clock = page.evaluate(
        """() => ({
        now: Date.now(),
        perf: performance.now(),
        origin: performance.timeOrigin,
        sum: performance.timeOrigin + performance.now(),
        })"""
    )
    assert_near(perf_clock["origin"], 1577836800000)
    assert_near(perf_clock["sum"], perf_clock["now"])
    mark_clock = page.evaluate(
        """() => {
        performance.clearMarks();
        const mark = performance.mark('rustwright-mark', { startTime: 123, detail: { ignored: true } });
        return {
          name: mark.name,
          type: mark.entryType,
          start: mark.startTime,
          duration: mark.duration,
          detail: mark.detail,
          json: mark.toJSON(),
          namedEntries: performance.getEntriesByName('rustwright-mark'),
          typedEntries: performance.getEntriesByType('mark'),
        };
        }"""
    )
    assert mark_clock == {
        "name": "rustwright-mark",
        "type": "mark",
        "start": 0,
        "duration": 0,
        "detail": None,
        "json": '{"name":"rustwright-mark","entryType":"mark","startTime":0,"duration":0}',
        "namedEntries": [],
        "typedEntries": [],
    }
    measure_clock = page.evaluate(
        """() => {
        performance.clearMeasures();
        const measure = performance.measure('rustwright-measure', { start: 25, end: 75, detail: { ignored: true } });
        let missingError = null;
        try {
          performance.measure('missing-measure', 'missing-start', 'missing-end');
        } catch (error) {
          missingError = String(error);
        }
        return {
          name: measure.name,
          type: measure.entryType,
          start: measure.startTime,
          duration: measure.duration,
          detail: measure.detail,
          json: measure.toJSON(),
          missingError,
          namedEntries: performance.getEntriesByName('rustwright-measure'),
          typedEntries: performance.getEntriesByType('measure'),
        };
        }"""
    )
    assert measure_clock == {
        "name": "rustwright-measure",
        "type": "measure",
        "start": 0,
        "duration": 50,
        "detail": None,
        "json": '{"name":"rustwright-measure","entryType":"measure","startTime":0,"duration":50}',
        "missingError": None,
        "namedEntries": [],
        "typedEntries": [],
    }
    timer_handles = page.evaluate(
        """() => {
        const timeoutA = setTimeout(() => {}, 100);
        const timeoutB = setTimeout(() => {}, 100);
        const interval = setInterval(() => {}, 100);
        const raf = requestAnimationFrame(() => {});
        const idle = requestIdleCallback(() => {});
        clearTimeout(timeoutA);
        clearTimeout(timeoutB);
        clearInterval(interval);
        cancelAnimationFrame(raf);
        cancelIdleCallback(idle);
        return {
          ids: [timeoutA, timeoutB, interval, raf, idle],
          types: [typeof timeoutA, typeof timeoutB, typeof interval, typeof raf, typeof idle],
        };
        }"""
    )
    assert timer_handles == {
        "ids": [1000000000000, 1000000000001, 1000000000002, 1000000000003, 1000000000004],
        "types": ["number", "number", "number", "number", "number"],
    }
    event_clock = page.evaluate(
        """() => {
        const constructed = new Event('rustwright');
        const constructedStamp = constructed.timeStamp;
        let dispatchedStamp = null;
        document.body.addEventListener('click', event => dispatchedStamp = event.timeStamp, { once: true });
        document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        return {
          constructed: constructedStamp,
          reread: constructed.timeStamp,
          dispatched: dispatchedStamp,
          perf: performance.now(),
          eventInstance: constructed instanceof Event,
          mouseInstance: new MouseEvent('click') instanceof MouseEvent,
        };
        }"""
    )
    assert_near(event_clock["constructed"], event_clock["reread"])
    assert_near(event_clock["dispatched"], event_clock["perf"])
    assert event_clock["eventInstance"] is True
    assert event_clock["mouseInstance"] is True
    intl_default = page.evaluate(
        """() => {
        const formatter = new Intl.DateTimeFormat('en-US', {
          timeZone: 'UTC',
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
        });
        const partMap = parts => Object.fromEntries(
          parts.filter(part => part.type !== 'literal').map(part => [part.type, part.value])
        );
        return {
          constructed: partMap(formatter.formatToParts()),
          called: partMap(Intl.DateTimeFormat('en-US', {
            timeZone: 'UTC',
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
          }).formatToParts()),
          explicit: partMap(formatter.formatToParts(new Date(Date.UTC(1999, 0, 2)))),
          zero: partMap(formatter.formatToParts(0)),
          nullValue: partMap(formatter.formatToParts(null)),
        };
        }"""
    )
    assert intl_default["constructed"] == {"month": "01", "day": "01", "year": "2020"}
    assert intl_default["called"] == {"month": "01", "day": "01", "year": "2020"}
    assert intl_default["explicit"] == {"month": "01", "day": "02", "year": "1999"}
    assert intl_default["zero"] == {"month": "01", "day": "01", "year": "2020"}
    assert intl_default["nullValue"] == {"month": "01", "day": "01", "year": "2020"}

    unpaused_system_before = page.evaluate(
        "() => ({ date: Date.now(), perf: performance.now(), origin: performance.timeOrigin })"
    )
    page.clock.set_system_time("2020-01-01T00:05:00Z")
    unpaused_system_after = page.evaluate(
        "() => ({ date: Date.now(), perf: performance.now(), origin: performance.timeOrigin })"
    )
    assert_near(unpaused_system_after["date"], 1577837100000)
    assert_near(unpaused_system_after["origin"], 1577836800000)
    assert_near(unpaused_system_after["perf"], unpaused_system_before["perf"], 100)

    browser = page.context.browser
    assert browser is not None
    timer_page = browser.new_page()
    page = timer_page
    page.set_content("<main>clock timer parity</main>")
    page.clock.install(time="2020-01-01T00:00:00Z")

    page.clock.pause_at("2020-01-01T00:00:00Z")
    page.evaluate(
        """() => {
        window.__clockAbortSignal = { aborted: false, name: '', message: '' };
        const signal = AbortSignal.timeout(250);
        signal.addEventListener('abort', () => {
          window.__clockAbortSignal = {
            aborted: signal.aborted,
            name: signal.reason && signal.reason.name,
            message: signal.reason && signal.reason.message,
          };
        });
        }"""
    )
    page.clock.run_for(249)
    assert page.evaluate("() => window.__clockAbortSignal.aborted") is False
    page.clock.run_for(1)
    assert page.evaluate("() => window.__clockAbortSignal") == {
        "aborted": True,
        "name": "TimeoutError",
        "message": "signal timed out",
    }

    page.evaluate(
        """() => {
        window.__clockHeldEvent = new Event('rustwright');
        window.__clockHeldEventStamp = window.__clockHeldEvent.timeStamp;
        window.__clockFired = [];
        setTimeout(() => __clockFired.push(Date.now()), 1000);
        }"""
    )
    page.clock.run_for(1000)
    fired = page.evaluate("() => window.__clockFired")
    assert len(fired) == 1
    assert_near(fired[0], 1577836801250)
    held_event_clock = page.evaluate(
        """() => ({
        before: window.__clockHeldEventStamp,
        after: window.__clockHeldEvent.timeStamp,
        perf: performance.now(),
        })"""
    )
    assert_near(held_event_clock["after"], held_event_clock["before"])
    assert held_event_clock["perf"] > held_event_clock["after"] + 900
    perf_clock = page.evaluate(
        """() => ({
        now: Date.now(),
        origin: performance.timeOrigin,
        sum: performance.timeOrigin + performance.now(),
        })"""
    )
    assert_near(perf_clock["origin"], 1577836800000)
    assert_near(perf_clock["sum"], perf_clock["now"])

    page.clock.pause_at("2020-01-01T00:00:02Z")
    page.evaluate(
        """() => {
        window.__clockFastForward = [];
        const intervalId = setInterval(() => {
          __clockFastForward.push({ kind: 'interval', now: Date.now() });
          clearInterval(intervalId);
        }, 100);
        setTimeout(() => __clockFastForward.push({ kind: 'timeout', now: Date.now() }), 50);
        }"""
    )
    page.clock.fast_forward(500)
    fast_forward_events = page.evaluate("() => window.__clockFastForward")
    assert [event["kind"] for event in fast_forward_events] == ["interval", "timeout"]
    assert all(abs(event["now"] - 1577836802500) < 25 for event in fast_forward_events)

    page.evaluate(
        """() => {
        window.__clockRunFor = [];
        const intervalId = setInterval(() => {
          __clockRunFor.push(Date.now());
          if (__clockRunFor.length >= 3) clearInterval(intervalId);
        }, 100);
        }"""
    )
    page.clock.run_for(350)
    assert page.evaluate("() => window.__clockRunFor") == [
        1577836802600,
        1577836802700,
        1577836802800,
    ]

    callback_base = page.evaluate("() => ({ dateNow: Date.now(), performanceNow: Math.round(performance.now()) })")
    page.evaluate(
        """() => {
        window.__clockCallbacks = [];
        requestIdleCallback(deadline => __clockCallbacks.push({
          kind: 'idle',
          didTimeout: deadline.didTimeout,
          timeRemaining: Math.round(deadline.timeRemaining()),
          dateNow: Date.now(),
          performanceNow: Math.round(performance.now()),
        }));
        requestIdleCallback(deadline => __clockCallbacks.push({
          kind: 'idle-timeout',
          didTimeout: deadline.didTimeout,
          timeRemaining: Math.round(deadline.timeRemaining()),
          dateNow: Date.now(),
          performanceNow: Math.round(performance.now()),
        }), { timeout: 20 });
        requestAnimationFrame(timestamp => __clockCallbacks.push({
          kind: 'raf',
          timestamp: Math.round(timestamp),
          dateNow: Date.now(),
          performanceNow: Math.round(performance.now()),
        }));
        }"""
    )
    page.clock.run_for(16)
    callbacks = page.evaluate("() => window.__clockCallbacks")
    assert [callback["kind"] for callback in callbacks] == ["idle", "raf"]
    assert callbacks[0]["didTimeout"] is False
    assert callbacks[0]["timeRemaining"] == 0
    assert_near(callbacks[0]["performanceNow"], callback_base["performanceNow"])
    assert_near(callbacks[1]["timestamp"], callback_base["performanceNow"] + 16)
    assert_near(callbacks[1]["performanceNow"], callback_base["performanceNow"] + 16)
    assert_near(callbacks[1]["dateNow"], callback_base["dateNow"] + 16)
    page.clock.run_for(4)
    callbacks = page.evaluate("() => window.__clockCallbacks")
    assert [callback["kind"] for callback in callbacks] == ["idle", "raf", "idle-timeout"]
    assert callbacks[2]["didTimeout"] is False
    assert callbacks[2]["timeRemaining"] == 0
    assert_near(callbacks[2]["performanceNow"], callback_base["performanceNow"] + 20)
    assert_near(callbacks[2]["dateNow"], callback_base["dateNow"] + 20)
    later_mark = page.evaluate(
        """() => ({
        now: performance.now(),
        start: performance.mark('later-mark').startTime,
        entries: performance.getEntriesByType('mark').length,
        })"""
    )
    assert later_mark["entries"] == 0
    assert later_mark["now"] > callback_base["performanceNow"]
    assert later_mark["start"] == 0
    later_measure = page.evaluate(
        """() => ({
        now: performance.now(),
        start: performance.measure('later-measure').startTime,
        duration: performance.measure('later-measure-2', { start: 0, end: performance.now() }).duration,
        entries: performance.getEntriesByType('measure').length,
        })"""
    )
    assert later_measure["entries"] == 0
    assert later_measure["now"] > callback_base["performanceNow"]
    assert later_measure["start"] == 0
    assert later_measure["duration"] == 50

    system_before = page.evaluate("() => ({ date: Date.now(), perf: performance.now(), origin: performance.timeOrigin })")
    page.clock.set_system_time("2020-01-01T00:10:00Z")
    system_after = page.evaluate("() => ({ date: Date.now(), perf: performance.now(), origin: performance.timeOrigin })")
    assert_near(system_after["date"], 1577837400000)
    assert_near(system_after["origin"], 1577836800000)
    assert_near(system_after["perf"], system_before["perf"], 100)
    page.clock.run_for(500)
    system_advanced = page.evaluate("() => ({ date: Date.now(), perf: performance.now(), origin: performance.timeOrigin })")
    assert_near(system_advanced["date"], 1577837400500)
    assert_near(system_advanced["perf"], system_after["perf"] + 500)
    assert_near(system_advanced["origin"], 1577836800000)

    fixed_perf_before = page.evaluate("() => performance.now()")
    page.clock.set_fixed_time("2021-02-03T04:05:06Z")
    assert page.evaluate("() => new Date().toISOString()") == "2021-02-03T04:05:06.000Z"
    fixed_perf_after = page.evaluate("() => performance.now()")
    assert_near(fixed_perf_after, fixed_perf_before, 100)
    assert page.evaluate(
        "() => new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', year: 'numeric' }).format()"
    ) == "2021"
    timer_page.close()


@case
def clock_time_and_tick_validation_matches_playwright(page):
    def expect_error(operation, expected, exc_type=None):
        try:
            operation()
        except Exception as exc:
            if exc_type is not None:
                assert type(exc) is exc_type
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("clock operation unexpectedly accepted invalid value")

    page.clock.set_fixed_time(1)
    assert page.evaluate("() => Date.now()") == 1000

    local_epoch_ms = int(datetime(2020, 1, 1, 0, 0, 0).timestamp() * 1000)
    page.clock.set_fixed_time(datetime(2020, 1, 1, 0, 0, 0))
    assert page.evaluate("() => Date.now()") == local_epoch_ms

    page.clock.set_fixed_time("2020-01-01T00:00:00Z")
    assert page.evaluate("() => Date.now()") == 1577836800000

    expect_error(lambda: page.clock.install(time="bad"), "Clock.install: Invalid date: bad")
    expect_error(lambda: page.clock.set_system_time("bad"), "Clock.set_system_time: Invalid date: bad")
    expect_error(lambda: page.clock.pause_at("bad"), "Clock.pause_at: Invalid date: bad")

    page.clock.install(time=0)
    page.clock.fast_forward("01:02")
    assert_near(page.evaluate("() => Date.now()"), 62_000, 50)

    expect_error(
        lambda: page.clock.fast_forward("1s"),
        "Clock.fast_forward: Clock only understands numbers, 'mm:ss' and 'hh:mm:ss'",
    )
    expect_error(
        lambda: page.clock.run_for("bad"),
        "Clock.run_for: Clock only understands numbers, 'mm:ss' and 'hh:mm:ss'",
    )
    expect_error(
        lambda: page.clock.fast_forward(1.2),
        "Clock.fast_forward: ticks_string: expected string, got number",
    )
    expect_error(
        lambda: page.clock.fast_forward(True),
        "Clock.fast_forward: ticks_number: expected float, got boolean",
    )
    expect_error(
        lambda: page.clock.fast_forward(-1),
        "Clock.fast_forward: Error: Cannot fast-forward to the past",
    )
    expect_error(
        lambda: page.clock.run_for(-1),
        "Clock.run_for: TypeError: Negative ticks are not supported",
    )


@case
def clock_resume_before_install_uses_epoch_baseline(page):
    page.set_content("<main>clock auto install</main>")

    page.clock.resume()
    snapshot = page.evaluate(
        """() => ({
        now: Date.now(),
        date: new Date().toISOString(),
        perf: performance.now(),
        origin: performance.timeOrigin,
        })"""
    )

    assert 0 <= snapshot["now"] < 500
    assert snapshot["date"].startswith("1970-01-01T00:00:00.")
    assert_near(snapshot["perf"], snapshot["now"], 100)
    assert_near(snapshot["origin"], 0, 100)


@case
def clock_pause_at_before_install_uses_epoch_performance(page):
    page.set_content("<main>clock pause auto install</main>")

    page.clock.pause_at("2020-01-01T00:00:00Z")
    snapshot = page.evaluate(
        """() => ({
        now: Date.now(),
        date: new Date().toISOString(),
        perf: performance.now(),
        origin: performance.timeOrigin,
        sum: performance.timeOrigin + performance.now(),
        })"""
    )

    assert_near(snapshot["now"], 1577836800000)
    assert snapshot["date"] == "2020-01-01T00:00:00.000Z"
    assert_near(snapshot["perf"], 1577836800000)
    assert_near(snapshot["origin"], 0, 100)
    assert_near(snapshot["sum"], snapshot["now"], 100)


@case
def add_locator_handler_dismisses_overlay_before_action(page):
    page.set_content(
        """
        <style>
          #target { position: absolute; left: 80px; top: 80px; width: 120px; height: 40px; }
          #overlay { position: fixed; inset: 0; z-index: 20; background: rgba(0,0,0,.2); }
          #dismiss { position: absolute; left: 20px; top: 20px; }
        </style>
        <button id="target" onclick="document.body.dataset.clicked = 'yes'">Target</button>
        <div id="overlay"><button id="dismiss" onclick="this.parentElement.remove()">Dismiss</button></div>
        """
    )
    calls = []

    def dismiss_overlay(locator):
        calls.append(locator.get_attribute("id"))
        page.get_by_role("button", name="Dismiss").click()

    page.add_locator_handler(page.locator("#overlay"), dismiss_overlay)
    page.get_by_role("button", name="Target").click()

    assert calls == ["overlay"]
    assert page.evaluate("document.body.dataset.clicked") == "yes"
    assert page.locator("#overlay").count() == 0


@case
def add_locator_handler_option_validation_matches_playwright(page):
    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(
        lambda: page.add_locator_handler(page.locator("body"), lambda: None, no_wait_after="bad"),
        "Page.add_locator_handler: no_wait_after: expected boolean, got string",
    )
    expect_error(
        lambda: page.add_locator_handler(None, lambda: None),
        "'NoneType' object has no attribute '_impl_obj'",
    )
    expect_error(
        lambda: page.add_locator_handler(123, lambda: None),
        "'int' object has no attribute '_impl_obj'",
    )
    expect_error(
        lambda: page.remove_locator_handler(None),
        "'NoneType' object has no attribute '_impl_obj'",
    )
    expect_error(
        lambda: page.remove_locator_handler(123),
        "'int' object has no attribute '_impl_obj'",
    )

    owner_browser = page.context.browser
    assert owner_browser is not None
    other_page = owner_browser.new_page()
    try:
        expect_error(
            lambda: page.add_locator_handler(other_page.locator("body"), lambda: None),
            "Locator must belong to the main frame of this page",
        )
    finally:
        other_page.close()

    page.set_content(
        """
        <iframe srcdoc="<button id='inside'>Inside</button>"></iframe>
        <div id="banner">Notice</div>
        <button id="target" onclick="document.body.dataset.clicked = String((Number(document.body.dataset.clicked || 0) + 1))">
          Target
        </button>
        """
    )
    child_frame = next(frame for frame in page.frames if frame is not page.main_frame)
    expect_error(
        lambda: page.add_locator_handler(child_frame.locator("#inside"), lambda: None),
        "Locator must belong to the main frame of this page",
    )
    banner = page.locator("#banner")
    target = page.locator("#target")

    for value, expected_calls in [
        (0, 0),
        (False, 0),
        (True, 1),
        (1, 1),
        (2, 2),
        (-1, 4),
        (1.5, 4),
        (None, 4),
    ]:
        calls = []
        page.add_locator_handler(banner, lambda value=value: calls.append(value), no_wait_after=True, times=value)
        target.click(timeout=1_000)
        target.click(timeout=1_000)
        assert len(calls) == expected_calls
        page.remove_locator_handler(banner)

    page.add_locator_handler(banner, lambda: None, no_wait_after=True, times="bad")
    page.remove_locator_handler(banner)

    page.add_locator_handler(banner, None, no_wait_after=True)
    page.remove_locator_handler(banner)
    page.add_locator_handler(banner, 123, no_wait_after=True)
    page.remove_locator_handler(banner)


@case
def locator_click_force_skips_receives_events_check(page):
    page.set_content(
        """
        <style>
          #target { position: absolute; left: 20px; top: 20px; width: 120px; height: 40px; z-index: 1; }
          #overlay { position: absolute; left: 0; top: 0; width: 200px; height: 100px; z-index: 2; background: rgba(0,0,0,.1); }
        </style>
        <button id="target" onclick="document.body.dataset.clicked = 'target'">Target</button>
        <div id="overlay" onclick="document.body.dataset.overlay = 'yes'"></div>
        """
    )

    try:
        page.locator("#target").click(timeout=250)
    except Exception as error:
        assert "Timeout" in type(error).__name__ or "Timeout" in str(error)
    else:
        raise AssertionError("covered locator click unexpectedly succeeded")

    page.locator("#target").click(force=True, timeout=1_000)

    assert page.evaluate("document.body.dataset.clicked || null") is None
    assert page.evaluate("document.body.dataset.overlay") == "yes"


@case
def locator_check_force_skips_receives_events_then_verifies_state(page):
    page.set_content(
        """
        <style>
          #agree { position: absolute; left: 20px; top: 20px; width: 30px; height: 30px; z-index: 1; }
          #overlay { position: absolute; left: 0; top: 0; width: 100px; height: 80px; z-index: 2; background: rgba(0,0,0,.1); }
        </style>
        <input id="agree" type="checkbox">
        <div id="overlay" onclick="document.body.dataset.overlay = 'yes'"></div>
        """
    )

    try:
        page.locator("#agree").check(timeout=250)
    except Exception as error:
        assert "Timeout" in type(error).__name__ or "Timeout" in str(error)
    else:
        raise AssertionError("covered locator check unexpectedly succeeded")

    try:
        page.locator("#agree").check(force=True, timeout=1_000)
    except Exception as error:
        assert "did not change its state" in str(error)
    else:
        raise AssertionError("covered force check unexpectedly changed state")

    assert page.locator("#agree").is_checked() is False
    assert page.evaluate("document.body.dataset.overlay") == "yes"


@case
def locator_hover_force_skips_receives_events_check(page):
    page.set_content(
        """
        <style>
          #target { position: absolute; left: 20px; top: 20px; width: 120px; height: 40px; z-index: 1; }
          #overlay { position: absolute; left: 0; top: 0; width: 200px; height: 100px; z-index: 2; background: rgba(0,0,0,.1); }
        </style>
        <button id="target" onmouseover="document.body.dataset.hover = 'target'">Target</button>
        <div id="overlay" onmouseover="document.body.dataset.overlay = 'hover'"></div>
        """
    )

    try:
        page.locator("#target").hover(timeout=250)
    except Exception as error:
        assert "Timeout" in type(error).__name__ or "Timeout" in str(error)
    else:
        raise AssertionError("covered locator hover unexpectedly succeeded")

    page.locator("#target").hover(force=True, timeout=1_000)

    assert page.evaluate("document.body.dataset.hover || null") is None
    assert page.evaluate("document.body.dataset.overlay") == "hover"


def _set_native_hover_event_content(page):
    page.set_content(
        """
        <style>
        #target { position:absolute; left:40px; top:40px; width:120px; height:40px; }
        </style>
        <button id="target">Target</button>
        <script>
        window.events = [];
        const target = document.getElementById('target');
        for (const type of ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'pointermove', 'mousemove']) {
          target.addEventListener(type, event => window.events.push(`${type}:${Math.round(event.clientX)}:${Math.round(event.clientY)}:${event.buttons}`));
        }
        </script>
        """
    )
    page.mouse.move(1, 1)
    page.evaluate("window.events = []")


def _native_hover_dispatch(page, owner):
    if owner == "page":
        page.hover("#target", timeout=1_000)
        return
    if owner == "frame":
        page.main_frame.hover("#target", timeout=1_000)
        return
    if owner == "locator":
        page.locator("#target").hover(timeout=1_000)
        return
    if owner == "element":
        handle = page.query_selector("#target")
        assert handle is not None
        handle.hover(timeout=1_000)
        return
    raise AssertionError(f"unknown hover owner {owner!r}")


@case
def hover_dispatches_native_pointer_mouse_events_like_playwright(page):
    expected = [
        "pointerover:100:60:0",
        "pointerenter:100:60:0",
        "mouseover:100:60:0",
        "mouseenter:100:60:0",
        "pointermove:100:60:0",
        "mousemove:100:60:0",
    ]
    browser = page.context.browser
    assert browser is not None
    for owner in ["page", "frame", "locator", "element"]:
        hover_page = browser.new_page()
        try:
            _set_native_hover_event_content(hover_page)
            _native_hover_dispatch(hover_page, owner)
            assert hover_page.evaluate("window.events") == expected
        finally:
            hover_page.close()


@case
def mouse_wheel_dispatches_single_trusted_event_like_playwright(page):
    page.set_content(
        """
        <style>
        body { margin: 0; height: 2000px; }
        #area { position:absolute; left:20px; top:20px; width:200px; height:120px; background:#eee; }
        </style>
        <div id="area">area</div>
        <script>
        window.events = [];
        for (const listenerTarget of [document, window]) {
          listenerTarget.addEventListener('wheel', event => {
            window.events.push({
              listener: listenerTarget === document ? 'document' : 'window',
              target: event.target.id || event.target.nodeName,
              x: Math.round(event.clientX),
              y: Math.round(event.clientY),
              deltaX: event.deltaX,
              deltaY: event.deltaY,
              buttons: event.buttons,
              trusted: event.isTrusted,
            });
          });
        }
        </script>
        """
    )

    page.mouse.move(50, 50)
    page.mouse.wheel(0, 40)
    page.wait_for_timeout(100)

    assert page.evaluate("window.events") == [
        {
            "listener": "document",
            "target": "area",
            "x": 50,
            "y": 50,
            "deltaX": 0,
            "deltaY": 40,
            "buttons": 0,
            "trusted": True,
        },
        {
            "listener": "window",
            "target": "area",
            "x": 50,
            "y": 50,
            "deltaX": 0,
            "deltaY": 40,
            "buttons": 0,
            "trusted": True,
        },
    ]
    assert page.evaluate("window.scrollY") == 40


@case
def mouse_wheel_fractional_delta_nudges_like_playwright(page):
    page.set_content(
        """
        <style>
        body { margin: 0; height: 400px; }
        #area { position:absolute; left:20px; top:20px; width:200px; height:120px; background:#eee; }
        </style>
        <div id="area">area</div>
        <script>
        window.events = [];
        area.addEventListener('wheel', event => {
          window.events.push({
            target: event.target.id || event.target.nodeName,
            x: Math.round(event.clientX),
            y: Math.round(event.clientY),
            deltaX: event.deltaX,
            deltaY: event.deltaY,
            deltaMode: event.deltaMode,
            buttons: event.buttons,
            trusted: event.isTrusted,
          });
        });
        </script>
        """
    )

    page.mouse.move(50, 50)
    page.mouse.wheel(0, -1e-5)
    page.mouse.wheel(0, 1e-5)
    page.wait_for_timeout(100)

    events = page.evaluate("window.events")
    assert len(events) == 2
    for event in events:
        assert event["target"] == "area"
        assert event["x"] == 50
        assert event["y"] == 50
        assert event["deltaX"] == 0
        assert event["deltaMode"] == 0
        assert event["buttons"] == 0
        assert event["trusted"] is True
        assert 0 < abs(event["deltaY"]) < 0.001
    assert events[0]["deltaY"] < 0
    assert events[1]["deltaY"] > 0


@case
def mouse_click_count_dispatch_sequence_matches_playwright(page):
    page.set_content(
        """
        <button id="target" style="position:absolute;left:20px;top:20px;width:90px;height:45px">Target</button>
        <script>
        window.events = [];
        for (const type of ['mousedown', 'mouseup', 'click', 'dblclick']) {
          target.addEventListener(type, event => {
            window.events.push(`${type}:${event.detail}:${event.button}:${event.buttons}:${event.isTrusted}`);
          });
        }
        </script>
        """
    )

    page.mouse.click(65, 42, click_count=0)
    page.mouse.click(65, 42, click_count=-1)
    assert page.evaluate("window.events") == []

    page.mouse.click(65, 42, click_count=3)
    assert page.evaluate("window.events") == [
        "mousedown:1:0:1:true",
        "mouseup:1:0:0:true",
        "click:1:0:0:true",
        "mousedown:2:0:1:true",
        "mouseup:2:0:0:true",
        "click:2:0:0:true",
        "dblclick:2:0:0:true",
        "mousedown:3:0:1:true",
        "mouseup:3:0:0:true",
        "click:3:0:0:true",
    ]


@case
def mouse_dblclick_delay_reuses_click_count_sequence_like_playwright(page):
    page.set_content(
        """
        <button id="target" style="position:absolute;left:20px;top:20px;width:90px;height:45px">Target</button>
        <script>
        window.events = [];
        const start = performance.now();
        for (const type of ['mousedown', 'mouseup', 'click', 'dblclick']) {
          target.addEventListener(type, event => {
            window.events.push({ type, detail: event.detail, t: Math.round(performance.now() - start), trusted: event.isTrusted });
          });
        }
        </script>
        """
    )

    page.mouse.dblclick(65, 42, delay=50)
    events = page.evaluate("window.events")
    assert [{key: event[key] for key in ["type", "detail", "trusted"]} for event in events] == [
        {"type": "mousedown", "detail": 1, "trusted": True},
        {"type": "mouseup", "detail": 1, "trusted": True},
        {"type": "click", "detail": 1, "trusted": True},
        {"type": "mousedown", "detail": 2, "trusted": True},
        {"type": "mouseup", "detail": 2, "trusted": True},
        {"type": "click", "detail": 2, "trusted": True},
        {"type": "dblclick", "detail": 2, "trusted": True},
    ]
    assert events[1]["t"] - events[0]["t"] >= 30
    assert events[3]["t"] - events[2]["t"] >= 30
    assert events[4]["t"] - events[3]["t"] >= 30


@case
def locator_tap_force_skips_receives_events_check(page):
    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(has_touch=True, is_mobile=True)
    touch_page = context.new_page()
    try:
        touch_page.set_content(
            """
            <style>
              #target { position: absolute; left: 20px; top: 20px; width: 120px; height: 40px; z-index: 1; }
              #overlay { position: absolute; left: 0; top: 0; width: 200px; height: 100px; z-index: 2; background: rgba(0,0,0,.1); }
            </style>
            <button id="target" onclick="document.body.dataset.tap = 'target'">Target</button>
            <div id="overlay" onclick="document.body.dataset.overlay = 'tap'"></div>
            """
        )

        try:
            touch_page.locator("#target").tap(timeout=250)
        except Exception as error:
            assert "Timeout" in type(error).__name__ or "Timeout" in str(error)
        else:
            raise AssertionError("covered locator tap unexpectedly succeeded")

        touch_page.locator("#target").tap(force=True, timeout=1_000)

        assert touch_page.evaluate("document.body.dataset.tap || null") is None
        assert touch_page.evaluate("document.body.dataset.overlay") == "tap"
    finally:
        context.close()


@case
def forced_pointer_actions_hidden_visibility_errors_match_playwright(page):
    page.set_content("<button id='hidden' style='display:none'>Hidden</button>")
    handle = page.query_selector("#hidden")
    assert handle is not None

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(
        lambda: page.locator("#hidden").click(force=True, timeout=1_000),
        "Locator.click: Element is not visible",
    )
    expect_error(
        lambda: page.click("#hidden", force=True, timeout=1_000),
        "Page.click: Element is not visible",
    )
    expect_error(
        lambda: page.main_frame.click("#hidden", force=True, timeout=1_000),
        "Frame.click: Element is not visible",
    )
    expect_error(
        lambda: handle.click(force=True, timeout=1_000),
        "ElementHandle.click: Element is not visible",
    )
    expect_error(
        lambda: page.locator("#hidden").dblclick(force=True, timeout=1_000),
        "Locator.dblclick: Element is not visible",
    )
    expect_error(
        lambda: page.locator("#hidden").hover(force=True, timeout=1_000),
        "Locator.hover: Element is not visible",
    )

    page.set_content(
        """
        <button id="delayed" style="display:none" onclick="document.body.dataset.delayed='clicked'">Delayed</button>
        <script>
        setTimeout(() => { document.querySelector('#delayed').style.display = 'block'; }, 200);
        </script>
        """
    )
    expect_error(
        lambda: page.locator("#delayed").click(force=True, timeout=1_000),
        "Locator.click: Element is not visible",
    )
    assert page.evaluate("document.body.dataset.delayed || null") is None

    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(has_touch=True, is_mobile=True)
    touch_page = context.new_page()
    try:
        touch_page.set_content("<button id='hidden' style='display:none'>Hidden</button>")
        expect_error(
            lambda: touch_page.locator("#hidden").tap(force=True, timeout=1_000),
            "Locator.tap: Element is not visible",
        )
    finally:
        context.close()


@case
def forced_pointer_trial_actions_dispatch_like_playwright(page):
    def set_mouse_content():
        page.set_content(
            """
            <style>
            #target { position:absolute; left:20px; top:20px; width:120px; height:40px; z-index:1; }
            #overlay { position:absolute; left:0; top:0; width:200px; height:100px; z-index:2; }
            </style>
            <button id="target">Target</button>
            <div id="overlay"></div>
            <script>
            window.events = [];
            for (const id of ['target', 'overlay']) {
              const node = document.getElementById(id);
              for (const type of ['mouseover', 'mouseenter', 'mousemove', 'mousedown', 'mouseup', 'click', 'dblclick']) {
                node.addEventListener(type, event => window.events.push(`${id}:${type}:${event.detail}`));
              }
            }
            </script>
            """
        )

    set_mouse_content()
    page.locator("#target").click(force=True, trial=True, timeout=1_000)
    assert page.evaluate("window.events") == [
        "overlay:mouseover:0",
        "overlay:mouseenter:0",
        "overlay:mousemove:0",
        "overlay:mousedown:1",
        "overlay:mouseup:1",
        "overlay:click:1",
    ]

    set_mouse_content()
    page.locator("#target").dblclick(force=True, trial=True, timeout=1_000)
    assert page.evaluate("window.events") == [
        "overlay:mouseover:0",
        "overlay:mouseenter:0",
        "overlay:mousemove:0",
        "overlay:mousedown:1",
        "overlay:mouseup:1",
        "overlay:click:1",
        "overlay:mousedown:2",
        "overlay:mouseup:2",
        "overlay:click:2",
        "overlay:dblclick:2",
    ]

    set_mouse_content()
    page.locator("#target").hover(force=True, trial=True, timeout=1_000)
    assert page.evaluate("window.events") == [
        "overlay:mouseover:0",
        "overlay:mouseenter:0",
        "overlay:mousemove:0",
    ]

    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(has_touch=True, is_mobile=True)
    touch_page = context.new_page()
    try:
        touch_page.set_content(
            """
            <style>
            #target { position:absolute; left:20px; top:20px; width:120px; height:40px; z-index:1; }
            #overlay { position:absolute; left:0; top:0; width:200px; height:100px; z-index:2; }
            </style>
            <button id="target">Target</button>
            <div id="overlay"></div>
            <script>
            window.events = [];
            for (const id of ['target', 'overlay']) {
              const node = document.getElementById(id);
              for (const type of ['touchstart', 'touchend', 'pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click']) {
                node.addEventListener(type, event => window.events.push(`${id}:${type}`));
              }
            }
            </script>
            """
        )
        touch_page.locator("#target").tap(force=True, trial=True, timeout=1_000)
        assert touch_page.evaluate("window.events") == [
            "overlay:pointerdown",
            "overlay:touchstart",
            "overlay:pointerup",
            "overlay:touchend",
            "overlay:mousedown",
            "overlay:mouseup",
            "overlay:click",
        ]
    finally:
        context.close()


@case
def forced_drag_hidden_visibility_errors_match_playwright(page):
    page.set_content(
        """
        <style>
        #source, #hidden-source { width:80px; height:30px; background:#ddd; margin:8px; }
        #target, #hidden-target { width:100px; height:50px; background:#cfc; margin:8px; }
        #hidden-source, #hidden-target { display:none; }
        </style>
        <div id="source" draggable="true">Source</div>
        <div id="hidden-source" draggable="true">Hidden Source</div>
        <div id="target">Target</div>
        <div id="hidden-target">Hidden Target</div>
        <script>
        for (const id of ['target', 'hidden-target']) {
          const node = document.getElementById(id);
          node.addEventListener('dragover', event => event.preventDefault());
          node.addEventListener('drop', event => {
            event.preventDefault();
            document.body.dataset.drop = id;
          });
        }
        </script>
        """
    )

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(
        lambda: page.drag_and_drop("#hidden-source", "#target", force=True, timeout=1_000),
        "Page.drag_and_drop: Element is not visible",
    )
    expect_error(
        lambda: page.drag_and_drop("#source", "#hidden-target", force=True, timeout=1_000),
        "Page.drag_and_drop: Element is not visible",
    )
    expect_error(
        lambda: page.main_frame.drag_and_drop("#hidden-source", "#target", force=True, timeout=1_000),
        "Frame.drag_and_drop: Element is not visible",
    )
    expect_error(
        lambda: page.locator("#hidden-source").drag_to(page.locator("#target"), force=True, timeout=1_000),
        "Locator.drag_to: Element is not visible",
    )
    expect_error(
        lambda: page.locator("#source").drag_to(page.locator("#hidden-target"), force=True, timeout=1_000),
        "Locator.drag_to: Element is not visible",
    )
    assert page.evaluate("document.body.dataset.drop || null") is None

    page.set_content(
        """
        <style>
        #source, #target { width:80px; height:30px; margin:8px; background:#ddd; }
        </style>
        <div id="target">Target</div>
        <script>
        const target = document.getElementById('target');
        target.addEventListener('dragover', event => event.preventDefault());
        target.addEventListener('drop', event => {
          event.preventDefault();
          document.body.dataset.drop = 'delayed';
        });
        setTimeout(() => {
          const source = document.createElement('div');
          source.id = 'source';
          source.draggable = true;
          source.textContent = 'Source';
          document.body.insertBefore(source, target);
        }, 200);
        </script>
        """
    )
    page.drag_and_drop("#source", "#target", force=True, timeout=1_000)
    assert page.evaluate("document.body.dataset.drop") == "delayed"


@case
def default_viewport_size(page):
    assert page.viewport_size == {"width": 1280, "height": 720}
    assert page.evaluate("({ width: innerWidth, height: innerHeight })") == {"width": 1280, "height": 720}


@case
def check_and_uncheck(page):
    page.set_content(
        """
        <label><input id='agree' type='checkbox'> Agree</label>
        <input id='checked-radio' type='radio' checked>
        <input id='hidden-unchecked' type='checkbox' style='display:none'>
        <input id='hidden-checked' type='checkbox' style='display:none' checked>
        <input id='disabled-check' type='checkbox' disabled>
        <div id='plain-check'>Plain</div>
        <div id='role-button-check' role='button' aria-checked='false'>Button Role</div>
        <div id='role-checkbox' role='checkbox' aria-checked='false' onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked') === 'true' ? 'false' : 'true')">Role Checkbox</div>
        <div id='role-switch' role='switch' aria-checked='false' onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked') === 'true' ? 'false' : 'true')">Role Switch</div>
        <div id='role-option' role='option' aria-checked='true' onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked') === 'true' ? 'false' : 'true')">Role Option</div>
        <div id='role-treeitem' role='treeitem' aria-checked='false' onclick="this.setAttribute('aria-checked', this.getAttribute('aria-checked') === 'true' ? 'false' : 'true')">Role Treeitem</div>
        """
    )
    def expect_error(callback, *substrings):
        try:
            callback()
        except Exception as exc:
            text = str(exc)
            for substring in substrings:
                assert substring in text
        else:
            raise AssertionError(f"expected error containing {substrings!r}")

    page.locator("#agree").check(trial=True)
    page.check("#agree", trial=True)
    assert not page.is_checked("#agree")
    page.check("#agree")
    assert page.is_checked("#agree")
    page.locator("#agree").uncheck(trial=True)
    page.uncheck("#agree", trial=True)
    page.locator("#agree").set_checked(False, trial=True)
    assert page.is_checked("#agree")
    page.uncheck("#agree")
    assert not page.is_checked("#agree")
    expect_error(lambda: page.check("#plain-check", timeout=300), "Not a checkbox or radio button")
    expect_error(lambda: page.check("#role-button-check", timeout=300), "Not a checkbox or radio button")
    page.check("#role-checkbox")
    assert page.is_checked("#role-checkbox")
    page.uncheck("#role-checkbox")
    assert not page.is_checked("#role-checkbox")
    page.check("#role-switch")
    assert page.is_checked("#role-switch")
    page.uncheck("#role-switch")
    assert not page.is_checked("#role-switch")
    page.uncheck("#role-option")
    assert not page.is_checked("#role-option")
    page.check("#role-treeitem")
    assert page.is_checked("#role-treeitem")
    expect_error(lambda: page.uncheck("#checked-radio", timeout=300), "Cannot uncheck radio button")
    try:
        page.locator("#hidden-unchecked").check(force=True, timeout=300)
    except Exception as error:
        assert "Element is not visible" in str(error)
    else:
        raise AssertionError("hidden check(force=True) unexpectedly succeeded")
    page.locator("#hidden-checked").check(force=True)
    assert page.is_checked("#hidden-checked")
    try:
        page.locator("#hidden-checked").uncheck(force=True, timeout=300)
    except Exception as error:
        assert "Element is not visible" in str(error)
    else:
        raise AssertionError("hidden uncheck(force=True) unexpectedly succeeded")
    page.locator("#hidden-unchecked").uncheck(force=True)
    assert not page.is_checked("#hidden-unchecked")
    try:
        page.locator("#disabled-check").check(force=True, timeout=300)
    except Exception as error:
        assert "did not change its state" in str(error)
    else:
        raise AssertionError("disabled check(force=True) unexpectedly succeeded")


def _set_native_check_event_content(page):
    page.set_content(
        """
        <label><input id="agree" type="checkbox"> Agree</label>
        <input id="checked" type="checkbox" checked>
        <input id="block-check" type="checkbox">
        <input id="block-uncheck" type="checkbox" checked>
        <script>
        window.events = [];
        for (const id of ['agree', 'checked', 'block-check', 'block-uncheck']) {
          const el = document.getElementById(id);
          for (const type of ['mouseover', 'mouseenter', 'mousemove', 'mousedown', 'mouseup', 'click', 'input', 'change']) {
            el.addEventListener(type, event => window.events.push(`${id}:${type}:${event.detail || 0}:${el.checked}`));
          }
        }
        document.getElementById('block-check').addEventListener('click', event => event.preventDefault());
        document.getElementById('block-uncheck').addEventListener('click', event => event.preventDefault());
        </script>
        """
    )


def _native_check_dispatch(page, owner, selector, action, checked=None):
    if owner == "page":
        if action == "set_checked":
            page.set_checked(selector, checked, timeout=1_000)
        else:
            getattr(page, action)(selector, timeout=1_000)
        return
    if owner == "frame":
        if action == "set_checked":
            page.main_frame.set_checked(selector, checked, timeout=1_000)
        else:
            getattr(page.main_frame, action)(selector, timeout=1_000)
        return
    if owner == "locator":
        if action == "set_checked":
            page.locator(selector).set_checked(checked, timeout=1_000)
        else:
            getattr(page.locator(selector), action)(timeout=1_000)
        return
    if owner == "element":
        handle = page.query_selector(selector)
        assert handle is not None
        if action == "set_checked":
            handle.set_checked(checked, timeout=1_000)
        else:
            getattr(handle, action)(timeout=1_000)
        return
    if owner == "label":
        page.get_by_label("Agree").check(timeout=1_000)
        return
    raise AssertionError(f"unknown check owner {owner!r}")


@case
def native_check_uncheck_mouse_events_and_prevent_default_match_playwright(page):
    checked_events = [
        "agree:mouseover:0:false",
        "agree:mouseenter:0:false",
        "agree:mousemove:0:false",
        "agree:mousedown:1:false",
        "agree:mouseup:1:false",
        "agree:click:1:true",
        "agree:input:0:true",
        "agree:change:0:true",
    ]
    unchecked_events = [
        "checked:mouseover:0:true",
        "checked:mouseenter:0:true",
        "checked:mousemove:0:true",
        "checked:mousedown:1:true",
        "checked:mouseup:1:true",
        "checked:click:1:false",
        "checked:input:0:false",
        "checked:change:0:false",
    ]

    for owner in ["page", "frame", "locator", "element", "label"]:
        _set_native_check_event_content(page)
        _native_check_dispatch(page, owner, "#agree", "check")
        assert page.is_checked("#agree")
        assert page.evaluate("window.events.filter(event => event.startsWith('agree:'))") == checked_events

    for owner in ["page", "frame", "locator", "element"]:
        _set_native_check_event_content(page)
        _native_check_dispatch(page, owner, "#checked", "uncheck")
        assert not page.is_checked("#checked")
        assert page.evaluate("window.events.filter(event => event.startsWith('checked:'))") == unchecked_events

    def expect_error(operation, expected_message, selector):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            assert not page.is_checked(selector) if selector == "#block-check" else page.is_checked(selector)
            return
        raise AssertionError(f"expected {expected_message!r}")

    for owner, action, selector, checked, message in [
        ("page", "check", "#block-check", None, "Page.check: Clicking the checkbox did not change its state"),
        ("frame", "uncheck", "#block-uncheck", None, "Frame.uncheck: Clicking the checkbox did not change its state"),
        (
            "locator",
            "set_checked",
            "#block-check",
            True,
            "Locator.set_checked: Clicking the checkbox did not change its state",
        ),
        (
            "element",
            "set_checked",
            "#block-uncheck",
            False,
            "ElementHandle.set_checked: Clicking the checkbox did not change its state",
        ),
    ]:
        _set_native_check_event_content(page)
        expect_error(lambda: _native_check_dispatch(page, owner, selector, action, checked), message, selector)


@case
def select_option(page):
    page.set_content(
        """
        <select id='plan'>
          <option value='free'>Free</option>
          <option value='pro'>Pro</option>
          <option value='enterprise'>Enterprise</option>
        </select>
        <select id='multi-plan' multiple>
          <option value='free'>Free</option>
          <option value='pro'>Pro</option>
          <option value='enterprise'>Enterprise</option>
        </select>
        <select id='labeled-plan'>
          <option value='labeled' label='Labeled Plan'>Raw Text</option>
        </select>
        """
    )
    assert page.select_option("#plan", "pro") == ["pro"]
    assert page.evaluate("document.querySelector('#plan').value") == "pro"
    assert page.select_option("#plan", value="free") == ["free"]
    assert page.locator("#plan").select_option(label="Enterprise") == ["enterprise"]
    assert page.locator("#plan").select_option(index=1) == ["pro"]
    assert page.select_option("#plan", value=["pro", "free"]) == ["free"]
    assert page.locator("#plan").select_option(label=["Enterprise", "Free"]) == ["free"]
    assert page.locator("#plan").select_option(index=[2, 1]) == ["pro"]
    assert page.select_option("#multi-plan", value=["pro", "free"]) == ["free", "pro"]
    assert page.locator("#multi-plan").select_option(label=["Enterprise", "Free"]) == ["free", "enterprise"]
    assert page.locator("#multi-plan").select_option(index=[2, 1]) == ["pro", "enterprise"]
    assert page.select_option("#labeled-plan", value="Labeled Plan") == ["labeled"]
    try:
        page.select_option("#multi-plan", ["free", "missing"], timeout=300)
    except Exception:
        pass
    else:
        raise AssertionError("multi select_option unexpectedly succeeded with a missing option")
    option = page.query_selector("option[value='free']")
    assert option is not None
    try:
        assert page.select_option("#plan", element=option) == ["free"]
    finally:
        option.dispose()

    page.set_content(
        """
        <select id='hidden-plan' style='display:none'>
          <option value='free'>Free</option>
          <option value='pro'>Pro</option>
        </select>
        """
    )
    try:
        page.select_option("#hidden-plan", "pro", timeout=300)
    except Exception:
        pass
    else:
        raise AssertionError("hidden select_option unexpectedly succeeded without force")
    assert page.select_option("#hidden-plan", "pro", force=True) == ["pro"]
    assert page.evaluate("document.querySelector('#hidden-plan').value") == "pro"


@case
def select_option_timeout_and_target_errors_match_playwright(page):
    page.set_content(
        """
        <select id='plan'>
          <option value='free'>Free</option>
          <option value='pro'>Pro</option>
        </select>
        <select id='multi-plan' multiple>
          <option value='free'>Free</option>
          <option value='pro'>Pro</option>
        </select>
        <select id='disabled-plan' disabled>
          <option value='free'>Free</option>
        </select>
        <div id='plain'>Plain</div>
        """
    )

    def expect_first_line(operation, expected_name, expected_message):
        try:
            operation()
        except Exception as exc:
            assert type(exc).__name__ == expected_name
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_name}: {expected_message}")

    for owner, selector in [
        (page, "#plan"),
        (page.main_frame, "#plan"),
        (page.locator("#plan"), None),
        (page.query_selector("#plan"), None),
    ]:
        assert owner is not None
        prefix = owner.__class__.__name__
        if prefix == "Frame":
            prefix = "Frame"
        expected = f"{prefix}.select_option: Timeout 100ms exceeded."
        if selector is None:
            expect_first_line(lambda owner=owner: owner.select_option("missing", timeout=100), "TimeoutError", expected)
        else:
            expect_first_line(lambda owner=owner, selector=selector: owner.select_option(selector, "missing", timeout=100), "TimeoutError", expected)

    expect_first_line(
        lambda: page.select_option("#multi-plan", ["free", "missing"], timeout=100),
        "TimeoutError",
        "Page.select_option: Timeout 100ms exceeded.",
    )
    expect_first_line(
        lambda: page.select_option("#plan", index=-1, timeout=100),
        "TimeoutError",
        "Page.select_option: Timeout 100ms exceeded.",
    )
    expect_first_line(
        lambda: page.select_option("#disabled-plan", "free", timeout=100),
        "TimeoutError",
        "Page.select_option: Timeout 100ms exceeded.",
    )
    expect_first_line(
        lambda: page.select_option("#plain", "free", timeout=100),
        "Error",
        "Page.select_option: Error: Element is not a <select> element",
    )


@case
def locator_filter_has_text(page):
    page.set_content("<ul><li>Alpha</li><li>Beta target</li><li>Gamma</li></ul>")
    assert page.locator("li").filter(has_text="target").inner_text() == "Beta target"


@case
def locator_constructor_filter_options(page):
    page.set_content(
        """
        <section id="root">
          <article id="alpha"><h2>Revenue</h2><button>Open</button></article>
          <article id="beta"><h2>Cost</h2></article>
          <article id="gamma"><h2>Forecast</h2><button>Review</button></article>
          <iframe name="child" srcdoc="<article id='frame-alpha'><button>Frame Open</button></article><article id='frame-beta'>Frame Beta</article>"></iframe>
        </section>
        """
    )

    assert page.locator("article", has_text="Revenue").get_attribute("id") == "alpha"
    assert page.locator("article", has_not_text=re.compile("cost", re.I)).evaluate_all("(els) => els.map(el => el.id)") == [
        "alpha",
        "gamma",
    ]
    assert page.locator("article", has=page.locator("button", has_text="Open")).get_attribute("id") == "alpha"
    assert page.locator("article", has_not=page.locator("button")).get_attribute("id") == "beta"
    assert page.locator("#root").locator("article", has_text="Cost").get_attribute("id") == "beta"
    assert page.locator("#root").locator(page.locator("article", has_text="Revenue")).get_attribute("id") == "alpha"
    for operation in (
        lambda: page.locator("article").filter(has="button").count(),
        lambda: page.locator("article").filter(has_not="button").count(),
        lambda: page.locator("article", has="button").count(),
        lambda: page.locator("article", has_not="button").count(),
        lambda: page.locator("article").and_("button").count(),
        lambda: page.locator("article").or_("button").count(),
    ):
        try:
            operation()
        except AttributeError as exc:
            assert str(exc).splitlines()[0] == "'str' object has no attribute '_impl_obj'"
        else:
            raise AssertionError("non-locator locator value unexpectedly succeeded")
    for operation, expected in (
        (lambda: page.locator("article").locator(None).count(), "'NoneType' object has no attribute '_frame'"),
        (lambda: page.locator("article").locator(123).count(), "'int' object has no attribute '_frame'"),
    ):
        try:
            operation()
        except AttributeError as exc:
            assert str(exc).splitlines()[0] == expected
        else:
            raise AssertionError("non-string nested locator selector unexpectedly succeeded")

    frame = page.frame(name="child")
    assert frame is not None
    assert frame.locator("article", has=frame.locator("button", has_text="Frame Open")).get_attribute("id") == "frame-alpha"
    assert page.frame_locator("iframe").locator("article", has_text="Frame Open").get_attribute("id") == "frame-alpha"


@case
def label_and_placeholder_locators(page):
    page.set_content("<label>Email <input></label><input placeholder='Search'>")
    page.get_by_label("Email").fill("user@example.com")
    page.get_by_placeholder("Search").fill("invoice")
    assert page.evaluate("document.querySelector('label input').value") == "user@example.com"
    assert page.evaluate("document.querySelector('[placeholder=Search]').value") == "invoice"


@case
def label_locator_aria_sources_match_playwright(page):
    page.set_content(
        """
        <section id="scope">
          <label for="for-input">For Label</label><input id="for-input">
          <label>Wrapped Label <textarea id="wrapped"></textarea></label>
          <label>Button Wrap <button id="wrapped-button">Button</button></label>
          <input id="aria-label-input" aria-label="Aria Label">
          <input id="aria-labelledby-input" aria-labelledby="label-one label-two">
          <span id="label-one">Composite</span><span id="label-two">Name</span>
          <input id="aria-labelledby-single" aria-labelledby="single-label">
          <span id="single-label">Single Label</span>
          <label>Hidden Native <span style="display:none">Suffix</span><input id="hidden-native-label"></label>
          <input id="hidden-reference-label" aria-labelledby="hidden-reference-text">
          <span id="hidden-reference-text">Hidden Reference <span hidden>Suffix</span></span>
          <button id="aria-button" aria-label="Button Label">ignored text</button>
          <div id="editable" contenteditable aria-label="Editable Label"></div>
          <div id="plain" aria-label="Plain Label"></div>
        </section>
        """
    )

    assert page.get_by_label("For Label").evaluate_all("(els) => els.map(el => el.id)") == ["for-input"]
    assert page.get_by_label("Wrapped Label").evaluate_all("(els) => els.map(el => el.id)") == ["wrapped"]
    assert page.get_by_label("Button Wrap").evaluate_all("(els) => els.map(el => el.id)") == ["wrapped-button"]
    assert page.get_by_label("Aria Label").evaluate_all("(els) => els.map(el => el.id)") == ["aria-label-input"]
    assert page.get_by_label("Composite").evaluate_all("(els) => els.map(el => el.id)") == ["aria-labelledby-input"]
    assert page.get_by_label("Name", exact=True).evaluate_all("(els) => els.map(el => el.id)") == ["aria-labelledby-input"]
    assert page.get_by_label("Composite Name").count() == 0
    assert page.get_by_label("Single Label").evaluate_all("(els) => els.map(el => el.id)") == ["aria-labelledby-single"]
    assert page.get_by_label("Hidden Native", exact=True).count() == 0
    assert page.get_by_label("Hidden Native Suffix", exact=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "hidden-native-label"
    ]
    assert page.get_by_label("Hidden Reference", exact=True).count() == 0
    assert page.get_by_label("Hidden Reference Suffix", exact=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "hidden-reference-label"
    ]
    assert page.get_by_label("Button Label").evaluate_all("(els) => els.map(el => el.id)") == ["aria-button"]
    assert page.get_by_label("Editable Label").evaluate_all("(els) => els.map(el => el.id)") == ["editable"]
    assert page.get_by_label("Plain Label").evaluate_all("(els) => els.map(el => el.id)") == ["plain"]
    assert page.locator("#scope").get_by_label("Aria Label").get_attribute("id") == "aria-label-input"
    assert page.get_by_label(re.compile(r"single\s+label", re.I)).get_attribute("id") == "aria-labelledby-single"


@case
def label_locator_form_control_reference_sources_match_playwright(page):
    page.set_content(
        """
        <section id="scope">
          <input id="text-ref" value="Typed value" aria-label="Input label">
          <input id="empty-text-ref" value="" aria-label="Fallback label">
          <input id="button-ref" type="button" value="Button value">
          <input id="submit-ref" type="submit" value="Submit value">
          <input id="reset-ref" type="reset">
          <input id="image-ref" type="image" alt="Image alt">
          <textarea id="textarea-ref">Textarea value</textarea>
          <select id="select-ref"><option selected>Selected option</option></select>
          <input id="by-text-input" aria-labelledby="text-ref">
          <input id="by-empty-text-input" aria-labelledby="empty-text-ref">
          <input id="by-button-input" aria-labelledby="button-ref">
          <input id="by-submit-input" aria-labelledby="submit-ref">
          <input id="by-reset-input" aria-labelledby="reset-ref">
          <input id="by-image-input" aria-labelledby="image-ref">
          <input id="by-textarea" aria-labelledby="textarea-ref">
          <input id="by-select" aria-labelledby="select-ref">
        </section>
        """
    )

    assert page.get_by_label("Input label", exact=True).evaluate_all("(els) => els.map(el => el.id)") == ["text-ref"]
    assert page.get_by_label("Typed value", exact=True).count() == 0
    assert page.get_by_label("Fallback label", exact=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "empty-text-ref"
    ]
    assert page.get_by_label("Button value", exact=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "by-button-input"
    ]
    assert page.get_by_label("Submit value", exact=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "by-submit-input"
    ]
    assert page.get_by_label("Reset", exact=True).count() == 0
    assert page.get_by_label("Image alt", exact=True).count() == 0
    assert page.get_by_label("Textarea value", exact=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "by-textarea"
    ]
    assert page.get_by_label("Selected option", exact=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "by-select"
    ]
    assert page.locator("#scope").get_by_label("Button value", exact=True).get_attribute("id") == "by-button-input"


@case
def test_id_alt_and_title_locators(page):
    page.set_content(
        """
        <button data-testid='save-button'>Save</button>
        <img alt='Company logo'>
        <span title='Status badge'>Ready</span>
        """
    )
    assert page.get_by_test_id("save-button").inner_text() == "Save"
    assert page.get_by_alt_text("Company logo").is_visible()
    assert page.get_by_title("Status badge").inner_text() == "Ready"


@case
def test_id_regex_locators_match_playwright(page, playwright):
    page.set_content(
        """
        <section id="scope">
          <div id="save" data-testid="save">Save</div>
          <div id="save-extra" data-testid="save-extra">Save extra</div>
          <div id="discard" data-testid="discard">Discard</div>
          <div id="custom" data-qa="custom-save">Custom</div>
        </section>
        """
    )

    ids = "(els) => els.map(el => el.id)"
    assert page.get_by_test_id(re.compile("save")).evaluate_all(ids) == ["save", "save-extra"]
    assert page.get_by_test_id(re.compile("^save$")).evaluate_all(ids) == ["save"]
    assert page.get_by_test_id(re.compile("SAVE", re.I)).evaluate_all(ids) == ["save", "save-extra"]
    assert page.locator("#scope").get_by_test_id(re.compile("extra$")).evaluate_all(ids) == ["save-extra"]
    assert page.main_frame.get_by_test_id(re.compile("discard")).evaluate_all(ids) == ["discard"]
    assert page.get_by_test_id("save").evaluate_all(ids) == ["save"]

    playwright.selectors.set_test_id_attribute("data-qa")
    try:
        assert page.get_by_test_id(re.compile("save$")).evaluate_all(ids) == ["custom"]
    finally:
        playwright.selectors.set_test_id_attribute("data-testid")


@case
def internal_text_and_testid_selector_engines_match_playwright(page):
    page.set_content(
        """
        <section>
          <button id="save" data-testid="save" data-qa="custom-save">Save button</button>
          <button id="lower" data-testid="lower-save">save</button>
          <button id="other">Savory</button>
          <button id="extra" data-testid="save extra">Extra</button>
        </section>
        """
    )

    ids = "(els) => els.map(el => el.id)"
    assert page.locator('internal:text="av"i').evaluate_all(ids) == ["save", "lower", "other"]
    assert page.locator('internal:text="av"s').evaluate_all(ids) == []
    assert page.locator('internal:text="save"s').evaluate_all(ids) == ["lower"]
    assert page.locator('internal:text=/save/i').evaluate_all(ids) == ["save", "lower"]
    assert page.locator('button >> internal:text="av"i').evaluate_all(ids) == ["save", "lower", "other"]
    assert page.locator('button >> internal:text="av"s').evaluate_all(ids) == []
    assert page.locator('internal:testid=[data-testid="save"]').evaluate_all(ids) == ["save"]
    assert page.locator("internal:testid=[data-testid='save']").evaluate_all(ids) == ["save"]
    assert page.locator("internal:testid=[data-testid=save]").evaluate_all(ids) == ["save"]
    assert page.locator("internal:testid=[data-testid=/save/]").evaluate_all(ids) == ["save", "lower", "extra"]
    assert page.locator('internal:testid=[data-qa="custom-save"]').evaluate_all(ids) == ["save"]


@case
def internal_role_selector_engine_matches_playwright(page):
    page.set_content(
        """
        <section>
          <button id="save">Save</button>
          <button id="lower">save</button>
          <button id="disabled" disabled>Disabled</button>
          <button id="hidden" hidden>Hidden</button>
          <input id="checked" type="checkbox" checked>
          <input id="unchecked" type="checkbox">
        </section>
        """
    )

    ids = "(els) => els.map(el => el.id)"
    assert page.locator("internal:role=button").evaluate_all(ids) == ["save", "lower", "disabled"]
    assert page.locator('internal:role=button[name="Save"i]').evaluate_all(ids) == ["save", "lower"]
    assert page.locator('internal:role=button[name="Save"s]').evaluate_all(ids) == ["save"]
    assert page.locator('internal:role=button[name="save"s]').evaluate_all(ids) == ["lower"]
    assert page.locator("internal:role=button[name=/save/i]").evaluate_all(ids) == ["save", "lower"]
    assert page.locator("internal:role=checkbox[checked=true]").evaluate_all(ids) == ["checked"]
    assert page.locator("internal:role=checkbox[checked=false]").evaluate_all(ids) == ["unchecked"]
    assert page.locator("internal:role=button[disabled=true]").evaluate_all(ids) == ["disabled"]
    assert page.locator("internal:role=button[include-hidden=true]").evaluate_all(ids) == [
        "save",
        "lower",
        "disabled",
        "hidden",
    ]
    assert page.locator('button >> internal:role=button[name="Save"i]').evaluate_all(ids) == []
    assert page.locator('role=button[name="Save"i]').evaluate_all(ids) == ["save", "lower"]
    assert page.locator('role=button[name="Save"s]').evaluate_all(ids) == ["save"]


@case
def frame_locator_reads_srcdoc(page):
    page.set_content("<iframe srcdoc='<main><button>Frame Save</button></main>'></iframe>")
    assert page.frame_locator("iframe").get_by_role("button", name="Frame Save").inner_text() == "Frame Save"


@case
def wait_for_function(page):
    page.set_content("<main></main>")
    page.evaluate("() => setTimeout(() => document.body.dataset.ready = 'yes', 20)")
    handle = page.wait_for_function("() => document.body.dataset.ready === 'yes'", timeout=2_000)
    try:
        assert handle.json_value() is True
    finally:
        handle.dispose()
    raf_handle = page.wait_for_function("() => true", polling="raf", timeout=500)
    raf_handle.dispose()
    interval_handle = page.wait_for_function("() => true", polling=1.5, timeout=500)
    interval_handle.dispose()

    def expect_error(callback, message):
        try:
            callback()
        except Exception as exc:
            assert str(exc).splitlines()[0] == message
        else:
            raise AssertionError(f"expected error {message!r}")

    expect_error(lambda: page.wait_for_function("() => true", polling="mutation", timeout=100), "Unknown polling option: mutation")
    expect_error(
        lambda: page.wait_for_function("() => true", polling=0, timeout=100),
        "Page.wait_for_function: Cannot poll with non-positive interval: 0",
    )
    expect_error(
        lambda: page.wait_for_function("() => true", polling=True, timeout=100),
        "Page.wait_for_function: polling_interval: expected float, got boolean",
    )
    expect_error(
        lambda: page.wait_for_function("() => true", timeout="100"),
        "Page.wait_for_function: timeout: expected float, got string",
    )
    expect_error(
        lambda: page.wait_for_function("() => true", timeout=True),
        "Page.wait_for_function: timeout: expected float, got boolean",
    )
    expect_error(
        lambda: page.wait_for_function("() => false", timeout=-1, polling=1),
        "Page.wait_for_function: Timeout -1ms exceeded.",
    )
    page.set_default_timeout(1)
    try:
        page.evaluate("() => { window.__zeroTimeoutReady = false; setTimeout(() => window.__zeroTimeoutReady = true, 20); }")
        no_timeout_handle = page.wait_for_function("() => window.__zeroTimeoutReady", timeout=0, polling=1)
        no_timeout_handle.dispose()
    finally:
        page.set_default_timeout(30_000)


@case
def frame_wait_for_function_returns_js_handle(page):
    child = "<main>Frame</main><script>setTimeout(() => { window.readyValue = { answer: 42 }; }, 20)</script>"
    page.set_content(f'<iframe name="child" srcdoc="{escape(child, quote=True)}"></iframe>')
    frame = page.frame(name="child")
    assert frame is not None

    handle = frame.wait_for_function("() => window.readyValue", timeout=2_000)
    try:
        assert handle.json_value() == {"answer": 42}
    finally:
        handle.dispose()
    try:
        frame.wait_for_function("() => true", polling="mutation", timeout=100)
    except Exception as exc:
        assert str(exc).splitlines()[0] == "Unknown polling option: mutation"
    else:
        raise AssertionError("Frame.wait_for_function invalid polling unexpectedly succeeded")
    try:
        frame.wait_for_function("() => true", polling=0, timeout=100)
    except Exception as exc:
        assert str(exc).splitlines()[0] == "Frame.wait_for_function: Cannot poll with non-positive interval: 0"
    else:
        raise AssertionError("Frame.wait_for_function non-positive polling unexpectedly succeeded")
    try:
        frame.wait_for_function("() => true", timeout="100")
    except Exception as exc:
        assert str(exc).splitlines()[0] == "Frame.wait_for_function: timeout: expected float, got string"
    else:
        raise AssertionError("Frame.wait_for_function invalid timeout unexpectedly succeeded")
    try:
        frame.wait_for_function("() => false", timeout=-1, polling=1)
    except Exception as exc:
        assert str(exc).splitlines()[0] == "Frame.wait_for_function: Timeout -1ms exceeded."
    else:
        raise AssertionError("Frame.wait_for_function negative timeout unexpectedly succeeded")
    page.set_default_timeout(1)
    try:
        frame.evaluate("() => { window.__zeroTimeoutReady = false; setTimeout(() => window.__zeroTimeoutReady = true, 20); }")
        no_timeout_handle = frame.wait_for_function("() => window.__zeroTimeoutReady", timeout=0, polling=1)
        no_timeout_handle.dispose()
    finally:
        page.set_default_timeout(30_000)


@case
def dom_node_serialization_and_frame_handle(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    child = "<button id='go'>Frame Button</button>"
    page.set_content(
        f"<main><button id='outer'>Outer</button></main>"
        f'<iframe name="child" srcdoc="{escape(child, quote=True)}"></iframe>'
    )
    frame = page.frame(name="child")
    assert frame is not None

    assert page.evaluate("() => document.querySelector('#outer')") == "ref: <Node>"
    assert frame.evaluate("() => document.querySelector('#go')") == "ref: <Node>"

    handle = frame.evaluate_handle("() => document.querySelector('#go')")
    element = None
    try:
        assert handle.json_value() == "ref: <Node>"
        assert handle.evaluate("(element) => element.textContent") == "Frame Button"
        element = handle.as_element()
        assert element is not None
        assert element.inner_text() == "Frame Button"
    finally:
        handle.dispose()
    assert element is not None
    try:
        element.inner_text()
    except sync_api.Error as exc:
        assert str(exc).splitlines()[0] == "ElementHandle.inner_text: Target page, context or browser has been closed"
    else:
        raise AssertionError("element from disposed JSHandle should be unusable")


@case
def evaluate_handle_property(page):
    page.set_content("<main></main>")
    handle = page.evaluate_handle("() => ({ answer: 42 })")
    try:
        assert handle.get_property("answer").json_value() == 42
        assert handle.get_property(property_name="answer").json_value() == 42
    finally:
        handle.dispose()
    element = page.query_selector("main")
    assert element is not None
    tag_name = element.get_property(property_name="tagName")
    try:
        assert tag_name.json_value() == "MAIN"
    finally:
        tag_name.dispose()


@case
def expect_console_message(page):
    page.set_content("<main></main>")
    with page.expect_console_message(lambda message: message.text == "parity-log") as message_info:
        page.evaluate("() => console.log('parity-log')")
    assert message_info.value.text == "parity-log"


@case
def expect_object_set_options_default_timeout(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect

    assert isinstance(expect, sync_api.Expect)
    page.set_content(
        """
        <title>Loading</title>
        <main id="status">Loading</main>
        <script>
        setTimeout(() => {
          document.title = 'Object Expect';
          document.querySelector('#status').textContent = 'Ready';
        }, 50);
        </script>
        """
    )

    expect.set_options(timeout=3_000)
    try:
        expect(page).to_have_title("Object Expect")
        expect(page.locator("#status")).to_have_text("Ready")
    finally:
        expect.set_options()


@case
def expect_api_response_to_be_ok(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")

    with header_case_server() as base_url:
        sync_api.expect(page.context.request.get(f"{base_url}/headers")).to_be_ok()
        sync_api.expect(page.context.request.get(f"{base_url}/bad")).not_to_be_ok()
        try:
            sync_api.expect(page.context.request.get(f"{base_url}/bad")).to_be_ok()
        except AssertionError:
            pass
        else:
            raise AssertionError("expect(api_response).to_be_ok() should fail for a 500 response")


@case
def expect_to_have_count_timeout_message_matches_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")

    page.set_content("<button>One</button><button>Two</button>")

    for expected, expected_message in (
        (2.5, "Locator expected to have count '2.5'"),
        (None, "Locator expected to have count"),
    ):
        try:
            sync_api.expect(page.locator("button")).to_have_count(expected, timeout=100)
        except AssertionError as exc:
            first_line = str(exc).splitlines()[0]
        else:
            raise AssertionError(f"to_have_count({expected!r}) unexpectedly succeeded")

        assert first_line == expected_message


@case
def expect_locator_assertion_strict_value_parity(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect

    page.set_content(
        """
        <button id="button" tabindex="1">Button</button>
        <input id="checked" type="checkbox" checked value="abc">
        <input id="unchecked" type="checkbox">
        """
    )

    expect(page.locator("#button")).to_have_js_property("tabIndex", 1)
    expect(page.locator("#button")).to_have_js_property("tabIndex", 1.0)
    expect(page.locator("#checked")).to_have_js_property("checked", True)
    expect(page.locator("#checked")).to_be_checked(checked=0)
    expect(page.locator("#checked")).to_be_checked(checked="")
    expect(page.locator("#unchecked")).to_be_checked(checked=False)

    for label, assertion in (
        (
            "numeric JS property should not match a string expectation",
            lambda: expect(page.locator("#button")).to_have_js_property("tabIndex", "1", timeout=50),
        ),
        (
            "boolean JS property should not match a numeric expectation",
            lambda: expect(page.locator("#checked")).to_have_js_property("checked", 1, timeout=50),
        ),
        (
            "string JS property should not match a regex expectation",
            lambda: expect(page.locator("#checked")).to_have_js_property("value", re.compile("abc"), timeout=50),
        ),
        (
            "checked=0 should still expect checked",
            lambda: expect(page.locator("#unchecked")).to_be_checked(checked=0, timeout=50),
        ),
    ):
        try:
            assertion()
        except AssertionError:
            pass
        else:
            raise AssertionError(label)


@case
def expect_locator_assertion_boolean_option_truthiness(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect

    page.set_content(
        """
        <button id="visible">Visible</button>
        <button id="hidden" style="display:none">Hidden</button>
        <input id="enabled">
        <input id="disabled" disabled>
        <input id="editable">
        <input id="readonly" readonly>
        """
    )

    expect(page.locator("#visible")).to_be_visible(visible=1)
    expect(page.locator("#hidden")).to_be_visible(visible=0)
    expect(page.locator("#hidden")).to_be_visible(visible="")
    expect(page.locator("#visible")).to_be_visible(visible="false")
    expect(page.locator("#disabled")).to_be_enabled(enabled=0)
    expect(page.locator("#enabled")).to_be_enabled(enabled="false")
    expect(page.locator("#readonly")).to_be_editable(editable=0)
    expect(page.locator("#editable")).to_be_editable(editable="false")

    for label, assertion in (
        (
            "visible=0 should expect hidden",
            lambda: expect(page.locator("#visible")).to_be_visible(visible=0, timeout=50),
        ),
        (
            "enabled='' should expect disabled",
            lambda: expect(page.locator("#enabled")).to_be_enabled(enabled="", timeout=50),
        ),
        (
            "editable=False should expect readonly",
            lambda: expect(page.locator("#editable")).to_be_editable(editable=False, timeout=50),
        ),
    ):
        try:
            assertion()
        except AssertionError:
            pass
        else:
            raise AssertionError(label)


@case
def api_response_dispose_blocks_body_reads(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")

    with header_case_server() as base_url:
        response = page.context.request.get(f"{base_url}/headers")
        assert response.body().startswith(b"{")
        assert response.text().startswith("{")
        assert response.json() == {"ok": True}
        response.dispose()
        for reader in (response.body, response.text, response.json):
            try:
                reader()
            except sync_api.Error as exc:
                if "Response has been disposed" not in str(exc):
                    raise
            else:
                raise AssertionError("disposed APIResponse body reader should fail")
        assert response.headers["content-type"] == "application/json"


@case
def expect_locator_extra_assertions(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.goto(
        data_url(
            """
            <title>Assertions</title>
            <button id="go" class="primary large">Go</button>
            <svg id="svg-class" class="foo bar"></svg>
            <input id="mixed" type="checkbox">
            <input id="readonly" value="Read only" readonly>
            <button id="disabled" disabled>Disabled</button>
            <fieldset disabled>
              <button id="fieldset-disabled">Fieldset Disabled</button>
              <input id="fieldset-editable">
            </fieldset>
            <div aria-disabled="true">
              <button id="aria-disabled-child">Aria Disabled Child</button>
              <span id="aria-disabled-plain">Aria Disabled Plain</span>
            </div>
            <header id="role-banner">Header</header>
            <footer id="role-contentinfo">Footer</footer>
            <aside id="role-complementary">Aside</aside>
            <article id="role-article">Article<header id="role-article-header">Article Header</header><footer id="role-article-footer">Article Footer</footer></article>
            <form id="role-form" aria-label="Signup"><input></form>
            <form id="role-unnamed-form"><input></form>
            <section id="role-region" aria-label="Featured">Featured<header id="role-section-header">Section Header</header><footer id="role-section-footer">Section Footer</footer></section>
            <section id="role-unnamed-section">Unnamed</section>
            <hr id="role-separator">
            <details id="role-details"><summary>More</summary><p>Body</p></details>
            <fieldset id="role-group"><legend>Choice Group</legend><input></fieldset>
            <dialog id="role-dialog" open>Dialog</dialog>
            <blockquote id="role-blockquote">Quote body</blockquote>
            <code id="role-code">print()</code>
            <del id="role-deletion">old</del>
            <em id="role-emphasis">important</em>
            <ins id="role-insertion">new</ins>
            <p id="role-paragraph">Paragraph role</p>
            <search id="role-search-landmark">Search landmark</search>
            <strong id="role-strong">Strong text</strong>
            <sub id="role-subscript">2</sub>
            <sup id="role-superscript">3</sup>
            <time id="role-time">10:00</time>
            <div id="role-multi" role="button link">Multi Role</div>
            <div id="role-fallback" role="unknown button">Fallback Role</div>
            <div id="role-none-button" role="none button">None Button</div>
            <div id="role-presentation-button" role="presentation button">Presentation Button</div>
            <div id="role-uppercase" role="BUTTON">Uppercase Role</div>
            <img id="logo" alt="Logo">
            <ul id="role-list"><li id="role-item">One</li></ul>
            <table id="role-table"><caption id="role-caption">Grid Caption</caption><tr id="role-row"><th id="role-columnheader" scope="col">Col</th><th id="role-rowheader" scope="row">Row</th><td id="role-cell">Cell</td></tr></table>
            <input id="role-search" type="search">
            <input id="role-number" type="number">
            <progress id="role-progress" value="1" max="2"></progress>
            <meter id="role-meter" value="0.5">half</meter>
            <output id="role-status">7</output>
            <nav id="role-nav" aria-label="Primary"></nav>
            <main id="role-main"></main>
            <div role="main" id="role-main-scope"><header id="role-main-scope-header">Role Main Header</header><footer id="role-main-scope-footer">Role Main Footer</footer></div>
            <div role="article" id="role-article-scope"><header id="role-article-scope-header">Role Article Header</header><footer id="role-article-scope-footer">Role Article Footer</footer></div>
            <div role="region" aria-label="Scoped Region" id="role-region-scope"><header id="role-region-scope-header">Role Region Header</header><footer id="role-region-scope-footer">Role Region Footer</footer></div>
            <div id="aria-true" role="checkbox" aria-checked="true"></div>
            <div id="aria-mixed" role="checkbox" aria-checked="mixed"></div>
            <div id="copy">Visible<span style="display:none"> Hidden</span></div>
            <div id="spaced">  Hello
              world <span> now</span></div>
            <div id="hidden" hidden>Hidden</div>
            <div id="space">   </div>
            <div id="empty-child"><span></span></div>
            <input id="empty-input" value="">
            <input id="space-input" value="   ">
            <textarea id="space-textarea">   </textarea>
            <div id="half" style="position: fixed; left: 0; top: 50vh; width: 100px; height: 100vh"></div>
            <div id="offscreen" style="position: fixed; left: -1000px; top: -1000px; width: 20px; height: 20px"></div>
            <div id="empty"></div>
            <select id="choices" multiple>
              <option value="a" selected>A</option>
              <option value="c" selected>C</option>
            </select>
            <select id="single-choice"><option id="role-option">One</option></select>
            <select id="single-choice-value"><option value="a" selected>A</option></select>
            <ul id="text-list">
              <li>One<span style="display:none"> Hidden</span></li>
              <li>Two</li>
              <li>Three</li>
              <li>Four</li>
            </ul>
            <script>document.getElementById("mixed").indeterminate = true;</script>
            """
        )
    )
    page.locator("#go").focus()

    expect(page).to_have_url(re.compile("TEXT/HTML"), ignore_case=True)
    expect(page).to_have_url(url_or_reg_exp=re.compile("TEXT/HTML"), ignore_case=True)
    expect(page).not_to_have_url(re.compile("TEXT/HTML", re.I), ignore_case=False)
    expect(page).not_to_have_url(url_or_reg_exp=re.compile("TEXT/HTML", re.I), ignore_case=False)
    expect(page).to_have_title(title_or_reg_exp="Assertions")
    expect(page.locator("#go")).to_be_attached()
    expect(page.locator("#go")).to_be_attached(attached=None)
    expect(page.locator("#missing")).to_be_attached(attached=False)
    expect(page.locator("#go")).to_be_focused()
    expect(page.locator("#go")).to_be_visible(visible=True)
    expect(page.locator("#hidden")).to_be_visible(visible=False)
    expect(page.locator("#disabled")).to_be_enabled(enabled=False)
    expect(page.locator("#fieldset-disabled")).to_be_disabled()
    expect(page.locator("#fieldset-editable")).to_be_editable(editable=False)
    expect(page.locator("#aria-disabled-child")).to_be_disabled()
    expect(page.locator("#aria-disabled-plain")).to_be_enabled()
    expect(page.locator("#readonly")).to_be_editable(editable=False)
    expect(page.locator("#go")).to_have_id("go")
    expect(page.locator("#go")).to_have_id(id="go")
    expect(page.locator("#go")).to_have_class("primary large")
    expect(page.locator("#go")).to_contain_class("primary")
    expect(page.locator("#svg-class")).to_have_class("foo bar")
    expect(page.locator("#svg-class")).to_contain_class("foo")
    expect(page.locator("#go")).to_have_text("go", ignore_case=True)
    expect(page.locator("#go")).to_contain_text("O", ignore_case=True)
    expect(page.locator("#go")).to_have_attribute("class", "PRIMARY LARGE", ignore_case=True)
    for label, assertion in (
        (
            "missing attribute should not match an empty string",
            lambda: expect(page.locator("#go")).to_have_attribute("data-missing", "", timeout=50),
        ),
        (
            "single select should not satisfy to_have_values",
            lambda: expect(page.locator("#single-choice-value")).to_have_values(["a"], timeout=50),
        ),
    ):
        try:
            assertion()
        except AssertionError:
            pass
        else:
            raise AssertionError(label)
    expect(page.locator("#mixed")).to_be_checked(indeterminate=True)
    expect(page.locator("#mixed")).to_be_checked(checked=False, indeterminate=False)
    expect(page.locator("#aria-true")).to_be_checked()
    expect(page.locator("#aria-mixed")).to_be_checked(indeterminate=True)
    expect(page.locator("#aria-mixed")).to_be_checked(checked=False, indeterminate=False)
    expect(page.locator("#copy")).to_have_text("Visible Hidden")
    expect(page.locator("#copy")).to_have_text("Visible", use_inner_text=True)
    expect(page.locator("#copy")).not_to_contain_text("Hidden", use_inner_text=True)
    expect(page.locator("#spaced")).to_have_text("Hello world now")
    expect(page.locator("#spaced")).to_contain_text("world now")
    expect(page.locator("#spaced")).to_have_text(re.compile(r"^  Hello\s+world  now$"))
    expect(page.locator("#text-list li")).to_have_text(["One Hidden", "Two", "Three", "Four"])
    expect(page.locator("#text-list li")).to_have_text(["One", "Two", "Three", "Four"], use_inner_text=True)
    expect(page.locator("#text-list li")).to_have_count(count=4)
    expect(page.locator("#text-list li")).to_contain_text(["Two", "Four"])
    expect(page.locator("#text-list li")).not_to_contain_text(["Four", "Two"])
    expect(page.locator("#go")).to_have_role("button")
    expect(page.locator("#role-nav")).to_have_role(role="navigation")
    expect(page.locator("#role-banner")).to_have_role("banner")
    expect(page.locator("#role-contentinfo")).to_have_role("contentinfo")
    expect(page.locator("#role-article-header")).not_to_have_role("banner")
    expect(page.locator("#role-article-footer")).not_to_have_role("contentinfo")
    expect(page.locator("#role-section-header")).not_to_have_role("banner")
    expect(page.locator("#role-section-footer")).not_to_have_role("contentinfo")
    expect(page.locator("#role-main-scope-header")).not_to_have_role("banner")
    expect(page.locator("#role-main-scope-footer")).not_to_have_role("contentinfo")
    expect(page.locator("#role-article-scope-header")).not_to_have_role("banner")
    expect(page.locator("#role-article-scope-footer")).not_to_have_role("contentinfo")
    expect(page.locator("#role-region-scope-header")).not_to_have_role("banner")
    expect(page.locator("#role-region-scope-footer")).not_to_have_role("contentinfo")
    expect(page.locator("#role-complementary")).to_have_role("complementary")
    expect(page.locator("#role-article")).to_have_role("article")
    expect(page.locator("#role-form")).to_have_role("form")
    expect(page.locator("#role-unnamed-form")).not_to_have_role("form")
    expect(page.locator("#role-region")).to_have_role("region")
    expect(page.locator("#role-unnamed-section")).not_to_have_role("region")
    expect(page.locator("#role-separator")).to_have_role("separator")
    expect(page.locator("#role-details")).to_have_role("group")
    expect(page.locator("#role-group")).to_have_role("group")
    expect(page.locator("#role-dialog")).to_have_role("dialog")
    expect(page.locator("#role-blockquote")).to_have_role("blockquote")
    expect(page.locator("#role-caption")).to_have_role("caption")
    expect(page.locator("#role-code")).to_have_role("code")
    expect(page.locator("#role-deletion")).to_have_role("deletion")
    expect(page.locator("#role-emphasis")).to_have_role("emphasis")
    expect(page.locator("#role-insertion")).to_have_role("insertion")
    expect(page.locator("#role-paragraph")).to_have_role("paragraph")
    expect(page.locator("#role-search-landmark")).to_have_role("search")
    expect(page.locator("#role-strong")).to_have_role("strong")
    expect(page.locator("#role-subscript")).to_have_role("subscript")
    expect(page.locator("#role-superscript")).to_have_role("superscript")
    expect(page.locator("#role-time")).to_have_role("time")
    expect(page.locator("#role-multi")).to_have_role("button")
    expect(page.locator("#role-multi")).not_to_have_role("link")
    expect(page.locator("#role-fallback")).to_have_role("button")
    expect(page.locator("#role-none-button")).to_have_role("none")
    expect(page.locator("#role-none-button")).not_to_have_role("button")
    expect(page.locator("#role-presentation-button")).to_have_role("presentation")
    expect(page.locator("#role-presentation-button")).not_to_have_role("button")
    expect(page.locator("#role-uppercase")).not_to_have_role("button")
    expect(page.locator("#logo")).to_have_role("img")
    expect(page.locator("#role-list")).to_have_role("list")
    expect(page.locator("#role-item")).to_have_role("listitem")
    expect(page.locator("#role-table")).to_have_role("table")
    expect(page.locator("#role-row")).to_have_role("row")
    expect(page.locator("#role-columnheader")).to_have_role("columnheader")
    expect(page.locator("#role-rowheader")).to_have_role("rowheader")
    expect(page.locator("#role-cell")).to_have_role("cell")
    expect(page.locator("#role-search")).to_have_role("searchbox")
    expect(page.locator("#role-number")).to_have_role("spinbutton")
    expect(page.locator("#role-progress")).to_have_role("progressbar")
    expect(page.locator("#role-meter")).to_have_role("meter")
    expect(page.locator("#role-status")).to_have_role("status")
    expect(page.locator("#role-option")).to_have_role("option")
    expect(page.locator("#choices")).to_have_role("listbox")
    expect(page.locator("#single-choice")).to_have_role("combobox")
    expect(page.locator("#role-nav")).to_have_role("navigation")
    expect(page.locator("#role-main")).to_have_role("main")
    expect(page.locator("#half")).to_be_in_viewport(ratio=0.5)
    expect(page.locator("#half")).to_be_in_viewport(ratio=None)
    expect(page.locator("#offscreen")).not_to_be_in_viewport(ratio=0)
    expect(page.locator("#empty")).to_be_empty()
    expect(page.locator("#space")).to_be_empty()
    expect(page.locator("#empty-child")).to_be_empty()
    expect(page.locator("#empty-input")).to_be_empty()
    expect(page.locator("#space-input")).not_to_be_empty()
    expect(page.locator("#space-textarea")).not_to_be_empty()
    expect(page.locator("#readonly")).to_have_value(value="Read only")
    expect(page.locator("#choices")).to_have_values(["a", "c"])
    expect(page.locator("#choices")).to_have_values(values=["a", "c"])
    expect(page.locator("#go")).not_to_be_empty()
    expect(page.locator("#go")).not_to_have_id("bad")
    expect(page.locator("#go")).not_to_contain_class("missing")
    expect(page).not_to_have_title("Wrong")
    expect(page).not_to_have_title(title_or_reg_exp="Wrong")


@case
def expect_locator_accessible_assertions(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <button id="save" aria-label="Save order" aria-describedby="save-help">Save</button>
        <button id="image-button"><img alt="Search"></button>
        <a id="image-link" href="#"><img alt="Docs"></a>
        <button id="hidden-name"><span aria-hidden="true">Hidden</span>Visible</button>
        <div id="snapshot-multi" role="button link">Multi</div>
        <div id="snapshot-fallback" role="unknown button">Fallback</div>
        <div id="snapshot-none" role="none button">None Button</div>
        <div id="snapshot-presentation" role="presentation button">Presentation Button</div>
        <div id="snapshot-upper" role="BUTTON">Upper</div>
        <table id="snapshot-table"><tr><th scope="col">Col</th><th scope="row">Row</th><td>Cell</td></tr></table>
        <input id="snapshot-search" type="search" aria-label="Find">
        <input id="snapshot-number" type="number" aria-label="Qty" value="3">
        <input id="snapshot-range" type="range" aria-label="Volume" value="40">
        <input id="snapshot-image-alt" type="image" alt="Image Submit">
        <input id="snapshot-image-value" type="image" value="Image Value">
        <input id="snapshot-image-empty" type="image">
        <input id="snapshot-title-placeholder" title="Profile Name" placeholder="Nickname" value="Alice">
        <progress id="snapshot-progress" value="1" max="2" aria-label="Loading"></progress>
        <meter id="snapshot-meter" value="0.5" aria-label="Fuel">half</meter>
        <output id="snapshot-output">7</output>
        <blockquote id="snapshot-blockquote">Quote body</blockquote>
        <code id="snapshot-code">print()</code>
        <del id="snapshot-deletion">old</del>
        <em id="snapshot-emphasis">important</em>
        <ins id="snapshot-insertion">new</ins>
        <p id="snapshot-paragraph">Paragraph role</p>
        <search id="snapshot-search-role">Search landmark</search>
        <strong id="snapshot-strong">Strong text</strong>
        <sub id="snapshot-subscript">2</sub>
        <sup id="snapshot-superscript">3</sup>
        <time id="snapshot-time">10:00</time>
        <select id="snapshot-listbox" multiple aria-label="Many"><option selected>One</option><option>Two</option></select>
        <p id="save-help">Primary checkout action</p>
        <div id="email-error">Required field</div>
        <input id="email" aria-label="Email" aria-invalid="true" aria-errormessage="email-error">
        """
    )

    expect(page.locator("#save")).to_have_accessible_name("Save order")
    expect(page.locator("#save")).to_have_accessible_description("Primary checkout action")
    expect(page.locator("#save")).not_to_have_accessible_description(name="Secondary action")
    expect(page.locator("#image-button")).to_have_accessible_name("Search")
    expect(page.locator("#image-link")).to_have_accessible_name("Docs")
    expect(page.locator("#snapshot-image-alt")).to_have_accessible_name("Image Submit")
    expect(page.locator("#snapshot-image-value")).to_have_accessible_name("Submit")
    expect(page.locator("#snapshot-image-empty")).to_have_accessible_name("Submit")
    expect(page.locator("#snapshot-title-placeholder")).to_have_accessible_name("Profile Name")
    expect(page.locator("#hidden-name")).to_have_accessible_name("Visible")
    expect(page.locator("#email")).to_have_accessible_error_message("Required field")
    expect(page.locator("#save")).to_match_aria_snapshot('- button "Save order": Save')
    expect(page.locator("#snapshot-multi")).to_match_aria_snapshot('- button "Multi"')
    expect(page.locator("#snapshot-fallback")).to_match_aria_snapshot('- button "Fallback"')
    expect(page.locator("#snapshot-none")).to_match_aria_snapshot("- text: None Button")
    expect(page.locator("#snapshot-presentation")).to_match_aria_snapshot("- text: Presentation Button")
    expect(page.locator("#snapshot-upper")).to_match_aria_snapshot("- text: Upper")
    expect(page.locator("#snapshot-table")).to_match_aria_snapshot(
        '- table:\n'
        '  - rowgroup:\n'
        '    - row "Col Row Cell":\n'
        '      - columnheader "Col"\n'
        '      - rowheader "Row"\n'
        '      - cell "Cell"'
    )
    expect(page.locator("#snapshot-search")).to_match_aria_snapshot('- searchbox "Find"')
    expect(page.locator("#snapshot-number")).to_match_aria_snapshot('- spinbutton "Qty": "3"')
    expect(page.locator("#snapshot-range")).to_match_aria_snapshot('- slider "Volume": "40"')
    expect(page.locator("#snapshot-image-alt")).to_match_aria_snapshot('- button "Image Submit"')
    expect(page.locator("#snapshot-image-value")).to_match_aria_snapshot('- button "Submit": Image Value')
    expect(page.locator("#snapshot-image-empty")).to_match_aria_snapshot('- button "Submit"')
    expect(page.locator("#snapshot-title-placeholder")).to_match_aria_snapshot(
        '- textbox "Profile Name":\n'
        "  - /placeholder: Nickname\n"
        "  - text: Alice"
    )
    expect(page.locator("#snapshot-progress")).to_match_aria_snapshot('- progressbar "Loading"')
    expect(page.locator("#snapshot-meter")).to_match_aria_snapshot('- meter "Fuel": half')
    expect(page.locator("#snapshot-output")).to_match_aria_snapshot('- status: "7"')
    expect(page.locator("#snapshot-blockquote")).to_match_aria_snapshot("- blockquote: Quote body")
    expect(page.locator("#snapshot-code")).to_match_aria_snapshot("- code: print()")
    expect(page.locator("#snapshot-deletion")).to_match_aria_snapshot("- deletion: old")
    expect(page.locator("#snapshot-emphasis")).to_match_aria_snapshot("- emphasis: important")
    expect(page.locator("#snapshot-insertion")).to_match_aria_snapshot("- insertion: new")
    expect(page.locator("#snapshot-paragraph")).to_match_aria_snapshot("- paragraph: Paragraph role")
    expect(page.locator("#snapshot-search-role")).to_match_aria_snapshot("- search: Search landmark")
    expect(page.locator("#snapshot-strong")).to_match_aria_snapshot("- strong: Strong text")
    expect(page.locator("#snapshot-subscript")).to_match_aria_snapshot('- subscript: "2"')
    expect(page.locator("#snapshot-superscript")).to_match_aria_snapshot('- superscript: "3"')
    expect(page.locator("#snapshot-time")).to_match_aria_snapshot("- time: 10:00")
    expect(page.locator("#snapshot-listbox")).to_match_aria_snapshot(
        '- listbox "Many":\n'
        '  - option "One" [selected]\n'
        '  - option "Two"'
    )
    expect(page.locator("#save")).not_to_have_accessible_name("Cancel")
    expect(page.locator("#snapshot-title-placeholder")).not_to_have_accessible_name("Nickname")
    expect(page.locator("#email")).not_to_have_accessible_error_message("Different")
    expect(page.locator("#save")).not_to_match_aria_snapshot('- button "Cancel"')


@case
def aria_snapshot_stateful_controls_and_details_match_playwright(page):
    page.set_content(
        """
        <main aria-label="App">
          <button id="pressed" aria-pressed="true">Pressed</button>
          <button id="expanded" aria-expanded="true">Expanded</button>
          <button id="disabled" disabled>Disabled</button>
          <input id="check" type="checkbox" checked aria-label="Check">
          <input id="mixed" type="checkbox" aria-label="Mixed">
          <select id="select" aria-label="Choice"><option>One</option><option selected>Two</option></select>
          <select id="list" multiple aria-label="Many"><option selected>One</option><option>Two</option></select>
          <details id="details" open><summary>Summary</summary><p>Body</p></details>
          <script>document.querySelector("#mixed").indeterminate = true</script>
        </main>
        """
    )

    assert page.aria_snapshot() == (
        '- main "App":\n'
        '  - button "Pressed" [pressed]\n'
        '  - button "Expanded" [expanded]\n'
        '  - button "Disabled" [disabled]\n'
        '  - checkbox "Check" [checked]\n'
        '  - checkbox "Mixed" [checked=mixed]\n'
        '  - combobox "Choice":\n'
        '    - option "One"\n'
        '    - option "Two" [selected]\n'
        '  - listbox "Many":\n'
        '    - option "One" [selected]\n'
        '    - option "Two"\n'
        '  - group:\n'
        '    - text: Summary\n'
        '    - paragraph: Body'
    )


@case
def native_select_optgroup_snapshots_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Optgroup probe">
          <select id="single" aria-label="Single">
            <optgroup id="single-group" label="Group A">
              <option id="single-a">A</option>
              <option id="single-b" selected>B</option>
            </optgroup>
          </select>
          <select id="multi" aria-label="Multi" multiple>
            <optgroup id="multi-group" label="Group B">
              <option id="multi-a" selected>A</option>
              <option id="multi-b">B</option>
            </optgroup>
          </select>
          <select id="mixed" aria-label="Mixed">
            <option id="mixed-top">Top</option>
            <optgroup id="mixed-group" label="Group C">
              <option id="mixed-a" selected>A</option>
            </optgroup>
          </select>
        </main>
        """
    )

    assert page.aria_snapshot() == (
        '- main "Optgroup probe":\n'
        '  - combobox "Single"\n'
        '  - listbox "Multi":\n'
        "    - group:\n"
        '      - option "A" [selected]\n'
        '      - option "B"\n'
        '  - combobox "Mixed":\n'
        '    - option "Top"'
    )
    assert page.locator("#single").aria_snapshot() == '- combobox "Single"'
    assert page.locator("#single-group").aria_snapshot() == ""
    assert page.locator("#single-a").aria_snapshot() == '- option "A"'
    assert page.locator("#single-b").aria_snapshot() == '- option "B" [selected]'
    assert page.locator("#multi").aria_snapshot() == (
        '- listbox "Multi":\n'
        "  - group:\n"
        '    - option "A" [selected]\n'
        '    - option "B"'
    )
    assert page.locator("#multi-group").aria_snapshot() == (
        "- group:\n"
        '  - option "A" [selected]\n'
        '  - option "B"'
    )
    assert page.locator("#mixed").aria_snapshot() == '- combobox "Mixed":\n  - option "Top"'
    assert page.locator("#mixed-group").aria_snapshot() == ""
    assert page.locator("#mixed-a").aria_snapshot() == '- option "A" [selected]'


@case
def closed_details_hidden_content_snapshots_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Closed details probe">
          <details id="closed">
            <summary id="closed-summary"><span id="closed-summary-text">Closed Summary</span></summary>
            <p id="closed-body">Closed Body</p>
            <button id="closed-button">Hidden Action</button>
          </details>
          <details id="open" open>
            <summary id="open-summary">Open Summary</summary>
            <p id="open-body">Open Body</p>
            <button id="open-button">Open Action</button>
          </details>
        </main>
        """
    )

    assert page.aria_snapshot() == (
        '- main "Closed details probe":\n'
        "  - group: Closed Summary\n"
        "  - group:\n"
        "    - text: Open Summary\n"
        "    - paragraph: Open Body\n"
        '    - button "Open Action"'
    )
    assert page.locator("#closed").aria_snapshot() == "- group: Closed Summary"
    assert page.locator("#closed-summary").aria_snapshot() == "- text: Closed Summary"
    assert page.locator("#closed-summary-text").aria_snapshot() == "- text: Closed Summary"
    assert page.locator("#closed-body").aria_snapshot() == ""
    assert page.locator("#closed-button").aria_snapshot() == ""
    assert page.locator("#open").aria_snapshot() == (
        "- group:\n"
        "  - text: Open Summary\n"
        "  - paragraph: Open Body\n"
        '  - button "Open Action"'
    )
    assert page.locator("#open-summary").aria_snapshot() == "- text: Open Summary"
    assert page.locator("#open-body").aria_snapshot() == "- paragraph: Open Body"
    assert page.locator("#open-button").aria_snapshot() == '- button "Open Action"'
    assert page.get_by_role("group").evaluate_all("(els) => els.map(el => el.id)") == ["closed", "open"]
    assert page.get_by_role("paragraph").evaluate_all("(els) => els.map(el => el.id)") == ["open-body"]
    assert page.get_by_role("button").evaluate_all("(els) => els.map(el => el.id)") == ["open-button"]
    assert page.get_by_role("paragraph", include_hidden=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "closed-body",
        "open-body",
    ]
    assert page.get_by_role("button", include_hidden=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "closed-button",
        "open-button",
    ]


@case
def aria_snapshot_common_widget_roles_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Widgets">
          <div role="tablist" aria-label="Sections">
            <div id="tab" role="tab" aria-selected="true">Details</div>
            <div role="tab">History</div>
          </div>
          <div id="switch" role="switch" aria-checked="true">Power</div>
          <div id="option" role="option" aria-selected="true">Choice</div>
          <div id="menuitemcheckbox" role="menuitemcheckbox" aria-checked="mixed">Auto save</div>
          <div id="treeitem" role="treeitem" aria-expanded="true" aria-selected="true">Folder</div>
          <div id="current-link" role="link" aria-current="page">Current</div>
          <div id="busy-region" role="status" aria-busy="true">Loading</div>
        </main>
        """
    )

    assert page.aria_snapshot() == (
        '- main "Widgets":\n'
        '  - tablist "Sections":\n'
        '    - tab "Details" [selected]\n'
        '    - tab "History"\n'
        '  - switch "Power" [checked]\n'
        '  - option "Choice" [selected]\n'
        '  - menuitemcheckbox "Auto save" [checked=mixed]\n'
        '  - treeitem "Folder" [expanded] [selected]\n'
        '  - link "Current"\n'
        '  - status: Loading'
    )


@case
def semantic_container_child_snapshots_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Container value probe">
          <blockquote id="quote"><p id="quote-p">Nested paragraph</p></blockquote>
          <p id="mixed-p">Lead <strong id="strong">Strong</strong> tail <em id="em">Em</em></p>
          <strong id="mixed-strong">Strong lead <em id="inner-em">Inner em</em></strong>
          <search id="search"><label>Query <input id="input" value="abc"></label><button id="go">Go</button></search>
        </main>
        """
    )

    assert page.get_by_role("blockquote").evaluate_all("(els) => els.map(el => el.id)") == ["quote"]
    assert page.get_by_role("paragraph").evaluate_all("(els) => els.map(el => el.id)") == ["quote-p", "mixed-p"]
    assert page.get_by_role("strong").evaluate_all("(els) => els.map(el => el.id)") == ["strong", "mixed-strong"]
    assert page.get_by_role("emphasis").evaluate_all("(els) => els.map(el => el.id)") == ["em", "inner-em"]
    assert page.get_by_role("search").evaluate_all("(els) => els.map(el => el.id)") == ["search"]
    assert page.aria_snapshot() == (
        '- main "Container value probe":\n'
        "  - blockquote:\n"
        "    - paragraph: Nested paragraph\n"
        "  - paragraph:\n"
        "    - text: Lead\n"
        "    - strong: Strong\n"
        "    - text: tail\n"
        "    - emphasis: Em\n"
        "  - strong:\n"
        "    - text: Strong lead\n"
        "    - emphasis: Inner em\n"
        "  - search:\n"
        "    - text: Query\n"
        '    - textbox "Query": abc\n'
        '    - button "Go"'
    )
    assert page.locator("#quote").aria_snapshot() == "- blockquote:\n  - paragraph: Nested paragraph"
    assert page.locator("#mixed-p").aria_snapshot() == (
        "- paragraph:\n"
        "  - text: Lead\n"
        "  - strong: Strong\n"
        "  - text: tail\n"
        "  - emphasis: Em"
    )
    assert page.locator("#mixed-strong").aria_snapshot() == (
        "- strong:\n"
        "  - text: Strong lead\n"
        "  - emphasis: Inner em"
    )
    assert page.locator("#search").aria_snapshot() == (
        "- search:\n"
        "  - text: Query\n"
        '  - textbox "Query": abc\n'
        '  - button "Go"'
    )


@case
def author_named_control_value_snapshots_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Author named values">
          <button id="button-label" aria-label="Named">Visible text</button>
          <button id="button-same" aria-label="Same">Same</button>
          <span id="button-ref" hidden>Ref name</span>
          <button id="button-labelledby" aria-labelledby="button-ref">Button body</button>
          <a id="link-label" aria-label="Named link" href="/x">Link text</a>
          <a id="link-same" aria-label="Same" href="/same">Same</a>
          <h2 id="heading" aria-label="Named heading">Heading text</h2>
          <li id="listitem-same" aria-label="Same item">Same item</li>
          <li id="listitem-diff" aria-label="Named item">Item text</li>
          <div id="checkbox" role="checkbox" aria-checked="true" aria-label="Named checkbox">Check text</div>
          <div id="radio" role="radio" aria-checked="true" aria-label="Named radio">Radio text</div>
          <div id="option" role="option" aria-selected="true" aria-label="Named option">Option text</div>
          <div id="tab" role="tab" aria-label="Named tab">Tab text</div>
          <div id="switch" role="switch" aria-label="Named switch">Switch text</div>
          <div id="menuitem" role="menuitem" aria-label="Named item">Menu text</div>
          <div id="treeitem" role="treeitem" aria-label="Named tree">Tree text</div>
          <div id="tooltip" role="tooltip" aria-label="Named tip">Tip text</div>
          <div id="cell-same" role="cell" aria-label="Same cell">Same cell</div>
          <div id="cell-diff" role="cell" aria-label="Named cell">Cell text</div>
          <input id="submit" type="submit" aria-label="Named submit" value="Submit value">
          <input id="textbox-same" aria-label="Text same" value="Text same">
          <input id="range-same" type="range" aria-label="5" value="5">
        </main>
        """
    )

    assert page.get_by_role("button", name="Named", exact=True).evaluate("el => el.id") == "button-label"
    assert page.get_by_role("button", name="Ref name", exact=True).evaluate("el => el.id") == "button-labelledby"
    assert page.get_by_role("link", name="Named link", exact=True).evaluate("el => el.id") == "link-label"
    assert page.get_by_role("heading", name="Named heading", exact=True).evaluate("el => el.id") == "heading"
    assert page.get_by_role("checkbox", name="Named checkbox", exact=True).evaluate("el => el.id") == "checkbox"
    assert page.get_by_role("textbox", name="Text same", exact=True).evaluate("el => el.id") == "textbox-same"
    assert page.locator("#button-label").aria_snapshot() == '- button "Named": Visible text'
    assert page.locator("#button-same").aria_snapshot() == '- button "Same"'
    assert page.locator("#link-label").aria_snapshot() == '- link "Named link":\n  - /url: /x\n  - text: Link text'
    assert page.locator("#link-same").aria_snapshot() == '- link "Same":\n  - /url: /same'
    assert page.locator("#textbox-same").aria_snapshot() == '- textbox "Text same"'
    assert page.locator("#range-same").aria_snapshot() == '- slider "5"'
    assert page.aria_snapshot() == (
        '- main "Author named values":\n'
        '  - button "Named": Visible text\n'
        '  - button "Same"\n'
        '  - button "Ref name": Button body\n'
        '  - link "Named link":\n'
        '    - /url: /x\n'
        '    - text: Link text\n'
        '  - link "Same":\n'
        '    - /url: /same\n'
        '  - heading "Named heading" [level=2]: Heading text\n'
        '  - listitem "Same item"\n'
        '  - listitem "Named item": Item text\n'
        '  - checkbox "Named checkbox" [checked]: Check text\n'
        '  - radio "Named radio" [checked]: Radio text\n'
        '  - option "Named option" [selected]: Option text\n'
        '  - tab "Named tab": Tab text\n'
        '  - switch "Named switch": Switch text\n'
        '  - menuitem "Named item": Menu text\n'
        '  - treeitem "Named tree": Tree text\n'
        '  - tooltip "Named tip": Tip text\n'
        '  - cell "Same cell"\n'
        '  - cell "Named cell": Cell text\n'
        '  - button "Named submit": Submit value\n'
        '  - textbox "Text same"\n'
        '  - slider "5"'
    )


@case
def labelledby_form_control_sources_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Labelledby control probe">
          <input id="text-ref" value="Typed value" aria-label="Input label">
          <input id="button-ref" type="button" value="Button value">
          <input id="submit-ref" type="submit" value="Submit value">
          <input id="image-ref" type="image" alt="Image alt">
          <textarea id="textarea-ref">Textarea value</textarea>
          <select id="select-ref"><option selected>Selected option</option></select>
          <button id="by-text-input" aria-labelledby="text-ref">Body</button>
          <button id="by-button-input" aria-labelledby="button-ref">Body</button>
          <button id="by-submit-input" aria-labelledby="submit-ref">Body</button>
          <button id="by-image-input" aria-labelledby="image-ref">Body</button>
          <button id="by-textarea" aria-labelledby="textarea-ref">Body</button>
          <button id="by-select" aria-labelledby="select-ref">Body</button>
        </main>
        """
    )

    assert page.get_by_role("button", name="Typed value", exact=True).evaluate("el => el.id") == "by-text-input"
    assert page.get_by_role("button", name="Input label", exact=True).count() == 0
    assert page.get_by_role("button", name="Button value", exact=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "button-ref",
        "by-button-input",
    ]
    assert page.get_by_role("button", name="Submit value", exact=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "submit-ref",
        "by-submit-input",
    ]
    assert page.get_by_role("button", name="Image alt", exact=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "image-ref",
        "by-image-input",
    ]
    assert page.get_by_role("button", name="Textarea value", exact=True).evaluate("el => el.id") == "by-textarea"
    assert page.get_by_role("button", name="Selected option", exact=True).evaluate("el => el.id") == "by-select"
    expect(page.locator("#by-text-input")).to_have_accessible_name("Typed value")
    expect(page.locator("#by-select")).to_have_accessible_name("Selected option")
    assert page.locator("#by-text-input").aria_snapshot() == '- button "Typed value": Body'
    assert page.locator("#by-select").aria_snapshot() == '- button "Selected option": Body'
    assert page.aria_snapshot() == (
        '- main "Labelledby control probe":\n'
        '  - textbox "Input label": Typed value\n'
        '  - button "Button value"\n'
        '  - button "Submit value"\n'
        '  - button "Image alt"\n'
        "  - textbox: Textarea value\n"
        "  - combobox:\n"
        '    - option "Selected option" [selected]\n'
        '  - button "Typed value": Body\n'
        '  - button "Button value": Body\n'
        '  - button "Submit value": Body\n'
        '  - button "Image alt": Body\n'
        '  - button "Textarea value": Body\n'
        '  - button "Selected option": Body'
    )


@case
def svg_image_role_and_snapshot_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Graphics">
          <img id="img-alt" alt="Raster logo" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
          <svg id="svg-empty"></svg>
          <svg id="svg-title"><title>Vector logo</title><circle cx="5" cy="5" r="5"></circle></svg>
          <svg id="svg-label" aria-label="Label vector"></svg>
          <svg id="svg-role" role="img"><title>Role vector</title><circle cx="5" cy="5" r="5"></circle></svg>
          <canvas id="canvas" aria-label="Chart"></canvas>
          <div id="role-img" role="img" aria-label="Role image"></div>
        </main>
        """
    )

    assert page.get_by_role("img").evaluate_all("(els) => els.map(el => el.id)") == [
        "img-alt",
        "svg-empty",
        "svg-title",
        "svg-label",
        "svg-role",
        "role-img",
    ]
    assert page.get_by_role("img", name="Vector logo").get_attribute("id") == "svg-title"
    assert page.get_by_role("img", name="Label vector").get_attribute("id") == "svg-label"
    assert page.get_by_role("img", name="Role vector").get_attribute("id") == "svg-role"
    assert page.get_by_role("img", name="Chart").count() == 0
    expect(page.locator("#svg-title")).to_have_role("img")
    expect(page.locator("#svg-title")).to_have_accessible_name("Vector logo")
    expect(page.locator("#svg-role")).to_have_accessible_name("Role vector")
    assert page.aria_snapshot() == (
        '- main "Graphics":\n'
        '  - img "Raster logo"\n'
        "  - img\n"
        '  - img "Vector logo"\n'
        '  - img "Label vector"\n'
        '  - img "Role vector"\n'
        '  - img "Role image"'
    )


@case
def decorative_empty_alt_images_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    image_src = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    page.set_content(
        f"""
        <main aria-label="Empty alt probe">
          <img id="plain" alt="" src="{image_src}">
          <img id="missing" src="{image_src}">
          <img id="label" alt="" aria-label="Named empty" src="{image_src}">
          <img id="title" alt="" title="Titled empty" src="{image_src}">
          <img id="role-img" alt="" role="img" aria-label="Role named" src="{image_src}">
          <img id="tabindex" alt="" tabindex="0" src="{image_src}">
          <img id="role-none" alt="Photo" role="none" src="{image_src}">
        </main>
        """
    )

    assert page.get_by_role("img").evaluate_all("(els) => els.map(el => el.id)") == [
        "missing",
        "label",
        "title",
        "role-img",
        "tabindex",
    ]
    expect(page.locator("#plain")).not_to_have_role("img")
    expect(page.locator("#missing")).to_have_role("img")
    expect(page.locator("#label")).to_have_role("img")
    expect(page.locator("#title")).to_have_role("img")
    expect(page.locator("#role-img")).to_have_role("img")
    expect(page.locator("#tabindex")).to_have_role("img")
    expect(page.locator("#role-none")).not_to_have_role("img")
    assert page.aria_snapshot() == (
        '- main "Empty alt probe":\n'
        "  - img\n"
        '  - img "Named empty"\n'
        '  - img "Titled empty"\n'
        '  - img "Role named"\n'
        "  - img"
    )
    assert page.locator("#plain").aria_snapshot() == ""
    assert page.locator("#missing").aria_snapshot() == "- img"
    assert page.locator("#label").aria_snapshot() == '- img "Named empty"'
    assert page.locator("#title").aria_snapshot() == '- img "Titled empty"'
    assert page.locator("#role-img").aria_snapshot() == '- img "Role named"'
    assert page.locator("#tabindex").aria_snapshot() == "- img"
    assert page.locator("#role-none").aria_snapshot() == ""


@case
def presentational_image_conflicts_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    image_src = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    page.set_content(
        f"""
        <main aria-label="Presentational image probe">
          <img id="none-alt" role="none" alt="Named" src="{image_src}">
          <img id="none-tab" role="none" tabindex="0" alt="Named tab" src="{image_src}">
          <img id="presentation-tab" role="presentation" tabindex="0" alt="Named presentation" src="{image_src}">
          <img id="none-label" role="none" aria-label="Aria named" alt="" src="{image_src}">
          <svg id="svg-none-tab" role="none" tabindex="0"><title>SVG none</title></svg>
        </main>
        """
    )

    assert page.get_by_role("img").evaluate_all("(els) => els.map(el => el.id)") == [
        "none-tab",
        "presentation-tab",
        "none-label",
        "svg-none-tab",
    ]
    assert page.get_by_role("none").evaluate_all("(els) => els.map(el => el.id)") == ["none-alt"]
    assert page.get_by_role("presentation").evaluate_all("(els) => els.map(el => el.id)") == []
    expect(page.locator("#none-alt")).to_have_role("none")
    expect(page.locator("#none-alt")).not_to_have_role("img")
    expect(page.locator("#none-tab")).to_have_role("img")
    expect(page.locator("#presentation-tab")).to_have_role("img")
    expect(page.locator("#none-label")).to_have_role("img")
    expect(page.locator("#svg-none-tab")).to_have_role("img")
    assert page.aria_snapshot() == (
        '- main "Presentational image probe":\n'
        '  - img "Named tab"\n'
        '  - img "Named presentation"\n'
        '  - img "Aria named"\n'
        '  - img "SVG none"'
    )
    assert page.locator("#none-alt").aria_snapshot() == ""
    assert page.locator("#none-tab").aria_snapshot() == '- img "Named tab"'
    assert page.locator("#presentation-tab").aria_snapshot() == '- img "Named presentation"'
    assert page.locator("#none-label").aria_snapshot() == '- img "Aria named"'
    assert page.locator("#svg-none-tab").aria_snapshot() == '- img "SVG none"'


@case
def semantic_roles_figure_term_definition_math_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Semantics">
          <figure id="chart-figure">
            <img id="chart" alt="Quarterly chart" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
            <figcaption>Quarterly results</figcaption>
          </figure>
          <figure id="titled-figure" title="Standalone figure"><div>Figure body</div></figure>
          <dl><dt id="term">API</dt><dd id="definition">Application programming interface</dd></dl>
          <dfn id="dfn">Fetch</dfn>
          <math id="formula" aria-label="Formula"><mi>x</mi></math>
        </main>
        """
    )

    assert page.get_by_role("figure").evaluate_all("(els) => els.map(el => el.id)") == [
        "chart-figure",
        "titled-figure",
    ]
    assert page.get_by_role("term").evaluate_all("(els) => els.map(el => el.id)") == ["term", "dfn"]
    assert page.get_by_role("definition").evaluate_all("(els) => els.map(el => el.id)") == ["definition"]
    assert page.get_by_role("math").evaluate_all("(els) => els.map(el => el.id)") == ["formula"]
    assert page.get_by_role("figure", name="Quarterly results").get_attribute("id") == "chart-figure"
    assert page.get_by_role("figure", name="Standalone figure").get_attribute("id") == "titled-figure"
    assert page.get_by_role("math", name="Formula").get_attribute("id") == "formula"
    assert page.get_by_role("term", name="API").count() == 0
    assert page.get_by_role("definition", name="Application programming interface").count() == 0
    expect(page.locator("#chart-figure")).to_have_role("figure")
    expect(page.locator("#term")).to_have_role("term")
    expect(page.locator("#definition")).to_have_role("definition")
    expect(page.locator("#formula")).to_have_role("math")
    expect(page.locator("#chart-figure")).to_have_accessible_name("Quarterly results")
    expect(page.locator("#titled-figure")).to_have_accessible_name("Standalone figure")
    expect(page.locator("#formula")).to_have_accessible_name("Formula")
    expect(page.locator("#term")).to_have_accessible_name("")
    expect(page.locator("#definition")).to_have_accessible_name("")
    assert page.aria_snapshot() == (
        '- main "Semantics":\n'
        '  - figure "Quarterly results":\n'
        '    - img "Quarterly chart"\n'
        '    - text: Quarterly results\n'
        '  - figure "Standalone figure": Figure body\n'
        "  - term: API\n"
        "  - definition: Application programming interface\n"
        "  - term: Fetch\n"
        '  - math "Formula": x'
    )


@case
def native_iframe_snapshot_role_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Iframe probe">
          <iframe id="title-frame" title="Frame title" srcdoc="<button>Inside</button>"></iframe>
          <iframe id="aria-frame" aria-label="ARIA frame" srcdoc="<p>Text</p>"></iframe>
          <iframe id="labelled-frame" aria-labelledby="frame-label" srcdoc="<p>Text</p>"></iframe>
          <span id="frame-label">Labelled frame</span>
          <iframe id="hidden-frame" hidden title="Hidden frame" srcdoc="<p>Text</p>"></iframe>
          <iframe id="aria-hidden-frame" aria-hidden="true" title="Hidden frame" srcdoc="<p>Text</p>"></iframe>
        </main>
        """
    )

    assert page.aria_snapshot() == (
        '- main "Iframe probe":\n'
        "  - iframe\n"
        "  - iframe\n"
        "  - iframe\n"
        "  - text: Labelled frame"
    )
    assert page.locator("#title-frame").aria_snapshot() == "- iframe"
    assert page.locator("#aria-frame").aria_snapshot() == "- iframe"
    assert page.locator("#labelled-frame").aria_snapshot() == "- iframe"
    assert page.locator("#hidden-frame").aria_snapshot() == ""
    assert page.locator("#aria-hidden-frame").aria_snapshot() == ""


@case
def native_area_and_menu_snapshot_roles_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Native misc probe">
          <map name="m">
            <area id="area-link" shape="rect" coords="0,0,10,10" href="/area" alt="Area link">
          </map>
          <img id="mapped-image" usemap="#m" alt="Mapped image" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
          <menu id="plain-menu"><li>Menu item</li></menu>
          <menu id="toolbar-menu" type="toolbar"><li>Toolbar item</li></menu>
        </main>
        """
    )

    assert page.aria_snapshot() == (
        '- main "Native misc probe":\n'
        '  - link "Area link":\n'
        "    - /url: /area\n"
        '  - img "Mapped image"\n'
        "  - list:\n"
        "    - listitem: Menu item\n"
        "  - list:\n"
        "    - listitem: Toolbar item"
    )
    assert page.locator("#area-link").aria_snapshot() == '- link "Area link":\n  - /url: /area'
    assert page.locator("#plain-menu").aria_snapshot() == "- list:\n  - listitem: Menu item"
    assert page.locator("#toolbar-menu").aria_snapshot() == "- list:\n  - listitem: Toolbar item"


@case
def mark_role_and_snapshot_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Highlights">
          <mark id="plain">Highlighted</mark>
          <mark id="label" aria-label="Named mark">Label text</mark>
          <div id="explicit" role="mark" aria-label="Explicit named">Explicit body</div>
        </main>
        """
    )

    assert page.get_by_role("mark").evaluate_all("(els) => els.map(el => el.id)") == [
        "plain",
        "label",
        "explicit",
    ]
    assert page.get_by_role("mark", name="Highlighted").count() == 0
    assert page.get_by_role("mark", name="Named mark").count() == 0
    assert page.get_by_role("mark", name="Explicit named").count() == 0
    assert page.get_by_role("mark", name="Explicit body").count() == 0
    expect(page.locator("#plain")).to_have_role("mark")
    expect(page.locator("#explicit")).to_have_role("mark")
    expect(page.locator("#plain")).to_have_accessible_name("")
    expect(page.locator("#label")).to_have_accessible_name("")
    expect(page.locator("#explicit")).to_have_accessible_name("")
    assert page.aria_snapshot() == (
        '- main "Highlights":\n'
        "  - mark: Highlighted\n"
        "  - mark: Label text\n"
        "  - mark: Explicit body"
    )


@case
def generic_role_name_and_snapshot_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Generic">
          <div id="plain">Plain div</div>
          <div id="named" aria-label="Named generic">Visible named</div>
          <div id="generic-text" role="generic" aria-label="Named generic">Generic text</div>
          <div id="generic-child" role="generic" aria-label="Child generic"><button>Child button</button></div>
        </main>
        """
    )

    assert page.get_by_role("generic").evaluate_all("(els) => els.map(el => el.id)") == [
        "generic-text",
        "generic-child",
    ]
    assert page.get_by_role("generic", name="Named generic").count() == 0
    assert page.get_by_role("generic", name="Generic text").count() == 0
    assert page.get_by_role("generic", name="Child generic").count() == 0
    expect(page.locator("#generic-text")).to_have_role("generic")
    expect(page.locator("#generic-child")).to_have_role("generic")
    expect(page.locator("#generic-text")).to_have_accessible_name("")
    expect(page.locator("#generic-child")).to_have_accessible_name("")
    assert page.aria_snapshot() == (
        '- main "Generic":\n'
        "  - text: Plain div Visible named\n"
        "  - generic: Generic text\n"
        "  - generic:\n"
        '    - button "Child button"'
    )


@case
def live_region_role_names_and_snapshots_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Live roles">
          <div id="alert" role="alert">Danger</div>
          <div id="log" role="log">Log entry</div>
          <div id="timer" role="timer">10:00</div>
          <div id="marquee" role="marquee">Ticker</div>
          <div id="note" role="note">Remember this</div>
          <div id="named-alert" role="alert" aria-label="Warning label">Visible alert</div>
          <div id="button-alert" role="alert"><button>Fix</button></div>
        </main>
        """
    )

    assert page.get_by_role("alert").evaluate_all("(els) => els.map(el => el.id)") == [
        "alert",
        "named-alert",
        "button-alert",
    ]
    assert page.get_by_role("log").evaluate_all("(els) => els.map(el => el.id)") == ["log"]
    assert page.get_by_role("timer").evaluate_all("(els) => els.map(el => el.id)") == ["timer"]
    assert page.get_by_role("marquee").evaluate_all("(els) => els.map(el => el.id)") == ["marquee"]
    assert page.get_by_role("note").evaluate_all("(els) => els.map(el => el.id)") == ["note"]
    assert page.get_by_role("alert", name="Warning label").get_attribute("id") == "named-alert"
    assert page.get_by_role("alert", name="Danger").count() == 0
    assert page.get_by_role("log", name="Log entry").count() == 0
    assert page.get_by_role("timer", name="10:00").count() == 0
    assert page.get_by_role("marquee", name="Ticker").count() == 0
    assert page.get_by_role("note", name="Remember this").count() == 0
    expect(page.locator("#alert")).to_have_role("alert")
    expect(page.locator("#log")).to_have_role("log")
    expect(page.locator("#timer")).to_have_role("timer")
    expect(page.locator("#marquee")).to_have_role("marquee")
    expect(page.locator("#note")).to_have_role("note")
    expect(page.locator("#alert")).to_have_accessible_name("")
    expect(page.locator("#named-alert")).to_have_accessible_name("Warning label")
    expect(page.locator("#log")).to_have_accessible_name("")
    assert page.aria_snapshot() == (
        '- main "Live roles":\n'
        "  - alert: Danger\n"
        "  - log: Log entry\n"
        "  - timer: 10:00\n"
        "  - marquee: Ticker\n"
        "  - note: Remember this\n"
        '  - alert "Warning label": Visible alert\n'
        "  - alert:\n"
        '    - button "Fix"'
    )


@case
def value_role_and_tooltip_names_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="More roles">
          <div id="status" role="status">Saved</div>
          <div id="named-status" role="status" aria-label="Save status">Saved label body</div>
          <output id="output">7</output>
          <div id="feed" role="feed">Feed text</div>
          <div id="toolbar" role="toolbar">Tools</div>
          <div id="separator" role="separator">---</div>
          <div id="named-separator" role="separator" aria-label="Split">---</div>
          <div id="tooltip" role="tooltip">Help text</div>
        </main>
        """
    )

    assert page.get_by_role("status").evaluate_all("(els) => els.map(el => el.id)") == [
        "status",
        "named-status",
        "output",
    ]
    assert page.get_by_role("feed").evaluate_all("(els) => els.map(el => el.id)") == ["feed"]
    assert page.get_by_role("toolbar").evaluate_all("(els) => els.map(el => el.id)") == ["toolbar"]
    assert page.get_by_role("separator").evaluate_all("(els) => els.map(el => el.id)") == [
        "separator",
        "named-separator",
    ]
    assert page.get_by_role("tooltip").evaluate_all("(els) => els.map(el => el.id)") == ["tooltip"]
    assert page.get_by_role("status", name="Save status").get_attribute("id") == "named-status"
    assert page.get_by_role("separator", name="Split").get_attribute("id") == "named-separator"
    assert page.get_by_role("tooltip", name="Help text").get_attribute("id") == "tooltip"
    assert page.get_by_role("status", name="Saved").count() == 0
    assert page.get_by_role("status", name="7").count() == 0
    assert page.get_by_role("feed", name="Feed text").count() == 0
    assert page.get_by_role("toolbar", name="Tools").count() == 0
    assert page.get_by_role("separator", name="---").count() == 0
    expect(page.locator("#status")).to_have_accessible_name("")
    expect(page.locator("#named-status")).to_have_accessible_name("Save status")
    expect(page.locator("#output")).to_have_accessible_name("")
    expect(page.locator("#feed")).to_have_accessible_name("")
    expect(page.locator("#toolbar")).to_have_accessible_name("")
    expect(page.locator("#separator")).to_have_accessible_name("")
    expect(page.locator("#named-separator")).to_have_accessible_name("Split")
    expect(page.locator("#tooltip")).to_have_accessible_name("Help text")
    assert page.aria_snapshot() == (
        '- main "More roles":\n'
        "  - status: Saved\n"
        '  - status "Save status": Saved label body\n'
        '  - status: "7"\n'
        "  - feed: Feed text\n"
        "  - toolbar: Tools\n"
        '  - separator: "---"\n'
        '  - separator "Split": "---"\n'
        '  - tooltip "Help text"'
    )


@case
def native_meter_value_snapshots_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Meter probe">
          <meter id="empty-meter" value="0.7"></meter>
          <meter id="text-meter" value="0.5">half</meter>
          <meter id="named-empty-meter" aria-label="Fuel" value="0.5"></meter>
          <meter id="named-text-meter" aria-label="Fuel" value="0.5">half</meter>
          <div id="role-empty-meter" role="meter" aria-valuenow="7"></div>
          <div id="role-text-meter" role="meter" aria-valuenow="7">seven</div>
        </main>
        """
    )

    assert page.aria_snapshot() == (
        '- main "Meter probe":\n'
        "  - meter\n"
        "  - meter: half\n"
        '  - meter "Fuel"\n'
        '  - meter "Fuel": half\n'
        "  - meter\n"
        "  - meter: seven"
    )
    assert page.locator("#empty-meter").aria_snapshot() == "- meter"
    assert page.locator("#text-meter").aria_snapshot() == "- meter: half"
    assert page.locator("#named-empty-meter").aria_snapshot() == '- meter "Fuel"'
    assert page.locator("#named-text-meter").aria_snapshot() == '- meter "Fuel": half'
    assert page.locator("#role-empty-meter").aria_snapshot() == "- meter"
    assert page.locator("#role-text-meter").aria_snapshot() == "- meter: seven"


@case
def structure_role_names_and_snapshots_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Structure roles">
          <div id="alertdialog" role="alertdialog">Confirm delete</div>
          <div id="named-alertdialog" role="alertdialog" aria-label="Delete dialog">Visible dialog body</div>
          <div id="application" role="application">App text</div>
          <div id="directory" role="directory">Entry text</div>
          <div id="document" role="document">Doc text</div>
          <div id="grid" role="grid" aria-label="Data grid"><div role="row"><span id="gridcell" role="gridcell">Cell A</span></div></div>
          <div id="scrollbar" role="scrollbar" aria-label="Scroll" aria-valuenow="50" aria-valuemin="0" aria-valuemax="100">Scroll body</div>
        </main>
        """
    )

    assert page.get_by_role("alertdialog").evaluate_all("(els) => els.map(el => el.id)") == [
        "alertdialog",
        "named-alertdialog",
    ]
    assert page.get_by_role("application").evaluate_all("(els) => els.map(el => el.id)") == ["application"]
    assert page.get_by_role("directory").evaluate_all("(els) => els.map(el => el.id)") == ["directory"]
    assert page.get_by_role("document").evaluate_all("(els) => els.map(el => `${el.tagName}#${el.id}`)") == [
        "HTML#",
        "DIV#document",
    ]
    assert page.get_by_role("gridcell", name="Cell A").get_attribute("id") == "gridcell"
    assert page.get_by_role("scrollbar", name="Scroll").get_attribute("id") == "scrollbar"
    assert page.get_by_role("alertdialog", name="Confirm delete").count() == 0
    assert page.get_by_role("alertdialog", name="Delete dialog").get_attribute("id") == "named-alertdialog"
    assert page.get_by_role("application", name="App text").count() == 0
    assert page.get_by_role("directory", name="Entry text").count() == 0
    assert page.get_by_role("document", name="Doc text").count() == 0
    assert page.get_by_role("scrollbar", name="Scroll body").count() == 0
    expect(page.locator("#alertdialog")).to_have_role("alertdialog")
    expect(page.locator("html")).to_have_role("document")
    expect(page.locator("#document")).to_have_role("document")
    expect(page.locator("#alertdialog")).to_have_accessible_name("")
    expect(page.locator("#named-alertdialog")).to_have_accessible_name("Delete dialog")
    expect(page.locator("#application")).to_have_accessible_name("")
    expect(page.locator("#directory")).to_have_accessible_name("")
    expect(page.locator("#document")).to_have_accessible_name("")
    expect(page.locator("#gridcell")).to_have_accessible_name("Cell A")
    expect(page.locator("#scrollbar")).to_have_accessible_name("Scroll")
    assert page.aria_snapshot() == (
        '- main "Structure roles":\n'
        "  - alertdialog: Confirm delete\n"
        '  - alertdialog "Delete dialog": Visible dialog body\n'
        "  - application: App text\n"
        "  - directory: Entry text\n"
        "  - document: Doc text\n"
        '  - grid "Data grid":\n'
        '    - row "Cell A":\n'
        '      - gridcell "Cell A"\n'
        '  - scrollbar "Scroll": Scroll body'
    )
    assert page.locator("html").aria_snapshot() == (
        "- document:\n"
        '  - main "Structure roles":\n'
        "    - alertdialog: Confirm delete\n"
        '    - alertdialog "Delete dialog": Visible dialog body\n'
        "    - application: App text\n"
        "    - directory: Entry text\n"
        "    - document: Doc text\n"
        '    - grid "Data grid":\n'
        '      - row "Cell A":\n'
        '        - gridcell "Cell A"\n'
        '    - scrollbar "Scroll": Scroll body'
    )


@case
def container_widget_role_names_and_snapshots_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="More widget roles">
          <div id="tablist" role="tablist">Tabs label text<div id="tab-a" role="tab" aria-selected="true">Overview</div></div>
          <div id="named-tablist" role="tablist" aria-label="Named tabs"><div role="tab">Named tab</div></div>
          <div id="listbox" role="listbox"><div id="opt-a" role="option" aria-selected="true">Alpha</div><div role="option">Beta</div></div>
          <div id="named-listbox" role="listbox" aria-label="Choices"><div role="option">Gamma</div></div>
          <div id="combobox" role="combobox">Combo text</div>
          <div id="named-combobox" role="combobox" aria-label="Combo label">Combo body</div>
          <div id="radiogroup" role="radiogroup"><div role="radio" aria-checked="true">One</div></div>
          <div id="named-radiogroup" role="radiogroup" aria-label="Named group"><div role="radio">Two</div></div>
          <div id="tree" role="tree"><div role="treeitem">Root</div></div>
          <div id="named-tree" role="tree" aria-label="Files"><div role="treeitem">File</div></div>
          <div id="menu" role="menu"><div role="menuitem">Open</div></div>
          <div id="named-menu" role="menu" aria-label="Actions"><div role="menuitem">Close</div></div>
          <div id="menubar" role="menubar"><div role="menuitem">File</div></div>
          <div id="named-menubar" role="menubar" aria-label="Main menu"><div role="menuitem">Edit</div></div>
        </main>
        """
    )

    for role, ids in {
        "tablist": ["tablist", "named-tablist"],
        "listbox": ["listbox", "named-listbox"],
        "combobox": ["combobox", "named-combobox"],
        "radiogroup": ["radiogroup", "named-radiogroup"],
        "tree": ["tree", "named-tree"],
        "menu": ["menu", "named-menu"],
        "menubar": ["menubar", "named-menubar"],
    }.items():
        assert page.get_by_role(role).evaluate_all("(els) => els.map(el => el.id)") == ids

    for role, name, expected_id in [
        ("tablist", "Named tabs", "named-tablist"),
        ("listbox", "Choices", "named-listbox"),
        ("combobox", "Combo label", "named-combobox"),
        ("radiogroup", "Named group", "named-radiogroup"),
        ("tree", "Files", "named-tree"),
        ("menu", "Actions", "named-menu"),
        ("menubar", "Main menu", "named-menubar"),
    ]:
        assert page.get_by_role(role, name=name).get_attribute("id") == expected_id

    for role, name in [
        ("tablist", "Tabs label text"),
        ("listbox", "Alpha Beta"),
        ("combobox", "Combo text"),
        ("radiogroup", "One"),
        ("tree", "Root"),
        ("menu", "Open"),
        ("menubar", "File"),
    ]:
        assert page.get_by_role(role, name=name).count() == 0

    for selector in ["#tablist", "#listbox", "#combobox", "#radiogroup", "#tree", "#menu", "#menubar"]:
        expect(page.locator(selector)).to_have_accessible_name("")
    expect(page.locator("#named-tablist")).to_have_accessible_name("Named tabs")
    expect(page.locator("#named-listbox")).to_have_accessible_name("Choices")
    expect(page.locator("#named-combobox")).to_have_accessible_name("Combo label")
    expect(page.locator("#named-radiogroup")).to_have_accessible_name("Named group")
    expect(page.locator("#named-tree")).to_have_accessible_name("Files")
    expect(page.locator("#named-menu")).to_have_accessible_name("Actions")
    expect(page.locator("#named-menubar")).to_have_accessible_name("Main menu")
    assert page.aria_snapshot() == (
        '- main "More widget roles":\n'
        "  - tablist:\n"
        "    - text: Tabs label text\n"
        '    - tab "Overview" [selected]\n'
        '  - tablist "Named tabs":\n'
        '    - tab "Named tab"\n'
        "  - listbox:\n"
        '    - option "Alpha" [selected]\n'
        '    - option "Beta"\n'
        '  - listbox "Choices":\n'
        '    - option "Gamma"\n'
        "  - combobox: Combo text\n"
        '  - combobox "Combo label": Combo body\n'
        "  - radiogroup:\n"
        '    - radio "One" [checked]\n'
        '  - radiogroup "Named group":\n'
        '    - radio "Two"\n'
        "  - tree:\n"
        '    - treeitem "Root"\n'
        '  - tree "Files":\n'
        '    - treeitem "File"\n'
        "  - menu:\n"
        '    - menuitem "Open"\n'
        '  - menu "Actions":\n'
        '    - menuitem "Close"\n'
        "  - menubar:\n"
        '    - menuitem "File"\n'
        '  - menubar "Main menu":\n'
        '    - menuitem "Edit"'
    )


@case
def composite_role_names_and_snapshots_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Composite roles">
          <div id="grid" role="grid">Grid label text<div role="row"><span id="gridcell" role="gridcell">Cell A</span></div></div>
          <div id="named-grid" role="grid" aria-label="Named grid"><div role="row"><span role="gridcell">Cell B</span></div></div>
          <table id="table"><caption>Native table title</caption><tr><th id="th">Head</th><td id="td">Cell</td></tr></table>
          <table id="aria-table" aria-label="Aria table"><tr><td>Aria cell</td></tr></table>
          <div id="table-role" role="table">Role table text<div role="row"><span role="cell">Role cell</span></div></div>
          <div id="named-table-role" role="table" aria-label="Named table"><div role="row"><span role="cell">Named role cell</span></div></div>
          <div id="group" role="group">Group text</div>
          <div id="named-group" role="group" aria-label="Named group">Group body</div>
          <fieldset id="fieldset"><legend>Legend name</legend><input></fieldset>
          <dialog id="dialog" open>Dialog text</dialog>
          <dialog id="named-dialog" aria-label="Named dialog" open>Dialog body</dialog>
          <section id="region" aria-label="Region name">Region body</section>
          <article id="article">Article body</article>
          <article id="named-article" aria-label="Named article">Named article body</article>
          <aside id="aside">Aside body</aside>
          <aside id="named-aside" aria-label="Named aside">Named aside body</aside>
        </main>
        """
    )

    assert page.get_by_role("grid", name="Named grid").get_attribute("id") == "named-grid"
    assert page.get_by_role("table", name="Native table title").get_attribute("id") == "table"
    assert page.get_by_role("table", name="Aria table").get_attribute("id") == "aria-table"
    assert page.get_by_role("table", name="Named table").get_attribute("id") == "named-table-role"
    assert page.get_by_role("group", name="Named group").get_attribute("id") == "named-group"
    assert page.get_by_role("group", name="Legend name").get_attribute("id") == "fieldset"
    assert page.get_by_role("dialog", name="Named dialog").get_attribute("id") == "named-dialog"
    assert page.get_by_role("region", name="Region name").get_attribute("id") == "region"
    assert page.get_by_role("article", name="Named article").get_attribute("id") == "named-article"
    assert page.get_by_role("complementary", name="Named aside").get_attribute("id") == "named-aside"

    for role, name in [
        ("grid", "Grid label text Cell A"),
        ("table", "Role table text Role cell"),
        ("group", "Group text"),
        ("dialog", "Dialog text"),
        ("region", "Region body"),
        ("article", "Article body"),
        ("complementary", "Aside body"),
    ]:
        assert page.get_by_role(role, name=name).count() == 0

    for selector in ["#grid", "#table-role", "#group", "#dialog", "#article", "#aside"]:
        expect(page.locator(selector)).to_have_accessible_name("")
    expect(page.locator("#table")).to_have_accessible_name("Native table title")
    expect(page.locator("#fieldset")).to_have_accessible_name("Legend name")
    expect(page.locator("#region")).to_have_accessible_name("Region name")
    expect(page.locator("#named-aside")).to_have_accessible_name("Named aside")
    assert page.aria_snapshot() == (
        '- main "Composite roles":\n'
        "  - grid:\n"
        "    - text: Grid label text\n"
        '    - row "Cell A":\n'
        '      - gridcell "Cell A"\n'
        '  - grid "Named grid":\n'
        '    - row "Cell B":\n'
        '      - gridcell "Cell B"\n'
        '  - table "Native table title":\n'
        "    - caption: Native table title\n"
        "    - rowgroup:\n"
        '      - row "Head Cell":\n'
        '        - rowheader "Head"\n'
        '        - cell "Cell"\n'
        '  - table "Aria table":\n'
        "    - rowgroup:\n"
        '      - row "Aria cell":\n'
        '        - cell "Aria cell"\n'
        "  - table:\n"
        "    - text: Role table text\n"
        '    - row "Role cell":\n'
        '      - cell "Role cell"\n'
        '  - table "Named table":\n'
        '    - row "Named role cell":\n'
        '      - cell "Named role cell"\n'
        "  - group: Group text\n"
        '  - group "Named group": Group body\n'
        '  - group "Legend name":\n'
        "    - text: Legend name\n"
        "    - textbox\n"
        "  - dialog: Dialog text\n"
        '  - dialog "Named dialog": Dialog body\n'
        '  - region "Region name": Region body\n'
        "  - article: Article body\n"
        '  - article "Named article": Named article body\n'
        "  - complementary: Aside body\n"
        '  - complementary "Named aside": Named aside body'
    )


@case
def landmark_treegrid_tabpanel_names_and_snapshots_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <header id="banner">Header text</header>
        <header id="named-banner" aria-label="Site banner">Ignored banner</header>
        <footer id="contentinfo">Footer text</footer>
        <footer id="named-contentinfo" aria-label="Site footer">Ignored footer</footer>
        <nav id="nav-text">Nav text</nav>
        <nav id="nav-named" aria-label="Primary nav">Nav body</nav>
        <main id="main-text">Main text</main>
        <main id="main-named" aria-label="Main area">Main body</main>
        <search id="search-text">Search text</search>
        <search id="search-named" aria-label="Search area">Search body</search>
        <div id="treegrid" role="treegrid">Treegrid text<div role="row"><span role="gridcell">Cell</span></div></div>
        <div id="named-treegrid" role="treegrid" aria-label="Data tree"><div role="row"><span role="gridcell">Named cell</span></div></div>
        <div id="tabpanel" role="tabpanel">Panel text</div>
        <div id="named-tabpanel" role="tabpanel" aria-label="Panel label">Panel body</div>
        """
    )

    assert page.get_by_role("banner").evaluate_all("(els) => els.map(el => el.id)") == ["banner", "named-banner"]
    assert page.get_by_role("contentinfo").evaluate_all("(els) => els.map(el => el.id)") == [
        "contentinfo",
        "named-contentinfo",
    ]
    assert page.get_by_role("navigation").evaluate_all("(els) => els.map(el => el.id)") == ["nav-text", "nav-named"]
    assert page.get_by_role("main").evaluate_all("(els) => els.map(el => el.id)") == ["main-text", "main-named"]
    assert page.get_by_role("search").evaluate_all("(els) => els.map(el => el.id)") == ["search-text", "search-named"]
    assert page.get_by_role("treegrid").evaluate_all("(els) => els.map(el => el.id)") == [
        "treegrid",
        "named-treegrid",
    ]
    assert page.get_by_role("tabpanel").evaluate_all("(els) => els.map(el => el.id)") == [
        "tabpanel",
        "named-tabpanel",
    ]

    for role, name, expected_id in [
        ("banner", "Site banner", "named-banner"),
        ("contentinfo", "Site footer", "named-contentinfo"),
        ("navigation", "Primary nav", "nav-named"),
        ("main", "Main area", "main-named"),
        ("search", "Search area", "search-named"),
        ("treegrid", "Data tree", "named-treegrid"),
        ("tabpanel", "Panel label", "named-tabpanel"),
    ]:
        assert page.get_by_role(role, name=name).get_attribute("id") == expected_id

    for role, name in [
        ("banner", "Header text"),
        ("contentinfo", "Footer text"),
        ("navigation", "Nav text"),
        ("main", "Main text"),
        ("search", "Search text"),
        ("treegrid", "Treegrid text Cell"),
        ("tabpanel", "Panel text"),
    ]:
        assert page.get_by_role(role, name=name).count() == 0

    for selector in ["#banner", "#contentinfo", "#nav-text", "#main-text", "#search-text", "#treegrid", "#tabpanel"]:
        expect(page.locator(selector)).to_have_accessible_name("")
    expect(page.locator("#named-banner")).to_have_accessible_name("Site banner")
    expect(page.locator("#named-contentinfo")).to_have_accessible_name("Site footer")
    expect(page.locator("#nav-named")).to_have_accessible_name("Primary nav")
    expect(page.locator("#main-named")).to_have_accessible_name("Main area")
    expect(page.locator("#search-named")).to_have_accessible_name("Search area")
    expect(page.locator("#named-treegrid")).to_have_accessible_name("Data tree")
    expect(page.locator("#named-tabpanel")).to_have_accessible_name("Panel label")
    assert page.aria_snapshot() == (
        "- banner: Header text\n"
        '- banner "Site banner": Ignored banner\n'
        "- contentinfo: Footer text\n"
        '- contentinfo "Site footer": Ignored footer\n'
        "- navigation: Nav text\n"
        '- navigation "Primary nav": Nav body\n'
        "- main: Main text\n"
        '- main "Main area": Main body\n'
        "- search: Search text\n"
        '- search "Search area": Search body\n'
        "- treegrid:\n"
        "  - text: Treegrid text\n"
        '  - row "Cell":\n'
        '    - gridcell "Cell"\n'
        '- treegrid "Data tree":\n'
        '  - row "Named cell":\n'
        '    - gridcell "Named cell"\n'
        "- tabpanel: Panel text\n"
        '- tabpanel "Panel label": Panel body'
    )


@case
def list_table_container_role_values_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <ul id="ul"><li id="li">Native item</li><li id="li-label" aria-label="Native label">Native body</li></ul>
        <ol id="ol" aria-label="Native list"><li>Ordered item</li></ol>
        <table id="table"><thead id="thead"><tr><th id="h">Head</th></tr></thead><tbody id="tbody"><tr><td id="td">Cell</td></tr></tbody></table>
        <div id="role-list" role="list"><div id="role-li" role="listitem">Role item</div><div id="role-li-label" role="listitem" aria-label="Role label">Role body</div></div>
        <div id="role-rowgroup" role="rowgroup"><div id="role-row" role="row">Role row text</div><div id="role-row-label" role="row" aria-label="Role row label">Role row body</div></div>
        <div id="role-cell" role="cell" aria-label="Cell label">Cell body</div>
        <div id="role-columnheader" role="columnheader" aria-label="Column label">Column body</div>
        <div id="role-rowheader" role="rowheader" aria-label="Row label">Row body</div>
        """
    )

    assert page.get_by_role("list", name="Native list").get_attribute("id") == "ol"
    assert page.get_by_role("list", name="Native item").count() == 0
    assert page.get_by_role("list", name="Role item").count() == 0
    assert page.get_by_role("listitem", name="Native item").count() == 0
    assert page.get_by_role("listitem", name="Native label").get_attribute("id") == "li-label"
    assert page.get_by_role("listitem", name="Role item").count() == 0
    assert page.get_by_role("listitem", name="Role label").get_attribute("id") == "role-li-label"
    assert page.get_by_role("rowgroup").evaluate_all("(els) => els.map(el => el.id)") == [
        "thead",
        "tbody",
        "role-rowgroup",
    ]
    assert page.get_by_role("rowgroup", name="Head").count() == 0
    assert page.get_by_role("row", name="Role row text").get_attribute("id") == "role-row"
    assert page.get_by_role("row", name="Role row label").get_attribute("id") == "role-row-label"
    assert page.get_by_role("columnheader", name="Head").get_attribute("id") == "h"
    assert page.get_by_role("columnheader", name="Column label").get_attribute("id") == "role-columnheader"
    assert page.get_by_role("rowheader", name="Row label").get_attribute("id") == "role-rowheader"
    assert page.get_by_role("cell", name="Cell label").get_attribute("id") == "role-cell"

    for selector in ["#ul", "#li", "#role-list", "#role-li", "#role-rowgroup"]:
        expect(page.locator(selector)).to_have_accessible_name("")
    expect(page.locator("#li-label")).to_have_accessible_name("Native label")
    expect(page.locator("#role-li-label")).to_have_accessible_name("Role label")
    expect(page.locator("#role-row")).to_have_accessible_name("Role row text")
    expect(page.locator("#role-row-label")).to_have_accessible_name("Role row label")
    expect(page.locator("#role-cell")).to_have_accessible_name("Cell label")
    expect(page.locator("#role-columnheader")).to_have_accessible_name("Column label")
    expect(page.locator("#role-rowheader")).to_have_accessible_name("Row label")
    assert page.aria_snapshot() == (
        "- list:\n"
        "  - listitem: Native item\n"
        '  - listitem "Native label": Native body\n'
        '- list "Native list":\n'
        "  - listitem: Ordered item\n"
        "- table:\n"
        "  - rowgroup:\n"
        '    - row "Head":\n'
        '      - columnheader "Head"\n'
        "  - rowgroup:\n"
        '    - row "Cell":\n'
        '      - cell "Cell"\n'
        "- list:\n"
        "  - listitem: Role item\n"
        '  - listitem "Role label": Role body\n'
        "- rowgroup:\n"
        '  - row "Role row text"\n'
        '  - row "Role row label": Role row body\n'
        '- cell "Cell label": Cell body\n'
        '- columnheader "Column label": Column body\n'
        '- rowheader "Row label": Row body'
    )


@case
def presentational_role_conflicts_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="None probe">
          <div id="none" role="none">None text</div>
          <div id="none-label" role="none" aria-label="None label">None label text</div>
          <div id="none-labelledby" role="none" aria-labelledby="none-ref">None labelledby text</div><span id="none-ref">None ref</span>
          <button id="none-button" role="none">Button none</button>
          <a id="none-link" role="none" href="/x">Link none</a>
          <div id="presentation" role="presentation">Presentation text</div>
          <div id="presentation-label" role="presentation" aria-label="Presentation label">Presentation label text</div>
          <button id="presentation-button" role="presentation">Button presentation</button>
          <div id="mixed-none" role="none button">Mixed none</div>
          <div id="mixed-presentation" role="presentation button">Mixed presentation</div>
        </main>
        """
    )

    assert page.get_by_role("none").evaluate_all("(els) => els.map(el => el.id)") == ["none", "mixed-none"]
    assert page.get_by_role("presentation").evaluate_all("(els) => els.map(el => el.id)") == [
        "presentation",
        "mixed-presentation",
    ]
    assert page.get_by_role("none", name="None text").count() == 0
    assert page.get_by_role("none", name="None label").count() == 0
    assert page.get_by_role("none", name="None ref").count() == 0
    assert page.get_by_role("presentation", name="Presentation text").count() == 0
    assert page.get_by_role("presentation", name="Presentation label").count() == 0
    assert page.get_by_role("button").evaluate_all("(els) => els.map(el => el.id)") == [
        "none-button",
        "presentation-button",
    ]
    assert page.get_by_role("button", name="Button none").get_attribute("id") == "none-button"
    assert page.get_by_role("button", name="Button presentation").get_attribute("id") == "presentation-button"
    assert page.get_by_role("link", name="Link none").get_attribute("id") == "none-link"

    expect(page.locator("#none")).to_have_role("none")
    expect(page.locator("#presentation")).to_have_role("presentation")
    expect(page.locator("#none-button")).to_have_role("button")
    expect(page.locator("#presentation-button")).to_have_role("button")
    for selector in ["#none", "#presentation", "#mixed-none", "#mixed-presentation"]:
        expect(page.locator(selector)).to_have_accessible_name("")
    expect(page.locator("#none-label")).to_have_accessible_name("None label")
    expect(page.locator("#none-labelledby")).to_have_accessible_name("None ref")
    expect(page.locator("#presentation-label")).to_have_accessible_name("Presentation label")
    expect(page.locator("#none-button")).to_have_accessible_name("Button none")
    expect(page.locator("#presentation-button")).to_have_accessible_name("Button presentation")
    assert page.aria_snapshot() == (
        '- main "None probe":\n'
        "  - text: None text None label text None labelledby text None ref\n"
        '  - button "Button none"\n'
        '  - link "Link none":\n'
        "    - /url: /x\n"
        "  - text: Presentation text Presentation label text\n"
        '  - button "Button presentation"\n'
        "  - text: Mixed none Mixed presentation"
    )


@case
def presentational_table_conflicts_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Table presentation probe">
          <table id="plain-table"><tbody id="plain-body"><tr id="plain-row"><td id="plain-cell">Plain</td></tr></tbody></table>
          <table id="none-table" role="none"><tbody id="none-body"><tr id="none-row"><td id="none-cell">None table</td></tr></tbody></table>
          <table id="presentation-table" role="presentation"><tbody id="presentation-body"><tr id="presentation-row"><td id="presentation-cell">Presentation table</td></tr></tbody></table>
          <table id="focus-none-table" role="none" tabindex="0"><tbody id="focus-none-body"><tr id="focus-none-row"><td id="focus-none-cell">Focus none</td></tr></tbody></table>
          <table id="label-none-table" role="none" aria-label="Label none"><tbody id="label-none-body"><tr id="label-none-row"><td id="label-none-cell">Label none</td></tr></tbody></table>
        </main>
        """
    )

    assert page.get_by_role("table").evaluate_all("(els) => els.map(el => el.id)") == [
        "plain-table",
        "focus-none-table",
        "label-none-table",
    ]
    assert page.get_by_role("row").evaluate_all("(els) => els.map(el => el.id)") == [
        "plain-row",
        "focus-none-row",
        "label-none-row",
    ]
    assert page.get_by_role("cell").evaluate_all("(els) => els.map(el => el.id)") == [
        "plain-cell",
        "focus-none-cell",
        "label-none-cell",
    ]
    assert page.get_by_role("none").evaluate_all("(els) => els.map(el => el.id)") == [
        "none-table",
        "none-body",
        "none-row",
        "none-cell",
    ]
    assert page.get_by_role("presentation").evaluate_all("(els) => els.map(el => el.id)") == [
        "presentation-table",
        "presentation-body",
        "presentation-row",
        "presentation-cell",
    ]
    expect(page.locator("#none-table")).to_have_role("none")
    expect(page.locator("#none-row")).to_have_role("none")
    expect(page.locator("#presentation-table")).to_have_role("presentation")
    expect(page.locator("#presentation-cell")).to_have_role("presentation")
    expect(page.locator("#focus-none-table")).to_have_role("table")
    expect(page.locator("#label-none-table")).to_have_role("table")
    assert page.aria_snapshot() == (
        '- main "Table presentation probe":\n'
        "  - table:\n"
        "    - rowgroup:\n"
        '      - row "Plain":\n'
        '        - cell "Plain"\n'
        "  - text: None table Presentation table\n"
        "  - table:\n"
        "    - rowgroup:\n"
        '      - row "Focus none":\n'
        '        - cell "Focus none"\n'
        '  - table "Label none":\n'
        "    - rowgroup:\n"
        '      - row "Label none":\n'
        '        - cell "Label none"'
    )
    assert page.locator("#none-table").aria_snapshot() == "- text: None table"
    assert page.locator("#none-body").aria_snapshot() == "- text: None table"
    assert page.locator("#none-row").aria_snapshot() == "- text: None table"
    assert page.locator("#none-cell").aria_snapshot() == "- text: None table"
    assert page.locator("#presentation-table").aria_snapshot() == "- text: Presentation table"
    assert page.locator("#presentation-body").aria_snapshot() == "- text: Presentation table"
    assert page.locator("#presentation-row").aria_snapshot() == "- text: Presentation table"
    assert page.locator("#presentation-cell").aria_snapshot() == "- text: Presentation table"
    assert page.locator("#focus-none-table").aria_snapshot() == (
        "- table:\n"
        "  - rowgroup:\n"
        '    - row "Focus none":\n'
        '      - cell "Focus none"'
    )
    assert page.locator("#label-none-table").aria_snapshot() == (
        '- table "Label none":\n'
        "  - rowgroup:\n"
        '    - row "Label none":\n'
        '      - cell "Label none"'
    )


@case
def aria_state_filters_and_pressed_mixed_match_playwright(page):
    page.set_content(
        """
        <main aria-label="State probe">
          <button id="btn-expanded-false" aria-expanded="false">Closed</button>
          <button id="btn-expanded-true" aria-expanded="true">Open</button>
          <button id="btn-pressed-false" aria-pressed="false">Not pressed</button>
          <button id="btn-pressed-mixed" aria-pressed="mixed">Mixed pressed</button>
          <button id="btn-pressed-true" aria-pressed="true">Pressed</button>
          <div id="tab-false" role="tab" aria-selected="false">Tab false</div>
          <div id="tab-true" role="tab" aria-selected="true">Tab true</div>
          <div id="option-false" role="option" aria-selected="false">Option false</div>
          <div id="option-true" role="option" aria-selected="true">Option true</div>
          <div id="checkbox-false" role="checkbox" aria-checked="false">Check false</div>
          <div id="checkbox-true" role="checkbox" aria-checked="true">Check true</div>
          <div id="checkbox-mixed" role="checkbox" aria-checked="mixed">Check mixed</div>
          <input id="native-checkbox" type="checkbox" aria-label="Native check">
          <input id="native-checkbox-checked" type="checkbox" aria-label="Native checked" checked>
          <input id="native-checkbox-indeterminate" type="checkbox" aria-label="Native mixed">
          <div id="switch-false" role="switch" aria-checked="false">Switch false</div>
          <div id="switch-true" role="switch" aria-checked="true">Switch true</div>
          <div id="treeitem-expanded-false" role="treeitem" aria-expanded="false">Tree closed</div>
          <div id="treeitem-expanded-true" role="treeitem" aria-expanded="true">Tree open</div>
          <div id="button-disabled-false" role="button" aria-disabled="false">Enabled role</div>
          <div id="button-disabled-true" role="button" aria-disabled="true">Disabled role</div>
        </main>
        """
    )
    page.evaluate("document.querySelector('#native-checkbox-indeterminate').indeterminate = true")

    assert page.get_by_role("button", expanded=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "btn-expanded-false"
    ]
    assert page.get_by_role("button", expanded=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "btn-expanded-true"
    ]
    assert page.get_by_role("button", pressed=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "btn-expanded-false",
        "btn-expanded-true",
        "btn-pressed-false",
        "button-disabled-false",
        "button-disabled-true",
    ]
    assert page.get_by_role("button", pressed=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "btn-pressed-true"
    ]
    assert page.get_by_role("tab", selected=False).get_attribute("id") == "tab-false"
    assert page.get_by_role("tab", selected=True).get_attribute("id") == "tab-true"
    assert page.get_by_role("option", selected=False).get_attribute("id") == "option-false"
    assert page.get_by_role("option", selected=True).get_attribute("id") == "option-true"
    assert page.get_by_role("checkbox", checked=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "checkbox-false",
        "native-checkbox",
    ]
    assert page.get_by_role("checkbox", checked=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "checkbox-true",
        "native-checkbox-checked",
    ]
    assert page.get_by_role("switch", checked=False).get_attribute("id") == "switch-false"
    assert page.get_by_role("switch", checked=True).get_attribute("id") == "switch-true"
    assert page.get_by_role("treeitem", expanded=False).get_attribute("id") == "treeitem-expanded-false"
    assert page.get_by_role("treeitem", expanded=True).get_attribute("id") == "treeitem-expanded-true"
    assert page.get_by_role("button", disabled=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "btn-expanded-false",
        "btn-expanded-true",
        "btn-pressed-false",
        "btn-pressed-mixed",
        "btn-pressed-true",
        "button-disabled-false",
    ]
    assert page.get_by_role("button", disabled=True).get_attribute("id") == "button-disabled-true"
    assert page.aria_snapshot() == (
        '- main "State probe":\n'
        '  - button "Closed"\n'
        '  - button "Open" [expanded]\n'
        '  - button "Not pressed"\n'
        '  - button "Mixed pressed" [pressed=mixed]\n'
        '  - button "Pressed" [pressed]\n'
        '  - tab "Tab false"\n'
        '  - tab "Tab true" [selected]\n'
        '  - option "Option false"\n'
        '  - option "Option true" [selected]\n'
        '  - checkbox "Check false"\n'
        '  - checkbox "Check true" [checked]\n'
        '  - checkbox "Check mixed" [checked=mixed]\n'
        '  - checkbox "Native check"\n'
        '  - checkbox "Native checked" [checked]\n'
        '  - checkbox "Native mixed" [checked=mixed]\n'
        '  - switch "Switch false"\n'
        '  - switch "Switch true" [checked]\n'
        '  - treeitem "Tree closed"\n'
        '  - treeitem "Tree open" [expanded]\n'
        '  - button "Enabled role"\n'
        '  - button "Disabled role" [disabled]'
    )


@case
def aria_state_filter_defaults_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Role default states">
          <div id="checkbox-missing" role="checkbox">Role checkbox missing</div>
          <div id="checkbox-invalid" role="checkbox" aria-checked="invalid">Role checkbox invalid</div>
          <div id="checkbox-false" role="checkbox" aria-checked="false">Role checkbox false</div>
          <div id="checkbox-true" role="checkbox" aria-checked="true">Role checkbox true</div>
          <div id="checkbox-mixed" role="checkbox" aria-checked="mixed">Role checkbox mixed</div>
          <input id="native-checkbox" type="checkbox" aria-label="Native checkbox">
          <input id="native-checkbox-checked" type="checkbox" aria-label="Native checkbox checked" checked>
          <div id="radio-missing" role="radio">Role radio missing</div>
          <div id="radio-invalid" role="radio" aria-checked="invalid">Role radio invalid</div>
          <div id="radio-false" role="radio" aria-checked="false">Role radio false</div>
          <div id="radio-true" role="radio" aria-checked="true">Role radio true</div>
          <input id="native-radio" type="radio" aria-label="Native radio">
          <input id="native-radio-checked" type="radio" aria-label="Native radio checked" checked>
          <div id="switch-missing" role="switch">Switch missing</div>
          <div id="switch-invalid" role="switch" aria-checked="invalid">Switch invalid</div>
          <div id="switch-false" role="switch" aria-checked="false">Switch false</div>
          <div id="switch-true" role="switch" aria-checked="true">Switch true</div>
          <div id="switch-mixed" role="switch" aria-checked="mixed">Switch mixed</div>
          <div id="mic-missing" role="menuitemcheckbox">MIC missing</div>
          <div id="mic-invalid" role="menuitemcheckbox" aria-checked="invalid">MIC invalid</div>
          <div id="mic-false" role="menuitemcheckbox" aria-checked="false">MIC false</div>
          <div id="mic-true" role="menuitemcheckbox" aria-checked="true">MIC true</div>
          <div id="mic-mixed" role="menuitemcheckbox" aria-checked="mixed">MIC mixed</div>
          <div id="mir-missing" role="menuitemradio">MIR missing</div>
          <div id="mir-invalid" role="menuitemradio" aria-checked="invalid">MIR invalid</div>
          <div id="mir-false" role="menuitemradio" aria-checked="false">MIR false</div>
          <div id="mir-true" role="menuitemradio" aria-checked="true">MIR true</div>
          <select id="single" aria-label="Single">
            <option id="single-a">A</option>
            <option id="single-b" selected>B</option>
          </select>
          <select id="multi" aria-label="Multi" multiple>
            <option id="multi-a">A</option>
            <option id="multi-b" selected>B</option>
          </select>
          <div id="option-missing" role="option">Role option missing</div>
          <div id="option-invalid" role="option" aria-selected="invalid">Role option invalid</div>
          <div id="option-false" role="option" aria-selected="false">Role option false</div>
          <div id="option-true" role="option" aria-selected="true">Role option true</div>
          <div id="tab-missing" role="tab">Role tab missing</div>
          <div id="tab-invalid" role="tab" aria-selected="invalid">Role tab invalid</div>
          <div id="tab-false" role="tab" aria-selected="false">Role tab false</div>
          <div id="tab-true" role="tab" aria-selected="true">Role tab true</div>
          <button id="expanded-missing">Expanded missing</button>
          <button id="expanded-invalid" aria-expanded="invalid">Expanded invalid</button>
          <button id="expanded-false" aria-expanded="false">Expanded false</button>
          <button id="expanded-true" aria-expanded="true">Expanded true</button>
        </main>
        """
    )

    assert page.get_by_role("checkbox", checked=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "checkbox-missing",
        "checkbox-invalid",
        "checkbox-false",
        "native-checkbox",
    ]
    assert page.get_by_role("checkbox", checked=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "checkbox-true",
        "native-checkbox-checked",
    ]
    assert page.get_by_role("radio", checked=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "radio-missing",
        "radio-invalid",
        "radio-false",
        "native-radio",
    ]
    assert page.get_by_role("radio", checked=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "radio-true",
        "native-radio-checked",
    ]
    assert page.get_by_role("switch", checked=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "switch-missing",
        "switch-invalid",
        "switch-false",
    ]
    assert page.get_by_role("switch", checked=True).evaluate_all("(els) => els.map(el => el.id)") == ["switch-true"]
    assert page.get_by_role("menuitemcheckbox", checked=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "mic-missing",
        "mic-invalid",
        "mic-false",
    ]
    assert page.get_by_role("menuitemcheckbox", checked=True).evaluate_all("(els) => els.map(el => el.id)") == ["mic-true"]
    assert page.get_by_role("menuitemradio", checked=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "mir-missing",
        "mir-invalid",
        "mir-false",
    ]
    assert page.get_by_role("menuitemradio", checked=True).evaluate_all("(els) => els.map(el => el.id)") == ["mir-true"]
    assert page.get_by_role("option", selected=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "single-a",
        "multi-a",
        "option-missing",
        "option-invalid",
        "option-false",
    ]
    assert page.get_by_role("option", selected=True).evaluate_all("(els) => els.map(el => el.id)") == [
        "single-b",
        "multi-b",
        "option-true",
    ]
    assert page.get_by_role("tab", selected=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "tab-missing",
        "tab-invalid",
        "tab-false",
    ]
    assert page.get_by_role("tab", selected=True).evaluate_all("(els) => els.map(el => el.id)") == ["tab-true"]
    assert page.get_by_role("button", expanded=False).evaluate_all("(els) => els.map(el => el.id)") == [
        "expanded-invalid",
        "expanded-false",
    ]
    assert page.get_by_role("button", expanded=True).evaluate_all("(els) => els.map(el => el.id)") == ["expanded-true"]


@case
def aria_level_state_snapshots_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Level probe">
          <div id="tree" role="tree">
            <div id="tree-root" role="treeitem" aria-level="1" aria-expanded="true">Root</div>
            <div id="tree-child" role="treeitem" aria-level="2" aria-selected="true">Child</div>
            <div id="tree-all" role="treeitem" aria-level="2" aria-checked="mixed" aria-expanded="true" aria-selected="true">All</div>
            <div id="tree-invalid" role="treeitem" aria-level="bad">Invalid</div>
            <div id="tree-zero" role="treeitem" aria-level="0">Zero</div>
          </div>
          <div id="list" role="list">
            <div id="list-item" role="listitem" aria-level="3">Item</div>
          </div>
          <div id="row" role="row" aria-level="4" aria-selected="true">Row</div>
        </main>
        """
    )

    assert page.aria_snapshot() == (
        '- main "Level probe":\n'
        "  - tree:\n"
        '    - treeitem "Root" [expanded] [level=1]\n'
        '    - treeitem "Child" [level=2] [selected]\n'
        '    - treeitem "All" [checked=mixed] [expanded] [level=2] [selected]\n'
        '    - treeitem "Invalid"\n'
        '    - treeitem "Zero"\n'
        "  - list:\n"
        "    - listitem [level=3]: Item\n"
        '  - row "Row" [level=4] [selected]'
    )
    assert page.locator("#tree").aria_snapshot() == (
        "- tree:\n"
        '  - treeitem "Root" [expanded] [level=1]\n'
        '  - treeitem "Child" [level=2] [selected]\n'
        '  - treeitem "All" [checked=mixed] [expanded] [level=2] [selected]\n'
        '  - treeitem "Invalid"\n'
        '  - treeitem "Zero"'
    )
    assert page.locator("#list-item").aria_snapshot() == "- listitem [level=3]: Item"
    assert page.locator("#row").aria_snapshot() == '- row "Row" [level=4] [selected]'


@case
def heading_level_zero_and_snapshot_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Heading probe">
          <h1 id="h1">Native one</h1>
          <h3 id="h3">Native three</h3>
          <div id="role-missing" role="heading">Role missing</div>
          <div id="role-empty" role="heading" aria-level="">Role empty</div>
          <div id="role-invalid" role="heading" aria-level="bad">Role invalid</div>
          <div id="role-zero" role="heading" aria-level="0">Role zero</div>
          <div id="role-two" role="heading" aria-level="2">Role two</div>
          <div id="role-seven" role="heading" aria-level="7">Role seven</div>
        </main>
        """
    )

    assert page.get_by_role("heading").evaluate_all("(els) => els.map(el => el.id)") == [
        "h1",
        "h3",
        "role-missing",
        "role-empty",
        "role-invalid",
        "role-zero",
        "role-two",
        "role-seven",
    ]
    assert page.get_by_role("heading", level=0).evaluate_all("(els) => els.map(el => el.id)") == [
        "role-missing",
        "role-empty",
        "role-invalid",
        "role-zero",
    ]
    assert page.get_by_role("heading", level=1).evaluate_all("(els) => els.map(el => el.id)") == ["h1"]
    assert page.get_by_role("heading", level=2).evaluate_all("(els) => els.map(el => el.id)") == ["role-two"]
    assert page.get_by_role("heading", level=3).evaluate_all("(els) => els.map(el => el.id)") == ["h3"]
    assert page.get_by_role("heading", level=7).evaluate_all("(els) => els.map(el => el.id)") == ["role-seven"]
    assert page.aria_snapshot() == (
        '- main "Heading probe":\n'
        '  - heading "Native one" [level=1]\n'
        '  - heading "Native three" [level=3]\n'
        '  - heading "Role missing"\n'
        '  - heading "Role empty"\n'
        '  - heading "Role invalid"\n'
        '  - heading "Role zero"\n'
        '  - heading "Role two" [level=2]\n'
        '  - heading "Role seven" [level=7]'
    )


@case
def icon_control_names_and_snapshots_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Icon names">
          <button id="img-button"><img alt="Settings"></button>
          <a id="img-link" href="/home"><img alt="Home"></a>
          <button id="svg-button"><svg><title>Chart</title><circle cx="1" cy="1" r="1"></circle></svg></button>
          <a id="svg-link" href="/chart"><svg><title>Chart link</title></svg></a>
          <button id="mixed-button">Visible <span hidden>Hidden</span><img alt="Icon"></button>
          <button id="aria-hidden-image"><img alt="Ignored" aria-hidden="true">Fallback</button>
        </main>
        """
    )

    assert page.get_by_role("button", name="Settings").evaluate_all("(els) => els.map(el => el.id)") == [
        "img-button"
    ]
    assert page.get_by_role("button", name="Chart").evaluate_all("(els) => els.map(el => el.id)") == ["svg-button"]
    assert page.get_by_role("button", name="Visible Icon").evaluate_all("(els) => els.map(el => el.id)") == [
        "mixed-button"
    ]
    assert page.get_by_role("button", name="Fallback").evaluate_all("(els) => els.map(el => el.id)") == [
        "aria-hidden-image"
    ]
    assert page.get_by_role("link", name="Home").evaluate_all("(els) => els.map(el => el.id)") == ["img-link"]
    assert page.get_by_role("link", name="Chart link").evaluate_all("(els) => els.map(el => el.id)") == ["svg-link"]
    assert page.aria_snapshot() == (
        '- main "Icon names":\n'
        '  - button "Settings":\n'
        '    - img "Settings"\n'
        '  - link "Home":\n'
        '    - /url: /home\n'
        '    - img "Home"\n'
        '  - button "Chart":\n'
        '    - img "Chart"\n'
        '  - link "Chart link":\n'
        '    - /url: /chart\n'
        '    - img "Chart link"\n'
        '  - button "Visible Icon":\n'
        '    - text: Visible\n'
        '    - img "Icon"\n'
        '  - button "Fallback"'
    )


@case
def aria_labelledby_name_sources_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Labelledby names">
          <span id="raw">Raw Label</span>
          <span id="hidden" hidden>Hidden Label</span>
          <span id="aria" aria-label="ARIA Label">Ignored visible</span>
          <span id="img-ref"><img alt="Image Label"></span>
          <span id="svg-ref"><svg><title>SVG Label</title></svg></span>
          <span id="nested-ref" aria-labelledby="raw"></span>
          <button id="raw-button" aria-labelledby="raw">Raw</button>
          <button id="hidden-button" aria-labelledby="hidden">Hidden</button>
          <button id="aria-button" aria-labelledby="aria">Aria</button>
          <button id="img-button" aria-labelledby="img-ref">Image</button>
          <button id="svg-button" aria-labelledby="svg-ref">SVG</button>
          <button id="nested-button" aria-labelledby="nested-ref">Nested</button>
        </main>
        """
    )

    assert page.get_by_role("button", name="Raw Label").evaluate_all("(els) => els.map(el => el.id)") == [
        "raw-button"
    ]
    assert page.get_by_role("button", name="Hidden Label").evaluate_all("(els) => els.map(el => el.id)") == [
        "hidden-button"
    ]
    assert page.get_by_role("button", name="ARIA Label").evaluate_all("(els) => els.map(el => el.id)") == [
        "aria-button"
    ]
    assert page.get_by_role("button", name="Ignored visible").count() == 0
    assert page.get_by_role("button", name="Image Label").evaluate_all("(els) => els.map(el => el.id)") == [
        "img-button"
    ]
    assert page.get_by_role("button", name="SVG Label").evaluate_all("(els) => els.map(el => el.id)") == [
        "svg-button"
    ]
    assert page.get_by_role("button", name="Nested").evaluate_all("(els) => els.map(el => el.id)") == [
        "nested-button"
    ]
    assert page.aria_snapshot() == (
        '- main "Labelledby names":\n'
        "  - text: Raw Label Ignored visible\n"
        '  - img "Image Label"\n'
        '  - img "SVG Label"\n'
        '  - button "Raw Label": Raw\n'
        '  - button "Hidden Label": Hidden\n'
        '  - button "ARIA Label": Aria\n'
        '  - button "Image Label": Image\n'
        '  - button "SVG Label": SVG\n'
        '  - button "Nested"'
    )


@case
def accessible_description_name_sources_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main>
          <span id="raw">Raw description</span>
          <span id="hidden" hidden>Hidden description</span>
          <span id="aria" aria-label="ARIA description">Ignored visible</span>
          <span id="img-ref"><img alt="Image description"></span>
          <span id="svg-ref"><svg><title>SVG description</title></svg></span>
          <button id="raw-button" aria-describedby="raw">Raw</button>
          <button id="hidden-button" aria-describedby="hidden">Hidden</button>
          <button id="aria-button" aria-describedby="aria">Aria</button>
          <button id="img-button" aria-describedby="img-ref">Image</button>
          <button id="svg-button" aria-describedby="svg-ref">SVG</button>
        </main>
        """
    )

    expect(page.locator("#raw-button")).to_have_accessible_description("Raw description")
    expect(page.locator("#hidden-button")).to_have_accessible_description("Hidden description")
    expect(page.locator("#aria-button")).to_have_accessible_description("ARIA description")
    expect(page.locator("#aria-button")).not_to_have_accessible_description("Ignored visible")
    expect(page.locator("#img-button")).to_have_accessible_description("Image description")
    expect(page.locator("#svg-button")).to_have_accessible_description("SVG description")


@case
def native_label_image_names_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Native label names">
          <label id="img-label"><img alt="Image Label"><input id="img-input"></label>
          <label id="svg-label"><svg><title>SVG Label</title></svg><input id="svg-input"></label>
          <label id="mixed-label">Visible <img alt="Icon"><input id="mixed-input"></label>
          <label id="hidden-img-label"><img alt="Ignored" aria-hidden="true">Fallback<input id="hidden-img-input"></label>
        </main>
        """
    )

    assert page.get_by_role("textbox", name="Image Label").evaluate_all("(els) => els.map(el => el.id)") == [
        "img-input"
    ]
    assert page.get_by_role("textbox", name="SVG Label").evaluate_all("(els) => els.map(el => el.id)") == [
        "svg-input"
    ]
    assert page.get_by_role("textbox", name="Visible Icon").evaluate_all("(els) => els.map(el => el.id)") == [
        "mixed-input"
    ]
    assert page.get_by_role("textbox", name="Fallback").evaluate_all("(els) => els.map(el => el.id)") == [
        "hidden-img-input"
    ]
    assert page.get_by_label("Image Label").count() == 0
    assert page.get_by_label("SVG Label").evaluate_all("(els) => els.map(el => el.id)") == ["svg-input"]
    assert page.get_by_label("Visible Icon").count() == 0
    assert page.get_by_label("Fallback").evaluate_all("(els) => els.map(el => el.id)") == ["hidden-img-input"]
    expect(page.locator("#img-input")).to_have_accessible_name("Image Label")
    expect(page.locator("#svg-input")).to_have_accessible_name("SVG Label")
    expect(page.locator("#mixed-input")).to_have_accessible_name("Visible Icon")
    expect(page.locator("#hidden-img-input")).to_have_accessible_name("Fallback")
    assert page.aria_snapshot() == (
        '- main "Native label names":\n'
        '  - img "Image Label"\n'
        '  - textbox "Image Label"\n'
        '  - img "SVG Label"\n'
        '  - textbox "SVG Label"\n'
        "  - text: Visible\n"
        '  - img "Icon"\n'
        '  - textbox "Visible Icon"\n'
        "  - text: Fallback\n"
        '  - textbox "Fallback"'
    )


@case
def native_label_role_image_names_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <main aria-label="Role image label">
          <label><span role="img" aria-label="Role image label"></span><input id="role-img-label-input"></label>
        </main>
        """
    )

    assert page.get_by_role("textbox", name="Role image label").evaluate_all("(els) => els.map(el => el.id)") == [
        "role-img-label-input"
    ]
    expect(page.locator("#role-img-label-input")).to_have_accessible_name("Role image label")
    assert page.aria_snapshot() == (
        '- main "Role image label":\n'
        '  - img "Role image label"\n'
        '  - textbox "Role image label"'
    )


@case
def aria_owns_snapshot_structure_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Owns probe">
          <div id="list" role="list" aria-owns="owned-one owned-two">
            <div id="dom-item" role="listitem">DOM item</div>
          </div>
          <div id="owned-one" role="listitem">Owned one</div>
          <div id="owned-two" role="listitem">Owned two</div>
          <div id="tree" role="tree" aria-owns="owned-treeitem"></div>
          <div id="owned-treeitem" role="treeitem">Owned tree item</div>
        </main>
        """
    )

    assert page.aria_snapshot() == (
        '- main "Owns probe":\n'
        "  - list:\n"
        "    - listitem: DOM item\n"
        "    - listitem: Owned one\n"
        "    - listitem: Owned two\n"
        "  - tree:\n"
        '    - treeitem "Owned tree item"'
    )
    assert page.locator("#list").aria_snapshot() == (
        "- list:\n"
        "  - listitem: DOM item\n"
        "  - listitem: Owned one\n"
        "  - listitem: Owned two"
    )
    assert page.locator("#tree").aria_snapshot() == '- tree:\n  - treeitem "Owned tree item"'


@case
def aria_snapshot_depth_mode_and_fragment_url_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    page.set_content(
        """
        <main aria-label="Depth options">
          <section aria-label="Nested region">
            <a id="hash-link" href="#">Hash Link</a>
          </section>
        </main>
        """
    )
    full_snapshot = (
        '- main "Depth options":\n'
        '  - region "Nested region":\n'
        '    - link "Hash Link":\n'
        '      - /url: "#"'
    )

    assert page.aria_snapshot(depth=0) == full_snapshot
    assert page.locator("main").aria_snapshot(depth=0) == full_snapshot
    assert page.locator("main").aria_snapshot(depth=1) == '- main "Depth options":\n  - region "Nested region"'
    assert page.locator("main").aria_snapshot(depth=-1) == ""
    assert page.locator("#hash-link").aria_snapshot() == '- link "Hash Link":\n  - /url: "#"'

    invalid_calls = [
        (
            lambda: page.aria_snapshot(depth="1"),
            "Page.aria_snapshot: depth: expected integer, got string",
        ),
        (
            lambda: page.aria_snapshot(depth=1.2),
            "Page.aria_snapshot: depth: expected integer, got float 1.2",
        ),
        (
            lambda: page.aria_snapshot(depth=True),
            "Page.aria_snapshot: depth: expected integer, got boolean",
        ),
        (
            lambda: page.locator("main").aria_snapshot(depth="1"),
            "Locator.aria_snapshot: depth: expected integer, got string",
        ),
        (
            lambda: page.aria_snapshot(mode="compact"),
            "Page.aria_snapshot: mode: expected one of (ai|default)",
        ),
        (
            lambda: page.locator("main").aria_snapshot(mode="compact"),
            "Locator.aria_snapshot: mode: expected one of (ai|default)",
        ),
    ]
    for call, expected_message in invalid_calls:
        try:
            call()
        except sync_api.Error as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message}")


@case
def empty_href_url_snapshot_match_playwright(page):
    page.set_content(
        """
        <main aria-label="URL probe">
          <a id="empty" href="">Empty</a>
          <a id="hash" href="#x">Hash</a>
          <a id="relative" href="rel/path">Relative</a>
          <a id="data" href="data:text/plain,a:b">Data</a>
          <map name="empty-map">
            <area id="empty-area" shape="rect" coords="0,0,10,10" href="" alt="Empty area">
          </map>
        </main>
        """
    )

    assert page.aria_snapshot() == (
        '- main "URL probe":\n'
        '  - link "Empty":\n'
        '    - /url: ""\n'
        '  - link "Hash":\n'
        '    - /url: "#x"\n'
        '  - link "Relative":\n'
        '    - /url: rel/path\n'
        '  - link "Data":\n'
        '    - /url: data:text/plain,a:b\n'
        '  - link "Empty area":\n'
        '    - /url: ""'
    )
    assert page.locator("#empty").aria_snapshot() == '- link "Empty":\n  - /url: ""'
    assert page.locator("#hash").aria_snapshot() == '- link "Hash":\n  - /url: "#x"'
    assert page.locator("#relative").aria_snapshot() == '- link "Relative":\n  - /url: rel/path'
    assert page.locator("#data").aria_snapshot() == '- link "Data":\n  - /url: data:text/plain,a:b'
    assert page.locator("#empty-area").aria_snapshot() == '- link "Empty area":\n  - /url: ""'


@case
def aria_snapshot_yaml_scalar_quoting_match_playwright(page):
    page.set_content(
        """
        <main aria-label="Scalar probe">
          <p id="colon">Name: value</p>
          <p id="dash">- leading dash</p>
          <p id="bracket">[checked]</p>
          <p id="quote">He said "hi"</p>
          <p id="hash">#hash text</p>
          <button id="button-colon">Name: value</button>
          <a id="url-colon" href="data:text/plain,a:b">Url Colon</a>
        </main>
        """
    )

    assert page.aria_snapshot() == (
        '- main "Scalar probe":\n'
        '  - paragraph: "Name: value"\n'
        '  - paragraph: "- leading dash"\n'
        '  - paragraph: "[checked]"\n'
        '  - paragraph: He said "hi"\n'
        '  - paragraph: "#hash text"\n'
        '  - \'button "Name: value"\'\n'
        '  - link "Url Colon":\n'
        "    - /url: data:text/plain,a:b"
    )
    assert page.locator("#colon").aria_snapshot() == '- paragraph: "Name: value"'
    assert page.locator("#dash").aria_snapshot() == '- paragraph: "- leading dash"'
    assert page.locator("#bracket").aria_snapshot() == '- paragraph: "[checked]"'
    assert page.locator("#hash").aria_snapshot() == '- paragraph: "#hash text"'
    assert page.locator("#button-colon").aria_snapshot() == '- \'button "Name: value"\''


@case
def wait_for_request_and_response(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        with page.expect_event("request", lambda item: item.url.endswith("/headers"), timeout=3_000) as request_info:
            page.evaluate(
                """() => fetch('/headers', {
                  headers: { 'X-Client-Mixed': 'ReqValue' }
                })"""
            )
        request = request_info.value
        with page.expect_event("response", lambda item: item.url.endswith("/headers"), timeout=3_000) as response_info:
            page.evaluate("() => fetch('/headers')")
        response = response_info.value

    assert request.method == "GET"
    assert request.headers["x-client-mixed"] == "ReqValue"
    assert response.status == 200
    assert response.headers["content-type"] == "application/json"


@case
def expect_request_finished_helper_returns_completed_request(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        with page.expect_request_finished(lambda request: request.url.endswith("/headers")) as request_info:
            page.evaluate("() => fetch('/headers')")

    request = request_info.value
    assert request.url.endswith("/headers")
    assert request.method == "GET"
    assert request.failure is None
    assert request.service_worker is None
    assert request.sizes()["responseBodySize"] >= 0


@case
def response_event_resource_type_and_body_match_skyvern_xhr_download_capture(page):
    seen: list[tuple[str, int, str | None, bytes]] = []

    def on_response(response):
        if response.url.endswith("/xhr-download"):
            seen.append(
                (
                    response.request.resource_type,
                    response.status,
                    response.headers.get("content-disposition"),
                    response.body(),
                )
            )

    page.route(
        "**/page",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="""<button id="go" onclick="fetch('/xhr-download')
                .then(response => response.text())
                .then(text => document.body.dataset.done = text)">Go</button>""",
        ),
    )
    page.route(
        "**/xhr-download",
        lambda route: route.fulfill(
            status=200,
            headers={
                "content-type": "text/plain",
                "content-disposition": 'attachment; filename="skyvern.txt"',
            },
            body="skyvern body",
        ),
    )
    page.on("response", on_response)
    try:
        page.goto("http://example.test/page")
        page.click("#go")
        page.wait_for_function("() => document.body.dataset.done === 'skyvern body'", timeout=3_000)

        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            page.wait_for_timeout(20)
    finally:
        page.remove_listener("response", on_response)
        page.unroute("**/xhr-download")
        page.unroute("**/page")

    assert seen == [("fetch", 200, 'attachment; filename="skyvern.txt"', b"skyvern body")]


@case
def context_expect_console_event(page):
    with page.context.expect_event("console", lambda message: message.text == "context-log") as message_info:
        page.evaluate("() => console.log('context-log')")
    assert message_info.value.text == "context-log"


@case
def context_console_message_location_items_match_skyvern_browser_log(page):
    seen: list[tuple[bool, str, str, dict[str, object], list[tuple[str, object]]]] = []

    def browser_console_log(message):
        location = dict(message.location)
        seen.append((message.page is page, message.type, message.text, location, list(message.location.items())))

    page.context.on("console", browser_console_log)
    try:
        with page.context.expect_event("console", lambda message: message.text == "skyvern context location 42") as message_info:
            page.goto(data_url("<script>console.warn('skyvern context location', 42)</script><main>location</main>"))

        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            page.wait_for_timeout(20)
    finally:
        page.context.remove_listener("console", browser_console_log)

    message = message_info.value
    assert message.page is page
    assert message.type == "warning"
    assert message.text == "skyvern context location 42"
    assert {"url", "lineNumber", "columnNumber"}.issubset(message.location)
    assert list(message.location.items())
    assert seen
    assert seen[0][0:3] == (True, "warning", "skyvern context location 42")
    assert {"url", "lineNumber", "columnNumber"}.issubset(seen[0][3])
    assert seen[0][4] == list(seen[0][3].items())


@case
def once_listener_can_be_removed_by_original_callback(page):
    page_seen = []

    def wait_for_seen(items):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if items:
                return list(items)
            page.wait_for_timeout(20)
        return list(items)

    def page_console(message):
        page_seen.append(message.text)

    page.once("console", page_console)
    page.remove_listener("console", page_console)
    page.evaluate("() => console.log('removed-page-once')")
    page.wait_for_timeout(100)
    assert page_seen == []

    page.once("console", page_console)
    page.evaluate("() => console.log('kept-page-once')")
    assert wait_for_seen(page_seen) == ["kept-page-once"]
    page.evaluate("() => console.log('ignored-page-once')")
    page.wait_for_timeout(100)
    assert page_seen == ["kept-page-once"]

    context_seen = []

    def context_console(message):
        context_seen.append(message.text)

    page.context.once("console", context_console)
    page.context.remove_listener("console", context_console)
    page.evaluate("() => console.log('removed-context-once')")
    page.wait_for_timeout(100)
    assert context_seen == []

    page.context.once("console", context_console)
    page.evaluate("() => console.log('kept-context-once')")
    assert wait_for_seen(context_seen) == ["kept-context-once"]
    page.evaluate("() => console.log('ignored-context-once')")
    page.wait_for_timeout(100)
    assert context_seen == ["kept-context-once"]

    duplicate_page_seen = []

    def duplicate_page_console(message):
        duplicate_page_seen.append(message.text)

    page.on("console", duplicate_page_console)
    page.on("console", duplicate_page_console)
    page.evaluate("() => console.log('deduped-page-listener')")
    assert wait_for_seen(duplicate_page_seen) == ["deduped-page-listener"]

    duplicate_context_seen = []

    def duplicate_context_console(message):
        duplicate_context_seen.append(message.text)

    page.context.on("console", duplicate_context_console)
    page.context.on("console", duplicate_context_console)
    page.evaluate("() => console.log('deduped-context-listener')")
    assert wait_for_seen(duplicate_context_seen) == ["deduped-context-listener"]


@case
def context_network_event_context_managers(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        with page.context.expect_event("request", lambda item: item.url.endswith("/headers")) as request_info:
            with page.context.expect_event("response", lambda item: item.url.endswith("/headers")) as response_info:
                with page.context.expect_event("requestfinished", lambda item: item.url.endswith("/headers")) as finished_info:
                    page.evaluate(
                        """() => fetch('/headers', {
                          headers: { 'X-Context-Event': 'seen' }
                        })"""
                    )

    assert request_info.value.method == "GET"
    assert request_info.value.headers["x-context-event"] == "seen"
    assert response_info.value.status == 200
    assert response_info.value.headers["content-type"] == "application/json"
    assert finished_info.value.url.endswith("/headers")
    assert finished_info.value.failure is None


@case
def context_wait_for_console_and_network_events(page):
    context = page.context
    with header_case_server() as base_url:
        page.goto(base_url)

        page.evaluate("() => setTimeout(() => console.error('context wait console'), 20)")
        message = context.wait_for_event(
            "console",
            lambda item: item.text == "context wait console",
            timeout=3_000,
        )
        assert message.page is page
        assert (message.type, message.text) == ("error", "context wait console")

        page.evaluate(
            """() => setTimeout(() => fetch('/query?event=request', {
              headers: { 'X-Wait-Event': 'request' }
            }), 20)"""
        )
        request = context.wait_for_event(
            "request",
            lambda item: item.url.endswith("/query?event=request"),
            timeout=3_000,
        )
        assert request.method == "GET"
        assert request.headers["x-wait-event"] == "request"

        page.evaluate("() => setTimeout(() => fetch('/query?event=response'), 20)")
        response = context.wait_for_event(
            "response",
            lambda item: item.url.endswith("/query?event=response"),
            timeout=3_000,
        )
        assert response.status == 200
        assert response.headers["content-type"] == "application/json"

        page.evaluate("() => setTimeout(() => fetch('/query?event=finished'), 20)")
        finished = context.wait_for_event(
            "requestfinished",
            lambda item: item.url.endswith("/query?event=finished"),
            timeout=3_000,
        )
        assert finished.method == "GET"
        assert finished.failure is None

        page.route("**/context-wait-abort", lambda route: route.abort())
        page.evaluate(
            "() => setTimeout(() => fetch('http://example.test/context-wait-abort').catch(() => {}), 20)"
        )
        failed = context.wait_for_event(
            "requestfailed",
            lambda item: item.url.endswith("/context-wait-abort"),
            timeout=3_000,
        )
        assert failed.url == "http://example.test/context-wait-abort"
        assert failed.failure


@case
def page_add_init_script_before_navigation(page):
    page.add_init_script("window.__parityInitValue = 42")
    page.goto(data_url("<main>init script</main>"))
    assert page.evaluate("() => window.__parityInitValue") == 42


@case
def add_init_script_argument_validation_matches_playwright(page):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("add_init_script unexpectedly accepted invalid script")

    expected = "Either path or script parameter must be specified"
    expect_error(lambda: page.add_init_script(), expected)
    expect_error(lambda: page.add_init_script(123), expected)
    expect_error(lambda: page.add_init_script(True), expected)

    context = browser.new_context()
    try:
        expect_error(lambda: context.add_init_script(), expected)
        expect_error(lambda: context.add_init_script(123), expected)
        expect_error(lambda: context.add_init_script(True), expected)
    finally:
        context.close()


@case
def page_expose_function_roundtrip(page):
    page.expose_function("addFromPython", lambda left, right: left + right)
    page.goto(data_url("<main>binding</main>"))
    assert page.evaluate("async () => await window.addFromPython(19, 23)") == 42


@case
def page_expose_binding_source_metadata_survives_navigation(page):
    seen = []

    def describe_source(source, value):
        seen.append(
            {
                "page_matches": source["page"] is page,
                "context_matches": source["context"] is page.context,
                "frame_matches": source["frame"] == page.main_frame,
                "value": value,
            }
        )
        return f"seen:{value}"

    page.expose_binding("describeSource", describe_source)
    page.goto(data_url("<main>First</main>"))
    assert page.evaluate("async () => await window.describeSource('first')") == "seen:first"
    page.goto(data_url("<main>Second</main>"))
    assert page.evaluate("async () => await window.describeSource('second')") == "seen:second"
    assert seen == [
        {"page_matches": True, "context_matches": True, "frame_matches": True, "value": "first"},
        {"page_matches": True, "context_matches": True, "frame_matches": True, "value": "second"},
    ]


@case
def page_expose_binding_source_metadata_reports_child_frame(page):
    seen = []

    def describe_source(source, value):
        seen.append(
            {
                "page_matches": source["page"] is page,
                "context_matches": source["context"] is page.context,
                "frame_is_main": source["frame"] == page.main_frame,
                "frame_name": source["frame"].name,
                "value": value,
            }
        )
        return f"frame:{source['frame'].name}:{value}"

    page.expose_binding("describeFrameSource", describe_source)
    with header_case_server() as base_url:
        page.goto(f"{base_url}/frame-page")
        child = page.frame(name="child")
        assert child is not None
        assert child.evaluate("() => typeof window.describeFrameSource") == "function"
        page.evaluate(
            """
            () => {
              const childWindow = document.querySelector('iframe').contentWindow;
              childWindow.describeFrameSource('ok').then(value => {
                window.__childFrameBindingResult = value;
              }).catch(error => {
                window.__childFrameBindingError = String(error);
              });
            }
            """
        )
        handle = page.wait_for_function(
            "() => window.__childFrameBindingResult || window.__childFrameBindingError",
            timeout=3_000,
        )
        handle.dispose()
        assert page.evaluate("() => window.__childFrameBindingError || null") is None
        assert page.evaluate("() => window.__childFrameBindingResult") == "frame:child:ok"
    assert seen == [
        {
            "page_matches": True,
            "context_matches": True,
            "frame_is_main": False,
            "frame_name": "child",
            "value": "ok",
        }
    ]


@case
def page_expose_binding_is_available_to_init_script_navigation(page):
    seen = []

    def record_event(source, payload):
        seen.append(
            {
                "page_matches": source["page"] is page,
                "context_matches": source["context"] is page.context,
                "frame_matches": source["frame"] == page.main_frame,
                "payload": payload,
            }
        )
        return f"ack:{payload['phase']}"

    page.expose_binding("recordSkyvernEvent", record_event)
    page.add_init_script(
        """
        window.__skyvernBindingReadyAtInit = typeof window.recordSkyvernEvent === 'function';
        window.__skyvernEmitEvent = payload => window.recordSkyvernEvent(payload);
        """
    )
    page.goto(
        data_url(
            """
            <main>binding init</main>
            <script>
            window.__skyvernBindingResults = [];
            window.__skyvernEmitEvent({
              phase: 'inline',
              ready: window.__skyvernBindingReadyAtInit,
            }).then(value => {
              window.__skyvernBindingResults.push(value);
              document.body.dataset.bindingDone = 'yes';
            }).catch(error => {
              document.body.dataset.bindingError = String(error);
            });
            </script>
            """
        )
    )

    handle = page.wait_for_function(
        "() => document.body.dataset.bindingDone === 'yes' || document.body.dataset.bindingError",
        timeout=3_000,
    )
    handle.dispose()
    assert page.evaluate("document.body.dataset.bindingError || null") is None
    assert page.evaluate("window.__skyvernBindingResults") == ["ack:inline"]
    assert seen == [
        {
            "page_matches": True,
            "context_matches": True,
            "frame_matches": True,
            "payload": {"phase": "inline", "ready": True},
        }
    ]


@case
def expose_binding_handle_option(page):
    page.set_content("<main>binding handle</main>")
    seen = []

    def inspect_handle(source, handle):
        seen.append((source["page"] is page, source["frame"] == page.main_frame, handle))
        return "page-ok"

    page.expose_binding("inspectHandle", inspect_handle, handle=True)
    assert page.evaluate("async () => await window.inspectHandle({ value: 41, nested: { ok: true } })") == "page-ok"
    assert len(seen) == 1
    source_page_matches, source_frame_matches, handle = seen[0]
    assert source_page_matches is True
    assert source_frame_matches is True
    try:
        assert handle.json_value() == {"value": 41, "nested": {"ok": True}}
        assert handle.get_property("value").json_value() == 41
    finally:
        handle.dispose()

    context = page.context.browser.new_context()
    context_seen = []
    try:
        def inspect_context_handle(source, handle):
            context_seen.append((source["page"], source["frame"], handle))
            return "context-ok"

        context.expose_binding("inspectContextHandle", inspect_context_handle, handle=True)
        target = context.new_page()
        target.set_content("<main>context binding handle</main>")
        assert target.evaluate("async () => await window.inspectContextHandle({ label: 'ctx' })") == "context-ok"
        assert len(context_seen) == 1
        source_page, source_frame, context_handle = context_seen[0]
        assert source_page is target
        assert source_frame == target.main_frame
        try:
            assert context_handle.json_value() == {"label": "ctx"}
        finally:
            context_handle.dispose()
    finally:
        context.close()


@case
def expose_binding_handle_option_reports_child_frame(page):
    seen = []

    def inspect_handle(source, handle):
        seen.append(
            {
                "page_matches": source["page"] is page,
                "frame_is_main": source["frame"] == page.main_frame,
                "frame_name": source["frame"].name,
                "handle": handle,
            }
        )
        return f"handle:{source['frame'].name}"

    page.expose_binding("inspectChildHandle", inspect_handle, handle=True)
    with header_case_server() as base_url:
        page.goto(f"{base_url}/frame-page")
        child = page.frame(name="child")
        assert child is not None
        assert child.evaluate("() => typeof window.inspectChildHandle") == "function"
        page.evaluate(
            """
            () => {
              const childWindow = document.querySelector('iframe').contentWindow;
              childWindow.inspectChildHandle({ value: 17, nested: { ok: true } }).then(value => {
                window.__childFrameHandleBindingResult = value;
              }).catch(error => {
                window.__childFrameHandleBindingError = String(error);
              });
            }
            """
        )
        handle = page.wait_for_function(
            "() => window.__childFrameHandleBindingResult || window.__childFrameHandleBindingError",
            timeout=3_000,
        )
        handle.dispose()
        assert page.evaluate("() => window.__childFrameHandleBindingError || null") is None
        assert page.evaluate("() => window.__childFrameHandleBindingResult") == "handle:child"
    assert len(seen) == 1
    entry = seen[0]
    assert entry["page_matches"] is True
    assert entry["frame_is_main"] is False
    assert entry["frame_name"] == "child"
    child_handle = entry["handle"]
    try:
        assert child_handle.json_value() == {"value": 17, "nested": {"ok": True}}
        assert child_handle.get_property("value").json_value() == 17
    finally:
        child_handle.dispose()


@case
def context_expose_binding_child_frame_source_and_handle(page):
    browser = page.context.browser
    context = browser.new_context()
    try:
        seen = []

        def first_binding(source, value):
            seen.append(("first", source["frame"].name, value))
            return f"first:{source['frame'].name}:{value}"

        def second_binding(source, value):
            seen.append(("second", source["frame"].name, value))
            return f"second:{source['frame'].name}:{value}"

        def inspect_handle(source, handle):
            seen.append(("handle", source["frame"].name, handle))
            return f"handle:{source['frame'].name}"

        context.expose_binding("contextFirstChildBinding", first_binding)
        context.expose_binding("contextSecondChildBinding", second_binding)
        context.expose_binding("contextChildHandleBinding", inspect_handle, handle=True)

        target = context.new_page()
        with header_case_server() as base_url:
            target.goto(f"{base_url}/frame-page")
            child = target.frame(name="child")
            assert child is not None
            assert child.evaluate("() => typeof window.contextFirstChildBinding") == "function"
            assert child.evaluate("() => typeof window.contextSecondChildBinding") == "function"
            assert child.evaluate("() => typeof window.contextChildHandleBinding") == "function"
            target.evaluate(
                """
                () => {
                  const childWindow = document.querySelector('iframe').contentWindow;
                  Promise.all([
                    childWindow.contextFirstChildBinding('alpha'),
                    childWindow.contextSecondChildBinding('beta'),
                    childWindow.contextChildHandleBinding({ value: 29, nested: { ok: true } }),
                  ]).then(values => {
                    window.__contextChildBindingResults = values;
                  }).catch(error => {
                    window.__contextChildBindingError = String(error);
                  });
                }
                """
            )
            waiter = target.wait_for_function(
                "() => window.__contextChildBindingResults || window.__contextChildBindingError",
                timeout=5_000,
            )
            waiter.dispose()
            assert target.evaluate("() => window.__contextChildBindingError || null") is None
            assert target.evaluate("() => window.__contextChildBindingResults") == [
                "first:child:alpha",
                "second:child:beta",
                "handle:child",
            ]

        assert len(seen) == 3
        assert seen[0] == ("first", "child", "alpha")
        assert seen[1] == ("second", "child", "beta")
        assert seen[2][0] == "handle"
        assert seen[2][1] == "child"
        handle = seen[2][2]
        try:
            assert handle.json_value() == {"value": 29, "nested": {"ok": True}}
            assert handle.get_property("value").json_value() == 29
        finally:
            handle.dispose()
    finally:
        context.close()


@case
def expose_binding_option_validation_matches_playwright(page):
    browser = page.context.browser

    def expect_error(operation, expected):
        context = browser.new_context()
        target = context.new_page()
        try:
            try:
                operation(target, context)
            except Exception as exc:
                assert str(exc).splitlines()[0] == expected
                return
            raise AssertionError("expose binding/function option unexpectedly succeeded")
        finally:
            context.close()

    expect_error(
        lambda target, context: target.expose_binding("badHandle", lambda source: None, handle="bad"),
        "Page.expose_binding: needs_handle: expected boolean, got string",
    )
    expect_error(
        lambda target, context: target.expose_binding("badHandleNumber", lambda source: None, handle=1),
        "Page.expose_binding: needs_handle: expected boolean, got number",
    )
    expect_error(
        lambda target, context: target.expose_binding(123, lambda source: None),
        "Page.expose_binding: name: expected string, got number",
    )
    expect_error(
        lambda target, context: target.expose_function(True, lambda: None),
        "Page.expose_function: name: expected string, got boolean",
    )
    expect_error(
        lambda target, context: context.expose_binding("badHandle", lambda source: None, handle="bad"),
        "BrowserContext.expose_binding: needs_handle: expected boolean, got string",
    )
    expect_error(
        lambda target, context: context.expose_binding("badHandleNumber", lambda source: None, handle=1),
        "BrowserContext.expose_binding: needs_handle: expected boolean, got number",
    )
    expect_error(
        lambda target, context: context.expose_binding(123, lambda source: None),
        "BrowserContext.expose_binding: name: expected string, got number",
    )
    expect_error(
        lambda target, context: context.expose_function(123, lambda: None),
        "BrowserContext.expose_function: name: expected string, got number",
    )
    duplicate_cases = [
        (
            lambda target, context: (
                target.expose_function("dupPageFunction", lambda: None),
                target.expose_function("dupPageFunction", lambda: None),
            ),
            'Function "dupPageFunction" has been already registered',
        ),
        (
            lambda target, context: (
                target.expose_binding("dupPageBinding", lambda source: None),
                target.expose_binding("dupPageBinding", lambda source: None),
            ),
            'Function "dupPageBinding" has been already registered',
        ),
        (
            lambda target, context: (
                target.expose_function("dupPageMixed", lambda: None),
                target.expose_binding("dupPageMixed", lambda source: None),
            ),
            'Function "dupPageMixed" has been already registered',
        ),
        (
            lambda target, context: (
                context.expose_function("dupContextFunction", lambda: None),
                context.expose_function("dupContextFunction", lambda: None),
            ),
            'Function "dupContextFunction" has been already registered',
        ),
        (
            lambda target, context: (
                context.expose_binding("dupContextBinding", lambda source: None),
                context.expose_binding("dupContextBinding", lambda source: None),
            ),
            'Function "dupContextBinding" has been already registered',
        ),
        (
            lambda target, context: (
                context.expose_function("dupContextMixed", lambda: None),
                context.expose_binding("dupContextMixed", lambda source: None),
            ),
            'Function "dupContextMixed" has been already registered',
        ),
        (
            lambda target, context: (
                context.expose_function("contextThenPage", lambda: None),
                target.expose_function("contextThenPage", lambda: None),
            ),
            'Function "contextThenPage" has been already registered in the browser context',
        ),
        (
            lambda target, context: (
                target.expose_function("pageThenContext", lambda: None),
                context.expose_function("pageThenContext", lambda: None),
            ),
            'Function "pageThenContext" has been already registered in one of the pages',
        ),
    ]
    for operation, message in duplicate_cases:
        expect_error(operation, message)


@case
def context_expect_page_captures_popup(page):
    page.set_content(
        """
        <button id="open" onclick="
          const popup = window.open();
          popup.document.write('<title>Shared Popup</title><main>popup</main>');
          popup.document.close();
        ">Open</button>
        """
    )

    with page.context.expect_page(lambda popup: popup.title() == "Shared Popup") as popup_info:
        page.click("#open")

    popup = popup_info.value
    try:
        assert popup.title() == "Shared Popup"
        assert popup.opener() is page
    finally:
        popup.close()


@case
def evaluate_window_open_returns_without_popup_waiter(page):
    result = page.evaluate("() => { const popup = window.open('about:blank'); return popup === null; }")
    assert result is False

    page.wait_for_timeout(100)
    for popup in [candidate for candidate in page.context.pages if candidate is not page]:
        popup.close()


@case
def page_expect_popup_captures_window_open(page):
    page.set_content(
        """
        <button id="open" onclick="
          const popup = window.open();
          popup.document.write('<title>Page Popup</title><main>page popup</main>');
          popup.document.close();
        ">Open</button>
        """
    )

    with page.expect_popup(lambda popup: popup.title() == "Page Popup") as popup_info:
        page.click("#open")

    popup = popup_info.value
    try:
        assert popup.context is page.context
        assert popup in page.context.pages
        assert popup.opener() is page
        assert popup.text_content("main") == "page popup"
    finally:
        popup.close()


@case
def page_generic_popup_event_helpers(page):
    page.set_content(
        """
        <button id="expect-open" onclick="
          const popup = window.open();
          popup.document.write('<title>Generic Popup</title><main>generic popup</main>');
          popup.document.close();
        ">Open expect</button>
        <button id="wait-open" onclick="
          const popup = window.open();
          popup.document.write('<title>Waited Popup</title><main>waited popup</main>');
          popup.document.close();
        ">Open wait</button>
        """
    )

    with page.expect_event("popup", lambda popup: popup.title() == "Generic Popup") as popup_info:
        page.click("#expect-open")

    popup = popup_info.value
    try:
        assert popup.context is page.context
        assert popup.opener() is page
        assert popup.text_content("main") == "generic popup"
    finally:
        popup.close()

    page.evaluate("() => setTimeout(() => document.querySelector('#wait-open').click(), 20)")
    waited_popup = page.wait_for_event("popup", lambda candidate: candidate.title() == "Waited Popup", timeout=3_000)
    try:
        assert waited_popup.context is page.context
        assert waited_popup.opener() is page
        assert waited_popup.text_content("main") == "waited popup"
    finally:
        waited_popup.close()


@case
def context_generic_page_event_helpers(page):
    context = page.context.browser.new_context()
    owner = context.new_page()
    try:
        with context.expect_event("page", lambda candidate: candidate.url == "about:blank") as page_info:
            created = context.new_page()
        assert page_info.value is created
        created.close()

        owner.set_content(
            """
            <button id="open" onclick="
              const popup = window.open();
              popup.document.write('<title>Generic Context Page</title><main>context generic</main>');
              popup.document.close();
            ">Open</button>
            """
        )
        owner.evaluate("() => setTimeout(() => document.querySelector('#open').click(), 20)")
        popup = context.wait_for_event(
            "page",
            lambda candidate: candidate.opener() is owner and candidate.title() == "Generic Context Page",
            timeout=3_000,
        )
        try:
            assert popup.context is context
            assert popup in context.pages
            assert popup.opener() is owner
            assert popup.text_content("main") == "context generic"
        finally:
            popup.close()
    finally:
        context.close()


@case
def context_setup_applies_to_popup_pages(page):
    context = page.context.browser.new_context()
    context.add_init_script("window.__popupContextInit = 42")
    context.expose_function("fromContextPopup", lambda value: value + 1)
    owner = context.new_page()
    try:
        owner.set_content("<button id='open' onclick=\"window.open('about:blank')\">Open</button>")

        with context.expect_page() as popup_info:
            owner.click("#open")

        popup = popup_info.value
        try:
            assert popup.evaluate("window.__popupContextInit") == 42
            assert popup.evaluate("async () => await window.fromContextPopup(8)") == 9
            popup.goto(data_url("<title>Popup Setup</title><main>popup</main>"))
            assert popup.evaluate("window.__popupContextInit") == 42
            assert popup.evaluate("async () => await window.fromContextPopup(8)") == 9
            assert popup.opener() is owner
        finally:
            popup.close()
    finally:
        context.close()


@case
def page_expect_dialog_accepts_prompt(page):
    page.set_content(
        """
        <script>
        window.showPrompt = () => setTimeout(() => {
          document.body.dataset.answer = prompt('Name?', 'Ada');
        }, 0);
        </script>
        """
    )

    with page.expect_event("dialog") as dialog_info:
        page.evaluate("() => window.showPrompt()")

    dialog = dialog_info.value
    assert (dialog.type, dialog.message, dialog.default_value) == ("prompt", "Name?", "Ada")
    dialog.accept("Grace")
    handle = page.wait_for_function("() => document.body.dataset.answer === 'Grace'", timeout=3_000)
    try:
        assert handle.json_value() is True
    finally:
        handle.dispose()


@case
def dialog_listener_must_handle_dialog_like_playwright(page):
    dialogs = []
    page.on("dialog", lambda dialog: dialogs.append(dialog))
    page.set_content(
        """
        <button id="alert" onclick="alert('Listener must handle me'); document.body.dataset.done = 'yes'">
          Alert
        </button>
        """
    )

    try:
        page.click("#alert", timeout=300)
    except Exception as exc:
        assert "Timeout" in type(exc).__name__ or "Timeout" in str(exc).splitlines()[0]
    else:
        raise AssertionError("dialog-triggering click unexpectedly completed before dialog was handled")

    assert len(dialogs) == 1
    assert (dialogs[0].type, dialogs[0].message) == ("alert", "Listener must handle me")
    dialogs[0].dismiss()
    handle = page.wait_for_function("() => document.body.dataset.done === 'yes'", timeout=3_000)
    try:
        assert handle.json_value() is True
    finally:
        handle.dispose()


@case
def dialog_accept_dismiss_validation_matches_playwright(page):
    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("dialog operation unexpectedly succeeded")

    def capture_prompt(key):
        with page.expect_event("dialog") as dialog_info:
            page.evaluate("key => window.showPrompt(key)", key)
        return dialog_info.value

    page.set_content(
        """
        <script>
        window.showPrompt = key => setTimeout(() => {
          document.body.dataset[key] = prompt('Question?', 'default') ?? 'null';
        }, 0);
        </script>
        """
    )

    first = capture_prompt("first")
    expect_error(
        lambda: first.accept(prompt_text=123),
        "Dialog.accept: prompt_text: expected string, got number",
    )
    first.dismiss()
    first_done = page.wait_for_function("() => document.body.dataset.first === 'null'", timeout=3_000)
    try:
        assert first_done.json_value() is True
    finally:
        first_done.dispose()

    second = capture_prompt("second")
    second.accept("Grace")
    second_done = page.wait_for_function("() => document.body.dataset.second === 'Grace'", timeout=3_000)
    try:
        assert second_done.json_value() is True
    finally:
        second_done.dispose()
    expect_error(
        lambda: second.dismiss(),
        "Dialog.dismiss: Cannot dismiss dialog which is already handled!",
    )

    third = capture_prompt("third")
    third.dismiss()
    third_done = page.wait_for_function("() => document.body.dataset.third === 'null'", timeout=3_000)
    try:
        assert third_done.json_value() is True
    finally:
        third_done.dispose()
    expect_error(
        lambda: third.accept("Grace"),
        "Dialog.accept: Cannot accept dialog which is already handled!",
    )


@case
def context_generic_dialog_event_helpers(page):
    browser = page.context.browser
    context = browser.new_context()
    dialog_page = context.new_page()

    try:
        dialog_page.set_content(
            """
            <script>
            window.showPrompt = () => setTimeout(() => {
              document.body.dataset.promptAnswer = prompt('Context name?', 'Ada');
            }, 0);
            window.showAlert = () => setTimeout(() => {
              alert('Context alert');
              document.body.dataset.alertDone = 'yes';
            }, 100);
            </script>
            """
        )

        with context.expect_event("dialog", lambda dialog: dialog.message == "Context name?") as dialog_info:
            dialog_page.evaluate("() => window.showPrompt()")

        dialog = dialog_info.value
        assert dialog.page is dialog_page
        assert (dialog.type, dialog.message, dialog.default_value) == ("prompt", "Context name?", "Ada")
        dialog.accept("Grace")
        handle = dialog_page.wait_for_function(
            "() => document.body.dataset.promptAnswer === 'Grace'",
            timeout=3_000,
        )
        try:
            assert handle.json_value() is True
        finally:
            handle.dispose()

        dialog_page.evaluate("() => window.showAlert()")
        waited_dialog = context.wait_for_event(
            "dialog",
            lambda candidate: candidate.message == "Context alert",
            timeout=3_000,
        )
        assert waited_dialog.page is dialog_page
        assert (waited_dialog.type, waited_dialog.message, waited_dialog.default_value) == (
            "alert",
            "Context alert",
            "",
        )
        waited_dialog.dismiss()
        done = dialog_page.wait_for_function("() => document.body.dataset.alertDone === 'yes'", timeout=3_000)
        try:
            assert done.json_value() is True
        finally:
            done.dispose()
    finally:
        context.close()


@case
def page_close_run_before_unload_emits_dialog(page):
    browser = page.context.browser
    context = browser.new_context()
    owned_page = context.new_page()

    try:
        owned_page.goto(
            data_url(
                """
            <button id="activate">Activate</button>
            <script>
            window.addEventListener("beforeunload", event => {
              event.preventDefault();
              event.returnValue = "leave?";
            });
            </script>
            """
            )
        )
        owned_page.click("#activate")
        with owned_page.expect_event("dialog") as dialog_info:
            owned_page.close(run_before_unload=True)
        dialog = dialog_info.value
        try:
            dialog.dismiss()
        except Exception:
            pass
        seen = [(dialog.type, dialog.message, dialog.default_value, dialog.page is owned_page)]
        deadline = time.monotonic() + 3
        while not owned_page.is_closed() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        if not context.is_closed():
            context.close()

    assert owned_page.is_closed() is True
    assert owned_page not in context.pages
    assert seen == [("beforeunload", "", "", True)]


@case
def close_reason_option_validation_matches_playwright(page):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("close unexpectedly accepted invalid reason")

    close_page = browser.new_page()
    try:
        expect_error(
            lambda: close_page.close(reason=123),
            "Page.close: reason: expected string, got number",
        )
    finally:
        close_page.close()

    context = browser.new_context()
    try:
        expect_error(
            lambda: context.close(reason=123),
            "BrowserContext.close: reason: expected string, got number",
        )
    finally:
        context.close()

    extra_browser = browser.browser_type.launch(headless=True)
    try:
        expect_error(
            lambda: extra_browser.close(reason=123),
            "Browser.close: reason: expected string, got number",
        )
    finally:
        extra_browser.close()

    already_closed_page = browser.new_page()
    already_closed_page.close()
    already_closed_page.close(reason=123)


@case
def page_close_reason_rejects_waiters_and_closed_actions_like_playwright(page):
    try:
        with page.expect_event("console", timeout=30_000):
            page.close(reason="skyvern close reason")
    except Exception as exc:
        assert exc.__class__.__name__ == "Error"
        assert str(exc).splitlines()[0] == "skyvern close reason"
    else:
        raise AssertionError("page close reason did not reject the pending event waiter")

    try:
        page.locator("body").click(timeout=100)
    except Exception as exc:
        assert exc.__class__.__name__ == "TargetClosedError"
        assert str(exc).splitlines()[0] == "Locator.click: Target page, context or browser has been closed"
    else:
        raise AssertionError("closed-page locator click unexpectedly succeeded")


@case
def context_and_browser_close_reason_reject_page_waiters_like_playwright(page):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert exc.__class__.__name__ == "Error"
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError(f"operation did not raise {expected!r}")

    context = browser.new_context()
    owned_page = context.new_page()

    def close_context_while_waiting_for_page_request():
        with owned_page.expect_event("request", timeout=30_000):
            context.close(reason="skyvern context shutdown")

    try:
        expect_error(close_context_while_waiting_for_page_request, "skyvern context shutdown")
    finally:
        if not context.is_closed():
            context.close()

    generic_context = browser.new_context()

    def close_context_while_waiting_for_context_page():
        with generic_context.expect_event("page", timeout=30_000):
            generic_context.close(reason="skyvern context shutdown")

    try:
        expect_error(
            close_context_while_waiting_for_context_page,
            "Target page, context or browser has been closed",
        )
    finally:
        if not generic_context.is_closed():
            generic_context.close()

    extra_browser = browser.browser_type.launch(headless=True)
    extra_context = extra_browser.new_context()
    extra_page = extra_context.new_page()

    def close_browser_while_waiting_for_page_request():
        with extra_page.expect_event("request", timeout=30_000):
            extra_browser.close(reason="skyvern browser shutdown")

    try:
        expect_error(close_browser_while_waiting_for_page_request, "skyvern browser shutdown")
    finally:
        extra_browser.close()


@case
def browser_bind_returns_endpoint(page):
    browser = page.context.browser
    result = browser.bind("parity-bind", host="127.0.0.1", port=0)
    try:
        assert isinstance(result, dict)
        assert isinstance(result.get("endpoint"), str)
        assert result["endpoint"]
    finally:
        browser.unbind()


@case
def browser_close_is_idempotent_and_emits_disconnected_once(page):
    browser = page.context.browser.browser_type.launch(headless=True)
    seen = []
    once_seen = []

    try:
        browser.on("disconnected", lambda: seen.append("on"))
        browser.once("disconnected", lambda: once_seen.append("once"))
        assert browser.is_connected() is True
        browser.close()
        browser.close()
    finally:
        browser.close()

    assert browser.is_connected() is False
    assert seen == ["on"]
    assert once_seen == ["once"]


@case
def connect_over_cdp_adopts_default_context_pages_and_disconnects_only(page):
    browser_type = page.context.browser.browser_type
    with remote_debugging_chromium(browser_type) as (endpoint, process):
        browser = browser_type.connect_over_cdp(endpoint)
        try:
            assert len(browser.contexts) == 1
            context = browser.contexts[0]
            assert context.browser is browser
            assert len(context.pages) >= 1
            adopted = context.pages[0]
            assert adopted.url == "about:blank"
            adopted.set_content("<title>CDP Adopted</title><h1>ready</h1>")
            assert adopted.title() == "CDP Adopted"
        finally:
            browser.close()
        time.sleep(0.1)
        assert process.poll() is None


@case
def connect_over_cdp_emits_disconnected_when_remote_browser_dies(page):
    browser_type = page.context.browser.browser_type
    with remote_debugging_chromium(browser_type) as (endpoint, process):
        browser = browser_type.connect_over_cdp(endpoint)
        seen = []
        try:
            browser.on("disconnected", lambda: seen.append(browser.is_connected()))
            process.terminate()
            process.wait(timeout=3)
            deadline = time.time() + 5.0
            while time.time() < deadline and not seen:
                time.sleep(0.05)
        finally:
            browser.close()

    assert seen == [False]
    assert browser.is_connected() is False


@case
def connect_over_cdp_context_request_syncs_remote_cookies_for_download(page):
    browser_type = page.context.browser.browser_type
    with header_case_server() as base_url, remote_debugging_chromium(browser_type) as (endpoint, _process):
        browser = browser_type.connect_over_cdp(endpoint)
        try:
            assert browser.contexts
            context = browser.contexts[0]
            assert context.pages
            context.pages[0].goto(f"{base_url}/set-cookies")
            cookie_names = {cookie["name"] for cookie in context.cookies([base_url])}
            assert {"first", "second"} <= cookie_names

            response = context.request.get(f"{base_url}/protected-download")
            assert response.status == 200
            assert response.body() == b"protected-download-body"
        finally:
            browser.close()


@case
def cdp_fetch_response_stream_fulfill_matches_playwright(page):
    with header_case_server() as base_url:
        page.goto(f"{base_url}/fetch-json-page")
        session = page.context.new_cdp_session(page)
        bodies = []
        errors = []

        def on_request_paused(event):
            try:
                request_id = event["requestId"]
                stream_result = session.send("Fetch.takeResponseBodyAsStream", {"requestId": request_id})
                stream_handle = stream_result["stream"]
                chunks = []
                try:
                    while True:
                        read_result = session.send("IO.read", {"handle": stream_handle, "size": 1024})
                        data = read_result.get("data", "")
                        if data:
                            chunks.append(
                                base64.b64decode(data) if read_result.get("base64Encoded") else data.encode()
                            )
                        if read_result.get("eof"):
                            break
                finally:
                    session.send("IO.close", {"handle": stream_handle})

                body = b"".join(chunks)
                bodies.append(body)
                session.send(
                    "Fetch.fulfillRequest",
                    {
                        "requestId": request_id,
                        "responseCode": event["responseStatusCode"],
                        "responseHeaders": event.get("responseHeaders", []),
                        "body": base64.b64encode(body).decode(),
                    },
                )
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        session.on("Fetch.requestPaused", on_request_paused)
        session.send("Fetch.enable", {"patterns": [{"urlPattern": "*headers*", "requestStage": "Response"}]})
        page.click("#fetch-json")
        page.wait_for_function("() => document.body.dataset.fetchOk === 'true'")
        session.send("Fetch.disable")
        session.detach()

        assert errors == []
        assert bodies == [b'{"ok":true}']


@case
def cdp_fetch_continue_request_and_disable_matches_skyvern_interceptor(page):
    with header_case_server() as base_url:
        session = page.context.new_cdp_session(page)
        paused_urls = []
        errors = []

        def on_request_paused(event):
            try:
                paused_urls.append(event["request"]["url"])
                headers = [
                    {"name": name, "value": str(value)}
                    for name, value in event.get("request", {}).get("headers", {}).items()
                ]
                headers.append({"name": "X-Route-Header", "value": "continued-by-cdp"})
                session.send(
                    "Fetch.continueRequest",
                    {
                        "requestId": event["requestId"],
                        "headers": headers,
                    },
                )
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        session.on("Fetch.requestPaused", on_request_paused)
        session.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*echo-headers?cdp-continue-request*", "requestStage": "Request"}]},
        )
        try:
            first_response = page.goto(f"{base_url}/echo-headers?cdp-continue-request")
            assert first_response is not None
            first_body = first_response.json()
            session.send("Fetch.disable")

            second_response = page.goto(f"{base_url}/echo-headers?after-disable")
            assert second_response is not None
            second_body = second_response.json()
        finally:
            session.detach()

    assert errors == []
    assert paused_urls == [f"{base_url}/echo-headers?cdp-continue-request"]
    assert first_body["x-route-header"] == "continued-by-cdp"
    assert second_body["x-route-header"] is None


@case
def cdp_fetch_attachment_download_stream_matches_skyvern_interceptor(page):
    with header_case_server() as base_url:
        page.set_content(f"<a id='download' href='{base_url}/download'>Download</a>")
        session = page.context.new_cdp_session(page)
        bodies = []
        errors = []

        def on_request_paused(event):
            try:
                request_id = event["requestId"]
                if event.get("responseStatusCode") is None:
                    session.send("Fetch.continueRequest", {"requestId": request_id})
                    return
                if not event["request"]["url"].endswith("/download"):
                    session.send("Fetch.continueResponse", {"requestId": request_id})
                    return

                stream_result = session.send("Fetch.takeResponseBodyAsStream", {"requestId": request_id})
                stream_handle = stream_result["stream"]
                chunks = []
                try:
                    while True:
                        read_result = session.send("IO.read", {"handle": stream_handle, "size": 1024})
                        data = read_result.get("data", "")
                        if data:
                            chunks.append(
                                base64.b64decode(data) if read_result.get("base64Encoded") else data.encode()
                            )
                        if read_result.get("eof"):
                            break
                finally:
                    session.send("IO.close", {"handle": stream_handle})

                body = b"".join(chunks)
                bodies.append(body)
                session.send(
                    "Fetch.fulfillRequest",
                    {
                        "requestId": request_id,
                        "responseCode": event["responseStatusCode"],
                        "responseHeaders": event.get("responseHeaders", []),
                        "body": base64.b64encode(body).decode(),
                    },
                )
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        session.on("Fetch.requestPaused", on_request_paused)
        session.send("Fetch.enable", {"patterns": [{"requestStage": "Response"}]})
        try:
            with page.expect_download() as download_info:
                page.click("#download")
            download = download_info.value
            downloaded_path = Path(download.path())
        finally:
            session.send("Fetch.disable")
            session.detach()

    assert errors == []
    assert bodies == [b"download-body"]
    assert download.suggested_filename == "report.txt"
    assert downloaded_path.read_bytes() == b"download-body"


@case
def cdp_fetch_direct_body_and_continue_response_match_playwright(page):
    with header_case_server() as base_url:
        page.goto(f"{base_url}/fetch-json-page")
        session = page.context.new_cdp_session(page)
        bodies = []
        continued_urls = []
        errors = []

        def on_request_paused(event):
            try:
                request_id = event["requestId"]
                request_url = event["request"]["url"]
                if "direct-body" in request_url:
                    direct_result = session.send("Fetch.getResponseBody", {"requestId": request_id})
                    raw_body = direct_result.get("body", "")
                    body = base64.b64decode(raw_body) if direct_result.get("base64Encoded") else raw_body.encode()
                    bodies.append(body)
                    session.send(
                        "Fetch.fulfillRequest",
                        {
                            "requestId": request_id,
                            "responseCode": event["responseStatusCode"],
                            "responseHeaders": event.get("responseHeaders", []),
                            "body": base64.b64encode(body).decode(),
                        },
                    )
                else:
                    continued_urls.append(request_url)
                    session.send("Fetch.continueResponse", {"requestId": request_id})
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        session.on("Fetch.requestPaused", on_request_paused)
        session.send("Fetch.enable", {"patterns": [{"urlPattern": "*headers*", "requestStage": "Response"}]})
        try:
            page.evaluate(
                """async (url) => {
                const response = await fetch(url);
                const data = await response.json();
                document.body.dataset.directBodyOk = String(data.ok);
                }""",
                f"{base_url}/headers?direct-body",
            )
            page.wait_for_function("() => document.body.dataset.directBodyOk === 'true'")
            page.evaluate(
                """async (url) => {
                const response = await fetch(url);
                const data = await response.json();
                document.body.dataset.continueResponseOk = String(data.ok);
                }""",
                f"{base_url}/headers?continue-response",
            )
            page.wait_for_function("() => document.body.dataset.continueResponseOk === 'true'")
        finally:
            session.send("Fetch.disable")
            session.detach()

        assert errors == []
        assert bodies == [b'{"ok":true}']
        assert len(continued_urls) == 1
        assert continued_urls[0].endswith("/headers?continue-response")


@case
def cdp_fetch_auth_required_continue_with_auth_matches_playwright(page):
    expected_authorization = "Basic " + base64.b64encode(b"user:pass").decode("ascii")
    with header_case_server() as base_url:
        session = page.context.new_cdp_session(page)
        auth_events = []
        paused_urls = []
        errors = []

        def on_auth_required(event):
            try:
                auth_events.append(event.get("authChallenge", {}))
                session.send(
                    "Fetch.continueWithAuth",
                    {
                        "requestId": event["requestId"],
                        "authChallengeResponse": {
                            "response": "ProvideCredentials",
                            "username": "user",
                            "password": "pass",
                        },
                    },
                )
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        def on_request_paused(event):
            try:
                paused_urls.append(event["request"]["url"])
                session.send("Fetch.continueRequest", {"requestId": event["requestId"]})
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        session.on("Fetch.authRequired", on_auth_required)
        session.on("Fetch.requestPaused", on_request_paused)
        session.send(
            "Fetch.enable",
            {
                "patterns": [{"urlPattern": "*basic-auth-challenge*", "requestStage": "Request"}],
                "handleAuthRequests": True,
            },
        )
        try:
            response = page.goto(f"{base_url}/basic-auth-challenge")
            assert response is not None
            body = response.json()
        finally:
            session.send("Fetch.disable")
            session.detach()

    assert errors == []
    assert body["authorization"] == expected_authorization
    assert body["attempts"] == 2
    assert len(auth_events) == 1
    assert auth_events[0]["source"] == "Server"
    assert auth_events[0]["scheme"].lower() == "basic"
    assert any(url.endswith("/basic-auth-challenge") for url in paused_urls)


@case
def cdp_storage_clear_and_reset_history_match_skyvern_execution_channel(page):
    with header_case_server() as base_url:
        page.goto(f"{base_url}/query?one")
        page.evaluate(
            """async () => {
            document.cookie = 'skyvern_cookie=yes; path=/';
            localStorage.setItem('skyvern-local', '1');
            sessionStorage.setItem('skyvern-session', '2');
            const cache = await caches.open('skyvern-cache');
            await cache.put('/cached-skyvern-value', new Response('cached'));
            const db = await new Promise((resolve, reject) => {
                const request = indexedDB.open('skyvern-db', 1);
                request.onupgradeneeded = () => request.result.createObjectStore('store');
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
            });
            const tx = db.transaction('store', 'readwrite');
            tx.objectStore('store').put('value', 'key');
            await new Promise((resolve, reject) => {
                tx.oncomplete = resolve;
                tx.onerror = () => reject(tx.error);
            });
            db.close();
            }"""
        )

        session = page.context.new_cdp_session(page)
        try:
            origin = page.evaluate("location.origin")
            assert session.send(
                "Storage.clearDataForOrigin",
                {
                    "origin": origin,
                    "storageTypes": (
                        "cookies,local_storage,session_storage,indexeddb,websql,"
                        "service_workers,cache_storage,shader_cache,file_systems"
                    ),
                },
            ) == {}
            page.reload(wait_until="domcontentloaded")
            values = page.evaluate(
                """async () => {
                const dbs = indexedDB.databases ? await indexedDB.databases() : [];
                const cacheNames = await caches.keys();
                return {
                    cookie: document.cookie,
                    local: localStorage.getItem('skyvern-local'),
                    session: sessionStorage.getItem('skyvern-session'),
                    dbNames: dbs.map(db => db.name).sort(),
                    cacheNames: cacheNames.sort(),
                };
                }"""
            )

            page.goto(f"{base_url}/query?two")
            assert session.send("Page.resetNavigationHistory") == {}
            back_response = page.go_back(wait_until="domcontentloaded", timeout=1000)
        finally:
            session.detach()

    assert values == {
        "cookie": "",
        "local": None,
        "session": "2",
        "dbNames": [],
        "cacheNames": [],
    }
    assert back_response is None
    assert page.url.endswith("/query?two")


@case
def cdp_session_close_event_on_page_close_matches_playwright(page):
    session = page.context.new_cdp_session(page)
    seen: list[bool] = []
    session.on("close", lambda closed_session: seen.append(closed_session is session))

    page.close()
    deadline = time.monotonic() + 2
    while not seen and time.monotonic() < deadline:
        time.sleep(0.02)

    assert seen == [True]
    try:
        session.send("Runtime.evaluate", {"expression": "1+1"})
    except Exception as exc:
        assert str(exc).splitlines()[0] == "CDPSession.send: Target page, context or browser has been closed"
    else:
        raise AssertionError("CDPSession.send unexpectedly succeeded after page close")


@case
def browser_cdp_session_send_after_browser_close_matches_playwright(page):
    browser = page.context.browser
    session = browser.new_browser_cdp_session()
    seen: list[bool] = []
    session.on("close", lambda closed_session: seen.append(closed_session is session))

    browser.close()

    assert seen == []
    try:
        session.send("Browser.getVersion")
    except Exception as exc:
        assert str(exc).splitlines()[0] == "CDPSession.send: Target page, context or browser has been closed"
    else:
        raise AssertionError("browser CDPSession.send unexpectedly succeeded after browser close")


@case
def cdp_runtime_target_and_navigation_events_match_playwright(page):
    browser = page.context.browser
    browser_session = browser.new_browser_cdp_session()
    target_events = []
    page_events = []
    console_values = []
    errors = []

    def wait_for(predicate, label):
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {label}")

    def record_target(name):
        def handler(event):
            try:
                target_events.append((name, event.get("targetInfo", event)))
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        return handler

    def record_page_event(name):
        def handler(event):
            try:
                page_events.append((name, event))
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        return handler

    browser_session.on("Target.targetCreated", record_target("created"))
    browser_session.on("Target.targetInfoChanged", record_target("info"))
    browser_session.on("Target.targetDestroyed", record_target("destroyed"))
    browser_session.send("Target.setDiscoverTargets", {"discover": True})

    with header_case_server() as base_url:
        extra_page = browser.new_page()
        try:
            extra_page.goto(f"{base_url}/headers?target-info")
            wait_for(
                lambda: any(
                    name == "info" and info.get("url", "").endswith("/headers?target-info")
                    for name, info in target_events
                ),
                "Target.targetInfoChanged for navigated page",
            )
        finally:
            extra_page.close()

        wait_for(
            lambda: any(name == "created" and info.get("type") == "page" for name, info in target_events),
            "Target.targetCreated page event",
        )
        wait_for(
            lambda: any(name == "destroyed" and info.get("targetId") for name, info in target_events),
            "Target.targetDestroyed page event",
        )

        session = page.context.new_cdp_session(page)
        try:
            session.on(
                "Runtime.consoleAPICalled",
                lambda event: console_values.append([arg.get("value") for arg in event.get("args", [])]),
            )
            for event_name in [
                "Page.frameRequestedNavigation",
                "Page.frameStartedNavigating",
                "Page.frameNavigated",
                "Page.navigatedWithinDocument",
            ]:
                session.on(event_name, record_page_event(event_name))

            session.send("Runtime.enable")
            session.send("Page.enable")
            page.set_content(f"<a id='nav' href='{base_url}/headers?cdp-nav'>Navigate</a>")
            page.evaluate("() => console.log('skyvern-cdp-console', 7)")
            wait_for(
                lambda: ["skyvern-cdp-console", 7] in console_values,
                "Runtime.consoleAPICalled payload",
            )

            page.click("#nav")
            page.wait_for_url("**/headers?cdp-nav")
            page.evaluate("() => { location.hash = 'same-doc'; }")
            page.wait_for_function("() => location.hash === '#same-doc'")
            for event_name in [
                "Page.frameRequestedNavigation",
                "Page.frameStartedNavigating",
                "Page.frameNavigated",
                "Page.navigatedWithinDocument",
            ]:
                wait_for(
                    lambda event_name=event_name: event_name in {name for name, _event in page_events},
                    event_name,
                )
        finally:
            session.detach()
            browser_session.detach()

    assert errors == []


@case
def cdp_page_session_target_discovery_events_match_playwright(page):
    browser = page.context.browser
    session = page.context.new_cdp_session(page)
    target_events = []
    errors = []

    def record_target(name):
        def handler(event):
            try:
                target_events.append((name, event.get("targetInfo", event)))
            except BaseException as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        return handler

    def wait_for(predicate, label):
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {label}")

    session.on("Target.targetCreated", record_target("created"))
    session.on("Target.targetInfoChanged", record_target("info"))
    session.on("Target.targetDestroyed", record_target("destroyed"))
    try:
        session.send("Target.setDiscoverTargets", {"discover": True})
        with header_case_server() as base_url:
            target_page = browser.new_page()
            try:
                target_page.goto(f"{base_url}/headers?page-session-target")
                wait_for(
                    lambda: any(
                        name == "info" and info.get("url", "").endswith("/headers?page-session-target")
                        for name, info in target_events
                    ),
                    "Target.targetInfoChanged from page-scoped CDP session",
                )
            finally:
                target_page.close()

        wait_for(
            lambda: any(name == "created" and info.get("type") == "page" for name, info in target_events),
            "Target.targetCreated from page-scoped CDP session",
        )
        wait_for(
            lambda: any(name == "destroyed" and info.get("targetId") for name, info in target_events),
            "Target.targetDestroyed from page-scoped CDP session",
        )
    finally:
        session.detach()

    assert errors == []


@case
def cdp_input_dispatch_mouse_key_and_wheel_matches_playwright(page):
    page.set_viewport_size({"width": 400, "height": 300})
    page.set_content(
        """
        <input id="field" style="margin:20px;width:120px;height:24px">
        <div id="scroller" style="margin:20px;width:120px;height:60px;overflow:auto;background:#eee">
          <div style="height:500px"></div>
        </div>
        <script>
        window.events = [];
        for (const type of ['mousemove', 'mousedown', 'mouseup', 'click', 'wheel', 'keydown', 'keyup', 'input']) {
          document.addEventListener(type, event => window.events.push({
            type,
            target: event.target.id || event.target.tagName,
            key: event.key || '',
            deltaY: event.deltaY || 0,
            trusted: event.isTrusted
          }));
        }
        </script>
        """
    )
    session = page.context.new_cdp_session(page)
    try:
        field_box = page.locator("#field").bounding_box()
        assert field_box is not None
        field_x = field_box["x"] + 8
        field_y = field_box["y"] + 8
        session.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": field_x, "y": field_y, "button": "none"})
        session.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": field_x, "y": field_y, "button": "left", "clickCount": 1},
        )
        session.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": field_x, "y": field_y, "button": "left", "clickCount": 1},
        )
        page.wait_for_function("() => document.activeElement.id === 'field'")

        session.send(
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": "a", "code": "KeyA", "text": "a", "windowsVirtualKeyCode": 65},
        )
        session.send(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65},
        )
        page.wait_for_function("() => document.querySelector('#field').value === 'a'")

        scroller_box = page.locator("#scroller").bounding_box()
        assert scroller_box is not None
        wheel_x = scroller_box["x"] + 20
        wheel_y = scroller_box["y"] + 20
        session.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": wheel_x, "y": wheel_y, "button": "none"})
        session.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": wheel_x, "y": wheel_y, "deltaX": 0, "deltaY": 120},
        )
        page.wait_for_function("() => document.querySelector('#scroller').scrollTop > 0")
    finally:
        session.detach()

    result = page.evaluate(
        """() => ({
        value: document.querySelector('#field').value,
        scrollTop: document.querySelector('#scroller').scrollTop,
        events: window.events
        })"""
    )
    assert result["value"] == "a"
    assert result["scrollTop"] > 0
    event_types = [event["type"] for event in result["events"]]
    assert event_types[:7] == ["mousemove", "mousedown", "mouseup", "click", "keydown", "input", "keyup"]
    assert any(event["type"] == "wheel" and event["deltaY"] == 120 for event in result["events"])
    assert all(event["trusted"] is True for event in result["events"])


@case
def cdp_screencast_frame_stream_matches_playwright(page):
    page.set_viewport_size({"width": 320, "height": 240})
    page.set_content(
        """
        <main style="background:#f8f2aa;width:100%;height:100%">
          <h1>Skyvern Screencast</h1>
        </main>
        """
    )
    session = page.context.new_cdp_session(page)
    frames = []
    errors = []

    def on_frame(event):
        try:
            frames.append(event)
            session.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
        except BaseException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    session.on("Page.screencastFrame", on_frame)
    try:
        session.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 60, "maxWidth": 320, "maxHeight": 240},
        )
        page.evaluate("() => document.body.dataset.screencast = 'running'")
        for tick in range(100):
            if frames or errors:
                break
            page.evaluate(
                "(tick) => { document.body.style.outline = `${tick % 2}px solid transparent`; }",
                tick,
            )
            page.wait_for_timeout(50)
        assert frames, "expected at least one Page.screencastFrame event"
    finally:
        try:
            session.send("Page.stopScreencast", {})
        finally:
            session.detach()

    assert errors == []
    first_frame = frames[0]
    assert sorted(first_frame.keys()) == ["data", "metadata", "sessionId"]
    image_bytes = base64.b64decode(first_frame["data"])
    assert image_bytes.startswith(b"\xff\xd8")
    metadata = first_frame["metadata"]
    assert metadata["deviceWidth"] == 320
    assert metadata["deviceHeight"] == 240
    assert metadata["pageScaleFactor"] > 0


@case
def browser_type_connect_over_cdp_validation_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("connect_over_cdp validation unexpectedly succeeded")

    expect_error(
        lambda: browser_type.connect_over_cdp(123),
        "BrowserType.connect_over_cdp: endpoint_url: expected string, got number",
    )
    expect_error(
        lambda: browser_type.connect_over_cdp(""),
        "BrowserType.connect_over_cdp: Invalid URL",
    )
    expect_error(
        lambda: browser_type.connect_over_cdp("ws://127.0.0.1:1/devtools/browser/test", timeout="10"),
        "BrowserType.connect_over_cdp: timeout: expected float, got string",
    )
    expect_error(
        lambda: browser_type.connect_over_cdp("ws://127.0.0.1:1/devtools/browser/test", slow_mo=True),
        "BrowserType.connect_over_cdp: slow_mo: expected float, got boolean",
    )
    expect_error(
        lambda: browser_type.connect_over_cdp("ws://127.0.0.1:1/devtools/browser/test", is_local=1),
        "BrowserType.connect_over_cdp: is_local: expected boolean, got number",
    )
    expect_error(
        lambda: browser_type.connect_over_cdp("ws://127.0.0.1:1/devtools/browser/test", headers="x"),
        "'str' object has no attribute 'items'",
        AttributeError,
    )
    expect_error(
        lambda: browser_type.connect_over_cdp("ws://127.0.0.1:1/devtools/browser/test", headers={1: "value"}),
        "BrowserType.connect_over_cdp: headers[0].name: expected string, got number",
    )
    expect_error(
        lambda: browser_type.connect_over_cdp("ws://127.0.0.1:1/devtools/browser/test", headers={"X-Test": 1}),
        "BrowserType.connect_over_cdp: headers[0].value: expected string, got number",
    )


@case
def connect_over_cdp_http_discovery_status_error_matches_playwright(page):
    browser_type = page.context.browser.browser_type
    with cdp_discovery_status_server(500) as endpoint:
        try:
            browser_type.connect_over_cdp(endpoint, timeout=1000)
        except Exception as exc:
            first_line = str(exc).splitlines()[0]
        else:
            raise AssertionError("connect_over_cdp unexpectedly succeeded for HTTP 500 discovery endpoint")

    assert first_line == (
        "BrowserType.connect_over_cdp: Unexpected status 500 when connecting to "
        f"{endpoint}/json/version/."
    )


@case
def connect_over_cdp_websocket_status_error_matches_playwright(page):
    browser_type = page.context.browser.browser_type
    with cdp_websocket_status_server(502, "Bad Gateway") as (endpoint, body):
        try:
            browser_type.connect_over_cdp(endpoint, timeout=1000)
        except Exception as exc:
            lines = str(exc).splitlines()
        else:
            raise AssertionError("connect_over_cdp unexpectedly succeeded for HTTP 502 WebSocket endpoint")

    assert lines[0] == f"BrowserType.connect_over_cdp: WebSocket error: {endpoint} 502 Bad Gateway"
    assert lines[1] == body
    assert any(
        line == f"  - <ws unexpected response> {endpoint} 502 Bad Gateway" for line in lines
    )


@case
def browser_type_launch_ignore_default_args_filters_selected_defaults(page):
    browser_type = page.context.browser.browser_type
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        args_file = tmp_path / "launch-args.json"
        real_chromium = browser_type.executable_path
        wrapper = tmp_path / "chromium-arg-probe.py"
        wrapper.write_text(
            "\n".join(
                [
                    f"#!{sys.executable}",
                    "import json",
                    "import os",
                    "from pathlib import Path",
                    "import sys",
                    f"Path({str(args_file)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')",
                    f"os.execv({real_chromium!r}, [{real_chromium!r}] + sys.argv[1:])",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        browser = browser_type.launch(
            headless=True,
            executable_path=str(wrapper),
            ignore_default_args=["--mute-audio"],
            args=["--disable-features=RustwrightIgnoreDefaultArgsProbe"],
        )
        try:
            launched_page = browser.new_page()
            launched_page.set_content("<title>Ignore Default Args</title>")
            launch_args = json.loads(args_file.read_text(encoding="utf-8"))
        finally:
            browser.close()

    assert "--mute-audio" not in launch_args
    assert "--disable-features=RustwrightIgnoreDefaultArgsProbe" in launch_args


@case
def browser_type_launch_ignore_default_args_validation_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("ignore_default_args validation unexpectedly succeeded")

    expect_error(
        lambda: browser_type.launch(headless=True, ignore_default_args="--mute-audio"),
        "BrowserType.launch: ignore_default_args: expected array, got string",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, ignore_default_args=123),
        "BrowserType.launch: ignore_default_args: expected array, got number",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, ignore_default_args=[123]),
        "BrowserType.launch: ignoreDefaultArgs[0]: expected string, got number",
    )


@case
def browser_type_launch_boolean_option_validation_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("launch boolean option validation unexpectedly succeeded")

    expect_error(
        lambda: browser_type.launch(headless="yes"),
        "BrowserType.launch: headless: expected boolean, got string",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, chromium_sandbox=1),
        "BrowserType.launch: chromium_sandbox: expected boolean, got number",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, handle_sigint=[]),
        "BrowserType.launch: handle_sigint: expected boolean, got object",
    )


@case
def browser_type_launch_args_validation_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("launch args validation unexpectedly succeeded")

    expect_error(
        lambda: browser_type.launch(headless=True, args="--mute-audio"),
        "BrowserType.launch: args: expected array, got string",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, args=123),
        "BrowserType.launch: args: expected array, got number",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, args=["--mute-audio", 123]),
        "BrowserType.launch: args[1]: expected string, got number",
    )


@case
def browser_type_launch_args_cannot_open_page_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("launch page-argument validation unexpectedly succeeded")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        port = unused_tcp_port()
        expect_error(
            lambda: browser_type.launch(headless=True, args=["about:blank"], timeout=1_000),
            "BrowserType.launch: Arguments can not specify page to be opened",
        )
        expect_error(
            lambda: browser_type.launch(
                headless=True,
                args=["--host-resolver-rules", "MAP example.com 127.0.0.1"],
                timeout=1_000,
            ),
            "BrowserType.launch: Arguments can not specify page to be opened",
        )
        expect_error(
            lambda: browser_type.launch(
                headless=True,
                args=["--remote-debugging-port", str(port)],
                timeout=1_000,
            ),
            "BrowserType.launch: Arguments can not specify page to be opened",
        )
        expect_error(
            lambda: browser_type.launch_persistent_context(
                str(root / "profile"),
                headless=True,
                args=["https://example.com"],
                timeout=1_000,
            ),
            "BrowserType.launch_persistent_context: Arguments can not specify page to be opened",
        )


@case
def browser_type_launch_user_data_dir_arg_rejection_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("--user-data-dir launch arg validation unexpectedly succeeded")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        expected_launch = (
            "BrowserType.launch: Pass user_data_dir parameter to "
            "'browser_type.launch_persistent_context(user_data_dir, **kwargs)' "
            "instead of specifying '--user-data-dir' argument"
        )
        expected_persistent = (
            "BrowserType.launch_persistent_context: Pass user_data_dir parameter to "
            "'browser_type.launch_persistent_context(user_data_dir, **kwargs)' "
            "instead of specifying '--user-data-dir' argument"
        )
        expect_error(
            lambda: browser_type.launch(
                headless=True,
                args=[f"--user-data-dir={root / 'launch-profile'}"],
                timeout=1_000,
            ),
            expected_launch,
        )
        expect_error(
            lambda: browser_type.launch(
                headless=True,
                args=["--user-data-dir", str(root / "launch-profile-split")],
                timeout=1_000,
            ),
            expected_launch,
        )
        expect_error(
            lambda: browser_type.launch_persistent_context(
                str(root / "persistent-profile"),
                headless=True,
                args=[f"--user-data-dir={root / 'persistent-arg-profile'}"],
                timeout=1_000,
            ),
            expected_persistent,
        )


@case
def browser_type_launch_remote_debugging_pipe_arg_rejection_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("--remote-debugging-pipe launch arg validation unexpectedly succeeded")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        expect_error(
            lambda: browser_type.launch(
                headless=True,
                args=["--remote-debugging-pipe"],
                timeout=1_000,
            ),
            "BrowserType.launch: Playwright manages remote debugging connection itself.",
        )
        expect_error(
            lambda: browser_type.launch(
                headless=True,
                args=["--remote-debugging-pipe=1"],
                timeout=1_000,
            ),
            "BrowserType.launch: Playwright manages remote debugging connection itself.",
        )
        expect_error(
            lambda: browser_type.launch_persistent_context(
                str(root / "persistent-profile"),
                headless=True,
                args=["--remote-debugging-pipe"],
                timeout=1_000,
            ),
            "BrowserType.launch_persistent_context: Playwright manages remote debugging connection itself.",
        )


@case
def browser_type_launch_timeout_validation_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("launch timeout validation unexpectedly succeeded")

    expect_error(
        lambda: browser_type.launch(headless=True, timeout="100"),
        "BrowserType.launch: timeout: expected float, got string",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, timeout=True),
        "BrowserType.launch: timeout: expected float, got boolean",
    )


@case
def browser_type_launch_environment_path_and_pacing_validation_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("launch environment/path/pacing validation unexpectedly succeeded")

    expect_error(
        lambda: browser_type.launch(headless=True, slow_mo="10"),
        "BrowserType.launch: slow_mo: expected float, got string",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, channel=123),
        "BrowserType.launch: channel: expected string, got number",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, env={1: "value"}),
        "BrowserType.launch: env[0].name: expected string, got number",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, env="x"),
        "'str' object has no attribute 'items'",
        AttributeError,
    )
    expect_error(
        lambda: browser_type.launch(headless=True, executable_path=123),
        "argument should be a str or an os.PathLike object where __fspath__ returns a str, not 'int'",
        TypeError,
    )
    expect_error(
        lambda: browser_type.launch(headless=True, downloads_path=[]),
        "argument should be a str or an os.PathLike object where __fspath__ returns a str, not 'list'",
        TypeError,
    )
    expect_error(
        lambda: browser_type.launch(headless=True, timeout=[]),
        "BrowserType.launch: timeout: expected float, got object",
    )


@case
def browser_type_launch_executable_path_failure_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("launch executable_path failure unexpectedly succeeded")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        missing = str(root / "missing-browser")
        expect_error(
            lambda: browser_type.launch(headless=True, executable_path=missing, timeout=1_000),
            f"BrowserType.launch: Failed to launch chromium because executable doesn't exist at {missing}",
        )
        expect_error(
            lambda: browser_type.launch_persistent_context(
                str(root / "profile-missing"),
                headless=True,
                executable_path=missing,
                timeout=1_000,
            ),
            f"BrowserType.launch_persistent_context: Failed to launch chromium because executable doesn't exist at {missing}",
        )

        if sys.platform != "win32":
            non_executable = root / "not-executable"
            non_executable.write_text("not a browser", encoding="utf-8")
            non_executable.chmod(0o644)
            expect_error(
                lambda: browser_type.launch(headless=True, executable_path=str(non_executable), timeout=1_000),
                f"BrowserType.launch: Failed to launch: Error: spawn {non_executable} EACCES",
            )
            expect_error(
                lambda: browser_type.launch_persistent_context(
                    str(root / "profile-eacces"),
                    headless=True,
                    executable_path=str(non_executable),
                    timeout=1_000,
                ),
                f"BrowserType.launch_persistent_context: Failed to launch: Error: spawn {non_executable} EACCES",
            )


@case
def browser_type_launch_chromium_channel_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    browser = browser_type.launch(headless=True, channel="chromium", timeout=10_000)
    try:
        new_page = browser.new_page()
        new_page.goto("data:text/html,<title>chromium-channel</title>")
        assert "chromium-channel" in new_page.title()
    finally:
        browser.close()

    with tempfile.TemporaryDirectory() as directory:
        context = browser_type.launch_persistent_context(
            str(Path(directory) / "profile"),
            headless=True,
            channel="chromium",
            timeout=10_000,
        )
        try:
            assert len(context.pages) >= 1
        finally:
            context.close()

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("unsupported channel validation unexpectedly succeeded")

    expect_error(
        lambda: browser_type.launch(headless=True, channel="not-real-channel", timeout=1_000),
        'BrowserType.launch: Unsupported chromium channel "not-real-channel"',
    )
    with tempfile.TemporaryDirectory() as directory:
        expect_error(
            lambda: browser_type.launch_persistent_context(
                str(Path(directory) / "profile-invalid"),
                headless=True,
                channel="not-real-channel",
                timeout=1_000,
            ),
            'BrowserType.launch_persistent_context: Unsupported chromium channel "not-real-channel"',
        )


@case
def browser_type_launch_user_remote_debugging_port_matches_playwright(page):
    browser_type = page.context.browser.browser_type
    port = unused_tcp_port()

    browser = browser_type.launch(
        headless=True,
        args=[f"--remote-debugging-port={port}"],
        timeout=10_000,
    )
    try:
        launched_page = browser.new_page()
        launched_page.set_content("<title>Launch User CDP Port</title>")
        assert launched_page.title() == "Launch User CDP Port"

        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["webSocketDebuggerUrl"].startswith(f"ws://127.0.0.1:{port}/")
    finally:
        browser.close()


@case
def launch_persistent_context_user_remote_debugging_port_matches_playwright(page):
    browser_type = page.context.browser.browser_type
    port = unused_tcp_port()

    with tempfile.TemporaryDirectory() as directory:
        context = browser_type.launch_persistent_context(
            str(Path(directory) / "profile"),
            headless=True,
            args=[f"--remote-debugging-port={port}"],
            timeout=10_000,
        )
        try:
            initial_page = context.pages[0] if context.pages else context.new_page()
            initial_page.set_content("<title>User CDP Port</title>")
            assert initial_page.title() == "User CDP Port"

            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            assert payload["webSocketDebuggerUrl"].startswith(f"ws://127.0.0.1:{port}/")
        finally:
            context.close()


@case
def launch_persistent_context_dynamic_remote_debugging_port_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        profile = root / "profile"
        context = browser_type.launch_persistent_context(
            str(profile),
            headless=True,
            args=["--remote-debugging-port=0"],
            timeout=10_000,
        )
        try:
            initial_page = context.pages[0] if context.pages else context.new_page()
            initial_page.set_content("<title>Dynamic CDP Port</title>")
            assert initial_page.title() == "Dynamic CDP Port"

            active_port_file = profile / "DevToolsActivePort"
            lines = active_port_file.read_text(encoding="utf-8").splitlines()
            assert len(lines) >= 2
            port = int(lines[0])
            browser_path = lines[1]
            assert port > 0
            assert browser_path.startswith("/devtools/browser/")
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            assert payload["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{port}{browser_path}"
        finally:
            context.close()


@case
def browser_type_launch_proxy_validation_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("launch proxy validation unexpectedly succeeded")

    expect_error(
        lambda: browser_type.launch(headless=True, proxy="bad"),
        "BrowserType.launch: proxy: expected object, got string",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, proxy={}),
        "BrowserType.launch: proxy.server: expected string, got undefined",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, proxy={"server": 1}),
        "BrowserType.launch: proxy.server: expected string, got number",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, proxy={"server": "http://127.0.0.1:1", "bypass": 1}),
        "BrowserType.launch: proxy.bypass: expected string, got number",
    )
    expect_error(
        lambda: browser_type.launch(headless=True, proxy={"server": ""}),
        "BrowserType.launch: Invalid URL",
    )


@case
def browser_type_launch_persistent_context_option_validation_matches_playwright(page):
    browser_type = page.context.browser.browser_type

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("persistent context option validation unexpectedly succeeded")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cases = [
            (
                "java-script",
                {"java_script_enabled": "bad"},
                "BrowserType.launch_persistent_context: java_script_enabled: expected boolean, got string",
            ),
            (
                "service-workers",
                {"service_workers": "bad"},
                "BrowserType.launch_persistent_context: service_workers: expected one of (allow|block)",
            ),
            (
                "color-scheme",
                {"color_scheme": "bad"},
                "BrowserType.launch_persistent_context: color_scheme: expected one of (dark|light|no-preference|no-override)",
            ),
            (
                "geolocation",
                {"geolocation": {"latitude": 100, "longitude": 0}},
                "BrowserType.launch_persistent_context: geolocation.latitude: precondition -90 <= LATITUDE <= 90 failed.",
            ),
            (
                "geolocation-empty",
                {"geolocation": {}},
                "BrowserType.launch_persistent_context: geolocation.longitude: expected float, got undefined",
            ),
            (
                "record-video-size",
                {
                    "record_video_dir": str(root / "videos"),
                    "record_video_size": {"width": "100", "height": 100},
                },
                "BrowserType.launch_persistent_context: recordVideo.size.width: expected integer, got string",
            ),
            (
                "extra-headers",
                {"extra_http_headers": {"X-Test": 1}},
                "BrowserType.launch_persistent_context: extraHTTPHeaders[0].value: expected string, got number",
            ),
            (
                "extra-headers-container",
                {"extra_http_headers": []},
                "'list' object has no attribute 'items'",
            ),
        ]
        for name, options, message in cases:
            expect_error(
                lambda options=options, name=name: browser_type.launch_persistent_context(
                    str(root / f"profile-{name}"),
                    headless=True,
                    **options,
                ),
                message,
            )


@case
def closed_page_and_context_are_removed_from_owner_lists(page):
    browser = page.context.browser
    context = browser.new_context()
    owned_page = context.new_page()
    page_close_seen = []
    context_close_seen = []

    try:
        owned_page.on("close", lambda closed_page: page_close_seen.append((closed_page is owned_page, owned_page in context.pages)))
        context.on("close", lambda closed_context: context_close_seen.append((closed_context is context, context in browser.contexts)))
        assert owned_page in context.pages
        assert context in browser.contexts
        owned_page.close()
        assert owned_page not in context.pages
        context.close()
        assert context not in browser.contexts
    finally:
        if not context.is_closed():
            context.close()

    assert page_close_seen == [(True, False)]
    assert context_close_seen == [(True, False)]


@case
def page_and_context_close_events_return_owner_objects(page):
    browser = page.context.browser
    context = browser.new_context()
    owned_page = context.new_page()

    try:
        with owned_page.expect_event("close") as page_close_info:
            owned_page.close()
        with context.expect_event("close") as context_close_info:
            context.close()
    finally:
        if not context.is_closed():
            context.close()

    assert page_close_info.value is owned_page
    assert context_close_info.value is context


@case
def close_event_handlers_accept_zero_or_value_callbacks_like_playwright(page):
    browser = page.context.browser
    context = browser.new_context()
    owned_page = context.new_page()
    seen: list[object] = []

    def page_zero_arg() -> None:
        seen.append("page-zero")

    def page_value_arg(closed_page) -> None:
        seen.append(("page-value", closed_page is owned_page))

    def context_zero_arg() -> None:
        seen.append("context-zero")

    def context_value_arg(closed_context) -> None:
        seen.append(("context-value", closed_context is context))

    try:
        owned_page.on("close", page_zero_arg)
        owned_page.on("close", page_value_arg)
        context.on("close", context_zero_arg)
        context.on("close", context_value_arg)
        owned_page.close()
        context.close()
    finally:
        if not owned_page.is_closed():
            owned_page.close()
        if not context.is_closed():
            context.close()

    assert seen == [
        "page-zero",
        ("page-value", True),
        "context-zero",
        ("context-value", True),
    ]


@case
def closed_page_and_context_close_waiters_do_not_replay(page):
    browser = page.context.browser
    context = browser.new_context()
    owned_page = context.new_page()

    def expect_timeout(operation):
        try:
            operation()
        except Exception as exc:
            assert exc.__class__.__name__ == "TimeoutError"
            assert str(exc).splitlines()[0] == 'Timeout 10ms exceeded while waiting for event "close"'
            return
        raise AssertionError("closed close event waiter unexpectedly replayed")

    try:
        with owned_page.expect_event("close") as page_close_info:
            owned_page.close()
        assert page_close_info.value is owned_page
        expect_timeout(lambda: owned_page.wait_for_event("close", timeout=10))

        with context.expect_event("close") as context_close_info:
            context.close()
        assert context_close_info.value is context
        expect_timeout(lambda: context.wait_for_event("close", timeout=10))
    finally:
        if not owned_page.is_closed():
            owned_page.close()
        if not context.is_closed():
            context.close()


@case
def specialized_event_timeout_messages_match_playwright(page):
    def expect_timeout(event, operation):
        try:
            operation()
        except Exception as exc:
            assert exc.__class__.__name__ == "TimeoutError"
            assert str(exc).splitlines()[0] == f'Timeout 5ms exceeded while waiting for event "{event}"'
            return
        raise AssertionError(f"{event} waiter unexpectedly resolved")

    page_events = [
        "request",
        "response",
        "requestfinished",
        "requestfailed",
        "console",
        "dialog",
        "pageerror",
        "download",
        "filechooser",
        "popup",
        "websocket",
        "worker",
        "load",
        "domcontentloaded",
        "framenavigated",
        "frameattached",
        "framedetached",
    ]
    for event in page_events:
        expect_timeout(event, lambda event=event: page.wait_for_event(event, timeout=5))

    context = page.context.browser.new_context()
    try:
        context.new_page()
        context_events = [
            "page",
            "request",
            "response",
            "requestfinished",
            "requestfailed",
            "console",
            "dialog",
            "weberror",
            "pageload",
            "pageclose",
            "backgroundpage",
            "serviceworker",
        ]
        for event in context_events:
            expect_timeout(event, lambda event=event: context.wait_for_event(event, timeout=5))
    finally:
        context.close()


@case
def unknown_event_waiters_timeout_like_playwright(page):
    def expect_timeout(event, operation):
        try:
            operation()
        except Exception as exc:
            assert exc.__class__.__name__ == "TimeoutError"
            assert str(exc).splitlines()[0] == f'Timeout 5ms exceeded while waiting for event "{event}"'
            return
        raise AssertionError(f"{event} waiter unexpectedly resolved")

    def expect_context_timeout(owner, event):
        with owner.expect_event(event, timeout=5):
            pass

    expect_timeout("unknown-event", lambda: page.wait_for_event("unknown-event", timeout=5))
    expect_timeout("unknown-event", lambda: expect_context_timeout(page, "unknown-event"))

    context = page.context.browser.new_context()
    try:
        expect_timeout("unknown-event", lambda: context.wait_for_event("unknown-event", timeout=5))
        expect_timeout("unknown-event", lambda: expect_context_timeout(context, "unknown-event"))
    finally:
        context.close()


@case
def specialized_event_waiters_reject_when_owner_closes(page):
    browser = page.context.browser

    def expect_target_closed(operation):
        try:
            operation()
        except Exception as exc:
            assert exc.__class__.__name__ == "Error"
            assert "Target page, context or browser has been closed" in str(exc).splitlines()[0]
            return
        raise AssertionError("specialized event waiter did not reject when owner closed")

    page_events = [
        "request",
        "response",
        "requestfinished",
        "requestfailed",
        "console",
        "dialog",
        "pageerror",
        "download",
        "filechooser",
        "popup",
        "websocket",
        "worker",
        "load",
        "domcontentloaded",
        "framenavigated",
        "frameattached",
        "framedetached",
    ]
    for event in page_events:
        owned_page = browser.new_page()
        try:
            def close_inside_waiter(event=event, owned_page=owned_page):
                with owned_page.expect_event(event, timeout=3_000):
                    owned_page.close()

            expect_target_closed(close_inside_waiter)
        finally:
            if not owned_page.is_closed():
                owned_page.close()

    context_events = [
        "page",
        "request",
        "response",
        "requestfinished",
        "requestfailed",
        "console",
        "dialog",
        "weberror",
        "pageload",
        "backgroundpage",
        "serviceworker",
    ]
    for event in context_events:
        context = browser.new_context()
        context.new_page()
        try:
            def close_context_inside_waiter(event=event, context=context):
                with context.expect_event(event, timeout=3_000):
                    context.close()

            expect_target_closed(close_context_inside_waiter)
        finally:
            if not context.is_closed():
                context.close()


@case
def non_close_event_waiters_reject_when_owner_closes(page):
    browser = page.context.browser

    def expect_target_closed(operation):
        try:
            operation()
        except Exception as exc:
            assert exc.__class__.__name__ == "Error"
            assert "Target page, context or browser has been closed" in str(exc).splitlines()[0]
            return
        raise AssertionError("non-close event waiter did not reject when owner closed")

    context = browser.new_context()
    owned_page = context.new_page()
    try:
        def wait_for_page_crash_then_close():
            with owned_page.expect_event("crash", timeout=3_000):
                owned_page.close()

        expect_target_closed(wait_for_page_crash_then_close)
    finally:
        if not context.is_closed():
            context.close()

    context = browser.new_context()
    try:
        def wait_for_context_page_then_close():
            with context.expect_event("page", timeout=3_000):
                context.close()

        expect_target_closed(wait_for_context_page_then_close)
    finally:
        if not context.is_closed():
            context.close()


@case
def browser_close_closes_contexts_and_pages_before_disconnected(page):
    browser = page.context.browser.browser_type.launch(headless=True)
    context = browser.new_context()
    owned_page = context.new_page()
    events = []

    owned_page.on(
        "close",
        lambda closed_page: events.append(
            (
                "page",
                closed_page is owned_page,
                browser.is_connected(),
                context.is_closed(),
                owned_page.is_closed(),
                context in browser.contexts,
                owned_page in context.pages,
            )
        ),
    )
    context.on(
        "close",
        lambda closed_context: events.append(
            (
                "context",
                closed_context is context,
                browser.is_connected(),
                context.is_closed(),
                owned_page.is_closed(),
                context in browser.contexts,
                owned_page in context.pages,
            )
        ),
    )
    browser.on(
        "disconnected",
        lambda: events.append(
            (
                "browser",
                browser.is_connected(),
                context.is_closed(),
                owned_page.is_closed(),
                context in browser.contexts,
                owned_page in context.pages,
            )
        ),
    )

    try:
        browser.close()
    finally:
        browser.close()

    assert browser.is_connected() is False
    assert context.is_closed() is True
    assert owned_page.is_closed() is True
    assert context not in browser.contexts
    assert owned_page not in context.pages
    assert events == [
        ("page", True, True, False, True, True, False),
        ("context", True, True, True, True, False, False),
        ("browser", False, True, True, False, False),
    ]


@case
def browser_close_closes_implicit_context_page_before_disconnected(page):
    browser = page.context.browser.browser_type.launch(headless=True)
    owned_page = browser.new_page()
    context = owned_page.context
    events = []

    owned_page.on(
        "close",
        lambda closed_page: events.append(
            (
                "page",
                closed_page is owned_page,
                browser.is_connected(),
                context.is_closed(),
                owned_page.is_closed(),
                context in browser.contexts,
                owned_page in context.pages,
            )
        ),
    )
    context.on(
        "close",
        lambda closed_context: events.append(
            (
                "context",
                closed_context is context,
                browser.is_connected(),
                context.is_closed(),
                owned_page.is_closed(),
                context in browser.contexts,
                owned_page in context.pages,
            )
        ),
    )
    browser.on(
        "disconnected",
        lambda: events.append(
            (
                "browser",
                browser.is_connected(),
                context.is_closed(),
                owned_page.is_closed(),
                context in browser.contexts,
                owned_page in context.pages,
            )
        ),
    )

    try:
        browser.close()
    finally:
        browser.close()

    assert browser.is_connected() is False
    assert context.is_closed() is True
    assert owned_page.is_closed() is True
    assert context not in browser.contexts
    assert owned_page not in context.pages
    assert events == [
        ("page", True, True, False, True, True, False),
        ("context", True, True, True, True, False, False),
        ("browser", False, True, True, False, False),
    ]


@case
def playwright_private_target_closed_error_import_and_type(page):
    from playwright._impl._errors import Error as ImplError
    from playwright._impl._errors import TargetClosedError
    from playwright._impl._errors import TimeoutError as ImplTimeoutError
    from playwright.sync_api import Error as PublicError
    from playwright.sync_api import TimeoutError as PublicTimeoutError

    assert ImplError is PublicError
    assert ImplTimeoutError is PublicTimeoutError
    assert issubclass(TargetClosedError, ImplError)
    assert not issubclass(TargetClosedError, ImplTimeoutError)

    closed_page = page.context.browser.new_page()
    closed_page.close()
    try:
        closed_page.title()
    except TargetClosedError as exc:
        assert "Target page, context or browser has been closed" in str(exc).splitlines()[0]
    else:
        raise AssertionError("closed page title did not raise TargetClosedError")


@case
def context_close_closes_pages_before_context_close_event(page):
    browser = page.context.browser
    context = browser.new_context()
    owned_page = context.new_page()
    events = []

    owned_page.on(
        "close",
        lambda closed_page: events.append(
            (
                "page",
                closed_page is owned_page,
                browser.is_connected(),
                context.is_closed(),
                owned_page.is_closed(),
                context in browser.contexts,
                owned_page in context.pages,
            )
        ),
    )
    context.on(
        "close",
        lambda closed_context: events.append(
            (
                "context",
                closed_context is context,
                browser.is_connected(),
                context.is_closed(),
                owned_page.is_closed(),
                context in browser.contexts,
                owned_page in context.pages,
            )
        ),
    )

    try:
        context.close()
    finally:
        if not context.is_closed():
            context.close()

    assert browser.is_connected() is True
    assert context.is_closed() is True
    assert owned_page.is_closed() is True
    assert context not in browser.contexts
    assert owned_page not in context.pages
    assert events == [
        ("page", True, True, True, True, True, False),
        ("context", True, True, True, True, False, False),
    ]


@case
def worker_console_event(page):
    with page.expect_worker() as worker_info:
        page.evaluate(
            """() => {
            const source = `self.ready = true; setInterval(() => {}, 1000);`;
            const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
            window.__parityWorker = new Worker(url);
            }"""
        )
    worker = worker_info.value
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if worker.evaluate("() => self.ready === true"):
            break
        time.sleep(0.05)
    assert worker.evaluate("() => self.ready === true") is True

    with worker.expect_event("console", lambda message: message.text == "worker parity 13") as message_info:
        worker.evaluate("() => console.log('worker parity', 13)")

    message = message_info.value
    assert message.type == "log"
    assert message.page is page
    assert message.worker is worker
    with page.expect_event("console", lambda item: item.text == "worker page parity 17") as page_message_info:
        worker.evaluate("() => console.info('worker page parity', 17)")
    assert page_message_info.value.worker is worker
    with page.expect_worker() as reject_worker_info:
        page.evaluate(
            """() => {
            const source = `setInterval(() => {}, 1000);`;
            const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
            window.__parityRejectWorker = new Worker(url);
            }"""
        )
    reject_worker = reject_worker_info.value
    try:
        with reject_worker.expect_event("console", timeout=3_000):
            page.evaluate("() => window.__parityRejectWorker.terminate()")
    except Exception as exc:
        assert "Target page, context or browser has been closed" in str(exc).splitlines()[0]
    else:
        raise AssertionError("worker non-close waiter did not reject when worker closed")

    with worker.expect_event("close") as close_info:
        page.evaluate("() => window.__parityWorker.terminate()")
    assert close_info.value is worker
    try:
        with worker.expect_event("close", timeout=10):
            pass
    except Exception as exc:
        assert exc.__class__.__name__ == "TimeoutError"
        assert str(exc).splitlines()[0] == 'Timeout 10ms exceeded while waiting for event "close"'
    else:
        raise AssertionError("closed worker close event unexpectedly replayed")


@case
def worker_close_listener_receives_worker_like_playwright(page):
    with page.expect_worker() as worker_info:
        page.evaluate(
            """() => {
            const source = `setInterval(() => {}, 1000);`;
            const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
            window.__closeListenerWorker = new Worker(url);
            }"""
        )

    worker = worker_info.value
    seen = []
    worker.on("close", lambda closed_worker: seen.append(closed_worker is worker))

    page.evaluate("() => window.__closeListenerWorker.terminate()")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not seen:
        page.wait_for_timeout(50)
    assert seen == [True]


@case
def worker_unknown_event_expect_event_times_out_like_playwright(page):
    with page.expect_worker() as worker_info:
        page.evaluate(
            """() => {
            const source = `setInterval(() => {}, 1000);`;
            const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
            window.__unknownEventWorker = new Worker(url);
            }"""
        )
    worker = worker_info.value
    try:
        try:
            with worker.expect_event("unknown-event", timeout=5):
                pass
        except Exception as exc:
            assert exc.__class__.__name__ == "TimeoutError"
            assert str(exc).splitlines()[0] == 'Timeout 5ms exceeded while waiting for event "unknown-event"'
        else:
            raise AssertionError("worker unknown event unexpectedly resolved")
    finally:
        try:
            with worker.expect_event("close", timeout=3_000):
                page.evaluate("() => window.__unknownEventWorker.terminate()")
        except Exception:
            pass


@case
def public_type_shape_exports_are_importable(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    names = [
        "BrowserBindResult",
        "ChromiumBrowserContext",
        "Cookie",
        "FilePayload",
        "FloatRect",
        "Geolocation",
        "HttpCredentials",
        "Position",
        "ProxySettings",
        "ResourceTiming",
        "StorageState",
        "StorageStateCookie",
        "ViewportSize",
        "APIResponseAssertionsImpl",
        "LocatorAssertionsImpl",
        "PageAssertionsImpl",
    ]
    for name in names:
        assert hasattr(sync_api, name), name
    assert sync_api.ViewportSize.__annotations__ == {"width": int, "height": int}
    assert "server" in sync_api.ProxySettings.__annotations__


@case
def context_weberror_wraps_page_error(page):
    context = page.context
    seen = []
    context.on("weberror", lambda web_error: seen.append(web_error))

    with context.expect_event("weberror", lambda web_error: "context parity boom" in str(web_error.error)) as error_info:
        page.evaluate("() => setTimeout(() => { throw new Error('context parity boom'); }, 0)")

    web_error = error_info.value
    assert str(web_error.error) == "context parity boom"
    assert web_error.page is page
    assert any(item.page is page and "context parity boom" in str(item.error) for item in seen)


@case
def page_and_context_direct_error_waiters(page):
    page.evaluate("() => setTimeout(() => { throw new Error('direct pageerror parity'); }, 20)")
    page_error = page.wait_for_event(
        "pageerror",
        lambda error: "direct pageerror parity" in str(error),
        timeout=3_000,
    )
    assert str(page_error) == "direct pageerror parity"

    page.evaluate("() => setTimeout(() => { throw new Error('direct weberror parity'); }, 20)")
    web_error = page.context.wait_for_event(
        "weberror",
        lambda error: "direct weberror parity" in str(error.error),
        timeout=3_000,
    )
    assert str(web_error.error) == "direct weberror parity"
    assert web_error.page is page


@case
def route_fulfill(page):
    page.route(
        "**/api/parity",
        lambda route: route.fulfill(status=200, content_type="text/plain", body="route-ok"),
    )
    page.goto("http://example.test/api/parity")
    assert page.text_content("body") == "route-ok"


@case
def route_fulfill_header_defaults_match_playwright(page):
    with header_case_server() as base_url:
        specs = {
            "body_empty": {"body": ""},
            "body_text": {"body": "hello"},
            "json_false": {"json": False},
            "json_zero": {"json": 0},
            "content_type_body": {"body": "hi", "content_type": "text/plain"},
            "header_null": {"body": "hi", "headers": {"X-Keep": "yes", "X-Drop": None}},
        }
        results = {}
        for label, kwargs in specs.items():
            page.unroute("**/fulfill-*")
            page.route(f"**/fulfill-{label}", lambda route, request, kwargs=kwargs: route.fulfill(**kwargs))
            response = page.goto(f"{base_url}/fulfill-{label}")
            results[label] = {
                "status": response.status,
                "headers": response.headers,
                "text": response.text(),
            }

    assert results["body_empty"] == {"status": 200, "headers": {}, "text": ""}
    assert results["body_text"] == {
        "status": 200,
        "headers": {"content-length": "5"},
        "text": "hello",
    }
    assert results["json_false"] == {"status": 200, "headers": {"content-length": "5"}, "text": "false"}
    assert results["json_zero"] == {"status": 200, "headers": {"content-length": "1"}, "text": "0"}
    assert results["content_type_body"] == {
        "status": 200,
        "headers": {"content-length": "2", "content-type": "text/plain"},
        "text": "hi",
    }
    assert results["header_null"] == {
        "status": 200,
        "headers": {"content-length": "2", "x-keep": "yes"},
        "text": "hi",
    }


@case
def route_fulfill_path_option_validation_matches_playwright(page):
    with tempfile.TemporaryDirectory() as directory:
        body_path = Path(directory) / "route-body.txt"
        body_path.write_text("path-body", encoding="utf-8")
        errors = []

        def handler(route):
            try:
                route.fulfill(path=123)
            except Exception as exc:
                errors.append(str(exc).splitlines()[0])
            else:
                raise AssertionError("route.fulfill(path=123) unexpectedly succeeded")
            route.fulfill(path=body_path)

        page.route("**/route-fulfill-path-validation", handler)
        response = page.goto("http://example.test/route-fulfill-path-validation")

    assert response.status == 200
    assert response.header_value("content-type") == "text/plain"
    assert response.text() == "path-body"
    assert errors == [
        "argument should be a str or an os.PathLike object where __fspath__ returns a str, not 'int'"
    ]


@case
def route_fulfill_path_unknown_extension_content_type_matches_playwright(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        with tempfile.TemporaryDirectory() as directory:
            specs = [
                ("unknown", Path(directory) / "payload.unknownextensionforparity", "unknown-body"),
                ("extensionless", Path(directory) / "payload", "extensionless-body"),
            ]
            results = {}
            for label, path, body in specs:
                path.write_text(body, encoding="utf-8")
                page.unroute("**/route-fulfill-path-*")
                page.route(
                    f"**/route-fulfill-path-{label}",
                    lambda route, request, path=path: route.fulfill(path=path),
                )
                results[label] = page.evaluate(
                    """async url => {
                        const response = await fetch(url);
                        return {
                            status: response.status,
                            headers: Object.fromEntries(response.headers.entries()),
                            text: await response.text(),
                        };
                    }""",
                    f"{base_url}/route-fulfill-path-{label}",
                )

    assert results == {
        "unknown": {
            "status": 200,
            "headers": {"content-length": "12", "content-type": "application/octet-stream"},
            "text": "unknown-body",
        },
        "extensionless": {
            "status": 200,
            "headers": {"content-length": "18", "content-type": "application/octet-stream"},
            "text": "extensionless-body",
        },
    }


@case
def route_fulfill_status_code_edges_match_playwright(page):
    statuses = [-1, 0, 1, 42, 99, 199, 299, 599, 999, 1000, 65535, 65536]
    results = {}
    for status in statuses:
        page.unroute("**/route-fulfill-status-*")
        page.route(
            f"**/route-fulfill-status-{status}",
            lambda route, request, status=status: route.fulfill(status=status, body=f"body-{status}"),
        )
        response = page.goto(f"http://example.test/route-fulfill-status-{status}")
        results[status] = {
            "status": response.status,
            "status_text": response.status_text,
            "ok": response.ok,
            "text": response.text(),
        }

    assert results == {
        -1: {"status": 200, "status_text": "", "ok": True, "text": "body--1"},
        0: {"status": 200, "status_text": "OK", "ok": True, "text": "body-0"},
        1: {"status": 1, "status_text": "Unknown", "ok": False, "text": "body-1"},
        42: {"status": 42, "status_text": "Unknown", "ok": False, "text": "body-42"},
        99: {"status": 99, "status_text": "Unknown", "ok": False, "text": "body-99"},
        199: {"status": 199, "status_text": "Unknown", "ok": False, "text": "body-199"},
        299: {"status": 299, "status_text": "Unknown", "ok": True, "text": "body-299"},
        599: {"status": 599, "status_text": "Unknown", "ok": False, "text": "body-599"},
        999: {"status": 999, "status_text": "Unknown", "ok": False, "text": "body-999"},
        1000: {"status": 1000, "status_text": "Unknown", "ok": False, "text": "body-1000"},
        65535: {"status": 65535, "status_text": "Unknown", "ok": False, "text": "body-65535"},
        65536: {"status": 65536, "status_text": "Unknown", "ok": False, "text": "body-65536"},
    }


@case
def route_fulfill_204_navigation_aborts_like_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    with header_case_server() as base_url:
        page.goto(base_url)
        before = page.url
        target = f"{base_url}/fulfill-204"
        page.route("**/fulfill-204", lambda route, request: route.fulfill(status=204))
        try:
            page.goto(target, timeout=2_000)
        except sync_api.Error as exc:
            message = str(exc).splitlines()[0]
        else:
            raise AssertionError("route.fulfill(status=204) navigation unexpectedly succeeded")
        after = page.url

    assert message == f"Page.goto: net::ERR_ABORTED at {target}"
    assert after == before


@case
def goto_download_navigation_error_matches_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    with header_case_server() as base_url:
        page.goto(base_url)
        before = page.url
        try:
            page.goto(f"{base_url}/download", timeout=2_000)
        except sync_api.Error as exc:
            message = str(exc).splitlines()[0]
        else:
            raise AssertionError("download navigation unexpectedly succeeded")
        after = page.url

    assert message == "Page.goto: Download is starting"
    assert after == before


@case
def request_post_data_and_response_headers(page):
    page.route(
        "**/post-page",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=(
                "<button id='send' onclick=\"fetch('/api/post', {"
                "method: 'POST',"
                "headers: {'Content-Type': 'application/json'},"
                "body: JSON.stringify({hello: 'parity'})"
                "})\">Send</button>"
            ),
        ),
    )
    page.route(
        "**/api/post",
        lambda route: route.fulfill(status=202, content_type="application/json", body='{"accepted":true}'),
    )
    page.goto("http://example.test/post-page")

    with page.expect_request(lambda request: request.url.endswith("/api/post")) as request_info:
        with page.expect_response(lambda response: response.url.endswith("/api/post")) as response_info:
            page.click("#send")

    request = request_info.value
    response = response_info.value

    assert request.method == "POST"
    assert request.post_data == '{"hello":"parity"}'
    assert request.post_data_buffer == b'{"hello":"parity"}'
    assert request.post_data_json == {"hello": "parity"}
    assert response.status == 202
    assert response.header_value("content-type") == "application/json"
    assert any(
        header["name"].lower() == "content-type" and header["value"] == "application/json"
        for header in response.headers_array()
    )
    assert response.request.post_data_json == {"hello": "parity"}


@case
def request_binary_post_data_buffer_matches_playwright(page):
    body = b"\x00\xffA"
    routed_buffers = []

    def handler(route):
        routed_buffers.append(route.request.post_data_buffer)
        route.fulfill(status=200, body="ok")

    page.route("**/binary-post", handler)
    page.goto("about:blank")

    with page.expect_request("**/binary-post") as request_info:
        page.evaluate(
            """() => fetch('https://example.com/binary-post', {
            method: 'POST',
            mode: 'no-cors',
            headers: { 'content-type': 'application/octet-stream' },
            body: new Uint8Array([0, 255, 65]).buffer,
            }).catch(() => {})"""
        )

    request = request_info.value
    assert routed_buffers == [body]
    assert request.post_data_buffer == body
    try:
        request.post_data
    except UnicodeDecodeError as exc:
        assert "utf-8" in str(exc)
    else:
        raise AssertionError("binary request.post_data unexpectedly decoded")


@case
def request_sizes_timing_and_response_link(page):
    body = b'{"hello":"sizes"}'

    with header_case_server() as base_url:
        page.goto(base_url)

        with page.expect_request(lambda request: request.url == f"{base_url}/echo") as request_info:
            with page.expect_response(lambda response: response.url == f"{base_url}/echo") as response_info:
                page.evaluate(
                    """url => fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Test-Header': 'from-page' },
                    body: JSON.stringify({ hello: 'sizes' }),
                    })""",
                    f"{base_url}/echo",
                )

        request = request_info.value
        response = response_info.value
        response.finished()

    request_sizes = request.sizes()
    response_request_sizes = response.request.sizes()

    assert request.method == "POST"
    assert request.post_data == body.decode("utf-8")
    assert request.post_data_buffer == body
    assert request.post_data_json == {"hello": "sizes"}
    assert request_sizes["requestBodySize"] == len(body)
    assert request_sizes["requestHeadersSize"] > 0
    assert response.status == 202
    assert request is response.request
    assert request.response() is response
    assert response.request.response() is response
    assert response_request_sizes["requestBodySize"] == len(body)
    assert response_request_sizes["requestHeadersSize"] > 0
    assert response_request_sizes["responseHeadersSize"] > 0
    assert response_request_sizes["responseBodySize"] == len(response.body())
    assert response.request.timing["startTime"] > 0
    assert response.request.timing["requestStart"] >= -1


@case
def request_post_data_json_parses_form_urlencoded(page):
    page.route(
        "**/form-page",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=(
                "<button id='send' onclick=\"fetch('/api/form', {"
                "method: 'POST',"
                "body: new URLSearchParams({hello: 'form', number: '7'})"
                "})\">Send</button>"
            ),
        ),
    )
    page.route("**/api/form", lambda route: route.fulfill(status=204, body=""))
    page.goto("http://example.test/form-page")

    with page.expect_request(lambda request: request.url.endswith("/api/form")) as request_info:
        page.click("#send")

    request = request_info.value
    assert request.header_value("content-type").startswith("application/x-www-form-urlencoded")
    assert request.post_data == "hello=form&number=7"
    assert request.post_data_json == {"hello": "form", "number": "7"}


@case
def locator_content_frame_property(page):
    page.set_content("<iframe srcdoc='<main><button id=\"inner\">Inside</button></main>'></iframe>")
    frame_locator = page.locator("iframe").content_frame
    assert frame_locator is not None
    assert frame_locator.locator("#inner").inner_text() == "Inside"


@case
def element_handle_frame_relationships(page):
    grand = '<button id="deep">Deep</button>'
    child = f'<button id="inner">Inside</button><iframe id="grand" name="grand" srcdoc="{escape(grand)}"></iframe>'
    page.set_content(
        f"""
        <div id="box">Box</div>
        <iframe id="child-frame" srcdoc="{escape(child)}"></iframe>
        """
    )
    main = page.main_frame
    box = page.query_selector("#box")
    frame_element = page.query_selector("#child-frame")

    assert box is not None
    assert frame_element is not None
    assert page.main_frame is main
    assert box.owner_frame() is main
    assert frame_element.owner_frame() is main
    assert box.content_frame() is None
    child_frame = frame_element.content_frame()
    assert child_frame is not None
    assert child_frame.locator("#inner").inner_text() == "Inside"
    inner = child_frame.query_selector("#inner")
    grand_frame_element = child_frame.query_selector("#grand")
    assert inner is not None
    assert grand_frame_element is not None
    assert inner.owner_frame() is child_frame
    assert grand_frame_element.owner_frame() is child_frame
    assert inner.content_frame() is None
    grand_frame = grand_frame_element.content_frame()
    assert grand_frame is not None
    deep = grand_frame.query_selector("#deep")
    assert deep is not None
    assert deep.owner_frame() is grand_frame
    assert deep.inner_text() == "Deep"
    assert grand_frame.evaluate("() => document.querySelector('#deep').textContent") == "Deep"
    assert page.frames == [main, child_frame, grand_frame]
    assert main.child_frames == [child_frame]
    assert child_frame.parent_frame is main
    assert child_frame.child_frames == [grand_frame]
    assert grand_frame.parent_frame is child_frame
    assert grand_frame.child_frames == []
    assert page.frame(name="grand") is grand_frame
    assert child_frame.frame_element().get_attribute("id") == "child-frame"
    assert child_frame.frame_element().owner_frame() is main
    assert child_frame.frame_element().content_frame() is child_frame
    assert grand_frame.frame_element().get_attribute("id") == "grand"
    assert grand_frame.frame_element().owner_frame() is child_frame
    assert grand_frame.frame_element().content_frame() is grand_frame
    assert not child_frame.is_detached()
    assert not grand_frame.is_detached()
    child_frame.evaluate("() => document.querySelector('#grand').remove()")
    assert grand_frame.is_detached()


@case
def detached_frame_child_cache_and_errors_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")

    inner_doc = '<p id="inner-text">Inner</p>'
    outer_doc = f'<p id="outer-text">Outer</p><iframe id="inner" name="inner" srcdoc="{escape(inner_doc)}"></iframe>'
    page.set_content(f'<iframe id="outer" name="outer" srcdoc="{escape(outer_doc)}"></iframe>')

    outer = page.query_selector("#outer").content_frame()
    assert outer is not None
    inner = outer.query_selector("#inner").content_frame()
    assert inner is not None
    assert [frame.name for frame in page.frames] == ["", "outer", "inner"]
    assert outer.child_frames == [inner]
    assert inner.child_frames == []

    page.evaluate("() => document.querySelector('#outer').remove()")
    page.wait_for_timeout(100)

    assert [frame.name for frame in page.frames] == [""]
    assert page.frame(name="outer") is None
    assert page.frame(name="inner") is None
    assert outer.is_detached() is True
    assert inner.is_detached() is True
    assert outer.child_frames == [inner]
    assert inner.child_frames == []
    assert outer.parent_frame is page.main_frame
    assert inner.parent_frame is outer

    for stale_frame in (outer, inner):
        try:
            stale_frame.frame_element()
        except sync_api.Error as exc:
            assert str(exc) == "Frame.frame_element: Frame has been detached."
        else:
            raise AssertionError("detached frame_element() unexpectedly succeeded")

    for stale_frame, selector in ((outer, "#outer-text"), (inner, "#inner-text")):
        try:
            stale_frame.locator(selector).count()
        except sync_api.Error as exc:
            assert str(exc) == "Locator.count: Frame was detached"
        else:
            raise AssertionError("detached frame locator count unexpectedly succeeded")


@case
def locator_description_property(page):
    page.set_content("<p>Saved</p>")
    locator = page.get_by_text("Saved").describe("status copy")
    assert locator.description == "status copy"


@case
def locator_and_or_composition(page):
    page.set_content("<button>Save</button><button>Cancel</button>")
    assert page.get_by_role("button").and_(page.get_by_text("Save")).inner_text() == "Save"
    assert page.get_by_text("Missing").or_(page.get_by_text("Cancel")).inner_text() == "Cancel"


@case
def keyboard_type_and_backspace(page):
    page.set_content("<input id='field'>")
    page.focus("#field")
    page.keyboard.type("ab")
    page.keyboard.press("Backspace")
    assert page.input_value("#field") == "a"


def _set_keyboard_attached_input_content(page):
    page.set_content(
        """
        <style>
        #covered {
          position:absolute;
          left:20px;
          top:20px;
          width:120px;
          height:24px;
          z-index:1;
        }
        #cover {
          position:absolute;
          left:0;
          top:0;
          width:220px;
          height:80px;
          z-index:2;
        }
        </style>
        <input id="visible">
        <input id="hidden" style="display:none">
        <input id="disabled" disabled>
        <input id="readonly" readonly>
        <input id="covered">
        <div id="cover"></div>
        <script>
        window.values = () => Object.fromEntries(
          Array.from(document.querySelectorAll('input')).map(input => [input.id, input.value])
        );
        window.activeId = () => document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : null;
        window.blurActive = () => {
          if (document.activeElement && typeof document.activeElement.blur === 'function')
            document.activeElement.blur();
        };
        </script>
        """
    )


def _keyboard_attached_values(page):
    return page.evaluate("window.values()")


def _keyboard_attached_blur_active(page):
    page.evaluate("window.blurActive()")


def _keyboard_attached_dispatch(page, owner, action, selector, value):
    if owner == "locator":
        getattr(page.locator(selector), action)(value, timeout=1_000)
        return
    if owner == "page":
        getattr(page, action)(selector, value, timeout=1_000)
        return
    if owner == "frame":
        getattr(page.main_frame, action)(selector, value, timeout=1_000)
        return
    if owner == "element":
        handle = page.query_selector(selector)
        assert handle is not None
        getattr(handle, action)(value, timeout=1_000)
        return
    raise AssertionError(f"unknown keyboard owner {owner!r}")


def _keyboard_click_dispatch(page, owner, selector):
    if owner == "locator":
        page.locator(selector).click(timeout=1_000)
        return
    if owner == "page":
        page.click(selector, timeout=1_000)
        return
    if owner == "frame":
        page.main_frame.click(selector, timeout=1_000)
        return
    if owner == "element":
        handle = page.query_selector(selector)
        assert handle is not None
        handle.click(timeout=1_000)
        return
    raise AssertionError(f"unknown keyboard owner {owner!r}")


def _keyboard_dblclick_dispatch(page, owner, selector):
    if owner == "locator":
        page.locator(selector).dblclick(timeout=1_000)
        return
    if owner == "page":
        page.dblclick(selector, timeout=1_000)
        return
    if owner == "frame":
        page.main_frame.dblclick(selector, timeout=1_000)
        return
    if owner == "element":
        handle = page.query_selector(selector)
        assert handle is not None
        handle.dblclick(timeout=1_000)
        return
    raise AssertionError(f"unknown keyboard owner {owner!r}")


def _assert_keyboard_attached_input_sequence(page, owner):
    _set_keyboard_attached_input_content(page)
    _keyboard_attached_dispatch(page, owner, "press", "#visible", "a")
    assert _keyboard_attached_values(page)["visible"] == "a"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, owner, "press", "#hidden", "a")
    assert _keyboard_attached_values(page) == {
        "visible": "a",
        "hidden": "",
        "disabled": "",
        "readonly": "",
        "covered": "",
    }
    assert page.evaluate("window.activeId()") != "hidden"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, owner, "press", "#disabled", "a")
    assert _keyboard_attached_values(page)["disabled"] == ""
    assert page.evaluate("window.activeId()") != "disabled"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, owner, "press", "#readonly", "a")
    assert _keyboard_attached_values(page)["readonly"] == ""
    assert page.evaluate("window.activeId()") == "readonly"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, owner, "press", "#covered", "a")
    assert _keyboard_attached_values(page)["covered"] == "a"

    _set_keyboard_attached_input_content(page)
    _keyboard_attached_dispatch(page, owner, "type", "#visible", "ab")
    assert _keyboard_attached_values(page)["visible"] == "ab"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, owner, "type", "#hidden", "ab")
    assert _keyboard_attached_values(page) == {
        "visible": "ab",
        "hidden": "",
        "disabled": "",
        "readonly": "",
        "covered": "",
    }
    assert page.evaluate("window.activeId()") != "hidden"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, owner, "type", "#disabled", "ab")
    assert _keyboard_attached_values(page)["disabled"] == ""
    assert page.evaluate("window.activeId()") != "disabled"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, owner, "type", "#readonly", "ab")
    assert _keyboard_attached_values(page)["readonly"] == ""
    assert page.evaluate("window.activeId()") == "readonly"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, owner, "type", "#covered", "ab")
    assert _keyboard_attached_values(page)["covered"] == "ab"


def _assert_locator_press_sequentially_attached_input_sequence(page):
    _set_keyboard_attached_input_content(page)
    _keyboard_attached_dispatch(page, "locator", "press_sequentially", "#visible", "ab")
    assert _keyboard_attached_values(page)["visible"] == "ab"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, "locator", "press_sequentially", "#hidden", "ab")
    assert _keyboard_attached_values(page) == {
        "visible": "ab",
        "hidden": "",
        "disabled": "",
        "readonly": "",
        "covered": "",
    }
    assert page.evaluate("window.activeId()") != "hidden"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, "locator", "press_sequentially", "#disabled", "ab")
    assert _keyboard_attached_values(page)["disabled"] == ""
    assert page.evaluate("window.activeId()") != "disabled"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, "locator", "press_sequentially", "#readonly", "ab")
    assert _keyboard_attached_values(page)["readonly"] == ""
    assert page.evaluate("window.activeId()") == "readonly"

    _keyboard_attached_blur_active(page)
    _keyboard_attached_dispatch(page, "locator", "press_sequentially", "#covered", "ab")
    assert _keyboard_attached_values(page)["covered"] == "ab"


@case
def locator_type_press_attached_input_behavior_matches_playwright(page):
    _assert_keyboard_attached_input_sequence(page, "locator")


@case
def selector_and_element_type_press_attached_input_behavior_matches_playwright(page):
    for owner in ["page", "frame", "element"]:
        _assert_keyboard_attached_input_sequence(page, owner)


@case
def locator_press_sequentially_attached_input_behavior_matches_playwright(page):
    _assert_locator_press_sequentially_attached_input_sequence(page)


@case
def click_editable_controls_places_caret_for_keyboard_type_like_playwright(page):
    for owner in ["locator", "page", "frame", "element"]:
        for selector, expected in [("#field", "xab"), ("#area", "xab")]:
            page.set_content('<input id="field" value="x"><textarea id="area">x</textarea>')
            _keyboard_click_dispatch(page, owner, selector)
            page.keyboard.type("ab")
            assert page.input_value(selector) == expected
            assert page.evaluate("selector => document.querySelector(selector).selectionStart", selector) == len(expected)


@case
def dblclick_dispatch_and_editable_selection_match_playwright(page):
    page.set_content(
        """
        <button id="target">Target</button>
        <script>
        window.events = [];
        const target = document.querySelector('#target');
        for (const type of ['mouseover', 'mouseenter', 'mousemove', 'mousedown', 'mouseup', 'click', 'dblclick']) {
          target.addEventListener(type, event => window.events.push(`${type}:${event.detail}`));
        }
        </script>
        """
    )
    page.locator("#target").dblclick(timeout=1_000)
    assert page.evaluate("window.events") == [
        "mouseover:0",
        "mouseenter:0",
        "mousemove:0",
        "mousedown:1",
        "mouseup:1",
        "click:1",
        "mousedown:2",
        "mouseup:2",
        "click:2",
        "dblclick:2",
    ]

    content = """
        <input id="field" value="alpha beta gamma" style="font: 20px monospace; width: 260px; padding: 0; margin: 40px;">
        <textarea id="area" style="font: 20px monospace; width: 260px; height: 40px; padding: 0; margin: 40px;">alpha beta gamma</textarea>
        <div id="edit" contenteditable style="font: 20px monospace; width: 260px; padding: 0; margin: 40px;">alpha beta gamma</div>
    """
    for owner in ["locator", "page", "frame", "element"]:
        for selector in ["#field", "#area"]:
            page.set_content(content)
            _keyboard_dblclick_dispatch(page, owner, selector)
            page.keyboard.type("X")
            assert page.input_value(selector) == "alpha beta X"
            assert page.evaluate("selector => document.querySelector(selector).selectionStart", selector) == len("alpha beta X")

        page.set_content(content)
        _keyboard_dblclick_dispatch(page, owner, "#edit")
        page.keyboard.type("X")
        assert page.locator("#edit").inner_text() == "alpha beta X"


@case
def reload_wait_until_domcontentloaded(page):
    with slow_body_server() as base_url:
        page.goto(f"{base_url}/slow-body")
        started = time.monotonic()
        response = page.reload(wait_until="domcontentloaded")
        elapsed = time.monotonic() - started

    assert response is not None
    assert response.status == 200
    assert response.url == f"{base_url}/slow-body"
    assert elapsed >= 0.15
    assert page.title() == "Slow Body"
    assert page.evaluate("() => window.__slowBodyParsed") is True


@case
def wait_for_url_wait_until_domcontentloaded(page):
    with slow_body_server() as base_url:
        target = f"{base_url}/slow-body"
        page.set_content(f"<a id='nav' href='{target}'>Navigate</a>")
        started = time.monotonic()
        page.click("#nav")
        page.wait_for_url("**/slow-body", wait_until="domcontentloaded")
        page.wait_for_url(target, wait_until="domcontentloaded")
        try:
            page.wait_for_url("slow-body", timeout=100)
        except Exception:
            pass
        else:
            raise AssertionError("plain string wait_for_url matched a substring URL")
        elapsed = time.monotonic() - started

    assert page.url == target
    assert elapsed >= 0.15
    assert page.title() == "Slow Body"
    assert page.evaluate("() => window.__slowBodyParsed") is True


@case
def wait_for_url_argument_validation_matches_playwright(page):
    page.goto(data_url("<title>URL Validation</title><main>Ready</main>"))

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(
        lambda: page.wait_for_url(None, timeout=50),
        "Frame.wait_for_url() missing 1 required positional argument: 'url'",
        TypeError,
    )
    expect_error(lambda: page.wait_for_url(123, timeout=50), "'int' object is not callable", TypeError)
    expect_error(lambda: page.wait_for_url(True, timeout=50), "'bool' object is not callable", TypeError)
    expect_error(lambda: page.wait_for_url(lambda url: False, timeout=50), "Timeout 50ms exceeded.")
    expect_error(
        lambda: page.wait_for_url(lambda url: (_ for _ in ()).throw(ValueError("bad predicate")), timeout=50),
        "bad predicate",
        ValueError,
    )

    assert page.main_frame.wait_for_url(None, timeout=50) is None
    expect_error(lambda: page.main_frame.wait_for_url(123, timeout=50), "'int' object is not callable", TypeError)
    expect_error(lambda: page.main_frame.wait_for_url(lambda url: False, timeout=50), "Timeout 50ms exceeded.")


@case
def navigation_argument_validation_matches_playwright(page):
    page.goto("about:blank")

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(
        lambda: page.goto(None, timeout=50),
        "Frame.goto() missing 1 required positional argument: 'url'",
        TypeError,
    )
    expect_error(lambda: page.goto(123, timeout=50), "Page.goto: url: expected string, got number")
    expect_error(lambda: page.goto(True, timeout=50), "Page.goto: url: expected string, got boolean")
    expect_error(lambda: page.main_frame.goto(None, timeout=50), "Frame.goto: url: expected string, got undefined")
    expect_error(lambda: page.main_frame.goto(123, timeout=50), "Frame.goto: url: expected string, got number")

    expect_error(lambda: page.goto("about:blank", referer=123, timeout=50), "Page.goto: referer: expected string, got number")
    expect_error(
        lambda: page.main_frame.goto("about:blank", referer=123, timeout=50),
        "Frame.goto: referer: expected string, got number",
    )
    expect_error(lambda: page.goto("about:blank", timeout="bad"), "Page.goto: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.goto("about:blank", timeout="bad"), "Frame.goto: timeout: expected float, got string")
    expect_error(lambda: page.reload(timeout="bad"), "Page.reload: timeout: expected float, got string")
    expect_error(lambda: page.go_back(timeout="bad"), "Page.go_back: timeout: expected float, got string")
    expect_error(lambda: page.go_forward(timeout="bad"), "Page.go_forward: timeout: expected float, got string")


@case
def default_timeout_setters_defer_validation(page):
    page.set_content("<button id='ready'>Ready</button>")

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    page.set_default_timeout(None)
    assert page.wait_for_selector("#ready").text_content() == "Ready"

    try:
        page.set_default_timeout("bad")
        expect_error(
            lambda: page.wait_for_selector("#missing"),
            "Page.wait_for_selector: timeout: expected float, got string",
        )
        page.set_default_timeout(True)
        expect_error(
            lambda: page.locator("#missing").wait_for(),
            "Locator.wait_for: timeout: expected float, got boolean",
        )
    finally:
        page.set_default_timeout(30_000)

    try:
        page.set_default_navigation_timeout("bad")
        expect_error(lambda: page.goto("about:blank"), "Page.goto: timeout: expected float, got string")
    finally:
        page.set_default_navigation_timeout(None)

    context = page.context.browser.new_context()
    context_page = context.new_page()
    try:
        context_page.set_content("<main></main>")
        context.set_default_timeout("bad")
        expect_error(
            lambda: context_page.wait_for_selector("#missing"),
            "Page.wait_for_selector: timeout: expected float, got string",
        )
    finally:
        context.set_default_timeout(30_000)
        context.close()


@case
def goto_invalid_url_errors_match_playwright(page):
    page.goto("about:blank")

    def expect_error(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    invalid_urls = ["foo", "http://", "http://[::1"]
    for target in invalid_urls:
        expect_error(
            lambda target=target: page.goto(target, timeout=500),
            "Page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL",
        )
        expect_error(
            lambda target=target: page.main_frame.goto(target, timeout=500),
            "Frame.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL",
        )


@case
def goto_timeout_message_matches_playwright(page):
    with header_case_server() as base_url:
        def expect_timeout(operation, expected_message):
            try:
                operation()
            except Exception as exc:
                assert str(exc).splitlines()[0] == expected_message
            else:
                raise AssertionError(f"expected {expected_message!r}")

        def cleanup(pattern, handler, expected_message):
            try:
                page.unroute(pattern, handler)
            except Exception as exc:
                assert str(exc).splitlines()[0] == expected_message
            else:
                raise AssertionError(f"expected cleanup error {expected_message!r}")

        def page_handler(route):
            raise RuntimeError("page goto timeout boom")

        page.route("**/goto-timeout-page", page_handler)
        expect_timeout(
            lambda: page.goto(f"{base_url}/goto-timeout-page", timeout=50),
            "Page.goto: Timeout 50ms exceeded.",
        )
        cleanup("**/goto-timeout-page", page_handler, "Page.unroute: page goto timeout boom")

        def frame_handler(route):
            raise RuntimeError("frame goto timeout boom")

        page.route("**/goto-timeout-frame", frame_handler)
        expect_timeout(
            lambda: page.main_frame.goto(f"{base_url}/goto-timeout-frame", timeout=50),
            "Frame.goto: Timeout 50ms exceeded.",
        )
        cleanup("**/goto-timeout-frame", frame_handler, "Page.unroute: frame goto timeout boom")


@case
def reload_and_history_timeout_messages_match_playwright(page):
    with header_case_server() as base_url:
        def expect_timeout(operation, expected_message):
            try:
                operation()
            except Exception as exc:
                assert str(exc).splitlines()[0] == expected_message
            else:
                raise AssertionError(f"expected {expected_message!r}")

        def cleanup(pattern, handler, expected_message):
            try:
                page.unroute(pattern, handler)
            except Exception as exc:
                assert str(exc).splitlines()[0] == expected_message

        page.goto(f"{base_url}/headers?reload-timeout")

        def reload_handler(route):
            raise RuntimeError("reload timeout boom")

        page.route("**/headers?reload-timeout", reload_handler)
        expect_timeout(lambda: page.reload(timeout=50), "Page.reload: Timeout 50ms exceeded.")
        cleanup("**/headers?reload-timeout", reload_handler, "Page.unroute: reload timeout boom")

        page.goto(f"{base_url}/headers?back-timeout")
        page.goto(f"{base_url}/headers?middle")

        def back_handler(route):
            raise RuntimeError("back timeout boom")

        page.route("**/headers?back-timeout", back_handler)
        expect_timeout(lambda: page.go_back(timeout=50), "Page.go_back: Timeout 50ms exceeded.")
        cleanup("**/headers?back-timeout", back_handler, "Page.unroute: back timeout boom")

        page.goto(f"{base_url}/headers?forward-start")
        page.goto(f"{base_url}/headers?forward-timeout")
        page.go_back()

        def forward_handler(route):
            raise RuntimeError("forward timeout boom")

        page.route("**/headers?forward-timeout", forward_handler)
        expect_timeout(lambda: page.go_forward(timeout=50), "Page.go_forward: Timeout 50ms exceeded.")
        cleanup("**/headers?forward-timeout", forward_handler, "Page.unroute: forward timeout boom")


@case
def set_content_and_load_state_timeout_messages_match_playwright(page):
    def expect_timeout(operation, expected_message):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    def slow_asset(route):
        time.sleep(0.3)
        try:
            route.abort()
        except Exception:
            pass

    page.route("**/set-content-slow.png", slow_asset)
    expect_timeout(
        lambda: page.set_content(
            "<img src='http://example.test/set-content-slow.png'>",
            wait_until="load",
            timeout=50,
        ),
        "Page.set_content: Timeout 50ms exceeded.",
    )
    page.unroute("**/set-content-slow.png", slow_asset)

    page.route("**/frame-set-content-slow.png", slow_asset)
    expect_timeout(
        lambda: page.main_frame.set_content(
            "<img src='http://example.test/frame-set-content-slow.png'>",
            wait_until="load",
            timeout=50,
        ),
        "Frame.set_content: Timeout 50ms exceeded.",
    )
    page.unroute("**/frame-set-content-slow.png", slow_asset)

    expect_timeout(
        lambda: page.wait_for_load_state("networkidle", timeout=50),
        "Timeout 50ms exceeded.",
    )
    expect_timeout(
        lambda: page.main_frame.wait_for_load_state("networkidle", timeout=50),
        "Timeout 50ms exceeded.",
    )


@case
def goto_special_scheme_responses_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")

    assert page.goto("about:blank") is None
    assert page.url == "about:blank"

    data_target = data_url("<title>Special Scheme</title><main>data</main>")
    assert page.goto(data_target) is None
    assert page.url == data_target
    assert page.title() == "Special Scheme"

    before = page.url
    javascript_target = "javascript:document.title='changed'"
    try:
        page.goto(javascript_target, timeout=2_000)
    except sync_api.Error as exc:
        message = str(exc).splitlines()[0]
    else:
        raise AssertionError("javascript: navigation unexpectedly succeeded")

    assert message == f"Page.goto: net::ERR_ABORTED at {javascript_target}"
    assert page.url == before
    assert page.title() == ""
    assert page.evaluate("() => document.body.innerText") == "changed"


@case
def goto_same_document_hash_returns_none(page):
    with header_case_server() as base_url:
        response = page.goto(base_url)
        assert response is not None
        target = f"{base_url}/#target"

        assert page.goto(target, timeout=2_000) is None
        assert page.url == target
        assert page.goto(target, timeout=2_000) is None
        assert page.url == target

        other_target = f"{base_url}/#other"
        assert page.goto(other_target, timeout=2_000) is None
        assert page.url == other_target


@case
def set_content_argument_validation_matches_playwright(page):
    page.goto("about:blank")

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(
        lambda: page.set_content(None, timeout=50),
        "Frame.set_content() missing 1 required positional argument: 'html'",
        TypeError,
    )
    expect_error(lambda: page.set_content(123, timeout=50), "Page.set_content: html: expected string, got number")
    expect_error(lambda: page.set_content("<main>x</main>", timeout="bad"), "Page.set_content: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.set_content(None, timeout=50), "Frame.set_content: html: expected string, got undefined")
    expect_error(lambda: page.main_frame.set_content(123, timeout=50), "Frame.set_content: html: expected string, got number")
    expect_error(
        lambda: page.main_frame.set_content("<main>x</main>", timeout="bad"),
        "Frame.set_content: timeout: expected float, got string",
    )


@case
def select_option_argument_validation_matches_playwright(page):
    page.set_content(
        """
        <select id="plan">
          <option value="free">Free</option>
          <option value="pro">Pro</option>
        </select>
        <select id="multi" multiple>
          <option value="a">A</option>
          <option value="b">B</option>
        </select>
        """
    )

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(lambda: page.select_option("#plan", index="bad", timeout=50), "Page.select_option: options[0].index: expected integer, got string")
    expect_error(lambda: page.select_option("#plan", "free", timeout="bad"), "Page.select_option: timeout: expected float, got string")
    expect_error(
        lambda: page.select_option("#multi", value=["a", 123], timeout=50),
        "Page.select_option: options[1].valueOrLabel: expected string, got number",
    )
    expect_error(
        lambda: page.select_option("#multi", label=["A", 123], timeout=50),
        "Page.select_option: options[1].label: expected string, got number",
    )
    expect_error(
        lambda: page.main_frame.select_option("#plan", index="bad", timeout=50),
        "Frame.select_option: options[0].index: expected integer, got string",
    )
    expect_error(
        lambda: page.main_frame.select_option("#plan", "free", timeout="bad"),
        "Frame.select_option: timeout: expected float, got string",
    )
    expect_error(
        lambda: page.locator("#plan").select_option(index="bad", timeout=50),
        "Locator.select_option: options[0].index: expected integer, got string",
    )
    expect_error(
        lambda: page.locator("#plan").select_option("free", timeout="bad"),
        "Locator.select_option: timeout: expected float, got string",
    )


@case
def locator_timeout_argument_validation_matches_playwright(page):
    page.set_content("<button id='go'>Go</button><input id='field'>")

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(lambda: page.locator("#go").click(timeout="bad"), "Locator.click: timeout: expected float, got string")
    expect_error(lambda: page.locator("#field").fill("x", timeout="bad"), "Locator.fill: timeout: expected float, got string")
    expect_error(
        lambda: page.locator("#go").get_attribute("id", timeout="bad"),
        "Locator.get_attribute: timeout: expected float, got string",
    )
    expect_error(
        lambda: page.locator("#go").text_content(timeout="bad"),
        "Locator.text_content: timeout: expected float, got string",
    )
    expect_error(lambda: page.locator("#go").wait_for(timeout="bad"), "Locator.wait_for: timeout: expected float, got string")
    expect_error(lambda: page.query_selector("#go").click(timeout="bad"), "ElementHandle.click: timeout: expected float, got string")
    expect_error(lambda: page.query_selector("#field").fill("x", timeout="bad"), "ElementHandle.fill: timeout: expected float, got string")


@case
def element_handle_timeout_argument_validation_matches_playwright(page):
    page.set_content(
        """
        <button id="go">Go</button>
        <input id="field">
        <input id="check" type="checkbox">
        <select id="sel"><option value="a">A</option></select>
        <input id="file" type="file">
        <div id="child"><span id="inner">Inner</span></div>
        """
    )
    handles = {
        "go": page.query_selector("#go"),
        "field": page.query_selector("#field"),
        "check": page.query_selector("#check"),
        "sel": page.query_selector("#sel"),
        "file": page.query_selector("#file"),
        "child": page.query_selector("#child"),
    }
    assert all(handles.values())

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    try:
        go = handles["go"]
        field = handles["field"]
        check = handles["check"]
        sel = handles["sel"]
        file = handles["file"]
        child = handles["child"]
        expect_error(lambda: go.dblclick(timeout="bad"), "ElementHandle.dblclick: timeout: expected float, got string")
        expect_error(lambda: go.hover(timeout="bad"), "ElementHandle.hover: timeout: expected float, got string")
        expect_error(lambda: field.type("x", timeout="bad"), "ElementHandle.type: timeout: expected float, got string")
        expect_error(lambda: field.press("A", timeout="bad"), "ElementHandle.press: timeout: expected float, got string")
        expect_error(lambda: go.tap(timeout="bad"), "ElementHandle.tap: timeout: expected float, got string")
        expect_error(lambda: child.wait_for_selector("#inner", timeout="bad"), "ElementHandle.wait_for_selector: timeout: expected float, got string")
        expect_error(lambda: check.check(timeout="bad"), "ElementHandle.check: timeout: expected float, got string")
        expect_error(lambda: check.uncheck(timeout="bad"), "ElementHandle.uncheck: timeout: expected float, got string")
        expect_error(lambda: check.set_checked(True, timeout="bad"), "ElementHandle.set_checked: timeout: expected float, got string")
        expect_error(lambda: sel.select_option("a", timeout="bad"), "ElementHandle.select_option: timeout: expected float, got string")
        expect_error(lambda: file.set_input_files([], timeout="bad"), "ElementHandle.set_input_files: timeout: expected float, got string")
        assert field.input_value(timeout="bad") == ""
        expect_error(
            lambda: go.scroll_into_view_if_needed(timeout="bad"),
            "ElementHandle.scroll_into_view_if_needed: timeout: expected float, got string",
        )
        expect_error(lambda: field.select_text(timeout="bad"), "ElementHandle.select_text: timeout: expected float, got string")
        expect_error(
            lambda: go.wait_for_element_state("visible", timeout="bad"),
            "ElementHandle.wait_for_element_state: timeout: expected float, got string",
        )
    finally:
        for handle in handles.values():
            if handle is not None:
                handle.dispose()


@case
def action_option_validation_matches_playwright(page):
    page.set_content("<button id='go'>Go</button>")
    go = page.query_selector("#go")
    assert go is not None

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    try:
        expect_error(lambda: page.click("#go", button="bad"), "Page.click: button: expected one of (left|right|middle)")
        expect_error(lambda: page.main_frame.click("#go", button="bad"), "Frame.click: button: expected one of (left|right|middle)")
        expect_error(lambda: page.locator("#go").click(button="bad"), "Locator.click: button: expected one of (left|right|middle)")
        expect_error(lambda: go.click(button="bad"), "ElementHandle.click: button: expected one of (left|right|middle)")
        expect_error(lambda: page.click("#go", click_count="bad"), "Page.click: click_count: expected integer, got string")
        expect_error(lambda: page.locator("#go").click(click_count="bad"), "Locator.click: click_count: expected integer, got string")
        expect_error(lambda: go.click(click_count="bad"), "ElementHandle.click: click_count: expected integer, got string")
        expect_error(lambda: page.click("#go", delay=True), "Page.click: delay: expected float, got boolean")
        expect_error(lambda: page.locator("#go").click(delay=True), "Locator.click: delay: expected float, got boolean")
        expect_error(lambda: go.click(delay=True), "ElementHandle.click: delay: expected float, got boolean")
        expect_error(lambda: page.dblclick("#go", button="bad"), "Page.dblclick: button: expected one of (left|right|middle)")
        expect_error(lambda: page.locator("#go").dblclick(button="bad"), "Locator.dblclick: button: expected one of (left|right|middle)")
        expect_error(lambda: go.dblclick(button="bad"), "ElementHandle.dblclick: button: expected one of (left|right|middle)")
        expect_error(lambda: page.dblclick("#go", delay=True), "Page.dblclick: delay: expected float, got boolean")
        expect_error(lambda: page.locator("#go").dblclick(delay=True), "Locator.dblclick: delay: expected float, got boolean")
        expect_error(lambda: go.dblclick(delay=True), "ElementHandle.dblclick: delay: expected float, got boolean")
        expect_error(
            lambda: page.click("#go", modifiers=["Bad"]),
            "Page.click: modifiers[0]: expected one of (Alt|Control|ControlOrMeta|Meta|Shift)",
        )
        expect_error(
            lambda: page.locator("#go").click(modifiers=["Bad"]),
            "Locator.click: modifiers[0]: expected one of (Alt|Control|ControlOrMeta|Meta|Shift)",
        )
        expect_error(
            lambda: go.click(modifiers=["Bad"]),
            "ElementHandle.click: modifiers[0]: expected one of (Alt|Control|ControlOrMeta|Meta|Shift)",
        )
        expect_error(lambda: page.click("#go", position={"x": "bad", "y": 1}), "Page.click: position.x: expected float, got string")
        expect_error(
            lambda: page.locator("#go").click(position={"x": "bad", "y": 1}),
            "Locator.click: position.x: expected float, got string",
        )
        expect_error(lambda: go.click(position={"x": "bad", "y": 1}), "ElementHandle.click: position.x: expected float, got string")
        expect_error(
            lambda: page.hover("#go", modifiers=["Bad"]),
            "Page.hover: modifiers[0]: expected one of (Alt|Control|ControlOrMeta|Meta|Shift)",
        )
        expect_error(
            lambda: page.locator("#go").hover(position={"x": "bad", "y": 1}),
            "Locator.hover: position.x: expected float, got string",
        )
        expect_error(
            lambda: go.hover(modifiers=["Bad"]),
            "ElementHandle.hover: modifiers[0]: expected one of (Alt|Control|ControlOrMeta|Meta|Shift)",
        )
        expect_error(
            lambda: page.tap("#go", modifiers=["Bad"]),
            "Page.tap: modifiers[0]: expected one of (Alt|Control|ControlOrMeta|Meta|Shift)",
        )
        expect_error(
            lambda: page.locator("#go").tap(position={"x": "bad", "y": 1}),
            "Locator.tap: position.x: expected float, got string",
        )
        expect_error(
            lambda: go.tap(modifiers=["Bad"]),
            "ElementHandle.tap: modifiers[0]: expected one of (Alt|Control|ControlOrMeta|Meta|Shift)",
        )
    finally:
        go.dispose()


@case
def action_boolean_and_drag_option_validation_matches_playwright(page):
    page.set_content(
        """
        <button id="go">Go</button>
        <input id="check" type="checkbox">
        <div id="drag" draggable="true">Drag</div>
        <div id="drop">Drop</div>
        """
    )
    handles = {
        "go": page.query_selector("#go"),
        "check": page.query_selector("#check"),
    }
    assert all(handles.values())

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    try:
        go = handles["go"]
        check = handles["check"]
        expect_error(lambda: page.click("#go", force="bad"), "Page.click: force: expected boolean, got string")
        expect_error(lambda: page.main_frame.click("#go", force="bad"), "Frame.click: force: expected boolean, got string")
        expect_error(lambda: page.locator("#go").click(force="bad"), "Locator.click: force: expected boolean, got string")
        expect_error(lambda: go.click(force="bad"), "ElementHandle.click: force: expected boolean, got string")
        expect_error(lambda: page.click("#go", trial="bad"), "Page.click: trial: expected boolean, got string")
        expect_error(lambda: page.locator("#go").click(trial="bad"), "Locator.click: trial: expected boolean, got string")
        expect_error(lambda: go.click(trial="bad"), "ElementHandle.click: trial: expected boolean, got string")
        expect_error(lambda: page.check("#check", force="bad"), "Page.check: force: expected boolean, got string")
        expect_error(lambda: page.main_frame.check("#check", force="bad"), "Frame.check: force: expected boolean, got string")
        expect_error(lambda: page.locator("#check").check(force="bad"), "Locator.check: force: expected boolean, got string")
        expect_error(lambda: check.check(force="bad"), "ElementHandle.check: force: expected boolean, got string")
        expect_error(lambda: page.check("#check", trial="bad"), "Page.check: trial: expected boolean, got string")
        expect_error(lambda: page.locator("#check").check(trial="bad"), "Locator.check: trial: expected boolean, got string")
        expect_error(lambda: check.check(trial="bad"), "ElementHandle.check: trial: expected boolean, got string")
        expect_error(lambda: page.check("#check", position={"x": "bad", "y": 1}), "Page.check: position.x: expected float, got string")
        expect_error(
            lambda: page.locator("#check").check(position={"x": "bad", "y": 1}),
            "Locator.check: position.x: expected float, got string",
        )
        expect_error(lambda: check.check(position={"x": "bad", "y": 1}), "ElementHandle.check: position.x: expected float, got string")
        expect_error(lambda: page.drag_and_drop("#drag", "#drop", force="bad"), "Page.drag_and_drop: force: expected boolean, got string")
        expect_error(
            lambda: page.main_frame.drag_and_drop("#drag", "#drop", force="bad"),
            "Frame.drag_and_drop: force: expected boolean, got string",
        )
        expect_error(
            lambda: page.locator("#drag").drag_to(page.locator("#drop"), force="bad"),
            "Locator.drag_to: force: expected boolean, got string",
        )
        expect_error(lambda: page.drag_and_drop("#drag", "#drop", trial="bad"), "Page.drag_and_drop: trial: expected boolean, got string")
        expect_error(
            lambda: page.locator("#drag").drag_to(page.locator("#drop"), trial="bad"),
            "Locator.drag_to: trial: expected boolean, got string",
        )
        expect_error(
            lambda: page.drag_and_drop("#drag", "#drop", source_position={"x": "bad", "y": 1}),
            "Page.drag_and_drop: sourcePosition.x: expected float, got string",
        )
        expect_error(
            lambda: page.locator("#drag").drag_to(page.locator("#drop"), source_position={"x": "bad", "y": 1}),
            "Locator.drag_to: sourcePosition.x: expected float, got string",
        )
        expect_error(
            lambda: page.drag_and_drop("#drag", "#drop", target_position={"x": "bad", "y": 1}),
            "Page.drag_and_drop: targetPosition.x: expected float, got string",
        )
        expect_error(
            lambda: page.locator("#drag").drag_to(page.locator("#drop"), target_position={"x": "bad", "y": 1}),
            "Locator.drag_to: targetPosition.x: expected float, got string",
        )
        expect_error(lambda: page.drag_and_drop("#drag", "#drop", steps="bad"), "Page.drag_and_drop: steps: expected integer, got string")
        expect_error(
            lambda: page.locator("#drag").drag_to(page.locator("#drop"), steps="bad"),
            "Locator.drag_to: steps: expected integer, got string",
        )
    finally:
        for handle in handles.values():
            if handle is not None:
                handle.dispose()


@case
def selector_strict_and_no_wait_after_validation_matches_playwright(page):
    page.set_content(
        """
        <button id="go">Go</button><button id="other">Other</button>
        <input id="field"><input id="check" type="checkbox">
        <select id="choice"><option value="one">One</option></select>
        <input id="file" type="file">
        <div id="root"><span>one</span><span>two</span></div>
        """
    )
    handles = {
        "go": page.query_selector("#go"),
        "field": page.query_selector("#field"),
        "root": page.query_selector("#root"),
    }
    assert all(handles.values())

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    try:
        go = handles["go"]
        field = handles["field"]
        root = handles["root"]
        expect_error(lambda: page.click("button", strict="bad"), "Page.click: strict: expected boolean, got string")
        expect_error(lambda: page.main_frame.click("button", strict="bad"), "Frame.click: strict: expected boolean, got string")
        expect_error(lambda: page.query_selector("button", strict="bad"), "Page.query_selector: strict: expected boolean, got string")
        expect_error(lambda: page.main_frame.query_selector("button", strict="bad"), "Frame.query_selector: strict: expected boolean, got string")
        expect_error(lambda: page.wait_for_selector("button", strict="bad"), "Page.wait_for_selector: strict: expected boolean, got string")
        expect_error(lambda: page.main_frame.wait_for_selector("button", strict="bad"), "Frame.wait_for_selector: strict: expected boolean, got string")
        expect_error(lambda: page.is_visible("button", strict="bad"), "Page.is_visible: strict: expected boolean, got string")
        expect_error(lambda: page.dispatch_event("button", "click", strict="bad"), "Page.dispatch_event: strict: expected boolean, got string")
        expect_error(lambda: page.eval_on_selector("button", "(el) => el.id", strict="bad"), "Page.eval_on_selector: strict: expected boolean, got string")
        expect_error(lambda: page.get_attribute("button", "id", strict="bad"), "Page.get_attribute: strict: expected boolean, got string")
        expect_error(lambda: page.inner_text("button", strict="bad"), "Page.inner_text: strict: expected boolean, got string")
        expect_error(lambda: page.text_content("button", strict="bad"), "Page.text_content: strict: expected boolean, got string")
        expect_error(lambda: page.input_value("input", strict="bad"), "Page.input_value: strict: expected boolean, got string")
        expect_error(lambda: page.fill("input", "x", strict="bad"), "Page.fill: strict: expected boolean, got string")
        expect_error(lambda: page.press("input", "A", strict="bad"), "Page.press: strict: expected boolean, got string")
        expect_error(lambda: page.select_option("select", "one", strict="bad"), "Page.select_option: strict: expected boolean, got string")
        expect_error(lambda: page.set_input_files("input[type=file]", [], strict="bad"), "Page.set_input_files: strict: expected boolean, got string")
        expect_error(lambda: page.set_checked("input[type=checkbox]", True, strict="bad"), "Page.set_checked: strict: expected boolean, got string")
        expect_error(lambda: root.wait_for_selector("span", strict="bad"), "ElementHandle.wait_for_selector: strict: expected boolean, got string")

        expect_error(lambda: page.click("#go", no_wait_after="bad"), "Page.click: no_wait_after: expected boolean, got string")
        expect_error(lambda: page.main_frame.click("#go", no_wait_after="bad"), "Frame.click: no_wait_after: expected boolean, got string")
        expect_error(lambda: page.locator("#go").click(no_wait_after="bad"), "Locator.click: no_wait_after: expected boolean, got string")
        expect_error(lambda: go.click(no_wait_after="bad"), "ElementHandle.click: no_wait_after: expected boolean, got string")
        expect_error(lambda: page.press("#field", "A", no_wait_after="bad"), "Page.press: no_wait_after: expected boolean, got string")
        expect_error(lambda: page.main_frame.press("#field", "A", no_wait_after="bad"), "Frame.press: no_wait_after: expected boolean, got string")
        expect_error(lambda: page.locator("#field").press("A", no_wait_after="bad"), "Locator.press: no_wait_after: expected boolean, got string")
        expect_error(lambda: field.press("A", no_wait_after="bad"), "ElementHandle.press: no_wait_after: expected boolean, got string")

        page.fill("#field", "ok", no_wait_after="bad")
        page.type("#field", "!", no_wait_after="bad")
        page.check("#check", no_wait_after="bad")
        assert page.is_checked("#check")
        assert page.select_option("#choice", "one", no_wait_after="bad") == ["one"]
        page.set_input_files("#file", [], no_wait_after="bad")
    finally:
        for handle in handles.values():
            if handle is not None:
                handle.dispose()


@case
def locator_state_timeout_semantics_match_playwright(page):
    page.set_content(
        "<button id='go'>Go</button><input id='check' type='checkbox'>"
        "<input id='field' value='x'><main id='plain'>Plain</main>"
    )

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    assert page.locator("#go").is_visible(timeout="bad") is True
    assert page.locator("#go").is_hidden(timeout="bad") is False
    expect_error(lambda: page.locator("#go").is_enabled(timeout="bad"), "Locator.is_enabled: timeout: expected float, got string")
    expect_error(lambda: page.locator("#go").is_disabled(timeout="bad"), "Locator.is_disabled: timeout: expected float, got string")
    expect_error(lambda: page.locator("#check").is_checked(timeout="bad"), "Locator.is_checked: timeout: expected float, got string")
    expect_error(lambda: page.locator("#field").is_editable(timeout="bad"), "Locator.is_editable: timeout: expected float, got string")

    missing_timeout = 10
    expect_error(lambda: page.locator("#missing").is_enabled(timeout=missing_timeout), "Locator.is_enabled: Timeout 10ms exceeded.")
    expect_error(lambda: page.locator("#missing").is_disabled(timeout=missing_timeout), "Locator.is_disabled: Timeout 10ms exceeded.")
    expect_error(lambda: page.locator("#missing").is_checked(timeout=missing_timeout), "Locator.is_checked: Timeout 10ms exceeded.")
    expect_error(lambda: page.locator("#missing").is_editable(timeout=missing_timeout), "Locator.is_editable: Timeout 10ms exceeded.")

    plain = page.query_selector("#plain")
    assert plain is not None
    assert plain.is_enabled() is True
    assert plain.is_disabled() is False
    expect_error(
        lambda: plain.is_editable(),
        "ElementHandle.is_editable: Error: Element is not an <input>, <textarea>, <select> or [contenteditable] "
        "and does not have a role allowing [aria-readonly]",
    )
    expect_error(lambda: plain.is_checked(), "ElementHandle.is_checked: Error: Not a checkbox or radio button")


@case
def selector_helper_timeout_prefixes_match_playwright(page):
    page.set_content("<button id='go'>Go</button><input id='field'>")

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(lambda: page.click("#go", timeout="bad"), "Page.click: timeout: expected float, got string")
    expect_error(lambda: page.fill("#field", "x", timeout="bad"), "Page.fill: timeout: expected float, got string")
    expect_error(lambda: page.focus("#field", timeout="bad"), "Page.focus: timeout: expected float, got string")
    expect_error(lambda: page.hover("#go", timeout="bad"), "Page.hover: timeout: expected float, got string")
    expect_error(lambda: page.get_attribute("#go", "id", timeout="bad"), "Page.get_attribute: timeout: expected float, got string")
    expect_error(lambda: page.text_content("#go", timeout="bad"), "Page.text_content: timeout: expected float, got string")
    expect_error(lambda: page.input_value("#field", timeout="bad"), "Page.input_value: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.click("#go", timeout="bad"), "Frame.click: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.fill("#field", "x", timeout="bad"), "Frame.fill: timeout: expected float, got string")
    expect_error(
        lambda: page.main_frame.get_attribute("#go", "id", timeout="bad"),
        "Frame.get_attribute: timeout: expected float, got string",
    )
    expect_error(
        lambda: page.main_frame.text_content("#go", timeout="bad"),
        "Frame.text_content: timeout: expected float, got string",
    )


@case
def extended_selector_helper_timeout_prefixes_match_playwright(page):
    page.set_content(
        """
        <button id="go">Go</button>
        <input id="field">
        <input id="check" type="checkbox">
        <input id="file" type="file">
        <div id="drag" draggable="true">Drag</div>
        <div id="drop">Drop</div>
        """
    )

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(lambda: page.wait_for_selector("#go", timeout="bad"), "Page.wait_for_selector: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.wait_for_selector("#go", timeout="bad"), "Frame.wait_for_selector: timeout: expected float, got string")
    expect_error(lambda: page.dispatch_event("#go", "click", timeout="bad"), "Page.dispatch_event: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.dispatch_event("#go", "click", timeout="bad"), "Frame.dispatch_event: timeout: expected float, got string")
    expect_error(lambda: page.dblclick("#go", timeout="bad"), "Page.dblclick: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.dblclick("#go", timeout="bad"), "Frame.dblclick: timeout: expected float, got string")
    expect_error(lambda: page.type("#field", "x", timeout="bad"), "Page.type: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.type("#field", "x", timeout="bad"), "Frame.type: timeout: expected float, got string")
    expect_error(lambda: page.press("#field", "A", timeout="bad"), "Page.press: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.press("#field", "A", timeout="bad"), "Frame.press: timeout: expected float, got string")
    expect_error(lambda: page.tap("#go", timeout="bad"), "Page.tap: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.tap("#go", timeout="bad"), "Frame.tap: timeout: expected float, got string")
    expect_error(lambda: page.check("#check", timeout="bad"), "Page.check: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.check("#check", timeout="bad"), "Frame.check: timeout: expected float, got string")
    expect_error(lambda: page.uncheck("#check", timeout="bad"), "Page.uncheck: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.uncheck("#check", timeout="bad"), "Frame.uncheck: timeout: expected float, got string")
    expect_error(lambda: page.set_checked("#check", True, timeout="bad"), "Page.set_checked: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.set_checked("#check", True, timeout="bad"), "Frame.set_checked: timeout: expected float, got string")
    expect_error(lambda: page.set_input_files("#file", [], timeout="bad"), "Page.set_input_files: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.set_input_files("#file", [], timeout="bad"), "Frame.set_input_files: timeout: expected float, got string")
    expect_error(lambda: page.drag_and_drop("#drag", "#drop", timeout="bad"), "Page.drag_and_drop: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.drag_and_drop("#drag", "#drop", timeout="bad"), "Frame.drag_and_drop: timeout: expected float, got string")
    expect_error(lambda: page.inner_text("#go", timeout="bad"), "Page.inner_text: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.inner_text("#go", timeout="bad"), "Frame.inner_text: timeout: expected float, got string")
    expect_error(lambda: page.inner_html("#go", timeout="bad"), "Page.inner_html: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.inner_html("#go", timeout="bad"), "Frame.inner_html: timeout: expected float, got string")


@case
def selector_state_timeout_prefixes_match_playwright(page):
    page.set_content("<button id='go'>Go</button><input id='check' type='checkbox'><input id='field' value='x'>")

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    assert page.is_visible("#go", timeout="bad") is True
    assert page.is_hidden("#go", timeout="bad") is False
    expect_error(lambda: page.is_enabled("#go", timeout="bad"), "Page.is_enabled: timeout: expected float, got string")
    expect_error(lambda: page.is_disabled("#go", timeout="bad"), "Page.is_disabled: timeout: expected float, got string")
    expect_error(lambda: page.is_checked("#check", timeout="bad"), "Page.is_checked: timeout: expected float, got string")
    expect_error(lambda: page.is_editable("#field", timeout="bad"), "Page.is_editable: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.is_enabled("#go", timeout="bad"), "Frame.is_enabled: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.is_disabled("#go", timeout="bad"), "Frame.is_disabled: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.is_checked("#check", timeout="bad"), "Frame.is_checked: timeout: expected float, got string")
    expect_error(lambda: page.main_frame.is_editable("#field", timeout="bad"), "Frame.is_editable: timeout: expected float, got string")

    missing_timeout = 10
    expect_error(lambda: page.is_enabled("#missing", timeout=missing_timeout), "Page.is_enabled: Timeout 10ms exceeded.")
    expect_error(lambda: page.is_disabled("#missing", timeout=missing_timeout), "Page.is_disabled: Timeout 10ms exceeded.")
    expect_error(lambda: page.is_checked("#missing", timeout=missing_timeout), "Page.is_checked: Timeout 10ms exceeded.")
    expect_error(lambda: page.is_editable("#missing", timeout=missing_timeout), "Page.is_editable: Timeout 10ms exceeded.")
    expect_error(lambda: page.main_frame.is_enabled("#missing", timeout=missing_timeout), "Frame.is_enabled: Timeout 10ms exceeded.")
    expect_error(lambda: page.main_frame.is_disabled("#missing", timeout=missing_timeout), "Frame.is_disabled: Timeout 10ms exceeded.")
    expect_error(lambda: page.main_frame.is_checked("#missing", timeout=missing_timeout), "Frame.is_checked: Timeout 10ms exceeded.")
    expect_error(lambda: page.main_frame.is_editable("#missing", timeout=missing_timeout), "Frame.is_editable: Timeout 10ms exceeded.")


@case
def set_content_waits_for_load_by_default(page):
    with slow_body_server() as base_url:
        started = time.monotonic()
        page.set_content(f"<img id='slow' src='{base_url}/slow-image.gif'>")
        elapsed = time.monotonic() - started

    assert elapsed >= 0.15
    assert page.evaluate("() => document.querySelector('#slow').complete") is True


@case
def wait_for_load_state_already_loaded(page):
    page.set_content("<title>Loaded</title><main>Ready</main>")
    started = time.monotonic()
    page.wait_for_load_state("domcontentloaded", timeout=250)
    page.wait_for_load_state("load", timeout=250)
    elapsed = time.monotonic() - started

    assert elapsed < 0.2


@case
def navigation_lifecycle_state_validation(page):
    expected = "expected one of (load|domcontentloaded|networkidle|commit)"

    def expect_lifecycle_error(callback, prefix):
        try:
            callback()
        except Exception as exc:
            assert str(exc).splitlines()[0] == f"{prefix}: {expected}"
        else:
            raise AssertionError(f"{prefix} invalid lifecycle state unexpectedly succeeded")

    page.goto(data_url("<title>one</title>"))
    page.goto(data_url("<title>two</title>"))

    expect_lifecycle_error(lambda: page.wait_for_load_state("dom_content_loaded", timeout=50), "state")
    expect_lifecycle_error(lambda: page.set_content("<main>x</main>", wait_until="network_idle", timeout=50), "Page.set_content: wait_until")
    expect_lifecycle_error(lambda: page.goto(data_url("<title>bad</title>"), wait_until="network_idle", timeout=50), "Page.goto: wait_until")
    expect_lifecycle_error(lambda: page.reload(wait_until="network_idle", timeout=50), "Page.reload: wait_until")
    expect_lifecycle_error(lambda: page.go_back(wait_until="network_idle", timeout=50), "Page.go_back: wait_until")
    expect_lifecycle_error(lambda: page.go_forward(wait_until="network_idle", timeout=50), "Page.go_forward: wait_until")
    expect_lifecycle_error(lambda: page.wait_for_url("**/*", wait_until="network_idle", timeout=50), "state")
    expect_lifecycle_error(lambda: page.main_frame.wait_for_load_state("network_idle", timeout=50), "state")
    expect_lifecycle_error(lambda: page.main_frame.set_content("<main>x</main>", wait_until="network_idle", timeout=50), "Frame.set_content: wait_until")
    expect_lifecycle_error(
        lambda: page.main_frame.goto(data_url("<title>frame bad</title>"), wait_until="network_idle", timeout=50),
        "Frame.goto: wait_until",
    )
    expect_lifecycle_error(lambda: page.main_frame.wait_for_url("**/*", wait_until="network_idle", timeout=50), "state")


@case
def wait_for_timeout_argument_validation(page):
    def expect_timeout_error(callback, message):
        try:
            callback()
        except Exception as exc:
            assert str(exc).splitlines()[0] == message
        else:
            raise AssertionError(f"expected timeout argument error {message!r}")

    page.wait_for_timeout(-1)
    page.main_frame.wait_for_timeout(-1)
    expect_timeout_error(
        lambda: page.wait_for_timeout("abc"),
        "Page.wait_for_timeout: wait_timeout: expected float, got string",
    )
    expect_timeout_error(
        lambda: page.wait_for_timeout(True),
        "Page.wait_for_timeout: wait_timeout: expected float, got boolean",
    )
    expect_timeout_error(
        lambda: page.main_frame.wait_for_timeout(None),
        "Frame.wait_for_timeout: wait_timeout: expected float, got undefined",
    )


@case
def history_navigation_returns_document_responses(page):
    with header_case_server() as base_url:
        first_response = page.goto(base_url)
        second_response = page.goto(f"{base_url}/headers")
        second_payload = second_response.json()
        back_response = page.go_back()
        forward_response = page.go_forward()

    assert first_response is not None
    assert first_response.ok
    assert second_response is not None
    assert second_response.url == f"{base_url}/headers"
    assert second_payload == {"ok": True}
    assert back_response is not None
    assert back_response.url.rstrip("/") == base_url
    assert back_response.ok
    assert forward_response is not None
    assert forward_response.url == f"{base_url}/headers"
    assert forward_response.json() == {"ok": True}


@case
def skyvern_hard_reload_cdp_session_detach_keeps_page_alive(page):
    with header_case_server() as base_url:
        response = page.goto(f"{base_url}/headers")
        assert response is not None
        assert response.status == 200

        session = page.context.new_cdp_session(page)
        try:
            assert session.send("Network.clearBrowserCache") == {}
        finally:
            session.detach()

        reload_response = page.reload(wait_until="domcontentloaded")
        assert reload_response is not None
        assert reload_response.status == 200
        assert reload_response.url == f"{base_url}/headers"
        assert page.evaluate("() => location.pathname") == "/headers"


@case
def goto_default_load_waits_for_slow_subresource(page):
    with slow_body_server() as base_url:
        started = time.monotonic()
        response = page.goto(f"{base_url}/slow-image-page")
        elapsed = time.monotonic() - started

    assert response is not None
    assert response.status == 200
    assert response.url == f"{base_url}/slow-image-page"
    assert elapsed >= 0.15
    assert page.title() == "Slow Image"
    assert page.evaluate("() => document.querySelector('#slow').complete") is True


@case
def goto_wait_until_domcontentloaded_does_not_wait_for_slow_subresource(page):
    with slow_body_server() as base_url:
        url = f"{base_url}/slow-image-page?domcontentloaded"
        started = time.monotonic()
        response = page.goto(url, wait_until="domcontentloaded")
        elapsed = time.monotonic() - started

    assert response is not None
    assert response.status == 200
    assert response.url == url
    assert elapsed < 0.3
    assert page.title() == "Slow Image"


@case
def goto_wait_until_networkidle_waits_for_fetch(page):
    with slow_body_server() as base_url:
        started = time.monotonic()
        response = page.goto(f"{base_url}/networkidle-page", wait_until="networkidle")
        elapsed = time.monotonic() - started

    assert response is not None
    assert response.status == 200
    assert response.url == f"{base_url}/networkidle-page"
    assert elapsed >= 0.5
    assert page.title() == "Network Idle"
    assert page.evaluate("() => document.body.dataset.fetchDone") == "yes"


@case
def wait_for_load_state_networkidle_times_out_during_post_load_fetch(page):
    with slow_body_server() as base_url:
        page.goto(f"{base_url}/empty")
        page.evaluate(
            """() => {
            document.body.innerHTML = `<button id="go">go</button>`;
            document.querySelector("#go").onclick = async () => {
              document.body.dataset.fetchDone = "pending";
              await fetch("/slow-fetch-timeout");
              document.body.dataset.fetchDone = "yes";
            };
        }"""
        )
        started = time.monotonic()
        page.click("#go")
        page.wait_for_function("() => document.body.dataset.fetchDone === 'pending'", timeout=500)
        try:
            page.wait_for_load_state("networkidle", timeout=800)
        except Exception as exc:
            message = str(exc).splitlines()[0].lower()
            assert "timeout" in message or "timed out" in message
        else:
            raise AssertionError("wait_for_load_state('networkidle') unexpectedly succeeded during post-load fetch")
        elapsed = time.monotonic() - started

    assert elapsed >= 0.7
    assert page.evaluate("() => document.body.dataset.fetchDone") == "pending"


@case
def expect_navigation_default_load_waits_for_slow_subresource(page):
    with slow_body_server() as base_url:
        url = f"{base_url}/slow-image-page?expect-navigation"
        page.set_content(f'<a id="nav" href="{url}">go</a>')
        started = time.monotonic()
        with page.expect_navigation() as navigation:
            page.click("#nav")
        elapsed = time.monotonic() - started

    response = navigation.value
    assert response is not None
    assert response.status == 200
    assert response.url == url
    assert response.request is not None
    assert response.request.is_navigation_request() is True
    assert elapsed >= 0.15
    assert page.title() == "Slow Image"
    assert page.evaluate("() => document.querySelector('#slow').complete") is True


@case
def expect_navigation_same_document_hash_returns_none(page):
    with slow_body_server() as base_url:
        page.goto(base_url)
        page.set_content('<a id="hash" href="#target">hash</a><main id="target">Target</main>')
        with page.expect_navigation(url="**#target") as navigation:
            page.click("#hash")

    assert navigation.value is None
    assert page.url.endswith("#target")


@case
def expect_navigation_timeout_message_matches_playwright(page):
    page.set_content('<button id="go">go</button>')

    for label, action in (
        ("no_action", lambda: None),
        ("click_without_navigation", lambda: page.click("#go")),
    ):
        try:
            with page.expect_navigation(timeout=50):
                action()
        except Exception as exc:
            first_line = str(exc).splitlines()[0]
        else:
            raise AssertionError(f"{label} expect_navigation unexpectedly succeeded")

        assert first_line == "Timeout 50ms exceeded."


@case
def expect_event_load_waits_for_slow_subresource(page):
    with slow_body_server() as base_url:
        url = f"{base_url}/slow-image-page?load-event"
        page.set_content(f'<a id="nav" href="{url}">go</a>')
        started = time.monotonic()
        with page.expect_event("load") as load_info:
            page.click("#nav")
        elapsed = time.monotonic() - started

    assert load_info.value is page
    assert elapsed >= 0.15
    assert page.title() == "Slow Image"
    assert page.evaluate("() => document.querySelector('#slow').complete") is True


@case
def expect_event_domcontentloaded_does_not_wait_for_slow_subresource(page):
    with slow_body_server() as base_url:
        url = f"{base_url}/slow-image-page?domcontentloaded-event"
        page.set_content(f'<a id="nav" href="{url}">go</a>')
        started = time.monotonic()
        with page.expect_event("domcontentloaded") as domcontentloaded_info:
            page.click("#nav")
        elapsed = time.monotonic() - started

    assert domcontentloaded_info.value is page
    assert elapsed < 0.3
    assert page.title() == "Slow Image"


@case
def page_on_lifecycle_events_receive_page(page):
    seen: list[tuple[str, bool]] = []
    page.on("domcontentloaded", lambda event_page: seen.append(("domcontentloaded", event_page is page)))
    page.on("load", lambda event_page: seen.append(("load", event_page is page)))

    with slow_body_server() as base_url:
        page.goto(f"{base_url}/slow-image-page?on-lifecycle")

    deadline = time.monotonic() + 2
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)

    assert ("domcontentloaded", True) in seen
    assert ("load", True) in seen


@case
def framenavigated_event_for_document_and_hash_navigation(page):
    seen: list[str] = []
    page.on("framenavigated", lambda frame: seen.append(frame.url))

    with slow_body_server() as base_url:
        target = f"{base_url}/slow-image-page?framenavigated"
        with page.expect_event("framenavigated") as event_info:
            page.goto(target)
        frame = event_info.value

        assert frame.page is page
        assert frame.url == target
        deadline = time.monotonic() + 2
        while target not in seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert target in seen

        page.set_content('<a id="hash" href="#target">hash</a><main id="target">Target</main>')
        with page.expect_event("framenavigated") as same_document_info:
            page.click("#hash")

    assert same_document_info.value.page is page
    assert same_document_info.value.url.endswith("#target")
    deadline = time.monotonic() + 2
    while not any(url.endswith("#target") for url in seen) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert any(url.endswith("#target") for url in seen)


@case
def page_wait_for_filechooser_event_helper(page):
    with tempfile.TemporaryDirectory() as directory:
        upload = Path(directory) / "waited-filechooser.txt"
        upload.write_text("waited chooser", encoding="utf-8")
        page.set_content(
            """
            <input id="wait-file" type="file">
            <script>
            document.querySelector('#wait-file').addEventListener('change', event => {
              document.body.dataset.waitedChooser = event.target.files[0].name;
            });
            </script>
            """
        )

        page.evaluate("() => setTimeout(() => document.querySelector('#wait-file').click(), 20)")
        chooser = page.wait_for_event(
            "filechooser",
            lambda candidate: candidate.element.get_attribute("id") == "wait-file",
            timeout=3_000,
        )
        assert chooser.page is page
        assert chooser.is_multiple() is False
        chooser.set_files(str(upload))
        selected = page.wait_for_function(
            "() => document.body.dataset.waitedChooser === 'waited-filechooser.txt'",
            timeout=3_000,
        )
        try:
            assert selected.json_value() is True
        finally:
            selected.dispose()


@case
def frameattached_and_framedetached_events(page):
    page.set_content("<main></main>")
    attached_seen: list[tuple[bool, bool, bool]] = []
    page.on(
        "frameattached",
        lambda frame: attached_seen.append((frame.page is page, frame.parent_frame is not None and frame.parent_frame.page is page, frame.is_detached())),
    )

    with page.expect_event("frameattached") as attached_info:
        page.evaluate(
            """() => {
            const frame = document.createElement('iframe');
            frame.name = 'child';
            frame.srcdoc = '<p>child</p>';
            document.body.appendChild(frame);
            }"""
        )

    attached = attached_info.value
    assert attached.page is page
    assert attached.parent_frame is not None
    assert attached.parent_frame.page is page
    assert attached.is_detached() is False
    deadline = time.monotonic() + 2
    while not any(item[0] and item[1] and item[2] is False for item in attached_seen) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert any(item[0] and item[1] and item[2] is False for item in attached_seen)

    detached_seen: list[tuple[bool, bool, bool]] = []
    page.on(
        "framedetached",
        lambda frame: detached_seen.append((frame.page is page, frame.parent_frame is not None and frame.parent_frame.page is page, frame.is_detached())),
    )

    with page.expect_event("framedetached") as detached_info:
        page.evaluate("() => document.querySelector('iframe').remove()")

    detached = detached_info.value
    assert detached.page is page
    assert detached.parent_frame is not None
    assert detached.parent_frame.page is page
    assert detached.is_detached() is True
    deadline = time.monotonic() + 2
    while not any(item[0] and item[1] and item[2] is True for item in detached_seen) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert any(item[0] and item[1] and item[2] is True for item in detached_seen)


@case
def file_chooser_predicate_matches_multiple_input(page):
    page.set_content(
        """
        <input id="single" type="file">
        <input id="multi" type="file" multiple>
        """
    )

    with page.expect_file_chooser(lambda chooser: chooser.is_multiple()) as chooser_info:
        page.click("#multi")

    chooser = chooser_info.value
    assert chooser.page is page
    assert chooser.is_multiple() is True
    chooser.set_files([])

    seen = []
    page.on("filechooser", lambda chooser: seen.append((chooser.page is page, chooser.is_multiple())))
    page.click("#single")
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        page.wait_for_timeout(20)
    assert seen == [(True, False)]


@case
def response_finished_waits_for_slow_body(page):
    with slow_body_server() as base_url:
        page.goto(base_url)
        started = time.monotonic()
        with page.expect_response("**/slow-body") as response_info:
            page.evaluate("() => { fetch('/slow-body').then(response => response.text()); }")
        response = response_info.value
        assert response.finished() is None
        elapsed = time.monotonic() - started

    assert elapsed >= 0.15
    assert response.text().startswith("<title>Slow Body</title>")


@case
def navigation_response_body_is_readable_before_later_navigation(page):
    with header_case_server() as base_url:
        first = page.goto(f"{base_url}/echo-headers")
        first_body = first.json()
        second = page.goto(f"{base_url}/query?after=1")
        second_body = second.json()

    assert second_body == {"path": "/query", "query": {"after": ["1"]}}
    assert first_body["x-route-header"] is None


@case
def network_header_maps_are_lowercase_and_arrays_preserve_original_case(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        with page.expect_request("**/headers") as request_info:
            with page.expect_response("**/headers") as response_info:
                page.click("#go")

        request = request_info.value
        response = response_info.value

    assert request.headers["x-client-mixed"] == "ReqValue"
    assert "X-Client-Mixed" not in request.headers
    assert {"name": "X-Client-Mixed", "value": "ReqValue"} in request.headers_array()
    assert request.all_headers()["x-client-mixed"] == "ReqValue"

    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-mixed-case"] == "ResponseValue"
    assert "X-Mixed-Case" not in response.headers
    assert {"name": "X-Mixed-Case", "value": "ResponseValue"} in response.headers_array()
    assert response.all_headers()["x-mixed-case"] == "ResponseValue"


@case
def subframe_request_response_frame_attribution(page):
    with header_case_server() as base_url:
        page.goto(f"{base_url}/frame-page")
        frame = page.frame(name="child")
        assert frame is not None
        assert frame.url == f"{base_url}/frame-child"

        with page.expect_request("**/headers") as request_info:
            with page.expect_response("**/headers") as response_info:
                frame.click("#fetch")

        request = request_info.value
        response = response_info.value
        metadata = {
            "request_frame_is_child": request.frame is frame,
            "response_frame_is_child": response.frame is frame,
            "response_request_frame_is_child": response.request.frame is frame,
            "frame_name": request.frame.name,
            "frame_page_is_page": request.frame.page is page,
            "dataset": frame.evaluate("() => document.body.dataset.fetchOk"),
        }

    assert metadata == {
        "request_frame_is_child": True,
        "response_frame_is_child": True,
        "response_request_frame_is_child": True,
        "frame_name": "child",
        "frame_page_is_page": True,
        "dataset": "true",
    }


@case
def cross_origin_subframe_request_response_frame_attribution(page):
    with header_case_server() as top_base:
        with header_case_server() as child_base:
            page.goto(top_base)
            frame_url = f"{child_base}/frame-child-auto"

            with page.expect_request(f"{child_base}/headers", timeout=5_000) as request_info:
                with page.expect_response(f"{child_base}/headers", timeout=5_000) as response_info:
                    page.evaluate(
                        """(url) => {
                        const frame = document.createElement('iframe');
                        frame.name = 'child';
                        frame.src = url;
                        document.body.appendChild(frame);
                        }""",
                        frame_url,
                    )

            request = request_info.value
            response = response_info.value
            frame = request.frame
            metadata = {
                "request_frame_is_main": frame is page.main_frame,
                "response_frame_is_child": response.frame is frame,
                "response_request_frame_is_child": response.request.frame is frame,
                "frame_in_page_frames": frame in page.frames,
                "frame_name": frame.name,
                "frame_url": frame.url,
                "frame_page_is_page": frame.page is page,
                "json": response.json(),
            }

    assert metadata == {
        "request_frame_is_main": False,
        "response_frame_is_child": True,
        "response_request_frame_is_child": True,
        "frame_in_page_frames": True,
        "frame_name": "child",
        "frame_url": frame_url,
        "frame_page_is_page": True,
        "json": {"ok": True},
    }


@case
def response_set_cookie_helpers_use_extra_info_headers(page):
    with header_case_server() as base_url:
        response = page.goto(f"{base_url}/set-cookies")

    assert "set-cookie" not in response.headers
    assert response.all_headers()["set-cookie"] == "first=one; Path=/\nsecond=two; Path=/"
    assert response.header_value("set-cookie") == "first=one; Path=/\nsecond=two; Path=/"
    assert response.header_values("set-cookie") == ["first=one; Path=/", "second=two; Path=/"]
    assert {"name": "Set-Cookie", "value": "first=one; Path=/"} in response.headers_array()
    assert {"name": "Set-Cookie", "value": "second=two; Path=/"} in response.headers_array()


@case
def https_response_metadata_shape(page):
    browser = page.context.browser
    assert browser is not None
    with https_case_server() as base_url:
        context = browser.new_context(ignore_https_errors=True)
        try:
            secure_page = context.new_page()
            response = secure_page.goto(f"{base_url}/secure")
            assert response is not None
            metadata = {
                "http_version": response.http_version(),
                "security_details": response.security_details(),
                "server_addr": response.server_addr(),
            }
        finally:
            context.close()

    assert metadata["http_version"].startswith("http/")
    security_details = metadata["security_details"]
    assert security_details is not None
    assert set(security_details) == {"issuer", "protocol", "subjectName", "validFrom", "validTo"}
    assert security_details["issuer"] == "localhost"
    assert security_details["subjectName"] == "localhost"
    assert security_details["protocol"].startswith("TLS ")
    assert isinstance(security_details["validFrom"], int)
    assert security_details["validTo"] > security_details["validFrom"]
    server_addr = metadata["server_addr"]
    assert server_addr is not None
    assert server_addr["ipAddress"] == "127.0.0.1"
    assert server_addr["port"] > 0


@case
def service_worker_fulfilled_response_metadata(page):
    browser = page.context.browser
    assert browser is not None

    with service_worker_case_server() as base_url:
        context = browser.new_context()
        try:
            worker_page = context.new_page()
            worker_page.goto(f"{base_url}/service-worker-page")
            handle = worker_page.wait_for_function("() => document.body.dataset.sw === 'ready'", timeout=3_000)
            handle.dispose()
            worker_page.reload()
            handle = worker_page.wait_for_function("() => navigator.serviceWorker.controller !== null", timeout=3_000)
            handle.dispose()

            with worker_page.expect_response("**/sw-controlled") as response_info:
                worker_page.evaluate(
                    """
                    () => fetch('/sw-controlled')
                      .then(response => response.json())
                      .then(data => { document.body.dataset.swFetch = data.source; })
                    """
                )

            response = response_info.value
            metadata = {
                "status": response.status,
                "from_service_worker": response.from_service_worker,
                "x_sw": response.header_value("x-sw"),
                "json": response.json(),
                "request_service_worker": response.request.service_worker,
                "request_frame_is_page": response.request.frame.page is worker_page,
                "dataset": worker_page.evaluate("() => document.body.dataset.swFetch"),
            }
        finally:
            context.close()

    assert metadata == {
        "status": 203,
        "from_service_worker": True,
        "x_sw": "yes",
        "json": {"source": "service-worker"},
        "request_service_worker": None,
        "request_frame_is_page": True,
        "dataset": "service-worker",
    }


@case
def context_service_worker_event_listing_and_evaluation(page):
    browser = page.context.browser
    assert browser is not None

    with service_worker_case_server() as base_url:
        context = browser.new_context()
        try:
            worker_page = context.new_page()
            with context.expect_event("serviceworker") as worker_info:
                worker_page.goto(f"{base_url}/service-worker-page")

            worker = worker_info.value
            assert worker.url == f"{base_url}/sw.js"
            handle = worker_page.wait_for_function("() => document.body.dataset.sw === 'ready'", timeout=3_000)
            handle.dispose()
            assert worker.evaluate("() => self.__parityServiceWorkerValue") == 73

            listed = []
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not listed:
                listed = context.service_workers
                worker_page.wait_for_timeout(20)
            assert any(existing.url == f"{base_url}/sw.js" for existing in listed)
        finally:
            context.close()


@case
def context_service_workers_block_prevents_registration(page):
    browser = page.context.browser
    assert browser is not None

    with service_worker_case_server() as base_url:
        context = browser.new_context(service_workers="block")
        try:
            seen = []

            def on_service_worker(worker):
                seen.append(worker)

            context.on("serviceworker", on_service_worker)
            worker_page = context.new_page()
            worker_page.goto(f"{base_url}/service-worker-page")
            worker_page.wait_for_timeout(500)

            assert worker_page.evaluate("() => document.body.dataset.sw || null") is None
            assert worker_page.evaluate("async () => (await navigator.serviceWorker.getRegistrations()).length") == 0
            assert context.service_workers == []
            assert seen == []
        finally:
            context.close()


@case
def api_request_context_get_post_params_and_dispose(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")

    with header_case_server() as base_url:
        get_response = page.context.request.get(f"{base_url}/headers", headers={"X-Test-Header": "from-api"})
        post_response = page.context.request.post(f"{base_url}/echo", data={"hello": "world"})
        query_response = page.context.request.get(f"{base_url}/query", params={"alpha": "one"})

        try:
            page.context.request.get(f"{base_url}/bad", fail_on_status_code=True)
        except sync_api.Error as exc:
            assert "500" in str(exc)
        else:
            raise AssertionError("fail_on_status_code=True should raise on a 500 API response")

    assert get_response.ok is True
    assert get_response.status == 200
    assert get_response.status_text == "OK"
    assert get_response.headers["content-type"] == "application/json"
    assert {"name": "Content-Type", "value": "application/json"} in get_response.headers_array
    assert get_response.json() == {"ok": True}
    get_response.dispose()
    try:
        get_response.body()
    except sync_api.Error as exc:
        assert "Response has been disposed" in str(exc)
    else:
        raise AssertionError("disposed APIResponse.body() should fail")

    assert post_response.status == 202
    payload = post_response.json()
    assert payload["content_type"].startswith("application/json")
    assert json.loads(payload["body"]) == {"hello": "world"}
    assert query_response.json() == {"path": "/query", "query": {"alpha": ["one"]}}


@case
def browser_context_request_client_certificates_authenticate(page):
    browser = page.context.browser
    assert browser is not None

    with mtls_case_server() as server:
        context = browser.new_context(
            ignore_https_errors=True,
            client_certificates=[
                {
                    "origin": server["origin"],
                    "certPath": server["client_cert"],
                    "keyPath": server["client_key"],
                }
            ],
        )
        try:
            response = context.request.get(server["url"])
            assert response.ok
            payload = response.json()
        finally:
            context.close()

    assert payload["path"] == "/secure"
    assert "commonName=Rustwright Client" in payload["client_subject"]


@case
def browser_context_page_goto_client_certificates_authenticate(page):
    browser = page.context.browser
    assert browser is not None

    with mtls_case_server() as server:
        context = browser.new_context(
            ignore_https_errors=True,
            client_certificates=[
                {
                    "origin": server["origin"],
                    "certPath": server["client_cert"],
                    "keyPath": server["client_key"],
                }
            ],
        )
        try:
            secured_page = context.new_page()
            response = secured_page.goto(server["url"])
            assert response is not None
            assert response.ok
            payload = response.json()
            body_payload = json.loads(secured_page.locator("body").inner_text())
        finally:
            context.close()

    assert payload["path"] == "/secure"
    assert "commonName=Rustwright Client" in payload["client_subject"]
    assert body_payload["path"] == "/secure"


@case
def api_request_context_data_body_encoding(page):
    with header_case_server() as base_url:
        int_response = page.context.request.post(f"{base_url}/echo", data=7)
        bool_response = page.context.request.post(f"{base_url}/echo", data=True)
        json_string_response = page.context.request.post(
            f"{base_url}/echo",
            headers={"Content-Type": "application/json"},
            data="raw-body",
        )

    assert int_response.json() == {
        "content_type": "application/json",
        "x_test": None,
        "body": "7",
    }
    assert bool_response.json() == {
        "content_type": "application/json",
        "x_test": None,
        "body": "true",
    }
    assert json_string_response.json() == {
        "content_type": "application/json",
        "x_test": None,
        "body": '"raw-body"',
    }


@case
def api_request_context_fetch_uses_request_object_defaults(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        with page.expect_request("**/echo") as request_info:
            page.evaluate(
                """() => fetch('/echo', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Test-Header': 'from-page-request' },
                    body: JSON.stringify({ source: 'page-request' })
                })"""
            )
        response = page.context.request.fetch(request_info.value)

    assert response.status == 202
    assert response.json() == {
        "content_type": "application/json",
        "x_test": "from-page-request",
        "body": '{"source":"page-request"}',
    }


@case
def api_request_context_multipart_file_payload(page):
    with header_case_server() as base_url:
        response = page.context.request.post(
            f"{base_url}/multipart",
            multipart={
                "empty": "",
                "title": "Quarterly report",
                "zero": "0",
                "rawBytes": b"raw bytes are skipped by Playwright Python",
                "attachment": {
                    "name": "report.txt",
                    "mimeType": "text/plain",
                    "buffer": b"hello multipart",
                },
                "mimeFallback": {
                    "name": "payload.bin",
                    "mimeType": "",
                    "buffer": b"fallback mime",
                },
            },
        )

    payload = response.json()
    assert payload["content_type"].startswith("multipart/form-data; boundary=")
    assert "rustwright" not in payload["content_type"].lower()
    assert "rustwright" not in payload["body"].lower()
    assert 'name="empty"' not in payload["body"]
    assert 'name="rawBytes"' not in payload["body"]
    assert 'content-disposition: form-data; name="title"' in payload["body"]
    assert "Quarterly report" in payload["body"]
    assert 'content-disposition: form-data; name="zero"' in payload["body"]
    assert "\r\n0\r\n" in payload["body"]
    assert 'content-disposition: form-data; name="attachment"; filename="report.txt"' in payload["body"]
    assert "content-type: text/plain" in payload["body"]
    assert "hello multipart" in payload["body"]
    assert 'content-disposition: form-data; name="mimeFallback"; filename="payload.bin"' in payload["body"]
    assert "content-type: application/octet-stream" in payload["body"]
    assert "fallback mime" in payload["body"]


@case
def api_request_context_body_option_truthiness_matches_playwright(page):
    with header_case_server() as base_url:
        cases = {
            "empty_multipart": {"multipart": {}},
            "empty_form": {"form": {}},
            "data_with_empty_multipart": {"data": "body", "multipart": {}},
            "data_with_empty_form": {"data": "body", "form": {}},
            "empty_data_with_form": {"data": "", "form": {"x": "y"}},
            "zero_data_with_form": {"data": 0, "form": {"x": "y"}},
        }
        results = {
            label: page.context.request.post(f"{base_url}/echo", **kwargs).json()
            for label, kwargs in cases.items()
        }

    assert results == {
        "empty_multipart": {"body": "", "content_type": None, "x_test": None},
        "empty_form": {"body": "", "content_type": None, "x_test": None},
        "data_with_empty_multipart": {
            "body": "body",
            "content_type": "application/octet-stream",
            "x_test": None,
        },
        "data_with_empty_form": {
            "body": "body",
            "content_type": "application/octet-stream",
            "x_test": None,
        },
        "empty_data_with_form": {"body": "", "content_type": None, "x_test": None},
        "zero_data_with_form": {"body": "0", "content_type": "application/json", "x_test": None},
    }


@case
def api_request_context_invalid_url_errors_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    request = page.context.request

    cases = [
        ("get_relative", lambda: request.get("/headers")),
        ("post_relative", lambda: request.post("headers", data="x")),
        ("fetch_malformed", lambda: request.fetch("http://")),
        ("get_unsupported_protocol", lambda: request.get("ftp://example.com/file")),
        ("fetch_unsupported_protocol", lambda: request.fetch("data:text/plain,hi")),
        ("get_colon_protocol", lambda: request.get("localhost:1234/path")),
    ]
    errors: list[list[str]] = []
    for label, operation in cases:
        try:
            operation()
        except sync_api.Error as exc:
            errors.append([label, type(exc).__name__, str(exc).splitlines()[0]])
        else:
            raise AssertionError(f"{label} unexpectedly accepted an invalid API request URL")

    assert errors == [
        ["get_relative", "Error", "APIRequestContext.get: Invalid URL"],
        ["post_relative", "Error", "APIRequestContext.post: Invalid URL"],
        ["fetch_malformed", "Error", "APIRequestContext.fetch: Invalid URL"],
        [
            "get_unsupported_protocol",
            "Error",
            'APIRequestContext.get: Protocol "ftp:" not supported. Expected "http:"',
        ],
        [
            "fetch_unsupported_protocol",
            "Error",
            'APIRequestContext.fetch: Protocol "data:" not supported. Expected "http:"',
        ],
        [
            "get_colon_protocol",
            "Error",
            'APIRequestContext.get: Protocol "localhost:" not supported. Expected "http:"',
        ],
    ]


@case
def api_request_context_dispose_reason_and_header_errors_match_playwright(page, playwright):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")

    with header_case_server() as base_url:
        errors: list[list[str]] = []
        request = playwright.request.new_context(base_url=base_url)
        try:
            try:
                request.get("/headers", headers={None: "x"})
            except sync_api.Error as exc:
                errors.append(["header_none_name", str(exc).splitlines()[0]])
            else:
                raise AssertionError("APIRequestContext.get() accepted a None header name")
        finally:
            request.dispose()

        truthy_reason = playwright.request.new_context(base_url=base_url)
        try:
            try:
                truthy_reason.dispose(reason=123)
            except sync_api.Error as exc:
                errors.append(["dispose_reason_number", str(exc).splitlines()[0]])
            else:
                raise AssertionError("APIRequestContext.dispose(reason=123) unexpectedly succeeded")
            try:
                truthy_reason.get("/headers")
            except Exception as exc:
                errors.append(["after_truthy_invalid_reason", str(exc).splitlines()[0]])
            else:
                raise AssertionError("truthy invalid dispose reason did not close APIRequestContext")
        finally:
            try:
                truthy_reason.dispose()
            except Exception:
                pass

        false_reason = playwright.request.new_context(base_url=base_url)
        try:
            try:
                false_reason.dispose(reason=False)
            except sync_api.Error as exc:
                errors.append(["dispose_reason_false", str(exc).splitlines()[0]])
            else:
                raise AssertionError("APIRequestContext.dispose(reason=False) unexpectedly succeeded")
            errors.append(["after_false_invalid_reason", str(false_reason.get("/headers").status)])
        finally:
            false_reason.dispose()

    assert errors == [
        ["header_none_name", "APIRequestContext.get: headers[0].name: expected string, got object"],
        ["dispose_reason_number", "APIRequestContext.dispose: reason: expected string, got number"],
        ["after_truthy_invalid_reason", "123"],
        ["dispose_reason_false", "APIRequestContext.dispose: reason: expected string, got boolean"],
        ["after_false_invalid_reason", "200"],
    ]


@case
def api_request_context_head_put_patch_and_delete(page):
    with header_case_server() as base_url:
        head_response = page.context.request.head(f"{base_url}/method")
        put_response = page.context.request.put(f"{base_url}/method", data="put-body")
        patch_response = page.context.request.patch(f"{base_url}/method", data={"patched": True})
        delete_response = page.context.request.delete(f"{base_url}/method")

    assert head_response.status == 204
    assert head_response.status_text == "No Content"
    assert head_response.headers["x-method"] == "HEAD"
    assert head_response.body() == b""

    assert put_response.status == 200
    assert put_response.json() == {
        "method": "PUT",
        "content_type": "application/octet-stream",
        "body": "put-body",
    }

    patch_payload = patch_response.json()
    assert patch_payload["method"] == "PATCH"
    assert patch_payload["content_type"].startswith("application/json")
    assert json.loads(patch_payload["body"]) == {"patched": True}

    assert delete_response.status == 200
    assert delete_response.json() == {"method": "DELETE", "content_type": None, "body": ""}


@case
def api_request_context_cookie_storage_state(page):
    with header_case_server() as base_url:
        page.context.request.get(f"{base_url}/set-cookies")
        echo = page.context.request.get(f"{base_url}/cookie-echo").json()["cookie"]

        assert "first=one" in echo
        assert "second=two" in echo

        state = page.context.request.storage_state()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "api-state.json"
            assert page.context.request.storage_state(path=str(state_path)) == state
            assert json.loads(state_path.read_text(encoding="utf-8")) == state

    cookie_names = {cookie["name"] for cookie in state["cookies"]}
    assert {"first", "second"}.issubset(cookie_names)
    assert state["origins"] == []


@case
def browser_context_storage_state_replays_cookies_and_local_storage(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        source = browser.new_context()
        restored = None
        try:
            source_page = source.new_page()
            source_page.goto(base_url)
            source_page.evaluate("localStorage.setItem('token', 'abc')")
            source.add_cookies([{"name": "sid", "value": "123", "url": base_url}])
            state = source.storage_state()
            source.close()

            restored = browser.new_context(storage_state=state)
            restored_page = restored.new_page()
            restored_page.goto(base_url)

            assert restored_page.evaluate("localStorage.getItem('token')") == "abc"
            assert "sid=123" in restored_page.evaluate("document.cookie")
        finally:
            if not source.is_closed():
                source.close()
            if restored is not None and not restored.is_closed():
                restored.close()


@case
def browser_context_storage_state_path_roundtrip(page):
    browser = page.context.browser

    with tempfile.TemporaryDirectory() as directory:
        state_path = Path(directory) / "browser-state.json"

        with header_case_server() as base_url:
            source = browser.new_context()
            restored = None
            try:
                source_page = source.new_page()
                source_page.goto(base_url)
                source_page.evaluate("localStorage.setItem('path-token', 'from-file')")
                source.add_cookies([{"name": "path_sid", "value": "456", "url": base_url}])

                state = source.storage_state(path=str(state_path))
                assert json.loads(state_path.read_text(encoding="utf-8")) == state

                source.close()

                restored = browser.new_context(storage_state=str(state_path))
                restored_page = restored.new_page()
                restored_page.goto(base_url)

                assert restored_page.evaluate("localStorage.getItem('path-token')") == "from-file"
                assert "path_sid=456" in restored_page.evaluate("document.cookie")
            finally:
                if not source.is_closed():
                    source.close()
                if restored is not None and not restored.is_closed():
                    restored.close()


@case
def browser_context_storage_state_validation_and_tuple_input(page, playwright):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("storage_state payload was unexpectedly accepted")

    invalid_context_cases = [
        (
            lambda: browser.new_context(storage_state={"cookies": "bad"}),
            "Browser.new_context: storageState.cookies: expected array, got string",
        ),
        (
            lambda: browser.new_context(storage_state={"cookies": [1]}),
            "Browser.new_context: storageState.cookies[0]: expected object, got number",
        ),
        (
            lambda: browser.new_context(storage_state={"origins": "bad"}),
            "Browser.new_context: storageState.origins: expected array, got string",
        ),
        (
            lambda: browser.new_context(storage_state={"origins": [{}]}),
            "Browser.new_context: storageState.origins[0].origin: expected string, got undefined",
        ),
        (
            lambda: browser.new_context(
                storage_state={
                    "origins": [
                        {
                            "origin": "https://example.com",
                            "localStorage": [{"name": "n", "value": 1}],
                        }
                    ]
                }
            ),
            "Browser.new_context: storageState.origins[0].localStorage[0].value: expected string, got number",
        ),
    ]
    for operation, message in invalid_context_cases:
        expect_error(operation, message)

    expect_error(
        lambda: browser.new_page(storage_state={"cookies": [1]}),
        "Browser.new_page: storageState.cookies[0]: expected object, got number",
    )
    expect_error(
        lambda: playwright.request.new_context(storage_state={"cookies": "bad"}),
        "APIRequest.new_context: storageState.cookies: expected array, got string",
    )
    with tempfile.TemporaryDirectory() as directory:
        state_path = Path(directory) / "empty-storage-state.json"
        state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        request_from_bytes = playwright.request.new_context(storage_state=bytes(str(state_path), "utf-8"))
        request_from_bytes.dispose()
        context_from_bytes = browser.new_context(storage_state=bytes(str(state_path), "utf-8"))
        context_from_bytes.close()

    expect_error(
        lambda: browser.new_context(storage_state=[]),
        "expected str, bytes or os.PathLike object, not list",
    )
    expect_error(
        lambda: browser.new_context(storage_state=()),
        "expected str, bytes or os.PathLike object, not tuple",
    )
    empty_request_list = playwright.request.new_context(storage_state=[])
    empty_request_list.dispose()
    empty_request_tuple = playwright.request.new_context(storage_state=())
    empty_request_tuple.dispose()
    expect_error(
        lambda: playwright.request.new_context(storage_state=""),
        "APIRequest.new_context: storage_state: expected object, got string",
    )
    expect_error(
        lambda: playwright.request.new_context(storage_state=["state.json"]),
        "expected str, bytes or os.PathLike object, not list",
    )

    with header_case_server() as base_url:
        with tempfile.TemporaryDirectory() as directory:
            origin_state_path = Path(directory) / "origin-storage-state.json"
            origin_state_path.write_text(
                json.dumps(
                    {
                        "cookies": [],
                        "origins": [
                            {
                                "origin": base_url,
                                "localStorage": [{"name": "set-token", "value": "from-file"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            loaded_context = browser.new_context()
            try:
                loaded_context.set_storage_state(str(origin_state_path))
                loaded_page = loaded_context.new_page()
                loaded_page.goto(base_url)
                assert loaded_page.evaluate("localStorage.getItem('set-token')") == "from-file"
            finally:
                loaded_context.close()

            for value in (bytes(str(origin_state_path), "utf-8"), [], (), ["state.json"], None):
                noop_context = browser.new_context()
                try:
                    noop_context.set_storage_state(value)
                    noop_page = noop_context.new_page()
                    noop_page.goto(base_url)
                    assert noop_page.evaluate("localStorage.getItem('set-token')") is None
                finally:
                    noop_context.close()

            def invalid_set_storage_state(value):
                invalid_context = browser.new_context()
                try:
                    invalid_context.set_storage_state(value)
                finally:
                    invalid_context.close()

            expect_error(
                lambda: invalid_set_storage_state(123),
                "BrowserContext.set_storage_state: storage_state: expected object, got number",
            )

        parsed = urlparse(base_url)
        context = browser.new_context(
            storage_state={
                "cookies": (
                    {
                        "name": "tuple_state",
                        "value": "yes",
                        "domain": parsed.hostname,
                        "path": "/",
                        "expires": int(time.time()) + 3600,
                    },
                ),
                "origins": (
                    {
                        "origin": base_url,
                        "localStorage": (
                            {"name": "token", "value": "abc"},
                        ),
                    },
                ),
            }
        )
        try:
            restored_page = context.new_page()
            restored_page.goto(base_url)
            assert restored_page.evaluate("document.cookie") == "tuple_state=yes"
            assert restored_page.evaluate("localStorage.getItem('token')") == "abc"
        finally:
            context.close()


@case
def browser_context_storage_state_replays_indexed_db(page):
    browser = page.context.browser
    create_indexed_db = """async () => {
    const request = indexedDB.open('auth-db', 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore('tokens', { keyPath: 'id' });
    };
    const db = await new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const tx = db.transaction('tokens', 'readwrite');
    tx.objectStore('tokens').put({ id: 'primary', token: 'secret', nested: { ok: true } });
    await new Promise((resolve, reject) => {
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
    db.close();
    }"""
    read_indexed_db = """async () => {
    const request = indexedDB.open('auth-db');
    const db = await new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const tx = db.transaction('tokens', 'readonly');
    const value = await new Promise((resolve, reject) => {
      const get = tx.objectStore('tokens').get('primary');
      get.onsuccess = () => resolve(get.result);
      get.onerror = () => reject(get.error);
    });
    db.close();
    return value;
    }"""

    with header_case_server() as base_url:
        source = browser.new_context()
        restored = None
        try:
            source_page = source.new_page()
            source_page.goto(base_url)
            source_page.evaluate(create_indexed_db)
            state = source.storage_state(indexed_db=True)
            source.close()

            origin_state = next(item for item in state["origins"] if item["origin"] == base_url)
            assert origin_state["indexedDB"][0]["name"] == "auth-db"
            assert origin_state["indexedDB"][0]["stores"][0]["records"] == [
                {"value": {"id": "primary", "nested": {"ok": True}, "token": "secret"}}
            ]

            restored = browser.new_context(storage_state=state)
            restored_page = restored.new_page()
            restored_page.goto(base_url)

            assert restored_page.evaluate(read_indexed_db) == {"id": "primary", "nested": {"ok": True}, "token": "secret"}
        finally:
            if not source.is_closed():
                source.close()
            if restored is not None and not restored.is_closed():
                restored.close()


@case
def browser_context_clear_cookies_with_filters(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context()
        try:
            context.add_cookies(
                [
                    {"name": "sid", "value": "123", "url": base_url},
                    {"name": "theme", "value": "dark", "url": base_url},
                ]
            )
            assert {cookie["name"] for cookie in context.cookies(base_url)} == {"sid", "theme"}

            context.clear_cookies(name="sid")

            assert {cookie["name"] for cookie in context.cookies(base_url)} == {"theme"}
        finally:
            context.close()


@case
def browser_context_cookies_url_filters_and_regex_clear(page):
    browser = page.context.browser
    context = browser.new_context()
    try:
        context.add_cookies(
            [
                {"name": "root", "value": "r", "url": "https://example.com/"},
                {"name": "app", "value": "a", "domain": "example.com", "path": "/app"},
                {"name": "remote", "value": "yes", "url": "https://other.example.org/"},
            ]
        )

        assert {cookie["name"] for cookie in context.cookies("https://example.com/")} == {"root"}
        assert {cookie["name"] for cookie in context.cookies("https://example.com/app/page")} == {"root", "app"}
        assert {cookie["name"] for cookie in context.cookies(["https://other.example.org/"])} == {"remote"}

        context.clear_cookies(path=re.compile("^/app"))
        assert {cookie["name"] for cookie in context.cookies("https://example.com/app/page")} == {"root"}

        context.clear_cookies(domain=re.compile("other\\.example\\.org$"))
        assert {cookie["name"] for cookie in context.cookies()} == {"root"}
    finally:
        context.close()


@case
def browser_context_cookie_attributes_roundtrip(page):
    browser = page.context.browser
    context = browser.new_context()
    expires_at = 2147483647
    try:
        context.add_cookies(
            [
                {
                    "name": "attr",
                    "value": "v",
                    "domain": "example.com",
                    "path": "/secure",
                    "expires": expires_at,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Strict",
                }
            ]
        )

        cookies = context.cookies("https://example.com/secure/page")
        assert len(cookies) == 1
        cookie = cookies[0]
        assert cookie["name"] == "attr"
        assert cookie["value"] == "v"
        assert cookie["domain"] == "example.com"
        assert cookie["path"] == "/secure"
        assert int(cookie["expires"]) > int(time.time())
        assert cookie["httpOnly"] is True
        assert cookie["secure"] is True
        assert cookie["sameSite"] == "Strict"

        state_cookie = context.storage_state()["cookies"][0]
        assert state_cookie["name"] == "attr"
        assert state_cookie["value"] == "v"
        assert state_cookie["domain"] == "example.com"
        assert state_cookie["path"] == "/secure"
        assert int(state_cookie["expires"]) == int(cookie["expires"])
        assert state_cookie["httpOnly"] is True
        assert state_cookie["secure"] is True
        assert state_cookie["sameSite"] == "Strict"
    finally:
        context.close()


@case
def browser_context_add_cookies_validation_and_tuple_input(page):
    browser = page.context.browser
    context = browser.new_context()
    try:

        def expect_error(value, expected):
            try:
                context.add_cookies(value)
            except Exception as exc:
                assert str(exc).splitlines()[0] == expected
                return
            raise AssertionError("add_cookies unexpectedly accepted invalid payload")

        invalid_cases = [
            ("bad", "BrowserContext.add_cookies: cookies: expected array, got string"),
            ([1], "BrowserContext.add_cookies: cookies[0]: expected object, got number"),
            ([{}], "BrowserContext.add_cookies: cookies[0].name: expected string, got undefined"),
            (
                [{"name": "sid", "value": "v"}],
                "BrowserContext.add_cookies: Cookie should have a url or a domain/path pair",
            ),
            (
                [{"name": "sid", "value": "v", "url": "https://example.com", "domain": "example.com"}],
                "BrowserContext.add_cookies: Cookie should have either url or domain",
            ),
            (
                [{"name": "sid", "value": "v", "url": "https://example.com", "expires": True}],
                "BrowserContext.add_cookies: cookies[0].expires: expected float, got boolean",
            ),
            (
                [{"name": "sid", "value": "v", "url": "https://example.com", "sameSite": "Bad"}],
                "BrowserContext.add_cookies: cookies[0].sameSite: expected one of (Strict|Lax|None)",
            ),
        ]
        for value, message in invalid_cases:
            expect_error(value, message)

        context.add_cookies(
            (
                {
                    "name": "tuple_cookie",
                    "value": "ok",
                    "domain": "example.com",
                    "path": "/",
                    "expires": int(time.time()) + 3600,
                    "httpOnly": False,
                    "secure": False,
                    "sameSite": "Lax",
                },
            )
        )
        cookies = context.cookies("https://example.com")
        assert any(cookie["name"] == "tuple_cookie" and cookie["value"] == "ok" for cookie in cookies)
    finally:
        context.close()


@case
def browser_context_cookies_url_validation_and_tuple_input(page):
    browser = page.context.browser
    context = browser.new_context()
    try:

        def expect_error(value, expected):
            try:
                context.cookies(value)
            except Exception as exc:
                assert str(exc).splitlines()[0] == expected
                return
            raise AssertionError("cookies unexpectedly accepted invalid urls")

        invalid_cases = [
            (123, "BrowserContext.cookies: urls: expected array, got number"),
            (True, "BrowserContext.cookies: urls: expected array, got boolean"),
            ([123], "BrowserContext.cookies: urls[0]: expected string, got number"),
            ([True], "BrowserContext.cookies: urls[0]: expected string, got boolean"),
        ]
        for value, message in invalid_cases:
            expect_error(value, message)

        context.add_cookies([{"name": "tuple_url", "value": "ok", "url": "https://example.com/path"}])
        cookies = context.cookies(("https://example.com/path",))
        assert any(cookie["name"] == "tuple_url" and cookie["value"] == "ok" for cookie in cookies)
        assert context.cookies(("https://other.example.org/",)) == []
    finally:
        context.close()


@case
def page_goto_referer_option_sets_navigation_header(page):
    with header_case_server() as base_url:
        response = page.goto(
            f"{base_url}/echo-headers",
            referer="https://referrer.example/source",
        )

    assert response.json()["referer"] == "https://referrer.example/source"


@case
def frame_goto_referer_option_sets_navigation_header(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        page.set_content('<iframe name="child" srcdoc="<main>old</main>"></iframe>')
        frame = page.frame(name="child")
        assert frame is not None
        response = frame.goto(
            f"{base_url}/echo-headers",
            referer=f"{base_url}/from-frame",
            wait_until="domcontentloaded",
        )

    assert response is not None
    assert response.status == 200
    assert response.request is not None
    assert response.request.frame is frame
    assert response.json()["referer"] == f"{base_url}/from-frame"


@case
def frame_goto_special_scheme_responses_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    page.set_content('<iframe name="child" srcdoc="<main>old</main>"></iframe>')
    frame = page.frame(name="child")
    assert frame is not None

    assert frame.goto("about:blank") is None
    assert frame.url == "about:blank"

    data_target = data_url("<title>Frame Data</title><main>data</main>")
    assert frame.goto(data_target) is None
    assert frame.url == data_target
    assert frame.title() == "Frame Data"

    before = frame.url
    javascript_target = "javascript:void(document.body.dataset.changed='yes')"
    try:
        frame.goto(javascript_target, timeout=2_000)
    except sync_api.Error as exc:
        message = str(exc).splitlines()[0]
    else:
        raise AssertionError("frame javascript navigation unexpectedly succeeded")

    assert message == f"Frame.goto: net::ERR_ABORTED at {javascript_target}"
    assert frame.url == before
    assert frame.evaluate("() => document.body.dataset.changed") == "yes"

    string_target = "javascript:'<h1>frame replacement</h1>'"
    try:
        frame.goto(string_target, timeout=2_000)
    except sync_api.Error as exc:
        string_message = str(exc).splitlines()[0]
    else:
        raise AssertionError("frame javascript string navigation unexpectedly succeeded")

    assert string_message == f"Frame.goto: net::ERR_ABORTED at {string_target}"
    assert frame.url == before
    assert frame.title() == ""
    assert frame.evaluate("() => document.body.innerText") == "frame replacement"
    assert frame.content() == "<html><head></head><body><h1>frame replacement</h1></body></html>"


@case
def route_continue_overrides_request_headers(page):
    with header_case_server() as base_url:
        page.route(
            "**/echo-headers",
            lambda route: route.continue_(headers={"X-Route-Header": "from-route"}),
        )
        response = page.goto(f"{base_url}/echo-headers")

    assert response.json()["x-route-header"] == "from-route"


@case
def route_continue_rejects_protocol_change(page):
    with header_case_server() as base_url:
        errors = []
        goto_error = None

        def handler(route):
            try:
                route.continue_(url="https://example.com/changed")
            except Exception as exc:
                errors.append(str(exc).splitlines()[0])

        page.route("**/route-continue-protocol", handler)
        try:
            page.goto(f"{base_url}/route-continue-protocol", timeout=250)
        except Exception as exc:
            goto_error = str(exc).splitlines()[0]

    assert errors == ["Route.continue_: New URL must have same protocol as overridden URL"]
    assert goto_error is not None and "250" in goto_error


@case
def route_continue_invalid_url_validation_matches_playwright(page):
    with header_case_server() as base_url:
        errors = []
        goto_error = None

        def handler(route):
            try:
                route.continue_(url="/headers")
            except Exception as exc:
                errors.append(str(exc).splitlines()[0])

        page.route("**/route-continue-invalid-url", handler)
        try:
            page.goto(f"{base_url}/route-continue-invalid-url", timeout=250)
        except Exception as exc:
            goto_error = str(exc).splitlines()[0]

    assert errors == ["Route.continue_: Invalid URL"]
    assert goto_error is not None and "250" in goto_error


@case
def route_structured_post_data_encoding(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        page.route(
            "**/echo",
            lambda route: route.continue_(
                headers={"Content-Type": "application/json"},
                post_data={"patched": True},
            ),
        )
        continued = page.evaluate(
            """() => fetch('/echo', {
                method: 'POST',
                headers: { 'Content-Type': 'text/plain' },
                body: 'base'
            }).then(response => response.json())"""
        )
        page.unroute("**/echo")

        fetched = []

        def handler(route):
            upstream = route.fetch(
                headers={"Content-Type": "application/json"},
                post_data={"fetch": 7},
            )
            fetched.append(upstream.json())
            route.fulfill(response=upstream)

        page.route("**/echo", handler)
        fulfilled = page.evaluate(
            """() => fetch('/echo', {
                method: 'POST',
                headers: { 'Content-Type': 'text/plain' },
                body: 'base'
            }).then(response => response.json())"""
        )
        page.unroute("**/echo")

        fallback_requests = []

        def terminal(route, request):
            fallback_requests.append((request.post_data, request.post_data_json))
            route.continue_()

        page.route("**/echo", terminal)
        page.route(
            "**/echo",
            lambda route: route.fallback(
                headers={"Content-Type": "application/json"},
                post_data={"fallback": 3},
            ),
        )
        fallback_continued = page.evaluate(
            """() => fetch('/echo', {
                method: 'POST',
                headers: { 'Content-Type': 'text/plain' },
                body: 'base'
            }).then(response => response.json())"""
        )

    assert continued["content_type"] == "application/json"
    assert continued["body"] == '{"patched": true}'
    assert fetched[0]["content_type"] == "application/json"
    assert fetched[0]["body"] == '{"fetch": 7}'
    assert fulfilled == fetched[0]
    assert fallback_requests == [('{"fallback": 3}', {"fallback": 3})]
    assert fallback_continued["content_type"] == "application/json"
    assert fallback_continued["body"] == '{"fallback": 3}'


@case
def route_fallback_chains_matching_handlers(page):
    calls: list[tuple[str, str, str | None]] = []

    def terminal(route, request):
        calls.append(("terminal", request.url, request.header_value("x-fallback")))
        route.fulfill(json={"ok": True, "handler": "terminal"})

    def first(route, request):
        calls.append(("first", request.url, request.header_value("x-fallback")))
        route.fallback(headers={"X-Fallback": "yes"})

    page.route("**/fallback-chain", terminal)
    page.route("**/fallback-chain", first)

    response = page.goto("http://example.test/fallback-chain")

    assert response.json() == {"ok": True, "handler": "terminal"}
    assert calls == [
        ("first", "http://example.test/fallback-chain", None),
        ("terminal", "http://example.test/fallback-chain", "yes"),
    ]


@case
def route_fallback_url_override_rematches_next_handler(page):
    with header_case_server() as base_url:
        calls: list[tuple[str, str, str | None]] = []

        def terminal(route, request):
            calls.append(("terminal", request.url, request.header_value("x-route-header")))
            route.fulfill(
                json={
                    "handler": "terminal",
                    "url": request.url,
                    "header": request.header_value("x-route-header"),
                }
            )

        def first(route, request):
            calls.append(("first", request.url, request.header_value("x-route-header")))
            route.fallback(
                url=f"{base_url}/echo-headers",
                headers={"X-Route-Header": "from-fallback"},
            )

        page.route("**/echo-headers", terminal)
        page.route("**/headers", first)

        response = page.goto(f"{base_url}/headers")

    assert response.json() == {
        "handler": "terminal",
        "url": f"{base_url}/echo-headers",
        "header": "from-fallback",
    }
    assert calls == [
        ("first", f"{base_url}/headers", None),
        ("terminal", f"{base_url}/echo-headers", "from-fallback"),
    ]


@case
def route_fallback_method_override_preserves_non_string_until_terminal_handler(page):
    with header_case_server() as base_url:
        calls: list[tuple[str, str, str]] = []

        def terminal(route, request):
            method_type = type(request.method).__name__
            method_repr = repr(request.method)
            calls.append(("terminal", method_type, method_repr))
            route.fulfill(json={"method_type": method_type, "method_repr": method_repr})

        def first(route, request):
            calls.append(("first", type(request.method).__name__, repr(request.method)))
            route.fallback(method=123)

        page.route("**/headers", terminal)
        page.route("**/headers", first)

        response = page.goto(f"{base_url}/headers")

    assert response.json() == {"method_type": "int", "method_repr": "123"}
    assert calls == [
        ("first", "str", "'GET'"),
        ("terminal", "int", "123"),
    ]


@case
def route_fallback_header_override_preserves_non_string_in_headers_array(page):
    with header_case_server() as base_url:
        errors: list[list[str]] = []

        def terminal(route, request):
            picked = [
                header
                for header in request.headers_array()
                if header["name"].lower().startswith("x-fallback")
            ]
            for label, getter in (
                ("headers", lambda: request.headers),
                ("header_value", lambda: request.header_value("x-fallback-number")),
            ):
                try:
                    getter()
                except Exception as exc:
                    errors.append([label, type(exc).__name__, str(exc).splitlines()[0]])
            route.fulfill(json={"picked": picked, "errors": errors})

        def first(route, request):
            route.fallback(headers={"X-Fallback-Number": 123, "X-Fallback-None": None})

        page.route("**/headers", terminal)
        page.route("**/headers", first)

        response = page.goto(f"{base_url}/headers")

    assert response.json() == {
        "picked": [{"name": "X-Fallback-Number", "value": 123}],
        "errors": [
            ["headers", "TypeError", "sequence item 0: expected str instance, int found"],
            ["header_value", "TypeError", "sequence item 0: expected str instance, int found"],
        ],
    }


@case
def route_fallback_header_name_override_errors_match_playwright(page):
    with header_case_server() as base_url:
        errors: list[list[str]] = []

        def terminal(route, request):
            for label, getter in (
                ("headers_array", lambda: request.headers_array()),
                ("headers", lambda: request.headers),
                ("header_value", lambda: request.header_value("123")),
                ("all_headers", lambda: request.all_headers()),
            ):
                try:
                    getter()
                except Exception as exc:
                    errors.append([label, type(exc).__name__, str(exc).splitlines()[0]])
            route.fulfill(json={"errors": errors})

        def first(route, request):
            route.fallback(headers={123: "value", "X-Ok": "yes"})

        page.route("**/headers", terminal)
        page.route("**/headers", first)

        response = page.goto(f"{base_url}/headers")

    assert response.json() == {
        "errors": [
            ["headers_array", "AttributeError", "'int' object has no attribute 'lower'"],
            ["headers", "AttributeError", "'int' object has no attribute 'lower'"],
            ["header_value", "AttributeError", "'int' object has no attribute 'lower'"],
            ["all_headers", "AttributeError", "'int' object has no attribute 'lower'"],
        ]
    }


@case
def route_fallback_non_mapping_headers_override_errors_match_playwright(page):
    with header_case_server() as base_url:
        errors: list[list[str]] = []

        def terminal(route, request):
            for label, getter in (
                ("headers_array", lambda: request.headers_array()),
                ("headers", lambda: request.headers),
                ("header_value", lambda: request.header_value("x")),
                ("all_headers", lambda: request.all_headers()),
            ):
                try:
                    getter()
                except Exception as exc:
                    errors.append([label, type(exc).__name__, str(exc).splitlines()[0]])
            route.fulfill(json={"errors": errors})

        def first(route, request):
            route.fallback(headers=[("X", "Y")])

        page.route("**/headers", terminal)
        page.route("**/headers", first)

        response = page.goto(f"{base_url}/headers")

    assert response.json() == {
        "errors": [
            ["headers_array", "AttributeError", "'list' object has no attribute 'items'"],
            ["headers", "AttributeError", "'list' object has no attribute 'items'"],
            ["header_value", "AttributeError", "'list' object has no attribute 'items'"],
            ["all_headers", "AttributeError", "'list' object has no attribute 'items'"],
        ]
    }


@case
def route_fallback_empty_post_data_preserves_original_request_accessors(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        results = {}

        for label, post_data in (
            ("empty_string", ""),
            ("empty_bytes", b""),
            ("zero_int", 0),
            ("false_bool", False),
        ):
            def terminal(route, request, *, label=label):
                payload = {
                    "post_data": request.post_data,
                    "buffer_hex": None if request.post_data_buffer is None else request.post_data_buffer.hex(),
                }
                results[label] = payload
                route.fulfill(json=payload)

            def first(route, request, *, post_data=post_data):
                route.fallback(post_data=post_data)

            page.route("**/echo", terminal)
            page.route("**/echo", first)
            response_payload = page.evaluate(
                """async () => {
                    const response = await fetch('/echo', { method: 'POST', body: 'base' });
                    return await response.json();
                }"""
            )
            assert response_payload == results[label]
            page.unroute("**/echo")

    assert results == {
        "empty_string": {"post_data": "base", "buffer_hex": "62617365"},
        "empty_bytes": {"post_data": "base", "buffer_hex": "62617365"},
        "zero_int": {"post_data": "0", "buffer_hex": "30"},
        "false_bool": {"post_data": "false", "buffer_hex": "66616c7365"},
    }


@case
def route_fallback_marks_route_handled_for_current_handler(page):
    with header_case_server() as base_url:
        errors = []

        def handler(route):
            route.fallback(headers={"X-Route-Header": "from-fallback"})
            try:
                route.fulfill(json={"unexpected": True})
            except Exception as exc:
                errors.append(str(exc).splitlines()[0])

        page.route("**/echo-headers", handler)
        response = page.goto(f"{base_url}/echo-headers")

    assert response.json()["x-route-header"] == "from-fallback"
    assert errors == ["Route is already handled!"]


@case
def route_terminal_actions_mark_route_handled(page):
    with header_case_server() as base_url:
        errors = []

        def continue_handler(route):
            route.continue_()
            try:
                route.fulfill(json={"unexpected": True})
            except Exception as exc:
                errors.append(["continue", str(exc).splitlines()[0]])

        def fulfill_handler(route):
            route.fulfill(json={"ok": True})
            try:
                route.continue_()
            except Exception as exc:
                errors.append(["fulfill", str(exc).splitlines()[0]])

        page.route("**/echo-headers", continue_handler)
        continued = page.goto(f"{base_url}/echo-headers")
        continued_body = continued.json()
        page.unroute("**/echo-headers", continue_handler)
        page.route("**/echo-headers", fulfill_handler)
        fulfilled = page.goto(f"{base_url}/echo-headers")
        fulfilled_body = fulfilled.json()

    assert continued_body["x-route-header"] is None
    assert fulfilled_body == {"ok": True}
    assert errors == [
        ["continue", "Route is already handled!"],
        ["fulfill", "Route is already handled!"],
    ]


@case
def route_fetch_and_fulfill_response_patch(page):
    fetched = []

    with header_case_server() as base_url:
        def handler(route):
            upstream = route.fetch(headers={"X-Route-Fetch": "yes"})
            fetched.append(upstream.json())
            route.fulfill(response=upstream, status=202, json={"patched": upstream.json()})

        page.route("**/echo-headers", handler)
        response = page.goto(f"{base_url}/echo-headers")

    assert response.status == 202
    assert response.json()["patched"]["x-route-fetch"] == "yes"
    assert fetched[0]["x-route-header"] is None
    assert fetched[0]["x-route-fetch"] == "yes"


@case
def context_route_callable_url_predicate_receives_url_string(page):
    with header_case_server() as base_url:
        target_url = f"{base_url}/query?callable=1"
        seen: list[tuple[str, str]] = []
        handled: list[tuple[str, bool]] = []

        def matcher(url):
            seen.append((type(url).__name__, str(url)))
            return str(url).endswith("/query?callable=1")

        def handler(route):
            handled.append((route.request.url, route.request.is_navigation_request()))
            route.fulfill(status=200, json={"matched": True})

        page.context.route(matcher, handler)
        page.goto(f"{base_url}/page")
        result = page.evaluate(
            "async url => await fetch(url).then(response => response.json())",
            target_url,
        )

    assert result == {"matched": True}
    assert handled == [(target_url, False)]
    assert any(value == target_url for type_name, value in seen if type_name == "str")
    assert all(type_name == "str" for type_name, _ in seen)


@case
def route_fetch_headers_replace_original_request_headers(page):
    with header_case_server() as base_url:
        page.set_extra_http_headers(
            {
                "X-Route-Header": "original-route",
                "X-Route-Fetch": "original-fetch",
                "X-Extra": "original-extra",
            }
        )
        fetched = []

        def handler(route):
            upstream = route.fetch(
                url=f"{base_url}/echo-headers",
                headers={"X-Route-Fetch": None, "X-Extra": "override-extra"},
            )
            fetched.append(upstream.json())
            route.fulfill(response=upstream)

        page.route("**/route-fetch-replace-headers", handler)
        response = page.goto(f"{base_url}/route-fetch-replace-headers")

    assert fetched == [
        {
            "x-route-header": None,
            "x-route-fetch": None,
            "x-extra": "override-extra",
            "x-context": None,
            "x-page": None,
            "x-shared": None,
            "x-after": None,
            "referer": None,
            "user-agent": fetched[0]["user-agent"],
            "accept-language": None,
            "authorization": None,
        }
    ]
    assert response.json() == fetched[0]


@case
def route_fetch_user_agent_none_preserves_original_header(page):
    with header_case_server() as base_url:
        results = {}

        def handler(route):
            for label, headers in {
                "empty": {},
                "user_agent_none": {"User-Agent": None},
                "user_agent_custom": {"User-Agent": "RustwrightParityUA"},
            }.items():
                results[label] = route.fetch(url=f"{base_url}/echo-headers", headers=headers).json()
            route.fulfill(json=results)

        page.route("**/route-fetch-user-agent-none", handler)
        response = page.goto(f"{base_url}/route-fetch-user-agent-none")

    payload = response.json()
    assert payload["empty"]["user-agent"]
    assert payload["empty"]["user-agent"] != "Python-urllib/3.13"
    assert payload["user_agent_none"]["user-agent"] == payload["empty"]["user-agent"]
    assert payload["user_agent_custom"]["user-agent"] == "RustwrightParityUA"
    assert payload["user_agent_none"]["accept-language"] is None


@case
def route_fetch_empty_headers_preserve_post_request_headers(page):
    with header_case_server() as base_url:
        page.goto(base_url)
        results: dict[str, dict[str, str | None]] = {}

        def handler(route):
            for label, kwargs in {
                "no_headers": {},
                "empty_headers": {"headers": {}},
                "custom_header": {"headers": {"X-Test-Header": "override"}},
                "drop_content_type": {"headers": {"Content-Type": None}},
            }.items():
                upstream = route.fetch(url=f"{base_url}/echo", **kwargs)
                results[label] = upstream.json()
            route.fulfill(status=200, json=results)

        page.route("**/route-fetch-post-header-replace", handler)
        payload = page.evaluate(
            """async () => {
              const response = await fetch('/route-fetch-post-header-replace', {
                method: 'POST',
                headers: { 'Content-Type': 'text/plain', 'X-Test-Header': 'original' },
                body: 'base-body',
              });
              return await response.json();
            }"""
        )

    assert payload == {
        "no_headers": {"body": "base-body", "content_type": "text/plain", "x_test": "original"},
        "empty_headers": {"body": "base-body", "content_type": "text/plain", "x_test": "original"},
        "custom_header": {"body": "base-body", "content_type": "application/octet-stream", "x_test": "override"},
        "drop_content_type": {"body": "base-body", "content_type": "application/octet-stream", "x_test": None},
    }


@case
def route_fetch_replacement_headers_use_context_cookies(page):
    with header_case_server() as base_url:
        host = urlparse(base_url).hostname or "127.0.0.1"
        page.context.add_cookies(
            [
                {"name": "target_only", "value": "yes", "domain": host, "path": "/cookie-echo"},
                {"name": "route_only", "value": "yes", "domain": host, "path": "/route-fetch-cookie-probe"},
            ]
        )
        results: dict[str, dict[str, str]] = {}

        def handler(route):
            for label, kwargs in {
                "no_headers": {},
                "empty_headers": {"headers": {}},
                "custom_header": {"headers": {"X-Extra": "override"}},
                "cookie_none": {"headers": {"Cookie": None}},
                "cookie_custom": {"headers": {"Cookie": "manual=one"}},
            }.items():
                upstream = route.fetch(url=f"{base_url}/cookie-echo", **kwargs)
                results[label] = upstream.json()
            route.fulfill(status=200, json=results)

        page.route("**/route-fetch-cookie-probe", handler)
        response = page.goto(f"{base_url}/route-fetch-cookie-probe")

    assert response.json() == {
        "no_headers": {"cookie": "route_only=yes"},
        "empty_headers": {"cookie": "route_only=yes"},
        "custom_header": {"cookie": "target_only=yes"},
        "cookie_none": {"cookie": "target_only=yes"},
        "cookie_custom": {"cookie": "manual=one"},
    }


@case
def route_fetch_set_cookie_updates_browser_context(page):
    with header_case_server() as base_url:
        fetched: list[dict[str, object]] = []

        def handler(route):
            upstream = route.fetch(url=f"{base_url}/set-cookies")
            fetched.append({"status": upstream.status, "set_cookie": upstream.headers.get("set-cookie")})
            route.fulfill(status=200, json={"status": upstream.status})

        page.route("**/route-fetch-set-cookies", handler)
        first = page.goto(f"{base_url}/route-fetch-set-cookies").json()
        echo = page.goto(f"{base_url}/cookie-echo").json()
        cookies = {cookie["name"]: cookie["value"] for cookie in page.context.cookies([base_url])}

    assert first == {"status": 200}
    assert fetched == [{"status": 200, "set_cookie": "first=one; Path=/\nsecond=two; Path=/"}]
    assert echo == {"cookie": "first=one; second=two"}
    assert cookies == {"first": "one", "second": "two"}


@case
def route_fetch_honors_context_http_credentials(page, playwright):
    outer_browser = page.context.browser
    try:
        page.close()
    except Exception:
        pass
    if outer_browser is not None:
        try:
            outer_browser.close()
        except Exception:
            pass
    expected = "Basic " + base64.b64encode(b"user:pass").decode("ascii")
    browser = playwright.chromium.launch(headless=True)

    def run_scenario(label: str, credentials: dict[str, object]) -> dict[str, object]:
        with header_case_server() as base_url:
            context = browser.new_context(http_credentials=credentials)
            try:
                context_page = context.new_page()
                result: dict[str, object] = {}

                def handler(route):
                    upstream = route.fetch(url=f"{base_url}/basic-auth-challenge")
                    result["status"] = upstream.status
                    if upstream.status == 200:
                        result["body"] = upstream.json()
                    else:
                        result["body"] = upstream.text()
                    route.fulfill(status=200, json=result)

                context_page.route(f"**/route-fetch-basic-auth-{label}", handler)
                return context_page.goto(f"{base_url}/route-fetch-basic-auth-{label}").json()
            finally:
                context.close()

    try:
        default = run_scenario("default", {"username": "user", "password": "pass"})
        send_always = run_scenario("send-always", {"username": "user", "password": "pass", "send": "always"})
        wrong = run_scenario("wrong", {"username": "wrong", "password": "pass"})
        wrong_origin = run_scenario(
            "wrong-origin",
            {"username": "user", "password": "pass", "origin": "http://127.0.0.1:1"},
        )
    finally:
        browser.close()

    assert default == {"status": 200, "body": {"authorization": expected, "attempts": 2}}
    assert send_always == {"status": 200, "body": {"authorization": expected, "attempts": 1}}
    assert wrong == {"status": 401, "body": ""}
    assert wrong_origin == {"status": 401, "body": ""}


@case
def route_fetch_honors_context_ignore_https_errors(page):
    browser = page.context.browser
    assert browser is not None
    with header_case_server() as http_base, https_case_server() as https_base:

        def run_scenario(label: str, context_options: dict[str, object]) -> dict[str, object]:
            context = browser.new_context(**context_options)
            try:
                context_page = context.new_page()
                result: dict[str, object] = {}

                def handler(route):
                    try:
                        upstream = route.fetch(url=f"{https_base}/secure")
                        result["status"] = upstream.status
                        result["json"] = upstream.json()
                    except Exception as exc:
                        result["error"] = type(exc).__name__
                    route.fulfill(status=200, json=result)

                context_page.route(f"**/route-fetch-https-{label}", handler)
                return context_page.goto(f"{http_base}/route-fetch-https-{label}").json()
            finally:
                context.close()

        default = run_scenario("default", {})
        ignore = run_scenario("ignore", {"ignore_https_errors": True})

    assert default == {"error": "Error"}
    assert ignore == {"status": 200, "json": {"secure": True, "path": "/secure"}}


@case
def route_fetch_honors_context_proxy(page):
    browser = page.context.browser
    assert browser is not None
    with header_case_server() as http_base, http_proxy_case_server() as (proxy_url, proxy_seen):
        context = browser.new_context(proxy={"server": proxy_url, "bypass": "127.0.0.1"})
        try:
            context_page = context.new_page()
            result: dict[str, object] = {}

            def handler(route):
                proxied = route.fetch(url="http://route-fetch-proxy.invalid/proxied")
                bypassed = route.fetch(url=f"{http_base}/headers")
                result["proxied"] = proxied.json()
                result["bypassed"] = bypassed.json()
                route.fulfill(status=200, json=result)

            context_page.route("**/route-fetch-proxy-trigger", handler)
            payload = context_page.goto("http://route-fetch-trigger.invalid/route-fetch-proxy-trigger").json()
        finally:
            context.close()

    assert payload == {
        "proxied": {
            "proxied": True,
            "url": "http://route-fetch-proxy.invalid/proxied",
            "host": "route-fetch-proxy.invalid",
        },
        "bypassed": {"ok": True},
    }
    assert proxy_seen_for_url(proxy_seen, "http://route-fetch-proxy.invalid/proxied") == [
        {"url": "http://route-fetch-proxy.invalid/proxied", "host": "route-fetch-proxy.invalid"}
    ]


def route_fetch_honors_context_proxy_credentials(page):
    browser = page.context.browser
    assert browser is not None
    with authenticated_http_proxy_case_server() as (proxy_url, proxy_seen, expected_auth):
        context = browser.new_context(
            proxy={"server": proxy_url, "username": "user", "password": "pass"}
        )
        try:
            context_page = context.new_page()
            result: dict[str, object] = {}

            def handler(route):
                proxied = route.fetch(url="http://route-fetch-proxy-auth.invalid/proxied")
                result["proxied"] = proxied.json()
                route.fulfill(status=200, json=result)

            context_page.route("**/route-fetch-proxy-auth-trigger", handler)
            payload = context_page.goto(
                "http://route-fetch-trigger.invalid/route-fetch-proxy-auth-trigger"
            ).json()
        finally:
            context.close()

    assert payload == {
        "proxied": {
            "proxied": True,
            "url": "http://route-fetch-proxy-auth.invalid/proxied",
            "host": "route-fetch-proxy-auth.invalid",
            "proxy_authorization": expected_auth,
        }
    }
    assert any(
        entry
        == {
            "url": "http://route-fetch-proxy-auth.invalid/proxied",
            "host": "route-fetch-proxy-auth.invalid",
            "proxy_authorization": expected_auth,
        }
        for entry in proxy_seen
    )


@case
def route_fetch_honors_context_base_url(page):
    browser = page.context.browser
    assert browser is not None
    with header_case_server() as base_url:
        context = browser.new_context(base_url=f"{base_url}/base/")
        try:
            context_page = context.new_page()
            result: dict[str, object] = {}

            def handler(route):
                absolute = route.fetch(url="/headers")
                parent = route.fetch(url="../query?from=route-fetch")
                result["absolute"] = {"status": absolute.status, "url": absolute.url, "json": absolute.json()}
                result["parent"] = {"status": parent.status, "url": parent.url, "json": parent.json()}
                route.fulfill(status=200, json=result)

            context_page.route("**/route-fetch-base-url-trigger", handler)
            payload = context_page.goto("route-fetch-base-url-trigger").json()
        finally:
            context.close()

    assert payload == {
        "absolute": {"status": 200, "url": f"{base_url}/headers", "json": {"ok": True}},
        "parent": {
            "status": 200,
            "url": f"{base_url}/query?from=route-fetch",
            "json": {"path": "/query", "query": {"from": ["route-fetch"]}},
        },
    }


@case
def route_fetch_post_data_encoding_matches_playwright(page):
    with header_case_server() as base_url:
        results: dict[str, dict[str, str | None]] = {}

        def handler(route):
            for label, kwargs in {
                "empty_str": {"method": "POST", "post_data": ""},
                "empty_bytes": {"method": "POST", "post_data": b""},
                "zero": {"method": "POST", "post_data": 0},
                "body": {"method": "POST", "post_data": "body"},
            }.items():
                results[label] = route.fetch(url=f"{base_url}/echo", **kwargs).json()
            route.fulfill(status=200, json=results)

        page.route("**/route-fetch-post-data", handler)
        response = page.goto(f"{base_url}/route-fetch-post-data")

    assert response.json() == {
        "empty_str": {"body": "", "content_type": None, "x_test": None},
        "empty_bytes": {"body": "", "content_type": None, "x_test": None},
        "zero": {"body": "0", "content_type": "application/json", "x_test": None},
        "body": {"body": "body", "content_type": "application/octet-stream", "x_test": None},
    }


@case
def route_fetch_relative_url_validation_matches_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    with header_case_server() as base_url:
        errors: list[list[str]] = []

        def handler(route):
            for value in ["/json", "json", "./json", "../json"]:
                try:
                    route.fetch(url=value)
                except sync_api.Error as exc:
                    errors.append([value, str(exc).splitlines()[0]])
                else:
                    raise AssertionError(f"route.fetch(url={value!r}) unexpectedly succeeded")
            route.fulfill(status=200, json={"errors": errors})

        page.route("**/route-fetch-relative-url-validation", handler)
        response = page.goto(f"{base_url}/route-fetch-relative-url-validation")

    assert response.json()["errors"] == [
        ["/json", "Route.fetch: Invalid URL"],
        ["json", "Route.fetch: Invalid URL"],
        ["./json", "Route.fetch: Invalid URL"],
        ["../json", "Route.fetch: Invalid URL"],
    ]


@case
def route_fetch_unsupported_protocol_errors_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    with header_case_server() as base_url:
        errors: list[list[str]] = []

        def handler(route):
            for value in [
                "ftp://example.com/file",
                "data:text/plain,hi",
                "localhost:123/path",
                "http://",
                "//example.com/path",
            ]:
                try:
                    route.fetch(url=value)
                except sync_api.Error as exc:
                    errors.append([value, str(exc).splitlines()[0]])
                else:
                    raise AssertionError(f"route.fetch(url={value!r}) unexpectedly succeeded")
            route.fulfill(status=200, json={"errors": errors})

        page.route("**/route-fetch-unsupported-protocol-validation", handler)
        response = page.goto(f"{base_url}/route-fetch-unsupported-protocol-validation")

    assert response.json()["errors"] == [
        ["ftp://example.com/file", 'Route.fetch: Protocol "ftp:" not supported. Expected "http:"'],
        ["data:text/plain,hi", 'Route.fetch: Protocol "data:" not supported. Expected "http:"'],
        ["localhost:123/path", 'Route.fetch: Protocol "localhost:" not supported. Expected "http:"'],
        ["http://", "Route.fetch: Invalid URL"],
        ["//example.com/path", "Route.fetch: Invalid URL"],
    ]


@case
def route_fetch_negative_timeout_matches_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    with header_case_server() as base_url:
        errors: list[list[float | int | str]] = []

        def handler(route):
            for timeout in (-1,):
                try:
                    route.fetch(url=f"{base_url}/slow-headers", timeout=timeout)
                except sync_api.TimeoutError as exc:
                    errors.append([timeout, type(exc).__name__, str(exc).splitlines()[0]])
                else:
                    raise AssertionError(f"route.fetch(timeout={timeout!r}) unexpectedly succeeded")
            accepted = route.fetch(url=f"{base_url}/headers", timeout=0).status
            route.fulfill(status=200, json={"errors": errors, "accepted": accepted})

        page.route("**/route-fetch-negative-timeout", handler)
        response = page.goto(f"{base_url}/route-fetch-negative-timeout")

    assert response.json() == {
        "errors": [
            [-1, "TimeoutError", "Route.fetch: Timeout -1ms exceeded."],
        ],
        "accepted": 200,
    }


@case
def route_fetch_honors_max_redirects(page):
    sync_api = importlib.import_module(
        f"{page.__class__.__module__.split('.', 1)[0]}.sync_api"
    )

    with header_case_server() as base_url:
        seen = []

        def no_redirect_handler(route):
            upstream = route.fetch(url=f"{base_url}/redirect-one", max_redirects=0)
            seen.append(("no-redirect", upstream.status, upstream.url))
            route.fulfill(status=200, json={"status": upstream.status, "url": upstream.url})

        page.route("**/route-fetch-no-redirect", no_redirect_handler)
        no_redirect = page.goto(f"{base_url}/route-fetch-no-redirect")
        no_redirect_json = no_redirect.json()

        errors = []

        def limited_redirect_handler(route):
            try:
                route.fetch(url=f"{base_url}/redirect-hop-one", max_redirects=1)
            except sync_api.Error as exc:
                errors.append(str(exc))
                route.fulfill(status=200, json={"error": str(exc)})
                return
            raise AssertionError("route.fetch(max_redirects=1) should fail after one redirect")

        page.route("**/route-fetch-limited-redirect", limited_redirect_handler)
        limited = page.goto(f"{base_url}/route-fetch-limited-redirect")

    assert no_redirect_json == {"status": 302, "url": f"{base_url}/redirect-one"}
    assert seen == [("no-redirect", 302, f"{base_url}/redirect-one")]
    assert "Max redirect count exceeded" in limited.json()["error"]
    assert any("Max redirect count exceeded" in error for error in errors)


@case
def route_abort_emits_requestfailed_event(page):
    page.route("**/blocked", lambda route: route.abort())
    page.set_content(
        """
        <button id="go" onclick="fetch('http://example.test/blocked').catch(() => {})">Go</button>
        """
    )

    with page.expect_event("requestfailed", lambda request: request.url.endswith("/blocked")) as request_info:
        page.click("#go")

    assert request_info.value.url == "http://example.test/blocked"
    assert request_info.value.failure


@case
def locator_select_text_and_file_payloads(page):
    page.set_content(
        """
        <p id="copy">Alpha Beta</p>
        <input id="copy-input" value="InputText">
        <textarea id="copy-textarea">TextArea</textarea>
        <p id="hidden-copy" style="display:none">Hidden Text</p>
        <input id="file" type="file" multiple>
        """
    )

    page.locator("#copy").select_text()
    assert page.evaluate("() => getSelection().toString()") == "Alpha Beta"
    page.locator("#copy-input").select_text()
    assert page.evaluate("() => getSelection().toString()") == "InputText"
    assert page.evaluate("() => document.querySelector('#copy-input').selectionStart") == 0
    assert page.evaluate("() => document.querySelector('#copy-input').selectionEnd") == len("InputText")
    page.locator("#copy-textarea").select_text()
    assert page.evaluate("() => getSelection().toString()") == "TextArea"
    assert page.evaluate("() => document.querySelector('#copy-textarea').selectionStart") == 0
    assert page.evaluate("() => document.querySelector('#copy-textarea').selectionEnd") == len("TextArea")
    try:
        page.locator("#hidden-copy").select_text(timeout=300)
    except Exception:
        pass
    else:
        raise AssertionError("hidden select_text unexpectedly succeeded without force")
    page.locator("#hidden-copy").select_text(force=True)

    assert page.locator("#file").set_input_files(
        [
            {"name": "alpha.txt", "mimeType": "text/plain", "buffer": b"alpha"},
            {"name": "beta.txt", "mimeType": "text/plain", "buffer": b"beta"},
        ]
    ) is None

    assert page.evaluate(
        "async () => Promise.all(Array.from(document.querySelector('#file').files, async file => `${file.name}:${await file.text()}`))"
    ) == ["alpha.txt:alpha", "beta.txt:beta"]


@case
def direct_keyboard_and_mouse_devices(page):
    page.set_content(
        """
        <input id="field">
        <script>
        window.keyEvents = [];
        document.querySelector('#field').addEventListener('keydown', event => {
          window.keyEvents.push({ key: event.key, repeat: event.repeat });
        });
        document.addEventListener('wheel', event => {
          document.body.dataset.wheelDelta = String(event.deltaY);
        });
        </script>
        """
    )

    page.focus("#field")
    page.keyboard.type("ab")
    page.keyboard.press("Shift+C")
    page.keyboard.press("Backspace")
    page.keyboard.insert_text("é✓")
    page.keyboard.down("x")
    page.keyboard.down("x")
    page.keyboard.up("x")
    page.keyboard.press("Home")
    page.keyboard.press("Y")
    page.keyboard.press("End")
    page.keyboard.press("Z")
    page.mouse.wheel(0, 45)

    assert page.input_value("#field") == "Yabé✓xxZ"
    assert page.evaluate("() => window.keyEvents") == [
        {"key": "a", "repeat": False},
        {"key": "b", "repeat": False},
        {"key": "Shift", "repeat": False},
        {"key": "C", "repeat": False},
        {"key": "Backspace", "repeat": False},
        {"key": "x", "repeat": False},
        {"key": "x", "repeat": True},
        {"key": "Home", "repeat": False},
        {"key": "Y", "repeat": False},
        {"key": "End", "repeat": False},
        {"key": "Z", "repeat": False},
    ]
    deadline = time.monotonic() + 2
    while page.evaluate("document.body.dataset.wheelDelta") != "45" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert page.evaluate("document.body.dataset.wheelDelta") == "45"


@case
def keyboard_punctuation_key_metadata(page):
    page.set_content(
        """
        <input id="field">
        <script>
        window.keyEvents = [];
        document.querySelector('#field').addEventListener('keydown', event => {
          window.keyEvents.push({
            key: event.key,
            code: event.code,
            keyCode: event.keyCode,
            which: event.which,
            shift: event.shiftKey
          });
        });
        </script>
        """
    )

    page.focus("#field")
    for key in ["-", "=", "[", "]", "\\", ";", "'", ",", ".", "/", "`", "!", "@", "?"]:
        page.keyboard.press(key)

    assert page.input_value("#field") == "-=[]\\;',./`!@?"
    assert page.evaluate("() => window.keyEvents") == [
        {"key": "-", "code": "Minus", "keyCode": 189, "which": 189, "shift": False},
        {"key": "=", "code": "Equal", "keyCode": 187, "which": 187, "shift": False},
        {"key": "[", "code": "BracketLeft", "keyCode": 219, "which": 219, "shift": False},
        {"key": "]", "code": "BracketRight", "keyCode": 221, "which": 221, "shift": False},
        {"key": "\\", "code": "Backslash", "keyCode": 220, "which": 220, "shift": False},
        {"key": ";", "code": "Semicolon", "keyCode": 186, "which": 186, "shift": False},
        {"key": "'", "code": "Quote", "keyCode": 222, "which": 222, "shift": False},
        {"key": ",", "code": "Comma", "keyCode": 188, "which": 188, "shift": False},
        {"key": ".", "code": "Period", "keyCode": 190, "which": 190, "shift": False},
        {"key": "/", "code": "Slash", "keyCode": 191, "which": 191, "shift": False},
        {"key": "`", "code": "Backquote", "keyCode": 192, "which": 192, "shift": False},
        {"key": "!", "code": "Digit1", "keyCode": 49, "which": 49, "shift": False},
        {"key": "@", "code": "Digit2", "keyCode": 50, "which": 50, "shift": False},
        {"key": "?", "code": "Slash", "keyCode": 191, "which": 191, "shift": False},
    ]


@case
def keyboard_function_key_metadata(page):
    page.set_content(
        """
        <input id="field">
        <script>
        window.keyEvents = [];
        document.querySelector('#field').addEventListener('keydown', event => {
          window.keyEvents.push({
            key: event.key,
            code: event.code,
            keyCode: event.keyCode,
            which: event.which,
            shift: event.shiftKey
          });
        });
        </script>
        """
    )

    page.focus("#field")
    for key in ["F1", "F5", "F12"]:
        page.keyboard.press(key)

    assert page.input_value("#field") == ""
    assert page.evaluate("() => window.keyEvents") == [
        {"key": "F1", "code": "F1", "keyCode": 112, "which": 112, "shift": False},
        {"key": "F5", "code": "F5", "keyCode": 116, "which": 116, "shift": False},
        {"key": "F12", "code": "F12", "keyCode": 123, "which": 123, "shift": False},
    ]


@case
def keyboard_extended_key_locations(page):
    page.set_content(
        """
        <input id="field">
        <script>
        window.keyEvents = [];
        document.querySelector('#field').addEventListener('keydown', event => {
          window.keyEvents.push({
            key: event.key,
            code: event.code,
            keyCode: event.keyCode,
            which: event.which,
            location: event.location
          });
        });
        </script>
        """
    )

    page.focus("#field")
    for key in [
        "AltLeft",
        "AltRight",
        "ControlLeft",
        "ControlRight",
        "ShiftLeft",
        "ShiftRight",
        "MetaLeft",
        "MetaRight",
        "CapsLock",
        "NumLock",
        "ScrollLock",
        "PrintScreen",
        "Pause",
        "ContextMenu",
        "Numpad0",
        "Numpad1",
        "NumpadAdd",
        "NumpadSubtract",
        "NumpadMultiply",
        "NumpadDivide",
        "NumpadDecimal",
        "NumpadEnter",
    ]:
        page.keyboard.press(key)

    assert page.input_value("#field") == "+-*/"
    assert page.evaluate("() => window.keyEvents") == [
        {"key": "Alt", "code": "AltLeft", "keyCode": 18, "which": 18, "location": 1},
        {"key": "Alt", "code": "AltRight", "keyCode": 18, "which": 18, "location": 2},
        {"key": "Control", "code": "ControlLeft", "keyCode": 17, "which": 17, "location": 1},
        {"key": "Control", "code": "ControlRight", "keyCode": 17, "which": 17, "location": 2},
        {"key": "Shift", "code": "ShiftLeft", "keyCode": 16, "which": 16, "location": 1},
        {"key": "Shift", "code": "ShiftRight", "keyCode": 16, "which": 16, "location": 2},
        {"key": "Meta", "code": "MetaLeft", "keyCode": 91, "which": 91, "location": 1},
        {"key": "Meta", "code": "MetaRight", "keyCode": 92, "which": 92, "location": 2},
        {"key": "CapsLock", "code": "CapsLock", "keyCode": 20, "which": 20, "location": 0},
        {"key": "NumLock", "code": "NumLock", "keyCode": 144, "which": 144, "location": 0},
        {"key": "ScrollLock", "code": "ScrollLock", "keyCode": 145, "which": 145, "location": 0},
        {"key": "PrintScreen", "code": "PrintScreen", "keyCode": 44, "which": 44, "location": 0},
        {"key": "Pause", "code": "Pause", "keyCode": 19, "which": 19, "location": 0},
        {"key": "ContextMenu", "code": "ContextMenu", "keyCode": 93, "which": 93, "location": 0},
        {"key": "Insert", "code": "Numpad0", "keyCode": 45, "which": 45, "location": 3},
        {"key": "End", "code": "Numpad1", "keyCode": 35, "which": 35, "location": 3},
        {"key": "+", "code": "NumpadAdd", "keyCode": 107, "which": 107, "location": 3},
        {"key": "-", "code": "NumpadSubtract", "keyCode": 109, "which": 109, "location": 3},
        {"key": "*", "code": "NumpadMultiply", "keyCode": 106, "which": 106, "location": 3},
        {"key": "/", "code": "NumpadDivide", "keyCode": 111, "which": 111, "location": 3},
        {"key": "\x00", "code": "NumpadDecimal", "keyCode": 46, "which": 46, "location": 3},
        {"key": "Enter", "code": "NumpadEnter", "keyCode": 13, "which": 13, "location": 3},
    ]


@case
def keyboard_physical_code_names(page):
    page.set_content(
        """
        <input id="field">
        <script>
        window.keyEvents = [];
        document.querySelector('#field').addEventListener('keydown', event => {
          window.keyEvents.push({
            key: event.key,
            code: event.code,
            keyCode: event.keyCode,
            which: event.which,
            location: event.location,
            shift: event.shiftKey
          });
        });
        </script>
        """
    )

    page.focus("#field")
    for key in [
        "KeyA",
        "Digit1",
        "Slash",
        "Backquote",
        "Shift+KeyA",
        "Shift+Digit1",
        "Shift+Slash",
        "Shift+Backquote",
    ]:
        page.keyboard.press(key)

    assert page.input_value("#field") == "a1/`A!?~"
    assert page.evaluate("() => window.keyEvents") == [
        {"key": "a", "code": "KeyA", "keyCode": 65, "which": 65, "location": 0, "shift": False},
        {"key": "1", "code": "Digit1", "keyCode": 49, "which": 49, "location": 0, "shift": False},
        {"key": "/", "code": "Slash", "keyCode": 191, "which": 191, "location": 0, "shift": False},
        {"key": "`", "code": "Backquote", "keyCode": 192, "which": 192, "location": 0, "shift": False},
        {"key": "Shift", "code": "ShiftLeft", "keyCode": 16, "which": 16, "location": 1, "shift": True},
        {"key": "A", "code": "KeyA", "keyCode": 65, "which": 65, "location": 0, "shift": True},
        {"key": "Shift", "code": "ShiftLeft", "keyCode": 16, "which": 16, "location": 1, "shift": True},
        {"key": "!", "code": "Digit1", "keyCode": 49, "which": 49, "location": 0, "shift": True},
        {"key": "Shift", "code": "ShiftLeft", "keyCode": 16, "which": 16, "location": 1, "shift": True},
        {"key": "?", "code": "Slash", "keyCode": 191, "which": 191, "location": 0, "shift": True},
        {"key": "Shift", "code": "ShiftLeft", "keyCode": 16, "which": 16, "location": 1, "shift": True},
        {"key": "~", "code": "Backquote", "keyCode": 192, "which": 192, "location": 0, "shift": True},
    ]


@case
def keyboard_unknown_keys_and_unicode_type(page):
    page.set_content(
        """
        <input id="field">
        <script>
        window.keyEvents = [];
        document.querySelector('#field').addEventListener('keydown', event => {
          window.keyEvents.push(event.key);
        });
        </script>
        """
    )

    page.focus("#field")
    page.keyboard.type("aé✓")

    errors = []
    for method, key in [
        (page.keyboard.press, "Esc"),
        (page.keyboard.press, "é"),
        (page.keyboard.press, "Ctrl+A"),
        (page.keyboard.down, "NoSuchKey"),
        (page.keyboard.up, "NoSuchKey"),
    ]:
        try:
            method(key)
        except Exception as exc:
            errors.append(str(exc).splitlines()[0])

    assert page.input_value("#field") == "aé✓"
    assert page.evaluate("() => window.keyEvents") == ["a"]
    assert errors == [
        'Keyboard.press: Unknown key: "Esc"',
        'Keyboard.press: Unknown key: "é"',
        'Keyboard.press: Unknown key: "Ctrl"',
        'Keyboard.down: Unknown key: "NoSuchKey"',
        'Keyboard.up: Unknown key: "NoSuchKey"',
    ]


@case
def page_emulate_media_applies_common_features(page):
    page.emulate_media(
        color_scheme="dark",
        reduced_motion="reduce",
        forced_colors="active",
        contrast="more",
    )

    assert page.evaluate(
        """() => ({
        dark: matchMedia('(prefers-color-scheme: dark)').matches,
        reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
        forced: matchMedia('(forced-colors: active)').matches,
        contrast: matchMedia('(prefers-contrast: more)').matches,
        })"""
    ) == {"dark": True, "reduced": True, "forced": True, "contrast": True}


@case
def js_handle_and_element_handle_arguments(page):
    page.set_content("<button id='go' data-kind='primary'>Go</button>")
    element = page.query_selector("#go")
    data = page.evaluate_handle("() => ({ count: 4, suffix: '!' })")

    try:
        assert page.evaluate(
            "(arg) => ({ text: arg.node.textContent + arg.data.suffix, total: arg.data.count + arg.node.id.length })",
            {"node": element, "data": data},
        ) == {"text": "Go!", "total": 6}

        mapped = data.evaluate_handle(
            "(value, arg) => ({ text: arg.node.textContent, total: value.count + arg.node.id.length })",
            {"node": element},
        )
        try:
            assert mapped.json_value() == {"text": "Go", "total": 6}
        finally:
            mapped.dispose()
    finally:
        data.dispose()


@case
def frame_evaluate_accepts_nested_element_handle_arguments_like_skyvern(page):
    child = (
        "<main id='root'>"
        "<button id='go' data-kind='primary'>Go</button>"
        "<span id='tail'>Tail</span>"
        "</main>"
    )
    page.set_content(f'<iframe name="child" srcdoc="{escape(child, quote=True)}"></iframe>')
    frame = next(existing for existing in page.frames if existing is not page.main_frame)
    root = frame.query_selector("#root")
    button = frame.query_selector("#go")
    tail = frame.query_selector("#tail")

    assert frame.evaluate(
        "([element, page_by_page]) => element.textContent + ':' + page_by_page",
        [button, True],
    ) == "Go:true"
    assert frame.evaluate(
        "async ([frameName, element, interactable]) => ({ frameName, id: element.id, interactable })",
        ["child", button, False],
    ) == {"frameName": "child", "id": "go", "interactable": False}
    assert frame.evaluate(
        "([parent, child, suffix]) => parent.contains(child) && suffix.textContent",
        [root, button, tail],
    ) == "Tail"


@case
def locator_and_element_handle_evaluate_accept_handle_arguments(page):
    page.set_content("<button id='go' data-kind='primary'>Go</button><span id='tail'>Tail</span>")
    button = page.query_selector("#go")
    tail = page.query_selector("#tail")
    data = page.evaluate_handle("() => ({ count: 4, suffix: '!' })")

    try:
        assert page.locator("#go").evaluate(
            "(node, arg) => node.textContent + arg.data.suffix + ':' + arg.tail.textContent",
            {"data": data, "tail": tail},
        ) == "Go!:Tail"

        locator_result = page.locator("#go").evaluate_handle(
            "(node, arg) => ({ text: node.textContent, total: arg.data.count + arg.tail.id.length })",
            {"data": data, "tail": tail},
        )
        try:
            assert locator_result.json_value() == {"text": "Go", "total": 8}
        finally:
            locator_result.dispose()

        assert button.evaluate(
            "(node, arg) => node.dataset.kind + ':' + arg.data.suffix + arg.tail.id",
            {"data": data, "tail": tail},
        ) == "primary:!tail"

        element_result = button.evaluate_handle(
            "(node, arg) => ({ id: node.id, label: arg.tail.textContent, total: arg.data.count + node.id.length })",
            {"data": data, "tail": tail},
        )
        try:
            assert element_result.json_value() == {"id": "go", "label": "Tail", "total": 6}
        finally:
            element_result.dispose()
    finally:
        data.dispose()


@case
def locator_evaluate_all_accepts_handle_arguments(page):
    page.set_content("<ul><li>A</li><li>B</li></ul><span id='suffix'>!</span>")
    suffix = page.query_selector("#suffix")
    data = page.evaluate_handle("() => ({ prefix: 'x' })")

    try:
        assert page.locator("li").evaluate_all(
            "(items, arg) => items.map(item => arg.data.prefix + item.textContent + arg.suffix.textContent)",
            {"data": data, "suffix": suffix},
        ) == ["xA!", "xB!"]
    finally:
        data.dispose()


@case
def add_script_style_tags_and_wait_for_function(page):
    page.set_content("<html><head></head><body><div id='box'>Box</div><iframe></iframe></body></html>")

    page.add_script_tag(content="window.__tagValue = 7")
    page.add_style_tag(content="#box { color: rgb(1, 2, 3); }")
    page.evaluate("() => setTimeout(() => { window.__ready = true }, 25)")

    assert page.evaluate("window.__tagValue") == 7
    handle = page.wait_for_function("() => window.__ready === true", timeout=3_000)
    try:
        assert handle.json_value() is True
    finally:
        handle.dispose()
    assert page.evaluate("getComputedStyle(document.querySelector('#box')).color") == "rgb(1, 2, 3)"

    with slow_body_server() as base_url:
        page.add_script_tag(url=f"{base_url}/delayed-script.js")
        page.add_style_tag(url=f"{base_url}/delayed-style.css")

    assert page.evaluate("window.__delayedScriptTag") == "loaded"
    assert page.evaluate("getComputedStyle(document.querySelector('#box')).backgroundColor") == "rgb(4, 5, 6)"

    def expect_error(callback, message):
        try:
            callback()
        except Exception as exc:
            assert str(exc).splitlines()[0] == message
        else:
            raise AssertionError(f"expected error {message!r}")

    with tempfile.TemporaryDirectory() as directory:
        script_path = Path(directory) / "tag.js"
        script_path.write_text("window.__pathScript = 11", encoding="utf-8")
        style_path = Path(directory) / "tag.css"
        style_path.write_text("body { --path-style: 22; }", encoding="utf-8")
        frame = page.frames[1]

        for target, prefix in [(page, "Page"), (frame, "Frame")]:
            expect_error(
                lambda target=target: target.add_script_tag(),
                f"{prefix}.add_script_tag: Provide an object with a `url`, `path` or `content` property",
            )
            expect_error(
                lambda target=target: target.add_style_tag(),
                f"{prefix}.add_style_tag: Provide an object with a `url`, `path` or `content` property",
            )
            script = target.add_script_tag(content="window.__ignoredContentScript = 1", path=script_path)
            try:
                assert script.evaluate("node => node.textContent") == f"window.__pathScript = 11\n//# sourceURL={script_path}"
            finally:
                script.dispose()
            assert target.evaluate("() => window.__pathScript") == 11
            assert target.evaluate("() => window.__ignoredContentScript") is None

            url_script = target.add_script_tag(
                content="window.__ignoredUrlContentScript = 1",
                path=script_path,
                url="data:text/javascript,window.__urlScript=31",
            )
            try:
                assert url_script.evaluate("node => node.src") == "data:text/javascript,window.__urlScript=31"
                assert url_script.evaluate("node => node.textContent") == ""
            finally:
                url_script.dispose()
            assert target.evaluate("() => window.__urlScript") == 31
            assert target.evaluate("() => window.__ignoredUrlContentScript") is None

            style = target.add_style_tag(content="body { --ignored-content-style: 1; }", path=style_path)
            try:
                assert style.evaluate("node => node.textContent") == f"body {{ --path-style: 22; }}\n/*# sourceURL={style_path}*/"
            finally:
                style.dispose()
            assert target.evaluate("() => getComputedStyle(document.body).getPropertyValue('--path-style').trim()") == "22"

            url_style = target.add_style_tag(
                content="body { --ignored-url-content-style: 1; }",
                path=style_path,
                url="data:text/css,body%7B--url-style:32%7D",
            )
            try:
                assert url_style.evaluate("node => node.tagName") == "LINK"
                assert url_style.evaluate("node => node.href") == "data:text/css,body%7B--url-style:32%7D"
            finally:
                url_style.dispose()
            assert target.evaluate("() => getComputedStyle(document.body).getPropertyValue('--url-style').trim()") == "32"
            assert target.evaluate("() => getComputedStyle(document.body).getPropertyValue('--ignored-url-content-style').trim()") == ""

        expect_error(
            lambda: page.add_script_tag(foo="bar"),
            "Page.add_script_tag() got an unexpected keyword argument 'foo'",
        )
        expect_error(
            lambda: frame.add_script_tag(foo="bar"),
            "Frame.add_script_tag() got an unexpected keyword argument 'foo'",
        )
        expect_error(
            lambda: page.add_style_tag(type="text/css"),
            "Page.add_style_tag() got an unexpected keyword argument 'type'",
        )
        expect_error(
            lambda: frame.add_style_tag(type="text/css"),
            "Frame.add_style_tag() got an unexpected keyword argument 'type'",
        )


@case
def add_script_style_tag_option_validation_matches_playwright(page):
    page.set_content("<iframe></iframe>")
    frame = page.frames[1]

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("add_script_tag/add_style_tag unexpectedly accepted invalid option")

    invalid_cases = [
        (
            lambda: page.add_script_tag(content=123),
            "Page.add_script_tag: content: expected string, got number",
        ),
        (
            lambda: page.add_script_tag(content=True),
            "Page.add_script_tag: content: expected string, got boolean",
        ),
        (
            lambda: page.add_script_tag(url=123),
            "Page.add_script_tag: url: expected string, got number",
        ),
        (
            lambda: page.add_script_tag(content="window.__badType = 1", type=123),
            "Page.add_script_tag: type: expected string, got number",
        ),
        (
            lambda: page.add_script_tag(content="window.__badType = 1", type=True),
            "Page.add_script_tag: type: expected string, got boolean",
        ),
        (
            lambda: page.add_style_tag(content=123),
            "Page.add_style_tag: content: expected string, got number",
        ),
        (
            lambda: page.add_style_tag(content=True),
            "Page.add_style_tag: content: expected string, got boolean",
        ),
        (
            lambda: page.add_style_tag(url=123),
            "Page.add_style_tag: url: expected string, got number",
        ),
        (
            lambda: frame.add_script_tag(content=123),
            "Frame.add_script_tag: content: expected string, got number",
        ),
        (
            lambda: frame.add_script_tag(content="window.__frameBadType = 1", type=123),
            "Frame.add_script_tag: type: expected string, got number",
        ),
        (
            lambda: frame.add_style_tag(content=123),
            "Frame.add_style_tag: content: expected string, got number",
        ),
    ]
    for operation, message in invalid_cases:
        expect_error(operation, message)


@case
def expect_polls_for_page_locator_and_response_changes(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect

    page.set_content(
        """
        <title>Loading</title>
        <main id="status">Loading</main>
        <script>
        setTimeout(() => {
          document.title = 'Ready';
          document.querySelector('#status').textContent = 'Ready';
        }, 25);
        </script>
        """
    )

    expect(page).to_have_title("Ready", timeout=3_000)
    expect(page.locator("#status")).to_have_text("Ready", timeout=3_000)


@case
def evaluate_expression_function_and_arg(page):
    page.set_content("<div id='value'>4</div>")

    assert page.evaluate("document.querySelector('#value').textContent") == "4"
    assert page.evaluate("(value) => value * 3", 7) == 21
    assert page.evaluate("() => ({ ok: true, items: [1, 2, 3] })") == {"ok": True, "items": [1, 2, 3]}


@case
def evaluation_expression_validation_and_error_prefixes(page):
    page.set_content("<body><section id='root'><div id='one'>Hi</div><div>Two</div></section></body>")

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(lambda: page.evaluate(None), "Page.evaluate: expression: expected string, got undefined")
    expect_error(lambda: page.evaluate(123), "Page.evaluate: expression: expected string, got number")
    expect_error(lambda: page.evaluate_handle(None), "Page.evaluate_handle: expression: expected string, got undefined")
    expect_error(
        lambda: page.wait_for_function(None, timeout=100),
        "Frame.wait_for_function() missing 1 required positional argument: 'expression'",
        TypeError,
    )
    expect_error(lambda: page.wait_for_function(123, timeout=100), "Page.wait_for_function: expression: expected string, got number")
    expect_error(lambda: page.wait_for_function("() => { throw 'bad'; }", timeout=100), "Page.wait_for_function: bad")

    frame = page.main_frame
    expect_error(lambda: frame.evaluate(None), "Frame.evaluate: expression: expected string, got undefined")
    expect_error(lambda: frame.evaluate("() => { throw 'bad'; }"), "Frame.evaluate: bad")
    expect_error(lambda: frame.evaluate_handle("() => { throw 'bad'; }"), "Frame.evaluate_handle: bad")
    expect_error(lambda: frame.wait_for_function(None, timeout=100), "Frame.wait_for_function: expression: expected string, got undefined")
    expect_error(lambda: frame.wait_for_function(123, timeout=100), "Frame.wait_for_function: expression: expected string, got number")
    expect_error(lambda: frame.wait_for_function("() => { throw 'bad'; }", timeout=100), "Frame.wait_for_function: bad")

    locator = page.locator("body")
    expect_error(lambda: locator.evaluate(None), "Locator.evaluate: expression: expected string, got undefined")
    expect_error(lambda: locator.evaluate("() => { throw 'bad'; }"), "Locator.evaluate: bad")
    expect_error(lambda: locator.evaluate_handle(None), "Locator.evaluate_handle: expression: expected string, got undefined")
    expect_error(lambda: locator.evaluate_handle("() => { throw 'bad'; }"), "Locator.evaluate_handle: bad")
    expect_error(
        lambda: page.locator("div").evaluate_all(None),
        "Frame.eval_on_selector_all() missing 1 required positional argument: 'expression'",
        TypeError,
    )
    expect_error(lambda: page.locator("div").evaluate_all(123), "Locator.evaluate_all: expression: expected string, got number")

    element = page.query_selector("body")
    assert element is not None
    expect_error(lambda: element.evaluate(None), "ElementHandle.evaluate: expression: expected string, got undefined")
    expect_error(lambda: element.evaluate("() => { throw 'bad'; }"), "ElementHandle.evaluate: bad")
    expect_error(lambda: element.evaluate_handle("() => { throw 'bad'; }"), "ElementHandle.evaluate_handle: bad")

    expect_error(lambda: page.eval_on_selector("#one", None), "Page.eval_on_selector: expression: expected string, got undefined")
    expect_error(lambda: page.eval_on_selector("#one", 123), "Page.eval_on_selector: expression: expected string, got number")
    expect_error(lambda: page.eval_on_selector("#one", "() => { throw 'bad'; }"), "Page.eval_on_selector: bad")
    expect_error(lambda: page.eval_on_selector_all("div", None), "Page.eval_on_selector_all: expression: expected string, got undefined")
    expect_error(lambda: page.eval_on_selector_all("div", 123), "Page.eval_on_selector_all: expression: expected string, got number")
    expect_error(lambda: page.eval_on_selector_all("div", "() => { throw 'bad'; }"), "Page.eval_on_selector_all: bad")
    expect_error(lambda: frame.eval_on_selector("#one", None), "Frame.eval_on_selector: expression: expected string, got undefined")
    expect_error(lambda: frame.eval_on_selector("#one", "() => { throw 'bad'; }"), "Frame.eval_on_selector: bad")
    expect_error(lambda: frame.eval_on_selector_all("div", 123), "Frame.eval_on_selector_all: expression: expected string, got number")

    root_handle = page.query_selector("#root")
    assert root_handle is not None
    try:
        expect_error(
            lambda: root_handle.eval_on_selector("#one", None),
            "ElementHandle.eval_on_selector: expression: expected string, got undefined",
        )
        expect_error(lambda: root_handle.eval_on_selector("#one", "() => { throw 'bad'; }"), "ElementHandle.eval_on_selector: bad")
        expect_error(
            lambda: root_handle.eval_on_selector_all("div", 123),
            "ElementHandle.eval_on_selector_all: expression: expected string, got number",
        )
    finally:
        root_handle.dispose()

    object_handle = page.evaluate_handle("() => ({ x: 1 })")
    primitive_handle = page.evaluate_handle("() => 1")
    try:
        expect_error(lambda: object_handle.evaluate(None), "JSHandle.evaluate: expression: expected string, got undefined")
        expect_error(
            lambda: object_handle.evaluate_handle(None),
            "JSHandle.evaluate_handle: expression: expected string, got undefined",
        )
        expect_error(lambda: primitive_handle.evaluate("() => { throw 'bad'; }"), "JSHandle.evaluate: bad")
        mapped = primitive_handle.evaluate_handle("(value, arg) => value + arg", 2)
        try:
            assert mapped.json_value() == 3
        finally:
            mapped.dispose()
    finally:
        object_handle.dispose()
        primitive_handle.dispose()


@case
def evaluate_handle_properties_and_dispose(page):
    handle = page.evaluate_handle("() => ({ name: 'Ada', nested: { count: 2 }, items: [1, 2, 3] })")

    try:
        assert handle.json_value() == {"name": "Ada", "nested": {"count": 2}, "items": [1, 2, 3]}
        assert handle.get_property("nested").json_value() == {"count": 2}
        assert handle.evaluate("(value) => value.items.length") == 3
        properties = handle.get_properties()
        assert properties["name"].json_value() == "Ada"
    finally:
        handle.dispose()
    for operation in (
        handle.json_value,
        lambda: handle.get_property("name"),
        lambda: handle.evaluate("(value) => value"),
        lambda: handle.evaluate_handle("(value) => value"),
    ):
        try:
            operation()
        except Exception:
            pass
        else:
            raise AssertionError("disposed JSHandle operation unexpectedly succeeded")

    primitive = page.evaluate_handle("() => 42")
    assert primitive.json_value() == 42
    primitive.dispose()
    try:
        primitive.json_value()
    except Exception:
        pass
    else:
        raise AssertionError("disposed primitive JSHandle unexpectedly returned a value")

    nan_handle = page.evaluate_handle("() => Number.NaN")
    try:
        assert math.isnan(nan_handle.json_value())
        assert math.isnan(nan_handle.evaluate("(value) => value"))
    finally:
        nan_handle.dispose()

    special_handle = page.evaluate_handle(
        "() => ({ nested: [Number.NaN, -0, 4n], date: new Date('2020-01-02T03:04:05.678Z'), regex: /abc/gi, url: new URL('https://example.com/a?b=1') })"
    )
    try:
        nested = special_handle.get_property("nested").json_value()
        assert math.isnan(nested[0])
        assert nested[1] == 0 and math.copysign(1, nested[1]) == -1
        assert nested[2] == 4
        assert special_handle.get_property("date").json_value() == datetime(
            2020, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc
        )
        assert special_handle.get_property("regex").json_value() == {"r": {"p": "abc", "f": "gi"}}
        assert special_handle.get_property("url").json_value() == urlparse("https://example.com/a?b=1")
    finally:
        special_handle.dispose()


@case
def js_handle_string_previews(page):
    page.set_content("<button>Hi</button>")
    cases = [
        ("() => ({ a: 1 })", "Object"),
        ("() => 42", "42"),
        ("() => undefined", "undefined"),
        ("() => null", "null"),
        ("() => true", "true"),
        ("() => false", "false"),
        ("() => 'abc'", "abc"),
        ("() => Symbol('x')", "Symbol(x)"),
        ("() => Number.NaN", "NaN"),
        ("() => -0", "0"),
        ("() => Infinity", "Infinity"),
        ("() => [1, 2]", "Array(2)"),
        ("() => /a/g", "/a/g"),
        ("() => function f(){}", "function f(){}"),
        ("() => document.querySelector('button')", "JSHandle@node"),
    ]

    for expression, preview in cases:
        handle = page.evaluate_handle(expression)
        try:
            assert str(handle) == preview
            assert repr(handle) == f"<JSHandle preview={preview}>"
        finally:
            handle.dispose()
        assert str(handle) == preview
        assert repr(handle) == f"<JSHandle preview={preview}>"


@case
def js_handle_get_properties_remote_enumeration(page):
    handle = page.evaluate_handle(
        """() => {
            const obj = { a: 1, b: undefined };
            obj.self = obj;
            return obj;
        }"""
    )
    try:
        properties = handle.get_properties()
        assert sorted(properties) == ["a", "b", "self"]
        assert properties["a"].json_value() == 1
        assert properties["b"].json_value() is None
        assert properties["self"].evaluate("(value) => value === value.self") is True
    finally:
        handle.dispose()

    array_handle = page.evaluate_handle("() => { const arr = [10]; arr.extra = 20; return arr; }")
    try:
        properties = array_handle.get_properties()
        assert sorted(properties) == ["0", "extra"]
        assert properties["0"].json_value() == 10
        assert properties["extra"].json_value() == 20
    finally:
        array_handle.dispose()


@case
def js_handle_string_primitive_properties(page):
    handle = page.evaluate_handle("() => 'abc'")
    try:
        assert handle.get_property("0").json_value() == "a"
        assert handle.get_property("1").json_value() == "b"
        assert handle.get_property("length").json_value() == 3
        assert handle.get_property("missing").json_value() is None
        assert handle.get_properties() == {}
    finally:
        handle.dispose()


@case
def js_and_element_handle_get_property_validation(page):
    page.set_content("<input id='field' value='ok'>")
    js_handle = page.evaluate_handle("() => ({ value: 1 })")
    element = page.query_selector("#field")
    assert element is not None
    try:
        validations = [
            (lambda: js_handle.get_property(None), "JSHandle.get_property: name: expected string, got undefined"),
            (lambda: js_handle.get_property(1), "JSHandle.get_property: name: expected string, got number"),
            (lambda: element.get_property(None), "ElementHandle.get_property: name: expected string, got undefined"),
            (lambda: element.get_property(1), "ElementHandle.get_property: name: expected string, got number"),
        ]
        for operation, message in validations:
            try:
                operation()
            except Exception as exc:
                assert str(exc).splitlines()[0] == message
            else:
                raise AssertionError(f"expected {message!r}")
    finally:
        js_handle.dispose()


@case
def js_handle_json_value_cyclic_references(page):
    handle = page.evaluate_handle(
        """() => {
            const obj = { a: 1, child: { b: 2 } };
            obj.self = obj;
            obj.child.parent = obj;
            return obj;
        }"""
    )
    try:
        value = handle.json_value()
        assert sorted(value) == ["a", "child", "self"]
        assert value["self"] is value
        assert value["child"]["parent"] is value
        assert value["child"]["b"] == 2
    finally:
        handle.dispose()


@case
def js_handle_json_value_cyclic_array_references(page):
    handle = page.evaluate_handle(
        """() => {
            const root = { name: "root" };
            const items = [1, root];
            root.items = items;
            items.push(items, { parent: root });
            return items;
        }"""
    )
    try:
        value = handle.json_value()
        assert value[0] == 1
        assert value[1]["name"] == "root"
        assert value[1]["items"] is value
        assert value[2] is value
        assert value[3]["parent"] is value[1]
    finally:
        handle.dispose()


@case
def js_handle_json_value_function_serialization(page):
    handle = page.evaluate_handle(
        """() => ({
            top() {},
            nested: [function inner() {}, 7],
        })"""
    )
    try:
        assert handle.json_value() == {"top": None, "nested": [None, 7]}
    finally:
        handle.dispose()

    function_handle = page.evaluate_handle("() => function named() {}")
    try:
        assert function_handle.json_value() is None
    finally:
        function_handle.dispose()


@case
def json_value_preserves_undefined_object_properties(page):
    assert page.evaluate("() => ({ a: undefined, b: 1 })") == {"a": None, "b": 1}

    handle = page.evaluate_handle(
        """() => ({
            nested: { a: undefined },
            arr: [undefined, 2],
        })"""
    )
    try:
        assert handle.json_value() == {"nested": {"a": None}, "arr": [None, 2]}
    finally:
        handle.dispose()


@case
def json_value_serializes_typed_arrays_and_dom_geometry(page):
    assert page.evaluate("() => new Uint8Array([1, 2, 255])") == [1, 2, 255]
    floats = page.evaluate("() => new Float32Array([1.5, -0])")
    assert floats[0] == 1.5
    assert floats[1] == 0 and math.copysign(1, floats[1]) == -1
    assert page.evaluate("() => new BigInt64Array([1n, -2n])") == [1, -2]

    rect = page.evaluate("() => new DOMRect(1, 2, 3, 4)")
    assert rect == {
        "x": 1,
        "y": 2,
        "width": 3,
        "height": 4,
        "top": 2,
        "right": 4,
        "bottom": 6,
        "left": 1,
    }
    assert page.evaluate("() => new DOMPoint(1, 2, 3, 4)") == {"x": 1, "y": 2, "z": 3, "w": 4}
    matrix = page.evaluate("() => new DOMMatrix([1, 2, 3, 4, 5, 6])")
    assert matrix["a"] == 1
    assert matrix["f"] == 6
    assert matrix["m33"] == 1
    assert matrix["is2D"] is True
    assert matrix["isIdentity"] is False

    handle = page.evaluate_handle(
        """() => ({
            bytes: new Uint8ClampedArray([1, 260]),
            quad: new DOMQuad(
                new DOMPoint(1, 2),
                new DOMPoint(3, 4),
                new DOMPoint(5, 6),
                new DOMPoint(7, 8)
            ),
        })"""
    )
    try:
        value = handle.json_value()
        assert value["bytes"] == [1, 255]
        assert value["quad"]["p1"] == {"x": 1, "y": 2, "z": 0, "w": 1}
        assert value["quad"]["p4"] == {"x": 7, "y": 8, "z": 0, "w": 1}
    finally:
        handle.dispose()


@case
def js_handle_json_value_skips_throwing_getters_and_error_prefixes(page):
    handle = page.evaluate_handle(
        """() => {
            const obj = { a: 1 };
            Object.defineProperty(obj, "ok", { enumerable: true, get() { return 2; } });
            Object.defineProperty(obj, "boom", { enumerable: true, get() { throw "bad json"; } });
            return obj;
        }"""
    )
    try:
        assert handle.json_value() == {"a": 1, "ok": 2}
    finally:
        handle.dispose()

    error_handle = page.evaluate_handle(
        """() => {
            const obj = {};
            Object.defineProperty(obj, "boom", { get() { throw new Error("bad getter"); } });
            return obj;
        }"""
    )
    try:
        try:
            error_handle.get_property("boom")
        except Exception as exc:
            assert str(exc).splitlines()[0] == "JSHandle.get_property: Error: bad getter"
        else:
            raise AssertionError("throwing getter unexpectedly produced a property handle")

        try:
            error_handle.evaluate("() => { throw 'bad'; }")
        except Exception as exc:
            assert str(exc).splitlines()[0] == "JSHandle.evaluate: bad"
        else:
            raise AssertionError("JSHandle.evaluate throw unexpectedly succeeded")

        try:
            error_handle.evaluate_handle("() => { throw 'bad'; }")
        except Exception as exc:
            assert str(exc).splitlines()[0] == "JSHandle.evaluate_handle: bad"
        else:
            raise AssertionError("JSHandle.evaluate_handle throw unexpectedly succeeded")
    finally:
        error_handle.dispose()


@case
def evaluate_primitive_throw_messages_match_playwright(page):
    for thrown, expected in (
        ("'sync'", "sync"),
        ("42", "42"),
        ("null", "null"),
        ("undefined", "undefined"),
        ("{a: 1}", "Object"),
    ):
        try:
            page.evaluate(f"() => {{ throw {thrown}; }}")
        except Exception as exc:
            assert str(exc).splitlines()[0] == f"Page.evaluate: {expected}"
        else:
            raise AssertionError(f"throw {thrown} unexpectedly succeeded")

    try:
        page.evaluate_handle("async () => { throw 'oops'; }")
    except Exception as exc:
        assert str(exc).splitlines()[0] == "Page.evaluate_handle: oops"
    else:
        raise AssertionError("evaluate_handle rejection unexpectedly succeeded")


@case
def js_handle_as_element_maps_to_element(page):
    page.set_content("<button id='go'>Go</button>")
    handle = page.evaluate_handle("() => document.querySelector('#go')")
    try:
        element = handle.as_element()
        assert element is not None
        assert element.text_content() == "Go"
    finally:
        handle.dispose()


@case
def js_handle_as_element_uses_exact_path_for_duplicate_local_selectors(page):
    page.set_content(
        """
        <div><button>A</button></div>
        <section><button>B</button></section>
        """
    )
    handle = page.evaluate_handle("() => document.querySelector('section button')")
    try:
        element = handle.as_element()
        assert element is not None
        assert element.text_content() == "B"
        assert element.evaluate("(node) => node === document.querySelector('section button')") is True
    finally:
        handle.dispose()


@case
def element_handle_keeps_remote_node_after_dom_changes(page):
    page.set_content("<button>A</button><button>B</button>")
    element = page.query_selector("button")
    assert element is not None

    assert element.json_value() == "ref: <Node>"
    page.evaluate("() => document.querySelector('button').remove()")

    assert element.text_content() == "A"
    assert element.evaluate("(node) => node.isConnected") is False
    text = element.get_property("textContent")
    try:
        assert text.json_value() == "A"
    finally:
        text.dispose()
    element.dispose()


@case
def element_handle_actions_keep_remote_node_identity(page):
    page.set_content(
        """
        <button onclick="window.clicked = 'a'">A</button>
        <button onclick="window.clicked = 'b'">B</button>
        """
    )
    element = page.query_selector("button")
    assert element is not None

    page.evaluate(
        """() => {
        const button = document.createElement('button');
        button.textContent = 'X';
        button.onclick = () => window.clicked = 'x';
        document.body.insertBefore(button, document.body.firstElementChild);
        }"""
    )

    element.click()
    assert page.evaluate("() => window.clicked") == "a"


@case
def detached_element_handle_action_state_and_dispatch_behavior(page):
    page.set_content(
        """
        <button onclick="window.clicked = 'a'">A</button>
        <button onclick="window.clicked = 'b'">B</button>
        """
    )
    element = page.query_selector("button")
    assert element is not None
    element.wait_for_element_state("stable", timeout=500)
    try:
        element.wait_for_element_state("attached", timeout=300)
    except Exception as exc:
        assert "ElementHandle.wait_for_element_state: state: expected one of" in str(exc)
    else:
        raise AssertionError("ElementHandle.wait_for_element_state invalid state unexpectedly succeeded")
    element.evaluate("(node) => node.remove()")

    try:
        element.click(timeout=300)
    except Exception as exc:
        assert str(exc).splitlines()[0] == "ElementHandle.click: Element is not attached to the DOM"
    else:
        raise AssertionError("detached ElementHandle.click unexpectedly succeeded")
    assert page.evaluate("() => window.clicked || null") is None

    assert element.is_visible() is False
    assert element.is_hidden() is True
    try:
        element.is_enabled()
    except Exception as exc:
        assert str(exc).splitlines()[0] == "ElementHandle.is_enabled: Element is not attached to the DOM"
    else:
        raise AssertionError("detached ElementHandle.is_enabled unexpectedly succeeded")

    element.wait_for_element_state("hidden", timeout=300)
    try:
        element.wait_for_element_state("visible", timeout=300)
    except Exception as exc:
        assert str(exc).splitlines()[0] == "ElementHandle.wait_for_element_state: Element is not attached to the DOM"
    else:
        raise AssertionError("detached ElementHandle.wait_for_element_state('visible') unexpectedly succeeded")

    element.dispatch_event("click")
    assert page.evaluate("() => window.clicked") == "a"

    page.set_content("<input value='A'><input value='B'>")
    input_element = page.query_selector("input")
    assert input_element is not None
    input_element.evaluate("(node) => node.remove()")
    assert input_element.input_value() == "A"
    assert input_element.bounding_box() is None
    try:
        input_element.is_editable()
    except Exception as exc:
        assert str(exc).splitlines()[0] == "ElementHandle.is_editable: Element is not attached to the DOM"
    else:
        raise AssertionError("detached ElementHandle.is_editable unexpectedly succeeded")


@case
def element_handle_wait_for_element_state_timeout_messages_match_playwright(page):
    page.set_content(
        """
        <button id="visible">Visible</button>
        <button id="hidden" style="display:none">Hidden</button>
        <button id="disabled" disabled>Disabled</button>
        <input id="readonly" readonly value="Read only">
        <input id="editable" value="Editable">
        """
    )
    handles = {
        "visible": page.query_selector("#visible"),
        "hidden": page.query_selector("#hidden"),
        "disabled": page.query_selector("#disabled"),
        "readonly": page.query_selector("#readonly"),
        "editable": page.query_selector("#editable"),
    }
    assert all(handles.values())

    def expect_timeout(operation):
        try:
            operation()
        except Exception as exc:
            assert type(exc).__name__ == "TimeoutError"
            assert str(exc).splitlines()[0] == "ElementHandle.wait_for_element_state: Timeout 300ms exceeded."
        else:
            raise AssertionError("wait_for_element_state timeout case unexpectedly succeeded")

    try:
        handles["visible"].wait_for_element_state("visible", timeout=300)
        handles["hidden"].wait_for_element_state("hidden", timeout=300)
        handles["disabled"].wait_for_element_state("disabled", timeout=300)
        handles["editable"].wait_for_element_state("editable", timeout=300)

        expect_timeout(lambda: handles["visible"].wait_for_element_state("hidden", timeout=300))
        expect_timeout(lambda: handles["hidden"].wait_for_element_state("visible", timeout=300))
        expect_timeout(lambda: handles["disabled"].wait_for_element_state("enabled", timeout=300))
        expect_timeout(lambda: handles["readonly"].wait_for_element_state("editable", timeout=300))
    finally:
        for handle in handles.values():
            handle.dispose()


@case
def element_handle_relative_selectors_use_remote_node_identity(page):
    page.set_content(
        """
        <article><button>A</button><span>One</span></article>
        <article><button>B</button><span>Two</span></article>
        """
    )
    element = page.query_selector("article")
    assert element is not None

    page.evaluate(
        """() => {
        const article = document.createElement('article');
        article.innerHTML = '<button>X</button><span>Inserted</span>';
        document.body.insertBefore(article, document.body.firstElementChild);
        }"""
    )

    child = element.query_selector("button")
    assert child is not None
    assert child.text_content() == "A"
    assert [item.text_content() for item in element.query_selector_all("button, span")] == ["A", "One"]
    assert element.eval_on_selector("span", "node => node.textContent") == "One"
    assert element.eval_on_selector_all("button, span", "nodes => nodes.map(node => node.textContent)") == [
        "A",
        "One",
    ]

    page.set_content("<article><button>A</button><span>One</span></article><article><button>B</button></article>")
    detached = page.query_selector("article")
    assert detached is not None
    detached.evaluate("node => node.remove()")

    child = detached.query_selector("button")
    assert child is not None
    assert child.text_content() == "A"
    assert [item.text_content() for item in detached.query_selector_all("button, span")] == ["A", "One"]
    assert detached.eval_on_selector("span", "node => node.textContent") == "One"
    assert detached.eval_on_selector_all("button, span", "nodes => nodes.map(node => node.textContent)") == [
        "A",
        "One",
    ]
    try:
        detached.wait_for_selector("button", timeout=300)
    except Exception as exc:
        assert str(exc).splitlines()[0] == (
            "ElementHandle.wait_for_selector: Error: Element is not attached to the DOM"
        )
    else:
        raise AssertionError("detached ElementHandle.wait_for_selector unexpectedly succeeded")


@case
def element_handle_relative_selector_engines_use_remote_node_identity(page):
    page.set_content(
        """
        <article><button>A</button><span>One</span></article>
        <article><button>B</button><span>Two</span></article>
        """
    )
    element = page.query_selector("article")
    assert element is not None

    page.evaluate(
        """() => {
        const article = document.createElement('article');
        article.innerHTML = '<button>X</button><span>Inserted</span>';
        document.body.insertBefore(article, document.body.firstElementChild);
        }"""
    )

    role_child = element.query_selector("role=button")
    assert role_child is not None
    assert role_child.text_content() == "A"
    assert element.query_selector("text=Inserted") is None
    assert element.query_selector("text=Two") is None
    assert element.query_selector("text=One").text_content() == "One"
    assert element.query_selector("xpath=.//button").text_content() == "A"
    assert element.query_selector("xpath=//button").text_content() == "A"
    assert element.eval_on_selector("xpath=//button", "node => node.textContent") == "A"

    page.set_content("<article><button>A</button><span>One</span></article><article><button>B</button><span>Two</span></article>")
    detached = page.query_selector("article")
    assert detached is not None
    detached.evaluate("node => node.remove()")

    assert detached.query_selector("role=button") is None
    assert detached.query_selector("text=Two") is None
    assert detached.query_selector("text=One").text_content() == "One"
    assert detached.query_selector("xpath=.//button").text_content() == "A"
    assert detached.query_selector("xpath=//button").text_content() == "A"
    assert [item.text_content() for item in detached.query_selector_all("xpath=.//*")] == ["A", "One"]
    assert detached.eval_on_selector("text=One", "node => node.textContent") == "One"
    assert detached.eval_on_selector("xpath=//button", "node => node.textContent") == "A"


@case
def disposed_element_handle_read_operations_raise(page):
    page.set_content("<button>A</button>")
    element = page.query_selector("button")
    assert element is not None
    element.dispose()

    for method, operation in (
        ("text_content", element.text_content),
        ("evaluate", lambda: element.evaluate("(node) => node.textContent")),
        ("get_property", lambda: element.get_property("textContent")),
        ("json_value", element.json_value),
    ):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == (
                f"ElementHandle.{method}: Target page, context or browser has been closed"
            )
        else:
            raise AssertionError(f"disposed ElementHandle.{method} unexpectedly succeeded")


@case
def query_selector_element_handle_methods(page):
    page.set_content("<article id='card' data-kind='story'><h2>Title</h2><p>Hello</p></article>")

    element = page.query_selector("#card")
    assert element is not None
    assert element.get_attribute("data-kind") == "story"
    assert element.query_selector("h2").inner_text() == "Title"
    assert "Hello" in element.inner_html()
    assert element.bounding_box()["width"] > 0


@case
def query_selector_all_and_xpath(page):
    page.set_content(
        """
        <ul><li>One</li><li>Two</li><li>Three</li></ul>
        <section id="root">
          <span id="hello">Hello world</span>
          <div id="alpha-id">Alpha id</div>
          <div id="space id">Space id</div>
          <div data-testid="save-test">Save test id</div>
          <div data-test-id="legacy-test">Legacy test id</div>
          <div data-test="short-test">Short test id</div>
          <button id="shown">Shown</button>
          <button id="hidden" style="display:none">Hidden</button>
          <button id="transparent" style="visibility:hidden">Transparent</button>
          <article id="alpha"><h2>Alpha</h2><span style="display:none">Secret Token</span><p>Quarterly Revenue</p></article>
          <article id="beta"><h2>Beta</h2><p>Costs</p></article>
          <article id="mixed"><h2>Mixed</h2><p>revenue and costs</p></article>
          <button id="save">Save draft</button>
          <button id="exact">Save</button>
          <button id="case">sAvE</button>
          <span id="hidden-save" style="display:none">Hidden Save</span>
          <div id="literal-chain">A >> B</div>
        </section>
        """
    )

    assert [item.inner_text() for item in page.query_selector_all("li")] == ["One", "Two", "Three"]
    assert [item.inner_text() for item in page.query_selector_all("xpath=//li[position() < 3]")] == ["One", "Two"]
    assert page.locator("button:visible").evaluate_all("(els) => els.map(el => el.id)") == [
        "shown",
        "save",
        "exact",
        "case",
    ]
    assert page.locator("css=button:visible").count() == 4
    assert page.locator("#root").locator("button:visible").evaluate_all("(els) => els.map(el => el.id)") == [
        "shown",
        "save",
        "exact",
        "case",
    ]
    assert page.query_selector("button:visible").get_attribute("id") == "shown"
    assert page.locator('article:has-text("Revenue")').evaluate_all("(els) => els.map(el => el.id)") == [
        "alpha",
        "mixed",
    ]
    assert page.locator('css=article:has-text("quarterly revenue")').get_attribute("id") == "alpha"
    assert page.locator("#root").locator('article:has-text("costs")').evaluate_all("(els) => els.map(el => el.id)") == [
        "beta",
        "mixed",
    ]
    assert page.locator('article:has-text("Secret Token")').get_attribute("id") == "alpha"
    assert page.query_selector('#root:has-text("Costs")').get_attribute("id") == "root"
    label = "(els) => els.map(el => el.id || el.getAttribute('data-testid'))"
    assert page.locator("text=Save").evaluate_all(label) == [
        "save-test",
        "save",
        "exact",
        "case",
        "hidden-save",
    ]
    assert page.locator('text="Save"').evaluate_all("(els) => els.map(el => el.id)") == ["exact"]
    assert page.locator("text=/Save/").evaluate_all(label) == [
        "save-test",
        "save",
        "exact",
        "hidden-save",
    ]
    assert page.locator("text=/save/i").evaluate_all(label) == [
        "save-test",
        "save",
        "exact",
        "case",
        "hidden-save",
    ]
    assert page.locator("#root").locator("text=Hello").get_attribute("id") == "hello"
    assert page.query_selector("text=Save").get_attribute("data-testid") == "save-test"
    assert page.locator("id=alpha-id").inner_text() == "Alpha id"
    assert page.locator("id=space id").inner_text() == "Space id"
    assert page.locator('id="space id"').count() == 0
    assert page.locator("data-testid=save-test").inner_text() == "Save test id"
    assert page.locator("data-test-id=legacy-test").inner_text() == "Legacy test id"
    assert page.locator("data-test=short-test").inner_text() == "Short test id"
    assert page.locator("#root").locator("data-testid=save-test").inner_text() == "Save test id"
    assert page.query_selector("id=alpha-id").inner_text() == "Alpha id"
    assert page.locator("#root >> id=alpha-id").inner_text() == "Alpha id"
    assert page.locator("#root >> data-testid=save-test").inner_text() == "Save test id"
    assert page.locator("#root >> text=Hello").get_attribute("id") == "hello"
    assert page.locator('#root >> text="A >> B"').get_attribute("id") == "literal-chain"
    assert page.locator("#root>>text=/Save/").evaluate_all(label) == [
        "save-test",
        "save",
        "exact",
        "hidden-save",
    ]
    assert page.locator("button >> nth=1").get_attribute("id") == "hidden"
    assert page.locator("button >> nth=-1").get_attribute("id") == "case"
    assert page.locator("#root >> button >> nth=3").get_attribute("id") == "save"
    assert page.locator("button").locator("nth=2").get_attribute("id") == "transparent"
    assert page.query_selector("button >> nth=4").get_attribute("id") == "exact"
    assert page.locator("button >> nth=99").count() == 0
    assert page.locator("button >> nth=foo").count() == 0


@case
def css_text_pseudo_selectors(page):
    page.set_content(
        """
        <section id="root">
          <article id="alpha"><h2 id="alpha-title">Quarterly Revenue</h2><p id="alpha-body">Revenue grew</p></article>
          <button id="save">Save draft</button>
          <button id="exact">Save</button>
          <button id="case">sAvE</button>
          <button id="outer"><span id="inner">Nested Save</span></button>
          <span id="hidden" style="display:none">Hidden Save</span>
          <div id="literal">A >> B</div>
          <input id="text-input" value="Save text">
          <input id="button-input" type="button" value="Save input">
          <input id="submit-input" type="submit" value="Save submit">
          <textarea id="area">Save area</textarea>
        </section>
        """
    )

    assert page.locator(':text("Save")').evaluate_all("(els) => els.map(el => el.id)") == [
        "save",
        "exact",
        "case",
        "inner",
        "hidden",
        "button-input",
        "submit-input",
        "area",
    ]
    assert page.locator('button:text("Save")').evaluate_all("(els) => els.map(el => el.id)") == [
        "save",
        "exact",
        "case",
    ]
    assert page.locator('section:text("Save")').count() == 0
    assert page.locator('article:text("revenue")').count() == 0
    assert page.locator('article:has-text("revenue")').get_attribute("id") == "alpha"
    assert page.locator(':text-is("Save")').evaluate_all("(els) => els.map(el => el.id)") == ["exact"]
    assert page.locator(':text-is("sAvE")').evaluate_all("(els) => els.map(el => el.id)") == ["case"]
    assert page.locator(':text("hidden save")').get_attribute("id") == "hidden"
    assert page.locator(':text-matches("Save")').evaluate_all("(els) => els.map(el => el.id)") == [
        "save",
        "exact",
        "inner",
        "hidden",
        "button-input",
        "submit-input",
        "area",
    ]
    assert page.locator(':text-matches("save", "i")').evaluate_all("(els) => els.map(el => el.id)") == [
        "save",
        "exact",
        "case",
        "inner",
        "hidden",
        "button-input",
        "submit-input",
        "area",
    ]
    assert page.locator(':text("A >> B")').get_attribute("id") == "literal"
    assert page.locator('input:text("Save")').evaluate_all("(els) => els.map(el => el.id)") == [
        "button-input",
        "submit-input",
    ]
    assert page.locator('textarea:text("Save")').get_attribute("id") == "area"


@case
def exact_text_selector_and_regex_raw_text_match_playwright(page):
    page.set_content(
        """
        <section id="root">
          <div id="direct">Alpha beta</div>
          <div id="nested">Alpha <span id="nested-child">beta</span></div>
          <div id="spaced"> Alpha   beta </div>
          <button id="button-nested">Alpha <span id="button-child">beta</span></button>
          <input id="button-input" type="button" value="Alpha beta">
        </section>
        """
    )

    ids = "(els) => els.map(el => el.id)"
    assert page.locator('text="Alpha beta"').evaluate_all(ids) == ["direct", "spaced", "button-input"]
    assert page.locator('text="Alpha"').evaluate_all(ids) == ["nested", "button-nested"]
    assert page.locator('div:text-is("Alpha beta")').evaluate_all(ids) == ["direct", "spaced"]
    assert page.locator('div:has-text("Alpha beta")').evaluate_all(ids) == ["direct", "nested", "spaced"]
    assert page.locator("text=/alpha beta/i").evaluate_all(ids) == [
        "direct",
        "nested",
        "button-nested",
        "button-input",
    ]
    assert page.locator(r"text=/alpha\s+beta/i").evaluate_all(ids) == [
        "direct",
        "nested",
        "spaced",
        "button-nested",
        "button-input",
    ]
    assert page.get_by_text(re.compile("alpha beta", re.I)).evaluate_all(ids) == [
        "direct",
        "nested",
        "button-nested",
        "button-input",
    ]
    assert page.get_by_text(re.compile(r"alpha\s+beta", re.I)).evaluate_all(ids) == [
        "direct",
        "nested",
        "spaced",
        "button-nested",
        "button-input",
    ]


@case
def input_button_submit_text_selectors_match_playwright(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.set_content(
        """
        <section id="container">
          <input id="button-input" type="button" value="Save">
          <input id="submit-input" type="submit" value="Send">
          <input id="reset-input" type="reset" value="Clear">
          <input id="submit-empty" type="submit">
          <input id="reset-empty" type="reset">
          <input id="text-input" value="Typed Text">
          <button id="button">Save</button>
          <textarea id="textarea">Area Text</textarea>
        </section>
        """
    )

    ids = "(els) => els.map(el => el.id)"
    assert page.get_by_text("Save").evaluate_all(ids) == ["button-input", "button"]
    assert page.get_by_text("Send").evaluate_all(ids) == ["submit-input"]
    assert page.get_by_text("Clear").count() == 0
    assert page.get_by_text("Submit").count() == 0
    assert page.get_by_text("Reset").count() == 0
    assert page.get_by_text("Typed Text").count() == 0
    assert page.get_by_text("Area Text").evaluate_all(ids) == ["textarea"]
    assert page.locator("text=Save").evaluate_all(ids) == ["button-input", "button"]
    assert page.locator("text=Send").evaluate_all(ids) == ["submit-input"]
    assert page.locator("text=Clear").count() == 0
    assert page.locator("text=Submit").count() == 0
    assert page.locator("text=Reset").count() == 0
    assert page.locator('input:text("Save")').evaluate_all(ids) == ["button-input"]
    assert page.locator('input:text("Send")').evaluate_all(ids) == ["submit-input"]
    assert page.locator('input:text("Clear")').count() == 0
    assert page.locator('input:text("Submit")').count() == 0
    assert page.locator('input:text("Reset")').count() == 0
    assert page.locator('#container:has-text("Send")').get_attribute("id") == "container"
    assert page.locator('#container:has-text("Clear")').count() == 0
    assert page.locator("#container").filter(has_text="Send").get_attribute("id") == "container"
    assert page.locator("#container").filter(has_not_text="Clear").get_attribute("id") == "container"
    expect(page.locator("#button-input")).to_have_text("Save")
    expect(page.locator("#button-input")).to_contain_text("av")
    expect(page.locator("#submit-input")).to_have_text("Send")
    expect(page.locator("#submit-input")).to_contain_text("en")
    expect(page.locator("#reset-input")).not_to_have_text("Clear")
    expect(page.locator("#reset-input")).not_to_contain_text("le")
    expect(page.locator("input")).to_have_text(["Save", "Send", "", "", "", ""])
    expect(page.locator("input")).to_contain_text(["Save", "Send"])


@case
def non_string_text_matchers_do_not_match_playwright(page):
    page.set_content(
        """
        <section>
          <div id="mixed">123 True None {'x': 1}</div>
          <label>123 True<input id="label-target"></label>
        </section>
        """
    )

    for value in [True, 123, None, {"x": 1}]:
        assert page.get_by_text(value).count() == 0
        assert page.get_by_label(value).count() == 0
    for value in [True, 123, {"x": 1}]:
        assert page.locator("div").filter(has_text=value).count() == 0
        assert page.locator("div").filter(has_not_text=value).evaluate_all("(els) => els.map(el => el.id)") == ["mixed"]


@case
def role_name_public_dict_validation_matches_playwright(page):
    page.set_content(
        """
        <section id="root">
          <button id="save">Save</button>
        </section>
        """
    )

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("operation unexpectedly succeeded")

    expected = "'dict' object has no attribute 'replace'"
    expect_error(lambda: page.get_by_role("button", name={"x": 1}).count(), expected, AttributeError)
    expect_error(
        lambda: page.locator("section").get_by_role("button", name={"x": 1}).count(),
        expected,
        AttributeError,
    )
    expect_error(lambda: page.main_frame.get_by_role("button", name={"x": 1}).count(), expected, AttributeError)
    assert page.locator("role=button[name=/save/i]").get_attribute("id") == "save"


@case
def role_selector_role_name_validation_matches_playwright(page):
    page.set_content("<section><button id='save'>Save</button></section>")

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("operation unexpectedly succeeded")

    invalid_selector = "Locator.count: InvalidSelectorError: Error while parsing selector"
    expect_error(
        lambda: page.get_by_role({"x": 1}).count(),
        f"{invalid_selector} `{{'x': 1}}` - unexpected symbol \"{{\" at position 0",
    )
    expect_error(
        lambda: page.locator("section").get_by_role([]).count(),
        f'{invalid_selector} `[]` - unexpected symbol "]" at position 1 during parsing property path',
    )
    expect_error(
        lambda: page.main_frame.get_by_role(1.2).count(),
        f'{invalid_selector} `1.2` - unexpected symbol "." at position 1',
    )
    expect_error(lambda: page.locator("role=[button]").count(), "Locator.count: Error: Role must not be empty")
    expect_error(
        lambda: page.locator("role=foo/bar").count(),
        f'{invalid_selector} `foo/bar` - unexpected symbol "/" at position 3',
    )
    assert page.get_by_role(None).count() == 0
    assert page.get_by_role(123).count() == 0
    assert page.get_by_role("button").get_attribute("id") == "save"


@case
def role_selector_attribute_validation_matches_playwright(page):
    page.set_content(
        """
        <section>
          <input id="cb" type="checkbox" checked>
          <input id="cb2" type="checkbox">
          <div id="mixed" role="checkbox" aria-checked="mixed">Mixed</div>
          <button id="button" disabled aria-pressed="mixed" aria-expanded="true">Save</button>
          <div id="tab" role="tab" aria-selected="true">Tab</div>
          <h1 id="heading">Head</h1>
        </section>
        """
    )

    ids = "(els) => els.map(el => el.id)"

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("operation unexpectedly succeeded")

    assert page.locator("role=checkbox[checked]").evaluate_all(ids) == ["cb"]
    assert page.locator("role=checkbox[checked=false]").evaluate_all(ids) == ["cb2"]
    assert page.locator("role=checkbox[checked=mixed]").evaluate_all(ids) == ["mixed"]
    assert page.locator("role=button[disabled]").evaluate_all(ids) == ["button"]
    assert page.locator("role=button[pressed=mixed]").evaluate_all(ids) == ["button"]
    assert page.locator("role=button[expanded=true]").evaluate_all(ids) == ["button"]
    assert page.locator("role=tab[selected]").evaluate_all(ids) == ["tab"]
    assert page.locator("role=heading[level=1]").evaluate_all(ids) == ["heading"]

    prefix = "Locator.evaluate_all: "
    expect_error(
        lambda: page.locator("role=button[unknown=true]").evaluate_all(ids),
        prefix
        + 'Error: Unknown attribute "unknown", must be one of "checked", "disabled", "expanded", '
        '"include-hidden", "level", "name", "pressed", "selected".',
    )
    expect_error(
        lambda: page.locator("role=checkbox[checked=maybe]").evaluate_all(ids),
        prefix + 'Error: "checked" must be one of true, false, "mixed"',
    )
    expect_error(
        lambda: page.locator("role=button[disabled=maybe]").evaluate_all(ids),
        prefix + 'Error: "disabled" must be one of true, false',
    )
    expect_error(
        lambda: page.locator("role=button[level=1]").evaluate_all(ids),
        prefix + 'Error: "level" attribute is only supported for roles: "heading", "listitem", "row", "treeitem"',
    )
    expect_error(
        lambda: page.locator("role=heading[level]").evaluate_all(ids),
        prefix + 'Error: "level" attribute must be compared to a number',
    )
    expect_error(
        lambda: page.locator("role=button[name={x}]").evaluate_all(ids),
        prefix
        + 'InvalidSelectorError: Error while parsing selector `button[name={x}]` - unexpected symbol "{" at position 12 during parsing attribute value',
    )
    expect_error(
        lambda: page.locator("role=button[checked=true]x").evaluate_all(ids),
        prefix
        + 'InvalidSelectorError: Error while parsing selector `button[checked=true]x` - unexpected symbol "x" at position 20',
    )
    expect_error(
        lambda: page.locator("role=button[=true]").evaluate_all(ids),
        prefix
        + 'InvalidSelectorError: Error while parsing selector `button[=true]` - unexpected symbol "=" at position 7 during parsing property path',
    )
    expect_error(
        lambda: page.locator("role=button[]").evaluate_all(ids),
        prefix
        + 'InvalidSelectorError: Error while parsing selector `button[]` - unexpected symbol "]" at position 7 during parsing property path',
    )


@case
def role_state_filter_role_support_matches_playwright(page):
    page.set_content(
        """
        <section>
          <button id="button" aria-pressed="true" aria-expanded="true">Button</button>
          <input id="checkbox" type="checkbox" checked>
          <input id="radio" type="radio" checked>
          <div id="switch" role="switch" aria-checked="true">Switch</div>
          <select><option id="option" selected>Option</option></select>
          <div id="tab" role="tab" aria-selected="true">Tab</div>
          <div id="treeitem" role="treeitem" aria-selected="true" aria-expanded="true" aria-checked="true">Tree</div>
          <div id="row" role="row" aria-selected="true">Row</div>
          <a id="link" href="#" aria-expanded="true">Link</a>
          <h1 id="heading">Heading</h1>
        </section>
        """
    )

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("operation unexpectedly succeeded")

    assert page.locator("role=option[selected=true]").get_attribute("id") == "option"
    assert page.locator("role=tab[selected=true]").get_attribute("id") == "tab"
    assert page.locator("role=treeitem[selected=true]").get_attribute("id") == "treeitem"
    assert page.locator("role=row[selected=true]").get_attribute("id") == "row"
    assert page.locator("role=button[pressed=true]").get_attribute("id") == "button"
    assert page.locator("role=button[expanded=true]").get_attribute("id") == "button"
    assert page.locator("role=treeitem[expanded=true]").get_attribute("id") == "treeitem"
    assert page.locator("role=link[expanded=true]").get_attribute("id") == "link"
    assert page.get_by_role("option", selected=True).get_attribute("id") == "option"
    assert page.get_by_role("button", pressed=True).get_attribute("id") == "button"
    assert page.get_by_role("link", expanded=True).get_attribute("id") == "link"

    prefix = "Locator.count: Error: "
    expect_error(
        lambda: page.locator("role=button[selected=true]").count(),
        prefix
        + '"selected" attribute is only supported for roles: "columnheader", "gridcell", "option", "row", "rowheader", "tab", "treeitem"',
    )
    expect_error(
        lambda: page.locator("role=checkbox[pressed=true]").count(),
        prefix + '"pressed" attribute is only supported for roles: "button"',
    )
    expect_error(
        lambda: page.locator("role=radio[expanded=true]").count(),
        prefix
        + '"expanded" attribute is only supported for roles: "application", "button", "checkbox", "columnheader", '
        '"combobox", "gridcell", "link", "listbox", "menuitem", "menuitemcheckbox", "menuitemradio", '
        '"row", "rowheader", "rowheader", "switch", "tab", "treeitem"',
    )
    expect_error(
        lambda: page.get_by_role("button", selected=True).count(),
        prefix
        + '"selected" attribute is only supported for roles: "columnheader", "gridcell", "option", "row", "rowheader", "tab", "treeitem"',
    )
    expect_error(
        lambda: page.get_by_role("checkbox", pressed=True).count(),
        prefix + '"pressed" attribute is only supported for roles: "button"',
    )
    expect_error(
        lambda: page.get_by_role("radio", expanded=True).count(),
        prefix
        + '"expanded" attribute is only supported for roles: "application", "button", "checkbox", "columnheader", '
        '"combobox", "gridcell", "link", "listbox", "menuitem", "menuitemcheckbox", "menuitemradio", '
        '"row", "rowheader", "rowheader", "switch", "tab", "treeitem"',
    )


@case
def role_selector_boolean_value_case_sensitivity_matches_playwright(page):
    page.set_content(
        """
        <section>
          <input id="checked" type="checkbox" checked>
          <input id="unchecked" type="checkbox">
          <div id="mixed" role="checkbox" aria-checked="mixed">Mixed</div>
          <button id="disabled" disabled>Disabled</button>
          <button id="hidden" hidden>Hidden</button>
        </section>
        """
    )

    ids = "(els) => els.map(el => el.id)"

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("operation unexpectedly succeeded")

    assert page.locator("role=checkbox[checked=true]").evaluate_all(ids) == ["checked"]
    assert page.locator("role=checkbox[checked=false]").evaluate_all(ids) == ["unchecked"]
    assert page.locator("role=checkbox[checked=mixed]").evaluate_all(ids) == ["mixed"]
    assert page.locator("role=button[disabled=true]").evaluate_all(ids) == ["disabled"]
    assert page.locator("role=button[include-hidden=true]").evaluate_all(ids) == ["disabled", "hidden"]

    prefix = "Locator.evaluate_all: Error: "
    expect_error(
        lambda: page.locator("role=checkbox[checked=True]").evaluate_all(ids),
        prefix + '"checked" must be one of true, false, "mixed"',
    )
    expect_error(
        lambda: page.locator("role=checkbox[checked=FALSE]").evaluate_all(ids),
        prefix + '"checked" must be one of true, false, "mixed"',
    )
    expect_error(
        lambda: page.locator("role=checkbox[checked=Mixed]").evaluate_all(ids),
        prefix + '"checked" must be one of true, false, "mixed"',
    )
    expect_error(
        lambda: page.locator("role=button[disabled=True]").evaluate_all(ids),
        prefix + '"disabled" must be one of true, false',
    )
    expect_error(
        lambda: page.locator("role=button[disabled=FALSE]").evaluate_all(ids),
        prefix + '"disabled" must be one of true, false',
    )
    expect_error(
        lambda: page.locator("role=button[include-hidden=True]").evaluate_all(ids),
        prefix + '"include-hidden" must be one of true, false',
    )
    expect_error(
        lambda: page.locator("role=button[include-hidden=FALSE]").evaluate_all(ids),
        prefix + '"include-hidden" must be one of true, false',
    )


@case
def role_selector_quoted_attribute_values_match_playwright(page):
    page.set_content(
        """
        <section>
          <button id="bracket">Close ] bracket</button>
          <button id="square">A [ B</button>
          <button id="escape-bracket">A ] B</button>
          <button id="escape-letter">A q B</button>
          <button id="escape-slash">A / B</button>
          <button id="escape-quote">A &quot; B</button>
          <button id="escape-backslash">A \\ B</button>
          <input id="checked" type="checkbox" checked>
          <h1 id="head">Heading</h1>
        </section>
        """
    )

    ids = "(els) => els.map(el => el.id)"

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
            return
        raise AssertionError("operation unexpectedly succeeded")

    assert page.locator('role=button[name="Close ] bracket"]').evaluate_all(ids) == ["bracket"]
    assert page.locator("role=button[name='Close ] bracket']").evaluate_all(ids) == ["bracket"]
    assert page.locator("role=button[name=/Close ] bracket/]").evaluate_all(ids) == ["bracket"]
    assert page.locator('role=button[name="A [ B"]').evaluate_all(ids) == ["square"]
    assert page.locator('role=button[name="A \\] B"]').evaluate_all(ids) == ["escape-bracket"]
    assert page.locator('role=button[name="A \\q B"]').evaluate_all(ids) == ["escape-letter"]
    assert page.locator('role=button[name="A \\/ B"]').evaluate_all(ids) == ["escape-slash"]
    assert page.locator('role=button[name="A \\" B"]').evaluate_all(ids) == ["escape-quote"]
    assert page.locator('role=button[name="A \\\\ B"]').evaluate_all(ids) == ["escape-backslash"]
    assert page.locator('role=heading[level="1"]').evaluate_all(ids) == ["head"]
    assert page.locator("role=heading[level='1']").evaluate_all(ids) == ["head"]

    prefix = "Locator.evaluate_all: Error: "
    expect_error(
        lambda: page.locator('role=checkbox[checked="true"]').evaluate_all(ids),
        prefix + '"checked" must be one of true, false, "mixed"',
    )
    expect_error(
        lambda: page.locator("role=checkbox[checked='true']").evaluate_all(ids),
        prefix + '"checked" must be one of true, false, "mixed"',
    )


@case
def text_selector_chain_scope_element_matches_playwright(page):
    page.set_content(
        """
        <section id="root">
          <button id="close">Close ] bracket</button>
          <div id="outer">Outer <span id="child">Child</span></div>
        </section>
        """
    )

    ids = "(els) => els.map(el => el.id)"
    assert page.locator('button >> text="Close ] bracket"').evaluate_all(ids) == ["close"]
    assert page.locator("button >> text=/Close ] bracket/").evaluate_all(ids) == ["close"]
    assert page.locator('#outer >> text="Outer"').evaluate_all(ids) == ["outer"]
    assert page.locator('#outer >> text="Child"').evaluate_all(ids) == ["child"]
    assert page.locator('#child >> text="Child"').evaluate_all(ids) == ["child"]
    assert page.locator("button").locator('text="Close ] bracket"').evaluate_all(ids) == ["close"]
    assert page.locator("#outer").locator('text="Child"').evaluate_all(ids) == ["child"]
    assert page.locator("#child").locator('text="Child"').evaluate_all(ids) == ["child"]
    assert page.locator("button >> css=button").evaluate_all(ids) == []
    assert page.locator('button >> role=button[name="Close ] bracket"]').evaluate_all(ids) == []


@case
def get_by_text_hidden_and_empty_text_match_playwright(page):
    page.set_content(
        """
        <section id="root">
          <div id="visible">Alpha</div>
          <div id="hidden" hidden>Alpha</div>
          <div id="display" style="display:none">Alpha</div>
          <div id="empty"></div>
          <div id="space"> </div>
          <span id="span"></span>
        </section>
        """
    )

    ids = "(els) => els.map(el => el.id)"
    assert page.get_by_text("Alpha").evaluate_all(ids) == ["visible", "hidden", "display"]
    assert page.get_by_text("Alpha", exact=True).evaluate_all(ids) == ["visible", "hidden", "display"]
    assert page.get_by_text("").evaluate_all(ids) == ["visible", "hidden", "display", "empty", "space", "span"]
    assert page.get_by_text(" ").evaluate_all(ids) == ["visible", "hidden", "display", "empty", "space", "span"]
    assert page.locator("div").filter(has_text="").evaluate_all(ids) == [
        "visible",
        "hidden",
        "display",
        "empty",
        "space",
    ]
    assert page.locator("div").filter(has_not_text="").evaluate_all(ids) == [
        "visible",
        "hidden",
        "display",
        "empty",
        "space",
    ]


@case
def selector_argument_validation_matches_playwright(page):
    page.set_content("<section id='root'><button>Go</button></section>")
    frame = page.main_frame
    element = page.query_selector("#root")
    assert element is not None

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(lambda: page.locator(None).count(), "Locator.count: selector: expected string, got undefined")
    expect_error(lambda: page.locator(123).count(), "Locator.count: selector: expected string, got number")
    expect_error(lambda: page.query_selector(None), "Page.query_selector: selector: expected string, got undefined")
    expect_error(lambda: page.query_selector(123), "Page.query_selector: selector: expected string, got number")
    expect_error(lambda: page.query_selector_all(None), "Page.query_selector_all: selector: expected string, got undefined")
    expect_error(
        lambda: page.wait_for_selector(None, timeout=100),
        "Frame.wait_for_selector() missing 1 required positional argument: 'selector'",
        TypeError,
    )
    expect_error(lambda: page.eval_on_selector(None, "el => el"), "Page.eval_on_selector: selector: expected string, got undefined")
    expect_error(
        lambda: page.dispatch_event(None, "click"),
        "Frame.dispatch_event() missing 1 required positional argument: 'selector'",
        TypeError,
    )

    expect_error(lambda: frame.locator(None).count(), "Locator.count: selector: expected string, got undefined")
    expect_error(lambda: frame.query_selector(123), "Frame.query_selector: selector: expected string, got number")
    expect_error(lambda: frame.wait_for_selector(None, timeout=100), "Frame.wait_for_selector: selector: expected string, got undefined")
    expect_error(lambda: frame.dispatch_event(None, "click"), "Frame.dispatch_event: selector: expected string, got undefined")
    expect_error(
        lambda: frame.eval_on_selector_all(None, "els => els.length"),
        "Frame.eval_on_selector_all: selector: expected string, got undefined",
    )

    try:
        expect_error(
            lambda: element.query_selector(None),
            "ElementHandle.query_selector: selector: expected string, got undefined",
        )
        expect_error(
            lambda: element.query_selector_all(123),
            "ElementHandle.query_selector_all: selector: expected string, got number",
        )
        expect_error(
            lambda: element.wait_for_selector(None, timeout=100),
            "ElementHandle.wait_for_selector: selector: expected string, got undefined",
        )
        expect_error(
            lambda: element.eval_on_selector(None, "el => el"),
            "ElementHandle.eval_on_selector: selector: expected string, got undefined",
        )
    finally:
        element.dispose()


@case
def builtin_locator_argument_validation_matches_playwright(page):
    page.set_content(
        """
        <section id="scope">
          <label>Name <input id="name"></label>
          <button>Go</button>
          <input placeholder="Email">
          <img alt="Logo" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
          <div title="Tip" data-testid="save">Text</div>
        </section>
        <iframe id="child" srcdoc="<button title='Frame title' data-testid='inside'>Inside</button>"></iframe>
        """
    )
    frame = page.main_frame
    locator = page.locator("#scope")
    frame_locator = page.frame_locator("#child")

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    assert page.get_by_text(None).count() == 0
    assert page.get_by_text(123).count() == 0
    assert page.get_by_role(None).count() == 0
    assert page.get_by_role(123).count() == 0
    assert page.get_by_label(None).count() == 0
    assert page.get_by_label(123).count() == 0
    assert frame.get_by_text(None).count() == 0
    assert locator.get_by_label(True).count() == 0
    assert frame_locator.get_by_text(None).count() == 0

    expect_error(lambda: page.get_by_role("button", name=123).count(), "'int' object has no attribute 'replace'", AttributeError)
    expect_error(lambda: page.get_by_role("textbox", name=True).count(), "'bool' object has no attribute 'replace'", AttributeError)
    expect_error(lambda: page.get_by_test_id(None).count(), "'NoneType' object has no attribute 'replace'", AttributeError)
    expect_error(lambda: page.get_by_test_id(123).count(), "'int' object has no attribute 'replace'", AttributeError)
    expect_error(lambda: page.get_by_placeholder(123).count(), "'int' object has no attribute 'replace'", AttributeError)
    expect_error(lambda: page.get_by_placeholder(None, exact=True).count(), "'NoneType' object has no attribute 'replace'", AttributeError)
    expect_error(lambda: page.get_by_alt_text(None).count(), "'NoneType' object has no attribute 'replace'", AttributeError)
    expect_error(lambda: page.get_by_title(123).count(), "'int' object has no attribute 'replace'", AttributeError)
    expect_error(lambda: frame.get_by_placeholder(123).count(), "'int' object has no attribute 'replace'", AttributeError)
    expect_error(lambda: locator.get_by_test_id(123).count(), "'int' object has no attribute 'replace'", AttributeError)
    expect_error(lambda: frame_locator.get_by_title(123).count(), "'int' object has no attribute 'replace'", AttributeError)


@case
def role_locator_level_filter_validation_matches_playwright(page):
    page.set_content(
        """
        <section id="scope">
          <h2>Title</h2>
          <h3>Subhead</h3>
        </section>
        """
    )
    scoped = page.locator("#scope")

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    assert page.get_by_role("heading", level=2).count() == 1
    assert page.get_by_role("heading", level=2.0).count() == 1
    assert page.get_by_role("heading", level=2.5).count() == 0
    assert page.get_by_role("heading", level="2").count() == 1
    assert page.get_by_role("heading", level="2.0").count() == 1
    assert page.get_by_role("heading", level="02").count() == 1
    assert page.get_by_role("heading", level=None).count() == 2
    assert scoped.get_by_role("heading", level="3").count() == 1
    assert page.locator("role=heading[level=2.0]").count() == 1
    assert page.locator("role=heading[level=2.5]").count() == 0

    expect_error(
        lambda: page.get_by_role("heading", level="bad").count(),
        'Locator.count: Error: "level" attribute must be compared to a number',
    )
    expect_error(
        lambda: page.get_by_role("heading", level=False).count(),
        'Locator.count: Error: "level" attribute must be compared to a number',
    )
    expect_error(
        lambda: page.locator("role=heading[level=true]").count(),
        'Locator.count: Error: "level" attribute must be compared to a number',
    )
    expect_error(
        lambda: page.locator("role=heading[level=bad]").count(),
        'Locator.count: Error: "level" attribute must be compared to a number',
    )
    expect_error(
        lambda: page.locator("role=heading[level=[]]").count(),
        'Locator.count: InvalidSelectorError: Error while parsing selector `heading[level=[]]` - unexpected symbol "[" at position 14 during parsing attribute value',
    )
    expect_error(
        lambda: page.locator("role=heading[level={}]").count(),
        'Locator.count: InvalidSelectorError: Error while parsing selector `heading[level={}]` - unexpected symbol "{" at position 14 during parsing attribute value',
    )
    expect_error(
        lambda: page.get_by_role("heading", level=[]).count(),
        'Locator.count: InvalidSelectorError: Error while parsing selector `heading[level=[]]` - unexpected symbol "[" at position 14 during parsing attribute value',
    )
    expect_error(
        lambda: page.get_by_role("heading", level={}).count(),
        'Locator.count: InvalidSelectorError: Error while parsing selector `heading[level={}]` - unexpected symbol "{" at position 14 during parsing attribute value',
    )


@case
def string_argument_validation_matches_playwright(page):
    page.set_content("<button id='go' data-kind='primary'>Go</button><input id='field'>")
    locator = page.locator("#go")
    element = page.query_selector("#go")
    assert element is not None

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(
        lambda: page.get_attribute("#go", None),
        "Frame.get_attribute() missing 1 required positional argument: 'name'",
        TypeError,
    )
    expect_error(lambda: page.get_attribute("#go", 123), "Page.get_attribute: name: expected string, got number")
    expect_error(lambda: page.main_frame.get_attribute("#go", None), "Frame.get_attribute: name: expected string, got undefined")
    expect_error(
        lambda: locator.get_attribute(None),
        "Frame.get_attribute() missing 1 required positional argument: 'name'",
        TypeError,
    )
    expect_error(lambda: locator.get_attribute(123), "Locator.get_attribute: name: expected string, got number")

    expect_error(
        lambda: page.dispatch_event("#go", None),
        "Frame.dispatch_event() missing 1 required positional argument: 'type'",
        TypeError,
    )
    expect_error(lambda: page.dispatch_event("#go", 123), "Page.dispatch_event: type: expected string, got number")
    expect_error(lambda: page.main_frame.dispatch_event("#go", None), "Frame.dispatch_event: type: expected string, got undefined")
    expect_error(
        lambda: locator.dispatch_event(None),
        "Frame.dispatch_event() missing 1 required positional argument: 'type'",
        TypeError,
    )
    expect_error(lambda: locator.dispatch_event(123), "Locator.dispatch_event: type: expected string, got number")

    expect_error(
        lambda: page.locator("#field").fill(None),
        "Frame.fill() missing 1 required positional argument: 'value'",
        TypeError,
    )
    expect_error(lambda: page.locator("#field").fill(123), "Locator.fill: value: expected string, got number")
    expect_error(
        lambda: page.main_frame.fill("#field", None),
        "Frame._fill() missing 1 required positional argument: 'value'",
        TypeError,
    )
    expect_error(lambda: page.main_frame.fill("#field", 123), "Frame.fill: value: expected string, got number")
    expect_error(
        lambda: page.fill("#field", None),
        "Frame.fill() missing 1 required positional argument: 'value'",
        TypeError,
    )
    expect_error(lambda: page.fill("#field", 123), "Page.fill: value: expected string, got number")

    try:
        expect_error(lambda: element.get_attribute(None), "ElementHandle.get_attribute: name: expected string, got undefined")
        expect_error(lambda: element.get_attribute(123), "ElementHandle.get_attribute: name: expected string, got number")
        expect_error(lambda: element.dispatch_event(None), "ElementHandle.dispatch_event: type: expected string, got undefined")
        expect_error(lambda: element.dispatch_event(123), "ElementHandle.dispatch_event: type: expected string, got number")
        input_element = page.query_selector("#field")
        assert input_element is not None
        try:
            expect_error(lambda: input_element.fill(None), "ElementHandle.fill: value: expected string, got undefined")
        finally:
            input_element.dispose()
    finally:
        element.dispose()


@case
def type_and_press_argument_validation_matches_playwright(page):
    page.set_content("<input id='field' value=''>")
    locator = page.locator("#field")
    element = page.query_selector("#field")
    assert element is not None

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(lambda: page.type(None, "x"), "Frame.type() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: page.type(123, "x"), "Page.type: selector: expected string, got number")
    expect_error(lambda: page.type("#field", None), "Frame.type() missing 1 required positional argument: 'text'", TypeError)
    expect_error(lambda: page.type("#field", 123), "Page.type: text: expected string, got number")
    expect_error(lambda: page.main_frame.type(None, "x"), "Frame.type: selector: expected string, got undefined")
    expect_error(lambda: page.main_frame.type("#field", None), "Frame.type: text: expected string, got undefined")
    expect_error(lambda: page.main_frame.type("#field", 123), "Frame.type: text: expected string, got number")
    expect_error(lambda: locator.type(None), "Frame.type() missing 1 required positional argument: 'text'", TypeError)
    expect_error(lambda: locator.type(123), "Locator.type: text: expected string, got number")
    expect_error(lambda: locator.press_sequentially(None), "Frame.type() missing 1 required positional argument: 'text'", TypeError)
    expect_error(lambda: locator.press_sequentially(123), "Locator.press_sequentially: text: expected string, got number")
    expect_error(lambda: locator.press_sequentially("a", delay="bad"), "Locator.press_sequentially: delay: expected float, got string")

    expect_error(lambda: page.press(None, "A"), "Frame.press() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: page.press(123, "A"), "Page.press: selector: expected string, got number")
    expect_error(lambda: page.press("#field", None), "Frame.press() missing 1 required positional argument: 'key'", TypeError)
    expect_error(lambda: page.press("#field", 123), "Page.press: key: expected string, got number")
    expect_error(lambda: page.main_frame.press(None, "A"), "Frame.press: selector: expected string, got undefined")
    expect_error(lambda: page.main_frame.press("#field", None), "Frame.press: key: expected string, got undefined")
    expect_error(lambda: page.main_frame.press("#field", 123), "Frame.press: key: expected string, got number")
    expect_error(lambda: locator.press(None), "Frame.press() missing 1 required positional argument: 'key'", TypeError)
    expect_error(lambda: locator.press(123), "Locator.press: key: expected string, got number")

    try:
        expect_error(lambda: element.type(None), "ElementHandle.type: text: expected string, got undefined")
        expect_error(lambda: element.type(123), "ElementHandle.type: text: expected string, got number")
        expect_error(lambda: element.press(None), "ElementHandle.press: key: expected string, got undefined")
        expect_error(lambda: element.press(123), "ElementHandle.press: key: expected string, got number")
    finally:
        element.dispose()


@case
def action_selector_argument_validation_matches_playwright(page):
    page.set_content(
        """
        <input id="field">
        <button id="go">Go</button>
        <select id="sel"><option value="a">A</option></select>
        <input id="file" type="file">
        <input id="check" type="checkbox">
        """
    )
    frame = page.main_frame

    def expect_error(operation, expected_message, error_type=Exception):
        try:
            operation()
        except error_type as exc:
            assert str(exc).splitlines()[0] == expected_message
        else:
            raise AssertionError(f"expected {expected_message!r}")

    expect_error(lambda: page.click(None), "Frame._click() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: page.click(123), "Page.click: selector: expected string, got number")
    expect_error(lambda: frame.click(None), "Frame._click() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.click(123), "Frame.click: selector: expected string, got number")
    expect_error(lambda: page.dblclick(None), "Frame.dblclick() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.dblclick(None), "Frame.dblclick: selector: expected string, got undefined")
    expect_error(lambda: page.fill(None, "x"), "Frame.fill() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.fill(None, "x"), "Frame._fill() missing 1 required positional argument: 'selector'", TypeError)

    expect_error(lambda: page.hover(None), "Frame.hover() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.hover(None), "Frame.hover: selector: expected string, got undefined")
    expect_error(lambda: page.focus(None), "Frame.focus() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.focus(None), "Frame.focus: selector: expected string, got undefined")
    expect_error(lambda: page.check(None), "Frame.check() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.check(None), "Frame.check: selector: expected string, got undefined")
    expect_error(lambda: page.uncheck(None), "Frame.uncheck() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.uncheck(None), "Frame.uncheck: selector: expected string, got undefined")
    expect_error(lambda: page.set_checked(None, True), "Frame.check() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.set_checked(None, True), "Frame.set_checked: selector: expected string, got undefined")

    expect_error(
        lambda: page.drag_and_drop(None, "#go"),
        "Frame.drag_and_drop() missing 1 required positional argument: 'source'",
        TypeError,
    )
    expect_error(lambda: page.drag_and_drop(123, "#go"), "Page.drag_and_drop: source: expected string, got number")
    expect_error(
        lambda: page.drag_and_drop("#go", None),
        "Frame.drag_and_drop() missing 1 required positional argument: 'target'",
        TypeError,
    )
    expect_error(lambda: page.drag_and_drop("#go", 123), "Page.drag_and_drop: target: expected string, got number")
    expect_error(lambda: frame.drag_and_drop(None, "#go"), "Frame.drag_and_drop: source: expected string, got undefined")
    expect_error(lambda: frame.drag_and_drop("#go", 123), "Frame.drag_and_drop: target: expected string, got number")
    expect_error(lambda: page.locator("#go").drag_to(None), "'NoneType' object has no attribute '_impl_obj'", AttributeError)

    expect_error(lambda: page.select_option(None, "a"), "Frame.select_option() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.select_option(None, "a"), "Frame.select_option: selector: expected string, got undefined")
    expect_error(lambda: page.set_input_files(None, []), "Frame.set_input_files() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.set_input_files(None, []), "Frame.set_input_files: selector: expected string, got undefined")
    expect_error(lambda: page.input_value(None), "Frame.input_value() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.input_value(None), "Frame.input_value: selector: expected string, got undefined")

    expect_error(lambda: page.text_content(None), "Frame.text_content() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.text_content(None), "Frame.text_content: selector: expected string, got undefined")
    expect_error(lambda: page.inner_text(None), "Frame.inner_text() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.inner_text(None), "Frame.inner_text: selector: expected string, got undefined")
    expect_error(lambda: page.inner_html(None), "Frame.inner_html() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.inner_html(None), "Frame.inner_html: selector: expected string, got undefined")

    expect_error(lambda: page.is_visible(None), "Page.is_visible: selector: expected string, got undefined")
    expect_error(lambda: frame.is_visible(123), "Frame.is_visible: selector: expected string, got number")
    expect_error(lambda: page.is_hidden(None), "Page.is_hidden: selector: expected string, got undefined")
    expect_error(lambda: frame.is_hidden(123), "Frame.is_hidden: selector: expected string, got number")
    expect_error(lambda: page.is_enabled(None), "Frame.is_enabled() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.is_enabled(None), "Frame.is_enabled: selector: expected string, got undefined")
    expect_error(lambda: page.is_checked(None), "Frame.is_checked() missing 1 required positional argument: 'selector'", TypeError)
    expect_error(lambda: frame.is_editable(None), "Frame.is_editable: selector: expected string, got undefined")


@case
def shadow_dom_selector_piercing(page):
    page.set_content(
        """
        <div id="open-host"></div>
        <div id="closed-host"></div>
        <script>
        const openRoot = document.querySelector('#open-host').attachShadow({ mode: 'open' });
        openRoot.innerHTML = `
          <article>
            <button id="shadow-action" data-testid="shadow-action" title="Shadow Title" aria-label="Shadow Save">Shadow Save</button>
            <label>Shadow Email <input id="shadow-input" placeholder="Shadow Placeholder"></label>
          </article>
        `;
        const closedRoot = document.querySelector('#closed-host').attachShadow({ mode: 'closed' });
        closedRoot.innerHTML = `<button id="closed-action" data-testid="closed-action">Closed Save</button>`;
        window.__closedRootHasButton = !!closedRoot.querySelector('#closed-action');
        </script>
        """
    )

    assert page.locator("#shadow-action").inner_text() == "Shadow Save"
    assert page.get_by_text("Shadow Save").get_attribute("id") == "shadow-action"
    assert page.get_by_role("button", name="Shadow Save").get_attribute("id") == "shadow-action"
    assert page.get_by_test_id("shadow-action").get_attribute("id") == "shadow-action"
    assert page.get_by_title("Shadow Title").get_attribute("id") == "shadow-action"
    assert page.get_by_placeholder("Shadow Placeholder").get_attribute("id") == "shadow-input"
    assert page.get_by_label("Shadow Email").get_attribute("id") == "shadow-input"

    assert page.evaluate("window.__closedRootHasButton") is True
    assert page.locator("#closed-action").count() == 0
    assert page.get_by_text("Closed Save").count() == 0
    assert page.get_by_test_id("closed-action").count() == 0


@case
def selectors_register_duplicate_engine_errors(page, *, playwright):
    engine_name = f"duplicateengine{int(time.time() * 1000000)}"
    hyphen_engine_name = f"custom-engine-{int(time.time() * 1000000)}"
    source = """
    {
      query(root, selector) {
        return root.querySelector(selector);
      },
      queryAll(root, selector) {
        return Array.from(root.querySelectorAll(selector));
      }
    }
    """
    playwright.selectors.register(hyphen_engine_name, source, content_script=True)
    playwright.selectors.register(engine_name, source)

    try:
        playwright.selectors.register(f"nosource{int(time.time() * 1000000)}")
    except Exception as exc:
        assert "Either source or path should be specified" in str(exc)
    else:
        raise AssertionError("selector engine registration without a source did not fail")

    try:
        playwright.selectors.register(f"bad:name{int(time.time() * 1000000)}", source)
    except Exception as exc:
        assert "Selectors.register: Selector engine name may only contain [a-zA-Z0-9_] characters" in str(exc)
    else:
        raise AssertionError("selector engine registration with an invalid name did not fail")

    try:
        playwright.selectors.register("css", source)
    except Exception as exc:
        assert 'Selectors.register: "css" is a predefined selector engine' in str(exc)
    else:
        raise AssertionError("selector engine registration with a predefined name did not fail")

    try:
        playwright.selectors.register(f"badsource{int(time.time() * 1000000)}", 123)
    except Exception as exc:
        assert "Selectors.register: selectorEngine.source: expected string, got number" in str(exc)
    else:
        raise AssertionError("selector engine registration with a non-string source did not fail")

    try:
        playwright.selectors.register(f"badkeyword{int(time.time() * 1000000)}", source, unknown=True)
    except TypeError as exc:
        assert "unexpected keyword argument" in str(exc)
    else:
        raise AssertionError("selector engine registration with an unknown keyword did not fail")

    try:
        playwright.selectors.register(engine_name, source)
    except Exception as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate selector engine registration did not fail")

    page.set_content("<button>Registered</button>")
    assert page.locator(f"{engine_name}=button").inner_text() == "Registered"
    assert page.locator(f"{hyphen_engine_name}=button").inner_text() == "Registered"


@case
def nth_match_css_pseudo_selector(page):
    page.set_content(
        """
        <button id="outside">Buy</button>
        <section id="root">
          <button id="first">Buy</button>
          <div id="group"><button id="second">Buy</button><button id="third">Buy now</button></div>
          <button id="fourth" style="display:none">Buy hidden</button>
          <span id="text">Buy text</span>
        </section>
        """
    )

    assert page.locator(":nth-match(button, 1)").get_attribute("id") == "outside"
    assert page.locator(":nth-match(button, 2)").get_attribute("id") == "first"
    assert page.locator(":nth-match(button, 2.0)").get_attribute("id") == "first"
    assert page.locator(":nth-match(button, 2e0)").get_attribute("id") == "first"
    assert page.locator(":nth-match(button, 5)").get_attribute("id") == "fourth"
    assert page.locator(":nth-match(button, 2e1)").count() == 0
    assert page.locator(":nth-match(button, 6)").count() == 0
    assert page.locator(':nth-match(:text("Buy"), 3)').get_attribute("id") == "second"
    assert page.locator("#root >> :nth-match(button, 2)").get_attribute("id") == "second"
    assert page.locator(":nth-match(button:visible, 4)").get_attribute("id") == "third"
    for selector in [":nth-match(button, 0)", ":nth-match(button, foo)"]:
        try:
            page.locator(selector).count()
        except Exception:
            pass
        else:
            raise AssertionError(f"{selector} should reject invalid nth-match indexes")


@case
def role_selector_engine(page):
    page.set_content(
        """
        <section id="root">
          <button id="save" aria-label="Save order">Save</button>
          <button id="cancel">Cancel</button>
          <button id="disabled" disabled>Disabled</button>
          <input id="checked" type="checkbox" checked aria-label="Agree">
          <h2 id="heading">Title</h2>
          <div id="explicit" role="button">Explicit</div>
          <div id="pressed" role="button" aria-pressed="true">Pressed</div>
          <button id="hidden-role" style="display:none">Hidden Action</button>
        </section>
        """
    )

    assert page.locator("role=button").evaluate_all("(els) => els.map(el => el.id)") == [
        "save",
        "cancel",
        "disabled",
        "explicit",
        "pressed",
    ]
    assert page.locator('role=button[name="Save order"]').get_attribute("id") == "save"
    assert page.locator('role=button[name="Save"]').count() == 0
    assert page.locator("role=button[name=/save/i]").get_attribute("id") == "save"
    assert page.locator("role=button[disabled=true]").get_attribute("id") == "disabled"
    assert page.locator("role=checkbox[checked=true]").get_attribute("id") == "checked"
    assert page.locator("role=heading[level=2]").get_attribute("id") == "heading"
    assert page.locator("role=button[pressed=true]").get_attribute("id") == "pressed"
    assert page.locator('role=button[name="Hidden Action"]').count() == 0
    assert page.locator('role=button[name="Hidden Action"][include-hidden=true]').get_attribute("id") == "hidden-role"
    assert page.locator('#root >> role=button[name="Cancel"]').get_attribute("id") == "cancel"
    assert page.locator("role=button >> nth=1").get_attribute("id") == "cancel"
    try:
        page.locator("role=button[foo=bar]").count()
    except Exception:
        pass
    else:
        raise AssertionError("role=button[foo=bar] should reject unknown role selector attributes")


@case
def layout_css_pseudo_selectors(page):
    page.set_viewport_size({"width": 500, "height": 500})
    page.set_content(
        """
        <style>
        body { margin: 0; }
        .box { position: absolute; width: 20px; height: 20px; }
        #anchor { left: 100px; top: 100px; background: red; }
        #right-near { left: 140px; top: 100px; background: blue; }
        #right-far { left: 220px; top: 105px; background: blue; }
        #left-near { left: 60px; top: 100px; background: green; }
        #above-near { left: 100px; top: 60px; background: purple; }
        #below-near { left: 105px; top: 140px; background: orange; }
        #diagonal-near { left: 126px; top: 126px; background: black; }
        #overlap { left: 110px; top: 110px; background: gray; }
        </style>
        <section id="stage">
          <div id="anchor" class="box">A</div>
          <div id="right-near" class="box"></div>
          <div id="right-far" class="box"></div>
          <div id="left-near" class="box"></div>
          <div id="above-near" class="box"></div>
          <div id="below-near" class="box"></div>
          <div id="diagonal-near" class="box"></div>
          <div id="overlap" class="box"></div>
        </section>
        """
    )

    ids = "(els) => els.map(el => el.id)"
    assert page.locator("div:right-of(#anchor)").evaluate_all(ids) == ["right-near", "diagonal-near", "right-far"]
    assert page.locator("div:left-of(#anchor)").evaluate_all(ids) == ["left-near"]
    assert page.locator("div:above(#anchor)").evaluate_all(ids) == ["above-near"]
    assert page.locator("div:below(#anchor)").evaluate_all(ids) == ["below-near", "diagonal-near"]
    assert page.locator("div:near(#anchor)").evaluate_all(ids) == [
        "overlap",
        "diagonal-near",
        "right-near",
        "left-near",
        "above-near",
        "below-near",
    ]
    assert page.locator("div:near(#anchor, 19)").count() == 2
    assert page.locator("div:near(#anchor, 20)").evaluate_all(ids) == [
        "overlap",
        "diagonal-near",
        "right-near",
        "left-near",
        "above-near",
        "below-near",
    ]
    assert page.locator("div:right-of(#anchor):below(#anchor)").evaluate_all(ids) == ["diagonal-near"]
    assert page.locator('div:right-of(:text("A"))').evaluate_all(ids) == [
        "right-near",
        "diagonal-near",
        "right-far",
    ]
    assert page.locator("#stage >> div:right-of(#anchor)").evaluate_all(ids) == [
        "right-near",
        "diagonal-near",
        "right-far",
    ]


@case
def eval_on_selector_and_dispatch_event(page):
    page.set_content(
        """
        <button id="go">Go</button>
        <input id="first"><input id="second">
        <script>
        document.querySelector('#go').addEventListener('click', () => {
          document.body.dataset.clicked = 'yes';
        });
        </script>
        """
    )

    assert page.eval_on_selector("#go", "(el) => el.textContent.trim()") == "Go"
    assert page.eval_on_selector_all("input", "(nodes) => nodes.length") == 2
    page.dispatch_event("#go", "click")
    assert page.evaluate("document.body.dataset.clicked") == "yes"


@case
def locator_collection_text_helpers(page):
    page.set_content("<ul><li>One</li><li>Two</li><li>Three</li></ul>")
    items = page.locator("li")

    assert items.count() == 3
    assert items.first.inner_text() == "One"
    assert items.last.inner_text() == "Three"
    assert items.all_inner_texts() == ["One", "Two", "Three"]
    assert items.all_text_contents() == ["One", "Two", "Three"]
    assert [item.inner_text() for item in items.all()] == ["One", "Two", "Three"]


@case
def locator_collection_input_value_helpers(page):
    page.set_content(
        """
        <input class="field" value="alpha">
        <textarea class="field">bravo</textarea>
        <select class="field"><option value="basic">Basic</option><option value="charlie" selected>Charlie</option></select>
        <div class="plain">not a control</div>
        """
    )
    fields = page.locator(".field")

    assert fields.first.input_value() == "alpha"
    assert fields.nth(1).input_value() == "bravo"
    assert fields.last.input_value() == "charlie"

    try:
        fields.input_value(timeout=500)
    except Exception as exc:
        assert "strict mode violation" in str(exc)
    else:
        raise AssertionError("expected strict mode violation")

    try:
        page.locator(".plain").input_value(timeout=500)
    except Exception as exc:
        assert "Node is not an <input>" in str(exc)
    else:
        raise AssertionError("expected non-control input_value error")


@case
def locator_collection_attribute_helpers(page):
    page.set_content(
        """
        <button class="item" data-id="alpha" id="first"></button>
        <button class="item" data-id="bravo" id="second"></button>
        <button class="item" id="third"></button>
        """
    )
    items = page.locator(".item")

    assert items.first.get_attribute("data-id") == "alpha"
    assert items.nth(1).get_attribute("data-id") == "bravo"
    assert items.last.get_attribute("data-id") is None
    assert items.last.get_attribute("id") == "third"

    try:
        items.get_attribute("data-id", timeout=500)
    except Exception as exc:
        assert "strict mode violation" in str(exc)
    else:
        raise AssertionError("expected strict mode violation")


@case
def locator_collection_inner_html_helpers(page):
    page.set_content(
        """
        <section class="card" id="first"><b>Alpha</b></section>
        <section class="card" id="second"><span>Bravo</span></section>
        <section class="card" id="third"></section>
        """
    )
    cards = page.locator(".card")

    assert cards.first.inner_html() == "<b>Alpha</b>"
    assert cards.nth(1).inner_html() == "<span>Bravo</span>"
    assert cards.last.inner_html() == ""

    try:
        cards.inner_html(timeout=500)
    except Exception as exc:
        assert "strict mode violation" in str(exc)
    else:
        raise AssertionError("expected strict mode violation")


@case
def locator_collection_visibility_helpers(page):
    page.set_content(
        """
        <button class="item" id="shown">Shown</button>
        <button class="item" id="display-none" style="display:none">Hidden</button>
        <button class="item" id="visibility-hidden" style="visibility:hidden">Transparent</button>
        <button class="item" id="zero" style="width:0;height:0;padding:0;border:0"></button>
        """
    )
    items = page.locator(".item")

    assert items.first.is_visible()
    assert items.nth(1).is_visible(timeout="bad") is False
    assert items.nth(2).is_hidden()
    assert items.last.is_visible() is False
    assert page.locator("#missing").is_visible() is False

    try:
        items.is_visible()
    except Exception as exc:
        assert "strict mode violation" in str(exc)
    else:
        raise AssertionError("expected strict mode violation")


@case
def locator_collection_enabled_helpers(page):
    page.set_content(
        """
        <button class="state" id="enabled">Enabled</button>
        <button class="state" id="disabled" disabled>Disabled</button>
        <fieldset disabled><button class="state" id="fieldset-disabled">Fieldset Disabled</button></fieldset>
        <div aria-disabled="true">
          <button class="state" id="aria-disabled-child">Aria Disabled Child</button>
          <span class="state" id="aria-plain">Aria Plain</span>
        </div>
        <span class="state" id="aria-role-disabled" role="button" aria-disabled="true">Role Disabled</span>
        """
    )
    states = page.locator(".state")

    assert states.first.is_enabled()
    assert states.nth(1).is_enabled() is False
    assert states.nth(2).is_disabled()
    assert states.nth(3).is_disabled()
    assert states.nth(4).is_enabled()
    assert states.last.is_disabled()

    try:
        states.is_enabled(timeout=500)
    except Exception as exc:
        assert "strict mode violation" in str(exc)
    else:
        raise AssertionError("expected strict mode violation")


@case
def locator_last_is_dynamic_match_playwright(page):
    page.set_content("<ul><li>One</li><li>Two</li></ul>")
    items = page.locator("li")
    first = items.first
    last = items.last

    page.evaluate(
        """() => {
        const item = document.createElement('li');
        item.textContent = 'Three';
        document.querySelector('ul').appendChild(item);
        }"""
    )
    assert first.text_content() == "One"
    assert last.text_content() == "Three"

    page.evaluate("() => document.querySelector('li:last-child').remove()")
    assert last.text_content() == "Two"
    assert items.nth(-1).text_content() == "Two"


@case
def locator_nth_index_coercion_matches_playwright(page):
    page.set_content(
        """
        <button>A</button><button>B</button>
        <iframe srcdoc="<button>Frame A</button>"></iframe>
        <iframe srcdoc="<button>Frame B</button>"></iframe>
        """
    )
    buttons = page.locator("button")

    assert buttons.nth("1").text_content() == "B"
    assert buttons.nth("1.2").text_content() == "B"
    assert buttons.nth("").text_content() == "A"
    assert buttons.nth(1.2).text_content() == "B"
    assert buttons.nth(-1).text_content() == "B"
    assert page.locator("button >> nth=1.2").all_text_contents() == ["B"]
    assert page.locator("button >> nth=").all_text_contents() == ["A"]
    for invalid_index in [True, False, None, {}, [], "abc", "True", "None", math.nan, math.inf, -math.inf]:
        assert buttons.nth(invalid_index).count() == 0
    for selector in ["button >> nth=True", "button >> nth=abc", "button >> nth=NaN"]:
        assert page.locator(selector).count() == 0

    frames = page.frame_locator("iframe")
    assert frames.nth("1").locator("button").all_text_contents() == ["Frame B"]
    assert frames.nth("1.2").locator("button").all_text_contents() == ["Frame B"]
    assert frames.nth("").locator("button").all_text_contents() == ["Frame A"]
    assert frames.nth(1.2).locator("button").all_text_contents() == ["Frame B"]
    assert frames.nth(-1).locator("button").all_text_contents() == ["Frame B"]
    for invalid_index in [True, False, None, {}, [], "abc", "True", "None", math.nan, math.inf, -math.inf]:
        try:
            frames.nth(invalid_index).locator("button").all_text_contents()
        except Exception as error:
            assert "Failed to find frame" in str(error)
        else:
            raise AssertionError(f"FrameLocator.nth({invalid_index!r}) unexpectedly resolved a frame")


@case
def locator_explicit_index_narrows_collections(page):
    page.set_content("<ul><li>One</li><li>Two</li><li>Three</li></ul>")
    items = page.locator("li")
    second = items.nth(1)
    missing = items.nth(9)

    assert second.count() == 1
    assert second.all_text_contents() == ["Two"]
    assert second.evaluate_all("(elements) => elements.map(element => element.textContent)") == ["Two"]
    assert [item.text_content() for item in second.all()] == ["Two"]
    assert second.first.all_text_contents() == ["Two"]
    assert second.last.all_text_contents() == ["Two"]
    assert second.nth(0).all_text_contents() == ["Two"]
    assert second.nth(1).count() == 0
    assert items.nth(-2).all_text_contents() == ["Two"]
    assert missing.count() == 0
    assert missing.all_text_contents() == []
    assert missing.all() == []

    handles = second.element_handles()
    try:
        assert [handle.text_content() for handle in handles] == ["Two"]
    finally:
        for handle in handles:
            handle.dispose()


@case
def locator_highlight_is_non_mutating(page):
    page.set_content(
        """
        <button id="target" style="outline: 1px solid rgb(0, 0, 0)">First</button>
        <button>Second</button>
        """
    )

    before = page.evaluate("document.body.innerHTML")
    page.locator("#target").highlight()
    page.locator("#missing").highlight()
    page.locator("button").highlight()

    assert page.evaluate("document.body.innerHTML") == before
    assert page.evaluate("getComputedStyle(document.querySelector('#target')).outline") == "rgb(0, 0, 0) solid 1px"


@case
def locator_filter_has_and_has_not(page):
    page.set_content(
        """
        <section><h2>Alpha</h2><button>Open</button></section>
        <section><h2>Beta</h2></section>
        """
    )

    sections = page.locator("section")
    assert sections.filter(has=page.get_by_role("button")).locator("h2").inner_text() == "Alpha"
    assert sections.filter(has_not=page.get_by_role("button")).locator("h2").inner_text() == "Beta"


@case
def locator_filter_visible_and_has_not_text(page):
    page.set_content(
        """
        <p id="public">Public report</p>
        <p id="draft" hidden>Draft report</p>
        <p id="archive">Archive note</p>
        """
    )

    assert page.locator("p").filter(visible=True).count() == 2
    assert page.locator("p").filter(visible=False).evaluate_all("(els) => els.map(el => el.id)") == ["draft"]
    assert page.locator("p").filter(visible="false").evaluate_all("(els) => els.map(el => el.id)") == [
        "public",
        "archive",
    ]
    assert page.locator("p").filter(visible=1).evaluate_all("(els) => els.map(el => el.id)") == ["public", "archive"]
    assert page.locator("p").filter(visible=0).evaluate_all("(els) => els.map(el => el.id)") == ["draft"]
    assert page.locator("p").filter(has_not_text="report", visible=True).inner_text() == "Archive note"


@case
def locator_bounding_box_and_scroll_into_view(page):
    page.set_content(
        """
        <style>
          body { margin: 0; height: 2200px; }
          #target { margin-top: 1800px; width: 40px; height: 30px; }
          #hidden-scroll { display: none; }
          #invisible-scroll { visibility: hidden; width: 10px; height: 10px; }
          #zero-scroll { width: 0; height: 0; }
        </style>
        <button id="target">Go</button>
        <div id="hidden-scroll">Hidden</div>
        <div id="invisible-scroll">Invisible</div>
        <div id="zero-scroll"></div>
        """
    )

    locator = page.locator("#target")
    locator.scroll_into_view_if_needed()
    box = locator.bounding_box()
    assert box["width"] == 40
    assert box["height"] == 30
    try:
        page.locator("#hidden-scroll").scroll_into_view_if_needed(timeout=300)
    except Exception:
        pass
    else:
        raise AssertionError("display:none scroll_into_view_if_needed unexpectedly succeeded")
    page.locator("#invisible-scroll").scroll_into_view_if_needed()
    page.locator("#zero-scroll").scroll_into_view_if_needed()


@case
def page_and_locator_hover_events(page):
    page.set_content(
        """
        <button id="page">Page</button>
        <button id="locator">Locator</button>
        <script>
        document.querySelector('#page').addEventListener('mouseover', () => document.body.dataset.pageHover = 'yes');
        document.querySelector('#locator').addEventListener('mouseover', () => document.body.dataset.locatorHover = 'yes');
        </script>
        """
    )

    page.hover("#page")
    page.locator("#locator").hover()
    assert page.evaluate("document.body.dataset.pageHover") == "yes"
    assert page.evaluate("document.body.dataset.locatorHover") == "yes"


@case
def drag_and_drop_dispatches_drag_events(page):
    page.set_content(
        """
        <div id="source" draggable="true">Card</div>
        <div id="target">Drop</div>
        <script>
        document.querySelector('#source').addEventListener('dragstart', event => {
          event.dataTransfer.setData('text/plain', 'Card');
        });
        document.querySelector('#target').addEventListener('dragover', event => event.preventDefault());
        document.querySelector('#target').addEventListener('drop', event => {
          event.preventDefault();
          document.body.dataset.dropped = event.dataTransfer.getData('text/plain');
        });
        </script>
        """
    )

    page.drag_and_drop("#source", "#target")
    assert page.evaluate("document.body.dataset.dropped") == "Card"


@case
def locator_drag_to_dispatches_drag_events(page):
    page.set_content(
        """
        <div class="source" draggable="true">Item</div>
        <div class="target">Drop</div>
        <script>
        document.querySelector('.source').addEventListener('dragstart', event => {
          event.dataTransfer.setData('text/plain', 'Item');
        });
        document.querySelector('.target').addEventListener('dragover', event => event.preventDefault());
        document.querySelector('.target').addEventListener('drop', event => {
          event.preventDefault();
          document.body.dataset.locatorDropped = event.dataTransfer.getData('text/plain');
        });
        </script>
        """
    )

    page.locator(".source").drag_to(page.locator(".target"), trial=True)
    assert page.evaluate("document.body.dataset.locatorDropped || null") is None
    page.locator(".source").drag_to(page.locator(".target"))
    assert page.evaluate("document.body.dataset.locatorDropped") == "Item"


@case
def drag_and_drop_dispatches_native_pointer_mouse_events_like_playwright(page):
    html = """
        <style>
        #source { position:absolute; left:40px; top:40px; width:90px; height:45px; background:#ddd; }
        #target { position:absolute; left:240px; top:42px; width:100px; height:50px; background:#cfc; }
        </style>
        <div id="source" draggable="true">Source</div>
        <div id="target">Target</div>
        <script>
        window.events = [];
        const types = [
          'pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'pointermove', 'mousemove',
          'pointerdown', 'mousedown', 'dragstart', 'drag', 'dragenter', 'dragover', 'drop',
          'dragend', 'pointerup', 'mouseup', 'click'
        ];
        for (const id of ['source', 'target']) {
          const node = document.getElementById(id);
          for (const type of types) {
            node.addEventListener(type, event => {
              if (type === 'dragstart') event.dataTransfer.setData('text/plain', 'payload');
              if (type === 'dragover' || type === 'drop') event.preventDefault();
              const dataTransferTypes = event.dataTransfer ? Array.from(event.dataTransfer.types).join('|') : '-';
              window.events.push(
                `${id}:${type}:${Math.round(event.clientX)}:${Math.round(event.clientY)}:` +
                `${event.buttons || 0}:${event.isTrusted}:${dataTransferTypes}`
              );
            });
          }
        }
        </script>
        """
    expected = [
        "source:pointerover:85:63:0:true:-",
        "source:pointerenter:85:63:0:true:-",
        "source:mouseover:85:62:0:true:-",
        "source:mouseenter:85:62:0:true:-",
        "source:pointermove:85:63:0:true:-",
        "source:mousemove:85:62:0:true:-",
        "source:pointerdown:85:63:1:true:-",
        "source:mousedown:85:62:1:true:-",
        "target:pointerover:290:67:1:true:-",
        "target:pointerenter:290:67:1:true:-",
        "target:mouseover:290:67:1:true:-",
        "target:mouseenter:290:67:1:true:-",
        "target:pointermove:290:67:1:true:-",
        "target:mousemove:290:67:1:true:-",
        "source:dragstart:85:62:1:true:text/plain",
        "source:drag:290:67:0:true:text/plain",
        "target:dragenter:290:67:0:true:text/plain",
        "target:dragover:290:67:0:true:text/plain",
        "target:drop:290:67:0:true:text/plain",
        "source:dragend:290:67:0:true:text/plain",
    ]

    page.set_content(html)
    page.drag_and_drop("#source", "#target")
    assert page.evaluate("window.events") == expected

    browser = page.context.browser
    assert browser is not None
    locator_page = browser.new_page()
    try:
        locator_page.set_content(html)
        locator_page.locator("#source").drag_to(locator_page.locator("#target"))
        assert locator_page.evaluate("window.events") == expected
    finally:
        locator_page.close()


@case
def locator_drag_to_force_skips_target_receives_events(page):
    page.set_content(
        """
        <style>
        #source { position: absolute; left: 20px; top: 20px; width: 80px; height: 40px; }
        #target { position: absolute; left: 140px; top: 20px; width: 100px; height: 50px; }
        #overlay { position: absolute; left: 140px; top: 20px; width: 100px; height: 50px; }
        </style>
        <div id="source" draggable="true">Item</div>
        <div id="target">Target</div>
        <div id="overlay">Overlay</div>
        <script>
        document.querySelector('#source').addEventListener('dragstart', event => {
          event.dataTransfer.setData('text/plain', 'Item');
        });
        for (const id of ['target', 'overlay']) {
          const node = document.querySelector('#' + id);
          node.addEventListener('dragover', event => event.preventDefault());
          node.addEventListener('drop', event => {
            event.preventDefault();
            document.body.dataset.drop = id;
          });
        }
        </script>
        """
    )

    page.locator("#source").drag_to(page.locator("#target"), force=True, timeout=1_000)
    assert page.evaluate("document.body.dataset.drop") == "overlay"


@case
def locator_drag_to_honors_source_and_target_positions(page):
    page.set_content(
        """
        <style>
        #source { position: absolute; left: 20px; top: 20px; width: 80px; height: 40px; }
        #target { position: absolute; left: 150px; top: 20px; width: 100px; height: 60px; }
        </style>
        <div id="source" draggable="true">Item</div>
        <div id="target">Target</div>
        <script>
        document.querySelector('#source').addEventListener('dragstart', event => {
          document.body.dataset.start = `${Math.round(event.clientX)},${Math.round(event.clientY)}`;
          event.dataTransfer.setData('text/plain', 'Item');
        });
        document.querySelector('#target').addEventListener('dragover', event => event.preventDefault());
        document.querySelector('#target').addEventListener('drop', event => {
          event.preventDefault();
          document.body.dataset.drop = `${Math.round(event.clientX)},${Math.round(event.clientY)}`;
        });
        </script>
        """
    )

    page.locator("#source").drag_to(
        page.locator("#target"),
        source_position={"x": 10, "y": 15},
        target_position={"x": 20, "y": 25},
    )
    assert page.evaluate("document.body.dataset.start") == "30,35"
    assert page.evaluate("document.body.dataset.drop") == "170,45"


@case
def locator_drag_to_honors_steps(page):
    page.set_content(
        """
        <style>
        #source { position: absolute; left: 20px; top: 20px; width: 80px; height: 40px; }
        #target { position: absolute; left: 160px; top: 30px; width: 100px; height: 60px; }
        </style>
        <div id="source" draggable="true">Item</div>
        <div id="target">Target</div>
        <script>
        window.dragEvents = [];
        const record = event => window.dragEvents.push({
          type: event.type,
          target: event.target.id,
          x: Math.round(event.clientX),
          y: Math.round(event.clientY),
        });
        document.querySelector('#source').addEventListener('dragstart', event => {
          record(event);
          event.dataTransfer.setData('text/plain', 'Item');
        });
        document.querySelector('#source').addEventListener('drag', record);
        document.querySelector('#source').addEventListener('dragend', record);
        for (const type of ['dragenter', 'dragover', 'drop']) {
          document.querySelector('#target').addEventListener(type, event => {
            event.preventDefault();
            record(event);
          });
        }
        </script>
        """
    )

    page.locator("#source").drag_to(
        page.locator("#target"),
        source_position={"x": 10, "y": 10},
        target_position={"x": 20, "y": 20},
        steps=3,
    )
    assert page.evaluate("window.dragEvents") == [
        {"type": "dragstart", "target": "source", "x": 30, "y": 30},
        {"type": "drag", "target": "source", "x": 80, "y": 36},
        {"type": "drag", "target": "source", "x": 130, "y": 43},
        {"type": "drag", "target": "source", "x": 180, "y": 50},
        {"type": "dragenter", "target": "target", "x": 180, "y": 50},
        {"type": "dragover", "target": "target", "x": 180, "y": 50},
        {"type": "drop", "target": "target", "x": 180, "y": 50},
        {"type": "dragend", "target": "source", "x": 180, "y": 50},
    ]


@case
def page_drag_and_drop_forwards_force_and_position_options(page):
    page.set_content(
        """
        <style>
        #source { position: absolute; left: 20px; top: 20px; width: 80px; height: 40px; }
        #target { position: absolute; left: 150px; top: 20px; width: 100px; height: 60px; }
        #overlay { position: absolute; left: 150px; top: 20px; width: 100px; height: 60px; }
        </style>
        <div id="source" draggable="true">Item</div>
        <div id="target">Target</div>
        <div id="overlay">Overlay</div>
        <script>
        document.querySelector('#source').addEventListener('dragstart', event => {
          document.body.dataset.start = `${Math.round(event.clientX)},${Math.round(event.clientY)}`;
          event.dataTransfer.setData('text/plain', 'Item');
        });
        for (const id of ['target', 'overlay']) {
          const node = document.querySelector('#' + id);
          node.addEventListener('dragover', event => event.preventDefault());
          node.addEventListener('drop', event => {
            event.preventDefault();
            document.body.dataset.drop = id;
            document.body.dataset.point = `${Math.round(event.clientX)},${Math.round(event.clientY)}`;
          });
        }
        </script>
        """
    )

    page.drag_and_drop(
        "#source",
        "#target",
        force=True,
        source_position={"x": 10, "y": 15},
        target_position={"x": 20, "y": 25},
    )
    assert page.evaluate("document.body.dataset.drop") == "overlay"
    assert page.evaluate("document.body.dataset.start") == "30,35"
    assert page.evaluate("document.body.dataset.point") == "170,45"


@case
def locator_drag_to_accepts_non_css_target_locator(page):
    page.set_content(
        """
        <div class="source" draggable="true">Item</div>
        <section aria-label="Drop zone">Drop Here</section>
        <script>
        document.querySelector('.source').addEventListener('dragstart', event => {
          event.dataTransfer.setData('text/plain', 'Item');
        });
        document.querySelector('section').addEventListener('dragover', event => event.preventDefault());
        document.querySelector('section').addEventListener('drop', event => {
          event.preventDefault();
          document.body.dataset.nonCssDrop = event.dataTransfer.getData('text/plain');
        });
        </script>
        """
    )

    page.locator(".source").drag_to(page.get_by_text("Drop Here"))
    assert page.evaluate("document.body.dataset.nonCssDrop") == "Item"


@case
def form_state_helpers_and_set_checked(page):
    page.set_content(
        """
        <input id="agree" type="checkbox">
        <input id="native-checked-aria-false" type="checkbox" checked aria-checked="false">
        <input id="native-unchecked-aria-true" type="checkbox" aria-checked="true">
        <input id="native-checked-aria-mixed" type="checkbox" checked aria-checked="mixed">
        <input id="native-mixed-aria-false" type="checkbox" aria-checked="false">
        <input id="native-radio-checked-aria-false" type="radio" checked aria-checked="false">
        <input id="native-radio-unchecked-aria-true" type="radio" aria-checked="true">
        <input id="disabled" disabled>
        <fieldset disabled>
          <button id="fieldset-button">Fieldset Button</button>
          <input id="fieldset-input">
        </fieldset>
        <div aria-disabled="true">
          <button id="aria-button">Aria Button</button>
          <span id="aria-plain">Aria Plain</span>
        </div>
        <input id="readonly" readonly>
        <select id="plan"><option value="free">Free</option><option value="pro">Pro</option></select>
        <select id="disabled-plan" disabled><option value="free">Free</option></select>
        <div id="plain-state">Plain</div>
        <button id="button-state">Button</button>
        <div id="role-textbox" role="textbox">Role Textbox</div>
        <div id="role-textbox-readonly" role="textbox" aria-readonly="true">Read Only</div>
        <div id="role-gridcell" role="gridcell">Grid Cell</div>
        <div id="role-gridcell-readonly" role="gridcell" aria-readonly="true">Grid Cell Read Only</div>
        <div id="plain-checked" aria-checked="true">Plain Checked</div>
        <div id="role-button-checked" role="button" aria-checked="true">Button Checked</div>
        <div id="role-switch-checked" role="switch" aria-checked="true">Switch Checked</div>
        <div id="role-option-checked" role="option" aria-checked="true">Option Checked</div>
        <div id="role-treeitem-mixed" role="treeitem" aria-checked="mixed">Treeitem Mixed</div>
        <div id="role-menuitemcheckbox-checked" role="menuitemcheckbox" aria-checked="true">Menu Checkbox</div>
        <div id="role-menuitemradio-unchecked" role="menuitemradio" aria-checked="false">Menu Radio</div>
        """
    )
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    expect = sync_api.expect
    page.locator("#native-mixed-aria-false").evaluate("(el) => el.indeterminate = true")

    def expect_error(callback, *substrings):
        try:
            callback()
        except Exception as exc:
            text = str(exc)
            for substring in substrings:
                assert substring in text
        else:
            raise AssertionError(f"expected error containing {substrings!r}")

    assert page.is_enabled("#agree")
    assert page.is_editable("#agree")
    assert page.is_disabled("#disabled")
    assert page.is_disabled("#fieldset-button")
    assert not page.is_editable("#fieldset-input")
    assert page.is_disabled("#aria-button")
    assert not page.is_disabled("#aria-plain")
    assert not page.is_editable("#readonly")
    assert page.is_editable("#plan")
    assert not page.is_editable("#disabled-plan")
    assert page.is_editable("#role-textbox")
    assert not page.is_editable("#role-textbox-readonly")
    assert page.is_editable("#role-gridcell")
    assert not page.is_editable("#role-gridcell-readonly")
    expect_error(lambda: page.is_editable("#plain-state"), "role allowing")
    expect_error(lambda: page.is_editable("#button-state"), "role allowing")
    expect_error(lambda: page.is_checked("#plain-checked"), "Not a checkbox or radio button")
    expect_error(lambda: page.is_checked("#role-button-checked"), "Not a checkbox or radio button")
    assert page.is_checked("#native-checked-aria-false")
    assert not page.is_checked("#native-unchecked-aria-true")
    assert page.is_checked("#native-checked-aria-mixed")
    assert not page.is_checked("#native-mixed-aria-false")
    expect(page.locator("#native-mixed-aria-false")).to_be_checked(indeterminate=True)
    assert page.is_checked("#native-radio-checked-aria-false")
    assert not page.is_checked("#native-radio-unchecked-aria-true")
    assert page.is_checked("#role-switch-checked")
    assert page.is_checked("#role-option-checked")
    assert not page.is_checked("#role-treeitem-mixed")
    assert page.is_checked("#role-menuitemcheckbox-checked")
    assert not page.is_checked("#role-menuitemradio-unchecked")
    page.set_checked("#agree", True)
    assert page.is_checked("#agree")
    page.set_checked("#agree", False)
    assert not page.is_checked("#agree")
    assert page.select_option("#plan", "pro") == ["pro"]
    assert page.input_value("#plan") == "pro"
    assert page.query_selector("#plan").input_value() == "pro"
    assert page.query_selector("#agree").input_value() == "on"
    expect_error(lambda: page.query_selector("#plain-state").input_value(), "Node is not an <input>")


@case
def role_locator_state_filters(page):
    page.set_content(
        """
        <input type="checkbox" aria-label="Subscribe" checked>
        <button disabled>Save</button>
        <fieldset disabled><button>Archive</button></fieldset>
        <div aria-disabled="true"><button>Blocked</button><span id="aria-plain">Plain</span></div>
        <span role="button" aria-disabled="true">Role Disabled</span>
        <h2>Section</h2>
        <div role="tab" aria-selected="true">Details</div>
        """
    )

    assert page.get_by_role("checkbox", name="Subscribe", checked=True).is_visible()
    assert page.get_by_role("button", name="Save", disabled=True).is_visible()
    assert page.get_by_role("button", name="Archive", disabled=True).is_visible()
    assert page.get_by_role("button", name="Blocked", disabled=True).is_visible()
    assert page.get_by_role("button", name="Role Disabled", disabled=True).is_visible()
    assert not page.locator("#aria-plain").is_disabled()
    assert page.get_by_role("heading", name="Section", level=2).is_visible()
    assert page.get_by_role("tab", selected=True).inner_text() == "Details"


@case
def regex_text_label_and_placeholder_locators(page):
    page.set_content(
        """
        <label>Email address <input></label>
        <input placeholder="Search catalog">
        <p>Quarterly Revenue Report</p>
        """
    )

    page.get_by_label(re.compile("email", re.I)).fill("ada@example.test")
    page.get_by_placeholder(re.compile(r"search\s+catalog", re.I)).fill("invoice")

    assert page.get_by_text(re.compile("revenue report", re.I)).is_visible()
    assert page.evaluate("document.querySelector('label input').value") == "ada@example.test"
    assert page.evaluate("document.querySelector('[placeholder]').value") == "invoice"


@case
def frame_api_queries_and_actions(page):
    page.set_content(
        """
        <iframe name="child" srcdoc='<button id="inside" onclick="document.body.dataset.clicked = `yes`">Inside</button><input id="field"><input id="hidden-field" style="display:none" value="hidden">'></iframe>
        """
    )

    frame = page.frame(name="child")
    assert frame is not None
    assert page.frame("child") is frame
    assert frame.parent_frame is not None
    assert frame.parent_frame.page is page
    assert page.frame(url="about:srcdoc") is frame
    assert page.frame(url="*srcdoc") is frame
    assert page.frame(url=re.compile("srcdoc$")) is frame
    assert page.frame(url=lambda value: value.endswith("srcdoc")) is frame
    assert page.frame(url="srcdoc") is None
    assert frame.locator("#inside").inner_text() == "Inside"
    frame.click("#inside")
    frame.fill("#field", "typed in frame")
    assert frame.evaluate("document.body.dataset.clicked") == "yes"
    assert frame.input_value("#field") == "typed in frame"
    frame.fill("#hidden-field", "forced hidden frame", force=True)
    assert frame.input_value("#hidden-field") == "hidden"


@case
def frame_goto_returns_navigation_response(page):
    with slow_body_server() as base_url:
        page.goto(f"{base_url}/frame-main")
        page.set_content('<iframe name="child"></iframe>')
        frame = page.frame(name="child")
        assert frame is not None

        target_url = f"{base_url}/frame-goto-target"
        response = frame.goto(target_url)

    assert response is not None
    assert response.url == target_url
    assert response.status == 200
    assert response.request is not None
    assert response.request.is_navigation_request()
    assert response.request.frame is frame
    assert response.frame is frame
    assert frame.url == target_url
    assert frame.title() == "Slow Body"
    assert frame.evaluate("() => window.__slowBodyParsed") is True


@case
def frame_locator_nested_get_by(page):
    page.set_content(
        """
        <button>Outside</button>
        <iframe id="child" srcdoc='<main><button title="Frame action">Inside</button><div data-testid="marker">Frame Marker</div></main>'></iframe>
        """
    )

    frame_locator = page.frame_locator("#child")
    assert frame_locator.get_by_title("Frame action").inner_text() == "Inside"
    assert frame_locator.get_by_test_id("marker").inner_text() == "Frame Marker"


@case
def locator_parent_xpath_shorthand_matches_skyvern_dom_helpers(page):
    page.set_content(
        """
        <section data-skyvern-id="parent" data-state="ready">
          <label>
            <span data-skyvern-id="label-text">Name</span>
            <input data-skyvern-id="child" value="Ada">
          </label>
        </section>
        """
    )

    child = page.locator("[data-skyvern-id='child']")
    label = child.locator("..")
    section = label.locator("..")

    assert label.evaluate("node => node.tagName.toLowerCase()") == "label"
    assert label.text_content().strip() == "Name"
    assert section.get_attribute("data-skyvern-id") == "parent"
    assert section.get_attribute("data-state") == "ready"


@case
def frame_locator_strict_single_target_semantics(page):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    first_child = "<button>One</button>"
    second_child = "<button>Two</button>"
    page.set_content(
        f"""
        <iframe id="a" srcdoc="{escape(first_child, quote=True)}"></iframe>
        <iframe id="b" srcdoc="{escape(second_child, quote=True)}"></iframe>
        """
    )

    buttons = page.frame_locator("iframe").locator("button")
    assert buttons.count() == 1
    assert buttons.all_text_contents() == ["One"]
    try:
        buttons.inner_text()
    except sync_api.Error as exc:
        message = str(exc)
        assert "strict mode violation" in message
        assert 'locator("iframe")' in message
    else:
        raise AssertionError("bare frame_locator should be strict for single-target reads")

    assert page.frame_locator("iframe").first.locator("button").inner_text() == "One"
    assert page.frame_locator("iframe").nth(1).locator("button").inner_text() == "Two"

    owner = page.frame_locator("iframe").owner
    assert owner.count() == 2
    try:
        owner.get_attribute("id")
    except sync_api.Error as exc:
        assert "strict mode violation" in str(exc)
    else:
        raise AssertionError("bare frame_locator.owner should be strict for single-target reads")
    assert page.frame_locator("iframe").first.owner.get_attribute("id") == "a"
    assert page.frame_locator("iframe").nth(1).owner.get_attribute("id") == "b"


@case
def frame_locator_indexing_and_missing_frame_collection_errors(page):
    first_child = "<button>One</button>"
    second_child = "<button>Two</button>"
    page.set_content(
        f"""
        <iframe id="a" srcdoc="{escape(first_child, quote=True)}"></iframe>
        <iframe id="b" srcdoc="{escape(second_child, quote=True)}"></iframe>
        """
    )

    frames = page.frame_locator("iframe")

    assert frames.first.locator("button").all_text_contents() == ["One"]
    assert frames.last.locator("button").all_text_contents() == ["Two"]
    assert frames.nth(-1).locator("button").all_text_contents() == ["Two"]
    assert frames.nth(-2).locator("button").all_text_contents() == ["One"]

    missing = frames.nth(2).locator("button")
    assert missing.count() == 0
    assert missing.all() == []
    assert missing.element_handles() == []
    for action in (
        missing.all_text_contents,
        missing.all_inner_texts,
        lambda: missing.evaluate_all("(elements) => elements.length"),
    ):
        try:
            action()
        except Exception as exc:
            assert "Failed to find frame" in str(exc)
        else:
            raise AssertionError("missing frame collection evaluation unexpectedly succeeded")


@case
def frame_locators_are_rejected_inside_composite_locators(page):
    page.set_content(
        """
        <section><iframe srcdoc="<article><section class='body'><button class='primary'><span>Save</span></button><button class='secondary'>Cancel</button></section></article>"></iframe></section>
        <button>Outside</button>
        """
    )

    frame = page.frame_locator("iframe")

    assert frame.locator("button").and_(frame.locator(".primary")).all_text_contents() == ["Save"]
    assert frame.locator(".primary").or_(frame.locator(".secondary")).all_text_contents() == ["Save", "Cancel"]
    assert frame.locator("article").locator(".body").filter(has=frame.locator("button.primary").locator("span")).count() == 1
    assert frame.locator("article").locator("button").and_(frame.locator("article").locator(".primary")).all_text_contents() == ["Save"]
    assert frame.locator("article").locator(".primary").or_(frame.locator("article").locator(".secondary")).all_text_contents() == ["Save", "Cancel"]

    frame_button = frame.locator("button")
    cases = [
        page.locator("section").filter(has=frame_button),
        page.locator("section").filter(has_not=frame_button),
        page.locator("section", has=frame_button),
        page.locator("section", has_not=frame_button),
        page.locator("section").and_(frame_button),
        page.locator("section").or_(frame_button),
    ]

    for locator in cases:
        try:
            locator.count()
        except Exception as exc:
            assert "Frame locators are not allowed inside composite locators" in str(exc)
        else:
            raise AssertionError("frame locator inside composite locator unexpectedly succeeded")


@case
def locator_frame_locator_scopes_from_non_css_locator(page):
    child = "<button aria-label='Save frame'>Inside</button>"
    outside_child = "<button>Outside</button>"
    page.set_content(
        f"""
        <section data-testid="shell">
          <iframe id="scoped-frame" data-testid="scoped-frame" srcdoc="{escape(child, quote=True)}"></iframe>
        </section>
        <iframe id="outside-frame" srcdoc="{escape(outside_child, quote=True)}"></iframe>
        """
    )

    frame_locator = page.get_by_test_id("shell").frame_locator("iframe")
    assert frame_locator.get_by_role("button", name="Save frame").inner_text() == "Inside"
    assert frame_locator.owner.get_attribute("id") == "scoped-frame"

    content_frame = page.get_by_test_id("scoped-frame").content_frame
    assert content_frame is not None
    assert content_frame.locator("button").inner_text() == "Inside"


@case
def context_extra_http_headers_apply_to_new_pages(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context(extra_http_headers={"X-Extra": "from-context"})
        try:
            context_page = context.new_page()
            response = context_page.goto(f"{base_url}/echo-headers")
            assert response.json()["x-extra"] == "from-context"
        finally:
            context.close()


@case
def context_and_page_extra_http_headers_merge_and_update(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context(extra_http_headers={"X-Context": "initial", "X-Shared": "context"})
        try:
            context_page = context.new_page()
            context_page.set_extra_http_headers({"X-Page": "page", "X-Shared": "page"})

            first = context_page.goto(f"{base_url}/echo-headers")
            first_headers = first.json()
            assert first_headers["x-context"] == "initial"
            assert first_headers["x-page"] == "page"
            assert first_headers["x-shared"] == "page"

            context.set_extra_http_headers({"X-Context": "updated", "X-After": "context"})
            second = context_page.goto(f"{base_url}/echo-headers")
            second_headers = second.json()
            assert second_headers["x-context"] == "updated"
            assert second_headers["x-after"] == "context"
            assert second_headers["x-page"] == "page"
            assert second_headers["x-shared"] == "page"

            context_page.set_extra_http_headers({})
            third = context_page.goto(f"{base_url}/echo-headers")
            third_headers = third.json()
            assert third_headers["x-context"] == "updated"
            assert third_headers["x-after"] == "context"
            assert third_headers["x-page"] is None
            assert third_headers["x-shared"] is None

            future_page = context.new_page()
            future_response = future_page.goto(f"{base_url}/echo-headers")
            future_headers = future_response.json()
            assert future_headers["x-context"] == "updated"
            assert future_headers["x-after"] == "context"
            assert future_headers["x-page"] is None
            assert future_headers["x-shared"] is None
        finally:
            context.close()


@case
def context_and_page_set_extra_http_headers_validation_and_none_values(page):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("set_extra_http_headers unexpectedly accepted invalid headers")

    context = browser.new_context()
    try:
        context_page = context.new_page()

        invalid_context_cases = [
            (
                lambda: context.set_extra_http_headers({1: "x"}),
                "BrowserContext.set_extra_http_headers: headers[0].name: expected string, got number",
            ),
            (
                lambda: context.set_extra_http_headers({"X": 1}),
                "BrowserContext.set_extra_http_headers: headers[0].value: expected string, got number",
            ),
            (
                lambda: context.set_extra_http_headers({"X": True}),
                "BrowserContext.set_extra_http_headers: headers[0].value: expected string, got boolean",
            ),
        ]
        for operation, message in invalid_context_cases:
            expect_error(operation, message)
        top_level_context_cases = [
            (lambda: context.set_extra_http_headers(None), "'NoneType' object has no attribute 'items'"),
            (lambda: context.set_extra_http_headers("bad"), "'str' object has no attribute 'items'"),
            (lambda: context.set_extra_http_headers([("X", "Y")]), "'list' object has no attribute 'items'"),
            (lambda: context.set_extra_http_headers((("X", "Y"),)), "'tuple' object has no attribute 'items'"),
        ]
        for operation, message in top_level_context_cases:
            expect_error(operation, message)

        invalid_page_cases = [
            (
                lambda: context_page.set_extra_http_headers({1: "x"}),
                "Page.set_extra_http_headers: headers[0].name: expected string, got number",
            ),
            (
                lambda: context_page.set_extra_http_headers({"X": 1}),
                "Page.set_extra_http_headers: headers[0].value: expected string, got number",
            ),
            (
                lambda: context_page.set_extra_http_headers({"X": True}),
                "Page.set_extra_http_headers: headers[0].value: expected string, got boolean",
            ),
        ]
        for operation, message in invalid_page_cases:
            expect_error(operation, message)
        top_level_page_cases = [
            (lambda: context_page.set_extra_http_headers(None), "'NoneType' object has no attribute 'items'"),
            (lambda: context_page.set_extra_http_headers("bad"), "'str' object has no attribute 'items'"),
            (lambda: context_page.set_extra_http_headers([("X", "Y")]), "'list' object has no attribute 'items'"),
            (lambda: context_page.set_extra_http_headers((("X", "Y"),)), "'tuple' object has no attribute 'items'"),
        ]
        for operation, message in top_level_page_cases:
            expect_error(operation, message)

        with header_case_server() as base_url:
            context.set_extra_http_headers({"X-Context": "kept", "X-After": None})
            context_page.set_extra_http_headers({"X-Page": "kept", "X-Shared": None})
            headers = context_page.goto(f"{base_url}/echo-headers").json()
            assert headers["x-context"] == "kept"
            assert headers["x-page"] == "kept"
            assert headers["x-after"] is None
            assert headers["x-shared"] is None
    finally:
        context.close()


@case
def context_string_header_and_http_credentials_validation_matches_playwright(page):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("context option unexpectedly accepted invalid value")

    invalid_context_cases = [
        (
            lambda: browser.new_context(timezone_id=123),
            "Browser.new_context: timezone_id: expected string, got number",
        ),
        (
            lambda: browser.new_context(locale=123),
            "Browser.new_context: locale: expected string, got number",
        ),
        (
            lambda: browser.new_context(user_agent=123),
            "Browser.new_context: user_agent: expected string, got number",
        ),
        (
            lambda: browser.new_context(base_url=123),
            "Browser.new_context: base_url: expected string, got number",
        ),
        (
            lambda: browser.new_context(extra_http_headers=[]),
            "'list' object has no attribute 'items'",
        ),
        (
            lambda: browser.new_context(extra_http_headers={1: "x"}),
            "Browser.new_context: extraHTTPHeaders[0].name: expected string, got number",
        ),
        (
            lambda: browser.new_context(extra_http_headers={"X": 1}),
            "Browser.new_context: extraHTTPHeaders[0].value: expected string, got number",
        ),
        (
            lambda: browser.new_context(http_credentials="bad"),
            "Browser.new_context: http_credentials: expected object, got string",
        ),
        (
            lambda: browser.new_context(http_credentials={"username": "u"}),
            "Browser.new_context: httpCredentials.password: expected string, got undefined",
        ),
        (
            lambda: browser.new_context(http_credentials={"password": "p"}),
            "Browser.new_context: httpCredentials.username: expected string, got undefined",
        ),
        (
            lambda: browser.new_context(http_credentials={"username": 1, "password": "p"}),
            "Browser.new_context: httpCredentials.username: expected string, got number",
        ),
        (
            lambda: browser.new_context(http_credentials={"username": "u", "password": 1}),
            "Browser.new_context: httpCredentials.password: expected string, got number",
        ),
        (
            lambda: browser.new_context(http_credentials={"username": "u", "password": "p", "origin": 1}),
            "Browser.new_context: httpCredentials.origin: expected string, got number",
        ),
        (
            lambda: browser.new_context(http_credentials={"username": "u", "password": "p", "send": "bad"}),
            "Browser.new_context: httpCredentials.send: expected one of (always|unauthorized)",
        ),
    ]
    for operation, message in invalid_context_cases:
        expect_error(operation, message)

    expect_error(
        lambda: browser.new_page(base_url=123),
        "Browser.new_page: base_url: expected string, got number",
    )
    expect_error(
        lambda: browser.new_page(extra_http_headers={"X": 1}),
        "Browser.new_page: extraHTTPHeaders[0].value: expected string, got number",
    )
    expect_error(
        lambda: browser.new_page(extra_http_headers=[]),
        "Browser.new_page: 'list' object has no attribute 'items'",
    )
    expect_error(
        lambda: browser.new_page(http_credentials={"username": "u"}),
        "Browser.new_page: httpCredentials.password: expected string, got undefined",
    )


@case
def context_base_url_resolves_page_and_frame_navigation(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context(base_url=f"{base_url}/root/")
        try:
            context_page = context.new_page()
            response = context_page.goto("../headers")
            assert response.status == 200
            assert response.url == f"{base_url}/headers"

            context_page.set_content("<iframe name='child'></iframe>")
            frame = context_page.frame(name="child")
            assert frame is not None
            frame_response = frame.goto("/frame-child")
            assert frame_response.status == 200
            assert frame_response.url == f"{base_url}/frame-child"
            assert frame.locator("button").inner_text() == "Fetch in frame"
        finally:
            context.close()


@case
def context_base_url_resolves_routes_waiters_and_api_requests(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context(base_url=f"{base_url}/")
        routed_urls = []

        def handle_route(route):
            routed_urls.append(route.request.url)
            route.fulfill(json={"routed": True, "url": route.request.url})

        context.route("query?from=route", handle_route)
        try:
            context_page = context.new_page()
            response = context_page.goto("query?from=route")
            assert response.status == 200
            assert response.json() == {"routed": True, "url": f"{base_url}/query?from=route"}
            assert routed_urls == [f"{base_url}/query?from=route"]

            context_page.set_content("<button id='fetch' onclick=\"fetch('/query?waiter=1')\">Fetch</button>")
            with context_page.expect_response("/query?waiter=1") as response_info:
                context_page.click("#fetch")
            waiter_response = response_info.value
            assert waiter_response.url == f"{base_url}/query?waiter=1"
            assert waiter_response.json()["query"] == {"waiter": ["1"]}

            api_response = context.request.get("query?api=1")
            assert api_response.url == f"{base_url}/query?api=1"
            assert api_response.json()["query"] == {"api": ["1"]}
        finally:
            context.close()


@case
def context_http_credentials_answer_server_challenge(page):
    browser = page.context.browser
    expected = "Basic " + base64.b64encode(b"user:pass").decode("ascii")

    with header_case_server() as base_url:
        context = browser.new_context(http_credentials={"username": "user", "password": "pass"})
        try:
            context_page = context.new_page()
            response = context_page.goto(f"{base_url}/basic-auth-challenge")
            assert response.status == 200
            body = response.json()
            assert body["authorization"] == expected
            assert body["attempts"] >= 2
        finally:
            context.close()


@case
def context_http_credentials_send_always_does_not_preempt_page_navigation(page):
    browser = page.context.browser
    expected = "Basic " + base64.b64encode(b"user:pass").decode("ascii")

    with header_case_server() as base_url:
        context = browser.new_context(
            http_credentials={"username": "user", "password": "pass", "send": "always"}
        )
        try:
            context_page = context.new_page()
            response = context_page.goto(f"{base_url}/echo-headers")
            assert response.status == 200
            assert response.json()["authorization"] is None

            challenge = context_page.goto(f"{base_url}/basic-auth-challenge")
            assert challenge.status == 200
            body = challenge.json()
            assert body["authorization"] == expected
            assert body["attempts"] >= 2
        finally:
            context.close()


@case
def browser_new_page_context_options_apply_to_implicit_context(page):
    browser = page.context.browser
    assert browser is not None

    with header_case_server() as base_url:
        implicit_page = browser.new_page(
            base_url=f"{base_url}/nested/",
            viewport={"width": 420, "height": 260},
            user_agent="ImplicitPageAgent/1.0",
            locale="en-GB",
            timezone_id="Europe/Rome",
            extra_http_headers={"X-Extra": "from-implicit-context"},
        )
        try:
            response = implicit_page.goto("../echo-headers")
            assert response.url == f"{base_url}/echo-headers"
            assert response.json()["user-agent"] == "ImplicitPageAgent/1.0"
            assert response.json()["x-extra"] == "from-implicit-context"
            assert implicit_page.context.pages == [implicit_page]
            assert implicit_page.viewport_size == {"width": 420, "height": 260}
            assert implicit_page.evaluate("({ width: innerWidth, height: innerHeight })") == {
                "width": 420,
                "height": 260,
            }
            assert implicit_page.evaluate("navigator.language") == "en-GB"
            assert implicit_page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == "Europe/Rome"
        finally:
            implicit_page.context.close()

    assert implicit_page.is_closed()


@case
def context_strict_selectors_option_sets_selector_default(page):
    browser = page.context.browser
    assert browser is not None

    context = browser.new_context(strict_selectors=True)
    try:
        strict_page = context.new_page()
        strict_page.set_content(
            """
            <button onclick="document.body.dataset.clicked='one'">One</button>
            <button onclick="document.body.dataset.clicked='two'">Two</button>
            """
        )
        try:
            strict_page.click("button", timeout=500)
        except Exception as exc:
            assert "strict mode violation" in str(exc)
        else:
            raise AssertionError("strict_selectors=True did not make page.click strict")

        assert strict_page.evaluate("document.body.dataset.clicked || null") is None
        strict_page.click("button", strict=False)
        assert strict_page.evaluate("document.body.dataset.clicked") == "one"
    finally:
        context.close()


@case
def context_accept_downloads_false_reports_download_error(page):
    browser = page.context.browser
    assert browser is not None

    context = browser.new_context(accept_downloads=False)
    try:
        download_page = context.new_page()
        with tempfile.TemporaryDirectory() as directory:
            copy_path = Path(directory) / "copy.txt"
            with header_case_server() as base_url:
                download_page.set_content(f"<a id='download' href='{base_url}/download'>Download</a>")
                with download_page.expect_download() as download_info:
                    download_page.click("#download")

            download = download_info.value
            assert download.suggested_filename == "report.txt"
            assert "Pass 'accept_downloads=True'" in str(download.failure())
            for operation in (download.path, lambda: download.save_as(str(copy_path))):
                try:
                    operation()
                except Exception as exc:
                    assert "Pass 'accept_downloads=True'" in str(exc)
                else:
                    raise AssertionError("download file access unexpectedly succeeded")
    finally:
        context.close()


@case
def context_no_viewport_and_viewport_none_disable_viewport_emulation(page):
    browser = page.context.browser
    assert browser is not None

    cases = (
        ({"no_viewport": True}, None),
        ({"viewport": None}, {"width": 1280, "height": 720}),
    )
    for options, expected_viewport in cases:
        context = browser.new_context(**options)
        try:
            context_page = context.new_page()
            context_page.set_content("<main>No viewport emulation</main>")
            assert context_page.viewport_size == expected_viewport
            size = context_page.evaluate(
                "() => ({ innerWidth, innerHeight, screenWidth: screen.width, screenHeight: screen.height })"
            )
            if expected_viewport is None:
                assert size["innerWidth"] > 0
                assert size["innerHeight"] > 0
                assert size["screenWidth"] > 0
                assert size["screenHeight"] > 0
            else:
                assert size == {
                    "innerWidth": expected_viewport["width"],
                    "innerHeight": expected_viewport["height"],
                    "screenWidth": expected_viewport["width"],
                    "screenHeight": expected_viewport["height"],
                }
        finally:
            context.close()


@case
def context_viewport_and_user_agent_options(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context(viewport={"width": 500, "height": 300}, user_agent="ParityAgent/1.0")
        try:
            context_page = context.new_page()
            response = context_page.goto(f"{base_url}/echo-headers")
            assert context_page.evaluate("({ width: innerWidth, height: innerHeight })") == {"width": 500, "height": 300}
            assert context_page.evaluate("({ width: screen.width, height: screen.height })") == {
                "width": 500,
                "height": 300,
            }
            assert response.json()["user-agent"] == "ParityAgent/1.0"
        finally:
            context.close()


@case
def context_viewport_screen_device_option_validation_matches_playwright(page):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("viewport/device option unexpectedly accepted invalid value")

    invalid_context_cases = [
        (
            lambda: browser.new_context(viewport="bad"),
            "Browser.new_context: viewport: expected object, got string",
        ),
        (
            lambda: browser.new_context(viewport={"height": 100}),
            "Browser.new_context: viewport.width: expected integer, got undefined",
        ),
        (
            lambda: browser.new_context(viewport={"width": 100.5, "height": 100}),
            "Browser.new_context: viewport.width: expected integer, got float 100.5",
        ),
        (
            lambda: browser.new_context(screen="bad"),
            "Browser.new_context: screen: expected object, got string",
        ),
        (
            lambda: browser.new_context(screen={"height": 100}),
            "Browser.new_context: screen.width: expected integer, got undefined",
        ),
        (
            lambda: browser.new_context(device_scale_factor="2"),
            "Browser.new_context: device_scale_factor: expected float, got string",
        ),
        (
            lambda: browser.new_context(no_viewport=True, device_scale_factor=2),
            'Browser.new_context: "deviceScaleFactor" option is not supported with null "viewport"',
        ),
        (
            lambda: browser.new_context(is_mobile="bad"),
            "Browser.new_context: is_mobile: expected boolean, got string",
        ),
        (
            lambda: browser.new_context(no_viewport=True, is_mobile=True),
            'Browser.new_context: "isMobile" option is not supported with null "viewport"',
        ),
        (
            lambda: browser.new_context(has_touch="bad"),
            "Browser.new_context: has_touch: expected boolean, got string",
        ),
    ]
    for operation, message in invalid_context_cases:
        expect_error(operation, message)

    expect_error(
        lambda: browser.new_page(screen="bad"),
        "Browser.new_page: screen: expected object, got string",
    )
    expect_error(
        lambda: browser.new_page(viewport={"width": "100", "height": 100}),
        "Browser.new_page: viewport.width: expected integer, got string",
    )
    expect_error(
        lambda: browser.new_page(no_viewport=True, device_scale_factor=2),
        'Browser.new_page: "deviceScaleFactor" option is not supported with null "viewport"',
    )
    expect_error(
        lambda: browser.new_page(no_viewport=True, is_mobile=True),
        'Browser.new_page: "isMobile" option is not supported with null "viewport"',
    )

    viewport_page = browser.new_page()
    try:
        expect_error(
            lambda: viewport_page.set_viewport_size({"width": 100, "height": -1}),
            "Page.set_viewport_size: Protocol error (Emulation.setDeviceMetricsOverride): "
            "Screen width and height values must be positive, not greater than 10000000",
        )
    finally:
        viewport_page.close()

    negative_context = browser.new_context(viewport={"width": -1, "height": 100})
    try:
        expect_error(
            lambda: negative_context.new_page(),
            "BrowserContext.new_page: Protocol error (Emulation.setDeviceMetricsOverride): "
            "Screen width and height values must be positive, not greater than 10000000",
        )
    finally:
        negative_context.close()

    context = browser.new_context(
        viewport={"width": 360, "height": 240},
        screen={"width": 390, "height": 260},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
    )
    try:
        context_page = context.new_page()
        context_page.set_content("<meta name='viewport' content='width=device-width'><main>Device</main>")
        assert context_page.viewport_size == {"width": 360, "height": 240}
        assert context_page.evaluate("({ width: screen.width, height: screen.height })") == {
            "width": 390,
            "height": 260,
        }
        assert_near(context_page.evaluate("window.devicePixelRatio"), 2, 0.01)
        assert context_page.evaluate("navigator.maxTouchPoints") > 0
    finally:
        context.close()


@case
def context_locale_options_apply_to_page_and_headers(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context(locale="fr-FR")
        try:
            context_page = context.new_page()
            response = context_page.goto(f"{base_url}/echo-headers")
            assert context_page.evaluate("Intl.DateTimeFormat().resolvedOptions().locale") == "fr-FR"
            assert context_page.evaluate("navigator.language") == "fr-FR"
            assert context_page.evaluate("navigator.languages") == ["fr-FR"]
            assert response.json()["accept-language"] == "fr-FR"
        finally:
            context.close()


@case
def context_timezone_and_media_options_apply_to_pages(page):
    browser = page.context.browser
    context = browser.new_context(
        timezone_id="America/Los_Angeles",
        color_scheme="dark",
        reduced_motion="reduce",
        forced_colors="active",
        contrast="more",
    )
    try:
        context_page = context.new_page()
        context_page.set_content("<main>Environment</main>")
        assert context_page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == "America/Los_Angeles"
        assert context_page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches") is True
        assert context_page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches") is True
        assert context_page.evaluate("matchMedia('(forced-colors: active)').matches") is True
        assert context_page.evaluate("matchMedia('(prefers-contrast: more)').matches") is True
    finally:
        context.close()


@case
def context_environment_and_emulate_media_validation_matches_playwright(page):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("environment option unexpectedly accepted invalid value")

    invalid_context_cases = [
        (
            lambda: browser.new_context(color_scheme="bad"),
            "Browser.new_context: color_scheme: expected one of (dark|light|no-preference|no-override)",
        ),
        (
            lambda: browser.new_context(reduced_motion="bad"),
            "Browser.new_context: reduced_motion: expected one of (reduce|no-preference|no-override)",
        ),
        (
            lambda: browser.new_context(forced_colors="bad"),
            "Browser.new_context: forced_colors: expected one of (active|none|no-override)",
        ),
        (
            lambda: browser.new_context(contrast="bad"),
            "Browser.new_context: contrast: expected one of (no-preference|more|no-override)",
        ),
        (
            lambda: browser.new_context(service_workers="bad"),
            "Browser.new_context: service_workers: expected one of (allow|block)",
        ),
        (
            lambda: browser.new_context(java_script_enabled="bad"),
            "Browser.new_context: java_script_enabled: expected boolean, got string",
        ),
        (
            lambda: browser.new_context(offline="bad"),
            "Browser.new_context: offline: expected boolean, got string",
        ),
        (
            lambda: browser.new_context(bypass_csp="bad"),
            "Browser.new_context: bypass_csp: expected boolean, got string",
        ),
    ]
    for operation, message in invalid_context_cases:
        expect_error(operation, message)

    expect_error(
        lambda: browser.new_page(color_scheme="bad"),
        "Browser.new_page: color_scheme: expected one of (dark|light|no-preference|no-override)",
    )
    expect_error(
        lambda: browser.new_page(java_script_enabled="bad"),
        "Browser.new_page: java_script_enabled: expected boolean, got string",
    )

    invalid_emulate_cases = [
        (
            lambda: page.emulate_media(media="bad"),
            "Page.emulate_media: media: expected one of (screen|print|no-override)",
        ),
        (
            lambda: page.emulate_media(color_scheme="none"),
            "Page.emulate_media: color_scheme: expected one of (dark|light|no-preference|no-override)",
        ),
        (
            lambda: page.emulate_media(reduced_motion="bad"),
            "Page.emulate_media: reduced_motion: expected one of (reduce|no-preference|no-override)",
        ),
        (
            lambda: page.emulate_media(forced_colors="bad"),
            "Page.emulate_media: forced_colors: expected one of (active|none|no-override)",
        ),
        (
            lambda: page.emulate_media(contrast="bad"),
            "Page.emulate_media: contrast: expected one of (no-preference|more|no-override)",
        ),
    ]
    for operation, message in invalid_emulate_cases:
        expect_error(operation, message)

    context = browser.new_context(
        color_scheme="no-override",
        reduced_motion="null",
        forced_colors="no-override",
        contrast="null",
    )
    context.close()
    page.emulate_media(
        media="no-override",
        color_scheme="no-override",
        reduced_motion="null",
        forced_colors="no-override",
        contrast="null",
    )


@case
def context_offline_option_and_toggle_apply_to_new_pages(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context(offline=True)
        try:
            first = context.new_page()
            first.set_content("<main>Offline</main>")
            assert first.evaluate(
                "async url => fetch(url).then(() => 'ok').catch(() => 'failed')",
                f"{base_url}/echo-headers",
            ) == "failed"

            context.set_offline(False)
            second = context.new_page()
            response = second.goto(f"{base_url}/echo-headers")
            assert response.ok

            context.set_offline(True)
            third = context.new_page()
            third.set_content("<main>Offline again</main>")
            assert third.evaluate(
                "async url => fetch(url).then(() => 'ok').catch(() => 'failed')",
                f"{base_url}/echo-headers",
            ) == "failed"
        finally:
            context.close()


@case
def context_bypass_csp_allows_inline_script_injection(page):
    browser = page.context.browser

    def expect_csp_error(callback):
        try:
            callback()
        except Exception as exc:
            assert "Content Security Policy" in str(exc)
        else:
            raise AssertionError("inline script unexpectedly bypassed CSP without bypass_csp")

    with header_case_server() as base_url:
        normal_context = browser.new_context()
        try:
            normal_page = normal_context.new_page()
            normal_page.goto(f"{base_url}/csp")
            expect_csp_error(lambda: normal_page.add_script_tag(content="window.__parityCspProbe = 'blocked';"))
            assert normal_page.evaluate("window.__parityCspProbe") is None
        finally:
            normal_context.close()

        bypass_context = browser.new_context(bypass_csp=True)
        try:
            bypass_page = bypass_context.new_page()
            bypass_page.goto(f"{base_url}/csp")
            bypass_page.add_script_tag(content="window.__parityCspProbe = 'allowed';")
            assert bypass_page.evaluate("window.__parityCspProbe") == "allowed"
        finally:
            bypass_context.close()


@case
def context_java_script_enabled_option_controls_page_scripts(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        disabled_context = browser.new_context(java_script_enabled=False)
        try:
            disabled_page = disabled_context.new_page()
            disabled_page.goto(f"{base_url}/scripted")
            disabled_html = disabled_page.content()
            assert 'data-script="ran"' not in disabled_html
            assert '<main id="probe">Initial</main>' in disabled_html
        finally:
            disabled_context.close()

        enabled_context = browser.new_context(java_script_enabled=True)
        try:
            enabled_page = enabled_context.new_page()
            enabled_page.goto(f"{base_url}/scripted")
            enabled_html = enabled_page.content()
            assert 'data-script="ran"' in enabled_html
            assert '<main id="probe">Ran</main>' in enabled_html
        finally:
            enabled_context.close()


@case
def device_descriptor_context_options(page, *, playwright):
    browser = page.context.browser
    descriptor = dict(playwright.devices["Pixel 7"])
    expected_viewport = descriptor["viewport"]

    context = browser.new_context(**descriptor)
    try:
        context_page = context.new_page()
        context_page.set_content("<meta name='viewport' content='width=device-width,initial-scale=1'><main>Device</main>")

        assert context_page.viewport_size == expected_viewport
        assert context_page.evaluate("({ width: innerWidth, height: innerHeight })") == expected_viewport
        assert_near(context_page.evaluate("window.devicePixelRatio"), descriptor["device_scale_factor"], 0.01)
        assert context_page.evaluate("navigator.userAgent") == descriptor["user_agent"]
        assert context_page.evaluate("navigator.maxTouchPoints") > 0
        assert context_page.evaluate("'ontouchstart' in window") is False
    finally:
        context.close()


@case
def desktop_hidpi_descriptor_context_options(page, *, playwright):
    browser = page.context.browser
    descriptor = dict(playwright.devices["Desktop Chrome HiDPI"])
    expected_viewport = descriptor["viewport"]
    expected_screen = descriptor.get("screen") or expected_viewport

    context = browser.new_context(**descriptor)
    try:
        context_page = context.new_page()
        context_page.set_content("<main>Desktop Device</main>")

        assert context_page.viewport_size == expected_viewport
        assert context_page.evaluate("({ width: innerWidth, height: innerHeight })") == expected_viewport
        assert context_page.evaluate("({ width: screen.width, height: screen.height })") == expected_screen
        assert_near(context_page.evaluate("window.devicePixelRatio"), descriptor["device_scale_factor"], 0.01)
        assert context_page.evaluate("navigator.userAgent") == descriptor["user_agent"]
        assert context_page.evaluate("navigator.maxTouchPoints") == 0
        assert context_page.evaluate("'ontouchstart' in window") is False
        assert context_page.evaluate("() => ({ type: screen.orientation.type, angle: screen.orientation.angle })") == {
            "type": "landscape-primary",
            "angle": 0,
        }
    finally:
        context.close()


@case
def context_geolocation_permission(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context(geolocation={"latitude": 37.7749, "longitude": -122.4194})
        try:
            context.grant_permissions(["geolocation"], origin=base_url)
            context_page = context.new_page()
            context_page.goto(base_url)
            position = context_page.evaluate(
                """async () => await new Promise((resolve, reject) => {
                navigator.geolocation.getCurrentPosition(
                  position => resolve({
                    latitude: Number(position.coords.latitude.toFixed(4)),
                    longitude: Number(position.coords.longitude.toFixed(4)),
                  }),
                  error => reject(new Error(error.message))
                );
                })"""
            )
            assert position == {"latitude": 37.7749, "longitude": -122.4194}
        finally:
            context.close()


@case
def context_clear_permissions_revokes_geolocation(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context(
            geolocation={"latitude": 37.7749, "longitude": -122.4194},
            permissions=["geolocation"],
        )
        try:
            context_page = context.new_page()
            context_page.goto(base_url)
            permission_state = context_page.evaluate(
                "async () => (await navigator.permissions.query({ name: 'geolocation' })).state"
            )
            assert permission_state == "granted"

            context.clear_permissions()
            permission_state = context_page.evaluate(
                "async () => (await navigator.permissions.query({ name: 'geolocation' })).state"
            )
            assert permission_state == "prompt"

            context.grant_permissions(["geolocation"], origin=base_url)
            permission_state = context_page.evaluate(
                "async () => (await navigator.permissions.query({ name: 'geolocation' })).state"
            )
            assert permission_state == "granted"
        finally:
            context.close()


@case
def context_clipboard_permissions_enable_read_write(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        try:
            context_page = context.new_page()
            context_page.goto(base_url)
            states = context_page.evaluate(
                """async () => ({
                read: (await navigator.permissions.query({ name: "clipboard-read" })).state,
                write: (await navigator.permissions.query({ name: "clipboard-write" })).state,
                })"""
            )
            assert states == {"read": "granted", "write": "granted"}

            context_page.evaluate("() => navigator.clipboard.writeText('skyvern-clipboard-parity')")
            text = context_page.evaluate("() => navigator.clipboard.readText()")
            assert text == "skyvern-clipboard-parity"
        finally:
            context.close()


@case
def context_permission_option_validation_matches_playwright(page):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("permission option unexpectedly accepted invalid value")

    invalid_context_cases = [
        (
            lambda: browser.new_context(permissions="geolocation"),
            "Browser.new_context: permissions: expected array, got string",
        ),
        (
            lambda: browser.new_context(permissions=123),
            "Browser.new_context: permissions: expected array, got number",
        ),
        (
            lambda: browser.new_context(permissions=[1]),
            "Browser.new_context: permissions[0]: expected string, got number",
        ),
        (
            lambda: browser.new_context(permissions=[True]),
            "Browser.new_context: permissions[0]: expected string, got boolean",
        ),
        (
            lambda: browser.new_context(permissions=["unknown-permission"]),
            "Browser.new_context: Unknown permission: unknown-permission",
        ),
    ]
    for operation, message in invalid_context_cases:
        expect_error(operation, message)

    expect_error(
        lambda: browser.new_page(permissions=[1]),
        "Browser.new_page: permissions[0]: expected string, got number",
    )

    context = browser.new_context()
    try:
        invalid_grant_cases = [
            (
                lambda: context.grant_permissions("geolocation"),
                "BrowserContext.grant_permissions: permissions: expected array, got string",
            ),
            (
                lambda: context.grant_permissions(123),
                "BrowserContext.grant_permissions: permissions: expected array, got number",
            ),
            (
                lambda: context.grant_permissions([1]),
                "BrowserContext.grant_permissions: permissions[0]: expected string, got number",
            ),
            (
                lambda: context.grant_permissions([True]),
                "BrowserContext.grant_permissions: permissions[0]: expected string, got boolean",
            ),
            (
                lambda: context.grant_permissions(["unknown-permission"]),
                "BrowserContext.grant_permissions: Unknown permission: unknown-permission",
            ),
            (
                lambda: context.grant_permissions(["geolocation"], origin=123),
                "BrowserContext.grant_permissions: origin: expected string, got number",
            ),
            (
                lambda: context.grant_permissions(["geolocation"], origin=True),
                "BrowserContext.grant_permissions: origin: expected string, got boolean",
            ),
        ]
        for operation, message in invalid_grant_cases:
            expect_error(operation, message)

    finally:
        context.close()

    accepted_context = browser.new_context()
    try:
        accepted_context.grant_permissions(("geolocation",), origin=None)
        accepted_context.grant_permissions([], origin="https://example.com")
    finally:
        accepted_context.close()


@case
def context_set_geolocation_none_returns_position_unavailable(page):
    browser = page.context.browser

    with header_case_server() as base_url:
        context = browser.new_context(
            geolocation={"latitude": 37.5, "longitude": -122.5, "accuracy": 7},
            permissions=["geolocation"],
        )
        try:
            context_page = context.new_page()
            context_page.goto(base_url)
            latitude = context_page.evaluate(
                """async () => await new Promise((resolve, reject) => {
                navigator.geolocation.getCurrentPosition(
                  position => resolve(position.coords.latitude),
                  error => reject(new Error(error.message))
                );
                })"""
            )
            assert latitude == 37.5

            context.set_geolocation(None)
            unavailable = context_page.evaluate(
                """async () => await new Promise(resolve => {
                navigator.geolocation.getCurrentPosition(
                  position => resolve({ ok: true, latitude: position.coords.latitude }),
                  error => resolve({ ok: false, code: error.code }),
                  { timeout: 1000, maximumAge: 0 }
                );
                })"""
            )
            assert unavailable == {"ok": False, "code": 2}

            new_page = context.new_page()
            new_page.goto(base_url)
            assert new_page.evaluate(
                """async () => await new Promise(resolve => {
                navigator.geolocation.getCurrentPosition(
                  position => resolve({ ok: true }),
                  error => resolve({ ok: false, code: error.code }),
                  { timeout: 1000, maximumAge: 0 }
                );
                })"""
            ) == {"ok": False, "code": 3}
        finally:
            context.close()


@case
def geolocation_option_validation_matches_playwright(page):
    browser = page.context.browser

    def expect_error(operation, expected):
        try:
            operation()
        except Exception as exc:
            assert str(exc).splitlines()[0] == expected
            return
        raise AssertionError("geolocation option unexpectedly accepted invalid value")

    invalid_context_cases = [
        (
            lambda: browser.new_context(geolocation={}),
            "Browser.new_context: geolocation.longitude: expected float, got undefined",
        ),
        (
            lambda: browser.new_context(geolocation={"longitude": 0}),
            "Browser.new_context: geolocation.latitude: expected float, got undefined",
        ),
        (
            lambda: browser.new_context(geolocation={"latitude": 0}),
            "Browser.new_context: geolocation.longitude: expected float, got undefined",
        ),
        (
            lambda: browser.new_context(geolocation={"latitude": 100, "longitude": 0}),
            "Browser.new_context: geolocation.latitude: precondition -90 <= LATITUDE <= 90 failed.",
        ),
        (
            lambda: browser.new_context(geolocation={"latitude": 0, "longitude": 200}),
            "Browser.new_context: geolocation.longitude: precondition -180 <= LONGITUDE <= 180 failed.",
        ),
        (
            lambda: browser.new_context(geolocation={"latitude": 100, "longitude": 200}),
            "Browser.new_context: geolocation.longitude: precondition -180 <= LONGITUDE <= 180 failed.",
        ),
        (
            lambda: browser.new_context(geolocation={"latitude": 0, "longitude": 0, "accuracy": -1}),
            "Browser.new_context: geolocation.accuracy: precondition 0 <= ACCURACY failed.",
        ),
        (
            lambda: browser.new_context(geolocation={"latitude": "x", "longitude": 0}),
            "Browser.new_context: geolocation.latitude: expected float, got string",
        ),
        (
            lambda: browser.new_context(geolocation="bad"),
            "Browser.new_context: geolocation: expected object, got string",
        ),
    ]
    for operation, message in invalid_context_cases:
        expect_error(operation, message)

    expect_error(
        lambda: browser.new_page(geolocation={"latitude": 0, "longitude": -200}),
        "Browser.new_page: geolocation.longitude: precondition -180 <= LONGITUDE <= 180 failed.",
    )

    context = browser.new_context()
    try:
        invalid_set_cases = [
            (
                lambda: context.set_geolocation({"longitude": 0}),
                "BrowserContext.set_geolocation: geolocation.latitude: expected float, got undefined",
            ),
            (
                lambda: context.set_geolocation({"latitude": 0}),
                "BrowserContext.set_geolocation: geolocation.longitude: expected float, got undefined",
            ),
            (
                lambda: context.set_geolocation({"latitude": -100, "longitude": 0}),
                "BrowserContext.set_geolocation: geolocation.latitude: precondition -90 <= LATITUDE <= 90 failed.",
            ),
            (
                lambda: context.set_geolocation({"latitude": 0, "longitude": 0, "accuracy": True}),
                "BrowserContext.set_geolocation: geolocation.accuracy: expected float, got boolean",
            ),
            (
                lambda: context.set_geolocation(7),
                "BrowserContext.set_geolocation: geolocation: expected object, got number",
            ),
        ]
        for operation, message in invalid_set_cases:
            expect_error(operation, message)
    finally:
        context.close()


@case
def api_request_context_redirect_options(page):
    with header_case_server() as base_url:
        no_redirect = page.context.request.get(f"{base_url}/redirect-one", max_redirects=0)
        followed = page.context.request.get(f"{base_url}/redirect-one", max_redirects=1)

    assert no_redirect.status == 302
    assert no_redirect.url == f"{base_url}/redirect-one"
    assert followed.status == 200
    assert followed.url == f"{base_url}/headers"


@case
def api_request_context_dispose_reason_and_storage_state(page, playwright):
    with header_case_server() as base_url:
        request = playwright.request.new_context(base_url=base_url)
        request.dispose()
        try:
            request.get("/headers")
        except Exception as exc:
            closed_error = str(exc).splitlines()[0]
        else:
            raise AssertionError("disposed APIRequestContext.get() should fail")
        try:
            request.storage_state()
        except Exception as exc:
            storage_error = str(exc).splitlines()[0]
        else:
            raise AssertionError("disposed APIRequestContext.storage_state() should fail")

        reason_request = playwright.request.new_context(base_url=base_url)
        reason_request.dispose(reason="api parity shutdown")
        try:
            reason_request.get("/headers")
        except Exception as exc:
            reason_error = str(exc).splitlines()[0]
        else:
            raise AssertionError("APIRequestContext.get() after dispose(reason=...) should fail")

    assert closed_error == "APIRequestContext.get: Target page, context or browser has been closed"
    assert storage_error == "APIRequestContext.storage_state: Target page, context or browser has been closed"
    assert reason_error == "api parity shutdown"


@case
def negative_redirect_and_retry_option_validation(page):
    with header_case_server() as base_url:
        request_errors = []
        for option_name in ("max_redirects", "max_retries"):
            try:
                page.context.request.get(f"{base_url}/headers", **{option_name: -1})
            except AssertionError as exc:
                request_errors.append((option_name, str(exc)))

        route_errors = []

        def handler(route):
            for option_name in ("max_redirects", "max_retries"):
                try:
                    route.fetch(url=f"{base_url}/headers", **{option_name: -1})
                except AssertionError as exc:
                    route_errors.append((option_name, str(exc)))
            route.fulfill(status=200, json={"errors": route_errors})

        page.route("**/negative-route-options", handler)
        route_response = page.goto(f"{base_url}/negative-route-options")

    assert request_errors == [
        ("max_redirects", "'max_redirects' must be greater than or equal to '0'"),
        ("max_retries", "'max_retries' must be greater than or equal to '0'"),
    ]
    assert route_response.json()["errors"] == [
        ["max_redirects", "Route.fetch: 'max_redirects' must be greater than or equal to '0'"],
        ["max_retries", "Route.fetch: 'max_retries' must be greater than or equal to '0'"],
    ]


@case
def api_request_context_negative_max_redirects_default_matches_playwright(page, playwright):
    with header_case_server() as base_url:
        request = playwright.request.new_context(base_url=base_url, max_redirects=-1)
        try:
            no_redirect = request.get("/redirect-one")
            followed = request.get("/redirect-one", max_redirects=1)
            disabled = request.get("/redirect-one", max_redirects=0)
            try:
                request.get("/redirect-one", max_redirects=-1)
            except AssertionError as exc:
                per_call_error = str(exc)
            else:
                raise AssertionError("per-call max_redirects=-1 unexpectedly succeeded")
        finally:
            request.dispose()

    assert {
        "default": {"status": no_redirect.status, "url": no_redirect.url},
        "override_follow": {"status": followed.status, "url": followed.url},
        "override_disable": {"status": disabled.status, "url": disabled.url},
        "per_call_error": per_call_error,
    } == {
        "default": {"status": 302, "url": f"{base_url}/redirect-one"},
        "override_follow": {"status": 200, "url": f"{base_url}/headers"},
        "override_disable": {"status": 302, "url": f"{base_url}/redirect-one"},
        "per_call_error": "'max_redirects' must be greater than or equal to '0'",
    }


@case
def api_request_context_negative_timeout_errors_match_playwright(page, playwright):
    sync_api = importlib.import_module(f"{page.__class__.__module__.split('.', 1)[0]}.sync_api")
    with slow_body_server() as base_url:
        errors = []
        default_timeout = playwright.request.new_context(base_url=base_url, timeout=-1)
        try:
            try:
                default_timeout.get("/slow-fetch")
            except sync_api.TimeoutError as exc:
                errors.append(["default_get", str(exc).splitlines()[0]])
            else:
                raise AssertionError("default timeout get unexpectedly succeeded")
        finally:
            default_timeout.dispose()

        per_call_timeout = playwright.request.new_context(base_url=base_url)
        try:
            try:
                per_call_timeout.get("/slow-fetch", timeout=-1)
            except sync_api.TimeoutError as exc:
                errors.append(["per_call_get", str(exc).splitlines()[0]])
            else:
                raise AssertionError("per-call timeout get unexpectedly succeeded")
        finally:
            per_call_timeout.dispose()

    assert errors == [
        ["default_get", "APIRequestContext.get: Timeout -1ms exceeded."],
        ["per_call_get", "APIRequestContext.get: Timeout -1ms exceeded."],
    ]


@case
def page_route_times_intercepts_once(page):
    with header_case_server() as base_url:
        calls = []

        def handler(route):
            calls.append(route.request.url)
            route.fulfill(json={"routed": True})

        page.route("**/echo-headers", handler, times=1)
        page.goto(f"{base_url}/echo-headers")
        first_body = page.text_content("body")
        page.goto(f"{base_url}/echo-headers")
        second_body = page.text_content("body")

    assert json.loads(first_body) == {"routed": True}
    assert json.loads(second_body)["x-route-header"] is None
    assert len(calls) == 1


@case
def page_route_times_numeric_edges_match_playwright(page):
    browser = page.context.browser

    cases = [
        ("zero", 0, 3, 3),
        ("false", False, 3, 3),
        ("true", True, 3, 1),
        ("negative", -1, 3, 1),
        ("float", 1.5, 3, 2),
    ]

    with header_case_server() as base_url:
        for label, times, attempts, expected_calls in cases:
            context = browser.new_context()
            route_page = context.new_page()
            calls = []

            def handler(route, request, label=label):
                calls.append(request.url)
                route.fulfill(json={"routed": label})

            try:
                route_page.route("**/echo-headers", handler, times=times)
                bodies = []
                for _ in range(attempts):
                    route_page.goto(f"{base_url}/echo-headers")
                    bodies.append(json.loads(route_page.text_content("body")))
            finally:
                context.close()

            assert len(calls) == expected_calls
            for body in bodies[:expected_calls]:
                assert body == {"routed": label}
            for body in bodies[expected_calls:]:
                assert body["x-route-header"] is None


@case
def context_route_times_numeric_edges_match_playwright(page):
    browser = page.context.browser

    cases = [
        ("zero", 0, 3, 3),
        ("false", False, 3, 3),
        ("true", True, 3, 1),
        ("negative", -1, 3, 1),
        ("float", 1.5, 3, 2),
    ]

    with header_case_server() as base_url:
        for label, times, attempts, expected_calls in cases:
            context = browser.new_context()
            calls = []

            def handler(route, request, label=label):
                calls.append(request.url)
                route.fulfill(json={"routed": label})

            try:
                context.route("**/echo-headers", handler, times=times)
                route_page = context.new_page()
                bodies = []
                for _ in range(attempts):
                    route_page.goto(f"{base_url}/echo-headers")
                    bodies.append(json.loads(route_page.text_content("body")))
            finally:
                context.close()

            assert len(calls) == expected_calls
            for body in bodies[:expected_calls]:
                assert body == {"routed": label}
            for body in bodies[expected_calls:]:
                assert body["x-route-header"] is None


@case
def page_unroute_all_accepts_wait_behavior(page):
    with header_case_server() as base_url:
        calls = []

        def handler(route):
            calls.append(route.request.url)
            route.fulfill(status=200, body="waited")

        page.goto(f"{base_url}/headers")
        page.route(re.compile(r".*/query\?unroute=wait$"), handler)
        assert page.evaluate("async () => await fetch('/query?unroute=wait').then(response => response.text())") == "waited"
        page.unroute_all(behavior="wait")

        assert len(calls) == 1
        assert page.goto(f"{base_url}/query?unroute=wait").json() == {
            "path": "/query",
            "query": {"unroute": ["wait"]},
        }


@case
def page_and_context_unroute_all_are_scoped(page):
    with header_case_server() as base_url:
        context = page.context

        context.route("**/query?scope=context", lambda route: route.fulfill(json={"scope": "context"}))
        page.route("**/query?scope=page", lambda route: route.fulfill(json={"scope": "page"}))
        page.unroute_all()

        assert page.goto(f"{base_url}/query?scope=context").json() == {"scope": "context"}
        assert page.goto(f"{base_url}/query?scope=page").json() == {
            "path": "/query",
            "query": {"scope": ["page"]},
        }

        page.route("**/query?scope=page-again", lambda route: route.fulfill(json={"scope": "page-again"}))
        context.unroute_all()

        assert page.goto(f"{base_url}/query?scope=context").json() == {
            "path": "/query",
            "query": {"scope": ["context"]},
        }
        assert page.goto(f"{base_url}/query?scope=page-again").json() == {"scope": "page-again"}


@case
def page_unroute_removes_matching_handler(page):
    with header_case_server() as base_url:
        def handler(route):
            route.fulfill(json={"routed": True})

        page.route("**/echo-headers", handler)
        page.unroute("**/echo-headers", handler)
        response = page.goto(f"{base_url}/echo-headers")

    assert response.json()["x-route-header"] is None


@case
def route_handler_errors_surface_on_unroute_cleanup(page):
    with header_case_server() as base_url:
        context = page.context

        def trigger(path: str) -> None:
            try:
                page.goto(f"{base_url}/{path}", timeout=250)
            except Exception:
                return
            raise AssertionError(f"expected navigation to time out for {path}")

        def captured_error(call):
            try:
                call()
            except Exception as exc:
                return [type(exc).__name__, str(exc).splitlines()[0]]
            raise AssertionError("expected route cleanup to surface the handler error")

        def page_unroute_handler(route):
            raise TypeError("page unroute boom")

        page.route("**/route-handler-error-page-unroute", page_unroute_handler)
        trigger("route-handler-error-page-unroute")
        assert captured_error(lambda: page.unroute("**/route-handler-error-page-unroute", page_unroute_handler)) == [
            "TypeError",
            "Page.unroute: page unroute boom",
        ]

        def page_unroute_all_handler(route):
            raise RuntimeError("page unroute_all boom")

        page.route("**/route-handler-error-page-unroute-all", page_unroute_all_handler)
        trigger("route-handler-error-page-unroute-all")
        assert captured_error(lambda: page.unroute_all()) == [
            "RuntimeError",
            "Page.unroute_all: page unroute_all boom",
        ]

        def context_unroute_handler(route):
            raise ValueError("context unroute boom")

        context.route("**/route-handler-error-context-unroute", context_unroute_handler)
        trigger("route-handler-error-context-unroute")
        assert captured_error(
            lambda: context.unroute("**/route-handler-error-context-unroute", context_unroute_handler)
        ) == [
            "ValueError",
            "BrowserContext.unroute: context unroute boom",
        ]

        def context_unroute_all_handler(route):
            raise RuntimeError("context unroute_all boom")

        context.route("**/route-handler-error-context-unroute-all", context_unroute_all_handler)
        trigger("route-handler-error-context-unroute-all")
        assert captured_error(lambda: context.unroute_all()) == [
            "RuntimeError",
            "BrowserContext.unroute_all: context unroute_all boom",
        ]


BENCHMARK_STRICT_CASE_NAMES = [
    "goto_and_title",
    "set_content_and_read_text",
    "evaluate_json",
    "click_button",
    "fill_input",
    "type_input",
    "locator_count",
    "locator_nth_text",
    "locator_collection_text_helpers",
    "locator_collection_input_value_helpers",
    "locator_collection_attribute_helpers",
    "locator_collection_inner_html_helpers",
    "locator_collection_visibility_helpers",
    "locator_collection_enabled_helpers",
    "role_locator",
    "text_locator",
    "wait_for_selector",
    "screenshot",
    "webvoyager_checkout_workflow",
    "mind2web_table_triage_workflow",
    "research_navigation_workflow",
    "screenshot_type_quality_and_path_extension",
    "screenshot_scale_and_clip",
    "screenshot_omit_background",
    "screenshot_mask_and_mask_color",
    "role_locator_implicit_roles_and_names",
    "file_input_button_role_matches_playwright",
    "input_role_defaults_match_playwright",
    "page_console_messages_history_and_clear",
    "page_wait_for_event_console_does_not_replay_history",
    "context_console_message_captures_immediate_popup_console",
    "page_requests_history_records_recent_requests",
    "page_once_and_remove_listener_for_console",
    "page_errors_history_and_clear",
    "locator_click_force_skips_receives_events_check",
    "locator_hover_force_skips_receives_events_check",
    "hover_dispatches_native_pointer_mouse_events_like_playwright",
    "mouse_wheel_dispatches_single_trusted_event_like_playwright",
    "mouse_wheel_fractional_delta_nudges_like_playwright",
    "mouse_click_count_dispatch_sequence_matches_playwright",
    "mouse_dblclick_delay_reuses_click_count_sequence_like_playwright",
    "locator_bounding_box_and_scroll_into_view",
    "default_viewport_size",
    "check_and_uncheck",
    "native_check_uncheck_mouse_events_and_prevent_default_match_playwright",
    "select_option",
    "locator_filter_has_text",
    "locator_constructor_filter_options",
    "label_and_placeholder_locators",
    "label_locator_aria_sources_match_playwright",
    "test_id_alt_and_title_locators",
    "test_id_regex_locators_match_playwright",
    "internal_text_and_testid_selector_engines_match_playwright",
    "internal_role_selector_engine_matches_playwright",
    "frame_locator_reads_srcdoc",
    "wait_for_function",
    "frame_wait_for_function_returns_js_handle",
    "dom_node_serialization_and_frame_handle",
    "evaluate_handle_property",
    "expect_console_message",
    "expect_object_set_options_default_timeout",
    "expect_api_response_to_be_ok",
    "expect_locator_extra_assertions",
    "aria_snapshot_stateful_controls_and_details_match_playwright",
    "native_select_optgroup_snapshots_match_playwright",
    "closed_details_hidden_content_snapshots_match_playwright",
    "aria_snapshot_common_widget_roles_match_playwright",
    "semantic_container_child_snapshots_match_playwright",
    "author_named_control_value_snapshots_match_playwright",
    "labelledby_form_control_sources_match_playwright",
    "svg_image_role_and_snapshot_match_playwright",
    "decorative_empty_alt_images_match_playwright",
    "presentational_image_conflicts_match_playwright",
    "semantic_roles_figure_term_definition_math_match_playwright",
    "native_iframe_snapshot_role_match_playwright",
    "native_area_and_menu_snapshot_roles_match_playwright",
    "mark_role_and_snapshot_match_playwright",
    "generic_role_name_and_snapshot_match_playwright",
    "live_region_role_names_and_snapshots_match_playwright",
]

_CASES_BY_NAME = {case.__name__: case for case in CASES}
BENCHMARK_STRICT_SKIPPED_CASE_NAMES = [
    name for name in BENCHMARK_STRICT_CASE_NAMES if _CASES_BY_NAME[name].__code__.co_argcount != 1
]
BENCHMARK_STRICT_CASES = [
    _CASES_BY_NAME[name]
    for name in BENCHMARK_STRICT_CASE_NAMES
    if _CASES_BY_NAME[name].__code__.co_argcount == 1
]
