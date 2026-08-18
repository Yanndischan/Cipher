# feed_fetcher.py
# Main loop: Binance WebSocket + signal detection + trend filter +
# position caps + duplicate prevention. Fixed version.

import asyncio
import json
import time
import requests
import concurrent.futures
import websockets
from datetime import datetime, timezone
from threading import Lock
from py_clob_client_v2.client import ClobClient

import config
import order_executor
import position_monitor
import alerts
from logger import log

# =======================================================
# CLIENTS
# =======================================================
clob_client = ClobClient(host=config.CLOB_HOST, chain_id=config.CHAIN_ID)

# Use a shared session to pool connections and strictly enforce API timeouts
api_session = requests.Session()
# Increase connection pool size to match max thread workers (100)
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
api_session.mount('http://', adapter)
api_session.mount('https://', adapter)

# =======================================================
# SHARED STATE
# =======================================================
binance_prices    = {s: None for s in config.SYMBOLS}
last_signal_time  = {s: {i: 0 for i in config.INTERVALS} for s in config.SYMBOLS}

# Track prices over time for trend filter
price_history     = {s: [] for s in config.SYMBOLS}
last_history_update = {s: 0 for s in config.SYMBOLS}
history_lock      = Lock()

# Track open market keys to prevent double-entry
active_markets    = set()
pending_markets   = set()   # market keys with an order IN FLIGHT (not yet in open_positions)
markets_lock      = Lock()

watching_signals  = {}
watching_lock     = Lock()
gamma_cache       = {}
_midpoint_cache   = {}    # {token_id: (price, timestamp)} — 2s TTL

# =======================================================
# FUNNEL (all keys pre-declared so reports are accurate)
# =======================================================
funnel = {
    "signals_fired"      : 0,
    "cooldown_blocked"   : 0,
    "no_market_found"    : 0,
    "not_accepting"      : 0,
    "missing_prices"     : 0,
    "wick_grace_period"  : 0,
    "time_too_early"     : 0,
    "time_left_floor"    : 0,
    "entry_too_high"     : 0,
    "entry_too_low"      : 0,
    "score_too_low"      : 0,
    "trend_rejected"     : 0,
    "position_cap"       : 0,
    "symbol_cap"         : 0,
    "duplicate_blocked"  : 0,
    "keep_looking_refused": 0,
    "entries_taken"      : 0,   # attempted — feed_fetcher decided to try, does NOT
                                   # mean a real trade resulted (see confirmed_entries)
    "confirmed_entries"  : 0,   # only increments when order_executor.execute()
                                   # returns True — a real paper/live trade actually opened
    "confirmed_but_paper_fallback": 0,   # subset of confirmed_entries where MODE=="live"
                                   # but the trade actually landed as paper because the
                                   # real live order failed and fell back
}

# Wire up alerts module with shared references
alerts.set_funnel_ref(funnel)
alerts.set_log_file(config.get_log_file())

last_funnel_print = 0

# =======================================================
# TREND TRACKER
# =======================================================
def update_price_history(symbol, price):
    now = time.time()
    
    # Rate-limit appends to once per second to prevent CPU exhaustion on the asyncio thread
    if now - last_history_update[symbol] < 1.0:
        return
    last_history_update[symbol] = now

    with history_lock:
        price_history[symbol].append((now, price))
        
        # Retain enough history to cover the longest candle open (15m = 900s)
        if len(price_history[symbol]) > 1000:
            max_interval = max(config.INTERVALS.values())
            retention    = max(config.TREND_WINDOW * 2, max_interval * 2)
            cutoff       = now - retention
            price_history[symbol] = [(t, p) for t, p in price_history[symbol] if t > cutoff]

def get_trend(symbol):
    """Return (direction, strength_pct). direction ∈ {UP, DOWN, FLAT, UNKNOWN}."""
    with history_lock:
        history = list(price_history[symbol])  # snapshot to release lock quickly

    if len(history) < config.TREND_MIN_SAMPLES:
        return "UNKNOWN", 0.0

    now    = time.time()
    cutoff = now - config.TREND_WINDOW
    window = [(t, p) for t, p in history if t > cutoff]

    if len(window) < 10:
        return "UNKNOWN", 0.0

    third = max(len(window) // 3, 1)
    early = [p for _, p in window[:third]]
    late  = [p for _, p in window[-third:]]

    early_avg = sum(early) / len(early)
    late_avg  = sum(late) / len(late)

    if early_avg == 0:
        return "UNKNOWN", 0.0

    change = ((late_avg - early_avg) / early_avg) * 100

    if change > config.TREND_FLAT_BAND:
        return "UP", change
    if change < -config.TREND_FLAT_BAND:
        return "DOWN", change
    return "FLAT", change

def get_price_at_timestamp(symbol, target_ts, tolerance=10):
    """Return the Binance price closest to target_ts (within tolerance seconds)."""
    with history_lock:
        history = list(price_history[symbol])
    if not history:
        return None
    closest = min(history, key=lambda x: abs(x[0] - target_ts))
    return closest[1] if abs(closest[0] - target_ts) <= tolerance else None

def trend_confirms(symbol, direction):
    """Return (confirmed, trend, strength). FLAT/UNKNOWN always confirm."""
    trend, strength = get_trend(symbol)

    if trend in ("UNKNOWN", "FLAT"):
        return True, trend, strength

    if direction == "UP" and trend == "UP":
        return True, trend, strength
    if direction == "DOWN" and trend == "DOWN":
        return True, trend, strength

    return False, trend, strength

# =======================================================
# TIME / MARKET HELPERS
# =======================================================
def get_time_remaining(end_date_str):
    """Return seconds until market closes."""
    try:
        if not end_date_str:
            return 9999
        from datetime import datetime, timezone
        s = str(end_date_str).strip()
        if s.endswith("Z"): s = s[:-1] + "+00:00"
        if len(s) == 10:    s = s + "T23:59:59+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return 9999


def safe_parse(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return []

def safe_float(value):
    try:
        if isinstance(value, list):
            value = value[0]
        return float(value)
    except (TypeError, ValueError, IndexError):
        return None

def fetch_market_by_slug(slug):
    now = time.time()
    if slug in gamma_cache:
        cached_data, cached_ts = gamma_cache[slug]
        if now - cached_ts < 60:
            return cached_data

    if len(gamma_cache) > 200:
        stale = [k for k, (d, ts) in gamma_cache.items() if now - ts >= 60]
        for k in stale:
            gamma_cache.pop(k, None)

    try:
        resp = api_session.get(
            f"{config.GAMMA_API}/markets",
            params={"slug": slug},
            timeout=3
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            result = data[0]
        elif isinstance(data, dict) and data.get("slug"):
            result = data
        else:
            result = None
            
        if result:
            gamma_cache[slug] = (result, now)
        return result
    except Exception:
        return None

def get_clob_midpoint(token_id):
    """Fetch CLOB midpoint with 2s cache to reduce REST overhead."""
    if not token_id:
        return None

    now = time.time()
    cached = _midpoint_cache.get(token_id)
    if cached and now - cached[1] < 2.0:
        return cached[0]

    try:
        resp = api_session.get(f"{config.CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=2)
        if resp.status_code == 200:
            data = resp.json()
            if data and "mid" in data:
                price = float(data["mid"])
                _midpoint_cache[token_id] = (price, now)
                return price
        return None
    except Exception:
        return None

def get_market_price(symbol, interval):
    """Fetch Polymarket prices for a symbol + interval, matching by outcome label."""
    interval_sec = config.INTERVALS.get(interval)
    if not interval_sec:
        return None

    now = int(time.time())
    snapped = (now // interval_sec) * interval_sec
    slug = f"{symbol.lower()}-updown-{interval}-{snapped}"
    raw  = fetch_market_by_slug(slug)

    if not raw or raw.get("closed", False):
        return None

    outcomes = safe_parse(raw.get("outcomes", "[]"))
    tokens   = safe_parse(raw.get("clobTokenIds", "[]"))
    prices   = safe_parse(raw.get("outcomePrices", "[]"))

    if len(outcomes) < 2 or len(tokens) < 2 or len(outcomes) != len(tokens):
        return None

    by_outcome = {}
    for i, label in enumerate(outcomes):
        if not isinstance(label, str):
            continue
        fallback_price = safe_float(prices[i]) if i < len(prices) else None
        by_outcome[label.strip().lower()] = (tokens[i], fallback_price)

    if "up" not in by_outcome or "down" not in by_outcome:
        return None

    token_up,   fallback_up   = by_outcome["up"]
    token_down, fallback_down = by_outcome["down"]

    up_price   = get_clob_midpoint(token_up)   or fallback_up
    down_price = get_clob_midpoint(token_down) or fallback_down

    return {
        "slug"            : slug,
        "question"        : raw.get("question", ""),
        "token_up"        : token_up,
        "token_down"      : token_down,
        "up_price"        : up_price,
        "down_price"      : down_price,
        "end_date"        : raw.get("endDate", ""),
        "liquidity"       : safe_float(raw.get("liquidity", 0)) or 0,
        "acceptingOrders": raw.get("acceptingOrders", False),
    }

_binance_open_cache = {}

def get_exact_binance_open(symbol, interval_sec, target_ts):
    """Fetch exact kline open price from Binance REST API, with caching."""
    cache_key = (symbol, interval_sec, target_ts)
    if cache_key in _binance_open_cache:
        return _binance_open_cache[cache_key]

    interval_str = next((k for k, v in config.INTERVALS.items() if v == interval_sec), "5m")
    sym = f"{symbol.upper()}USDT"
    try:
        resp = api_session.get(
            "https://api.binance.com/api/v3/klines",
            params={
                "symbol": sym,
                "interval": interval_str,
                "startTime": target_ts * 1000,
                "limit": 1
            },
            timeout=3
        )
        if resp.status_code == 200:
            data = resp.json()
            if data and isinstance(data, list) and len(data) > 0:
                open_px = float(data[0][1])
                _binance_open_cache[cache_key] = open_px
                if len(_binance_open_cache) > 100:
                    for k in list(_binance_open_cache.keys())[:-50]:
                        _binance_open_cache.pop(k, None)
                return open_px
    except Exception as e:
        log.debug(f"Binance kline fetch failed for {sym}: {e}")
    return None

# =======================================================
# FUNNEL REPORT
# =======================================================
def print_funnel():
    print("\n" + "=" * 60)
    print("  FUNNEL REPORT")
    print("-" * 60)
    print(f"  Signals fired:        {funnel['signals_fired']}")
    print(f"  ├─ Cooldown:          {funnel['cooldown_blocked']}")
    print(f"  ├─ No market:         {funnel['no_market_found']}")
    print(f"  ├─ Not accepting:     {funnel['not_accepting']}")
    print(f"  ├─ Missing prices:    {funnel['missing_prices']}")
    print(f"  ├─ Wick grace period: {funnel.get('wick_grace_period', 0)}")
    print(f"  ├─ Too early (>T-{config.ENTRY_T_MINUS_SEC}s):  {funnel['time_too_early']}")
    print(f"  ├─ Time left < {config.MIN_TIME_REMAINING_SEC}s:    {funnel['time_left_floor']}")
    print(f"  ├─ Entry > {config.ENTRY_ZONE_MAX}:      {funnel['entry_too_high']}")
    print(f"  ├─ Entry < {config.ENTRY_HARD_FLOOR}:      {funnel['entry_too_low']}")
    print(f"  ├─ Score < {getattr(config, 'MIN_SIGNAL_SCORE', 40)}:         {funnel.get('score_too_low', 0)}")
    print(f"  ├─ Trend rejected:    {funnel['trend_rejected']}")
    print(f"  ├─ Position cap:      {funnel['position_cap']}")
    print(f"  ├─ Symbol cap:        {funnel['symbol_cap']}")
    print(f"  ├─ Duplicate:         {funnel['duplicate_blocked']}")
    print(f"  ├─ Keep-look refused: {funnel.get('keep_looking_refused', 0)}")
    print(f"  ├─ Attempted:         {funnel['entries_taken']}  (handed to executor — NOT necessarily a real trade)")
    print(f"  ├─ CONFIRMED:         {funnel.get('confirmed_entries', 0)}  (a real paper/live trade actually opened)")
    if funnel.get("confirmed_but_paper_fallback", 0) > 0:
        print(f"  └─ ⚠️  ...of which fell back to PAPER (live failed): {funnel['confirmed_but_paper_fallback']}")
    print("=" * 60 + "\n")

# =======================================================
# SIGNAL EVALUATOR
# =======================================================

# =======================================================
# SIGNAL QUALITY SCORE
# =======================================================
def calculate_signal_score(delta_pct, entry_price, time_left, interval_sec, liquidity):
    """
    Score 0-100 measuring signal strength using Delta-to-Open strategy.
    Score >= 70: bot can enter below 0.45, stake boosted 10%.
    """
    score = 0

    # Time remaining + Delta interaction
    time_pct = time_left / interval_sec if interval_sec > 0 else 0
    time_elapsed_pct = 1.0 - time_pct

    abs_delta = abs(delta_pct)
    
    # Delta strength (0-50) — The lead over the open price
    if abs_delta >= 0.30:   score += 50
    elif abs_delta >= 0.20: score += 40
    elif abs_delta >= 0.15: score += 30
    elif abs_delta >= 0.10: score += 20
    elif abs_delta >= 0.05: score += 10

    # Time elapsed component (0-25) — A lead is safer late in the candle
    if time_elapsed_pct >= 0.90: score += 25
    elif time_elapsed_pct >= 0.75: score += 20
    elif time_elapsed_pct >= 0.50: score += 10
    elif time_elapsed_pct >= 0.25: score += 5

    # Entry component (0-20)
    if entry_price <= 0.35:   score += 20
    elif entry_price <= 0.45: score += 16
    elif entry_price <= 0.55: score += 12
    elif entry_price <= 0.65: score += 7
    elif entry_price <= 0.75: score += 3

    # Liquidity component (0-5)
    if liquidity >= 5000:   score += 5
    elif liquidity >= 2000: score += 3
    elif liquidity >= 500:  score += 1

    return min(score, 100)

QUALITY_SCORE_THRESHOLD = 70   # at or above: allow below-0.45 entry + 10% stake boost

def evaluate_signal(symbol, interval, direction, binance_price, open_price, delta_pct):
    """Evaluate a detected signal and execute if all filters pass."""
    # Paused check FIRST, before signals_fired increments, so funnel
    # accounting stays internally consistent while paused. execute() has
    # its own gates too — this just avoids doing evaluation work (and
    # keep-looking state churn) for trades that can't happen anyway.
    if not alerts.is_trading_active():
        log.debug(f"{symbol.upper()} {interval}: signal ignored — trading paused")
        return

    now = int(time.time())
    funnel["signals_fired"] += 1

    # Global position cap — checked once for the whole evaluation
    total_open = len(order_executor.open_positions)
    if total_open >= config.MAX_OPEN_POSITIONS:
        log.debug(f"{symbol.upper()}: position cap ({total_open}/{config.MAX_OPEN_POSITIONS})")
        funnel["position_cap"] += 1
        return

    # Per-symbol cap
    sym_open = len([t for t in order_executor.open_positions if t["symbol"] == symbol])
    if sym_open >= config.MAX_PER_SYMBOL:
        log.debug(f"{symbol.upper()}: symbol cap ({sym_open}/{config.MAX_PER_SYMBOL})")
        funnel["symbol_cap"] += 1
        return

    # Trend filter
    if config.TREND_FILTER_ACTIVE:
        confirmed, trend, strength = trend_confirms(symbol, direction)
        if not confirmed:
            log.debug(
                f"{symbol.upper()}: trend REJECTS {direction} "
                f"(5m trend {trend} {strength:+.3f}%)"
            )
            funnel["trend_rejected"] += 1
            return
    else:
        trend, strength = "OFF", 0

    tag = f"{symbol.upper()} {interval}"

    # Cooldown
    last = last_signal_time[symbol][interval]
    if now - last < config.SIGNAL_COOLDOWN:
        remaining = config.SIGNAL_COOLDOWN - (now - last)
        log.debug(f"{tag}: cooldown {remaining}s left")
        funnel["cooldown_blocked"] += 1
        return

    # Re-check position cap mid-evaluation (could fill between iterations)
    if len(order_executor.open_positions) >= config.MAX_OPEN_POSITIONS:
        funnel["position_cap"] += 1
        return

    # Fetch Polymarket price
    market = get_market_price(symbol, interval)
    if not market:
        log.debug(f"{tag}: no market found")
        funnel["no_market_found"] += 1
        return
    if not market["acceptingOrders"]:
        log.debug(f"{tag}: not accepting orders")
        funnel["not_accepting"] += 1
        return

    up_price, down_price = market["up_price"], market["down_price"]
    if up_price is None or down_price is None:
        log.debug(f"{tag}: missing prices")
        funnel["missing_prices"] += 1
        return

    # Time remaining
    time_left    = get_time_remaining(market["end_date"])
    interval_sec = config.INTERVALS[interval]

    # Wick Grace Period: Wait for the initial manipulation to finish
    candle_start = (now // interval_sec) * interval_sec
    actual_candle_age = now - candle_start
    if actual_candle_age < getattr(config, 'CANDLE_START_GRACE_SEC', 60):
        log.debug(f"{tag}: wick grace period ({actual_candle_age}s < {getattr(config, 'CANDLE_START_GRACE_SEC', 60)}s)")
        funnel["wick_grace_period"] += 1
        return

    if config.TIME_GUARDIAN_ACTIVE:
        if time_left > config.ENTRY_T_MINUS_SEC:
            log.debug(f"{tag}: too early in frame ({int(time_left)}s left, waiting for T-{config.ENTRY_T_MINUS_SEC})")
            funnel["time_too_early"] += 1
            return

    if time_left < config.MIN_TIME_REMAINING_SEC:
        log.debug(f"{tag}: time left {int(time_left)}s < {config.MIN_TIME_REMAINING_SEC}s floor")
        funnel["time_left_floor"] += 1
        return

    # Entry side — determined by gap direction
    if direction == "UP":
        entry_price, other_price = up_price,   down_price
        entry_side, token_to_buy = "UP",       market["token_up"]
    else:
        entry_price, other_price = down_price, up_price
        entry_side, token_to_buy = "DOWN",     market["token_down"]

    # KEEP-LOOKING retrace guard
    watch_key  = (symbol, interval)
    abs_delta  = abs(delta_pct)
    retrace_pct     = 0.0
    watched_for_sec = 0.0
    timed_out       = False

    if getattr(config, "KEEP_LOOKING_ENABLED", False):
        with watching_lock:
            w = watching_signals.get(watch_key)

            if w is None or w["direction"] != direction:
                # First sighting of this signal (or it flipped direction).
                # Only engage keep-looking if the price is actually expensive
                # right now — cheap signals (including the ENTRY_ZONE_MIN
                # high-quality carve-out) must never be touched by this and
                # should just be evaluated normally, immediately.
                if entry_price > config.ENTRY_ZONE_MAX:
                    watching_signals[watch_key] = {
                        "peak_delta": abs_delta, "first_seen": now, "direction": direction,
                    }
                    w = watching_signals[watch_key]
                else:
                    w = None  # not engaging — falls through to normal checks below

            else:
                w["peak_delta"] = max(w["peak_delta"], abs_delta)

            if w is not None:
                retrace_pct     = (w["peak_delta"] - abs_delta) / w["peak_delta"] if w["peak_delta"] > 0 else 0.0
                watched_for_sec = now - w["first_seen"]

                # getattr-guarded: same class of bug as PAPER_MAX_DAILY —
                # a hard read of a late-added config key raises
                # AttributeError on every signal if the deployed config.py
                # predates it, silently killing all trading with no logs.
                effective_timeout = min(
                    getattr(config, "KEEP_LOOKING_TIMEOUT_SEC", 60),
                    max(0, time_left - config.MIN_TIME_REMAINING_SEC),
                )

                if watched_for_sec > effective_timeout:
                    # Time's up. Per explicit config, take whatever price is
                    # available now if it's within normal entry-zone bounds —
                    # this OVERRIDES the retrace refusal below, on purpose.
                    timed_out = True
                    del watching_signals[watch_key]
                elif retrace_pct > getattr(config, "KEEP_LOOKING_MAX_RETRACE", 0.5):
                    log.info(
                        f"{tag}: thesis broke — delta retraced {retrace_pct:.0%} off peak "
                        f"({w['peak_delta']:.3f}% -> {abs_delta:.3f}%), refusing to enter on this dip"
                    )
                    funnel["keep_looking_refused"] += 1
                    del watching_signals[watch_key]
                    return  # not yet timed out, and the thesis looks dead — refuse

    # Signal quality score
    quality_score = calculate_signal_score(
        delta_pct, entry_price, time_left,
        interval_sec, market["liquidity"]
    )

    if quality_score < getattr(config, 'MIN_SIGNAL_SCORE', 40):
        log.debug(f"{tag}: score {quality_score} < min {getattr(config, 'MIN_SIGNAL_SCORE', 40)}")
        funnel["score_too_low"] += 1
        return

    high_quality = quality_score >= QUALITY_SCORE_THRESHOLD

    # Entry quality — high quality signals can enter below 0.45
    if entry_price > config.ENTRY_ZONE_MAX:
        log.debug(f"{tag}: entry {entry_price:.3f} > {config.ENTRY_ZONE_MAX}")
        funnel["entry_too_high"] += 1
        return
    if entry_price < config.ENTRY_HARD_FLOOR:
        log.debug(f"{tag}: entry {entry_price:.3f} < {config.ENTRY_HARD_FLOOR}")
        funnel["entry_too_low"] += 1
        return
    # Below normal zone — only allow on high quality signals (score >= 79)
    if entry_price < config.ENTRY_ZONE_MIN and not high_quality:
        log.debug(f"{tag}: entry {entry_price:.3f} < {config.ENTRY_ZONE_MIN} (score {quality_score} < {QUALITY_SCORE_THRESHOLD})")
        funnel["entry_too_low"] += 1
        return

    # Duplicate market check
    market_key = (symbol, interval, market["slug"])
    with markets_lock:
        if market_key in active_markets:
            log.debug(f"{tag}: already in this market ({market['slug']})")
            funnel["duplicate_blocked"] += 1
            return
        active_markets.add(market_key)
        # Mark as IN FLIGHT. A live order now takes seconds to complete
        # (posting + retries + two-consecutive-read fill confirmation) and
        # is not in open_positions during that time. The main loop's
        # cleanup prunes active_markets down to markets that have an open
        # position, so without this it would DELETE the guard for an order
        # still in flight — letting the next poll fire a second entry into
        # the identical market. That is exactly how two identical XRP 15m
        # positions (L-0026 / L-0027) were opened.
        pending_markets.add(market_key)

    with watching_lock:
        watching_signals.pop(watch_key, None)

    # Log signal
    quality = f"score={quality_score}{'🔥' if high_quality else ''}"
    profit_pct = ((1.0 - entry_price) / entry_price) * 100
    mins_left = int(time_left // 60)
    secs_left = int(time_left % 60)
    trend_label = f" | Trend: {trend} ({strength:+.3f}%)" if trend not in ("UNKNOWN", "OFF") else ""

    log.info(
        f"SIGNAL {tag} {direction}{trend_label} | "
        f"Binance ${binance_price:,.4f} vs Open ${open_price:,.4f} | "
        f"Lead {delta_pct:+.3f}% | Entry {entry_side}@{entry_price:.3f} "
        f"Other {other_price:.3f} | Liq ${market['liquidity']:,.0f} | "
        f"Time {mins_left}m{secs_left}s | {quality} | Est ROI {profit_pct:.1f}% | "
        f"Retrace {retrace_pct:.0%} | Watched {watched_for_sec:.0f}s{' | TIMEOUT-FILL' if timed_out else ''}"
    )

    # Record state before executing
    funnel["entries_taken"] += 1   # attempted — see confirmed_entries below for reality
    last_signal_time[symbol][interval] = now

    # Stake boost label for logging
    boost_label = f" 🔥 SCORE={quality_score} +10% stake" if high_quality else f" score={quality_score}"
    log.info(f"{tag}: executing{boost_label}")

    try:
        executed_as = order_executor.execute({
            "symbol"          : symbol,
            "interval"        : interval,
            "direction"       : direction,
            "entry_side"      : entry_side,
            "entry_price"     : entry_price,
            "token_to_buy"    : token_to_buy,
            "slug"            : market["slug"],
            "end_date"        : market["end_date"],
            "chainlink_price" : open_price,  # Repurposed for logging
            "quality_score"   : quality_score,
            "high_quality"    : high_quality,
            "binance_open"    : open_price,
            "retrace_pct"     : round(retrace_pct, 4),
            "watched_for_sec" : round(watched_for_sec, 1),
            "timed_out_fill"  : timed_out,
        })
    finally:
        # Always clear the in-flight marker, success or failure.
        with markets_lock:
            pending_markets.discard(market_key)

    if executed_as:
        funnel["confirmed_entries"] += 1
        # This is the exact gap that was flagged: MODE=="live" but the
        # trade actually landed as "paper" means the live order failed
        # and silently fell back — "Confirmed" alone doesn't say that.
        # Flag it as its own distinct, visible thing rather than letting
        # it blend into the generic confirmed count.
        if config.MODE == "live" and executed_as == "paper":
            funnel["confirmed_but_paper_fallback"] = funnel.get("confirmed_but_paper_fallback", 0) + 1
            log.warning(
                f"{tag}: CONFIRMED but as PAPER, not LIVE as intended — "
                f"the live order failed and fell back. Check bot.log above "
                f"for the specific failure reason (allowance, balance, "
                f"exposure cap, order rejection, etc.)."
            )
    else:
        # No trade resulted — release the duplicate guard so a later,
        # genuine signal for this same market isn't permanently blocked.
        with markets_lock:
            active_markets.discard(market_key)
        log.info(f"{tag}: attempt did not result in a real trade "
                 f"(blocked downstream by an executor-level cap — see bot.log for the specific gate)")

# =======================================================
# BINANCE WEBSOCKET
# =======================================================
async def binance_stream():
    backoff = 5
    while True:
        try:
            # open_timeout=15 gives the handshake time to complete on a slow VPS link
            async with websockets.connect(
                config.BINANCE_WS,
                ping_interval=20,
                ping_timeout=20,
                open_timeout=15,
            ) as ws:
                log.info("Binance WebSocket connected")
                backoff = 5  # reset on successful connect
                while True:
                    msg   = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    data  = json.loads(msg)
                    trade = data.get("data", {})
                    sym   = trade.get("s")
                    price = trade.get("p")
                    if sym in config.SYMBOL_MAP and price:
                        symbol = config.SYMBOL_MAP[sym]
                        px     = float(price)
                        binance_prices[symbol] = px
                        update_price_history(symbol, px)
        except asyncio.TimeoutError:
            log.warning(f"Binance WS timed out — reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)  # exponential backoff up to 60s
        except Exception as e:
            log.warning(f"Binance WS error: {e} — reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

# =======================================================
# SIGNAL DETECTOR LOOP
# =======================================================
async def signal_detector():
    global last_funnel_print
    log.info("Signal detector started")
    # Announce EVERY startup with the restored trading state. The
    # supervisor silently restarts the bot on crash; before the pause
    # state was persisted, each restart also silently resumed trading.
    # Now the state survives, and this message makes every restart —
    # and the state it came back with — visible in Telegram.
    try:
        state = "🟢 ACTIVE" if alerts.is_trading_active() else "🔴 PAUSED (restored from last /stop)"
        alerts.send(f"🤖 <b>Bot started</b> — Trading: {state} | Mode: {config.MODE.upper()}", force=True)
    except Exception as e:
        log.warning(f"Startup announcement failed: {e}")
    await asyncio.sleep(3)

    while True:
        # getattr with a safe default: this line runs on EVERY loop
        # iteration, so a missing SIGNAL_POLL_INTERVAL_SEC in config.py
        # (easy to lose — deploys were told NOT to copy our config.py)
        # would otherwise AttributeError -> detector dies -> supervisor
        # restarts -> dies again: an infinite crash loop that detects
        # almost no signals and resets the funnel every few seconds.
        await asyncio.sleep(getattr(config, "SIGNAL_POLL_INTERVAL_SEC", 2))
        
        now_ts = int(time.time())

        for symbol in config.SYMBOLS:
            binance_price = binance_prices[symbol]
            if binance_price is None:
                continue

            for interval_str, interval_sec in config.INTERVALS.items():
                candle_start_ts = (now_ts // interval_sec) * interval_sec
                open_price = get_exact_binance_open(symbol, interval_sec, candle_start_ts)
                
                if open_price is None or open_price == 0:
                    continue

                delta = binance_price - open_price
                delta_pct = (delta / open_price) * 100

                direction = None
                min_gap = config.GAP_THRESHOLD_5M if interval_str == "5m" else config.GAP_THRESHOLD_15M
                
                if delta_pct > min_gap:
                    signal_str = "🟢 BULLISH"
                    direction  = "UP"
                elif delta_pct < -min_gap:
                    signal_str = "🔴 BEARISH"
                    direction  = "DOWN"
                else:
                    signal_str = "⚪ No signal"

                trend, _ = get_trend(symbol)
                trend_arrow = {"UP": "↑", "DOWN": "↓", "FLAT": "→", "UNKNOWN": "?"}.get(trend, "?")

                if config.VERBOSE_MODE:
                    print(
                        f"[{symbol.upper():>3} {interval_str}] "
                        f"Binance: ${binance_price:>12,.4f} | "
                        f"Open: ${open_price:>12,.4f} | "
                        f"Lead: {delta_pct:>+7.3f}% | "
                        f"Trend: {trend_arrow} | "
                        f"{signal_str}"
                    )

                if direction:
                    loop = asyncio.get_event_loop()
                    future = loop.run_in_executor(
                        None,
                        evaluate_signal,
                        symbol,
                        interval_str,
                        direction,
                        binance_price,
                        open_price,
                        delta_pct
                    )
                    future.add_done_callback(
                        lambda f: log.error(f"evaluate_signal crashed: {f.exception()}") if f.exception() else None
                    )

        if config.VERBOSE_MODE:
            print("-" * 100)

        # Position monitor (disabled by default in its module)
        loop = asyncio.get_event_loop()
        mon_future = loop.run_in_executor(
            None,
            position_monitor.check_positions,
            order_executor.open_positions,
            order_executor
        )
        mon_future.add_done_callback(
            lambda f: log.error(f"position_monitor.check_positions crashed: {f.exception()}") if f.exception() else None
        )

        # Resolve any open positions — pass live Binance snapshot for immediate resolution
        _px_snapshot = dict(binance_prices)
        res_future = loop.run_in_executor(
            None,
            order_executor.resolve_open_positions,
            _px_snapshot
        )
        res_future.add_done_callback(
            lambda f: log.error(f"resolve_open_positions crashed: {f.exception()}") if f.exception() else None
        )

        # Clean up resolved markets from duplicate tracker.
        # pending_markets MUST be preserved: those orders are in flight and
        # not yet in open_positions, so pruning them here would drop the
        # duplicate guard mid-order and allow a second entry into the same
        # market (the L-0026 / L-0027 XRP duplicate).
        with markets_lock:
            open_slugs = {
                (t["symbol"], t["interval"], t["slug"])
                for t in order_executor.open_positions
            }
            active_markets.intersection_update(open_slugs | pending_markets)

        # Keep retrying cancellation of any orders whose cancel failed —
        # a resting uncancellable GTC order is silent live exposure.
        try:
            order_executor.retry_orphan_cancels()
        except Exception as e:
            log.error(f"retry_orphan_cancels crashed: {e}")

        # Periodic reports
        now_ts = int(time.time())
        if now_ts - last_funnel_print >= config.FUNNEL_PRINT_INTERVAL:
            print_funnel()
            order_executor.print_performance()
            position_monitor.print_exit_stats()
            last_funnel_print = now_ts

        # Hourly heartbeat to Telegram (fire-and-forget so it doesn't block)
        hb_future = loop.run_in_executor(None, alerts.send_heartbeat)
        hb_future.add_done_callback(
            lambda f: log.error(f"heartbeat crashed: {f.exception()}") if f.exception() else None
        )

# =======================================================
# MAIN
# =======================================================
async def main():
    # Runs BEFORE anything else — a missing config key fails loudly and
    # immediately here, instead of crashing the whole process hours into
    # a run on whatever code path happens to touch it first.
    #
    # hasattr-guarded ON PURPOSE: if this file is deployed while an older
    # config.py is still in place (a real scenario — config.py uploads
    # have hit permission errors), an unguarded call would raise
    # AttributeError on every start, and the supervisor would restart it
    # into an infinite crash loop. Missing validator = skip validation
    # with a warning, never take the bot down over it.
    if hasattr(config, "validate_config"):
        config.validate_config()
    else:
        print("  [!] config.py has no validate_config() — skipping startup validation.")
        print("      (Deploy the current config.py to enable it.)")

    print("=" * 60)
    print("  Polymarket Signal Bot — Starting")
    print(f"  Symbols:           {', '.join(s.upper() for s in config.SYMBOLS)}")
    print(f"  Intervals:         {', '.join(config.INTERVALS.keys())}")
    print(f"  Mode:              {config.MODE}")
    print(f"  Sizing:            {config.SIZING_MODE}")
    print(f"  Gap 5m / 15m:      {config.GAP_THRESHOLD_5M}% / {config.GAP_THRESHOLD_15M}%")
    print(f"  Entry zone:        {config.ENTRY_ZONE_MIN} - {config.ENTRY_ZONE_MAX}")
    print(f"  Max positions:     {config.MAX_OPEN_POSITIONS} total, {config.MAX_PER_SYMBOL} per symbol")
    print(f"  Trend filter:      {'ACTIVE' if config.TREND_FILTER_ACTIVE else 'OFF'}")
    print(f"  Time guardian:     {'ACTIVE' if getattr(config, 'TIME_GUARDIAN_ACTIVE', False) else 'OFF'}")
    print("=" * 60 + "\n")

    # Massively increase thread pool size to prevent RPC/API lag from causing thread starvation.
    # This fixes the bot slowing down and missing signals mid-run, and stops DNS lookup timeouts.
    loop = asyncio.get_event_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=100))

    alerts.start_polling()
    alerts.bot_started()

    await asyncio.gather(
        binance_stream(),
        signal_detector()
    )

if __name__ == "__main__":
    # uvloop: drop-in replacement for asyncio — 2-4x faster event loop
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        print("  [✓] uvloop active (2-4x async speedup)")
    except ImportError:
        print("  [i] uvloop not installed — using default asyncio")
        print("      Install with: pip3 install uvloop --break-system-packages")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
        alerts.bot_stopped()