CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    plan_hash TEXT NOT NULL,
    plan_kind TEXT NOT NULL CHECK (plan_kind IN ('flow', 'seed', 'edit', 'scene', 'score')),
    plan_name TEXT NOT NULL,
    plan TEXT NOT NULL,
    spec TEXT,
    tree TEXT,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL ON UPDATE CASCADE,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    session_name TEXT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor TEXT,
    instance TEXT,
    status TEXT NOT NULL CHECK (status IN ('queued', 'preparing', 'running', 'paused', 'passed', 'failed',
                                           'aborted', 'error')),
    reason TEXT,
    options TEXT NOT NULL DEFAULT '{}',
    request TEXT NOT NULL DEFAULT '{}',
    environment TEXT NOT NULL DEFAULT '{}',
    outputs TEXT,
    counts TEXT NOT NULL DEFAULT '{}',
    cache TEXT NOT NULL DEFAULT '{}',
    simulated INTEGER NOT NULL DEFAULT 0 CHECK (simulated IN (0, 1)),
    replay_of TEXT REFERENCES runs(id) ON DELETE SET NULL,
    repeat_of TEXT REFERENCES runs(id) ON DELETE SET NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS runs_created ON runs (created_at DESC);
CREATE INDEX IF NOT EXISTS runs_project ON runs (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_session ON runs (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_plan ON runs (plan_hash, created_at DESC);

CREATE TABLE IF NOT EXISTS run_nodes (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    instance TEXT NOT NULL DEFAULT '',
    parent_id TEXT,
    parent_instance TEXT,
    seq INTEGER NOT NULL,
    op TEXT NOT NULL,
    node_hash TEXT NOT NULL,
    label TEXT,
    src TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'running', 'paused', 'passed', 'soft_failed', 'failed',
                                           'skipped', 'cancelled')),
    attempt INTEGER NOT NULL DEFAULT 0,
    args TEXT,
    output TEXT,
    has_output INTEGER NOT NULL DEFAULT 0 CHECK (has_output IN (0, 1)),
    error TEXT,
    cache TEXT CHECK (cache IS NULL OR cache IN ('hit', 'miss', 'stored', 'bypass', 'off')),
    cache_key TEXT,
    resolution TEXT,
    executor TEXT,
    location TEXT,
    simulated INTEGER NOT NULL DEFAULT 0 CHECK (simulated IN (0, 1)),
    edited INTEGER NOT NULL DEFAULT 0 CHECK (edited IN (0, 1)),
    artifacts TEXT NOT NULL DEFAULT '[]',
    queued_at REAL,
    started_at REAL,
    finished_at REAL,
    duration_ms INTEGER,
    PRIMARY KEY (run_id, node_id, instance)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS run_nodes_order ON run_nodes (run_id, seq);
CREATE INDEX IF NOT EXISTS run_nodes_status ON run_nodes (run_id, status);

CREATE TABLE IF NOT EXISTS node_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_id TEXT,
    instance TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 0,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS node_events_run ON node_events (run_id, id);
CREATE INDEX IF NOT EXISTS node_events_node ON node_events (run_id, node_id, instance, id);

CREATE TABLE IF NOT EXISTS run_expansions (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    instance TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('each', 'dynamic', 'branch')),
    detail TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    PRIMARY KEY (run_id, node_id, instance)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS run_resources (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    session_key TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'acquiring', 'held', 'released', 'failed')),
    handle TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    acquired_at REAL,
    released_at REAL,
    PRIMARY KEY (run_id, resource_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS cache_entries (
    key TEXT PRIMARY KEY,
    node_hash TEXT NOT NULL,
    op TEXT NOT NULL,
    value TEXT NOT NULL,
    blobs TEXT NOT NULL DEFAULT '[]',
    material TEXT NOT NULL DEFAULT '{}',
    resolution TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    run_id TEXT,
    created_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS cache_entries_node ON cache_entries (node_hash);
CREATE INDEX IF NOT EXISTS cache_entries_used ON cache_entries (last_used_at);
