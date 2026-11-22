"""Tests für den Terminal-Client (client/cli.py).

Der Ablauf-Test spielt eine ganze Partie mit zwei TerminalClients über das
echte Gateway. Statt Lobby-Service und PostgreSQL steht dahinter eine kleine
Lobby im Speicher mit denselben Endpunkten, dazu der StubClusterClient. So
läuft der Test auch ohne Datenbank."""

import asyncio
import json
import socket
import uuid

import pytest
import uvicorn
from fastapi import FastAPI, HTTPException

from client.cli import CommandError, TerminalClient, parse_command
from services.cluster_stub import StubClusterClient
from services.gateway.app import create_app as create_gateway

PLAYERS = ["a", "b", "c"]


# --- Befehle ------------------------------------------------------------------------
def test_lobby_commands():
    assert parse_command("neu", []) == {"op": "create_lobby"}
    assert parse_command("join abcd", []) == {"op": "join_lobby", "lobby_id": "ABCD"}
    assert parse_command("bereit", []) == {"op": "ready", "ready": True}
    assert parse_command("nicht", []) == {"op": "ready", "ready": False}
    assert parse_command("verlassen", []) == {"op": "leave_lobby"}
    assert parse_command("  ", []) is None
    assert parse_command("TISCH", []) == "tisch"


@pytest.mark.parametrize("line, expected", [
    ("p 0 2", {"type": "PeekInitial", "slots": [0, 2]}),
    ("z", {"type": "DrawFromDeck"}),
    ("a", {"type": "TakeFromDiscard"}),
    ("t 3", {"type": "SwapWithHand", "slot": 3}),
    ("w", {"type": "DiscardDrawn"}),
    ("b 1 0 3 2", {"type": "UseJack", "first": {"player_id": "a", "slot": 0},
                   "second": {"player_id": "c", "slot": 2}}),
    ("k 2 1", {"type": "UseKing", "target": {"player_id": "b", "slot": 1}}),
    ("s", {"type": "SkipSpecial"}),
    ("klopf", {"type": "Knock"}),
    ("x 1", {"type": "ThrowIn", "slot": 1}),
])
def test_game_commands_map_player_numbers_and_get_action_id(line, expected):
    message = parse_command(line, PLAYERS)
    action = dict(message["action"])
    assert message["op"] == "action"
    assert len(action.pop("action_id")) == 32
    assert action == expected


def test_each_action_gets_a_new_action_id():
    ids = {parse_command("z", PLAYERS)["action"]["action_id"] for _ in range(5)}
    assert len(ids) == 5


@pytest.mark.parametrize("line, players, text", [
    ("t", PLAYERS, "erwartet 1"),
    ("t eins", PLAYERS, "Zahl"),
    ("k 4 0", PLAYERS, "gibt es nicht"),
    ("k 1 0", [], "erst, wenn die Partie läuft"),
    ("tanzen", PLAYERS, "Unbekannter Befehl"),
])
def test_wrong_input_gives_clear_message(line, players, text):
    with pytest.raises(CommandError, match=text):
        parse_command(line, players)


# --- Ablauf über das Gateway ----------------------------------------------------------
def fake_lobby(stub: StubClusterClient) -> FastAPI:
    """Lobby-Endpunkte wie services/lobby/app.py, aber im Speicher."""
    app = FastAPI()
    lobbies: dict[str, dict] = {}

    def get(lobby_id: str) -> dict:
        if lobby_id not in lobbies:
            raise HTTPException(404, "Lobby nicht gefunden.")
        return lobbies[lobby_id]

    @app.post("/lobbies")
    async def create(body: dict):
        lobby_id = uuid.uuid4().hex[:4].upper()
        lobbies[lobby_id] = {"lobby_id": lobby_id, "host_id": body["player_id"],
                             "status": "waiting", "game_id": None, "cluster": None,
                             "cluster_nodes": [], "served_by": "fake",
                             "players": [{"player_id": body["player_id"],
                                          "name": body["name"], "ready": False}]}
        return lobbies[lobby_id]

    @app.get("/lobbies/{lobby_id}")
    async def show(lobby_id: str):
        return get(lobby_id)

    @app.post("/lobbies/{lobby_id}/join")
    async def join(lobby_id: str, body: dict):
        lobby = get(lobby_id)
        if all(p["player_id"] != body["player_id"] for p in lobby["players"]):
            lobby["players"].append({"player_id": body["player_id"],
                                     "name": body["name"], "ready": False})
        return lobby

    @app.post("/lobbies/{lobby_id}/ready")
    async def ready(lobby_id: str, body: dict):
        lobby = get(lobby_id)
        for p in lobby["players"]:
            if p["player_id"] == body["player_id"]:
                p["ready"] = body["ready"]
        if len(lobby["players"]) >= 2 and all(p["ready"] for p in lobby["players"]):
            game_id = f"{lobby_id}-g"
            players = [[p["player_id"], p["name"]] for p in lobby["players"]]
            await stub.submit({"type": "CreateGame", "game_id": game_id, "players": players})
            await stub.submit({"type": "Action", "game_id": game_id, "action": {
                "type": "StartGame", "player_id": players[0][0],
                "action_id": f"{game_id}-start", "seed": "cli-test"}})
            lobby.update(status="started", game_id=game_id, cluster="a",
                         cluster_nodes=["http://stub"])
        return lobby

    return app


class Server:
    def __init__(self, app):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning"))

    async def start(self):
        self.task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)

    async def stop(self):
        self.server.should_exit = True
        await self.task


class Player:
    """TerminalClient mit Eingabe-Queue und mitgeschriebener Ausgabe."""

    def __init__(self, ws_url, name, player_id=None):
        self.output: list[str] = []
        self.client = TerminalClient(ws_url, name, player_id, out=self.output.append)
        self.received: list[dict] = []
        handle = self.client.on_message
        self.client.on_message = lambda raw: (self.received.append(json.loads(raw)),
                                              handle(raw))
        self.lines: asyncio.Queue = asyncio.Queue()
        self.task = asyncio.create_task(self.client.run(self.lines))

    def type(self, line):
        self.lines.put_nowait(line)

    async def until(self, condition, timeout=10):
        async def wait():
            while not condition(self.client):
                await asyncio.sleep(0.02)
        await asyncio.wait_for(wait(), timeout)

    async def phase(self, *phases):
        await self.until(lambda c: c.view is not None and c.view["phase"] in phases)

    def said(self, text):
        return any(text in line for line in self.output)

    async def quit(self):
        self.type("q")
        await asyncio.wait_for(self.task, 5)


async def take_turn(player, other):
    """Ziehen, ablegen, ggf. Sonderaktion überspringen."""
    turn = player.client.view["turn_number"]
    player.type("z")
    await player.phase("await_decision")
    player.type("w")
    await player.until(lambda c: c.view["phase"] != "await_decision")
    if player.client.view["phase"] == "await_special":
        player.type("s")
    await other.until(lambda c: c.view["turn_number"] > turn
                      or c.view["phase"] == "finished")


def test_full_game_with_terminal_clients():
    async def scenario():
        stub = StubClusterClient()
        lobby = Server(fake_lobby(stub))
        await lobby.start()
        gateway = Server(create_gateway([lobby.url], factory=lambda urls: stub))
        await gateway.start()
        ws_url = gateway.url.replace("http", "ws") + "/ws"
        try:
            anna, ben = Player(ws_url, "Anna"), Player(ws_url, "Ben")
            await anna.until(lambda c: c.player_id)
            await ben.until(lambda c: c.player_id)

            anna.type("neu")
            await anna.until(lambda c: c.lobby)
            ben.type(f"join {anna.client.lobby['lobby_id'].lower()}")
            await ben.until(lambda c: c.lobby)
            anna.type("bereit")
            ben.type("bereit")
            for p in (anna, ben):
                await p.phase("initial_peek")
                p.type("p 0 1")
            for p in (anna, ben):
                await p.phase("await_draw")
            assert anna.said("🔒 deine Karten: Slot 0 =")
            assert ben.said("🔒 deine Karten: Slot 0 =")

            first = anna if anna.client.view["active_player"] == anna.client.player_id else ben
            second = ben if first is anna else anna

            # Falsche Eingaben: verständliche Meldung, kein Absturz
            second.type("t eins")
            second.type("z")  # nicht am Zug: Ablehnung kommt vom Server
            await second.until(lambda c: any("nicht am Zug" in line for line in second.output))
            assert second.said("✗ Slot muss eine Zahl sein")
            assert not first.said("nicht am Zug")

            await take_turn(first, second)

            # Reconnect: Verbindung bricht ab, Client meldet sich selbst wieder an
            await second.client.ws.close()
            await second.until(lambda c: any("Wieder verbunden" in line
                                             for line in second.output), timeout=5)

            second.type("klopf")
            await first.until(lambda c: c.view["knocker"] == second.client.player_id)
            await take_turn(first, second)
            for p in (anna, ben):
                await p.phase("finished")
                assert p.said("Auflösung:") and p.said("Punkte")

            # Neustart des Programms mit --player-id: wieder in der (beendeten) Partie
            ben_id, version = ben.client.player_id, ben.client.view["version"]
            await ben.quit()
            ben2 = Player(ws_url, "Ben", player_id=ben_id)
            await ben2.until(lambda c: c.view is not None)
            assert ben2.client.view["version"] == version
            assert ben2.said("Wieder verbunden")

            # Kein Client hat ein privates Ereignis eines anderen bekommen
            for p in (anna, ben):
                events = [e for m in p.received if m["op"] == "events" for e in m["events"]]
                assert any(e["audience"] == p.client.player_id for e in events)
                assert all(e["audience"] in (None, p.client.player_id) for e in events)
            await anna.quit()
            await ben2.quit()
        finally:
            await gateway.stop()
            await lobby.stop()
    asyncio.run(scenario())


def test_unreachable_server_retries_without_crash():
    async def scenario():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]  # danach frei: niemand lauscht
        p = Player(f"ws://127.0.0.1:{port}/ws", "Anna")
        await p.until(lambda c: any("Keine Verbindung" in line for line in p.output))
        p.type("z")
        await p.until(lambda c: any("nicht verbunden" in line for line in p.output))
        await p.quit()
    asyncio.run(scenario())
