#!/bin/bash
# Install to /usr/local/sbin/iob-watchdog.sh (root:root 755).
#
# Deliberately NOT kept in the checkout: the iob user has write access there,
# and this script is executed by root.
#
# Targets the unit by name. The old stopeverything.sh used `pkill -f`, which
# matches by pattern across every process on the box -- dangerous here, where
# other algos share the VPS.

set -u

UNIT="iob"
LOG_DIR="/opt/algo/kotak_zerodha_prasad_patankar/logs"
STALE_AFTER=240   # seconds; matches the old monitor.sh threshold

systemctl is-active --quiet "$UNIT" || exit 0

LOG=$(ls -t "$LOG_DIR"/Logfile_*.log 2>/dev/null | head -1)
[ -z "$LOG" ] && exit 0

AGE=$(( $(date +%s) - $(stat -c %Y "$LOG") ))
if (( AGE > STALE_AFTER )); then
    logger -t iob-watchdog "log stale ${AGE}s (>${STALE_AFTER}s) -- restarting $UNIT"
    systemctl restart "$UNIT"
fi
