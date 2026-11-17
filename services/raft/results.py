"""Abgeschlossene, bestätigte Partien im Hintergrund speichern."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Protocol

from queens_engine import Phase

from .replication import Replication

log = logging.getLogger(__name__)


class ResultStore(Protocol):
    async def save(self, game_id: str, results: list[dict], knocker: str | None) -> bool: ...


class ResultWriter:
    def __init__(self, replication: Replication, repository: ResultStore,
                 retry_errors: tuple[type[Exception], ...] = (OSError,)) -> None:
        self.replication = replication
        self.repository = repository
        self.retry_errors = retry_errors
        self.saved: set[str] = set()

    async def write_finished(self) -> None:
        if not self.replication.is_leader:
            return
        term = self.replication.node.current_term
        for game in list(self.replication.state_machine.games.values()):
            if not self.replication.is_leader or self.replication.node.current_term != term:
                return
            if game.phase != Phase.FINISHED or game.game_id in self.saved:
                continue
            rows = [
                {"player_id": player.player_id, "name": player.name,
                 "score": game.scores[player.player_id], "winner": player.player_id in game.winners}
                for player in game.players
            ]
            knocker = game.get_player(game.knocker_id)
            try:
                await self.repository.save(game.game_id, rows, knocker.name if knocker else None)
            except self.retry_errors as error:
                log.warning("Ergebnis für %s konnte nicht gespeichert werden: %s", game.game_id, error)
                return
            self.saved.add(game.game_id)

    async def run(self) -> None:
        while True:
            await self.write_finished()
            await asyncio.sleep(1)


@asynccontextmanager
async def result_writer(replication: Replication, dsn: str | None):
    """Im Lifespan des Game-Service öffnen. Ohne --db wird kein Pool benötigt."""
    if dsn is None:
        yield
        return

    from asyncpg import InterfaceError, PostgresConnectionError
    from asyncpg.exceptions import OperatorInterventionError

    from ..db import Database, DatabaseUnavailable, ResultRepository

    database = Database(dsn)
    # Bei der erstmaligen Schema-Anlage können Verbindungsfehler direkt ankommen.
    retry_errors = (DatabaseUnavailable, OSError, asyncio.TimeoutError, InterfaceError,
                    PostgresConnectionError, OperatorInterventionError)
    writer = ResultWriter(replication, ResultRepository(database), retry_errors)
    task = asyncio.create_task(writer.run())
    try:
        yield
    finally:
        task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await task
        finally:
            await database.close()
