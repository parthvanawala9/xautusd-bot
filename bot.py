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
# XAUTUSD MULTI ACCOUNT BOT
#
# COMPATIBLE WITH EXISTING app.js
#
# FEATURES
# ------------------------------------------------------------
# PRIMARY ACCOUNT
# CLIENT ACCOUNTS
# START / STOP BOT
# EXISTING POSITION RECOVERY
# ONLINE / OFFLINE
# BALANCE
# PRICE
# POSITION
# ENTRY
# STOP LOSS
# UNREALIZED PNL
# TODAY HISTORY
# ALL-TIME HISTORY
# CLIENT SUBSCRIPTION
# ADD CLIENT
# EDIT SUBSCRIPTION
# DELETE CLIENT
#
# TRADING RULES
# ------------------------------------------------------------
# 05:45 IST = strategy starts
#
# START BOT:
#   No position:
#       normal trading
#
#   Existing position:
#       recover position
#       DO NOT CLOSE IT
#
# STOP BOT:
#   disables new entries
#   closes existing position
#   saves MANUAL_STOP trade
#
# FLAT:
#   price > HIGH -> LONG
#   price < LOW  -> SHORT
#
# LONG:
#   new HIGH -> update HIGH
#
# SHORT:
#   new LOW -> update LOW
#
# LONG SL:
#   session LOW at entry
#
# SHORT SL:
#   session HIGH at entry
#
# SL HIT:
#   LONG -> SHORT
#   SHORT -> LONG
#
# SATURDAY 05:00 IST:
#   close open position
#
# IMPORTANT:
# Existing live exchange positions are NEVER closed
# during process startup.
#
# They are only recovered when START BOT is pressed.
# ============================================================


load_dotenv()


# ============================================================
# GLOBAL CONFIG
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

PRIMARY_ACCOUNT_ID = os.getenv(
    "PRIMARY_ACCOUNT_ID",
    "primary"
).strip()

PRIMARY_ACCOUNT_NAME = os.getenv(
    "ACCOUNT_NAME",
    "Primary Account"
).strip()

PRIMARY_API_KEY = os.getenv(
    "DELTA_API_KEY",
    ""
).strip()

PRIMARY_API_SECRET = os.getenv(
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

DASHBOARD_PORT = int(
    os.getenv(
        "DASHBOARD_PORT",
        "8000"
    )
)

RECONNECT_SECONDS = 3

POSITION_CACHE_SECONDS = float(
    os.getenv(
        "POSITION_CACHE_SECONDS",
        "1.0"
    )
)

BALANCE_CACHE_SECONDS = float(
    os.getenv(
        "BALANCE_CACHE_SECONDS",
        "3.0"
    )
)

CLIENTS_FILE = os.getenv(
    "CLIENTS_FILE",
    os.path.join(
        BASE_DIR,
        "clients.json"
    )
)

PRIMARY_STATE_FILE = os.getenv(
    "STATE_FILE",
    os.path.join(
        BASE_DIR,
        "xautusd_state.json"
    )
)

PRIMARY_HISTORY_FILE = os.getenv(
    "TRADE_HISTORY_FILE",
    os.path.join(
        BASE_DIR,
        "trade_history.json"
    )
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
    "User-Agent": "XAUTUSD-MultiAccount-Bot/3.0"
})


# ============================================================
# FILE HELPERS
# ============================================================

def safe_account_filename(
    account_id
):

    value = str(
        account_id
    )

    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789_-"
    )

    value = "".join(
        c
        for c in value
        if c in allowed
    )

    return value or "account"


def account_state_file(
    account_id,
    primary=False
):

    if primary:

        return PRIMARY_STATE_FILE

    return os.path.join(
        BASE_DIR,
        f"state_{safe_account_filename(account_id)}.json"
    )


def account_history_file(
    account_id,
    primary=False
):

    if primary:

        return PRIMARY_HISTORY_FILE

    return os.path.join(
        BASE_DIR,
        f"history_{safe_account_filename(account_id)}.json"
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
        microsecond=0
    )

    if dt < boundary:

        boundary -= timedelta(
            days=1
        )

    return boundary


def strategy_start(
    day_start
):

    return day_start.replace(
        hour=5,
        minute=45,
        second=0,
        microsecond=0
    )


def weekend(
    dt=None
):

    dt = dt or now_ist()

    if dt.weekday() == 5:

        return dt.hour >= 5

    if dt.weekday() == 6:

        return True

    return False


def saturday_squareoff(
    dt=None
):

    dt = dt or now_ist()

    return (
        dt.weekday() == 5
        and dt.hour == 5
    )


# ============================================================
# DELTA API SIGNING
# ============================================================

def make_signature(
    api_key,
    api_secret,
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
        api_secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

    return {
        "api-key": api_key,
        "signature": signature,
        "timestamp": timestamp
    }


# ============================================================
# ACCOUNT API
# ============================================================

def account_api(
    account,
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

        headers = make_signature(
            account.api_key,
            account.api_secret,
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

    if data.get(
        "success"
    ) is False:

        raise RuntimeError(
            f"Delta error: {data}"
        )

    account.last_api_ok_time = (
        now_ist().isoformat()
    )

    account.api_error = None

    return data


# ============================================================
# ACCOUNT CLASS
# ============================================================

class Account:

    def __init__(
        self,
        account_id,
        account_name,
        api_key,
        api_secret,
        account_type="client",
        subscription=None,
        primary=False
    ):

        self.account_id = str(
            account_id
        )

        self.account_name = (
            account_name
            or self.account_id
        )

        self.api_key = (
            api_key
            or ""
        ).strip()

        self.api_secret = (
            api_secret
            or ""
        ).strip()

        self.account_type = (
            account_type
        )

        self.primary = (
            primary
            or account_type == "primary"
        )

        self.subscription = (
            subscription
            or {}
        )

        self.lock = threading.RLock()

        self.product = None
        self.product_id = None

        self.bot = None

        self.last_api_ok_time = None
        self.api_error = None

        self.websocket_connected = False
        self.last_ws_message_time = None

    # ========================================================
    # SUBSCRIPTION
    # ========================================================

    def subscription_info(
        self
    ):

        if self.primary:

            return {
                "active": True,
                "expired": False,
                "start": None,
                "expiry": None,
                "fee": 0
            }

        subscription = (
            self.subscription
            or {}
        )

        start = subscription.get(
            "subscription_start"
        )

        expiry = subscription.get(
            "subscription_expiry"
        )

        fee = subscription.get(
            "subscription_fee",
            0
        )

        active = False
        expired = False

        try:

            if expiry:

                expiry_dt = datetime.fromisoformat(
                    str(expiry).replace(
                        "Z",
                        "+00:00"
                    )
                )

                if expiry_dt.tzinfo is None:

                    expiry_dt = expiry_dt.replace(
                        tzinfo=IST
                    )

                active = (
                    now_ist()
                    <= expiry_dt.astimezone(IST)
                )

                expired = not active

        except Exception:

            active = False

        return {

            "active":
                active,

            "expired":
                expired,

            "start":
                start,

            "expiry":
                expiry,

            "fee":
                float(fee or 0)
        }

    # ========================================================
    # PRODUCT
    # ========================================================

    def load_product(
        self
    ):

        data = account_api(
            self,
            "GET",
            f"/v2/products/{SYMBOL}"
        )

        self.product = data[
            "result"
        ]

        self.product_id = int(
            self.product["id"]
        )

        return self.product

    # ========================================================
    # LEVERAGE
    # ========================================================

    def set_leverage(
        self
    ):

        if not self.product_id:

            return

        try:

            account_api(
                self,
                "POST",
                f"/v2/products/{self.product_id}/orders/leverage",
                body={
                    "leverage":
                        str(LEVERAGE)
                },
                auth=True
            )

            logging.info(
                f"[{self.account_name}] "
                f"LEVERAGE = {LEVERAGE}x"
            )

        except Exception as e:

            logging.warning(
                f"[{self.account_name}] "
                f"Could not set leverage: {e}"
            )

    # ========================================================
    # CREATE BOT
    # ========================================================

    def create_bot(
        self
    ):

        if not self.product:

            self.load_product()

        self.bot = Bot(
            account=self,
            product_info=self.product,
            state_file=account_state_file(
                self.account_id,
                self.primary
            ),
            history_file=account_history_file(
                self.account_id,
                self.primary
            )
        )

        return self.bot


# ============================================================
# POSITION
# ============================================================

def get_position(
    account,
    product_id
):

    data = account_api(
        account,
        "GET",
        "/v2/positions",
        params={
            "product_id":
                int(product_id)
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
            "stop_loss": None,
            "unrealized_pnl": 0
        }

    raw_size = (
        result.get(
            "size",
            0
        )
        or 0
    )

    try:

        size = int(
            float(
                raw_size
            )
        )

    except Exception:

        size = 0

    return {

        "size":
            size,

        "entry":
            result.get(
                "entry_price"
            ),

        "stop_loss":
            result.get(
                "stop_loss"
            ),

        "unrealized_pnl":
            result.get(
                "unrealized_pnl"
            )
    }


# ============================================================
# BALANCE
# ============================================================

def get_balance(
    account
):

    data = account_api(
        account,
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

            value = wallet.get(
                "available_balance"
            )

            if value is None:

                value = wallet.get(
                    "balance"
                )

            if value is not None:

                return Decimal(
                    str(value)
                )

    raise RuntimeError(
        "USD/USDT balance not found."
    )


# ============================================================
# ORDER SIZE
# ============================================================

def calculate_order_size(
    account,
    product_info,
    price
):

    bal = get_balance(
        account
    )

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
        f"[{account.account_name}] "
        f"SIZE | Balance={bal} "
        f"| Margin={margin} "
        f"| Notional={notional} "
        f"| Size={size}"
    )

    return size


# ============================================================
# MARKET ENTRY
# ============================================================

def place_market_entry(
    account,
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
            (
                f"entry_"
                f"{safe_account_filename(account.account_id)}_"
                f"{int(time.time()*1000)}"
            )[-32:]
    }

    logging.warning(
        "========================================"
    )

    logging.warning(
        f"[{account.account_name}] "
        f"ENTRY {side.upper()}"
    )

    logging.warning(
        f"SIZE = {size}"
    )

    logging.warning(
        f"SL = {sl}"
    )

    logging.warning(
        "========================================"
    )

    return account_api(
        account,
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )


# ============================================================
# CLOSE POSITION
# ============================================================

def close_exchange_position(
    account,
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
            (
                f"close_"
                f"{safe_account_filename(account.account_id)}_"
                f"{int(time.time()*1000)}"
            )[-32:]
    }

    logging.warning(
        f"[{account.account_name}] "
        f"CLOSING POSITION | SIZE={size}"
    )

    return account_api(
        account,
        "POST",
        "/v2/orders",
        body=body,
        auth=True
    )


# ============================================================
# HISTORICAL HIGH / LOW
# ============================================================

def historical_high_low(
    account,
    start,
    end
):

    try:

        data = account_api(
            account,
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
            f"[{account.account_name}] "
            f"HISTORY ERROR | {e}"
        )

        return None, None


# ============================================================
# HISTORY
# ============================================================

def load_history(
    file_path
):

    if not os.path.exists(
        file_path
    ):

        return []

    try:

        with open(
            file_path,
            "r"
        ) as f:

            data = json.load(
                f
            )

        if isinstance(
            data,
            list
        ):

            return data

    except Exception as e:

        logging.warning(
            f"HISTORY LOAD ERROR | {e}"
        )

    return []


def save_history(
    file_path,
    history
):

    tmp = (
        file_path
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
        file_path
    )


# ============================================================
# PNL
# ============================================================

def contract_value(
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


def calculate_pnl(
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

        cv = contract_value(
            product_info
        )

        if direction == "LONG":

            return (
                exit_value
                - entry
            ) * qty * cv

        return (
            entry
            - exit_value
        ) * qty * cv

    except Exception:

        return Decimal(
            "0"
        )


# ============================================================
# BOT
# ============================================================

class Bot:

    def __init__(
        self,
        account,
        product_info,
        state_file,
        history_file
    ):

        self.account = account

        self.product = product_info

        self.product_id = int(
            product_info["id"]
        )

        self.state_file = (
            state_file
        )

        self.history_file = (
            history_file
        )

        self.day = None

        self.high = None
        self.low = None

        self.sl = None

        self.trade_high = None
        self.trade_low = None

        self.last_position = 0

        self.last_price = None

        self.ready = False

        self.active_trade = None

        self.bot_enabled = False

        self.stop_reason = (
            "START REQUIRED"
        )

        self.cached_position = {
            "size": 0,
            "entry": None,
            "stop_loss": None,
            "unrealized_pnl": 0
        }

        self.position_cache_time = 0

        self.cached_balance = None

        self.balance_cache_time = 0

        self.lock = threading.RLock()

        self.load_state()

        # ----------------------------------------------------
        # NEVER automatically resume after restart.
        # Existing position remains on exchange.
        # ----------------------------------------------------

        self.bot_enabled = False

        self.stop_reason = (
            "START REQUIRED"
        )

        self.save()


    # ========================================================
    # STATE LOAD
    # ========================================================

    def load_state(
        self
    ):

        if not os.path.exists(
            self.state_file
        ):

            return

        try:

            with open(
                self.state_file,
                "r"
            ) as f:

                state = json.load(
                    f
                )

            if state.get(
                "day"
            ):

                self.day = datetime.fromisoformat(
                    state["day"]
                )

            if state.get(
                "high"
            ) is not None:

                self.high = Decimal(
                    str(
                        state["high"]
                    )
                )

            if state.get(
                "low"
            ) is not None:

                self.low = Decimal(
                    str(
                        state["low"]
                    )
                )

            if state.get(
                "sl"
            ) is not None:

                self.sl = Decimal(
                    str(
                        state["sl"]
                    )
                )

            if state.get(
                "trade_high"
            ) is not None:

                self.trade_high = Decimal(
                    str(
                        state["trade_high"]
                    )
                )

            if state.get(
                "trade_low"
            ) is not None:

                self.trade_low = Decimal(
                    str(
                        state["trade_low"]
                    )
                )

            if state.get(
                "active_trade"
            ):

                self.active_trade = (
                    state["active_trade"]
                )

        except Exception as e:

            logging.warning(
                f"[{self.account.account_name}] "
                f"STATE LOAD ERROR | {e}"
            )


    # ========================================================
    # STATE SAVE
    # ========================================================

    def save(
        self
    ):

        data = {

            "account_id":
                self.account.account_id,

            "account_name":
                self.account.account_name,

            "symbol":
                SYMBOL,

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
            self.state_file
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
            self.state_file
        )


    # ========================================================
    # POSITION CACHE
    # ========================================================

    def refresh_position(
        self,
        force=False
    ):

        current = time.time()

        if (
            not force
            and (
                current
                - self.position_cache_time
            )
            < POSITION_CACHE_SECONDS
        ):

            return self.cached_position

        try:

            pos = get_position(
                self.account,
                self.product_id
            )

            self.cached_position = pos

            self.position_cache_time = (
                current
            )

            self.account.last_api_ok_time = (
                now_ist().isoformat()
            )

            self.account.api_error = None

            return pos

        except Exception as e:

            self.account.api_error = (
                str(e)
            )

            logging.warning(
                f"[{self.account.account_name}] "
                f"POSITION ERROR | {e}"
            )

            return self.cached_position


    # ========================================================
    # BALANCE CACHE
    # ========================================================

    def refresh_balance(
        self,
        force=False
    ):

        current = time.time()

        if (
            not force
            and self.cached_balance is not None
            and (
                current
                - self.balance_cache_time
            )
            < BALANCE_CACHE_SECONDS
        ):

            return self.cached_balance

        try:

            value = get_balance(
                self.account
            )

            self.cached_balance = value

            self.balance_cache_time = (
                current
            )

            self.account.last_api_ok_time = (
                now_ist().isoformat()
            )

            self.account.api_error = None

            return value

        except Exception as e:

            self.account.api_error = (
                str(e)
            )

            logging.warning(
                f"[{self.account.account_name}] "
                f"BALANCE ERROR | {e}"
            )

            return self.cached_balance


    # ========================================================
    # START BOT
    # ========================================================

    def start_bot(
        self
    ):

        with self.lock:

            # ------------------------------------------------
            # Client subscription check.
            # ------------------------------------------------

            subscription = (
                self.account.subscription_info()
            )

            if (
                not self.account.primary
                and not subscription["active"]
            ):

                return {

                    "success":
                        False,

                    "bot_enabled":
                        False,

                    "message":
                        "Client subscription is inactive or expired."
                }

            logging.warning(
                "========================================"
            )

            logging.warning(
                f"[{self.account.account_name}] "
                "START BOT REQUEST"
            )

            # ------------------------------------------------
            # Force live exchange position read.
            # ------------------------------------------------

            try:

                pos = self.refresh_position(
                    force=True
                )

            except Exception as e:

                logging.exception(
                    f"[{self.account.account_name}] "
                    f"START POSITION ERROR | {e}"
                )

                return {

                    "success":
                        False,

                    "message":
                        "Could not verify exchange position."
                }

            size = int(
                pos.get(
                    "size",
                    0
                )
            )

            # =================================================
            # EXISTING POSITION
            # =================================================

            if size != 0:

                direction = (
                    "LONG"
                    if size > 0
                    else "SHORT"
                )

                exchange_entry = (
                    pos.get(
                        "entry"
                    )
                )

                recovered_entry = None

                if exchange_entry is not None:

                    try:

                        recovered_entry = Decimal(
                            str(
                                exchange_entry
                            )
                        )

                    except Exception:

                        pass

                if recovered_entry is None:

                    if self.active_trade:

                        saved_entry = (
                            self.active_trade.get(
                                "entry_price"
                            )
                        )

                        if saved_entry is not None:

                            try:

                                recovered_entry = Decimal(
                                    str(
                                        saved_entry
                                    )
                                )

                            except Exception:

                                pass

                if (
                    recovered_entry is None
                    and self.last_price is not None
                ):

                    recovered_entry = (
                        self.last_price
                    )

                # ------------------------------------------------
                # Recover SL.
                # ------------------------------------------------

                recovered_sl = None

                exchange_sl = (
                    pos.get(
                        "stop_loss"
                    )
                )

                if exchange_sl is not None:

                    try:

                        recovered_sl = Decimal(
                            str(
                                exchange_sl
                            )
                        )

                    except Exception:

                        pass

                if recovered_sl is None:

                    recovered_sl = self.sl

                if recovered_sl is None:

                    if direction == "LONG":

                        recovered_sl = self.low

                    else:

                        recovered_sl = self.high

                # ------------------------------------------------
                # Preserve existing active trade.
                # ------------------------------------------------

                if self.active_trade is None:

                    self.active_trade = {

                        "direction":
                            direction,

                        "entry_price":
                            (
                                float(
                                    recovered_entry
                                )
                                if recovered_entry
                                is not None
                                else None
                            ),

                        "entry_time":
                            now_ist().isoformat(),

                        "size":
                            abs(size)
                    }

                else:

                    self.active_trade[
                        "direction"
                    ] = direction

                    self.active_trade[
                        "size"
                    ] = abs(size)

                    if (
                        self.active_trade.get(
                            "entry_price"
                        ) is None
                        and recovered_entry
                        is not None
                    ):

                        self.active_trade[
                            "entry_price"
                        ] = float(
                            recovered_entry
                        )

                if recovered_sl is not None:

                    self.sl = recovered_sl

                # ------------------------------------------------
                # Recover trade extreme.
                # ------------------------------------------------

                if direction == "LONG":

                    if self.trade_high is None:

                        self.trade_high = (
                            self.high
                            or recovered_entry
                        )

                    self.trade_low = None

                else:

                    if self.trade_low is None:

                        self.trade_low = (
                            self.low
                            or recovered_entry
                        )

                    self.trade_high = None

                # ------------------------------------------------
                # CRITICAL:
                #
                # NO CLOSE.
                #
                # Existing position stays LIVE.
                # ------------------------------------------------

                self.last_position = size

                self.bot_enabled = True

                self.stop_reason = None

                self.save()

                logging.warning(
                    f"[{self.account.account_name}] "
                    f"EXISTING POSITION RECOVERED"
                )

                logging.warning(
                    f"DIRECTION={direction}"
                )

                logging.warning(
                    f"SIZE={size}"
                )

                logging.warning(
                    f"ENTRY={recovered_entry}"
                )

                logging.warning(
                    f"SL={self.sl}"
                )

                logging.warning(
                    "POSITION WAS NOT CLOSED"
                )

                return {

                    "success":
                        True,

                    "bot_enabled":
                        True,

                    "position_recovered":
                        True,

                    "direction":
                        direction,

                    "size":
                        abs(size),

                    "entry":
                        (
                            float(
                                recovered_entry
                            )
                            if recovered_entry
                            is not None
                            else None
                        ),

                    "stop_loss":
                        (
                            float(self.sl)
                            if self.sl is not None
                            else None
                        ),

                    "message":
                        (
                            "Bot started with existing "
                            f"{direction} position. "
                            "Position was NOT closed."
                        )
                }

            # =================================================
            # FLAT
            # =================================================

            self.last_position = 0

            self.active_trade = None

            self.sl = None

            self.trade_high = None

            self.trade_low = None

            self.bot_enabled = True

            self.stop_reason = None

            self.save()

            logging.warning(
                f"[{self.account.account_name}] "
                "BOT MANUALLY STARTED"
            )

            return {

                "success":
                    True,

                "bot_enabled":
                    True,

                "position_recovered":
                    False,

                "message":
                    "Bot started. It can now take new positions."
            }


    # ========================================================
    # STOP BOT
    # ========================================================

    def stop_bot(
        self
    ):

        with self.lock:

            self.bot_enabled = False

            self.stop_reason = (
                "MANUAL STOP"
            )

            self.save()

            logging.warning(
                "========================================"
            )

            logging.warning(
                f"[{self.account.account_name}] "
                "BOT MANUALLY STOPPED"
            )

            try:

                pos = self.refresh_position(
                    force=True
                )

            except Exception:

                return {

                    "success":
                        False,

                    "bot_enabled":
                        False,

                    "message":
                        (
                            "Bot stopped, but exchange "
                            "position could not be checked."
                        )
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

                self.active_trade = None

                self.sl = None

                self.trade_high = None

                self.trade_low = None

                self.save()

                return {

                    "success":
                        True,

                    "bot_enabled":
                        False,

                    "position_closed":
                        True,

                    "message":
                        "Bot stopped. No open position."
                }

            # ------------------------------------------------
            # Close live position.
            # ------------------------------------------------

            try:

                close_exchange_position(
                    self.account,
                    self.product_id,
                    size
                )

            except Exception as e:

                logging.exception(
                    f"[{self.account.account_name}] "
                    f"STOP CLOSE ERROR | {e}"
                )

                return {

                    "success":
                        False,

                    "bot_enabled":
                        False,

                    "position_closed":
                        False,

                    "message":
                        (
                            "Bot stopped, but closing "
                            "the position failed."
                        )
                }

            # ------------------------------------------------
            # Verify close.
            # ------------------------------------------------

            final_position = None

            closed = False

            for _ in range(50):

                time.sleep(
                    0.2
                )

                try:

                    final_position = get_position(
                        self.account,
                        self.product_id
                    )

                    self.cached_position = (
                        final_position
                    )

                    self.position_cache_time = (
                        time.time()
                    )

                    if int(
                        final_position.get(
                            "size",
                            0
                        )
                    ) == 0:

                        closed = True

                        break

                except Exception:

                    continue

            if not closed:

                remaining = (
                    int(
                        final_position.get(
                            "size",
                            size
                        )
                    )
                    if final_position
                    else size
                )

                self.last_position = (
                    remaining
                )

                self.save()

                return {

                    "success":
                        False,

                    "bot_enabled":
                        False,

                    "position_closed":
                        False,

                    "message":
                        (
                            "Bot stopped, but position "
                            "could not be confirmed closed."
                        )
                }

            # ------------------------------------------------
            # Record completed trade.
            # ------------------------------------------------

            exit_price = (
                self.last_price
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

            self.active_trade = None

            self.sl = None

            self.trade_high = None

            self.trade_low = None

            self.cached_position = {

                "size":
                    0,

                "entry":
                    None,

                "stop_loss":
                    None,

                "unrealized_pnl":
                    0
            }

            self.save()

            return {

                "success":
                    True,

                "bot_enabled":
                    False,

                "position_closed":
                    True,

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

        self.day = day

        self.high = None

        self.low = None

        self.ready = False

        live_size = self.last_position

        if live_size == 0:

            try:

                pos = self.refresh_position(
                    force=True
                )

                live_size = int(
                    pos.get(
                        "size",
                        0
                    )
                )

            except Exception:

                live_size = 0

        if live_size == 0:

            self.sl = None

            self.trade_high = None

            self.trade_low = None

            self.active_trade = None

        # ----------------------------------------------------
        # Every new session requires manual START.
        # ----------------------------------------------------

        self.bot_enabled = False

        self.stop_reason = (
            "START REQUIRED"
        )

        self.save()

        logging.warning(
            f"[{self.account.account_name}] "
            f"NEW SESSION | {day}"
        )


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

            return True

        try:

            pos = self.refresh_position(
                force=True
            )

        except Exception:

            pos = {
                "size":
                    self.last_position
            }

        self.last_position = int(
            pos.get(
                "size",
                0
            )
        )

        # ----------------------------------------------------
        # Recover today's high / low.
        # ----------------------------------------------------

        if now > (
            start
            + timedelta(
                seconds=5
            )
        ):

            high, low = historical_high_low(
                self.account,
                start,
                now
            )

            if (
                high is not None
                and low is not None
            ):

                self.high = high

                self.low = low

                self.ready = True

                self.save()

                logging.info(
                    f"[{self.account.account_name}] "
                    f"SESSION RANGE "
                    f"HIGH={high} LOW={low}"
                )

                return True

        # ----------------------------------------------------
        # Fallback.
        # ----------------------------------------------------

        self.high = price

        self.low = price

        self.ready = True

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

        if not self.bot_enabled:

            return False

        if self.last_position != 0:

            return False

        subscription = (
            self.account.subscription_info()
        )

        if (
            not self.account.primary
            and not subscription["active"]
        ):

            logging.warning(
                f"[{self.account.account_name}] "
                "ENTRY BLOCKED - SUBSCRIPTION INACTIVE"
            )

            return False

        pos = self.refresh_position(
            force=True
        )

        if pos["size"] != 0:

            self.last_position = (
                pos["size"]
            )

            return False

        if sl is None:

            return False

        if direction == "LONG":

            if sl >= price:

                logging.error(
                    f"[{self.account.account_name}] "
                    f"LONG BLOCKED | "
                    f"PRICE={price} SL={sl}"
                )

                return False

            side = "buy"

        else:

            if sl <= price:

                logging.error(
                    f"[{self.account.account_name}] "
                    f"SHORT BLOCKED | "
                    f"PRICE={price} SL={sl}"
                )

                return False

            side = "sell"

        try:

            size = calculate_order_size(
                self.account,
                self.product,
                price
            )

            place_market_entry(
                self.account,
                self.product_id,
                side,
                size,
                sl
            )

        except Exception as e:

            logging.exception(
                f"[{self.account.account_name}] "
                f"ENTRY ERROR | {e}"
            )

            return False

        confirmed = False

        for _ in range(50):

            time.sleep(
                0.2
            )

            try:

                pos = get_position(
                    self.account,
                    self.product_id
                )

            except Exception:

                continue

            self.cached_position = (
                pos
            )

            self.position_cache_time = (
                time.time()
            )

            if direction == "LONG":

                if pos["size"] > 0:

                    self.last_position = (
                        pos["size"]
                    )

                    confirmed = True

                    break

            else:

                if pos["size"] < 0:

                    self.last_position = (
                        pos["size"]
                    )

                    confirmed = True

                    break

        if not confirmed:

            logging.error(
                f"[{self.account.account_name}] "
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
            f"[{self.account.account_name}] "
            f"TRADE LIVE | "
            f"{direction} | "
            f"ENTRY≈{price} | "
            f"SL={sl}"
        )

        return True


    # ========================================================
    # FINISH ACTIVE TRADE
    # ========================================================

    def finish_active_trade(
        self,
        exit_price,
        reason
    ):

        if not self.active_trade:

            return

        if exit_price is None:

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

        if (
            entry_price is None
            or not trade_size
        ):

            self.active_trade = None

            self.save()

            return

        pnl = calculate_pnl(
            direction,
            entry_price,
            exit_price,
            trade_size,
            self.product
        )

        trade = {

            "id":
                (
                    f"trade_"
                    f"{int(time.time()*1000)}"
                ),

            "account_id":
                self.account.account_id,

            "account":
                self.account.account_name,

            "symbol":
                SYMBOL,

            "date":
                now_ist().strftime(
                    "%Y-%m-%d"
                ),

            "direction":
                direction,

            "entry_time":
                self.active_trade.get(
                    "entry_time"
                ),

            "exit_time":
                now_ist().isoformat(),

            "entry_price":
                float(
                    entry_price
                ),

            "exit_price":
                float(
                    exit_price
                ),

            "size":
                abs(
                    int(
                        trade_size
                    )
                ),

            "stop_loss":
                (
                    float(self.sl)
                    if self.sl is not None
                    else None
                ),

            "pnl":
                float(pnl),

            "reason":
                reason
        }

        history = load_history(
            self.history_file
        )

        history.append(
            trade
        )

        save_history(
            self.history_file,
            history
        )

        logging.warning(
            f"[{self.account.account_name}] "
            f"TRADE HISTORY SAVED | "
            f"{direction} | "
            f"ENTRY={entry_price} | "
            f"EXIT={exit_price} | "
            f"P&L={pnl}"
        )

        self.active_trade = None

        self.save()


    # ========================================================
    # PRICE TICK
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

            self.account.last_ws_message_time = (
                now_ist().isoformat()
            )

            now = now_ist()

            # ------------------------------------------------
            # Saturday square-off.
            # ------------------------------------------------

            if saturday_squareoff(now):

                try:

                    pos = self.refresh_position(
                        force=True
                    )

                    if pos["size"] != 0:

                        close_exchange_position(
                            self.account,
                            self.product_id,
                            pos["size"]
                        )

                        self.finish_active_trade(
                            price,
                            "SATURDAY_SQUAREOFF"
                        )

                        self.last_position = 0

                        self.sl = None

                        self.cached_position = {

                            "size":
                                0,

                            "entry":
                                None,

                            "stop_loss":
                                None,

                            "unrealized_pnl":
                                0
                        }

                        self.save()

                except Exception as e:

                    logging.exception(
                        f"[{self.account.account_name}] "
                        f"SATURDAY CLOSE ERROR | {e}"
                    )

                return

            # ------------------------------------------------
            # Weekend.
            # ------------------------------------------------

            if weekend(now):

                return

            # ------------------------------------------------
            # New session.
            # ------------------------------------------------

            self.new_day(
                now
            )

            # ------------------------------------------------
            # Before strategy start.
            # ------------------------------------------------

            if now < strategy_start(
                self.day
            ):

                return

            # ------------------------------------------------
            # Prepare range.
            # ------------------------------------------------

            if not self.prepare(
                now,
                price
            ):

                return

            # ------------------------------------------------
            # Position.
            # ------------------------------------------------

            pos = self.refresh_position()

            size = int(
                pos.get(
                    "size",
                    0
                )
            )

            # =================================================
            # POSITION CLOSED
            # =================================================

            if (
                size == 0
                and self.last_position != 0
            ):

                old = self.last_position

                # ------------------------------------------------
                # STOP LOSS -> REVERSE
                # ------------------------------------------------

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

                        self.finish_active_trade(
                            price,
                            "STOP_LOSS"
                        )

                        self.last_position = 0

                        self.sl = None

                        if peak is not None:

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

                        self.finish_active_trade(
                            price,
                            "STOP_LOSS"
                        )

                        self.last_position = 0

                        self.sl = None

                        if trough is not None:

                            self.enter(
                                "LONG",
                                price,
                                trough
                            )

                    return

                # ------------------------------------------------
                # External/manual close.
                # ------------------------------------------------

                self.finish_active_trade(
                    price,
                    "EXTERNAL_CLOSE"
                )

                self.last_position = 0

                self.sl = None

                self.cached_position = {

                    "size":
                        0,

                    "entry":
                        None,

                    "stop_loss":
                        None,

                    "unrealized_pnl":
                        0
                }

                self.save()

                return

            # =================================================
            # LONG
            # =================================================

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

            # =================================================
            # SHORT
            # =================================================

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

            # =================================================
            # FLAT
            # =================================================

            self.last_position = 0

            # ------------------------------------------------
            # STOPPED = NO NEW ENTRY.
            # ------------------------------------------------

            if not self.bot_enabled:

                return

            # ------------------------------------------------
            # Subscription.
            # ------------------------------------------------

            subscription = (
                self.account.subscription_info()
            )

            if (
                not self.account.primary
                and not subscription["active"]
            ):

                return

            # ------------------------------------------------
            # NEW HIGH -> LONG
            # ------------------------------------------------

            if (
                self.high is not None
                and price > self.high
            ):

                old_high = self.high

                sl = self.low

                logging.info(
                    f"[{self.account.account_name}] "
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
            # NEW LOW -> SHORT
            # ------------------------------------------------

            if (
                self.low is not None
                and price < self.low
            ):

                old_low = self.low

                sl = self.high

                logging.info(
                    f"[{self.account.account_name}] "
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
# ACCOUNT STORAGE
# ============================================================

ACCOUNTS = {}

ACCOUNTS_LOCK = threading.RLock()


def load_clients():

    if not os.path.exists(
        CLIENTS_FILE
    ):

        return []

    try:

        with open(
            CLIENTS_FILE,
            "r"
        ) as f:

            data = json.load(
                f
            )

        if isinstance(
            data,
            list
        ):

            return data

    except Exception as e:

        logging.warning(
            f"CLIENT FILE LOAD ERROR | {e}"
        )

    return []


def save_clients(
    clients
):

    tmp = (
        CLIENTS_FILE
        + ".tmp"
    )

    with open(
        tmp,
        "w"
    ) as f:

        json.dump(
            clients,
            f,
            indent=2
        )

    os.replace(
        tmp,
        CLIENTS_FILE
    )


# ============================================================
# CREATE PRIMARY
# ============================================================

def create_primary_account():

    if (
        not PRIMARY_API_KEY
        or not PRIMARY_API_SECRET
    ):

        raise SystemExit(
            "Missing DELTA_API_KEY or DELTA_API_SECRET."
        )

    account = Account(

        account_id=
            PRIMARY_ACCOUNT_ID,

        account_name=
            PRIMARY_ACCOUNT_NAME,

        api_key=
            PRIMARY_API_KEY,

        api_secret=
            PRIMARY_API_SECRET,

        account_type=
            "primary",

        primary=
            True
    )

    account.load_product()

    account.set_leverage()

    account.create_bot()

    return account


# ============================================================
# CREATE CLIENT ACCOUNT
# ============================================================

def create_client_account(
    data
):

    account_id = str(
        data.get(
            "account_id"
        )
        or ""
    ).strip()

    if not account_id:

        account_id = (
            "client_"
            + str(
                int(
                    time.time() * 1000
                )
            )
        )

    account = Account(

        account_id=
            account_id,

        account_name=
            data.get(
                "name",
                "Client"
            ),

        api_key=
            data.get(
                "api_key",
                ""
            ),

        api_secret=
            data.get(
                "api_secret",
                ""
            ),

        account_type=
            "client",

        subscription={

            "subscription_start":
                data.get(
                    "subscription_start"
                ),

            "subscription_expiry":
                data.get(
                    "subscription_expiry"
                ),

            "subscription_fee":
                data.get(
                    "subscription_fee",
                    0
                )
        }
    )

    account.load_product()

    account.set_leverage()

    account.create_bot()

    return account


# ============================================================
# LOAD ALL ACCOUNTS
# ============================================================

def initialize_accounts():

    primary = (
        create_primary_account()
    )

    with ACCOUNTS_LOCK:

        ACCOUNTS[
            primary.account_id
        ] = primary

    clients = load_clients()

    for client in clients:

        try:

            account = (
                create_client_account(
                    client
                )
            )

            with ACCOUNTS_LOCK:

                ACCOUNTS[
                    account.account_id
                ] = account

            logging.warning(
                f"CLIENT LOADED | "
                f"{account.account_name}"
            )

        except Exception as e:

            logging.exception(
                f"CLIENT LOAD FAILED | {e}"
            )

    return primary


# ============================================================
# HISTORY STATISTICS
# ============================================================

def history_statistics(
    history
):

    today = now_ist().strftime(
        "%Y-%m-%d"
    )

    today_trades = [
        trade
        for trade in history
        if trade.get(
            "date"
        ) == today
    ]

    def pnl_value(
        trade
    ):

        try:

            return float(
                trade.get(
                    "pnl",
                    0
                )
            )

        except Exception:

            return 0.0

    all_time_count = len(
        history
    )

    all_time_winning = sum(
        1
        for trade in history
        if pnl_value(trade) > 0
    )

    all_time_losing = sum(
        1
        for trade in history
        if pnl_value(trade) < 0
    )

    all_time_pnl = sum(
        pnl_value(trade)
        for trade in history
    )

    today_count = len(
        today_trades
    )

    today_winning = sum(
        1
        for trade in today_trades
        if pnl_value(trade) > 0
    )

    today_losing = sum(
        1
        for trade in today_trades
        if pnl_value(trade) < 0
    )

    today_pnl = sum(
        pnl_value(trade)
        for trade in today_trades
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
                    (
                        today_winning
                        / today_count
                        * 100
                    )
                    if today_count
                    else 0,
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
                    (
                        all_time_winning
                        / all_time_count
                        * 100
                    )
                    if all_time_count
                    else 0,
                    1
                ),

            "pnl":
                round(
                    all_time_pnl,
                    2
                )
        }
    }


# ============================================================
# DASHBOARD ACCOUNT DATA
# ============================================================

def account_dashboard_data(
    account
):

    bot = account.bot

    if bot is None:

        return None

    with bot.lock:

        try:

            live_position = (
                bot.refresh_position()
            )

        except Exception:

            live_position = {
                "size": 0,
                "entry": None,
                "stop_loss": None,
                "unrealized_pnl": 0
            }

        try:

            live_balance = (
                bot.refresh_balance()
            )

        except Exception:

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

        # ----------------------------------------------------
        # Unrealized PnL.
        # ----------------------------------------------------

        unrealized = (
            live_position.get(
                "unrealized_pnl"
            )
        )

        if unrealized is None:

            try:

                if (
                    size != 0
                    and live_position.get(
                        "entry"
                    ) is not None
                    and bot.last_price is not None
                ):

                    entry = Decimal(
                        str(
                            live_position.get(
                                "entry"
                            )
                        )
                    )

                    qty = Decimal(
                        str(
                            abs(size)
                        )
                    )

                    cv = contract_value(
                        bot.product
                    )

                    if size > 0:

                        unrealized = (
                            bot.last_price
                            - entry
                        ) * qty * cv

                    else:

                        unrealized = (
                            entry
                            - bot.last_price
                        ) * qty * cv

                else:

                    unrealized = Decimal(
                        "0"
                    )

            except Exception:

                unrealized = Decimal(
                    "0"
                )

        history = load_history(
            bot.history_file
        )

        subscription = (
            account.subscription_info()
        )

        # ----------------------------------------------------
        # ONLINE:
        #
        # WebSocket connected OR recent price received.
        # ----------------------------------------------------

        online = (
            account.websocket_connected
            or (
                bot.last_price is not None
                and account.last_ws_message_time
                is not None
            )
        )

        return {

            # ------------------------------------------------
            # APP.JS REQUIRED
            # ------------------------------------------------

            "account_id":
                account.account_id,

            "account_name":
                account.account_name,

            "account_type":
                account.account_type,

            "bot_running":
                online,

            "server_online":
                True,

            "online":
                online,

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

            "leverage":
                float(
                    LEVERAGE
                ),

            "balance":
                (
                    float(
                        live_balance
                    )
                    if live_balance is not None
                    else None
                ),

            "current_price":
                (
                    float(
                        bot.last_price
                    )
                    if bot.last_price is not None
                    else None
                ),

            "high":
                (
                    float(bot.high)
                    if bot.high is not None
                    else None
                ),

            "low":
                (
                    float(bot.low)
                    if bot.low is not None
                    else None
                ),

            "stop_loss":
                (
                    float(bot.sl)
                    if bot.sl is not None
                    else None
                ),

            "position": {

                "direction":
                    direction,

                "size":
                    abs(size),

                "entry_price":
                    (
                        float(
                            live_position.get(
                                "entry"
                            )
                        )
                        if live_position.get(
                            "entry"
                        ) is not None
                        else None
                    ),

                "stop_loss":
                    (
                        float(
                            live_position.get(
                                "stop_loss"
                            )
                        )
                        if live_position.get(
                            "stop_loss"
                        ) is not None
                        else (
                            float(bot.sl)
                            if bot.sl is not None
                            else None
                        )
                    ),

                "unrealized_pnl":
                    (
                        float(unrealized)
                        if unrealized is not None
                        else 0
                    )
            },

            "statistics":
                history_statistics(
                    history
                ),

            "trade_history":
                list(
                    reversed(
                        history
                    )
                ),

            "history_count":
                len(history),

            "subscription":
                subscription,

            "connection": {

                "websocket":
                    account.websocket_connected,

                "last_message":
                    account.last_ws_message_time,

                "last_api_ok":
                    account.last_api_ok_time,

                "api_error":
                    account.api_error
            },

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
# FULL DASHBOARD RESPONSE
# ============================================================

def dashboard_data():

    accounts = []

    with ACCOUNTS_LOCK:

        account_list = list(
            ACCOUNTS.values()
        )

    # --------------------------------------------------------
    # Primary first.
    # --------------------------------------------------------

    account_list.sort(
        key=lambda a:
            (
                0
                if a.primary
                else 1,
                a.account_name.lower()
            )
    )

    for account in account_list:

        try:

            data = (
                account_dashboard_data(
                    account
                )
            )

            if data is not None:

                accounts.append(
                    data
                )

        except Exception as e:

            logging.exception(
                f"DASHBOARD ACCOUNT ERROR | "
                f"{account.account_name} | {e}"
            )

    primary = next(
        (
            a
            for a in accounts
            if a.get(
                "account_type"
            ) == "primary"
        ),
        None
    )

    return {

        "success":
            True,

        # ----------------------------------------------------
        # THIS IS THE MOST IMPORTANT PART FOR app.js
        # ----------------------------------------------------

        "accounts":
            accounts,

        "account_count":
            len(accounts),

        "online":
            bool(
                primary
                and primary.get(
                    "online"
                )
            ),

        "bot_running":
            bool(
                primary
                and primary.get(
                    "bot_running"
                )
            ),

        "bot_enabled":
            bool(
                primary
                and primary.get(
                    "bot_enabled"
                )
            ),

        "account_name":
            (
                primary.get(
                    "account_name"
                )
                if primary
                else PRIMARY_ACCOUNT_NAME
            ),

        "symbol":
            SYMBOL,

        "server_online":
            True
    }


# ============================================================
# CLIENT API HELPERS
# ============================================================

def find_account(
    account_id
):

    with ACCOUNTS_LOCK:

        return ACCOUNTS.get(
            str(account_id)
        )


def client_record_from_account(
    account
):

    return {

        "account_id":
            account.account_id,

        "name":
            account.account_name,

        "api_key":
            account.api_key,

        "api_secret":
            account.api_secret,

        "subscription_start":
            account.subscription.get(
                "subscription_start"
            ),

        "subscription_expiry":
            account.subscription.get(
                "subscription_expiry"
            ),

        "subscription_fee":
            account.subscription.get(
                "subscription_fee",
                0
            )
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


    # ========================================================
    # GET
    # ========================================================

    def do_GET(
        self
    ):

        path = self.path.split(
            "?",
            1
        )[0]

        if path == "/api/health":

            self.send_json({

                "success":
                    True,

                "online":
                    True,

                "bot_running":
                    True,

                "accounts":
                    len(
                        ACCOUNTS
                    ),

                "symbol":
                    SYMBOL
