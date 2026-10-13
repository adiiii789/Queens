"""PLATZHALTER – Raft-Knoten für die Replikation des Spielzustands.

Aufteilung:
  - Leader-Wahl und Ausfallerkennung: request_vote, on_election_timeout,
    send_heartbeats
  - Log-Replikation und Konsistenz: append_entries, submit, advance_commit_index,
    apply_committed

Zustandsmaschine ist die Spiellogik: Log-Einträge sind serialisierte Aktionen
(action_to_dict). Nach dem Commit wendet jeder Knoten sie per
queens_engine.apply an, in derselben Reihenfolge.

Geplant: drei Instanzen als eigene Prozesse (FastAPI, eigener Port),
RPCs als HTTP-Requests zwischen den Knoten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from queens_engine import GameState, action_from_dict, apply

from ..backend import GameBackend


class Role(str, Enum):
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


@dataclass
class LogEntry:
    term: int
    game_id: str
    command: dict[str, Any]  # action_to_dict(action) oder {"type": "CreateGame", ...}


@dataclass
class RaftNode:
    node_id: str
    peers: list[str]  # Adressen der anderen Knoten, z. B. "http://localhost:8102"

    role: Role = Role.FOLLOWER
    current_term: int = 0
    voted_for: Optional[str] = None
    log: list[LogEntry] = field(default_factory=list)
    commit_index: int = -1
    last_applied: int = -1
    leader_id: Optional[str] = None

    # Zustandsmaschine: ein Spielzustand pro Partie
    games: dict[str, GameState] = field(default_factory=dict)

    # --- Leader-Wahl ----------------------------------------------------------
    def on_election_timeout(self) -> None:
        """Kein Heartbeat erhalten: Kandidat werden, Term erhöhen, Stimmen anfragen."""
        raise NotImplementedError

    def request_vote(self, term: int, candidate_id: str,
                     last_log_index: int, last_log_term: int) -> dict[str, Any]:
        """RPC RequestVote. Rückgabe: {"term": ..., "vote_granted": bool}"""
        raise NotImplementedError

    def send_heartbeats(self) -> None:
        """Als Leader periodisch leere AppendEntries an alle Peers senden."""
        raise NotImplementedError

    # --- Log-Replikation ------------------------------------------------------
    def submit(self, game_id: str, command: dict[str, Any]) -> int:
        """Nur auf dem Leader: Eintrag anhängen, an Peers replizieren.
        Liefert den Log-Index; Follower leiten an leader_id weiter."""
        raise NotImplementedError

    def append_entries(self, term: int, leader_id: str, prev_log_index: int,
                       prev_log_term: int, entries: list[LogEntry],
                       leader_commit: int) -> dict[str, Any]:
        """RPC AppendEntries (auch Heartbeat). Rückgabe: {"term": ..., "success": bool}"""
        raise NotImplementedError

    def advance_commit_index(self) -> None:
        """Leader: commit_index auf den höchsten Index setzen, den eine
        Mehrheit gespeichert hat (nur Einträge des aktuellen Terms)."""
        raise NotImplementedError

    def apply_committed(self) -> list[tuple[str, list]]:
        """Bestätigte Einträge der Reihe nach auf die Spiellogik anwenden.

        Bereits fertig: so sieht die Anbindung an die Engine aus.
        """
        results = []
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            entry = self.log[self.last_applied]
            action = action_from_dict(entry.command)
            state = self.games[entry.game_id]
            # Regelverstöße sollte der Leader vorab per validate() abfangen.
            new_state, events = apply(state, action)
            self.games[entry.game_id] = new_state
            results.append((entry.game_id, events))
        return results


class RaftGameBackend(GameBackend):
    """PLATZHALTER – GameBackend, das Aktionen über den Raft-Cluster schickt.

    Ersetzt im Gateway später LocalGameBackend.
    """

    def __init__(self, cluster: list[str]) -> None:
        self.cluster = cluster  # Adressen aller Game-Service-Instanzen
        self.leader: Optional[str] = None

    def create_game(self, game_id, players, config=None):
        raise NotImplementedError

    def submit(self, game_id, action):
        """An den aktuellen Leader schicken; bei Fehler/Umleitung neuen
        Leader suchen und erneut senden (gleiche action_id -> kein Doppel)."""
        raise NotImplementedError

    def get_state(self, game_id):
        raise NotImplementedError
