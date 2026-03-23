"""
Stablecoin Depeg Monitor v0.1
Phase 1 MVP — lemonade stand edition.

Polls CoinGecko (primary) + DeFiLlama (failover) every 60s.
Sends Telegram alerts on peg deviations.
Logs every depeg event to SQLite.
$0 stack — no paid APIs required.
"""

import os
import time
import sqlite3
import logging
import requests
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")  # e.g. @depegalerts or chat_id
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))  # seconds
DB_PATH = os.environ.get("DB_PATH", "depeg_events.db")

# Deviation thresholds (absolute % from $1.00 peg)
THRESHOLDS = {
    "warning":  0.0010,   # 0.10%
    "alert":    0.0050,   # 0.50%
    "critical": 0.0100,   # 1.00%
}

# Stablecoins to monitor — Phase 1: 10 majors
STABLECOINS = [
    {"id": "tether",          "symbol": "USDT", "name": "Tether"},
    {"id": "usd-coin",        "symbol": "USDC", "name": "USD Coin"},
    {"id": "dai",             "symbol": "DAI",  "name": "Dai"},
    {"id": "frax",            "symbol": "FRAX", "name": "Frax"},
    {"id": "true-usd",        "symbol": "TUSD", "name": "TrueUSD"},
    {"id": "paypal-usd",      "symbol": "PYUSD","name": "PayPal USD"},
    {"id": "liquity-usd",     "symbol": "LUSD", "name": "Liquity USD"},
    {"id": "curve-dao-token", "symbol": "CRV",  "name": "Curve (skip — not stablecoin, placeholder)"},
    {"id": "usdd",            "symbol": "USDD", "name": "USDD"},
    {"id": "first-digital-usd","symbol": "FDUSD","name": "First Digital USD"},
]

# Remove the CRV placeholder — was just for slot count
STABLECOINS = [s for s in STABLECOINS if s["symbol"] != "CRV"]

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
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            symbol      TEXT    NOT NULL,
            price       REAL    NOT NULL,
            deviation   REAL    NOT NULL,
            severity    TEXT    NOT NULL,
            source      TEXT    NOT NULL
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

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
DEFILLAMA_URL = "https://coins.llama.fi/prices/current/"

def fetch_coingecko(coin_ids: list[str]) -> dict:
    """Returns {coin_id: price} or {} on failure."""
    try:
        resp = requests.get(
            COINGECKO_URL,
            params={"ids": ",".join(coin_ids), "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return {k: v["usd"] for k, v in data.items() if "usd" in v}
    except Exception as e:
        log.warning(f"CoinGecko fetch failed: {e}")
        return {}

def fetch_defillama(coin_ids: list[str]) -> dict:
    """
    DeFiLlama coins API. Expects coingecko:coin-id format.
    Returns {coingecko_id: price} or {} on failure.
    """
    try:
        keys = ",".join(f"coingecko:{cid}" for cid in coin_ids)
        resp = requests.get(f"{DEFILLAMA_URL}{keys}", timeout=10)
        resp.raise_for_status()
        coins = resp.json().get("coins", {})
        result = {}
        for k, v in coins.items():
            cid = k.replace("coingecko:", "")
            result[cid] = v.get("price")
        return {k: v for k, v in result.items() if v is not None}
    except Exception as e:
        log.warning(f"DeFiLlama fetch failed: {e}")
        return {}

def get_prices() -> tuple[dict, str]:
    """
    Returns (prices_dict, source_label).
    Tries CoinGecko first; falls over to DeFiLlama if empty.
    """
    ids = [s["id"] for s in STABLECOINS]
    prices = fetch_coingecko(ids)
    if prices:
        return prices, "CoinGecko"
    log.info("CoinGecko returned empty — falling over to DeFiLlama")
    prices = fetch_defillama(ids)
    return prices, "DeFiLlama"

# ── Alert Logic ───────────────────────────────────────────────────────────────

def classify_severity(deviation: float) -> str | None:
    """Returns severity string or None if within normal range."""
    if deviation >= THRESHOLDS["critical"]:
        return "critical"
    if deviation >= THRESHOLDS["alert"]:
        return "alert"
    if deviation >= THRESHOLDS["warning"]:
        return "warning"
    return None

SEVERITY_EMOJI = {
    "warning":  "⚠️",
    "alert":    "🚨",
    "critical": "🔴",
}

def format_alert(coin: dict, price: float, deviation: float, severity: str) -> str:
    emoji = SEVERITY_EMOJI[severity]
    direction = "ABOVE" if price > 1.0 else "BELOW"
    pct = deviation * 100
    cg_link = f"https://www.coingecko.com/en/coins/{coin['id']}"
    return (
        f"{emoji} *{coin['name']} ({coin['symbol']}) DEPEG {severity.upper()}*\n"
        f"Price: `${price:.5f}` — {pct:.3f}% {direction} peg\n"
        f"[View on CoinGecko]({cg_link})\n"
        f"_Reported: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHANNEL:
        log.warning("Telegram not configured — printing alert locally:")
        log.warning(message)
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHANNEL,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False

# ── Alert Dedup ───────────────────────────────────────────────────────────────

# In-memory cooldown: don't re-alert same coin+severity within 10 minutes
_last_alert: dict[str, float] = {}
COOLDOWN_SECONDS = 600  # 10 minutes

def should_alert(symbol: str, severity: str) -> bool:
    key = f"{symbol}:{severity}"
    now = time.time()
    last = _last_alert.get(key, 0)
    if now - last >= COOLDOWN_SECONDS:
        _last_alert[key] = now
        return True
    return False

# ── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    log.info("=== Stablecoin Depeg Monitor v0.1 starting ===")
    log.info(f"Monitoring {len(STABLECOINS)} stablecoins | Poll interval: {POLL_INTERVAL}s")
    log.info(f"Thresholds — Warning: {THRESHOLDS['warning']*100:.2f}% | Alert: {THRESHOLDS['alert']*100:.2f}% | Critical: {THRESHOLDS['critical']*100:.2f}%")

    init_db()

    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set — alerts will print to console only")

    cycle = 0
    while True:
        cycle += 1
        prices, source = get_prices()

        if not prices:
            log.warning(f"[Cycle {cycle}] Both price sources failed. Sleeping {POLL_INTERVAL}s.")
            time.sleep(POLL_INTERVAL)
            continue

        alerts_fired = 0
        for coin in STABLECOINS:
            price = prices.get(coin["id"])
            if price is None:
                log.debug(f"  {coin['symbol']}: no price data from {source}")
                continue

            deviation = abs(price - 1.0)
            severity = classify_severity(deviation)

            if severity:
                log.warning(
                    f"  {coin['symbol']}: ${price:.5f} | {deviation*100:.3f}% off peg | {severity.upper()} [{source}]"
                )
                log_event(coin["symbol"], price, deviation, severity, source)
                if should_alert(coin["symbol"], severity):
                    msg = format_alert(coin, price, deviation, severity)
                    sent = send_telegram(msg)
                    alerts_fired += 1
                    log.info(f"  Alert sent via Telegram: {sent}")
            else:
                log.debug(f"  {coin['symbol']}: ${price:.5f} — OK")

        log.info(f"[Cycle {cycle}] Checked {len(prices)} coins via {source}. Alerts fired: {alerts_fired}.")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
