import os
import time
import json
import hmac
import hashlib
import logging
import threading
import uuid
import atexit
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, urlparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import requests
import websocket
from dotenv import load_dotenv


load_dotenv()


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.getenv(
    "RAILWAY_VOLUME_MOUNT_PATH",
    BASE_DIR
)

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")

# IMPORTANT:
# Current Delta India public websocket
WS_URL = os.getenv(
    "DELTA_PUBLIC_WS_URL",
    "wss://public-socket.india.delta.exchange"
)

PORT = int(
    os.getenv("PORT")
    or os.getenv("DASHBOARD_PORT")
    or "8080"
)

API_KEY = os.getenv(
    "DELTA_API_KEY",
    ""
).strip()

API_SECRET = os.getenv(
    "DELTA_API_SECRET",
    ""
).strip()

ACCOUNT_NAME = os.getenv(
    "ACCOUNT_NAME",
    "Main"
).strip()

ACCOUNT_ID = os.getenv(
    "ACCOUNT_ID",
    "primary"
).strip()

SYMBOL = "XAUTUSD"


# ============================================================
# STRATEGY
# ============================================================

MARGIN_FRACTION = Decimal("0.10")

MAX_LEVERAGE = 100
MIN_LEVERAGE = 10

# Supertrend
SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = Decimal("3.0")

# Only CLOSED 1-minute candles generate a new signal.
TIMEFRAME = "1m"


# ============================================================
# TIMING
# ============================================================

RECONNECT_SECONDS = 5

ENTRY_CONFIRM_TIMEOUT = 12
CLOSE_CONFIRM_TIMEOUT = 12

POLL_INTERVAL = 0.30

# Dashboard REST polling.
DASHBOARD_POSITION_CACHE_SECONDS = 1.0


# ============================================================
# FILES
# ============================================================

STATE_FILE = os.path.join(
    DATA_DIR,
    "xautusd_bot_state.json"
)

HISTORY_FILE = os.path.join(
    DATA_DIR,
    "xautusd_trade_history.json"
)

LOCK_FILE = os.path.join(
    DATA_DIR,
    "xautusd_bot.lock"
)

LOCK_HANDLE = None

PUBLIC_IP = "Loading..."


os.makedirs(
    DATA_DIR,
    exist_ok=True
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True
)


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def atomic_write(path, data):
    tmp = path + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            data,
            f,
            indent=2,
            default=str
        )

    os.replace(
        tmp,
        path
    )


def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception:
        return default


def load_history():
    data = load_json(
        HISTORY_FILE,
        []
    )

    return data if isinstance(
        data,
        list
    ) else []


def save_history(history):
    atomic_write(
        HISTORY_FILE,
        history
    )


def as_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def as_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def is_market_closed(dt=None):
    """
    Delta trading session handling retained from your old bot.

    Saturday >= 05:30 IST  -> closed
    Sunday                 -> closed
    Monday < 05:30 IST     -> closed
    """

    dt = dt or now_ist()

    weekday = dt.weekday()
    t = dt.time()

    if weekday == 5 and t >= dtime(5, 30):
        return True

    if weekday == 6:
        return True

    if weekday == 0 and t < dtime(5, 30):
        return True

    return False


def candle_bucket_from_timestamp(value):
    """
    Delta websocket candlestick ts is microseconds.
    Convert to the 1-minute UTC bucket.

    Also supports seconds/milliseconds defensively.
    """

    try:
        ts = float(value)

        if ts > 1e14:
            ts = ts / 1_000_000.0
        elif ts > 1e11:
            ts = ts / 1_000.0

        return int(ts // 60)

    except Exception:
        return None


def normalize_candle(item):
    """
    Normalize REST/WebSocket candle into:

    {
        bucket,
        time,
        open,
        high,
        low,
        close,
        volume
    }
    """

    try:

        if isinstance(item, dict):

            close = item.get("close")
            high = item.get("high")
            low = item.get("low")
            open_price = item.get("open")

            if (
                close is None
                or high is None
                or low is None
                or open_price is None
            ):
                # WebSocket compact fields
                close = item.get("c")
                high = item.get("h")
                low = item.get("l")
                open_price = item.get("o")

            if (
                close is None
                or high is None
                or low is None
                or open_price is None
            ):
                return None

            raw_time = (
                item.get("time")
                or item.get("timestamp")
                or item.get("ts")
            )

            bucket = candle_bucket_from_timestamp(
                raw_time
            )

            if bucket is None:
                bucket = int(
                    time.time() // 60
                )

            return {
                "bucket": bucket,
                "
