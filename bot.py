import os
import time
import json
import hmac
import hashlib
import logging
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv


# ============================================================
# XAUTUSD FIXED-SL BREAKOUT BOT
# ============================================================
#
# TRADING DAY
# ------------------------------------------------------------
# 05:30 IST -> next day 05:30 IST
#
# TRADING START
# ------------------------------------------------------------
# 05:45 IST
#
# INITIAL RANGE
# ------------------------------------------------------------
# 05:30-05:45 candle:
#
#   HIGH = initial day high
#   LOW  = initial day low
#
# At/after 05:45:
#
#   BREAK DAY HIGH -> LONG
#   BREAK DAY LOW  -> SHORT
#
# IMPORTANT
# ------------------------------------------------------------
# The 05:30 candle is ONLY used to establish the starting
# range.
#
# It is NOT itself an SL.
#
#
# FIXED SL
# ------------------------------------------------------------
# LONG:
#   SL = DAY LOW at the moment of entry
#
# SHORT:
#   SL = DAY HIGH at the moment of entry
#
# Once placed:
#
#   SL DOES NOT TRAIL.
#
# Day high/low can continue changing, but current_sl remains
# unchanged until the position closes or a new trading day
# requires a new SL.
#
#
# SL REVERSAL
# ------------------------------------------------------------
# LONG SL hit:
#   cancel old LONG SL
#   LONG closes
#   SHORT opens
#   SHORT SL = CURRENT DAY HIGH
#
# SHORT SL hit:
#   cancel old SHORT SL
#   SHORT closes
#   LONG opens
#   LONG SL = CURRENT DAY LOW
#
#
# CRITICAL STOP RULE
# ------------------------------------------------------------
# Whenever a NEW SL is required:
#
#   1. Cancel ALL existing stop orders.
#   2. Verify old stops are gone.
#   3. Place exactly ONE new stop.
#   4. Verify the new stop.
#
# The same unchanged SL is NOT recreated every loop.
#
#
# OVERNIGHT
# ------------------------------------------------------------
# If a position survives into the next trading day:
#
#   05:30 -> new day starts
#   05:30-05:45 -> establish new day range
#   after 05:45:
#
#   carried LONG:
#       new SL = new day's LOW
#
#   carried SHORT:
#       new SL = new day's HIGH
#
# Old SL is deleted before the new one is created.
#
#
# MANUAL CLOSE
# ------------------------------------------------------------
# If user manually closes:
#
#   no immediate re-entry.
#
# Bot waits for a genuinely NEW high or NEW low.
#
#
# WEEKEND
# ------------------------------------------------------------
# Saturday 05:00:
#   square off.
#
# Saturday:
#   no trading.
#
# Sunday:
#   no trading.
#
# Monday:
#   new trading day starts at 05:30.
#   trading starts at 05:45.
#
#
# POSITION SIZE
# ------------------------------------------------------------
# 10% balance as margin
# 50x leverage
#
# ============================================================


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")

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

POLL_SECONDS = float(
    os.getenv(
        "POLL_SECONDS",
        "0.50"
    )
)

STATE_FILE = os.getenv(
    "STATE_FILE",
    "xautusd_state.json"
)

STOP_CANCEL_RETRIES = int(
    os.getenv(
        "STOP_CANCEL_RETRIES",
        "5"
    )
)

STOP_CANCEL_WAIT = float(
    os.getenv(
        "STOP_CANCEL_WAIT",
        "0.25"
    )
)


if not API_KEY:
    raise SystemExit(
        "Missing DELTA_API_KEY."
    )

if not API_SECRET:
    raise SystemExit(
        "Missing DELTA_API_SECRET."
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    )
)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": (
            "XAUTUSD-Fixed-SL-Breakout-Bot/11.0"
        )
    }
)


# ============================================================
# TIME
# ============================================================

def now_ist():
    return datetime.now(IST)


def trading_day_start(
    dt=None
):

    dt = dt or now_ist()

    boundary = dt.replace(
        hour=5,
        minute=30,
        second=0,
        microsecond=0
    )

    if dt < boundary:

        boundary -= timedelta(
            days=1
        )

    return boundary


def trading_start_time(
    day
):

    return day + timedelta(
        minutes=15
    )


def weekend_block(
    dt=None
):

    dt = dt or now_ist()

    # Saturday from 05:00 onward.
    if (
        dt.weekday() == 5
        and dt.hour >= 5
    ):
        return True

    # Sunday.
    if dt.weekday() == 6:
        return True

    # Monday before 05:45.
    if (
        dt.weekday() == 0
        and dt < trading_start_time(
            trading_day_start(dt)
        )
    ):
        return True

    return False


def force_squareoff(
    dt=None
):

    dt = dt or now_ist()

    return (
        dt.weekday() == 5
        and dt.hour == 5
        and dt.minute < 5
    )


# ============================================================
# AUTHENTICATION
# ============================================================

def sign_request(
    method,
    path,
    query_string="",
    body=""
):

    timestamp = str(
        int(
            time.time()
        )
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
# API
# ============================================================

def api(
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
            separators=(
                ",",
                ":"
            ),
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
            timeout=15
        )

    except requests.RequestException as exc:

        raise RuntimeError(
            f"Network error "
            f"{method} {path}: {exc}"
        ) from exc

    if not response.ok:

        raise RuntimeError(
            f"{method} {path} "
            f"HTTP {response.status_code}: "
            f"{response.text}"
        )

    try:

        data = response.json()

    except ValueError as exc:

        raise RuntimeError(
            f"Invalid JSON from "
            f"{method} {path}: "
            f"{response.text}"
        ) from exc

    if data.get(
        "success"
    ) is False:

        raise RuntimeError(
            f"{method} {path}: {data}"
        )

    return data


# ============================================================
# PRODUCT
# ============================================================

def get_product():

    return api(
        "GET",
        f"/v2/products/{SYMBOL}"
    )["result"]


# ============================================================
# TICKER
# ============================================================

def get_ticker():

    return api(
        "GET",
        f"/v2/tickers/{SYMBOL}"
    )["result"]


def get_price():

    ticker = get_ticker()

    raw_price = (
        ticker.get("close")
        or ticker.get("last_price")
        or ticker.get("mark_price")
    )

    if raw_price is None:

        raise RuntimeError(
            "Ticker returned no price."
        )

    return Decimal(
        str(raw_price)
    )


# ============================================================
# POSITION
# ============================================================

def get_position(
    product_id
):

    result = api(
        "GET",
        "/v2/positions",
        params={
            "product_id": int(
                product_id
            )
        },
        auth=True
    )["result"]

    if not result:

        return {
            "size": 0,
            "entry_price": None,
            "raw": result
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
        ),
        "raw": result
    }


# ============================================================
# BALANCE
# ============================================================

def get_balance():

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

        if asset not in (
            "USDT",
            "USD"
        ):
            continue

        for key in (
            "balance",
            "available_balance"
        ):

            value = wallet.get(
                key
            )

            if value not in (
                None,
                ""
            ):

                return Decimal(
                    str(value)
                )

    meta = data.get(
        "meta",
        {}
    )

    if meta.get(
        "net_equity"
    ) not in (
        None,
        ""
    ):

        return Decimal(
            str(
                meta["net_equity"]
            )
        )

    raise RuntimeError(
        "Could not find USD/USDT balance."
    )


# ============================================================
# CANDLES
# ============================================================

def candles(
    resolution,
    start_dt,
    end_dt
):

    return api(
        "GET",
        "/v2/history/candles",
        params={
            "resolution": resolution,
            "symbol": SYMBOL,
            "start": int(
                start_dt
                .astimezone(
                    UTC
                )
                .timestamp()
            ),
            "end": int(
                end_dt
                .astimezone(
                    UTC
                )
                .timestamp()
            )
        }
    )["result"]


# ============================================================
# MARKET ORDER
# ============================================================

def market_order(
    product_id,
    side,
    size,
    client_id
):

    body = {
        "product_id": int(
            product_id
        ),
        "product_symbol": SYMBOL,
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "client_order_id": client_id[:32]
    }

    logging.warning(
        "LIVE MARKET ORDER: %s",
        body
    )

    return api(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )


# ============================================================
# STOP MARKET ORDER
# ============================================================

def stop_order(
    product_id,
    side,
    size,
    price,
    client_id
):

    body = {
        "product_id": int(
            product_id
        ),
        "product_symbol": SYMBOL,
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(price),
        "stop_trigger_method": "last_traded_price",
        "reduce_only": True,
        "client_order_id": client_id[:32]
    }

    logging.warning(
        "LIVE STOP ORDER: %s",
        body
    )

    return api(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )


# ============================================================
# CANCEL ORDER
# ============================================================

def cancel_order(
    order_id
):

    if not order_id:
        return

    try:

        api(
            "DELETE",
            f"/v2/orders/{order_id}",
            auth=True
        )

    except RuntimeError as exc:

        if "HTTP 404" in str(exc):

            logging.info(
                "Order %s already removed.",
                order_id
            )

            return

        raise


# ============================================================
# OPEN STOPS
# ============================================================

def open_stops(
    product_id
):

    data = api(
        "GET",
        "/v2/orders",
        params={
            "product_ids": int(
                product_id
            ),
            "states": "open,pending",
            "order_types": "all_stop"
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

        return [result]

    return result


# ============================================================
# CANCEL ALL STOPS
# ============================================================

def cancel_all_stops(
    product_id,
    verify=True
):

    """
    Cancel EVERY stop order for this product.

    This is deliberately separate from SL creation.

    Whenever a new SL is required, this function is called
    first, then the exchange is checked again before a new
    SL is submitted.
    """

    for attempt in range(
        1,
        STOP_CANCEL_RETRIES + 1
    ):

        orders = open_stops(
            product_id
        )

        if not orders:

            if attempt > 1:

                logging.info(
                    "ALL OLD STOP ORDERS CONFIRMED REMOVED."
                )

            return True

        logging.warning(
            "CANCELLING %s OLD STOP ORDER(S) | ATTEMPT %s/%s",
            len(orders),
            attempt,
            STOP_CANCEL_RETRIES
        )

        for order in orders:

            order_id = order.get(
                "id"
            )

            if not order_id:
                continue

            try:

                cancel_order(
                    order_id
                )

            except Exception as exc:

                logging.error(
                    "Could not cancel stop %s: %s",
                    order_id,
                    exc
                )

        if not verify:

            return True

        time.sleep(
            STOP_CANCEL_WAIT
        )

    remaining = open_stops(
        product_id
    )

    if remaining:

        logging.error(
            "STOP CANCEL VERIFICATION FAILED."
        )

        logging.error(
            "REMAINING STOPS = %s",
            remaining
        )

        return False

    return True


# ============================================================
# LEVERAGE
# ============================================================

def set_leverage(
    product_id
):

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
        "LEVERAGE SET: %sx",
        LEVERAGE
    )


# ============================================================
# PRODUCT FIELD
# ============================================================

def dfield(
    product,
    *names,
    default=None
):

    for name in names:

        value = product.get(
            name
        )

        if value is None:
            continue

        try:

            return Decimal(
                str(value)
            )

        except Exception:

            pass

    return default


# ============================================================
# POSITION SIZE
# ============================================================

def contract_size(
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

    contract_value = dfield(
        product,
        "contract_value",
        "contract_value_usd",
        "contract_unit_value"
    )

    if (
        contract_value is None
        or contract_value <= 0
    ):

        raise RuntimeError(
            "Invalid contract_value."
        )

    raw_size = (
        notional
        / (
            price
            * contract_value
        )
    )

    lot = dfield(
        product,
        "lot_size",
        "order_size_increment",
        default=Decimal("1")
    )

    minimum = dfield(
        product,
        "min_order_size",
        "minimum_order_size",
        default=lot
    )

    size_decimal = (
        (
            raw_size
            / lot
        )
        .to_integral_value(
            rounding=ROUND_DOWN
        )
        * lot
    )

    if (
        minimum is not None
        and size_decimal < minimum
    ):

        raise RuntimeError(
            "10% balance is below "
            "exchange minimum order."
        )

    size = int(
        size_decimal
    )

    if size <= 0:

        raise RuntimeError(
            "Calculated size is zero."
        )

    return (
        size,
        balance,
        margin,
        notional
    )


# ============================================================
# STATE
# ============================================================

def load_state():

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(
                file
            )

    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError
    ):

        return {}


def save_state(
    state
):

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


def decimal_value(
    value
):

    if value is None:
        return None

    return Decimal(
        str(value)
    )


# ============================================================
# STRATEGY
# ============================================================

class Strategy:

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

        self.day = None

        self.day_high = None
        self.day_low = None

        self.range_ready = False

        # ----------------------------------------------------
        # POSITION
        # ----------------------------------------------------

        self.last_position = 0

        # ----------------------------------------------------
        # FIXED SL
        # ----------------------------------------------------

        self.current_sl = None
        self.stop_id = None

        # ----------------------------------------------------
        # MANUAL CLOSE
        # ----------------------------------------------------

        self.manual_flat = False

        self.manual_reference_high = None
        self.manual_reference_low = None

        # ----------------------------------------------------
        # BREAKOUT CONTROL
        # ----------------------------------------------------

        self.trading_started = False

        self.high_breakout_consumed = False
        self.low_breakout_consumed = False

        # ----------------------------------------------------
        # OVERNIGHT
        # ----------------------------------------------------

        self.carried_position = False
        self.overnight_sl_set = False

        # ----------------------------------------------------
        # ENTRY LOCK
        # ----------------------------------------------------

        self.entry_lock = False

        self.state = load_state()

        self.restore_state()


    # ========================================================
    # RESTORE
    # ========================================================

    def restore_state(
        self
    ):

        saved_day = self.state.get(
            "day"
        )

        if saved_day:

            try:

                self.day = (
                    datetime.fromisoformat(
                        saved_day
                    )
                )

            except ValueError:

                self.day = None

        self.day_high = decimal_value(
            self.state.get(
                "day_high"
            )
        )

        self.day_low = decimal_value(
            self.state.get(
                "day_low"
            )
        )

        self.range_ready = bool(
            self.state.get(
                "range_ready",
                False
            )
        )

        self.current_sl = decimal_value(
            self.state.get(
                "current_sl"
            )
        )

        self.manual_flat = bool(
            self.state.get(
                "manual_flat",
                False
            )
        )

        self.manual_reference_high = (
            decimal_value(
                self.state.get(
                    "manual_reference_high"
                )
            )
        )

        self.manual_reference_low = (
            decimal_value(
                self.state.get(
                    "manual_reference_low"
                )
            )
        )

        self.trading_started = bool(
            self.state.get(
                "trading_started",
                False
            )
        )

        self.high_breakout_consumed = bool(
            self.state.get(
                "high_breakout_consumed",
                False
            )
        )

        self.low_breakout_consumed = bool(
            self.state.get(
                "low_breakout_consumed",
                False
            )
        )

        self.carried_position = bool(
            self.state.get(
                "carried_position",
                False
            )
        )

        self.overnight_sl_set = bool(
            self.state.get(
                "overnight_sl_set",
                False
            )
        )


    # ========================================================
    # SAVE
    # ========================================================

    def persist(
        self
    ):

        save_state(
            {
                "day": (
                    self.day.isoformat()
                    if self.day
                    else None
                ),

                "day_high": (
                    str(
                        self.day_high
                    )
                    if self.day_high is not None
                    else None
                ),

                "day_low": (
                    str(
                        self.day_low
                    )
                    if self.day_low is not None
                    else None
                ),

                "range_ready":
                    self.range_ready,

                "current_sl": (
                    str(
                        self.current_sl
                    )
                    if self.current_sl is not None
                    else None
                ),

                "manual_flat":
                    self.manual_flat,

                "manual_reference_high": (
                    str(
                        self.manual_reference_high
                    )
                    if self.manual_reference_high is not None
                    else None
                ),

                "manual_reference_low": (
                    str(
                        self.manual_reference_low
                    )
                    if self.manual_reference_low is not None
                    else None
                ),

                "trading_started":
                    self.trading_started,

                "high_breakout_consumed":
                    self.high_breakout_consumed,

                "low_breakout_consumed":
                    self.low_breakout_consumed,

                "carried_position":
                    self.carried_position,

                "overnight_sl_set":
                    self.overnight_sl_set
            }
        )


    # ========================================================
    # NEW DAY
    # ========================================================

    def new_day(
        self,
        now,
        existing_position
    ):

        new_day = trading_day_start(
            now
        )

        if self.day == new_day:
            return

        logging.warning(
            "================================================"
        )

        logging.warning(
            "NEW TRADING DAY"
        )

        logging.warning(
            "DAY START = %s IST",
            new_day
        )

        logging.warning(
            "TRADING START = %s IST",
            trading_start_time(
                new_day
            )
        )

        logging.warning(
            "================================================"
        )

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # If an old position exists, the old SL belongs to the
        # previous trading day.
        #
        # We do NOT immediately create a replacement here.
        #
        # The replacement is created only after the new
        # 05:30-05:45 range is available.
        # ----------------------------------------------------

        self.day = new_day

        self.day_high = None
        self.day_low = None

        self.range_ready = False

        self.trading_started = False

        self.high_breakout_consumed = False
        self.low_breakout_consumed = False

        self.manual_flat = False

        self.manual_reference_high = None
        self.manual_reference_low = None

        if existing_position != 0:

            self.carried_position = True
            self.overnight_sl_set = False

            logging.warning(
                "POSITION CARRIED INTO NEW DAY | SIZE=%s",
                existing_position
            )

        else:

            self.carried_position = False
            self.overnight_sl_set = False

            # No position = no valid SL.
            self.current_sl = None
            self.stop_id = None

            # Remove any old orphan stop.
            try:

                cancel_all_stops(
                    self.product_id
                )

            except Exception as exc:

                logging.error(
                    "New day orphan stop cleanup failed: %s",
                    exc
                )

        self.persist()


    # ========================================================
    # BUILD INITIAL RANGE
    # ========================================================

    def build_initial_range(
        self
    ):

        if self.day is None:
            return False

        if self.range_ready:
            return True

        start = self.day

        end = trading_start_time(
            self.day
        )

        rows = candles(
            "15m",
            start,
            end + timedelta(
                seconds=1
            )
        )

        target = None

        for row in rows:

            candle_time = (
                datetime.fromtimestamp(
                    int(
                        row["time"]
                    ),
                    UTC
                )
                .astimezone(
                    IST
                )
            )

            if candle_time == start:

                target = row
                break

        if target is None:

            raise RuntimeError(
                "05:30-05:45 candle "
                "was not found."
            )

        self.day_high = Decimal(
            str(
                target["high"]
            )
        )

        self.day_low = Decimal(
            str(
                target["low"]
            )
        )

        self.range_ready = True

        self.persist()

        logging.warning(
            "================================================"
        )

        logging.warning(
            "05:45 TRADING RANGE READY"
        )

        logging.warning(
            "DAY HIGH = %s",
            self.day_high
        )

        logging.warning(
            "DAY LOW  = %s",
            self.day_low
        )

        logging.warning(
            "================================================"
        )

        return True


    # ========================================================
    # UPDATE DAY EXTREMES
    # ========================================================

    def update_day_range(
        self,
        price
    ):

        changed = False

        if (
            self.day_high is None
            or price > self.day_high
        ):

            self.day_high = price
            changed = True

        if (
            self.day_low is None
            or price < self.day_low
        ):

            self.day_low = price
            changed = True

        if changed:

            self.persist()


    # ========================================================
    # STOP SIDE
    # ========================================================

    @staticmethod
    def stop_side(
        size
    ):

        if size > 0:
            return "sell"

        if size < 0:
            return "buy"

        return None


    # ========================================================
    # STOP PRICE
    # ========================================================

    @staticmethod
    def stop_price(
        order
    ):

        for key in (
            "stop_price",
            "trigger_price",
            "stop_trigger_price"
        ):

            value = order.get(
                key
            )

            if value in (
                None,
                ""
            ):
                continue

            try:

                return Decimal(
                    str(value)
                )

            except Exception:

                pass

        return None


    # ========================================================
    # VERIFY EXACT STOP
    # ========================================================

    def find_matching_stop(
        self,
        position_size,
        sl_price
    ):

        expected_side = (
            self.stop_side(
                position_size
            )
        )

        orders = open_stops(
            self.product_id
        )

        matches = []

        for order in orders:

            side = str(
                order.get(
                    "side",
                    ""
                )
            ).lower()

            order_price = (
                self.stop_price(
                    order
                )
            )

            if (
                side == expected_side
                and order_price == sl_price
            ):

                matches.append(
                    order
                )

        return matches


    # ========================================================
    # INSTALL NEW FIXED SL
    # ========================================================

    def install_new_sl(
        self,
        position_size,
        sl_price,
        market_price
    ):

        """
        CENTRAL SL FUNCTION.

        This function MUST be used whenever a NEW SL is
        required.

        Sequence:

            OLD STOP(S)
                ↓
            CANCEL ALL
                ↓
            VERIFY ZERO
                ↓
            CREATE ONE NEW STOP
                ↓
            VERIFY NEW STOP
        """

        if position_size == 0:

            logging.error(
                "Cannot install SL with zero position."
            )

            return False

        if sl_price is None:

            logging.error(
                "Cannot install SL: price is None."
            )

            return False

        # ----------------------------------------------------
        # Validate SL side.
        # ----------------------------------------------------

        if position_size > 0:

            if sl_price >= market_price:

                logging.error(
                    "LONG SL INVALID | "
                    "SL=%s | PRICE=%s",
                    sl_price,
                    market_price
                )

                return False

        else:

            if sl_price <= market_price:

                logging.error(
                    "SHORT SL INVALID | "
                    "SL=%s | PRICE=%s",
                    sl_price,
                    market_price
                )

                return False

        # ----------------------------------------------------
        # If EXACT same SL already exists, do not recreate it.
        #
        # This prevents cancellation/recreation every 0.5 sec.
        # ----------------------------------------------------

        existing_matching = (
            self.find_matching_stop(
                position_size,
                sl_price
            )
        )

        if (
            len(existing_matching) == 1
            and self.current_sl == sl_price
        ):

            self.stop_id = (
                existing_matching[0].get(
                    "id"
                )
            )

            return True

        # ----------------------------------------------------
        # NEW SL REQUIRED
        #
        # First remove EVERY existing stop.
        # ----------------------------------------------------

        logging.warning(
            "================================================"
        )

        logging.warning(
            "REPLACING PROTECTIVE SL"
        )

        logging.warning(
            "OLD STO​​PS WILL BE DELETED FIRST"
        )

        logging.warning(
            "NEW SL = %s",
            sl_price
        )

        logging.warning(
            "================================================"
        )

        cancelled = cancel_all_stops(
            self.product_id,
            verify=True
        )

        if not cancelled:

            logging.error(
                "ABORTING NEW SL CREATION."
            )

            logging.error(
                "OLD STOP COULD NOT BE CONFIRMED REMOVED."
            )

            return False

        # ----------------------------------------------------
        # Double verification.
        # ----------------------------------------------------

        remaining = open_stops(
            self.product_id
        )

        if remaining:

            logging.error(
                "ABORT: OLD STOP STILL EXISTS."
            )

            logging.error(
                "REMAINING=%s",
                remaining
            )

            return False

        # ----------------------------------------------------
        # Create exactly ONE new stop.
        # ----------------------------------------------------

        expected_side = (
            self.stop_side(
                position_size
            )
        )

        result = stop_order(
            self.product_id,
            expected_side,
            abs(position_size),
            sl_price,
            "xsl"
            + str(
                int(
                    time.time()
                    * 1000
                )
            )
        )

        self.current_sl = sl_price
        self.stop_id = None

        result_data = result.get(
            "result",
            []
        )

        if isinstance(
            result_data,
            list
        ):

            if result_data:

                self.stop_id = (
                    result_data[0].get(
                        "id"
                    )
                )

        elif isinstance(
            result_data,
            dict
        ):

            self.stop_id = (
                result_data.get(
                    "id"
                )
            )

        # ----------------------------------------------------
        # Verify new stop exists.
        # ----------------------------------------------------

        time.sleep(
            STOP_CANCEL_WAIT
        )

        new_stops = (
            self.find_matching_stop(
                position_size,
                sl_price
            )
        )

        if len(new_stops) != 1:

            logging.error(
                "NEW SL VERIFICATION FAILED."
            )

            logging.error(
                "EXPECTED ONE STOP | FOUND %s",
                len(new_stops)
            )

            return False

        self.stop_id = (
            new_stops[0].get(
                "id"
            )
        )

        self.current_sl = sl_price

        self.persist()

        logging.warning(
            "================================================"
        )

        logging.warning(
            "NEW FIXED SL ACTIVE"
        )

        logging.warning(
            "POSITION = %s",
            (
                "LONG"
                if position_size > 0
                else "SHORT"
            )
        )

        logging.warning(
            "SL = %s",
            sl_price
        )

        logging.warning(
            "OLD SL = DELETED"
        )

        logging.warning(
            "ACTIVE STOP COUNT = 1"
        )

        logging.warning(
            "SL WILL NOT TRAIL"
        )

        logging.warning(
            "================================================"
        )

        return True


    # ========================================================
    # ENTRY
    # ========================================================

    def enter(
        self,
        direction,
        price,
        sl_price,
        reason
    ):

        if self.entry_lock:
            return False

        if weekend_block():
            return False

        if sl_price is None:
            return False

        position = get_position(
            self.product_id
        )

        if position["size"] != 0:

            self.last_position = (
                position["size"]
            )

            return False

        # ----------------------------------------------------
        # Validate SL.
        # ----------------------------------------------------

        if direction == "LONG":

            if sl_price >= price:

                logging.error(
                    "LONG ENTRY BLOCKED | "
                    "SL=%s >= PRICE=%s",
                    sl_price,
                    price
                )

                return False

        else:

            if sl_price <= price:

                logging.error(
                    "SHORT ENTRY BLOCKED | "
                    "SL=%s <= PRICE=%s",
                    sl_price,
                    price
                )

                return False

        size, balance, margin, notional = (
            contract_size(
                self.product,
                price
            )
        )

        side = (
            "buy"
            if direction == "LONG"
            else "sell"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "LIVE ENTRY = %s",
            direction
        )

        logging.warning(
            "PRICE = %s",
            price
        )

        logging.warning(
            "FIXED SL = %s",
            sl_price
        )

        logging.warning(
            "DAY HIGH = %s",
            self.day_high
        )

        logging.warning(
            "DAY LOW = %s",
            self.day_low
        )

        logging.warning(
            "SIZE = %s",
            size
        )

        logging.warning(
            "BALANCE = %s",
            balance
        )

        logging.warning(
            "MARGIN = %s",
            margin
        )

        logging.warning(
            "NOTIONAL = %s",
            notional
        )

        logging.warning(
            "REASON = %s",
            reason
        )

        logging.warning(
            "================================================"
        )

        self.entry_lock = True

        try:

            market_order(
                self.product_id,
                side,
                size,
                "xent"
                + str(
                    int(
                        time.time()
                        * 1000
                    )
                )
            )

            # ------------------------------------------------
            # Wait for fill.
            # ------------------------------------------------

            for _ in range(30):

                time.sleep(
                    0.2
                )

                position = get_position(
                    self.product_id
                )

                correct = (
                    (
                        direction == "LONG"
                        and position["size"] > 0
                    )
                    or
                    (
                        direction == "SHORT"
                        and position["size"] < 0
                    )
                )

                if correct:

                    actual_size = (
                        position["size"]
                    )

                    self.last_position = (
                        actual_size
                    )

                    self.manual_flat = False

                    self.carried_position = False

                    self.overnight_sl_set = False

                    self.current_sl = None
                    self.stop_id = None

                    self.persist()

                    # ------------------------------------------------
                    # CRITICAL:
                    #
                    # Every new position gets its SL through the
                    # central install_new_sl() function.
                    #
                    # Therefore any old/orphan SL is deleted first.
                    # ------------------------------------------------

                    actual_price = get_price()

                    success = (
                        self.install_new_sl(
                            actual_size,
                            sl_price,
                            actual_price
                        )
                    )

                    if not success:

                        raise RuntimeError(
                            "Position opened but "
                            "new protective SL "
                            "could not be verified."
                        )

                    return True

            raise RuntimeError(
                "Entry sent but fill "
                "was not confirmed."
            )

        finally:

            self.entry_lock = False


    # ========================================================
    # FIRST BREAKOUT
    # ========================================================

    def opening_breakout(
        self,
        price
    ):

        if not self.range_ready:
            return False

        if self.day_high is None:
            return False

        if self.day_low is None:
            return False

        # ----------------------------------------------------
        # HIGH BREAK -> LONG
        # ----------------------------------------------------

        if (
            not self.high_breakout_consumed
            and price > self.day_high
        ):

            sl = self.day_low

            success = self.enter(
                "LONG",
                price,
                sl,
                "DAY HIGH BREAKOUT"
            )

            if success:

                self.high_breakout_consumed = True
                self.trading_started = True

                self.persist()

            return success

        # ----------------------------------------------------
        # LOW BREAK -> SHORT
        # ----------------------------------------------------

        if (
            not self.low_breakout_consumed
            and price < self.day_low
        ):

            sl = self.day_high

            success = self.enter(
                "SHORT",
                price,
                sl,
                "DAY LOW BREAKOUT"
            )

            if success:

                self.low_breakout_consumed = True
                self.trading_started = True

                self.persist()

            return success

        return False


    # ========================================================
    # FLAT BREAKOUT
    # ========================================================

    def flat_breakout(
        self,
        price
    ):

        if not self.range_ready:
            return False

        # ----------------------------------------------------
        # MANUAL CLOSE MODE
        # ----------------------------------------------------

        if self.manual_flat:

            reference_high = (
                self.manual_reference_high
                if self.manual_reference_high is not None
                else self.day_high
            )

            reference_low = (
                self.manual_reference_low
                if self.manual_reference_low is not None
                else self.day_low
            )

            # NEW HIGH
            if (
                reference_high is not None
                and price > reference_high
            ):

                sl = self.day_low

                success = self.enter(
                    "LONG",
                    price,
                    sl,
                    "MANUAL CLOSE -> NEW DAY HIGH"
                )

                if success:

                    self.manual_flat = False

                    self.high_breakout_consumed = True

                    self.manual_reference_high = None
                    self.manual_reference_low = None

                    self.persist()

                return success

            # NEW LOW
            if (
                reference_low is not None
                and price < reference_low
            ):

                sl = self.day_high

                success = self.enter(
                    "SHORT",
                    price,
                    sl,
                    "MANUAL CLOSE -> NEW DAY LOW"
                )

                if success:

                    self.manual_flat = False

                    self.low_breakout_consumed = True

                    self.manual_reference_high = None
                    self.manual_reference_low = None

                    self.persist()

                return success

            return False

        # ----------------------------------------------------
        # NORMAL FLAT MODE
        # ----------------------------------------------------

        if (
            not self.high_breakout_consumed
            and self.day_high is not None
            and price > self.day_high
        ):

            sl = self.day_low

            success = self.enter(
                "LONG",
                price,
                sl,
                "NEW DAY HIGH BREAKOUT"
            )

            if success:

                self.high_breakout_consumed = True

                self.persist()

            return success

        if (
            not self.low_breakout_consumed
            and self.day_low is not None
            and price < self.day_low
        ):

            sl = self.day_high

            success = self.enter(
                "SHORT",
                price,
                sl,
                "NEW DAY LOW BREAKOUT"
            )

            if success:

                self.low_breakout_consumed = True

                self.persist()

            return success

        return False


    # ========================================================
    # CLOSE REASON
    # ========================================================

    def detect_close_reason(
        self,
        old_size,
        price
    ):

        if old_size == 0:
            return "none"

        if self.current_sl is not None:

            if (
                old_size > 0
                and price <= self.current_sl
            ):

                return "sl"

            if (
                old_size < 0
                and price >= self.current_sl
            ):

                return "sl"

        return "manual"


    # ========================================================
    # HANDLE CLOSED POSITION
    # ========================================================

    def handle_closed_position(
        self,
        old_size,
        price
    ):

        old_sl = self.current_sl

        reason = (
            self.detect_close_reason(
                old_size,
                price
            )
        )

        reference_high = self.day_high
        reference_low = self.day_low

        logging.warning(
            "================================================"
        )

        logging.warning(
            "POSITION CLOSED"
        )

        logging.warning(
            "OLD POSITION = %s",
            (
                "LONG"
                if old_size > 0
                else "SHORT"
            )
        )

        logging.warning(
            "OLD SL = %s",
            old_sl
        )

        logging.warning(
            "CLOSE REASON = %s",
            reason
        )

        logging.warning(
            "================================================"
        )

        # ----------------------------------------------------
        # ALWAYS DELETE THE OLD SL.
        #
        # This is now mandatory for BOTH:
        #
        #   manual close
        #   SL reversal
        # ----------------------------------------------------

        try:

            cancelled = (
                cancel_all_stops(
                    self.product_id,
                    verify=True
                )
            )

            if not cancelled:

                logging.error(
                    "OLD SL COULD NOT BE CONFIRMED REMOVED."
                )

        except Exception as exc:

            logging.error(
                "Stop cleanup after close failed: %s",
                exc
            )

        self.current_sl = None
        self.stop_id = None
        self.last_position = 0

        # ====================================================
        # MANUAL CLOSE
        # ====================================================

        if reason == "manual":

            logging.warning(
                "MANUAL CLOSE DETECTED"
            )

            logging.warning(
                "NO IMMEDIATE RE-ENTRY"
            )

            logging.warning(
                "WAITING FOR NEW HIGH / NEW LOW"
            )

            self.manual_flat = True

            self.manual_reference_high = (
                reference_high
            )

            self.manual_reference_low = (
                reference_low
            )

            self.carried_position = False
            self.overnight_sl_set = False

            self.persist()

            return


        # ====================================================
        # SL HIT -> REVERSE
        # ====================================================

        logging.warning(
            "================================================"
        )

        logging.warning(
            "FIXED SL HIT"
        )

        logging.warning(
            "REVERSING POSITION"
        )

        logging.warning(
            "OLD SL = %s",
            old_sl
        )

        logging.warning(
            "================================================"
        )

        self.manual_flat = False
        self.carried_position = False
        self.overnight_sl_set = False

        # ----------------------------------------------------
        # Update day extremes BEFORE calculating the opposite
        # SL.
        #
        # The running day high/low continues to update while a
        # position is open, but current_sl never follows it.
        # ----------------------------------------------------

        self.update_day_range(
            price
        )

        # ----------------------------------------------------
        # LONG -> SHORT
        # New SHORT SL = current day HIGH
        # ----------------------------------------------------

        if old_size > 0:

            new_sl = self.day_high

            if new_sl is None:

                logging.error(
                    "Cannot reverse LONG -> SHORT: "
                    "day high unavailable."
                )

                return

            self.enter(
                "SHORT",
                price,
                new_sl,
                "LONG FIXED SL HIT -> REVERSE SHORT"
            )

            return

        # ----------------------------------------------------
        # SHORT -> LONG
        # New LONG SL = current day LOW
        # ----------------------------------------------------

        new_sl = self.day_low

        if new_sl is None:

            logging.error(
                "Cannot reverse SHORT -> LONG: "
                "day low unavailable."
            )

            return

        self.enter(
            "LONG",
            price,
            new_sl,
            "SHORT FIXED SL HIT -> REVERSE LONG"
        )


    # ========================================================
    # OVERNIGHT SL
    # ========================================================

    def apply_overnight_sl(
        self,
        position_size,
        price
    ):

        if not self.carried_position:
            return False

        if self.overnight_sl_set:
            return False

        if not self.range_ready:
            return False

        # ----------------------------------------------------
        # NEW DAY SL
        # ----------------------------------------------------

        if position_size > 0:

            new_sl = self.day_low
            direction = "LONG"

        else:

            new_sl = self.day_high
            direction = "SHORT"

        if new_sl is None:
            return False

        # ----------------------------------------------------
        # Exact strategy level must be valid.
        # We do NOT move it to another level.
        # ----------------------------------------------------

        if (
            position_size > 0
            and new_sl >= price
        ):

            logging.error(
                "OVERNIGHT LONG SL INVALID | "
                "NEW DAY LOW=%s | PRICE=%s",
                new_sl,
                price
            )

            return False

        if (
            position_size < 0
            and new_sl <= price
        ):

            logging.error(
                "OVERNIGHT SHORT SL INVALID | "
                "NEW DAY HIGH=%s | PRICE=%s",
                new_sl,
                price
            )

            return False

        logging.warning(
            "================================================"
        )

        logging.warning(
            "NEW TRADING DAY -> REPLACING OLD SL"
        )

        logging.warning(
            "POSITION = %s",
            direction
        )

        logging.warning(
            "NEW DAY HIGH = %s",
            self.day_high
        )

        logging.warning(
            "NEW DAY LOW = %s",
            self.day_low
        )

        logging.warning(
            "NEW FIXED SL = %s",
            new_sl
        )

        logging.warning(
            "================================================"
        )

        # ----------------------------------------------------
        # THIS DELETES YESTERDAY'S SL FIRST.
        # ----------------------------------------------------

        success = (
            self.install_new_sl(
                position_size,
                new_sl,
                price
            )
        )

        if not success:

            logging.error(
                "OVERNIGHT SL REPLACEMENT FAILED."
            )

            return False

        self.current_sl = new_sl
        self.overnight_sl_set = True

        self.persist()

        logging.warning(
            "OVERNIGHT SL REPLACED SUCCESSFULLY"
        )

        return True


    # ========================================================
    # RUN ONE LOOP
    # ========================================================

    def run_once(
        self
    ):

        now = now_ist()

        # ----------------------------------------------------
        # POSITION BEFORE DAY CHANGE
        # ----------------------------------------------------

        position_before = (
            get_position(
                self.product_id
            )
        )

        size_before = (
            position_before["size"]
        )

        # ----------------------------------------------------
        # NEW DAY
        # ----------------------------------------------------

        self.new_day(
            now,
            size_before
        )

        # ----------------------------------------------------
        # PRICE
        # ----------------------------------------------------

        price = get_price()

        # ----------------------------------------------------
        # SATURDAY SQUARE OFF
        # ----------------------------------------------------

        if force_squareoff(
            now
        ):

            position = get_position(
                self.product_id
            )

            size = position["size"]

            if size != 0:

                logging.warning(
                    "================================================"
                )

                logging.warning(
                    "WEEKEND SQUARE OFF"
                )

                logging.warning(
                    "POSITION SIZE = %s",
                    size
                )

                logging.warning(
                    "================================================"
                )

                try:

                    cancel_all_stops(
                        self.product_id,
                        verify=True
                    )

                except Exception:
                    pass

                market_order(
                    self.product_id,
                    (
                        "sell"
                        if size > 0
                        else "buy"
                    ),
                    abs(size),
                    "xoff"
                    + str(
                        int(
                            time.time()
                            * 1000
                        )
                    )
                )

                self.current_sl = None
                self.stop_id = None
                self.last_position = 0
                self.carried_position = False

                self.persist()

            return

        # ----------------------------------------------------
        # WEEKEND BLOCK
        # ----------------------------------------------------

        if weekend_block(
            now
        ):

            return

        # ----------------------------------------------------
        # CURRENT POSITION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        new_size = position["size"]

        old_size = self.last_position

        # ====================================================
        # POSITION OPEN
        # ====================================================

        if new_size != 0:

            # ------------------------------------------------
            # POSITION JUST APPEARED
            # ------------------------------------------------

            if (
                old_size == 0
                and new_size != 0
            ):

                logging.warning(
                    "OPEN POSITION DETECTED | SIZE=%s",
                    new_size
                )

                self.last_position = (
                    new_size
                )

                # If current_sl exists, preserve it.
                #
                # If no SL exists, do not invent one here.
                # The normal entry/reversal/overnight logic
                # must establish it.
                if self.current_sl is not None:

                    try:

                        self.install_new_sl(
                            new_size,
                            self.current_sl,
                            price
                        )

                    except Exception as exc:

                        logging.error(
                            "Existing SL verification failed: %s",
                            exc
                        )

                self.persist()

                return

            # ------------------------------------------------
            # Build today's range after 05:45.
            # ------------------------------------------------

            if (
                not self.range_ready
                and now >= trading_start_time(
                    self.day
                )
            ):

                self.build_initial_range()

            # ------------------------------------------------
            # OVERNIGHT POSITION
            # ------------------------------------------------

            if (
                self.carried_position
                and not self.overnight_sl_set
                and now >= trading_start_time(
                    self.day
                )
            ):

                self.apply_overnight_sl(
                    new_size,
                    price
                )

                self.last_position = (
                    new_size
                )

                return

            # ------------------------------------------------
            # IMPORTANT:
            #
            # UPDATE DAY HIGH/LOW WHILE POSITION IS RUNNING.
            #
            # BUT NEVER UPDATE current_sl.
            #
            # This allows a reversal to use the true current
            # day extreme.
            # ------------------------------------------------

            if (
                self.range_ready
                and now >= trading_start_time(
                    self.day
                )
            ):

                self.update_day_range(
                    price
                )

            self.last_position = (
                new_size
            )

            # ------------------------------------------------
            # VERIFY SAME FIXED SL.
            #
            # If it exists, leave it alone.
            #
            # If exchange lost it, recreate the SAME SL.
            # It will not trail.
            # ------------------------------------------------

            if self.current_sl is not None:

                try:

                    matches = (
                        self.find_matching_stop(
                            new_size,
                            self.current_sl
                        )
                    )

                    if len(matches) == 1:

                        self.stop_id = (
                            matches[0].get(
                                "id"
                            )
                        )

                    elif len(matches) == 0:

                        logging.error(
                            "PROTECTIVE SL MISSING!"
                        )

                        logging.warning(
                            "RECREATING SAME FIXED SL = %s",
                            self.current_sl
                        )

                        self.install_new_sl(
                            new_size,
                            self.current_sl,
                            price
                        )

                    else:

                        logging.warning(
                            "DUPLICATE SL DETECTED."
                        )

                        self.install_new_sl(
                            new_size,
                            self.current_sl,
                            price
                        )

                except Exception as exc:

                    logging.error(
                        "SL verification error: %s",
                        exc
                    )

            return

        # ====================================================
        # FLAT
        # ====================================================

        self.last_position = 0

        # ----------------------------------------------------
        # POSITION JUST CLOSED
        # ----------------------------------------------------

        if old_size != 0:

            self.handle_closed_position(
                old_size,
                price
            )

            return

        # ----------------------------------------------------
        # FLAT = NO SL
        # ----------------------------------------------------

        self.current_sl = None
        self.stop_id = None

        # Remove orphan stops.
        try:

            stops = open_stops(
                self.product_id
            )

            if stops:

                logging.warning(
                    "FLAT POSITION WITH STOP(S) FOUND."
                )

                cancel_all_stops(
                    self.product_id,
                    verify=True
                )

        except Exception as exc:

            logging.error(
                "Orphan stop cleanup: %s",
                exc
            )

        # ----------------------------------------------------
        # BEFORE 05:45
        # ----------------------------------------------------

        if now < trading_start_time(
            self.day
        ):

            return

        # ----------------------------------------------------
        # BUILD 05:30-05:45 RANGE
        # ----------------------------------------------------

        if not self.range_ready:

            self.build_initial_range()

        self.trading_started = True

        # ----------------------------------------------------
        # CHECK BREAKOUT BEFORE UPDATING RANGE
        # ----------------------------------------------------

        triggered = (
            self.flat_breakout(
                price
            )
        )

        if triggered:

            return

        # ----------------------------------------------------
        # UPDATE CURRENT DAY EXTREMES
        # ----------------------------------------------------

        self.update_day_range(
            price
        )


    # ========================================================
    # RUN
    # ========================================================

    def run(
        self
    ):

        logging.warning(
            "================================================"
        )

        logging.warning(
            "XAUTUSD FIXED-SL BREAKOUT BOT STARTING"
        )

        logging.warning(
            "STRATEGY VERSION 11.0"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "DAY START      = 05:30 IST"
        )

        logging.warning(
            "TRADING START  = 05:45 IST"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "HIGH BREAK -> LONG"
        )

        logging.warning(
            "LOW BREAK  -> SHORT"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "LONG SL  = DAY LOW AT ENTRY"
        )

        logging.warning(
            "SHORT SL = DAY HIGH AT ENTRY"
        )

        logging.warning(
            "SL NEVER TRAILS"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "NEW SL RULE:"
        )

        logging.warning(
            "DELETE ALL OLD STOPS"
        )

        logging.warning(
            "VERIFY ZERO OLD STOPS"
        )

        logging.warning(
            "PLACE EXACTLY ONE NEW STOP"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "LONG SL HIT  -> SHORT"
        )

        logging.warning(
            "SHORT SL HIT -> LONG"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "SATURDAY/SUNDAY = NO TRADING"
        )

        logging.warning(
            "SATURDAY 05:00 = SQUARE OFF"
        )

        logging.warning(
            "================================================"
        )

        set_leverage(
            self.product_id
        )

        # ----------------------------------------------------
        # STARTUP
        # ----------------------------------------------------

        now = now_ist()

        position = get_position(
            self.product_id
        )

        startup_size = (
            position["size"]
        )

        self.new_day(
            now,
            startup_size
        )

        # ----------------------------------------------------
        # WEEKEND
        # ----------------------------------------------------

        if weekend_block(
            now
        ):

            logging.warning(
                "BOT STARTED DURING WEEKEND"
            )

            logging.warning(
                "NO TRADING WILL OCCUR."
            )

        # ----------------------------------------------------
        # STARTUP WITH POSITION
        # ----------------------------------------------------

        if startup_size != 0:

            logging.warning(
                "================================================"
            )

            logging.warning(
                "STARTED WITH OPEN POSITION"
            )

            logging.warning(
                "SIZE = %s",
                startup_size
            )

            logging.warning(
                "================================================"
            )

            self.last_position = (
                startup_size
            )

            self.carried_position = True

            self.overnight_sl_set = False

            # ------------------------------------------------
            # If after 05:45, establish TODAY'S range.
            #
            # This is critical when bot is started at 06:30.
            #
            # It does NOT use an old 05:30 candle high as the
            # position's SL.
            # ------------------------------------------------

            if (
                now >= trading_start_time(
                    self.day
                )
                and not weekend_block(
                    now
                )
            ):

                try:

                    self.build_initial_range()

                    price = get_price()

                    self.apply_overnight_sl(
                        startup_size,
                        price
                    )

                except Exception as exc:

                    logging.error(
                        "Startup position SL setup failed: %s",
                        exc
                    )

            self.persist()

        # ----------------------------------------------------
        # STARTUP FLAT
        # ----------------------------------------------------

        else:

            logging.warning(
                "STARTED FLAT"
            )

            self.last_position = 0

            self.current_sl = None
            self.stop_id = None

            if (
                now >= trading_start_time(
                    self.day
                )
                and not weekend_block(
                    now
                )
            ):

                try:

                    self.build_initial_range()

                except Exception as exc:

                    logging.error(
                        "Startup range setup failed: %s",
                        exc
                    )

            # ------------------------------------------------
            # Remove ALL old stops.
            # ------------------------------------------------

            try:

                cancel_all_stops(
                    self.product_id,
                    verify=True
                )

            except Exception as exc:

                logging.error(
                    "Startup stop cleanup failed: %s",
                    exc
                )

        # ----------------------------------------------------
        # MAIN LOOP
        # ----------------------------------------------------

        while True:

            try:

                self.run_once()

            except KeyboardInterrupt:

                logging.warning(
                    "Bot stopped by user."
                )

                break

            except Exception as exc:

                logging.exception(
                    "BOT ERROR: %s",
                    exc
                )

                time.sleep(
                    3
                )

            time.sleep(
                POLL_SECONDS
            )


# ============================================================
# MAIN
# ============================================================

def main():

    logging.info(
        "Connecting to Delta India"
    )

    logging.info(
        "SYMBOL=%s",
        SYMBOL
    )

    product = get_product()

    Strategy(
        product
    ).run()


if __name__ == "__main__":

    main()
