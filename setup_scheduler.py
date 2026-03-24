"""
setup_scheduler.py
==================
Run this ONCE to register a Windows Task Scheduler job that runs the
Zoho Agent every day at 8:00 AM.

Usage:
    python setup_scheduler.py              # register / update daily 8 AM job
    python setup_scheduler.py --delete     # remove the scheduled task
    python setup_scheduler.py --run-now    # trigger the task immediately (test)
    python setup_scheduler.py --status     # show task info
"""

import subprocess
import sys
import os
from pathlib import Path

TASK_NAME   = "ZohoProjectsAgent"
PYTHON_EXE  = r"C:\Python314\python.exe"
SCRIPT_PATH = Path(__file__).parent / "main.py"
RUN_TIME    = "08:00"          # 24h format  — change if you prefer another time
RUN_DAY     = "MON,TUE,WED,THU,FRI,SAT,SUN"   # every day; use e.g. "MON-FRI" for weekdays only


def run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    return r.returncode, out


def register():
    print(f"Registering Windows Task Scheduler job: {TASK_NAME}")
    print(f"  Script : {SCRIPT_PATH}")
    print(f"  Python : {PYTHON_EXE}")
    print(f"  Time   : {RUN_TIME} every day")

    # Delete if already exists (so we can update)
    run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])

    rc, out = run([
        "schtasks", "/Create",
        "/TN",  TASK_NAME,
        "/TR",  f'"{PYTHON_EXE}" "{SCRIPT_PATH}"',
        "/SC",  "DAILY",
        "/ST",  RUN_TIME,
        "/F",                       # force overwrite
    ])
    if rc == 0:
        print(f"\n[OK] Task '{TASK_NAME}' scheduled successfully.")
        print(f"  It will run every day at {RUN_TIME}.")
        print(f"  Logs -> {SCRIPT_PATH.parent / 'logs' / 'agent.log'}")
    else:
        print(f"\n[FAIL] Failed to create task:\n{out}")
        sys.exit(1)


def delete():
    rc, out = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if rc == 0:
        print(f"[OK] Task '{TASK_NAME}' deleted.")
    else:
        print(f"[FAIL] Could not delete task:\n{out}")


def run_now():
    print(f"Running task '{TASK_NAME}' immediately...")
    rc, out = run(["schtasks", "/Run", "/TN", TASK_NAME])
    if rc == 0:
        print(f"[OK] Task started. Check logs at:")
        print(f"  {SCRIPT_PATH.parent / 'logs' / 'agent.log'}")
    else:
        print(f"[FAIL] Failed:\n{out}")


def status():
    rc, out = run(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"])
    if rc == 0:
        print(out)
    else:
        print(f"Task '{TASK_NAME}' not found or error:\n{out}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--delete" in args:
        delete()
    elif "--run-now" in args:
        run_now()
    elif "--status" in args:
        status()
    else:
        register()