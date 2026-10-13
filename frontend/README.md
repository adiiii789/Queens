# Frontend (Platzhalter)

Browser-Client für Queens (HTML/JavaScript).

Geplant:
- Verbindung per WebSocket zu `ws://<gateway>/ws/<game_id>/<player_id>`
- Senden: Aktionen als JSON im Format von `action_to_dict`, z. B.
  `{"type": "DrawFromDeck", "player_id": "p1", "action_id": "<uuid>"}`
- Empfangen: gefilterte Ereignisse und die eigene Spielersicht (`player_view`)
- Angesehene Karten nur kurz anzeigen (Spiel „wie am echten Tisch“)
- Bei Verbindungsabbruch automatisch neu verbinden; unbestätigte Aktion mit
  gleicher `action_id` erneut senden
