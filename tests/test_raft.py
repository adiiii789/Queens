"""Tests für Leader-Wahl, Ausfall und Check-Quorum (P3).

Die Knoten laufen in einem Event-Loop über `InMemoryNetwork`, mit leerem Log
(`HeartbeatOnly`) und verkürzten Zeiten: Election-Timeout 0,15–0,3 s,
Heartbeat 0,03 s.
"""

import asyncio
import random
import tempfile
import unittest

from services.raft.heartbeat import HeartbeatOnly
from services.raft.node import CANDIDATE, FOLLOWER, LEADER, RaftNode
from services.raft.storage import JsonStorage
from services.raft.transport import InMemoryNetwork

ELECTION_MIN, ELECTION_MAX, HEARTBEAT = 0.15, 0.3, 0.03


class Cluster:
    def __init__(self, size: int, seed: int = 1) -> None:
        self.network = InMemoryNetwork()
        self.urls = {str(i): f"http://node{i}" for i in range(1, size + 1)}
        self.seed = seed
        self.nodes: dict[str, RaftNode] = {}

    async def start(self) -> "Cluster":
        for node_id in self.urls:
            await self.add(node_id)
        return self

    async def add(self, node_id: str, **kwargs) -> RaftNode:
        transport = self.network.transport(node_id)
        node = RaftNode(node_id, self.urls, transport, election_min=ELECTION_MIN,
                        election_max=ELECTION_MAX, tick=0.005,
                        rng=random.Random(f"{self.seed}:{node_id}"), **kwargs)
        node.attach(HeartbeatOnly(node, transport, heartbeat=HEARTBEAT))
        self.network.register(node_id, node.handle_rpc)
        self.nodes[node_id] = node
        await node.start()
        return node

    async def crash(self, node_id: str) -> None:
        self.network.remove(node_id)
        await self.nodes.pop(node_id).stop()

    async def stop(self) -> None:
        for node_id in list(self.nodes):
            await self.crash(node_id)

    def leaders(self, among=None) -> list[RaftNode]:
        among = self.nodes if among is None else among
        return [self.nodes[i] for i in among if self.nodes[i].role == LEADER]

    async def wait_for(self, condition, timeout: float = 3.0) -> None:
        async def poll():
            while not condition():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), timeout)

    async def wait_for_leader(self, among=None, timeout: float = 3.0) -> RaftNode:
        """Wartet, bis unter `among` genau ein Leader ist, den alle kennen."""
        among = list(self.nodes if among is None else among)

        def settled():
            leaders = self.leaders(among)
            return (len(leaders) == 1 and all(
                self.nodes[i].leader_id == leaders[0].node_id
                and self.nodes[i].current_term == leaders[0].current_term for i in among))

        await self.wait_for(settled, timeout)
        return self.leaders(among)[0]


class ElectionTests(unittest.IsolatedAsyncioTestCase):
    async def cluster(self, size: int, seed: int = 1) -> Cluster:
        cluster = await Cluster(size, seed).start()
        self.addAsyncCleanup(cluster.stop)
        return cluster

    async def test_exactly_one_leader_and_stable(self):
        cluster = await self.cluster(3)
        leader = await cluster.wait_for_leader()
        term = leader.current_term
        await asyncio.sleep(4 * ELECTION_MAX)  # viele Election-Timeouts lang
        self.assertEqual([n.node_id for n in cluster.leaders()], [leader.node_id])
        self.assertTrue(all(n.current_term == term for n in cluster.nodes.values()))

    async def test_new_leader_in_higher_term_after_crash(self):
        cluster = await self.cluster(3)
        old = await cluster.wait_for_leader()
        await cluster.crash(old.node_id)
        new = await cluster.wait_for_leader()
        self.assertNotEqual(new.node_id, old.node_id)
        self.assertGreater(new.current_term, old.current_term)

    async def test_isolated_leader_steps_down_and_majority_reelects(self):
        cluster = await self.cluster(3)
        old = await cluster.wait_for_leader()
        old_term = old.current_term
        cluster.network.isolate(old.node_id)
        start = asyncio.get_running_loop().time()

        rest = [i for i in cluster.nodes if i != old.node_id]
        new = await cluster.wait_for_leader(among=rest)
        self.assertGreater(new.current_term, old_term)
        # Check-Quorum: der abgetrennte Leader gibt nach ca. election_max auf.
        await cluster.wait_for(lambda: old.role != LEADER)
        self.assertLess(asyncio.get_running_loop().time() - start, 3 * ELECTION_MAX)

        cluster.network.heal(old.node_id)
        final = await cluster.wait_for_leader()
        self.assertEqual(len(cluster.leaders()), 1)
        self.assertEqual(old.leader_id, final.node_id)

    async def test_five_nodes_tolerate_two_failures_but_not_three(self):
        cluster = await self.cluster(5)
        for _ in range(2):
            leader = await cluster.wait_for_leader()
            await cluster.crash(leader.node_id)
        survivor_leader = await cluster.wait_for_leader()
        self.assertEqual(len(cluster.nodes), 3)

        await cluster.crash(survivor_leader.node_id)
        # Nur noch 2 von 5: es darf kein Leader mehr bestätigt werden.
        await asyncio.sleep(5 * ELECTION_MAX)
        self.assertEqual(cluster.leaders(), [])
        self.assertTrue(any(n.role == CANDIDATE for n in cluster.nodes.values())
                        or all(n.role == FOLLOWER for n in cluster.nodes.values()))

    async def test_minority_partition_cannot_elect(self):
        cluster = await self.cluster(5)
        leader = await cluster.wait_for_leader()
        minority = [leader.node_id, next(i for i in cluster.nodes if i != leader.node_id)]
        for node_id in minority:
            cluster.network.isolate(node_id)
        majority = [i for i in cluster.nodes if i not in minority]
        await cluster.wait_for_leader(among=majority)
        await asyncio.sleep(3 * ELECTION_MAX)
        self.assertEqual(cluster.leaders(minority), [])

    async def test_single_node_becomes_leader_immediately(self):
        network = InMemoryNetwork()
        node = RaftNode("1", {"1": "http://node1"}, network.transport("1"),
                        election_min=10, election_max=20, tick=0.005)
        node.attach(HeartbeatOnly(node, network.transport("1")))
        self.addAsyncCleanup(node.stop)
        await node.start()
        await asyncio.wait_for(self._until(lambda: node.role == LEADER), 0.5)
        self.assertEqual(node.current_term, 1)
        await asyncio.sleep(0.1)
        self.assertEqual(node.role, LEADER)  # Check-Quorum gilt für 1 Knoten immer

    async def test_many_seeds_never_two_leaders_in_same_term(self):
        for seed in range(5):
            cluster = await Cluster(3, seed).start()
            try:
                seen: dict[int, set[str]] = {}
                end = asyncio.get_running_loop().time() + 1.0
                crashed = False
                while asyncio.get_running_loop().time() < end:
                    for node in cluster.leaders():
                        seen.setdefault(node.current_term, set()).add(node.node_id)
                    if not crashed and cluster.leaders():
                        await cluster.crash(cluster.leaders()[0].node_id)
                        crashed = True
                    await asyncio.sleep(0.005)
                for term, leaders in seen.items():
                    self.assertEqual(len(leaders), 1, f"Seed {seed}, Term {term}: {leaders}")
            finally:
                await cluster.stop()

    @staticmethod
    async def _until(condition):
        while not condition():
            await asyncio.sleep(0.005)


class FakeLog:
    """Log mit vorgegebenen Terms, für die Abstimmungsregeln."""

    def __init__(self, terms):
        self.terms = list(terms)
        self.peer_last_response = {}

    @property
    def last_index(self):
        return len(self.terms)

    def term_at(self, index):
        return 0 if index == 0 else self.terms[index - 1]

    def fail_pending(self):
        pass


class RequestVoteTests(unittest.IsolatedAsyncioTestCase):
    def node(self, log_terms=(), storage=None):
        network = InMemoryNetwork()
        node = RaftNode("1", {"1": "a", "2": "b", "3": "c"}, network.transport("1"),
                        storage=storage)
        node.attach(FakeLog(log_terms))
        node.reset_election_timer()
        return node

    async def test_one_vote_per_term(self):
        node = self.node()
        self.assertTrue(node.handle_request_vote(5, "2", 0, 0)["vote_granted"])
        self.assertEqual((node.current_term, node.voted_for), (5, "2"))
        self.assertFalse(node.handle_request_vote(5, "3", 0, 0)["vote_granted"])
        self.assertTrue(node.handle_request_vote(5, "2", 0, 0)["vote_granted"])  # Wiederholung
        self.assertTrue(node.handle_request_vote(6, "3", 0, 0)["vote_granted"])  # neuer Term

    async def test_stale_term_rejected(self):
        node = self.node()
        node.handle_request_vote(4, "2", 0, 0)
        response = node.handle_request_vote(3, "3", 0, 0)
        self.assertEqual(response, {"term": 4, "vote_granted": False})

    async def test_candidate_log_must_be_up_to_date(self):
        node = self.node(log_terms=[1, 1, 2])
        # älterer letzter Term -> keine Stimme, aber Term wird übernommen
        response = node.handle_request_vote(3, "2", 10, 1)
        self.assertEqual(response, {"term": 3, "vote_granted": False})
        self.assertIsNone(node.voted_for)
        # gleicher letzter Term, kürzeres Log -> keine Stimme
        self.assertFalse(node.handle_request_vote(3, "2", 2, 2)["vote_granted"])
        # höherer letzter Term schlägt Länge
        self.assertTrue(node.handle_request_vote(3, "3", 1, 3)["vote_granted"])

    async def test_higher_term_makes_leader_step_down(self):
        node = self.node()
        node.current_term, node.role, node.leader_id = 2, LEADER, "1"
        node.handle_request_vote(3, "2", 0, 0)
        self.assertEqual((node.role, node.current_term, node.leader_id), (FOLLOWER, 3, None))

    async def test_vote_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.node(storage=JsonStorage(directory, "1"))
            self.assertTrue(node.handle_request_vote(3, "2", 0, 0)["vote_granted"])
            restarted = self.node(storage=JsonStorage(directory, "1"))
            self.assertEqual((restarted.current_term, restarted.voted_for), (3, "2"))
            self.assertFalse(restarted.handle_request_vote(3, "3", 0, 0)["vote_granted"])

    async def test_broken_storage_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = JsonStorage(directory, "1")
            storage.path.write_text("{kaputt", encoding="utf-8")
            self.assertEqual(self.node(storage=storage).current_term, 0)
