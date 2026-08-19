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
# XAUTUSD LIVE BOT
# ============================================================
#
# TRADING DAY:
#   05:30 IST -> next day 05:30 IST
#
# FIRST TRADE:
#   After 05:45:
#       05:30 candle HIGH breakout -> LONG
#       05:30 candle LOW  breakout -> SHORT
#
# AFTER FIRST TRADE:
#       NEW DAY HIGH -> LONG
#       NEW DAY LOW  -> SHORT
#
# STOP LOSS:
#       LONG  -> CURRENT DAY LOW
#       SHORT -> CURRENT DAY HIGH
#
# IMPORTANT:
#   The 05:30 candle is ONLY used as the first breakout
#   reference. It is NOT permanently used as the SL.
#
# MANUAL CLOSE:
#   No immediate re-entry.
#   Bot waits for a NEW day-high/day-low breakout.
#
# SL HIT:
#   Reverse direction immediately.
#
# POSITION SIZE:
#   10% account balance as margin.
#   50x leverage.
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
    ),
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
            "XAUTUSD-DayBreakout-Live-Bot/5.0"
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
        microsecond=0,
    )

    if dt < boundary:
        boundary -= timedelta(days=1)

    return boundary


def opening_end(day):

    return day + timedelta(
        minutes=15
    )


def weekend_block(dt=None):

    dt = dt or now_ist()

    # Saturday from 05:00 IST
    if (
        dt.weekday() == 5
        and dt.time()
        >= datetime.strptime(
            "05:00",
            "%H:%M"
        ).time()
    ):
        return True

    # Sunday
    if dt.weekday() == 6:
        return True

    # Monday before 05:45
    if (
        dt.weekday() == 0
        and dt.time()
        < datetime.strptime(
            "05:45",
            "%H:%M"
        ).time()
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
        "timestamp": timestamp,
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
            ensure_ascii=False,
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
            body_text,
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
            timeout=15,
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


def ticker_price():

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
        auth=True,
    )["result"]

    if not result:

        return {
            "size": 0,
            "entry_price": None,
            "raw": result,
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
        "raw": result,
    }


# ============================================================
# BALANCE
# ============================================================

def get_balance():

    data = api(
        "GET",
        "/v2/wallet/balances",
        auth=True,
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
            ),
        },
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
        "client_order_id": (
            client_id[:32]
        ),
    }

    logging.warning(
        "LIVE MARKET ORDER: %s",
        body
    )

    return api(
        "POST",
        "/v2/orders",
        body=body,
        auth=True,
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
        "size": int(
            size
        ),
        "side": side,
        "order_type": "market_order",
        "stop_order_type": (
            "stop_loss_order"
        ),
        "stop_price": str(
            price
        ),
        "stop_trigger_method": (
            "last_traded_price"
        ),
        "reduce_only": True,
        "client_order_id": (
            client_id[:32]
        ),
    }

    logging.warning(
        "LIVE STOP ORDER: %s",
        body
    )

    return api(
        "POST",
        "/v2/orders",
        body=body,
        auth=True,
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
            auth=True,
        )

    except RuntimeError as exc:

        # A stop may have already triggered/cancelled on Delta.
        # Treat HTTP 404 as already gone instead of retrying forever.
        if "HTTP 404" in str(exc):

            logging.info(
                "Stop/order %s already gone.",
                order_id
            )
            return

        raise


# ============================================================
# OPEN STOP ORDERS
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
            "order_types": "all_stop",
        },
        auth=True,
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
                "Could not cancel stop: %s",
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
        f"/v2/products/"
        f"{product_id}/orders/leverage",
        body={
            "leverage": str(
                LEVERAGE
            )
        },
        auth=True,
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

    # Exactly 10% margin.
    margin = (
        balance
        * BALANCE_FRACTION
    )

    # 50x leverage.
    notional = (
        margin
        * LEVERAGE
    )

    contract_value = dfield(
        product,
        "contract_value",
        "contract_value_usd",
        "contract_unit_value",
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
        default=Decimal("1"),
    )

    minimum = dfield(
        product,
        "min_order_size",
        "minimum_order_size",
        default=lot,
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
        notional,
    )


# ============================================================
# PERSISTENT STATE
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


def to_decimal(
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

        self.day = None

        # 05:30-05:45 candle
        self.opening_high = None
        self.opening_low = None
        self.opening_ready = False

        # ACTUAL running day extremes
        self.day_high = None
        self.day_low = None

        # First trade of current trading day
        self.first_trade_taken = False

        # Timestamp of the first strategy entry for this trading day.
        # This lets the bot distinguish a real first trade from an old
        # or stale state file after deployment/restart.
        self.first_trade_time = None

        # Only identifies whether the current position
        # was the special first position.
        self.first_position = False

        # Manual close protection
        self.manual_flat = False

        # Last exchange position seen by bot
        self.last_position = 0

        # Current protective SL
        self.current_sl = None

        self.stop_id = None

        self.entry_lock = False

        self.state = load_state()

        self.restore_state()


    # ========================================================
    # RESTORE STATE
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

        self.first_trade_taken = bool(
            self.state.get(
                "first_trade_taken",
                False
            )
        )

        self.first_trade_time = self.state.get(
            "first_trade_time"
        )

        self.first_position = bool(
            self.state.get(
                "first_position",
                False
            )
        )

        self.manual_flat = bool(
            self.state.get(
                "manual_flat",
                False
            )
        )

        self.opening_ready = bool(
            self.state.get(
                "opening_ready",
                False
            )
        )

        self.opening_high = to_decimal(
            self.state.get(
                "opening_high"
            )
        )

        self.opening_low = to_decimal(
            self.state.get(
                "opening_low"
            )
        )

        self.day_high = to_decimal(
            self.state.get(
                "day_high"
            )
        )

        self.day_low = to_decimal(
            self.state.get(
                "day_low"
            )
        )


    # ========================================================
    # SAVE STATE
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
                "first_trade_taken":
                    self.first_trade_taken,
                "first_trade_time":
                    self.first_trade_time,
                "first_position":
                    self.first_position,
                "manual_flat":
                    self.manual_flat,
                "opening_ready":
                    self.opening_ready,
                "opening_high": (
                    str(
                        self.opening_high
                    )
                    if self.opening_high is not None
                    else None
                ),
                "opening_low": (
                    str(
                        self.opening_low
                    )
                    if self.opening_low is not None
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
            }
        )


    # ========================================================
    # NEW TRADING DAY
    # ========================================================

    def new_day(
        self,
        now
    ):

        day = trading_day_start(
            now
        )

        if self.day == day:
            return

        logging.warning(
            "=============================================="
        )

        logging.warning(
            "NEW TRADING DAY: %s IST",
            day
        )

        logging.warning(
            "=============================================="
        )

        self.day = day

        self.opening_high = None
        self.opening_low = None
        self.opening_ready = False

        self.day_high = None
        self.day_low = None

        self.first_trade_taken = False
        self.first_trade_time = None
        self.first_position = False

        self.manual_flat = False

        self.current_sl = None
        self.stop_id = None

        self.persist()

        # Rebuild the entire trading day's extremes
        # if enough market data already exists.
        if now > day:

            self.rebuild_day_extremes(
                now
            )

        if now >= opening_end(
            day
        ):

            self.load_opening()


    # ========================================================
    # LOAD OPENING CANDLE
    # ========================================================

    def load_opening(
        self
    ):

        if self.opening_ready:
            return

        if self.day is None:
            return

        end = (
            self.day
            + timedelta(
                minutes=15,
                seconds=1
            )
        )

        rows = candles(
            "15m",
            self.day,
            end
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

            if candle_time == self.day:

                target = row

                break

        if target is None:

            raise RuntimeError(
                "05:30-05:45 candle "
                "was not found."
            )

        self.opening_high = Decimal(
            str(
                target["high"]
            )
        )

        self.opening_low = Decimal(
            str(
                target["low"]
            )
        )

        self.opening_ready = True

        self.persist()

        logging.info(
            "OPENING RANGE FIXED | "
            "HIGH=%s | LOW=%s",
            self.opening_high,
            self.opening_low,
        )


    # ========================================================
    # REBUILD ACTUAL DAY HIGH / LOW
    # ========================================================
    #
    # THIS IS THE IMPORTANT FIX.
    #
    # When bot restarts, we do NOT set day_high/day_low
    # from the current ticker.
    #
    # We rebuild today's actual high and low from 1-minute
    # candles and then include the current price.
    #
    # This prevents:
    #
    #   SL = current price
    #
    # and prevents the bot from forgetting today's
    # previous high/low after restart.
    #
    # ========================================================

    def rebuild_day_extremes(
        self,
        now
    ):

        if self.day is None:
            return

        if now <= self.day:
            return

        try:

            # IMPORTANT:
            # Rebuild ONLY from completed historical candles.
            # Do NOT add the current live price here.
            #
            # The next update_extremes() call must be able to see:
            #
            #     previous_high -> current price = NEW HIGH
            #
            # Otherwise a restart exactly at a breakout would hide
            # the breakout by making the current price the day high first.
            historical_end = now.replace(
                second=0,
                microsecond=0
            )

            rows = candles(
                "1m",
                self.day,
                historical_end
            )

        except Exception as exc:

            logging.error(
                "Could not rebuild day extremes: %s",
                exc
            )

            return

        high = None
        low = None

        current_minute = now.replace(
            second=0,
            microsecond=0
        )

        for row in rows:

            try:

                row_time = datetime.fromtimestamp(
                    int(row["time"]),
                    UTC
                ).astimezone(IST)

                # Ignore the currently forming 1-minute candle.
                # Its high/low may already contain the live breakout.
                if row_time >= current_minute:
                    continue

                candle_high = Decimal(
                    str(row["high"])
                )

                candle_low = Decimal(
                    str(row["low"])
                )

            except (
                KeyError,
                ValueError,
                TypeError
            ):

                continue

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

        if high is not None:
            self.day_high = high

        if low is not None:
            self.day_low = low

        self.persist()

        logging.warning(
            "HISTORICAL DAY RANGE | "
            "HIGH=%s | LOW=%s",
            self.day_high,
            self.day_low,
        )


    # ========================================================
    # UPDATE LIVE DAY EXTREMES
    # ========================================================

    def update_extremes(
        self,
        price
    ):

        previous_high = self.day_high
        previous_low = self.day_low

        if (
            self.day_high is None
            or price > self.day_high
        ):

            self.day_high = price

        if (
            self.day_low is None
            or price < self.day_low
        ):

            self.day_low = price

        if (
            previous_high != self.day_high
            or previous_low != self.day_low
        ):

            self.persist()

        return (
            previous_high,
            previous_low
        )


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
            "stop_trigger_price",
        ):

            value = order.get(
                key
            )

            if value not in (
                None,
                ""
            ):

                try:

                    return Decimal(
                        str(value)
                    )

                except Exception:

                    pass

        return None


    # ========================================================
    # DESIRED SL
    # ========================================================
    #
    # IMPORTANT:
    #
    # The 05:30 candle is NOT the permanent SL.
    #
    # LONG  -> current DAY LOW
    # SHORT -> current DAY HIGH
    #
    # ========================================================

    def desired_sl(
        self,
        size
    ):

        if size > 0:

            return self.day_low

        if size < 0:

            return self.day_high

        return None


    # ========================================================
    # SYNC EXACTLY ONE SL
    # ========================================================

    def sync_sl(
        self,
        size,
        price,
        force=False
    ):

        desired = self.desired_sl(
            size
        )

        if desired is None:

            logging.warning(
                "SL WAITING | "
                "DAY HIGH/LOW NOT READY"
            )

            return

        # LONG SL must be BELOW current price.
        if (
            size > 0
            and desired >= price
        ):

            logging.warning(
                "LONG SL INVALID | "
                "SL=%s | PRICE=%s",
                desired,
                price
            )

            return

        # SHORT SL must be ABOVE current price.
        if (
            size < 0
            and desired <= price
        ):

            logging.warning(
                "SHORT SL INVALID | "
                "SL=%s | PRICE=%s",
                desired,
                price
            )

            return

        expected_side = (
            self.stop_side(
                size
            )
        )

        orders = open_stops(
            self.product_id
        )

        valid = []

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

            order_id = order.get(
                "id"
            )

            if (
                side == expected_side
                and order_price == desired
                and order_id
            ):

                valid.append(
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
                            "extra stop %s: %s",
                            order_id,
                            exc
                        )

        # Keep exactly one correct stop.
        if len(valid) > 1:

            for extra in valid[1:]:

                try:

                    cancel_order(
                        extra.get(
                            "id"
                        )
                    )

                except Exception:

                    pass

            valid = valid[:1]

        # Correct SL already exists.
        if (
            valid
            and not force
            and self.current_sl == desired
        ):

            self.stop_id = (
                valid[0].get(
                    "id"
                )
            )

            return

        # Cancel existing correct SL if replacing.
        for order in valid:

            try:

                cancel_order(
                    order.get(
                        "id"
                    )
                )

            except Exception:

                pass

        result = stop_order(
            self.product_id,
            expected_side,
            abs(size),
            desired,
            "xsl"
            + str(
                int(
                    time.time()
                    * 1000
                )
            ),
        )

        self.current_sl = desired
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
            "ONE SL ACTIVE | "
            "POSITION=%s | "
            "SIDE=%s | "
            "TRIGGER=%s | "
            "ID=%s",
            (
                "LONG"
                if size > 0
                else "SHORT"
            ),
            expected_side,
            desired,
            self.stop_id,
        )


    # ========================================================
    # ENTER
    # ========================================================

    def enter(
        self,
        direction,
        price,
        reason,
        first=False
    ):

        if self.entry_lock:
            return False

        if self.manual_flat:
            return False

        if weekend_block():
            return False

        current = get_position(
            self.product_id
        )

        if current["size"] != 0:

            self.last_position = (
                current["size"]
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
            "=============================================="
        )

        logging.warning(
            "LIVE ENTRY: %s",
            direction
        )

        logging.warning(
            "PRICE=%s",
            price
        )

        logging.warning(
            "SIZE=%s",
            size
        )

        logging.warning(
            "BALANCE=%s",
            balance
        )

        logging.warning(
            "MARGIN=10%% = %s",
            margin
        )

        logging.warning(
            "NOTIONAL=50x = %s",
            notional
        )

        logging.warning(
            "REASON=%s",
            reason
        )

        logging.warning(
            "=============================================="
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
                ),
            )

            for _ in range(30):

                time.sleep(
                    0.2
                )

                position = (
                    get_position(
                        self.product_id
                    )
                )

                correct_fill = (
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

                if correct_fill:

                    self.last_position = (
                        position["size"]
                    )

                    self.first_trade_taken = True

                    if first:
                        self.first_trade_time = now_ist().isoformat()

                    self.first_position = (
                        first
                    )

                    self.manual_flat = False

                    self.persist()

                    self.current_sl = None
                    self.stop_id = None

                    # Rebuild day extremes once more
                    # immediately after entry so the SL
                    # uses the true current day low/high.
                    self.rebuild_day_extremes(
                        now_ist()
                    )

                    live_price = ticker_price()

                    self.sync_sl(
                        position["size"],
                        live_price,
                        force=True
                    )

                    return True

            raise RuntimeError(
                "Entry sent but "
                "fill was not confirmed."
            )

        finally:

            self.entry_lock = False


    # ========================================================
    # DETECT FLAT EVENT
    # ========================================================

    def detect_flat_event(
        self,
        old_size,
        price
    ):

        if old_size == 0:
            return "none"

        # SL event is identified from price crossing
        # the bot's tracked SL.
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
    # HANDLE POSITION CLOSED
    # ========================================================

    def handle_flat(
        self,
        old_size,
        price
    ):

        event = (
            self.detect_flat_event(
                old_size,
                price
            )
        )

        try:

            cancel_all_stops(
                self.product_id
            )

        except Exception:

            pass

        self.current_sl = None
        self.stop_id = None
        self.last_position = 0

        # ----------------------------------------------------
        # MANUAL CLOSE
        # ----------------------------------------------------

        if event == "manual":

            logging.warning(
                "=============================================="
            )

            logging.warning(
                "MANUAL CLOSE DETECTED"
            )

            logging.warning(
                "NO IMMEDIATE RE-ENTRY"
            )

            logging.warning(
                "WAITING FOR NEW DAY HIGH/LOW BREAKOUT"
            )

            logging.warning(
                "=============================================="
            )

            self.manual_flat = True

            self.persist()

            return

        # ----------------------------------------------------
        # SL HIT
        # ----------------------------------------------------

        logging.warning(
            "=============================================="
        )

        logging.warning(
            "STOP LOSS HIT"
        )

        logging.warning(
            "REVERSING DIRECTION"
        )

        logging.warning(
            "=============================================="
        )

        self.manual_flat = False
        self.first_position = False

        self.persist()

        if old_size > 0:

            self.enter(
                "SHORT",
                price,
                "SL HIT: DAY LOW BREAKOUT",
                first=False
            )

        else:

            self.enter(
                "LONG",
                price,
                "SL HIT: DAY HIGH BREAKOUT",
                first=False
            )


    # ========================================================
    # FIRST TRADE
    # ========================================================

    def first_breakout(
        self,
        price
    ):

        if self.first_trade_taken:
            return

        if self.manual_flat:
            return

        if not self.opening_ready:
            return

        if (
            self.opening_high is None
            or self.opening_low is None
        ):
            return

        logging.info(
            "FIRST BREAKOUT CHECK | "
            "PRICE=%s | OPEN_HIGH=%s | OPEN_LOW=%s",
            price,
            self.opening_high,
            self.opening_low,
        )

        # FIRST LONG
        if price > self.opening_high:

            self.enter(
                "LONG",
                price,
                "FIRST TRADE: 05:30-05:45 HIGH BREAKOUT",
                first=True
            )

            return

        # FIRST SHORT
        if price < self.opening_low:

            self.enter(
                "SHORT",
                price,
                "FIRST TRADE: 05:30-05:45 LOW BREAKOUT",
                first=True
            )


    # ========================================================
    # AFTER FIRST TRADE
    # ========================================================

    def post_first_breakout(
        self,
        price,
        previous_high,
        previous_low
    ):

        if not self.first_trade_taken:
            return

        logging.info(
            "DAY BREAKOUT CHECK | "
            "PRICE=%s | PREV_HIGH=%s | PREV_LOW=%s",
            price,
            previous_high,
            previous_low,
        )

        # ----------------------------------------------------
        # NEW DAY HIGH
        # ----------------------------------------------------

        if (
            previous_high is not None
            and price > previous_high
        ):

            self.manual_flat = False

            self.persist()

            self.enter(
                "LONG",
                price,
                "NEW DAY HIGH BREAKOUT",
                first=False
            )

            return

        # ----------------------------------------------------
        # NEW DAY LOW
        # ----------------------------------------------------

        if (
            previous_low is not None
            and price < previous_low
        ):

            self.manual_flat = False

            self.persist()

            self.enter(
                "SHORT",
                price,
                "NEW DAY LOW BREAKOUT",
                first=False
            )


    # ========================================================
    # ONE LOOP
    # ========================================================

    def run_once(
        self
    ):

        now = now_ist()

        # ----------------------------------------------------
        # TRADING DAY
        # ----------------------------------------------------

        self.new_day(
            now
        )

        # ----------------------------------------------------
        # PRICE
        # ----------------------------------------------------

        price = ticker_price()

        # ----------------------------------------------------
        # OPENING CANDLE
        # ----------------------------------------------------

        if (
            now >= opening_end(
                self.day
            )
            and not self.opening_ready
        ):

            self.load_opening()

        # ----------------------------------------------------
        # IMPORTANT BREAKOUT ORDER
        # ----------------------------------------------------
        #
        # We MUST check today's previous high/low BEFORE
        # adding the current live price to day_high/day_low.
        #
        # Example:
        #
        # Previous today's high = 4365
        # Current live price     = 4427
        #
        # The bot must see:
        #
        #     4427 > 4365
        #
        # and enter LONG.
        #
        # If we update day_high first, day_high becomes 4427
        # and the breakout disappears.
        #
        # This is the exact bug we are fixing.
        #
        # ----------------------------------------------------

        if (
            self.day_high is None
            or self.day_low is None
        ):

            self.rebuild_day_extremes(
                now
            )

        previous_high = self.day_high
        previous_low = self.day_low

        # ----------------------------------------------------
        # SATURDAY SQUARE OFF
        # ----------------------------------------------------

        if force_squareoff(
            now
        ):

            position = get_position(
                self.product_id
            )

            size = position[
                "size"
            ]

            if size != 0:

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
                    ),
                )

            return

        # ----------------------------------------------------
        # WEEKEND
        # ----------------------------------------------------

        if weekend_block(
            now
        ):

            return

        # ----------------------------------------------------
        # POSITION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        new_size = position[
            "size"
        ]

        old_size = (
            self.last_position
        )

        # ----------------------------------------------------
        # POSITION APPEARED
        # ----------------------------------------------------

        if (
            old_size == 0
            and new_size != 0
        ):

            logging.warning(
                "OPEN POSITION DETECTED | "
                "SIZE=%s",
                new_size
            )

            # Existing/manual position is NOT considered
            # a new first trade.
            self.first_trade_taken = True
            self.first_position = False
            self.manual_flat = False

            self.last_position = (
                new_size
            )

            self.persist()

            # Make absolutely sure today's actual
            # high/low are available before SL creation.
            self.rebuild_day_extremes(
                now
            )

            self.sync_sl(
                new_size,
                price,
                force=True
            )

            return

        # ----------------------------------------------------
        # POSITION CLOSED
        # ----------------------------------------------------

        if (
            old_size != 0
            and new_size == 0
        ):

            self.handle_flat(
                old_size,
                price
            )

            return

        # ----------------------------------------------------
        # POSITION OPEN
        # ----------------------------------------------------

        if new_size != 0:

            # For an OPEN position, the current price must first
            # become part of today's actual range so the dynamic
            # day HIGH/LOW stop can trail correctly.
            self.update_extremes(
                price
            )

            self.last_position = (
                new_size
            )

            self.sync_sl(
                new_size,
                price
            )

            return

        # ----------------------------------------------------
        # FLAT
        # ----------------------------------------------------

        self.last_position = 0

        # Remove orphan stops.
        orphan_stops = (
            open_stops(
                self.product_id
            )
        )

        if orphan_stops:

            cancel_all_stops(
                self.product_id
            )

            self.current_sl = None
            self.stop_id = None

        # ----------------------------------------------------
        # WAIT UNTIL 05:45
        # ----------------------------------------------------

        if now < opening_end(
            self.day
        ):

            # Before the first trade window is complete,
            # simply build today's running range.
            self.update_extremes(
                price
            )

            return

        if not self.opening_ready:

            self.load_opening()

        # ----------------------------------------------------
        # FIRST TRADE
        # ----------------------------------------------------

        if not self.first_trade_taken:

            self.first_breakout(
                price
            )

            # If first_breakout() entered a position, do NOT
            # update the range here. enter() rebuilds the range
            # and installs the correct SL.
            current_after_entry = get_position(
                self.product_id
            )

            if current_after_entry["size"] != 0:

                return

            # No entry happened. Now this live price becomes
            # part of today's running high/low.
            self.update_extremes(
                price
            )

            return

        # ----------------------------------------------------
        # AFTER FIRST TRADE
        # ----------------------------------------------------
        #
        # THIS IS THE CRITICAL FIX:
        #
        # Compare current price with the PREVIOUS today's
        # running high/low first.
        #
        # Only AFTER the comparison do we update day_high/
        # day_low.
        #
        # Therefore:
        #
        #   previous day high = 4365
        #   current price     = 4427
        #
        # becomes a valid NEW DAY HIGH BREAKOUT.
        #
        # ----------------------------------------------------

        logging.warning(
            "DAY BREAKOUT CHECK | "
            "PRICE=%s | TODAY_PREVIOUS_HIGH=%s | "
            "TODAY_PREVIOUS_LOW=%s",
            price,
            previous_high,
            previous_low,
        )

        self.post_first_breakout(
            price,
            previous_high,
            previous_low
        )

        # If a breakout entered a position, enter() has already
        # rebuilt today's range and installed the SL.
        current_after_entry = get_position(
            self.product_id
        )

        if current_after_entry["size"] != 0:

            return

        # No breakout. Now update today's running range.
        self.update_extremes(
            price
        )


    # ========================================================
    # RUN
    # ========================================================

    def run(
        self
    ):

        logging.warning(
            "=============================================="
        )

        logging.warning(
            "XAUTUSD BOT STARTING"
        )

        logging.warning(
            "10%% BALANCE"
        )

        logging.warning(
            "50x LEVERAGE"
        )

        logging.warning(
            "DAY HIGH/LOW SL MODE"
        )

        logging.warning(
            "=============================================="
        )

        set_leverage(
            self.product_id
        )

        # ----------------------------------------------------
        # STARTUP POSITION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        self.last_position = (
            position["size"]
        )

        # The previous versions of this bot could mark
        # first_trade_taken=True simply because an open position
        # was found at startup. If the bot is currently FLAT and
        # the state file has no real first-trade timestamp, treat
        # that old flag as stale so today's opening-range breakout
        # can actually trigger.
        current_day = trading_day_start(now_ist())

        if (
            position["size"] == 0
            and self.day == current_day
            and self.first_trade_taken
            and not self.first_trade_time
        ):

            logging.warning(
                "LEGACY STATE DETECTED | "
                "RESETTING STALE FIRST-TRADE FLAG"
            )

            self.first_trade_taken = False
            self.first_position = False
            self.manual_flat = False
            self.persist()

        if position["size"] != 0:

            logging.warning(
                "STARTED WITH OPEN POSITION | "
                "SIZE=%s",
                position["size"]
            )

            # An already-open position is managed immediately, but it is
            # NOT automatically counted as the bot's first trade.
            # Preserve first_trade_taken from persistent state if it exists.
            self.first_position = False
            self.manual_flat = False

            self.persist()

            # FIX:
            # Rebuild actual day's high/low BEFORE creating SL.
            current_day = trading_day_start(
                now_ist()
            )

            if self.day != current_day:

                self.day = current_day

                self.opening_high = None
                self.opening_low = None
                self.opening_ready = False

                self.day_high = None
                self.day_low = None

                self.persist()

            self.rebuild_day_extremes(
                now_ist()
            )

            if now_ist() >= opening_end(
                self.day
            ):

                try:

                    self.load_opening()

                except Exception as exc:

                    logging.error(
                        "Opening candle load failed: %s",
                        exc
                    )

            try:

                current_price = (
                    ticker_price()
                )

                logging.warning(
                    "STARTUP DAY RANGE | "
                    "HIGH=%s | LOW=%s",
                    self.day_high,
                    self.day_low
                )

                self.sync_sl(
                    position["size"],
                    current_price,
                    force=True
                )

            except Exception as exc:

                logging.error(
                    "Startup SL setup failed: %s",
                    exc
                )

        else:

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
