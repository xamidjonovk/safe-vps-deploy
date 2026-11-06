#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
test_home=$(mktemp -d)
trap 'rm -rf -- "$test_home"' EXIT
SD_SOURCE="$root" SD_HOME="$test_home" bash "$root/install.sh" --codex
target=$test_home/.codex/skills/server-deploy
test -f "$target/SKILL.md"
printf 'preserve\n' > "$target/local-marker"
SD_SOURCE="$root" SD_HOME="$test_home" bash "$root/install.sh" --codex
find "$test_home/.codex/skill-backups" -name local-marker | grep -q .
printf 'Bash install and update passed\n'
