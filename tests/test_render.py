"""Tests für client/render.py: Texte zu allen Ereignissen der Engine, Tisch
und Auflösung, mit echten Sichten aus queens_engine."""

import re
from pathlib import Path

from client.render import (
    KNOWN_EVENTS, LOCK, Names, describe, hints, player_numbers, render_lobby,
    render_table,
)
from queens_engine import (
    DiscardDrawn, DrawFromDeck, Knock, PeekInitial, StartGame, apply, create_game,
    player_view,
)

ENGINE = Path(__file__).resolve().parents[1] / "queens_engine" / "engine.py"


def started():
    state = create_game("g1", [("a", "Anna"), ("b", "Ben")])
    state, events = apply(state, StartGame(player_id="a", seed="x"))
    return state, events


def test_every_engine_event_has_a_text():
    source = ENGINE.read_text(encoding="utf-8")
    emitted = set(re.findall(r'(?:public|private)\(\s*(?:[\w.]+,\s*)?"(\w+)"', source))
    assert emitted, "keine Ereignisse in engine.py gefunden"
    assert emitted <= KNOWN_EVENTS, f"ohne Text: {emitted - KNOWN_EVENTS}"


def test_private_events_are_marked_and_cards_sorted():
    names = Names({"a": "Anna"}, me="a")
    line = describe({"type": "CardsPeeked", "audience": "a",
                     "data": {"cards": {"3": "Q♥", "0": "7♠"}}}, names)
    assert line.startswith(LOCK)
    assert line.index("Slot 0") < line.index("Slot 3")
    public = describe({"type": "PlayerKnocked", "audience": None,
                       "data": {"player_id": "a"}}, names)
    assert public.startswith("•") and "Anna (du)" in public


def test_unknown_or_broken_event_does_not_crash():
    names = Names()
    assert "Neu" in describe({"type": "Neu", "data": {"x": 1}}, names)
    assert "TurnStarted" in describe({"type": "TurnStarted", "data": {}}, names)


def test_table_shows_slots_without_card_values():
    state, _ = started()
    view = player_view(state, "a")
    text = render_table(view, Names({"a": "Anna", "b": "Ben"}, me="a"), remaining=42)
    assert "[0] [1] [2] [3]" in text and "noch 42 s" in text
    assert "p <slot> <slot>" in text
    hand_cards = {c.to_str() for p in state.players for c in p.hand}
    assert not any(card in text.replace(f"Ablage: {view['discard_top']}", "")
                   for card in hand_cards - {view["discard_top"]})


def test_hints_follow_phase_and_active_player():
    state, _ = started()
    for pid in ("a", "b"):
        state, _ = apply(state, PeekInitial(player_id=pid, slots=(0, 1)))
    assert hints(player_view(state, "a")).startswith("z ziehen")
    assert hints(player_view(state, "b")).startswith("Warte")
    state, _ = apply(state, DrawFromDeck(player_id="a"))
    assert "t <slot>" in hints(player_view(state, "a"))
    assert "Gezogen:" in render_table(player_view(state, "a"), Names(me="a"))
    assert "hält eine gezogene Karte" in render_table(player_view(state, "b"), Names(me="b"))


def test_result_lists_cards_scores_and_winner():
    state, _ = started()
    for pid in ("a", "b"):
        state, _ = apply(state, PeekInitial(player_id=pid, slots=(0, 1)))
    state, _ = apply(state, Knock(player_id="a"))
    state, _ = apply(state, DrawFromDeck(player_id="b"))
    state, _ = apply(state, DiscardDrawn(player_id="b"))
    if state.phase.value == "await_special":
        from queens_engine import SkipSpecial
        state, _ = apply(state, SkipSpecial(player_id="b"))
    view = player_view(state, "b")
    text = render_table(view, Names({"a": "Anna", "b": "Ben"}, me="b"))
    assert "Auflösung" in text and "★ Gewinner" in text
    assert f"= {state.scores['a']} Punkte" in text


def test_lobby_and_player_numbers():
    lobby = {"lobby_id": "ABCD", "host_id": "a", "status": "waiting", "served_by": "l2",
             "players": [{"player_id": "a", "name": "Anna", "ready": True},
                         {"player_id": "b", "name": "Ben", "ready": False}]}
    text = render_lobby(lobby, me="b")
    assert "ABCD" in text and "l2" in text and "Host" in text and "✗" in text
    state, _ = started()
    assert player_numbers(player_view(state, "a")) == ["a", "b"]
    assert player_numbers(None) == []
