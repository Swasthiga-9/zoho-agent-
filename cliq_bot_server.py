"""
MithilAI Cliq Bot Server
========================
FastAPI webhook server for Zoho Cliq ProjectStatusBot.

Features:
  - Subscribe → receive full team daily update (everyone gets all tasks)
  - Suggestion buttons on every reply for quick navigation
  - my tasks    → your own tasks only
  - all tasks   → all active tasks across all projects
  - overdue     → all overdue tasks
  - in testing  → tasks currently in testing/QA
  - unassigned  → tasks with no owner
  - summary     → quick count overview
  - @name       → tasks for a specific person
  - help        → command list with suggestions
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
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

ZOHO_PORTAL     = os.environ["ZOHO_PORTAL"]
ZOHO_CLIENT_ID  = os.environ["ZOHO_CLIENT_ID"].strip("'")
ZOHO_CLIENT_SEC = os.environ["ZOHO_CLIENT_SECRET"].strip("'")
ZOHO_REFRESH    = os.environ["ZOHO_REFRESH_TOKEN"].strip("'")
PORTAL_BASE     = f"https://projectsapi.zoho.in/restapi/portal/{ZOHO_PORTAL}"

OPEN_STATUSES    = {"open", "not started", "to do", "todo", "new"}
ACTIVE_STATUSES  = {"in progress", "in-progress", "working", "development",
                    "review", "in review", "pending"}
TESTING_STATUSES = {"testing", "for testing", "in testing", "qa", "in qa", "uat testing"}
CLOSED_STATUSES  = {
    "closed", "completed", "deployed", "ready for uat", "uat", "done",
    "complete", "finish", "finished", "signed off", "sign off",
    "cancelled", "canceled", "rejected", "deferred", "archived", "ongoing",
}

SUBSCRIBERS_FILE = Path(__file__).parent / "subscribers.json"

# ── Subscriber management ─────────────────────────────────────────────────────

def load_subscribers() -> list[dict]:
    try:
        return json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_subscribers(subs: list[dict]) -> None:
    SUBSCRIBERS_FILE.write_text(json.dumps(subs, indent=2), encoding="utf-8")

def add_subscriber(email: str, name: str) -> bool:
    """Returns True if newly added, False if already subscribed."""
    subs   = load_subscribers()
    emails = {s["email"].lower() for s in subs}
    if email.lower() in emails:
        return False
    subs.append({"email": email, "name": name})
    save_subscribers(subs)
    return True

# ── Zoho token ────────────────────────────────────────────────────────────────

def get_access_token() -> str:
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

# ── Zoho task helpers ─────────────────────────────────────────────────────────

async def fetch_all_tasks(client: httpx.AsyncClient) -> list[dict]:
    headers = {"Authorization": f"Zoho-oauthtoken {get_access_token()}"}
    try:
        r = await client.get(f"{PORTAL_BASE}/projects/", headers=headers)
        r.raise_for_status()
        projects = r.json().get("projects", [])
    except Exception as e:
        log.error("Failed to fetch projects: %s", e)
        return []

    all_tasks = []
    for proj in projects:
        pid   = proj.get("id_string") or proj.get("id")
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
            log.warning("Failed tasks for %s: %s", pname, e)
    return all_tasks

def owner_names(task: dict) -> list[str]:
    raw = (task.get("details") or {}).get("owners") or task.get("owners") or task.get("owner") or []
    return [o.get("name") or o.get("full_name", "") for o in raw
            if isinstance(o, dict) and (o.get("name") or o.get("full_name"))]

def t_status(task: dict) -> str:
    return (task.get("status", {}).get("name") or "").strip()

def t_updated(task: dict) -> int:
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
        return datetime.strptime(end, "%m-%d-%Y").date() < datetime.now().date()
    except Exception:
        return False

def task_line(t: dict) -> str:
    name    = t.get("name", "?")
    proj    = t.get("project", {}).get("name", "")
    status  = t_status(t)
    owners  = ", ".join(owner_names(t)) or "Unassigned"
    pct     = t.get("percent_complete", "0")
    due     = t.get("end_date", "")
    flag    = " ⚠" if is_overdue(t) else ""
    due_str = f" | Due: {due}" if due else ""
    return f"  • *{name}*\n    {proj} | {status} | {owners} | {pct}%{due_str}{flag}"

# ── Suggestion buttons ────────────────────────────────────────────────────────

# Zoho Cliq supports "suggestions" — clickable text chips shown below a message
MAIN_SUGGESTIONS = ["my tasks", "all tasks", "overdue", "in testing", "unassigned", "summary", "help"]
AFTER_SUBSCRIBE  = ["my tasks", "all tasks", "overdue", "summary", "help"]

def cliq_response(text: str, suggestions: list[str] = None) -> dict:
    """Build a Zoho Cliq bot response with optional suggestion chips."""
    resp: dict = {"text": text}
    if suggestions:
        resp["suggestions"] = suggestions
    return resp

# ── Command parser ────────────────────────────────────────────────────────────

def parse_command(text: str) -> tuple[str, str]:
    t = text.strip().lower()
    if t in ("hi", "hello", "hey", "start", "subscribe", "sub"):
        return "subscribe", ""
    if t in ("my tasks", "mytasks", "my task", "mine"):
        return "my_tasks", ""
    if t in ("all tasks", "alltasks", "all", "show all", "everyone", "team tasks"):
        return "all_tasks", ""
    if t in ("overdue", "overdue tasks", "late", "delayed"):
        return "overdue", ""
    if t in ("in testing", "testing", "for testing", "qa", "test"):
        return "testing", ""
    if t in ("unassigned", "no owner", "not assigned"):
        return "unassigned", ""
    if t in ("summary", "overview", "count", "stats"):
        return "summary", ""
    if t in ("help", "commands", "?", "what can you do"):
        return "help", ""
    m = re.search(r'@([\w][\w\s]*)', text)
    if m:
        return "user_tasks", m.group(1).strip()
    m = re.search(r'(?:tasks? for|show|updates? for|status of)\s+([\w][\w\s]+)', t)
    if m:
        return "user_tasks", m.group(1).strip()
    return "all_tasks", ""   # default — show all tasks

# ── Response builders ─────────────────────────────────────────────────────────

def build_all_tasks(tasks: list[dict], sender_name: str) -> str:
    """Full team task view — what every subscriber gets."""
    active = [t for t in tasks if t_status(t).lower() not in CLOSED_STATUSES]

    in_prog  = [t for t in active if t_status(t).lower() in ACTIVE_STATUSES]
    testing  = [t for t in active if t_status(t).lower() in TESTING_STATUSES]
    overdue  = [t for t in active if is_overdue(t)]
    unassign = [t for t in active if not owner_names(t)]

    in_prog.sort(key=t_updated, reverse=True)
    testing.sort(key=t_updated, reverse=True)

    today = datetime.now().strftime("%d %b %Y")
    lines = [f"📋 *Team Task Report — {today}*\n"]

    if in_prog:
        # Group by owner
        by_owner: dict[str, list] = {}
        for t in in_prog:
            for o in owner_names(t) or ["Unassigned"]:
                by_owner.setdefault(o, []).append(t)
        lines.append(f"*🔵 In Progress ({len(in_prog)})*")
        for o in sorted(by_owner):
            lines.append(f"  ▸ *{o}*")
            for t in by_owner[o]:
                proj    = t.get("project", {}).get("name", "")
                pct     = t.get("percent_complete", "0")
                due     = t.get("end_date", "")
                flag    = " ⚠" if is_overdue(t) else ""
                due_str = f" | Due: {due}" if due else ""
                lines.append(f"    • {t.get('name','?')} — {proj} ({pct}%){due_str}{flag}")
        lines.append("")

    if testing:
        lines.append(f"*🧪 In Testing ({len(testing)})*")
        for t in testing:
            owners  = ", ".join(owner_names(t)) or "Unassigned"
            proj    = t.get("project", {}).get("name", "")
            due     = t.get("end_date", "")
            due_str = f" | Due: {due}" if due else ""
            lines.append(f"  • {t.get('name','?')} — {proj} | {owners}{due_str}")
        lines.append("")

    if overdue:
        lines.append(f"*⚠ Overdue ({len(overdue)})*")
        for t in overdue[:5]:
            owners = ", ".join(owner_names(t)) or "Unassigned"
            proj   = t.get("project", {}).get("name", "")
            lines.append(f"  • {t.get('name','?')} — {proj} | {owners} | Due: {t.get('end_date','?')}")
        if len(overdue) > 5:
            lines.append(f"  ... and {len(overdue)-5} more. Type *overdue* for full list.")
        lines.append("")

    if unassign:
        lines.append(f"*👤 Unassigned ({len(unassign)})*")
        for t in unassign[:5]:
            proj   = t.get("project", {}).get("name", "")
            status = t_status(t)
            lines.append(f"  • {t.get('name','?')} — {proj} [{status}]")
        if len(unassign) > 5:
            lines.append(f"  ... and {len(unassign)-5} more. Type *unassigned* for full list.")
        lines.append("")

    lines.append(
        f"📊 *{len(in_prog)}* in progress  |  *{len(testing)}* in testing  |  "
        f"*{len(overdue)}* overdue  |  *{len(unassign)}* unassigned  |  *{len(active)}* total active"
    )
    lines.append(f"\n_MithilAI Agent — {today}_")
    return "\n".join(lines)


def build_my_tasks(tasks: list[dict], sender_name: str) -> str:
    """Only tasks assigned to the requesting user."""
    name_lower = sender_name.lower()
    mine = [t for t in tasks
            if any(name_lower in o.lower() for o in owner_names(t))
            and t_status(t).lower() not in CLOSED_STATUSES]

    today = datetime.now().strftime("%d %b %Y")
    if not mine:
        return (f"*{sender_name}* — no active tasks found assigned to you today.\n"
                f"_MithilAI Agent — {today}_")

    active  = sorted([t for t in mine if t_status(t).lower() in ACTIVE_STATUSES], key=t_updated, reverse=True)
    testing = sorted([t for t in mine if t_status(t).lower() in TESTING_STATUSES], key=t_updated, reverse=True)
    open_   = sorted([t for t in mine if t_status(t).lower() in OPEN_STATUSES], key=t_updated, reverse=True)

    lines = [f"👤 *My Tasks — {sender_name}* — {today}\n"]
    if active:
        lines.append(f"*🔵 In Progress ({len(active)})*")
        for t in active:
            lines.append(task_line(t))
        lines.append("")
    if testing:
        lines.append(f"*🧪 In Testing ({len(testing)})*")
        for t in testing:
            lines.append(task_line(t))
        lines.append("")
    if open_:
        lines.append(f"*⬜ Open ({len(open_)})*")
        for t in open_:
            lines.append(task_line(t))
        lines.append("")
    if not active and not testing and not open_:
        lines.append("All your tasks are completed ✅")
    lines.append(f"_MithilAI Agent — {today}_")
    return "\n".join(lines)


def build_overdue(tasks: list[dict]) -> str:
    overdue = [t for t in tasks
               if t_status(t).lower() not in CLOSED_STATUSES and is_overdue(t)]
    today = datetime.now().strftime("%d %b %Y")
    if not overdue:
        return f"✅ No overdue tasks as of {today}."
    overdue.sort(key=lambda t: t.get("end_date", ""), reverse=False)
    lines = [f"⚠ *Overdue Tasks ({len(overdue)}) — {today}*\n"]
    for t in overdue:
        lines.append(task_line(t))
    lines.append(f"\n_MithilAI Agent — {today}_")
    return "\n".join(lines)


def build_testing(tasks: list[dict]) -> str:
    testing = [t for t in tasks if t_status(t).lower() in TESTING_STATUSES]
    today = datetime.now().strftime("%d %b %Y")
    if not testing:
        return f"🧪 No tasks currently in testing as of {today}."
    testing.sort(key=t_updated, reverse=True)
    lines = [f"🧪 *In Testing ({len(testing)}) — {today}*\n"]
    for t in testing:
        lines.append(task_line(t))
    lines.append(f"\n_MithilAI Agent — {today}_")
    return "\n".join(lines)


def build_unassigned(tasks: list[dict]) -> str:
    unassign = [t for t in tasks
                if t_status(t).lower() not in CLOSED_STATUSES and not owner_names(t)]
    today = datetime.now().strftime("%d %b %Y")
    if not unassign:
        return f"✅ No unassigned tasks as of {today}."
    lines = [f"👤 *Unassigned Tasks ({len(unassign)}) — {today}*\n"]
    for t in unassign:
        proj   = t.get("project", {}).get("name", "")
        status = t_status(t)
        lines.append(f"  • *{t.get('name','?')}*\n    {proj} | {status}")
    lines.append(f"\n_MithilAI Agent — {today}_")
    return "\n".join(lines)


def build_summary(tasks: list[dict]) -> str:
    active   = [t for t in tasks if t_status(t).lower() not in CLOSED_STATUSES]
    in_prog  = [t for t in active if t_status(t).lower() in ACTIVE_STATUSES]
    testing  = [t for t in active if t_status(t).lower() in TESTING_STATUSES]
    open_    = [t for t in active if t_status(t).lower() in OPEN_STATUSES]
    overdue  = [t for t in active if is_overdue(t)]
    unassign = [t for t in active if not owner_names(t)]

    # Per person workload
    by_person: dict[str, int] = {}
    for t in in_prog:
        for o in owner_names(t) or ["Unassigned"]:
            by_person[o] = by_person.get(o, 0) + 1

    today = datetime.now().strftime("%d %b %Y")
    lines = [f"📊 *Team Summary — {today}*\n"]
    lines.append(f"🔵 In Progress : *{len(in_prog)}*")
    lines.append(f"🧪 In Testing  : *{len(testing)}*")
    lines.append(f"⬜ Open        : *{len(open_)}*")
    lines.append(f"⚠ Overdue     : *{len(overdue)}*")
    lines.append(f"👤 Unassigned  : *{len(unassign)}*")
    lines.append(f"📋 Total Active: *{len(active)}*\n")

    if by_person:
        lines.append("*In Progress by person:*")
        for person, count in sorted(by_person.items(), key=lambda x: -x[1]):
            lines.append(f"  • {person}: {count} task(s)")

    lines.append(f"\n_MithilAI Agent — {today}_")
    return "\n".join(lines)


def build_user_tasks(tasks: list[dict], target_name: str) -> str:
    name_lower = target_name.lower()
    theirs = [t for t in tasks
              if any(name_lower in o.lower() for o in owner_names(t))
              and t_status(t).lower() not in CLOSED_STATUSES]
    today = datetime.now().strftime("%d %b %Y")
    if not theirs:
        return f"No active tasks found for *{target_name}* — {today}."
    theirs.sort(key=t_updated, reverse=True)
    lines = [f"👤 *Tasks for {target_name}* — {today}\n"]
    for t in theirs:
        lines.append(task_line(t))
    lines.append(f"\n_MithilAI Agent — {today}_")
    return "\n".join(lines)


def build_subscribe(sender_name: str, sender_email: str, already: bool) -> str:
    if already:
        return (
            f"👋 Hi *{sender_name}*! You are already subscribed.\n"
            f"You receive the full team task report every morning at *8 AM IST*.\n"
            f"Use the suggestions below to explore tasks anytime."
        )
    return (
        f"✅ *{sender_name}*, you are now subscribed to *MithilAI Agent*!\n\n"
        f"Every morning at *8 AM IST* you will receive:\n"
        f"• Full team task report — all projects, all owners\n"
        f"• In Progress tasks grouped by person\n"
        f"• In Testing list\n"
        f"• Overdue & unassigned highlights\n\n"
        f"You can also ask me anything anytime using the suggestions below."
    )


def build_help() -> str:
    return (
        "🤖 *MithilAI Agent — Commands*\n\n"
        "• *all tasks* — full team task report\n"
        "• *my tasks* — only your assigned tasks\n"
        "• *overdue* — all tasks past due date\n"
        "• *in testing* — tasks currently in QA/testing\n"
        "• *unassigned* — tasks with no owner\n"
        "• *summary* — quick count + per-person workload\n"
        "• *@name* — tasks for a specific person\n"
        "• *subscribe* — sign up for daily 8 AM updates\n\n"
        "Daily updates are sent automatically every morning at *8 AM IST*."
    )

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="MithilAI Cliq Bot")


@app.get("/")
async def health():
    return {"status": "MithilAI Cliq Bot is running", "time": datetime.now().isoformat()}


@app.post("/cliq/handler")
async def cliq_handler(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    log.info("[Bot] Incoming: %s", json.dumps(body)[:400])

    # Extract sender
    sender = body.get("sender") or body.get("user") or {}
    if isinstance(sender, str):
        sender_name  = sender
        sender_email = ""
    else:
        sender_email = sender.get("email") or ""
        sender_name  = (
            sender.get("display_name") or
            sender.get("name") or
            sender_email.split("@")[0] or
            "Team Member"
        )

    # Extract message text
    raw_text = body.get("text") or ""
    if not isinstance(raw_text, str):
        raw_text = (body.get("message") or {}).get("text") or ""
    text = raw_text.strip()

    # Ignore empty or bot-originated messages
    if body.get("type") in ("bot", "bot_message") or not text:
        return JSONResponse({"text": ""})

    log.info("[Bot] From: %s (%s) | Message: %s", sender_name, sender_email, text)

    command, arg = parse_command(text)

    # Commands that need task data
    needs_tasks = command in ("all_tasks", "my_tasks", "overdue", "testing",
                              "unassigned", "summary", "user_tasks")
    tasks = []
    if needs_tasks:
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                tasks = await fetch_all_tasks(client)
        except Exception as e:
            log.error("[Bot] Task fetch failed: %s", e)
            return JSONResponse(cliq_response(
                f"⚠️ Could not fetch tasks right now: {e}\nPlease try again in a moment.",
                MAIN_SUGGESTIONS
            ))

    # Build response
    if command == "subscribe":
        already  = not add_subscriber(sender_email, sender_name)
        msg      = build_subscribe(sender_name, sender_email, already)
        return JSONResponse(cliq_response(msg, AFTER_SUBSCRIBE))

    elif command == "all_tasks":
        msg = build_all_tasks(tasks, sender_name)
        return JSONResponse(cliq_response(msg, MAIN_SUGGESTIONS))

    elif command == "my_tasks":
        msg = build_my_tasks(tasks, sender_name)
        return JSONResponse(cliq_response(msg, MAIN_SUGGESTIONS))

    elif command == "overdue":
        msg = build_overdue(tasks)
        return JSONResponse(cliq_response(msg, MAIN_SUGGESTIONS))

    elif command == "testing":
        msg = build_testing(tasks)
        return JSONResponse(cliq_response(msg, MAIN_SUGGESTIONS))

    elif command == "unassigned":
        msg = build_unassigned(tasks)
        return JSONResponse(cliq_response(msg, MAIN_SUGGESTIONS))

    elif command == "summary":
        msg = build_summary(tasks)
        return JSONResponse(cliq_response(msg, MAIN_SUGGESTIONS))

    elif command == "user_tasks":
        msg = build_user_tasks(tasks, arg or sender_name)
        return JSONResponse(cliq_response(msg, MAIN_SUGGESTIONS))

    else:
        msg = build_help()
        return JSONResponse(cliq_response(msg, MAIN_SUGGESTIONS))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("cliq_bot_server:app", host="0.0.0.0", port=port, reload=False)
