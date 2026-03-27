# Zoho Projects AI Agent — Tools, Technologies & Features Reference

**Organisation:** Mithilai Solutions
**Version:** 2.0
**Last Updated:** March 2026

---

## Table of Contents

1. [Core Language](#1-core-language)
2. [Zoho Platform](#2-zoho-platform)
3. [AI Engine](#3-ai-engine)
4. [HTTP & Networking](#4-http--networking)
5. [Email](#5-email)
6. [Security & Configuration](#6-security--configuration)
7. [Scheduling & Deployment](#7-scheduling--deployment)
8. [Logging & Monitoring](#8-logging--monitoring)
9. [Local Preview Server](#9-local-preview-server)
10. [Summary Table](#10-summary-table)
11. [Python Package Dependencies](#11-python-package-dependencies-requirementstxt)
12. [Features — Complete List](#12-features--complete-list)

---

## 1. Core Language

### Python 3.11
- **What it is:** A high-level, general-purpose programming language.
- **Why it is used:** Python is the primary language of the entire agent. It has rich libraries for HTTP, email, async programming, and AI — all needed for this project.
- **Where it runs:**
  - Locally on Windows (`C:\Python314\python.exe`)
  - On GitHub Actions cloud runners (Ubuntu Linux)
- **Key features used:**
  - `asyncio` — run multiple Zoho API calls simultaneously instead of one by one
  - `re` — regex to strip HTML tags from Zoho comment content
  - `json` — parse all Zoho API responses
  - `datetime` — calculate days overdue, hours since last comment, task age
  - `os` — read environment variables from `.env` / GitHub Secrets
  - `pathlib` — manage log file paths cross-platform
  - `smtplib` — send emails via Gmail
  - `urllib.request` — make HTTP calls for token refresh and Cliq webhook posts
  - `http.server` — serve the local interactive report

### asyncio (Python Built-in)
- **What it is:** Python's built-in library for asynchronous, concurrent programming.
- **Why it is used:** The agent processes hundreds of tasks. Without async, each task would wait for the previous one — making a run take 30+ minutes. With asyncio, up to 5 tasks are processed simultaneously and up to 10 comments are fetched in parallel, keeping runs under 2 minutes.
- **Key usage in the agent:**
  - `asyncio.Semaphore(5)` — limits task processing to 5 at a time (avoids Zoho API rate limits)
  - `asyncio.Semaphore(10)` — limits comment fetching to 10 concurrent requests
  - `asyncio.gather()` — runs all tasks concurrently and waits for all to finish
  - `asyncio.run()` — entry point that starts the entire async workflow

---

## 2. Zoho Platform

### Zoho Projects REST API v3
- **What it is:** Zoho's official HTTP API for reading and writing data in Zoho Projects.
- **Base URL:** `https://projectsapi.zoho.in/restapi/portal/mithilai/`
- **Why it is used:** The agent needs to read every task across all projects and post comments back — this is only possible via the REST API.
- **Endpoints used:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/projects/` | GET | List all projects in the portal |
| `/projects/{id}/tasks/` | GET | Fetch all tasks in a project |
| `/projects/{id}/tasks/{id}/comments/` | GET | Fetch all comments on a task |
| `/projects/{id}/tasks/{id}/comments/` | POST | Post a new comment on a task |
| `/portal/{portal}/users/` | GET | Fetch all portal users (for unassigned matching) |

- **Authentication:** Bearer token (OAuth 2.0 access token) sent in every request header:
  ```
  Authorization: Zoho-oauthtoken {access_token}
  ```
- **Rate limits:** Zoho limits concurrent requests — handled by `asyncio.Semaphore`

---

### Zoho OAuth 2.0
- **What it is:** Zoho's authentication system using the OAuth 2.0 standard.
- **Why it is used:** The Zoho Projects API does not accept usernames/passwords. It requires a short-lived access token (valid 1 hour) generated from a long-lived refresh token.
- **How it works in the agent:**
  ```
  On every run:
    1. Agent sends refresh_token + client_id + client_secret
       to https://accounts.zoho.in/oauth/v2/token
    2. Zoho returns a new access_token (valid 1 hour)
    3. Agent uses this token for all API calls in that run
    4. New token is saved to .env / GitHub Secrets automatically
  ```
- **Credentials involved:**
  - `ZOHO_CLIENT_ID` — identifies the OAuth app
  - `ZOHO_CLIENT_SECRET` — proves the app's identity
  - `ZOHO_REFRESH_TOKEN` — long-lived token (never expires unless revoked)
  - `ZOHO_ACCESS_TOKEN` — short-lived token (1 hour, auto-refreshed)
- **Retry logic:** If the token refresh fails due to a network error, the agent retries up to 3 times before stopping.

---

### Zoho Cliq Bot API
- **What it is:** Zoho's chat platform API that allows bots to post messages to channels and group chats.
- **Why it is used:** The agent posts two daily notifications to the team's Zoho Cliq group:
  1. A **For Testing** list (to the functional chat) — lists all tasks in testing status, grouped by project and priority
  2. A **Daily Status Update** (to the main group for Arul) — lists all open and in-progress tasks by project with owner and % complete
- **Bot name:** `projectstatusbot`
- **Bot permalink:** `https://cliq.zoho.in/company/60049859675/bots/projectstatusbot`
- **Message format:** Plain text with emoji formatting — works in both channels and group chats
- **Config keys:** `CLIQ_FUNCTIONAL_WEBHOOK`, `CLIQ_STATUS_WEBHOOK`

---

## 3. AI Engine

### OpenAI GPT-4o
- **What it is:** OpenAI's most capable language model as of 2024-2026, able to understand context and generate human-quality text.
- **Why it is used:** Every comment posted to Zoho Projects is generated by GPT-4o. The agent sends GPT-4o a detailed JSON snapshot of the task (name, status, priority, owner, due date, description, recent comments, missing fields) and tells it what type of comment to write. GPT-4o returns a comment that is 100% specific to that task — no generic text.
- **Model ID:** `gpt-4o`
- **How it is called:**
  ```
  Input  →  Task snapshot (JSON) + comment type instruction
  Output →  {
               "comment_text":    "specific comment text",
               "escalate":        true/false,
               "escalate_reason": "reason if escalating",
               "summary":         "one-line task health summary"
             }
  ```
- **Comment types GPT-4o handles:**

| Type | What GPT-4o writes |
|---|---|
| `new_task` | Welcome comment, asks only for genuinely missing fields |
| `missing_info` | Targeted questions about missing owner / due date / description |
| `analytics` | Progress report with % complete, days elapsed, next steps |
| `replan` | Direct flag naming the exact issue, proposes new timeline |
| `digest` | Brief nudge to post a status update |
| `feedback_ack` | Acknowledges the human's reply, summarises state, lists next steps |
| `testing_check` | Structured checklist: hours logged, fields, screenshots, test steps, sign-off |

- **Strict rules given to GPT-4o:**
  - Never use placeholder text like [owner name] or [date]
  - Never ask about things already present in the snapshot
  - Always use the actual task name
  - Keep comments under 400 words
  - Return valid JSON only

- **API key:** Stored as `OPENAI_API_KEY` in GitHub Secrets / `.env`
- **SDK:** `openai` Python package (async client `AsyncOpenAI`)
- **Max tokens per call:** 1,200

---

### openai Python SDK
- **What it is:** Official Python library for calling OpenAI APIs.
- **Version:** Latest (`openai>=1.0`)
- **Why it is used:** Provides async support (`AsyncOpenAI`) so GPT-4o calls don't block the agent while processing other tasks simultaneously.
- **Usage:**
  ```python
  from openai import AsyncOpenAI
  client = AsyncOpenAI(api_key=OPENAI_KEY)
  response = await client.chat.completions.create(
      model="gpt-4o",
      messages=[{"role": "system", ...}, {"role": "user", ...}]
  )
  ```

---

## 4. HTTP & Networking

### httpx
- **What it is:** A modern, async-capable HTTP client for Python — a drop-in upgrade from the popular `requests` library.
- **Why it is used:** All Zoho Projects API calls are made via `httpx.AsyncClient`. This allows multiple API calls to run simultaneously (e.g. fetching comments for 10 tasks at the same time) without blocking.
- **Key features used:**
  - `AsyncClient` — persistent connection pool, reused across all API calls in a run
  - `timeout=30` — all requests timeout after 30 seconds to prevent hanging
  - Automatic JSON parsing
- **Why not `requests`:** `requests` is synchronous — it would block the agent and make runs much slower.

### urllib.request (Python Built-in)
- **What it is:** Python's built-in HTTP library.
- **Why it is used:** Used for two specific tasks where a lightweight, dependency-free call is preferable:
  1. **Token refresh** — POST to `accounts.zoho.in/oauth/v2/token`
  2. **Zoho Cliq webhook** — POST messages to Cliq channels/bots
- **Why not httpx here:** These calls happen outside the main async context (at startup and end-of-run), so the simpler built-in library is used.

---

## 5. Email

### Gmail SMTP
- **What it is:** Google's email sending service accessed via the SMTP protocol.
- **Why it is used:** The agent sends two types of emails:
  1. **Daily Report Email** — full HTML report with per-person task breakdown
  2. **Escalation Email** — urgent alert when a task is overdue / blocked / no reply
- **Server:** `smtp.gmail.com`
- **Port:** `465` (SSL — encrypted from the start)
- **Authentication:** Gmail App Password (16-character password generated specifically for the agent — not the regular Gmail login password)
- **Why App Password:** Gmail blocks regular passwords when 2-Step Verification is enabled (error 534). App Passwords bypass this restriction securely.
- **Sender:** `swasthiga6@gmail.com`

### smtplib (Python Built-in)
- **What it is:** Python's built-in library for sending email via SMTP.
- **Why it is used:** Connects to Gmail's SMTP server and sends the HTML email messages.
- **Usage pattern:**
  ```python
  with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
      server.login(GMAIL_USER, GMAIL_PASS)
      server.sendmail(sender, recipients, message)
  ```

### email.mime (Python Built-in)
- **What it is:** Python's built-in library for constructing email messages.
- **Why it is used:** Builds properly formatted HTML emails with correct headers (To, CC, Subject, From).
- **Classes used:**
  - `MIMEMultipart("alternative")` — container for the email
  - `MIMEText(html, "html")` — attaches the HTML body

---

## 6. Security & Configuration

### keyring
- **What it is:** A Python library that interfaces with the operating system's secure credential storage.
- **Why it is used:** The Gmail App Password must never be stored in plain text in `.env` files or code. `keyring` stores it encrypted in **Windows Credential Manager** on local machine and reads it back when needed.
- **Storage location:** Windows Credential Manager → `zoho_agent_gmail` service
- **How to set up:**
  ```
  python setup_email.py          → prompts for password, stores it
  python setup_email.py --test   → tests the stored password
  python setup_email.py --clear  → removes the stored password
  ```
- **Fallback:** On GitHub Actions (Linux), `keyring` has no backend — the agent falls back to `GMAIL_APP_PASSWORD` environment variable (GitHub Secret) automatically.

### python-dotenv
- **What it is:** A library that reads key=value pairs from a `.env` file and loads them as environment variables.
- **Why it is used:** All configuration (Zoho credentials, email settings, escalation thresholds) is stored in `.env` for local development. The same variables are set as GitHub Secrets for cloud deployment — the code reads them the same way in both environments via `os.getenv()`.
- **Key file:** `.env` (never committed to GitHub — listed in `.gitignore`)
- **Template file:** `.env.example` (committed to GitHub — shows what variables are needed without values)

### GitHub Secrets
- **What it is:** GitHub's encrypted storage for sensitive values used in GitHub Actions workflows.
- **Why it is used:** All credentials (Zoho tokens, OpenAI key, Gmail password, email addresses) are stored as GitHub Secrets. They are injected as environment variables when the workflow runs — they are never visible in logs or code.
- **Location:** GitHub → Repository → Settings → Secrets and variables → Actions
- **Secrets stored:**

| Secret | Description |
|---|---|
| `ZOHO_CLIENT_ID` | Zoho OAuth app client ID |
| `ZOHO_CLIENT_SECRET` | Zoho OAuth app client secret |
| `ZOHO_REFRESH_TOKEN` | Zoho OAuth long-lived refresh token |
| `ZOHO_ACCESS_TOKEN` | Zoho OAuth access token |
| `ZOHO_PORTAL` | Portal name: mithilai |
| `OPENAI_API_KEY` | OpenAI GPT-4o API key |
| `GMAIL_USER` | Gmail sender address |
| `GMAIL_APP_PASSWORD` | Gmail 16-char App Password |
| `EMAIL_TO_1` | Report recipient 1 |
| `EMAIL_TO_2` | Report recipient 2 |
| `SIVA_EMAIL` | Siva's email for escalation routing |
| `DHINESH_EMAIL` | Dhinesh's email for escalation routing |
| `SIVA_TEAM` | Comma-separated names in Siva's team |
| `DHINESH_TEAM` | Comma-separated names in Dhinesh's team |
| `CLIQ_FUNCTIONAL_WEBHOOK` | Zoho Cliq webhook for testing channel |
| `CLIQ_STATUS_WEBHOOK` | Zoho Cliq webhook for main group |

---

## 7. Scheduling & Deployment

### GitHub Actions
- **What it is:** GitHub's built-in CI/CD and automation platform that runs workflows on cloud servers.
- **Why it is used:** The agent must run every day at 8 AM IST even when the local PC is off. GitHub Actions provides free cloud compute for this.
- **Workflow file:** `.github/workflows/daily_agent.yml`
- **Trigger:** Cron schedule `30 2 * * *` (02:30 UTC = 08:00 AM IST)
- **Also triggered:** Manually via "Run workflow" button in GitHub Actions tab
- **Runner:** `ubuntu-latest` (GitHub-hosted Linux VM)
- **Steps in the workflow:**
  1. Set up job
  2. Checkout repository (download code)
  3. Set up Python 3.11
  4. Install dependencies (`pip install -r requirements.txt`)
  5. Run Zoho Agent (`python main.py`)
  6. Upload logs as artifact
- **Free tier:** GitHub Actions gives 2,000 free minutes/month — the agent uses about 2 minutes/day = ~60 minutes/month, well within the free limit.
- **Logs:** Saved as downloadable artifacts after every run, retained for 30 days.

### Windows Task Scheduler
- **What it is:** Windows built-in task automation tool.
- **Why it is used:** As a local fallback — if GitHub Actions is ever unavailable, the agent can still run locally when the PC is on.
- **Task name:** `ZohoProjectsAgent`
- **Schedule:** Daily at 08:00 AM
- **Command:** `C:\Python314\python.exe C:\Users\Admin\zoho_agent\main.py`
- **Setup script:** `python setup_scheduler.py`
- **Limitation:** PC must be on and logged in — this is why GitHub Actions is the primary deployment.

---

## 8. Logging & Monitoring

### Python logging (Built-in)
- **What it is:** Python's built-in logging framework.
- **Why it is used:** Every action the agent takes is logged with a timestamp — token refresh, tasks processed, comments posted, emails sent, errors encountered. This makes it easy to diagnose problems after a run.
- **Log format:**
  ```
  2026-03-27 08:00:01  INFO     [Auth] Refreshing Zoho access token...
  2026-03-27 08:00:03  INFO     [Luna] Fix login timeout (2.5d, In Progress, High)
  2026-03-27 08:00:04  INFO       Comment type: analytics
  2026-03-27 08:00:05  INFO       Summary: Task 60% complete, on track for deadline
  2026-03-27 08:00:05  INFO       Actions: analytics
  ```
- **Log file:** `logs/agent.log` (local machine)
- **Handlers:** Both file (`agent.log`) and console (stdout) — so logs appear in terminal and are saved to file simultaneously.
- **Log levels used:** INFO (normal flow), WARNING (non-fatal issues), ERROR (failures)

### GitHub Actions Artifacts
- **What it is:** GitHub's feature for saving files produced during a workflow run.
- **Why it is used:** After each run on GitHub Actions, `agent.log` is uploaded as a downloadable artifact. This lets you review exactly what the agent did, even without local access.
- **Retention:** 30 days (default GitHub setting)
- **How to access:** GitHub → Repository → Actions → click a run → scroll to Artifacts → download `agent-logs`

---

## 9. Local Preview Server

### http.server — BaseHTTPRequestHandler (Python Built-in)
- **What it is:** Python's built-in HTTP server framework.
- **Why it is used:** `preview_report.py` runs a local web server on port 8766 that serves an interactive HTML report of all tasks. This lets the team view the daily report in a browser and post comments directly from it — without needing to open Zoho Projects.
- **Endpoints:**

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Serves the interactive per-person HTML report |
| `/api/comment` | POST | Receives comment from browser JS, forwards to Zoho API |
| `/api/refresh` | GET | Triggers background data re-fetch without restarting server |

- **How commenting works:**
  ```
  User clicks "Add Comment" on a task card in browser
        ↓
  JS fetch() POSTs to localhost:8766/api/comment
        ↓
  Python server forwards to Zoho Projects API
        ↓
  Comment appears live in Zoho Projects
        ↓
  New comment appended to task card in browser instantly
  ```
- **Why not just open Zoho directly:** The local server provides a consolidated view of ALL tasks across ALL projects in one page, with AI action pills, progress bars, and comment history — not available in the standard Zoho Projects interface.

### webbrowser (Python Built-in)
- **What it is:** Python's built-in library for opening URLs in the default browser.
- **Why it is used:** When `preview_report.py` starts the local server, it automatically opens `http://localhost:8766` in the browser so the user doesn't have to manually type the URL.

---

## 10. Summary Table

| # | Tool / Technology | Category | Type | Purpose |
|---|---|---|---|---|
| 1 | Python 3.11 | Core | Language | Primary agent language |
| 2 | asyncio | Core | Built-in Library | Concurrent task processing |
| 3 | Zoho Projects REST API v3 | Zoho | External API | Fetch tasks, post comments |
| 4 | Zoho OAuth 2.0 | Zoho | Auth Protocol | Secure API authentication |
| 5 | Zoho Cliq Bot API | Zoho | External API | Post messages to group chat |
| 6 | OpenAI GPT-4o | AI | External API | Generate task comments |
| 7 | openai Python SDK | AI | Python Package | Connect to GPT-4o |
| 8 | httpx | HTTP | Python Package | Async Zoho API calls |
| 9 | urllib.request | HTTP | Built-in Library | Token refresh + Cliq webhooks |
| 10 | Gmail SMTP | Email | External Service | Send daily report + escalations |
| 11 | smtplib | Email | Built-in Library | Gmail connection |
| 12 | email.mime | Email | Built-in Library | Build HTML email messages |
| 13 | keyring | Security | Python Package | Store Gmail password securely |
| 14 | python-dotenv | Config | Python Package | Load .env configuration |
| 15 | GitHub Secrets | Security | Cloud Service | Store credentials for cloud runs |
| 16 | GitHub Actions | Deployment | Cloud Service | Daily scheduled execution |
| 17 | Windows Task Scheduler | Deployment | OS Tool | Local fallback scheduler |
| 18 | Python logging | Monitoring | Built-in Library | Run logs with timestamps |
| 19 | GitHub Artifacts | Monitoring | Cloud Feature | Download logs after cloud runs |
| 20 | http.server | Preview | Built-in Library | Local interactive report server |
| 21 | webbrowser | Preview | Built-in Library | Auto-open report in browser |

**Total: 21 tools across 9 categories**

---

## 11. Python Package Dependencies (requirements.txt)

```
httpx
openai
python-dotenv
keyring
```

All other tools (asyncio, smtplib, email, urllib, http.server, logging, webbrowser, json, re, os, datetime, pathlib) are part of the Python Standard Library — no installation needed.

---

---

## 12. Features — Complete List

This section documents every feature built into the agent, including the purpose, how it works, and the business reason behind it.

---

### Feature 1 — Fetch All Tasks from All Projects

**What it does:**
Connects to the Zoho Projects portal (`mithilai`) every day and pulls every single task from every project — open, closed, in progress, testing, on hold — nothing is missed.

**How it works:**
- Fetches the list of all projects first
- Then fetches all tasks from each project concurrently (up to 5 at a time)
- Each task includes: name, status, priority, owner(s), start date, due date, description, % complete, created time, last updated time

**Why it matters:**
Manual monitoring of tasks across multiple projects is time-consuming and error-prone. The agent does a full sweep every morning so nothing slips through the cracks.

---

### Feature 2 — Intelligent Comment Types (7 Types)

**What it does:**
For every active task, the agent decides what kind of comment to post based on the task's current state — not a generic message, but a context-aware comment written specifically for that task by GPT-4o.

**Comment Types:**

| Type | When it triggers | What it says |
|---|---|---|
| `new_task` | First ever comment on a task | Welcomes the task by name, asks only for genuinely missing fields (owner, due date, description, blockers) |
| `missing_info` | Owner / due date / description absent | Targeted questions about what is missing — never asks about things already filled in |
| `analytics` | Task in progress, all info present | Progress health report: % complete, days elapsed, risks, next steps with owner names |
| `replan` | Overdue / blocked / no reply 48h / on hold | Direct flag naming the exact issue, proposes concrete new timeline |
| `digest` | Low-priority, inactive task | Brief 3–4 sentence nudge to post a status update |
| `feedback_ack` | Human replied to bot's previous comment | Acknowledges the reply by name, summarises state, lists 2–3 next steps |
| `testing_check` | Task moved to Testing / For Testing / QA | Structured testing checklist (see Feature 5) |

**Business reason:**
Generic "please update your task" comments get ignored. Comments that reference the actual task name, actual missing fields, actual days overdue, and actual human replies get read and acted on.

---

### Feature 3 — Comment Once Per Status (No Repetition)

**What it does:**
Once the agent has posted a comment for a task in a given status (e.g. "In Progress"), it will NOT post another comment until the status changes. This prevents the team from being bombarded with repeated messages.

**How it works:**
- Every bot comment silently embeds a hidden HTML marker: `<!--zs:in progress-->`
- On the next run, the agent reads this marker from the last bot comment
- If the current status matches the marker and no human has replied → the agent skips that task
- The agent only comments again when:
  - The task status changes (e.g. from In Progress → Testing)
  - A human replies to the bot's previous comment
  - The task becomes overdue or blocked keywords are found
  - No reply for 48+ hours (escalation trigger)

**Business reason:**
Requested directly — "once the review comment is added, we don't need to add again if the task is in the same status." Repeated comments train the team to ignore them.

---

### Feature 4 — Open Tasks Are Not Nagged

**What it does:**
Tasks in Open / Not Started / To Do status receive only one initial `new_task` comment. After that, the agent waits silently until the team picks it up and moves it to In Progress — it does not keep posting reminders while the task sits in the backlog.

**Why it matters:**
Requested directly — "the task may be open for some time before it gets picked up, so I don't want the team to rush through the tasks because of this review comment." This prevents pressure on tasks that are legitimately queued.

---

### Feature 5 — Testing Status Checklist

**What it does:**
When a task moves to **Testing / For Testing / QA / In Testing**, the agent automatically posts a structured quality checklist as a comment on the task in Zoho Projects.

**Checklist items:**
1. **Hours logged** — confirm build time has been logged against this task
2. **Required fields** — New Customisation Layer (if applicable), Menu/Module name, Client, Priority, Criticality
3. **Screenshot evidence** — attach screenshots of ALL test scenarios (pass and fail)
4. **Test steps** — document the exact steps taken so results can be reproduced
5. **Sign-off question** — "Is this ready to move to UAT, or are there items still failing?"

**How it works:**
- Posted once per testing cycle — will not repeat until the task leaves and re-enters testing status
- If the tester replies to the checklist comment, the agent acknowledges the reply (`feedback_ack`)
- If no reply for 48h → escalation triggered

**Business reason:**
Requested directly — ensure testers fill in hours, evidence, fields, and steps before moving to UAT. Enforces a quality gate at every testing cycle.

---

### Feature 6 — Feedback Loop (Human Reply Detection)

**What it does:**
The agent reads every comment on every task. When it detects that a human has replied to a previous bot comment, it adjusts its next comment type accordingly — it does not repeat the same question.

**How it works:**
```
Bot posts comment (e.g. "missing_info" — asks for due date)
         ↓
Human replies: "Due date is 15 April"
         ↓
Next run: Agent detects the human reply
         ↓
Posts "feedback_ack" — acknowledges the reply,
confirms the due date, lists next steps
         ↓
Does NOT ask for the due date again
```

**Reply with blocked keyword:**
If the human's reply contains words like "blocked", "stuck", "waiting" → the agent skips `feedback_ack` and posts a `replan` comment + sends an escalation email instead.

**Business reason:**
Without reply detection, the bot would repeat the same question even after it has been answered — making it feel robotic and useless. The feedback loop makes comments feel like a real conversation.

---

### Feature 7 — No-Reply Escalation (48-Hour Rule)

**What it does:**
If the agent asked a question (via `new_task` or `missing_info` comment) and nobody from the team replied for **48 hours**, it automatically:
1. Posts a `replan` comment on the task flagging the lack of response
2. Sends an escalation email to the relevant manager (Siva or Dhinesh)

**Configurable:** `NO_REPLY_HOURS=48` in `.env` — can be changed to any number of hours.

**Business reason:**
Unanswered questions on tasks are a sign of a blocked or stalled task. 48 hours of silence is enough to warrant management attention.

---

### Feature 8 — Skip Closed / UAT / Completed Tasks

**What it does:**
The agent completely ignores tasks in the following statuses — no comment is posted, no escalation is triggered:
- Closed
- Completed
- Deployed
- Ready for UAT
- UAT
- Done

**Business reason:**
Requested directly — "please don't comment on closed or ready for UAT tasks." Commenting on finished tasks is noise and could confuse the team about whether action is needed.

---

### Feature 9 — 24-Hour Cooldown Guard

**What it does:**
Even if all other conditions are met, the agent will never post more than one comment on the same task within a 24-hour window.

**Configurable:** `BOT_COOLDOWN_HOURS=24` in `.env`

**Business reason:**
Prevents the agent from flooding a task with multiple comments in edge cases (e.g. if run twice in a day or if multiple triggers fire simultaneously).

---

### Feature 10 — Unassigned Task Detection & Smart Assignment Email

**What it does:**
At the end of every run, the agent scans all open tasks with no owner assigned. For each unassigned task, it finds the single best-matched person from the team and sends a **personal email only to that one person** asking if they can take it.

**How the matching works:**
```
Unassigned task: "Fix decimal hours in Labour Hire PO"
         ↓
Scan all COMPLETED tasks across all projects
         ↓
Keyword match: "Labour Hire", "PO", "decimal", "hours"
         ↓
Person who completed the most similar tasks = Ravi
         ↓
Email sent only to Ravi:
"This task is unassigned. Based on your past work
 on similar tasks, you seem like the best fit.
 Can you take it?"
```

**What it does NOT do:**
- Does not email everyone
- Does not auto-assign (disabled by design)
- Does not send any email if no match is found

**Business reason:**
Unassigned tasks fall into a gap. Sending the email only to the most relevant person (not a group) makes it a specific, actionable ask rather than a broadcast that everyone ignores.

---

### Feature 11 — Smart Escalation Routing (Siva / Dhinesh)

**What it does:**
When an escalation is triggered, the email goes to the right manager — not both — based on which team the task owner belongs to.

**Routing logic:**
```
Task owner is in SIVA_TEAM   → escalation email goes only to Siva
Task owner is in DHINESH_TEAM → escalation email goes only to Dhinesh
Owner not in either list      → email goes to both Siva and Dhinesh
Lists are blank               → email goes to both (safe fallback)
```

**Configuration in .env:**
```
SIVA_TEAM=Ravi,Anbu,Kiran
DHINESH_TEAM=Priya,Suresh,Meena
SIVA_EMAIL=sivakumarm@mithilai.com
DHINESH_EMAIL=dhineshkumars@mithilai.com
```

**Escalation triggers:**
- Task overdue by 7+ days (`ESCALATION_DAYS=7`)
- Keywords found: blocked, urgent, stuck, waiting, delayed, overdue, review needed
- No reply to bot question for 48+ hours
- High priority task that is overdue

**Business reason:**
Sending every escalation to both managers creates noise. Routing to the right person ensures accountability and faster resolution.

---

### Feature 12 — Daily HTML Email Report

**What it does:**
After processing all tasks, sends a comprehensive HTML email report to the configured recipients.

**Email contents:**
- **Run summary table** — total tasks processed, comments posted, escalations triggered, run timestamp
- **Per-person task breakdown** — for every team member with active tasks:
  - Task name and project
  - Status badge (colour-coded)
  - Priority badge
  - Progress bar (% complete)
  - Timeline (start date → due date, days in progress)
  - Last 3 comments on the task (with author and timestamp)
  - What action the agent took today (comment type posted)
  - "Open in Zoho ↗" link — click to go directly to the task in Zoho Projects

**Recipients:**
- **To:** Siva + Dhinesh (configurable via `EMAIL_TO_1`, `EMAIL_TO_2`)
- **CC:** Arul + Prabha (configurable via `EMAIL_CC_1`, `EMAIL_CC_2`)

**Business reason:**
Managers get a full picture of where every person and every project stands — in their inbox every morning, without opening Zoho Projects.

---

### Feature 13 — Zoho Cliq: For Testing Notification

**What it does:**
Posts a daily message to the **Functional Chat** channel in Zoho Cliq listing all tasks currently in Testing / For Testing / QA status.

**Message format:**
```
🧪 For Testing — 27 Mar 2026

━━ Luna Project ━━
  • Fix login timeout
    Priority: HIGH  |  Owner: Ravi  |  Due: 28 Mar
  • Decimal hours in PO
    Priority: MEDIUM  |  Owner: Siva  |  Due: 30 Mar

Please ensure screenshots, test steps and all required
fields are filled before moving to UAT.
```

**Grouping:** By project
**Sorting:** High → Medium → Low priority within each project

**Business reason:**
Requested directly — "share the list of tasks in For Testing stage by project, priority and criticality in the functional chat." Gives testers a single consolidated view of what needs their attention today.

---

### Feature 14 — Zoho Cliq: Daily Status Update for Arul

**What it does:**
Posts a daily message to the **Main Group / Status Update chat** listing all Open and In Progress tasks grouped by project — so Arul can check the status of every project first thing in the morning without opening Zoho Projects.

**Message format:**
```
📋 Daily Status Update — 27 Mar 2026

Luna Project (3 tasks)
  • [In Progress] Dashboard redesign — Priya (60%)
  • [In Progress] API integration — Suresh (30%)
  • [Open] Report module — Unassigned (0%)

Fenner Project (2 tasks)
  • [In Progress] BPM Receipt Entry — Anbu (80%)
  • [Open] Labour Hire PO fix — Unassigned (0%)
```

**Business reason:**
Requested directly — "share the open and in-progress tasks — a separate message by projects in the main group or status update chat group, so Arul knows where we are in individual projects. Usually he will look in the morning: What is the status of Luna tasks?"

---

### Feature 15 — Local Interactive Report Server

**What it does:**
Running `preview_report.py` starts a local web server on port 8766 and opens an interactive HTML report in the browser. From this report, anyone can post comments directly to Zoho Projects without opening Zoho.

**How inline commenting works:**
```
Click "Add Comment" on any task card
         ↓
Type comment in the text box
         ↓
Click "Post to Zoho ✓"
         ↓
Browser JS sends to localhost:8766/api/comment
         ↓
Python server forwards to Zoho Projects API
         ↓
Comment appears live in Zoho Projects
         ↓
New comment shown on the task card immediately
```

**Additional controls:**
- **↻ Refresh Data** — re-fetches all tasks and comments without restarting the server
- **Open in Zoho ↗** — per-task link that opens the task directly in Zoho Projects

**Business reason:**
Provides a single-page consolidated view of all tasks with inline commenting — faster than navigating Zoho Projects across multiple projects.

---

### Feature 16 — Automated Daily Schedule (GitHub Actions)

**What it does:**
The entire agent runs automatically every day at **8:00 AM IST** on GitHub's cloud servers — even when the local PC is switched off.

**Manual trigger:**
Can also be triggered manually at any time from the GitHub Actions tab → Run workflow button.

**Business reason:**
Requested directly — "I need it to run also when the PC is in off condition." GitHub Actions provides free, reliable cloud compute for this.

---

### Feature 17 — Secure Credential Storage

**What it does:**
No passwords or API keys are ever stored in plain text in code or committed to GitHub.

**Storage method:**
| Credential | Storage Location |
|---|---|
| Gmail App Password (local) | Windows Credential Manager via `keyring` |
| All secrets (cloud) | GitHub Secrets (AES-256 encrypted) |
| Zoho tokens | `.env` file (in `.gitignore`) + GitHub Secrets |

**Business reason:**
Protects the organisation's API credentials, email accounts, and Zoho portal from exposure if the code repository is ever accessed by an unauthorised person.

---

### Feature Summary Table

| # | Feature | Requested By | Status |
|---|---|---|---|
| 1 | Fetch all tasks from all projects | Core requirement | ✅ Live |
| 2 | 7 intelligent comment types via GPT-4o | Core requirement | ✅ Live |
| 3 | Comment once per status (no repetition) | User feedback | ✅ Live |
| 4 | Open tasks not nagged | User feedback | ✅ Live |
| 5 | Testing status checklist | User feedback | ✅ Live |
| 6 | Feedback loop (human reply detection) | User feedback | ✅ Live |
| 7 | No-reply escalation (48h rule) | Core requirement | ✅ Live |
| 8 | Skip closed / UAT / completed tasks | User feedback | ✅ Live |
| 9 | 24-hour cooldown guard | Core requirement | ✅ Live |
| 10 | Unassigned task detection + smart email | User request | ✅ Live |
| 11 | Smart escalation routing (Siva/Dhinesh) | User request | ✅ Live |
| 12 | Daily HTML email report (per-person) | User request | ✅ Live |
| 13 | Zoho Cliq: For Testing notification | User request | ✅ Live |
| 14 | Zoho Cliq: Daily status update for Arul | User request | ✅ Live |
| 15 | Local interactive report server | User request | ✅ Live |
| 16 | Automated daily schedule (GitHub Actions) | User request | ✅ Live |
| 17 | Secure credential storage | Core requirement | ✅ Live |

---

*Zoho Projects AI Agent — Tools, Technologies & Features Reference*
*Built for Mithilai Solutions | Automated with GitHub Actions + OpenAI GPT-4o*
