# Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| `Permission denied (publickey)` as `sdp-<name>` | the human has not run `sdp-admin add-project`, or the access was revoked (`sdp-admin grant-agent <name> --agent-key '...'`), or wrong `IdentityFile` in `~/.ssh/config` |
| `sudo: a password is required` | you ran something other than `sudo sdp ...` — only sdp is allowed |
| `install.sh: ports 80/443 are used by 'docker-proxy'/'caddy'/'apache2'` | another reverse proxy owns the web ports; sdp cannot coexist without the owner reconfiguring the server. Stop and tell the user |
| `git clone failed` | deploy key not added to the repo, or repo URL wrong (use `git@github.com:owner/repo.git`) |
| `required variable X is missing a value` | `${X:?..}` in compose and X empty on the server → `sdp env-status`, then env-gen/env-set or ask the human |
| deploy: `migrate: exited with code 1` | migration error → `sdp logs migrate`; often DATABASE_URL wrong (check `env-status` shows `${POSTGRES_PASSWORD}` reference) or db not healthy yet |
| deploy: service `starting`/`unhealthy` until timeout | healthcheck command missing in the image (`curl` not installed → use python/node check like the template), app not listening on `0.0.0.0`, wrong port, or slow start → raise `start_period` or `sdp config set wait_timeout 600` |
| deploy: `http://127.0.0.1:PORT/health -> 404` | `health_urls` points to a path the app does not have → add `/health` or change `config set health_urls` |
| backend works locally but 502 Bad Gateway via domain | domain routed to the wrong port (`sdp domains`), or container not running (`sdp ps`) |
| frontend calls `localhost:8000` in the browser | `NEXT_PUBLIC_API_URL` was empty at **build** time → set it with env-set and redeploy (build args) |
| CORS errors | backend CORS origins must include `https://<frontend-domain>` → env-set and redeploy |
| FastAPI generates `http://` links / redirects | uvicorn needs `--proxy-headers --forwarded-allow-ips "*"` |
| `domain-add`: does not resolve / resolves elsewhere | create/fix the DNS A record to the server IP; wait for propagation (`dig +short <domain>`); Cloudflare proxy → `--skip-dns` |
| `tls`: dry run failed | DNS not pointing here yet, port 80 blocked (ufw/provider firewall), or the domain is behind Cloudflare proxy with "Always use HTTPS" |
| `tls`: too many certificates | Let's Encrypt rate limit — wait (sdp always dry-runs first to avoid this) |
| `nginx configuration on this server is already broken` | another site's config is broken; sdp refuses to reload. The server owner must fix it (`nginx -t`) |
| upload fails with 413 | `sdp domain-add <domain> <port> --max-body 100m` (re-run with the same port/path) |
| Disk full | old images: deploy prunes this project's dangling images; build cache is global — ask the owner to run `docker builder prune` |
| GitHub Action: `Host key verification failed` | re-run `ssh sdp-<name> sudo sdp host-keys \| gh secret set SDP_KNOWN_HOSTS -R owner/repo` |
| GitHub Action: `Permission denied (publickey)` | CI key rotated/removed → `ssh sdp-<name> sudo sdp ci-key \| gh secret set SDP_SSH_KEY -R owner/repo` (needs agent access; otherwise the human as root: `sdp -p <name> ci-key --show` and pastes it into GitHub secrets) |
| GitHub Action: `commit ... is not part of origin/main` | the workflow ran for a branch other than the configured one; deploy only from the configured branch |
| want to roll back by hand | `sdp deploy --sha <older full commit sha from main>` (DB migrations are not reverted) |
