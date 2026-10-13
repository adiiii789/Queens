"""Zustandsübergänge: apply(state, action) -> (neuer Zustand, Ereignisse).

Die Engine ist deterministisch: kein Netzwerk, keine Uhrzeit, Zufall nur
über den Seed aus StartGame. Gleicher Zustand + gleiche Aktion ergibt immer
dasselbe Ergebnis. Genau diese Eigenschaft braucht Raft, damit alle
Replikate denselben Zustand erreichen.
"""

from __future__ import annotations

import copy
import random

from . import scoring
from .actions import (
    Action, DiscardDrawn, DrawFromDeck, Knock, PeekInitial, SkipSpecial,
    StartGame, SwapWithHand, TakeFromDiscard, ThrowIn, TurnTimeout, UseJack,
    UseKing,
)
from .events import Event, private, public
from .models import Card, GameState, Phase, SpecialType, full_deck
from .rules import validate


def apply(state: GameState, action: Action) -> tuple[GameState, list[Event]]:
    """Prüft und wendet eine Aktion an. Wirft RuleViolation bei Regelverstoß.

    Der übergebene Zustand wird nicht verändert. Bereits verarbeitete
    Aktionen (gleiche action_id) werden ignoriert.
    """
    if action.action_id in state.processed_action_ids:
        return state, []

    validate(state, action)

    new_state = copy.deepcopy(state)
    handler = _HANDLERS[type(action)]
    events = handler(new_state, action)
    new_state.processed_action_ids.add(action.action_id)
    new_state.version += 1
    return new_state, events


# --- interne Hilfsfunktionen -----------------------------------------------
def _rng(state: GameState) -> random.Random:
    # String-Seeds sind in Python plattform- und laufübergreifend stabil.
    return random.Random(f"{state.seed}:{state.reshuffle_count}")


def _ensure_deck(state: GameState, events: list[Event]) -> bool:
    """Mischt bei leerem Ziehstapel den Ablagestapel (ohne oberste Karte) neu."""
    if state.deck:
        return True
    if len(state.discard) <= 1:
        return False
    top = state.discard.pop()
    state.deck = state.discard
    state.discard = [top]
    state.reshuffle_count += 1
    _rng(state).shuffle(state.deck)
    events.append(public("DeckReshuffled", deck_size=len(state.deck)))
    return True


def _start_turn(state: GameState, events: list[Event]) -> None:
    state.phase = Phase.AWAIT_DRAW
    state.turn_number += 1
    events.append(public(
        "TurnStarted",
        player_id=state.current_player.player_id,
        turn_number=state.turn_number,
    ))


def _end_turn(state: GameState, events: list[Event]) -> None:
    state.pending_special = None
    state.drawn_card = None
    state.drawn_from = None
    state.current_index = (state.current_index + 1) % len(state.players)
    if state.knocker_id == state.current_player.player_id:
        _finish(state, events)
    else:
        _start_turn(state, events)


def _after_discard(state: GameState, card: Card, events: list[Event]) -> None:
    """Ablegen durch den aktiven Spieler: Bube/König lösen Sonderaktion aus."""
    special = {"J": SpecialType.JACK, "K": SpecialType.KING}.get(card.rank)
    if special is None:
        _end_turn(state, events)
        return
    state.pending_special = special
    state.phase = Phase.AWAIT_SPECIAL
    events.append(public(
        "SpecialAvailable",
        player_id=state.current_player.player_id,
        special=special.value,
    ))


def _finish(state: GameState, events: list[Event]) -> None:
    state.phase = Phase.FINISHED
    state.scores = scoring.compute_scores(state)
    state.winners = scoring.determine_winners(state.scores)
    events.append(public(
        "GameFinished",
        hands={p.player_id: [c.to_str() if c else None for c in p.hand]
               for p in state.players},
        scores=state.scores,
        winners=state.winners,
    ))


# --- Handler je Aktion --------------------------------------------------------
def _start_game(state: GameState, a: StartGame) -> list[Event]:
    events: list[Event] = []
    state.seed = a.seed
    state.deck = full_deck()
    _rng(state).shuffle(state.deck)
    for player in state.players:
        player.hand = [state.deck.pop() for _ in range(state.config.hand_size)]
    state.discard = [state.deck.pop()]
    state.phase = Phase.INITIAL_PEEK
    events.append(public(
        "GameStarted",
        players=[p.player_id for p in state.players],
        discard_top=state.discard_top().to_str(),
    ))
    return events


def _peek_initial(state: GameState, a: PeekInitial) -> list[Event]:
    player = state.get_player(a.player_id)
    player.has_peeked = True
    events = [
        private(a.player_id, "CardsPeeked",
                cards={slot: player.hand[slot].to_str() for slot in a.slots}),
        public("PlayerPeeked", player_id=a.player_id),
    ]
    if all(p.has_peeked for p in state.players):
        state.current_index = 0
        _start_turn(state, events)
    return events


def _draw_from_deck(state: GameState, a: DrawFromDeck) -> list[Event]:
    events: list[Event] = []
    _ensure_deck(state, events)
    card = state.deck.pop()
    state.drawn_card = card
    state.drawn_from = "deck"
    state.phase = Phase.AWAIT_DECISION
    events.append(private(a.player_id, "CardDrawn", card=card.to_str()))
    events.append(public("PlayerDrew", player_id=a.player_id, source="deck"))
    return events


def _take_from_discard(state: GameState, a: TakeFromDiscard) -> list[Event]:
    card = state.discard.pop()
    state.drawn_card = card
    state.drawn_from = "discard"
    state.phase = Phase.AWAIT_DECISION
    return [public("PlayerDrew", player_id=a.player_id, source="discard",
                   card=card.to_str())]


def _swap_with_hand(state: GameState, a: SwapWithHand) -> list[Event]:
    player = state.get_player(a.player_id)
    old = player.hand[a.slot]
    player.hand[a.slot] = state.drawn_card
    state.drawn_card = None
    state.discard.append(old)
    events = [public("CardSwapped", player_id=a.player_id, slot=a.slot,
                     discarded=old.to_str())]
    _after_discard(state, old, events)
    return events


def _discard_drawn(state: GameState, a: DiscardDrawn) -> list[Event]:
    card = state.drawn_card
    state.drawn_card = None
    state.discard.append(card)
    events = [public("CardDiscarded", player_id=a.player_id, card=card.to_str())]
    _after_discard(state, card, events)
    return events


def _use_jack(state: GameState, a: UseJack) -> list[Event]:
    p1 = state.get_player(a.first.player_id)
    p2 = state.get_player(a.second.player_id)
    p1.hand[a.first.slot], p2.hand[a.second.slot] = (
        p2.hand[a.second.slot], p1.hand[a.first.slot])
    events = [public(
        "CardsSwappedByJack", player_id=a.player_id,
        first={"player_id": a.first.player_id, "slot": a.first.slot},
        second={"player_id": a.second.player_id, "slot": a.second.slot},
    )]
    _end_turn(state, events)
    return events


def _use_king(state: GameState, a: UseKing) -> list[Event]:
    card = state.card_at(a.target)
    events = [
        private(a.player_id, "CardRevealedByKing",
                target={"player_id": a.target.player_id, "slot": a.target.slot},
                card=card.to_str()),
        public("PlayerPeekedByKing", player_id=a.player_id,
               target={"player_id": a.target.player_id, "slot": a.target.slot}),
    ]
    _end_turn(state, events)
    return events


def _skip_special(state: GameState, a: SkipSpecial) -> list[Event]:
    events = [public("SpecialSkipped", player_id=a.player_id)]
    _end_turn(state, events)
    return events


def _knock(state: GameState, a: Knock) -> list[Event]:
    state.knocker_id = a.player_id
    events = [public("PlayerKnocked", player_id=a.player_id)]
    _end_turn(state, events)
    return events


def _throw_in(state: GameState, a: ThrowIn) -> list[Event]:
    player = state.get_player(a.player_id)
    card = player.hand[a.slot]
    top = state.discard_top()
    events: list[Event] = []

    if card.rank == top.rank:
        player.hand[a.slot] = None
        state.discard.append(card)
        events.append(public("ThrowInSucceeded", player_id=a.player_id,
                             slot=a.slot, card=card.to_str()))
        return events

    # Falsch geworfen: Karte wird allen gezeigt und bleibt liegen, dazu Strafkarte.
    events.append(public("ThrowInFailed", player_id=a.player_id,
                         slot=a.slot, card=card.to_str()))
    if state.config.penalty_on_wrong_throw_in and _ensure_deck(state, events):
        player.hand.append(state.deck.pop())
        events.append(public("PenaltyCard", player_id=a.player_id,
                             slot=len(player.hand) - 1))
    return events


def _turn_timeout(state: GameState, a: TurnTimeout) -> list[Event]:
    player_id = state.current_player.player_id
    events = [public("TurnTimedOut", player_id=player_id,
                     turn_number=state.turn_number)]
    if state.phase == Phase.AWAIT_DECISION and state.drawn_card is not None:
        # Gezogene Karte wird ohne Sonderaktion abgelegt.
        state.discard.append(state.drawn_card)
        events.append(public("CardDiscarded", player_id=player_id,
                             card=state.drawn_card.to_str()))
    _end_turn(state, events)
    return events


_HANDLERS = {
    StartGame: _start_game,
    PeekInitial: _peek_initial,
    DrawFromDeck: _draw_from_deck,
    TakeFromDiscard: _take_from_discard,
    SwapWithHand: _swap_with_hand,
    DiscardDrawn: _discard_drawn,
    UseJack: _use_jack,
    UseKing: _use_king,
    SkipSpecial: _skip_special,
    Knock: _knock,
    ThrowIn: _throw_in,
    TurnTimeout: _turn_timeout,
}
