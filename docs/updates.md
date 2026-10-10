# Updates

eks-harness updates from its GitHub source. An update installs one exact commit with
`uv tool install --force --reinstall --link-mode copy "eks-harness[<installed extras>] @ git+<repository>@<commit>"`,
so the web UI, workers and scene runtime are built on the machine (Node.js 22+ and pnpm on PATH, as for
the first install), and then restarts the daemon gracefully (leases, devices, browsers and backends are
kept) and the node agent when its service is installed.

The install always copies its files (`--link-mode copy`), whatever `link-mode` the uv config sets: a
symlinked install points into the uv cache, and `uv cache clean` or `uv cache prune` then leaves the
package with dangling files (the web UI first among them).

## Command

```bash
eks-harness update --check        # installed commit, latest commit, the commits in between
eks-harness update                # install the latest commit of update.ref and restart
eks-harness update --ref v1.2     # a branch, tag or commit
eks-harness update --force        # reinstall, or replace an install built from a local checkout
eks-harness update --no-restart --json
```

The installed commit comes from the package's `direct_url.json` (git installs) or from `gitCommit` in
the build info (local builds). The latest commit is read with `git ls-remote`, which uses git's own
credentials, so a private repository works wherever `git clone` does; `gh api` is the fallback and
lists the commits between the two.

## Automatic updates

The daemon (service mode) checks every `update.checkMinutes` (60) and installs a new commit when nothing
is running: no live leases, no render running or queued, no active node job. Until then it waits and
reports why. After the install it restarts itself gracefully. A failed install is not retried for the
same commit; a newer commit or an explicit apply tries again.

| Setting | Default | |
| --- | --- | --- |
| `update.auto` | true | install new commits automatically |
| `update.repository` | `https://github.com/eksenova/eks-harness.git` | where updates come from |
| `update.ref` | `main` | branch, tag or commit to follow |
| `update.checkMinutes` | 60 | check interval |
| `update.replaceLocal` | false | let automatic updates replace an install built from a local checkout |

Installs from a local checkout (`uv tool install "eks-harness @ /path"`) and editable installs are left
alone by automatic updates unless `update.replaceLocal` is on; `eks-harness update --force` switches
them to the GitHub build.

## API

`GET /api/update` returns the state (`idle`, `checking`, `available`, `waiting`, `installing`,
`restarting`, `error`), the message, the installed build, the last check result with its commits, what
keeps the daemon busy, the last update and the last failure. Admins can `POST /api/update/check` and
`POST /api/update/apply` (`{"force": true}` reinstalls even when up to date); apply waits for idle like
the automatic update.

Nodes installed from a hub wheel do not reach GitHub themselves; run `eks-harness update` on a node that
can, or reinstall it from the hub with `eks-harness node add`.
