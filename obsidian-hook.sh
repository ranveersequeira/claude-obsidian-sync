#!/bin/bash
# obsidian-hook.sh
# Claude Code SessionEnd/Stop hook wrapper.
# Pipes the hook JSON from stdin into the Python script.
# Has a simple lock to prevent double-processing if both
# Stop and SessionEnd fire for the same session.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_SCRIPT="${SCRIPT_DIR}/obsidian-session-sync.py"
LOG_FILE="${HOME}/.claude/obsidian-sync.log"
LOCK_DIR="${HOME}/.claude/.obsidian-locks"

# Ensure dirs exist
mkdir -p "$(dirname "$LOG_FILE")" "$LOCK_DIR"

# Read all of stdin (hook input JSON)
INPUT=$(cat)

# Extract session_id for dedup lock
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)

if [ -n "$SESSION_ID" ]; then
    LOCK_FILE="${LOCK_DIR}/${SESSION_ID}.lock"

    # If lock exists and is < 30s old, skip (already processed by other hook)
    if [ -f "$LOCK_FILE" ]; then
        AGE=$(( $(date +%s) - $(stat -f%m "$LOCK_FILE" 2>/dev/null || stat -c%Y "$LOCK_FILE" 2>/dev/null || echo 0) ))
        if [ "$AGE" -lt 30 ]; then
            echo "$(date -Iseconds) SKIP duplicate session $SESSION_ID (age=${AGE}s)" >> "$LOG_FILE"
            exit 0
        fi
    fi

    # Create lock
    touch "$LOCK_FILE"

    # Clean old locks (>1hr)
    find "$LOCK_DIR" -name "*.lock" -mmin +60 -delete 2>/dev/null
fi

# Run the sync script
echo "$INPUT" | python3 "$PYTHON_SCRIPT" >> "$LOG_FILE" 2>&1
echo "$(date -Iseconds) Processed session ${SESSION_ID:-unknown}" >> "$LOG_FILE"

# Always exit 0 — passive hook, never block session end
exit 0
