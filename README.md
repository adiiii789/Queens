# Queens – verteiltes Mehrspieler-Kartenspiel

Projekt im Modul Verteilte Systeme (DHBW Stuttgart).

**Stand:** Die Spiellogik ist vollständig und getestet. Gateway, Lobby, Raft
und Frontend sind als Platzhalter mit klaren Schnittstellen angelegt.

## Schnellstart

```bash
python -m pip install -r requirements.txt
python cli.py          # Spiel im Terminal testen (Hotseat, 2–8 Spieler)
pytest                 # Tests (DB-Tests brauchen PostgreSQL, s. u.)
```

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
  backend.py              GameBackend-Schnittstelle + LocalGameBackend – FERTIG
  gateway/app.py          PLATZHALTER: FastAPI + WebSockets
  lobby/app.py            PLATZHALTER: Lobby, Ready-Status
  raft/node.py            PLATZHALTER: Raft-Knoten + RaftGameBackend

frontend/                 PLATZHALTER: Browser-Client
cli.py                    Terminal-Testmodus
tests/test_engine.py      Unit-Tests
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
- Timeout (`TurnTimeout`, vom System erzeugt): Zug wird übersprungen

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

Solange `services/raft/client.py` fehlt, nutzen Lobby und Gateway den
`StubClusterClient` aus `services/cluster_stub.py` (Partien nur im Prozess).

## Nächste Schritte

1. Frontend: Spieltisch im Browser
2. Raft-Knoten und `ClusterClient` (ersetzen den Stub)
