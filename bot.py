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
# FIRST ENTRY:
#
#   After 05:45:
#
#   Price > 05:30 candle HIGH
#       -> LONG
#
#   Price < 05:30 candle LOW
#       -> SHORT
#
#
# STOP LOSS:
#
#   IMPORTANT:
#
#   05:30 candle HIGH/LOW is ONLY the first ENTRY trigger.
#
#   IT IS NEVER USED AS STOP LOSS.
#
#   ALL LONG POSITIONS:
#       SL = CURRENT TODAY LOW
#
#   ALL SHORT POSITIONS:
#       SL = CURRENT TODAY HIGH
#
#
# AFTER ANY POSITION:
#
#   NEW TODAY HIGH -> LONG
#   NEW TODAY LOW  -> SHORT
#
#
# MANUAL CLOSE:
#
#   If user manually closes a position:
#
#       Bot remains active.
#
#       It does NOT wait for tomorrow.
#
#       It waits for:
#
#           NEW TODAY HIGH -> LONG
#           NEW TODAY LOW  -> SHORT
#
#
# STOP LOSS:
#
#   LONG SL hit:
#       -> SHORT
#
#   SHORT SL hit:
#       -> LONG
#
#
# POSITION SIZE:
#
#   10% account balance as margin
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
            "XAUTUSD-OpeningRange-Live-Bot/6.0"
        ),
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
        microsecond=0,
    )

    if dt < boundary:

        boundary -= timedelta(
            days=1
        )

    return boundary


def opening_end(
    day
):

    return day + timedelta(
        minutes=15
    )


def weekend_block(
    dt=None
):

    dt = dt or now_ist()

    # Saturday after 05:00
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

    if raw_price is None:

        raise RuntimeError(
            "Ticker returned no price."
        )

    return Decimal(
        str(
            raw_price
        )
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
                    str(
                        value
                    )
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
        "Could not find "
        "USD/USDT balance."
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

        # 404 = order is already gone.
        if "HTTP 404" in str(
            exc
        ):

            logging.info(
                "Stop order %s already gone.",
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
                "Could not cancel stop %s: %s",
                order.get(
                    "id"
                ),
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
                str(
                    value
                )
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

    # 10% balance as margin.
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


def to_decimal(
    value
):

    if value is None:

        return None

    return Decimal(
        str(
            value
        )
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

        # ----------------------------------------------------
        # 05:30 CANDLE
        #
        # ONLY ENTRY TRIGGER.
        # NEVER USED AS SL.
        # ----------------------------------------------------

        self.opening_high = None
        self.opening_low = None
        self.opening_ready = False

        # ----------------------------------------------------
        # TODAY RUNNING EXTREMES
        # ----------------------------------------------------

        self.day_high = None
        self.day_low = None

        # ----------------------------------------------------
        # FIRST TRADE
        # ----------------------------------------------------

        self.first_trade_taken = False
        self.first_position = False

        # ----------------------------------------------------
        # MANUAL CLOSE
        #
        # This does NOT stop trading.
        # It simply records that we are flat after manual exit.
        # ----------------------------------------------------

        self.manual_flat = False

        self.last_position = 0

        # Current protective SL.
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
        self.first_position = False

        self.manual_flat = False

        self.current_sl = None
        self.stop_id = None

        self.persist()

        if now >= opening_end(
            day
        ):

            self.load_opening()

            self.rebuild_today_extremes()


    # ========================================================
    # LOAD 05:30-05:45 CANDLE
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
    # REBUILD TODAY HIGH/LOW
    # ========================================================

    def rebuild_today_extremes(
        self
    ):

        if self.day is None:

            return

        if not self.opening_ready:

            return

        now = now_ist()

        if now < opening_end(
            self.day
        ):

            return

        # Current running 15m candle is excluded.
        # Its live price is handled separately.
        current_candle_start = (
            now.replace(
                minute=(
                    now.minute
                    // 15
                ) * 15,
                second=0,
                microsecond=0,
            )
        )

        rows = candles(
            "15m",
            self.day,
            current_candle_start
        )

        high = self.opening_high
        low = self.opening_low

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

            if candle_high > high:

                high = candle_high

            if candle_low < low:

                low = candle_low

        self.day_high = high
        self.day_low = low

        self.persist()

        logging.warning(
            "TODAY RANGE REBUILT | "
            "HIGH=%s | LOW=%s",
            self.day_high,
            self.day_low
        )


    # ========================================================
    # UPDATE TODAY RANGE
    #
    # Returns previous high/low BEFORE changing them.
    # ========================================================

    def update_today_range(
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

            logging.info(
                "TODAY RANGE UPDATED | "
                "HIGH=%s | LOW=%s | PRICE=%s",
                self.day_high,
                self.day_low,
                price
            )

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
                        str(
                            value
                        )
                    )

                except Exception:

                    pass

        return None


    # ========================================================
    # DESIRED STOP LOSS
    #
    # THIS IS THE IMPORTANT FIX.
    #
    # 05:30 candle is NEVER used here.
    #
    # LONG:
    #   SL = TODAY LOW
    #
    # SHORT:
    #   SL = TODAY HIGH
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
                "SL NOT READY | "
                "POSITION=%s | "
                "TODAY HIGH=%s | "
                "TODAY LOW=%s",
                size,
                self.day_high,
                self.day_low
            )

            return

        # LONG SL must be BELOW current price.
        if (
            size > 0
            and desired >= price
        ):

            logging.warning(
                "LONG SL INVALID | "
                "TODAY LOW=%s | "
                "PRICE=%s",
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
                "TODAY HIGH=%s | "
                "PRICE=%s",
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

        # Keep exactly one matching stop.
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

        # Correct stop already exists.
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

        # Remove existing matching stop if replacing.
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
            "=============================================="
        )

        logging.warning(
            "ONE SL ACTIVE"
        )

        logging.warning(
            "POSITION=%s",
            (
                "LONG"
                if size > 0
                else "SHORT"
            )
        )

        logging.warning(
            "TODAY HIGH=%s",
            self.day_high
        )

        logging.warning(
            "TODAY LOW=%s",
            self.day_low
        )

        logging.warning(
            "SL=%s",
            desired
        )

        logging.warning(
            "ID=%s",
            self.stop_id
        )

        logging.warning(
            "=============================================="
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

        # ----------------------------------------------------
        # VERY IMPORTANT:
        #
        # Before entering, today's range must exist.
        # ----------------------------------------------------

        if (
            self.day_high is None
            or self.day_low is None
        ):

            self.rebuild_today_extremes()

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
            "TODAY HIGH=%s",
            self.day_high
        )

        logging.warning(
            "TODAY LOW=%s",
            self.day_low
        )

        logging.warning(
            "SL WILL BE=%s",
            (
                self.day_low
                if direction == "LONG"
                else self.day_high
            )
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

            # Wait for actual fill.
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

                    self.first_position = first

                    self.manual_flat = False

                    self.persist()

                    self.current_sl = None
                    self.stop_id = None

                    actual_price = (
                        ticker_price()
                    )

                    # ALL positions use today's high/low.
                    self.sync_sl(
                        position["size"],
                        actual_price,
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

        # If price crossed the tracked SL,
        # classify it as SL.
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

        # Otherwise user manually closed it.
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
                "POSITION IS FLAT"
            )

            logging.warning(
                "BOT REMAINS ACTIVE"
            )

            logging.warning(
                "WAITING FOR NEW TODAY HIGH/LOW"
            )

            logging.warning(
                "TODAY HIGH=%s | TODAY LOW=%s",
                self.day_high,
                self.day_low
            )

            logging.warning(
                "=============================================="
            )

            self.manual_flat = True

            self.first_position = False

            self.persist()

            return


        # ----------------------------------------------------
        # STOP LOSS
        # ----------------------------------------------------

        logging.warning(
            "=============================================="
        )

        logging.warning(
            "STOP LOSS HIT"
        )

        logging.warning(
            "REVERSING POSITION"
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
                "SL HIT: TODAY LOW BREAKOUT",
                first=False
            )

        else:

            self.enter(
                "LONG",
                price,
                "SL HIT: TODAY HIGH BREAKOUT",
                first=False
            )


    # ========================================================
    # FIRST ENTRY
    #
    # 05:30 CANDLE IS USED ONLY HERE.
    # ========================================================

    def first_breakout(
        self,
        price
    ):

        if self.first_trade_taken:

            return

        if not self.opening_ready:

            return

        if (
            self.opening_high is None
            or self.opening_low is None
        ):

            return

        # ----------------------------------------------------
        # FIRST LONG
        # ----------------------------------------------------

        if price > self.opening_high:

            logging.warning(
                "=============================================="
            )

            logging.warning(
                "FIRST LONG BREAKOUT"
            )

            logging.warning(
                "PRICE=%s",
                price
            )

            logging.warning(
                "05:30 HIGH=%s",
                self.opening_high
            )

            logging.warning(
                "TODAY LOW=%s",
                self.day_low
            )

            logging.warning(
                "SL WILL BE TODAY LOW=%s",
                self.day_low
            )

            logging.warning(
                "=============================================="
            )

            self.enter(
                "LONG",
                price,
                "FIRST TRADE: 05:30 HIGH BREAKOUT",
                first=True
            )

            return

        # ----------------------------------------------------
        # FIRST SHORT
        # ----------------------------------------------------

        if price < self.opening_low:

            logging.warning(
                "=============================================="
            )

            logging.warning(
                "FIRST SHORT BREAKOUT"
            )

            logging.warning(
                "PRICE=%s",
                price
            )

            logging.warning(
                "05:30 LOW=%s",
                self.opening_low
            )

            logging.warning(
                "TODAY HIGH=%s",
                self.day_high
            )

            logging.warning(
                "SL WILL BE TODAY HIGH=%s",
                self.day_high
            )

            logging.warning(
                "=============================================="
            )

            self.enter(
                "SHORT",
                price,
                "FIRST TRADE: 05:30 LOW BREAKOUT",
                first=True
            )


    # ========================================================
    # POST-FIRST BREAKOUT
    #
    # Works after:
    #   - normal exit
    #   - manual exit
    #   - restart
    #
    # Current price is compared against TODAY'S PREVIOUS
    # EXTREME before updating the extreme.
    # ========================================================

    def post_first_breakout(
        self,
        price,
        previous_high,
        previous_low
    ):

        if not self.first_trade_taken:

            return

        # ----------------------------------------------------
        # NEW TODAY HIGH
        # ----------------------------------------------------

        if (
            previous_high is not None
            and price > previous_high
        ):

            logging.warning(
                "=============================================="
            )

            logging.warning(
                "NEW TODAY HIGH BREAKOUT"
            )

            logging.warning(
                "PREVIOUS TODAY HIGH=%s",
                previous_high
            )

            logging.warning(
                "CURRENT PRICE=%s",
                price
            )

            logging.warning(
                "TODAY LOW=%s",
                self.day_low
            )

            logging.warning(
                "ENTERING LONG"
            )

            logging.warning(
                "LONG SL WILL BE TODAY LOW=%s",
                self.day_low
            )

            logging.warning(
                "=============================================="
            )

            self.manual_flat = False

            self.persist()

            self.enter(
                "LONG",
                price,
                "NEW TODAY HIGH BREAKOUT",
                first=False
            )

            return

        # ----------------------------------------------------
        # NEW TODAY LOW
        # ----------------------------------------------------

        if (
            previous_low is not None
            and price < previous_low
        ):

            logging.warning(
                "=============================================="
            )

            logging.warning(
                "NEW TODAY LOW BREAKOUT"
            )

            logging.warning(
                "PREVIOUS TODAY LOW=%s",
                previous_low
            )

            logging.warning(
                "CURRENT PRICE=%s",
                price
            )

            logging.warning(
                "TODAY HIGH=%s",
                self.day_high
            )

            logging.warning(
                "ENTERING SHORT"
            )

            logging.warning(
                "SHORT SL WILL BE TODAY HIGH=%s",
                self.day_high
            )

            logging.warning(
                "=============================================="
            )

            self.manual_flat = False

            self.persist()

            self.enter(
                "SHORT",
                price,
                "NEW TODAY LOW BREAKOUT",
                first=False
            )


    # ========================================================
    # STATUS
    # ========================================================

    def status_log(
        self,
        price,
        position_size
    ):

        if position_size > 0:

            position_name = "LONG"

        elif position_size < 0:

            position_name = "SHORT"

        else:

            position_name = "FLAT"

        logging.info(
            "STATUS | "
            "PRICE=%s | "
            "TODAY_HIGH=%s | "
            "TODAY_LOW=%s | "
            "POSITION=%s | "
            "SL=%s | "
            "FIRST_TRADE=%s | "
            "MANUAL_FLAT=%s",
            price,
            self.day_high,
            self.day_low,
            position_name,
            self.current_sl,
            self.first_trade_taken,
            self.manual_flat
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
        # TODAY RANGE
        # ----------------------------------------------------

        if (
            self.day_high is None
            or self.day_low is None
        ):

            if now >= opening_end(
                self.day
            ):

                self.rebuild_today_extremes()

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

                logging.warning(
                    "SATURDAY SQUARE OFF | SIZE=%s",
                    size
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

        old_size = self.last_position

        # ----------------------------------------------------
        # POSITION APPEARED
        # ----------------------------------------------------

        if (
            old_size == 0
            and new_size != 0
        ):

            logging.warning(
                "OPEN POSITION DETECTED | SIZE=%s",
                new_size
            )

            self.first_trade_taken = True

            self.first_position = False

            self.manual_flat = False

            self.last_position = new_size

            self.persist()

            try:

                self.sync_sl(
                    new_size,
                    price,
                    force=True
                )

            except Exception as exc:

                logging.error(
                    "Could not setup SL: %s",
                    exc
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

            self.last_position = new_size

            try:

                self.sync_sl(
                    new_size,
                    price
                )

            except Exception as exc:

                logging.error(
                    "SL sync error: %s",
                    exc
                )

            self.status_log(
                price,
                new_size
            )

            return

        # ----------------------------------------------------
        # FLAT
        # ----------------------------------------------------

        self.last_position = 0

        # Remove orphan stops.
        try:

            orphan_stops = open_stops(
                self.product_id
            )

            if orphan_stops:

                cancel_all_stops(
                    self.product_id
                )

                self.current_sl = None
                self.stop_id = None

        except Exception as exc:

            logging.error(
                "Orphan stop cleanup error: %s",
                exc
            )

        # ----------------------------------------------------
        # BEFORE OPENING RANGE
        # ----------------------------------------------------

        if now < opening_end(
            self.day
        ):

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

            return

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # SAVE TODAY'S OLD HIGH/LOW FIRST.
        #
        # Then check whether current price broke them.
        # ----------------------------------------------------

        previous_high = self.day_high
        previous_low = self.day_low

        # ----------------------------------------------------
        # NEW TODAY HIGH / LOW
        # ----------------------------------------------------

        self.post_first_breakout(
            price,
            previous_high,
            previous_low
        )

        # ----------------------------------------------------
        # ONLY AFTER BREAKOUT CHECK:
        # update today's extremes.
        # ----------------------------------------------------

        if (
            self.day_high is None
            or price > self.day_high
        ):

            self.day_high = price

            self.persist()

        if (
            self.day_low is None
            or price < self.day_low
        ):

            self.day_low = price

            self.persist()

        self.status_log(
            price,
            0
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
            "=============================================="
        )

        logging.warning(
            "ENTRY:"
        )

        logging.warning(
            "05:30 HIGH/LOW = FIRST ENTRY TRIGGER"
        )

        logging.warning(
            "=============================================="
        )

        logging.warning(
            "STOP LOSS:"
        )

        logging.warning(
            "LONG  = TODAY LOW"
        )

        logging.warning(
            "SHORT = TODAY HIGH"
        )

        logging.warning(
            "=============================================="
        )

        logging.warning(
            "MANUAL EXIT:"
        )

        logging.warning(
            "CONTINUE TODAY"
        )

        logging.warning(
            "NEW TODAY HIGH/LOW = NEXT ENTRY"
        )

        logging.warning(
            "=============================================="
        )

        set_leverage(
            self.product_id
        )

        # ----------------------------------------------------
        # INITIALIZE CURRENT DAY
        # ----------------------------------------------------

        now = now_ist()

        self.new_day(
            now
        )

        if now >= opening_end(
            self.day
        ):

            self.load_opening()

            self.rebuild_today_extremes()

        # ----------------------------------------------------
        # STARTUP POSITION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        self.last_position = (
            position["size"]
        )

        if position["size"] != 0:

            logging.warning(
                "STARTED WITH OPEN POSITION | "
                "SIZE=%s",
                position["size"]
            )

            # Existing position means first trade already
            # happened.
            self.first_trade_taken = True

            self.first_position = False

            self.manual_flat = False

            self.persist()

            try:

                price = ticker_price()

                self.sync_sl(
                    position["size"],
                    price,
                    force=True
                )

            except Exception as exc:

                logging.error(
                    "Startup SL setup failed: %s",
                    exc
                )

        else:

            logging.warning(
                "STARTED FLAT"
            )

            logging.warning(
                "TODAY HIGH=%s",
                self.day_high
            )

            logging.warning(
                "TODAY LOW=%s",
                self.day_low
            )

            logging.warning(
                "FIRST TRADE TAKEN=%s",
                self.first_trade_taken
            )

            logging.warning(
                "MANUAL FLAT=%s",
                self.manual_flat
            )

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
