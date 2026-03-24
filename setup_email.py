"""
setup_email.py
==============
Run this ONCE to securely store the Gmail App Password in
Windows Credential Manager. The password is never written to
any file — it lives only in the OS keychain.

Usage:
    python setup_email.py           # store / update password
    python setup_email.py --test    # verify login works
    python setup_email.py --clear   # remove stored password
"""

import getpass
import smtplib
import sys
import keyring

SERVICE  = "zoho_agent_gmail"
USERNAME = "gmail_app_password"


def store():
    print("=" * 55)
    print("  Zoho Agent — Secure Email Credential Setup")
    print("=" * 55)
    print()
    print("Generate a Gmail App Password at:")
    print("  https://myaccount.google.com/apppasswords")
    print("  (Sign in → Security → App Passwords → Create)")
    print()
    pwd = getpass.getpass("Paste your Gmail App Password (input hidden): ")
    if not pwd.strip():
        print("[!] No password entered — aborted.")
        sys.exit(1)
    keyring.set_password(SERVICE, USERNAME, pwd.strip())
    print()
    print("[OK] Password stored in Windows Credential Manager.")
    print("     It is encrypted and never written to any file.")
    print()
    print("Run 'python setup_email.py --test' to verify it works.")


def test():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    gmail_user = os.getenv("GMAIL_USER", "")
    if not gmail_user:
        print("[!] GMAIL_USER not set in .env")
        sys.exit(1)

    pwd = keyring.get_password(SERVICE, USERNAME)
    if not pwd:
        print("[!] No password found. Run 'python setup_email.py' first.")
        sys.exit(1)

    print(f"Testing SMTP login for: {gmail_user} ...")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as s:
            s.login(gmail_user, pwd)
        print("[OK] Gmail login succeeded! Email is ready.")
    except Exception as e:
        print(f"[FAIL] {e}")
        print()
        print("If you see 'Application-specific password required':")
        print("  1. Go to https://myaccount.google.com/apppasswords")
        print("  2. Make sure 2-Step Verification is ON")
        print("  3. Create a new App Password, paste it when prompted")
        sys.exit(1)


def clear():
    try:
        keyring.delete_password(SERVICE, USERNAME)
        print("[OK] Password removed from Windows Credential Manager.")
    except keyring.errors.PasswordDeleteError:
        print("[!] No password found to delete.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--test" in args:
        test()
    elif "--clear" in args:
        clear()
    else:
        store()