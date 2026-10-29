# sdp server policy — what is rejected and how to fix it

The server checks the compose file **before** anything runs (`sdp validate`, and again in every
`sdp deploy`). Errors block the deploy; warnings do not. Never try to bypass the policy — fix the repo.

## Why these rules exist

The VPS can host other people's projects. Docker runs as root, so a single `privileged: true`,
a docker.sock mount or a host path mount would give full control of the server and every other
project. The policy makes sure one project can only affect its own containers, volumes and domains.

## Errors and fixes

| Error says | Fix in the repo |
|---|---|
| `ports: '0.0.0.0:8000:8000' is public` | `"127.0.0.1:20010:8000"` — nginx is the only public entry |
| `a fixed host port is required` | write the host port explicitly; get free ones with `sdp port-suggest` |
| `host port N is already used by ...` / `reserved by another project` | choose another port from `sdp port-suggest`, update compose + `sdp domain-add` |
| `privileged`, `cap_add X`, `security_opt ... unconfined`, `devices` | remove them; normal web apps never need them |
| `network_mode 'host'`, `pid/ipc host` | remove; services talk to each other by service name (`db`, `redis`) |
| `mounting the Docker socket` | remove; containers must not control Docker |
| `volumes: '/some/path' is outside the project` | use a named volume (`pgdata:/var/lib/postgresql/data`) or a path inside the repo (`./config:/app/config:ro`) |
| `env_file ... points outside the project` | use `env_file: .env` (the server's managed .env at the repo root) |
| `variables are not allowed in file paths` | write the path literally |
| `external volumes/networks are forbidden` | remove `external: true`; let compose create project-scoped ones |
| `name 'x' must start with 'sdp-<project>_'` | remove the custom `name:` on volumes/networks |
| `built images must be named 'sdp-<project>-<service>'` | remove `image:` from services that have `build:` |
| `build.ssh` / `build.cache_to` / `cache_from type=local` | remove; install private deps with a token build secret instead |
| `secret file is committed to git: .env...` | `git rm --cached <file>`, add to `.gitignore`, **rotate** those secrets |
| `variables used by the compose file but missing from the server .env` | `sdp env-gen KEY` / `sdp env-set KEY VALUE` / human: `sdp-admin secret <project> KEY`, or `${KEY:-default}` in compose |
| `X is not allowed by the server policy` | an unusual compose key; remove it or ask the server owner |

## Warnings worth fixing

- **no memory limit** → `mem_limit: 512m` (shared server: one leaking service must not kill the others)
- **no restart policy** → `restart: unless-stopped` (or `"no"` for one-shot jobs)
- **container_name** → remove (names are global on the server)
- **image not pinned** → use a version tag (`postgres:16-alpine`, not `postgres` or `:latest`)
- **hard-coded secret in compose** → move to `.env`, reference as `${KEY}`

## What the server adds automatically

- Compose project name `sdp-<project>` (isolates containers, networks, volumes)
- Log rotation for every service (json-file, 10 MB × 3)
- `.env` symlink at the repo root → `/srv/apps/<project>/.env` (root-only, 0600)
