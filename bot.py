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
# STRATEGY (SUPERTREND ONLY CONFIGURATION)
# ============================================================

MARGIN_FRACTION = Decimal("0.10")

MAX_LEVERAGE = 100
MIN_LEVERAGE = 10

# Supertrend Parameters (1-minute timeframe)
SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = Decimal("3.0")


# ============================================================
# TIMING
# ============================================================

RECONNECT_SECONDS = 5
ENTRY_CONFIRM_TIMEOUT = 10
CLOSE_CONFIRM_TIMEOUT = 10
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
                "User-Agent": "XAUTUSD-Supertrend-Bot/4.4"
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
            "User-Agent": "XAUTUSD-Supertrend-Bot/4.4"
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
# XAUT SUPERTREND BOT
# ============================================================

class XAUTSupertrendBot:

    def __init__(self):

        self.client = DeltaClient()

        self.product = None
        self.product_id = 0

        self.last_price = None

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

        self.load_state()

    def save(self):
        atomic_write(
            STATE_FILE,
            {
                "last_price": self.last_price,
                "bot_running": self.bot_running,
                "position": self.position,
                "direction": self.direction,
                "entry_price": self.entry_price,
                "stop_loss": self.stop_loss,
                "size": self.size,
                "leverage": self.leverage,
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
            self.last_price = as_float(state.get("last_price"))
            self.bot_running = bool(state.get("bot_running", False))
            self.position = state.get("position")
            self.direction = state.get("direction")
            self.entry_price = as_float(state.get("entry_price"))
            self.stop_loss = as_float(state.get("stop_loss"), 0.0) or 0.0
            self.size = as_int(state.get("size"), 0)
            self.leverage = as_int(state.get("leverage"), 10) or 10
            self.trade_started_at = state.get("trade_started_at")
            self.trade_id = state.get("trade_id")
            self.execution_uncertain = bool(state.get("execution_uncertain", False))
        except Exception as e:
            logging.error("State load error: %s", e)

    def prepare_product(self):
        if self.product_id:
            return True
        try:
            self.product = self.client.product()
            self.product_id = int(self.product["id"])
            return True
        except Exception as e:
            logging.error("Product error: %s", e)
            return False

    def contract_value(self):
        if not self.product:
            return Decimal("0.001")
        return Decimal(str(self.product.get("contract_value") or "0.001"))

    def calculate_supertrend(self, candles):
        if not candles or len(candles) < SUPERTREND_PERIOD:
            return None, None

        highs = []
        lows = []
        closes = []

        for c in candles:
            try:
                if isinstance(c, dict):
                    highs.append(float(c.get("high", 0)))
                    lows.append(float(c.get("low", 0)))
                    closes.append(float(c.get("close", 0)))
                elif isinstance(c, list) and len(c) >= 5:
                    highs.append(float(c[2]))
                    lows.append(float(c[3]))
                    closes.append(float(c[4]))
            except Exception:
                continue

        if len(closes) < SUPERTREND_PERIOD:
            return None, None

        atr = []
        for i in range(len(closes)):
            if i == 0:
                tr = highs[i] - lows[i]
            else:
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1])
                )
            atr.append(tr)

        period = SUPERTREND_PERIOD
        multiplier = float(SUPERTREND_MULTIPLIER)

        supertrend_dir = "BUY"
        final_upperband = 0.0
        final_lowerband = 0.0

        for i in range(period, len(closes)):
            current_atr = sum(atr[i - period + 1:i + 1]) / period
            hl2 = (highs[i] + lows[i]) / 2.0
            basic_upperband = hl2 + (multiplier * current_atr)
            basic_lowerband = hl2 - (multiplier * current_atr)

            if i == period:
                final_upperband = basic_upperband
                final_lowerband = basic_lowerband
            
            if basic_upperband < final_upperband or closes[i - 1] > final_upperband:
                final_upperband = basic_upperband

            if basic_lowerband > final_lowerband or closes[i - 1] < final_lowerband:
                final_lowerband = basic_lowerband

            if i == period:
                supertrend_dir = "BUY" if closes[i] >= final_lowerband else "SELL"
            else:
                prev_dir = supertrend_dir
                if prev_dir == "BUY":
                    if closes[i] <= final_lowerband:
                        supertrend_dir = "SELL"
                    else:
                        supertrend_dir = "BUY"
                else:
                    if closes[i] >= final_upperband:
                        supertrend_dir = "BUY"
                    else:
                        supertrend_dir = "SELL"

        return supertrend_dir, final_lowerband if supertrend_dir == "BUY" else final_upperband

    def wait_for_position(self, expected_direction=None, timeout=ENTRY_CONFIRM_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                position = self.client.position(self.product_id)
                size = as_int(position.get("size"), 0)
                if size == 0:
                    time.sleep(POLL_INTERVAL)
                    continue
                if expected_direction == "LONG" and size > 0:
                    return position
                if expected_direction == "SHORT" and size < 0:
                    return position
                if expected_direction is None:
                    return position
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)
        return None

    def wait_until_flat(self, timeout=CLOSE_CONFIRM_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                position = self.client.position(self.product_id)
                if as_int(position.get("size"), 0) == 0:
                    return True
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)
        return False

    def clear_position(self):
        self.position = None
        self.direction = None
        self.entry_price = None
        self.stop_loss = 0.0
        self.size = 0
        self.trade_started_at = None
        self.trade_id = None
        self.execution_uncertain = False

    def start_bot(self):
        with self.lock:
            try:
                if not self.prepare_product():
                    raise RuntimeError("Unable to load XAUTUSD product.")
                self.bot_running = True
                self.execution_uncertain = False
                self.save()
                return {
                    "success": True,
                    "bot_running": True,
                    "message": "XAUTUSD Supertrend bot started."
                }
            except Exception as e:
                self.execution_uncertain = True
                self.save()
                return {
                    "success": False,
                    "bot_running": self.bot_running,
                    "message": "Startup failed.",
                    "error": str(e)
                }

    def stop_bot(self):
        with self.lock:
            self.bot_running = False
            try:
                self.prepare_product()
                position = self.client.position(self.product_id)
                size = as_int(position.get("size"), 0)
                if size:
                    self.client.cancel_all_orders(self.product_id)
                    self.client.reduce_only_market_close(self.product_id, size)
                    if not self.wait_until_flat():
                        self.execution_uncertain = True
                        self.save()
                        return {"success": False, "message": "Position close not confirmed."}
                self.clear_position()
                self.save()
                return {"success": True, "bot_running": False, "message": "XAUTUSD bot stopped."}
            except Exception as e:
                self.execution_uncertain = True
                self.save()
                return {"success": False, "message": str(e)}

    def calculate_liquidation_estimate(self, entry_price, leverage, direction):
        entry = Decimal(str(entry_price))
        lev = Decimal(str(leverage))
        if entry <= 0 or lev <= 0:
            return None

        maintenance = Decimal("0")
        taker_fee = Decimal("0")
        try:
            maintenance = Decimal(str(self.product.get("maintenance_margin", 0))) / Decimal("100")
            taker_fee = Decimal(str(self.product.get("taker_commission_rate", 0)))
        except Exception:
            pass

        effective = maintenance + taker_fee + Decimal("0.0010")

        if direction == "LONG":
            return entry * (Decimal("1") - (Decimal("1") / lev) + effective)
        if direction == "SHORT":
            return entry * (Decimal("1") + (Decimal("1") / lev) - effective)
        return None

    def leverage_ladder(self):
        return list(range(MAX_LEVERAGE, MIN_LEVERAGE - 1, -10))

    def choose_safe_leverage(self, entry_price, stop_loss, direction):
        for leverage in self.leverage_ladder():
            liquidation = self.calculate_liquidation_estimate(entry_price, leverage, direction)
            if liquidation is None:
                continue
            if direction == "LONG" and liquidation < Decimal(str(stop_loss)):
                return leverage
            if direction == "SHORT" and liquidation > Decimal(str(stop_loss)):
                return leverage
        return MIN_LEVERAGE

    def calculate_trade_pnl(self, entry_price, exit_price, quantity, direction):
        if entry_price is None or exit_price is None or quantity <= 0:
            return 0.0
        entry = Decimal(str(entry_price))
        exit_decimal = Decimal(str(exit_price))
        qty = Decimal(str(quantity))
        contract = self.contract_value()

        if direction == "LONG":
            pnl = (exit_decimal - entry) * qty * contract
        else:
            pnl = (entry - exit_decimal) * qty * contract
        return float(pnl)

    def calculate_live_pnl(self, current_price, size, entry_price, direction):
        if not current_price or not entry_price or size == 0:
            return 0.0
        return self.calculate_trade_pnl(entry_price, current_price, abs(size), direction)

    def history_pnl(self, history):
        total = 0.0
        for item in history:
            total += as_float(item.get("pnl"), 0.0)
        return total

    def record_full_close(self, reason, exit_price, closed_size, direction, entry_price):
        history = load_history()
        pnl = self.calculate_trade_pnl(entry_price, exit_price, closed_size, direction)

        history.append({
            "id": f"trade_{int(time.time() * 1000)}",
            "date": now_ist().strftime("%Y-%m-%d %H:%M"),
            "symbol": SYMBOL,
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": float(exit_price),
            "size": closed_size,
            "reason": reason,
            "pnl": round(pnl, 8),
            "pnl_type": "PROFIT" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT")
        })
        save_history(history)

    def enter_trade(self, direction, price, stop_loss):
        if not self.bot_running or self.execution_uncertain or self.order_in_progress or is_market_closed():
            return False

        with self.lock:
            self.order_in_progress = True
            try:
                existing = self.client.position(self.product_id)
                existing_size = as_int(existing.get("size"), 0)

                if existing_size != 0:
                    current_dir = "LONG" if existing_size > 0 else "SHORT"
                    if current_dir == direction:
                        return False
                    else:
                        ex_entry = as_float(existing.get("entry_price")) or price
                        self.client.cancel_all_orders(self.product_id)
                        self.client.reduce_only_market_close(self.product_id, existing_size)
                        self.wait_until_flat()
                        self.record_full_close("SUPERTREND_REVERSE", price, abs(existing_size), current_dir, ex_entry)

                leverage = self.choose_safe_leverage(price, stop_loss, direction)
                self.client.set_leverage(self.product_id, leverage)

                size = self.client.calculate_order_size(self.product, price, leverage)
                if size <= 0:
                    raise RuntimeError("Calculated order size is zero.")

                self.client.market_entry(self.product_id, direction, size)
                confirmed = self.wait_for_position(expected_direction=direction)

                if confirmed is None:
                    self.execution_uncertain = True
                    self.save()
                    return False

                confirmed_size = abs(as_int(confirmed.get("size"), 0))
                if confirmed_size <= 0:
                    self.execution_uncertain = True
                    self.save()
                    return False

                self.position = direction
                self.direction = direction
                self.entry_price = confirmed.get("entry_price") or price
                self.stop_loss = float(stop_loss)
                self.size = confirmed_size
                self.leverage = as_int(confirmed.get("leverage"), 0) or leverage
                self.trade_started_at = now_ist().strftime("%Y-%m-%d %H:%M:%S")
                self.trade_id = f"xaut_{int(time.time() * 1000)}"
                self.execution_uncertain = False

                logging.info(
                    "SUPERTREND ENTRY | %s | Entry=%s | SL=%s | Size=%s | Leverage=%sx",
                    direction, self.entry_price, self.stop_loss, confirmed_size, self.leverage
                )
                self.save()
                return True

            except Exception as e:
                logging.error("ENTRY ERROR: %s", e)
                self.execution_uncertain = True
                self.save()
                return False
            finally:
                self.order_in_progress = False

    def close_all_position(self, reason, price):
        if self.order_in_progress:
            return False

        with self.lock:
            self.order_in_progress = True
            try:
                exchange_position = self.client.position(self.product_id)
                exchange_size = as_int(exchange_position.get("size"), 0)
                ex_entry = as_float(exchange_position.get("entry_price")) or price
                ex_dir = "LONG" if exchange_size > 0 else "SHORT"

                if exchange_size == 0:
                    self.clear_position()
                    self.save()
                    return True

                close_size = abs(exchange_size)
                self.client.cancel_all_orders(self.product_id)
                self.client.reduce_only_market_close(self.product_id, exchange_size)
                self.wait_until_flat()

                self.record_full_close(reason, price, close_size, ex_dir, ex_entry)
                self.clear_position()
                self.save()
                return True

            except Exception as e:
                self.execution_uncertain = True
                self.save()
                return False
            finally:
                self.order_in_progress = False

    def evaluate(self, price):
        with self.lock:
            if not self.bot_running or self.execution_uncertain:
                return

            if is_market_closed(now_ist()):
                return

            try:
                current_price = float(price)
            except Exception:
                return

            if current_price <= 0:
                return

            self.last_price = current_price

            if not self.prepare_product():
                return

            end_ts = int(time.time())
            start_ts = end_ts - (60 * 100)
            candles = self.client.candles("1m", start_ts, end_ts)

            if not candles:
                return

            st_dir, st_level = self.calculate_supertrend(candles)
            if not st_dir:
                return

            if st_level > 0:
                self.stop_loss = float(st_level)

            exchange_position = self.client.position(self.product_id)
            exchange_size = as_int(exchange_position.get("size"), 0)

            # CORRECTED LOGIC: BUY (Green) -> Open LONG, SELL (Red) -> Open SHORT
            if st_dir == "BUY":
                if exchange_size < 0:
                    logging.info("Supertrend turned BUY. Closing short and reversing to LONG.")
                    self.close_all_position("SUPERTREND_SIGNAL_FLIP", current_price)
                
                exchange_position = self.client.position(self.product_id)
                exchange_size = as_int(exchange_position.get("size"), 0)
                if exchange_size == 0:
                    stop_loss = st_level if st_level > 0 else current_price * 0.99
                    logging.info("Supertrend BUY Signal. Opening LONG.")
                    self.enter_trade("LONG", current_price, stop_loss)

            elif st_dir == "SELL":
                if exchange_size > 0:
                    logging.info("Supertrend turned SELL. Closing long and reversing to SHORT.")
                    self.close_all_position("SUPERTREND_SIGNAL_FLIP", current_price)
                
                exchange_position = self.client.position(self.product_id)
                exchange_size = as_int(exchange_position.get("size"), 0)
                if exchange_size == 0:
                    stop_loss = st_level if st_level > 0 else current_price * 1.01
                    logging.info("Supertrend SELL Signal. Opening SHORT.")
                    self.enter_trade("SHORT", current_price, stop_loss)

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
                res_pos = self.client.position(self.product_id)
                if res_pos:
                    position_data.update(res_pos)
        except Exception:
            pass

        try:
            if self.product_id:
                margined = self.client.margined_position(self.product_id)
                if margined:
                    for key in ("unrealized_pnl", "realized_pnl", "margin", "liquidation_price", "mark_price"):
                        if margined.get(key) is not None:
                            position_data[key] = margined.get(key)
        except Exception:
            pass

        exchange_size = as_int(position_data.get("size"), 0)
        direction = "LONG" if exchange_size > 0 else ("SHORT" if exchange_size < 0 else "FLAT")
        entry_price = position_data.get("entry_price") or self.entry_price or 0.0

        live_pnl = self.calculate_live_pnl(self.last_price, exchange_size, entry_price, direction) if direction != "FLAT" else 0.0

        balance_val = 0.0
        try:
            balance_val = float(self.client.balance())
        except Exception:
            balance_val = 0.0

        history = load_history()
        total_closed_pnl_val = self.history_pnl(history)

        current_sl = self.stop_loss or position_data.get("stop_loss") or 0.0

        bot_obj = {
            "id": ACCOUNT_ID,
            "account_name": ACCOUNT_NAME,
            "symbol": SYMBOL,
            "bot_enabled": self.bot_running,
            "status": "ACTIVE" if self.bot_running else "STOPPED",
            "balance": balance_val,
            "last_price": self.last_price or 0.0,
            "local_position": direction,
            "size": abs(exchange_size),
            "entry_price": float(entry_price),
            "stop_loss": float(current_sl),
            "leverage": position_data.get("leverage") or self.leverage or 10,
            "margin": position_data.get("margin") or 0.0,
            "unrealized_pnl": round(live_pnl, 8),
            "realized_pnl": position_data.get("realized_pnl", 0.0) or 0.0,
            "liquidation_price": position_data.get("liquidation_price") or 0.0,
            "total_closed_pnl": round(total_closed_pnl_val, 8),
            "contract_value": float(self.contract_value())
        }

        return {
            "success": True,
            "server_ip": get_public_ip(),
            "bot_running": self.bot_running,
            "bot": bot_obj,
            "bots": [bot_obj],
            "total_closed_pnl": round(total_closed_pnl_val, 8),
            "trades": history[-50:]
        }


BOT = XAUTSupertrendBot()


class DashboardHandler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def log_message(self, format_string, *args):
        pass

    def send_json(self, payload, status=200):
        try:
            raw = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(raw)
        except Exception:
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            self.send_json({
                "success": True,
                "online": True,
                "time": now_ist().isoformat(),
                "server_ip": get_public_ip(),
                "bot_running": BOT.bot_running
            })
            return

        if path in ("/api/dashboard", "/api/state", "/api/accounts"):
            self.send_json(BOT.dashboard_data())
            return

        if path == "/api/history":
            self.send_json({
                "success": True,
                "history": load_history(),
                "total_closed_pnl": BOT.history_pnl(load_history())
            })
            return

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>XAUTUSD Supertrend Bot</title>
    <style>
        body { background-color: #0b0f19; color: #e2e8f0; font-family: Arial, sans-serif; margin: 0; padding: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; background: #111827; padding: 15px 20px; border-radius: 8px; margin-bottom: 20px; }
        .btn-toggle { padding: 12px 24px; font-weight: bold; border: none; border-radius: 6px; cursor: pointer; color: white; width: 100%%; font-size: 16px; margin-bottom: 20px; }
        .btn-start { background-color: #10b981; }
        .btn-stop { background-color: #ef4444; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
        .card { background: #111827; padding: 20px; border-radius: 8px; border: 1px solid #1f2937; }
        .card h3 { margin-top: 0; color: #38bdf8; border-bottom: 1px solid #1f2937; padding-bottom: 10px; }
        .row { display: flex; justify-content: space-between; margin: 8px 0; }
        table { width: 100%%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #1f2937; font-size: 14px; }
        th { color: #9ca3af; }
        .profit { color: #10b981; }
        .loss { color: #ef4444; }
        .ip-badge { background: #1f2937; padding: 4px 8px; border-radius: 4px; font-family: monospace; color: #38bdf8; }
    </style>
</head>
<body>
    <div class="header">
        <div id="status-bar">Status: Loading... | Balance: $0.00 | LTP: $0.00</div>
        <div>Server IP: <span id="server-ip" class="ip-badge">Loading...</span></div>
    </div>

    <button id="toggle-btn" class="btn-toggle btn-start" onclick="toggleBot()">START BOT</button>

    <div class="grid">
        <div class="card">
            <h3>LIVE POSITION</h3>
            <div class="row"><span>Dir:</span> <span id="pos-dir">-</span></div>
            <div class="row"><span>Size:</span> <span id="pos-size">0</span></div>
            <div class="row"><span>Entry:</span> <span id="pos-entry">-</span></div>
            <div class="row"><span>Stop Loss:</span> <span id="pos-sl">-</span></div>
            <div class="row"><span>Leverage:</span> <span id="pos-lev">-</span></div>
            <div class="row"><span>Margin:</span> <span id="pos-margin">-</span></div>
            <div class="row"><span>Liquidation:</span> <span id="pos-liq">-</span></div>
            <div class="row"><span>PnL:</span> <span id="pos-pnl">-</span></div>
        </div>

        <div class="card">
            <h3>SESSION & STRATEGY</h3>
            <div class="row"><span>Strategy:</span> <span>Supertrend (1m)</span></div>
            <div class="row"><span>Timeframe:</span> <span>1 Minute</span></div>
            <div class="row"><span>Total Lifetime PnL:</span> <span id="total-pnl" style="font-weight: bold;">$0.00</span></div>
        </div>
    </div>

    <div class="card">
        <h3>TRADE HISTORY (Recent)</h3>
        <table>
            <thead>
                <tr>
                    <th>Date</th>
                    <th>Symbol</th>
                    <th>Direction</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>Size</th>
                    <th>Reason</th>
                    <th>PnL</th>
                </tr>
            </thead>
            <tbody id="trades-table">
                <tr><td colspan="8" style="text-align: center;">No trades recorded yet.</td></tr>
            </tbody>
        </table>
    </div>

    <script>
        let isRunning = false;

        async function fetchDashboard() {
            try {
                let res = await fetch('/api/dashboard');
                let data = await res.json();
                if (data.success && data.bot) {
                    let b = data.bot;
                    isRunning = b.bot_enabled;
                    
                    document.getElementById('status-bar').innerText = `Status: ${b.status} | Balance: $${b.balance.toFixed(2)} | LTP: $${b.last_price.toFixed(2)}`;
                    document.getElementById('server-ip').innerText = data.server_ip || 'Unknown';
                    
                    let btn = document.getElementById('toggle-btn');
                    if (isRunning) {
                        btn.innerText = "STOP BOT";
                        btn.className = "btn-toggle btn-stop";
                    } else {
                        btn.innerText = "START BOT";
                        btn.className = "btn-toggle btn-start";
                    }

                    document.getElementById('pos-dir').innerText = b.local_position;
                    document.getElementById('pos-size').innerText = b.size;
                    document.getElementById('pos-entry').innerText = b.entry_price ? b.entry_price.toFixed(2) : '-';
                    document.getElementById('pos-sl').innerText = b.stop_loss ? b.stop_loss.toFixed(2) : '-';
                    document.getElementById('pos-lev').innerText = b.leverage + 'x';
                    document.getElementById('pos-margin').innerText = '$' + b.margin.toFixed(2);
                    document.getElementById('pos-liq').innerText = b.liquidation_price ? b.liquidation_price.toFixed(2) : '-';
                    
                    let pnlEl = document.getElementById('pos-pnl');
                    let pnlVal = b.unrealized_pnl;
                    pnlEl.innerText = (pnlVal >= 0 ? '$' : '-$') + Math.abs(pnlVal).toFixed(2);
                    pnlEl.className = pnlVal >= 0 ? 'profit' : 'loss';

                    let totalPnlEl = document.getElementById('total-pnl');
                    let totalVal = data.total_closed_pnl;
                    totalPnlEl.innerText = (totalVal >= 0 ? '$' : '-$') + Math.abs(totalVal).toFixed(2);
                    totalPnlEl.className = totalVal >= 0 ? 'profit' : 'loss';

                    let tradesHtml = '';
                    if (data.trades && data.trades.length > 0) {
                        let sortedTrades = [...data.trades].reverse();
                        sortedTrades.forEach(t => {
                            let pCl = t.pnl >= 0 ? 'profit' : 'loss';
                            tradesHtml += `<tr>
                                <td>${t.date}</td>
                                <td>${t.symbol}</td>
                                <td>${t.direction}</td>
                                <td>${t.entry_price}</td>
                                <td>${t.exit_price}</td>
                                <td>${t.size}</td>
                                <td>${t.reason}</td>
                                <td class="${pCl}">${(t.pnl >= 0 ? '$' : '-$') + Math.abs(t.pnl).toFixed(2)}</td>
                            </tr>`;
                        });
                    } else {
                        tradesHtml = '<tr><td colspan="8" style="text-align: center;">No trades recorded yet.</td></tr>';
                    }
                    document.getElementById('trades-table').innerHTML = tradesHtml;
                }
            } catch (err) {
                console.error("Dashboard fetch error:", err);
            }
        }

        async function toggleBot() {
            let endpoint = isRunning ? '/api/stop' : '/api/start';
            try {
                let res = await fetch(endpoint, { method: 'POST' });
                let data = await res.json();
                fetchDashboard();
            } catch (err) {
                console.error("Toggle error:", err);
            }
        }

        setInterval(fetchDashboard, 2000);
        fetchDashboard();
    </script>
</body>
</html>
"""
            self.wfile.write(html.encode("utf-8"))
            return

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/start":
            self.send_json(BOT.start_bot())
            return

        if path == "/api/stop":
            self.send_json(BOT.stop_bot())
            return

        self.send_json({"success": False, "message": "Unknown endpoint."}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def extract_trade(message):
    try:
        data = json.loads(message)
    except Exception:
        return None, None

    if not isinstance(data, dict):
        return None, None

    candidates = [data]
    if isinstance(data.get("payload"), dict):
        candidates.append(data["payload"])
    if isinstance(data.get("data"), dict):
        candidates.append(data["data"])

    for item in candidates:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol") or item.get("product_symbol") or item.get("sy") or item.get("s")
        price = item.get("price") or item.get("last_price") or item.get("close") or item.get("p")
        if symbol and price is not None:
            try:
                return str(symbol).upper(), float(price)
            except Exception:
                return None, None

    return None, None


def websocket_on_open(ws):
    payload = {
        "type": "subscribe",
        "payload": {
            "channels": [
                {
                    "name": "trades",
                    "symbols": [SYMBOL]
                }
            ]
        }
    }
    ws.send(json.dumps(payload))
    logging.info("XAUTUSD websocket connected.")


def websocket_on_message(ws, message):
    symbol, price = extract_trade(message)
    if symbol != SYMBOL:
        return
    try:
        BOT.evaluate(price)
    except Exception as e:
        logging.error("Evaluate error: %s", e)


def websocket_on_error(ws, error):
    logging.warning("Websocket error: %s", error)


def websocket_on_close(ws, code, message):
    logging.warning("Websocket closed: %s %s", code, message)


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
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            logging.error("Websocket loop error: %s", e)
        time.sleep(RECONNECT_SECONDS)


def main():
    if not acquire_single_process_lock():
        logging.error("Another bot process is already running.")
        return

    get_public_ip()

    try:
        BOT.prepare_product()
        result = BOT.start_bot()
        logging.info("BOT START RESULT --> %s", result)
    except Exception as e:
        logging.error("Startup error: %s", e)
        BOT.execution_uncertain = True
        BOT.save()

    websocket_thread = threading.Thread(
        target=websocket_loop,
        name="xaut-public-websocket",
        daemon=True
    )
    websocket_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), DashboardHandler)
    logging.info("Dashboard running on port %s", PORT)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
