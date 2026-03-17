#!/usr/bin/env python3
"""
obsidian-session-sync.py
========================
Claude Code SessionEnd hook → Obsidian vault writer.

Reads the session transcript JSONL, extracts project/task metadata
heuristically (no AI calls), and writes/updates a markdown note
in the Obsidian vault organized by project and date.

Input: JSON on stdin from Claude Code hook system
       {session_id, transcript_path, cwd, hook_event_name, reason}

Vault structure:
  <VAULT>/Claude Sessions/
    ├── Projects/
    │   └── <project-name>/
    │       └── YYYY-MM-DD.md          ← daily project note (appends sessions)
    ├── Daily/
    │       └── YYYY-MM-DD.md          ← daily aggregate (links to all projects)
    └── _index.md                      ← master MOC
"""

import json
import sys
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from typing import Optional


# ─── CONFIG ───────────────────────────────────────────────────────────────────
VAULT_PATH = Path(os.environ.get(
    "OBSIDIAN_VAULT_PATH",
    os.path.expanduser("~/vault")
))
SESSIONS_ROOT = VAULT_PATH / "Claude Sessions"
PROJECTS_DIR  = SESSIONS_ROOT / "Projects"
DAILY_DIR     = SESSIONS_ROOT / "Daily"
INDEX_FILE    = SESSIONS_ROOT / "_index.md"

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def ensure_dirs():
    """Create vault subdirectories if missing."""
    for d in [SESSIONS_ROOT, PROJECTS_DIR, DAILY_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def read_stdin_hook_input() -> dict:
    """Read the JSON blob Claude Code passes on stdin."""
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return {}


def parse_transcript(path: str) -> list[dict]:
    """
    Parse the JSONL transcript file into a list of message dicts.
    Each line is a standalone JSON object.
    """
    entries = []
    transcript = Path(path)
    if not transcript.exists():
        return entries
    with open(transcript, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def derive_project_name(cwd: str, transcript_path: str) -> str:
    """
    Infer project name from:
      1. The cwd (working directory) — last component
      2. Fall back to the Claude projects folder name
    """
    if cwd:
        # Use the leaf directory name of the working dir
        name = Path(cwd).name
        if name and name != "/" and not name.startswith("."):
            return sanitize_name(name)

    # Fallback: parse from transcript_path
    # e.g. ~/.claude/projects/-Users-ranveer-projects-myapp/session.jsonl
    if transcript_path:
        parts = Path(transcript_path).parent.name  # e.g. "-Users-ranveer-projects-myapp"
        # Take last meaningful segment
        segments = [s for s in parts.split("-") if s and s.lower() not in (
            "users", "home", "ranveer", "ranveer.kumar", "documents", "desktop"
        )]
        if segments:
            return sanitize_name(segments[-1])

    return "unknown-project"


def sanitize_name(name: str) -> str:
    """Clean a string for use as folder/file name."""
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'[\s]+', '-', name)
    return name.lower().strip("-") or "unnamed"


def clean_user_message(content: str) -> str:
    """
    Clean up a user message for display:
    - Strip <local-command-*> tags and their contents
    - Extract plan title from "# Plan: ..." headers
    - Trim to reasonable length
    """
    if not content:
        return ""

    # Remove local-command tags entirely
    content = re.sub(r'<local-command-[^>]*>.*?</local-command-[^>]*>', '', content, flags=re.DOTALL)
    content = re.sub(r'<local-command-[^>]*>[^<]*', '', content)

    # If it starts with "Implement the following plan:", extract the plan title
    plan_match = re.search(r'#\s*Plan:\s*(.+?)(?:\n|$)', content)
    if plan_match:
        return plan_match.group(1).strip()

    # Strip markdown headers
    content = re.sub(r'^#+\s+', '', content, flags=re.MULTILINE)

    # Collapse whitespace
    content = re.sub(r'\n{2,}', ' | ', content)
    content = re.sub(r'\n', ' ', content)
    content = re.sub(r'\s{2,}', ' ', content)

    return content.strip()


def extract_session_data(entries: list[dict]) -> dict:
    """
    Heuristically extract useful info from transcript entries.
    Returns dict with: user_messages, assistant_summaries, files_touched,
    tools_used, git_branch, first_prompt, duration_estimate, error_count
    """
    user_messages = []
    assistant_texts = []
    files_touched = set()
    tools_used = Counter()
    git_branch = ""
    timestamps = []
    errors = []

    for entry in entries:
        etype = entry.get("type", "")
        msg = entry.get("message", {})
        ts = entry.get("timestamp", "")
        if ts:
            timestamps.append(ts)

        # Track git branch from first entry
        if not git_branch and entry.get("gitBranch"):
            git_branch = entry["gitBranch"]

        # User messages
        if etype == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                cleaned = clean_user_message(content)
                if cleaned and len(cleaned) > 2:
                    user_messages.append(cleaned)

        # Assistant messages
        elif etype == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                assistant_texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            assistant_texts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_name = block.get("name", "unknown")
                            tools_used[tool_name] += 1
                            inp = block.get("input", {})
                            # Extract file paths from tool inputs
                            for key in ("file_path", "path", "filePath"):
                                if key in inp and isinstance(inp[key], str):
                                    files_touched.add(inp[key])
                            # Extract from bash commands
                            if tool_name in ("Bash", "bash") and "command" in inp:
                                cmd = inp["command"]
                                _extract_files_from_command(cmd, files_touched)

        # Tool results — look for errors
        elif etype == "tool_result":
            content = msg.get("content", "")
            if isinstance(content, str) and any(kw in content.lower() for kw in ["error", "traceback", "exception", "failed"]):
                errors.append(content[:200])

    # Duration estimate
    duration = ""
    if len(timestamps) >= 2:
        try:
            t0 = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
            delta = t1 - t0
            mins = int(delta.total_seconds() / 60)
            if mins < 1:
                duration = "<1 min"
            elif mins < 60:
                duration = f"{mins} min"
            else:
                duration = f"{mins // 60}h {mins % 60}m"
        except (ValueError, TypeError):
            pass

    return {
        "user_messages": user_messages,
        "assistant_texts": assistant_texts,
        "files_touched": sorted(files_touched),
        "tools_used": dict(tools_used.most_common(10)),
        "git_branch": git_branch,
        "first_prompt": user_messages[0] if user_messages else "",
        "duration": duration,
        "errors": errors[:5],
        "message_count": len(user_messages),
        "first_timestamp": timestamps[0] if timestamps else "",
        "last_timestamp": timestamps[-1] if timestamps else "",
    }


def _extract_files_from_command(cmd: str, files: set):
    """Best-effort file path extraction from shell commands."""
    # Match common file-touching patterns
    patterns = [
        r'(?:cat|less|head|tail|vim|nano|code|open|cp|mv|rm)\s+["\']?([^\s"\'|&;>]+)',
        r'>\s*["\']?([^\s"\'|&;]+)',  # redirections
    ]
    for pat in patterns:
        for m in re.finditer(pat, cmd):
            path = m.group(1)
            if "/" in path or "." in path:
                # Skip obvious non-files
                if not path.startswith("-") and not path.startswith("$"):
                    files.add(path)


def generate_task_title(data: dict) -> str:
    """
    Generate a short title from the first user prompt.
    Handles plan titles, local-command sessions, etc.
    """
    prompt = data["first_prompt"]
    if not prompt:
        return "Untitled Session"

    # Already cleaned by clean_user_message, but double-check for plan prefix
    prompt = re.sub(r'^Implement the following plan:\s*', '', prompt, flags=re.IGNORECASE)
    prompt = re.sub(r'^#\s*Plan:\s*', '', prompt, flags=re.IGNORECASE)

    # Strip common conversational prefixes
    for prefix in ["can you ", "please ", "help me ", "i need to ", "let's ", "lets ",
                    "i want to ", "we need to ", "could you "]:
        if prompt.lower().startswith(prefix):
            prompt = prompt[len(prefix):]

    # If empty after stripping
    if not prompt.strip():
        return "Untitled Session"

    # Truncate and clean
    title = prompt[:80].strip()
    if len(prompt) > 80:
        title = title.rsplit(" ", 1)[0] + "..."

    # Capitalize first letter
    return title[0].upper() + title[1:] if title else "Untitled Session"


def extract_todos_from_assistant(texts: list[str]) -> list[str]:
    """Find TODO-like items from assistant responses."""
    todos = []
    patterns = [
        r'(?:TODO|FIXME|HACK|XXX|NOTE):\s*(.+)',
        r'- \[ \]\s*(.+)',
        r'(?:you should|you\'ll need to|don\'t forget to|make sure to|remember to)\s+(.+?)(?:\.|$)',
    ]
    for text in texts[-3:]:  # Only look at last few messages
        for pat in patterns:
            for m in re.finditer(pat, text, re.IGNORECASE):
                todo = m.group(1).strip()
                if len(todo) > 10 and len(todo) < 200:
                    todos.append(todo)
    return todos[:5]  # Cap at 5


# ─── MARKDOWN GENERATION ─────────────────────────────────────────────────────

def build_session_block(
    session_id: str,
    data: dict,
    project_name: str,
    cwd: str,
    now: datetime,
    ai_summary: str = "",
) -> str:
    """Build a single session block to be appended to the daily project note."""
    time_str = now.strftime("%H:%M")
    title = generate_task_title(data)
    todos = extract_todos_from_assistant(data["assistant_texts"])

    lines = []
    summary_tag = " 🧠" if ai_summary else ""
    lines.append(f"## {time_str} — {title}{summary_tag}")
    lines.append("")

    # Metadata
    meta = []
    if data["duration"]:
        meta.append(f"Duration: {data['duration']}")
    meta.append(f"Messages: {data['message_count']}")
    if data["git_branch"]:
        meta.append(f"Branch: `{data['git_branch']}`")
    meta.append(f"Session: `{session_id[:12]}`")
    if ai_summary:
        meta.append("Source: AI summary")
    lines.append(" · ".join(meta))
    lines.append("")

    # If we have an AI summary, use it as the primary content
    if ai_summary:
        lines.append(ai_summary)
        lines.append("")
        # Still append heuristic file list if AI didn't mention files
        if data["files_touched"] and "## Files" not in ai_summary:
            lines.append("**Files touched (auto-detected):**")
            for f in data["files_touched"][:15]:
                lines.append(f"- `{f}`")
            lines.append("")
    else:
        # Heuristic-only output (no AI summary)

        # Files touched
        if data["files_touched"]:
            lines.append("**Files touched:**")
            for f in data["files_touched"][:15]:
                lines.append(f"- `{f}`")
            lines.append("")

        # Tools used
        if data["tools_used"]:
            tool_str = ", ".join(f"{k} ({v})" for k, v in data["tools_used"].items())
            lines.append(f"**Tools:** {tool_str}")
            lines.append("")

        # Key prompts (first + last if different)
        lines.append("**What I worked on:**")
        if data["user_messages"]:
            lines.append(f"> {data['user_messages'][0][:200]}")
            if len(data["user_messages"]) > 2:
                last = data["user_messages"][-1][:200]
                if last != data["user_messages"][0][:200]:
                    lines.append(f"> ...last: {last}")
        lines.append("")

        # Errors
        if data["errors"]:
            lines.append(f"**Errors encountered:** {len(data['errors'])}")
            for err in data["errors"][:3]:
                short = err.replace("\n", " ")[:120]
                lines.append(f"- `{short}`")
            lines.append("")

        # TODOs
        if todos:
            lines.append("**TODOs:**")
            for t in todos:
                lines.append(f"- [ ] {t}")
            lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def build_daily_project_frontmatter(project_name: str, date_str: str) -> str:
    """YAML frontmatter for the daily project note."""
    return f"""---
project: {project_name}
date: {date_str}
type: claude-session
tags:
  - claude-session
  - project/{project_name}
---

# {project_name} — {date_str}

"""


def build_daily_note_entry(project_name: str, date_str: str, title: str, time_str: str) -> str:
    """One-liner for the daily aggregate note."""
    return f"- {time_str} [[Projects/{project_name}/{date_str}|{project_name}]] — {title}\n"


def build_daily_note_frontmatter(date_str: str) -> str:
    return f"""---
date: {date_str}
type: claude-daily
tags:
  - claude-daily
---

# Claude Sessions — {date_str}

"""


# ─── FILE WRITERS ─────────────────────────────────────────────────────────────

def write_project_note(project_name: str, date_str: str, session_block: str):
    """Append session block to Projects/<name>/YYYY-MM-DD.md"""
    proj_dir = PROJECTS_DIR / project_name
    proj_dir.mkdir(parents=True, exist_ok=True)

    note_path = proj_dir / f"{date_str}.md"

    if note_path.exists():
        # Append to existing
        with open(note_path, "a", encoding="utf-8") as f:
            f.write("\n" + session_block)
    else:
        # Create with frontmatter
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(build_daily_project_frontmatter(project_name, date_str))
            f.write(session_block)


def update_daily_note(date_str: str, entry: str):
    """Append one-liner to Daily/YYYY-MM-DD.md"""
    note_path = DAILY_DIR / f"{date_str}.md"

    if note_path.exists():
        with open(note_path, "a", encoding="utf-8") as f:
            f.write(entry)
    else:
        with open(note_path, "w", encoding="utf-8") as f:
            f.write(build_daily_note_frontmatter(date_str))
            f.write(entry)


def update_index(project_name: str, date_str: str):
    """Update or create the master index (MOC) file."""
    if INDEX_FILE.exists():
        content = INDEX_FILE.read_text(encoding="utf-8")
    else:
        content = """---
type: claude-moc
tags:
  - claude-moc
---

# Claude Sessions — Index

"""

    # Add project link if not already present
    project_link = f"[[Projects/{project_name}/{date_str}|{project_name}]]"
    project_section = f"## [[Projects/{project_name}|{project_name}]]"

    if project_section not in content:
        content += f"\n{project_section}\n\n"

    # Add date link under project if not present
    date_link_line = f"- [[Projects/{project_name}/{date_str}|{date_str}]]"
    if date_link_line not in content:
        # Insert after project header
        idx = content.find(project_section)
        if idx != -1:
            insert_at = content.find("\n\n", idx + len(project_section))
            if insert_at == -1:
                content += date_link_line + "\n"
            else:
                content = content[:insert_at] + "\n" + date_link_line + content[insert_at:]

    INDEX_FILE.write_text(content, encoding="utf-8")


# ─── JSONL → READABLE CONVERTER ──────────────────────────────────────────────

def convert_jsonl_to_readable(transcript_path: str, output_path: str = None, fmt: str = "md"):
    """
    Convert a JSONL transcript to a human-readable file.
    fmt: "md" for markdown (default, much more readable), "json" for structured JSON
    """
    entries = parse_transcript(transcript_path)
    if not entries:
        print(f"No entries found in {transcript_path}")
        return

    if fmt == "json":
        _convert_to_json(entries, transcript_path, output_path)
    else:
        _convert_to_markdown(entries, transcript_path, output_path)


def _convert_to_markdown(entries: list[dict], transcript_path: str, output_path: str = None):
    """Convert transcript to a clean, readable markdown file."""
    if not output_path:
        output_path = transcript_path.replace(".jsonl", ".md")

    # Extract session metadata from first entry
    session_info = {}
    for entry in entries:
        if entry.get("sessionId"):
            session_info = {
                "session_id": entry.get("sessionId", ""),
                "cwd": entry.get("cwd", ""),
                "git_branch": entry.get("gitBranch", ""),
                "version": entry.get("version", ""),
            }
            break

    data = extract_session_data(entries)
    project = derive_project_name(session_info.get("cwd", ""), transcript_path)

    lines = []
    lines.append(f"# Session: {project}")
    lines.append("")

    # Metadata block
    meta = []
    if session_info.get("session_id"):
        meta.append(f"ID: `{session_info['session_id'][:12]}`")
    if session_info.get("cwd"):
        meta.append(f"Dir: `{session_info['cwd']}`")
    if session_info.get("git_branch"):
        meta.append(f"Branch: `{session_info['git_branch']}`")
    if data.get("duration"):
        meta.append(f"Duration: {data['duration']}")
    meta.append(f"Messages: {data['message_count']}")
    lines.append(" · ".join(meta))
    lines.append("")
    lines.append("---")
    lines.append("")

    # Conversation
    msg_num = 0
    for entry in entries:
        etype = entry.get("type", "")
        msg = entry.get("message", {})
        ts = entry.get("timestamp", "")

        time_str = ""
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M:%S")
            except (ValueError, TypeError):
                pass

        if etype == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                cleaned = clean_user_message(content)
                if cleaned:
                    msg_num += 1
                    lines.append(f"### [{time_str}] You (#{msg_num})")
                    lines.append("")
                    # Show first 500 chars of cleaned message
                    display = cleaned[:500]
                    if len(cleaned) > 500:
                        display += "..."
                    lines.append(display)
                    lines.append("")

        elif etype == "assistant":
            content = msg.get("content", "")
            texts = []
            tools = []

            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            texts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_name = block.get("name", "?")
                            inp = block.get("input", {})
                            tools.append(f"`{tool_name}` → {_summarize_input(inp)}")

            if texts or tools:
                lines.append(f"### [{time_str}] Claude")
                lines.append("")

                if tools:
                    for t in tools:
                        lines.append(f"  {t}")
                    lines.append("")

                if texts:
                    combined = "\n".join(t for t in texts if t.strip())
                    # Show first 800 chars
                    display = combined[:800]
                    if len(combined) > 800:
                        display += "\n\n*...truncated...*"
                    lines.append(display)
                    lines.append("")

        elif etype == "tool_result":
            content = msg.get("content", "")
            result_text = ""
            if isinstance(content, str):
                result_text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        result_text = block.get("text", "")
                        break

            if result_text:
                # Only show errors or short results, skip long tool output
                is_error = any(kw in result_text.lower() for kw in ["error", "traceback", "exception"])
                if is_error:
                    lines.append(f"> **Error:** `{result_text[:200].replace(chr(10), ' ')}`")
                    lines.append("")
                elif len(result_text) < 200:
                    lines.append(f"> `{result_text.strip()[:200]}`")
                    lines.append("")

    # Summary at bottom
    lines.append("---")
    lines.append("")
    lines.append("## Files touched")
    if data["files_touched"]:
        for f in data["files_touched"][:20]:
            lines.append(f"- `{f}`")
    else:
        lines.append("*(none detected)*")
    lines.append("")

    if data["tools_used"]:
        tool_str = ", ".join(f"{k} ({v})" for k, v in data["tools_used"].items())
        lines.append(f"**Tools used:** {tool_str}")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Converted: {output_path}")
    return output_path


def _convert_to_json(entries: list[dict], transcript_path: str, output_path: str = None):
    """Convert transcript to structured JSON (original format)."""
    if not output_path:
        output_path = transcript_path.replace(".jsonl", ".json")

    readable = {
        "session_info": {},
        "conversation": [],
    }

    for entry in entries:
        etype = entry.get("type", "")
        msg = entry.get("message", {})
        ts = entry.get("timestamp", "")

        if not readable["session_info"] and entry.get("sessionId"):
            readable["session_info"] = {
                "session_id": entry.get("sessionId"),
                "cwd": entry.get("cwd"),
                "git_branch": entry.get("gitBranch", ""),
                "version": entry.get("version", ""),
            }

        conv_entry = {"type": etype, "timestamp": ts}

        if etype == "user":
            content = msg.get("content", "")
            conv_entry["content"] = clean_user_message(content) if isinstance(content, str) else str(content)

        elif etype == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                conv_entry["content"] = content
            elif isinstance(content, list):
                texts = []
                tool_calls = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            texts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "tool": block.get("name"),
                                "input_summary": _summarize_input(block.get("input", {})),
                            })
                if texts:
                    conv_entry["content"] = "\n".join(texts)
                if tool_calls:
                    conv_entry["tool_calls"] = tool_calls

        elif etype == "tool_result":
            content = msg.get("content", "")
            if isinstance(content, str):
                conv_entry["result_preview"] = content[:300]
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        conv_entry["result_preview"] = block.get("text", "")[:300]
                        break

        readable["conversation"].append(conv_entry)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(readable, f, indent=2, ensure_ascii=False)

    print(f"Converted: {output_path}")
    return output_path


def _summarize_input(inp: dict) -> str:
    """Short summary of tool input for readability."""
    if "command" in inp:
        return f"$ {inp['command'][:100]}"
    if "file_path" in inp:
        return f"file: {inp['file_path']}"
    if "path" in inp:
        return f"path: {inp['path']}"
    if "query" in inp:
        return f"query: {inp['query'][:80]}"
    # Generic: show keys
    return ", ".join(inp.keys())[:80] if inp else ""


def get_session_datetime(data: dict, fallback: datetime = None) -> datetime:
    """
    Get the actual session datetime from transcript timestamps.
    Falls back to now() if no timestamps found.
    """
    ts = data.get("first_timestamp", "")
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            # Convert to local time
            return dt.astimezone(tz=None).replace(tzinfo=None)
        except (ValueError, TypeError):
            pass
    return fallback or datetime.now()


def process_single_transcript(
    transcript_path: str,
    cwd: str = "",
    session_id: str = "unknown",
    dry_run: bool = False,
    ai_summary: str = "",
) -> Optional[dict]:

    """
    Process one transcript file → write to vault.
    Returns info dict or None if skipped.
    """
    entries = parse_transcript(transcript_path)
    if not entries:
        return None

    data = extract_session_data(entries)

    if data["message_count"] < 2:
        return None

    # Get cwd from transcript entries if not provided
    if not cwd:
        for entry in entries:
            if entry.get("cwd"):
                cwd = entry["cwd"]
                break

    # Get session_id from transcript if not provided
    if session_id == "unknown":
        for entry in entries:
            if entry.get("sessionId"):
                session_id = entry["sessionId"]
                break
        if session_id == "unknown":
            session_id = Path(transcript_path).stem

    project_name = derive_project_name(cwd, transcript_path)
    session_dt = get_session_datetime(data)
    date_str = session_dt.strftime("%Y-%m-%d")
    time_str = session_dt.strftime("%H:%M")

    if dry_run:
        title = generate_task_title(data)
        return {
            "project": project_name,
            "date": date_str,
            "time": time_str,
            "title": title,
            "messages": data["message_count"],
            "files": len(data["files_touched"]),
            "transcript": transcript_path,
        }

    ensure_dirs()
    session_block = build_session_block(session_id, data, project_name, cwd, session_dt, ai_summary=ai_summary)
    title = generate_task_title(data)

    write_project_note(project_name, date_str, session_block)
    update_daily_note(date_str, build_daily_note_entry(project_name, date_str, title, time_str))
    update_index(project_name, date_str)

    return {
        "project": project_name,
        "date": date_str,
        "title": title,
        "transcript": transcript_path,
    }


def get_all_transcripts(recent_n: int = 0) -> list[Path]:
    """
    Find all JSONL transcript files across all Claude Code projects.
    If recent_n > 0, return only the N most recently modified.
    """
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return []

    all_jsonl = []
    for proj_dir in claude_projects.iterdir():
        if proj_dir.is_dir():
            for f in proj_dir.glob("*.jsonl"):
                all_jsonl.append(f)

    # Sort by modification time (newest first)
    all_jsonl.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    if recent_n > 0:
        return all_jsonl[:recent_n]
    return all_jsonl


def get_synced_sessions() -> set:
    """
    Read a tracking file to know which sessions we've already synced.
    Prevents duplicates on re-runs.
    """
    tracker = SESSIONS_ROOT / ".synced_sessions"
    if tracker.exists():
        return set(tracker.read_text().strip().split("\n"))
    return set()


def mark_session_synced(session_id: str):
    """Append session ID to the tracking file."""
    tracker = SESSIONS_ROOT / ".synced_sessions"
    tracker.parent.mkdir(parents=True, exist_ok=True)
    with open(tracker, "a") as f:
        f.write(session_id + "\n")


def cmd_sync(recent_n: int = 0, dry_run: bool = False, force: bool = False):
    """
    Bulk sync all (or recent N) existing Claude Code sessions to Obsidian.
    Skips sessions already synced unless --force is used.
    """
    transcripts = get_all_transcripts(recent_n)
    if not transcripts:
        print("No transcripts found in ~/.claude/projects/")
        return

    already_synced = set() if force else get_synced_sessions()
    synced = 0
    skipped_dup = 0
    skipped_empty = 0

    print(f"Found {len(transcripts)} transcript(s) to process...")
    if dry_run:
        print("(DRY RUN — no files will be written)\n")

    for t_path in transcripts:
        session_id = t_path.stem

        if session_id in already_synced:
            skipped_dup += 1
            continue

        result = process_single_transcript(
            transcript_path=str(t_path),
            session_id=session_id,
            dry_run=dry_run,
        )

        if result is None:
            skipped_empty += 1
            continue

        synced += 1
        if dry_run:
            print(f"  [{result['date']} {result['time']}] {result['project']}: "
                  f"{result['title'][:60]}  ({result['messages']} msgs, {result['files']} files)")
        else:
            mark_session_synced(session_id)
            print(f"  ✓ {result['date']} | {result['project']} | {result['title'][:50]}")

    print(f"\nDone: {synced} synced, {skipped_dup} already synced, {skipped_empty} empty/skipped")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Claude Code → Obsidian session sync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Sync all existing sessions to Obsidian (skips already-synced)
  python3 obsidian-session-sync.py --sync

  # Sync only the last 10 sessions
  python3 obsidian-session-sync.py --sync --recent 10

  # Preview what would be synced (no writes)
  python3 obsidian-session-sync.py --sync --dry-run

  # Force re-sync everything (ignores tracking)
  python3 obsidian-session-sync.py --sync --force

  # Convert JSONL to readable markdown (default)
  python3 obsidian-session-sync.py --convert <file.jsonl>

  # Convert JSONL to JSON instead
  python3 obsidian-session-sync.py --convert <file.jsonl> --json

  # List all Claude Code projects
  python3 obsidian-session-sync.py --list-projects

  # Hook mode (called by Claude Code automatically, reads stdin)
  echo '{"session_id":...}' | python3 obsidian-session-sync.py
""",
    )

    parser.add_argument("--sync", action="store_true",
                        help="Bulk sync existing sessions to Obsidian vault")
    parser.add_argument("--recent", type=int, default=0,
                        help="Only sync the N most recent sessions (use with --sync)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be synced without writing files")
    parser.add_argument("--force", action="store_true",
                        help="Re-sync all sessions even if already synced")
    parser.add_argument("--convert", metavar="JSONL_FILE",
                        help="Convert JSONL transcript to readable markdown (or JSON with --json)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON instead of markdown (use with --convert)")
    parser.add_argument("--output", metavar="FILE",
                        help="Output path for --convert (default: same name with .md or .json)")
    parser.add_argument("--list-projects", action="store_true",
                        help="List all Claude Code projects and session counts")

    # If no args at all, assume hook mode (stdin)
    if len(sys.argv) == 1:
        # Hook mode: read JSON from stdin
        hook_input = read_stdin_hook_input()
        if not hook_input:
            sys.exit(0)

        session_id = hook_input.get("session_id", "unknown")
        ai_summary = ""

        # Check if /my-memory was triggered (flag file exists)
        flag_file = Path.home() / ".claude" / ".obsidian-locks" / f"{session_id}.memory"
        if flag_file.exists():
            # Extract AI summary from last_assistant_message (Stop hook provides this)
            last_msg = hook_input.get("last_assistant_message", "")
            if last_msg:
                ai_summary = last_msg
            else:
                # Fallback: read last assistant message from transcript
                transcript_path = hook_input.get("transcript_path", "")
                if transcript_path:
                    entries = parse_transcript(transcript_path)
                    # Find the last assistant text (the summary)
                    for entry in reversed(entries):
                        if entry.get("type") == "assistant":
                            content = entry.get("message", {}).get("content", "")
                            if isinstance(content, str) and len(content) > 100:
                                ai_summary = content
                                break
                            elif isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        text = block.get("text", "")
                                        if len(text) > 100:
                                            ai_summary = text
                                            break
                                if ai_summary:
                                    break

            # Clean up flag file
            flag_file.unlink(missing_ok=True)

        result = process_single_transcript(
            transcript_path=hook_input.get("transcript_path", ""),
            cwd=hook_input.get("cwd", ""),
            session_id=session_id,
            ai_summary=ai_summary,
        )
        if result:
            ensure_dirs()
            mark_session_synced(session_id)
        sys.exit(0)

    args = parser.parse_args()

    if args.convert:
        fmt = "json" if args.json else "md"
        convert_jsonl_to_readable(args.convert, args.output, fmt=fmt)
    elif args.list_projects:
        claude_projects = Path.home() / ".claude" / "projects"
        if claude_projects.exists():
            total_sessions = 0
            for d in sorted(claude_projects.iterdir()):
                if d.is_dir():
                    sessions = list(d.glob("*.jsonl"))
                    total_sessions += len(sessions)
                    print(f"  {d.name}  ({len(sessions)} sessions)")
            print(f"\n  Total: {total_sessions} sessions")
        else:
            print("No ~/.claude/projects/ directory found")
    elif args.sync:
        cmd_sync(recent_n=args.recent, dry_run=args.dry_run, force=args.force)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
