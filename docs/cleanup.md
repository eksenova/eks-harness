# Cleanup

The Evidence > Clean up page (`/cleanup`) deletes old artifacts by conditions. It previews what matches
while you edit the conditions and deletes only after a confirmation. It reaches only projects and
sessions where you are an editor.

## Conditions

All conditions combine with AND; lists inside one condition match any of their values.

| Field | Meaning |
| --- | --- |
| `projects` | project ids; empty means every project you can edit |
| `sessions`, `excludeSessions` | session slugs or names to keep to, or to leave out |
| `sessionPattern` | wildcard on the session slug or name (`feature-*`) |
| `projectLevel` | `include` (default), `exclude` or `only` artifacts without a session |
| `sessionIdleDays` | the session's last activity is older than this |
| `olderThanDays`, `createdAfter`, `createdBefore` | creation age, or a range (epoch seconds) |
| `kinds` | artifact kinds (`screenshot`, `video`, ...) |
| `tagsAny`, `tagsAll`, `tagsNone` | has any, all or none of these tags |
| `sources` | `agent`, `cli`, `ui`, `mcp` |
| `minSize`, `maxSize` | bytes |
| `q` | text in the name, caption or tags |
| `seen` | `never` (nobody opened it), `unseen` or `seen` (by you) |
| `includePinned`, `includeShared`, `includeLive` | also delete pinned artifacts, artifacts with live share links, captures of live leases (all off by default) |

## API

`POST /api/cleanup/preview` with `{"filter": {...}, "sample": 50, "order": "oldest|newest|largest"}`
returns `count` and `bytes` (what would be deleted), `matched` (before protections), `skipped`
(`pinned`, `shared`, `live`), `oldest`, `newest`, breakdowns `projects`, `sessions`, `kinds`, `tags`,
`emptySessions` (sessions left with no artifacts), `sample` (artifacts) and `empty` (no condition set).

`POST /api/cleanup/apply` with `{"filter": {...}, "expectCount": N, "removeEmptySessions": true}` deletes
in batches, re-checking the conditions inside each delete. It refuses an empty filter (400) and a count
that changed since the preview (409 `cleanup_changed`, preview again). With `removeEmptySessions`,
sessions left without artifacts are removed unless they have notes or live leases.

## Backends

Backends clean themselves up. An instance with no lease left waits for its idle timeout (`backend.idleTimeout`,
default `15m`, overridden by `[backends] idle_timeout` in the backend tree's `.harness/project.toml`, the
definition, or the acquire request; `0` stops at once, `never` keeps it), then the daemon stops it and
runs the project's `cleanup` hook (older plugins: `teardown(final=True)`), which drops the instance's
data, and forgets the record and its stored secrets. Leases that are not renewed expire after their ttl,
so a crashed agent cannot keep a backend alive. A new lease inside the idle window cancels the cleanup
and reuses the instance.

By hand: `eks-harness backend stop --id <backend> [--cleanup]` (or Stop on the backend's page) stops it
now and ends its leases; without `--cleanup` the data stays and the record waits, stopped, for its idle
timeout before the cleanup runs. After a daemon restart, instances whose processes died and that nobody
holds are cleaned up the same way, and processes whose backend record is gone are stopped.
