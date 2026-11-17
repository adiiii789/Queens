# Browser-Client (funktional)

Das Gateway liefert diesen Ordner unter `/` aus: `http://<gateway>:8000/`.
Die Seite verbindet sich selbst mit `ws://<gleicher Host>/ws`, es ist keine
Adresse einzugeben. Die Gestaltung fehlt noch bewusst. Alles ist bedienbar,
damit später eine richtige Oberfläche darauf aufsetzen kann.

| Datei | Inhalt |
| --- | --- |
| `protocol.js` | `QueensClient`: Verbindung, Reconnect (1 s → 8 s), `player_id` im `sessionStorage`, veraltete Sichten verwerfen, je Aktion eine Methode mit eigener `action_id`. Kein DOM, wiederverwendbar. |
| `app.js` | Anzeige und Klicks, Ereignistexte (wie `client/render.py`), Karten 3 s aufdecken. |
| `index.html` | Aufbau mit festen IDs (`deck`, `discard`, `players`, …), die `app.js` nutzt. |
| `style.css` | nur das Nötigste |

Bedienung: Stapel anklicken zum Ziehen; eigenen Slot anklicken zum Tauschen
(nach dem Ziehen), zum Ansehen (Startphase: zwei Slots) oder nach dem Knopf
„Zwischenwerfen“ zum Zwischenwerfen. Beim Buben zwei beliebige Slots, beim
König einen. Knöpfe für Ablegen, Überspringen und Klopfen.

Spielregeln stehen hier nicht: Ob eine Aktion gültig ist, entscheidet der
Server, Fehler erscheinen als Hinweis. Die Nachrichten des Protokolls stehen
im Docstring von `services/gateway/app.py`.

Neue Oberfläche bauen: `protocol.js` übernehmen und
`new QueensClient({onMessage, onStatus})` aufrufen, dann auf `welcome`,
`lobby`, `game_started`, `events`, `state`, `info` und `error` reagieren.
