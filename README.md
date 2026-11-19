# Queens – verteiltes Mehrspieler-Kartenspiel

Projekt im Modul Verteilte Systeme (DHBW Stuttgart).

**Stand:** Spiellogik, Raft-Cluster, Lobby-Service, Gateway und Browser-Client
sind umgesetzt und getestet. Das ganze Spiel ist im Browser spielbar, die
Dienste lassen sich dort auch starten und stoppen (Tab „Server“).

## Schnellstart

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
python run_local.py --public --open   # alles starten, Browser öffnet http://127.0.0.1:8080/
python hotseat.py                     # Spiel im Terminal testen (Hotseat, 2–8 Spieler)
pytest                                # Tests (DB-Tests brauchen PostgreSQL, s. u.)
```

- `--public`: Mitspieler auf anderen Rechnern (WLAN, Handy-Hotspot) können
  beitreten. Ohne `--public` ist das Spiel nur auf diesem Rechner erreichbar.
- `--open`: öffnet den Browser.
- In PyCharm: in der Run-Konfiguration von `run_local.py` unter „Parameters“
  `--public --open` eintragen.
- Keine Adminrechte nötig. Nur **einmal** starten – läuft noch eine ältere
  Instanz, bricht `run_local.py` mit „Ports schon belegt“ ab.

Python ≥ 3.10. Die Spiellogik selbst braucht keine externen Pakete.

## Struktur

```
queens_engine/            Spiellogik – FERTIG
  config.py               Regelvarianten (Handgröße, Klopf-Sperre, Strafkarte)
  models.py               Karte, Spieler, GameState, create_game()
  actions.py              Aktionen + Serialisierung (für Netzwerk / Raft-Log)
  rules.py                validate(): Regelprüfung je Phase
  engine.py               apply(): Zustandsübergänge, deterministisch
  scoring.py              Punktewertung
  views.py                player_view(): was ein Spieler sehen darf
  events.py               Ereignisse mit Empfängerkreis (alle / ein Spieler)

services/
  backend.py              GameBackend-Schnittstelle + LocalGameBackend
  gateway/app.py          Realtime-Gateway: FastAPI + WebSockets
  lobby/app.py            Lobby-Service (zustandslos, Daten in PostgreSQL)
  db.py, schema.sql       Datenbankzugriff und Schema
  raft/                   Raft-Knoten: Wahl, Log-Replikation, Zustandsmaschine
  cluster_stub.py         Ersatz-Cluster im Prozess (für Tests)

client/
  cli.py                  Terminal-Client für das Gateway (python -m client.cli)
  render.py               Texte für Ereignisse, Lobby, Tisch und Auflösung
  web/                    Browser-Client: Spieltisch, Lobby, Leitstand (Tab „Server“)
run_local.py              startet alle Dienste als Prozesse + Admin-Konsole + Leitstand
control.py                Leitstand: liefert den Browser-Client aus, HTTP-API zum Steuern
netcheck.py               Netzwerk-Check für Host und Client
hotseat.py                Terminal-Testmodus (Hotseat, ohne Netzwerk)
tests/                    Unit-, Integrations- und Ende-zu-Ende-Tests
```

## Architekturprinzip

Die Engine ist eine **deterministische Zustandsmaschine**:
`apply(state, action) -> (new_state, events)`. Kein Netzwerk, keine Uhrzeit,
Zufall nur über den Seed in `StartGame`. Dadurch können später alle
Raft-Replikate dieselben bestätigten Aktionen in derselben Reihenfolge anwenden
und erreichen denselben Zustand.

Das Gateway kennt nur `GameBackend`. Heute: `LocalGameBackend` (Engine im
Prozess). Später: `RaftGameBackend` (Aktionen an den Leader des Clusters).

Gleichzeitige Aktionen (z. B. Zwischenwerfen) muss die Engine nicht lösen:
Sie bekommt Aktionen immer nacheinander. Die Reihenfolge legt das Backend fest
(lokal per Lock, später der Raft-Leader über das Log).

## Spielregeln (umgesetzt)

- 2–8 Spieler, 52er-Blatt, je 4 verdeckte Karten, eine Karte offen auf der Ablage
- Startphase: jeder sieht sich 2 eigene Karten einmalig an
- Gespielt wird „wie am echten Tisch“: Karten bleiben verdeckt, man muss sie sich merken
- Zug: vom Zieh- oder Ablagestapel ziehen, dann gegen eigene Karte tauschen oder ablegen
- Wird ein Bube oder König abgelegt (auch nach Aufnahme vom Ablagestapel):
  - Bube: zwei beliebige Karten blind tauschen
  - König: eine beliebige Karte ansehen
  - Sonderaktion kann übersprungen werden
- Zwischenwerfen: jeder Spieler, jederzeit, Karte gleichen Rangs wie die oberste Ablagekarte
  - falsch geworfen: Karte wird gezeigt, bleibt liegen, dazu eine Strafkarte
- Klopfen statt Ziehen: alle anderen spielen noch einmal, dann wird aufgedeckt
  - Karten des Klopfenden sind ab dann gesperrt (abschaltbar in `config.py`)
- Werte: Dame −1, Ass 1, Zahlen nach Augenwert, Bube/König 10; niedrigste Summe gewinnt
- Leerer Ziehstapel: Ablagestapel (ohne oberste Karte) wird neu gemischt
- Timeout (`TurnTimeout`, vom System erzeugt): Zug wird übersprungen; in der
  Startphase verzichten Spieler, die noch nicht angesehen haben, aufs Ansehen

## PostgreSQL einrichten

Lobby-Service und Ergebnisse brauchen eine zentrale PostgreSQL (ab Version 13).
Benutzer `queens`, Passwort `queens`, Datenbanken `queens` (Betrieb) und
`queens_test` (Tests). Das Schema legen die Dienste beim Start selbst an
(`services/schema.sql`).

**Mit Docker:**

```bash
docker run -d --name queens-pg -p 5432:5432 -e POSTGRES_USER=queens \
    -e POSTGRES_PASSWORD=queens -e POSTGRES_DB=queens postgres:16-alpine
docker exec queens-pg psql -U queens -c "CREATE DATABASE queens_test"
```

**Mit lokaler Installation** (als Superuser, z. B. `psql -U postgres`):

```sql
CREATE USER queens WITH PASSWORD 'queens';
CREATE DATABASE queens OWNER queens;
CREATE DATABASE queens_test OWNER queens;
```

Andere Adresse: `--db postgresql://user:pw@host:5432/queens` bzw. Umgebungsvariable
`QUEENS_DB`; für die Tests `QUEENS_TEST_DB`. Ohne erreichbare Testdatenbank werden
die Datenbank-, Lobby- und Gateway-Tests übersprungen.

## Lobby und Gateway starten

```bash
# zwei Lobby-Instanzen (Ports 8001, 8002), Cluster "a" mit drei Raft-Knoten
python -m services.lobby.app --id l1 --clusters "a=http://127.0.0.1:8101|http://127.0.0.1:8102|http://127.0.0.1:8103"
python -m services.lobby.app --id l2 --clusters "a=http://127.0.0.1:8101|http://127.0.0.1:8102|http://127.0.0.1:8103"
# Gateway (Port 8000), verteilt Lobby-Anfragen reihum
python -m services.gateway.app --lobby http://127.0.0.1:8001,http://127.0.0.1:8002
```

Optionen des Gateways: `--host 0.0.0.0` (für andere Rechner), `--turn-timeout 60`,
`--afk-timeout 15` (Spieler am Zug getrennt), `--peek-timeout 60`.
Die Endpunkte und Nachrichten stehen in den Docstrings von
`services/lobby/app.py` und `services/gateway/app.py`.

## Spielen

### Im Browser (empfohlen)

```bash
python run_local.py --public --open          # startet alles, öffnet den Leitstand
python run_local.py --public --no-autostart  # nur Leitstand; Dienste im Tab „Server“ starten
```

`http://127.0.0.1:8080/` (Leitstand) oder `http://127.0.0.1:8000/` (Gateway)
zeigen denselben Client. Jeder Browser-Tab ist ein eigener Spieler.

- **Spiel:** Name eingeben, Lobby erstellen oder mit Code beitreten
  („Link kopieren“ erzeugt einen Einladungslink), alle auf „Bereit“.
  Am Tisch ist alles anklickbar und leuchtet, wenn es gerade dran ist:
  Ziehstapel, Ablage (nehmen bzw. gezogene Karte ablegen), eigene Karten
  (ansehen, tauschen, zwischenwerfen), beim Buben/König beliebige Karten.
  Tasten: `Z` ziehen, `A` Ablage, `W` ablegen, `S` überspringen, `K` klopfen,
  `X` zwischenwerfen, `0`–`9` eigene Karte, `Esc` abbrechen.
- **Server:** Status aller Raft-Knoten (Rolle, Term, Log, Commit), Lobbys,
  Gateway und Datenbank; Dienste einzeln oder alle starten/stoppen (Stoppen =
  harter Absturz), Raft-Knoten isolieren/verbinden, Logs und letzte Partien.
  Der Leitstand lauscht nur auf `127.0.0.1`, steuern geht also nur am
  Host-Rechner. Port ändern mit `--control-port`, `0` schaltet ihn ab.
- Mit `--public` spielen andere Rechner über `http://<IP-des-Hosts>:8000/` mit.
  „Link kopieren“ setzt dafür automatisch die IP des Hosts und Port 8000
  ein (nicht 127.0.0.1 bzw. den Leitstand-Port 8080). Klappt der Link nicht,
  stehen unter „Andere Adressen“ in der Lobby alle Adressen des Hosts
  (WLAN/Hotspot, Tailscale) zum Kopieren.

### Im Terminal

```bash
python -m client.cli --server ws://127.0.0.1:8000/ws --name Anna
```

Befehle im Terminal (`h` zeigt sie auch im Spiel):

| Lobby | Spiel (Slots ab 0, Spieler als Nummer aus der Tabelle) | Sonst |
| --- | --- | --- |
| `neu` Lobby anlegen | `p <slot> <slot>` Startkarten ansehen | `tisch` neu anzeigen |
| `join <code>` beitreten | `z` ziehen · `a` Ablage nehmen | `h` Hilfe |
| `bereit` / `nicht` | `t <slot>` tauschen · `w` ablegen | `q` beenden |
| `verlassen` | `b <sp> <slot> <sp> <slot>` Bube · `k <sp> <slot>` König · `s` überspringen | |
| | `klopf` klopfen · `x <slot>` zwischenwerfen | |

Bricht die Verbindung ab, verbindet sich der Client selbst neu. Nach einem
Neustart mit `--player-id <id>` (wird beim Anmelden und Beenden angezeigt)
ist man wieder im laufenden Spiel; im Browser reicht Neuladen.

**Über mehrere Rechner:** Nur der Host braucht PostgreSQL und startet das
Backend mit `python run_local.py --public`. Mitspieler starten **nichts**
(kein `run_local.py`, keine Datenbank) und öffnen nur `http://<IP-des-Hosts>:8000/`
im Browser bzw. den Einladungslink. Der Tab „Server“ funktioniert nur am Host.
Auf dem Host zeigt `python netcheck.py host` die IP-Adressen und die
Client-Befehle; auf jedem Spieler-Rechner prüft
`python netcheck.py client <ip-des-hosts>` die Verbindung und erklärt
Fehler (Firewall, falsches Netz, Gateway läuft nicht).

## Nächste Schritte

1. Browser-Client gestalten (Aufbau und `client/web/protocol.js` bleiben)

## Nächste Schritte

1. Frontend: Spieltisch im Browser
2. Raft-Knoten und `ClusterClient` (ersetzen den Stub)
