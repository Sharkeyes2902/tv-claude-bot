"""
NSE Auto-Fetch → Claude → Telegram Daily Trade Brief Bot
=========================================================
No TradingView subscription needed. This server:
  1. Runs a scheduled job every day at 3:30 PM IST
  2. Fetches end-of-day data directly from NSE's website (free)
  3. Sends each stock to Claude for analysis
  4. Delivers the brief to your Telegram

Deploy on Render.com (free tier) as a Python web service.
Start command: python app.py

Required Environment Variables (set in Render dashboard):
  ANTHROPIC_API_KEY    → Your Anthropic API key (sk-ant-...)
  TELEGRAM_BOT_TOKEN   → Your Telegram bot token from @BotFather
  TELEGRAM_CHAT_ID     → Your Telegram chat ID
  WATCHLIST            → Comma-separated NSE symbols e.g. RELIANCE,HDFCBANK,INFY
"""

import os
import json
import logging
import threading
import time
from datetime import datetime, timezone
import pytz
import requests
import anthropic
from flask import Flask, request, jsonify

# ─────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# Load secrets from environment variables
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Your watchlist — set this in Render as: RELIANCE,HDFCBANK,INFY,NIFTY 50
WATCHLIST_RAW = os.environ.get("WATCHLIST", "RELIANCE,HDFCBANK,INFY")
WATCHLIST = [s.strip().upper() for s in WATCHLIST_RAW.split(",") if s.strip()]

IST = pytz.timezone("Asia/Kolkata")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ─────────────────────────────────────────────────────────────
# TELEGRAM HELPER
# Sends a message to your Telegram chat
# ─────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("Telegram message sent.")
            return True
        log.error(f"Telegram error {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        log.error(f"Telegram failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# NSE DATA FETCHER
# Pulls today's OHLCV directly from NSE's website — free, no API key
# NSE blocks plain requests, so we send browser-like headers to get through
# ─────────────────────────────────────────────────────────────

def get_nse_quote(symbol: str) -> dict | None:
    """
    Fetches today's OHLCV quote for an NSE stock symbol.
    Returns a dict with open, high, low, close, volume — or None if it fails.

    NSE has two types of instruments:
      - Regular stocks  → uses /api/quote-equity
      - Indices (NIFTY) → uses /api/allIndices
    We try equity first, then fall back to indices.
    """

    # These headers trick NSE into thinking we are a real browser
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
        "Connection": "keep-alive",
    }

    session = requests.Session()

    try:
        # Step 1: Visit NSE homepage first — NSE requires a valid cookie session
        # Without this, the API calls return 401 or 403
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
        time.sleep(1)  # Small pause — be polite to NSE's servers

        # Step 2: Try equity quote endpoint
        url = f"https://www.nseindia.com/api/quote-equity?symbol={symbol}"
        resp = session.get(url, headers=headers, timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            pd = data.get("priceInfo", {})
            return {
                "symbol": symbol,
                "open":   pd.get("open",            "N/A"),
                "high":   pd.get("intraDayHighLow", {}).get("max", "N/A"),
                "low":    pd.get("intraDayHighLow", {}).get("min", "N/A"),
                "close":  pd.get("lastPrice",        "N/A"),
                "prev_close": pd.get("previousClose","N/A"),
                "volume": data.get("marketDeptOrderBook", {})
                               .get("tradeInfo", {})
                               .get("totalTradedVolume", "N/A"),
                "change_pct": pd.get("pChange", "N/A"),
                "52w_high": pd.get("weekHighLow", {}).get("max", "N/A"),
                "52w_low":  pd.get("weekHighLow", {}).get("min", "N/A"),
                "time":  datetime.now(IST).strftime("%d %b %Y"),
            }

        # Step 3: If equity fails, try index endpoint (for NIFTY, BANKNIFTY etc.)
        idx_url = "https://www.nseindia.com/api/allIndices"
        idx_resp = session.get(idx_url, headers=headers, timeout=10)

        if idx_resp.status_code == 200:
            indices = idx_resp.json().get("data", [])
            for idx in indices:
                if symbol in idx.get("index", "").upper().replace(" ", ""):
                    return {
                        "symbol":   symbol,
                        "open":     idx.get("open",    "N/A"),
                        "high":     idx.get("dayHigh", "N/A"),
                        "low":      idx.get("dayLow",  "N/A"),
                        "close":    idx.get("last",    "N/A"),
                        "prev_close": idx.get("previousClose", "N/A"),
                        "volume":   "N/A (Index)",
                        "change_pct": idx.get("percentChange", "N/A"),
                        "52w_high": idx.get("yearHigh", "N/A"),
                        "52w_low":  idx.get("yearLow",  "N/A"),
                        "time": datetime.now(IST).strftime("%d %b %Y"),
                    }

        log.warning(f"Could not fetch data for {symbol}. HTTP {resp.status_code}")
        return None

    except Exception as e:
        log.error(f"NSE fetch error for {symbol}: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# CLAUDE ANALYSIS
# Sends the NSE data to Claude and gets back a structured brief
# ─────────────────────────────────────────────────────────────

def analyse_with_claude(quote: dict) -> str:
    """
    Takes a quote dict (from get_nse_quote) and returns
    a formatted trade brief from Claude.
    """

    symbol = quote["symbol"]

    # Calculate candle stats — gives Claude more to work with
    try:
        o = float(quote["open"])
        h = float(quote["high"])
        l = float(quote["low"])
        c = float(quote["close"])
        pc = float(quote["prev_close"])

        day_range     = round(h - l, 2)
        body          = round(abs(c - o), 2)
        body_pct      = round(body / day_range * 100, 1) if day_range > 0 else 0
        upper_wick    = round(h - max(o, c), 2)
        lower_wick    = round(min(o, c) - l, 2)
        is_bullish    = c > o
        change_vs_prev = round((c - pc) / pc * 100, 2)
    except Exception:
        # If any calculation fails, just use raw values
        o=h=l=c=pc=day_range=body=body_pct=upper_wick=lower_wick=0
        is_bullish = False
        change_vs_prev = quote.get("change_pct", "N/A")

    prompt = f"""You are a senior technical analyst with 20+ years of experience in Indian equity markets (NSE/BSE).
You specialise in swing trading setups for retail traders. Be direct, specific, and practical.

Today's data for {symbol} (NSE):
  Date        : {quote.get('time', 'N/A')}
  Open        : {quote['open']}
  High        : {quote['high']}
  Low         : {quote['low']}
  Close       : {quote['close']}
  Prev Close  : {quote['prev_close']}
  Volume      : {quote['volume']}
  Change      : {change_vs_prev}%
  52-week High: {quote.get('52w_high', 'N/A')}
  52-week Low : {quote.get('52w_low',  'N/A')}

Candle analysis:
  Day range   : {day_range} pts
  Body size   : {body} pts ({body_pct}% of range)
  Upper wick  : {upper_wick} pts
  Lower wick  : {lower_wick} pts
  Colour      : {'Bullish (green)' if is_bullish else 'Bearish (red)'}

Write a pre-market brief for TOMORROW. Use EXACTLY this format:

*{symbol} — Daily Brief ({quote.get('time', '')})*

*Candle read:* [2 lines — what today's candle signals about sentiment and momentum]

*Key levels tomorrow:*
• Resistance : [specific price]
• Support    : [specific price]
• Day high   : {quote['high']} | Day low : {quote['low']}

*Bias for tomorrow:* [Bullish / Bearish / Neutral] — [one-line reason]

*Setup (if valid):*
• Entry trigger : [exact price or condition]
• Stop loss     : [price]
• Target 1      : [price]
• Target 2      : [price]
• R:R ratio     : [e.g. 1:2.1]

*52-week context:* [One line — is price near 52w high/low? What does that mean?]

*Risk note:* [One line — volume comment, broad market caveat, or event risk]

*Conviction:* [Low / Medium / High]

Keep it tight. A trader should read this in 30 seconds. No filler sentences."""

    try:
        message = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=650,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        log.error(f"Claude error for {symbol}: {e}")
        return f"⚠️ Analysis failed for {symbol}: {str(e)}"


# ─────────────────────────────────────────────────────────────
# DAILY JOB
# This is the function that runs automatically at 3:30 PM IST
# It loops through your watchlist and analyses each stock
# ─────────────────────────────────────────────────────────────

def run_daily_brief():
    """
    Fetches NSE data for every stock in your WATCHLIST,
    runs Claude analysis on each, and sends briefs to Telegram.
    Called automatically by the scheduler at 3:30 PM IST.
    """
    now_ist = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    log.info(f"Running daily brief at {now_ist} for {len(WATCHLIST)} stocks.")

    send_telegram(
        f"📊 *Daily Trade Brief — {now_ist}*\n"
        f"Analysing {len(WATCHLIST)} stocks: {', '.join(WATCHLIST)}\n"
        f"Briefs incoming..."
    )

    success_count = 0
    for symbol in WATCHLIST:
        log.info(f"Processing {symbol}...")

        quote = get_nse_quote(symbol)

        if quote is None:
            send_telegram(
                f"⚠️ Could not fetch NSE data for *{symbol}* today. "
                f"Market may be closed or symbol may be wrong."
            )
            continue

        analysis = analyse_with_claude(quote)
        send_telegram(analysis)
        success_count += 1

        # Wait 3 seconds between stocks — avoids hammering NSE or Claude APIs
        time.sleep(3)

    send_telegram(
        f"✅ *Done.* {success_count}/{len(WATCHLIST)} briefs sent. "
        f"Plan your trades — trade your plan."
    )


# ─────────────────────────────────────────────────────────────
# SCHEDULER
# Runs in the background and fires run_daily_brief() at 3:30 PM IST
# This is like a built-in alarm clock inside the server
# ─────────────────────────────────────────────────────────────

def scheduler_loop():
    """
    Infinite loop that checks the time every minute.
    When IST time hits 15:30 on a weekday (Mon–Fri),
    it fires the daily brief job.
    """
    log.info("Scheduler started. Will run daily brief at 15:30 IST on weekdays.")
    last_run_date = None  # Tracks last run so we don't run twice on same day

    while True:
        now = datetime.now(IST)

        is_weekday    = now.weekday() < 5          # 0=Mon … 4=Fri
        is_brief_time = now.hour == 15 and now.minute == 30
        not_run_today = last_run_date != now.date()

        if is_weekday and is_brief_time and not_run_today:
            log.info("Scheduler: firing daily brief now.")
            last_run_date = now.date()
            try:
                run_daily_brief()
            except Exception as e:
                log.error(f"Daily brief failed: {e}")
                send_telegram(f"⚠️ Daily brief job crashed: {str(e)}")

        time.sleep(60)  # Check once per minute


# ─────────────────────────────────────────────────────────────
# FLASK ENDPOINTS
# These are URLs your server exposes
# ─────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    """Health check — Render calls this to confirm the server is alive."""
    return jsonify({
        "status":    "running",
        "watchlist": WATCHLIST,
        "time_ist":  datetime.now(IST).strftime("%d %b %Y %I:%M %p IST"),
    }), 200


@app.route("/ping", methods=["GET"])
def ping():
    """
    Keep-alive endpoint.
    Point cron-job.org at:  https://your-app.onrender.com/ping
    Set it to run every 10 minutes so Render never sleeps.
    """
    return "pong", 200


@app.route("/run-now", methods=["GET"])
def run_now():
    """
    Manual trigger — visit this URL in your browser any time
    to immediately run the daily brief for all watchlist stocks.
    Useful for testing or running on demand.
    URL: https://your-app.onrender.com/run-now
    """
    log.info("Manual trigger: /run-now called.")

    # Run in a background thread so the browser gets an instant response
    thread = threading.Thread(target=run_daily_brief)
    thread.daemon = True
    thread.start()

    return jsonify({
        "status":  "started",
        "message": f"Running brief for: {', '.join(WATCHLIST)}. Check Telegram."
    }), 200


@app.route("/quote/<symbol>", methods=["GET"])
def quote(symbol):
    """
    Test endpoint — check if NSE data fetch is working for a symbol.
    URL: https://your-app.onrender.com/quote/RELIANCE
    Returns raw quote data (no Claude analysis).
    """
    data = get_nse_quote(symbol.upper())
    if data:
        return jsonify(data), 200
    return jsonify({"error": f"Could not fetch data for {symbol}"}), 404


# ─────────────────────────────────────────────────────────────
# START EVERYTHING
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    # Check environment variables
    missing = []
    for var in ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        log.warning(f"Missing environment variables: {', '.join(missing)}")
    else:
        log.info(f"All env vars found. Watchlist: {WATCHLIST}")

    # Start the scheduler in a background thread
    # It runs silently alongside the web server
    scheduler_thread = threading.Thread(target=scheduler_loop)
    scheduler_thread.daemon = True  # Dies automatically when the main server stops
    scheduler_thread.start()
    log.info("Background scheduler started.")

    # Start the Flask web server
    log.info(f"Web server starting on port {port}")
    app.run(host="0.0.0.0", port=port)
