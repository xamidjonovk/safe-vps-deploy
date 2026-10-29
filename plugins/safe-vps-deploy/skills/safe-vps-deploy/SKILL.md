---
name: safe-vps-deploy
description: Securely deploy a docker-compose project (FastAPI, Next.js/React frontend, Postgres, Redis, Celery, MinIO...) from GitHub to a VPS that may already host other projects (Contabo, Hetzner, DigitalOcean...), with nginx + Let's Encrypt HTTPS on a domain and GitHub Actions push-to-deploy - without ever getting the root password or seeing secret values. Use when the user wants to deploy, publish or "put on the server" a project, set up CI/CD to a VPS, or connect a domain/HTTPS to a dockerized app.
---

# Safe VPS Deploy (sdp)

You deploy the user's GitHub project to their VPS end-to-end: production Dockerfiles and compose,
server setup, `.env`, nginx, HTTPS, GitHub Actions CI/CD, health checks, and a final report.
You do it through **sdp** (Safe Deploy Proxy), a small broker installed on the server. It makes
the deploy safe on a shared server: you log in as an unprivileged user `sdp-<project>` whose only
privilege is `sudo sdp <command>` for its own project.

Talk to the user in their language (Uzbek if they write Uzbek). Keep them informed at each phase.

## Non-negotiable rules

1. **Never ask for, accept, store or use the root password** (or root SSH). If the user pastes it,
   tell them to change it and do not use it. The one root-only step is done by the human (Phase 2).
2. **Never read, print or copy secret values**: no `cat .env`, no reading private keys, no echoing
   tokens. On the server, secrets are only touched through `sudo sdp env-gen` (random, never shown)
   or by the human via `sdp-admin secret`. Locally, read only `.env.example`.
3. On the server run **only** `sudo sdp <command>` (plus harmless read-only shell like `ls`, `df`).
   Never try other sudo commands, docker, or files of other projects. Never try to weaken or bypass
   the sdp policy: if it rejects something, fix the compose file in the repo.
4. **Ask before**: `git push`, adding GitHub secrets/deploy keys, accepting Let's Encrypt terms
   (needs the user's email), and any action outside the plan. Everything else, just do.
5. Never touch other projects, the firewall, SSH config, other nginx sites, or global Docker settings.

## Files in this skill

- `server/` — copied to the VPS by the human: `install.sh`, `sdp`, `sdp-admin`, `sdp-ci-entry`, `sdp_core.py`
- `templates/docker-compose.prod.yml` — reference stack that passes the policy (read it before writing compose)
- `templates/Dockerfile.fastapi`, `templates/Dockerfile.nextjs`, `templates/dockerignore.txt`
- `templates/github-workflows/deploy.yml` — CI workflow
- `templates/claude-code/settings.json` — optional local deny rules for `.env` reading
- `references/policy.md` — what the server policy forbids and how to fix each error
- `references/troubleshooting.md` — common failures and fixes

Resolve this skill's directory first — the folder containing this SKILL.md; you need it for `server/`.
Typical locations: `~/.claude/skills/safe-vps-deploy`, `~/.agents/skills/safe-vps-deploy` (Codex), or the
Claude Code plugin cache. If unsure: `find ~/.claude ~/.agents -path '*safe-vps-deploy*/server/sdp_core.py' 2>/dev/null | head -1`.

## Phase 0 — Gather facts (one AskUserQuestion round, max 4 questions)

Collect, guessing sensible defaults from the repo where possible:
- **Server IP** (Contabo panel shows it).
- **Project name** — slug `[a-z][a-z0-9-]{1,30}` (default: repo name, lowercased). User becomes `sdp-<name>`.
- **Domain(s)** and which service each serves, e.g. `app.example.uz` -> frontend, `api.example.uz` -> backend
  (or one domain with `/api` path). The DNS **A record must point to the server IP** — tell the user to
  create it now so it propagates while you work.
- **Email for Let's Encrypt** and **branch** to deploy (default `main`).
Also check locally: `git remote get-url origin`, current branch, `gh auth status` (if `gh` is logged in
you can add the deploy key and secrets yourself; otherwise the user pastes them in GitHub UI).

## Phase 1 — Make the repository production-ready (local, no server needed)

Inspect the repo (Dockerfiles, compose files, `.env.example`, framework configs). Produce a root
`docker-compose.prod.yml` (or fix the existing compose) following `templates/docker-compose.prod.yml`:

- **Compose file at the repository root.** Services reference `env_file: .env` (root). sdp provides
  that `.env` on the server.
- **Ports**: only services that nginx must reach publish a port, always `"127.0.0.1:<20000-29999>:<container_port>"`.
  Postgres/Redis/Celery publish nothing. Pick ports later with `sudo sdp port-suggest` if unsure;
  keep 20010+ as a default and change if the server says they are busy.
- No `container_name`, no `privileged`, no `network_mode: host`, no docker.sock, no absolute host
  paths, no `external` volumes/networks, no `image:` on built services (or `sdp-<name>-<service>`).
- Named volumes for data (`pgdata`, `redisdata`, `miniodata`). `restart: unless-stopped` for long-running
  services, `restart: "no"` for one-shots. `mem_limit` on every service (shared server).
- **Healthchecks** for db/redis/minio/backend/frontend; `depends_on` with `condition: service_healthy`.
- **Migrations** as a one-shot `migrate` service (e.g. `alembic upgrade head`) that the backend
  `depends_on` with `condition: service_completed_successfully`.
- **FastAPI**: `uvicorn ... --proxy-headers --forwarded-allow-ips "*"`; a `/health` endpoint
  (add one if missing: returns `{"status":"ok"}`); CORS origins and allowed hosts from env vars.
- **Celery**: `worker` and `beat` services reuse the backend image (YAML anchor), broker
  `redis://redis:6379/0`.
- **Next.js**: `output: "standalone"`; `NEXT_PUBLIC_*` passed as **build args** (baked at build time).
  Vite/React SPA: build stage + `nginx:alpine` or `node` static server inside the container on a localhost port.
- **MinIO**: S3 API on a localhost port if files must be public (route a domain like `files.example.uz`
  to it), one-shot `minio-init` creating the bucket. Console (9001) stays private.
- Hard-coded secrets in compose → `${VAR}` from `.env`. Use `${VAR:?message}` for required ones.
- `.env.example` at the root listing **every** variable (placeholders only). Secret-looking keys
  (`*_PASSWORD`, `*_SECRET`, `*_TOKEN`, `*_KEY`, `DATABASE_URL`...) get empty or dummy values.
  Compose derived values with references, e.g. `DATABASE_URL=postgresql+asyncpg://app:${POSTGRES_PASSWORD}@db:5432/app`.
- `.gitignore` contains `.env` and `.env.*` (but not `.env.example`). If a real `.env` is tracked in
  git: `git rm --cached` it and tell the user to **rotate** those secrets.
- `.dockerignore` in each build context (template provided).
- Add `.github/workflows/deploy.yml` from the template (adjust branch).
- If `docker` is available locally, run `docker compose -f docker-compose.prod.yml config -q` with a
  dummy env file to catch syntax errors (never with the real `.env`).

Show the user a short summary of the changes, ask, then commit and push.

## Phase 2 — Human bootstrap (the ONLY root step; the human runs it in their own terminal)

1. Create a dedicated key for this agent and project (no passphrase, used only for this server):
   `ssh-keygen -t ed25519 -N "" -C "sdp-agent-<name>" -f ~/.ssh/sdp_<name>`
2. Give the user these commands to run **themselves** in a separate terminal (they type the root
   password; you never see it). Substitute real values; on Windows PowerShell replace
   `$(cat ...)` with `$(Get-Content ...)`:

```bash
scp -r <skill-dir>/server root@<IP>:/root/sdp-installer
ssh root@<IP> "bash /root/sdp-installer/install.sh"
ssh root@<IP> "sdp-admin add-project <name> --agent-key '$(cat ~/.ssh/sdp_<name>.pub)'"
```

   `install.sh` asks for confirmation, installs Docker/nginx/certbot only if missing, and refuses to
   run if ports 80/443 belong to something other than nginx (then stop: the server needs its owner).
   Recommend a Contabo **snapshot** before this step if the server already runs other projects.
3. Add an SSH alias locally (append to `~/.ssh/config`):

```
Host sdp-<name>
  HostName <IP>
  User sdp-<name>
  IdentityFile ~/.ssh/sdp_<name>
  IdentitiesOnly yes
```

4. Verify: `ssh -o StrictHostKeyChecking=accept-new sdp-<name> sudo sdp preflight`.
   Read it: Docker/compose/nginx/certbot present, nginx config valid, ports 80/443 owned by nginx,
   free disk/RAM, used ports. Report anything alarming to the user before continuing.

## Phase 3 — Repository and secrets on the server

Every server command below is `ssh sdp-<name> sudo sdp <command>`.

1. `repo-init <git-ssh-url> --branch <branch>` → prints a **public** deploy key.
   Add it read-only: with gh, save it to a temp file and
   `gh repo deploy-key add <file> -R <owner>/<repo> --title "sdp <name> server"` (no `--allow-write`);
   otherwise ask the user to add it under GitHub → Settings → Deploy keys. Public repos over https need no key.
2. `sync` — clones and checks out the branch (nothing runs yet).
3. `env-template` — copies keys from `.env.example`; secret keys are created **empty**.
4. Fill values:
   - random secrets you can generate: `env-gen POSTGRES_PASSWORD`, `env-gen SECRET_KEY`,
     `env-gen MINIO_ROOT_PASSWORD`, `env-gen JWT_SECRET` ... (never shown, never leave the server)
   - non-secret config: `env-set NEXT_PUBLIC_API_URL https://api.example.uz`, `env-set DEBUG false`,
     `env-set CORS_ORIGINS https://app.example.uz`, credential-free URLs like `redis://redis:6379/0`,
     and references to other keys. SSH runs the command through TWO shells (yours and the server's), so
     quote values that contain `$` or spaces like this:
     `ssh sdp-<name> "sudo sdp env-set DATABASE_URL 'postgresql+asyncpg://app:\${POSTGRES_PASSWORD}@db:5432/app'"`
     then confirm with `env-status` that the value still contains `${POSTGRES_PASSWORD}`
   - external secrets you cannot generate (payment, SMS, OpenAI, OAuth): list them and ask the user to run,
     in their terminal: `ssh root@<IP> sdp-admin secret <name> <KEY>` (hidden input). Wait for "done".
5. `env-status` — shows which keys are set/empty/missing (no secret values). Repeat until complete.
6. `config set le_email <email>`; if the backend has `/health`:
   `config set health_urls http://127.0.0.1:<backend_port>/health`.

## Phase 4 — Validate, deploy, domains, HTTPS

1. `validate` — policy + env check. On errors: fix compose in the repo, commit, push (ask), `sync`, repeat.
   Never argue with the policy; see `references/policy.md`.
2. `deploy` — fetch, validate, build, `up -d`, wait for every container to be running/healthy (one-shots
   must exit 0), HTTP checks, and **automatic rollback** to the last good commit on failure.
   First builds can take several minutes. On failure read the masked logs it prints, then
   `logs <service> --tail 200`, fix in the repo, push, redeploy.
3. Domains (DNS must already point to the server):
   - `domain-add app.example.uz <frontend_port>`
   - `domain-add api.example.uz <backend_port>` — or same domain with a path:
     `domain-add app.example.uz <backend_port> --path /api` (add `--strip` if FastAPI routes do not start with `/api`)
   - big uploads: `--max-body 100m`. Cloudflare-proxied DNS: `--skip-dns`.
4. HTTPS: confirm with the user that they accept Let's Encrypt terms, then `tls <domain>` for each domain.
   It runs a dry run first (no rate-limit risk), then the real certificate, then enables http→https and HSTS.
   Renewal is automatic (certbot timer).
5. Verify from your machine: `curl -sS -o /dev/null -w "%{http_code}\n" https://<domain>/` and the API
   health URL. Open the frontend in the browser tool if available.

## Phase 5 — CI/CD (push to deploy)

Ask first, then (with `gh` logged in; otherwise give the user the exact values to paste in GitHub UI):

```bash
ssh sdp-<name> sudo sdp ci-key   | gh secret set SDP_SSH_KEY     -R <owner>/<repo>
ssh sdp-<name> sudo sdp host-keys | gh secret set SDP_KNOWN_HOSTS -R <owner>/<repo>
gh secret set SDP_HOST -R <owner>/<repo> --body "<IP>"
gh secret set SDP_USER -R <owner>/<repo> --body "sdp-<name>"
```

Pipe `ci-key` straight into `gh` so the private key never appears in your context or the chat.
That key can only run `sdp deploy` (forced command) — it cannot open a shell.
Make sure `.github/workflows/deploy.yml` is on the deploy branch, then trigger it
(`gh workflow run Deploy -R <owner>/<repo>` or a small commit) and watch it:
`gh run watch -R <owner>/<repo>`. It must end green.

## Phase 6 — Report and hand-off

Report to the user (in their language), concisely:
- live URLs (https), which service each serves; CI status; last deployed commit
- what you changed in the repo (files)
- secrets: which were generated on the server, which the user entered, any still missing
- ports used, memory limits, warnings from `validate` that remain
- how to operate: push to `main` = deploy; `ssh sdp-<name> sudo sdp status|logs|ps`
- **Close the agent's access** now that setup is done (the CI key keeps working):
  `ssh root@<IP> sdp-admin revoke-agent <name>`; re-grant later with
  `sdp-admin grant-agent <name> --agent-key '...'`
- recommendations: database backups (`pg_dump` cron or provider snapshots), rotate the root password
  if it was ever shared, SSH keys instead of root passwords for humans too (only if the server owner agrees).

## sdp command reference (run as `sudo sdp <cmd>`; project is implied by your login)

| Command | Purpose |
|---|---|
| `preflight` | read-only server facts (OS, docker, nginx, certbot, ports 80/443, disk, RAM) |
| `status` | repo, checked-out/last-good commit, containers, domains, certificate expiry |
| `port-suggest [-n 3]` | free localhost ports (20000-29999) not used by anyone |
| `repo-init URL [--branch B]` / `sync` | set repo + print deploy key / fetch and check out (no deploy) |
| `validate [--sync]` | run the security policy and env checks |
| `deploy [--sha SHA] [--pull]` | full deploy with health checks and auto-rollback |
| `ps` / `logs [svc] [--tail N] [--since 10m]` | containers / masked logs |
| `restart [svc...]` / `down` | restart / stop (volumes kept; no data deletion command exists) |
| `exec SVC -- CMD` / `run SVC -- CMD` | one command in a container (output masked; may be disabled by admin) |
| `env-status` / `env-template` | list keys (no secret values) / add keys from `.env.example` |
| `env-gen KEY [--bytes 32] [--force]` | random secret, never displayed |
| `env-set KEY VALUE` / `env-unset KEY` | non-secret values, credential-free URLs, `${REF}` compositions |
| `config [show]` / `config set KEY VALUE` | `compose_file`, `branch`, `le_email`, `health_urls`, `wait_timeout` |
| `domains` / `domain-add D PORT [--path P] [--strip] [--max-body 50m] [--skip-dns]` / `domain-remove D [--path P]` | nginx routing |
| `tls DOMAIN` | Let's Encrypt certificate + https |
| `ci-key` / `host-keys` | deploy-only CI key (prints PRIVATE key — pipe it) / known_hosts lines |

Root-only (the human): `sdp-admin add-project | grant-agent | revoke-agent | secret | env-edit | exec on/off | list | remove-project`.
