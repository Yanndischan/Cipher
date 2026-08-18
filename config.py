# config.py
# Centralized configuration for the Polymarket trading bot.
# All knobs live here. Change once, propagate everywhere.

import os
from dotenv import load_dotenv

load_dotenv()

# =======================================================
# ENDPOINTS
# =======================================================
RPC_URL   = os.getenv("POLYGON_RPC_URL", "https://polygon.publicnode.com")
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID  = 137

# =======================================================
# MARKETS
# =======================================================
SYMBOLS   = ["btc", "eth", "sol", "xrp"]
INTERVALS = {"5m": 300, "15m": 900}

# =======================================================
# SIGNAL THRESHOLDS (Delta to Open)
# =======================================================
GAP_THRESHOLD_5M  = 0.15  # Distance from candle open to trigger signal
GAP_THRESHOLD_15M = 0.18  # 15m markets require even stronger signal

# =======================================================
# ENTRY FILTERS
# =======================================================
MIN_SIGNAL_SCORE = 0       # Minimum quality score (0-100) to enter a trade
ENTRY_ZONE_MIN = 0.20       # Lower floor — better odds (was 0.45)
ENTRY_ZONE_MAX = 0.75       # Avoid expensive entries (was 0.75)
ENTRY_HARD_FLOOR = 0.20     # below this, signal is likely wrong
SIGNAL_COOLDOWN  = 0      # seconds between same symbol+interval signals (was 0)

# Time remaining guardrails — T-20 mode: only enter in the final 20s of any tile frame
TIME_GUARDIAN_ACTIVE   = False  # Set to True to restrict entries to the final seconds
CANDLE_START_GRACE_SEC = 0     # Ignore the first X seconds of a candle to avoid manipulative wicks
ENTRY_T_MINUS_SEC      = 20     # only enter when this many seconds (or fewer) remain
MIN_TIME_REMAINING_PCT = 0.0    # disabled — T-20 uses absolute window, not percentage
MIN_TIME_REMAINING_SEC = 5      # absolute floor: need ≥5s for order execution

# How often the signal detector checks for a delta crossing. The underlying
# Binance price itself updates in real time via WebSocket — this interval
# is purely how often the bot LOOKS at it. Binance open-price lookups are
# cached per candle window, so lowering this is cheap: it doesn't create
# more signals, it just detects the same real crossings closer to when
# they actually happened, which matters directly for entry price on fast
# moves. Don't set this near-zero — there's no benefit below roughly
# network/event-loop granularity, and it just burns CPU for nothing.
SIGNAL_POLL_INTERVAL_SEC = 0.5   # was a hardcoded 2

# Paper mode checks the REAL order book before "filling" a trade, using
# the same read-only lookup live uses to price orders. Without this,
# paper always fills at an estimated price with zero chance of failure —
# a structural gap from live, which fails to fill constantly (thin
# books, price ceiling, etc). This makes paper's fill/no-fill decision
# mirror what live would actually have done. Read-only: no order is
# placed, no money moves, even in live mode.
PAPER_USE_REAL_BOOK = True

# =======================================================
# KEEP-LOOKING (delayed entry on the same signal)
# =======================================================
KEEP_LOOKING_ENABLED    = True    # master on/off switch — toggle via /config keeplooking
# NOTE: there used to be a separate KEEP_LOOKING_MIN_TRIGGER_PRICE here.
# Removed on purpose: engagement is now tied directly to ENTRY_ZONE_MAX
# (see feed_fetcher.py) rather than an independent number. A watch state
# only ever persists past a single poll if the signal ALSO fails the
# ENTRY_ZONE_MAX check that same poll — so a separate trigger was always
# redundant as long as it stayed <= ENTRY_ZONE_MAX, and it becomes an
# active bug (a "dead zone" of silently-rejected prices with no
# keep-looking protection) the moment ENTRY_ZONE_MAX gets tuned below it.
# Tying the two together makes that misconfiguration structurally
# impossible, at the cost of losing an independent dial — a trade worth
# making, since the independent dial was never doing real work anyway.
KEEP_LOOKING_MAX_RETRACE = 0.40   # cancel watching if delta has given back
                                    # more than 40% of its peak value
KEEP_LOOKING_TIMEOUT_SEC = 45      # at timeout, stop refusing on retrace and
                                    # take the current price if it's within the
                                    # normal entry-zone bounds


# =======================================================
# KEEP-LOOKING (delayed entry on the same signal)
# =======================================================
KEEP_LOOKING_MAX_RETRACE = 0.40   # cancel watching if delta has given back
                                    # more than 40% of its peak value
KEEP_LOOKING_TIMEOUT_SEC = 45      # stop watching this signal after this long


# =======================================================
# POSITION LIMITS
# =======================================================
MAX_OPEN_POSITIONS = 50    # total across all symbols/intervals (was 100)
MAX_PER_SYMBOL     = 1   # prevents double-entry on same symbol (was 2)

# =======================================================
# TREND FILTER
# =======================================================
TREND_WINDOW       = 900   # 15 minutes of price history (was 300)
TREND_MIN_SAMPLES  = 300   # need at least 300 ticks to trust the trend
TREND_FLAT_BAND    = 0.015 # More sensitive to slow bleeds (was 0.03)
TREND_FILTER_ACTIVE = False # Enabled to prevent buying against macro dumps

# =======================================================
# SIZING
# =======================================================
SIZING_MODE = "flat"   # "flat" or "kelly"
FLAT_STAKE  = 3.00     # $3.00 per trade for initial live testing

# Kelly
KELLY_FRACTION         = 0.25
KELLY_WIN_RATE         = 0.77
KELLY_AVG_ODDS         = 0.866
KELLY_MIN_STAKE        = 3.00
KELLY_MAX_STAKE        = 50.00
KELLY_MAX_BANKROLL_PCT = 0.25

# Concurrent exposure cap: total stake in all open positions must not exceed
# this fraction of the total bankroll (free cash + locked in open trades).
# Prevents correlated multi-asset losses from wiping gains — e.g. 0.20 means
# at most 20% of bankroll is at risk at any one moment across all open trades.
# Set to 1.0 to disable.
MAX_CONCURRENT_EXPOSURE = 0.20

# Auto-update Kelly parameters once this many trades resolve
KELLY_AUTOUPDATE_MIN_TRADES = 20

# =======================================================
# EXECUTION
# =======================================================
MODE          = "paper"   # "paper" | "live" | "both"
PAPER_BALANCE = 100.00
SLIPPAGE_BPS  = 150      # 1.5% modeled slippage for paper

# Live safety rails
LIVE_MAX_DAILY  = 15.00  # Bot stops trading if it spends/loses this much in 24h

# Paper's own daily cap, structurally identical to LIVE_MAX_DAILY/LIVE_MAX_TRADES
# below. Deliberately defaults UNBOUNDED (paper's whole value is generating
# enough volume to statistically judge the strategy — literally mirroring
# live's $15/day would strangle that). Tighten this ONLY when you
# specifically want a true apples-to-apples trade-FREQUENCY comparison
# between paper and live for a testing period — e.g. set it to match
# LIVE_MAX_DAILY exactly via /config paperdailymax 15.
PAPER_MAX_DAILY  = 1000000.00
PAPER_MAX_TRADES = 1000000
                          # NOTE: this used to be a $1,000,000 placeholder that
                          # was silently used as the live sizing bankroll too
                          # (see order_executor.py fix) — meaning it was never
                          # a real constraint. It IS now. Set this deliberately
                          # relative to your actual account size, not as an
                          # afterthought — a sane starting point is roughly
                          # 10-20% of your real balance per day while rebuilding
                          # confidence in the system.
LIVE_PAPER_FALLBACK = False   # if a live order fails, do NOT open a phantom paper position
LIVE_MAX_TRADES = 100000     # Max live trades per day

# =======================================================
# RESOLUTION
# =======================================================
RESOLVE_BUFFER_SEC    = 30    # wait this long after market end before first check

# Minimum wait before EVER trusting the Binance fallback for resolution.
# Confirmed incident: a real trade was graded WIN via Binance kline data
# that turned out to be wrong — Polymarket's own settlement for that exact
# market showed the opposite direction. Binance spot price is not
# guaranteed to match Polymarket's real settlement source, especially on
# thin moves. This must stay meaningfully longer than RESOLVE_BUFFER_SEC
# so Polymarket's authoritative API gets several real chances to answer
# first, not just one instant check.
RESOLVE_BINANCE_FALLBACK_DELAY_SEC = 120
RESOLVE_MAX_RETRIES   = 180   # 180 × 10s = 30min timeout (Polymarket can be slow)
RESOLVE_INTERVAL      = 10    # seconds between resolution poll attempts
RESOLVE_DEAD_ZONE_PCT = 0.02  # unused — kept for reference only

# =======================================================
# CSV LOG FILES
# =======================================================
TRADE_LOG_FLAT  = "trade_log_flat.csv"
TRADE_LOG_KELLY = "trade_log_kelly.csv"

def validate_config():
    """
    Startup check: verifies config.py has what the code actually needs,
    BEFORE any trading happens.

    Built after a real incident (2026-06-03): a missing KELLY_MAX_STAKE
    crashed the entire bot process hours into a run, deep inside
    print_performance() -> get_kelly_info() -> get_stake_size(). With 70+
    config keys read across the codebase, guarding each read individually
    is neither proportionate nor future-proof — one startup check catches
    the whole class of bug in a single place, with a clear message.

    Two tiers, and the distinction is important:
      MANDATORY — read directly as config.X somewhere. Missing one WILL
                  crash at runtime, so refuse to start instead.
      OPTIONAL  — only ever read via getattr(config, "X", default). Safe
                  to be absent; warn for visibility, but NEVER block
                  startup over a key the code already handles. Blocking
                  on these would take the bot down over settings that
                  work fine by design.

    Regenerate when adding config keys (grep config\\.[A-Z_]+ and
    getattr(config, "...") across order_executor/feed_fetcher/alerts).
    """
    MANDATORY = [
        'API_KEY', 'API_PASSPHRASE', 'API_SECRET', 'BINANCE_WS', 'CHAIN_ID',
        'CLOB_HOST', 'ENTRY_HARD_FLOOR', 'ENTRY_T_MINUS_SEC', 'ENTRY_ZONE_MAX',
        'ENTRY_ZONE_MIN', 'FLAT_STAKE', 'FUNNEL_PRINT_INTERVAL', 'GAMMA_API',
        'GAP_THRESHOLD_15M', 'GAP_THRESHOLD_5M', 'INTERVALS',
        'KELLY_AUTOUPDATE_MIN_TRADES', 'KELLY_AVG_ODDS', 'KELLY_FRACTION',
        'KELLY_MAX_BANKROLL_PCT', 'KELLY_MAX_STAKE', 'KELLY_MIN_STAKE',
        'KELLY_WIN_RATE', 'LIVE_MAX_DAILY', 'LIVE_MAX_TRADES',
        'MAX_CONCURRENT_EXPOSURE', 'MAX_OPEN_POSITIONS', 'MAX_PER_SYMBOL',
        'MIN_ALERT_INTERVAL', 'MIN_GAS_POL', 'MIN_TIME_REMAINING_SEC',
        'MIN_WITHDRAWAL_USDC', 'MODE', 'NOTIFY_ON_ENTRY', 'NOTIFY_ON_RESOLVE',
        'PAPER_BALANCE', 'PRIVATE_KEY', 'PROXY_ADDRESS', 'RESOLVE_BUFFER_SEC',
        'RESOLVE_INTERVAL', 'RESOLVE_MAX_RETRIES', 'RPC_URL', 'SIGNAL_COOLDOWN',
        'SIZING_MODE', 'SLIPPAGE_BPS', 'SYMBOLS', 'SYMBOL_MAP',
        'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'TELEGRAM_ENABLED',
        'TIME_GUARDIAN_ACTIVE', 'TRADE_LOG_FLAT', 'TRADE_LOG_KELLY',
        'TREND_FILTER_ACTIVE', 'TREND_FLAT_BAND', 'TREND_MIN_SAMPLES',
        'TREND_WINDOW', 'VERBOSE_MODE', 'WALLET_ADDRESS',
    ]
    OPTIONAL = [
        'CANDLE_START_GRACE_SEC', 'KEEP_LOOKING_ENABLED',
        'KEEP_LOOKING_MAX_RETRACE', 'KEEP_LOOKING_TIMEOUT_SEC',
        'LIVE_PAPER_FALLBACK', 'MIN_SIGNAL_SCORE', 'PAPER_MAX_DAILY',
        'PAPER_MAX_TRADES', 'PAPER_USE_REAL_BOOK',
        'RESOLVE_BINANCE_FALLBACK_DELAY_SEC', 'SIGNAL_POLL_INTERVAL_SEC',
    ]

    here = globals()

    missing_optional = [k for k in OPTIONAL if k not in here]
    if missing_optional:
        print(f"  [i] {len(missing_optional)} optional config key(s) absent — "
              f"using built-in defaults (NOT an error):")
        for k in missing_optional:
            print(f"      - {k}")

    missing = [k for k in MANDATORY if k not in here]
    if missing:
        print("=" * 60)
        print("  FATAL: config.py is missing REQUIRED settings")
        print("=" * 60)
        print(f"  {len(missing)} key(s) missing. The code reads these directly,")
        print("  so leaving them absent WILL crash the bot mid-run:")
        for k in missing:
            print(f"    - {k}")
        print()
        print("  Refusing to start. Add the key(s) above to config.py and")
        print("  restart. (A missing KELLY_MAX_STAKE once took the whole bot")
        print("  down hours into a run instead of failing clearly here.)")
        print("=" * 60)
        raise SystemExit(1)
    return True

# =======================================================
# KEYS (from .env)
# =======================================================
PRIVATE_KEY         = os.getenv("POLYMARKET_PRIVATE_KEY", "")
API_KEY             = os.getenv("POLYMARKET_API_KEY", "")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
API_SECRET          = os.getenv("POLYMARKET_API_SECRET", "")
API_PASSPHRASE      = os.getenv("POLYMARKET_API_PASSPHRASE", "")
# Polymarket proxy/funder address — the address your USDC sits in.
# If you log in with email/Magic: export from reveal.polymarket.com
# If you log in with MetaMask: this is your MetaMask wallet address
# Leave blank to derive automatically from POLYMARKET_PRIVATE_KEY
WALLET_ADDRESS = os.getenv("POLYMARKET_WALLET_ADDRESS", "")
PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
# =======================================================
# WALLET / ON-CHAIN
# =======================================================
# USDC.e on Polygon — this is the token Polymarket uses for deposits/withdrawals
USDC_CONTRACT_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Minimum USDC withdrawal (protects against fat-finger mistakes)
MIN_WITHDRAWAL_USDC = 1.00

# Minimum POL balance required to cover gas before a withdrawal is allowed
MIN_GAS_POL = 0.001

# =======================================================
# LOGGING
# =======================================================
LOG_FILE  = "bot.log"
LOG_LEVEL = "INFO"  # DEBUG | INFO | WARNING | ERROR
VERBOSE_MODE = False  # False for max speed (mutes aggressive console printing)

# =======================================================
# TELEGRAM
# =======================================================
TELEGRAM_ENABLED    = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
MIN_ALERT_INTERVAL  = 3
NOTIFY_ON_ENTRY     = False
NOTIFY_ON_RESOLVE   = True

FUNNEL_PRINT_INTERVAL = 60  # seconds

SYMBOL_MAP = {
    "BTCUSDT": "btc",
    "ETHUSDT": "eth",
    "SOLUSDT": "sol",
    "XRPUSDT": "xrp",
}

BINANCE_WS = (
    "wss://stream.binance.com:443/stream?streams="
    "btcusdt@trade/ethusdt@trade/solusdt@trade/xrpusdt@trade"
)


def get_log_file():
    """Return the appropriate CSV log file for the current sizing mode."""
    return TRADE_LOG_KELLY if SIZING_MODE == "kelly" else TRADE_LOG_FLAT
