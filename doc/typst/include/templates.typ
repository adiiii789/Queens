

#let deckblatt(
  thema: none,
  titel: none,
  studiengang: none,
  standort: none,
  autoren: (),
  abgabedatum: datetime,
  bearbeitungszeitraum: none,
  martikelnummer: none,
  kurs: none,
  dualer_partner: none,
  betreuer: none,
  gutachter: none,
  doc,
) = {

  set page(
    paper: "a4",
    number-align: right + bottom,
    margin: (
      top: 3cm,
    )
  )
  
  set page(
  header: rect(
    height: 100%,
    inset: 0mm,
    stroke: none,
    // fill: red,
  )[
    #set align(top)
    #set text(8pt)
    #grid(
      // fill: blue,
      columns: (auto, 1fr, auto),
      rows: (4cm),
      align: horizon,
      image("images/DHBW-Logo.svg", height: 2cm),
    )],
  )
  
  set rect(
    width: 100%,
    height: 100%,
    inset: 4pt,
  )
  
  set par(
    spacing: 2em,
    leading: 1em,
    justify: false,
  )
  
  set align(center)
  
  linebreak()
  linebreak()
  linebreak()
  text(strong(thema))
  linebreak()
  linebreak()
  linebreak()
  linebreak()

  text(20pt, titel)

  linebreak()
  linebreak()
  linebreak()
  
  text("\nim Studiengang\n" + strong(studiengang) + "\nan der Dualen Hochschule Baden-Württemberg " + standort + "\n\nvon")

  let count = autoren.len()
  let ncols = calc.min(count, 3)
  grid(
    columns: (1fr,) * ncols,
    row-gutter: 24pt,
    ..autoren.map(author => [
      #author.name \
    ]),
  )

  linebreak()
  
  abgabedatum.display("[day].[month].[year]")

  
  set align(left)

  linebreak()
  
  text("\nBearbeitungszeitraum:                            " + bearbeitungszeitraum)
  text("\nMartikelnummer:                                    " + martikelnummer)
  text("\nKurs:                                                        " + kurs)
  
  set page(header: none)
  pagebreak()
  doc

}