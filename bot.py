import os
import time
import json
import hmac
import hashlib
import logging
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, parse_qs, urlparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import requests
import websocket
from dotenv import load_dotenv


# =====================================================================
# DELTA PRO AUTOTRADER
# DUAL STRATEGY - XAUTUSD ONLY
#
# S1 = PEAK HIGH / PEAK LOW BREAKOUT
# S2 = 15 MINUTE CANDLE SAR
#
# IMPORTANT:
# Only ONE strategy can be active for an account at a time.
# S2 is the default PRIMARY strategy.
# =====================================================================

load_dotenv()


# =====================================================================
# GLOBAL CONFIG
# =====================================================================

IST = ZoneInfo("Asia/Kolkata")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PERSISTENT_DATA_DIR = os.getenv(
    "RAILWAY_VOLUME_MOUNT_PATH",
    BASE_DIR
)

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")

WS_URL = os.getenv(
    "DELTA_PUBLIC_WS_URL",
    "wss://public-socket.india.delta.exchange"
)

DASHBOARD_PORT = int(
    os.getenv("DASHBOARD_PORT", "8000")
)

PORT = int(
    os.getenv("PORT", str(DASHBOARD_PORT))
)

SYMBOL = "XAUTUSD"

TRADING_START_TIME = dtime(5, 45)

SESSION_START_TIME = dtime(5, 30)

RECONNECT_SECONDS = 3

POSITION_VERIFY_RETRIES = 8

POSITION_VERIFY_DELAY = 0.35

DEFAULT_PRIMARY_STRATEGY = "s2"

# Same leverage ladder for BOTH strategies
LEVERAGE_LADDER = [100, 90, 80, 70, 60, 50, 40, 30, 20, 10]

DEFAULT_LEVERAGE = Decimal("100")

DEFAULT_BALANCE_FRACTION = Decimal("0.10")


# =====================================================================
# DIRECTORIES
# =====================================================================

STATE_DIR = os.path.join(
    PERSISTENT_DATA_DIR,
    "account_states"
)

HISTORY_DIR = os.path.join(
    PERSISTENT_DATA_DIR,
    "account_history"
)

CLIENTS_FILE = os.path.join(
    PERSISTENT_DATA_DIR,
    "clients_config.json"
)

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)


# =====================================================================
# PRIMARY ACCOUNT
# =====================================================================

PRIMARY_ACCOUNT_ID = os.getenv(
    "ACCOUNT_ID",
    "primary"
).strip()

PRIMARY_ACCOUNT_NAME = os.getenv(
    "ACCOUNT_NAME",
    "Primary Account"
).strip()

PRIMARY_API_KEY = os.getenv(
    "DELTA_API_KEY",
    ""
).strip()

PRIMARY_API_SECRET = os.getenv(
    "DELTA_API_SECRET",
    ""
).strip()


# =====================================================================
# LOGGING
# =====================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True
)

CACHED_SERVER_IP = "Detecting..."


# =====================================================================
# GLOBAL ACCOUNT / STRATEGY MANAGEMENT
# =====================================================================

BOT_ACCOUNTS = {}

ACCOUNTS_LOCK = threading.RLock()

# One shared execution lock for each account + symbol.
# This is extremely important because S1 and S2 use the same
# exchange position.
EXECUTION_LOCKS = {}

EXECUTION_LOCKS_MASTER = threading.RLock()


def get_execution_lock(account_id, symbol):
    key = f"{account_id}_{symbol}".lower()

    with EXECUTION_LOCKS_MASTER:
        if key not in EXECUTION_LOCKS:
            EXECUTION_LOCKS[key] = threading.RLock()
        return EXECUTION_LOCKS[key]


# Active strategy per base account.
#
# Example:
# primary -> s2
# client_123 -> s1
#
ACTIVE_STRATEGIES = {}

ACTIVE_STRATEGIES_LOCK = threading.RLock()


def get_active_strategy(account_id):
    with ACTIVE_STRATEGIES_LOCK:
        return ACTIVE_STRATEGIES.get(
            account_id,
            DEFAULT_PRIMARY_STRATEGY
        )


def set_active_strategy(account_id, strategy_key):
    with ACTIVE_STRATEGIES_LOCK:
        ACTIVE_STRATEGIES[account_id] = strategy_key


# =====================================================================
# GENERAL HELPERS
# =====================================================================

def update_server_ip():
    global CACHED_SERVER_IP

    try:
        res = requests.get(
            "https://api.ipify.org?format=json",
            timeout=5
        )

        ip = res.json().get("ip")

        if ip:
            CACHED_SERVER_IP = ip

            logging.warning(
                "=================================================="
            )
            logging.warning(
                f" RAILWAY OUTBOUND IP --> {ip}"
            )
            logging.warning(
                " WHITELIST THIS IP IN DELTA EXCHANGE API SETTINGS"
            )
            logging.warning(
                "=================================================="
            )

    except Exception as e:
        logging.warning(
            f"IP FETCH ERROR | {e}"
        )


def now_ist():
    return datetime.now(IST)


def is_weekend(dt=None):
    """
    Trading weekend:
    Saturday from 05:30 IST
    Entire Sunday
    Monday before 05:30 IST
    """

    dt = dt or now_ist()

    weekday = dt.weekday()
    current_time = dt.time()

    # Saturday
    if weekday == 5:
        return current_time >= SESSION_START_TIME

    # Sunday
    if weekday == 6:
        return True

    # Monday before new session
    if weekday == 0 and current_time < SESSION_START_TIME:
        return True

    return False


def get_current_session_start(dt=None):
    dt = dt or now_ist()

    current_time = dt.time()

    session_start = dt.replace(
        hour=5,
        minute=30,
        second=0,
        microsecond=0
    )

    if current_time >= SESSION_START_TIME:
        return session_start

    return session_start - timedelta(days=1)


def safe_filename(value):
    result = ""

    for char in str(value):
        if char.isalnum() or char in ("-", "_"):
            result += char
        else:
            result += "_"

    return result or "account"


def account_state_file(unique_id):
    return os.path.join(
        STATE_DIR,
        safe_filename(unique_id) + ".json"
    )


def account_history_file(unique_id):
    return os.path.join(
        HISTORY_DIR,
        safe_filename(unique_id) + ".json"
    )


def atomic_write_json(filename, data):
    tmp = filename + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=2
        )

    os.replace(tmp, filename)


def load_clients_config():
    if not os.path.exists(CLIENTS_FILE):
        return {}

    try:
        with open(
            CLIENTS_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}

    except Exception:
        return {}


def save_clients_config(cfg):
    atomic_write_json(
        CLIENTS_FILE,
        cfg
    )


def wait_for_position_size(
    client,
    product_id,
    expected_size=None,
    expected_zero=False
):
    """
    Verify exchange position after an order.

    expected_zero=True:
        wait until position is actually flat.

    expected_size:
        wait until position size is non-zero and direction/size
        roughly matches the requested order.
    """

    for _ in range(POSITION_VERIFY_RETRIES):

        try:
            pos = client.position(product_id)

            actual_size = int(
                pos.get("size", 0) or 0
            )

            if expected_zero:

                if actual_size == 0:
                    return pos

            elif expected_size is not None:

                if actual_size != 0:

                    if abs(actual_size) >= abs(
                        int(expected_size)
                    ):
                        return pos

        except Exception:
            pass

        time.sleep(POSITION_VERIFY_DELAY)

    try:
        return client.position(product_id)
    except Exception:
        return {
            "size": 0,
            "entry_price": None,
            "stop_loss": None,
            "unrealized_pnl": 0
        }


# =====================================================================
# DELTA CLIENT
# =====================================================================

class DeltaClient:

    def __init__(
        self,
        api_key,
        api_secret,
        account_name,
        symbol
    ):
        self.api_key = (
            api_key or ""
        ).strip()

        self.api_secret = (
            api_secret or ""
        ).strip()

        self.account_name = (
            account_name or "Account"
        ).strip()

        self.symbol = (
            symbol or SYMBOL
        ).strip().upper()

        self.session = requests.Session()

        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "MultiBot/95.0"
        })


    def sign(
        self,
        method,
        path,
        query="",
        body=""
    ):
        timestamp = str(
            int(time.time())
        )

        message = (
            method.upper()
            + timestamp
            + path
            + query
            + body
        )

        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
