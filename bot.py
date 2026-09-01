import os
import time
import json
import hmac
import hashlib
import logging
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import requests
import websocket
from dotenv import load_dotenv


# ============================================================
# XAUTUSD SIMPLE NEW HIGH / NEW LOW BOT
#
# TRADING RULES
# ------------------------------------------------------------
# 05:45 IST = NEW TRADING SESSION
#
# IMPORTANT:
#   BOT MUST BE MANUALLY STARTED FROM DASHBOARD.
#
# STOP BOT:
#   - Closes existing position.
#   - Saves completed trade as MANUAL_STOP.
#   - Prevents all new entries.
#
# START BOT:
#   - Enables trading.
#   - Only then can a new position be taken.
#
# FLAT:
#   price > HIGH -> LONG
#   price < LOW  -> SHORT
#
# LONG:
#   new HIGH -> update HIGH only
#   no second LONG
#
# SHORT:
#   new LOW -> update LOW only
#   no second SHORT
#
# SL:
#   LONG  SL = LOW at entry
#   SHORT SL = HIGH at entry
#
# If SL closes position:
#   LONG -> SHORT
#   SHORT -> LONG
#
# One position at a time.
#
# Restart after 05:45:
#   Recover today's historical 1-minute HIGH/LOW.
#
# Saturday 05:00:
#   Close position.
#
# DASHBOARD:
#   Runs from THIS SAME bot.py process.
#
# TRADE HISTORY:
#   Completed trades are permanently stored in
#   trade_history.json.
#
#   Dashboard shows:
#   - Today's statistics
#   - All-time statistics
#   - All completed trades
# ============================================================


load_dotenv()


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")

WS_URL = os.getenv(
    "DELTA_PUBLIC_WS_URL",
    "wss://public-socket.india.delta.exchange"
)

SYMBOL = os.getenv(
    "DELTA_SYMBOL",
    "XAUTUSD"
).strip()

API_KEY = os.getenv(
    "DELTA_API_KEY",
    ""
).strip()

API_SECRET = os.getenv(
    "DELTA_API_SECRET",
    ""
).strip()

LEVERAGE = Decimal(
    os.getenv("LEVERAGE", "50")
)

BALANCE_FRACTION = Decimal(
    os.getenv("BALANCE_FRACTION", "0.10")
)

STATE_FILE = os.getenv(
    "STATE_FILE",
    os.path.join(
        BASE_DIR,
        "xautusd_state.json"
    )
)

TRADE_HISTORY_FILE = os.getenv(
    "TRADE_HISTORY_FILE",
    os.path.join(
        BASE_DIR,
        "trade_history.json"
    )
)

DASHBOARD_PORT = int(
    os.getenv(
        "DASHBOARD_PORT",
        "8000"
    )
)

RECONNECT_SECONDS = 3


if not API_KEY or not API_SECRET:

    raise SystemExit(
        "Missing DELTA_API_KEY or DELTA_API_SECRET."
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "XAUTUSD-Simple-Bot/1.0"
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
        microsecond=0
    )

    if dt < boundary:

        boundary -= timedelta(
            days=1
        )

    return boundary


def strategy_start(day_start):

    return day_start.replace(
        hour=5,
        minute=45,
        second=0,
        microsecond=0
    )


def weekend(dt=None):

    dt = dt or now_ist()

    if dt.weekday() == 5:

        return dt.hour >= 5

    if dt.weekday() == 6:

        return True

    return False


def saturday_squareoff(dt=None):

    dt = dt or now_ist()

    return (
        dt.weekday() == 5
        and dt.hour == 5
    )


# ============================================================
# AUTH
# ============================================================

def sign(
    method,
    path,
    query="",
    body=""
):

    timestamp = str(
        int(time.time())
    )

    message = (
        method.upper()
        + timestamp
        + path
        + query
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
# REST
# ============================================================

def api(
    method,
    path,
    params=None,
    body=None,
    auth=False
):

    params = params or {}

    body_text = ""

    if body is not None:

        body_text = json.dumps(
            body,
            separators=(",", ":")
        )

    query = ""

    if params:

        query = "?" + urlencode(
            params,
            doseq=True
        )

    headers = {}

    if auth:

        headers = sign(
            method,
            path,
            query,
            body_text
        )

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
        timeout=(5, 15)
    )

    response.raise_for_status()

    data = response.json()

    if data.get("success") is False:

        raise RuntimeError(
            f"Delta error: {data}"
        )

    return data


# ============================================================
# PRODUCT
# ============================================================

def product():

    return api(
        "GET",
        f"/v2/products/{SYMBOL}"
    )["result"]


# ============================================================
# POSITION
# ============================================================

def position(product_id):

    data = api(
        "GET",
        "/v2/positions",
        params={
            "product_id": int(
                product_id
            )
        },
        auth=True
    )

    result = data.get(
        "result"
    )

    if not isinstance(
        result,
        dict
    ):

        return {
            "size": 0,
            "entry": None,
            "stop_loss": 0,
            "unrealized_pnl": 0
        }

    return {
        "size": int(
            result.get(
                "size",
                0
            )
        ),
        "entry": result.get(
            "entry_price"
        ),
        "stop_loss": result.get(
            "stop_loss"
        ),
        "unrealized_pnl": result.get(
            "unrealized_pnl",
            0
        )
    }


# ============================================================
# BALANCE
# ============================================================

def balance():

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

        if asset in (
            "USD",
            "USDT"
        ):

            value = (
                wallet.get(
                    "available_balance"
                )
                or wallet.get(
                    "balance"
                )
            )

            if value is not None:

                return Decimal(
                    str(value)
                )

    raise RuntimeError(
        "USD/USDT balance not found."
    )


# ============================================================
# LEVERAGE
# ============================================================

def set_leverage(product_id):

    try:

        api(
            "POST",
            f"/v2/products/{product_id}/orders/leverage",
            body={
                "leverage": str(
                    LEVERAGE
                )
            },
            auth=True
        )

        logging.info(
            f"LEVERAGE = {LEVERAGE}x"
        )

    except Exception as e:

        logging.warning(
            f"Could not set leverage: {e}"
        )


# ============================================================
# ORDER SIZE
# ============================================================

def order_size(
    product_info,
    price
):

    bal = balance()

    margin = (
        bal
        * BALANCE_FRACTION
    )

    notional = (
        margin
        * LEVERAGE
    )

    contract_value = Decimal(
        str(
            product_info.get(
                "contract_value"
            )
            or product_info.get(
                "contract_value_usd"
            )
            or "1"
        )
    )

    if contract_value <= 0:

        contract_value = Decimal(
            "1"
        )

    raw = (
        notional
        / price
        / contract_value
    )

    increment = Decimal(
        str(
            product_info.get(
                "lot_size"
            )
            or product_info.get(
                "order_size_increment"
            )
            or "1"
        )
    )

    minimum = Decimal(
        str(
            product_info.get(
                "min_order_size"
            )
            or product_info.get(
                "minimum_order_size"
            )
            or increment
        )
    )

    size_decimal = (
        raw / increment
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * increment

    if size_decimal < minimum:

        size_decimal = minimum

    size = int(
        size_decimal
    )

    if size <= 0:

        raise RuntimeError(
            "Order size calculated as zero."
        )

    logging.info(
        f"SIZE | Balance={bal} "
        f"| Margin={margin} "
        f"| Size={size}"
    )

    return size


# ============================================================
# ENTRY
# ============================================================

def market_entry(
    product_id,
    side,
    size,
    sl
):

    body = {

        "product_id":
            int(product_id),

        "product_symbol":
            SYMBOL,

        "size":
            int(abs(size)),

        "side":
            side,

        "order_type":
            "market_order",

        "bracket_stop_loss_price":
            str(sl),

        "bracket_stop_trigger_method":
            "last_traded_price",

        "client_order_id":
            f"simple_{int(time.time()*1000)}"[-32:]
    }

    logging.warning(
        "========================================"
    )

    logging.warning(
        f"ENTRY {side.upper()}"
    )

    logging.warning(
        f"SIZE = {size}"
    )

    logging.warning(
        f"SL   = {sl}"
    )

    logging.warning(
        "========================================"
    )

    return api(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )


# ============================================================
# CLOSE
# ============================================================

def close_position(
    product_id,
    size
):

    if size == 0:

        return

    side = (
        "sell"
        if size > 0
        else "buy"
    )

    body = {

        "product_id":
            int(product_id),

        "product_symbol":
            SYMBOL,

        "size":
            abs(int(size)),

        "side":
            side,

        "order_type":
            "market_order",

        "reduce_only":
            True,

        "client_order_id":
            f"close_{int(time.time()*1000)}"[-32:]
    }

    logging.warning(
        f"CLOSING POSITION | SIZE={size}"
    )

    api(
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )


# ============================================================
# HISTORICAL HIGH / LOW
# ============================================================

def historical_high_low(
    start,
    end
):

    try:

        data = api(
            "GET",
            "/v2/history/candles",
            params={
                "resolution":
                    "1m",

                "symbol":
                    SYMBOL,

                "start":
                    int(
                        start.timestamp()
                    ),

                "end":
                    int(
                        end.timestamp()
                    )
            }
        )

        candles = data.get(
            "result",
            []
        )

        high = None
        low = None

        for candle in candles:

            try:

                h = Decimal(
                    str(
                        candle["high"]
                    )
                )

                l = Decimal(
                    str(
                        candle["low"]
                    )
                )

                if (
                    high is None
                    or h > high
                ):

                    high = h

                if (
                    low is None
                    or l < low
                ):

                    low = l

            except Exception:

                continue

        return high, low

    except Exception as e:

        logging.warning(
            f"HISTORY ERROR | {e}"
        )

        return None, None


# ============================================================
# TRADE HISTORY
# ============================================================

def load_trade_history():

    if not os.path.exists(
        TRADE_HISTORY_FILE
    ):

        return []

    try:

        with open(
            TRADE_HISTORY_FILE,
            "r"
        ) as f:

            data = json.load(f)

        if isinstance(
            data,
            list
        ):

            return data

    except Exception as e:

        logging.warning(
            f"TRADE HISTORY LOAD ERROR | {e}"
        )

    return []


def save_trade_history(
    history
):

    tmp = (
        TRADE_HISTORY_FILE
        + ".tmp"
    )

    with open(
        tmp,
        "w"
    ) as f:

        json.dump(
            history,
            f,
            indent=2
        )

    os.replace(
        tmp,
        TRADE_HISTORY_FILE
    )


def contract_value_from_product(
    product_info
):

    value = (
        product_info.get(
            "contract_value"
        )
        or product_info.get(
            "contract_value_usd"
        )
        or "1"
    )

    try:

        value = Decimal(
            str(value)
        )

        if value <= 0:

            return Decimal(
                "1"
            )

        return value

    except Exception:

        return Decimal(
            "1"
        )


def calculate_trade_pnl(
    direction,
    entry_price,
    exit_price,
    size,
    product_info
):

    try:

        entry = Decimal(
            str(entry_price)
        )

        exit_value = Decimal(
            str(exit_price)
        )

        qty = Decimal(
            str(abs(size))
        )

        contract_value = (
            contract_value_from_product(
                product_info
            )
        )

        if direction == "LONG":

            pnl = (
                exit_value
                - entry
            ) * qty * contract_value

        else:

            pnl = (
                entry
                - exit_value
            ) * qty * contract_value

        return pnl

    except Exception as e:

        logging.warning(
            f"P&L CALCULATION ERROR | {e}"
        )

        return Decimal(
            "0"
        )


def record_completed_trade(
    bot,
    direction,
    entry_price,
    exit_price,
    size,
    reason
):

    try:

        if entry_price is None:
            return

        if exit_price is None:
            return

        if not size:
            return

        entry = Decimal(
            str(entry_price)
        )

        exit_value = Decimal(
            str(exit_price)
        )

        quantity = abs(
            int(size)
        )

        pnl = calculate_trade_pnl(
            direction,
            entry,
            exit_value,
            quantity,
            bot.product
        )

        entry_time = None

        if bot.active_trade:

            entry_time = (
                bot.active_trade.get(
                    "entry_time"
                )
            )

        if not entry_time:

            entry_time = (
                now_ist().isoformat()
            )

        trade = {

            "id":
                f"trade_{int(time.time()*1000)}",

            "symbol":
                SYMBOL,

            "date":
                now_ist().strftime(
                    "%Y-%m-%d"
                ),

            "direction":
                direction,

            "entry_time":
                entry_time,

            "exit_time":
                now_ist().isoformat(),

            "entry_price":
                float(entry),

            "exit_price":
                float(exit_value),

            "size":
                quantity,

            "stop_loss":
                (
                    float(bot.sl)
                    if bot.sl is not None
                    else None
                ),

            "pnl":
                float(pnl),

            "reason":
                reason
        }

        history = load_trade_history()

        history.append(
            trade
        )

        save_trade_history(
            history
        )

        logging.warning(
            "========================================"
        )

        logging.warning(
            "TRADE HISTORY SAVED"
        )

        logging.warning(
            f"{direction} | "
            f"ENTRY={entry} | "
            f"EXIT={exit_value} | "
            f"P&L={pnl}"
        )

        logging.warning(
            f"TOTAL SAVED TRADES = {len(history)}"
        )

        logging.warning(
            "========================================"
        )

    except Exception as e:

        logging.exception(
            f"TRADE HISTORY ERROR | {e}"
        )


# ============================================================
# SIMPLE BOT
# ============================================================

class Bot:

    def __init__(
        self,
        product_info
    ):

        self.product = product_info

        self.product_id = int(
            product_info["id"]
        )

        self.day = None

        self.high = None
        self.low = None

        self.sl = None

        self.last_position = 0

        self.trade_high = None
        self.trade_low = None

        self.last_price = None

        self.ready = False

        self.lock = threading.RLock()

        self.active_trade = None

        # ----------------------------------------------------
        # IMPORTANT:
        # Bot ALWAYS starts in STOPPED state.
        # User must manually press START BOT.
        # ----------------------------------------------------

        self.bot_enabled = False

        self.stop_reason = "START REQUIRED"

        self.load_state()

        # Never automatically resume trading after process restart.
        self.bot_enabled = False

        self.stop_reason = "START REQUIRED"

        self.save()


    # ========================================================
    # STATE
    # ========================================================

    def load_state(self):

        if not os.path.exists(
            STATE_FILE
        ):

            return

        try:

            with open(
                STATE_FILE,
                "r"
            ) as f:

                s = json.load(f)

            if s.get("day"):

                self.day = (
                    datetime.fromisoformat(
                        s["day"]
                    )
                )

            if s.get("high") is not None:

                self.high = Decimal(
                    str(s["high"])
                )

            if s.get("low") is not None:

                self.low = Decimal(
                    str(s["low"])
                )

            if s.get("sl") is not None:

                self.sl = Decimal(
                    str(s["sl"])
                )

            if s.get("trade_high") is not None:

                self.trade_high = Decimal(
                    str(s["trade_high"])
                )

            if s.get("trade_low") is not None:

                self.trade_low = Decimal(
                    str(s["trade_low"])
                )

            if s.get(
                "active_trade"
            ):

                self.active_trade = (
                    s["active_trade"]
                )

            logging.info(
                f"STATE | HIGH={self.high} "
                f"| LOW={self.low} "
                f"| SL={self.sl}"
            )

        except Exception as e:

            logging.warning(
                f"STATE LOAD ERROR | {e}"
            )


    def save(self):

        data = {

            "day":
                (
                    self.day.isoformat()
                    if self.day
                    else None
                ),

            "high":
                (
                    str(self.high)
                    if self.high is not None
                    else None
                ),

            "low":
                (
                    str(self.low)
                    if self.low is not None
                    else None
                ),

            "sl":
                (
                    str(self.sl)
                    if self.sl is not None
                    else None
                ),

            "trade_high":
                (
                    str(self.trade_high)
                    if self.trade_high is not None
                    else None
                ),

            "trade_low":
                (
                    str(self.trade_low)
                    if self.trade_low is not None
                    else None
                ),

            "active_trade":
                self.active_trade,

            "bot_enabled":
                self.bot_enabled,

            "stop_reason":
                self.stop_reason
        }

        tmp = (
            STATE_FILE
            + ".tmp"
        )

        with open(
            tmp,
            "w"
        ) as f:

            json.dump(
                data,
                f,
                indent=2
            )

        os.replace(
            tmp,
            STATE_FILE
        )


    # ========================================================
    # START BOT
    # ========================================================

    def start_bot(self):

        with self.lock:

            # ------------------------------------------------
            # Check exchange first.
            # We do not allow START if an old position
            # somehow remains open after STOP.
            # ------------------------------------------------

            try:

                pos = position(
                    self.product_id
                )

            except Exception as e:

                logging.exception(
                    f"START CHECK ERROR | {e}"
                )

                return {
                    "success": False,
                    "message":
                        "Could not verify exchange position."
                }

            if pos["size"] != 0:

                self.bot_enabled = False

                self.stop_reason = (
                    "OPEN POSITION EXISTS"
                )

                self.save()

                return {
                    "success": False,
                    "message":
                        "Cannot start: an open position still exists. "
                        "Close it first."
                }

            self.last_position = 0

            self.active_trade = None
            self.sl = None
            self.trade_high = None
            self.trade_low = None

            self.bot_enabled = True
            self.stop_reason = None

            self.save()

            logging.warning(
                "========================================"
            )

            logging.warning(
                "BOT MANUALLY STARTED"
            )

            logging.warning(
                f"START TIME = {now_ist().isoformat()}"
            )

            logging.warning(
                "BOT CAN NOW TAKE NEW POSITIONS"
            )

            logging.warning(
                "========================================"
            )

            return {
                "success": True,
                "bot_enabled": True,
                "message":
                    "Bot started. It can now take new positions."
            }


    # ========================================================
    # STOP BOT
    # ========================================================

    def stop_bot(self):

        with self.lock:

            # ------------------------------------------------
            # Disable NEW entries immediately.
            # ------------------------------------------------

            self.bot_enabled = False
            self.stop_reason = "MANUAL STOP"

            self.save()

            logging.warning(
                "========================================"
            )

            logging.warning(
                "BOT MANUALLY STOPPED"
            )

            logging.warning(
                f"STOP TIME = {now_ist().isoformat()}"
            )

            # ------------------------------------------------
            # Check live exchange position.
            # ------------------------------------------------

            try:

                pos = position(
                    self.product_id
                )

            except Exception as e:

                logging.exception(
                    f"STOP POSITION CHECK ERROR | {e}"
                )

                return {
                    "success": False,
                    "bot_enabled": False,
                    "message":
                        "Bot stopped, but exchange position "
                        "could not be checked."
                }

            size = int(
                pos.get(
                    "size",
                    0
                )
            )

            # ------------------------------------------------
            # Already flat.
            # ------------------------------------------------

            if size == 0:

                self.last_position = 0
                self.sl = None
                self.active_trade = None
                self.trade_high = None
                self.trade_low = None

                self.save()

                logging.warning(
                    "NO OPEN POSITION"
                )

                logging.warning(
                    "BOT IS STOPPED"
                )

                logging.warning(
                    "========================================"
                )

                return {
                    "success": True,
                    "bot_enabled": False,
                    "position_closed": True,
                    "message":
                        "Bot stopped. No open position."
                }

            # ------------------------------------------------
            # Position exists -> close it.
            # ------------------------------------------------

            try:

                logging.warning(
                    f"STOP -> CLOSING POSITION | SIZE={size}"
                )

                close_position(
                    self.product_id,
                    size
                )

            except Exception as e:

                logging.exception(
                    f"STOP CLOSE ERROR | {e}"
                )

                logging.warning(
                    "BOT REMAINS STOPPED"
                )

                logging.warning(
                    "========================================"
                )

                return {
                    "success": False,
                    "bot_enabled": False,
                    "position_closed": False,
                    "message":
                        "Bot stopped, but closing the position failed. "
                        "Please check the exchange."
                }

            # ------------------------------------------------
            # Wait for exchange confirmation.
            # ------------------------------------------------

            closed = False
            final_position = None

            for _ in range(50):

                time.sleep(
                    0.2
                )

                try:

                    final_position = position(
                        self.product_id
                    )

                    if (
                        int(
                            final_position.get(
                                "size",
                                0
                            )
                        ) == 0
                    ):

                        closed = True
                        break

                except Exception as e:

                    logging.warning(
                        f"STOP CLOSE VERIFY ERROR | {e}"
                    )

            # ------------------------------------------------
            # Could not confirm close.
            # ------------------------------------------------

            if not closed:

                self.last_position = (
                    int(
                        final_position.get(
                            "size",
                            size
                        )
                    )
                    if final_position
                    else size
                )

                self.save()

                logging.error(
                    "STOP FAILED TO CONFIRM FLAT POSITION"
                )

                logging.warning(
                    "BOT REMAINS STOPPED"
                )

                logging.warning(
                    "========================================"
                )

                return {
                    "success": False,
                    "bot_enabled": False,
                    "position_closed": False,
                    "message":
                        "Bot stopped, but the position could not "
                        "be confirmed closed."
                }

            # ------------------------------------------------
            # Record manually closed trade.
            # ------------------------------------------------

            exit_price = (
                self.last_price
                or final_position.get("entry")
                if final_position
                else self.last_price
            )

            if exit_price is None:

                exit_price = (
                    self.active_trade.get(
                        "entry_price"
                    )
                    if self.active_trade
                    else None
                )

            self.finish_active_trade(
                exit_price,
                "MANUAL_STOP"
            )

            self.last_position = 0
            self.sl = None
            self.trade_high = None
            self.trade_low = None

            self.save()

            logging.warning(
                "POSITION CLOSED BY MANUAL STOP"
            )

            logging.warning(
                "BOT IS STOPPED"
            )

            logging.warning(
                "NO NEW POSITION CAN BE TAKEN"
            )

            logging.warning(
                "========================================"
            )

            return {
                "success": True,
                "bot_enabled": False,
                "position_closed": True,
                "message":
                    "Bot stopped and open position was closed."
            }


    # ========================================================
    # NEW DAY
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
            "========================================"
        )

        logging.warning(
            f"NEW DAY | {day}"
        )

        logging.warning(
            "========================================"
        )

        self.day = day

        self.high = None
        self.low = None
        self.sl = None

        self.trade_high = None
        self.trade_low = None

        self.ready = False

        # ----------------------------------------------------
        # IMPORTANT:
        # New day NEVER automatically enables the bot.
        # ----------------------------------------------------

        self.bot_enabled = False
        self.stop_reason = "START REQUIRED"

        self.save()


    # ========================================================
    # PREPARE
    # ========================================================

    def prepare(
        self,
        now,
        price
    ):

        start = strategy_start(
            self.day
        )

        if now < start:

            return False

        if self.ready:

            if (
                self.last_position != 0
                and self.active_trade is None
            ):

                try:

                    pos = position(
                        self.product_id
                    )

                    if pos["size"] != 0:

                        direction = (
                            "LONG"
                            if pos["size"] > 0
                            else "SHORT"
                        )

                        entry = pos.get(
                            "entry"
                        )

                        if entry is not None:

                            self.active_trade = {

                                "direction":
                                    direction,

                                "entry_price":
                                    float(entry),

                                "entry_time":
                                    now_ist().isoformat(),

                                "size":
                                    abs(
                                        int(
                                            pos["size"]
                                        )
                                    )
                            }

                            self.save()

                except Exception as e:

                    logging.warning(
                        f"ACTIVE TRADE RECOVERY ERROR | {e}"
                    )

            return True

        pos = position(
            self.product_id
        )

        self.last_position = pos[
            "size"
        ]

        if (
            self.high is not None
            and self.low is not None
        ):

            self.ready = True

            return True

        if now > start + timedelta(
            seconds=5
        ):

            high, low = historical_high_low(
                start,
                now
            )

            if (
                high is not None
                and low is not None
            ):

                self.high = high
                self.low = low

                logging.warning(
                    "RECOVERED TODAY RANGE"
                )

                logging.warning(
                    f"HIGH = {self.high}"
                )

                logging.warning(
                    f"LOW  = {self.low}"
                )

                self.ready = True

                self.save()

                return True

        self.high = price
        self.low = price

        self.ready = True

        logging.warning(
            f"INITIAL RANGE | "
            f"HIGH={price} LOW={price}"
        )

        self.save()

        return True


    # ========================================================
    # ENTER
    # ========================================================

    def enter(
        self,
        direction,
        price,
        sl
    ):

        # ----------------------------------------------------
        # HARD SAFETY:
        # No entry unless manually started.
        # ----------------------------------------------------

        if not self.bot_enabled:

            logging.info(
                f"ENTRY BLOCKED | BOT STOPPED | {direction}"
            )

            return False

        if self.last_position != 0:

            return False

        pos = position(
            self.product_id
        )

        if pos["size"] != 0:

            self.last_position = pos[
                "size"
            ]

            return False

        if direction == "LONG":

            if sl >= price:

                logging.error(
                    f"LONG BLOCKED | "
                    f"PRICE={price} SL={sl}"
                )

                return False

            side = "buy"

        else:

            if sl <= price:

                logging.error(
                    f"SHORT BLOCKED | "
                    f"PRICE={price} SL={sl}"
                )

                return False

            side = "sell"

        size = order_size(
            self.product,
            price
        )

        market_entry(
            self.product_id,
            side,
            size,
            sl
        )

        for _ in range(50):

            time.sleep(
                0.2
            )

            pos = position(
                self.product_id
            )

            if direction == "LONG":

                if pos["size"] > 0:

                    self.last_position = (
                        pos["size"]
                    )

                    break

            else:

                if pos["size"] < 0:

                    self.last_position = (
                        pos["size"]
                    )

                    break

        if self.last_position == 0:

            logging.error(
                "ENTRY NOT CONFIRMED"
            )

            return False

        self.sl = Decimal(
            str(sl)
        )

        if direction == "LONG":

            self.trade_high = price
            self.trade_low = None

        else:

            self.trade_low = price
            self.trade_high = None

        self.active_trade = {

            "direction":
                direction,

            "entry_price":
                float(price),

            "entry_time":
                now_ist().isoformat(),

            "size":
                abs(
                    int(
                        self.last_position
                    )
                )
        }

        self.save()

        logging.warning(
            f"TRADE LIVE | "
            f"{direction} | "
            f"ENTRY≈{price} | "
            f"SL={sl}"
        )

        return True


    # ========================================================
    # FINISH TRADE
    # ========================================================

    def finish_active_trade(
        self,
        exit_price,
        reason
    ):

        if not self.active_trade:

            return

        direction = (
            self.active_trade.get(
                "direction"
            )
        )

        entry_price = (
            self.active_trade.get(
                "entry_price"
            )
        )

        trade_size = (
            self.active_trade.get(
                "size",
                abs(
                    int(
                        self.last_position
                    )
                )
            )
        )

        record_completed_trade(
            self,
            direction,
            entry_price,
            exit_price,
            trade_size,
            reason
        )

        self.active_trade = None

        self.save()


    # ========================================================
    # PRICE
    # ========================================================

    def price_tick(
        self,
        price_text
    ):

        try:

            price = Decimal(
                str(price_text)
            )

        except Exception:

            return

        with self.lock:

            self.last_price = price

            now = now_ist()

            # ------------------------------------------------
            # Saturday square-off
            # ------------------------------------------------

            if saturday_squareoff(now):

                pos = position(
                    self.product_id
                )

                if pos["size"] != 0:

                    close_position(
                        self.product_id,
                        pos["size"]
                    )

                    self.finish_active_trade(
                        price,
                        "SATURDAY_SQUAREOFF"
                    )

                    self.last_position = 0

                    self.sl = None

                return

            # ------------------------------------------------
            # Weekend
            # ------------------------------------------------

            if weekend(now):

                return

            # ------------------------------------------------
            # Day
            # ------------------------------------------------

            self.new_day(
                now
            )

            # ------------------------------------------------
            # BEFORE 05:45
            #
            # We intentionally allow preparation/range
            # recovery to happen while STOPPED.
            #
            # But NO ENTRY can happen until START BOT.
            # ------------------------------------------------

            if now < strategy_start(
                self.day
            ):

                return

            # ------------------------------------------------
            # Prepare
            # ------------------------------------------------

            if not self.prepare(
                now,
                price
            ):

                return

            # ------------------------------------------------
            # Current exchange position
            # ------------------------------------------------

            pos = position(
                self.product_id
            )

            size = pos[
                "size"
            ]

            # ------------------------------------------------
            # POSITION CLOSED
            # ------------------------------------------------

            if (
                size == 0
                and self.last_position != 0
            ):

                old = self.last_position

                # --------------------------------------------
                # STOP LOSS
                #
                # Only reverse if BOT IS RUNNING.
                #
                # If bot is stopped, it must NEVER open
                # another position.
                # --------------------------------------------

                if (
                    self.bot_enabled
                    and self.sl is not None
                    and (
                        (
                            old > 0
                            and price <= self.sl
                        )
                        or
                        (
                            old < 0
                            and price >= self.sl
                        )
                    )
                ):

                    if old > 0:

                        peak = (
                            self.trade_high
                            or self.high
                        )

                        logging.warning(
                            "LONG SL -> SHORT"
                        )

                        self.finish_active_trade(
                            price,
                            "STOP_LOSS"
                        )

                        self.last_position = 0
                        self.sl = None

                        self.enter(
                            "SHORT",
                            price,
                            peak
                        )

                    else:

                        trough = (
                            self.trade_low
                            or self.low
                        )

                        logging.warning(
                            "SHORT SL -> LONG"
                        )

                        self.finish_active_trade(
                            price,
                            "STOP_LOSS"
                        )

                        self.last_position = 0
                        self.sl = None

                        self.enter(
                            "LONG",
                            price,
                            trough
                        )

                    return

                # --------------------------------------------
                # EXTERNAL CLOSE / MANUAL CLOSE
                # --------------------------------------------

                self.finish_active_trade(
                    price,
                    "EXTERNAL_CLOSE"
                )

                self.last_position = 0
                self.sl = None

                return

            # ------------------------------------------------
            # LONG
            # ------------------------------------------------

            if size > 0:

                self.last_position = size

                if (
                    self.trade_high is None
                    or price > self.trade_high
                ):

                    self.trade_high = price

                    if self.high is not None:

                        self.high = max(
                            self.high,
                            price
                        )

                    else:

                        self.high = price

                    self.save()

                return

            # ------------------------------------------------
            # SHORT
            # ------------------------------------------------

            if size < 0:

                self.last_position = size

                if (
                    self.trade_low is None
                    or price < self.trade_low
                ):

                    self.trade_low = price

                    if self.low is not None:

                        self.low = min(
                            self.low,
                            price
                        )

                    else:

                        self.low = price

                    self.save()

                return

            # ------------------------------------------------
            # FLAT
            # ------------------------------------------------

            self.last_position = 0

            # ------------------------------------------------
            # CRITICAL:
            # If BOT STOPPED, do not take ANY new position.
            # ------------------------------------------------

            if not self.bot_enabled:

                return

            # ------------------------------------------------
            # NEW HIGH
            # ------------------------------------------------

            if price > self.high:

                old_high = self.high
                sl = self.low

                logging.warning(
                    f"NEW HIGH | "
                    f"{old_high} -> {price}"
                )

                if self.enter(
                    "LONG",
                    price,
                    sl
                ):

                    self.high = price

                    self.save()

                return

            # ------------------------------------------------
            # NEW LOW
            # ------------------------------------------------

            if price < self.low:

                old_low = self.low
                sl = self.high

                logging.warning(
                    f"NEW LOW | "
                    f"{old_low} -> {price}"
                )

                if self.enter(
                    "SHORT",
                    price,
                    sl
                ):

                    self.low = price

                    self.save()

                return


# ============================================================
# DASHBOARD
# ============================================================

BOT_INSTANCE = None


def decimal_json(value):

    if value is None:

        return None

    if isinstance(
        value,
        Decimal
    ):

        return float(value)

    try:

        return float(value)

    except Exception:

        return value


def history_statistics(
    history
):

    today = now_ist().strftime(
        "%Y-%m-%d"
    )

    today_trades = [
        trade
        for trade in history
        if trade.get("date") == today
    ]

    all_time_count = len(
        history
    )

    all_time_winning = sum(
        1
        for trade in history
        if float(
            trade.get(
                "pnl",
                0
            )
        ) > 0
    )

    all_time_losing = sum(
        1
        for trade in history
        if float(
            trade.get(
                "pnl",
                0
            )
        ) < 0
    )

    all_time_pnl = sum(
        float(
            trade.get(
                "pnl",
                0
            )
        )
        for trade in history
    )

    all_time_win_rate = (
        (
            all_time_winning
            / all_time_count
            * 100
        )
        if all_time_count > 0
        else 0
    )

    today_count = len(
        today_trades
    )

    today_winning = sum(
        1
        for trade in today_trades
        if float(
            trade.get(
                "pnl",
                0
            )
        ) > 0
    )

    today_losing = sum(
        1
        for trade in today_trades
        if float(
            trade.get(
                "pnl",
                0
            )
        ) < 0
    )

    today_pnl = sum(
        float(
            trade.get(
                "pnl",
                0
            )
        )
        for trade in today_trades
    )

    today_win_rate = (
        (
            today_winning
            / today_count
            * 100
        )
        if today_count > 0
        else 0
    )

    return {

        "today": {

            "total_trades":
                today_count,

            "winning_trades":
                today_winning,

            "losing_trades":
                today_losing,

            "win_rate":
                round(
                    today_win_rate,
                    1
                ),

            "pnl":
                round(
                    today_pnl,
                    2
                )
        },

        "all_time": {

            "total_trades":
                all_time_count,

            "winning_trades":
                all_time_winning,

            "losing_trades":
                all_time_losing,

            "win_rate":
                round(
                    all_time_win_rate,
                    1
                ),

            "pnl":
                round(
                    all_time_pnl,
                    2
                )
        }
    }


def dashboard_data():

    bot = BOT_INSTANCE

    if bot is None:

        return {

            "success":
                True,

            "bot_running":
                False,

            "bot_enabled":
                False,

            "message":
                "Bot is starting..."
        }

    with bot.lock:

        try:

            live_position = position(
                bot.product_id
            )

        except Exception as e:

            logging.warning(
                f"DASHBOARD POSITION ERROR | {e}"
            )

            live_position = {

                "size":
                    0,

                "entry":
                    None,

                "stop_loss":
                    None,

                "unrealized_pnl":
                    0
            }

        try:

            live_balance = balance()

        except Exception as e:

            logging.warning(
                f"DASHBOARD BALANCE ERROR | {e}"
            )

            live_balance = None

        size = int(
            live_position.get(
                "size",
                0
            )
        )

        if size > 0:

            direction = "LONG"

        elif size < 0:

            direction = "SHORT"

        else:

            direction = "FLAT"

        history = load_trade_history()

        # Newest first.
        history_for_dashboard = list(
            reversed(history)
        )

        statistics = history_statistics(
            history
        )

        return {

            "success":
                True,

            # Process/server status.
            "bot_running":
                True,

            # Trading status.
            "bot_enabled":
                bot.bot_enabled,

            "bot_status":
                (
                    "RUNNING"
                    if bot.bot_enabled
                    else "STOPPED"
                ),

            "stop_reason":
                bot.stop_reason,

            "symbol":
                SYMBOL,

            "current_price":
                decimal_json(
                    bot.last_price
                ),

            "high":
                decimal_json(
                    bot.high
                ),

            "low":
                decimal_json(
                    bot.low
                ),

            "stop_loss":
                decimal_json(
                    bot.sl
                ),

            "balance":
                decimal_json(
                    live_balance
                ),

            "position": {

                "direction":
                    direction,

                "size":
                    abs(size),

                "entry_price":
                    decimal_json(
                        live_position.get(
                            "entry"
                        )
                    ),

                "stop_loss":
                    decimal_json(
                        live_position.get(
                            "stop_loss"
                        )
                        or bot.sl
                    ),

                "unrealized_pnl":
                    decimal_json(
                        live_position.get(
                            "unrealized_pnl",
                            0
                        )
                    )
            },

            "statistics":
                statistics,

            "trade_history":
                history_for_dashboard,

            "history_count":
                len(history),

            "session": {

                "day":
                    (
                        bot.day.isoformat()
                        if bot.day
                        else None
                    ),

                "strategy_start":
                    (
                        strategy_start(
                            bot.day
                        ).isoformat()
                        if bot.day
                        else None
                    ),

                "ready":
                    bot.ready
            }
        }


# ============================================================
# DASHBOARD HTTP HANDLER
# ============================================================

class DashboardHandler(
    SimpleHTTPRequestHandler
):

    def __init__(
        self,
        *args,
        **kwargs
    ):

        super().__init__(
            *args,
            directory=BASE_DIR,
            **kwargs
        )


    def do_GET(self):

        if self.path == "/api/health":

            bot = BOT_INSTANCE

            self.send_json({

                "success":
                    True,

                "bot_running":
                    bot is not None,

                "bot_enabled":
                    (
                        bot.bot_enabled
                        if bot
                        else False
                    ),

                "symbol":
                    SYMBOL

            })

            return

        if self.path == "/api/dashboard":

            self.send_json(
                dashboard_data()
            )

            return

        if self.path == "/":

            self.path = "/index.html"

        return super().do_GET()


    def do_POST(self):

        # ----------------------------------------------------
        # START BOT
        # ----------------------------------------------------

        if self.path == "/api/bot/start":

            bot = BOT_INSTANCE

            if bot is None:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Bot is not ready yet."
                    },
                    status=503
                )

                return

            try:

                result = bot.start_bot()

                self.send_json(
                    result,
                    status=(
                        200
                        if result.get("success")
                        else 409
                    )
                )

            except Exception as e:

                logging.exception(
                    f"START API ERROR | {e}"
                )

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Failed to start bot."
                    },
                    status=500
                )

            return

        # ----------------------------------------------------
        # STOP BOT
        # ----------------------------------------------------

        if self.path == "/api/bot/stop":

            bot = BOT_INSTANCE

            if bot is None:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Bot is not ready yet."
                    },
                    status=503
                )

                return

            try:

                result = bot.stop_bot()

                self.send_json(
                    result,
                    status=(
                        200
                        if result.get("success")
                        else 500
                    )
                )

            except Exception as e:

                logging.exception(
                    f"STOP API ERROR | {e}"
                )

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Failed to stop bot."
                    },
                    status=500
                )

            return

        self.send_json(
            {
                "success": False,
                "message":
                    "Unknown endpoint."
            },
            status=404
        )


    def send_json(
        self,
        data,
        status=200
    ):

        raw = json.dumps(
            data,
            separators=(",", ":")
        ).encode(
            "utf-8"
        )

        self.send_response(
            status
        )

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(raw))
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.end_headers()

        self.wfile.write(
            raw
        )


    def log_message(
        self,
        format,
        *args
    ):

        return


def start_dashboard():

    def server_thread():

        try:

            server = ThreadingHTTPServer(
                (
                    "127.0.0.1",
                    DASHBOARD_PORT
                ),
                DashboardHandler
            )

            logging.warning(
                "========================================"
            )

            logging.warning(
                "DASHBOARD STARTED"
            )

            logging.warning(
                f"PORT = {DASHBOARD_PORT}"
            )

            logging.warning(
                "START/STOP CONTROL = ENABLED"
            )

            logging.warning(
                "========================================"
            )

            server.serve_forever()

        except Exception as e:

            logging.exception(
                f"DASHBOARD SERVER ERROR | {e}"
            )

    thread = threading.Thread(
        target=server_thread,
        daemon=True,
        name="dashboard-server"
    )

    thread.start()


# ============================================================
# WEBSOCKET
# ============================================================

def run_websocket(bot):

    while True:

        try:

            logging.warning(
                f"CONNECTING WS | {WS_URL}"
            )

            def on_open(ws):

                payload = {

                    "type":
                        "subscribe",

                    "payload": {

                        "channels": [

                            {

                                "name":
                                    "trades",

                                "symbols": [
                                    SYMBOL
                                ]

                            }

                        ]

                    }

                }

                ws.send(
                    json.dumps(
                        payload
                    )
                )

                logging.warning(
                    f"TRADES SUBSCRIBED | {SYMBOL}"
                )


            def on_message(
                ws,
                message
            ):

                try:

                    data = json.loads(
                        message
                    )

                    if data.get(
                        "type"
                    ) == "trades":

                        symbol = (
                            data.get("sy")
                            or data.get("symbol")
                        )

                        price = data.get(
                            "p"
                        )

                        if (
                            symbol == SYMBOL
                            and price is not None
                        ):

                            bot.price_tick(
                                price
                            )

                except Exception as e:

                    logging.error(
                        f"WS MESSAGE ERROR | {e}"
                    )


            def on_error(
                ws,
                error
            ):

                logging.error(
                    f"WS ERROR | {error}"
                )


            def on_close(
                ws,
                code,
                msg
            ):

                logging.warning(
                    f"WS CLOSED | {code} | {msg}"
                )


            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )

            ws.run_forever(
                ping_interval=30,
                ping_timeout=10
            )

        except Exception as e:

            logging.exception(
                f"WS CRASH | {e}"
            )

        logging.warning(
            f"RECONNECTING IN {RECONNECT_SECONDS}s"
        )

        time.sleep(
            RECONNECT_SECONDS
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    logging.warning(
        "============================================"
    )

    logging.warning(
        "XAUTUSD SIMPLE NEW HIGH / NEW LOW BOT"
    )

    logging.warning(
        f"SYMBOL = {SYMBOL}"
    )

    logging.warning(
        f"LEVERAGE = {LEVERAGE}x"
    )

    logging.warning(
        f"BALANCE = {BALANCE_FRACTION * 100}%"
    )

    logging.warning(
        "START = MANUAL FROM DASHBOARD"
    )

    logging.warning(
        "FEED = TRADES WEBSOCKET"
    )

    logging.warning(
        "DASHBOARD = SAME BOT PROCESS"
    )

    logging.warning(
        f"DASHBOARD PORT = {DASHBOARD_PORT}"
    )

    logging.warning(
        "TRADE HISTORY = ALL-TIME ENABLED"
    )

    logging.warning(
        f"TRADE HISTORY FILE = {TRADE_HISTORY_FILE}"
    )

    logging.warning(
        "BOT STARTUP STATE = STOPPED"
    )

    logging.warning(
        "============================================"
    )

    try:

        product_info = product()

        set_leverage(
            int(
                product_info["id"]
            )
        )

        bot = Bot(
            product_info
        )

        BOT_INSTANCE = bot

        start_dashboard()

        run_websocket(
            bot
        )

    except KeyboardInterrupt:

        logging.warning(
            "BOT STOPPED"
        )

    except Exception as e:

        logging.exception(
            f"FATAL ERROR | {e}"
    )
