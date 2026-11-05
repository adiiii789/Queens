"""Dauerhafter Raft-Zustand eines Knotens als JSON-Datei (optional, `--data-dir`).

Raft verlangt, dass `current_term` und `voted_for` vor jeder Antwort auf
Platte stehen. Sonst könnte ein neu gestarteter Knoten im selben Term ein
zweites Mal abstimmen, und es gäbe zwei Leader in einem Term.

Geschrieben wird atomar: erst in eine temporäre Datei, dann per
`os.replace` umbenennen. Ein Absturz mitten im Schreiben hinterlässt so
entweder den alten oder den neuen Stand, nie eine halbe Datei.

Das Log selbst wird hier nicht gesichert: Ein neu gestarteter Knoten holt es
vom Leader nach (siehe Reflexion in der Ausarbeitung).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("raft.storage")


class JsonStorage:
    def __init__(self, data_dir: str | os.PathLike, node_id: str) -> None:
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"raft-{node_id}.json"

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            log.warning("Raft-Zustand %s nicht lesbar (%s), starte leer", self.path, error)
            return {}
        if not isinstance(data, dict):
            return {}
        log.info("Raft-Zustand geladen: Term %s, Stimme %s",
                 data.get("current_term"), data.get("voted_for"))
        return data

    def save(self, current_term: int, voted_for: Optional[str]) -> None:
        temporary = self.path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump({"current_term": current_term, "voted_for": voted_for}, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.path)
