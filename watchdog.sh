#!/bin/bash
# Watchdog for APRS dashboard. Run once per minute from cron.
# Priority: relay (direwolf + aprs-lite) must ALWAYS run.
# Dashboard is secondary and will be killed if it threatens the relay.

URL="http://127.0.0.1:5080/api/stats"
TIMEOUT=10
LOG="/tmp/watchdog.log"
LOAD_THRESHOLD=10
CPU_THRESHOLD=80

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# --- 1. CPU guard: if system load is critical, restart dashboard to protect relay ---
LOAD=$(awk '{printf "%d", $1}' /proc/loadavg)
if [ "$LOAD" -gt "$LOAD_THRESHOLD" ]; then
    log "CRIT: load=$LOAD (>${LOAD_THRESHOLD}) -> restart dashboard to protect relay"
    sudo /bin/systemctl restart aprs-dashboard
    sleep 5
    # If load is still critical after restart, stop dashboard entirely
    LOAD2=$(awk '{printf "%d", $1}' /proc/loadavg)
    if [ "$LOAD2" -gt "$LOAD_THRESHOLD" ]; then
        log "CRIT: load=$LOAD2 still high -> stopping dashboard"
        sudo /bin/systemctl stop aprs-dashboard
        # Ensure direwolf and aprs-lite are running
        sudo /bin/systemctl start aprs-direwolf 2>/dev/null
        sudo /bin/systemctl start aprs-lite-tui 2>/dev/null
        exit 0
    fi
fi

# --- 2. Dashboard CPU check: if gunicorn eats > 80% CPU, restart it ---
DASH_CPU=$(ps -C gunicorn -o %cpu= 2>/dev/null | awk '{s+=$1} END {printf "%d", s}')
if [ "${DASH_CPU:-0}" -gt "$CPU_THRESHOLD" ]; then
    log "WARN: dashboard CPU=${DASH_CPU}% (>${CPU_THRESHOLD}%) -> restarting"
    sudo /bin/systemctl restart aprs-dashboard
    sleep 3
fi

# --- 3. Doublon detection: let systemd own the process tree ---
PIDS=$(pgrep -f 'gunicorn.*app:app' 2>/dev/null)
COUNT=$(echo "$PIDS" | grep -c '[0-9]' || true)
# gunicorn master + 1 eventlet worker = 2 PIDs normal
if [ "$COUNT" -gt 2 ]; then
    log "WARN: $COUNT gunicorn PIDs (expected 2) -> restarting aprs-dashboard"
    sudo /bin/systemctl restart aprs-dashboard
    sleep 3
fi

# --- 4. HTTP health check ---
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$URL" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "000" ] || [ "$HTTP_CODE" = "502" ] || [ "$HTTP_CODE" = "503" ]; then
    log "WARN: dashboard HTTP $HTTP_CODE -> restarting aprs-dashboard"
    sudo /bin/systemctl restart aprs-dashboard
fi

# --- 5. Relay health: ensure direwolf is always running ---
if ! systemctl is-active --quiet aprs-direwolf; then
    log "CRIT: direwolf down -> restarting"
    sudo /bin/systemctl restart aprs-direwolf
fi
if ! systemctl is-active --quiet aprs-lite-tui; then
    log "CRIT: aprs-lite-tui down -> restarting"
    sudo /bin/systemctl restart aprs-lite-tui
fi
