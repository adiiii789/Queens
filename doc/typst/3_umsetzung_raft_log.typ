#import "include/templates.typ": abschnitt_von

=== Game-Service: Log-Replikation und Ergebnisspeicherung <raft-replikation>

Die Leader-Wahl sorgt dafür, dass es einen zuständigen Knoten gibt. Damit ist aber noch keine Spielaktion sicher: Erst die Log-Replikation stellt sicher, dass jede Aktion auf einer Mehrheit der Knoten liegt, bevor sie gilt, und dass alle Knoten die Aktionen in derselben Reihenfolge auf die Spiellogik anwenden. Raft beschreibt dieses Prinzip als replizierte Zustandsmaschine @ongaro2014raft. Dieser Abschnitt beschreibt, wie der Leader Befehle entgegennimmt, verteilt und bestätigt, wie Lobby und Gateway den Cluster ansprechen und wie am Ende einer Partie das Ergebnis in die Datenbank kommt.

#abschnitt_von("Luca")


==== Aufbau

Auch dieser Teil liegt in `services/raft/` und ist auf wenige Dateien verteilt:

- `replication.py` verwaltet das Log, den Commit-Index und je Follower einen Replikator.
- `state_machine.py` verbindet das Log mit der deterministischen Spiel-Engine von Jannis.
- `client.py` enthält den `ClusterClient`, über den Lobby und Gateway den Cluster ansprechen, und `client_routes.py` die zugehörigen HTTP-Endpunkte.
- `results.py` speichert beendete Partien im Hintergrund in PostgreSQL.

Die Schnittstelle zu Gergös Leader-Wahl haben wir vorab gemeinsam festgelegt. Sein Knoten stellt Rolle, Term und Transport bereit, ruft beim Amtsantritt `on_become_leader()` und beim Rücktritt `fail_pending()` auf und liest für die Abstimmung über `last_index` und `term_at(index)` den Stand des Logs. In die andere Richtung meldet die Replikation einen gültigen Leader mit `accept_leader()` und einen höheren Term mit `step_down()`. Für das Check-Quorum hält sie in `peer_last_response` fest, wann jeder Follower zuletzt geantwortet hat. Für die Datenbank nutzt die Ergebnisspeicherung das `ResultRepository` von Adrian.

#abschnitt_von("Luca")


==== Log und Replikatoren

Ein Log-Eintrag besteht aus `term` und `command`. Die Indizes beginnen bei 1; Index 0 mit Term 0 steht für den leeren Anfang des Logs, damit auch der erste Eintrag einen Vorgänger hat, gegen den geprüft werden kann. `commit_index` markiert, bis wohin das Log bestätigt ist, `last_applied`, bis wohin es schon auf die Spiellogik angewendet wurde. Für jeden Follower merkt sich der Leader, welchen Eintrag er als Nächstes schicken muss (`next_index`) und bis wohin der Follower bestätigt hat (`match_index`).

Sobald ein Knoten Leader wird, setzt er diese Werte zurück, hängt einen `NoOp`-Eintrag mit seinem Term an und startet für jeden Follower einen eigenen asynchronen Replikator. Ein Replikator schickt mit jedem `AppendEntries` bis zu 200 Einträge, dazu Index und Term des vorherigen Eintrags und den Commit-Index des Leaders. Kommt ein neuer Befehl hinzu, weckt ein `asyncio.Event` die Replikatoren sofort; passiert nichts, senden sie nach 0,3 s einen leeren Heartbeat.

Der Follower lehnt Nachrichten aus einem älteren Term ab. Sonst erkennt er den Absender als Leader an und prüft, ob der vorherige Eintrag zu seinem eigenen Log passt. Passt er nicht, antwortet er mit einem `conflict_index`, sodass der Leader mehrere Einträge auf einmal zurückspringen kann, statt sich Eintrag für Eintrag heranzutasten. Abweichende, noch nicht bestätigte Einträge schneidet der Follower ab und ersetzt sie durch die des Leaders. Auf diesem Weg holt auch ein Knoten, der mit leerem Log neu startet, das gesamte Log nach.

Eine Feinheit betrifft den Heartbeat: Er darf nur den Teil des Logs bestätigen, der tatsächlich verglichen wurde. Hat ein Follower hinter dieser Stelle noch alte, abweichende Einträge, darf er sie nicht anwenden, nur weil der Commit-Index des Leaders größer ist. Der Follower begrenzt seinen Commit-Index deshalb auf den vorherigen Index plus die Zahl der mitgeschickten Einträge.

#abschnitt_von("Luca")


==== Commit und Verarbeitung von Befehlen

Der Leader bestätigt einen Index, sobald eine Mehrheit einschließlich ihm selbst den Eintrag gespeichert hat und der Eintrag aus dem aktuellen Term stammt. Ältere Einträge werden nur indirekt mitbestätigt, wenn ein späterer Eintrag committet wird. Ohne diese Bedingung könnte ein Eintrag als bestätigt gelten und nach einem weiteren Leader-Wechsel trotzdem überschrieben werden; das Raft-Paper zeigt diesen Fall ausführlich @ongaro2014raft. Der `NoOp` direkt nach der Wahl sorgt dafür, dass es sofort einen Eintrag aus dem aktuellen Term gibt und ältere Einträge nicht liegen bleiben.

Alle Knoten wenden bestätigte Einträge streng in Indexreihenfolge an. Wartet ein Client auf einen Eintrag, bekommt er das Ergebnis über ein Future. Wirft das Anwenden eine Ausnahme, wird sie protokolliert und als Fehler `internal` zurückgegeben; der Knoten arbeitet mit den nächsten Einträgen weiter.

Schreibanfragen laufen über `submit(command)` und werden mit einem `asyncio.Lock` nacheinander abgearbeitet. Bevor ein neuer Befehl geprüft wird, wartet der Leader, bis sein bisheriges Log vollständig angewendet ist. Nach einer Wahl betrifft das den `NoOp`, nach einem Timeout unter Umständen auch einen Befehl, der noch nachträglich committet wurde. Nur so prüft die Zustandsmaschine gegen den wirklich aktuellen Spielstand. Ist der Befehl gültig, wird er angehängt und die Replikatoren werden geweckt. Für den ganzen Ablauf einschließlich der Wartezeit hat der Leader standardmäßig fünf Sekunden.

Fehlt dem Leader die Mehrheit, kann er den Befehl nicht bestätigen, und nach Ablauf der Frist kommt ein `CommitTimeout` zurück. Verliert er seine Rolle, werden die Replikatoren abgebrochen und alle wartenden Anfragen enden mit `NotLeader` und der Adresse des bekannten Leaders.

#abschnitt_von("Luca")


==== Zustandsmaschine und wiederholte Anfragen

Die `GameStateMachine` kennt drei Befehle. `NoOp` ändert nichts. `CreateGame` legt eine Partie mit eindeutiger `game_id` an; wird dieselbe Partie noch einmal angelegt, ist das ebenfalls ein Erfolg. `Action` enthält eine Spielaktion, die nach dem Commit an `queens_engine.apply` geht. Die Antwort enthält die entstandenen Ereignisse mit ihrem Empfängerkreis und die Sicht jedes Spielers mit ihrer `version`; wer welche Teile bekommt, entscheidet danach das Gateway.

Schon vor dem Anhängen prüft die Zustandsmaschine, ob die Partie existiert, die Spielerliste gültig ist und die Aktion den Regeln entspricht. Ungültige Befehle kommen gar nicht erst ins Log. Jede Aktion braucht außerdem eine feste, nicht leere `action_id`. Die Engine merkt sich die bereits verarbeiteten IDs im Spielzustand. Kommt dieselbe Aktion noch einmal, etwa weil ein Client nach einem Timeout wiederholt, gibt es eine Erfolgsmeldung mit der aktuellen Sicht und einer leeren Ereignisliste. Die Aktion wird also nicht zweimal ausgeführt und ihre Ereignisse werden nicht doppelt verteilt.

Fehlt die Aktions-ID, wird der Befehl abgelehnt. Andernfalls würde die Engine beim Einlesen selbst eine UUID erzeugen, und zwar auf jedem Knoten eine andere, sodass die Spielzustände auseinanderlaufen. Aus demselben Grund muss der Seed von `StartGame` eine Zeichenfolge sein und wird als Teil des Befehls mitrepliziert, statt auf den Knoten erzeugt zu werden.

#abschnitt_von("Luca")


==== Cluster-Client und Lesezugriffe

Der `ClusterClient` merkt sich den Knoten, der zuletzt erfolgreich geantwortet hat. Antwortet ein Knoten mit `not_leader`, folgt der Client dem mitgeschickten Leader-Hinweis; bei Verbindungsfehlern probiert er die übrigen Knoten. Ein `timeout` wird genauso behandelt: Der Client versucht es erneut, bis ein Knoten den Befehl bestätigt. Nach insgesamt 15 s gibt er mit `ClusterUnavailable` auf. Die Aktions-ID bleibt über alle Versuche gleich, sodass ein Befehl auch dann nur einmal ausgeführt wird, wenn ein früherer Versuch doch noch committet wurde.

Nach außen bietet jeder Knoten `POST /client/submit` und `GET /client/views/{game_id}` an. `NotLeader` und `CommitTimeout` werden dort auf die vereinbarten JSON-Antworten abgebildet. Auch das Lesen der Sichten läuft über den Leader: Er wartet zunächst, bis sein `NoOp` angewendet ist, und dann auf frische Antworten einer Mehrheit. So kann ein abgeschnittener alter Leader, der sich noch für zuständig hält, keine veraltete Sicht als gültig ausliefern.

#abschnitt_von("Luca")


==== Spielergebnisse und Datenbankausfälle

Der `ResultWriter` läuft als Hintergrundaufgabe in jedem Game-Service, ist aber nur auf dem Leader aktiv. Jede Sekunde durchsucht er den angewendeten Spielzustand nach Partien in der Phase `finished`, die noch nicht gespeichert sind. Vor jeder Partie prüft er erneut Rolle und Term, damit ein gerade abgesetzter Leader nicht weiterschreibt. Was dieser Knoten erfolgreich gespeichert hat, merkt er sich in der Menge `saved`.

Je Partie übergibt er die `game_id`, den Namen des Klopfenden und für jeden Spieler ID, Name, Punkte und ob er gewonnen hat an `ResultRepository.save()`. Adrians Repository schreibt Partie und Spielergebnisse in einer Transaktion und setzt die Lobby auf `finished`. Weil `game_id` eindeutig ist und mit `ON CONFLICT DO NOTHING` gespeichert wird @pg-insert, landet eine Partie auch dann nur einmal in der Datenbank, wenn nach einem Leader-Wechsel der neue Leader sie noch einmal schreibt.

Ist die Datenbank nicht erreichbar, schreibt der Writer eine Warnung ins Log und versucht es eine Sekunde später erneut. Da das Speichern außerhalb des Commit-Pfads liegt, laufen andere Partien währenddessen ungestört weiter. Stürzt der Leader ab, bevor er das Ergebnis geschrieben hat, ist das ebenfalls kein Problem: Der neue Leader hat denselben bestätigten Spielzustand und findet die beendete Partie selbst.

Damit das funktioniert, hat der Game-Service den optionalen Parameter `--db` bekommen, und der Writer wird im Lebenszyklus der FastAPI-Anwendung gestartet und beendet. Beim Herunterfahren wird zuerst der Writer gestoppt und sein Datenbankpool geschlossen, danach Knoten und Transport. `run_local.py` gibt die Datenbankadresse an jeden Raft-Prozess weiter. Fehlt `--db`, läuft der Game-Service ohne Ergebnisspeicherung; so bleiben die Raft-Tests unabhängig von PostgreSQL.

#abschnitt_von("Luca")


==== Tests <raft-replikation-tests>

`tests/test_raft_replication.py` enthält sechs Tests, die die Leader-Rolle über Test-Doubles gezielt steuern. Geprüft wird, dass nach einem Leader-Ausfall die Partie weiterläuft, eine doppelt eingereichte Aktion nur einmal angewendet wird und ein leer neu gestarteter Knoten dasselbe Log und denselben Spielzustand erreicht. Weitere Tests zeigen, dass ein isolierter Leader nichts bestätigen kann und sein abweichendes Log nach der Rückkehr ersetzt wird, dass ungültige Aktionen und Aktionen ohne ID das Log nicht verlängern und dass wartende Anfragen beim Rücktritt sofort enden. Zwei davon sind Regressionstests für die oben beschriebenen Sonderfälle: den ungeprüften Log-Rest beim Heartbeat und die Commit-Regel für Einträge aus älteren Terms.

`tests/test_raft_client.py` prüft, dass der Client Leader-Hinweisen folgt und die Aktions-ID unverändert lässt, und testet die HTTP-Routen über einen ASGI-Testtransport. `tests/test_raft_results.py` spielt eine Partie zu Ende, simuliert einen Datenbankausfall und einen anschließenden Leader-Wechsel und prüft, dass das Ergebnis nachgeholt, doppelte Schreibversuche verworfen und Namen, Punkte und Gewinner richtig übergeben werden. In `tests/test_raft_cluster.py` laufen Gergös echte Knoten mit der Replikation und automatischer Wahl zusammen in einem simulierten Netz.

Der gemeinsame Ende-zu-Ende-Test (`tests/test_e2e.py`) startet Raft-Knoten, Lobby und Gateway als echte Prozesse und spielt mit zwei WebSocket-Clients. Für die Replikation ist dabei wichtig, dass die Partie nach einem Leader-Absturz weiterläuft und der neu gestartete Knoten das Log aufholt. Der schwierigste Fall ist die Ergebnisspeicherung durch einen neuen Leader: Dafür sperrt der Test die Tabelle `games` exklusiv, beendet die Partie und dann den Leader. Erst nach der Freigabe kann der Nachfolger das Ergebnis schreiben. Am Ende muss genau eine Partie mit allen Spielerzeilen, richtigen Punkten und Lobby-Status `finished` in der Datenbank stehen.

Am 10.11.2026 liefen auf macOS (Apple Silicon, Python 3.13) mit einer temporären PostgreSQL-18.6-Instanz alle 123 Tests des damaligen Stands fehlerfrei durch, der vollständige Ende-zu-Ende-Test zehnmal hintereinander. In einem eigenen Versuch wurde PostgreSQL gestoppt, nachdem der Verbindungspool aufgebaut war, dann die laufende Partie beendet und PostgreSQL wieder gestartet; das ausstehende Ergebnis wurde danach gespeichert. Die gemeinsamen Messungen zum Datenbankausfall vom 17.11.2026 stehen in @tab-db-ausfall. Bei der Durchsicht der Dokumentation am 19.11.2026 liefen die zwölf Tests für Replikation, Cluster, Client und `ResultWriter` erneut ohne Fehler.

#abschnitt_von("Luca")
