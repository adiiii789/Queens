"""Game-Service: ein Raft-Knoten als eigener Prozess (FastAPI).

Start (Beispiel, drei Knoten auf einem Rechner):
  python -m services.raft.server --id 1 --nodes "1=http://127.0.0.1:8101,2=http://127.0.0.1:8102,3=http://127.0.0.1:8103"
  python -m services.raft.server --id 2 --nodes "…"
  python -m services.raft.server --id 3 --nodes "…"

Endpunkte:
  POST /raft/request_vote    RPC zwischen den Knoten (503, wenn isoliert)
  POST /raft/append_entries  RPC zwischen den Knoten, an die Replikation (503, wenn isoliert)
  POST /client/submit        Befehl einreichen (Replikation, P4)
  GET  /client/views/{game}  Sichten aller Spieler (Replikation, P4)
  GET  /status               Rolle, Term, Leader, Log-Länge, Commit-Index, Partien
  POST /admin/isolate        {"isolated": bool} – Netzwerktrennung für die Demo

Die Clustergröße ist frei: n Knoten verkraften (n − 1) // 2 Ausfälle.
"""

from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from ..netconfig import parse_nodes, setup_logging
from .heartbeat import HeartbeatOnly
from .node import RaftNode
from .storage import JsonStorage
from .transport import HttpTransport

try:  # Log-Replikation (P4); bis zum Merge nur Heartbeats mit leerem Log
    from .client_routes import client_router
    from .replication import Replication
except ImportError:  # pragma: no cover - abhängig vom Branch-Stand
    Replication = None
    client_router = None

log = logging.getLogger("raft.server")

RAFT_PAYLOAD_ERRORS = (TypeError, KeyError, ValueError)


def create_app(node_id: str, nodes: Mapping[str, str], *, election_min: float = 1.5,
               election_max: float = 3.0, heartbeat: float = 0.3,
               rpc_timeout: float = 1.0, data_dir: Optional[str] = None,
               http_transport: Any = None) -> FastAPI:
    """`http_transport` (httpx-Transport) nur für Tests ohne echte Sockets."""
    transport = HttpTransport(nodes, timeout=rpc_timeout, http_transport=http_transport)
    storage = JsonStorage(data_dir, node_id) if data_dir else None
    node = RaftNode(node_id, nodes, transport, election_min=election_min,
                    election_max=election_max, storage=storage)
    if Replication is not None:
        replication = Replication(node, transport, heartbeat=heartbeat)
    else:
        log.warning("services.raft.replication fehlt: nur Leader-Wahl mit leerem Log")
        replication = HeartbeatOnly(node, transport, heartbeat=heartbeat)
    node.attach(replication)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await node.start()
        log.info("Knoten %s gestartet (%d Knoten, Mehrheit %d, Election-Timeout %.1f–%.1f s)",
                 node_id, len(nodes), node.majority, election_min, election_max)
        yield
        await node.stop()
        await transport.close()

    app = FastAPI(title=f"Queens Game-Service {node_id}", lifespan=lifespan)
    app.state.node = node
    app.state.transport = transport

    def isolated_response() -> JSONResponse:
        return JSONResponse({"detail": "Knoten ist isoliert."}, status_code=503)

    async def rpc(name: str, payload: dict) -> Any:
        if transport.isolated:
            return isolated_response()
        try:
            return node.handle_rpc(name, payload)
        except RAFT_PAYLOAD_ERRORS as error:
            return JSONResponse({"detail": f"Ungültiger RPC: {error}"}, status_code=400)

    @app.post("/raft/request_vote")
    async def request_vote(payload: dict = Body(...)):
        return await rpc("request_vote", payload)

    @app.post("/raft/append_entries")
    async def append_entries(payload: dict = Body(...)):
        return await rpc("append_entries", payload)

    @app.get("/status")
    async def status():
        return {**node.status(), "isolated": transport.isolated,
                "replication": type(replication).__name__}

    @app.post("/admin/isolate")
    async def isolate(body: dict = Body(default={})):
        isolated = bool(body.get("isolated", True))
        if isolated != transport.isolated:
            log.warning("Knoten %s %s", node_id,
                        "vom Netz getrennt (isoliert)" if isolated else "wieder verbunden")
        transport.isolated = isolated
        return {"node_id": node_id, "isolated": isolated}

    if client_router is not None:
        app.include_router(client_router(replication))

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Queens Game-Service (Raft-Knoten)")
    parser.add_argument("--id", required=True, help="Knotennummer, z. B. 1")
    parser.add_argument("--nodes", required=True,
                        help='alle Knoten inkl. diesem: "1=http://127.0.0.1:8101,2=…"')
    parser.add_argument("--host", default="127.0.0.1",
                        help="0.0.0.0 = für andere Rechner erreichbar")
    parser.add_argument("--port", type=int, help="Standard: Port aus der eigenen Adresse")
    parser.add_argument("--election-min", type=float, default=1.5)
    parser.add_argument("--election-max", type=float, default=3.0)
    parser.add_argument("--heartbeat", type=float, default=0.3)
    parser.add_argument("--rpc-timeout", type=float, default=1.0)
    parser.add_argument("--data-dir", help="Term und Stimme hier speichern (optional)")
    args = parser.parse_args()

    nodes = parse_nodes(args.nodes)
    if args.id not in nodes:
        parser.error(f"--id {args.id} kommt in --nodes nicht vor")
    port = args.port or urlparse(nodes[args.id]).port
    if port is None:
        parser.error("Port fehlt: --port angeben oder in --nodes mitgeben")

    import uvicorn

    setup_logging(f"raft-{args.id}")
    app = create_app(args.id, nodes, election_min=args.election_min,
                     election_max=args.election_max, heartbeat=args.heartbeat,
                     rpc_timeout=args.rpc_timeout, data_dir=args.data_dir)
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
