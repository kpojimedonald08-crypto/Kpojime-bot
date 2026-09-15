import os
import time
import requests
from datetime import datetime

# ── ENV VARS ──────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TWELVEDATA_KEY   = os.environ.get("TWELVEDATA_KEY")
SCAN_INTERVAL    = int(os.environ.get("SCAN_INTERVAL", "300"))   # 5 min default
PAIR             = "XAU/USD"
HEARTBEAT_INTERVAL = 7200   # 2 hours in seconds

# ── TELEGRAM ──────────────────────────────────────────────
def send(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

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
        candles = [
            {
                "o": float(v["open"]),
                "h": float(v["high"]),
                "l": float(v["low"]),
                "c": float(v["close"]),
            }
            for v in d["values"]
        ]
        return candles   # index 0 = most recent
    except Exception as e:
        print(f"Fetch exception {symbol} {interval}: {e}")
        return None

# ── BIAS (trend direction) ────────────────────────────────
def bias(candles):
    """
    Looks at last 10 candles.
    BEARISH if recent highs and lows are making lower highs and lower lows.
    BULLISH if making higher highs and higher lows.
    """
    if not candles or len(candles) < 10:
        return "NEUTRAL"
    highs = [c["h"] for c in candles[:10]]
    lows  = [c["l"] for c in candles[:10]]
    # Compare first half vs second half averages
    avg_h_recent = sum(highs[:5]) / 5
    avg_h_older  = sum(highs[5:]) / 5
    avg_l_recent = sum(lows[:5])  / 5
    avg_l_older  = sum(lows[5:])  / 5
    if avg_h_recent < avg_h_older and avg_l_recent < avg_l_older:
        return "BEARISH"
    if avg_h_recent > avg_h_older and avg_l_recent > avg_l_older:
        return "BULLISH"
    return "NEUTRAL"

# ── BOS (break of structure) ──────────────────────────────
def bos(candles, direction):
    """
    Checks if the most recent closed candle broke structure.
    BEARISH BOS: close breaks below the lowest low of candles 2-6.
    BULLISH BOS: close breaks above the highest high of candles 2-6.
    """
    if not candles or len(candles) < 7:
        return False, None, None
    latest = candles[0]
    structure = candles[2:7]   # candles before the breakout
    if direction == "BEARISH":
        structure_low = min(c["l"] for c in structure)
        if latest["c"] < structure_low:
            return True, latest["l"], structure_low   # bos=True, wick, level
    if direction == "BULLISH":
        structure_high = max(c["h"] for c in structure)
        if latest["c"] > structure_high:
            return True, latest["h"], structure_high
    return False, None, None

# ── RETEST CHECK ──────────────────────────────────────────
def retest(candles, direction, bos_level):
    """
    After BOS, checks if price pulled back toward the broken level.
    Within 20% of distance between current price and BOS level = retest happening.
    """
    if not candles or bos_level is None:
        return False
    current = candles[0]["c"]
    if direction == "BEARISH":
        # Price dropped then bounced back up toward bos_level
        retest_zone_top = bos_level
        retest_zone_bot = bos_level - (bos_level * 0.001)   # 0.1% band
        return retest_zone_bot <= current <= retest_zone_top
    if direction == "BULLISH":
        retest_zone_bot = bos_level
        retest_zone_top = bos_level + (bos_level * 0.001)
        return retest_zone_bot <= current <= retest_zone_top
    return False

# ── M5 BOS TRIGGER ────────────────────────────────────────
def m5_trigger(candles, direction):
    """
    M5 confirmation: requires a clean BOS on M5 in the same direction.
    """
    if not candles or len(candles) < 7:
        return False
    latest = candles[0]
    structure = candles[2:7]
    if direction == "BEARISH":
        structure_low = min(c["l"] for c in structure)
        return latest["c"] < structure_low
    if direction == "BULLISH":
        structure_high = max(c["h"] for c in structure)
        return latest["c"] > structure_high
    return False

# ── MACD CHECK ────────────────────────────────────────────
def macd_aligned(candles, direction):
    """
    Simple MACD approximation using EMA12 - EMA26.
    Checks if MACD is negative (bearish) or positive (bullish).
    """
    if not candles or len(candles) < 26:
        return True   # not enough data, don't block signal
    closes = [c["c"] for c in candles]

    def ema(data, period):
        k = 2 / (period + 1)
        ema_val = sum(data[-period:]) / period
        for price in reversed(data[:-period]):
            ema_val = price * k + ema_val * (1 - k)
        return ema_val

    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd  = ema12 - ema26

    if direction == "BEARISH":
        return macd < 0
    if direction == "BULLISH":
        return macd > 0
    return True

# ── CALCULATE SL AND TPs ──────────────────────────────────
def levels(entry, direction, bos_wick):
    """
    SL: beyond the BOS candle wick + 3 point buffer.
    TP1: 1:1, TP2: 1:2, TP3: 1:3
    """
    buffer = 3.0
    if direction == "BEARISH":
        sl    = bos_wick + buffer
        risk  = sl - entry
        tp1   = entry - risk
        tp2   = entry - (risk * 2)
        tp3   = entry - (risk * 3)
    else:
        sl    = bos_wick - buffer
        risk  = entry - sl
        tp1   = entry + risk
        tp2   = entry + (risk * 2)
        tp3   = entry + (risk * 3)
    return sl, tp1, tp2, tp3

# ── MAIN SCAN ─────────────────────────────────────────────
last_signal_time = 0
last_heartbeat   = 0
signal_cooldown  = 3600   # don't repeat same signal within 1 hour

def scan():
    global last_signal_time, last_heartbeat

    now_ts = time.time()
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ── Heartbeat ──
    if now_ts - last_heartbeat >= HEARTBEAT_INTERVAL:
        send(
            f"🤖 Kpojime Bot — ACTIVE\n"
            f"Scanning: XAU/USD\n"
            f"No setup yet — market not ready.\n"
            f"Time: {now_str}"
        )
        last_heartbeat = now_ts

    # ── Fetch all timeframes ──
    print(f"Scanning {PAIR} at {now_str}")
    h4  = fetch(PAIR, "4h",    30)
    h1  = fetch(PAIR, "1h",    30)
    m15 = fetch(PAIR, "15min", 30)
    m5  = fetch(PAIR, "5min",  30)

    if not h4 or not h1 or not m15 or not m5:
        print(f"{PAIR}: missing data, skipping")
        return

    # ── Step 1: H4 bias ──
    h4b = bias(h4)
    print(f"H4 bias: {h4b}")
    if h4b == "NEUTRAL":
        print("H4 neutral — no trade")
        return

    # ── Step 2: H1 BOS ──
    h1_bos, h1_wick, h1_level = bos(h1, h4b)
    print(f"H1 BOS: {h1_bos}")
    if not h1_bos:
        print("No H1 BOS — no trade")
        return

    # ── Step 3: M15 confirmation ──
    m15b = bias(m15)
    m15_confirmed = (m15b == h4b)
    print(f"M15 bias: {m15b} | Confirmed: {m15_confirmed}")
    if not m15_confirmed:
        print("M15 not aligned — no trade")
        return

    # ── Step 4: MACD aligned on H1 ──
    macd_ok = macd_aligned(h1, h4b)
    print(f"MACD aligned: {macd_ok}")
    if not macd_ok:
        print("MACD conflict — no trade")
        return

    # ── Step 5: M5 trigger ──
    m5_ok = m5_trigger(m5, h4b)
    print(f"M5 trigger: {m5_ok}")
    if not m5_ok:
        print("M5 not triggered — waiting")
        return

    # ── Step 6: Cooldown check ──
    if now_ts - last_signal_time < signal_cooldown:
        print("Signal cooldown active — skipping")
        return

    # ── All conditions met — build signal ──
    entry  = m5[0]["c"]
    bos_wick_used = h1_wick if h1_wick else entry
    direction = "SELL" if h4b == "BEARISH" else "BUY"
    sl, tp1, tp2, tp3 = levels(entry, h4b, bos_wick_used)
    risk = abs(entry - sl)
    rr2  = round(abs(tp2 - entry) / risk, 1) if risk > 0 else 0

    emoji = "🔴" if direction == "SELL" else "🟢"

    msg = (
        f"{emoji} SIGNAL ALERT — XAU/USD\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Direction : {direction}\n"
        f"Entry     : {entry:.2f}\n"
        f"Stop Loss : {sl:.2f}\n"
        f"TP1 (1:1) : {tp1:.2f}\n"
        f"TP2 (1:2) : {tp2:.2f}\n"
        f"TP3 (1:3) : {tp3:.2f}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"H4 Bias   : {h4b}\n"
        f"H1 BOS    : ✅\n"
        f"M15 Confirm: ✅\n"
        f"M5 Trigger : ✅\n"
        f"MACD      : ✅\n"
        f"R:R (TP2) : 1:{rr2}\n"
        f"Time      : {now_str}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ Confirm on chart. Use 2% risk."
    )

    send(msg)
    last_signal_time = now_ts
    print(f"Signal sent: {direction} @ {entry:.2f}")


# ── ENTRY POINT ───────────────────────────────────────────
if __name__ == "__main__":
    print("Kpojime Bot starting...")
    send(
        f"🚀 Kpojime Bot STARTED\n"
        f"Scanning XAU/USD every {SCAN_INTERVAL//60} minutes.\n"
        f"Heartbeat every 2 hours.\n"
        f"Waiting for setup..."
    )
    while True:
        try:
            scan()
        except Exception as e:
            print(f"Error: {e}")
        print(f"Sleeping {SCAN_INTERVAL//60} min...")
        time.sleep(SCAN_INTERVAL)
