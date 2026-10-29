#!/usr/bin/env bash
# safe-vps-deploy — one-line installer for macOS / Linux (also Git Bash / WSL on Windows)
#
#   curl -fsSL https://raw.githubusercontent.com/xamidjonovk/safe-vps-deploy/main/install.sh | bash
#
# Installs the skill for:
#   Claude Code  ->  ~/.claude/skills/safe-vps-deploy
#   Codex        ->  ~/.agents/skills/safe-vps-deploy
# Run the same line again to update.  Options (after `bash -s --`):
#   --claude     only Claude Code        --codex      only Codex
#   --uninstall  remove the skill
set -euo pipefail

REPO="${SVD_REPO:-xamidjonovk/safe-vps-deploy}"
REF="${SVD_REF:-main}"
NAME="safe-vps-deploy"
CLAUDE_DIR="${HOME}/.claude/skills/${NAME}"
CODEX_DIR="${HOME}/.agents/skills/${NAME}"

if [ -t 1 ]; then G=$'\033[1;32m'; Y=$'\033[1;33m'; R=$'\033[1;31m'; B=$'\033[1m'; N=$'\033[0m'; else G=; Y=; R=; B=; N=; fi
ok()   { printf '%s✔%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$*"; }
fail() { printf '%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

want_claude=1; want_codex=1; uninstall=0
for arg in "$@"; do
  case "$arg" in
    --claude) want_codex=0 ;;
    --codex) want_claude=0 ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '2,13p' "$0" 2>/dev/null || true; exit 0 ;;
    *) fail "unknown option: $arg" ;;
  esac
done

targets=()
[ "$want_claude" = 1 ] && targets+=("$CLAUDE_DIR")
[ "$want_codex" = 1 ] && targets+=("$CODEX_DIR")

if [ "$uninstall" = 1 ]; then
  for t in "${targets[@]}"; do
    if [ -f "$t/SKILL.md" ]; then rm -rf "$t"; ok "removed $t"; fi
  done
  exit 0
fi

tmp="$(mktemp -d 2>/dev/null || mktemp -d -t svd)"
trap 'rm -rf "$tmp"' EXIT

# Source: a local clone (when this file is run from the repo) or a fresh download from GitHub.
here="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd)" || here="$(pwd)"
src="$here/plugins/$NAME/skills/$NAME"
if [ ! -f "$src/SKILL.md" ]; then
  url="${SVD_TARBALL_URL:-https://codeload.github.com/${REPO}/tar.gz/refs/heads/${REF}}"
  printf 'Downloading %s%s%s (%s) ...\n' "$B" "$REPO" "$N" "$REF"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$tmp/src.tgz" || fail "download failed: $url"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp/src.tgz" "$url" || fail "download failed: $url"
  else
    fail "curl or wget is required"
  fi
  tar -xzf "$tmp/src.tgz" -C "$tmp" || fail "could not unpack the archive"
  src="$(find "$tmp" -type f -path "*/plugins/$NAME/skills/$NAME/SKILL.md" | head -n 1)"
  [ -n "$src" ] || fail "SKILL.md not found in the downloaded archive"
  src="$(dirname "$src")"
fi

version="$(cat "$src/../../../../VERSION" 2>/dev/null || echo "?")"
for t in "${targets[@]}"; do
  mkdir -p "$(dirname "$t")"
  if [ -e "$t" ] && [ ! -f "$t/SKILL.md" ]; then
    fail "$t exists but is not this skill — move it away and re-run"
  fi
  rm -rf "$t"
  cp -R "$src" "$t"
  chmod +x "$t/server/install.sh" "$t/server/sdp" "$t/server/sdp-admin" "$t/server/sdp-ci-entry" 2>/dev/null || true
  ok "installed v$version -> $t"
done

echo
printf '%sReady.%s Open your project and ask the agent, e.g.:\n' "$B" "$N"
echo '   "Loyihamni VPS ga deploy qil. IP: 1.2.3.4, domen: app.misol.uz"'
echo
command -v claude >/dev/null 2>&1 || warn "Claude Code not found in PATH (that's fine if you use Codex)."
echo "Claude Code: start a NEW session (or run /reload-plugins) so it picks up the skill."
echo "Update later: run the same install command again."
