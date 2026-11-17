"""Gemeinsame Hilfen für Adressen und Logging."""

from __future__ import annotations

import logging


def parse_nodes(spec: str) -> dict[str, str]:
    """'1=http://127.0.0.1:8101,2=http://...' -> {'1': 'http://...', ...}"""
    nodes: dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        node_id, _, addr = part.partition("=")
        if not addr:
            raise ValueError(f"Ungültiger Knoten-Eintrag: {part!r} (erwartet id=url)")
        nodes[node_id.strip()] = addr.strip().rstrip("/")
    return nodes


def parse_clusters(spec: str) -> dict[str, list[str]]:
    """'a=http://h:8101|http://h:8102|http://h:8103;b=...' -> {'a': [...], ...}

    Mehrere unabhängige Game-Service-Cluster ermöglichen horizontale
    Skalierung: die Lobby verteilt neue Partien reihum auf die Cluster.
    """
    clusters: dict[str, list[str]] = {}
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, urls = part.partition("=")
        clusters[name.strip()] = [u.strip().rstrip("/") for u in urls.split("|") if u.strip()]
    return clusters


def setup_logging(name: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{name}] %(message)s",
        datefmt="%H:%M:%S",
    )
