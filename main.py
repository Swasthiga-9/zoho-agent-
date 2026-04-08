"""
Zoho Projects Daily Agent  —  Full Workflow
============================================
Runs every day at 8 AM (Windows Task Scheduler).

Features:
  1. Fetches EVERY task from ALL projects
  2. Feedback loop  — reads human replies to bot questions; upgrades/downgrades comment type
  3. Smart routing  — escalation emails go to Siva OR Dhinesh based on task owner team
  4. Auto-assign    — unassigned tasks are assigned to the configured default person
  5. Comment types  — new_task / missing_info / analytics / replan / digest
  6. Daily HTML report email sent after every run

Config: edit .env  |  Logs: logs/agent.log  |  Schedule: setup_scheduler.py
"""

import asyncio
import json
import logging
import os
import re
import smtplib
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
import httpx
import keyring
from openai import AsyncOpenAI
from dotenv import load_dotenv, set_key

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_DIR  = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "agent.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("zoho_agent")

# ── Config ────────────────────────────────────────────────────────────────────

ZOHO_PORTAL     = os.environ["ZOHO_PORTAL"]
ZOHO_CLIENT_ID  = os.environ["ZOHO_CLIENT_ID"].strip("'")
ZOHO_CLIENT_SEC = os.environ["ZOHO_CLIENT_SECRET"].strip("'")
ZOHO_REFRESH    = os.environ["ZOHO_REFRESH_TOKEN"].strip("'")
OPENAI_KEY      = os.getenv("OPENAI_API_KEY", "").strip("'")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "").strip("'")

if not OPENAI_KEY:
    log.error("OPENAI_API_KEY is not set in .env — exiting.")
    sys.exit(1)
if not ANTHROPIC_KEY:
    log.warning("ANTHROPIC_API_KEY not set — BPM task analysis will fall back to OpenAI.")

# BPM task detection keywords (task name / tasklist / description)
BPM_KEYWORDS = [
    "bpm", "business process management", "epicor bpm",
    "pre-processing", "post-processing", "method directive",
    "baq", "customization", "bos", "service connect",
    "erp customisation", "erp customization", "customisation layer",
]

GMAIL_USER    = os.getenv("GMAIL_USER", "")
SIVA_EMAIL    = os.getenv("SIVA_EMAIL", "")
DHINESH_EMAIL = os.getenv("DHINESH_EMAIL", "")

# Email To / CC — 2 primary recipients + 2 CC
_EMAIL_TO = [e for e in [
    os.getenv("EMAIL_TO_1", ""),
    os.getenv("EMAIL_TO_2", ""),
] if e.strip()]
_EMAIL_CC = [e for e in [
    os.getenv("EMAIL_CC_1", ""),
    os.getenv("EMAIL_CC_2", ""),
] if e.strip()]
# Fallback: if TO list not set, use Siva + Dhinesh
if not _EMAIL_TO:
    _EMAIL_TO = [e for e in [SIVA_EMAIL, DHINESH_EMAIL] if e]

# Password: try Windows Credential Manager first, fall back to GMAIL_APP_PASSWORD env var
try:
    _keyring_pass = keyring.get_password("zoho_agent_gmail", "gmail_app_password") or ""
except Exception:
    _keyring_pass = ""
GMAIL_PASS = (_keyring_pass or os.getenv("GMAIL_APP_PASSWORD", "")).strip()
if not GMAIL_PASS:
    log.warning("[Email] No Gmail password found. "
                "On Windows run 'python setup_email.py'. "
                "On GitHub Actions set GMAIL_APP_PASSWORD secret.")
EMAIL_ENABLED = bool(GMAIL_USER and GMAIL_PASS and _EMAIL_TO)

# Team membership — comma-separated first names or full names (case-insensitive)
SIVA_TEAM    = [n.strip().lower() for n in os.getenv("SIVA_TEAM", "").split(",") if n.strip()]
DHINESH_TEAM = [n.strip().lower() for n in os.getenv("DHINESH_TEAM", "").split(",") if n.strip()]

ESCALATION_DAYS    = int(os.getenv("ESCALATION_DAYS", "7"))
DIGEST_HOURS       = int(os.getenv("DIGEST_INACTIVITY_HOURS", "24"))
BOT_COOLDOWN_HOURS = int(os.getenv("BOT_COOLDOWN_HOURS", "24"))
NO_REPLY_HOURS     = int(os.getenv("NO_REPLY_HOURS", "48"))   # hours before "no reply" escalation
KEYWORDS           = [k.strip().lower() for k in os.getenv(
    "KEYWORDS", "blocked,urgent,overdue,review needed,stuck,waiting,delayed"
).split(",")]

ENV_PATH    = os.path.join(os.path.dirname(__file__), ".env")
PORTAL_BASE = f"https://projectsapi.zoho.in/restapi/portal/{ZOHO_PORTAL}"
BOT_MARKER  = "— MithilAI Agent •"

# ── Status sets ───────────────────────────────────────────────────────────────

OPEN_STATUSES    = {"open", "not started", "to do", "todo", "new"}
TESTING_STATUSES = {"testing", "for testing", "in testing", "qa", "in qa", "uat testing"}
CLOSED_STATUSES  = {
    "closed", "completed", "deployed", "ready for uat", "uat", "done",
    "complete", "finish", "finished", "signed off", "sign off",
    "cancelled", "canceled", "rejected", "deferred", "won't fix", "wontfix",
    "deployed to production", "prod deployed", "released", "archived",
    "ready for deployment", "ready to deploy",
    "ongoing",  # team development status — no agent comments needed
}

def _on(t: dict) -> list[str]:
    """Return list of owner names for a task."""
    raw = (t.get("details") or {}).get("owners") or t.get("owners") or t.get("owner") or []
    return [o.get("name") or o.get("full_name", "") for o in raw
            if isinstance(o, dict) and (o.get("name") or o.get("full_name"))]

def build_owner_tag(task: dict) -> str:
    """Build an @mention tag line for all task owners using Zoho Projects mention format."""
    raw = (task.get("details") or {}).get("owners") or task.get("owners") or task.get("owner") or []
    tags = []
    for o in raw:
        if not isinstance(o, dict):
            continue
        uid  = o.get("id") or o.get("user_id") or ""
        name = o.get("name") or o.get("full_name") or ""
        if uid and name:
            tags.append(f'<a href="javascript:void(0)">@{name}</a>')
        elif name:
            tags.append(f"@{name}")
    return " ".join(tags)

# ── Token refresh ─────────────────────────────────────────────────────────────

def refresh_access_token() -> str:
    import time
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "client_id":     ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SEC,
        "refresh_token": ZOHO_REFRESH,
    }).encode()
    req = urllib.request.Request(
        "https://accounts.zoho.in/oauth/v2/token", data=data, method="POST"
    )
    for attempt in range(1, 4):
        try:
            log.info("[Auth] Refreshing Zoho access token (attempt %d/3)...", attempt)
            with urllib.request.urlopen(req, timeout=15) as r:
                result = json.loads(r.read().decode())
            if "access_token" not in result:
                raise RuntimeError(f"Token refresh failed: {result}")
            token = result["access_token"]
            set_key(ENV_PATH, "ZOHO_ACCESS_TOKEN", token)
            log.info("[Auth] New token saved.")
            return token
        except Exception as e:
            log.warning("[Auth] Attempt %d failed: %s", attempt, e)
            if attempt < 3:
                log.info("[Auth] Retrying in 5 seconds...")
                time.sleep(5)
    log.error("[Auth] All 3 token refresh attempts failed. Check your internet connection.")
    sys.exit(1)

ZOHO_TOKEN   = refresh_access_token()
ZOHO_HEADERS = {"Authorization": f"Zoho-oauthtoken {ZOHO_TOKEN}"}

openai_client  = AsyncOpenAI(api_key=OPENAI_KEY)
claude_client  = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None
MODEL          = "gpt-4o-mini"
CLAUDE_MODEL   = "claude-opus-4-6"

# ── Zoho API helpers ──────────────────────────────────────────────────────────

async def zoho_get(client: httpx.AsyncClient, path: str) -> dict:
    r = await client.get(f"{PORTAL_BASE}{path}", headers=ZOHO_HEADERS)
    r.raise_for_status()
    return r.json()

async def zoho_post(client: httpx.AsyncClient, path: str, data: dict) -> dict:
    r = await client.post(f"{PORTAL_BASE}{path}", headers=ZOHO_HEADERS, data=data)
    r.raise_for_status()
    return r.json()

async def zoho_put(client: httpx.AsyncClient, path: str, data: dict) -> dict:
    r = await client.request("PUT", f"{PORTAL_BASE}{path}", headers=ZOHO_HEADERS, data=data)
    r.raise_for_status()
    return r.json()

# ── Portal users (for auto-assignment) ───────────────────────────────────────

_portal_users: list[dict] = []   # cached after first fetch

async def fetch_portal_users(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all portal users once; cache result."""
    global _portal_users
    if _portal_users:
        return _portal_users
    try:
        data = await zoho_get(client, "/users/?type=allusers")
        _portal_users = data.get("users", [])
        log.info("[Users] Loaded %d portal users", len(_portal_users))
    except Exception as e:
        log.warning("[Users] Could not fetch portal users: %s", e)
        _portal_users = []
    return _portal_users

def find_user_by_name(users: list[dict], name: str) -> dict | None:
    """Find a Zoho user whose display name contains `name` (case-insensitive)."""
    name_lower = name.strip().lower()
    for u in users:
        display = (u.get("name") or u.get("full_name") or "").lower()
        if name_lower in display or display in name_lower:
            return u
    return None

# ── Fetch ALL tasks ───────────────────────────────────────────────────────────

async def fetch_all_tasks(client: httpx.AsyncClient) -> list[dict]:
    """Fetch ALL tasks (open + closed) across all projects."""
    portal_data = await zoho_get(client, "/projects/")
    projects    = portal_data.get("projects", [])
    log.info("  Found %d project(s) in portal", len(projects))

    async def project_tasks(project: dict) -> list[dict]:
        pid  = project["id"]
        name = project.get("name", pid)
        collected: list[dict] = []
        index = 1
        page_size = 100
        try:
            while True:
                data  = await zoho_get(client, f"/projects/{pid}/tasks/?index={index}&range={page_size}")
                batch = data.get("tasks", [])
                for t in batch:
                    t.setdefault("project", {"id": pid, "name": name})
                collected.extend(batch)
                if len(batch) < page_size:
                    break
                index += page_size
            open_cnt   = sum(1 for t in collected
                             if t.get("status", {}).get("name", "").lower() not in CLOSED_STATUSES)
            closed_cnt = len(collected) - open_cnt
            log.info("  %s: %d task(s) total  (%d active, %d closed)",
                     name, len(collected), open_cnt, closed_cnt)
            return collected
        except Exception as e:
            log.warning("  %s: failed to fetch tasks — %s", name, e)
            return []

    results = await asyncio.gather(*[project_tasks(p) for p in projects])
    seen: set[str] = set()
    all_tasks: list[dict] = []
    for batch in results:
        for t in batch:
            tid = t.get("id", "")
            if tid not in seen:
                seen.add(tid)
                all_tasks.append(t)
    return all_tasks

# ── Comment helpers ───────────────────────────────────────────────────────────

def is_bot_comment(c: dict) -> bool:
    return BOT_MARKER in c.get("content", "")

def bot_already_commented_recently(comments: list[dict]) -> bool:
    for c in reversed(comments):
        if is_bot_comment(c):
            ts = c.get("created_time_long")
            if ts and hours_since_ms(ts) < BOT_COOLDOWN_HOURS:
                return True
            break
    return False

def last_bot_comment(comments: list[dict]) -> dict | None:
    for c in reversed(comments):
        if is_bot_comment(c):
            return c
    return None

def human_replies_after_bot(comments: list[dict]) -> list[dict]:
    """Return all non-bot comments posted AFTER the most recent bot comment."""
    bot_c = last_bot_comment(comments)
    if not bot_c:
        return []
    bot_ts = int(bot_c.get("created_time_long") or 0)
    return [
        c for c in comments
        if not is_bot_comment(c) and int(c.get("created_time_long") or 0) > bot_ts
    ]

def last_bot_commented_status(comments: list[dict]) -> str | None:
    """Return the task status that was active when the bot last commented (from embedded marker)."""
    bot_c = last_bot_comment(comments)
    if not bot_c:
        return None
    m = re.search(r'<!--zs:(.*?)-->', bot_c.get("content", ""))
    return m.group(1).strip().lower() if m else None

async def fetch_comments(client: httpx.AsyncClient, project_id: str, task_id: str) -> list[dict]:
    try:
        data = await zoho_get(client, f"/projects/{project_id}/tasks/{task_id}/comments/")
        return data.get("comments", [])
    except Exception:
        return []

async def post_comment(client: httpx.AsyncClient, project_id: str, task_id: str, content: str) -> bool:
    try:
        await zoho_post(client, f"/projects/{project_id}/tasks/{task_id}/comments/", {"content": content})
        log.info("[Zoho] Comment posted on task %s", task_id)
        return True
    except Exception as e:
        log.warning("[Zoho] Could not post comment on task %s — %s", task_id, e)
        return False

# ── Task auto-assignment ──────────────────────────────────────────────────────

async def assign_task(client: httpx.AsyncClient, project_id: str, task_id: str, user_id: str) -> bool:
    """Assign a task to the given Zoho user ID."""
    try:
        await zoho_put(client, f"/projects/{project_id}/tasks/{task_id}/",
                       {"person_responsible": user_id})
        log.info("[Zoho] Task %s assigned to user %s", task_id, user_id)
        return True
    except Exception as e:
        log.warning("[Zoho] Could not assign task %s — %s", task_id, e)
        return False

# ── HTML Comment Boxes ────────────────────────────────────────────────────────

PRIORITY_BADGE = {
    "high":   ('<span style="background:#ef4444;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">&#128308; HIGH PRIORITY</span>', "#ef4444"),
    "medium": ('<span style="background:#f59e0b;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">&#128992; MEDIUM PRIORITY</span>', "#f59e0b"),
    "low":    ('<span style="background:#22c55e;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">&#128994; LOW PRIORITY</span>', "#22c55e"),
    "none":   ("", None),
}

def html_box(label: str, icon: str, bg: str, border: str, body: str, priority: str = "none") -> str:
    body_html  = body.replace("\n", "<br>")
    badge_html, _ = PRIORITY_BADGE.get(priority.lower(), ("", None))
    if priority.lower() == "high":
        border = "#ef4444"
        bg     = "#fff5f5"
    priority_row = f'<div style="margin-bottom:8px">{badge_html}</div>' if badge_html else ""
    return (
        f'<div style="background:{bg};border-left:5px solid {border};'
        f'padding:14px 16px;border-radius:6px;font-family:Arial,sans-serif;'
        f'font-size:13px;margin:4px 0">'
        f'{priority_row}'
        f'<b style="color:{border};font-size:14px">{icon} {label}</b>'
        f'<hr style="border:none;border-top:1px solid {border};opacity:0.3;margin:8px 0">'
        f'{body_html}'
        f'<br><br><span style="color:#888;font-size:11px">{BOT_MARKER} {datetime.now().strftime("%Y-%m-%d %H:%M")}</span>'
        f'</div>'
    )

def box_new_task(text: str, priority: str = "none") -> str:
    return html_box("New Task — Onboarding Check-in", "&#128221;", "#f0fdf4", "#16a34a", text, priority)

def box_missing_info(text: str, priority: str = "none") -> str:
    return html_box("Check-in Required", "&#128269;", "#fff8e1", "#f59e0b", text, priority)

def box_analytics(text: str, priority: str = "none") -> str:
    return html_box("Task Analytics", "&#128202;", "#e0f2fe", "#0ea5e9", text, priority)

def box_replan(text: str, priority: str = "none") -> str:
    return html_box("Replan Suggestion", "&#128260;", "#fde8e8", "#ef4444", text, priority)

def box_digest(text: str, priority: str = "none") -> str:
    return html_box("Daily Digest", "&#128203;", "#f3e8ff", "#9333ea", text, priority)

def box_feedback_ack(text: str, priority: str = "none") -> str:
    return html_box("Update Acknowledged", "&#9989;", "#ecfdf5", "#10b981", text, priority)

def box_testing_check(text: str, priority: str = "none") -> str:
    return html_box("Testing Checklist", "&#128203;", "#f0f9ff", "#0284c7", text, priority)

def box_bpm_analysis(sections: dict, priority: str = "none") -> str:
    """Plain-text style BPM comment matching the team's preferred readable format."""
    cs           = sections.get("code_suggestion", {})
    team_status  = sections.get("team_status", "")
    blocker_note = sections.get("blocker_analysis", "")

    def bullets(items):
        return "".join(f"- {i}<br>" for i in items) if items else ""

    def indent_bullets(items, prefix="  - "):
        return "".join(f"{prefix}{i}<br>" for i in items) if items else ""

    pseudocode = cs.get("pseudocode", "").replace("\n", "<br>")
    pseudo_block = (
        f"  Pseudocode:<br>"
        f'  <span style="font-family:monospace;font-size:12px">'
        f'{pseudocode}</span><br>'
    ) if pseudocode else ""

    code_block = ""
    if cs:
        code_block = (
            f"<b>Code Suggestion:</b><br>"
            f"- Likely implementation area: {cs.get('area', '')}<br>"
            f"- Key processing steps:<br>"
            f"{indent_bullets(cs.get('steps', []))}"
            f"- Validation logic:<br>"
            f"  - {cs.get('validation', '')}<br>"
            f"- Error handling:<br>"
            f"  - {cs.get('error_handling', '')}<br>"
        )
        if cs.get("edge_cases"):
            code_block += "- Edge cases:<br>" + indent_bullets(cs.get("edge_cases", []))
        code_block += pseudo_block

    # Blocker / status banner shown only when team is stuck or overdue
    status_banner = ""
    if team_status in ("stuck", "overdue") and blocker_note:
        banner_color = "#dc2626" if team_status == "overdue" else "#b45309"
        banner_bg    = "#fff1f2" if team_status == "overdue" else "#fffbeb"
        label        = "&#9888; OVERDUE — ACTION NEEDED" if team_status == "overdue" else "&#128721; BLOCKER DETECTED"
        status_banner = (
            f'<div style="background:{banner_bg};border:1px solid {banner_color};'
            f'border-radius:4px;padding:8px 12px;margin-bottom:10px">'
            f'<b style="color:{banner_color}">{label}</b><br>'
            f'{blocker_note}'
            f'</div>'
        )

    ts   = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = (
        f"{status_banner}"

        f"<b>Task Understanding:</b><br>"
        f"{sections.get('task_understanding', '')}<br><br>"

        f"<b>{sections.get('logic_summary_label', 'Implementation / Logic Summary')}:</b><br>"
        f"{bullets(sections.get('logic_summary', []))}<br>"

        f"<b>Compact UAT Scenarios:</b><br>"
        f"{bullets(sections.get('uat_scenarios', []))}<br>"

        f"<b>Clarification Questions:</b><br>"
        f"{bullets(sections.get('clarification_questions', []))}<br>"

        f"{code_block}"
        f"<br><span style='color:#888;font-size:11px'>"
        f"{BOT_MARKER} {ts}</span>"
    )

    return (
        f'<div style="font-family:Arial,sans-serif;font-size:13px;'
        f'border-left:4px solid #0369a1;padding:12px 16px;margin:4px 0;'
        f'background:#f8fafc;border-radius:4px">'
        f'{body}'
        f'</div>'
    )

def is_bpm_task(task: dict) -> bool:
    """Return True if the task name, tasklist, or description contains BPM-related keywords."""
    haystack = " ".join([
        task.get("name", ""),
        task.get("tasklist", {}).get("name", ""),
        re.sub(r'<[^>]+>', ' ', task.get("description", "")),
    ]).lower()
    return any(kw in haystack for kw in BPM_KEYWORDS)

COMMENT_TYPE_BOX = {
    "new_task":       box_new_task,
    "missing_info":   box_missing_info,
    "analytics":      box_analytics,
    "replan":         box_replan,
    "digest":         box_digest,
    "feedback_ack":   box_feedback_ack,
    "testing_check":  box_testing_check,
    "bpm_analysis":   None,   # handled separately via Claude — not via box_fn
}

# ── Utilities ─────────────────────────────────────────────────────────────────

def hours_since_ms(ts_ms) -> float:
    if not ts_ms:
        return 9999.0
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return 9999.0

def days_since(ts_ms) -> float:
    return hours_since_ms(ts_ms) / 24

def is_overdue(task: dict) -> bool:
    end_ms = task.get("end_date_long")
    if not end_ms:
        return False
    try:
        return int(end_ms) < datetime.now(timezone.utc).timestamp() * 1000
    except Exception:
        return False

def has_keyword(text: str) -> list[str]:
    lower = text.lower()
    return [k for k in KEYWORDS if k in lower]

def task_owner_names(task: dict) -> list[str]:
    owners_raw = (task.get("details") or {}).get("owners") \
                 or task.get("owners") or task.get("owner") or []
    return [
        o.get("name") or o.get("full_name", "")
        for o in owners_raw
        if isinstance(o, dict) and (o.get("name") or o.get("full_name"))
    ]

def _comment_author(c: dict) -> str:
    for key in ("added_by", "posted_by", "added_person"):
        v = c.get(key)
        if isinstance(v, dict):
            return v.get("name", "?")
        if isinstance(v, str) and v:
            return v
    return "?"

def summarise_comments(comments: list[dict], limit: int = 5) -> str:
    if not comments:
        return "No comments yet."
    recent = comments[-limit:]
    return "\n".join(
        f"- [{c.get('created_time_format', c.get('created_time', '?'))}] "
        f"{_comment_author(c)}: "
        f"{re.sub('<[^>]+>', '', c.get('content', ''))[:200]}"
        for c in recent
    )

def summarise_human_replies(replies: list[dict]) -> str:
    if not replies:
        return ""
    return "\n".join(
        f"- {_comment_author(c)}: {re.sub('<[^>]+>', '', c.get('content', ''))[:300]}"
        for c in replies
    )

# ── Smart email routing ───────────────────────────────────────────────────────

def route_escalation_recipients(owners: list[str]) -> list[str]:
    """
    Map task owners to Siva or Dhinesh based on team membership in .env.
    Falls back to both if owner is not in either team, or teams are not configured.
    """
    if not owners or (not SIVA_TEAM and not DHINESH_TEAM):
        # No team mapping configured → send to both
        return [e for e in [SIVA_EMAIL, DHINESH_EMAIL] if e]

    recipients: set[str] = set()
    for owner_name in owners:
        owner_lower = owner_name.lower()
        in_siva    = any(t in owner_lower or owner_lower in t for t in SIVA_TEAM)
        in_dhinesh = any(t in owner_lower or owner_lower in t for t in DHINESH_TEAM)
        if in_siva and SIVA_EMAIL:
            recipients.add(SIVA_EMAIL)
        if in_dhinesh and DHINESH_EMAIL:
            recipients.add(DHINESH_EMAIL)
        if not in_siva and not in_dhinesh:
            # Owner not mapped → alert both
            if SIVA_EMAIL:
                recipients.add(SIVA_EMAIL)
            if DHINESH_EMAIL:
                recipients.add(DHINESH_EMAIL)

    return list(recipients) or [e for e in [SIVA_EMAIL, DHINESH_EMAIL] if e]

# ── Comment type determination  (with feedback loop) ─────────────────────────

def determine_comment_type(
    task: dict,
    comments: list[dict],
    days_in_progress: float,
    keywords: list[str],
    human_replied: bool,
    hours_since_last_bot: float,
) -> str | None:
    """
    Priority order:
    0. BPM task        — always post bpm_analysis (once per status cycle, via Claude)
    1. Testing status  — always post testing_check (once per entry into testing)
    2. Feedback loop   — if human replied to bot's question → acknowledge + analyse
    3. Feedback loop   — if bot asked question and no reply for NO_REPLY_HOURS → replan/escalate
    4. Same-status skip — if bot already commented for this exact status and no human reply → skip
    5. Status/priority matrix

    Returns None to signal "skip this task entirely".
    """
    priority = (task.get("priority") or "none").lower()
    status   = (task.get("status", {}).get("name") or "").lower()
    overdue  = is_overdue(task)
    bot_c    = last_bot_comment(comments)
    bot_type = ""
    if bot_c:
        content = re.sub('<[^>]+>', '', bot_c.get("content", "")).lower()
        if "onboarding check-in" in content or "new task" in content:
            bot_type = "new_task"
        elif "check-in required" in content or "missing" in content:
            bot_type = "missing_info"

    # ── BPM task: post ERP/BPM analysis once per status cycle via Claude ────────
    if is_bpm_task(task) and status not in CLOSED_STATUSES:
        last_status = last_bot_commented_status(comments)
        if last_status == status and not human_replied:
            return None   # already analysed in this status cycle
        return "bpm_analysis"

    # ── Testing status: post checklist comment once per testing cycle ─────────
    if status in TESTING_STATUSES:
        last_status = last_bot_commented_status(comments)
        if last_status in TESTING_STATUSES and not human_replied:
            return None   # already checked during this testing cycle
        return "testing_check"

    # ── Feedback loop: human replied ─────────────────────────────────────────
    if human_replied:
        if keywords:
            return "replan"
        return "feedback_ack"

    # ── Feedback loop: bot asked a question, no reply for too long ────────────
    if bot_type in ("new_task", "missing_info") and not human_replied:
        if hours_since_last_bot >= NO_REPLY_HOURS:
            return "replan"

    # ── Closed tasks should never reach here, but guard anyway ───────────────
    if status in CLOSED_STATUSES:
        return None

    # ── Same-status skip for in-progress tasks ────────────────────────────────
    # Once the bot has commented for a given status, don't repeat until status changes
    # Exception: always re-comment if overdue, blocked keywords, or no-reply escalation
    last_status = last_bot_commented_status(comments)
    if (last_status and last_status == status
            and not overdue and not keywords
            and hours_since_last_bot < NO_REPLY_HOURS):
        return None   # already commented for this status, nothing new to say

    # ── Check what's actually missing on this task ───────────────────────────
    owners_list = task_owner_names(task)
    has_owner   = bool(owners_list)
    has_due     = bool(task.get("end_date", ""))
    has_desc    = len(re.sub(r'<[^>]+>', '', task.get("description", "")).strip()) >= 10

    # ── Open tasks: only comment once (new_task) then wait for status change ──
    if status in OPEN_STATUSES:
        if not comments:
            return "new_task"
        if not has_owner or not has_due or not has_desc:
            return "missing_info"
        return None   # open but not yet picked up — don't nag

    # ── On hold ──────────────────────────────────────────────────────────────
    if "on hold" in status or "hold" in status:
        return "replan"

    # ── Active task — no comments yet ────────────────────────────────────────
    if not comments:
        if not has_owner or not has_due:
            return "missing_info"
        return "new_task"

    # ── Priority matrix ───────────────────────────────────────────────────────
    if priority == "high":
        if overdue or keywords:
            return "replan"
        if not has_owner or not has_due:
            return "missing_info"
        return "analytics"

    if priority == "medium":
        if overdue or days_in_progress >= ESCALATION_DAYS:
            return "replan"
        if not has_owner or not has_due:
            return "missing_info"
        return "analytics"

    # low / none priority
    if not has_owner or not has_due:
        return "missing_info"
    return "digest"

# ── AI Agent ──────────────────────────────────────────────────────────────────

ANALYST_SYSTEM = """You are a senior project manager reviewing a specific Zoho Projects task.
You receive a real task snapshot as JSON. Write a comment that is 100% specific to THIS task —
never use placeholder text, never ask questions that are already answered in the snapshot.

Return EXACTLY this JSON (no markdown, no code fences):
{
  "comment_text":    "...",
  "escalate":        true|false,
  "escalate_reason": "...",
  "summary":         "one-line task health summary (max 120 chars)"
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMMENT TYPE INSTRUCTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"new_task"
  This task just appeared with no activity. Greet the task by its actual NAME.
  Look at the snapshot and identify ONLY what is genuinely missing:
  - If owner is empty → ask who will own this task
  - If due_date is empty → ask for a target completion date
  - If description is empty or vague → ask for a brief scope/goal description
  - If start_date is empty → ask when work will begin
  - Always ask: what are the known blockers or dependencies before starting?
  Do NOT ask about things already present in the snapshot.
  Keep tone friendly and welcoming.

"missing_info"
  The task has been open/active but key context is missing or it appears stalled.
  Based on the snapshot, ask ONLY about what is genuinely unclear:
  - Reference the actual task status (e.g. "In Progress since X days")
  - If no owner → ask who is responsible
  - If no due date → ask for an ETA
  - If comments exist but no recent update → ask for a current status update
  - If last update was >24h ago → ask what the current blocker is
  Be specific, professional, and concise. Maximum 3 targeted questions.

"feedback_ack"
  The team replied to the bot's previous question. The reply text is in "human_replies_to_bot".
  REQUIRED: Start by quoting or paraphrasing what they actually said.
  Then:
  - Confirm you understood their update
  - Summarise the current state of the task based on their reply
  - List 2–3 concrete next steps with realistic timeframes
  - If their reply mentions a blocker or risk → flag it explicitly
  Never write generic text. Every sentence must reference their actual reply.

"analytics"
  Task is progressing. Write a health report using actual numbers from the snapshot:
  - State the task name, current status, and % complete
  - Note how many days it has been in progress
  - If overdue → state exactly how many days overdue
  - Summarise recent comment activity (what was discussed, by whom)
  - List 2–3 specific next steps with owner names from the snapshot
  - Flag any risks if progress % seems low relative to days elapsed

"replan"
  Something is wrong. Be direct. Reference the SPECIFIC issue:
  - If is_overdue=true → state the due date and how many days overdue
  - If keywords_found is non-empty → quote the exact keywords found (e.g. "blocked", "stuck")
  - If no_reply_escalation=true → state that the bot asked a question X hours ago and no one replied
  - If on hold → ask for a clear restart date
  Then suggest 2–3 concrete replanning actions with a proposed new timeline.
  Do NOT be vague. Every issue must be named explicitly.

"digest"
  Brief daily nudge. State:
  - The task name and current status
  - How many hours since the last update
  - One gentle reminder to post a status update today
  Keep it short — 3–4 sentences maximum.

"testing_check"
  The task has just entered Testing / For Testing / QA. Post a structured checklist reminder:
  1. Hours logged — ask the developer to confirm build time has been logged against this task.
  2. Required fields — ask the tester to confirm these are filled in: New Customisation Layer (if applicable), Menu/Module name, Client, Priority, Criticality.
  3. Evidence — remind the tester to attach screenshots of ALL test scenarios (pass and fail).
  4. Test steps — ask them to document the exact steps taken so the result can be reproduced.
  5. Sign-off — ask: "Is this ready to move to UAT, or are there items still failing?"
  Format as a numbered checklist. Address the task owner by name if available.
  Tone: professional and thorough — this is a quality gate, not a nudge.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRIORITY TONE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- HIGH:      Open with "🔴 ACTION REQUIRED:" — be direct, time-sensitive, no pleasantries
- MEDIUM:    Professional and clear, include specific next steps with dates
- LOW/NONE:  Friendly and informational, gentle nudge style

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ESCALATION RULES — set escalate=true when ANY of:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  - keywords_found is non-empty (blocked, urgent, stuck, etc.)
  - days_in_progress >= escalation_days
  - priority="high" AND is_overdue=true
  - no_reply_escalation=true
Always set escalate_reason to a specific one-line reason, or "" if escalate=false.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Never use placeholder text like [owner name] or [date]
- Never ask questions that are already answered in the snapshot data
- Always use the actual task name in your comment
- Keep comment_text under 400 words
- Return valid JSON only — no markdown, no extra text"""


BPM_SYSTEM_PROMPT = """You are an ERP technical-functional analyst specialising in Epicor ERP BPM, BAQ, and customisation development.

Analyse the given Zoho Projects task and generate a compact structured response for use as a project comment.
Read the full task description AND all previous human comments before writing anything.

RULES:
1. Be 100% specific to THIS task — no generic boilerplate.
2. Do NOT re-ask questions already answered in previous comments.
3. If the team is stuck or blocked, prioritise concrete solutions in code_suggestion.
4. Always include code_suggestion — even for early-stage tasks provide probable area and pseudocode.
5. Keep everything compact and practical.
6. Write for a FUNCTIONAL TEAM — avoid raw developer jargon in task_understanding and blocker_analysis. Plain English only.

STATUS-BASED BEHAVIOUR:
- If task status is Testing / For Testing / QA / In Testing:
  * logic_summary_label must be "Testing Verification Points" (not Implementation)
  * logic_summary bullets must describe WHAT THE FUNCTIONAL TEAM SHOULD VERIFY, not how it was built
  * Do NOT describe implementation steps in logic_summary when task is in testing
  * code_suggestion is still included — for reference only, not as action items
  * Audience for this comment is the functional/QA team, not developers

- If task status is In Progress / Open / On Hold:
  * logic_summary_label must be "Implementation / Logic Summary"
  * logic_summary bullets must describe the implementation logic steps

CLARIFICATION QUESTIONS RULES — never ask these:
- Do NOT ask about Classic vs Kinetic compatibility unless task description specifically raises it
- Do NOT ask who is responsible for UAT sign-off
- Do NOT ask client-specific questions (which client, which user, which department)
- Do NOT re-ask anything already mentioned or answered in previous comments
- Only ask questions that genuinely block progress on THIS task right now

BLOCKER ANALYSIS RULES:
- Write in plain English for a functional team manager reading it
- Do NOT name specific developers from the comments (e.g. do not say "Siva has not responded")
- Say "the development team" or "the assigned developer" instead
- Focus on WHAT action is needed and by WHEN, not who failed to respond
- Keep it to 3-4 sentences maximum

Your response must be exactly this JSON (no markdown, no code fences, no extra keys):
{
  "task_understanding": "1-2 lines in plain English for a functional team member — what this task does, current state, and what needs to happen next",
  "logic_summary_label": "Implementation / Logic Summary  OR  Testing Verification Points — based on task status",
  "logic_summary": [
    "bullet specific to this task — implementation step OR testing verification point depending on status",
    "... 5-8 bullets total"
  ],
  "uat_scenarios": [
    "compact scenario -> expected result  (e.g. '106 received on PO 100 -> block')",
    "... exactly 5 scenarios — pick only the most critical ones that cover the core rule, one boundary case, and one rejection/block case"
  ],
  "clarification_questions": [
    "Question that is NOT already answered and NOT about Classic/UAT-signoff/client specifics?",
    "... 5-7 questions total"
  ],
  "team_status": "on_track | needs_input | stuck | overdue",
  "blocker_analysis": "if stuck or overdue: plain-English diagnosis and recommended actions for the functional team. No developer names. Max 3-4 sentences.",
  "code_suggestion": {
    "area": "probable module/event e.g. ReceivingEntry.Update pre-processing BPM",
    "steps": [
      "implementation step 1",
      "... key processing steps"
    ],
    "validation": "core validation logic in plain English",
    "error_handling": "error message and how it is shown to the user",
    "edge_cases": ["edge case 1", "edge case 2", "..."],
    "pseudocode": "8-15 line pseudocode, use \\n for line breaks"
  },
  "escalate": false,
  "escalate_reason": "",
  "summary": "one-line task health summary (max 120 chars)"
}

UAT scenario format rules:
- Exactly 5 scenarios — no more, no less
- One compact line per scenario using ->  e.g. "106 received on PO 100 -> block"
- Use real values and field names from the task description where possible
- Pick the 5 most important: 1 happy path, 1 boundary value, 1 block/reject, 1 edge case, 1 integration or update scenario
- No explanatory sentences — just condition -> result

Return valid JSON only — no markdown, no extra text."""


def _bpm_format_comments(comments: list[dict]) -> str:
    """Format ALL comments for BPM analysis, separating human vs bot comments."""
    if not comments:
        return "No comments yet."
    lines = []
    for c in comments:
        author = _comment_author(c)
        ts     = c.get("created_time_format", c.get("created_time", "?"))
        raw    = re.sub(r'<[^>]+>', ' ', c.get("content", "")).strip()
        # truncate very long comments but keep enough context
        text   = raw[:400] if len(raw) > 400 else raw
        is_bot = BOT_MARKER in raw
        label  = "[BOT]" if is_bot else "[HUMAN]"
        lines.append(f"{label} [{ts}] {author}: {text}")
    return "\n".join(lines)


def _bpm_detect_blockers(comments: list[dict]) -> dict:
    """Scan human comments for stuck/blocked/error signals."""
    stuck_keywords = [
        "stuck", "blocked", "not working", "error", "issue", "problem",
        "failed", "failing", "cannot", "can't", "doesn't work", "not sure",
        "help", "doubt", "confused", "unclear",
    ]
    human_comments = [
        c for c in comments
        if BOT_MARKER not in re.sub(r'<[^>]+>', '', c.get("content", ""))
    ]
    is_stuck   = False
    found_kws  = []
    last_human = ""
    if human_comments:
        last_c     = human_comments[-1]
        last_human = re.sub(r'<[^>]+>', ' ', last_c.get("content", "")).strip()[:300]
        # scan all human comments for stuck signals
        all_human_text = " ".join(
            re.sub(r'<[^>]+>', ' ', c.get("content", "")).lower()
            for c in human_comments
        )
        found_kws = [kw for kw in stuck_keywords if kw in all_human_text]
        is_stuck  = bool(found_kws)
    return {
        "is_stuck":       is_stuck,
        "stuck_keywords": found_kws[:5],
        "last_human_comment": last_human,
        "human_comment_count": len(human_comments),
    }


async def analyse_bpm_task(
    task: dict,
    comments: list[dict],
    days_in_progress: float,
) -> dict:
    """Call Claude Opus 4.6 to analyse a BPM task and return structured sections."""
    if not claude_client:
        log.warning("[BPM] ANTHROPIC_API_KEY not set — skipping BPM analysis")
        return {
            "task_understanding": "BPM analysis unavailable — ANTHROPIC_API_KEY not configured.",
            "logic_summary": [], "uat_scenarios": [], "clarification_questions": [],
            "code_suggestion": {}, "escalate": False, "escalate_reason": "",
            "summary": "BPM task — no Claude key",
        }

    desc_plain    = re.sub(r'<[^>]+>', ' ', task.get("description", "")).strip()
    owners        = task_owner_names(task)
    blocker_info  = _bpm_detect_blockers(comments)
    all_comments  = _bpm_format_comments(comments)  # ALL comments, not just last 5

    snapshot = {
        "task_name":              task.get("name"),
        "project":                task.get("project", {}).get("name", "?"),
        "tasklist":               task.get("tasklist", {}).get("name", "?"),
        "status":                 task.get("status", {}).get("name", "Unknown"),
        "priority":               task.get("priority", "None") or "None",
        "owners":                 owners,
        "due_date":               task.get("end_date", ""),
        "description":            desc_plain[:1500] if desc_plain else "",
        "percent_complete":       task.get("percent_complete", "0"),
        "days_in_progress":       round(days_in_progress, 1),
        "is_overdue":             is_overdue(task),
        "team_is_stuck":          blocker_info["is_stuck"],
        "stuck_signals_detected": blocker_info["stuck_keywords"],
        "last_human_comment":     blocker_info["last_human_comment"],
        "all_comments":           all_comments,   # full comment history
    }

    try:
        async with claude_client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=3000,
            thinking={"type": "adaptive"},
            system=BPM_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(snapshot, indent=2)}],
        ) as stream:
            final = await stream.get_final_message()

        text = next((b.text for b in final.content if b.type == "text"), "")
        text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("```").strip()
        result = json.loads(text)
        return result
    except Exception as e:
        log.warning("[BPM] Claude analysis failed: %s", e)
        return {
            "task_understanding": f"BPM analysis failed: {e}",
            "logic_summary": [], "uat_scenarios": [], "clarification_questions": [],
            "code_suggestion": {}, "escalate": False, "escalate_reason": "",
            "summary": "BPM task — Claude error",
        }


async def analyse_task(
    task: dict,
    comments: list[dict],
    human_replies: list[dict],
    days_in_progress: float,
    comment_type: str,
    no_reply_escalation: bool,
) -> dict:
    desc_plain     = re.sub('<[^>]+>', ' ', task.get("description", "")).strip()
    keywords_found = has_keyword(task.get("name", "") + " " + desc_plain)
    for c in comments:
        keywords_found += has_keyword(re.sub('<[^>]+>', '', c.get("content", "")))
    keywords_found = list(set(keywords_found))

    last_comment_ts = comments[-1].get("created_time_long") if comments else None
    priority        = task.get("priority", "None") or "None"
    overdue         = is_overdue(task)

    owners         = task_owner_names(task)
    due_date       = task.get("end_date", "")
    start_date     = task.get("start_date", "")
    missing_fields = []
    if not owners:
        missing_fields.append("owner")
    if not due_date:
        missing_fields.append("due_date")
    if not desc_plain or len(desc_plain) < 10:
        missing_fields.append("description")
    if not start_date:
        missing_fields.append("start_date")

    # Extract last bot comment text so AI knows what was previously asked
    bot_c = last_bot_comment(comments)
    last_bot_text = ""
    if bot_c:
        last_bot_text = re.sub(r'<[^>]+>', '', bot_c.get("content", ""))[:400].strip()

    snapshot = {
        "comment_type":          comment_type,
        "no_reply_escalation":   no_reply_escalation,
        "task_id":               task.get("id"),
        "name":                  task.get("name"),
        "project":               task.get("project", {}).get("name", "?"),
        "tasklist":              task.get("tasklist", {}).get("name", "?"),
        "status":                task.get("status", {}).get("name", "Unknown"),
        "priority":              priority,
        "percent_complete":      task.get("percent_complete", "0"),
        "owners":                owners,
        "due_date":              due_date,
        "start_date":            start_date,
        "missing_fields":        missing_fields,
        "is_overdue":            overdue,
        "days_overdue":          round(days_since(task.get("end_date_long")), 1) if overdue else 0,
        "days_in_progress":      round(days_in_progress, 1),
        "hours_since_update":    round(hours_since_ms(task.get("last_updated_time_long")), 1),
        "no_comments_yet":       not bool(comments),
        "escalation_days":       ESCALATION_DAYS,
        "inactivity_hours":      round(hours_since_ms(last_comment_ts), 1),
        "keywords_found":        keywords_found,
        "description_summary":   desc_plain[:400] if desc_plain else "",
        "recent_comments":       summarise_comments(comments, limit=6),
        "last_bot_question":     last_bot_text,
        "human_replies_to_bot":  summarise_human_replies(human_replies),
    }

    response = await openai_client.chat.completions.create(
        model=MODEL,
        max_tokens=1200,
        messages=[
            {"role": "system", "content": ANALYST_SYSTEM},
            {"role": "user",   "content": json.dumps(snapshot, indent=2)},
        ],
    )

    text = response.choices[0].message.content.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("```").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "comment_text":    text[:300],
            "escalate":        False,
            "escalate_reason": "",
            "summary":         text[:120],
        }

# ── Escalation email ──────────────────────────────────────────────────────────

def send_escalation_email(
    task: dict,
    reason: str,
    days: float,
    keywords: list[str],
    owners: list[str],
) -> None:
    if not EMAIL_ENABLED:
        log.info("[Email] Escalation skipped (not configured) — %s", reason[:80])
        return

    recipients = route_escalation_recipients(owners)
    owner_str  = ", ".join(owners) or "Unassigned"
    project    = task.get("project", {}).get("name", "?")
    subject    = f"[MithilAI Agent] Escalation: {task.get('name', 'Task')} — Action Required"

    # Indicate which team this is routed to
    routed_to = []
    if SIVA_EMAIL in recipients:
        routed_to.append("Siva")
    if DHINESH_EMAIL in recipients:
        routed_to.append("Dhinesh")
    routed_str = " & ".join(routed_to) or "Team"

    body = f"""
<html><body style="font-family:sans-serif;max-width:640px">
<div style="background:#c0392b;color:#fff;padding:16px 24px;border-radius:8px 8px 0 0">
  <h2 style="margin:0">&#128680; Task Escalation Alert — {routed_str}</h2>
</div>
<table style="border-collapse:collapse;width:100%;border:1px solid #e5e7eb">
  <tr><td style="padding:8px 12px;font-weight:bold;background:#f5f5f5;width:180px">Project</td>
      <td style="padding:8px 12px">{project}</td></tr>
  <tr><td style="padding:8px 12px;font-weight:bold;background:#f5f5f5">Task</td>
      <td style="padding:8px 12px">{task.get('name')}</td></tr>
  <tr><td style="padding:8px 12px;font-weight:bold;background:#f5f5f5">Status</td>
      <td style="padding:8px 12px">{task.get('status',{}).get('name','?')}</td></tr>
  <tr><td style="padding:8px 12px;font-weight:bold;background:#f5f5f5">Owner(s)</td>
      <td style="padding:8px 12px">{owner_str}</td></tr>
  <tr><td style="padding:8px 12px;font-weight:bold;background:#f5f5f5">Days in Progress</td>
      <td style="padding:8px 12px">{round(days, 1)}</td></tr>
  <tr><td style="padding:8px 12px;font-weight:bold;background:#f5f5f5">Priority</td>
      <td style="padding:8px 12px">{task.get('priority','None')}</td></tr>
  <tr><td style="padding:8px 12px;font-weight:bold;background:#f5f5f5">Keywords Found</td>
      <td style="padding:8px 12px">{", ".join(keywords) if keywords else "N/A"}</td></tr>
  <tr><td style="padding:8px 12px;font-weight:bold;background:#fef2f2">Escalation Reason</td>
      <td style="padding:8px 12px;color:#c0392b"><b>{reason}</b></td></tr>
</table>
<p style="margin-top:16px;color:#7f8c8d;font-size:12px">
  Routed to: <b>{routed_str}</b> based on task ownership.<br>
  Sent by MithilAI Agent — please review and take action in Zoho Projects.
</p>
</body></html>"""

    # Build To: escalation goes to routed recipients only (no CC)
    esc_to   = recipients
    all_rcpt = esc_to

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = ", ".join(esc_to)
    msg.attach(MIMEText(body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, all_rcpt, msg.as_string())
        log.info("[Email] Escalation sent → To:%s", ", ".join(esc_to))
    except Exception as e:
        log.error("[Email] Escalation failed: %s", e)

# ── Unassigned task: find best candidate & send email ────────────────────

def _task_keywords(task: dict) -> set[str]:
    """Extract significant words from task name + description."""
    text = task.get("name", "") + " " + re.sub(r"<[^>]+>", " ", task.get("description", ""))
    words = re.findall(r"[a-zA-Z]{4,}", text.lower())
    stopwords = {"task", "this", "that", "with", "from", "have", "will", "been",
                 "should", "please", "need", "make", "into", "also", "when", "then"}
    return {w for w in words if w not in stopwords}


def find_best_candidate(
    unassigned_task: dict,
    all_tasks: list[dict],
    users: list[dict],
) -> tuple[str, str] | tuple[None, None]:
    """
    Look at all closed/completed tasks and count keyword overlap per owner.
    Returns (owner_name, owner_email) of the best match, or (None, None).
    """
    target_kw = _task_keywords(unassigned_task)
    if not target_kw:
        return None, None

    score: dict[str, int] = {}
    for t in all_tasks:
        if t.get("status", {}).get("name", "").lower() not in CLOSED_STATUSES:
            continue
        owners = _on(t)
        if not owners or owners == ["Unassigned"]:
            continue
        overlap = len(target_kw & _task_keywords(t))
        if overlap > 0:
            for o in owners:
                score[o] = score.get(o, 0) + overlap

    if not score:
        return None, None

    best_name = max(score, key=lambda n: score[n])
    user_obj  = find_user_by_name(users, best_name)
    email     = (user_obj or {}).get("email", "")
    return best_name, email


def send_unassigned_task_email(
    task: dict,
    candidate_name: str,
    candidate_email: str,
    score_summary: str,
) -> None:
    if not EMAIL_ENABLED:
        log.info("[Email] Skipping unassigned email — email not configured.")
        return
    if not candidate_email:
        log.warning("[Email] No email found for candidate '%s' — skipping.", candidate_name)
        return

    task_name   = task.get("name", "Unnamed Task")
    project_nm  = task.get("project", {}).get("name", "Unknown Project")
    project_id  = task.get("project", {}).get("id", "")
    task_id     = task.get("id", "")
    zoho_url    = (f"https://projects.zoho.in/portal/{ZOHO_PORTAL}"
                   f"/project/{project_id}/task/{task_id}/")
    description = re.sub(r"<[^>]+>", " ", task.get("description", "")).strip()[:300] or "No description provided."
    end_date    = task.get("end_date", "—")
    priority    = task.get("priority", "None") or "None"
    first_name  = candidate_name.split()[0]
    subject     = f"[Action Required] Unassigned Task — Could you take this on? | {task_name}"

    body = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;color:#1e293b;max-width:600px;margin:auto">
<div style="background:#1e293b;padding:18px 24px;border-radius:8px 8px 0 0">
  <h2 style="color:#fff;margin:0;font-size:18px">Unassigned Task — Your Expertise Needed</h2>
  <p style="color:#94a3b8;margin:4px 0 0;font-size:12px">MithilAI Agent</p>
</div>
<div style="border:1px solid #e2e8f0;border-top:none;padding:24px;border-radius:0 0 8px 8px">
  <p style="margin:0 0 16px">Hi <b>{first_name}</b>,</p>
  <p style="margin:0 0 16px">
    The following task in <b>{project_nm}</b> is currently
    <span style="background:#fef2f2;color:#ef4444;padding:2px 8px;border-radius:4px;font-weight:600">Unassigned</span>.
    Based on your past work on similar tasks, you appear to be the best person to take it on.
  </p>
  <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin:0 0 20px">
    <table style="width:100%;font-size:13px;border-collapse:collapse">
      <tr><td style="padding:4px 0;color:#64748b;width:110px">Task</td>
          <td style="padding:4px 0;font-weight:700">{task_name}</td></tr>
      <tr><td style="padding:4px 0;color:#64748b">Project</td>
          <td style="padding:4px 0">{project_nm}</td></tr>
      <tr><td style="padding:4px 0;color:#64748b">Priority</td>
          <td style="padding:4px 0">{priority}</td></tr>
      <tr><td style="padding:4px 0;color:#64748b">Due Date</td>
          <td style="padding:4px 0">{end_date}</td></tr>
      <tr><td style="padding:4px 0;color:#64748b;vertical-align:top">Description</td>
          <td style="padding:4px 0;color:#475569">{description}</td></tr>
    </table>
  </div>
  <p style="margin:0 0 8px;color:#475569;font-size:13px"><b>Why you?</b> {score_summary}</p>
  <div style="margin:20px 0;text-align:center">
    <a href="{zoho_url}" target="_blank"
       style="display:inline-block;background:#0ea5e9;color:#fff;padding:10px 24px;
              border-radius:6px;text-decoration:none;font-weight:700;font-size:14px">
      View Task in Zoho &#8599;
    </a>
  </div>
  <p style="margin:16px 0 0;font-size:12px;color:#94a3b8">
    Please reply to this email or add a comment in Zoho to confirm if you can take this task,
    or suggest someone else who might be a better fit.
  </p>
  <p style="margin:8px 0 0;font-size:12px;color:#94a3b8">— MithilAI Agent (automated)</p>
</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = candidate_email
    msg.attach(MIMEText(body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, [candidate_email], msg.as_string())
        log.info("[Email] Unassigned task email sent to %s (%s) for '%s'",
                 candidate_name, candidate_email, task_name)
    except Exception as e:
        log.error("[Email] Unassigned task email failed for '%s': %s", task_name, e)


async def handle_unassigned_tasks(all_tasks: list[dict], users: list[dict]) -> None:
    """Find all unassigned open tasks and email the best candidate for each."""
    unassigned = [
        t for t in all_tasks
        if (not _on(t) or _on(t) == ["Unassigned"])
        and t.get("status", {}).get("name", "").lower() not in CLOSED_STATUSES
    ]
    if not unassigned:
        log.info("[Unassigned] No unassigned open tasks found.")
        return

    log.info("[Unassigned] %d unassigned open task(s) found — finding candidates...", len(unassigned))
    for task in unassigned:
        name, email = find_best_candidate(task, all_tasks, users)
        task_name   = task.get("name", "?")
        if not name:
            log.info("[Unassigned] '%s' — no candidate found (no similar closed tasks).", task_name)
            continue
        target_kw     = _task_keywords(task)
        score_summary = (
            f"You have previously completed similar tasks involving: "
            f"{', '.join(list(target_kw)[:6])}."
        )
        log.info("[Unassigned] '%s' → candidate: %s (%s)", task_name, name, email or "no email")
        send_unassigned_task_email(task, name, email, score_summary)


# ── Daily report email ────────────────────────────────────────────────────────

ACTION_LABEL = {
    "new_task":          ("New Task Check-in",       "#8b5cf6"),
    "missing_info":      ("Follow-up Required",      "#f59e0b"),
    "feedback_ack":      ("Reply Acknowledged",      "#10b981"),
    "analytics":         ("Analytics Posted",        "#0ea5e9"),
    "replan":            ("Replan Suggested",        "#ef4444"),
    "digest":            ("Daily Digest Posted",     "#6b7280"),
    "auto_assigned":     ("Auto-Assigned",           "#7c3aed"),
    "skipped_cooldown":  ("Cooldown — Skipped",      "#d1d5db"),
    "skipped_closed":    ("Closed — No Comment",     "#d1d5db"),
    "comment_failed":    ("Comment Failed",          "#ef4444"),
    "escalation_email":  ("Escalation Email Sent",   "#dc2626"),
}

def _action_pill(action: str) -> str:
    label, color = ACTION_LABEL.get(action, (action, "#6b7280"))
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:10px;'
        f'font-size:11px;font-weight:600;white-space:nowrap">{label}</span>'
    )

def _priority_color(priority: str) -> str:
    return {"high": "#ef4444", "medium": "#f59e0b", "low": "#22c55e"}.get(priority.lower(), "#6b7280")


async def build_per_person_html(tasks: list[dict], results: list[dict], run_time: str) -> str:
    """Fetch comments for active tasks and build per-person HTML for the daily email."""
    CLOSED = CLOSED_STATUSES
    active_tasks = [t for t in tasks
                    if t.get("status", {}).get("name", "").lower() not in CLOSED]
    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient(timeout=30) as client:
        async def _get(task):
            async with sem:
                pid = task.get("project", {}).get("id", "")
                tid = task["id"]
                try:
                    data = await zoho_get(client,
                        f"/projects/{pid}/tasks/{tid}/comments/")
                    return tid, data.get("comments", [])
                except Exception:
                    return tid, []
        pairs = await asyncio.gather(*[_get(t) for t in active_tasks])
    cmap = dict(pairs)
    result_by_id = {r["task_id"]: r for r in results}

    def _s(x): return re.sub(r"<[^>]+>", "", x or "").strip()
    def _ca(c):
        for k in ("added_by", "posted_by", "added_person"):
            v = c.get(k)
            if isinstance(v, dict): return v.get("name", "?")
            if isinstance(v, str) and v: return v
        return "?"

    by_owner: dict[str, list] = {}
    for task in tasks:
        names     = _on(task) or ["Unassigned"]
        status_nm = task.get("status", {}).get("name", "?")
        priority  = task.get("priority", "None") or "None"
        is_closed = status_nm.lower() in CLOSED
        tid       = task["id"]
        comments  = cmap.get(tid, [])
        ov        = is_overdue(task)
        days_in   = round(days_since(task.get("created_time_long")), 1)
        human_c   = [c for c in comments if BOT_MARKER not in c.get("content", "")]
        lb        = last_bot_comment(comments)
        h_rep = (bool(human_c) and lb and
                 any(int(c.get("created_time_long", 0)) >
                     int(lb.get("created_time_long", 0)) for c in human_c)
                 ) if lb else bool(human_c)
        recent = [{"author": _ca(c),
                   "text":   _s(c.get("content", ""))[:160],
                   "ts":     c.get("created_time_format", c.get("created_time", ""))[:16],
                   "is_bot": BOT_MARKER in c.get("content", "")} for c in comments[-3:]]
        res = result_by_id.get(tid, {})
        detail = {
            "task_id":       tid,
            "project_id":    task.get("project", {}).get("id", ""),
            "task_name":     task.get("name", tid),
            "project":       task.get("project", {}).get("name", "?"),
            "status":        status_nm, "priority": priority,
            "pct":           task.get("percent_complete", "0") or "0",
            "start_date":    task.get("start_date", "—"),
            "end_date":      task.get("end_date", "—"),
            "days_in":       days_in, "overdue": ov, "is_closed": is_closed,
            "description":   _s(task.get("description", ""))[:200],
            "total_comments":len(comments), "recent_cmts": recent,
            "human_replied": h_rep,
            "actions":       res.get("actions", []),
            "ai_summary":    res.get("summary", ""),
        }
        for name in names:
            by_owner.setdefault(name, []).append(detail)

    SC = {"open":"#3b82f6","in progress":"#0ea5e9","not started":"#6b7280","on hold":"#f59e0b",
          "closed":"#22c55e","completed":"#22c55e","ready for uat":"#8b5cf6",
          "uat":"#8b5cf6","done":"#22c55e"}
    AL = {"new_task":"#8b5cf6","missing_info":"#f59e0b","feedback_ack":"#10b981",
          "analytics":"#0ea5e9","replan":"#ef4444","digest":"#6b7280",
          "escalation_email":"#dc2626","comment_failed":"#ef4444"}
    LL = {"new_task":"Check-in","missing_info":"Follow-up","feedback_ack":"Reply Ack",
          "analytics":"Analytics","replan":"Replan","digest":"Digest",
          "escalation_email":"Escalated","comment_failed":"Failed"}

    def sb(s):
        c = SC.get(s.lower(), "#94a3b8")
        return (f'<span style="background:{c};color:#fff;padding:1px 5px;'
                f'border-radius:3px;font-size:10px;font-weight:600">{s}</span>')
    def pb(p):
        cfg = {"high": ("#ef4444","🔴HIGH"), "medium": ("#f59e0b","🟠MED"),
               "low":  ("#22c55e","🟢LOW")}
        c, l = cfg.get(p.lower(), ("#94a3b8", p or "NONE"))
        return f'<span style="color:{c};font-weight:700;font-size:10px">{l}</span>'
    def bar(pct):
        try: v = int(pct)
        except: v = 0
        c = "#22c55e" if v >= 80 else ("#f59e0b" if v >= 40 else "#ef4444")
        return (f'<div style="display:inline-flex;align-items:center;gap:3px">'
                f'<div style="background:#e5e7eb;border-radius:3px;width:55px;height:5px">'
                f'<div style="background:{c};width:{v}%;height:5px;border-radius:3px">'
                f'</div></div><span style="font-size:10px">{v}%</span></div>')
    def apills(acts):
        r = ""
        for a in acts:
            if a in ("skipped_cooldown", "skipped_closed"): continue
            c = AL.get(a, "#94a3b8"); l = LL.get(a, a)
            r += (f'<span style="background:{c};color:#fff;padding:1px 5px;'
                  f'border-radius:7px;font-size:10px;font-weight:600;margin:1px">{l}</span>')
        return r
    def chtml(cmts):
        if not cmts:
            return '<span style="color:#94a3b8;font-size:10px;font-style:italic">No comments</span>'
        r = ""
        for c in cmts:
            bg = "#eef2ff" if c["is_bot"] else "#f9fafb"
            lbl = "🤖" if c["is_bot"] else "👤"
            r += (f'<div style="background:{bg};border-radius:3px;padding:3px 6px;'
                  f'margin:2px 0;font-size:10px"><b>{lbl} {c["author"]}</b>'
                  f'<span style="color:#94a3b8;margin-left:4px">{c["ts"]}</span>'
                  f'<div style="color:#475569;margin-top:1px">{c["text"]}</div></div>')
        return r
    def trow(d, i):
        bg = "#fff" if i % 2 == 0 else "#f9fafb"
        fd = "opacity:0.5;" if d["is_closed"] else ""
        ov = ('<span style="color:#ef4444;font-size:9px;font-weight:700"> ⚠OVR</span>'
              if d["overdue"] and not d["is_closed"] else "")
        ai = (f'<div style="font-size:10px;color:#64748b;font-style:italic;margin-top:2px">'
              f'{d["ai_summary"][:110]}</div>' if d["ai_summary"] else "")
        zoho_url = (f'https://projects.zoho.in/portal/{ZOHO_PORTAL}'
                    f'/project/{d["project_id"]}/task/{d["task_id"]}/')
        view_btn = (f'<a href="{zoho_url}" target="_blank" style="display:inline-block;'
                    f'padding:3px 8px;background:#0ea5e9;color:#fff;border-radius:4px;'
                    f'font-size:10px;font-weight:600;text-decoration:none;white-space:nowrap">'
                    f'Open in Zoho &#8599;</a>')
        return (f'<tr style="background:{bg};{fd}border-bottom:1px solid #f1f5f9">'
                f'<td style="padding:7px 9px;vertical-align:top;min-width:180px;max-width:260px">'
                f'<div style="font-weight:700;font-size:11px;color:#1e293b">'
                f'{d["task_name"][:55]}{ov}</div>'
                f'<div style="font-size:10px;color:#94a3b8">&#128193; {d["project"]}</div>{ai}</td>'
                f'<td style="padding:7px 9px;vertical-align:top;white-space:nowrap">'
                f'{sb(d["status"])}<br>'
                f'<span style="font-size:10px;color:#64748b">'
                f'{d["start_date"]}&#8594;{d["end_date"]}</span><br>'
                f'<span style="font-size:10px;color:#94a3b8">{d["days_in"]}d</span></td>'
                f'<td style="padding:7px 9px;vertical-align:top">{pb(d["priority"])}</td>'
                f'<td style="padding:7px 9px;vertical-align:top">{bar(d["pct"])}</td>'
                f'<td style="padding:7px 9px;vertical-align:top;max-width:200px">'
                f'{chtml(d["recent_cmts"])}</td>'
                f'<td style="padding:7px 9px;vertical-align:top;white-space:nowrap">'
                f'{apills(d["actions"])}</td>'
                f'<td style="padding:7px 9px;vertical-align:top;white-space:nowrap">'
                f'{view_btn}</td></tr>')

    owner_secs = ""
    for owner in sorted(by_owner, key=lambda o: (o == "Unassigned", o.lower())):
        tlist  = by_owner[owner]
        active = [t for t in tlist if not t["is_closed"]]
        closed = [t for t in tlist if t["is_closed"]]
        ov_cnt = sum(1 for t in active if t["overdue"])
        ini    = "".join(p[0].upper() for p in owner.split()[:2]) if owner != "Unassigned" else "?"
        avbg   = "#0ea5e9" if owner != "Unassigned" else "#94a3b8"
        alerts = (f'<span style="background:#fef2f2;color:#ef4444;border:1px solid #fecaca;'
                  f'padding:1px 5px;border-radius:7px;font-size:10px;font-weight:600">'
                  f'⚠{ov_cnt} OVR</span>' if ov_cnt else "")
        rows   = "".join(trow(t, i) for i, t in enumerate(active))
        cl_row = ""
        if closed:
            cl_txt = " · ".join(t["task_name"][:30] for t in closed)
            cl_row = (f'<tr><td colspan="7" style="padding:4px 9px;background:#f8fafc;'
                      f'font-size:10px;color:#94a3b8">✓ Completed: {cl_txt}</td></tr>')
        if not active and not closed:
            continue
        owner_secs += (
            f'<div style="margin:14px 0">'
            f'<div style="display:flex;align-items:center;gap:10px;padding:10px 14px;'
            f'background:#1e293b;border-radius:8px 8px 0 0">'
            f'<div style="background:{avbg};color:#fff;border-radius:50%;width:32px;'
            f'height:32px;display:flex;align-items:center;justify-content:center;'
            f'font-weight:800;font-size:12px;flex-shrink:0">{ini}</div>'
            f'<div style="flex:1;color:#fff;font-weight:700;font-size:13px">{owner}'
            f'<span style="color:#94a3b8;font-size:11px;font-weight:400;margin-left:6px">'
            f'{len(active)} active · {len(closed)} closed &nbsp;{alerts}</span></div>'
            f'<div style="color:#7dd3fc;font-weight:800;font-size:18px">{len(tlist)}</div></div>'
            f'<table style="width:100%;border-collapse:collapse;font-size:11px;'
            f'border:1px solid #e5e7eb;border-top:none">'
            f'<thead><tr style="background:#334155;color:#94a3b8;font-size:10px;font-weight:600">'
            f'<th style="padding:5px 9px;text-align:left">Task / AI Summary</th>'
            f'<th style="padding:5px 9px;text-align:left">Status / Timeline</th>'
            f'<th style="padding:5px 9px;text-align:left">Priority</th>'
            f'<th style="padding:5px 9px;text-align:left">Progress</th>'
            f'<th style="padding:5px 9px;text-align:left">Recent Comments</th>'
            f'<th style="padding:5px 9px;text-align:left">Agent Action</th>'
            f'<th style="padding:5px 9px;text-align:left">View</th>'
            f'</tr></thead><tbody>{rows}{cl_row}</tbody></table></div>'
        )

    tt = sum(len(v) for v in by_owner.values())
    at = sum(1 for ts in by_owner.values() for t in ts if not t["is_closed"])
    ot = sum(1 for ts in by_owner.values() for t in ts
             if t["overdue"] and not t["is_closed"])
    pt = len([o for o in by_owner if o != "Unassigned"])
    return (
        f'<div style="font-family:Arial,sans-serif;color:#1e293b">'
        f'<div style="background:linear-gradient(135deg,#1e293b,#334155);color:#fff;'
        f'padding:18px 24px;border-radius:8px;margin-bottom:14px">'
        f'<h2 style="margin:0;font-size:16px">📋 Per-Person Task Summary — {run_time}</h2>'
        f'<p style="margin:4px 0 0;opacity:0.7;font-size:11px">Portal: {ZOHO_PORTAL} · '
        f'{tt} tasks · {pt} people · {at} active · {ot} overdue</p></div>'
        f'{owner_secs}'
        f'<p style="font-size:10px;color:#94a3b8;text-align:center;margin-top:12px">'
        f'MithilAI Agent · {run_time}</p></div>'
    )


def send_daily_report_email(results: list[dict], run_time: str,
                            per_person_html: str = "") -> None:
    if not EMAIL_ENABLED:
        log.info("[Email] Daily report skipped — email not configured.")
        return

    total       = len(results)
    commented   = sum(1 for r in results if any(
        a not in ("skipped_cooldown", "skipped_closed", "comment_failed") for a in r["actions"]))
    skipped_cd  = sum(1 for r in results if r["actions"] == ["skipped_cooldown"])
    skipped_cl  = sum(1 for r in results if r["actions"] == ["skipped_closed"])
    escalated   = sum(1 for r in results if "escalation_email" in r["actions"])
    ack_replies = sum(1 for r in results if "feedback_ack" in r["actions"])
    failed      = sum(1 for r in results if "comment_failed" in r["actions"])

    by_project: dict[str, list] = {}
    for r in results:
        by_project.setdefault(r["project"], []).append(r)

    rows_html = ""
    row_bg    = ["#ffffff", "#f9fafb"]
    idx       = 0
    for pname in sorted(by_project):
        rows_html += (
            f'<tr><td colspan="6" style="background:#1e293b;color:#fff;'
            f'padding:8px 12px;font-weight:700;font-size:13px">'
            f'&#128193; {pname}</td></tr>'
        )
        for r in by_project[pname]:
            bg       = row_bg[idx % 2]; idx += 1
            priority = r.get("priority", "none")
            pcolor   = _priority_color(priority)
            owners   = ", ".join(r.get("owners", [])) or "Unassigned"
            pills    = " ".join(_action_pill(a) for a in r["actions"])
            rows_html += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:8px 10px;font-size:12px;max-width:220px"><b>{r["task_name"][:60]}</b></td>'
                f'<td style="padding:8px 10px;font-size:12px;color:#475569">{owners[:40]}</td>'
                f'<td style="padding:8px 10px;font-size:12px">{r.get("status","?")}</td>'
                f'<td style="padding:8px 10px"><span style="color:{pcolor};font-weight:700;font-size:11px">'
                f'{priority.upper() or "NONE"}</span></td>'
                f'<td style="padding:8px 10px;white-space:nowrap">{pills}</td>'
                f'<td style="padding:8px 10px;font-size:11px;color:#64748b;max-width:300px">{r.get("summary","")[:120]}</td>'
                f'</tr>'
            )

    stat_card = lambda val, label, color: (
        f'<div style="background:#fff;border-radius:8px;padding:14px 20px;flex:1;text-align:center;border-top:4px solid {color}">'
        f'<div style="font-size:28px;font-weight:800;color:{color}">{val}</div>'
        f'<div style="font-size:12px;color:#64748b;margin-top:4px">{label}</div></div>'
    )

    stats = (
        stat_card(total,       "Total Tasks",         "#0ea5e9") +
        stat_card(commented,   "Comments Posted",      "#22c55e") +
        stat_card(ack_replies, "Replies Acknowledged", "#10b981") +
        stat_card(escalated,   "Escalations",          "#ef4444") +
        stat_card(skipped_cd,  "On Cooldown",          "#6b7280") +
        stat_card(skipped_cl,  "Closed Tasks",         "#d1d5db") +
        (stat_card(failed, "Failed Posts", "#ef4444") if failed else "")
    )

    body = f"""
<html>
<body style="font-family:Arial,sans-serif;max-width:1100px;margin:auto;color:#1e293b">
<div style="background:linear-gradient(135deg,#1e293b,#334155);color:#fff;padding:24px 32px;border-radius:12px 12px 0 0">
  <h1 style="margin:0;font-size:22px">&#128202; Zoho Projects — Daily Agent Report</h1>
  <p style="margin:6px 0 0;opacity:0.7;font-size:13px">Run on {run_time}</p>
</div>
<div style="background:#f1f5f9;padding:20px 32px;display:flex;gap:12px;flex-wrap:wrap">{stats}</div>
<table style="width:100%;border-collapse:collapse;font-size:13px">
  <thead>
    <tr style="background:#334155;color:#fff">
      <th style="padding:10px 12px;text-align:left">Task</th>
      <th style="padding:10px 12px;text-align:left">Owner</th>
      <th style="padding:10px 12px;text-align:left">Status</th>
      <th style="padding:10px 12px;text-align:left">Priority</th>
      <th style="padding:10px 12px;text-align:left">Action</th>
      <th style="padding:10px 12px;text-align:left">AI Summary</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
<div style="background:#f1f5f9;padding:16px 32px;border-radius:0 0 12px 12px;font-size:11px;color:#94a3b8;margin-top:0">
  MithilAI Agent &bull; Automated daily report &bull; {run_time}<br>
  Cooldown: {BOT_COOLDOWN_HOURS}h &bull; No-reply escalation after: {NO_REPLY_HOURS}h &bull;
  Escalation threshold: {ESCALATION_DAYS} days
</div>

<div style="margin-top:24px;border-top:2px solid #e2e8f0;padding-top:20px">
{per_person_html}
</div>

</body></html>"""

    # To: Siva + Dhinesh only (no CC)
    report_to  = _EMAIL_TO
    all_rcpt   = report_to

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"[MithilAI Agent] Daily Report — {run_time[:10]} — "
        f"{total} tasks, {commented} comments, {escalated} escalations"
    )
    msg["From"] = GMAIL_USER
    msg["To"]   = ", ".join(report_to)
    msg.attach(MIMEText(body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, all_rcpt, msg.as_string())
        log.info("[Email] Daily report sent → To:%s", ", ".join(report_to))
    except Exception as e:
        log.error("[Email] Daily report failed: %s", e)

# ── Task Processor ────────────────────────────────────────────────────────────

async def process_task(
    client: httpx.AsyncClient,
    task: dict,
) -> dict:
    task_id    = task["id"]
    task_name  = task.get("name", task_id)
    project_id = task.get("project", {}).get("id", "")
    project_nm = task.get("project", {}).get("name", "?")
    status_nm  = task.get("status", {}).get("name", "?")
    priority   = task.get("priority", "None") or "None"
    created    = task.get("created_time_long")
    days_in    = days_since(created)
    owners     = task_owner_names(task)

    log.info("[%s] %s  (%.1fd, %s, %s)", project_nm, task_name, days_in, status_nm, priority)

    base_result = {
        "task_id":   task_id,
        "task_name": task_name,
        "project":   project_nm,
        "status":    status_nm,
        "priority":  priority,
        "owners":    owners,
        "summary":   "",
        "actions":   [],
    }

    # ── Closed / UAT / 100% complete: no comment ─────────────────────────────
    pct_done = int(task.get("percent_complete", 0) or 0)
    if status_nm.lower() in CLOSED_STATUSES or pct_done >= 100:
        log.info("  Closed/UAT/100%% — skipping comment (status=%s, pct=%d)", status_nm, pct_done)
        base_result["actions"] = ["skipped_closed"]
        base_result["summary"] = f"Task is {status_nm} ({pct_done}%%) — no comment posted."
        return base_result

    # ── Fetch existing comments ───────────────────────────────────────────────
    comments = await fetch_comments(client, project_id, task_id)

    if bot_already_commented_recently(comments):
        log.info("  Skipped — bot already commented within %dh", BOT_COOLDOWN_HOURS)
        base_result["actions"] = ["skipped_cooldown"]
        base_result["summary"] = "Bot cooldown active — already commented today."
        return base_result

    actions: list[str] = []

    # ── Feedback loop: detect human replies ───────────────────────────────────
    human_replies        = human_replies_after_bot(comments)
    human_replied        = bool(human_replies)
    bot_c                = last_bot_comment(comments)
    hours_since_last_bot = hours_since_ms(bot_c.get("created_time_long") if bot_c else None)
    no_reply_escalation  = (
        bot_c is not None
        and not human_replied
        and hours_since_last_bot >= NO_REPLY_HOURS
    )

    if human_replied:
        log.info("  Feedback loop: %d human reply(s) detected since last bot comment", len(human_replies))
    if no_reply_escalation:
        log.info("  Feedback loop: no reply for %.1fh — will escalate", hours_since_last_bot)

    # ── Keywords ──────────────────────────────────────────────────────────────
    desc_plain     = re.sub('<[^>]+>', ' ', task.get("description", "")).strip()
    keywords_found = has_keyword(task.get("name", "") + " " + desc_plain)
    for c in comments:
        keywords_found += has_keyword(re.sub('<[^>]+>', '', c.get("content", "")))
    for c in human_replies:
        keywords_found += has_keyword(re.sub('<[^>]+>', '', c.get("content", "")))
    keywords_found = list(set(keywords_found))

    # ── Determine comment type ────────────────────────────────────────────────
    comment_type = determine_comment_type(
        task, comments, days_in, keywords_found,
        human_replied, hours_since_last_bot,
    )

    # None = skip (same status, open-but-not-started, or already in testing cycle)
    if comment_type is None:
        log.info("  Skipped — no new comment needed (same status or not yet in progress)")
        base_result["actions"] = ["skipped_same_status"]
        base_result["summary"] = f"No comment needed — status unchanged ({status_nm})."
        return base_result

    log.info("  Comment type: %s  (priority=%s, human_replied=%s, no_reply_esc=%s)",
             comment_type, priority, human_replied, no_reply_escalation)

    # ── BPM task: use Claude Opus 4.6 for structured ERP analysis ─────────────
    if comment_type == "bpm_analysis":
        log.info("  [BPM] Calling Claude Opus 4.6 for ERP/BPM analysis...")
        bpm_sections = await analyse_bpm_task(task, comments, days_in)
        log.info("  [BPM] Summary: %s", bpm_sections.get("summary", ""))
        owner_tag    = build_owner_tag(task)
        tag_prefix   = f"{owner_tag}<br>" if owner_tag else ""
        html_comment  = tag_prefix + box_bpm_analysis(bpm_sections, priority)
        html_comment += f'<!--zs:{status_nm.lower()}-->'
        posted = await post_comment(client, project_id, task_id, html_comment)
        actions.append("bpm_analysis" if posted else "comment_failed")
        if bpm_sections.get("escalate"):
            send_escalation_email(task, bpm_sections.get("escalate_reason", "BPM escalation"),
                                  days_in, keywords_found, owners)
            actions.append("escalation_email")
        log.info("  Actions: %s", ", ".join(actions))
        base_result["actions"] = actions
        base_result["summary"] = bpm_sections.get("summary", "")
        base_result["owners"]  = owners
        return base_result

    # ── Standard tasks: use OpenAI GPT-4o ────────────────────────────────────
    box_fn = COMMENT_TYPE_BOX[comment_type]

    # ── AI analysis ───────────────────────────────────────────────────────────
    plan = await analyse_task(
        task, comments, human_replies, days_in, comment_type, no_reply_escalation
    )
    log.info("  Summary: %s", plan.get("summary", ""))

    # ── Post comment (embed status marker so we can detect same-status on next run) ──
    html_comment = box_fn(plan.get("comment_text", ""), priority)
    html_comment += f'<!--zs:{status_nm.lower()}-->'
    posted = await post_comment(client, project_id, task_id, html_comment)
    actions.append(comment_type if posted else "comment_failed")

    # ── Escalation (smart routing) ────────────────────────────────────────────
    should_escalate = plan.get("escalate") or no_reply_escalation
    if should_escalate:
        reason = plan.get("escalate_reason") or (
            f"No reply to bot's question for {round(hours_since_last_bot, 0):.0f}h"
            if no_reply_escalation else "AI-flagged escalation"
        )
        send_escalation_email(task, reason, days_in, keywords_found, owners)
        actions.append("escalation_email")

    log.info("  Actions: %s", ", ".join(actions))
    base_result["actions"] = actions
    base_result["summary"] = plan.get("summary", "")
    base_result["owners"]  = owners
    return base_result

# ── Zoho Cliq Notifications ───────────────────────────────────────────────────

CLIQ_FUNCTIONAL_WEBHOOK = os.getenv("CLIQ_FUNCTIONAL_WEBHOOK", "")   # For Testing list
CLIQ_STATUS_WEBHOOK     = os.getenv("CLIQ_STATUS_WEBHOOK", "")        # Open/In Progress by project

def _cliq_post(webhook_url: str, text: str) -> None:
    """Post a plain-text message to a Zoho Cliq channel via incoming webhook."""
    if not webhook_url:
        return
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook_url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        log.info("[Cliq] Posted to %s...", webhook_url[:60])
    except Exception as e:
        log.warning("[Cliq] Failed to post: %s", e)


def cliq_for_testing(tasks: list[dict]) -> None:
    """Post all For-Testing tasks grouped by project, sorted by priority then criticality."""
    if not CLIQ_FUNCTIONAL_WEBHOOK:
        log.info("[Cliq] CLIQ_FUNCTIONAL_WEBHOOK not set — skipping For Testing notification")
        return

    testing = [
        t for t in tasks
        if t.get("status", {}).get("name", "").lower() in TESTING_STATUSES
    ]
    if not testing:
        log.info("[Cliq] No tasks in Testing — skipping functional chat notification")
        return

    PRIORITY_ORDER    = {"high": 0, "medium": 1, "low": 2, "none": 3, "": 3}
    CRITICALITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "": 4}

    def _criticality(t: dict) -> str:
        """Extract criticality from custom fields (case-insensitive key match)."""
        for cf in t.get("custom_fields", []) or []:
            if "critical" in (cf.get("label_name") or cf.get("column_name") or "").lower():
                return (cf.get("value") or "").strip()
        return ""

    by_project: dict[str, list] = {}
    for t in testing:
        by_project.setdefault(t.get("project", {}).get("name", "?"), []).append(t)

    lines = [f"🧪 *For Testing — {datetime.now().strftime('%d %b %Y')}*\n"]
    for proj in sorted(by_project):
        lines.append(f"━━ {proj} ━━")
        sorted_tasks = sorted(
            by_project[proj],
            key=lambda t: (
                PRIORITY_ORDER.get((t.get("priority") or "").lower(), 3),
                CRITICALITY_ORDER.get(_criticality(t).lower(), 4),
            )
        )
        for t in sorted_tasks:
            name        = t.get("name", "?")
            priority    = (t.get("priority") or "None").upper()
            criticality = _criticality(t) or "—"
            owners      = ", ".join(task_owner_names(t)) or "Unassigned"
            due         = t.get("end_date", "No due date")
            lines.append(f"  • {name}")
            lines.append(f"    Priority: {priority}  |  Criticality: {criticality}  |  Owner: {owners}  |  Due: {due}")
        lines.append("")

    lines.append("Please ensure screenshots, test steps and all required fields are filled before moving to UAT.")
    _cliq_post(CLIQ_FUNCTIONAL_WEBHOOK, "\n".join(lines))


def cliq_status_update(tasks: list[dict]) -> None:
    """Post open + in-progress tasks grouped by project to the status/main group chat."""
    if not CLIQ_STATUS_WEBHOOK:
        log.info("[Cliq] CLIQ_STATUS_WEBHOOK not set — skipping status update notification")
        return

    ACTIVE = {"open", "in progress", "not started", "to do", "todo", "new",
              "in review", "review", "development", "in development"}

    active = [
        t for t in tasks
        if t.get("status", {}).get("name", "").lower() in ACTIVE
    ]
    if not active:
        log.info("[Cliq] No open/in-progress tasks — skipping status update")
        return

    by_project: dict[str, list] = {}
    for t in active:
        by_project.setdefault(t.get("project", {}).get("name", "?"), []).append(t)

    lines = [f"📋 *Daily Status Update — {datetime.now().strftime('%d %b %Y')}*\n"]
    for proj in sorted(by_project):
        ptasks = by_project[proj]
        lines.append(f"*{proj}* ({len(ptasks)} task(s))")
        for t in ptasks:
            name    = t.get("name", "?")
            status  = t.get("status", {}).get("name", "?")
            owners  = ", ".join(task_owner_names(t)) or "Unassigned"
            pct     = t.get("percent_complete", "0")
            lines.append(f"  • [{status}] {name} — {owners} ({pct}%)")
        lines.append("")

    lines.append("_MithilAI Agent — automated daily update_")
    _cliq_post(CLIQ_STATUS_WEBHOOK, "\n".join(lines))


# ── Main Workflow ─────────────────────────────────────────────────────────────

async def run_workflow() -> None:
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    log.info("=" * 65)
    log.info("MithilAI Agent — %s", run_time)
    log.info("Portal: %s  |  ALL PROJECTS + ALL TASKS", ZOHO_PORTAL)
    log.info("Feedback loop: no-reply escalation after %dh", NO_REPLY_HOURS)
    log.info("Smart routing: Siva team=%s | Dhinesh team=%s",
             SIVA_TEAM or "all", DHINESH_TEAM or "all")
    log.info("Auto-assign: disabled")
    log.info("=" * 65)

    async with httpx.AsyncClient(timeout=30) as client:

        log.info("[1/2] Fetching ALL tasks from ALL Zoho projects...")
        tasks = await fetch_all_tasks(client)
        users = await fetch_portal_users(client)

        by_project: dict[str, list] = {}
        for t in tasks:
            by_project.setdefault(t.get("project", {}).get("name", "?"), []).append(t)

        log.info("Total: %d tasks across %d project(s):", len(tasks), len(by_project))
        for pname, ptasks in sorted(by_project.items()):
            log.info("  %s: %d task(s)", pname, len(ptasks))

        if not tasks:
            log.info("Nothing to do.")
            return

        log.info("[2/2] Processing all tasks (max 5 concurrent)...")
        sem = asyncio.Semaphore(5)

        async def bounded(task):
            async with sem:
                return await process_task(client, task)

        results = await asyncio.gather(*[bounded(t) for t in tasks])

    # ── Run summary ───────────────────────────────────────────────────────────
    log.info("=" * 65)
    log.info("RUN SUMMARY — %s", run_time)
    log.info("=" * 65)
    total_actions = 0
    by_proj: dict[str, list] = {}
    for r in results:
        by_proj.setdefault(r["project"], []).append(r)

    for pname, presults in sorted(by_proj.items()):
        log.info("%s:", pname)
        for r in presults:
            tag = ", ".join(r["actions"]) if r["actions"] else "no action"
            log.info("  %-50s  [%s]", r["task_name"][:50], tag)
            if r["actions"] not in (["skipped_cooldown"], ["skipped_closed"]):
                total_actions += len(r["actions"])

    log.info("Total tasks processed : %d", len(results))
    log.info("Total actions taken   : %d", total_actions)
    log.info("=" * 65)

    log.info("[Unassigned] Checking for unassigned tasks...")
    await handle_unassigned_tasks(tasks, users)

    log.info("[Cliq] Posting For Testing list to functional chat...")
    cliq_for_testing(tasks)

    log.info("[Cliq] Posting open/in-progress status update to main group...")
    cliq_status_update(tasks)

    log.info("[Email] Building per-person report...")
    per_person_html = await build_per_person_html(tasks, results, run_time)

    log.info("[Email] Sending daily report to %s ...", ", ".join(_EMAIL_TO))
    send_daily_report_email(list(results), run_time, per_person_html)


if __name__ == "__main__":
    asyncio.run(run_workflow())