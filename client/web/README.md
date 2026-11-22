# Browser-Client

Gateway (`http://<gateway>:8000/`) und Leitstand (`http://127.0.0.1:8080/`,
`control.py`) liefern diesen Ordner aus. Die Seite fragt `/api/config`: Kommt
sie vom Leitstand, verbindet sie sich mit dem dort genannten Gateway-Port,
sonst mit `ws://<gleicher Host>/ws`. Überschreiben mit `?gateway=host:port`
bzw. `?control=http://host:port`. `?lobby=CODE` tritt nach dem Anmelden der
Lobby bei (Einladungslink).

| Datei | Inhalt |
| --- | --- |
| `protocol.js` | `QueensClient`: Verbindung, Reconnect (1 s → 8 s, `reconnectNow()`), `player_id` im `sessionStorage`, veraltete Sichten verwerfen, je Aktion eine Methode mit eigener `action_id`; `resolveEndpoints()`. Kein DOM, wiederverwendbar. |
| `app.js` | Spieltisch, Lobby, Auflösung, Ereignistexte (wie `client/render.py`), Karten 3 s aufdecken, Tastaturkürzel. |
| `admin.js` | Tab „Server“: Status und Steuerung über die API von `control.py`. |
| `index.html` | Aufbau mit festen IDs (`deck`, `discard`, `opponents`, `my-seat`, …). |
| `style.css` | Gestaltung (Filztisch, gezeichnete Karten, schmale Bildschirme). |

Bedienung: Was gerade anklickbar ist, leuchtet. Stapel anklicken zum Ziehen,
Ablage zum Nehmen bzw. (mit gezogener Karte) zum Ablegen; eigene Karte
anklicken zum Tauschen, zum Ansehen (Startphase: zwei Karten) oder nach
„Zwischenwerfen“ zum Zwischenwerfen. Beim Buben zwei beliebige Karten, beim
König eine. Knöpfe für Ablegen, Überspringen und Klopfen.

Spielregeln stehen hier nicht: Ob eine Aktion gültig ist, entscheidet der
Server, Fehler erscheinen als Hinweis. Die Nachrichten des Protokolls stehen
im Docstring von `services/gateway/app.py`.

Neue Oberfläche bauen: `protocol.js` übernehmen und
`new QueensClient({onMessage, onStatus})` aufrufen, dann auf `welcome`,
`lobby`, `game_started`, `events`, `state`, `info` und `error` reagieren.
