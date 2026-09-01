import os
import time
import json
import hmac
import hashlib
import logging
import threading
import queue

from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
import websocket
from dotenv import load_dotenv


# ============================================================
# XAUTUSD HIGH / LOW BREAKOUT + REVERSAL BOT
# VERSION 30.0
#
# ============================================================
#
# STRATEGY
# ------------------------------------------------------------
#
# 05:45 IST = START OF NEW TRADING SESSION
#
# IMPORTANT:
# 05:45 IS NOT A "TRADE AT THIS PRICE" SIGNAL.
#
# 05:45 only starts today's strategy.
#
# From 05:45 onward:
#
#   FLAT:
#       New HIGH  -> LONG
#       New LOW   -> SHORT
#
#   LONG:
#       Initial SL = LOW existing when LONG entered.
#       Keep tracking highest peak made during LONG.
#
#       If LONG SL is hit:
#           LONG closes
#           immediately SHORT
#           SHORT SL = highest peak made during LONG
#
#   SHORT:
#       Initial SL = HIGH existing when SHORT entered.
#       Keep tracking lowest trough made during SHORT.
#
#       If SHORT SL is hit:
#           SHORT closes
#           immediately LONG
#           LONG SL = lowest trough made during SHORT
#
# ONE POSITION ONLY.
#
#
# DAILY RESET
# ------------------------------------------------------------
#
# Every day at 05:45:
#
#   1. Close any previous position.
#   2. Forget previous day's HIGH/LOW.
#   3. Start a completely new trading session.
#
#
# LATE START / RESTART
# ------------------------------------------------------------
#
# If bot starts after 05:45:
#
#   Example:
#       Bot starts at 16:00.
#
#   It loads historical 1-minute candles from 05:45
#   until the last completed minute.
#
#   It reconstructs today's HIGH and LOW.
#
#   Then the live trade feed continues.
#
#   Therefore:
#
#       Current price > recovered HIGH -> LONG
#       Current price < recovered LOW  -> SHORT
#
#   Otherwise wait for the next new HIGH/LOW.
#
#
# IMPORTANT
# ------------------------------------------------------------
#
# Normal restart during the same trading day:
#   Saved state is restored.
#
# If there is an active position:
#   Its SL / peak / trough are restored.
#
# The bot does NOT create a new baseline at restart time.
#
#
# DATA ARCHITECTURE
# ------------------------------------------------------------
#
# Public WebSocket:
#       trades
#
# Private WebSocket:
#       positions
#       orders
#       v2/user_trades
#
# REST:
#       startup
#       historical recovery
#       order placement
#       balance
#       leverage
#       low-frequency watchdog
#
# Market ticks NEVER call REST /positions.
#
# ============================================================


load_dotenv()


# ============================================================
# TIMEZONE
# ============================================================

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")


PUBLIC_WS_URL = os.getenv(
    "DELTA_PUBLIC_WS_URL",
    "wss://public-socket.india.delta.exchange"
)


PRIVATE_WS_URL = os.getenv(
    "DELTA_PRIVATE_WS_URL",
    "wss://socket.india.delta.exchange"
)


SYMBOL = os.getenv(
    "DELTA_SYMBOL",
    "XAUTUSD"
)


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


STATE_FILE = os.getenv(
    "STATE_FILE",
    "xautusd_state.json"
)


WATCHDOG_SECONDS = float(
    os.getenv(
        "WATCHDOG_SECONDS",
        "2.0"
    )
)


if not API_KEY or not API_SECRET:
    raise SystemExit(
        "ERROR: DELTA_API_KEY / DELTA_API_SECRET missing."
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
    "User-Agent": "XAUTUSD-Extreme-Reversal-Engine/30.0"
})


# ============================================================
# GLOBAL EVENT QUEUES
# ============================================================

market_queue = queue.Queue(
    maxsize=50000
)


private_queue = queue.Queue(
    maxsize=10000
)


# ============================================================
# TIME HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def trading_day_start(dt=None):
    """
    Trading date boundary = 05:30 IST.

    Strategy itself starts at 05:45.
    """

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


def strategy_start_time(day_start):
    return day_start + timedelta(
        minutes=15
    )


def is_strategy_time(dt=None):
    dt = dt or now_ist()

    return (
        dt >= strategy_start_time(
            trading_day_start(dt)
        )
        and not is_weekend_blocked(dt)
    )


def is_weekend_blocked(dt=None):
    dt = dt or now_ist()

    weekday = dt.weekday()

    # Saturday after 05:00
    if weekday == 5:
        return dt.hour >= 5

    # Sunday
    if weekday == 6:
        return True

    # Monday before 05:30
    if weekday == 0:
        return dt < dt.replace(
            hour=5,
            minute=30,
            second=0,
            microsecond=0
        )

    return False


def is_saturday_squareoff(dt=None):
    dt = dt or now_ist()

    return (
        dt.weekday() == 5
        and dt.hour == 5
        and dt.minute < 30
    )


def floor_to_minute(dt):
    return dt.replace(
        second=0,
        microsecond=0
    )


# ============================================================
# DELTA REST AUTH
# ============================================================

def sign_request(
    method,
    path,
    query_string="",
    body=""
):

    timestamp = str(
        int(time.time())
    )

    message = (
        method.upper()
        + timestamp
        + path
        + query_string
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
# REST API
# ============================================================

def api_call(
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
            separators=(",", ":"),
            ensure_ascii=False
        )
        if body is not None
        else ""
    )

    query_string = (
        "?" + urlencode(
            params,
            doseq=True
        )
        if params
        else ""
    )

    headers = (
        sign_request(
            method,
            path,
            query_string,
            body_text
        )
        if auth
        else {}
    )

    try:

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
                f"Delta API error: {data}"
            )

        return data

    except Exception as exc:

        raise RuntimeError(
            f"HTTP failed "
            f"{method} {path}: {exc}"
        ) from exc


# ============================================================
# PRODUCT
# ============================================================

def get_product():

    data = api_call(
        "GET",
        f"/v2/products/{SYMBOL}"
    )

    return data["result"]


# ============================================================
# POSITION REST
#
# LOW FREQUENCY ONLY.
#
# NEVER CALL THIS FROM EVERY MARKET TICK.
# ============================================================

def get_position(
    product_id
):

    data = api_call(
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
        "result"
    )

    if not isinstance(
        result,
        dict
    ):

        return {
            "size": 0,
            "entry_price": None
        }

    return {
        "size": int(
            result.get(
                "size",
                0
            )
        ),
        "entry_price":
            result.get(
                "entry_price"
            )
    }


# ============================================================
# BALANCE
# ============================================================

def get_balance():

    data = api_call(
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
                    "balance"
                )
                or wallet.get(
                    "available_balance"
                )
            )

            if value is not None:
                return Decimal(
                    str(value)
                )

    net_equity = (
        data.get(
            "meta",
            {}
        ).get(
            "net_equity"
        )
    )

    if net_equity is not None:
        return Decimal(
            str(net_equity)
        )

    raise RuntimeError(
        "Could not retrieve wallet balance."
    )


# ============================================================
# LEVERAGE
# ============================================================

def set_leverage(
    product_id
):

    try:

        api_call(
            "POST",
            f"/v2/products/"
            f"{product_id}/orders/leverage",
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

    except Exception as exc:

        logging.warning(
            f"Could not set leverage: {exc}"
        )


# ============================================================
# ORDER SIZE
# ============================================================

def calculate_order_size(
    product,
    price
):

    balance = get_balance()

    margin = (
        balance
        * BALANCE_FRACTION
    )

    notional = (
        margin
        * LEVERAGE
    )

    contract_value = Decimal(
        str(
            product.get(
                "contract_value"
            )
            or product.get(
                "contract_value_usd"
            )
            or "1"
        )
    )

    if contract_value <= 0:
        contract_value = Decimal("1")

    raw_size = (
        notional
        / (
            price
            * contract_value
        )
    )

    lot_size = Decimal(
        str(
            product.get(
                "lot_size"
            )
            or product.get(
                "order_size_increment"
            )
            or "1"
        )
    )

    if lot_size <= 0:
        lot_size = Decimal("1")

    minimum_size = Decimal(
        str(
            product.get(
                "min_order_size"
            )
            or product.get(
                "minimum_order_size"
            )
            or lot_size
        )
    )

    size_decimal = (
        raw_size
        / lot_size
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * lot_size

    if size_decimal < minimum_size:
        size_decimal = minimum_size

    size = int(
        size_decimal
    )

    if size <= 0:
        raise RuntimeError(
            "Calculated order size is zero."
        )

    logging.info(
        "SIZE CALC | "
        f"BALANCE={balance} | "
        f"MARGIN={margin} | "
        f"NOTIONAL={notional} | "
        f"SIZE={size}"
    )

    return size


# ============================================================
# MARKET ENTRY
# ============================================================

def place_market_entry(
    product_id,
    side,
    size,
    sl_price
):

    client_order_id = (
        f"xv30e{int(time.time() * 1000)}"
    )[-32:]

    body = {
        "product_id": int(
            product_id
        ),
        "product_symbol": SYMBOL,
        "size": int(
            abs(size)
        ),
        "side": side,
        "order_type": "market_order",

        # Exchange-side protective stop.
        "bracket_stop_loss_price":
            str(sl_price),

        "bracket_stop_trigger_method":
            "last_traded_price",

        "client_order_id":
            client_order_id
    }

    logging.warning(
        "========================================"
    )

    logging.warning(
        "PLACING ENTRY"
    )

    logging.warning(
        f"SIDE={side.upper()}"
    )

    logging.warning(
        f"SIZE={abs(size)}"
    )

    logging.warning(
        f"SL={sl_price}"
    )

    logging.warning(
        f"CLIENT_OID={client_order_id}"
    )

    logging.warning(
        "========================================"
    )

    result = api_call(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )

    return (
        result,
        client_order_id
    )


# ============================================================
# CLOSE POSITION
# ============================================================

def close_position_market(
    product_id,
    size
):

    if size == 0:
        return None

    side = (
        "sell"
        if size > 0
        else "buy"
    )

    body = {
        "product_id": int(
            product_id
        ),
        "product_symbol": SYMBOL,
        "size": int(
            abs(size)
        ),
        "side": side,
        "order_type": "market_order",
        "reduce_only": True,
        "client_order_id":
            f"xv30close{int(time.time()*1000)}"[-32:]
    }

    logging.warning(
        "CLOSING POSITION | "
        f"SIDE={side.upper()} | "
        f"SIZE={abs(size)}"
    )

    return api_call(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )


# ============================================================
# HISTORICAL 1-MINUTE DATA
# ============================================================

def get_historical_day_range(
    day_start,
    now
):

    strategy_start = (
        strategy_start_time(
            day_start
        )
    )

    current_minute = floor_to_minute(
        now
    )

    # We deliberately exclude the currently
    # forming minute.
    end_dt = (
        current_minute
        - timedelta(seconds=1)
    )

    if end_dt <= strategy_start:
        return None, None

    start_ts = int(
        strategy_start.timestamp()
    )

    end_ts = int(
        end_dt.timestamp()
    )

    logging.warning(
        "HISTORICAL RECOVERY | "
        f"FROM={strategy_start} | "
        f"TO={end_dt}"
    )

    data = api_call(
        "GET",
        "/v2/history/candles",
        params={
            "resolution": "1m",
            "symbol": SYMBOL,
            "start": start_ts,
            "end": end_ts
        },
        auth=False
    )

    candles = data.get(
        "result",
        []
    )

    if not candles:
        return None, None

    highs = []
    lows = []

    for candle in candles:

        try:

            high = Decimal(
                str(
                    candle["high"]
                )
            )

            low = Decimal(
                str(
                    candle["low"]
                )
            )

            highs.append(high)
            lows.append(low)

        except Exception:
            continue

    if not highs or not lows:
        return None, None

    return (
        max(highs),
        min(lows)
    )


# ============================================================
# TRADING STRATEGY
# ============================================================

class TradingEngine:

    def __init__(
        self,
        product
    ):

        self.product = product

        self.product_id = int(
            product["id"]
        )

        self.lock = threading.RLock()

        # ----------------------------------------------------
        # SESSION
        # ----------------------------------------------------

        self.day_start = None

        self.session_initialized = False

        # ----------------------------------------------------
        # GLOBAL DAY EXTREMES
        # ----------------------------------------------------

        self.running_high = None

        self.running_low = None

        # ----------------------------------------------------
        # CURRENT POSITION
        #
        # positive = LONG
        # negative = SHORT
        # zero     = FLAT
        # ----------------------------------------------------

        self.position_size = 0

        self.entry_price = None

        # ----------------------------------------------------
        # CURRENT TRADE STOP
        # ----------------------------------------------------

        self.current_sl = None

        # ----------------------------------------------------
        # CURRENT TRADE EXTREME
        #
        # LONG:
        #   trade_peak
        #
        # SHORT:
        #   trade_trough
        # ----------------------------------------------------

        self.trade_peak = None

        self.trade_trough = None

        # ----------------------------------------------------
        # STOP / REVERSAL
        # ----------------------------------------------------

        self.stop_triggered = False

        self.pending_reversal = None

        self.pending_reversal_sl = None

        # ----------------------------------------------------
        # ORDER CONTROL
        # ----------------------------------------------------

        self.order_in_flight = False

        self.last_order_id = None

        self.last_client_order_id = None

        # ----------------------------------------------------
        # LAST PRICE
        # ----------------------------------------------------

        self.last_price = None

        # ----------------------------------------------------
        # STATE
        # ----------------------------------------------------

        self.load_state()


    # ========================================================
    # STATE LOAD
    # ========================================================

    def load_state(
        self
    ):

        if not os.path.exists(
            STATE_FILE
        ):

            logging.info(
                "No state file found."
            )

            return

        try:

            with open(
                STATE_FILE,
                "r",
                encoding="utf-8"
            ) as f:

                state = json.load(f)

            day_text = state.get(
                "day_start"
            )

            if day_text:

                self.day_start = (
                    datetime.fromisoformat(
                        day_text
                    )
                )

            self.session_initialized = bool(
                state.get(
                    "session_initialized",
                    False
                )
            )

            self.running_high = (
                Decimal(
                    str(
                        state["running_high"]
                    )
                )
                if state.get(
                    "running_high"
                ) is not None
                else None
            )

            self.running_low = (
                Decimal(
                    str(
                        state["running_low"]
                    )
                )
                if state.get(
                    "running_low"
                ) is not None
                else None
            )

            self.position_size = int(
                state.get(
                    "position_size",
                    0
                )
            )

            self.entry_price = (
                Decimal(
                    str(
                        state["entry_price"]
                    )
                )
                if state.get(
                    "entry_price"
                ) is not None
                else None
            )

            self.current_sl = (
                Decimal(
                    str(
                        state["current_sl"]
                    )
                )
                if state.get(
                    "current_sl"
                ) is not None
                else None
            )

            self.trade_peak = (
                Decimal(
                    str(
                        state["trade_peak"]
                    )
                )
                if state.get(
                    "trade_peak"
                ) is not None
                else None
            )

            self.trade_trough = (
                Decimal(
                    str(
                        state["trade_trough"]
                    )
                )
                if state.get(
                    "trade_trough"
                ) is not None
                else None
            )

            logging.info(
                "STATE LOADED | "
                f"DAY={self.day_start} | "
                f"HIGH={self.running_high} | "
                f"LOW={self.running_low} | "
                f"POS={self.position_size} | "
                f"SL={self.current_sl} | "
                f"PEAK={self.trade_peak} | "
                f"TROUGH={self.trade_trough}"
            )

        except Exception as exc:

            logging.exception(
                f"STATE LOAD ERROR: {exc}"
            )


    # ========================================================
    # STATE SAVE
    # ========================================================

    def save_state(
        self
    ):

        with self.lock:

            state = {

                "day_start":
                    self.day_start.isoformat()
                    if self.day_start
                    else None,

                "session_initialized":
                    self.session_initialized,

                "running_high":
                    str(
                        self.running_high
                    )
                    if self.running_high is not None
                    else None,

                "running_low":
                    str(
                        self.running_low
                    )
                    if self.running_low is not None
                    else None,

                "position_size":
                    self.position_size,

                "entry_price":
                    str(
                        self.entry_price
                    )
                    if self.entry_price is not None
                    else None,

                "current_sl":
                    str(
                        self.current_sl
                    )
                    if self.current_sl is not None
                    else None,

                "trade_peak":
                    str(
                        self.trade_peak
                    )
                    if self.trade_peak is not None
                    else None,

                "trade_trough":
                    str(
                        self.trade_trough
                    )
                    if self.trade_trough is not None
                    else None
            }

            temp_file = (
                STATE_FILE + ".tmp"
            )

            with open(
                temp_file,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    state,
                    f,
                    indent=2
                )

            os.replace(
                temp_file,
                STATE_FILE
            )


    # ========================================================
    # NEW SESSION
    # ========================================================

    def start_new_session(
        self,
        now
    ):

        new_day = trading_day_start(
            now
        )

        with self.lock:

            if (
                self.session_initialized
                and self.day_start == new_day
            ):
                return

            logging.warning(
                "================================================"
            )

            logging.warning(
                "NEW 05:45 TRADING SESSION"
            )

            logging.warning(
                f"TRADING DAY = {new_day}"
            )

            logging.warning(
                "Previous day's HIGH/LOW will NOT be reused."
            )

            logging.warning(
                "================================================"
            )

            self.day_start = new_day

            self.session_initialized = False

            self.running_high = None
            self.running_low = None

            self.current_sl = None

            self.trade_peak = None
            self.trade_trough = None

            self.stop_triggered = False

            self.pending_reversal = None
            self.pending_reversal_sl = None

            self.save_state()

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Close any position from the previous session.
        # ----------------------------------------------------

        try:

            position = get_position(
                self.product_id
            )

            size = position["size"]

            if size != 0:

                logging.warning(
                    "05:45 SESSION RESET | "
                    f"CLOSING OLD POSITION={size}"
                )

                close_position_market(
                    self.product_id,
                    size
                )

                deadline = (
                    time.time() + 10
                )

                while time.time() < deadline:

                    time.sleep(
                        0.25
                    )

                    check = get_position(
                        self.product_id
                    )

                    if check["size"] == 0:
                        break

                final = get_position(
                    self.product_id
                )

                if final["size"] != 0:

                    logging.error(
                        "FAILED TO CLOSE OLD POSITION "
                        "AT NEW SESSION."
                    )

                    return False

        except Exception as exc:

            logging.exception(
                f"05:45 position reset error: {exc}"
            )

            return False

        # ----------------------------------------------------
        # Late-start recovery.
        # ----------------------------------------------------

        now = now_ist()

        high = None
        low = None

        if now >= strategy_start_time(
            new_day
        ):

            high, low = (
                get_historical_day_range(
                    new_day,
                    now
                )
            )

        with self.lock:

            if high is not None:

                self.running_high = high
                self.running_low = low

                logging.warning(
                    "LATE START RECOVERY COMPLETE | "
                    f"HIGH={high} | "
                    f"LOW={low}"
                )

            else:

                logging.warning(
                    "NO COMPLETED 1-MINUTE DATA TO RECOVER."
                )

                logging.warning(
                    "Waiting for live price to create "
                    "today's first HIGH/LOW."
                )

            self.position_size = 0
            self.entry_price = None

            self.session_initialized = True

            self.save_state()

        logging.warning(
            "NEW SESSION READY."
        )

        return True


    # ========================================================
    # INITIALIZE SESSION IF NEEDED
    # ========================================================

    def ensure_session(
        self,
        now
    ):

        current_day = trading_day_start(
            now
        )

        with self.lock:

            same_day = (
                self.day_start
                == current_day
            )

            already_initialized = (
                self.session_initialized
            )

        if same_day and already_initialized:
            return True

        # ----------------------------------------------------
        # If this is a restart after 05:45 and a position
        # exists, first determine whether it belongs to the
        # saved current session.
        # ----------------------------------------------------

        return self.start_new_session(
            now
        )


    # ========================================================
    # ESTABLISH FIRST LIVE EXTREME
    # ========================================================

    def initialize_first_price(
        self,
        price
    ):

        with self.lock:

            if (
                self.running_high is None
                or self.running_low is None
            ):

                self.running_high = price
                self.running_low = price

                logging.warning(
                    "FIRST LIVE SESSION PRICE | "
                    f"HIGH={price} | "
                    f"LOW={price}"
                )

                self.save_state()


    # ========================================================
    # ENTRY
    # ========================================================

    def enter_position(
        self,
        direction,
        price,
        sl_price,
        reason
    ):

        with self.lock:

            if self.order_in_flight:

                logging.warning(
                    "ENTRY BLOCKED | "
                    "Order already in flight."
                )

                return False

            if self.position_size != 0:

                logging.warning(
                    "ENTRY BLOCKED | "
                    f"Position already exists: "
                    f"{self.position_size}"
                )

                return False

            if sl_price is None:

                logging.error(
                    "ENTRY BLOCKED | SL is None."
                )

                return False

            # ------------------------------------------------
            # Validate SL direction.
            # ------------------------------------------------

            if direction == "LONG":

                if sl_price >= price:

                    logging.error(
                        "LONG ENTRY INVALID | "
                        f"PRICE={price} | "
                        f"SL={sl_price}"
                    )

                    return False

            elif direction == "SHORT":

                if sl_price <= price:

                    logging.error(
                        "SHORT ENTRY INVALID | "
                        f"PRICE={price} | "
                        f"SL={sl_price}"
                    )

                    return False

            else:

                return False

            self.order_in_flight = True

        try:

            # ------------------------------------------------
            # REST position check ONLY ON ENTRY.
            # ------------------------------------------------

            exchange_position = get_position(
                self.product_id
            )

            if exchange_position["size"] != 0:

                logging.warning(
                    "ENTRY BLOCKED | "
                    "Exchange already has position "
                    f"{exchange_position['size']}"
                )

                with self.lock:

                    self.position_size = (
                        exchange_position["size"]
                    )

                    self.entry_price = (
                        Decimal(
                            str(
                                exchange_position[
                                    "entry_price"
                                ]
                            )
                        )
                        if exchange_position[
                            "entry_price"
                        ] is not None
                        else None
                    )

                    self.save_state()

                return False

            size = calculate_order_size(
                self.product,
                price
            )

            side = (
                "buy"
                if direction == "LONG"
                else "sell"
            )

            result, client_oid = (
                place_market_entry(
                    self.product_id,
                    side,
                    size,
                    sl_price
                )
            )

            order = result.get(
                "result",
                {}
            )

            order_id = order.get(
                "id"
            )

            with self.lock:

                self.last_order_id = (
                    order_id
                )

                self.last_client_order_id = (
                    client_oid
                )

                self.current_sl = (
                    Decimal(
                        str(sl_price)
                    )
                )

                # ------------------------------------------------
                # We immediately record intended position.
                #
                # Private positions channel will confirm it.
                # ------------------------------------------------

                if direction == "LONG":

                    self.position_size = (
                        size
                    )

                    self.entry_price = (
                        price
                    )

                    self.trade_peak = (
                        price
                    )

                    self.trade_trough = None

                else:

                    self.position_size = (
                        -size
                    )

                    self.entry_price = (
                        price
                    )

                    self.trade_trough = (
                        price
                    )

                    self.trade_peak = None

                self.stop_triggered = False

                self.pending_reversal = None
                self.pending_reversal_sl = None

                self.save_state()

            logging.warning(
                "****************************************"
            )

            logging.warning(
                f"ENTRY CONFIRMED/ACCEPTED | "
                f"{direction}"
            )

            logging.warning(
                f"PRICE={price}"
            )

            logging.warning(
                f"SL={sl_price}"
            )

            logging.warning(
                f"SIZE={size}"
            )

            logging.warning(
                f"REASON={reason}"
            )

            logging.warning(
                f"ORDER_ID={order_id}"
            )

            logging.warning(
                "****************************************"
            )

            return True

        except Exception as exc:

            logging.exception(
                f"ENTRY FAILED: {exc}"
            )

            return False

        finally:

            with self.lock:
                self.order_in_flight = False


    # ========================================================
    # LONG -> SHORT REVERSAL
    # ========================================================

    def prepare_long_stop(
        self,
        price
    ):

        with self.lock:

            peak = (
                self.trade_peak
                or self.running_high
            )

            if peak is None:
                logging.error(
                    "Cannot reverse LONG -> SHORT: "
                    "no peak available."
                )
                return

            self.stop_triggered = True

            self.pending_reversal = (
                "SHORT"
            )

            self.pending_reversal_sl = (
                peak
            )

            logging.warning(
                "LONG STOP EVENT | "
                f"SHORT SL={peak}"
            )

            self.save_state()


    # ========================================================
    # SHORT -> LONG REVERSAL
    # ========================================================

    def prepare_short_stop(
        self,
        price
    ):

        with self.lock:

            trough = (
                self.trade_trough
                or self.running_low
            )

            if trough is None:
                logging.error(
                    "Cannot reverse SHORT -> LONG: "
                    "no trough available."
                )
                return

            self.stop_triggered = True

            self.pending_reversal = (
                "LONG"
            )

            self.pending_reversal_sl = (
                trough
            )

            logging.warning(
                "SHORT STOP EVENT | "
                f"LONG SL={trough}"
            )

            self.save_state()


    # ========================================================
    # POSITION UPDATE
    # ========================================================

    def update_position(
        self,
        size,
        entry_price=None
    ):

        with self.lock:

            old_size = (
                self.position_size
            )

            self.position_size = int(
                size
            )

            if entry_price is not None:

                try:

                    self.entry_price = (
                        Decimal(
                            str(
                                entry_price
                            )
                        )
                    )

                except Exception:
                    pass

            # ------------------------------------------------
            # Exchange says position is flat.
            # ------------------------------------------------

            if size == 0:

                if old_size != 0:

                    logging.warning(
                        "EXCHANGE POSITION FLAT | "
                        f"OLD={old_size}"
                    )

                # ------------------------------------------------
                # If stop-triggered reversal is waiting,
                # execute it after position becomes flat.
                # ------------------------------------------------

                if (
                    self.stop_triggered
                    and self.pending_reversal
                    and self.pending_reversal_sl
                ):

                    direction = (
                        self.pending_reversal
                    )

                    reversal_sl = (
                        self.pending_reversal_sl
                    )

                    price = (
                        self.last_price
                        or self.entry_price
                    )

                    self.position_size = 0
                    self.entry_price = None
                    self.current_sl = None

                    self.stop_triggered = False

                    self.pending_reversal = None
                    self.pending_reversal_sl = None

                    self.trade_peak = None
                    self.trade_trough = None

                    self.save_state()

                    # ------------------------------------------------
                    # Reverse immediately.
                    # ------------------------------------------------

                    if price is not None:

                        logging.warning(
                            "EXECUTING AUTOMATIC REVERSAL | "
                            f"{direction} | "
                            f"PRICE={price} | "
                            f"SL={reversal_sl}"
                        )

                        # Do not call while holding lock.
                        threading.Thread(
                            target=self._execute_reversal,
                            args=(
                                direction,
                                Decimal(
                                    str(price)
                                ),
                                Decimal(
                                    str(reversal_sl)
                                )
                            ),
                            daemon=True
                        ).start()

                    return

                # ------------------------------------------------
                # External/manual closure.
                # ------------------------------------------------

                if old_size != 0:

                    logging.warning(
                        "POSITION CLOSED WITHOUT "
                        "STOP-TRIGGER EVENT."
                    )

                    logging.warning(
                        "No automatic reversal."
                    )

                    self.current_sl = None
                    self.entry_price = None
                    self.trade_peak = None
                    self.trade_trough = None

                    self.save_state()

                return

            # ------------------------------------------------
            # Position exists.
            # ------------------------------------------------

            if size > 0:

                if self.trade_peak is None:

                    self.trade_peak = (
                        Decimal(
                            str(
                                entry_price
                                or self.last_price
                            )
                        )
                    )

            elif size < 0:

                if self.trade_trough is None:

                    self.trade_trough = (
                        Decimal(
                            str(
                                entry_price
                                or self.last_price
                            )
                        )
                    )

            self.save_state()


    # ========================================================
    # REVERSAL WORKER
    # ========================================================

    def _execute_reversal(
        self,
        direction,
        price,
        sl_price
    ):

        # Small delay gives the exchange position update
        # time to settle.
        time.sleep(
            0.05
        )

        self.enter_position(
            direction,
            price,
            sl_price,
            (
                "LONG SL HIT -> SHORT"
                if direction == "SHORT"
                else
                "SHORT SL HIT -> LONG"
            )
        )


    # ========================================================
    # PROCESS MARKET PRICE
    # ========================================================

    def process_price(
        self,
        price
    ):

        try:

            price = Decimal(
                str(price)
            )

        except (
            InvalidOperation,
            ValueError,
            TypeError
        ):

            return

        now = now_ist()

        with self.lock:

            self.last_price = price

        # ----------------------------------------------------
        # Weekend
        # ----------------------------------------------------

        if is_weekend_blocked(now):

            return

        # ----------------------------------------------------
        # Saturday 05:00 square-off
        # ----------------------------------------------------

        if is_saturday_squareoff(now):

            try:

                position = get_position(
                    self.product_id
                )

                if position["size"] != 0:

                    close_position_market(
                        self.product_id,
                        position["size"]
                    )

            except Exception as exc:

                logging.exception(
                    f"Saturday square-off failed: {exc}"
                )

            return

        # ----------------------------------------------------
        # Make sure today's session exists.
        # ----------------------------------------------------

        if not self.ensure_session(
            now
        ):

            return

        # ----------------------------------------------------
        # Before 05:45
        # ----------------------------------------------------

        if now < strategy_start_time(
            self.day_start
        ):

            return

        # ----------------------------------------------------
        # First live price if historical recovery did not
        # produce a range.
        # ----------------------------------------------------

        self.initialize_first_price(
            price
        )

        # ----------------------------------------------------
        # Update global HIGH/LOW.
        #
        # But FIRST compare against old levels when FLAT.
        # ----------------------------------------------------

        with self.lock:

            position_size = (
                self.position_size
            )

            old_high = (
                self.running_high
            )

            old_low = (
                self.running_low
            )

            current_sl = (
                self.current_sl
            )

        # ====================================================
        # POSITION MANAGEMENT
        # ====================================================

        if position_size > 0:

            # ------------------------------------------------
            # LONG
            # ------------------------------------------------

            with self.lock:

                if (
                    self.trade_peak is None
                    or price > self.trade_peak
                ):

                    self.trade_peak = price

                    logging.info(
                        "LONG PEAK | "
                        f"{price}"
                    )

                if (
                    self.running_high is None
                    or price > self.running_high
                ):

                    self.running_high = price

                if (
                    self.running_low is None
                    or price < self.running_low
                ):

                    self.running_low = price

                self.save_state()

            # ------------------------------------------------
            # Local stop detection backup.
            #
            # Actual protection remains exchange-side SL.
            # We only prepare reversal here.
            # ------------------------------------------------

            if (
                current_sl is not None
                and price <= current_sl
                and not self.stop_triggered
            ):

                logging.warning(
                    "LOCAL LONG SL DETECTED | "
                    f"PRICE={price} | "
                    f"SL={current_sl}"
                )

                self.prepare_long_stop(
                    price
                )

            return

        if position_size < 0:

            # ------------------------------------------------
            # SHORT
            # ------------------------------------------------

            with self.lock:

                if (
                    self.trade_trough is None
                    or price < self.trade_trough
                ):

                    self.trade_trough = price

                    logging.info(
                        "SHORT TROUGH | "
                        f"{price}"
                    )

                if (
                    self.running_high is None
                    or price > self.running_high
                ):

                    self.running_high = price

                if (
                    self.running_low is None
                    or price < self.running_low
                ):

                    self.running_low = price

                self.save_state()

            # ------------------------------------------------
            # Local stop detection backup.
            # ------------------------------------------------

            if (
                current_sl is not None
                and price >= current_sl
                and not self.stop_triggered
            ):

                logging.warning(
                    "LOCAL SHORT SL DETECTED | "
                    f"PRICE={price} | "
                    f"SL={current_sl}"
                )

                self.prepare_short_stop(
                    price
                )

            return

        # ====================================================
        # FLAT
        # ====================================================

        with self.lock:

            self.position_size = 0
            self.current_sl = None

        # ----------------------------------------------------
        # NEW HIGH -> LONG
        #
        # Compare against OLD high first.
        # ----------------------------------------------------

        if (
            old_high is not None
            and price > old_high
        ):

            sl = old_low

            if (
                sl is not None
                and sl < price
            ):

                logging.warning(
                    "########################################"
                )

                logging.warning(
                    "NEW HIGH BREAKOUT"
                )

                logging.warning(
                    f"OLD HIGH = {old_high}"
                )

                logging.warning(
                    f"PRICE     = {price}"
                )

                logging.warning(
                    f"LONG SL   = {sl}"
                )

                logging.warning(
                    "########################################"
                )

                entered = self.enter_position(
                    "LONG",
                    price,
                    sl,
                    "NEW HIGH BREAKOUT"
                )

                if entered:

                    with self.lock:

                        self.running_high = price
                        self.save_state()

                    return

        # ----------------------------------------------------
        # NEW LOW -> SHORT
        #
        # Compare against OLD low first.
        # ----------------------------------------------------

        if (
            old_low is not None
            and price < old_low
        ):

            sl = old_high

            if (
                sl is not None
                and sl > price
            ):

                logging.warning(
                    "########################################"
                )

                logging.warning(
                    "NEW LOW BREAKDOWN"
                )

                logging.warning(
                    f"OLD LOW  = {old_low}"
                )

                logging.warning(
                    f"PRICE     = {price}"
                )

                logging.warning(
                    f"SHORT SL  = {sl}"
                )

                logging.warning(
                    "########################################"
                )

                entered = self.enter_position(
                    "SHORT",
                    price,
                    sl,
                    "NEW LOW BREAKDOWN"
                )

                if entered:

                    with self.lock:

                        self.running_low = price
                        self.save_state()

                    return

        # ----------------------------------------------------
        # No entry.
        #
        # Now update today's global HIGH/LOW.
        # ----------------------------------------------------

        changed = False

        with self.lock:

            if (
                self.running_high is None
                or price > self.running_high
            ):

                self.running_high = price
                changed = True

            if (
                self.running_low is None
                or price < self.running_low
            ):

                self.running_low = price
                changed = True

            if changed:
                self.save_state()


    # ========================================================
    # PROCESS PRIVATE POSITION EVENT
    # ========================================================

    def process_position_event(
        self,
        message
    ):

        try:

            action = message.get(
                "action"
            )

            symbol = (
                message.get(
                    "symbol"
                )
                or message.get(
                    "product_symbol"
                )
            )

            if (
                symbol
                and symbol != SYMBOL
            ):
                return

            # Snapshot format:
            #
            # result = [...]
            # ------------------------------------------------

            if action == "snapshot":

                result = message.get(
                    "result",
                    []
                )

                found = False

                if isinstance(
                    result,
                    list
                ):

                    for item in result:

                        item_symbol = (
                            item.get(
                                "symbol"
                            )
                            or item.get(
                                "product_symbol"
                            )
                        )

                        if (
                            item_symbol
                            == SYMBOL
                        ):

                            self.update_position(
                                int(
                                    item.get(
                                        "size",
                                        0
                                    )
                                ),
                                item.get(
                                    "entry_price"
                                )
                            )

                            found = True
                            break

                if not found:

                    self.update_position(
                        0
                    )

                return

            # ------------------------------------------------
            # Incremental update.
            # ------------------------------------------------

            if symbol == SYMBOL:

                self.update_position(
                    int(
                        message.get(
                            "size",
                            0
                        )
                    ),
                    message.get(
                        "entry_price"
                    )
                )

        except Exception as exc:

            logging.exception(
                f"Position event error: {exc}"
            )


    # ========================================================
    # PROCESS ORDER EVENT
    # ========================================================

    def process_order_event(
        self,
        message
    ):

        try:

            symbol = (
                message.get(
                    "symbol"
                )
                or message.get(
                    "product_symbol"
                )
            )

            if (
                symbol
                and symbol != SYMBOL
            ):
                return

            reason = (
                message.get(
                    "reason"
                )
                or ""
            )

            reason = str(
                reason
            ).lower()

            # ------------------------------------------------
            # STOP TRIGGER
            # ------------------------------------------------

            if reason == "stop_trigger":

                with self.lock:

                    current_position = (
                        self.position_size
                    )

                    price = (
                        self.last_price
                    )

                logging.warning(
                    "========================================"
                )

                logging.warning(
                    "EXCHANGE STOP TRIGGER RECEIVED"
                )

                logging.warning(
                    f"POSITION={current_position}"
                )

                logging.warning(
                    f"LAST PRICE={price}"
                )

                logging.warning(
                    "========================================"
                )

                if current_position > 0:

                    self.prepare_long_stop(
                        price
                    )

                elif current_position < 0:

                    self.prepare_short_stop(
                        price
                    )

                return

            # ------------------------------------------------
            # Fill
            # ------------------------------------------------

            if reason == "fill":

                logging.info(
                    "ORDER FILL EVENT | "
                    f"{message}"
                )

        except Exception as exc:

            logging.exception(
                f"Order event error: {exc}"
            )


    # ========================================================
    # PROCESS USER TRADE
    # ========================================================

    def process_user_trade(
        self,
        message
    ):

        try:

            symbol = (
                message.get(
                    "sy"
                )
                or message.get(
                    "symbol"
                )
            )

            if symbol != SYMBOL:
                return

            price = (
                message.get(
                    "p"
                )
                or message.get(
                    "price"
                )
            )

            side = (
                message.get(
                    "S"
                )
                or message.get(
                    "side"
                )
            )

            logging.info(
                "USER FILL | "
                f"SIDE={side} | "
                f"PRICE={price}"
            )

        except Exception:
            pass


    # ========================================================
    # MARKET WORKER
    # ========================================================

    def market_worker(
        self
    ):

        while True:

            try:

                price = market_queue.get()

                self.process_price(
                    price
                )

            except Exception as exc:

                logging.exception(
                    f"MARKET WORKER ERROR: {exc}"
                )


    # ========================================================
    # PRIVATE WORKER
    # ========================================================

    def private_worker(
        self
    ):

        while True:

            try:

                message = (
                    private_queue.get()
                )

                msg_type = message.get(
                    "type"
                )

                if msg_type == "positions":

                    self.process_position_event(
                        message
                    )

                elif msg_type == "orders":

                    self.process_order_event(
                        message
                    )

                elif msg_type == "v2/user_trades":

                    self.process_user_trade(
                        message
                    )

            except Exception as exc:

                logging.exception(
                    f"PRIVATE WORKER ERROR: {exc}"
                )


# ============================================================
# PUBLIC WEBSOCKET
# ============================================================

def start_public_websocket():

    def on_open(
        ws
    ):

        logging.warning(
            "PUBLIC WS CONNECTED"
        )

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

        logging.warning(
            f"SUBSCRIBED | TRADES | {SYMBOL}"
        )


    def on_message(
        ws,
        raw
    ):

        try:

            data = json.loads(
                raw
            )

            if data.get(
                "type"
            ) != "trades":

                return

            symbol = (
                data.get(
                    "sy"
                )
                or data.get(
                    "symbol"
                )
            )

            if symbol != SYMBOL:
                return

            price = (
                data.get(
                    "p"
                )
                or data.get(
                    "price"
                )
            )

            if price is None:
                return

            try:

                market_queue.put_nowait(
                    str(price)
                )

            except queue.Full:

                logging.error(
                    "MARKET QUEUE FULL!"
                )

        except Exception as exc:

            logging.exception(
                f"PUBLIC WS MESSAGE ERROR: {exc}"
            )


    def on_error(
        ws,
        error
    ):

        logging.error(
            f"PUBLIC WS ERROR: {error}"
        )


    def on_close(
        ws,
        code,
        reason
    ):

        logging.warning(
            "PUBLIC WS CLOSED | "
            f"CODE={code} | "
            f"REASON={reason}"
        )


    while True:

        try:

            ws = websocket.WebSocketApp(
                PUBLIC_WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )

            logging.warning(
                f"CONNECTING PUBLIC WS | "
                f"{PUBLIC_WS_URL}"
            )

            ws.run_forever(
                ping_interval=30,
                ping_timeout=10
            )

        except Exception as exc:

            logging.exception(
                f"PUBLIC WS CRASH: {exc}"
            )

        logging.warning(
            "PUBLIC WS RECONNECTING IN 3 SECONDS..."
        )

        time.sleep(3)


# ============================================================
# PRIVATE WEBSOCKET
# ============================================================

def private_signature():

    timestamp = str(
        int(time.time())
    )

    message = (
        "GET"
        + timestamp
        + "/live"
    )

    signature = hmac.new(
        API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

    return (
        timestamp,
        signature
    )


def start_private_websocket():

    def subscribe(
        ws,
        channel
    ):

        payload = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {
                        "name": channel,
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


    def on_open(
        ws
    ):

        logging.warning(
            "PRIVATE WS CONNECTED"
        )

        timestamp, signature = (
            private_signature()
        )

        auth_message = {
            "type": "key-auth",
            "payload": {
                "api-key": API_KEY,
                "timestamp": timestamp,
                "signature": signature
            }
        }

        ws.send(
            json.dumps(
                auth_message
            )
        )

        logging.warning(
            "PRIVATE WS AUTH SENT"
        )


    def on_message(
        ws,
        raw
    ):

        try:

            data = json.loads(
                raw
            )

            msg_type = data.get(
                "type"
            )

            # ------------------------------------------------
            # Authentication
            # ------------------------------------------------

            if msg_type == "key-auth":

                if data.get(
                    "success"
                ):

                    logging.warning(
                        "PRIVATE WS AUTHENTICATED"
                    )

                    subscribe(
                        ws,
                        "positions"
                    )

                    subscribe(
                        ws,
                        "orders"
                    )

                    subscribe(
                        ws,
                        "v2/user_trades"
                    )

                    logging.warning(
                        "PRIVATE WS SUBSCRIPTIONS SENT"
                    )

                else:

                    logging.error(
                        f"PRIVATE WS AUTH FAILED | "
                        f"{data}"
                    )

                return

            # ------------------------------------------------
            # Position
            # ------------------------------------------------

            if msg_type == "positions":

                private_queue.put(
                    data
                )

                return

            # ------------------------------------------------
            # Orders
            # ------------------------------------------------

            if msg_type == "orders":

                private_queue.put(
                    data
                )

                return

            # ------------------------------------------------
            # User trades
            # ------------------------------------------------

            if msg_type == "v2/user_trades":

                private_queue.put(
                    data
                )

                return

        except Exception as exc:

            logging.exception(
                f"PRIVATE WS MESSAGE ERROR: {exc}"
            )


    def on_error(
        ws,
        error
    ):

        logging.error(
            f"PRIVATE WS ERROR: {error}"
        )


    def on_close(
        ws,
        code,
        reason
    ):

        logging.warning(
            "PRIVATE WS CLOSED | "
            f"CODE={code} | "
            f"REASON={reason}"
        )


    while True:

        try:

            ws = websocket.WebSocketApp(
                PRIVATE_WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )

            logging.warning(
                f"CONNECTING PRIVATE WS | "
                f"{PRIVATE_WS_URL}"
            )

            ws.run_forever(
                ping_interval=30,
                ping_timeout=10
            )

        except Exception as exc:

            logging.exception(
                f"PRIVATE WS CRASH: {exc}"
            )

        logging.warning(
            "PRIVATE WS RECONNECTING IN 3 SECONDS..."
        )

        time.sleep(3)


# ============================================================
# SESSION CLOCK
#
# This makes the 05:45 reset independent of market ticks.
# ============================================================

def session_clock(
    engine
):

    last_session_day = None

    while True:

        try:

            now = now_ist()

            if is_weekend_blocked(
                now
            ):

                time.sleep(1)

                continue

            current_day = (
                trading_day_start(
                    now
                )
            )

            start_time = (
                strategy_start_time(
                    current_day
                )
            )

            if (
                now >= start_time
                and last_session_day
                != current_day
            ):

                logging.warning(
                    "SESSION CLOCK | "
                    f"05:45 SESSION START | "
                    f"{current_day}"
                )

                # Only start a new session if the engine
                # has not already initialized this same day.
                engine.ensure_session(
                    now
                )

                last_session_day = (
                    current_day
                )

            time.sleep(
                1
            )

        except Exception as exc:

            logging.exception(
                f"SESSION CLOCK ERROR: {exc}"
            )

            time.sleep(
                2
            )


# ============================================================
# LOW-FREQUENCY WATCHDOG
#
# REST position check every few seconds.
#
# This is ONLY a safety fallback.
# It is NOT the market-data engine.
# ============================================================

def watchdog(
    engine
):

    while True:

        try:

            time.sleep(
                WATCHDOG_SECONDS
            )

            now = now_ist()

            if is_weekend_blocked(
                now
            ):

                continue

            # ------------------------------------------------
            # Do not interfere before strategy start.
            # ------------------------------------------------

            if now < strategy_start_time(
                trading_day_start(now)
            ):

                continue

            # ------------------------------------------------
            # REST position check.
            # ------------------------------------------------

            position = get_position(
                engine.product_id
            )

            exchange_size = (
                position["size"]
            )

            exchange_entry = (
                position["entry_price"]
            )

            with engine.lock:

                local_size = (
                    engine.position_size
                )

                local_stop = (
                    engine.current_sl
                )

                local_stop_triggered = (
                    engine.stop_triggered
                )

                current_price = (
                    engine.last_price
                )

            # ------------------------------------------------
            # Exchange has position, local doesn't.
            # ------------------------------------------------

            if (
                exchange_size != 0
                and local_size == 0
            ):

                logging.warning(
                    "WATCHDOG SYNC | "
                    f"EXCHANGE POSITION={exchange_size}"
                )

                engine.update_position(
                    exchange_size,
                    exchange_entry
                )

                continue

            # ------------------------------------------------
            # Exchange is flat, local says position.
            # ------------------------------------------------

            if (
                exchange_size == 0
                and local_size != 0
            ):

                logging.warning(
                    "WATCHDOG | "
                    "EXCHANGE FLAT BUT LOCAL POSITION EXISTS"
                )

                # If price is beyond the stored SL,
                # prepare reversal.
                if (
                    local_stop is not None
                    and current_price is not None
                ):

                    if (
                        local_size > 0
                        and current_price
                        <= local_stop
                    ):

                        engine.prepare_long_stop(
                            current_price
                        )

                    elif (
                        local_size < 0
                        and current_price
                        >= local_stop
                    ):

                        engine.prepare_short_stop(
                            current_price
                        )

                # Force position update to zero.
                engine.update_position(
                    0
                )

        except Exception as exc:

            logging.exception(
                f"WATCHDOG ERROR: {exc}"

            )

            time.sleep(
                2
            )


# ============================================================
# STARTUP
# ============================================================

def main():

    logging.warning(
        "===================================================="
    )

    logging.warning(
        "XAUTUSD EXTREME BREAKOUT / REVERSAL BOT v30.0"
    )

    logging.warning(
        "===================================================="
    )

    logging.warning(
        f"SYMBOL       = {SYMBOL}"
    )

    logging.warning(
        f"LEVERAGE     = {LEVERAGE}x"
    )

    logging.warning(
        f"BALANCE USE  = {BALANCE_FRACTION * 100}%"
    )

    logging.warning(
        f"REST         = {BASE_URL}"
    )

    logging.warning(
        f"PUBLIC WS    = {PUBLIC_WS_URL}"
    )

    logging.warning(
        f"PRIVATE WS   = {PRIVATE_WS_URL}"
    )

    logging.warning(
        "START TIME   = 05:45 IST"
    )

    logging.warning(
        "MARKET FEED  = REAL-TIME TRADES"
    )

    logging.warning(
        "POSITION     = PRIVATE WEBSOCKET + WATCHDOG"
    )

    logging.warning(
        "===================================================="
    )

    # --------------------------------------------------------
    # Product
    # --------------------------------------------------------

    product = get_product()

    product_id = int(
        product["id"]
    )

    logging.warning(
        f"PRODUCT ID = {product_id}"
    )

    # --------------------------------------------------------
    # Leverage
    # --------------------------------------------------------

    set_leverage(
        product_id
    )

    # --------------------------------------------------------
    # Engine
    # --------------------------------------------------------

    engine = TradingEngine(
        product
    )

    # --------------------------------------------------------
    # Startup position sync.
    # --------------------------------------------------------

    try:

        position = get_position(
            product_id
        )

        logging.warning(
            "STARTUP EXCHANGE POSITION | "
            f"SIZE={position['size']} | "
            f"ENTRY={position['entry_price']}"
        )

        # ----------------------------------------------------
        # If saved state belongs to today's session,
        # preserve it.
        #
        # Otherwise the session initializer will handle it.
        # ----------------------------------------------------

        current_day = (
            trading_day_start(
                now_ist()
            )
        )

        with engine.lock:

            if (
                engine.day_start
                == current_day
                and engine.session_initialized
            ):

                engine.position_size = (
                    position["size"]
                )

                if position["entry_price"] is not None:

                    engine.entry_price = (
                        Decimal(
                            str(
                                position[
                                    "entry_price"
                                ]
                            )
                        )
                    )

                engine.save_state()

            else:

                # If a position exists but there is no valid
                # current-session saved state, don't blindly
                # continue with an unknown SL.
                if (
                    position["size"] != 0
                    and now_ist()
                    >= strategy_start_time(
                        current_day
                    )
                ):

                    logging.warning(
                        "POSITION EXISTS WITHOUT "
                        "VALID CURRENT SESSION STATE."
                    )

                    logging.warning(
                        "Closing it before starting "
                        "a clean session."
                    )

                    close_position_market(
                        product_id,
                        position["size"]
                    )

                    deadline = (
                        time.time() + 10
                    )

                    while (
                        time.time()
                        < deadline
                    ):

                        time.sleep(
                            0.25
                        )

                        check = get_position(
                            product_id
                        )

                        if check["size"] == 0:
                            break

                    if get_position(
                        product_id
                    )["size"] != 0:

                        raise RuntimeError(
                            "Could not flatten unknown "
                            "startup position."
                        )

                    engine.position_size = 0

    except Exception as exc:

        logging.exception(
            f"STARTUP POSITION ERROR: {exc}"
        )

        raise

    # --------------------------------------------------------
    # Workers
    # --------------------------------------------------------

    threading.Thread(
        target=engine.market_worker,
        daemon=True,
        name="MarketWorker"
    ).start()

    threading.Thread(
        target=engine.private_worker,
        daemon=True,
        name="PrivateWorker"
    ).start()

    # --------------------------------------------------------
    # Public WebSocket
    # --------------------------------------------------------

    threading.Thread(
        target=start_public_websocket,
        daemon=True,
        name="PublicWS"
    ).start()

    # --------------------------------------------------------
    # Private WebSocket
    # --------------------------------------------------------

    threading.Thread(
        target=start_private_websocket,
        daemon=True,
        name="PrivateWS"
    ).start()

    # --------------------------------------------------------
    # Session clock
    # --------------------------------------------------------

    threading.Thread(
        target=session_clock,
        args=(engine,),
        daemon=True,
        name="SessionClock"
    ).start()

    # --------------------------------------------------------
    # Watchdog
    # --------------------------------------------------------

    threading.Thread(
        target=watchdog,
        args=(engine,),
        daemon=True,
        name="Watchdog"
    ).start()

    # --------------------------------------------------------
    # Startup session recovery
    # --------------------------------------------------------

    try:

        now = now_ist()

        if (
            not is_weekend_blocked(now)
            and now >= strategy_start_time(
                trading_day_start(now)
            )
        ):

            engine.ensure_session(
                now
            )

    except Exception as exc:

        logging.exception(
            f"INITIAL SESSION RECOVERY ERROR: {exc}"
        )

    # --------------------------------------------------------
    # Main process
    # --------------------------------------------------------

    logging.warning(
        "===================================================="
    )

    logging.warning(
        "BOT IS LIVE"
    )

    logging.warning(
        "===================================================="
    )

    while True:

        try:

            time.sleep(
                10
            )

        except KeyboardInterrupt:

            logging.warning(
                "BOT STOPPED BY USER."
            )

            break

        except Exception as exc:

            logging.exception(
                f"MAIN LOOP ERROR: {exc}"
            )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        logging.warning(
            "BOT STOPPED BY USER."
        )

    except Exception as exc:

        logging.exception(
            f"FATAL BOT ERROR: {exc}"
        )

        raise
