import json
import unittest
from importlib.util import find_spec


@unittest.skipUnless(find_spec("httpx") and find_spec("fastapi"), "HTTP-Abhängigkeiten fehlen")
class ClusterClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_leader_hint_and_preserves_action_id(self):
        import httpx

        from services.raft.client import ClusterClient

        attempts = []

        async def request(request):
            attempts.append((request.url.host, json.loads(request.content)["action"]["action_id"]))
            if request.url.host == "one":
                return httpx.Response(200, json={"ok": False, "error": "not_leader",
                                                 "leader": "http://three"})
            return httpx.Response(200, json={"ok": True})

        client = ClusterClient(["http://one", "http://two", "http://three"],
                               transport=httpx.MockTransport(request))
        self.addAsyncCleanup(client.close)
        action = {"type": "Action", "game_id": "game", "action": {"action_id": "fixed-id"}}
        self.assertTrue((await client.submit(action))["ok"])
        self.assertTrue((await client.submit(action))["ok"])
        self.assertEqual(attempts, [("one", "fixed-id"), ("three", "fixed-id"),
                                    ("three", "fixed-id")])

    async def test_client_uses_real_http_routes(self):
        import httpx
        from fastapi import FastAPI

        from services.raft.client import ClusterClient
        from services.raft.client_routes import client_router
        from test_raft_replication import CREATE, Node

        network = {}
        node = network["1"] = Node("1", "1", network)
        self.addAsyncCleanup(node.replication.close)
        node.elect(1)
        app = FastAPI()
        app.include_router(client_router(node.replication))
        client = ClusterClient(["http://1"], transport=httpx.ASGITransport(app=app))
        self.addAsyncCleanup(client.close)
        self.assertTrue((await client.submit(CREATE))["ok"])
        response = await client.views("game")
        self.assertEqual(set(response["views"]), {"a", "b"})
        self.assertEqual((await client.views("missing"))["error"], "unknown_game")
