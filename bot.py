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

    query_string includes '?' when query parameters exist.
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
    order_id,
    product_id=None
):
    """
    Delta API cancellation.

    IMPORTANT:
    Delta's current API uses:

        DELETE /v2/orders

    with the order ID in the JSON body.
    """

    if not order_id:
        return

    body = {
        "id": int(
            order_id
        )
    }

    if product_id is not None:

        body["product_id"] = int(
            product_id
        )

    logging.info(
        "CANCEL ORDER: %s",
        body
    )

    request(
        "DELETE",
        "/v2/orders",
        body=body,
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
            "product_ids": str(
                int(product_id)
            ),
            "states": (
                "open,pending"
            ),
            "order_types": (
                "all_stop"
            ),
            "page_size": 100,
        },
        authenticated=True,
    )

    return data.get(
        "result",
        []
    )


# ============================================================
# CANCEL BOT STOP ORDERS
# ============================================================

def cancel_all_strategy_stops(
    product_id
):
    """
    Cancel ONLY stop orders created by this bot.

    Bot SL orders use client_order_id beginning with:
        xsl

    This avoids cancelling unrelated manual stop orders.
    """

    orders = get_open_stop_orders(
        product_id
    )

    cancelled = 0

    for order in orders:

        client_order_id = str(
            order.get(
                "client_order_id",
                ""
            )
        )

        # Only cancel this bot's stop orders.
        if not client_order_id.startswith(
            "xsl"
        ):

            continue

        order_id = order.get(
            "id"
        )

        if not order_id:

            continue

        try:

            cancel_order(
                order_id,
                product_id
            )

            cancelled += 1

        except Exception as exc:

            logging.error(
                "Could not cancel bot stop %s: %s",
                order_id,
                exc
            )

    if cancelled:

        logging.info(
            "CANCELLED %s OLD BOT STOP ORDER(S)",
            cancelled
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

        # ----------------------------------------------------
        # IMPORTANT:
        # Prevent repeated opening-breakout entries
        # on the same strategy day.
        #
        # SL reversals do NOT use this restriction.
        # ----------------------------------------------------

        self.last_entry_day = None


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

        # New strategy day means a new
        # opening-breakout entry is allowed.
        self.last_entry_day = None

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

        # ----------------------------------------------------
        # IMPORTANT FIX
        #
        # Do NOT rely only on self.stop_order_id.
        #
        # If the bot restarted, or the previous order ID
        # was lost, an old xsl stop can still remain on Delta.
        #
        # Therefore every time we need a new SL:
        #
        #   1. Find all active/pending bot SLs.
        #   2. Cancel them.
        #   3. Place exactly one new SL.
        # ----------------------------------------------------

        cancel_all_strategy_stops(
            self.product_id
        )

        self.current_sl = None
        self.stop_order_id = None

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

        # ----------------------------------------------------
        # OPENING BREAKOUT PROTECTION
        #
        # Only one initial opening-range entry per day.
        #
        # Reversal entries caused by SL are allowed.
        # ----------------------------------------------------

        is_opening_entry = (
            reason
            == "opening candle HIGH breakout"
            or
            reason
            == "opening candle LOW breakout"
        )

        if (
            is_opening_entry
            and self.last_entry_day == self.day
        ):

            logging.info(
                "OPENING ENTRY ALREADY TAKEN "
                "FOR STRATEGY DAY %s. "
                "IGNORING DUPLICATE SIGNAL.",
                self.day
            )

            return

        # ----------------------------------------------------
        # EXTRA POSITION SAFETY
        #
        # Never send another entry if Delta already
        # reports a position.
        # ----------------------------------------------------

        existing_position = get_position(
            self.product_id
        )

        if existing_position["size"] != 0:

            logging.warning(
                "ENTRY BLOCKED: "
                "POSITION ALREADY EXISTS. "
                "SIZE=%s",
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

        market_order(
            self.product_id,
            side,
            size,
            client_id,
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

                # Mark the opening entry as taken.
                if is_opening_entry:

                    self.last_entry_day = (
                        self.day
                    )

                self.current_sl = None
                self.stop_order_id = None

                self.place_or_replace_sl(
                    position["size"],
                    force=True
                )

                return

        raise RuntimeError(
            "Market entry was sent but "
            "position fill was not confirmed."
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

        # Always remove this bot's old SLs.
        cancel_all_strategy_stops(
            self.product_id
        )

        if size == 0:

            self.current_sl = None
            self.stop_order_id = None

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

        # ----------------------------------------------------
        # IMPORTANT:
        # Remove any old bot stop before reversal.
        #
        # This prevents an old SL from remaining active
        # while the new opposite position is opened.
        # ----------------------------------------------------

        cancel_all_strategy_stops(
            self.product_id
        )

        self.current_sl = None
        self.stop_order_id = None

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
        # DO NOT RE-ENTER OPENING BREAKOUT
        # ----------------------------------------------------
        #
        # Once the initial breakout entry has happened for
        # this strategy day, no further opening breakout
        # entry is allowed.
        #
        # SL reversal is handled separately above.
        # ----------------------------------------------------

        if self.last_entry_day == self.day:

            return

        # ====================================================
        # OPENING RANGE BREAKOUT
        # ====================================================
        #
        # Check recent 1-minute candles so a short breakout
        # is less likely to be missed between polling cycles.
        # ====================================================

        recent_start = now - timedelta(
            minutes=2
        )

        recent_candles = get_candles(
            "1m",
            recent_start,
            now
        )

        for candle in recent_candles:

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

            # Ignore opening candle itself.
            if candle_time <= self.day:

                continue

            candle_high = Decimal(
                str(
                    candle[
                        "high"
                    ]
                )
            )

            candle_low = Decimal(
                str(
                    candle[
                        "low"
                    ]
                )
            )

            # ------------------------------------------------
            # LONG BREAKOUT
            # ------------------------------------------------

            if candle_high > self.opening_high:

                self.enter(
                    "LONG",
                    price,
                    "opening candle HIGH breakout"
                )

                return

            # ------------------------------------------------
            # SHORT BREAKOUT
            # ------------------------------------------------

            if candle_low < self.opening_low:

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
