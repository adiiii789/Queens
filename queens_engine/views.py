"""Spielersicht: was ein einzelner Client vom Zustand sehen darf.

Gespielt wird „wie am echten Tisch“: Handkarten sind für niemanden offen,
auch nicht für den Besitzer. Angesehene Karten erfährt ein Spieler nur
einmalig über ein privates Ereignis und muss sie sich merken.
Erst nach Spielende werden alle Karten aufgedeckt.
"""

from __future__ import annotations

from typing import Any

from .models import GameState, Phase


def player_view(state: GameState, player_id: str) -> dict[str, Any]:
    is_active = (
        state.phase not in (Phase.LOBBY, Phase.INITIAL_PEEK, Phase.FINISHED)
        and state.current_player.player_id == player_id
    )
    me = state.get_player(player_id)
    top = state.discard_top()

    view: dict[str, Any] = {
        "game_id": state.game_id,
        "you": player_id,
        "phase": state.phase.value,
        # steigt mit jeder angewendeten Aktion; Clients verwerfen ältere Sichten
        "version": state.version,
        "turn_number": state.turn_number,
        "active_player": (state.current_player.player_id
                          if state.phase in (Phase.AWAIT_DRAW, Phase.AWAIT_DECISION,
                                             Phase.AWAIT_SPECIAL) else None),
        "knocker": state.knocker_id,
        "deck_size": len(state.deck),
        "discard_top": top.to_str() if top else None,
        "discard_size": len(state.discard),
        # Gezogene Karte sieht nur der aktive Spieler selbst
        "drawn_card": (state.drawn_card.to_str()
                       if is_active and state.drawn_card else None),
        "someone_holds_card": state.drawn_card is not None,
        "pending_special": (state.pending_special.value
                            if state.pending_special else None),
        "needs_initial_peek": bool(me and state.phase == Phase.INITIAL_PEEK
                                   and not me.has_peeked),
        "players": [
            {
                "player_id": p.player_id,
                "name": p.name,
                # nur Belegung der Slots, keine Kartenwerte
                "slots": [c is not None for c in p.hand],
                "card_count": p.card_count(),
            }
            for p in state.players
        ],
    }

    if state.phase == Phase.FINISHED:
        view["result"] = {
            "hands": {p.player_id: [c.to_str() if c else None for c in p.hand]
                      for p in state.players},
            "scores": state.scores,
            "winners": state.winners,
        }
    return view
