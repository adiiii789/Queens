"""Speichert beendete Partien in PostgreSQL (Raft_Log&Consistency Teil 2).

Nur der aktuelle Leader schreibt. Er sieht regelmäßig nach, welche Partien
im bestätigten (committeten) Zustand beendet sind, und speichert die noch
fehlenden. Ist die Datenbank nicht erreichbar, versucht er es später erneut;
laufende Partien hängen davon nicht ab.

Genau einmal: Nach einem Leader-Wechsel kennt der neue Leader die schon
gespeicherten Partien nicht und schreibt sie erneut. Die Datenbank verwirft
Doppelte (game_id ist Primärschlüssel, ON CONFLICT DO NOTHING in
`ResultRepository.save`). So geht auch nichts verloren, wenn der alte Leader
mitten im Schreiben ausfällt.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Protocol

from queens_engine import GameState, Phase

log = logging.getLogger("raft.results")


class ResultStore(Protocol):
    async def save(self, game_id: str, results: list[dict[str, Any]],
                   knocker: Optional[str] = None) -> bool: ...


def result_rows(state: GameState) -> list[dict[str, Any]]:
    """Je Spieler eine Zeile im Format von `ResultRepository.save`."""
    return [{"player_id": p.player_id, "name": p.name,
             "score": state.scores[p.player_id],
             "winner": p.player_id in state.winners}
            for p in state.players]


def knocker_name(state: GameState) -> Optional[str]:
    player = state.get_player(state.knocker_id) if state.knocker_id else None
    return player.name if player else None


class ResultWriter:
    """`replication` liefert `is_leader` und `state_machine.games`
    (services/raft/replication.py); `store` ist ein ResultRepository."""

    def __init__(self, replication: Any, store: ResultStore,
                 interval: float = 1.0) -> None:
        self.replication = replication
        self.store = store
        self.interval = interval
        self.saved: set[str] = set()  # von diesem Knoten bereits geschrieben
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            if self.replication.is_leader:
                await self.flush()

    def pending(self) -> list[GameState]:
        games = self.replication.state_machine.games
        return [state for game_id, state in list(games.items())
                if state.phase == Phase.FINISHED and game_id not in self.saved]

    async def flush(self) -> int:
        """Speichert alle beendeten, noch nicht gespeicherten Partien.
        Liefert die Anzahl neu gespeicherter Partien."""
        new = 0
        for state in self.pending():
            try:
                inserted = await self.store.save(state.game_id, result_rows(state),
                                                 knocker=knocker_name(state))
            except Exception as error:  # DB weg, Timeout …: beim nächsten Durchlauf erneut
                log.warning("Ergebnis von %s nicht gespeichert (%s: %s), neuer Versuch folgt",
                            state.game_id, type(error).__name__, error)
                return new
            self.saved.add(state.game_id)
            if inserted:
                new += 1
                log.info("Ergebnis von %s gespeichert", state.game_id)
        return new
