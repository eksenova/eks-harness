CREATE TABLE IF NOT EXISTS artifact_versions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    file_rel_path TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'initial'
        CHECK (kind IN ('initial', 'rerender', 'recapture', 'restore')),
    spec TEXT NOT NULL DEFAULT '{}',
    style_name TEXT NOT NULL DEFAULT '',
    style_snapshot TEXT NOT NULL DEFAULT '{}',
    boxes TEXT NOT NULL DEFAULT '{}',
    report TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    created_by TEXT,
    UNIQUE (artifact_id, version_no)
);

CREATE INDEX IF NOT EXISTS artifact_versions_artifact
    ON artifact_versions (artifact_id, version_no);

CREATE TABLE IF NOT EXISTS project_assets (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    rel_path TEXT NOT NULL DEFAULT '',
    mime TEXT NOT NULL DEFAULT 'application/octet-stream',
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (project_id, name)
);
