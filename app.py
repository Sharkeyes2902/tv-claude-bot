"""
TradingView → Claude → Telegram Daily Trade Brief Bot
======================================================
Deploy this on Render.com (free tier) as a Python web service.

Required Environment Variables (set in Render dashboard):
  ANTHROPIC_API_KEY    - Your Anthropic API key (sk-ant-...)
  TELEGRAM_BOT_TOKEN   - Your Telegram bot token from @BotFather
  TELEGRAM_CHAT_ID     - Your Telegram chat ID (the number from getUpdates)
  WEBHOOK_SECRET       - Any password you choose (must match TradingView alert JSON)

Start command for Render:  python app.py
"""

import os
import json
import logging
from datetime import datetime, timezone
import requests
import anthropic
from flask import Flask, request, jsonify

# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# Load secrets from environment variables (never hardcode these)
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ─────────────────────────────────────────────
# TELEGRAM HELPER
# ─────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    """
    Sends a message to your Telegram chat.
    Returns True if successful, False otherwise.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"   # lets us use *bold* and `code` in messages
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            log.info("Telegram message sent successfully.")
            return True
        else:
            log.error(f"Telegram error {response.status_code}: {response.text}")
            return False
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


# ─────────────────────────────────────────────
# CLAUDE ANALYSIS
# ─────────────────────────────────────────────

def analyse_with_claude(symbol: str, ohlcv: dict, extra_context: str = "") -> str:
    """
    Sends OHLCV data to Claude and returns a structured trade brief.
    
    symbol      - NSE ticker e.g. "RELIANCE", "NIFTY"
    ohlcv       - dict with keys: open, high, low, close, volume, time
    extra_context - any extra text from TradingView alert (optional)
    """

    # Calculate some basic derived values to give Claude more to work with
    candle_range  = float(ohlcv["high"]) - float(ohlcv["low"])
    candle_body   = abs(float(ohlcv["close"]) - float(ohlcv["open"]))
    is_bullish    = float(ohlcv["close"]) > float(ohlcv["open"])
    body_pct      = round((candle_body / candle_range * 100), 1) if candle_range > 0 else 0
    upper_wick    = round(float(ohlcv["high"]) - max(float(ohlcv["open"]), float(ohlcv["close"])), 2)
    lower_wick    = round(min(float(ohlcv["open"]), float(ohlcv["close"])) - float(ohlcv["low"]), 2)
    change_pct    = round(
        (float(ohlcv["close"]) - float(ohlcv["open"])) / float(ohlcv["open"]) * 100, 2
    )

    prompt = f"""You are a senior technical analyst with 20+ years of experience in Indian equity markets (NSE/BSE). 
You specialize in swing trading setups for the next trading day using price action, key levels, and risk management.

Today's OHLCV data for {symbol}:
  Date   : {ohlcv.get('time', 'N/A')}
  Open   : {ohlcv['open']}
  High   : {ohlcv['high']}
  Low    : {ohlcv['low']}
  Close  : {ohlcv['close']}
  Volume : {ohlcv.get('volume', 'N/A')}

Derived candle stats:
  Day change    : {change_pct}%
  Candle body   : {round(candle_body, 2)} pts ({body_pct}% of range)
  Upper wick    : {upper_wick} pts
  Lower wick    : {lower_wick} pts
  Candle colour : {'Bullish (green)' if is_bullish else 'Bearish (red)'}
  Day range     : {round(candle_range, 2)} pts

{f'Additional context from chart: {extra_context}' if extra_context else ''}

Based ONLY on this single candle's data, provide a concise pre-market trade brief for TOMORROW. 
Structure your response EXACTLY as shown below (use the same headings):

*{symbol} — Daily Brief ({datetime.now().strftime('%d %b %Y')})*

*Candle read:* [2-line interpretation of today's candle — what it signals about sentiment]

*Key levels to watch:*
• Resistance: [price level]
• Support: [price level]
• Day high: {ohlcv['high']} | Day low: {ohlcv['low']}

*Directional bias for tomorrow:* [Bullish / Bearish / Neutral — with one-line reason]

*Trade setup (if any):*
• Entry trigger: [specific price or condition e.g. "Buy above 2925 on 15-min close"]
• Stop loss: [price level]
• Target 1: [price level]
• Target 2: [price level]
• R:R ratio: [e.g. 1:1.8]

*Risk note:* [1 line — e.g. volume comment, broad market caveat, upcoming event if known]

*Conviction:* [Low / Medium / High — based on candle clarity]

Keep it tight — a trader should be able to read this in 30 seconds. No waffle. If the candle is ambiguous, say so clearly."""

    try:
        message = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return f"Analysis failed for {symbol}: {str(e)}"


# ─────────────────────────────────────────────
# WEBHOOK ENDPOINT
# (TradingView posts to this URL)
# ─────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receives POST from TradingView alert.
    Expected JSON body:
    {
        "secret":  "your-webhook-secret",
        "symbol":  "{{ticker}}",
        "open":    "{{open}}",
        "high":    "{{high}}",
        "low":     "{{low}}",
        "close":   "{{close}}",
        "volume":  "{{volume}}",
        "time":    "{{time}}",
        "context": ""          (optional free-text field)
    }
    """
    try:
        data = request.get_json(force=True)
        log.info(f"Webhook received: {data}")
    except Exception:
        log.warning("Could not parse JSON body.")
        return jsonify({"error": "invalid JSON"}), 400

    # ── Security check ──────────────────────────────────────────────────────
    if not WEBHOOK_SECRET or data.get("secret") != WEBHOOK_SECRET:
        log.warning("Webhook received with wrong secret — rejected.")
        return jsonify({"error": "unauthorized"}), 401

    # ── Extract fields ───────────────────────────────────────────────────────
    symbol = data.get("symbol", "UNKNOWN").upper()
    required_fields = ["open", "high", "low", "close"]
    
    for field in required_fields:
        if field not in data:
            msg = f"Missing field '{field}' in webhook payload."
            log.error(msg)
            send_telegram(f"⚠️ Bot error for {symbol}: {msg}")
            return jsonify({"error": msg}), 400

    ohlcv = {
        "open":   data["open"],
        "high":   data["high"],
        "low":    data["low"],
        "close":  data["close"],
        "volume": data.get("volume", "N/A"),
        "time":   data.get("time", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
    }
    extra_context = data.get("context", "")

    # ── Call Claude ──────────────────────────────────────────────────────────
    log.info(f"Requesting Claude analysis for {symbol}...")
    analysis = analyse_with_claude(symbol, ohlcv, extra_context)

    # ── Send to Telegram ─────────────────────────────────────────────────────
    success = send_telegram(analysis)

    if success:
        return jsonify({"status": "ok", "symbol": symbol}), 200
    else:
        return jsonify({"status": "telegram_error", "symbol": symbol}), 500


# ─────────────────────────────────────────────
# BATCH ENDPOINT
# (Optional: send multiple symbols at once)
# ─────────────────────────────────────────────

@app.route("/batch", methods=["POST"])
def batch():
    """
    Accepts a list of symbols with OHLCV data and analyses all of them.
    Useful if you want a single end-of-day alert for your full watchlist.
    
    Expected JSON:
    {
        "secret": "your-secret",
        "stocks": [
            {"symbol":"RELIANCE","open":"2850","high":"2920","low":"2840","close":"2910","volume":"5200000"},
            {"symbol":"HDFCBANK","open":"1640","high":"1670","low":"1630","close":"1655","volume":"8100000"}
        ]
    }
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid JSON"}), 400

    if not WEBHOOK_SECRET or data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    stocks = data.get("stocks", [])
    if not stocks:
        return jsonify({"error": "no stocks provided"}), 400

    send_telegram(f"📊 *Batch brief starting — {len(stocks)} stocks*")

    results = []
    for stock in stocks:
        sym = stock.get("symbol", "UNKNOWN").upper()
        required = ["open", "high", "low", "close"]
        if not all(k in stock for k in required):
            send_telegram(f"⚠️ Skipping {sym} — missing OHLCV fields.")
            continue
        
        analysis = analyse_with_claude(sym, stock, stock.get("context", ""))
        send_telegram(analysis)
        results.append({"symbol": sym, "status": "sent"})
        log.info(f"Batch: {sym} done.")

    return jsonify({"status": "ok", "processed": results}), 200


# ─────────────────────────────────────────────
# HEALTH CHECK
# (Render uses this to confirm the server is up)
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "running",
        "bot": "TradingView-Claude-Telegram",
        "time": datetime.now(timezone.utc).isoformat()
    }), 200


@app.route("/ping", methods=["GET"])
def ping():
    """
    Hit this URL from cron-job.org every 10 minutes
    to keep your Render free-tier server awake.
    URL: https://your-app.onrender.com/ping
    """
    return "pong", 200


# ─────────────────────────────────────────────
# START SERVER
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # Render sets the PORT environment variable automatically
    port = int(os.environ.get("PORT", 5000))
    
    log.info(f"Starting TradingView-Claude-Telegram bot on port {port}")
    
    # Validate that all required env vars are set
    missing = []
    for var in ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WEBHOOK_SECRET"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        log.warning(f"WARNING: Missing environment variables: {', '.join(missing)}")
        log.warning("Set these in your Render dashboard under Environment.")
    else:
        log.info("All environment variables found. Bot is ready.")
    
    app.run(host="0.0.0.0", port=port)
