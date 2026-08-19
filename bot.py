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
# STRATEGY
#
# TRADING DAY:
#   05:30 IST -> next day 05:30 IST
#
# FIRST ENTRY:
#   05:30-05:45 candle HIGH breaks -> LONG
#   05:30-05:45 candle LOW  breaks -> SHORT
#
# IMPORTANT:
#   The 05:30 candle can trigger ONLY ONE first trade.
#   Once its breakout is consumed, it can NEVER trigger
#   another trade during that trading day.
#
# INTRADAY:
#   LONG  -> DAY LOW is SL
#   SHORT -> DAY HIGH is SL
#
# NO PROFIT TARGET.
#
# OVERNIGHT:
#   If LONG survives into next day:
#       after new 05:30-05:45 candle closes,
#       its LOW becomes the new day's starting SL.
#
#   If SHORT survives into next day:
#       after new 05:30-05:45 candle closes,
#       its HIGH becomes the new day's starting SL.
#
#   After that, the new day's running LOW/HIGH continues
#   to be used as the SL.
#
# SL REVERSAL:
#   LONG SL hit  -> SHORT
#   SHORT SL hit -> LONG
#
#   Reversal does NOT repeatedly trigger from the same level.
#
# MANUAL CLOSE:
#   If user manually closes a position:
#       no immediate re-entry.
#
#   Bot waits for a NEW day high or NEW day low.
#
#   The same already-broken level cannot trigger again.
#
# POSITION SIZE:
#   10% balance as margin
#   50x leverage
#
# WEEKEND:
#   Friday/Saturday 05:00 square-off.
#   No trading Saturday/Sunday.
#   Monday starts after 05:45.
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
            "XAUTUSD-OpeningRange-Live-Bot/8.0"
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

        boundary -= timedelta(
            days=1
        )

    return boundary


def opening_end(day):

    return day + timedelta(
        minutes=15
    )


def weekend_block(dt=None):

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
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "client_order_id": client_id[:32],
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
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(price),
        "stop_trigger_method": "last_traded_price",
        "reduce_only": True,
        "client_order_id": client_id[:32],
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

        # Order already disappeared from exchange.
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

        return [result]

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


def dec(
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

        # Current trading day.
        self.day = None

        # 05:30-05:45 opening candle.
        self.opening_high = None
        self.opening_low = None
        self.opening_ready = False

        # Running current-day extremes.
        self.day_high = None
        self.day_low = None

        # ----------------------------------------------------
        # BREAKOUT LOCKS
        # ----------------------------------------------------

        # Opening candle can trigger ONLY ONE first trade.
        self.opening_breakout_used = False

        # After manual close / while flat, these prevent the
        # exact same breakout from being used again.
        self.high_breakout_used = False
        self.low_breakout_used = False

        # ----------------------------------------------------
        # POSITION STATE
        # ----------------------------------------------------

        self.first_trade_taken = False

        # True only if current position was the opening-range
        # first entry.
        self.first_position = False

        # Manual close state.
        self.manual_flat = False

        self.last_position = 0

        # Current active exchange SL.
        self.current_sl = None
        self.stop_id = None

        self.entry_lock = False

        # Existing position carried into a new day.
        self.carried_into_day = False

        # New 05:30 candle has been applied as the initial
        # SL for an overnight position.
        self.overnight_candle_applied = False

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

        self.opening_high = dec(
            self.state.get(
                "opening_high"
            )
        )

        self.opening_low = dec(
            self.state.get(
                "opening_low"
            )
        )

        self.opening_ready = bool(
            self.state.get(
                "opening_ready",
                False
            )
        )

        self.day_high = dec(
            self.state.get(
                "day_high"
            )
        )

        self.day_low = dec(
            self.state.get(
                "day_low"
            )
        )

        self.opening_breakout_used = bool(
            self.state.get(
                "opening_breakout_used",
                False
            )
        )

        self.high_breakout_used = bool(
            self.state.get(
                "high_breakout_used",
                False
            )
        )

        self.low_breakout_used = bool(
            self.state.get(
                "low_breakout_used",
                False
            )
        )

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

        self.carried_into_day = bool(
            self.state.get(
                "carried_into_day",
                False
            )
        )

        self.overnight_candle_applied = bool(
            self.state.get(
                "overnight_candle_applied",
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

                "opening_ready":
                    self.opening_ready,

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

                "opening_breakout_used":
                    self.opening_breakout_used,

                "high_breakout_used":
                    self.high_breakout_used,

                "low_breakout_used":
                    self.low_breakout_used,

                "first_trade_taken":
                    self.first_trade_taken,

                "first_position":
                    self.first_position,

                "manual_flat":
                    self.manual_flat,

                "carried_into_day":
                    self.carried_into_day,

                "overnight_candle_applied":
                    self.overnight_candle_applied,
            }
        )


    # ========================================================
    # NEW TRADING DAY
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
            "=============================================="
        )

        logging.warning(
            "NEW TRADING DAY: %s IST",
            new_day
        )

        logging.warning(
            "=============================================="
        )

        self.day = new_day

        # New opening candle must be loaded.
        self.opening_high = None
        self.opening_low = None
        self.opening_ready = False

        # New day range starts from opening candle.
        self.day_high = None
        self.day_low = None

        # VERY IMPORTANT:
        #
        # Opening candle gets one fresh chance each day.
        #
        # But if an existing position is being carried,
        # this opening candle is NOT an entry trigger.
        if existing_position != 0:

            self.opening_breakout_used = True

            self.first_trade_taken = True

            self.first_position = False

            self.manual_flat = False

            self.carried_into_day = True

            self.overnight_candle_applied = False

            logging.warning(
                "POSITION CARRIED INTO NEW DAY | "
                "SIZE=%s",
                existing_position
            )

        else:

            self.opening_breakout_used = False

            self.first_trade_taken = False

            self.first_position = False

            self.manual_flat = False

            self.carried_into_day = False

            self.overnight_candle_applied = False

            # These are reset because they belong to the
            # current trading day's new-breakout logic.
            self.high_breakout_used = False
            self.low_breakout_used = False

        self.current_sl = None
        self.stop_id = None

        self.persist()

        if now >= opening_end(
            new_day
        ):

            self.load_opening()

            if existing_position != 0:

                self.apply_opening_to_new_day_range()


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

        logging.warning(
            "OPENING RANGE FIXED | "
            "HIGH=%s | LOW=%s",
            self.opening_high,
            self.opening_low
        )


    # ========================================================
    # APPLY NEW OPENING CANDLE TO CARRIED POSITION
    # ========================================================

    def apply_opening_to_new_day_range(
        self
    ):

        if not self.opening_ready:
            return

        self.day_high = self.opening_high
        self.day_low = self.opening_low

        self.overnight_candle_applied = True

        self.persist()

        logging.warning(
            "=============================================="
        )

        logging.warning(
            "OVERNIGHT POSITION NEW SL RANGE"
        )

        logging.warning(
            "05:30 CANDLE HIGH=%s",
            self.opening_high
        )

        logging.warning(
            "05:30 CANDLE LOW=%s",
            self.opening_low
        )

        logging.warning(
            "=============================================="
        )


    # ========================================================
    # UPDATE DAY EXTREMES
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
    # REBUILD RANGE
    # ========================================================

    def rebuild_day_range(
        self
    ):

        if self.day is None:
            return

        if not self.opening_ready:
            return

        high = self.opening_high
        low = self.opening_low

        now = now_ist()

        if now > opening_end(
            self.day
        ):

            end = now.replace(
                second=0,
                microsecond=0
            )

            rows = candles(
                "15m",
                self.day,
                end
            )

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

        logging.info(
            "DAY RANGE | HIGH=%s | LOW=%s",
            self.day_high,
            self.day_low
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
    # READ STOP PRICE
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
    # SYNC ONE STOP
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

            return

        # LONG SL must remain below price.
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

        # SHORT SL must remain above price.
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

        # Existing correct stop.
        if (
            len(valid) == 1
            and not force
            and self.current_sl == desired
        ):

            self.stop_id = valid[0].get(
                "id"
            )

            return

        # Replace existing correct stop when forced.
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
            "SL ACTIVE | %s | SL=%s | DAY HIGH=%s | DAY LOW=%s",
            (
                "LONG"
                if size > 0
                else "SHORT"
            ),
            desired,
            self.day_high,
            self.day_low
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

        if (
            self.day_high is None
            or self.day_low is None
        ):

            self.rebuild_day_range()

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
            "DAY HIGH=%s | DAY LOW=%s",
            self.day_high,
            self.day_low
        )

        logging.warning(
            "SIZE=%s | BALANCE=%s",
            size,
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

                    self.last_position = (
                        position["size"]
                    )

                    self.first_trade_taken = True
                    self.first_position = first
                    self.manual_flat = False

                    self.persist()

                    self.current_sl = None
                    self.stop_id = None

                    actual_price = get_price()

                    self.sync_sl(
                        position["size"],
                        actual_price,
                        force=True
                    )

                    return True

            raise RuntimeError(
                "Entry sent but fill "
                "was not confirmed."
            )

        finally:

            self.entry_lock = False


    # ========================================================
    # FIRST OPENING BREAKOUT
    # ========================================================

    def opening_breakout(
        self,
        price
    ):

        if self.opening_breakout_used:
            return False

        if not self.opening_ready:
            return False

        # ----------------------------------------------------
        # HIGH BREAK -> LONG
        # ----------------------------------------------------

        if price > self.opening_high:

            logging.warning(
                "=============================================="
            )

            logging.warning(
                "OPENING HIGH BREAKOUT"
            )

            logging.warning(
                "PRICE=%s > HIGH=%s",
                price,
                self.opening_high
            )

            logging.warning(
                "=============================================="
            )

            success = self.enter(
                "LONG",
                price,
                "05:30-05:45 HIGH BREAKOUT",
                first=True
            )

            if success:

                # CONSUME THE OPENING BREAKOUT.
                #
                # This is the critical protection against
                # repeated trades from the same candle.
                self.opening_breakout_used = True
                self.high_breakout_used = True

                self.persist()

            return success

        # ----------------------------------------------------
        # LOW BREAK -> SHORT
        # ----------------------------------------------------

        if price < self.opening_low:

            logging.warning(
                "=============================================="
            )

            logging.warning(
                "OPENING LOW BREAKOUT"
            )

            logging.warning(
                "PRICE=%s < LOW=%s",
                price,
                self.opening_low
            )

            logging.warning(
                "=============================================="
            )

            success = self.enter(
                "SHORT",
                price,
                "05:30-05:45 LOW BREAKOUT",
                first=True
            )

            if success:

                self.opening_breakout_used = True
                self.low_breakout_used = True

                self.persist()

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

        # ----------------------------------------------------
        # NEW HIGH
        #
        # day_high is the previous recorded extreme.
        # We compare FIRST and update AFTER.
        # ----------------------------------------------------

        if (
            not self.high_breakout_used
            and self.day_high is not None
            and price > self.day_high
        ):

            previous_high = self.day_high

            success = self.enter(
                "LONG",
                price,
                "MANUAL CLOSE -> NEW DAY HIGH",
                first=False
            )

            if success:

                self.manual_flat = False

                # Consume this breakout.
                self.high_breakout_used = True

                logging.warning(
                    "NEW DAY HIGH BREAKOUT CONSUMED | "
                    "OLD HIGH=%s | ENTRY=%s",
                    previous_high,
                    price
                )

                self.persist()

            return success

        # ----------------------------------------------------
        # NEW LOW
        # ----------------------------------------------------

        if (
            not self.low_breakout_used
            and self.day_low is not None
            and price < self.day_low
        ):

            previous_low = self.day_low

            success = self.enter(
                "SHORT",
                price,
                "MANUAL CLOSE -> NEW DAY LOW",
                first=False
            )

            if success:

                self.manual_flat = False

                self.low_breakout_used = True

                logging.warning(
                    "NEW DAY LOW BREAKOUT CONSUMED | "
                    "OLD LOW=%s | ENTRY=%s",
                    previous_low,
                    price
                )

                self.persist()

            return success

        return False


    # ========================================================
    # POST FIRST TRADE BREAKOUT
    #
    # Used when flat but strategy is already past the
    # opening trade.
    #
    # A breakout is checked BEFORE updating day extremes.
    #
    # Therefore:
    #
    # old high = 4400
    # price     = 4401
    #
    # -> one LONG.
    #
    # After that day_high becomes 4401.
    #
    # Price staying at 4401/4402 cannot repeatedly enter
    # because we are no longer flat once entry succeeds.
    #
    # If manually closed at 4401:
    # day_high is already 4401.
    # Price must exceed 4401 to enter again.
    # ========================================================

    def post_first_breakout(
        self,
        price
    ):

        if self.day_high is None:
            return False

        if self.day_low is None:
            return False

        # ----------------------------------------------------
        # NEW HIGH
        # ----------------------------------------------------

        if (
            not self.high_breakout_used
            and price > self.day_high
        ):

            previous_high = self.day_high

            success = self.enter(
                "LONG",
                price,
                "NEW DAY HIGH BREAKOUT",
                first=False
            )

            if success:

                self.high_breakout_used = True

                logging.warning(
                    "HIGH BREAKOUT CONSUMED | "
                    "OLD HIGH=%s | ENTRY=%s",
                    previous_high,
                    price
                )

                self.persist()

            return success

        # ----------------------------------------------------
        # NEW LOW
        # ----------------------------------------------------

        if (
            not self.low_breakout_used
            and price < self.day_low
        ):

            previous_low = self.day_low

            success = self.enter(
                "SHORT",
                price,
                "NEW DAY LOW BREAKOUT",
                first=False
            )

            if success:

                self.low_breakout_used = True

                logging.warning(
                    "LOW BREAKOUT CONSUMED | "
                    "OLD LOW=%s | ENTRY=%s",
                    previous_low,
                    price
                )

                self.persist()

            return success

        return False


    # ========================================================
    # DETECT CLOSED POSITION
    # ========================================================

    def detect_close_reason(
        self,
        old_size,
        price
    ):

        if old_size == 0:
            return "none"

        # If price crossed our known SL, treat as SL.
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

        reason = (
            self.detect_close_reason(
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

        if reason == "manual":

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
                "WAITING FOR NEW DAY HIGH/LOW"
            )

            logging.warning(
                "=============================================="
            )

            self.manual_flat = True
            self.first_position = False

            # IMPORTANT:
            #
            # We DO NOT reset day_high/day_low.
            #
            # Therefore the same price cannot immediately
            # trigger another trade.
            #
            # A genuinely NEW high/low is required.

            self.persist()

            return


        # ----------------------------------------------------
        # SL HIT -> REVERSE
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

        # Reversal is a new position.
        self.carried_into_day = False
        self.overnight_candle_applied = True

        # Make sure the current price is reflected in the
        # current day's range BEFORE selecting the opposite
        # stop.
        self.update_day_extremes(
            price
        )

        self.persist()

        # ----------------------------------------------------
        # LONG -> SHORT
        #
        # SHORT SL = current DAY HIGH
        # ----------------------------------------------------

        if old_size > 0:

            self.enter(
                "SHORT",
                price,
                "LONG SL HIT -> REVERSE SHORT",
                first=False
            )

        # ----------------------------------------------------
        # SHORT -> LONG
        #
        # LONG SL = current DAY LOW
        # ----------------------------------------------------

        else:

            self.enter(
                "LONG",
                price,
                "SHORT SL HIT -> REVERSE LONG",
                first=False
            )


    # ========================================================
    # APPLY OVERNIGHT OPENING CANDLE
    # ========================================================

    def apply_overnight_sl(
        self,
        position_size,
        price
    ):

        if not self.carried_into_day:
            return

        if self.overnight_candle_applied:
            return

        if not self.opening_ready:
            return

        if now_ist() < opening_end(
            self.day
        ):
            return

        # New day starts its range from the 05:30 candle.
        self.day_high = self.opening_high
        self.day_low = self.opening_low

        self.overnight_candle_applied = True

        self.persist()

        logging.warning(
            "=============================================="
        )

        logging.warning(
            "OVERNIGHT SL UPDATED"
        )

        if position_size > 0:

            logging.warning(
                "CARRIED LONG"
            )

            logging.warning(
                "NEW SL = 05:30 CANDLE LOW = %s",
                self.opening_low
            )

        else:

            logging.warning(
                "CARRIED SHORT"
            )

            logging.warning(
                "NEW SL = 05:30 CANDLE HIGH = %s",
                self.opening_high
            )

        logging.warning(
            "=============================================="
        )

        self.current_sl = None
        self.stop_id = None

        self.sync_sl(
            position_size,
            price,
            force=True
        )


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
        # DAY CHANGE
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
        # LOAD OPENING CANDLE
        # ----------------------------------------------------

        if (
            now >= opening_end(
                self.day
            )
            and not self.opening_ready
        ):

            self.load_opening()

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
                    "FRIDAY/SATURDAY SQUARE OFF | SIZE=%s",
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
        # CURRENT POSITION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        new_size = position["size"]

        old_size = self.last_position

        # ----------------------------------------------------
        # POSITION IS OPEN
        # ----------------------------------------------------

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

                self.first_trade_taken = True
                self.first_position = False
                self.manual_flat = False

                self.last_position = new_size

                self.persist()

                self.sync_sl(
                    new_size,
                    price,
                    force=True
                )

                return

            # ------------------------------------------------
            # OVERNIGHT POSITION
            # ------------------------------------------------

            if (
                self.carried_into_day
                and not self.overnight_candle_applied
                and now >= opening_end(
                    self.day
                )
            ):

                self.apply_overnight_sl(
                    new_size,
                    price
                )

                self.last_position = new_size

                return

            # ------------------------------------------------
            # NORMAL POSITION
            # ------------------------------------------------

            self.last_position = new_size

            # Update current day range.
            #
            # For an overnight position, the opening candle
            # was already initialized first.
            self.update_day_extremes(
                price
            )

            self.sync_sl(
                new_size,
                price
            )

            return

        # ----------------------------------------------------
        # POSITION IS FLAT
        # ----------------------------------------------------

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
        # FLAT - CLEAN ORPHAN STOPS
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

        if now < opening_end(
            self.day
        ):

            return

        # ----------------------------------------------------
        # OPENING CANDLE
        # ----------------------------------------------------

        if not self.opening_ready:

            self.load_opening()

        # ----------------------------------------------------
        # MANUAL CLOSE MODE
        # ----------------------------------------------------

        if self.manual_flat:

            # IMPORTANT:
            #
            # Check breakout BEFORE updating day extremes.
            #
            # This means:
            #
            # day_high = 4400
            # price    = 4401
            #
            # -> NEW HIGH BREAKOUT.
            #
            # Once entered, day_high becomes 4401.
            #
            # If manually closed at 4401:
            # price must go > 4401 before another LONG.
            #

            triggered = (
                self.manual_breakout(
                    price
                )
            )

            # If no trade happened, update the range.
            if not triggered:

                self.update_day_extremes(
                    price
                )

            return

        # ----------------------------------------------------
        # FIRST TRADE OF THE DAY
        # ----------------------------------------------------

        if not self.opening_breakout_used:

            # IMPORTANT:
            #
            # Check opening breakout BEFORE updating the
            # current day range.
            #
            # This guarantees that price crossing the opening
            # HIGH/LOW at 05:45 is detected.
            #

            triggered = (
                self.opening_breakout(
                    price
                )
            )

            if triggered:

                return

            # No opening breakout yet.
            #
            # Update today's range after checking.
            self.update_day_extremes(
                price
            )

            return

        # ----------------------------------------------------
        # OPENING BREAKOUT ALREADY CONSUMED
        #
        # We are flat because of manual close or some other
        # flat state.
        #
        # Only a NEW day HIGH/LOW can trigger.
        # ----------------------------------------------------

        triggered = (
            self.post_first_breakout(
                price
            )
        )

        if triggered:

            return

        # No breakout.
        #
        # Update extremes only AFTER checking.
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
            "XAUTUSD BOT STARTING"
        )

        logging.warning(
            "STRATEGY VERSION 8.0"
        )

        logging.warning(
            "10%% BALANCE"
        )

        logging.warning(
            "50x LEVERAGE"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "FIRST ENTRY:"
        )

        logging.warning(
            "05:30-05:45 HIGH -> LONG"
        )

        logging.warning(
            "05:30-05:45 LOW  -> SHORT"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "INTRADAY:"
        )

        logging.warning(
            "LONG  SL = DAY LOW"
        )

        logging.warning(
            "SHORT SL = DAY HIGH"
        )

        logging.warning(
            "NO TARGET"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "OVERNIGHT:"
        )

        logging.warning(
            "LONG  -> NEXT 05:30 CANDLE LOW"
        )

        logging.warning(
            "SHORT -> NEXT 05:30 CANDLE HIGH"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "SL REVERSAL:"
        )

        logging.warning(
            "LONG SL  -> SHORT"
        )

        logging.warning(
            "SHORT SL -> LONG"
        )

        logging.warning(
            "================================================"
        )

        set_leverage(
            self.product_id
        )

        # ----------------------------------------------------
        # STARTUP POSITION
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

        if now >= opening_end(
            self.day
        ):

            self.load_opening()

        if startup_size != 0:

            logging.warning(
                "STARTED WITH OPEN POSITION | SIZE=%s",
                startup_size
            )

            # Existing position is NEVER treated as a new
            # opening breakout.
            self.opening_breakout_used = True
            self.first_trade_taken = True
            self.first_position = False
            self.manual_flat = False
            self.carried_into_day = True

            self.last_position = startup_size

            # If startup is after 05:45, the new opening candle
            # becomes the current day's starting range.
            if (
                now >= opening_end(
                    self.day
                )
                and self.opening_ready
            ):

                self.apply_opening_to_new_day_range(
                )

            self.persist()

            try:

                price = get_price()

                self.sync_sl(
                    startup_size,
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

            if now >= opening_end(
                self.day
            ):

                try:

                    self.load_opening()

                    self.rebuild_day_range()

                except Exception as exc:

                    logging.error(
                        "Startup range setup failed: %s",
                        exc
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
