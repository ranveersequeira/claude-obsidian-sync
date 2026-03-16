#!/bin/bash
# my-memory-check.sh
# Fast gate for UserPromptSubmit hook.
# Reads stdin, checks if prompt is /save-brain (or legacy /my-memory).
# Only launches Python if it matches. Otherwise exits in <5ms.

INPUT=$(cat)

# Extract prompt — use jq if available (fast), fallback to sed
if command -v jq &>/dev/null; then
  PROMPT=$(echo "$INPUT" | jq -r '.prompt // ""' 2>/dev/null | tr '[:upper:]' '[:lower:]' | xargs)
else
  PROMPT=$(echo "$INPUT" | sed -n 's/.*"prompt"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | tr '[:upper:]' '[:lower:]' | xargs)
fi

case "$PROMPT" in
  /save-brain|save-brain|/my-memory|my-memory)
    echo "$INPUT" | python3 ~/.claude/hooks/my-memory-prompt-hook.py
    ;;
  *)
    exit 0
    ;;
esac
