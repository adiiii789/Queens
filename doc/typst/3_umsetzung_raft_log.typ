#import "include/templates.typ": abschnitt_von

#pagebreak(weak: true)

#[
#set text(font: "Arial", size: 11pt)
#set par(leading: 0.65em, spacing: 0.65em, justify: true)

=== Game-Service - Log-Replikation und Ergebnisspeicherung <p4-replikation>

P4 umfasst die geordnete Verarbeitung der Spielbefehle im Raft-Cluster und die
Speicherung abgeschlossener Partien. Der Leader nimmt Befehle entgegen,
repliziert sie auf die Follower und liefert das Ergebnis nach dem Commit und
dem Anwenden auf die Spiellogik zurück. Die Umsetzung folgt dem Prinzip der
replizierten Zustandsmaschine aus @ongaro2014raft. Die Ergebnisspeicherung aus
Commit `e243e20` ist inzwischen auch in `develop` enthalten.

#abschnitt_von("Luca Hertfelder (P4)")

==== Komponenten und Schnittstellen

Die P4-Implementierung liegt in `services/raft/`. `replication.py` verwaltet
Log, Commit-Index und Replikatoren. `state_machine.py` verbindet das Log mit
P1s deterministischer Queens-Engine. `client.py` stellt den gemeinsamen
`ClusterClient` für Lobby und Gateway bereit, und `client_routes.py` enthält
die HTTP-Endpunkte für Schreib- und Leseanfragen. `results.py` übernimmt die
Ergebnisspeicherung als Hintergrundaufgabe.

P3s Knoten stellt Rolle, Term und Transport bereit. Beim Amtsantritt ruft er
`on_become_leader()` auf, beim Verlust der Leader-Rolle `fail_pending()`.
Für Wahlanfragen liest er über `last_index` und `term_at(index)` den
Log-Stand. Die Replikation meldet gültige Leader-Nachrichten über
`accept_leader()` und höhere Terms über `step_down()`. Für Check-Quorum
führt sie die Antwortzeitpunkte je Follower in `peer_last_response`.
P2s `ResultRepository` bildet die Schnittstelle zur PostgreSQL-Datenbank.

==== Log und Replikatoren

Jeder Log-Eintrag enthält `term` und `command`. Die Indizes beginnen bei 1,
Index 0 mit Term 0 bezeichnet den virtuellen Anfang des Logs. `commit_index`
markiert den bestätigten Bereich, `last_applied` den bereits angewendeten.
Für jeden Follower hält der Leader einen nächsten Sendeindex
(`next_index`) und den zuletzt bestätigten Index (`match_index`).

Nach der Wahl initialisiert der Leader diese Werte, hängt einen `NoOp` mit
seinem aktuellen Term an und startet je Follower einen asynchronen Replikator.
Ein Replikator sendet höchstens 200 Einträge je `AppendEntries`, zusammen
mit Index und Term des vorherigen Eintrags sowie dem Commit-Index des Leaders.
Neue Befehle wecken die Tasks über `asyncio.Event`. Im Leerlauf warten sie
standardmäßig 0,3 s und senden anschließend einen Heartbeat.

Der Follower lehnt Nachrichten aus einem kleineren Term ab. Andernfalls
übernimmt er den Leader und prüft, ob der vorherige Eintrag zu seinem Log
passt. Bei einem Widerspruch liefert er einen `conflict_index`, bis zu dem
der Leader zurückspringen kann. Abweichende, unbestätigte Einträge werden
abgeschnitten und durch die Einträge des Leaders ersetzt. Ein leer neu
gestarteter Knoten kann so das gesamte Log nachholen.

Ein Heartbeat darf nur den tatsächlich verglichenen Log-Bereich bestätigen.
Der Follower begrenzt seinen neuen Commit-Index deshalb auf den vorherigen
Index zuzüglich der Zahl empfangener Einträge. Ein noch ungeprüftes,
abweichendes Log-Ende wird dadurch vorzeitigem Anwenden entzogen.

==== Commit-Regel und Verarbeitung

Ein Index darf auf dem Leader direkt bestätigt werden, wenn ihn eine
Mehrheit einschließlich des Leaders repliziert hat und der Eintrag aus dem
aktuellen Term stammt. Frühere Einträge werden über einen späteren Commit
mitbestätigt. Diese Term-Bedingung verhindert die im Raft-Paper erläuterte
Fehlentscheidung nach Leader-Wechseln. Der `NoOp` schafft unmittelbar nach
dem Amtsantritt einen Eintrag des aktuellen Terms. @ongaro2014raft

Alle Knoten wenden bestätigte Einträge in Indexreihenfolge an. Ein wartender
Client erhält das Ergebnis des zugehörigen Eintrags über ein Future.
Ausnahmen werden protokolliert und als `internal` gemeldet, während der
Knoten weitere Einträge verarbeiten kann.

`submit(command)` serialisiert Schreibanfragen mit einem
`asyncio.Lock`. Vor der Vorabprüfung wartet es, bis das bisherige Log
angewendet ist. Das umfasst nach einer Wahl den `NoOp` und nach einem
Timeout eventuell noch ausstehende Befehle. Anschließend prüft die
Zustandsmaschine den neuen Befehl, hängt ihn bei Erfolg an und weckt die
Replikatoren. Standardmäßig stehen für diesen Ablauf einschließlich des
Wartens höchstens fünf Sekunden zur Verfügung.

Geht die Mehrheit verloren, kann der Leader einen neuen Befehl nicht
bestätigen. Bei Ablauf der Frist entsteht ein `CommitTimeout`. Bei einem
Rollenwechsel werden die Replikatoren abgebrochen und wartende Aufrufe
beenden sich mit `NotLeader` und der bekannten Leader-Adresse.

==== Zustandsmaschine und wiederholte Anfragen

`GameStateMachine` verarbeitet drei Befehlstypen. `NoOp` verändert kein
Spiel. `CreateGame` legt eine Partie mit eindeutiger `game_id` an,
wiederholtes Anlegen derselben Partie liefert Erfolg.
`Action` deserialisiert eine Spielaktion und übergibt sie nach dem Commit an
`queens_engine.apply`. Die Antwort enthält Ereignisse mit Empfängerkreis und
die getrennten Sichten aller Spieler mit ihrer `version`. Das Gateway
übernimmt anschließend die Verteilung.

Die Vorabprüfung erkennt unbekannte Partien, ungültige Spielerlisten und
Regelverstöße. Solche Befehle verlängern das Log nicht. Jede Aktion muss
eine feste, nicht leere `action_id` besitzen. Die Engine führt bereits
verarbeitete IDs im Spielzustand mit. Eine erneute Anfrage liefert Erfolg,
die aktuelle Sicht und eine leere Ereignisliste. So entstehen weder ein
zweiter Zustandsübergang noch erneut ausgelieferte Ereignisse.

Eine fehlende Aktions-ID wird vor dem Anhängen zurückgewiesen, weil die
Engine sonst beim Deserialisieren lokal eine UUID erzeugen könnte.
Unterschiedliche IDs auf den Knoten würden den replizierten Zustand
auseinanderlaufen lassen. Auch der Seed von `StartGame` wird als
Zeichenfolge geprüft und als Teil des Befehls repliziert.

==== Cluster-Client und Lesezugriffe

Der `ClusterClient` merkt sich die zuletzt erfolgreiche Knotenadresse,
folgt einem `not_leader`-Hinweis und probiert bei Verbindungsproblemen
weitere konfigurierte Knoten. Bei `timeout` versucht er denselben Befehl
erneut. Nach insgesamt 15 s meldet er `ClusterUnavailable`.
Die Aktions-ID bleibt über alle Versuche erhalten.

Die HTTP-Anbindung stellt `POST /client/submit` und
`GET /client/views/{game_id}` bereit. `NotLeader` und
`CommitTimeout` werden auf die vereinbarten JSON-Antworten abgebildet.
Ein Lesezugriff wartet zunächst auf den angewendeten `NoOp` und
anschließend auf frische Antworten einer Mehrheit. Ein abgetrennter
bisheriger Leader kann dadurch keine veraltete Sicht als erfolgreiche
Leseantwort ausgeben.

==== Spielergebnisse und Datenbankausfälle

Der `ResultWriter` durchsucht den angewendeten Spielzustand nach Partien
mit Phase `finished`. Er prüft jeweils Rolle und Term und schreibt während
der Leader-Rolle die noch ausstehenden Ergebnisse. Eine lokale Menge
`saved` verhindert wiederholte Schreibversuche desselben Knotens nach
erfolgreicher Speicherung. Zwischen den Durchläufen wartet der Task eine
Sekunde.

Pro Partie übergibt er `game_id`, den Namen des Klopfenden und je Spieler
ID, Name, Punkte und Gewinnerstatus an `ResultRepository.save()`. P2s
Repository schreibt Partie und Spielergebnisse in einer Transaktion und
setzt den Lobby-Status auf `finished`. Die Eindeutigkeit von `game_id`
und `ON CONFLICT DO NOTHING` fangen erneute Schreibversuche nach einem
Leader-Wechsel ab. @pg-insert

Bei einer nicht erreichbaren Datenbank protokolliert der Writer eine
Warnung. Die Partie bleibt ausstehend und wird nach der nächsten
Wartephase erneut versucht. Die Datenbankoperation liegt außerhalb des
Commit-Pfads, sodass laufende Partien währenddessen weiterlaufen können.
Ein neuer Leader besitzt denselben bestätigten Spielzustand und findet
die beendete Partie auch dann, wenn der vorherige Leader vor dem Schreiben
abgestürzt ist.

P4 ergänzt im Game-Service den optionalen Parameter `--db` und die
Verwaltung des Writers im FastAPI-Lebenszyklus. Beim Herunterfahren wird
zuerst der Writer beendet und sein Datenbankpool geschlossen, anschließend
werden Knoten und Transport beendet. Das Startskript `run_local.py`
reicht die konfigurierte Datenbankadresse an jeden Raft-Prozess weiter.
Bei direktem Start ohne `--db` wird der Writer ausgelassen.

==== Tests und Nachweise <p4-tests>

Die sechs Tests in `test_raft_replication.py` steuern die Leader-Rolle
gezielt über Test-Doubles. Sie prüfen übereinstimmende Logs und
Spielzustände, Leader-Ausfall mit Fortsetzung der Partie, einen leeren
Neustart, Isolation und Korrektur eines abweichenden Logs, ungültige und
doppelte Aktionen sowie das Aufwecken wartender Anfragen beim Rücktritt.
Zwei gezielte Regressionstests decken den ungeprüften Log-Rest bei einem
Heartbeat und die Commit-Regel für ältere Terms ab.

`test_raft_client.py` prüft Leader-Hinweise, unveränderte Aktions-IDs und
die HTTP-Routen über einen ASGI-Testtransport.
`test_raft_results.py` simuliert einen Datenbankausfall und einen
Leader-Wechsel. Es kontrolliert die Wiederholung, das Verwerfen doppelter
Schreibversuche sowie Namen, Punkte und Gewinner.
Die gemeinsamen Tests in `test_raft_cluster.py` verwenden P3s echte
Knoten mit P4s Replikation und automatischer Wahl in einem simulierten Netz.

Der gemeinsame Ende-zu-Ende-Test startet Raft, Lobby und Gateway als
Prozesse und verbindet zwei WebSocket-Clients. Der P4-Beitrag prüft die
Fortsetzung nach einem Leader-Absturz, das Nachholen des Logs und die
Ergebnisspeicherung durch den neuen Leader. Für den letzten Fall hält der
Test eine exklusive Sperre auf der Tabelle `games`, beendet die Partie
und anschließend den Leader. Erst nach Freigabe der Tabelle kann dessen
Nachfolger das Ergebnis schreiben. Geprüft werden genau eine Partie,
sämtliche Spielerzeilen, die Ergebniswerte und der Lobby-Status.

Im Prüfstand vom 10.11.2026 auf macOS mit Apple Silicon, Python 3.13 und
einer temporären PostgreSQL-18.6-Instanz bestanden alle 123 Tests des
P4-Stands `e243e20`. Der vollständige Ende-zu-Ende-Test war zehnmal
hintereinander erfolgreich. Ein separater Versuch stoppte PostgreSQL
nach Aufbau des Verbindungspools, beendete die laufende Partie und
startete PostgreSQL erneut. Anschließend wurde das ausstehende Ergebnis
gespeichert. Die späteren gemeinsamen Messungen zum Datenbankausfall vom
17.11.2026 sind im Testprotokoll und in @tab-db-ausfall dokumentiert.

Bei der Dokumentationsprüfung am 19.11.2026 bestanden die zwölf Tests
für Replikation, Cluster-Anbindung, Client und ResultWriter erneut.

#abschnitt_von("Luca Hertfelder (P4)")

]
