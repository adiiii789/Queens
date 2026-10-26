"""Zugriff auf die zentrale PostgreSQL-Datenbank.

Mehrere Lobby-Instanzen (und die Game-Service-Leader für die Ergebnisse)
greifen gleichzeitig zu. Deshalb läuft jede Änderung als eigene Transaktion,
und die betroffene Lobby-Zeile wird mit `SELECT … FOR UPDATE` gesperrt.
Damit sind z. B. „alle bereit → Spiel starten“ und „Lobby voll?“ auch über
Instanzgrenzen hinweg eindeutig.

Verbindung: Parameter `--db` oder Umgebungsvariable QUEENS_DB, z. B.
  postgresql://queens:queens@127.0.0.1:5432/queens
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import asyncpg

log = logging.getLogger("db")

DEFAULT_DSN = "postgresql://queens:queens@127.0.0.1:5432/queens"
SCHEMA_SQL = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
SCHEMA_LOCK = 0x51EE5  # Advisory-Lock: n Instanzen starten gleichzeitig

# Ohne 0/O und 1/I, damit sich Codes gut vorlesen lassen
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 4
MAX_PLAYERS = 8
MIN_PLAYERS = 2
STALE_START_SECONDS = 15.0

# Fehler, die bedeuten: Datenbank (gerade) nicht erreichbar
_UNAVAILABLE = (
    OSError,
    asyncio.TimeoutError,
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.exceptions.OperatorInterventionError,
)


class DatabaseUnavailable(Exception):
    """PostgreSQL ist nicht erreichbar."""


class LobbyError(Exception):
    """Fachlicher Fehler, wird vom Lobby-Service auf einen HTTP-Status abgebildet."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class StartTicket:
    """Berechtigung, eine Partie im Game-Service zu starten. Wird pro Lobby
    nur genau einmal ausgegeben (oder nach Ablauf übernommen)."""

    lobby_id: str
    game_id: str
    cluster: str
    host_id: str
    players: list[tuple[str, str]]  # (player_id, name) in Beitrittsreihenfolge


class Database:
    """asyncpg-Pool mit Schema-Anlage und Neuverbindung bei Bedarf."""

    def __init__(self, dsn: Optional[str] = None, min_size: int = 1,
                 max_size: int = 5) -> None:
        self.dsn = dsn or os.environ.get("QUEENS_DB") or DEFAULT_DSN
        self.min_size = min_size
        self.max_size = max_size
        self._pool: Optional[asyncpg.Pool] = None
        self._lock = asyncio.Lock()

    async def pool(self) -> asyncpg.Pool:
        """Pool holen; ist er (noch) nicht da, wird neu verbunden."""
        if self._pool is None:
            async with self._lock:
                if self._pool is None:
                    try:
                        pool = await asyncpg.create_pool(
                            self.dsn, min_size=self.min_size, max_size=self.max_size,
                            timeout=3, command_timeout=5)
                    except _UNAVAILABLE as e:
                        raise DatabaseUnavailable(f"Datenbank nicht erreichbar: {e}") from e
                    try:
                        await self._create_schema(pool)
                    except BaseException:
                        await pool.close()
                        raise
                    self._pool = pool
                    log.info("Mit Datenbank verbunden")
        return self._pool

    @staticmethod
    async def _create_schema(pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK)
                await conn.execute(SCHEMA_SQL)

    async def connect(self) -> None:
        await self.pool()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        pool = await self.pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    yield conn
        except _UNAVAILABLE as e:
            raise DatabaseUnavailable(f"Datenbank nicht erreichbar: {e}") from e

    async def ping(self) -> bool:
        try:
            async with self.transaction() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except DatabaseUnavailable:
            return False


def normalize_code(lobby_id: str) -> str:
    return lobby_id.strip().upper()


class LobbyRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- Hilfen ---------------------------------------------------------------
    @staticmethod
    async def _lock(conn: asyncpg.Connection, lobby_id: str) -> asyncpg.Record:
        row = await conn.fetchrow(
            "SELECT * FROM lobbies WHERE lobby_id = $1 FOR UPDATE", lobby_id)
        if row is None:
            raise LobbyError(404, f"Lobby {lobby_id} gibt es nicht.")
        return row

    @staticmethod
    async def _players(conn: asyncpg.Connection, lobby_id: str) -> list[asyncpg.Record]:
        return await conn.fetch(
            "SELECT player_id, name, ready FROM lobby_players "
            "WHERE lobby_id = $1 ORDER BY joined_at, player_id", lobby_id)

    async def _load(self, conn: asyncpg.Connection, lobby_id: str) -> dict[str, Any]:
        row = await conn.fetchrow("SELECT * FROM lobbies WHERE lobby_id = $1", lobby_id)
        if row is None:
            raise LobbyError(404, f"Lobby {lobby_id} gibt es nicht.")
        return {
            "lobby_id": row["lobby_id"],
            "host_id": row["host_id"],
            "status": row["status"],
            "game_id": row["game_id"],
            "cluster": row["cluster"],
            "players": [dict(p) for p in await self._players(conn, lobby_id)],
        }

    @staticmethod
    def _ticket(row: asyncpg.Record, players: list[asyncpg.Record]) -> StartTicket:
        return StartTicket(
            lobby_id=row["lobby_id"], game_id=row["game_id"], cluster=row["cluster"],
            host_id=row["host_id"],
            players=[(p["player_id"], p["name"]) for p in players])

    # --- Lesen ----------------------------------------------------------------
    async def get(self, lobby_id: str) -> dict[str, Any]:
        async with self.db.transaction() as conn:
            return await self._load(conn, normalize_code(lobby_id))

    # --- Ändern (jeweils eine Transaktion) -------------------------------------
    async def create(self, player_id: str, name: str) -> dict[str, Any]:
        for _ in range(20):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            async with self.db.transaction() as conn:
                inserted = await conn.fetchval(
                    "INSERT INTO lobbies (lobby_id, host_id) VALUES ($1, $2) "
                    "ON CONFLICT DO NOTHING RETURNING lobby_id", code, player_id)
                if inserted is None:
                    continue  # Kollision: neu würfeln
                await conn.execute(
                    "INSERT INTO lobby_players (lobby_id, player_id, name) "
                    "VALUES ($1, $2, $3)", code, player_id, name)
                return await self._load(conn, code)
        raise LobbyError(503, "Kein freier Lobby-Code gefunden.")

    async def join(self, lobby_id: str, player_id: str, name: str) -> dict[str, Any]:
        lobby_id = normalize_code(lobby_id)
        async with self.db.transaction() as conn:
            row = await self._lock(conn, lobby_id)
            players = await self._players(conn, lobby_id)
            if any(p["player_id"] == player_id for p in players):
                return await self._load(conn, lobby_id)  # erneuter Beitritt: harmlos
            if row["status"] != "waiting":
                raise LobbyError(409, "In dieser Lobby läuft bereits ein Spiel.")
            if len(players) >= MAX_PLAYERS:
                raise LobbyError(409, f"Die Lobby ist voll ({MAX_PLAYERS} Spieler).")
            await conn.execute(
                "INSERT INTO lobby_players (lobby_id, player_id, name) VALUES ($1, $2, $3)",
                lobby_id, player_id, name)
            return await self._load(conn, lobby_id)

    async def leave(self, lobby_id: str, player_id: str) -> Optional[dict[str, Any]]:
        """Liefert die Lobby danach oder None, wenn sie leer war und gelöscht wurde."""
        lobby_id = normalize_code(lobby_id)
        async with self.db.transaction() as conn:
            row = await self._lock(conn, lobby_id)
            if row["status"] in ("starting", "started"):
                raise LobbyError(409, "Das Spiel hat bereits begonnen.")
            await conn.execute(
                "DELETE FROM lobby_players WHERE lobby_id = $1 AND player_id = $2",
                lobby_id, player_id)
            remaining = await self._players(conn, lobby_id)
            if not remaining:
                await conn.execute("DELETE FROM lobbies WHERE lobby_id = $1", lobby_id)
                return None
            if row["host_id"] == player_id:
                await conn.execute("UPDATE lobbies SET host_id = $2 WHERE lobby_id = $1",
                                   lobby_id, remaining[0]["player_id"])
            return await self._load(conn, lobby_id)

    async def set_ready(self, lobby_id: str, player_id: str, ready: bool,
                        clusters: list[str]) -> tuple[dict[str, Any], Optional[StartTicket]]:
        """Ready-Status setzen. Sind danach alle bereit, wechselt die Lobby auf
        `starting`, und nur dieser eine Aufruf bekommt ein StartTicket."""
        lobby_id = normalize_code(lobby_id)
        async with self.db.transaction() as conn:
            row = await self._lock(conn, lobby_id)
            players = await self._players(conn, lobby_id)
            if not any(p["player_id"] == player_id for p in players):
                raise LobbyError(403, "Du bist nicht in dieser Lobby.")
            if row["status"] != "waiting":
                raise LobbyError(409, "Das Spiel startet bereits.")
            await conn.execute(
                "UPDATE lobby_players SET ready = $3 WHERE lobby_id = $1 AND player_id = $2",
                lobby_id, player_id, ready)
            ticket = None
            all_ready = all(p["ready"] or p["player_id"] == player_id for p in players)
            if ready and len(players) >= MIN_PLAYERS and all_ready:
                if not clusters:
                    raise LobbyError(503, "Kein Game-Service-Cluster konfiguriert.")
                rr = await conn.fetchval("SELECT nextval('cluster_rr')")
                cluster = clusters[(rr - 1) % len(clusters)]
                game_id = f"{lobby_id}-{secrets.token_hex(3)}"
                row = await conn.fetchrow(
                    "UPDATE lobbies SET status = 'starting', game_id = $2, cluster = $3, "
                    "status_changed_at = now() WHERE lobby_id = $1 RETURNING *",
                    lobby_id, game_id, cluster)
                ticket = self._ticket(row, players)
            return await self._load(conn, lobby_id), ticket

    async def claim_stale_start(self, lobby_id: str,
                                stale_after: float = STALE_START_SECONDS
                                ) -> Optional[StartTicket]:
        """Hängt die Lobby zu lange in `starting` (Instanz abgestürzt), darf
        der Aufrufer den Start übernehmen. Nur ein Aufrufer bekommt das Ticket."""
        lobby_id = normalize_code(lobby_id)
        async with self.db.transaction() as conn:
            row = await self._lock(conn, lobby_id)
            if row["status"] != "starting":
                return None
            stale = await conn.fetchval(
                "SELECT $1::timestamptz < now() - make_interval(secs => $2)",
                row["status_changed_at"], stale_after)
            if not stale:
                return None
            row = await conn.fetchrow(
                "UPDATE lobbies SET status_changed_at = now() WHERE lobby_id = $1 "
                "RETURNING *", lobby_id)
            return self._ticket(row, await self._players(conn, lobby_id))

    async def mark_started(self, lobby_id: str, game_id: str) -> dict[str, Any]:
        lobby_id = normalize_code(lobby_id)
        async with self.db.transaction() as conn:
            await self._lock(conn, lobby_id)
            await conn.execute(
                "UPDATE lobbies SET status = 'started', status_changed_at = now() "
                "WHERE lobby_id = $1 AND game_id = $2 AND status = 'starting'",
                lobby_id, game_id)
            return await self._load(conn, lobby_id)

    async def abort_start(self, lobby_id: str, game_id: str) -> dict[str, Any]:
        """Start fehlgeschlagen: zurück auf `waiting`, alle müssen sich neu
        bereit melden."""
        lobby_id = normalize_code(lobby_id)
        async with self.db.transaction() as conn:
            await self._lock(conn, lobby_id)
            updated = await conn.fetchval(
                "UPDATE lobbies SET status = 'waiting', game_id = NULL, cluster = NULL, "
                "status_changed_at = now() "
                "WHERE lobby_id = $1 AND game_id = $2 AND status = 'starting' "
                "RETURNING lobby_id", lobby_id, game_id)
            if updated:
                await conn.execute(
                    "UPDATE lobby_players SET ready = FALSE WHERE lobby_id = $1", lobby_id)
            return await self._load(conn, lobby_id)


class ResultRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def save(self, game_id: str, results: list[dict[str, Any]],
                   knocker: Optional[str] = None) -> bool:
        """Speichert eine beendete Partie. `results`: je Spieler
        {"player_id", "name", "score", "winner"}. Liefert False, wenn die
        Partie schon gespeichert war (z. B. nach einem Leader-Wechsel)."""
        async with self.db.transaction() as conn:
            inserted = await conn.fetchval(
                "INSERT INTO games (game_id, lobby_id, knocker) VALUES "
                "($1, (SELECT lobby_id FROM lobbies WHERE game_id = $1), $2) "
                "ON CONFLICT DO NOTHING RETURNING game_id", game_id, knocker)
            if inserted is None:
                return False
            await conn.executemany(
                "INSERT INTO game_results (game_id, player_id, name, score, winner) "
                "VALUES ($1, $2, $3, $4, $5)",
                [(game_id, r["player_id"], r["name"], int(r["score"]), bool(r["winner"]))
                 for r in results])
            await conn.execute(
                "UPDATE lobbies SET status = 'finished', status_changed_at = now() "
                "WHERE game_id = $1", game_id)
            return True

    async def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """Letzte Partien mit Ergebnissen, neueste zuerst (für die Admin-Konsole)."""
        async with self.db.transaction() as conn:
            games = await conn.fetch(
                "SELECT game_id, lobby_id, knocker, finished_at FROM games "
                "ORDER BY finished_at DESC LIMIT $1", limit)
            rows = await conn.fetch(
                "SELECT game_id, player_id, name, score, winner FROM game_results "
                "WHERE game_id = ANY($1::text[]) ORDER BY score, name",
                [g["game_id"] for g in games])
        results: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            results.setdefault(r["game_id"], []).append(
                {k: r[k] for k in ("player_id", "name", "score", "winner")})
        return [{**dict(g), "finished_at": g["finished_at"].isoformat(),
                 "results": results.get(g["game_id"], [])} for g in games]
