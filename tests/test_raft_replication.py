import asyncio
import unittest
from copy import deepcopy

from queens_engine import DrawFromDeck, Knock, PeekInitial, StartGame, action_to_dict
from services.raft.replication import CommitTimeout, NotLeader, Replication


CREATE = {"type": "CreateGame", "game_id": "game", "players": [["a", "Anna"], ["b", "Ben"]]}


def command(action):
    return {"type": "Action", "game_id": "game", "action": action_to_dict(action)}


class Node:
    """Wahl- und Transport-Doubles. Der Test bestimmt die gewählte Instanz."""

    def __init__(self, node_id, members, network):
        self.node_id = node_id
        self.nodes = {peer: f"http://{peer}" for peer in members}
        self.network = network
        self.role = "follower"
        self.current_term = 0
        self.leader_id = None
        self.replication = Replication(self, self, heartbeat=0.01, commit_timeout=0.3)

    def step_down(self, term):
        self.role = "follower"
        self.current_term = term
        self.leader_id = None
        self.replication.fail_pending()

    def accept_leader(self, term, leader_id):
        if term > self.current_term or self.role != "follower":
            self.step_down(term)
        self.leader_id = leader_id

    def elect(self, term):
        self.current_term = term
        self.role = "leader"
        self.leader_id = self.node_id
        self.replication.on_become_leader()

    async def call(self, peer, rpc, payload):
        await asyncio.sleep(0)
        target = self.network.get(peer)
        if target is not None and self.network.get(self.node_id) is self:
            assert rpc == "append_entries"
            return target.replication.handle_append_entries(**deepcopy(payload))
        return None


class ClusterTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.network = {}

    def node(self, node_id, members="123"):
        node = Node(node_id, members, self.network)
        self.network[node_id] = node
        self.addAsyncCleanup(node.replication.close)
        return node

    async def wait_until(self, condition):
        async def poll():
            while not condition():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(poll(), timeout=2)

    async def start_game(self, replication):
        for cmd in (CREATE, command(StartGame(player_id="a", seed="x")),
                    command(PeekInitial(player_id="a", slots=(0, 1))),
                    command(PeekInitial(player_id="b", slots=(0, 1)))):
            self.assertTrue((await replication.submit(cmd))["ok"])


class ReplicationTests(ClusterTestCase):
    async def test_failover_continues_game_and_empty_node_replays_log(self):
        first, second, third = (self.node(node_id) for node_id in "123")
        first.elect(1)
        await self.start_game(first.replication)
        await self.wait_until(lambda: second.replication.last_applied == first.replication.commit_index)
        self.network.pop("1")
        await first.replication.close()
        second.elect(2)
        draw = command(DrawFromDeck(player_id="a"))
        result = await second.replication.submit(draw)
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["views"]["a"]["drawn_card"])
        self.assertIsNone(result["views"]["b"]["drawn_card"])
        index = second.replication.last_index
        self.assertEqual((await second.replication.submit(draw))["events"], [])
        self.assertEqual(second.replication.last_index, index)

        restarted = self.node("1")
        for follower in (third, restarted):
            await self.wait_until(
                lambda: follower.replication.last_applied == second.replication.commit_index
            )
            self.assertEqual(follower.replication.log, second.replication.log)
            self.assertEqual(follower.replication.state_machine.games,
                             second.replication.state_machine.games)

    async def test_isolated_leader_cannot_acknowledge_and_conflict_is_replaced(self):
        first, second, third = (self.node(node_id) for node_id in "123")
        first.elect(1)
        await self.start_game(first.replication)
        await self.wait_until(lambda: third.replication.last_applied == first.replication.commit_index)
        self.network.pop("1")
        with self.assertRaises(CommitTimeout):
            await first.replication.submit(command(DrawFromDeck(player_id="a")))
        self.assertGreater(first.replication.last_index, first.replication.commit_index)
        with self.assertRaises(CommitTimeout):
            await first.replication.views("game")

        second.elect(2)
        self.assertTrue((await second.replication.submit(command(Knock(player_id="a"))))["ok"])
        self.network["1"] = first
        await self.wait_until(lambda: first.replication.last_applied == second.replication.commit_index)
        self.assertEqual(first.replication.log, second.replication.log)
        self.assertEqual(first.replication.state_machine.games["game"].knocker_id, "a")
        with self.assertRaises(NotLeader) as error:
            await first.replication.submit(CREATE)
        self.assertEqual(error.exception.leader, "http://2")

    async def test_invalid_action_and_missing_id_do_not_extend_log(self):
        node = self.node("1", "1")
        node.elect(1)
        await self.start_game(node.replication)
        last_index = node.replication.last_index
        wrong_turn = command(DrawFromDeck(player_id="b"))
        missing_id = command(DrawFromDeck(player_id="a"))
        del missing_id["action"]["action_id"]
        for invalid in (wrong_turn, missing_id):
            self.assertEqual((await node.replication.submit(invalid))["error"], "rule")
            self.assertEqual(node.replication.last_index, last_index)
        self.assertEqual((await node.replication.views("game"))["views"]["a"]["version"], 3)

    async def test_step_down_wakes_pending_request(self):
        first, _, _ = (self.node(node_id) for node_id in "123")
        first.elect(1)
        await self.start_game(first.replication)
        self.network.pop("1")
        task = asyncio.create_task(first.replication.submit(command(Knock(player_id="a"))))
        self.addCleanup(task.cancel)
        await self.wait_until(lambda: first.replication.last_index > first.replication.commit_index)
        first.accept_leader(2, "2")
        with self.assertRaises(NotLeader):
            await task

    async def test_heartbeat_does_not_commit_unmatched_suffix(self):
        follower = self.node("2").replication
        follower.log = [
            {"term": 1, "command": {"type": "NoOp"}},
            {"term": 1, "command": CREATE},
        ]
        response = follower.handle_append_entries(2, "1", 1, 1, [], 2)
        self.assertTrue(response["success"])
        self.assertEqual(follower.commit_index, 1)
        self.assertNotIn("game", follower.state_machine.games)

    async def test_majority_cannot_directly_commit_previous_term(self):
        leader = self.node("1")
        leader.replication.log.append({"term": 1, "command": CREATE})
        leader.elect(2)
        leader.replication.match_index["2"] = 1
        leader.replication.advance_commit_index()
        self.assertEqual(leader.replication.commit_index, 0)
        leader.replication.match_index["2"] = 2
        leader.replication.advance_commit_index()
        self.assertEqual(leader.replication.commit_index, 2)
        self.assertIn("game", leader.replication.state_machine.games)
