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
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
import websocket
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")

WS_URL = os.getenv(
    "DELTA_PUBLIC_WS_URL",
    "wss://public-socket.india.delta.exchange"
)

PORT = int(
    os.getenv("PORT")
    or os.getenv("DASHBOARD_PORT")
    or "8080"
)

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "Main").strip()
ACCOUNT_ID = os.getenv("ACCOUNT_ID", "primary").strip()

SYMBOL = "XAUTUSD"

MARGIN_FRACTION = Decimal("0.10")

MAX_LEVERAGE = 100
MIN_LEVERAGE = 10

SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = 3.0

RECONNECT_SECONDS = 5

ENTRY_CONFIRM_TIMEOUT = 12
CLOSE_CONFIRM_TIMEOUT = 12

POLL_INTERVAL = 0.30
POSITION_REFRESH_SECONDS = 1.0

# ============================================================
# WEEKEND SETTINGS
# ============================================================

# Saturday 5:00 AM IST:
# Open position must be force closed.
WEEKEND_CLOSE_TIME = dtime(5, 0)

# Monday 5:30 AM IST:
# Trading becomes allowed again.
WEEKEND_RESUME_TIME = dtime(5, 30)

# Scheduler checks every second.
WEEKEND_CHECK_INTERVAL = 1.0

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

os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True,
)


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def atomic_write(path, data):
    tmp = path + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=2,
            default=str
        )

    os.replace(tmp, path)


def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default

        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        return default


def load_history():
    data = load_json(HISTORY_FILE, [])

    return data if isinstance(data, list) else []


def save_history(history):
    atomic_write(HISTORY_FILE, history)


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


# ============================================================
# MARKET SCHEDULE
# ============================================================

def is_market_closed(dt=None):
    """
    Weekend trading schedule:

    Saturday:
        00:00 - 04:59  -> technically Friday session continuation
        05:00 onward   -> CLOSED

    Sunday:
        FULL DAY CLOSED

    Monday:
        00:00 - 05:29  -> CLOSED
        05:30 onward   -> OPEN

    This function is used to prevent new trades.
    """

    dt = dt or now_ist()

    wd = dt.weekday()
    t = dt.time()

    # Saturday
    if wd == 5 and t >= WEEKEND_CLOSE_TIME:
        return True

    # Sunday
    if wd == 6:
        return True

    # Monday before 05:30
    if wd == 0 and t < WEEKEND_RESUME_TIME:
        return True

    return False


def weekend_state(dt=None):
    """
    Returns:

        PRE_CLOSE
        WEEKEND
        MONDAY_PRE_OPEN
        OPEN
    """

    dt = dt or now_ist()

    wd = dt.weekday()
    t = dt.time()

    # Saturday before 05:00
    if wd == 5 and t < WEEKEND_CLOSE_TIME:
        return "PRE_CLOSE"

    # Saturday after 05:00
    if wd == 5 and t >= WEEKEND_CLOSE_TIME:
        return "WEEKEND"

    # Sunday
    if wd == 6:
        return "WEEKEND"

    # Monday before 05:30
    if wd == 0 and t < WEEKEND_RESUME_TIME:
        return "MONDAY_PRE_OPEN"

    return "OPEN"


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_bucket(ts):
    try:
        x = float(ts)

        if x > 1e14:
            x /= 1_000_000

        elif x > 1e11:
            x /= 1_000

        return int(x // 60)

    except Exception:
        return None


def normalize_candle(item):
    try:
        if not isinstance(item, dict):
            return None

        o = item.get(
            "open",
            item.get("o")
        )

        h = item.get(
            "high",
            item.get("h")
        )

        l = item.get(
            "low",
            item.get("l")
        )

        c = item.get(
            "close",
            item.get("c")
        )

        ts = item.get(
            "time",
            item.get(
                "timestamp",
                item.get("ts")
            )
        )

        if None in (o, h, l, c):
            return None

        b = candle_bucket(ts)

        if b is None:
            b = int(time.time() // 60)

        return {
            "bucket": b,
            "time": ts,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(
                item.get(
                    "volume",
                    item.get("v", 0)
                ) or 0
            ),
        }

    except Exception:
        return None


# ============================================================
# PROCESS LOCK
# ============================================================

def acquire_lock():
    global LOCK_HANDLE

    try:
        import fcntl

        LOCK_HANDLE = open(
            LOCK_FILE,
            "w",
            encoding="utf-8"
        )

        fcntl.flock(
            LOCK_HANDLE.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB
        )

        LOCK_HANDLE.write(str(os.getpid()))
        LOCK_HANDLE.flush()

        atexit.register(release_lock)

        return True

    except BlockingIOError:
        return False

    except ImportError:
        return True

    except Exception:
        return False


def release_lock():
    global LOCK_HANDLE

    if LOCK_HANDLE is None:
        return

    try:
        import fcntl

        fcntl.flock(
            LOCK_HANDLE.fileno(),
            fcntl.LOCK_UN
        )

    except Exception:
        pass

    try:
        LOCK_HANDLE.close()

    except Exception:
        pass

    LOCK_HANDLE = None


# ============================================================
# PUBLIC IP
# ============================================================

def get_public_ip():
    global PUBLIC_IP

    if PUBLIC_IP != "Loading...":
        return PUBLIC_IP

    try:
        r = requests.get(
            "https://api.ipify.org?format=json",
            timeout=5
        )

        ip = r.json().get("ip")

        PUBLIC_IP = ip or "Unknown"

    except Exception:
        PUBLIC_IP = "Unknown"

    return PUBLIC_IP


# ============================================================
# DELTA CLIENT
# ============================================================

class DeltaClient:

    def __init__(self):

        self.session = requests.Session()

        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20
        )

        self.session.mount(
            "https://",
            adapter
        )

        self.session.mount(
            "http://",
            adapter
        )

        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "XAUTUSD-Supertrend-Bot/5.1",
        })

    def sign(
        self,
        method,
        path,
        query="",
        body=""
    ):

        ts = str(int(time.time()))

        msg = (
            method.upper()
            + ts
            + path
            + query
            + body
        )

        sig = hmac.new(
            API_SECRET.encode(),
            msg.encode(),
            hashlib.sha256
        ).hexdigest()

        return {
            "api-key": API_KEY,
            "signature": sig,
            "timestamp": ts,
            "User-Agent": "XAUTUSD-Supertrend-Bot/5.1",
        }

    def api(
        self,
        method,
        path,
        params=None,
        body=None,
        auth=False
    ):

        params = params or {}

        body_text = (
            json.dumps(
                body,
                separators=(",", ":")
            )
            if body is not None
            else ""
        )

        query = (
            "?"
            + urlencode(
                params,
                doseq=True
            )
            if params
            else ""
        )

        headers = (
            self.sign(
                method,
                path,
                query,
                body_text
            )
            if auth
            else {}
        )

        r = self.session.request(
            method.upper(),
            BASE_URL + path,
            params=params,
            data=(
                body_text
                if body is not None
                else None
            ),
            headers=headers,
            timeout=(4, 12),
        )

        r.raise_for_status()

        data = r.json()

        if data.get("success") is False:
            raise RuntimeError(
                f"Delta API error: {data}"
            )

        return data

    def product(self):

        data = self.api(
            "GET",
            f"/v2/products/{SYMBOL}"
        )

        result = data.get("result")

        if not isinstance(result, dict):
            raise RuntimeError(
                f"Invalid product response: {data}"
            )

        return result

    def position(self, product_id):

        data = self.api(
            "GET",
            "/v2/positions",
            params={
                "product_id": int(product_id)
            },
            auth=True,
        )

        result = data.get(
            "result",
            {}
        )

        pos = {}

        if isinstance(result, dict):

            pos = result

        elif isinstance(result, list):

            for item in result:

                if (
                    isinstance(item, dict)
                    and as_int(
                        item.get("product_id")
                    ) == int(product_id)
                ):
                    pos = item
                    break

        return {
            "size": as_int(
                pos.get("size"),
                0
            ),
            "entry_price": as_float(
                pos.get("entry_price")
                or pos.get("avg_price")
            ),
        }

    def margined_position(
        self,
        product_id
    ):

        try:

            data = self.api(
                "GET",
                "/v2/positions/margined",
                params={
                    "product_ids":
                    str(int(product_id))
                },
                auth=True,
            )

            result = data.get(
                "result",
                []
            )

            if isinstance(result, dict):
                result = [result]

            for item in result:

                if (
                    isinstance(item, dict)
                    and as_int(
                        item.get("product_id")
                    ) == int(product_id)
                ):

                    return {
                        "size": as_int(
                            item.get("size"),
                            0
                        ),
                        "entry_price": as_float(
                            item.get("entry_price")
                        ),
                        "unrealized_pnl": as_float(
                            item.get("unrealized_pnl"),
                            0.0
                        ) or 0.0,
                        "realized_pnl": as_float(
                            item.get("realized_pnl"),
                            0.0
                        ) or 0.0,
                        "margin": as_float(
                            item.get("margin"),
                            0.0
                        ) or 0.0,
                        "liquidation_price": as_float(
                            item.get("liquidation_price")
                        ),
                        "mark_price": as_float(
                            item.get("mark_price")
                        ),
                    }

        except Exception as e:

            logging.debug(
                "Margined position error: %s",
                e
            )

        return {}

    def balance(self):

        data = self.api(
            "GET",
            "/v2/wallet/balances",
            auth=True
        )

        result = data.get(
            "result",
            []
        )

        if isinstance(result, dict):
            result = [result]

        for w in result:

            if not isinstance(w, dict):
                continue

            asset = str(
                w.get(
                    "asset_symbol",
                    ""
                )
            ).upper()

            if asset not in (
                "USD",
                "USDT"
            ):
                continue

            value = w.get(
                "available_balance"
            )

            if value is None:
                value = w.get(
                    "balance"
                )

            if value is not None:
                return Decimal(
                    str(value)
                )

        raise RuntimeError(
            "USD/USDT balance not found."
        )

    def set_leverage(
        self,
        product_id,
        leverage
    ):

        return self.api(
            "POST",
            f"/v2/products/{product_id}/orders/leverage",
            body={
                "leverage": str(
                    int(leverage)
                )
            },
            auth=True,
        )

    def calculate_order_size(
        self,
        product,
        price,
        leverage
    ):

        balance = self.balance()

        if balance <= 0:
            raise RuntimeError(
                "Available balance is zero."
            )

        margin = (
            balance
            * MARGIN_FRACTION
        )

        notional = (
            margin
            * Decimal(str(leverage))
        )

        contract_value = Decimal(
            str(
                product.get(
                    "contract_value"
                )
                or "0.001"
            )
        )

        if contract_value <= 0:
            raise RuntimeError(
                "Invalid contract value."
            )

        raw = (
            notional
            / Decimal(str(price))
            / contract_value
        )

        increment = Decimal(
            str(
                product.get(
                    "lot_size"
                )
                or "1"
            )
        )

        minimum = Decimal(
            str(
                product.get(
                    "min_order_size"
                )
                or increment
            )
        )

        size = (
            raw / increment
        ).to_integral_value(
            rounding=ROUND_DOWN
        ) * increment

        if size < minimum:
            size = minimum

        return int(size)

    def market_entry(
        self,
        product_id,
        direction,
        size
    ):

        side = (
            "buy"
            if direction == "LONG"
            else "sell"
        )

        return self.api(
            "POST",
            "/v2/orders",
            body={
                "product_id": int(product_id),
                "product_symbol": SYMBOL,
                "size": int(size),
                "side": side,
                "order_type": "market_order",
                "client_order_id":
                    self.client_id("entry"),
            },
            auth=True,
        )

    def reduce_only_close(
        self,
        product_id,
        signed_size
    ):

        if signed_size == 0:
            return None

        side = (
            "sell"
            if signed_size > 0
            else "buy"
        )

        return self.api(
            "POST",
            "/v2/orders",
            body={
                "product_id":
                    int(product_id),
                "product_symbol":
                    SYMBOL,
                "size":
                    abs(int(signed_size)),
                "side":
                    side,
                "order_type":
                    "market_order",
                "reduce_only":
                    True,
                "client_order_id":
                    self.client_id("close"),
            },
            auth=True,
        )

    def cancel_all_orders(
        self,
        product_id
    ):

        try:

            return self.api(
                "DELETE",
                "/v2/orders/all",
                body={
                    "product_id":
                        int(product_id)
                },
                auth=True,
            )

        except Exception:
            return None

    def client_id(self, prefix):

        return (
            f"{prefix}_"
            f"{int(time.time()*1000)}_"
            f"{uuid.uuid4().hex[:8]}"
        )[-32:]

    def candles(
        self,
        resolution,
        start_ts,
        end_ts
    ):

        data = self.api(
            "GET",
            "/v2/history/candles",
            params={
                "resolution":
                    resolution,
                "symbol":
                    SYMBOL,
                "start":
                    int(start_ts),
                "end":
                    int(end_ts),
            },
        )

        result = data.get(
            "result",
            []
        )

        return (
            result
            if isinstance(result, list)
            else []
        )


# ============================================================
# BOT
# ============================================================

class Bot:

    def __init__(self):

        self.client = DeltaClient()

        self.product = None
        self.product_id = 0

        self.last_price = None
        self.last_price_time = None

        self.candles = []
        self.current_bucket = None
        self.last_closed_bucket = None

        self.supertrend_direction = None
        self.supertrend_level = None

        self.last_signal = None
        self.signal_initialized = False

        self.bot_running = False

        self.position = None
        self.direction = None

        self.entry_price = None
        self.stop_loss = 0.0

        self.size = 0
        self.leverage = 10

        self.trade_started_at = None
        self.trade_id = None

        self.execution_uncertain = False
        self.order_in_progress = False

        self.lock = threading.RLock()

        self.cached_position = {
            "size": 0,
            "entry_price": None
        }

        self.cached_margin = {}

        self.last_position_refresh = 0.0

        self.weekend_close_attempted = False

        self.load_state()

    # ========================================================
    # STATE
    # ========================================================

    def save(self):

        atomic_write(
            STATE_FILE,
            {
                "last_price":
                    self.last_price,

                "last_price_time":
                    self.last_price_time,

                "bot_running":
                    self.bot_running,

                "position":
                    self.position,

                "direction":
                    self.direction,

                "entry_price":
                    self.entry_price,

                "stop_loss":
                    self.stop_loss,

                "size":
                    self.size,

                "leverage":
                    self.leverage,

                "trade_started_at":
                    self.trade_started_at,

                "trade_id":
                    self.trade_id,

                "execution_uncertain":
                    self.execution_uncertain,

                "supertrend_direction":
                    self.supertrend_direction,

                "supertrend_level":
                    self.supertrend_level,

                "last_signal":
                    self.last_signal,

                "signal_initialized":
                    self.signal_initialized,

                "current_bucket":
                    self.current_bucket,

                "last_closed_bucket":
                    self.last_closed_bucket,

                "weekend_close_attempted":
                    self.weekend_close_attempted,
            }
        )

    def load_state(self):

        s = load_json(
            STATE_FILE,
            {}
        )

        self.last_price = as_float(
            s.get("last_price")
        )

        self.last_price_time = s.get(
            "last_price_time"
        )

        self.bot_running = bool(
            s.get(
                "bot_running",
                False
            )
        )

        self.position = s.get(
            "position"
        )

        self.direction = s.get(
            "direction"
        )

        self.entry_price = as_float(
            s.get("entry_price")
        )

        self.stop_loss = (
            as_float(
                s.get("stop_loss"),
                0.0
            )
            or 0.0
        )

        self.size = as_int(
            s.get("size"),
            0
        )

        self.leverage = (
            as_int(
                s.get("leverage"),
                10
            )
            or 10
        )

        self.trade_started_at = s.get(
            "trade_started_at"
        )

        self.trade_id = s.get(
            "trade_id"
        )

        self.execution_uncertain = bool(
            s.get(
                "execution_uncertain",
                False
            )
        )

        self.supertrend_direction = s.get(
            "supertrend_direction"
        )

        self.supertrend_level = as_float(
            s.get("supertrend_level")
        )

        self.last_signal = s.get(
            "last_signal"
        )

        self.signal_initialized = bool(
            s.get(
                "signal_initialized",
                False
            )
        )

        self.current_bucket = s.get(
            "current_bucket"
        )

        self.last_closed_bucket = s.get(
            "last_closed_bucket"
        )

        self.weekend_close_attempted = bool(
            s.get(
                "weekend_close_attempted",
                False
            )
        )

    # ========================================================
    # PRODUCT
    # ========================================================

    def prepare_product(self):

        if self.product_id:
            return True

        try:

            self.product = self.client.product()

            self.product_id = int(
                self.product["id"]
            )

            logging.info(
                "XAUTUSD product ready | ID=%s",
                self.product_id
            )

            return True

        except Exception as e:

            logging.error(
                "Product error: %s",
                e
            )

            return False

    def contract_value(self):

        return Decimal(
            str(
                (self.product or {}).get(
                    "contract_value"
                )
                or "0.001"
            )
        )

    # ========================================================
    # SUPERTREND
    # ========================================================

    def calculate_supertrend(
        self,
        candles
    ):

        if len(candles) < (
            SUPERTREND_PERIOD + 2
        ):
            return None, None

        highs = [
            float(x["high"])
            for x in candles
        ]

        lows = [
            float(x["low"])
            for x in candles
        ]

        closes = [
            float(x["close"])
            for x in candles
        ]

        n = len(closes)

        p = SUPERTREND_PERIOD
        m = SUPERTREND_MULTIPLIER

        tr = [0.0] * n

        for i in range(n):

            if i == 0:

                tr[i] = (
                    highs[i]
                    - lows[i]
                )

            else:

                tr[i] = max(
                    highs[i] - lows[i],
                    abs(
                        highs[i]
                        - closes[i - 1]
                    ),
                    abs(
                        lows[i]
                        - closes[i - 1]
                    ),
                )

        atr = [None] * n

        if n <= p:
            return None, None

        atr[p] = (
            sum(
                tr[1:p + 1]
            ) / p
        )

        for i in range(
            p + 1,
            n
        ):

            atr[i] = (
                (
                    atr[i - 1]
                    * (p - 1)
                )
                + tr[i]
            ) / p

        upper = [None] * n
        lower = [None] * n
        direction = [None] * n
        supertrend = [None] * n

        first = p

        hl2 = (
            highs[first]
            + lows[first]
        ) / 2.0

        upper[first] = (
            hl2
            + m * atr[first]
        )

        lower[first] = (
            hl2
            - m * atr[first]
        )

        direction[first] = "SELL"

        supertrend[first] = (
            upper[first]
        )

        for i in range(
            first + 1,
            n
        ):

            hl2 = (
                highs[i]
                + lows[i]
            ) / 2.0

            basic_upper = (
                hl2
                + m * atr[i]
            )

            basic_lower = (
                hl2
                - m * atr[i]
            )

            prev_upper = upper[i - 1]
            prev_lower = lower[i - 1]

            prev_close = closes[i - 1]

            upper[i] = (
                basic_upper
                if (
                    basic_upper < prev_upper
                    or prev_close > prev_upper
                )
                else prev_upper
            )

            lower[i] = (
                basic_lower
                if (
                    basic_lower > prev_lower
                    or prev_close < prev_lower
                )
                else prev_lower
            )

            prev_st = supertrend[i - 1]

            if prev_st == prev_upper:

                direction[i] = (
                    "BUY"
                    if closes[i] > upper[i]
                    else "SELL"
                )

            else:

                direction[i] = (
                    "SELL"
                    if closes[i] < lower[i]
                    else "BUY"
                )

            supertrend[i] = (
                lower[i]
                if direction[i] == "BUY"
                else upper[i]
            )

        return (
            direction[-1],
            float(supertrend[-1])
        )

    def calculate_supertrend_series(
        self,
        candles
    ):

        if len(candles) < (
            SUPERTREND_PERIOD + 2
        ):
            return []

        highs = [
            float(x["high"])
            for x in candles
        ]

        lows = [
            float(x["low"])
            for x in candles
        ]

        closes = [
            float(x["close"])
            for x in candles
        ]

        n = len(closes)

        p = SUPERTREND_PERIOD
        m = SUPERTREND_MULTIPLIER

        tr = [0.0] * n

        for i in range(n):

            if i == 0:

                tr[i] = (
                    highs[i]
                    - lows[i]
                )

            else:

                tr[i] = max(
                    highs[i] - lows[i],
                    abs(
                        highs[i]
                        - closes[i - 1]
                    ),
                    abs(
                        lows[i]
                        - closes[i - 1]
                    ),
                )

        atr = [None] * n

        atr[p] = (
            sum(
                tr[1:p + 1]
            ) / p
        )

        for i in range(
            p + 1,
            n
        ):

            atr[i] = (
                (
                    atr[i - 1]
                    * (p - 1)
                )
                + tr[i]
            ) / p

        upper = [None] * n
        lower = [None] * n
        direction = [None] * n
        st = [None] * n

        upper[p] = (
            (
                highs[p]
                + lows[p]
            ) / 2.0
            + m * atr[p]
        )

        lower[p] = (
            (
                highs[p]
                + lows[p]
            ) / 2.0
            - m * atr[p]
        )

        direction[p] = "SELL"
        st[p] = upper[p]

        for i in range(
            p + 1,
            n
        ):

            hl2 = (
                highs[i]
                + lows[i]
            ) / 2.0

            bu = (
                hl2
                + m * atr[i]
            )

            bl = (
                hl2
                - m * atr[i]
            )

            upper[i] = (
                bu
                if (
                    bu < upper[i - 1]
                    or closes[i - 1]
                    > upper[i - 1]
                )
                else upper[i - 1]
            )

            lower[i] = (
                bl
                if (
                    bl > lower[i - 1]
                    or closes[i - 1]
                    < lower[i - 1]
                )
                else lower[i - 1]
            )

            if st[i - 1] == upper[i - 1]:

                direction[i] = (
                    "BUY"
                    if closes[i] > upper[i]
                    else "SELL"
                )

            else:

                direction[i] = (
                    "SELL"
                    if closes[i] < lower[i]
                    else "BUY"
                )

            st[i] = (
                lower[i]
                if direction[i] == "BUY"
                else upper[i]
            )

        return [
            {
                "bucket":
                    candles[i]["bucket"],

                "direction":
                    direction[i],

                "level":
                    float(st[i]),

                "close":
                    closes[i],
            }

            for i in range(
                p,
                n
            )

            if (
                direction[i]
                is not None
                and st[i]
                is not None
            )
        ]

    # ========================================================
    # INITIAL CANDLES
    # ========================================================

    def load_initial_candles(self):

        try:

            end_ts = int(time.time())

            start_ts = (
                end_ts
                - 60 * 300
            )

            raw = self.client.candles(
                "1m",
                start_ts,
                end_ts
            )

            out = []

            current_bucket = (
                int(time.time() // 60)
            )

            for x in raw:

                c = normalize_candle(x)

                if not c:
                    continue

                if (
                    c["bucket"]
                    >= current_bucket
                ):
                    continue

                out.append(c)

            if not out:
                raise RuntimeError(
                    "No CLOSED 1m candles received."
                )

            unique = {
                x["bucket"]: x
                for x in out
            }

            self.candles = sorted(
                unique.values(),
                key=lambda x: x["bucket"]
            )[-250:]

            self.current_bucket = (
                current_bucket
            )

            series = (
                self.calculate_supertrend_series(
                    self.candles
                )
            )

            if not series:
                raise RuntimeError(
                    "Not enough closed candles for Supertrend 10/3."
                )

            last = series[-1]

            self.supertrend_direction = (
                last["direction"]
            )

            self.supertrend_level = (
                last["level"]
            )

            self.last_signal = (
                last["direction"]
            )

            self.signal_initialized = True

            self.last_closed_bucket = (
                last["bucket"]
            )

            logging.info(
                "SUPERTREND BASELINE | candle=%s | close=%.2f | direction=%s | level=%.2f | NO STARTUP TRADE",
                last["bucket"],
                last["close"],
                last["direction"],
                last["level"],
            )

            self.save()

            return True

        except Exception as e:

            logging.error(
                "Initial candles error: %s",
                e
            )

            return False

    # ========================================================
    # EXISTING POSITION
    # ========================================================

    def sync_existing_position(self):

        try:

            pos = self.client.position(
                self.product_id
            )

            size = as_int(
                pos.get("size"),
                0
            )

            entry = as_float(
                pos.get("entry_price")
            )

            self.cached_position = {
                "size": size,
                "entry_price": entry,
            }

            if size != 0:

                self.direction = (
                    "LONG"
                    if size > 0
                    else "SHORT"
                )

                self.position = (
                    self.direction
                )

                self.size = abs(size)

                self.entry_price = entry

                logging.info(
                    "EXISTING POSITION | %s | size=%s | entry=%s",
                    self.direction,
                    self.size,
                    self.entry_price
                )

            else:

                self.direction = None
                self.position = None
                self.size = 0
                self.entry_price = None

            self.save()

            return True

        except Exception as e:

            logging.error(
                "Existing position sync error: %s",
                e
            )

            return False

    # ========================================================
    # WEEKEND FORCE CLOSE
    # ========================================================

    def weekend_force_close(self):

        """
        HARD SAFETY:

        Saturday 05:00 IST onward:
            Close any open position.

        Sunday:
            Keep account FLAT.

        Monday before 05:30:
            Keep account FLAT.

        This function does NOT depend on bot_running.
        Therefore an open position is closed even if the
        dashboard says STOPPED.
        """

        state = weekend_state()

        if state == "PRE_CLOSE":
            return

        if state == "OPEN":
            return

        try:

            if not self.prepare_product():
                logging.error(
                    "Weekend square-off: product unavailable."
                )
                return

            with self.lock:

                if self.order_in_progress:
                    return

                pos = self.client.position(
                    self.product_id
                )

                size = as_int(
                    pos.get("size"),
                    0
                )

                if size == 0:

                    self.weekend_close_attempted = True

                    self.cached_position = {
                        "size": 0,
                        "entry_price": None
                    }

                    self.clear_position()

                    self.save()

                    return

                direction = (
                    "LONG"
                    if size > 0
                    else "SHORT"
                )

                entry = (
                    as_float(
                        pos.get("entry_price")
                    )
                    or self.entry_price
                    or self.last_price
                )

                exit_price = (
                    self.last_price
                    or entry
                    or 0.0
                )

                logging.warning(
                    "WEEKEND SQUARE-OFF START | state=%s | direction=%s | size=%s | entry=%s",
                    state,
                    direction,
                    abs(size),
                    entry
                )

                self.order_in_progress = True

                try:

                    # Cancel any exchange-side orders first.
                    self.client.cancel_all_orders(
                        self.product_id
                    )

                    # Force reduce-only market close.
                    self.client.reduce_only_close(
                        self.product_id,
                        size
                    )

                finally:

                    self.order_in_progress = False

                if not self.wait_until_flat(
                    CLOSE_CONFIRM_TIMEOUT
                ):

                    self.execution_uncertain = True

                    self.save()

                    logging.error(
                        "WEEKEND SQUARE-OFF FAILED | position not confirmed flat."
                    )

                    return

                # Record the weekend close.
                self.record_close(
                    "WEEKEND_SQUARE_OFF",
                    exit_price,
                    abs(size),
                    direction,
                    entry
                )

                self.clear_position()

                self.weekend_close_attempted = True

                self.cached_position = {
                    "size": 0,
                    "entry_price": None
                }

                self.save()

                logging.warning(
                    "WEEKEND SQUARE-OFF CONFIRMED | position FLAT | state=%s",
                    state
                )

        except Exception as e:

            logging.error(
                "Weekend square-off error: %s",
                e
            )

            self.execution_uncertain = True
            self.save()

    # ========================================================
    # WEEKEND RESET
    # ========================================================

    def weekend_scheduler(self):

        logging.info(
            "Weekend scheduler started | close=%s IST | resume=%s IST",
            WEEKEND_CLOSE_TIME.strftime("%H:%M"),
            WEEKEND_RESUME_TIME.strftime("%H:%M")
        )

        last_state = None

        while True:

            try:

                state = weekend_state()

                if state != last_state:

                    logging.info(
                        "MARKET SCHEDULE STATE | %s",
                        state
                    )

                    last_state = state

                if state in (
                    "WEEKEND",
                    "MONDAY_PRE_OPEN"
                ):

                    self.weekend_force_close()

                elif state == "OPEN":

                    # Once Monday 05:30 arrives,
                    # allow normal trading again.

                    if self.weekend_close_attempted:

                        logging.info(
                            "MARKET OPEN | Monday 05:30 IST reached | trading enabled."
                        )

                        self.weekend_close_attempted = False

                        self.execution_uncertain = False

                        self.save()

                time.sleep(
                    WEEKEND_CHECK_INTERVAL
                )

            except Exception as e:

                logging.error(
                    "Weekend scheduler error: %s",
                    e
                )

                time.sleep(
                    WEEKEND_CHECK_INTERVAL
                )

    # ========================================================
    # START / STOP
    # ========================================================

    def start_bot(self):

        with self.lock:

            if not self.prepare_product():

                return {
                    "success": False,
                    "message":
                        "Product load failed."
                }

            # IMPORTANT:
            # If somebody starts/restarts the bot during
            # Saturday/Sunday/Monday pre-open, immediately
            # force account flat.
            if is_market_closed():

                logging.warning(
                    "BOT START REQUEST DURING MARKET CLOSED WINDOW."
                )

                self.weekend_force_close()

            if (
                not self.candles
                and not self.load_initial_candles()
            ):

                return {
                    "success": False,
                    "message":
                        "1m candle initialization failed."
                }

            self.sync_existing_position()

            # Extra weekend safety check after position sync.
            if is_market_closed():

                self.weekend_force_close()

                # Re-sync after possible close.
                self.sync_existing_position()

            self.bot_running = True

            self.execution_uncertain = False

            self.save()

            return {
                "success": True,
                "bot_running": True,
                "message":
                    "Bot started. Weekend protection active.",
            }

    def stop_bot(self):

        with self.lock:

            self.bot_running = False

            try:

                self.prepare_product()

                pos = self.client.position(
                    self.product_id
                )

                size = as_int(
                    pos.get("size"),
                    0
                )

                if size:

                    self.client.cancel_all_orders(
                        self.product_id
                    )

                    self.client.reduce_only_close(
                        self.product_id,
                        size
                    )

                    if not self.wait_until_flat():

                        self.execution_uncertain = True

                        self.save()

                        return {
                            "success": False,
                            "message":
                                "Close not confirmed."
                        }

                self.clear_position()

                self.save()

                return {
                    "success": True,
                    "bot_running": False,
                    "message":
                        "Bot stopped."
                }

            except Exception as e:

                self.execution_uncertain = True

                self.save()

                return {
                    "success": False,
                    "message": str(e)
                }

    # ========================================================
    # POSITION WAIT
    # ========================================================

    def wait_until_flat(
        self,
        timeout=CLOSE_CONFIRM_TIMEOUT
    ):

        deadline = (
            time.time()
            + timeout
        )

        while time.time() < deadline:

            try:

                pos = self.client.position(
                    self.product_id
                )

                if (
                    as_int(
                        pos.get("size"),
                        0
                    ) == 0
                ):
                    return True

            except Exception:
                pass

            time.sleep(
                POLL_INTERVAL
            )

        return False

    def wait_for_position(
        self,
        direction,
        timeout=ENTRY_CONFIRM_TIMEOUT
    ):

        deadline = (
            time.time()
            + timeout
        )

        while time.time() < deadline:

            try:

                pos = self.client.position(
                    self.product_id
                )

                size = as_int(
                    pos.get("size"),
                    0
                )

                if (
                    direction == "LONG"
                    and size > 0
                ):
                    return pos

                if (
                    direction == "SHORT"
                    and size < 0
                ):
                    return pos

            except Exception:
                pass

            time.sleep(
                POLL_INTERVAL
            )

        return None

    # ========================================================
    # CLEAR POSITION
    # ========================================================

    def clear_position(self):

        self.position = None
        self.direction = None
        self.entry_price = None
        self.stop_loss = 0.0
        self.size = 0
        self.trade_started_at = None
        self.trade_id = None
        self.execution_uncertain = False

    # ========================================================
    # PNL
    # ========================================================

    def calculate_trade_pnl(
        self,
        entry,
        exit_price,
        qty,
        direction
    ):

        if (
            entry is None
            or exit_price is None
            or qty <= 0
        ):
            return 0.0

        e = Decimal(str(entry))
        x = Decimal(str(exit_price))
        q = Decimal(str(qty))

        cv = self.contract_value()

        if direction == "LONG":

            pnl = (
                x - e
            ) * q * cv

        else:

            pnl = (
                e - x
            ) * q * cv

        return float(pnl)

    # ========================================================
    # HISTORY
    # ========================================================

    def record_close(
        self,
        reason,
        exit_price,
        size,
        direction,
        entry
    ):

        history = load_history()

        pnl = self.calculate_trade_pnl(
            entry,
            exit_price,
            size,
            direction
        )

        history.append({
            "id":
                f"trade_{int(time.time()*1000)}",

            "date":
                now_ist().strftime(
                    "%Y-%m-%d %H:%M"
                ),

            "symbol":
                SYMBOL,

            "direction":
                direction,

            "entry_price":
                entry,

            "exit_price":
                float(exit_price),

            "size":
                size,

            "reason":
                reason,

            "pnl":
                round(pnl, 8),

            "pnl_type":
                (
                    "PROFIT"
                    if pnl > 0
                    else (
                        "LOSS"
                        if pnl < 0
                        else "FLAT"
                    )
                ),
        })

        save_history(history)

    def history_pnl(
        self,
        history
    ):

        return sum(
            as_float(
                x.get("pnl"),
                0.0
            )
            for x in history
        )

    # ========================================================
    # LIQUIDATION
    # ========================================================

    def liquidation_estimate(
        self,
        entry,
        leverage,
        direction
    ):

        try:

            e = Decimal(str(entry))

            lev = Decimal(
                str(leverage)
            )

            maintenance = Decimal(
                str(
                    (
                        self.product or {}
                    ).get(
                        "maintenance_margin",
                        0
                    )
                )
            ) / Decimal("100")

            fee = Decimal(
                str(
                    (
                        self.product or {}
                    ).get(
                        "taker_commission_rate",
                        0
                    )
                )
            )

            effective = (
                maintenance
                + fee
                + Decimal("0.0010")
            )

            if direction == "LONG":

                return (
                    e
                    * (
                        Decimal("1")
                        - Decimal("1") / lev
                        + effective
                    )
                )

            return (
                e
                * (
                    Decimal("1")
                    + Decimal("1") / lev
                    - effective
                )
            )

        except Exception:
            return None

    def choose_leverage(
        self,
        entry,
        sl,
        direction
    ):

        for lev in range(
            MAX_LEVERAGE,
            MIN_LEVERAGE - 1,
            -10
        ):

            liq = self.liquidation_estimate(
                entry,
                lev,
                direction
            )

            if liq is None:
                continue

            if (
                direction == "LONG"
                and liq < Decimal(str(sl))
            ):
                return lev

            if (
                direction == "SHORT"
                and liq > Decimal(str(sl))
            ):
                return lev

        return MIN_LEVERAGE

    # ========================================================
    # ENTRY
    # ========================================================

    def enter_trade(
        self,
        direction,
        price,
        stop_loss
    ):

        # HARD WEEKEND BLOCK
        if is_market_closed():

            logging.info(
                "ENTRY BLOCKED | market closed | state=%s",
                weekend_state()
            )

            return False

        if (
            self.order_in_progress
            or self.execution_uncertain
            or not self.bot_running
        ):

            return False

        with self.lock:

            self.order_in_progress = True

            try:

                existing = self.client.position(
                    self.product_id
                )

                existing_size = as_int(
                    existing.get("size"),
                    0
                )

                if existing_size != 0:

                    existing_dir = (
                        "LONG"
                        if existing_size > 0
                        else "SHORT"
                    )

                    if existing_dir == direction:

                        return False

                    existing_entry = (
                        as_float(
                            existing.get(
                                "entry_price"
                            )
                        )
                        or price
                    )

                    self.client.cancel_all_orders(
                        self.product_id
                    )

                    self.client.reduce_only_close(
                        self.product_id,
                        existing_size
                    )

                    if not self.wait_until_flat():

                        self.execution_uncertain = True

                        self.save()

                        return False

                    self.record_close(
                        "SUPERTREND_REVERSE",
                        price,
                        abs(existing_size),
                        existing_dir,
                        existing_entry,
                    )

                # Check weekend again immediately before
                # sending actual order.
                if is_market_closed():

                    logging.warning(
                        "ENTRY CANCELLED | market closed before order execution."
                    )

                    return False

                leverage = self.choose_leverage(
                    price,
                    stop_loss,
                    direction
                )

                self.client.set_leverage(
                    self.product_id,
                    leverage
                )

                size = (
                    self.client.calculate_order_size(
                        self.product,
                        price,
                        leverage
                    )
                )

                if size <= 0:

                    raise RuntimeError(
                        "Calculated size is zero."
                    )

                self.client.market_entry(
                    self.product_id,
                    direction,
                    size
                )

                confirmed = (
                    self.wait_for_position(
                        direction
                    )
                )

                if confirmed is None:

                    self.execution_uncertain = True

                    self.save()

                    return False

                confirmed_size = abs(
                    as_int(
                        confirmed.get("size"),
                        0
                    )
                )

                confirmed_entry = (
                    as_float(
                        confirmed.get(
                            "entry_price"
                        )
                    )
                    or price
                )

                self.position = direction
                self.direction = direction

                self.entry_price = confirmed_entry

                self.stop_loss = float(
                    stop_loss
                )

                self.size = confirmed_size

                self.leverage = leverage

                self.trade_started_at = (
                    now_ist().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                )

                self.trade_id = (
                    f"xaut_{int(time.time()*1000)}"
                )

                self.execution_uncertain = False

                self.save()

                logging.info(
                    "ENTRY CONFIRMED | %s | entry=%s | size=%s | leverage=%sx | ST=%s",
                    direction,
                    confirmed_entry,
                    confirmed_size,
                    leverage,
                    stop_loss,
                )

                return True

            except Exception as e:

                logging.error(
                    "ENTRY ERROR: %s",
                    e
                )

                self.execution_uncertain = True

                self.save()

                return False

            finally:

                self.order_in_progress = False

    # ========================================================
    # CLOSE POSITION
    # ========================================================

    def close_position(
        self,
        reason,
        price
    ):

        if self.order_in_progress:
            return False

        with self.lock:

            self.order_in_progress = True

            try:

                pos = self.client.position(
                    self.product_id
                )

                size = as_int(
                    pos.get("size"),
                    0
                )

                if size == 0:

                    self.clear_position()

                    self.save()

                    return True

                entry = (
                    as_float(
                        pos.get(
                            "entry_price"
                        )
                    )
                    or price
                )

                direction = (
                    "LONG"
                    if size > 0
                    else "SHORT"
                )

                self.client.cancel_all_orders(
                    self.product_id
                )

                self.client.reduce_only_close(
                    self.product_id,
                    size
                )

                if not self.wait_until_flat():

                    self.execution_uncertain = True

                    self.save()

                    return False

                self.record_close(
                    reason,
                    price,
                    abs(size),
                    direction,
                    entry
                )

                self.clear_position()

                self.save()

                return True

            except Exception as e:

                logging.error(
                    "CLOSE ERROR: %s",
                    e
                )

                self.execution_uncertain = True

                self.save()

                return False

            finally:

                self.order_in_progress = False

    # ========================================================
    # CLOSED CANDLE
    # ========================================================

    def process_closed_candle(
        self,
        candle
    ):

        with self.lock:

            if (
                not self.bot_running
                or candle is None
            ):
                return

            b = candle["bucket"]

            if (
                self.last_closed_bucket
                == b
            ):
                return

            self.last_closed_bucket = b

            self.candles.append(
                candle
            )

            unique = {
                x["bucket"]: x
                for x in self.candles
            }

            self.candles = sorted(
                unique.values(),
                key=lambda x: x["bucket"]
            )[-250:]

            series = (
                self.calculate_supertrend_series(
                    self.candles
                )
            )

            if not series:
                return

            current = series[-1]

            direction = (
                current["direction"]
            )

            level = (
                current["level"]
            )

            previous = self.last_signal

            self.supertrend_direction = (
                direction
            )

            self.supertrend_level = level

            logging.info(
                "CLOSED 1M CANDLE | bucket=%s | O=%.2f H=%.2f L=%.2f C=%.2f | ST=%s | ST_LEVEL=%.2f | PREV=%s",
                candle["bucket"],
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                direction,
                level,
                previous,
            )

            if (
                not self.signal_initialized
                or previous is None
            ):

                self.last_signal = direction

                self.signal_initialized = True

                self.save()

                return

            # Same direction = NO TRADE
            if direction == previous:

                self.save()

                return

            # Save confirmed flip.
            self.last_signal = direction

            self.save()

            logging.info(
                "CONFIRMED SUPERTREND FLIP | %s -> %s | candle_close=%.2f | st_level=%.2f | ACTION=%s",
                previous,
                direction,
                candle["close"],
                level,
                (
                    "LONG"
                    if direction == "BUY"
                    else "SHORT"
                )
            )

            # HARD WEEKEND CHECK
            if is_market_closed():

                logging.info(
                    "FLIP ignored because market is closed | state=%s",
                    weekend_state()
                )

                return

            signal_price = float(
                candle["close"]
            )

            pos = self.client.position(
                self.product_id
            )

            size = as_int(
                pos.get("size"),
                0
            )

            if direction == "BUY":

                if size < 0:

                    if not self.close_position(
                        "SUPERTREND_SIGNAL_FLIP",
                        self.last_price
                        or signal_price
                    ):
                        return

                    size = 0

                if size == 0:

                    sl = float(level)

                    self.enter_trade(
                        "LONG",
                        self.last_price
                        or signal_price,
                        sl
                    )

            elif direction == "SELL":

                if size > 0:

                    if not self.close_position(
                        "SUPERTREND_SIGNAL_FLIP",
                        self.last_price
                        or signal_price
                    ):
                        return

                    size = 0

                if size == 0:

                    sl = float(level)

                    self.enter_trade(
                        "SHORT",
                        self.last_price
                        or signal_price,
                        sl
                    )

    # ========================================================
    # CANDLE
    # ========================================================

    def on_candle(
        self,
        candle
    ):

        if candle is None:
            return

        with self.lock:

            b = candle["bucket"]

            current_live_bucket = (
                int(time.time() // 60)
            )

            if (
                b > current_live_bucket
            ):
                return

            if self.current_bucket is None:

                self.current_bucket = (
                    current_live_bucket
                )

                if (
                    b
                    == current_live_bucket
                ):
                    self._upsert_candle(
                        candle
                    )

                return

            if (
                b
                == self.current_bucket
            ):

                self._upsert_candle(
                    candle
                )

                return

            if (
                b
                < self.current_bucket
            ):
                return

            old_bucket = (
                self.current_bucket
            )

            old = next(
                (
                    x
                    for x in self.candles
                    if x["bucket"]
                    == old_bucket
                ),
                None
            )

            if old is not None:

                self.process_closed_candle(
                    old
                )

            self.current_bucket = b

            self._upsert_candle(
                candle
            )

    def _upsert_candle(
        self,
        candle
    ):

        found = False

        for i, old in enumerate(
            self.candles
        ):

            if (
                old["bucket"]
                == candle["bucket"]
            ):

                self.candles[i] = candle

                found = True

                break

        if not found:

            self.candles.append(
                candle
            )

        self.candles.sort(
            key=lambda x: x["bucket"]
        )

        self.candles = (
            self.candles[-250:]
        )

    # ========================================================
    # PRICE
    # ========================================================

    def on_price(
        self,
        price
    ):

        try:

            p = float(price)

            if p > 0:

                self.last_price = p

                self.last_price_time = (
                    now_ist().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                )

        except Exception:
            pass

    # ========================================================
    # POSITION REFRESH
    # ========================================================

    def refresh_position(
        self,
        force=False
    ):

        now = time.time()

        if (
            not force
            and now
            - self.last_position_refresh
            < POSITION_REFRESH_SECONDS
        ):
            return

        try:

            self.cached_position = (
                self.client.position(
                    self.product_id
                )
            )

            self.cached_margin = (
                self.client.margined_position(
                    self.product_id
                )
            )

            self.last_position_refresh = now

        except Exception as e:

            logging.debug(
                "Position refresh: %s",
                e
            )

    # ========================================================
    # DASHBOARD
    # ========================================================

    def dashboard(self):

        self.prepare_product()

        self.refresh_position()

        pos = dict(
            self.cached_position
        )

        pos.update({
            k: v
            for k, v
            in self.cached_margin.items()
            if v is not None
        })

        size = as_int(
            pos.get("size"),
            0
        )

        direction = (
            "LONG"
            if size > 0
            else (
                "SHORT"
                if size < 0
                else "FLAT"
            )
        )

        entry = (
            as_float(
                pos.get("entry_price")
            )
            or self.entry_price
            or 0.0
        )

        exchange_pnl = (
            as_float(
                pos.get("unrealized_pnl"),
                0.0
            )
            or 0.0
        )

        if (
            direction != "FLAT"
            and exchange_pnl == 0
            and self.last_price
            and entry
        ):

            live_pnl = (
                self.calculate_trade_pnl(
                    entry,
                    self.last_price,
                    abs(size),
                    direction
                )
            )

        else:

            live_pnl = exchange_pnl

        history = load_history()

        try:

            balance = float(
                self.client.balance()
            )

        except Exception:

            balance = 0.0

        return {

            "success": True,

            "server_ip":
                get_public_ip(),

            "bot_running":
                self.bot_running,

            "market_closed":
                is_market_closed(),

            "market_state":
                weekend_state(),

            "bot": {

                "id":
                    ACCOUNT_ID,

                "account_name":
                    ACCOUNT_NAME,

                "symbol":
                    SYMBOL,

                "status":
                    "ACTIVE"
                    if self.bot_running
                    else "STOPPED",

                "bot_enabled":
                    self.bot_running,

                "balance":
                    balance,

                "last_price":
                    self.last_price
                    or 0.0,

                "last_price_time":
                    self.last_price_time,

                "local_position":
                    direction,

                "size":
                    abs(size),

                "entry_price":
                    entry,

                "stop_loss":
                    (
                        self.supertrend_level
                        if (
                            direction
                            != "FLAT"
                            and self.supertrend_level
                        )
                        else 0.0
                    ),

                "leverage":
                    as_int(
                        pos.get("leverage"),
                        self.leverage
                    )
                    or self.leverage,

                "margin":
                    as_float(
                        pos.get("margin"),
                        0.0
                    )
                    or 0.0,

                "liquidation_price":
                    as_float(
                        pos.get(
                            "liquidation_price"
                        ),
                        0.0
                    )
                    or 0.0,

                "unrealized_pnl":
                    round(
                        live_pnl,
                        8
                    ),

                "exchange_unrealized_pnl":
                    round(
                        exchange_pnl,
                        8
                    ),

                "realized_pnl":
                    as_float(
                        pos.get(
                            "realized_pnl"
                        ),
                        0.0
                    )
                    or 0.0,

                "total_closed_pnl":
                    round(
                        self.history_pnl(
                            history
                        ),
                        8
                    ),

                "supertrend":
                    self.supertrend_direction
                    or "-",

                "supertrend_level":
                    self.supertrend_level
                    or 0.0,

                "last_signal":
                    self.last_signal
                    or "-",

                "execution_uncertain":
                    self.execution_uncertain,

                "strategy":
                    "Supertrend 10/3 - 1m",
            },

            "total_closed_pnl":
                round(
                    self.history_pnl(
                        history
                    ),
                    8
                ),

            "trades":
                history[-50:],
        }


# ============================================================
# GLOBAL BOT
# ============================================================

BOT = Bot()


# ============================================================
# DASHBOARD HTML
# ============================================================

HTML = """<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>XAUTUSD Supertrend</title>

<style>

body{
background:#0b0f19;
color:#e5e7eb;
font-family:Arial;
margin:0;
padding:12px
}

.card{
background:#111827;
border:1px solid #1f2937;
border-radius:10px;
padding:15px;
margin-bottom:12px
}

.grid{
display:grid;
grid-template-columns:1fr 1fr;
gap:12px
}

.row{
display:flex;
justify-content:space-between;
margin:9px 0
}

button{
width:100%;
padding:14px;
border:0;
border-radius:8px;
color:white;
font-weight:bold;
margin-bottom:12px
}

.start{
background:#10b981
}

.stop{
background:#ef4444
}

.big{
font-size:28px;
font-weight:bold
}

.green{
color:#10b981
}

.red{
color:#ef4444
}

table{
width:100%;
border-collapse:collapse;
font-size:12px
}

td,th{
padding:7px;
border-bottom:1px solid #1f2937;
text-align:left;
white-space:nowrap
}

.wrap{
overflow:auto
}

@media(max-width:700px){

.grid{
grid-template-columns:1fr
}

}

</style>

</head>

<body>

<div class="card">

<h2>XAUTUSD Supertrend Bot</h2>

<div>
Server IP:
<span id="ip">-</span>
</div>

<div>
Status:
<span id="status">-</span>
</div>

<div>
Market:
<span id="market">-</span>
</div>

</div>

<button
id="btn"
class="start"
onclick="toggle()">
START BOT
</button>

<div class="grid">

<div class="card">

<h3>LIVE MARKET</h3>

<div class="row">
<span>Price</span>
<b id="price" class="big">$0.00</b>
</div>

<div class="row">
<span>Supertrend</span>
<b id="st">-</b>
</div>

<div class="row">
<span>ST Level</span>
<span id="stl">-</span>
</div>

<div class="row">
<span>Last Signal</span>
<span id="sig">-</span>
</div>

</div>

<div class="card">

<h3>POSITION</h3>

<div class="row">
<span>Direction</span>
<b id="dir">FLAT</b>
</div>

<div class="row">
<span>Size</span>
<span id="size">0</span>
</div>

<div class="row">
<span>Entry</span>
<span id="entry">-</span>
</div>

<div class="row">
<span>SL</span>
<span id="sl">-</span>
</div>

<div class="row">
<span>Leverage</span>
<span id="lev">-</span>
</div>

<div class="row">
<span>Margin</span>
<span id="margin">-</span>
</div>

<div class="row">
<span>Liquidation</span>
<span id="liq">-</span>
</div>

<div class="row">
<span>Live PnL</span>
<b id="pnl">$0.00</b>
</div>

</div>

</div>

<div class="grid">

<div class="card">

<h3>ACCOUNT</h3>

<div class="row">
<span>Balance</span>
<span id="bal">$0.00</span>
</div>

<div class="row">
<span>Closed PnL</span>
<b id="closed">$0.00</b>
</div>

</div>

<div class="card">

<h3>STRATEGY</h3>

<div class="row">
<span>Supertrend</span>
<span>10 / 3</span>
</div>

<div class="row">
<span>Timeframe</span>
<span>1 Minute</span>
</div>

<div class="row">
<span>Entry</span>
<span>FLIP ONLY</span>
</div>

<div class="row">
<span>Weekend Close</span>
<span>Saturday 05:00</span>
</div>

<div class="row">
<span>Resume</span>
<span>Monday 05:30</span>
</div>

</div>

</div>

<div class="card">

<h3>TRADE HISTORY</h3>

<div class="wrap">

<table>

<thead>

<tr>

<th>Date</th>
<th>Dir</th>
<th>Entry</th>
<th>Exit</th>
<th>Size</th>
<th>Reason</th>
<th>PnL</th>

</tr>

</thead>

<tbody id="trades"></tbody>

</table>

</div>

</div>

<script>

let running=false;

const $=id=>
document.getElementById(id);

const money=x=>{

x=Number(x||0);

return (
x<0
?"-$"
:"$"
)
+Math.abs(x).toFixed(2);

};

const px=x=>
Number(x||0)
?Number(x).toFixed(2)
:"-";

async function refresh(){

try{

const r=await fetch(
"/api/dashboard",
{cache:"no-store"}
);

const d=await r.json();

if(!d.success)return;

const b=d.bot;

running=!!b.bot_enabled;

$("ip").textContent=
d.server_ip||"-";

$("status").textContent=
b.status;

$("market").textContent=
d.market_state||"-";

$("btn").textContent=
running
?"STOP BOT"
:"START BOT";

$("btn").className=
running
?"stop"
:"start";

$("price").textContent=
"$"+
Number(
b.last_price||0
).toFixed(2);

$("st").textContent=
b.supertrend||"-";

$("st").className=
b.supertrend==="BUY"
?"green"
:(
b.supertrend==="SELL"
?"red"
:""
);

$("stl").textContent=
px(b.supertrend_level);

$("sig").textContent=
b.last_signal||"-";

$("dir").textContent=
b.local_position;

$("size").textContent=
b.size;

$("entry").textContent=
px(b.entry_price);

$("sl").textContent=
px(b.stop_loss);

$("lev").textContent=
(b.leverage||0)+"x";

$("margin").textContent=
money(b.margin);

$("liq").textContent=
px(b.liquidation_price);

$("pnl").textContent=
money(b.unrealized_pnl);

$("pnl").className=
b.unrealized_pnl>=0
?"green"
:"red";

$("bal").textContent=
money(b.balance);

$("closed").textContent=
money(d.total_closed_pnl);

$("closed").className=
d.total_closed_pnl>=0
?"green"
:"red";

let rows="";

[...(d.trades||[])]
.reverse()
.forEach(t=>{

rows+=`
<tr>
<td>${t.date||""}</td>
<td>${t.direction||""}</td>
<td>${t.entry_price||""}</td>
<td>${t.exit_price||""}</td>
<td>${t.size||""}</td>
<td>${t.reason||""}</td>
<td>${money(t.pnl)}</td>
</tr>
`;

});

$("trades").innerHTML=
rows
||"<tr><td colspan='7'>No trades</td></tr>";

}
catch(e){

console.error(e);

}

}

async function toggle(){

try{

await fetch(
running
?"/api/stop"
:"/api/start",
{
method:"POST"
}
);

refresh();

}
catch(e){

console.error(e);

}

}

setInterval(
refresh,
2000
);

refresh();

</script>

</body>
</html>"""


# ============================================================
# HTTP HANDLER
# ============================================================

class Handler(
    BaseHTTPRequestHandler
):

    def send_json(
        self,
        data,
        status=200
    ):

        raw = json.dumps(
            data,
            default=str
        ).encode("utf-8")

        self.send_response(
            status
        )

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(raw))
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.end_headers()

        self.wfile.write(raw)

    def log_message(
        self,
        fmt,
        *args
    ):
        pass

    def do_GET(self):

        path = urlparse(
            self.path
        ).path

        if path == "/api/health":

            self.send_json({

                "success":
                    True,

                "online":
                    True,

                "time":
                    now_ist().isoformat(),

                "server_ip":
                    get_public_ip(),

                "bot_running":
                    BOT.bot_running,

                "market_closed":
                    is_market_closed(),

                "market_state":
                    weekend_state(),

                "last_price":
                    BOT.last_price
                    or 0.0,
            })

            return

        if path in (
            "/api/dashboard",
            "/api/state",
            "/api/accounts"
        ):

            self.send_json(
                BOT.dashboard()
            )

            return

        if path == "/api/history":

            h = load_history()

            self.send_json({

                "success":
                    True,

                "history":
                    h,

                "total_closed_pnl":
                    BOT.history_pnl(h),

            })

            return

        if path in (
            "/",
            "/index.html"
        ):

            raw = HTML.encode(
                "utf-8"
            )

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(raw))
            )

            self.end_headers()

            self.wfile.write(raw)

            return

        self.send_json(
            {
                "success":
                    False,
                "message":
                    "Not found"
            },
            404
        )

    def do_POST(self):

        path = urlparse(
            self.path
        ).path

        if path == "/api/start":

            self.send_json(
                BOT.start_bot()
            )

            return

        if path == "/api/stop":

            self.send_json(
                BOT.stop_bot()
            )

            return

        self.send_json(
            {
                "success":
                    False,
                "message":
                    "Unknown endpoint"
            },
            404
        )


# ============================================================
# WEBSOCKET
# ============================================================

def ws_subscribe(
    ws,
    channel
):

    ws.send(
        json.dumps({

            "type":
                "subscribe",

            "payload": {

                "channels": [

                    {
                        "name":
                            channel,

                        "symbols":
                            [SYMBOL]
                    }

                ]

            }

        })
    )


def ws_open(ws):

    try:

        ws_subscribe(
            ws,
            "ticker"
        )

        ws_subscribe(
            ws,
            "candlestick_1m"
        )

        logging.info(
            "WebSocket connected | ticker + candlestick_1m | %s",
            SYMBOL
        )

    except Exception as e:

        logging.error(
            "WebSocket subscribe error: %s",
            e
        )


def ws_message(
    ws,
    message
):

    try:

        data = json.loads(
            message
        )

    except Exception:

        return

    if not isinstance(
        data,
        dict
    ):
        return

    msg_type = str(
        data.get(
            "type",
            ""
        )
    ).lower()

    symbol = str(
        data.get(
            "symbol",
            data.get(
                "sy",
                ""
            )
        )
    ).upper()

    if (
        symbol
        and symbol != SYMBOL
    ):
        return

    if msg_type in (
        "ticker",
        "trades"
    ):

        p = (
            data.get("close")
            or data.get("last_price")
            or data.get("price")
            or data.get("p")
        )

        if p is not None:

            BOT.on_price(p)

        return

    if msg_type in (
        "candlestick_1m",
        "candlesticks"
    ):

        c = normalize_candle(
            data
        )

        if c:

            BOT.on_price(
                c["close"]
            )

            BOT.on_candle(c)


def ws_error(
    ws,
    error
):

    logging.warning(
        "WebSocket error: %s",
        error
    )


def ws_close(
    ws,
    code,
    msg
):

    logging.warning(
        "WebSocket closed: %s %s",
        code,
        msg
    )


def websocket_loop():

    while True:

        try:

            ws = websocket.WebSocketApp(

                WS_URL,

                on_open=
                    ws_open,

                on_message=
                    ws_message,

                on_error=
                    ws_error,

                on_close=
                    ws_close,
            )

            ws.run_forever(
                ping_interval=25,
                ping_timeout=10
            )

        except Exception as e:

            logging.error(
                "WebSocket loop error: %s",
                e
            )

        time.sleep(
            RECONNECT_SECONDS
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not acquire_lock():

        logging.error(
            "Another bot process is already running."
        )

        return

    get_public_ip()

    try:

        if not BOT.prepare_product():

            raise RuntimeError(
                "Could not load XAUTUSD product."
            )

        if not BOT.load_initial_candles():

            raise RuntimeError(
                "Could not load initial 1m candles."
            )

        # ====================================================
        # WEEKEND SAFETY AT STARTUP
        # ====================================================

        if is_market_closed():

            logging.warning(
                "STARTUP DURING MARKET CLOSED WINDOW | state=%s",
                weekend_state()
            )

            BOT.weekend_force_close()

        # ====================================================
        # EXISTING POSITION SYNC
        # ====================================================

        BOT.sync_existing_position()

        # If restart happened during weekend and position
        # somehow still exists, close it again.
        if is_market_closed():

            BOT.weekend_force_close()

            BOT.sync_existing_position()

        # ====================================================
        # NORMAL BOT START
        # ====================================================

        result = BOT.start_bot()

        logging.info(
            "BOT START RESULT --> %s",
            result
        )

    except Exception as e:

        logging.error(
            "Startup error: %s",
            e
        )

        BOT.execution_uncertain = True

        BOT.save()

    # ========================================================
    # WEBSOCKET THREAD
    # ========================================================

    threading.Thread(
        target=websocket_loop,
        name="xaut-public-websocket",
        daemon=True,
    ).start()

    # ========================================================
    # WEEKEND SCHEDULER THREAD
    # ========================================================

    threading.Thread(
        target=BOT.weekend_scheduler,
        name="weekend-squareoff-scheduler",
        daemon=True,
    ).start()

    # ========================================================
    # DASHBOARD
    # ========================================================

    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        Handler
    )

    logging.info(
        "Dashboard running on port %s",
        PORT
    )

    try:

        server.serve_forever()

    except KeyboardInterrupt:

        pass

    finally:

        server.server_close()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
