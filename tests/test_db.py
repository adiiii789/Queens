"""Tests für services/db.py gegen eine echte PostgreSQL (siehe conftest.py).

Zwei getrennte Pools stehen für zwei Lobby-Instanzen, die gleichzeitig auf
dieselbe Datenbank zugreifen."""

import asyncio

import pytest

from services.db import (
    CODE_ALPHABET, Database, DatabaseUnavailable, LobbyError, LobbyRepository,
    ResultRepository,
)


def run_with_two_instances(dsn, scenario):
    """Führt scenario(repo_a, repo_b, results) mit zwei Pools aus."""
    async def main():
        db_a, db_b = Database(dsn), Database(dsn)
        try:
            await asyncio.gather(db_a.connect(), db_b.connect())
            return await scenario(LobbyRepository(db_a), LobbyRepository(db_b),
                                  ResultRepository(db_a))
        finally:
            await db_a.close()
            await db_b.close()
    return asyncio.run(main())


def ids(lobby):
    return [p["player_id"] for p in lobby["players"]]


def test_create_join_leave_and_host_change(db_dsn):
    async def scenario(a, b, _):
        lobby = await a.create("p1", "Anna")
        code = lobby["lobby_id"]
        assert len(code) == 4 and all(c in CODE_ALPHABET for c in code)
        assert lobby["host_id"] == "p1" and lobby["status"] == "waiting"

        await b.join(code.lower(), "p2", "Ben")  # Kleinschreibung erlaubt
        await b.join(code, "p2", "Ben")          # erneuter Beitritt ist harmlos
        lobby = await a.join(code, "p3", "Cem")
        assert ids(lobby) == ["p1", "p2", "p3"]
        assert (await b.get(code)) == lobby

        lobby = await b.leave(code, "p1")        # Host geht, nächster übernimmt
        assert lobby["host_id"] == "p2" and ids(lobby) == ["p2", "p3"]
        await a.leave(code, "p2")
        assert await a.leave(code, "p3") is None  # leere Lobby wird gelöscht
        with pytest.raises(LobbyError) as err:
            await b.get(code)
        assert err.value.status == 404
    run_with_two_instances(db_dsn, scenario)


def test_ready_rules(db_dsn):
    async def scenario(a, b, _):
        code = (await a.create("p1", "Anna"))["lobby_id"]
        lobby, ticket = await a.set_ready(code, "p1", True, ["a"])
        assert ticket is None  # allein startet kein Spiel
        with pytest.raises(LobbyError) as err:
            await a.set_ready(code, "fremd", True, ["a"])
        assert err.value.status == 403

        await b.join(code, "p2", "Ben")
        lobby, ticket = await b.set_ready(code, "p2", True, ["a"])
        assert lobby["status"] == "starting" and ticket is not None
        assert ticket.players == [("p1", "Anna"), ("p2", "Ben")]
        assert ticket.game_id.startswith(code + "-")

        with pytest.raises(LobbyError) as err:
            await a.join(code, "p3", "Cem")
        assert err.value.status == 409
        with pytest.raises(LobbyError):
            await a.leave(code, "p1")

        lobby = await a.abort_start(code, ticket.game_id)
        assert lobby["status"] == "waiting" and lobby["game_id"] is None
        assert not any(p["ready"] for p in lobby["players"])
    run_with_two_instances(db_dsn, scenario)


def test_concurrent_ready_gives_exactly_one_ticket(db_dsn):
    async def scenario(a, b, _):
        for _ in range(20):
            code = (await a.create("p1", "Anna"))["lobby_id"]
            await b.join(code, "p2", "Ben")
            results = await asyncio.gather(
                a.set_ready(code, "p1", True, ["a", "b"]),
                b.set_ready(code, "p2", True, ["a", "b"]))
            tickets = [t for _, t in results if t is not None]
            assert len(tickets) == 1
            assert (await a.get(code))["status"] == "starting"
    run_with_two_instances(db_dsn, scenario)


def test_parallel_joins_respect_limit(db_dsn):
    async def scenario(a, b, _):
        code = (await a.create("host", "Host"))["lobby_id"]

        async def try_join(i):
            repo = a if i % 2 else b
            try:
                await repo.join(code, f"p{i}", f"P{i}")
                return True
            except LobbyError as e:
                assert e.status == 409
                return False

        joined = await asyncio.gather(*(try_join(i) for i in range(14)))
        assert sum(joined) == 7
        assert len((await a.get(code))["players"]) == 8
    run_with_two_instances(db_dsn, scenario)


def test_clusters_round_robin(db_dsn):
    async def scenario(a, b, _):
        clusters = []
        for i in range(4):
            code = (await a.create("p1", "Anna"))["lobby_id"]
            await a.join(code, "p2", "Ben")
            await a.set_ready(code, "p1", True, ["a", "b"])
            _, ticket = await (a if i % 2 else b).set_ready(code, "p2", True, ["a", "b"])
            clusters.append(ticket.cluster)
        assert clusters == ["a", "b", "a", "b"]
    run_with_two_instances(db_dsn, scenario)


def test_stale_start_can_be_claimed_once(db_dsn):
    async def scenario(a, b, _):
        code = (await a.create("p1", "Anna"))["lobby_id"]
        await a.join(code, "p2", "Ben")
        await a.set_ready(code, "p1", True, ["a"])
        _, ticket = await a.set_ready(code, "p2", True, ["a"])

        assert await b.claim_stale_start(code) is None  # noch nicht abgelaufen
        await asyncio.sleep(0.6)
        claims = await asyncio.gather(b.claim_stale_start(code, stale_after=0.5),
                                      a.claim_stale_start(code, stale_after=0.5))
        claimed = [c for c in claims if c is not None]
        assert len(claimed) == 1 and claimed[0].game_id == ticket.game_id

        lobby = await b.mark_started(code, ticket.game_id)
        assert lobby["status"] == "started"
        assert await b.claim_stale_start(code, stale_after=0) is None
    run_with_two_instances(db_dsn, scenario)


def test_result_saved_exactly_once(db_dsn):
    async def scenario(a, b, results):
        code = (await a.create("p1", "Anna"))["lobby_id"]
        await a.join(code, "p2", "Ben")
        await a.set_ready(code, "p1", True, ["a"])
        _, ticket = await a.set_ready(code, "p2", True, ["a"])
        await a.mark_started(code, ticket.game_id)

        rows = [{"player_id": "p1", "name": "Anna", "score": 3, "winner": True},
                {"player_id": "p2", "name": "Ben", "score": 17, "winner": False}]
        saved = await asyncio.gather(
            results.save(ticket.game_id, rows, knocker="Anna"),
            ResultRepository(b.db).save(ticket.game_id, rows, knocker="Anna"))
        assert sorted(saved) == [False, True]
        assert await results.save(ticket.game_id, rows, knocker="Anna") is False

        recent = await results.recent()
        assert len(recent) == 1
        assert recent[0]["lobby_id"] == code and recent[0]["knocker"] == "Anna"
        assert [r["name"] for r in recent[0]["results"]] == ["Anna", "Ben"]
        assert (await a.get(code))["status"] == "finished"
    run_with_two_instances(db_dsn, scenario)


def test_unreachable_database():
    async def scenario():
        db = Database("postgresql://queens:queens@127.0.0.1:1/queens")
        assert await db.ping() is False
        with pytest.raises(DatabaseUnavailable):
            await LobbyRepository(db).create("p1", "Anna")
    asyncio.run(scenario())
