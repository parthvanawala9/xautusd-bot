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
# DASHBOARD + BOT IN SAME PROCESS
#
# IMPORTANT STARTUP FIX:
# Dashboard is started BEFORE account loading.
# A client/API/product error must NOT kill the dashboard.
# ============================================================

load_dotenv()


# ============================================================
# GLOBAL CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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

LEVERAGE = Decimal(
    os.getenv("LEVERAGE", "50")
)

BALANCE_FRACTION = Decimal(
    os.getenv("BALANCE_FRACTION", "0.10")
)

DASHBOARD_PORT = int(
    os.getenv("DASHBOARD_PORT", "8000")
)

RECONNECT_SECONDS = 3

POSITION_CACHE_SECONDS = float(
    os.getenv("POSITION_CACHE_SECONDS", "1.0")
)

BALANCE_CACHE_SECONDS = float(
    os.getenv("BALANCE_CACHE_SECONDS", "5.0")
)

ACCOUNTS_FILE = os.getenv(
    "ACCOUNTS_FILE",
    os.path.join(BASE_DIR, "accounts.json")
)

STATE_DIR = os.getenv(
    "STATE_DIR",
    os.path.join(BASE_DIR, "account_states")
)

HISTORY_DIR = os.getenv(
    "HISTORY_DIR",
    os.path.join(BASE_DIR, "account_history")
)

ADMIN_PIN = os.getenv(
    "ADMIN_PIN",
    ""
).strip()


# ============================================================
# PRIMARY ACCOUNT
# ============================================================

PRIMARY_ACCOUNT_ID = os.getenv(
    "ACCOUNT_ID",
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


# ============================================================
# DIRECTORIES
# ============================================================

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
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
        microsecond=0
    )

    if dt < boundary:
        boundary -= timedelta(days=1)

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
# FILE HELPERS
# ============================================================

def safe_filename(value):

    result = ""

    for char in str(value):

        if char.isalnum() or char in ("-", "_"):
            result += char
        else:
            result += "_"

    return result or "account"


def account_state_file(account_id):

    return os.path.join(
        STATE_DIR,
        safe_filename(account_id) + ".json"
    )


def account_history_file(account_id):

    return os.path.join(
        HISTORY_DIR,
        safe_filename(account_id) + ".json"
    )


def atomic_write_json(filename, data):

    tmp = filename + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            indent=2
        )

    os.replace(tmp, filename)


# ============================================================
# ACCOUNTS FILE
# ============================================================

accounts_file_lock = threading.RLock()


def load_client_accounts():

    if not os.path.exists(ACCOUNTS_FILE):
        return []

    try:

        with open(
            ACCOUNTS_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if not isinstance(data, list):
            return []

        return data

    except Exception as e:

        logging.warning(
            f"ACCOUNTS LOAD ERROR | {e}"
        )

        return []


def save_client_accounts(accounts):

    with accounts_file_lock:

        atomic_write_json(
            ACCOUNTS_FILE,
            accounts
        )


# ============================================================
# DELTA CLIENT
# ============================================================

class DeltaClient:

    def __init__(
        self,
        api_key,
        api_secret,
        account_name
    ):

        self.api_key = (
            api_key or ""
        ).strip()

        self.api_secret = (
            api_secret or ""
        ).strip()

        self.account_name = (
            account_name or "Account"
        ).strip()

        self.session = requests.Session()

        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "XAUTUSD-Multi-Account-Bot/1.0"
        })


    # ========================================================
    # AUTH
    # ========================================================

    def sign(
        self,
        method,
        path,
        query="",
        body=""
    ):

        timestamp = str(int(time.time()))

        message = (
            method.upper()
            + timestamp
            + path
            + query
            + body
        )

        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "User-Agent": "XAUTUSD-Multi-Account-Bot/1.0"
        }


    # ========================================================
    # REST
    # ========================================================

    def api(
        self,
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

            headers = self.sign(
                method,
                path,
                query,
                body_text
            )

        try:

            response = self.session.request(
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

        except requests.RequestException as e:

            raise RuntimeError(
                f"Delta connection error: {e}"
            ) from e

        try:

            response.raise_for_status()

        except requests.HTTPError as e:

            try:

                error_body = response.json()

                raise RuntimeError(
                    f"Delta HTTP {response.status_code}: "
                    f"{error_body}"
                ) from e

            except ValueError:

                text = (
                    response.text or ""
                ).strip()

                raise RuntimeError(
                    f"Delta HTTP {response.status_code}: "
                    f"{text[:300]}"
                ) from e

        try:

            data = response.json()

        except ValueError as e:

            raise RuntimeError(
                "Delta returned invalid JSON."
            ) from e

        if data.get("success") is False:

            raise RuntimeError(
                f"Delta error: {data}"
            )

        return data


    # ========================================================
    # PRODUCT
    # ========================================================

    def product(self):

        data = self.api(
            "GET",
            f"/v2/products/{SYMBOL}"
        )

        result = data.get("result")

        if not isinstance(result, dict):

            raise RuntimeError(
                f"Invalid product response: {data}"
            )

        return result


    # ========================================================
    # POSITION
    # ========================================================

    def position(self, product_id):

        data = self.api(
            "GET",
            "/v2/positions",
            params={
                "product_id": int(product_id)
            },
            auth=True
        )

        result = data.get("result")

        if not isinstance(result, dict):

            return {
                "size": 0,
                "entry": None,
                "stop_loss": None,
                "unrealized_pnl": 0
            }

        return {
            "size": int(
                result.get("size", 0) or 0
            ),
            "entry": result.get("entry_price"),
            "stop_loss": result.get("stop_loss"),
            "unrealized_pnl": result.get(
                "unrealized_pnl",
                0
            )
        }


    # ========================================================
    # BALANCE
    # ========================================================

    def balance(self):

        data = self.api(
            "GET",
            "/v2/wallet/balances",
            auth=True
        )

        result = data.get(
            "result",
            []
        )

        if isinstance(result, dict):
            result = [result]

        for wallet in result:

            if not isinstance(wallet, dict):
                continue

            asset = str(
                wallet.get(
                    "asset_symbol",
                    ""
                )
            ).upper()

            if asset in ("USD", "USDT"):

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


    # ========================================================
    # LEVERAGE
    # ========================================================

    def set_leverage(self, product_id):

        try:

            self.api(
                "POST",
                f"/v2/products/{product_id}/orders/leverage",
                body={
                    "leverage": str(LEVERAGE)
                },
                auth=True
            )

            logging.info(
                f"{self.account_name} | "
                f"LEVERAGE = {LEVERAGE}x"
            )

        except Exception as e:

            logging.warning(
                f"{self.account_name} | "
                f"LEVERAGE ERROR | {e}"
            )


    # ========================================================
    # ORDER SIZE
    # ========================================================

    def order_size(
        self,
        product_info,
        price
    ):

        bal = self.balance()

        margin = bal * BALANCE_FRACTION

        notional = margin * LEVERAGE

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
            contract_value = Decimal("1")

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

        if increment <= 0:
            increment = Decimal("1")

        size_decimal = (
            raw / increment
        ).to_integral_value(
            rounding=ROUND_DOWN
        ) * increment

        if size_decimal < minimum:
            size_decimal = minimum

        size = int(size_decimal)

        if size <= 0:

            raise RuntimeError(
                "Order size calculated as zero."
            )

        logging.info(
            f"{self.account_name} | "
            f"SIZE | Balance={bal} | "
            f"Margin={margin} | "
            f"Notional={notional} | "
            f"Size={size}"
        )

        return size


    # ========================================================
    # MARKET ENTRY
    # ========================================================

    def market_entry(
        self,
        product_id,
        side,
        size,
        sl
    ):

        body = {
            "product_id": int(product_id),
            "product_symbol": SYMBOL,
            "size": int(abs(size)),
            "side": side,
            "order_type": "market_order",
            "bracket_stop_loss_price": str(sl),
            "bracket_stop_trigger_method":
                "last_traded_price",
            "client_order_id":
                (
                    f"simple_{int(time.time() * 1000)}"
                )[-32:]
        }

        logging.warning(
            f"{self.account_name} | "
            f"ENTRY {side.upper()} | "
            f"SIZE={size} | SL={sl}"
        )

        return self.api(
            "POST",
            "/v2/orders",
            body=body,
            auth=True
        )


    # ========================================================
    # CLOSE POSITION
    # ========================================================

    def close_position(
        self,
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
            "product_id": int(product_id),
            "product_symbol": SYMBOL,
            "size": abs(int(size)),
            "side": side,
            "order_type": "market_order",
            "reduce_only": True,
            "client_order_id":
                (
                    f"close_{int(time.time() * 1000)}"
                )[-32:]
        }

        logging.warning(
            f"{self.account_name} | "
            f"CLOSE POSITION | SIZE={size}"
        )

        return self.api(
            "POST",
            "/v2/orders",
            body=body,
            auth=True
        )


    # ========================================================
    # HISTORICAL HIGH LOW
    # ========================================================

    def historical_high_low(
        self,
        start,
        end
    ):

        try:

            data = self.api(
                "GET",
                "/v2/history/candles",
                params={
                    "resolution": "1m",
                    "symbol": SYMBOL,
                    "start": int(start.timestamp()),
                    "end": int(end.timestamp())
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
                        str(candle["high"])
                    )

                    l = Decimal(
                        str(candle["low"])
                    )

                    if high is None or h > high:
                        high = h

                    if low is None or l < low:
                        low = l

                except Exception:
                    continue

            return high, low

        except Exception as e:

            logging.warning(
                f"{self.account_name} | "
                f"HISTORY ERROR | {e}"
            )

            return None, None


# ============================================================
# TRADE HISTORY
# ============================================================

def load_trade_history(account_id):

    filename = account_history_file(
        account_id
    )

    if not os.path.exists(filename):
        return []

    try:

        with open(
            filename,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if isinstance(data, list):
            return data

    except Exception as e:

        logging.warning(
            f"HISTORY LOAD ERROR | "
            f"{account_id} | {e}"
        )

    return []


def save_trade_history(
    account_id,
    history
):

    atomic_write_json(
        account_history_file(account_id),
        history
    )


# ============================================================
# PNL
# ============================================================

def contract_value_from_product(
    product_info
):

    value = (
        product_info.get("contract_value")
        or product_info.get("contract_value_usd")
        or "1"
    )

    try:

        value = Decimal(str(value))

        if value <= 0:
            return Decimal("1")

        return value

    except Exception:

        return Decimal("1")


def calculate_trade_pnl(
    direction,
    entry_price,
    exit_price,
    size,
    product_info
):

    try:

        entry = Decimal(str(entry_price))
        exit_value = Decimal(str(exit_price))
        qty = Decimal(str(abs(size)))

        contract_value = (
            contract_value_from_product(
                product_info
            )
        )

        if direction == "LONG":

            return (
                exit_value - entry
            ) * qty * contract_value

        return (
            entry - exit_value
        ) * qty * contract_value

    except Exception as e:

        logging.warning(
            f"PNL ERROR | {e}"
        )

        return Decimal("0")


# ============================================================
# ACCOUNT BOT
# ============================================================

class AccountBot:

    def __init__(
        self,
        account_id,
        account_name,
        account_type,
        api_key,
        api_secret,
        subscription=None
    ):

        self.account_id = account_id
        self.account_name = account_name
        self.account_type = account_type

        self.subscription = (
            subscription or {}
        )

        self.client = DeltaClient(
            api_key,
            api_secret,
            account_name
        )

        # Product information is deliberately loaded here,
        # but startup manager catches failures per account.
        self.product = self.client.product()

        self.product_id = int(
            self.product["id"]
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

        self.bot_enabled = False

        self.stop_reason = "START REQUIRED"

        self.active_trade = None

        self.lock = threading.RLock()

        self.cached_position = {
            "size": 0,
            "entry": None,
            "stop_loss": None,
            "unrealized_pnl": 0
        }

        self.position_cache_time = 0

        self.cached_balance = None
        self.balance_cache_time = 0

        self.websocket_connected = False
        self.last_ws_message_time = None
        self.last_api_ok_time = None
        self.api_error = None

        self.load_state()

        # Never automatically resume trading.
        self.bot_enabled = False
        self.stop_reason = "START REQUIRED"

        self.save()


    # ========================================================
    # STATE
    # ========================================================

    def load_state(self):

        filename = account_state_file(
            self.account_id
        )

        if not os.path.exists(filename):
            return

        try:

            with open(
                filename,
                "r",
                encoding="utf-8"
            ) as f:

                state = json.load(f)

            if state.get("day"):

                self.day = datetime.fromisoformat(
                    state["day"]
                )

            if state.get("high") is not None:

                self.high = Decimal(
                    str(state["high"])
                )

            if state.get("low") is not None:

                self.low = Decimal(
                    str(state["low"])
                )

            if state.get("sl") is not None:

                self.sl = Decimal(
                    str(state["sl"])
                )

            if state.get("trade_high") is not None:

                self.trade_high = Decimal(
                    str(state["trade_high"])
                )

            if state.get("trade_low") is not None:

                self.trade_low = Decimal(
                    str(state["trade_low"])
                )

            if state.get("active_trade"):

                self.active_trade = (
                    state["active_trade"]
                )

        except Exception as e:

            logging.warning(
                f"{self.account_name} | "
                f"STATE LOAD ERROR | {e}"
            )


    def save(self):

        data = {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "symbol": SYMBOL,
            "day": (
                self.day.isoformat()
                if self.day
                else None
            ),
            "high": (
                str(self.high)
                if self.high is not None
                else None
            ),
            "low": (
                str(self.low)
                if self.low is not None
                else None
            ),
            "sl": (
                str(self.sl)
                if self.sl is not None
                else None
            ),
            "trade_high": (
                str(self.trade_high)
                if self.trade_high is not None
                else None
            ),
            "trade_low": (
                str(self.trade_low)
                if self.trade_low is not None
                else None
            ),
            "active_trade": self.active_trade,
            "bot_enabled": self.bot_enabled,
            "stop_reason": self.stop_reason
        }

        atomic_write_json(
            account_state_file(
                self.account_id
            ),
            data
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
            ) < POSITION_CACHE_SECONDS
        ):

            return self.cached_position

        try:

            pos = self.client.position(
                self.product_id
            )

            self.cached_position = pos
            self.position_cache_time = current

            self.last_api_ok_time = (
                now_ist().isoformat()
            )

            self.api_error = None

            return pos

        except Exception as e:

            self.api_error = str(e)

            logging.warning(
                f"{self.account_name} | "
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
            ) < BALANCE_CACHE_SECONDS
        ):

            return self.cached_balance

        try:

            value = self.client.balance()

            self.cached_balance = value
            self.balance_cache_time = current

            self.last_api_ok_time = (
                now_ist().isoformat()
            )

            self.api_error = None

            return value

        except Exception as e:

            self.api_error = str(e)

            logging.warning(
                f"{self.account_name} | "
                f"BALANCE ERROR | {e}"
            )

            return self.cached_balance


    # ========================================================
    # START BOT
    # ========================================================

    def start_bot(self):

        with self.lock:

            logging.warning(
                f"{self.account_name} | "
                f"START BOT REQUEST"
            )

            if self.account_type != "primary":

                if not self.subscription_active():

                    self.bot_enabled = False
                    self.stop_reason = (
                        "SUBSCRIPTION EXPIRED"
                    )

                    self.save()

                    return {
                        "success": False,
                        "message":
                            "Client subscription is not active."
                    }

            pos = self.refresh_position(
                force=True
            )

            size = int(
                pos.get("size", 0)
            )

            if size != 0:

                direction = (
                    "LONG"
                    if size > 0
                    else "SHORT"
                )

                recovered_entry = None

                exchange_entry = (
                    pos.get("entry")
                )

                if exchange_entry is not None:

                    try:

                        recovered_entry = Decimal(
                            str(exchange_entry)
                        )

                    except Exception:
                        pass

                if (
                    recovered_entry is None
                    and self.active_trade
                ):

                    saved_entry = (
                        self.active_trade.get(
                            "entry_price"
                        )
                    )

                    if saved_entry is not None:

                        try:

                            recovered_entry = Decimal(
                                str(saved_entry)
                            )

                        except Exception:
                            pass

                if (
                    recovered_entry is None
                    and self.last_price is not None
                ):

                    recovered_entry = self.last_price

                recovered_sl = None

                exchange_sl = (
                    pos.get("stop_loss")
                )

                if exchange_sl is not None:

                    try:

                        recovered_sl = Decimal(
                            str(exchange_sl)
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

                if self.active_trade is None:

                    self.active_trade = {
                        "direction": direction,
                        "entry_price": (
                            float(recovered_entry)
                            if recovered_entry is not None
                            else None
                        ),
                        "entry_time":
                            now_ist().isoformat(),
                        "size": abs(size)
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
                        and recovered_entry is not None
                    ):

                        self.active_trade[
                            "entry_price"
                        ] = float(
                            recovered_entry
                        )

                if recovered_sl is not None:
                    self.sl = recovered_sl

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

                self.last_position = size
                self.bot_enabled = True
                self.stop_reason = None

                self.save()

                return {
                    "success": True,
                    "bot_enabled": True,
                    "position_recovered": True,
                    "direction": direction,
                    "size": abs(size),
                    "entry": (
                        float(recovered_entry)
                        if recovered_entry is not None
                        else None
                    ),
                    "stop_loss": (
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

            self.last_position = 0
            self.active_trade = None
            self.sl = None
            self.trade_high = None
            self.trade_low = None
            self.bot_enabled = True
            self.stop_reason = None

            self.save()

            logging.warning(
                f"{self.account_name} | BOT STARTED"
            )

            return {
                "success": True,
                "bot_enabled": True,
                "position_recovered": False,
                "message":
                    "Bot started. It can now take new positions."
            }


    # ========================================================
    # SUBSCRIPTION
    # ========================================================

    def subscription_active(self):

        if self.account_type == "primary":
            return True

        subscription = (
            self.subscription or {}
        )

        expiry = subscription.get(
            "expiry"
        )

        if not expiry:
            return False

        try:

            expiry_dt = datetime.fromisoformat(
                expiry.replace(
                    "Z",
                    "+00:00"
                )
            )

            if expiry_dt.tzinfo is None:

                expiry_dt = expiry_dt.replace(
                    tzinfo=IST
                )

            return expiry_dt > now_ist()

        except Exception:

            return False


    # ========================================================
    # STOP BOT
    # ========================================================

    def stop_bot(self):

        with self.lock:

            self.bot_enabled = False
            self.stop_reason = "MANUAL STOP"

            self.save()

            try:

                pos = self.refresh_position(
                    force=True
                )

            except Exception as e:

                logging.exception(
                    f"{self.account_name} | "
                    f"STOP POSITION ERROR | {e}"
                )

                return {
                    "success": False,
                    "bot_enabled": False,
                    "message":
                        (
                            "Bot stopped, but exchange "
                            "position could not be checked."
                        )
                }

            size = int(
                pos.get("size", 0)
            )

            if size == 0:

                self.last_position = 0
                self.sl = None
                self.active_trade = None
                self.trade_high = None
                self.trade_low = None

                self.cached_position = {
                    "size": 0,
                    "entry": None,
                    "stop_loss": None,
                    "unrealized_pnl": 0
                }

                self.save()

                return {
                    "success": True,
                    "bot_enabled": False,
                    "position_closed": True,
                    "message":
                        "Bot stopped. No open position."
                }

            try:

                self.client.close_position(
                    self.product_id,
                    size
                )

            except Exception as e:

                logging.exception(
                    f"{self.account_name} | "
                    f"STOP CLOSE ERROR | {e}"
                )

                return {
                    "success": False,
                    "bot_enabled": False,
                    "position_closed": False,
                    "message":
                        (
                            "Bot stopped, but closing "
                            "the position failed."
                        )
                }

            closed = False
            final_position = None

            for _ in range(50):

                time.sleep(0.2)

                try:

                    final_position = (
                        self.client.position(
                            self.product_id
                        )
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
                    pass

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

                self.last_position = remaining

                self.save()

                return {
                    "success": False,
                    "bot_enabled": False,
                    "position_closed": False,
                    "message":
                        (
                            "Bot stopped, but position "
                            "could not be confirmed closed."
                        )
                }

            exit_price = self.last_price

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

            self.cached_position = {
                "size": 0,
                "entry": None,
                "stop_loss": None,
                "unrealized_pnl": 0
            }

            self.save()

            return {
                "success": True,
                "bot_enabled": False,
                "position_closed": True,
                "message":
                    "Bot stopped and open position was closed."
            }


    # ========================================================
    # NEW SESSION
    # ========================================================

    def new_day(self, now):

        day = trading_day_start(now)

        if self.day == day:
            return

        logging.warning(
            f"{self.account_name} | "
            f"NEW SESSION | {day}"
        )

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
                    pos.get("size", 0)
                )

            except Exception:

                live_size = 0

        if live_size == 0:

            self.sl = None
            self.trade_high = None
            self.trade_low = None
            self.active_trade = None

        self.bot_enabled = False
        self.stop_reason = "START REQUIRED"

        self.save()


    # ========================================================
    # PREPARE SESSION
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
                "size": self.last_position,
                "entry": None,
                "stop_loss": None
            }

        self.last_position = int(
            pos.get("size", 0)
        )

        if now > start + timedelta(seconds=5):

            high, low = (
                self.client.historical_high_low(
                    start,
                    now
                )
            )

            if high is not None and low is not None:

                self.high = high
                self.low = low
                self.ready = True

                self.save()

                logging.warning(
                    f"{self.account_name} | "
                    f"RECOVERED RANGE | "
                    f"HIGH={high} | LOW={low}"
                )

                return True

        self.high = price
        self.low = price
        self.ready = True

        self.save()

        logging.warning(
            f"{self.account_name} | "
            f"INITIAL RANGE | HIGH={price} | LOW={price}"
        )

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

        if sl is None:

            logging.error(
                f"{self.account_name} | "
                f"ENTRY BLOCKED | SL NONE"
            )

            return False

        pos = self.refresh_position(
            force=True
        )

        if pos["size"] != 0:

            self.last_position = pos["size"]
            return False

        if direction == "LONG":

            if sl >= price:

                logging.error(
                    f"{self.account_name} | "
                    f"LONG BLOCKED | "
                    f"PRICE={price} | SL={sl}"
                )

                return False

            side = "buy"

        else:

            if sl <= price:

                logging.error(
                    f"{self.account_name} | "
                    f"SHORT BLOCKED | "
                    f"PRICE={price} | SL={sl}"
                )

                return False

            side = "sell"

        try:

            size = self.client.order_size(
                self.product,
                price
            )

            self.client.market_entry(
                self.product_id,
                side,
                size,
                sl
            )

        except Exception as e:

            logging.exception(
                f"{self.account_name} | "
                f"ENTRY ORDER ERROR | {e}"
            )

            return False

        confirmed = False

        for _ in range(50):

            time.sleep(0.2)

            try:

                pos = self.client.position(
                    self.product_id
                )

                self.cached_position = pos
                self.position_cache_time = time.time()

                if direction == "LONG":

                    if pos["size"] > 0:

                        self.last_position = pos["size"]
                        confirmed = True
                        break

                else:

                    if pos["size"] < 0:

                        self.last_position = pos["size"]
                        confirmed = True
                        break

            except Exception as e:

                logging.warning(
                    f"{self.account_name} | "
                    f"ENTRY VERIFY ERROR | {e}"
                )

        if not confirmed:

            logging.error(
                f"{self.account_name} | "
                f"ENTRY NOT CONFIRMED"
            )

            return False

        self.sl = Decimal(str(sl))

        if direction == "LONG":

            self.trade_high = price
            self.trade_low = None

        else:

            self.trade_low = price
            self.trade_high = None

        self.active_trade = {
            "direction": direction,
            "entry_price": float(price),
            "entry_time": now_ist().isoformat(),
            "size": abs(int(self.last_position))
        }

        self.save()

        logging.warning(
            f"{self.account_name} | "
            f"TRADE LIVE | {direction} | "
            f"ENTRY={price} | SL={sl}"
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
                abs(int(self.last_position))
            )
        )

        if entry_price is None:

            self.active_trade = None
            self.save()
            return

        pnl = calculate_trade_pnl(
            direction,
            entry_price,
            exit_price,
            trade_size,
            self.product
        )

        trade = {
            "id":
                f"trade_{int(time.time() * 1000)}",
            "account_id":
                self.account_id,
            "account":
                self.account_name,
            "symbol":
                SYMBOL,
            "date":
                now_ist().strftime("%Y-%m-%d"),
            "direction":
                direction,
            "entry_time":
                self.active_trade.get(
                    "entry_time"
                ),
            "exit_time":
                now_ist().isoformat(),
            "entry_price":
                float(entry_price),
            "exit_price":
                float(exit_price),
            "size":
                abs(int(trade_size)),
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

        history = load_trade_history(
            self.account_id
        )

        history.append(trade)

        save_trade_history(
            self.account_id,
            history
        )

        logging.warning(
            f"{self.account_name} | "
            f"TRADE HISTORY SAVED | "
            f"{direction} | "
            f"ENTRY={entry_price} | "
            f"EXIT={exit_price} | "
            f"PNL={pnl} | "
            f"REASON={reason}"
        )

        self.active_trade = None
        self.save()


    # ========================================================
    # PRICE TICK
    # ========================================================

    def price_tick(self, price):

        with self.lock:

            self.last_price = price

            now = now_ist()

            self.last_ws_message_time = (
                now.isoformat()
            )

            # ------------------------------------------------
            # Saturday square-off
            # ------------------------------------------------

            if saturday_squareoff(now):

                try:

                    pos = self.refresh_position(
                        force=True
                    )

                    if pos["size"] != 0:

                        self.client.close_position(
                            self.product_id,
                            pos["size"]
                        )

                        self.finish_active_trade(
                            price,
                            "SATURDAY_SQUAREOFF"
                        )

                        self.last_position = 0
                        self.sl = None
                        self.trade_high = None
                        self.trade_low = None

                        self.cached_position = {
                            "size": 0,
                            "entry": None,
                            "stop_loss": None,
                            "unrealized_pnl": 0
                        }

                        self.save()

                except Exception as e:

                    logging.exception(
                        f"{self.account_name} | "
                        f"SATURDAY SQUAREOFF ERROR | {e}"
                    )

                return

            # ------------------------------------------------
            # Weekend
            # ------------------------------------------------

            if weekend(now):
                return

            # ------------------------------------------------
            # New session
            # ------------------------------------------------

            self.new_day(now)

            # ------------------------------------------------
            # Before 05:45
            # ------------------------------------------------

            if now < strategy_start(self.day):
                return

            # ------------------------------------------------
            # Prepare range
            # ------------------------------------------------

            if not self.prepare(
                now,
                price
            ):
                return

            # ------------------------------------------------
            # Position
            # ------------------------------------------------

            pos = self.refresh_position()

            size = int(
                pos.get("size", 0)
            )

            # =================================================
            # POSITION CLOSED
            # =================================================

            if (
                size == 0
                and self.last_position != 0
            ):

                old = self.last_position

                sl_hit = (
                    self.sl is not None
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
                )

                if self.bot_enabled and sl_hit:

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

                self.finish_active_trade(
                    price,
                    "EXTERNAL_CLOSE"
                )

                self.last_position = 0
                self.sl = None
                self.trade_high = None
                self.trade_low = None

                self.cached_position = {
                    "size": 0,
                    "entry": None,
                    "stop_loss": None,
                    "unrealized_pnl": 0
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

            if not self.bot_enabled:
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

                logging.warning(
                    f"{self.account_name} | "
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

                logging.warning(
                    f"{self.account_name} | "
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
# ACCOUNT MANAGER
# ============================================================

BOT_ACCOUNTS = {}

ACCOUNTS_LOCK = threading.RLock()


def create_primary_account():

    return AccountBot(
        account_id=PRIMARY_ACCOUNT_ID,
        account_name=PRIMARY_ACCOUNT_NAME,
        account_type="primary",
        api_key=PRIMARY_API_KEY,
        api_secret=PRIMARY_API_SECRET,
        subscription={}
    )


def create_client_account(data):

    return AccountBot(
        account_id=data["account_id"],
        account_name=data.get(
            "name",
            "Client Account"
        ),
        account_type="client",
        api_key=data["api_key"],
        api_secret=data["api_secret"],
        subscription={
            "start":
                data.get(
                    "subscription_start"
                ),
            "expiry":
                data.get(
                    "subscription_expiry"
                ),
            "fee":
                data.get(
                    "subscription_fee",
                    0
                )
        }
    )


# ============================================================
# LOAD PRIMARY
# ============================================================

def load_primary_account():

    try:

        primary = create_primary_account()

        primary.client.set_leverage(
            primary.product_id
        )

        with ACCOUNTS_LOCK:

            BOT_ACCOUNTS[
                primary.account_id
            ] = primary

        logging.warning(
            f"PRIMARY ACCOUNT LOADED | "
            f"{primary.account_name}"
        )

        return True

    except Exception as e:

        logging.exception(
            f"PRIMARY ACCOUNT LOAD ERROR | {e}"
        )

        return False


# ============================================================
# LOAD CLIENTS
# ============================================================

def load_client_accounts_into_manager():

    clients = load_client_accounts()

    loaded = 0

    for data in clients:

        try:

            account_id = str(
                data.get(
                    "account_id",
                    ""
                )
            ).strip()

            if not account_id:
                continue

            if account_id == PRIMARY_ACCOUNT_ID:
                continue

            if not data.get("api_key"):
                logging.warning(
                    f"CLIENT SKIPPED | {account_id} | "
                    f"API KEY MISSING"
                )
                continue

            if not data.get("api_secret"):
                logging.warning(
                    f"CLIENT SKIPPED | {account_id} | "
                    f"API SECRET MISSING"
                )
                continue

            client_bot = create_client_account(
                data
            )

            client_bot.client.set_leverage(
                client_bot.product_id
            )

            with ACCOUNTS_LOCK:

                BOT_ACCOUNTS[
                    account_id
                ] = client_bot

            loaded += 1

            logging.warning(
                f"CLIENT ACCOUNT LOADED | "
                f"{client_bot.account_name}"
            )

        except Exception as e:

            logging.exception(
                f"CLIENT LOAD ERROR | "
                f"{data.get('name', 'Unknown')} | {e}"
            )

            # IMPORTANT:
            # One bad client MUST NOT stop other clients
            # or the dashboard.


    return loaded


# ============================================================
# SAFE ACCOUNT LOADER
# ============================================================

def load_all_accounts():

    logging.warning(
        "========================================"
    )

    logging.warning(
        "ACCOUNT LOADING STARTED"
    )

    logging.warning(
        "========================================"
    )

    with ACCOUNTS_LOCK:

        BOT_ACCOUNTS.clear()

    primary_ok = load_primary_account()

    if not primary_ok:

        logging.error(
            "PRIMARY ACCOUNT COULD NOT BE LOADED."
        )

    client_count = (
        load_client_accounts_into_manager()
    )

    logging.warning(
        f"ACCOUNT LOADING COMPLETE | "
        f"PRIMARY_OK={primary_ok} | "
        f"CLIENTS={client_count} | "
        f"TOTAL={len(BOT_ACCOUNTS)}"
    )

    return primary_ok


def get_bot(account_id):

    with ACCOUNTS_LOCK:

        return BOT_ACCOUNTS.get(
            str(account_id)
        )


# ============================================================
# DASHBOARD HELPERS
# ============================================================

def decimal_json(value):

    if value is None:
        return None

    if isinstance(value, Decimal):
        return float(value)

    try:
        return float(value)

    except Exception:
        return value


def history_statistics(history):

    today = now_ist().strftime(
        "%Y-%m-%d"
    )

    today_trades = [
        trade
        for trade in history
        if trade.get("date") == today
    ]

    all_time_count = len(history)

    all_time_winning = sum(
        1
        for trade in history
        if float(
            trade.get("pnl", 0)
        ) > 0
    )

    all_time_losing = sum(
        1
        for trade in history
        if float(
            trade.get("pnl", 0)
        ) < 0
    )

    all_time_pnl = sum(
        float(
            trade.get("pnl", 0)
        )
        for trade in history
    )

    all_time_win_rate = (
        all_time_winning
        / all_time_count
        * 100
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
            trade.get("pnl", 0)
        ) > 0
    )

    today_losing = sum(
        1
        for trade in today_trades
        if float(
            trade.get("pnl", 0)
        ) < 0
    )

    today_pnl = sum(
        float(
            trade.get("pnl", 0)
        )
        for trade in today_trades
    )

    today_win_rate = (
        today_winning
        / today_count
        * 100
        if today_count > 0
        else 0
    )

    return {
        "today": {
            "total_trades": today_count,
            "winning_trades": today_winning,
            "losing_trades": today_losing,
            "win_rate": round(
                today_win_rate,
                1
            ),
            "pnl": round(
                today_pnl,
                2
            )
        },
        "all_time": {
            "total_trades": all_time_count,
            "winning_trades": all_time_winning,
            "losing_trades": all_time_losing,
            "win_rate": round(
                all_time_win_rate,
                1
            ),
            "pnl": round(
                all_time_pnl,
                2
            )
        }
    }


def subscription_data(bot):

    if bot.account_type == "primary":

        return {
            "active": True,
            "expired": False,
            "start": None,
            "expiry": None,
            "fee": 0
        }

    sub = (
        bot.subscription or {}
    )

    expiry = sub.get(
        "expiry"
    )

    active = bot.subscription_active()

    return {
        "active": active,
        "expired": (
            bool(expiry)
            and not active
        ),
        "start": sub.get(
            "start"
        ),
        "expiry": expiry,
        "fee": sub.get(
            "fee",
            0
        )
    }


def account_dashboard(bot):

    with bot.lock:

        live_position = (
            bot.refresh_position()
        )

        live_balance = (
            bot.refresh_balance()
        )

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
                        str(abs(size))
                    )

                    cv = (
                        contract_value_from_product(
                            bot.product
                        )
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

                    unrealized = Decimal("0")

            except Exception:

                unrealized = Decimal("0")

        history = load_trade_history(
            bot.account_id
        )

        statistics = history_statistics(
            history
        )

        history_for_dashboard = list(
            reversed(history)
        )

        online = (
            bot.websocket_connected
            or
            bot.last_price is not None
        )

        return {
            "account_id":
                bot.account_id,

            "account_name":
                bot.account_name,

            "account_type":
                bot.account_type,

            "online":
                online,

            "bot_running":
                bot.bot_enabled,

            "server_online":
                True,

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
                float(LEVERAGE),

            "balance_fraction":
                float(BALANCE_FRACTION),

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
                        bot.sl
                    ),

                "unrealized_pnl":
                    decimal_json(
                        unrealized
                    )
            },

            "statistics":
                statistics,

            "trade_history":
                history_for_dashboard,

            "history_count":
                len(history),

            "subscription":
                subscription_data(bot),

            "connection": {
                "websocket":
                    bot.websocket_connected,

                "last_message":
                    bot.last_ws_message_time,

                "last_api_ok":
                    bot.last_api_ok_time,

                "api_error":
                    bot.api_error
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


def dashboard_data():

    with ACCOUNTS_LOCK:

        bots = list(
            BOT_ACCOUNTS.values()
        )

    accounts = []

    for bot in bots:

        try:

            accounts.append(
                account_dashboard(bot)
            )

        except Exception as e:

            logging.exception(
                f"DASHBOARD ACCOUNT ERROR | "
                f"{bot.account_name} | {e}"
            )

            accounts.append({
                "account_id":
                    bot.account_id,
                "account_name":
                    bot.account_name,
                "account_type":
                    bot.account_type,
                "online":
                    False,
                "bot_running":
                    False,
                "server_online":
                    True,
                "bot_enabled":
                    False,
                "bot_status":
                    "ERROR",
                "stop_reason":
                    "ACCOUNT DATA ERROR",
                "symbol":
                    SYMBOL,
                "leverage":
                    float(LEVERAGE),
                "balance":
                    None,
                "current_price":
                    decimal_json(
                        bot.last_price
                    ),
                "position": {
                    "direction": "FLAT",
                    "size": 0,
                    "entry_price": None,
                    "stop_loss": None,
                    "unrealized_pnl": 0
                },
                "statistics":
                    history_statistics(
                        load_trade_history(
                            bot.account_id
                        )
                    ),
                "trade_history": [],
                "history_count": 0,
                "subscription":
                    subscription_data(bot),
                "connection": {
                    "websocket":
                        bot.websocket_connected,
                    "last_message":
                        bot.last_ws_message_time,
                    "last_api_ok":
                        bot.last_api_ok_time,
                    "api_error":
                        str(e)
                },
                "session": {
                    "day": None,
                    "strategy_start": None,
                    "ready": False
                }
            })


    any_bot_running = any(
        bot.bot_enabled
        for bot in bots
    )

    any_online = any(
        (
            bot.websocket_connected
            or
            bot.last_price is not None
        )
        for bot in bots
    )

    return {
        "success": True,
        "online": any_online,
        "server_online": True,
        "bot_running": any_bot_running,
        "accounts": accounts
    }


# ============================================================
# HTTP BODY
# ============================================================

def read_json_body(handler):

    try:

        length = int(
            handler.headers.get(
                "Content-Length",
                "0"
            )
        )

    except Exception:

        length = 0

    if length <= 0:
        return {}

    raw = handler.rfile.read(length)

    if not raw:
        return {}

    try:

        return json.loads(
            raw.decode("utf-8")
        )

    except Exception:

        return {}


# ============================================================
# ADMIN PIN
# ============================================================

def verify_admin_pin(handler):

    if not ADMIN_PIN:
        return True

    supplied = (
        handler.headers.get(
            "X-Admin-Pin",
            ""
        )
    )

    return hmac.compare_digest(
        supplied,
        ADMIN_PIN
    )


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

    def do_GET(self):

        path = self.path.split(
            "?",
            1
        )[0]

        if path == "/api/health":

            with ACCOUNTS_LOCK:

                account_count = len(
                    BOT_ACCOUNTS
                )

                running = any(
                    bot.bot_enabled
                    for bot
                    in BOT_ACCOUNTS.values()
                )

            self.send_json({
                "success": True,
                "online": True,
                "server_online": True,
                "bot_running": running,
                "accounts": account_count,
                "symbol": SYMBOL
            })

            return


        if path == "/api/dashboard":

            try:

                self.send_json(
                    dashboard_data()
                )

            except Exception as e:

                logging.exception(
                    f"DASHBOARD ERROR | {e}"
                )

                self.send_json(
                    {
                        "success": False,
                        "message":
                            f"Dashboard data error: {e}"
                    },
                    status=500
                )

            return


        if path == "/":

            self.path = "/index.html"

        return super().do_GET()


    # ========================================================
    # POST
    # ========================================================

    def do_POST(self):

        if not verify_admin_pin(self):

            self.send_json(
                {
                    "success": False,
                    "message":
                        "Invalid admin PIN."
                },
                status=403
            )

            return

        path = self.path.split(
            "?",
            1
        )[0]

        body = read_json_body(self)


        # ====================================================
        # START BOT
        # ====================================================

        if path == "/api/bot/start":

            account_id = body.get(
                "account_id"
            )

            bot = get_bot(
                account_id
            )

            if bot is None:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Account not found."
                    },
                    status=404
                )

                return

            try:

                result = bot.start_bot()

                self.send_json(
                    result,
                    status=(
                        200
                        if result.get(
                            "success"
                        )
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
                            f"Failed to start bot: {e}"
                    },
                    status=500
                )

            return


        # ====================================================
        # STOP BOT
        # ====================================================

        if path == "/api/bot/stop":

            account_id = body.get(
                "account_id"
            )

            bot = get_bot(
                account_id
            )

            if bot is None:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Account not found."
                    },
                    status=404
                )

                return

            try:

                result = bot.stop_bot()

                self.send_json(
                    result,
                    status=(
                        200
                        if result.get(
                            "success"
                        )
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
                            f"Failed to stop bot: {e}"
                    },
                    status=500
                )

            return


        # ====================================================
        # ADD CLIENT
        # ====================================================

        if path == "/api/client/add":

            name = str(
                body.get("name", "")
            ).strip()

            api_key = str(
                body.get("api_key", "")
            ).strip()

            api_secret = str(
                body.get("api_secret", "")
            ).strip()

            subscription_start = body.get(
                "subscription_start"
            )

            subscription_expiry = body.get(
                "subscription_expiry"
            )

            subscription_fee = body.get(
                "subscription_fee",
                0
            )

            if not name:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Enter client name."
                    },
                    status=400
                )

                return

            if not api_key or not api_secret:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Enter Delta API key and API secret."
                    },
                    status=400
                )

                return

            if (
                not subscription_start
                or
                not subscription_expiry
            ):

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Enter subscription start and expiry."
                    },
                    status=400
                )

                return

            account_id = (
                f"client_{int(time.time() * 1000)}"
            )

            client_record = {
                "account_id":
                    account_id,

                "name":
                    name,

                "api_key":
                    api_key,

                "api_secret":
                    api_secret,

                "subscription_start":
                    subscription_start,

                "subscription_expiry":
                    subscription_expiry,

                "subscription_fee":
                    float(
                        subscription_fee or 0
                    )
            }

            try:

                # Validate credentials/product before saving.
                test_client = DeltaClient(
                    api_key,
                    api_secret,
                    name
                )

                test_product = (
                    test_client.product()
                )

                product_id = int(
                    test_product["id"]
                )

                test_client.set_leverage(
                    product_id
                )

                clients = load_client_accounts()

                clients.append(
                    client_record
                )

                save_client_accounts(
                    clients
                )

                bot = AccountBot(
                    account_id=account_id,
                    account_name=name,
                    account_type="client",
                    api_key=api_key,
                    api_secret=api_secret,
                    subscription={
                        "start":
                            subscription_start,
                        "expiry":
                            subscription_expiry,
                        "fee":
                            float(
                                subscription_fee or 0
                            )
                    }
                )

                bot.client.set_leverage(
                    bot.product_id
                )

                with ACCOUNTS_LOCK:

                    BOT_ACCOUNTS[
                        account_id
                    ] = bot

                logging.warning(
                    f"CLIENT ADDED | "
                    f"{name} | {account_id}"
                )

                self.send_json({
                    "success": True,
                    "account_id":
                        account_id,
                    "message":
                        "Client account added successfully."
                })

            except Exception as e:

                logging.exception(
                    f"ADD CLIENT ERROR | {e}"
                )

                self.send_json(
                    {
                        "success": False,
                        "message":
                            (
                                "Could not add client. "
                                f"{e}"
                            )
                    },
                    status=500
                )

            return


        # ====================================================
        # UPDATE SUBSCRIPTION
        # ====================================================

        if path == "/api/client/subscription":

            account_id = str(
                body.get(
                    "account_id",
                    ""
                )
            ).strip()

            bot = get_bot(
                account_id
            )

            if bot is None:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Account not found."
                    },
                    status=404
                )

                return

            if bot.account_type == "primary":

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Primary account subscription cannot be changed."
                    },
                    status=400
                )

                return

            start = body.get(
                "subscription_start"
            )

            expiry = body.get(
                "subscription_expiry"
            )

            fee = body.get(
                "subscription_fee",
                0
            )

            try:

                clients = load_client_accounts()

                found = False

                for client in clients:

                    if str(
                        client.get(
                            "account_id"
                        )
                    ) == account_id:

                        client[
                            "subscription_start"
                        ] = start

                        client[
                            "subscription_expiry"
                        ] = expiry

                        client[
                            "subscription_fee"
                        ] = float(
                            fee or 0
                        )

                        found = True
                        break

                if not found:

                    self.send_json(
                        {
                            "success": False,
                            "message":
                                "Client account not found."
                        },
                        status=404
                    )

                    return

                save_client_accounts(
                    clients
                )

                bot.subscription = {
                    "start": start,
                    "expiry": expiry,
                    "fee": float(fee or 0)
                }

                if not bot.subscription_active():

                    bot.bot_enabled = False
                    bot.stop_reason = (
                        "SUBSCRIPTION EXPIRED"
                    )

                    bot.save()

                self.send_json({
                    "success": True,
                    "message":
                        "Subscription updated successfully."
                })

            except Exception as e:

                logging.exception(
                    f"SUBSCRIPTION UPDATE ERROR | {e}"
                )

                self.send_json(
                    {
                        "success": False,
                        "message":
                            f"Failed to update subscription: {e}"
                    },
                    status=500
                )

            return


        # ====================================================
        # DELETE CLIENT
        # ====================================================

        if path == "/api/client/delete":

            account_id = str(
                body.get(
                    "account_id",
                    ""
                )
            ).strip()

            bot = get_bot(
                account_id
            )

            if bot is None:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Account not found."
                    },
                    status=404
                )

                return

            if bot.account_type == "primary":

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Primary account cannot be deleted."
                    },
                    status=400
                )

                return

            try:

                pos = bot.refresh_position(
                    force=True
                )

                if int(
                    pos.get(
                        "size",
                        0
                    )
                ) != 0:

                    bot.bot_enabled = False
                    bot.stop_reason = (
                        "CLIENT DELETED"
                    )

                    bot.save()

                    bot.client.close_position(
                        bot.product_id,
                        int(pos["size"])
                    )

                    for _ in range(50):

                        time.sleep(0.2)

                        try:

                            check = (
                                bot.client.position(
                                    bot.product_id
                                )
                            )

                            if int(
                                check.get(
                                    "size",
                                    0
                                )
                            ) == 0:

                                break

                        except Exception:
                            pass

                clients = load_client_accounts()

                clients = [
                    client
                    for client in clients
                    if str(
                        client.get(
                            "account_id"
                        )
                    ) != account_id
                ]

                save_client_accounts(
                    clients
                )

                with ACCOUNTS_LOCK:

                    BOT_ACCOUNTS.pop(
                        account_id,
                        None
                    )

                logging.warning(
                    f"CLIENT DELETED | "
                    f"{account_id}"
                )

                self.send_json({
                    "success": True,
                    "message":
                        "Client account deleted successfully."
                })

            except Exception as e:

                logging.exception(
                    f"DELETE CLIENT ERROR | {e}"
                )

                self.send_json(
                    {
                        "success": False,
                        "message":
                            (
                                "Could not delete client. "
                                f"{e}"
                            )
                    },
                    status=500
                )

            return


        # ====================================================
        # UNKNOWN
        # ====================================================

        self.send_json(
            {
                "success": False,
                "message":
                    "Unknown endpoint."
            },
            status=404
        )


    # ========================================================
    # SEND JSON
    # ========================================================

    def send_json(
        self,
        data,
        status=200
    ):

        raw = json.dumps(
            data,
            separators=(",", ":")
        ).encode("utf-8")

        self.send_response(status)

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
            "no-store, no-cache, must-revalidate"
        )

        self.send_header(
            "Pragma",
            "no-cache"
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.end_headers()

        self.wfile.write(raw)


    # ========================================================
    # LOG
    # ========================================================

    def log_message(
        self,
        format,
        *args
    ):

        return


# ============================================================
# DASHBOARD SERVER
# ============================================================

dashboard_server = None


def start_dashboard():

    global dashboard_server

    def server_thread():

        global dashboard_server

        try:

            server = ThreadingHTTPServer(
                (
                    "127.0.0.1",
                    DASHBOARD_PORT
                ),
                DashboardHandler
            )

            dashboard_server = server

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
                "DASHBOARD AVAILABLE"
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

    # Give server a moment to bind.
    time.sleep(0.2)


# ============================================================
# WEBSOCKET
# ============================================================

def run_websocket():

    while True:

        try:

            logging.warning(
                f"CONNECTING WS | {WS_URL}"
            )

            def on_open(ws):

                with ACCOUNTS_LOCK:

                    for bot in (
                        BOT_ACCOUNTS.values()
                    ):

                        bot.websocket_connected = True

                payload = {
                    "type": "subscribe",
                    "payload": {
                        "channels": [
                            {
                                "name": "trades",
                                "symbols": [SYMBOL]
                            }
                        ]
                    }
                }

                ws.send(
                    json.dumps(payload)
                )

                logging.warning(
                    f"TRADES SUBSCRIBED | {SYMBOL}"
                )


            def on_message(
                ws,
                message
            ):

                current_time = (
                    now_ist().isoformat()
                )

                try:

                    data = json.loads(message)

                    with ACCOUNTS_LOCK:

                        bots = list(
                            BOT_ACCOUNTS.values()
                        )

                    for bot in bots:

                        bot.last_ws_message_time = (
                            current_time
                        )

                    if data.get("type") != "trades":
                        return

                    symbol = (
                        data.get("sy")
                        or
                        data.get("symbol")
                    )

                    price_value = data.get("p")

                    if (
                        price_value is None
                        and isinstance(
                            data.get("data"),
                            dict
                        )
                    ):

                        trade_data = data.get(
                            "data"
                        )

                        price_value = (
                            trade_data.get("p")
                            or
                            trade_data.get("price")
                        )

                        if symbol is None:

                            symbol = (
                                trade_data.get("sy")
                                or
                                trade_data.get("symbol")
                            )

                    if (
                        symbol != SYMBOL
                        or
                        price_value is None
                    ):
                        return

                    try:

                        price = Decimal(
                            str(price_value)
                        )

                    except Exception:

                        return

                    for bot in bots:

                        try:

                            bot.price_tick(
                                price
                            )

                        except Exception as e:

                            logging.exception(
                                f"{bot.account_name} | "
                                f"PRICE TICK ERROR | {e}"
                            )

                except Exception as e:

                    logging.exception(
                        f"WS MESSAGE ERROR | {e}"
                    )


            def on_error(
                ws,
                error
            ):

                with ACCOUNTS_LOCK:

                    for bot in (
                        BOT_ACCOUNTS.values()
                    ):

                        bot.websocket_connected = False

                logging.error(
                    f"WS ERROR | {error}"
                )


            def on_close(
                ws,
                code,
                msg
            ):

                with ACCOUNTS_LOCK:

                    for bot in (
                        BOT_ACCOUNTS.values()
                    ):

                        bot.websocket_connected = False

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
            f"RECONNECTING IN "
            f"{RECONNECT_SECONDS}s"
        )

        time.sleep(
            RECONNECT_SECONDS
        )


# ============================================================
# BACKGROUND ACCOUNT RETRY
# ============================================================

def account_retry_worker():

    while True:

        try:

            with ACCOUNTS_LOCK:

                has_primary = (
                    PRIMARY_ACCOUNT_ID
                    in BOT_ACCOUNTS
                )

            if not has_primary:

                logging.warning(
                    "PRIMARY ACCOUNT MISSING | "
                    "RETRYING ACCOUNT LOAD"
                )

                load_primary_account()

            # Load clients that were not successfully loaded.
            client_records = load_client_accounts()

            for data in client_records:

                account_id = str(
                    data.get(
                        "account_id",
                        ""
                    )
                ).strip()

                if not account_id:
                    continue

                with ACCOUNTS_LOCK:

                    already_loaded = (
                        account_id
                        in BOT_ACCOUNTS
                    )

                if already_loaded:
                    continue

                try:

                    if (
                        not data.get("api_key")
                        or
                        not data.get("api_secret")
                    ):
                        continue

                    bot = create_client_account(
                        data
                    )

                    bot.client.set_leverage(
                        bot.product_id
                    )

                    with ACCOUNTS_LOCK:

                        BOT_ACCOUNTS[
                            account_id
                        ] = bot

                    logging.warning(
                        f"CLIENT RETRY SUCCESS | "
                        f"{bot.account_name}"
                    )

                except Exception as e:

                    logging.warning(
                        f"CLIENT RETRY FAILED | "
                        f"{data.get('name', account_id)} | "
                        f"{e}"
                    )

        except Exception as e:

            logging.exception(
                f"ACCOUNT RETRY WORKER ERROR | {e}"
            )

        time.sleep(30)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    logging.warning(
        "============================================"
    )

    logging.warning(
        "XAUTUSD MULTI ACCOUNT BOT"
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
        "STRATEGY = NEW HIGH / NEW LOW"
    )

    logging.warning(
        "SESSION = 05:30 IST"
    )

    logging.warning(
        "TRADING START = 05:45 IST"
    )

    logging.warning(
        "START = MANUAL FROM DASHBOARD"
    )

    logging.warning(
        "EXISTING POSITION = RECOVERED, NOT CLOSED"
    )

    logging.warning(
        "STOP = CLOSE POSITION + STOP BOT"
    )

    logging.warning(
        "SATURDAY = 05:00 SQUARE OFF"
    )

    logging.warning(
        "MULTI ACCOUNT = ENABLED"
    )

    logging.warning(
        "DASHBOARD = SAME BOT PROCESS"
    )

    logging.warning(
        f"DASHBOARD PORT = {DASHBOARD_PORT}"
    )

    logging.warning(
        f"BASE DIR = {BASE_DIR}"
    )

    logging.warning(
        "============================================"
    )

    try:

        # ====================================================
        # CRITICAL:
        # Dashboard starts FIRST.
        # Account/API errors cannot prevent dashboard startup.
        # ====================================================

        start_dashboard()

        # ====================================================
        # Load accounts after dashboard is alive.
        # ====================================================

        load_all_accounts()

        # ====================================================
        # Retry missing accounts in background.
        # ====================================================

        retry_thread = threading.Thread(
            target=account_retry_worker,
            daemon=True,
            name="account-retry"
        )

        retry_thread.start()

        # ====================================================
        # Public WebSocket.
        # ====================================================

        run_websocket()

    except KeyboardInterrupt:

        logging.warning(
            "BOT PROCESS STOPPED"
        )

    except Exception as e:

        logging.exception(
            f"FATAL ERROR | {e}"
        )

        # Keep process alive long enough to show the
        # dashboard if an unexpected error occurs.
        while True:

            time.sleep(60)
