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
# DAY RANGE
# ------------------------------------------------------------
# From 05:30 onward:
#
#   DAY HIGH = highest price of current trading day
#   DAY LOW  = lowest price of current trading day
#
# At 05:45 trading begins.
#
# ENTRY
# ------------------------------------------------------------
# If flat:
#
#   price > DAY HIGH -> LONG
#   price < DAY LOW  -> SHORT
#
# IMPORTANT:
# Breakout is checked BEFORE updating the day range.
#
# FIXED STOP
# ------------------------------------------------------------
# LONG:
#   SL = DAY LOW at entry
#
# SHORT:
#   SL = DAY HIGH at entry
#
# Once entered:
#
#   SL NEVER MOVES.
#
# Day high/low may continue changing, but current_sl
# remains exactly where it was placed.
#
# STOP LOSS REVERSAL
# ------------------------------------------------------------
# LONG SL HIT:
#   close LONG
#   open SHORT
#   SHORT SL = CURRENT DAY HIGH
#
# SHORT SL HIT:
#   close SHORT
#   open LONG
#   LONG SL = CURRENT DAY LOW
#
# The old SL is ALWAYS removed before the new SL is created.
#
# OVERNIGHT
# ------------------------------------------------------------
# If a position survives into a new trading day:
#
#   NO NEW ENTRY.
#
# At/after 05:45:
#
#   delete old SL
#   calculate new day's HIGH/LOW
#   LONG  -> new SL = new DAY LOW
#   SHORT -> new SL = new DAY HIGH
#
# Then the new SL remains FIXED for that position.
#
# MANUAL CLOSE
# ------------------------------------------------------------
# Manual close:
#
#   no immediate re-entry at the same level.
#
# The bot waits for a genuinely new HIGH or LOW.
#
# WEEKEND
# ------------------------------------------------------------
# Saturday 05:00 IST:
#   square off
#
# Saturday:
#   no trading
#
# Sunday:
#   no trading
#
# Monday:
#   05:30 new trading day
#   05:45 trading begins
#
# POSITION SIZE
# ------------------------------------------------------------
# 10% balance as margin
# 50x leverage
#
# ORDER
# ------------------------------------------------------------
# Market order for entries/reversals.
#
# Exactly ONE protective stop for an open position.
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
            "XAUTUSD-Fixed-SL-Breakout-Bot/12.0"
        )
    }
)


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

        boundary -= timedelta(
            days=1
        )

    return boundary


def trading_start(day):

    return day + timedelta(
        minutes=15
    )


def weekend_block(dt=None):

    dt = dt or now_ist()

    # Saturday from 05:00 onward.
    if (
        dt.weekday() == 5
        and dt.time() >= datetime.strptime(
            "05:00",
            "%H:%M"
        ).time()
    ):
        return True

    # Entire Sunday.
    if dt.weekday() == 6:
        return True

    # Monday before 05:45.
    if (
        dt.weekday() == 0
        and dt < trading_start(
            trading_day_start(dt)
        )
    ):
        return True

    return False


def force_squareoff(dt=None):

    dt = dt or now_ist()

    return (
        dt.weekday() == 5
        and dt.hour == 5
        and dt.minute < 5
    )


# ============================================================
# AUTH
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

    if data.get("success") is False:

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

    raw = (
        ticker.get("close")
        or ticker.get("last_price")
        or ticker.get("mark_price")
    )

    if raw is None:

        raise RuntimeError(
            "Ticker returned no price."
        )

    return Decimal(
        str(raw)
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
                "Order %s already gone.",
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
# DELETE EVERY STOP
# ============================================================

def cancel_all_stops(
    product_id
):

    orders = open_stops(
        product_id
    )

    if not orders:

        return


    logging.warning(
        "REMOVING %s EXISTING STOP ORDER(S)",
        len(orders)
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
                "FAILED TO CANCEL STOP %s: %s",
                order_id,
                exc
            )


# ============================================================
# WAIT UNTIL ALL STOPS ARE GONE
# ============================================================

def wait_until_no_stops(
    product_id,
    timeout=8
):

    deadline = (
        time.time()
        + timeout
    )

    while time.time() < deadline:

        orders = open_stops(
            product_id
        )

        if not orders:

            return True

        # Try cancellation again.
        for order in orders:

            order_id = order.get(
                "id"
            )

            if order_id:

                try:

                    cancel_order(
                        order_id
                    )

                except Exception as exc:

                    logging.error(
                        "Retry cancel failed "
                        "for stop %s: %s",
                        order_id,
                        exc
                    )

        time.sleep(
            0.25
        )

    remaining = open_stops(
        product_id
    )

    if remaining:

        logging.error(
            "STOP CLEANUP FAILED. "
            "%s STOP(S) STILL OPEN.",
            len(remaining)
        )

        for order in remaining:

            logging.error(
                "REMAINING STOP: %s",
                order
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

        # THIS IS THE SL FOR THE CURRENT POSITION.
        #
        # It is NEVER recalculated while the same position
        # remains open.
        #
        self.current_sl = None
        self.stop_id = None

        # ----------------------------------------------------
        # MANUAL CLOSE
        # ----------------------------------------------------

        self.manual_flat = False

        self.manual_reference_high = None
        self.manual_reference_low = None

        # ----------------------------------------------------
        # OVERNIGHT
        # ----------------------------------------------------

        self.carried_position = False
        self.overnight_sl_set = False

        # ----------------------------------------------------
        # ENTRY LOCK
        # ----------------------------------------------------

        self.entry_lock = False

        # ----------------------------------------------------
        # STATE
        # ----------------------------------------------------

        self.state = load_state()

        self.restore_state()


    # ========================================================
    # RESTORE
    # ========================================================

    def restore_state(self):

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

    def persist(self):

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
            return False

        logging.warning(
            "================================================"
        )

        logging.warning(
            "NEW TRADING DAY"
        )

        logging.warning(
            "DAY START     = %s",
            new_day
        )

        logging.warning(
            "TRADING START = %s",
            trading_start(
                new_day
            )
        )

        logging.warning(
            "================================================"
        )

        self.day = new_day

        self.day_high = None
        self.day_low = None
        self.range_ready = False

        self.manual_flat = False
        self.manual_reference_high = None
        self.manual_reference_low = None

        # ----------------------------------------------------
        # POSITION CARRIED
        # ----------------------------------------------------

        if existing_position != 0:

            self.carried_position = True

            self.overnight_sl_set = False

            # VERY IMPORTANT:
            #
            # The old day's SL must NOT remain.
            #
            # We clear it from state and physically remove
            # every exchange stop.
            #
            self.current_sl = None
            self.stop_id = None

            logging.warning(
                "POSITION CARRIED INTO NEW DAY | SIZE=%s",
                existing_position
            )

            try:

                cancel_all_stops(
                    self.product_id
                )

                if not wait_until_no_stops(
                    self.product_id
                ):

                    logging.error(
                        "Could not fully remove "
                        "old day's SL."
                    )

            except Exception as exc:

                logging.error(
                    "Old SL cleanup failed: %s",
                    exc
                )

        else:

            self.carried_position = False
            self.overnight_sl_set = False

            self.current_sl = None
            self.stop_id = None

            try:

                cancel_all_stops(
                    self.product_id
                )

            except Exception as exc:

                logging.error(
                    "New-day flat stop cleanup failed: %s",
                    exc
                )

        self.persist()

        return True


    # ========================================================
    # BUILD CURRENT DAY RANGE
    # ========================================================

    def build_day_range(
        self,
        now=None
    ):

        now = now or now_ist()

        if self.day is None:
            return False

        start = self.day

        # We only build the trading range once trading has
        # started.
        end = min(
            now,
            trading_start(
                self.day
            )
            + timedelta(
                seconds=1
            )
        )

        # Before 05:45 we cannot build a completed trading
        # range.
        if now < trading_start(
            self.day
        ):

            return False

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # At startup later in the day we MUST calculate the
        # WHOLE CURRENT DAY range.
        #
        # This is what prevents the old 05:30-candle-only bug.
        # ----------------------------------------------------

        end = now

        rows = candles(
            "15m",
            start,
            end + timedelta(
                seconds=1
            )
        )

        high = None
        low = None

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

            if candle_time < start:
                continue

            candle_high = Decimal(
                str(
                    row["high"]
                )
            )

            candle_low = Decimal(
                str(
                    row["low"]
                )
            )

            if (
                high is None
                or candle_high > high
            ):

                high = candle_high

            if (
                low is None
                or candle_low < low
            ):

                low = candle_low

        # Include live price as well.
        price = get_price()

        if (
            high is None
            or price > high
        ):

            high = price

        if (
            low is None
            or price < low
        ):

            low = price

        if (
            high is None
            or low is None
        ):

            raise RuntimeError(
                "Could not calculate "
                "current trading-day range."
            )

        self.day_high = high
        self.day_low = low
        self.range_ready = True

        self.persist()

        logging.warning(
            "================================================"
        )

        logging.warning(
            "CURRENT DAY RANGE"
        )

        logging.warning(
            "DAY = %s",
            self.day
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
            "================================================"
        )

        return True


    # ========================================================
    # UPDATE DAY EXTREMES
    # ========================================================

    def update_day_extremes(
        self,
        price
    ):

        if self.day_high is None:

            self.day_high = price

        elif price > self.day_high:

            self.day_high = price

        if self.day_low is None:

            self.day_low = price

        elif price < self.day_low:

            self.day_low = price

        self.persist()


    # ========================================================
    # STOP SIDE
    # ========================================================

    @staticmethod
    def stop_side(
        position_size
    ):

        if position_size > 0:
            return "sell"

        if position_size < 0:
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
    # CREATE EXACTLY ONE STOP
    # ========================================================

    def replace_stop(
        self,
        position_size,
        sl_price,
        market_price
    ):

        if position_size == 0:
            return False

        if sl_price is None:

            raise RuntimeError(
                "SL price is None."
            )

        # ----------------------------------------------------
        # VALIDATE SL
        # ----------------------------------------------------

        if position_size > 0:

            if sl_price >= market_price:

                raise RuntimeError(
                    f"LONG SL INVALID: "
                    f"SL={sl_price}, "
                    f"PRICE={market_price}"
                )

        else:

            if sl_price <= market_price:

                raise RuntimeError(
                    f"SHORT SL INVALID: "
                    f"SL={sl_price}, "
                    f"PRICE={market_price}"
                )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "REPLACING PROTECTIVE SL"
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
            "NEW SL = %s",
            sl_price
        )

        logging.warning(
            "================================================"
        )

        # ----------------------------------------------------
        # STEP 1
        # DELETE EVERY EXISTING STOP
        # ----------------------------------------------------

        cancel_all_stops(
            self.product_id
        )

        # ----------------------------------------------------
        # STEP 2
        # WAIT UNTIL EXCHANGE CONFIRMS NONE REMAIN
        # ----------------------------------------------------

        if not wait_until_no_stops(
            self.product_id,
            timeout=10
        ):

            raise RuntimeError(
                "STOP REPLACEMENT ABORTED: "
                "old stop order(s) still exist."
            )

        # ----------------------------------------------------
        # STEP 3
        # CREATE ONE NEW STOP
        # ----------------------------------------------------

        side = self.stop_side(
            position_size
        )

        result = stop_order(
            self.product_id,
            side,
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

        order_id = None

        result_data = result.get(
            "result",
            []
        )

        if isinstance(
            result_data,
            list
        ):

            if result_data:

                order_id = (
                    result_data[0].get(
                        "id"
                    )
                )

        elif isinstance(
            result_data,
            dict
        ):

            order_id = (
                result_data.get(
                    "id"
                )
            )

        self.current_sl = sl_price
        self.stop_id = order_id

        self.persist()

        # ----------------------------------------------------
        # VERIFY EXACTLY ONE STOP
        # ----------------------------------------------------

        time.sleep(
            0.25
        )

        stops = open_stops(
            self.product_id
        )

        matching = []

        for order in stops:

            if (
                str(
                    order.get(
                        "side",
                        ""
                    )
                ).lower()
                == side
                and self.stop_price(
                    order
                )
                == sl_price
            ):

                matching.append(
                    order
                )

        # Remove any duplicate that appeared.
        if len(matching) > 1:

            logging.error(
                "DUPLICATE STOP DETECTED. "
                "CLEANING EXTRA STOPS."
            )

            for extra in matching[1:]:

                try:

                    cancel_order(
                        extra.get(
                            "id"
                        )
                    )

                except Exception as exc:

                    logging.error(
                        "Duplicate stop cleanup failed: %s",
                        exc
                    )

            matching = matching[:1]

        if len(matching) == 0:

            raise RuntimeError(
                "New protective SL was not "
                "confirmed on exchange."
            )

        self.stop_id = matching[0].get(
            "id"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "ONE PROTECTIVE SL CONFIRMED"
        )

        logging.warning(
            "SL = %s",
            sl_price
        )

        logging.warning(
            "ORDER ID = %s",
            self.stop_id
        )

        logging.warning(
            "================================================"
        )

        return True


    # ========================================================
    # ENSURE EXISTING FIXED STOP
    # ========================================================

    def ensure_fixed_stop(
        self,
        position_size,
        market_price
    ):

        if self.current_sl is None:

            raise RuntimeError(
                "Open position has no fixed SL."
            )

        side = self.stop_side(
            position_size
        )

        stops = open_stops(
            self.product_id
        )

        matching = []

        for order in stops:

            order_side = str(
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
                order_side == side
                and order_price == self.current_sl
            ):

                matching.append(
                    order
                )

            else:

                # Any stop that is not the exact current
                # protective stop is stale and MUST go.
                try:

                    cancel_order(
                        order.get(
                            "id"
                        )
                    )

                except Exception as exc:

                    logging.error(
                        "Could not remove stale stop %s: %s",
                        order.get("id"),
                        exc
                    )

        # ----------------------------------------------------
        # EXACTLY ONE CORRECT STOP
        # ----------------------------------------------------

        if len(matching) == 1:

            self.stop_id = (
                matching[0].get(
                    "id"
                )
            )

            return True

        # ----------------------------------------------------
        # ZERO OR DUPLICATES
        # ----------------------------------------------------

        if len(matching) > 1:

            for extra in matching[1:]:

                try:

                    cancel_order(
                        extra.get(
                            "id"
                        )
                    )

                except Exception:
                    pass

            matching = matching[:1]

            self.stop_id = (
                matching[0].get(
                    "id"
                )
            )

            return True

        # No correct stop exists.
        #
        # Recreate EXACTLY the stored fixed SL.
        return self.replace_stop(
            position_size,
            self.current_sl,
            market_price
        )


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
        # VALIDATE SL
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

        # ----------------------------------------------------
        # ABSOLUTELY NO OLD STOPS BEFORE ENTRY
        # ----------------------------------------------------

        cancel_all_stops(
            self.product_id
        )

        if not wait_until_no_stops(
            self.product_id
        ):

            logging.error(
                "ENTRY BLOCKED: "
                "old stop order still exists."
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
            # WAIT FOR POSITION
            # ------------------------------------------------

            for _ in range(40):

                time.sleep(
                    0.20
                )

                position = get_position(
                    self.product_id
                )

                actual_size = (
                    position["size"]
                )

                correct = (
                    (
                        direction == "LONG"
                        and actual_size > 0
                    )
                    or
                    (
                        direction == "SHORT"
                        and actual_size < 0
                    )
                )

                if correct:

                    self.last_position = (
                        actual_size
                    )

                    self.manual_flat = False

                    self.carried_position = False

                    self.overnight_sl_set = False

                    # ------------------------------------------------
                    # FIXED SL STORED ONCE.
                    # ------------------------------------------------

                    self.current_sl = (
                        sl_price
                    )

                    self.stop_id = None

                    self.persist()

                    actual_market_price = (
                        get_price()
                    )

                    # ------------------------------------------------
                    # CREATE EXACTLY ONE SL.
                    # ------------------------------------------------

                    self.replace_stop(
                        actual_size,
                        sl_price,
                        actual_market_price
                    )

                    # ------------------------------------------------
                    # NOW INCLUDE ENTRY PRICE IN DAY RANGE.
                    #
                    # This prevents a manual close at the same
                    # breakout level from immediately re-entering.
                    # ------------------------------------------------

                    self.update_day_extremes(
                        price
                    )

                    return True

            raise RuntimeError(
                "Entry sent but fill "
                "was not confirmed."
            )

        finally:

            self.entry_lock = False


    # ========================================================
    # FLAT BREAKOUT
    # ========================================================

    def flat_breakout(
        self,
        price
    ):

        if not self.range_ready:
            return False

        if (
            self.day_high is None
            or self.day_low is None
        ):

            return False

        # ----------------------------------------------------
        # MANUAL CLOSE
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

                    self.manual_reference_high = None
                    self.manual_reference_low = None

                    self.persist()

                return success

            return False

        # ----------------------------------------------------
        # NORMAL FLAT
        # ----------------------------------------------------

        # HIGH BREAK -> LONG
        if price > self.day_high:

            breakout_high = self.day_high

            sl = self.day_low

            success = self.enter(
                "LONG",
                price,
                sl,
                "DAY HIGH BREAKOUT"
            )

            if success:

                logging.warning(
                    "DAY HIGH BROKEN | "
                    "OLD HIGH=%s | ENTRY=%s | SL=%s",
                    breakout_high,
                    price,
                    sl
                )

            return success

        # LOW BREAK -> SHORT
        if price < self.day_low:

            breakout_low = self.day_low

            sl = self.day_high

            success = self.enter(
                "SHORT",
                price,
                sl,
                "DAY LOW BREAKOUT"
            )

            if success:

                logging.warning(
                    "DAY LOW BROKEN | "
                    "OLD LOW=%s | ENTRY=%s | SL=%s",
                    breakout_low,
                    price,
                    sl
                )

            return success

        return False


    # ========================================================
    # DETECT CLOSE REASON
    # ========================================================

    def detect_close_reason(
        self,
        old_size,
        price
    ):

        if old_size == 0:
            return "none"

        if self.current_sl is None:

            return "manual"

        # LONG SL
        if (
            old_size > 0
            and price <= self.current_sl
        ):

            return "sl"

        # SHORT SL
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

        reason = self.detect_close_reason(
            old_size,
            price
        )

        # Save range BEFORE clearing anything.
        current_day_high = self.day_high
        current_day_low = self.day_low

        logging.warning(
            "POSITION CLOSED | "
            "OLD SIZE=%s | "
            "OLD SL=%s | "
            "PRICE=%s | "
            "REASON=%s",
            old_size,
            old_sl,
            price,
            reason
        )

        # ----------------------------------------------------
        # REMOVE OLD SL COMPLETELY
        # ----------------------------------------------------

        cancel_all_stops(
            self.product_id
        )

        if not wait_until_no_stops(
            self.product_id
        ):

            logging.error(
                "Could not completely remove old SL."
            )

            return

        self.stop_id = None
        self.current_sl = None
        self.last_position = 0

        # ----------------------------------------------------
        # MANUAL CLOSE
        # ----------------------------------------------------

        if reason == "manual":

            logging.warning(
                "================================================"
            )

            logging.warning(
                "MANUAL CLOSE"
            )

            logging.warning(
                "NO IMMEDIATE RE-ENTRY"
            )

            logging.warning(
                "WAIT FOR NEW HIGH / NEW LOW"
            )

            logging.warning(
                "================================================"
            )

            self.manual_flat = True

            self.manual_reference_high = (
                current_day_high
            )

            self.manual_reference_low = (
                current_day_low
            )

            self.carried_position = False
            self.overnight_sl_set = False

            # Include closing price in range.
            self.update_day_extremes(
                price
            )

            self.persist()

            return


        # ----------------------------------------------------
        # SL HIT -> REVERSE
        # ----------------------------------------------------

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
            "================================================"
        )

        self.manual_flat = False
        self.carried_position = False
        self.overnight_sl_set = False

        # ----------------------------------------------------
        # LONG -> SHORT
        #
        # SHORT SL = CURRENT DAY HIGH
        # ----------------------------------------------------

        if old_size > 0:

            new_sl = current_day_high

            if new_sl is None:

                raise RuntimeError(
                    "Cannot reverse LONG -> SHORT: "
                    "DAY HIGH unavailable."
                )

            # If the day high is currently below/equal to
            # price, this would be an invalid short stop.
            #
            # In that rare case update the range with price
            # first. The stop remains the current day high.
            if new_sl <= price:

                if price > new_sl:

                    new_sl = price

                else:

                    raise RuntimeError(
                        "Invalid SHORT reversal SL."
                    )

            success = self.enter(
                "SHORT",
                price,
                new_sl,
                "LONG FIXED SL HIT -> SHORT"
            )

            if not success:

                logging.error(
                    "LONG -> SHORT reversal failed."
                )

            return


        # ----------------------------------------------------
        # SHORT -> LONG
        #
        # LONG SL = CURRENT DAY LOW
        # ----------------------------------------------------

        new_sl = current_day_low

        if new_sl is None:

            raise RuntimeError(
                "Cannot reverse SHORT -> LONG: "
                "DAY LOW unavailable."
            )

        if new_sl >= price:

            if price < new_sl:

                new_sl = price

            else:

                raise RuntimeError(
                    "Invalid LONG reversal SL."
                )

        success = self.enter(
            "LONG",
            price,
            new_sl,
            "SHORT FIXED SL HIT -> LONG"
        )

        if not success:

            logging.error(
                "SHORT -> LONG reversal failed."
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
        # NEW DAY RANGE
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
        # VALIDATE
        # ----------------------------------------------------

        if (
            position_size > 0
            and new_sl >= price
        ):

            logging.error(
                "OVERNIGHT LONG SL INVALID | "
                "DAY LOW=%s | PRICE=%s",
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
                "DAY HIGH=%s | PRICE=%s",
                new_sl,
                price
            )

            return False

        logging.warning(
            "================================================"
        )

        logging.warning(
            "NEW DAY OVERNIGHT SL"
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
        # DELETE OLD DAY'S SL + CREATE NEW DAY'S SL
        # ----------------------------------------------------

        self.replace_stop(
            position_size,
            new_sl,
            price
        )

        self.overnight_sl_set = True

        self.persist()

        return True


    # ========================================================
    # RECONCILE POSITION
    # ========================================================

    def reconcile_position(
        self,
        new_size,
        old_size,
        price,
        now
    ):

        # ----------------------------------------------------
        # POSITION IS OPEN
        # ----------------------------------------------------

        if new_size != 0:

            # ------------------------------------------------
            # POSITION CHANGED DIRECTION DIRECTLY
            #
            # This can happen if exchange state changes
            # between two polling cycles.
            # ------------------------------------------------

            if (
                old_size != 0
                and (
                    old_size > 0
                    and new_size < 0
                    or
                    old_size < 0
                    and new_size > 0
                )
            ):

                logging.warning(
                    "POSITION DIRECTION CHANGED "
                    "DIRECTLY: %s -> %s",
                    old_size,
                    new_size
                )

                # The previous SL has either executed or the
                # position was reversed externally.
                #
                # For safety, remove every stop and create the
                # correct fixed SL for the NEW position.
                #
                if new_size > 0:

                    new_sl = self.day_low

                else:

                    new_sl = self.day_high

                if new_sl is None:

                    self.build_day_range(
                        now
                    )

                    new_sl = (
                        self.day_low
                        if new_size > 0
                        else self.day_high
                    )

                self.current_sl = None
                self.stop_id = None

                self.replace_stop(
                    new_size,
                    new_sl,
                    price
                )

                self.last_position = new_size

                self.persist()

                return


            # ------------------------------------------------
            # POSITION JUST APPEARED
            # ------------------------------------------------

            if old_size == 0:

                logging.warning(
                    "OPEN POSITION DETECTED | SIZE=%s",
                    new_size
                )

                self.last_position = new_size

                # If the bot already knows the fixed SL,
                # preserve it.
                if self.current_sl is not None:

                    self.ensure_fixed_stop(
                        new_size,
                        price
                    )

                    self.persist()

                    return

                # No known SL means we must establish one.
                if not self.range_ready:

                    self.build_day_range(
                        now
                    )

                if new_size > 0:

                    sl = self.day_low

                else:

                    sl = self.day_high

                self.current_sl = sl

                self.replace_stop(
                    new_size,
                    sl,
                    price
                )

                self.persist()

                return


            # ------------------------------------------------
            # NEW DAY CARRIED POSITION
            # ------------------------------------------------

            if (
                self.carried_position
                and not self.overnight_sl_set
                and now >= trading_start(
                    self.day
                )
            ):

                if not self.range_ready:

                    self.build_day_range(
                        now
                    )

                self.apply_overnight_sl(
                    new_size,
                    price
                )

                self.last_position = new_size

                return


            # ------------------------------------------------
            # NORMAL RUNNING POSITION
            # ------------------------------------------------

            self.last_position = new_size

            # Update day high/low ONLY.
            #
            # NEVER change current_sl.
            self.update_day_extremes(
                price
            )

            # Make sure exactly one stop exists at the
            # STORED fixed SL.
            self.ensure_fixed_stop(
                new_size,
                price
            )

            return


        # ====================================================
        # POSITION FLAT
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
        # FLAT = NO STOPS
        # ----------------------------------------------------

        self.current_sl = None
        self.stop_id = None

        try:

            stops = open_stops(
                self.product_id
            )

            if stops:

                cancel_all_stops(
                    self.product_id
                )

        except Exception as exc:

            logging.error(
                "Flat stop cleanup failed: %s",
                exc
            )

        # ----------------------------------------------------
        # WEEKEND
        # ----------------------------------------------------

        if weekend_block(
            now
        ):

            return

        # ----------------------------------------------------
        # BEFORE 05:45
        # ----------------------------------------------------

        if now < trading_start(
            self.day
        ):

            return

        # ----------------------------------------------------
        # BUILD WHOLE DAY RANGE
        # ----------------------------------------------------

        if not self.range_ready:

            self.build_day_range(
                now
            )

        # ----------------------------------------------------
        # FLAT BREAKOUT
        # ----------------------------------------------------

        triggered = self.flat_breakout(
            price
        )

        if triggered:

            return

        # ----------------------------------------------------
        # UPDATE RANGE AFTER BREAKOUT CHECK
        # ----------------------------------------------------

        self.update_day_extremes(
            price
        )


    # ========================================================
    # RUN ONCE
    # ========================================================

    def run_once(self):

        now = now_ist()

        # ----------------------------------------------------
        # POSITION BEFORE DAY CHANGE
        # ----------------------------------------------------

        position_before = get_position(
            self.product_id
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
        # WEEKEND SQUARE OFF
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
                    "SIZE = %s",
                    size
                )

                logging.warning(
                    "================================================"
                )

                cancel_all_stops(
                    self.product_id
                )

                wait_until_no_stops(
                    self.product_id
                )

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

                self.last_position = 0
                self.current_sl = None
                self.stop_id = None

                self.persist()

            return

        # ----------------------------------------------------
        # WEEKEND
        # ----------------------------------------------------

        if weekend_block(
            now
        ):

            return

        # ----------------------------------------------------
        # PRICE
        # ----------------------------------------------------

        price = get_price()

        # ----------------------------------------------------
        # CURRENT POSITION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        new_size = position["size"]

        old_size = self.last_position

        # ----------------------------------------------------
        # POSITION MANAGEMENT
        # ----------------------------------------------------

        self.reconcile_position(
            new_size,
            old_size,
            price,
            now
        )


    # ========================================================
    # STARTUP
    # ========================================================

    def startup(self):

        now = now_ist()

        position = get_position(
            self.product_id
        )

        startup_size = position["size"]

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
                "BOT STARTED DURING WEEKEND."
            )

            logging.warning(
                "NO TRADING."
            )

            return

        # ----------------------------------------------------
        # OPEN POSITION AT STARTUP
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

            self.last_position = startup_size

            # ------------------------------------------------
            # If state has a fixed SL for this same trading
            # day, preserve it.
            # ------------------------------------------------

            if (
                self.current_sl is not None
                and self.day == (
                    self.state.get(
                        "day"
                    )
                    and datetime.fromisoformat(
                        self.state["day"]
                    )
                )
            ):

                try:

                    self.ensure_fixed_stop(
                        startup_size,
                        get_price()
                    )

                except Exception as exc:

                    logging.error(
                        "Startup existing-SL "
                        "reconciliation failed: %s",
                        exc
                    )

            else:

                # ------------------------------------------------
                # Position is being recognized as carried into
                # this trading day.
                # ------------------------------------------------

                self.carried_position = True
                self.overnight_sl_set = False

                self.current_sl = None
                self.stop_id = None

                cancel_all_stops(
                    self.product_id
                )

                wait_until_no_stops(
                    self.product_id
                )

                if now >= trading_start(
                    self.day
                ):

                    try:

                        self.build_day_range(
                            now
                        )

                        self.apply_overnight_sl(
                            startup_size,
                            get_price()
                        )

                    except Exception as exc:

                        logging.error(
                            "Startup carried-position "
                            "SL setup failed: %s",
                            exc
                        )

            self.persist()

            return

        # ----------------------------------------------------
        # START FLAT
        # ----------------------------------------------------

        logging.warning(
            "STARTED FLAT"
        )

        self.last_position = 0
        self.current_sl = None
        self.stop_id = None

        # Remove ALL orphan stops.
        try:

            cancel_all_stops(
                self.product_id
            )

            wait_until_no_stops(
                self.product_id
            )

        except Exception as exc:

            logging.error(
                "Startup stop cleanup failed: %s",
                exc
            )

        # ----------------------------------------------------
        # If already after 05:45, calculate WHOLE DAY range.
        # ----------------------------------------------------

        if now >= trading_start(
            self.day
        ):

            try:

                self.build_day_range(
                    now
                )

            except Exception as exc:

                logging.error(
                    "Startup range setup failed: %s",
                    exc
                )


    # ========================================================
    # RUN
    # ========================================================

    def run(self):

        logging.warning(
            "================================================"
        )

        logging.warning(
            "XAUTUSD FIXED-SL BREAKOUT BOT"
        )

        logging.warning(
            "VERSION 12.0"
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
            "DAY HIGH BREAK -> LONG"
        )

        logging.warning(
            "DAY LOW BREAK  -> SHORT"
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
            "LONG SL HIT  -> SHORT"
        )

        logging.warning(
            "SHORT SL HIT -> LONG"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "NEW DAY WITH POSITION:"
        )

        logging.warning(
            "NO NEW ENTRY"
        )

        logging.warning(
            "OLD SL DELETED"
        )

        logging.warning(
            "NEW DAY SL CREATED AFTER 05:45"
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

        self.startup()

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
        "SYMBOL = %s",
        SYMBOL
    )

    product = get_product()

    strategy = Strategy(
        product
    )

    strategy.run()


if __name__ == "__main__":

    main()
