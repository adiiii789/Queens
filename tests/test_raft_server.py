"""Tests für den HTTP-Server eines Raft-Knotens (Endpunkte, Isolation).

Drei Knoten-Apps sprechen über ihre echten FastAPI-Routen miteinander; ein
kleiner ASGI-Router leitet nach Hostname weiter, es werden keine Ports
geöffnet.
"""

import asyncio
import unittest
from importlib.util import find_spec


@unittest.skipUnless(find_spec("httpx") and find_spec("fastapi"), "HTTP-Abhängigkeiten fehlen")
class RaftServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx

        from services.raft.server import create_app

        apps = {}

        async def router(scope, receive, send):
            host = dict(scope["headers"])[b"host"].decode().split(":")[0]
            await apps[host](scope, receive, send)

        self.urls = {str(i): f"http://node{i}" for i in (1, 2, 3)}
        asgi = httpx.ASGITransport(app=router)
        for node_id in self.urls:
            apps[f"node{node_id}"] = create_app(
                node_id, self.urls, election_min=0.15, election_max=0.3, heartbeat=0.03,
                rpc_timeout=0.2, http_transport=asgi)
        self.nodes = {i: apps[f"node{i}"].state.node for i in self.urls}
        for node in self.nodes.values():
            await node.start()
            self.addAsyncCleanup(node.stop)
        self.http = httpx.AsyncClient(transport=asgi)
        self.addAsyncCleanup(self.http.aclose)

    async def status(self, node_id):
        return (await self.http.get(f"{self.urls[node_id]}/status")).json()

    async def leader(self, among=None, timeout=3.0):
        among = among or list(self.urls)

        async def poll():
            while True:
                states = [await self.status(i) for i in among]
                leaders = [s for s in states if s["role"] == "leader"]
                if len(leaders) == 1 and all(s["leader_id"] == leaders[0]["node_id"]
                                             for s in states):
                    return leaders[0]
                await asyncio.sleep(0.02)
        return await asyncio.wait_for(poll(), timeout)

    async def test_status_shows_leader_for_all_nodes(self):
        leader = await self.leader()
        follower = await self.status(next(i for i in self.urls if i != leader["node_id"]))
        self.assertEqual(follower["role"], "follower")
        self.assertEqual(follower["leader"], self.urls[leader["node_id"]])
        self.assertEqual(follower["term"], leader["term"])
        self.assertFalse(follower["isolated"])
        for key in ("log_length", "commit_index", "games", "cluster_size"):
            self.assertIn(key, follower)

    async def test_isolate_via_admin_endpoint_and_heal(self):
        old = await self.leader()
        url = self.urls[old["node_id"]]
        response = await self.http.post(f"{url}/admin/isolate", json={"isolated": True})
        self.assertEqual(response.json()["isolated"], True)
        vote = await self.http.post(f"{url}/raft/request_vote", json={
            "term": 99, "candidate_id": "2", "last_log_index": 0, "last_log_term": 0})
        self.assertEqual(vote.status_code, 503)

        rest = [i for i in self.urls if i != old["node_id"]]
        new = await self.leader(among=rest)
        self.assertGreater(new["term"], old["term"])
        await asyncio.wait_for(self._until_not_leader(old["node_id"]), 2)

        await self.http.post(f"{url}/admin/isolate", json={"isolated": False})
        final = await self.leader()
        self.assertEqual((await self.status(old["node_id"]))["leader_id"], final["node_id"])

    async def test_invalid_rpc_payload_is_rejected(self):
        response = await self.http.post(f"{self.urls['1']}/raft/request_vote",
                                         json={"term": 1})
        self.assertEqual(response.status_code, 400)

    async def _until_not_leader(self, node_id):
        while (await self.status(node_id))["role"] == "leader":
            await asyncio.sleep(0.02)
