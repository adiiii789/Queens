  

#let titel = "\"Queens\" als verteiltes Multiplayer-Kartenspiel"
#let autor = "Gergö Boross ♠, Adrian Döring ♥, Jannis Eleftherakis ♦ und Luca Hertfelder ♣"
#let thema = "Programmentwurf Verteilte Systeme (T4INF3301)"
#let kurzthema = "Programmentwurf Verteilte Systeme"

#let abgabedatum = datetime(
  year: 2026,
  month: 12,
  day: 08,
)



#set document(title: titel + ", " + autor, author: autor, description: thema, date: abgabedatum)
#set page(
  paper: "a4",
  number-align: right + bottom,
  margin: (inside: 2.5cm, outside: 2.5cm, y: 2.5cm)
)
#set text(
  size: 14pt,
  lang: "de"
)

#import "include/templates.typ": deckblatt



////////////////////////////////
// DECKBLATT
////////////////////////////////

#show: deckblatt.with(
  thema: thema,
  titel: titel,

  studiengang: "Informatik",
  standort: "Stuttgart",
  autoren: (
    (
      name: autor,
    ),
  ),
  abgabedatum: abgabedatum,
  bearbeitungszeitraum: "05.12.2026 - 08.12.2026",
  martikelnummer: "5337368 ♠, 4898576 ♥, 1306243 ♦, \n                                                                 7200707 ♣",
  kurs: "TINF24IN"
)

////////////////////////////////

// Kopfzeile
#counter(page).update(1)
#set page(
  numbering: "i",
  header: rect(
    height: 100%,
    inset: 0mm,
    stroke: none,
    // fill: red,
  )[
    #set align(top)
    #set text(11pt)
    #grid(
      // fill: blue,
      columns: (4fr, 1fr, auto),
      rows: (4cm),
      align: horizon,
      image("include/images/DHBW-Logo.svg", height: 1.6cm),
      h(1fr),
      // text(titel + " " + autor + "\ndfs"),
      box([
        #set align(right)
        #titel \
        #kurzthema
      ])
    )
  ],
)


#let main_heading_abstand = 1.5em

#let main_heading(title) = [
  #v(main_heading_abstand)
  #heading(
    level: 1,
    numbering: none,
    outlined: true,
  )[#title]
]


#show heading: set text(rgb("#da001d"))
#show heading: set block(below: 1em, above: 2em)

#set heading(numbering: "1.1.1 - ")

// highlighted wörter mit `Hochkommas`
#show raw.where(block: false): it => box(
  fill: rgb("#f2f2f2"),
  inset: 3pt,
  radius: 2pt,
  it,
)
#show raw: set text(font: "Consolas")


// Absätze
#set par(spacing: 2em, justify: true)


#show math.equation: set align(center)

#show figure.caption: set text(weight: 600)

////////////////////////////////



////////////////////////////////
// INHALTS-VERZEICHNIS
////////////////////////////////

#v(main_heading_abstand)
#outline(title: "Inhaltsverzeichnis")
#pagebreak()

////////////////////////////////



////////////////////////////////
// ABBILDUNGS-VERZEICHNIS
////////////////////////////////

#main_heading("Abbildungsverzeichnis")

#show cite: it => [#it]

#show outline.where(target: figure.where(kind: image)): it => {
  show outline.entry: it => {
    show cite: it => { }
    it
  }
  it
}

#outline(target: figure.where(kind: image), title: none)
#pagebreak()

////////////////////////////////



////////////////////////////////
// ABKÜRZUNGS-VERZEICHNIS
////////////////////////////////

#main_heading("Abkürzungsverzeichnis")

// #include "abkürzungen.typ"
#pagebreak()

////////////////////////////////



#set page(numbering: "1")
#counter(page).update(1)



////////////////////////////////
// TEXT
////////////////////////////////

#include "1_einleitung.typ"
#pagebreak()

#include "2_architektur.typ"
#pagebreak()

#include "3_umsetzung.typ"
#pagebreak()

#include "4_reflexion.typ"
#pagebreak()

////////////////////////////////



////////////////////////////////
// LITERATUR-VERZEICHNIS
////////////////////////////////

#v(main_heading_abstand)
#bibliography("include/literatur.bib", title: "Literaturverzeichnis", full: false)

#pagebreak()

////////////////////////////////



////////////////////////////////
// EIGENSTÄNDIGKEITSERKLÄRUNGEN
////////////////////////////////

#main_heading("Eigenständigkeitserklärungen")

#linebreak()

Ich versichere, dass ich die mit meinem Namen gekennzeichneten Abschnitte und
Komponenten selbstständig erstellt und keine anderen als die angegebenen Quellen und
Hilfsmittel verwendet habe.
Folgende KI-Werkzeuge habe ich genutzt:
- [Werkzeug] für [Zweck] (bzw. ‚keine‘).

#linebreak()
#linebreak()

#set overline(offset: -2em)
#overline("Ort, Datum                          ")
#h(4cm) 
#overline("Gergö Boross                               ")


#linebreak()
#linebreak()

Ich versichere, dass ich die mit meinem Namen gekennzeichneten Abschnitte und
Komponenten selbstständig erstellt und keine anderen als die angegebenen Quellen und
Hilfsmittel verwendet habe.
Folgende KI-Werkzeuge habe ich genutzt:
- [Werkzeug] für [Zweck] (bzw. ‚keine‘).

#linebreak()
#linebreak()

#set overline(offset: -2em)
#overline("Ort, Datum                          ")
#h(4cm) 
#overline("Adrian Döring                              ")


#linebreak()
#linebreak()

Ich versichere, dass ich die mit meinem Namen gekennzeichneten Abschnitte und
Komponenten selbstständig erstellt und keine anderen als die angegebenen Quellen und
Hilfsmittel verwendet habe.
Folgende KI-Werkzeuge habe ich genutzt:
- [Werkzeug] für [Zweck] (bzw. ‚keine‘).

#linebreak()
#linebreak()

#set overline(offset: -2em)
#overline("Ort, Datum                          ")
#h(4cm) 
#overline("Jannis Eleftherakis                        ")


#linebreak()
#linebreak()

Ich versichere, dass ich die mit meinem Namen gekennzeichneten Abschnitte und
Komponenten selbstständig erstellt und keine anderen als die angegebenen Quellen und
Hilfsmittel verwendet habe.
Folgende KI-Werkzeuge habe ich genutzt:
- [Werkzeug] für [Zweck] (bzw. ‚keine‘).

#linebreak()
#linebreak()

#set overline(offset: -2em)
#overline("Ort, Datum                          ")
#h(4cm) 
#overline("Luca Hertfelder                            ")
#pagebreak()

////////////////////////////////



////////////////////////////////
// ANHANG
////////////////////////////////

#main_heading("Anhang")

#set heading(numbering: "A.1.1 -")
#counter(heading).update(0)

// #include "anhang.typ"

////////////////////////////////