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
# FIRST TRADE ONLY:
#   The 05:30-05:45 IST 15-minute candle is used ONCE.
#
#   Break 05:30 candle HIGH -> LONG
#   Break 05:30 candle LOW  -> SHORT
#
# AFTER FIRST TRADE:
#   The 05:30 candle is retired as an ENTRY trigger.
#   New trades/reversals may only use NEW extremes formed
#   after the first trade.
#
#   LONG:
#       SL = current post-first-trade LOW
#
#   SHORT:
#       SL = current post-first-trade HIGH
#
#   When OUR stop-loss is actually filled:
#       LONG  -> SHORT
#       SHORT -> LONG
#
# MANUAL CLOSE / MANUAL SL CANCEL:
#   Never treated as an SL hit.
#   Never causes an automatic reversal.
#   Bot stays flat for the rest of that trading day.
#
# POSITION:
#   Maximum one position.
#
# STOP:
#   Exactly ONE protective stop for the current position.
#
# SIZE:
#   10% of current USD/USDT balance as margin.
#   50x leverage.
#
# IMPORTANT:
#   The bot identifies a real SL execution from the actual
#   Delta order state. A cancelled SL is NOT an SL execution.
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


if not API_KEY:
    raise SystemExit("Missing DELTA_API_KEY.")

if not API_SECRET:
    raise SystemExit("Missing DELTA_API_SECRET.")


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
        "User-Agent": "XAUTUSD-OpeningRange-Live-Bot/4.0",
    }
)


# ============================================================
# TIME HELPERS
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


def opening_candle_end(day):
    return day + timedelta(minutes=15)


def is_weekend_block(dt=None):
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
# AUTHENTICATION
# ============================================================

def sign_request(
    method,
    path,
    query_string="",
    body=""
):
    timestamp = str(
        int(time.time())
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
        hashlib.sha256,
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
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if body is not None
        else ""
    )

    query_string = (
        "?"
        + urlencode(
            params,
            doseq=True,
        )
        if params
        else ""
    )

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

    url = BASE_URL + path

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
        f"/v2/products/{SYMBOL}",
    )["result"]


def get_ticker():
    return request(
        "GET",
        f"/v2/tickers/{SYMBOL}",
    )["result"]


# ============================================================
# POSITION
# ============================================================

def get_position(product_id):
    result = request(
        "GET",
        "/v2/positions",
        params={
            "product_id": int(product_id)
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

    wallets = data.get(
        "result",
        []
    )

    # Use actual USD/USDT balance because the requested rule
    # is 10% of the account balance.
    for wallet in wallets:
        asset = str(
            wallet.get(
                "asset_symbol",
                ""
            )
        ).upper()

        if asset not in ("USDT", "USD"):
            continue

        value = wallet.get("balance")

        if value not in (None, ""):
            return Decimal(str(value))

        value = wallet.get(
            "available_balance"
        )

        if value not in (None, ""):
            return Decimal(str(value))

    meta = data.get(
        "meta",
        {}
    )

    value = meta.get(
        "net_equity"
    )

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
        "product_id": int(product_id),
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
        "product_id": int(product_id),
        "product_symbol": SYMBOL,
        "size": int(size),
        "side": side,
        "order_type": "market_order",
        "stop_order_type": "stop_loss_order",
        "stop_price": str(stop_price),
        "stop_trigger_method": "last_traded_price",
        "reduce_only": True,
        "client_order_id": client_id[:32],
    }

    logging.warning(
        "LIVE STOP MARKET ORDER: %s",
        body
    )

    return request(
        "POST",
        "/v2/orders",
        body=body,
        authenticated=True,
    )


# ============================================================
# ORDER LOOKUP
# ============================================================

def get_order(order_id):
    if not order_id:
        return None

    try:
        data = request(
            "GET",
            f"/v2/orders/{order_id}",
            authenticated=True,
        )

        return data.get(
            "result"
        )
    except Exception as exc:
        logging.error(
            "Could not read order %s: %s",
            order_id,
            exc
        )
        return None


def get_order_by_client_id(client_id):
    if not client_id:
        return None

    try:
        data = request(
            "GET",
            f"/v2/orders/client_order_id/{client_id}",
            authenticated=True,
        )

        return data.get(
            "result"
        )
    except Exception as exc:
        logging.error(
            "Could not read client order %s: %s",
            client_id,
            exc
        )
        return None


# ============================================================
# CANCEL ORDER
# ============================================================

def cancel_order(order_id):
    if not order_id:
        return

    logging.info(
        "CANCEL ORDER: %s",
        order_id
    )

    # Keep the endpoint format already used by this bot.
    request(
        "DELETE",
        f"/v2/orders/{order_id}",
        authenticated=True,
    )


# ============================================================
# OPEN STOP ORDERS
# ============================================================

def get_open_stop_orders(product_id):
    data = request(
        "GET",
        "/v2/orders",
        params={
            "product_ids": int(product_id),
            "states": "open,pending",
            "order_types": "all_stop",
        },
        authenticated=True,
    )

    result = data.get(
        "result",
        []
    )

    if isinstance(result, dict):
        return [result]

    return result


# ============================================================
# CANCEL ALL STOP ORDERS
# ============================================================

def cancel_all_strategy_stops(product_id):
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
            cancel_order(order_id)
        except Exception as exc:
            logging.error(
                "Could not cancel stop %s: %s",
                order_id,
                exc
            )


# ============================================================
# LEVERAGE
# ============================================================

def set_leverage(product_id):
    body = {
        "leverage": str(LEVERAGE)
    }

    logging.info(
        "SETTING LEVERAGE: %sx",
        LEVERAGE
    )

    request(
        "POST",
        f"/v2/products/{product_id}/orders/leverage",
        body=body,
        authenticated=True,
    )


# ============================================================
# PRODUCT DECIMAL FIELD
# ============================================================

def decimal_field(
    product,
    *names,
    default=None
):
    for name in names:
        value = product.get(name)

        if value is None:
            continue

        try:
            return Decimal(str(value))
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

    # Exactly 10% of balance is the margin budget.
    target_margin = (
        equity * BALANCE_FRACTION
    )

    # 50x leverage converts margin budget to notional.
    target_notional = (
        target_margin * LEVERAGE
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
            "XAUTUSD product response does not contain "
            "a usable contract_value."
        )

    raw_size = (
        target_notional
        / (price * contract_value)
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

    if lot_size is None or lot_size <= 0:
        lot_size = Decimal("1")

    # FLOOR so the position can never exceed the 10% margin budget.
    size_decimal = (
        (
            raw_size / lot_size
        )
        .to_integral_value(
            rounding=ROUND_DOWN
        )
        * lot_size
    )

    if (
        min_size is not None
        and size_decimal < min_size
    ):
        raise RuntimeError(
            "10% balance budget is below the exchange "
            "minimum order size. "
            f"calculated={size_decimal}, "
            f"minimum={min_size}"
        )

    size = int(size_decimal)

    if size <= 0:
        raise RuntimeError(
            "Calculated position size is zero."
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

    def __init__(self, product):

        self.product = product

        self.product_id = int(
            product["id"]
        )

        # ----------------------------------------------------
        # DAY
        # ----------------------------------------------------

        self.day = None

        # Fixed 05:30-05:45 candle.
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
        # ACTIVE SL
        # ----------------------------------------------------

        self.current_sl = None
        self.stop_order_id = None
        self.stop_client_id = None

        # ----------------------------------------------------
        # STRATEGY STATE
        # ----------------------------------------------------

        self.first_trade_taken = False

        # These are the NEW extremes formed after the first trade.
        # The opening candle is never copied into these values.
        self.post_first_high = None
        self.post_first_low = None

        # First position uses opening candle opposite side.
        self.opening_sl_active = False

        # Manual close/cancel lock for the current day.
        self.manual_close_lock = False

        # Prevent duplicate entries.
        self.entry_in_progress = False

        # Prevent duplicate reversals.
        self.reversal_lock = False

        # A reversal is allowed only when a genuinely new
        # post-first-trade extreme exists for the new position.
        self.have_new_post_first_high = False
        self.have_new_post_first_low = False


    # ========================================================
    # REFRESH DAY
    # ========================================================

    def refresh_day(self, now):

        new_day = trading_day_start(now)

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
        self.opening_candle_ready = False

        self.first_trade_taken = False

        self.post_first_high = None
        self.post_first_low = None

        self.have_new_post_first_high = False
        self.have_new_post_first_low = False

        self.opening_sl_active = False

        self.manual_close_lock = False

        self.current_sl = None
        self.stop_order_id = None
        self.stop_client_id = None

        # Do not use historical "today high/low" to create a
        # post-first-trade extreme. They must be NEW after
        # the first trade.
        #
        # The opening candle is loaded separately below.

        if now >= opening_candle_end(new_day):
            self.load_opening_candle()


    # ========================================================
    # LOAD 05:30-05:45 CANDLE
    # ========================================================

    def load_opening_candle(self):

        if self.opening_candle_ready:
            return

        if self.day is None:
            return

        start = self.day

        end = (
            self.day
            + timedelta(
                minutes=15,
                seconds=1,
            )
        )

        candles = get_candles(
            "15m",
            start,
            end,
        )

        target = None

        for candle in candles:

            candle_time = (
                datetime.fromtimestamp(
                    int(candle["time"]),
                    UTC,
                )
                .astimezone(IST)
            )

            if candle_time == start:
                target = candle
                break

        if target is None:
            raise RuntimeError(
                "05:30-05:45 IST opening candle not found."
            )

        self.opening_high = Decimal(
            str(target["high"])
        )

        self.opening_low = Decimal(
            str(target["low"])
        )

        self.opening_candle_ready = True

        logging.info(
            "OPENING RANGE FIXED | "
            "05:30-05:45 | HIGH=%s | LOW=%s",
            self.opening_high,
            self.opening_low,
        )


    # ========================================================
    # UPDATE POST-FIRST-TRADE EXTREMES
    # ========================================================

    def update_post_first_extremes(self, price):

        if not self.first_trade_taken:
            return

        # ----------------------------------------------------
        # NEW HIGH
        #
        # Must be strictly ABOVE the 05:30 opening high.
        # Therefore the opening high can never become a
        # later reversal SL again.
        # ----------------------------------------------------

        if (
            self.opening_high is not None
            and price > self.opening_high
        ):

            if (
                self.post_first_high is None
                or price > self.post_first_high
            ):
                self.post_first_high = price
                self.have_new_post_first_high = True

        # ----------------------------------------------------
        # NEW LOW
        #
        # Must be strictly BELOW the 05:30 opening low.
        # Therefore the opening low can never become a
        # later reversal SL again.
        # ----------------------------------------------------

        if (
            self.opening_low is not None
            and price < self.opening_low
        ):

            if (
                self.post_first_low is None
                or price < self.post_first_low
            ):
                self.post_first_low = price
                self.have_new_post_first_low = True


    # ========================================================
    # DESIRED SL
    # ========================================================

    def desired_sl(self, position_size):

        # ----------------------------------------------------
        # FIRST TRADE ONLY
        # ----------------------------------------------------

        if self.opening_sl_active:

            if position_size > 0:
                return self.opening_low

            if position_size < 0:
                return self.opening_high

            return None

        # ----------------------------------------------------
        # AFTER FIRST SL / REVERSAL
        #
        # LONG  -> newest NEW LOW
        # SHORT -> newest NEW HIGH
        # ----------------------------------------------------

        if position_size > 0:

            if not self.have_new_post_first_low:
                return None

            return self.post_first_low

        if position_size < 0:

            if not self.have_new_post_first_high:
                return None

            return self.post_first_high

        return None


    # ========================================================
    # STOP SIDE
    # ========================================================

    @staticmethod
    def stop_side(position_size):

        if position_size > 0:
            return "sell"

        if position_size < 0:
            return "buy"

        return None


    # ========================================================
    # STOP PRICE FROM ORDER
    # ========================================================

    @staticmethod
    def order_stop_price(order):

        for key in (
            "stop_price",
            "trigger_price",
            "stop_trigger_price",
        ):

            value = order.get(key)

            if value in (None, ""):
                continue

            try:
                return Decimal(str(value))
            except Exception:
                pass

        return None


    # ========================================================
    # RECONCILE STOP ORDERS
    # ========================================================

    def reconcile_stop_orders(
        self,
        position_size,
        desired
    ):

        orders = get_open_stop_orders(
            self.product_id
        )

        expected_side = self.stop_side(
            position_size
        )

        valid = []

        for order in orders:

            order_id = order.get("id")

            if not order_id:
                continue

            side = str(
                order.get(
                    "side",
                    ""
                )
            ).lower()

            stop_price = self.order_stop_price(
                order
            )

            if (
                side == expected_side
                and desired is not None
                and stop_price == desired
            ):
                valid.append(order)
                continue

            # Any other stop is invalid/extra.
            try:
                logging.warning(
                    "CANCEL INVALID/EXTRA STOP | "
                    "id=%s | side=%s | price=%s",
                    order_id,
                    side,
                    stop_price,
                )

                cancel_order(order_id)

            except Exception as exc:
                logging.error(
                    "Could not cancel invalid stop %s: %s",
                    order_id,
                    exc
                )

        # Keep exactly one valid stop.
        if len(valid) > 1:

            for extra in valid[1:]:

                try:
                    cancel_order(
                        extra.get("id")
                    )
                except Exception as exc:
                    logging.error(
                        "Could not cancel duplicate stop: %s",
                        exc
                    )

            valid = valid[:1]

        if valid:

            self.stop_order_id = valid[0].get("id")

            self.stop_client_id = (
                valid[0].get(
                    "client_order_id"
                )
            )

            self.current_sl = (
                self.order_stop_price(
                    valid[0]
                )
            )

        else:

            self.stop_order_id = None
            self.stop_client_id = None


    # ========================================================
    # PLACE EXACTLY ONE SL
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

        # LONG stop must be below market.
        if (
            position_size > 0
            and desired >= self.last_price
        ):
            logging.warning(
                "LONG SL NOT VALID | SL=%s | LTP=%s",
                desired,
                self.last_price,
            )
            return

        # SHORT stop must be above market.
        if (
            position_size < 0
            and desired <= self.last_price
        ):
            logging.warning(
                "SHORT SL NOT VALID | SL=%s | LTP=%s",
                desired,
                self.last_price,
            )
            return

        # ----------------------------------------------------
        # FIRST check what is already on the exchange.
        # ----------------------------------------------------

        self.reconcile_stop_orders(
            position_size,
            desired,
        )

        # Correct one already exists.
        if (
            not force
            and self.current_sl == desired
            and self.stop_order_id
        ):
            return

        # ----------------------------------------------------
        # Remove every existing strategy stop.
        # ----------------------------------------------------

        orders = get_open_stop_orders(
            self.product_id
        )

        for order in orders:

            order_id = order.get("id")

            if not order_id:
                continue

            try:
                cancel_order(order_id)
            except Exception as exc:
                logging.error(
                    "Cancel existing SL failed: %s",
                    exc
                )

        self.current_sl = None
        self.stop_order_id = None
        self.stop_client_id = None

        # ----------------------------------------------------
        # Create ONE stop only.
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
        self.stop_client_id = client_id

        result_data = result.get(
            "result",
            []
        )

        if isinstance(
            result_data,
            list
        ):

            if result_data:
                self.stop_order_id = (
                    result_data[0].get("id")
                )

        elif isinstance(
            result_data,
            dict
        ):

            self.stop_order_id = (
                result_data.get("id")
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
    # ENTER
    # ========================================================

    def enter(
        self,
        direction,
        price,
        reason
    ):

        if self.entry_in_progress:
            logging.warning(
                "ENTRY ALREADY IN PROGRESS."
            )
            return False

        if is_weekend_block():
            return False

        if self.manual_close_lock:
            return False

        # Never allow a second position.
        current = get_position(
            self.product_id
        )

        if current["size"] != 0:

            logging.warning(
                "ENTRY BLOCKED | Existing position=%s",
                current["size"]
            )

            self.last_position_size = (
                current["size"]
            )

            return False

        # ----------------------------------------------------
        # SIZE = EXACTLY 10% BALANCE MARGIN, 50x LEVERAGE
        # ----------------------------------------------------

        (
            size,
            equity,
            margin,
            notional,
            contract_value,
        ) = calculate_contract_size(
            self.product,
            price,
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

            # Wait for the position to become active.
            for _ in range(30):

                time.sleep(0.2)

                position = get_position(
                    self.product_id
                )

                correct_direction = (
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

                if not correct_direction:
                    continue

                self.last_position_size = (
                    position["size"]
                )

                # ------------------------------------------------
                # FIRST TRADE
                # ------------------------------------------------

                if not self.first_trade_taken:

                    self.first_trade_taken = True

                    # First trade uses ONLY the opening candle
                    # for its initial SL.
                    self.opening_sl_active = True

                    # Start post-first-trade extreme tracking
                    # empty. The 05:30 candle is not copied.
                    self.post_first_high = None
                    self.post_first_low = None

                    self.have_new_post_first_high = False
                    self.have_new_post_first_low = False

                else:

                    # A reversal is a NEW trade. The opening
                    # candle remains retired permanently.
                    self.opening_sl_active = False

                self.current_sl = None
                self.stop_order_id = None
                self.stop_client_id = None

                # Update extremes once using the actual entry.
                self.update_post_first_extremes(
                    price
                )

                # For a reversal, the opposite extreme must
                # already be genuinely new. If it does not
                # exist, no invalid SL is created.
                self.place_or_replace_sl(
                    position["size"],
                    force=True,
                )

                return True

            raise RuntimeError(
                "Market entry was sent but "
                "position fill was not confirmed."
            )

        finally:
            self.entry_in_progress = False


    # ========================================================
    # DETECT REAL SL EXECUTION
    # ========================================================

    def was_our_stop_triggered(self):

        # We need the ACTUAL Delta order state.
        # "closed" = filled.
        # "cancelled" = manually cancelled.
        #
        # If Delta cannot confirm the order, we choose the
        # safe behavior: NO reversal.

        order = None

        if self.stop_order_id:
            order = get_order(
                self.stop_order_id
            )

        if order is None and self.stop_client_id:
            order = get_order_by_client_id(
                self.stop_client_id
            )

        if order is None:
            logging.warning(
                "SL ORDER STATE UNKNOWN -> NO REVERSAL"
            )
            return False

        state = str(
            order.get(
                "state",
                ""
            )
        ).lower()

        logging.info(
            "LAST SL ORDER STATE | id=%s | client=%s | state=%s",
            order.get("id"),
            order.get("client_order_id"),
            state,
        )

        if state == "closed":
            return True

        # cancelled/open/pending/anything unknown:
        # never assume it was triggered.
        return False


    # ========================================================
    # POSITION CLOSED
    # ========================================================

    def handle_position_closed(
        self,
        old_size
    ):

        if old_size == 0:
            return

        sl_triggered = (
            self.was_our_stop_triggered()
        )

        # Remove any remaining protective stops now that
        # the position is flat.
        try:
            cancel_all_strategy_stops(
                self.product_id
            )
        except Exception as exc:
            logging.error(
                "Could not clean stops after flat: %s",
                exc
            )

        self.current_sl = None
        self.stop_order_id = None
        self.stop_client_id = None

        # ----------------------------------------------------
        # MANUAL CLOSE / MANUAL STOP CANCEL
        # ----------------------------------------------------

        if not sl_triggered:

            logging.warning(
                "=============================================="
            )
            logging.warning(
                "POSITION CLOSED WITHOUT OUR SL FILL"
            )
            logging.warning(
                "NO REVERSAL"
            )
            logging.warning(
                "BOT STAYS FLAT FOR TODAY"
            )
            logging.warning(
                "=============================================="
            )

            self.manual_close_lock = True
            self.opening_sl_active = False

            return

        # ----------------------------------------------------
        # REAL SL HIT
        # ----------------------------------------------------

        self.opening_sl_active = False

        if self.reversal_lock:
            return

        if is_weekend_block():
            return

        if self.last_price is None:
            return

        direction = (
            "SHORT"
            if old_size > 0
            else "LONG"
        )

        # ----------------------------------------------------
        # VERY IMPORTANT:
        # Do not reverse using the retired 05:30 candle.
        # ----------------------------------------------------

        if direction == "SHORT":

            if not self.have_new_post_first_high:
                logging.warning(
                    "SL HIT, BUT NO NEW HIGH AFTER FIRST TRADE. "
                    "NO REVERSAL."
                )
                return

        else:

            if not self.have_new_post_first_low:
                logging.warning(
                    "SL HIT, BUT NO NEW LOW AFTER FIRST TRADE. "
                    "NO REVERSAL."
                )
                return

        logging.warning(
            "=============================================="
        )
        logging.warning(
            "OUR STOP LOSS WAS FILLED"
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

        self.reversal_lock = True

        try:

            self.enter(
                direction,
                self.last_price,
                "our SL was actually filled",
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

        # The opening range can be used ONLY ONCE.
        if self.first_trade_taken:
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

        # HIGH BREAK -> FIRST LONG
        if price > self.opening_high:

            self.enter(
                "LONG",
                price,
                "FIRST TRADE | 05:30-05:45 HIGH BREAK",
            )

            return

        # LOW BREAK -> FIRST SHORT
        if price < self.opening_low:

            self.enter(
                "SHORT",
                price,
                "FIRST TRADE | 05:30-05:45 LOW BREAK",
            )

            return


    # ========================================================
    # ONE LOOP
    # ========================================================

    def run_once(self):

        now = now_ist()

        # ----------------------------------------------------
        # PRICE
        # ----------------------------------------------------

        ticker = get_ticker()

        raw_price = (
            ticker.get("close")
            or ticker.get("last_price")
            or ticker.get("mark_price")
        )

        if raw_price is None:
            raise RuntimeError(
                f"Could not find LTP in ticker: {ticker}"
            )

        price = Decimal(
            str(raw_price)
        )

        self.last_price = price

        # ----------------------------------------------------
        # DAY
        # ----------------------------------------------------

        self.refresh_day(now)

        # ----------------------------------------------------
        # AFTER FIRST TRADE:
        # only genuinely NEW extremes beyond the opening
        # candle are recorded.
        # ----------------------------------------------------

        self.update_post_first_extremes(
            price
        )

        # ----------------------------------------------------
        # SATURDAY SQUARE OFF
        # ----------------------------------------------------

        if is_force_squareoff_time(now):

            self.square_off()

            return

        if is_weekend_block(now):
            return

        # ----------------------------------------------------
        # POSITION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        new_size = position["size"]
        old_size = self.last_position_size

        # ----------------------------------------------------
        # POSITION OPENED OUTSIDE THIS LOOP
        # ----------------------------------------------------

        if (
            old_size == 0
            and new_size != 0
        ):

            logging.warning(
                "OPEN POSITION DETECTED | size=%s",
                new_size
            )

            self.last_position_size = new_size

            # Treat it as an already-running position.
            self.first_trade_taken = True
            self.opening_sl_active = False

            # Do not fabricate a 05:30 extreme.
            self.update_post_first_extremes(
                price
            )

            self.place_or_replace_sl(
                new_size,
                force=True,
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
                "POSITION DIRECTION CHANGED | %s -> %s",
                old_size,
                new_size,
            )

            self.last_position_size = new_size
            self.opening_sl_active = False

            self.place_or_replace_sl(
                new_size,
                force=True,
            )

            return

        # ----------------------------------------------------
        # MANAGE OPEN POSITION
        # ----------------------------------------------------

        if new_size != 0:

            self.last_position_size = new_size

            # Dynamic post-first-trade SL.
            self.place_or_replace_sl(
                new_size
            )

            return

        # ----------------------------------------------------
        # FLAT
        # ----------------------------------------------------

        self.last_position_size = 0

        # Remove orphan stops.
        orphan_stops = get_open_stop_orders(
            self.product_id
        )

        if orphan_stops:

            cancel_all_strategy_stops(
                self.product_id
            )

            self.current_sl = None
            self.stop_order_id = None
            self.stop_client_id = None

        # Manual close lock.
        if self.manual_close_lock:
            return

        # ----------------------------------------------------
        # WAIT FOR OPENING CANDLE
        # ----------------------------------------------------

        if now < opening_candle_end(self.day):
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
    # SATURDAY SQUARE OFF
    # ========================================================

    def square_off(self):

        position = get_position(
            self.product_id
        )

        size = position["size"]

        cancel_all_strategy_stops(
            self.product_id
        )

        self.current_sl = None
        self.stop_order_id = None
        self.stop_client_id = None

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
    # MAIN LOOP
    # ========================================================

    def run(self):

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
        # STARTUP POSITION CHECK
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
                "BOT WILL NOT OPEN A DUPLICATE POSITION"
            )
            logging.warning(
                "=============================================="
            )

            self.first_trade_taken = True
            self.opening_sl_active = False

            # If there is already a valid stop, reconcile it.
            # If not, create one only when a valid post-first
            # extreme exists.
            try:

                ticker = get_ticker()

                raw_price = (
                    ticker.get("close")
                    or ticker.get("last_price")
                    or ticker.get("mark_price")
                )

                if raw_price is not None:
                    self.last_price = Decimal(
                        str(raw_price)
                    )

                self.refresh_day(
                    now_ist()
                )

                self.update_post_first_extremes(
                    self.last_price
                )

                self.place_or_replace_sl(
                    position["size"],
                    force=True,
                )

            except Exception as exc:

                logging.error(
                    "Startup position reconciliation failed: %s",
                    exc
                )

        else:

            # Bot starts flat: remove stale stops from previous
            # bot versions before looking for today's first trade.
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
        # LOOP
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

                time.sleep(3)

            time.sleep(
                POLL_SECONDS
            )


# ============================================================
# MAIN
# ============================================================

def main():

    logging.info(
        "CONNECTING TO DELTA INDIA PRODUCTION"
    )

    logging.info(
        "BASE URL: %s",
        BASE_URL
    )

    logging.info(
        "SYMBOL: %s",
        SYMBOL
    )

    product = get_product()

    product_symbol = str(
        product.get(
            "symbol",
            SYMBOL
        )
    ).upper()

    if product_symbol != SYMBOL.upper():

        raise RuntimeError(
            "Requested product symbol does not match API response. "
            f"Requested={SYMBOL} "
            f"Received={product_symbol}"
        )

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

    if not LIVE_TRADING:
        raise RuntimeError(
            "This bot is configured for LIVE trading only."
        )

    Strategy(
        product
    ).run()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
