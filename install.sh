#!/usr/bin/env bash
set -euo pipefail
agent=both
for arg in "$@"; do
  case "$arg" in
    --codex) agent=codex;; --claude) agent=claude;;
    --help) printf 'Usage: install.sh [--codex|--claude]\nOptional SD_SOURCE=local-checkout SD_HOME=isolated-test-home\n'; exit 0;;
    *) printf 'Unknown option: %s\n' "$arg" >&2; exit 1;;
  esac
done
base=${SD_HOME:-$HOME}
[[ $base = /* && $base != / ]] || { echo 'Home must be an absolute non-root path' >&2; exit 1; }
mkdir -p "$base"
base=$(cd "$base" && pwd -P)
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
if [[ -n ${SD_SOURCE:-} ]]; then
  source_root=$(cd "$SD_SOURCE" && pwd -P)
else
  curl -fSL --retry 2 --connect-timeout 15 --max-time 180 https://codeload.github.com/xamidjonovk/safe-vps-deploy/tar.gz/refs/heads/main -o "$tmp/source.tgz"
  tar -xzf "$tmp/source.tgz" -C "$tmp"
  source_root=$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -n 1)
fi
src=$source_root/plugins/server-deploy/skills/server-deploy
[[ -f $src/SKILL.md ]] || { echo 'Download is missing SKILL.md' >&2; exit 1; }
install_one() {
  local application=$1 parent target staged backup='' backup_root
  parent=$base/$application/skills
  target=$parent/server-deploy
  mkdir -p "$parent"
  # Do not follow a skills symlink into an unrelated directory during replacement.
  [[ ! -L $base/$application && ! -L $parent && ! -L $target ]] || { echo 'Refusing symlink destination' >&2; exit 1; }
  staged=$tmp/stage-$application
  cp -R "$src" "$staged"
  if [[ -e $target ]]; then
    [[ -f $target/SKILL.md ]] && grep -Eq '^name: server-deploy[[:space:]]*$' "$target/SKILL.md" || { echo 'Refusing unrelated destination' >&2; exit 1; }
    backup_root=$base/$application/skill-backups
    mkdir -p "$backup_root"
    backup=$(mktemp -d "$backup_root/server-deploy.XXXXXXXX")
    rmdir "$backup"
    mv "$target" "$backup"
  fi
  if ! mv "$staged" "$target"; then
    [[ -z $backup ]] || mv "$backup" "$target"
    return 1
  fi
  chmod +x "$target"/server/{bootstrap-tools.sh,install.sh,sdp,sdp-admin,sdp-ci-entry}
  printf 'Installed: %s\n' "$target"
  [[ -z $backup ]] || printf 'Backup: %s\n' "$backup"
}
[[ $agent = claude ]] || install_one .codex
[[ $agent = codex ]] || install_one .claude
printf 'Ready. Open a new chat and invoke $server-deploy. Requires Python 3.11+, uv and OpenSSH.\n'
