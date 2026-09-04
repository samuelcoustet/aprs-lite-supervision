#!/bin/bash
# Watchdog HTTP for the APRS dashboard. Run once per minute from cron.

URL="http://127.0.0.1:5080/api/stats"
TIMEOUT=10
LOG="/tmp/watchdog.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# Let systemd own the process tree. Never kill individual dashboard PIDs.
PIDS=$(ps aux | grep 'aprs-sidecar-dashboard.*app\.py' | grep -v grep | awk '{print $2}')
COUNT=$(echo "$PIDS" | grep -c '[0-9]' || true)
if [ "$COUNT" -gt 1 ]; then
    log "WARN: $COUNT instances detected -> restarting aprs-dashboard"
    sudo /bin/systemctl restart aprs-dashboard
    sleep 3
fi

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$URL" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "000" ] || [ "$HTTP_CODE" = "502" ] || [ "$HTTP_CODE" = "503" ]; then
    log "WARN: dashboard HTTP $HTTP_CODE -> restarting aprs-dashboard"
    sudo /bin/systemctl restart aprs-dashboard
fi
