-- Schema der zentralen PostgreSQL-Datenbank (Lobbys, Spieler, Ergebnisse).
-- Idempotent: wird von jeder Lobby-Instanz beim Start ausgeführt
-- (unter einem Advisory-Lock, siehe services/db.py).

CREATE TABLE IF NOT EXISTS lobbies (
    lobby_id          TEXT PRIMARY KEY,
    host_id           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'waiting'
                      CHECK (status IN ('waiting', 'starting', 'started', 'finished')),
    game_id           TEXT UNIQUE,
    cluster           TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lobby_players (
    lobby_id  TEXT NOT NULL REFERENCES lobbies (lobby_id) ON DELETE CASCADE,
    player_id TEXT NOT NULL,
    name      TEXT NOT NULL,
    ready     BOOLEAN NOT NULL DEFAULT FALSE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (lobby_id, player_id)
);

CREATE TABLE IF NOT EXISTS games (
    game_id     TEXT PRIMARY KEY,
    lobby_id    TEXT REFERENCES lobbies (lobby_id) ON DELETE SET NULL,
    knocker     TEXT,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS game_results (
    game_id   TEXT NOT NULL REFERENCES games (game_id) ON DELETE CASCADE,
    player_id TEXT NOT NULL,
    name      TEXT NOT NULL,
    score     INTEGER NOT NULL,
    winner    BOOLEAN NOT NULL,
    PRIMARY KEY (game_id, player_id)
);

-- Reihum-Verteilung neuer Partien auf die Game-Service-Cluster
CREATE SEQUENCE IF NOT EXISTS cluster_rr;
