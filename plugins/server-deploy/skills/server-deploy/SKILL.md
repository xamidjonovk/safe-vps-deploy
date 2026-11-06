---
name: server-deploy
description: Deploy a Git-backed Docker Compose project to a shared VPS. Bootstrap project-scoped SSH access from local SERVER and PAROL variables, inspect existing services, and deploy through an isolated broker with health checks and rollback.
---

# Server deploy

Handle the user's authorized deployment end to end. Use the user's language. Preserve other server projects, data, SSH settings and firewall rules. Keep the existing sdp broker command names: renaming the skill does not require renaming installed server tools.

## Credentials and access

When the user authorizes using `.env`, use `scripts/bootstrap_ssh.py`; it reads `SERVER` and `PAROL` internally. Never dump `.env`, put a password in command arguments, print secrets or copy the local environment to the VPS. `SERVER` accepts `host`, `user@host` or `ssh://user@host:port`; optional `SSH_USER`, `SSH_PORT`, `SSH_HOST_FINGERPRINT` refine it. A bare host defaults to root for this one-time bootstrap only. Existing application settings are unrelated to SSH and must not be uploaded.

The helper creates an Ed25519 key with `ssh-keygen` for `sdp-<project>`. Prefer known host keys or a provider-supplied SHA256 fingerprint. Unknown hosts fail closed by default; `--trust-new-host` is explicit first-use trust, not independent identity verification. Explain that limitation before using it. A changed saved host key always stops. Do not delete known-host entries to bypass a mismatch.

```powershell
uv run <skill-dir>/scripts/bootstrap_ssh.py --env-file .env --project <slug>
# After reviewing the read-only inspection, install isolated access:
uv run <skill-dir>/scripts/bootstrap_ssh.py --env-file .env --project <slug> --apply
```

First invocation inspects only; `--apply` is appropriate when SSH setup is already authorized. If root or sudo privileges are unavailable, stop with the exact missing capability. Non-root bootstrap needs noninteractive sudo or the same password for sudo. No password is saved by the helper. Following bootstrap use only the printed project-scoped key/known-hosts paths and `sudo sdp ...`; do not continue routine deployment as root.

`server/bootstrap-tools.sh` installs the broker only when prerequisites already exist. It never installs packages, restarts global services, changes firewall rules or SSH configuration, or overwrites a differing broker shared with another project. Missing dependencies or an incompatible installed broker require a separate concrete remediation plan. Existing deploy users/keys are preserved; conflicting keys stop instead of silently rotating access.

## Inspect before deploying

Determine repo URL, branch, git status and application requirements locally. Keep unrelated user changes out of commits. User authorization to deploy via git covers the necessary project commit/push; ask only if repository, branch, production target or destructive effects are ambiguous. Never push secrets. Inspect the actual server before choosing directories and ports:

- `sudo sdp preflight`, `status`, `port-suggest` and `ps`.
- Record existing container names/states and listening ports before and after deployment; unexpected changes outside this project stop the workflow.
- Check available RAM/disk, existing reverse proxy, unhealthy services and port conflicts. Never use global prune, compose without its project scope, external volumes, or another project's directories.

## Prepare production configuration

Read [references/policy.md](references/policy.md) and [templates/docker-compose.prod.yml](templates/docker-compose.prod.yml) before editing production Compose. Production Compose is a separate root file, so local development continues working.

- Unique Compose project `sdp-<project>`; named private volumes/networks; no fixed container names, host mode, docker.sock or privileged services.
- Reserve a free localhost port in 20000–29999 using `port-suggest`; do not infer availability from the local machine.
- Add memory limits and bounded log rotation, health checks and appropriate restart policies. Keep databases private.
- One migration service, followed by API with `service_completed_successfully`. Override a Dockerfile's startup migration in this production API command to avoid duplicate/concurrent migrations.
- Health probes must use dependencies installed in the image (Python urllib for this FastAPI project). Separate liveness from database readiness.
- Build from the committed lockfile; exclude environment files and SSH material. Do not pass server bootstrap variables to application containers. Generate application secrets on the server via `env-gen`; configure nonsecret values with `env-set`.
- Preserve all historical migration IDs. Back up before schema changes; prefer backward-compatible migrations. Application rollback does not undo a database migration or restore data.

Read [references/deploy-flow.md](references/deploy-flow.md) for the Git → server → verify sequence. Use existing broker validation, health checks and rollback rather than replacing them with an unrestricted root shell.

## Completion and failures

A completed deploy means the exact committed SHA is running, migrations exited successfully, every required service is ready, intended HTTP endpoints respond and existing projects remain healthy. Test the public URL only if routing/domain setup was requested and available. Never call a localhost-only service publicly deployed. Domain ownership, DNS and Let's Encrypt account/terms require the user's actual choices; do not invent them.

One retry is appropriate for a transient network failure; deterministic configuration errors require a fix, validation and a fresh commit. Never loop password guesses. Stop on host-key changes, policy rejection, unexpected project changes, insufficient resources or migration failure. Report what is live, the SHA, URLs, health evidence, remaining limitations and the precise command needed to recover. Revocation of agent access should be coordinated with completion, not performed before verification.

No workflow can guarantee zero downtime or zero errors. Do not claim success until observed checks pass.
