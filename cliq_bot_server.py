"""
MithilAI Cliq Bot Server
========================
A FastAPI webhook server that handles incoming Zoho Cliq bot messages.

Features:
  - Instant reply when a user messages the bot
  - "my tasks"   → shows their in-progress tasks sorted by last update
  - "status"     → shows all their open/active tasks with status
  - "all tasks"  → shows all tasks across all projects for the user
  - "@name"      → shows tasks for any named team member
  - "help"       → shows available commands
  - Daily digest is sent automatically by main.py

Deploy: Render.com (free tier) or any always-on Python host.
Set env vars same as .env (ZOHO_*, ZOHO_PORTAL, CLIQ_BOT_TOKEN etc.)
"""

import asyncio
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "cliq_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("cliq_bot")

# ── Config ────────────────────────────────────────────────────────────────────

ZOHO_PORTAL    = os.environ["ZOHO_PORTAL"]
ZOHO_CLIENT_ID = os.environ["ZOHO_CLIENT_ID"].strip("'")
ZOHO_CLIENT_SEC= os.environ["ZOHO_CLIENT_SECRET"].strip("'")
ZOHO_REFRESH   = os.environ["ZOHO_REFRESH_TOKEN"].strip("'")
PORTAL_BASE    = f"https://projectsapi.zoho.in/restapi/portal/{ZOHO_PORTAL}"

OPEN_STATUSES    = {"open", "not started", "to do", "todo", "new"}
ACTIVE_STATUSES  = {"in progress", "in-progress", "working", "development",
                    "review", "in review", "pending", "hold", "on hold"}
TESTING_STATUSES = {"testing", "for testing", "in testing", "qa", "in qa", "uat testing"}
CLOSED_STATUSES  = {
    "closed", "completed", "deployed", "ready for uat", "uat", "done",
    "complete", "finish", "finished", "signed off", "sign off",
    "cancelled", "canceled", "rejected", "deferred", "archived", "ongoing",
}

# ── Token management ──────────────────────────────────────────────────────────

_token_cache: dict = {}

def get_access_token() -> str:
    """Get a fresh Zoho access token via refresh token."""
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "client_id":     ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SEC,
        "refresh_token": ZOHO_REFRESH,
    }).encode()
    req = urllib.request.Request(
        "https://accounts.zoho.in/oauth/v2/token", data=data, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        result = json.loads(r.read().decode())
    if "access_token" not in result:
        raise RuntimeError(f"Token refresh failed: {result}")
    return result["access_token"]

def zoho_headers() -> dict:
    token = get_access_token()
    return {"Authorization": f"Zoho-oauthtoken {token}"}

# ── Zoho API helpers ──────────────────────────────────────────────────────────

async def fetch_all_tasks(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all tasks from all projects."""
    headers = zoho_headers()
    # Get all projects
    try:
        r = await client.get(f"{PORTAL_BASE}/projects/", headers=headers)
        r.raise_for_status()
        projects = r.json().get("projects", [])
    except Exception as e:
        log.error("Failed to fetch projects: %s", e)
        return []

    all_tasks = []
    for proj in projects:
        pid = proj.get("id_string") or proj.get("id")
        pname = proj.get("name", "?")
        try:
            r = await client.get(
                f"{PORTAL_BASE}/projects/{pid}/tasks/",
                headers=headers,
                params={"range": 100, "action": "allopenandclosed"},
            )
            r.raise_for_status()
            tasks = r.json().get("tasks", [])
            for t in tasks:
                t["project"] = {"id": pid, "name": pname}
            all_tasks.extend(tasks)
        except Exception as e:
            log.warning("Failed to fetch tasks for project %s: %s", pname, e)
    return all_tasks

def task_owner_names(task: dict) -> list[str]:
    raw = (task.get("details") or {}).get("owners") or task.get("owners") or task.get("owner") or []
    return [o.get("name") or o.get("full_name", "") for o in raw
            if isinstance(o, dict) and (o.get("name") or o.get("full_name"))]

def task_status(task: dict) -> str:
    return (task.get("status", {}).get("name") or "").strip()

def task_updated_ts(task: dict) -> int:
    """Return last updated timestamp as int for sorting (newest first)."""
    ts = task.get("last_updated_time_long") or task.get("updated_time_long") or 0
    try:
        return int(ts)
    except Exception:
        return 0

def is_overdue(task: dict) -> bool:
    end = task.get("end_date", "")
    if not end:
        return False
    try:
        due = datetime.strptime(end, "%m-%d-%Y")
        return due.date() < datetime.now().date()
    except Exception:
        return False

def format_task_line(task: dict, idx: int = 0) -> str:
    name    = task.get("name", "?")
    status  = task_status(task)
    pct     = task.get("percent_complete", "0")
    due     = task.get("end_date", "")
    overdue = " ⚠ OVERDUE" if is_overdue(task) else ""
    proj    = task.get("project", {}).get("name", "")
    owners  = ", ".join(task_owner_names(task)) or "Unassigned"

    updated_ts = task_updated_ts(task)
    if updated_ts:
        updated_dt = datetime.fromtimestamp(updated_ts / 1000)
        updated    = updated_dt.strftime("%d %b %Y %H:%M")
    else:
        updated = "—"

    num = f"{idx}. " if idx else "• "
    line = (
        f"{num}*{name}*\n"
        f"   Project: {proj} | Status: {status} | {pct}%{overdue}\n"
        f"   Owner: {owners} | Due: {due or '—'} | Updated: {updated}\n"
    )
    return line

# ── Message parser ────────────────────────────────────────────────────────────

def parse_command(text: str) -> tuple[str, str]:
    """Return (command, argument). Commands: my_tasks, status, all_tasks, user_tasks, help."""
    t = text.strip().lower()
    if t in ("hi", "hello", "hey", "start", "subscribe"):
        return "subscribe", ""
    if t in ("my tasks", "mytasks", "my task", "tasks", "what are my tasks"):
        return "my_tasks", ""
    if t in ("status", "my status", "update", "updates", "my updates"):
        return "status", ""
    if t in ("all tasks", "alltasks", "show all", "all"):
        return "all_tasks", ""
    if t in ("help", "commands", "?"):
        return "help", ""
    # @name or "tasks for name" or "show name tasks"
    m = re.search(r'@(\w[\w\s]*)', text)
    if m:
        return "user_tasks", m.group(1).strip()
    m = re.search(r'(?:tasks? for|show|what is|updates? for|status of)\s+(\w[\w\s]+)', t)
    if m:
        return "user_tasks", m.group(1).strip()
    # default — treat as my_tasks
    return "my_tasks", ""

# ── Response builders ─────────────────────────────────────────────────────────

def build_my_tasks_response(tasks: list[dict], sender_name: str) -> str:
    """Tasks owned by sender, in-progress first, sorted by last updated."""
    name_lower = sender_name.lower()
    mine = [t for t in tasks
            if any(name_lower in o.lower() for o in task_owner_names(t))]

    if not mine:
        return (f"Hi *{sender_name}* 👋\n"
                f"No tasks found assigned to you in Zoho Projects.\n"
                f"Check with your project manager if tasks have been assigned.")

    # Split by active/testing vs open vs closed
    active  = [t for t in mine if task_status(t).lower() in ACTIVE_STATUSES]
    testing = [t for t in mine if task_status(t).lower() in TESTING_STATUSES]
    open_   = [t for t in mine if task_status(t).lower() in OPEN_STATUSES]

    # Sort each group by last updated (newest first)
    for grp in (active, testing, open_):
        grp.sort(key=task_updated_ts, reverse=True)

    lines = [f"📋 *Tasks for {sender_name}* — {datetime.now().strftime('%d %b %Y %H:%M')}\n"]

    if active:
        lines.append(f"*🔵 In Progress ({len(active)})*")
        for i, t in enumerate(active, 1):
            lines.append(format_task_line(t, i))

    if testing:
        lines.append(f"*🧪 In Testing ({len(testing)})*")
        for i, t in enumerate(testing, 1):
            lines.append(format_task_line(t, i))

    if open_:
        lines.append(f"*⬜ Open / Not Started ({len(open_)})*")
        for i, t in enumerate(open_, 1):
            lines.append(format_task_line(t, i))

    if not active and not testing and not open_:
        lines.append("All your tasks are completed. Great work! ✅")

    lines.append(f"\n_MithilAI Agent — {datetime.now().strftime('%d %b %Y %H:%M')}_")
    return "\n".join(lines)


def build_status_response(tasks: list[dict], sender_name: str) -> str:
    """All non-closed tasks for sender grouped by status."""
    name_lower = sender_name.lower()
    mine = [t for t in tasks
            if any(name_lower in o.lower() for o in task_owner_names(t))
            and task_status(t).lower() not in CLOSED_STATUSES]

    if not mine:
        return f"*{sender_name}* — no active tasks found."

    mine.sort(key=task_updated_ts, reverse=True)

    by_status: dict[str, list] = {}
    for t in mine:
        s = task_status(t) or "Unknown"
        by_status.setdefault(s, []).append(t)

    lines = [f"📊 *Status Update for {sender_name}* — {datetime.now().strftime('%d %b %Y')}\n"]
    for status, stasks in by_status.items():
        lines.append(f"*{status}* ({len(stasks)})")
        for t in stasks:
            name    = t.get("name", "?")
            pct     = t.get("percent_complete", "0")
            proj    = t.get("project", {}).get("name", "")
            overdue = " ⚠" if is_overdue(t) else ""
            lines.append(f"  • {name} — {proj} ({pct}%){overdue}")
        lines.append("")

    lines.append(f"_MithilAI Agent — {datetime.now().strftime('%d %b %Y %H:%M')}_")
    return "\n".join(lines)


def build_user_tasks_response(tasks: list[dict], target_name: str) -> str:
    """Tasks for a specific named team member."""
    name_lower = target_name.lower()
    theirs = [t for t in tasks
              if any(name_lower in o.lower() for o in task_owner_names(t))
              and task_status(t).lower() not in CLOSED_STATUSES]

    if not theirs:
        return f"No active tasks found for *{target_name}*."

    theirs.sort(key=task_updated_ts, reverse=True)

    lines = [f"👤 *Tasks for {target_name}* — {datetime.now().strftime('%d %b %Y')}\n"]
    for i, t in enumerate(theirs, 1):
        lines.append(format_task_line(t, i))
    lines.append(f"_MithilAI Agent — {datetime.now().strftime('%d %b %Y %H:%M')}_")
    return "\n".join(lines)


def build_all_tasks_response(tasks: list[dict]) -> str:
    """Summary of all active tasks grouped by project."""
    active = [t for t in tasks if task_status(t).lower() not in CLOSED_STATUSES]
    active.sort(key=task_updated_ts, reverse=True)

    by_proj: dict[str, list] = {}
    for t in active:
        p = t.get("project", {}).get("name", "?")
        by_proj.setdefault(p, []).append(t)

    lines = [f"📋 *All Active Tasks* — {datetime.now().strftime('%d %b %Y')}\n"]
    for proj in sorted(by_proj):
        ptasks = by_proj[proj]
        lines.append(f"*{proj}* ({len(ptasks)} tasks)")
        for t in ptasks:
            name    = t.get("name", "?")
            status  = task_status(t)
            owners  = ", ".join(task_owner_names(t)) or "Unassigned"
            pct     = t.get("percent_complete", "0")
            overdue = " ⚠" if is_overdue(t) else ""
            lines.append(f"  • [{status}] {name} — {owners} ({pct}%){overdue}")
        lines.append("")

    lines.append(f"_MithilAI Agent — {datetime.now().strftime('%d %b %Y %H:%M')}_")
    return "\n".join(lines)


def build_help_response() -> str:
    return (
        "🤖 *MithilAI Agent — Bot Commands*\n\n"
        "• *my tasks* — shows your in-progress & open tasks (sorted by last update)\n"
        "• *status* — shows all your active tasks grouped by status\n"
        "• *all tasks* — shows all active tasks across all projects\n"
        "• *@name* or *tasks for name* — shows tasks for any team member\n"
        "• *help* — shows this list\n\n"
        "The bot also sends an automatic daily update every morning at 8 AM IST."
    )


def build_subscribe_response(sender_name: str) -> str:
    return (
        f"👋 Hi *{sender_name}*! Welcome to *MithilAI Agent*.\n\n"
        "I'll send you a daily task status update every morning at *8 AM IST*.\n\n"
        "You can also ask me anytime:\n"
        "• *my tasks* — your current tasks\n"
        "• *status* — all your task statuses\n"
        "• *@name* — check another team member's tasks\n"
        "• *help* — full command list"
    )

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="MithilAI Cliq Bot")


@app.get("/")
async def health():
    return {"status": "MithilAI Cliq Bot is running", "time": datetime.now().isoformat()}


@app.post("/cliq/handler")
async def cliq_handler(request: Request):
    """Main handler for incoming Zoho Cliq bot messages."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    log.info("[Bot] Incoming: %s", json.dumps(body)[:300])

    # Extract sender info — Zoho Cliq sends different shapes
    sender = body.get("sender", body.get("user", {}))
    if isinstance(sender, str):
        sender_name = sender
    else:
        sender_name = (
            sender.get("display_name") or
            sender.get("name") or
            sender.get("email", "").split("@")[0] or
            "Team Member"
        )

    text = (
        body.get("text") or
        body.get("message", {}).get("text") or
        body.get("payload", {}).get("text") or
        ""
    ).strip()

    log.info("[Bot] From: %s | Message: %s", sender_name, text)

    command, arg = parse_command(text)

    # Fetch tasks for all commands except help/subscribe
    response_text = ""
    if command in ("my_tasks", "status", "all_tasks", "user_tasks"):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                tasks = await fetch_all_tasks(client)
        except Exception as e:
            log.error("[Bot] Task fetch failed: %s", e)
            response_text = f"⚠️ Could not fetch tasks right now: {e}"
            return JSONResponse({"text": response_text})

        if command == "my_tasks":
            response_text = build_my_tasks_response(tasks, sender_name)
        elif command == "status":
            response_text = build_status_response(tasks, sender_name)
        elif command == "all_tasks":
            response_text = build_all_tasks_response(tasks)
        elif command == "user_tasks":
            response_text = build_user_tasks_response(tasks, arg or sender_name)

    elif command == "subscribe":
        response_text = build_subscribe_response(sender_name)
    else:
        response_text = build_help_response()

    log.info("[Bot] Responding to %s with %d chars", sender_name, len(response_text))
    return JSONResponse({"text": response_text})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("cliq_bot_server:app", host="0.0.0.0", port=port, reload=False)
