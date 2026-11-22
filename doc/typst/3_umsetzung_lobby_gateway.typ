#import "include/templates.typ": abschnitt_von

=== Lobby, Gateway und Datenbank <LobbyGatewayDatenbank>

Bevor eine Partie im Game-Service läuft, müssen sich die Spieler finden, und während der Partie muss jemand die Verbindungen zu den Clients halten. Diese Aufgaben teilen sich drei Komponenten: eine zentrale PostgreSQL-Datenbank für Lobbys und Ergebnisse, ein Lobby-Service aus beliebig vielen gleichberechtigten Instanzen und ein Gateway als einziger Einstiegspunkt der Clients. Ziel war, dass der Ausfall einer Lobby-Instanz für die Spieler unsichtbar bleibt und eine Partie auch dann genau einmal startet, wenn sich die letzten Spieler gleichzeitig über verschiedene Instanzen bereit melden.

#abschnitt_von("Adrian")


==== Aufbau

- `services/schema.sql` und `services/db.py` enthalten das Datenbankschema und den Zugriff darauf. `LobbyRepository` bündelt alle Lobby-Operationen, `ResultRepository` speichert beendete Partien.
- `services/lobby/app.py` ist der Lobby-Service als FastAPI-Anwendung @fastapi mit einer REST-Schnittstelle (`POST /lobbies`, `/join`, `/leave`, `/ready`, `GET /lobbies/{id}` und `/health`).
- `services/gateway/app.py` ist das Gateway mit dem WebSocket-Endpunkt `/ws` @rfc6455. Es liefert außerdem den Browser-Client aus.
- `services/cluster_stub.py` bildet den `ClusterClient` der Log-Replikation nach, wendet die Befehle aber direkt auf die Spiellogik an. Damit ließen sich Lobby und Gateway entwickeln und testen, bevor der Raft-Branch gemergt war. Sobald `services/raft/client.py` vorhanden ist, wird automatisch der echte Client verwendet.

Eine Lobby-Instanz hält keine Lobby-Daten im Speicher; jede Anfrage liest und schreibt direkt in PostgreSQL. Deshalb kann jede Instanz jede Anfrage beantworten, und der Ausfall einer Instanz kostet keine Daten. Im Prozess leben nur die Cluster-Clients, einer je Game-Service-Cluster. Jede Antwort enthält mit `served_by` den Namen der Instanz, sodass in der Demo sichtbar ist, welche Instanz geantwortet hat.

#abschnitt_von("Adrian")


==== Datenbank und gleichzeitige Zugriffe

Das Schema besteht aus vier Tabellen: `lobbies` mit Code, Host, Status, zugeordneter Partie und Cluster, `lobby_players` mit Name und Bereit-Status je Spieler, `games` und `game_results` für die Ergebnisse. Eine Lobby durchläuft die Status `waiting`, `starting`, `started` und `finished`.

Weil mehrere Instanzen gleichzeitig auf dieselben Zeilen zugreifen, läuft jede Operation des Repositorys als eigene Transaktion, die zuerst die Lobby-Zeile mit `SELECT … FOR UPDATE` sperrt @pg-locking. Ohne diese Sperre entsteht beim Bereitmelden ein klassisches Wettlaufproblem: Melden sich die letzten beiden Spieler gleichzeitig über zwei Instanzen bereit, liest jede Transaktion den Stand vor der anderen. Beide sehen dann einen Mitspieler, der noch nicht bereit ist, und keine startet die Partie; oder beide sehen alle bereit, und die Partie startet doppelt. Mit der Sperre wartet die zweite Transaktion, bis die erste abgeschlossen ist. Da PostgreSQL in der Standardisolation _Read Committed_ jeder Anweisung einen neuen Schnappschuss gibt @pg-isolation, sieht die anschließende Abfrage der Spieler bereits den Bereit-Status der ersten Transaktion. Genau eine der beiden stellt fest, dass nun alle bereit sind, setzt die Lobby auf `starting`, vergibt `game_id` und Cluster und erhält als einzige ein `StartTicket`. Nach demselben Muster prüft `join` die Höchstzahl von acht Spielern, sodass auch viele gleichzeitige Beitritte die Grenze nicht überschreiten.

Weitere Stellen nutzen Eigenschaften der Datenbank, statt eigene Sperren einzuführen:

- *Lobby-Codes* bestehen aus vier Zeichen eines Alphabets ohne die verwechselbaren Zeichen 0/O und 1/I, also $32^4 approx 10^6$ möglichen Codes. Ein Code wird mit `INSERT … ON CONFLICT DO NOTHING RETURNING` angelegt @pg-insert; kommt keine Zeile zurück, war er vergeben, und es wird neu gewürfelt.
- *Cluster-Verteilung:* Neue Partien werden reihum über die Sequenz `cluster_rr` auf die Game-Service-Cluster verteilt. Eine Sequenz liefert auch bei gleichzeitigen Aufrufen nie zweimal denselben Wert und braucht deshalb keine Sperre.
- *Ergebnisse* werden mit `ON CONFLICT DO NOTHING` auf den Schlüssel `game_id` gespeichert. Speichern nach einem Leader-Wechsel der alte und der neue Leader dieselbe Partie, steht sie trotzdem genau einmal in der Datenbank.
- *Schema-Anlage:* Jede Instanz führt beim Start `schema.sql` aus. `CREATE TABLE IF NOT EXISTS` ist aber nicht sicher, wenn mehrere Verbindungen es gleichzeitig ausführen, und kann dann mit einer Schlüsselverletzung abbrechen. Deshalb läuft die Anlage unter einem Advisory-Lock @pg-locking, sodass auch $n$ gleichzeitig startende Instanzen sauber hochfahren.

Die Verbindungen verwaltet ein asyncpg-Pool @asyncpg mit ein bis fünf Verbindungen. Er wird erst bei der ersten Anfrage angelegt; schlägt das fehl, versucht es die nächste Anfrage erneut. Eine Lobby-Instanz startet also auch, wenn die Datenbank noch nicht läuft, und verbindet sich nach einem Datenbankausfall selbstständig neu. Solange die Datenbank fehlt, beantwortet sie Anfragen mit HTTP 503 und `GET /health` meldet `db: false`.

#abschnitt_von("Adrian")


==== Spielstart genau einmal

Die Instanz mit dem Start-Ticket legt die Partie im zugeordneten Cluster an (`CreateGame` mit der Spielerliste), startet sie (`StartGame` mit zufälligem Seed) und setzt die Lobby auf `started`. Die Antwort enthält die Adressen der Cluster-Knoten, über die das Gateway die Partie anschließend direkt anspricht.

Datenbank und Cluster lassen sich nicht in einer gemeinsamen Transaktion ändern. Stürzt eine Instanz zwischen dem Ticket und dem Start ab, bliebe die Lobby für immer in `starting`. Gelöst ist das über Idempotenz: `CreateGame` ist je `game_id` idempotent, und `StartGame` trägt die feste `action_id` `<game_id>-start`, die die Zustandsmaschine nur einmal ausführt. Beide Schritte dürfen also gefahrlos wiederholt werden. Fragt jemand eine Lobby ab, die länger als 15 s in `starting` hängt, übernimmt die antwortende Instanz den Start mit `claim_stale_start`. Diese Methode setzt unter derselben Zeilensperre den Zeitstempel neu, sodass auch hier nur eine Instanz übernimmt. Damit die Abfrage tatsächlich kommt, fragt das Gateway bei Lobbys in `starting` alle 2 s nach.

Ist der Cluster beim Start nicht erreichbar, setzt `abort_start` die Lobby auf `waiting` zurück und nimmt allen Spielern den Bereit-Status. Die Spieler erhalten die Meldung „Spielserver nicht erreichbar. Bitte erneut bereit melden.“ und können es später noch einmal versuchen, statt in einer scheinbar startenden Lobby zu warten.

#abschnitt_von("Adrian")


==== Gateway

Das Gateway hält die WebSocket-Verbindungen aller Clients. Es läuft als einzelne Instanz, wie mit dem Dozenten abgestimmt. Eine WebSocket-Verbindung ist an einen Prozess gebunden, und mehrere Gateways müssten ihre Sitzungen untereinander abgleichen; der Schwerpunkt der Ausfallsicherheit liegt deshalb beim Game-Service und beim Lobby-Service. Die erste Nachricht eines Clients muss `hello` mit Name und optionaler `player_id` sein. Fehlt die ID, vergibt das Gateway eine neue. Zu jeder ID gibt es eine Sitzung mit Verbindung, Lobby, Partie und einem Sende-Lock. Der Lock ist nötig, weil mehrere Tasks gleichzeitig an denselben Client senden können, etwa die Aktion eines Mitspielers und ein ablaufender Zug-Timer.

*Reconnect.* Meldet sich ein Client mit einer bekannten `player_id`, ersetzt die neue Verbindung die alte. Die Mitspieler erhalten einen Hinweis, und der Spieler bekommt die Lobby oder seine aktuelle Spielsicht neu, die das Gateway vom Cluster abfragt. So überstehen Clients kurze Netzunterbrechungen und auch einen Neustart, ohne die Partie zu verlassen.

*Lobby-Anfragen* verteilt der `LobbyPool` reihum auf alle Instanzen. Kommt keine Verbindung zustande, nimmt er die nächste Instanz und überspringt die ausgefallene für 5 s. Wiederholt wird dabei nur, wenn die Anfrage sicher nicht angekommen ist: Bei einem Verbindungsfehler ist das der Fall, bei einem Abbruch nach dem Senden nicht, denn ein `join` oder `ready` könnte schon ausgeführt sein. Solche Anfragen wiederholt das Gateway nur bei lesenden `GET`-Anfragen und meldet sonst einen Fehler an den Absender.

*Spielaktionen* reicht das Gateway über `ClusterClient.submit` beim Raft-Leader ein. Die `player_id` setzt es immer aus der Sitzung, sodass kein Client im Namen eines anderen handeln kann, und ergänzt eine `action_id`, falls sie fehlt. Mit ihr kann der Cluster-Client eine Aktion nach einem Leader-Wechsel wiederholen, ohne dass sie doppelt ausgeführt wird. Eine abgelehnte Aktion geht als `error` nur an den Absender.

*Verteilung.* Die Antwort des Clusters enthält die neuen Ereignisse und für jeden Spieler seine eigene Sicht. Jeder Spieler erhält nur die Ereignisse, deren `audience` leer ist oder seine ID trägt, und nur seine eigene Sicht; fremde Kartenwerte verlassen das Gateway also nie. Weil Antworten gleichzeitiger Aktionen in anderer Reihenfolge ankommen können, als der Cluster sie bestätigt hat, verwirft das Gateway Sichten mit kleinerer `version` als der zuletzt gesendeten.

*Zug-Timeouts.* Die Spiellogik ist eine deterministische Zustandsmaschine und darf deshalb keine Uhr lesen; sonst kämen die Knoten beim Abspielen des Logs zu unterschiedlichen Ergebnissen. Die Zeit misst daher das Gateway: 60 s für das Ansehen der Startkarten, 60 s pro Zug und 15 s, wenn der Spieler am Zug getrennt ist. Läuft ein Timer ab, reicht das Gateway eine gewöhnliche `TurnTimeout`-Aktion mit der Zugnummer und einer festen `action_id` ein. Sie läuft wie jede andere Aktion durch das Log; ist der Zug inzwischen vorbei, lehnt die Zustandsmaschine sie ab. Der Timer wird nur bei einem neuen Zug gestellt, damit Zwischenschritte im selben Zug die Zeit nicht verlängern. Ist der Cluster gerade ohne Leader, wiederholt das Gateway die Aktion alle 2 s.

#abschnitt_von("Adrian")


==== Verhalten bei Ausfällen

@tab-ausfall-lobby fasst zusammen, wie sich Ausfälle in diesem Teil des Systems auswirken und wo das Verhalten geprüft wird.

#figure(
  table(
    columns: (auto, 1fr, auto),
    align: (left, left, left),
    table.header([*Ausfall*], [*Verhalten*], [*Nachweis*]),
    [Lobby-Instanz], [Gateway nimmt die nächste Instanz; für die Spieler unsichtbar], [Test, E2E],
    [Lobby-Instanz während des Starts], [andere Instanz übernimmt den Start nach 15 s], [Test],
    [Cluster beim Start nicht erreichbar], [Lobby wieder offen, Meldung an die Spieler], [Test],
    [Raft-Leader während der Partie], [kurze Verzögerung, die WebSocket-Verbindung bleibt], [E2E],
    [Verbindung eines Clients], [Zug nach 15 s übersprungen; Reconnect bringt die Sicht zurück], [Test, E2E],
    [PostgreSQL], [Lobby-Anfragen mit HTTP 503; laufende Partien sind nicht betroffen, das Ergebnis wird nachgeholt], [Messung],
    [Gateway], [Partien bleiben im Cluster erhalten, Verbindungen gehen verloren (siehe Reflexion)], [–],
  ),
  caption: [Verhalten von Lobby-Service und Gateway bei Ausfällen]
) <tab-ausfall-lobby>

#abschnitt_von("Adrian")


==== Ergebnisse

Den Ausfall der Datenbank haben wir mit allen Diensten als echten Prozessen gemessen (drei Raft-Knoten, zwei Lobby-Instanzen, Gateway). In drei Läufen wurde PostgreSQL mitten in einer Partie gestoppt, die Partie zu Ende gespielt und die Datenbank nach gut 10 s wieder gestartet; die Werte zeigt @tab-db-ausfall. Laufende Partien bemerken den Ausfall nicht, weil das Gateway während der Partie nur mit dem Raft-Cluster spricht. Neue Lobbys werden mit einer verständlichen Meldung abgelehnt. Nach dem Wiederanlauf holt der Leader das Ergebnis innerhalb von höchstens 1 s nach, was seinem Wiederholungstakt entspricht, und es steht in jedem Lauf genau einmal in der Datenbank. Fällt die Datenbank aus, während sich die Spieler bereit melden, erhält nur der Absender einen Fehler, und die Lobby bleibt unverändert; nach dem Wiederanlauf startet die Partie beim nächsten Bereitmelden.

#figure(
  table(
    columns: 4,
    align: (left, right, right, right),
    table.header([*Messung*], [*Lauf 1*], [*Lauf 2*], [*Lauf 3*]),
    [Lobby meldet Datenbank als nicht erreichbar], [0,02 s], [0,02 s], [0,03 s],
    [Zug in laufender Partie während des Ausfalls], [0,06 s], [0,03 s], [0,03 s],
    [Fehler bei den Spielern während der Partie], [keine], [keine], [keine],
    [Ergebnis gespeichert nach Wiederanlauf], [0,97 s], [0,44 s], [0,02 s],
    [Partie in `games` / Spieler in `game_results`], [1 / 2], [1 / 2], [1 / 2],
    [Neue Lobby nach Wiederanlauf], [ok], [ok], [ok],
  ),
  caption: [Messwerte zum Datenbankausfall (Testprotokoll, 17.11.2026)]
) <tab-db-ausfall>

Nicht gemessen ist eine Netzwerktrennung, bei der die Verbindungen zur Datenbank nicht abgebaut werden. Dann greifen erst die Timeouts des Pools (3 s für den Verbindungsaufbau, 5 s je Befehl), und Lobby-Anfragen würden entsprechend später mit HTTP 503 beantwortet.

#abschnitt_von("Adrian")


==== Tests

Die Datenbanktests (`tests/test_db.py`) laufen gegen eine echte Testdatenbank `queens_test` und verwenden zwei getrennte Pools, die für zwei Lobby-Instanzen stehen. Geprüft werden Anlegen, Beitreten, Verlassen und Host-Wechsel, dass gleichzeitiges Bereitmelden über beide Pools in 20 Wiederholungen jeweils genau ein Start-Ticket ergibt, dass 14 gleichzeitige Beitritte zu genau acht Spielern führen, die Verteilung auf Cluster, die Übernahme eines hängenden Starts und das einmalige Speichern eines Ergebnisses. `tests/test_lobby.py` startet zwei Lobby-Instanzen gegen dieselbe Datenbank und prüft, dass beide dieselbe Lobby liefern, dass gleichzeitiges Bereitmelden die Partie genau einmal startet und dass ein nicht erreichbarer Cluster die Lobby wieder öffnet. `tests/test_gateway.py` spielt über echte WebSocket-Verbindungen eine komplette Partie und prüft dabei, dass kein Client fremde Ereignisse oder Karten erhält und eine gefälschte `player_id` abgelehnt wird. Weitere Tests decken den Ausfall einer Lobby-Instanz, den Reconnect, das Überspringen eines getrennten Spielers und den Zug-Timeout ab. Ist keine Datenbank erreichbar, überspringen sich diese Tests, damit die übrigen überall laufen. Mit PostgreSQL 18 liefen alle 17 Tests fünfmal hintereinander fehlerfrei, der Ende-zu-Ende-Test einschließlich der Prüfung des gespeicherten Ergebnisses zehnmal.

#abschnitt_von("Adrian")
