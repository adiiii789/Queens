"""Ende-zu-Ende-Test des Gesamtsystems (Federführung P3, Testfälle von allen).

Alle Dienste laufen als echte Prozesse auf eigenen Ports (18xxx): drei
Raft-Knoten mit verkürzten Zeiten (Wahl 0,5–1 s, Heartbeat 0,1 s), zwei
Lobby-Instanzen gegen die Testdatenbank und das Gateway. Zwei
WebSocket-Clients spielen eine Partie, während Lobby-Instanz und Raft-Leader
ausfallen.

Wird übersprungen, wenn keine PostgreSQL-Testdatenbank erreichbar ist
(siehe tests/conftest.py und README „PostgreSQL einrichten“).
"""

from __future__ import annotations

import asyncio
import warnings
from importlib.util import find_spec

import httpx

from run_local import Service
from test_gateway import by_turn, finish_turn, start_game

GATEWAY_PORT, LOBBY_BASE, RAFT_BASE = 18000, 18000, 18100
NODES = ("1", "2", "3")
TIMEOUT = 20  # großzügig: Leader-Wahl plus Wiederholung im ClusterClient
HAS_RESULT_WRITER = find_spec("services.raft.results") is not None  # P4 Teil 2


def card_value(card: str) -> int:
    rank = card[:-1]
    return {"Q": -1, "A": 1, "J": 10, "K": 10}.get(rank) or int(rank)


class E2ESystem:
    def __init__(self, dsn: str, log_dir) -> None:
        self.raft_urls = {n: f"http://127.0.0.1:{RAFT_BASE + int(n)}" for n in NODES}
        nodes_spec = ",".join(f"{n}={url}" for n, url in self.raft_urls.items())
        cluster_spec = "a=" + "|".join(self.raft_urls.values())
        self.services: dict[str, Service] = {}
        for n, url in self.raft_urls.items():
            self.services[n] = Service(n, "raft", url, [
                "-m", "services.raft.server", "--id", n, "--nodes", nodes_spec,
                "--election-min", "0.5", "--election-max", "1.0", "--heartbeat", "0.1",
                "--data-dir", str(log_dir / "raft-state"),
                *(["--db", dsn] if HAS_RESULT_WRITER else []),
            ], log_dir / f"raft-{n}.log")
        lobby_urls = []
        for name in ("l1", "l2"):
            port = LOBBY_BASE + int(name[1:])
            lobby_urls.append(f"http://127.0.0.1:{port}")
            self.services[name] = Service(name, "lobby", lobby_urls[-1], [
                "-m", "services.lobby.app", "--id", name, "--port", str(port),
                "--db", dsn, "--clusters", cluster_spec,
            ], log_dir / f"lobby-{name}.log")
        self.gateway_url = f"http://127.0.0.1:{GATEWAY_PORT}"
        self.services["gateway"] = Service("gateway", "gateway", self.gateway_url, [
            "-m", "services.gateway.app", "--port", str(GATEWAY_PORT),
            "--lobby", ",".join(lobby_urls),
            "--turn-timeout", "120", "--afk-timeout", "120", "--peek-timeout", "120",
        ], log_dir / "gateway.log")
        self.ws_url = f"ws://127.0.0.1:{GATEWAY_PORT}/ws"
        self.http = httpx.AsyncClient(timeout=1.0)

    async def start(self) -> None:
        for service in self.services.values():
            service.start()
        await self.leader()
        for service in self.services.values():
            path = "/status" if service.kind == "raft" else "/health"
            await self.until(lambda s=service, p=path: self.get(s.url + p))

    async def stop(self) -> None:
        for service in self.services.values():
            service.kill()
        await self.http.aclose()

    def logs(self) -> str:
        parts = []
        for service in self.services.values():
            try:
                tail = service.log_file.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue
            parts.append(f"--- {service.name} ---\n" + "\n".join(tail.splitlines()[-15:]))
        return "\n".join(parts)

    async def get(self, url: str):
        try:
            response = await self.http.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            return None

    async def until(self, condition, timeout: float = TIMEOUT):
        async def poll():
            while True:
                result = condition()
                if asyncio.iscoroutine(result):
                    result = await result
                if result:
                    return result
                await asyncio.sleep(0.1)
        return await asyncio.wait_for(poll(), timeout)

    async def statuses(self) -> dict[str, dict]:
        result = {}
        for n, url in self.raft_urls.items():
            if self.services[n].running:
                state = await self.get(url + "/status")
                if state is not None:
                    result[n] = state
        return result

    async def leader(self) -> dict:
        async def settled():
            states = list((await self.statuses()).values())
            leaders = [s for s in states if s["role"] == "leader"]
            if len(leaders) == 1 and all(s["leader_id"] == leaders[0]["node_id"]
                                         for s in states):
                return leaders[0]
            return None
        return await self.until(settled)


def test_end_to_end(db_dsn, tmp_path):
    async def scenario():
        system = E2ESystem(db_dsn, tmp_path)
        try:
            await system.start()
            await run(system, db_dsn)
        except BaseException:
            print(system.logs())  # bei Fehlern: Ende aller Logs im pytest-Bericht
            raise
        finally:
            await system.stop()

    asyncio.run(scenario())


async def run(system: E2ESystem, dsn: str) -> None:
    # 1. Lobby-Instanz l1 fällt aus; alles läuft über l2 (P2).
    system.services["l1"].kill()

    # 2. Lobby, Beitritt, beide bereit, Startkarten ansehen (P1).
    anna, ben = await start_game(system, "Anna", "Ben")
    for client in (anna, ben):
        peeked = [e for e in client.events() if e["type"] == "CardsPeeked"]
        assert len(peeked) == 1 and peeked[0]["audience"] == client.player_id
        assert all(e["audience"] in (None, client.player_id) for e in client.events())
    first, second = by_turn([anna, ben])

    # 3. Ungültige Aktion: Fehler nur beim Absender (P1, P2).
    await second.act(type="DrawFromDeck")
    await second.expect("error")
    assert first.errors() == []

    # 4. Ein Zug, dann den Raft-Leader beenden; der nächste Zug klappt (P3, P4).
    await finish_turn(first)
    old = await system.leader()
    system.services[old["node_id"]].kill()
    view = await finish_turn_slow(second)
    assert view["active_player"] == first.player_id
    new = await system.leader()
    assert new["node_id"] != old["node_id"] and new["term"] > old["term"]

    # 5. Reconnect mit gleicher ID bringt die aktuelle Sicht zurück (P2).
    await first.close()
    welcome = await first.connect()
    assert welcome["reconnected"]
    await first.expect("game_started", timeout=TIMEOUT)
    state = await first.expect_state(lambda v: True, timeout=TIMEOUT)
    assert state["version"] == second.view["version"]

    # 6. Klopfen, letzte Runde, Punkte prüfen (P1).
    await first.act(type="Knock")
    final = await finish_turn_slow(second)
    assert final["phase"] == "finished" and final["knocker"] == first.player_id
    result = final["result"]
    for player_id, hand in result["hands"].items():
        assert result["scores"][player_id] == sum(card_value(c) for c in hand if c)
    best = min(result["scores"].values())
    assert set(result["winners"]) == {p for p, s in result["scores"].items() if s == best}

    # 7. Ausgefallenen Knoten neu starten: Er holt das Log auf (P4).
    system.services[old["node_id"]].start()
    leader = await system.leader()

    async def caught_up():
        state = (await system.statuses()).get(old["node_id"])
        return state and state["commit_index"] >= leader["commit_index"] \
            and state["games"] == leader["games"] >= 1
    await system.until(caught_up)

    # 8. Ergebnis steht genau einmal in PostgreSQL, Lobby ist `finished` (P2, P4).
    if not HAS_RESULT_WRITER:
        warnings.warn("Schritt 8 übersprungen: services/raft/results.py "
                      "(feature/Raft_Log&Consistency Teil 2) fehlt noch")
        return
    import asyncpg

    async def stored():
        connection = await asyncpg.connect(dsn)
        try:
            games = await connection.fetchval(
                "SELECT count(*) FROM games WHERE game_id = $1", final["game_id"])
            rows = await connection.fetchval(
                "SELECT count(*) FROM game_results WHERE game_id = $1", final["game_id"])
            status = await connection.fetchval(
                "SELECT status FROM lobbies WHERE game_id = $1", final["game_id"])
        finally:
            await connection.close()
        return (games, rows, status) == (1, 2, "finished")
    await system.until(stored)


async def finish_turn_slow(client):
    """Warten, bis `client` am Zug ist (ältere Sichten in der Queue werden
    dabei verbraucht), dann finish_turn aus test_gateway – mit Geduld für
    eine Leader-Wahl."""
    await client.expect_state(lambda v: v["active_player"] == client.player_id
                              and v["phase"] == "await_draw", timeout=TIMEOUT)
    return await asyncio.wait_for(finish_turn(client), TIMEOUT * 2)
