#import "include/templates.typ": abschnitt_von

== Frontend

Die größte Herausforderung war, ein Spiel mit überwiegend verdeckten Karten verständlich darzustellen, obwohl der Client die Regeln nicht kennt.
Er erhält nur die eigene Sicht und die Ereignisse, daher muss jede Aktion anderer Spielender aus diesen Nachrichten nachvollziehbar werden.
Gelöst wurde das, indem der Client aus Phase und Sicht nur ableitet, was gerade anklickbar ist, und jede Ablehnung des Servers als Hinweis anzeigt.
Verlauf und Hinweiszeile machen den Ablauf zusätzlich nachvollziehbar.
Ein Test (`tests/test_web.py`) stellt sicher, dass es für jedes Ereignis der Spiel-Engine einen Text im Client gibt.

Eine zweite Schwierigkeit waren Verbindungsabbrüche.
Die `player_id` liegt im `sessionStorage` des Browser-Tabs, sodass ein Neuladen direkt zurück in die laufende Partie führt und mehrere Tabs im selben Browser trotzdem verschiedene Spielende sind.
Bricht die Verbindung ab, verbindet sich der Client mit wachsendem Abstand von einer bis acht Sekunden neu.

Mit mehr Zeit würden wir Folgendes anders machen:

- *Automatisierte Browser-Tests.* Die Oberfläche wurde überwiegend von Hand getestet. Tests mit einem gesteuerten Browser würden Fehler in der Bedienung früher finden.
- *Aktionen sichtbarer machen.* Was andere Spielende tun, steht bisher vor allem im Verlauf. Animationen, etwa eine von Stapel zu Hand fliegende Karte, würden den Ablauf am Tisch schneller erfassbar machen.
- *Frühzeitig auf mehreren Rechnern testen.* Probleme wie Firewalls oder Einladungslinks mit `127.0.0.1` fielen erst beim Spielen über mehrere Rechner auf.

#abschnitt_von("Jannis")
