"""Tests für services/raft/results.py: Ergebnisse genau einmal speichern,
nur als Leader, bei Datenbankausfall später erneut."""

import asyncio
from types import SimpleNamespace

from queens_engine import (
    DiscardDrawn, DrawFromDeck, Knock, PeekInitial, StartGame, apply, create_game,
)
from services.raft.results import ResultWriter, knocker_name, result_rows


def finished_game(game_id="g1"):
    state = create_game(game_id, [("p1", "Anna"), ("p2", "Ben")])
    for action in (StartGame(player_id="p1", seed="s"),
                   PeekInitial(player_id="p1", slots=(0, 1)),
                   PeekInitial(player_id="p2", slots=(0, 1)),
                   Knock(player_id="p1"),
                   DrawFromDeck(player_id="p2"),
                   DiscardDrawn(player_id="p2")):
        state, _ = apply(state, action)
    if state.phase.value == "await_special":  # Bube/König abgelegt
        from queens_engine import SkipSpecial
        state, _ = apply(state, SkipSpecial(player_id="p2"))
    assert state.phase.value == "finished"
    return state


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.calls = 0
        self.fail = False

    async def save(self, game_id, results, knocker=None):
        self.calls += 1
        if self.fail:
            raise OSError("Datenbank weg")
        if game_id in self.rows:
            return False
        self.rows[game_id] = (results, knocker)
        return True


def replication(games, leader=True):
    return SimpleNamespace(is_leader=leader, state_machine=SimpleNamespace(games=games))


def test_rows_match_repository_format():
    state = finished_game()
    rows = result_rows(state)
    assert [r["name"] for r in rows] == ["Anna", "Ben"]
    assert all(set(r) == {"player_id", "name", "score", "winner"} for r in rows)
    assert {r["player_id"] for r in rows if r["winner"]} == set(state.winners)
    assert knocker_name(state) == "Anna"


def test_saves_only_finished_games_once():
    running = create_game("g2", [("p1", "Anna"), ("p2", "Ben")])
    store = FakeStore()
    writer = ResultWriter(replication({"g1": finished_game(), "g2": running}), store)
    assert asyncio.run(writer.flush()) == 1
    assert asyncio.run(writer.flush()) == 0
    assert list(store.rows) == ["g1"] and store.calls == 1
    assert store.rows["g1"][1] == "Anna"


def test_new_leader_rewrites_without_duplicates():
    games = {"g1": finished_game()}
    store = FakeStore()
    asyncio.run(ResultWriter(replication(games), store).flush())
    # Neuer Leader kennt `saved` nicht, die Datenbank verwirft das Doppelte
    assert asyncio.run(ResultWriter(replication(games), store).flush()) == 0
    assert len(store.rows) == 1


def test_retries_after_database_outage():
    store = FakeStore()
    store.fail = True
    writer = ResultWriter(replication({"g1": finished_game()}), store)
    assert asyncio.run(writer.flush()) == 0
    assert writer.saved == set()
    store.fail = False
    assert asyncio.run(writer.flush()) == 1


def test_loop_writes_only_as_leader():
    async def scenario():
        repl = replication({"g1": finished_game()}, leader=False)
        store = FakeStore()
        writer = ResultWriter(repl, store, interval=0.01)
        writer.start()
        await asyncio.sleep(0.05)
        assert store.calls == 0
        repl.is_leader = True
        await asyncio.sleep(0.05)
        await writer.stop()
        assert list(store.rows) == ["g1"]
    asyncio.run(scenario())


def test_saved_in_postgres_and_lobby_finished(db_dsn):
    from services.db import Database, LobbyRepository, ResultRepository

    async def scenario():
        db = Database(db_dsn)
        try:
            lobbies = LobbyRepository(db)
            lobby = await lobbies.create("p1", "Anna")
            await lobbies.join(lobby["lobby_id"], "p2", "Ben")
            await lobbies.set_ready(lobby["lobby_id"], "p1", True, ["a"])
            _, ticket = await lobbies.set_ready(lobby["lobby_id"], "p2", True, ["a"])
            await lobbies.mark_started(ticket.lobby_id, ticket.game_id)

            state = finished_game(ticket.game_id)
            writer = ResultWriter(replication({ticket.game_id: state}), ResultRepository(db))
            assert await writer.flush() == 1
            recent = await ResultRepository(db).recent()
            assert recent[0]["game_id"] == ticket.game_id and recent[0]["knocker"] == "Anna"
            assert {r["player_id"]: r["score"] for r in recent[0]["results"]} == state.scores
            assert (await lobbies.get(ticket.lobby_id))["status"] == "finished"
        finally:
            await db.close()
    asyncio.run(scenario())
