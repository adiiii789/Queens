from queens_engine import DiscardDrawn, DrawFromDeck, Knock, Phase, SkipSpecial
from services.raft.results import ResultWriter
from test_raft_replication import ClusterTestCase, command


class Repository:
    def __init__(self):
        self.games = {}
        self.offline = False
        self.attempts = 0

    async def save(self, game_id, results, knocker):
        self.attempts += 1
        if self.offline:
            raise OSError("DB offline")
        if game_id in self.games:
            return False
        self.games[game_id] = (results, knocker)
        return True


class ResultTests(ClusterTestCase):
    async def test_result_survives_database_outage_and_leader_change(self):
        first, second, _ = (self.node(node_id) for node_id in "123")
        first.elect(1)
        await self.start_game(first.replication)
        for action in (Knock(player_id="a"), DrawFromDeck(player_id="b"),
                       DiscardDrawn(player_id="b")):
            self.assertTrue((await first.replication.submit(command(action)))["ok"])
        game = first.replication.state_machine.games["game"]
        if game.phase == Phase.AWAIT_SPECIAL:
            self.assertTrue((await first.replication.submit(
                command(SkipSpecial(player_id="b"))
            ))["ok"])
            game = first.replication.state_machine.games["game"]
        self.assertEqual(game.phase, Phase.FINISHED)
        await self.wait_until(lambda: second.replication.last_applied == first.replication.commit_index)
        repository = Repository()
        writer = ResultWriter(first.replication, repository)
        repository.offline = True
        with self.assertLogs("services.raft.results", "WARNING"):
            await writer.write_finished()
        self.assertEqual(repository.games, {})
        self.network.pop("1")
        await first.replication.close()
        second.elect(2)
        await second.replication.views("game")
        repository.offline = False
        new_writer = ResultWriter(second.replication, repository)
        await new_writer.write_finished()
        # Ein neuer Writer kennt den Schreibstatus nicht, der Speicher dedupliziert.
        await ResultWriter(second.replication, repository).write_finished()
        await new_writer.write_finished()
        self.assertEqual(repository.attempts, 3)
        self.assertEqual(list(repository.games), ["game"])
        rows, knocker = repository.games["game"]
        self.assertEqual(knocker, "Anna")
        self.assertEqual({row["player_id"]: row["score"] for row in rows}, game.scores)
        self.assertEqual({row["player_id"] for row in rows if row["winner"]}, set(game.winners))
