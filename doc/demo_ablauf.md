# Demo-Ablauf für die Vorstellung

Alles läuft über ein Terminal mit `run_local.py`; weitere Terminals braucht es
nur für die Clients. Voraussetzung: PostgreSQL ist eingerichtet (siehe README).

## Vorbereitung

```bash
python run_local.py                 # 3 Raft-Knoten, 2 Lobby-Instanzen, Gateway
python run_local.py --nodes 5 --lobbies 3   # größerer Cluster
python run_local.py --public        # Clients auf anderen Rechnern
```

Das Skript prüft zuerst die Datenbank, startet alle Dienste als eigene Prozesse
(Logs in `logs/`), wartet auf einen Leader und zeigt den Status. Danach steht die
Admin-Konsole `queens>` bereit.

| Befehl                 | Wirkung                                                   |
|------------------------|-----------------------------------------------------------|
| `status`               | Rolle, Term, Leader, Log, Commit, Partien je Knoten; Lobbys; Gateway; DB |
| `leader`               | nur den aktuellen Leader                                  |
| `kill <name>`          | Prozess hart beenden (Absturz): `kill 2`, `kill l1`       |
| `start <name>`         | beendeten Prozess neu starten                             |
| `isolate <n>` / `heal <n>` | Raft-Knoten vom Netz trennen bzw. wieder verbinden    |
| `ergebnisse`           | letzte Partien aus PostgreSQL                             |
| `log <name> [zeilen]`  | Ende einer Log-Datei: `log 1`, `log l2 40`, `log gateway` |
| `quit`                 | alle Prozesse beenden                                     |

## Ablauf (etwa 5 Minuten)

1. **Partie starten.** Zwei bis vier Clients verbinden sich mit dem Gateway,
   einer legt eine Lobby an, die anderen treten mit dem Code bei, alle melden
   sich bereit. In der Konsole: `status` – der Leader hat jetzt eine Partie,
   Log-Länge und Commit-Index sind auf allen Knoten gleich.
2. **Leader beenden.** `kill <leader>`. Die Konsole meldet nach knapp 2 s den
   neuen Leader in einem höheren Term. Die Spieler ziehen weiter; die
   WebSocket-Verbindung bleibt bestehen, ein Zug dauert höchstens kurz länger.
3. **Alten Knoten neu starten.** `start <n>`, dann `status`: Der Knoten ist
   Follower und hat nach kurzer Zeit dieselbe Log-Länge wie der Leader.
4. **Leader isolieren.** `isolate <leader>`. Die Mehrheit wählt neu; der
   isolierte Knoten hält sich noch bis zu 3 s für Leader und tritt dann
   zurück (`status` zeigt „follower (isoliert)“). Weiterspielen klappt.
   `heal <n>` verbindet ihn wieder, er übernimmt das neue Log.
5. **Mehrheit verlieren (optional).** Bei 3 Knoten zwei beenden: `status`
   zeigt „keiner bestätigt“, Aktionen der Spieler schlagen mit einer Meldung
   fehl. Nach `start` ist der Cluster wieder handlungsfähig.
6. **Lobby-Instanz beenden.** `kill l1`, dann eine neue Lobby anlegen – das
   Gateway weicht unbemerkt auf `l2` aus.
7. **Reconnect.** Einen Client mit Strg+C beenden und mit `--player-id` neu
   starten: Er ist sofort wieder in der Partie und sieht den aktuellen Tisch.
8. **Ergebnisse.** Partie mit Klopfen beenden, dann `ergebnisse`. (Gespeichert
   wird erst, wenn `services/raft/results.py` aus feature/Raft_Log&Consistency
   Teil 2 da ist.)
9. `quit` – es bleiben keine Prozesse zurück.
