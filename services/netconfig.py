"""Gemeinsame Hilfen für Adressen und Logging."""

from __future__ import annotations

import logging
import socket


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


def local_ips() -> list[str]:
    """IPv4-Adressen dieses Rechners, über die andere ihn erreichen können
    (ohne 127.x). Die Adresse der Standardroute steht vorn."""
    addresses: list[str] = []
    try:  # Adresse der Standardroute; dabei wird nichts gesendet
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            addresses.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        addresses += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return [a for a in dict.fromkeys(addresses) if not a.startswith("127.")]


def setup_logging(name: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{name}] %(message)s",
        datefmt="%H:%M:%S",
    )
