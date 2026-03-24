# Zoho Projects Support Agent — Technical Brief

## Architecture Overview

A Python-based automation agent that connects to Zoho Projects via REST API, uses OpenAI GPT-4o-mini for task analysis, and posts structured HTML comment boxes back into each Zoho task.

---

## Stack

| Component      | Technology                                  |
|----------------|---------------------------------------------|
| Runtime        | Python 3.11+, `asyncio`                     |
| AI Analysis    | OpenAI `gpt-4o-mini` via `AsyncOpenAI`      |
| Project Data   | Zoho Projects REST API (India region)       |
| Auth           | Zoho OAuth2 refresh token flow              |
| Email Alerts   | Gmail SMTP SSL (App Password required)      |
| Scheduling     | Windows Task Scheduler / cron               |

---

## How It Works (End-to-End)

```
Startup
  └─ Refresh Zoho OAuth2 access token (short-lived, refreshed each run)

Fetch Tasks
  └─ GET /mytasks/ → returns tasks across ALL projects in one call
  └─ Filter out closed/completed/deployed tasks
  └─ Result: ~31 active tasks across 5 projects

Per Task (concurrent, semaphore-limited to 5 at a time)
  ├─ Cooldown check: skip if bot posted in last 24h (prevents spam)
  ├─ Build snapshot: priority, overdue status, last-update age, log hours,
  │   description, comment history, tasklist, billing type
  ├─ Send snapshot → GPT-4o-mini with structured ANALYST_SYSTEM prompt
  ├─ Parse JSON response: which comment types to post + escalation flag
  └─ Post HTML comment boxes to Zoho task

Comment Types Posted
  ├─ Missing Info Box  → when task has no description/context
  ├─ Analytics Box     → progress %, days active, overdue warning
  ├─ Replan Box        → when task is overdue or HIGH priority + stalled
  └─ Digest Box        → periodic activity summary
```

---

## Priority-Aware Logic

```
HIGH   → Red border box, "URGENT:" / "ACTION REQUIRED:" prefix
         If also overdue → always triggers replan + escalation email

MEDIUM → Yellow/amber styling, professional tone

LOW    → Green/neutral styling, friendly check-in tone

NONE   → Default styling, minimal intervention
```

---

## Deduplication (Anti-Spam)

Every bot comment contains the marker string `— Zoho Agent •`.

Before posting, the agent:
1. Reads the task's full comment history
2. Checks if any comment containing `— Zoho Agent •` was posted within the last 24 hours
3. If yes → **skips** the task entirely (no duplicate post)
4. If no → proceeds with analysis and posting

This ensures that even if the script is run multiple times in a day, each task only receives **one bot comment per 24-hour window**.

---

## Key Files

| File                        | Purpose                                             |
|-----------------------------|-----------------------------------------------------|
| `main.py`                   | Core agent — fetch, analyse, post comments          |
| `dashboard.py`              | Local HTML Kanban board of all tasks                |
| `test_connection.py`        | Diagnostic — verify all API connections before run  |
| `.env`                      | Credentials and configuration                       |
| `requirements.txt`          | Python dependencies (`openai`, `httpx`, `python-dotenv`) |

---

## Environment Variables (`.env`)

| Variable                | Description                                         |
|-------------------------|-----------------------------------------------------|
| `ZOHO_REFRESH_TOKEN`    | Long-lived Zoho OAuth2 refresh token                |
| `ZOHO_CLIENT_ID`        | Zoho OAuth2 client ID                               |
| `ZOHO_CLIENT_SECRET`    | Zoho OAuth2 client secret                           |
| `ZOHO_PORTAL`           | Zoho portal name (e.g. `mithilai`)                  |
| `ZOHO_PROJECT_ID`       | Default project ID (used in test script)            |
| `OPENAI_API_KEY`        | OpenAI API key for GPT-4o-mini                      |
| `GMAIL_USER`            | Gmail address for sending escalation emails         |
| `GMAIL_APP_PASSWORD`    | 16-char Google App Password (not regular password)  |
| `SIVA_EMAIL`            | Escalation email recipient 1                        |
| `DHINESH_EMAIL`         | Escalation email recipient 2                        |
| `ESCALATION_DAYS`       | Days overdue before escalation triggers (default: 7)|
| `DIGEST_INACTIVITY_HOURS` | Hours of inactivity before digest posts (default: 24) |
| `KEYWORDS`              | Comma-separated keywords that flag a task as blocked|

---

## Running It

```bash
# Verify all connections first (recommended before first run)
python test_connection.py

# Run the agent (analyses all tasks, posts comments)
python main.py

# Open the visual Kanban dashboard in browser
python dashboard.py
```

---

## Automation — Windows Task Scheduler

To run the agent automatically every day at 9:00 AM:

1. Open **Task Scheduler** (search in Start Menu)
2. Click **Create Basic Task** in the right panel
3. **Name:** `Zoho Agent Daily Run`
4. **Trigger:** Daily → 9:00 AM
5. **Action:** Start a program
   - **Program/script:**
     ```
     C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe
     ```
   - **Add arguments:**
     ```
     C:\Users\Admin\zoho_agent\main.py
     ```
   - **Start in:**
     ```
     C:\Users\Admin\zoho_agent
     ```
6. Click **Finish**

The agent will now run silently every morning, analyse all open tasks, and post comments automatically.

---

## Zoho API Endpoints Used

| Endpoint                                                              | Purpose                    |
|-----------------------------------------------------------------------|----------------------------|
| `POST https://accounts.zoho.in/oauth/v2/token`                       | Refresh access token       |
| `GET /restapi/portal/{portal}/mytasks/`                               | Fetch all tasks (all projects) |
| `GET /restapi/portal/{portal}/projects/{pid}/tasks/`                  | Fetch tasks for one project |
| `GET /restapi/portal/{portal}/projects/{pid}/tasks/{tid}/comments/`   | Read task comments          |
| `POST /restapi/portal/{portal}/projects/{pid}/tasks/{tid}/comments/`  | Post comment to task        |

> **Region note:** This agent uses the **India region** (`zoho.in`). If your Zoho account is on a different region (EU, US, AU), update the base URLs accordingly.

---

## Active Projects (as of March 2026)

| Project Name          | Tasks |
|-----------------------|-------|
| DECHRA Conversion     | 19    |
| Mithilai Automation   | 8     |
| Team Development      | 2     |
| Signcraft             | 1     |
| Fenner-AI             | 1     |
| **Total**             | **31**|

---

## Pending / Next Steps

### Enable Email Escalation
The Gmail App Password in `.env` is currently a regular password (not valid for SMTP).

To enable escalation emails:
1. Go to [myaccount.google.com](https://myaccount.google.com) → **Security** → **App Passwords**
2. Create a new app password for **Mail**
3. Copy the 16-character code (format: `xxxx xxxx xxxx xxxx`)
4. Update `.env`:
   ```
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   ```

Once set, the agent will automatically email escalation alerts for:
- Tasks overdue by more than `ESCALATION_DAYS` (default: 7 days)
- HIGH priority tasks with no recent activity
- Tasks containing keywords: `blocked`, `urgent`, `overdue`, `review needed`, `stuck`, `waiting`, `delayed`

---

*Generated: March 2026 | Zoho Projects Support Agent v1.0*
