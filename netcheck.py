"""Netzwerk-Check für Spiele über mehrere Rechner.

  python netcheck.py host   [--port 8000]          auf dem Rechner mit dem Gateway
  python netcheck.py client <adresse> [--port 8000] auf dem Rechner eines Spielers

Host: zeigt die lokalen IP-Adressen und ob das Gateway darauf lauscht.
Client: prüft TCP-Verbindung und /health und erklärt die Ursache, wenn es
nicht klappt (Zeitüberschreitung = Firewall oder falsches Netz, abgelehnt =
Gateway läuft nicht). `<adresse>` darf auch die ws://…-Adresse des Clients
sein. Nur Standardbibliothek, damit es auch ohne installierte Pakete läuft.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import urlsplit

DEFAULT_PORT = 8000
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")  # Carrier-Grade-NAT, u. a. Tailscale

EXPLAIN = {
    "ok": "Verbindung klappt.",
    "timeout": ("Zeitüberschreitung: Es kommt keine Antwort. Meist blockiert die "
                "Firewall des Hosts Port {port}, oder die Rechner sind nicht im selben "
                "Netz (eduroam und Gäste-WLANs trennen Geräte oft voneinander; "
                "Handy-Hotspot oder Tailscale nutzen)."),
    "refused": ("Verbindung abgelehnt: Der Rechner ist erreichbar, aber auf Port {port} "
                "lauscht nichts. Läuft das Gateway? Für andere Rechner muss es mit "
                "run_local.py --public bzw. --host 0.0.0.0 gestartet sein."),
    "dns": "Name nicht auflösbar: Tippfehler in der Adresse? Am besten die IP verwenden.",
    "unreachable": ("Netz nicht erreichbar: Dieser Rechner hat keine Route dorthin "
                    "(nicht verbunden, oder falsches Netz)."),
}


# --- Adressen -----------------------------------------------------------------------
def local_ips() -> list[str]:
    """IPv4-Adressen dieses Rechners, über die andere ihn erreichen können
    (ohne 127.x). Wird auch von run_local.py --public genutzt."""
    addresses: list[str] = []
    try:  # Adresse der Standardroute; dabei wird nichts gesendet
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


def classify(ip: str) -> str:
    """loopback | lokal | tailscale | link-local | öffentlich | ungültig"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "ungültig"
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link-local"
    if addr.version == 4 and addr in TAILSCALE:
        return "tailscale"
    if addr.is_private:
        return "lokal"
    return "öffentlich"


def parse_target(target: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """'ws://1.2.3.4:8000/ws', 'http://host', '1.2.3.4:8000' oder '1.2.3.4'."""
    if "://" not in target:
        target = "//" + target
    parts = urlsplit(target)
    if not parts.hostname:
        raise ValueError(f"Keine Adresse erkannt: {target!r}")
    return parts.hostname, parts.port or default_port


# --- Prüfungen ----------------------------------------------------------------------
def check_tcp(host: str, port: int, timeout: float = 3.0) -> tuple[str, str]:
    """(Ergebnis, technische Meldung); Ergebnis ist ein Schlüssel von EXPLAIN."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "ok", ""
    except socket.gaierror as e:
        return "dns", str(e)
    except (socket.timeout, TimeoutError) as e:
        return "timeout", str(e) or "timed out"
    except ConnectionRefusedError as e:
        return "refused", str(e)
    except OSError as e:
        return "unreachable", str(e)


def check_health(host: str, port: int, timeout: float = 3.0) -> tuple[bool, Any]:
    """GET /health des Gateways: (erfolgreich, JSON-Antwort oder Fehlertext)."""
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, str(getattr(e, "reason", e))


# --- Berichte ---------------------------------------------------------------------
def host_report(port: int) -> int:
    print(f"Host-Check für Port {port}\n")
    ips = local_ips()
    if not ips:
        print("Keine Netzwerkadresse gefunden: Ist der Rechner mit einem Netz verbunden?")
    reachable = []
    for ip in ["127.0.0.1", *ips]:
        status, _ = check_tcp(ip, port, timeout=1.0)
        listening = "lauscht ✓" if status == "ok" else "lauscht nicht ✗"
        print(f"  {ip:<16} {classify(ip):<11} Gateway {listening}")
        if status == "ok" and ip != "127.0.0.1":
            reachable.append(ip)
    local_ok = check_tcp("127.0.0.1", port, timeout=1.0)[0] == "ok"
    print()
    if not local_ok and not reachable:
        print("Das Gateway läuft nicht. Starten mit: python run_local.py --public")
        return 1
    if not reachable:
        print("Das Gateway lauscht nur auf 127.0.0.1, andere Rechner kommen nicht ran.\n"
              "Neu starten mit: python run_local.py --public  (bzw. --host 0.0.0.0)")
        return 1
    for ip in reachable:
        print(f"Mitspieler verbinden sich mit:  python -m client.cli "
              f"--server ws://{ip}:{port}/ws --name <Name>")
    print("\nHinweis: Die Firewall lässt sich nur vom anderen Rechner aus prüfen "
          "(python netcheck.py client <ip>). Unter Windows Port freigeben (als Admin):\n"
          f"  New-NetFirewallRule -DisplayName \"Queens Gateway\" -Direction Inbound "
          f"-Protocol TCP -LocalPort {port} -Action Allow")
    return 0


def client_report(target: str, port: int) -> int:
    try:
        host, port = parse_target(target, port)
    except ValueError as e:
        print(f"✗ {e}")
        return 2
    print(f"Client-Check für {host}:{port}\n")
    ip: Optional[str]
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        ip = None
    if ip is not None:
        kind = classify(ip)
        print(f"  Adresse: {ip} ({kind})")
        if kind == "öffentlich":
            print("  ⚠ Das ist eine öffentliche IP-Adresse, z. B. von „wieistmeineip“.\n"
                  "    Darüber ist der Host fast nie erreichbar (Router/NAT). Im selben\n"
                  "    WLAN oder Hotspot die lokale Adresse nehmen, die\n"
                  "    'python netcheck.py host' auf dem Host anzeigt.")
        elif kind == "loopback":
            print("  ⚠ 127.0.0.1 ist immer der eigene Rechner, nicht der Host.")
    status, detail = check_tcp(host, port)
    mark = "✓" if status == "ok" else "✗"
    print(f"  TCP-Verbindung: {mark} {EXPLAIN[status].format(port=port)}")
    if detail:
        print(f"    ({detail})")
    if status != "ok":
        return 1
    ok, health = check_health(host, port)
    if not ok:
        print(f"  /health: ✗ {health} – lauscht dort wirklich das Queens-Gateway?")
        return 1
    print(f"  /health: ✓ {health}")
    print(f"\nAlles bereit:  python -m client.cli --server ws://{host}:{port}/ws --name <Name>")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Queens – Netzwerk-Check")
    sub = parser.add_subparsers(dest="mode")
    host = sub.add_parser("host", help="auf dem Rechner mit dem Gateway")
    host.add_argument("--port", type=int, default=DEFAULT_PORT)
    client = sub.add_parser("client", help="auf dem Rechner eines Spielers")
    client.add_argument("address", help="IP, host:port oder ws://host:port/ws")
    client.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.mode == "client":
        return client_report(args.address, args.port)
    return host_report(getattr(args, "port", DEFAULT_PORT))


if __name__ == "__main__":
    sys.exit(main())
