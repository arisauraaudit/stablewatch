"""
Stablecoin Depeg Monitor v0.1
Phase 1 MVP — lemonade stand edition.

Polls CoinGecko (primary) + DeFiLlama (failover) every 60s.
Sends Telegram alerts on peg deviations.
Handles /start, /status, /help bot commands.
Logs every depeg event to SQLite.
$0 stack — no paid APIs required.
"""

import os
import time
import sqlite3
import logging
import threading
import requests
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")  # e.g. @stablewatchalerts
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL", "60"))
DB_PATH          = os.environ.get("DB_PATH", "depeg_events.db")

THRESHOLDS = {
    "warning":  0.0010,
    "alert":    0.0050,
    "critical": 0.0100,
}

STABLECOINS = [
    {"id": "tether",             "symbol": "USDT",  "name": "Tether"},
    {"id": "usd-coin",           "symbol": "USDC",  "name": "USD Coin"},
    {"id": "dai",                "symbol": "DAI",   "name": "Dai"},
    {"id": "frax",               "symbol": "FRAX",  "name": "Frax"},
    {"id": "true-usd",           "symbol": "TUSD",  "name": "TrueUSD"},
    {"id": "paypal-usd",         "symbol": "PYUSD", "name": "PayPal USD"},
    {"id": "liquity-usd",        "symbol": "LUSD",  "name": "Liquity USD"},
    {"id": "usdd",               "symbol": "USDD",  "name": "USDD"},
    {"id": "first-digital-usd",  "symbol": "FDUSD", "name": "First Digital USD"},
]

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS depeg_events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT    NOT NULL,
            symbol    TEXT    NOT NULL,
            price     REAL    NOT NULL,
            deviation REAL    NOT NULL,
            severity  TEXT    NOT NULL,
            source    TEXT    NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def log_event(symbol, price, deviation, severity, source):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO depeg_events (ts, symbol, price, deviation, severity, source) VALUES (?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), symbol, price, deviation, severity, source)
    )
    conn.commit()
    conn.close()

# ── Price Fetching ────────────────────────────────────────────────────────────

def fetch_coingecko(ids):
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ",".join(ids), "vs_currencies": "usd"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return {k: v["usd"] for k, v in data.items() if "usd" in v}
    except Exception as e:
        log.warning(f"CoinGecko failed: {e}")
        return {}

def fetch_defillama(ids):
    try:
        keys = ",".join(f"coingecko:{i}" for i in ids)
        r = requests.get(f"https://coins.llama.fi/prices/current/{keys}", timeout=10)
        r.raise_for_status()
        coins = r.json().get("coins", {})
        return {k.replace("coingecko:", ""): v["price"]
                for k, v in coins.items() if "price" in v}
    except Exception as e:
        log.warning(f"DeFiLlama failed: {e}")
        return {}

def get_prices():
    ids = [s["id"] for s in STABLECOINS]
    prices = fetch_coingecko(ids)
    if prices:
        return prices, "CoinGecko"
    log.info("CoinGecko empty — falling over to DeFiLlama")
    return fetch_defillama(ids), "DeFiLlama"

# ── Alert Logic ───────────────────────────────────────────────────────────────

def classify_severity(deviation):
    if deviation >= THRESHOLDS["critical"]: return "critical"
    if deviation >= THRESHOLDS["alert"]:    return "alert"
    if deviation >= THRESHOLDS["warning"]:  return "warning"
    return None

SEVERITY_EMOJI = {"warning": "⚠️", "alert": "🚨", "critical": "🔴"}

def format_alert(coin, price, deviation, severity):
    emoji     = SEVERITY_EMOJI[severity]
    direction = "ABOVE" if price > 1.0 else "BELOW"
    pct       = deviation * 100
    cg_link   = f"https://www.coingecko.com/en/coins/{coin['id']}"
    channel   = f" | Join: {TELEGRAM_CHANNEL}" if TELEGRAM_CHANNEL else ""
    return (
        f"{emoji} *{coin['name']} ({coin['symbol']}) — DEPEG {severity.upper()}*\n"
        f"Price: `${price:.5f}` — {pct:.3f}% {direction} peg\n"
        f"[View on CoinGecko]({cg_link}){channel}\n"
        f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )

# ── Telegram ──────────────────────────────────────────────────────────────────

def tg(method, **kwargs):
    if not TELEGRAM_TOKEN:
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
            json=kwargs, timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Telegram {method} failed: {e}")
        return None

def send_message(chat_id, text, parse_mode="Markdown"):
    tg("sendMessage", chat_id=chat_id, text=text,
       parse_mode=parse_mode, disable_web_page_preview=True)

def broadcast(text):
    """Send to the public alerts channel."""
    if TELEGRAM_CHANNEL:
        send_message(TELEGRAM_CHANNEL, text)
    else:
        log.warning(f"[NO CHANNEL] {text}")

# ── Alert Dedup ───────────────────────────────────────────────────────────────

_last_alert: dict = {}
COOLDOWN = 600  # 10 min per coin+severity

def should_alert(symbol, severity):
    key  = f"{symbol}:{severity}"
    now  = time.time()
    if now - _last_alert.get(key, 0) >= COOLDOWN:
        _last_alert[key] = now
        return True
    return False

# ── Snapshot helper ───────────────────────────────────────────────────────────

def snapshot_text(prices, source):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"📊 *Stablecoin Status* — {now} ({source})\n"]
    for coin in STABLECOINS:
        price = prices.get(coin["id"])
        if price is None:
            lines.append(f"  {coin['symbol']:6s} — no data")
            continue
        dev  = abs(price - 1.0)
        sev  = classify_severity(dev)
        icon = SEVERITY_EMOJI.get(sev, "✅") if sev else "✅"
        lines.append(f"  {icon} {coin['symbol']:6s} `${price:.5f}`  ({dev*100:.3f}% off peg)")
    return "\n".join(lines)

# ── Bot Command Listener ──────────────────────────────────────────────────────

_last_update_id = 0
_latest_prices  = {}
_latest_source  = "pending"

def handle_commands():
    global _last_update_id
    while True:
        try:
            result = tg("getUpdates", offset=_last_update_id + 1, timeout=30)
            if not result or not result.get("ok"):
                time.sleep(5)
                continue
            for update in result.get("result", []):
                _last_update_id = update["update_id"]
                msg = update.get("message") or update.get("channel_post")
                if not msg:
                    continue
                text    = msg.get("text", "")
                chat_id = msg["chat"]["id"]
                cmd     = text.split()[0].split("@")[0].lower() if text else ""

                if cmd == "/start":
                    channel_line = f"\n\n📢 Follow the alerts channel: {TELEGRAM_CHANNEL}" if TELEGRAM_CHANNEL else ""
                    send_message(chat_id,
                        f"👋 *Welcome to StableWatch!*\n\n"
                        f"I monitor {len(STABLECOINS)} major stablecoins 24/7 and alert you the moment any drifts from its $1.00 peg.\n\n"
                        f"*Alert thresholds:*\n"
                        f"⚠️ WARNING — >0.10% off peg\n"
                        f"🚨 ALERT — >0.50% off peg\n"
                        f"🔴 CRITICAL — >1.00% off peg\n"
                        f"{channel_line}\n\n"
                        f"Use /status to see current prices. Free during beta."
                    )

                elif cmd == "/status":
                    if _latest_prices:
                        send_message(chat_id, snapshot_text(_latest_prices, _latest_source))
                    else:
                        send_message(chat_id, "⏳ Fetching first price data... try again in 30 seconds.")

                elif cmd == "/help":
                    send_message(chat_id,
                        f"*StableWatch — How it works*\n\n"
                        f"I poll {len(STABLECOINS)} stablecoins every {POLL_INTERVAL}s via CoinGecko + DeFiLlama failover.\n\n"
                        f"*Commands:*\n"
                        f"/status — Live peg status for all coins\n"
                        f"/help — This message\n\n"
                        f"*Monitored:* " + ", ".join(s["symbol"] for s in STABLECOINS) + "\n\n"
                        f"Free beta. No spam. Alerts fire max once per 10 min per coin."
                    )
        except Exception as e:
            log.error(f"Command handler error: {e}")
            time.sleep(5)

# ── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    global _latest_prices, _latest_source

    log.info("=== StableWatch Depeg Monitor v0.1 ===")
    log.info(f"Monitoring {len(STABLECOINS)} stablecoins | Poll: {POLL_INTERVAL}s | Channel: {TELEGRAM_CHANNEL or 'NOT SET'}")

    init_db()

    if TELEGRAM_TOKEN:
        cmd_thread = threading.Thread(target=handle_commands, daemon=True)
        cmd_thread.start()
        log.info("Command listener started")
    else:
        log.warning("TELEGRAM_TOKEN not set — console-only mode")

    cycle = 0
    while True:
        cycle += 1
        prices, source = get_prices()
        _latest_prices = prices
        _latest_source = source

        if not prices:
            log.warning(f"[Cycle {cycle}] Both price sources failed.")
            time.sleep(POLL_INTERVAL)
            continue

        fired = 0
        for coin in STABLECOINS:
            price = prices.get(coin["id"])
            if price is None:
                continue
            dev      = abs(price - 1.0)
            severity = classify_severity(dev)
            if severity:
                log.warning(f"  {coin['symbol']}: ${price:.5f} | {dev*100:.3f}% | {severity.upper()} [{source}]")
                log_event(coin["symbol"], price, dev, severity, source)
                if should_alert(coin["symbol"], severity):
                    broadcast(format_alert(coin, price, dev, severity))
                    fired += 1
            else:
                log.debug(f"  {coin['symbol']}: ${price:.5f} OK")

        log.info(f"[Cycle {cycle}] {len(prices)} coins via {source}. Alerts: {fired}.")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
