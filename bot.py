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
# ============================================================
#
# TRADING DAY
#   05:30 IST -> next day 05:30 IST
#
# TRADING START
#   05:45 IST
#
# ENTRY
#   After 05:45:
#
#   NEW DAY HIGH -> LONG
#   NEW DAY LOW  -> SHORT
#
# STOP LOSS
#
#   LONG:
#       SL = DAY LOW AT THE MOMENT OF ENTRY
#
#   SHORT:
#       SL = DAY HIGH AT THE MOMENT OF ENTRY
#
# IMPORTANT:
#   SL DOES NOT TRAIL.
#
#   Once LONG is entered with SL=4437:
#       SL remains 4437.
#
#   If that SL is hit:
#       LONG closes
#       SHORT opens
#       SHORT SL = CURRENT DAY HIGH
#
#   If SHORT SL is hit:
#       SHORT closes
#       LONG opens
#       LONG SL = CURRENT DAY LOW
#
#
# STOP MANAGEMENT
#
#   Before ANY new SL:
#       1. Cancel ALL existing stops.
#       2. Verify ALL stops are gone.
#       3. Create exactly ONE new stop.
#
#   This prevents old stops such as:
#       4486
#       4457
#   remaining together with the correct stop.
#
#
# OVERNIGHT
#
#   Existing position at 05:30:
#       NO NEW TRADE.
#
#   At 05:45:
#       Build new day's range.
#
#   Carried LONG:
#       new SL = new day's LOW
#
#   Carried SHORT:
#       new SL = new day's HIGH
#
#   That new SL then remains fixed.
#
#
# STARTING BOT LATE
#
#   If bot starts at 08:30 with an existing LONG:
#       Rebuild today's actual range from 05:30
#       until current time.
#
#       LONG SL = actual current DAY LOW.
#
#   Therefore if DAY LOW = 4437:
#       SL = 4437.
#
#
# WEEKEND
#
#   Saturday 05:00 -> square off.
#   Saturday -> no trading.
#   Sunday   -> no trading.
#   Monday 05:45 -> trading resumes.
#
#
# POSITION SIZE
#
#   10% balance margin
#   50x leverage
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
        ),
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
        and dt.time()
        >= datetime.strptime(
            "05:00",
            "%H:%M"
        ).time()
    ):
        return True

    # Sunday.
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
# STOP ORDER
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
        "size": int(
            size
        ),
        "side": side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(price),
        "stop_trigger_method": "last_traded_price",
        "reduce_only": True,
        "client_order_id": client_id[:32]
    }

    logging.warning(
        "CREATING ONE STOP: %s",
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
                "Stop %s already gone.",
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

    for attempt in range(5):

        orders = open_stops(
            product_id
        )

        if not orders:

            if verify:

                logging.info(
                    "STOP CLEANUP VERIFIED: "
                    "0 open stops."
                )

            return True

        logging.warning(
            "STOP CLEANUP: found %s stop(s), "
            "attempt %s/5",
            len(orders),
            attempt + 1
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
                    "Failed cancelling stop %s: %s",
                    order_id,
                    exc
                )

        time.sleep(
            0.5
        )

    remaining = open_stops(
        product_id
    )

    if remaining:

        logging.error(
            "STOP CLEANUP FAILED: "
            "%s stop(s) still remain.",
            len(remaining)
        )

        return False

    logging.info(
        "STOP CLEANUP VERIFIED: 0 open stops."
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

        # ----------------------------------------------------
        # STATE
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # CRITICAL:
        #
        # OLD DAY STOP MUST BE REMOVED.
        # ----------------------------------------------------

        try:

            if self.product_id:

                logging.warning(
                    "REMOVING ALL OLD DAY STOPS..."
                )

                cancel_all_stops(
                    self.product_id
                )

        except Exception as exc:

            logging.error(
                "Old-day stop cleanup failed: %s",
                exc
            )

        self.day = new_day

        self.day_high = None
        self.day_low = None

        self.range_ready = False

        self.high_breakout_consumed = False
        self.low_breakout_consumed = False

        self.manual_flat = False

        self.manual_reference_high = None
        self.manual_reference_low = None

        # NEVER carry old SL into new day.
        self.current_sl = None
        self.stop_id = None

        if existing_position != 0:

            self.carried_position = True
            self.overnight_sl_set = False

            logging.warning(
                "POSITION CARRIED INTO NEW DAY | "
                "SIZE=%s",
                existing_position
            )

            logging.warning(
                "NO NEW TRADE WILL BE TAKEN."
            )

        else:

            self.carried_position = False
            self.overnight_sl_set = False

        self.persist()


    # ========================================================
    # BUILD CURRENT DAY RANGE
    #
    # IMPORTANT:
    #
    # This does NOT only read the 05:30 candle.
    #
    # It rebuilds the actual day HIGH/LOW from 05:30
    # up to the requested time.
    #
    # This fixes the "bot started late" problem.
    # ========================================================

    def rebuild_current_day_range(
        self,
        end_time=None
    ):

        if self.day is None:
            return False

        end_time = (
            end_time
            or now_ist()
        )

        if end_time <= self.day:

            return False

        rows = candles(
            "15m",
            self.day,
            end_time + timedelta(
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

            if candle_time < self.day:
                continue

            if candle_time > end_time:
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

        # Include live price so the current partial candle
        # is represented too.
        try:

            live_price = get_price()

            if (
                high is None
                or live_price > high
            ):

                high = live_price

            if (
                low is None
                or live_price < low
            ):

                low = live_price

        except Exception as exc:

            logging.error(
                "Could not add live price to range: %s",
                exc
            )

        if high is None or low is None:

            return False

        self.day_high = high
        self.day_low = low

        self.range_ready = True

        self.persist()

        logging.warning(
            "================================================"
        )

        logging.warning(
            "CURRENT DAY RANGE REBUILT"
        )

        logging.warning(
            "FROM = %s IST",
            self.day
        )

        logging.warning(
            "TO   = %s IST",
            end_time
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
    #
    # ONLY changes the DAY RANGE.
    #
    # IT NEVER CHANGES current_sl.
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
    #
    # THIS IS THE MOST IMPORTANT FUNCTION.
    #
    # We NEVER try to "keep the correct old stop".
    #
    # We ALWAYS:
    #
    #   CANCEL ALL
    #   VERIFY ZERO
    #   CREATE ONE
    #
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

            logging.error(
                "STOP REJECTED: SL is None."
            )

            return False

        # ----------------------------------------------------
        # Validate direction.
        # ----------------------------------------------------

        if (
            position_size > 0
            and sl_price >= market_price
        ):

            logging.error(
                "LONG STOP INVALID | "
                "SL=%s | MARKET=%s",
                sl_price,
                market_price
            )

            return False

        if (
            position_size < 0
            and sl_price <= market_price
        ):

            logging.error(
                "SHORT STOP INVALID | "
                "SL=%s | MARKET=%s",
                sl_price,
                market_price
            )

            return False

        direction = (
            "LONG"
            if position_size > 0
            else "SHORT"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "REPLACING PROTECTIVE STOP"
        )

        logging.warning(
            "POSITION = %s",
            direction
        )

        logging.warning(
            "NEW SL = %s",
            sl_price
        )

        logging.warning(
            "MARKET = %s",
            market_price
        )

        logging.warning(
            "================================================"
        )

        # ----------------------------------------------------
        # STEP 1:
        # CANCEL EVERYTHING.
        # ----------------------------------------------------

        cleaned = cancel_all_stops(
            self.product_id,
            verify=True
        )

        if not cleaned:

            logging.error(
                "STOP REPLACEMENT ABORTED."
            )

            logging.error(
                "OLD STOP(S) COULD NOT BE REMOVED."
            )

            logging.error(
                "NO NEW STOP WILL BE CREATED."
            )

            return False

        # ----------------------------------------------------
        # STEP 2:
        # EXTRA verification.
        # ----------------------------------------------------

        remaining = open_stops(
            self.product_id
        )

        if remaining:

            logging.error(
                "STOP REPLACEMENT ABORTED: "
                "%s stop(s) still exist.",
                len(remaining)
            )

            return False

        # ----------------------------------------------------
        # STEP 3:
        # CREATE EXACTLY ONE.
        # ----------------------------------------------------

        result = stop_order(
            self.product_id,
            self.stop_side(
                position_size
            ),
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

        self.persist()

        # ----------------------------------------------------
        # STEP 4:
        # VERIFY STOP EXISTS.
        # ----------------------------------------------------

        time.sleep(
            0.4
        )

        verify = open_stops(
            self.product_id
        )

        if len(verify) != 1:

            logging.error(
                "STOP VERIFICATION FAILED."
            )

            logging.error(
                "EXPECTED = 1 STOP"
            )

            logging.error(
                "FOUND = %s",
                len(verify)
            )

            return False

        verified_price = (
            self.stop_price(
                verify[0]
            )
        )

        if verified_price != sl_price:

            logging.error(
                "STOP PRICE VERIFICATION FAILED."
            )

            logging.error(
                "EXPECTED = %s",
                sl_price
            )

            logging.error(
                "EXCHANGE STOP = %s",
                verified_price
            )

            return False

        self.stop_id = verify[0].get(
            "id"
        )

        self.persist()

        logging.warning(
            "================================================"
        )

        logging.warning(
            "STOP VERIFIED"
        )

        logging.warning(
            "POSITION = %s",
            direction
        )

        logging.warning(
            "EXACTLY ONE STOP = %s",
            sl_price
        )

        logging.warning(
            "================================================"
        )

        return True


    # ========================================================
    # ENTER
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

            logging.error(
                "ENTRY BLOCKED: SL is None."
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
        # Validate SL.
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
            "NEW ENTRY"
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
            "================================================"
        )

        self.entry_lock = True

        try:

            # ------------------------------------------------
            # Before entry, remove any stale stops.
            # ------------------------------------------------

            if not cancel_all_stops(
                self.product_id,
                verify=True
            ):

                raise RuntimeError(
                    "Could not clean old stops "
                    "before entry."
                )

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

            filled = None

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

                    filled = position

                    break

            if filled is None:

                raise RuntimeError(
                    "Entry sent but fill "
                    "was not confirmed."
                )

            actual_size = (
                filled["size"]
            )

            self.last_position = (
                actual_size
            )

            self.manual_flat = False

            self.carried_position = False

            self.overnight_sl_set = False

            # ------------------------------------------------
            # IMPORTANT:
            #
            # This SL belongs ONLY to this position.
            #
            # It is stored ONCE.
            # ------------------------------------------------

            self.current_sl = sl_price

            self.persist()

            actual_market = get_price()

            # ------------------------------------------------
            # Create exactly one stop.
            # ------------------------------------------------

            if not self.replace_stop(
                actual_size,
                sl_price,
                actual_market
            ):

                raise RuntimeError(
                    "Position is open but "
                    "protective SL could not be verified."
                )

            return True

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

            old_high = self.day_high

            # LONG SL = CURRENT DAY LOW.
            #
            # THIS IS THE EXACT RULE.
            #
            sl = self.day_low

            success = self.enter(
                "LONG",
                price,
                sl,
                "DAY HIGH BREAKOUT"
            )

            if success:

                self.high_breakout_consumed = True

                self.persist()

                logging.warning(
                    "LONG ENTRY CONSUMED | "
                    "BREAK HIGH=%s | "
                    "SL=%s",
                    old_high,
                    sl
                )

            return success

        # ----------------------------------------------------
        # NEW LOW -> SHORT
        # ----------------------------------------------------

        if (
            not self.low_breakout_consumed
            and price < self.day_low
        ):

            old_low = self.day_low

            # SHORT SL = CURRENT DAY HIGH.
            sl = self.day_high

            success = self.enter(
                "SHORT",
                price,
                sl,
                "DAY LOW BREAKOUT"
            )

            if success:

                self.low_breakout_consumed = True

                self.persist()

                logging.warning(
                    "SHORT ENTRY CONSUMED | "
                    "BREAK LOW=%s | "
                    "SL=%s",
                    old_low,
                    sl
                )

            return success

        return False


    # ========================================================
    # MANUAL CLOSE BREAKOUT
    # ========================================================

    def manual_breakout(
        self,
        price
    ):

        if not self.manual_flat:
            return False

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

        # ----------------------------------------------------
        # NEW HIGH -> LONG
        # ----------------------------------------------------

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

                self.high_breakout_consumed = True

                self.persist()

            return success

        # ----------------------------------------------------
        # NEW LOW -> SHORT
        # ----------------------------------------------------

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

                self.low_breakout_consumed = True

                self.persist()

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

        # Use the fixed SL belonging to the position.
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
    # POSITION CLOSED
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

        # ----------------------------------------------------
        # Capture CURRENT DAY extremes before cleanup.
        # ----------------------------------------------------

        current_day_high = self.day_high
        current_day_low = self.day_low

        # Include the closing price in the range.
        self.update_day_range(
            price
        )

        current_day_high = self.day_high
        current_day_low = self.day_low

        # ----------------------------------------------------
        # REMOVE ALL OLD STOPS.
        # ----------------------------------------------------

        cleaned = cancel_all_stops(
            self.product_id,
            verify=True
        )

        if not cleaned:

            logging.error(
                "Could not clean old stops after close."
            )

            return

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
                "MANUAL CLOSE"
            )

            logging.warning(
                "NO IMMEDIATE RE-ENTRY"
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
        # FIXED SL HIT
        # ====================================================

        logging.warning(
            "================================================"
        )

        logging.warning(
            "FIXED SL HIT"
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
            old_sl
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
            "REVERSING"
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

            self.enter(
                "SHORT",
                price,
                new_sl,
                "LONG SL HIT -> SHORT"
            )

            return

        # ----------------------------------------------------
        # SHORT -> LONG
        #
        # LONG SL = CURRENT DAY LOW
        # ----------------------------------------------------

        new_sl = current_day_low

        self.enter(
            "LONG",
            price,
            new_sl,
            "SHORT SL HIT -> LONG"
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
        # LONG -> NEW DAY LOW
        # SHORT -> NEW DAY HIGH
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
        # Do NOT invent another SL if exact level is invalid.
        # ----------------------------------------------------

        if (
            position_size > 0
            and new_sl >= price
        ):

            logging.error(
                "CARRIED LONG NEW SL INVALID | "
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
                "CARRIED SHORT NEW SL INVALID | "
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
            "DAY HIGH = %s",
            self.day_high
        )

        logging.warning(
            "DAY LOW = %s",
            self.day_low
        )

        logging.warning(
            "NEW FIXED SL = %s",
            new_sl
        )

        logging.warning(
            "================================================"
        )

        if not self.replace_stop(
            position_size,
            new_sl,
            price
        ):

            return False

        self.overnight_sl_set = True

        self.current_sl = new_sl

        self.persist()

        return True


    # ========================================================
    # RUN ONCE
    # ========================================================

    def run_once(
        self
    ):

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
        # SQUARE OFF FIRST
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
                    self.product_id,
                    verify=True
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

        # ====================================================
        # POSITION OPEN
        # ====================================================

        if new_size != 0:

            # ------------------------------------------------
            # NEW POSITION DETECTED
            # ------------------------------------------------

            if (
                old_size == 0
                and new_size != 0
            ):

                logging.warning(
                    "POSITION DETECTED | SIZE=%s",
                    new_size
                )

                self.last_position = (
                    new_size
                )

                # If we don't have a trusted fixed SL,
                # reconstruct today's actual range.
                #
                # This is especially important when the bot
                # was restarted with an already-open position.
                if (
                    self.day is not None
                    and now >= trading_start(
                        self.day
                    )
                ):

                    self.rebuild_current_day_range(
                        now
                    )

                # If current_sl exists from old state,
                # DO NOT trust it blindly.
                #
                # We will use the actual current day's range
                # for a newly detected existing position.
                if self.day_high is not None and self.day_low is not None:

                    if new_size > 0:

                        correct_sl = self.day_low

                    else:

                        correct_sl = self.day_high

                    # ------------------------------------------------
                    # IMPORTANT:
                    #
                    # Replace ALL old stops with the actual
                    # day-based SL.
                    # ------------------------------------------------

                    if (
                        correct_sl is not None
                    ):

                        if (
                            (
                                new_size > 0
                                and correct_sl < price
                            )
                            or
                            (
                                new_size < 0
                                and correct_sl > price
                            )
                        ):

                            self.current_sl = (
                                correct_sl
                            )

                            self.replace_stop(
                                new_size,
                                correct_sl,
                                price
                            )

                self.persist()

                return

            # ------------------------------------------------
            # BEFORE 05:45
            # ------------------------------------------------

            if now < trading_start(
                self.day
            ):

                self.last_position = new_size

                return

            # ------------------------------------------------
            # OVERNIGHT / NEW DAY POSITION
            # ------------------------------------------------

            if (
                self.carried_position
                and not self.overnight_sl_set
            ):

                # Build NEW day's range.
                if not self.range_ready:

                    self.rebuild_current_day_range(
                        trading_start(
                            self.day
                        )
                    )

                if self.range_ready:

                    # Include current price only for range,
                    # not to move an already-existing SL.
                    #
                    # For carried position before the new
                    # day's SL is established, this is the
                    # new day's range.
                    self.update_day_range(
                        price
                    )

                    self.apply_overnight_sl(
                        new_size,
                        price
                    )

                self.last_position = new_size

                return

            # ------------------------------------------------
            # NORMAL OPEN POSITION
            # ------------------------------------------------

            self.last_position = new_size

            # ------------------------------------------------
            # NEVER CHANGE current_sl HERE.
            # ------------------------------------------------

            if self.current_sl is not None:

                # Verify that EXACTLY ONE stop exists and
                # that it is the fixed SL.
                #
                # If something is wrong, replace it.
                stops = open_stops(
                    self.product_id
                )

                valid = False

                if len(stops) == 1:

                    existing_price = (
                        self.stop_price(
                            stops[0]
                        )
                    )

                    if (
                        existing_price
                        == self.current_sl
                    ):

                        valid = True

                if not valid:

                    logging.warning(
                        "PROTECTIVE STOP IS WRONG "
                        "OR DUPLICATED."
                    )

                    logging.warning(
                        "FIXED SL SHOULD BE = %s",
                        self.current_sl
                    )

                    self.replace_stop(
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

            self.handle_closed_position(
                old_size,
                price
            )

            return

        # ----------------------------------------------------
        # FLAT = NO FIXED SL
        # ----------------------------------------------------

        self.current_sl = None
        self.stop_id = None

        # Remove any orphan stops.
        cancel_all_stops(
            self.product_id,
            verify=True
        )

        # ----------------------------------------------------
        # BEFORE 05:45
        # ----------------------------------------------------

        if now < trading_start(
            self.day
        ):

            return

        # ----------------------------------------------------
        # BUILD DAY RANGE
        # ----------------------------------------------------

        if not self.range_ready:

            # If the bot started late, reconstruct the actual
            # range from 05:30 until NOW.
            self.rebuild_current_day_range(
                now
            )

        # ----------------------------------------------------
        # BREAKOUT
        # ----------------------------------------------------

        if self.manual_flat:

            triggered = (
                self.manual_breakout(
                    price
                )
            )

        else:

            triggered = (
                self.breakout(
                    price
                )
            )

        if triggered:

            return

        # ----------------------------------------------------
        # ONLY AFTER BREAKOUT CHECK:
        #
        # update current day high/low.
        #
        # This does NOT change current_sl.
        # ----------------------------------------------------

        self.update_day_range(
            price
        )


    # ========================================================
    # START
    # ========================================================

    def run(
        self
    ):

        logging.warning(
            "================================================"
        )

        logging.warning(
            "XAUTUSD FIXED-SL BOT STARTING"
        )

        logging.warning(
            "VERSION = 12.0"
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
            "NEW DAY HIGH -> LONG"
        )

        logging.warning(
            "NEW DAY LOW  -> SHORT"
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
            "EVERY NEW SL:"
        )

        logging.warning(
            "CANCEL ALL -> VERIFY ZERO -> CREATE ONE"
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

        now = now_ist()

        position = get_position(
            self.product_id
        )

        startup_size = (
            position["size"]
        )

        # ----------------------------------------------------
        # NEW DAY
        # ----------------------------------------------------

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

        # ====================================================
        # START WITH OPEN POSITION
        # ====================================================

        if startup_size != 0:

            logging.warning(
                "================================================"
            )

            logging.warning(
                "STARTED WITH EXISTING POSITION"
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

            # ------------------------------------------------
            # CRITICAL:
            #
            # DO NOT TRUST THE OLD SAVED SL.
            #
            # Rebuild today's actual range.
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

                    self.rebuild_current_day_range(
                        now
                    )

                    price = get_price()

                    if (
                        self.day_high is not None
                        and self.day_low is not None
                    ):

                        if startup_size > 0:

                            startup_sl = (
                                self.day_low
                            )

                        else:

                            startup_sl = (
                                self.day_high
                            )

                        logging.warning(
                            "STARTUP POSITION SL"
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
                            "POSITION = %s",
                            (
                                "LONG"
                                if startup_size > 0
                                else "SHORT"
                            )
                        )

                        logging.warning(
                            "CORRECT SL = %s",
                            startup_sl
                        )

                        # ------------------------------------------------
                        # THIS REMOVES THE WRONG 4486 / 4457
                        # AND CREATES THE CORRECT DAY SL.
                        # ------------------------------------------------

                        if (
                            (
                                startup_size > 0
                                and startup_sl < price
                            )
                            or
                            (
                                startup_size < 0
                                and startup_sl > price
                            )
                        ):

                            self.current_sl = (
                                startup_sl
                            )

                            self.replace_stop(
                                startup_size,
                                startup_sl,
                                price
                            )

                            self.carried_position = False
                            self.overnight_sl_set = True

                        else:

                            logging.error(
                                "STARTUP SL IS ALREADY "
                                "ON THE WRONG SIDE OF PRICE."
                            )

                except Exception as exc:

                    logging.exception(
                        "Startup position setup failed: %s",
                        exc
                    )

            self.persist()

        # ====================================================
        # START FLAT
        # ====================================================

        else:

            logging.warning(
                "STARTED FLAT"
            )

            self.last_position = 0

            self.current_sl = None
            self.stop_id = None

            # ------------------------------------------------
            # ALWAYS clean old stops at startup.
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

            # ------------------------------------------------
            # If trading already started, rebuild actual range.
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

                    self.rebuild_current_day_range(
                        now
                    )

                except Exception as exc:

                    logging.error(
                        "Startup range setup failed: %s",
                        exc
                    )

        # ====================================================
        # MAIN LOOP
        # ====================================================

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
        "Connecting to Delta India..."
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
