#!/bin/sh
# Install the Docker-independent job observer on a NixOS runner host.
# Writes /run/forge/runner-workers.json (total jobs) and runner-lanes.json
# (per-lane max/busy and the dispatcher cap; the agent reports min(cap, sum max)).
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
# Per-lane capacity: max from the config the running dispatcher uses, busy =
# Runner.Worker processes in each lane's cgroup slice (dyn-<lane>-N containers).
# The RUNNING dispatcher's config: a runtime override may differ from the Nix one.
config=$(systemctl show -p ExecStart --value pool-dispatcher.service 2>/dev/null \
  | grep -o -- '--config [^ ;]*' | head -n1 | cut -d' ' -f2 || true)
if [ -n "$config" ] && [ -r "$config" ]; then
  slices=''
  for pid in $(pgrep -x Runner.Worker || true); do
    s=$(grep -o 'gh-lane-[A-Za-z0-9_]*\.slice' "/proc/$pid/cgroup" 2>/dev/null | tail -n1 || true)
    slices="$slices $s"
  done
  temp=$(mktemp /run/forge/.runner-lanes.XXXXXX)
  jq -c --argjson now "$observed" --arg slices "$slices" '
    ($slices | split(" ") | map(select(. != ""))) as $busy
    | {cap, updated_at: $now, lanes: [.lanes[] | .name as $n
        | ("gh-lane-" + ($n | gsub("-"; "_")) + ".slice") as $slice
        | {name: $n, max, busy: ([$busy[] | select(. == $slice)] | length)}]}' \
    "$config" > "$temp"
  chmod 0644 "$temp"
  mv -T "$temp" /run/forge/runner-lanes.json
fi
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
