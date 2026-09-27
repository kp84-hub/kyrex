#!/usr/bin/env bash
# Run from the repository root inside a Railway Sandbox with Docker installed.
set -euo pipefail

cd "$(dirname "$0")/.."
profiles="$PWD/browser-host/profiles"
env_file="$PWD/browser-host/enrollment.env"
viewer_pass="$PWD/browser-host/viewer-vnc-pass"
cloud=(docker compose --env-file "$env_file" -f browser-host/docker-compose.cloud.yml \
  -f browser-host/docker-compose.sandbox.yml --profile cloud)

usage() {
  echo 'Usage: browser-host/railway_sandbox.sh {init|password|viewer-start OWNER BOT|viewer-stop|agent-start|agent-stop|checkpoint-ready|status}' >&2
  exit 2
}

require_env() {
  if [[ ! -f "$env_file" ]] || [[ $(stat -c '%a' "$env_file") != 600 ]]; then
    echo 'Create browser-host/enrollment.env as a mode-0600 file first.' >&2
    exit 1
  fi
}

case "${1:-}" in
  init)
    docker compose version >/dev/null
    install -d -m 700 -o 1000 -g 1000 "$profiles"
    install -d -m 700 -o 1000 -g 1000 "$profiles/.kyrex-agent-home"
    docker build -f browser-host/Dockerfile -t kyrex-browser-host:phase1 .
    docker build -f browser-host/Dockerfile.viewer -t kyrex-browser-host:viewer .
    ;;
  password)
    [[ -t 0 ]] || { echo 'Set the viewer password in an interactive SSH shell.' >&2; exit 1; }
    read -rsp 'Viewer password: ' password
    echo
    [[ ${#password} -ge 8 ]] || { echo 'Use at least eight characters.' >&2; exit 1; }
    umask 077
    printf '%s\n' "$password" > "$viewer_pass"
    unset password
    chown 1000:1000 "$viewer_pass"
    chmod 600 "$viewer_pass"
    ;;
  viewer-start)
    [[ $# == 3 ]] || usage
    [[ -f "$viewer_pass" && $(stat -c '%a' "$viewer_pass") == 600 ]] || {
      echo 'Run the password command first.' >&2; exit 1;
    }
    "${cloud[@]}" stop agent 2>/dev/null || true
    KYREX_VIEWER_ACCESS_LABEL=SSH-forward-only \
    KYREX_VIEWER_VNC_PASSWORD_FILE="$viewer_pass" \
      python3 browser-host/viewer_ctl.py start --owner "$2" --bot "$3" --ttl 2700
    ;;
  viewer-stop)
    python3 browser-host/viewer_ctl.py end
    ;;
  agent-start)
    require_env
    "${cloud[@]}" up -d --no-build agent
    ;;
  agent-stop)
    require_env
    "${cloud[@]}" stop agent
    ;;
  checkpoint-ready)
    require_env
    "${cloud[@]}" stop agent
    python3 browser-host/viewer_ctl.py end
    echo 'Agent and viewer stopped. Capture the sandbox disk checkpoint now.'
    ;;
  status)
    docker ps --filter label=com.docker.compose.service=agent \
      --format 'agent: {{.Status}}'
    docker ps --filter label=com.docker.compose.service=viewer \
      --format 'viewer: {{.Status}}'
    python3 browser-host/viewer_ctl.py status
    ;;
  *) usage ;;
esac
