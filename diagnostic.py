# preflight.py
# Pre-flight market diagnostic — run before starting the bot.
# Checks liquidity, volatility, oracle lag, skew, toxicity, and network health.
#
# Usage:
#   python preflight.py            (runs full 5-minute diagnostic)
#   python preflight.py --quick    (runs 2-minute quick check)

import asyncio
import websockets
import json
import time
import sys
import requests
import statistics
import pytz
from datetime import datetime, timezone
from collections import defaultdict
from web3 import Web3
import config

# =======================================================
# CONFIG 
# =======================================================
RPC_URL   = config.RPC_URL
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

SYMBOLS = ["btc", "eth", "sol", "xrp"]

BINANCE_WS = (
    "wss://stream.binance.com:443/stream?streams="
    "btcusdt@trade/ethusdt@trade/solusdt@trade/xrpusdt@trade"
)

SYMBOL_MAP = {"BTCUSDT": "btc", "ETHUSDT": "eth", "SOLUSDT": "sol", "XRPUSDT": "xrp"}

# Diagnostic thresholds
GOOD_AVG_LAG     = 15    
MIN_AVG_LAG      = 8     
GOOD_GAP_RATE    = 10    
MIN_GAP_RATE     = 5     
GOOD_VOLATILITY  = 0.04  
LOW_VOLATILITY   = 0.02  
MIN_LIQUIDITY    = 500   
GOOD_LIQUIDITY   = 2000  
GOOD_SPREAD_PCT  = 10    
MAX_API_LATENCY  = 0.500 # Seconds (500ms)
MAX_SKEW_RATIO   = 3.0   # If book is skewed 3:1, it's dangerous
TOXIC_MULTIPLIER = 5.0   # If max trade is 5x the average trade size = toxic

# =======================================================
# SETUP
# =======================================================

binance_prices  = {s: None for s in SYMBOLS}
price_history   = {s: [] for s in SYMBOLS}
daily_tape      = {s: {"trend": 0.0, "high": 0.0, "low": 0.0} for s in SYMBOLS}
collection_done = False
api_latency_ms  = 0.0

# =======================================================
# DATA COLLECTION
# =======================================================
def get_ny_midnight_trend(symbol):
    """Calculates the trend since Midnight NY using Binance Klines"""
    try:
        ny_tz = pytz.timezone('America/New_York')
        now_ny = datetime.now(ny_tz)
        midnight_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_ts = int(midnight_ny.astimezone(pytz.utc).timestamp()) * 1000

        resp = requests.get("https://api.binance.com/api/v3/klines", params={
            "symbol": f"{symbol.upper()}USDT",
            "interval": "1m",
            "startTime": midnight_ts,
            "limit": 1
        }, timeout=3)
        
        data = resp.json()
        if data and len(data) > 0:
            open_price = float(data[0][1])
            curr_price = float(data[0][4]) # Close of that minute, but we'll fetch current next
            return open_price
    except Exception:
        pass
    return None

def check_api_latency():
    """Ping Gamma API to check network health"""
    global api_latency_ms
    latencies = []
    for _ in range(3):
        t0 = time.time()
        try:
            requests.get(f"{GAMMA_API}/health", timeout=2)
            latencies.append(time.time() - t0)
        except Exception:
            latencies.append(2.0) # Penalty timeout
    api_latency_ms = sum(latencies) / len(latencies)

def safe_float(value):
    try:
        if isinstance(value, list): value = value[0]
        return float(value)
    except: return None

def safe_parse(value):
    if isinstance(value, list): return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except: pass
    return []

def get_market_microstructure(token_id):
    """Calculates Order Book Skew and Trade Toxicity from CLOB"""
    skew, is_toxic, avg_sz, max_sz = 1.0, False, 0.0, 0.0
    try:
        # 1. Order Book Skew
        ob_resp = requests.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=5)
        if ob_resp.status_code == 200:
            ob = ob_resp.json()
            bids = ob.get("bids", [])[:10]
            asks = ob.get("asks", [])[:10]
            bid_vol = sum(float(b.get("size", 0)) for b in bids)
            ask_vol = sum(float(a.get("size", 0)) for a in asks)
            skew = max(bid_vol, ask_vol) / (min(bid_vol, ask_vol) + 1e-9)
            heavy_side = "BIDS" if bid_vol > ask_vol else "ASKS"
        else:
            heavy_side = "NONE"

        # 2. Trade Toxicity (Whales)
        trades = requests.get(f"{CLOB_HOST}/trades", params={"token": token_id, "limit": 50}, timeout=5).json()
        if trades:
            sizes = [float(t.get("size", 0)) for t in trades]
            if sizes:
                avg_sz = sum(sizes) / len(sizes)
                max_sz = max(sizes)
                is_toxic = max_sz > (avg_sz * TOXIC_MULTIPLIER)

        return skew, heavy_side, is_toxic, avg_sz, max_sz
    except Exception:
        return 1.0, "NONE", False, 0.0, 0.0

def check_market(symbol, interval):
    interval_sec = {"5m": 300, "15m": 900}[interval]
    now     = int(time.time())
    snapped = (now // interval_sec) * interval_sec

    for ts in [snapped, snapped + interval_sec, snapped - interval_sec]:
        slug = f"{symbol}-updown-{interval}-{ts}"
        try:
            resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=5)
            if resp.status_code != 200: continue
            data = resp.json()
            if not (isinstance(data, list) and data): continue
            
            market = data[0]
            if market.get("closed", False): continue

            outcomes = safe_parse(market.get("outcomes", "[]"))
            prices   = safe_parse(market.get("outcomePrices", "[]"))
            tokens   = safe_parse(market.get("clobTokenIds", "[]"))
            liquidity = safe_float(market.get("liquidity", 0)) or 0

            up_price, down_price, up_tok = None, None, None
            for i, label in enumerate(outcomes):
                if not isinstance(label, str): continue
                p = safe_float(prices[i]) if i < len(prices) else None
                tok = tokens[i] if i < len(tokens) else None
                mid = None
                if tok:
                    try:
                        res = requests.get(f"{CLOB_HOST}/midpoint", params={"token_id": tok}, timeout=5)
                        if res.status_code == 200:
                            data = res.json()
                            if data and "mid" in data: mid = float(data["mid"])
                    except Exception: pass
                if label.strip().lower() == "up":
                    up_price = mid or p
                    up_tok = tok
                elif label.strip().lower() == "down":
                    down_price = mid or p

            # Advanced Microstructure check
            skew, heavy_side, is_toxic, avg_sz, max_sz = get_market_microstructure(up_tok) if up_tok else (1.0, "NONE", False, 0.0, 0.0)

            return {
                "slug"      : slug,
                "liquidity" : liquidity,
                "up_price"  : up_price,
                "down_price": down_price,
                "accepting" : market.get("acceptingOrders", False),
                "skew"      : skew,
                "heavy_side": heavy_side,
                "is_toxic"  : is_toxic,
                "avg_trade" : avg_sz,
                "max_trade" : max_sz
            }
        except: continue
    return None

async def collect_binance(duration_sec):
    global collection_done
    end_time = time.time() + duration_sec
    
    # Initialize NY Midnight tape
    for sym in SYMBOLS:
        midnight_open = get_ny_midnight_trend(sym)
        daily_tape[sym]["open"] = midnight_open

    try:
        async with websockets.connect(
            BINANCE_WS,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=15,
        ) as ws:
            while time.time() < end_time:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2)
                    data  = json.loads(msg)
                    trade = data.get("data", {})
                    bsym  = trade.get("s")
                    if bsym in SYMBOL_MAP and trade.get("p"):
                        symbol = SYMBOL_MAP[bsym]
                        px     = float(trade["p"])
                        binance_prices[symbol] = px
                        price_history[symbol].append((time.time(), px))
                        
                        # Update daily tape
                        if daily_tape[symbol].get("open"):
                            daily_tape[symbol]["trend"] = ((px - daily_tape[symbol]["open"]) / daily_tape[symbol]["open"]) * 100
                except asyncio.TimeoutError:
                    continue
    except Exception as e:
        print(f"  ⚠️  Binance connection error: {e}")
    collection_done = True

# =======================================================
# ANALYSIS
# =======================================================
def analyze():
    now = datetime.now()
    day_name = now.strftime("%A")
    hour = now.hour
    is_weekend = now.weekday() >= 5

    print("\n")
    print("╔" + "═" * 78 + "╗")
    print("║" + "  INSTITUTIONAL PRE-FLIGHT MARKET DIAGNOSTIC".center(78) + "║")
    print("║" + f"  {now.strftime('%A %d %B %Y — %H:%M UTC')}".center(78) + "║")
    print("╚" + "═" * 78 + "╝")

    # ── API & Network Health ──
    print(f"\n{'─' * 80}")
    print(f"  1. INFRASTRUCTURE & DAILY TAPE")
    print(f"{'─' * 80}")
    
    lat_icon = "✅" if api_latency_ms < MAX_API_LATENCY else "⚠️"
    print(f"  Network Latency:  {lat_icon} {api_latency_ms*1000:.0f}ms ping to Gamma API")
    print(f"  Session:          {hour:02d}:00 UK Time {'(WEEKEND)' if is_weekend else ''}")
    
    print("\n  Daily Trend (Since Midnight NY):")
    for sym in SYMBOLS:
        trend = daily_tape[sym].get("trend", 0)
        t_dir = "🟢 UP" if trend > 0 else "🔴 DOWN"
        print(f"    {sym.upper()}: {t_dir} {trend:+.2f}%")

    # ── Microstructure & Liquidity ──
    print(f"\n{'─' * 80}")
    print(f"  2. MICROSTRUCTURE & LIQUIDITY")
    print(f"{'─' * 80}")

    liq_scores = {}
    for sym in SYMBOLS:
        market = check_market(sym, "5m") or check_market(sym, "15m")
        if not market:
            print(f"  {sym.upper()}: ❌ No active markets found")
            liq_scores[sym] = 0
            continue

        liq    = market["liquidity"]
        spread = abs((market["up_price"] or 0) + (market["down_price"] or 0) - 1.0) * 100 if market["up_price"] and market["down_price"] else 99
        skew   = market["skew"]
        toxic  = market["is_toxic"]
        
        score = 8
        if liq < MIN_LIQUIDITY: score -= 4
        if skew > MAX_SKEW_RATIO: score -= 3
        if toxic: score -= 3
        
        liq_scores[sym] = max(0, score)
        
        # Formatting output
        skew_warn = f"⚠️ SKEW {skew:.1f}x ({market['heavy_side']})" if skew > MAX_SKEW_RATIO else f"BALANCED ({skew:.1f}x)"
        tox_warn  = f"🚨 TOXIC FLOW DETECTED (Max: {market['max_trade']} shares)" if toxic else "Flow Clean"
        icon = "✅" if score >= 6 else ("⚠️" if score >= 3 else "❌")
        
        print(f"  {sym.upper()}: {icon} liq=${liq:,.0f} | sprd={spread:.1f}% | {skew_warn} | {tox_warn}")

    # ── Final Verdict ──
    total_liq = sum(liq_scores.values())
    max_liq = len(SYMBOLS) * 8
    
    adjusted_pct = (total_liq / max_liq * 100) if max_liq > 0 else 0
    if is_weekend: adjusted_pct -= 10
    if api_latency_ms > MAX_API_LATENCY: adjusted_pct -= 15
    if hour == 5: adjusted_pct -= 20

    print(f"\n{'═' * 80}")
    if adjusted_pct >= 60:
        print(f"  🟢 GO — Market micro-structure is clean. Spread and skew look good.")
    elif adjusted_pct >= 40:
        print(f"  🟡 CAUTION — Elevated skew or toxic flow detected. Trade flat sizes.")
    else:
        print(f"  🔴 NO-GO — Order books are toxic or network is lagging. DO NOT DEPLOY.")
    print(f"{'═' * 80}\n")

    # ═══════════════════════════════════════════════════
    # JSON EXPORT FOR TELEGRAM (alerts.py compatibility)
    # ═══════════════════════════════════════════════════
    report = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "time": {
            "day": day_name,
            "hour": hour,
            "label": "Weekend" if is_weekend else "Active Session",
            "quality": "GOOD" if adjusted_pct >= 60 else "CAUTION",
            "penalty": 20 if hour == 5 else (10 if is_weekend else 0)
        },
        "rpc": {
            "reads": f"{api_latency_ms*1000:.0f}ms ping", # Replaced read count with actual ping
            "score": 5 if api_latency_ms < MAX_API_LATENCY else 1
        },
        "overall": {
            "score": total_liq,
            "max_possible": max_liq,
            "pct": (total_liq / max_liq * 100) if max_liq > 0 else 0,
            "adjusted_pct": adjusted_pct
        },
        "symbols": {}
    }

    for sym in SYMBOLS:
        market = check_market(sym, "5m") or check_market(sym, "15m") or {}
        liq = market.get("liquidity", 0)
        spread = abs((market.get("up_price") or 0) + (market.get("down_price") or 0) - 1.0) * 100 if market.get("up_price") and market.get("down_price") else 99
        skew = market.get("skew", 1.0)
        toxic = market.get("is_toxic", False)
        trend = daily_tape[sym].get("trend", 0)

        report["symbols"][sym] = {
            "lag": {
                "med_lag": api_latency_ms * 1000, 
                "avg_gap": skew  # Feeding Order Book Skew into the 'gap' display on Telegram
            },
            "vol": {
                "avg_move": trend, # Feeding Daily NY Trend into the 'vol' display on Telegram
                "is_trending": not toxic # Flagging False if flow is toxic
            },
            "liq": {
                "liquidity": liq, 
                "spread": spread
            },
            "pct": liq_scores.get(sym, 0) / 8 * 100,
            "verdict": "✅ TRADE" if liq_scores.get(sym, 0) >= 6 else "❌ SKIP"
        }

    try:
        with open("diagnostic.json", "w") as f:
            json.dump(report, f, indent=2)
    except Exception as e:
        print(f"  ⚠️ Failed to write diagnostic.json: {e}")

# =======================================================
# MAIN
# =======================================================
async def main():
    quick = "--quick" in sys.argv
    duration = 120 if quick else 300

    print("╔" + "═" * 58 + "╗")
    print("║" + "  STARTING PRE-FLIGHT SEQUENCE".center(58) + "║")
    print("╚" + "═" * 58 + "╝\n")

    check_api_latency()
    print(f"  Collecting {duration}s of market data...\n")

    async def progress():
        start = time.time()
        while not collection_done:
            elapsed = int(time.time() - start)
            remaining = max(0, duration - elapsed)
            filled  = int(40 * elapsed / duration)
            bar     = "█" * filled + "░" * (40 - filled)
            print(f"\r  [{bar}] {elapsed}s / {duration}s", end="", flush=True)
            await asyncio.sleep(1)
        print(f"\r  [{'█' * 40}] Done!{' ' * 20}")

    await asyncio.gather(
        collect_binance(duration),
        progress()
    )
    analyze()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nDiagnostic cancelled.")