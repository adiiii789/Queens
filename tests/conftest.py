"""Gemeinsame Fixtures. Die Datenbank-Tests laufen gegen eine echte
PostgreSQL-Testdatenbank (siehe README, „PostgreSQL einrichten“) und werden
übersprungen, wenn keine erreichbar ist.

Adresse: Umgebungsvariable QUEENS_TEST_DB, Standard
  postgresql://queens:queens@127.0.0.1:5432/queens_test
"""

import asyncio
import os

import pytest

TEST_DSN = os.environ.get(
    "QUEENS_TEST_DB", "postgresql://queens:queens@127.0.0.1:5432/queens_test")


async def _reset(dsn: str) -> None:
    import asyncpg
    conn = await asyncpg.connect(dsn, timeout=2)
    try:
        await conn.execute(
            "DROP TABLE IF EXISTS game_results, games, lobby_players, lobbies CASCADE;"
            "DROP SEQUENCE IF EXISTS cluster_rr;")
    finally:
        await conn.close()


@pytest.fixture
def db_dsn():
    """Leere Testdatenbank; Schema legt der Code selbst an."""
    try:
        asyncio.run(_reset(TEST_DSN))
    except ImportError:
        pytest.skip("asyncpg nicht installiert")
    except Exception as e:  # nicht erreichbar, falsches Passwort, …
        pytest.skip(f"Keine Testdatenbank erreichbar ({type(e).__name__})")
    return TEST_DSN
