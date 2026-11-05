"""Textdarstellung für den Terminal-Client: Ereignisse, Lobby, Tisch, Auflösung.

Reine Funktionen ohne Netzwerk und ohne Spielregeln. Sie arbeiten nur mit dem
Ereignis- und Sichtformat aus queens_engine (events.py, views.py) und der
Lobby-Antwort des Lobby-Service. Spieler werden in der Reihenfolge von
`view["players"]` ab 1 nummeriert; diese Nummern nutzen die Befehle b und k.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

LOCK = "🔒"
LINE = "─" * 56

LOBBY_STATUS = {
    "waiting": "wartet auf Spieler",
    "starting": "Partie startet …",
    "started": "Partie läuft",
    "finished": "Partie beendet",
    "deleted": "aufgelöst",
}

LOBBY_HINT = "neu · join <code> · bereit · nicht · verlassen · h Hilfe"


class Names:
    """Spielernamen; der eigene Name wird mit „(du)“ markiert."""

    def __init__(self, names: Optional[dict[str, str]] = None, me: Optional[str] = None):
        self.names = dict(names or {})
        self.me = me

    def __call__(self, player_id: Optional[str]) -> str:
        name = self.names.get(player_id or "", player_id or "?")
        return f"{name} (du)" if player_id == self.me else name


def _ref(names: Names, ref: dict[str, Any]) -> str:
    return f"{names(ref['player_id'])} Slot {ref['slot']}"


_TEXTS: dict[str, Callable[[dict[str, Any], Names], str]] = {
    "GameStarted": lambda d, n: f"Karten sind ausgeteilt, Ablage: {d['discard_top']}",
    "CardsPeeked": lambda d, n: "deine Karten: " + ", ".join(
        f"Slot {s} = {c}" for s, c in sorted(d["cards"].items(), key=lambda i: int(i[0]))),
    "PlayerPeeked": lambda d, n: f"{n(d['player_id'])} hat sich die Startkarten angesehen",
    "PeekTimedOut": lambda d, n: (
        "Zeit zum Ansehen abgelaufen, ohne Ansehen weiter: "
        + ", ".join(n(p) for p in d["player_ids"])),
    "TurnStarted": lambda d, n: f"Zug {d['turn_number']}: {n(d['player_id'])} ist dran",
    "CardDrawn": lambda d, n: f"du hast gezogen: {d['card']}",
    "PlayerDrew": lambda d, n: f"{n(d['player_id'])} zieht vom " + (
        "Ziehstapel" if d["source"] == "deck" else f"Ablagestapel ({d['card']})"),
    "CardSwapped": lambda d, n: (f"{n(d['player_id'])} tauscht Slot {d['slot']} "
                                 f"und legt {d['discarded']} ab"),
    "CardDiscarded": lambda d, n: f"{n(d['player_id'])} legt {d['card']} ab",
    "SpecialAvailable": lambda d, n: f"{n(d['player_id'])} darf " + (
        "zwei Karten tauschen (Bube)" if d["special"] == "jack"
        else "eine Karte ansehen (König)"),
    "CardsSwappedByJack": lambda d, n: (f"Bube: {_ref(n, d['first'])} ↔ "
                                        f"{_ref(n, d['second'])}"),
    "CardRevealedByKing": lambda d, n: f"König: {_ref(n, d['target'])} ist {d['card']}",
    "PlayerPeekedByKing": lambda d, n: (f"{n(d['player_id'])} sieht sich "
                                        f"{_ref(n, d['target'])} an (König)"),
    "SpecialSkipped": lambda d, n: f"{n(d['player_id'])} verzichtet auf die Sonderaktion",
    "PlayerKnocked": lambda d, n: f"{n(d['player_id'])} klopft! Alle anderen noch einmal.",
    "ThrowInSucceeded": lambda d, n: (f"{n(d['player_id'])} wirft {d['card']} "
                                      f"(Slot {d['slot']}) zwischen ✓"),
    "ThrowInFailed": lambda d, n: (f"{n(d['player_id'])} wirft falsch: {d['card']} "
                                   f"(Slot {d['slot']}) bleibt liegen"),
    "PenaltyCard": lambda d, n: f"{n(d['player_id'])} bekommt eine Strafkarte in Slot {d['slot']}",
    "DeckReshuffled": lambda d, n: f"Ablagestapel neu gemischt ({d['deck_size']} Karten)",
    "TurnTimedOut": lambda d, n: f"Zeit abgelaufen für {n(d['player_id'])}",
    "GameFinished": lambda d, n: "Partie beendet, Karten werden aufgedeckt",
}

KNOWN_EVENTS = frozenset(_TEXTS)


def describe(event: dict[str, Any], names: Names) -> str:
    """Eine Zeile je Ereignis; private Ereignisse mit Schloss markiert."""
    text = _TEXTS.get(event.get("type", ""))
    try:
        line = text(event.get("data") or {}, names) if text else None
    except (KeyError, TypeError, ValueError):
        line = None
    if line is None:  # unbekanntes oder unerwartetes Format: roh anzeigen
        line = f"{event.get('type')} {event.get('data')}"
    prefix = f"{LOCK} " if event.get("audience") else "• "
    return prefix + line


# --- Lobby ------------------------------------------------------------------------
def render_lobby(lobby: dict[str, Any], me: Optional[str]) -> str:
    status = lobby.get("status", "")
    lines = [LINE, f"Lobby {lobby.get('lobby_id')} – {LOBBY_STATUS.get(status, status)}"
             + (f"  (Instanz {lobby['served_by']})" if lobby.get("served_by") else "")]
    for i, p in enumerate(lobby.get("players", []), 1):
        marks = []
        if p["player_id"] == lobby.get("host_id"):
            marks.append("Host")
        if p["player_id"] == me:
            marks.append("du")
        ready = "✓ bereit" if p.get("ready") else "✗ nicht bereit"
        extra = f" ({', '.join(marks)})" if marks else ""
        lines.append(f"  {i}. {p['name']}{extra:<12} {ready}")
    if status == "waiting":
        lines.append(f"Code für Mitspieler: {lobby.get('lobby_id')}  ·  {LOBBY_HINT}")
    return "\n".join(lines)


# --- Tisch ------------------------------------------------------------------------
def _slots(slots: list[bool]) -> str:
    return " ".join(f"[{i}]" if filled else "[ ]" for i, filled in enumerate(slots))


def hints(view: dict[str, Any]) -> str:
    """Passende Befehle für die aktuelle Phase. Ob ein Befehl gültig ist,
    entscheidet trotzdem der Server."""
    phase, me = view["phase"], view["you"]
    active = view.get("active_player") == me
    top = view.get("discard_top")
    throw = f"x <slot> zwischenwerfen (Ablage {top})" if top else ""
    if phase == "initial_peek":
        return ("p <slot> <slot>  zwei eigene Karten ansehen, z. B. p 0 1"
                if view.get("needs_initial_peek")
                else "Warte, bis alle ihre Startkarten angesehen haben.")
    if phase == "finished":
        return "Neue Partie: neu oder join <code>"
    if not active:
        return f"Warte auf den Zug · {throw}" if throw else "Warte auf den Zug"
    if phase == "await_draw":
        options = ["z ziehen", "a Ablage nehmen"]
        if view.get("knocker") is None:
            options.append("klopf klopfen")
        return " · ".join(options + ([throw] if throw else []))
    if phase == "await_decision":
        return "t <slot> gegen eigene Karte tauschen · w ablegen"
    if phase == "await_special":
        if view.get("pending_special") == "jack":
            return "b <sp> <slot> <sp> <slot>  zwei Karten tauschen · s überspringen"
        return "k <sp> <slot>  eine Karte ansehen · s überspringen"
    return ""


def render_table(view: dict[str, Any], names: Names,
                 remaining: Optional[float] = None) -> str:
    phase = view["phase"]
    head = [f"Partie {view.get('game_id')}"]
    if view.get("turn_number"):
        head.append(f"Zug {view['turn_number']}")
    if view.get("active_player"):
        head.append(f"{names(view['active_player'])} ist am Zug")
    elif phase == "initial_peek":
        head.append("Startkarten ansehen")
    if remaining is not None and phase != "finished":
        head.append(f"noch {remaining:.0f} s")
    size = view.get("discard_size", 0)
    lines = [LINE, " · ".join(head),
             f"Ziehstapel: {view['deck_size']}   Ablage: {view.get('discard_top') or '-'}"
             f" ({size} {'Karte' if size == 1 else 'Karten'})"]
    for i, p in enumerate(view["players"], 1):
        marks = []
        if p["player_id"] == view.get("active_player"):
            marks.append("◀ am Zug")
        if p["player_id"] == view.get("knocker"):
            marks.append("(hat geklopft)")
        lines.append(f"  {i}. {names(p['player_id']):<16} {_slots(p['slots'])}  "
                     + " ".join(marks))
    if view.get("drawn_card"):
        lines.append(f"Gezogen: {view['drawn_card']}")
    elif view.get("someone_holds_card") and view.get("active_player"):
        lines.append(f"{names(view['active_player'])} hält eine gezogene Karte.")
    if phase == "finished" and view.get("result"):
        lines.append(render_result(view, names))
    hint = hints(view)
    if hint:
        lines.append("» " + hint)
    return "\n".join(lines)


def render_result(view: dict[str, Any], names: Names) -> str:
    result = view["result"]
    winners = set(result.get("winners", []))
    lines = ["Auflösung:"]
    for p in view["players"]:
        pid = p["player_id"]
        cards = " ".join(c for c in result["hands"].get(pid, []) if c)
        mark = "  ★ Gewinner" if pid in winners else ""
        lines.append(f"  {names(pid):<16} {cards:<28} = {result['scores'].get(pid)} Punkte{mark}")
    return "\n".join(lines)


def player_numbers(view: Optional[dict[str, Any]]) -> list[str]:
    """player_ids in Tabellenreihenfolge (Nummer 1 = Index 0)."""
    return [p["player_id"] for p in (view or {}).get("players", [])]
