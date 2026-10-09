ALTER TABLE leases ADD COLUMN project_id TEXT REFERENCES projects(id) ON DELETE SET NULL ON UPDATE CASCADE;

UPDATE leases SET project_id = (SELECT s.project_id FROM sessions s WHERE s.id = leases.session_id)
WHERE session_id IS NOT NULL;

CREATE INDEX leases_project ON leases (project_id, state);

CREATE TEMP TABLE session_merge AS
SELECT s.id AS old_id,
       (SELECT MIN(t.id) FROM sessions t WHERE t.slug = s.slug) AS new_id,
       s.project_id AS project_id,
       s.created_at AS created_at,
       s.last_active_at AS last_active_at
FROM sessions s;

CREATE TABLE session_projects (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE ON UPDATE CASCADE,
    created_at REAL NOT NULL,
    last_active_at REAL NOT NULL,
    PRIMARY KEY (session_id, project_id)
) WITHOUT ROWID;

CREATE INDEX session_projects_project ON session_projects (project_id, last_active_at DESC);

INSERT INTO session_projects (session_id, project_id, created_at, last_active_at)
SELECT new_id, project_id, MIN(created_at), MAX(last_active_at) FROM session_merge GROUP BY new_id, project_id;

UPDATE artifacts SET session_id = (SELECT m.new_id FROM session_merge m WHERE m.old_id = artifacts.session_id)
WHERE session_id IN (SELECT old_id FROM session_merge WHERE old_id <> new_id);

UPDATE leases SET session_id = (SELECT m.new_id FROM session_merge m WHERE m.old_id = leases.session_id)
WHERE session_id IN (SELECT old_id FROM session_merge WHERE old_id <> new_id);

UPDATE notes SET session_id = (SELECT m.new_id FROM session_merge m WHERE m.old_id = notes.session_id)
WHERE session_id IN (SELECT old_id FROM session_merge WHERE old_id <> new_id);

UPDATE grants SET session_id = (SELECT m.new_id FROM session_merge m WHERE m.old_id = grants.session_id)
WHERE session_id IN (SELECT old_id FROM session_merge WHERE old_id <> new_id);

UPDATE events SET session_id = (SELECT m.new_id FROM session_merge m WHERE m.old_id = events.session_id)
WHERE session_id IN (SELECT old_id FROM session_merge WHERE old_id <> new_id);

CREATE TABLE sessions_shared (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_active_at REAL NOT NULL
);

INSERT INTO sessions_shared (id, name, slug, created_at, last_active_at)
SELECT s.id, s.name, s.slug, m.created_at, m.last_active_at
FROM sessions s
JOIN (SELECT new_id, MIN(created_at) AS created_at, MAX(last_active_at) AS last_active_at
      FROM session_merge GROUP BY new_id) m ON m.new_id = s.id;

DROP TABLE sessions;

ALTER TABLE sessions_shared RENAME TO sessions;

CREATE INDEX sessions_last_active ON sessions (last_active_at DESC);

CREATE TRIGGER sessions_fts_rename AFTER UPDATE OF name ON sessions BEGIN
    UPDATE artifacts_fts SET session_name = new.name
    WHERE artifact_id IN (SELECT id FROM artifacts WHERE session_id = new.id);
END;

UPDATE artifacts_fts SET session_name = COALESCE(
    (SELECT s.name FROM artifacts a JOIN sessions s ON s.id = a.session_id WHERE a.id = artifacts_fts.artifact_id), '');

DROP TABLE session_merge;
