# Raft-Replikation und Ergebnisspeicherung

Luca Hertfelder (P4)

## Architektur und Umsetzung

Der Game-Service hält für jede Partie eine deterministische Zustandsmaschine.
Eine Aktion wird vorab gegen den bestätigten Spielzustand geprüft. Der Leader
hängt gültige Befehle mit dem aktuellen Term an sein Log an. Pro Follower läuft
ein Replikator, der bis zu 200 Einträge auf einmal sendet. Der Follower prüft
Index und Term des vorherigen Eintrags und ersetzt bei Bedarf sein abweichendes,
noch unbestätigtes Log-Ende. Im Leerlauf sendet der Replikator Heartbeats.

Der Leader bestätigt einen Eintrag aus seinem aktuellen Term, sobald ihn eine
Mehrheit repliziert hat. Damit sind auch die vorausgehenden Einträge bestätigt.
Der `NoOp` beim Amtsantritt ermöglicht diese Bestätigung unmittelbar nach einem
Leader-Wechsel. Alle Knoten wenden bestätigte Einträge in Log-Reihenfolge an.
Erst danach antwortet der Leader auf den zugehörigen Schreibaufruf. Für einen
Leseaufruf wartet er zusätzlich auf frische Antworten einer Mehrheit. Die
Commit-Regel und das Verhalten bei abweichenden Logs folgen [1], Abschnitte
5.3–5.4 und 8.

Die Spiellogik liefert die Ereignisse und eine getrennte Sicht pro Spieler. Ein
Retry behält dieselbe `action_id`. Die Zustandsmaschine erkennt bereits
angewendete Aktionen und ändert den Spielzustand beim Retry kein zweites Mal.
Eine fehlende ID wird vor dem Anhängen abgelehnt. Die lokale UUID-Vorgabe der
Engine würde sonst auf jedem Knoten eine andere ID erzeugen.

Der `ResultWriter` läuft getrennt von der Replikation. Er übergibt die
Ergebnisse abgeschlossener Partien nur während der Leader-Rolle an das
PostgreSQL-Repository. Ein Datenbankfehler verzögert das Speichern, während
Spielzüge weiter verarbeitet werden können. Das Repository verwendet die
`game_id` als eindeutigen Schlüssel. Es kann deshalb dieselbe Partie auch bei
einer Wiederholung durch einen neuen Leader genau einmal speichern.

## Schnittstellen zu P2 und P3

- P3 hält Knoten-ID, Adressliste `nodes`, Rolle, Term und Leader-ID und stellt
  den asynchronen Transport `call(peer_id, rpc, payload)` bereit. Der Transport
  liefert bei Nichterreichbarkeit `None`. P3 erstellt pro Knoten eine Instanz
  `Replication(node, transport, heartbeat=...)`.
- Nach dem Wechsel zum Leader ruft P3 `replication.on_become_leader()` auf.
  Beim Abstieg ruft P3 `replication.fail_pending()` auf. Der Knoten nimmt gültige
  `AppendEntries` über `accept_leader(term, leader_id)` an und setzt dabei den
  Wahl-Timer zurück. Bei einer Antwort mit höherem Term ruft die Replikation
  `node.step_down(term)` auf. Diese Aufrufe erfolgen im selben Event-Loop.
- Die Wahl nutzt `replication.last_index` und `replication.term_at(index)`.
  Check-Quorum liest die monotonen Zeitstempel aus `peer_last_response`.
  Der Raft-Endpunkt delegiert an `replication.handle_append_entries(**payload)`.
- Der gemeinsame Server registriert `app.include_router(client_router(replication))`
  aus `services.raft.client_routes`. Für Teil 2 ergänzt der Server
  `parser.add_argument("--db")` und öffnet im Lifespan
  `async with result_writer(replication, args.db)` aus `services.raft.results`.
  Beim Beenden wartet der Server auf `replication.close()`.
- Lobby und Gateway nutzen `ClusterClient(urls)` mit `submit(command)`,
  `views(game_id)` und `close()`. Der Writer nutzt P2s
  `ResultRepository.save(game_id, results, knocker)`.

## Tests und Reflexion

Die Komponententests steuern die Leader-Wechsel in einem simulierten
Drei-Knoten-Cluster. Geprüft werden die Fortsetzung einer Partie, ein leerer
Neustart, die Korrektur eines abweichenden Log-Endes, die Ablehnung ungültiger
und doppelter Spielzüge, die Commit-Regel und die Wiederholung nach einer
simulierten Datenbankunterbrechung. Der HTTP-Test verwendet die FastAPI-Routen
über einen ASGI-Testtransport.

Für den gemeinsamen Ende-zu-Ende-Test sind drei P4-Prüfungen vorgesehen.

1. Einen Spielzug bestätigen, den Leader-Prozess beenden und den nächsten Zug
   auf dem neuen Leader bestätigen lassen.
2. Den alten Prozess neu starten und warten, bis Log-Länge und Commit-Index
   mit dem Leader übereinstimmen.
3. Nach Spielende den Leader beenden und in PostgreSQL prüfen, dass die Partie
   einmal mit allen Spielerzeilen gespeichert wurde und die Lobby `finished` ist.

Messwerte zur realen Wahlzeit und zur PostgreSQL-Wiederherstellung werden nach
der Integration ergänzt. Das Log dieses Teilstands liegt im Arbeitsspeicher.
Für Wiederanlauf nach Verlust mehrerer Log-Kopien müssen Term, Stimme und
Log-Einträge gemäß [1] dauerhaft gespeichert werden.

## Kurzvorstellung

- **0:00–1:00** Eine Spielaktion vom Gateway bis zur Antwort verfolgen.
- **1:00–2:30** Mehrheit, aktuellen Term und `NoOp` erklären.
- **2:30–4:00** Beim Leader-Ausfall den nächsten Zug und das Aufholen des
  neu gestarteten Knotens zeigen.
- **4:00–5:00** Die Ergebnisspeicherung nach einem Leader-Wechsel erläutern.

Für die eigene Erklärung dokumentieren, dass ChatGPT Codex zur Umsetzung von
P4, zu Tests und für einen Entwurf dieses Abschnitts verwendet wurde.

## Quelle

[1] D. Ongaro und J. Ousterhout, *In Search of an Understandable Consensus
Algorithm*, USENIX ATC, 2014, erweiterte Fassung.
<https://raft.github.io/raft.pdf>
