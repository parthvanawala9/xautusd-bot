import os
import time
import json
import hmac
import hashlib
import logging
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# XAUTUSD DELTA LIVE AUTO-TRADING BOT
#
# LIVE TRADING ONLY
#
# This bot is permanently configured for:
#   Delta Exchange India PRODUCTION API
#   https://api.india.delta.exchange
#
# There is NO testnet mode.
# There is NO demo mode.
# There is NO dry-run mode.
# Orders sent by this bot are REAL orders.
#
# Strategy:
#   Trading day = 05:30 IST -> next day 05:30 IST
#
#   Opening candle = 05:30-05:45 IST
#
#   If flat after 05:45:
#       break opening HIGH -> market LONG, SL = day LOW
#       break opening LOW  -> market SHORT, SL = day HIGH
#
#   If already in a position at a new 05:30:
#       DO NOT open a new trade.
#       Reset the day's reference extremes and replace the SL
#       as the new day's LOW (long) / HIGH (short) develops.
#
#   SL trigger = last traded price, stop-market, reduce-only.
#
#   When SL closes a position:
#       immediately reverse with a MARKET order.
#
#   No profit target.
#
#   Friday trade may continue overnight.
#
#   Saturday 05:00 IST:
#       force square-off and stop trading.
#
#   No Saturday/Sunday entries.
#
#   Monday 05:45 IST:
#       new entries allowed.
#
# LIVE TRADING:
#   Always enabled.
#   No environment variable can disable live trading.
# ============================================================

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

# ============================================================
# PRODUCTION ONLY
# ============================================================

BASE_URL = "https://api.india.delta.exchange"

SYMBOL = os.getenv("DELTA_SYMBOL", "XAUTUSD")

API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")

# PERMANENTLY LIVE.
# There is intentionally no testnet/dry-run switch.
LIVE_TRADING = True

LEVERAGE = Decimal(os.getenv("LEVERAGE", "50"))
BALANCE_FRACTION = Decimal(os.getenv("BALANCE_FRACTION", "0.10"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "0.50"))

# The bot requires real Delta API credentials.
if not API_KEY or not API_SECRET:
    raise SystemExit(
        "Missing DELTA_API_KEY / DELTA_API_SECRET in .env"
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

session = requests.Session()

session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "XAUTUSD-Live-OpeningRange-Bot/1.0",
})


def now_ist():
    return datetime.now(IST)


def trading_day_start(dt=None):
    """
    Returns the current strategy day boundary at 05:30 IST.
    """
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


def is_weekend_block(dt=None):
    """
    Saturday 05:00 through Monday 05:45 is blocked.

    Friday trading can continue overnight until
    Saturday 05:00 IST.
    """

    dt = dt or now_ist()

    wd = dt.weekday()

    # Saturday from 05:00 onward.
    if wd == 5 and dt.time() >= datetime.strptime(
        "05:00",
        "%H:%M"
    ).time():
        return True

    # Entire Sunday.
    if wd == 6:
        return True

    # Monday before 05:45.
    if wd == 0 and dt.time() < datetime.strptime(
        "05:45",
        "%H:%M"
    ).time():
        return True

    return False


def is_force_squareoff_time(dt=None):
    dt = dt or now_ist()

    return (
        dt.weekday() == 5
        and dt.hour == 5
        and dt.minute >= 0
    )


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

    headers = {
        "api-key": API_KEY,
        "signature": signature,
        "timestamp": timestamp,
    }

    return headers


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

    query = "&".join(
        f"{k}={v}"
        for k, v in params.items()
    )

    headers = {}

    if authenticated:
        headers.update(
            sign_request(
                method,
                path,
                query,
                body_text,
            )
        )

    url = BASE_URL + path

    r = session.request(
        method.upper(),
        url,
        params=params,
        data=body_text if body is not None else None,
        headers=headers,
        timeout=15,
    )

    if not r.ok:
        raise RuntimeError(
            f"{method} {path} HTTP {r.status_code}: {r.text}"
        )

    data = r.json()

    if data.get("success") is False:
        raise RuntimeError(
            f"{method} {path}: {data}"
        )

    return data


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


def get_position(product_id):
    result = request(
        "GET",
        "/v2/positions",
        params={
            "product_id": product_id,
        },
        authenticated=True,
    )["result"]

    if not result:
        return {
            "size": 0,
            "entry_price": None,
        }

    size = int(
        result.get(
            "size",
            0,
        )
    )

    return {
        "size": size,
        "entry_price": result.get(
            "entry_price"
        ),
        "raw": result,
    }


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
        {},
    )

    if meta.get("net_equity") not in (
        None,
        "",
    ):
        return Decimal(
            str(
                meta["net_equity"]
            )
        )

    for wallet in data.get(
        "result",
        [],
    ):
        if str(
            wallet.get(
                "asset_symbol",
                "",
            )
        ).upper() in (
            "USDT",
            "USD",
        ):
            return Decimal(
                str(
                    wallet.get(
                        "balance",
                        "0",
                    )
                )
            )

    raise RuntimeError(
        "Could not find account equity / USDT balance."
    )


def get_candles(
    resolution,
    start_dt,
    end_dt,
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

    # ========================================================
    # REAL ORDER
    # ========================================================

    logging.warning(
        "LIVE MARKET ORDER | %s",
        body,
    )

    return request(
        "POST",
        "/v2/orders",
        body=body,
        authenticated=True,
    )


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

    # ========================================================
    # REAL STOP ORDER
    # ========================================================

    logging.warning(
        "LIVE STOP ORDER | %s",
        body,
    )

    return request(
        "POST",
        "/v2/orders",
        body=body,
        authenticated=True,
    )


def cancel_order(order_id):
    if not order_id:
        return

    request(
        "DELETE",
        f"/v2/orders/{order_id}",
        authenticated=True,
    )


def get_open_stop_orders(product_id):
    data = request(
        "GET",
        "/v2/orders",
        params={
            "product_ids": product_id,
            "states": "open,pending",
            "order_types": "all_stop",
        },
        authenticated=True,
    )

    return data.get(
        "result",
        [],
    )


def cancel_all_strategy_stops(product_id):
    orders = get_open_stop_orders(
        product_id
    )

    for order in orders:
        try:
            cancel_order(
                order.get("id")
            )

        except Exception as e:
            logging.error(
                "Could not cancel stop %s: %s",
                order.get("id"),
                e,
            )


def set_leverage(product_id):
    body = {
        "leverage": str(
            LEVERAGE
        )
    }

    logging.warning(
        "SETTING LIVE LEVERAGE: %sx",
        LEVERAGE,
    )

    request(
        "POST",
        f"/v2/products/{product_id}/orders/leverage",
        body=body,
        authenticated=True,
    )


def decimal_field(
    product,
    *names,
    default=None,
):
    for name in names:
        if product.get(name) is not None:
            try:
                return Decimal(
                    str(
                        product[name]
                    )
                )
            except Exception:
                pass

    return default


def calculate_contract_size(
    product,
    price,
):
    """
    Target notional =
        10% of current equity * 50x leverage.

    Contract size is calculated from the actual
    XAUTUSD product metadata.
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
            "XAUTUSD product response did not expose "
            "a usable contract_value."
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

    if lot_size <= 0:
        lot_size = Decimal("1")

    size = (
        raw_size
        / lot_size
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


class Strategy:

    def __init__(
        self,
        product,
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

        self.opening_candle_ready = False

        self.last_price = None

        self.last_position_size = 0

        self.current_sl = None
        self.stop_order_id = None

        self.reversal_lock = False

        self.last_entry_day = None

    def refresh_day(
        self,
        now,
    ):
        new_day = trading_day_start(
            now
        )

        if self.day == new_day:
            return

        logging.info(
            "NEW STRATEGY DAY: %s IST",
            new_day,
        )

        self.day = new_day

        self.opening_high = None
        self.opening_low = None

        self.opening_candle_ready = False

        try:
            candles = get_candles(
                "1m",
                new_day,
                now,
            )

            if candles:
                self.day_high = max(
                    Decimal(
                        str(
                            c["high"]
                        )
                    )
                    for c in candles
                )

                self.day_low = min(
                    Decimal(
                        str(
                            c["low"]
                        )
                    )
                    for c in candles
                )

            else:
                self.day_high = self.last_price
                self.day_low = self.last_price

        except Exception as e:
            logging.error(
                "Could not rebuild day High/Low: %s",
                e,
            )

            self.day_high = self.last_price
            self.day_low = self.last_price

        if (
            now
            >= new_day
            + timedelta(minutes=15)
        ):
            self.load_opening_candle()

    def load_opening_candle(self):

        if self.opening_candle_ready:
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
                    int(
                        candle["time"]
                    ),
                    UTC,
                )
                .astimezone(IST)
            )

            if candle_time == start:
                target = candle
                break

        if target is None:
            raise RuntimeError(
                "5:30–5:45 IST opening candle not found."
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

        self.opening_candle_ready = True

        logging.info(
            "OPENING CANDLE 05:30-05:45 | High=%s Low=%s",
            self.opening_high,
            self.opening_low,
        )

    def update_extreme(
        self,
        price,
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

    def desired_sl(
        self,
        position_size,
    ):
        if position_size > 0:
            return self.day_low

        if position_size < 0:
            return self.day_high

        return None

    def place_or_replace_sl(
        self,
        position_size,
        force=False,
    ):
        desired = self.desired_sl(
            position_size
        )

        if desired is None:
            return

        if self.last_price is None:
            return

        # LONG stop must be below current price.
        if (
            position_size > 0
            and desired >= self.last_price
        ):
            return

        # SHORT stop must be above current price.
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

        if self.stop_order_id:
            try:
                cancel_order(
                    self.stop_order_id
                )

            except Exception as e:
                logging.error(
                    "Cancel old SL failed: %s",
                    e,
                )

        side = (
            "sell"
            if position_size > 0
            else "buy"
        )

        size = abs(
            position_size
        )

        cid = (
            f"xsl{int(time.time()*1000)}"
        )

        result = stop_market_order(
            self.product_id,
            side,
            size,
            desired,
            cid,
        )

        self.current_sl = desired

        try:

            result_list = result.get(
                "result",
                [],
            )

            if (
                isinstance(
                    result_list,
                    list,
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
                dict,
            ):
                self.stop_order_id = (
                    result_list.get(
                        "id"
                    )
                )

        except Exception:
            self.stop_order_id = None

        logging.info(
            "LIVE SL SET | position=%s | SL=%s | side=%s",
            (
                "LONG"
                if position_size > 0
                else "SHORT"
            ),
            desired,
            side,
        )

    def enter(
        self,
        direction,
        price,
        reason,
    ):
        if is_weekend_block():
            return

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

        cid = (
            f"xent{int(time.time()*1000)}"
        )

        logging.warning(
            "================================================"
        )

        logging.warning(
            "LIVE ENTRY %s",
            direction,
        )

        logging.warning(
            "Price=%s | Size=%s | Equity=%s | "
            "Margin=%s | Notional=%s",
            price,
            size,
            equity,
            margin,
            notional,
        )

        logging.warning(
            "Reason=%s",
            reason,
        )

        logging.warning(
            "================================================"
        )

        # REAL MARKET ORDER
        market_order(
            self.product_id,
            side,
            size,
            cid,
        )

        # Wait for Delta position state.
        for _ in range(30):

            time.sleep(0.2)

            pos = get_position(
                self.product_id
            )

            if (
                direction == "LONG"
                and pos["size"] > 0
            ) or (
                direction == "SHORT"
                and pos["size"] < 0
            ):

                self.last_position_size = (
                    pos["size"]
                )

                self.current_sl = None
                self.stop_order_id = None

                self.place_or_replace_sl(
                    pos["size"],
                    force=True,
                )

                return

        raise RuntimeError(
            "Live market entry was sent but "
            "position fill was not confirmed."
        )

    def square_off(self):

        pos = get_position(
            self.product_id
        )

        size = pos["size"]

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
            "================================================"
        )

        logging.warning(
            "LIVE FORCE SQUARE-OFF | size=%s",
            size,
        )

        logging.warning(
            "================================================"
        )

        market_order(
            self.product_id,
            side,
            abs(size),
            f"xoff{int(time.time()*1000)}",
        )

        self.current_sl = None
        self.stop_order_id = None

    def process_position_transition(
        self,
        old_size,
        new_size,
    ):
        # A reduce-only SL closed the position.
        # Immediately reverse.
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

        direction = (
            "SHORT"
            if old_size > 0
            else "LONG"
        )

        logging.warning(
            "LIVE SL EXIT DETECTED | old=%s | "
            "reversing to %s",
            old_size,
            direction,
        )

        self.reversal_lock = True

        try:

            self.current_sl = None
            self.stop_order_id = None

            self.enter(
                direction,
                self.last_price,
                "SL reversal",
            )

        finally:
            self.reversal_lock = False

    def run_once(self):

        now = now_ist()

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

        self.refresh_day(
            now
        )

        self.update_extreme(
            price
        )

        # Saturday square-off.
        if (
            now.weekday() == 5
            and now.hour == 5
            and now.minute >= 0
        ):

            if now.minute < 5:
                self.square_off()

            return

        if is_weekend_block():
            return

        pos = get_position(
            self.product_id
        )

        new_size = pos["size"]

        old_size = self.last_position_size

        # Existing position handling.
        if (
            old_size == 0
            and new_size != 0
        ):

            logging.info(
                "Existing live position detected: %s",
                new_size,
            )

        # Detect SL closure.
        if (
            old_size != 0
            and new_size == 0
        ):

            self.process_position_transition(
                old_size,
                new_size,
            )

            pos = get_position(
                self.product_id
            )

            new_size = pos["size"]

        self.last_position_size = new_size

        # ====================================================
        # EXISTING POSITION
        #
        # Never open a second trade.
        # Only manage the live position and SL.
        # ====================================================

        if new_size != 0:

            self.place_or_replace_sl(
                new_size
            )

            return

        # ====================================================
        # FLAT
        #
        # Entry only after opening candle completes.
        # ====================================================

        if now < (
            self.day
            + timedelta(
                minutes=15
            )
        ):
            return

        if not self.opening_candle_ready:
            self.load_opening_candle()

        # LONG breakout.
        if (
            price
            > self.opening_high
        ):

            self.enter(
                "LONG",
                price,
                "opening candle HIGH breakout",
            )

            return

        # SHORT breakout.
        if (
            price
            < self.opening_low
        ):

            self.enter(
                "SHORT",
                price,
                "opening candle LOW breakout",
            )

            return

    def run(self):

        logging.warning(
            "================================================"
        )

        logging.warning(
            "XAUTUSD LIVE TRADING BOT STARTING"
        )

        logging.warning(
            "PRODUCTION API:"
        )

        logging.warning(
            "%s",
            BASE_URL,
        )

        logging.warning(
            "SYMBOL=%s",
            SYMBOL,
        )

        logging.warning(
            "LIVE_TRADING=True"
        )

        logging.warning(
            "LEVERAGE=%sx",
            LEVERAGE,
        )

        logging.warning(
            "BALANCE FRACTION=%s%%",
            BALANCE_FRACTION * 100,
        )

        logging.warning(
            "REAL MONEY TRADING IS ENABLED."
        )

        logging.warning(
            "================================================"
        )

        # Live leverage setting.
        set_leverage(
            self.product_id
        )

        # Reconcile any existing live position.
        pos = get_position(
            self.product_id
        )

        self.last_position_size = (
            pos["size"]
        )

        if pos["size"] != 0:

            logging.warning(
                "BOT STARTED WITH OPEN LIVE POSITION: %s",
                pos["size"],
            )

            logging.warning(
                "It will manage the existing position "
                "and will NOT open a duplicate trade."
            )

        while True:

            try:

                self.run_once()

            except KeyboardInterrupt:

                logging.warning(
                    "Stopped by user."
                )

                break

            except Exception as e:

                logging.exception(
                    "LIVE BOT ERROR: %s",
                    e,
                )

                time.sleep(3)

            time.sleep(
                POLL_SECONDS
            )


def main():

    logging.warning(
        "Connecting to Delta Exchange India PRODUCTION..."
    )

    product = get_product()

    logging.info(
        "PRODUCT RESPONSE:"
    )

    logging.info(
        json.dumps(
            product,
            indent=2,
        )
    )

    if str(
        product.get(
            "symbol",
            SYMBOL,
        )
    ).upper() != SYMBOL.upper():

        raise RuntimeError(
            "Requested product symbol does not match API response."
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
            "XAUTUSD product is not live/active. "
            f"State={state}"
        )

    Strategy(
        product
    ).run()


if __name__ == "__main__":
    main()
