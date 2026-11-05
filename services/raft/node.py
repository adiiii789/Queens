"""Raft-Knoten: gemeinsamer Zustand, Rollen, Terms und Ticker.

Aufteilung im Team:
  - node.py, election.py (P3): Rollen, Terms, Leader-Wahl, Check-Quorum
  - replication.py (P4): Log, AppendEntries, Commit, Zustandsmaschine

Der Knoten kennt die Replikation nur über wenige Methoden (`ReplicationHooks`).
Umgekehrt ruft die Replikation am Knoten nur `accept_leader()` und
`step_down()` auf und liest `node_id`, `nodes`, `current_term`, `role` und
`leader_id`.

Ablauf:
  - Ein Ticker läuft alle 20 ms. Ist ein Follower oder Candidate länger als
    seinen (zufälligen) Election-Timeout ohne Nachricht vom Leader, startet er
    eine Wahl (election.py).
  - Ein Leader prüft im Ticker das Quorum: Hat ihm länger als den maximalen
    Election-Timeout keine Mehrheit geantwortet, tritt er zurück. So hält sich
    ein abgetrennter Leader nicht weiter für zuständig.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Mapping, Optional, Protocol

from . import election
from .transport import Transport

log = logging.getLogger("raft")

FOLLOWER = "follower"
CANDIDATE = "candidate"
LEADER = "leader"


class ReplicationHooks(Protocol):
    """Was der Knoten von der Log-Replikation (P4) braucht."""

    peer_last_response: dict[str, float]  # Antwortzeitpunkte (loop.time()) je Follower

    @property
    def last_index(self) -> int: ...
    def term_at(self, index: int) -> int: ...
    def on_become_leader(self) -> None: ...
    def fail_pending(self) -> None: ...
    def handle_append_entries(self, term: int, leader_id: str, prev_log_index: int,
                              prev_log_term: int, entries: list,
                              leader_commit: int) -> dict: ...
    async def close(self) -> None: ...


class Storage(Protocol):
    def load(self) -> dict[str, Any]: ...
    def save(self, current_term: int, voted_for: Optional[str]) -> None: ...


class RaftNode:
    def __init__(self, node_id: str, nodes: Mapping[str, str], transport: Transport, *,
                 election_min: float = 1.5, election_max: float = 3.0,
                 tick: float = 0.02, storage: Optional[Storage] = None,
                 rng: Optional[random.Random] = None) -> None:
        if node_id not in nodes:
            raise ValueError(f"Knoten {node_id!r} fehlt in der Knotenliste.")
        if not 0 < election_min <= election_max:
            raise ValueError("Es muss 0 < election_min <= election_max gelten.")
        self.node_id = node_id
        self.nodes: dict[str, str] = dict(nodes)  # id -> Basis-URL, inkl. sich selbst
        self.transport = transport
        self.election_min = election_min
        self.election_max = election_max
        self.tick_interval = tick
        self.storage = storage
        self.rng = rng or random.Random()

        self.current_term = 0
        self.voted_for: Optional[str] = None
        self.role = FOLLOWER
        self.leader_id: Optional[str] = None
        if storage is not None:
            saved = storage.load()
            self.current_term = int(saved.get("current_term", 0))
            self.voted_for = saved.get("voted_for")

        self.replication: Optional[ReplicationHooks] = None
        self.leader_since = 0.0           # Beginn der aktuellen Amtszeit (loop.time())
        self.election_deadline = 0.0
        self.elections_started = 0
        self._ticker: Optional[asyncio.Task] = None
        self._election: Optional[asyncio.Task] = None

    # --- Eigenschaften ----------------------------------------------------------
    @property
    def peers(self) -> list[str]:
        return [peer for peer in self.nodes if peer != self.node_id]

    @property
    def majority(self) -> int:
        return len(self.nodes) // 2 + 1

    @property
    def leader_url(self) -> Optional[str]:
        return self.nodes.get(self.leader_id) if self.leader_id else None

    def attach(self, replication: ReplicationHooks) -> None:
        self.replication = replication

    def _repl(self) -> ReplicationHooks:
        if self.replication is None:
            raise RuntimeError("Keine Replikation angebunden (attach() fehlt).")
        return self.replication

    def last_log(self) -> tuple[int, int]:
        """(letzter Index, Term des letzten Eintrags) für den Log-Vergleich."""
        replication = self._repl()
        index = replication.last_index
        return index, replication.term_at(index)

    @staticmethod
    def now() -> float:
        return asyncio.get_running_loop().time()

    # --- Lebenszyklus -----------------------------------------------------------
    async def start(self) -> None:
        self._repl()
        self.reset_election_timer()
        if len(self.nodes) == 1:
            self.election_deadline = self.now()  # Ein-Knoten-Cluster: sofort wählen
        self._ticker = asyncio.create_task(self._run_ticker(), name=f"raft-{self.node_id}-ticker")

    async def stop(self) -> None:
        for task in (self._ticker, self._election):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (self._ticker, self._election) if t),
                             return_exceptions=True)
        self._ticker = self._election = None
        if self.replication is not None:
            await self.replication.close()

    async def _run_ticker(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # der Ticker darf nie sterben
                log.exception("Fehler im Raft-Ticker")
            await asyncio.sleep(self.tick_interval)

    def tick(self) -> None:
        now = self.now()
        if self.role == LEADER:
            if not election.has_quorum(self, now):
                log.warning("Term %d: seit %.1f s keine Antwort von einer Mehrheit – "
                            "trete als Leader zurück (Check-Quorum)",
                            self.current_term, self.election_max)
                self.step_down(self.current_term)
        elif now >= self.election_deadline:
            self.start_election()

    def reset_election_timer(self) -> None:
        timeout = self.rng.uniform(self.election_min, self.election_max)
        self.election_deadline = self.now() + timeout

    def start_election(self) -> None:
        if self._election is not None and not self._election.done():
            self._election.cancel()  # vorige Wahl ohne Ergebnis (Split Vote)
        self.elections_started += 1
        self._election = asyncio.create_task(election.run_election(self),
                                             name=f"raft-{self.node_id}-election")

    # --- Zustandswechsel ----------------------------------------------------------
    def persist(self) -> None:
        if self.storage is not None:
            self.storage.save(self.current_term, self.voted_for)

    def _set_term(self, term: int) -> None:
        if term > self.current_term:
            self.current_term = term
            self.voted_for = None
            self.persist()

    def step_down(self, term: int) -> None:
        """Follower werden; bei höherem Term diesen übernehmen (Stimme frei).
        Wird auch von der Replikation aufgerufen, wenn ein Follower mit
        höherem Term antwortet."""
        was_leader = self.role == LEADER
        old_role = self.role
        self._set_term(term)
        self.role = FOLLOWER
        if was_leader:
            self.leader_id = None
            self.reset_election_timer()
            if self.replication is not None:
                self.replication.fail_pending()  # offene Anfragen: "nicht mehr Leader"
        if old_role != FOLLOWER:
            log.info("Term %d: %s -> Follower", self.current_term, _ROLE_NAMES[old_role])

    def accept_leader(self, term: int, leader_id: str) -> None:
        """Gültiges AppendEntries (term >= current_term) vom Leader erhalten."""
        if term > self.current_term or self.role != FOLLOWER:
            self.step_down(term)
        if self.leader_id != leader_id:
            log.info("Term %d: Leader ist Knoten %s", self.current_term, leader_id)
        self.leader_id = leader_id
        self.reset_election_timer()

    def become_leader(self) -> None:
        self.role = LEADER
        self.leader_id = self.node_id
        self.leader_since = self.now()
        log.info("Term %d: zum Leader gewählt", self.current_term)
        self._repl().on_become_leader()  # P4: No-Op anhängen, Replikatoren starten

    # --- RPCs ---------------------------------------------------------------------
    def handle_request_vote(self, term: int, candidate_id: str, last_log_index: int,
                            last_log_term: int) -> dict:
        return election.handle_request_vote(self, term, candidate_id,
                                            last_log_index, last_log_term)

    def handle_append_entries(self, **payload: Any) -> dict:
        return self._repl().handle_append_entries(**payload)

    def handle_rpc(self, rpc: str, payload: dict) -> dict:
        """Einstieg für den Transport (HTTP-Server bzw. InMemoryNetwork)."""
        if rpc == "request_vote":
            return self.handle_request_vote(**payload)
        if rpc == "append_entries":
            return self.handle_append_entries(**payload)
        raise ValueError(f"Unbekannter RPC: {rpc}")

    # --- Status -------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        replication = self.replication
        machine = getattr(replication, "state_machine", None)
        return {
            "node_id": self.node_id,
            "role": self.role,
            "term": self.current_term,
            "voted_for": self.voted_for,
            "leader_id": self.leader_id,
            "leader": self.leader_url,
            "log_length": replication.last_index if replication else 0,
            "commit_index": getattr(replication, "commit_index", 0),
            "last_applied": getattr(replication, "last_applied", 0),
            "games": len(getattr(machine, "games", {}) or {}),
            "cluster_size": len(self.nodes),
        }


_ROLE_NAMES = {FOLLOWER: "Follower", CANDIDATE: "Candidate", LEADER: "Leader"}
