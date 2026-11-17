"""Asynchroner Game-Service-Client für Lobby und Gateway."""

import asyncio
from urllib.parse import quote

import httpx


class ClusterUnavailable(Exception):
    """Innerhalb der Wartezeit konnte kein Leader antworten."""


class ClusterClient:
    def __init__(self, nodes: list[str], *, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.nodes = list(dict.fromkeys(node.rstrip("/") for node in nodes))
        self.leader: str | None = None
        self.timeout = timeout
        # Ein Submit kann serverseitig bis zu fünf Sekunden auf den Commit warten.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(6.0, connect=1.0), transport=transport
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def submit(self, command: dict) -> dict:
        # Derselbe Befehl behält bei allen Versuchen seine action_id.
        return await self._request("POST", "/client/submit", json=command)

    async def views(self, game_id: str) -> dict:
        return await self._request("GET", f"/client/views/{quote(game_id, safe='')}")

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if not self.nodes:
            raise ClusterUnavailable("Keine Raft-Knoten konfiguriert.")
        try:
            return await asyncio.wait_for(
                self._try_nodes(method, path, **kwargs), self.timeout
            )
        except asyncio.TimeoutError as error:
            raise ClusterUnavailable("Kein Leader erreichbar.") from error

    async def _try_nodes(self, method: str, path: str, **kwargs) -> dict:
        while True:
            remaining = self.nodes.copy()
            if self.leader in remaining:
                remaining.remove(self.leader)
                remaining.insert(0, self.leader)
            while remaining:
                node = remaining.pop(0)
                try:
                    response = await self._http.request(method, node + path, **kwargs)
                    response.raise_for_status()
                    result = response.json()
                except (httpx.HTTPError, ValueError):
                    continue
                if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                    continue
                if result.get("error") not in ("not_leader", "timeout"):
                    self.leader = node
                    return result
                hint = result.get("leader")
                if isinstance(hint, str):
                    hint = hint.rstrip("/")
                if hint in remaining:
                    remaining.remove(hint)
                    remaining.insert(0, hint)
            await asyncio.sleep(0.1)
