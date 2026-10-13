"""Datenmodell des Spiels: Karten, Spieler und Spielzustand."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .config import RulesConfig

SUITS = ("S", "H", "D", "C")  # Pik, Herz, Karo, Kreuz
SUIT_SYMBOLS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
# J = Bube, Q = Dame (Queen), K = König


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str

    @property
    def value(self) -> int:
        """Punktwert: Dame -1, Ass 1, Zahlen nach Augenwert, Bube/König 10."""
        if self.rank == "Q":
            return -1
        if self.rank == "A":
            return 1
        if self.rank in ("J", "K"):
            return 10
        return int(self.rank)

    def to_str(self) -> str:
        return f"{self.rank}{SUIT_SYMBOLS[self.suit]}"

    def __str__(self) -> str:
        return self.to_str()


def full_deck() -> list[Card]:
    """52er-Blatt in fester Reihenfolge (gemischt wird mit Seed in der Engine)."""
    return [Card(rank, suit) for suit in SUITS for rank in RANKS]


@dataclass(frozen=True)
class CardRef:
    """Verweis auf eine Kartenposition: Spieler + Slot-Index."""

    player_id: str
    slot: int


class Phase(str, Enum):
    LOBBY = "lobby"                    # Spieler stehen fest, Spiel noch nicht gestartet
    INITIAL_PEEK = "initial_peek"      # jeder sieht sich zwei eigene Karten an
    AWAIT_DRAW = "await_draw"          # aktiver Spieler zieht oder klopft
    AWAIT_DECISION = "await_decision"  # aktiver Spieler tauscht oder legt ab
    AWAIT_SPECIAL = "await_special"    # Bube/König wurde abgelegt
    FINISHED = "finished"


class SpecialType(str, Enum):
    JACK = "jack"  # Bube: zwei Karten tauschen
    KING = "king"  # König: eine Karte ansehen


@dataclass
class PlayerState:
    player_id: str
    name: str
    # Feste Slots: eine Karte oder None (leerer Slot nach Zwischenwerfen).
    # Slot-Indizes bleiben stabil, damit Verweise (CardRef) gültig bleiben.
    hand: list[Optional[Card]] = field(default_factory=list)
    has_peeked: bool = False

    def occupied_slots(self) -> list[int]:
        return [i for i, c in enumerate(self.hand) if c is not None]

    def card_count(self) -> int:
        return len(self.occupied_slots())


@dataclass
class GameState:
    game_id: str
    players: list[PlayerState]
    config: RulesConfig = field(default_factory=RulesConfig)

    phase: Phase = Phase.LOBBY
    seed: Optional[str] = None
    deck: list[Card] = field(default_factory=list)     # oberste Karte = letztes Element
    discard: list[Card] = field(default_factory=list)  # oberste Karte = letztes Element
    reshuffle_count: int = 0

    current_index: int = 0
    turn_number: int = 0
    drawn_card: Optional[Card] = None
    drawn_from: Optional[str] = None  # "deck" oder "discard"
    pending_special: Optional[SpecialType] = None
    knocker_id: Optional[str] = None

    scores: dict[str, int] = field(default_factory=dict)
    winners: list[str] = field(default_factory=list)

    # Duplikatschutz und Versionszähler (Anzahl angewendeter Aktionen)
    processed_action_ids: set[str] = field(default_factory=set)
    version: int = 0

    # --- Hilfsfunktionen -------------------------------------------------
    @property
    def current_player(self) -> PlayerState:
        return self.players[self.current_index]

    def get_player(self, player_id: str) -> Optional[PlayerState]:
        for p in self.players:
            if p.player_id == player_id:
                return p
        return None

    def discard_top(self) -> Optional[Card]:
        return self.discard[-1] if self.discard else None

    def card_at(self, ref: CardRef) -> Optional[Card]:
        p = self.get_player(ref.player_id)
        if p is None or not (0 <= ref.slot < len(p.hand)):
            return None
        return p.hand[ref.slot]


def create_game(
    game_id: str,
    players: list[tuple[str, str]],
    config: Optional[RulesConfig] = None,
) -> GameState:
    """Legt eine neue Partie in der Phase LOBBY an.

    `players` ist eine Liste aus (player_id, name). Beitritt und Ready-Status
    verwaltet später der Lobby-Service; die Engine bekommt nur die fertige
    Spielerliste.
    """
    config = config or RulesConfig()
    if not (config.min_players <= len(players) <= config.max_players):
        raise ValueError(
            f"Spieleranzahl muss zwischen {config.min_players} und "
            f"{config.max_players} liegen."
        )
    ids = [pid for pid, _ in players]
    if len(set(ids)) != len(ids):
        raise ValueError("Spieler-IDs müssen eindeutig sein.")
    return GameState(
        game_id=game_id,
        players=[PlayerState(player_id=pid, name=name) for pid, name in players],
        config=config,
    )
