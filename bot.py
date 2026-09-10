import os
import time
import requests
from datetime import datetime

# ── Config from environment variables ──────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TWELVEDATA_KEY = os.environ.get("TWELVEDATA_KEY")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "900"))  # 15 minutes default

PAIRS = ["XAU/USD", "EUR/USD"]

# ── Twelve Data fetch ───────────────────────────────────────────────────
def fetch_candles(symbol, interval, count=10):
    try:
        url = (
            f"https://api.twelvedata.com/time_series"
            f"?symbol={symbol}&interval={interval}"
            f"&outputsize={count}&apikey={TWELVEDATA_KEY}&format=JSON"
        )
        r = requests.get(url, timeout=15)
        data = r.json()
        if data.get("status") == "error" or "values" not in data:
            print(f"  API error for {symbol} {interval}: {data.get('message','')}")
            return None
        return [
            {
                "open":  float(v["open"]),
                "high":  float(v["high"]),
                "low":   float(v["low"]),
                "close": float(v["close"]),
            }
            for v in data["values"]
        ]
    except Exception as e:
        print(f"  Fetch error {symbol} {interval}: {e}")
        return None

# ── Analysis helpers ────────────────────────────────────────────────────
def detect_bias(candles):
    if not candles or len(candles) < 5:
        return "NEUTRAL"
    highs = [c["high"] for c in candles[:5]]
    lows  = [c["low"]  for c in candles[:5]]
    if highs[0] > highs[2] and highs[1] > highs[3] and lows[0] > lows[2] and lows[1] > lows[3]:
        return "BULLISH"
    if highs[0] < highs[2] and highs[1] < highs[3] and lows[0] < lows[2] and lows[1] < lows[3]:
        return "BEARISH"
    return "NEUTRAL"

def detect_bos(candles, direction):
    if not candles or len(candles) < 4:
        return False
    c0, _, c2, c3 = candles[0], candles[1], candles[2], candles[3]
    if direction == "BEARISH":
        return c0["close"] < min(c2["low"], c3["low"])
    if direction == "BULLISH":
        return c0["close"] > max(c2["high"], c3["high"])
    return False

def dxy_ok(dxy_bias, pair_bias, pair):
    if dxy_bias == "NEUTRAL":
        return True
    quoted = ["XAU/USD", "EUR/USD"]
    if pair in quoted:
        if dxy_bias == "BULLISH" and pair_bias == "BULLISH":
            return False
        if dxy_bias == "BEARISH" and pair_bias == "BEARISH":
            return False
    return True

# ── Telegram sender ─────────────────────────────────────────────────────
def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"Telegram error: {r.text}")
    except Exception as e:
        print(f"Telegram send failed: {e}")

# ── Main scan ───────────────────────────────────────────────────────────
def scan():
    now = datetime.utcnow().strftime("%H:%M UTC")
    print(f"\n[{now}] Starting scan...")

    # DXY bias
    dxy_candles = fetch_candles("DXY", "1day", 8)
    dxy_bias = detect_bias(dxy_candles)
    print(f"  DXY Bias: {dxy_bias}")

    alerts = []

    for pair in PAIRS:
        print(f"  Scanning {pair}...")

        d1  = fetch_candles(pair, "1day", 10)
        h4  = fetch_candles(pair, "4h",   10)
        h1  = fetch_candles(pair, "1h",   10)
        m15 = fetch_candles(pair, "15min",10)

        if not h4 or not h1:
            print(f"    No data for {pair}")
            continue

        d1_bias  = detect_bias(d1)
        h4_bias  = detect_bias(h4)
        h1_bos   = detect_bos(h1, h4_bias)
        m15_conf = detect_bias(m15) == h4_bias if m15 else False
        dxy_good = dxy_ok(dxy_bias, h4_bias, pair)
        d1_align = d1_bias == "NEUTRAL" or d1_bias == h4_bias
        price    = h1[0]["close"]

        print(f"    D1:{d1_bias} H4:{h4_bias} H1_BOS:{h1_bos} M15:{m15_conf} DXY_OK:{dxy_good} D1_OK:{d1_align}")

        # Signal logic
        if not dxy_good or not d1_align:
            signal = "CONFLICT"
        elif h1_bos and m15_conf:
            signal = "WATCH"
        else:
            signal = "WAIT"

        # Only alert on WATCH (potential setup forming)
        if signal == "WATCH":
            direction = "🟢 BUY" if h4_bias == "BULLISH" else "🔴 SELL"
            msg = (
                f"<b>⚡ SETUP ALERT — {pair}</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"Signal: <b>WATCH</b> {direction}\n"
                f"Price: <b>{price:.5f}</b>\n\n"
                f"📊 <b>Analysis</b>\n"
                f"D1 Bias: {d1_bias}\n"
                f"H4 Bias: {h4_bias}\n"
                f"H1 BOS: {'✅' if h1_bos else '❌'}\n"
                f"M15 Confirm: {'✅' if m15_conf else '❌'}\n"
                f"DXY OK: {'✅' if dxy_good else '❌'}\n"
                f"D1 Aligned: {'✅' if d1_align else '❌'}\n\n"
                f"⏰ {now}\n"
                f"⚠️ Check M5 for entry trigger. Apply 2% risk rule."
            )
            alerts.append(msg)

        elif signal == "CONFLICT":
            print(f"    {pair} CONFLICT — skipping alert")

    if alerts:
        for alert in alerts:
            send_telegram(alert)
    else:
        print("  No setups found this scan.")

# ── Keep-alive web server (required by Render free tier) ────────────────
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Kpojime Bot Running")
    def log_message(self, format, *args):
        pass  # suppress server logs

def run_server():
    server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), PingHandler)
    server.serve_forever()

# ── Entry point ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Kpojime BOS Scanner Bot starting...")
    print(f"Pairs: {PAIRS}")
    print(f"Scan interval: {SCAN_INTERVAL}s ({SCAN_INTERVAL//60} min)")

    # Start web server in background
    Thread(target=run_server, daemon=True).start()
    print("Web server started on port 8080")

    # Send startup message
    send_telegram(
        "🤖 <b>Kpojime BOS Scanner Started</b>\n"
        f"Scanning XAU/USD and EUR/USD every {SCAN_INTERVAL//60} minutes.\n"
        "You will be alerted when a setup appears."
    )

    # Run scan loop
    while True:
        try:
            scan()
        except Exception as e:
            print(f"Scan error: {e}")
        print(f"  Sleeping {SCAN_INTERVAL//60} minutes...")
        time.sleep(SCAN_INTERVAL)
