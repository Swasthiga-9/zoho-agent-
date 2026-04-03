# MithilAI Agent — Architecture & Workflow Document

**Organisation:** Mithilai Solutions
**Version:** 2.0
**Last Updated:** March 2026

---

## 1. System Overview

The MithilAI Agent is a fully automated daily workflow system built for Mithilai Solutions. It monitors every task across all Zoho Projects, posts intelligent AI-generated comments, detects blockers, routes escalation emails to the right manager, sends structured testing checklists, and broadcasts daily status updates to the team via Zoho Cliq — all without any manual intervention. It runs every day at 8:00 AM IST via GitHub Actions even when all PCs are switched off.

---

## 2. Full Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          GITHUB ACTIONS (Cloud)                              │
│                    Runs daily at 8:00 AM IST (02:30 UTC)                     │
│                  Manual trigger available via Run Workflow                    │
│                       Works even when PC is OFF                              │
└─────────────────────────────┬────────────────────────────────────────────────┘
                              │ triggers main.py
                              ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                        STEP 1 — Authentication                               │
│                                                                              │
│   Zoho OAuth2 Token Refresh                                                  │
│   ├── Uses ZOHO_REFRESH_TOKEN to get new access token                        │
│   ├── Retries up to 3 times on failure                                       │
│   └── Saves new token back to environment                                    │
└─────────────────────────────┬────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                      STEP 2 — Fetch All Tasks                                │
│                                                                              │
│   Zoho Projects REST API v3 (zoho.in)                                        │
│   ├── Fetches ALL projects in mithilai portal                                │
│   ├── Fetches ALL tasks from every project (paginated)                       │
│   ├── Includes: name, status, priority, owners, dates,                       │
│   │             description, % complete, tasklist                            │
│   └── Logs count per project (active vs closed)                              │
└─────────────────────────────┬────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                  STEP 3 — Process Each Task (max 5 concurrent)               │
│                                                                              │
│   For every task, run the following decision pipeline:                       │
│                                                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                     GUARD CHECKS (skip if true)                     │   │
│   │                                                                     │   │
│   │  1. Status in CLOSED set?          → SKIP (skipped_closed)         │   │
│   │     (closed, completed, deployed,                                   │   │
│   │      done, cancelled, signed off,                                   │   │
│   │      ready for uat, released ...)                                   │   │
│   │                                                                     │   │
│   │  2. Percent complete = 100%?        → SKIP (skipped_closed)        │   │
│   │                                                                     │   │
│   │  3. Bot commented within 24h?       → SKIP (skipped_cooldown)      │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                              │                                               │
│                              ▼                                               │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                 COMMENT TYPE DECISION ENGINE                        │   │
│   │                                                                     │   │
│   │  Priority order:                                                    │   │
│   │                                                                     │   │
│   │  1. BPM Task detected?                                              │   │
│   │     └── Keywords: bpm, epicor bpm, baq, method directive,          │   │
│   │         pre-processing, customisation layer, etc.                   │   │
│   │         ├── Same status as last bot comment + no human reply?       │   │
│   │         │   └── SKIP                                                │   │
│   │         └── Post: BPM_ANALYSIS (via Claude Opus 4.6)               │   │
│   │                                                                     │   │
│   │  2. Status in TESTING set?                                          │   │
│   │     (testing, for testing, qa, in qa, uat testing)                  │   │
│   │         ├── Already checked in this testing cycle?                  │   │
│   │         │   └── SKIP                                                │   │
│   │         └── Post: TESTING_CHECK                                     │   │
│   │                                                                     │   │
│   │  3. Human replied to bot's last comment?                            │   │
│   │         ├── Reply has blocked/stuck keywords? → Post: REPLAN        │   │
│   │         └── Normal reply?                     → Post: FEEDBACK_ACK  │   │
│   │                                                                     │   │
│   │  4. Bot asked question + no reply for 48h?                          │   │
│   │         └── Post: REPLAN + send ESCALATION EMAIL                   │   │
│   │                                                                     │   │
│   │  5. Same status as last bot comment + no escalation trigger?        │   │
│   │         └── SKIP (avoid repeat nagging)                            │   │
│   │                                                                     │   │
│   │  6. Status = OPEN / Not Started?                                    │   │
│   │         ├── No comments yet?              → Post: NEW_TASK          │   │
│   │         ├── Missing owner/date/desc?       → Post: MISSING_INFO     │   │
│   │         └── All info present + commented?  → SKIP (wait)           │   │
│   │                                                                     │   │
│   │  7. Status = ON HOLD?                                               │   │
│   │         └── Post: REPLAN                                           │   │
│   │                                                                     │   │
│   │  8. Active task — Priority matrix:                                  │   │
│   │         HIGH   → overdue/keywords? REPLAN : ANALYTICS              │   │
│   │         MEDIUM → overdue/7+days?   REPLAN : ANALYTICS              │   │
│   │         LOW    → missing info?     MISSING_INFO : DIGEST            │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴──────────────────┐
              ▼                                  ▼
┌─────────────────────────┐        ┌─────────────────────────────────────────┐
│   NON-BPM TASKS         │        │   BPM TASKS ONLY                        │
│   OpenAI GPT-4o-mini    │        │   Claude Opus 4.6 (Anthropic API)       │
│                         │        │                                         │
│  Comment types:         │        │  Reads:                                 │
│  • new_task             │        │  • Full task description (1500 chars)   │
│  • missing_info         │        │  • ALL previous comments (human + bot)  │
│  • analytics            │        │  • Stuck/blocker detection              │
│  • replan               │        │                                         │
│  • digest               │        │  Generates 5-section analysis:          │
│  • feedback_ack         │        │  1. Task Understanding (1-2 lines)      │
│  • testing_check        │        │  2. Implementation/Logic Summary        │
│                         │        │     (5-8 specific bullets)              │
│  Format: Styled HTML    │        │  3. Compact UAT Scenarios               │
│  box with priority      │        │     (8-12 lines: condition -> result)   │
│  badge + footer         │        │  4. Clarification Questions (5-7)       │
└────────────┬────────────┘        │  5. Code Suggestion                     │
             │                     │     (area, steps, validation,           │
             │                     │      error handling, pseudocode)        │
             │                     │                                         │
             │                     │  + Blocker banner if team is stuck      │
             │                     └──────────────────┬──────────────────────┘
             │                                        │
             └──────────────┬─────────────────────────┘
                            │
                            ▼
              ┌─────────────────────────┐
              │  POST COMMENT TO ZOHO   │
              │  Projects REST API      │
              │                         │
              │  Hidden marker embedded:│
              │  <!--zs:status:xyz-->   │
              │  (tracks last status)   │
              └────────────┬────────────┘
                           │
                           ▼ (if escalation triggered)
              ┌─────────────────────────┐
              │   ESCALATION EMAIL      │
              │                         │
              │  Triggers when:         │
              │  • Overdue 7+ days      │
              │  • Keywords: blocked,   │
              │    stuck, urgent etc.   │
              │  • No reply for 48h     │
              │                         │
              │  Smart routing:         │
              │  Owner in SIVA_TEAM?    │
              │  └── Email → Siva only  │
              │  Owner in DHINESH_TEAM? │
              │  └── Email → Dhinesh    │
              │  Not mapped?            │
              │  └── Email → Both       │
              └─────────────────────────┘


═══════════════════════════════════════════════════════════════
         AFTER ALL TASKS PROCESSED — POST-RUN ACTIONS
═══════════════════════════════════════════════════════════════

┌─────────────────────────────────────────────────────────────────────────────┐
│                     UNASSIGNED TASK DETECTION                               │
│                                                                             │
│  Find all open tasks with no owner assigned                                 │
│      │                                                                      │
│      ▼                                                                      │
│  For each unassigned task:                                                  │
│      Scan ALL completed tasks across ALL projects                           │
│      Match by keyword similarity (name + description)                       │
│      │                                                                      │
│      ▼                                                                      │
│  Find person who completed the most similar tasks                           │
│      │                                                                      │
│      ├── Match found? → Send personal email to ONLY that person            │
│      └── No match?   → Skip silently (no email sent)                       │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                    ZOHO CLIQ — FOR TESTING MESSAGE                          │
│                    (Functional Chat via ProjectStatusBot)                   │
│                                                                             │
│  Lists every task in Testing / For Testing / QA / UAT Testing               │
│  Grouped by project                                                         │
│  Sorted by Priority (High → Medium → Low) then Criticality                 │
│  Shows: Task name, Priority, Criticality, Owner, Due date                  │
│  Ends with reminder: screenshots + test steps + required fields             │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                   ZOHO CLIQ — DAILY STATUS UPDATE                           │
│                    (Main Group via ProjectStatusBot)                        │
│                                                                             │
│  Lists all Open + In Progress tasks                                         │
│  Grouped by project                                                         │
│  Shows: Status, Task name, Owner, % complete                               │
│  Gives Arul a full morning briefing on every project                        │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                      DAILY HTML EMAIL REPORT                                │
│                    (Gmail SMTP → configured recipients)                     │
│                                                                             │
│  Run summary: tasks processed, comments posted, escalations                 │
│  Per-person breakdown for every team member:                                │
│  ├── Status badge, priority badge, % progress bar                          │
│  ├── Timeline (start date → due date)                                       │
│  ├── Last 3 comments per task                                               │
│  └── "Open in Zoho ↗" link per task                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Comment Types Reference

| Type | Trigger | AI Engine | Content |
|---|---|---|---|
| `bpm_analysis` | BPM keyword detected in task | Claude Opus 4.6 | Task Understanding + Logic Summary + UAT Scenarios + Clarification Questions + Code Suggestion |
| `new_task` | First ever comment on a task | OpenAI GPT-4o-mini | Welcome message, asks for owner / due date / description |
| `missing_info` | Owner / due date / description missing | OpenAI GPT-4o-mini | Asks only for what is genuinely absent |
| `analytics` | In progress, all info present | OpenAI GPT-4o-mini | Progress report: % complete, days elapsed, next steps |
| `replan` | Overdue / blocked keyword / no reply 48h | OpenAI GPT-4o-mini | Direct flag, proposes new timeline |
| `digest` | Low priority, inactive | OpenAI GPT-4o-mini | Brief nudge to post a status update |
| `feedback_ack` | Human replied to bot comment | OpenAI GPT-4o-mini | Acknowledges reply, summarises state, next steps |
| `testing_check` | Task moves to Testing / QA | OpenAI GPT-4o-mini | Hours logged, fields checklist, screenshots, test steps, sign-off |

---

## 4. Skip Conditions (No Comment Posted)

| Condition | Reason |
|---|---|
| Status = Closed / Completed / Deployed / Done / Cancelled etc. | Task is finished |
| Percent complete = 100% | Task is finished |
| Bot commented within last 24 hours | Cooldown guard |
| Same status as last bot comment + no human reply + no escalation | Avoids repeat nagging |
| Task is Open / Not Started and already has a comment | Waiting for team to pick it up |
| BPM task already analysed in this status cycle | Once per status cycle only |
| Testing task already checked in this testing cycle | Once per testing cycle only |

---

## 5. BPM Task Detection

The agent automatically identifies BPM tasks — no manual tagging needed. It scans the task **name**, **tasklist name**, and **description** for these keywords:

`bpm` · `business process management` · `epicor bpm` · `pre-processing` · `post-processing` · `method directive` · `baq` · `customization` · `bos` · `service connect` · `erp customisation` · `erp customization` · `customisation layer`

---

## 6. Escalation Routing

```
Task needs escalation?
        │
        ▼
Is task owner in SIVA_TEAM list?
    YES → Email goes to Siva only
        │
        NO
        ▼
Is task owner in DHINESH_TEAM list?
    YES → Email goes to Dhinesh only
        │
        NO
        ▼
Owner not mapped → Email goes to BOTH Siva and Dhinesh
```

**Escalation triggers:**
- Task overdue by 7+ days
- Comment contains: `blocked` · `urgent` · `stuck` · `waiting` · `delayed` · `overdue` · `review needed`
- No human reply to bot question for 48+ hours

---

## 7. Technology Stack

| Component | Technology |
|---|---|
| Language | Python 3.11 |
| Scheduler | GitHub Actions (cron `30 2 * * *` = 8AM IST) |
| Zoho API | REST API v3 — `projectsapi.zoho.in` |
| BPM AI Engine | Claude Opus 4.6 — `claude-opus-4-6` (Anthropic) |
| General AI Engine | OpenAI GPT-4o-mini |
| HTTP Client | `httpx` (async) |
| Concurrency | `asyncio` + `asyncio.Semaphore(5)` |
| Email | Gmail SMTP SSL port 465 + App Password |
| Password Storage | Windows Credential Manager (`keyring`) / GitHub Secrets |
| Chat | Zoho Cliq Bot API — `projectstatusbot` |
| Config | `.env` file (local) + GitHub Secrets (cloud) |
| Logging | Python `logging` → `logs/agent.log` + GitHub Artifacts |

---

## 8. File Structure

| File | Purpose |
|---|---|
| `main.py` | Core agent — all logic, API calls, email, Cliq |
| `.env` | Local config (never committed to GitHub) |
| `.github/workflows/daily_agent.yml` | GitHub Actions workflow — daily cron trigger |
| `requirements.txt` | Python dependencies |
| `setup_email.py` | Stores Gmail App Password in Windows Credential Manager |
| `setup_scheduler.py` | Registers Windows Task Scheduler job (local fallback) |
| `logs/agent.log` | Local run logs |
| `ARCHITECTURE.md` | This document |
| `TOOLS_AND_TECHNOLOGIES.md` | All tools and features documented |

---

## 9. Deployment

### Production — GitHub Actions (Recommended)
- Repo: `https://github.com/Swasthiga-9/zoho-agent-`
- Runs automatically every day at 8 AM IST
- All secrets managed via GitHub → Settings → Secrets → Actions
- No server or PC required
- Logs saved as downloadable artifacts after every run
- Manual trigger available via "Run workflow" button

### Local Fallback — Windows Task Scheduler
- Registered as `ZohoProjectsAgent` task
- Runs `C:\Python314\python.exe main.py` at 08:00 daily
- PC must be on and logged in

---

## 10. What the Agent Never Does

- Does **not** comment on Closed / UAT / Completed / Deployed tasks
- Does **not** comment twice on the same task within 24 hours
- Does **not** repeat the same comment while task status is unchanged
- Does **not** send unassigned task emails to everyone — only the one best-matched person
- Does **not** auto-assign tasks (disabled by design)
- Does **not** store passwords in code or `.env` — uses Credential Manager / GitHub Secrets
- Does **not** post BPM analysis on non-BPM tasks

---

*MithilAI Agent — Built for Mithilai Solutions*
*Automated with GitHub Actions + Claude Opus 4.6 + OpenAI GPT-4o-mini + Zoho REST API*
