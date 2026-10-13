"""Terminal-Testmodus (Hotseat): alle Spieler an einem Terminal.

Dient zum Testen der Spiellogik ohne Netzwerk. Private Ereignisse
(angesehene/gezogene Karten) werden mit Kennzeichnung angezeigt und danach
optional verdeckt.

Start:  python cli.py
"""

from __future__ import annotations

import secrets
import sys

from queens_engine import (
    CardRef, DiscardDrawn, DrawFromDeck, Knock, PeekInitial, RuleViolation,
    SkipSpecial, StartGame, SwapWithHand, TakeFromDiscard, ThrowIn, UseJack,
    UseKing,
)
from services.backend import LocalGameBackend

GAME_ID = "local"

HELP = """
Befehle (Spieler als Nummer, Slots ab 0):
  z                 vom Ziehstapel ziehen
  a                 oberste Karte vom Ablagestapel nehmen
  t <slot>          gezogene Karte gegen eigenen Slot tauschen
  w                 gezogene Karte ablegen
  b <s1> <slot1> <s2> <slot2>   Bube: zwei Karten tauschen
  k <spieler> <slot>            König: Karte ansehen
  s                 Sonderaktion überspringen
  klopf             klopfen (statt ziehen)
  x <spieler> <slot>            Zwischenwerfen (jeder Spieler, jederzeit)
  h                 Hilfe
  q                 beenden
"""


def clear() -> None:
    print("\n" * 60)


class Hotseat:
    def __init__(self, names: list[str]) -> None:
        self.backend = LocalGameBackend()
        self.ids = [f"p{i + 1}" for i in range(len(names))]
        self.names = dict(zip(self.ids, names))
        self.backend.create_game(GAME_ID, list(zip(self.ids, names)))

    # --- Hilfen ----------------------------------------------------------------
    def pid(self, number: str) -> str:
        idx = int(number) - 1
        if not 0 <= idx < len(self.ids):
            raise ValueError("Ungültige Spielernummer.")
        return self.ids[idx]

    def n(self, player_id: str) -> str:
        return self.names.get(player_id, player_id)

    def submit(self, action) -> bool:
        try:
            events = self.backend.submit(GAME_ID, action)
        except RuleViolation as e:
            print(f"  ✗ {e}")
            return False
        has_private = False
        for ev in events:
            line = self.describe(ev)
            if ev.audience is None:
                print(f"  • {line}")
            else:
                has_private = True
                print(f"  🔒 nur für {self.n(ev.audience)}: {line}")
        if has_private:
            input("  (Enter zum Verdecken) ")
            clear()
        return True

    def describe(self, ev) -> str:
        d, n = ev.data, self.n
        ref = lambda r: f"{n(r['player_id'])} Slot {r['slot']}"  # noqa: E731
        texts = {
            "GameStarted": lambda: f"Spiel gestartet, Ablage: {d['discard_top']}",
            "CardsPeeked": lambda: "deine Karten: " + ", ".join(
                f"Slot {s}: {c}" for s, c in d["cards"].items()),
            "PlayerPeeked": lambda: f"{n(d['player_id'])} hat sich Karten angesehen",
            "TurnStarted": lambda: f"Zug {d['turn_number']}: {n(d['player_id'])} ist dran",
            "CardDrawn": lambda: f"gezogen: {d['card']}",
            "PlayerDrew": lambda: f"{n(d['player_id'])} zieht vom "
                                  + ("Ziehstapel" if d["source"] == "deck"
                                     else f"Ablagestapel ({d['card']})"),
            "CardSwapped": lambda: f"{n(d['player_id'])} tauscht Slot {d['slot']}, "
                                   f"legt {d['discarded']} ab",
            "CardDiscarded": lambda: f"{n(d['player_id'])} legt {d['card']} ab",
            "SpecialAvailable": lambda: f"{n(d['player_id'])} darf "
                                        + ("tauschen (Bube)" if d["special"] == "jack"
                                           else "eine Karte ansehen (König)"),
            "CardsSwappedByJack": lambda: f"Bube: {ref(d['first'])} ↔ {ref(d['second'])}",
            "CardRevealedByKing": lambda: f"{ref(d['target'])} ist {d['card']}",
            "PlayerPeekedByKing": lambda: f"{n(d['player_id'])} sieht {ref(d['target'])} an",
            "SpecialSkipped": lambda: f"{n(d['player_id'])} verzichtet auf Sonderaktion",
            "PlayerKnocked": lambda: f"{n(d['player_id'])} klopft! Letzte Runde.",
            "ThrowInSucceeded": lambda: f"{n(d['player_id'])} wirft {d['card']} "
                                        f"(Slot {d['slot']}) zwischen ✓",
            "ThrowInFailed": lambda: f"{n(d['player_id'])} wirft falsch: {d['card']} "
                                     f"(Slot {d['slot']}) bleibt liegen",
            "PenaltyCard": lambda: f"{n(d['player_id'])} erhält Strafkarte in Slot {d['slot']}",
            "DeckReshuffled": lambda: "Ablagestapel wird neu gemischt",
            "TurnTimedOut": lambda: f"Zeit abgelaufen für {n(d['player_id'])}",
            "GameFinished": lambda: "Spiel beendet",
        }
        return texts.get(ev.type, lambda: f"{ev.type} {d}")()

    def show_table(self) -> None:
        state = self.backend.get_state(GAME_ID)
        print("\n" + "─" * 50)
        top = state.discard_top()
        print(f"Ziehstapel: {len(state.deck)} | Ablage oben: {top or '-'}")
        for i, p in enumerate(state.players, 1):
            slots = " ".join(f"[{s}]" if c else "[ ]" for s, c in enumerate(p.hand))
            marks = []
            if state.phase.value.startswith("await") and p is state.current_player:
                marks.append("◀ am Zug")
            if state.knocker_id == p.player_id:
                marks.append("(hat geklopft)")
            print(f"  {i}. {p.name:<10} {slots} {' '.join(marks)}")
        if state.pending_special:
            print(f"Offene Sonderaktion: {state.pending_special.value}")
        if state.drawn_card is not None:
            print(f"{state.current_player.name} hält eine gezogene Karte "
                  f"(nur für sie/ihn sichtbar: {state.drawn_card})")

    # --- Ablauf ----------------------------------------------------------------
    def run(self) -> None:
        self.submit(StartGame(player_id=self.ids[0], seed=secrets.token_hex(8)))
        self.initial_peek()
        print(HELP)
        while self.backend.get_state(GAME_ID).phase.value != "finished":
            self.show_table()
            state = self.backend.get_state(GAME_ID)
            try:
                cmd = input(f"{state.current_player.name}> ").strip().split()
            except EOFError:
                return
            if cmd:
                self.handle(cmd, state.current_player.player_id)
        self.show_result()

    def initial_peek(self) -> None:
        for pid in self.ids:
            clear()
            while True:
                raw = input(f"{self.n(pid)}: zwei Slots ansehen (z. B. '0 1'): ").split()
                try:
                    if self.submit(PeekInitial(player_id=pid,
                                               slots=tuple(int(s) for s in raw))):
                        break
                except ValueError:
                    print("  ✗ Bitte zwei Zahlen eingeben.")

    def handle(self, cmd: list[str], active: str) -> None:
        c, args = cmd[0].lower(), cmd[1:]
        try:
            if c == "q":
                sys.exit(0)
            elif c == "h":
                print(HELP)
            elif c == "z":
                self.submit(DrawFromDeck(player_id=active))
            elif c == "a":
                self.submit(TakeFromDiscard(player_id=active))
            elif c == "t":
                self.submit(SwapWithHand(player_id=active, slot=int(args[0])))
            elif c == "w":
                self.submit(DiscardDrawn(player_id=active))
            elif c == "b":
                self.submit(UseJack(
                    player_id=active,
                    first=CardRef(self.pid(args[0]), int(args[1])),
                    second=CardRef(self.pid(args[2]), int(args[3]))))
            elif c == "k":
                self.submit(UseKing(player_id=active,
                                    target=CardRef(self.pid(args[0]), int(args[1]))))
            elif c == "s":
                self.submit(SkipSpecial(player_id=active))
            elif c == "klopf":
                self.submit(Knock(player_id=active))
            elif c == "x":
                self.submit(ThrowIn(player_id=self.pid(args[0]), slot=int(args[1])))
            else:
                print("  ✗ Unbekannter Befehl (h für Hilfe).")
        except (IndexError, ValueError):
            print("  ✗ Falsche Argumente (h für Hilfe).")

    def show_result(self) -> None:
        state = self.backend.get_state(GAME_ID)
        print("\n" + "═" * 50 + "\nAuflösung:")
        for p in state.players:
            cards = " ".join(str(c) for c in p.hand if c)
            print(f"  {p.name:<10} {cards:<30} = {state.scores[p.player_id]} Punkte")
        print("Gewinner: " + ", ".join(self.n(w) for w in state.winners))


def main() -> None:
    print("Queens – Terminal-Testmodus")
    while True:
        raw = input("Spielernamen (2–8, durch Komma getrennt): ")
        names = [n.strip() for n in raw.split(",") if n.strip()]
        if 2 <= len(names) <= 8:
            break
        print("Bitte 2 bis 8 Namen eingeben.")
    Hotseat(names).run()


if __name__ == "__main__":
    main()
