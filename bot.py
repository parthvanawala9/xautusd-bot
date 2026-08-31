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
# VERSION 28.0
#
# IMPORTANT STRATEGY RULE
# ------------------------------------------------------------
# DAILY STRATEGY START = 05:45 IST
#
# 05:45 is NOT a candle range.
# 05:45 is simply the beginning of the strategy day.
#
# AFTER 05:45:
#
#   FLAT:
#       New HIGH -> LONG
#       New LOW  -> SHORT
#
#   LONG:
#       SL = LOW existing when LONG was entered.
#       Track highest peak made during LONG.
#       If SL hits -> SHORT.
#       SHORT SL = highest peak made during LONG.
#
#   SHORT:
#       SL = HIGH existing when SHORT was entered.
#       Track lowest trough made during SHORT.
#       If SL hits -> LONG.
#       LONG SL = lowest trough made during SHORT.
#
# ONE POSITION ONLY.
#
# ------------------------------------------------------------
# LATE START / RESTART BEHAVIOUR
# ------------------------------------------------------------
#
# If bot starts at:
#
#   05:45
#   09:45
#   12:00
#   16:00
#   20:00
#   or any other time
#
# it NEVER creates a new baseline at that startup time.
#
# Instead it reconstructs:
#
#       05:45 -> current time
#
# and finds the actual HIGH and LOW of the trading day.
#
# Example:
#
#   05:45 HIGH = 4410
#   06:30 HIGH = 4420
#   08:00 LOW  = 4390
#   Bot restarts at 16:00
#
# It reconstructs:
#
#   DAY HIGH = 4420
#   DAY LOW  = 4390
#
# Then it waits for the NEXT actual breakout.
#
# If current price at startup is already above the reconstructed
# high, the current price is treated as the breakout and LONG
# is taken.
#
# If current price at startup is already below the reconstructed
# low, SHORT is taken.
#
# ------------------------------------------------------------
# DAILY RESET
# ------------------------------------------------------------
#
# A new trading day is identified at 05:30 IST.
# Strategy becomes active at 05:45 IST.
#
# Old trading-day levels are discarded.
#
# An old position from a previous day is closed at 05:45.
#
# IMPORTANT:
# Restarting the bot later on the SAME day does NOT perform
# another 05:45 reset.
#
# ------------------------------------------------------------
# WEEKEND
# ------------------------------------------------------------
#
# Saturday 05:00 IST:
#       Square off.
#
# Saturday after 05:00:
#       No trading.
#
# Sunday:
#       No trading.
#
# Monday before 05:30:
#       No trading.
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
    "User-Agent": "XAUTUSD-NewExtreme-Engine/28.0"
})


# ============================================================
# TIME HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def trading_day_start(dt=None):
    """
    The trading DATE changes at 05:30 IST.

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
    Actual strategy start = 05:45 IST.
    """

    return day_start + timedelta(
        minutes=15
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


def is_strategy_active(dt=None):
    dt = dt or now_ist()

    if is_weekend_blocked(dt):
        return False

    day_start = trading_day_start(dt)

    return dt >= strategy_start_time(
        day_start
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

    if (
        not result
        or not isinstance(result, dict)
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
        data
        .get("meta", {})
        .get("net_equity")
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

def get_candles(
    start_dt,
    end_dt
):
    """
    Get 15-minute candles between 05:45 and now.

    Used primarily for late startup/restart recovery.
    """

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

    if end_ts <= start_ts:
        return []

    data = api_call(
        "GET",
        "/v2/history/candles",
        params={
            "resolution": "15m",
            "symbol": SYMBOL,
            "start": start_ts,
            "end": end_ts
        }
    )

    result = data.get(
        "result",
        []
    )

    if not isinstance(
        result,
        list
    ):
        return []

    return result


def reconstruct_day_extremes(
    day_start,
    current_time,
    current_price
):
    """
    Reconstruct the HIGH and LOW from:

        05:45 IST -> current time

    IMPORTANT:
    The current incomplete candle is NOT blindly treated as
    the complete day's range.

    The current live price is added separately.

    This means a late-start bot can recover the day's actual
    known range and then check whether current price is breaking
    that range.
    """

    strategy_start = strategy_start_time(
        day_start
    )

    if current_time <= strategy_start:
        return (
            current_price,
            current_price
        )

    try:

        candles = get_candles(
            strategy_start,
            current_time
        )

    except Exception as exc:

        logging.error(
            "HISTORY RECOVERY FAILED | "
            f"{exc}"
        )

        return (
            None,
            None
        )

    highest = None
    lowest = None

    for candle in candles:

        try:

            candle_time = int(
                candle.get(
                    "time"
                )
            )

        except Exception:
            continue

        candle_dt = datetime.fromtimestamp(
            candle_time,
            UTC
        ).astimezone(IST)

        # Only candles beginning at or after 05:45.
        if candle_dt < strategy_start:
            continue

        try:

            candle_high = Decimal(
                str(
                    candle["high"]
                )
            )

            candle_low = Decimal(
                str(
                    candle["low"]
                )
            )

        except (
            KeyError,
            ValueError,
            InvalidOperation,
            TypeError
        ):

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

    # Add current live price.
    if highest is None:
        highest = current_price
    else:
        highest = max(
            highest,
            current_price
        )

    if lowest is None:
        lowest = current_price
    else:
        lowest = min(
            lowest,
            current_price
        )

    return (
        highest,
        lowest
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
                "leverage":
                    str(LEVERAGE)
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
        raw_size / lot_size
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
        "ORDER SIZE | "
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
            str(sl_price),

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

    logging.warning(
        "ENTRY ORDER ACCEPTED | "
        f"ORDER_ID={order.get('id')}"
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

        "reduce_only":
            True,

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
# TRADING STRATEGY
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
        # DAY HIGH / LOW
        #
        # These are the actual extremes after 05:45.
        # ----------------------------------------------------

        self.running_high = None
        self.running_low = None

        # ----------------------------------------------------
        # CURRENT POSITION
        #
        # +size = LONG
        # -size = SHORT
        #  0 = FLAT
        # ----------------------------------------------------

        self.last_position = 0

        # ----------------------------------------------------
        # CURRENT SL
        # ----------------------------------------------------

        self.current_sl = None

        # ----------------------------------------------------
        # CURRENT TRADE EXTREMES
        #
        # LONG  -> trade_high
        # SHORT -> trade_low
        # ----------------------------------------------------

        self.trade_high = None
        self.trade_low = None

        # ----------------------------------------------------
        # LAST PRICE
        # ----------------------------------------------------

        self.last_price = None

        # ----------------------------------------------------
        # HAS THE DAY BEEN INITIALIZED?
        # ----------------------------------------------------

        self.day_reset_done = False

        # ----------------------------------------------------
        # 05:45 -> history recovery completed
        # ----------------------------------------------------

        self.baseline_ready = False

        # ----------------------------------------------------
        # Prevent duplicate entries.
        # ----------------------------------------------------

        self.order_in_flight = False

        # ----------------------------------------------------
        # Startup recovery flag.
        # ----------------------------------------------------

        self.startup_recovery_done = False

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
                f"SL={self.current_sl} | "
                f"BASELINE={self.baseline_ready} | "
                f"RESET={self.day_reset_done}"
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
    # NEW DAY
    # ========================================================

    def handle_new_day(
        self,
        now,
        current_position
    ):

        new_day = trading_day_start(
            now
        )

        # ----------------------------------------------------
        # SAME DAY:
        #
        # DO NOT reset anything.
        #
        # This is extremely important for a late restart.
        # ----------------------------------------------------

        if self.day_start == new_day:

            return False


        # ----------------------------------------------------
        # NEW DAY.
        # ----------------------------------------------------

        logging.warning(
            "============================================"
        )

        logging.warning(
            "NEW TRADING DAY | "
            f"{new_day}"
        )

        logging.warning(
            "OLD DAY LEVELS WILL BE DISCARDED."
        )

        logging.warning(
            "============================================"
        )

        self.day_start = new_day

        self.running_high = None
        self.running_low = None

        self.current_sl = None

        self.trade_high = None
        self.trade_low = None

        self.baseline_ready = False

        self.day_reset_done = False

        self.startup_recovery_done = False

        # Do NOT change last_position here.
        # Exchange will be checked during reset.

        if current_position != 0:

            logging.warning(
                "POSITION EXISTS FROM PREVIOUS STATE | "
                f"SIZE={current_position} | "
                "WILL BE CLOSED AT 05:45."
            )

        self.save_state()

        return True


    # ========================================================
    # 05:45 DAILY RESET
    # ========================================================

    def perform_0545_reset(
        self,
        now
    ):

        if self.day_start is None:
            return False

        if now < strategy_start_time(
            self.day_start
        ):
            return False

        # ----------------------------------------------------
        # VERY IMPORTANT:
        #
        # If bot was restarted at 09:45 / 16:00 and state
        # already says today's reset was completed, DO NOT
        # close today's running position.
        # ----------------------------------------------------

        if self.day_reset_done:

            return True


        logging.warning(
            "============================================"
        )

        logging.warning(
            "05:45 IST DAILY RESET"
        )

        logging.warning(
            "OLD POSITION WILL BE CLOSED IF PRESENT."
        )

        logging.warning(
            "============================================"
        )


        position = get_position(
            self.product_id
        )

        current_size = position[
            "size"
        ]


        # ----------------------------------------------------
        # Close previous-day position.
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
                    "05:45 RESET FAILED | "
                    "OLD POSITION STILL OPEN."
                )

                return False


        # ----------------------------------------------------
        # Fresh strategy day.
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
            "05:45 RESET COMPLETE."
        )

        return True


    # ========================================================
    # RECOVER TODAY'S HIGH / LOW
    # ========================================================

    def recover_today_levels(
        self,
        now,
        current_price
    ):

        if self.day_start is None:
            return False

        strategy_start = strategy_start_time(
            self.day_start
        )

        if now < strategy_start:
            return False


        logging.warning(
            "============================================"
        )

        logging.warning(
            "RECOVERING TODAY'S MARKET RANGE"
        )

        logging.warning(
            f"FROM = {strategy_start}"
        )

        logging.warning(
            f"TO   = {now}"
        )

        logging.warning(
            "============================================"
        )


        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Always reconstruct from 05:45 on a late startup.
        #
        # Do NOT use startup time as baseline.
        # ----------------------------------------------------

        high, low = reconstruct_day_extremes(
            self.day_start,
            now,
            current_price
        )


        if high is None or low is None:

            logging.error(
                "TODAY'S HIGH/LOW COULD NOT BE RECOVERED."
            )

            return False


        self.running_high = high
        self.running_low = low

        self.baseline_ready = True

        self.startup_recovery_done = True

        self.save_state()


        logging.warning(
            "============================================"
        )

        logging.warning(
            "TODAY'S RANGE RECOVERED"
        )

        logging.warning(
            f"DAY HIGH = {self.running_high}"
        )

        logging.warning(
            f"DAY LOW  = {self.running_low}"
        )

        logging.warning(
            f"CURRENT  = {current_price}"
        )

        logging.warning(
            "============================================"
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
                "ENTRY BLOCKED | "
                "SL is None."
            )

            return False


        # ----------------------------------------------------
        # LONG SL must be below price.
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
        # SHORT SL must be above price.
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
                f"Unknown direction: "
                f"{direction}"
            )

            return False


        # ----------------------------------------------------
        # ALWAYS check actual exchange position.
        # ----------------------------------------------------

        existing = get_position(
            self.product_id
        )

        if existing["size"] != 0:

            logging.warning(
                "ENTRY BLOCKED | "
                "EXCHANGE POSITION ALREADY OPEN | "
                f"SIZE={existing['size']}"
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

                position_size = (
                    position["size"]
                )


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

            self.current_sl = (
                Decimal(
                    str(sl_price)
                )
            )


            # ------------------------------------------------
            # New trade peak/trough.
            # ------------------------------------------------

            if direction == "LONG":

                self.trade_high = price
                self.trade_low = None

            else:

                self.trade_low = price
                self.trade_high = None


            self.save_state()


            logging.warning(
                "============================================"
            )

            logging.warning(
                f"ENTRY CONFIRMED | "
                f"{direction}"
            )

            logging.warning(
                f"SIZE={filled_size}"
            )

            logging.warning(
                f"ENTRY≈{price}"
            )

            logging.warning(
                f"SL={sl_price}"
            )

            logging.warning(
                f"REASON={reason}"
            )

            logging.warning(
                "============================================"
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
                "LONG -> SHORT blocked | "
                "No peak high available."
            )

            return False


        logging.warning(
            "LONG STOP HIT -> "
            "REVERSING SHORT | "
            f"SHORT SL={peak_high}"
        )


        position = get_position(
            self.product_id
        )


        if position["size"] != 0:

            logging.warning(
                "Waiting for LONG position "
                "to become flat."
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
                "LONG -> SHORT blocked | "
                "Old position still open."
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
                "SHORT -> LONG blocked | "
                "No trough low available."
            )

            return False


        logging.warning(
            "SHORT STOP HIT -> "
            "REVERSING LONG | "
            f"LONG SL={trough_low}"
        )


        position = get_position(
            self.product_id
        )


        if position["size"] != 0:

            logging.warning(
                "Waiting for SHORT position "
                "to become flat."
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
                "SHORT -> LONG blocked | "
                "Old position still open."
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


        # ----------------------------------------------------
        # Manual / external closure.
        # ----------------------------------------------------

        if not sl_triggered:

            logging.warning(
                "POSITION CLOSED "
                "MANUALLY/EXTERNALLY."
            )

            logging.warning(
                "NO AUTOMATIC REVERSAL."
            )

            self.trade_high = None
            self.trade_low = None

            self.save_state()

            return


        # ----------------------------------------------------
        # SL reversal.
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
    # LATE START / RESTART ENTRY CHECK
    # ========================================================

    def check_startup_breakout(
        self,
        current_price
    ):
        """
        Called after historical recovery.

        If bot starts late and current price is already outside
        the reconstructed 05:45->now range, enter immediately.

        Otherwise wait for the NEXT new high/low.
        """

        if not self.baseline_ready:
            return False

        if self.running_high is None:
            return False

        if self.running_low is None:
            return False


        # ----------------------------------------------------
        # Current price above recovered HIGH.
        # ----------------------------------------------------

        if current_price > self.running_high:

            old_high = self.running_high
            old_low = self.running_low

            logging.warning(
                "LATE START HIGH BREAKOUT | "
                f"RECOVERED_HIGH={old_high} | "
                f"CURRENT={current_price}"
            )

            entered = self.execute_entry(
                "LONG",
                current_price,
                old_low,
                "LATE START -> HIGH BREAKOUT"
            )

            if entered:

                self.trade_high = current_price
                self.running_high = current_price

                self.save_state()

                return True


        # ----------------------------------------------------
        # Current price below recovered LOW.
        # ----------------------------------------------------

        if current_price < self.running_low:

            old_high = self.running_high
            old_low = self.running_low

            logging.warning(
                "LATE START LOW BREAKDOWN | "
                f"RECOVERED_LOW={old_low} | "
                f"CURRENT={current_price}"
            )

            entered = self.execute_entry(
                "SHORT",
                current_price,
                old_high,
                "LATE START -> LOW BREAKDOWN"
            )

            if entered:

                self.trade_low = current_price
                self.running_low = current_price

                self.save_state()

                return True


        return False


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
                f"INVALID PRICE | "
                f"{price_str}"
            )

            return


        now = now_ist()

        self.last_price = current_price


        # ====================================================
        # SATURDAY SQUARE-OFF
        # ====================================================

        if is_saturday_squareoff_time(
            now
        ):

            position = get_position(
                self.product_id
            )

            if position["size"] != 0:

                logging.warning(
                    "SATURDAY 05:00 "
                    "SQUARE-OFF | "
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


        # ====================================================
        # WEEKEND
        # ====================================================

        if is_weekend_blocked(
            now
        ):

            return


        # ====================================================
        # CURRENT EXCHANGE POSITION
        # ====================================================

        try:

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

        except Exception as exc:

            logging.error(
                "POSITION CHECK FAILED | "
                f"{exc}"
            )

            return


        # ====================================================
        # DAY TRANSITION
        # ====================================================

        self.handle_new_day(
            now,
            current_exchange_size
        )


        # ====================================================
        # BEFORE 05:45
        # ====================================================

        if now < strategy_start_time(
            self.day_start
        ):

            self.last_position = (
                current_exchange_size
            )

            return


        # ====================================================
        # 05:45 RESET
        # ====================================================

        if not self.perform_0545_reset(
            now
        ):

            return


        # ====================================================
        # LATE START / RESTART RECOVERY
        #
        # If state was already recovered today, keep it.
        #
        # If bot starts today with no baseline, reconstruct
        # 05:45 -> NOW.
        # ====================================================

        if not self.baseline_ready:

            # Make sure no position remains after a fresh-day
            # reset before establishing the market range.

            position = get_position(
                self.product_id
            )

            if position["size"] != 0:

                logging.warning(
                    "WAITING | "
                    "POSITION STILL OPEN "
                    "AFTER RESET."
                )

                return


            if not self.recover_today_levels(
                now,
                current_price
            ):

                return


            # ------------------------------------------------
            # IMPORTANT:
            #
            # We DO check the current price against the
            # reconstructed range.
            #
            # Therefore a late restart can still enter if
            # current price is outside the full 05:45->NOW
            # range.
            # ------------------------------------------------

            if self.check_startup_breakout(
                current_price
            ):

                return


            # No breakout at startup.
            return


        # ====================================================
        # REAL EXCHANGE POSITION
        # ====================================================

        position = get_position(
            self.product_id
        )

        current_size = position[
            "size"
        ]


        # ====================================================
        # POSITION CLOSED SINCE LAST TICK
        # ====================================================

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


        # ====================================================
        # LONG POSITION
        # ====================================================

        if current_size > 0:

            self.last_position = (
                current_size
            )


            # ------------------------------------------------
            # Track highest peak during LONG.
            # ------------------------------------------------

            if (
                self.trade_high is None
                or current_price > self.trade_high
            ):

                self.trade_high = (
                    current_price
                )

                self.save_state()

                logging.info(
                    "LONG PEAK UPDATED | "
                    f"HIGH={self.trade_high}"
                )


            # ------------------------------------------------
            # Informational SL level.
            #
            # Actual protection remains exchange-side.
            # ------------------------------------------------

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


        # ====================================================
        # SHORT POSITION
        # ====================================================

        if current_size < 0:

            self.last_position = (
                current_size
            )


            # ------------------------------------------------
            # Track lowest trough during SHORT.
            # ------------------------------------------------

            if (
                self.trade_low is None
                or current_price < self.trade_low
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
                and current_price >= self.current_sl
            ):

                logging.warning(
                    "SHORT SL LEVEL TOUCHED | "
                    f"PRICE={current_price} | "
                    f"SL={self.current_sl}"
                )


            return


        # ====================================================
        # FLAT
        # ====================================================

        self.last_position = 0

        self.current_sl = None


        if (
            self.running_high is None
            or self.running_low is None
        ):

            return


        # ====================================================
        # IMPORTANT BREAKOUT ORDER
        #
        # FIRST compare against OLD levels.
        # THEN update levels.
        #
        # This prevents a new high/low from becoming its own
        # baseline before the breakout check.
        # ====================================================

        old_high = self.running_high
        old_low = self.running_low


        # ====================================================
        # NEW HIGH -> LONG
        # ====================================================

        if current_price > old_high:

            sl = old_low

            if sl is not None:

                logging.warning(
                    "============================================"
                )

                logging.warning(
                    "NEW HIGH BREAKOUT"
                )

                logging.warning(
                    f"OLD HIGH = {old_high}"
                )

                logging.warning(
                    f"PRICE     = {current_price}"
                )

                logging.warning(
                    f"SL        = {sl}"
                )

                logging.warning(
                    "============================================"
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


        # ====================================================
        # NEW LOW -> SHORT
        # ====================================================

        if current_price < old_low:

            sl = old_high

            if sl is not None:

                logging.warning(
                    "============================================"
                )

                logging.warning(
                    "NEW LOW BREAKDOWN"
                )

                logging.warning(
                    f"OLD LOW  = {old_low}"
                )

                logging.warning(
                    f"PRICE     = {current_price}"
                )

                logging.warning(
                    f"SL        = {sl}"
                )

                logging.warning(
                    "============================================"
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


        # ====================================================
        # NO ENTRY
        #
        # NOW update running HIGH / LOW.
        # ====================================================

        changed = False


        if (
            current_price
            > self.running_high
        ):

            self.running_high = (
                current_price
            )

            changed = True


        if (
            current_price
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


        # ====================================================
        # MESSAGE
        # ====================================================

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


                # ------------------------------------------------
                # Ignore non-price messages.
                # ------------------------------------------------

                if msg_type in (
                    "subscriptions",
                    "heartbeat",
                    "pong"
                ):

                    return


                # ------------------------------------------------
                # TICKER FORMAT
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


                # ------------------------------------------------
                # Wrapped ticker.
                # ------------------------------------------------

                result = data.get(
                    "result"
                )

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
                    "WEBSOCKET MESSAGE ERROR | "
                    f"{exc}"
                )


        # ====================================================
        # ERROR
        # ====================================================

        def on_error(
            ws,
            error
        ):

            logging.error(
                f"WEBSOCKET ERROR | "
                f"{error}"
            )


        # ====================================================
        # CLOSE
        # ====================================================

        def on_close(
            ws,
            close_status_code,
            close_msg
        ):

            logging.warning(
                "WEBSOCKET DISCONNECTED | "
                f"CODE={close_status_code} | "
                f"MSG={close_msg}"
            )


        # ====================================================
        # OPEN
        # ====================================================

        def on_open(ws):

            logging.warning(
                "WEBSOCKET CONNECTED"
            )


            # ------------------------------------------------
            # Subscribe to ticker.
            # ------------------------------------------------

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


        # ====================================================
        # WEBSOCKET LOOP
        # ====================================================

        def websocket_loop():

            while True:

                try:

                    logging.warning(
                        "CONNECTING WEBSOCKET | "
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
                        "WEBSOCKET CRASHED | "
                        f"{exc}"
                    )


                logging.warning(
                    "WEBSOCKET RECONNECTING "
                    "IN 3 SECONDS..."
                )

                time.sleep(3)


        # ====================================================
        # STARTUP
        # ====================================================

        logging.warning(
            "============================================"
        )

        logging.warning(
            "XAUTUSD NEW EXTREME BREAKOUT ENGINE v28.0"
        )

        logging.warning(
            f"SYMBOL      = {SYMBOL}"
        )

        logging.warning(
            f"LEVERAGE    = {LEVERAGE}x"
        )

        logging.warning(
            f"BALANCE USE = "
            f"{BALANCE_FRACTION * 100}%"
        )

        logging.warning(
            f"REST API    = {BASE_URL}"
        )

        logging.warning(
            f"WEBSOCKET   = {WS_URL}"
        )

        logging.warning(
            "STRATEGY    = "
            "05:45 -> NEW HIGH / NEW LOW"
        )

        logging.warning(
            "LATE START  = "
            "RECOVER 05:45 -> CURRENT TIME"
        )

        logging.warning(
            "============================================"
        )


        # ====================================================
        # LEVERAGE
        # ====================================================

        set_leverage(
            self.product_id
        )


        # ====================================================
        # STARTUP POSITION SYNC
        # ====================================================

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


                # ------------------------------------------------
                # If state already has today's position details,
                # preserve them.
                #
                # We do NOT close a same-day position merely
                # because bot restarted.
                # ------------------------------------------------

                current_day = trading_day_start(
                    now_ist()
                )

                if (
                    self.day_start
                    == current_day
                    and self.day_reset_done
                ):

                    logging.warning(
                        "SAME-DAY RESTART | "
                        "PRESERVING CURRENT POSITION STATE."
                    )

                else:

                    logging.warning(
                        "POSITION MAY BELONG TO "
                        "PREVIOUS TRADING DAY."
                    )


            else:

                logging.info(
                    "STARTUP POSITION = FLAT"
                )


        except Exception as exc:

            logging.error(
                "STARTUP POSITION CHECK FAILED | "
                f"{exc}"
            )


        # ====================================================
        # START WEBSOCKET THREAD
        # ====================================================

        ws_thread = threading.Thread(
            target=websocket_loop,
            daemon=True
        )

        ws_thread.start()


        # ====================================================
        # WATCHDOG
        # ====================================================

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
                    # Exchange flat while local position exists.
                    # ------------------------------------------------

                    if (
                        exchange_size == 0
                        and self.last_position != 0
                    ):

                        logging.warning(
                            "WATCHDOG | "
                            "EXCHANGE FLAT WHILE "
                            "LOCAL POSITION EXISTS."
                        )


                    # ------------------------------------------------
                    # Exchange has position while local says flat.
                    # ------------------------------------------------

                    elif (
                        exchange_size != 0
                        and self.last_position == 0
                    ):

                        logging.warning(
                            "WATCHDOG | "
                            "EXCHANGE POSITION DETECTED | "
                            f"SIZE={exchange_size}"
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
                    "MAIN WATCHDOG ERROR | "
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
            "FATAL STARTUP ERROR | "
            f"{exc}"
        )

        raise
