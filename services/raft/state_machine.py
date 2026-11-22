"""Deterministische Anbindung der Queens-Engine an das Raft-Log."""

from queens_engine import (
    Action, GameState, RuleViolation, StartGame, action_from_dict, apply, create_game,
    player_view, validate,
)


class GameStateMachine:
    def __init__(self) -> None:
        self.games: dict[str, GameState] = {}

    def precheck(self, command: dict) -> dict | None:
        """Gibt für Fehler und bereits verarbeitete Befehle direkt eine Antwort."""
        try:
            kind = command["type"]
            if kind == "NoOp":
                return None
            game_id = command["game_id"]
            if not isinstance(game_id, str) or not game_id:
                raise ValueError("Ungültige Spiel-ID.")
            if kind == "CreateGame":
                if game_id in self.games:
                    return {"ok": True, "game_id": game_id}
                self._create(command)
                return None
            if kind != "Action":
                raise ValueError("Unbekannter Raft-Befehl.")
            state = self.games.get(game_id)
            if state is None:
                raise ValueError("Unbekanntes Spiel.")
            action = self._decode_action(command)
            if action.action_id in state.processed_action_ids:
                return self._action_result(game_id, [])
            validate(state, action)
            return None
        except (AttributeError, KeyError, TypeError, ValueError, RuleViolation) as error:
            return {"ok": False, "error": "rule", "message": str(error)}

    def apply(self, command: dict) -> dict:
        kind = command["type"]
        if kind == "NoOp":
            return {"ok": True}
        game_id = command["game_id"]
        if kind == "CreateGame":
            if game_id not in self.games:
                self.games[game_id] = self._create(command)
            return {"ok": True, "game_id": game_id}
        if kind != "Action":
            raise ValueError(f"Unbekannter Raft-Befehl: {kind}")
        state, events = apply(self.games[game_id], self._decode_action(command))
        self.games[game_id] = state
        return self._action_result(game_id, [event.to_dict() for event in events])

    def views(self, game_id: str) -> dict:
        state = self.games.get(game_id)
        if state is None:
            return {"ok": False, "error": "unknown_game"}
        views = {}
        for player in state.players:
            view = player_view(state, player.player_id)
            view.setdefault("version", state.version)
            views[player.player_id] = view
        return {"ok": True, "views": views}

    def _action_result(self, game_id: str, events: list) -> dict:
        return {"game_id": game_id, "events": events, **self.views(game_id)}

    @staticmethod
    def _decode_action(command: dict) -> Action:
        data = command["action"]
        if not isinstance(data, dict) or not isinstance(data.get("action_id"), str) or not data["action_id"]:
            raise ValueError("Ungültige Aktions-ID.")
        action = action_from_dict(data)
        if isinstance(action, StartGame) and not isinstance(action.seed, str):
            raise ValueError("Seed muss eine Zeichenfolge sein.")
        return action

    @staticmethod
    def _create(command: dict) -> GameState:
        players = command["players"]
        if not isinstance(players, (list, tuple)) or any(
            not isinstance(player, (list, tuple)) or len(player) != 2
            or any(not isinstance(value, str) or not value for value in player)
            for player in players
        ):
            raise ValueError("Spielerliste muss aus IDs und Namen bestehen.")
        return create_game(command["game_id"], players)
