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
# LIVE TRADING ONLY
#
# Strategy:
#
# Trading day:
#   05:30 IST -> next day 05:30 IST
#
# Opening candle:
#   05:30 -> 05:45 IST
#
# Entry:
#   Price breaks opening HIGH -> LONG
#   Price breaks opening LOW  -> SHORT
#
# Stop Loss:
#   LONG  -> current trading-day LOW
#   SHORT -> current trading-day HIGH
#
# After SL:
#   Immediately reverse at MARKET.
#
# Position handling:
#   If a position already exists when bot starts,
#   bot manages that position and does NOT duplicate it.
#
# New trading day:
#   Existing position is NOT closed.
#   SL is rebuilt using the new day's high/low.
#
# Weekend:
#   Saturday 05:00 IST -> square off.
#   Saturday 05:00 -> Monday 05:45 = no entries.
#
# Position sizing:
#   10% of current equity as margin
#   50x leverage
#
# IMPORTANT:
#   This file is LIVE ONLY.
#   There is NO dry-run/demo trading mode.
# ============================================================


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()


IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


# Delta Exchange India production API.
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

# HARD-CODED LIVE MODE.
# There is intentionally no dry-run mode in this file.
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
# REQUIRED CREDENTIAL CHECK
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
            "XAUTUSD-OpeningRange-Live-Bot/2.0"
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
    Current strategy day starts at 05:30 IST.
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


def is_weekend_block(dt=None):
    """
    Saturday 05:00 IST through Monday 05:45 IST:
    no new trades.

    Existing position is squared off at Saturday 05:00.
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
    """
    Saturday from 05:00 IST.
    """

    dt = dt or now_ist()

    return (
        dt.weekday() == 5
        and dt.hour == 5
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
    """
    Delta signature.

    IMPORTANT:
    query_string must contain the leading '?' when
    query parameters exist.

    Example:

        ?product_id=131253

    This is the correction for the previous
    Signature Mismatch error.
    """

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
    """
    Central Delta REST API request.

    Query string used for the HTTP request and the
    query string used for signing are generated from
    the same encoded parameters.
    """

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


    # Build the EXACT encoded query string.
    #
    # IMPORTANT:
    # Include '?' in the signature.
    #
    # Example:
    # ?product_id=131253
    #
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
# WALLET BALANCES
# ============================================================

def get_balances():

    return request(
        "GET",
        "/v2/wallet/balances",
        authenticated=True,
    )


def get_usdt_equity():

    data = get_balances()


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
                meta[
                    "net_equity"
                ]
            )
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
            "USDT",
            "USD"
        ):

            return Decimal(
                str(
                    wallet.get(
                        "balance",
                        "0"
                    )
                )
            )


    raise RuntimeError(
        "Could not find "
        "USDT/USD account equity."
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
            "states": (
                "open,pending"
            ),
            "order_types": (
                "all_stop"
            ),
        },
        authenticated=True,
    )


    return data.get(
        "result",
        []
    )


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
    """
    Uses:

        10% of current equity
        x 50 leverage

    as target notional.

    Contract metadata from Delta is used to determine
    the actual number of contracts.
    """

    equity = get_usdt_equity()


    target_margin = (
        equity
        * BALANCE_FRACTION
    )


    target_notional = (
        target_margin
        * LEVERAGE
    )


    contract_value = decimal_field(
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
            "XAUTUSD product response "
            "does not contain a usable "
            "contract_value."
        )


    raw_size = (
        target_notional
        / (
            price
            * contract_value
        )
    )


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


    if (
        lot_size is None
        or lot_size <= 0
    ):

        lot_size = Decimal("1")


    size = (
        (
            raw_size
            / lot_size
        )
        .to_integral_value(
            rounding=ROUND_DOWN
        )
        * lot_size
    )


    if size < min_size:

        size = min_size


    return (
        int(size),
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


        self.day = None


        self.day_high = None
        self.day_low = None


        self.opening_high = None
        self.opening_low = None


        self.opening_candle_ready = (
            False
        )


        self.last_price = None


        self.last_position_size = 0


        self.current_sl = None
        self.stop_order_id = None


        self.reversal_lock = False


        self.last_entry_day = None


        # ====================================================
        # DUPLICATE ENTRY PROTECTION
        # ====================================================
        #
        # Prevents the bot from sending another market order
        # while the previous market order is still waiting for
        # Delta to report the resulting position.
        #
        self.entry_in_progress = False


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


        self.opening_high = None
        self.opening_low = None


        self.opening_candle_ready = (
            False
        )


        # Rebuild the day's actual high/low
        # from 1-minute candles.
        try:

            candles = get_candles(
                "1m",
                new_day,
                now
            )


            if candles:

                self.day_high = max(
                    Decimal(
                        str(
                            candle[
                                "high"
                            ]
                        )
                    )
                    for candle in candles
                )


                self.day_low = min(
                    Decimal(
                        str(
                            candle[
                                "low"
                            ]
                        )
                    )
                    for candle in candles
                )

            else:

                self.day_high = (
                    self.last_price
                )

                self.day_low = (
                    self.last_price
                )


        except Exception as exc:

            logging.error(
                "Could not rebuild "
                "day High/Low: %s",
                exc
            )


            self.day_high = (
                self.last_price
            )


            self.day_low = (
                self.last_price
            )


        # Load opening candle once complete.
        if (
            now
            >= (
                new_day
                + timedelta(
                    minutes=15
                )
            )
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


        self.opening_candle_ready = (
            True
        )


        logging.info(
            "OPENING CANDLE 05:30-05:45 | "
            "HIGH=%s | LOW=%s",
            self.opening_high,
            self.opening_low,
        )


    # ========================================================
    # UPDATE EXTREMES
    # ========================================================

    def update_extreme(
        self,
        price
    ):

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


    # ========================================================
    # DESIRED STOP
    # ========================================================

    def desired_sl(
        self,
        position_size
    ):

        if position_size > 0:

            return self.day_low


        if position_size < 0:

            return self.day_high


        return None


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


        # Long SL must be below current price.
        if (
            position_size > 0
            and desired >= self.last_price
        ):

            return


        # Short SL must be above current price.
        if (
            position_size < 0
            and desired <= self.last_price
        ):

            return


        if (
            not force
            and self.current_sl == desired
            and self.stop_order_id
        ):

            return


        # Cancel previous SL.
        if self.stop_order_id:

            try:

                cancel_order(
                    self.stop_order_id
                )

            except Exception as exc:

                logging.error(
                    "Cancel old SL failed: %s",
                    exc
                )


        side = (
            "sell"
            if position_size > 0
            else "buy"
        )


        size = abs(
            position_size
        )


        client_id = (
            f"xsl"
            f"{int(time.time() * 1000)}"
        )


        result = stop_market_order(
            self.product_id,
            side,
            size,
            desired,
            client_id,
        )


        self.current_sl = desired


        self.stop_order_id = None


        try:

            result_list = result.get(
                "result",
                []
            )


            if (
                isinstance(
                    result_list,
                    list
                )
                and result_list
            ):

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


        except Exception:

            self.stop_order_id = None


        logging.info(
            "LIVE SL SET | position=%s | "
            "SL=%s | side=%s | order_id=%s",
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
    # ENTER TRADE
    # ========================================================

    def enter(
        self,
        direction,
        price,
        reason
    ):

        if is_weekend_block():

            return


        # ====================================================
        # DUPLICATE ENTRY PROTECTION
        # ====================================================
        #
        # If an entry order has already been submitted and
        # Delta has not yet confirmed the resulting position,
        # absolutely do NOT submit another market order.
        #
        if self.entry_in_progress:

            logging.warning(
                "ENTRY BLOCKED: previous market "
                "entry is still being confirmed."
            )

            return


        # ====================================================
        # DUPLICATE ENTRY PROTECTION
        # ====================================================
        #
        # Always check the real Delta position immediately
        # before sending a new market order.
        #
        existing_position = get_position(
            self.product_id
        )


        if existing_position["size"] != 0:

            logging.warning(
                "ENTRY BLOCKED: existing "
                "position already exists | size=%s",
                existing_position["size"]
            )

            self.last_position_size = (
                existing_position["size"]
            )

            return


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
            f"xent"
            f"{int(time.time() * 1000)}"
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
            "CONTRACT VALUE=%s",
            contract_value
        )

        logging.warning(
            "REASON=%s",
            reason
        )

        logging.warning(
            "=============================================="
        )


        # ====================================================
        # LOCK BEFORE SENDING MARKET ORDER
        # ====================================================
        #
        # This is critical.
        #
        # Once the market order is successfully submitted,
        # another loop cannot submit a second 10% entry while
        # Delta is still updating the position.
        #
        try:

            market_order(
                self.product_id,
                side,
                size,
                client_id,
            )

        except Exception:

            # If Delta rejected the order before submission,
            # allow the strategy to try again normally.
            self.entry_in_progress = False

            raise


        self.entry_in_progress = True


        logging.info(
            "ENTRY ORDER SUBMITTED. "
            "ENTRY LOCK ACTIVE."
        )


        # Wait for Delta to report the position.
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


                self.current_sl = None
                self.stop_order_id = None


                self.place_or_replace_sl(
                    position["size"],
                    force=True
                )


                # ====================================================
                # ENTRY CONFIRMED
                # ====================================================
                #
                # Now it is safe to release the entry lock.
                #
                self.entry_in_progress = False


                logging.info(
                    "ENTRY CONFIRMED. "
                    "ENTRY LOCK RELEASED."
                )


                return


        # ====================================================
        # IMPORTANT:
        #
        # The market order was successfully submitted, but
        # Delta did not confirm the position within the normal
        # confirmation window.
        #
        # DO NOT release the lock.
        #
        # This prevents the main loop from submitting another
        # 10% market order.
        # ====================================================

        logging.error(
            "MARKET ENTRY WAS SUBMITTED BUT "
            "POSITION WAS NOT CONFIRMED."
        )

        logging.error(
            "ENTRY LOCK REMAINS ACTIVE TO "
            "PREVENT DUPLICATE ORDERS."
        )


        raise RuntimeError(
            "Market entry was sent but "
            "position fill was not confirmed. "
            "Duplicate-entry protection remains active."
        )


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


        if size == 0:

            cancel_all_strategy_stops(
                self.product_id
            )


            self.current_sl = None
            self.stop_order_id = None


            return


        cancel_all_strategy_stops(
            self.product_id
        )


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
                f"xoff"
                f"{int(time.time() * 1000)}"
            ),
        )


        self.current_sl = None
        self.stop_order_id = None


    # ========================================================
    # POSITION TRANSITION
    # ========================================================

    def process_position_transition(
        self,
        old_size,
        new_size
    ):

        # Position did not transition from open -> flat.
        if (
            old_size == 0
            or new_size != 0
        ):

            return


        if (
            self.reversal_lock
            or is_weekend_block()
        ):

            return


        if self.last_price is None:

            return


        # If LONG was stopped -> SHORT.
        #
        # If SHORT was stopped -> LONG.
        direction = (
            "SHORT"
            if old_size > 0
            else "LONG"
        )


        logging.warning(
            "=============================================="
        )

        logging.warning(
            "SL EXIT DETECTED"
        )

        logging.warning(
            "OLD SIZE=%s",
            old_size
        )

        logging.warning(
            "REVERSING TO %s",
            direction
        )

        logging.warning(
            "=============================================="
        )


        self.reversal_lock = True


        try:

            self.current_sl = None
            self.stop_order_id = None


            self.enter(
                direction,
                self.last_price,
                "SL reversal"
            )


        finally:

            self.reversal_lock = False


    # ========================================================
    # ONE STRATEGY LOOP
    # ========================================================

    def run_once(
        self
    ):

        now = now_ist()


        # ----------------------------------------------------
        # PRICE
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


        self.update_extreme(
            price
        )


        # ----------------------------------------------------
        # SATURDAY SQUARE-OFF
        # ----------------------------------------------------

        if is_force_squareoff_time(
            now
        ):

            # Execute only during the first
            # five minutes of the Saturday boundary.
            if now.minute < 5:

                self.square_off()

            return


        # ----------------------------------------------------
        # WEEKEND BLOCK
        # ----------------------------------------------------

        if is_weekend_block(
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
            self.last_position_size
        )


        # ----------------------------------------------------
        # EXISTING POSITION
        # ----------------------------------------------------

        if (
            old_size == 0
            and new_size != 0
        ):

            logging.info(
                "Existing position detected: %s",
                new_size
            )


        # ----------------------------------------------------
        # POSITION CLOSED
        # ----------------------------------------------------

        if (
            old_size != 0
            and new_size == 0
        ):

            self.process_position_transition(
                old_size,
                new_size
            )


            # Re-read after reversal.
            position = get_position(
                self.product_id
            )


            new_size = position[
                "size"
            ]


        self.last_position_size = (
            new_size
        )


        # ----------------------------------------------------
        # MANAGE OPEN POSITION
        # ----------------------------------------------------

        if new_size != 0:

            self.place_or_replace_sl(
                new_size
            )

            return


        # ----------------------------------------------------
        # WAIT FOR OPENING CANDLE
        # ----------------------------------------------------

        if now < (
            self.day
            + timedelta(
                minutes=15
            )
        ):

            return


        # ----------------------------------------------------
        # OPENING CANDLE
        # ----------------------------------------------------

        if not self.opening_candle_ready:

            self.load_opening_candle()


        # ----------------------------------------------------
        # LONG BREAKOUT
        # ----------------------------------------------------

        if price > self.opening_high:

            self.enter(
                "LONG",
                price,
                "opening candle HIGH breakout"
            )

            return


        # ----------------------------------------------------
        # SHORT BREAKOUT
        # ----------------------------------------------------

        if price < self.opening_low:

            self.enter(
                "SHORT",
                price,
                "opening candle LOW breakout"
            )

            return


    # ========================================================
    # MAIN BOT LOOP
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
            "DUPLICATE ENTRY PROTECTION=ON"
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
        # STARTUP POSITION RECONCILIATION
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
                "BOT WILL MANAGE IT."
            )

            logging.warning(
                "BOT WILL NOT OPEN A DUPLICATE POSITION."
            )

            logging.warning(
                "=============================================="
            )


        # ----------------------------------------------------
        # CONTINUOUS LIVE LOOP
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


                # Wait before retrying.
                # Prevents API spam if Delta temporarily
                # returns an error.
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
    # LIVE MODE CHECK
    # --------------------------------------------------------

    if not LIVE_TRADING:

        raise RuntimeError(
            "This bot is configured for LIVE trading only."
        )


    # --------------------------------------------------------
    # START STRATEGY
    # --------------------------------------------------------

    Strategy(
        product
    ).run()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
