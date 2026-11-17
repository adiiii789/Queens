"""Terminal-Client: verbindet sich per WebSocket mit dem Gateway.

Enthält keine Spielregeln. Eingaben werden in Nachrichten des
WebSocket-Protokolls übersetzt (siehe services/gateway/app.py), geprüft wird
alles auf dem Server. Die Anzeige übernimmt client/render.py.

Start:
  python -m client.cli --server ws://127.0.0.1:8000/ws --name Anna
  python -m client.cli --server ws://… --name Anna --player-id <id>   (Wiedereinstieg)

Bei Verbindungsabbruch verbindet sich der Client selbst neu (1 s, verdoppelt
bis 8 s) und meldet sich mit derselben player_id an. Eingaben liest ein
eigener Thread, damit eingehende Nachrichten nie blockiert werden.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
import uuid
from typing import Any, Callable, Optional, Union

import websockets
from websockets.exceptions import InvalidURI, WebSocketException

from .render import (
    LINE, Names, describe, player_numbers, render_lobby, render_table,
)

HELP = """
Lobby:
  neu                 neue Lobby anlegen
  join <code>         Lobby beitreten
  bereit / nicht      bereit melden / zurücknehmen
  verlassen           Lobby verlassen
Spiel (Slots ab 0, Spieler als Nummer aus der Tabelle):
  p <slot> <slot>     zu Beginn zwei eigene Karten ansehen
  z                   vom Ziehstapel ziehen
  a                   oberste Karte vom Ablagestapel nehmen
  t <slot>            gezogene Karte gegen eigenen Slot tauschen
  w                   gezogene Karte ablegen
  b <sp> <slot> <sp> <slot>   Bube: zwei Karten tauschen
  k <sp> <slot>       König: eine Karte ansehen
  s                   Sonderaktion überspringen
  klopf               klopfen (statt ziehen)
  x <slot>            eigene Karte zwischenwerfen (jederzeit)
Sonst:
  tisch               Tisch bzw. Lobby neu anzeigen
  h                   diese Hilfe
  q                   beenden
"""

RECONNECT_START = 1.0
RECONNECT_MAX = 8.0
CLOSE_REPLACED = 4001   # Gateway: durch neue Verbindung mit gleicher ID ersetzt
CLOSE_REJECTED = 4400   # Gateway: erste Nachricht war kein gültiges hello

Message = dict[str, Any]


class CommandError(ValueError):
    """Eingabe ist kein gültiger Befehl; Text geht an den Spieler."""


# --- Eingabe → Nachricht ---------------------------------------------------------
def _int(value: str, what: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise CommandError(f"{what} muss eine Zahl sein, nicht {value!r}.") from None


def _player(value: str, players: list[str]) -> str:
    if not players:
        raise CommandError("Spielernummern gibt es erst, wenn die Partie läuft.")
    number = _int(value, "Spielernummer")
    if not 1 <= number <= len(players):
        raise CommandError(f"Spieler {number} gibt es nicht (1–{len(players)}).")
    return players[number - 1]


def _ref(player: str, slot: str, players: list[str]) -> dict[str, Any]:
    return {"player_id": _player(player, players), "slot": _int(slot, "Slot")}


def _action(type_: str, **fields: Any) -> Message:
    # Neue action_id je Eingabe: das Gateway darf sie nach einem Leader-Wechsel
    # gefahrlos erneut einreichen, angewendet wird sie trotzdem nur einmal.
    return {"op": "action", "action": {"type": type_, "action_id": uuid.uuid4().hex,
                                       **fields}}


# Befehl → (Anzahl Argumente, Nachricht aus Argumenten und Spielerliste)
_COMMANDS: dict[str, tuple[int, Callable[[list[str], list[str]], Message]]] = {
    "neu": (0, lambda a, p: {"op": "create_lobby"}),
    "join": (1, lambda a, p: {"op": "join_lobby", "lobby_id": a[0].upper()}),
    "bereit": (0, lambda a, p: {"op": "ready", "ready": True}),
    "nicht": (0, lambda a, p: {"op": "ready", "ready": False}),
    "verlassen": (0, lambda a, p: {"op": "leave_lobby"}),
    "p": (2, lambda a, p: _action("PeekInitial", slots=[_int(s, "Slot") for s in a])),
    "z": (0, lambda a, p: _action("DrawFromDeck")),
    "a": (0, lambda a, p: _action("TakeFromDiscard")),
    "t": (1, lambda a, p: _action("SwapWithHand", slot=_int(a[0], "Slot"))),
    "w": (0, lambda a, p: _action("DiscardDrawn")),
    "b": (4, lambda a, p: _action("UseJack", first=_ref(a[0], a[1], p),
                                  second=_ref(a[2], a[3], p))),
    "k": (2, lambda a, p: _action("UseKing", target=_ref(a[0], a[1], p))),
    "s": (0, lambda a, p: _action("SkipSpecial")),
    "klopf": (0, lambda a, p: _action("Knock")),
    "x": (1, lambda a, p: _action("ThrowIn", slot=_int(a[0], "Slot"))),
}
LOCAL_COMMANDS = ("tisch", "h", "q")


def parse_command(line: str, players: list[str]) -> Union[Message, str, None]:
    """Übersetzt eine Eingabezeile. Ergebnis: Nachricht an das Gateway, Name
    eines lokalen Befehls (tisch, h, q) oder None bei leerer Zeile.
    `players` sind die player_ids in Tabellenreihenfolge."""
    words = line.split()
    if not words:
        return None
    command, args = words[0].lower(), words[1:]
    if command in LOCAL_COMMANDS:
        return command
    if command not in _COMMANDS:
        raise CommandError(f"Unbekannter Befehl {words[0]!r} (h für Hilfe).")
    count, build = _COMMANDS[command]
    if len(args) != count:
        raise CommandError(f"{command} erwartet {count} Angabe(n), siehe h.")
    return build(args, players)


# --- Client ----------------------------------------------------------------------
class TerminalClient:
    def __init__(self, server: str, name: str, player_id: Optional[str] = None,
                 out: Callable[[str], None] = print) -> None:
        self.server = server
        self.name = name
        self.player_id = player_id
        self.out = out
        self.ws: Any = None
        self.lobby: Optional[dict[str, Any]] = None
        self.view: Optional[dict[str, Any]] = None
        self.names: dict[str, str] = {}
        self.last_version = -1
        self.deadline: Optional[float] = None
        self.closing = False
        self.lines: Optional[asyncio.Queue] = None
        self._delay = RECONNECT_START

    # --- Ablauf ---------------------------------------------------------------
    async def run(self, lines: asyncio.Queue) -> None:
        """Läuft, bis `q` eingegeben wird, die Eingabe endet (None in der
        Queue) oder das Gateway die Verbindung endgültig ablehnt."""
        self.lines = lines
        connection = asyncio.create_task(self.connection_loop())
        try:
            while True:
                line = await lines.get()
                if line is None or not await self.handle_line(line):
                    break
        finally:
            self.closing = True
            connection.cancel()
            if self.ws is not None:
                await self.ws.close()
            try:
                await connection
            except asyncio.CancelledError:
                pass

    def stop(self) -> None:
        if self.lines is not None:
            self.lines.put_nowait(None)

    async def connection_loop(self) -> None:
        while not self.closing:
            code = None
            try:
                async with websockets.connect(self.server, ping_interval=5,
                                              ping_timeout=10, open_timeout=5) as ws:
                    self.ws = ws
                    await ws.send(json.dumps({"op": "hello", "name": self.name,
                                              "player_id": self.player_id}))
                    try:
                        async for raw in ws:
                            self.on_message(raw)
                    finally:
                        code = ws.close_code
            except InvalidURI:
                self.out(f"✗ Ungültige Server-Adresse {self.server!r} "
                         "(Beispiel: ws://127.0.0.1:8000/ws).")
                self.stop()
                return
            except (OSError, asyncio.TimeoutError, WebSocketException) as e:
                if self._delay == RECONNECT_START:  # nur beim ersten Fehlschlag erklären
                    self.out(f"✗ Keine Verbindung zu {self.server} ({type(e).__name__}). "
                             "Tipp: python netcheck.py client <host>")
            finally:
                self.ws = None
            if self.closing:
                return
            if code == CLOSE_REPLACED:
                self.out("✗ Mit deiner ID hat sich eine andere Verbindung angemeldet. Beendet.")
                self.stop()
                return
            if code == CLOSE_REJECTED:
                self.out("✗ Das Gateway hat die Anmeldung abgelehnt. Beendet.")
                self.stop()
                return
            self.out(f"… Verbindung weg, neuer Versuch in {self._delay:.0f} s")
            await asyncio.sleep(self._delay)
            self._delay = min(self._delay * 2, RECONNECT_MAX)

    # --- Eingang ---------------------------------------------------------------
    def on_message(self, raw: Union[str, bytes]) -> None:
        try:
            message = json.loads(raw)
            op = message.get("op")
        except (ValueError, AttributeError):
            self.out("✗ Unverständliche Nachricht vom Server.")
            return
        handler = getattr(self, f"_on_{op}", None)
        if handler is None:
            self.out(f"? {message}")
            return
        handler(message)

    def _on_welcome(self, m: Message) -> None:
        self.player_id = m["player_id"]
        self._delay = RECONNECT_START
        if m.get("reconnected"):
            self.out(f"✓ Wieder verbunden als {m['name']}.")
        else:
            self.out(f"✓ Angemeldet als {m['name']} (ID {self.player_id}).\n"
                     f"  Wiedereinstieg nach Neustart: --player-id {self.player_id}\n"
                     f"  Los geht's mit 'neu' oder 'join <code>' (h für Hilfe).")

    def _on_lobby(self, m: Message) -> None:
        lobby = m["lobby"]
        mine = any(p["player_id"] == self.player_id for p in lobby.get("players", []))
        self.lobby = lobby if mine and lobby.get("status") != "deleted" else None
        if mine:
            self.out(render_lobby(lobby, self.player_id))

    def _on_game_started(self, m: Message) -> None:
        self.names = dict(m.get("names") or {})
        self.view, self.last_version, self.deadline = None, -1, None
        self.out(f"{LINE}\nPartie {m['game_id']} beginnt mit "
                 + ", ".join(self.names.values()) + ".")

    def _on_events(self, m: Message) -> None:
        names = self.names_view()
        for event in m.get("events", []):
            self.out(describe(event, names))

    def _on_state(self, m: Message) -> None:
        view = m["view"]
        version = view.get("version", 0)
        if version < self.last_version:
            return  # veraltete Sicht, z. B. nach Leader-Wechsel doppelt zugestellt
        self.last_version, self.view = version, view
        timeout = m.get("timeout")
        self.deadline = time.monotonic() + timeout if timeout is not None else None
        self.show_table()

    def _on_info(self, m: Message) -> None:
        self.out(f"ℹ {m.get('message')}")

    def _on_error(self, m: Message) -> None:
        self.out(f"✗ {m.get('message')}")

    # --- Ausgang ---------------------------------------------------------------
    def names_view(self) -> Names:
        return Names(self.names, me=self.player_id)

    def remaining(self) -> Optional[float]:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def show_table(self) -> None:
        if self.view is not None:
            self.out(render_table(self.view, self.names_view(), self.remaining()))
        elif self.lobby is not None:
            self.out(render_lobby(self.lobby, self.player_id))
        else:
            self.out("Noch in keiner Lobby: 'neu' oder 'join <code>'.")

    async def handle_line(self, line: str) -> bool:
        """Verarbeitet eine Eingabe; False heißt beenden."""
        try:
            parsed = parse_command(line, player_numbers(self.view))
        except CommandError as e:
            self.out(f"✗ {e}")
            return True
        if parsed is None:
            return True
        if parsed == "q":
            return False
        if parsed == "h":
            self.out(HELP)
        elif parsed == "tisch":
            self.show_table()
        else:
            await self.send(parsed)
        return True

    async def send(self, message: Message) -> None:
        if self.ws is None:
            self.out("✗ Gerade nicht verbunden, bitte gleich noch einmal versuchen.")
            return
        try:
            await self.ws.send(json.dumps(message))
        except WebSocketException:
            self.out("✗ Senden fehlgeschlagen, Verbindung wird neu aufgebaut.")


# --- Start -------------------------------------------------------------------------
def start_input_thread(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    """stdin in eigenem Thread lesen und zeilenweise in die Queue legen."""
    def read() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)  # Eingabe zu Ende
    threading.Thread(target=read, name="stdin", daemon=True).start()


async def amain(client: TerminalClient) -> None:
    lines: asyncio.Queue = asyncio.Queue()
    start_input_thread(asyncio.get_running_loop(), lines)
    await client.run(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Queens – Terminal-Client")
    parser.add_argument("--server", default="ws://127.0.0.1:8000/ws",
                        help="Gateway-Adresse, z. B. ws://192.168.0.10:8000/ws")
    parser.add_argument("--name", required=True, help="Anzeigename")
    parser.add_argument("--player-id", default=None,
                        help="gespeicherte ID für den Wiedereinstieg nach einem Neustart")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Kartensymbole auch in alten Konsolen
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    client = TerminalClient(args.server, args.name, args.player_id)
    print(f"Queens – verbinde mit {args.server} … (h für Hilfe, q zum Beenden)")
    try:
        asyncio.run(amain(client))
    except KeyboardInterrupt:
        pass
    if client.player_id:
        print(f"\nBeendet. Wiedereinstieg: python -m client.cli --server {args.server} "
              f"--name {args.name} --player-id {client.player_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
