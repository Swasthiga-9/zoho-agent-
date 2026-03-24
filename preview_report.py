"""
preview_report.py  —  Live Report Server
=========================================
Fetches all tasks from Zoho Projects, groups by owner, serves
an interactive HTML report with inline comment posting.

Comments typed here are posted directly to Zoho Projects and
are immediately visible to the whole team there.

Run:   python preview_report.py
Opens: http://localhost:8766/

POST /api/comment  → proxied to Zoho API (same-origin, no CORS)
GET  /api/refresh  → refresh data without restarting
"""

import asyncio
import json
import os
import re
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
from dotenv import load_dotenv, set_key

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

PORT     = 8766
PORTAL   = os.environ["ZOHO_PORTAL"]
CLIENT_ID  = os.environ["ZOHO_CLIENT_ID"].strip("'")
CLIENT_SEC = os.environ["ZOHO_CLIENT_SECRET"].strip("'")
REFRESH_TK = os.environ["ZOHO_REFRESH_TOKEN"].strip("'")
PORTAL_BASE = f"https://projectsapi.zoho.in/restapi/portal/{PORTAL}"
ENV_PATH    = os.path.join(os.path.dirname(__file__), ".env")
BOT_MARKER  = "— Zoho Agent •"
CLOSED_STATUSES = {"closed", "completed", "deployed", "ready for uat", "uat", "done"}

# ── Token ──────────────────────────────────────────────────────────────────────

_token = ""

def refresh_token() -> str:
    global _token
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token", "client_id": CLIENT_ID,
        "client_secret": CLIENT_SEC, "refresh_token": REFRESH_TK,
    }).encode()
    req = urllib.request.Request("https://accounts.zoho.in/oauth/v2/token",
                                 data=data, method="POST")
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read().decode())
    _token = result["access_token"]
    set_key(ENV_PATH, "ZOHO_ACCESS_TOKEN", _token)
    return _token

def headers() -> dict:
    return {"Authorization": f"Zoho-oauthtoken {_token}"}

# ── Helpers ────────────────────────────────────────────────────────────────────

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
    return int(end_ms) < datetime.now(timezone.utc).timestamp() * 1000

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()

def owner_names(task: dict) -> list[str]:
    owners_raw = (task.get("details") or {}).get("owners") \
                 or task.get("owners") or task.get("owner") or []
    return [
        o.get("name") or o.get("full_name", "")
        for o in owners_raw
        if isinstance(o, dict) and (o.get("name") or o.get("full_name"))
    ]

def comment_author(c: dict) -> str:
    for key in ("added_by", "posted_by", "added_person"):
        v = c.get(key)
        if isinstance(v, dict):
            return v.get("name", "?")
        if isinstance(v, str) and v:
            return v
    return "?"

# ── Zoho fetch ─────────────────────────────────────────────────────────────────

async def zoho_get(client: httpx.AsyncClient, path: str) -> dict:
    r = await client.get(f"{PORTAL_BASE}{path}", headers=headers())
    if r.status_code == 401:
        refresh_token()
        r = await client.get(f"{PORTAL_BASE}{path}", headers=headers())
    r.raise_for_status()
    return r.json()

async def fetch_all_tasks(client: httpx.AsyncClient) -> list[dict]:
    portal_data = await zoho_get(client, "/projects/")
    projects    = portal_data.get("projects", [])
    print(f"  {len(projects)} projects found")

    async def project_tasks(project: dict) -> list[dict]:
        pid  = project["id"]
        name = project.get("name", pid)
        collected, index = [], 1
        try:
            while True:
                data  = await zoho_get(client, f"/projects/{pid}/tasks/?index={index}&range=100")
                batch = data.get("tasks", [])
                for t in batch:
                    t.setdefault("project", {"id": pid, "name": name})
                collected.extend(batch)
                if len(batch) < 100:
                    break
                index += 100
            return collected
        except Exception as e:
            print(f"  {name}: ERROR {e}")
            return []

    results = await asyncio.gather(*[project_tasks(p) for p in projects])
    seen, all_tasks = set(), []
    for batch in results:
        for t in batch:
            if t.get("id") not in seen:
                seen.add(t["id"])
                all_tasks.append(t)
    print(f"  {len(all_tasks)} tasks total")
    return all_tasks

async def fetch_comments_for_active(client: httpx.AsyncClient,
                                    tasks: list[dict]) -> dict[str, list]:
    active = [t for t in tasks
              if t.get("status", {}).get("name", "").lower() not in CLOSED_STATUSES]
    print(f"  Fetching comments for {len(active)} active tasks...")
    sem = asyncio.Semaphore(10)

    async def get_cmts(task: dict) -> tuple[str, list]:
        async with sem:
            pid = task.get("project", {}).get("id", "")
            tid = task["id"]
            try:
                data = await zoho_get(client,
                    f"/projects/{pid}/tasks/{tid}/comments/")
                return tid, data.get("comments", [])
            except Exception:
                return tid, []

    pairs = await asyncio.gather(*[get_cmts(t) for t in active])
    return dict(pairs)

# ── Build owner summary ────────────────────────────────────────────────────────

def build_owner_summary(tasks: list[dict],
                        comments_map: dict[str, list]) -> dict[str, list]:
    by_owner: dict[str, list] = {}
    for task in tasks:
        names = owner_names(task) or ["Unassigned"]
        status_nm  = task.get("status", {}).get("name", "?")
        priority   = task.get("priority", "None") or "None"
        project_nm = task.get("project", {}).get("name", "?")
        project_id = task.get("project", {}).get("id", "")
        pct        = task.get("percent_complete", "0") or "0"
        start_date = task.get("start_date", "—")
        end_date   = task.get("end_date", "—")
        days_in    = round(days_since(task.get("created_time_long")), 1)
        overdue    = is_overdue(task)
        desc       = strip_html(task.get("description", ""))[:300]
        task_id    = task["id"]
        is_closed  = status_nm.lower() in CLOSED_STATUSES
        comments   = comments_map.get(task_id, [])

        # Last human comment
        human_cmts    = [c for c in comments if BOT_MARKER not in c.get("content", "")]
        last_cmt      = human_cmts[-1] if human_cmts else None
        last_bot      = next((c for c in reversed(comments)
                              if BOT_MARKER in c.get("content", "")), None)
        human_replied = False
        if last_bot and human_cmts:
            bot_ts = int(last_bot.get("created_time_long") or 0)
            human_replied = any(int(c.get("created_time_long") or 0) > bot_ts
                                for c in human_cmts)

        hours_no_reply = None
        if last_bot and not human_replied and not is_closed:
            hours_no_reply = round(hours_since_ms(
                last_bot.get("created_time_long")), 1)

        # Recent comments (last 3, stripped)
        recent_cmts = []
        for c in comments[-3:]:
            author  = comment_author(c)
            text    = strip_html(c.get("content", ""))[:180]
            ts      = c.get("created_time_format", c.get("created_time", ""))
            is_bot  = BOT_MARKER in c.get("content", "")
            recent_cmts.append({"author": author, "text": text,
                                 "ts": ts, "is_bot": is_bot})

        detail = {
            "task_id":       task_id,
            "project_id":    project_id,
            "task_name":     task.get("name", task_id),
            "project":       project_nm,
            "status":        status_nm,
            "priority":      priority,
            "pct":           pct,
            "start_date":    start_date,
            "end_date":      end_date,
            "days_in":       days_in,
            "overdue":       overdue,
            "is_closed":     is_closed,
            "description":   desc,
            "total_comments":len(comments),
            "recent_cmts":   recent_cmts,
            "human_replied": human_replied,
            "hours_no_reply":hours_no_reply,
        }
        for name in names:
            by_owner.setdefault(name, []).append(detail)
    return by_owner

# ── HTML builder ───────────────────────────────────────────────────────────────

STATUS_COLOR = {
    "open": "#3b82f6", "in progress": "#0ea5e9", "not started": "#6b7280",
    "on hold": "#f59e0b", "closed": "#22c55e", "completed": "#22c55e",
    "ready for uat": "#8b5cf6", "uat": "#8b5cf6", "done": "#22c55e",
}

def status_badge(s: str) -> str:
    c = STATUS_COLOR.get(s.lower(), "#94a3b8")
    return (f'<span style="background:{c};color:#fff;padding:2px 8px;'
            f'border-radius:4px;font-size:11px;font-weight:600">{s}</span>')

def priority_badge(p: str) -> str:
    cfg = {"high": ("#ef4444","🔴 HIGH"), "medium": ("#f59e0b","🟠 MEDIUM"),
           "low":  ("#22c55e","🟢 LOW")}
    color, label = cfg.get(p.lower(), ("#94a3b8", p or "NONE"))
    return f'<span style="color:{color};font-weight:700;font-size:11px">{label}</span>'

def pct_bar(pct: str) -> str:
    try:    val = int(pct)
    except: val = 0
    color = "#22c55e" if val >= 80 else ("#f59e0b" if val >= 40 else "#ef4444")
    return (f'<div style="display:flex;align-items:center;gap:6px">'
            f'<div style="background:#e5e7eb;border-radius:4px;width:90px;height:7px">'
            f'<div style="background:{color};width:{val}%;height:7px;border-radius:4px">'
            f'</div></div>'
            f'<span style="font-size:11px;color:#374151">{val}%</span></div>')

def recent_comments_html(cmts: list[dict]) -> str:
    if not cmts:
        return '<div style="color:#94a3b8;font-size:11px;font-style:italic">No comments yet</div>'
    rows = ""
    for c in cmts:
        bg     = "#f0f4ff" if c["is_bot"] else "#f9fafb"
        border = "#c7d2fe" if c["is_bot"] else "#e5e7eb"
        label  = "🤖 Agent" if c["is_bot"] else f'👤 {c["author"]}'
        rows += (
            f'<div style="background:{bg};border:1px solid {border};border-radius:6px;'
            f'padding:7px 10px;margin:4px 0;font-size:11px">'
            f'<span style="font-weight:600;color:#374151">{label}</span>'
            f'<span style="color:#94a3b8;margin-left:6px">{c["ts"]}</span>'
            f'<div style="color:#475569;margin-top:3px">{c["text"]}</div></div>'
        )
    return rows

def task_card(d: dict, card_idx: int) -> str:
    uid        = f'{d["project_id"]}_{d["task_id"]}'
    bg         = "#ffffff" if card_idx % 2 == 0 else "#f9fafb"
    faded      = "opacity:0.5;" if d["is_closed"] else ""
    overdue_fl = ('<span style="background:#fef2f2;color:#ef4444;border:1px solid #fecaca;'
                  'padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;'
                  'margin-left:6px">⚠ OVERDUE</span>'
                  if d["overdue"] and not d["is_closed"] else "")
    no_rpl_fl  = ""
    if d["hours_no_reply"] is not None and d["hours_no_reply"] >= 48:
        no_rpl_fl = (f'<span style="background:#fff7ed;color:#ea580c;border:1px solid #fed7aa;'
                     f'padding:1px 6px;border-radius:4px;font-size:10px;font-weight:700;'
                     f'margin-left:6px">⏳ No reply {int(d["hours_no_reply"])}h</span>')

    comment_btn = "" if d["is_closed"] else f"""
<button onclick="openComment('{uid}','{d['project_id']}','{d['task_id']}')"
  style="background:#0ea5e9;color:#fff;border:none;padding:5px 12px;border-radius:6px;
         font-size:11px;font-weight:600;cursor:pointer;margin-top:8px">
  💬 Add Comment
</button>
<div id="form_{uid}" style="display:none;margin-top:8px">
  <textarea id="txt_{uid}" rows="3" placeholder="Type your comment here…"
    style="width:100%;box-sizing:border-box;border:1px solid #cbd5e1;border-radius:6px;
           padding:8px;font-size:12px;resize:vertical;font-family:Arial,sans-serif"></textarea>
  <div style="display:flex;gap:8px;margin-top:6px">
    <button onclick="submitComment('{uid}','{d['project_id']}','{d['task_id']}')"
      style="background:#16a34a;color:#fff;border:none;padding:5px 14px;border-radius:6px;
             font-size:11px;font-weight:600;cursor:pointer">Post to Zoho ✓</button>
    <button onclick="closeComment('{uid}')"
      style="background:#f1f5f9;color:#475569;border:none;padding:5px 12px;border-radius:6px;
             font-size:11px;cursor:pointer">Cancel</button>
  </div>
  <div id="msg_{uid}" style="font-size:11px;margin-top:4px"></div>
</div>"""

    desc_row = (
        f'<div style="font-size:11px;color:#64748b;font-style:italic;'
        f'padding:4px 0 8px">{d["description"]}</div>'
        if d["description"] else ""
    )

    return f"""
<div id="card_{uid}"
  style="background:{bg};border:1px solid #e5e7eb;border-radius:8px;
         margin:8px 0;padding:14px 16px;{faded}">

  <!-- Task title row -->
  <div style="display:flex;align-items:flex-start;justify-content:space-between;
              margin-bottom:8px">
    <div>
      <span style="font-weight:700;font-size:13px;color:#1e293b">{d['task_name']}</span>
      {overdue_fl}{no_rpl_fl}
      <div style="font-size:11px;color:#94a3b8;margin-top:2px">📁 {d['project']}</div>
    </div>
    <div style="text-align:right;flex-shrink:0;margin-left:12px">
      {status_badge(d['status'])}
    </div>
  </div>

  {desc_row}

  <!-- Details grid -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;
              font-size:12px;margin-bottom:10px">
    <div><span style="color:#64748b">Priority: </span>{priority_badge(d['priority'])}</div>
    <div><span style="color:#64748b">Progress: </span>{pct_bar(d['pct'])}</div>
    <div><span style="color:#64748b">Start: </span><b>{d['start_date']}</b></div>
    <div><span style="color:#64748b">Due: </span><b>{d['end_date']}</b></div>
    <div><span style="color:#64748b">Days in progress: </span><b>{d['days_in']}d</b></div>
    <div><span style="color:#64748b">Comments: </span><b>{d['total_comments']}</b>
      {'&nbsp;✅ replied' if d['human_replied'] else ''}
    </div>
  </div>

  <!-- Recent comments -->
  <div style="margin-bottom:8px">
    <div style="font-size:11px;font-weight:600;color:#475569;margin-bottom:4px">
      Recent Comments
    </div>
    {recent_comments_html(d['recent_cmts'])}
  </div>

  <!-- Comment form -->
  {comment_btn}
</div>"""

def build_html(by_owner: dict[str, list], run_time: str) -> str:
    total_tasks   = sum(len(v) for v in by_owner.values())
    total_owners  = len([o for o in by_owner if o != "Unassigned"])
    active_count  = sum(1 for ts in by_owner.values() for t in ts if not t["is_closed"])
    overdue_count = sum(1 for ts in by_owner.values()
                        for t in ts if t["overdue"] and not t["is_closed"])
    no_rpl_count  = sum(1 for ts in by_owner.values()
                        for t in ts
                        if t["hours_no_reply"] is not None and t["hours_no_reply"] >= 48)

    sections = ""
    for owner in sorted(by_owner, key=lambda o: (o == "Unassigned", o.lower())):
        tasks  = by_owner[owner]
        active = [t for t in tasks if not t["is_closed"]]
        closed = [t for t in tasks if t["is_closed"]]
        ov     = sum(1 for t in active if t["overdue"])
        nr     = sum(1 for t in active
                     if t["hours_no_reply"] is not None and t["hours_no_reply"] >= 48)

        initials  = "".join(p[0].upper() for p in owner.split()[:2]) if owner != "Unassigned" else "?"
        avatar_bg = "#0ea5e9" if owner != "Unassigned" else "#94a3b8"
        alerts    = ""
        if ov: alerts += (f'<span style="background:#fef2f2;color:#ef4444;border:1px solid #fecaca;'
                          f'padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">'
                          f'⚠ {ov} Overdue</span> ')
        if nr: alerts += (f'<span style="background:#fff7ed;color:#ea580c;border:1px solid #fed7aa;'
                          f'padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">'
                          f'⏳ {nr} No Reply</span>')

        active_html = "".join(task_card(t, i) for i, t in enumerate(active))
        closed_html = "".join(
            f'<span style="background:#f1f5f9;color:#94a3b8;padding:2px 8px;border-radius:4px;'
            f'font-size:11px;margin:2px;display:inline-block">✓ {t["task_name"][:45]}</span>'
            for t in closed
        )
        closed_sec = (
            f'<details style="margin-top:10px"><summary style="cursor:pointer;font-size:12px;'
            f'color:#94a3b8;padding:4px 0">▸ {len(closed)} completed task(s)</summary>'
            f'<div style="margin-top:8px">{closed_html}</div></details>'
            if closed else ""
        )

        sections += f"""
<div style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;
            margin:20px 0;box-shadow:0 1px 4px rgba(0,0,0,0.06)">
  <div style="padding:16px 20px;border-bottom:1px solid #f1f5f9;
              display:flex;align-items:center;gap:14px">
    <div style="background:{avatar_bg};color:#fff;border-radius:50%;width:44px;height:44px;
                display:flex;align-items:center;justify-content:center;
                font-weight:800;font-size:16px;flex-shrink:0">{initials}</div>
    <div style="flex:1">
      <div style="font-size:16px;font-weight:700;color:#1e293b">{owner}</div>
      <div style="font-size:12px;color:#64748b;margin-top:2px">
        {len(active)} active &nbsp;·&nbsp; {len(closed)} closed &nbsp;&nbsp;{alerts}
      </div>
    </div>
    <div style="text-align:right">
      <div style="font-size:22px;font-weight:800;color:#0ea5e9">{len(tasks)}</div>
      <div style="font-size:11px;color:#94a3b8">total tasks</div>
    </div>
  </div>
  <div style="padding:12px 16px">
    {active_html or '<p style="color:#94a3b8;font-size:12px;text-align:center;padding:20px">No active tasks</p>'}
    {closed_sec}
  </div>
</div>"""

    stat_card = lambda v, l, c: (
        f'<div style="background:#fff;border-radius:10px;padding:14px 20px;flex:1;'
        f'text-align:center;border-top:4px solid {c};min-width:100px;'
        f'box-shadow:0 1px 3px rgba(0,0,0,0.07)">'
        f'<div style="font-size:26px;font-weight:800;color:{c}">{v}</div>'
        f'<div style="font-size:12px;color:#64748b;margin-top:4px">{l}</div></div>'
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Zoho Agent — Task Report — {run_time}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: Arial, sans-serif; max-width: 960px; margin: auto;
          padding: 20px; background: #f8fafc; color: #1e293b; }}
  textarea:focus {{ outline: 2px solid #0ea5e9; border-color: #0ea5e9; }}
  button:hover {{ filter: brightness(0.92); }}
  details summary::-webkit-details-marker {{ display:none; }}
</style>
</head>
<body>

<div style="background:linear-gradient(135deg,#1e293b,#334155);color:#fff;
            padding:28px 32px;border-radius:12px;margin-bottom:24px">
  <h1 style="margin:0;font-size:22px">📋 Zoho Projects — Per-Person Task Report</h1>
  <p style="margin:8px 0 0;opacity:0.7;font-size:13px">
    Generated: {run_time} &nbsp;·&nbsp; Portal: {PORTAL}
    &nbsp;&nbsp;
    <a href="/api/refresh" style="color:#7dd3fc;font-size:12px"
       onclick="refreshData(event)">↻ Refresh Data</a>
  </p>
</div>

<div style="display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap">
  {stat_card(total_tasks,   "Total Tasks",    "#0ea5e9")}
  {stat_card(total_owners,  "People",         "#22c55e")}
  {stat_card(active_count,  "Active Tasks",   "#f59e0b")}
  {stat_card(overdue_count, "Overdue",        "#ef4444")}
  {stat_card(no_rpl_count,  "No Reply 48h+",  "#ea580c")}
</div>

{sections}

<div style="text-align:center;padding:20px;font-size:11px;color:#94a3b8;margin-top:20px">
  Zoho Projects Agent &nbsp;·&nbsp; {run_time}
  &nbsp;·&nbsp; Comments posted here appear live in Zoho Projects
</div>

<script>
function openComment(uid, pid, tid) {{
  document.getElementById('form_' + uid).style.display = 'block';
  document.getElementById('txt_'  + uid).focus();
}}
function closeComment(uid) {{
  document.getElementById('form_' + uid).style.display = 'none';
  document.getElementById('msg_'  + uid).textContent = '';
}}
function submitComment(uid, pid, tid) {{
  const txt = document.getElementById('txt_' + uid).value.trim();
  if (!txt) {{ alert('Please enter a comment.'); return; }}
  const msg = document.getElementById('msg_' + uid);
  msg.style.color = '#64748b';
  msg.textContent = 'Posting…';
  fetch('/api/comment', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{project_id: pid, task_id: tid, content: txt}})
  }})
  .then(r => r.json())
  .then(data => {{
    if (data.ok) {{
      msg.style.color = '#16a34a';
      msg.textContent = '✓ Comment posted to Zoho Projects!';
      document.getElementById('txt_' + uid).value = '';
      // Append the new comment to the card's recent comments list
      const card = document.getElementById('card_' + uid);
      const cmtsDiv = card.querySelector('.recent-cmts');
      if (cmtsDiv) {{
        const now = new Date().toLocaleString();
        cmtsDiv.innerHTML += `<div style="background:#f9fafb;border:1px solid #e5e7eb;
          border-radius:6px;padding:7px 10px;margin:4px 0;font-size:11px">
          <span style="font-weight:600;color:#374151">👤 You</span>
          <span style="color:#94a3b8;margin-left:6px">${{now}}</span>
          <div style="color:#475569;margin-top:3px">${{txt}}</div></div>`;
      }}
      setTimeout(() => closeComment(uid), 2500);
    }} else {{
      msg.style.color = '#ef4444';
      msg.textContent = '✗ ' + (data.error || 'Failed to post comment');
    }}
  }})
  .catch(err => {{
    msg.style.color = '#ef4444';
    msg.textContent = '✗ Network error: ' + err.message;
  }});
}}
function refreshData(e) {{
  e.preventDefault();
  if (!confirm('Refresh all task data from Zoho? This may take 30–60 seconds.')) return;
  document.body.style.opacity = '0.5';
  fetch('/api/refresh')
    .then(() => location.reload())
    .catch(() => {{ document.body.style.opacity='1'; alert('Refresh failed'); }});
}}
// Mark recent-cmts divs for JS appending
document.querySelectorAll('.recent-cmts-wrapper').forEach(el => el.classList.add('recent-cmts'));
</script>
</body>
</html>"""

# ── HTTP Server ────────────────────────────────────────────────────────────────

class ReportHandler(BaseHTTPRequestHandler):
    html      = ""
    token_ref = staticmethod(refresh_token)

    def log_message(self, fmt, *args):
        print(f"[HTTP] {self.address_string()} {fmt % args}")

    # ── GET / ──────────────────────────────────────────────────────────────────
    def do_GET(self):
        if self.path == "/api/refresh":
            self._trigger_refresh()
            return
        body = ReportHandler.html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _trigger_refresh(self):
        """Rebuild HTML from fresh Zoho data in a background thread."""
        def rebuild():
            print("[Refresh] Re-fetching data...")
            ReportHandler.html = asyncio.run(_fetch_and_build())
            print("[Refresh] Done.")
        threading.Thread(target=rebuild, daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    # ── POST /api/comment ──────────────────────────────────────────────────────
    def do_POST(self):
        if self.path != "/api/comment":
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            pid    = body["project_id"]
            tid    = body["task_id"]
            text   = body["content"].strip()
            if not text:
                raise ValueError("Empty comment")
            ok, err = _post_comment_sync(pid, tid, text)
            resp = json.dumps({"ok": ok, "error": err}).encode()
        except Exception as e:
            resp = json.dumps({"ok": False, "error": str(e)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

# ── Sync comment post (called from request handler thread) ────────────────────

def _post_comment_sync(project_id: str, task_id: str, content: str) -> tuple[bool, str]:
    url  = f"{PORTAL_BASE}/projects/{project_id}/tasks/{task_id}/comments/"
    data = urllib.parse.urlencode({"content": content}).encode()
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers=headers())
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
            print(f"[Zoho] Comment posted → task {task_id}")
            return True, ""
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                refresh_token()
                continue
            return False, f"Zoho API error {e.code}"
        except Exception as e:
            return False, str(e)
    return False, "Unknown error"

# ── Async data fetch + HTML build ─────────────────────────────────────────────

async def _fetch_and_build() -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        tasks        = await fetch_all_tasks(client)
        comments_map = await fetch_comments_for_active(client, tasks)
    by_owner = build_owner_summary(tasks, comments_map)
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    for owner, ts in sorted(by_owner.items()):
        active = sum(1 for t in ts if not t["is_closed"])
        print(f"  {owner:30s}  {len(ts):3d} tasks  ({active} active)")
    return build_html(by_owner, run_time)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*58}")
    print(f"  Zoho Agent — Live Report Server")
    print(f"{'='*58}")
    print("\n[Auth] Refreshing token...")
    refresh_token()
    print("[1/2] Fetching tasks + comments from Zoho Projects...")
    html = asyncio.run(_fetch_and_build())
    ReportHandler.html = html

    server = HTTPServer(("localhost", PORT), ReportHandler)
    url    = f"http://localhost:{PORT}/"
    print(f"\n[2/2] Server running at {url}")
    print("  • View per-person report in your browser")
    print("  • Click '💬 Add Comment' on any task to post to Zoho")
    print("  • Click '↻ Refresh Data' to re-fetch from Zoho")
    print("  • Press Ctrl+C to stop\n")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Server] Stopped.")

if __name__ == "__main__":
    main()