# Zoho Projects AI Agent — Architecture & System Document

**Organisation:** Mithilai Solutions
**Version:** 1.0
**Last Updated:** March 2026

---

## 1. System Overview

The Zoho Projects AI Agent is a fully automated daily workflow system that monitors all tasks across all Zoho Projects, posts intelligent context-aware comments, sends escalation alerts, and broadcasts status updates to the team via Zoho Cliq and Email — without any manual intervention.

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         GITHUB ACTIONS (Cloud)                          │
│                      Runs daily at 8:00 AM IST                          │
│                    Works even when PC is OFF                             │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ triggers
                               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         main.py — Agent Core                            │
│                                                                         │
│   ┌─────────────┐    ┌──────────────┐    ┌──────────────────────────┐  │
│   │ Step 1      │    │ Step 2       │    │ Step 3                   │  │
│   │ Token       │───▶│ Fetch ALL    │───▶│ Process Each Task        │  │
│   │ Refresh     │    │ Tasks from   │    │ (max 5 concurrent)       │  │
│   │ (Zoho OAuth)│    │ ALL Projects │    │                          │  │
│   └─────────────┘    └──────────────┘    └──────────┬───────────────┘  │
│                                                      │                  │
│                           ┌──────────────────────────┘                  │
│                           ▼                                             │
│   ┌────────────────────────────────────────────────────────────────┐   │
│   │                 COMMENT INTELLIGENCE ENGINE                     │   │
│   │                                                                 │   │
│   │  ┌─────────────┐   ┌──────────────────────────────────────┐   │   │
│   │  │ Status Check│   │ Comment Type Decision                │   │   │
│   │  │             │   │                                      │   │   │
│   │  │ CLOSED/UAT  │──▶│  SKIP — no comment posted           │   │   │
│   │  │ OPEN        │──▶│  new_task (first time only)         │   │   │
│   │  │ IN PROGRESS │──▶│  analytics / missing_info / replan  │   │   │
│   │  │ TESTING     │──▶│  testing_check (checklist)          │   │   │
│   │  │ ON HOLD     │──▶│  replan                             │   │   │
│   │  └─────────────┘   └──────────────────────────────────────┘   │   │
│   │                                                                 │   │
│   │  ┌──────────────────────────────────────────────────────────┐  │   │
│   │  │                 FEEDBACK LOOP                            │  │   │
│   │  │                                                          │  │   │
│   │  │  Human replied to bot?  ──YES──▶  feedback_ack          │  │   │
│   │  │         │                         (or replan if blocked) │  │   │
│   │  │         NO                                               │  │   │
│   │  │         ▼                                                │  │   │
│   │  │  No reply for 48h?      ──YES──▶  replan + escalation   │  │   │
│   │  │         │                                               │  │   │
│   │  │         NO                                               │  │   │
│   │  │         ▼                                                │  │   │
│   │  │  Same status as last    ──YES──▶  SKIP (wait for        │  │   │
│   │  │  bot comment?                      status change)        │  │   │
│   │  └──────────────────────────────────────────────────────────┘  │   │
│   │                                                                 │   │
│   │  ┌──────────────────────────────────────────────────────────┐  │   │
│   │  │              OpenAI GPT-4o (AI Engine)                   │  │   │
│   │  │  Receives: task snapshot, status, priority, comments,    │  │   │
│   │  │            missing fields, comment type instruction      │  │   │
│   │  │  Returns:  comment_text, escalate (yes/no), summary      │  │   │
│   │  └──────────────────────────────────────────────────────────┘  │   │
│   │                                                                 │   │
│   │  ┌──────────────────────────────────────────────────────────┐  │   │
│   │  │              24h Cooldown Guard                          │  │   │
│   │  │  Never posts twice on the same task within 24 hours      │  │   │
│   │  └──────────────────────────────────────────────────────────┘  │   │
│   └────────────────────────────────────────────────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
                               │
              ┌────────────────┼──────────────────┐
              ▼                ▼                  ▼
┌─────────────────┐  ┌─────────────────┐  ┌──────────────────────────────┐
│  ZOHO PROJECTS  │  │  EMAIL (Gmail)  │  │     ZOHO CLIQ                │
│                 │  │                 │  │                              │
│ • Post comment  │  │ Escalation Mail │  │ Functional Chat:             │
│   on task       │  │ → Siva or       │  │  🧪 For Testing tasks        │
│ • Marks status  │  │   Dhinesh based │  │     by project + priority    │
│   with hidden   │  │   on team       │  │                              │
│   <!--zs:tag--> │  │                 │  │ Main Group (for Arul):       │
│                 │  │ Daily Report    │  │  📋 Open + In Progress       │
│                 │  │ → Full HTML     │  │     tasks by project         │
│                 │  │   per-person    │  │     with % complete          │
│                 │  │   breakdown     │  │                              │
└─────────────────┘  └─────────────────┘  └──────────────────────────────┘
```

---

## 3. Component Breakdown

### 3.1 Trigger — GitHub Actions
| Property | Value |
|---|---|
| Platform | GitHub Actions (cloud-hosted runner) |
| Schedule | Every day at 08:00 AM IST (02:30 UTC) |
| Trigger | Also manual via "Run workflow" button |
| Availability | Runs even when PC is off |
| Logs | Saved as downloadable artifacts after every run |

### 3.2 Authentication — Zoho OAuth2
| Property | Value |
|---|---|
| Method | OAuth 2.0 Refresh Token flow |
| Region | Zoho India (`accounts.zoho.in`) |
| Token refresh | Automatic at every run start |
| Retry | Up to 3 attempts on network failure |
| Storage | GitHub Secrets (never in code) |

### 3.3 Task Fetching
- Fetches **all tasks** from **all projects** in the `mithilai` portal
- Includes task name, status, priority, owners, dates, description, % complete
- Concurrent comment fetching (up to 10 parallel requests via `asyncio.Semaphore`)

### 3.4 Comment Intelligence Engine

#### Comment Types
| Type | Trigger | What it says |
|---|---|---|
| `new_task` | First ever comment on task | Welcomes task, asks for owner / due date / description / blockers |
| `missing_info` | Owner / due date / description missing | Asks only for what is genuinely absent |
| `analytics` | Task in progress, all info present | Progress report: % complete, days elapsed, next steps |
| `replan` | Overdue / blocked keyword / no reply 48h | Direct flag, proposes new timeline |
| `digest` | Low-priority, inactive | Brief nudge to post update |
| `feedback_ack` | Human replied to bot's previous comment | Acknowledges reply, summarises state, lists next steps |
| `testing_check` | Task moves to Testing / For Testing / QA | Structured checklist: hours logged, fields, screenshots, test steps, sign-off |

#### Skip Conditions (no comment posted)
| Condition | Reason |
|---|---|
| Status = Closed / Completed / Deployed / Ready for UAT / Done | Task is finished |
| Bot already commented within last 24 hours | Cooldown guard |
| Same status as last bot comment, no human reply, no escalation trigger | Avoids repetitive nagging |
| Task is Open / Not Started and already has a comment | Waiting for team to pick it up |

#### Feedback Loop
```
Bot posts comment (with <!--zs:status--> hidden marker)
         │
         ▼
Human replies?
    YES → bot posts feedback_ack (or replan if blocked keywords in reply)
    NO  → wait 48 hours
              │
              ▼
         Still no reply?
              YES → replan comment + escalation email sent
              NO  → continue normal cycle
```

### 3.5 Escalation Routing
| Condition | Action |
|---|---|
| Task overdue 7+ days | Escalation email |
| Keywords: blocked, urgent, stuck, waiting, delayed, overdue | Escalation email |
| No reply to bot question for 48+ hours | Escalation email |
| **Routing:** owner in SIVA_TEAM | Email goes only to Siva |
| **Routing:** owner in DHINESH_TEAM | Email goes only to Dhinesh |
| **Routing:** owner not mapped | Email goes to both |

### 3.6 Unassigned Task Detection
```
Find all open tasks with no owner assigned
         │
         ▼
For each unassigned task:
  Scan all completed tasks across all projects
  Match by keyword similarity (task name / description)
         │
         ▼
Find the person who completed the most similar tasks
         │
         ▼
Send personal email to ONLY that one person:
  "This task is unassigned. Based on your past work,
   you seem like the best fit. Can you take it?"
         │
         ▼
No match found? → Skip silently (no email sent)
```

### 3.7 Zoho Cliq Notifications

#### Functional Chat — For Testing
- Posted daily after the main run
- Lists every task currently in Testing / For Testing / QA
- Grouped by project
- Sorted by priority (High → Medium → Low)
- Shows owner name and due date
- Ends with reminder about screenshots, test steps, and fields

#### Main Group — Daily Status Update (for Arul)
- Posted daily to "Mithilai Solutions - Work Chats and Sessions"
- Lists all Open + In Progress tasks
- Grouped by project
- Shows status, owner, and % complete
- Gives Arul a full picture of where each project stands

### 3.8 Daily Email Report
- **To:** Configured recipients (currently testing with `swasthiga6@gmail.com`)
- **Content:** Full HTML email with:
  - Run summary table (tasks processed, comments posted, escalations)
  - Per-person task breakdown for every team member
  - Status badge, priority badge, progress bar, timeline
  - Last 3 comments per task
  - "Open in Zoho ↗" link per task

---

## 4. Data Flow

```
GitHub Actions
     │
     ▼
Zoho OAuth token refresh
     │
     ▼
Fetch all tasks (all projects)
     │
     ├──▶ For each task (max 5 concurrent):
     │         Fetch comments
     │         Run feedback loop check
     │         Determine comment type
     │         Call OpenAI GPT-4o
     │         Post comment to Zoho Projects
     │         If escalation → send email
     │
     ├──▶ Unassigned task detection
     │         Find candidates
     │         Send personal emails
     │
     ├──▶ Zoho Cliq — For Testing notification
     │
     ├──▶ Zoho Cliq — Daily Status Update
     │
     └──▶ Email — Daily HTML Report
```

---

## 5. Technology Stack

| Component | Technology |
|---|---|
| Language | Python 3.11 |
| Scheduler | GitHub Actions (cron) |
| Zoho API | REST API v3 (zoho.in region) |
| AI Engine | OpenAI GPT-4o via `openai` SDK |
| HTTP Client | `httpx` (async) |
| Concurrency | `asyncio` + `asyncio.Semaphore` |
| Email | Gmail SMTP SSL (port 465) |
| Password Storage | Windows Credential Manager (`keyring`) / GitHub Secrets |
| Chat | Zoho Cliq Bot API |
| Config | `.env` file + GitHub Secrets |
| Logging | Python `logging` → `logs/agent.log` + GitHub Artifacts |

---

## 6. Configuration Reference

### .env / GitHub Secrets

| Key | Purpose |
|---|---|
| `ZOHO_CLIENT_ID` | Zoho OAuth app client ID |
| `ZOHO_CLIENT_SECRET` | Zoho OAuth app client secret |
| `ZOHO_REFRESH_TOKEN` | Zoho OAuth refresh token (long-lived) |
| `ZOHO_ACCESS_TOKEN` | Zoho OAuth access token (auto-refreshed) |
| `ZOHO_PORTAL` | Portal name: `mithilai` |
| `OPENAI_API_KEY` | OpenAI API key for GPT-4o |
| `GMAIL_USER` | Gmail sender address |
| `GMAIL_APP_PASSWORD` | Gmail 16-char App Password |
| `EMAIL_TO_1` | Primary report recipient 1 |
| `EMAIL_TO_2` | Primary report recipient 2 |
| `SIVA_EMAIL` | Siva's email for escalation routing |
| `DHINESH_EMAIL` | Dhinesh's email for escalation routing |
| `SIVA_TEAM` | Comma-separated names of Siva's team |
| `DHINESH_TEAM` | Comma-separated names of Dhinesh's team |
| `CLIQ_FUNCTIONAL_WEBHOOK` | Zoho Cliq webhook for For Testing channel |
| `CLIQ_STATUS_WEBHOOK` | Zoho Cliq webhook/bot for main group |
| `ESCALATION_DAYS` | Days before escalation triggers (default: 7) |
| `BOT_COOLDOWN_HOURS` | Min hours between bot comments (default: 24) |
| `NO_REPLY_HOURS` | Hours before no-reply escalation (default: 48) |
| `KEYWORDS` | Comma-separated escalation trigger words |

---

## 7. Files

| File | Purpose |
|---|---|
| `main.py` | Core agent — all logic, API calls, email, Cliq |
| `preview_report.py` | Local HTTP server (port 8766) for interactive report |
| `setup_email.py` | Stores Gmail App Password in Windows Credential Manager |
| `setup_scheduler.py` | Registers Windows Task Scheduler job (local fallback) |
| `.env` | Local config (never committed to GitHub) |
| `.env.example` | Template for setting up the environment |
| `.github/workflows/daily_agent.yml` | GitHub Actions workflow — daily cron trigger |
| `requirements.txt` | Python dependencies |
| `logs/agent.log` | Local run logs |

---

## 8. Deployment

### Production (GitHub Actions — Recommended)
- Code lives at: https://github.com/Swasthiga-9/zoho-agent-
- Runs automatically every day at 8 AM IST
- Secrets managed via GitHub → Settings → Secrets → Actions
- No server, no PC required
- Logs downloadable as GitHub Artifacts after each run

### Local Fallback (Windows Task Scheduler)
- Registered as `ZohoProjectsAgent` task
- Runs `C:\Python314\python.exe main.py` at 08:00 daily
- PC must be on and logged in

---

## 9. What the Agent Never Does

- Does **not** comment on Closed / UAT / Completed / Deployed tasks
- Does **not** comment twice on the same task within 24 hours
- Does **not** repeat a comment while the task status is unchanged
- Does **not** send unassigned task emails to everyone — only to the one best-matched person
- Does **not** auto-assign tasks (disabled by design)
- Does **not** store passwords in code or `.env` — uses Credential Manager / GitHub Secrets

---

*Zoho Projects AI Agent — Built for Mithilai Solutions*
*Automated with GitHub Actions + OpenAI GPT-4o + Zoho REST API*
