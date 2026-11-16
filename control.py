"""Leitstand: HTTP-Steuerung des lokalen Backends für den Browser.

`run_local.py` startet ihn mit (Standard-Port 8080, nur 127.0.0.1). Er liefert
den Browser-Client aus client/web/ aus – auch dann, wenn das Gateway gerade
gestoppt ist – und bietet eine kleine JSON-API, mit der die Seite (Tab
„Server“) die Dienste starten, stoppen, isolieren und beobachten kann.

API (JSON):
  GET  /api/config                       {"gateway_port", "public", "control": true}
  GET  /api/status                       Dienste, Raft-Status, Leader, Datenbank
  GET  /api/logs/<name>?lines=80         Ende der Log-Datei eines Dienstes
  GET  /api/results?limit=10             letzte Partien aus PostgreSQL
  POST /api/start-all | /api/stop-all
  POST /api/services/<name>/start|kill|isolate|heal

Schutz: Der Leitstand lauscht nur auf 127.0.0.1. POST verlangt
`Content-Type: application/json` und eine lokale bzw. fehlende Origin. So
kann keine fremde Webseite im Browser Dienste beenden.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx

if TYPE_CHECKING:  # pragma: no cover
    from run_local import Backend

WEB_DIR = Path(__file__).resolve().parent / "client" / "web"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
DB_CACHE_SECONDS = 5.0


class ControlError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Control:
    """Operationen des Leitstands, unabhängig von HTTP (testbar)."""

    def __init__(self, backend: "Backend") -> None:
        self.backend = backend
        self.lock = threading.Lock()  # Starten/Beenden nicht gleichzeitig
        self.pool = ThreadPoolExecutor(max_workers=8)
        self._db: tuple[float, Optional[str]] = (0.0, None)

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    # --- Abfragen ---------------------------------------------------------------------
    def config(self) -> dict[str, Any]:
        args = self.backend.args
        return {"control": True, "gateway_port": args.gateway_port,
                "public": bool(args.public)}

    def database(self, fresh: bool = False) -> dict[str, Any]:
        from run_local import check_database
        checked_at, error = self._db
        if fresh or time.monotonic() - checked_at > DB_CACHE_SECONDS:
            error = check_database(self.backend.dsn)
            self._db = (time.monotonic(), error)
        return {"ok": error is None, "error": error, "dsn": self.backend.dsn}

    def _probe(self, service: Any) -> Optional[dict]:
        if not service.running:
            return None
        path = "/status" if service.kind == "raft" else "/health"
        return self.backend.get(service.url + path)

    def status(self) -> dict[str, Any]:
        services = list(self.backend.services.values())
        probes = list(self.pool.map(self._probe, services))
        db = self.pool.submit(self.database)
        now = time.monotonic()
        rows = []
        for service, probe in zip(services, probes):
            rows.append({
                "name": service.name, "kind": service.kind, "url": service.url,
                "running": service.running,
                "responding": probe is not None,
                "uptime": round(now - service.started_at) if service.running else None,
                "detail": probe,
            })
        raft = [r["detail"] for r in rows if r["kind"] == "raft" and r["detail"]]
        reachable = [s for s in raft if not s.get("isolated")]
        leaders = [s for s in reachable if s.get("role") == "leader"]
        leader = None
        if len(leaders) == 1 and all(s.get("leader_id") == leaders[0]["node_id"]
                                     for s in reachable):
            leader = {"node_id": leaders[0]["node_id"], "term": leaders[0]["term"]}
        nodes = len(self.backend.raft_urls)
        return {"services": rows, "leader": leader, "nodes": nodes,
                "majority": nodes // 2 + 1, "database": db.result(),
                "gateway_port": self.backend.args.gateway_port}

    def log(self, name: str, lines: int = 80) -> dict[str, Any]:
        service = self.service(name)
        lines = max(1, min(lines, 500))
        try:
            content = service.log_file.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            content = ""
        return {"name": service.name, "lines": content.splitlines()[-lines:]}

    def results(self, limit: int = 10) -> dict[str, Any]:
        from run_local import recent_results
        try:
            return {"ok": True, "games": recent_results(self.backend.dsn, max(1, min(limit, 50)))}
        except Exception as error:  # Datenbank nicht erreichbar
            return {"ok": False, "error": f"{type(error).__name__}: {error}", "games": []}

    # --- Steuerung --------------------------------------------------------------------
    def service(self, name: str, kinds: tuple[str, ...] = ("raft", "lobby", "gateway")) -> Any:
        service = self.backend.services.get(name.lower())
        if service is None or service.kind not in kinds:
            raise ControlError(404, f"Unbekannter Dienst: {name}")
        return service

    def start(self, name: str) -> dict[str, Any]:
        service = self.service(name)
        with self.lock:
            if service.running:
                return {"ok": True, "message": f"{service.name} läuft bereits."}
            service.start()
        return {"ok": True, "message": f"{service.name} gestartet."}

    def kill(self, name: str) -> dict[str, Any]:
        service = self.service(name)
        with self.lock:
            if not service.running:
                return {"ok": True, "message": f"{service.name} läuft nicht."}
            service.kill()
        return {"ok": True, "message": f"{service.name} beendet."}

    def isolate(self, name: str, isolated: bool) -> dict[str, Any]:
        service = self.service(name, ("raft",))
        if not service.running:
            raise ControlError(409, f"Knoten {service.name} läuft nicht.")
        try:
            response = self.backend.http.post(f"{service.url}/admin/isolate",
                                              json={"isolated": isolated})
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ControlError(502, f"Knoten {service.name} nicht erreichbar "
                                    f"({type(error).__name__}).") from error
        return {"ok": True, "message": f"Knoten {service.name} "
                                       + ("isoliert." if isolated else "wieder verbunden.")}

    def start_all(self) -> dict[str, Any]:
        db = self.database(fresh=True)
        with self.lock:
            self.backend.start_all()
        message = "Alle Dienste gestartet."
        if not db["ok"]:
            message += " Achtung: PostgreSQL nicht erreichbar, Lobbys funktionieren erst mit Datenbank."
        return {"ok": True, "message": message}

    def stop_all(self) -> dict[str, Any]:
        with self.lock:
            for service in reversed(list(self.backend.services.values())):
                service.kill()
        return {"ok": True, "message": "Alle Dienste beendet."}


def make_handler(control: Control) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "QueensControl/1"

        def log_message(self, *args: Any) -> None:  # kein Rauschen in der Konsole
            pass

        # --- Hilfen ---
        def origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = urlparse(origin).hostname or ""
            return host in LOCAL_HOSTS

        def cors(self) -> None:
            origin = self.headers.get("Origin")
            if origin and self.origin_ok():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

        def reply(self, status: int, data: Any) -> None:
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.cors()
            self.end_headers()
            self.wfile.write(body)

        def static(self, path: str) -> None:
            relative = path.lstrip("/") or "index.html"
            target = (WEB_DIR / relative).resolve()
            if WEB_DIR.resolve() not in target.parents or not target.is_file():
                self.reply(404, {"detail": "Nicht gefunden."})
                return
            body = target.read_bytes()
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix == ".js":
                ctype = "text/javascript"
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def run(self, func: Any, *args: Any) -> None:
            try:
                self.reply(200, func(*args))
            except ControlError as error:
                self.reply(error.status, {"ok": False, "detail": error.message})
            except Exception as error:  # Leitstand soll nie abstürzen
                self.reply(500, {"ok": False, "detail": f"{type(error).__name__}: {error}"})

        # --- Methoden ---
        def do_OPTIONS(self) -> None:
            if not self.origin_ok():
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(204)
            self.cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            parts = [p for p in url.path.split("/") if p]

            def number(key: str, default: int) -> int:
                try:
                    return int(query.get(key, [default])[0])
                except ValueError:
                    return default

            if parts[:1] != ["api"]:
                self.static(url.path)
            elif parts == ["api", "config"]:
                self.run(control.config)
            elif parts == ["api", "status"]:
                self.run(control.status)
            elif parts == ["api", "results"]:
                self.run(control.results, number("limit", 10))
            elif len(parts) == 3 and parts[1] == "logs":
                self.run(control.log, parts[2], number("lines", 80))
            else:
                self.reply(404, {"detail": "Unbekannter Endpunkt."})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 65536))
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if not self.origin_ok() or ctype != "application/json":
                self.reply(403, {"ok": False, "detail": "Nur lokal und mit JSON erlaubt."})
                return
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if parts == ["api", "start-all"]:
                self.run(control.start_all)
            elif parts == ["api", "stop-all"]:
                self.run(control.stop_all)
            elif len(parts) == 4 and parts[:2] == ["api", "services"]:
                name, op = parts[2], parts[3]
                ops = {"start": lambda: control.start(name), "kill": lambda: control.kill(name),
                       "isolate": lambda: control.isolate(name, True),
                       "heal": lambda: control.isolate(name, False)}
                if op in ops:
                    self.run(ops[op])
                else:
                    self.reply(404, {"ok": False, "detail": f"Unbekannte Operation: {op}"})
            else:
                self.reply(404, {"ok": False, "detail": "Unbekannter Endpunkt."})

    return Handler


class ControlServer:
    """Leitstand in einem Hintergrund-Thread."""

    def __init__(self, backend: "Backend", port: int, host: str = "127.0.0.1") -> None:
        self.control = Control(backend)
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.control))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True,
                                       name="control")

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self) -> "ControlServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.control.close()
