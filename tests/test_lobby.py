"""Tests für den Lobby-Service: zwei Instanzen gegen dieselbe Datenbank,
Game-Service-Cluster als StubClusterClient."""

import asyncio

import httpx

from services.cluster_stub import StubClusterClient
from services.db import Database, LobbyRepository
from services.lobby.app import create_app

CLUSTERS = {"a": ["http://stub-a"]}


def run_lobbies(dsn, scenario, stale_start=15.0):
    """Startet die Instanzen l1 und l2 (ASGI, ohne Netzwerk) mit einem
    gemeinsamen Stub-Cluster und ruft scenario(l1, l2, stub, dbs) auf."""
    async def main():
        stub = StubClusterClient()
        dbs = [Database(dsn), Database(dsn)]
        clients = []
        for name, db in zip(("l1", "l2"), dbs):
            await db.connect()
            app = create_app(name, db, CLUSTERS, factory=lambda urls: stub,
                             stale_start=stale_start)
            clients.append(httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                             base_url=f"http://{name}"))
        try:
            return await scenario(*clients, stub, dbs)
        finally:
            for c in clients:
                await c.aclose()
            for db in dbs:
                await db.close()
    return asyncio.run(main())


def started_games(stub):
    return [c for c in stub.commands
            if c["type"] == "Action" and c["action"]["type"] == "StartGame"]


def test_two_instances_serve_same_lobby(db_dsn):
    async def scenario(l1, l2, stub, dbs):
        r = await l1.post("/lobbies", json={"player_id": "p1", "name": "Anna"})
        assert r.status_code == 200 and r.json()["served_by"] == "l1"
        code = r.json()["lobby_id"]
        r = await l2.post(f"/lobbies/{code}/join", json={"player_id": "p2", "name": "Ben"})
        assert r.json()["served_by"] == "l2"
        a, b = (await l1.get(f"/lobbies/{code}")).json(), (await l2.get(f"/lobbies/{code}")).json()
        a.pop("served_by"), b.pop("served_by")
        assert a == b and [p["name"] for p in a["players"]] == ["Anna", "Ben"]

        r = await l2.get("/lobbies/ZZZZ")
        assert r.status_code == 404 and "ZZZZ" in r.json()["detail"]
        r = await l1.post(f"/lobbies/{code}/ready", json={"player_id": "fremd"})
        assert r.status_code == 403
        r = await l1.post("/lobbies", json={"player_id": "p3", "name": ""})
        assert r.status_code == 422

        r = await l1.post(f"/lobbies/{code}/leave", json={"player_id": "p1"})
        assert r.json()["host_id"] == "p2"
        r = await l2.post(f"/lobbies/{code}/leave", json={"player_id": "p2"})
        assert r.json()["status"] == "deleted"
        assert (await l1.get("/health")).json() == {"instance": "l1", "db": True}
    run_lobbies(db_dsn, scenario)


def test_concurrent_ready_starts_game_once(db_dsn):
    async def scenario(l1, l2, stub, dbs):
        for _ in range(5):
            code = (await l1.post("/lobbies", json={"player_id": "p1", "name": "Anna"})).json()["lobby_id"]
            await l2.post(f"/lobbies/{code}/join", json={"player_id": "p2", "name": "Ben"})
            r1, r2 = await asyncio.gather(
                l1.post(f"/lobbies/{code}/ready", json={"player_id": "p1"}),
                l2.post(f"/lobbies/{code}/ready", json={"player_id": "p2"}))
            assert r1.status_code == r2.status_code == 200
            lobby = (await l1.get(f"/lobbies/{code}")).json()
            assert lobby["status"] == "started"
            assert lobby["cluster"] == "a" and lobby["cluster_nodes"] == CLUSTERS["a"]
            assert stub.games[lobby["game_id"]].phase.value == "initial_peek"
        assert len(started_games(stub)) == 5
    run_lobbies(db_dsn, scenario)


def test_unreachable_cluster_reopens_lobby(db_dsn):
    async def scenario(l1, l2, stub, dbs):
        code = (await l1.post("/lobbies", json={"player_id": "p1", "name": "Anna"})).json()["lobby_id"]
        await l1.post(f"/lobbies/{code}/join", json={"player_id": "p2", "name": "Ben"})
        await l1.post(f"/lobbies/{code}/ready", json={"player_id": "p1"})
        stub.available = False
        r = await l2.post(f"/lobbies/{code}/ready", json={"player_id": "p2"})
        assert r.status_code == 503 and "erneut bereit" in r.json()["detail"]
        lobby = (await l1.get(f"/lobbies/{code}")).json()
        assert lobby["status"] == "waiting" and not any(p["ready"] for p in lobby["players"])

        stub.available = True
        await l1.post(f"/lobbies/{code}/ready", json={"player_id": "p1"})
        r = await l2.post(f"/lobbies/{code}/ready", json={"player_id": "p2"})
        assert r.json()["status"] == "started"
    run_lobbies(db_dsn, scenario)


def test_other_instance_takes_over_stale_start(db_dsn):
    async def scenario(l1, l2, stub, dbs):
        code = (await l1.post("/lobbies", json={"player_id": "p1", "name": "Anna"})).json()["lobby_id"]
        await l1.post(f"/lobbies/{code}/join", json={"player_id": "p2", "name": "Ben"})
        # Instanz bekommt das Ticket und stürzt ab, bevor sie startet
        repo = LobbyRepository(dbs[0])
        await repo.set_ready(code, "p1", True, ["a"])
        _, ticket = await repo.set_ready(code, "p2", True, ["a"])
        assert ticket is not None

        assert (await l2.get(f"/lobbies/{code}")).json()["status"] == "starting"
        await asyncio.sleep(0.6)
        lobby = (await l2.get(f"/lobbies/{code}")).json()
        assert lobby["status"] == "started" and lobby["game_id"] == ticket.game_id
        assert ticket.game_id in stub.games
    run_lobbies(db_dsn, scenario, stale_start=0.5)
