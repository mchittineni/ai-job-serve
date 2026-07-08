#!/usr/bin/env bash
# Railway entrypoint: sync the Obsidian vault (a private git repo) into the
# persistent volume, run the agent grounded in it, then push the application
# notes the run wrote back to the vault repo.
#
# Env:
#   DATA_DIR        volume mount path (Railway: /data). State + vault live here.
#   VAULT_REPO_URL  https://<token>@github.com/<you>/<vault-repo>.git
#                   Unset = run vault-less (master resume only), still fine.
#
# Local/dev runs don't need this script — call `python job-connect.py` directly
# with OBSIDIAN_VAULT_PATH pointing at your real vault.
set -uo pipefail

DATA_DIR="${DATA_DIR:-/data}"

if [ -n "${VAULT_REPO_URL:-}" ]; then
  export OBSIDIAN_VAULT_PATH="$DATA_DIR/vault"
  if [ -d "$OBSIDIAN_VAULT_PATH/.git" ]; then
    git -C "$OBSIDIAN_VAULT_PATH" pull --rebase --quiet ||
      echo "[vault] pull failed — running with the cached copy on the volume"
  else
    git clone --depth 1 --quiet "$VAULT_REPO_URL" "$OBSIDIAN_VAULT_PATH" || {
      echo "[vault] clone failed — running vault-less this run"
      unset OBSIDIAN_VAULT_PATH
    }
  fi
fi

python job-connect.py
status=$?

# Push application notes back even if the run partially failed — whatever notes
# were written should reach the vault rather than sit invisible on the volume.
if [ -n "${VAULT_REPO_URL:-}" ] && [ -d "${OBSIDIAN_VAULT_PATH:-}/.git" ]; then
  cd "$OBSIDIAN_VAULT_PATH"
  git add -A
  if ! git diff --cached --quiet; then
    git -c user.name="job-agent" -c user.email="job-agent@railway" \
      commit --quiet -m "job agent: application notes $(date -u +'%Y-%m-%d %H:%M') UTC"
    git push --quiet ||
      echo "[vault] push failed — notes stay on the volume and retry next run"
  fi
fi

exit $status
