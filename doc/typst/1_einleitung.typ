#import "include/templates.typ": abschnitt_von


= Einleitung

== Motivation

Ein digitales Mehrspielerspiel muss räumlich getrennten Clients einen gemeinsamen und verbindlichen Spielzustand bereitstellen.
Treffen mehrere Aktionen fast gleichzeitig ein, benötigt das System eine eindeutige Reihenfolge.
Jede Aktion muss gegenüber dem aktuellen Zustand geprüft und die daraus entstehende Änderung konsistent an die beteiligten Clients verteilt werden.

Das Kartenspiel „Queens“ verbindet rundenbasierte Züge mit spontanen Aktionen.
Beim Zwischenwerfen dürfen auch Personen handeln, die aktuell nicht am Zug sind.
Dadurch können konkurrierende Eingaben entstehen.
Zusätzlich liegen die meisten Karten verdeckt.
Jeder Client darf deshalb nur die nach dem Regelwerk sichtbaren Informationen erhalten.

Wird der Spielzustand ausschließlich von einer Serverinstanz verwaltet, führt deren Ausfall zum Stillstand der Partie.
Das Projekt verteilt die Verarbeitung daher auf mehrere Game-Service-Knoten.
Mithilfe von Raft einigen sie sich auf die Reihenfolge der Aktionen.
Fällt der Leader aus, kann ein neuer gewählt werden, solange eine Mehrheit der Knoten erreichbar bleibt.
Das Projekt untersucht damit, wie ein gemeinsamer Spielzustand trotz konkurrierender Aktionen und einzelner Ausfälle konsistent gehalten werden kann.
Im Unterschied zu einer rein lokalen Umsetzung müssen Spielregel und Systemzustand dabei unabhängig von einzelnen Clients bleiben.
Ein Client übermittelt lediglich die gewünschte Aktion.
Ob diese zulässig ist und welche Folgen sie hat, entscheidet das Backend.
Dadurch lässt sich vermeiden, dass unterschiedliche Programmstände oder manipulierte Eingaben zu abweichenden Spielverläufen führen.

Als konkreter Anwendungsfall macht „Queens“ die Eigenschaften eines verteilten Systems unmittelbar sichtbar.
Eine Leader-Wahl kann während einer laufenden Partie demonstriert werden, während die Spielenden anschließend mit demselben Zustand fortfahren.
Gleichzeitig lässt sich anhand verdeckter Karten prüfen, ob die technische Verteilung nicht nur verfügbar und konsistent, sondern auch fachlich korrekt erfolgt.

#abschnitt_von("Jannis")


== Zielsetzung und Abgrenzung

Ziel ist die prototypische Umsetzung von „Queens“ als browserbasiertes Mehrspielerspiel für zwei bis acht Personen.
Lobbys ermöglichen das Erstellen und Betreten einer Partie sowie die Verwaltung des Bereitschaftsstatus.
Während des Spiels übermittelt ein Realtime-Gateway die Aktionen der Clients an das Backend.
Dort werden sie validiert, durch den Raft-Cluster geordnet und von einer deterministischen Spiel-Engine verarbeitet.
Aus dem vollständigen Zustand erzeugt das Backend für jede Person eine individuelle Sicht.

Der Schwerpunkt liegt auf typischen Problemen verteilter Systeme: eindeutige Aktionsreihenfolge, Replikation, Leader-Wahl, Ausfall einzelner Knoten und Trennung öffentlicher und privater Informationen.
Der Prototyp ist nicht als produktionsreife Spieleplattform vorgesehen.
Nicht betrachtet werden öffentliches Matchmaking, dauerhafte Benutzerkonten, geografisch verteilte Rechenzentren und byzantinische Fehler.
Ebenso erfolgt keine vollständige Absicherung gegen manipulierte Clients oder Denial-of-Service-Angriffe.

Die Ausfallsicherheit gilt nur, solange eine Raft-Mehrheit erreichbar bleibt.
Ohne Mehrheit werden keine neuen Aktionen bestätigt.
Außerdem werden zwar Term und Stimme persistiert, jedoch nicht das vollständige Raft-Log.
Daher wird keine verlustfreie Wiederherstellung nach einem vollständigen Cluster-Neustart zugesichert.

Die Zielerreichung wird anhand mehrerer Ebenen bewertet.
Unit-Tests prüfen die deterministische Spiellogik, Integrationstests die Zusammenarbeit der Dienste und eine Ausfalldemonstration das Verhalten bei einem beendeten Raft-Knoten.
Entscheidend ist, dass die gewählte Architektur die geforderten Zustandsübergänge und Fehlerszenarien unterstützt.

#abschnitt_von("Jannis")


== Spielprinzip von „Queens"

„Queens“ wird mit 52 Karten und zwei bis acht Personen gespielt.
Jede Person erhält vier verdeckte Karten und darf zu Beginn einmalig zwei davon ansehen.
Ziel ist eine möglichst niedrige Summe der eigenen Kartenwerte.

Das Grundprinzip ist mit Kartenspielen wie „Biberbande“ und „CABO“ vergleichbar.
Auch dort liegen vier Karten verdeckt vor jeder Person, nur ein Teil der eigenen Karten ist zu Beginn bekannt und Karten mit hohen Werten sollten durch niedrigere ersetzt werden.
Der Spielverlauf verbindet dadurch Glück mit einem Gedächtniselement: Die Spielenden müssen sich bekannte Kartenwerte und spätere Positionsänderungen merken.
„Queens“ übernimmt dieses grundlegende Prinzip, setzt mit einem klassischen 52-Karten-Blatt, dem jederzeit möglichen Zwischenwerfen sowie den eigenen Sonderaktionen jedoch andere Schwerpunkte.
Der Vergleich dient daher der Einordnung des Spieltyps und bedeutet keine vollständige Regelgleichheit.

Im regulären Zug wird eine Karte vom Zieh- oder Ablagestapel aufgenommen.
Sie kann gegen eine eigene verdeckte Karte getauscht oder direkt abgelegt werden.
Die ersetzte Karte wird offen auf den Ablagestapel gelegt.
Ist der Ziehstapel leer, wird der Ablagestapel mit Ausnahme seiner obersten Karte neu gemischt.

Beim Ablegen bestimmter Karten kann eine Sonderaktion ausgeführt werden.
Ein Bube erlaubt den verdeckten Tausch zweier beliebiger Karten.
Mit einem König darf eine beliebige Karte angesehen werden.
Die Sonderaktion kann jedoch auch übersprungen werden.

#figure(
  table(
    columns: (1fr, 1fr, 3fr),
    inset: 5pt,
    table.header([*Karte*], [*Wert*], [*Bedeutung*]),
    [Dame], [−1], [Keine Sonderaktion],
    [Ass], [1], [Keine Sonderaktion],
    [Zahlenkarte], [2 bis 10], [Wert entspricht der Zahl],
    [Bube], [10], [Zwei Karten blind tauschen],
    [König], [10], [Eine beliebige Karte ansehen],
  ),
  caption: [Kartenwerte und Sonderaktionen],
) <sonderkarten>

Beim Zwischenwerfen darf jederzeit eine eigene Karte abgelegt werden, wenn ihr Rang der obersten Ablagekarte entspricht.
Bei einem falschen Zwischenwurf bleibt die aufgedeckte Karte liegen und die Person erhält zusätzlich eine Strafkarte.
Mehrere nahezu gleichzeitige Zwischenwürfe werden nacheinander verarbeitet und jeweils gegen den dann aktuellen Zustand geprüft.

Anstelle des Ziehens kann die aktive Person klopfen, also signalisieren, dass sie fertig ist beziehungsweise glaubt, die niedrigste Kartensumme zu haben.
Danach erhalten alle anderen noch einen letzten Zug.
Anschließend werden die Karten aufgedeckt und addiert; die niedrigste Summe gewinnt.
Reagiert eine aktive Person nicht rechtzeitig, kann diese nach einer Wartezeit auch übersprungen werden.

#abschnitt_von("Jannis")



// #pagebreak()

#include "1.5_anforderungen.typ"