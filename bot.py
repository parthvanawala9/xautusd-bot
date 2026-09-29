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
from datetime import datetime, timedelta, time as dtime
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
# 12-HOUR DUAL SESSIONS CONFIGURATION
# ============================================================

MORNING_SESSION_START = dtime(5, 30)
MORNING_TRADING_START = dtime(5, 45)

EVENING_SESSION_START = dtime(17, 30)
EVENING_TRADING_START = dtime(17, 45)


# ============================================================
# STRATEGY
# ============================================================

MARGIN_FRACTION = Decimal("0.10")

MAX_LEVERAGE = 100
MIN_LEVERAGE = 10

TARGET_COUNT = 10


# ============================================================
# TIMING
# ============================================================

RECONNECT_SECONDS = 5

ENTRY_CONFIRM_TIMEOUT = 10
CLOSE_CONFIRM_TIMEOUT = 10
PARTIAL_CONFIRM_TIMEOUT = 10

POLL_INTERVAL = 0.25


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

CLIENTS_FILE = os.path.join(
    DATA_DIR,
    "xautusd_clients.json"
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


def load_clients():
    data = load_json(
        CLIENTS_FILE,
        []
    )

    return data if isinstance(
        data,
        list
    ) else []


def save_clients(clients):
    atomic_write(
        CLIENTS_FILE,
        clients
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


def current_session_start(dt=None):
    dt = dt or now_ist()
    t = dt.time()

    morning_base = dt.replace(hour=5, minute=30, second=0, microsecond=0)
    evening_base = dt.replace(hour=17, minute=30, second=0, microsecond=0)

    if dtime(5, 30) <= t < dtime(17, 30):
        return morning_base
    elif t >= dtime(17, 30):
        return evening_base
    else:
        return evening_base - timedelta(days=1)


def is_weekend(dt=None):
    dt = dt or now_ist()

    return dt.weekday() in (
        5,
        6
    )


# ============================================================
# PROCESS LOCK
# ============================================================

def acquire_single_process_lock():
    global LOCK_HANDLE

    try:
        import fcntl
    except ImportError:
        return True

    try:
        LOCK_HANDLE = open(
            LOCK_FILE,
            "w",
            encoding="utf-8"
        )

        fcntl.flock(
            LOCK_HANDLE.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB
        )

    except BlockingIOError:
        return False

    except Exception:
        return False

    LOCK_HANDLE.write(
        str(os.getpid())
    )
    LOCK_HANDLE.flush()

    atexit.register(
        release_single_process_lock
    )

    return True


def release_single_process_lock():
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
        response = requests.get(
            "https://api.ipify.org?format=json",
            timeout=5
        )

        ip = response.json().get(
            "ip"
        )

        if ip:
            PUBLIC_IP = ip

            logging.info(
                "RAILWAY OUTBOUND IP --> %s",
                ip
            )

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
            pool_connections=50,
            pool_maxsize=50
        )

        self.session.mount(
            "https://",
            adapter
        )

        self.session.mount(
            "http://",
            adapter
        )

        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "XAUTUSD-Target-Bot/3.2"
            }
        )

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
            API_SECRET.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

        return {
            "api-key": API_KEY,
            "signature": signature,
            "timestamp": timestamp,
            "User-Agent": "XAUTUSD-Target-Bot/3.2"
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
            "?" + urlencode(
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

        response = self.session.request(
            method.upper(),
            BASE_URL + path,
            params=params,
            data=(
                body_text
                if body is not None
                else None
            ),
            headers=headers,
            timeout=(4, 12)
        )

        response.raise_for_status()

        data = response.json()

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

        result = data.get(
            "result"
        )

        if not isinstance(
            result,
            dict
        ):
            raise RuntimeError(
                f"Invalid product response: {data}"
            )

        return result

    def position(
        self,
        product_id
    ):
        try:
            data = self.api(
                "GET",
                "/v2/positions",
                params={
                    "product_id": int(
                        product_id
                    )
                },
                auth=True
            )

            result = data.get(
                "result",
                {}
            )

            position = {}

            if isinstance(
                result,
                dict
            ):
                position = result

            elif isinstance(
                result,
                list
            ):
                for item in result:
                    if not isinstance(
                        item,
                        dict
                    ):
                        continue

                    if (
                        as_int(
                            item.get(
                                "product_id"
                            ),
                            0
                        )
                        == int(product_id)
                    ):
                        position = item
                        break

                if (
                    not position
                    and result
                    and isinstance(
                        result[0],
                        dict
                    )
                ):
                    position = result[0]

            return {
                "size": as_int(
                    position.get(
                        "size"
                    ),
                    0
                ),

                "entry_price": as_float(
                    position.get(
                        "entry_price"
                    )
                    or position.get(
                        "avg_price"
                    )
                ),

                "stop_loss": as_float(
                    position.get(
                        "stop_loss"
                    )
                ),

                "liquidation_price": as_float(
                    position.get(
                        "liquidation_price"
                    )
                ),

                "mark_price": as_float(
                    position.get(
                        "mark_price"
                    )
                ),

                "unrealized_pnl": as_float(
                    position.get(
                        "unrealized_pnl"
                    ),
                    0.0
                ) or 0.0,

                "realized_pnl": as_float(
                    position.get(
                        "realized_pnl"
                    ),
                    0.0
                ) or 0.0,

                "leverage": as_int(
                    position.get(
                        "leverage"
                    )
                    or position.get(
                        "user_leverage"
                    ),
                    0
                ),

                "margin": as_float(
                    position.get(
                        "margin"
                    ),
                    0.0
                ) or 0.0
            }

        except Exception as e:
            logging.warning(
                "Position API error: %s",
                e
            )

            return {
                "size": 0,
                "entry_price": None,
                "stop_loss": None,
                "liquidation_price": None,
                "mark_price": None,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
                "leverage": 0,
                "margin": 0.0
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
                    "product_ids": str(
                        int(product_id)
                    )
                },
                auth=True
            )

            result = data.get(
                "result",
                []
            )

            if isinstance(
                result,
                dict
            ):
                result = [result]

            if not isinstance(
                result,
                list
            ):
                return {}

            for item in result:
                if not isinstance(
                    item,
                    dict
                ):
                    continue

                if (
                    as_int(
                        item.get(
                            "product_id"
                        ),
                        0
                    )
                    == int(product_id)
                ):
                    return {
                        "size": as_int(
                            item.get(
                                "size"
                            ),
                            0
                        ),

                        "entry_price": as_float(
                            item.get(
                                "entry_price"
                            )
                        ),

                        "unrealized_pnl": as_float(
                            item.get(
                                "unrealized_pnl"
                            ),
                            0.0
                        ) or 0.0,

                        "realized_pnl": as_float(
                            item.get(
                                "realized_pnl"
                            ),
                            0.0
                        ) or 0.0,

                        "margin": as_float(
                            item.get(
                                "margin"
                            ),
                            0.0
                        ) or 0.0,

                        "liquidation_price": as_float(
                            item.get(
                                "liquidation_price"
                            )
                        ),

                        "mark_price": as_float(
                            item.get(
                                "mark_price"
                            )
                        )
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

        if isinstance(
            result,
            dict
        ):
            result = [result]

        for wallet in result:
            if not isinstance(
                wallet,
                dict
            ):
                continue

            asset = str(
                wallet.get(
                    "asset_symbol",
                    ""
                )
            ).upper()

            if asset not in (
                "USD",
                "USDT"
            ):
                continue

            value = (
                wallet.get(
                    "available_balance"
                )
                if wallet.get(
                    "available_balance"
                ) is not None
                else wallet.get(
                    "balance"
                )
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
            auth=True
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
            * Decimal(
                str(leverage)
            )
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

        raw_size = (
            notional
            / Decimal(
                str(price)
            )
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

        size_decimal = (
            (
                raw_size
                / increment
            )
            .to_integral_value(
                rounding=ROUND_DOWN
            )
            * increment
        )

        if size_decimal < minimum:
            size_decimal = minimum

        return int(
            size_decimal
        )

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

        body = {
            "product_id": int(
                product_id
            ),
            "product_symbol": SYMBOL,
            "size": int(size),
            "side": side,
            "order_type": "market_order",
            "client_order_id": self.make_client_id(
                "entry"
            )
        }

        return self.api(
            "POST",
            "/v2/orders",
            body=body,
            auth=True
        )

    def reduce_only_market_close(
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

        body = {
            "product_id": int(
                product_id
            ),
            "product_symbol": SYMBOL,
            "size": abs(
                int(signed_size)
            ),
            "side": side,
            "order_type": "market_order",
            "reduce_only": True,
            "client_order_id": self.make_client_id(
                "close"
            )
        }

        return self.api(
            "POST",
            "/v2/orders",
            body=body,
            auth=True
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
                    "product_id": int(
                        product_id
                    )
                },
                auth=True
            )
        except Exception:
            return None

    def make_client_id(
        self,
        prefix
    ):
        return (
            f"{prefix}_"
            f"{int(time.time() * 1000)}_"
            f"{uuid.uuid4().hex[:8]}"
        )[-32:]

    def candles(
        self,
        resolution,
        start_ts,
        end_ts
    ):
        try:
            data = self.api(
                "GET",
                "/v2/history/candles",
                params={
                    "resolution": resolution,
                    "symbol": SYMBOL,
                    "start": int(
                        start_ts
                    ),
                    "end": int(
                        end_ts
                    )
                }
            )

            result = data.get(
                "result",
                []
            )

            return (
                result
                if isinstance(
                    result,
                    list
                )
                else []
            )

        except Exception:
            return []


# ============================================================
# XAU TARGET BOT
# ============================================================

class XAUTTargetBot:

    def __init__(self):

        self.client = DeltaClient()

        self.product = None
        self.product_id = 0

        self.session = None

        self.day_high = None
        self.day_low = None

        self.last_price = None

        self.bot_running = False
        self.trading_armed = False

        self.position = None
        self.direction = None

        self.entry_price = None
        self.stop_loss = 0.0

        self.original_size = 0
        self.remaining_size = 0

        self.leverage = 10

        self.target_hit = [
            False
            for _ in range(
                TARGET_COUNT
            )
        ]

        self.target_quantities = [
            0
            for _ in range(
                TARGET_COUNT
            )
        ]

        self.trade_started_at = None
        self.trade_id = None

        self.execution_uncertain = False
        self.order_in_progress = False

        self.lock = threading.RLock()

        self.load_state()

    def save(self):

        atomic_write(
            STATE_FILE,
            {
                "session": (
                    self.session.isoformat()
                    if self.session
                    else None
                ),

                "day_high": (
                    str(self.day_high)
                    if self.day_high is not None
                    else None
                ),

                "day_low": (
                    str(self.day_low)
                    if self.day_low is not None
                    else None
                ),

                "last_price": self.last_price,

                "bot_running": self.bot_running,

                "trading_armed": self.trading_armed,

                "position": self.position,

                "direction": self.direction,

                "entry_price": self.entry_price,

                "stop_loss": self.stop_loss,

                "original_size": self.original_size,

                "remaining_size": self.remaining_size,

                "leverage": self.leverage,

                "target_hit": self.target_hit,

                "target_quantities": self.target_quantities,

                "trade_started_at": self.trade_started_at,

                "trade_id": self.trade_id,

                "execution_uncertain": self.execution_uncertain
            }
        )

    def load_state(self):

        state = load_json(
            STATE_FILE,
            {}
        )

        try:

            if state.get(
                "session"
            ):
                self.session = datetime.fromisoformat(
                    state["session"]
                )

            if state.get(
                "day_high"
            ) is not None:
                self.day_high = Decimal(
                    str(
                        state["day_high"]
                    )
                )

            if state.get(
                "day_low"
            ) is not None:
                self.day_low = Decimal(
                    str(
                        state["day_low"]
                    )
                )

            self.last_price = as_float(
                state.get(
                    "last_price"
                )
            )

            self.bot_running = bool(
                state.get(
                    "bot_running",
                    False
                )
            )

            self.trading_armed = bool(
                state.get(
                    "trading_armed",
                    False
                )
            )

            self.position = state.get(
                "position"
            )

            self.direction = state.get(
                "direction"
            )

            self.entry_price = as_float(
                state.get(
                    "entry_price"
                )
            )

            self.stop_loss = as_float(
                state.get(
                    "stop_loss"
                ),
                0.0
            ) or 0.0

            self.original_size = as_int(
                state.get(
                    "original_size"
                ),
                0
            )

            self.remaining_size = as_int(
                state.get(
                    "remaining_size"
                ),
                0
            )

            self.leverage = as_int(
                state.get(
                    "leverage"
                ),
                10
            ) or 10

            hits = state.get(
                "target_hit"
            )

            if (
                isinstance(
                    hits,
                    list
                )
                and len(hits)
                == TARGET_COUNT
            ):
                self.target_hit = [
                    bool(x)
                    for x in hits
                ]

            quantities = state.get(
                "target_quantities"
            )

            if (
                isinstance(
                    quantities,
                    list
                )
                and len(quantities)
                == TARGET_COUNT
            ):
                self.target_quantities = [
                    as_int(x)
                    for x in quantities
                ]

            self.trade_started_at = state.get(
                "trade_started_at"
            )

            self.trade_id = state.get(
                "trade_id"
            )

            self.execution_uncertain = bool(
                state.get(
                    "execution_uncertain",
                    False
                )
            )

        except Exception as e:

            logging.error(
                "State load error: %s",
                e
            )

    def prepare_product(self):

        if self.product_id:
            return True

        try:

            self.product = (
                self.client.product()
            )

            self.product_id = int(
                self.product["id"]
            )

            return True

        except Exception as e:

            logging.error(
                "Product error: %s",
                e
            )

            return False

    def contract_value(self):

        if not self.product:
            return Decimal(
                "0.001"
            )

        return Decimal(
            str(
                self.product.get(
                    "contract_value"
                )
                or "0.001"
            )
        )

    def get_session_high_low(
        self,
        session_start
    ):

        candles = self.client.candles(
            "1m",
            int(
                session_start.timestamp()
            ),
            int(
                now_ist().timestamp()
            )
        )

        highest = None
        lowest = None

        for candle in candles:

            try:

                if isinstance(
                    candle,
                    dict
                ):

                    high = Decimal(
                        str(
                            candle.get(
                                "high"
                            )
                        )
                    )

                    low = Decimal(
                        str(
                            candle.get(
                                "low"
                            )
                        )
                    )

                elif (
                    isinstance(
                        candle,
                        list
                    )
                    and len(candle) >= 4
                ):

                    high = Decimal(
                        str(
                            candle[2]
                        )
                    )

                    low = Decimal(
                        str(
                            candle[3]
                        )
                    )

                else:

                    continue

                if (
                    highest is None
                    or high > highest
                ):
                    highest = high

                if (
                    lowest is None
                    or low < lowest
                ):
                    lowest = low

            except Exception:
                continue

        return highest, lowest

    def wait_for_position(
        self,
        expected_direction=None,
        timeout=ENTRY_CONFIRM_TIMEOUT
    ):

        deadline = (
            time.time()
            + timeout
        )

        while time.time() < deadline:

            try:

                position = (
                    self.client.position(
                        self.product_id
                    )
                )

                size = as_int(
                    position.get(
                        "size"
                    ),
                    0
                )

                if size == 0:

                    time.sleep(
                        POLL_INTERVAL
                    )

                    continue

                if (
                    expected_direction
                    == "LONG"
                    and size > 0
                ):
                    return position

                if (
                    expected_direction
                    == "SHORT"
                    and size < 0
                ):
                    return position

                if (
                    expected_direction
                    is None
                ):
                    return position

            except Exception:
                pass

            time.sleep(
                POLL_INTERVAL
            )

        return None

    def wait_for_remaining_size(
        self,
        expected_max_size,
        timeout=PARTIAL_CONFIRM_TIMEOUT
    ):

        deadline = (
            time.time()
            + timeout
        )

        expected_max_size = abs(
            int(
                expected_max_size
            )
        )

        while time.time() < deadline:

            try:

                position = (
                    self.client.position(
                        self.product_id
                    )
                )

                current_size = abs(
                    as_int(
                        position.get(
                            "size"
                        ),
                        0
                    )
                )

                if (
                    current_size
                    <= expected_max_size
                ):
                    return position

            except Exception:
                pass

            time.sleep(
                POLL_INTERVAL
            )

        return None

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

                position = (
                    self.client.position(
                        self.product_id
                    )
                )

                if (
                    as_int(
                        position.get(
                            "size"
                        ),
                        0
                    )
                    == 0
                ):
                    return True

            except Exception:
                pass

            time.sleep(
                POLL_INTERVAL
            )

        return False

    def adopt_exchange_position(
        self,
        position
    ):

        size = as_int(
            position.get(
                "size"
            ),
            0
        )

        if size == 0:
            return

        self.direction = (
            "LONG"
            if size > 0
            else "SHORT"
        )

        self.position = self.direction

        self.remaining_size = abs(
            size
        )

        if self.original_size <= 0:
            self.original_size = abs(
                size
            )

        exchange_entry = as_float(
            position.get(
                "entry_price"
            )
        )

        if exchange_entry:
            self.entry_price = (
                exchange_entry
            )

        exchange_leverage = as_int(
            position.get(
                "leverage"
            ),
            0
        )

        if exchange_leverage > 0:
            self.leverage = (
                exchange_leverage
            )

        if (
            sum(
                self.target_quantities
            ) <= 0
        ):

            self.target_quantities = (
                self.split_into_ten_parts(
                    self.original_size
                )
            )

        if len(
            self.target_hit
        ) != TARGET_COUNT:

            self.target_hit = [
                False
                for _ in range(
                    TARGET_COUNT
                )
            ]

        self.bot_running = True

    def clear_position(self):

        self.position = None
        self.direction = None

        self.entry_price = None
        self.stop_loss = 0.0

        self.original_size = 0
        self.remaining_size = 0

        self.target_hit = [
            False
            for _ in range(
                TARGET_COUNT
            )
        ]

        self.target_quantities = [
            0
            for _ in range(
                TARGET_COUNT
            )
        ]

        self.trade_started_at = None
        self.trade_id = None

        self.execution_uncertain = False

    def recover_missing_strategy_state(self):

        if not self.position or self.entry_price is None:
            return False

        changed = False

        if self.original_size <= 0 and self.remaining_size > 0:
            self.original_size = self.remaining_size
            changed = True

        if (
            sum(self.target_quantities) <= 0
            and self.original_size > 0
        ):
            self.target_quantities = self.split_into_ten_parts(
                self.original_size
            )
            self.target_hit = [
                quantity <= 0
                for quantity in self.target_quantities
            ]
            changed = True

        if self.stop_loss <= 0:

            if self.day_high is None or self.day_low is None:
                try:
                    high, low = self.get_session_high_low(self.session)
                    if high is not None:
                        self.day_high = high
                        changed = True
                    if low is not None:
                        self.day_low = low
                        changed = True
                except Exception:
                    pass

            entry = Decimal(str(self.entry_price))

            if self.direction == "LONG" and self.day_low is not None:
                candidate = Decimal(str(self.day_low))
                if candidate > 0 and candidate < entry:
                    self.stop_loss = float(candidate)
                    changed = True

            elif self.direction == "SHORT" and self.day_high is not None:
                candidate = Decimal(str(self.day_high))
                if candidate > entry:
                    self.stop_loss = float(candidate)
                    changed = True

        if changed:
            self.save()

        return self.stop_loss > 0

    def start_bot(self):

        with self.lock:

            try:

                if not self.prepare_product():

                    raise RuntimeError(
                        "Unable to load XAUTUSD product."
                    )

                self.bot_running = True
                self.execution_uncertain = False
                self.save()

                return {
                    "success": True,
                    "bot_running": True,
                    "message": (
                        "XAUTUSD bot started."
                    )
                }

            except Exception as e:

                self.execution_uncertain = True

                self.save()

                return {
                    "success": False,
                    "bot_running": self.bot_running,
                    "message": (
                        "Startup reconciliation failed."
                    ),
                    "error": str(e)
                }

    def stop_bot(self):

        with self.lock:

            self.bot_running = False

            try:

                self.prepare_product()

                position = (
                    self.client.position(
                        self.product_id
                    )
                )

                size = as_int(
                    position.get(
                        "size"
                    ),
                    0
                )

                if size:

                    self.client.cancel_all_orders(
                        self.product_id
                    )

                    self.client.reduce_only_market_close(
                        self.product_id,
                        size
                    )

                    if not self.wait_until_flat():

                        self.execution_uncertain = True

                        self.save()

                        return {
                            "success": False,
                            "message": (
                                "Position close "
                                "not confirmed."
                            )
                        }

                self.clear_position()

                self.save()

                return {
                    "success": True,
                    "bot_running": False,
                    "message": (
                        "XAUTUSD bot stopped."
                    )
                }

            except Exception as e:

                self.execution_uncertain = True

                self.save()

                return {
                    "success": False,
                    "message": str(e)
                }

    def calculate_liquidation_estimate(
        self,
        entry_price,
        leverage,
        direction
    ):

        entry = Decimal(
            str(entry_price)
        )

        lev = Decimal(
            str(leverage)
        )

        if (
            entry <= 0
            or lev <= 0
        ):
            return None

        maintenance = Decimal(
            "0"
        )

        taker_fee = Decimal(
            "0"
        )

        try:

            maintenance = (
                Decimal(
                    str(
                        self.product.get(
                            "maintenance_margin",
                            0
                        )
                    )
                )
                / Decimal("100")
            )

            taker_fee = Decimal(
                str(
                    self.product.get(
                        "taker_commission_rate",
                        0
                    )
                )
            )

        except Exception as e:

            logging.warning(
                "Liquidation parameter error: %s",
                e
            )

            maintenance = Decimal(
                "0"
            )

            taker_fee = Decimal(
                "0"
            )

        effective = (
            maintenance
            + taker_fee
            + Decimal(
                "0.0010"
            )
        )

        if direction == "LONG":

            return (
                entry
                * (
                    Decimal("1")
                    - (
                        Decimal("1")
                        / lev
                    )
                    + effective
                )
            )

        if direction == "SHORT":

            return (
                entry
                * (
                    Decimal("1")
                    + (
                        Decimal("1")
                        / lev
                    )
                    - effective
                )
            )

        return None

    def leverage_ladder(self):

        return list(
            range(
                MAX_LEVERAGE,
                MIN_LEVERAGE - 1,
                -10
            )
        )

    def choose_safe_leverage(
        self,
        entry_price,
        stop_loss,
        direction
    ):

        for leverage in (
            self.leverage_ladder()
        ):

            liquidation = (
                self.calculate_liquidation_estimate(
                    entry_price,
                    leverage,
                    direction
                )
            )

            if liquidation is None:
                continue

            if (
                direction == "LONG"
                and liquidation
                < Decimal(
                    str(stop_loss)
                )
            ):
                return leverage

            if (
                direction == "SHORT"
                and liquidation
                > Decimal(
                    str(stop_loss)
                )
            ):
                return leverage

        return MIN_LEVERAGE

    def split_into_ten_parts(
        self,
        total_size
    ):

        total_size = int(
            total_size
        )

        if total_size <= 0:

            return [
                0
                for _ in range(
                    TARGET_COUNT
                )
            ]

        base = (
            total_size
            // TARGET_COUNT
        )

        remainder = (
            total_size
            - (
                base
                * TARGET_COUNT
            )
        )

        quantities = [
            base
            for _ in range(
                TARGET_COUNT
            )
        ]

        quantities[-1] += remainder

        return quantities

    def calculate_target_price(
        self,
        target_index
    ):

        if (
            self.entry_price is None
            or self.stop_loss <= 0
            or not self.direction
        ):
            return None

        risk = abs(
            Decimal(
                str(
                    self.entry_price
                )
            )
            - Decimal(
                str(
                    self.stop_loss
                )
            )
        )

        multiple = Decimal(
            str(
                target_index + 1
            )
        )

        entry = Decimal(
            str(
                self.entry_price
            )
        )

        if self.direction == "LONG":

            return float(
                entry
                + (
                    risk
                    * multiple
                )
            )

        if self.direction == "SHORT":

            return float(
                entry
                - (
                    risk
                    * multiple
                )
            )

        return None

    def calculate_trade_pnl(
        self,
        entry_price,
        exit_price,
        quantity,
        direction
    ):

        if (
            entry_price is None
            or exit_price is None
            or quantity <= 0
        ):
            return 0.0

        entry = Decimal(
            str(entry_price)
        )

        exit_decimal = Decimal(
            str(exit_price)
        )

        qty = Decimal(
            str(quantity)
        )

        contract = (
            self.contract_value()
        )

        if direction == "LONG":

            pnl = (
                exit_decimal
                - entry
            ) * qty * contract

        else:

            pnl = (
                entry
                - exit_decimal
            ) * qty * contract

        return float(
            pnl
        )

    def build_targets(self):

        targets = []

        remaining = (
            self.original_size
            or self.remaining_size
        )

        for index in range(
            TARGET_COUNT
        ):

            quantity = (
                self.target_quantities[index]
                if index
                < len(
                    self.target_quantities
                )
                else 0
            )

            target_price = (
                self.calculate_target_price(
                    index
                )
            )

            remaining_after = max(
                0,
                remaining - quantity
            )

            expected_pnl = 0.0

            if (
                target_price is not None
                and quantity > 0
            ):

                expected_pnl = (
                    self.calculate_trade_pnl(
                        self.entry_price,
                        target_price,
                        quantity,
                        self.direction
                    )
                )

            hit = bool(
                self.target_hit[index]
            )

            if hit:

                status = "HIT"

            elif quantity <= 0:

                status = "SKIPPED"

            else:

                status = "PENDING"

            targets.append(
                {
                    "number": index + 1,

                    "label": (
                        f"{index + 1}R"
                    ),

                    "price": (
                        target_price
                        if target_price is not None
                        else 0.0
                    ),

                    "quantity": int(
                        quantity
                    ),

                    "exit_quantity": int(
                        quantity
                    ),

                    "remaining_after": int(
                        remaining_after
                    ),

                    "expected_pnl": round(
                        expected_pnl,
                        8
                    ),

                    "status": status,

                    "hit": hit
                }
            )

            remaining = (
                remaining_after
            )

        return targets

    def active_target_data(self):

        targets = self.build_targets()

        for target in targets:

            if (
                target["status"]
                == "PENDING"
                and target["quantity"] > 0
            ):

                return target

        return None

    def record_partial_trade(
        self,
        target_index,
        quantity,
        exit_price
    ):

        pnl = (
            self.calculate_trade_pnl(
                self.entry_price,
                exit_price,
                quantity,
                self.direction
            )
        )

        history = load_history()

        history.append(
            {
                "id": (
                    f"{self.trade_id}"
                    f"_TARGET_{target_index + 1}_"
                    f"{int(time.time() * 1000)}"
                ),

                "trade_id": self.trade_id,

                "date": now_ist().strftime(
                    "%Y-%m-%d %H:%M"
                ),

                "symbol": SYMBOL,

                "direction": self.direction,

                "entry_price": self.entry_price,

                "exit_price": float(
                    exit_price
                ),

                "size": int(
                    quantity
                ),

                "reason": (
                    f"TARGET_{target_index + 1}R"
                ),

                "target": (
                    target_index + 1
                ),

                "leverage": self.leverage,

                "contract_value": float(
                    self.contract_value()
                ),

                "pnl": round(
                    pnl,
                    8
                ),

                "pnl_type": (
                    "PROFIT"
                    if pnl > 0
                    else (
                        "LOSS"
                        if pnl < 0
                        else "FLAT"
                    )
                )
            }
        )

        save_history(
            history
        )

    def record_full_close(
        self,
        reason,
        exit_price,
        closed_size=None
    ):

        history = load_history()

        size = (
            self.original_size
            if closed_size is None
            else int(
                closed_size
            )
        )

        pnl = (
            self.calculate_trade_pnl(
                self.entry_price,
                exit_price,
                size,
                self.direction
            )
        )

        history.append(
            {
                "id": (
                    f"{self.trade_id}"
                    f"_STOP_"
                    f"{int(time.time() * 1000)}"
                ),

                "trade_id": self.trade_id,

                "date": now_ist().strftime(
                    "%Y-%m-%d %H:%M"
                ),

                "symbol": SYMBOL,

                "direction": self.direction,

                "entry_price": self.entry_price,

                "exit_price": float(
                    exit_price
                ),

                "size": size,

                "reason": reason,

                "trade_type": "STOP",

                "leverage": self.leverage,

                "contract_value": float(
                    self.contract_value()
                ),

                "pnl": round(
                    pnl,
                    8
                ),

                "pnl_type": (
                    "PROFIT"
                    if pnl > 0
                    else (
                        "LOSS"
                        if pnl < 0
                        else "FLAT"
                    )
                )
            }
        )

        save_history(
            history
        )

    def enter_trade(
        self,
        direction,
        price,
        stop_loss
    ):

        if (
            not self.bot_running
            or self.execution_uncertain
            or self.order_in_progress
            or is_weekend()
        ):
            return False

        try:

            existing = (
                self.client.position(
                    self.product_id
                )
            )

            if (
                as_int(
                    existing.get(
                        "size"
                    ),
                    0
                )
                != 0
            ):
                return False

        except Exception:

            return False

        with self.lock:

            self.order_in_progress = True

            try:

                leverage = (
                    self.choose_safe_leverage(
                        price,
                        stop_loss,
                        direction
                    )
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
                        "Calculated order size is zero."
                    )

                self.client.market_entry(
                    self.product_id,
                    direction,
                    size
                )

                confirmed = (
                    self.wait_for_position(
                        expected_direction=direction
                    )
                )

                if confirmed is None:

                    self.execution_uncertain = True

                    self.save()

                    return False

                confirmed_size = abs(
                    as_int(
                        confirmed.get(
                            "size"
                        ),
                        0
                    )
                )

                if confirmed_size <= 0:

                    self.execution_uncertain = True

                    self.save()

                    return False

                self.position = direction

                self.direction = direction

                self.entry_price = (
                    confirmed.get(
                        "entry_price"
                    )
                    or price
                )

                self.stop_loss = float(
                    stop_loss
                )

                self.original_size = (
                    confirmed_size
                )

                self.remaining_size = (
                    confirmed_size
                )

                confirmed_leverage = as_int(
                    confirmed.get(
                        "leverage"
                    ),
                    0
                )

                self.leverage = (
                    confirmed_leverage
                    if confirmed_leverage > 0
                    else leverage
                )

                self.target_quantities = (
                    self.split_into_ten_parts(
                        confirmed_size
                    )
                )

                self.target_hit = [
                    quantity <= 0
                    for quantity
                    in self.target_quantities
                ]

                self.trade_started_at = (
                    now_ist().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                )

                self.trade_id = (
                    f"xaut_"
                    f"{int(time.time() * 1000)}_"
                    f"{uuid.uuid4().hex[:8]}"
                )

                self.execution_uncertain = False

                logging.info(
                    "ENTRY | %s | Entry=%s | SL=%s | "
                    "Size=%s | Leverage=%sx",
                    direction,
                    self.entry_price,
                    self.stop_loss,
                    confirmed_size,
                    self.leverage
                )

                self.save()

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

    def close_partial(
        self,
        target_index,
        quantity,
        exit_price
    ):

        if (
            quantity <= 0
            or self.order_in_progress
            or not self.position
        ):
            return False

        with self.lock:

            self.order_in_progress = True

            try:

                exchange_position = (
                    self.client.position(
                        self.product_id
                    )
                )

                exchange_size = as_int(
                    exchange_position.get(
                        "size"
                    ),
                    0
                )

                if exchange_size == 0:

                    self.clear_position()

                    self.save()

                    return True

                actual_quantity = min(
                    int(quantity),
                    abs(
                        exchange_size
                    )
                )

                if actual_quantity <= 0:
                    return False

                signed_close_size = (
                    actual_quantity
                    if exchange_size > 0
                    else -actual_quantity
                )

                expected_remaining = (
                    abs(
                        exchange_size
                    )
                    - actual_quantity
                )

                self.client.reduce_only_market_close(
                    self.product_id,
                    signed_close_size
                )

                confirmed = (
                    self.wait_for_remaining_size(
                        expected_remaining
                    )
                )

                if confirmed is None:

                    self.execution_uncertain = True

                    self.save()

                    return False

                confirmed_remaining = abs(
                    as_int(
                        confirmed.get(
                            "size"
                        ),
                        0
                    )
                )

                self.target_hit[
                    target_index
                ] = True

                self.remaining_size = (
                    confirmed_remaining
                )

                self.record_partial_trade(
                    target_index,
                    actual_quantity,
                    exit_price
                )

                if all(
                    self.target_hit
                ):

                    if confirmed_remaining != 0:

                        final_position = (
                            self.client.position(
                                self.product_id
                            )
                        )

                        final_size = as_int(
                            final_position.get(
                                "size"
                            ),
                            0
                        )

                        if final_size != 0:

                            self.client.reduce_only_market_close(
                                self.product_id,
                                final_size
                            )

                            self.wait_until_flat()

                    self.clear_position()

                self.save()

                return True

            except Exception as e:

                self.execution_uncertain = True

                self.save()

                return False

            finally:

                self.order_in_progress = False

    def close_all_at_stop(
        self,
        price
    ):

        if self.order_in_progress:
            return False

        with self.lock:

            self.order_in_progress = True

            try:

                exchange_position = (
                    self.client.position(
                        self.product_id
                    )
                )

                exchange_size = as_int(
                    exchange_position.get(
                        "size"
                    ),
                    0
                )

                if exchange_size == 0:

                    self.clear_position()

                    self.save()

                    return True

                close_size = abs(
                    exchange_size
                )

                self.client.cancel_all_orders(
                    self.product_id
                )

                self.client.reduce_only_market_close(
                    self.product_id,
                    exchange_size
                )

                self.wait_until_flat()

                self.record_full_close(
                    "DAY_EXTREME_SL",
                    price,
                    close_size
                )

                self.clear_position()

                self.save()

                return True

            except Exception as e:

                self.execution_uncertain = True

                self.save()

                return False

            finally:

                self.order_in_progress = False

    def check_stop(
        self,
        price
    ):

        if (
            not self.position
            or self.stop_loss <= 0
        ):
            return False

        if (
            self.direction == "LONG"
            and price <= self.stop_loss
        ):

            return self.close_all_at_stop(
                price
            )

        if (
            self.direction == "SHORT"
            and price >= self.stop_loss
        ):

            return self.close_all_at_stop(
                price
            )

        return False

    def check_targets(
        self,
        price
    ):

        if (
            not self.position
            or self.entry_price is None
        ):
            return

        for index in range(
            TARGET_COUNT
        ):

            if self.target_hit[index]:
                continue

            target_price = (
                self.calculate_target_price(
                    index
                )
            )

            if target_price is None:
                return

            reached = (
                price >= target_price
                if self.direction == "LONG"
                else price <= target_price
            )

            if not reached:
                break

            quantity = (
                self.target_quantities[index]
            )

            if quantity <= 0:

                self.target_hit[
                    index
                ] = True

                continue

            success = self.close_partial(
                index,
                quantity,
                price
            )

            if not success:
                break

            if not self.position:
                break

    # ========================================================
    # DUAL SESSION RESET (05:30 AM & 05:30 PM)
    # ========================================================

    def reset_for_new_session(
        self,
        new_session
    ):

        with self.lock:

            try:

                exchange_position = (
                    self.client.position(
                        self.product_id
                    )
                )

                exchange_size = as_int(
                    exchange_position.get(
                        "size"
                    ),
                    0
                )

                if exchange_size != 0:
                    logging.info(
                        "SESSION CHANGE (05:30) | Closing existing live position of size: %s",
                        exchange_size
                    )
                    self.client.cancel_all_orders(
                        self.product_id
                    )
                    self.client.reduce_only_market_close(
                        self.product_id,
                        exchange_size
                    )
                    self.wait_until_flat()

                self.clear_position()

                self.session = new_session
                self.day_high = None
                self.day_low = None
                self.trading_armed = False

                try:

                    high, low = (
                        self.get_session_high_low(
                            new_session
                        )
                    )

                    if high is not None:
                        self.day_high = high

                    if low is not None:
                        self.day_low = low

                except Exception:
                    pass

                logging.info(
                    "NEW 12H DUAL SESSION INITIALIZED | %s | High=%s | Low=%s",
                    new_session,
                    self.day_high,
                    self.day_low
                )

                self.save()

                return True

            except Exception as e:

                logging.error(
                    "Session reset error: %s",
                    e
                )

                self.execution_uncertain = True

                self.save()

                return False

    def update_session_extremes(
        self,
        price
    ):

        price_decimal = Decimal(
            str(price)
        )

        if (
            self.day_high is None
            or price_decimal
            > self.day_high
        ):

            self.day_high = (
                price_decimal
            )

            return True

        if (
            self.day_low is None
            or price_decimal
            < self.day_low
        ):

            self.day_low = (
                price_decimal
            )

            return True

        return False

    def calculate_live_pnl(
        self,
        current_price,
        position_size,
        entry_price,
        direction
    ):

        if (
            current_price is None
            or entry_price is None
            or position_size == 0
        ):
            return 0.0

        current = Decimal(
            str(current_price)
        )

        entry = Decimal(
            str(entry_price)
        )

        qty = Decimal(
            str(
                abs(
                    position_size
                )
            )
        )

        contract = (
            self.contract_value()
        )

        if direction == "LONG":

            pnl = (
                current
                - entry
            ) * qty * contract

        else:

            pnl = (
                entry
                - current
            ) * qty * contract

        return float(
            pnl
        )

    def history_pnl(
        self,
        history=None
    ):

        history = (
            history
            if history is not None
            else load_history()
        )

        total = 0.0

        for item in history:

            total += (
                as_float(
                    item.get(
                        "pnl"
                    ),
                    0.0
                )
                or 0.0
            )

        return total

    def today_pnl(
        self,
        history=None
    ):

        history = (
            history
            if history is not None
            else load_history()
        )

        today = now_ist().strftime(
            "%Y-%m-%d"
        )

        total = 0.0

        for item in history:

            date_text = str(
                item.get(
                    "date",
                    ""
                )
            )

            if not date_text.startswith(
                today
            ):
                continue

            total += (
                as_float(
                    item.get(
                        "pnl"
                    ),
                    0.0
                )
                or 0.0
            )

        return total

    # ========================================================
    # EVALUATE
    # ========================================================

    def evaluate(
        self,
        price
    ):

        with self.lock:

            if (
                not self.bot_running
                or self.execution_uncertain
            ):
                return

            current_time = now_ist()

            if is_weekend(
                current_time
            ):
                return

            try:
                current_price = float(
                    price
                )
            except Exception:
                return

            if current_price <= 0:
                return

            self.last_price = (
                current_price
            )

            if not self.prepare_product():
                return

            current_session = (
                current_session_start(
                    current_time
                )
            )

            if (
                self.session
                != current_session
            ):

                if not self.reset_for_new_session(
                    current_session
                ):
                    return

            if (
                self.day_high is None
                or self.day_low is None
            ):

                high, low = (
                    self.get_session_high_low(
                        self.session
                    )
                )

                if high is not None:
                    self.day_high = high

                if low is not None:
                    self.day_low = low

                if self.day_high is None:
                    self.day_high = Decimal(
                        str(
                            current_price
                        )
                    )

                if self.day_low is None:
                    self.day_low = Decimal(
                        str(
                            current_price
                        )
                    )

                self.save()

            # 12-hour dual sessions trading start buffer (05:30 - 05:45 AM/PM)
            t = current_time.time()
            in_morning_buffer = dtime(5, 30) <= t < dtime(5, 45)
            in_evening_buffer = dtime(17, 30) <= t < dtime(17, 45)

            if in_morning_buffer or in_evening_buffer:
                if self.update_session_extremes(
                    current_price
                ):
                    self.save()

                self.trading_armed = False

                return

            if not self.trading_armed:

                self.trading_armed = True

                self.update_session_extremes(
                    current_price
                )

                self.save()

                return

            exchange_position = (
                self.client.position(
                    self.product_id
                )
            )

            exchange_size = as_int(
                exchange_position.get(
                    "size"
                ),
                0
            )

            if self.position:

                if self.stop_loss <= 0:
                    self.recover_missing_strategy_state()

                if exchange_size == 0:

                    self.clear_position()

                    self.update_session_extremes(
                        current_price
                    )

                    self.save()

                    return

                if self.check_stop(
                    current_price
                ):
                    return

                self.check_targets(
                    current_price
                )

                changed = (
                    self.update_session_extremes(
                        current_price
                    )
                )

                if changed:
                    self.save()

                return

            if exchange_size:

                self.adopt_exchange_position(
                    exchange_position
                )

                self.save()

                return

            previous_high = (
                float(
                    self.day_high
                )
                if self.day_high is not None
                else current_price
            )

            previous_low = (
                float(
                    self.day_low
                )
                if self.day_low is not None
                else current_price
            )

            if current_price > previous_high:

                stop_loss = previous_low

                self.day_high = Decimal(
                    str(
                        current_price
                    )
                )

                self.save()

                self.enter_trade(
                    "LONG",
                    current_price,
                    stop_loss
                )

                return

            if current_price < previous_low:

                stop_loss = previous_high

                self.day_low = Decimal(
                    str(
                        current_price
                    )
                )

                self.save()

                self.enter_trade(
                    "SHORT",
                    current_price,
                    stop_loss
                )

                return

            if self.update_session_extremes(
                current_price
            ):
                self.save()

    def dashboard_data(self):

        self.prepare_product()

        position_data = {
            "size": 0,
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "leverage": self.leverage,
            "margin": 0.0,
            "liquidation_price": 0.0,
            "mark_price": 0.0
        }

        try:

            if self.product_id:

                res_pos = (
                    self.client.position(
                        self.product_id
                    )
                )

                if res_pos:
                    position_data.update(
                        res_pos
                    )

        except Exception:
            pass

        try:

            if self.product_id:

                margined = (
                    self.client.margined_position(
                        self.product_id
                    )
                )

                if margined:

                    for key in (
                        "unrealized_pnl",
                        "realized_pnl",
                        "margin",
                        "liquidation_price",
                        "mark_price"
                    ):

                        if margined.get(
                            key
                        ) is not None:

                            position_data[
                                key
                            ] = margined.get(
                                key
                            )

        except Exception:
            pass

        exchange_size = as_int(
            position_data.get(
                "size"
            ),
            0
        )

        direction = (
            "LONG"
            if exchange_size > 0
            else (
                "SHORT"
                if exchange_size < 0
                else "FLAT"
            )
        )

        entry_price = (
            position_data.get(
                "entry_price"
            )
            or self.entry_price
            or 0.0
        )

        live_pnl = (
            self.calculate_live_pnl(
                self.last_price,
                exchange_size,
                entry_price,
                direction
            )
            if direction != "FLAT"
            else 0.0
        )

        balance_val = 0.0

        try:

            balance_val = float(
                self.client.balance()
            )

        except Exception:

            balance_val = 0.0

        targets = (
            self.build_targets()
            if self.position
            else []
        )

        active_target = (
            self.active_target_data()
            if self.position
            else None
        )

        history = load_history()

        today_pnl = (
            self.today_pnl(
                history
            )
        )

        total_closed_pnl = (
            self.history_pnl(
                history
            )
        )

        local_direction = (
            self.direction
            if self.direction
            else direction
        )

        if exchange_size and not self.position:
            self.adopt_exchange_position(position_data)

        if exchange_size and self.stop_loss <= 0:
            self.recover_missing_strategy_state()

        targets = (
            self.build_targets()
            if exchange_size and self.position
            else targets
        )

        active_target = (
            self.active_target_data()
            if exchange_size and self.position
            else active_target
        )

        bot_obj = {

            "id": ACCOUNT_ID,

            "account_name": ACCOUNT_NAME,

            "symbol": SYMBOL,

            "bot_enabled": self.bot_running,

            "status": (
                "ACTIVE"
                if self.bot_running
                else "STOPPED"
            ),

            "balance": balance_val,

            "last_price": (
                self.last_price
                or 0.0
            ),

            "local_position": (
                local_direction
                if direction != "FLAT"
                else "FLAT"
            ),

            "size": abs(
                exchange_size
            ),

            "entry_price": float(
                entry_price
            ),

            "stop_loss": (
                self.stop_loss
                or position_data.get(
                    "stop_loss"
                )
                or 0.0
            ),

            "leverage": (
                position_data.get(
                    "leverage"
                )
                or self.leverage
                or 10
            ),

            "margin": (
                position_data.get(
                    "margin"
                )
                or 0.0
            ),

            "unrealized_pnl": round(
                live_pnl,
                8
            ),

            "exchange_unrealized_pnl": (
                position_data.get(
                    "unrealized_pnl",
                    0.0
                )
                or 0.0
            ),

            "realized_pnl": (
                position_data.get(
                    "realized_pnl",
                    0.0
                )
                or 0.0
            ),

            "liquidation_price": (
                position_data.get(
                    "liquidation_price"
                )
                or 0.0
            ),

            "day_high": (
                float(
                    self.day_high
                )
                if self.day_high is not None
                else 0.0
            ),

            "day_low": (
                float(
                    self.day_low
                )
                if self.day_low is not None
                else 0.0
            ),

            "targets": targets,

            "active_target": active_target,

            "target_count": TARGET_COUNT,

            "targets_hit": sum(
                1
                for target in targets
                if target["hit"]
            ),

            "today_pnl": round(
                today_pnl,
                8
            ),

            "total_closed_pnl": round(
                total_closed_pnl,
                8
            ),

            "stats": {
                "today": {
                    "total_trades": len(
                        history
                    ),
                    "pnl": round(
                        today_pnl,
                        8
                    )
                }
            },

            "trade_id": self.trade_id,

            "remaining_size": (
                self.remaining_size
            ),

            "original_size": (
                self.original_size
            ),

            "target_hit": (
                self.target_hit
            ),

            "target_quantities": (
                self.target_quantities
            ),

            "trading_armed": (
                self.trading_armed
            ),

            "execution_uncertain": (
                self.execution_uncertain
            ),

            "contract_value": float(
                self.contract_value()
            )
        }

        return {

            "success": True,

            "server_ip": get_public_ip(),

            "bot_running": self.bot_running,

            "bot": bot_obj,

            "bots": [
                bot_obj
            ],

            "targets": targets,

            "active_target": active_target,

            "today_pnl": round(
                today_pnl,
                8
            ),

            "total_closed_pnl": round(
                total_closed_pnl,
                8
            ),

            "trades": history[-50:],

            "clients": load_clients()
        }


BOT = XAUTTargetBot()


def add_client_record(
    payload
):

    clients = load_clients()

    name = str(
        payload.get(
            "name",
            ""
        )
    ).strip()

    client_id = str(
        payload.get(
            "client_id",
            ""
        )
    ).strip()

    if not name:

        return {
            "success": False,
            "message": "Client name required."
        }

    if not client_id:

        client_id = (
            f"client_"
            f"{int(time.time() * 1000)}"
        )

    record = {
        "id": client_id,

        "name": name,

        "account_name": str(
            payload.get(
                "account_name",
                name
            )
        ).strip(),

        "created_at": now_ist().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),

        "active": True
    }

    clients.append(
        record
    )

    save_clients(
        clients
    )

    return {
        "success": True,
        "message": "Client added successfully.",
        "client": record,
        "clients": clients
    }


class DashboardHandler(
    SimpleHTTPRequestHandler
):

    def __init__(
        self,
        *args,
        **kwargs
    ):

        super().__init__(
            *args,
            directory=BASE_DIR,
            **kwargs
        )

    def log_message(
        self,
        format_string,
        *args
    ):

        pass

    def send_json(
        self,
        payload,
        status=200
    ):

        try:

            raw = json.dumps(
                payload,
                default=str
            ).encode(
                "utf-8"
            )

            self.send_response(
                status
            )

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(
                    len(raw)
                )
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

            self.wfile.write(
                raw
            )

        except Exception:
            pass

    def read_json_body(self):

        try:

            length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            if length <= 0:
                return {}

            raw = self.rfile.read(
                length
            )

            return json.loads(
                raw.decode(
                    "utf-8"
                )
            )

        except Exception:

            return {}

    def do_GET(self):

        parsed = urlparse(
            self.path
        )

        path = parsed.path

        if path == "/api/health":

            self.send_json(
                {
                    "success": True,
                    "online": True,
                    "time": now_ist().isoformat(),
                    "server_ip": get_public_ip(),
                    "bot_running": BOT.bot_running
                }
            )

            return

        if path in (
            "/api/dashboard",
            "/api/state",
            "/api/accounts"
        ):

            self.send_json(
                BOT.dashboard_data()
            )

            return

        if path == "/api/history":

            self.send_json(
                {
                    "success": True,
                    "history": load_history()
                }
            )

            return

        if path == "/api/clients":

            self.send_json(
                {
                    "success": True,
                    "clients": load_clients()
                }
            )

            return

        if path == "/api/targets":

            data = BOT.dashboard_data()

            self.send_json(
                {
                    "success": True,
                    "targets": data.get(
                        "targets",
                        []
                    ),
                    "active_target": data.get(
                        "active_target"
                    )
                }
            )

            return

        super().do_GET()

    def do_POST(self):

        parsed = urlparse(
            self.path
        )

        path = parsed.path

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

        if path == "/api/client/add":

            payload = (
                self.read_json_body()
            )

            self.send_json(
                add_client_record(
                    payload
                )
            )

            return

        self.send_json(
            {
                "success": False,
                "message": "Unknown endpoint."
            },
            404
        )

    def do_OPTIONS(self):

        self.send_response(
            204
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET,POST,OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )

        self.end_headers()


def extract_trade(
    message
):

    try:

        data = json.loads(
            message
        )

    except Exception:

        return None, None

    if not isinstance(
        data,
        dict
    ):

        return None, None

    candidates = [
        data
    ]

    if isinstance(
        data.get(
            "payload"
        ),
        dict
    ):

        candidates.append(
            data["payload"]
        )

    if isinstance(
        data.get(
            "data"
        ),
        dict
    ):

        candidates.append(
            data["data"]
        )

    for item in candidates:

        if not isinstance(
            item,
            dict
        ):
            continue

        symbol = (
            item.get(
                "symbol"
            )
            or item.get(
                "product_symbol"
            )
            or item.get(
                "sy"
            )
            or item.get(
                "s"
            )
        )

        price = (
            item.get(
                "price"
            )
            or item.get(
                "last_price"
            )
            or item.get(
                "close"
            )
            or item.get(
                "p"
            )
        )

        if (
            symbol
            and price is not None
        ):

            try:

                return (
                    str(
                        symbol
                    ).upper(),
                    float(
                        price
                    )
                )

            except Exception:

                return None, None

    return None, None


def websocket_on_open(
    ws
):

    payload = {
        "type": "subscribe",
        "payload": {
            "channels": [
                {
                    "name": "trades",
                    "symbols": [
                        SYMBOL
                    ]
                }
            ]
        }
    }

    ws.send(
        json.dumps(
            payload
        )
    )

    logging.info(
        "XAUTUSD websocket connected."
    )


def websocket_on_message(
    ws,
    message
):

    symbol, price = (
        extract_trade(
            message
        )
    )

    if symbol != SYMBOL:
        return

    try:

        BOT.evaluate(
            price
        )

    except Exception as e:

        logging.error(
            "Evaluate error: %s",
            e
        )


def websocket_on_error(
    ws,
    error
):

    logging.warning(
        "Websocket error: %s",
        error
    )


def websocket_on_close(
    ws,
    code,
    message
):

    logging.warning(
        "Websocket closed: %s %s",
        code,
        message
    )


def websocket_loop():

    while True:

        try:

            ws = websocket.WebSocketApp(

                WS_URL,

                on_open=websocket_on_open,

                on_message=websocket_on_message,

                on_error=websocket_on_error,

                on_close=websocket_on_close
            )

            ws.run_forever(
                ping_interval=20,
                ping_timeout=10
            )

        except Exception as e:

            logging.error(
                "Websocket loop error: %s",
                e
            )

        time.sleep(
            RECONNECT_SECONDS
        )


def main():

    if not acquire_single_process_lock():

        logging.error(
            "Another bot process is already running."
        )

        return

    get_public_ip()

    try:

        BOT.prepare_product()

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

    websocket_thread = threading.Thread(
        target=websocket_loop,
        name="xaut-public-websocket",
        daemon=True
    )

    websocket_thread.start()

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        DashboardHandler
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


if __name__ == "__main__":

    main()
