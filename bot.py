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
            {"o": float(v["open"]), "h": float(v["high"]),
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
def chop_signal(m15, m5):
    """
    Range = high/low of the M15 candles before the most recent 3 (45 min).
    Signal when an M5 candle in the last 3 wicked beyond a range edge and the
    latest M5 closed back inside the range with a rejection candle.
    Uses candles already fetched for the BOS scan, so no extra API calls.
    Returns a dict or None.
    """
    if not m15 or len(m15) < CHOP_LOOKBACK + 3 or not m5 or len(m5) < 3:
        return None

    rng      = m15[3:3 + CHOP_LOOKBACK]
    r_high   = max(c["h"] for c in rng)
    r_low    = min(c["l"] for c in rng)
    width    = r_high - r_low
    if width < CHOP_MIN_RANGE or width > CHOP_MAX_RANGE:
        return None
    mid      = (r_high + r_low) / 2

    latest   = m5[0]
    recent   = m5[:3]
    hi_wick  = max(c["h"] for c in recent)
    lo_wick  = min(c["l"] for c in recent)
    entry    = latest["c"]

    swept_high = hi_wick > r_high
    swept_low  = lo_wick < r_low
    if swept_high and swept_low:
        return None  # ambiguous, skip

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
        if CHOP_ENABLED and condition == "CHOPPY":
            chop = chop_signal(m15, m5)
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
    while True:
        try:
            scan()
        except Exception as e:
            print(f"Error: {e}")
        print(f"Sleeping {SCAN_INTERVAL//60} min...")
        time.sleep(SCAN_INTERVAL)
