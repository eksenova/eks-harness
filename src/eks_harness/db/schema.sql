CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    builtin INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

INSERT INTO users (username, password_hash, role, disabled, builtin, created_at, updated_at)
VALUES ('local', NULL, 'admin', 0, 1, strftime('%s', 'now'), strftime('%s', 'now'));

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    implicit INTEGER NOT NULL DEFAULT 1 CHECK (implicit IN (0, 1)),
    retention_days INTEGER CHECK (retention_days IS NULL OR retention_days > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (owner, name)
);

CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE ON UPDATE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_active_at REAL NOT NULL,
    UNIQUE (project_id, slug)
);

CREATE INDEX sessions_last_active ON sessions (project_id, last_active_at DESC);

CREATE TABLE leases (
    id TEXT PRIMARY KEY,
    sid TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('browser', 'ios', 'android')),
    resource TEXT,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    owner_instance TEXT NOT NULL,
    owner_kind TEXT NOT NULL DEFAULT 'agent' CHECK (owner_kind IN ('agent', 'manual')),
    owner_user INTEGER REFERENCES users(id) ON DELETE SET NULL,
    backend_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('queued', 'active', 'idle', 'released', 'broken')),
    phase TEXT CHECK (phase IS NULL OR phase IN ('preparing', 'ready', 'failed')),
    reason TEXT,
    error TEXT,
    queued_at REAL NOT NULL,
    acquired_at REAL,
    heartbeat_at REAL,
    last_poll_at REAL,
    released_at REAL,
    idle_limit REAL,
    owner_pid INTEGER,
    owner_started TEXT,
    tree TEXT,
    state_dir TEXT,
    label TEXT,
    previous_sid TEXT,
    meta TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX leases_state_kind ON leases (state, kind, queued_at);
CREATE INDEX leases_instance ON leases (owner_instance, kind, state);
CREATE INDEX leases_session ON leases (session_id, state);
CREATE INDEX leases_resource ON leases (resource, acquired_at DESC);
CREATE UNIQUE INDEX leases_one_live_holder ON leases (resource) WHERE state IN ('active', 'idle') AND resource IS NOT NULL;

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE ON UPDATE CASCADE,
    session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    lease_sid TEXT,
    kind TEXT NOT NULL,
    mime TEXT NOT NULL DEFAULT 'application/octet-stream',
    filename TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    width INTEGER,
    height INTEGER,
    duration_ms INTEGER,
    caption TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    source TEXT NOT NULL DEFAULT 'cli' CHECK (source IN ('agent', 'cli', 'ui', 'mcp')),
    created_by TEXT,
    created_at REAL NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX artifacts_project ON artifacts (project_id, id DESC);
CREATE INDEX artifacts_session ON artifacts (session_id, id DESC);
CREATE INDEX artifacts_kind ON artifacts (kind, id DESC);
CREATE INDEX artifacts_lease ON artifacts (lease_sid);
CREATE INDEX artifacts_retention ON artifacts (project_id, pinned, created_at);

CREATE TABLE artifact_tags (
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (artifact_id, tag)
);

CREATE INDEX artifact_tags_tag ON artifact_tags (tag);

CREATE VIRTUAL TABLE artifacts_fts USING fts5 (
    artifact_id UNINDEXED,
    filename,
    caption,
    tags,
    session_name,
    project_id,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER artifacts_fts_insert AFTER INSERT ON artifacts BEGIN
    INSERT INTO artifacts_fts (artifact_id, filename, caption, tags, session_name, project_id)
    VALUES (
        new.id,
        new.filename,
        new.caption,
        COALESCE((SELECT group_concat(tag, ' ') FROM artifact_tags WHERE artifact_id = new.id), ''),
        COALESCE((SELECT name FROM sessions WHERE id = new.session_id), ''),
        new.project_id
    );
END;

CREATE TRIGGER artifacts_fts_update AFTER UPDATE OF filename, caption, session_id, project_id ON artifacts BEGIN
    UPDATE artifacts_fts SET
        filename = new.filename,
        caption = new.caption,
        session_name = COALESCE((SELECT name FROM sessions WHERE id = new.session_id), ''),
        project_id = new.project_id
    WHERE artifact_id = new.id;
END;

CREATE TRIGGER artifacts_fts_delete AFTER DELETE ON artifacts BEGIN
    DELETE FROM artifacts_fts WHERE artifact_id = old.id;
END;

CREATE TRIGGER artifact_tags_fts_insert AFTER INSERT ON artifact_tags BEGIN
    UPDATE artifacts_fts SET
        tags = COALESCE((SELECT group_concat(tag, ' ') FROM artifact_tags WHERE artifact_id = new.artifact_id), '')
    WHERE artifact_id = new.artifact_id;
END;

CREATE TRIGGER artifact_tags_fts_delete AFTER DELETE ON artifact_tags BEGIN
    UPDATE artifacts_fts SET
        tags = COALESCE((SELECT group_concat(tag, ' ') FROM artifact_tags WHERE artifact_id = old.artifact_id), '')
    WHERE artifact_id = old.artifact_id;
END;

CREATE TRIGGER sessions_fts_rename AFTER UPDATE OF name ON sessions BEGIN
    UPDATE artifacts_fts SET session_name = new.name
    WHERE artifact_id IN (SELECT id FROM artifacts WHERE session_id = new.id);
END;

CREATE TRIGGER projects_fts_rename AFTER UPDATE OF id ON projects BEGIN
    UPDATE artifacts_fts SET project_id = new.id WHERE project_id = old.id;
END;

CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    lease_sid TEXT,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX notes_session ON notes (session_id, created_at);

CREATE TABLE api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    prefix TEXT NOT NULL UNIQUE,
    secret_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL
);

CREATE INDEX api_keys_user ON api_keys (user_id);

CREATE TABLE grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE ON UPDATE CASCADE,
    session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    level TEXT NOT NULL CHECK (level IN ('viewer', 'editor')),
    created_at REAL NOT NULL
);

CREATE UNIQUE INDEX grants_unique ON grants (user_id, project_id, COALESCE(session_id, 0));
CREATE INDEX grants_project ON grants (project_id);

CREATE TABLE web_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen_at REAL,
    ip TEXT,
    user_agent TEXT
);

CREATE INDEX web_sessions_user ON web_sessions (user_id);
CREATE INDEX web_sessions_expiry ON web_sessions (expires_at);

CREATE TABLE shares (
    token TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    created_by TEXT,
    created_at REAL NOT NULL,
    expires_at REAL,
    revoked_at REAL,
    views INTEGER NOT NULL DEFAULT 0,
    last_viewed_at REAL
);

CREATE INDEX shares_artifact ON shares (artifact_id);

CREATE TABLE seen (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    seen_at REAL NOT NULL,
    PRIMARY KEY (user_id, artifact_id)
) WITHOUT ROWID;

CREATE INDEX seen_artifact ON seen (artifact_id);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    resource TEXT,
    lease_sid TEXT,
    session_id INTEGER,
    project_id TEXT,
    actor TEXT,
    type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX events_ts ON events (ts);
CREATE INDEX events_resource ON events (resource, id DESC);
CREATE INDEX events_session ON events (session_id, id DESC);
CREATE INDEX events_lease ON events (lease_sid, id DESC);
CREATE INDEX events_project ON events (project_id, id DESC);

CREATE TABLE settings_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user TEXT,
    key TEXT NOT NULL,
    old TEXT,
    new TEXT
);

CREATE INDEX settings_audit_ts ON settings_audit (ts DESC);
