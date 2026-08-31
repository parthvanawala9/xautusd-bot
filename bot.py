import os
import time
import json
import hmac
import hashlib
import logging
import threading
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
import websocket
from dotenv import load_dotenv


# ============================================================
# XAUTUSD NEW HIGH / NEW LOW BREAKOUT + REVERSAL BOT
# VERSION 27.1
#
# STRATEGY
# ------------------------------------------------------------
# 05:45 IST:
#   - New trading day starts.
#   - Any old position is closed.
#   - Old HIGH/LOW are discarded.
#
# NORMAL START:
#   - First tick after 05:45 establishes HIGH and LOW.
#
# LATE START:
#   - If bot starts after 05:45, it loads today's complete
#     05:45 -> current-time HIGH and LOW from Delta history.
#   - If current price is already above today's HIGH:
#       LONG immediately.
#   - If current price is already below today's LOW:
#       SHORT immediately.
#   - Otherwise it waits for the next breakout.
#
# FLAT:
#   - New HIGH -> LONG immediately.
#   - New LOW  -> SHORT immediately.
#
# LONG:
#   - SL = LOW that existed when LONG was entered.
#   - Track highest peak made during LONG.
#   - If SL hits -> immediately SHORT.
#   - SHORT SL = highest peak made during that LONG.
#
# SHORT:
#   - SL = HIGH that existed when SHORT was entered.
#   - Track lowest low made during SHORT.
#   - If SL hits -> immediately LONG.
#   - LONG SL = lowest low made during that SHORT.
#
# ONE POSITION ONLY.
#
# Saturday:
#   - 05:00 IST square-off.
#
# Sunday:
#   - No trading.
#
# IMPORTANT:
#   bot.py itself does NOT depend on GitHub Actions.
#   It is intended to run continuously on the Oracle VM.
# ============================================================


load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")

WS_URL = os.getenv(
    "DELTA_PUBLIC_WS_URL",
    os.getenv(
        "DELTA_WS_URL",
        "wss://public-socket.india.delta.exchange"
    )
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

POLL_SECONDS = float(
    os.getenv(
        "POLL_SECONDS",
        "0.25"
    )
)

POSITION_CHECK_SECONDS = float(
    os.getenv(
        "POSITION_CHECK_SECONDS",
        "1.0"
    )
)


if not API_KEY or not API_SECRET:
    raise SystemExit(
        "Missing DELTA_API_KEY or DELTA_API_SECRET in environment."
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
    "User-Agent": "XAUTUSD-NewExtreme-Engine/27.1"
})


# ============================================================
# TIME HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def trading_day_start(dt=None):
    """
    Trading date boundary = 05:30 IST.

    Strategy itself starts at 05:45 IST.
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
    """
    Actual strategy activation = 05:45 IST.
    """

    return day_start + timedelta(
        minutes=15
    )


def is_strategy_active(dt=None):
    dt = dt or now_ist()

    if is_weekend_blocked(dt):
        return False

    day_start = trading_day_start(dt)

    return dt >= strategy_start_time(
        day_start
    )


def is_weekend_blocked(dt=None):
    dt = dt or now_ist()

    weekday = dt.weekday()

    # Saturday
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


def is_saturday_squareoff_time(dt=None):
    dt = dt or now_ist()

    return (
        dt.weekday() == 5
        and dt.hour == 5
        and dt.minute < 30
    )


# ============================================================
# DELTA AUTHENTICATION
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
# DELTA REST API
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
        "?"
        + urlencode(
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
                f"Delta API Error: {data}"
            )

        return data

    except Exception as exc:

        raise RuntimeError(
            f"HTTP Request failed for "
            f"{method} {path}: {exc}"
        ) from exc


# ============================================================
# PRODUCT
# ============================================================

def get_product():

    result = api_call(
        "GET",
        f"/v2/products/{SYMBOL}"
    )

    return result["result"]


# ============================================================
# POSITION
# ============================================================

def get_position(
    product_id
):

    result = api_call(
        "GET",
        "/v2/positions",
        params={
            "product_id": int(
                product_id
            )
        },
        auth=True
    )["result"]

    if (
        not result
        or not isinstance(
            result,
            dict
        )
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
        "entry_price": result.get(
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
                wallet.get("balance")
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
# HISTORICAL CANDLES
# ============================================================

def get_historical_extremes(
    start_dt,
    end_dt
):
    """
    Get the actual HIGH and LOW between start_dt and end_dt.

    Uses Delta's 1-minute historical OHLC candles.

    This is used ONLY when the bot starts late, so it can
    reconstruct today's 05:45 -> startup range.
    """

    if end_dt <= start_dt:

        return None, None

    start_ts = int(
        start_dt.astimezone(
            UTC
        ).timestamp()
    )

    end_ts = int(
        end_dt.astimezone(
            UTC
        ).timestamp()
    )

    data = api_call(
        "GET",
        "/v2/history/candles",
        params={
            "resolution": "1m",
            "symbol": SYMBOL,
            "start": start_ts,
            "end": end_ts
        }
    )

    candles = data.get(
        "result",
        []
    )

    highest = None
    lowest = None

    for candle in candles:

        if not isinstance(
            candle,
            dict
        ):
            continue

        try:

            candle_time = int(
                candle.get(
                    "time"
                )
            )

            candle_high = Decimal(
                str(
                    candle.get(
                        "high"
                    )
                )
            )

            candle_low = Decimal(
                str(
                    candle.get(
                        "low"
                    )
                )
            )

        except (
            ValueError,
            TypeError,
            InvalidOperation
        ):

            continue

        # Ignore candles that start before strategy time.
        if candle_time < start_ts:
            continue

        # Ignore candles that start after requested end.
        if candle_time > end_ts:
            continue

        if (
            highest is None
            or candle_high > highest
        ):

            highest = candle_high

        if (
            lowest is None
            or candle_low < lowest
        ):

            lowest = candle_low

    return highest, lowest


# ============================================================
# LEVERAGE
# ============================================================

def set_leverage(
    product_id
):

    try:

        api_call(
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
            f"LEVERAGE SET = "
            f"{LEVERAGE}x"
        )

    except Exception as exc:

        logging.warning(
            f"Leverage setting failed: "
            f"{exc}"
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

        contract_value = Decimal(
            "1"
        )

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

        lot_size = Decimal(
            "1"
        )

    min_size = Decimal(
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

    if size_decimal < min_size:

        size_decimal = min_size

    size = int(
        size_decimal
    )

    if size <= 0:

        raise RuntimeError(
            "Calculated order size is zero."
        )

    logging.info(
        f"ORDER SIZE | "
        f"BALANCE={balance} | "
        f"MARGIN={margin} | "
        f"NOTIONAL={notional} | "
        f"SIZE={size}"
    )

    return size


# ============================================================
# MARKET ENTRY + BRACKET STOP
# ============================================================

def execute_bracket_market_order(
    product_id,
    side,
    size,
    sl_price
):

    if sl_price is None:

        raise RuntimeError(
            "Cannot place entry: "
            "SL price is None."
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

        "bracket_stop_loss_price": str(
            sl_price
        ),

        "bracket_stop_trigger_method":
            "last_traded_price",

        "client_order_id":
            f"xent_{int(time.time() * 1000)}"[-32:]
    }

    logging.warning(
        "LIVE ENTRY | "
        f"SIDE={side.upper()} | "
        f"SIZE={abs(size)} | "
        f"SL={sl_price}"
    )

    result = api_call(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )

    order = result.get(
        "result",
        {}
    )

    order_id = order.get(
        "id"
    )

    logging.warning(
        f"ENTRY ORDER ACCEPTED | "
        f"ORDER_ID={order_id}"
    )

    return result


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
            f"xexit_{int(time.time() * 1000)}"[-32:]
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
# STATE ENGINE
# ============================================================

class TradingStrategy:

    def __init__(
        self,
        product
    ):

        self.product = product

        self.product_id = int(
            product["id"]
        )

        # ----------------------------------------------------
        # CURRENT TRADING DAY
        # ----------------------------------------------------

        self.day_start = None

        # ----------------------------------------------------
        # GLOBAL EXTREMES AFTER 05:45
        # ----------------------------------------------------

        self.running_high = None
        self.running_low = None

        # ----------------------------------------------------
        # CURRENT POSITION
        # ----------------------------------------------------

        self.last_position = 0

        # ----------------------------------------------------
        # CURRENT STOP
        # ----------------------------------------------------

        self.current_sl = None

        # ----------------------------------------------------
        # CURRENT TRADE PEAK / TROUGH
        # ----------------------------------------------------

        self.trade_high = None
        self.trade_low = None

        # ----------------------------------------------------
        # LAST PRICE
        # ----------------------------------------------------

        self.last_price = None

        # ----------------------------------------------------
        # BASELINE
        # ----------------------------------------------------

        self.baseline_ready = False

        # ----------------------------------------------------
        # LATE START SYNCHRONIZATION
        # ----------------------------------------------------

        self.history_loaded = False

        # ----------------------------------------------------
        # PREVENT DOUBLE ORDERS
        # ----------------------------------------------------

        self.order_in_flight = False

        # ----------------------------------------------------
        # DAY RESET
        # ----------------------------------------------------

        self.day_reset_done = False

        self.load_state()


    # ========================================================
    # LOAD STATE
    # ========================================================

    def load_state(self):

        if not os.path.exists(
            STATE_FILE
        ):

            logging.info(
                "No previous state file."
            )

            return

        try:

            with open(
                STATE_FILE,
                "r",
                encoding="utf-8"
            ) as file:

                state = json.load(
                    file
                )

            self.day_start = (
                datetime.fromisoformat(
                    state["day_start"]
                )
                if state.get(
                    "day_start"
                )
                else None
            )

            self.running_high = (
                Decimal(
                    state["running_high"]
                )
                if state.get(
                    "running_high"
                )
                else None
            )

            self.running_low = (
                Decimal(
                    state["running_low"]
                )
                if state.get(
                    "running_low"
                )
                else None
            )

            self.current_sl = (
                Decimal(
                    state["current_sl"]
                )
                if state.get(
                    "current_sl"
                )
                else None
            )

            self.trade_high = (
                Decimal(
                    state["trade_high"]
                )
                if state.get(
                    "trade_high"
                )
                else None
            )

            self.trade_low = (
                Decimal(
                    state["trade_low"]
                )
                if state.get(
                    "trade_low"
                )
                else None
            )

            self.baseline_ready = bool(
                state.get(
                    "baseline_ready",
                    False
                )
            )

            self.day_reset_done = bool(
                state.get(
                    "day_reset_done",
                    False
                )
            )

            logging.info(
                "STATE LOADED | "
                f"DAY={self.day_start} | "
                f"HIGH={self.running_high} | "
                f"LOW={self.running_low} | "
                f"SL={self.current_sl}"
            )

        except Exception as exc:

            logging.error(
                f"STATE LOAD ERROR: {exc}"
            )


    # ========================================================
    # SAVE STATE
    # ========================================================

    def save_state(self):

        state = {

            "day_start":
                self.day_start.isoformat()
                if self.day_start
                else None,

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

            "current_sl":
                str(
                    self.current_sl
                )
                if self.current_sl is not None
                else None,

            "trade_high":
                str(
                    self.trade_high
                )
                if self.trade_high is not None
                else None,

            "trade_low":
                str(
                    self.trade_low
                )
                if self.trade_low is not None
                else None,

            "baseline_ready":
                self.baseline_ready,

            "day_reset_done":
                self.day_reset_done
        }

        temp_file = (
            STATE_FILE
            + ".tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                state,
                file,
                indent=2
            )

        os.replace(
            temp_file,
            STATE_FILE
        )


    # ========================================================
    # NEW TRADING DAY
    # ========================================================

    def handle_new_day(
        self,
        now,
        current_position
    ):

        new_day = trading_day_start(
            now
        )

        if self.day_start == new_day:

            return

        logging.warning(
            "NEW TRADING DAY | "
            f"{new_day}"
        )

        self.day_start = new_day

        self.running_high = None
        self.running_low = None

        self.current_sl = None

        self.trade_high = None
        self.trade_low = None

        self.baseline_ready = False

        self.history_loaded = False

        self.day_reset_done = False

        if current_position != 0:

            logging.warning(
                "OLD POSITION DETECTED ON NEW DAY. "
                "WILL CLOSE IT AT 05:45 IST."
            )

        self.save_state()


    # ========================================================
    # 05:45 RESET
    # ========================================================

    def perform_0545_reset(
        self,
        now
    ):

        if is_weekend_blocked(
            now
        ):

            return

        if now < strategy_start_time(
            self.day_start
        ):

            return

        if self.day_reset_done:

            return

        logging.warning(
            "===== 05:45 IST NEW STRATEGY SESSION ====="
        )

        position = get_position(
            self.product_id
        )

        current_size = position[
            "size"
        ]

        # ----------------------------------------------------
        # CLOSE ANY OLD POSITION
        # ----------------------------------------------------

        if current_size != 0:

            logging.warning(
                "05:45 RESET | "
                f"CLOSING OLD POSITION "
                f"SIZE={current_size}"
            )

            close_position_market(
                self.product_id,
                current_size
            )

            for _ in range(30):

                time.sleep(
                    0.20
                )

                check = get_position(
                    self.product_id
                )

                if check["size"] == 0:

                    break

            final_check = get_position(
                self.product_id
            )

            if final_check["size"] != 0:

                logging.error(
                    "05:45 RESET FAILED: "
                    "OLD POSITION STILL OPEN."
                )

                return

        # ----------------------------------------------------
        # FRESH SESSION
        # ----------------------------------------------------

        self.last_position = 0

        self.current_sl = None

        self.running_high = None
        self.running_low = None

        self.trade_high = None
        self.trade_low = None

        self.baseline_ready = False

        self.history_loaded = False

        self.day_reset_done = True

        self.save_state()

        logging.warning(
            "===== 05:45 RESET COMPLETE ====="
        )


    # ========================================================
    # NORMAL FIRST-TICK BASELINE
    # ========================================================

    def establish_baseline(
        self,
        price
    ):

        self.running_high = price

        self.running_low = price

        self.baseline_ready = True

        self.history_loaded = True

        self.save_state()

        logging.warning(
            "05:45 BASELINE CREATED | "
            f"HIGH={price} | "
            f"LOW={price}"
        )


    # ========================================================
    # LATE START HISTORICAL SYNCHRONIZATION
    # ========================================================

    def synchronize_late_start(
        self,
        now,
        current_price
    ):
        """
        If the bot starts after 05:45, reconstruct today's
        HIGH and LOW from 05:45 until now.

        Example:

            05:45 = 4410
            06:20 = 4440
            08:00 = 4380
            09:45 = 4420

        The bot starts at 09:45.

        It will load:

            HIGH = 4440
            LOW  = 4380

        Then:

            current > 4440 -> LONG
            current < 4380 -> SHORT
            otherwise      -> wait for future breakout
        """

        if self.history_loaded:

            return True

        session_start = strategy_start_time(
            self.day_start
        )

        if now < session_start:

            return False

        logging.warning(
            "LATE START DETECTED | "
            "LOADING TODAY'S 05:45 -> NOW HIGH/LOW..."
        )

        try:

            historical_high, historical_low = (
                get_historical_extremes(
                    session_start,
                    now
                )
            )

        except Exception as exc:

            logging.error(
                "HISTORICAL HIGH/LOW LOAD FAILED | "
                f"{exc}"
            )

            return False

        # ----------------------------------------------------
        # Make sure current live price is included.
        # ----------------------------------------------------

        if historical_high is None:

            historical_high = current_price

        if historical_low is None:

            historical_low = current_price

        if current_price > historical_high:

            historical_high = current_price

        if current_price < historical_low:

            historical_low = current_price

        self.running_high = historical_high

        self.running_low = historical_low

        self.baseline_ready = True

        self.history_loaded = True

        self.save_state()

        logging.warning(
            "LATE START RANGE LOADED | "
            f"05:45->NOW HIGH={self.running_high} | "
            f"LOW={self.running_low} | "
            f"CURRENT={current_price}"
        )

        return True


    # ========================================================
    # ENTRY
    # ========================================================

    def execute_entry(
        self,
        direction,
        price,
        sl_price,
        reason
    ):

        if self.order_in_flight:

            logging.warning(
                "ENTRY BLOCKED | "
                "Another order is already in flight."
            )

            return False

        if not is_strategy_active():

            return False

        if sl_price is None:

            logging.error(
                "ENTRY BLOCKED | SL is None."
            )

            return False

        # ----------------------------------------------------
        # LONG SL MUST BE BELOW ENTRY
        # ----------------------------------------------------

        if direction == "LONG":

            if sl_price >= price:

                logging.error(
                    "LONG ENTRY BLOCKED | "
                    f"PRICE={price} | "
                    f"SL={sl_price}"
                )

                return False

        # ----------------------------------------------------
        # SHORT SL MUST BE ABOVE ENTRY
        # ----------------------------------------------------

        elif direction == "SHORT":

            if sl_price <= price:

                logging.error(
                    "SHORT ENTRY BLOCKED | "
                    f"PRICE={price} | "
                    f"SL={sl_price}"
                )

                return False

        else:

            logging.error(
                f"Unknown direction: {direction}"
            )

            return False

        # ----------------------------------------------------
        # REAL EXCHANGE POSITION CHECK
        # ----------------------------------------------------

        existing = get_position(
            self.product_id
        )

        if existing["size"] != 0:

            logging.warning(
                "ENTRY BLOCKED | "
                f"Exchange position already open: "
                f"{existing['size']}"
            )

            self.last_position = (
                existing["size"]
            )

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

        self.order_in_flight = True

        try:

            execute_bracket_market_order(
                self.product_id,
                side,
                size,
                sl_price
            )

            filled_size = 0

            for _ in range(40):

                time.sleep(
                    0.20
                )

                position = get_position(
                    self.product_id
                )

                position_size = position[
                    "size"
                ]

                if direction == "LONG":

                    if position_size > 0:

                        filled_size = (
                            position_size
                        )

                        break

                else:

                    if position_size < 0:

                        filled_size = (
                            position_size
                        )

                        break

            if filled_size == 0:

                raise RuntimeError(
                    "Entry order was sent but "
                    "position fill was not confirmed."
                )

            self.last_position = (
                filled_size
            )

            self.current_sl = Decimal(
                str(sl_price)
            )

            if direction == "LONG":

                self.trade_high = price

                self.trade_low = None

            else:

                self.trade_low = price

                self.trade_high = None

            self.save_state()

            logging.warning(
                "========================================"
            )

            logging.warning(
                f"ENTRY CONFIRMED | "
                f"{direction} | "
                f"SIZE={filled_size} | "
                f"ENTRY≈{price} | "
                f"SL={sl_price} | "
                f"REASON={reason}"
            )

            logging.warning(
                "========================================"
            )

            return True

        finally:

            self.order_in_flight = False


    # ========================================================
    # LONG -> SHORT
    # ========================================================

    def reverse_long_to_short(
        self,
        price
    ):

        peak_high = (
            self.trade_high
            or self.running_high
        )

        if peak_high is None:

            logging.error(
                "LONG -> SHORT reversal blocked: "
                "no peak high available."
            )

            return False

        logging.warning(
            "LONG STOP HIT -> "
            f"REVERSING SHORT | "
            f"SHORT SL={peak_high}"
        )

        position = get_position(
            self.product_id
        )

        if position["size"] != 0:

            logging.warning(
                "Waiting for LONG position "
                "to become flat before reversal."
            )

            for _ in range(30):

                time.sleep(
                    0.20
                )

                position = get_position(
                    self.product_id
                )

                if position["size"] == 0:

                    break

        if get_position(
            self.product_id
        )["size"] != 0:

            logging.error(
                "LONG -> SHORT reversal blocked: "
                "old position still open."
            )

            return False

        return self.execute_entry(
            "SHORT",
            price,
            peak_high,
            "LONG SL HIT -> REVERSE SHORT"
        )


    # ========================================================
    # SHORT -> LONG
    # ========================================================

    def reverse_short_to_long(
        self,
        price
    ):

        trough_low = (
            self.trade_low
            or self.running_low
        )

        if trough_low is None:

            logging.error(
                "SHORT -> LONG reversal blocked: "
                "no trough low available."
            )

            return False

        logging.warning(
            "SHORT STOP HIT -> "
            f"REVERSING LONG | "
            f"LONG SL={trough_low}"
        )

        position = get_position(
            self.product_id
        )

        if position["size"] != 0:

            logging.warning(
                "Waiting for SHORT position "
                "to become flat before reversal."
            )

            for _ in range(30):

                time.sleep(
                    0.20
                )

                position = get_position(
                    self.product_id
                )

                if position["size"] == 0:

                    break

        if get_position(
            self.product_id
        )["size"] != 0:

            logging.error(
                "SHORT -> LONG reversal blocked: "
                "old position still open."
            )

            return False

        return self.execute_entry(
            "LONG",
            price,
            trough_low,
            "SHORT SL HIT -> REVERSE LONG"
        )


    # ========================================================
    # POSITION CLOSED
    # ========================================================

    def handle_position_closed(
        self,
        old_size,
        current_price
    ):

        old_sl = self.current_sl

        sl_triggered = False

        if old_sl is not None:

            if (
                old_size > 0
                and current_price <= old_sl
            ):

                sl_triggered = True

            elif (
                old_size < 0
                and current_price >= old_sl
            ):

                sl_triggered = True

        self.current_sl = None

        self.last_position = 0

        if not sl_triggered:

            logging.warning(
                "POSITION CLOSED EXTERNALLY/MANUALLY | "
                "NO AUTOMATIC REVERSAL."
            )

            self.trade_high = None
            self.trade_low = None

            self.save_state()

            return

        if old_size > 0:

            self.reverse_long_to_short(
                current_price
            )

        else:

            self.reverse_short_to_long(
                current_price
            )


    # ========================================================
    # TICK ENGINE
    # ========================================================

    def on_tick(
        self,
        price_str
    ):

        try:

            current_price = Decimal(
                str(price_str)
            )

        except (
            InvalidOperation,
            ValueError,
            TypeError
        ):

            logging.warning(
                f"Invalid price received: "
                f"{price_str}"
            )

            return

        now = now_ist()

        self.last_price = current_price

        # ----------------------------------------------------
        # SATURDAY SQUARE-OFF
        # ----------------------------------------------------

        if is_saturday_squareoff_time(
            now
        ):

            position = get_position(
                self.product_id
            )

            if position["size"] != 0:

                logging.warning(
                    "SATURDAY 05:00 SQUARE-OFF | "
                    f"SIZE={position['size']}"
                )

                close_position_market(
                    self.product_id,
                    position["size"]
                )

            self.last_position = 0
            self.current_sl = None

            self.trade_high = None
            self.trade_low = None

            return

        # ----------------------------------------------------
        # WEEKEND
        # ----------------------------------------------------

        if is_weekend_blocked(
            now
        ):

            return

        # ----------------------------------------------------
        # DAY STATE
        # ----------------------------------------------------

        current_exchange_position = (
            get_position(
                self.product_id
            )
        )

        current_exchange_size = (
            current_exchange_position[
                "size"
            ]
        )

        self.handle_new_day(
            now,
            current_exchange_size
        )

        # ----------------------------------------------------
        # BEFORE 05:45
        # ----------------------------------------------------

        if now < strategy_start_time(
            self.day_start
        ):

            self.last_position = (
                current_exchange_size
            )

            return

        # ----------------------------------------------------
        # 05:45 RESET
        # ----------------------------------------------------

        self.perform_0545_reset(
            now
        )

        if not self.day_reset_done:

            return

        # ----------------------------------------------------
        # LATE START / NORMAL START
        #
        # If history is not loaded yet:
        #
        # - Started exactly after 05:45:
        #   use first live tick as baseline.
        #
        # - Started later:
        #   load complete 05:45 -> now history.
        # ----------------------------------------------------

        if not self.baseline_ready:

            position = get_position(
                self.product_id
            )

            if position["size"] != 0:

                logging.warning(
                    "Waiting for 05:45 reset "
                    "to become completely flat."
                )

                return

            session_start = (
                strategy_start_time(
                    self.day_start
                )
            )

            # ------------------------------------------------
            # If this is actually a late startup, load history.
            # ------------------------------------------------

            late_start = (
                now
                > session_start
                + timedelta(
                    seconds=10
                )
            )

            if late_start:

                if not self.synchronize_late_start(
                    now,
                    current_price
                ):

                    return

                # --------------------------------------------
                # After loading historical extremes, check
                # whether CURRENT PRICE is already outside
                # the historical range.
                # --------------------------------------------

                historical_high = (
                    self.running_high
                )

                historical_low = (
                    self.running_low
                )

                # IMPORTANT:
                # We need the historical range BEFORE adding
                # current price to it.
                #
                # synchronize_late_start already includes the
                # current price, so recover the range again
                # from history for this one startup decision.
                # --------------------------------------------

                try:

                    historical_high, historical_low = (
                        get_historical_extremes(
                            session_start,
                            now
                        )
                    )

                except Exception as exc:

                    logging.error(
                        "LATE START ENTRY CHECK FAILED | "
                        f"{exc}"
                    )

                    return

                if historical_high is None:

                    historical_high = current_price

                if historical_low is None:

                    historical_low = current_price

                # --------------------------------------------
                # CURRENT PRICE ALREADY BROKE TODAY'S HIGH
                # --------------------------------------------

                if current_price > historical_high:

                    logging.warning(
                        "LATE START HIGH BREAKOUT | "
                        f"HISTORICAL_HIGH={historical_high} | "
                        f"CURRENT={current_price} | "
                        f"SL={historical_low}"
                    )

                    entered = self.execute_entry(
                        "LONG",
                        current_price,
                        historical_low,
                        "LATE START -> TODAY HIGH BREAKOUT"
                    )

                    if entered:

                        self.running_high = current_price

                        self.running_low = historical_low

                        self.trade_high = current_price

                        self.baseline_ready = True

                        self.history_loaded = True

                        self.save_state()

                        return

                # --------------------------------------------
                # CURRENT PRICE ALREADY BROKE TODAY'S LOW
                # --------------------------------------------

                if current_price < historical_low:

                    logging.warning(
                        "LATE START LOW BREAKDOWN | "
                        f"HISTORICAL_LOW={historical_low} | "
                        f"CURRENT={current_price} | "
                        f"SL={historical_high}"
                    )

                    entered = self.execute_entry(
                        "SHORT",
                        current_price,
                        historical_high,
                        "LATE START -> TODAY LOW BREAKDOWN"
                    )

                    if entered:

                        self.running_high = historical_high

                        self.running_low = current_price

                        self.trade_low = current_price

                        self.baseline_ready = True

                        self.history_loaded = True

                        self.save_state()

                        return

                # --------------------------------------------
                # No historical breakout currently active.
                #
                # Keep the complete historical range and wait
                # for the NEXT live breakout.
                # --------------------------------------------

                self.running_high = historical_high

                self.running_low = historical_low

                self.baseline_ready = True

                self.history_loaded = True

                self.last_position = 0

                self.save_state()

                logging.warning(
                    "LATE START READY | "
                    f"HIGH={historical_high} | "
                    f"LOW={historical_low} | "
                    f"CURRENT={current_price} | "
                    "WAITING FOR NEXT BREAKOUT"
                )

                return

            # ------------------------------------------------
            # NORMAL START AROUND 05:45
            # ------------------------------------------------

            self.establish_baseline(
                current_price
            )

            self.last_position = 0

            return

        # ----------------------------------------------------
        # REAL EXCHANGE POSITION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        current_size = position[
            "size"
        ]

        # ----------------------------------------------------
        # POSITION CLOSED
        # ----------------------------------------------------

        if (
            current_size == 0
            and self.last_position != 0
        ):

            old_size = (
                self.last_position
            )

            self.handle_position_closed(
                old_size,
                current_price
            )

            return

        # ----------------------------------------------------
        # LONG
        # ----------------------------------------------------

        if current_size > 0:

            self.last_position = (
                current_size
            )

            if (
                self.trade_high is None
                or current_price
                > self.trade_high
            ):

                self.trade_high = (
                    current_price
                )

                self.save_state()

                logging.info(
                    "LONG PEAK UPDATED | "
                    f"HIGH={self.trade_high}"
                )

            if (
                self.current_sl is not None
                and current_price
                <= self.current_sl
            ):

                logging.warning(
                    "LONG SL LEVEL TOUCHED | "
                    f"PRICE={current_price} | "
                    f"SL={self.current_sl}"
                )

            return

        # ----------------------------------------------------
        # SHORT
        # ----------------------------------------------------

        if current_size < 0:

            self.last_position = (
                current_size
            )

            if (
                self.trade_low is None
                or current_price
                < self.trade_low
            ):

                self.trade_low = (
                    current_price
                )

                self.save_state()

                logging.info(
                    "SHORT TROUGH UPDATED | "
                    f"LOW={self.trade_low}"
                )

            if (
                self.current_sl is not None
                and current_price
                >= self.current_sl
            ):

                logging.warning(
                    "SHORT SL LEVEL TOUCHED | "
                    f"PRICE={current_price} | "
                    f"SL={self.current_sl}"
                )

            return

        # ----------------------------------------------------
        # FLAT
        # ----------------------------------------------------

        self.last_position = 0

        self.current_sl = None

        # ----------------------------------------------------
        # IMPORTANT:
        # Compare against OLD HIGH/LOW FIRST.
        # Then update the range.
        # ----------------------------------------------------

        old_high = (
            self.running_high
        )

        old_low = (
            self.running_low
        )

        # ----------------------------------------------------
        # NEW HIGH -> LONG
        # ----------------------------------------------------

        if (
            old_high is not None
            and current_price > old_high
        ):

            sl = old_low

            if sl is not None:

                logging.warning(
                    "NEW HIGH BREAKOUT | "
                    f"OLD_HIGH={old_high} | "
                    f"PRICE={current_price} | "
                    f"SL={sl}"
                )

                entered = self.execute_entry(
                    "LONG",
                    current_price,
                    sl,
                    "NEW HIGH BREAKOUT"
                )

                if entered:

                    self.trade_high = (
                        current_price
                    )

                    self.running_high = (
                        current_price
                    )

                    self.save_state()

                    return

        # ----------------------------------------------------
        # NEW LOW -> SHORT
        # ----------------------------------------------------

        if (
            old_low is not None
            and current_price < old_low
        ):

            sl = old_high

            if sl is not None:

                logging.warning(
                    "NEW LOW BREAKDOWN | "
                    f"OLD_LOW={old_low} | "
                    f"PRICE={current_price} | "
                    f"SL={sl}"
                )

                entered = self.execute_entry(
                    "SHORT",
                    current_price,
                    sl,
                    "NEW LOW BREAKDOWN"
                )

                if entered:

                    self.trade_low = (
                        current_price
                    )

                    self.running_low = (
                        current_price
                    )

                    self.save_state()

                    return

        # ----------------------------------------------------
        # NO ENTRY:
        # UPDATE EXTREMES
        # ----------------------------------------------------

        changed = False

        if (
            self.running_high is None
            or current_price
            > self.running_high
        ):

            self.running_high = (
                current_price
            )

            changed = True

        if (
            self.running_low is None
            or current_price
            < self.running_low
        ):

            self.running_low = (
                current_price
            )

            changed = True

        if changed:

            self.save_state()


    # ========================================================
    # WEBSOCKET
    # ========================================================

    def start_websocket(self):

        def on_message(
            ws,
            message
        ):

            try:

                data = json.loads(
                    message
                )

                msg_type = data.get(
                    "type"
                )

                if msg_type in (
                    "subscriptions",
                    "heartbeat",
                    "pong"
                ):

                    return

                # ------------------------------------------------
                # Delta public ticker format.
                # ------------------------------------------------

                if msg_type == "ticker":

                    ticker = data

                    price = (
                        ticker.get(
                            "close"
                        )
                        or ticker.get(
                            "last_price"
                        )
                        or ticker.get(
                            "mark_price"
                        )
                    )

                    if price is not None:

                        self.on_tick(
                            str(price)
                        )

                    return

                # ------------------------------------------------
                # Wrapped ticker compatibility.
                # ------------------------------------------------

                result = data.get(
                    "result"
                )

                if isinstance(
                    result,
                    dict
                ):

                    price = (
                        result.get(
                            "close"
                        )
                        or result.get(
                            "last_price"
                        )
                        or result.get(
                            "mark_price"
                        )
                    )

                    symbol = (
                        result.get(
                            "symbol"
                        )
                        or result.get(
                            "product_symbol"
                        )
                    )

                    if (
                        price is not None
                        and (
                            symbol is None
                            or symbol == SYMBOL
                        )
                    ):

                        self.on_tick(
                            str(price)
                        )

                    return

            except Exception as exc:

                logging.exception(
                    "WebSocket message error: "
                    f"{exc}"
                )


        def on_error(
            ws,
            error
        ):

            logging.error(
                f"WebSocket error: {error}"
            )


        def on_close(
            ws,
            close_status_code,
            close_msg
        ):

            logging.warning(
                "WebSocket disconnected | "
                f"CODE={close_status_code} | "
                f"MSG={close_msg}"
            )


        def on_open(
            ws
        ):

            logging.warning(
                "WebSocket connected."
            )

            subscribe_payload = {

                "type": "subscribe",

                "payload": {

                    "channels": [

                        {
                            "name": "ticker",
                            "symbols": [
                                SYMBOL
                            ]
                        }

                    ]

                }

            }

            ws.send(
                json.dumps(
                    subscribe_payload
                )
            )

            logging.warning(
                "SUBSCRIBED TO TICKER | "
                f"{SYMBOL}"
            )


        def websocket_loop():

            while True:

                try:

                    logging.warning(
                        "Connecting WebSocket | "
                        f"{WS_URL}"
                    )

                    ws_app = websocket.WebSocketApp(
                        WS_URL,
                        on_open=on_open,
                        on_message=on_message,
                        on_error=on_error,
                        on_close=on_close
                    )

                    ws_app.run_forever(
                        ping_interval=30,
                        ping_timeout=10
                    )

                except Exception as exc:

                    logging.exception(
                        f"WebSocket crashed: {exc}"
                    )

                logging.warning(
                    "WebSocket reconnecting "
                    "in 3 seconds..."
                )

                time.sleep(3)


        # ========================================================
        # STARTUP
        # ========================================================

        logging.warning(
            "============================================"
        )

        logging.warning(
            "XAUTUSD NEW EXTREME BREAKOUT ENGINE v27.1"
        )

        logging.warning(
            f"SYMBOL       = {SYMBOL}"
        )

        logging.warning(
            f"LEVERAGE     = {LEVERAGE}x"
        )

        logging.warning(
            f"BALANCE USE  = "
            f"{BALANCE_FRACTION * 100}%"
        )

        logging.warning(
            f"REST API     = {BASE_URL}"
        )

        logging.warning(
            f"WEBSOCKET    = {WS_URL}"
        )

        logging.warning(
            "STRATEGY     = "
            "05:45 NEW HIGH / NEW LOW"
        )

        logging.warning(
            "LATE START   = "
            "HISTORICAL HIGH/LOW ENABLED"
        )

        logging.warning(
            "============================================"
        )

        set_leverage(
            self.product_id
        )

        # --------------------------------------------------------
        # Startup position sync.
        # --------------------------------------------------------

        try:

            position = get_position(
                self.product_id
            )

            self.last_position = (
                position["size"]
            )

            if position["size"] != 0:

                logging.warning(
                    "STARTUP POSITION DETECTED | "
                    f"SIZE={position['size']} | "
                    f"ENTRY={position['entry_price']}"
                )

            else:

                logging.info(
                    "STARTUP POSITION = FLAT"
                )

        except Exception as exc:

            logging.error(
                "Startup position check failed: "
                f"{exc}"
            )

        # --------------------------------------------------------
        # WebSocket thread.
        # --------------------------------------------------------

        ws_thread = threading.Thread(
            target=websocket_loop,
            daemon=True
        )

        ws_thread.start()

        # --------------------------------------------------------
        # Watchdog.
        # --------------------------------------------------------

        last_position_check = 0

        while True:

            try:

                now = time.time()

                if (
                    now
                    - last_position_check
                    >= POSITION_CHECK_SECONDS
                ):

                    position = get_position(
                        self.product_id
                    )

                    exchange_size = (
                        position["size"]
                    )

                    if (
                        exchange_size == 0
                        and self.last_position != 0
                    ):

                        logging.warning(
                            "WATCHDOG: Exchange is FLAT "
                            "while local position exists."
                        )

                    elif (
                        exchange_size != 0
                        and self.last_position == 0
                    ):

                        logging.warning(
                            "WATCHDOG: Exchange position "
                            "detected."
                        )

                        self.last_position = (
                            exchange_size
                        )

                    last_position_check = now

                time.sleep(
                    POLL_SECONDS
                )

            except KeyboardInterrupt:

                logging.warning(
                    "BOT STOPPED BY USER."
                )

                break

            except Exception as exc:

                logging.exception(
                    "MAIN WATCHDOG ERROR: "
                    f"{exc}"
                )

                time.sleep(3)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:

        product_info = get_product()

        TradingStrategy(
            product_info
        ).start_websocket()

    except KeyboardInterrupt:

        logging.warning(
            "BOT STOPPED BY USER."
        )

    except Exception as exc:

        logging.exception(
            f"FATAL STARTUP ERROR: {exc}"
        )

        raise
