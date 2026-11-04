# Testprotokoll

Material für das Kapitel zur Evaluation. Jede Person ergänzt die Messungen zu
ihrer Komponente; bitte Datum, Rechner und Einstellungen mit angeben.

## 1. Leader-Wahl und Ausfall (P3) – 04.11.2026

**Aufbau:** echte Prozesse (`python -m services.raft.server`) auf einem Rechner
(Windows 11, Python 3.12), Standardeinstellungen: Election-Timeout 1,5–3 s,
Heartbeat 0,3 s, RPC-Timeout 1 s. Gemessen wurde von außen über `GET /status`
(Abfrage alle 20 ms). „Neuer Leader“ heißt: genau ein Knoten ist Leader und alle
laufenden Knoten kennen ihn.

| Messung                                         | 3 Knoten      | 5 Knoten      |
|-------------------------------------------------|---------------|---------------|
| Erste Wahl nach dem Start der Prozesse          | 1,58 s        | 1,16 s        |
| Neuer Leader nach Absturz, Mittel (5 Läufe)     | 1,88 s        | 1,78 s        |
| Neuer Leader nach Absturz, min–max              | 1,48–2,69 s   | 1,50–2,06 s   |
| Neuer Leader nach Isolation, Mittel (3 Läufe)   | 1,95 s        | 1,92 s        |
| Isolierter Leader tritt zurück, Mittel          | 2,99 s        | 2,96 s        |
| Leader bei (n−1)/2 Ausfällen                     | ja (1 Ausfall)| ja (2 Ausfälle)|
| Leader bei einem Ausfall mehr (nach 8 s)        | nein          | nein          |

**Beobachtungen**

- Die Zeit bis zum neuen Leader liegt wie erwartet im Bereich des
  Election-Timeouts. Werte knapp unter 1,5 s entstehen, weil der Timer der
  Follower schon mit dem letzten Heartbeat vor dem Absturz läuft.
- Absturz (`kill`) und Isolation (`isolate`) verhalten sich für die übrigen
  Knoten gleich. Der Unterschied liegt beim alten Leader: Bei Isolation hält er
  sich noch bis zu 3 s für Leader (Check-Quorum), kann in dieser Zeit aber
  nichts bestätigen. Danach beantwortet er Anfragen mit `not_leader`.
- Nach `heal` übernimmt der zurückgekehrte Knoten den aktuellen Leader und holt
  das Log auf. Weil er während der Isolation Wahlen gestartet hat, ist sein Term
  höher; das löst manchmal eine zusätzliche Neuwahl aus (kein PreVote, siehe
  Reflexion).
- Mit gespeichertem Raft-Zustand (`--data-dir`) behält ein neu gestarteter Knoten
  Term und Stimme; im Log ist das als „Raft-Zustand geladen“ zu sehen.

**Automatische Tests:** `tests/test_raft.py` (13 Tests, simuliertes Netz),
`tests/test_raft_server.py` (HTTP-Endpunkte), `tests/test_raft_cluster.py`
(Wahl mit Replikation). Jeweils fünfmal hintereinander grün.

## 2. Ende-zu-Ende (alle, Federführung P3) – 04.11.2026

`tests/test_e2e.py`: alle Dienste als Prozesse auf Ports 18xxx, Raft mit
verkürzten Zeiten (0,5–1 s, Heartbeat 0,1 s), zwei WebSocket-Clients.

| Schritt | Prüfung                                                  | Ergebnis |
|---------|----------------------------------------------------------|----------|
| 1       | Lobby-Instanz `l1` beendet, alles läuft über `l2`        | ok       |
| 2       | Lobby, Beitritt, Start; Startkarten nur beim Besitzer    | ok       |
| 3       | Ungültige Aktion: Fehler nur beim Absender               | ok       |
| 4       | Raft-Leader beendet, nächster Zug klappt, neuer Leader   | ok       |
| 5       | Reconnect mit gleicher ID bringt die aktuelle Sicht      | ok       |
| 6       | Klopfen, Spielende, Punkte stimmen mit den Karten überein | ok       |
| 7       | Ausgefallener Knoten startet neu und holt das Log auf    | ok       |
| 8       | Ergebnis genau einmal in PostgreSQL, Lobby `finished`    | offen: wartet auf `services/raft/results.py` |

Der Ablauf lief zehnmal hintereinander fehlerfrei (je etwa 9 s). Dabei wurde
das Lobby-Repository durch eine Attrappe im Speicher ersetzt, weil auf dem
Testrechner keine Testdatenbank eingerichtet war; Raft, Gateway und
WebSockets liefen echt. **Noch zu tun:** mit echter PostgreSQL (`queens_test`)
wiederholen.

## 3. Noch offen

- [ ] Verhalten bei Datenbankausfall (P2, P4): PostgreSQL-Dienst stoppen,
      weiterspielen, Ergebnis nach dem Neustart nachgeholt?
- [ ] Test auf mehreren Rechnern (Hotspot oder Tailscale): drei Raft-Knoten auf
      drei Rechnern, Zeit bis zum neuen Leader erneut messen
- [ ] Gleichzeitiges Bereitmelden über zwei Lobby-Instanzen (P2)
