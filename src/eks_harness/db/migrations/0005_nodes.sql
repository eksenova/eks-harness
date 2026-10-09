CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL DEFAULT '',
    token_prefix TEXT NOT NULL UNIQUE,
    token_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL,
    capabilities TEXT NOT NULL DEFAULT '{}',
    config TEXT NOT NULL DEFAULT '{}',
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS node_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
    slot TEXT,
    requirements TEXT NOT NULL DEFAULT '{}',
    payload TEXT NOT NULL DEFAULT '{}',
    inputs TEXT NOT NULL DEFAULT '[]',
    result TEXT,
    error TEXT,
    progress REAL,
    message TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    owner TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS node_jobs_state ON node_jobs(state, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS node_jobs_node ON node_jobs(node_id, state);
