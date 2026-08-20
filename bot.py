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
# XAUTUSD FIXED STOP-LOSS BREAKOUT BOT
# ============================================================
#
# STRATEGY
#
# Trading day:
#     05:30 IST -> next day 05:30 IST
#
# Trading starts:
#     05:45 IST
#
# At 05:45:
#     DAY HIGH BREAK -> LONG
#     DAY LOW  BREAK -> SHORT
#
# LONG:
#     SL = DAY LOW at the moment of entry
#
# SHORT:
#     SL = DAY HIGH at the moment of entry
#
# SAME DAY:
#     SL NEVER MOVES.
#
# If SL is hit:
#     LONG  -> SHORT
#     SHORT -> LONG
#
# Reverse position:
#     SHORT SL = current DAY HIGH
#     LONG SL  = current DAY LOW
#
# NEW DAY WITH OPEN POSITION:
#     NO NEW ENTRY.
#
#     Old SL is completely deleted.
#     Full new day's range is calculated.
#     New SL:
#         LONG  -> new DAY LOW
#         SHORT -> new DAY HIGH
#
# IMPORTANT:
#     Old stop orders are removed BEFORE a new stop is created.
#     The bot verifies that ZERO old stops remain.
#     Only then is the new stop created.
#
# WEEKEND:
#     Saturday/Sunday = NO TRADING
#     Saturday 05:00 = square off
#
# POSITION:
#     10% balance margin
#     50x leverage
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
# HTTP
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": (
            "XAUTUSD-Fixed-SL-Breakout-Bot/14.0"
        )
    }
)


# ============================================================
# TIME
# ============================================================

def now_ist():

    return datetime.now(
        IST
    )


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


def trading_start(
    day
):

    return (
        day
        + timedelta(
            minutes=15
        )
    )


def weekend_block(
    dt=None
):

    dt = dt or now_ist()

    # Saturday from 05:00.
    if dt.weekday() == 5:

        return (
            dt.hour >= 5
        )

    # Entire Sunday.
    if dt.weekday() == 6:

        return True

    # Monday before 05:45.
    if dt.weekday() == 0:

        if dt < trading_start(
            trading_day_start(dt)
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
        and dt.minute < 30
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
# PRICE
# ============================================================

def get_ticker():

    return api(
        "GET",
        f"/v2/tickers/{SYMBOL}"
    )["result"]


def get_price():

    ticker = get_ticker()

    value = (
        ticker.get("close")
        or ticker.get("last_price")
        or ticker.get("mark_price")
    )

    if value is None:

        raise RuntimeError(
            "Ticker returned no price."
        )

    return Decimal(
        str(value)
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
            "USD",
            "USDT"
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

def get_candles(
    start_dt,
    end_dt
):

    return api(
        "GET",
        "/v2/history/candles",
        params={
            "resolution": "15m",
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
# STOP ORDER
# ============================================================

def create_stop_order(
    product_id,
    side,
    size,
    price
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
        "stop_trigger_method": (
            "last_traded_price"
        ),
        "reduce_only": True,
        "client_order_id": (
            "xsl"
            + str(
                int(
                    time.time()
                    * 1000
                )
            )
        )[:32]
    }

    logging.warning(
        "CREATING NEW PROTECTIVE SL: %s",
        body
    )

    return api(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )


# ============================================================
# OPEN STOP ORDERS
# ============================================================

def get_open_stops(
    product_id
):

    data = api(
        "GET",
        "/v2/orders",
        params={
            "product_ids": str(
                int(product_id)
            ),
            "states": "open,pending",
            "order_types": "all_stop",
            "page_size": 100
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

        return [
            result
        ]

    return result


# ============================================================
# CANCEL SINGLE STOP
# ============================================================

def cancel_single_stop(
    product_id,
    order
):

    order_id = order.get(
        "id"
    )

    if not order_id:

        logging.error(
            "STOP HAS NO ORDER ID: %s",
            order
        )

        return False

    body = {
        "product_id": int(
            product_id
        ),
        "id": int(
            order_id
        )
    }

    logging.warning(
        "CANCELLING STOP ORDER ID=%s",
        order_id
    )

    try:

        api(
            "DELETE",
            "/v2/orders",
            body=body,
            auth=True
        )

        return True

    except Exception as exc:

        logging.error(
            "STOP CANCEL FAILED ID=%s | %s",
            order_id,
            exc
        )

        return False


# ============================================================
# CANCEL ALL STOPS
# ============================================================

def cancel_all_stops(
    product_id
):

    logging.warning(
        "================================================"
    )

    logging.warning(
        "DELETING ALL OLD STOP ORDERS"
    )

    logging.warning(
        "================================================"
    )

    # --------------------------------------------------------
    # FIRST: bulk cancel
    # --------------------------------------------------------

    try:

        api(
            "DELETE",
            "/v2/orders/all",
            body={
                "product_id": int(
                    product_id
                ),
                "cancel_limit_orders": False,
                "cancel_stop_orders": True,
                "cancel_reduce_only_orders": False
            },
            auth=True
        )

    except Exception as exc:

        logging.error(
            "BULK STOP CANCEL ERROR: %s",
            exc
        )

    time.sleep(
        0.30
    )

    # --------------------------------------------------------
    # SECOND: repeatedly check
    # --------------------------------------------------------

    for attempt in range(
        10
    ):

        stops = get_open_stops(
            product_id
        )

        if not stops:

            logging.warning(
                "ALL OLD STOPS DELETED."
            )

            return True

        logging.warning(
            "STILL %d STOP(S) ACTIVE | "
            "CLEANUP ATTEMPT %d",
            len(stops),
            attempt + 1
        )

        # ----------------------------------------------------
        # Explicit cancellation of every remaining stop.
        # ----------------------------------------------------

        for order in stops:

            cancel_single_stop(
                product_id,
                order
            )

        time.sleep(
            0.30
        )

    # --------------------------------------------------------
    # FINAL VERIFICATION
    # --------------------------------------------------------

    remaining = get_open_stops(
        product_id
    )

    if remaining:

        logging.error(
            "================================================"
        )

        logging.error(
            "STOP CLEANUP FAILED"
        )

        logging.error(
            "ACTIVE STOPS STILL EXIST: %s",
            [
                order.get(
                    "id"
                )
                for order in remaining
            ]
        )

        logging.error(
            "NEW STOP WILL NOT BE CREATED."
        )

        logging.error(
            "================================================"
        )

        return False

    logging.warning(
        "ALL OLD STOPS CONFIRMED DELETED."
    )

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

def decimal_field(
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

    contract_value = decimal_field(
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

    lot = decimal_field(
        product,
        "lot_size",
        "order_size_increment",
        default=Decimal("1")
    )

    minimum = decimal_field(
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

    temp = (
        STATE_FILE
        + ".tmp"
    )

    with open(
        temp,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            indent=2
        )

    os.replace(
        temp,
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

        self.state = load_state()

        self.day = None

        self.day_high = None
        self.day_low = None

        self.range_ready = False

        self.current_sl = None
        self.stop_id = None

        self.last_position = 0

        self.carried_position = False
        self.new_day_sl_set = False

        self.manual_flat = False

        self.high_consumed = False
        self.low_consumed = False

        self.entry_lock = False

        self.restore()


    # ========================================================
    # RESTORE
    # ========================================================

    def restore(
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

        self.carried_position = bool(
            self.state.get(
                "carried_position",
                False
            )
        )

        self.new_day_sl_set = bool(
            self.state.get(
                "new_day_sl_set",
                False
            )
        )

        self.manual_flat = bool(
            self.state.get(
                "manual_flat",
                False
            )
        )

        self.high_consumed = bool(
            self.state.get(
                "high_consumed",
                False
            )
        )

        self.low_consumed = bool(
            self.state.get(
                "low_consumed",
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

                "carried_position":
                    self.carried_position,

                "new_day_sl_set":
                    self.new_day_sl_set,

                "manual_flat":
                    self.manual_flat,

                "high_consumed":
                    self.high_consumed,

                "low_consumed":
                    self.low_consumed
            }
        )


    # ========================================================
    # NEW DAY
    # ========================================================

    def handle_new_day(
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
            "DAY START = %s IST",
            new_day
        )

        logging.warning(
            "================================================"
        )

        self.day = new_day

        self.day_high = None
        self.day_low = None

        self.range_ready = False

        self.high_consumed = False
        self.low_consumed = False

        self.manual_flat = False

        # ----------------------------------------------------
        # POSITION CARRIED
        # ----------------------------------------------------

        if existing_position != 0:

            self.carried_position = True
            self.new_day_sl_set = False

            logging.warning(
                "POSITION CARRIED INTO NEW DAY | SIZE=%s",
                existing_position
            )

            # VERY IMPORTANT:
            #
            # Old SL is deleted immediately.
            #
            # New one will be created after the new day's
            # range is available.
            #
            self.current_sl = None
            self.stop_id = None

            try:

                cancel_all_stops(
                    self.product_id
                )

            except Exception as exc:

                logging.error(
                    "OLD SL DELETE FAILED: %s",
                    exc
                )

        else:

            self.carried_position = False
            self.new_day_sl_set = False

            self.current_sl = None
            self.stop_id = None

            try:

                cancel_all_stops(
                    self.product_id
                )

            except Exception as exc:

                logging.error(
                    "OLD STOP CLEANUP FAILED: %s",
                    exc
                )

        self.persist()

        return True


    # ========================================================
    # BUILD DAY RANGE
    # ========================================================

    def build_day_range(
        self,
        now
    ):

        if self.day is None:

            return False

        if now < trading_start(
            self.day
        ):

            return False

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Get candles from 05:30 until current completed
        # candle.
        #
        # This is NOT "use only 05:30 candle".
        # ----------------------------------------------------

        start = self.day

        end = now.replace(
            second=0,
            microsecond=0
        )

        rows = get_candles(
            start,
            end
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

            # Do not use a candle that has not completed yet.
            if (
                candle_time
                + timedelta(
                    minutes=15
                )
                > now
            ):
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

        if high is None or low is None:

            logging.warning(
                "DAY RANGE NOT READY YET."
            )

            return False

        self.day_high = high
        self.day_low = low

        self.range_ready = True

        self.persist()

        logging.warning(
            "================================================"
        )

        logging.warning(
            "DAY RANGE READY"
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
    # REFRESH DAY RANGE
    # ========================================================

    def refresh_day_range(
        self,
        now
    ):

        if not self.range_ready:

            return

        if now < trading_start(
            self.day
        ):

            return

        # We maintain the day's extreme for breakout logic.
        #
        # BUT:
        # this NEVER changes current_sl while a position
        # is running.

        rows = get_candles(
            self.day,
            now
        )

        high = self.day_high
        low = self.day_low

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

            if candle_time < self.day:
                continue

            if (
                candle_time
                + timedelta(
                    minutes=15
                )
                > now
            ):
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

        changed = (
            high != self.day_high
            or low != self.day_low
        )

        if changed:

            self.day_high = high
            self.day_low = low

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
    # READ STOP PRICE
    # ========================================================

    @staticmethod
    def read_stop_price(
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
    # CREATE EXACTLY ONE SL
    # ========================================================

    def install_sl(
        self,
        position_size,
        sl_price,
        market_price
    ):

        if sl_price is None:

            logging.error(
                "SL PRICE IS NONE."
            )

            return False

        # ----------------------------------------------------
        # Validate SL
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
        # DELETE EVERYTHING FIRST
        # ----------------------------------------------------

        if not cancel_all_stops(
            self.product_id
        ):

            logging.error(
                "OLD STOP(S) STILL EXIST."
            )

            logging.error(
                "NEW STOP WILL NOT BE CREATED."
            )

            return False

        # ----------------------------------------------------
        # DOUBLE CHECK
        # ----------------------------------------------------

        remaining = get_open_stops(
            self.product_id
        )

        if remaining:

            logging.error(
                "STOP VERIFICATION FAILED."
            )

            logging.error(
                "REMAINING=%s",
                [
                    x.get("id")
                    for x in remaining
                ]
            )

            return False

        # ----------------------------------------------------
        # CREATE NEW STOP
        # ----------------------------------------------------

        side = self.stop_side(
            position_size
        )

        result = create_stop_order(
            self.product_id,
            side,
            abs(position_size),
            sl_price
        )

        result_data = result.get(
            "result",
            []
        )

        stop_id = None

        if isinstance(
            result_data,
            list
        ):

            if result_data:

                stop_id = (
                    result_data[0].get(
                        "id"
                    )
                )

        elif isinstance(
            result_data,
            dict
        ):

            stop_id = (
                result_data.get(
                    "id"
                )
            )

        # ----------------------------------------------------
        # VERIFY CREATED STOP
        # ----------------------------------------------------

        time.sleep(
            0.30
        )

        stops = get_open_stops(
            self.product_id
        )

        if len(stops) != 1:

            logging.error(
                "STOP VERIFICATION FAILED."
            )

            logging.error(
                "EXPECTED 1 STOP, FOUND %d",
                len(stops)
            )

            # Do not leave unknown duplicate stops.
            cancel_all_stops(
                self.product_id
            )

            return False

        actual = stops[0]

        actual_price = (
            self.read_stop_price(
                actual
            )
        )

        actual_side = str(
            actual.get(
                "side",
                ""
            )
        ).lower()

        if (
            actual_price != sl_price
            or actual_side != side
        ):

            logging.error(
                "WRONG STOP CREATED."
            )

            logging.error(
                "EXPECTED SIDE=%s PRICE=%s",
                side,
                sl_price
            )

            logging.error(
                "ACTUAL SIDE=%s PRICE=%s",
                actual_side,
                actual_price
            )

            cancel_all_stops(
                self.product_id
            )

            return False

        self.current_sl = sl_price
        self.stop_id = (
            stop_id
            or actual.get(
                "id"
            )
        )

        self.persist()

        logging.warning(
            "================================================"
        )

        logging.warning(
            "PROTECTIVE SL ACTIVE"
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
            "EXCHANGE ORDER ID = %s",
            self.stop_id
        )

        logging.warning(
            "EXACTLY ONE STOP CONFIRMED"
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
        # SL validation
        # ----------------------------------------------------

        if direction == "LONG":

            if sl_price >= price:

                logging.error(
                    "LONG ENTRY BLOCKED | "
                    "SL=%s PRICE=%s",
                    sl_price,
                    price
                )

                return False

        else:

            if sl_price <= price:

                logging.error(
                    "SHORT ENTRY BLOCKED | "
                    "SL=%s PRICE=%s",
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
            "NEW ENTRY = %s",
            direction
        )

        logging.warning(
            "PRICE = %s",
            price
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
            "SL = %s",
            sl_price
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

            # Clean any stale stops before entry.
            if not cancel_all_stops(
                self.product_id
            ):

                logging.error(
                    "OLD STOP CLEANUP FAILED."
                )

                return False

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
            # Wait for position fill.
            # ------------------------------------------------

            for _ in range(
                30
            ):

                time.sleep(
                    0.20
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

                    self.current_sl = (
                        sl_price
                    )

                    self.manual_flat = False
                    self.carried_position = False
                    self.new_day_sl_set = True

                    self.persist()

                    # ------------------------------------------------
                    # Install exactly one fixed SL.
                    # ------------------------------------------------

                    ok = self.install_sl(
                        actual_size,
                        sl_price,
                        get_price()
                    )

                    if not ok:

                        raise RuntimeError(
                            "Could not install "
                            "protective SL."
                        )

                    return True

            raise RuntimeError(
                "Entry sent but fill "
                "was not confirmed."
            )

        finally:

            self.entry_lock = False


    # ========================================================
    # BREAKOUT
    # ========================================================

    def breakout(
        self,
        price
    ):

        if not self.range_ready:

            return False

        # ----------------------------------------------------
        # HIGH BREAK -> LONG
        # ----------------------------------------------------

        if (
            not self.high_consumed
            and self.day_high is not None
            and price > self.day_high
        ):

            old_high = self.day_high

            # IMPORTANT:
            #
            # LONG SL is current DAY LOW.
            #

            sl = self.day_low

            if sl is None:

                return False

            success = self.enter(
                "LONG",
                price,
                sl,
                "DAY HIGH BREAKOUT"
            )

            if success:

                self.high_consumed = True

                self.persist()

                logging.warning(
                    "HIGH BREAK CONSUMED | "
                    "OLD HIGH=%s",
                    old_high
                )

            return success

        # ----------------------------------------------------
        # LOW BREAK -> SHORT
        # ----------------------------------------------------

        if (
            not self.low_consumed
            and self.day_low is not None
            and price < self.day_low
        ):

            old_low = self.day_low

            # IMPORTANT:
            #
            # SHORT SL is current DAY HIGH.
            #

            sl = self.day_high

            if sl is None:

                return False

            success = self.enter(
                "SHORT",
                price,
                sl,
                "DAY LOW BREAKOUT"
            )

            if success:

                self.low_consumed = True

                self.persist()

                logging.warning(
                    "LOW BREAK CONSUMED | "
                    "OLD LOW=%s",
                    old_low
                )

            return success

        return False


    # ========================================================
    # CLOSE REASON
    # ========================================================

    def close_reason(
        self,
        old_size,
        price
    ):

        # ----------------------------------------------------
        # If exchange position disappeared and price crossed
        # our known fixed SL, this is an SL reversal.
        # ----------------------------------------------------

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

    def handle_closed(
        self,
        old_size,
        price
    ):

        old_sl = self.current_sl

        reason = self.close_reason(
            old_size,
            price
        )

        logging.warning(
            "POSITION CLOSED | "
            "REASON=%s | OLD SL=%s",
            reason,
            old_sl
        )

        # ----------------------------------------------------
        # DELETE OLD STOP.
        # ----------------------------------------------------

        cancel_all_stops(
            self.product_id
        )

        self.current_sl = None
        self.stop_id = None
        self.last_position = 0

        # ----------------------------------------------------
        # MANUAL CLOSE
        # ----------------------------------------------------

        if reason == "manual":

            logging.warning(
                "MANUAL CLOSE."
            )

            logging.warning(
                "NO IMMEDIATE RE-ENTRY."
            )

            self.manual_flat = True

            self.carried_position = False
            self.new_day_sl_set = False

            self.persist()

            return


        # ====================================================
        # SL HIT
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
            "================================================"
        )

        self.manual_flat = False
        self.carried_position = False
        self.new_day_sl_set = True

        # ----------------------------------------------------
        # Refresh day extremes before choosing reverse SL.
        #
        # This gives the reverse position the CURRENT day
        # high/low.
        # ----------------------------------------------------

        self.refresh_day_range(
            now_ist()
        )

        # ----------------------------------------------------
        # LONG -> SHORT
        # SHORT SL = CURRENT DAY HIGH
        # ----------------------------------------------------

        if old_size > 0:

            new_sl = self.day_high

            if new_sl is None:

                logging.error(
                    "DAY HIGH unavailable "
                    "for SHORT reversal."
                )

                return

            self.enter(
                "SHORT",
                price,
                new_sl,
                "LONG SL HIT -> REVERSE SHORT"
            )

            return

        # ----------------------------------------------------
        # SHORT -> LONG
        # LONG SL = CURRENT DAY LOW
        # ----------------------------------------------------

        new_sl = self.day_low

        if new_sl is None:

            logging.error(
                "DAY LOW unavailable "
                "for LONG reversal."
            )

            return

        self.enter(
            "LONG",
            price,
            new_sl,
            "SHORT SL HIT -> REVERSE LONG"
        )


    # ========================================================
    # NEW DAY STOP
    # ========================================================

    def install_new_day_stop(
        self,
        position_size,
        price
    ):

        if not self.range_ready:

            return False

        if position_size > 0:

            new_sl = self.day_low

            direction = "LONG"

        else:

            new_sl = self.day_high

            direction = "SHORT"

        if new_sl is None:

            return False

        logging.warning(
            "================================================"
        )

        logging.warning(
            "NEW DAY POSITION STOP"
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
            "NEW SL = %s",
            new_sl
        )

        logging.warning(
            "OLD SL HAS ALREADY BEEN DELETED"
        )

        logging.warning(
            "================================================"
        )

        # ----------------------------------------------------
        # If the new day's SL has already been crossed,
        # the position is already invalid for the new day.
        # ----------------------------------------------------

        if position_size > 0:

            if new_sl >= price:

                logging.warning(
                    "NEW DAY LONG SL ALREADY HIT."
                )

                return False

        else:

            if new_sl <= price:

                logging.warning(
                    "NEW DAY SHORT SL ALREADY HIT."
                )

                return False

        ok = self.install_sl(
            position_size,
            new_sl,
            price
        )

        if ok:

            self.new_day_sl_set = True
            self.current_sl = new_sl

            self.persist()

        return ok


    # ========================================================
    # RUN ONCE
    # ========================================================

    def run_once(
        self
    ):

        now = now_ist()

        # ----------------------------------------------------
        # Position BEFORE checking new day.
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

        self.handle_new_day(
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
                    "SATURDAY 05:00 SQUARE OFF"
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
                self.new_day_sl_set = False

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
        # PRICE
        # ----------------------------------------------------

        price = get_price()

        # ----------------------------------------------------
        # BUILD NEW DAY RANGE AFTER 05:45
        # ----------------------------------------------------

        if now >= trading_start(
            self.day
        ):

            if not self.range_ready:

                self.build_day_range(
                    now
                )

            elif self.carried_position:

                # For carried position we need the new day's
                # completed range before installing its new SL.
                self.build_day_range(
                    now
                )

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
            # NEWLY DETECTED POSITION
            # ------------------------------------------------

            if old_size == 0:

                self.last_position = new_size

                # If this is a position already existing
                # outside our state, preserve current SL if
                # known.
                if self.current_sl is not None:

                    self.install_sl(
                        new_size,
                        self.current_sl,
                        price
                    )

                self.persist()

                return

            # ------------------------------------------------
            # CARRIED POSITION INTO NEW DAY
            # ------------------------------------------------

            if (
                self.carried_position
                and now >= trading_start(
                    self.day
                )
                and not self.new_day_sl_set
            ):

                ok = self.install_new_day_stop(
                    new_size,
                    price
                )

                if not ok:

                    # If new day SL is already crossed,
                    # treat it as a stop event.
                    if (
                        new_size > 0
                        and self.day_low is not None
                        and price <= self.day_low
                    ):

                        self.last_position = (
                            new_size
                        )

                        self.handle_closed(
                            new_size,
                            price
                        )

                        return

                    if (
                        new_size < 0
                        and self.day_high is not None
                        and price >= self.day_high
                    ):

                        self.last_position = (
                            new_size
                        )

                        self.handle_closed(
                            new_size,
                            price
                        )

                        return

                self.last_position = new_size

                return

            # ------------------------------------------------
            # NORMAL RUNNING POSITION
            # ------------------------------------------------

            self.last_position = new_size

            # ------------------------------------------------
            # CRITICAL:
            #
            # DO NOT recalculate current_sl from day_high/
            # day_low here.
            #
            # current_sl remains exactly where it was placed.
            #
            # Only repair the SAME stop if it disappeared.
            # ------------------------------------------------

            if self.current_sl is not None:

                stops = get_open_stops(
                    self.product_id
                )

                correct = False

                expected_side = (
                    self.stop_side(
                        new_size
                    )
                )

                for stop in stops:

                    stop_price = (
                        self.read_stop_price(
                            stop
                        )
                    )

                    stop_side = str(
                        stop.get(
                            "side",
                            ""
                        )
                    ).lower()

                    if (
                        stop_price
                        == self.current_sl
                        and stop_side
                        == expected_side
                    ):

                        correct = True
                        break

                # If the correct stop is missing, recreate it.
                if not correct:

                    logging.warning(
                        "PROTECTIVE STOP MISSING."
                    )

                    logging.warning(
                        "RECREATING SAME FIXED SL = %s",
                        self.current_sl
                    )

                    self.install_sl(
                        new_size,
                        self.current_sl,
                        price
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

            self.handle_closed(
                old_size,
                price
            )

            return

        # ----------------------------------------------------
        # FLAT = NO SL
        # ----------------------------------------------------

        self.current_sl = None
        self.stop_id = None

        try:

            cancel_all_stops(
                self.product_id
            )

        except Exception as exc:

            logging.error(
                "FLAT STOP CLEANUP FAILED: %s",
                exc
            )

            return

        # ----------------------------------------------------
        # BEFORE 05:45
        # ----------------------------------------------------

        if now < trading_start(
            self.day
        ):

            return

        # ----------------------------------------------------
        # RANGE
        # ----------------------------------------------------

        if not self.range_ready:

            if not self.build_day_range(
                now
            ):

                return

        # ----------------------------------------------------
        # BREAKOUT
        # ----------------------------------------------------

        if not self.manual_flat:

            triggered = self.breakout(
                price
            )

            if triggered:

                return

        else:

            # ------------------------------------------------
            # Manual close:
            #
            # Wait for a genuinely NEW high/low.
            # ------------------------------------------------

            if (
                self.day_high is not None
                and price > self.day_high
            ):

                sl = self.day_low

                if self.enter(
                    "LONG",
                    price,
                    sl,
                    "MANUAL CLOSE -> NEW HIGH"
                ):

                    self.manual_flat = False
                    self.high_consumed = True
                    self.persist()

                    return

            if (
                self.day_low is not None
                and price < self.day_low
            ):

                sl = self.day_high

                if self.enter(
                    "SHORT",
                    price,
                    sl,
                    "MANUAL CLOSE -> NEW LOW"
                ):

                    self.manual_flat = False
                    self.low_consumed = True
                    self.persist()

                    return

        # ----------------------------------------------------
        # Update day extremes ONLY AFTER breakout check.
        # ----------------------------------------------------

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
    # RUN
    # ========================================================

    def run(
        self
    ):

        logging.warning(
            "================================================"
        )

        logging.warning(
            "XAUTUSD FIXED-SL BREAKOUT BOT"
        )

        logging.warning(
            "VERSION 14.0"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "DAY START     = 05:30 IST"
        )

        logging.warning(
            "TRADING START = 05:45 IST"
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
            "SAME DAY SL NEVER MOVES"
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
            "NEW DAY WITH POSITION = NO NEW ENTRY"
        )

        logging.warning(
            "OLD SL DELETED FIRST"
        )

        logging.warning(
            "NEW DAY SL CREATED AFTER RANGE READY"
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

        self.handle_new_day(
            now,
            startup_size
        )

        # ----------------------------------------------------
        # STARTUP DURING WEEKEND
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

        # ----------------------------------------------------
        # START WITH OPEN POSITION
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
            self.new_day_sl_set = False

            # ------------------------------------------------
            # If after 05:45, calculate complete current-day
            # range and install correct new-day SL.
            # ------------------------------------------------

            if (
                now >= trading_start(
                    self.day
                )
                and not weekend_block(
                    now
                )
            ):

                try:

                    self.build_day_range(
                        now
                    )

                    price = get_price()

                    if (
                        self.day_low is not None
                        and startup_size > 0
                        and self.day_low >= price
                    ):

                        logging.warning(
                            "STARTUP LONG ALREADY BELOW/AT DAY LOW."
                        )

                        self.handle_closed(
                            startup_size,
                            price
                        )

                    elif (
                        self.day_high is not None
                        and startup_size < 0
                        and self.day_high <= price
                    ):

                        logging.warning(
                            "STARTUP SHORT ALREADY ABOVE/AT DAY HIGH."
                        )

                        self.handle_closed(
                            startup_size,
                            price
                        )

                    else:

                        self.install_new_day_stop(
                            startup_size,
                            price
                        )

                except Exception as exc:

                    logging.exception(
                        "STARTUP STOP SETUP FAILED: %s",
                        exc
                    )

        # ----------------------------------------------------
        # START FLAT
        # ----------------------------------------------------

        else:

            logging.warning(
                "STARTED FLAT"
            )

            self.last_position = 0
            self.current_sl = None
            self.stop_id = None

            # ------------------------------------------------
            # CRITICAL CLEANUP:
            #
            # Any stop left from the previous bot version
            # is removed before doing anything.
            # ------------------------------------------------

            try:

                cancel_all_stops(
                    self.product_id
                )

            except Exception as exc:

                logging.error(
                    "STARTUP STOP CLEANUP FAILED: %s",
                    exc
                )

            if (
                now >= trading_start(
                    self.day
                )
                and not weekend_block(
                    now
                )
            ):

                try:

                    self.build_day_range(
                        now
                    )

                except Exception as exc:

                    logging.error(
                        "STARTUP RANGE ERROR: %s",
                        exc
                    )

        self.persist()

        # ----------------------------------------------------
        # MAIN LOOP
        # ----------------------------------------------------

        while True:

            try:

                self.run_once()

            except KeyboardInterrupt:

                logging.warning(
                    "BOT STOPPED BY USER."
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

    Strategy(
        product
    ).run()


if __name__ == "__main__":

    main()
