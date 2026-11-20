#import "include/templates.typ": abschnitt_von

== Lobby, Gateway und Datenbank <ReflexionLobbyGatewayDatenbank>

Die größte Herausforderung war, eine Partie über mehrere unabhängige Instanzen hinweg genau einmal zu starten. Die naheliegende Lösung, den Zustand der Lobby im Speicher einer Instanz zu halten, hätte den Lobby-Service wieder an einen einzelnen Prozess gebunden. Stattdessen entscheidet die Datenbank: Die Zeilensperre sorgt dafür, dass genau eine Transaktion das Start-Ticket erhält. Schwieriger als erwartet war der Teil danach, denn Datenbank und Game-Service lassen sich nicht in einer Transaktion ändern. Gelöst haben wir das, indem jeder Schritt des Starts idempotent ist und eine andere Instanz einen hängenden Start einfach wiederholen darf. Dasselbe Prinzip trägt das Gateway: Weil jede Aktion eine feste `action_id` hat, sind Wiederholungen nach einem Leader-Wechsel ungefährlich.

Eine zweite Schwierigkeit war das Testen der gleichzeitigen Zugriffe. Wettlaufprobleme treten nur auf, wenn zwei Transaktionen zeitlich eng zusammenfallen, und ein einzelner erfolgreicher Testlauf beweist wenig. Die Tests verwenden deshalb zwei getrennte Verbindungspools gegen eine echte PostgreSQL, statt die Datenbank nachzubilden, und wiederholen kritische Abläufe wie das gleichzeitige Bereitmelden 20-mal. Ebenso wichtig war die Unterscheidung beim Wiederholen von Anfragen im Gateway: Eine Anfrage, die nie angekommen ist, darf an eine andere Instanz gehen, eine abgebrochene Änderung dagegen nicht, weil sie bereits ausgeführt sein könnte.

Mit mehr Zeit würden wir Folgendes anders machen:

- *Gateway redundant auslegen.* Das Gateway läuft als einzelne Instanz und hält die Zuordnung von Spielern zu Partien sowie die Zug-Timer nur im Speicher. Fällt es aus, laufen die Partien im Cluster zwar weiter, aber nach einem Neustart kennt es die Sitzungen nicht mehr, und ohne Timer bleibt eine Partie stehen, wenn ein Spieler nicht zurückkehrt. Abhilfe wäre, die Zuordnung in PostgreSQL abzulegen und mehrere Gateway-Instanzen hinter einem Load-Balancer zu betreiben, die die Partien über den Cluster abgleichen.
- *Benachrichtigungen statt Abfragen.* Hängt eine Lobby in `starting`, fragt das Gateway alle 2 s nach. Mit `LISTEN/NOTIFY` könnte PostgreSQL Änderungen an einer Lobby selbst melden @pg-notify; das spart Anfragen und verkürzt die Wartezeit.
- *Datenbank absichern.* Der Lobby-Service ist nur so verfügbar wie die zentrale PostgreSQL. Für einen echten Betrieb bräuchte sie eine Replikation mit automatischem Umschalten, oder die Lobbys würden ebenfalls in einem Raft-Cluster gehalten.

#abschnitt_von("Adrian")
