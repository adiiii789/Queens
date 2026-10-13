"""Unit-Tests der Spiellogik. Ausführen mit:  python -m pytest"""

import pytest

from queens_engine import (
    SYSTEM_PLAYER, Card, CardRef, DiscardDrawn, DrawFromDeck, Knock,
    PeekInitial, Phase, RuleViolation, SkipSpecial, StartGame, SwapWithHand,
    TakeFromDiscard, ThrowIn, TurnTimeout, UseJack, UseKing, action_from_dict,
    action_to_dict, apply, create_game, player_view,
)


def C(text: str) -> Card:
    """'QH' -> Dame Herz, '10S' -> Zehn Pik."""
    return Card(text[:-1], text[-1])


def started(n_players=3, seed="test"):
    state = create_game("g", [(f"p{i}", f"P{i}") for i in range(1, n_players + 1)])
    state, _ = apply(state, StartGame(player_id="p1", seed=seed))
    for p in state.players:
        state, _ = apply(state, PeekInitial(player_id=p.player_id, slots=(0, 1)))
    return state


def set_cards(state, hands=None, discard=None, deck=None):
    """Feste Karten für reproduzierbare Tests setzen."""
    for pid, cards in (hands or {}).items():
        state.get_player(pid).hand = [C(c) if c else None for c in cards]
    if discard is not None:
        state.discard = [C(c) for c in discard]
    if deck is not None:
        state.deck = [C(c) for c in deck]  # letztes Element = oberste Karte
    return state


def test_start_deals_cards_and_starts_peek_phase():
    state = create_game("g", [("p1", "A"), ("p2", "B")])
    state, events = apply(state, StartGame(player_id="p1", seed="x"))
    assert state.phase == Phase.INITIAL_PEEK
    assert all(len(p.hand) == 4 for p in state.players)
    assert len(state.discard) == 1
    assert len(state.deck) == 52 - 8 - 1


def test_same_seed_gives_same_state():
    a, b = started(seed="abc"), started(seed="abc")
    assert [p.hand for p in a.players] == [p.hand for p in b.players]
    assert a.deck == b.deck


def test_peek_is_private_and_turns_start_after_all_peeked():
    state = create_game("g", [("p1", "A"), ("p2", "B")])
    state, _ = apply(state, StartGame(player_id="p1", seed="x"))
    state, events = apply(state, PeekInitial(player_id="p1", slots=(0, 3)))
    peek = next(e for e in events if e.type == "CardsPeeked")
    assert peek.audience == "p1"
    assert state.phase == Phase.INITIAL_PEEK
    state, _ = apply(state, PeekInitial(player_id="p2", slots=(1, 2)))
    assert state.phase == Phase.AWAIT_DRAW
    assert state.current_player.player_id == "p1"


def test_only_active_player_may_draw():
    state = started()
    with pytest.raises(RuleViolation):
        apply(state, DrawFromDeck(player_id="p2"))


def test_draw_and_swap_puts_old_card_on_discard():
    state = set_cards(started(), hands={"p1": ["5H", "7S", "9D", "2C"]},
                      discard=["3H"], deck=["QH"])
    state, _ = apply(state, DrawFromDeck(player_id="p1"))
    state, _ = apply(state, SwapWithHand(player_id="p1", slot=0))
    assert state.get_player("p1").hand[0] == C("QH")
    assert state.discard_top() == C("5H")
    assert state.current_player.player_id == "p2"


def test_discarding_jack_allows_swap_between_players():
    state = set_cards(started(), hands={"p1": ["5H", "7S", "9D", "2C"],
                                        "p2": ["AH", "AS", "AD", "AC"]},
                      deck=["JH"])
    state, _ = apply(state, DrawFromDeck(player_id="p1"))
    state, _ = apply(state, DiscardDrawn(player_id="p1"))
    assert state.phase == Phase.AWAIT_SPECIAL
    state, _ = apply(state, UseJack(player_id="p1", first=CardRef("p1", 0),
                                    second=CardRef("p2", 0)))
    assert state.get_player("p1").hand[0] == C("AH")
    assert state.get_player("p2").hand[0] == C("5H")
    assert state.current_player.player_id == "p2"


def test_jack_taken_from_discard_and_discarded_again_triggers_special():
    state = set_cards(started(), discard=["2H", "JS"])
    state, _ = apply(state, TakeFromDiscard(player_id="p1"))
    state, _ = apply(state, DiscardDrawn(player_id="p1"))
    assert state.phase == Phase.AWAIT_SPECIAL


def test_king_reveals_card_only_to_active_player():
    state = set_cards(started(), hands={"p2": ["QH", "7S", "9D", "2C"]},
                      deck=["KS"])
    state, _ = apply(state, DrawFromDeck(player_id="p1"))
    state, _ = apply(state, DiscardDrawn(player_id="p1"))
    state, events = apply(state, UseKing(player_id="p1", target=CardRef("p2", 0)))
    reveal = next(e for e in events if e.type == "CardRevealedByKing")
    assert reveal.audience == "p1" and reveal.data["card"] == "Q♥"


def test_skip_special_ends_turn():
    state = set_cards(started(), deck=["KS"])
    state, _ = apply(state, DrawFromDeck(player_id="p1"))
    state, _ = apply(state, DiscardDrawn(player_id="p1"))
    state, _ = apply(state, SkipSpecial(player_id="p1"))
    assert state.current_player.player_id == "p2"


def test_throw_in_matching_rank_out_of_turn():
    state = set_cards(started(), hands={"p3": ["9H", "7S", "9D", "2C"]},
                      discard=["9S"])
    state, _ = apply(state, ThrowIn(player_id="p3", slot=0))
    assert state.get_player("p3").hand[0] is None
    assert state.discard_top() == C("9H")
    # zweiter Wurf mit gleichem Rang ist ebenfalls gültig
    state, _ = apply(state, ThrowIn(player_id="p3", slot=2))
    assert state.get_player("p3").card_count() == 2


def test_wrong_throw_in_gives_penalty_card():
    state = set_cards(started(), hands={"p2": ["5H", "7S", "9D", "2C"]},
                      discard=["9S"], deck=["4H"])
    state, events = apply(state, ThrowIn(player_id="p2", slot=0))
    p2 = state.get_player("p2")
    assert p2.hand[0] == C("5H")          # Karte bleibt liegen
    assert len(p2.hand) == 5 and p2.hand[4] == C("4H")
    assert any(e.type == "ThrowInFailed" for e in events)


def test_knock_ends_game_when_knocker_is_next():
    state = set_cards(started(), hands={"p1": ["QH", "AS", "2D", "AC"],
                                        "p2": ["KH", "KS", "KD", "KC"],
                                        "p3": ["5H", "5S", "5D", "5C"]},
                      deck=["3H", "3S"])
    state, _ = apply(state, Knock(player_id="p1"))
    state, _ = apply(state, DrawFromDeck(player_id="p2"))
    state, _ = apply(state, DiscardDrawn(player_id="p2"))
    state, _ = apply(state, DrawFromDeck(player_id="p3"))
    state, _ = apply(state, DiscardDrawn(player_id="p3"))
    assert state.phase == Phase.FINISHED
    assert state.scores == {"p1": 3, "p2": 40, "p3": 20}
    assert state.winners == ["p1"]


def test_knocker_cards_are_locked():
    state = set_cards(started(), hands={"p1": ["9H", "7S", "9D", "2C"]},
                      discard=["9S"])
    state, _ = apply(state, Knock(player_id="p1"))
    with pytest.raises(RuleViolation):
        apply(state, ThrowIn(player_id="p1", slot=0))


def test_only_one_knock_per_game():
    state = started()
    state, _ = apply(state, Knock(player_id="p1"))
    with pytest.raises(RuleViolation):
        apply(state, Knock(player_id="p2"))


def test_duplicate_action_is_ignored():
    state = started()
    action = DrawFromDeck(player_id="p1")
    state, _ = apply(state, action)
    state2, events = apply(state, action)
    assert events == [] and state2 is state


def test_reshuffle_when_deck_empty():
    state = set_cards(started(), discard=["2H", "3H", "4H", "5H"], deck=[])
    state, events = apply(state, DrawFromDeck(player_id="p1"))
    assert any(e.type == "DeckReshuffled" for e in events)
    assert state.discard == [C("5H")]


def test_timeout_discards_drawn_card_and_rejects_stale_timeout():
    state = started()
    turn = state.turn_number
    state, _ = apply(state, DrawFromDeck(player_id="p1"))
    state, _ = apply(state, TurnTimeout(player_id=SYSTEM_PLAYER, turn_number=turn))
    assert state.current_player.player_id == "p2"
    with pytest.raises(RuleViolation):
        apply(state, TurnTimeout(player_id=SYSTEM_PLAYER, turn_number=turn))


def test_view_hides_all_hand_cards():
    state = started()
    view = player_view(state, "p1")
    assert all(isinstance(s, bool) for p in view["players"] for s in p["slots"])
    assert "result" not in view


def test_action_serialization_roundtrip():
    action = UseJack(player_id="p1", first=CardRef("p1", 0), second=CardRef("p2", 3))
    assert action_from_dict(action_to_dict(action)) == action


def test_full_random_game_terminates():
    """Simuliert eine komplette Partie mit 8 Spielern."""
    import random
    rng = random.Random(1)
    state = started(n_players=8, seed="sim")
    for _ in range(500):
        if state.phase == Phase.FINISHED:
            break
        pid = state.current_player.player_id
        if state.phase == Phase.AWAIT_DRAW:
            action = (Knock(player_id=pid) if state.turn_number > 30
                      and state.knocker_id is None
                      else DrawFromDeck(player_id=pid))
        elif state.phase == Phase.AWAIT_DECISION:
            slots = state.current_player.occupied_slots()
            action = (SwapWithHand(player_id=pid, slot=rng.choice(slots))
                      if slots and rng.random() < 0.5 else DiscardDrawn(player_id=pid))
        else:
            action = SkipSpecial(player_id=pid)
        state, _ = apply(state, action)
    assert state.phase == Phase.FINISHED
    total = sum(len([c for c in p.hand if c]) for p in state.players)
    assert total + len(state.deck) + len(state.discard) == 52
