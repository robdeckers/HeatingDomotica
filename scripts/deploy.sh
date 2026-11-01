#!/usr/bin/env bash
# Deploy the heating optimizer AppDaemon apps to Home Assistant OS over SSH.
#
# Usage: scripts/deploy.sh [--dry-run] [--config] [--no-restart]
#   --dry-run     show what would change, change nothing
#   --config      also overwrite heating_optimizer.yaml on the Pi (by default it is only copied when missing,
#                 so the config you filled in on the Pi is never overwritten)
#   --no-restart  do not restart the AppDaemon app afterwards
#
# Settings come from the environment or scripts/deploy.env (git-ignored, see deploy.env.example):
#   HA_HOST (default homeassistant.local), HA_PORT (default 22), HA_USER (required),
#   APPDAEMON_SLUG (default a0d7b954_appdaemon)
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(dirname "${script_dir}")"
apps_dir="${project_dir}/appdaemon/apps"

if [[ -f "${script_dir}/deploy.env" ]]; then
  # shellcheck source=/dev/null
  source "${script_dir}/deploy.env"
fi
HA_HOST="${HA_HOST:-homeassistant.local}"
HA_PORT="${HA_PORT:-22}"
APPDAEMON_SLUG="${APPDAEMON_SLUG:-a0d7b954_appdaemon}"
dry_run=false
overwrite_config=false
restart=true
for arg in "$@"; do
  case "${arg}" in
  --dry-run) dry_run=true ;;
  --config) overwrite_config=true ;;
  --no-restart) restart=false ;;
  -h | --help)
    sed -n '2,13p' "$0"
    exit 0
    ;;
  *)
    echo "Unknown option: ${arg}" >&2
    exit 1
    ;;
  esac
done

if [[ -z ${HA_USER:-} ]]; then
  echo "HA_USER is not set: put it in scripts/deploy.env (the username from the SSH app's config)." >&2
  exit 1
fi

remote_dir="/addon_configs/${APPDAEMON_SLUG}/apps"
ssh_cmd=(ssh -p "${HA_PORT}" -o BatchMode=yes)
target="${HA_USER}@${HA_HOST}"

echo "Deploying to ${target}:${remote_dir} (port ${HA_PORT})"
if ! "${ssh_cmd[@]}" "${target}" "test -d '${remote_dir}'"; then
  echo "${remote_dir} not found on the Pi. Is the AppDaemon app installed, and is APPDAEMON_SLUG right?" >&2
  exit 1
fi

excludes=(--exclude "__pycache__/" --exclude "*.pyc" --exclude ".pytest_cache/")
entry_files=(heating_optimizer_main.py heating_optimizer_demo.py)

if "${ssh_cmd[@]}" "${target}" "command -v rsync >/dev/null"; then
  rsync_opts=(-rtv --itemize-changes -e "ssh -p ${HA_PORT}")
  ${dry_run} && rsync_opts+=(-n)
  # The package is owned by this project: mirror it, deleting files that no longer exist.
  rsync "${rsync_opts[@]}" --delete "${excludes[@]}" "${apps_dir}/heating_optimizer/" "${target}:${remote_dir}/heating_optimizer/"
  rsync "${rsync_opts[@]}" "${entry_files[@]/#/${apps_dir}/}" "${target}:${remote_dir}/"
  config_opts=()
  ${overwrite_config} || config_opts+=(--ignore-existing)
  rsync "${rsync_opts[@]}" "${config_opts[@]}" "${apps_dir}/heating_optimizer.yaml" "${target}:${remote_dir}/"
else
  echo "rsync is not available in the SSH app: falling back to tar over ssh (no --delete, no preview)."
  if ${dry_run}; then
    echo "Would copy: heating_optimizer/ ${entry_files[*]} (and heating_optimizer.yaml if missing or --config)"
    exit 0
  fi
  files=(heating_optimizer "${entry_files[@]}")
  tar -C "${apps_dir}" "${excludes[@]}" -czf - "${files[@]}" | "${ssh_cmd[@]}" "${target}" "tar -C '${remote_dir}' -xzf -"
  if ${overwrite_config} || ! "${ssh_cmd[@]}" "${target}" "test -f '${remote_dir}/heating_optimizer.yaml'"; then
    "${ssh_cmd[@]}" "${target}" "cat > '${remote_dir}/heating_optimizer.yaml'" <"${apps_dir}/heating_optimizer.yaml"
  else
    echo "Kept the existing heating_optimizer.yaml on the Pi (use --config to overwrite)."
  fi
fi

if ${dry_run}; then
  echo "Dry run: nothing changed."
  exit 0
fi

if ${restart}; then
  echo "Restarting the AppDaemon app..."
  # Newer ha CLI versions name the command "apps"; older ones "addons".
  "${ssh_cmd[@]}" "${target}" "ha apps restart ${APPDAEMON_SLUG} 2>/dev/null || ha addons restart ${APPDAEMON_SLUG}" ||
    echo "Restart failed: restart AppDaemon from Settings → Apps." >&2
fi

echo
echo "Follow the app log with:"
echo "  ssh -p ${HA_PORT} ${target} 'tail -f /addon_configs/${APPDAEMON_SLUG}/logs/heating_optimizer/heating_optimizer.log'"
