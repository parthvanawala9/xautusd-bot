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
# PRIMARY ACCOUNT + CLIENT ACCOUNTS
#
# SAME STRATEGY FOR EVERY ACCOUNT
#
# PRIMARY:
#   DELTA_API_KEY
#   DELTA_API_SECRET
#
# CLIENT:
#   API credentials entered from dashboard
#
# SUBSCRIPTION:
#   start date
#   expiry date
#   fee
#
# WHEN SUBSCRIPTION EXPIRES:
#   - bot disabled
#   - open position closed
#   - no new trades
#
# IMPORTANT:
#   Existing strategy is preserved:
#
#   05:45 IST = trading session
#
#   FLAT:
#       price > HIGH -> LONG
#       price < LOW  -> SHORT
#
#   LONG:
#       new HIGH -> update HIGH
#
#   SHORT:
#       new LOW -> update LOW
#
#   LONG SL = LOW
#   SHORT SL = HIGH
#
#   SL hit:
#       LONG -> SHORT
#       SHORT -> LONG
#
#   Saturday 05:00:
#       close position
#
#   One position per account.
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

PRIMARY_API_KEY = os.getenv(
    "DELTA_API_KEY",
    ""
).strip()

PRIMARY_API_SECRET = os.getenv(
    "DELTA_API_SECRET",
    ""
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

ADMIN_PIN = os.getenv(
    "DASHBOARD_ADMIN_PIN",
    ""
).strip()

ACCOUNTS_FILE = os.getenv(
    "ACCOUNTS_FILE",
    os.path.join(
        BASE_DIR,
        "client_accounts.json"
    )
)

RECONNECT_SECONDS = 3


if not PRIMARY_API_KEY or not PRIMARY_API_SECRET:
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
    "User-Agent": "XAUTUSD-MultiAccount-Bot/1.0"
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
# ACCOUNT FILE
# ============================================================

accounts_lock = threading.RLock()


def load_accounts():

    if not os.path.exists(ACCOUNTS_FILE):
        return {}

    try:

        with open(
            ACCOUNTS_FILE,
            "r"
        ) as f:

            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception as e:

        logging.warning(
            f"ACCOUNT FILE LOAD ERROR | {e}"
        )

    return {}


def save_accounts(accounts):

    tmp = ACCOUNTS_FILE + ".tmp"

    with open(
        tmp,
        "w"
    ) as f:

        json.dump(
            accounts,
            f,
            indent=2
        )

    os.replace(
        tmp,
        ACCOUNTS_FILE
    )


# ============================================================
# DELTA CLIENT
# ============================================================

class DeltaClient:

    def __init__(
        self,
        api_key,
        api_secret
    ):

        self.api_key = api_key
        self.api_secret = api_secret

    def sign(
        self,
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
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp
        }

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

    def product(self):

        return self.api(
            "GET",
            f"/v2/products/{SYMBOL}"
        )["result"]

    def position(
        self,
        product_id
    ):

        data = self.api(
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
            "unrealized_pnl":
                result.get(
                    "unrealized_pnl",
                    0
                )
        }

    def balance(self):

        data = self.api(
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

    def set_leverage(
        self,
        product_id
    ):

        try:

            self.api(
                "POST",
                f"/v2/products/{product_id}/orders/leverage",
                body={
                    "leverage":
                        str(LEVERAGE)
                },
                auth=True
            )

        except Exception as e:

            logging.warning(
                f"LEVERAGE ERROR | {e}"
            )

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

        self.api(
            "POST",
            "/v2/orders",
            body=body,
            auth=True
        )

    def market_entry(
        self,
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
                f"entry_{int(time.time()*1000)}"[-32:]
        }

        return self.api(
            "POST",
            "/v2/orders",
            body=body,
            auth=True
        )

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
# ACCOUNT BOT
# ============================================================

class AccountBot:

    def __init__(
        self,
        account_id,
        name,
        client,
        product_info,
        account_type="client"
    ):

        self.account_id = account_id
        self.name = name
        self.client = client
        self.product = product_info
        self.product_id = int(
            product_info["id"]
        )
        self.account_type = account_type

        self.day = None
        self.high = None
        self.low = None
        self.sl = None

        self.last_position = 0

        self.trade_high = None
        self.trade_low = None

        self.last_price = None

        self.ready = False
        self.bot_enabled = False

        self.stop_reason = (
            "START REQUIRED"
        )

        self.active_trade = None

        self.subscription_start = None
        self.subscription_expiry = None
        self.subscription_fee = 0

        self.trade_history = []

        self.lock = threading.RLock()

        self.load_account_state()

        if self.account_type == "primary":

            self.bot_enabled = False
            self.stop_reason = (
                "START REQUIRED"
            )


    # ========================================================
    # ACCOUNT STATE
    # ========================================================

    def load_account_state(self):

        if self.account_type == "primary":

            self.load_file_state(
                os.path.join(
                    BASE_DIR,
                    "xautusd_state.json"
                ),
                os.path.join(
                    BASE_DIR,
                    "trade_history.json"
                )
            )

            return

        accounts = load_accounts()

        data = accounts.get(
            self.account_id
        )

        if not data:
            return

        self.name = data.get(
            "name",
            self.name
        )

        self.subscription_start = data.get(
            "subscription_start"
        )

        self.subscription_expiry = data.get(
            "subscription_expiry"
        )

        self.subscription_fee = data.get(
            "subscription_fee",
            0
        )

        state = data.get(
            "state",
            {}
        )

        self.day = (
            datetime.fromisoformat(
                state["day"]
            )
            if state.get("day")
            else None
        )

        for attr in (
            "high",
            "low",
            "sl",
            "trade_high",
            "trade_low"
        ):

            value = state.get(attr)

            if value is not None:

                setattr(
                    self,
                    attr,
                    Decimal(str(value))
                )

        self.active_trade = state.get(
            "active_trade"
        )

        history = data.get(
            "trade_history",
            []
        )

        if isinstance(
            history,
            list
        ):

            self.trade_history = history

        self.stop_reason = data.get(
            "stop_reason",
            "START REQUIRED"
        )


    def load_file_state(
        self,
        state_file,
        history_file
    ):

        if os.path.exists(
            state_file
        ):

            try:

                with open(
                    state_file,
                    "r"
                ) as f:

                    state = json.load(f)

                if state.get("day"):

                    self.day = (
                        datetime.fromisoformat(
                            state["day"]
                        )
                    )

                for attr in (
                    "high",
                    "low",
                    "sl",
                    "trade_high",
                    "trade_low"
                ):

                    value = state.get(attr)

                    if value is not None:

                        setattr(
                            self,
                            attr,
                            Decimal(
                                str(value)
                            )
                        )

                self.active_trade = state.get(
                    "active_trade"
                )

            except Exception as e:

                logging.warning(
                    f"PRIMARY STATE ERROR | {e}"
                )

        if os.path.exists(
            history_file
        ):

            try:

                with open(
                    history_file,
                    "r"
                ) as f:

                    data = json.load(f)

                if isinstance(
                    data,
                    list
                ):

                    self.trade_history = data

            except Exception as e:

                logging.warning(
                    f"PRIMARY HISTORY ERROR | {e}"
                )


    def state_dict(self):

        return {

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
                self.active_trade
        }


    def save(self):

        if self.account_type == "primary":

            state_file = os.path.join(
                BASE_DIR,
                "xautusd_state.json"
            )

            history_file = os.path.join(
                BASE_DIR,
                "trade_history.json"
            )

            tmp = state_file + ".tmp"

            with open(
                tmp,
                "w"
            ) as f:

                json.dump(
                    {
                        **self.state_dict(),
                        "bot_enabled":
                            self.bot_enabled,
                        "stop_reason":
                            self.stop_reason
                    },
                    f,
                    indent=2
                )

            os.replace(
                tmp,
                state_file
            )

            with open(
                history_file,
                "w"
            ) as f:

                json.dump(
                    self.trade_history,
                    f,
                    indent=2
                )

            return

        with accounts_lock:

            accounts = load_accounts()

            account = accounts.get(
                self.account_id,
                {}
            )

            account["name"] = self.name
            account["subscription_start"] = (
                self.subscription_start
            )
            account["subscription_expiry"] = (
                self.subscription_expiry
            )
            account["subscription_fee"] = (
                self.subscription_fee
            )
            account["state"] = (
                self.state_dict()
            )
            account["trade_history"] = (
                self.trade_history
            )
            account["bot_enabled"] = (
                self.bot_enabled
            )
            account["stop_reason"] = (
                self.stop_reason
            )

            accounts[
                self.account_id
            ] = account

            save_accounts(
                accounts
            )


    # ========================================================
    # SUBSCRIPTION
    # ========================================================

    def subscription_active(self):

        if self.account_type == "primary":
            return True

        if not self.subscription_expiry:
            return False

        try:

            expiry = datetime.fromisoformat(
                self.subscription_expiry
            )

            return now_ist() < expiry

        except Exception:

            return False


    def subscription_status(self):

        if self.account_type == "primary":

            return {
                "active": True,
                "expired": False,
                "expiry": None,
                "fee": 0
            }

        active = self.subscription_active()

        expired = False

        if self.subscription_expiry:

            try:

                expired = (
                    now_ist()
                    >= datetime.fromisoformat(
                        self.subscription_expiry
                    )
                )

            except Exception:
                expired = True

        return {

            "active": active,

            "expired": expired,

            "expiry":
                self.subscription_expiry,

            "start":
                self.subscription_start,

            "fee":
                self.subscription_fee
        }


    def check_subscription(self):

        if self.account_type == "primary":
            return

        if (
            self.bot_enabled
            and not self.subscription_active()
        ):

            logging.warning(
                f"SUBSCRIPTION EXPIRED | "
                f"{self.name}"
            )

            self.stop_bot(
                reason="SUBSCRIPTION_EXPIRED"
            )


    def configure_subscription(
        self,
        start_date,
        expiry_date,
        fee
    ):

        with self.lock:

            self.subscription_start = (
                start_date
            )

            self.subscription_expiry = (
                expiry_date
            )

            self.subscription_fee = (
                float(fee or 0)
            )

            self.stop_reason = (
                "SUBSCRIPTION READY"
            )

            self.save()

            return {
                "success": True,
                "message":
                    "Subscription saved."
            }


    # ========================================================
    # ORDER SIZE
    # ========================================================

    def order_size(
        self,
        price
    ):

        bal = self.client.balance()

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
                self.product.get(
                    "contract_value"
                )
                or self.product.get(
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
                self.product.get(
                    "lot_size"
                )
                or self.product.get(
                    "order_size_increment"
                )
                or "1"
            )
        )

        minimum = Decimal(
            str(
                self.product.get(
                    "min_order_size"
                )
                or self.product.get(
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

        return size


    # ========================================================
    # START
    # ========================================================

    def start_bot(self):

        with self.lock:

            if (
                self.account_type == "client"
                and not self.subscription_active()
            ):

                self.bot_enabled = False
                self.stop_reason = (
                    "SUBSCRIPTION EXPIRED"
                )

                self.save()

                return {
                    "success": False,
                    "message":
                        "Subscription is not active."
                }

            try:

                pos = self.client.position(
                    self.product_id
                )

            except Exception as e:

                logging.exception(
                    f"START CHECK ERROR | "
                    f"{self.name} | {e}"
                )

                return {
                    "success": False,
                    "message":
                        "Could not verify Delta position."
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
                        "An open position exists. Close it first."
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
                f"BOT STARTED | {self.name}"
            )

            return {
                "success": True,
                "bot_enabled": True,
                "message":
                    f"{self.name} bot started."
            }


    # ========================================================
    # STOP
    # ========================================================

    def stop_bot(
        self,
        reason="MANUAL_STOP"
    ):

        with self.lock:

            self.bot_enabled = False
            self.stop_reason = reason

            self.save()

            try:

                pos = self.client.position(
                    self.product_id
                )

            except Exception as e:

                logging.exception(
                    f"STOP POSITION ERROR | "
                    f"{self.name} | {e}"
                )

                return {
                    "success": False,
                    "bot_enabled": False,
                    "message":
                        "Bot stopped but position check failed."
                }

            size = int(
                pos.get("size", 0)
            )

            if size == 0:

                self.last_position = 0
                self.active_trade = None
                self.sl = None
                self.trade_high = None
                self.trade_low = None

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
                    f"STOP CLOSE ERROR | "
                    f"{self.name} | {e}"
                )

                return {
                    "success": False,
                    "bot_enabled": False,
                    "position_closed": False,
                    "message":
                        "Bot stopped, but position close failed."
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

                return {
                    "success": False,
                    "bot_enabled": False,
                    "position_closed": False,
                    "message":
                        "Bot stopped but position could not be confirmed closed."
                }

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
                reason
            )

            self.last_position = 0
            self.active_trade = None
            self.sl = None
            self.trade_high = None
            self.trade_low = None

            self.save()

            return {
                "success": True,
                "bot_enabled": False,
                "position_closed": True,
                "message":
                    f"{self.name} bot stopped and position closed."
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
        self.sl = None

        self.trade_high = None
        self.trade_low = None

        self.ready = False

        self.bot_enabled = False
        self.stop_reason = (
            "START REQUIRED"
        )

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
            return True

        try:

            pos = self.client.position(
                self.product_id
            )

            self.last_position = (
                pos["size"]
            )

        except Exception:

            self.last_position = 0

        if (
            self.high is not None
            and self.low is not None
        ):

            self.ready = True
            return True

        if now > start + timedelta(
            seconds=5
        ):

            high, low = (
                self.client.historical_high_low(
                    start,
                    now
                )
            )

            if (
                high is not None
                and low is not None
            ):

                self.high = high
                self.low = low

                self.ready = True

                self.save()

                return True

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

        if (
            self.account_type == "client"
            and not self.subscription_active()
        ):

            return False

        if self.last_position != 0:
            return False

        pos = self.client.position(
            self.product_id
        )

        if pos["size"] != 0:

            self.last_position = (
                pos["size"]
            )

            return False

        if direction == "LONG":

            if sl >= price:
                return False

            side = "buy"

        else:

            if sl <= price:
                return False

            side = "sell"

        size = self.order_size(
            price
        )

        self.client.market_entry(
            self.product_id,
            side,
            size,
            sl
        )

        confirmed = False

        for _ in range(50):

            time.sleep(0.2)

            pos = self.client.position(
                self.product_id
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
                f"ENTRY NOT CONFIRMED | {self.name}"
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
            f"ENTRY | {self.name} | "
            f"{direction} | {price} | SL={sl}"
        )

        return True


    # ========================================================
    # PNL
    # ========================================================

    def calculate_pnl(
        self,
        direction,
        entry,
        exit_price,
        size
    ):

        try:

            entry = Decimal(
                str(entry)
            )

            exit_price = Decimal(
                str(exit_price)
            )

            qty = Decimal(
                str(abs(size))
            )

            contract_value = Decimal(
                str(
                    self.product.get(
                        "contract_value"
                    )
                    or self.product.get(
                        "contract_value_usd"
                    )
                    or "1"
                )
            )

            if direction == "LONG":

                return (
                    exit_price - entry
                ) * qty * contract_value

            return (
                entry - exit_price
            ) * qty * contract_value

        except Exception:

            return Decimal("0")


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

        size = (
            self.active_trade.get(
                "size",
                abs(
                    int(
                        self.last_position
                    )
                )
            )
        )

        pnl = self.calculate_pnl(
            direction,
            entry_price,
            exit_price,
            size
        )

        self.trade_history.append({

            "id":
                f"{self.account_id}_{int(time.time()*1000)}",

            "account_id":
                self.account_id,

            "account_name":
                self.name,

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
                float(entry_price),

            "exit_price":
                float(exit_price),

            "size":
                int(size),

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
        })

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

            now = now_ist()

            self.check_subscription()

            if saturday_squareoff(now):

                try:

                    pos = self.client.position(
                        self.product_id
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

                except Exception as e:

                    logging.error(
                        f"SATURDAY CLOSE ERROR | "
                        f"{self.name} | {e}"
                    )

                return

            if weekend(now):
                return

            self.new_day(now)

            if now < strategy_start(
                self.day
            ):
                return

            if not self.prepare(
                now,
                price
            ):
                return

            try:

                pos = self.client.position(
                    self.product_id
                )

            except Exception as e:

                logging.warning(
                    f"POSITION ERROR | "
                    f"{self.name} | {e}"
                )

                return

            size = int(
                pos["size"]
            )

            # ------------------------------------------------
            # CLOSED POSITION
            # ------------------------------------------------

            if (
                size == 0
                and self.last_position != 0
            ):

                old = self.last_position

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

            self.last_position = 0

            if not self.bot_enabled:
                return

            if (
                self.high is not None
                and price > self.high
            ):

                sl = self.low

                if self.enter(
                    "LONG",
                    price,
                    sl
                ):

                    self.high = price
                    self.save()

                return

            if (
                self.low is not None
                and price < self.low
            ):

                sl = self.high

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

ACCOUNT_BOTS = {}
ACCOUNT_MANAGER_LOCK = threading.RLock()


def create_primary():

    product_info = DeltaClient(
        PRIMARY_API_KEY,
        PRIMARY_API_SECRET
    ).product()

    client = DeltaClient(
        PRIMARY_API_KEY,
        PRIMARY_API_SECRET
    )

    client.set_leverage(
        int(product_info["id"])
    )

    return AccountBot(
        "primary",
        "Primary Account",
        client,
        product_info,
        "primary"
    )


PRIMARY_BOT = create_primary()

ACCOUNT_BOTS["primary"] = PRIMARY_BOT


def create_client_account(
    account_id,
    name,
    api_key,
    api_secret,
    start_date,
    expiry_date,
    fee
):

    client = DeltaClient(
        api_key,
        api_secret
    )

    product_info = client.product()

    client.set_leverage(
        int(product_info["id"])
    )

    bot = AccountBot(
        account_id,
        name,
        client,
        product_info,
        "client"
    )

    bot.subscription_start = (
        start_date
    )

    bot.subscription_expiry = (
        expiry_date
    )

    bot.subscription_fee = (
        float(fee or 0)
    )

    bot.save()

    return bot


def load_client_accounts():

    accounts = load_accounts()

    for account_id, data in accounts.items():

        if account_id == "primary":
            continue

        try:

            bot = create_client_account(
                account_id,
                data.get(
                    "name",
                    account_id
                ),
                data.get(
                    "api_key",
                    ""
                ),
                data.get(
                    "api_secret",
                    ""
                ),
                data.get(
                    "subscription_start"
                ),
                data.get(
                    "subscription_expiry"
                ),
                data.get(
                    "subscription_fee",
                    0
                )
            )

            ACCOUNT_BOTS[
                account_id
            ] = bot

        except Exception as e:

            logging.exception(
                f"CLIENT LOAD ERROR | "
                f"{account_id} | {e}"
            )


load_client_accounts()


# ============================================================
# DASHBOARD HELPERS
# ============================================================

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


def statistics(
    history
):

    today = now_ist().strftime(
        "%Y-%m-%d"
    )

    today_trades = [
        t for t in history
        if t.get("date") == today
    ]

    def pnl(items):

        return round(
            sum(
                float(
                    t.get(
                        "pnl",
                        0
                    )
                )
                for t in items
            ),
            2
        )

    def winning(items):

        return sum(
            1
            for t in items
            if float(
                t.get(
                    "pnl",
                    0
                )
            ) > 0
        )

    def losing(items):

        return sum(
            1
            for t in items
            if float(
                t.get(
                    "pnl",
                    0
                )
            ) < 0
        )

    all_count = len(history)
    today_count = len(today_trades)

    all_win = winning(history)
    today_win = winning(today_trades)

    return {

        "today": {

            "total_trades":
                today_count,

            "winning_trades":
                today_win,

            "losing_trades":
                losing(today_trades),

            "win_rate":
                round(
                    today_win
                    / today_count
                    * 100,
                    1
                )
                if today_count
                else 0,

            "pnl":
                pnl(today_trades)
        },

        "all_time": {

            "total_trades":
                all_count,

            "winning_trades":
                all_win,

            "losing_trades":
                losing(history),

            "win_rate":
                round(
                    all_win
                    / all_count
                    * 100,
                    1
                )
                if all_count
                else 0,

            "pnl":
                pnl(history)
        }
    }


def account_dashboard(
    bot
):

    try:

        pos = bot.client.position(
            bot.product_id
        )

    except Exception:

        pos = {
            "size": 0,
            "entry": None,
            "stop_loss": None,
            "unrealized_pnl": 0
        }

    try:

        bal = bot.client.balance()

    except Exception:

        bal = None

    size = int(
        pos.get("size", 0)
    )

    direction = (
        "LONG"
        if size > 0
        else
        "SHORT"
        if size < 0
        else
        "FLAT"
    )

    history = list(
        reversed(
            bot.trade_history
        )
    )

    subscription = (
        bot.subscription_status()
    )

    return {

        "account_id":
            bot.account_id,

        "account_name":
            bot.name,

        "account_type":
            bot.account_type,

        "symbol":
            SYMBOL,

        "bot_running":
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
                bal
            ),

        "position": {

            "direction":
                direction,

            "size":
                abs(size),

            "entry_price":
                decimal_json(
                    pos.get(
                        "entry"
                    )
                ),

            "stop_loss":
                decimal_json(
                    pos.get(
                        "stop_loss"
                    )
                    or bot.sl
                ),

            "unrealized_pnl":
                decimal_json(
                    pos.get(
                        "unrealized_pnl",
                        0
                    )
                )
        },

        "statistics":
            statistics(
                bot.trade_history
            ),

        "trade_history":
            history,

        "history_count":
            len(
                bot.trade_history
            ),

        "subscription":
            subscription,

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
                )
                if bot.day
                else None,

            "ready":
                bot.ready
        }
    }


def all_dashboard_data():

    return {
        "success": True,
        "accounts": [
            account_dashboard(bot)
            for bot in ACCOUNT_BOTS.values()
        ]
    }


# ============================================================
# HTTP HANDLER
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


    def authorized(self):

        if not ADMIN_PIN:
            return True

        return (
            self.headers.get(
                "X-Admin-Pin",
                ""
            ).strip()
            == ADMIN_PIN
        )


    def do_GET(self):

        if self.path == "/api/dashboard":

            self.send_json(
                all_dashboard_data()
            )

            return

        if self.path == "/api/health":

            self.send_json({
                "success": True,
                "bot_running": True,
                "accounts":
                    len(ACCOUNT_BOTS)
            })

            return

        if self.path == "/":

            self.path = "/index.html"

        return super().do_GET()


    def read_json_body(self):

        length = int(
            self.headers.get(
                "Content-Length",
                "0"
            )
        )

        if length <= 0:
            return {}

        raw = self.rfile.read(
            length
        )

        try:

            return json.loads(
                raw.decode("utf-8")
            )

        except Exception:

            return {}


    def do_POST(self):

        if not self.authorized():

            self.send_json(
                {
                    "success": False,
                    "message":
                        "Unauthorized."
                },
                401
            )

            return

        body = self.read_json_body()


        # ----------------------------------------------------
        # START
        # ----------------------------------------------------

        if self.path == "/api/bot/start":

            account_id = body.get(
                "account_id",
                "primary"
            )

            bot = ACCOUNT_BOTS.get(
                account_id
            )

            if not bot:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Account not found."
                    },
                    404
                )

                return

            try:

                result = bot.start_bot()

                self.send_json(
                    result,
                    200
                    if result.get("success")
                    else 409
                )

            except Exception as e:

                logging.exception(e)

                self.send_json(
                    {
                        "success": False,
                        "message":
                            str(e)
                    },
                    500
                )

            return


        # ----------------------------------------------------
        # STOP
        # ----------------------------------------------------

        if self.path == "/api/bot/stop":

            account_id = body.get(
                "account_id",
                "primary"
            )

            bot = ACCOUNT_BOTS.get(
                account_id
            )

            if not bot:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Account not found."
                    },
                    404
                )

                return

            try:

                result = bot.stop_bot()

                self.send_json(
                    result,
                    200
                    if result.get("success")
                    else 500
                )

            except Exception as e:

                logging.exception(e)

                self.send_json(
                    {
                        "success": False,
                        "message":
                            str(e)
                    },
                    500
                )

            return


        # ----------------------------------------------------
        # ADD CLIENT
        # ----------------------------------------------------

        if self.path == "/api/client/add":

            name = str(
                body.get(
                    "name",
                    ""
                )
            ).strip()

            api_key = str(
                body.get(
                    "api_key",
                    ""
                )
            ).strip()

            api_secret = str(
                body.get(
                    "api_secret",
                    ""
                )
            ).strip()

            start_date = body.get(
                "subscription_start"
            )

            expiry_date = body.get(
                "subscription_expiry"
            )

            fee = body.get(
                "subscription_fee",
                0
            )

            if not name:
                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Client name required."
                    },
                    400
                )
                return

            if not api_key or not api_secret:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Delta API key and secret required."
                    },
                    400
                )

                return

            account_id = (
                "client_"
                + str(
                    int(
                        time.time() * 1000
                    )
                )
            )

            try:

                bot = create_client_account(
                    account_id,
                    name,
                    api_key,
                    api_secret,
                    start_date,
                    expiry_date,
                    fee
                )

                with accounts_lock:

                    accounts = load_accounts()

                    accounts[
                        account_id
                    ] = {

                        "name":
                            name,

                        "api_key":
                            api_key,

                        "api_secret":
                            api_secret,

                        "subscription_start":
                            start_date,

                        "subscription_expiry":
                            expiry_date,

                        "subscription_fee":
                            float(fee or 0),

                        "state":
                            bot.state_dict(),

                        "trade_history":
                            bot.trade_history,

                        "bot_enabled":
                            False,

                        "stop_reason":
                            "SUBSCRIPTION READY"
                    }

                    save_accounts(
                        accounts
                    )

                ACCOUNT_BOTS[
                    account_id
                ] = bot

                self.send_json({
                    "success": True,
                    "account_id":
                        account_id,
                    "message":
                        "Client account added successfully."
                })

            except Exception as e:

                logging.exception(e)

                self.send_json(
                    {
                        "success": False,
                        "message":
                            f"Could not add Delta account: {e}"
                    },
                    500
                )

            return


        # ----------------------------------------------------
        # SUBSCRIPTION
        # ----------------------------------------------------

        if self.path == "/api/client/subscription":

            account_id = body.get(
                "account_id"
            )

            bot = ACCOUNT_BOTS.get(
                account_id
            )

            if not bot:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Client not found."
                    },
                    404
                )

                return

            result = (
                bot.configure_subscription(
                    body.get(
                        "subscription_start"
                    ),
                    body.get(
                        "subscription_expiry"
                    ),
                    body.get(
                        "subscription_fee",
                        0
                    )
                )
            )

            self.send_json(
                result
            )

            return


        # ----------------------------------------------------
        # START CLIENT SUBSCRIPTION
        # ----------------------------------------------------

        if self.path == "/api/client/start":

            account_id = body.get(
                "account_id"
            )

            bot = ACCOUNT_BOTS.get(
                account_id
            )

            if not bot:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Client not found."
                    },
                    404
                )

                return

            result = bot.start_bot()

            self.send_json(
                result,
                200
                if result.get("success")
                else 409
            )

            return


        # ----------------------------------------------------
        # DELETE CLIENT
        # ----------------------------------------------------

        if self.path == "/api/client/delete":

            account_id = body.get(
                "account_id"
            )

            if account_id == "primary":

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Primary account cannot be deleted."
                    },
                    400
                )

                return

            bot = ACCOUNT_BOTS.get(
                account_id
            )

            if not bot:

                self.send_json(
                    {
                        "success": False,
                        "message":
                            "Client not found."
                    },
                    404
                )

                return

            try:

                bot.stop_bot(
                    "CLIENT_DELETED"
                )

            except Exception:
                pass

            ACCOUNT_BOTS.pop(
                account_id,
                None
            )

            with accounts_lock:

                accounts = load_accounts()

                accounts.pop(
                    account_id,
                    None
                )

                save_accounts(
                    accounts
                )

            self.send_json({
                "success": True,
                "message":
                    "Client removed."
            })

            return


        self.send_json(
            {
                "success": False,
                "message":
                    "Unknown endpoint."
            },
            404
        )


    def send_json(
        self,
        data,
        status=200
    ):

        raw = json.dumps(
            data,
            separators=(",", ":")
        ).encode("utf-8")

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


# ============================================================
# DASHBOARD SERVER
# ============================================================

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
                f"DASHBOARD STARTED | "
                f"PORT={DASHBOARD_PORT}"
            )

            server.serve_forever()

        except Exception as e:

            logging.exception(
                f"DASHBOARD ERROR | {e}"
            )

    threading.Thread(
        target=server_thread,
        daemon=True
    ).start()


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

                payload = {

                    "type":
                        "subscribe",

                    "payload": {

                        "channels": [

                            {

                                "name":
                                    "trades",

                                "symbols":
                                    [SYMBOL]
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
                    ) != "trades":

                        return

                    symbol = (
                        data.get("sy")
                        or
                        data.get("symbol")
                    )

                    price = data.get("p")

                    if (
                        symbol != SYMBOL
                        or price is None
                    ):
                        return

                    for bot in list(
                        ACCOUNT_BOTS.values()
                    ):

                        try:

                            bot.price_tick(
                                price
                            )

                        except Exception as e:

                            logging.exception(
                                f"ACCOUNT TICK ERROR | "
                                f"{bot.name} | {e}"
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
        f"ACCOUNTS = {len(ACCOUNT_BOTS)}"
    )

    logging.warning(
        "PRIMARY + CLIENT ACCOUNTS ENABLED"
    )

    logging.warning(
        "SUBSCRIPTION EXPIRY ENABLED"
    )

    logging.warning(
        "============================================"
    )

    try:

        start_dashboard()

        run_websocket()

    except KeyboardInterrupt:

        logging.warning(
            "BOT STOPPED"
        )

    except Exception as e:

        logging.exception(
            f"FATAL ERROR | {e}"
        )
