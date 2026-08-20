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
# XAUTUSD FIXED SL BREAKOUT BOT
# VERSION 11
# ============================================================
#
# STRATEGY
#
# TRADING DAY:
#   05:30 IST -> next day 05:30 IST
#
# TRADING START:
#   05:45 IST
#
# IMPORTANT:
#   THERE IS NO SPECIAL 05:30 CANDLE LOGIC.
#
#   The bot calculates the ACTUAL trading-day HIGH and LOW
#   from 05:30 onward.
#
#
# ENTRY:
#
#   After 05:45:
#
#   Break current DAY HIGH -> LONG
#   Break current DAY LOW  -> SHORT
#
#
# FIXED STOP:
#
#   LONG:
#       SL = DAY LOW at entry
#
#   SHORT:
#       SL = DAY HIGH at entry
#
#   SL NEVER MOVES.
#
#
# REVERSAL:
#
#   LONG SL HIT:
#       LONG closes
#       SHORT opens
#       SHORT SL = CURRENT DAY HIGH
#
#   SHORT SL HIT:
#       SHORT closes
#       LONG opens
#       LONG SL = CURRENT DAY LOW
#
#
# OVERNIGHT:
#
#   Position survives into next trading day.
#
#   New day starts at 05:30.
#
#   After 05:45:
#
#       LONG  -> new day LOW becomes fixed SL
#       SHORT -> new day HIGH becomes fixed SL
#
#
# MANUAL CLOSE:
#
#   No immediate re-entry.
#
#   Bot waits for a NEW day high or NEW day low.
#
#
# WEEKEND:
#
#   Saturday 05:00 -> square off
#   Saturday       -> no trading
#   Sunday         -> no trading
#   Monday 05:30  -> new trading day
#
#
# POSITION:
#
#   10% balance margin
#   50x leverage
#
# ORDER:
#
#   Market entry
#   One protective stop only
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

# IMPORTANT:
# Changing this version invalidates the old state file.
STATE_VERSION = 11


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
    """
    Trading day begins at 05:30 IST.
    """

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
    """
    Trading begins at 05:45 IST.
    """

    return day + timedelta(
        minutes=15
    )


def is_weekend(
    dt=None
):
    """
    Saturday from 05:00 onward:
        no trading

    Sunday:
        no trading

    Monday before 05:45:
        no trading
    """

    dt = dt or now_ist()

    # Saturday after 05:00
    if (
        dt.weekday() == 5
        and dt.hour >= 5
    ):
        return True

    # Entire Sunday
    if dt.weekday() == 6:
        return True

    # Monday before 05:45
    if (
        dt.weekday() == 0
        and dt < trading_start(
            trading_day_start(dt)
        )
    ):
        return True

    return False


def force_squareoff(
    dt=None
):
    """
    Saturday 05:00-05:04:59 IST.
    """

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
# HISTORICAL CANDLES
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
# BUILD ACTUAL DAY RANGE
# ============================================================

def get_actual_day_range(
    day,
    until_dt=None
):
    """
    IMPORTANT:

    This is NOT an opening-candle calculation.

    It calculates the actual trading-day HIGH/LOW
    from 05:30 until the requested time.

    The 05:30 candle has NO special status.

    If bot starts at 06:30:
        05:30 -> 06:30 is calculated.

    If bot starts at 10:00:
        05:30 -> 10:00 is calculated.
    """

    until_dt = until_dt or now_ist()

    if until_dt <= day:

        return (
            None,
            None
        )

    # Use 15-minute historical candles to reconstruct
    # the actual day range.
    #
    # These candles are simply historical data.
    # There is NO opening-candle logic here.

    rows = candles(
        "15m",
        day,
        until_dt
    )

    day_high = None
    day_low = None

    for row in rows:

        try:

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

            if candle_time < day:
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
                day_high is None
                or candle_high > day_high
            ):

                day_high = candle_high

            if (
                day_low is None
                or candle_low < day_low
            ):

                day_low = candle_low

        except (
            KeyError,
            ValueError,
            TypeError
        ):

            continue

    # Always include current market price.
    #
    # This makes the current range correct even when
    # the latest candle is still forming.

    try:

        current_price = get_price()

        if (
            day_high is None
            or current_price > day_high
        ):

            day_high = current_price

        if (
            day_low is None
            or current_price < day_low
        ):

            day_low = current_price

    except Exception as exc:

        logging.error(
            "Could not add current price to range: %s",
            exc
        )

    return (
        day_high,
        day_low
    )


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
        "size": int(
            size
        ),
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

        return [
            result
        ]

    return result


# ============================================================
# CANCEL ALL STOPS
# ============================================================

def cancel_all_stops(
    product_id
):

    orders = open_stops(
        product_id
    )

    for order in orders:

        try:

            cancel_order(
                order.get(
                    "id"
                )
            )

        except Exception as exc:

            logging.error(
                "Could not cancel stop %s: %s",
                order.get("id"),
                exc
            )


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

            data = json.load(
                file
            )

            # ------------------------------------------------
            # VERY IMPORTANT
            #
            # Old versions used 05:30 candle state.
            #
            # NEVER restore that state into version 11.
            # ------------------------------------------------

            if data.get(
                "version"
            ) != STATE_VERSION:

                logging.warning(
                    "OLD BOT STATE DETECTED."
                )

                logging.warning(
                    "IGNORING OLD STATE "
                    "TO PREVENT OLD 05:30 LOGIC."
                )

                return {}

            return data

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
        # BREAKOUT CONSUMPTION
        # ----------------------------------------------------

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
                "version": STATE_VERSION,

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
            trading_start(
                new_day
            )
        )

        logging.warning(
            "================================================"
        )

        self.day = new_day

        # ----------------------------------------------------
        # NEW DAY RANGE
        # ----------------------------------------------------

        self.day_high = None
        self.day_low = None
        self.range_ready = False

        # ----------------------------------------------------
        # NEW DAY BREAKOUT STATE
        # ----------------------------------------------------

        self.high_breakout_consumed = False
        self.low_breakout_consumed = False

        # ----------------------------------------------------
        # MANUAL CLOSE DOES NOT CARRY ACROSS DAY
        # ----------------------------------------------------

        self.manual_flat = False
        self.manual_reference_high = None
        self.manual_reference_low = None

        # ----------------------------------------------------
        # EXISTING POSITION
        # ----------------------------------------------------

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

            self.current_sl = None
            self.stop_id = None

        self.persist()


    # ========================================================
    # BUILD ACTUAL DAY RANGE
    # ========================================================

    def rebuild_day_range(
        self
    ):
        """
        Build actual HIGH/LOW from 05:30 until now.

        NO opening candle logic.
        """

        if self.day is None:
            return False

        now = now_ist()

        if now <= self.day:

            return False

        high, low = (
            get_actual_day_range(
                self.day,
                now
            )
        )

        if high is None or low is None:

            return False

        self.day_high = high
        self.day_low = low

        self.range_ready = (
            now >= trading_start(
                self.day
            )
        )

        self.persist()

        logging.warning(
            "================================================"
        )

        logging.warning(
            "ACTUAL DAY RANGE"
        )

        logging.warning(
            "FROM = %s IST",
            self.day
        )

        logging.warning(
            "UNTIL = %s IST",
            now
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
            "NO SPECIAL 05:30 CANDLE"
        )

        logging.warning(
            "================================================"
        )

        return True


    # ========================================================
    # UPDATE LIVE DAY EXTREMES
    # ========================================================

    def update_day_extremes(
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
    # READ STOP PRICE
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
    # CREATE EXACTLY ONE FIXED SL
    # ========================================================

    def ensure_fixed_sl(
        self,
        position_size,
        sl_price,
        market_price,
        force=False
    ):

        if sl_price is None:

            logging.error(
                "SL PRICE IS NONE."
            )

            return False

        # ----------------------------------------------------
        # VALIDATE SL SIDE
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

        elif position_size < 0:

            if sl_price <= market_price:

                logging.error(
                    "SHORT SL INVALID | "
                    "SL=%s | PRICE=%s",
                    sl_price,
                    market_price
                )

                return False

        else:

            return False

        expected_side = (
            self.stop_side(
                position_size
            )
        )

        orders = open_stops(
            self.product_id
        )

        matching = []

        # ----------------------------------------------------
        # REMOVE ALL WRONG/EXTRA STOPS
        # ----------------------------------------------------

        for order in orders:

            order_id = order.get(
                "id"
            )

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

            correct = (
                side == expected_side
                and order_price == sl_price
                and order_id
            )

            if correct:

                matching.append(
                    order
                )

            else:

                if order_id:

                    try:

                        cancel_order(
                            order_id
                        )

                    except Exception as exc:

                        logging.error(
                            "Could not cancel "
                            "wrong stop %s: %s",
                            order_id,
                            exc
                        )

        # ----------------------------------------------------
        # REMOVE DUPLICATES
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

        # ----------------------------------------------------
        # EXISTING CORRECT STOP
        # ----------------------------------------------------

        if (
            len(matching) == 1
            and not force
        ):

            self.stop_id = (
                matching[0].get(
                    "id"
                )
            )

            return True

        # ----------------------------------------------------
        # FORCE REPLACE
        # ----------------------------------------------------

        for order in matching:

            try:

                cancel_order(
                    order.get(
                        "id"
                    )
                )

            except Exception:
                pass

        # ----------------------------------------------------
        # CREATE EXACTLY ONE STOP
        # ----------------------------------------------------

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

        logging.warning(
            "================================================"
        )

        logging.warning(
            "FIXED SL ACTIVE"
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
            "FIXED SL = %s",
            sl_price
        )

        logging.warning(
            "SL WILL NOT MOVE"
        )

        logging.warning(
            "================================================"
        )

        return True


    # ========================================================
    # ENTER POSITION
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

        if is_weekend():
            return False

        if sl_price is None:

            logging.error(
                "ENTRY BLOCKED: SL IS NONE."
            )

            return False

        current = get_position(
            self.product_id
        )

        if current["size"] != 0:

            self.last_position = (
                current["size"]
            )

            return False

        # ----------------------------------------------------
        # SL VALIDATION
        # ----------------------------------------------------

        if direction == "LONG":

            if sl_price >= price:

                logging.error(
                    "LONG ENTRY BLOCKED | "
                    "SL=%s | PRICE=%s",
                    sl_price,
                    price
                )

                return False

        else:

            if sl_price <= price:

                logging.error(
                    "SHORT ENTRY BLOCKED | "
                    "SL=%s | PRICE=%s",
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
            "NEW LIVE ENTRY"
        )

        logging.warning(
            "DIRECTION = %s",
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
            "FIXED SL = %s",
            sl_price
        )

        logging.warning(
            "REASON = %s",
            reason
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
            # WAIT FOR FILL
            # ------------------------------------------------

            for _ in range(30):

                time.sleep(
                    0.2
                )

                position = (
                    get_position(
                        self.product_id
                    )
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

                    # ------------------------------------------------
                    # CRITICAL:
                    #
                    # SAVE THE SL ONCE.
                    #
                    # It is NEVER recalculated by update_day_extremes.
                    # ------------------------------------------------

                    self.current_sl = (
                        sl_price
                    )

                    self.persist()

                    actual_market_price = (
                        get_price()
                    )

                    if not self.ensure_fixed_sl(
                        actual_size,
                        self.current_sl,
                        actual_market_price,
                        force=True
                    ):

                        raise RuntimeError(
                            "Position opened but "
                            "fixed SL could not be created."
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

    def check_breakout(
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
        # NEW HIGH -> LONG
        # ----------------------------------------------------

        if (
            not self.high_breakout_consumed
            and price > self.day_high
        ):

            previous_high = (
                self.day_high
            )

            # FIXED LONG SL:
            # day low BEFORE the breakout is used.

            fixed_sl = (
                self.day_low
            )

            success = self.enter(
                "LONG",
                price,
                fixed_sl,
                "DAY HIGH BREAKOUT"
            )

            if success:

                self.high_breakout_consumed = True

                self.persist()

                logging.warning(
                    "HIGH BREAKOUT CONSUMED | "
                    "OLD HIGH=%s",
                    previous_high
                )

            return success

        # ----------------------------------------------------
        # NEW LOW -> SHORT
        # ----------------------------------------------------

        if (
            not self.low_breakout_consumed
            and price < self.day_low
        ):

            previous_low = (
                self.day_low
            )

            # FIXED SHORT SL:
            # day high BEFORE the breakout is used.

            fixed_sl = (
                self.day_high
            )

            success = self.enter(
                "SHORT",
                price,
                fixed_sl,
                "DAY LOW BREAKOUT"
            )

            if success:

                self.low_breakout_consumed = True

                self.persist()

                logging.warning(
                    "LOW BREAKOUT CONSUMED | "
                    "OLD LOW=%s",
                    previous_low
                )

            return success

        return False


    # ========================================================
    # MANUAL CLOSE BREAKOUT
    # ========================================================

    def check_manual_reentry(
        self,
        price
    ):

        if not self.manual_flat:
            return False

        reference_high = (
            self.manual_reference_high
        )

        reference_low = (
            self.manual_reference_low
        )

        # ----------------------------------------------------
        # NEW HIGH
        # ----------------------------------------------------

        if (
            reference_high is not None
            and price > reference_high
        ):

            fixed_sl = (
                self.day_low
            )

            success = self.enter(
                "LONG",
                price,
                fixed_sl,
                "MANUAL CLOSE -> NEW HIGH"
            )

            if success:

                self.manual_flat = False

                self.manual_reference_high = None
                self.manual_reference_low = None

                self.high_breakout_consumed = True

                self.persist()

            return success

        # ----------------------------------------------------
        # NEW LOW
        # ----------------------------------------------------

        if (
            reference_low is not None
            and price < reference_low
        ):

            fixed_sl = (
                self.day_high
            )

            success = self.enter(
                "SHORT",
                price,
                fixed_sl,
                "MANUAL CLOSE -> NEW LOW"
            )

            if success:

                self.manual_flat = False

                self.manual_reference_high = None
                self.manual_reference_low = None

                self.low_breakout_consumed = True

                self.persist()

            return success

        return False


    # ========================================================
    # DETECT CLOSE
    # ========================================================

    def detect_close_reason(
        self,
        old_size,
        price
    ):

        if old_size == 0:
            return "none"

        # ----------------------------------------------------
        # FIXED SL
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

    def handle_closed_position(
        self,
        old_size,
        price
    ):

        fixed_sl_that_was_used = (
            self.current_sl
        )

        reason = (
            self.detect_close_reason(
                old_size,
                price
            )
        )

        # Save current extremes BEFORE clearing anything.

        current_day_high = (
            self.day_high
        )

        current_day_low = (
            self.day_low
        )

        # ----------------------------------------------------
        # CANCEL OLD STOP
        # ----------------------------------------------------

        try:

            cancel_all_stops(
                self.product_id
            )

        except Exception as exc:

            logging.error(
                "Stop cleanup failed: %s",
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
                "================================================"
            )

            logging.warning(
                "MANUAL CLOSE DETECTED"
            )

            logging.warning(
                "NO IMMEDIATE RE-ENTRY"
            )

            logging.warning(
                "WAITING FOR NEW DAY HIGH/LOW"
            )

            logging.warning(
                "REFERENCE HIGH = %s",
                current_day_high
            )

            logging.warning(
                "REFERENCE LOW = %s",
                current_day_low
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

            self.persist()

            return


        # ====================================================
        # STOP LOSS
        # ====================================================

        logging.warning(
            "================================================"
        )

        logging.warning(
            "FIXED STOP LOSS HIT"
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
            "OLD FIXED SL = %s",
            fixed_sl_that_was_used
        )

        logging.warning(
            "CURRENT DAY HIGH = %s",
            current_day_high
        )

        logging.warning(
            "CURRENT DAY LOW = %s",
            current_day_low
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
        # New SHORT SL = current day HIGH
        # ----------------------------------------------------

        if old_size > 0:

            if current_day_high is None:

                logging.error(
                    "Cannot reverse LONG -> SHORT: "
                    "day high unavailable."
                )

                return

            self.enter(
                "SHORT",
                price,
                current_day_high,
                "LONG SL HIT -> REVERSE SHORT"
            )

            return

        # ----------------------------------------------------
        # SHORT -> LONG
        #
        # New LONG SL = current day LOW
        # ----------------------------------------------------

        if current_day_low is None:

            logging.error(
                "Cannot reverse SHORT -> LONG: "
                "day low unavailable."
            )

            return

        self.enter(
            "LONG",
            price,
            current_day_low,
            "SHORT SL HIT -> REVERSE LONG"
        )


    # ========================================================
    # OVERNIGHT POSITION
    # ========================================================

    def apply_overnight_sl(
        self,
        position_size,
        price
    ):
        """
        Existing position survives into a new day.

        We calculate the ACTUAL new day's high/low from
        05:30 onward.

        At/after 05:45:

            LONG  -> new day LOW
            SHORT -> new day HIGH

        Then the SL remains fixed.
        """

        if not self.carried_position:
            return False

        if self.overnight_sl_set:
            return False

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

        # ----------------------------------------------------
        # SL MUST BE ON CORRECT SIDE
        # ----------------------------------------------------

        if (
            position_size > 0
            and new_sl >= price
        ):

            logging.error(
                "CARRIED LONG SL INVALID | "
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
                "CARRIED SHORT SL INVALID | "
                "DAY HIGH=%s | PRICE=%s",
                new_sl,
                price
            )

            return False

        self.current_sl = new_sl

        self.overnight_sl_set = True

        self.persist()

        logging.warning(
            "================================================"
        )

        logging.warning(
            "OVERNIGHT POSITION"
        )

        logging.warning(
            "DIRECTION = %s",
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
            "SL WILL NOT MOVE"
        )

        logging.warning(
            "================================================"
        )

        self.ensure_fixed_sl(
            position_size,
            new_sl,
            price,
            force=True
        )

        return True


    # ========================================================
    # STARTUP RECONCILIATION
    # ========================================================

    def startup_reconcile(
        self,
        now,
        position_size
    ):
        """
        VERY IMPORTANT.

        If the bot starts at 06:30 with an existing position,
        we DO NOT use the 05:30 candle.

        We calculate the actual day's range first.

        If the old state belonged to the previous bot version,
        it was already discarded by load_state().
        """

        self.day = trading_day_start(
            now
        )

        # ----------------------------------------------------
        # NO POSITION
        # ----------------------------------------------------

        if position_size == 0:

            self.last_position = 0

            self.current_sl = None
            self.stop_id = None

            self.carried_position = False
            self.overnight_sl_set = False

            if (
                now >= trading_start(
                    self.day
                )
                and not is_weekend(now)
            ):

                self.rebuild_day_range()

            else:

                self.day_high = None
                self.day_low = None
                self.range_ready = False

            self.persist()

            return

        # ----------------------------------------------------
        # EXISTING POSITION
        # ----------------------------------------------------

        logging.warning(
            "================================================"
        )

        logging.warning(
            "STARTUP WITH OPEN POSITION"
        )

        logging.warning(
            "SIZE = %s",
            position_size
        )

        logging.warning(
            "CALCULATING ACTUAL DAY RANGE"
        )

        logging.warning(
            "NO 05:30 CANDLE LOGIC"
        )

        logging.warning(
            "================================================"
        )

        self.last_position = (
            position_size
        )

        self.carried_position = True
        self.overnight_sl_set = False
        self.manual_flat = False

        if (
            now >= trading_start(
                self.day
            )
            and not is_weekend(now)
        ):

            self.rebuild_day_range()

            price = get_price()

            # ------------------------------------------------
            # IMPORTANT:
            #
            # Existing position gets the ACTUAL current
            # trading-day high/low.
            #
            # LONG  -> actual day LOW
            # SHORT -> actual day HIGH
            # ------------------------------------------------

            if position_size > 0:

                startup_sl = (
                    self.day_low
                )

            else:

                startup_sl = (
                    self.day_high
                )

            if startup_sl is None:

                raise RuntimeError(
                    "Could not calculate "
                    "startup fixed SL."
                )

            self.current_sl = startup_sl

            self.overnight_sl_set = True

            self.persist()

            self.ensure_fixed_sl(
                position_size,
                startup_sl,
                price,
                force=True
            )

        self.persist()


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
                    "SIZE = %s",
                    size
                )

                logging.warning(
                    "================================================"
                )

                try:

                    cancel_all_stops(
                        self.product_id
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

                self.persist()

            return

        # ----------------------------------------------------
        # WEEKEND
        # ----------------------------------------------------

        if is_weekend(now):

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

                # If a fixed SL already exists in state,
                # preserve it.
                if self.current_sl is not None:

                    self.ensure_fixed_sl(
                        new_size,
                        self.current_sl,
                        price,
                        force=False
                    )

                self.persist()

                return

            # ------------------------------------------------
            # NEW DAY RANGE FOR CARRIED POSITION
            # ------------------------------------------------

            if (
                self.carried_position
                and not self.overnight_sl_set
            ):

                if now >= trading_start(
                    self.day
                ):

                    if not self.range_ready:

                        self.rebuild_day_range()

                    self.apply_overnight_sl(
                        new_size,
                        price
                    )

                    self.last_position = (
                        new_size
                    )

                    return

                self.last_position = (
                    new_size
                )

                return

            # ------------------------------------------------
            # NORMAL RUNNING POSITION
            # ------------------------------------------------

            self.last_position = (
                new_size
            )

            # ------------------------------------------------
            # CRITICAL:
            #
            # NEVER recalculate current_sl.
            #
            # NEVER use day_high/day_low to move the SL.
            #
            # Only ensure the SAME stop exists.
            # ------------------------------------------------

            if self.current_sl is not None:

                self.ensure_fixed_sl(
                    new_size,
                    self.current_sl,
                    price,
                    force=False
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

        # ----------------------------------------------------
        # REMOVE ORPHAN STOPS
        # ----------------------------------------------------

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
                "Orphan stop cleanup: %s",
                exc
            )

        # ----------------------------------------------------
        # BEFORE 05:45
        # ----------------------------------------------------

        if now < trading_start(
            self.day
        ):

            return

        # ----------------------------------------------------
        # BUILD ACTUAL DAY RANGE
        # ----------------------------------------------------

        if not self.range_ready:

            self.rebuild_day_range()

        if not self.range_ready:

            return

        # ----------------------------------------------------
        # MANUAL CLOSE MODE
        # ----------------------------------------------------

        if self.manual_flat:

            triggered = (
                self.check_manual_reentry(
                    price
                )
            )

            if triggered:

                return

            # Keep actual range updated.
            self.update_day_extremes(
                price
            )

            return

        # ----------------------------------------------------
        # NORMAL BREAKOUT
        # ----------------------------------------------------

        triggered = (
            self.check_breakout(
                price
            )
        )

        if triggered:

            return

        # ----------------------------------------------------
        # NO BREAKOUT
        #
        # Update actual day's high/low.
        #
        # IMPORTANT:
        #
        # This does NOT change an existing SL because there
        # is no position here.
        # ----------------------------------------------------

        self.update_day_extremes(
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
            "XAUTUSD FIXED-SL BREAKOUT BOT"
        )

        logging.warning(
            "VERSION 11"
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
            "NO SPECIAL 05:30 CANDLE LOGIC"
        )

        logging.warning(
            "ACTUAL DAY HIGH/LOW FROM 05:30"
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
            "SL NEVER MOVES"
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

        self.startup_reconcile(
            now,
            startup_size
        )

        # ----------------------------------------------------
        # WEEKEND MESSAGE
        # ----------------------------------------------------

        if is_weekend(now):

            logging.warning(
                "BOT STARTED DURING WEEKEND."
            )

            logging.warning(
                "NO TRADING."
            )

        # ----------------------------------------------------
        # CLEAN OLD STOPS IF FLAT
        # ----------------------------------------------------

        if startup_size == 0:

            try:

                cancel_all_stops(
                    self.product_id
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
