#!/bin/sh
# Install or update forge-metrics-agent on one host, over SSH.
#
#   deploy/install-agent.sh [--force] <ssh-target> <ingest-url> [ssh options...] </dev/null
#   printf '%s' "$token" | deploy/install-agent.sh [--force] <ssh-target> <ingest-url> ...
#
# Hosts self-identify: the agent sends the host's own /etc/hostname (mounted
# read-only) as X-Forge-Host, and the backend rejects a token that does not
# belong to that host (403), so a token copied to the wrong machine can never
# post as its owner.
#
# Hosts whose hostname is not their dashboard id (e.g. the admin droplet) set
# FORGE_HOST_ID=<id> in the operator's environment to override it explicitly.
# Token handling, on the target:
#   - no token on stdin, none on the host: a fresh one is generated ON the host;
#     only its host id and sha256 are printed, for FORGE_INGEST_TOKEN_SHA256.
#   - no token on stdin, one on the host: the existing token is kept.
#   - token on stdin: written to /var/lib/forge-metrics/token (0600, root), but
#     an existing DIFFERENT token is never overwritten without --force.
# The token itself is never echoed or passed in argv.
# The agent runs as a docker container: python:3.12-alpine, --network host so
# /proc/net/dev shows the host's interfaces, read-only root, no capabilities.
# --pid host lets it see soldr paired-worker attestation processes in /proc
# (read-only: no capabilities, so it cannot signal or trace them).
# Static hardware (CPU, RAM, disks, btrfs profiles) comes from /proc and the
# container's default read-only /sys; /dev/disk/by-label is bind-mounted
# read-only for disk labels.
# Idempotent: re-running replaces the script and restarts the container.
set -eu

force=0
if [ "${1:-}" = --force ]; then
  force=1
  shift
fi
target=${1:?ssh target, e.g. root@192.168.1.173}
url=${2:?ingest URL, e.g. https://forge-ci-com.onrender.com/api/ingest}
shift 2

here=$(cd "$(dirname "$0")/.." && pwd)
agent="$here/agent/forge_metrics_agent.py"
image="python:3.12-alpine"

host_override=${FORGE_HOST_ID:-}
case $host_override in
  *[!a-z0-9-]*) echo "FORGE_HOST_ID must be lowercase letters, digits and dashes" >&2; exit 2 ;;
esac

token=''
if [ ! -t 0 ]; then
  token=$(cat)
fi
case $token in
  *[!A-Za-z0-9._~+/=-]*) echo "token on stdin has unexpected characters" >&2; exit 2 ;;
esac

# shellcheck disable=SC2029  # $url and $image are expanded locally on purpose
{
  printf '%s\n' "$token"
  cat "$agent"
} | ssh "$@" "$target" "
  set -eu
  install -d -m 0700 /var/lib/forge-metrics
  IFS= read -r token || token=''
  umask 077
  tf=/var/lib/forge-metrics/token
  host_id='$host_override'
  [ -n \"\$host_id\" ] || host_id=\$(tr -d '[:space:]' < /etc/hostname)
  [ -n \"\$host_id\" ] || { echo 'target has an empty /etc/hostname' >&2; exit 3; }
  existing=''
  [ -s \"\$tf\" ] && existing=\$(tr -d '[:space:]' < \"\$tf\")
  if [ -z \"\$token\" ]; then
    if [ -z \"\$existing\" ]; then
      od -An -tx1 -N32 /dev/urandom | tr -d ' \\n' > \"\$tf\"
      echo \"generated a fresh token on \$host_id\"
    fi
  elif [ -n \"\$existing\" ] && [ \"\$existing\" != \"\$token\" ] && [ $force != 1 ]; then
    echo \"refusing: \$host_id already has a different token (re-run with --force to replace it)\" >&2
    exit 4
  else
    printf '%s' \"\$token\" > \"\$tf\"
  fi
  chmod 0600 \"\$tf\"
  echo \"host=\$host_id token_sha256=\$(tr -d '[:space:]' < \"\$tf\" | sha256sum | cut -d' ' -f1)\"
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
  # Filesystem labels (e.g. forge-build) for the static hardware summary; the
  # symlinks only name devices, so this exposes no device nodes.
  label_mount=''
  if [ -d /dev/disk/by-label ]; then
    label_mount='--mount type=bind,src=/dev/disk/by-label,dst=/run/forge-host/disk-by-label,readonly'
  fi
  # shellcheck disable=SC2086
  docker run -d --name forge-metrics-agent \
    --restart unless-stopped \
    --network host --pid host \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --memory 64m --cpus 0.2 --pids-limit 32 \
    \$logopts \$runner_mount \$label_mount \
    -v /var/lib/forge-metrics:/etc/forge-metrics:ro \
    -v /etc/hostname:/etc/forge-host/hostname:ro \
    -e FORGE_INGEST_URL=$url \
    -e FORGE_HOST_ID=\$host_id \
    -e FORGE_INGEST_TOKEN_FILE=/etc/forge-metrics/token \
    $image python /etc/forge-metrics/agent.py >/dev/null
  sleep 8
  docker ps --filter name=forge-metrics-agent --format '{{.Names}} {{.Status}}'
  docker logs --tail 5 forge-metrics-agent 2>&1
"
