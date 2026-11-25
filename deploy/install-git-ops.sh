#!/bin/sh
# Usage: deploy/install-git-ops.sh actions|runner root@host [SSH options]
set -eu
kind=${1:?actions or runner}
target=${2:?SSH target}
shift 2
here=$(cd "$(dirname "$0")/.." && pwd)
case "$kind" in
  actions) name=forge-git-ops; script=forge_git_ops.py; interval=120s ;;
  runner) name=forge-runner-events; script=forge_runner_events.py; interval=20s ;;
  *) exit 2 ;;
esac
ssh "$@" "$target" "install -d -m 0755 /usr/local/lib/forge-dashboard && cat > /usr/local/lib/forge-dashboard/$script" < "$here/agent/$script"
ssh "$@" "$target" "
set -eu
python=\$(command -v python3 || true)
if [ -z \"\$python\" ]; then
    pid=\$(systemctl show pool-dispatcher -p MainPID --value)
    python=\$(readlink -f /proc/\$pid/exe)
fi
test -x \"\$python\"
units=/etc/systemd/system.control
install -d -m 0755 \"\$units/timers.target.wants\"
cat > \"\$units/$name.service\" <<UNIT
[Unit]
Description=$name reporter for Forge CI

[Service]
Type=oneshot
ExecStart=\$python /usr/local/lib/forge-dashboard/$script
Environment=PATH=/run/current-system/sw/bin:/usr/local/bin:/usr/bin:/bin
StateDirectory=$name
StateDirectoryMode=0700
NoNewPrivileges=yes
TimeoutStartSec=110s
UNIT
cat > \"\$units/$name.timer\" <<UNIT
[Timer]
OnBootSec=20s
OnUnitActiveSec=$interval
AccuracySec=3s
UNIT
ln -sfn ../$name.timer \"\$units/timers.target.wants/$name.timer\"
systemctl daemon-reload
systemctl start $name.timer
systemctl start --no-block $name.service
"
