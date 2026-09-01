import os
import time
import json
import hmac
import hashlib
import logging
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
import websocket
from dotenv import load_dotenv

from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse


# ============================================================
# XAUTUSD SIMPLE NEW HIGH / NEW LOW BOT
#
# DASHBOARD IS BUILT INTO THIS SAME PROCESS.
#
# Trading logic:
#
# 05:45 IST = new session
#
# FLAT:
#   price > HIGH -> LONG
#   price < LOW  -> SHORT
#
# LONG:
#   new HIGH -> update HIGH only
#
# SHORT:
#   new LOW -> update LOW only
#
# LONG SL = LOW at entry
# SHORT SL = HIGH at entry
#
# SL hit:
#   LONG  -> SHORT
#   SHORT -> LONG
#
# One position at a time.
#
# Dashboard:
#   http://SERVER:8000
#
# API:
#   /api/health
#   /api/dashboard
# ============================================================


load_dotenv()

IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DASHBOARD_DIR = BASE_DIR / "dashboard"

STATE_FILE = Path(
    os.getenv(
        "STATE_FILE",
        str(BASE_DIR / "xautusd_state.json")
    )
)

TRADE_HISTORY_FILE = BASE_DIR / "trade_history.json"


# ============================================================
# CONFIG
# ============================================================

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")

WS_URL = os.getenv(
    "DELTA_PUBLIC_WS_URL",
    "wss://public-socket.india.delta.exchange"
)

SYMBOL = os.getenv(
    "DELTA_SYMBOL",
    "XAUTUSD"
).strip()

API_KEY = os.getenv(
    "DELTA_API_KEY",
    ""
).strip()

API_SECRET = os.getenv(
    "DELTA_API_SECRET",
    ""
).strip()

LEVERAGE = Decimal(
    os.getenv(
        "LEVERAGE",
        "50"
    )
)

BALANCE_FRACTION = Decimal(
    os.getenv(
        "BALANCE_FRACTION",
        "0.10"
    )
)

RECONNECT_SECONDS = 3

DASHBOARD_HOST = os.getenv(
    "DASHBOARD_HOST",
    "0.0.0.0"
)

DASHBOARD_PORT = int(
    os.getenv(
        "DASHBOARD_PORT",
        "8000"
    )
)


if not API_KEY or not API_SECRET:

    raise SystemExit(
        "Missing DELTA_API_KEY or DELTA_API_SECRET."
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "XAUTUSD-Simple-Bot-Dashboard/1.0"
})


# ============================================================
# TIME
# ============================================================

def now_ist():
    return datetime.now(IST)


def trading_day_start(dt=None):

    dt = dt or now_ist()

    boundary = dt.replace(
        hour=5,
        minute=30,
        second=0,
        microsecond=0
    )

    if dt < boundary:
        boundary -= timedelta(days=1)

    return boundary


def strategy_start(day_start):

    return day_start.replace(
        hour=5,
        minute=45,
        second=0,
        microsecond=0
    )


def weekend(dt=None):

    dt = dt or now_ist()

    # Saturday after 05:00
    if dt.weekday() == 5:
        return dt.hour >= 5

    # Sunday
    if dt.weekday() == 6:
        return True

    return False


def saturday_squareoff(dt=None):

    dt = dt or now_ist()

    return (
        dt.weekday() == 5
        and dt.hour == 5
    )


# ============================================================
# AUTH
# ============================================================

def sign(
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
        "timestamp": timestamp
    }


# ============================================================
# REST
# ============================================================

def api(
    method,
    path,
    params=None,
    body=None,
    auth=False
):

    params = params or {}

    body_text = ""

    if body is not None:

        body_text = json.dumps(
            body,
            separators=(",", ":")
        )

    query = ""

    if params:

        query = "?" + urlencode(
            params,
            doseq=True
        )

    headers = {}

    if auth:

        headers = sign(
            method,
            path,
            query,
            body_text
        )

    response = session.request(
        method.upper(),
        BASE_URL + path,
        params=params,
        data=(
            body_text
            if body is not None
            else None
        ),
        headers=headers,
        timeout=(5, 15)
    )

    response.raise_for_status()

    data = response.json()

    if data.get("success") is False:

        raise RuntimeError(
            f"Delta error: {data}"
        )

    return data


# ============================================================
# PRODUCT
# ============================================================

def product():

    return api(
        "GET",
        f"/v2/products/{SYMBOL}"
    )["result"]


# ============================================================
# POSITION
# ============================================================

def position(product_id):

    data = api(
        "GET",
        "/v2/positions",
        params={
            "product_id": int(product_id)
        },
        auth=True
    )

    result = data.get("result")

    if not isinstance(
        result,
        dict
    ):

        return {
            "size": 0,
            "entry": None
        }

    return {
        "size": int(
            result.get(
                "size",
                0
            )
        ),
        "entry": result.get(
            "entry_price"
        )
    }


# ============================================================
# BALANCE
# ============================================================

def balance():

    data = api(
        "GET",
        "/v2/wallet/balances",
        auth=True
    )

    for wallet in data.get(
        "result",
        []
    ):

        asset = str(
            wallet.get(
                "asset_symbol",
                ""
            )
        ).upper()

        if asset in (
            "USD",
            "USDT"
        ):

            value = (
                wallet.get(
                    "available_balance"
                )
                or wallet.get(
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


# ============================================================
# LEVERAGE
# ============================================================

def set_leverage(product_id):

    try:

        api(
            "POST",
            f"/v2/products/{product_id}/orders/leverage",
            body={
                "leverage": str(
                    LEVERAGE
                )
            },
            auth=True
        )

        logging.info(
            f"LEVERAGE = {LEVERAGE}x"
        )

    except Exception as e:

        logging.warning(
            f"Could not set leverage: {e}"
        )


# ============================================================
# ORDER SIZE
# ============================================================

def order_size(
    product_info,
    price
):

    bal = balance()

    margin = (
        bal
        * BALANCE_FRACTION
    )

    notional = (
        margin
        * LEVERAGE
    )

    contract_value = Decimal(
        str(
            product_info.get(
                "contract_value"
            )
            or product_info.get(
                "contract_value_usd"
            )
            or "1"
        )
    )

    if contract_value <= 0:

        contract_value = Decimal("1")

    raw = (
        notional
        / price
        / contract_value
    )

    increment = Decimal(
        str(
            product_info.get(
                "lot_size"
            )
            or product_info.get(
                "order_size_increment"
            )
            or "1"
        )
    )

    minimum = Decimal(
        str(
            product_info.get(
                "min_order_size"
            )
            or product_info.get(
                "minimum_order_size"
            )
            or increment
        )
    )

    size_decimal = (
        raw / increment
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * increment

    if size_decimal < minimum:

        size_decimal = minimum

    size = int(
        size_decimal
    )

    if size <= 0:

        raise RuntimeError(
            "Order size calculated as zero."
        )

    logging.info(
        f"SIZE | Balance={bal} "
        f"| Margin={margin} "
        f"| Size={size}"
    )

    return size


# ============================================================
# ENTRY
# ============================================================

def market_entry(
    product_id,
    side,
    size,
    sl
):

    body = {

        "product_id":
            int(product_id),

        "product_symbol":
            SYMBOL,

        "size":
            int(abs(size)),

        "side":
            side,

        "order_type":
            "market_order",

        "bracket_stop_loss_price":
            str(sl),

        "bracket_stop_trigger_method":
            "last_traded_price",

        "client_order_id":
            f"simple_{int(time.time()*1000)}"[-32:]
    }

    logging.warning(
        "========================================"
    )

    logging.warning(
        f"ENTRY {side.upper()}"
    )

    logging.warning(
        f"SIZE = {size}"
    )

    logging.warning(
        f"SL   = {sl}"
    )

    logging.warning(
        "========================================"
    )

    return api(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )


# ============================================================
# CLOSE
# ============================================================

def close_position(
    product_id,
    size
):

    if size == 0:
        return

    side = (
        "sell"
        if size > 0
        else "buy"
    )

    body = {

        "product_id":
            int(product_id),

        "product_symbol":
            SYMBOL,

        "size":
            abs(int(size)),

        "side":
            side,

        "order_type":
            "market_order",

        "reduce_only":
            True,

        "client_order_id":
            f"close_{int(time.time()*1000)}"[-32:]
    }

    logging.warning(
        f"CLOSING POSITION | SIZE={size}"
    )

    api(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )


# ============================================================
# HISTORICAL HIGH / LOW
# ============================================================

def historical_high_low(
    start,
    end
):

    try:

        data = api(
            "GET",
            "/v2/history/candles",
            params={
                "resolution": "1m",
                "symbol": SYMBOL,
                "start": int(
                    start.timestamp()
                ),
                "end": int(
                    end.timestamp()
                )
            }
        )

        candles = data.get(
            "result",
            []
        )

        high = None
        low = None

        for candle in candles:

            try:

                h = Decimal(
                    str(
                        candle["high"]
                    )
                )

                l = Decimal(
                    str(
                        candle["low"]
                    )
                )

                if high is None or h > high:
                    high = h

                if low is None or l < low:
                    low = l

            except Exception:

                continue

        return high, low

    except Exception as e:

        logging.warning(
            f"HISTORY ERROR | {e}"
        )

        return None, None


# ============================================================
# BOT
# ============================================================

class Bot:

    def __init__(
        self,
        product_info
    ):

        self.product = product_info

        self.product_id = int(
            product_info["id"]
        )

        self.contract_value = Decimal(
            str(
                product_info.get(
                    "contract_value"
                )
                or product_info.get(
                    "contract_value_usd"
                )
                or "1"
            )
        )

        self.day = None

        self.high = None
        self.low = None

        self.sl = None

        self.last_position = 0

        self.trade_high = None
        self.trade_low = None

        self.last_price = None

        self.ready = False

        self.websocket_connected = False

        self.last_tick_time = None

        self.lock = threading.RLock()

        self.trade_history = []

        self.open_trade = None

        self.dashboard_cache = None
        self.dashboard_cache_time = 0

        self.load_state()
        self.load_trade_history()


    # ========================================================
    # STATE
    # ========================================================

    def load_state(self):

        if not STATE_FILE.exists():

            return

        try:

            with open(
                STATE_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                s = json.load(f)

            if s.get("day"):

                self.day = datetime.fromisoformat(
                    s["day"]
                )

            if s.get("high") is not None:

                self.high = Decimal(
                    str(s["high"])
                )

            if s.get("low") is not None:

                self.low = Decimal(
                    str(s["low"])
                )

            if s.get("sl") is not None:

                self.sl = Decimal(
                    str(s["sl"])
                )

            if s.get("trade_high") is not None:

                self.trade_high = Decimal(
                    str(s["trade_high"])
                )

            if s.get("trade_low") is not None:

                self.trade_low = Decimal(
                    str(s["trade_low"])
                )

            logging.info(
                f"STATE | HIGH={self.high} "
                f"| LOW={self.low} "
                f"| SL={self.sl}"
            )

        except Exception as e:

            logging.warning(
                f"STATE LOAD ERROR | {e}"
            )


    def save(self):

        data = {

            "day":
                (
                    self.day.isoformat()
                    if self.day
                    else None
                ),

            "high":
                (
                    str(self.high)
                    if self.high is not None
                    else None
                ),

            "low":
                (
                    str(self.low)
                    if self.low is not None
                    else None
                ),

            "sl":
                (
                    str(self.sl)
                    if self.sl is not None
                    else None
                ),

            "trade_high":
                (
                    str(self.trade_high)
                    if self.trade_high is not None
                    else None
                ),

            "trade_low":
                (
                    str(self.trade_low)
                    if self.trade_low is not None
                    else None
                )
        }

        tmp = Path(
            str(STATE_FILE)
            + ".tmp"
        )

        with open(
            tmp,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                indent=2
            )

        os.replace(
            tmp,
            STATE_FILE
        )


    # ========================================================
    # TRADE HISTORY
    # ========================================================

    def load_trade_history(self):

        if not TRADE_HISTORY_FILE.exists():

            self.trade_history = []

            return

        try:

            with open(
                TRADE_HISTORY_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                data = json.load(f)

            if isinstance(
                data,
                list
            ):

                self.trade_history = data

            else:

                self.trade_history = []

        except Exception:

            self.trade_history = []


    def save_trade_history(self):

        tmp = Path(
            str(TRADE_HISTORY_FILE)
            + ".tmp"
        )

        with open(
            tmp,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                self.trade_history[-500:],
                f,
                indent=2
            )

        os.replace(
            tmp,
            TRADE_HISTORY_FILE
        )


    def record_trade_close(
        self,
        exit_price
    ):

        if not self.open_trade:

            return

        try:

            direction = self.open_trade[
                "direction"
            ]

            entry_price = Decimal(
                str(
                    self.open_trade[
                        "entry_price"
                    ]
                )
            )

            size = int(
                self.open_trade[
                    "size"
                ]
            )

            exit_price = Decimal(
                str(exit_price)
            )

            if direction == "LONG":

                pnl = (
                    exit_price
                    - entry_price
                ) * Decimal(size) * self.contract_value

            else:

                pnl = (
                    entry_price
                    - exit_price
                ) * Decimal(size) * self.contract_value

            record = {

                "time":
                    now_ist().isoformat(),

                "direction":
                    direction,

                "entry_price":
                    float(entry_price),

                "exit_price":
                    float(exit_price),

                "size":
                    size,

                "pnl":
                    float(pnl)
            }

            self.trade_history.append(
                record
            )

            self.save_trade_history()

            logging.info(
                f"TRADE CLOSED | "
                f"{direction} | "
                f"ENTRY={entry_price} | "
                f"EXIT={exit_price} | "
                f"PNL={pnl}"
            )

        except Exception as e:

            logging.warning(
                f"TRADE HISTORY ERROR | {e}"
            )

        finally:

            self.open_trade = None


    # ========================================================
    # NEW DAY
    # ========================================================

    def new_day(
        self,
        now
    ):

        day = trading_day_start(
            now
        )

        if self.day == day:

            return

        logging.warning(
            "========================================"
        )

        logging.warning(
            f"NEW DAY | {day}"
        )

        logging.warning(
            "========================================"
        )

        self.day = day

        self.high = None
        self.low = None
        self.sl = None

        self.trade_high = None
        self.trade_low = None

        self.ready = False

        self.save()


    # ========================================================
    # PREPARE
    # ========================================================

    def prepare(
        self,
        now,
        price
    ):

        start = strategy_start(
            self.day
        )

        if now < start:

            return False

        if self.ready:

            return True

        pos = position(
            self.product_id
        )

        self.last_position = pos[
            "size"
        ]

        if (
            self.high is not None
            and self.low is not None
        ):

            self.ready = True

            return True

        if now > (
            start
            + timedelta(seconds=5)
        ):

            high, low = historical_high_low(
                start,
                now
            )

            if (
                high is not None
                and low is not None
            ):

                self.high = high
                self.low = low

                logging.warning(
                    "RECOVERED TODAY RANGE"
                )

                logging.warning(
                    f"HIGH = {self.high}"
                )

                logging.warning(
                    f"LOW  = {self.low}"
                )

                self.ready = True

                self.save()

                return True

        self.high = price
        self.low = price

        self.ready = True

        logging.warning(
            f"INITIAL RANGE | "
            f"HIGH={price} LOW={price}"
        )

        self.save()

        return True


    # ========================================================
    # ENTER
    # ========================================================

    def enter(
        self,
        direction,
        price,
        sl
    ):

        if self.last_position != 0:

            return False

        pos = position(
            self.product_id
        )

        if pos["size"] != 0:

            self.last_position = pos[
                "size"
            ]

            return False

        if direction == "LONG":

            if sl >= price:

                logging.error(
                    f"LONG BLOCKED | "
                    f"PRICE={price} SL={sl}"
                )

                return False

            side = "buy"

        else:

            if sl <= price:

                logging.error(
                    f"SHORT BLOCKED | "
                    f"PRICE={price} SL={sl}"
                )

                return False

            side = "sell"

        size = order_size(
            self.product,
            price
        )

        market_entry(
            self.product_id,
            side,
            size,
            sl
        )

        for _ in range(50):

            time.sleep(
                0.2
            )

            pos = position(
                self.product_id
            )

            if direction == "LONG":

                if pos["size"] > 0:

                    self.last_position = pos[
                        "size"
                    ]

                    break

            else:

                if pos["size"] < 0:

                    self.last_position = pos[
                        "size"
                    ]

                    break

        if self.last_position == 0:

            logging.error(
                "ENTRY NOT CONFIRMED"
            )

            return False

        self.sl = Decimal(
            str(sl)
        )

        if direction == "LONG":

            self.trade_high = price
            self.trade_low = None

        else:

            self.trade_low = price
            self.trade_high = None

        self.open_trade = {

            "direction":
                direction,

            "entry_price":
                float(price),

            "size":
                abs(
                    int(
                        self.last_position
                    )
                )
        }

        self.save()

        logging.warning(
            f"TRADE LIVE | "
            f"{direction} | "
            f"ENTRY≈{price} | "
            f"SL={sl}"
        )

        return True


    # ========================================================
    # PRICE
    # ========================================================

    def price_tick(
        self,
        price_text
    ):

        try:

            price = Decimal(
                str(price_text)
            )

        except Exception:

            return

        with self.lock:

            self.last_price = price

            self.last_tick_time = time.time()

            now = now_ist()

            # ------------------------------------------------
            # Saturday square-off
            # ------------------------------------------------

            if saturday_squareoff(now):

                pos = position(
                    self.product_id
                )

                if pos["size"] != 0:

                    self.record_trade_close(
                        price
                    )

                    close_position(
                        self.product_id,
                        pos["size"]
                    )

                return

            # ------------------------------------------------
            # Weekend
            # ------------------------------------------------

            if weekend(now):

                return

            # ------------------------------------------------
            # New day
            # ------------------------------------------------

            self.new_day(now)

            # ------------------------------------------------
            # Before 05:45
            # ------------------------------------------------

            if now < strategy_start(
                self.day
            ):

                return

            # ------------------------------------------------
            # Prepare
            # ------------------------------------------------

            if not self.prepare(
                now,
                price
            ):

                return

            # ------------------------------------------------
            # Exchange position
            # ------------------------------------------------

            pos = position(
                self.product_id
            )

            size = pos["size"]

            # ------------------------------------------------
            # POSITION CLOSED
            # ------------------------------------------------

            if (
                size == 0
                and self.last_position != 0
            ):

                old = self.last_position

                sl_hit = (
                    self.sl is not None
                    and (
                        (
                            old > 0
                            and price <= self.sl
                        )
                        or
                        (
                            old < 0
                            and price >= self.sl
                        )
                    )
                )

                self.record_trade_close(
                    price
                )

                if sl_hit:

                    if old > 0:

                        peak = (
                            self.trade_high
                            or self.high
                        )

                        logging.warning(
                            "LONG SL -> SHORT"
                        )

                        self.last_position = 0
                        self.sl = None

                        self.enter(
                            "SHORT",
                            price,
                            peak
                        )

                    else:

                        trough = (
                            self.trade_low
                            or self.low
                        )

                        logging.warning(
                            "SHORT SL -> LONG"
                        )

                        self.last_position = 0
                        self.sl = None

                        self.enter(
                            "LONG",
                            price,
                            trough
                        )

                    return

                self.last_position = 0
                self.sl = None

                return

            # ------------------------------------------------
            # LONG
            # ------------------------------------------------

            if size > 0:

                self.last_position = size

                if (
                    self.trade_high is None
                    or price > self.trade_high
                ):

                    self.trade_high = price

                    self.high = max(
                        self.high,
                        price
                    )

                    self.save()

                return

            # ------------------------------------------------
            # SHORT
            # ------------------------------------------------

            if size < 0:

                self.last_position = size

                if (
                    self.trade_low is None
                    or price < self.trade_low
                ):

                    self.trade_low = price

                    self.low = min(
                        self.low,
                        price
                    )

                    self.save()

                return

            # ------------------------------------------------
            # FLAT
            # ------------------------------------------------

            self.last_position = 0

            # ------------------------------------------------
            # NEW HIGH
            # ------------------------------------------------

            if price > self.high:

                old_high = self.high

                sl = self.low

                logging.warning(
                    f"NEW HIGH | "
                    f"{old_high} -> {price}"
                )

                if self.enter(
                    "LONG",
                    price,
                    sl
                ):

                    self.high = price

                    self.save()

                return

            # ------------------------------------------------
            # NEW LOW
            # ------------------------------------------------

            if price < self.low:

                old_low = self.low

                sl = self.high

                logging.warning(
                    f"NEW LOW | "
                    f"{old_low} -> {price}"
                )

                if self.enter(
                    "SHORT",
                    price,
                    sl
                ):

                    self.low = price

                    self.save()

                return


    # ========================================================
    # DASHBOARD DATA
    # ========================================================

    def dashboard_data(self):

        now = time.time()

        # Cache REST account calls for 3 seconds.
        # This prevents dashboard polling from hammering Delta.
        if (
            self.dashboard_cache is not None
            and now - self.dashboard_cache_time < 3
        ):

            return self.dashboard_cache

        with self.lock:

            try:

                pos = position(
                    self.product_id
                )

            except Exception as e:

                logging.warning(
                    f"DASHBOARD POSITION ERROR | {e}"
                )

                pos = {
                    "size": 0,
                    "entry": None
                }

            try:

                bal = balance()

                balance_value = float(
                    bal
                )

            except Exception as e:

                logging.warning(
                    f"DASHBOARD BALANCE ERROR | {e}"
                )

                balance_value = 0.0

            size = int(
                pos.get(
                    "size",
                    0
                )
            )

            entry = pos.get(
                "entry"
            )

            entry_value = (
                float(entry)
                if entry is not None
                else 0.0
            )

            current_price = (
                float(
                    self.last_price
                )
                if self.last_price is not None
                else 0.0
            )

            unrealized = 0.0

            if (
                size != 0
                and entry is not None
                and current_price > 0
            ):

                entry_decimal = Decimal(
                    str(entry)
                )

                current_decimal = Decimal(
                    str(current_price)
                )

                if size > 0:

                    unrealized_decimal = (
                        current_decimal
                        - entry_decimal
                    )

                else:

                    unrealized_decimal = (
                        entry_decimal
                        - current_decimal
                    )

                unrealized_decimal *= Decimal(
                    abs(size)
                )

                unrealized_decimal *= (
                    self.contract_value
                )

                unrealized = float(
                    unrealized_decimal
                )

            closed = self.trade_history

            total_trades = len(
                closed
            )

            winning = sum(
                1
                for t in closed
                if float(
                    t.get(
                        "pnl",
                        0
                    )
                ) > 0
            )

            losing = sum(
                1
                for t in closed
                if float(
                    t.get(
                        "pnl",
                        0
                    )
                ) < 0
            )

            total_pnl = sum(
                float(
                    t.get(
                        "pnl",
                        0
                    )
                )
                for t in closed
            )

            win_rate = (
                (
                    winning
                    / total_trades
                    * 100
                )
                if total_trades
                else 0
            )

            if size > 0:

                direction = "LONG"

            elif size < 0:

                direction = "SHORT"

            else:

                direction = "FLAT"

            last_tick = None

            if self.last_tick_time:

                last_tick = (
                    datetime.fromtimestamp(
                        self.last_tick_time,
                        IST
                    ).isoformat()
                )

            data = {

                "success": True,

                "bot_running": True,

                "websocket_connected":
                    self.websocket_connected,

                "symbol":
                    SYMBOL,

                "current_price":
                    current_price,

                "high":
                    (
                        float(self.high)
                        if self.high is not None
                        else 0.0
                    ),

                "low":
                    (
                        float(self.low)
                        if self.low is not None
                        else 0.0
                    ),

                "session_start":
                    (
                        strategy_start(
                            self.day
                        ).isoformat()
                        if self.day
                        else None
                    ),

                "balance":
                    balance_value,

                "total_pnl":
                    total_pnl,

                "today_pnl":
                    total_pnl,

                "position": {

                    "direction":
                        direction,

                    "size":
                        abs(size),

                    "entry_price":
                        entry_value,

                    "stop_loss":
                        (
                            float(self.sl)
                            if self.sl is not None
                            else 0.0
                        ),

                    "unrealized_pnl":
                        unrealized
                },

                "statistics": {

                    "total_trades":
                        total_trades,

                    "winning_trades":
                        winning,

                    "losing_trades":
                        losing,

                    "win_rate":
                        round(
                            win_rate,
                            1
                        )
                },

                "last_tick":
                    last_tick,

                "trades":
                    closed[-50:]
            }

            self.dashboard_cache = data

            self.dashboard_cache_time = now

            return data


# ============================================================
# DASHBOARD HTTP SERVER
# ============================================================

BOT_INSTANCE = None


class DashboardHandler(
    SimpleHTTPRequestHandler
):

    def log_message(
        self,
        format,
        *args
    ):

        return


    def send_json(
        self,
        payload,
        status=200
    ):

        raw = json.dumps(
            payload,
            ensure_ascii=False
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
            str(len(raw))
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.end_headers()

        self.wfile.write(
            raw
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
            "GET, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "*"
        )

        self.end_headers()


    def do_GET(self):

        parsed = urlparse(
            self.path
        )

        path = parsed.path

        if path == "/api/health":

            if BOT_INSTANCE is None:

                self.send_json(
                    {
                        "success": False,
                        "bot_running": False
                    },
                    503
                )

                return

            self.send_json(
                {
                    "success": True,
                    "bot_running": True,
                    "websocket_connected":
                        BOT_INSTANCE.websocket_connected,
                    "symbol": SYMBOL,
                    "port": DASHBOARD_PORT
                }
            )

            return

        if path == "/api/dashboard":

            if BOT_INSTANCE is None:

                self.send_json(
                    {
                        "success": False,
                        "bot_running": False
                    },
                    503
                )

                return

            try:

                data = BOT_INSTANCE.dashboard_data()

                self.send_json(
                    data
                )

            except Exception as e:

                logging.exception(
                    "DASHBOARD API ERROR"
                )

                self.send_json(
                    {
                        "success": False,
                        "error": str(e)
                    },
                    500
                )

            return

        # Serve frontend.
        self.directory = str(
            DASHBOARD_DIR
        )

        super().do_GET()


def start_dashboard_server(
    bot
):

    global BOT_INSTANCE

    BOT_INSTANCE = bot

    DASHBOARD_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    server = ThreadingHTTPServer(
        (
            DASHBOARD_HOST,
            DASHBOARD_PORT
        ),
        DashboardHandler
    )

    logging.warning(
        "========================================"
    )

    logging.warning(
        "DASHBOARD SERVER STARTED"
    )

    logging.warning(
        f"HOST = {DASHBOARD_HOST}"
    )

    logging.warning(
        f"PORT = {DASHBOARD_PORT}"
    )

    logging.warning(
        f"DIRECTORY = {DASHBOARD_DIR}"
    )

    logging.warning(
        "========================================"
    )

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="dashboard-server"
    )

    thread.start()

    return server


# ============================================================
# WEBSOCKET
# ============================================================

def run_websocket(
    bot
):

    while True:

        try:

            logging.warning(
                f"CONNECTING WS | {WS_URL}"
            )

            def on_open(ws):

                bot.websocket_connected = True

                payload = {

                    "type":
                        "subscribe",

                    "payload": {

                        "channels": [

                            {

                                "name":
                                    "trades",

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

                logging.warning(
                    f"TRADES SUBSCRIBED | {SYMBOL}"
                )


            def on_message(
                ws,
                message
            ):

                try:

                    data = json.loads(
                        message
                    )

                    if data.get(
                        "type"
                    ) == "trades":

                        symbol = (
                            data.get(
                                "sy"
                            )
                            or data.get(
                                "symbol"
                            )
                        )

                        price = data.get(
                            "p"
                        )

                        if (
                            symbol == SYMBOL
                            and price is not None
                        ):

                            bot.price_tick(
                                price
                            )

                except Exception as e:

                    logging.error(
                        f"WS MESSAGE ERROR | {e}"
                    )


            def on_error(
                ws,
                error
            ):

                bot.websocket_connected = False

                logging.error(
                    f"WS ERROR | {error}"
                )


            def on_close(
                ws,
                code,
                msg
            ):

                bot.websocket_connected = False

                logging.warning(
                    f"WS CLOSED | {code} | {msg}"
                )


            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )

            ws.run_forever(
                ping_interval=30,
                ping_timeout=10
            )

        except Exception as e:

            bot.websocket_connected = False

            logging.exception(
                f"WS CRASH | {e}"
            )

        logging.warning(
            f"RECONNECTING IN "
            f"{RECONNECT_SECONDS}s"
        )

        time.sleep(
            RECONNECT_SECONDS
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    logging.warning(
        "============================================"
    )

    logging.warning(
        "XAUTUSD SIMPLE NEW HIGH / NEW LOW BOT"
    )

    logging.warning(
        f"SYMBOL = {SYMBOL}"
    )

    logging.warning(
        f"LEVERAGE = {LEVERAGE}x"
    )

    logging.warning(
        f"BALANCE = {BALANCE_FRACTION * 100}%"
    )

    logging.warning(
        "START = 05:45 IST"
    )

    logging.warning(
        "FEED = TRADES WEBSOCKET"
    )

    logging.warning(
        "DASHBOARD = SAME BOT PROCESS"
    )

    logging.warning(
        "============================================"
    )

    try:

        product_info = product()

        set_leverage(
            int(
                product_info["id"]
            )
        )

        bot = Bot(
            product_info
        )

        # Dashboard starts inside SAME bot process.
        start_dashboard_server(
            bot
        )

        # Trading websocket remains the main engine.
        run_websocket(
            bot
        )

    except KeyboardInterrupt:

        logging.warning(
            "BOT STOPPED"
        )

    except Exception as e:

        logging.exception(
            f"FATAL ERROR | {e}"
        )
