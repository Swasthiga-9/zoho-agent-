# Zoho Projects AI Agent — Tools & Technologies Reference

**Organisation:** Mithilai Solutions
**Version:** 1.0
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

*Zoho Projects AI Agent — Tools & Technologies Reference*
*Built for Mithilai Solutions | Automated with GitHub Actions + OpenAI GPT-4o*
