#import "include/templates.typ": abschnitt_von

= Anforderungen

Die folgenden Anforderungen beschreiben die benötigten Funktionen und Qualitätsmerkmale des Prototyps.
Sie dienen zugleich als Grundlage für die spätere Überprüfung durch Tests und Ausfalldemonstrationen.

== Funktionale Anforderungen

#table(
  columns: (0.8fr, 3.2fr, 3fr),
  inset: 5pt,
  table.header([*ID*], [*Anforderung*], [*Akzeptanzkriterium*]),
  [FA-01], [Lobbys können erstellt, betreten und verlassen werden.], [Der Lobby-Service führt eine aktuelle Mitgliederliste für zwei bis acht Personen.],
  [FA-02], [Der Bereitschaftsstatus wird verwaltet.], [Eine Partie startet nur unter den festgelegten Startbedingungen.],
  [FA-03], [Clients nehmen über das Realtime-Gateway am Spiel teil.], [Aktionen und Zustandsänderungen werden über WebSockets übertragen.],
  [FA-04], [Alle Spielaktionen werden serverseitig validiert.], [Unzulässige Aktionen verändern den verbindlichen Zustand nicht.],
  [FA-05], [Die implementierten Spielregeln werden vollständig verarbeitet.], [Ziehen, Tauschen, Ablegen, Sonderaktionen, Zwischenwerfen, Klopfen und Überspringen führen zu den definierten Zustandsübergängen.],
  [FA-06], [Spielaktionen erhalten clusterweit eine eindeutige Reihenfolge.], [Eine Aktion wird erst nach Bestätigung durch die erforderliche Raft-Mehrheit angewendet.],
  [FA-07], [Alle Replikate verarbeiten bestätigte Aktionen deterministisch.], [Gleicher Ausgangszustand und gleiche Aktionsfolge erzeugen denselben Folgezustand.],
  [FA-08], [Jede Person erhält eine individuelle Spieleransicht.], [Verdeckte Kartenwerte anderer Personen werden nicht übertragen.],
  [FA-09], [Öffentliche und private Ereignisse werden getrennt verteilt.], [Private Karteneinsichten erreichen nur die berechtigte Person.],
  [FA-10], [Nach Rundenende wird das Ergebnis berechnet und gespeichert.], [Die Kartenwerte werden ausgewertet und das Ergebnis an die Persistenz übergeben.],
)

Das Gateway übernimmt Verbindung und Weiterleitung, entscheidet jedoch nicht über die Gültigkeit einer Aktion.
Diese Verantwortung liegt bei der Spiel-Engine.
Konkurrierende Aktionen werden vom Backend beziehungsweise vom Raft-Leader geordnet.
Erst nach dem Commit wird eine Aktion Bestandteil des verbindlichen Spielverlaufs.
Der intern vollständige Zustand wird vor der Übertragung durch eine individuelle Spieleransicht gefiltert.

Die funktionalen Anforderungen bilden zugleich den Ablauf einer Partie ab: Zunächst wird eine Lobby erzeugt und mit Spielenden befüllt.
Nach dem Start übernimmt das Gateway die dauerhafte Kommunikation.
Jede eingehende Aktion durchläuft Validierung, Replikation und Zustandsübergang, bevor die daraus abgeleiteten Ereignisse verteilt werden.
Am Rundenende wird der final berechnete Zustand in ein dauerhaftes Ergebnis überführt.
Diese Reihenfolge erleichtert die Zuordnung der Anforderungen zu den später beschriebenen Komponenten.

#abschnitt_von("Jannis")


== Nicht-funktionale Anforderungen

#table(
  columns: (0.8fr, 1.5fr, 3fr, 2.7fr),
  inset: 5pt,
  table.header([*ID*], [*Merkmal*], [*Anforderung*], [*Prüfung oder Grenze*]),
  [NA-01], [Konsistenz], [Bestätigte Aktionen werden auf allen Replikaten in derselben Reihenfolge angewendet.], [Vergleich der Zustände nach identischer Aktionsfolge.],
  [NA-02], [Ausfallsicherheit], [Der Ausfall eines Followers darf den Spielbetrieb bei bestehender Mehrheit nicht verhindern.], [Beenden eines Followers und Ausführen einer weiteren Aktion.],
  [NA-03], [Leader-Failover], [Nach Ausfall des Leaders wird ein neuer Leader gewählt.], [Beenden des Leaders während einer Partie und Fortsetzen des Spiels.],
  [NA-04], [Mehrheitsschutz], [Ohne Raft-Mehrheit werden keine Aktionen bestätigt.], [Isolation der Mehrheit verhindert weitere Commits.],
  [NA-05], [Skalierbarkeit], [Lobby-Service und Gateway sind unabhängig vom Raft-Cluster vervielfältigbar.], [Mehrere Instanzen können mit gemeinsamer Persistenz gestartet werden.],
  [NA-06], [Vertraulichkeit], [Clients erhalten keine unzulässigen verdeckten Kartenwerte.], [Vergleich der Ansichten verschiedener Personen.],
  [NA-07], [Wartbarkeit], [Engine, Gateway, Lobby und Raft besitzen getrennte Verantwortlichkeiten.], [Komponenten können isoliert getestet oder ausgetauscht werden.],
  [NA-08], [Testbarkeit], [Zustandsübergänge und verteilte Abläufe sind reproduzierbar prüfbar.], [Feste Seeds sowie Unit-, Integrations- und Ende-zu-Ende-Tests.],
)

Konsistenz wird gegenüber dauerhafter Schreibverfügbarkeit priorisiert.
Ein Cluster aus drei Knoten benötigt zwei erreichbare Knoten und toleriert damit einen Ausfall.
Bei fünf Knoten sind drei Knoten für eine Mehrheit erforderlich, sodass zwei Ausfälle toleriert werden können.
Ohne Mehrheit pausiert die Verarbeitung neuer Aktionen, statt voneinander abweichende Spielverläufe zu erzeugen.

Skalierbarkeit ist komponentenabhängig.
Weitere Lobby- oder Gateway-Instanzen können zusätzliche Verbindungen bedienen.
Zusätzliche Raft-Knoten erhöhen dagegen hauptsächlich die Fehlertoleranz und nicht automatisch den Durchsatz, da jede Aktion von einer Mehrheit bestätigt werden muss.
Die gemeinsame Datenbank und bestehende WebSocket-Verbindungen bleiben mögliche Engpässe beziehungsweise Ausfallgrenzen.

Verdeckte Karten werden nicht nur in der Oberfläche ausgeblendet, sondern bereits serverseitig aus der jeweiligen Spieleransicht entfernt.
Die deterministische Engine enthält keine Netzwerk- oder Uhrzeitabhängigkeit und kann daher isoliert getestet werden.
Die zugesicherte Ausfallsicherheit umfasst einzelne Knotenfehler im laufenden Betrieb.
Da das vollständige Raft-Log nicht persistent gespeichert wird, ist die verlustfreie Wiederherstellung nach einem Neustart des gesamten Clusters nicht Gegenstand der Anforderungen.

Für das Reaktionsverhalten wird keine harte Echtzeitgrenze gefordert.
Im lokalen Demonstrationsaufbau sollen normale Spielaktionen jedoch ohne wahrnehmbare, unnötige Verzögerung verarbeitet werden.
Während einer Leader-Neuwahl ist eine kurze Unterbrechung zulässig, da zunächst eine neue Mehrheit entstehen muss.
Eine Aktion darf in dieser Phase lieber verzögert oder abgelehnt als mehrfach oder in unterschiedlicher Reihenfolge bestätigt werden.
Damit folgt auch das Reaktionsverhalten der Priorisierung von Konsistenz gegenüber uneingeschränkter Verfügbarkeit.

#abschnitt_von("Jannis")
