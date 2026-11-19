#import "include/templates.typ": abschnitt_von

#pagebreak(weak: true)

#[
#set text(font: "Arial", size: 11pt)
#set par(leading: 0.65em, spacing: 0.65em, justify: true)

== Log-Replikation und Ergebnisspeicherung

Die größte Schwierigkeit war die Behandlung unklarer Ergebnisse nach einem
Timeout. Ein Client kann keine Antwort erhalten haben, obwohl der Befehl
später noch bestätigt wird. Deshalb bleiben Aktions-IDs beim Wiederholen
gleich, und die nächste Vorabprüfung wartet auf das Anwenden des bisherigen
Logs. Der Spielzustand enthält die verarbeiteten IDs, sodass auch ein neuer
Leader den Duplikatschutz fortsetzt.

Besondere Aufmerksamkeit erforderten zwei unscheinbare Fälle. Ein
Heartbeat darf einen abweichenden Log-Rest nicht bestätigen. Außerdem
genügt eine Mehrheit für einen Eintrag aus einem früheren Term allein
nicht zum direkten Commit. Beide Bedingungen haben eigene
Regressionstests. Der `NoOp` nach einer Wahl verbindet die Commit-Regel
mit dem Bedarf, neue Anfragen auf einem aktuellen Spielzustand zu prüfen.

Bei der Ergebnisspeicherung liegen Konsens und PostgreSQL in getrennten
Systemen. Zwischen erfolgreichem Schreiben und lokaler Markierung kann
der Leader ausfallen. Die Lösung kombiniert wiederholbare Schreibversuche
mit einem eindeutigen Schlüssel und einer Transaktion im Repository.
Der Effekt in der Datenbank ist dadurch einmalig, auch wenn mehrere
Schreibversuche stattfinden. Die Sperre im Ende-zu-Ende-Test macht diesen
kritischen Zeitpunkt reproduzierbar.

Die Implementierung hat Grenzen. Das Raft-Log und die laufenden
Spielzustände liegen im Arbeitsspeicher. Ein einzelner neu gestarteter
Knoten holt den Zustand von den verbleibenden Replikaten nach. Bei Verlust
aller Log-Kopien gehen laufende Partien verloren. Auch der dauerhafte
Mehrheitsnachweis, den Raft voraussetzt, ist bei Neustarts mehrerer
Knoten ohne gespeichertes Log nicht gewährleistet. Für einen dauerhaften
Betrieb müssten Einträge vor einer erfolgreichen Replikationsantwort
persistiert und beim Start wiederhergestellt werden. Snapshots und
Log-Kompaktion würden anschließend Speicherbedarf und Wiederanlaufzeit
begrenzen. @ongaro2014raft

Die Fehlerbehandlung beim Anwenden hält den Knoten nach einer Ausnahme
arbeitsfähig und protokolliert den betroffenen Eintrag. Sie repariert einen
auseinanderlaufenden Spielzustand jedoch nicht. Ein produktiver Betrieb
bräuchte zusätzlich Zustandsvergleiche und eine Wiederherstellung aus
einem geprüften Snapshot oder dem Log.

Auch die wiederholte Antwort auf eine Aktion hat eine Grenze. Sie liefert
den aktuellen Zustand und eine leere Ereignisliste. Geht die erste Antwort
mit einem privaten Ereignis verloren, wird dieses Ereignis beim Retry
nicht erneut geliefert. Für eine verlässliche Wiederzustellung wäre ein
gezielt abrufbares, nach Empfänger geschütztes Ergebnis je Aktions-ID
erforderlich.

Die Zeit bis zur Ergebnisspeicherung hängt von Erreichbarkeit,
Datenbank-Timeouts und dem Wiederholungstakt des Writers ab. Eine
Netzwerktrennung ohne Verbindungsabbau wurde in den dokumentierten
Versuchen zum Datenbankausfall noch nicht vermessen. Für weitere Prüfungen
wären verzögerte und verlorene Nachrichten sowie wiederholte Neustarts
während gleichzeitig laufender Partien geeignete Ergänzungen.

#abschnitt_von("Luca Hertfelder (P4)")

]
