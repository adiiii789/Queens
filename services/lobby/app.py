"""PLATZHALTER – Lobby-Service (FastAPI, REST).

Aufgaben:
  - Lobby erstellen, beitreten, verlassen
  - Ready-Status je Spieler
  - Sind alle ready (2–8 Spieler): Partie im GameBackend anlegen
    (create_game) und StartGame mit zufälligem Seed abschicken
  - Später: Zuweisung der Partie an einen Game-Service-Cluster (Skalierung)

Start (später): uvicorn services.lobby.app:app --port 8001
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    from fastapi import FastAPI
except ImportError:
    FastAPI = None  # type: ignore


@dataclass
class Lobby:
    lobby_id: str
    host_id: str
    players: dict[str, str] = field(default_factory=dict)  # player_id -> name
    ready: set[str] = field(default_factory=set)
    game_id: str | None = None

    def all_ready(self, min_players: int = 2) -> bool:
        return len(self.players) >= min_players and self.ready == set(self.players)


if FastAPI is not None:
    app = FastAPI(title="Queens Lobby")

    @app.post("/lobbies")
    async def create_lobby():
        raise NotImplementedError

    @app.post("/lobbies/{lobby_id}/join")
    async def join_lobby(lobby_id: str):
        raise NotImplementedError

    @app.post("/lobbies/{lobby_id}/ready")
    async def set_ready(lobby_id: str):
        raise NotImplementedError

    @app.get("/lobbies/{lobby_id}")
    async def get_lobby(lobby_id: str):
        raise NotImplementedError
