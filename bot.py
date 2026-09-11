import os
import time
import requests
from datetime import datetime

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TWELVEDATA_KEY = os.environ.get("TWELVEDATA_KEY")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "900"))
PAIRS = ["XAU/USD", "EUR/USD"]

def fetch(symbol, interval, count=10):
    try:
        url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&outputsize={count}&apikey={TWELVEDATA_KEY}&format=JSON"
        r = requests.get(url, timeout=15)
        d = r.json()
        if d.get("status") == "error" or "values" not in d:
            return None
        return [{"o":float(v["open"]),"h":float(v["high"]),"l":float(v["low"]),"c":float(v["close"])} for v in d["values"]]
    except:
        return None

def bias(candles):
    if not candles or len(candles) < 5:
        return "NEUTRAL"
    h = [c["h"] for c in candles[:5]]
    l = [c["l"] for c in candles[:5]]
    if h[0]>h[2] and h[1]>h[3] and l[0]>l[2] and l[1]>l[3]:
        return "BULLISH"
    if h[0]<h[2] and h[1]<h[3] and l[0]<l[2] and l[1]<l[3]:
        return "BEARISH"
    return "NEUTRAL"

def bos(candles, direction):
    if not candles or len(candles) < 4:
        return False
    c0,c1,c2,c3 = candles[0],candles[1],candles[2],candles[3]
    if direction == "BEARISH":
        return c0["c"] < min(c2["l"], c3["l"])
    if direction == "BULLISH":
        return c0["c"] > max(c2["h"], c3["h"])
    return False

def dxy_ok(db, pb, pair):
    if db == "NEUTRAL":
        return True
    quoted = ["XAU/USD","EUR/USD"]
    if pair in quoted:
        if db=="BULLISH" and pb=="BULLISH": return False
        if db=="BEARISH" and pb=="BEARISH": return False
    return True

def send(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"}, timeout=10)
    except Exception as e:
        print(f"Send error: {e}")

def scan():
    now = datetime.utcnow().strftime("%H:%M UTC")
    print(f"[{now}] Scanning...")
    dxy = fetch("DXY", "1day", 8)
    db = bias(dxy)
    print(f"DXY: {db}")
    for pair in PAIRS:
        h4 = fetch(pair, "4h", 10)
        h1 = fetch(pair, "1h", 10)
        m15 = fetch(pair, "15min", 10)
        if not h4 or not h1:
            print(f"{pair}: no data")
            continue
        h4b = bias(h4)
        h1b = bos(h1, h4b)
        m15b = bias(m15) == h4b if m15 else False
        dok = dxy_ok(db, h4b, pair)
        price = h1[0]["c"]
        print(f"{pair}: H4={h4b} BOS={h1b} M15={m15b} DXY={dok}")
        if h1b and m15b and dok:
            direction = "BUY" if h4b=="BULLISH" else "SELL"
            msg = (
                f"SETUP ALERT - {pair}\n"
                f"Signal: WATCH - {direction}\n"
                f"Price: {price:.5f}\n"
                f"H4 Bias: {h4b}\n"
                f"H1 BOS: YES\n"
                f"M15 Confirm: YES\n"
                f"DXY OK: YES\n"
                f"Time: {now}\n"
                f"Check M5 for entry. Use 2% risk rule."
            )
            send(msg)
        elif not dok:
            print(f"{pair}: DXY conflict - skipping")

from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot running")
    def log_message(self, *a): pass

def run_server():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT",8080))), H).serve_forever()

if __name__ == "__main__":
    print("Bot starting...")
    Thread(target=run_server, daemon=True).start()
    send("Kpojime Bot Started. Scanning XAU/USD and EUR/USD every 15 minutes.")
    while True:
        try:
            scan()
        except Exception as e:
            print(f"Error: {e}")
        print(f"Sleeping {SCAN_INTERVAL//60} min...")
        time.sleep(SCAN_INTERVAL)
