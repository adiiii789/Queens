"""Realtime-Gateway (FastAPI + WebSockets), einziger Einstiegspunkt der Clients.

Aufgaben:
  - WebSocket-Verbindungen halten, Spieler-Sitzungen mit Reconnect
  - Lobby-Befehle reihum an die Lobby-Instanzen weiterleiten (LobbyPool)
  - Spielaktionen über den ClusterClient an den Raft-Leader schicken
  - jedem Spieler nur die Ereignisse seiner `audience` und seine eigene Sicht
    senden
  - Zug-Timeouts und AFK-Schutz (TurnTimeout-Aktionen)

Start:
  python -m services.gateway.app --port 8000 --lobby http://127.0.0.1:8001,http://127.0.0.1:8002

WebSocket-Protokoll (/ws, JSON mit Feld `op`):
  Client → Gateway:
    hello        {"name", "player_id"?}            erste Nachricht, Pflicht
    create_lobby {}
    join_lobby   {"lobby_id"}
    leave_lobby  {}
    ready        {"ready": bool}
    action       {"action": {"type", "action_id"?, …}}   player_id setzt das Gateway
  Gateway → Client:
    welcome      {"player_id", "name", "reconnected"}
    lobby        {"lobby"}                          Antwort des Lobby-Service
    game_started {"game_id", "names": {player_id: name}}
    events       {"events": [{"type", "data", "audience"}]}
    state        {"view", "timeout"}                eigene Sicht + Restzeit in s
    info         {"message"}
    error        {"message"}                        nur an den Absender
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from queens_engine import SYSTEM_PLAYER

from ..cluster_stub import ClusterFactory, ClusterUnavailable, cluster_client_factory
from ..netconfig import setup_logging

log = logging.getLogger("gateway")

TURN_PHASES = ("await_draw", "await_decision", "await_special")


def events_for(player_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filtert Ereignisse auf das, was dieser Spieler sehen darf."""
    return [e for e in events if e.get("audience") in (None, player_id)]


# --- Lobby-Instanzen ------------------------------------------------------------
class LobbyUnavailable(Exception):
    pass


class LobbyPool:
    """Verteilt Anfragen reihum auf die Lobby-Instanzen. Kommt keine
    Verbindung zustande, wird die nächste Instanz genommen und die ausgefallene
    `skip_seconds` lang übersprungen. War die Anfrage schon angekommen
    (Abbruch danach), wird nur bei GET wiederholt, weil eine Änderung bereits
    ausgeführt sein könnte."""

    def __init__(self, urls: list[str], skip_seconds: float = 5.0,
                 timeout: float = 30.0) -> None:
        if not urls:
            raise ValueError("Mindestens eine Lobby-Adresse nötig")
        self.urls = [u.rstrip("/") for u in urls]
        self.skip_seconds = skip_seconds
        self.down_until: dict[str, float] = {}
        self._rr = itertools.cycle(range(len(self.urls)))
        self.http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.http.aclose()

    def _order(self) -> list[str]:
        start = next(self._rr)
        ordered = self.urls[start:] + self.urls[:start]
        now = time.monotonic()
        alive = [u for u in ordered if self.down_until.get(u, 0) <= now]
        return alive + [u for u in ordered if u not in alive]  # zur Not alle

    async def request(self, method: str, path: str,
                      body: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
        for url in self._order():
            try:
                resp = await self.http.request(method, url + path, json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                log.warning("Lobby-Instanz %s nicht erreichbar (%s)", url, type(e).__name__)
                self.down_until[url] = time.monotonic() + self.skip_seconds
                continue
            except httpx.HTTPError as e:
                if method == "GET":
                    continue
                raise LobbyUnavailable(f"Lobby-Anfrage abgebrochen ({type(e).__name__})") from e
            try:
                data = resp.json()
            except ValueError:
                data = {}
            return resp.status_code, data
        raise LobbyUnavailable("Kein Lobby-Server erreichbar.")


# --- Sitzungen und Partien --------------------------------------------------------
@dataclass
class Session:
    player_id: str
    name: str
    ws: Optional[WebSocket] = None
    lobby_id: Optional[str] = None
    game_id: Optional[str] = None
    last_version: int = -1
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Game:
    game_id: str
    lobby_id: str
    client: Any
    players: list[str]
    names: dict[str, str]
    version: int = -1
    public: Optional[dict[str, Any]] = None  # irgendeine Sicht: Phase, Zug, Aktiver
    deadline: Optional[float] = None
    timer: Optional[asyncio.Task] = None
    timer_key: Optional[tuple[str, int]] = None  # ("peek" | "turn", Zugnummer)

    @property
    def finished(self) -> bool:
        return bool(self.public and self.public["phase"] == "finished")


class Gateway:
    def __init__(self, lobby: LobbyPool, factory: ClusterFactory,
                 unavailable: tuple[type[Exception], ...],
                 turn_timeout: float = 60.0, afk_timeout: float = 15.0,
                 peek_timeout: float = 60.0) -> None:
        self.lobby = lobby
        self.factory = factory
        self.unavailable = (*unavailable, ClusterUnavailable)
        self.turn_timeout = turn_timeout
        self.afk_timeout = afk_timeout
        self.peek_timeout = peek_timeout
        self.sessions: dict[str, Session] = {}
        self.games: dict[str, Game] = {}
        self.lobbies: dict[str, dict[str, Any]] = {}     # letzte bekannte Lobby-Daten
        self.lobby_games: dict[str, str] = {}            # lobby_id -> game_id
        self.pollers: dict[str, asyncio.Task] = {}
        self.clients: dict[tuple[str, ...], Any] = {}

    async def close(self) -> None:
        for task in [*self.pollers.values(),
                     *(g.timer for g in self.games.values() if g.timer)]:
            task.cancel()
        for client in self.clients.values():
            await client.close()
        await self.lobby.close()

    def cluster(self, nodes: list[str]) -> Any:
        key = tuple(nodes)
        if key not in self.clients:
            self.clients[key] = self.factory(list(nodes))
        return self.clients[key]

    # --- Senden -------------------------------------------------------------------
    async def send(self, session: Session, message: dict[str, Any]) -> None:
        ws = session.ws
        if ws is None:
            return
        async with session.send_lock:
            try:
                await ws.send_json(message)
            except Exception:  # Verbindung weg; das merkt die Leseschleife
                pass

    async def send_to(self, player_ids: list[str], message: dict[str, Any],
                      exclude: Optional[str] = None) -> None:
        await asyncio.gather(*(self.send(self.sessions[pid], message)
                               for pid in player_ids
                               if pid in self.sessions and pid != exclude))

    async def info(self, session: Session, message: str) -> None:
        await self.send(session, {"op": "info", "message": message})

    async def error(self, session: Session, message: str) -> None:
        await self.send(session, {"op": "error", "message": message})

    def members(self, session: Session) -> list[str]:
        """Mitspieler in Partie oder Lobby (für Hinweise)."""
        game = self.games.get(session.game_id or "")
        if game and not game.finished:
            return game.players
        lobby = self.lobbies.get(session.lobby_id or "")
        return [p["player_id"] for p in lobby["players"]] if lobby else []

    # --- Verbindung ---------------------------------------------------------------
    async def handle_socket(self, ws: WebSocket) -> None:
        await ws.accept()
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            await ws.close(code=4400)
            return
        if not isinstance(hello, dict) or hello.get("op") != "hello":
            await ws.send_json({"op": "error", "message": "Erst 'hello' senden."})
            await ws.close(code=4400)
            return
        session, reconnected = await self.attach(ws, hello)
        try:
            while True:
                try:
                    message = json.loads(await ws.receive_text())
                    if not isinstance(message, dict):
                        raise ValueError
                except ValueError:
                    await self.error(session, "Ungültige Nachricht (JSON erwartet).")
                    continue
                await self.dispatch(session, message)
        except WebSocketDisconnect:
            pass
        finally:
            await self.detach(session, ws)

    async def attach(self, ws: WebSocket, hello: dict[str, Any]) -> tuple[Session, bool]:
        name = str(hello.get("name") or "").strip()[:24] or "Spieler"
        player_id = str(hello.get("player_id") or "").strip()[:64] or None
        session = self.sessions.get(player_id) if player_id else None
        reconnected = session is not None
        if session is None:
            player_id = player_id or uuid.uuid4().hex[:12]
            session = self.sessions[player_id] = Session(player_id, name)
        old, session.ws = session.ws, ws
        if old is not None:
            try:
                await old.close(code=4001)  # durch neue Verbindung ersetzt
            except Exception:
                pass
        await self.send(session, {"op": "welcome", "player_id": session.player_id,
                                  "name": session.name, "reconnected": reconnected})
        log.info("%s %s (%s)", session.name,
                 "wieder verbunden" if reconnected else "verbunden", session.player_id)
        if reconnected:
            await self.send_to(self.members(session),
                               {"op": "info", "message": f"{session.name} ist wieder da."},
                               exclude=session.player_id)
            await self.restore(session)
        return session, reconnected

    async def restore(self, session: Session) -> None:
        """Nach Reconnect: aktuelle Lobby oder Spielsicht neu schicken."""
        game = self.games.get(session.game_id or "")
        if game is not None:
            await self.send(session, {"op": "game_started", "game_id": game.game_id,
                                      "names": game.names})
            session.last_version = -1
            await self.refresh_views(game, only=session.player_id)
        elif session.lobby_id:
            await self.lobby_call(session, "GET", f"/lobbies/{session.lobby_id}")

    async def detach(self, session: Session, ws: WebSocket) -> None:
        if session.ws is not ws:
            return  # bereits durch eine neue Verbindung ersetzt
        session.ws = None
        log.info("%s getrennt", session.name)
        await self.send_to(self.members(session),
                           {"op": "info", "message": f"{session.name}: Verbindung verloren."})
        game = self.games.get(session.game_id or "")
        if (game and game.public and game.public["phase"] in TURN_PHASES
                and game.public["active_player"] == session.player_id):
            remaining = self.remaining(game)
            if remaining is None or remaining > self.afk_timeout:
                self.schedule(game, self.afk_timeout)

    # --- Nachrichten ------------------------------------------------------------
    async def dispatch(self, session: Session, message: dict[str, Any]) -> None:
        op = message.get("op")
        if op == "action":
            await self.handle_action(session, message.get("action"))
        elif op in ("create_lobby", "join_lobby"):
            await self.enter_lobby(session, op, message)
        elif op == "leave_lobby":
            await self.leave_lobby(session)
        elif op == "ready":
            if not session.lobby_id:
                await self.error(session, "Du bist in keiner Lobby.")
                return
            await self.lobby_call(session, "POST", f"/lobbies/{session.lobby_id}/ready",
                                  {"player_id": session.player_id,
                                   "ready": bool(message.get("ready", True))})
        elif op == "hello":
            await self.error(session, "Bereits angemeldet.")
        else:
            await self.error(session, f"Unbekannte Nachricht: {op!r}")

    def in_running_game(self, session: Session) -> bool:
        game = self.games.get(session.game_id or "")
        return game is not None and not game.finished

    async def enter_lobby(self, session: Session, op: str, message: dict[str, Any]) -> None:
        if self.in_running_game(session):
            await self.error(session, "Du bist gerade in einem laufenden Spiel.")
            return
        if session.game_id is not None:  # Partie beendet: alte Lobby ist erledigt
            session.lobby_id = session.game_id = None
        body = {"player_id": session.player_id, "name": session.name}
        if op == "join_lobby":
            lobby_id = str(message.get("lobby_id") or "").strip().upper()
            if not lobby_id:
                await self.error(session, "Lobby-Code fehlt.")
                return
            if lobby_id == session.lobby_id:
                await self.lobby_call(session, "GET", f"/lobbies/{lobby_id}")
                return
            path = f"/lobbies/{lobby_id}/join"
        else:
            path = "/lobbies"
        if session.lobby_id:
            await self.leave_lobby(session, quiet=True)
        lobby = await self.lobby_call(session, "POST", path, body, broadcast=False)
        if lobby is not None:
            session.lobby_id = lobby["lobby_id"]
            session.game_id = None
            session.last_version = -1
            await self.handle_lobby(lobby)

    async def leave_lobby(self, session: Session, quiet: bool = False) -> None:
        lobby_id = session.lobby_id
        if self.in_running_game(session):
            if not quiet:
                await self.error(session, "Das Spiel läuft bereits.")
            return
        if session.game_id is not None:  # Partie beendet: alte Lobby ist erledigt
            session.lobby_id = session.game_id = None
            if not quiet:
                await self.info(session, f"Lobby {lobby_id} verlassen.")
            return
        if not lobby_id:
            if not quiet:
                await self.error(session, "Du bist in keiner Lobby.")
            return
        lobby = await self.lobby_call(session, "POST", f"/lobbies/{lobby_id}/leave",
                                      {"player_id": session.player_id}, broadcast=False)
        if lobby is None:
            return
        session.lobby_id = None
        if not quiet:
            await self.info(session, f"Lobby {lobby_id} verlassen.")
        await self.handle_lobby(lobby)

    async def lobby_call(self, session: Session, method: str, path: str,
                         body: Optional[dict[str, Any]] = None,
                         broadcast: bool = True) -> Optional[dict[str, Any]]:
        """Anfrage an eine Lobby-Instanz. Fehler gehen nur an den Absender."""
        try:
            status, data = await self.lobby.request(method, path, body)
        except LobbyUnavailable as e:
            await self.error(session, str(e))
            return None
        if status != 200:
            detail = data.get("detail")
            await self.error(session, detail if isinstance(detail, str)
                             else f"Ungültige Anfrage (HTTP {status}).")
            if status == 503 and session.lobby_id and method == "POST":
                await self.refresh_lobby(session.lobby_id)  # z. B. Start abgebrochen
            return None
        if broadcast:
            await self.handle_lobby(data)
        return data

    async def refresh_lobby(self, lobby_id: str) -> Optional[dict[str, Any]]:
        try:
            status, data = await self.lobby.request("GET", f"/lobbies/{lobby_id}")
        except LobbyUnavailable:
            return None
        if status == 200:
            await self.handle_lobby(data)
            return data
        return None

    async def handle_lobby(self, lobby: dict[str, Any]) -> None:
        """Neue Lobby-Daten an alle Mitglieder verteilen; bei `started` die
        Partie einrichten."""
        lobby_id, status = lobby["lobby_id"], lobby["status"]
        if lobby_id in self.lobby_games and status not in ("started", "finished"):
            return  # veraltete Antwort, die Partie läuft schon
        if status == "started" and lobby_id in self.lobby_games:
            return
        self.lobbies[lobby_id] = lobby
        member_ids = [p["player_id"] for p in lobby["players"]]
        for pid in member_ids:
            if pid in self.sessions and not self.in_running_game(self.sessions[pid]):
                self.sessions[pid].lobby_id = lobby_id
        await self.send_to(member_ids, {"op": "lobby", "lobby": lobby})
        if status == "starting":
            self.poll_starting(lobby_id)
        elif status == "started":
            await self.setup_game(lobby)

    def poll_starting(self, lobby_id: str) -> None:
        """Hängt eine Lobby in `starting` (z. B. Instanz beim Start abgestürzt),
        regelmäßig nachfragen; der Lobby-Service übernimmt dann den Start."""
        if lobby_id in self.pollers and not self.pollers[lobby_id].done():
            return

        async def poll():
            for _ in range(60):
                await asyncio.sleep(2)
                if lobby_id in self.lobby_games:
                    return
                lobby = await self.refresh_lobby(lobby_id)
                if lobby is not None and lobby["status"] != "starting":
                    return
        self.pollers[lobby_id] = asyncio.create_task(poll())

    # --- Partie ----------------------------------------------------------------
    async def setup_game(self, lobby: dict[str, Any]) -> None:
        game_id = lobby["game_id"]
        if game_id in self.games:
            return
        players = [p["player_id"] for p in lobby["players"]]
        names = {p["player_id"]: p["name"] for p in lobby["players"]}
        if not lobby.get("cluster_nodes"):
            log.error("Lobby %s: keine Cluster-Adressen für %s", lobby["lobby_id"], game_id)
            return
        game = Game(game_id, lobby["lobby_id"], self.cluster(lobby["cluster_nodes"]),
                    players, names)
        self.games[game_id] = game
        self.lobby_games[lobby["lobby_id"]] = game_id
        for pid in players:
            session = self.sessions.get(pid)
            if session is not None:
                session.lobby_id, session.game_id, session.last_version = \
                    lobby["lobby_id"], game_id, -1
        log.info("Partie %s eingerichtet (Cluster %s, %d Spieler)",
                 game_id, lobby.get("cluster"), len(players))
        await self.send_to(players, {"op": "game_started", "game_id": game_id,
                                     "names": names})
        await self.refresh_views(game)

    async def refresh_views(self, game: Game, only: Optional[str] = None) -> None:
        try:
            result = await game.client.views(game.game_id)
        except self.unavailable:
            log.warning("Sichten für %s nicht abrufbar (Cluster nicht erreichbar)",
                        game.game_id)
            return
        if result.get("ok"):
            await self.distribute(game, [], result["views"], only=only)

    async def handle_action(self, session: Session, action: Any) -> None:
        game = self.games.get(session.game_id or "")
        if game is None:
            await self.error(session, "Du bist in keinem Spiel.")
            return
        if not isinstance(action, dict):
            await self.error(session, "Aktion fehlt.")
            return
        action = {**action, "player_id": session.player_id}  # nicht fälschbar
        if not action.get("action_id"):
            action["action_id"] = uuid.uuid4().hex
        await self.submit(game, action, sender=session)

    async def submit(self, game: Game, action: dict[str, Any],
                     sender: Optional[Session] = None) -> bool:
        command = {"type": "Action", "game_id": game.game_id, "action": action}
        try:
            result = await game.client.submit(command)
        except self.unavailable:
            if sender is not None:
                await self.error(sender, "Spielserver gerade nicht erreichbar, "
                                         "bitte erneut versuchen.")
            raise
        if not result.get("ok"):
            if sender is not None:
                await self.error(sender, result.get("message")
                                 or f"Aktion abgelehnt ({result.get('error')}).")
            return False
        await self.distribute(game, result.get("events", []), result.get("views", {}))
        return True

    async def distribute(self, game: Game, events: list[dict[str, Any]],
                         views: dict[str, dict[str, Any]],
                         only: Optional[str] = None) -> None:
        """Jedem Spieler seine Ereignisse und seine Sicht schicken. Sichten mit
        kleinerer `version` als zuletzt gesendet werden verworfen."""
        if views:
            sample = next(iter(views.values()))
            if sample.get("version", 0) > game.version:
                game.version, game.public = sample.get("version", 0), sample
                self.on_new_state(game)
        timeout = self.remaining(game)

        async def deliver(pid: str) -> None:
            session = self.sessions.get(pid)
            if session is None or session.ws is None or session.game_id != game.game_id:
                return
            visible = events_for(pid, events)
            if visible:
                await self.send(session, {"op": "events", "events": visible})
            view = views.get(pid)
            if view is not None and view.get("version", 0) >= session.last_version:
                session.last_version = view.get("version", 0)
                await self.send(session, {"op": "state", "view": view,
                                          "timeout": timeout})

        await asyncio.gather(*(deliver(pid) for pid in game.players
                               if only is None or pid == only))

    # --- Timeouts --------------------------------------------------------------
    def remaining(self, game: Game) -> Optional[float]:
        if game.deadline is None:
            return None
        return max(0.0, round(game.deadline - time.monotonic(), 1))

    def on_new_state(self, game: Game) -> None:
        """Timer nur stellen, wenn ein neuer Zug (oder die Startphase) beginnt;
        Zwischenschritte im selben Zug verlängern die Zeit nicht."""
        phase = game.public["phase"]
        turn = game.public.get("turn_number", 0)
        if phase == "finished":
            self.cancel_timer(game)
            game.deadline = None
            log.info("Partie %s beendet", game.game_id)
        elif phase == "initial_peek" and game.timer_key != ("peek", turn):
            self.schedule(game, self.peek_timeout)
        elif phase in TURN_PHASES and game.timer_key != ("turn", turn):
            active = self.sessions.get(game.public["active_player"])
            connected = active is not None and active.ws is not None
            self.schedule(game, self.turn_timeout if connected else self.afk_timeout)

    def cancel_timer(self, game: Game) -> None:
        if game.timer is not None and game.timer is not asyncio.current_task():
            game.timer.cancel()
        game.timer = None

    def schedule(self, game: Game, seconds: float) -> None:
        """Timer für den aktuellen Zug (bzw. die Startphase) neu stellen."""
        self.cancel_timer(game)
        public = game.public
        turn, phase = public.get("turn_number", 0), public["phase"]
        game.deadline = time.monotonic() + seconds
        game.timer_key = ("peek" if phase == "initial_peek" else "turn", turn)
        game.timer = asyncio.create_task(self._expire(game, seconds, turn, phase))

    async def _expire(self, game: Game, seconds: float, turn: int, phase: str) -> None:
        await asyncio.sleep(seconds)
        action_id = (f"{game.game_id}-peek-timeout" if phase == "initial_peek"
                     else f"{game.game_id}-timeout-{turn}")
        action = {"type": "TurnTimeout", "player_id": SYSTEM_PLAYER,
                  "action_id": action_id, "turn_number": turn}
        log.info("Zeit abgelaufen in %s (Zug %s)", game.game_id, turn)
        while True:
            try:
                accepted = await self.submit(game, action)
                break
            except self.unavailable:
                await asyncio.sleep(2)  # Cluster wählt gerade neu
        if not accepted:
            log.info("Timeout für %s Zug %s abgelehnt", game.game_id, turn)
            if game.timer is asyncio.current_task():
                game.deadline = game.timer = None


def create_app(lobby_urls: list[str], factory: Optional[ClusterFactory] = None,
               unavailable: tuple[type[Exception], ...] = (),
               turn_timeout: float = 60.0, afk_timeout: float = 15.0,
               peek_timeout: float = 60.0) -> FastAPI:
    if factory is None:
        factory, unavailable = cluster_client_factory()
    gateway = Gateway(LobbyPool(lobby_urls), factory, unavailable,
                      turn_timeout=turn_timeout, afk_timeout=afk_timeout,
                      peek_timeout=peek_timeout)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await gateway.close()

    app = FastAPI(title="Queens Gateway", lifespan=lifespan)
    app.state.gateway = gateway

    @app.websocket("/ws")
    async def socket(ws: WebSocket):
        await gateway.handle_socket(ws)

    @app.get("/health")
    async def health():
        return {"status": "ok",
                "connected": sum(s.ws is not None for s in gateway.sessions.values()),
                "games": sum(not g.finished for g in gateway.games.values()),
                "lobbies": gateway.lobby.urls}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Queens Gateway")
    parser.add_argument("--host", default="127.0.0.1",
                        help="0.0.0.0 = für andere Rechner erreichbar")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--lobby", default="http://127.0.0.1:8001",
                        help="Lobby-Instanzen, kommagetrennt")
    parser.add_argument("--turn-timeout", type=float, default=60.0)
    parser.add_argument("--afk-timeout", type=float, default=15.0)
    parser.add_argument("--peek-timeout", type=float, default=60.0)
    args = parser.parse_args()

    import uvicorn

    setup_logging("gateway")
    app = create_app([u.strip() for u in args.lobby.split(",") if u.strip()],
                     turn_timeout=args.turn_timeout, afk_timeout=args.afk_timeout,
                     peek_timeout=args.peek_timeout)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
