# Queens – verteiltes Mehrspieler-Kartenspiel

Programmentwurf im Modul Verteilte Systeme (T4INF3301), DHBW Stuttgart.

Das Kartenspiel „Queens“ für 2–8 Spieler wird im Browser gespielt. Der
Spielzustand liegt in einem Raft-Cluster (Standard: 3 Knoten), die Lobbys
laufen auf mehreren zustandslosen Instanzen, Ergebnisse und Lobbydaten liegen
in PostgreSQL. Ein Gateway verbindet die Browser per WebSocket.

Die schriftliche Dokumentation steht in `doc/typst/` (Einstieg: `main.typ`).

---

## 1. Voraussetzungen

| Software | Version | Zweck |
| --- | --- | --- |
| Python | ≥ 3.10 | alle Dienste |
| PostgreSQL | ≥ 13 | Lobbys und Spielergebnisse (am einfachsten per Docker) |
| Browser | aktuell | Spieloberfläche |

## 2. Installation

Alle Befehle im Projektordner ausführen.

**Schritt 1 – Virtuelle Umgebung anlegen und Pakete installieren**

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

> Blockiert PowerShell das Aktivierungsskript, einmalig
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` ausführen.

**Schritt 2 – PostgreSQL bereitstellen**

Erwartet werden Benutzer `queens`, Passwort `queens` und die Datenbanken
`queens` (Betrieb) und `queens_test` (Tests) auf `127.0.0.1:5432`. Die Tabellen
legen die Dienste beim Start selbst an.

*Variante A – Docker (empfohlen):*

```bash
docker run -d --name queens-pg -p 5432:5432 -e POSTGRES_USER=queens -e POSTGRES_PASSWORD=queens -e POSTGRES_DB=queens postgres:16-alpine
docker exec queens-pg psql -U queens -c "CREATE DATABASE queens_test"
```

Später reicht `docker start queens-pg`, um die Datenbank wieder zu starten.

*Variante B – lokale PostgreSQL-Installation* (als Superuser, z. B. `psql -U postgres`):

```sql
CREATE USER queens WITH PASSWORD 'queens';
CREATE DATABASE queens OWNER queens;
CREATE DATABASE queens_test OWNER queens;
```

Andere Datenbankadresse: Option `--db postgresql://user:pw@host:5432/queens`
oder Umgebungsvariable `QUEENS_DB` (für Tests `QUEENS_TEST_DB`).

## 3. Ausführung

Vorher die virtuelle Umgebung aktivieren (siehe Schritt 1) und PostgreSQL starten.

**Alles starten**

```bash
python run_local.py --open
```

Damit starten 3 Raft-Knoten, 2 Lobby-Instanzen und das Gateway als eigene
Prozesse, und der Browser öffnet `http://127.0.0.1:8080/`.

**Spielen**

1. Namen eingeben, „Lobby erstellen“ wählen.
2. Weitere Spieler öffnen einen neuen Browser-Tab (jeder Tab ist ein Spieler)
   und treten mit dem Lobby-Code bei, oder sie nutzen „Link kopieren“.
3. Alle klicken „Bereit“, dann startet die Partie.

**Ausfall vorführen**

Im Tab „Server“ des Browsers oder in der Konsole, in der `run_local.py` läuft:

| Befehl | Wirkung |
| --- | --- |
| `status` | Rolle, Term, Leader und Log aller Knoten, Lobbys, Datenbank |
| `kill 1` / `start 1` | Raft-Knoten 1 hart beenden bzw. neu starten |
| `kill l1` / `start l1` | Lobby-Instanz 1 beenden bzw. neu starten |
| `isolate 1` / `heal 1` | Knoten 1 vom Netz trennen bzw. wieder verbinden |
| `ergebnisse` | letzte Partien aus PostgreSQL |
| `quit` | alle Dienste beenden |

Die Logs der Dienste liegen in `logs/`.

**Mit mehreren Rechnern spielen**

Nur der Host braucht PostgreSQL und startet:

```bash
python run_local.py --public
```

Die Mitspieler installieren nichts, sie öffnen `http://<IP-des-Hosts>:8000/`
im Browser. Auf dem Host muss die Firewall eingehende Verbindungen auf Port 8000
zulassen. Bei Problemen helfen `python netcheck.py host` (auf dem Host) und
`python netcheck.py client <IP-des-Hosts>` (beim Mitspieler).

**Weitere Optionen**

```bash
python run_local.py --nodes 5 --lobbies 3                     # größerer Cluster
python -m client.cli --server ws://127.0.0.1:8000/ws --name Anna   # Terminal-Client
python hotseat.py                                             # Spiellogik ohne Netzwerk und DB testen
```

Ports: Leitstand 8080, Gateway 8000, Lobby-Instanz *i* = 8000 + *i*,
Raft-Knoten *n* = 8100 + *n*. Alle Optionen: `python run_local.py --help`.

## 4. Tests

```bash
pytest
```

Ohne erreichbare Testdatenbank `queens_test` werden die Datenbank-, Lobby- und
Gateway-Tests übersprungen, alle anderen laufen trotzdem.

## 5. Projektstruktur

```
queens_engine/    Spiellogik als deterministische Zustandsmaschine
services/raft/    Raft-Knoten: Leader-Wahl, Log-Replikation, Zustandsmaschine
services/lobby/   Lobby-Service (zustandslos, Daten in PostgreSQL)
services/gateway/ Gateway: WebSockets zum Browser, leitet an Lobby und Raft weiter
client/web/       Browser-Client (Spieltisch, Lobby, Leitstand)
client/cli.py     Terminal-Client
run_local.py      startet alle Dienste und die Admin-Konsole
control.py        Leitstand im Browser (Tab „Server“)
tests/            Unit-, Integrations- und Ende-zu-Ende-Tests
doc/typst/        Dokumentation (Typst)
```
