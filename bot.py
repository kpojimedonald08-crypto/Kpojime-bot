import os
import time
import threading
import requests
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── ENV VARS ──────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")
TWELVEDATA_KEY     = os.environ.get("TWELVEDATA_KEY")
SCAN_INTERVAL      = int(os.environ.get("SCAN_INTERVAL", "300"))
RENDER_URL         = os.environ.get("RENDER_URL", "")
PAIRS              = ["XAU/USD"]
HEARTBEAT_INTERVAL = 7200
PING_INTERVAL      = 600

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

# ── TRADE TRACKER SETTINGS ────────────────────────────────
# While a signal is open the bot polls M1 candles (1 credit per poll, per pair)
# and alerts on TP1 / TP2 / TP3 / SL. Polling stops when the trade closes.
TRACK_INTERVAL  = int(os.environ.get("TRACK_INTERVAL", "120"))    # seconds between polls
TRACK_MAX_HOURS = float(os.environ.get("TRACK_MAX_HOURS", "4"))   # stop tracking after this
BE_AFTER_TP1    = os.environ.get("BE_AFTER_TP1", "1") == "1"      # treat entry as stop after TP1

# ── KEEP-ALIVE SERVER ─────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Kpojime Bot running")
    def log_message(self, *a): pass

def run_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), H).serve_forever()

def self_ping():
    while True:
        time.sleep(PING_INTERVAL)
        if RENDER_URL:
            try:
                requests.get(RENDER_URL, timeout=10)
                print("Self-ping sent")
            except Exception as e:
                print(f"Ping error: {e}")

# ── TELEGRAM ──────────────────────────────────────────────
def send(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

# ── WEEKEND CHECK ─────────────────────────────────────────
def is_weekend():
    now = datetime.utcnow()
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
    now = now or datetime.utcnow()
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
        r = requests.get(url, timeout=15)
        d = r.json()
        if d.get("status") == "error" or "values" not in d:
            print(f"Fetch error {symbol} {interval}: {d.get('message','no data')}")
            return None
        return [
            {"t": v.get("datetime", ""),
             "o": float(v["open"]), "h": float(v["high"]),
             "l": float(v["low"]),  "c": float(v["close"])}
            for v in d["values"]
        ]
    except Exception as e:
        print(f"Fetch exception {symbol} {interval}: {e}")
        return None

# ── EMA ───────────────────────────────────────────────────
def ema(data, period):
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
    macd = ema(closes, 12) - ema(closes, 26)
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
    return ema(closes, 12) - ema(closes, 26)

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
    macd = ema(closes, 12) - ema(closes, 26)
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

# ── S/R WARNING ───────────────────────────────────────────
def sr_warning(candles, direction, entry):
    if not candles or len(candles) < 20:
        return ""
    highs = [c["h"] for c in candles[:20]]
    lows  = [c["l"] for c in candles[:20]]
    resistance = max(highs)
    support    = min(lows)
    if direction == "BULLISH" and (resistance - entry) < 20:
        return f"⚠️ Near resistance at {resistance:.2f}"
    if direction == "BEARISH" and (entry - support) < 20:
        return f"⚠️ Near support at {support:.2f}"
    return ""

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

# ── TRADE TRACKER ─────────────────────────────────────────
active_trades = []
chop_state = {"losses": 0, "paused_until": 0}

def add_trade(pair, kind, direction, entry, sl, tps):
    """tps = [("TP1", price), ("TP2", price), ...] in order."""
    active_trades.append({
        "pair": pair, "kind": kind, "direction": direction,
        "entry": entry, "sl": sl, "risk": abs(entry - sl),
        "tps": tps, "hit": 0, "base_t": None, "created": time.time(),
    })
    print(f"{pair}: tracking {kind} {direction} @ {entry:.4f}")

def _r_mult(trade, price):
    return abs(price - trade["entry"]) / trade["risk"] if trade["risk"] > 0 else 0

def _reached(trade, c, price):
    return c["h"] >= price if trade["direction"] == "BUY" else c["l"] <= price

def _stopped(trade, c, level):
    return c["l"] <= level if trade["direction"] == "BUY" else c["h"] >= level

def process_candle(t, c):
    """Returns True when the trade is finished."""
    head = f"{t['pair']} {t['direction']} ({t['kind']})"
    be   = BE_AFTER_TP1 and t["hit"] >= 1
    stop = t["entry"] if be else t["sl"]

    # Stop is checked first, so a candle touching both stop and target counts
    # as a loss (conservative).
    if _stopped(t, c, stop):
        if be:
            send(f"🟡 BREAKEVEN — {head}\n"
                 f"Price back at entry {t['entry']:.4f} after TP1.\n"
                 f"If you moved SL to entry, you're out at ~0R.")
        else:
            send(f"❌ STOP LOSS HIT — {head}\n"
                 f"SL {t['sl']:.4f} touched (-1R). Trade closed.")
            if t["kind"] == "CHOP":
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
        if t["kind"] == "CHOP":
            chop_state["losses"] = 0      # a win resets the loss streak
        final = t["hit"] == len(t["tps"])
        if final:
            tail = "🏁 Final target reached. Trade complete."
        elif t["hit"] == 1 and BE_AFTER_TP1:
            tail = f"👉 Move SL to entry ({t['entry']:.4f}) to protect the trade."
        else:
            tail = "👉 Consider closing part or trailing your stop."
        send(f"✅ {name} HIT — {head}\n"
             f"Price reached {price:.4f} (+{_r_mult(t, price):.1f}R)\n{tail}")
        if final:
            return True
    return False

def track_trades():
    if not active_trades:
        return
    now_ts = time.time()

    for t in active_trades[:]:
        if now_ts - t["created"] > TRACK_MAX_HOURS * 3600:
            send(f"⏱ Tracking stopped — {t['pair']} {t['direction']} ({t['kind']})\n"
                 f"Open for over {TRACK_MAX_HOURS:g}h with no final result. Manage it manually.")
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

# ── SCAN ──────────────────────────────────────────────────
last_signal_time = {}
last_heartbeat    = 0
signal_cooldown   = 3600
sleep_notified    = False

def scan():
    global last_heartbeat, sleep_notified
    now_ts  = time.time()
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

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
        print(f"Daily sleep window ({SLEEP_START_UTC:02d}:00-{WAKE_UTC:02d}:00 UTC) — skipping scan")
        return
    if sleep_notified:
        send(f"☀️ Kpojime Bot — AWAKE\nScanning resumed.\nTime: {now_str}")
        sleep_notified = False

    for pair in PAIRS:
        print(f"Scanning {pair} at {now_str}")
        h4  = fetch(pair, "4h",    30)
        h1  = fetch(pair, "1h",    30)
        m15 = fetch(pair, "15min", 30)
        m5  = fetch(pair, "5min",  30)

        if not h4 or not h1 or not m15 or not m5:
            print(f"{pair}: Missing data — skip"); continue

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

        # ── CHOP SIGNAL (only when market is choppy) ──────
        if CHOP_ENABLED and condition == "CHOPPY" and now_ts >= chop_state["paused_until"]:
            chop = chop_signal(m15, m5, macd_value(h1))
            chop_key = f"{pair}_chop"
            if chop and now_ts - last_signal_time.get(chop_key, 0) >= signal_cooldown:
                emoji = "🔴" if chop["direction"] == "SELL" else "🟢"
                msg = (
                    f"{emoji} CHOP SIGNAL — {pair}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"Direction : {chop['direction']}\n"
                    f"Entry     : {chop['entry']:.4f}\n"
                    f"Stop Loss : {chop['sl']:.4f}\n"
                    f"TP1 (mid) : {chop['tp1']:.4f}\n"
                    f"TP2 (edge): {chop['tp2']:.4f}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"Setup        : M5 sweep of {chop['swept']} + rejection\n"
                    f"M15 Range    : {chop['r_low']:.2f} — {chop['r_high']:.2f}\n"
                    f"R:R (TP2)    : 1:{chop['rr2']}\n"
                    f"Time         : {now_str}\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"⚠️ Range trade, not a trend trade. Take profit early, size smaller."
                )
                send(msg)
                last_signal_time[chop_key] = now_ts
                add_trade(pair, "CHOP", chop["direction"], chop["entry"], chop["sl"],
                          [("TP1", chop["tp1"]), ("TP2", chop["tp2"])])
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

        entry = m5[0]["c"]
        sl, tp1, tp2, tp3 = levels(entry, h4b, h1_wick if h1_wick else entry)
        direction  = "SELL" if h4b == "BEARISH" else "BUY"
        risk       = abs(entry - sl)
        rr2        = round(abs(tp2 - entry) / risk, 1) if risk > 0 else 0
        emoji      = "🔴" if direction == "SELL" else "🟢"
        liq        = "✅" if liquidity_grab(m15, h4b) else "❌"
        sr_warn    = sr_warning(h1, h4b, entry)
        sr_line    = f"S/R Warning  : {sr_warn}\n" if sr_warn else ""

        msg = (
            f"{emoji} SIGNAL ALERT — {pair}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Direction : {direction}\n"
            f"Entry     : {entry:.4f}\n"
            f"Stop Loss : {sl:.4f}\n"
            f"TP1 (1:1) : {tp1:.4f}\n"
            f"TP2 (1:2) : {tp2:.4f}\n"
            f"TP3 (1:3) : {tp3:.4f}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"H4 Bias      : {h4b}\n"
            f"H1 BOS       : ✅\n"
            f"M15 Confirm  : ✅\n"
            f"M5 Trigger   : ✅\n"
            f"MACD         : ✅\n"
            f"Liq. Grab    : {liq}\n"
            f"{sr_line}"
            f"R:R (TP2)    : 1:{rr2}\n"
            f"Time         : {now_str}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"⚠️ Confirm on chart. Use 2% risk."
        )
        send(msg)
        last_signal_time[pair] = now_ts
        add_trade(pair, "TREND", direction, entry, sl,
                  [("TP1", tp1), ("TP2", tp2), ("TP3", tp3)])
        print(f"{pair}: Signal sent: {direction} @ {entry:.4f}")

# ── MAIN ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("Kpojime Bot starting...")
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=self_ping,  daemon=True).start()
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
