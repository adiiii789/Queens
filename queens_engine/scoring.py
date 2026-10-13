"""Punktewertung."""

from __future__ import annotations

from .models import GameState, PlayerState


def hand_score(player: PlayerState) -> int:
    return sum(card.value for card in player.hand if card is not None)


def compute_scores(state: GameState) -> dict[str, int]:
    return {p.player_id: hand_score(p) for p in state.players}


def determine_winners(scores: dict[str, int]) -> list[str]:
    """Niedrigste Punktzahl gewinnt; bei Gleichstand mehrere Gewinner."""
    if not scores:
        return []
    best = min(scores.values())
    return [pid for pid, s in scores.items() if s == best]
