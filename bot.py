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
from urllib.parse import urlencode, urlparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

import requests
import websocket
from dotenv import load_dotenv


load_dotenv()


# ============================================================
# CONFIG
# ============================================================

IST = ZoneInfo("Asia/Kolkata")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
    os.getenv("PORT")
    or os.getenv("DASHBOARD_PORT")
    or "8000"
)

SESSION_START_TIME = dtime(5, 30)
TRADING_START_TIME = dtime(5, 45)

RECONNECT_SECONDS = 5

ENTRY_CONFIRM_TIMEOUT = 10.0
CLOSE_CONFIRM_TIMEOUT = 10.0
POSITION_POLL_INTERVAL = 0.25

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

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True,
)

CACHED_SERVER_IP = "Detecting..."


# ============================================================
# TIME HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


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
                " RAILWAY OUTBOUND IP --> %s",
                ip
            )
            logging.warning(
                " WHITELIST THIS IP IN DELTA EXCHANGE API SETTINGS"
            )
            logging.warning(
                "=================================================="
            )

    except Exception as e:
        logging.warning(
            "IP FETCH ERROR | %s",
            e
        )


def is_weekend(symbol=None, dt=None):
    dt = dt or now_ist()

    weekday = dt.weekday()
    current_time = dt.time()

    if weekday == 5 and current_time >= SESSION_START_TIME:
        return True

    if weekday == 6:
        return True

    if weekday == 0 and current_time < SESSION_START_TIME:
        return True

    return False


def get_current_session_start(dt=None):
    dt = dt or now_ist()

    session_time = dt.replace(
        hour=5,
        minute=30,
        second=0,
        microsecond=0,
    )

    if dt.time() >= SESSION_START_TIME:
        return session_time

    return session_time - timedelta(days=1)


def get_5m_bucket_start(dt=None):
    """
    Returns the start timestamp of the CURRENT running 5-minute candle.
    """

    dt = dt or now_ist()

    ts = int(dt.timestamp())

    return (ts // 300) * 300


# ============================================================
# FILE HELPERS
# ============================================================

def safe_filename(value):
    result = ""

    for char in str(value):
        if char.isalnum() or char in "-_":
            result += char
        else:
            result += "_"

    return result or "account"


def account_state_file(unique_id):
    return os.path.join(
        STATE_DIR,
        safe_filename(unique_id) + ".json"
    )


def account_history_file(unique_id):
    return os.path.join(
        HISTORY_DIR,
        safe_filename(unique_id) + ".json"
    )


def atomic_write_json(filename, data):
    tmp = filename + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=2,
            default=str
        )

    os.replace(tmp, filename)


def load_clients_config():
    if not os.path.exists(CLIENTS_FILE):
        return {}

    try:
        with open(
            CLIENTS_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}

    except Exception as e:
        logging.warning(
            "Client config read error: %s",
            e
        )

        return {}


def save_clients_config(cfg):
    atomic_write_json(
        CLIENTS_FILE,
        cfg
    )


# ============================================================
# DELTA CLIENT
# ============================================================

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

        self.session = requests.Session()

        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "MultiBot/100.0",
        })


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
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "User-Agent": "MultiBot/100.0",
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

        body_text = (
            json.dumps(
                body,
                separators=(",", ":")
            )
            if body is not None
            else ""
        )

        query = (
            "?" + urlencode(
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
            data=body_text
            if body is not None
            else None,
            headers=headers,
            timeout=(3, 8),
        )

        response.raise_for_status()

        data = response.json()

        if data.get("success") is False:
            raise RuntimeError(
                f"Delta error: {data}"
            )

        return data


    def product(self):

        data = self.api(
            "GET",
            f"/v2/products/{self.symbol}"
        )

        result = data.get("result")

        if not isinstance(result, dict):
            raise RuntimeError(
                f"Invalid product response: {data}"
            )

        return result


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
                    "resolution": "1m",
                    "symbol": self.symbol,
                    "start": start_ts,
                    "end": end_ts,
                },
            )

            candles = data.get(
                "result",
                []
            )

            if (
                not isinstance(candles, list)
                or not candles
            ):
                return None, None

            highest = None
            lowest = None

            for candle in candles:

                try:

                    if isinstance(candle, dict):

                        ts_raw = (
                            candle.get("time")
                            or candle.get("timestamp")
                            or candle.get("start")
                        )

                        high_raw = candle.get("high")
                        low_raw = candle.get("low")

                    elif (
                        isinstance(candle, list)
                        and len(candle) >= 4
                    ):

                        ts_raw = candle[0]
                        high_raw = candle[2]
                        low_raw = candle[3]

                    else:
                        continue

                    if ts_raw is not None:

                        ts = float(ts_raw)

                        if ts > 100000000000:
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

            return highest, lowest

        except Exception as e:

            logging.warning(
                "[%s] Session high/low error: %s",
                self.symbol,
                e
            )

            return None, None


    def position(self, product_id):

        data = self.api(
            "GET",
            "/v2/positions",
            params={
                "product_id": int(product_id)
            },
            auth=True,
        )

        result = data.get(
            "result",
            {}
        )

        pos_item = {}

        if isinstance(result, dict):

            pos_item = result

        elif isinstance(result, list):

            for p in result:

                if (
                    isinstance(p, dict)
                    and int(
                        p.get(
                            "product_id",
                            0
                        )
                    )
                    == int(product_id)
                ):

                    pos_item = p
                    break

            if (
                not pos_item
                and result
                and isinstance(result[0], dict)
            ):
                pos_item = result[0]

        if not pos_item:

            return {
                "size": 0,
                "entry_price": None,
                "stop_loss": None,
                "liquidation_price": None,
                "bankruptcy_price": None,
                "margin": None,
                "mark_price": None,
                "unrealized_pnl": 0,
                "leverage": None,
            }

        raw_entry = (
            pos_item.get("entry_price")
            or pos_item.get("entry")
            or pos_item.get("avg_price")
        )

        try:

            entry_val = (
                float(raw_entry)
                if (
                    raw_entry is not None
                    and float(raw_entry) > 0
                )
                else None
            )

        except Exception:
            entry_val = None

        lev_val = (
            pos_item.get("leverage")
            or pos_item.get("user_leverage")
        )


        def fval(key):

            try:

                value = pos_item.get(key)

                return (
                    float(value)
                    if value is not None
                    else None
                )

            except Exception:
                return None


        raw_size = int(
            pos_item.get(
                "size",
                0
            )
            or 0
        )

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
            "size": raw_size,
            "entry_price": entry_val,
            "stop_loss": fval("stop_loss"),
            "liquidation_price": fval(
                "liquidation_price"
            ),
            "bankruptcy_price": fval(
                "bankruptcy_price"
            ),
            "margin": fval("margin"),
            "mark_price": fval("mark_price"),
            "unrealized_pnl": unrealized,
            "leverage": leverage,
        }


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
        product_id,
        leverage_val
    ):

        self.api(
            "POST",
            f"/v2/products/{product_id}/orders/leverage",
            body={
                "leverage": str(leverage_val)
            },
            auth=True,
        )


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
                or "0.001"
            )
        )

        if contract_value <= 0:
            contract_value = Decimal("0.001")

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
                or increment
            )
        )

        if increment <= 0:
            increment = Decimal("1")

        size_decimal = (
            (
                raw / increment
            ).to_integral_value(
                rounding=ROUND_DOWN
            )
            * increment
        )

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


    def cancel_all_orders(
        self,
        product_id
    ):

        try:

            return self.api(
                "DELETE",
                "/v2/orders/all",
                body={
                    "product_id": int(product_id)
                },
                auth=True
            )

        except Exception as e:

            logging.warning(
                "[%s] Cancel all orders failed: %s",
                self.symbol,
                e
            )

            return None


    def make_client_order_id(
        self,
        prefix
    ):

        return (
            f"{prefix}_"
            f"{int(time.time() * 1000)}_"
            f"{uuid.uuid4().hex[:10]}"
        )[-32:]


    def market_entry_pure(
        self,
        product_id,
        side,
        size
    ):

        body = {
            "product_id": int(product_id),
            "product_symbol": self.symbol,
            "size": int(abs(size)),
            "side": side,
            "order_type": "market_order",
            "client_order_id":
                self.make_client_order_id(
                    "entry"
                ),
        }

        logging.info(
            "[%s] MARKET ENTRY | SIDE=%s | SIZE=%s",
            self.symbol,
            side.upper(),
            size
        )

        return self.api(
            "POST",
            "/v2/orders",
            body=body,
            auth=True
        )


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
            "product_id": int(product_id),
            "product_symbol": self.symbol,
            "size": abs(int(size)),
            "side": side,
            "order_type": "market_order",
            "reduce_only": True,
            "client_order_id":
                self.make_client_order_id(
                    "close"
                ),
        }

        logging.info(
            "[%s] MARKET CLOSE | SIDE=%s | SIZE=%s",
            self.symbol,
            side.upper(),
            abs(int(size))
        )

        return self.api(
            "POST",
            "/v2/orders",
            body=body,
            auth=True
        )


    def last_traded_price(self):

        try:

            data = self.api(
                "GET",
                f"/v2/tickers/{self.symbol}"
            )

            result = data.get(
                "result"
            )

            if isinstance(result, dict):

                price = (
                    result.get("close")
                    or result.get("spot_price")
                    or result.get("ltp")
                )

                if price is not None:
                    return Decimal(
                        str(price)
                    )

        except Exception:
            pass

        return None


# ============================================================
# HISTORY
# ============================================================

def load_trade_history(unique_id):

    filename = account_history_file(
        unique_id
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

        return (
            data
            if isinstance(data, list)
            else []
        )

    except Exception:
        return []


def save_trade_history(
    unique_id,
    history
):

    atomic_write_json(
        account_history_file(unique_id),
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
                or "0.001"
            )
        )

        if contract_value <= 0:
            contract_value = Decimal(
                "0.001"
            )

        if direction == "LONG":

            return (
                (exit_val - entry)
                * qty
                * contract_value
            )

        return (
            (entry - exit_val)
            * qty
            * contract_value
        )

    except Exception:
        return Decimal("0")


def calculate_statistics(history):

    def compute_stats(trades):

        total = len(trades)

        wins = [
            t
            for t in trades
            if float(
                t.get("pnl", 0)
                or 0
            ) > 0
        ]

        losses = [
            t
            for t in trades
            if float(
                t.get("pnl", 0)
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
            "total_trades": total,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate":
                (
                    len(wins)
                    / total
                    * 100
                )
                if total
                else 0.0,
            "pnl": float(pnl),
        }


    today = now_ist().strftime(
        "%Y-%m-%d"
    )

    today_trades = [
        t
        for t in history
        if str(
            t.get("date", "")
        ).startswith(today)
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


# ============================================================
# BOT
# ============================================================

class BreakoutSARBot:

    def __init__(
        self,
        account_id,
        account_name,
        account_type,
        api_key,
        api_secret,
        symbol="XAUTUSD",
        subscription=None
    ):

        self.base_account_id = account_id

        self.symbol = (
            symbol.strip().upper()
        )

        self.strategy_key = (
            f"breakout_sar_{self.symbol.lower()}"
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

        self.account_type = account_type

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

        self.session_start = None

        self.day_high = None
        self.day_low = None

        self.last_price = None
        self.prev_price = None

        self.ready = False
        self.trading_armed = False
        self.bot_enabled = False

        self.stop_reason = None
        self.manual_squareoff_flag = False

        self.position = None

        self.stop_loss = 0.0
        self.entry_price = None
        self.size = 0

        self.is_reversal_position = False

        self.base_breakout_ready = True

        # Timestamp of the completed 5m candle
        # currently being used for SL.
        self.last_checked_candle_time = 0

        self.lock = threading.RLock()

        self.order_in_progress = False

        self.execution_uncertain = False
        self.execution_unknown_since = None

        self.last_reconciliation_time = 0.0
        self.last_execution_time = 0.0

        self.leverage = (
            Decimal("200")
            if "BTC" in self.symbol
            else Decimal("100")
        )

        self.balance_fraction = Decimal(
            "0.10"
        )

        self.load_state()
        self.save()


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


    def load_state(self):

        filename = account_state_file(
            self.unique_id
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
                        state["day_high"]
                    )
                )


            if state.get(
                "day_low"
            ) is not None:

                self.day_low = Decimal(
                    str(
                        state["day_low"]
                    )
                )


            self.position = state.get(
                "position"
            )

            self.stop_loss = float(
                state.get(
                    "stop_loss",
                    0
                )
                or 0
            )

            self.entry_price = state.get(
                "entry_price"
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


            self.bot_enabled = bool(
                state.get(
                    "bot_enabled",
                    False
                )
            )

            self.stop_reason = state.get(
                "stop_reason"
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
                "[%s] State load error: %s",
                self.symbol,
                e
            )


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

            logging.info(
                "[%s] PRODUCT READY | ID=%s",
                self.symbol,
                self.product_id
            )

            return True

        except Exception as e:

            logging.error(
                "[%s] Product prepare error: %s",
                self.symbol,
                e
            )

            return False


    # ========================================================
    # 5 MINUTE CANDLE FUNCTIONS
    # ========================================================

    def get_5m_candles(
        self,
        limit=10
    ):

        try:

            end_ts = int(
                now_ist().timestamp()
            )

            start_ts = (
                end_ts
                - limit * 5 * 60
                - 600
            )

            data = self.client.api(
                "GET",
                "/v2/history/candles",
                params={
                    "resolution": "5m",
                    "symbol": self.symbol,
                    "start": start_ts,
                    "end": end_ts,
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
                            candle.get(
                                "time"
                            )
                            or candle.get(
                                "timestamp"
                            )
                            or candle.get(
                                "start"
                            )
                            or 0
                        )

                        if ts > 100000000000:
                            ts /= 1000.0

                        formatted.append({
                            "time": ts,
                            "high": float(
                                candle.get(
                                    "high"
                                )
                            ),
                            "low": float(
                                candle.get(
                                    "low"
                                )
                            ),
                            "close": float(
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
                            "time": ts,
                            "high": float(
                                candle[2]
                            ),
                            "low": float(
                                candle[3]
                            ),
                            "close": float(
                                candle[4]
                            ),
                        })

                except Exception:
                    continue

            formatted.sort(
                key=lambda x: x["time"]
            )

            return formatted

        except Exception as e:

            logging.warning(
                "[%s] 5m candle API error: %s",
                self.symbol,
                e
            )

            return []


    def get_previous_completed_5m_candle(self):
        """
        IMPORTANT:

        Current running candle is NEVER used.

        If current time is 07:23:
            Current candle = 07:20-07:25
            Previous completed = 07:15-07:20

        If current time is 07:25:
            Current candle = 07:25-07:30
            Previous completed = 07:20-07:25
        """

        candles = self.get_5m_candles(
            limit=10
        )

        if not candles:
            return None

        current_bucket = get_5m_bucket_start()

        completed = []

        for candle in candles:

            candle_time = int(
                float(candle["time"])
            )

            if candle_time < current_bucket:
                completed.append(
                    candle
                )

        if not completed:
            return None

        previous_candle = max(
            completed,
            key=lambda x: x["time"]
        )

        return previous_candle


    def get_previous_candle_sl(
        self,
        direction
    ):
        """
        LONG  -> previous completed candle LOW
        SHORT -> previous completed candle HIGH
        """

        candle = (
            self.get_previous_completed_5m_candle()
        )

        if candle is None:
            return None, None

        candle_time = int(
            float(candle["time"])
        )

        if direction == "LONG":

            sl = float(
                candle["low"]
            )

        else:

            sl = float(
                candle["high"]
            )

        return sl, candle_time


    def update_trailing_5m_sl(
        self,
        current_price
    ):
        """
        SL trails to the most recently completed 5m candle.

        LONG:
            SL = LOW of latest completed candle

        SHORT:
            SL = HIGH of latest completed candle

        It updates ONLY when a NEW 5m candle has completed.
        """

        if (
            not self.position
            or self.size <= 0
        ):
            return False

        candle = (
            self.get_previous_completed_5m_candle()
        )

        if candle is None:
            return False

        candle_time = int(
            float(candle["time"])
        )

        # Same completed candle.
        # No need to shift SL again.
        if (
            self.last_checked_candle_time
            == candle_time
        ):
            return False

        old_sl = self.stop_loss

        if self.position == "LONG":

            new_sl = float(
                candle["low"]
            )

        else:

            new_sl = float(
                candle["high"]
            )


        candle_dt = datetime.fromtimestamp(
            candle_time,
            IST
        )


        self.stop_loss = new_sl

        self.last_checked_candle_time = (
            candle_time
        )

        self.save()


        logging.warning(
            "=================================================="
        )

        logging.warning(
            "[%s] 5M TRAILING SL SHIFT",
            self.symbol
        )

        logging.warning(
            "[%s] POSITION = %s",
            self.symbol,
            self.position
        )

        logging.warning(
            "[%s] COMPLETED CANDLE = %s",
            self.symbol,
            candle_dt.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

        logging.warning(
            "[%s] CANDLE HIGH = %s",
            self.symbol,
            candle["high"]
        )

        logging.warning(
            "[%s] CANDLE LOW = %s",
            self.symbol,
            candle["low"]
        )

        logging.warning(
            "[%s] OLD SL = %s",
            self.symbol,
            old_sl
        )

        logging.warning(
            "[%s] NEW SL = %s",
            self.symbol,
            new_sl
        )

        logging.warning(
            "[%s] CURRENT PRICE = %s",
            self.symbol,
            current_price
        )

        logging.warning(
            "=================================================="
        )

        return True


    # ========================================================
    # EXCHANGE POSITION
    # ========================================================

    def read_exchange_position(self):

        if not self.product_id:

            return {
                "size": 0,
                "entry_price": None,
                "stop_loss":
                    self.stop_loss,
                "unrealized_pnl": 0,
                "leverage": None,
                "liquidation_price":
                    None,
            }

        try:

            return self.client.position(
                self.product_id
            )

        except Exception as e:

            logging.warning(
                "[%s] Exchange position read failed: %s",
                self.symbol,
                e
            )

            return None


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

                    if expected_direction is None:
                        return position

            except Exception:
                pass

            time.sleep(
                POSITION_POLL_INTERVAL
            )

        return None


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

                if int(
                    position.get(
                        "size",
                        0
                    )
                    or 0
                ) == 0:

                    return True

            except Exception:
                pass

            time.sleep(
                POSITION_POLL_INTERVAL
            )

        return False


    def reconcile_exchange_state(self):

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
                    "[%s] RECONCILE FAILED: %s",
                    self.symbol,
                    e
                )

                self.execution_uncertain = True
                self.save()

                return False


            exchange_size = int(
                position.get(
                    "size",
                    0
                )
                or 0
            )


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

                self.position = (
                    exchange_direction
                )

                self.size = abs(
                    exchange_size
                )

                if exchange_entry:
                    self.entry_price = float(
                        exchange_entry
                    )

                self.execution_uncertain = False

                self.save()

                logging.info(
                    "[%s] RECONCILED | %s | SIZE=%s | ENTRY=%s",
                    self.symbol,
                    exchange_direction,
                    self.size,
                    self.entry_price
                )

                return True


            self.position = None
            self.entry_price = None
            self.size = 0
            self.stop_loss = 0.0
            self.is_reversal_position = False
            self.base_breakout_ready = True
            self.last_checked_candle_time = 0
            self.execution_uncertain = False

            self.save()

            return True


    def update_settings(
        self,
        new_lev
    ):

        with self.lock:

            try:

                self.leverage = Decimal(
                    str(new_lev)
                )

                if self.product_id:

                    self.client.set_leverage(
                        self.product_id,
                        self.leverage
                    )

                self.save()

                return {
                    "success": True,
                    "message":
                        f"Saved {self.symbol} Settings!"
                }

            except Exception as e:

                return {
                    "success": False,
                    "message": str(e)
                }


    # ========================================================
    # START / STOP
    # ========================================================

    def start_bot(self):

        with self.lock:

            if self.is_expired():

                return {
                    "success": False,
                    "message":
                        "Subscription expired."
                }

            self.manual_squareoff_flag = False
            self.stop_reason = None

            if not self.prepare_product():

                return {
                    "success": False,
                    "message":
                        "Unable to prepare Delta product."
                }

            if not self.reconcile_exchange_state():

                return {
                    "success": False,
                    "message":
                        "Exchange reconciliation failed."
                }

            self.bot_enabled = True

            if (
                self.position is None
                or self.size <= 0
            ):

                self.base_breakout_ready = True

            self.save()

            logging.info(
                "[%s] BOT STARTED | HIGH=%s | LOW=%s",
                self.symbol,
                self.day_high,
                self.day_low
            )

            return {
                "success": True,
                "bot_enabled": True,
                "message":
                    f"Bot for {self.symbol} Started."
            }


    def stop_bot(self):

        with self.lock:

            self.bot_enabled = False
            self.stop_reason = "MANUAL STOP"
            self.manual_squareoff_flag = True

            if (
                self.product_id
                and self.position
                and self.size > 0
            ):

                try:

                    self.order_in_progress = True

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

                        self.client.close_position(
                            self.product_id,
                            exchange_size
                        )

                        self.wait_until_flat()

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
                    self.last_checked_candle_time = 0

                except Exception as e:

                    logging.error(
                        "[%s] Manual close failed: %s",
                        self.symbol,
                        e
                    )

                finally:

                    self.order_in_progress = False

            self.save()

            return {
                "success": True,
                "bot_enabled": False,
                "message":
                    f"Bot for {self.symbol} Stopped."
            }


    # ========================================================
    # SESSION
    # ========================================================

    def check_session_change(
        self,
        now
    ):

        current_session = (
            get_current_session_start(
                now
            )
        )

        if self.session_start == current_session:
            return True

        logging.info(
            "[%s] NEW SESSION | %s",
            self.symbol,
            current_session
        )

        self.session_start = (
            current_session
        )

        self.day_high = None
        self.day_low = None

        self.position = None
        self.entry_price = None
        self.size = 0
        self.stop_loss = 0.0

        self.is_reversal_position = False
        self.base_breakout_ready = True

        self.ready = False
        self.trading_armed = False

        self.prev_price = None
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

                logging.info(
                    "[%s] SESSION LEVELS | HIGH=%s | LOW=%s",
                    self.symbol,
                    self.day_high,
                    self.day_low
                )

        self.save()

        return True


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

                logging.info(
                    "[%s] INITIAL SESSION LEVELS | HIGH=%s | LOW=%s",
                    self.symbol,
                    self.day_high,
                    self.day_low
                )

                self.save()

        return True


    # ========================================================
    # ENTRY
    # ========================================================

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
            or is_weekend(
                self.symbol
            )
        ):
            return False

        if self.order_in_progress:
            return False

        self.order_in_progress = True

        try:

            leverage = self.leverage

            self.client.set_leverage(
                self.product_id,
                leverage
            )

            size = self.client.order_size(
                self.product,
                Decimal(str(price)),
                leverage,
                self.balance_fraction
            )

            side = (
                "buy"
                if direction == "LONG"
                else "sell"
            )

            logging.warning(
                "=================================================="
            )

            logging.warning(
                "[%s] ENTRY SIGNAL",
                self.symbol
            )

            logging.warning(
                "[%s] DIRECTION = %s",
                self.symbol,
                direction
            )

            logging.warning(
                "[%s] PRICE = %s",
                self.symbol,
                price
            )

            logging.warning(
                "[%s] INITIAL 5M SL = %s",
                self.symbol,
                initial_sl
            )

            logging.warning(
                "[%s] SIZE = %s",
                self.symbol,
                size
            )

            logging.warning(
                "[%s] REVERSAL = %s",
                self.symbol,
                is_reversal
            )

            logging.warning(
                "=================================================="
            )

            self.client.market_entry_pure(
                self.product_id,
                side,
                size
            )

            confirmed_position = (
                self.wait_for_position(
                    expected_direction=direction
                )
            )

            if not confirmed_position:

                logging.error(
                    "[%s] ENTRY NOT CONFIRMED",
                    self.symbol
                )

                self.execution_uncertain = True
                self.save()

                return False


            self.position = direction

            self.entry_price = float(
                confirmed_position.get(
                    "entry_price"
                )
                or price
            )

            self.size = abs(
                int(
                    confirmed_position.get(
                        "size",
                        size
                    )
                )
            )

            self.stop_loss = float(
                initial_sl
            )

            self.is_reversal_position = bool(
                is_reversal
            )

            self.base_breakout_ready = False

            self.execution_uncertain = False

            self.last_execution_time = (
                time.time()
            )

            # Mark the exact completed candle
            # from which this initial SL came.
            previous_candle = (
                self.get_previous_completed_5m_candle()
            )

            if previous_candle is not None:

                self.last_checked_candle_time = int(
                    float(
                        previous_candle["time"]
                    )
                )

            else:

                self.last_checked_candle_time = 0

            self.save()

            logging.warning(
                "[%s] ENTRY CONFIRMED | %s | ENTRY=%s | SIZE=%s | SL=%s | CANDLE_TS=%s",
                self.symbol,
                self.position,
                self.entry_price,
                self.size,
                self.stop_loss,
                self.last_checked_candle_time
            )

            return True

        except Exception as e:

            logging.error(
                "[%s] Entry error: %s",
                self.symbol,
                e,
                exc_info=True
            )

            return False

        finally:

            self.order_in_progress = False


    # ========================================================
    # REVERSAL
    # ========================================================

    def reverse_after_stop(
        self,
        stopped_direction,
        price
    ):
        """
        LONG SL -> SHORT
        SHORT SL -> LONG

        Reversal SL uses the SAME previous completed
        5-minute candle logic.
        """

        if (
            not self.bot_enabled
            or self.manual_squareoff_flag
            or self.is_expired()
            or is_weekend(self.symbol)
        ):
            return False

        reverse_direction = (
            "SHORT"
            if stopped_direction == "LONG"
            else "LONG"
        )

        logging.warning(
            "=================================================="
        )

        logging.warning(
            "[%s] REVERSAL START",
            self.symbol
        )

        logging.warning(
            "[%s] STOPPED = %s",
            self.symbol,
            stopped_direction
        )

        logging.warning(
            "[%s] REVERSE = %s",
            self.symbol,
            reverse_direction
        )

        logging.warning(
            "[%s] PRICE = %s",
            self.symbol,
            price
        )

        logging.warning(
            "=================================================="
        )


        reversal_sl, candle_time = (
            self.get_previous_candle_sl(
                reverse_direction
            )
        )

        if (
            reversal_sl is None
            or candle_time is None
        ):

            logging.error(
                "[%s] REVERSAL CANCELLED | Previous completed 5m candle unavailable",
                self.symbol
            )

            self.base_breakout_ready = True
            self.save()

            return False


        # LONG needs SL below current price.
        if (
            reverse_direction == "LONG"
            and reversal_sl >= price
        ):

            logging.error(
                "[%s] REVERSAL LONG CANCELLED | SL=%s >= PRICE=%s",
                self.symbol,
                reversal_sl,
                price
            )

            self.base_breakout_ready = True
            self.save()

            return False


        # SHORT needs SL above current price.
        if (
            reverse_direction == "SHORT"
            and reversal_sl <= price
        ):

            logging.error(
                "[%s] REVERSAL SHORT CANCELLED | SL=%s <= PRICE=%s",
                self.symbol,
                reversal_sl,
                price
            )

            self.base_breakout_ready = True
            self.save()

            return False


        self.last_checked_candle_time = (
            candle_time
        )

        self.save()


        return self.enter(
            reverse_direction,
            price,
            reversal_sl,
            is_reversal=True
        )


    # ========================================================
    # CLOSE
    # ========================================================

    def close_current_position(
        self,
        reason,
        exit_price
    ):

        if (
            not self.position
            or self.size <= 0
        ):
            return True

        if self.order_in_progress:
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

            if exchange_size != 0:

                self.client.close_position(
                    self.product_id,
                    exchange_size
                )

                flat = (
                    self.wait_until_flat()
                )

                if not flat:

                    logging.error(
                        "[%s] CLOSE NOT CONFIRMED",
                        self.symbol
                    )

                    self.execution_uncertain = True
                    self.save()

                    return False


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

            self.last_checked_candle_time = 0

            self.execution_uncertain = False

            self.save()

            logging.info(
                "[%s] POSITION CLOSED | REASON=%s | EXIT=%s",
                self.symbol,
                reason,
                exit_price
            )

            return True

        except Exception as e:

            logging.error(
                "[%s] Close error: %s",
                self.symbol,
                e,
                exc_info=True
            )

            return False

        finally:

            self.order_in_progress = False


    # ========================================================
    # MAIN STRATEGY
    # ========================================================

    def evaluate(
        self,
        price=None
    ):

        with self.lock:

            if (
                not self.bot_enabled
                or self.is_expired()
            ):
                return

            now = now_ist()

            if is_weekend(
                self.symbol,
                now
            ):
                return


            # ------------------------------------------------
            # LIVE PRICE
            # ------------------------------------------------

            if price is None:

                price = (
                    self.client.last_traded_price()
                )

                if price is None:
                    return

            try:

                new_price = float(
                    price
                )

            except Exception:

                return

            if new_price <= 0:
                return

            self.last_price = new_price


            # ------------------------------------------------
            # SESSION
            # ------------------------------------------------

            if not self.check_session_change(
                now
            ):
                return


            if not self.prepare(
                now
            ):
                return


            if (
                self.day_high is None
                or self.day_low is None
            ):
                return


            # ------------------------------------------------
            # BEFORE 05:45
            # ------------------------------------------------

            if (
                now.time()
                < TRADING_START_TIME
            ):

                self.prev_price = (
                    new_price
                )

                return


            # ------------------------------------------------
            # ARM
            # ------------------------------------------------

            if not self.trading_armed:

                self.trading_armed = True

                logging.info(
                    "[%s] TRADING ARMED | PRICE=%s | HIGH=%s | LOW=%s",
                    self.symbol,
                    new_price,
                    self.day_high,
                    self.day_low
                )

                self.save()


            current_high = float(
                self.day_high
            )

            current_low = float(
                self.day_low
            )

            previous_price = (
                self.prev_price
            )


            # =================================================
            # OPEN LONG
            # =================================================

            if (
                self.position == "LONG"
                and self.size > 0
            ):

                # FIRST:
                # If a NEW 5-minute candle has completed,
                # shift SL to its LOW.
                self.update_trailing_5m_sl(
                    new_price
                )


                # THEN:
                # Check the NEW SL immediately.
                if (
                    self.stop_loss > 0
                    and new_price <= self.stop_loss
                ):

                    stopped_direction = (
                        self.position
                    )

                    logging.warning(
                        "[%s] LONG 5M TRAILING SL HIT | PRICE=%s | SL=%s",
                        self.symbol,
                        new_price,
                        self.stop_loss
                    )

                    closed = (
                        self.close_current_position(
                            "SL_HIT",
                            new_price
                        )
                    )

                    if closed:

                        self.reverse_after_stop(
                            stopped_direction,
                            new_price
                        )

                self.prev_price = (
                    new_price
                )

                return


            # =================================================
            # OPEN SHORT
            # =================================================

            if (
                self.position == "SHORT"
                and self.size > 0
            ):

                # FIRST:
                # If a NEW 5-minute candle has completed,
                # shift SL to its HIGH.
                self.update_trailing_5m_sl(
                    new_price
                )


                # THEN:
                # Check the NEW SL immediately.
                if (
                    self.stop_loss > 0
                    and new_price >= self.stop_loss
                ):

                    stopped_direction = (
                        self.position
                    )

                    logging.warning(
                        "[%s] SHORT 5M TRAILING SL HIT | PRICE=%s | SL=%s",
                        self.symbol,
                        new_price,
                        self.stop_loss
                    )

                    closed = (
                        self.close_current_position(
                            "SL_HIT",
                            new_price
                        )
                    )

                    if closed:

                        self.reverse_after_stop(
                            stopped_direction,
                            new_price
                        )

                self.prev_price = (
                    new_price
                )

                return


            # =================================================
            # NO POSITION
            # =================================================

            if not (
                self.position is None
                and self.size == 0
                and self.base_breakout_ready
            ):

                self.prev_price = (
                    new_price
                )

                return


            # =================================================
            # DAY HIGH BREAKOUT -> LONG
            # =================================================

            high_break = False

            if previous_price is None:

                if new_price > current_high:
                    high_break = True

            else:

                if (
                    previous_price <= current_high
                    and new_price > current_high
                ):
                    high_break = True


            if high_break:

                logging.warning(
                    "=================================================="
                )

                logging.warning(
                    "[%s] *** DAY HIGH BREAKOUT ***",
                    self.symbol
                )

                logging.warning(
                    "[%s] PREV PRICE = %s",
                    self.symbol,
                    previous_price
                )

                logging.warning(
                    "[%s] DAY HIGH = %s",
                    self.symbol,
                    current_high
                )

                logging.warning(
                    "[%s] LIVE PRICE = %s",
                    self.symbol,
                    new_price
                )

                logging.warning(
                    "[%s] ACTION = LONG",
                    self.symbol
                )

                logging.warning(
                    "=================================================="
                )


                # EXACT previous completed 5m candle LOW
                initial_sl, candle_time = (
                    self.get_previous_candle_sl(
                        "LONG"
                    )
                )


                if (
                    initial_sl is None
                    or candle_time is None
                ):

                    logging.error(
                        "[%s] LONG ENTRY CANCELLED | Previous completed 5m candle unavailable",
                        self.symbol
                    )

                    self.prev_price = new_price
                    return


                if initial_sl >= new_price:

                    logging.error(
                        "[%s] LONG ENTRY CANCELLED | PREVIOUS 5M LOW=%s >= ENTRY=%s",
                        self.symbol,
                        initial_sl,
                        new_price
                    )

                    self.prev_price = new_price
                    return


                self.day_high = Decimal(
                    str(new_price)
                )

                self.last_checked_candle_time = (
                    candle_time
                )

                self.save()


                self.enter(
                    "LONG",
                    new_price,
                    initial_sl,
                    is_reversal=False
                )

                self.prev_price = (
                    new_price
                )

                return


            # =================================================
            # DAY LOW BREAKDOWN -> SHORT
            # =================================================

            low_break = False

            if previous_price is None:

                if new_price < current_low:
                    low_break = True

            else:

                if (
                    previous_price >= current_low
                    and new_price < current_low
                ):
                    low_break = True


            if low_break:

                logging.warning(
                    "=================================================="
                )

                logging.warning(
                    "[%s] *** DAY LOW BREAKDOWN ***",
                    self.symbol
                )

                logging.warning(
                    "[%s] PREV PRICE = %s",
                    self.symbol,
                    previous_price
                )

                logging.warning(
                    "[%s] DAY LOW = %s",
                    self.symbol,
                    current_low
                )

                logging.warning(
                    "[%s] LIVE PRICE = %s",
                    self.symbol,
                    new_price
                )

                logging.warning(
                    "[%s] ACTION = SHORT",
                    self.symbol
                )

                logging.warning(
                    "=================================================="
                )


                # EXACT previous completed 5m candle HIGH
                initial_sl, candle_time = (
                    self.get_previous_candle_sl(
                        "SHORT"
                    )
                )


                if (
                    initial_sl is None
                    or candle_time is None
                ):

                    logging.error(
                        "[%s] SHORT ENTRY CANCELLED | Previous completed 5m candle unavailable",
                        self.symbol
                    )

                    self.prev_price = new_price
                    return


                if initial_sl <= new_price:

                    logging.error(
                        "[%s] SHORT ENTRY CANCELLED | PREVIOUS 5M HIGH=%s <= ENTRY=%s",
                        self.symbol,
                        initial_sl,
                        new_price
                    )

                    self.prev_price = new_price
                    return


                self.day_low = Decimal(
                    str(new_price)
                )

                self.last_checked_candle_time = (
                    candle_time
                )

                self.save()


                self.enter(
                    "SHORT",
                    new_price,
                    initial_sl,
                    is_reversal=False
                )

                self.prev_price = (
                    new_price
                )

                return


            self.prev_price = (
                new_price
            )


    # ========================================================
    # TRADE HISTORY
    # ========================================================

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
                f"{self.symbol.lower()}_"
                f"{int(time.time() * 1000)}",

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
                float(self.entry_price),

            "exit_price":
                float(exit_price),

            "size":
                self.size,

            "pnl":
                float(pnl),

            "reason":
                reason,
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

        logging.info(
            "[%s] TRADE FINISHED | %s | ENTRY=%s | EXIT=%s | PNL=%s | REASON=%s",
            self.symbol,
            self.position,
            self.entry_price,
            exit_price,
            pnl,
            reason
        )


# ============================================================
# ACCOUNTS
# ============================================================

BOT_ACCOUNTS = {}

ACCOUNTS_LOCK = threading.RLock()


def create_all_accounts():

    new_accounts = {}


    # PRIMARY
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


    # CLIENTS
    clients_cfg = (
        load_clients_config()
    )

    for client_id, client_data in (
        clients_cfg.items()
    ):

        if not isinstance(
            client_data,
            dict
        ):
            continue


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


def load_all_accounts():

    global BOT_ACCOUNTS

    with ACCOUNTS_LOCK:

        BOT_ACCOUNTS = (
            create_all_accounts()
        )

        logging.info(
            "=================================================="
        )

        logging.info(
            "TOTAL BOTS LOADED = %s",
            len(BOT_ACCOUNTS)
        )

        for bot in BOT_ACCOUNTS.values():

            logging.info(
                "BOT = %s | SYMBOL=%s",
                bot.unique_id,
                bot.symbol
            )

        logging.info(
            "=================================================="
        )


def get_bot(unique_id):

    with ACCOUNTS_LOCK:

        return BOT_ACCOUNTS.get(
            unique_id
        )


# ============================================================
# DASHBOARD SERIALIZATION
# ============================================================

def serialize_bot(bot):

    position = (
        bot.read_exchange_position()
    )

    if position is None:

        position = {
            "size":
                bot.size,

            "entry_price":
                bot.entry_price,

            "unrealized_pnl":
                0
        }


    history = load_trade_history(
        bot.unique_id
    )

    stats = calculate_statistics(
        history
    )


    return {

        "id":
            bot.unique_id,

        "unique_id":
            bot.unique_id,

        "account_id":
            bot.base_account_id,

        "account_name":
            bot.account_name,

        "account_type":
            bot.account_type,

        "symbol":
            bot.symbol,

        "bot_enabled":
            bot.bot_enabled,

        "expired":
            bot.is_expired(),

        "last_price":
            bot.last_price,

        "day_high":
            float(bot.day_high)
            if bot.day_high is not None
            else None,

        "day_low":
            float(bot.day_low)
            if bot.day_low is not None
            else None,

        "local_position":
            bot.position,

        "size":
            abs(
                int(
                    position.get(
                        "size",
                        0
                    )
                )
            ),

        "entry_price":
            position.get(
                "entry_price"
            ),

        "stop_loss":
            bot.stop_loss,

        "unrealized_pnl":
            position.get(
                "unrealized_pnl",
                0
            ),

        "leverage":
            int(bot.leverage),

        "stats":
            stats,

        "history":
            history[-20:],

        "server_ip":
            CACHED_SERVER_IP,
    }


# ============================================================
# DASHBOARD SERVER
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


    def send_json(
        self,
        payload,
        status=200
    ):

        raw = json.dumps(
            payload,
            default=str
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

        self.end_headers()

        self.wfile.write(
            raw
        )


    def read_json_body(self):

        try:

            length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

        except Exception:

            length = 0


        if length <= 0:
            return {}


        try:

            return json.loads(
                self.rfile.read(
                    length
                ).decode("utf-8")
            )

        except Exception:

            return {}


    def do_GET(self):

        parsed = urlparse(
            self.path
        )


        if parsed.path == "/api/dashboard":

            with ACCOUNTS_LOCK:

                bots = list(
                    BOT_ACCOUNTS.values()
                )


            self.send_json({

                "success":
                    True,

                "server_ip":
                    CACHED_SERVER_IP,

                "bots":
                    [
                        serialize_bot(bot)
                        for bot in bots
                    ],
            })

            return


        super().do_GET()


    def do_POST(self):

        parsed = urlparse(
            self.path
        )

        body = self.read_json_body()


        # START
        if parsed.path == "/api/start":

            bot = get_bot(
                body.get("id")
                or body.get("unique_id")
            )

            if not bot:

                return self.send_json(
                    {
                        "success": False,
                        "message":
                            "Bot not found"
                    },
                    404
                )

            return self.send_json(
                bot.start_bot()
            )


        # STOP
        if parsed.path == "/api/stop":

            bot = get_bot(
                body.get("id")
                or body.get("unique_id")
            )

            if not bot:

                return self.send_json(
                    {
                        "success": False,
                        "message":
                            "Bot not found"
                    },
                    404
                )

            return self.send_json(
                bot.stop_bot()
            )


        # ADD CLIENT
        if parsed.path == "/api/client/add":

            client_id = str(
                body.get("client_id")
                or body.get("id")
                or uuid.uuid4().hex[:10]
            ).strip()

            name = str(
                body.get("name")
                or "Client"
            ).strip()

            api_key = str(
                body.get("api_key")
                or ""
            ).strip()

            api_secret = str(
                body.get("api_secret")
                or ""
            ).strip()


            if (
                not api_key
                or not api_secret
            ):

                return self.send_json(
                    {
                        "success": False,
                        "message":
                            "API key and secret required"
                    },
                    400
                )


            clients_cfg = (
                load_clients_config()
            )


            clients_cfg[
                client_id
            ] = {

                "name":
                    name,

                "api_key":
                    api_key,

                "api_secret":
                    api_secret,

                "subscription_start":
                    body.get(
                        "subscription_start"
                    ),

                "subscription_expiry":
                    body.get(
                        "subscription_expiry"
                    ),
            }


            save_clients_config(
                clients_cfg
            )

            load_all_accounts()


            return self.send_json({

                "success":
                    True,

                "message":
                    "Client added successfully"
            })


        self.send_json(
            {
                "success": False,
                "message":
                    "Endpoint not found"
            },
            404
        )


# ============================================================
# WEBSOCKET
# ============================================================

def extract_trade(message):

    try:

        data = json.loads(
            message
        )


        item = (
            data.get("payload")
            or data.get("data")
            or data
        )


        if isinstance(item, list):

            if not item:
                return None, None

            item = item[0]


        if not isinstance(
            item,
            dict
        ):

            return None, None


        symbol = (
            item.get("sy")
            or item.get("symbol")
            or ""
        )


        price = (
            item.get("p")
            or item.get("price")
        )


        if not symbol:
            return None, None

        if price is None:
            return None, None


        price_float = float(
            price
        )


        if price_float <= 0:
            return None, None


        return (
            str(symbol).upper(),
            price_float
        )


    except Exception as e:

        logging.debug(
            "Trade parse error: %s",
            e
        )

        return None, None


def websocket_on_open(ws):

    logging.info(
        "=================================================="
    )

    logging.info(
        "DELTA WEBSOCKET CONNECTED"
    )

    logging.info(
        "SUBSCRIBING XAUTUSD + BTCUSD TRADES"
    )

    logging.info(
        "=================================================="
    )


    subscribe_message = {

        "type":
            "subscribe",

        "payload": {

            "channels": [

                {
                    "name":
                        "trades",

                    "symbols": [
                        "XAUTUSD",
                        "BTCUSD"
                    ]
                }
            ]
        }
    }


    ws.send(
        json.dumps(
            subscribe_message
        )
    )


def websocket_on_message(
    ws,
    message
):

    try:

        symbol, price = (
            extract_trade(
                message
            )
        )


        if not symbol or price is None:
            return


        with ACCOUNTS_LOCK:

            bots = list(
                BOT_ACCOUNTS.values()
            )


        for bot in bots:

            if (
                bot.symbol
                == symbol
                and bot.bot_enabled
            ):

                try:

                    bot.evaluate(
                        price
                    )

                except Exception as e:

                    logging.error(
                        "[%s] EVALUATE ERROR: %s",
                        bot.symbol,
                        e,
                        exc_info=True
                    )


    except Exception as e:

        logging.error(
            "WEBSOCKET MESSAGE ERROR: %s",
            e,
            exc_info=True
        )


def websocket_on_error(
    ws,
    error
):

    logging.error(
        "DELTA WEBSOCKET ERROR | %s",
        error
    )


def websocket_on_close(
    ws,
    close_status_code,
    close_msg
):

    logging.warning(
        "DELTA WEBSOCKET CLOSED | CODE=%s | MSG=%s",
        close_status_code,
        close_msg
    )


def run_websocket_forever():

    while True:

        try:

            logging.info(
                "Connecting Delta WebSocket..."
            )


            ws = websocket.WebSocketApp(

                WS_URL,

                on_open=
                    websocket_on_open,

                on_message=
                    websocket_on_message,

                on_error=
                    websocket_on_error,

                on_close=
                    websocket_on_close,
            )


            ws.run_forever(
                ping_interval=20,
                ping_timeout=10
            )


        except Exception as e:

            logging.error(
                "WEBSOCKET LOOP ERROR | %s",
                e,
                exc_info=True
            )


        logging.info(
            "WebSocket reconnecting in %s seconds...",
            RECONNECT_SECONDS
        )

        time.sleep(
            RECONNECT_SECONDS
        )


# ============================================================
# MAIN
# ============================================================

def main():

    logging.info(
        "=================================================="
    )

    logging.info(
        "MULTI BOT STARTING"
    )

    logging.info(
        "5M TRAILING SL + REVERSAL ENABLED"
    )

    logging.info(
        "BASE URL = %s",
        BASE_URL
    )

    logging.info(
        "WS URL = %s",
        WS_URL
    )

    logging.info(
        "PORT = %s",
        DASHBOARD_PORT
    )

    logging.info(
        "SESSION START = %s",
        SESSION_START_TIME
    )

    logging.info(
        "TRADING START = %s",
        TRADING_START_TIME
    )

    logging.info(
        "=================================================="
    )


    update_server_ip()


    load_all_accounts()


    websocket_thread = threading.Thread(
        target=run_websocket_forever,
        name="ws",
        daemon=True
    )

    websocket_thread.start()


    logging.info(
        "WEBSOCKET THREAD STARTED"
    )


    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            DASHBOARD_PORT
        ),
        DashboardHandler
    )


    logging.info(
        "=================================================="
    )

    logging.info(
        "DASHBOARD SERVER STARTED | PORT=%s",
        DASHBOARD_PORT
    )

    logging.info(
        "=================================================="
    )


    try:

        server.serve_forever()

    except KeyboardInterrupt:

        logging.info(
            "SERVER STOPPING..."
        )

    finally:

        server.server_close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
