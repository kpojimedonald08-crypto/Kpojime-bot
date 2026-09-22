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
PAIR               = "XAU/USD"
HEARTBEAT_INTERVAL = 7200
PING_INTERVAL      = 600   # ping self every 10 min to stay awake

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

# ── BIAS ──────────────────────────────────────────────────
def bias(candles):
    
        if not candles or len(candles) < 26:
            return "NEUTRAL"
        closes = [c["c"] for c in candles]
        def ema(data, period):
            k = 2 / (period + 1)
            val = sum(data[-period:]) / period
            for p in reversed(data[:-period]):
                val = p * k + val * (1 - k)
            return val
        macd = ema(closes, 12) - ema(closes, 26)
        if macd > 0:
            return "BULLISH"
        elif macd < 0:
            return "BEARISH"
        return "NEUTRAL"

# ── 
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

# ── MACD ──────────────────────────────────────────────────
def macd_aligned(candles, direction):
    if not candles or len(candles) < 26:
        return True
    closes = [c["c"] for c in candles]
    def ema(data, period):
        k = 2 / (period + 1)
        val = sum(data[-period:]) / period
        for p in reversed(data[:-period]):
            val = p * k + val * (1 - k)
        return val
    macd = ema(closes, 12) - ema(closes, 26)
    if direction == "BEARISH": return macd < 0
    if direction == "BULLISH": return macd > 0
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

# ── SCAN ──────────────────────────────────────────────────
last_signal_time = 0
last_heartbeat   = 0
signal_cooldown  = 3600

def scan():
    global last_signal_time, last_heartbeat
    now_ts  = time.time()
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if now_ts - last_heartbeat >= HEARTBEAT_INTERVAL:
        send(f"🤖 Kpojime Bot — ACTIVE\nScanning: XAU/USD\nNo setup yet — market not ready.\nTime: {now_str}")
        last_heartbeat = now_ts

    print(f"Scanning {PAIR} at {now_str}")
    h4  = fetch(PAIR, "4h",    30)
    h1  = fetch(PAIR, "1h",    30)
    m15 = fetch(PAIR, "15min", 30)
    m5  = fetch(PAIR, "5min",  30)

    if not h4 or not h1 or not m15 or not m5:
        print("Missing data — skip"); return

    h4b = bias(h4)
    print(f"H4: {h4b}")
    if h4b == "NEUTRAL": h4b = bias(m15)
    if h4b != bias(h1): return

        
    
        
        
        
        
        
    h1_bos, h1_wick, _ = bos(h1, h4b)
    print(f"H1 BOS: {h1_bos}")
    if not h1_bos: return
    current_price = m5[0]["c"]
        tolerance = 5.0
        if abs(current_price - h1_wick) > tolerance:
            print("Price not at BOS level - waiting")
            return
    if bias(m15) != h4b: print("M15 not aligned"); return
    if not macd_aligned(h1, h4b): print("MACD conflict"); return
    if not m5_trigger(m5, h4b): print("M5 not triggered"); return
    if now_ts - last_signal_time < signal_cooldown: print("Cooldown"); return

    entry = m5[0]["c"]
    sl, tp1, tp2, tp3 = levels(entry, h4b, h1_wick if h1_wick else entry)
    direction = "SELL" if h4b == "BEARISH" else "BUY"
    risk = abs(entry - sl)
    rr2  = round(abs(tp2 - entry) / risk, 1) if risk > 0 else 0
    emoji = "🔴" if direction == "SELL" else "🟢"
    highs = [c["h"] for c in h1[:20]]
        lows = [c["l"] for c in h1[:20]]
        resistance = max(highs)
        support = min(lows)
        sr_warning = ""
        if h4b == "BULLISH" and (resistance - entry) < 20:
            sr_warning = f"\n⚠️ APPROACHING RESISTANCE at {resistance:.2f}"
        if h4b == "BEARISH" and (entry - support) < 20:
            sr_warning = f"\n⚠️ APPROACHING SUPPORT at {support:.2f}"
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
       f"{sr_warning}\n"
        f"⚠️ Confirm on chart. Use 2% risk."
    )
    send(msg)
    last_signal_time = now_ts
    print(f"Signal sent: {direction} @ {entry:.2f}")

# ── MAIN ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("Kpojime Bot starting...")
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=self_ping,  daemon=True).start()
    send(f"🚀 Kpojime Bot STARTED\nScanning XAU/USD every {SCAN_INTERVAL//60} min.\nHeartbeat every 2 hours.")
    while True:
        try:
            scan()
        except Exception as e:
            print(f"Error: {e}")
        print(f"Sleeping {SCAN_INTERVAL//60} min...")
        time.sleep(SCAN_INTERVAL)
