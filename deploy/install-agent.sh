#!/bin/sh
# Install or update forge-metrics-agent on one host, over SSH.
#
#   deploy/install-agent.sh <ssh-target> <ingest-url> [ssh options...]
#
# The bearer token is read from STDIN (never argv), written to
# /var/lib/forge-metrics/token (0600, root) on the host, and never echoed.
# The agent runs as a docker container: python:3.12-alpine, --network host so
# /proc/net/dev shows the host's interfaces, read-only root, no capabilities.
# Idempotent: re-running replaces the script and restarts the container.
set -eu

target=${1:?ssh target, e.g. root@192.168.1.173}
url=${2:?ingest URL, e.g. https://forge-ci-com.onrender.com/api/ingest}
shift 2

here=$(cd "$(dirname "$0")/.." && pwd)
agent="$here/agent/forge_metrics_agent.py"
image="python:3.12-alpine"

token=$(cat)
[ -n "$token" ] || { echo "empty token on stdin" >&2; exit 2; }

# shellcheck disable=SC2029  # $url and $image are expanded locally on purpose
{
  printf '%s\n' "$token"
  cat "$agent"
} | ssh "$@" "$target" "
  set -eu
  install -d -m 0700 /var/lib/forge-metrics
  IFS= read -r token
  umask 077
  printf '%s\n' \"\$token\" > /var/lib/forge-metrics/token
  cat > /var/lib/forge-metrics/agent.py
  chmod 0644 /var/lib/forge-metrics/agent.py
  docker pull -q $image >/dev/null
  docker rm -f forge-metrics-agent >/dev/null 2>&1 || true
  # Rotation options exist only for json-file; journald (NixOS hosts) rotates itself.
  logopts=''
  if [ \"\$(docker info --format '{{.LoggingDriver}}')\" = json-file ]; then
    logopts='--log-opt max-size=1m --log-opt max-file=2'
  fi
  runner_mount=''
  if [ -d /run/forge ]; then
    runner_mount='--mount type=bind,src=/run/forge,dst=/run/forge,readonly'
  fi
  # shellcheck disable=SC2086
  docker run -d --name forge-metrics-agent \
    --restart unless-stopped \
    --network host \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --memory 64m --cpus 0.2 --pids-limit 32 \
    \$logopts \$runner_mount \
    -v /var/lib/forge-metrics:/etc/forge-metrics:ro \
    -e FORGE_INGEST_URL=$url \
    -e FORGE_INGEST_TOKEN_FILE=/etc/forge-metrics/token \
    $image python /etc/forge-metrics/agent.py >/dev/null
  sleep 8
  docker ps --filter name=forge-metrics-agent --format '{{.Names}} {{.Status}}'
  docker logs --tail 5 forge-metrics-agent 2>&1
"
