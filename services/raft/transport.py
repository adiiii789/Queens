"""Transport der Raft-RPCs zwischen den Knoten.

`call(peer, rpc, payload)` liefert die Antwort des Knotens `peer` oder `None`,
wenn er nicht (rechtzeitig) antwortet. Raft behandelt beides gleich: Eine
fehlende Antwort ist keine Stimme bzw. keine Bestätigung.

- `HttpTransport`: echte Prozesse, POST an `<url>/raft/<rpc>`.
- `InMemoryNetwork`: alle Knoten in einem Event-Loop, für schnelle Tests ohne
  HTTP. `isolate()` / `heal()` simulieren eine Netzwerktrennung, `remove()`
  einen Absturz.
"""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import Any, Awaitable, Callable, Mapping, Protocol

log = logging.getLogger("raft.transport")

RPCS = ("request_vote", "append_entries")


class Transport(Protocol):
    async def call(self, peer: str, rpc: str, payload: dict) -> dict | None: ...


class HttpTransport:
    """RPCs als HTTP-POST. `isolated = True` trennt den Knoten für die Demo
    vom Netz: Er sendet nichts mehr (und der Server lehnt Eingehendes ab)."""

    def __init__(self, nodes: Mapping[str, str], timeout: float = 1.0,
                 http_transport: Any = None) -> None:
        """`http_transport`: optionaler httpx-Transport (Tests ohne Sockets)."""
        import httpx

        self.nodes = {node_id: url.rstrip("/") for node_id, url in nodes.items()}
        self.isolated = False
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout),
                                       transport=http_transport)
        self._errors = (httpx.HTTPError, ValueError)

    async def call(self, peer: str, rpc: str, payload: dict) -> dict | None:
        if self.isolated or rpc not in RPCS:
            return None
        url = self.nodes.get(peer)
        if url is None:
            return None
        try:
            response = await self._http.post(f"{url}/raft/{rpc}", json=payload)
            response.raise_for_status()
            result = response.json()
        except self._errors:
            return None
        return result if isinstance(result, dict) else None

    async def close(self) -> None:
        await self._http.aclose()


Handler = Callable[[str, dict], Awaitable[dict] | dict]


class InMemoryNetwork:
    """Simuliertes Netz für Tests. Jeder Knoten meldet einen Handler
    `handler(rpc, payload)` an und bekommt mit `transport(node_id)` seinen
    Transport. Nachrichten von oder zu isolierten bzw. entfernten Knoten
    gehen verloren."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self._handlers: dict[str, Handler] = {}
        self._isolated: set[str] = set()

    def register(self, node_id: str, handler: Handler) -> None:
        self._handlers[node_id] = handler

    def remove(self, node_id: str) -> None:
        """Knoten abstürzen lassen: Er ist nicht mehr erreichbar."""
        self._handlers.pop(node_id, None)

    def isolate(self, node_id: str) -> None:
        self._isolated.add(node_id)

    def heal(self, node_id: str) -> None:
        self._isolated.discard(node_id)

    def reachable(self, sender: str, receiver: str) -> bool:
        return (sender in self._handlers and receiver in self._handlers
                and sender not in self._isolated and receiver not in self._isolated)

    def transport(self, node_id: str) -> "InMemoryTransport":
        return InMemoryTransport(self, node_id)


class InMemoryTransport:
    def __init__(self, network: InMemoryNetwork, node_id: str) -> None:
        self.network = network
        self.node_id = node_id

    async def call(self, peer: str, rpc: str, payload: dict) -> dict | None:
        await asyncio.sleep(self.network.delay)
        if not self.network.reachable(self.node_id, peer):
            return None
        # Kopie: Wie über das echte Netz teilen sich Sender und Empfänger nichts.
        result: Any = self.network._handlers[peer](rpc, deepcopy(payload))
        if asyncio.iscoroutine(result):
            result = await result
        # Die Antwort kann unterwegs verloren gehen, wenn sich die Lage ändert.
        if not self.network.reachable(self.node_id, peer):
            return None
        return result

    async def close(self) -> None:
        pass
