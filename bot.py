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
# VERSION 27.0
#
# STRATEGY
# ------------------------------------------------------------
# 05:45 IST:
#   - New trading day starts.
#   - Any old position is closed.
#   - Old HIGH/LOW are discarded.
#
# AFTER 05:45:
#   - First tick establishes the initial HIGH and LOW.
#
# FLAT:
#   - New HIGH -> LONG
#   - New LOW  -> SHORT
#
# LONG:
#   - SL = LOW that existed when LONG was entered.
#   - Track highest peak made during the LONG.
#   - If SL hits -> immediately SHORT.
#   - SHORT SL = highest peak made during that LONG.
#
# SHORT:
#   - SL = HIGH that existed when SHORT was entered.
#   - Track lowest low made during the SHORT.
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

# Current public market-data WebSocket endpoint.
WS_URL = os.getenv(
    "DELTA_PUBLIC_WS_URL",
    os.getenv(
        "DELTA_WS_URL",
        "wss://public-socket.india.delta.exchange"
    )
)

SYMBOL = os.getenv("DELTA_SYMBOL", "XAUTUSD")

API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

LEVERAGE = Decimal(os.getenv("LEVERAGE", "50"))
BALANCE_FRACTION = Decimal(os.getenv("BALANCE_FRACTION", "0.10"))

STATE_FILE = os.getenv(
    "STATE_FILE",
    "xautusd_state.json"
)

POLL_SECONDS = float(
    os.getenv("POLL_SECONDS", "0.25")
)

POSITION_CHECK_SECONDS = float(
    os.getenv("POSITION_CHECK_SECONDS", "1.0")
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
    "User-Agent": "XAUTUSD-NewExtreme-Engine/27.0"
})


# ============================================================
# TIME HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def trading_day_start(dt=None):
    """
    Trading day boundary = 05:30 IST.

    IMPORTANT:
    Strategy activation is 05:45 IST.
    05:30 is only used to identify the trading date.
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
    Actual strategy activation time.
    """
    return day_start + timedelta(minutes=15)


def is_strategy_active(dt=None):
    dt = dt or now_ist()

    if is_weekend_blocked(dt):
        return False

    day_start = trading_day_start(dt)
    return dt >= strategy_start_time(day_start)


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
    timestamp = str(int(time.time()))

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
        "?" + urlencode(params, doseq=True)
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
            data=body_text if body is not None else None,
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

def get_position(product_id):
    result = api_call(
        "GET",
        "/v2/positions",
        params={
            "product_id": int(product_id)
        },
        auth=True
    )["result"]

    if not result or not isinstance(result, dict):
        return {
            "size": 0,
            "entry_price": None
        }

    return {
        "size": int(result.get("size", 0)),
        "entry_price": result.get("entry_price")
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

    for wallet in data.get("result", []):
        asset = str(
            wallet.get("asset_symbol", "")
        ).upper()

        if asset in ("USD", "USDT"):
            value = (
                wallet.get("balance")
                or wallet.get("available_balance")
            )

            if value is not None:
                return Decimal(str(value))

    net_equity = (
        data.get("meta", {})
        .get("net_equity")
    )

    if net_equity is not None:
        return Decimal(str(net_equity))

    raise RuntimeError(
        "Could not retrieve wallet balance."
    )


# ============================================================
# LEVERAGE
# ============================================================

def set_leverage(product_id):
    try:
        api_call(
            "POST",
            f"/v2/products/{product_id}/orders/leverage",
            body={
                "leverage": str(LEVERAGE)
            },
            auth=True
        )

        logging.info(
            f"LEVERAGE SET = {LEVERAGE}x"
        )

    except Exception as exc:
        logging.warning(
            f"Leverage setting failed: {exc}"
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
            product.get("contract_value")
            or product.get("contract_value_usd")
            or "1"
        )
    )

    if contract_value <= 0:
        contract_value = Decimal("1")

    raw_size = (
        notional
        / (price * contract_value)
    )

    lot_size = Decimal(
        str(
            product.get("lot_size")
            or product.get("order_size_increment")
            or "1"
        )
    )

    if lot_size <= 0:
        lot_size = Decimal("1")

    min_size = Decimal(
        str(
            product.get("min_order_size")
            or product.get("minimum_order_size")
            or lot_size
        )
    )

    size_decimal = (
        raw_size / lot_size
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * lot_size

    if size_decimal < min_size:
        size_decimal = min_size

    size = int(size_decimal)

    if size <= 0:
        raise RuntimeError(
            "Calculated order size is zero."
        )

    logging.info(
        f"ORDER SIZE | BALANCE={balance} "
        f"| MARGIN={margin} "
        f"| NOTIONAL={notional} "
        f"| SIZE={size}"
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
            "Cannot place entry: SL price is None."
        )

    body = {
        "product_id": int(product_id),
        "product_symbol": SYMBOL,
        "size": int(abs(size)),
        "side": side,
        "order_type": "market_order",

        # Exchange-side protective stop.
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

    order = result.get("result", {})

    order_id = order.get("id")

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
        "product_id": int(product_id),
        "product_symbol": SYMBOL,
        "size": int(abs(size)),
        "side": side,
        "order_type": "market_order",

        # Closing only.
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

    def __init__(self, product):

        self.product = product

        self.product_id = int(
            product["id"]
        )

        # Current trading day.
        self.day_start = None

        # ----------------------------------------------------
        # GLOBAL EXTREMES AFTER 05:45
        #
        # These are the levels used while FLAT.
        # ----------------------------------------------------

        self.running_high = None
        self.running_low = None

        # ----------------------------------------------------
        # CURRENT POSITION
        # +size = LONG
        # -size = SHORT
        #  0    = FLAT
        # ----------------------------------------------------

        self.last_position = 0

        # ----------------------------------------------------
        # CURRENT EXCHANGE STOP
        # ----------------------------------------------------

        self.current_sl = None

        # ----------------------------------------------------
        # PEAK/TRough OF CURRENT TRADE
        #
        # LONG:
        #   trade_high tracks highest high made
        #
        # SHORT:
        #   trade_low tracks lowest low made
        # ----------------------------------------------------

        self.trade_high = None
        self.trade_low = None

        # ----------------------------------------------------
        # LAST PRICE
        # ----------------------------------------------------

        self.last_price = None

        # ----------------------------------------------------
        # FIRST TICK AFTER 05:45
        # ----------------------------------------------------

        self.baseline_ready = False

        # ----------------------------------------------------
        # PREVENT DOUBLE ORDERS
        # ----------------------------------------------------

        self.order_in_flight = False

        # ----------------------------------------------------
        # DAY RESET LOCK
        # ----------------------------------------------------

        self.day_reset_done = False

        self.load_state()


    # ========================================================
    # LOAD STATE
    # ========================================================

    def load_state(self):

        if not os.path.exists(STATE_FILE):
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

                state = json.load(file)

            self.day_start = (
                datetime.fromisoformat(
                    state["day_start"]
                )
                if state.get("day_start")
                else None
            )

            self.running_high = (
                Decimal(
                    state["running_high"]
                )
                if state.get("running_high")
                else None
            )

            self.running_low = (
                Decimal(
                    state["running_low"]
                )
                if state.get("running_low")
                else None
            )

            self.current_sl = (
                Decimal(
                    state["current_sl"]
                )
                if state.get("current_sl")
                else None
            )

            self.trade_high = (
                Decimal(
                    state["trade_high"]
                )
                if state.get("trade_high")
                else None
            )

            self.trade_low = (
                Decimal(
                    state["trade_low"]
                )
                if state.get("trade_low")
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
                str(self.running_high)
                if self.running_high is not None
                else None,

            "running_low":
                str(self.running_low)
                if self.running_low is not None
                else None,

            "current_sl":
                str(self.current_sl)
                if self.current_sl is not None
                else None,

            "trade_high":
                str(self.trade_high)
                if self.trade_high is not None
                else None,

            "trade_low":
                str(self.trade_low)
                if self.trade_low is not None
                else None,

            "baseline_ready":
                self.baseline_ready,

            "day_reset_done":
                self.day_reset_done
        }

        temp_file = (
            STATE_FILE + ".tmp"
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

        new_day = trading_day_start(now)

        if self.day_start == new_day:
            return

        logging.warning(
            "NEW TRADING DAY | "
            f"{new_day}"
        )

        self.day_start = new_day

        # Old day's market levels are discarded.
        self.running_high = None
        self.running_low = None

        self.current_sl = None

        self.trade_high = None
        self.trade_low = None

        self.baseline_ready = False

        self.day_reset_done = False

        # Important:
        # If an old position is still open, it must be
        # closed at 05:45, not carried into the new day.
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

        if is_weekend_blocked(now):
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

        # Get actual exchange position.
        position = get_position(
            self.product_id
        )

        current_size = position["size"]

        # ----------------------------------------------------
        # IMPORTANT:
        # Any previous day's position is CLOSED.
        # ----------------------------------------------------

        if current_size != 0:

            logging.warning(
                "05:45 RESET | "
                f"CLOSING OLD POSITION SIZE={current_size}"
            )

            close_position_market(
                self.product_id,
                current_size
            )

            # Wait until exchange confirms flat.
            for _ in range(30):

                time.sleep(0.20)

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
        # Fresh day.
        # ----------------------------------------------------

        self.last_position = 0

        self.current_sl = None

        self.running_high = None
        self.running_low = None

        self.trade_high = None
        self.trade_low = None

        self.baseline_ready = False

        self.day_reset_done = True

        self.save_state()

        logging.warning(
            "===== 05:45 RESET COMPLETE ====="
        )


    # ========================================================
    # INITIAL BASELINE
    # ========================================================

    def establish_baseline(
        self,
        price
    ):

        self.running_high = price
        self.running_low = price

        self.baseline_ready = True

        self.save_state()

        logging.warning(
            "05:45 BASELINE CREATED | "
            f"HIGH={price} | LOW={price}"
        )


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
        # LONG requires SL below current price.
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
        # SHORT requires SL above current price.
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
        # ALWAYS RECHECK REAL EXCHANGE POSITION.
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

            # Wait for fill.
            filled_size = 0

            for _ in range(40):

                time.sleep(0.20)

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

            self.last_position = filled_size

            self.current_sl = (
                Decimal(str(sl_price))
            )

            # ------------------------------------------------
            # Start tracking peak/trough for THIS trade.
            # ------------------------------------------------

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
    # HANDLE LONG STOP
    # ========================================================

    def reverse_long_to_short(
        self,
        price
    ):

        # Peak HIGH made while LONG.
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

        # We assume exchange-side SL has already closed
        # the old position. Re-check before reversing.
        position = get_position(
            self.product_id
        )

        if position["size"] != 0:

            logging.warning(
                "Waiting for LONG position "
                "to become flat before reversal."
            )

            for _ in range(30):

                time.sleep(0.20)

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

        # Start short.
        return self.execute_entry(
            "SHORT",
            price,
            peak_high,
            "LONG SL HIT -> REVERSE SHORT"
        )


    # ========================================================
    # HANDLE SHORT STOP
    # ========================================================

    def reverse_short_to_long(
        self,
        price
    ):

        # Lowest LOW made while SHORT.
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

                time.sleep(0.20)

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
    # POSITION DISAPPEARED
    # ========================================================

    def handle_position_closed(
        self,
        old_size,
        current_price
    ):

        old_sl = self.current_sl

        # ----------------------------------------------------
        # Determine whether this looks like our SL.
        # ----------------------------------------------------

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

            # External/manual closure.
            #
            # DO NOT automatically reverse.
            # Strategy resumes flat and waits for
            # the next new high/low.
            logging.warning(
                "POSITION CLOSED EXTERNALLY/MANUALLY | "
                "NO AUTOMATIC REVERSAL."
            )

            self.trade_high = None
            self.trade_low = None

            self.save_state()

            return

        # ----------------------------------------------------
        # SL -> immediate reversal.
        # ----------------------------------------------------

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
        # Saturday square-off.
        # ----------------------------------------------------

        if is_saturday_squareoff_time(now):

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
        # Weekend block.
        # ----------------------------------------------------

        if is_weekend_blocked(now):
            return

        # ----------------------------------------------------
        # Make sure day state is correct.
        # ----------------------------------------------------

        current_exchange_position = get_position(
            self.product_id
        )

        current_exchange_size = (
            current_exchange_position["size"]
        )

        self.handle_new_day(
            now,
            current_exchange_size
        )

        # ----------------------------------------------------
        # Before 05:45:
        #
        # No new strategy entries.
        # ----------------------------------------------------

        if now < strategy_start_time(
            self.day_start
        ):

            # Keep local knowledge of an existing
            # position but don't trade.
            self.last_position = (
                current_exchange_size
            )

            return

        # ----------------------------------------------------
        # 05:45 reset.
        # ----------------------------------------------------

        self.perform_0545_reset(now)

        if not self.day_reset_done:
            return

        # ----------------------------------------------------
        # FIRST TICK AFTER 05:45:
        #
        # This establishes the first HIGH and LOW.
        #
        # It does NOT immediately trade.
        # ----------------------------------------------------

        if not self.baseline_ready:

            # Make sure exchange is flat after reset.
            position = get_position(
                self.product_id
            )

            if position["size"] != 0:

                logging.warning(
                    "Waiting for 05:45 reset "
                    "to become completely flat."
                )

                return

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

        current_size = position["size"]

        # ----------------------------------------------------
        # POSITION WAS CLOSED SINCE LAST CHECK
        # ----------------------------------------------------

        if (
            current_size == 0
            and self.last_position != 0
        ):

            old_size = self.last_position

            self.handle_position_closed(
                old_size,
                current_price
            )

            return

        # ----------------------------------------------------
        # LONG POSITION MANAGEMENT
        # ----------------------------------------------------

        if current_size > 0:

            self.last_position = current_size

            # Track highest peak made during LONG.
            if (
                self.trade_high is None
                or current_price > self.trade_high
            ):

                self.trade_high = (
                    current_price
                )

                self.save_state()

                logging.info(
                    f"LONG PEAK UPDATED | "
                    f"HIGH={self.trade_high}"
                )

            # IMPORTANT:
            # Exchange-side SL is the actual protection.
            #
            # This local check is only for fast reversal
            # detection after the exchange has flattened.
            if (
                self.current_sl is not None
                and current_price <= self.current_sl
            ):

                logging.warning(
                    "LONG SL LEVEL TOUCHED | "
                    f"PRICE={current_price} | "
                    f"SL={self.current_sl}"
                )

            return

        # ----------------------------------------------------
        # SHORT POSITION MANAGEMENT
        # ----------------------------------------------------

        if current_size < 0:

            self.last_position = current_size

            # Track lowest trough made during SHORT.
            if (
                self.trade_low is None
                or current_price < self.trade_low
            ):

                self.trade_low = (
                    current_price
                )

                self.save_state()

                logging.info(
                    f"SHORT TROUGH UPDATED | "
                    f"LOW={self.trade_low}"
                )

            if (
                self.current_sl is not None
                and current_price >= self.current_sl
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
        # New HIGH / LOW breakout logic.
        #
        # IMPORTANT:
        # We compare against the OLD level FIRST.
        # Only AFTER checking breakout do we update
        # the running high/low.
        # ----------------------------------------------------

        old_high = self.running_high
        old_low = self.running_low

        # ----------------------------------------------------
        # NEW HIGH -> LONG
        # ----------------------------------------------------

        if (
            old_high is not None
            and current_price > old_high
        ):

            # SL = LOW that existed before the breakout.
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

                    # The new high becomes the first
                    # peak of this LONG.
                    self.trade_high = (
                        current_price
                    )

                    # Update global high after entry.
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

            # SL = HIGH that existed before breakdown.
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

                    # The new low becomes first trough.
                    self.trade_low = (
                        current_price
                    )

                    self.running_low = (
                        current_price
                    )

                    self.save_state()

                    return

        # ----------------------------------------------------
        # NO ENTRY.
        #
        # Now update running extremes.
        #
        # This ordering is critical.
        # ----------------------------------------------------

        changed = False

        if (
            self.running_high is None
            or current_price > self.running_high
        ):

            self.running_high = (
                current_price
            )

            changed = True

        if (
            self.running_low is None
            or current_price < self.running_low
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

                data = json.loads(message)

                msg_type = data.get(
                    "type"
                )

                # Ignore subscription/heartbeat messages.
                if msg_type in (
                    "subscriptions",
                    "heartbeat",
                    "pong"
                ):
                    return

                # ------------------------------------------------
                # Current Delta public ticker format.
                #
                # We accept several possible price fields
                # so the bot remains tolerant of ticker format.
                # ------------------------------------------------

                if msg_type == "ticker":

                    ticker = data

                    price = (
                        ticker.get("close")
                        or ticker.get("last_price")
                        or ticker.get("mark_price")
                    )

                    if price is not None:

                        self.on_tick(
                            str(price)
                        )

                    return

                # Some feeds may wrap the ticker payload.
                result = data.get("result")

                if isinstance(
                    result,
                    dict
                ):

                    price = (
                        result.get("close")
                        or result.get("last_price")
                        or result.get("mark_price")
                    )

                    symbol = (
                        result.get("symbol")
                        or result.get("product_symbol")
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
                    f"WebSocket message error: {exc}"
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


        def on_open(ws):

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
                f"SUBSCRIBED TO TICKER | "
                f"{SYMBOL}"
            )


        def websocket_loop():

            while True:

                try:

                    logging.warning(
                        f"Connecting WebSocket | "
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
                    "WebSocket reconnecting in 3 seconds..."
                )

                time.sleep(3)


        # --------------------------------------------------------
        # Startup checks
        # --------------------------------------------------------

        logging.warning(
            "============================================"
        )

        logging.warning(
            "XAUTUSD NEW EXTREME BREAKOUT ENGINE v27.0"
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
            f"REST API     = {BASE_URL}"
        )

        logging.warning(
            f"WEBSOCKET    = {WS_URL}"
        )

        logging.warning(
            "STRATEGY     = 05:45 NEW HIGH / NEW LOW"
        )

        logging.warning(
            "============================================"
        )

        set_leverage(
            self.product_id
        )

        # --------------------------------------------------------
        # Synchronize current exchange position on startup.
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
                f"Startup position check failed: {exc}"
            )


        # --------------------------------------------------------
        # Start WebSocket.
        # --------------------------------------------------------

        ws_thread = threading.Thread(
            target=websocket_loop,
            daemon=True
        )

        ws_thread.start()


        # --------------------------------------------------------
        # Main watchdog.
        #
        # The actual price engine runs in WebSocket thread.
        # This thread keeps the process alive and periodically
        # checks the exchange position so a missed WebSocket
        # event does not leave local state stale forever.
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

                    # ------------------------------------------------
                    # If exchange position changed to flat, the
                    # next price tick will handle the reversal.
                    # ------------------------------------------------

                    if (
                        exchange_size == 0
                        and self.last_position != 0
                    ):

                        logging.warning(
                            "WATCHDOG: Exchange is FLAT "
                            "while local position exists."
                        )

                    # If exchange has a position and local state
                    # somehow lost it, synchronize size.
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
                    f"MAIN WATCHDOG ERROR: {exc}"
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
