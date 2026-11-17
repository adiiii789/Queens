"""Tests für den Browser-Client: Auslieferung durch das Gateway und
Vollständigkeit der Ereignistexte in app.js."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from services.cluster_stub import StubClusterClient
from services.gateway.app import WEB_DIR, create_app

ROOT = Path(__file__).resolve().parents[1]


def gateway():
    return create_app(["http://127.0.0.1:1"], factory=lambda urls: StubClusterClient())


def test_gateway_serves_web_client_next_to_ws_and_health():
    with TestClient(gateway()) as http:
        page = http.get("/")
        assert page.status_code == 200 and 'src="app.js"' in page.text
        for asset in ("app.js", "protocol.js", "style.css"):
            assert http.get(f"/{asset}").status_code == 200
        assert http.get("/health").json()["status"] == "ok"
        with http.websocket_connect("/ws") as ws:
            ws.send_json({"op": "hello", "name": "Anna"})
            assert ws.receive_json()["op"] == "welcome"


def test_web_client_has_text_for_every_engine_event():
    source = (ROOT / "queens_engine" / "engine.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'(?:public|private)\(\s*(?:[\w.]+,\s*)?"(\w+)"', source))
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    known = set(re.findall(r"^\s{2}(\w+): \(", app_js, re.MULTILINE))
    assert emitted and emitted <= known, f"ohne Text in app.js: {emitted - known}"


def test_web_client_keeps_player_id_per_tab():
    protocol = (WEB_DIR / "protocol.js").read_text(encoding="utf-8")
    assert "sessionStorage" in protocol and "localStorage." not in protocol
