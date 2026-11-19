#import "include/templates.typ": abschnitt_von


Das Frontend ist ein Browser-Client.
Es besteht aus einer HTML-Seite, einem Stylesheet und drei JavaScript-Modulen in `client/web/`.
Das Gateway liefert diese Dateien zusammen mit dem WebSocket-Endpunkt aus, sodass Spielende nur eine Adresse im Browser öffnen müssen.
Spielregeln enthält der Client bewusst nicht: Ob eine Aktion gültig ist, entscheidet ausschließlich der Server.

#abschnitt_von("Jannis")


=== Aufbau

- `protocol.js` kapselt die Verbindung zum Gateway (`QueensClient`). Jede Aktion erhält eine eigene `action_id`, damit Wiederholungen nach einem Verbindungsabbruch nicht doppelt ausgeführt werden. Das Modul enthält keinen DOM-Zugriff und lässt sich daher für andere Oberflächen wiederverwenden.
- `app.js` zeichnet Anmeldung, Lobby, Spieltisch und Auflösung aus den Nachrichten des Gateways.
- `admin.js` bildet den Tab „Server“, über den sich die Dienste am Host-Rechner starten, stoppen und isolieren lassen.

Der Client erhält vom Gateway nur zwei Arten von Nachrichten: Ereignisse, die eine Aktion beschreiben, und die eigene Spielersicht (`player_view`).
Verdeckte Karten anderer Spielender sind in dieser Sicht gar nicht enthalten und können daher auch nicht im Browser ausgelesen werden.
Jede Sicht trägt eine Versionsnummer, veraltete Sichten verwirft der Client.

#abschnitt_von("Jannis")


=== Lobby und Spieltisch

In der Lobby (@frontend-lobby) erstellen Spielende eine Partie oder treten über einen vierstelligen Code bei.
„Link kopieren“ erzeugt einen Einladungslink mit der Netzwerkadresse des Host-Rechners, sodass Mitspielende ohne weitere Einrichtung beitreten können.
Die Partie startet, sobald mindestens zwei Personen da und alle bereit sind.

#figure(
  image("images/frontend_lobby.png", width: 60%),
  caption: [Lobby mit Code, Einladungslink und Bereitschaftsstatus]
) <frontend-lobby>

Am Spieltisch (@frontend-spieltisch) liegen die eigenen Karten unten, die der anderen Spielenden oben.
Alles, was gerade sinnvoll anklickbar ist, leuchtet: Stapel, Ablage und eigene Karten.
Eine Hinweiszeile beschreibt den nächsten Schritt, der Verlauf rechts listet alle Aktionen, private Informationen sind mit einem Schloss markiert.
Angesehene Karten werden nur drei Sekunden aufgedeckt, damit wie am echten Tisch das Merken Teil des Spiels bleibt.
Ein Zeitbalken zeigt die verbleibende Zugzeit, Tastaturkürzel ergänzen die Bedienung mit der Maus.

#figure(
  image("images/frontend_spieltisch.png", width: 100%),
  caption: [Spieltisch aus Sicht eines Spielers mit gezogener Karte]
) <frontend-spieltisch>

#abschnitt_von("Jannis")


=== Leitstand

Der Tab „Server“ (@frontend-leitstand) zeigt den Zustand aller Raft-Knoten (Rolle, Term, Log-Länge, Commit-Index), der Lobby-Instanzen, des Gateways und der Datenbank.
Einzelne Dienste lassen sich dort hart beenden oder vom Netz isolieren, sodass sich Leader-Wahl und Ausfallverhalten während einer laufenden Partie vorführen lassen.
Der Leitstand ist nur auf dem Host-Rechner unter `127.0.0.1` erreichbar.

#figure(
  image("images/frontend_leitstand.png", width: 100%),
  caption: [Leitstand mit Status und Steuerung der Dienste]
) <frontend-leitstand>

#abschnitt_von("Jannis")
