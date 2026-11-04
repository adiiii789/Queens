"""Lobby-Service (FastAPI, REST), beliebig viele Instanzen parallel.

Eine Instanz speichert selbst nichts: alle Lobby-Daten liegen in PostgreSQL
(services/db.py), daher kann jede Instanz jede Anfrage beantworten und eine
ausgefallene Instanz fällt nicht auf. Sind alle Spieler bereit, startet genau
die Instanz, die das StartTicket bekommen hat, die Partie im Game-Service-
Cluster (CreateGame, dann StartGame).

Start (Beispiel, zwei Instanzen):
  python -m services.lobby.app --id l1 --port 8001 --clusters "a=http://127.0.0.1:8101|http://127.0.0.1:8102|http://127.0.0.1:8103"
  python -m services.lobby.app --id l2 --port 8002 --clusters "a=…"

Endpunkte (Antwort jeweils die Lobby, siehe `lobby_response`):
  POST /lobbies                 {"player_id", "name"}           Lobby anlegen
  GET  /lobbies/{id}                                            Lobby abfragen
  POST /lobbies/{id}/join       {"player_id", "name"}           beitreten
  POST /lobbies/{id}/leave      {"player_id"}                   verlassen
  POST /lobbies/{id}/ready      {"player_id", "ready": bool}    bereit melden
  GET  /health                  {"instance", "db"}

Lobby-Antwort:
  {"lobby_id", "host_id", "status", "game_id", "cluster", "cluster_nodes",
   "players": [{"player_id", "name", "ready"}], "served_by"}
  status: waiting | starting | started | finished (nach leave auch "deleted")
Fehler: HTTP 403/404/409/503 mit {"detail": "<Meldung>"}.
"""

from __future__ import annotations

import argparse
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..cluster_stub import ClusterFactory, ClusterUnavailable, cluster_client_factory
from ..db import (
    STALE_START_SECONDS, Database, DatabaseUnavailable, LobbyError, LobbyRepository,
    StartTicket, normalize_code,
)
from ..netconfig import parse_clusters, setup_logging

log = logging.getLogger("lobby")


class PlayerBody(BaseModel):
    player_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=24)


class LeaveBody(BaseModel):
    player_id: str = Field(min_length=1, max_length=64)


class ReadyBody(BaseModel):
    player_id: str = Field(min_length=1, max_length=64)
    ready: bool = True


class StartFailed(Exception):
    pass


def create_app(instance_id: str, db: Database, clusters: dict[str, list[str]],
               factory: Optional[ClusterFactory] = None,
               unavailable: tuple[type[Exception], ...] = (),
               stale_start: float = STALE_START_SECONDS) -> FastAPI:
    """`factory(urls)` erzeugt einen Cluster-Client (Standard: echter
    ClusterClient, falls vorhanden, sonst Stub). Nach `stale_start` Sekunden
    in `starting` darf eine andere Instanz den Start übernehmen."""
    if factory is None:
        factory, unavailable = cluster_client_factory()
    unavailable = (*unavailable, ClusterUnavailable)
    clients = {name: factory(urls) for name, urls in clusters.items()}
    repo = LobbyRepository(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await db.connect()
        except DatabaseUnavailable as e:
            log.warning("%s – verbinde später erneut", e)
        yield
        for client in clients.values():
            await client.close()
        await db.close()

    app = FastAPI(title=f"Queens Lobby {instance_id}", lifespan=lifespan)

    @app.exception_handler(LobbyError)
    async def lobby_error(request: Request, e: LobbyError):
        return JSONResponse({"detail": e.message, "served_by": instance_id},
                            status_code=e.status)

    @app.exception_handler(DatabaseUnavailable)
    async def db_error(request: Request, e: DatabaseUnavailable):
        log.warning("%s", e)
        return JSONResponse({"detail": "Datenbank nicht erreichbar.",
                             "served_by": instance_id}, status_code=503)

    def lobby_response(lobby: dict[str, Any]) -> dict[str, Any]:
        return {**lobby, "cluster_nodes": clusters.get(lobby.get("cluster") or "", []),
                "served_by": instance_id}

    # --- Spielstart -------------------------------------------------------------
    async def submit(client: Any, command: dict[str, Any]) -> None:
        result = await client.submit(command)
        if not result.get("ok"):
            raise StartFailed(result.get("message") or result.get("error"))

    async def start_game(ticket: StartTicket) -> dict[str, Any]:
        """CreateGame und StartGame sind idempotent (game_id bzw. feste
        action_id), daher darf eine andere Instanz einen hängenden Start
        einfach wiederholen."""
        player_ids = [pid for pid, _ in ticket.players]
        starter = ticket.host_id if ticket.host_id in player_ids else player_ids[0]
        try:
            client = clients[ticket.cluster]
            await submit(client, {"type": "CreateGame", "game_id": ticket.game_id,
                                  "players": [list(p) for p in ticket.players]})
            await submit(client, {"type": "Action", "game_id": ticket.game_id, "action": {
                "type": "StartGame", "player_id": starter,
                "action_id": f"{ticket.game_id}-start", "seed": secrets.token_hex(8)}})
        except (*unavailable, StartFailed, KeyError) as e:
            log.warning("Start von %s fehlgeschlagen (%s: %s), Lobby wieder offen",
                        ticket.game_id, type(e).__name__, e)
            await repo.abort_start(ticket.lobby_id, ticket.game_id)
            raise LobbyError(503, "Spielserver nicht erreichbar. "
                                  "Bitte erneut bereit melden.") from e
        lobby = await repo.mark_started(ticket.lobby_id, ticket.game_id)
        log.info("Partie %s gestartet (Cluster %s, %d Spieler)",
                 ticket.game_id, ticket.cluster, len(ticket.players))
        return lobby

    # --- Endpunkte --------------------------------------------------------------
    @app.post("/lobbies")
    async def create_lobby(body: PlayerBody):
        lobby = await repo.create(body.player_id, body.name)
        log.info("Lobby %s angelegt von %s", lobby["lobby_id"], body.name)
        return lobby_response(lobby)

    @app.get("/lobbies/{lobby_id}")
    async def get_lobby(lobby_id: str):
        lobby = await repo.get(lobby_id)
        if lobby["status"] == "starting":
            # Erholung: hängt der Start (Instanz abgestürzt), übernehmen wir ihn
            ticket = await repo.claim_stale_start(lobby_id, stale_start)
            if ticket is not None:
                log.info("Übernehme hängenden Start von %s", ticket.game_id)
                lobby = await start_game(ticket)
        return lobby_response(lobby)

    @app.post("/lobbies/{lobby_id}/join")
    async def join_lobby(lobby_id: str, body: PlayerBody):
        lobby = await repo.join(lobby_id, body.player_id, body.name)
        return lobby_response(lobby)

    @app.post("/lobbies/{lobby_id}/leave")
    async def leave_lobby(lobby_id: str, body: LeaveBody):
        lobby = await repo.leave(lobby_id, body.player_id)
        if lobby is None:
            log.info("Lobby %s gelöscht (leer)", normalize_code(lobby_id))
            lobby = {"lobby_id": normalize_code(lobby_id), "host_id": None,
                     "status": "deleted", "game_id": None, "cluster": None,
                     "players": []}
        return lobby_response(lobby)

    @app.post("/lobbies/{lobby_id}/ready")
    async def set_ready(lobby_id: str, body: ReadyBody):
        lobby, ticket = await repo.set_ready(lobby_id, body.player_id, body.ready,
                                             list(clusters))
        if ticket is not None:
            lobby = await start_game(ticket)
        return lobby_response(lobby)

    @app.get("/health")
    async def health():
        return {"instance": instance_id, "db": await db.ping()}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Queens Lobby-Service")
    parser.add_argument("--id", default="l1", help="Instanzname, z. B. l1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, help="Standard: 8000 + Nummer aus --id")
    parser.add_argument("--db", help="PostgreSQL-DSN (Standard: $QUEENS_DB)")
    parser.add_argument("--clusters", required=True,
                        help='Game-Service-Cluster: "a=url|url|url;b=…"')
    args = parser.parse_args()

    import uvicorn

    port = args.port
    if port is None:
        digits = "".join(c for c in args.id if c.isdigit())
        port = 8000 + int(digits or 1)
    setup_logging(f"lobby-{args.id}")
    app = create_app(args.id, Database(args.db), parse_clusters(args.clusters))
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
