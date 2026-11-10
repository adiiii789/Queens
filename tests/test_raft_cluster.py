"""Leader-Wahl (P3) zusammen mit der Log-Replikation (P4).

Echte `RaftNode`s mit echter `Replication` über `InMemoryNetwork`; anders als
in test_raft_replication.py bestimmt hier kein Test, wer Leader ist – der
Cluster wählt selbst.
"""

import asyncio
import random
import unittest

from queens_engine import DrawFromDeck, Knock, PeekInitial, StartGame, action_to_dict
from services.raft.node import LEADER, RaftNode
from services.raft.replication import Replication
from services.raft.transport import InMemoryNetwork

CREATE = {"type": "CreateGame", "game_id": "g", "players": [["a", "Anna"], ["b", "Ben"]]}


def command(action):
    return {"type": "Action", "game_id": "g", "action": action_to_dict(action)}


class RaftClusterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.network = InMemoryNetwork()
        self.urls = {str(i): f"http://node{i}" for i in (1, 2, 3)}
        self.nodes: dict[str, RaftNode] = {}
        for node_id in self.urls:
            await self.start_node(node_id)

    async def asyncTearDown(self):
        for node in self.nodes.values():
            await node.stop()

    async def start_node(self, node_id):
        transport = self.network.transport(node_id)
        node = RaftNode(node_id, self.urls, transport, election_min=0.15,
                        election_max=0.3, tick=0.005, rng=random.Random(node_id))
        node.attach(Replication(node, transport, heartbeat=0.03, commit_timeout=0.5))
        self.network.register(node_id, node.handle_rpc)
        self.nodes[node_id] = node
        await node.start()
        return node

    async def crash(self, node_id):
        self.network.remove(node_id)
        await self.nodes.pop(node_id).stop()

    async def until(self, condition, timeout=3.0):
        async def poll():
            while not condition():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), timeout)

    async def leader(self, among=None) -> RaftNode:
        among = list(among or self.nodes)
        await self.until(lambda: sum(self.nodes[i].role == LEADER for i in among) == 1)
        return next(self.nodes[i] for i in among if self.nodes[i].role == LEADER)

    async def submit(self, cmd, among=None):
        """Wie der ClusterClient: beim aktuellen Leader einreichen, bei
        Leader-Wechsel erneut versuchen (gleiche action_id)."""
        for _ in range(50):
            leader = await self.leader(among)
            try:
                return await leader.replication.submit(cmd)
            except Exception:
                await asyncio.sleep(0.05)
        raise AssertionError("kein Commit möglich")

    def games(self, node_id):
        return self.nodes[node_id].replication.state_machine.games

    async def start_game(self):
        for cmd in (CREATE, command(StartGame(player_id="a", seed="s")),
                    command(PeekInitial(player_id="a", slots=(0, 1))),
                    command(PeekInitial(player_id="b", slots=(0, 1)))):
            self.assertTrue((await self.submit(cmd))["ok"])

    async def test_game_continues_after_leader_crash_and_node_catches_up(self):
        await self.start_game()
        old = await self.leader()
        await self.crash(old.node_id)
        result = await self.submit(command(DrawFromDeck(player_id="a")))
        self.assertTrue(result["ok"])
        new = await self.leader()
        self.assertGreater(new.current_term, old.current_term)

        restarted = await self.start_node(old.node_id)  # leer neu gestartet
        await self.until(lambda: restarted.replication.last_applied
                         == new.replication.commit_index)
        self.assertEqual(restarted.replication.log, new.replication.log)
        self.assertEqual(self.games(old.node_id), self.games(new.node_id))

    async def test_isolated_leader_steps_down_and_adopts_new_log(self):
        await self.start_game()
        old = await self.leader()
        self.network.isolate(old.node_id)
        rest = [i for i in self.nodes if i != old.node_id]
        self.assertTrue((await self.submit(command(Knock(player_id="a")), among=rest))["ok"])
        await self.until(lambda: old.role != LEADER)  # Check-Quorum

        self.network.heal(old.node_id)
        new = await self.leader()
        await self.until(lambda: old.replication.last_applied == new.replication.commit_index)
        self.assertEqual(self.games(old.node_id)["g"].knocker_id, "a")
        self.assertEqual(old.leader_id, new.node_id)

    async def test_follower_with_stale_log_cannot_win_election(self):
        await self.start_game()
        leader = await self.leader()
        stale = next(i for i in self.nodes if i != leader.node_id)
        self.network.isolate(stale)
        self.assertTrue((await self.submit(command(DrawFromDeck(player_id="a"))))["ok"])
        await self.crash(leader.node_id)
        self.network.heal(stale)
        # Der veraltete Knoten bekommt keine Stimme vom aktuellen -> der andere gewinnt.
        winner = await self.leader()
        self.assertNotEqual(winner.node_id, stale)
        # Der Zug ist nicht verloren: Er wird mit dem No-Op des neuen Terms
        # bestätigt und dann auf beiden Knoten angewendet.
        for node_id in (winner.node_id, stale):
            await self.until(lambda: self.games(node_id)["g"].drawn_card is not None)
