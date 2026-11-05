"""Startet das ganze Backend lokal und steuert die Ausfall-Demo.

  python run_local.py                      3 Raft-Knoten, 2 Lobby-Instanzen, Gateway
  python run_local.py --nodes 5 --lobbies 3
  python run_local.py --public             Gateway für andere Rechner erreichbar

Jeder Dienst läuft als eigener Prozess (kein Docker), mit Log in
logs/<name>.log. Danach steuert eine Admin-Konsole die Demo:

  status               Rolle, Term, Leader, Log, Commit, Partien je Knoten; Lobbys; DB
  kill <name>          Prozess hart beenden (Absturz), z. B. kill 1, kill l2
  start <name>         beendeten Prozess neu starten
  isolate <n> / heal <n>   Raft-Knoten vom Netz trennen bzw. wieder verbinden
  leader               nur den aktuellen Leader zeigen
  ergebnisse           letzte Partien aus PostgreSQL
  log <name> [zeilen]  Ende der Log-Datei, z. B. log 2, log l1, log gateway
  hilfe                diese Übersicht
  quit                 alles sauber beenden

Ports: Gateway 8000, Lobby-Instanz i = 8000 + i, Raft-Knoten n = 8100 + n
(änderbar mit --gateway-port, --lobby-base, --raft-base).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
DEFAULT_DSN = "postgresql://queens:queens@127.0.0.1:5432/queens"


# --- Prozesse -----------------------------------------------------------------------
@dataclass
class Service:
    name: str            # Name in der Konsole: "1", "l1", "gateway"
    kind: str            # "raft" | "lobby" | "gateway"
    url: str
    args: list[str]
    log_file: Path
    process: Optional[subprocess.Popen] = None
    started_at: float = 0.0
    log_handle: Any = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.running:
            return
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = open(self.log_file, "a", encoding="utf-8")
        self.log_handle.write(f"\n===== Start {time.strftime('%H:%M:%S')} =====\n")
        self.log_handle.flush()
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        # Eigene Prozessgruppe: Strg+C in der Konsole trifft nur run_local,
        # das dann alle Dienste geordnet beendet.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [sys.executable, *self.args], cwd=ROOT, env=env, stdout=self.log_handle,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags,
            start_new_session=os.name != "nt")
        self.started_at = time.monotonic()

    def kill(self) -> None:
        """Hart beenden, wie ein Absturz (kein geordnetes Herunterfahren)."""
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None


class Backend:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.dsn = args.db or os.environ.get("QUEENS_DB") or DEFAULT_DSN
        host = "127.0.0.1"
        self.raft_urls = {str(n): f"http://{host}:{args.raft_base + n}"
                          for n in range(1, args.nodes + 1)}
        nodes_spec = ",".join(f"{n}={url}" for n, url in self.raft_urls.items())
        cluster_spec = "a=" + "|".join(self.raft_urls.values())
        self.services: dict[str, Service] = {}

        for n, url in self.raft_urls.items():
            self.services[n] = Service(n, "raft", url, [
                "-m", "services.raft.server", "--id", n, "--nodes", nodes_spec,
                "--election-min", str(args.election_min),
                "--election-max", str(args.election_max),
                "--heartbeat", str(args.heartbeat),
                "--data-dir", str(LOG_DIR / "raft-state"),
            ], LOG_DIR / f"raft-{n}.log")

        lobby_urls = []
        for i in range(1, args.lobbies + 1):
            name, port = f"l{i}", args.lobby_base + i
            url = f"http://{host}:{port}"
            lobby_urls.append(url)
            self.services[name] = Service(name, "lobby", url, [
                "-m", "services.lobby.app", "--id", name, "--port", str(port),
                "--db", self.dsn, "--clusters", cluster_spec,
            ], LOG_DIR / f"lobby-{name}.log")

        gateway_host = "0.0.0.0" if args.public else host
        self.gateway_url = f"http://{host}:{args.gateway_port}"
        self.services["gateway"] = Service("gateway", "gateway", self.gateway_url, [
            "-m", "services.gateway.app", "--host", gateway_host,
            "--port", str(args.gateway_port), "--lobby", ",".join(lobby_urls),
            "--turn-timeout", str(args.turn_timeout),
            "--afk-timeout", str(args.afk_timeout),
            "--peek-timeout", str(args.peek_timeout),
        ], LOG_DIR / "gateway.log")
        self.http = httpx.Client(timeout=1.0)

    def of_kind(self, kind: str) -> list[Service]:
        return [s for s in self.services.values() if s.kind == kind]

    def start_all(self) -> None:
        # Erst die Raft-Knoten, damit Lobby und Gateway gleich einen Leader finden.
        for kind in ("raft", "lobby", "gateway"):
            for service in self.of_kind(kind):
                service.start()

    def stop_all(self) -> None:
        for service in reversed(list(self.services.values())):
            service.kill()
        self.http.close()

    # --- Abfragen ---------------------------------------------------------------------
    def get(self, url: str) -> Optional[dict]:
        try:
            response = self.http.get(url)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            return None

    def raft_status(self) -> dict[str, Optional[dict]]:
        return {s.name: self.get(f"{s.url}/status") if s.running else None
                for s in self.of_kind("raft")}

    def current_leader(self) -> Optional[dict]:
        """Leader, den alle erreichbaren, nicht isolierten Knoten anerkennen."""
        states = [s for s in self.raft_status().values() if s and not s.get("isolated")]
        leaders = [s for s in states if s["role"] == "leader"]
        if len(leaders) == 1 and all(s["leader_id"] == leaders[0]["node_id"] for s in states):
            return leaders[0]
        return None

    def wait_until(self, condition: Callable[[], Any], timeout: float) -> Any:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = condition()
            if result:
                return result
            time.sleep(0.1)
        return None


# --- Datenbank ------------------------------------------------------------------------
def check_database(dsn: str) -> Optional[str]:
    """None, wenn erreichbar; sonst eine verständliche Fehlermeldung."""
    try:
        import asyncpg
    except ImportError:
        return "asyncpg fehlt: python -m pip install -r requirements.txt"

    async def ping():
        connection = await asyncpg.connect(dsn, timeout=3)
        await connection.close()

    try:
        asyncio.run(ping())
    except Exception as error:  # nicht erreichbar, falsches Passwort, fehlende DB …
        return f"{type(error).__name__}: {error}"
    return None


def recent_results(dsn: str, limit: int = 10) -> list[dict]:
    from services.db import Database, ResultRepository

    async def load():
        db = Database(dsn)
        try:
            return await ResultRepository(db).recent(limit)
        finally:
            await db.close()

    return asyncio.run(load())


# --- Netzwerk ------------------------------------------------------------------------
def local_ips() -> list[str]:
    """IPv4-Adressen dieses Rechners, über die andere ihn erreichen können."""
    try:
        from netcheck import local_ips as from_netcheck  # feature/frontend, falls vorhanden
        return list(from_netcheck())
    except (ImportError, AttributeError):
        pass
    addresses: list[str] = []
    try:  # Adresse der Standardroute; es wird nichts gesendet
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            addresses.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        addresses += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return [a for a in dict.fromkeys(addresses) if not a.startswith("127.")]


# --- Konsole --------------------------------------------------------------------------
class Console:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.commands: dict[str, Callable[[list[str]], None]] = {
            "status": self.status, "s": self.status,
            "leader": self.leader,
            "kill": self.kill, "start": self.start,
            "isolate": lambda a: self.isolate(a, True), "heal": lambda a: self.isolate(a, False),
            "ergebnisse": self.results,
            "log": self.log,
            "hilfe": self.help, "help": self.help, "h": self.help,
        }

    def run(self) -> None:
        print("\nAdmin-Konsole – 'hilfe' zeigt alle Befehle, 'quit' beendet alles.")
        while True:
            try:
                line = input("queens> ").strip()
            except EOFError:
                return
            if not line:
                continue
            try:
                parts = shlex.split(line)
            except ValueError:
                parts = line.split()
            command, args = parts[0].lower(), parts[1:]
            if command in ("quit", "exit", "q"):
                return
            handler = self.commands.get(command)
            if handler is None:
                print(f"Unbekannter Befehl {command!r}. 'hilfe' zeigt alle Befehle.")
                continue
            try:
                handler(args)
            except Exception as error:  # die Konsole soll nie abstürzen
                print(f"Fehler: {type(error).__name__}: {error}")

    def help(self, _args: list[str]) -> None:
        print(__doc__.split("Admin-Konsole die Demo:")[1].split("Ports:")[0].rstrip())

    def service(self, args: list[str], kinds: tuple[str, ...]) -> Optional[Service]:
        if not args:
            print("Name fehlt, z. B. 1, 2, l1 oder gateway.")
            return None
        service = self.backend.services.get(args[0].lower())
        if service is None or service.kind not in kinds:
            names = ", ".join(s.name for s in self.backend.services.values() if s.kind in kinds)
            print(f"Unbekannt: {args[0]!r}. Möglich: {names}")
            return None
        return service

    def status(self, _args: list[str]) -> None:
        backend = self.backend
        print("\nRaft-Knoten (Cluster a)")
        print(f"  {'Knoten':<7}{'Adresse':<24}{'Rolle':<21}{'Term':>5}  {'Leader':<7}"
              f"{'Log':>5}{'Commit':>8}{'Partien':>9}")
        for name, state in backend.raft_status().items():
            service = backend.services[name]
            if state is None:
                note = "beendet" if not service.running else "antwortet nicht"
                print(f"  {name:<7}{service.url:<24}-- {note}")
                continue
            role = state["role"] + (" (isoliert)" if state.get("isolated") else "")
            print(f"  {name:<7}{service.url:<24}{role:<21}{state['term']:>5}  "
                  f"{state['leader_id'] or '-':<7}{state['log_length']:>5}"
                  f"{state['commit_index']:>8}{state['games']:>9}")
        leader = backend.current_leader()
        majority = len(backend.raft_urls) // 2 + 1
        print(f"  Leader: {leader['node_id'] if leader else 'keiner bestätigt'} "
              f"(Mehrheit {majority} von {len(backend.raft_urls)})")

        print("\nLobby-Instanzen")
        for service in backend.of_kind("lobby"):
            health = backend.get(f"{service.url}/health") if service.running else None
            if health is None:
                note = "beendet" if not service.running else "antwortet nicht"
            else:
                note = "ok, Datenbank " + ("erreichbar" if health.get("db") else "NICHT erreichbar")
            print(f"  {service.name:<7}{service.url:<24}{note}")

        gateway = backend.services["gateway"]
        health = backend.get(f"{gateway.url}/health") if gateway.running else None
        print("\nGateway")
        if health is None:
            print(f"  {gateway.url:<31}{'beendet' if not gateway.running else 'antwortet nicht'}")
        else:
            print(f"  {gateway.url:<31}ok, {health['connected']} verbunden, "
                  f"{health['games']} laufende Partie(n)")

        error = check_database(backend.dsn)
        print(f"\nDatenbank  {'ok' if error is None else 'NICHT erreichbar – ' + error}\n")

    def leader(self, _args: list[str]) -> None:
        leader = self.backend.current_leader()
        if leader is None:
            print("Kein bestätigter Leader (Wahl läuft oder keine Mehrheit).")
        else:
            print(f"Leader: Knoten {leader['node_id']} im Term {leader['term']}")

    def kill(self, args: list[str]) -> None:
        service = self.service(args, ("raft", "lobby", "gateway"))
        if service is None:
            return
        if not service.running:
            print(f"{service.name} läuft nicht.")
            return
        was_leader = (service.kind == "raft" and (self.backend.current_leader() or {})
                      .get("node_id") == service.name)
        service.kill()
        print(f"{service.name} beendet{' (war Leader)' if was_leader else ''}.")
        if was_leader:
            started = time.monotonic()
            leader = self.backend.wait_until(self.backend.current_leader, 15)
            if leader:
                print(f"Neuer Leader: Knoten {leader['node_id']} im Term {leader['term']} "
                      f"nach {time.monotonic() - started:.1f} s.")
            else:
                print("Noch kein neuer Leader – fehlt die Mehrheit? ('status')")

    def start(self, args: list[str]) -> None:
        service = self.service(args, ("raft", "lobby", "gateway"))
        if service is None:
            return
        if service.running:
            print(f"{service.name} läuft bereits.")
            return
        service.start()
        print(f"{service.name} gestartet ({service.url}), Log: {service.log_file}")

    def isolate(self, args: list[str], isolated: bool) -> None:
        service = self.service(args, ("raft",))
        if service is None:
            return
        try:
            response = self.backend.http.post(f"{service.url}/admin/isolate",
                                              json={"isolated": isolated})
            response.raise_for_status()
        except httpx.HTTPError as error:
            print(f"Knoten {service.name} nicht erreichbar ({type(error).__name__}).")
            return
        if isolated:
            print(f"Knoten {service.name} isoliert. Ein isolierter Leader tritt nach "
                  f"spätestens {self.backend.args.election_max:.1f} s zurück (Check-Quorum).")
        else:
            print(f"Knoten {service.name} wieder verbunden; er übernimmt den aktuellen "
                  f"Leader und holt das Log auf.")

    def results(self, args: list[str]) -> None:
        limit = int(args[0]) if args and args[0].isdigit() else 10
        try:
            games = recent_results(self.backend.dsn, limit)
        except Exception as error:
            print(f"Datenbank nicht erreichbar ({type(error).__name__}: {error}).")
            return
        if not games:
            print("Noch keine gespeicherten Partien.")
        for game in games:
            print(f"\n  {game['game_id']}  (Lobby {game['lobby_id'] or '-'}, "
                  f"{game['finished_at'][:19]}, geklopft: {game['knocker'] or '-'})")
            for row in game["results"]:
                mark = "  <- gewinnt" if row["winner"] else ""
                print(f"    {row['name']:<16}{row['score']:>4} Punkte{mark}")
        print()

    def log(self, args: list[str]) -> None:
        service = self.service(args, ("raft", "lobby", "gateway"))
        if service is None:
            return
        lines = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
        try:
            content = service.log_file.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            print("Noch kein Log vorhanden.")
            return
        print("\n".join(content.splitlines()[-lines:]))


# --- Start ----------------------------------------------------------------------------
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Queens-Backend lokal starten")
    parser.add_argument("--nodes", type=int, default=3, help="Raft-Knoten (Standard 3)")
    parser.add_argument("--lobbies", type=int, default=2, help="Lobby-Instanzen (Standard 2)")
    parser.add_argument("--db", help=f"PostgreSQL-DSN (Standard: $QUEENS_DB bzw. {DEFAULT_DSN})")
    parser.add_argument("--public", action="store_true",
                        help="Gateway auf allen Netzwerkkarten erreichbar machen")
    parser.add_argument("--gateway-port", type=int, default=8000)
    parser.add_argument("--lobby-base", type=int, default=8000, help="Lobby i = Basis + i")
    parser.add_argument("--raft-base", type=int, default=8100, help="Knoten n = Basis + n")
    parser.add_argument("--election-min", type=float, default=1.5)
    parser.add_argument("--election-max", type=float, default=3.0)
    parser.add_argument("--heartbeat", type=float, default=0.3)
    parser.add_argument("--turn-timeout", type=float, default=60.0)
    parser.add_argument("--afk-timeout", type=float, default=15.0)
    parser.add_argument("--peek-timeout", type=float, default=60.0)
    parser.add_argument("--no-console", action="store_true",
                        help="ohne Admin-Konsole laufen, bis Strg+C")
    args = parser.parse_args(argv)
    if args.nodes < 1 or args.lobbies < 1:
        parser.error("--nodes und --lobbies müssen mindestens 1 sein")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    backend = Backend(args)

    error = check_database(backend.dsn)
    if error is not None:
        print(f"PostgreSQL nicht erreichbar ({backend.dsn}):\n  {error}\n"
              "Siehe Abschnitt „PostgreSQL einrichten“ in der README.", file=sys.stderr)
        return 1

    print(f"Starte {args.nodes} Raft-Knoten, {args.lobbies} Lobby-Instanz(en) und das "
          f"Gateway. Logs: {LOG_DIR}")
    try:
        backend.start_all()
        started = time.monotonic()
        leader = backend.wait_until(backend.current_leader, 30)
        if leader is None:
            print("Warnung: nach 30 s noch kein Leader – siehe 'log 1'.")
        else:
            print(f"Leader: Knoten {leader['node_id']} (Term {leader['term']}) "
                  f"nach {time.monotonic() - started:.1f} s")
        lobbies_up = backend.wait_until(lambda: all(
            backend.get(f"{s.url}/health") for s in backend.of_kind("lobby")), 20)
        gateway_up = backend.wait_until(lambda: backend.get(f"{backend.gateway_url}/health"), 20)
        if not lobbies_up or not gateway_up:
            print("Warnung: nicht alle Dienste antworten – siehe 'status' und 'log <name>'.")

        Console(backend).status([])
        ws = f"ws://127.0.0.1:{args.gateway_port}/ws"
        print(f"Client starten:  python -m client.cli --server {ws} --name Anna")
        if args.public:
            for ip in local_ips():
                print(f"Von anderen Rechnern: python -m client.cli "
                      f"--server ws://{ip}:{args.gateway_port}/ws --name <Name>")
            print("Firewall: eingehende Verbindungen auf Port "
                  f"{args.gateway_port} erlauben. Keine öffentliche IP verwenden.")

        if args.no_console:
            while True:
                time.sleep(1)
        else:
            Console(backend).run()
    except KeyboardInterrupt:
        print()
    finally:
        print("Beende alle Dienste …")
        backend.stop_all()
        print("Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
