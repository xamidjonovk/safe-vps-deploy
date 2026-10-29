#!/usr/bin/env bash
# publish.sh — for the TEACHER, run once: creates the public GitHub repo and fills in your username
# so that students' one-line install commands work.
#
#   bash publish.sh                 # repo name: safe-vps-deploy
#   bash publish.sh my-repo-name    # custom name
#
# Needs: git, GitHub CLI (gh) logged in:  gh auth login
set -euo pipefail
cd "$(dirname "$0")"

REPO_NAME="${1:-safe-vps-deploy}"
command -v git >/dev/null || { echo "git is required"; exit 1; }
command -v gh  >/dev/null || { echo "GitHub CLI is required: https://cli.github.com  then: gh auth login"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Run: gh auth login"; exit 1; }

OWNER="$(gh api user -q .login)"
echo "Publishing as github.com/$OWNER/$REPO_NAME"

# Replace the OWNER/safe-vps-deploy placeholder in installers and docs.
grep -rl --exclude-dir=.git --exclude=publish.sh 'OWNER/safe-vps-deploy' . | while IFS= read -r f; do
  sed -i.bak "s#OWNER/safe-vps-deploy#$OWNER/$REPO_NAME#g" "$f" && rm -f "$f.bak"
done

if [ ! -d .git ]; then
  git init -q -b main
fi
git add -A
git -c user.name="${GIT_AUTHOR_NAME:-$OWNER}" -c user.email="${GIT_AUTHOR_EMAIL:-$OWNER@users.noreply.github.com}" \
  commit -q -m "safe-vps-deploy skill $(cat VERSION)" || true

if gh repo view "$OWNER/$REPO_NAME" >/dev/null 2>&1; then
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$OWNER/$REPO_NAME.git"
  git push -u origin main
else
  gh repo create "$OWNER/$REPO_NAME" --public --source . --push \
    --description "AI agent skill: secure VPS deploy (Docker, nginx, HTTPS, CI/CD) without root password"
fi

cat <<MSG

Done: https://github.com/$OWNER/$REPO_NAME

Send this to students:
  macOS / Linux:  curl -fsSL https://raw.githubusercontent.com/$OWNER/$REPO_NAME/main/install.sh | bash
  Windows:        irm https://raw.githubusercontent.com/$OWNER/$REPO_NAME/main/install.ps1 | iex
  Claude Code:    /plugin marketplace add $OWNER/$REPO_NAME
                  /plugin install safe-vps-deploy@komiljon-skills

To release an update later: edit files, bump VERSION and plugins/safe-vps-deploy/.claude-plugin/plugin.json "version",
then: git commit -am "..." && git push
MSG
