"""Tests für das Gateway: echte HTTP-/WebSocket-Server (uvicorn im selben
Event-Loop), zwei Lobby-Instanzen gegen die Testdatenbank, Game-Service als
gemeinsamer StubClusterClient."""

import asyncio
import json
import socket

import uvicorn
import websockets

from services.cluster_stub import StubClusterClient
from services.db import Database
from services.gateway.app import create_app as create_gateway
from services.lobby.app import create_app as create_lobby


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, app):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)

    async def stop(self):
        if self.task and not self.task.done():
            self.server.should_exit = True
            await self.task


class System:
    def __init__(self, dsn, **gateway_options):
        self.stub = StubClusterClient()
        self.lobbies = [Server(create_lobby(name, Database(dsn), {"a": ["http://stub"]},
                                            factory=lambda urls: self.stub))
                        for name in ("l1", "l2")]
        self.gateway = Server(create_gateway([l.url for l in self.lobbies],
                                             factory=lambda urls: self.stub,
                                             **gateway_options))
        self.ws_url = self.gateway.url.replace("http", "ws") + "/ws"

    async def __aenter__(self):
        for s in (*self.lobbies, self.gateway):
            await s.start()
        return self

    async def __aexit__(self, *exc):
        for s in (self.gateway, *self.lobbies):
            await s.stop()


class WsClient:
    def __init__(self, system, name):
        self.url = system.ws_url
        self.name = name
        self.player_id = None
        self.view = None
        self.received = []
        self.messages = asyncio.Queue()

    async def connect(self):
        self.ws = await websockets.connect(self.url)
        self.messages = asyncio.Queue()
        await self.send(op="hello", name=self.name, player_id=self.player_id)
        self.reader = asyncio.create_task(self._read())
        welcome = await self.expect("welcome")
        self.player_id = welcome["player_id"]
        return welcome

    async def _read(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if msg["op"] == "state":
                    self.view = msg["view"]
                self.received.append(msg)
                await self.messages.put(msg)
        except websockets.WebSocketException:
            pass

    async def close(self):
        await self.ws.close()
        self.reader.cancel()

    async def send(self, **msg):
        await self.ws.send(json.dumps(msg))

    async def act(self, **action):
        await self.send(op="action", action=action)

    async def expect(self, op, check=lambda m: True, timeout=10):
        async def find():
            while True:
                msg = await self.messages.get()
                if msg["op"] == "error" and op != "error":
                    raise AssertionError(f"{self.name}: Fehler {msg['message']}")
                if msg["op"] == op and check(msg):
                    return msg
        return await asyncio.wait_for(find(), timeout)

    async def expect_state(self, check, timeout=10):
        return (await self.expect("state", lambda m: check(m["view"]), timeout))["view"]

    def events(self):
        return [e for m in self.received if m["op"] == "events" for e in m["events"]]

    def errors(self):
        return [m for m in self.received if m["op"] == "error"]


async def start_game(system, *names):
    """Lobby anlegen, alle beitreten und bereit melden, Startkarten ansehen."""
    clients = [WsClient(system, n) for n in names]
    for c in clients:
        await c.connect()
    host = clients[0]
    await host.send(op="create_lobby")
    code = (await host.expect("lobby"))["lobby"]["lobby_id"]
    for c in clients[1:]:
        await c.send(op="join_lobby", lobby_id=code.lower())
        await c.expect("lobby", lambda m: any(p["player_id"] == c.player_id
                                              for p in m["lobby"]["players"]))
    for c in clients:
        await c.send(op="ready", ready=True)
    for c in clients:
        started = await c.expect("game_started")
        assert set(started["names"].values()) == set(names)
        await c.expect_state(lambda v: v["phase"] == "initial_peek")
    for c in clients:
        await c.act(type="PeekInitial", slots=[0, 1])
    for c in clients:
        await c.expect_state(lambda v: v["phase"] == "await_draw")
    return clients


async def finish_turn(client):
    """Ziehen, ablegen, ggf. Sonderaktion überspringen."""
    turn = client.view["turn_number"]
    await client.act(type="DrawFromDeck")
    await client.expect_state(lambda v: v["phase"] == "await_decision")
    await client.act(type="DiscardDrawn")
    view = await client.expect_state(lambda v: v["phase"] != "await_decision")
    if view["phase"] == "await_special":
        await client.act(type="SkipSpecial")
        view = await client.expect_state(
            lambda v: v["turn_number"] > turn or v["phase"] == "finished")
    return view


def by_turn(clients):
    active = clients[0].view["active_player"]
    first = next(c for c in clients if c.player_id == active)
    return first, next(c for c in clients if c is not first)


def test_full_game_over_gateway(db_dsn):
    async def scenario():
        async with System(db_dsn) as system:
            anna, ben = await start_game(system, "Anna", "Ben")
            first, second = by_turn([anna, ben])

            # Abgelehnte Aktion: Fehler nur beim Absender
            await second.act(type="DrawFromDeck")
            await second.expect("error")
            # player_id kann nicht gefälscht werden
            await second.act(type="DrawFromDeck", player_id=first.player_id)
            await second.expect("error")
            assert first.errors() == []

            await finish_turn(first)
            await second.act(type="Knock")
            await first.expect_state(lambda v: v["knocker"] == second.player_id)
            assert (await finish_turn(first))["phase"] == "finished"
            final = await second.expect_state(lambda v: v["phase"] == "finished")
            assert set(final["result"]["scores"]) == {anna.player_id, ben.player_id}

            # Kein Client sieht private Ereignisse oder gezogene Karten anderer
            for c in (anna, ben):
                assert c.events(), "keine Ereignisse empfangen"
                assert all(e["audience"] in (None, c.player_id) for e in c.events())
                for m in c.received:
                    if m["op"] == "state" and m["view"]["drawn_card"]:
                        assert m["view"]["active_player"] == c.player_id

            # Nach Spielende ist eine neue Lobby möglich
            await anna.send(op="create_lobby")
            assert (await anna.expect("lobby"))["lobby"]["status"] == "waiting"
            for c in (anna, ben):
                await c.close()
    asyncio.run(scenario())


def test_lobby_instance_failure_is_invisible(db_dsn):
    async def scenario():
        async with System(db_dsn) as system:
            await system.lobbies[0].stop()
            anna, ben, cem = await start_game(system, "Anna", "Ben", "Cem")
            assert all(c.errors() == [] for c in (anna, ben, cem))
            for c in (anna, ben, cem):
                await c.close()
    asyncio.run(scenario())


def test_reconnect_and_afk_skip(db_dsn):
    async def scenario():
        async with System(db_dsn, afk_timeout=0.5, turn_timeout=30) as system:
            anna, ben = await start_game(system, "Anna", "Ben")
            first, second = by_turn([anna, ben])
            turn = second.view["turn_number"]

            # Aktiver Spieler trennt sich: nach dem AFK-Timeout ist der andere dran
            await first.close()
            await second.expect("info", lambda m: "Verbindung verloren" in m["message"])
            view = await second.expect_state(
                lambda v: v["active_player"] == second.player_id, timeout=5)
            assert view["turn_number"] == turn + 1
            assert any(e["type"] == "TurnTimedOut" for e in second.events())

            # Reconnect mit gleicher ID bringt die aktuelle Sicht zurück
            welcome = await first.connect()
            assert welcome["reconnected"]
            await first.expect("game_started")
            state = await first.expect_state(lambda v: True)
            assert state["version"] == second.view["version"]
            await second.expect("info", lambda m: "wieder da" in m["message"])

            # Weiterspielen klappt
            await finish_turn(second)
            assert first.view is not None
            await first.expect_state(lambda v: v["active_player"] == first.player_id)
            for c in (anna, ben):
                await c.close()
    asyncio.run(scenario())


def test_turn_timeout_for_connected_player(db_dsn):
    async def scenario():
        async with System(db_dsn, turn_timeout=0.7) as system:
            anna, ben = await start_game(system, "Anna", "Ben")
            first, second = by_turn([anna, ben])
            state = await first.expect("state", lambda m: m["view"]["active_player"]
                                       == second.player_id, timeout=5)
            assert state["timeout"] is not None and state["timeout"] <= 0.7
            for c in (anna, ben):
                await c.close()
    asyncio.run(scenario())


def test_hello_required(db_dsn):
    async def scenario():
        async with System(db_dsn) as system:
            async with websockets.connect(system.ws_url) as ws:
                await ws.send(json.dumps({"op": "create_lobby"}))
                assert json.loads(await ws.recv())["op"] == "error"
    asyncio.run(scenario())
