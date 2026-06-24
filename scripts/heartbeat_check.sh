#!/bin/bash
# Heartbeat: after the morning launchd fire, this script checks whether the
# live site shows today's Melbourne date. If not, fires a macOS notification.
# Council #2 (2026-06-24) caught: Day 1 failures were noticed only because
# the user was awake. Without this, a Day 7 lid-closed run could die silently
# and not be caught until Day 14.
#
# Wired to a separate launchd plist that fires at 07:00 Melbourne local.

set -u

TODAY=$(TZ=Australia/Melbourne date +%Y-%m-%d)
URL="https://shapovalov0415-stack.github.io/urban-design-daily/"
LOG=/Users/hirotostation/Library/Logs/urban-design-daily-heartbeat.log

mkdir -p "$(dirname "$LOG")"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] heartbeat check for $TODAY" >> "$LOG"

# Fetch the live site and look for today's date in the inlined JSON.
if curl -sSL --max-time 30 "$URL" | grep -q "\"date\":[[:space:]]*\"$TODAY\""; then
    echo "  ✅ $TODAY visible on live site" >> "$LOG"
    exit 0
fi

# Not visible. Fire macOS notification.
osascript -e "display notification \"urban-design-daily: no $TODAY commit by 07:00 AEST. Check ~/Library/Logs/urban-design-daily-rss.log\" with title \"Urban Design Daily — silent failure\" sound name \"Funk\"" 2>/dev/null || true
echo "  ❌ $TODAY NOT on live site — notification fired" >> "$LOG"
exit 1
