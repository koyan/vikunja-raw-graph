# vikunja-raw-graph

A self-hosted [Vikunja](https://vikunja.io) task manager, plus a tool that turns
its task "blocked by" / "blocks" relations into an actual GraphViz dependency
graph — either as a one-off SVG file or as an always-on, password-protected
HTTP endpoint (`/raw-graph`) served alongside Vikunja itself.

## Contents

- [Stack overview](#stack-overview)
- [Setup](#setup)
- [Running Vikunja](#running-vikunja)
- [The dependency graph](#the-dependency-graph)
  - [How it works](#how-it-works)
  - [One-off local export](#one-off-local-export)
  - [Always-on HTTP endpoint](#always-on-http-endpoint)
  - [Task labels as "responsible person"](#task-labels-as-responsible-person)
- [Backups](#backups)
- [Caveats](#caveats)

## Stack overview

| Service             | Purpose                                                              | Always on? |
|----------------------|-----------------------------------------------------------------------|------------|
| `db`                 | Postgres, Vikunja's database                                          | yes |
| `redis`              | Vikunja's cache                                                       | yes |
| `api`                | Vikunja itself (API + bundled frontend), on port `3456`               | yes |
| `caddy`              | Reverse proxy / TLS termination, on `80`/`443`                        | yes |
| `kroki` / `mermaid`  | Self-hosted GraphViz renderer (never the public `kroki.io`)           | yes |
| `raw-graph-server`   | HTTP wrapper around the export below; served at `/raw-graph` via Caddy | yes |
| `raw-graph-export`   | The same code as a one-off CLI (`make raw-graph`)                     | on demand |

`raw-graph-export`/`raw-graph-server` talk to Vikunja over the internal
Docker network (`http://api:3456`), never through Caddy.

## Setup

Requires Docker and Docker Compose. Nothing else needs to be installed on the
host — the dependency-graph tooling runs entirely in its own container too.

```bash
cp env.example .env
```

Fill in `.env`:

| Variable | Used for |
|---|---|
| `DOMAIN`, `PUBLIC_URL` | The domain Caddy serves on / Vikunja uses for links. Use a real domain in production — `localhost` gets Caddy's self-signed local CA, which browsers won't trust (see [Caveats](#caveats)). |
| `POSTGRES_USER/PASSWORD/DB` | Vikunja's database credentials |
| `JWT_SECRET` | Generate with `openssl rand -hex 32` |
| `TIMEZONE` | For due dates/reminders, e.g. `Europe/Athens` |
| `SERVER_IP`, `SERVER_USER`, `SERVER_DIRECTORY` | Only for `make ssh`/`make dbfetch` against a remote deploy . Do not set them up in production. ONLY IN DEV |
| `VIKUNJA_API_TOKEN` | A token from Vikunja *Settings → API Tokens* (read access to tasks is enough) that the graph exporter authenticates with. Alternatively set `VIKUNJA_USERNAME`/`VIKUNJA_PASSWORD` and it'll log in for a JWT instead. |
| `RAW_GRAPH_USERNAME`, `RAW_GRAPH_PASSWORD` | HTTP Basic Auth for the `/raw-graph` endpoint. Both must be set — if either is missing, every request is rejected. |

## Running Vikunja

```bash
make up            # start everything in the background
make logs          # tail all services
make logs-api      # just Vikunja
make down          # stop and remove containers (volumes/data are kept)
make backup        # dump Postgres to ./backups/vikunja-<timestamp>.sql
```

Run `make help` for the full command list.

## The dependency graph

Vikunja's own UI doesn't visualize task dependencies. This exports every
task's `blocked`/`blocking` relations and renders them as a real
hierarchical GraphViz layout — dependencies flow left to right, arrows point
from blocker to blocked, and each task shows its assignee(s) and label.

### How it works

1. Fetch every task from Vikunja's API, along with each one's `related_tasks`.
2. Build a dependency graph with
   [taskdependencygraph](https://github.com/Hochfrequenz/task-dependency-graph)
   (used here purely for its graph structure — the "duration"/scheduling
   half of that library isn't meaningful for Vikunja tasks, which have no
   duration concept).
3. Render it to `dot` source and POST it to a self-hosted
   [kroki](https://kroki.io) instance, which runs GraphViz and returns SVG.

Node placement: nodes are declared in an order chosen so that tasks with more
dependency relations end up higher on the page and standalone tasks (no
relations at all) sink to the bottom. Nodes on the critical path (the longest
chain of not-yet-done tasks) are outlined in red.

### One-off local export

```bash
make raw-graph
```

Fetches current tasks, regenerates, and writes
`raw-graph-export/data/raw_graph.svg` — open it directly in a browser.

```bash
make raw-graph-build   # rebuild the image (e.g. after editing the script)
make raw-graph-clean   # remove generated output
```

### Always-on HTTP endpoint

The same export also runs continuously as `raw-graph-server`, reachable at:

```
https://<DOMAIN>/raw-graph
```

Every request regenerates the graph fresh from Vikunja (no caching) and
returns it as `image/svg+xml`, gated by HTTP Basic Auth
(`RAW_GRAPH_USERNAME`/`RAW_GRAPH_PASSWORD`). It starts automatically with
`make up`; `make raw-graph-server-logs` tails its logs.

### Task labels as "responsible person"

The exporter reads each task's Vikunja **labels** and treats them as who's
actually responsible for it: the label's title is added under the task name,
and the node is filled with the label's color. If a task has more than one
label, all their titles are shown but only the first label's color is used
(a node can only have one fill color).

## Backups

```bash
make backup     # dump Postgres locally to ./backups
make dbfetch     # pull the latest backup from the remote server (needs SERVER_* in .env)
make dbimport    # restore the latest local backup -- WIPES the local database first
```

## Caveats

- **Local `DOMAIN=localhost`**: Caddy can't get a real TLS cert for
  `localhost`, so it uses its own internal CA. Your browser won't trust it —
  accept the certificate warning to proceed. This goes away entirely with a
  real domain in production.
- **taskdependencygraph is a scheduling library, repurposed here for its graph
  structure only.** It requires a `planned_duration` per task to build its
  internal schedule (used for the critical-path calculation), but in my project
  tasks have no duration concept, so every task gets the same placeholder
  value. Node labels are built independently of that, though, and only ever
  show the task name, assignees, and responsibility label — never a
  fabricated duration or start time.
- **Cycles**: if Vikunja's relations ever form a genuine cycle (A blocks B
  blocks A), the export fails with a clear error naming the tasks involved,
  rather than silently producing a wrong graph.
