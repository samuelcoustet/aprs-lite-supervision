#!/bin/bash
# Watchdog for APRS dashboard. Run once per minute from cron.
# Priority: relay (direwolf + aprs-lite) must ALWAYS run.
# Dashboard is secondary and will be killed if it threatens the relay.

URL="http://127.0.0.1:5080/api/system/alive"
TIMEOUT=10
LOG="/tmp/watchdog.log"
LOAD_THRESHOLD=10
CPU_THRESHOLD=80
IDLE_MAX=1800  # 30 minutes
IDLE_FLAG="/tmp/dashboard_idle"
IDLE_WAKE=600  # 10 min: try restarting to check for new users

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# --- 0. Relay health: ensure direwolf + aprs-lite ALWAYS run ---
if ! systemctl is-active --quiet aprs-direwolf; then
    log "CRIT: direwolf down -> restarting"
    sudo -n /bin/systemctl restart aprs-direwolf
fi
if ! systemctl is-active --quiet aprs-lite-tui; then
    log "CRIT: aprs-lite-tui down -> restarting"
    sudo -n /bin/systemctl restart aprs-lite-tui
fi

# --- 1. CPU guard: if system load is critical, restart dashboard to protect relay ---
LOAD=$(awk '{printf "%d", $1}' /proc/loadavg)
if [ "$LOAD" -gt "$LOAD_THRESHOLD" ]; then
    log "CRIT: load=$LOAD (>${LOAD_THRESHOLD}) -> restart dashboard to protect relay"
    sudo -n /bin/systemctl restart aprs-dashboard
    sleep 5
    LOAD2=$(awk '{printf "%d", $1}' /proc/loadavg)
    if [ "$LOAD2" -gt "$LOAD_THRESHOLD" ]; then
        log "CRIT: load=$LOAD2 still high -> stopping dashboard"
        sudo -n /bin/systemctl stop aprs-dashboard
        sudo -n /bin/systemctl start aprs-direwolf 2>/dev/null
        sudo -n /bin/systemctl start aprs-lite-tui 2>/dev/null
        exit 0
    fi
fi

# --- 2. Dashboard not running ---
if ! systemctl is-active --quiet aprs-dashboard; then
    if [ -f "$IDLE_FLAG" ]; then
        # Intentional idle stop — wake up periodically to check for new users
        IDLE_AGE=$(( $(date +%s) - $(stat -c %Y "$IDLE_FLAG" 2>/dev/null || echo 0) ))
        if [ "$IDLE_AGE" -gt "$IDLE_WAKE" ]; then
            log "INFO: idle wake-up check (${IDLE_AGE}s since idle stop)"
            rm -f "$IDLE_FLAG"
            sudo -n /bin/systemctl start aprs-dashboard
        fi
    else
        # Crashed — restart immediately
        log "WARN: dashboard down (not idle) -> restarting"
        sudo -n /bin/systemctl restart aprs-dashboard
    fi
    exit 0
fi

# --- 3. Dashboard CPU check ---
DASH_CPU=$(ps -C gunicorn -o %cpu= 2>/dev/null | awk '{s+=$1} END {printf "%d", s}')
if [ "${DASH_CPU:-0}" -gt "$CPU_THRESHOLD" ]; then
    log "WARN: dashboard CPU=${DASH_CPU}% (>${CPU_THRESHOLD}%) -> restarting"
    sudo -n /bin/systemctl restart aprs-dashboard
    sleep 3
fi

# --- 4. Doublon detection ---
PIDS=$(pgrep -f '[g]unicorn.*app:app' 2>/dev/null | grep -v "$$")
COUNT=$(echo "$PIDS" | grep -c '[0-9]' || true)
if [ "$COUNT" -gt 2 ]; then
    log "WARN: $COUNT gunicorn PIDs (expected 2) -> restarting"
    sudo -n /bin/systemctl restart aprs-dashboard
    sleep 3
fi

# --- 5. Port health check (avoids gevent blocking on HTTP) ---
if ! ss -tlnp 2>/dev/null | grep -q ":5080 "; then
    log "WARN: port 5080 not listening -> restarting"
    sudo -n /bin/systemctl restart aprs-dashboard
    exit 0
fi

# --- 6. Idle timeout check ---
ACTIVITY_FILE="/tmp/dashboard_active"

# Try alive endpoint (short timeout — worker may be busy with WebSocket)
CLIENTS=-1
IDLE=0
RESP=$(curl -s --max-time 3 "$URL" 2>/dev/null)
if [ -n "$RESP" ]; then
    CLIENTS=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('clients',0))" 2>/dev/null || echo "-1")
    IDLE=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('idle_seconds',0))" 2>/dev/null || echo "0")
fi

# If alive unreachable (worker busy), fall back to activity file age
if [ "${CLIENTS}" = "-1" ]; then
    if [ -f "$ACTIVITY_FILE" ]; then
        LAST_TS=$(cat "$ACTIVITY_FILE" 2>/dev/null || echo "0")
        NOW_TS=$(date +%s)
        IDLE=$(( NOW_TS - LAST_TS ))
    fi
    CLIENTS=0
fi

# --- 7. Idle timeout: 0 clients AND idle > 30 min -> stop ---
if [ "${CLIENTS}" = "0" ] && [ "${IDLE}" -gt "$IDLE_MAX" ]; then
    log "INFO: idle ${IDLE}s, 0 clients -> stopping dashboard (will wake in ${IDLE_WAKE}s)"
    sudo -n /bin/systemctl stop aprs-dashboard
    touch "$IDLE_FLAG"
    exit 0
fi
