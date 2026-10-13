"""Ereignisse, die die Engine bei jeder angewendeten Aktion erzeugt.

`audience` bestimmt den Empfängerkreis:
  - None        -> an alle Spieler
  - "<player>"  -> nur an diesen einen Spieler (z. B. eine angesehene Karte)

Das Gateway muss Ereignisse damit nur noch zustellen und keine Spiellogik
kennen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    audience: Optional[str] = None

    def is_visible_to(self, player_id: str) -> bool:
        return self.audience is None or self.audience == player_id

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "audience": self.audience}


def public(type_: str, **data: Any) -> Event:
    return Event(type_, data, None)


def private(player_id: str, type_: str, **data: Any) -> Event:
    return Event(type_, data, player_id)
