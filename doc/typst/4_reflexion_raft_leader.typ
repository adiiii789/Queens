#import "include/templates.typ": abschnitt_von

== Leader-Wahl und Integration

#abschnitt_von("Gergö")

Die größte Herausforderung war die enge Grenze zwischen Leader-Wahl und Log-Replikation. Beide Teile greifen auf denselben Zustand zu: Die Wahl braucht den letzten Log-Eintrag, die Replikation muss bei einem höheren Term die Rolle wechseln, und das Check-Quorum braucht die Antwortzeiten der Replikation. Gelöst haben wir das, indem wir die gegenseitigen Methodenaufrufe vor Beginn festgelegt und in getrennte Dateien gelegt haben. Mit dem Heartbeat-Platzhalter ließ sich die Wahl testen, bevor die Replikation fertig war; beim Zusammenführen der Branches gab es keine Konflikte, und die gemeinsamen Tests liefen ohne Änderungen am Code durch.

Eine zweite Schwierigkeit waren zeitabhängige Tests. Leader-Wahlen hängen vom Zufall und von Zeitabständen ab, und erste Versionen der Tests schlugen gelegentlich fehl, weil sie feste Pausen statt eines Zustands abwarteten. Stabil wurden sie erst, als jeder Test auf eine Bedingung wartet (etwa „genau ein Leader, den alle kennen“) und das Netz im Unit-Test simuliert wird. Der Ende-zu-Ende-Test lief danach zehnmal hintereinander fehlerfrei.

Mit mehr Zeit würden wir Folgendes anders machen:

- *Log persistieren.* Gespeichert werden nur Term und Stimme. Ein neu gestarteter Knoten holt das Log vom Leader nach, was im Betrieb genügt. Streng genommen verletzt das aber die Annahme von Raft, dass ein bestätigter Eintrag auf einer Mehrheit dauerhaft liegt: Starten mehrere Knoten gleichzeitig ohne Log neu, können bestätigte Aktionen verloren gehen.
- *PreVote einführen.* Ein isolierter Knoten startet immer neue Wahlen und erhöht dabei seinen Term. Kehrt er zurück, zwingt sein hoher Term den amtierenden Leader zum Rücktritt und löst eine überflüssige Neuwahl aus. Die PreVote-Erweiterung @ongaro2014thesis[Abschn. 9.6] fragt vorher, ob eine Wahl überhaupt Aussicht auf Erfolg hat, und vermeidet diese Unterbrechung.
- *Bestehende Bibliothek prüfen.* Eine eigene Raft-Implementierung war für das Verständnis wertvoll, ist aber fehleranfällig. Für ein Produktivsystem wäre ein erprobter Koordinationsdienst wie etcd die sicherere Wahl, zumal sich die Spiellogik als deterministische Zustandsmaschine leicht daran anbinden ließe.
