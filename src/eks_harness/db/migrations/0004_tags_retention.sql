CREATE TABLE tag_colors (
    tag TEXT PRIMARY KEY,
    color TEXT NOT NULL CHECK (color GLOB '#[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    updated_at REAL NOT NULL,
    updated_by TEXT
) WITHOUT ROWID;

ALTER TABLE artifacts ADD COLUMN retention_days INTEGER CHECK (retention_days IS NULL OR retention_days > 0);

CREATE TABLE projects_rebuilt (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    implicit INTEGER NOT NULL DEFAULT 1 CHECK (implicit IN (0, 1)),
    retention_days INTEGER CHECK (retention_days IS NULL OR retention_days >= 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (owner, name)
);

INSERT INTO projects_rebuilt (id, owner, name, title, description, implicit, retention_days, created_at, updated_at)
SELECT id, owner, name, title, description, implicit, retention_days, created_at, updated_at FROM projects;

DROP TABLE projects;

ALTER TABLE projects_rebuilt RENAME TO projects;

CREATE TRIGGER projects_fts_rename AFTER UPDATE OF id ON projects BEGIN
    UPDATE artifacts_fts SET project_id = new.id WHERE project_id = old.id;
END;

INSERT OR IGNORE INTO artifact_tags (artifact_id, tag)
SELECT a.id, 'web' FROM artifacts a JOIN leases l ON l.sid = a.lease_sid WHERE l.kind = 'browser';

INSERT OR IGNORE INTO artifact_tags (artifact_id, tag)
SELECT a.id, 'mobile' FROM artifacts a JOIN leases l ON l.sid = a.lease_sid WHERE l.kind IN ('ios', 'android');

INSERT OR IGNORE INTO artifact_tags (artifact_id, tag)
SELECT a.id, l.kind FROM artifacts a JOIN leases l ON l.sid = a.lease_sid WHERE l.kind IN ('ios', 'android');
