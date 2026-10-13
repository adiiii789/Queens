"""Regel-Konfiguration für Queens.

Alle Regelvarianten, über die sich die Gruppe noch einigen kann, sind hier
gebündelt. Die Konfiguration ist Teil des Spielzustands und wird damit
später automatisch mit repliziert.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RulesConfig:
    min_players: int = 2
    max_players: int = 8
    hand_size: int = 4
    initial_peek_count: int = 2

    # Hinweis: Bube/König lösen nur beim Ablegen durch den aktiven Spieler
    # eine Sonderaktion aus, nicht beim Zwischenwerfen.

    # Sind die Karten des klopfenden Spielers ab dem Klopfen gesperrt?
    # (kein Zwischenwerfen durch ihn, kein Bube-Tausch mit seinen Karten)
    knocker_hand_locked: bool = True

    # Strafkarte bei falschem Zwischenwerfen
    penalty_on_wrong_throw_in: bool = True
