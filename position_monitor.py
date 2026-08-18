# position_monitor.py
# Monitors open positions for early exit when price collapses.
# DISABLED by default — your data showed it destroys win rate at small stakes.

import time
from threading import Lock
from py_clob_client_v2.client import ClobClient

import config
from logger import log

# =======================================================
# CONFIG
# =======================================================
EXIT_THRESHOLD   = 0.95   # sell when price drops to this fraction of fill
CHECK_INTERVAL   = 5
MIN_MONITOR_TIME = 30     # don't monitor if market ends in less than this
ENABLED          = False  # flip to True only after testing at higher stakes

# =======================================================
# STATE
# =======================================================
clob_client  = ClobClient(host=config.CLOB_HOST, chain_id=config.CHAIN_ID)
last_check   = 0
exit_log     = []
monitor_lock = Lock()

# =======================================================
# PRICE CHECK
# =======================================================
def get_current_price(token_id):
    if not token_id:
        return None
    try:
        result = clob_client.get_midpoint(token_id)
        if result and "mid" in result:
            return float(result["mid"])
        return None
    except Exception:
        return None

def should_exit(trade, current_price):
    fill_price = trade.get("fill_price", 0)
    if not fill_price or fill_price <= 0:
        return False, "", current_price
    if current_price is None:
        return False, "price unavailable", current_price

    drop_pct   = (fill_price - current_price) / fill_price
    exit_price = fill_price * EXIT_THRESHOLD

    if current_price <= exit_price:
        reason = (
            f"price {current_price:.3f} <= exit threshold {exit_price:.3f} "
            f"({drop_pct*100:.0f}% drop from {fill_price:.3f})"
        )
        return True, reason, current_price

    return False, "", current_price

# =======================================================
# MAIN CHECK
# =======================================================
def check_positions(open_positions, order_executor):
    global last_check

    if not ENABLED:
        return

    now = int(time.time())
    if now - last_check < CHECK_INTERVAL:
        return
    last_check = now

    if not open_positions:
        return

    trades_to_exit = []

    for trade in list(open_positions):
        end_ts = order_executor.get_end_timestamp(trade.get("end_date", ""))
        if end_ts == 0:
            continue
        time_left = end_ts - now
        if time_left < MIN_MONITOR_TIME:
            continue

        if trade.get("resolve_attempts", 0) > 0:
            continue

        token_id = trade.get("token_id", "")
        if not token_id:
            continue

        current_price = get_current_price(token_id)
        if current_price is None:
            continue

        exit_needed, reason, price = should_exit(trade, current_price)
        if exit_needed:
            trades_to_exit.append((trade, reason, price, time_left))

    for trade, reason, current_price, time_left in trades_to_exit:
        execute_early_exit(trade, reason, current_price, time_left, open_positions, order_executor)

# =======================================================
# EXECUTE EARLY EXIT
# =======================================================
def execute_early_exit(trade, reason, current_price, time_left, open_positions, order_executor):
    import alerts

    trade_id   = trade["trade_id"]
    mode       = trade.get("mode", "paper")
    fill_price = trade["fill_price"]
    stake      = trade["stake"]
    shares     = trade["shares"]

    recovery   = shares * current_price
    pnl        = recovery - stake

    if mode == "paper":
        with monitor_lock:
            trade["result"]         = "EARLY_EXIT"
            trade["payout"]         = round(recovery, 4)
            trade["pnl"]            = round(pnl, 4)
            trade["chainlink_exit"] = ""

            order_executor.paper_balance += recovery
            trade["balance_after"] = round(order_executor.paper_balance, 4)

            order_executor.closed_trades.append(trade)
            if trade in open_positions:
                open_positions.remove(trade)

            order_executor.log_trade(trade)

            exit_log.append({
                "trade_id"     : trade_id,
                "current_price": current_price,
                "fill_price"   : fill_price,
                "recovery"     : recovery,
                "pnl"          : pnl,
                "time_left"    : time_left,
            })

        log.info(
            f"EARLY EXIT {trade_id}: {trade['symbol'].upper()} {trade['interval']} "
            f"{trade['direction']} fill={fill_price:.3f} exit={current_price:.3f} "
            f"recovered=${recovery:.2f} pnl=${pnl:+.2f} | {reason}"
        )

        alerts.early_exit(trade_id, trade["symbol"], trade["interval"],
                          fill_price, current_price, pnl)

    elif mode == "live":
        log.warning(
            f"LIVE EARLY EXIT SIGNAL {trade_id} — sell not yet implemented, holding"
        )

# =======================================================
# STATS
# =======================================================
def print_exit_stats():
    if not exit_log:
        return

    total_exits  = len(exit_log)
    total_saved  = sum(e["recovery"] for e in exit_log)
    total_pnl    = sum(e["pnl"] for e in exit_log)
    avg_recovery = total_saved / total_exits

    msg = [
        "\n   🚪 EXIT MONITOR STATS",
        f"   Early exits taken: {total_exits}",
        f"   Total recovered:   ${total_saved:.2f}",
        f"   Avg recovered:     ${avg_recovery:.2f} per exit",
        f"   Total P&L (exits): ${total_pnl:+.2f}",
        "",
    ]
    log.info("\n".join(msg))
