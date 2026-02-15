#!/bin/bash
# pCloud Upload Pipeline - Staging Watcher
# Monitors the staging directory for new files and moves them to uploads
# when they are complete (not being written to).
#
# Uses inotifywait for real-time detection + periodic scan as fallback
# to catch files missed when directories are recreated.

# Load config from environment file
CONFIG_FILE="${PCLOUD_PIPELINE_ENV:-/etc/pcloud-pipeline.env}"
if [ -f "$CONFIG_FILE" ]; then
    set -a
    source "$CONFIG_FILE"
    set +a
fi

PIPELINE_BASE="${PIPELINE_BASE:?PIPELINE_BASE must be set}"
STAGING_DIR="$PIPELINE_BASE/staging"
UPLOAD_DIR="$PIPELINE_BASE/uploads"
LOG_FILE="$PIPELINE_BASE/logs/pcloud-staging.log"
SCAN_INTERVAL=60

mkdir -p "$STAGING_DIR" "$UPLOAD_DIR" "$(dirname "$LOG_FILE")"
echo "$(date +"%H:%M:%S"): Staging watcher started (with periodic scan every ${SCAN_INTERVAL}s)" >> "$LOG_FILE"

is_file_complete() {
    local file="$1"
    if lsof "$file" >/dev/null 2>&1; then return 1; fi
    local size1=$(stat -c%s "$file" 2>/dev/null || echo 0)
    sleep 5
    local size2=$(stat -c%s "$file" 2>/dev/null || echo 0)
    if [ "$size1" -eq "$size2" ] && [ "$size1" -gt 0 ]; then return 0; else return 1; fi
}

move_to_uploads() {
    local FILEPATH="$1"
    local SOURCE="$2"
    if [ ! -f "$FILEPATH" ]; then return; fi

    FILENAME=$(basename "$FILEPATH")
    FILESIZE=$(stat -c%s "$FILEPATH" 2>/dev/null || echo 0)
    FILESIZE_MB=$(awk "BEGIN {printf \"%.2f\", $FILESIZE/1024/1024}")
    RELPATH="${FILEPATH#$STAGING_DIR/}"

    echo "$(date +"%H:%M:%S") [STAGE] $FILENAME (${FILESIZE_MB}MB) [$SOURCE]" >> "$LOG_FILE"

    WAIT_COUNT=0
    while ! is_file_complete "$FILEPATH"; do
        WAIT_COUNT=$((WAIT_COUNT + 1))
        if [ $WAIT_COUNT -eq 1 ]; then
            echo "$(date +"%H:%M:%S") [STAGE] Waiting for completion..." >> "$LOG_FILE"
        fi
        sleep 15
    done

    DESTDIR="$UPLOAD_DIR/$(dirname "$RELPATH")"
    mkdir -p "$DESTDIR"

    if mv "$FILEPATH" "$UPLOAD_DIR/$RELPATH"; then
        echo "$(date +"%H:%M:%S") [STAGE] Queued: $RELPATH" >> "$LOG_FILE"

        # Safe cleanup: only rmdir empty dirs
        sleep 1
        PARENT_DIR=$(dirname "$FILEPATH")
        while [ "$PARENT_DIR" != "$STAGING_DIR" ] && [ -d "$PARENT_DIR" ]; do
            rmdir "$PARENT_DIR" 2>/dev/null || break
            PARENT_DIR=$(dirname "$PARENT_DIR")
        done
    else
        echo "$(date +"%H:%M:%S") [STAGE] FAILED to move: $FILENAME" >> "$LOG_FILE"
    fi
}

# Periodic scan: catches files missed by inotify (e.g. after dir recreated)
periodic_scan() {
    while true; do
        sleep "$SCAN_INTERVAL"
        local count=$(find "$STAGING_DIR" -type f 2>/dev/null | wc -l)
        if [ "$count" -gt 0 ]; then
            echo "$(date +"%H:%M:%S") [STAGE] Periodic scan found $count file(s)" >> "$LOG_FILE"
            find "$STAGING_DIR" -type f 2>/dev/null | while read FILEPATH; do
                move_to_uploads "$FILEPATH" "scan"
            done
        fi
    done
}

# Start periodic scan in background
periodic_scan &
SCAN_PID=$!
trap "kill $SCAN_PID 2>/dev/null; exit" EXIT INT TERM

# Process existing staged files on startup
echo "$(date +"%H:%M:%S"): Checking for existing staged files..." >> "$LOG_FILE"
find "$STAGING_DIR" -type f 2>/dev/null | while read FILEPATH; do
    move_to_uploads "$FILEPATH" "startup"
done
echo "$(date +"%H:%M:%S"): Existing file scan complete" >> "$LOG_FILE"

# Watch for new files via inotify
inotifywait -m -r -e close_write,moved_to "$STAGING_DIR" --format '%w%f' |
while read FILEPATH; do
    if [ ! -f "$FILEPATH" ]; then continue; fi
    move_to_uploads "$FILEPATH" "inotify"
done
