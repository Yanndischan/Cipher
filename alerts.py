# alerts.py — Cipher Bot Telegram interface (clean rebuild)
# Fixes:
#   - Callback queries (button presses) now properly routed
#   - /balance always reads same source as /status (executor paper_balance in paper mode)
#   - /card runs in dedicated thread (never blocks polling loop)
#   - Photo + .ttf upload state correctly tracked
#   - All previous command handlers preserved

import io
import os
import csv
import sys
import re
import json
import time
import socket
import subprocess
import requests
from threading       import Thread, Lock
from datetime        import datetime
from web3            import Web3

import config
from logger import log

# =======================================================
# STATE
# =======================================================
last_alert_time     = 0
alert_lock          = Lock()

# ROOT CAUSE of "/stop doesn't stick": this flag was in-memory only, and
# run_bot.py restarts feed_fetcher.py on every crash (bot.log shows
# multiple "Signal detector started" within hours). Every restart reset
# trading_active to True, so a confirmed /stop was silently undone
# minutes later and trading resumed with no notice. The pause state is
# now persisted to a marker file and restored on startup.
_PAUSE_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".trading_paused")
trading_active      = not os.path.exists(_PAUSE_MARKER)
polling_active      = False
is_standalone       = False
wallet_deposits     = 0.0
wallet_withdrawals  = 0.0

_TRADE_LOG_FILE     = config.get_log_file()
_funnel_ref         = None
_executor_ref       = None
_heartbeat_interval = 3600
_last_heartbeat     = 0
_bot_start_time     = time.time()

# Pending withdrawal — (to_address, amount) awaiting CONFIRM
_pending_withdrawal     = None
_pending_withdrawal_ts  = 0
WITHDRAWAL_CONFIRM_TIMEOUT = 120

# Upload state for /card customisation
_upload_state = None    # "bg" (background) | "font" (.ttf) | None

# Directory where the bot files live (dynamic, not hardcoded)
BOT_DIR = os.path.dirname(os.path.abspath(__file__))

# =======================================================
# REGISTRATION
# =======================================================
def set_log_file(fp):
    global _TRADE_LOG_FILE
    _TRADE_LOG_FILE = fp

def set_funnel_ref(d):
    global _funnel_ref
    _funnel_ref = d

def set_executor_ref(m):
    global _executor_ref
    _executor_ref = m

def get_funnel_data():
    return _funnel_ref

def is_trading_active():
    return trading_active

# =======================================================
# TELEGRAM HELPERS
# =======================================================
def send(message, silent=False, force=False, buttons=None):
    """Send a text message. Optionally with inline keyboard buttons."""
    global last_alert_time
    if not config.TELEGRAM_ENABLED:
        return False
    if not force:
        with alert_lock:
            now = time.time()
            if now - last_alert_time < config.MIN_ALERT_INTERVAL:
                return False
            last_alert_time = now
    try:
        payload = {
            "chat_id"             : config.TELEGRAM_CHAT_ID,
            "text"                : message,
            "parse_mode"          : "HTML",
            "disable_notification": silent,
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": lbl, "callback_data": cb} for lbl, cb in row]
                    for row in buttons
                ]
            }
        resp = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload, timeout=5
        )
        return resp.status_code == 200
    except Exception:
        return False

def _answer_cb(callback_query_id, text=""):
    """Acknowledge a callback query immediately (removes spinner)."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=5
        )
    except Exception:
        pass

def _delete_msg(message_id):
    if not message_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/deleteMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "message_id": message_id},
            timeout=5
        )
    except Exception:
        pass

def _send_photo(jpg_bytes, caption=""):
    """Send JPEG/PNG bytes as a photo."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id": config.TELEGRAM_CHAT_ID, "caption": caption},
            files={"photo": ("card.jpg", jpg_bytes, "image/jpeg")},
            timeout=30
        )
        return resp.status_code == 200
    except Exception as e:
        log.error(f"sendPhoto failed: {e}")
        return False

# =======================================================
# STATS
# =======================================================
def get_performance_stats(mode=None):
    """
    Returns stats for ONE explicit book — paper or live — never blended.
    If mode is not given, defaults to what config.MODE implies:
      "paper" -> paper book, "live" -> live book, "both" -> live book
      (since that's the one involving real money and most needs visibility;
      callers that want the paper side of a "both" run should ask for it
      explicitly with mode="paper").
    """
    if mode is None:
        mode = "paper" if config.MODE == "paper" else "live"

    if _executor_ref is not None:
        return _stats_from_executor(mode)
    return _stats_from_csv(mode)

def get_live_balance():
    """Real Polymarket USDC balance. None if it can't be fetched right now —
    callers must handle that rather than silently showing a stale/wrong number."""
    if _executor_ref is not None and hasattr(_executor_ref, "get_usdc_balance"):
        try:
            return _executor_ref.get_usdc_balance()
        except Exception:
            return None
    return None

def get_display_balance(mode=None):
    """The balance to show for a given book. Live -> real balance (falls
    back to paper only if the real fetch fails, so something is shown
    rather than nothing — but that fallback case should be rare and is
    worth investigating if it happens often). Paper -> paper balance."""
    if mode is None:
        mode = "paper" if config.MODE == "paper" else "live"
    if mode == "live":
        bal = get_live_balance()
        if bal is not None:
            return bal, True   # (balance, is_real)
        return get_paper_balance(), False
    return get_paper_balance(), True

def _stats_from_executor(mode):
    closed = [t for t in _executor_ref.closed_trades if t.get("mode") == mode]
    open_n = len([t for t in _executor_ref.open_positions if t.get("mode") == mode])
    if not closed:
        return None
    wins   = [t for t in closed if t.get("result") == "WIN"]
    losses = [t for t in closed if t.get("result") != "WIN"]
    total_pnl    = sum(float(t.get("pnl", 0) or 0) for t in closed)
    total_staked = sum(float(t.get("stake", 0) or 0) for t in closed)
    avg_win  = sum(float(t.get("pnl", 0) or 0) for t in wins)   / len(wins)   if wins   else 0
    avg_loss = sum(float(t.get("pnl", 0) or 0) for t in losses) / len(losses) if losses else 0

    best_s, worst_s, cl, ct = 0, 0, 0, None
    for t in closed:
        s = "WIN" if t.get("result") == "WIN" else "LOSS"
        if s == ct:
            cl += 1
        else:
            ct, cl = s, 1
        if ct == "WIN":
            best_s  = max(best_s,  cl)
        else:
            worst_s = max(worst_s, cl)

    symbols = {}
    for t in closed:
        sym = t.get("symbol", "?")
        d = symbols.setdefault(sym, {"wins": 0, "losses": 0, "pnl": 0.0})
        if t.get("result") == "WIN":
            d["wins"] += 1
        else:
            d["losses"] += 1
        d["pnl"] += float(t.get("pnl", 0) or 0)

    balance, is_real = get_display_balance(mode)

    return {
        "mode"         : mode,
        "total"        : len(closed),
        "open"         : open_n,
        "wins"         : len(wins),
        "losses"       : len(losses),
        "win_rate"     : (len(wins) / len(closed) * 100) if closed else 0,
        "total_pnl"    : total_pnl,
        "total_staked" : total_staked,
        "roi"          : (total_pnl / total_staked * 100) if total_staked > 0 else 0,
        "avg_win"      : avg_win,
        "avg_loss"     : avg_loss,
        "balance"      : balance,
        "balance_is_real": is_real,
        "symbols"      : symbols,
        "best_streak"  : best_s,
        "worst_streak" : worst_s,
    }

def _stats_from_csv(mode):
    if not os.path.exists(_TRADE_LOG_FILE):
        return None
    try:
        resolved, seen, balance = [], set(), config.PAPER_BALANCE
        with open(_TRADE_LOG_FILE, "r", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("mode") != mode:
                    continue
                r = row.get("result", "OPEN")
                if r in ("WIN", "LOSS", "EARLY_EXIT") or "TIMEOUT" in r or "FLAT" in r:
                    seen.add(row.get("trade_id", ""))
                    resolved.append(row)
                    # No live executor available in this fallback path, so
                    # we can't fetch a real balance — best available
                    # substitute is the last balance_after this book wrote,
                    # which for live rows is often blank (live doesn't
                    # track a running balance_after the way paper does).
                    try:
                        b = float(row.get("balance_after", 0))
                        if b > 0:
                            balance = b
                    except Exception:
                        pass

        oc = 0
        with open(_TRADE_LOG_FILE, "r", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("mode") == mode and row.get("result", "OPEN") == "OPEN" and row.get("trade_id") not in seen:
                    oc += 1

        if not resolved:
            return None

        wins   = [t for t in resolved if t["result"] == "WIN"]
        losses = [t for t in resolved if t["result"] != "WIN"]
        tp = sum(float(t.get("pnl", 0)   or 0) for t in resolved)
        ts = sum(float(t.get("stake", 0) or 0) for t in resolved)
        aw = sum(float(t.get("pnl", 0) or 0) for t in wins)   / len(wins)   if wins   else 0
        al = sum(float(t.get("pnl", 0) or 0) for t in losses) / len(losses) if losses else 0

        bs, ws, cl, ct = 0, 0, 0, None
        for t in resolved:
            s = "WIN" if t["result"] == "WIN" else "LOSS"
            if s == ct:
                cl += 1
            else:
                ct, cl = s, 1
            if ct == "WIN":
                bs = max(bs, cl)
            else:
                ws = max(ws, cl)

        syms = {}
        for t in resolved:
            sym = t.get("symbol", "?")
            d = syms.setdefault(sym, {"wins": 0, "losses": 0, "pnl": 0.0})
            if t["result"] == "WIN":
                d["wins"] += 1
            else:
                d["losses"] += 1
            try:
                d["pnl"] += float(t.get("pnl", 0))
            except Exception:
                pass

        if mode == "live":
            live_bal = get_live_balance()
            balance_is_real = live_bal is not None
            if live_bal is not None:
                balance = live_bal
        else:
            balance_is_real = True

        return {
            "mode"         : mode,
            "total"        : len(resolved),
            "open"         : oc,
            "wins"         : len(wins),
            "losses"       : len(losses),
            "win_rate"     : len(wins) / len(resolved) * 100,
            "total_pnl"    : tp,
            "total_staked" : ts,
            "roi"          : (tp / ts * 100) if ts > 0 else 0,
            "avg_win"      : aw,
            "avg_loss"     : al,
            "balance"      : balance,
            "balance_is_real": balance_is_real,
            "symbols"      : syms,
            "best_streak"  : bs,
            "worst_streak" : ws,
        }
    except Exception:
        return None

def count_open_positions(mode=None):
    if mode is None:
        mode = "paper" if config.MODE == "paper" else "live"
    if _executor_ref:
        return len([t for t in _executor_ref.open_positions if t.get("mode") == mode])
    if not os.path.exists(_TRADE_LOG_FILE):
        return 0
    try:
        ri, oi = set(), set()
        with open(_TRADE_LOG_FILE, "r", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("mode") != mode:
                    continue
                tid = row.get("trade_id", "")
                if row.get("result", "OPEN") != "OPEN":
                    ri.add(tid)
                else:
                    oi.add(tid)
        return len(oi - ri)
    except Exception:
        return 0

def get_paper_balance():
    """Single source of truth for the paper balance — used by /balance, /status, heartbeat."""
    if _executor_ref is not None:
        return _executor_ref.paper_balance
    return config.PAPER_BALANCE

# =======================================================
# COMMAND HANDLERS
# =======================================================

def _format_performance_block(s, label):
    balance_flag = "" if s.get("balance_is_real", True) else " ⚠️ (real balance fetch failed, showing paper)"
    msg = (
        f"📊 <b>{label}</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"Resolved: {s['total']} ({s['open']} open)\n"
        f"Wins: {s['wins']}  Losses: {s['losses']}\n"
        f"Win rate: <b>{s['win_rate']:.1f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"P&L: <b>${s['total_pnl']:+.2f}</b>  ROI: {s['roi']:+.1f}%\n"
        f"Staked: ${s['total_staked']:.2f}\n"
        f"Avg win: ${s['avg_win']:+.2f}  Avg loss: ${s['avg_loss']:+.2f}\n"
        f"Best streak: {s['best_streak']}  Worst: {s['worst_streak']}\n"
        f"Balance: <b>${s['balance']:.2f}</b>{balance_flag}\n"
    )
    if s["symbols"]:
        msg += "\n<b>By symbol:</b>\n"
        for sym, d in sorted(s["symbols"].items(), key=lambda x: -x[1]["pnl"]):
            tot = d["wins"] + d["losses"]
            wr  = (d["wins"] / tot * 100) if tot else 0
            msg += f"  {sym.upper()}: {d['wins']}W/{d['losses']}L ({wr:.0f}%) ${d['pnl']:+.2f}\n"
    return msg

def handle_performance():
    if config.MODE == "both":
        # Two separate books, never blended -- collapsing them into one
        # number would hide exactly the thing this command exists to show.
        live_s  = get_performance_stats(mode="live")
        paper_s = get_performance_stats(mode="paper")
        if not live_s and not paper_s:
            send("📊 <b>Performance</b>\nNo trades yet.\n\n"
                 f"Mode: {config.MODE}", force=True)
            return
        parts = []
        if live_s:
            parts.append(_format_performance_block(live_s, "LIVE Performance"))
        else:
            parts.append("📊 <b>LIVE Performance</b>\nNo live trades yet.")
        if paper_s:
            parts.append(_format_performance_block(paper_s, "PAPER Performance"))
        send("\n\n".join(parts), force=True)
        return

    s = get_performance_stats()
    if not s:
        label = "LIVE" if config.MODE == "live" else "Paper"
        send(f"📊 <b>{label} Performance</b>\nNo trades yet.\n\n"
             f"Open: {count_open_positions()} | Mode: {config.MODE}", force=True)
        return
    label = "LIVE Performance" if config.MODE == "live" else "Paper Performance"
    send(_format_performance_block(s, label), force=True)

def handle_signal_info():
    f = _funnel_ref
    if not f:
        send("ℹ️ No signal data yet.", force=True)
        return
    msg = (
        f"ℹ️ <b>Delta-to-Open Funnel</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"Signals fired:    {f.get('signals_fired', 0)}\n"
        f"├ Cooldown:       {f.get('cooldown_blocked', 0)}\n"
        f"├ No market:      {f.get('no_market_found', 0)}\n"
        f"├ Not accepting:  {f.get('not_accepting', 0)}\n"
        f"├ Missing prices: {f.get('missing_prices', 0)}\n"
  f"├ Wick grace pd:  {f.get('wick_grace_period', 0)}\n"
        f"├ Too early (T-20):{f.get('time_too_early', 0)}\n"
        f"├ Time left floor: {f.get('time_left_floor', 0)}\n"
        f"├ Entry &gt; max:    {f.get('entry_too_high', 0)}\n"
        f"├ Entry &lt; floor:  {f.get('entry_too_low', 0)}\n"
        f"├ Score too low:  {f.get('score_too_low', 0)}\n"
        f"├ Trend rejected: {f.get('trend_rejected', 0)}\n"
        f"├ Position cap:   {f.get('position_cap', 0)}\n"
        f"├ Symbol cap:     {f.get('symbol_cap', 0)}\n"
        f"├ Duplicate:      {f.get('duplicate_blocked', 0)}\n"
        f"├ KeepLook refused:{f.get('keep_looking_refused', 0)}\n"
        f"├ Attempted:     {f.get('entries_taken', 0)}  <i>(sent to executor, not necessarily real)</i>\n"
        f"└ <b>Confirmed:   {f.get('confirmed_entries', 0)}</b>  <i>(actually opened)</i>\n"
        + (f"  ⚠️ <b>{f.get('confirmed_but_paper_fallback', 0)} of those fell back to PAPER</b> "
           f"(live order failed)\n" if f.get("confirmed_but_paper_fallback", 0) > 0 else "")
    )
    send(msg, force=True)

def handle_start_trading():
    global trading_active
    trading_active = True
    try:
        if os.path.exists(_PAUSE_MARKER):
            os.remove(_PAUSE_MARKER)
    except Exception as e:
        log.error(f"Could not remove pause marker (state may not survive a restart): {e}")
    send("🟢 <b>Trading ACTIVE</b>\nBot will enter new positions.\n<i>(persists across restarts)</i>", force=True)
    log.info("Trading activated via Telegram")

def handle_stop_trading():
    global trading_active
    trading_active = False
    try:
        with open(_PAUSE_MARKER, "w") as f:
            f.write(datetime.now().isoformat())
        persisted = True
    except Exception as e:
        persisted = False
        log.error(f"Could not write pause marker — pause will NOT survive a restart: {e}")
    send(
        "🔴 <b>Trading PAUSED</b>\nOpen positions still resolve.\nUse /start to resume.\n"
        + ("<i>(persists across restarts)</i>" if persisted
           else "⚠️ <b>Could not persist — a bot restart will silently resume trading!</b>"),
        force=True
    )
    log.info("Trading paused via Telegram (persisted)" if persisted else "Trading paused via Telegram (NOT persisted!)")

def handle_deposit(arg):
    if not _executor_ref:
        send("⚠️ Bot not initialized.", force=True)
        return
    address = _executor_ref.get_wallet_address()
    if not address:
        send("⚠️ No private key configured. Add WALLET_ADDRESS to .env.", force=True)
        return

    # /deposit confirm <amount>
    parts = arg.strip().lower().split()
    if parts and parts[0] == "confirm" and len(parts) > 1:
        global wallet_deposits
        try:
            amount = float(parts[1])
            if amount > 0:
                wallet_deposits += amount
                send(f"✅ Deposit logged: ${amount:.2f}", force=True)
                return
        except ValueError:
            pass

    # MetaMask balance for raw wallet display
    usdc = _executor_ref.get_metamask_balance() if hasattr(_executor_ref, 'get_metamask_balance') else _executor_ref.get_usdc_balance(address)
    pol  = _executor_ref.get_pol_balance(address)
    usdc_s = f"${usdc:.2f}" if usdc is not None else "unavailable"
    pol_s  = f"{pol:.4f} POL" if pol is not None else "unavailable"

    send(
        f"💰 <b>Deposit Address</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"Send <b>USDC</b> on <b>Polygon</b> to:\n\n"
        f"<code>{address}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Current balance: {usdc_s}\n"
        f"Gas (POL): {pol_s}\n\n"
        f"⚠️ <b>Polygon network only.</b>\n"
        f"Sending on Ethereum or other networks will result in permanent loss.\n\n"
        f"After depositing, use:\n"
        f"<code>/deposit confirm 50</code>\n"
        f"to log the amount in your tracker.",
        force=True
    )

def handle_withdraw(arg):
    """Two-step withdraw flow."""
    global _pending_withdrawal, _pending_withdrawal_ts, wallet_withdrawals

    if arg.strip().upper() == "CONFIRM":
        if _pending_withdrawal is None:
            send("⚠️ No pending withdrawal. Use:\n<code>/withdraw 0xAddress 25</code>", force=True)
            return
        if time.time() - _pending_withdrawal_ts > WITHDRAWAL_CONFIRM_TIMEOUT:
            _pending_withdrawal = None
            send("⚠️ Withdrawal expired (2 min timeout). Please start again.", force=True)
            return

        to_address, amount = _pending_withdrawal
        _pending_withdrawal = None
        send(f"⏳ <b>Sending ${amount:.2f} USDC to</b>\n<code>{to_address}</code>\n\nPlease wait...", force=True)

        def do_transfer():
            global wallet_withdrawals
            result = _executor_ref.send_usdc(to_address, amount)
            if result["ok"]:
                wallet_withdrawals += amount
                tx_url = f"https://polygonscan.com/tx/{result['tx_hash']}"
                send(
                    f"✅ <b>Withdrawal sent!</b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"Amount: <b>${amount:.2f} USDC</b>\n"
                    f"To: <code>{to_address}</code>\n"
                    f"Tx: <a href='{tx_url}'>{result['tx_hash'][:20]}...</a>\n\n"
                    f"Confirm on Polygonscan in ~30 seconds.",
                    force=True
                )
                log.info(f"Withdrawal: ${amount:.2f} USDC to {to_address} | {result['tx_hash']}")
            else:
                send(f"❌ <b>Withdrawal failed</b>\n{result['error']}", force=True)
                log.error(f"Withdrawal failed: {result['error']}")

        Thread(target=do_transfer, daemon=True).start()
        return

    parts = arg.strip().split()
    if len(parts) < 2:
        send(
            "💸 <b>Withdraw USDC</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Usage:\n<code>/withdraw 0xYourAddress 25</code>\n\n"
            "Sends 25 USDC to the address on Polygon.\n"
            "You will be asked to confirm before transfer executes.",
            force=True
        )
        return

    to_address = parts[0]
    try:
        amount = float(parts[1])
    except ValueError:
        send("⚠️ Invalid amount. Use a number like <code>25</code>.", force=True)
        return

    if amount < config.MIN_WITHDRAWAL_USDC:
        send(f"⚠️ Minimum is ${config.MIN_WITHDRAWAL_USDC:.2f}", force=True)
        return

    if not Web3.is_address(to_address):
        send(f"⚠️ Invalid Polygon address:\n<code>{to_address}</code>", force=True)
        return

    usdc = _executor_ref.get_usdc_balance()
    pol  = _executor_ref.get_pol_balance()

    if usdc is not None and amount > usdc:
        send(f"❌ Insufficient balance.\nRequested: ${amount:.2f}\nAvailable: ${usdc:.2f}", force=True)
        return
    if pol is not None and pol < config.MIN_GAS_POL:
        send(f"⚠️ Low POL gas: {pol:.4f}. Top up at least {config.MIN_GAS_POL} POL.", force=True)

    _pending_withdrawal = (to_address, amount)
    _pending_withdrawal_ts = time.time()

    usdc_s = f"${usdc:.2f}" if usdc is not None else "unknown"
    send(
        f"💸 <b>Confirm Withdrawal</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"Amount: <b>${amount:.2f} USDC</b>\n"
        f"To: <code>{to_address}</code>\n"
        f"Available: {usdc_s}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Cannot be undone.\n"
        f"To confirm: <code>/withdraw CONFIRM</code>\n"
        f"Expires in 2 minutes.",
        force=True
    )

def handle_emergency(arg):
    if arg.strip() != "CONFIRM":
        send(
            "🚨 <b>Emergency Shutdown</b>\n\n"
            "This kills the bot. Open positions will NOT close.\n\n"
            "To confirm: <code>/emergency CONFIRM</code>",
            force=True
        )
        return
    send("🚨 <b>EMERGENCY SHUTDOWN</b>\nBot stopping. Restart manually.", force=True)
    log.warning("EMERGENCY SHUTDOWN triggered via Telegram")
    time.sleep(2)
    os._exit(42)

def handle_restart(arg):
    if arg.strip() != "CONFIRM":
        send(
            "🔄 <b>Restart Bot</b>\n\n"
            "This will restart the bot process.\n"
            "Open positions will remain active and resolve normally.\n\n"
            "To confirm: <code>/restart CONFIRM</code>",
            force=True
        )
        return
    send("🔄 <b>RESTARTING</b>\nBot is restarting. Please wait...", force=True)
    log.info("Restart triggered via Telegram")
    time.sleep(2)
    os._exit(43)

def handle_positions():
    if not _executor_ref:
        send("⚠️ Bot not initialized.", force=True)
        return
    if not _executor_ref.open_positions:
        send("📋 <b>No open positions</b>", force=True)
        return

    msg = "📋 <b>Open Positions</b>\n━━━━━━━━━━━━━━━━━━\n"
    now = int(time.time())
    for t in _executor_ref.open_positions:
        try:
            from order_executor import get_end_timestamp
            end_ts = get_end_timestamp(t.get("end_date", ""))
            if end_ts > 0 and now > end_ts:
                status = f"ended {int((now - end_ts) / 60)}m ago"
            elif end_ts > 0:
                status = f"{int(end_ts - now)}s left"
            else:
                status = "?"
        except Exception:
            status = "?"

        icon = "📄" if t.get("mode") == "paper" else "💰"
        msg += (
            f"{icon} {t['trade_id']}: {t['symbol'].upper()} {t['interval']} "
            f"{t['direction']} @ {t['fill_price']:.3f} ${t['stake']:.2f} | {status}\n"
        )
    send(msg, force=True)

def handle_mode(arg):
    arg = arg.strip().lower()
    sizing = (
        f"Flat ${config.FLAT_STAKE:.2f}" if config.SIZING_MODE == "flat"
        else f"Kelly {int(config.KELLY_FRACTION*100)}%"
    )
    if not arg:
        send(
            f"🔀 <b>Current mode: {config.MODE.upper()}</b>\n"
            f"Sizing: {sizing}\nSlippage: {config.SLIPPAGE_BPS/100:.1f}%\n\n"
            f"Change mode:\n<code>/mode paper</code>\n<code>/mode live</code>\n<code>/mode both</code>",
            force=True
        )
        return
    if arg in ("paper", "live", "both"):
        old = config.MODE
        config.MODE = arg
        saved = persist_config("MODE", arg)
        warn = "⚠️ Real money at stake!" if arg in ("live", "both") else "Paper only — safe."
        send(f"🔀 Mode changed: {old.upper()} → <b>{arg.upper()}</b>\n{warn}"
             f"{'' if saved else chr(10) + '⚠️ runtime only — will revert on restart'}", force=True)
        log.info(f"MODE changed: {old} -> {arg}")
    else:
        send("⚠️ Use: <code>/mode paper</code>, <code>/mode live</code>, or <code>/mode both</code>", force=True)

def handle_balance():
    """Single source of truth — same balance number everywhere."""
    if not _executor_ref:
        send("⚠️ Bot not initialized.", force=True)
        return

    paper_bal = get_paper_balance()
    p_pnl     = sum(float(t.get("pnl", 0) or 0) for t in _executor_ref.closed_trades if t.get("mode") == "paper")
    p_open    = sum(1 for t in _executor_ref.open_positions if t.get("mode") == "paper")

    msg = "💰 <b>Balance</b>\n━━━━━━━━━━━━━━━━━━\n"
    msg += (
        f"📄 <b>Paper</b>\n"
        f"Balance: <b>${paper_bal:.2f}</b>\n"
        f"P&L: ${p_pnl:+.2f}\n"
        f"Open: {p_open}\n"
    )

    # On-chain wallet — always visible regardless of mode
    usdc_chain = _executor_ref.get_metamask_balance() if hasattr(_executor_ref, 'get_metamask_balance') else None
    pol        = _executor_ref.get_pol_balance()
    usdc_chain_s = f"${usdc_chain:.2f}" if usdc_chain is not None else "unavailable"
    pol_s        = f"{pol:.4f} POL"     if pol        is not None else "unavailable"
    msg += (
        f"\n🔑 <b>Wallet (on-chain)</b>\n"
        f"USDC: {usdc_chain_s}\n"
        f"Gas (POL): {pol_s}\n"
    )

    if config.MODE in ("live", "both"):
        usdc   = _executor_ref.get_usdc_balance()
        usdc_s = f"${usdc:.2f}" if usdc is not None else "unavailable"
        l_pnl  = sum(float(t.get("pnl", 0) or 0) for t in _executor_ref.closed_trades if t.get("mode") == "live")
        l_open = sum(1 for t in _executor_ref.open_positions if t.get("mode") == "live")

        msg += (
            f"\n💰 <b>Live (Polymarket)</b>\n"
            f"pUSDC: {usdc_s}\n"
            f"P&L: ${l_pnl:+.2f}\n"
            f"Open: {l_open}\n"
            f"Daily spend: ${getattr(_executor_ref, 'live_daily_spent', 0):.2f}/${config.LIVE_MAX_DAILY:.2f}\n"
        )

    if wallet_deposits > 0 or wallet_withdrawals > 0:
        net = wallet_deposits - wallet_withdrawals
        msg += f"\n📥 Deposited: ${wallet_deposits:.2f}\n📤 Withdrawn: ${wallet_withdrawals:.2f}\nNet: ${net:.2f}\n"

    send(msg, force=True)

def persist_config(key, value):
    """
    Write a config value back to config.py so it SURVIVES A RESTART.
    Telegram changes were runtime-only: set sizing to kelly, bot restarts
    for any reason, and it silently reverts to whatever is on disk. That
    is how a live run ended up quietly using flat $3 sizing.

    Rewrites the LAST assignment of `key` at column 0 (config.py can have
    duplicate lines from layered deploys; Python's last-wins, so that is
    the line that actually takes effect). Returns True if written.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    try:
        with open(path, "r") as f:
            lines = f.readlines()

        literal = f'"{value}"' if isinstance(value, str) else repr(value)
        target = None
        for i, line in enumerate(lines):
            if re.match(rf"^{re.escape(key)}\s*=", line):
                target = i
        if target is None:
            return False

        comment = ""
        if "#" in lines[target]:
            comment = "  # " + lines[target].split("#", 1)[1].strip()
        lines[target] = f"{key} = {literal}{comment}\n"

        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(lines)
        os.replace(tmp, path)   # atomic; never leaves a half-written config
        return True
    except Exception as e:
        log.error(f"persist_config failed for {key}: {e}")
        return False


def handle_config(arg):
    parts = arg.strip().lower().split()
    if not parts:
        sizing = (
            f"Flat ${config.FLAT_STAKE:.2f}" if config.SIZING_MODE == "flat"
            else f"Kelly {int(config.KELLY_FRACTION*100)}%"
        )
        send(
            f"⚙️ <b>Config</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Mode: {config.MODE}\nSizing: {sizing}\n"
            f"Slippage: {config.SLIPPAGE_BPS} bps ({config.SLIPPAGE_BPS/100:.1f}%)\n"
            f"Max positions: {config.MAX_OPEN_POSITIONS} | Per symbol: {config.MAX_PER_SYMBOL}\n"
            f"Min signal score: {getattr(config, 'MIN_SIGNAL_SCORE', 40)}\n"
            f"Cooldown: {config.SIGNAL_COOLDOWN}s\n"
            f"Wick grace pd: {getattr(config, 'CANDLE_START_GRACE_SEC', 60)}s\n"
            f"Time guardian: {'ON' if getattr(config, 'TIME_GUARDIAN_ACTIVE', False) else 'OFF'}\n"
            f"Entry zone: {config.ENTRY_ZONE_MIN}-{config.ENTRY_ZONE_MAX} | Floor: {config.ENTRY_HARD_FLOOR}\n"
            f"Trend filter: {'ON' if config.TREND_FILTER_ACTIVE else 'OFF'}\n"
            f"Keep-looking: {'ON' if getattr(config, 'KEEP_LOOKING_ENABLED', False) else 'OFF'} | "
            f"Engages above maxentry ({config.ENTRY_ZONE_MAX}) | "
            f"Retrace {getattr(config, 'KEEP_LOOKING_MAX_RETRACE', 0.40)*100:.0f}% | "
            f"Timeout {getattr(config, 'KEEP_LOOKING_TIMEOUT_SEC', 45)}s\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Change with:</b>\n"
            f"<code>/config stake 5</code>  — flat stake\n"
            f"<code>/config slippage 200</code>  — bps\n"
            f"<code>/config maxpos 5</code>\n"
            f"<code>/config sizing flat</code> | <code>kelly25</code> | <code>kelly50</code> | <code>kelly100</code>\n"
            f"<code>/config trend on</code> | <code>off</code>\n"
            f"<code>/config minscore 40</code>\n"
            f"<code>/config grace 60</code>\n"
            f"<code>/config guardian on</code> | <code>off</code>\n"
            f"<code>/config floor 0.20</code>\n"
            f"<code>/config maxentry 0.75</code>  — also sets keep-looking's engage threshold\n"
            f"<code>/config keeplooking on</code> | <code>off</code>\n"
            f"<code>/config klretrace 40</code>  — percent\n"
            f"<code>/config kltimeout 45</code>  — seconds",
            force=True
        )
        return

    key = parts[0]
    val = parts[1] if len(parts) > 1 else ""

    if key == "stake":
        try:
            v = float(val)
            if not (0.5 <= v <= 500):
                send("⚠️ Stake must be $0.50–$500.", force=True); return
            old = config.FLAT_STAKE; config.FLAT_STAKE = v
            send(f"✅ Stake: ${old:.2f} → <b>${v:.2f}</b>", force=True)
            log.info(f"FLAT_STAKE {old} -> {v}")
        except ValueError:
            send("⚠️ Usage: <code>/config stake 5</code>", force=True)

    elif key == "slippage":
        try:
            v = int(val)
            if not (0 <= v <= 1000):
                send("⚠️ Must be 0-1000 bps.", force=True); return
            old = config.SLIPPAGE_BPS; config.SLIPPAGE_BPS = v
            send(f"✅ Slippage: {old} → <b>{v} bps ({v/100:.1f}%)</b>", force=True)
        except ValueError:
            send("⚠️ Usage: <code>/config slippage 200</code>", force=True)

    elif key == "maxpos":
        try:
            v = int(val)
            if not (1 <= v <= 100):
                send("⚠️ Must be 1-100.", force=True); return
            old = config.MAX_OPEN_POSITIONS; config.MAX_OPEN_POSITIONS = v
            send(f"✅ Max positions: {old} → <b>{v}</b>", force=True)
        except ValueError:
            send("⚠️ Usage: <code>/config maxpos 5</code>", force=True)

    elif key == "sizing":
        kelly_map = {"kelly25": 0.25, "kelly50": 0.50, "kelly100": 1.00, "kelly": 0.25}
        if val == "flat":
            old = config.SIZING_MODE; config.SIZING_MODE = "flat"
            saved = persist_config("SIZING_MODE", "flat")
            send(f"✅ Sizing: {old} → <b>Flat ${config.FLAT_STAKE:.2f}</b>"
                 f"{'  (saved to disk)' if saved else '  ⚠️ runtime only — will revert on restart'}", force=True)
        elif val in kelly_map:
            old = config.SIZING_MODE
            config.SIZING_MODE = "kelly"
            config.KELLY_FRACTION = kelly_map[val]
            s1 = persist_config("SIZING_MODE", "kelly")
            s2 = persist_config("KELLY_FRACTION", kelly_map[val])
            label = f"Kelly {int(config.KELLY_FRACTION*100)}%"
            send(f"✅ Sizing: {old} → <b>{label}</b>"
                 f"{'  (saved to disk)' if (s1 and s2) else '  ⚠️ runtime only — will revert on restart'}", force=True)
        else:
            send(
                "⚠️ Options:\n<code>/config sizing flat</code>\n"
                "<code>/config sizing kelly25</code>\n"
                "<code>/config sizing kelly50</code>\n"
                "<code>/config sizing kelly100</code>",
                force=True
            )

    elif key == "trend":
        if val in ("on", "true", "1"):
            config.TREND_FILTER_ACTIVE = True
            send("✅ Trend filter: <b>ON</b>", force=True)
        elif val in ("off", "false", "0"):
            config.TREND_FILTER_ACTIVE = False
            send("✅ Trend filter: <b>OFF</b>", force=True)
        else:
            send("⚠️ Usage: <code>/config trend on</code> or <code>off</code>", force=True)

    elif key == "minscore":
        try:
            v = int(val)
            if not (0 <= v <= 100):
                send("⚠️ Must be 0-100.", force=True); return
            old = getattr(config, 'MIN_SIGNAL_SCORE', 40); config.MIN_SIGNAL_SCORE = v
            send(f"✅ Min signal score: {old} → <b>{v}</b>", force=True)
        except ValueError:
            send("⚠️ Usage: <code>/config minscore 40</code>", force=True)

    elif key == "grace":
        try:
            v = int(val)
            if not (0 <= v <= 300):
                send("⚠️ Must be 0-300.", force=True); return
            old = getattr(config, 'CANDLE_START_GRACE_SEC', 60); config.CANDLE_START_GRACE_SEC = v
            send(f"✅ Wick grace period: {old}s → <b>{v}s</b>", force=True)
        except ValueError:
            send("⚠️ Usage: <code>/config grace 60</code>", force=True)

    elif key == "guardian":
        if val in ("on", "true", "1"):
            config.TIME_GUARDIAN_ACTIVE = True
            send("✅ Time guardian: <b>ON</b>", force=True)
        elif val in ("off", "false", "0"):
            config.TIME_GUARDIAN_ACTIVE = False
            send("✅ Time guardian: <b>OFF</b>", force=True)
        else:
            send("⚠️ Usage: <code>/config guardian on</code> or <code>off</code>", force=True)

    elif key == "floor":
        try:
            v = float(val)
            if not (0.01 <= v <= 0.50):
                send("⚠️ Must be 0.01-0.50.", force=True); return
            old = config.ENTRY_HARD_FLOOR; config.ENTRY_HARD_FLOOR = v
            send(f"✅ Entry floor: {old} → <b>{v}</b>", force=True)
        except ValueError:
            send("⚠️ Usage: <code>/config floor 0.20</code>", force=True)

    elif key == "maxentry":
        try:
            v = float(val)
            if not (0.30 <= v <= 0.95):
                send("⚠️ Must be 0.30-0.95.", force=True); return
            old = config.ENTRY_ZONE_MAX; config.ENTRY_ZONE_MAX = v
            send(f"✅ Max entry: {old} → <b>{v}</b>", force=True)
        except ValueError:
            send("⚠️ Usage: <code>/config maxentry 0.75</code>", force=True)

    elif key == "dailymax":
        try:
            v = float(val)
            if not (1.00 <= v <= 100000.00):
                send("⚠️ Must be 1.00-100000.00.", force=True); return
            old = config.LIVE_MAX_DAILY; config.LIVE_MAX_DAILY = v
            send(
                f"✅ Live daily max: ${old:.2f} → <b>${v:.2f}</b>\n"
                f"This is now also used as part of your live sizing bankroll "
                f"(min of this and your real balance) — set it deliberately.",
                force=True
            )
        except ValueError:
            send("⚠️ Usage: <code>/config dailymax 15</code>", force=True)

    elif key == "pollrate":
        try:
            v = float(val)
            if not (0.2 <= v <= 5.0):
                send("⚠️ Must be 0.2-5.0 seconds.", force=True); return
            old = getattr(config, "SIGNAL_POLL_INTERVAL_SEC", 2); config.SIGNAL_POLL_INTERVAL_SEC = v
            send(
                f"✅ Signal poll interval: {old}s → <b>{v}s</b>\n"
                f"Lower = detects delta crossings sooner (better entry timing on fast "
                f"moves). Binance open-price lookups are cached per candle, so this "
                f"is cheap to lower — takes effect on the detector's next loop.",
                force=True
            )
        except ValueError:
            send("⚠️ Usage: <code>/config pollrate 0.5</code>", force=True)

    elif key == "gap5":
        try:
            v = float(val)
            if not (0.01 <= v <= 2.0):
                send("⚠️ Must be 0.01-2.0 (percent).", force=True); return
            old = config.GAP_THRESHOLD_5M; config.GAP_THRESHOLD_5M = v
            saved = persist_config("GAP_THRESHOLD_5M", v)
            send(
                f"✅ 5m gap threshold: {old} → <b>{v}</b>\n"
                f"Lower = signals fire earlier in the move, while price is still cheap "
                f"— but on a less-confirmed signal. Higher = later, more confirmed, "
                f"more expensive."
                f"{'  (saved to disk)' if saved else '  ⚠️ runtime only — will revert on restart'}",
                force=True
            )
        except ValueError:
            send("⚠️ Usage: <code>/config gap5 0.08</code>", force=True)

    elif key == "gap15":
        try:
            v = float(val)
            if not (0.01 <= v <= 2.0):
                send("⚠️ Must be 0.01-2.0 (percent).", force=True); return
            old = config.GAP_THRESHOLD_15M; config.GAP_THRESHOLD_15M = v
            saved = persist_config("GAP_THRESHOLD_15M", v)
            send(
                f"✅ 15m gap threshold: {old} → <b>{v}</b>\n"
                f"Lower = signals fire earlier in the move, while price is still cheap "
                f"— but on a less-confirmed signal."
                f"{'  (saved to disk)' if saved else '  ⚠️ runtime only — will revert on restart'}",
                force=True
            )
        except ValueError:
            send("⚠️ Usage: <code>/config gap15 0.10</code>", force=True)

    elif key == "tminus":
        try:
            v = int(val)
            if not (1 <= v <= 300):
                send("⚠️ Must be 1-300 (seconds).", force=True); return
            old = config.ENTRY_T_MINUS_SEC; config.ENTRY_T_MINUS_SEC = v
            saved = persist_config("ENTRY_T_MINUS_SEC", v)
            send(
                f"✅ Entry T-minus: {old}s → <b>{v}s</b>\n"
                f"Higher = enters earlier in the candle, while price is still cheap "
                f"— but on a less-confirmed signal. This is the biggest lever for "
                f"lower entry prices."
                f"{'  (saved to disk)' if saved else '  ⚠️ runtime only — will revert on restart'}",
                force=True
            )
        except ValueError:
            send("⚠️ Usage: <code>/config tminus 60</code>", force=True)

    elif key == "realbook":
        if val in ("on", "true", "1"):
            config.PAPER_USE_REAL_BOOK = True
            saved = persist_config("PAPER_USE_REAL_BOOK", True)
            send(
                f"✅ Paper mode: real order book <b>ON</b>\n"
                f"Paper trades now check the real book before filling — a signal "
                f"paper takes is one live plausibly could have too."
                f"{'  (saved to disk)' if saved else '  ⚠️ runtime only — will revert on restart'}",
                force=True
            )
        elif val in ("off", "false", "0"):
            config.PAPER_USE_REAL_BOOK = False
            saved = persist_config("PAPER_USE_REAL_BOOK", False)
            send(
                f"✅ Paper mode: real order book <b>OFF</b>\n"
                f"Reverted to the old estimate-based fill (always succeeds) — "
                f"paper will again overstate what live could actually achieve."
                f"{'  (saved to disk)' if saved else '  ⚠️ runtime only — will revert on restart'}",
                force=True
            )
        else:
            send("⚠️ Usage: <code>/config realbook on</code> or <code>/config realbook off</code>", force=True)

    elif key == "paperdailymax":
        try:
            v = float(val)
            if not (1.00 <= v <= 1000000.00):
                send("⚠️ Must be 1.00-1000000.00.", force=True); return
            old = getattr(config, "PAPER_MAX_DAILY", 1000000.00); config.PAPER_MAX_DAILY = v
            saved = persist_config("PAPER_MAX_DAILY", v)
            send(
                f"✅ Paper daily max: ${old:.2f} → <b>${v:.2f}</b>\n"
                f"Set this to match your live daily max (e.g. same as "
                f"<code>/config dailymax</code>) for a true apples-to-apples "
                f"trade-frequency comparison between paper and live."
                f"{'  (saved to disk)' if saved else '  ⚠️ runtime only — will revert on restart'}",
                force=True
            )
        except ValueError:
            send("⚠️ Usage: <code>/config paperdailymax 15</code>", force=True)

    elif key == "keeplooking":
        if val in ("on", "true", "1"):
            config.KEEP_LOOKING_ENABLED = True
            send("✅ Keep-looking: <b>ON</b>", force=True)
        elif val in ("off", "false", "0"):
            config.KEEP_LOOKING_ENABLED = False
            send("✅ Keep-looking: <b>OFF</b> — bot enters at first valid price, as before", force=True)
        else:
            send("⚠️ Usage: <code>/config keeplooking on</code> or <code>off</code>", force=True)

    elif key == "kltrigger":
        send(
            "ℹ️ <code>/config kltrigger</code> was removed — keep-looking now "
            "engages automatically whenever a signal's price exceeds "
            "<code>maxentry</code>, so there's no separate trigger to set. "
            "Use <code>/config maxentry &lt;price&gt;</code> instead — it now "
            "controls both.",
            force=True
        )

    elif key == "klretrace":
        try:
            v = float(val)
            if not (5 <= v <= 90):
                send("⚠️ Must be 5-90 (percent).", force=True); return
            old = getattr(config, "KEEP_LOOKING_MAX_RETRACE", 0.40) * 100
            config.KEEP_LOOKING_MAX_RETRACE = v / 100.0
            send(f"✅ Keep-looking max retrace: {old:.0f}% → <b>{v:.0f}%</b>", force=True)
        except ValueError:
            send("⚠️ Usage: <code>/config klretrace 40</code>", force=True)

    elif key == "kltimeout":
        try:
            v = int(val)
            if not (5 <= v <= 280):
                send("⚠️ Must be 5-280 seconds.", force=True); return
            old = getattr(config, "KEEP_LOOKING_TIMEOUT_SEC", 45)
            config.KEEP_LOOKING_TIMEOUT_SEC = v
            send(f"✅ Keep-looking timeout: {old}s → <b>{v}s</b>", force=True)
        except ValueError:
            send("⚠️ Usage: <code>/config kltimeout 45</code>", force=True)

    else:
        send(f"⚠️ Unknown key: {key}\nSee /config", force=True)

def handle_diagnostic(arg):
    """Run diagnostic.py in a background thread and send detailed results."""
    is_full = arg.strip().lower() == "full"
    mode_text = "5-minute full" if is_full else "2-minute quick"
    send(f"🔬 <b>Diagnostic Started</b>\nRunning {mode_text} market check...\nI'll ping you when results are ready.", force=True)

    def run_diag():
        cmd = [sys.executable, "diagnostic.py"]
        if not is_full:
            cmd.append("--quick")
        bot_dir = os.path.dirname(os.path.abspath(__file__)) or "."
        try:
            result = subprocess.run(cmd, cwd=bot_dir, timeout=360, capture_output=True, text=True)
            if result.returncode != 0:
                import html
                err = html.escape((result.stderr or result.stdout or "no output")[:500])
                send(f"⚠️ Diagnostic exited with code {result.returncode}:\n<code>{err}</code>", force=True)
                return
        except Exception as e:
            import html
            send(f"⚠️ Diagnostic failed to run: {html.escape(str(e))}", force=True)
            return

        diag_path = os.path.join(bot_dir, "diagnostic.json")
        try:
            with open(diag_path, "r") as f:
                d = json.load(f)
        except Exception as e:
            send(f"⚠️ Could not read diagnostic.json: {e}", force=True)
            return

        # Guard against showing stale cached results from a previous run
        try:
            file_age = time.time() - os.path.getmtime(diag_path)
            if file_age > 900:  # older than 15 minutes
                send(
                    f"⚠️ <b>Diagnostic data is stale</b> ({int(file_age // 60)}m old).\n"
                    f"The diagnostic subprocess likely failed. Check VPS logs.",
                    force=True
                )
                return
        except Exception:
            pass

        # Overall
        o   = d.get("overall", {})
        adj = o.get("adjusted_pct", 0)
        if adj >= 60:   verdict = "🟢 <b>GO</b>"
        elif adj >= 40: verdict = "🟡 <b>CAUTION</b>"
        else:           verdict = "🔴 <b>NO-GO</b>"

        msg = (
            f"🔬 <b>Diagnostic Result</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"Status: {verdict}\n"
            f"Score: {o.get('score', 0)}/{o.get('max_possible', 0)} ({adj:.0f}%)\n"
        )

        # Time assessment
        t = d.get("time", {})
        if t:
            hour   = t.get("hour", "?")
            day    = t.get("day", "?")
            qual   = t.get("quality", "?")
            msg += (
                f"\n🕐 <b>Time Assessment</b>\n"
                f"Hour: {hour}:00 ({day}) — {qual}\n"
            )

        # RPC Health
        rpc = d.get("rpc", {})
        if rpc:
            reads = rpc.get("reads", "?")
            score = rpc.get("score", "?")
            msg += (
                f"\n🏓 <b>RPC Health</b>\n"
                f"Reads: {reads} (Score: {score}/5)\n"
            )

        # Per-symbol breakdown
        symbols = d.get("symbols", {})
        if symbols:
            msg += f"\n📊 <b>Symbol Breakdown</b>\n"
            for sym, sd in symbols.items():
                pct     = sd.get("pct", 0)
                verdict_sym = sd.get("verdict", "?")

                lag_d   = sd.get("lag", {})
                med_lag = lag_d.get("med_lag", 0)
                avg_gap = lag_d.get("avg_gap", 0)

                vol_d   = sd.get("vol", {})
                avg_mv  = vol_d.get("avg_move", 0)
                trend   = "Trending" if vol_d.get("is_trending") else "Toxic"

                liq_d   = sd.get("liq", {})
                liq     = liq_d.get("liquidity", 0)
                spread  = liq_d.get("spread", 0)

                msg += (
                    f"<b>{sym.upper()}</b>: {pct:.0f}% — {verdict_sym}\n"
                    f"  ├ Lag: {med_lag:.0f}s | Gap: {avg_gap:.3f}\n"
                    f"  ├ Vol: {avg_mv:.4f}% ({trend})\n"
                    f"  └ Liq: ${liq:,.0f} | Sprd: {spread:.1f}%\n"
                )

        send(msg, force=True)

    Thread(target=run_diag, daemon=True).start()

def handle_cap(arg):
    parts = arg.strip().lower().split()
    if not parts:
        cap_s = str(config.MAX_OPEN_POSITIONS) if config.MAX_OPEN_POSITIONS < 999 else "OFF"
        sym_s = str(config.MAX_PER_SYMBOL)     if config.MAX_PER_SYMBOL     < 999 else "OFF"
        kelly_max_s = f"${config.KELLY_MAX_STAKE:.0f}/${config.KELLY_MAX_STAKE*2:.0f}/${config.KELLY_MAX_STAKE*4:.0f}" \
                      if config.KELLY_MAX_STAKE < 9999 else "OFF"
        kelly_pct_s = f"{config.KELLY_MAX_BANKROLL_PCT*100:.0f}%" \
                      if config.KELLY_MAX_BANKROLL_PCT < 0.999 else "OFF"
        exp_s = f"{config.MAX_CONCURRENT_EXPOSURE*100:.0f}%" \
                if config.MAX_CONCURRENT_EXPOSURE < 1.0 else "OFF"
        send(
            f"🔒 <b>Position & Stake Caps</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Max positions: <b>{cap_s}</b>\n"
            f"Max per symbol: <b>{sym_s}</b>\n"
            f"Kelly stake max (k25/k50/k100): <b>{kelly_max_s}</b>\n"
            f"Kelly bankroll %: <b>{kelly_pct_s}</b>\n"
            f"Concurrent exposure cap: <b>{exp_s}</b>\n\n"
            f"<b>Position caps:</b>\n"
            f"<code>/cap off</code> — disable all\n"
            f"<code>/cap on</code> — restore (3/1)\n"
            f"<code>/cap 5</code> — set max\n"
            f"<code>/cap sym 2</code> — per symbol\n"
            f"<code>/cap sym off</code> — no symbol limit\n\n"
            f"<b>Kelly stake caps:</b>\n"
            f"<code>/cap kelly off</code> — no stake ceiling\n"
            f"<code>/cap kelly on</code> — restore $50/k25 ceiling\n"
            f"<code>/cap kelly 100</code> — set k25 cap to $100\n"
            f"<code>/cap kellypct off</code> — no bankroll % limit\n"
            f"<code>/cap kellypct 50</code> — 50% bankroll max\n\n"
            f"<b>Concurrent exposure:</b>\n"
            f"<code>/cap exposure off</code> — disable\n"
            f"<code>/cap exposure 20</code> — 20% of bankroll max concurrent",
            force=True
        )
        return
    if parts[0] == "off":
        config.MAX_OPEN_POSITIONS = 9999
        config.MAX_PER_SYMBOL     = 9999
        send("🔓 <b>Position caps DISABLED</b>", force=True)
    elif parts[0] == "on":
        config.MAX_OPEN_POSITIONS = 3
        config.MAX_PER_SYMBOL     = 1
        send("🔒 <b>Caps ENABLED</b> (3 / 1)", force=True)
    elif parts[0] == "sym" and len(parts) > 1:
        if parts[1] == "off":
            config.MAX_PER_SYMBOL = 9999
            send("🔓 Per-symbol cap DISABLED", force=True)
        else:
            try:
                v = int(parts[1])
                if 1 <= v <= 20:
                    config.MAX_PER_SYMBOL = v
                    send(f"✅ Max per symbol: <b>{v}</b>", force=True)
                else:
                    send("⚠️ 1-20 only", force=True)
            except ValueError:
                send("⚠️ Usage: <code>/cap sym 2</code>", force=True)
    elif parts[0] == "kelly" and len(parts) > 1:
        if parts[1] == "off":
            config.KELLY_MAX_STAKE = 9999
            send(
                "🔓 <b>Kelly stake cap DISABLED</b>\n"
                "Stakes can now scale freely with bankroll & Kelly fraction.\n"
                "⚠️ Kelly100 with no cap is very aggressive — monitor closely.",
                force=True
            )
        elif parts[1] == "on":
            config.KELLY_MAX_STAKE = 50.00
            send(
                "🔒 <b>Kelly stake cap ENABLED</b>\n"
                "k25 → $50 max, k50 → $100 max, k100 → $200 max",
                force=True
            )
        else:
            try:
                v = float(parts[1])
                if 1 <= v <= 10000:
                    config.KELLY_MAX_STAKE = v
                    send(
                        f"✅ Kelly k25 cap set to <b>${v:.0f}</b>\n"
                        f"k50 → ${v*2:.0f}, k100 → ${v*4:.0f}",
                        force=True
                    )
                else:
                    send("⚠️ Must be $1-$10000", force=True)
            except ValueError:
                send("⚠️ Usage: <code>/cap kelly 100</code> | <code>/cap kelly off</code>", force=True)
    elif parts[0] == "kellypct" and len(parts) > 1:
        if parts[1] == "off":
            config.KELLY_MAX_BANKROLL_PCT = 1.00
            send("🔓 <b>Kelly bankroll % cap DISABLED</b>", force=True)
        else:
            try:
                v = float(parts[1])
                if 1 <= v <= 100:
                    config.KELLY_MAX_BANKROLL_PCT = v / 100.0
                    send(f"✅ Kelly bankroll cap: <b>{v:.0f}%</b>", force=True)
                else:
                    send("⚠️ 1-100 only", force=True)
            except ValueError:
                send("⚠️ Usage: <code>/cap kellypct 50</code>", force=True)
    elif parts[0] == "exposure" and len(parts) > 1:
        if parts[1] == "off":
            config.MAX_CONCURRENT_EXPOSURE = 1.0
            send("🔓 <b>Concurrent exposure cap DISABLED</b>", force=True)
        else:
            try:
                v = float(parts[1])
                if 5 <= v <= 100:
                    config.MAX_CONCURRENT_EXPOSURE = v / 100.0
                    send(f"✅ Concurrent exposure cap: <b>{v:.0f}%</b> of bankroll", force=True)
                else:
                    send("⚠️ 5-100 only", force=True)
            except ValueError:
                send("⚠️ Usage: <code>/cap exposure 20</code>", force=True)
    else:
        try:
            v = int(parts[0])
            if 1 <= v <= 100:
                config.MAX_OPEN_POSITIONS = v
                send(f"✅ Max positions: <b>{v}</b>", force=True)
            else:
                send("⚠️ 1-100 only", force=True)
        except ValueError:
            send("⚠️ See /cap for options.", force=True)

# =======================================================
# CARD COMMAND — generates PnL card image
# =======================================================
def handle_card_command():
    """Show card menu with period + customisation buttons."""
    has_bg   = any(os.path.exists(f"{BOT_DIR}/card_bg.{e}") for e in ["jpg", "jpeg", "png"])
    has_font = os.path.exists(f"{BOT_DIR}/card_font.ttf")

    send(
        f"🎨 <b>PnL Card</b>\n"
        f"Background: {'custom' if has_bg else 'default'}  ·  "
        f"Font: {'custom' if has_font else 'default'}\n\n"
        f"Choose a period or customise:",
        buttons=[
            [("📊 All Time", "card:all"),
             ("📅 Daily",    "card:daily"),
             ("📆 Weekly",   "card:weekly")],
            [("🖼 Set Background", "card:setbg"),
             ("🔤 Set Font",       "card:setfont")],
        ],
        force=True
    )

def handle_card_callback(action, mid=None):
    """Handle button taps from /card menu."""
    global _upload_state
    _delete_msg(mid)

    if action in ("all", "daily", "weekly"):
        send("🎨 Generating card...", silent=True, force=True)
        _do_card_send(action)

    elif action == "setbg":
        _upload_state = "bg"
        send(
            "🖼 <b>Custom Background</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Send any <b>photo</b> to this chat.\n"
            "It will fill your card behind the stats.\n\n"
            "Type <code>cancel</code> to abort.",
            force=True
        )

    elif action == "setfont":
        _upload_state = "font"
        send(
            "🔤 <b>Custom Font</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Send a <b>.ttf</b> font file to this chat.\n\n"
            "Free fonts: fonts.google.com\n"
            "Download family → extract .ttf → send it here.\n\n"
            "Type <code>cancel</code> to abort.",
            force=True
        )

def handle_cardbg(arg):
    """/cardbg reset — clear custom background."""
    if arg.strip().lower() == "reset":
        removed = False
        for ext in ["jpg", "jpeg", "png"]:
            fp = f"{BOT_DIR}/card_bg.{ext}"
            if os.path.exists(fp):
                os.remove(fp)
                removed = True
        if removed:
            send("✅ Background reset to default.", force=True)
        else:
            send("ℹ️ Already using default background.", force=True)
    else:
        has_bg = any(os.path.exists(f"{BOT_DIR}/card_bg.{e}") for e in ["jpg", "jpeg", "png"])
        if has_bg:
            send(
                "🖼 <b>Custom background active</b>\n"
                "Send a new photo to replace it.\n"
                "<code>/cardbg reset</code> — restore default",
                force=True
            )
        else:
            send(
                "🖼 <b>Card Background</b>\n"
                "Currently default style.\n\n"
                "To set custom: send any photo, OR use <code>/card</code> → Set Background.",
                force=True
            )

def handle_font_reset():
    fp = f"{BOT_DIR}/card_font.ttf"
    if os.path.exists(fp):
        os.remove(fp)
        send("✅ Font reset to default.", force=True)
    else:
        send("ℹ️ Already using default font.", force=True)

def _do_card_send(period="all"):
    """Generate and send the card. Run in background thread."""
    try:
        import importlib
        if "card_generator" in sys.modules:
            del sys.modules["card_generator"]
        sys.path.insert(0, BOT_DIR)
        cg = importlib.import_module("card_generator")

        if _executor_ref is not None:
            stats = cg.build_stats_for_card(_executor_ref, config, period)
        else:
            stats = None

        if not stats:
            send("⚠️ No trade data yet for this period.", force=True)
            return

        font_path = f"{BOT_DIR}/card_font.ttf"
        if not os.path.exists(font_path):
            font_path = None

        jpg = cg.generate_card(
            stats,
            period=period,
            logo_path=f"{BOT_DIR}/cipher_logo.jpeg",
            font_path=font_path,
        )

        if not _send_photo(jpg):
            send(f"⚠️ Failed to send card photo.", force=True)
        else:
            log.info(f"Card sent ({len(jpg):,} bytes period={period})")

    except ModuleNotFoundError:
        send("⚠️ card_generator.py not found in bot directory.", force=True)
    except Exception as e:
        import traceback
        log.error(f"Card error: {traceback.format_exc()}")
        send(f"⚠️ Card error: <code>{str(e)[:200]}</code>", force=True)

# =======================================================
# UPLOAD HANDLERS
# =======================================================
def handle_photo_upload(message):
    """Save uploaded photo as card background."""
    global _upload_state
    if _upload_state != "bg":
        # Photo received but not in upload mode — ignore silently
        return

    try:
        photos = message.get("photo", [])
        if not photos:
            return
        file_id = photos[-1]["file_id"]

        resp = requests.get(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10
        )
        if resp.status_code != 200:
            send("⚠️ Could not retrieve photo.", force=True); return

        file_path = resp.json().get("result", {}).get("file_path", "")
        if not file_path:
            send("⚠️ Telegram returned empty file path.", force=True); return

        dl = requests.get(
            f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{file_path}",
            timeout=20
        )
        if dl.status_code != 200:
            send("⚠️ Could not download photo.", force=True); return

        # Remove any previous backgrounds
        for ext in ["jpg", "jpeg", "png"]:
            fp = f"{BOT_DIR}/card_bg.{ext}"
            if os.path.exists(fp):
                os.remove(fp)

        # Ensure save directory exists
        save_dir = os.path.dirname(os.path.abspath(__file__))
        save_path = os.path.join(save_dir, "card_bg.jpg")
        with open(save_path, "wb") as f:
            f.write(dl.content)

        _upload_state = None
        send(
            "✅ <b>Background saved!</b>\n"
            "Use /card and tap a period to see it.\n"
            "<code>/cardbg reset</code> — restore default",
            force=True
        )
        log.info(f"Card background updated ({len(dl.content):,} bytes)")

    except Exception as e:
        send(f"⚠️ Photo upload failed: {e}", force=True)
        log.error(f"Photo upload error: {e}")

def handle_document_upload(message):
    """Save uploaded .ttf as card font."""
    global _upload_state
    doc   = message.get("document", {})
    fname = doc.get("file_name", "")

    if not fname.lower().endswith(".ttf"):
        if _upload_state == "font":
            send(f"⚠️ Please send a <b>.ttf</b> file.\n({fname} is not a TTF font)", force=True)
        return

    if _upload_state != "font":
        # Got a TTF but we're not waiting for one — ignore
        return

    try:
        file_id = doc.get("file_id", "")
        resp = requests.get(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10
        )
        if resp.status_code != 200:
            send("⚠️ Could not retrieve file.", force=True); return

        file_path = resp.json().get("result", {}).get("file_path", "")
        dl = requests.get(
            f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{file_path}",
            timeout=20
        )
        if dl.status_code != 200:
            send("⚠️ Could not download font.", force=True); return

        # Validate it's a real TTF
        try:
            from PIL import ImageFont as _IF
            _IF.truetype(io.BytesIO(dl.content), 20)
        except Exception:
            send("⚠️ Invalid TTF file. Please send a valid font.", force=True); return

        save_dir  = os.path.dirname(os.path.abspath(__file__))
        save_path = os.path.join(save_dir, "card_font.ttf")
        with open(save_path, "wb") as f:
            f.write(dl.content)

        _upload_state = None
        send(
            f"✅ <b>Font saved!</b>\n<code>{fname}</code>\n"
            f"Use /card to see it in action.",
            force=True
        )
        log.info(f"Custom font saved: {fname} ({len(dl.content):,} bytes)")

    except Exception as e:
        send(f"⚠️ Font upload failed: {e}", force=True)
        log.error(f"Font upload error: {e}")

# =======================================================
# STATUS / HELP / HEARTBEAT
# =======================================================
def handle_status():
    up = int(time.time() - _bot_start_time)
    h, m = up // 3600, (up % 3600) // 60
    s = get_performance_stats()
    bal, is_real = get_display_balance()
    pnl     = s["total_pnl"] if s else 0.0
    wr      = s["win_rate"]  if s else 0.0
    tot     = s["total"]     if s else 0
    bal_label = "Live bal" if config.MODE in ("live", "both") else "Paper bal"
    bal_flag  = "" if is_real else " ⚠️ (real fetch failed, showing paper)"
    sizing = (
        f"Flat ${config.FLAT_STAKE:.2f}" if config.SIZING_MODE == "flat"
        else f"Kelly {int(config.KELLY_FRACTION*100)}%"
    )
    title = "⚡ <b>Standalone Listener</b>" if is_standalone else "⚡ <b>Main Bot Status</b>"
    send(
        f"{title}\nTrading: {'🟢 ACTIVE' if trading_active else '🔴 PAUSED'}\n"
        f"Mode: {config.MODE} | {sizing}\n"
        f"Open: {count_open_positions()} | Resolved: {tot}\n"
        f"{bal_label}: ${bal:.2f}{bal_flag}\n"
        f"P&L: ${pnl:+.2f} | WR: {wr:.1f}%\n"
        f"Uptime: {h}h {m}m",
        force=True
    )

def handle_help():
    send(
        "🤖 <b>Bot Commands</b>\n━━━━━━━━━━━━━━━━━━\n"
        "/status — Quick overview\n"
        "/performance — Full report\n"
        "/funnel — Signal info\n"
        "/positions — Open positions\n"
        "/balance — Balances\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/start — Enable trading\n"
        "/stop — Pause trading\n"
        "/mode &lt;paper|live|both&gt;\n"
        "/config — View/change settings\n"
        "/cap — Position caps\n"
        "/diagnostic — Run market check\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/deposit — Show address\n"
        "/withdraw &lt;addr&gt; &lt;amt&gt;\n"
        "/withdraw CONFIRM\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/card — PnL card 🎨\n"
        "/cardbg reset — Default background\n"
        "/font reset — Default font\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/restart CONFIRM 🔄\n"
        "/emergency CONFIRM 🚨\n"
        "/help — This message",
        force=True
    )

def send_heartbeat():
    global _last_heartbeat
    now = time.time()
    if now - _last_heartbeat < _heartbeat_interval:
        return
    _last_heartbeat = now
    up = int(now - _bot_start_time)
    h, m = up // 3600, (up % 3600) // 60
    s = get_performance_stats()
    bal, is_real = get_display_balance()
    bal_label = "Live" if config.MODE in ("live", "both") else "Bal"
    bal_flag  = " ⚠️" if not is_real else ""
    if s:
        msg = (
            f"💓 <b>Heartbeat</b> — {datetime.now().strftime('%H:%M')}\n"
            f"Uptime: {h}h {m}m\n"
            f"Trades: {s['total']} | WR: {s['win_rate']:.0f}%\n"
            f"P&L: ${s['total_pnl']:+.2f} | {bal_label}: ${bal:.2f}{bal_flag}\n"
            f"Open: {s['open']} | Mode: {config.MODE}"
        )
    else:
        msg = (
            f"💓 <b>Heartbeat</b> — {datetime.now().strftime('%H:%M')}\n"
            f"Uptime: {h}h {m}m\nNo trades yet | Mode: {config.MODE}\n"
            f"Trading: {'ACTIVE' if trading_active else 'PAUSED'}"
        )
    send(msg, silent=True, force=True)

# =======================================================
# POLLING — handles messages, photos, documents, callbacks
# =======================================================
def _process_update(update):
    """Process a single update from Telegram. Runs in main polling thread."""
    global _upload_state

    # ── BUTTON CALLBACK ──
    if "callback_query" in update:
        cq      = update["callback_query"]
        cq_id   = cq.get("id", "")
        cq_data = cq.get("data", "")
        cq_mid  = cq.get("message", {}).get("message_id")
        cq_chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))

        # Acknowledge immediately to remove the loading spinner
        _answer_cb(cq_id)

        if cq_chat != config.TELEGRAM_CHAT_ID:
            return

        # Card buttons — process in dedicated thread to never block polling
        if cq_data.startswith("card:"):
            action = cq_data.split(":", 1)[1] if ":" in cq_data else "all"
            Thread(target=handle_card_callback, args=(action, cq_mid), daemon=True).start()
        return

    # ── MESSAGE ──
    message = update.get("message", {})
    if not message:
        return

    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id != config.TELEGRAM_CHAT_ID:
        log.warning(f"Unauthorized chat_id: {chat_id}")
        return

    # Photo upload — saves as card background if in upload state
    if message.get("photo"):
        Thread(target=handle_photo_upload, args=(message,), daemon=True).start()
        return

    # Document upload — saves as font if in upload state
    if message.get("document"):
        Thread(target=handle_document_upload, args=(message,), daemon=True).start()
        return

    # Text message
    text = message.get("text", "").strip()
    if not text:
        return

    # 'cancel' during upload state
    if text.lower() == "cancel" and _upload_state is not None:
        _upload_state = None
        send("❌ Upload cancelled.", force=True)
        return

    parts = text.split(maxsplit=1)
    cmd   = parts[0].lower() if parts else ""
    if "@" in cmd:                    # strip /cmd@BotName suffix (groups / some clients)
        cmd = cmd.split("@")[0]
    arg   = parts[1] if len(parts) > 1 else ""

    # All commands run in their own thread so polling never blocks
    handlers = {
        "/start"      : lambda: handle_start_trading(),
        "/stop"       : lambda: handle_stop_trading(),
        "/status"     : handle_status,
        "/help"       : handle_help,
        "/funnel"     : handle_signal_info,
        "/signals"    : handle_signal_info,
        "/performance": handle_performance,
        "/stats"      : handle_performance,
        "/positions"  : handle_positions,
        "/balance"    : handle_balance,
        "/deposit"    : lambda: handle_deposit(arg),
        "/withdraw"   : lambda: handle_withdraw(arg),
        "/emergency"  : lambda: handle_emergency(arg),
        "/restart"    : lambda: handle_restart(arg),
        "/mode"       : lambda: handle_mode(arg),
        "/config"     : lambda: handle_config(arg),
        "/diagnostic" : lambda: handle_diagnostic(arg),
        "/diag"       : lambda: handle_diagnostic(arg),
        "/cap"        : lambda: handle_cap(arg),
        "/card"       : handle_card_command,
        "/cardbg"     : lambda: handle_cardbg(arg),
        "/font"       : lambda: (handle_font_reset() if arg.strip().lower() == "reset" else send(
            "🔤 Send a .ttf file to this chat to set a custom font.\n"
            "Or use /card → Set Font.\n<code>/font reset</code> — restore default",
            force=True
        )),
    }

    handler = handlers.get(cmd)
    if handler:
        Thread(target=handler, daemon=True).start()

def poll_commands():
    """Long-poll Telegram for updates. Runs in dedicated thread."""
    global polling_active
    if not config.TELEGRAM_ENABLED:
        return
    polling_active = True
    last_update_id = 0

    # Flush stale messages so old commands don't re-fire
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": -1, "timeout": 0}, timeout=5
        )
        data = resp.json()
        if data.get("ok") and data.get("result"):
            last_update_id = data["result"][-1]["update_id"]
            log.info(f"Flushed stale Telegram messages up to {last_update_id}")
    except Exception:
        pass

    log.info("Telegram polling started (long-poll, threaded)")
    while polling_active:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates",
                params={
                    "offset"         : last_update_id + 1,
                    "timeout"        : 30,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=35
            )
            if resp.status_code != 200:
                time.sleep(5); continue
            data = resp.json()
            if not data.get("ok"):
                time.sleep(5); continue

            for update in data.get("result", []):
                last_update_id = update["update_id"]
                try:
                    _process_update(update)
                except Exception as e:
                    log.error(f"Error processing update: {e}")

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log.debug(f"Polling error: {e}")
            time.sleep(10)

# =======================================================
# STARTUP
# =======================================================
_poll_lock_socket = None

def start_polling():
    global _poll_lock_socket
    if not config.TELEGRAM_ENABLED:
        log.info("Telegram disabled (no token/chat_id)")
        return

    # Prevent multiple instances clashing
    try:
        _poll_lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _poll_lock_socket.bind(("127.0.0.1", 52719))
    except socket.error:
        log.error("Duplicate poller detected — exiting to prevent conflicts.")
        os._exit(1)

    try:
        import order_executor
        set_executor_ref(order_executor)
    except Exception as e:
        log.warning(f"Could not set executor ref: {e}")

    Thread(target=poll_commands, daemon=True).start()

def stop_polling():
    global polling_active
    polling_active = False

# =======================================================
# ALERT FUNCTIONS — called from order_executor
# =======================================================
def trade_entered(trade_id, symbol, interval, direction, entry_side, fill_price, stake, mode):
    if not config.NOTIFY_ON_ENTRY:
        return
    e = "📄" if mode == "paper" else "💰"
    send(
        f"{e} <b>Trade Entered</b>\nID: {trade_id}\n"
        f"{symbol.upper()} {interval} — {direction}\n"
        f"Side: {entry_side} @ {fill_price:.3f}\nStake: ${stake:.2f}",
        silent=True
    )

def trade_resolved(trade_id, symbol, interval, direction, result, pnl, balance, mode):
    if not config.NOTIFY_ON_RESOLVE:
        return
    e  = "✅" if result == "WIN" else ("🚪" if result == "EARLY_EXIT" else "❌")
    me = "📄" if mode == "paper" else "💰"
    bs = f"\nBalance: ${balance:.2f}" if balance else ""
    send(
        f"{e} {me} <b>{result}</b> — {trade_id}\n"
        f"{symbol.upper()} {interval} {direction}\nP&L: ${pnl:+.2f}{bs}"
    )

def early_exit(trade_id, symbol, interval, fill_price, exit_price, pnl):
    send(
        f"🚪 <b>Early Exit</b> — {trade_id}\n"
        f"{symbol.upper()} {interval}\n{fill_price:.3f} → {exit_price:.3f}\nP&L: ${pnl:+.2f}"
    )

def bot_error(msg):
    send(f"⚠️ <b>Error</b>\n{msg}", force=True)

def bot_started():
    global _bot_start_time, _last_heartbeat
    _bot_start_time = time.time()
    _last_heartbeat = time.time()
    send("🟢 <b>Bot started</b>", force=True)

def bot_stopped():
    send("🔴 <b>Bot stopped</b>", force=True)

# =======================================================
# STANDALONE
# =======================================================
if __name__ == "__main__":
    is_standalone = True
    log.info("Starting Telegram alerts in standalone mode...")
    start_polling()
    while True:
        time.sleep(1)
