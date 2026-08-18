# order_executor.py
# Paper + live executor with Kelly sizing, Chainlink self-resolution,
# and CSV persistence. Cleaned up from previous version.

import time
import csv
import os
from datetime import datetime
from threading import Lock
from web3 import Web3

import config
import alerts
from logger import log

import requests

api_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
api_session.mount('http://', adapter)
api_session.mount('https://', adapter)

w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={'timeout': 2}, session=api_session))

# =======================================================
# WALLET — deposit address + USDC withdrawal
# =======================================================

# USDC contract on Polygon (USDC.e bridged — the one Polymarket uses)
USDC_ADDRESS  = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_ABI      = [
    {
        "name": "transfer",
        "type": "function",
        "inputs": [
            {"name": "_to",    "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable"
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "_owner", "type": "address"}],
        "outputs": [{"name": "balance", "type": "uint256"}],
        "stateMutability": "view"
    },
    {
        "name": "decimals",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view"
    }
]

_usdc_contract = None

def _get_usdc_contract():
    global _usdc_contract
    if _usdc_contract is None:
        _usdc_contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=USDC_ABI
        )
    return _usdc_contract

def get_wallet_address():
    """Return proxy address if set, otherwise derive from private key."""
    if config.WALLET_ADDRESS:
        try:
            return Web3.to_checksum_address(config.WALLET_ADDRESS)
        except Exception:
            pass
    if not config.PRIVATE_KEY:
        return None
    try:
        account = w3.eth.account.from_key(config.PRIVATE_KEY)
        return account.address
    except Exception as e:
        log.error(f"Wallet address derivation failed: {e}")
        return None

def get_usdc_balance(address=None):
    """Read USDC balance from Polymarket CLOB API (authoritative source for trading funds)."""
    try:
        from py_clob_client_v2.client import ClobClient as _C
        from py_clob_client_v2.clob_types import ApiCreds as _A
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        
        funder_addr = None
        if getattr(config, 'PROXY_ADDRESS', None):
            try:
                funder_addr = Web3.to_checksum_address(config.PROXY_ADDRESS)
            except Exception:
                funder_addr = config.PROXY_ADDRESS
                
        _client = _C(
            host=config.CLOB_HOST,
            chain_id=config.CHAIN_ID,
            key=config.PRIVATE_KEY,
            creds=_A(
                api_key=config.API_KEY,
                api_secret=config.API_SECRET,
                api_passphrase=config.API_PASSPHRASE,
            ),
            signature_type=2,
            funder=funder_addr,
        )
        result = _client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        raw = float(result.get("balance", 0))
        return raw / 1e6
    except Exception as e:
        log.error(f"Balance fetch failed: {e}")
        return None


def get_metamask_balance():
    """Deposit command reads raw MetaMask USDC balance."""
    try:
        addr = get_wallet_address()
        if not addr:
            return None
        usdc = _get_usdc_contract()
        raw  = usdc.functions.balanceOf(
            Web3.to_checksum_address(addr)
        ).call()
        dec  = usdc.functions.decimals().call()
        return raw / (10 ** dec)
    except Exception as e:
        log.error(f"MetaMask balance fetch failed: {e}")
        return None

def get_pol_balance(address=None):
    """Get POL balance — reads from MetaMask address, not proxy."""
    try:
        # Prefer the explicitly configured WALLET_ADDRESS from .env.
        # Deriving from the private key can read a different account if the key
        # belongs to a Polymarket embedded wallet rather than the MetaMask address.
        if config.WALLET_ADDRESS:
            addr = Web3.to_checksum_address(config.WALLET_ADDRESS)
        elif address:
            addr = Web3.to_checksum_address(address)
        else:
            addr = get_wallet_address()
        if not addr:
            return None
        raw = w3.eth.get_balance(addr)
        return float(w3.from_wei(raw, "ether"))
    except Exception as e:
        log.error(f"POL balance fetch failed: {e}")
        return None

def send_usdc(to_address: str, amount_usdc: float) -> dict:
    """
    Transfer USDC from the bot wallet to to_address.
    Returns {"ok": True, "tx_hash": "0x..."} on success,
            {"ok": False, "error": "reason"} on failure.
    """
    if not config.PRIVATE_KEY:
        return {"ok": False, "error": "No private key configured"}

    try:
        from_address = get_wallet_address()
        if not from_address:
            return {"ok": False, "error": "Could not derive wallet address"}

        # Validate destination
        if not w3.is_address(to_address):
            return {"ok": False, "error": f"Invalid address: {to_address}"}

        to_checksum = Web3.to_checksum_address(to_address)

        usdc      = _get_usdc_contract()
        decimals  = usdc.functions.decimals().call()
        raw_amount = int(amount_usdc * (10 ** decimals))

        # Safety: check balance first
        balance_raw = usdc.functions.balanceOf(Web3.to_checksum_address(from_address)).call()
        balance_usdc = balance_raw / (10 ** decimals)

        if raw_amount > balance_raw:
            return {
                "ok"   : False,
                "error": f"Insufficient USDC. Wallet has ${balance_usdc:.2f}, tried to send ${amount_usdc:.2f}"
            }

        # Safety: check POL for gas
        pol_balance = get_pol_balance(from_address)
        if pol_balance is not None and float(pol_balance) < 0.001:
            return {
                "ok"   : False,
                "error": f"Insufficient POL for gas. Have {float(pol_balance):.4f} POL, need ~0.001"
            }

        # Build transaction
        nonce    = w3.eth.get_transaction_count(Web3.to_checksum_address(from_address))
        gas_price = w3.eth.gas_price

        tx = usdc.functions.transfer(to_checksum, raw_amount).build_transaction({
            "from"    : Web3.to_checksum_address(from_address),
            "nonce"   : nonce,
            "gasPrice": gas_price,
            "chainId" : config.CHAIN_ID,
        })

        # Estimate gas
        try:
            tx["gas"] = w3.eth.estimate_gas(tx)
        except Exception:
            tx["gas"] = 100_000  # safe fallback for USDC transfers

        # Sign and broadcast
        signed = w3.eth.account.sign_transaction(tx, config.PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        log.info(f"USDC transfer: ${amount_usdc:.2f} to {to_checksum} | tx: {tx_hash_hex}")
        return {"ok": True, "tx_hash": tx_hash_hex, "amount": amount_usdc, "to": to_checksum}

    except Exception as e:
        log.error(f"USDC transfer failed: {e}")
        return {"ok": False, "error": str(e)}


# =======================================================
# LIVE CLOB CLIENT — lazy init
# =======================================================
live_client       = None
live_initialized  = False
live_daily_spent  = 0.0
live_daily_trades = 0
paper_daily_spent  = 0.0   # paper's own daily cap, structurally identical to
paper_daily_trades = 0     # live's — so trade FREQUENCY is comparable, even
paper_daily_reset  = 0      # though the underlying balances stay intentionally
                            # separate. Without this, paper could take far
                            # more trades/day than live's real cap ever allows,
                            # making "paper vs live" volume comparisons unfair.
live_daily_reset  = 0


def check_usdc_allowance():
    """V2: Polymarket handles allowances via deposit flow. Always True."""
    return True


def init_live_client():
    global live_client, live_initialized

    if live_initialized:
        return live_client is not None

    if not config.PRIVATE_KEY:
        log.warning("LIVE: No private key found in .env")
        live_initialized = True
        return False

    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds
        creds = ApiCreds(
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
            api_passphrase=config.API_PASSPHRASE,
        )
        
        funder_addr = None
        if getattr(config, 'PROXY_ADDRESS', None):
            try:
                funder_addr = Web3.to_checksum_address(config.PROXY_ADDRESS)
            except Exception:
                funder_addr = config.PROXY_ADDRESS
                
        live_client = ClobClient(
            host=config.CLOB_HOST,
            chain_id=config.CHAIN_ID,
            key=config.PRIVATE_KEY,
            signature_type=2,
            creds=creds,
            funder=funder_addr,
        )
        live_client.set_api_creds(creds)
        log.info("LIVE: CLOB client initialized")
        live_initialized = True
        return True
    except Exception as e:
        log.error(f"LIVE: Failed to initialize CLOB client: {e}")
        live_initialized = False
        return False

def check_live_safety():
    """Check daily spend and trade caps."""
    global live_daily_spent, live_daily_trades, live_daily_reset

    now = int(time.time())
    midnight = (now // 86400) * 86400

    if live_daily_reset < midnight:
        live_daily_spent  = 0.0
        live_daily_trades = 0
        live_daily_reset  = now

    if live_daily_spent >= config.LIVE_MAX_DAILY:
        log.warning(f"LIVE: daily spend cap reached (${live_daily_spent:.2f}/${config.LIVE_MAX_DAILY:.2f})")
        return False

    if live_daily_trades >= config.LIVE_MAX_TRADES:
        log.warning(f"LIVE: daily trade cap reached ({live_daily_trades}/{config.LIVE_MAX_TRADES})")
        return False

    return True

# =======================================================
# STATE (guarded by state_lock)
# =======================================================
paper_balance      = config.PAPER_BALANCE
open_positions     = []
closed_trades      = []
trade_counter      = 0
state_lock         = Lock()
_pending_live      = {}   # symbol -> count of in-flight live orders (cap reservation)
_orphan_orders     = []   # orders whose cancel failed: kept until confirmed gone or filled


class UnfilledOrderError(RuntimeError):
    """The routine, harmless non-fill: order accepted, never matched,
    cancelled cleanly, no money moved. This is the known and accepted
    behaviour of GTC limit orders on thin books — it needs a log line,
    not a Telegram error. Real-money events (cancel FAILED, orphan
    FILLED) alert separately and are never silenced."""
    pass


def retry_orphan_cancels():
    """
    Called periodically from the main loop. Re-attempts cancellation of
    orders whose original cancel failed. A resting GTC order that can't
    be cancelled is live capital exposure: it locks collateral and can
    fill at any moment as an UNTRACKED position — the confirmed mechanism
    behind "balance decreasing with no recorded losses". This keeps
    trying until each orphan is confirmed terminal, and alerts loudly if
    one fills in the meantime.
    """
    with state_lock:
        orphans = list(_orphan_orders)
    if not orphans:
        return

    for o in orphans:
        oid = o["order_id"]
        o["attempts"] = o.get("attempts", 0) + 1

        status_raw = None
        try:
            status_raw = live_client.get_order(oid)
        except Exception as e:
            log.warning(f"ORPHAN: status check failed for {oid[:12]}…: {e}")

        status_val = ""
        if isinstance(status_raw, dict):
            status_val = str(status_raw.get("status", "")).lower()
        filled = _extract_filled_size(status_raw)

        if filled > 0:
            log.error(
                f"ORPHAN FILLED: {oid} on {o['symbol'].upper()} {o['interval']} "
                f"filled {filled} — REAL untracked position exists on Polymarket."
            )
            alerts.bot_error(
                f"🚨 Orphan order {oid[:12]}… on {o['symbol'].upper()} {o['interval']} "
                f"FILLED ({filled} shares) — a REAL untracked position now exists on "
                f"Polymarket. It will resolve there on its own; check your portfolio."
            )
            with state_lock:
                _orphan_orders[:] = [x for x in _orphan_orders if x["order_id"] != oid]
            continue

        if any(m in status_val for m in _TERMINAL_UNFILLED_MARKERS):
            log.info(f"ORPHAN cleared: {oid[:12]}… now terminal ({status_val}).")
            with state_lock:
                _orphan_orders[:] = [x for x in _orphan_orders if x["order_id"] != oid]
            continue

        try:
            res = live_client.cancel_order(oid)
            if isinstance(res, dict) and oid in (res.get("canceled") or []):
                log.info(f"ORPHAN cancelled on retry {o['attempts']}: {oid[:12]}…")
                with state_lock:
                    _orphan_orders[:] = [x for x in _orphan_orders if x["order_id"] != oid]
                continue
        except Exception as e:
            log.warning(f"ORPHAN: cancel retry {o['attempts']} failed for {oid[:12]}…: {e}")

        if o["attempts"] >= 30:
            alerts.bot_error(
                f"🚨 GIVING UP on orphan order {oid[:12]}… ({o['symbol'].upper()}) after "
                f"{o['attempts']} cancel attempts — MANUAL ACTION REQUIRED: cancel it on "
                f"polymarket.com open orders."
            )
            with state_lock:
                _orphan_orders[:] = [x for x in _orphan_orders if x["order_id"] != oid]
last_resolve_check = 0

# Auto-updated Kelly parameters (start from config defaults)
_kelly_win_rate = config.KELLY_WIN_RATE
_kelly_avg_odds = config.KELLY_AVG_ODDS

# =======================================================
# CSV LOG
# =======================================================
def init_trade_log():
    """Create both CSV files with headers if they don't exist."""
    for log_file in [config.TRADE_LOG_FLAT, config.TRADE_LOG_KELLY]:
        if not os.path.exists(log_file):
            with open(log_file, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "trade_id", "timestamp", "mode", "symbol", "interval",
                    "direction", "entry_side", "entry_price", "fill_price",
                    "stake", "shares", "slug", "end_date",
                    "chainlink_entry", "chainlink_exit",
                    "result", "payout", "pnl", "balance_after",
                    "order_id", "retrace_pct", "watched_for_sec", "timed_out_fill"
                ])

def log_trade(trade):
    """Append a trade row to the active CSV log."""
    with open(config.get_log_file(), "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            trade["trade_id"],
            trade["timestamp"],
            trade["mode"],
            trade["symbol"],
            trade["interval"],
            trade["direction"],
            trade["entry_side"],
            trade["entry_price"],
            trade["fill_price"],
            trade["stake"],
            trade["shares"],
            trade["slug"],
            trade["end_date"],
            trade.get("chainlink_entry", ""),
            trade.get("chainlink_exit", ""),
            trade.get("result", "OPEN"),
            trade.get("payout", ""),
            trade.get("pnl", ""),
            trade.get("balance_after", ""),
            trade.get("order_id", ""),
            trade.get("retrace_pct", ""),
            trade.get("watched_for_sec", ""),
            trade.get("timed_out_fill", ""),
        ])

# =======================================================
# PERSISTENCE — restore open positions on startup
# =======================================================
def load_open_positions():
    """Clean slate on every restart. No positions restored."""
    log.info("Clean start — no positions restored from previous session")
    return


def estimate_fill_price(midpoint_price, side="BUY"):
    slippage = midpoint_price * (config.SLIPPAGE_BPS / 10000)
    if side == "BUY":
        return min(midpoint_price + slippage, 0.99)
    return max(midpoint_price - slippage, 0.01)

# =======================================================
# KELLY SIZING
# =======================================================
def calculate_kelly():
    """Compute the raw Kelly fraction from current parameters."""
    p = _kelly_win_rate
    q = 1 - p
    b = _kelly_avg_odds
    raw = (b * p - q) / b
    return max(0, raw)

def get_stake_size(bankroll, entry_price=None):
    """Stake for the next trade based on sizing mode and per-trade odds."""
    if config.SIZING_MODE == "flat":
        return config.FLAT_STAKE

    if bankroll <= 0:
        return config.KELLY_MIN_STAKE

    if entry_price and 0 < entry_price < 1:
        per_trade_odds = (1.0 / entry_price) - 1.0
    else:
        per_trade_odds = _kelly_avg_odds

    p = _kelly_win_rate
    q = 1 - p
    b = per_trade_odds

    raw_kelly = (b * p - q) / b
    if raw_kelly <= 0:
        return config.KELLY_MIN_STAKE

    adjusted = raw_kelly * config.KELLY_FRACTION
    stake    = bankroll * adjusted

    stake = max(stake, config.KELLY_MIN_STAKE)
    # getattr-guarded: confirmed real incident (2026-06-03) where a missing
    # KELLY_MAX_STAKE crashed the ENTIRE bot process via print_performance()
    # -> get_kelly_info() -> here, not just one trade. This function is also
    # called from both paper_execute and live_execute, so an unguarded read
    # here is a single point of failure for all trading, not just reporting.
    kelly_max_stake = getattr(config, "KELLY_MAX_STAKE", 9999)
    kelly_max_bankroll_pct = getattr(config, "KELLY_MAX_BANKROLL_PCT", 1.0)
    # Kelly max stake scales with fraction: kelly25 = $50, kelly50 = $100, kelly100 = $200
    # When caps are disabled (KELLY_MAX_STAKE >= 9999), there's no upper limit
    if kelly_max_stake < 9999:
        dynamic_max = kelly_max_stake * (config.KELLY_FRACTION / 0.25)
        stake = min(stake, dynamic_max)
    if kelly_max_bankroll_pct < 0.999:
        stake = min(stake, bankroll * kelly_max_bankroll_pct)

    return round(stake, 2)

def get_kelly_info(bankroll, entry_price=None):
    """Return Kelly sizing details for display."""
    stake = get_stake_size(bankroll, entry_price)
    raw   = calculate_kelly()

    if entry_price and 0 < entry_price < 1:
        per_trade_odds = (1.0 / entry_price) - 1.0
        trade_kelly    = ((per_trade_odds * _kelly_win_rate) - (1 - _kelly_win_rate)) / per_trade_odds
        trade_kelly_adj = max(0, trade_kelly * config.KELLY_FRACTION)
    else:
        trade_kelly_adj = raw * config.KELLY_FRACTION

    return {
        "stake"          : stake,
        "raw_kelly"      : raw,
        "adjusted_kelly" : trade_kelly_adj,
        "fraction"       : config.KELLY_FRACTION,
        "bankroll_pct"   : (stake / bankroll * 100) if bankroll > 0 else 0,
    }

def update_kelly_from_history():
    """Refit Kelly params from closed trades once we have enough data."""
    global _kelly_win_rate, _kelly_avg_odds

    if len(closed_trades) < config.KELLY_AUTOUPDATE_MIN_TRADES:
        return

    wins  = [t for t in closed_trades if t["result"] == "WIN"]
    total = len(closed_trades)

    if total == 0:
        return

    new_win_rate = len(wins) / total

    if wins:
        avg_entry    = sum(t["entry_price"] for t in wins) / len(wins)
        new_avg_odds = (1.0 / avg_entry) - 1.0 if avg_entry > 0 else _kelly_avg_odds
    else:
        new_avg_odds = _kelly_avg_odds

    old_wr, old_odds = _kelly_win_rate, _kelly_avg_odds

    _kelly_win_rate = round(new_win_rate, 4)
    _kelly_avg_odds = round(new_avg_odds, 4)

    if abs(old_wr - _kelly_win_rate) > 0.01 or abs(old_odds - _kelly_avg_odds) > 0.05:
        log.info(
            f"Kelly updated from {len(closed_trades)} trades: "
            f"WR {old_wr:.1%} -> {_kelly_win_rate:.1%}, "
            f"odds {old_odds:.3f} -> {_kelly_avg_odds:.3f}, "
            f"raw f* {calculate_kelly():.1%}"
        )

# =======================================================
# PAPER EXECUTE
# =======================================================

def check_polymarket_resolution(slug):
    """Use Gamma API outcomePrices - most accurate resolution source."""
    try:
        import requests as _req
        import json as _json
        r = _req.get(f"{config.GAMMA_API}/markets", params={"slug": slug}, timeout=3)
        if r.status_code != 200:
            return None, None
        data = r.json()
        if not isinstance(data, list) or not data:
            return None, None
        market = data[0]

        # Only trust Polymarket API if the market is formally closed or resolved.
        # Prevents false 'WIN' when price wicks to 0.99 just before the market officially closes.
        is_closed = market.get("closed", False)
        is_resolved = market.get("resolved", False)
        if not (is_closed or is_resolved):
            return None, None

        prices   = market.get("outcomePrices", [])
        outcomes = market.get("outcomes", [])
        if isinstance(prices, str):   prices   = _json.loads(prices)
        if isinstance(outcomes, str): outcomes = _json.loads(outcomes)

        for i, price in enumerate(prices):
            try:
                if float(price) >= 0.99:
                    label = outcomes[i].strip().lower() if i < len(outcomes) else ""
                    if label in ("up", "yes"): return "YES", prices
                    if label in ("down", "no"): return "NO", prices
            except (ValueError, TypeError):
                pass

        tokens = market.get("tokens", [])
        if isinstance(tokens, str): tokens = _json.loads(tokens)
        for token in tokens:
            if token.get("winner"):
                label = token.get("outcome", "").strip().lower()
                if label in ("up", "yes"): return "YES", prices
                if label in ("down", "no"): return "NO", prices

        if market.get("closed") or market.get("resolved"):
            outcome = str(market.get("outcome", "")).strip()
            
            # Handle if outcome is an index string (e.g. "0", "1")
            if outcome.isdigit() and int(outcome) < len(outcomes):
                outcome = outcomes[int(outcome)].strip().lower()
            else:
                outcome = outcome.lower()
                
            if outcome in ("up", "yes"): return "YES", prices
            if outcome in ("down", "no"): return "NO", prices

        return None, None
    except Exception as e:
        log.debug(f"PM resolution check failed for {slug}: {e}")
        return None, None


def paper_execute(signal):
    """Enter a paper trade. Single canonical implementation (no duplicate).
    Returns True if a trade was genuinely opened, False if blocked for any
    reason — callers (feed_fetcher's funnel) rely on this to distinguish
    "attempted" from "actually happened", which they previously could not."""
    global paper_balance, trade_counter, paper_daily_spent, paper_daily_trades, paper_daily_reset

    with state_lock:
        # Enforce max open positions per mode
        open_count = len([t for t in open_positions if t["mode"] == "paper"])
        if open_count >= config.MAX_OPEN_POSITIONS:
            log.info(f"PAPER: max positions reached ({open_count}/{config.MAX_OPEN_POSITIONS})")
            return False

        # Enforce per-symbol cap
        sym_open = len([t for t in open_positions
                        if t["mode"] == "paper" and t["symbol"] == signal["symbol"]])
        if sym_open >= config.MAX_PER_SYMBOL:
            log.info(f"PAPER: per-symbol cap reached for {signal['symbol'].upper()}")
            return False

        # Paper's own daily cap — same reset pattern as check_live_safety.
        # Defaults unbounded (see config.py); only binds if you deliberately
        # tighten it for a fair frequency comparison against live.
        now = int(time.time())
        midnight = (now // 86400) * 86400
        if paper_daily_reset < midnight:
            paper_daily_spent  = 0.0
            paper_daily_trades = 0
            paper_daily_reset  = now
        # getattr with safe defaults: these keys were added late, and if a
        # deployed config.py predates them (or an append didn't land), a
        # direct config.X read raises AttributeError on EVERY signal —
        # before any log line runs. That produced a silent total outage:
        # thousands of signals attempted, zero trades, zero errors logged,
        # every funnel gate reading zero. Never hard-depend on a late-added
        # config key.
        _paper_max_daily  = getattr(config, "PAPER_MAX_DAILY", 1000000.00)
        _paper_max_trades = getattr(config, "PAPER_MAX_TRADES", 1000000)

        if paper_daily_spent >= _paper_max_daily:
            log.info(f"PAPER: daily spend cap reached (${paper_daily_spent:.2f}/${_paper_max_daily:.2f})")
            return False
        if paper_daily_trades >= _paper_max_trades:
            log.info(f"PAPER: daily trade cap reached ({paper_daily_trades}/{_paper_max_trades})")
            return False
        entry_price  = signal.get("entry_price", 0)
        high_quality = signal.get("high_quality", False)
        token_id     = signal.get("token_to_buy")

        base_stake   = get_stake_size(paper_balance, entry_price)
        target_stake = round(base_stake * 1.10, 2) if high_quality else base_stake

        if paper_balance < target_stake:
            if paper_balance >= config.KELLY_MIN_STAKE:
                target_stake = round(paper_balance * 0.5, 2)
            else:
                log.warning(f"PAPER: insufficient balance (${paper_balance:.2f} < ${config.KELLY_MIN_STAKE:.2f})")
                return False

        # Concurrent exposure cap: total open stake must not exceed
        # MAX_CONCURRENT_EXPOSURE × (free balance + locked stake).
        if config.MAX_CONCURRENT_EXPOSURE < 1.0:
            open_stake     = sum(t["stake"] for t in open_positions if t["mode"] == "paper")
            total_bankroll = paper_balance + open_stake
            budget         = total_bankroll * config.MAX_CONCURRENT_EXPOSURE
            available      = max(0.0, budget - open_stake)
            if available < config.KELLY_MIN_STAKE:
                log.info(
                    f"PAPER: concurrent exposure cap ({config.MAX_CONCURRENT_EXPOSURE:.0%}) reached "
                    f"— open=${open_stake:.2f}, budget=${budget:.2f}, bankroll=${total_bankroll:.2f}"
                )
                return False
            if target_stake > available:
                log.info(
                    f"PAPER: stake capped by exposure limit ${target_stake:.2f} → ${available:.2f} "
                    f"(open=${open_stake:.2f}, budget=${budget:.2f})"
                )
                target_stake = round(available, 2)

        # Trim to remaining daily budget — mirrors live_execute's own
        # "stake = LIVE_MAX_DAILY - live_daily_spent" trim exactly. The
        # earlier check above only rejects if ALREADY at/over the cap;
        # without this trim a trade could still push spending past the
        # cap by up to one trade's worth, same gap live_execute closes.
        daily_remaining = round(_paper_max_daily - paper_daily_spent, 2)
        if target_stake > daily_remaining:
            if daily_remaining < config.KELLY_MIN_STAKE:
                log.info(
                    f"PAPER: remaining daily budget ${daily_remaining:.2f} < "
                    f"min ${config.KELLY_MIN_STAKE:.2f} — skipping"
                )
                return False
            log.info(
                f"PAPER: stake trimmed to remaining daily budget "
                f"${target_stake:.2f} → ${daily_remaining:.2f}"
            )
            target_stake = daily_remaining

    # Depth-aware fill simulation happens OUTSIDE state_lock — same pattern
    # live_execute uses for its own network-bound pricing. target_stake is
    # now the FULL intended size; this determines how much of it the real
    # book could actually have absorbed, exactly like live's partial-fill
    # tracking, instead of assuming infinite liquidity at one price.
    fill_price     = None
    final_stake    = target_stake
    used_real_book = False

    if getattr(config, "PAPER_USE_REAL_BOOK", True) and token_id:
        achievable_stake, avg_price, achievable_shares, readable = _simulate_book_fill(
            token_id, target_stake, config.ENTRY_ZONE_MAX
        )
        if achievable_stake > 0:
            if achievable_stake < target_stake * 0.999:
                log.info(
                    f"PAPER: book depth only supports ${achievable_stake:.2f} of the "
                    f"intended ${target_stake:.2f} under maxentry {config.ENTRY_ZONE_MAX} "
                    f"— filling the smaller REAL amount (partial fill, like live)"
                )
            final_stake    = achievable_stake
            fill_price      = avg_price
            used_real_book = True
        elif readable:
            # Book WAS successfully read and is genuinely empty/too
            # expensive under the price ceiling — a real "live wouldn't
            # have filled this either" signal. Correctly skip.
            log.info(
                f"PAPER: no real liquidity under maxentry {config.ENTRY_ZONE_MAX} — "
                f"skipping (live would not have filled this either)"
            )
            return False
        # else: readable=False -> book could not be checked at all (no
        # credentials, network failure). Deliberately falls through to
        # the estimate fallback below instead of skipping — this is the
        # exact case that silently zeroed out 467 paper attempts.

    if fill_price is None:
        # Real book unavailable (no credentials, or a read failure) —
        # fall back to the original synthetic-slippage estimate rather
        # than block paper mode entirely.
        fill_price  = estimate_fill_price(entry_price)
        final_stake = target_stake

    with state_lock:
        trade_counter += 1
        trade_id = f"P-{trade_counter:04d}"
        stake    = final_stake

        shares          = stake / fill_price
        chainlink_entry = signal.get("chainlink_price", 0)
        kelly_info      = get_kelly_info(paper_balance, entry_price)

        paper_balance -= stake

        trade = {
            "trade_id"         : trade_id,
            "timestamp"        : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode"             : "paper",
            "symbol"           : signal["symbol"],
            "interval"         : signal["interval"],
            "direction"        : signal["direction"],
            "entry_side"       : signal["entry_side"],
            "entry_price"      : entry_price,
            "fill_price"       : fill_price,
            "stake"            : stake,
            "shares"           : shares,
            "slug"             : signal["slug"],
            "retrace_pct"      : signal.get("retrace_pct", ""),
            "watched_for_sec"  : signal.get("watched_for_sec", ""),
            "timed_out_fill"   : signal.get("timed_out_fill", ""),
            "end_date"         : signal["end_date"],
            "chainlink_entry"  : chainlink_entry,
            "binance_open"     : signal.get("binance_open"),
            "token_id"         : signal["token_to_buy"],
            "resolve_attempts" : 0,
            "used_real_book"   : used_real_book,
        }

        open_positions.append(trade)
        paper_daily_spent  += stake
        paper_daily_trades += 1
        try:
            log_trade(trade)
        except Exception as log_err:
            log.error(f"log_trade failed for REAL position {trade_id} (trade is still tracked in memory): {log_err}")

    kelly_label = f"{config.KELLY_FRACTION:.0%} Kelly" if config.SIZING_MODE == "kelly" else "Flat"
    log.info(
        f"PAPER ENTERED {trade_id}: {signal['symbol'].upper()} {signal['interval']} "
        f"{signal['direction']} @ {fill_price:.3f} stake=${stake:.2f} ({kelly_label}) "
        f"balance=${paper_balance:.2f}"
    )

    try:
        alerts.trade_entered(
            trade_id, signal["symbol"], signal["interval"],
            signal["direction"], signal["entry_side"],
            fill_price, stake, "paper"
        )
    except Exception as alert_err:
        log.error(f"trade_entered alert failed for REAL position {trade_id} (trade is still tracked): {alert_err}")

    return "paper"

# =======================================================
# LIVE EXECUTE
# =======================================================
def _get_ask_levels(token_id):
    """
    Full ask-side depth (price, size) sorted cheapest-first — not just the
    single best price. Same defensive parsing as _get_best_ask, extended
    to also pull size.

    Returns (levels, readable):
      readable=True,  levels=[]     -> book WAS read successfully and is
                                        genuinely empty (or unparsable) —
                                        a real "no liquidity" signal.
      readable=False, levels=[]     -> book could NOT be read at all (no
                                        credentials, init failure, network
                                        error) — caller must NOT treat this
                                        as "no liquidity"; it must fall back
                                        to the synthetic estimate instead.
    Collapsing these two into one signal is exactly what caused paper mode
    to silently skip EVERY trade when live credentials were unavailable —
    467 attempted, 0 confirmed, over 24 hours — instead of falling back
    the way it was designed to.
    """
    global live_client
    if live_client is None:
        try:
            if not init_live_client():
                return [], False
        except Exception as e:
            log.debug(f"PAPER/LIVE: could not init client for book read: {e}")
            return [], False

    try:
        book = live_client.get_order_book(token_id)
    except Exception as e:
        log.warning(f"PAPER/LIVE: could not read order book depth: {e}")
        return [], False

    # A None / unusable response is NOT the same as "the book is empty".
    # This is the production case that silently killed every paper trade:
    # the read returns nothing usable, and treating that as a real empty
    # book made paper skip every single signal. Unreadable -> fall back.
    if book is None or isinstance(book, (str, int, float, bool)):
        log.warning(f"PAPER/LIVE: order book response unusable ({type(book).__name__}) — treating as UNREADABLE, not empty")
        return [], False

    # Distinguish "asks field exists and is an empty list" (genuinely no
    # liquidity — a real signal) from "no recognizable asks field at all"
    # (unexpected response shape — unreadable, must fall back).
    has_asks_field = False
    asks = None
    if isinstance(book, dict):
        for k in ("asks", "sells"):
            if k in book:
                has_asks_field = True
                asks = book.get(k)
                if asks:
                    break
    else:
        for k in ("asks", "sells"):
            if hasattr(book, k):
                has_asks_field = True
                asks = getattr(book, k, None)
                if asks:
                    break

    if not has_asks_field:
        log.warning(f"PAPER/LIVE: order book has no recognizable asks field — treating as UNREADABLE, not empty. raw={book!r}")
        return [], False

    if not asks:
        return [], True   # asks field present but empty — genuinely no liquidity

    levels = []
    for lvl in asks:
        if isinstance(lvl, dict):
            p, s = lvl.get("price"), lvl.get("size")
        else:
            p, s = getattr(lvl, "price", None), getattr(lvl, "size", None)
        try:
            if p is not None and s is not None:
                levels.append((float(p), float(s)))
        except (TypeError, ValueError):
            continue

    if not levels:
        # asks existed but NOTHING parsed into usable (price,size) pairs —
        # that's an unexpected level shape, i.e. a parse failure, not a
        # genuinely empty book. Fall back rather than skip every trade.
        log.warning(f"PAPER/LIVE: asks present but no parsable price/size levels — treating as UNREADABLE. raw={asks!r}")
        return [], False

    levels.sort(key=lambda x: x[0])
    return levels, True


def _simulate_book_fill(token_id, desired_stake, price_ceiling):
    """
    Walks real ask-side depth to determine what a market participant could
    ACTUALLY get filled for a given dollar stake — not just whether the
    single best price is acceptable. A best ask of 0.68¢ with only $2 of
    real size behind it does not mean a $20 stake fills at 0.68¢; the
    remainder has to walk up the book, exactly like it would on Polymarket.

    Returns (achievable_stake, avg_fill_price, achievable_shares, readable).
    achievable_stake may be LESS than desired_stake if the book is thin —
    this is the direct depth-aware analogue of live's partial-fill
    tracking, applied to paper so paper stops assuming infinite liquidity
    at the top-of-book price.

    readable=False means the book could not be checked at all (no
    credentials, network failure) — callers MUST fall back to the
    synthetic estimate in that case, not treat it as zero liquidity.
    Conflating these two produced a real incident: 467 paper attempts,
    0 confirmed, over 24 hours, because a read failure was silently
    treated as "no liquidity, skip every trade."
    """
    levels, readable = _get_ask_levels(token_id)
    if not levels:
        return 0.0, None, 0.0, readable

    remaining_stake = desired_stake
    total_shares     = 0.0
    total_spent      = 0.0

    for price, size in levels:
        if price > price_ceiling:
            break
        level_capacity = price * size
        take_stake = min(remaining_stake, level_capacity)
        if take_stake <= 0:
            break
        shares = take_stake / price
        total_shares += shares
        total_spent  += take_stake
        remaining_stake -= take_stake
        if remaining_stake <= 0.005:
            break

    if total_shares <= 0:
        return 0.0, None, 0.0, readable

    avg_price = total_spent / total_shares
    return round(total_spent, 4), round(avg_price, 4), round(total_shares, 4), readable


def _get_best_ask(token_id):
    """
    Lowest price someone is currently willing to SELL at. Returns None if
    the book can't be read or has no asks — callers must handle that
    rather than assume a price. Defensive about response shape since the
    exact schema isn't documented here; logs the raw book once on failure
    so the real field names can be confirmed from production output.
    """
    try:
        book = live_client.get_order_book(token_id)
    except Exception as e:
        log.warning(f"LIVE: could not read order book: {e}")
        return None

    asks = None
    if isinstance(book, dict):
        asks = book.get("asks") or book.get("sells")
    else:
        asks = getattr(book, "asks", None) or getattr(book, "sells", None)
    if not asks:
        log.warning(f"LIVE: order book has no asks. raw={book!r}")
        return None

    prices = []
    for lvl in asks:
        p = None
        if isinstance(lvl, dict):
            p = lvl.get("price")
        else:
            p = getattr(lvl, "price", None)
        try:
            if p is not None:
                prices.append(float(p))
        except (TypeError, ValueError):
            continue

    if not prices:
        log.warning(f"LIVE: could not parse ask prices. raw={asks!r}")
        return None
    return min(prices)


_TERMINAL_UNFILLED_MARKERS = ("cancel", "expired", "unmatched", "killed",
                               "rejected", "invalid", "failed")


def _extract_filled_size(order_status):
    """
    Returns the size Polymarket EXPLICITLY reports as matched. Returns 0.0
    unless there is a direct, positive assertion of a fill.

    Two rules, both derived from confirmed production data rather than
    assumption:

    1. NEVER infer a fill size from a status string. The old code fell
       back to `original_size` whenever status looked like "matched" but
       no matched-size field was present. That invented a 24.05-share
       position out of an order whose true final state was
       status='CANCELED_MARKET_RESOLVED', size_matched='0',
       associate_trades=[] — the phantom size matched original_size
       exactly. A missing field means UNKNOWN, which must be treated as
       unfilled, never as "fully filled".

    2. A terminal cancelled/expired/unmatched status is definitively not
       a fill, whatever any other field says.
    """
    if not isinstance(order_status, dict):
        return 0.0

    status_val = str(order_status.get("status", "")).lower()
    if any(marker in status_val for marker in _TERMINAL_UNFILLED_MARKERS):
        return 0.0

    for key in ("size_matched", "sizeMatched", "matched_size", "matchedSize",
                "filled_size", "filledSize", "size_filled", "sizeFilled"):
        if key in order_status:
            try:
                val = float(order_status[key])
            except (TypeError, ValueError):
                continue
            return val if val > 0 else 0.0

    # No explicit matched-size field at all -> unknown -> not filled.
    log.warning(
        f"_extract_filled_size: no explicit matched-size field present; "
        f"treating as UNFILLED rather than inferring one. raw={order_status!r}"
    )
    return 0.0


def _confirm_fill(order_id, expected_size, reads=5, delay=0.3):
    """
    Confirms a fill only when two CONSECUTIVE reads report the same
    positive matched size. Polymarket's order state was observed to be
    transiently inconsistent inside a short polling window; a value that
    appears once and then changes is not trustworthy, while a transient
    blip by definition does not survive a second consecutive read.

    Returns (confirmed_size, last_raw_response). confirmed_size is 0.0
    unless two consecutive reads agree on the same positive value.
    """
    consecutive = 0
    last_val    = None
    raw         = None

    for i in range(reads):
        try:
            raw = live_client.get_order(order_id)
        except Exception as e:
            log.warning(f"LIVE: fill check read {i+1}/{reads} failed: {e}")
            raw = None
            consecutive = 0
            last_val = None
            if i < reads - 1:
                time.sleep(delay)
            continue

        val = _extract_filled_size(raw)

        if val > 0 and last_val is not None and abs(val - last_val) < 1e-9:
            consecutive += 1
        elif val > 0:
            consecutive = 1
        else:
            consecutive = 0

        last_val = val

        if consecutive >= 2:
            log.info(f"LIVE: fill confirmed by 2 consecutive reads: {val} shares")
            return val, raw

        if i < reads - 1:
            time.sleep(delay)

    if last_val and last_val > 0:
        log.warning(
            f"LIVE: saw a positive matched size ({last_val}) but it was never "
            f"confirmed by two consecutive reads — treating as UNFILLED. raw={raw!r}"
        )
    return 0.0, raw


def live_execute(signal):
    """Returns True if a real trade was genuinely opened (live OR a paper
    fallback that itself succeeded), False if nothing real happened at all."""
    global trade_counter, live_daily_spent, live_daily_trades

    if not init_live_client():
        log.warning("LIVE: client unavailable — NO trade taken")
        if getattr(config, "LIVE_PAPER_FALLBACK", False):
            return paper_execute(signal)
        return False

    if not check_live_safety():
        return False

    with state_lock:
        # Count PENDING in-flight orders too — the cap check and the final
        # position append are separated by several seconds of retries and
        # fill-checks, and the signal detector polls every 0.5s. Without
        # reserving a slot here atomically, the same signal re-fires
        # during that window, sees no open position yet, and passes the
        # caps again — this exact race produced 3 simultaneous SOL and
        # 3 simultaneous BTC entries. Slot is released on every exit path.
        pend_total = sum(_pending_live.values())
        pend_sym   = _pending_live.get(signal["symbol"], 0)

        open_count = len([t for t in open_positions if t["mode"] == "live"]) + pend_total
        if open_count >= config.MAX_OPEN_POSITIONS:
            log.info(f"LIVE: max positions reached ({open_count}/{config.MAX_OPEN_POSITIONS})")
            return False

        sym_open = len([t for t in open_positions
                        if t["mode"] == "live" and t["symbol"] == signal["symbol"]]) + pend_sym
        if sym_open >= config.MAX_PER_SYMBOL:
            log.info(f"LIVE: per-symbol cap reached for {signal['symbol'].upper()}")
            return False

        _pending_live[signal["symbol"]] = pend_sym + 1

    try:
        return _live_execute_inner(signal)
    finally:
        with state_lock:
            _pending_live[signal["symbol"]] = max(0, _pending_live.get(signal["symbol"], 1) - 1)


def _live_execute_inner(signal):
    global trade_counter, live_daily_spent, live_daily_trades

    from py_clob_client_v2.order_builder.constants import BUY
    from py_clob_client_v2.clob_types import OrderArgs

    token_id    = signal["token_to_buy"]
    entry_price = signal["entry_price"]

    # Allowance check — ensure Polymarket can spend our USDC
    if not check_usdc_allowance():
        log.warning("LIVE: insufficient USDC allowance — approve on Polymarket website")
        alerts.bot_error("USDC allowance too low. Visit polymarket.com and approve USDC spending.")
        return False

    # CRITICAL FIX: live_bankroll was previously just
    # (LIVE_MAX_DAILY - live_daily_spent) — with LIVE_MAX_DAILY at its
    # placeholder default of $1,000,000, every stake/exposure calculation
    # was sizing against a fictional million-dollar bankroll instead of
    # the real account balance. A 20% exposure cap on $1,000,000 is
    # $200,000 — no real protection at all on an actual $100 account.
    # Fix: use the REAL fetched balance as the sizing reference, and let
    # LIVE_MAX_DAILY act as a genuinely separate daily-spend circuit
    # breaker layered on top, not the sizing bankroll itself.
    real_balance = get_usdc_balance()
    if real_balance is None:
        log.error("LIVE: could not fetch real balance — refusing to size a trade blind")
        alerts.bot_error("Live balance fetch failed — skipping this trade rather than sizing against an unknown bankroll.")
        return False

    daily_remaining = config.LIVE_MAX_DAILY - live_daily_spent
    live_bankroll   = min(real_balance, daily_remaining)

    # Quality score stake boost — high quality signals (79+) get 10% more
    high_quality = signal.get("high_quality", False)
    base_stake   = get_stake_size(live_bankroll, entry_price)
    stake        = round(base_stake * 1.10, 2) if high_quality else base_stake

    if stake + live_daily_spent > config.LIVE_MAX_DAILY:
        stake = round(config.LIVE_MAX_DAILY - live_daily_spent, 2)
        if stake < config.KELLY_MIN_STAKE:
            log.warning(f"LIVE: remaining daily budget ${stake:.2f} < min ${config.KELLY_MIN_STAKE:.2f}")
            return False

    # Concurrent exposure cap (same logic as paper)
    if config.MAX_CONCURRENT_EXPOSURE < 1.0:
        with state_lock:
            open_stake = sum(t["stake"] for t in open_positions if t["mode"] == "live")
        total_bankroll = live_bankroll + open_stake
        budget         = total_bankroll * config.MAX_CONCURRENT_EXPOSURE
        available      = max(0.0, budget - open_stake)
        if available < config.KELLY_MIN_STAKE:
            log.info(
                f"LIVE: concurrent exposure cap ({config.MAX_CONCURRENT_EXPOSURE:.0%}) reached "
                f"— open=${open_stake:.2f}, budget=${budget:.2f}"
            )
            return False
        if stake > available:
            log.info(
                f"LIVE: stake capped by exposure limit ${stake:.2f} → ${available:.2f} "
                f"(open=${open_stake:.2f}, budget=${budget:.2f})"
            )
            stake = round(available, 2)

    # Price against the REAL book, not a guessed buffer. Posting a blind
    # limit (midpoint, or +0.08 capped at maxentry) is why orders sat
    # unfilled: if the best ask is above our limit, nothing matches. Read
    # the actual best ask and pay that. Fills happen AT the ask, so this
    # does not overpay — it just makes the order marketable.
    if not alerts.is_trading_active():
        log.info(f"LIVE: trading paused — aborting before pricing for {signal['symbol'].upper()} {signal['interval']}")
        return False
    best_ask = _get_best_ask(token_id)
    if best_ask is not None:
        if best_ask > config.ENTRY_ZONE_MAX:
            log.info(
                f"LIVE: best ask {best_ask:.3f} is above maxentry "
                f"{config.ENTRY_ZONE_MAX} — skipping rather than overpaying"
            )
            return False
        order_price = round(min(best_ask, config.ENTRY_ZONE_MAX), 2)
    else:
        # Book unreadable — fall back to a small buffer, still capped by
        # the user's max entry so we can never pay above their limit.
        order_price = round(min(entry_price + 0.02, config.ENTRY_ZONE_MAX), 2)
    size        = round(stake / order_price, 2)

    # V2: minimum 5 shares
    if size < 5:
        min_stake    = round(5 * order_price, 2)
        real_balance = get_usdc_balance() or 0.0
        if real_balance >= min_stake:
            stake = min_stake
            size  = 5.0
        else:
            log.warning(
                f"LIVE: can't meet 5-share minimum — need ${min_stake:.2f}, "
                f"on-chain balance ${real_balance:.2f}."
            )
            return False

    with state_lock:
        trade_counter += 1
        trade_id = f"L-{trade_counter:04d}"

    try:
        order_args = OrderArgs(
            token_id = token_id,
            price    = order_price,
            size     = size,
            side     = BUY,
        )

        MAX_ORDER_ATTEMPTS = 3
        RETRY_DELAY_SEC    = 1.0
        TRANSIENT_MARKERS  = ("not ready", "425", "rate limit", "try again", "timeout")

        response   = None
        last_error = None
        for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
            # LAST-MOMENT STOP CHECK. is_trading_active() is only checked
            # once, at entry to execute() — a signal that already passed
            # that gate used to run to completion regardless, and posting
            # now takes several seconds (retries + fill confirmation), so
            # /stop could appear confirmed while an already-in-flight
            # signal still submitted a real order seconds later. This is
            # the actual point of no return — check again right here,
            # as late as possible, since nothing after this line can be
            # recalled once it's sent.
            if not alerts.is_trading_active():
                log.info(f"LIVE: trading paused mid-flight — aborting before order submission for {signal['symbol'].upper()} {signal['interval']}")
                return False
            try:
                signed_order = live_client.create_order(order_args)
                response     = live_client.post_order(signed_order)
                last_error = None
                break
            except Exception as post_err:
                last_error = post_err
                err_text = str(post_err).lower()
                is_transient = any(marker in err_text for marker in TRANSIENT_MARKERS)
                if is_transient and attempt < MAX_ORDER_ATTEMPTS:
                    log.warning(
                        f"LIVE order attempt {attempt}/{MAX_ORDER_ATTEMPTS} hit a transient "
                        f"error, retrying in {RETRY_DELAY_SEC}s: {post_err}"
                    )
                    time.sleep(RETRY_DELAY_SEC)
                    continue
                raise

        order_id = ""
        if isinstance(response, dict):
            order_id = response.get("orderID", response.get("id", ""))
        elif isinstance(response, str):
            order_id = response

        order_failed = not order_id
        error_detail = ""
        if isinstance(response, dict):
            if response.get("success") is False:
                order_failed = True
            error_detail = response.get("errorMsg") or response.get("error") or ""

        if order_failed:
            raise RuntimeError(
                f"Order rejected or no order_id returned. "
                f"detail={error_detail!r} raw_response={response!r}"
            )

        # An order_id means ACCEPTED/POSTED, not MATCHED. Poll for the
        # actual fill before trusting this as a real position; cancel and
        # take no trade if it never fills (the known, accepted "little
        # bug": occasional unfilled-order errors, honest and safe).
        # An order_id means ACCEPTED/POSTED, not MATCHED. Confirm the fill
        # with two consecutive agreeing reads before trusting it (a single
        # transient read is what produced a phantom position).
        filled_size, order_status_raw = _confirm_fill(order_id, size, reads=5, delay=0.3)

        if filled_size < size * 0.999:
            # The order did not (fully) fill within the poll window. Try
            # to cancel it — with RETRIES, since a single failed cancel
            # (transient error, rate limit) previously left a GTC order
            # silently resting on the book, able to fill later untracked:
            # the confirmed mechanism behind "balance decreasing with no
            # recorded losses".
            cancel_ok = False
            for c_attempt in range(1, 4):
                try:
                    cancel_result = live_client.cancel_order(order_id)
                    # Polymarket returns {"canceled": [...],
                    # "not_canceled": {id: reason}} — an order that
                    # already matched lands in not_canceled; the final
                    # truth check below catches that fill.
                    if isinstance(cancel_result, dict):
                        not_cancelled = cancel_result.get("not_canceled") or cancel_result.get("notCanceled") or {}
                        cancel_ok = order_id not in not_cancelled
                        if not cancel_ok:
                            log.warning(
                                f"LIVE: cancel REFUSED for {order_id} "
                                f"(likely already matched): {cancel_result!r}"
                            )
                            break  # refusal is definitive, retrying won't change it
                    else:
                        cancel_ok = True
                    break
                except Exception as cancel_err:
                    log.warning(f"LIVE: cancel attempt {c_attempt}/3 raised for {order_id}: {cancel_err}")
                    if c_attempt < 3:
                        time.sleep(0.75)

            # FINAL TRUTH CHECK — always re-verify after the cancel
            # attempt. Uses the same two-consecutive-read confirmation, so
            # a transient value can't manufacture a position here either.
            time.sleep(0.5)
            final_filled, final_status = _confirm_fill(order_id, size, reads=3, delay=0.3)

            if final_filled > 0:
                log.warning(
                    f"LIVE: order {order_id} FILLED {final_filled} despite cancel attempt "
                    f"(cancel_ok={cancel_ok}) — tracking as a REAL position rather than "
                    f"discarding it. raw={final_status!r}"
                )
                filled_size = final_filled
            else:
                if not cancel_ok:
                    # Couldn't cancel and it isn't filled: a GTC order may
                    # still be resting and could fill later, untracked.
                    # Register it as an ORPHAN — the main loop keeps
                    # re-attempting cancellation until it is confirmed
                    # gone, and alerts loudly if it fills meanwhile.
                    with state_lock:
                        _orphan_orders.append({
                            "order_id": order_id,
                            "symbol"  : signal["symbol"],
                            "interval": signal["interval"],
                            "size"    : size,
                            "attempts": 0,
                        })
                    log.error(
                        f"LIVE: could NOT cancel unfilled order {order_id} — registered as "
                        f"ORPHAN; will keep retrying cancellation in the background."
                    )
                    alerts.bot_error(
                        f"⚠️ Could not cancel order {order_id[:12]}… on {signal['symbol'].upper()}. "
                        f"Bot will keep retrying automatically and alert if it fills."
                    )
                raise UnfilledOrderError(
                    f"Order accepted (id={order_id}) but never filled — "
                    f"requested size={size}, filled={filled_size}. "
                    f"Raw status: {order_status_raw!r}. Cancelled and treating as failed."
                )

        # Track the ACTUAL filled amount, not the requested amount — a
        # partial fill is a real position of that smaller size.
        size = filled_size

        actual_stake    = round(order_price * size, 4)
        chainlink_entry = signal.get("chainlink_price", 0)

        trade = {
            "trade_id"         : trade_id,
            "timestamp"        : datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode"             : "live",
            "symbol"           : signal["symbol"],
            "interval"         : signal["interval"],
            "direction"        : signal["direction"],
            "entry_side"       : signal["entry_side"],
            "retrace_pct"      : signal.get("retrace_pct", ""),
            "timed_out_fill"   : signal.get("timed_out_fill", ""),
            "watched_for_sec"  : signal.get("watched_for_sec", ""),
            "entry_price"      : entry_price,
            "fill_price"       : order_price,
            "stake"            : actual_stake,
            "shares"           : size,
            "slug"             : signal["slug"],
            "end_date"         : signal["end_date"],
            "chainlink_entry"  : chainlink_entry,
            "binance_open"     : signal.get("binance_open"),
            "token_id"         : token_id,
            "resolve_attempts" : 0,
            "order_id"         : order_id,
        }

        with state_lock:
            open_positions.append(trade)
            live_daily_spent  += actual_stake
            live_daily_trades += 1
            try:
                log_trade(trade)
            except Exception as log_err:
                # The position is REAL and already tracked in memory —
                # a CSV write failure must not turn a genuine trade into
                # a reported failure (that mismatch — position exists,
                # funnel says "not confirmed" — is exactly the bug this
                # fixes). Log it loudly so it's not silently lost, but
                # don't let it affect the outcome below.
                log.error(f"log_trade failed for REAL position {trade_id} (trade is still tracked in memory): {log_err}")

        # Once the position is appended above, this trade is real and
        # committed — nothing past this point may cause the function to
        # report failure. Alerting is best-effort only.
        log.info(
            f"LIVE SUBMITTED {trade_id}: {signal['symbol'].upper()} {signal['interval']} "
            f"{signal['direction']} @ {order_price:.3f} stake=${actual_stake:.2f} "
            f"order={order_id} daily=${live_daily_spent:.2f}/${config.LIVE_MAX_DAILY:.2f}"
        )

        try:
            alerts.trade_entered(
                trade_id, signal["symbol"], signal["interval"],
                signal["direction"], signal["entry_side"],
                order_price, actual_stake, "live"
            )
        except Exception as alert_err:
            log.error(f"trade_entered alert failed for REAL position {trade_id} (trade is still tracked): {alert_err}")

        return "live"

    except UnfilledOrderError as e:
        # The known, accepted "little bug": no fill, clean cancel, no
        # money moved. Log it, count it via the funnel's attempted-vs-
        # confirmed gap, but do NOT send a Telegram error — this fires
        # often on thin books and pinging the user's phone for a
        # self-healing non-event is pure noise. The dangerous variants
        # (cancel FAILED -> orphan alert; orphan FILLED -> 🚨 alert)
        # still alert from their own paths and are never silenced.
        log.warning(f"LIVE: unfilled order skipped (no trade, no alert): {e}")
        if getattr(config, "LIVE_PAPER_FALLBACK", False):
            log.info("LIVE_PAPER_FALLBACK enabled — logging as paper")
            return paper_execute(signal)
        return False

    except Exception as e:
        log.error(f"LIVE order failed: {e}")
        # Do NOT open a paper position here. Falling back to paper in live
        # mode creates positions that look like real activity, report as
        # trades, and make /positions and /performance disagree — exactly
        # the confusion of seeing P-000X entries at flat sizing while in
        # live mode with Kelly configured. A failed live order is simply
        # NO TRADE. Opt in via LIVE_PAPER_FALLBACK if you ever want the
        # old logging behaviour back.
        if getattr(config, "LIVE_PAPER_FALLBACK", False):
            log.info("LIVE_PAPER_FALLBACK enabled — logging as paper")
            fallback_ok = paper_execute(signal)
            alerts.bot_error(f"Live order failed for {signal['symbol'].upper()} {signal['interval']} (logged as paper): {e}")
            return fallback_ok
        alerts.bot_error(f"Live order failed for {signal['symbol'].upper()} {signal['interval']} — NO trade taken: {e}")
        return False

# =======================================================
# SELF-RESOLUTION
# =======================================================
def get_end_timestamp(end_date_str):
    """Convert end date string to Unix timestamp. Handles all formats."""
    try:
        if not end_date_str:
            return 0
        from datetime import datetime, timezone
        s = str(end_date_str).strip()
        if s.endswith("Z"): s = s[:-1] + "+00:00"
        if len(s) == 10:    s = s + "T23:59:59+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def get_binance_kline_resolution(symbol, interval, slug, end_date_str=None):
    """Fetch exact open and close prices directly from Binance REST API."""
    try:
        import requests
        start_ts = None
        
        if end_date_str:
            end_ts = get_end_timestamp(end_date_str)
            if end_ts:
                interval_sec = 300 if interval == "5m" else 900
                start_ts = end_ts - interval_sec
                
        if not start_ts and slug:
            parts = slug.split("-")
            for p in reversed(parts):
                if p.isdigit() and len(p) >= 10:
                    start_ts = int(p)
                    break
                    
        if not start_ts:
            return None, None
            
        sym = f"{symbol.upper()}USDT"
        params = {
            "symbol": sym,
            "interval": interval,
            "startTime": start_ts * 1000,
            "limit": 1
        }
        resp = api_session.get("https://api.binance.com/api/v3/klines", params=params, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                open_px = float(data[0][1])
                close_px = float(data[0][4])
                close_time_ms = data[0][6]
                if int(time.time() * 1000) >= close_time_ms:
                    return open_px, close_px
    except Exception as e:
        log.debug(f"Binance kline fetch failed: {e}")
    return None, None

def resolve_open_positions(current_prices=None):
    """
    Resolve positions using the same method Polymarket uses:
      PRIMARY   — Binance open-vs-close price comparison (instant, same source as Polymarket)
      FALLBACK  — Polymarket outcomePrices API (used when Binance data unavailable)
      TIMEOUT   — after RESOLVE_MAX_RETRIES × RESOLVE_INTERVAL seconds with no result

    current_prices: live Binance price snapshot passed in from feed_fetcher.
    """
    global paper_balance, last_resolve_check
    now = int(time.time())
    if now - last_resolve_check < config.RESOLVE_INTERVAL:
        return
    last_resolve_check = now
    if not open_positions:
        return

    timeout_sec = config.RESOLVE_MAX_RETRIES * config.RESOLVE_INTERVAL

    with state_lock:
        for trade in list(open_positions):
            end_ts = get_end_timestamp(trade.get("end_date", ""))
            if end_ts == 0 or now < end_ts:
                continue

            time_past    = now - end_ts
            winning_side = None
            method       = ""

            # ── PRIMARY: Polymarket API (authoritative — matches actual payout) ──
            if time_past >= config.RESOLVE_BUFFER_SEC:
                poly_result, _ = check_polymarket_resolution(trade.get("slug", ""))
                if poly_result in ("YES", "NO", "FLAT"):
                    if poly_result == "YES":
                        winning_side = "UP"
                    elif poly_result == "NO":
                        winning_side = "DOWN"
                    else:
                        winning_side = "FLAT"
                    method = "Polymarket API"

            # ── FALLBACK: Binance kline (when Polymarket genuinely hasn't
            # settled after real waiting — NOT immediately) ──
            #
            # CONFIRMED BUG, found from a real resolved trade: ETH 15m
            # window 18:45-19:00 UTC Aug 10. Polymarket's own page for
            # this exact market showed $1,870.62 -> $1,870.97 (Up). Cipher
            # resolved it via "Binance kline 1,873.0600->1,872.9600" (Down)
            # at 19:00:02 -- two seconds after close. The Binance fallback
            # above only checked `if not winning_side`, with NO buffer of
            # its own, so it fired on the very first resolve attempt,
            # before Polymarket's real settlement had even been asked for
            # (RESOLVE_BUFFER_SEC=30 hadn't elapsed), let alone had time to
            # publish. A resolution that direction-flips a real trade is
            # the most serious class of bug in this file. Binance spot
            # price is NOT guaranteed to match whatever Polymarket actually
            # settles against, especially on thin moves — it must only be
            # trusted as a last resort after real, repeated attempts to
            # get the authoritative answer, never as an instant substitute.
            binance_fallback_delay = max(
                config.RESOLVE_BUFFER_SEC,
                getattr(config, "RESOLVE_BINANCE_FALLBACK_DELAY_SEC", 120)
            )
            if not winning_side and time_past >= binance_fallback_delay:
                binance_open, binance_close = None, None
                try:
                    binance_open, binance_close = get_binance_kline_resolution(trade["symbol"], trade["interval"], trade.get("slug"), trade.get("end_date"))
                except Exception:
                    pass

                if binance_open is not None and binance_close is not None:
                    if binance_close > binance_open:
                        winning_side = "UP"
                    elif binance_close < binance_open:
                        winning_side = "DOWN"
                    else:
                        winning_side = "FLAT"

                    if winning_side:
                        diff = abs(binance_close - binance_open)
                        symbol_char = '=' if winning_side == 'FLAT' else '↑' if winning_side == 'UP' else '↓'
                        method = (
                            f"Binance kline {binance_open:,.4f}→{binance_close:,.4f} ({symbol_char}{diff:,.4f}) "
                            f"[FALLBACK after {time_past}s — Polymarket API unavailable, verify against "
                            f"polymarket.com before trusting this result]"
                        )
                        log.warning(
                            f"{trade['trade_id']}: resolving via Binance FALLBACK after "
                            f"{time_past}s — Polymarket API never returned a result. This "
                            f"source is NOT guaranteed to match Polymarket's real settlement; "
                            f"cross-check this trade manually."
                        )

            # ── TIMEOUT ──
            if not winning_side:
                if time_past >= timeout_sec:
                    trade["result"]         = "TIMEOUT"
                    trade["pnl"]            = round(-trade["stake"], 4)
                    trade["payout"]         = 0.0
                    trade["chainlink_exit"] = ""
                    trade["balance_after"]  = round(paper_balance, 4) if trade["mode"] == "paper" else ""
                    open_positions.remove(trade)
                    closed_trades.append(trade)
                    log_trade(trade)
                    log.warning(
                        f"{trade['trade_id']}: TIMEOUT — no resolution after "
                        f"{timeout_sec // 60:.0f}min"
                    )
                else:
                    log.debug(
                        f"{trade['trade_id']}: waiting resolution "
                        f"({time_past:.0f}s past close)"
                    )
                continue

            # ── SETTLE ──
            trade_side = trade["entry_side"].upper()
            won = (trade_side == winning_side)
            if won:
                payout = trade["shares"] * 1.00
                pnl    = round(payout - trade["stake"], 4)
                result = "WIN"
            else:
                payout = 0.0
                pnl    = round(-trade["stake"], 4)
                result = "LOSS"

            if trade["mode"] == "paper":
                if won:
                    paper_balance += payout
                trade["balance_after"] = round(paper_balance, 4)
            else:
                trade["balance_after"] = ""

            trade["result"]         = result
            trade["payout"]         = round(payout, 4)
            trade["pnl"]            = pnl
            trade["chainlink_exit"] = ""

            open_positions.remove(trade)
            closed_trades.append(trade)
            log_trade(trade)

            log.info(
                f"{'✅' if won else '❌'} {trade['trade_id']}: "
                f"{trade['symbol'].upper()} {trade['interval']} "
                f"{trade['direction']} → {result} ${pnl:+.4f} | {method}"
            )

            try:
                import alerts
                bal = paper_balance if trade["mode"] == "paper" else None
                alerts.trade_resolved(
                    trade["trade_id"], trade["symbol"], trade["interval"],
                    trade["direction"], result, pnl, bal, trade["mode"]
                )
            except Exception:
                pass

    try:
        if live_client:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            live_client.update_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
    except Exception:
        pass


def print_performance():
    total_open = len(open_positions)

    if not closed_trades and total_open == 0:
        return

    if not closed_trades:
        log.info(f"PERFORMANCE: {total_open} open position(s), none resolved yet")
        return

    for mode in ["paper", "live"]:
        mode_trades = [t for t in closed_trades if t["mode"] == mode]
        if not mode_trades:
            continue

        mode_open = [t for t in open_positions if t["mode"] == mode]
        wins      = sum(1 for t in mode_trades if t["result"] == "WIN")
        losses    = sum(1 for t in mode_trades if t["result"] != "WIN")
        total     = len(mode_trades)
        timeouts  = sum(1 for t in mode_trades if "TIMEOUT" in str(t.get("result", "")))

        total_pnl    = sum(t["pnl"] for t in mode_trades)
        total_staked = sum(t["stake"] for t in mode_trades)
        win_rate     = (wins / total * 100) if total > 0 else 0
        roi          = (total_pnl / total_staked * 100) if total_staked > 0 else 0

        avg_win  = sum(t["pnl"] for t in mode_trades if t["result"] == "WIN") / wins if wins else 0
        losers   = [t for t in mode_trades if t["result"] != "WIN"]
        avg_loss = sum(t["pnl"] for t in losers) / len(losers) if losers else 0
        avg_stake = total_staked / total if total > 0 else 0

        label = "PAPER" if mode == "paper" else "LIVE"
        extras = []
        if timeouts:
            extras.append(f"{timeouts} timeouts")
        extra_str = f" ({', '.join(extras)})" if extras else ""

        print("\n" + "=" * 60)
        print(f"  {label} PERFORMANCE REPORT")
        print("-" * 60)
        print(f"  Resolved trades: {total} ({len(mode_open)} still open)")
        print(f"  Wins / Losses:   {wins} / {losses}{extra_str}")
        print(f"  Win rate:        {win_rate:.1f}%")
        print("-" * 60)
        print(f"  Total P&L:       ${total_pnl:+.2f}")
        print(f"  Total staked:    ${total_staked:.2f}")
        print(f"  ROI:             {roi:+.1f}%")
        print(f"  Avg stake:       ${avg_stake:.2f}")
        print(f"  Avg win:         ${avg_win:+.2f}")
        print(f"  Avg loss:        ${avg_loss:+.2f}")
        if mode == "paper":
            print(f"  Paper balance:   ${paper_balance:.2f}")

        if config.SIZING_MODE == "kelly":
            current_bankroll = paper_balance if mode == "paper" else (config.LIVE_MAX_DAILY - live_daily_spent)
            kelly_info = get_kelly_info(current_bankroll)
            fraction_name = {0.25: "Quarter", 0.50: "Half", 1.0: "Full"}.get(
                config.KELLY_FRACTION, f"{config.KELLY_FRACTION:.0%}"
            )
            print("-" * 60)
            print(f"  Kelly mode:      {fraction_name} Kelly ({config.KELLY_FRACTION:.0%})")
            print(f"  Raw Kelly f*:    {kelly_info['raw_kelly']:.1%}")
            print(f"  Next stake:      ${kelly_info['stake']:.2f} ({kelly_info['bankroll_pct']:.1f}% of bankroll)")

        print("=" * 60 + "\n")

    if config.SIZING_MODE == "kelly":
        update_kelly_from_history()

# =======================================================
# MAIN ENTRY POINT
# =======================================================
def execute(signal):
    """Route a signal to paper, live, or both based on config.MODE.
    Returns a string identifying what ACTUALLY executed — "live", "paper",
    or "" (falsy) for total failure. This distinguishes a genuine live
    trade from a live attempt that quietly fell back to paper, which a
    plain True/False could not — that distinction is exactly what was
    missing when a live order failed and the funnel still showed
    "Confirmed" with no indication it wasn't really live."""
    if not alerts.is_trading_active():
        log.info(f"Trading paused — skipping {signal['symbol'].upper()} {signal['interval']}")
        return ""

    if config.MODE == "paper":
        return paper_execute(signal)
    elif config.MODE == "live":
        return live_execute(signal)
    elif config.MODE == "both":
        paper_ok = paper_execute(signal)
        live_ok  = live_execute(signal)
        return live_ok or paper_ok
    else:
        log.error(f"Unknown MODE: {config.MODE}")
        return False

# Initialize on import
init_trade_log()
load_open_positions()
