import os
import time
import json
import hmac
import hashlib
import logging
import threading
import uuid
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, parse_qs, urlparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import requests
import websocket
from dotenv import load_dotenv


# =====================================================================
# DELTA PRO AUTOTRADER - DUAL ASSET
#
# XAUTUSD + BTCUSD
#
# SIMPLE STRATEGY
#
# SESSION:
#   05:30 IST = New session starts
#   05:45 IST = Trading starts
#
# BASE ENTRY:
#   Current running session HIGH break -> LONG
#   Current running session LOW  break -> SHORT
#
# BASE SL:
#   LONG  -> previous completed 5M candle LOW
#   SHORT -> previous completed 5M candle HIGH
#
# TRAILING:
#   Every new completed 5M candle:
#       LONG  -> previous completed 5M LOW
#       SHORT -> previous completed 5M HIGH
#
# BASE SL:
#   LONG SL  -> ONE SHORT REVERSAL
#   SHORT SL -> ONE LONG REVERSAL
#
# REVERSAL:
#   Same 5M trailing SL logic
#
# REVERSAL SL:
#   FLAT
#   NO SECOND REVERSAL
#   Wait for CURRENT session HIGH / LOW to break again
#
# WEEKEND:
#   Saturday / Sunday = NO TRADING
#   Existing exchange position is closed
#
# LEVERAGE:
#   XAUTUSD = 100,90,80,...10
#   BTCUSD  = 200,190,180,...10
#
# MARGIN:
#   10% of available balance
#
# EXECUTION SAFETY:
#   One order at a time
#   Entry confirmation required
#   Close confirmation required
#   Unknown execution blocks further orders
#   Exchange state is authoritative
#
# DASHBOARD:
#   Read-only position display
#   Dashboard never modifies strategy state
#
# =====================================================================


load_dotenv()


# =====================================================================
# CONSTANTS
# =====================================================================

IST = ZoneInfo("Asia/Kolkata")

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

PERSISTENT_DATA_DIR = os.getenv(
    "RAILWAY_VOLUME_MOUNT_PATH",
    BASE_DIR
)

BASE_URL = os.getenv(
    "DELTA_BASE_URL",
    "https://api.india.delta.exchange"
).rstrip("/")

WS_URL = os.getenv(
    "DELTA_PUBLIC_WS_URL",
    "wss://public-socket.india.delta.exchange"
)

DASHBOARD_PORT = int(
    os.getenv(
        "DASHBOARD_PORT",
        "8000"
    )
)

SESSION_START_TIME = dtime(5, 30)

TRADING_START_TIME = dtime(5, 45)

RECONNECT_SECONDS = 5

ENTRY_CONFIRM_TIMEOUT = 10.0

CLOSE_CONFIRM_TIMEOUT = 10.0

POSITION_POLL_INTERVAL = 0.25

EXECUTION_UNKNOWN_RECHECK_INTERVAL = 5.0


# =====================================================================
# FILES
# =====================================================================

STATE_DIR = os.path.join(
    PERSISTENT_DATA_DIR,
    "account_states"
)

HISTORY_DIR = os.path.join(
    PERSISTENT_DATA_DIR,
    "account_history"
)

CLIENTS_FILE = os.path.join(
    PERSISTENT_DATA_DIR,
    "clients_config.json"
)


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


os.makedirs(
    STATE_DIR,
    exist_ok=True
)

os.makedirs(
    HISTORY_DIR,
    exist_ok=True
)


# =====================================================================
# LOGGING
# =====================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True,
)


CACHED_SERVER_IP = "Detecting..."


# =====================================================================
# GENERAL HELPERS
# =====================================================================

def update_server_ip():

    global CACHED_SERVER_IP

    try:

        res = requests.get(
            "https://api.ipify.org?format=json",
            timeout=5
        )

        ip = res.json().get("ip")

        if ip:

            CACHED_SERVER_IP = ip

            logging.warning(
                "=================================================="
            )

            logging.warning(
                f" RAILWAY OUTBOUND IP --> {ip}"
            )

            logging.warning(
                " WHITELIST THIS IP IN DELTA EXCHANGE API SETTINGS"
            )

            logging.warning(
                "=================================================="
            )

    except Exception as e:

        logging.warning(
            f"IP FETCH ERROR | {e}"
        )


def now_ist():

    return datetime.now(IST)


def is_weekend(
    symbol=None,
    dt=None
):

    dt = dt or now_ist()

    weekday = dt.weekday()

    current_time = dt.time()


    # Saturday after 05:30 session boundary
    if (
        weekday == 5
        and current_time >= SESSION_START_TIME
    ):

        return True


    # Entire Sunday
    if weekday == 6:

        return True


    # Monday before 05:30
    if (
        weekday == 0
        and current_time < SESSION_START_TIME
    ):

        return True


    return False


def get_current_session_start(
    dt=None
):

    dt = dt or now_ist()

    session_time = dt.replace(
        hour=5,
        minute=30,
        second=0,
        microsecond=0
    )


    if dt.time() >= SESSION_START_TIME:

        return session_time


    return session_time - timedelta(
        days=1
    )


def safe_filename(value):

    result = ""

    for char in str(value):

        if char.isalnum() or char in (
            "-",
            "_"
        ):

            result += char

        else:

            result += "_"


    return result or "account"


def account_state_file(
    unique_id
):

    return os.path.join(
        STATE_DIR,
        safe_filename(unique_id) + ".json"
    )


def account_history_file(
    unique_id
):

    return os.path.join(
        HISTORY_DIR,
        safe_filename(unique_id) + ".json"
    )


def atomic_write_json(
    filename,
    data
):

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


    os.replace(
        tmp,
        filename
    )


def load_clients_config():

    if not os.path.exists(
        CLIENTS_FILE
    ):

        return {}


    try:

        with open(
            CLIENTS_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)


        return (
            data
            if isinstance(
                data,
                dict
            )
            else {}
        )


    except Exception as e:

        logging.warning(
            f"Client config read error: {e}"
        )

        return {}


def save_clients_config(
    cfg
):

    atomic_write_json(
        CLIENTS_FILE,
        cfg
    )


# =====================================================================
# DELTA CLIENT
# =====================================================================

class DeltaClient:

    def __init__(
        self,
        api_key,
        api_secret,
        account_name,
        symbol
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

        self.symbol = (
            symbol or ""
        ).strip().upper()


        self.session = (
            requests.Session()
        )


        self.session.headers.update({

            "Accept":
                "application/json",

            "Content-Type":
                "application/json",

            "User-Agent":
                "MultiBot/99.0",
        })


    # -----------------------------------------------------------------
    # SIGN
    # -----------------------------------------------------------------

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
            hashlib.sha256,
        ).hexdigest()


        return {

            "api-key":
                self.api_key,

            "signature":
                signature,

            "timestamp":
                timestamp,

            "User-Agent":
                "MultiBot/99.0",
        }


    # -----------------------------------------------------------------
    # API
    # -----------------------------------------------------------------

    def api(
        self,
        method,
        path,
        params=None,
        body=None,
        auth=False
    ):

        params = params or {}


        body_text = (

            json.dumps(
                body,
                separators=(
                    ",",
                    ":"
                )
            )

            if body is not None

            else ""
        )


        query = (

            "?"
            + urlencode(
                params,
                doseq=True
            )

            if params

            else ""
        )


        headers = (

            self.sign(
                method,
                path,
                query,
                body_text
            )

            if auth

            else {}
        )


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

            timeout=(
                3,
                8
            ),
        )


        response.raise_for_status()


        data = response.json()


        if data.get(
            "success"
        ) is False:

            raise RuntimeError(
                f"Delta error: {data}"
            )


        return data


    # -----------------------------------------------------------------
    # PRODUCT
    # -----------------------------------------------------------------

    def product(self):

        data = self.api(
            "GET",
            f"/v2/products/{self.symbol}"
        )


        result = data.get(
            "result"
        )


        if not isinstance(
            result,
            dict
        ):

            raise RuntimeError(
                f"Invalid product response: {data}"
            )


        return result


    # -----------------------------------------------------------------
    # SESSION HIGH / LOW
    # -----------------------------------------------------------------

    def get_session_high_low(
        self,
        session_start_dt
    ):

        try:

            start_ts = int(
                session_start_dt.timestamp()
            )

            end_ts = int(
                now_ist().timestamp()
            )


            if end_ts <= start_ts:

                return None, None


            data = self.api(

                "GET",

                "/v2/history/candles",

                params={

                    "resolution":
                        "1m",

                    "symbol":
                        self.symbol,

                    "start":
                        start_ts,

                    "end":
                        end_ts,
                },
            )


            candles = data.get(
                "result",
                []
            )


            if (
                not isinstance(
                    candles,
                    list
                )
                or not candles
            ):

                return None, None


            highest = None

            lowest = None


            for candle in candles:

                try:

                    if isinstance(
                        candle,
                        dict
                    ):

                        ts_raw = (
                            candle.get("time")
                            or candle.get("timestamp")
                            or candle.get("start")
                        )

                        high_raw = (
                            candle.get("high")
                        )

                        low_raw = (
                            candle.get("low")
                        )


                    elif (
                        isinstance(
                            candle,
                            list
                        )
                        and len(candle) >= 4
                    ):

                        ts_raw = candle[0]

                        high_raw = candle[2]

                        low_raw = candle[3]


                    else:

                        continue


                    if ts_raw is not None:

                        ts = float(
                            ts_raw
                        )


                        if (
                            ts
                            > 100000000000
                        ):

                            ts /= 1000.0


                        if (
                            ts < start_ts
                            or ts > end_ts
                        ):

                            continue


                    high = Decimal(
                        str(high_raw)
                    )

                    low = Decimal(
                        str(low_raw)
                    )


                    if (
                        high > 0
                        and (
                            highest is None
                            or high > highest
                        )
                    ):

                        highest = high


                    if (
                        low > 0
                        and (
                            lowest is None
                            or low < lowest
                        )
                    ):

                        lowest = low


                except Exception:

                    continue


            return (
                highest,
                lowest
            )


        except Exception as e:

            logging.warning(
                f"[{self.symbol}] "
                f"Session high/low error: {e}"
            )

            return None, None


    # -----------------------------------------------------------------
    # POSITION
    # -----------------------------------------------------------------

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

            auth=True,
        )


        result = data.get(
            "result",
            {}
        )


        pos_item = {}


        if isinstance(
            result,
            dict
        ):

            pos_item = result


        elif isinstance(
            result,
            list
        ):

            for p in result:

                if (
                    isinstance(
                        p,
                        dict
                    )
                    and int(
                        p.get(
                            "product_id",
                            0
                        )
                        or 0
                    )
                    == int(product_id)
                ):

                    pos_item = p

                    break


            if (
                not pos_item
                and result
                and isinstance(
                    result[0],
                    dict
                )
            ):

                pos_item = result[0]


        if not pos_item:

            return {

                "size":
                    0,

                "entry_price":
                    None,

                "stop_loss":
                    None,

                "liquidation_price":
                    None,

                "bankruptcy_price":
                    None,

                "margin":
                    None,

                "mark_price":
                    None,

                "unrealized_pnl":
                    0,

                "leverage":
                    None,
            }


        raw_entry = (

            pos_item.get(
                "entry_price"
            )

            or pos_item.get(
                "entry"
            )

            or pos_item.get(
                "avg_price"
            )

            or pos_item.get(
                "average_price"
            )

            or pos_item.get(
                "price"
            )
        )


        try:

            entry_val = (

                float(
                    raw_entry
                )

                if (
                    raw_entry is not None
                    and float(raw_entry) > 0
                )

                else None
            )


        except Exception:

            entry_val = None


        lev_val = (

            pos_item.get(
                "leverage"
            )

            or pos_item.get(
                "user_leverage"
            )

            or pos_item.get(
                "effective_leverage"
            )
        )


        def fval(
            key
        ):

            try:

                value = pos_item.get(
                    key
                )

                return (

                    float(value)

                    if value is not None

                    else None
                )

            except Exception:

                return None


        try:

            raw_size = int(

                pos_item.get(
                    "size",
                    0
                )
                or 0
            )


        except Exception:

            raw_size = 0


        try:

            unrealized = float(

                pos_item.get(
                    "unrealized_pnl",
                    0
                )
                or 0
            )


        except Exception:

            unrealized = 0.0


        try:

            leverage = (

                int(lev_val)

                if lev_val

                else None
            )


        except Exception:

            leverage = None


        return {

            "size":
                raw_size,

            "entry_price":
                entry_val,

            "stop_loss":
                fval("stop_loss"),

            "liquidation_price":
                fval("liquidation_price"),

            "bankruptcy_price":
                fval("bankruptcy_price"),

            "margin":
                fval("margin"),

            "mark_price":
                fval("mark_price"),

            "unrealized_pnl":
                unrealized,

            "leverage":
                leverage,
        }


    # -----------------------------------------------------------------
    # BALANCE
    # -----------------------------------------------------------------

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


        if isinstance(
            result,
            dict
        ):

            result = [result]


        for wallet in result:

            if not isinstance(
                wallet,
                dict
            ):

                continue


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


    # -----------------------------------------------------------------
    # LEVERAGE
    # -----------------------------------------------------------------

    def set_leverage(
        self,
        product_id,
        leverage_val
    ):

        self.api(

            "POST",

            f"/v2/products/{product_id}/orders/leverage",

            body={

                "leverage":
                    str(leverage_val)
            },

            auth=True,
        )


    # -----------------------------------------------------------------
    # ORDER SIZE
    # -----------------------------------------------------------------

    def order_size(
        self,
        product_info,
        price,
        leverage,
        balance_fraction
    ):

        balance = self.balance()


        margin = (
            balance
            * balance_fraction
        )


        notional = (
            margin
            * leverage
        )


        contract_value = Decimal(

            str(

                product_info.get(
                    "contract_value"
                )

                or product_info.get(
                    "contract_value_usd"
                )

                or "0.001"
            )
        )


        if contract_value <= 0:

            contract_value = Decimal(
                "0.001"
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


        if increment <= 0:

            increment = Decimal("1")


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


    # -----------------------------------------------------------------
    # CANCEL ORDERS
    # -----------------------------------------------------------------

    def cancel_all_orders(
        self,
        product_id
    ):

        try:

            return self.api(

                "DELETE",

                "/v2/orders/all",

                body={

                    "product_id":
                        int(product_id)
                },

                auth=True,
            )


        except Exception as e:

            logging.warning(
                f"[{self.symbol}] "
                f"Cancel all orders failed: {e}"
            )

            return None


    # -----------------------------------------------------------------
    # UNIQUE CLIENT ORDER ID
    # -----------------------------------------------------------------

    def make_client_order_id(
        self,
        prefix
    ):

        return (

            f"{prefix}_"
            f"{int(time.time() * 1000)}_"
            f"{uuid.uuid4().hex[:10]}"

        )[-32:]


    # -----------------------------------------------------------------
    # MARKET ENTRY
    # -----------------------------------------------------------------

    def market_entry_pure(
        self,
        product_id,
        side,
        size
    ):

        body = {

            "product_id":
                int(product_id),

            "product_symbol":
                self.symbol,

            "size":
                int(abs(size)),

            "side":
                side,

            "order_type":
                "market_order",

            "client_order_id":
                self.make_client_order_id(
                    "entry"
                ),
        }


        return self.api(

            "POST",

            "/v2/orders",

            body=body,

            auth=True
        )


    # -----------------------------------------------------------------
    # MARKET CLOSE
    # -----------------------------------------------------------------

    def close_position(
        self,
        product_id,
        size
    ):

        if size == 0:

            return None


        self.cancel_all_orders(
            product_id
        )


        side = (

            "sell"

            if size > 0

            else "buy"
        )


        body = {

            "product_id":
                int(product_id),

            "product_symbol":
                self.symbol,

            "size":
                abs(int(size)),

            "side":
                side,

            "order_type":
                "market_order",

            "reduce_only":
                True,

            "client_order_id":
                self.make_client_order_id(
                    "close"
                ),
        }


        return self.api(

            "POST",

            "/v2/orders",

            body=body,

            auth=True
        )


    # -----------------------------------------------------------------
    # LAST PRICE
    # -----------------------------------------------------------------

    def last_traded_price(self):

        try:

            data = self.api(

                "GET",

                f"/v2/tickers/{self.symbol}"
            )


            result = data.get(
                "result"
            )


            if isinstance(
                result,
                dict
            ):

                price = (

                    result.get(
                        "close"
                    )

                    or result.get(
                        "spot_price"
                    )

                    or result.get(
                        "ltp"
                    )
                )


                if price is not None:

                    return Decimal(
                        str(price)
                    )


        except Exception:

            pass


        return None


# =====================================================================
# HISTORY
# =====================================================================

def load_trade_history(
    unique_id
):

    filename = account_history_file(
        unique_id
    )


    if not os.path.exists(
        filename
    ):

        return []


    try:

        with open(
            filename,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)


        return (

            data

            if isinstance(
                data,
                list
            )

            else []
        )


    except Exception:

        return []


def save_trade_history(
    unique_id,
    history
):

    atomic_write_json(

        account_history_file(
            unique_id
        ),

        history
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

        exit_val = Decimal(
            str(exit_price)
        )

        qty = Decimal(
            str(abs(size))
        )


        contract_value = Decimal(

            str(

                product_info.get(
                    "contract_value"
                )

                or product_info.get(
                    "contract_value_usd"
                )

                or "0.001"
            )
        )


        if contract_value <= 0:

            contract_value = Decimal(
                "0.001"
            )


        if direction == "LONG":

            return (

                (
                    exit_val
                    - entry
                )
                * qty
                * contract_value
            )


        return (

            (
                entry
                - exit_val
            )
            * qty
            * contract_value
        )


    except Exception:

        return Decimal("0")


def calculate_statistics(
    history
):

    def compute_stats(
        trades
    ):

        total = len(
            trades
        )


        wins = [

            t

            for t in trades

            if float(
                t.get(
                    "pnl",
                    0
                )
                or 0
            ) > 0
        ]


        losses = [

            t

            for t in trades

            if float(
                t.get(
                    "pnl",
                    0
                )
                or 0
            ) < 0
        ]


        pnl = sum(

            Decimal(
                str(
                    t.get(
                        "pnl",
                        0
                    )
                    or 0
                )
            )

            for t in trades
        )


        return {

            "total_trades":
                total,

            "winning_trades":
                len(wins),

            "losing_trades":
                len(losses),

            "win_rate":
                (
                    len(wins)
                    / total
                    * 100
                )
                if total
                else 0.0,

            "pnl":
                float(pnl),
        }


    today = now_ist().strftime(
        "%Y-%m-%d"
    )


    today_trades = [

        trade

        for trade in history

        if str(
            trade.get(
                "date",
                ""
            )
        ).startswith(
            today
        )
    ]


    return {

        "today":
            compute_stats(
                today_trades
            ),

        "all_time":
            compute_stats(
                history
            ),
    }


# =====================================================================
# STRATEGY BOT
# =====================================================================

class BreakoutSARBot:

    def __init__(
        self,
        account_id,
        account_name,
        account_type,
        api_key,
        api_secret,
        symbol="XAUTUSD",
        subscription=None,
    ):

        self.base_account_id = (
            account_id
        )


        self.symbol = (
            symbol
            .strip()
            .upper()
        )


        self.strategy_key = (

            f"breakout_sar_"
            f"{self.symbol.lower()}"
        )


        self.unique_id = (

            f"{account_id}_"
            f"{self.symbol}_"
            f"{self.strategy_key}"
        )


        self.account_name = (

            f"{account_name} "
            f"[{self.symbol}: "
            f"Breakout + Reversal]"
        )


        self.account_type = (
            account_type
        )


        self.subscription = (
            subscription or {}
        )


        self.client = DeltaClient(

            api_key,
            api_secret,
            account_name,
            self.symbol
        )


        self.product = None

        self.product_id = 0


        # =============================================================
        # SESSION
        # =============================================================

        self.session_start = None

        self.day_high = None

        self.day_low = None


        # =============================================================
        # MARKET PRICE
        # =============================================================

        self.last_price = None

        self.prev_price = None

        self.last_strategy_price = None


        # =============================================================
        # BOT STATE
        # =============================================================

        self.ready = False

        self.trading_armed = False

        self.bot_enabled = False

        self.stop_reason = None

        self.manual_squareoff_flag = False


        # =============================================================
        # POSITION
        # =============================================================

        self.position = None

        self.stop_loss = 0.0

        self.entry_price = None

        self.size = 0

        self.is_reversal_position = False


        # =============================================================
        # BREAKOUT STATE
        #
        # True  = flat and allowed to wait for HIGH/LOW break
        # False = already entered a trade
        # =============================================================

        self.base_breakout_ready = True


        # =============================================================
        # 5M TRAILING
        # =============================================================

        self.last_checked_candle_time = 0


        # =============================================================
        # EXECUTION SAFETY
        # =============================================================

        self.lock = threading.RLock()

        self.order_in_progress = False

        self.execution_uncertain = False

        self.execution_unknown_since = None

        self.last_reconciliation_time = 0.0

        self.last_execution_time = 0.0


        # =============================================================
        # DEFAULT SETTINGS
        # =============================================================

        self.leverage = (

            Decimal("200")

            if "BTC" in self.symbol

            else Decimal("100")
        )


        # EXACTLY 10% MARGIN

        self.balance_fraction = (
            Decimal("0.10")
        )


        self.load_state()

        self.save()


    # -----------------------------------------------------------------
    # EXPIRY
    # -----------------------------------------------------------------

    def is_expired(self):

        if self.account_type == "primary":

            return False


        expiry = self.subscription.get(
            "expiry"
        )


        if not expiry:

            return False


        try:

            return (

                now_ist().date()

                >

                datetime.strptime(
                    expiry,
                    "%Y-%m-%d"
                ).date()
            )


        except Exception:

            return False


    # -----------------------------------------------------------------
    # LOAD STATE
    # -----------------------------------------------------------------

    def load_state(self):

        filename = account_state_file(
            self.unique_id
        )


        if not os.path.exists(
            filename
        ):

            return


        try:

            with open(
                filename,
                "r",
                encoding="utf-8"
            ) as f:

                state = json.load(f)


            if state.get(
                "session_start"
            ):

                self.session_start = (
                    datetime.fromisoformat(
                        state[
                            "session_start"
                        ]
                    )
                )


            if state.get(
                "day_high"
            ) is not None:

                self.day_high = Decimal(
                    str(
                        state[
                            "day_high"
                        ]
                    )
                )


            if state.get(
                "day_low"
            ) is not None:

                self.day_low = Decimal(
                    str(
                        state[
                            "day_low"
                        ]
                    )
                )


            self.position = (
                state.get(
                    "position"
                )
            )


            self.stop_loss = float(
                state.get(
                    "stop_loss",
                    0
                )
                or 0
            )


            self.entry_price = (
                state.get(
                    "entry_price"
                )
            )


            self.size = int(
                state.get(
                    "size",
                    0
                )
                or 0
            )


            if state.get(
                "leverage"
            ) is not None:

                self.leverage = Decimal(
                    str(
                        state[
                            "leverage"
                        ]
                    )
                )


            if state.get(
                "balance_fraction"
            ) is not None:

                self.balance_fraction = Decimal(
                    str(
                        state[
                            "balance_fraction"
                        ]
                    )
                )


            self.bot_enabled = bool(
                state.get(
                    "bot_enabled",
                    False
                )
            )


            self.stop_reason = (
                state.get(
                    "stop_reason"
                )
            )


            self.ready = bool(
                state.get(
                    "ready",
                    False
                )
            )


            self.trading_armed = bool(
                state.get(
                    "trading_armed",
                    False
                )
            )


            self.is_reversal_position = bool(
                state.get(
                    "is_reversal_position",
                    False
                )
            )


            self.base_breakout_ready = bool(
                state.get(
                    "base_breakout_ready",
                    True
                )
            )


            # ---------------------------------------------------------
            # Local FLAT is never trusted over exchange.
            # ---------------------------------------------------------

            if (
                not self.position
                or self.size <= 0
            ):

                self.position = None

                self.size = 0

                self.entry_price = None

                self.is_reversal_position = False


        except Exception as e:

            logging.warning(
                f"[{self.symbol}] "
                f"State load error: {e}"
            )


    # -----------------------------------------------------------------
    # SAVE STATE
    # -----------------------------------------------------------------

    def save(self):

        data = {

            "account_id":
                self.unique_id,

            "account_name":
                self.account_name,

            "symbol":
                self.symbol,

            "session_start":
                (
                    self.session_start.isoformat()
                    if self.session_start
                    else None
                ),

            "day_high":
                (
                    str(self.day_high)
                    if self.day_high is not None
                    else None
                ),

            "day_low":
                (
                    str(self.day_low)
                    if self.day_low is not None
                    else None
                ),

            "position":
                self.position,

            "stop_loss":
                self.stop_loss,

            "entry_price":
                self.entry_price,

            "size":
                self.size,

            "leverage":
                int(self.leverage),

            "balance_fraction":
                float(
                    self.balance_fraction
                ),

            "bot_enabled":
                self.bot_enabled,

            "stop_reason":
                self.stop_reason,

            "ready":
                self.ready,

            "trading_armed":
                self.trading_armed,

            "is_reversal_position":
                self.is_reversal_position,

            "base_breakout_ready":
                self.base_breakout_ready,

            "execution_uncertain":
                self.execution_uncertain,
        }


        atomic_write_json(

            account_state_file(
                self.unique_id
            ),

            data
        )


    # -----------------------------------------------------------------
    # PRODUCT PREPARE
    # -----------------------------------------------------------------

    def prepare_product(self):

        if self.product_id:

            return True


        try:

            self.product = (
                self.client.product()
            )


            self.product_id = int(
                self.product["id"]
            )


            return True


        except Exception as e:

            logging.error(
                f"[{self.symbol}] "
                f"Product prepare error: {e}"
            )

            return False


    # -----------------------------------------------------------------
    # 5M CANDLES
    # -----------------------------------------------------------------

    def get_5m_candles(
        self,
        limit=5
    ):

        try:

            end_ts = int(
                now_ist().timestamp()
            )


            start_ts = (

                end_ts
                - limit * 5 * 60
            )


            data = self.client.api(

                "GET",

                "/v2/history/candles",

                params={

                    "resolution":
                        "5m",

                    "symbol":
                        self.symbol,

                    "start":
                        start_ts,

                    "end":
                        end_ts,
                },
            )


            candles = data.get(
                "result",
                []
            )


            formatted = []


            for candle in candles:

                try:

                    if isinstance(
                        candle,
                        dict
                    ):

                        ts = float(

                            candle.get("time")
                            or candle.get("timestamp")
                            or candle.get("start")
                            or 0
                        )


                        if ts > 100000000000:

                            ts /= 1000.0


                        formatted.append({

                            "time":
                                ts,

                            "high":
                                float(
                                    candle.get(
                                        "high"
                                    )
                                ),

                            "low":
                                float(
                                    candle.get(
                                        "low"
                                    )
                                ),

                            "close":
                                float(
                                    candle.get(
                                        "close"
                                    )
                                ),
                        })


                    elif (

                        isinstance(
                            candle,
                            list
                        )

                        and len(candle) >= 5
                    ):

                        ts = float(
                            candle[0]
                        )


                        if ts > 100000000000:

                            ts /= 1000.0


                        formatted.append({

                            "time":
                                ts,

                            "high":
                                float(
                                    candle[2]
                                ),

                            "low":
                                float(
                                    candle[3]
                                ),

                            "close":
                                float(
                                    candle[4]
                                ),
                        })


                except Exception:

                    continue


            formatted.sort(
                key=lambda x: x["time"]
            )


            return formatted


        except Exception:

            return []


    # -----------------------------------------------------------------
    # READ ONLY EXCHANGE POSITION
    # -----------------------------------------------------------------

    def read_exchange_position(
        self
    ):

        if not self.product_id:

            return {

                "size":
                    0,

                "entry_price":
                    None,

                "stop_loss":
                    self.stop_loss,

                "unrealized_pnl":
                    0,

                "leverage":
                    None,

                "liquidation_price":
                    None,
            }


        try:

            return self.client.position(
                self.product_id
            )


        except Exception as e:

            logging.warning(
                f"[{self.symbol}] "
                f"Exchange position read failed: {e}"
            )

            return None


    # -----------------------------------------------------------------
    # WAIT FOR POSITION
    # -----------------------------------------------------------------

    def wait_for_position(
        self,
        expected_direction=None,
        timeout=ENTRY_CONFIRM_TIMEOUT
    ):

        deadline = (
            time.time()
            + timeout
        )


        while time.time() < deadline:

            try:

                position = (
                    self.client.position(
                        self.product_id
                    )
                )


                exchange_size = int(

                    position.get(
                        "size",
                        0
                    )
                    or 0
                )


                if exchange_size != 0:

                    if (
                        expected_direction
                        == "LONG"
                        and exchange_size > 0
                    ):

                        return position


                    if (
                        expected_direction
                        == "SHORT"
                        and exchange_size < 0
                    ):

                        return position


                    if (
                        expected_direction
                        is None
                    ):

                        return position


            except Exception as e:

                logging.warning(
                    f"[{self.symbol}] "
                    f"Position confirmation error: {e}"
                )


            time.sleep(
                POSITION_POLL_INTERVAL
            )


        return None


    # -----------------------------------------------------------------
    # WAIT UNTIL FLAT
    # -----------------------------------------------------------------

    def wait_until_flat(
        self,
        timeout=CLOSE_CONFIRM_TIMEOUT
    ):

        deadline = (
            time.time()
            + timeout
        )


        while time.time() < deadline:

            try:

                position = (
                    self.client.position(
                        self.product_id
                    )
                )


                exchange_size = int(

                    position.get(
                        "size",
                        0
                    )
                    or 0
                )


                if exchange_size == 0:

                    return True


            except Exception as e:

                logging.warning(
                    f"[{self.symbol}] "
                    f"Flat confirmation error: {e}"
                )


            time.sleep(
                POSITION_POLL_INTERVAL
            )


        return False


    # -----------------------------------------------------------------
    # RECONCILE EXCHANGE STATE
    # -----------------------------------------------------------------

    def reconcile_exchange_state(
        self,
        allow_local_updates=True
    ):

        with self.lock:

            if not self.prepare_product():

                return False


            try:

                position = (
                    self.client.position(
                        self.product_id
                    )
                )


            except Exception as e:

                logging.error(
                    f"[{self.symbol}] "
                    f"RECONCILE FAILED: {e}"
                )


                self.execution_uncertain = True

                self.execution_unknown_since = (

                    self.execution_unknown_since
                    or time.time()
                )


                self.save()

                return False


            exchange_size = int(

                position.get(
                    "size",
                    0
                )
                or 0
            )


            # =========================================================
            # EXCHANGE HAS POSITION
            # =========================================================

            if exchange_size != 0:

                exchange_direction = (

                    "LONG"

                    if exchange_size > 0

                    else "SHORT"
                )


                exchange_entry = (
                    position.get(
                        "entry_price"
                    )
                )


                if (
                    self.position is None
                    or self.size <= 0
                ):

                    logging.warning(

                        f"[{self.symbol}] "
                        f"RECONCILE: LOCAL FLAT BUT "
                        f"EXCHANGE HAS "
                        f"{exchange_direction} "
                        f"SIZE={abs(exchange_size)}. "
                        f"ADOPTING EXCHANGE POSITION."
                    )


                    self.position = (
                        exchange_direction
                    )


                    self.size = abs(
                        exchange_size
                    )


                    self.entry_price = (

                        float(exchange_entry)

                        if exchange_entry

                        else self.entry_price
                    )


                    if self.entry_price is None:

                        self.entry_price = (
                            self.last_price
                        )


                    # Existing exchange position:
                    # do not create another breakout order.

                    self.base_breakout_ready = False


                else:

                    if (
                        self.position
                        != exchange_direction
                        or self.size
                        != abs(exchange_size)
                    ):

                        logging.error(

                            f"[{self.symbol}] "
                            f"RECONCILE MISMATCH | "
                            f"LOCAL={self.position}/{self.size} | "
                            f"EXCHANGE={exchange_direction}/{abs(exchange_size)}"
                        )


                        self.position = (
                            exchange_direction
                        )


                        self.size = abs(
                            exchange_size
                        )


                        if exchange_entry:

                            self.entry_price = (
                                float(
                                    exchange_entry
                                )
                            )


                self.execution_uncertain = False

                self.execution_unknown_since = None

                self.last_reconciliation_time = (
                    time.time()
                )


                self.save()

                return True


            # =========================================================
            # EXCHANGE FLAT
            # =========================================================

            if (
                self.position
                and self.size > 0
            ):

                logging.warning(

                    f"[{self.symbol}] "
                    f"RECONCILE: LOCAL POSITION EXISTS "
                    f"BUT EXCHANGE IS FLAT."
                )


                self.position = None

                self.entry_price = None

                self.size = 0

                self.stop_loss = 0.0

                self.is_reversal_position = False

                self.base_breakout_ready = True


            self.execution_uncertain = False

            self.execution_unknown_since = None

            self.last_reconciliation_time = (
                time.time()
            )


            self.save()

            return True


    # -----------------------------------------------------------------
    # SETTINGS
    # -----------------------------------------------------------------

    def update_settings(
        self,
        new_lev,
        new_frac
    ):

        with self.lock:

            try:

                self.leverage = Decimal(
                    str(new_lev)
                )


                # Strategy uses fixed 10% margin.
                # Ignore any different dashboard fraction.

                self.balance_fraction = (
                    Decimal("0.10")
                )


                if self.product_id:

                    self.client.set_leverage(

                        self.product_id,

                        self.leverage
                    )


                self.save()


                return {

                    "success":
                        True,

                    "message":
                        (
                            f"Saved {self.symbol} Settings! "
                            f"Default: "
                            f"{int(self.leverage)}x | "
                            f"Margin: 10%"
                        ),
                }


            except Exception as e:

                return {

                    "success":
                        False,

                    "message":
                        str(e)
                }


    # -----------------------------------------------------------------
    # START BOT
    # -----------------------------------------------------------------

    def start_bot(self):

        with self.lock:

            if self.is_expired():

                return {

                    "success":
                        False,

                    "message":
                        "Subscription expired."
                }


            self.manual_squareoff_flag = False

            self.stop_reason = None


            if not self.prepare_product():

                return {

                    "success":
                        False,

                    "message":
                        "Unable to prepare Delta product."
                }


            # Exchange is authority.

            if not self.reconcile_exchange_state():

                self.bot_enabled = False

                self.stop_reason = (
                    "EXCHANGE RECONCILIATION FAILED"
                )

                self.save()


                return {

                    "success":
                        False,

                    "message":
                        (
                            "Exchange position could not be "
                            "reconciled. Bot remains stopped."
                        )
                }


            if self.execution_uncertain:

                self.bot_enabled = False

                self.save()


                return {

                    "success":
                        False,

                    "message":
                        (
                            "Execution state is uncertain. "
                            "Bot remains stopped."
                        )
                }


            self.bot_enabled = True


            if (
                self.position is None
                or self.size <= 0
            ):

                self.base_breakout_ready = True


            self.save()


            return {

                "success":
                    True,

                "bot_enabled":
                    True,

                "message":
                    f"Bot for {self.symbol} Started.",
            }


    # -----------------------------------------------------------------
    # STOP BOT
    # -----------------------------------------------------------------

    def stop_bot(self):

        with self.lock:

            self.bot_enabled = False

            self.stop_reason = (
                "MANUAL STOP"
            )

            self.manual_squareoff_flag = True


            if (
                self.product_id
                and self.position
                and self.size > 0
            ):

                old_position = (
                    self.position
                )

                old_size = (
                    self.size
                )


                try:

                    close_size = (

                        old_size

                        if old_position == "LONG"

                        else -old_size
                    )


                    self.order_in_progress = True


                    self.client.close_position(

                        self.product_id,

                        close_size
                    )


                    confirmed = (
                        self.wait_until_flat()
                    )


                    if confirmed:

                        self.finish_trade(

                            "MANUAL",

                            self.last_price or 0
                        )


                        self.position = None

                        self.entry_price = None

                        self.size = 0

                        self.stop_loss = 0.0

                        self.is_reversal_position = False

                        self.base_breakout_ready = True

                        self.execution_uncertain = False

                        self.execution_unknown_since = None


                    else:

                        self.execution_uncertain = True

                        self.execution_unknown_since = (

                            self.execution_unknown_since
                            or time.time()
                        )


                        logging.error(

                            f"[{self.symbol}] "
                            f"MANUAL STOP: "
                            f"EXCHANGE DID NOT CONFIRM FLAT."
                        )


                except Exception as e:

                    self.execution_uncertain = True

                    self.execution_unknown_since = (

                        self.execution_unknown_since
                        or time.time()
                    )


                    logging.error(

                        f"[{self.symbol}] "
                        f"Manual close failed: {e}"
                    )


                finally:

                    self.order_in_progress = False


            else:

                try:

                    if self.product_id:

                        position = (
                            self.client.position(
                                self.product_id
                            )
                        )


                        exchange_size = int(

                            position.get(
                                "size",
                                0
                            )
                            or 0
                        )


                        if exchange_size != 0:

                            self.execution_uncertain = True

                            self.execution_unknown_since = (

                                self.execution_unknown_since
                                or time.time()
                            )


                        else:

                            self.position = None

                            self.entry_price = None

                            self.size = 0

                            self.stop_loss = 0.0

                            self.is_reversal_position = False

                            self.base_breakout_ready = True


                except Exception as e:

                    self.execution_uncertain = True

                    self.execution_unknown_since = (

                        self.execution_unknown_since
                        or time.time()
                    )


                    logging.error(
                        f"[{self.symbol}] "
                        f"Stop reconciliation error: {e}"
                    )


            self.save()


            return {

                "success":
                    True,

                "bot_enabled":
                    False,

                "message":
                    f"Bot for {self.symbol} Stopped.",
            }


    # -----------------------------------------------------------------
    # SESSION CHANGE
    # -----------------------------------------------------------------

    def check_session_change(
        self,
        now
    ):

        current_session = (
            get_current_session_start(
                now
            )
        )


        if (
            self.session_start
            == current_session
        ):

            return True


        # =============================================================
        # OLD SESSION POSITION MUST BE CLOSED
        # =============================================================

        if self.product_id:

            try:

                self.client.cancel_all_orders(
                    self.product_id
                )

            except Exception:

                pass


            try:

                exchange_position = (
                    self.client.position(
                        self.product_id
                    )
                )


                exchange_size = int(

                    exchange_position.get(
                        "size",
                        0
                    )
                    or 0
                )


                if exchange_size != 0:

                    logging.info(

                        f"[{self.symbol}] "
                        f"SESSION CHANGE: closing "
                        f"existing position "
                        f"{exchange_size}"
                    )


                    self.order_in_progress = True


                    try:

                        self.client.close_position(

                            self.product_id,

                            exchange_size
                        )


                        confirmed = (
                            self.wait_until_flat()
                        )


                    finally:

                        self.order_in_progress = False


                    if not confirmed:

                        logging.error(

                            f"[{self.symbol}] "
                            f"SESSION CHANGE: "
                            f"POSITION NOT FLAT."
                        )


                        self.execution_uncertain = True

                        self.execution_unknown_since = (

                            self.execution_unknown_since
                            or time.time()
                        )


                        self.save()

                        return False


                    self.finish_trade(

                        "SESSION_CHANGE_CLOSE",

                        self.last_price or 0
                    )


                # Confirmed flat.

                self.position = None

                self.entry_price = None

                self.size = 0

                self.stop_loss = 0.0

                self.is_reversal_position = False

                self.base_breakout_ready = True

                self.execution_uncertain = False

                self.execution_unknown_since = None


            except Exception as e:

                logging.error(

                    f"[{self.symbol}] "
                    f"Session change error: {e}"
                )


                self.execution_uncertain = True

                self.execution_unknown_since = (

                    self.execution_unknown_since
                    or time.time()
                )


                self.save()

                return False


        # =============================================================
        # NEW SESSION
        # =============================================================

        self.session_start = (
            current_session
        )


        self.day_high = None

        self.day_low = None


        self.prev_price = None

        self.last_strategy_price = None


        self.position = None

        self.entry_price = None

        self.size = 0

        self.stop_loss = 0.0

        self.is_reversal_position = False

        self.base_breakout_ready = True


        self.manual_squareoff_flag = False

        self.ready = False

        self.trading_armed = False

        self.last_checked_candle_time = 0


        if (
            self.product_id
            and not is_weekend(
                self.symbol,
                now
            )
        ):

            high, low = (
                self.client.get_session_high_low(
                    self.session_start
                )
            )


            if (
                high is not None
                and low is not None
            ):

                self.day_high = high

                self.day_low = low

                self.ready = True


        self.save()

        return True


    # -----------------------------------------------------------------
    # PREPARE SESSION
    # -----------------------------------------------------------------

    def prepare(
        self,
        now
    ):

        if not self.prepare_product():

            return False


        if self.session_start is None:

            self.session_start = (
                get_current_session_start(
                    now
                )
            )


        if is_weekend(
            self.symbol,
            now
        ):

            return True


        if (
            self.day_high is None
            or self.day_low is None
        ):

            high, low = (

                self.client.get_session_high_low(

                    self.session_start
                )
            )


            if (
                high is not None
                and low is not None
            ):

                self.day_high = high

                self.day_low = low

                self.ready = True

                self.save()


        return True


    # -----------------------------------------------------------------
    # LIQUIDATION ESTIMATE
    # -----------------------------------------------------------------

    def estimate_liquidation_price(
        self,
        entry_price,
        leverage,
        direction
    ):

        entry = Decimal(
            str(entry_price)
        )

        lev = Decimal(
            str(leverage)
        )


        if (
            entry <= 0
            or lev <= 0
        ):

            return None


        maintenance_raw = (

            self.product.get(
                "maintenance_margin",
                0
            )

            if self.product

            else 0
        )


        taker_raw = (

            self.product.get(
                "taker_commission_rate",
                0
            )

            if self.product

            else 0
        )


        try:

            maintenance = (

                Decimal(
                    str(maintenance_raw)
                )
                / Decimal("100")
            )


        except Exception:

            maintenance = Decimal("0")


        try:

            taker_fee = Decimal(
                str(taker_raw)
            )


        except Exception:

            taker_fee = Decimal("0")


        effective_mm = (

            maintenance
            + taker_fee
            + Decimal("0.0010")
        )


        if direction == "LONG":

            return (

                entry
                * (
                    Decimal("1")
                    - Decimal("1") / lev
                    + effective_mm
                )
            )


        return (

            entry
            * (
                Decimal("1")
                + Decimal("1") / lev
                - effective_mm
            )
        )


    # -----------------------------------------------------------------
    # LEVERAGE LADDER
    # -----------------------------------------------------------------

    def get_leverage_ladder(self):

        if "BTC" in self.symbol:

            return list(
                range(
                    200,
                    9,
                    -10
                )
            )


        return list(
            range(
                100,
                9,
                -10
            )
        )


    # -----------------------------------------------------------------
    # ENTER
    # -----------------------------------------------------------------

    def enter(
        self,
        direction,
        price,
        initial_sl,
        is_reversal=False
    ):

        if (

            self.is_expired()

            or self.manual_squareoff_flag

            or not self.bot_enabled

            or is_weekend(self.symbol)
        ):

            return False


        if self.execution_uncertain:

            logging.error(

                f"[{self.symbol}] "
                f"ENTRY BLOCKED: execution uncertain."
            )

            return False


        if self.order_in_progress:

            logging.warning(

                f"[{self.symbol}] "
                f"ENTRY BLOCKED: another order active."
            )

            return False


        self.order_in_progress = True


        try:

            # =========================================================
            # EXCHANGE MUST BE FLAT
            # =========================================================

            current_position = (
                self.client.position(
                    self.product_id
                )
            )


            current_size = int(

                current_position.get(
                    "size",
                    0
                )
                or 0
            )


            if current_size != 0:

                logging.warning(

                    f"[{self.symbol}] "
                    f"ENTRY BLOCKED: exchange already has "
                    f"position {current_size}"
                )


                self.reconcile_exchange_state()

                return False


            # =========================================================
            # EXACT LEVERAGE LADDER
            # =========================================================

            leverage_ladder = (
                self.get_leverage_ladder()
            )


            confirmed_position = None

            chosen_leverage = None

            order_completed = False


            # =========================================================
            # TRY LEVERAGES FROM HIGH TO LOW
            # =========================================================

            for leverage_value in leverage_ladder:

                leverage_decimal = Decimal(
                    str(leverage_value)
                )


                candidate_liquidation = (

                    self.estimate_liquidation_price(

                        price,

                        leverage_decimal,

                        direction
                    )
                )


                if candidate_liquidation is None:

                    continue


                # -----------------------------------------------------
                # LONG:
                # liquidation must remain below SL
                # -----------------------------------------------------

                if (
                    direction == "LONG"
                    and candidate_liquidation
                    >= Decimal(
                        str(initial_sl)
                    )
                ):

                    continue


                # -----------------------------------------------------
                # SHORT:
                # liquidation must remain above SL
                # -----------------------------------------------------

                if (
                    direction == "SHORT"
                    and candidate_liquidation
                    <= Decimal(
                        str(initial_sl)
                    )
                ):

                    continue


                try:

                    # -------------------------------------------------
                    # Set leverage BEFORE order
                    # -------------------------------------------------

                    self.client.set_leverage(

                        self.product_id,

                        leverage_decimal
                    )


                    # -------------------------------------------------
                    # Calculate size using exactly 10% margin
                    # -------------------------------------------------

                    size = (

                        self.client.order_size(

                            self.product,

                            Decimal(
                                str(price)
                            ),

                            leverage_decimal,

                            Decimal("0.10")
                        )
                    )


                    side = (

                        "buy"

                        if direction == "LONG"

                        else "sell"
                    )


                    # =================================================
                    # ONE AND ONLY ONE ORDER
                    # =================================================

                    response = (

                        self.client.market_entry_pure(

                            self.product_id,

                            side,

                            size
                        )
                    )


                    logging.info(

                        f"[{self.symbol}] "
                        f"ENTRY ORDER SENT | "
                        f"{direction} | "
                        f"Size={size} | "
                        f"Lev={leverage_decimal} | "
                        f"Response={response}"
                    )


                    # =================================================
                    # CONFIRM EXCHANGE POSITION
                    # =================================================

                    confirmed_position = (

                        self.wait_for_position(

                            expected_direction=
                                direction,

                            timeout=
                                ENTRY_CONFIRM_TIMEOUT
                        )
                    )


                    if confirmed_position is None:

                        # IMPORTANT:
                        #
                        # Never try another leverage.
                        # The order may have been accepted.

                        self.execution_uncertain = True

                        self.execution_unknown_since = (
                            time.time()
                        )


                        self.save()


                        logging.error(

                            f"[{self.symbol}] "
                            f"ENTRY EXECUTION UNKNOWN. "
                            f"NO SECOND ORDER."
                        )


                        return False


                    confirmed_size = int(

                        confirmed_position.get(
                            "size",
                            0
                        )
                        or 0
                    )


                    if (
                        direction == "LONG"
                        and confirmed_size <= 0
                    ):

                        self.execution_uncertain = True

                        self.execution_unknown_since = (
                            time.time()
                        )

                        self.save()

                        return False


                    if (
                        direction == "SHORT"
                        and confirmed_size >= 0
                    ):

                        self.execution_uncertain = True

                        self.execution_unknown_since = (
                            time.time()
                        )

                        self.save()

                        return False


                    chosen_leverage = (
                        leverage_decimal
                    )


                    order_completed = True

                    break


                except Exception as e:

                    logging.warning(

                        f"[{self.symbol}] "
                        f"Entry attempt {leverage_value}x failed: {e}"
                    )


                    # -------------------------------------------------
                    # Before trying next leverage, check exchange.
                    # -------------------------------------------------

                    try:

                        check_position = (
                            self.client.position(
                                self.product_id
                            )
                        )


                        check_size = int(

                            check_position.get(
                                "size",
                                0
                            )
                            or 0
                        )


                        if check_size != 0:

                            exchange_direction = (

                                "LONG"

                                if check_size > 0

                                else "SHORT"
                            )


                            if (
                                exchange_direction
                                == direction
                            ):

                                confirmed_position = (
                                    check_position
                                )

                                chosen_leverage = (
                                    leverage_decimal
                                )

                                order_completed = True

                                break


                            # Opposite position means unknown state.

                            self.execution_uncertain = True

                            self.execution_unknown_since = (
                                time.time()
                            )

                            self.save()


                            logging.error(

                                f"[{self.symbol}] "
                                f"ENTRY ERROR BUT OPPOSITE "
                                f"POSITION EXISTS. "
                                f"NO MORE ORDERS."
                            )


                            return False


                    except Exception:

                        self.execution_uncertain = True

                        self.execution_unknown_since = (
                            time.time()
                        )

                        self.save()

                        return False


            if not order_completed:

                return False


            # =========================================================
            # EXCHANGE CONFIRMED
            # =========================================================

            exchange_size = abs(

                int(

                    confirmed_position.get(
                        "size",
                        0
                    )
                )
            )


            exchange_entry = (

                confirmed_position.get(
                    "entry_price"
                )
            )


            if exchange_size <= 0:

                self.execution_uncertain = True

                self.execution_unknown_since = (
                    time.time()
                )

                self.save()

                return False


            self.position = (
                direction
            )


            self.entry_price = (

                float(exchange_entry)

                if exchange_entry

                else float(price)
            )


            self.size = (
                exchange_size
            )


            self.leverage = (
                chosen_leverage
            )


            self.stop_loss = (
                float(initial_sl)
            )


            self.is_reversal_position = (
                bool(is_reversal)
            )


            # Once in a position, no base breakout allowed.

            self.base_breakout_ready = (
                False
            )


            self.execution_uncertain = False

            self.execution_unknown_since = None

            self.last_execution_time = (
                time.time()
            )


            self.save()


            logging.info(

                f"[{self.symbol}] "
                f"CONFIRMED ENTER | "
                f"{direction} | "
                f"{'REVERSAL' if is_reversal else 'BASE'} | "
                f"Entry={self.entry_price} | "
                f"Size={self.size} | "
                f"Lev={int(chosen_leverage)}x | "
                f"SL={self.stop_loss}"
            )


            return True


        except Exception as e:

            logging.error(

                f"[{self.symbol}] "
                f"Entry error: {e}"
            )

            return False


        finally:

            self.order_in_progress = False


    # -----------------------------------------------------------------
    # CLOSE CURRENT POSITION
    # -----------------------------------------------------------------

    def close_current_position(
        self,
        reason,
        exit_price
    ):

        # -------------------------------------------------------------
        # Local state says flat.
        # Verify exchange.
        # -------------------------------------------------------------

        if (
            not self.position
            or self.size <= 0
        ):

            try:

                exchange_position = (
                    self.client.position(
                        self.product_id
                    )
                )


                exchange_size = int(

                    exchange_position.get(
                        "size",
                        0
                    )
                    or 0
                )


                if exchange_size == 0:

                    return True


                self.execution_uncertain = True

                self.execution_unknown_since = (
                    time.time()
                )

                self.save()

                return False


            except Exception:

                self.execution_uncertain = True

                self.execution_unknown_since = (
                    time.time()
                )

                self.save()

                return False


        if self.order_in_progress:

            logging.warning(

                f"[{self.symbol}] "
                f"CLOSE BLOCKED: execution active."
            )

            return False


        self.order_in_progress = True


        try:

            exchange_position = (
                self.client.position(
                    self.product_id
                )
            )


            exchange_size = int(

                exchange_position.get(
                    "size",
                    0
                )
                or 0
            )


            # ---------------------------------------------------------
            # Exchange already flat
            # ---------------------------------------------------------

            if exchange_size == 0:

                logging.warning(

                    f"[{self.symbol}] "
                    f"{reason}: exchange already flat."
                )


                self.finish_trade(
                    reason,
                    exit_price
                )


                self.position = None

                self.entry_price = None

                self.size = 0

                self.stop_loss = 0.0

                self.is_reversal_position = False

                self.base_breakout_ready = True

                self.execution_uncertain = False

                self.execution_unknown_since = None

                self.save()

                return True


            # ---------------------------------------------------------
            # Close using exchange position size.
            # ---------------------------------------------------------

            self.client.close_position(

                self.product_id,

                exchange_size
            )


            confirmed = (
                self.wait_until_flat()
            )


            if not confirmed:

                # CRITICAL:
                # Keep local position.
                # No reversal.
                # No new order.

                self.execution_uncertain = True

                self.execution_unknown_since = (
                    time.time()
                )


                logging.error(

                    f"[{self.symbol}] "
                    f"{reason}: "
                    f"EXCHANGE DID NOT CONFIRM FLAT."
                )


                self.save()

                return False


            # =========================================================
            # FLAT CONFIRMED
            # =========================================================

            self.finish_trade(

                reason,

                exit_price
            )


            self.position = None

            self.entry_price = None

            self.size = 0

            self.stop_loss = 0.0

            self.is_reversal_position = False


            # Flat means breakout mode can be armed.
            self.base_breakout_ready = True


            self.execution_uncertain = False

            self.execution_unknown_since = None


            self.save()


            logging.info(

                f"[{self.symbol}] "
                f"CLOSE CONFIRMED | "
                f"Reason={reason}"
            )


            return True


        except Exception as e:

            logging.error(

                f"[{self.symbol}] "
                f"Close error: {e}"
            )


            self.execution_uncertain = True

            self.execution_unknown_since = (

                self.execution_unknown_since
                or time.time()
            )


            self.save()

            return False


        finally:

            self.order_in_progress = False


    # -----------------------------------------------------------------
    # REVERSAL FROM SL
    # -----------------------------------------------------------------

    def reverse_from_sl(
        self,
        price,
        prev_candle
    ):

        if (
            not self.position
            or self.size <= 0
        ):

            return False


        if self.execution_uncertain:

            return False


        old_direction = (
            self.position
        )


        old_is_reversal = (
            self.is_reversal_position
        )


        # =============================================================
        # REVERSAL POSITION SL
        #
        # NO SECOND REVERSAL
        # =============================================================

        if old_is_reversal:

            logging.info(

                f"[{self.symbol}] "
                f"REVERSAL {old_direction} SL HIT "
                f"-> FLAT "
                f"-> NO SECOND REVERSAL"
            )


            closed = (

                self.close_current_position(

                    "REVERSAL_SL_HIT",

                    price
                )
            )


            if not closed:

                return False


            # ---------------------------------------------------------
            # IMPORTANT:
            #
            # Do NOT reset day_high/day_low.
            #
            # If SL-hit price itself made a new extreme,
            # consume that extreme.
            #
            # Next entry must break the CURRENT extreme.
            # ---------------------------------------------------------

            changed = False


            price_decimal = Decimal(
                str(price)
            )


            if (
                self.day_high is None
                or price_decimal > self.day_high
            ):

                self.day_high = (
                    price_decimal
                )

                changed = True


            if (
                self.day_low is None
                or price_decimal < self.day_low
            ):

                self.day_low = (
                    price_decimal
                )

                changed = True


            # ---------------------------------------------------------
            # Explicitly re-arm base breakout.
            # No trade is entered on this same tick.
            # ---------------------------------------------------------

            self.base_breakout_ready = True

            self.last_strategy_price = float(
                price
            )


            if changed:

                self.save()

            else:

                self.save()


            logging.info(

                f"[{self.symbol}] "
                f"REVERSAL COMPLETE -> FLAT | "
                f"WAITING FOR NEW CURRENT "
                f"HIGH/LOW BREAK | "
                f"High={self.day_high} | "
                f"Low={self.day_low}"
            )


            return True


        # =============================================================
        # BASE LONG SL -> ONE SHORT
        # =============================================================

        if old_direction == "LONG":

            reversal_sl = float(
                prev_candle["high"]
            )


            logging.info(

                f"[{self.symbol}] "
                f"BASE LONG SL HIT "
                f"-> ONE SHORT REVERSAL | "
                f"Reversal SL={reversal_sl}"
            )


            # ---------------------------------------------------------
            # First close LONG.
            # ---------------------------------------------------------

            closed = (

                self.close_current_position(

                    "SL_HIT_REVERSAL",

                    price
                )
            )


            if not closed:

                logging.error(

                    f"[{self.symbol}] "
                    f"LONG close not confirmed. "
                    f"SHORT reversal BLOCKED."
                )

                return False


            # ---------------------------------------------------------
            # Prevent base breakout while reversal is being opened.
            # ---------------------------------------------------------

            self.base_breakout_ready = False

            self.save()


            # ---------------------------------------------------------
            # ONE SHORT REVERSAL ONLY
            # ---------------------------------------------------------

            success = self.enter(

                "SHORT",

                price,

                reversal_sl,

                is_reversal=True
            )


            if success:

                logging.info(

                    f"[{self.symbol}] "
                    f"ONE SHORT REVERSAL CONFIRMED."
                )

                return True


            # ---------------------------------------------------------
            # Reversal failed.
            # Check exchange.
            # ---------------------------------------------------------

            try:

                position = (
                    self.client.position(
                        self.product_id
                    )
                )


                exchange_size = int(

                    position.get(
                        "size",
                        0
                    )
                    or 0
                )


                if exchange_size == 0:

                    self.position = None

                    self.entry_price = None

                    self.size = 0

                    self.stop_loss = 0.0

                    self.is_reversal_position = False

                    self.base_breakout_ready = True

                    self.execution_uncertain = False

                    self.execution_unknown_since = None

                    self.save()


                else:

                    self.execution_uncertain = True

                    self.execution_unknown_since = (
                        time.time()
                    )

                    self.save()


            except Exception:

                self.execution_uncertain = True

                self.execution_unknown_since = (
                    time.time()
                )

                self.save()


            return False


        # =============================================================
        # BASE SHORT SL -> ONE LONG
        # =============================================================

        if old_direction == "SHORT":

            reversal_sl = float(
                prev_candle["low"]
            )


            logging.info(

                f"[{self.symbol}] "
                f"BASE SHORT SL HIT "
                f"-> ONE LONG REVERSAL | "
                f"Reversal SL={reversal_sl}"
            )


            # ---------------------------------------------------------
            # Close SHORT first.
            # ---------------------------------------------------------

            closed = (

                self.close_current_position(

                    "SL_HIT_REVERSAL",

                    price
                )
            )


            if not closed:

                logging.error(

                    f"[{self.symbol}] "
                    f"SHORT close not confirmed. "
                    f"LONG reversal BLOCKED."
                )

                return False


            self.base_breakout_ready = False

            self.save()


            # ---------------------------------------------------------
            # ONE LONG REVERSAL
            # ---------------------------------------------------------

            success = self.enter(

                "LONG",

                price,

                reversal_sl,

                is_reversal=True
            )


            if success:

                logging.info(

                    f"[{self.symbol}] "
                    f"ONE LONG REVERSAL CONFIRMED."
                )

                return True


            # ---------------------------------------------------------
            # Reversal failed.
            # ---------------------------------------------------------

            try:

                position = (
                    self.client.position(
                        self.product_id
                    )
                )


                exchange_size = int(

                    position.get(
                        "size",
                        0
                    )
                    or 0
                )


                if exchange_size == 0:

                    self.position = None

                    self.entry_price = None

                    self.size = 0

                    self.stop_loss = 0.0

                    self.is_reversal_position = False

                    self.base_breakout_ready = True

                    self.execution_uncertain = False

                    self.execution_unknown_since = None

                    self.save()


                else:

                    self.execution_uncertain = True

                    self.execution_unknown_since = (
                        time.time()
                    )

                    self.save()


            except Exception:

                self.execution_uncertain = True

                self.execution_unknown_since = (
                    time.time()
                )

                self.save()


            return False


        return False


    # -----------------------------------------------------------------
    # WEEKEND FLAT
    # -----------------------------------------------------------------

    def force_weekend_flat(self):

        with self.lock:

            if not self.product_id:

                return


            try:

                exchange_position = (
                    self.client.position(
                        self.product_id
                    )
                )


                exchange_size = int(

                    exchange_position.get(
                        "size",
                        0
                    )
                    or 0
                )


                if exchange_size != 0:

                    if self.order_in_progress:

                        return


                    self.order_in_progress = True


                    try:

                        self.client.close_position(

                            self.product_id,

                            exchange_size
                        )


                        confirmed = (
                            self.wait_until_flat()
                        )


                    finally:

                        self.order_in_progress = False


                    if confirmed:

                        if (
                            self.position
                            and self.size > 0
                        ):

                            self.finish_trade(

                                "WEEKEND_CLOSE",

                                self.last_price or 0
                            )


                        self.position = None

                        self.entry_price = None

                        self.size = 0

                        self.stop_loss = 0.0

                        self.is_reversal_position = False

                        self.base_breakout_ready = True

                        self.execution_uncertain = False

                        self.execution_unknown_since = None

                        self.save()


                        logging.info(

                            f"[{self.symbol}] "
                            f"WEEKEND POSITION CLOSED."
                        )


                    else:

                        self.execution_uncertain = True

                        self.execution_unknown_since = (

                            self.execution_unknown_since
                            or time.time()
                        )


                        logging.error(

                            f"[{self.symbol}] "
                            f"WEEKEND CLOSE NOT CONFIRMED."
                        )


                        self.save()


                else:

                    # Exchange confirms flat.

                    self.position = None

                    self.entry_price = None

                    self.size = 0

                    self.stop_loss = 0.0

                    self.is_reversal_position = False

                    self.base_breakout_ready = True

                    self.execution_uncertain = False

                    self.execution_unknown_since = None

                    self.save()


            except Exception as e:

                self.execution_uncertain = True

                self.execution_unknown_since = (

                    self.execution_unknown_since
                    or time.time()
                )


                logging.error(

                    f"[{self.symbol}] "
                    f"Weekend close error: {e}"
                )


                self.save()


    # -----------------------------------------------------------------
    # MAIN EVALUATION
    # -----------------------------------------------------------------

    def evaluate(
        self,
        price=None
    ):

        with self.lock:

            # =========================================================
            # BOT OFF
            # =========================================================

            if (
                not self.bot_enabled
                or self.is_expired()
            ):

                return


            now = now_ist()


            # =========================================================
            # WEEKEND
            # =========================================================

            if is_weekend(
                self.symbol,
                now
            ):

                self.force_weekend_flat()

                self.prev_price = None

                return


            # =========================================================
            # UNKNOWN EXECUTION
            #
            # NO NEW ORDER
            # =========================================================

            if self.execution_uncertain:

                if (

                    time.time()
                    - self.last_reconciliation_time

                    >=
                    EXECUTION_UNKNOWN_RECHECK_INTERVAL
                ):

                    self.reconcile_exchange_state()


                return


            # =========================================================
            # PRICE
            # =========================================================

            if price is None:

                price = (
                    self.client.last_traded_price()
                )


                if price is None:

                    return


            new_price = float(
                price
            )


            self.last_price = (
                new_price
            )


            # =========================================================
            # SAME PRICE DEDUPE
            # =========================================================

            if (

                self.last_strategy_price
                is not None

                and new_price
                == self.last_strategy_price
            ):

                return


            self.last_strategy_price = (
                new_price
            )


            # =========================================================
            # SESSION CHANGE
            # =========================================================

            if not self.check_session_change(
                now
            ):

                return


            # =========================================================
            # PREPARE SESSION HIGH/LOW
            # =========================================================

            if not self.prepare(
                now
            ):

                return


            if (
                self.day_high is None
                or self.day_low is None
            ):

                return


            self.ready = True


            # =========================================================
            # BEFORE 05:45
            #
            # Session high/low are already being built.
            # No trade before 05:45.
            # =========================================================

            if now.time() < TRADING_START_TIME:

                self.trading_armed = False

                return


            # =========================================================
            # FIRST TICK AFTER 05:45
            #
            # Arm trading but DO NOT fire on stale state.
            # =========================================================

            if not self.trading_armed:

                self.trading_armed = True

                self.save()

                return


            # =========================================================
            # GET 5M CANDLES
            # =========================================================

            candles = (
                self.get_5m_candles(
                    limit=5
                )
            )


            if len(candles) < 2:

                return


            # Last candle = current/incomplete.
            # Previous candle = completed candle.

            prev_candle = (
                candles[-2]
            )

            current_candle = (
                candles[-1]
            )


            current_candle_time = (
                current_candle["time"]
            )


            # =========================================================
            # ACTIVE LONG
            # =========================================================

            if (

                self.position == "LONG"

                and self.size > 0
            ):

                # -----------------------------------------------------
                # CURRENT STOP FIRST
                # -----------------------------------------------------

                if (

                    self.stop_loss > 0

                    and new_price
                    <= self.stop_loss
                ):

                    logging.info(

                        f"[{self.symbol}] "
                        f"LONG SL HIT | "
                        f"Price={new_price} | "
                        f"SL={self.stop_loss} | "
                        f"Reversal={self.is_reversal_position}"
                    )


                    self.reverse_from_sl(

                        new_price,

                        prev_candle
                    )


                    return


                # -----------------------------------------------------
                # NEW COMPLETED 5M CANDLE
                # -----------------------------------------------------

                if (

                    current_candle_time

                    !=

                    self.last_checked_candle_time
                ):

                    new_sl = float(
                        prev_candle["low"]
                    )


                    self.stop_loss = (
                        new_sl
                    )


                    self.last_checked_candle_time = (
                        current_candle_time
                    )


                    self.save()


                    logging.info(

                        f"[{self.symbol}] "
                        f"LONG TRAILING SL UPDATED | "
                        f"SL={new_sl}"
                    )


                    # -------------------------------------------------
                    # Check immediately after trailing.
                    # -------------------------------------------------

                    if (

                        new_price
                        <= self.stop_loss
                    ):

                        logging.info(

                            f"[{self.symbol}] "
                            f"NEW TRAILING LONG SL HIT | "
                            f"Price={new_price} | "
                            f"SL={self.stop_loss}"
                        )


                        self.reverse_from_sl(

                            new_price,

                            prev_candle
                        )


                        return


                # -----------------------------------------------------
                # Position is active.
                # Never execute base breakout.
                # -----------------------------------------------------

                return


            # =========================================================
            # ACTIVE SHORT
            # =========================================================

            if (

                self.position == "SHORT"

                and self.size > 0
            ):

                # -----------------------------------------------------
                # CURRENT STOP FIRST
                # -----------------------------------------------------

                if (

                    self.stop_loss > 0

                    and new_price
                    >= self.stop_loss
                ):

                    logging.info(

                        f"[{self.symbol}] "
                        f"SHORT SL HIT | "
                        f"Price={new_price} | "
                        f"SL={self.stop_loss} | "
                        f"Reversal={self.is_reversal_position}"
                    )


                    self.reverse_from_sl(

                        new_price,

                        prev_candle
                    )


                    return


                # -----------------------------------------------------
                # NEW COMPLETED 5M CANDLE
                # -----------------------------------------------------

                if (

                    current_candle_time

                    !=

                    self.last_checked_candle_time
                ):

                    new_sl = float(
                        prev_candle["high"]
                    )


                    self.stop_loss = (
                        new_sl
                    )


                    self.last_checked_candle_time = (
                        current_candle_time
                    )


                    self.save()


                    logging.info(

                        f"[{self.symbol}] "
                        f"SHORT TRAILING SL UPDATED | "
                        f"SL={new_sl}"
                    )


                    if (

                        new_price
                        >= self.stop_loss
                    ):

                        logging.info(

                            f"[{self.symbol}] "
                            f"NEW TRAILING SHORT SL HIT | "
                            f"Price={new_price} | "
                            f"SL={self.stop_loss}"
                        )


                        self.reverse_from_sl(

                            new_price,

                            prev_candle
                        )


                        return


                # -----------------------------------------------------
                # Position active.
                # Never execute base breakout.
                # -----------------------------------------------------

                return


            # =========================================================
            # FLAT BASE BREAKOUT
            # =========================================================

            if not (

                self.position is None

                and self.size == 0

                and self.base_breakout_ready

                and not self.manual_squareoff_flag

                and not self.execution_uncertain

                and not self.order_in_progress

                and self.day_high is not None

                and self.day_low is not None
            ):

                return


            current_high = float(
                self.day_high
            )

            current_low = float(
                self.day_low
            )


            # =========================================================
            # CURRENT HIGH BREAK -> LONG
            # =========================================================

            if new_price > current_high:

                initial_sl = float(
                    prev_candle["low"]
                )


                # -----------------------------------------------------
                # Consume the breakout level immediately.
                # -----------------------------------------------------

                self.day_high = Decimal(
                    str(new_price)
                )


                self.save()


                logging.info(

                    f"[{self.symbol}] "
                    f"CURRENT HIGH BREAK | "
                    f"OldHigh={current_high} | "
                    f"BreakPrice={new_price} | "
                    f"BASE LONG | "
                    f"SL={initial_sl}"
                )


                success = self.enter(

                    "LONG",

                    new_price,

                    initial_sl,

                    is_reversal=False
                )


                if success:

                    self.base_breakout_ready = False

                    self.is_reversal_position = False

                    self.save()


                return


            # =========================================================
            # CURRENT LOW BREAK -> SHORT
            # =========================================================

            if new_price < current_low:

                initial_sl = float(
                    prev_candle["high"]
                )


                # -----------------------------------------------------
                # Consume the breakout level immediately.
                # -----------------------------------------------------

                self.day_low = Decimal(
                    str(new_price)
                )


                self.save()


                logging.info(

                    f"[{self.symbol}] "
                    f"CURRENT LOW BREAK | "
                    f"OldLow={current_low} | "
                    f"BreakPrice={new_price} | "
                    f"BASE SHORT | "
                    f"SL={initial_sl}"
                )


                success = self.enter(

                    "SHORT",

                    new_price,

                    initial_sl,

                    is_reversal=False
                )


                if success:

                    self.base_breakout_ready = False

                    self.is_reversal_position = False

                    self.save()


                return


            # =========================================================
            # NO BREAKOUT
            #
            # Update current running session extremes only.
            # No order is triggered by this block.
            # =========================================================

            changed = False


            if (

                self.day_high is None

                or new_price
                > float(self.day_high)
            ):

                self.day_high = Decimal(
                    str(new_price)
                )

                changed = True


            if (

                self.day_low is None

                or new_price
                < float(self.day_low)
            ):

                self.day_low = Decimal(
                    str(new_price)
                )

                changed = True


            if changed:

                self.save()


    # -----------------------------------------------------------------
    # TRADE HISTORY
    # -----------------------------------------------------------------

    def finish_trade(
        self,
        reason,
        exit_price
    ):

        if (

            not self.position

            or not self.entry_price
        ):

            return


        pnl = calculate_trade_pnl(

            self.position,

            self.entry_price,

            exit_price,

            self.size,

            self.product
            or {
                "contract_value":
                    "0.001"
            },
        )


        trade = {

            "id":
                (
                    f"{self.symbol.lower()}_"
                    f"{int(time.time() * 1000)}_"
                    f"{uuid.uuid4().hex[:8]}"
                ),

            "account_id":
                self.unique_id,

            "account":
                self.account_name,

            "symbol":
                self.symbol,

            "date":
                now_ist().strftime(
                    "%Y-%m-%d %H:%M"
                ),

            "direction":
                self.position,

            "entry_price":
                float(
                    self.entry_price
                ),

            "exit_price":
                float(
                    exit_price
                ),

            "size":
                self.size,

            "pnl":
                float(pnl),

            "reason":
                reason,

            "trade_type":
                (
                    "REVERSAL"

                    if self.is_reversal_position

                    else "BREAKOUT"
                ),
        }


        history = load_trade_history(
            self.unique_id
        )


        history.append(
            trade
        )


        save_trade_history(

            self.unique_id,

            history
        )


# =====================================================================
# ACCOUNTS MANAGER
# =====================================================================

BOT_ACCOUNTS = {}

ACCOUNTS_LOCK = (
    threading.RLock()
)


def create_all_accounts():

    new_accounts = {}


    # ================================================================
    # PRIMARY
    # ================================================================

    if (
        PRIMARY_API_KEY
        and PRIMARY_API_SECRET
    ):

        for symbol in (
            "XAUTUSD",
            "BTCUSD"
        ):

            bot = BreakoutSARBot(

                PRIMARY_ACCOUNT_ID,

                PRIMARY_ACCOUNT_NAME,

                "primary",

                PRIMARY_API_KEY,

                PRIMARY_API_SECRET,

                symbol,
            )


            new_accounts[
                bot.unique_id
            ] = bot


    # ================================================================
    # CLIENTS
    # ================================================================

    clients_cfg = (
        load_clients_config()
    )


    for client_id, client_data in clients_cfg.items():

        subscription = {

            "start":
                client_data.get(
                    "subscription_start"
                ),

            "expiry":
                client_data.get(
                    "subscription_expiry"
                ),
        }


        api_key = (

            client_data.get(
                "api_key"
            )

            or ""
        ).strip()


        api_secret = (

            client_data.get(
                "api_secret"
            )

            or ""
        ).strip()


        if (
            not api_key
            or not api_secret
        ):

            logging.warning(

                f"Skipping client {client_id}: "
                f"API credentials missing."
            )

            continue


        for symbol in (
            "XAUTUSD",
            "BTCUSD"
        ):

            bot = BreakoutSARBot(

                client_id,

                client_data.get(
                    "name",
                    "Client"
                ),

                "client",

                api_key,

                api_secret,

                symbol,

                subscription,
            )


            new_accounts[
                bot.unique_id
            ] = bot


    return new_accounts


def load_all_accounts(
    preserve_running=True
):

    global BOT_ACCOUNTS


    new_accounts = (
        create_all_accounts()
    )


    with ACCOUNTS_LOCK:

        old_accounts = (
            BOT_ACCOUNTS
        )


        for key, new_bot in new_accounts.items():

            old_bot = (
                old_accounts.get(
                    key
                )
            )


            if (
                old_bot is not None
                and preserve_running
            ):

                new_accounts[key] = old_bot


        BOT_ACCOUNTS = (
            new_accounts
        )


    logging.info(

        f"ACCOUNTS LOADED | "
        f"Total bots={len(BOT_ACCOUNTS)}"
    )


# =====================================================================
# DASHBOARD
# =====================================================================

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


    # -----------------------------------------------------------------
    # GET
    # -----------------------------------------------------------------

    def do_GET(self):

        parsed = urlparse(
            self.path
        )


        path = parsed.path


        query = parse_qs(
            parsed.query
        )


        # =============================================================
        # HEALTH
        # =============================================================

        if path == "/api/health":

            self.send_json({

                "success":
                    True,

                "online":
                    True
            })

            return


        # =============================================================
        # DASHBOARD API
        # =============================================================

        if path == "/api/dashboard":

            client_token = (

                query.get(
                    "token",
                    [None]
                )[0]
            )


            with ACCOUNTS_LOCK:

                bots = list(
                    BOT_ACCOUNTS.values()
                )


            # ---------------------------------------------------------
            # Client token filtering
            # ---------------------------------------------------------

            if client_token:

                clients_cfg = (
                    load_clients_config()
                )


                target_client_id = None


                for client_id, client_data in clients_cfg.items():

                    if (

                        client_data.get(

                      
