# -*- coding: utf-8 -*-
"""云端无域名(自签证书)时按 SHA-256 指纹认云端：直连和走代理都必须校验。"""
import hashlib
import http.server
import select
import socket
import ssl
import subprocess
import threading

import pytest

import cloud_report


@pytest.fixture
def https_server(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(crt), "-days", "1", "-subj", "/CN=t"], check=True, capture_output=True)

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = b'{"ok": true, "registered": false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert(crt.read_text())).hexdigest()
    yield f"https://127.0.0.1:{srv.server_address[1]}", fp
    srv.shutdown()


@pytest.fixture
def connect_proxy():
    class P(http.server.BaseHTTPRequestHandler):
        def do_CONNECT(self):
            host, port = self.path.rsplit(":", 1)
            up = socket.create_connection((host, int(port)))
            self.send_response(200)
            self.end_headers()
            a = self.connection
            while True:
                r, _, _ = select.select([a, up], [], [], 5)
                if not r:
                    return
                for s in r:
                    d = s.recv(65536)
                    if not d:
                        return
                    (up if s is a else a).sendall(d)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_pinned_fingerprint_direct(https_server):
    url, fp = https_server
    assert cloud_report.test({"url": url, "token": "t", "tls_fingerprint": fp}, "s")[0]
    ok, msg = cloud_report.test({"url": url, "token": "t", "tls_fingerprint": "00" * 32}, "s")
    assert not ok and "指纹" in msg
    ok, msg = cloud_report.test({"url": url, "token": "t"}, "s")   # 自签证书、没填指纹
    assert not ok


def test_pinned_fingerprint_through_proxy(https_server, connect_proxy, monkeypatch):
    url, fp = https_server
    monkeypatch.setenv("HTTPS_PROXY", connect_proxy)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.delenv("no_proxy", raising=False)
    assert cloud_report.test({"url": url, "token": "t", "tls_fingerprint": fp}, "s")[0]
    ok, _msg = cloud_report.test({"url": url, "token": "t", "tls_fingerprint": "00" * 32}, "s")
    assert not ok   # 走代理也必须校验指纹
