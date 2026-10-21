"""
KPOJIME BOT - one Render service.

  Part 1  shared Twelve Data helper (call spacing + daily counter)
  Part 2  Kpojime bot: trend signal (H1 BOS) + chop signal (M15 range / M5 sweep)
          + tight-market signals (M30 squeeze box, M15 break, M5 trigger):
            TIGHT BREAKOUT  = break holds, M5 retest rejects the edge
            TIGHT FAKE-OUT  = break fails, price closes back inside the box
          + trade tracker (TP / SL alerts)
          + early warning alerts (WATCH / READY) and VALID UNTIL messages
          + silent test signals (TEST H1-RETEST, TEST LTF-RETEST): log only
          + OPPOSITE-DIRECTION BLOCK: no live signal against an open live trade
  Part 3  launcher: web server, self-ping, the bot, and an independent
          heartbeat that keeps running while the bot sleeps or the API is down.
          If the bot crashes it is restarted automatically.

Start command stays: python sweep_bot.py
"""

import os
import sys
import csv
import time
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# ======================================================================
# PART 1 - SHARED TWELVE DATA HELPER
# Both bots use the same key, so every call goes through td_get():
#   * calls are spaced MIN_GAP seconds apart (free plan: 8 requests/minute)
#   * calls are counted per UTC day (free plan: 800 credits/day)
# ======================================================================
MIN_GAP = 8.0  # seconds between calls (max 7.5 calls/minute)

_lock = threading.Lock()
_last_call = 0.0
_day = None
_count = 0


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def td_get(url, params=None, timeout=15):
    """requests.get() with spacing and a per-day call counter."""
    global _last_call, _day, _count
    with _lock:
        wait = MIN_GAP - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            return requests.get(url, params=params, timeout=timeout)
        finally:
            _last_call = time.time()
            today = _today()
            if today != _day:
                _day, _count = today, 0
            _count += 1


def calls_today():
    """Number of Twelve Data calls made today (UTC) by both bots."""
    return _count if _day == _today() else 0


# ======================================================================
# PART 2 - KPOJIME BOS BOT
# ======================================================================

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

# Shown in the heartbeat
status = {"last_scan": None, "api": "not called yet"}

# ── ENV VARS ──────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")
TWELVEDATA_KEY     = os.environ.get("TWELVEDATA_KEY") or os.environ.get("TWELVEDATA_API_KEY")
SCAN_INTERVAL      = int(os.environ.get("SCAN_INTERVAL", "300"))
PAIRS              = ["XAU/USD"]
HEARTBEAT_INTERVAL = 7200

# ── DAILY SLEEP WINDOW (UTC) ──────────────────────────────
# Bot sleeps from SLEEP_START_UTC until WAKE_UTC (saves Twelve Data credits
# and avoids the US session). 13:00 UTC = 2pm WAT, 23:00 UTC = midnight WAT.
SLEEP_START_UTC = int(os.environ.get("SLEEP_START_UTC", "13"))
WAKE_UTC        = int(os.environ.get("WAKE_UTC", "23"))

# ── CHOP SIGNAL SETTINGS ──────────────────────────────────
CHOP_ENABLED    = os.environ.get("CHOP_ENABLED", "1") == "1"
CHOP_LOOKBACK   = 20      # M15 candles used to build the range
CHOP_SL_BUFFER  = float(os.environ.get("CHOP_SL_BUFFER", "5.0"))
CHOP_MIN_RANGE  = float(os.environ.get("CHOP_MIN_RANGE", "20.0"))
CHOP_MAX_RANGE  = float(os.environ.get("CHOP_MAX_RANGE", "120.0"))
CHOP_MIN_RR     = float(os.environ.get("CHOP_MIN_RR", "1.0"))
# Filters that stop the bot fading a trend/breakout (fix for 3 straight stop-outs)
CHOP_MAX_H1_MACD = float(os.environ.get("CHOP_MAX_H1_MACD", "2.0"))  # |H1 MACD| above this = trending, no chop trade
CHOP_MAX_DRIFT   = float(os.environ.get("CHOP_MAX_DRIFT", "0.25"))   # range midpoint drift, as share of range width
CHOP_MAX_SWEEP   = float(os.environ.get("CHOP_MAX_SWEEP", "0.25"))   # sweep deeper than this share of width = breakout
CHOP_MIN_TOUCHES = int(os.environ.get("CHOP_MIN_TOUCHES", "2"))      # candles that must have tested each edge
CHOP_EDGE_ZONE   = 0.2                                               # edge zone = 20% of range width
CHOP_MAX_LOSSES  = int(os.environ.get("CHOP_MAX_LOSSES", "2"))       # consecutive chop SLs before pausing
CHOP_PAUSE_HOURS = float(os.environ.get("CHOP_PAUSE_HOURS", "3"))

# ── TIGHT-MARKET SIGNAL SETTINGS ──────────────────────────
# Separate from the trend and chop signals. Turn the whole thing off with
# TIGHT_ENABLED=0, or just one of the two with TIGHT_BREAKOUT=0 / TIGHT_FAKEOUT=0.
TIGHT_ENABLED    = os.environ.get("TIGHT_ENABLED", "1") == "1"
TIGHT_BREAKOUT   = os.environ.get("TIGHT_BREAKOUT", "1") == "1"
TIGHT_FAKEOUT    = os.environ.get("TIGHT_FAKEOUT", "1") == "1"
TIGHT_BARS       = int(os.environ.get("TIGHT_BARS", "5"))          # M30 bars in the squeeze (5 = 2.5h); box = last 2x this many M15 candles
TIGHT_WINDOW     = int(os.environ.get("TIGHT_WINDOW", "4"))        # newest M15 candles where a break may happen (4 = 1h)
TIGHT_SQUEEZE    = float(os.environ.get("TIGHT_SQUEEZE", "0.7"))   # avg M30 range must be <= this x the previous stretch
TIGHT_MIN_BOX    = float(os.environ.get("TIGHT_MIN_BOX", "8"))     # box height limits (gold points)
TIGHT_MAX_BOX    = float(os.environ.get("TIGHT_MAX_BOX", "35"))
TIGHT_RETEST_TOL = float(os.environ.get("TIGHT_RETEST_TOL", "3"))  # M5 must come back within this many points of the broken edge
TIGHT_SL_BUFFER  = float(os.environ.get("TIGHT_SL_BUFFER", "3"))
TIGHT_MAX_RISK   = float(os.environ.get("TIGHT_MAX_RISK", "25"))   # skip if stop is wider than this (points)
TIGHT_MIN_RR     = float(os.environ.get("TIGHT_MIN_RR", "1.0"))    # fake-out only: minimum R:R to TP2
TIGHT_COOLDOWN   = float(os.environ.get("TIGHT_COOLDOWN", "7200")) # seconds; roughly one signal per box
TIGHT_H1_ALIGN   = os.environ.get("TIGHT_H1_ALIGN", "0") == "1"    # 1 = breakout must agree with H1 MACD direction

# ── REAL SUPPORT / RESISTANCE SETTINGS ────────────────────
SR_WARN_DIST = float(os.environ.get("SR_WARN_DIST", "20"))   # warn if a level is closer than this (points)
SR_CLUSTER   = float(os.environ.get("SR_CLUSTER", "5"))      # swing points within this many points = one level
SR_ROUND_STEP = float(os.environ.get("SR_ROUND_STEP", "50"))  # round-number levels every N points
SR_ROUND_WARN = float(os.environ.get("SR_ROUND_WARN", "10"))  # warn on a round number only if this close
SR_STRENGTH  = 2                                             # candles each side that must be lower/higher

# ── TRADE TRACKER SETTINGS ────────────────────────────────
# While a signal is open the bot polls M1 candles (1 credit per poll, per pair)
# and alerts on TP1 / TP2 / TP3 / SL. Polling stops when the trade closes.
TRACK_INTERVAL  = int(os.environ.get("TRACK_INTERVAL", "120"))    # seconds between polls
TRACK_MAX_HOURS = float(os.environ.get("TRACK_MAX_HOURS", "4"))   # stop tracking after this
TRACK_TOLERANCE = float(os.environ.get("TRACK_TOLERANCE", "1.5"))  # TP counts as hit within this many points
                                                                   # (broker and Twelve Data prices differ slightly)
BE_AFTER_TP1    = os.environ.get("BE_AFTER_TP1", "1") == "1"      # treat entry as stop after TP1

# ── TREND STACKING LIMIT ──────────────────────────────────
# Max trend signals open at once in the same direction (applies to TREND only,
# never to TIGHT or CHOP). A trade stays "open" until its final TP, SL or
# breakeven alert, even after TP1. Set TREND_MAX_OPEN=0 on Render to turn it off.
TREND_MAX_OPEN  = int(os.environ.get("TREND_MAX_OPEN", "1"))

# ── OPPOSITE-DIRECTION BLOCK ──────────────────────────────
# While a LIVE trade (trend, chop, tight) is open on a pair, no new live signal
# in the OPPOSITE direction is sent. Same-direction signals still go out.
# The blocked signal is not sent to Telegram; it is tracked silently and shows in
# the trade log as "<KIND> (BLOCKED)" so you can check later whether the block
# helped. Silent TEST signals are not affected. A trade counts as open until its
# final TP, SL, breakeven or time stop. Set OPPOSITE_BLOCK=0 on Render to turn off.
OPPOSITE_BLOCK  = os.environ.get("OPPOSITE_BLOCK", "1") == "1"

# ── TRADE LOG ─────────────────────────────────────────────
# Every closed trade is written to a CSV and summarised on Telegram when the bot
# goes to sleep (13:00 UTC). Trend signals blocked by TREND_MAX_OPEN are tracked
# silently ("TREND (SKIPPED)") so you can see whether the rule blocked winners.
# NOTE: Render's free plan wipes files on every deploy/restart, so the daily CSV
# sent to Telegram is your permanent copy.
TRADE_LOG_PATH     = os.environ.get("TRADE_LOG_PATH", "/tmp/trade_log.csv")
TRADE_LOG_SEND_CSV = os.environ.get("TRADE_LOG_SEND_CSV", "1") == "1"

# ── PIPS ──────────────────────────────────────────────────
# Price move that counts as 1 pip. Gold convention: 1 pip = 0.10 ($1 move = 10 pips).
# If your broker counts 1 pip = 0.01, set PIP_SIZE=0.01 on Render.
PIP_SIZE        = float(os.environ.get("PIP_SIZE", "0.1"))

def pips(a, b):
    """Distance between two prices in pips."""
    return int(round(abs(a - b) / PIP_SIZE))

# ── TELEGRAM ──────────────────────────────────────────────
def send(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ── WEEKEND CHECK ─────────────────────────────────────────
def is_weekend():
    now = _utcnow()
    # Saturday = 5, Sunday = 6
    if now.weekday() == 5:
        return True
    # Friday after 21:00 UTC (market closes)
    if now.weekday() == 4 and now.hour >= 21:
        return True
    # Sunday before 22:00 UTC (market opens)
    if now.weekday() == 6 and now.hour < 22:
        return True
    return False

# ── DAILY SLEEP CHECK ─────────────────────────────────────
def in_daily_sleep(now=None):
    now = now or _utcnow()
    h = now.hour
    if SLEEP_START_UTC < WAKE_UTC:
        return SLEEP_START_UTC <= h < WAKE_UTC
    # window wraps past midnight
    return h >= SLEEP_START_UTC or h < WAKE_UTC

# ── FETCH CANDLES ─────────────────────────────────────────
def fetch(symbol, interval, count=30):
    try:
        url = (
            f"https://api.twelvedata.com/time_series"
            f"?symbol={symbol}&interval={interval}&outputsize={count}"
            f"&apikey={TWELVEDATA_KEY}&format=JSON"
        )
        r = td_get(url, timeout=15)
        d = r.json()
        if d.get("status") == "error" or "values" not in d:
            print(f"Fetch error {symbol} {interval}: {d.get('message','no data')}")
            status["api"] = f"ERROR at {_utcnow():%H:%M} UTC: {str(d.get('message','no data'))[:80]}"
            return None
        status["api"] = f"OK at {_utcnow():%H:%M} UTC"
        return [
            {"t": v.get("datetime", ""),
             "o": float(v["open"]), "h": float(v["high"]),
             "l": float(v["low"]),  "c": float(v["close"])}
            for v in d["values"]
        ]
    except Exception as e:
        print(f"Fetch exception {symbol} {interval}: {e}")
        status["api"] = f"ERROR at {_utcnow():%H:%M} UTC: {str(e)[:80]}"
        return None

# ── EMA ───────────────────────────────────────────────────
def bos_ema(data, period):
    k = 2 / (period + 1)
    val = sum(data[-period:]) / period
    for p in reversed(data[:-period]):
        val = p * k + val * (1 - k)
    return val

# ── BIAS ──────────────────────────────────────────────────
def bias(candles):
    if not candles or len(candles) < 26:
        return "NEUTRAL"
    closes = [c["c"] for c in candles]
    macd = bos_ema(closes, 12) - bos_ema(closes, 26)
    if macd > 0:
        return "BULLISH"
    elif macd < 0:
        return "BEARISH"
    return "NEUTRAL"

# ── MACD VALUE ────────────────────────────────────────────
def macd_value(candles):
    if not candles or len(candles) < 26:
        return 0
    closes = [c["c"] for c in candles]
    return bos_ema(closes, 12) - bos_ema(closes, 26)

# ── MARKET CONDITION ──────────────────────────────────────
def market_condition(h4_candles, h1_candles):
    if not h4_candles or not h1_candles:
        return "UNKNOWN", 0, 0, 0
    h4_macd = macd_value(h4_candles)
    h1_macd = macd_value(h1_candles)
    recent = h1_candles[:20]
    range_high = max(c["h"] for c in recent)
    range_low  = min(c["l"] for c in recent)
    if h4_macd < -15 and h1_macd < -5:
        condition = "TRENDING BEARISH"
    elif h4_macd > 15 and h1_macd > 5:
        condition = "TRENDING BULLISH"
    else:
        condition = "CHOPPY"
    return condition, h4_macd, range_high, range_low

# ── BOS ───────────────────────────────────────────────────
def bos(candles, direction):
    if not candles or len(candles) < 7:
        return False, None, None
    latest    = candles[0]
    structure = candles[2:7]
    if direction == "BEARISH":
        structure_low = min(c["l"] for c in structure)
        if latest["c"] < structure_low:
            return True, latest["l"], structure_low
    if direction == "BULLISH":
        structure_high = max(c["h"] for c in structure)
        if latest["c"] > structure_high:
            return True, latest["h"], structure_high
    return False, None, None

# ── LIQUIDITY GRAB ────────────────────────────────────────
def liquidity_grab(candles, direction):
    if not candles or len(candles) < 5:
        return False
    recent = candles[1:5]
    latest = candles[0]
    if direction == "BEARISH":
        recent_high = max(c["h"] for c in recent)
        if latest["h"] > recent_high and latest["c"] < recent_high:
            return True
    if direction == "BULLISH":
        recent_low = min(c["l"] for c in recent)
        if latest["l"] < recent_low and latest["c"] > recent_low:
            return True
    return False

# ── M5 TRIGGER ────────────────────────────────────────────
def m5_trigger(candles, direction):
    if not candles or len(candles) < 7:
        return False
    latest    = candles[0]
    structure = candles[2:7]
    if direction == "BEARISH":
        return latest["c"] < min(c["l"] for c in structure)
    if direction == "BULLISH":
        return latest["c"] > max(c["h"] for c in structure)
    return False

# ── MACD ALIGNED ─────────────────────────────────────────
def macd_aligned(candles, direction):
    if not candles or len(candles) < 26:
        return True
    closes = [c["c"] for c in candles]
    macd = bos_ema(closes, 12) - bos_ema(closes, 26)
    if direction == "BEARISH":
        return macd < 0
    if direction == "BULLISH":
        return macd > 0
    return True

# ── LEVELS ────────────────────────────────────────────────
def levels(entry, direction, bos_wick):
    buffer = 10.0
    if direction == "BEARISH":
        sl   = bos_wick + buffer
        risk = sl - entry
        return sl, entry - risk, entry - risk*2, entry - risk*3
    else:
        sl   = bos_wick - buffer
        risk = entry - sl
        return sl, entry + risk, entry + risk*2, entry + risk*3

# ── REAL S/R (confirmed swing points on H1 + H4) ──────────
def _pivots(candles, strength=SR_STRENGTH):
    """Swing highs/lows: a candle whose high (low) beats `strength` candles on
    each side. Candles are newest-first, so the newest `strength` candles can
    never be pivots, meaning the BOS/trigger candle itself is never a level."""
    out = []
    n = len(candles)
    for i in range(strength, n - strength):
        window = [j for j in range(i - strength, i + strength + 1) if j != i]
        if all(candles[i]["h"] > candles[j]["h"] for j in window):
            out.append(candles[i]["h"])
        if all(candles[i]["l"] < candles[j]["l"] for j in window):
            out.append(candles[i]["l"])
    return out

def sr_levels(h1, h4):
    """Returns clustered levels: [{"price", "touches", "tf"}], sorted by price."""
    pts = []
    for tf, candles in (("H1", h1), ("H4", h4)):
        if candles and len(candles) >= 2 * SR_STRENGTH + 1:
            pts += [(price, tf) for price in _pivots(candles)]
    pts.sort()
    levels, group = [], []
    for price, tf in pts:
        if group and price - group[-1][0] > SR_CLUSTER:
            levels.append(group); group = []
        group.append((price, tf))
    if group:
        levels.append(group)
    return [{"price": sum(p for p, _ in g) / len(g),
             "touches": len(g),
             "tf": "H4" if any(tf == "H4" for _, tf in g) else "H1"} for g in levels]

def sr_summary(h1, h4, direction, entry):
    """Text lines for the signal: nearest resistance above and support below
    the entry (swing levels + round numbers). Informational only, it never
    blocks a signal."""
    levels = sr_levels(h1, h4)
    if SR_ROUND_STEP > 0:
        lower = (entry // SR_ROUND_STEP) * SR_ROUND_STEP
        for price in (lower, lower + SR_ROUND_STEP):
            if abs(price - entry) > 0.01:
                levels.append({"price": price, "touches": 0, "tf": "ROUND"})

    above = [l for l in levels if l["price"] > entry]
    below = [l for l in levels if l["price"] < entry]
    res = min(above, key=lambda l: l["price"]) if above else None
    sup = max(below, key=lambda l: l["price"]) if below else None

    def fmt(l):
        dist = f"{l['price'] - entry:+.1f} pts"
        if l["tf"] == "ROUND":
            return f"{l['price']:.2f} ({dist}, round number)"
        return f"{l['price']:.2f} ({dist}, {l['tf']}, x{l['touches']})"

    def limit(l):
        return SR_ROUND_WARN if l["tf"] == "ROUND" else SR_WARN_DIST

    lines = ""
    lines += f"Resistance   : {fmt(res)}\n" if res else "Resistance   : none found\n"
    lines += f"Support      : {fmt(sup)}\n" if sup else "Support      : none found\n"
    if direction == "BULLISH" and res and res["price"] - entry < limit(res):
        lines += f"⚠️ Resistance {res['price'] - entry:.1f} pts ahead of entry\n"
    if direction == "BEARISH" and sup and entry - sup["price"] < limit(sup):
        lines += f"⚠️ Support {entry - sup['price']:.1f} pts ahead of entry\n"
    return lines

def sr_nearest(h1, h4, entry):
    """Distance in points to the nearest resistance above and support below the
    entry (same levels as sr_summary). Used only for the trade log."""
    lv = sr_levels(h1, h4)
    if SR_ROUND_STEP > 0:
        lower = (entry // SR_ROUND_STEP) * SR_ROUND_STEP
        for price in (lower, lower + SR_ROUND_STEP):
            if abs(price - entry) > 0.01:
                lv.append({"price": price, "touches": 0, "tf": "ROUND"})
    above = [l["price"] for l in lv if l["price"] > entry]
    below = [l["price"] for l in lv if l["price"] < entry]
    res = round(min(above) - entry, 1) if above else ""
    sup = round(entry - max(below), 1) if below else ""
    return res, sup

# ── CHOP SIGNAL (M15 range + M5 sweep & rejection) ────────
def chop_signal(m15, m5, h1_macd=0.0):
    """
    Range = high/low of the M15 candles before the most recent 3 (45 min).
    Signal when an M5 candle in the last 3 wicked beyond a range edge and the
    latest M5 closed back inside the range with a rejection candle.

    A real range must also pass these filters, otherwise a trend that keeps
    making new highs/lows looks like endless "sweeps" and the bot fades it:
      1. H1 momentum is weak (|H1 MACD| <= CHOP_MAX_H1_MACD)
      2. Range midpoint is not drifting (older vs newer half of the lookback)
      3. Both edges were tested at least CHOP_MIN_TOUCHES times
      4. The sweep is shallow, and no recent M15 candle closed outside the range
    Uses candles already fetched for the BOS scan, so no extra API calls.
    Returns a dict or None.
    """
    if not m15 or len(m15) < CHOP_LOOKBACK + 3 or not m5 or len(m5) < 3:
        return None
    if abs(h1_macd) > CHOP_MAX_H1_MACD:
        return None   # momentum too strong, not a range

    rng      = m15[3:3 + CHOP_LOOKBACK]
    r_high   = max(c["h"] for c in rng)
    r_low    = min(c["l"] for c in rng)
    width    = r_high - r_low
    if width < CHOP_MIN_RANGE or width > CHOP_MAX_RANGE:
        return None
    mid      = (r_high + r_low) / 2

    # drift: newer half vs older half of the range window
    half = CHOP_LOOKBACK // 2
    new_part, old_part = rng[:half], rng[half:]
    new_mid = (max(c["h"] for c in new_part) + min(c["l"] for c in new_part)) / 2
    old_mid = (max(c["h"] for c in old_part) + min(c["l"] for c in old_part)) / 2
    if abs(new_mid - old_mid) > CHOP_MAX_DRIFT * width:
        return None

    # both edges must have been respected more than once
    zone = CHOP_EDGE_ZONE * width
    top_touches = sum(1 for c in rng if c["h"] >= r_high - zone)
    bot_touches = sum(1 for c in rng if c["l"] <= r_low + zone)
    if top_touches < CHOP_MIN_TOUCHES or bot_touches < CHOP_MIN_TOUCHES:
        return None

    latest   = m5[0]
    recent   = m5[:3]
    hi_wick  = max(c["h"] for c in recent)
    lo_wick  = min(c["l"] for c in recent)
    entry    = latest["c"]

    swept_high = hi_wick > r_high
    swept_low  = lo_wick < r_low
    if swept_high and swept_low:
        return None  # ambiguous, skip

    # a close outside the range on M15 = breakout, not a sweep
    recent_m15 = m15[:3]
    if swept_high:
        if hi_wick - r_high > CHOP_MAX_SWEEP * width:
            return None
        if any(c["c"] > r_high for c in recent_m15):
            return None
    if swept_low:
        if r_low - lo_wick > CHOP_MAX_SWEEP * width:
            return None
        if any(c["c"] < r_low for c in recent_m15):
            return None

    if swept_high and mid < entry < r_high and latest["c"] < latest["o"]:
        sl   = hi_wick + CHOP_SL_BUFFER
        risk = sl - entry
        if risk <= 0:
            return None
        tp1, tp2 = mid, r_low
        rr2 = (entry - tp2) / risk
        if rr2 < CHOP_MIN_RR:
            return None
        return {"direction": "SELL", "entry": entry, "sl": sl, "tp1": tp1,
                "tp2": tp2, "rr2": round(rr2, 1), "r_high": r_high,
                "r_low": r_low, "swept": "range high"}

    if swept_low and r_low < entry < mid and latest["c"] > latest["o"]:
        sl   = lo_wick - CHOP_SL_BUFFER
        risk = entry - sl
        if risk <= 0:
            return None
        tp1, tp2 = mid, r_high
        rr2 = (tp2 - entry) / risk
        if rr2 < CHOP_MIN_RR:
            return None
        return {"direction": "BUY", "entry": entry, "sl": sl, "tp1": tp1,
                "tp2": tp2, "rr2": round(rr2, 1), "r_high": r_high,
                "r_low": r_low, "swept": "range low"}

    return None

# ── TIGHT-MARKET SIGNALS (M30 squeeze box / M15 break / M5 trigger) ──
def _m30_from_m15(m15):
    """Builds M30 candles from M15 candles (newest-first in, newest-first out).
    Only complete pairs (:00 + :15, or :30 + :45) are used. No extra API calls."""
    buckets, order = {}, []
    for c in m15:
        t = c.get("t", "")
        try:
            minute = int(t[14:16])
        except (ValueError, IndexError):
            continue
        key = t[:14] + ("00" if minute < 30 else "30")
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(c)
    out = []
    for key in order:
        cs = buckets[key]
        if len(cs) != 2:
            continue
        out.append({"t": key,
                    "o": cs[-1]["o"], "c": cs[0]["c"],
                    "h": max(x["h"] for x in cs), "l": min(x["l"] for x in cs)})
    return out

def tight_box(m15):
    """Finds a tight box: the last 2*TIGHT_BARS M15 candles that sit just before
    the newest TIGHT_WINDOW candles, but only when the M30 candles in that stretch
    are clearly smaller than the M30 candles before it (a squeeze).
    Returns {"high", "low", "mid", "width"} or None."""
    if not m15 or len(m15) < TIGHT_WINDOW + 2 * TIGHT_BARS:
        return None
    older = m15[TIGHT_WINDOW:]
    m30 = _m30_from_m15(older)
    if len(m30) < 2 * TIGHT_BARS:
        return None
    recent_m30 = m30[:TIGHT_BARS]
    prior_m30  = m30[TIGHT_BARS:2 * TIGHT_BARS]
    avg = lambda xs: sum(c["h"] - c["l"] for c in xs) / len(xs)
    if avg(recent_m30) > TIGHT_SQUEEZE * avg(prior_m30):
        return None          # no squeeze: candles are not getting smaller
    box = older[:2 * TIGHT_BARS]
    hi = max(c["h"] for c in box)
    lo = min(c["l"] for c in box)
    width = hi - lo
    if width < TIGHT_MIN_BOX or width > TIGHT_MAX_BOX:
        return None
    return {"high": hi, "low": lo, "mid": (hi + lo) / 2, "width": width}

def tight_signals(m15, m5, h1_macd=0.0):
    """
    TIGHT BREAKOUT: an M15 candle closed outside the box, no M15 candle has
      closed back inside since, and an M5 candle retested the broken edge and
      closed away from it in the break direction.
    TIGHT FAKE-OUT: an M15 candle closed outside the box, then a later M15
      candle closed back inside; an M5 candle confirms the move back.
    Uses candles already fetched for the other signals (no extra API calls).
    Returns a list of signal dicts (usually empty).
    """
    box = tight_box(m15)
    if not box or not m5 or len(m5) < 3 or len(m15) < TIGHT_WINDOW:
        return []
    hi, lo, mid, width = box["high"], box["low"], box["mid"], box["width"]

    win   = m15[:TIGHT_WINDOW]            # newest first
    entry = m5[0]["c"]
    last5 = m5[:3]
    bullish_m5 = m5[0]["c"] > m5[0]["o"]
    bearish_m5 = m5[0]["c"] < m5[0]["o"]

    # first (oldest) M15 close outside the box inside the window
    brk = None
    for i in range(len(win) - 1, -1, -1):
        if win[i]["c"] > hi:
            brk = ("UP", i)
            break
        if win[i]["c"] < lo:
            brk = ("DOWN", i)
            break
    if not brk:
        return []
    side, i = brk
    up = side == "UP"
    since_break = win[:i + 1]             # break candle and everything after it

    held = all((c["c"] > hi) if up else (c["c"] < lo) for c in since_break)
    out = []

    if held:
        # ── BREAKOUT: break still holds; trade the M5 retest of the edge ──
        if not TIGHT_BREAKOUT:
            return []
        if up:
            probe   = min(c["l"] for c in last5)
            touched = probe <= hi + TIGHT_RETEST_TOL
            good    = entry > hi and bullish_m5
            sl      = min(probe - TIGHT_SL_BUFFER, hi - 0.3 * width)
            risk    = entry - sl
            aligned = (not TIGHT_H1_ALIGN) or h1_macd > 0
        else:
            probe   = max(c["h"] for c in last5)
            touched = probe >= lo - TIGHT_RETEST_TOL
            good    = entry < lo and bearish_m5
            sl      = max(probe + TIGHT_SL_BUFFER, lo + 0.3 * width)
            risk    = sl - entry
            aligned = (not TIGHT_H1_ALIGN) or h1_macd < 0
        if touched and good and aligned and 0 < risk <= TIGHT_MAX_RISK:
            sign = 1 if up else -1
            tps = [("TP1", entry + sign * risk),
                   ("TP2", entry + sign * risk * 2),
                   ("TP3", entry + sign * risk * 3)]
            out.append({"kind": "TIGHT BREAKOUT",
                        "direction": "BUY" if up else "SELL",
                        "entry": entry, "sl": sl, "tps": tps, "rr2": 2.0,
                        "box": box,
                        "setup": f"M15 closed {'above' if up else 'below'} box + M5 retest of the {'high' if up else 'low'}"})
        return out

    # ── FAKE-OUT: broke out, then closed back inside the box ──
    if not TIGHT_FAKEOUT:
        return []
    if i == 0 or not (lo <= win[0]["c"] <= hi):
        return []
    if up:
        extreme = max(c["h"] for c in since_break)
        if not (entry < hi and entry > mid and bearish_m5):
            return []
        sl   = extreme + TIGHT_SL_BUFFER
        risk = sl - entry
        tp1, tp2 = mid, lo
        rr2  = (entry - tp2) / risk if risk > 0 else 0
        direction = "SELL"
        setup = "M15 broke above the box, closed back inside + M5 rejection"
    else:
        extreme = min(c["l"] for c in since_break)
        if not (entry > lo and entry < mid and bullish_m5):
            return []
        sl   = extreme - TIGHT_SL_BUFFER
        risk = entry - sl
        tp1, tp2 = mid, hi
        rr2  = (tp2 - entry) / risk if risk > 0 else 0
        direction = "BUY"
        setup = "M15 broke below the box, closed back inside + M5 rejection"
    if 0 < risk <= TIGHT_MAX_RISK and rr2 >= TIGHT_MIN_RR:
        out.append({"kind": "TIGHT FAKE-OUT", "direction": direction,
                    "entry": entry, "sl": sl,
                    "tps": [("TP1", tp1), ("TP2", tp2)], "rr2": round(rr2, 1),
                    "box": box, "setup": setup})
    return out

# ── TRADE TRACKER ─────────────────────────────────────────
active_trades = []
chop_state = {"losses": 0, "paused_until": 0}

def add_trade(pair, kind, direction, entry, sl, tps, meta=None, shadow=False):
    """tps = [("TP1", price), ("TP2", price), ...] in order.
    shadow=True tracks a trade silently (no Telegram alerts), log only."""
    active_trades.append({
        "pair": pair, "kind": kind, "direction": direction,
        "entry": entry, "sl": sl, "risk": abs(entry - sl),
        "tps": tps, "hit": 0, "base_t": None, "created": time.time(),
        "opened": _utcnow().strftime("%Y-%m-%d %H:%M"),
        "meta": meta or {}, "shadow": shadow,
    })
    print(f"{pair}: tracking {kind} {direction} @ {entry:.4f}")
    active_trades[-1]["h4"] = status.get("h4b", "")
    if EARLY_ALERTS and not shadow:
        try:
            send_valid_until(pair, kind, direction, entry, sl)
        except Exception as e:
            print(f"valid-until error: {e}")

def opposite_open(pair, direction):
    """True when a LIVE trade (not a silent/test/blocked one) is open on this pair
    in the opposite direction. direction is "BUY" or "SELL"."""
    if not OPPOSITE_BLOCK:
        return False
    return any(t["pair"] == pair and t["direction"] != direction and not t.get("shadow")
               for t in active_trades)

def log_blocked(pair, kind, direction, entry, sl, tps, meta=None):
    """Tracks a blocked opposite-direction signal silently so the trade log can
    show how it would have ended. Never raises."""
    try:
        add_trade(pair, kind, direction, entry, sl, tps, meta=meta, shadow=True)
        active_trades[-1]["blocked"] = True
        print(f"{pair}: {kind} {direction} blocked (opposite trade open), tracked silently")
    except Exception as e:
        print(f"{pair}: blocked-trade tracking error: {e}")

def _r_mult(trade, price):
    return abs(price - trade["entry"]) / trade["risk"] if trade["risk"] > 0 else 0

def _reached(trade, c, price):
    if trade["direction"] == "BUY":
        return c["h"] >= price - TRACK_TOLERANCE
    return c["l"] <= price + TRACK_TOLERANCE

def _stopped(trade, c, level):
    return c["l"] <= level if trade["direction"] == "BUY" else c["h"] >= level

# ── TRADE LOG ─────────────────────────────────────────────
LOG_FIELDS = ["opened_utc", "closed_utc", "pair", "kind", "direction", "entry", "sl",
              "risk_pips", "outcome", "tps_hit", "r_hold", "r_tp1", "pips_hold",
              "pips_tp1", "condition", "box_or_range", "liq_grab", "res_pts",
              "sup_pts", "h1_macd", "h4_bias", "mae_pts", "mfe_pts"]
trade_log = []        # closed trades (dicts), oldest first
log_cursor = 0        # trades before this index were already in a daily summary
_log_loaded = False

def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def _r_of(t, price):
    return abs(price - t["entry"]) / t["risk"] if t["risk"] > 0 else 0.0

def _load_trade_log():
    """Reads the CSV (if it survived) once at startup."""
    global log_cursor, _log_loaded
    if _log_loaded:
        return
    _log_loaded = True
    try:
        if os.path.exists(TRADE_LOG_PATH):
            with open(TRADE_LOG_PATH, newline="") as f:
                for row in csv.DictReader(f):
                    for k in ("r_hold", "r_tp1", "pips_hold", "pips_tp1"):
                        row[k] = _num(row.get(k))
                    trade_log.append(row)
    except Exception as e:
        print(f"Trade log load error: {e}")
    log_cursor = len(trade_log)

def log_trade(t, outcome):
    """outcome: SL | BE | TP_FINAL | TIMEOUT | WEEKEND.
    r_hold   = result if you hold with the stop at entry after TP1 (what the bot tracks)
    r_tp1    = result if you close everything at TP1 (SL = -1R if TP1 never came)
    Never raises, so a logging problem can't stop the bot."""
    try:
        m = t.get("meta", {})
        hit = t["hit"]
        risk_pips = pips(t["entry"], t["sl"])
        r1 = _r_of(t, t["tps"][0][1]) if t["tps"] else None
        if outcome == "SL":
            r_hold = -1.0
        elif outcome == "BE":
            r_hold = 0.0
        elif outcome == "TP_FINAL":
            r_hold = _r_of(t, t["tps"][-1][1])
        else:
            # TIMEOUT / WEEKEND: mark to market at the last price seen
            r_hold = None
            px = t.get("last_px")
            if px is not None and t["risk"] > 0:
                move = (px - t["entry"]) if t["direction"] == "BUY" else (t["entry"] - px)
                r_hold = max(-1.0, move / t["risk"])
        if hit >= 1:
            r_tp1 = r1
        elif outcome == "SL":
            r_tp1 = -1.0
        else:
            r_tp1 = None
        if t.get("shadow") and not t.get("test"):
            kind_label = t["kind"] + (" (BLOCKED)" if t.get("blocked") else " (SKIPPED)")
        else:
            kind_label = t["kind"]
        row = {
            "opened_utc": t.get("opened", ""),
            "closed_utc": _utcnow().strftime("%Y-%m-%d %H:%M"),
            "pair": t["pair"],
            "kind": kind_label,
            "direction": t["direction"],
            "entry": round(t["entry"], 2), "sl": round(t["sl"], 2),
            "risk_pips": risk_pips, "outcome": outcome, "tps_hit": hit,
            "r_hold": None if r_hold is None else round(r_hold, 2),
            "r_tp1": None if r_tp1 is None else round(r_tp1, 2),
            "pips_hold": None if r_hold is None else round(r_hold * risk_pips),
            "pips_tp1": None if r_tp1 is None else round(r_tp1 * risk_pips),
            "condition": m.get("cond", ""), "box_or_range": m.get("size", ""),
            "liq_grab": m.get("liq", ""), "res_pts": m.get("res", ""),
            "sup_pts": m.get("sup", ""), "h1_macd": m.get("h1macd", ""),
            "h4_bias": t.get("h4", ""),
            "mae_pts": round(t.get("mae", 0.0), 1),
            "mfe_pts": round(t.get("mfe", 0.0), 1),
        }
        trade_log.append(row)
        new_file = not os.path.exists(TRADE_LOG_PATH)
        with open(TRADE_LOG_PATH, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if new_file:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"Trade log error: {e}")

def _fmt_r(x):
    return "n/a" if x is None else f"{x:+.1f}R"

def _label(r):
    hit = int(_num(r.get("tps_hit")) or 0)
    o = r["outcome"]
    if o == "SL":
        return "SL -1.0R"
    if o == "BE":
        return f"TP{hit} then BE"
    if o == "TP_FINAL":
        return f"TP{hit} {_fmt_r(r['r_hold'])}"
    return f"{o.lower()} (TP{hit})"

def _group_stats(rows):
    done = [r for r in rows if r["r_hold"] is not None]
    wins = sum(1 for r in done if r["r_hold"] > 0)
    be   = sum(1 for r in done if r["r_hold"] == 0)
    loss = sum(1 for r in done if r["r_hold"] < 0)
    hold = sum(r["r_hold"] for r in done)
    pip_hold = sum((r["pips_hold"] or 0) for r in done)
    tp1 = sum(r["r_tp1"] for r in rows if r["r_tp1"] is not None)
    return len(rows), wins, be, loss, hold, pip_hold, tp1

def send_log_file():
    try:
        with open(TRADE_LOG_PATH, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                          data={"chat_id": TELEGRAM_CHAT_ID},
                          files={"document": ("trade_log.csv", f)}, timeout=30)
    except Exception as e:
        print(f"Log file send error: {e}")

def send_daily_log():
    """Telegram summary of trades closed since the last summary + running totals."""
    global log_cursor
    try:
        new = trade_log[log_cursor:]
        log_cursor = len(trade_log)
        if not new:
            return
        lines = [f"📒 TRADE LOG — {_utcnow():%Y-%m-%d}",
                 f"Closed since last log: {len(new)}"]
        for r in new[:15]:
            pp = r.get("pips_hold")
            tail = f" ({int(pp):+d} pips)" if pp is not None else ""
            lines.append(f"• {str(r['opened_utc'])[11:16]} {r['kind']} {r['direction']}: {_label(r)}{tail}")
        if len(new) > 15:
            lines.append(f"... and {len(new) - 15} more (see CSV)")
        lines.append("— All logged trades —")
        kinds = []
        for r in trade_log:
            if r["kind"] not in kinds:
                kinds.append(r["kind"])
        for k in kinds:
            n, w, be, l, hold, pip_hold, tp1 = _group_stats([r for r in trade_log if r["kind"] == k])
            lines.append(f"{k}: {n} | W{w} BE{be} L{l} | hold {hold:+.1f}R ({pip_hold:+.0f} pips) | close-at-TP1 {tp1:+.1f}R")
        send("\n".join(lines)[:3900])
        if TRADE_LOG_SEND_CSV and os.path.exists(TRADE_LOG_PATH):
            send_log_file()
    except Exception as e:
        print(f"Daily log error: {e}")

def process_candle(t, c):
    """Returns True when the trade is finished."""
    say  = (lambda m: None) if t.get("shadow") else send
    head = f"{t['pair']} {t['direction']} ({t['kind']})"
    _update_excursion(t, c)
    be   = BE_AFTER_TP1 and t["hit"] >= 1
    stop = t["entry"] if be else t["sl"]

    # Stop is checked first, so a candle touching both stop and target counts
    # as a loss (conservative).
    if _stopped(t, c, stop):
        if be:
            say(f"🟡 BREAKEVEN — {head}\n"
                f"Price back at entry {t['entry']:.4f} after TP1.\n"
                f"If you moved SL to entry, you're out at ~0R.")
            log_trade(t, "BE")
        else:
            say(f"❌ STOP LOSS HIT — {head}\n"
                f"SL {t['sl']:.4f} touched (-1R, -{pips(t['entry'], t['sl'])} pips). Trade closed.")
            log_trade(t, "SL")
            if t["kind"] == "CHOP" and not t.get("shadow"):
                chop_state["losses"] += 1
                if chop_state["losses"] >= CHOP_MAX_LOSSES:
                    chop_state["paused_until"] = time.time() + CHOP_PAUSE_HOURS * 3600
                    chop_state["losses"] = 0
                    send(f"⏸ CHOP signals paused for {CHOP_PAUSE_HOURS:g}h "
                         f"after {CHOP_MAX_LOSSES} stop-outs in a row.\n"
                         f"Market is likely trending, not ranging.")
        return True

    while t["hit"] < len(t["tps"]):
        name, price = t["tps"][t["hit"]]
        if not _reached(t, c, price):
            break
        t["hit"] += 1
        if t["kind"] == "CHOP" and not t.get("shadow"):
            chop_state["losses"] = 0      # a win resets the loss streak
        final = t["hit"] == len(t["tps"])
        if final:
            tail = "🏁 Final target reached. Trade complete."
        elif t["hit"] == 1 and BE_AFTER_TP1:
            tail = f"👉 Move SL to entry ({t['entry']:.4f}) to protect the trade."
        else:
            tail = "👉 Consider closing part or trailing your stop."
        say(f"✅ {name} HIT — {head}\n"
            f"Price reached {price:.4f} (+{_r_mult(t, price):.1f}R, +{pips(t['entry'], price)} pips)\n{tail}")
        if final:
            log_trade(t, "TP_FINAL")
            return True
    return False

def track_trades():
    if not active_trades:
        return
    now_ts = time.time()

    for t in active_trades[:]:
        if now_ts - t["created"] > TRACK_MAX_HOURS * 3600:
            if not t.get("shadow"):
                send(f"⏱ Tracking stopped — {t['pair']} {t['direction']} ({t['kind']})\n"
                     f"Open for over {TRACK_MAX_HOURS:g}h with no final result. Manage it manually.")
            log_trade(t, "TIMEOUT")
            active_trades.remove(t)

    for pair in {t["pair"] for t in active_trades}:
        candles = fetch(pair, "1min", 15)
        if not candles:
            continue
        newest_t = candles[0]["t"]
        for t in [x for x in active_trades if x["pair"] == pair]:
            if t["base_t"] is None:
                t["base_t"] = newest_t      # baseline taken right after the signal
                continue
            done = False
            for c in reversed(candles):     # oldest -> newest
                if c["t"] >= t["base_t"] and process_candle(t, c):
                    done = True
                    break
            if done:
                active_trades.remove(t)
            else:
                t["base_t"] = newest_t

# ======================================================================
# EARLY WARNING (WATCH / READY), VALID-UNTIL, EXTRA LOG DATA
# Read-only add-on. Nothing in here decides whether a trade is taken.
# Set EARLY_ALERTS=0 on Render to switch the new Telegram messages off.
# ======================================================================
EARLY_ALERTS    = os.environ.get("EARLY_ALERTS", "1") == "1"
EARLY_TIGHT     = os.environ.get("EARLY_TIGHT", "1") == "1"      # 0 = no WATCH/READY for tight boxes
WATCH_COOLDOWN  = float(os.environ.get("WATCH_COOLDOWN", "1800"))  # seconds between WATCH alerts (same setup)
READY_COOLDOWN  = float(os.environ.get("READY_COOLDOWN", "900"))   # seconds between READY alerts (same setup)
BOS_TOLERANCE   = 15.0   # same number the trend scan uses for "price at BOS level"
VALID_FRAC      = float(os.environ.get("VALID_FRAC", "0.3"))     # entry stale after this share of 1R is gone
VALID_MINUTES   = int(os.environ.get("VALID_MINUTES", "30"))     # ...or after this many minutes
H4_CACHE_MIN    = float(os.environ.get("H4_CACHE_MIN", "30"))    # reuse H4 candles this many minutes (saves ~140 credits/day); 0 = fetch every scan

early_sent = {}


def _early_ok(key, cooldown):
    now = time.time()
    if now - early_sent.get(key, 0) < cooldown:
        return False
    early_sent[key] = now
    return True


_h4_cache = {}


def fetch_h4(pair):
    """Same as fetch(pair, '4h', 30). With H4_CACHE_MIN > 0 it reuses the last
    H4 candles for that many minutes, saving Twelve Data credits."""
    ttl = H4_CACHE_MIN * 60
    if ttl > 0:
        hit = _h4_cache.get(pair)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    data = fetch(pair, "4h", 30)
    if data and ttl > 0:
        _h4_cache[pair] = (time.time(), data)
    return data


def _update_excursion(t, c):
    """Tracks how far price went against (mae) and for (mfe) the trade, in points."""
    try:
        if t["direction"] == "BUY":
            adverse, favour = t["entry"] - c["l"], c["h"] - t["entry"]
        else:
            adverse, favour = c["h"] - t["entry"], t["entry"] - c["l"]
        t["mae"] = max(t.get("mae", 0.0), adverse)
        t["mfe"] = max(t.get("mfe", 0.0), favour)
        t["last_px"] = c["c"]
    except Exception:
        pass


def send_valid_until(pair, kind, direction, entry, sl):
    """Short follow-up message right after a signal: when the entry goes stale."""
    risk = abs(entry - sl)
    if risk <= 0:
        return
    if direction == "SELL":
        lim = entry - VALID_FRAC * risk
        rule = f"price is still ABOVE {lim:.2f}"
    else:
        lim = entry + VALID_FRAC * risk
        rule = f"price is still BELOW {lim:.2f}"
    until = (_utcnow() + timedelta(minutes=VALID_MINUTES)).strftime("%H:%M")
    send(f"⏱ VALID UNTIL — {pair} {direction} ({kind})\n"
         f"Enter only while {rule}, and before {until} UTC.\n"
         f"Past that, too much of the move is gone: skip it.")


def _early_trend(pair, h4, h1, m15, m5, now_ts, now_str):
    """WATCH / READY for the trend signal. Mirrors the trend conditions but only reads."""
    if now_ts - last_signal_time.get(pair, 0) < signal_cooldown:
        return
    h4b = bias(h4)
    if h4b == "NEUTRAL":
        h4b = bias(m15)
    if h4b == "NEUTRAL" or h4b != bias(h1):
        return
    h1_macd = macd_value(h1)
    if h4b == "BEARISH" and h1_macd > -5:
        return
    if h4b == "BULLISH" and h1_macd < 5:
        return
    if not macd_aligned(h1, h4b):
        return
    h1_bos, h1_wick, _ = bos(h1, h4b)
    if not h1_bos:
        return
    side = "SELL" if h4b == "BEARISH" else "BUY"
    if opposite_open(pair, side):
        return                      # an opposite live trade is open: no alerts for this side
    if TREND_MAX_OPEN > 0:
        open_same = sum(1 for t in active_trades
                        if t["kind"] == "TREND" and t["pair"] == pair
                        and t["direction"] == side and not t.get("shadow"))
        if open_same >= TREND_MAX_OPEN:
            return

    price    = m5[0]["c"]
    gap      = abs(price - h1_wick)
    at_level = gap <= BOS_TOLERANCE
    m15_ok   = bias(m15) == h4b
    trig     = m5_trigger(m5, h4b)
    if at_level and m15_ok and trig:
        return                      # every condition met: the real signal fires now

    if h4b == "BEARISH":
        trig_level = min(c["l"] for c in m5[2:7])
        trig_text  = f"an M5 candle closes BELOW {trig_level:.2f}"
    else:
        trig_level = max(c["h"] for c in m5[2:7])
        trig_text  = f"an M5 candle closes ABOVE {trig_level:.2f}"

    if at_level and m15_ok:         # only the M5 trigger is missing
        if _early_ok((pair, "READY", side), READY_COOLDOWN):
            send(f"🟠 READY — {side} (trend) — {pair}\n"
                 f"━━━━━━━━━━━━━━━━\n"
                 f"Everything lines up except the M5 trigger.\n"
                 f"Price     : {price:.2f}\n"
                 f"BOS level : {h1_wick:.2f}\n"
                 f"Fires when: {trig_text}\n"
                 f"━━━━━━━━━━━━━━━━\n"
                 f"Get your chart ready. Not an entry yet: wait for the SIGNAL ALERT.\n"
                 f"Time: {now_str}")
        return

    if not at_level:
        why = (f"Price is {gap:.1f} pts from the BOS level ({h1_wick:.2f}). "
               f"Waiting for it to come back within {BOS_TOLERANCE:g} pts.")
    else:
        why = "Price is at the BOS level. Waiting for M15 to line up."
    if _early_ok((pair, "WATCH", side), WATCH_COOLDOWN):
        send(f"👀 WATCH — {side} (trend) setup forming — {pair}\n"
             f"━━━━━━━━━━━━━━━━\n"
             f"H4/H1 bias {h4b}, H1 break of structure is in.\n"
             f"{why}\n"
             f"Price: {price:.2f}\n"
             f"━━━━━━━━━━━━━━━━\n"
             f"Not an entry. A READY alert comes when one step is left.\n"
             f"Time: {now_str}")


def _early_tight(pair, h1, m15, m5, now_ts, now_str):
    """WATCH (box formed) and READY (break closed, waiting for M5 retest) for TIGHT."""
    if not (TIGHT_ENABLED and EARLY_TIGHT) or not m5 or len(m5) < 3:
        return
    box = tight_box(m15)
    if not box:
        return
    if tight_signals(m15, m5, macd_value(h1)):
        return                      # a real tight signal is firing or on cooldown
    hi, lo = box["high"], box["low"]
    tag = f"{hi:.1f}-{lo:.1f}"
    win = m15[:TIGHT_WINDOW]
    brk = None
    for i in range(len(win) - 1, -1, -1):
        if win[i]["c"] > hi:
            brk = ("UP", i)
            break
        if win[i]["c"] < lo:
            brk = ("DOWN", i)
            break

    if not brk:
        if _early_ok((pair, "TIGHT WATCH", tag), TIGHT_COOLDOWN):
            send(f"👀 WATCH — TIGHT box formed — {pair}\n"
                 f"━━━━━━━━━━━━━━━━\n"
                 f"Box : {lo:.2f} — {hi:.2f}  ({box['width']:.1f} pts)\n"
                 f"Price: {m5[0]['c']:.2f}\n"
                 f"A breakout above {hi:.2f} or below {lo:.2f} can follow.\n"
                 f"Not an entry.\nTime: {now_str}")
        return

    side, i = brk
    up = side == "UP"
    held = all((c["c"] > hi) if up else (c["c"] < lo) for c in win[:i + 1])
    if not held or not TIGHT_BREAKOUT:
        return                      # failed break: the fake-out logic handles it
    if opposite_open(pair, "BUY" if up else "SELL"):
        return                      # an opposite live trade is open: this signal would be blocked
    edge = hi if up else lo
    if _early_ok((pair, "TIGHT READY", tag, side), TIGHT_COOLDOWN):
        send(f"🟠 READY — TIGHT BREAKOUT {'BUY' if up else 'SELL'} — {pair}\n"
             f"━━━━━━━━━━━━━━━━\n"
             f"M15 closed {'above' if up else 'below'} the box ({lo:.2f} — {hi:.2f}).\n"
             f"Price: {m5[0]['c']:.2f}\n"
             f"Fires when M5 retests within {TIGHT_RETEST_TOL:g} pts of {edge:.2f} "
             f"and closes {'bullish' if up else 'bearish'}.\n"
             f"Not an entry yet: wait for the SIGNAL ALERT.\nTime: {now_str}")


def early_alerts(pair, h4, h1, m15, m5, now_ts, now_str):
    """Called once per scan. Never raises and never changes any signal."""
    try:
        status["h4b"] = bias(h4)    # saved into the trade log
    except Exception:
        pass
    if not EARLY_ALERTS:
        return
    try:
        _early_trend(pair, h4, h1, m15, m5, now_ts, now_str)
    except Exception as e:
        print(f"{pair}: early trend alert error: {e}")
    try:
        _early_tight(pair, h1, m15, m5, now_ts, now_str)
    except Exception as e:
        print(f"{pair}: early tight alert error: {e}")


# ======================================================================
# SILENT TEST SIGNALS (log only: nothing is ever sent to Telegram)
#   TEST H1-RETEST  = H1 candle CLOSES beyond structure, then M5 retests the
#                     broken level and rejects it (your BOS retest, done properly)
#   TEST LTF-RETEST = M15 candle closes beyond structure, M5 retests the broken
#                     level, M1 gives the trigger (M15 / M5 / M1)
# They are tracked like the "(SKIPPED)" trades and appear in the daily log and
# CSV under their own names, so you can compare them with TREND and TIGHT.
# TEST_ENABLED=0 on Render switches both off.
# ======================================================================
TEST_ENABLED    = os.environ.get("TEST_ENABLED", "1") == "1"
H1RT_ENABLED    = os.environ.get("H1RT_ENABLED", "1") == "1"
LTF_ENABLED     = os.environ.get("LTF_ENABLED", "1") == "1"
TEST_MAX_OPEN   = int(os.environ.get("TEST_MAX_OPEN", "3"))        # open test trades at once
TEST_CREDIT_CAP = int(os.environ.get("TEST_CREDIT_CAP", "700"))    # no NEW test trades above this many Twelve Data calls today
TEST_HARD_CAP   = int(os.environ.get("TEST_HARD_CAP", "750"))      # above this, open test trades are closed to protect live tracking

H1RT_TOL        = float(os.environ.get("H1RT_TOL", "4"))           # M5 must come back within this many pts of the broken level
H1RT_OVER       = float(os.environ.get("H1RT_OVER", "8"))          # ...but not push more than this beyond it (break failed)
H1RT_BUFFER     = float(os.environ.get("H1RT_BUFFER", "3"))        # stop sits this far beyond the retest extreme
H1RT_MIN_RISK   = float(os.environ.get("H1RT_MIN_RISK", "4"))
H1RT_MAX_RISK   = float(os.environ.get("H1RT_MAX_RISK", "30"))
H1RT_COOLDOWN   = float(os.environ.get("H1RT_COOLDOWN", "3600"))
H1RT_MAX_HOURS  = float(os.environ.get("H1RT_MAX_HOURS", "3"))

LTF_MIN_MACD    = float(os.environ.get("LTF_MIN_MACD", "1.0"))     # |M15 MACD| needed to pick a direction
LTF_TOL         = float(os.environ.get("LTF_TOL", "2"))
LTF_OVER        = float(os.environ.get("LTF_OVER", "5"))
LTF_BUFFER      = float(os.environ.get("LTF_BUFFER", "2"))
LTF_MIN_RISK    = float(os.environ.get("LTF_MIN_RISK", "3"))
LTF_MAX_RISK    = float(os.environ.get("LTF_MAX_RISK", "12"))
LTF_COOLDOWN    = float(os.environ.get("LTF_COOLDOWN", "900"))
LTF_MAX_HOURS   = float(os.environ.get("LTF_MAX_HOURS", "1.5"))

test_last = {}


def _dt(s):
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _closed_break(candles, direction, max_age):
    """Most recent CLOSED candle (index 1..max_age; index 0 is still forming)
    whose close is beyond the extreme of the 5 candles before it, and which no
    later closed candle has closed back through. Returns (index, level) or None."""
    for k in range(1, max_age + 1):
        base = candles[k + 1:k + 6]
        if len(base) < 5:
            return None
        if direction == "BEARISH":
            lvl = min(c["l"] for c in base)
            if candles[k]["c"] < lvl and all(c["c"] < lvl for c in candles[1:k]):
                return k, lvl
        else:
            lvl = max(c["h"] for c in base)
            if candles[k]["c"] > lvl and all(c["c"] > lvl for c in candles[1:k]):
                return k, lvl
    return None


def _retest(m5, direction, lvl, cutoff, tol, over):
    """Looks at the M5 candles after the break. Returns the retest candle (the one
    that reached back to the broken level) or None. The last closed M5 candle must
    have rejected the level (closed back on the break side, in the break direction)."""
    post = [c for c in m5 if (_dt(c["t"]) or cutoff) >= cutoff]
    if len(post) < 2 or len(m5) < 2:
        return None
    last = m5[1]
    if (_dt(last["t"]) or cutoff) < cutoff:
        return None
    if direction == "BEARISH":
        probe = max(post, key=lambda c: c["h"])
        if not (lvl - tol <= probe["h"] <= lvl + over):
            return None
        if not (last["c"] < lvl and last["c"] < last["o"]):
            return None
    else:
        probe = min(post, key=lambda c: c["l"])
        if not (lvl - over <= probe["l"] <= lvl + tol):
            return None
        if not (last["c"] > lvl and last["c"] > last["o"]):
            return None
    return probe


def h1_retest_signal(h4, h1, m15, m5):
    """TEST H1-RETEST. Same direction rules as the trend signal, but the H1 break
    must be a CLOSED candle and entry waits for the M5 retest + rejection."""
    h4b = bias(h4)
    if h4b == "NEUTRAL":
        h4b = bias(m15)
    if h4b == "NEUTRAL" or h4b != bias(h1):
        return None
    h1m = macd_value(h1)
    if h4b == "BEARISH" and h1m > -5:
        return None
    if h4b == "BULLISH" and h1m < 5:
        return None
    brk = _closed_break(h1, h4b, 3)
    if not brk:
        return None
    k, lvl = brk
    t_break = _dt(h1[k]["t"])
    if t_break is None:
        return None
    probe = _retest(m5, h4b, lvl, t_break + timedelta(hours=1), H1RT_TOL, H1RT_OVER)
    if not probe:
        return None
    entry = m5[0]["c"]
    if h4b == "BEARISH":
        sl, side = probe["h"] + H1RT_BUFFER, "SELL"
        risk = sl - entry
    else:
        sl, side = probe["l"] - H1RT_BUFFER, "BUY"
        risk = entry - sl
    if not (H1RT_MIN_RISK <= risk <= H1RT_MAX_RISK):
        return None
    return {"side": side, "dir": h4b, "entry": entry, "sl": sl, "risk": risk,
            "level": lvl, "h1macd": h1m}


def ltf_retest_signal(m15, m5, get_m1):
    """TEST LTF-RETEST. M15 closed break, M5 retest + rejection, M1 trigger.
    get_m1() costs 1 Twelve Data credit and is only called when M15 and M5 already agree."""
    mv = macd_value(m15)
    if abs(mv) < LTF_MIN_MACD:
        return None
    direction = "BEARISH" if mv < 0 else "BULLISH"
    brk = _closed_break(m15, direction, 4)
    if not brk:
        return None
    k, lvl = brk
    t_break = _dt(m15[k]["t"])
    if t_break is None:
        return None
    cutoff = t_break + timedelta(minutes=15)
    probe = _retest(m5, direction, lvl, cutoff, LTF_TOL, LTF_OVER)
    if not probe:
        return None
    t_probe = _dt(probe["t"])
    m1 = get_m1()
    if not m1 or len(m1) < 8 or t_probe is None:
        return None
    trig = None
    for j in range(1, 4):                         # last 3 CLOSED M1 candles
        base = m1[j + 1:j + 4]
        c = m1[j]
        ct = _dt(c["t"])
        if len(base) < 3 or ct is None or ct < t_probe:
            continue
        if direction == "BEARISH":
            if c["c"] < min(x["l"] for x in base) and c["c"] < lvl and c["c"] < c["o"]:
                trig = c
                break
        else:
            if c["c"] > max(x["h"] for x in base) and c["c"] > lvl and c["c"] > c["o"]:
                trig = c
                break
    if not trig:
        return None
    entry = trig["c"]
    if direction == "BEARISH":
        sl, side = probe["h"] + LTF_BUFFER, "SELL"
        risk = sl - entry
    else:
        sl, side = probe["l"] - LTF_BUFFER, "BUY"
        risk = entry - sl
    if not (LTF_MIN_RISK <= risk <= LTF_MAX_RISK):
        return None
    nxt = (_dt(trig["t"]) + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    return {"side": side, "dir": direction, "entry": entry, "sl": sl, "risk": risk,
            "level": lvl, "h1macd": None, "base_t": nxt}


def _open_test(pair, kind, sig, max_hours, meta):
    sign = 1 if sig["side"] == "BUY" else -1
    tps = [("TP1", sig["entry"] + sign * sig["risk"]),
           ("TP2", sig["entry"] + sign * sig["risk"] * 2),
           ("TP3", sig["entry"] + sign * sig["risk"] * 3)]
    add_trade(pair, kind, sig["side"], sig["entry"], sig["sl"], tps, meta=meta, shadow=True)
    t = active_trades[-1]
    t["test"] = True
    if sig.get("base_t"):
        t["base_t"] = sig["base_t"]               # replay from the M1 candle after the trigger
    # shorter lifetime than live trades: pretend it was created earlier
    t["created"] = time.time() - max(0.0, TRACK_MAX_HOURS - max_hours) * 3600


def _drop_tests():
    for t in active_trades[:]:
        if t.get("test"):
            log_trade(t, "TIMEOUT")
            active_trades.remove(t)


def test_signals(pair, h4, h1, m15, m5, condition, now_ts):
    """Called once per scan. Silent. Never changes a live signal."""
    if not TEST_ENABLED:
        return
    used = calls_today()
    if used >= TEST_HARD_CAP:
        _drop_tests()
        return
    if used >= TEST_CREDIT_CAP:
        return
    if sum(1 for t in active_trades if t.get("test")) >= TEST_MAX_OPEN:
        return

    def taken(kind, side, cooldown):
        key = (pair, kind, side)
        if now_ts - test_last.get(key, 0) < cooldown:
            return True
        return any(t["kind"] == kind and t["direction"] == side for t in active_trades)

    if H1RT_ENABLED:
        sig = h1_retest_signal(h4, h1, m15, m5)
        if sig and not taken("TEST H1-RETEST", sig["side"], H1RT_COOLDOWN):
            res, sup = sr_nearest(h1, h4, sig["entry"])
            _open_test(pair, "TEST H1-RETEST", sig, H1RT_MAX_HOURS,
                       {"cond": condition, "liq": "yes" if liquidity_grab(m15, sig["dir"]) else "no",
                        "res": res, "sup": sup, "h1macd": round(sig["h1macd"], 1)})
            test_last[(pair, "TEST H1-RETEST", sig["side"])] = now_ts
            print(f"{pair}: TEST H1-RETEST {sig['side']} @ {sig['entry']:.2f} (silent)")

    if LTF_ENABLED and sum(1 for t in active_trades if t.get("test")) < TEST_MAX_OPEN:
        sig = ltf_retest_signal(m15, m5, lambda: fetch(pair, "1min", 30))
        if sig and not taken("TEST LTF-RETEST", sig["side"], LTF_COOLDOWN):
            res, sup = sr_nearest(h1, h4, sig["entry"])
            _open_test(pair, "TEST LTF-RETEST", sig, LTF_MAX_HOURS,
                       {"cond": condition, "liq": "yes" if liquidity_grab(m15, sig["dir"]) else "no",
                        "res": res, "sup": sup, "h1macd": round(macd_value(h1), 1)})
            test_last[(pair, "TEST LTF-RETEST", sig["side"])] = now_ts
            print(f"{pair}: TEST LTF-RETEST {sig['side']} @ {sig['entry']:.2f} (silent)")


# ── SCAN ──────────────────────────────────────────────────
last_signal_time = {}
last_heartbeat    = 0
signal_cooldown   = 3600
sleep_notified    = False

def scan():
    global last_heartbeat, sleep_notified
    now_ts  = time.time()
    now_str = _utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ── WEEKEND CHECK ─────────────────────────────────────
    if is_weekend():
        print("Weekend — market closed. Sleeping...")
        return

    # ── DAILY SLEEP CHECK (no API calls while asleep) ─────
    if in_daily_sleep():
        if not sleep_notified:
            send(f"😴 Kpojime Bot — SLEEPING\n"
                 f"No scans until {WAKE_UTC:02d}:00 UTC.\n"
                 f"Time: {now_str}")
            sleep_notified = True
            send_daily_log()
        print(f"Daily sleep window ({SLEEP_START_UTC:02d}:00-{WAKE_UTC:02d}:00 UTC) — skipping scan")
        return
    if sleep_notified:
        send(f"☀️ Kpojime Bot — AWAKE\nScanning resumed.\nTime: {now_str}")
        sleep_notified = False

    for pair in PAIRS:
        print(f"Scanning {pair} at {now_str}")
        h4  = fetch_h4(pair)
        h1  = fetch(pair, "1h",    30)
        m15 = fetch(pair, "15min", 30)
        m5  = fetch(pair, "5min",  30)

        if not h4 or not h1 or not m15 or not m5:
            print(f"{pair}: Missing data — skip"); continue
        status["last_scan"] = _utcnow()

        condition, h4_macd, r_high, r_low = market_condition(h4, h1)

        # ── HEARTBEAT ─────────────────────────────────────
        if now_ts - last_heartbeat >= HEARTBEAT_INTERVAL:
            if condition == "CHOPPY":
                hb_msg = (
                    f"🤖 Kpojime Bot — ACTIVE\n"
                    f"Scanning: {pair}\n"
                    f"Time: {now_str}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"📊 Market: CHOPPY ⛔\n"
                    f"Range: {r_low:.2f} — {r_high:.2f}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"⛔ NO TREND TRADE until breakout\n"
                    f"🔼 Above {r_high:.2f} = BUY opportunity\n"
                    f"🔽 Below {r_low:.2f} = SELL opportunity\n"
                    f"↔️ CHOP sweep signals active (M15 range / M5 rejection)"
                )
            elif condition == "TRENDING BEARISH":
                hb_msg = (
                    f"🤖 Kpojime Bot — ACTIVE\n"
                    f"Scanning: {pair}\n"
                    f"Time: {now_str}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"📊 Market: TRENDING BEARISH 🔴\n"
                    f"H4 Momentum: Strong ({h4_macd:.1f})\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"✅ Good conditions — watch for SELL signals"
                )
            else:
                hb_msg = (
                    f"🤖 Kpojime Bot — ACTIVE\n"
                    f"Scanning: {pair}\n"
                    f"Time: {now_str}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"📊 Market: TRENDING BULLISH 🟢\n"
                    f"H4 Momentum: Strong ({h4_macd:.1f})\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"✅ Good conditions — watch for BUY signals"
                )
            send(hb_msg)
            last_heartbeat = now_ts

        # ── EARLY WARNING (read-only, never blocks a signal) ──
        early_alerts(pair, h4, h1, m15, m5, now_ts, now_str)

        # ── SILENT TEST SIGNALS (log only, never sent, never block anything) ──
        try:
            test_signals(pair, h4, h1, m15, m5, condition, now_ts)
        except Exception as e:
            print(f"{pair}: test signal error: {e}")

        # ── TIGHT-MARKET SIGNALS (separate from trend + chop) ──
        # Runs before the chop block and never skips the rest of the scan,
        # so trend and chop behave exactly as before.
        if TIGHT_ENABLED:
            try:
                for sig in tight_signals(m15, m5, macd_value(h1)):
                    is_break  = sig["kind"] == "TIGHT BREAKOUT"
                    break_key = f"{pair}_tight_break"
                    fake_key  = f"{pair}_tight_fake"
                    key       = break_key if is_break else fake_key
                    if now_ts - last_signal_time.get(key, 0) < TIGHT_COOLDOWN:
                        continue
                    # a breakout never fires after a fake-out on the same box
                    if is_break and now_ts - last_signal_time.get(fake_key, 0) < TIGHT_COOLDOWN:
                        continue
                    b = sig["box"]
                    # ── OPPOSITE-DIRECTION BLOCK ──
                    if opposite_open(pair, sig["direction"]):
                        blk_key = key + "_blocked"
                        if now_ts - last_signal_time.get(blk_key, 0) >= TIGHT_COOLDOWN:
                            last_signal_time[blk_key] = now_ts
                            log_blocked(pair, sig["kind"], sig["direction"], sig["entry"],
                                        sig["sl"], sig["tps"],
                                        meta={"cond": condition, "size": round(b["width"], 1)})
                        continue
                    emoji = "🔴" if sig["direction"] == "SELL" else "🟢"
                    tp_lines = "".join(
                        f"{name:<10}: {price:.4f}  ({pips(sig['entry'], price)} pips)\n"
                        for name, price in sig["tps"]
                    )
                    msg = (
                        f"{emoji} {sig['kind']} — {pair}\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"Direction : {sig['direction']}\n"
                        f"Entry     : {sig['entry']:.4f}\n"
                        f"Stop Loss : {sig['sl']:.4f}  ({pips(sig['entry'], sig['sl'])} pips)\n"
                        f"{tp_lines}"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"Setup        : {sig['setup']}\n"
                        f"Tight box    : {b['low']:.2f} — {b['high']:.2f}  ({b['width']:.1f} pts)\n"
                        f"R:R (TP2)    : 1:{sig['rr2']}\n"
                        f"Pip size     : {PIP_SIZE:g}\n"
                        f"Time         : {now_str}\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"⚠️ Confirm on chart. Use 2% risk."
                    )
                    send(msg)
                    last_signal_time[key] = now_ts
                    add_trade(pair, sig["kind"], sig["direction"], sig["entry"], sig["sl"], sig["tps"],
                              meta={"cond": condition, "size": round(b["width"], 1)})
                    print(f"{pair}: {sig['kind']} sent: {sig['direction']} @ {sig['entry']:.4f}")
            except Exception as e:
                print(f"{pair}: tight signal error: {e}")

        # ── CHOP SIGNAL (only when market is choppy) ──────
        if CHOP_ENABLED and condition == "CHOPPY" and now_ts >= chop_state["paused_until"]:
            chop = chop_signal(m15, m5, macd_value(h1))
            chop_key = f"{pair}_chop"
            # ── OPPOSITE-DIRECTION BLOCK ──
            if chop and opposite_open(pair, chop["direction"]):
                blk_key = chop_key + "_blocked"
                if now_ts - last_signal_time.get(blk_key, 0) >= signal_cooldown:
                    last_signal_time[blk_key] = now_ts
                    log_blocked(pair, "CHOP", chop["direction"], chop["entry"], chop["sl"],
                                [("TP1", chop["tp1"]), ("TP2", chop["tp2"])],
                                meta={"cond": condition,
                                      "size": round(chop["r_high"] - chop["r_low"], 1)})
                chop = None
            if chop and now_ts - last_signal_time.get(chop_key, 0) >= signal_cooldown:
                emoji = "🔴" if chop["direction"] == "SELL" else "🟢"
                msg = (
                    f"{emoji} CHOP SIGNAL — {pair}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"Direction : {chop['direction']}\n"
                    f"Entry     : {chop['entry']:.4f}\n"
                    f"Stop Loss : {chop['sl']:.4f}  ({pips(chop['entry'], chop['sl'])} pips)\n"
                    f"TP1 (mid) : {chop['tp1']:.4f}  ({pips(chop['entry'], chop['tp1'])} pips)\n"
                    f"TP2 (edge): {chop['tp2']:.4f}  ({pips(chop['entry'], chop['tp2'])} pips)\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"Setup        : M5 sweep of {chop['swept']} + rejection\n"
                    f"M15 Range    : {chop['r_low']:.2f} — {chop['r_high']:.2f}\n"
                    f"R:R (TP2)    : 1:{chop['rr2']}\n"
                    f"Pip size     : {PIP_SIZE:g}\n"
                    f"Time         : {now_str}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"⚠️ Range trade, not a trend trade. Take profit early, size smaller."
                )
                send(msg)
                last_signal_time[chop_key] = now_ts
                add_trade(pair, "CHOP", chop["direction"], chop["entry"], chop["sl"],
                          [("TP1", chop["tp1"]), ("TP2", chop["tp2"])],
                          meta={"cond": condition,
                                "size": round(chop["r_high"] - chop["r_low"], 1)})
                print(f"{pair}: CHOP signal sent: {chop['direction']} @ {chop['entry']:.4f}")
                continue

        h4b = bias(h4)
        print(f"{pair} H4: {h4b}")
        if h4b == "NEUTRAL":
            h4b = bias(m15)
        if h4b != bias(h1):
            print(f"{pair}: H4/H1 conflict — skip"); continue

        h1_macd = macd_value(h1)
        if h4b == "BEARISH" and h1_macd > -5:
            print(f"{pair}: H1 MACD too weak ({h1_macd:.2f}) — skip"); continue
        if h4b == "BULLISH" and h1_macd < 5:
            print(f"{pair}: H1 MACD too weak ({h1_macd:.2f}) — skip"); continue

        h1_bos, h1_wick, _ = bos(h1, h4b)
        print(f"{pair} H1 BOS: {h1_bos}")
        if not h1_bos: continue

        current_price = m5[0]["c"]
        tolerance = 15.0
        if abs(current_price - h1_wick) > tolerance:
            print(f"{pair}: Price not at BOS level - waiting"); continue

        if bias(m15) != h4b:
            print(f"{pair}: M15 not aligned"); continue
        if not macd_aligned(h1, h4b):
            print(f"{pair}: MACD conflict"); continue
        if not m5_trigger(m5, h4b):
            print(f"{pair}: M5 not triggered"); continue
        if now_ts - last_signal_time.get(pair, 0) < signal_cooldown:
            print(f"{pair}: Cooldown"); continue

        # ── TREND STACKING LIMIT (trend signals only) ─────
        if TREND_MAX_OPEN > 0:
            side = "SELL" if h4b == "BEARISH" else "BUY"
            open_same = sum(1 for t in active_trades
                            if t["kind"] == "TREND" and t["pair"] == pair
                            and t["direction"] == side and not t.get("shadow"))
            if open_same >= TREND_MAX_OPEN:
                print(f"{pair}: {open_same} trend {side} already open — skip")
                skip_key = f"{pair}_trend_skip_{side}"
                if now_ts - last_signal_time.get(skip_key, 0) >= signal_cooldown:
                    send(f"⏭ TREND {side} skipped — {pair}\n"
                         f"A trend {side} is still open, so no new one was sent.\n"
                         f"Would-be entry: {m5[0]['c']:.4f}\n"
                         f"I'll track it silently and show how it ended in the daily log.\n"
                         f"Time: {now_str}")
                    last_signal_time[skip_key] = now_ts
                    try:
                        s_entry = m5[0]["c"]
                        s_sl, s_tp1, s_tp2, s_tp3 = levels(s_entry, h4b, h1_wick if h1_wick else s_entry)
                        s_res, s_sup = sr_nearest(h1, h4, s_entry)
                        add_trade(pair, "TREND", side, s_entry, s_sl,
                                  [("TP1", s_tp1), ("TP2", s_tp2), ("TP3", s_tp3)],
                                  meta={"cond": condition,
                                        "liq": "yes" if liquidity_grab(m15, h4b) else "no",
                                        "res": s_res, "sup": s_sup,
                                        "h1macd": round(h1_macd, 1)},
                                  shadow=True)
                    except Exception as e:
                        print(f"{pair}: skipped-trade tracking error: {e}")
                continue

        # ── OPPOSITE-DIRECTION BLOCK (trend) ──────────────
        trend_side = "SELL" if h4b == "BEARISH" else "BUY"
        if opposite_open(pair, trend_side):
            blk_key = f"{pair}_trend_blocked_{trend_side}"
            if now_ts - last_signal_time.get(blk_key, 0) >= signal_cooldown:
                last_signal_time[blk_key] = now_ts
                try:
                    b_entry = m5[0]["c"]
                    b_sl, b_tp1, b_tp2, b_tp3 = levels(b_entry, h4b, h1_wick if h1_wick else b_entry)
                    b_res, b_sup = sr_nearest(h1, h4, b_entry)
                    log_blocked(pair, "TREND", trend_side, b_entry, b_sl,
                                [("TP1", b_tp1), ("TP2", b_tp2), ("TP3", b_tp3)],
                                meta={"cond": condition,
                                      "liq": "yes" if liquidity_grab(m15, h4b) else "no",
                                      "res": b_res, "sup": b_sup,
                                      "h1macd": round(h1_macd, 1)})
                except Exception as e:
                    print(f"{pair}: blocked trend tracking error: {e}")
            continue

        entry = m5[0]["c"]
        sl, tp1, tp2, tp3 = levels(entry, h4b, h1_wick if h1_wick else entry)
        direction  = "SELL" if h4b == "BEARISH" else "BUY"
        risk       = abs(entry - sl)
        rr2        = round(abs(tp2 - entry) / risk, 1) if risk > 0 else 0
        emoji      = "🔴" if direction == "SELL" else "🟢"
        liq        = "✅" if liquidity_grab(m15, h4b) else "❌"
        sr_line    = sr_summary(h1, h4, h4b, entry)

        msg = (
            f"{emoji} SIGNAL ALERT — {pair}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Direction : {direction}\n"
            f"Entry     : {entry:.4f}\n"
            f"Stop Loss : {sl:.4f}  ({pips(entry, sl)} pips)\n"
            f"TP1 (1:1) : {tp1:.4f}  ({pips(entry, tp1)} pips)\n"
            f"TP2 (1:2) : {tp2:.4f}  ({pips(entry, tp2)} pips)\n"
            f"TP3 (1:3) : {tp3:.4f}  ({pips(entry, tp3)} pips)\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"H4 Bias      : {h4b}\n"
            f"H1 BOS       : ✅\n"
            f"M15 Confirm  : ✅\n"
            f"M5 Trigger   : ✅\n"
            f"MACD         : ✅\n"
            f"Liq. Grab    : {liq}\n"
            f"{sr_line}"
            f"R:R (TP2)    : 1:{rr2}\n"
            f"Pip size     : {PIP_SIZE:g}\n"
            f"Time         : {now_str}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"⚠️ Confirm on chart. Use 2% risk."
        )
        send(msg)
        last_signal_time[pair] = now_ts
        res_pts, sup_pts = sr_nearest(h1, h4, entry)
        add_trade(pair, "TREND", direction, entry, sl,
                  [("TP1", tp1), ("TP2", tp2), ("TP3", tp3)],
                  meta={"cond": condition, "liq": "yes" if liq == "✅" else "no",
                        "res": res_pts, "sup": sup_pts, "h1macd": round(h1_macd, 1)})
        print(f"{pair}: Signal sent: {direction} @ {entry:.4f}")

# ── MAIN LOOP (started by sweep_bot.py) ───────────────────
def run_kpojime(announce=True):
    print("Kpojime Bot starting...")
    _load_trade_log()
    if announce:
        send(f"🚀 Kpojime Bot STARTED\n"
             f"Scanning {', '.join(PAIRS)} every {SCAN_INTERVAL//60} min.\n"
             f"Sleeps {SLEEP_START_UTC:02d}:00–{WAKE_UTC:02d}:00 UTC.\n"
             f"Heartbeat every 2 hours.")
    next_scan = 0
    while True:
        if time.time() >= next_scan:
            try:
                scan()
            except Exception as e:
                print(f"Error: {e}")
            next_scan = time.time() + SCAN_INTERVAL

        if is_weekend():
            for t in active_trades:
                log_trade(t, "WEEKEND")
            active_trades.clear()

        if active_trades:
            try:
                track_trades()
            except Exception as e:
                print(f"Tracker error: {e}")
            wait = min(TRACK_INTERVAL, max(1, next_scan - time.time()))
        else:
            wait = max(1, next_scan - time.time())
        time.sleep(wait)


# ======================================================================
# PART 3 - LAUNCHER
# ======================================================================

PING_INTERVAL = 600


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Kpojime Bot alive")

    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def start_self_ping():
    url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RENDER_URL")
    if not url:
        print("No RENDER_EXTERNAL_URL set: self-ping disabled")
        return

    def loop():
        while True:
            time.sleep(PING_INTERVAL)
            try:
                requests.get(url, timeout=10)
                print("Self-ping sent")
            except Exception as e:
                print(f"Ping error: {e}")

    threading.Thread(target=loop, daemon=True).start()


def alert(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=10)
    except Exception as e:
        print(f"Alert error: {e}")


def supervise(name, fn):
    """Run one bot forever. If it crashes, tell Telegram and restart it
    without touching the other bot."""
    first = True
    while True:
        try:
            fn(first)
        except Exception as e:
            print(f"{name} crashed: {e}")
            alert(f"⚠️ {name} crashed and is restarting: {str(e)[:100]}")
        first = False
        time.sleep(60)


HEARTBEAT_HOURS = float(os.getenv("HEARTBEAT_HOURS", "6"))


def build_heartbeat():
    now = _utcnow()
    if is_weekend():
        mode = "sleeping (market closed)"
    elif in_daily_sleep():
        mode = f"sleeping (until {WAKE_UTC:02d}:00 UTC)"
    else:
        mode = "scanning"
    last = f"{status['last_scan']:%Y-%m-%d %H:%M} UTC" if status["last_scan"] else "none yet"
    return (
        f"💓 Kpojime Bot alive\n"
        f"Time: {now:%Y-%m-%d %H:%M} UTC\n"
        f"Mode: {mode}\n"
        f"Last scan: {last}\n"
        f"Data API: {status['api']}\n"
        f"Open trades tracked: {len([t for t in active_trades if not t.get('shadow')])}\n"
        f"Twelve Data calls today: {calls_today()}"
    )


def start_heartbeat():
    """Independent of the scan loop: still sends while the bot sleeps or the
    data API is failing."""
    def loop():
        while True:
            time.sleep(HEARTBEAT_HOURS * 3600)
            try:
                send(build_heartbeat())
            except Exception as e:
                print(f"Heartbeat error: {e}")

    threading.Thread(target=loop, daemon=True).start()


def main():
    print("Launcher starting: Kpojime bot (trend + chop + tight)")
    start_health_server()
    start_self_ping()
    start_heartbeat()
    threading.Thread(target=supervise, args=("Kpojime bot", run_kpojime), daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
