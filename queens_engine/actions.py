"""Aktionen (Befehle), die Spieler oder das System an die Engine schicken.

Jede Aktion trägt eine eindeutige `action_id` (Duplikatschutz) und die
`player_id` des Absenders. Aktionen sind serialisierbar, damit sie später
übers Netzwerk geschickt und als Raft-Log-Einträge gespeichert werden können.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .models import CardRef

SYSTEM_PLAYER = "system"


def new_action_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True, kw_only=True)
class Action:
    player_id: str
    action_id: str = field(default_factory=new_action_id)


# --- Spielstart -------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class StartGame(Action):
    """Mischt mit dem Seed und teilt aus. Der Seed muss Teil der Aktion sein,
    damit alle Replikate exakt gleich mischen."""

    seed: str


@dataclass(frozen=True, kw_only=True)
class PeekInitial(Action):
    """Zu Beginn zwei eigene Karten ansehen."""

    slots: tuple[int, ...]


# --- Zug des aktiven Spielers -----------------------------------------------
@dataclass(frozen=True, kw_only=True)
class DrawFromDeck(Action):
    pass


@dataclass(frozen=True, kw_only=True)
class TakeFromDiscard(Action):
    pass


@dataclass(frozen=True, kw_only=True)
class SwapWithHand(Action):
    """Gezogene Karte gegen eigene Karte im Slot tauschen; die alte Karte
    wird abgelegt."""

    slot: int


@dataclass(frozen=True, kw_only=True)
class DiscardDrawn(Action):
    """Gezogene Karte direkt ablegen."""


@dataclass(frozen=True, kw_only=True)
class UseJack(Action):
    """Bube: zwei beliebige Karten (blind) vertauschen."""

    first: CardRef
    second: CardRef


@dataclass(frozen=True, kw_only=True)
class UseKing(Action):
    """König: eine beliebige Karte ansehen."""

    target: CardRef


@dataclass(frozen=True, kw_only=True)
class SkipSpecial(Action):
    """Sonderaktion verfallen lassen."""


@dataclass(frozen=True, kw_only=True)
class Knock(Action):
    """Klopfen statt Ziehen: Zug endet, beim nächsten eigenen Zug wird
    aufgedeckt."""


# --- Jederzeit möglich ------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class ThrowIn(Action):
    """Zwischenwerfen: eigene Karte auf gleichrangige Ablagekarte werfen."""

    slot: int


# --- System -----------------------------------------------------------------
@dataclass(frozen=True, kw_only=True)
class TurnTimeout(Action):
    """Vom Leader erzeugt, wenn der aktive Spieler zu lange braucht.
    Die Zugnummer verhindert, dass ein verspäteter Timeout einen neueren
    Zug abbricht."""

    turn_number: int


ACTION_TYPES: dict[str, type[Action]] = {
    cls.__name__: cls
    for cls in (
        StartGame, PeekInitial, DrawFromDeck, TakeFromDiscard, SwapWithHand,
        DiscardDrawn, UseJack, UseKing, SkipSpecial, Knock, ThrowIn, TurnTimeout,
    )
}


# --- Serialisierung (für Netzwerk und Raft-Log) -----------------------------
def action_to_dict(action: Action) -> dict[str, Any]:
    data = asdict(action)
    data["type"] = type(action).__name__
    return data


def action_from_dict(data: dict[str, Any]) -> Action:
    data = dict(data)
    type_name = data.pop("type", None)
    cls = ACTION_TYPES.get(type_name)
    if cls is None:
        raise ValueError(f"Unbekannter Aktionstyp: {type_name!r}")
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name in ("first", "second", "target") and isinstance(value, dict):
            value = CardRef(player_id=value["player_id"], slot=int(value["slot"]))
        elif f.name == "slots":
            value = tuple(int(s) for s in value)
        kwargs[f.name] = value
    return cls(**kwargs)
