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
| 8       | Ergebnis genau einmal in PostgreSQL, Lobby `finished`    | ok       |

Der Ablauf lief zehnmal hintereinander fehlerfrei (je etwa 9 s). Dabei wurde
das Lobby-Repository durch eine Attrappe im Speicher ersetzt, weil auf dem
Testrechner keine Testdatenbank eingerichtet war; Raft, Gateway und
WebSockets liefen echt.

**Wiederholung mit echter PostgreSQL (17.11.2026):** Windows 10, Python 3.12,
PostgreSQL 18 (lokale Installation, `queens_test`). `tests/test_e2e.py` lief
zehnmal hintereinander grün (9,4–13,4 s), einschließlich Schritt 8. Die
gesamte Testsuite (`python -m pytest`, 128 Tests) war grün, kein Test wurde
übersprungen.

## 3. Lobby, Gateway und Datenbank (P2) – 17.11.2026

**Aufbau:** wie oben, Windows 10, Python 3.12, PostgreSQL 18 (`queens_test`).

| Prüfung (Test)                                                        | Wiederholungen | Ergebnis |
|-----------------------------------------------------------------------|----------------|----------|
| Gleichzeitiges Bereitmelden über zwei Pools: genau ein Start-Ticket (`test_db.py`) | 20 je Lauf | ok |
| Gleichzeitiges Bereitmelden über zwei Lobby-Instanzen: Partie startet genau einmal (`test_lobby.py`) | 5 je Lauf | ok |
| 14 gleichzeitige Beitritte ergeben genau 8 Spieler (`test_db.py`)    | –              | ok       |
| Hängender Start wird von der anderen Instanz übernommen (`test_lobby.py`) | –          | ok       |
| Ausfall einer Lobby-Instanz unsichtbar, Reconnect, AFK (`test_gateway.py`) | –         | ok       |

`tests/test_db.py`, `tests/test_lobby.py` und `tests/test_gateway.py`
(17 Tests) liefen fünfmal hintereinander grün (je etwa 17 s).

## 4. Datenbankausfall (P2, P4) – 17.11.2026

**Aufbau:** alle Dienste als echte Prozesse wie im Ende-zu-Ende-Test (drei
Raft-Knoten mit verkürzten Zeiten, zwei Lobby-Instanzen, Gateway), zwei bzw.
drei WebSocket-Clients. Weil sich der lokale Windows-Dienst nur mit
Admin-Rechten stoppen lässt, lief PostgreSQL 16 für diese Messung in einem
Docker-Container (`docker stop` = geordnetes Herunterfahren, bestehende
Verbindungen werden beendet). Ablauf je Lauf: Partie starten, ein Zug,
Datenbank stoppen, neue Lobby versuchen, Partie zu Ende spielen, 10 s warten,
Datenbank starten. Messskript außerhalb des Repos (nach dem Vorbild von
`tests/test_e2e.py`).

| Messung                                                        | Lauf 1  | Lauf 2  | Lauf 3  |
|----------------------------------------------------------------|---------|---------|---------|
| Lobby-`/health` meldet `db: false` nach dem Stopp              | 0,02 s  | 0,02 s  | 0,03 s  |
| Neue Lobby während des Ausfalls                                | Fehler „Datenbank nicht erreichbar.“ | ebenso | ebenso |
| Zug in laufender Partie während des Ausfalls                   | 0,06 s  | 0,03 s  | 0,03 s  |
| Partie während des Ausfalls zu Ende gespielt, Fehler bei Spielern | ja, keine | ja, keine | ja, keine |
| Dauer des Ausfalls                                             | 10,5 s  | 10,5 s  | 10,5 s  |
| Ergebnis gespeichert nach Wiederanlauf der Datenbank           | 0,97 s  | 0,44 s  | 0,02 s  |
| Zeilen in `games` / `game_results`, Lobby-Status               | 1 / 2, `finished` | 1 / 2, `finished` | 1 / 2, `finished` |
| Punkte in der Datenbank stimmen mit der Partie überein         | ja      | ja      | ja      |
| Neue Lobby nach dem Wiederanlauf                               | ok      | ok      | ok      |

**Ausfall in der Lobby-Phase** (ein Lauf): Anna ist bereit, dann wird die
Datenbank gestoppt und Ben meldet sich bereit. Ben erhält „Datenbank nicht
erreichbar.“, Anna keinen Fehler. Nach dem Wiederanlauf ist die Lobby
unverändert (`waiting`, Anna weiterhin bereit); Ben meldet sich erneut bereit,
und die Partie startet.

**Beobachtungen**

- Laufende Partien merken vom Datenbankausfall nichts, weil das Gateway
  während der Partie nur mit dem Raft-Cluster spricht. Die Zugzeiten liegen im
  normalen Bereich.
- Nur der Leader versucht zu speichern: Im Log von Knoten 2 stehen in jedem
  Lauf etwa zehn Warnungen „Ergebnis … konnte nicht gespeichert werden“ (eine
  pro Sekunde), auf den Followern keine. Die Zeit bis zum Speichern nach dem
  Wiederanlauf (0,02–0,97 s, Mittel 0,48 s) entspricht diesem Takt von 1 s.
- Die Lobby-Instanzen verbinden sich ohne Neustart von selbst wieder; die erste
  Anfrage nach dem Wiederanlauf klappt.
- Nicht gemessen: eine Netzwerktrennung zur Datenbank ohne Verbindungsabbau
  (keine RST-Pakete). Dann greifen erst die Timeouts des Pools (3 s für den
  Verbindungsaufbau, 5 s je Befehl), und Anfragen würden entsprechend später
  mit 503 beantwortet.

## 5. Noch offen

- [x] Verhalten bei Datenbankausfall (P2, P4), siehe Abschnitt 4
- [ ] Test auf mehreren Rechnern (Hotspot oder Tailscale): drei Raft-Knoten auf
      drei Rechnern, Zeit bis zum neuen Leader erneut messen
- [x] Gleichzeitiges Bereitmelden über zwei Lobby-Instanzen (P2), siehe Abschnitt 3

## 6. Log-Replikation und Ergebnisspeicherung (P4)

**Verantwortlich** ist Luca Hertfelder. Die P4-Komponenten umfassen
`Replication`, `GameStateMachine`, `ClusterClient`, die Client-HTTP-Routen
und `ResultWriter` samt Anschluss im Game-Service und Weitergabe von
`--db` im Startskript.

**Prüfstand vom 10.11.2026** war Commit `e243e20` auf
`feature/Raft_Log&Consistency`. Die Tests liefen auf macOS mit Apple
Silicon, Python 3.13 und einer temporären PostgreSQL-18.6-Instanz.

| Prüfung | Ergebnis |
| --- | --- |
| Gesamte Testsuite mit echter PostgreSQL | 123 bestanden |
| Ende-zu-Ende-Test mit echter PostgreSQL, einschließlich Ergebnisprüfung | 10 aufeinanderfolgende Läufe bestanden |
| Leader-Ausfall während einer Partie und Fortsetzung auf dem neuen Leader | bestanden |
| Leer gestarteter Knoten übernimmt Log und Spielzustand | bestanden |
| Isolierter Leader bestätigt keine neue Aktion und übernimmt nach Rückkehr das aktuelle Log | bestanden |
| Ungültige Aktion oder fehlende Aktions-ID verlängert das Log nicht | bestanden |
| Doppelte Aktion verändert den Spielzustand genau einmal | bestanden |
| Heartbeat bestätigt keinen ungeprüften Log-Rest | bestanden |
| Mehrheit für einen Eintrag eines älteren Terms führt nicht direkt zum Commit | bestanden |
| Leader-Wechsel nach Spielende vor dem Datenbankschreiben | genau eine Partie mit allen Spielerzeilen und korrekten Ergebnissen |
| PostgreSQL nach Aufbau des Pools stoppen, Partie beenden, PostgreSQL starten | Spiel lief weiter, ausstehendes Ergebnis wurde gespeichert |

Die Komponententests in `tests/test_raft_replication.py` verwenden
Test-Doubles für Rolle und Transport. `tests/test_raft_cluster.py` prüft
zusätzlich P3s echte Leader-Wahl mit P4s Replikation.
`tests/test_raft_client.py` deckt Leader-Hinweise, unveränderte Aktions-IDs
und die HTTP-Routen ab. `tests/test_raft_results.py` simuliert Datenbankausfall,
Leader-Wechsel und doppelte Schreibversuche.

Für den Speicherversuch im gemeinsamen `tests/test_e2e.py` wird die Tabelle
`games` vor dem letzten Spielzug exklusiv gesperrt. Der Leader wird nach
Spielende beendet, bevor er das Ergebnis speichern kann. Nach Freigabe der
Sperre schreibt der neue Leader. Geprüft werden der Name des Klopfenden,
die Namen, Punkte und Gewinner aller Spieler, genau eine Partie und
der Lobby-Status `finished`.

Der separate Versuch mit tatsächlichem PostgreSQL-Neustart wurde außerhalb
des Repositories ausgeführt. Seine Beobachtung ergänzt den automatisierten
Komponententest. Weitere gemeinsame Messwerte vom 17.11.2026 stehen in
Abschnitt 4. Die Ergebnisse vom 10.11.2026 beziehen sich auf den damaligen
P4-Prüfstand.

**Nachprüfung am 19.11.2026** in einer temporären Kopie des aktuellen
Dokumentationsbranches `d1cb2a4` mit Python 3.13. Die zwölf Tests in
`test_raft_replication.py`, `test_raft_cluster.py`,
`test_raft_client.py` und `test_raft_results.py` bestanden in 2,93 s.
Diese Nachprüfung verwendet das simulierte Raft-Netz, einen
ASGI-Testtransport und das Datenbank-Test-Double.
