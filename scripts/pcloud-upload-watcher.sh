#!/bin/bash
# pCloud Upload Pipeline - Upload Watcher
# Monitors the uploads directory and uploads files to pCloud via rclone.
# Tracks progress in a JSON file for the dashboard and records
# transfer history in SQLite.

# Load config from environment file
CONFIG_FILE="${PCLOUD_PIPELINE_ENV:-/etc/pcloud-pipeline.env}"
if [ -f "$CONFIG_FILE" ]; then
    set -a
    source "$CONFIG_FILE"
    set +a
fi

PIPELINE_BASE="${PIPELINE_BASE:?PIPELINE_BASE must be set}"
RCLONE_REMOTE="${RCLONE_REMOTE:?RCLONE_REMOTE must be set}"
UPLOAD_DIR="$PIPELINE_BASE/uploads"
LOG_FILE="$PIPELINE_BASE/logs/pcloud-upload.log"
PROGRESS_FILE="$PIPELINE_BASE/pcloud-progress.json"
TEMP_FILE="$PIPELINE_BASE/pcloud-progress.json.tmp$$"
DB_FILE="$PIPELINE_BASE/transfers.db"
export TMPDIR="/tmp/pcloud-upload"
mkdir -p "$TMPDIR" "$UPLOAD_DIR"

echo "$(date +"%H:%M:%S"): Upload watcher started" >> "$LOG_FILE"
echo "{}" > "$PROGRESS_FILE"

update_progress() {
    local FILE="$1"
    local PERCENT="$2"
    (
        flock -x 200
        CURRENT=$(cat "$PROGRESS_FILE" 2>/dev/null || echo "{}")
        python3 -c "import json,sys; d=json.loads(sys.argv[1]); d[sys.argv[2]]=int(sys.argv[3]); print(json.dumps(d))" "$CURRENT" "$FILE" "$PERCENT" > "$TEMP_FILE" 2>/dev/null || echo "{\"$FILE\":$PERCENT}" > "$TEMP_FILE"
        mv -f "$TEMP_FILE" "$PROGRESS_FILE"
    ) 200>/var/lock/pcloud-progress.lock
}

remove_progress() {
    local FILE="$1"
    (
        flock -x 200
        CURRENT=$(cat "$PROGRESS_FILE" 2>/dev/null || echo "{}")
        python3 -c "import json,sys; d=json.loads(sys.argv[1]); d.pop(sys.argv[2],None); print(json.dumps(d))" "$CURRENT" "$FILE" > "$TEMP_FILE" 2>/dev/null || echo "{}" > "$TEMP_FILE"
        mv -f "$TEMP_FILE" "$PROGRESS_FILE"
    ) 200>/var/lock/pcloud-progress.lock
}

db_start_transfer() {
    local RELPATH="$1"
    local SIZE="$2"
    local ESCAPED=$(echo "$RELPATH" | sed "s/'/''/g")
    local ROW_ID
    ROW_ID=$(sqlite3 -cmd ".timeout 5000" "$DB_FILE" \
        "INSERT INTO transfers(source_path, dest_path, operation, state, size_bytes, progress, created_at, updated_at) VALUES('$ESCAPED', '$RCLONE_REMOTE/$ESCAPED', 'upload', 'uploading', $SIZE, 0, datetime('now'), datetime('now')); SELECT last_insert_rowid();" 2>/dev/null)
    echo "$ROW_ID"
}

db_complete_transfer() {
    local ROW_ID="$1"
    [ -z "$ROW_ID" ] && return
    sqlite3 -cmd ".timeout 5000" "$DB_FILE" \
        "UPDATE transfers SET state='completed', updated_at=datetime('now') WHERE id=$ROW_ID;" 2>/dev/null
}

db_fail_transfer() {
    local ROW_ID="$1"
    local ERROR="$2"
    [ -z "$ROW_ID" ] && return
    local ESCAPED_ERR=$(echo "$ERROR" | sed "s/'/''/g")
    sqlite3 -cmd ".timeout 5000" "$DB_FILE" \
        "UPDATE transfers SET state='failed', error='$ESCAPED_ERR', updated_at=datetime('now') WHERE id=$ROW_ID;" 2>/dev/null
}

upload_file() {
    local FILEPATH="$1"
    if [ ! -f "$FILEPATH" ]; then return; fi

    FILESIZE=$(stat -c%s "$FILEPATH" 2>/dev/null || echo 0)
    FILESIZE_MB=$(awk "BEGIN {printf \"%.2f\", $FILESIZE/1024/1024}")
    RELPATH="${FILEPATH#$UPLOAD_DIR/}"

    echo "$(date +"%H:%M:%S") [UPLOAD] Uploading: $RELPATH (${FILESIZE_MB}MB)" >> "$LOG_FILE"

    update_progress "$RELPATH" 0
    DB_ID=$(db_start_transfer "$RELPATH" "$FILESIZE")

    rclone moveto "$FILEPATH" "$RCLONE_REMOTE/$RELPATH" \
        --transfers 4 \
        --buffer-size 256M \
        --timeout 1h \
        --retries 5 \
        --checksum \
        --stats 2s \
        --stats-one-line \
        --log-level INFO 2>&1 | while IFS= read -r line; do
            if [[ "$line" =~ ([0-9]+)% ]]; then
                PERCENT="${BASH_REMATCH[1]}"
                update_progress "$RELPATH" "$PERCENT"
            fi
            echo "$line" >> "$LOG_FILE"
        done

    UPLOAD_STATUS=${PIPESTATUS[0]}

    if [ $UPLOAD_STATUS -eq 0 ]; then
        echo "$(date +"%H:%M:%S") [UPLOAD] Verified: $RELPATH" >> "$LOG_FILE"
        remove_progress "$RELPATH"
        db_complete_transfer "$DB_ID"

        PARENT_DIR=$(dirname "$FILEPATH")
        while [ "$PARENT_DIR" != "$UPLOAD_DIR" ] && [ -d "$PARENT_DIR" ]; do
            rmdir "$PARENT_DIR" 2>/dev/null || break
            PARENT_DIR=$(dirname "$PARENT_DIR")
        done
    else
        echo "$(date +"%H:%M:%S") [UPLOAD] FAILED: $RELPATH" >> "$LOG_FILE"
        remove_progress "$RELPATH"
        db_fail_transfer "$DB_ID" "rclone exit code $UPLOAD_STATUS"
    fi
}

# Upload all existing files at startup
echo "$(date +"%H:%M:%S"): Checking for existing files..." >> "$LOG_FILE"
find "$UPLOAD_DIR" -type f 2>/dev/null | while read FILEPATH; do
    echo "$(date +"%H:%M:%S"): Found existing: ${FILEPATH#$UPLOAD_DIR/}" >> "$LOG_FILE"
    upload_file "$FILEPATH" &
    while [ "$(jobs -r | wc -l)" -ge 2 ]; do sleep 1; done
done

echo "$(date +"%H:%M:%S"): Existing file scan complete. Watching for new files..." >> "$LOG_FILE"

# Watch for new files (moved_to from staging watcher's mv)
inotifywait -m -r -e moved_to "$UPLOAD_DIR" --format '%w%f' |
while read ITEMPATH; do
    sleep 1
    if [ -f "$ITEMPATH" ]; then
        upload_file "$ITEMPATH" &
        while [ "$(jobs -r | wc -l)" -ge 2 ]; do sleep 1; done
    fi
done
