# run_bot.py
# Supervisor wrapper — restarts the bot on crash with backoff.
# Sends Telegram notifications on crash, restart, and permanent shutdown.

import subprocess
import sys
import time
import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# =======================================================
# CONFIG
# =======================================================
MAX_RESTARTS     = 50
RESTART_DELAY    = 5
BACKOFF_MULTIPLY = 1.5
MAX_DELAY        = 120
LOG_FILE         = "bot_crashes.log"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# =======================================================
# TELEGRAM NOTIFICATIONS (supervisor-level)
# =======================================================
def notify(message, silent=False):
    """Send a supervisor-level notification to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_notification": silent,
            },
            timeout=5
        )
    except Exception as e:
        print(f"[Supervisor] Telegram notify failed: {e}")

# =======================================================
# CRASH LOGGING
# =======================================================
def log_crash(restart_count, error_msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] Restart #{restart_count} — {error_msg}\n")

def read_last_log_lines(n=15):
    """Read last n lines of bot.log for crash context."""
    if not os.path.exists("bot.log"):
        return ""
    try:
        with open("bot.log", "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return ""

# =======================================================
# MAIN LOOP
# =======================================================
def run():
    restart_count = 0
    delay = RESTART_DELAY
    first_run = True

    print("=" * 60)
    print("  Bot Supervisor — Starting")
    print(f"  Max restarts: {MAX_RESTARTS}")
    print(f"  Initial delay: {RESTART_DELAY}s")
    print("=" * 60 + "\n")

    while restart_count < MAX_RESTARTS:
        error_msg = "unknown"

        try:
            if first_run:
                print(f"[Supervisor] Starting bot (first run)...")
                first_run = False
            else:
                print(f"[Supervisor] Starting bot (attempt {restart_count + 1})...")

            result = subprocess.run(
                [sys.executable, "feed_fetcher.py"],
                cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
            )

            if result.returncode == 0:
                print("[Supervisor] Bot exited cleanly.")
                notify("🟢 <b>Bot exited cleanly</b>", silent=False)
                break

            if result.returncode == 42:
                print("[Supervisor] Emergency shutdown — not restarting.")
                notify("🚨 <b>Emergency shutdown</b>\nSupervisor stopped. Use terminal to restart.", silent=False)
                break

            if result.returncode == 43:
                print("[Supervisor] Restart requested via Telegram.")
                continue

            error_msg = f"Exit code {result.returncode}"
            print(f"[Supervisor] Bot crashed — {error_msg}")

        except KeyboardInterrupt:
            print("\n[Supervisor] Stopped by user.")
            notify("🛑 <b>Supervisor stopped by user</b>", silent=False)
            break

        except Exception as e:
            error_msg = str(e)
            print(f"[Supervisor] Error — {error_msg}")

        # Crash handling
        restart_count += 1
        log_crash(restart_count, error_msg)

        # Build crash notification with last log lines
        log_tail = read_last_log_lines(10)
        log_tail_formatted = log_tail.strip()[-800:] if log_tail else "(no log output)"

        crash_msg = (
            f"💥 <b>Bot crashed</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Restart: #{restart_count} / {MAX_RESTARTS}\n"
            f"Error: <code>{error_msg[:100]}</code>\n"
            f"Next retry in {delay:.0f}s\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Last log lines:</b>\n"
            f"<pre>{log_tail_formatted}</pre>"
        )
        notify(crash_msg, silent=False)

        if restart_count < MAX_RESTARTS:
            print(f"[Supervisor] Restarting in {delay:.0f}s... ({restart_count}/{MAX_RESTARTS})")
            time.sleep(delay)

            # Notify on restart
            notify(
                f"🔄 <b>Bot restarting</b>\n"
                f"Attempt {restart_count + 1}/{MAX_RESTARTS}",
                silent=True
            )

            delay = min(delay * BACKOFF_MULTIPLY, MAX_DELAY)
        else:
            final_msg = (
                f"🚨 <b>SUPERVISOR GIVING UP</b>\n"
                f"Bot crashed {MAX_RESTARTS} times in a row.\n"
                f"Manual intervention required.\n"
                f"Last error: <code>{error_msg[:150]}</code>"
            )
            print(f"[Supervisor] Max restarts reached ({MAX_RESTARTS}). Stopping.")
            notify(final_msg, silent=False)

if __name__ == "__main__":
    run()