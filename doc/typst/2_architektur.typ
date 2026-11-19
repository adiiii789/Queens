#import "include/templates.typ": abschnitt_von

= Architektur

== Allgemein

Das System besteht aus vier Schichten (@architekturmodell): Browser- oder Terminal-Clients, ein Realtime-Gateway als einziger Einstiegspunkt, ein Lobby-Service mit gemeinsamer PostgreSQL-Datenbank und ein Game-Service-Cluster, dessen Knoten sich per Raft @ongaro2014raft auf den Spielverlauf einigen. Alle Dienste sind eigene FastAPI-Prozesse @fastapi, die intern über HTTP mit JSON und nach außen nur über die WebSockets @rfc6455 des Gateways kommunizieren.

#figure(
  image("images/architekturmodell.svg", width: 55%),
  caption: [Architekturmodell]
) <architekturmodell>

Der Aufbau folgt aus wenigen Architekturentscheidungen:

- *Spiellogik als deterministische Zustandsmaschine.* Die Engine berechnet aus Zustand und Aktion den Folgezustand, ohne Netzwerk, Uhrzeit oder freien Zufall (gemischt wird per Seed). Repliziert werden daher nur die Aktionen: Wenden alle Knoten sie in derselben Reihenfolge an, haben sie denselben Spielstand. Zug-Timeouts misst das Gateway und reicht sie als gewöhnliche Aktion ein.
- *Raft für den Spielzustand.* Nur in einer Partie müssen konkurrierende Aktionen, etwa gleichzeitiges Zwischenwerfen, laufend eindeutig geordnet werden. Raft liefert diese Reihenfolge und übersteht den Ausfall einer Minderheit; Konsistenz geht dabei vor Verfügbarkeit, ohne Mehrheit ruht das Spiel. Raft wurde wegen seiner Verständlichkeit gewählt und selbst umgesetzt.
- *Zustandsloser Lobby-Service.* Lobbydaten liegen in PostgreSQL, gleichzeitige Zugriffe regeln Transaktionen. Jede Instanz kann jede Anfrage beantworten, sodass sich der Dienst ohne Konsens vervielfältigen lässt.
- *Ein Gateway.* WebSocket-Verbindungen sind an einen Prozess gebunden. Statt Sitzungen zwischen Gateways abzugleichen, gibt es in Absprache mit dem Dozenten eine Instanz.
- *Idempotenz statt verteilter Transaktionen.* Jede Aktion trägt eine `action_id`, die nur einmal ausgeführt wird. Gateway und Lobby dürfen Anfragen nach einem Leader-Wechsel daher gefahrlos wiederholen.

#abschnitt_von("Gergö")


== Systemkomponenten

- *Clients* (`client/`) zeigen die eigene Spielersicht an und senden Aktionen, ohne Spielregeln zu kennen.
- *Realtime-Gateway* (`services/gateway/`) hält die WebSocket-Sitzungen, verteilt Lobby-Anfragen reihum auf die Lobby-Instanzen, reicht Spielaktionen beim Raft-Leader ein und leitet jedem Spieler nur die für ihn bestimmten Ereignisse und seine eigene Sicht weiter.
- *Lobby-Service* (`services/lobby/`) verwaltet Lobbys, Beitritte und Bereit-Status in PostgreSQL, wo auch die Ergebnisse liegen, und legt beim Spielstart die Partie im Game-Service an. Bei mehreren Game-Service-Clustern verteilt er neue Partien reihum.
- *Game-Service* (`services/raft/`) ist ein Raft-Cluster aus beliebig vielen Knoten. Der Leader nimmt Befehle entgegen, repliziert sie im Log und wendet sie nach dem Commit auf die Engine (`queens_engine/`) an.

Die Komponenten wirken in drei Abläufen zusammen. Im *Lobby-Ablauf* leitet das Gateway Anfragen an eine beliebige Lobby-Instanz weiter, bei einem Ausfall an die nächste. Sobald alle bereit sind, erhält genau eine Instanz per Datenbanksperre das Start-Ticket und schickt `CreateGame` und `StartGame` an den Cluster. Im *Spielablauf* reicht das Gateway jede Aktion über den `ClusterClient` beim Leader ein. Antwortet ein Knoten mit „nicht Leader“, folgt der Client dessen Leader-Hinweis oder probiert den nächsten Knoten. Der Leader antwortet erst, wenn eine Mehrheit die Aktion im Log hat und sie angewendet ist. Ereignisse und Sichten der Antwort verteilt das Gateway gefiltert an die Spieler. Am *Partieende* schreibt der Leader das Ergebnis in die Datenbank; ein doppeltes Speichern nach einem Leader-Wechsel verhindert der Schlüssel `game_id`. Die Datenbank liegt damit nicht im kritischen Pfad einer laufenden Partie.

#abschnitt_von("Gergö")
