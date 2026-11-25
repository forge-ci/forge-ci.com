#!/bin/sh
# Install the Docker-independent job observer on a NixOS runner host.
set -eu

target=${1:?runner SSH target}
shift

ssh "$@" "$target" '/run/current-system/sw/bin/bash -se' <<'REMOTE'
set -euo pipefail
observer=/var/lib/forge-metrics/publish-runner-workers
units=/etc/systemd/system.control
install -d -m 0755 "$units" /run/forge
install -d -m 0700 /var/lib/forge-metrics
cat > "$observer" <<'SCRIPT'
set -euo pipefail
observed=$(date +%s)
# ps emits process leaders, not threads. Runner.Listener is idle and zombies
# are workers that have already exited.
count=$(ps -eo stat=,comm= | awk '$1 !~ /^Z/ && $2 == "Runner.Worker" { n++ } END { print n+0 }')
temp=$(mktemp /run/forge/.runner-workers.XXXXXX)
printf '{"gh_jobs_running":%s,"updated_at":%s}\n' "$count" "$observed" > "$temp"
chmod 0644 "$temp"
mv -T "$temp" /run/forge/runner-workers.json
SCRIPT
chmod 0600 "$observer"
cat > "$units/forge-runner-workers.service" <<UNIT
[Unit]
Description=Observe active GitHub Actions workers without querying Docker

[Service]
Type=oneshot
ExecStart=/run/current-system/sw/bin/bash $observer
Environment=PATH=/run/current-system/sw/bin
RuntimeDirectory=forge
RuntimeDirectoryPreserve=yes
NoNewPrivileges=yes
TimeoutStartSec=5s
UNIT
cat > "$units/forge-runner-workers.timer" <<'UNIT'
[Unit]
Description=Refresh the Forge CI worker count

[Timer]
OnBootSec=10s
OnUnitActiveSec=10s
AccuracySec=1s

[Install]
WantedBy=timers.target
UNIT
# NixOS manages /etc/systemd/system. Add the local dependency in the
# writable system.control unit layer along with the timer and service.
install -d -m 0755 "$units/timers.target.wants"
ln -sfn ../forge-runner-workers.timer "$units/timers.target.wants/forge-runner-workers.timer"
systemctl daemon-reload
systemctl start forge-runner-workers.timer
systemctl start forge-runner-workers.service
REMOTE
