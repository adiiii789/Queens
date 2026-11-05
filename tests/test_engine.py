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


def test_peek_timeout_skips_late_players_and_starts_first_turn():
    state = create_game("g", [("p1", "A"), ("p2", "B"), ("p3", "C")])
    state, _ = apply(state, StartGame(player_id="p1", seed="x"))
    state, _ = apply(state, PeekInitial(player_id="p2", slots=(0, 1)))
    state, events = apply(state, TurnTimeout(player_id=SYSTEM_PLAYER, turn_number=0))
    skipped = next(e for e in events if e.type == "PeekTimedOut")
    assert skipped.data["player_ids"] == ["p1", "p3"]
    assert state.phase == Phase.AWAIT_DRAW and state.turn_number == 1
    assert state.current_player.player_id == "p1"
    with pytest.raises(RuleViolation):
        apply(state, PeekInitial(player_id="p1", slots=(0, 1)))
    # derselbe Timeout ein zweites Mal (z. B. nach Leader-Wechsel) ist veraltet
    with pytest.raises(RuleViolation):
        apply(state, TurnTimeout(player_id=SYSTEM_PLAYER, turn_number=0))


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


# --- weitere Punkte aus „Fertig, wenn“ (Branch-Plan, feature/Spiellogik) ------
def _without_open_cards(view):
    """Sicht ohne die Felder, die Kartenwerte zeigen dürfen."""
    import json
    rest = {k: v for k, v in view.items() if k not in ("discard_top", "drawn_card")}
    return json.dumps(rest, ensure_ascii=False)


def test_views_never_contain_hand_card_values():
    state = create_game("g", [("p1", "A"), ("p2", "B")])
    state, events = apply(state, StartGame(player_id="p1", seed="x"))
    assert all(e.audience is None for e in events)
    assert "hands" not in events[0].data
    for peeking in ("p1", "p2"):
        for pid in ("p1", "p2"):
            assert not any(s in _without_open_cards(player_view(state, pid))
                           for s in "♠♥♦♣")
        state, _ = apply(state, PeekInitial(player_id=peeking, slots=(0, 1)))
    assert state.phase == Phase.AWAIT_DRAW
    assert not any(s in _without_open_cards(player_view(state, "p1")) for s in "♠♥♦♣")


def test_drawn_card_visible_only_to_active_player():
    state = set_cards(started(), deck=["7H"])
    state, _ = apply(state, DrawFromDeck(player_id="p1"))
    assert player_view(state, "p1")["drawn_card"] == "7♥"
    assert player_view(state, "p2")["drawn_card"] is None
    assert player_view(state, "p2")["someone_holds_card"] is True


def test_jack_cannot_swap_knocker_cards():
    state = set_cards(started(), deck=["JH"])
    state, _ = apply(state, Knock(player_id="p1"))
    state, _ = apply(state, DrawFromDeck(player_id="p2"))
    state, _ = apply(state, DiscardDrawn(player_id="p2"))
    with pytest.raises(RuleViolation):
        apply(state, UseJack(player_id="p2", first=CardRef("p1", 0),
                             second=CardRef("p3", 0)))


def test_knock_only_before_drawing():
    state = started()
    state, _ = apply(state, DrawFromDeck(player_id="p1"))
    with pytest.raises(RuleViolation):
        apply(state, Knock(player_id="p1"))


def test_timeout_only_from_system():
    state = started()
    with pytest.raises(RuleViolation):
        apply(state, TurnTimeout(player_id="p1", turn_number=state.turn_number))


def test_wrong_throw_in_without_penalty_when_disabled():
    from queens_engine import RulesConfig
    state = create_game("g", [("p1", "A"), ("p2", "B")],
                        RulesConfig(penalty_on_wrong_throw_in=False))
    state, _ = apply(state, StartGame(player_id="p1", seed="x"))
    state, _ = apply(state, TurnTimeout(player_id=SYSTEM_PLAYER, turn_number=0))
    state = set_cards(state, hands={"p2": ["5H", "7S", "9D", "2C"]}, discard=["9S"])
    state, _ = apply(state, ThrowIn(player_id="p2", slot=0))
    assert len(state.get_player("p2").hand) == 4


def test_create_game_checks_player_count_and_ids():
    with pytest.raises(ValueError):
        create_game("g", [("p1", "A")])
    with pytest.raises(ValueError):
        create_game("g", [(f"p{i}", "X") for i in range(9)])
    with pytest.raises(ValueError):
        create_game("g", [("p1", "A"), ("p1", "B")])


def test_all_action_types_roundtrip():
    ref = CardRef("p2", 1)
    actions = [
        StartGame(player_id="p1", seed="s"), PeekInitial(player_id="p1", slots=(0, 2)),
        DrawFromDeck(player_id="p1"), TakeFromDiscard(player_id="p1"),
        SwapWithHand(player_id="p1", slot=3), DiscardDrawn(player_id="p1"),
        UseJack(player_id="p1", first=CardRef("p1", 0), second=ref),
        UseKing(player_id="p1", target=ref), SkipSpecial(player_id="p1"),
        Knock(player_id="p1"), ThrowIn(player_id="p2", slot=0),
        TurnTimeout(player_id=SYSTEM_PLAYER, turn_number=7),
    ]
    assert len({type(a) for a in actions}) == 12
    for action in actions:
        import json
        wire = json.loads(json.dumps(action_to_dict(action)))  # wie übers Netz
        assert action_from_dict(wire) == action


def _random_game(seed: str, n_players: int = 8):
    """Spielt eine Partie mit zufällig gewählten, auch ungültigen Aktionen
    (Zwischenwerfen von allen, Bube, König, Timeouts). Gibt Zustand und die
    Kartenzahl nach jedem Schritt zurück."""
    import random
    rng = random.Random(seed)
    state = create_game("g", [(f"p{i}", f"P{i}") for i in range(n_players)])
    state, _ = apply(state, StartGame(player_id="p0", seed=seed))
    counts = []
    for _ in range(3000):
        if state.phase == Phase.FINISHED:
            break
        ids = [p.player_id for p in state.players]
        pid = state.current_player.player_id
        anyone = rng.choice(ids)
        ref = lambda: CardRef(rng.choice(ids), rng.randrange(6))  # noqa: E731
        candidates = {
            Phase.INITIAL_PEEK: [PeekInitial(player_id=anyone, slots=(0, 1)),
                                 TurnTimeout(player_id=SYSTEM_PLAYER, turn_number=0)],
            Phase.AWAIT_DRAW: [DrawFromDeck(player_id=pid), TakeFromDiscard(player_id=pid),
                               Knock(player_id=pid) if state.turn_number > 40
                               else DrawFromDeck(player_id=pid)],
            Phase.AWAIT_DECISION: [SwapWithHand(player_id=pid, slot=rng.randrange(6)),
                                   DiscardDrawn(player_id=pid)],
            Phase.AWAIT_SPECIAL: [UseJack(player_id=pid, first=ref(), second=ref()),
                                  UseKing(player_id=pid, target=ref()),
                                  SkipSpecial(player_id=pid)],
        }[state.phase]
        candidates += [ThrowIn(player_id=anyone, slot=rng.randrange(6)),
                       TurnTimeout(player_id=SYSTEM_PLAYER, turn_number=state.turn_number)
                       if rng.random() < 0.02 else DiscardDrawn(player_id=pid)]
        try:
            state, _ = apply(state, rng.choice(candidates))
        except RuleViolation:
            continue
        counts.append(sum(p.card_count() for p in state.players)
                      + len(state.deck) + len(state.discard)
                      + (state.drawn_card is not None))
    return state, counts


def test_random_8_player_game_ends_without_losing_cards():
    state, counts = _random_game("chaos")
    assert state.phase == Phase.FINISHED
    assert set(counts) == {52}
    assert set(state.scores) == {p.player_id for p in state.players}


def test_random_game_is_deterministic():
    a, _ = _random_game("same")
    b, _ = _random_game("same")
    assert [p.hand for p in a.players] == [p.hand for p in b.players]
    assert a.discard == b.discard and a.scores == b.scores
