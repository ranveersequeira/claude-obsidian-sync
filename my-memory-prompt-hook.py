#!/usr/bin/env python3
"""
my-memory-prompt-hook.py
========================
UserPromptSubmit hook — intercepts /save-brain command.

When user types "/save-brain", this hook:
1. Replaces the prompt with a structured summarize instruction
2. Creates a flag file so the Stop hook knows to capture the AI summary
3. Claude generates the summary → Stop hook writes it to Obsidian
"""

import json
import sys
import os
from pathlib import Path

FLAG_DIR = Path.home() / ".claude" / ".obsidian-locks"

def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    prompt = hook_input.get("prompt", "").strip()
    session_id = hook_input.get("session_id", "unknown")

    # Intercept /save-brain (primary) and /my-memory (legacy alias)
    if prompt.lower() not in (
        "/save-brain",
        "/save-brain ",
        "save-brain",
        "/my-memory",
        "/my-memory ",
        "my-memory",
    ):
        sys.exit(0)

    # Create flag file so Stop hook knows this is an AI-summary session
    FLAG_DIR.mkdir(parents=True, exist_ok=True)
    flag_file = FLAG_DIR / f"{session_id}.memory"
    flag_file.write_text("pending")

    # Replace the user's prompt with a structured summarize instruction
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "updatedPrompt": """Summarize this entire session for my knowledge base. Use this exact format:

## Session Summary
<2-3 sentence overview of what we accomplished>

## Key Decisions
- <decision 1>
- <decision 2>

## Files Changed
- `<file path>` — <what changed>

## What Worked
- <thing that went well>

## Open TODOs
- [ ] <todo 1>
- [ ] <todo 2>

## Gotchas / Learnings
- <anything surprising or worth remembering>

After generating this summary, exit the session."""
        }
    }

    json.dump(output, sys.stdout)
    sys.exit(0)

if __name__ == "__main__":
    main()
