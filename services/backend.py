"""Schnittstelle zwischen Gateway und Spiellogik.

Das Gateway spricht nur mit einem `GameBackend`. Heute steckt dahinter die
lokale Variante (Engine direkt im Prozess), später die Raft-Variante
(services/raft). Gateway und Engine müssen dafür nicht geändert werden.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any, Optional

from queens_engine import Action, Event, GameState, RulesConfig, apply, create_game, player_view


class GameBackend(ABC):
    @abstractmethod
    def create_game(self, game_id: str, players: list[tuple[str, str]],
                    config: Optional[RulesConfig] = None) -> None:
        """Legt eine Partie an (aufgerufen vom Lobby-Service, wenn alle ready sind)."""

    @abstractmethod
    def submit(self, game_id: str, action: Action) -> list[Event]:
        """Wendet eine Aktion an und liefert die Ereignisse.
        Wirft RuleViolation bei ungültiger Aktion."""

    @abstractmethod
    def get_state(self, game_id: str) -> GameState:
        """Aktueller (bestätigter) Spielzustand."""

    def view(self, game_id: str, player_id: str) -> dict[str, Any]:
        return player_view(self.get_state(game_id), player_id)


class LocalGameBackend(GameBackend):
    """Ein Prozess, keine Replikation. Für Entwicklung und Tests."""

    def __init__(self) -> None:
        self._games: dict[str, GameState] = {}
        self._lock = threading.Lock()  # Aktionen strikt nacheinander anwenden

    def create_game(self, game_id, players, config=None):
        with self._lock:
            if game_id in self._games:
                raise ValueError(f"Spiel {game_id} existiert bereits.")
            self._games[game_id] = create_game(game_id, players, config)

    def submit(self, game_id, action):
        with self._lock:
            state = self._games[game_id]
            new_state, events = apply(state, action)
            self._games[game_id] = new_state
            return events

    def get_state(self, game_id):
        return self._games[game_id]
