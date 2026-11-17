"""Tests für den Leitstand (control.py): HTTP-API und Schutz, ohne Dienste zu starten."""

import json
import urllib.error
import urllib.request

import pytest

from control import ControlServer
from run_local import Backend, parse_args


@pytest.fixture
def server(tmp_path):
    args = parse_args(["--control-port", "0", "--db", "postgresql://x:x@127.0.0.1:1/x"])
    backend = Backend(args)
    for service in backend.services.values():
        service.log_file = tmp_path / f"{service.name}.log"
    control = ControlServer(backend, 0).start()
    yield control
    control.stop()
    backend.stop_all()


def request(server, path, method="GET", headers=None, body=None):
    req = urllib.request.Request(server.url.rstrip("/") + path, method=method,
                                 headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def post(server, path, origin=None):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    return request(server, path, "POST", headers, b"{}")


def test_serves_web_client_and_config(server):
    status, headers, body = request(server, "/")
    assert status == 200 and b'src="app.js"' in body
    for asset in ("app.js", "admin.js", "protocol.js", "style.css"):
        assert request(server, f"/{asset}")[0] == 200
    assert request(server, "/../run_local.py")[0] == 404
    status, _, body = request(server, "/api/config")
    config = json.loads(body)
    assert status == 200 and isinstance(config.pop("lan_ips"), list)
    assert config == {"control": True, "gateway_port": 8000, "public": False}


def test_status_lists_all_services_when_stopped(server):
    status, _, body = request(server, "/api/status")
    data = json.loads(body)
    assert status == 200
    assert [s["name"] for s in data["services"]] == ["1", "2", "3", "l1", "l2", "gateway"]
    assert not any(s["running"] for s in data["services"])
    assert data["leader"] is None and data["majority"] == 2
    assert data["database"]["ok"] is False


def test_kill_stopped_service_and_unknown_service(server):
    status, _, body = post(server, "/api/services/gateway/kill")
    assert status == 200 and "läuft nicht" in json.loads(body)["message"]
    assert post(server, "/api/services/nope/start")[0] == 404
    assert post(server, "/api/services/l1/isolate")[0] == 404  # nur Raft-Knoten
    assert post(server, "/api/services/1/isolate")[0] == 409   # Knoten läuft nicht


def test_post_needs_json_and_local_origin(server):
    # einfacher Formular-POST einer fremden Seite: kein JSON → abgelehnt
    plain = request(server, "/api/stop-all", "POST", {"Content-Type": "text/plain"}, b"x")
    assert plain[0] == 403
    assert post(server, "/api/stop-all", origin="https://evil.example")[0] == 403
    status, headers, _ = post(server, "/api/stop-all", origin="http://127.0.0.1:8000")
    assert status == 200 and headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:8000"
    assert request(server, "/api/status", "OPTIONS", {"Origin": "https://evil.example"})[0] == 403


def test_logs_endpoint(server):
    server.control.backend.services["gateway"].log_file.write_text("a\nb\nc\n", encoding="utf-8")
    status, _, body = request(server, "/api/logs/gateway?lines=2")
    assert status == 200 and json.loads(body)["lines"] == ["b", "c"]
