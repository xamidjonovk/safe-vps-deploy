#!/usr/bin/env bash
# Install only project access; never install packages or restart host services.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Root or sudo capability required' >&2; exit 1; }
project=${1:?project required}
stage=${2:?staging directory required}
public_key=${3:?public key required}
[[ $project =~ ^[a-z][a-z0-9-]{1,30}$ ]] || exit 2
[[ $stage == /tmp/server-deploy.* && -d $stage ]] || exit 2
for tool in python3 sudo visudo useradd usermod ssh-keygen git docker flock; do
  command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
done
python3 -c 'import yaml' || { echo 'Missing prerequisite: python3-yaml' >&2; exit 1; }
docker compose version >/dev/null
docker info >/dev/null
exec 9>/run/lock/server-deploy-bootstrap.lock
flock -n 9 || { echo 'Another bootstrap is running' >&2; exit 1; }
# An existing broker is shared infrastructure. Never silently replace its code.
for name in sdp sdp-admin sdp-ci-entry sdp_core.py; do
  destination=/usr/local/sbin/$name
  [[ $name != sdp_core.py ]] || destination=/usr/local/lib/sdp/$name
  if [[ -e $destination ]] && ! cmp -s "$stage/$name" "$destination"; then
    echo "Existing shared broker differs: $destination; review an upgrade separately" >&2
    exit 1
  fi
done
if [[ -d /etc/sdp/projects/$project ]]; then
  auth=/home/sdp-$project/.ssh/authorized_keys
  # The broker may use a nonstandard home; resolve it rather than assuming.
  auth=$(getent passwd "sdp-$project" | cut -d: -f6)/.ssh/authorized_keys
  grep -Fq -- "$public_key" "$auth" || { echo 'Project has a different agent key; explicit rotation required' >&2; exit 1; }
  echo 'Existing project key preserved'
  exit 0
fi
install -d -m 0755 /usr/local/lib/sdp
for name in sdp sdp-admin sdp-ci-entry; do
  [[ -e /usr/local/sbin/$name ]] || install -m 0755 "$stage/$name" /usr/local/sbin/$name
done
[[ -e /usr/local/lib/sdp/sdp_core.py ]] || install -m 0644 "$stage/sdp_core.py" /usr/local/lib/sdp/sdp_core.py
[[ -d /etc/sdp ]] || install -d -m 0700 /etc/sdp
[[ -d /etc/sdp/projects ]] || install -d -m 0700 /etc/sdp/projects
[[ -d /srv/apps ]] || install -d -m 0711 /srv/apps
/usr/local/sbin/sdp-admin add-project "$project" --agent-key "$public_key"
