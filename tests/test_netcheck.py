"""Tests für netcheck.py: die drei Fehlerfälle (Zeitüberschreitung,
abgelehnt, Name unbekannt), Adress-Einordnung und /health."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import netcheck


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("ip, kind", [
    ("127.0.0.1", "loopback"), ("192.168.178.20", "lokal"), ("10.0.0.5", "lokal"),
    ("172.20.10.2", "lokal"), ("100.101.102.103", "tailscale"),
    ("169.254.1.1", "link-local"), ("93.184.216.34", "öffentlich"), ("hallo", "ungültig"),
])
def test_classify(ip, kind):
    assert netcheck.classify(ip) == kind


@pytest.mark.parametrize("target, expected", [
    ("ws://192.168.0.10:8000/ws", ("192.168.0.10", 8000)),
    ("http://host.local", ("host.local", 8000)),
    ("10.0.0.1:9000", ("10.0.0.1", 9000)),
    ("10.0.0.1", ("10.0.0.1", 8000)),
])
def test_parse_target(target, expected):
    assert netcheck.parse_target(target) == expected


def test_tcp_ok_and_refused():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        assert netcheck.check_tcp("127.0.0.1", server.getsockname()[1])[0] == "ok"
    assert netcheck.check_tcp("127.0.0.1", free_port())[0] == "refused"


def test_tcp_timeout_and_dns(monkeypatch):
    def timeout(*args, **kwargs):
        raise socket.timeout("timed out")
    monkeypatch.setattr(socket, "create_connection", timeout)
    assert netcheck.check_tcp("10.1.2.3", 8000)[0] == "timeout"

    def unknown(*args, **kwargs):
        raise socket.gaierror(11001, "getaddrinfo failed")
    monkeypatch.setattr(socket, "create_connection", unknown)
    assert netcheck.check_tcp("gibtsnicht", 8000)[0] == "dns"


def test_health_and_client_report(capsys):
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        assert netcheck.check_health("127.0.0.1", port) == (True, {"status": "ok"})
        assert netcheck.client_report(f"ws://127.0.0.1:{port}/ws", 8000) == 0
        assert "Alles bereit" in capsys.readouterr().out
    finally:
        server.shutdown()
    assert netcheck.client_report(f"127.0.0.1:{free_port()}", 8000) == 1
    assert "abgelehnt" in capsys.readouterr().out


def test_public_ip_warning(monkeypatch, capsys):
    monkeypatch.setattr(netcheck, "check_tcp", lambda *a, **k: ("timeout", "timed out"))
    assert netcheck.client_report("93.184.216.34", 8000) == 1
    out = capsys.readouterr().out
    assert "öffentliche IP" in out and "Firewall" in out


def test_local_ips_without_loopback():
    assert all(not ip.startswith("127.") for ip in netcheck.local_ips())
