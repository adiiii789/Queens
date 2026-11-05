"""Stub für den ClusterClient aus feature/Raft_Log&Consistency.

Gleiche Methoden und Antworten wie `services.raft.client.ClusterClient`, aber
ohne Raft: Befehle werden direkt im Prozess auf die Spiellogik angewendet.
Damit lassen sich Lobby und Gateway entwickeln und testen, bevor der
Raft-Branch gemergt ist. Teilen sich Lobby und Gateway (z. B. im Test) eine
Instanz, sehen beide dieselben Partien.

`cluster_client_factory()` liefert den echten ClusterClient, sobald es ihn
gibt, sonst diesen Stub.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from queens_engine import (
    GameState, RuleViolation, action_from_dict, apply, create_game, player_view,
    validate,
)

log = logging.getLogger("cluster-stub")


class ClusterUnavailable(Exception):
    """Kein Leader innerhalb der Wartezeit erreichbar."""


class StubClusterClient:
    def __init__(self, nodes: list[str] | None = None) -> None:
        self.nodes = list(nodes or [])
        self.games: dict[str, GameState] = {}
        self.available = True  # für Tests: Cluster-Ausfall simulieren
        self.commands: list[dict[str, Any]] = []

    async def close(self) -> None:
        pass

    def _check(self) -> None:
        if not self.available:
            raise ClusterUnavailable("Kein Leader erreichbar")

    async def submit(self, command: dict[str, Any]) -> dict[str, Any]:
        self._check()
        self.commands.append(command)
        ctype = command.get("type")
        if ctype == "CreateGame":
            game_id = command["game_id"]
            if game_id not in self.games:
                try:
                    self.games[game_id] = create_game(
                        game_id, [tuple(p) for p in command["players"]])
                except ValueError as e:
                    return {"ok": False, "error": "rule", "message": str(e)}
            return {"ok": True, "game_id": game_id}
        if ctype == "Action":
            state = self.games.get(command.get("game_id"))
            if state is None:
                return {"ok": False, "error": "rule", "message": "Unbekanntes Spiel."}
            try:
                action = action_from_dict(command["action"])
                if action.action_id not in state.processed_action_ids:
                    validate(state, action)
                new_state, events = apply(state, action)
            except (RuleViolation, KeyError, TypeError, ValueError) as e:
                return {"ok": False, "error": "rule", "message": str(e)}
            self.games[state.game_id] = new_state
            return {"ok": True, "game_id": state.game_id,
                    "events": [e.to_dict() for e in events],
                    "views": self._views(state.game_id)}
        return {"ok": True} if ctype == "NoOp" else {"ok": False, "error": "unknown_command"}

    async def views(self, game_id: str) -> dict[str, Any]:
        self._check()
        if game_id not in self.games:
            return {"ok": False, "error": "unknown_game"}
        return {"ok": True, "views": self._views(game_id)}

    def _views(self, game_id: str) -> dict[str, dict[str, Any]]:
        state = self.games[game_id]
        return {p.player_id: player_view(state, p.player_id) for p in state.players}


ClusterFactory = Callable[[list[str]], Any]


def cluster_client_factory() -> tuple[ClusterFactory, tuple[type[Exception], ...]]:
    """(Fabrik für Cluster-Clients, Ausnahme bei Nichterreichbarkeit)."""
    try:
        from .raft.client import ClusterClient
        from .raft.client import ClusterUnavailable as RealUnavailable
    except ImportError:
        log.warning("services.raft.client fehlt: verwende StubClusterClient "
                    "(Partien nur im Prozess, ohne Raft)")
        return StubClusterClient, (ClusterUnavailable,)
    return ClusterClient, (RealUnavailable, ClusterUnavailable)
