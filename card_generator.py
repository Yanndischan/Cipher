# card_generator.py — Cipher Bot PnL Card Generator
# High resolution (1600x900) cinematic cards with custom background support.
# Default: procedural animated background with particles + geometric shapes.
# Custom: place any image at /home/cipher/bot/card_bg.jpg to use as background.

import os
import io

_CARD_DIR = os.path.dirname(os.path.abspath(__file__))
import math
import random
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

# =======================================================
# RESOLUTION & COLORS
# =======================================================
W, H = 1600, 900

# Cipher palette
NEON_CYAN    = (0,   245, 212)
NEON_PURPLE  = (150, 80,  255)
NEON_GREEN   = (57,  255, 130)
NEON_RED     = (255, 65,  95)
NEON_GOLD    = (255, 200, 60)
DARK_BASE    = (6,   8,   18)
DARK_SURFACE = (11,  14,  30)
DARK_PANEL   = (16,  20,  42)
DARK_BORDER  = (28,  34,  65)
TEXT_BRIGHT  = (235, 240, 255)
TEXT_MID     = (140, 150, 185)
TEXT_DIM     = (60,  68,  100)

# =======================================================
# FONTS
# =======================================================
def _font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf" if bold
        else "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()

# =======================================================
# PROCEDURAL BACKGROUND
# =======================================================
def _make_default_background():
    """Generate a cinematic dark background with particles and geometry."""
    rng = random.Random(42)  # deterministic for consistency
    bg  = Image.new("RGB", (W, H), DARK_BASE)
    d   = ImageDraw.Draw(bg)

    # Deep gradient
    for y in range(H):
        t   = y / H
        r   = int(DARK_BASE[0] * (1-t) + 8  * t)
        g   = int(DARK_BASE[1] * (1-t) + 6  * t)
        b   = int(DARK_BASE[2] * (1-t) + 25 * t)
        d.line([(0, y), (W, y)], fill=(r, g, b))

    # Subtle grid
    grid = Image.new("RGBA", (W, H), (0,0,0,0))
    gd   = ImageDraw.Draw(grid)
    for x in range(0, W, 80):
        gd.line([(x,0),(x,H)], fill=(30,38,80,25))
    for y in range(0, H, 80):
        gd.line([(0,y),(W,y)], fill=(30,38,80,25))
    bg.paste(Image.alpha_composite(bg.convert("RGBA"), grid).convert("RGB"))

    # Large bokeh circles (blurred)
    bokeh = Image.new("RGBA", (W, H), (0,0,0,0))
    bd    = ImageDraw.Draw(bokeh)
    circles = [
        (200, 200, 320, NEON_PURPLE, 18),
        (W-180, 160, 280, NEON_CYAN, 14),
        (W//2+80, H-120, 360, NEON_CYAN, 12),
        (100, H-150, 240, NEON_PURPLE, 10),
        (W-300, H//2, 200, NEON_GREEN, 8),
        (W//2-200, 80, 160, NEON_GOLD, 6),
    ]
    for cx, cy, r, col, alpha in circles:
        bd.ellipse([cx-r, cy-r, cx+r, cy+r], fill=col+(alpha,))
    bokeh = bokeh.filter(ImageFilter.GaussianBlur(55))
    bg    = Image.alpha_composite(bg.convert("RGBA"), bokeh).convert("RGB")

    # Geometric lines — angular cyber feel
    ld = ImageDraw.Draw(bg)
    # Diagonal slash lines — top area
    for i in range(6):
        x1 = -200 + i * 130
        ld.line([(x1, 0), (x1+400, H//3)], fill=(40,50,100,0), width=1)
    # Bottom right geometric bracket
    bx, by = W-180, H-180
    ld.line([(bx, by),(bx+80, by)], fill=(*NEON_CYAN, 60), width=2)
    ld.line([(bx+80, by),(bx+80, by+80)], fill=(*NEON_CYAN, 60), width=2)

    # Particle dots
    for _ in range(180):
        px = rng.randint(0, W)
        py = rng.randint(0, H)
        ps = rng.randint(1, 3)
        col_choices = [NEON_CYAN, NEON_PURPLE, TEXT_DIM, NEON_GREEN]
        pc  = rng.choice(col_choices)
        alp = rng.randint(30, 100)
        dot = Image.new("RGBA", (W, H), (0,0,0,0))
        dd  = ImageDraw.Draw(dot)
        dd.ellipse([px-ps, py-ps, px+ps, py+ps], fill=pc+(alp,))
        dot = dot.filter(ImageFilter.GaussianBlur(1))
        bg  = Image.alpha_composite(bg.convert("RGBA"), dot).convert("RGB")

    # Scan lines — very subtle
    for y in range(0, H, 4):
        ld.line([(0,y),(W,y)], fill=(0,0,0), width=1)

    return bg

def _load_background(custom_path=None):
    """Load custom background or generate default."""
    paths = [
        custom_path,
        os.path.join(_CARD_DIR, "card_bg.jpg"),
        os.path.join(_CARD_DIR, "card_bg.jpeg"),
        os.path.join(_CARD_DIR, "card_bg.png"),
    ]
    for p in paths:
        if p and os.path.exists(p):
            try:
                bg = Image.open(p).convert("RGB")
                bg = bg.resize((W, H), Image.LANCZOS)
                # Darken for text readability
                bg = ImageEnhance.Brightness(bg).enhance(0.35)
                # Add dark tint overlay
                tint = Image.new("RGB", (W, H), (6, 8, 20))
                bg   = Image.blend(bg, tint, 0.45)
                print(f"  Using custom background: {p}")
                return bg
            except Exception as e:
                print(f"  Background load failed ({p}): {e}")
    return _make_default_background()

# =======================================================
# DRAWING HELPERS
# =======================================================
def _rgba(img):
    return img.convert("RGBA")

def _paste_layer(base, layer):
    return Image.alpha_composite(_rgba(base), layer).convert("RGB")

def _glow(img, text, pos, font, color, radius=12, intensity=0.6):
    """Draw text with neon glow behind it."""
    glow_layer = Image.new("RGBA", (W, H), (0,0,0,0))
    gd = ImageDraw.Draw(glow_layer)
    gd.text(pos, text, font=font, fill=color + (int(255*intensity),))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius))
    img = _paste_layer(img, glow_layer)
    draw = ImageDraw.Draw(img)
    draw.text(pos, text, font=font, fill=color)
    return img

def _text_center(draw, text, y, font, color):
    bb = draw.textbbox((0,0), text, font=font)
    x  = (W - (bb[2]-bb[0])) // 2
    draw.text((x, y), text, font=font, fill=color)
    return x

def _text_right(draw, text, y, font, color, margin=56):
    bb = draw.textbbox((0,0), text, font=font)
    x  = W - margin - (bb[2]-bb[0])
    draw.text((x, y), text, font=font, fill=color)

def _frosted_rect(img, xy, radius=18, alpha=155, border_color=None):
    """Frosted glass panel with optional glowing border."""
    x1, y1, x2, y2 = xy
    # Blur the region behind the panel
    region = img.crop((x1, y1, x2, y2)).filter(ImageFilter.GaussianBlur(8))
    panel  = Image.new("RGBA", (x2-x1, y2-y1), DARK_PANEL + (alpha,))
    merged = Image.alpha_composite(region.convert("RGBA"), panel)

    # Paste back
    img_rgba = _rgba(img)
    mask = Image.new("RGBA", (W, H), (0,0,0,0))
    md   = ImageDraw.Draw(mask)
    md.rounded_rectangle([x1,y1,x2,y2], radius=radius, fill=(255,255,255,255))
    img_rgba.paste(merged, (x1, y1), mask=mask.crop((x1,y1,x2,y2)))
    result = img_rgba.convert("RGB")

    if border_color:
        d = ImageDraw.Draw(result)
        d.rounded_rectangle([x1,y1,x2,y2], radius=radius,
                             outline=border_color, width=1)
    return result

def _stat_card(img, x, y, w, h, label, value, value_color,
               sublabel=None, subvalue=None, accent=False):
    """Render a single frosted stat card."""
    border = NEON_CYAN if accent else DARK_BORDER
    img    = _frosted_rect(img, (x, y, x+w, y+h), radius=14,
                           alpha=160, border_color=border)
    draw   = ImageDraw.Draw(img)

    # Top accent line
    if accent:
        draw.rectangle([x+1, y+1, x+w-1, y+3], fill=NEON_CYAN)

    draw.text((x+18, y+14), label, font=_font(20), fill=TEXT_MID)
    draw.text((x+18, y+40), value, font=_font(30, bold=True), fill=value_color)

    if sublabel and subvalue:
        draw.text((x+18, y+76), sublabel,  font=_font(16), fill=TEXT_DIM)
        draw.text((x+18, y+96), subvalue,  font=_font(20, bold=True), fill=TEXT_MID)

    return img

def _logo(img, logo_path, x, y, size=70):
    """Paste logo with transparency + cyan tint."""
    try:
        logo = Image.open(logo_path).convert("RGBA")
        data = logo.getdata()
        new  = []
        for px in data:
            if px[0]>200 and px[1]>200 and px[2]>200:
                new.append((0,0,0,0))
            else:
                bright  = 1 - (sum(px[:3])/(3*255))
                r = int(NEON_CYAN[0]*bright + px[0]*(1-bright))
                g = int(NEON_CYAN[1]*bright + px[1]*(1-bright))
                b = int(NEON_CYAN[2]*bright + px[2]*(1-bright))
                new.append((r, g, b, min(px[3]+80, 255)))
        logo.putdata(new)
        logo = logo.resize((size, size), Image.LANCZOS)
        base = _rgba(img)
        base.paste(logo, (x, y), logo)
        return base.convert("RGB")
    except Exception:
        # Fallback crosshair
        d  = ImageDraw.Draw(img)
        cx, cy, r = x+size//2, y+size//2, size//2-4
        d.ellipse([cx-r,cy-r,cx+r,cy+r], outline=NEON_CYAN+(200,), width=2)
        d.line([(cx-r-6,cy),(cx+r+6,cy)], fill=NEON_CYAN+(200,), width=2)
        d.line([(cx,cy-r-6),(cx,cy+r+6)], fill=NEON_CYAN+(200,), width=2)
        return img

def _horizontal_bar(draw, x, y, w, h, value, max_value, color):
    """Progress bar for win rate visualization."""
    pct  = min(value/max_value, 1.0) if max_value > 0 else 0
    draw.rounded_rectangle([x, y, x+w, y+h], radius=h//2, fill=DARK_BORDER)
    if pct > 0:
        draw.rounded_rectangle([x, y, x+int(w*pct), y+h], radius=h//2, fill=color)

# =======================================================
# MAIN CARD GENERATOR
# =======================================================
def generate_card(stats: dict, period: str = "all",
                  logo_path: str = None,
                  bg_path: str = None) -> bytes:
    """
    Generate a high-res (1600×900) cinematic PnL card.
    Returns PNG bytes ready to send to Telegram.
    """
    # ── BACKGROUND ──────────────────────────────────────
    img = _load_background(bg_path)

    # ── OVERLAY VIGNETTE ────────────────────────────────
    vig = Image.new("RGBA", (W, H), (0,0,0,0))
    vd  = ImageDraw.Draw(vig)
    for i in range(120):
        a = int(i * 1.5)
        vd.rectangle([i,i,W-i,H-i], outline=(0,0,0,a))
    img = _paste_layer(img, vig)

    # ── TOP CYAN ACCENT LINE ─────────────────────────────
    tl = Image.new("RGBA", (W, H), (0,0,0,0))
    td = ImageDraw.Draw(tl)
    td.rectangle([0,0,W,3], fill=NEON_CYAN+(220,))
    td.rectangle([0,3,W,5], fill=NEON_CYAN+(80,))
    img = _paste_layer(img, tl)

    # ── LOGO ────────────────────────────────────────────
    logo = logo_path or os.path.join(_CARD_DIR, "cipher_logo.jpeg")
    img  = _logo(img, logo, 52, 32, size=72)

    draw = ImageDraw.Draw(img)

    # ── CIPHER NAME ──────────────────────────────────────
    draw.text((W-56, 36), "CIPHER", font=_font(38, bold=True),
              fill=TEXT_BRIGHT, anchor="ra")

    period_map = {"all": "ALL TIME", "daily": "DAILY RECAP", "weekly": "WEEKLY RECAP"}
    period_str = period_map.get(period, "ALL TIME")
    draw.text((W-56, 80), period_str, font=_font(18),
              fill=NEON_CYAN, anchor="ra")

    # Timestamp
    ts = datetime.now().strftime("%d %b %Y  ·  %H:%M UTC")
    draw.text((142, 48), ts, font=_font(18), fill=TEXT_DIM)

    mode     = stats.get("mode", "paper").upper()
    mode_col = NEON_CYAN if mode == "LIVE" else NEON_PURPLE
    draw.text((142, 74), f"● {mode}", font=_font(16), fill=mode_col)

    # ── DIVIDER ──────────────────────────────────────────
    dl = Image.new("RGBA", (W,H), (0,0,0,0))
    dd = ImageDraw.Draw(dl)
    dd.line([(52, 120), (W-52, 120)], fill=NEON_CYAN+(50,), width=1)
    img = _paste_layer(img, dl)

    # ── BIG P&L ──────────────────────────────────────────
    pnl      = stats.get("total_pnl", 0)
    pnl_str  = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
    pnl_col  = NEON_GREEN if pnl >= 0 else NEON_RED
    pnl_font = _font(110, bold=True)

    draw2 = ImageDraw.Draw(img)
    bb    = draw2.textbbox((0,0), pnl_str, font=pnl_font)
    px    = (W - (bb[2]-bb[0])) // 2

    img  = _glow(img, pnl_str, (px, 138), pnl_font, pnl_col, radius=22, intensity=0.45)
    draw = ImageDraw.Draw(img)

    # ROI
    balance = stats.get("balance", 100)
    if balance > 0:
        roi     = (pnl / balance) * 100
        roi_str = f"{roi:+.1f}%  ROI"
        _text_center(draw, roi_str, 262, _font(26), TEXT_MID)

    # Win rate bar
    wr     = stats.get("win_rate", 0)
    wr_col = NEON_GREEN if wr >= 70 else (NEON_GOLD if wr >= 55 else NEON_RED)
    bar_w  = 420
    bar_x  = (W - bar_w) // 2
    _horizontal_bar(draw, bar_x, 298, bar_w, 8, wr, 100, wr_col)
    draw.text((bar_x, 312),       "0%",   font=_font(16), fill=TEXT_DIM)
    draw.text((bar_x+bar_w-24, 312), "100%", font=_font(16), fill=TEXT_DIM)

    # ── STAT CARDS GRID ──────────────────────────────────
    # Row 1 — 5 cards
    gx   = 52
    gy   = 344
    cw   = (W - 104 - 16) // 5   # card width
    ch   = 130                    # card height
    gap  = 4

    wins    = stats.get("wins", 0)
    losses  = stats.get("losses", 0)
    streak  = stats.get("best_streak", 0)
    avg_win = stats.get("avg_win", 0)
    avg_loss= stats.get("avg_loss", 0)
    sizing  = stats.get("sizing", "Flat")
    uptime  = stats.get("uptime_secs", 0)
    dwr     = stats.get("daily_wr", wr)

    # P&L per hour
    pph     = pnl / (uptime/3600) if uptime > 3600 else 0
    pph_col = NEON_GREEN if pph >= 0 else NEON_RED
    pph_str = f"{pph:+.2f}/h" if uptime > 3600 else "—"

    cards_r1 = [
        ("WIN RATE", f"{wr:.1f}%",      wr_col,       None, None,        True),
        ("DAILY WR", f"{dwr:.1f}%",     wr_col,       None, None,        False),
        ("TRADES",   f"{wins}W/{losses}L", TEXT_BRIGHT, "Total", str(wins+losses), False),
        ("BEST STREAK",f"🔥 {streak}",  NEON_CYAN,    None, None,        False),
        ("SIZING",   sizing,            NEON_PURPLE,  None, None,        False),
    ]

    for i, (lbl, val, col, sl, sv, acc) in enumerate(cards_r1):
        cx = gx + i*(cw+gap)
        img = _stat_card(img, cx, gy, cw, ch, lbl, val, col, sl, sv, acc)

    # Row 2 — 5 cards
    gy2 = gy + ch + gap + 6

    h_up  = uptime // 3600
    m_up  = (uptime % 3600) // 60

    cards_r2 = [
        ("AVG WIN",    f"+${avg_win:.2f}",  NEON_GREEN,  None, None, False),
        ("AVG LOSS",   f"${avg_loss:.2f}",  NEON_RED,    None, None, False),
        ("P&L / HOUR", pph_str,             pph_col,     None, None, False),
        ("UPTIME",     f"{h_up}h {m_up}m",  TEXT_BRIGHT, None, None, False),
        ("BALANCE",    f"${balance:.2f}",   TEXT_BRIGHT, None, None, False),
    ]

    for i, (lbl, val, col, sl, sv, acc) in enumerate(cards_r2):
        cx = gx + i*(cw+gap)
        img = _stat_card(img, cx, gy2, cw, ch, lbl, val, col, sl, sv, acc)

    # ── BOTTOM BAR ───────────────────────────────────────
    bb_layer = Image.new("RGBA", (W,H), (0,0,0,0))
    bd       = ImageDraw.Draw(bb_layer)
    bd.rectangle([0, H-50, W, H], fill=(6,8,18,220))
    bd.line([(0, H-51),(W, H-51)], fill=DARK_BORDER+(200,), width=1)
    img  = _paste_layer(img, bb_layer)
    draw = ImageDraw.Draw(img)

    draw.text((52,  H-34), f"● {mode}", font=_font(18), fill=mode_col)
    draw.text((140, H-34), "polymarket.com", font=_font(17), fill=TEXT_DIM)
    _text_right(draw, "cipher.trade", H-34, _font(17), TEXT_DIM)

    # Period badge bottom center
    period_badge = f"[ {period_str} ]"
    _text_center(draw, period_badge, H-34, _font(17), TEXT_DIM)

    # ── EXPORT ───────────────────────────────────────────
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=False)
    buf.seek(0)
    return buf.getvalue()


# =======================================================
# STATS BUILDER
# =======================================================
def _parse_ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.min

def build_stats_for_card(executor_ref, config_ref, period="all"):
    if executor_ref is None:
        return None

    import time as _time
    closed = list(executor_ref.closed_trades)
    now    = datetime.now()

    if period == "daily":
        cutoff   = now - timedelta(days=1)
        filtered = [t for t in closed if _parse_ts(t.get("timestamp","")) >= cutoff]
    elif period == "weekly":
        cutoff   = now - timedelta(days=7)
        filtered = [t for t in closed if _parse_ts(t.get("timestamp","")) >= cutoff]
    else:
        filtered = closed

    if not filtered:
        filtered = closed

    wins      = [t for t in filtered if t.get("result") == "WIN"]
    losses    = [t for t in filtered if t.get("result") != "WIN"]
    total_pnl = sum(float(t.get("pnl",0) or 0) for t in filtered)
    win_rate  = (len(wins)/len(filtered)*100) if filtered else 0.0
    avg_win   = sum(float(t.get("pnl",0) or 0) for t in wins)/len(wins) if wins else 0.0
    avg_loss  = sum(float(t.get("pnl",0) or 0) for t in losses)/len(losses) if losses else 0.0

    best_s, cur_s, cur_t = 0, 0, None
    for t in filtered:
        st = "WIN" if t.get("result") == "WIN" else "LOSS"
        if st == cur_t: cur_s += 1
        else:           cur_t, cur_s = st, 1
        if cur_t == "WIN": best_s = max(best_s, cur_s)

    today     = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_t   = [t for t in closed if _parse_ts(t.get("timestamp","")) >= today]
    today_w   = [t for t in today_t if t.get("result") == "WIN"]
    daily_wr  = (len(today_w)/len(today_t)*100) if today_t else 0.0

    sizing = (f"Flat ${config_ref.FLAT_STAKE:.2f}" if config_ref.SIZING_MODE == "flat"
              else f"Kelly {int(config_ref.KELLY_FRACTION*100)}%")

    start = getattr(executor_ref, 'start_time', None)
    uptime = int(_time.time() - start) if start else 0

    return {
        "total_pnl"    : total_pnl,
        "win_rate"     : win_rate,
        "total_trades" : len(filtered),
        "wins"         : len(wins),
        "losses"       : len(losses),
        "best_streak"  : best_s,
        "avg_win"      : avg_win,
        "avg_loss"     : avg_loss,
        "balance"      : executor_ref.paper_balance,
        "uptime_secs"  : uptime,
        "sizing"       : sizing,
        "mode"         : config_ref.MODE,
        "daily_wr"     : daily_wr,
        "daily_trades" : len(today_t),
    }
