#import "include/templates.typ": abschnitt_von

==== Game-Service: Leader-Wahl und Ausfallerkennung

#abschnitt_von("Gergö")

Der Game-Service hält den Zustand aller laufenden Partien. Damit eine Partie den Ausfall eines Servers übersteht, läuft er als Cluster aus $n$ gleichberechtigten Knoten, die sich mit dem Konsensalgorithmus Raft @ongaro2014raft auf eine gemeinsame Reihenfolge der Spielaktionen einigen. Raft trennt das Problem in Leader-Wahl, Log-Replikation und Sicherheit. Dieser Abschnitt beschreibt die Leader-Wahl und die Ausfallerkennung; die Replikation des Logs ist in einem eigenen Abschnitt beschrieben. Die Clustergröße ist frei wählbar: Weil jede Entscheidung eine Mehrheit von $floor(n slash 2) + 1$ Knoten braucht, verkraftet ein Cluster $floor((n - 1) slash 2)$ Ausfälle, also drei Knoten einen und fünf Knoten zwei.

===== Aufbau

Die Implementierung liegt in `services/raft/` und ist so geschnitten, dass Leader-Wahl und Replikation unabhängig voneinander entwickelt und getestet werden konnten:

- `node.py` enthält den gemeinsamen Zustand eines Knotens (aktueller Term, abgegebene Stimme, Rolle, bekannter Leader) und einen Ticker, der alle 20 ms läuft.
- `election.py` enthält die Wahl, die Abstimmungsregeln und die Prüfung des Quorums.
- `transport.py` kapselt die Kommunikation: `HttpTransport` schickt die RPCs als HTTP-POST zwischen den Prozessen, `InMemoryNetwork` simuliert für Tests ein Netz in einem Prozess, in dem sich Knoten abstürzen, isolieren und wieder verbinden lassen.
- `server.py` stellt einen Knoten als FastAPI-Prozess bereit, mit den Raft-Endpunkten, einer Statusabfrage und einem Admin-Endpunkt für die Ausfall-Demo.

Die Schnittstelle zur Replikation besteht aus wenigen Methoden. Der Knoten fragt den letzten Log-Index und dessen Term ab (für die Abstimmung) und ruft beim Amtsantritt `on_become_leader()` sowie beim Rücktritt `fail_pending()` auf. Umgekehrt meldet die Replikation einen gültigen Leader mit `accept_leader()` und einen höheren Term mit `step_down()`. Diese Methoden wurden vor Beginn der Umsetzung festgelegt; bis die Replikation fertig war, sendete ein Platzhalter (`heartbeat.py`) leere AppendEntries als Heartbeats, sodass die Wahl schon mit leerem Log lief und getestet werden konnte.

===== Ablauf der Wahl

Jeder Knoten startet als Follower. Erhält er innerhalb seines Election-Timeouts keine Nachricht von einem Leader, wird er Candidate: Er erhöht den Term, stimmt für sich selbst und schickt `RequestVote` parallel an alle anderen Knoten. Antwortet eine Mehrheit mit Zustimmung, wird er Leader; meldet eine Antwort einen höheren Term, wird er wieder Follower. Die Zustände und Übergänge zeigt @raft-zustaende.

// Quelltext: images/raft_zustaende.puml (mit PlantUML neu rendern, falls geändert)
#figure(
  image("images/raft_zustaende.svg", width: 85%),
  caption: [Zustände eines Raft-Knotens]
) <raft-zustaende>

Der Election-Timeout wird für jede Wartezeit neu zufällig aus 1,5 bis 3 s gezogen. Ohne diese Streuung würden nach einem Ausfall alle Follower gleichzeitig kandidieren, sich gegenseitig die Stimmen wegnehmen und die Wahl beliebig oft wiederholen. Mit Streuung läuft der Timer eines Knotens meist deutlich früher ab, und dieser gewinnt die Wahl, bevor ein zweiter kandidiert. Zurückgesetzt wird der Timer nur bei einem gültigen AppendEntries und bei einer abgegebenen Stimme, nicht bei jeder beliebigen Nachricht; sonst könnte ein Knoten mit veraltetem Log durch ständige Wahlanfragen eine Neuwahl verhindern.

Ein Knoten stimmt pro Term höchstens einmal ab, und nur für einen Kandidaten, dessen Log mindestens so aktuell ist wie das eigene. Verglichen wird zuerst der Term des letzten Eintrags, bei Gleichheit die Länge. Dadurch kann nur ein Knoten gewinnen, der alle bestätigten Einträge besitzt, denn jeder bestätigte Eintrag liegt auf einer Mehrheit und diese Mehrheit überschneidet sich mit jeder Mehrheit, die ein Kandidat zum Gewinnen braucht @ongaro2014raft. Für den Fall, dass ein Knoten neu startet, sichert `storage.py` Term und Stimme vor jeder Antwort in eine JSON-Datei. Geschrieben wird in eine temporäre Datei, die anschließend per Umbenennen die alte ersetzt, sodass ein Absturz während des Schreibens keine halbe Datei hinterlässt.

===== Check-Quorum

Das Raft-Paper beschreibt, wie ein Follower einen ausgefallenen Leader erkennt, aber nicht, wie ein Leader bemerkt, dass er selbst abgeschnitten ist. Ohne weitere Maßnahme hält sich ein vom Netz getrennter Leader unbegrenzt für zuständig, während die Mehrheit längst einen neuen gewählt hat. Bestätigen kann er zwar nichts mehr, weil ihm die Mehrheit fehlt, aber er nimmt weiterhin Anfragen an, die erst nach Ablauf der Wartezeit scheitern. Deshalb prüft der Leader in jedem Tick, ob ihm innerhalb des maximalen Election-Timeouts eine Mehrheit geantwortet hat. Die Antwortzeitpunkte erfasst die Replikation bei jedem AppendEntries. Fehlt die Mehrheit, tritt er zurück und Anfragen an ihn werden sofort mit dem Hinweis „nicht Leader“ beantwortet. Weil der neue Leader frühestens nach dem minimalen und der alte spätestens nach dem maximalen Election-Timeout wechselt, gibt es kurzzeitig zwei Knoten, die sich für Leader halten. Das ist unkritisch, denn der alte kann ohne Mehrheit nichts bestätigen.

===== Isolation für die Demo

Für die Vorstellung lässt sich ein Knoten mit `POST /admin/isolate` vom Netz trennen. Ein isolierter Knoten sendet keine RPCs mehr, und seine Raft-Endpunkte antworten mit HTTP 503. Damit lässt sich eine Netzwerkpartition zeigen, ohne Firewall-Regeln zu ändern. Die Startskript-Konsole (`run_local.py`) bietet dafür die Befehle `isolate` und `heal`, außerdem `kill` und `start` für Abstürze und `status` für den Zustand aller Knoten.

===== Ergebnisse

@tab-failover zeigt die mit drei und fünf echten Prozessen auf einem Rechner gemessenen Zeiten bei Standardeinstellungen (Election-Timeout 1,5 bis 3 s, Heartbeat 0,3 s). Der neue Leader steht im Mittel nach knapp 2 s fest, was dem Erwartungswert des Timeouts entspricht; ein isolierter Leader tritt wie vorgesehen nach 3 s zurück. Bei $floor((n-1) slash 2)$ Ausfällen wurde jeweils ein Leader gewählt, bei einem weiteren Ausfall nicht mehr.

#figure(
  table(
    columns: 3,
    align: (left, right, right),
    table.header([*Messung*], [*3 Knoten*], [*5 Knoten*]),
    [Erste Wahl nach dem Start], [1,58 s], [1,16 s],
    [Neuer Leader nach Absturz (Mittel, 5 Läufe)], [1,88 s], [1,78 s],
    [Neuer Leader nach Absturz (Bereich)], [1,48–2,69 s], [1,50–2,06 s],
    [Neuer Leader nach Isolation (Mittel, 3 Läufe)], [1,95 s], [1,92 s],
    [Rücktritt des isolierten Leaders (Mittel)], [2,99 s], [2,96 s],
    [Leader bei $floor((n-1) slash 2)$ Ausfällen], [ja (1)], [ja (2)],
    [Leader bei einem Ausfall mehr], [nein], [nein],
  ),
  caption: [Messwerte zur Leader-Wahl (Testprotokoll, 04.11.2026)]
) <tab-failover>

===== Tests

Die Wahl ist mit Unit-Tests über das simulierte Netz abgedeckt (`tests/test_raft.py`, verkürzte Zeiten von 0,15 bis 0,3 s): Es wird genau ein Leader gewählt und bleibt stabil, nach einem Absturz folgt ein neuer in höherem Term, ein isolierter Leader tritt zurück, und eine Minderheitspartition wählt keinen Leader. Ein Test mit mehreren Zufalls-Seeds prüft, dass es in keinem Term zwei Leader gibt. `tests/test_raft_server.py` prüft dieselben Abläufe über die echten HTTP-Endpunkte, `tests/test_raft_cluster.py` die Wahl zusammen mit der Replikation während einer laufenden Partie. Der Ende-zu-Ende-Test (`tests/test_e2e.py`) startet alle Dienste als Prozesse, beendet mitten in der Partie den Leader und prüft, dass der nächste Zug ohne Fehler beim Spieler ankommt.
