"""Heartbeats mit leerem Log – Platzhalter für die Replikation.

Erfüllt dieselben Hooks wie `replication.Replication` (P4), kennt aber kein
Log: Der Leader schickt nur leere AppendEntries als Heartbeat, Follower
erkennen daran den Leader. Damit lässt sich die Leader-Wahl isoliert testen
(tests/test_raft.py), und der Server läuft auch, solange
feature/Raft_Log&Consistency noch nicht gemergt ist.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .transport import Transport

if TYPE_CHECKING:
    from .node import RaftNode

log = logging.getLogger("raft.heartbeat")


class HeartbeatOnly:
    def __init__(self, node: "RaftNode", transport: Transport, *,
                 heartbeat: float = 0.3) -> None:
        self.node = node
        self.transport = transport
        self.heartbeat = heartbeat
        self.log: list[dict] = []
        self.commit_index = 0
        self.last_applied = 0
        self.peer_last_response: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def last_index(self) -> int:
        return 0

    def term_at(self, index: int) -> int:
        if index != 0:
            raise IndexError(index)
        return 0

    def on_become_leader(self) -> None:
        self.fail_pending()
        self.peer_last_response.clear()
        term = self.node.current_term
        for peer in self.node.peers:
            task = asyncio.create_task(self._beat(peer, term))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def fail_pending(self) -> None:
        for task in list(self._tasks):
            if task is not asyncio.current_task():
                task.cancel()

    async def close(self) -> None:
        self.fail_pending()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _still_leader(self, term: int) -> bool:
        return self.node.role == "leader" and self.node.current_term == term

    async def _beat(self, peer: str, term: int) -> None:
        loop = asyncio.get_running_loop()
        while self._still_leader(term):
            response = await self.transport.call(peer, "append_entries", {
                "term": term, "leader_id": self.node.node_id, "prev_log_index": 0,
                "prev_log_term": 0, "entries": [], "leader_commit": 0,
            })
            if not self._still_leader(term):
                return
            if response is not None:
                if response.get("term", 0) > term:
                    self.node.step_down(response["term"])
                    return
                self.peer_last_response[peer] = loop.time()
            await asyncio.sleep(self.heartbeat)

    def handle_append_entries(self, term: int, leader_id: str, prev_log_index: int,
                              prev_log_term: int, entries: list,
                              leader_commit: int) -> dict:
        if term < self.node.current_term:
            return {"term": self.node.current_term, "success": False}
        self.node.accept_leader(term, leader_id)
        return {"term": term, "success": prev_log_index == 0 and not entries}
