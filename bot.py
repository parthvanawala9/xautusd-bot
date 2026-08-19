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
# XAUTUSD DELTA INDIA LIVE BOT
# ============================================================
#
# STRATEGY
#
# 1. Trading day starts at 05:30 IST.
#
# 2. FIRST TRADE ONLY:
#       05:30-05:45 candle HIGH/LOW
#
#       Break HIGH -> LONG
#       Break LOW  -> SHORT
#
#       Once this first trade happens, the 05:30 candle
#       HIGH/LOW are NEVER used for another entry that day.
#
# 3. After the first trade:
#       Today's HIGH/LOW are used for the SL.
#
#       LONG  -> SL at today's LOW
#       SHORT -> SL at today's HIGH
#
# 4. If OUR SL triggers:
#       LONG  -> SHORT
#       SHORT -> LONG
#
# 5. Manual close:
#       NO reversal.
#       Bot stays flat for the rest of the day.
#
# 6. Maximum one position.
#
# 7. Exactly ONE stop-market order for the position.
#
# 8. Position size:
#       10% of account equity as margin
#       50x leverage
#
# 9. Saturday 05:00 IST:
#       Square off.
#
# ============================================================


load_dotenv()


# ============================================================
# CONSTANTS
# ============================================================

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

LIVE_TRADING = True

LEVERAGE = Decimal(
    os.getenv("LEVERAGE", "50")
)

BALANCE_FRACTION = Decimal(
    os.getenv("BALANCE_FRACTION", "0.10")
)

POLL_SECONDS = float(
    os.getenv("POLL_SECONDS", "0.50")
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
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "XAUTUSD-Live-Bot",
})


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


def opening_candle_end(day):
    return day + timedelta(minutes=15)


def is_weekend_block(dt=None):
    dt = dt or now_ist()

    # Saturday from 05:00
    if dt.weekday() == 5 and (
        dt.hour > 5 or
        (dt.hour == 5 and dt.minute >= 0)
    ):
        return True

    # Sunday
    if dt.weekday() == 6:
        return True

    # Monday before 05:45
    if dt.weekday() == 0 and (
        dt.hour < 5 or
        (dt.hour == 5 and dt.minute < 45)
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
# AUTH
# ============================================================

def sign_request(
    method,
    path,
    query_string="",
    body=""
):
    timestamp = str(int(time.time()))

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
# REQUEST
# ============================================================

def request(
    method,
    path,
    params=None,
    body=None,
    authenticated=False,
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
        "?" + urlencode(params, doseq=True)
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

    try:
        response = session.request(
            method.upper(),
            BASE_URL + path,
            params=params,
            data=body_text if body is not None else None,
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Network error: {exc}"
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
            f"Invalid JSON from {path}: "
            f"{response.text}"
        ) from exc

    if data.get("success") is False:
        raise RuntimeError(
            f"API error {path}: {data}"
        )

    return data


# ============================================================
# MARKET DATA
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


def get_candles(
    resolution,
    start_dt,
    end_dt,
):
    start = int(
        start_dt.astimezone(UTC).timestamp()
    )

    end = int(
        end_dt.astimezone(UTC).timestamp()
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

    return {
        "size": int(result.get("size", 0)),
        "entry_price": result.get("entry_price"),
        "raw": result,
    }


# ============================================================
# BALANCE
# ============================================================

def get_usdt_equity():
    data = request(
        "GET",
        "/v2/wallet/balances",
        authenticated=True,
    )

    meta = data.get("meta", {})

    if meta.get("net_equity") not in (
        None,
        "",
    ):
        return Decimal(
            str(meta["net_equity"])
        )

    for wallet in data.get("result", []):
        asset = str(
            wallet.get(
                "asset_symbol",
                "",
            )
        ).upper()

        if asset in ("USDT", "USD"):
            return Decimal(
                str(
                    wallet.get(
                        "balance",
                        "0",
                    )
                )
            )

    raise RuntimeError(
        "Could not find USDT/USD balance."
    )


# ============================================================
# PRODUCT FIELDS
# ============================================================

def decimal_field(
    product,
    *names,
    default=None,
):
    for name in names:
        value = product.get(name)

        if value is not None:
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
    price,
):
    """
    EXACTLY 10% ACCOUNT BALANCE AS MARGIN.

    Example:

        Balance = 1000
        10% margin = 100
        50x leverage
        target notional = 5000

    The resulting contract quantity is rounded DOWN,
    so we never exceed the 10% margin allocation.
    """

    equity = get_usdt_equity()

    target_margin = (
        equity * BALANCE_FRACTION
    )

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
            "Invalid XAUTUSD contract value."
        )

    # Delta contract quantity:
    #
    # quantity * price * contract_value
    # = desired notional
    #
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

    if lot_size <= 0:
        lot_size = Decimal("1")

    # ALWAYS ROUND DOWN.
    size = (
        raw_size / lot_size
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * lot_size

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
# MARKET ORDER
# ============================================================

def market_order(
    product_id,
    side,
    size,
    client_id,
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
        "MARKET ORDER: %s",
        body,
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
    client_id,
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
        "STOP MARKET ORDER: %s",
        body,
    )

    return request(
        "POST",
        "/v2/orders",
        body=body,
        authenticated=True,
    )


# ============================================================
# ORDERS
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

    result = data.get("result", [])

    if isinstance(result, dict):
        return [result]

    return result


def cancel_order(order_id):
    if not order_id:
        return

    request(
        "DELETE",
        f"/v2/orders/{order_id}",
        authenticated=True,
    )


def cancel_all_stop_orders(product_id):
    """
    IMPORTANT:
    Remove EVERY open/pending stop for this XAUTUSD
    product before creating the single correct SL.
    """

    orders = get_open_stop_orders(
        product_id
    )

    for order in orders:
        order_id = order.get("id")

        if not order_id:
            continue

        try:
            logging.warning(
                "CANCELLING OLD STOP: %s",
                order_id,
            )

            cancel_order(order_id)

        except Exception as exc:
            logging.error(
                "Could not cancel stop %s: %s",
                order_id,
                exc,
            )


# ============================================================
# LEVERAGE
# ============================================================

def set_leverage(product_id):
    request(
        "POST",
        f"/v2/products/"
        f"{product_id}/orders/leverage",
        body={
            "leverage": str(LEVERAGE)
        },
        authenticated=True,
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

        # Current strategy day.
        self.day = None

        # Today's high/low.
        self.day_high = None
        self.day_low = None

        # FIRST TRADE ONLY range.
        self.opening_high = None
        self.opening_low = None
        self.opening_candle_ready = False

        # Last market price.
        self.last_price = None

        # Position tracking.
        self.last_position_size = 0

        # SL tracking.
        self.stop_order_id = None
        self.current_sl = None

        # IMPORTANT:
        # Once first trade occurs, this becomes TRUE.
        #
        # The 05:30 candle can NEVER trigger another
        # entry after this.
        self.first_trade_taken = False

        # Manual close protection.
        self.manual_close_lock = False

        # Duplicate-entry protection.
        self.entry_in_progress = False

        # Duplicate-reversal protection.
        self.reversal_lock = False


    # ========================================================
    # NEW DAY
    # ========================================================

    def refresh_day(self, now):

        new_day = trading_day_start(now)

        if self.day == new_day:
            return

        logging.info(
            "===================================="
        )
        logging.info(
            "NEW TRADING DAY: %s IST",
            new_day,
        )
        logging.info(
            "===================================="
        )

        self.day = new_day

        self.day_high = None
        self.day_low = None

        self.opening_high = None
        self.opening_low = None
        self.opening_candle_ready = False

        self.first_trade_taken = False
        self.manual_close_lock = False

        self.stop_order_id = None
        self.current_sl = None

        # Build today's high/low from 05:30.
        try:
            candles = get_candles(
                "1m",
                new_day,
                now,
            )

            if candles:

                self.day_high = max(
                    Decimal(str(c["high"]))
                    for c in candles
                )

                self.day_low = min(
                    Decimal(str(c["low"]))
                    for c in candles
                )

        except Exception as exc:

            logging.error(
                "Could not rebuild day high/low: %s",
                exc,
            )

        if now >= opening_candle_end(new_day):
            self.load_opening_candle()


    # ========================================================
    # FIRST 15 MINUTE CANDLE
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
                ).astimezone(IST)
            )

            if candle_time == start:
                target = candle
                break

        if target is None:
            raise RuntimeError(
                "05:30-05:45 candle not found."
            )

        self.opening_high = Decimal(
            str(target["high"])
        )

        self.opening_low = Decimal(
            str(target["low"])
        )

        self.opening_candle_ready = True

        logging.info(
            "FIRST TRADE RANGE FIXED | "
            "HIGH=%s | LOW=%s",
            self.opening_high,
            self.opening_low,
        )


    # ========================================================
    # UPDATE DAY HIGH / LOW
    # ========================================================

    def update_day_extreme(self, price):

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
    # SL LEVEL
    # ========================================================

    def desired_sl(self, position_size):

        if position_size > 0:
            return self.day_low

        if position_size < 0:
            return self.day_high

        return None


    # ========================================================
    # SL SIDE
    # ========================================================

    @staticmethod
    def stop_side(position_size):

        # LONG closes with SELL.
        if position_size > 0:
            return "sell"

        # SHORT closes with BUY.
        if position_size < 0:
            return "buy"

        return None


    # ========================================================
    # CREATE EXACTLY ONE SL
    # ========================================================

    def set_single_sl(
        self,
        position_size,
    ):

        desired = self.desired_sl(
            position_size
        )

        if desired is None:
            return

        if self.last_price is None:
            return

        # LONG SL must be BELOW current price.
        if (
            position_size > 0
            and desired >= self.last_price
        ):
            logging.warning(
                "LONG SL skipped: "
                "day low is not below current price."
            )
            return

        # SHORT SL must be ABOVE current price.
        if (
            position_size < 0
            and desired <= self.last_price
        ):
            logging.warning(
                "SHORT SL skipped: "
                "day high is not above current price."
            )
            return

        # ====================================================
        # CRITICAL:
        # CANCEL ALL OLD STOP ORDERS FIRST.
        #
        # We do NOT trust self.stop_order_id.
        # This prevents stale orders such as the old
        # 4353.22 order remaining together with 4358.96.
        # ====================================================

        cancel_all_stop_orders(
            self.product_id
        )

        self.stop_order_id = None
        self.current_sl = None

        # ====================================================
        # CREATE ONLY ONE STOP.
        # ====================================================

        side = self.stop_side(
            position_size
        )

        size = abs(
            position_size
        )

        result = stop_market_order(
            self.product_id,
            side,
            size,
            desired,
            (
                "xsl"
                + str(
                    int(
                        time.time() * 1000
                    )
                )
            ),
        )

        self.current_sl = desired

        result_value = result.get(
            "result"
        )

        if isinstance(
            result_value,
            list,
        ) and result_value:

            self.stop_order_id = (
                result_value[0].get("id")
            )

        elif isinstance(
            result_value,
            dict,
        ):

            self.stop_order_id = (
                result_value.get("id")
            )

        logging.info(
            "ONE SL ACTIVE | "
            "POSITION=%s | "
            "SIDE=%s | "
            "SL=%s | "
            "ORDER=%s",
            (
                "LONG"
                if position_size > 0
                else "SHORT"
            ),
            side,
            desired,
            self.stop_order_id,
        )


    # ========================================================
    # ENTER
    # ========================================================

    def enter(
        self,
        direction,
        price,
        reason,
    ):

        # NEVER allow duplicate entry.
        if self.entry_in_progress:
            return False

        if self.manual_close_lock:
            return False

        if is_weekend_block():
            return False

        current = get_position(
            self.product_id
        )

        # NEVER open a second position.
        if current["size"] != 0:

            self.last_position_size = (
                current["size"]
            )

            logging.warning(
                "ENTRY BLOCKED: "
                "position already exists: %s",
                current["size"],
            )

            return False

        # ====================================================
        # EXACT 10% BALANCE CALCULATION
        # ====================================================

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

        logging.warning(
            "===================================="
        )

        logging.warning(
            "NEW %s",
            direction,
        )

        logging.warning(
            "ENTRY PRICE: %s",
            price,
        )

        logging.warning(
            "ACCOUNT EQUITY: %s",
            equity,
        )

        logging.warning(
            "10%% MARGIN: %s",
            margin,
        )

        logging.warning(
            "50x NOTIONAL: %s",
            notional,
        )

        logging.warning(
            "CONTRACT VALUE: %s",
            contract_value,
        )

        logging.warning(
            "ORDER SIZE: %s",
            size,
        )

        logging.warning(
            "REASON: %s",
            reason,
        )

        logging.warning(
            "===================================="
        )

        self.entry_in_progress = True

        try:

            market_order(
                self.product_id,
                side,
                size,
                (
                    "xent"
                    + str(
                        int(
                            time.time() * 1000
                        )
                    )
                ),
            )

            # Wait for actual position.
            for _ in range(30):

                time.sleep(0.2)

                position = get_position(
                    self.product_id
                )

                actual_size = position[
                    "size"
                ]

                if (
                    direction == "LONG"
                    and actual_size > 0
                ) or (
                    direction == "SHORT"
                    and actual_size < 0
                ):

                    self.last_position_size = (
                        actual_size
                    )

                    # ==================================================
                    # CRITICAL:
                    # First trade is now permanently consumed.
                    #
                    # The 05:30 range can NEVER be used for another
                    # entry during this trading day.
                    # ==================================================

                    self.first_trade_taken = True

                    self.current_sl = None
                    self.stop_order_id = None

                    # Place exactly ONE SL.
                    self.set_single_sl(
                        actual_size
                    )

                    return True

            raise RuntimeError(
                "Entry sent but position "
                "was not confirmed."
            )

        finally:

            self.entry_in_progress = False


    # ========================================================
    # SQUARE OFF
    # ========================================================

    def square_off(self):

        position = get_position(
            self.product_id
        )

        size = position["size"]

        cancel_all_stop_orders(
            self.product_id
        )

        self.stop_order_id = None
        self.current_sl = None

        if size == 0:
            self.last_position_size = 0
            return

        side = (
            "sell"
            if size > 0
            else "buy"
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
    # WAS OUR SL TRIGGERED?
    # ========================================================

    def was_our_sl_triggered(self):

        """
        Manual close:
            our stop is still present
            -> NOT a reversal

        Our SL:
            our stop disappeared
            -> reversal

        """

        if not self.stop_order_id:
            return False

        orders = get_open_stop_orders(
            self.product_id
        )

        for order in orders:

            if str(order.get("id")) == str(
                self.stop_order_id
            ):
                return False

        return True


    # ========================================================
    # POSITION CLOSED
    # ========================================================

    def handle_position_closed(
        self,
        old_size,
    ):

        if old_size == 0:
            return

        sl_triggered = (
            self.was_our_sl_triggered()
        )

        # Always clean remaining stops.
        cancel_all_stop_orders(
            self.product_id
        )

        self.stop_order_id = None
        self.current_sl = None

        # ====================================================
        # MANUAL CLOSE
        # ====================================================

        if not sl_triggered:

            logging.warning(
                "MANUAL CLOSE DETECTED."
            )

            logging.warning(
                "NO REVERSAL."
            )

            logging.warning(
                "BOT WILL STAY FLAT TODAY."
            )

            self.manual_close_lock = True

            return

        # ====================================================
        # OUR SL WAS HIT
        # ====================================================

        if self.reversal_lock:
            return

        if is_weekend_block():
            return

        if self.last_price is None:
            return

        new_direction = (
            "SHORT"
            if old_size > 0
            else "LONG"
        )

        logging.warning(
            "OUR SL WAS HIT."
        )

        logging.warning(
            "REVERSING -> %s",
            new_direction,
        )

        self.reversal_lock = True

        try:

            self.enter(
                new_direction,
                self.last_price,
                "strategy SL triggered",
            )

        finally:

            self.reversal_lock = False


    # ========================================================
    # FIRST TRADE ONLY
    # ========================================================

    def check_first_trade(
        self,
        price,
    ):

        # ====================================================
        # THIS IS THE CRITICAL LOCK.
        #
        # Once first_trade_taken=True:
        # the 05:30 candle HIGH/LOW are completely ignored.
        # ====================================================

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

        # First HIGH breakout.
        if price > self.opening_high:

            self.enter(
                "LONG",
                price,
                "FIRST TRADE - 05:30 HIGH BREAK",
            )

            return

        # First LOW breakout.
        if price < self.opening_low:

            self.enter(
                "SHORT",
                price,
                "FIRST TRADE - 05:30 LOW BREAK",
            )

            return


    # ========================================================
    # MAIN STRATEGY LOOP
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
                f"No price in ticker: {ticker}"
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
        # DAY HIGH / LOW
        # ----------------------------------------------------

        self.update_day_extreme(
            price
        )

        # ----------------------------------------------------
        # SATURDAY SQUARE OFF
        # ----------------------------------------------------

        if is_force_squareoff_time(now):

            self.square_off()

            return

        # ----------------------------------------------------
        # WEEKEND
        # ----------------------------------------------------

        if is_weekend_block(now):
            return

        # ----------------------------------------------------
        # CURRENT POSITION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        new_size = position["size"]

        old_size = self.last_position_size

        # ----------------------------------------------------
        # POSITION APPEARED
        # ----------------------------------------------------

        if (
            old_size == 0
            and new_size != 0
        ):

            self.last_position_size = (
                new_size
            )

            # If bot starts/reconnects with an existing
            # position, never open another one.
            self.first_trade_taken = True

            self.set_single_sl(
                new_size
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

            self.last_position_size = (
                new_size
            )

            self.current_sl = None
            self.stop_order_id = None

            self.set_single_sl(
                new_size
            )

            return

        # ----------------------------------------------------
        # POSITION EXISTS
        # ----------------------------------------------------

        if new_size != 0:

            self.last_position_size = (
                new_size
            )

            # Keep EXACTLY ONE SL.
            #
            # If today's high/low changed, the old SL is
            # cancelled and replaced by ONE new SL.
            self.set_single_sl(
                new_size
            )

            return

        # ----------------------------------------------------
        # FLAT
        # ----------------------------------------------------

        self.last_position_size = 0

        # Never leave stop orders while flat.
        existing_stops = get_open_stop_orders(
            self.product_id
        )

        if existing_stops:

            cancel_all_stop_orders(
                self.product_id
            )

            self.stop_order_id = None
            self.current_sl = None

        # Manual close means stay flat.
        if self.manual_close_lock:
            return

        # ----------------------------------------------------
        # WAIT UNTIL FIRST 15-MINUTE CANDLE IS COMPLETE
        # ----------------------------------------------------

        if now < opening_candle_end(
            self.day
        ):
            return

        # ----------------------------------------------------
        # LOAD 05:30-05:45 CANDLE
        # ----------------------------------------------------

        if not self.opening_candle_ready:

            self.load_opening_candle()

        # ----------------------------------------------------
        # FIRST TRADE ONLY
        # ----------------------------------------------------

        self.check_first_trade(
            price
        )


    # ========================================================
    # BOT LOOP
    # ========================================================

    def run(self):

        logging.info(
            "===================================="
        )

        logging.info(
            "XAUTUSD BOT STARTING"
        )

        logging.info(
            "SYMBOL: %s",
            SYMBOL,
        )

        logging.info(
            "LEVERAGE: %sx",
            LEVERAGE,
        )

        logging.info(
            "BALANCE FRACTION: %s%%",
            BALANCE_FRACTION * 100,
        )

        logging.info(
            "LIVE TRADING: %s",
            LIVE_TRADING,
        )

        logging.info(
            "===================================="
        )

        if not LIVE_TRADING:
            raise RuntimeError(
                "LIVE_TRADING is disabled."
            )

        # ----------------------------------------------------
        # SET 50x
        # ----------------------------------------------------

        set_leverage(
            self.product_id
        )

        # ----------------------------------------------------
        # STARTUP POSITION
        # ----------------------------------------------------

        position = get_position(
            self.product_id
        )

        self.last_position_size = (
            position["size"]
        )

        if position["size"] != 0:

            logging.warning(
                "BOT STARTED WITH OPEN POSITION: %s",
                position["size"],
            )

            # Existing position means the first trade
            # has already happened.
            self.first_trade_taken = True

            # Manage it, don't duplicate it.
            self.set_single_sl(
                position["size"]
            )

        else:

            # IMPORTANT:
            # Clean all old stops before starting.
            cancel_all_stop_orders(
                self.product_id
            )

        # ----------------------------------------------------
        # CONTINUOUS LOOP
        # ----------------------------------------------------

        while True:

            try:

                self.run_once()

            except KeyboardInterrupt:

                logging.warning(
                    "BOT STOPPED."
                )

                break

            except Exception as exc:

                logging.exception(
                    "BOT ERROR: %s",
                    exc,
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
        "Connecting to Delta India..."
    )

    product = get_product()

    product_symbol = str(
        product.get(
            "symbol",
            SYMBOL,
        )
    ).upper()

    if product_symbol != SYMBOL.upper():

        raise RuntimeError(
            "Wrong product returned. "
            f"Expected={SYMBOL}, "
            f"Received={product_symbol}"
        )

    state = str(
        product.get(
            "state",
            "",
        )
    ).lower()

    if state and state not in (
        "live",
        "active",
        "listed",
    ):

        raise RuntimeError(
            f"XAUTUSD is not live. "
            f"State={state}"
        )

    Strategy(product).run()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
