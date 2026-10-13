"""Regelprüfung: validate(state, action) wirft RuleViolation, wenn eine
Aktion im aktuellen Zustand nicht erlaubt ist. Verändert den Zustand nicht."""

from __future__ import annotations

from .actions import (
    SYSTEM_PLAYER, Action, DiscardDrawn, DrawFromDeck, Knock, PeekInitial,
    SkipSpecial, StartGame, SwapWithHand, TakeFromDiscard, ThrowIn,
    TurnTimeout, UseJack, UseKing,
)
from .models import CardRef, GameState, Phase, SpecialType

TURN_PHASES = (Phase.AWAIT_DRAW, Phase.AWAIT_DECISION, Phase.AWAIT_SPECIAL)


class RuleViolation(Exception):
    """Aktion verstößt gegen die Spielregeln."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuleViolation(message)


def _require_phase(state: GameState, *phases: Phase) -> None:
    _require(
        state.phase in phases,
        f"Aktion in Phase '{state.phase.value}' nicht erlaubt.",
    )


def _require_active(state: GameState, action: Action) -> None:
    _require(
        action.player_id == state.current_player.player_id,
        "Du bist nicht am Zug.",
    )


def _require_player(state: GameState, action: Action) -> None:
    _require(state.get_player(action.player_id) is not None, "Unbekannter Spieler.")


def _require_occupied(state: GameState, ref: CardRef) -> None:
    _require(state.get_player(ref.player_id) is not None, "Unbekannter Zielspieler.")
    _require(state.card_at(ref) is not None, "An dieser Position liegt keine Karte.")


def _is_locked(state: GameState, player_id: str) -> bool:
    return state.config.knocker_hand_locked and state.knocker_id == player_id


def can_draw_from_deck(state: GameState) -> bool:
    # Ist der Ziehstapel leer, wird der Ablagestapel (ohne oberste Karte) gemischt.
    return bool(state.deck) or len(state.discard) > 1


def validate(state: GameState, action: Action) -> None:
    if isinstance(action, StartGame):
        _require_phase(state, Phase.LOBBY)
        _require_player(state, action)
        _require(bool(action.seed), "Seed fehlt.")
        return

    if isinstance(action, TurnTimeout):
        _require(action.player_id == SYSTEM_PLAYER, "Timeouts erzeugt nur das System.")
        _require_phase(state, *TURN_PHASES)
        _require(action.turn_number == state.turn_number, "Veralteter Timeout.")
        return

    _require_player(state, action)

    if isinstance(action, PeekInitial):
        _require_phase(state, Phase.INITIAL_PEEK)
        player = state.get_player(action.player_id)
        _require(not player.has_peeked, "Karten wurden bereits angesehen.")
        _require(
            len(action.slots) == state.config.initial_peek_count,
            f"Genau {state.config.initial_peek_count} Karten wählen.",
        )
        _require(len(set(action.slots)) == len(action.slots), "Slots doppelt gewählt.")
        for slot in action.slots:
            _require(0 <= slot < len(player.hand), f"Ungültiger Slot {slot}.")
        return

    if isinstance(action, ThrowIn):
        _require_phase(state, *TURN_PHASES)
        _require(not _is_locked(state, action.player_id),
                 "Nach dem Klopfen sind deine Karten gesperrt.")
        _require_occupied(state, CardRef(action.player_id, action.slot))
        _require(state.discard_top() is not None, "Ablagestapel ist leer.")
        return

    # Ab hier: nur der aktive Spieler
    _require_active(state, action)

    if isinstance(action, Knock):
        _require_phase(state, Phase.AWAIT_DRAW)
        _require(state.knocker_id is None, "Es wurde bereits geklopft.")
        return

    if isinstance(action, DrawFromDeck):
        _require_phase(state, Phase.AWAIT_DRAW)
        _require(can_draw_from_deck(state), "Keine Karten mehr zum Ziehen.")
        return

    if isinstance(action, TakeFromDiscard):
        _require_phase(state, Phase.AWAIT_DRAW)
        _require(state.discard_top() is not None, "Ablagestapel ist leer.")
        return

    if isinstance(action, SwapWithHand):
        _require_phase(state, Phase.AWAIT_DECISION)
        _require_occupied(state, CardRef(action.player_id, action.slot))
        return

    if isinstance(action, DiscardDrawn):
        _require_phase(state, Phase.AWAIT_DECISION)
        return

    if isinstance(action, UseJack):
        _require_phase(state, Phase.AWAIT_SPECIAL)
        _require(state.pending_special == SpecialType.JACK, "Keine Bube-Aktion offen.")
        _require(action.first != action.second, "Zwei verschiedene Karten wählen.")
        for ref in (action.first, action.second):
            _require_occupied(state, ref)
            _require(not _is_locked(state, ref.player_id),
                     "Karten des klopfenden Spielers sind gesperrt.")
        return

    if isinstance(action, UseKing):
        _require_phase(state, Phase.AWAIT_SPECIAL)
        _require(state.pending_special == SpecialType.KING, "Keine König-Aktion offen.")
        _require_occupied(state, action.target)
        return

    if isinstance(action, SkipSpecial):
        _require_phase(state, Phase.AWAIT_SPECIAL)
        return

    raise RuleViolation(f"Unbekannte Aktion: {type(action).__name__}")
