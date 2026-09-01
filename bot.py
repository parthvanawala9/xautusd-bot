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
# XAUTUSD BREAKOUT + REVERSAL BOT
# VERSION 30.0
#
# IMPORTANT ARCHITECTURE
# ------------------------------------------------------------
# 1. REAL-TIME MARKET PRICE:
#       Delta PUBLIC "trades" websocket channel
#
# 2. START OF STRATEGY:
#       05:45 IST
#
# 3. DAY RANGE:
#       Starts fresh at 05:45.
#
# 4. FIRST LIVE PRICE:
#       Establishes initial HIGH and LOW.
#
# 5. FLAT:
#       price > running_high  -> LONG
#       price < running_low   -> SHORT
#
# 6. LONG:
#       SL = LOW existing at LONG entry.
#       Track highest price during LONG.
#
# 7. SHORT:
#       SL = HIGH existing at SHORT entry.
#       Track lowest price during SHORT.
#
# 8. SL:
#       Exchange bracket SL.
#       When position disappears after SL,
#       immediately reverse.
#
# 9. RESTART RECOVERY:
#       If bot starts after 05:45,
#       historical 1-minute candles are loaded from
#       05:45 until current time.
#
#       This reconstructs the day's HIGH/LOW instead
#       of incorrectly using the restart price as baseline.
#
# 10. IMPORTANT:
#       Restart recovery DOES NOT blindly enter a missed
#       historical trade. It reconstructs the range and
#       waits for the next live breakout.
#
# 11. Saturday:
#       Square-off at 05:00 IST.
#
# 12. Sunday:
#       No trading.
#
# 13. Friday position:
#       Closed Saturday at 05:00.
# ============================================================


load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


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
    os.getenv("LEVERAGE", "50")
)

BALANCE_FRACTION = Decimal(
    os.getenv("BALANCE_FRACTION", "0.10")
)

STATE_FILE = os.getenv(
    "STATE_FILE",
    "xautusd_state.json"
)

POSITION_CHECK_SECONDS = float(
    os.getenv("POSITION_CHECK_SECONDS", "1.0")
)

RECONNECT_SECONDS = float(
    os.getenv("RECONNECT_SECONDS", "3")
)

HISTORICAL_RESOLUTION = "1m"


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
# HTTP
# ============================================================

session = requests.Session()

session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "XAUTUSD-Breakout-Reversal-30.0"
})


# ============================================================
# TIME
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
    return day_start + timedelta(minutes=15)


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
        boundary = dt.replace(
            hour=5,
            minute=30,
            second=0,
            microsecond=0
        )

        return dt < boundary

    return False


def is_saturday_squareoff_time(dt=None):
    dt = dt or now_ist()

    return (
        dt.weekday() == 5
        and dt.hour == 5
        and dt.minute < 30
    )


# ============================================================
# DELTA AUTH
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

    headers = {}

    if auth:
        headers = sign_request(
            method,
            path,
            query_string,
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
            f"Delta API Error: {data}"
        )

    return data


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
# POSITION
# ============================================================

def get_position(product_id):

    data = api_call(
        "GET",
        "/v2/positions",
        params={
            "product_id": int(product_id)
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
        "Could not retrieve USD/USDT wallet balance."
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
                "leverage": str(
                    LEVERAGE
                )
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
        "ORDER SIZE | "
        f"BALANCE={balance} | "
        f"MARGIN={margin} | "
        f"NOTIONAL={notional} | "
        f"SIZE={size}"
    )

    return size


# ============================================================
# MARKET ENTRY
# ============================================================

def execute_market_entry(
    product_id,
    side,
    size,
    sl_price
):

    if sl_price is None:

        raise RuntimeError(
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
            f"xbrk_{int(time.time() * 1000)}"[-32:]
    }

    logging.warning(
        "========================================"
    )

    logging.warning(
        "LIVE ENTRY ORDER"
    )

    logging.warning(
        f"SIDE       = {side.upper()}"
    )

    logging.warning(
        f"SIZE       = {abs(size)}"
    )

    logging.warning(
        f"SL         = {sl_price}"
    )

    logging.warning(
        f"SYMBOL     = {SYMBOL}"
    )

    logging.warning(
        "========================================"
    )

    return api_call(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
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
            f"xclose_{int(time.time() * 1000)}"[-32:]
    }

    logging.warning(
        "CLOSE POSITION | "
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
# HISTORICAL RANGE RECOVERY
# ============================================================

def get_historical_range(
    start_dt,
    end_dt
):

    start_ts = int(
        start_dt.timestamp()
    )

    end_ts = int(
        end_dt.timestamp()
    )

    if end_ts <= start_ts:

        return None, None, 0

    logging.warning(
        "RECOVERY | Loading historical "
        f"{HISTORICAL_RESOLUTION} candles"
    )

    logging.warning(
        "RECOVERY | "
        f"FROM={start_dt.strftime('%Y-%m-%d %H:%M:%S')} "
        f"IST"
    )

    logging.warning(
        "RECOVERY | "
        f"TO={end_dt.strftime('%Y-%m-%d %H:%M:%S')} "
        f"IST"
    )

    all_candles = []

    # --------------------------------------------------------
    # API allows maximum 2000 candles.
    # 1m candles for one trading day are comfortably below this.
    # --------------------------------------------------------

    cursor_start = start_ts

    while cursor_start < end_ts:

        data = api_call(
            "GET",
            "/v2/history/candles",
            params={
                "resolution":
                    HISTORICAL_RESOLUTION,

                "symbol":
                    SYMBOL,

                "start":
                    cursor_start,

                "end":
                    end_ts
            },
            auth=False
        )

        candles = data.get(
            "result",
            []
        )

        if not candles:
            break

        all_candles.extend(
            candles
        )

        # Find latest candle time.
        times = []

        for candle in candles:

            try:

                times.append(
                    int(
                        candle["time"]
                    )
                )

            except Exception:
                pass

        if not times:
            break

        latest_time = max(times)

        if latest_time <= cursor_start:
            break

        # Move forward by one minute.
        cursor_start = (
            latest_time + 60
        )

        if len(candles) < 2000:
            break

    if not all_candles:

        logging.warning(
            "RECOVERY | No historical candles returned."
        )

        return None, None, 0

    high = None
    low = None

    valid_count = 0

    for candle in all_candles:

        try:

            candle_time = int(
                candle["time"]
            )

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

        except Exception:

            continue

        # ----------------------------------------------------
        # Keep only candles that overlap our strategy window.
        # ----------------------------------------------------

        if (
            candle_time < start_ts
            or candle_time > end_ts
        ):
            continue

        if high is None or candle_high > high:
            high = candle_high

        if low is None or candle_low < low:
            low = candle_low

        valid_count += 1

    if high is None or low is None:

        return None, None, 0

    logging.warning(
        "========================================"
    )

    logging.warning(
        "RECOVERY RANGE READY"
    )

    logging.warning(
        f"HIGH = {high}"
    )

    logging.warning(
        f"LOW  = {low}"
    )

    logging.warning(
        f"CANDLES USED = {valid_count}"
    )

    logging.warning(
        "========================================"
    )

    return high, low, valid_count


# ============================================================
# STRATEGY
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
        # DAY
        # ----------------------------------------------------

        self.day_start = None

        # ----------------------------------------------------
        # GLOBAL RANGE
        # ----------------------------------------------------

        self.running_high = None
        self.running_low = None

        # ----------------------------------------------------
        # POSITION
        # ----------------------------------------------------

        self.last_position = 0

        # ----------------------------------------------------
        # ACTIVE SL
        # ----------------------------------------------------

        self.current_sl = None

        # ----------------------------------------------------
        # CURRENT TRADE EXTREMES
        # ----------------------------------------------------

        self.trade_high = None
        self.trade_low = None

        # ----------------------------------------------------
        # LAST PRICE
        # ----------------------------------------------------

        self.last_price = None

        # ----------------------------------------------------
        # SESSION
        # ----------------------------------------------------

        self.session_ready = False

        # ----------------------------------------------------
        # ORDER LOCK
        # ----------------------------------------------------

        self.order_in_flight = False

        # ----------------------------------------------------
        # HISTORICAL RECOVERY DONE
        # ----------------------------------------------------

        self.recovery_done = False

        # ----------------------------------------------------
        # PREVENT DUPLICATE 05:45 RESET
        # ----------------------------------------------------

        self.day_reset_done = False

        # ----------------------------------------------------
        # THREAD LOCK
        # ----------------------------------------------------

        self.lock = threading.RLock()

        self.load_state()


    # ========================================================
    # STATE LOAD
    # ========================================================

    def load_state(self):

        if not os.path.exists(
            STATE_FILE
        ):

            logging.info(
                "STATE | No state file found."
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

            if state.get(
                "day_start"
            ):

                self.day_start = (
                    datetime.fromisoformat(
                        state["day_start"]
                    )
                )

            if state.get(
                "running_high"
            ) is not None:

                self.running_high = Decimal(
                    str(
                        state[
                            "running_high"
                        ]
                    )
                )

            if state.get(
                "running_low"
            ) is not None:

                self.running_low = Decimal(
                    str(
                        state[
                            "running_low"
                        ]
                    )
                )

            if state.get(
                "current_sl"
            ) is not None:

                self.current_sl = Decimal(
                    str(
                        state[
                            "current_sl"
                        ]
                    )
                )

            if state.get(
                "trade_high"
            ) is not None:

                self.trade_high = Decimal(
                    str(
                        state[
                            "trade_high"
                        ]
                    )
                )

            if state.get(
                "trade_low"
            ) is not None:

                self.trade_low = Decimal(
                    str(
                        state[
                            "trade_low"
                        ]
                    )
                )

            self.session_ready = bool(
                state.get(
                    "session_ready",
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
                f"STATE LOAD ERROR | {exc}"
            )


    # ========================================================
    # SAVE STATE
    # ========================================================

    def save_state(self):

        state = {

            "day_start":
                (
                    self.day_start.isoformat()
                    if self.day_start
                    else None
                ),

            "running_high":
                (
                    str(
                        self.running_high
                    )
                    if self.running_high is not None
                    else None
                ),

            "running_low":
                (
                    str(
                        self.running_low
                    )
                    if self.running_low is not None
                    else None
                ),

            "current_sl":
                (
                    str(
                        self.current_sl
                    )
                    if self.current_sl is not None
                    else None
                ),

            "trade_high":
                (
                    str(
                        self.trade_high
                    )
                    if self.trade_high is not None
                    else None
                ),

            "trade_low":
                (
                    str(
                        self.trade_low
                    )
                    if self.trade_low is not None
                    else None
                ),

            "session_ready":
                self.session_ready,

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
    # NEW DAY
    # ========================================================

    def handle_new_day(
        self,
        now
    ):

        new_day = trading_day_start(
            now
        )

        if (
            self.day_start
            == new_day
        ):

            return

        logging.warning(
            "========================================"
        )

        logging.warning(
            "NEW TRADING DATE"
        )

        logging.warning(
            f"DAY START = {new_day}"
        )

        logging.warning(
            "========================================"
        )

        self.day_start = new_day

        self.running_high = None
        self.running_low = None

        self.current_sl = None

        self.trade_high = None
        self.trade_low = None

        self.session_ready = False
        self.recovery_done = False

        self.day_reset_done = False

        self.save_state()


    # ========================================================
    # 05:45 SESSION START
    # ========================================================

    def perform_0545_reset(
        self,
        now
    ):

        if is_weekend_blocked(
            now
        ):
            return False

        start_time = strategy_start_time(
            self.day_start
        )

        if now < start_time:
            return False

        if self.day_reset_done:
            return True

        logging.warning(
            "========================================"
        )

        logging.warning(
            "05:45 IST SESSION RESET"
        )

        logging.warning(
            "OLD DAY RANGE WILL NOT BE USED."
        )

        logging.warning(
            "========================================"
        )

        position = get_position(
            self.product_id
        )

        current_size = position[
            "size"
        ]

        if current_size != 0:

            logging.warning(
                "05:45 | OLD POSITION FOUND | "
                f"SIZE={current_size}"
            )

            close_position_market(
                self.product_id,
                current_size
            )

            for _ in range(40):

                time.sleep(
                    0.25
                )

                check = get_position(
                    self.product_id
                )

                if check[
                    "size"
                ] == 0:

                    break

            final_check = get_position(
                self.product_id
            )

            if final_check[
                "size"
            ] != 0:

                logging.error(
                    "05:45 RESET FAILED | "
                    "OLD POSITION STILL OPEN"
                )

                return False

        self.last_position = 0

        self.running_high = None
        self.running_low = None

        self.current_sl = None

        self.trade_high = None
        self.trade_low = None

        self.session_ready = False

        self.recovery_done = False

        self.day_reset_done = True

        self.save_state()

        logging.warning(
            "05:45 RESET COMPLETE"
        )

        return True


    # ========================================================
    # START / RECOVER SESSION
    # ========================================================

    def prepare_session(
        self,
        now,
        current_price=None
    ):

        start_time = strategy_start_time(
            self.day_start
        )

        # ----------------------------------------------------
        # Before 05:45
        # ----------------------------------------------------

        if now < start_time:

            return False

        # ----------------------------------------------------
        # If already prepared
        # ----------------------------------------------------

        if self.session_ready:

            return True

        # ----------------------------------------------------
        # Make sure 05:45 reset occurred.
        # ----------------------------------------------------

        if not self.perform_0545_reset(
            now
        ):

            return False

        # ----------------------------------------------------
        # If bot is starting/restarting AFTER 05:45,
        # recover complete range from historical 1m candles.
        #
        # If we're exactly at session start and no historical
        # candles exist yet, first live trade establishes range.
        # ----------------------------------------------------

        if now > (
            start_time
            + timedelta(seconds=10)
        ):

            try:

                high, low, count = (
                    get_historical_range(
                        start_time,
                        now
                    )
                )

                if (
                    high is not None
                    and low is not None
                    and count > 0
                ):

                    self.running_high = high
                    self.running_low = low

                    self.recovery_done = True

                    self.session_ready = True

                    self.save_state()

                    logging.warning(
                        "SESSION RECOVERED FROM HISTORY | "
                        f"HIGH={high} | "
                        f"LOW={low}"
                    )

                    logging.warning(
                        "IMPORTANT | "
                        "Bot will now wait for the NEXT "
                        "LIVE breakout."
                    )

                    return True

            except Exception as exc:

                logging.exception(
                    "HISTORICAL RECOVERY FAILED | "
                    f"{exc}"
                )

        # ----------------------------------------------------
        # If historical recovery wasn't possible,
        # use current live price as first baseline.
        # ----------------------------------------------------

        if (
            self.running_high is None
            and current_price is not None
        ):

            self.running_high = current_price
            self.running_low = current_price

            logging.warning(
                "FIRST LIVE SESSION BASELINE | "
                f"HIGH={current_price} | "
                f"LOW={current_price}"
            )

        if (
            self.running_high is not None
            and self.running_low is not None
        ):

            self.session_ready = True

            self.save_state()

            return True

        return False


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

        with self.lock:

            if self.order_in_flight:

                logging.warning(
                    "ENTRY BLOCKED | "
                    "ORDER ALREADY IN FLIGHT"
                )

                return False

            now = now_ist()

            if is_weekend_blocked(
                now
            ):

                return False

            if now < strategy_start_time(
                self.day_start
            ):

                return False

            if sl_price is None:

                logging.error(
                    "ENTRY BLOCKED | SL NONE"
                )

                return False

            # ------------------------------------------------
            # Direction validation
            # ------------------------------------------------

            if direction == "LONG":

                if sl_price >= price:

                    logging.error(
                        "LONG ENTRY BLOCKED | "
                        f"PRICE={price} | "
                        f"SL={sl_price}"
                    )

                    return False

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
                    f"UNKNOWN DIRECTION={direction}"
                )

                return False

            # ------------------------------------------------
            # Exchange position check
            # ------------------------------------------------

            existing = get_position(
                self.product_id
            )

            if existing[
                "size"
            ] != 0:

                logging.warning(
                    "ENTRY BLOCKED | "
                    "EXCHANGE POSITION EXISTS | "
                    f"SIZE={existing['size']}"
                )

                self.last_position = (
                    existing["size"]
                )

                return False

            # ------------------------------------------------
            # Calculate size
            # ------------------------------------------------

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

                result = execute_market_entry(
                    self.product_id,
                    side,
                    size,
                    sl_price
                )

                logging.warning(
                    "ENTRY REQUEST ACCEPTED BY DELTA"
                )

                logging.warning(
                    f"ORDER RESPONSE = {result}"
                )

                # ------------------------------------------------
                # Wait for actual position.
                # ------------------------------------------------

                filled_size = 0

                for _ in range(50):

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
                        "ENTRY ORDER SENT BUT "
                        "POSITION FILL NOT CONFIRMED."
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
                    "ENTRY CONFIRMED"
                )

                logging.warning(
                    f"DIRECTION = {direction}"
                )

                logging.warning(
                    f"SIZE      = {filled_size}"
                )

                logging.warning(
                    f"ENTRY     ≈ {price}"
                )

                logging.warning(
                    f"SL        = {sl_price}"
                )

                logging.warning(
                    f"REASON    = {reason}"
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

        peak = (
            self.trade_high
            if self.trade_high is not None
            else self.running_high
        )

        if peak is None:

            logging.error(
                "REVERSAL BLOCKED | "
                "NO LONG PEAK"
            )

            return False

        logging.warning(
            "LONG SL HIT -> "
            f"REVERSE SHORT | SL={peak}"
        )

        position = get_position(
            self.product_id
        )

        if position[
            "size"
        ] != 0:

            logging.warning(
                "Waiting for exchange "
                "to become FLAT..."
            )

            for _ in range(40):

                time.sleep(
                    0.25
                )

                position = get_position(
                    self.product_id
                )

                if position[
                    "size"
                ] == 0:

                    break

        if get_position(
            self.product_id
        )["size"] != 0:

            logging.error(
                "REVERSAL BLOCKED | "
                "OLD LONG STILL OPEN"
            )

            return False

        return self.execute_entry(
            "SHORT",
            price,
            peak,
            "LONG SL HIT -> REVERSE SHORT"
        )


    # ========================================================
    # SHORT -> LONG
    # ========================================================

    def reverse_short_to_long(
        self,
        price
    ):

        trough = (
            self.trade_low
            if self.trade_low is not None
            else self.running_low
        )

        if trough is None:

            logging.error(
                "REVERSAL BLOCKED | "
                "NO SHORT TROUGH"
            )

            return False

        logging.warning(
            "SHORT SL HIT -> "
            f"REVERSE LONG | SL={trough}"
        )

        position = get_position(
            self.product_id
        )

        if position[
            "size"
        ] != 0:

            logging.warning(
                "Waiting for exchange "
                "to become FLAT..."
            )

            for _ in range(40):

                time.sleep(
                    0.25
                )

                position = get_position(
                    self.product_id
                )

                if position[
                    "size"
                ] == 0:

                    break

        if get_position(
            self.product_id
        )["size"] != 0:

            logging.error(
                "REVERSAL BLOCKED | "
                "OLD SHORT STILL OPEN"
            )

            return False

        return self.execute_entry(
            "LONG",
            price,
            trough,
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
                "POSITION CLOSED WITHOUT "
                "LOCAL SL CONFIRMATION"
            )

            logging.warning(
                "NO AUTOMATIC REVERSAL"
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
    # PRICE ENGINE
    # ========================================================

    def on_price(
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
                f"INVALID PRICE | {price_str}"
            )

            return

        with self.lock:

            self.last_price = (
                current_price
            )

            now = now_ist()

            # ------------------------------------------------
            # Saturday square-off
            # ------------------------------------------------

            if is_saturday_squareoff_time(
                now
            ):

                position = get_position(
                    self.product_id
                )

                if position[
                    "size"
                ] != 0:

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

                return

            # ------------------------------------------------
            # Weekend
            # ------------------------------------------------

            if is_weekend_blocked(
                now
            ):

                return

            # ------------------------------------------------
            # Day
            # ------------------------------------------------

            self.handle_new_day(
                now
            )

            # ------------------------------------------------
            # Before 05:45
            # ------------------------------------------------

            if now < strategy_start_time(
                self.day_start
            ):

                return

            # ------------------------------------------------
            # Session preparation
            # ------------------------------------------------

            if not self.prepare_session(
                now,
                current_price
            ):

                return

            # ------------------------------------------------
            # Exchange position
            # ------------------------------------------------

            position = get_position(
                self.product_id
            )

            current_size = (
                position["size"]
            )

            # ------------------------------------------------
            # Position disappeared
            # ------------------------------------------------

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

            # ------------------------------------------------
            # LONG
            # ------------------------------------------------

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

                    logging.info(
                        "LONG PEAK | "
                        f"{self.trade_high}"
                    )

                    self.save_state()

                return

            # ------------------------------------------------
            # SHORT
            # ------------------------------------------------

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

                    logging.info(
                        "SHORT TROUGH | "
                        f"{self.trade_low}"
                    )

                    self.save_state()

                return

            # ------------------------------------------------
            # FLAT
            # ------------------------------------------------

            self.last_position = 0
            self.current_sl = None

            old_high = (
                self.running_high
            )

            old_low = (
                self.running_low
            )

            if (
                old_high is None
                or old_low is None
            ):

                self.running_high = (
                    current_price
                )

                self.running_low = (
                    current_price
                )

                self.save_state()

                logging.warning(
                    "BASELINE CREATED | "
                    f"HIGH={current_price} | "
                    f"LOW={current_price}"
                )

                return

            # =================================================
            # NEW HIGH -> LONG
            # =================================================

            if current_price > old_high:

                sl = old_low

                logging.warning(
                    "========================================"
                )

                logging.warning(
                    "NEW HIGH BREAKOUT DETECTED"
                )

                logging.warning(
                    f"OLD HIGH = {old_high}"
                )

                logging.warning(
                    f"PRICE    = {current_price}"
                )

                logging.warning(
                    f"SL       = {sl}"
                )

                logging.warning(
                    "SIGNAL   = LONG"
                )

                logging.warning(
                    "========================================"
                )

                entered = self.execute_entry(
                    "LONG",
                    current_price,
                    sl,
                    "NEW HIGH BREAKOUT"
                )

                if entered:

                    self.running_high = (
                        current_price
                    )

                    self.trade_high = (
                        current_price
                    )

                    self.save_state()

                    return

            # =================================================
            # NEW LOW -> SHORT
            # =================================================

            if current_price < old_low:

                sl = old_high

                logging.warning(
                    "========================================"
                )

                logging.warning(
                    "NEW LOW BREAKDOWN DETECTED"
                )

                logging.warning(
                    f"OLD LOW  = {old_low}"
                )

                logging.warning(
                    f"PRICE    = {current_price}"
                )

                logging.warning(
                    f"SL       = {sl}"
                )

                logging.warning(
                    "SIGNAL   = SHORT"
                )

                logging.warning(
                    "========================================"
                )

                entered = self.execute_entry(
                    "SHORT",
                    current_price,
                    sl,
                    "NEW LOW BREAKDOWN"
                )

                if entered:

                    self.running_low = (
                        current_price
                    )

                    self.trade_low = (
                        current_price
                    )

                    self.save_state()

                    return

            # =================================================
            # UPDATE RANGE
            # =================================================

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
    # PUBLIC WEBSOCKET
    # ========================================================

    def start_public_websocket(
        self
    ):

        def subscribe(
            ws,
            channel,
            symbols
        ):

            payload = {
                "type": "subscribe",
                "payload": {
                    "channels": [
                        {
                            "name":
                                channel,

                            "symbols":
                                symbols
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
                f"SUBSCRIBE SENT | "
                f"{channel} | "
                f"{symbols}"
            )


        def on_open(
            ws
        ):

            logging.warning(
                "========================================"
            )

            logging.warning(
                "PUBLIC WEBSOCKET CONNECTED"
            )

            logging.warning(
                f"URL = {WS_URL}"
            )

            logging.warning(
                "========================================"
            )

            # ------------------------------------------------
            # REAL-TIME TRADES
            # ------------------------------------------------

            subscribe(
                ws,
                "trades",
                [SYMBOL]
            )


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
                # Subscription confirmation
                # ------------------------------------------------

                if msg_type == "subscriptions":

                    logging.warning(
                        f"SUBSCRIPTION RESPONSE | {data}"
                    )

                    return

                # ------------------------------------------------
                # Heartbeat / pong
                # ------------------------------------------------

                if msg_type in (
                    "heartbeat",
                    "pong"
                ):

                    return

                # ------------------------------------------------
                # REAL-TIME TRADES
                #
                # Current Delta format:
                #
                # {
                #   "p": "72141.5",
                #   "sy": "BTCUSD",
                #   "type": "trades"
                # }
                # ------------------------------------------------

                if msg_type == "trades":

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

                        self.on_price(
                            str(price)
                        )

                    return

                # ------------------------------------------------
                # Error messages
                # ------------------------------------------------

                if (
                    data.get(
                        "success"
                    ) is False
                ):

                    logging.error(
                        f"WEBSOCKET ERROR RESPONSE | "
                        f"{data}"
                    )

                    return

                # ------------------------------------------------
                # Log unexpected messages.
                # ------------------------------------------------

                if msg_type:

                    logging.info(
                        f"WS MESSAGE | {data}"
                    )

            except Exception as exc:

                logging.exception(
                    f"WS MESSAGE ERROR | {exc}"
                )


        def on_error(
            ws,
            error
        ):

            logging.error(
                f"PUBLIC WS ERROR | {error}"
            )


        def on_close(
            ws,
            close_status_code,
            close_msg
        ):

            logging.warning(
                "PUBLIC WS CLOSED | "
                f"CODE={close_status_code} | "
                f"MSG={close_msg}"
            )


        while True:

            try:

                logging.warning(
                    "CONNECTING PUBLIC WEBSOCKET..."
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
                    f"PUBLIC WS CRASH | {exc}"
                )

            logging.warning(
                f"WS RECONNECTING IN "
                f"{RECONNECT_SECONDS} SECONDS..."
            )

            time.sleep(
                RECONNECT_SECONDS
            )


    # ========================================================
    # PRIVATE WEBSOCKET
    #
    # Used only for visibility of orders/positions.
    # Trading itself does NOT depend on this socket.
    # ========================================================

    def start_private_websocket(
        self
    ):

        private_url = os.getenv(
            "DELTA_PRIVATE_WS_URL",
            "wss://socket.india.delta.exchange"
        )

        def generate_ws_signature():

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


        def subscribe(
            ws,
            channel
        ):

            payload = {
                "type": "subscribe",
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
                "PRIVATE WEBSOCKET CONNECTED"
            )

            timestamp, signature = (
                generate_ws_signature()
            )

            auth_message = {
                "type":
                    "key-auth",

                "payload": {
                    "api-key":
                        API_KEY,

                    "signature":
                        signature,

                    "timestamp":
                        timestamp
                }
            }

            ws.send(
                json.dumps(
                    auth_message
                )
            )


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

                if msg_type == "key-auth":

                    if data.get(
                        "success"
                    ):

                        logging.warning(
                            "PRIVATE WS AUTHENTICATED"
                        )

                        subscribe(
                            ws,
                            "orders"
                        )

                        subscribe(
                            ws,
                            "positions"
                        )

                    else:

                        logging.error(
                            "PRIVATE WS AUTH FAILED | "
                            f"{data}"
                        )

                    return

                if msg_type in (
                    "orders",
                    "positions",
                    "v2/user_trades"
                ):

                    logging.warning(
                        f"PRIVATE EVENT | {data}"
                    )

            except Exception as exc:

                logging.exception(
                    f"PRIVATE WS ERROR | {exc}"
                )


        def on_error(
            ws,
            error
        ):

            logging.error(
                f"PRIVATE WS ERROR | {error}"
            )


        def on_close(
            ws,
            code,
            msg
        ):

            logging.warning(
                "PRIVATE WS CLOSED | "
                f"CODE={code} | MSG={msg}"
            )


        while True:

            try:

                ws_app = websocket.WebSocketApp(
                    private_url,
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
                    f"PRIVATE WS CRASH | {exc}"
                )

            time.sleep(
                RECONNECT_SECONDS
            )


    # ========================================================
    # WATCHDOG
    # ========================================================

    def watchdog(
        self
    ):

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

                    with self.lock:

                        if (
                            exchange_size !=
                            self.last_position
                        ):

                            logging.warning(
                                "POSITION CHANGE | "
                                f"LOCAL={self.last_position} | "
                                f"EXCHANGE={exchange_size}"
                            )

                        # ------------------------------------------------
                        # If exchange has position, synchronize.
                        # ------------------------------------------------

                        if exchange_size != 0:

                            self.last_position = (
                                exchange_size
                            )

                        # ------------------------------------------------
                        # If exchange flattened a position,
                        # don't reverse here because the price
                        # engine needs current price to determine
                        # whether the SL was hit.
                        # ------------------------------------------------

                        elif (
                            exchange_size == 0
                            and self.last_position != 0
                        ):

                            logging.warning(
                                "WATCHDOG | "
                                "EXCHANGE FLAT BUT LOCAL "
                                "POSITION EXISTS."
                            )

                            if (
                                self.last_price
                                is not None
                            ):

                                old_size = (
                                    self.last_position
                                )

                                self.handle_position_closed(
                                    old_size,
                                    self.last_price
                                )

                    last_position_check = (
                        now
                    )

                time.sleep(
                    0.25
                )

            except Exception as exc:

                logging.exception(
                    f"WATCHDOG ERROR | {exc}"
                )

                time.sleep(2)


    # ========================================================
    # START
    # ========================================================

    def start(
        self
    ):

        logging.warning(
            "============================================"
        )

        logging.warning(
            "XAUTUSD BREAKOUT + REVERSAL BOT v30.0"
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
            f"PUBLIC WS    = {WS_URL}"
        )

        logging.warning(
            "PRICE FEED   = REAL-TIME TRADES"
        )

        logging.warning(
            "START TIME   = 05:45 IST"
        )

        logging.warning(
            "RECOVERY     = 1-MINUTE HISTORY"
        )

        logging.warning(
            "============================================"
        )

        # --------------------------------------------------------
        # Leverage
        # --------------------------------------------------------

        set_leverage(
            self.product_id
        )

        # --------------------------------------------------------
        # Startup position
        # --------------------------------------------------------

        try:

            position = get_position(
                self.product_id
            )

            self.last_position = (
                position["size"]
            )

            if position[
                "size"
            ] != 0:

                logging.warning(
                    "STARTUP POSITION | "
                    f"SIZE={position['size']} | "
                    f"ENTRY={position['entry_price']}"
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

        # --------------------------------------------------------
        # Start public market feed
        # --------------------------------------------------------

        public_thread = threading.Thread(
            target=self.start_public_websocket,
            daemon=True
        )

        public_thread.start()

        # --------------------------------------------------------
        # Start private visibility feed
        # --------------------------------------------------------

        private_thread = threading.Thread(
            target=self.start_private_websocket,
            daemon=True
        )

        private_thread.start()

        # --------------------------------------------------------
        # Start watchdog
        # --------------------------------------------------------

        watchdog_thread = threading.Thread(
            target=self.watchdog,
            daemon=True
        )

        watchdog_thread.start()

        # --------------------------------------------------------
        # Main keep-alive
        # --------------------------------------------------------

        while True:

            try:

                time.sleep(10)

            except KeyboardInterrupt:

                logging.warning(
                    "BOT STOPPED BY USER."
                )

                break

            except Exception as exc:

                logging.exception(
                    f"MAIN LOOP ERROR | {exc}"
                )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:

        product_info = get_product()

        bot = TradingStrategy(
            product_info
        )

        bot.start()

    except KeyboardInterrupt:

        logging.warning(
            "BOT STOPPED BY USER."
        )

    except Exception as exc:

        logging.exception(
            f"FATAL STARTUP ERROR | {exc}"
        )

        raise
