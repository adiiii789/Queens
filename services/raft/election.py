"""Leader-Wahl und Check-Quorum (Raft, Abschnitt 5.2 und 5.4.1 im Paper).

Wahl: Term + 1, für sich selbst stimmen, `RequestVote` parallel an alle
anderen Knoten. Bei Mehrheit wird der Kandidat Leader; antwortet jemand mit
höherem Term, wird er wieder Follower. Bleibt die Wahl ohne Ergebnis (z. B.
geteilte Stimmen), startet der Ticker nach dem nächsten zufälligen Timeout
eine neue Wahl in einem höheren Term.

Abstimmen: höchstens eine Stimme pro Term, und nur für Kandidaten, deren Log
mindestens so aktuell ist wie das eigene (erst letzter Term, dann letzter
Index). Damit kann nur ein Knoten Leader werden, der alle bestätigten
Einträge hat.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .node import RaftNode

log = logging.getLogger("raft.election")


async def run_election(node: "RaftNode") -> None:
    from .node import CANDIDATE

    node.current_term += 1
    node.role = CANDIDATE
    node.voted_for = node.node_id
    node.leader_id = None
    node.persist()
    node.reset_election_timer()
    term = node.current_term
    last_index, last_term = node.last_log()
    log.info("Term %d: Election-Timeout – starte Wahl", term)

    votes = 1
    if votes >= node.majority:
        node.become_leader()
        return

    payload = {"term": term, "candidate_id": node.node_id,
               "last_log_index": last_index, "last_log_term": last_term}
    requests = [asyncio.create_task(node.transport.call(peer, "request_vote", payload))
                for peer in node.peers]
    try:
        for next_response in asyncio.as_completed(requests):
            try:
                response = await next_response
            except Exception:  # Transportfehler zählt wie keine Antwort
                response = None
            if node.current_term != term or node.role != CANDIDATE:
                return  # inzwischen überholt (anderer Leader, höherer Term)
            if not isinstance(response, dict):
                continue
            if response.get("term", 0) > term:
                node.step_down(response["term"])
                return
            if response.get("vote_granted") and response.get("term") == term:
                votes += 1
                if votes >= node.majority:
                    log.info("Term %d: %d von %d Stimmen", term, votes, len(node.nodes))
                    node.become_leader()
                    return
        log.info("Term %d: keine Mehrheit (%d von %d Stimmen)", term, votes, len(node.nodes))
    finally:
        for request in requests:
            request.cancel()


def handle_request_vote(node: "RaftNode", term: int, candidate_id: str,
                        last_log_index: int, last_log_term: int) -> dict:
    if term < node.current_term:
        return {"term": node.current_term, "vote_granted": False}
    if term > node.current_term:
        node.step_down(term)

    my_index, my_term = node.last_log()
    log_ok = (last_log_term, last_log_index) >= (my_term, my_index)
    free = node.voted_for in (None, candidate_id)
    granted = free and log_ok
    if granted:
        node.voted_for = candidate_id
        node.persist()
        node.reset_election_timer()  # wer abstimmt, kandidiert nicht gleich selbst
        log.info("Term %d: Stimme für Knoten %s", term, candidate_id)
    elif not log_ok:
        log.info("Term %d: keine Stimme für %s (Log veraltet)", term, candidate_id)
    return {"term": node.current_term, "vote_granted": granted}


def has_quorum(node: "RaftNode", now: float) -> bool:
    """Check-Quorum für den Leader: Haben innerhalb des maximalen
    Election-Timeouts genug Follower geantwortet, dass er mit sich selbst eine
    Mehrheit hat? Kurz nach der Wahl gilt eine Schonfrist von derselben Länge."""
    if len(node.nodes) == 1 or now - node.leader_since < node.election_max:
        return True
    responses = getattr(node.replication, "peer_last_response", {})
    window_start = now - node.election_max
    acks = 1 + sum(responses.get(peer, float("-inf")) >= window_start for peer in node.peers)
    return acks >= node.majority
