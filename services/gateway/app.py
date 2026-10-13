"""PLATZHALTER – Realtime-Gateway (FastAPI + WebSockets).

Aufgaben:
  - WebSocket-Verbindungen der Browser-Clients halten
  - Nachrichten der Clients in Aktionen übersetzen (action_from_dict)
  - Aktionen an das GameBackend weiterleiten (heute lokal, später Raft-Leader)
  - Ereignisse an die richtigen Clients verteilen (events_for)
  - Jedem Client nach jeder Änderung seine Spielersicht senden (player_view)
  - Reconnect: Client meldet sich mit game_id/player_id neu an und erhält
    die aktuelle Sicht

Start (später): uvicorn services.gateway.app:app --port 8000
"""

from __future__ import annotations

from queens_engine import Event

from ..backend import GameBackend, LocalGameBackend

try:
    from fastapi import FastAPI, WebSocket
except ImportError:  # FastAPI wird erst für die Netzwerk-Variante benötigt
    FastAPI = None  # type: ignore
    WebSocket = None  # type: ignore


def events_for(player_id: str, events: list[Event]) -> list[dict]:
    """Filtert Ereignisse auf das, was dieser Spieler sehen darf."""
    return [e.to_dict() for e in events if e.is_visible_to(player_id)]


class ConnectionManager:
    """PLATZHALTER – verwaltet WebSockets pro (game_id, player_id)."""

    def __init__(self) -> None:
        self.connections: dict[tuple[str, str], "WebSocket"] = {}

    async def connect(self, game_id: str, player_id: str, ws: "WebSocket") -> None:
        raise NotImplementedError

    def disconnect(self, game_id: str, player_id: str) -> None:
        raise NotImplementedError

    async def broadcast(self, game_id: str, events: list[Event],
                        backend: GameBackend) -> None:
        """Für jeden verbundenen Spieler: gefilterte Ereignisse + neue Sicht senden."""
        raise NotImplementedError


backend: GameBackend = LocalGameBackend()  # später: RaftGameBackend(cluster=[...])
manager = ConnectionManager()

if FastAPI is not None:
    app = FastAPI(title="Queens Gateway")

    @app.websocket("/ws/{game_id}/{player_id}")
    async def game_socket(websocket: WebSocket, game_id: str, player_id: str):
        """Ablauf:
        1. manager.connect(...) und aktuelle Sicht senden
        2. Schleife: JSON empfangen -> action_from_dict (player_id erzwingen!)
           -> backend.submit -> manager.broadcast
        3. RuleViolation als Fehlermeldung nur an diesen Client senden
        4. Bei Verbindungsabbruch manager.disconnect
        """
        raise NotImplementedError
