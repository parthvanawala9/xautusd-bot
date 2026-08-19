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
# XAUTUSD DELTA INDIA LIVE AUTO-TRADING BOT
# ============================================================
#
# STRATEGY
#
# Trading day:
#   05:30 IST -> next day 05:30 IST
#
# FIRST TRADE OF THE DAY:
#
#   Reference candle:
#       05:30 -> 05:45 IST
#
#   At 05:45:
#       opening_high = 05:30 candle HIGH
#       opening_low  = 05:30 candle LOW
#
#   First entry only:
#       Break opening HIGH -> LONG
#       Break opening LOW  -> SHORT
#
# AFTER FIRST ENTRY:
#
#   The 05:30 candle is retired and is NEVER used for another
#   entry/SL trigger.
#
#   Only NEW highs/lows formed after the first trade are used:
#
#   LONG:
#       SL = newest post-opening LOW
#
#   SHORT:
#       SL = newest post-opening HIGH
#
#   If the BOT'S SL is hit:
#       LONG  -> SHORT
#       SHORT -> LONG
#
# MANUAL CLOSE:
#
#   Manual close is NOT treated as SL.
#   No automatic reversal.
#   Bot remains flat for the rest of that trading day.
#
# POSITION:
#
#   Maximum one position.
#
# STOP ORDERS:
#
#   Exactly one protective stop for the current position.
#
#   LONG:
#       only SELL stop below the position
#
#   SHORT:
#       only BUY stop above the position
#
# WEEKEND:
#
#   Saturday 05:00 IST -> square off.
#   No new trades until Monday 05:45 IST.
#
# POSITION SIZE:
#
#   10% of current equity as margin
#   50x leverage
#
# LIVE TRADING ONLY
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


# ============================================================
# LIVE SETTINGS
# ============================================================

LIVE_TRADING = True


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


# ============================================================
# CREDENTIAL CHECK
# ============================================================

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
            "XAUTUSD-OpeningRange-Live-Bot/3.0"
        ),
    }
)


# ============================================================
# TIME HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def trading_day_start(dt=None):
    """
    Strategy day starts at 05:30 IST.
    """

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


def opening_candle_end(day):
    return day + timedelta(
        minutes=15
    )


def is_weekend_block(dt=None):
    """
    No new trades:
        Saturday 05:00 onward
        Sunday all day
        Monday before 05:45
    """

    dt = dt or now_ist()

    weekday = dt.weekday()

    # Saturday
    if (
        weekday == 5
        and dt.time()
        >= datetime.strptime(
            "05:00",
            "%H:%M"
        ).time()
    ):
        return True

    # Sunday
    if weekday == 6:
        return True

    # Monday before 05:45
    if (
        weekday == 0
        and dt.time()
        < datetime.strptime(
            "05:45",
            "%H:%M"
        ).time()
    ):
        return True

    return False


def is_force_squareoff_time(dt=None):

    dt = dt or now_ist()

    return (
        dt.weekday() == 5
        and dt.hour == 5
        and dt.minute < 5
    )


# ============================================================
# DELTA API AUTHENTICATION
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
# API REQUEST
# ============================================================

def request(
    method,
    path,
    params=None,
    body=None,
    authenticated=False
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

    if params:

        query_string = (
            "?"
            + urlencode(
                params,
                doseq=True
            )
        )

    else:

        query_string = ""

    headers = {}

    if authenticated:

        headers.update(
            sign_request(
                method,
                path,
                query_string,
                body_text,
            )
        )

    url = (
        BASE_URL
        + path
    )

    try:

        response = session.request(
            method.upper(),
            url,
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
            f"Network error calling "
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
            f"{method} {path} returned "
            f"invalid JSON: {response.text}"
        ) from exc

    if data.get("success") is False:

        raise RuntimeError(
            f"{method} {path}: {data}"
        )

    return data


# ============================================================
# PUBLIC API
# ============================================================

def get_product():

    return request(
        "GET",
        f"/v2/products/{SYMBOL}"
    )["result"]


def get_ticker():

    return request(
        "GET",
        f"/v2/tickers/{SYMBOL}"
    )["result"]


# ============================================================
# POSITION
# ============================================================

def get_position(product_id):

    result = request(
        "GET",
        "/v2/positions",
        params={
            "product_id": int(
                product_id
            )
        },
        authenticated=True,
    )["result"]

    if not result:

        return {
            "size": 0,
            "entry_price": None,
            "raw": result,
        }

    size = int(
        result.get(
            "size",
            0
        )
    )

    return {
        "size": size,
        "entry_price": result.get(
            "entry_price"
        ),
        "raw": result,
    }


# ============================================================
# WALLET
# ============================================================

def get_balances():

    return request(
        "GET",
        "/v2/wallet/balances",
        authenticated=True,
    )


def get_usdt_equity():

    data = get_balances()

    # Size from the actual USD/USDT wallet balance.
    # This makes BALANCE_FRACTION mean exactly that fraction
    # of the account balance, instead of using robo equity.
    wallets = data.get("result", [])

    for wallet in wallets:

        asset = str(
            wallet.get("asset_symbol", "")
        ).upper()

        if asset not in ("USDT", "USD"):
            continue

        value = wallet.get("balance")

        if value not in (None, ""):
            return Decimal(str(value))

        value = wallet.get("available_balance")

        if value not in (None, ""):
            return Decimal(str(value))

    # Fallback if the account does not expose a USD/USDT row.
    meta = data.get("meta", {})
    value = meta.get("net_equity")

    if value not in (None, ""):
        return Decimal(str(value))

    raise RuntimeError(
        "Could not find USD/USDT account balance."
    )


# ============================================================
# HISTORICAL CANDLES
# ============================================================

def get_candles(
    resolution,
    start_dt,
    end_dt
):

    start = int(
        start_dt
        .astimezone(UTC)
        .timestamp()
    )

    end = int(
        end_dt
        .astimezone(UTC)
        .timestamp()
    )

    return request(
        "GET",
        "/v2/history/candles",
        params={
            "resolution": resolution,
            "symbol": SYMBOL,
            "start": start,
            "end": end,
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

    return request(
        "POST",
        "/v2/orders",
        body=body,
        authenticated=True,
    )


# ============================================================
# STOP MARKET ORDER
# ============================================================

def stop_market_order(
    product_id,
    side,
    size,
    stop_price,
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
            stop_price
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

    return request(
        "POST",
        "/v2/orders",
        body=body,
        authenticated=True,
    )


# ============================================================
# CANCEL ORDER
# ============================================================

def cancel_order(
    order_id
):

    if not order_id:
        return

    logging.info(
        "CANCEL ORDER: %s",
        order_id
    )

    request(
        "DELETE",
        f"/v2/orders/{order_id}",
        authenticated=True,
    )


# ============================================================
# OPEN STOP ORDERS
# ============================================================

def get_open_stop_orders(
    product_id
):

    data = request(
        "GET",
        "/v2/orders",
        params={
            "product_ids": int(
                product_id
            ),
            "states": "open,pending",
            "order_types": "all_stop",
        },
        authenticated=True,
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
# CANCEL ALL STOP ORDERS
# ============================================================

def cancel_all_strategy_stops(
    product_id
):

    orders = get_open_stop_orders(
        product_id
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


# ============================================================
# LEVERAGE
# ============================================================

def set_leverage(
    product_id
):

    body = {
        "leverage": str(
            LEVERAGE
        )
    }

    logging.info(
        "SETTING LEVERAGE: %sx",
        LEVERAGE
    )

    request(
        "POST",
        f"/v2/products/"
        f"{product_id}/orders/leverage",
        body=body,
        authenticated=True,
    )


# ============================================================
# DECIMAL PRODUCT FIELD
# ============================================================

def decimal_field(
    product,
    *names,
    default=None
):

    for name in names:

        if product.get(
            name
        ) is not None:

            try:

                return Decimal(
                    str(
                        product[name]
                    )
                )

            except Exception:

                pass

    return default


# ============================================================
# POSITION SIZE
# ============================================================

def calculate_contract_size(
    product,
    price
):

    equity = get_usdt_equity()

    # 10% of current account balance is the margin budget.
    target_margin = equity * BALANCE_FRACTION

    # 50x leverage converts that margin budget to position notional.
    target_notional = target_margin * LEVERAGE

    contract_value = decimal_field(
        product,
        "contract_value",
        "contract_value_usd",
        "contract_unit_value",
    )

    if contract_value is None or contract_value <= 0:
        raise RuntimeError(
            "XAUTUSD product response does not contain a usable "
            "contract_value."
        )

    raw_size = target_notional / (price * contract_value)

    lot_size = decimal_field(
        product,
        "lot_size",
        "order_size_increment",
        default=Decimal("1"),
    )

    min_size = decimal_field(
        product,
        "min_order_size",
        "minimum_order_size",
        default=lot_size,
    )

    if lot_size is None or lot_size <= 0:
        lot_size = Decimal("1")

    # Always FLOOR. Never round upward beyond the 10% budget.
    size_decimal = (
        (raw_size / lot_size)
        .to_integral_value(rounding=ROUND_DOWN)
        * lot_size
    )

    if min_size is not None and size_decimal < min_size:
        raise RuntimeError(
            "10% balance budget is below the exchange minimum order size. "
            f"calculated={size_decimal}, minimum={min_size}"
        )

    size = int(size_decimal)

    if size <= 0:
        raise RuntimeError(
            "Calculated position size is zero; no trade will be placed."
        )

    return (
        size,
        equity,
        target_margin,
        target_notional,
        contract_value,
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
        # DAY STATE
        # ----------------------------------------------------

        self.day = None

        self.day_high = None
        self.day_low = None

        # ----------------------------------------------------
        # FIRST-TRADE OPENING RANGE
        # ----------------------------------------------------

        self.opening_high = None
        self.opening_low = None

        self.opening_candle_ready = False

        # ----------------------------------------------------
        # PRICE
        # ----------------------------------------------------

        self.last_price = None

        # ----------------------------------------------------
        # POSITION
        # ----------------------------------------------------

        self.last_position_size = 0

        # ----------------------------------------------------
        # SL STATE
        # ----------------------------------------------------

        self.current_sl = None
        self.stop_order_id = None

        # ----------------------------------------------------
        # TRADE STATE
        # ----------------------------------------------------

        self.first_trade_taken = False

        # After the first 05:30-05:45 opening-range trade,
        # the opening candle is retired permanently as a trigger.
        # Later SL/reversal levels use only NEW extremes formed
        # after that first trade.
        self.post_first_high = None
        self.post_first_low = None

        # True only while the FIRST position is protected by
        # the opposite side of the fixed 05:30 candle.
        self.opening_sl_active = False

        # Manual close locks the strategy for the rest
        # of the trading day.
        self.manual_close_lock = False

        # Prevents duplicate entry calls.
        self.entry_in_progress = False

        # Prevents duplicate reversal calls.
        self.reversal_lock = False


    # ========================================================
    # REFRESH DAY
    # ========================================================

    def refresh_day(
        self,
        now
    ):

        new_day = trading_day_start(
            now
        )

        if self.day == new_day:

            return

        logging.info(
            "=============================================="
        )

        logging.info(
            "NEW STRATEGY DAY: %s IST",
            new_day
        )

        logging.info(
            "=============================================="
        )

        self.day = new_day

        # New day's extremes.
        self.day_high = None
        self.day_low = None

        # New opening range.
        self.opening_high = None
        self.opening_low = None

        self.opening_candle_ready = False

        # New day allows one first trade.
        self.first_trade_taken = False

        # No post-opening levels exist until the first trade.
        self.post_first_high = None
        self.post_first_low = None
        self.opening_sl_active = False

        # Manual close lock resets on a new day.
        self.manual_close_lock = False

        # Do not carry the previous SL state.
        self.current_sl = None
        self.stop_order_id = None

        # ----------------------------------------------------
        # Build today's HIGH/LOW from 05:30 onward.
        # These are for SL/reversal AFTER the first trade.
        # ----------------------------------------------------

        try:

            candles = get_candles(
                "1m",
                new_day,
                now
            )

            if candles:

                highs = [
                    Decimal(
                        str(
                            candle[
                                "high"
                            ]
                        )
                    )
                    for candle in candles
                ]

                lows = [
                    Decimal(
                        str(
                            candle[
                                "low"
                            ]
                        )
                    )
                    for candle in candles
                ]

                self.day_high = max(
                    highs
                )

                self.day_low = min(
                    lows
                )

        except Exception as exc:

            logging.error(
                "Could not rebuild today's "
                "High/Low: %s",
                exc
            )

        # ----------------------------------------------------
        # Load opening candle once complete.
        # ----------------------------------------------------

        if now >= opening_candle_end(
            new_day
        ):

            self.load_opening_candle()


    # ========================================================
    # OPENING CANDLE
    # ========================================================

    def load_opening_candle(
        self
    ):

        if self.opening_candle_ready:

            return

        if self.day is None:

            return

        start = self.day

        end = (
            self.day
            + timedelta(
                minutes=15,
                seconds=1
            )
        )

        candles = get_candles(
            "15m",
            start,
            end
        )

        target = None

        for candle in candles:

            candle_time = (
                datetime.fromtimestamp(
                    int(
                        candle[
                            "time"
                        ]
                    ),
                    UTC
                )
                .astimezone(IST)
            )

            if candle_time == start:

                target = candle

                break

        if target is None:

            raise RuntimeError(
                "05:30-05:45 IST "
                "opening candle not found."
            )

        self.opening_high = Decimal(
            str(
                target[
                    "high"
                ]
            )
        )

        self.opening_low = Decimal(
            str(
                target[
                    "low"
                ]
            )
        )

        self.opening_candle_ready = True

        logging.info(
            "OPENING RANGE FIXED | "
            "05:30-05:45 | HIGH=%s | LOW=%s",
            self.opening_high,
            self.opening_low,
        )


    # ========================================================
    # UPDATE TODAY'S HIGH / LOW
    # ========================================================

    def update_day_extreme(
        self,
        price
    ):

        # Full day extremes are retained for reference.
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

        # ----------------------------------------------------
        # AFTER FIRST TRADE:
        # only NEW extremes beyond the fixed 05:30 candle
        # are allowed to become later SL/reversal levels.
        # ----------------------------------------------------
        if self.first_trade_taken:

            if (
                self.opening_high is not None
                and price > self.opening_high
                and (
                    self.post_first_high is None
                    or price > self.post_first_high
                )
            ):
                self.post_first_high = price

            if (
                self.opening_low is not None
                and price < self.opening_low
                and (
                    self.post_first_low is None
                    or price < self.post_first_low
                )
            ):
                self.post_first_low = price


    # ========================================================
    # DESIRED SL
    # ========================================================

    def desired_sl(
        self,
        position_size
    ):

        # FIRST POSITION uses the fixed 05:30-05:45 candle.
        if self.opening_sl_active:

            if position_size > 0:
                return self.opening_low

            if position_size < 0:
                return self.opening_high

            return None

        # AFTER THE FIRST SL/reversal, the opening candle is retired.
        # Only NEW post-opening extremes are valid.
        if position_size > 0:
            return self.post_first_low

        if position_size < 0:
            return self.post_first_high

        return None


    # ========================================================
    # ORDER SIDE
    # ========================================================

    @staticmethod
    def stop_side(
        position_size
    ):

        # LONG position closes with SELL.
        if position_size > 0:

            return "sell"

        # SHORT position closes with BUY.
        if position_size < 0:

            return "buy"

        return None


    # ========================================================
    # READ STOP PRICE
    # ========================================================

    @staticmethod
    def order_stop_price(
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
    # CLEAN / RECONCILE STOPS
    # ========================================================

    def reconcile_stop_orders(
        self,
        position_size,
        desired
    ):

        """
        Make sure there is exactly ONE strategy SL.

        For a LONG:
            only SELL stop at today's LOW.

        For a SHORT:
            only BUY stop at today's HIGH.

        Any extra stop orders are cancelled.
        """

        orders = get_open_stop_orders(
            self.product_id
        )

        if not orders:

            self.stop_order_id = None

            return

        expected_side = self.stop_side(
            position_size
        )

        valid = []

        for order in orders:

            order_id = order.get(
                "id"
            )

            if not order_id:

                continue

            side = str(
                order.get(
                    "side",
                    ""
                )
            ).lower()

            stop_price = (
                self.order_stop_price(
                    order
                )
            )

            # Correct direction + correct price.
            if (
                side == expected_side
                and desired is not None
                and stop_price == desired
            ):

                valid.append(
                    order
                )

            else:

                try:

                    logging.warning(
                        "CANCEL INVALID/EXTRA STOP | "
                        "id=%s | side=%s | price=%s",
                        order_id,
                        side,
                        stop_price,
                    )

                    cancel_order(
                        order_id
                    )

                except Exception as exc:

                    logging.error(
                        "Could not cancel "
                        "invalid stop %s: %s",
                        order_id,
                        exc
                    )

        # Keep only one correct stop.
        if len(valid) > 1:

            for extra in valid[1:]:

                try:

                    cancel_order(
                        extra.get(
                            "id"
                        )
                    )

                except Exception as exc:

                    logging.error(
                        "Could not cancel "
                        "duplicate stop: %s",
                        exc
                    )

            valid = valid[:1]

        if valid:

            self.stop_order_id = valid[0].get(
                "id"
            )

        else:

            self.stop_order_id = None


    # ========================================================
    # PLACE / REPLACE SL
    # ========================================================

    def place_or_replace_sl(
        self,
        position_size,
        force=False
    ):

        desired = self.desired_sl(
            position_size
        )

        if desired is None:

            return

        if self.last_price is None:

            return

        # ----------------------------------------------------
        # LONG SL MUST BE BELOW MARKET
        # ----------------------------------------------------

        if (
            position_size > 0
            and desired >= self.last_price
        ):

            logging.warning(
                "LONG SL NOT VALID YET | "
                "SL=%s | LTP=%s",
                desired,
                self.last_price
            )

            return

        # ----------------------------------------------------
        # SHORT SL MUST BE ABOVE MARKET
        # ----------------------------------------------------

        if (
            position_size < 0
            and desired <= self.last_price
        ):

            logging.warning(
                "SHORT SL NOT VALID YET | "
                "SL=%s | LTP=%s",
                desired,
                self.last_price
            )

            return

        # ----------------------------------------------------
        # Reconcile existing exchange stops.
        # ----------------------------------------------------

        self.reconcile_stop_orders(
            position_size,
            desired
        )

        # Correct SL already exists.
        if (
            not force
            and self.current_sl == desired
            and self.stop_order_id
        ):

            return

        # ----------------------------------------------------
        # Cancel all existing strategy stops.
        # This guarantees one SL only.
        # ----------------------------------------------------

        orders = get_open_stop_orders(
            self.product_id
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
                    "Cancel existing SL failed: %s",
                    exc
                )

        self.stop_order_id = None
        self.current_sl = None

        # ----------------------------------------------------
        # Create exactly ONE SL.
        # ----------------------------------------------------

        side = self.stop_side(
            position_size
        )

        size = abs(
            position_size
        )

        client_id = (
            "xsl"
            + str(
                int(
                    time.time() * 1000
                )
            )
        )

        result = stop_market_order(
            self.product_id,
            side,
            size,
            desired,
            client_id,
        )

        self.current_sl = desired

        # ----------------------------------------------------
        # Extract order ID.
        # ----------------------------------------------------

        result_list = result.get(
            "result",
            []
        )

        if isinstance(
            result_list,
            list
        ):

            if result_list:

                self.stop_order_id = (
                    result_list[0].get(
                        "id"
                    )
                )

        elif isinstance(
            result_list,
            dict
        ):

            self.stop_order_id = (
                result_list.get(
                    "id"
                )
            )

        logging.info(
            "ONE LIVE SL SET | "
            "position=%s | SL=%s | side=%s | order_id=%s",
            (
                "LONG"
                if position_size > 0
                else "SHORT"
            ),
            desired,
            side,
            self.stop_order_id,
        )


    # ========================================================
    # ENTER FIRST TRADE
    # ========================================================

    def enter(
        self,
        direction,
        price,
        reason
    ):

        # Prevent duplicate calls.
        if self.entry_in_progress:

            logging.warning(
                "ENTRY ALREADY IN PROGRESS. "
                "IGNORING DUPLICATE ENTRY."
            )

            return False

        # Weekend.
        if is_weekend_block():

            return False

        # Manual close lock.
        if self.manual_close_lock:

            return False

        # Only one position.
        current = get_position(
            self.product_id
        )

        if current["size"] != 0:

            logging.warning(
                "ENTRY BLOCKED: position already exists: %s",
                current["size"]
            )

            self.last_position_size = (
                current["size"]
            )

            return False

        # ----------------------------------------------------
        # Calculate 10% position.
        # ----------------------------------------------------

        size, equity, margin, notional, contract_value = (
            calculate_contract_size(
                self.product,
                price
            )
        )

        side = (
            "buy"
            if direction == "LONG"
            else "sell"
        )

        client_id = (
            "xent"
            + str(
                int(
                    time.time() * 1000
                )
            )
        )

        logging.warning(
            "=============================================="
        )

        logging.warning(
            "LIVE ENTRY %s",
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
            "EQUITY=%s",
            equity
        )

        logging.warning(
            "MARGIN=%s",
            margin
        )

        logging.warning(
            "NOTIONAL=%s",
            notional
        )

        logging.warning(
            "BALANCE USED=%s%%",
            BALANCE_FRACTION * 100
        )

        logging.warning(
            "REASON=%s",
            reason
        )

        logging.warning(
            "=============================================="
        )

        self.entry_in_progress = True

        try:

            market_order(
                self.product_id,
                side,
                size,
                client_id,
            )

            # Wait for fill.
            for _ in range(30):

                time.sleep(
                    0.2
                )

                position = get_position(
                    self.product_id
                )

                if (
                    direction == "LONG"
                    and position["size"] > 0
                ) or (
                    direction == "SHORT"
                    and position["size"] < 0
                ):

                    self.last_position_size = (
                        position["size"]
                    )

                    # First trade has now happened.
                    self.first_trade_taken = True

                    # Retire the 05:30 candle permanently.
                    # The actual breakout price becomes the first
                    # post-opening extreme for later reversals.
                    if direction == "LONG":
                        self.post_first_high = price
                    else:
                        self.post_first_low = price

                    # The FIRST position gets its SL from the
                    # fixed 05:30 candle only.
                    self.opening_sl_active = True

                    # Clear old SL state.
                    self.current_sl = None
                    self.stop_order_id = None

                    # Place ONLY the correct SL.
                    self.place_or_replace_sl(
                        position["size"],
                        force=True
                    )

                    return True

            raise RuntimeError(
                "Market entry was sent but "
                "position fill was not confirmed."
            )

        finally:

            self.entry_in_progress = False


    # ========================================================
    # SQUARE OFF
    # ========================================================

    def square_off(
        self
    ):

        position = get_position(
            self.product_id
        )

        size = position[
            "size"
        ]

        # Cancel all protective stops first.
        cancel_all_strategy_stops(
            self.product_id
        )

        self.current_sl = None
        self.stop_order_id = None

        if size == 0:

            self.last_position_size = 0

            return

        side = (
            "sell"
            if size > 0
            else "buy"
        )

        logging.warning(
            "=============================================="
        )

        logging.warning(
            "SATURDAY FORCE SQUARE-OFF"
        )

        logging.warning(
            "POSITION SIZE=%s",
            size
        )

        logging.warning(
            "=============================================="
        )

        market_order(
            self.product_id,
            side,
            abs(size),
            (
                "xoff"
                + str(
                    int(
                        time.time() * 1000
                    )
                )
            ),
        )

        self.last_position_size = 0


    # ========================================================
    # DETERMINE WHETHER POSITION EXIT WAS OUR SL
    # ========================================================

    def was_our_stop_triggered(
        self,
        old_size
    ):

        """
        We distinguish:

            our SL triggered
                -> stop order disappears

        from:

            manual close
                -> protective stop is still open

        This prevents manual closing from causing
        an automatic reversal.
        """

        orders = get_open_stop_orders(
            self.product_id
        )

        if not orders:

            # If our tracked SL disappeared while the
            # position went flat, treat it as SL execution.
            return (
                self.stop_order_id is not None
            )

        # If the tracked stop still exists, position was
        # likely closed manually.
        tracked_exists = False

        for order in orders:

            order_id = order.get(
                "id"
            )

            if (
                self.stop_order_id
                and str(order_id)
                == str(self.stop_order_id)
            ):

                tracked_exists = True

                break

        if tracked_exists:

            return False

        # The tracked stop is gone but another stop may
        # remain. This is still treated as SL execution,
        # because our protective stop disappeared.
        return (
            self.stop_order_id is not None
        )


    # ========================================================
    # POSITION CLOSED
    # ========================================================

    def handle_position_closed(
        self,
        old_size
    ):

        if old_size == 0:

            return

        # ----------------------------------------------------
        # Determine whether our SL disappeared.
        # ----------------------------------------------------

        sl_triggered = (
            self.was_our_stop_triggered(
                old_size
            )
        )

        # ----------------------------------------------------
        # ALWAYS clean remaining stops after flat.
        # ----------------------------------------------------

        cancel_all_strategy_stops(
            self.product_id
        )

        self.current_sl = None
        self.stop_order_id = None

        # ----------------------------------------------------
        # MANUAL CLOSE
        # ----------------------------------------------------

        if not sl_triggered:

            logging.warning(
                "=============================================="
            )

            logging.warning(
                "MANUAL POSITION CLOSE DETECTED"
            )

            logging.warning(
                "NO REVERSAL"
            )

            logging.warning(
                "BOT WILL STAY FLAT FOR THE REST OF TODAY"
            )

            logging.warning(
                "=============================================="
            )

            self.manual_close_lock = True

            return

        # ----------------------------------------------------
        # SL HIT
        # ----------------------------------------------------

        # The first position's fixed 05:30 SL is now finished.
        # Every position after this uses only post-opening
        # NEW highs/lows.
        self.opening_sl_active = False

        if (
            self.reversal_lock
            or is_weekend_block()
        ):

            return

        if self.last_price is None:

            return

        direction = (
            "SHORT"
            if old_size > 0
            else "LONG"
        )

        logging.warning(
            "=============================================="
        )

        logging.warning(
            "OUR STOP LOSS WAS TRIGGERED"
        )

        logging.warning(
            "OLD POSITION=%s",
            old_size
        )

        logging.warning(
            "REVERSING TO %s",
            direction
        )

        logging.warning(
            "=============================================="
        )

        # A later reversal must have a NEW opposite-side extreme.
        # This prevents the retired 05:30 high/low from being reused.
        reversal_sl = (
            self.post_first_high
            if direction == "SHORT"
            else self.post_first_low
        )

        if reversal_sl is None:

            logging.warning(
                "SL HIT BUT NO NEW OPPOSITE EXTREME EXISTS. "
                "NO RE-ENTRY ON THE OLD 05:30 RANGE."
            )

            return

        self.reversal_lock = True

        try:

            self.enter(
                direction,
                self.last_price,
                "our SL triggered"
            )

        finally:

            self.reversal_lock = False


    # ========================================================
    # FIRST TRADE BREAKOUT
    # ========================================================

    def check_first_trade_breakout(
        self,
        price
    ):

        if self.first_trade_taken:

            # The 05:30-05:45 range is used exactly once.
            # No second entry can come from that range.
            return

        if self.manual_close_lock:

            return

        if not self.opening_candle_ready:

            return

        if (
            self.opening_high is None
            or self.opening_low is None
        ):

            return

        # ----------------------------------------------------
        # HIGH BREAK -> LONG
        # ----------------------------------------------------

        if price > self.opening_high:

            self.enter(
                "LONG",
                price,
                "first trade: 05:30 candle HIGH breakout"
            )

            return

        # ----------------------------------------------------
        # LOW BREAK -> SHORT
        # ----------------------------------------------------

        if price < self.opening_low:

            self.enter(
                "SHORT",
                price,
                "first trade: 05:30 candle LOW breakout"
            )

            return


    # ========================================================
    # ONE STRATEGY LOOP
    # ========================================================

    def run_once(
        self
    ):

        now = now_ist()

        # ----------------------------------------------------
        # GET PRICE
        # ----------------------------------------------------

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
                f"Could not find LTP "
                f"in ticker: {ticker}"
            )

        price = Decimal(
            str(
                raw_price
            )
        )

        self.last_price = price

        # ----------------------------------------------------
        # DAY
        # ----------------------------------------------------

        self.refresh_day(
            now
        )

        # ----------------------------------------------------
        # Update today's HIGH/LOW.
        #
        # IMPORTANT:
        # These are NOT the first-entry levels.
        # First entry uses the fixed 05:30-05:45 candle.
        # ----------------------------------------------------

        self.update_day_extreme(
            price
        )

        # ----------------------------------------------------
        # SATURDAY SQUARE OFF
        # ----------------------------------------------------

        if is_force_squareoff_time(
            now
        ):

            self.square_off()

            return

        # ----------------------------------------------------
        # WEEKEND
        # ----------------------------------------------------

        if is_weekend_block(
            now
        ):

            return

        # ----------------------------------------------------
        # GET CURRENT POSITION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        new_size = position[
            "size"
        ]

        old_size = (
            self.last_position_size
        )

        # ----------------------------------------------------
        # POSITION OPENED EXTERNALLY / AT STARTUP
        # ----------------------------------------------------

        if (
            old_size == 0
            and new_size != 0
        ):

            logging.info(
                "OPEN POSITION DETECTED | size=%s",
                new_size
            )

            self.last_position_size = (
                new_size
            )

            # Treat externally existing position as a
            # position to manage, not a new entry.
            self.first_trade_taken = True

            # Rebuild correct SL.
            self.place_or_replace_sl(
                new_size,
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

            self.handle_position_closed(
                old_size
            )

            self.last_position_size = 0

            return

        # ----------------------------------------------------
        # POSITION DIRECTION CHANGED
        #
        # This can happen after a reversal.
        # Manage the new position.
        # ----------------------------------------------------

        if (
            old_size != 0
            and new_size != 0
            and (
                (
                    old_size > 0
                    and new_size < 0
                )
                or
                (
                    old_size < 0
                    and new_size > 0
                )
            )
        ):

            logging.info(
                "POSITION DIRECTION CHANGED | "
                "%s -> %s",
                old_size,
                new_size
            )

            self.current_sl = None
            self.stop_order_id = None

            self.last_position_size = (
                new_size
            )

            self.place_or_replace_sl(
                new_size,
                force=True
            )

            return

        # ----------------------------------------------------
        # MANAGE EXISTING POSITION
        # ----------------------------------------------------

        if new_size != 0:

            self.last_position_size = (
                new_size
            )

            # The ONLY SL for the position.
            self.place_or_replace_sl(
                new_size
            )

            return

        # ----------------------------------------------------
        # FLAT
        # ----------------------------------------------------

        self.last_position_size = 0

        # Clean orphan stops if any remain.
        orphan_stops = get_open_stop_orders(
            self.product_id
        )

        if orphan_stops:

            logging.warning(
                "FLAT POSITION WITH "
                "ORPHAN STOP ORDERS. CLEANING."
            )

            cancel_all_strategy_stops(
                self.product_id
            )

            self.current_sl = None
            self.stop_order_id = None

        # ----------------------------------------------------
        # MANUAL CLOSE LOCK
        # ----------------------------------------------------

        if self.manual_close_lock:

            return

        # ----------------------------------------------------
        # WAIT FOR 05:30-05:45 CANDLE
        # ----------------------------------------------------

        if now < opening_candle_end(
            self.day
        ):

            return

        # ----------------------------------------------------
        # LOAD OPENING CANDLE
        # ----------------------------------------------------

        if not self.opening_candle_ready:

            self.load_opening_candle()

        # ----------------------------------------------------
        # FIRST TRADE ONLY
        # ----------------------------------------------------

        self.check_first_trade_breakout(
            price
        )


    # ========================================================
    # MAIN LOOP
    # ========================================================

    def run(
        self
    ):

        logging.info(
            "=============================================="
        )

        logging.info(
            "XAUTUSD LIVE BOT STARTING"
        )

        logging.info(
            "=============================================="
        )

        logging.info(
            "BASE_URL=%s",
            BASE_URL
        )

        logging.info(
            "SYMBOL=%s",
            SYMBOL
        )

        logging.info(
            "LIVE_TRADING=%s",
            LIVE_TRADING
        )

        logging.info(
            "LEVERAGE=%sx",
            LEVERAGE
        )

        logging.info(
            "BALANCE FRACTION=%s%%",
            BALANCE_FRACTION * 100
        )

        logging.info(
            "=============================================="
        )

        if not LIVE_TRADING:

            raise RuntimeError(
                "LIVE_TRADING is not enabled."
            )

        # ----------------------------------------------------
        # LEVERAGE
        # ----------------------------------------------------

        set_leverage(
            self.product_id
        )

        # ----------------------------------------------------
        # STARTUP RECONCILIATION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        self.last_position_size = (
            position["size"]
        )

        if position["size"] != 0:

            logging.warning(
                "=============================================="
            )

            logging.warning(
                "BOT STARTED WITH OPEN POSITION"
            )

            logging.warning(
                "SIZE=%s",
                position["size"]
            )

            logging.warning(
                "BOT WILL MANAGE IT"
            )

            logging.warning(
                "BOT WILL NOT OPEN A DUPLICATE POSITION"
            )

            logging.warning(
                "=============================================="
            )

            self.first_trade_taken = True

            # If the bot restarts with an already-open position,
            # begin post-opening tracking from the current price.
            try:
                startup_ticker = get_ticker()
                startup_raw = (
                    startup_ticker.get("close")
                    or startup_ticker.get("last_price")
                    or startup_ticker.get("mark_price")
                )
                startup_price = Decimal(str(startup_raw))

                if position["size"] > 0:
                    self.post_first_high = startup_price
                else:
                    self.post_first_low = startup_price

            except Exception as exc:
                logging.error(
                    "Could not initialize post-opening level: %s",
                    exc
                )

        else:

            # Remove old stops left by previous bot versions.
            try:

                cancel_all_strategy_stops(
                    self.product_id
                )

            except Exception as exc:

                logging.error(
                    "Startup stop cleanup failed: %s",
                    exc
                )

        # ----------------------------------------------------
        # CONTINUOUS LOOP
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
        "=============================================="
    )

    logging.info(
        "CONNECTING TO DELTA INDIA PRODUCTION"
    )

    logging.info(
        "=============================================="
    )

    logging.info(
        "BASE URL: %s",
        BASE_URL
    )

    logging.info(
        "SYMBOL: %s",
        SYMBOL
    )

    # --------------------------------------------------------
    # PRODUCT
    # --------------------------------------------------------

    product = get_product()

    logging.info(
        "PRODUCT RESPONSE:"
    )

    logging.info(
        json.dumps(
            product,
            indent=2
        )
    )

    # --------------------------------------------------------
    # SYMBOL CHECK
    # --------------------------------------------------------

    product_symbol = str(
        product.get(
            "symbol",
            SYMBOL
        )
    ).upper()

    if (
        product_symbol
        != SYMBOL.upper()
    ):

        raise RuntimeError(
            "Requested product symbol "
            "does not match API response. "
            f"Requested={SYMBOL} "
            f"Received={product_symbol}"
        )

    # --------------------------------------------------------
    # PRODUCT STATE
    # --------------------------------------------------------

    state = str(
        product.get(
            "state",
            ""
        )
    ).lower()

    if state and state not in (
        "live",
        "active",
        "listed",
    ):

        raise RuntimeError(
            "XAUTUSD product is not live/active. "
            f"State={state}"
        )

    # --------------------------------------------------------
    # LIVE MODE
    # --------------------------------------------------------

    if not LIVE_TRADING:

        raise RuntimeError(
            "This bot is configured for LIVE trading only."
        )

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    Strategy(
        product
    ).run()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
