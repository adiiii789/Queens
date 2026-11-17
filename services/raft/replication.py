"""Raft-Log, Replikation und Commit. Wahl und Rollenwechsel liegen beim Knoten."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from copy import deepcopy
from typing import Protocol, TypedDict

from .state_machine import GameStateMachine

log = logging.getLogger(__name__)


class LogEntry(TypedDict):
    term: int
    command: dict


class ElectionNode(Protocol):
    node_id: str
    nodes: Mapping[str, str]
    current_term: int
    role: str
    leader_id: str | None

    def accept_leader(self, term: int, leader_id: str) -> None: ...
    def step_down(self, term: int) -> None: ...


class Transport(Protocol):
    async def call(self, peer: str, rpc: str, payload: dict) -> dict | None: ...


class NotLeader(Exception):
    def __init__(self, leader: str | None) -> None:
        self.leader = leader
        super().__init__("Knoten ist kein Leader mehr.")


class CommitTimeout(Exception):
    """Der Commit konnte innerhalb der Wartefrist nicht bestätigt werden."""


class Replication:
    def __init__(self, node: ElectionNode, transport: Transport, *,
                 heartbeat: float = 0.3, commit_timeout: float = 5.0) -> None:
        self.node = node
        self.transport = transport
        self.heartbeat = heartbeat
        self.commit_timeout = commit_timeout
        self.state_machine = GameStateMachine()
        # Log-Index 0 ist der virtuelle leere Eintrag.
        self.log: list[LogEntry] = []
        self.commit_index = 0
        self.last_applied = 0
        self.next_index: dict[str, int] = {}
        self.match_index: dict[str, int] = {}
        self.peer_last_response: dict[str, float] = {}
        self._wakeups: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task] = set()
        self._submit_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._progress = asyncio.Event()
        self._confirmed_send_time: dict[str, float] = {}
        self._leader_noop: int | None = None
        self._leader_term: int | None = None

    @property
    def last_index(self) -> int:
        return len(self.log)

    def term_at(self, index: int) -> int:
        if index == 0:
            return 0
        if not 1 <= index <= self.last_index:
            raise IndexError(index)
        return self.log[index - 1]["term"]

    @property
    def is_leader(self) -> bool:
        return self.node.role == "leader" and self._leader_term == self.node.current_term

    def on_become_leader(self) -> None:
        """Nach dem Rollenwechsel im selben Event-Loop aufrufen."""
        self.fail_pending()
        self._leader_term = self.node.current_term
        peers = [peer for peer in self.node.nodes if peer != self.node.node_id]
        self.next_index = dict.fromkeys(peers, self.last_index + 1)
        self.match_index = dict.fromkeys(peers, 0)
        self.peer_last_response.clear()
        self._confirmed_send_time.clear()
        self._leader_noop = self._append_local({"type": "NoOp"})
        self._wakeups = {peer: asyncio.Event() for peer in peers}
        for peer, wakeup in self._wakeups.items():
            task = asyncio.create_task(self._replicate(peer, wakeup))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        self.advance_commit_index()

    def fail_pending(self) -> None:
        """Bei Verlust der Leader-Rolle offene Anfragen und Replikatoren beenden."""
        self._leader_term = None
        self._leader_noop = None
        for task in self._tasks:
            if task is not asyncio.current_task():
                task.cancel()
        self._wakeups.clear()
        self._signal_progress()

    async def close(self) -> None:
        self.fail_pending()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _signal_progress(self) -> None:
        self._progress.set()
        self._progress = asyncio.Event()

    def _append_local(self, command: dict) -> int:
        self.log.append({"term": self.node.current_term, "command": deepcopy(command)})
        return self.last_index

    def _require_leader(self, term: int | None = None) -> None:
        if not self.is_leader or (term is not None and term != self.node.current_term):
            raise NotLeader(self.node.nodes.get(self.node.leader_id))

    async def _wait_applied(self, index: int, term: int) -> None:
        while self.last_applied < index:
            self._require_leader(term)
            await self._progress.wait()
        self._require_leader(term)

    async def submit(self, command: dict) -> dict:
        self._require_leader()
        term = self.node.current_term
        try:
            return await asyncio.wait_for(self._submit(command, term), self.commit_timeout)
        except asyncio.TimeoutError as error:
            raise CommitTimeout("Commit konnte nicht bestätigt werden.") from error

    async def _submit(self, command: dict, term: int) -> dict:
        async with self._submit_lock:
            self._require_leader(term)
            # Nach einem Timeout kann der vorherige Befehl noch committen.
            # Vor der nächsten Vorabprüfung muss er angewendet sein.
            await self._wait_applied(self.last_index, term)
            result = self.state_machine.precheck(command)
            if result is not None:
                return result
            index = self._append_local(command)
            pending = asyncio.get_running_loop().create_future()
            self._pending[index] = pending
            try:
                for wakeup in self._wakeups.values():
                    wakeup.set()
                self.advance_commit_index()
                await self._wait_applied(index, term)
                return pending.result()
            finally:
                self._pending.pop(index, None)

    async def views(self, game_id: str) -> dict:
        self._require_leader()
        term = self.node.current_term
        try:
            await asyncio.wait_for(self._confirm_read(term), self.commit_timeout)
        except asyncio.TimeoutError as error:
            raise CommitTimeout("Leader ist noch nicht bereit.") from error
        self._require_leader(term)
        return self.state_machine.views(game_id)

    async def _confirm_read(self, term: int) -> None:
        self._require_leader(term)
        assert self._leader_noop is not None
        await self._wait_applied(self._leader_noop, term)
        # Frische Antworten bestätigen, dass der Leader noch ein Quorum hat.
        requested_at = asyncio.get_running_loop().time()
        for wakeup in self._wakeups.values():
            wakeup.set()
        majority = len(self.node.nodes) // 2 + 1
        while 1 + sum(time >= requested_at for time in self._confirmed_send_time.values()) < majority:
            self._require_leader(term)
            await self._progress.wait()

    def handle_append_entries(self, term: int, leader_id: str, prev_log_index: int,
                              prev_log_term: int, entries: list[LogEntry],
                              leader_commit: int) -> dict:
        if term < self.node.current_term:
            return {"term": self.node.current_term, "success": False}
        self.node.accept_leader(term, leader_id)
        if prev_log_index < 0 or prev_log_index > self.last_index:
            return {"term": term, "success": False, "conflict_index": self.last_index + 1}
        if self.term_at(prev_log_index) != prev_log_term:
            first = prev_log_index
            conflict_term = self.term_at(first)
            while first > 1 and self.term_at(first - 1) == conflict_term:
                first -= 1
            return {"term": term, "success": False, "conflict_index": max(1, first)}
        for offset, entry in enumerate(entries):
            index = prev_log_index + offset + 1
            if index <= self.last_index and self.term_at(index) == entry["term"]:
                continue
            if index <= self.commit_index:
                return {"term": term, "success": False, "conflict_index": index}
            self.log[index - 1:] = deepcopy(entries[offset:])
            break
        # Ein Heartbeat bestätigt nur den bis hierhin geprüften Teil des Logs.
        confirmed_index = prev_log_index + len(entries)
        if leader_commit > self.commit_index:
            self.commit_index = max(self.commit_index, min(leader_commit, confirmed_index))
            self.apply_committed()
        return {"term": term, "success": True}

    def advance_commit_index(self) -> None:
        if not self.is_leader:
            return
        # Der Median für die Mehrheit schließt das lokale Log des Leaders ein.
        replicated = sorted([self.last_index, *self.match_index.values()], reverse=True)
        majority_index = replicated[len(replicated) // 2]
        if majority_index > self.commit_index and self.term_at(majority_index) == self.node.current_term:
            self.commit_index = majority_index
            self.apply_committed()
            for wakeup in self._wakeups.values():
                wakeup.set()

    def apply_committed(self) -> None:
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            entry = self.log[self.last_applied - 1]
            try:
                result = self.state_machine.apply(entry["command"])
            except Exception:
                log.exception("Anwenden des Log-Eintrags %d fehlgeschlagen", self.last_applied)
                result = {"ok": False, "error": "internal", "message": "Anwenden fehlgeschlagen."}
            pending = self._pending.get(self.last_applied)
            if pending is not None:
                pending.set_result(result)
        self._signal_progress()

    async def _replicate(self, peer: str, wakeup: asyncio.Event) -> None:
        while self.is_leader and self._wakeups.get(peer) is wakeup:
            wakeup.clear()
            term = self.node.current_term
            start = self.next_index[peer]
            previous = start - 1
            entries = self.log[previous:previous + 200]
            sent_at = asyncio.get_running_loop().time()
            response = await self.transport.call(peer, "append_entries", {
                "term": term,
                "leader_id": self.node.node_id,
                "prev_log_index": previous,
                "prev_log_term": self.term_at(previous),
                "entries": entries,
                "leader_commit": self.commit_index,
            })
            if not self.is_leader or self._wakeups.get(peer) is not wakeup:
                return
            if response is not None and response["term"] > term:
                self.node.step_down(response["term"])
                return
            if response is not None and response["term"] == term:
                self.peer_last_response[peer] = asyncio.get_running_loop().time()
                self._confirmed_send_time[peer] = sent_at
                self._signal_progress()
                if response["success"]:
                    matched = previous + len(entries)
                    self.match_index[peer] = matched
                    self.next_index[peer] = matched + 1
                    self.advance_commit_index()
                    if matched < self.last_index:
                        continue
                else:
                    retry = max(1, min(start - 1, response.get("conflict_index", start - 1)))
                    self.next_index[peer] = retry
                    # Auch ein ohne Log neu gestarteter Follower kann hier ablehnen.
                    self.match_index[peer] = 0
                    if retry < start:
                        continue
            try:
                await asyncio.wait_for(wakeup.wait(), self.heartbeat)
            except asyncio.TimeoutError:
                pass
