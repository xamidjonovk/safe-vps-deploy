#!/usr/bin/env bash
# install.sh — installs sdp (Safe Deploy Proxy) on an Ubuntu/Debian VPS.
#
# Run ONCE as root (a human runs this, never the AI):
#     bash install.sh          (asks before changing anything)
#     bash install.sh -y       (non-interactive)
#
# Safe on servers that already host other projects:
#   * does NOT touch SSH settings, the firewall, existing nginx sites, or running containers
#   * refuses to continue if ports 80/443 belong to something other than nginx
#   * is idempotent: re-running it just upgrades the sdp tools
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YES=0
[[ "${1:-}" == "-y" ]] && YES=1

say()  { printf '\033[1;34m[sdp-install]\033[0m %s\n' "$*"; }
okay() { printf '\033[1;32m[sdp-install] OK\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[sdp-install] WARN\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[sdp-install] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
confirm() {
  [[ $YES -eq 1 ]] && return 0
  read -r -p "$1 [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

[[ $EUID -eq 0 ]] || fail "run as root (ssh root@<server> then: bash install.sh)"
[[ -r /etc/os-release ]] || fail "cannot detect the OS"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ;;
  *) fail "only Ubuntu and Debian are supported (found: ${PRETTY_NAME:-unknown})" ;;
esac
for f in sdp sdp-admin sdp-ci-entry sdp_core.py; do
  [[ -f "$HERE/$f" ]] || fail "missing $f next to install.sh — copy the whole 'server' folder"
done

# ---------------------------------------------------------------- 1. who owns ports 80 / 443?
port_owners() {
  ss -H -ltnp 'sport = :80 or sport = :443' 2>/dev/null \
    | grep -o 'users:(("[^"]*"' | sed -e 's/^users:(("//' -e 's/"$//' | sort -u | tr '\n' ' ' || true
}
OWNERS="$(port_owners)"
for o in $OWNERS; do
  if [[ "$o" != "nginx" ]]; then
    fail "ports 80/443 are used by '$o', not by a host nginx.
       sdp routes domains through the host nginx, so it cannot run next to $o without
       reconfiguring the server. Ask the server owner, or use a separate VPS."
  fi
done

# ---------------------------------------------------------------- 2. snap docker check
if command -v docker >/dev/null 2>&1; then
  if [[ "$(readlink -f "$(command -v docker)")" == /snap/* ]]; then
    fail "Docker is installed from snap. Snap Docker cannot access /srv/apps (strict confinement).
       Install Docker Engine from apt instead (https://docs.docker.com/engine/install/)."
  fi
fi

say "This will:"
say "  - apt install: git ca-certificates curl gnupg python3 python3-yaml sudo openssl iproute2 certbot"
command -v nginx  >/dev/null 2>&1 || say "  - install nginx (not present yet; ports 80/443 are free)"
command -v docker >/dev/null 2>&1 || say "  - install Docker Engine + compose plugin from Docker's official apt repository"
say "  - install /usr/local/sbin/{sdp,sdp-admin,sdp-ci-entry} and /usr/local/lib/sdp/"
say "  - create /etc/sdp (0700), /srv/apps (0711), /var/www/sdp-acme"
say "  - add ONE nginx helper file: /etc/nginx/conf.d/sdp-00-common.conf"
say "It will NOT change SSH, firewall rules, other nginx sites or running containers."
confirm "Continue?" || fail "aborted by user"

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------- 3. packages
say "installing base packages ..."
apt-get update -qq
apt-get install -y -qq git ca-certificates curl gnupg python3 python3-yaml sudo openssl iproute2 certbot >/dev/null
python3 - <<'PY' || fail "python3 >= 3.8 is required"
import sys
assert sys.version_info >= (3, 8)
PY
okay "base packages"

if ! command -v nginx >/dev/null 2>&1; then
  [[ -z "$(port_owners)" ]] || fail "ports 80/443 became busy; aborting"
  say "installing nginx ..."
  apt-get install -y -qq nginx >/dev/null
fi
systemctl enable --now nginx >/dev/null 2>&1 || true
nginx -t >/dev/null 2>&1 || fail "the existing nginx configuration is broken (run: nginx -t). Fix it first — sdp never edits other sites."
okay "nginx $(nginx -v 2>&1 | sed 's|.*/||')"

if ! command -v docker >/dev/null 2>&1; then
  say "installing Docker Engine from download.docker.com ..."
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  CODENAME="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
  [[ -n "$CODENAME" ]] || fail "cannot detect the distribution codename"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  systemctl enable --now docker >/dev/null
fi
if ! docker compose version >/dev/null 2>&1; then
  say "installing the docker compose plugin ..."
  apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 \
    || apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 \
    || fail "could not install 'docker compose' (v2). Install it manually, then re-run."
fi
docker info >/dev/null 2>&1 || fail "the Docker daemon is not running (systemctl status docker)"
okay "docker $(docker version --format '{{.Server.Version}}'), compose $(docker compose version --short)"

# ---------------------------------------------------------------- 4. sdp files
say "installing sdp tools ..."
install -d -m 0755 -o root -g root /usr/local/lib/sdp
rm -rf /usr/local/lib/sdp/__pycache__
install -m 0644 -o root -g root "$HERE/sdp_core.py"    /usr/local/lib/sdp/sdp_core.py
install -m 0755 -o root -g root "$HERE/sdp"            /usr/local/sbin/sdp
install -m 0755 -o root -g root "$HERE/sdp-admin"      /usr/local/sbin/sdp-admin
install -m 0755 -o root -g root "$HERE/sdp-ci-entry"   /usr/local/sbin/sdp-ci-entry
install -d -m 0700 -o root -g root /etc/sdp /etc/sdp/projects
install -d -m 0711 -o root -g root /srv/apps
install -d -m 0755 -o root -g root /var/www/sdp-acme/.well-known/acme-challenge
/usr/local/sbin/sdp --version >/dev/null || fail "sdp does not start"
okay "sdp $(/usr/local/sbin/sdp --version | awk '{print $2}')"

# ---------------------------------------------------------------- 5. nginx helper
COMMON=/etc/nginx/conf.d/sdp-00-common.conf
if [[ ! -f "$COMMON" ]]; then
  cat > "$COMMON" <<'NGX'
# Managed by sdp — shared helpers for sdp sites
map $http_upgrade $sdp_connection_upgrade {
    default upgrade;
    ''      close;
}
NGX
  chmod 0644 "$COMMON"
  if ! nginx -t >/dev/null 2>&1; then
    rm -f "$COMMON"
    fail "nginx rejected the sdp helper file; removed it again. Run 'nginx -t' to see why."
  fi
  systemctl reload nginx
fi
okay "nginx helper"

# ---------------------------------------------------------------- 6. certbot renewal + firewall info
if systemctl list-unit-files 2>/dev/null | grep -q '^certbot.timer'; then
  systemctl enable --now certbot.timer >/dev/null 2>&1 || true
  okay "certificate auto-renewal: certbot.timer enabled"
else
  warn "certbot.timer not found — make sure 'certbot renew' runs daily (cron) for HTTPS auto-renewal"
fi

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  if ! ufw status | grep -Eq '^(80|443|80,443)(/tcp)?[[:space:]]+ALLOW|Nginx (Full|HTTP)'; then
    warn "ufw is active and ports 80/443 are not open — Let's Encrypt and visitors need them."
    if confirm "Run 'ufw allow 80,443/tcp' now?"; then
      ufw allow 80,443/tcp >/dev/null && okay "ufw: 80,443/tcp allowed"
    fi
  fi
fi

cat <<EOF

$(printf '\033[1;32m')sdp is installed.$(printf '\033[0m')

Next step (still as root) — create an isolated project for the AI agent:

    sdp-admin add-project <name> --agent-key '<the agent public key: ssh-ed25519 AAAA...>'

The agent then logs in as 'sdp-<name>' and can ONLY run 'sudo sdp ...' for that project.
Other useful commands:  sdp-admin list | sdp-admin secret <name> KEY | sdp-admin revoke-agent <name>
EOF
