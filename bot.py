import os
import time
import json
import hmac
import hashlib
import logging
import threading
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
# IMPORTANT STRATEGY RULES
#
# 1. Saturday/Sunday:
#       NO NEW TRADES.
#       Any open position is closed.
#
# 2. BASE LONG:
#       Only when price creates a NEW running session HIGH.
#
# 3. BASE SHORT:
#       Only when price creates a NEW running session LOW.
#
# 4. BASE LONG SL:
#       Previous completed 5M candle LOW.
#
# 5. BASE SHORT SL:
#       Previous completed 5M candle HIGH.
#
# 6. TRAILING SL:
#       Every new completed 5M candle:
#           LONG  -> previous completed candle LOW
#           SHORT -> previous completed candle HIGH
#
# 7. BASE SL:
#       Base LONG SL -> exactly ONE SHORT reversal.
#       Base SHORT SL -> exactly ONE LONG reversal.
#
# 8. REVERSAL SL:
#       Close position -> FLAT.
#       NEVER create another reversal.
#
# 9. OLD BREAKOUT:
#       Cannot immediately trigger another base trade.
#       A genuinely NEW running session high/low is required.
#
# 10. EXCHANGE CONFIRMATION:
#       Entry is not considered active until exchange confirms position.
#       Close is not considered complete until exchange confirms FLAT.
#
# 11. DASHBOARD:
#       Dashboard refresh can READ exchange position,
#       but must NEVER reset strategy state.
#
# 12. NO REST TICKER STRATEGY LOOP:
#       Public websocket is the strategy price feed.
#
# =====================================================================


load_dotenv()

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
    os.getenv("DASHBOARD_PORT", "8000")
)

TRADING_START_TIME = dtime(5, 45)
SESSION_START_TIME = dtime(5, 30)

RECONNECT_SECONDS = 3

# Exchange confirmation settings.
ENTRY_CONFIRM_TIMEOUT = 6.0
CLOSE_CONFIRM_TIMEOUT = 6.0
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


def is_weekend(symbol=None, dt=None):
    """
    Both BTCUSD and XAUTUSD are blocked during weekend.

    Saturday:
        05:30 IST onward = blocked

    Sunday:
        Entire day = blocked

    Monday:
        Before 05:30 IST = blocked
    """

    dt = dt or now_ist()

    wday = dt.weekday()
    t = dt.time()

    # Saturday after session boundary.
    if wday == 5 and t >= SESSION_START_TIME:
        return True

    # Entire Sunday.
    if wday == 6:
        return True

    # Monday before new session.
    if wday == 0 and t < SESSION_START_TIME:
        return True

    return False


def get_current_session_start(dt=None):
    dt = dt or now_ist()

    m530 = dt.replace(
        hour=5,
        minute=30,
        second=0,
        microsecond=0
    )

    if dt.time() >= SESSION_START_TIME:
        return m530

    return m530 - timedelta(days=1)


def safe_filename(value):
    result = ""

    for char in str(value):
        if char.isalnum() or char in ("-", "_"):
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

    except Exception:
        return {}


def save_clients_config(cfg):
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

        self.session = requests.Session()

        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "MultiBot/97.0",
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
            "User-Agent": "MultiBot/97.0",
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

            for c in candles:

                try:

                    if isinstance(c, dict):

                        ts_raw = (
                            c.get("time")
                            or c.get("timestamp")
                            or c.get("start")
                        )

                        h_raw = c.get("high")
                        l_raw = c.get("low")

                    elif (
                        isinstance(c, list)
                        and len(c) >= 4
                    ):

                        ts_raw = c[0]
                        h_raw = c[2]
                        l_raw = c[3]

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

                    h = Decimal(str(h_raw))
                    l = Decimal(str(l_raw))

                    if (
                        h > 0
                        and (
                            highest is None
                            or h > highest
                        )
                    ):
                        highest = h

                    if (
                        l > 0
                        and (
                            lowest is None
                            or l < lowest
                        )
                    ):
                        lowest = l

                except Exception:
                    continue

            return highest, lowest

        except Exception as e:

            logging.warning(
                f"[{self.symbol}] "
                f"Session high/low fetch error: {e}"
            )

            return None, None


    def position(
        self,
        product_id
    ):
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
                        ) or 0
                    ) == int(product_id)
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
            or pos_item.get("average_price")
            or pos_item.get("price")
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
            or pos_item.get("effective_leverage")
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


        return {
            "size": int(
                pos_item.get(
                    "size",
                    0
                ) or 0
            ),

            "entry_price": entry_val,

            "stop_loss": fval(
                "stop_loss"
            ),

            "liquidation_price": fval(
                "liquidation_price"
            ),

            "bankruptcy_price": fval(
                "bankruptcy_price"
            ),

            "margin": fval(
                "margin"
            ),

            "mark_price": fval(
                "mark_price"
            ),

            "unrealized_pnl": float(
                pos_item.get(
                    "unrealized_pnl",
                    0
                ) or 0
            ),

            "leverage": (
                int(lev_val)
                if lev_val
                else None
            ),
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


    def set_leverage(
        self,
        product_id,
        leverage_val
    ):
        self.api(
            "POST",
            f"/v2/products/{product_id}/orders/leverage",
            body={
                "leverage": str(
                    leverage_val
                )
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

        bal = self.balance()

        margin = (
            bal
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


    def cancel_all_orders(
        self,
        product_id
    ):
        try:

            self.api(
                "DELETE",
                "/v2/orders/all",
                body={
                    "product_id": int(
                        product_id
                    )
                },
                auth=True,
            )

        except Exception:
            pass


    def market_entry_pure(
        self,
        product_id,
        side,
        size
    ):

        body = {
            "product_id": int(
                product_id
            ),

            "product_symbol": self.symbol,

            "size": int(
                abs(size)
            ),

            "side": side,

            "order_type": "market_order",

            "client_order_id": (
                f"entry_"
                f"{int(time.time() * 1000)}"
            )[-32:],
        }

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
            "product_id": int(
                product_id
            ),

            "product_symbol": self.symbol,

            "size": abs(
                int(size)
            ),

            "side": side,

            "order_type": "market_order",

            "reduce_only": True,

            "client_order_id": (
                f"close_"
                f"{int(time.time() * 1000)}"
            )[-32:],
        }

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

            res = data.get(
                "result"
            )

            if isinstance(
                res,
                dict
            ):

                p = (
                    res.get("close")
                    or res.get("spot_price")
                    or res.get("ltp")
                )

                if p is not None:
                    return Decimal(
                        str(p)
                    )

        except Exception:
            pass

        return None


# =====================================================================
# HISTORY / STATS
# =====================================================================

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

        cv = Decimal(
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

        if cv <= 0:
            cv = Decimal("0.001")

        if direction == "LONG":

            return (
                (exit_val - entry)
                * qty
                * cv
            )

        return (
            (entry - exit_val)
            * qty
            * cv
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

            "winning_trades": len(
                wins
            ),

            "losing_trades": len(
                losses
            ),

            "win_rate": (
                len(wins)
                / total
                * 100
            )
            if total
            else 0.0,

            "pnl": float(pnl),
        }


    today_str = now_ist().strftime(
        "%Y-%m-%d"
    )

    today_trades = [
        t
        for t in history
        if str(
            t.get(
                "date",
                ""
            )
        ).startswith(
            today_str
        )
    ]

    return {
        "today": compute_stats(
            today_trades
        ),

        "all_time": compute_stats(
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


        # -------------------------------------------------------------
        # LOCAL STRATEGY POSITION
        # -------------------------------------------------------------

        self.position = None

        self.stop_loss = 0.0

        self.entry_price = None

        self.size = 0


        # Timestamp of latest completed candle processed.
        self.last_checked_candle_time = 0


        # True ONLY when current position is the ONE allowed reversal.
        self.is_reversal_position = False


        # Base breakout is allowed only when flat.
        self.base_breakout_ready = True


        # Last websocket strategy price.
        self.last_strategy_price = None


        # -------------------------------------------------------------
        # HARD EXECUTION GUARDS
        # -------------------------------------------------------------

        # Prevent simultaneous strategy operations.
        self.lock = threading.RLock()

        # Separate flag to make order execution explicit.
        self.order_in_progress = False

        # Unique local execution sequence.
        self.last_execution_time = 0.0


        self.leverage = (
            Decimal("200")
            if "BTC" in self.symbol
            else Decimal("100")
        )

        self.balance_fraction = (
            Decimal("0.10")
        )


        self.load_state()

        self.save()


    # -----------------------------------------------------------------
    # SUBSCRIPTION
    # -----------------------------------------------------------------

    def is_expired(self):

        if self.account_type == "primary":
            return False

        expiry_str = (
            self.subscription.get(
                "expiry"
            )
        )

        if not expiry_str:
            return False

        try:

            return (
                now_ist().date()
                >
                datetime.strptime(
                    expiry_str,
                    "%Y-%m-%d"
                ).date()
            )

        except Exception:
            return False


    # -----------------------------------------------------------------
    # STATE
    # -----------------------------------------------------------------

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
                    0.0
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
            # IMPORTANT SAFETY:
            #
            # We DO NOT automatically change strategy state based only
            # on persisted local data.
            #
            # If local state says flat, we remain flat.
            #
            # Dashboard/exchange refresh is NOT allowed to re-arm
            # the strategy.
            # ---------------------------------------------------------

            if (
                not self.position
                or self.size <= 0
            ):

                self.position = None

                self.size = 0

                self.entry_price = None

                self.is_reversal_position = False

                self.base_breakout_ready = True


        except Exception as e:

            logging.warning(
                f"[{self.symbol}] "
                f"State load error: {e}"
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
        }

        atomic_write_json(
            account_state_file(
                self.unique_id
            ),
            data
        )


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

            for c in candles:

                try:

                    if isinstance(
                        c,
                        dict
                    ):

                        ts = float(
                            c.get("time")
                            or c.get("timestamp")
                            or c.get("start")
                            or 0
                        )

                        if ts > 100000000000:
                            ts /= 1000.0

                        formatted.append({
                            "time": ts,
                            "high": float(
                                c.get("high")
                            ),
                            "low": float(
                                c.get("low")
                            ),
                            "close": float(
                                c.get("close")
                            ),
                        })


                    elif (
                        isinstance(c, list)
                        and len(c) >= 5
                    ):

                        ts = float(
                            c[0]
                        )

                        if ts > 100000000000:
                            ts /= 1000.0

                        formatted.append({
                            "time": ts,
                            "high": float(c[2]),
                            "low": float(c[3]),
                            "close": float(c[4]),
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
    # POSITION READ ONLY
    # -----------------------------------------------------------------

    def read_exchange_position(self):

        if not self.product_id:
            return {
                "size": 0,
                "entry_price": None,
                "stop_loss": self.stop_loss,
                "unrealized_pnl": 0,
                "leverage": None,
                "liquidation_price": None,
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

            return {
                "size": 0,
                "entry_price": None,
                "stop_loss": self.stop_loss,
                "unrealized_pnl": 0,
                "leverage": None,
                "liquidation_price": None,
            }


    # -----------------------------------------------------------------
    # WAIT FOR EXCHANGE POSITION
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

        last_pos = None

        while time.time() < deadline:

            try:

                pos = self.client.position(
                    self.product_id
                )

                last_pos = pos

                ex_size = int(
                    pos.get(
                        "size",
                        0
                    )
                    or 0
                )

                if ex_size != 0:

                    if (
                        expected_direction
                        == "LONG"
                        and ex_size > 0
                    ):
                        return pos

                    if (
                        expected_direction
                        == "SHORT"
                        and ex_size < 0
                    ):
                        return pos

                    if expected_direction is None:
                        return pos

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

                pos = self.client.position(
                    self.product_id
                )

                ex_size = int(
                    pos.get(
                        "size",
                        0
                    )
                    or 0
                )

                if ex_size == 0:
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

                self.balance_fraction = Decimal(
                    str(new_frac)
                )

                if self.product_id:

                    self.client.set_leverage(
                        self.product_id,
                        self.leverage
                    )

                self.save()

                return {
                    "success": True,
                    "message": (
                        f"Saved "
                        f"{self.symbol} Settings! "
                        f"Lev: "
                        f"{int(self.leverage)}x | "
                        f"Margin: "
                        f"{float(self.balance_fraction) * 100}%"
                    ),
                }

            except Exception as e:

                return {
                    "success": False,
                    "message": str(e)
                }


    # -----------------------------------------------------------------
    # START
    # -----------------------------------------------------------------

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

            self.bot_enabled = True


            # Only local flat state can start a new base cycle.
            if (
                not self.position
                or self.size <= 0
            ):

                self.base_breakout_ready = True


            self.save()


            return {
                "success": True,
                "bot_enabled": True,
                "message":
                    f"Bot for {self.symbol} Started.",
            }


    # -----------------------------------------------------------------
    # STOP
    # -----------------------------------------------------------------

    def stop_bot(self):

        with self.lock:

            self.bot_enabled = False

            self.stop_reason = (
                "MANUAL STOP"
            )

            self.manual_squareoff_flag = True


            # ---------------------------------------------------------
            # IMPORTANT:
            # Close must be confirmed.
            # We DO NOT mark local position flat if close failed.
            # ---------------------------------------------------------

            if (
                self.position
                and self.size > 0
                and self.product_id
            ):

                old_position = (
                    self.position
                )

                old_size = (
                    self.size
                )

                try:

                    close_sz = (
                        old_size
                        if old_position == "LONG"
                        else -old_size
                    )

                    self.client.close_position(
                        self.product_id,
                        close_sz
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

                    else:

                        logging.error(
                            f"[{self.symbol}] "
                            f"MANUAL STOP: "
                            f"exchange did NOT confirm flat."
                        )

                except Exception as e:

                    logging.error(
                        f"[{self.symbol}] "
                        f"Manual close failed: {e}"
                    )


            else:

                self.position = None
                self.entry_price = None
                self.size = 0
                self.stop_loss = 0.0
                self.is_reversal_position = False
                self.base_breakout_ready = True


            self.save()


            return {
                "success": True,
                "bot_enabled": False,
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

        current_sess = (
            get_current_session_start(
                now
            )
        )

        if (
            self.session_start
            == current_sess
        ):
            return


        if self.product_id:

            try:

                self.client.cancel_all_orders(
                    self.product_id
                )

            except Exception:
                pass


        # -------------------------------------------------------------
        # If there is an actual exchange position, close it first.
        # Never simply erase local state.
        # -------------------------------------------------------------

        if (
            self.product_id
            and self.position
            and self.size > 0
        ):

            try:

                exchange_pos = (
                    self.client.position(
                        self.product_id
                    )
                )

                ex_size = int(
                    exchange_pos.get(
                        "size",
                        0
                    )
                    or 0
                )

                if ex_size != 0:

                    close_sz = (
                        ex_size
                    )

                    self.client.close_position(
                        self.product_id,
                        close_sz
                    )

                    confirmed = (
                        self.wait_until_flat()
                    )

                    if confirmed:

                        self.finish_trade(
                            "SESSION_CHANGE_CLOSE",
                            self.last_price or 0
                        )

                    else:

                        logging.error(
                            f"[{self.symbol}] "
                            f"Session change close "
                            f"NOT confirmed."
                        )

            except Exception as e:

                logging.error(
                    f"[{self.symbol}] "
                    f"Session change close error: {e}"
                )


        # -------------------------------------------------------------
        # New session state.
        # -------------------------------------------------------------

        self.session_start = current_sess

        self.day_high = None
        self.day_low = None

        self.prev_price = None

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

        self.last_strategy_price = None


        if (
            self.product_id
            and not is_weekend(
                self.symbol,
                now
            )
        ):

            h, l = (
                self.client.get_session_high_low(
                    self.session_start
                )
            )

            if (
                h is not None
                and l is not None
            ):

                self.day_high = h
                self.day_low = l
                self.ready = True


        self.save()


    # -----------------------------------------------------------------
    # PREPARE
    # -----------------------------------------------------------------

    def prepare(self, now):

        if not self.product_id:

            try:

                self.product = (
                    self.client.product()
                )

                self.product_id = int(
                    self.product["id"]
                )

            except Exception as e:

                logging.error(
                    f"[{self.symbol}] "
                    f"Product prepare error: {e}"
                )

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

            h, l = (
                self.client.get_session_high_low(
                    self.session_start
                )
            )

            if (
                h is not None
                and l is not None
            ):

                self.day_high = h
                self.day_low = l

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


        m_raw = (
            self.product.get(
                "maintenance_margin",
                0
            )
            if self.product
            else 0
        )

        t_raw = (
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
                    str(m_raw)
                )
                / Decimal("100")
            )

        except Exception:

            maintenance = Decimal("0")


        try:

            taker_fee = Decimal(
                str(t_raw)
            )

        except Exception:

            taker_fee = Decimal("0")


        effective_mm = (
            maintenance
            + taker_fee
            + Decimal("0.0010")
        )


        if direction == "LONG":

            return entry * (
                Decimal("1")
                - Decimal("1") / lev
                + effective_mm
            )


        return entry * (
            Decimal("1")
            + Decimal("1") / lev
            - effective_mm
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

        # -------------------------------------------------------------
        # HARD ENTRY GUARDS
        # -------------------------------------------------------------

        if (
            self.is_expired()
            or self.manual_squareoff_flag
            or not self.bot_enabled
            or is_weekend(self.symbol)
        ):
            return False


        if self.order_in_progress:

            logging.warning(
                f"[{self.symbol}] "
                f"ENTRY BLOCKED: "
                f"another order is already in progress."
            )

            return False


        self.order_in_progress = True

        try:

            # ---------------------------------------------------------
            # Exchange must be FLAT before any new entry.
            # ---------------------------------------------------------

            current_pos = (
                self.client.position(
                    self.product_id
                )
            )

            current_size = int(
                current_pos.get(
                    "size",
                    0
                )
                or 0
            )

            if current_size != 0:

                logging.warning(
                    f"[{self.symbol}] "
                    f"ENTRY BLOCKED: "
                    f"exchange already has position "
                    f"{current_size}"
                )

                return False


            lev_ladder = (
                list(
                    range(
                        200,
                        9,
                        -10
                    )
                )
                if "BTC" in self.symbol
                else [
                    100,
                    90,
                    80,
                    70,
                    60,
                    50,
                    40,
                    30,
                    20,
                    10,
                ]
            )


            order_done = False

            chosen_lev = (
                self.leverage
            )

            size = 0


            for lev in lev_ladder:

                lev_decimal = Decimal(
                    str(lev)
                )


                candidate_liq = (
                    self.estimate_liquidation_price(
                        price,
                        lev_decimal,
                        direction
                    )
                )


                if candidate_liq is None:
                    continue


                if (
                    direction == "LONG"
                    and candidate_liq
                    >= Decimal(str(initial_sl))
                ):
                    continue


                if (
                    direction == "SHORT"
                    and candidate_liq
                    <= Decimal(str(initial_sl))
                ):
                    continue


                try:

                    self.client.set_leverage(
                        self.product_id,
                        lev_decimal
                    )


                    size = (
                        self.client.order_size(
                            self.product,
                            Decimal(str(price)),
                            lev_decimal,
                            self.balance_fraction,
                        )
                    )


                    side = (
                        "buy"
                        if direction == "LONG"
                        else "sell"
                    )


                    self.client.market_entry_pure(
                        self.product_id,
                        side,
                        size
                    )


                    # -------------------------------------------------
                    # VERY IMPORTANT:
                    #
                    # Do NOT immediately assume order filled.
                    # Wait for exchange position confirmation.
                    # -------------------------------------------------

                    confirmed_pos = (
                        self.wait_for_position(
                            expected_direction=direction
                        )
                    )


                    if confirmed_pos is None:

                        logging.error(
                            f"[{self.symbol}] "
                            f"ENTRY SENT BUT "
                            f"EXCHANGE POSITION "
                            f"WAS NOT CONFIRMED."
                        )

                        # Do not submit another entry.
                        # Exchange could have accepted the order
                        # with delayed position update.
                        return False


                    confirmed_size = int(
                        confirmed_pos.get(
                            "size",
                            0
                        )
                        or 0
                    )


                    if (
                        direction == "LONG"
                        and confirmed_size <= 0
                    ):

                        return False


                    if (
                        direction == "SHORT"
                        and confirmed_size >= 0
                    ):

                        return False


                    chosen_lev = (
                        lev_decimal
                    )

                    order_done = True

                    break


                except Exception as e:

                    logging.warning(
                        f"[{self.symbol}] "
                        f"Entry attempt "
                        f"{lev}x failed: {e}"
                    )

                    continue


            if not order_done:
                return False


            # ---------------------------------------------------------
            # Only now local state is updated.
            # ---------------------------------------------------------

            exchange_size = abs(
                int(
                    confirmed_pos.get(
                        "size",
                        size
                    )
                )
            )

            exchange_entry = (
                confirmed_pos.get(
                    "entry_price"
                )
            )


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
                chosen_lev
            )

            self.stop_loss = (
                float(initial_sl)
            )

            self.is_reversal_position = (
                bool(is_reversal)
            )

            # Once any trade is active, base breakout is disabled.
            self.base_breakout_ready = False


            self.last_execution_time = (
                time.time()
            )


            self.save()


            logging.info(
                f"[{self.symbol}] "
                f"CONFIRMED ENTER "
                f"{direction} "
                f"{'REVERSAL' if is_reversal else 'BASE'} "
                f"| Price={price} "
                f"| Entry={self.entry_price} "
                f"| Size={self.size} "
                f"| Lev={int(chosen_lev)}x "
                f"| SL={initial_sl}"
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

        if (
            not self.position
            or self.size <= 0
        ):
            return True


        if self.order_in_progress:

            logging.warning(
                f"[{self.symbol}] "
                f"CLOSE BLOCKED: "
                f"another order is active."
            )

            return False


        self.order_in_progress = True


        old_position = (
            self.position
        )

        old_size = (
            self.size
        )

        try:

            close_sz = (
                old_size
                if old_position == "LONG"
                else -old_size
            )


            self.client.close_position(
                self.product_id,
                close_sz
            )


            # ---------------------------------------------------------
            # NEVER mark flat before exchange confirms flat.
            # ---------------------------------------------------------

            confirmed = (
                self.wait_until_flat()
            )


            if not confirmed:

                logging.error(
                    f"[{self.symbol}] "
                    f"{reason}: "
                    f"EXCHANGE DID NOT CONFIRM FLAT. "
                    f"NO NEW TRADE WILL BE STARTED."
                )

                return False


            # ---------------------------------------------------------
            # Only after confirmed close, save trade history.
            # ---------------------------------------------------------

            self.finish_trade(
                reason,
                exit_price
            )


            self.position = None
            self.entry_price = None
            self.size = 0
            self.stop_loss = 0.0
            self.is_reversal_position = False

            # Flat after reversal SL must re-arm base only through
            # a genuinely NEW running high/low.
            self.base_breakout_ready = True


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

            return False


        finally:

            self.order_in_progress = False


    # -----------------------------------------------------------------
    # REVERSAL
    # -----------------------------------------------------------------

    def reverse_from_sl(
        self,
        price,
        prev_candle
    ):

        # -------------------------------------------------------------
        # HARD SAFETY:
        #
        # This function MUST only be called while an actual position
        # exists. It can never independently create a reversal.
        # -------------------------------------------------------------

        if (
            not self.position
            or self.size <= 0
        ):
            return False


        old_direction = (
            self.position
        )

        old_is_reversal = (
            self.is_reversal_position
        )


        # -------------------------------------------------------------
        # REVERSAL POSITION:
        #
        # NEVER reverse again.
        # Just close -> FLAT.
        # -------------------------------------------------------------

        if old_is_reversal:

            logging.info(
                f"[{self.symbol}] "
                f"REVERSAL {old_direction} "
                f"SL HIT "
                f"| FLAT "
                f"| NO SECOND REVERSAL"
            )


            closed = (
                self.close_current_position(
                    "REVERSAL_SL_HIT",
                    price
                )
            )


            if closed:

                # -----------------------------------------------------
                # Consume the current extreme if price itself created
                # a new extreme. This means the next base trade needs
                # another NEW price beyond this extreme.
                # -----------------------------------------------------

                changed = False


                if (
                    self.day_high is None
                    or price > float(
                        self.day_high
                    )
                ):

                    self.day_high = Decimal(
                        str(price)
                    )

                    changed = True


                if (
                    self.day_low is None
                    or price < float(
                        self.day_low
                    )
                ):

                    self.day_low = Decimal(
                        str(price)
                    )

                    changed = True


                if changed:
                    self.save()


                return True


            return False


        # -------------------------------------------------------------
        # BASE LONG SL -> ONE SHORT REVERSAL
        # -------------------------------------------------------------

        if old_direction == "LONG":

            reversal_sl = float(
                prev_candle["high"]
            )


            logging.info(
                f"[{self.symbol}] "
                f"BASE LONG SL CONFIRMED "
                f"| ONE REVERSAL -> SHORT "
                f"| Reversal SL={reversal_sl}"
            )


            closed = (
                self.close_current_position(
                    "SL_HIT_REVERSAL",
                    price
                )
            )


            if not closed:

                logging.error(
                    f"[{self.symbol}] "
                    f"BASE LONG could not be "
                    f"confirmed FLAT. "
                    f"REVERSAL BLOCKED."
                )

                return False


            # ---------------------------------------------------------
            # The close_current_position() already made local state flat.
            #
            # Do NOT re-arm base here.
            # We are specifically creating the ONE reversal.
            # ---------------------------------------------------------

            self.base_breakout_ready = False

            self.save()


            success = self.enter(
                "SHORT",
                price,
                reversal_sl,
                is_reversal=True
            )


            if success:

                logging.info(
                    f"[{self.symbol}] "
                    f"ONE REVERSAL SHORT "
                    f"CONFIRMED."
                )

                return True


            # If reversal could not be entered,
            # remain flat and allow a future fresh breakout.
            self.position = None
            self.entry_price = None
            self.size = 0
            self.stop_loss = 0.0
            self.is_reversal_position = False
            self.base_breakout_ready = True

            self.save()

            return False


        # -------------------------------------------------------------
        # BASE SHORT SL -> ONE LONG REVERSAL
        # -------------------------------------------------------------

        if old_direction == "SHORT":

            reversal_sl = float(
                prev_candle["low"]
            )


            logging.info(
                f"[{self.symbol}] "
                f"BASE SHORT SL CONFIRMED "
                f"| ONE REVERSAL -> LONG "
                f"| Reversal SL={reversal_sl}"
            )


            closed = (
                self.close_current_position(
                    "SL_HIT_REVERSAL",
                    price
                )
            )


            if not closed:

                logging.error(
                    f"[{self.symbol}] "
                    f"BASE SHORT could not be "
                    f"confirmed FLAT. "
                    f"REVERSAL BLOCKED."
                )

                return False


            self.base_breakout_ready = False

            self.save()


            success = self.enter(
                "LONG",
                price,
                reversal_sl,
                is_reversal=True
            )


            if success:

                logging.info(
                    f"[{self.symbol}] "
                    f"ONE REVERSAL LONG "
                    f"CONFIRMED."
                )

                return True


            self.position = None
            self.entry_price = None
            self.size = 0
            self.stop_loss = 0.0
            self.is_reversal_position = False
            self.base_breakout_ready = True

            self.save()

            return False


        return False


    # -----------------------------------------------------------------
    # WEEKEND FLAT
    # -----------------------------------------------------------------

    def force_weekend_flat(self):

        with self.lock:

            if (
                self.product_id
                and self.position
                and self.size > 0
            ):

                try:

                    exchange_pos = (
                        self.client.position(
                            self.product_id
                        )
                    )

                    ex_size = int(
                        exchange_pos.get(
                            "size",
                            0
                        )
                        or 0
                    )


                    if ex_size != 0:

                        self.client.close_position(
                            self.product_id,
                            ex_size
                        )

                        confirmed = (
                            self.wait_until_flat()
                        )

                        if confirmed:

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

                        else:

                            logging.error(
                                f"[{self.symbol}] "
                                f"WEEKEND CLOSE NOT CONFIRMED."
                            )

                    else:

                        # Exchange already flat.
                        self.position = None
                        self.entry_price = None
                        self.size = 0
                        self.stop_loss = 0.0
                        self.is_reversal_position = False
                        self.base_breakout_ready = True


                except Exception as e:

                    logging.warning(
                        f"[{self.symbol}] "
                        f"Weekend close error: {e}"
                    )


            else:

                self.position = None
                self.entry_price = None
                self.size = 0
                self.stop_loss = 0.0
                self.is_reversal_position = False
                self.base_breakout_ready = True


            self.save()


    # -----------------------------------------------------------------
    # MAIN EVALUATION
    # -----------------------------------------------------------------

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


            # =========================================================
            # HARD WEEKEND LOCK
            # =========================================================

            if is_weekend(
                self.symbol,
                now
            ):

                self.force_weekend_flat()

                self.prev_price = None

                return


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
            # EXACT SAME PRICE MESSAGE
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


            if self.prev_price is None:

                self.prev_price = (
                    new_price
                )

                return


            self.prev_price = (
                new_price
            )


            # =========================================================
            # SESSION
            # =========================================================

            self.check_session_change(
                now
            )


            if (
                not self.prepare(now)
                or not self.ready
            ):
                return


            # =========================================================
            # WAIT UNTIL 05:45 IST
            # =========================================================

            if now.time() < TRADING_START_TIME:

                self.trading_armed = False

                return


            if not self.trading_armed:

                self.trading_armed = True

                return


            # =========================================================
            # 5M CANDLES
            # =========================================================

            candles = (
                self.get_5m_candles(
                    limit=4
                )
            )


            if len(candles) < 2:
                return


            prev_candle = (
                candles[-2]
            )

            curr_candle = (
                candles[-1]
            )

            curr_time = (
                curr_candle["time"]
            )


            # =========================================================
            # ACTIVE LONG
            # =========================================================

            if (
                self.position == "LONG"
                and self.size > 0
            ):

                # -----------------------------------------------------
                # CURRENT ACTIVE SL
                # -----------------------------------------------------

                if (
                    self.stop_loss > 0
                    and new_price
                    <= self.stop_loss
                ):

                    logging.info(
                        f"[{self.symbol}] "
                        f"LONG SL HIT "
                        f"| Price={new_price} "
                        f"| SL={self.stop_loss} "
                        f"| Reversal="
                        f"{self.is_reversal_position}"
                    )


                    was_reversal = (
                        self.is_reversal_position
                    )


                    # -------------------------------------------------
                    # ONLY HERE CAN reverse_from_sl() BE CALLED.
                    #
                    # This is the actual SL condition.
                    # -------------------------------------------------

                    result = (
                        self.reverse_from_sl(
                            new_price,
                            prev_candle
                        )
                    )


                    # If a BASE SL created a reversal,
                    # do absolutely nothing else on this tick.
                    if (
                        not was_reversal
                        and result
                        and self.position
                        and self.size > 0
                        and self.is_reversal_position
                    ):

                        return


                    # Reversal SL -> flat.
                    if was_reversal:

                        return


                    return


                # -----------------------------------------------------
                # NEW COMPLETED 5M CANDLE
                # -----------------------------------------------------

                if (
                    curr_time
                    != self.last_checked_candle_time
                ):

                    new_sl = float(
                        prev_candle["low"]
                    )

                    self.stop_loss = (
                        new_sl
                    )

                    self.last_checked_candle_time = (
                        curr_time
                    )

                    self.save()


                    logging.info(
                        f"[{self.symbol}] "
                        f"LONG TRAILING SL UPDATED "
                        f"| New SL={new_sl}"
                    )


                    # -------------------------------------------------
                    # New trailing SL is active immediately.
                    # -------------------------------------------------

                    if (
                        new_price
                        <= self.stop_loss
                    ):

                        logging.info(
                            f"[{self.symbol}] "
                            f"NEW TRAILING LONG SL HIT "
                            f"| Price={new_price} "
                            f"| SL={self.stop_loss}"
                        )


                        was_reversal = (
                            self.is_reversal_position
                        )


                        self.reverse_from_sl(
                            new_price,
                            prev_candle
                        )


                        return


            # =========================================================
            # ACTIVE SHORT
            # =========================================================

            elif (
                self.position == "SHORT"
                and self.size > 0
            ):

                # -----------------------------------------------------
                # CURRENT ACTIVE SL
                # -----------------------------------------------------

                if (
                    self.stop_loss > 0
                    and new_price
                    >= self.stop_loss
                ):

                    logging.info(
                        f"[{self.symbol}] "
                        f"SHORT SL HIT "
                        f"| Price={new_price} "
                        f"| SL={self.stop_loss} "
                        f"| Reversal="
                        f"{self.is_reversal_position}"
                    )


                    was_reversal = (
                        self.is_reversal_position
                    )


                    self.reverse_from_sl(
                        new_price,
                        prev_candle
                    )


                    # Never process breakout on same tick.
                    return


                # -----------------------------------------------------
                # NEW COMPLETED 5M CANDLE
                # -----------------------------------------------------

                if (
                    curr_time
                    != self.last_checked_candle_time
                ):

                    new_sl = float(
                        prev_candle["high"]
                    )

                    self.stop_loss = (
                        new_sl
                    )

                    self.last_checked_candle_time = (
                        curr_time
                    )

                    self.save()


                    logging.info(
                        f"[{self.symbol}] "
                        f"SHORT TRAILING SL UPDATED "
                        f"| New SL={new_sl}"
                    )


                    if (
                        new_price
                        >= self.stop_loss
                    ):

                        logging.info(
                            f"[{self.symbol}] "
                            f"NEW TRAILING SHORT SL HIT "
                            f"| Price={new_price} "
                            f"| SL={self.stop_loss}"
                        )


                        self.reverse_from_sl(
                            new_price,
                            prev_candle
                        )


                        return


            # =========================================================
            # BASE BREAKOUT MODE
            #
            # ONLY WHEN FLAT.
            # =========================================================

            if (
                self.position is None
                and self.size == 0
                and self.base_breakout_ready
                and not self.manual_squareoff_flag
                and self.day_high is not None
                and self.day_low is not None
            ):

                current_high = float(
                    self.day_high
                )

                current_low = float(
                    self.day_low
                )


                # =====================================================
                # NEW HIGH -> BASE LONG
                # =====================================================

                if new_price > current_high:

                    initial_sl = float(
                        prev_candle["low"]
                    )


                    # -------------------------------------------------
                    # Consume current high BEFORE order.
                    # -------------------------------------------------

                    self.day_high = Decimal(
                        str(new_price)
                    )

                    self.save()


                    logging.info(
                        f"[{self.symbol}] "
                        f"NEW HIGH BREAK "
                        f"| OldHigh={current_high} "
                        f"| NewHigh={new_price} "
                        f"| BASE LONG "
                        f"| Initial SL={initial_sl}"
                    )


                    success = self.enter(
                        "LONG",
                        new_price,
                        initial_sl,
                        is_reversal=False
                    )


                    if success:

                        self.is_reversal_position = False

                        self.base_breakout_ready = False

                        self.save()


                    # -------------------------------------------------
                    # VERY IMPORTANT:
                    #
                    # Never evaluate SHORT on same tick.
                    # -------------------------------------------------

                    return


                # =====================================================
                # NEW LOW -> BASE SHORT
                # =====================================================

                if new_price < current_low:

                    initial_sl = float(
                        prev_candle["high"]
                    )


                    self.day_low = Decimal(
                        str(new_price)
                    )

                    self.save()


                    logging.info(
                        f"[{self.symbol}] "
                        f"NEW LOW BREAK "
                        f"| OldLow={current_low} "
                        f"| NewLow={new_price} "
                        f"| BASE SHORT "
                        f"| Initial SL={initial_sl}"
                    )


                    success = self.enter(
                        "SHORT",
                        new_price,
                        initial_sl,
                        is_reversal=False
                    )


                    if success:

                        self.is_reversal_position = False

                        self.base_breakout_ready = False

                        self.save()


                    return


            # =========================================================
            # UPDATE RUNNING SESSION EXTREMES
            #
            # This is ONLY level maintenance.
            # It does NOT create an entry by itself.
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

            "id": (
                f"{self.symbol.lower()}_"
                f"{int(time.time() * 1000)}"
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
                float(exit_price),

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


def load_all_accounts():

    with ACCOUNTS_LOCK:

        for b_obj in list(
            BOT_ACCOUNTS.values()
        ):

            try:
                b_obj.stop_bot()
            except Exception:
                pass


        BOT_ACCOUNTS.clear()


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

                BOT_ACCOUNTS[
                    bot.unique_id
                ] = bot


        clients_cfg = (
            load_clients_config()
        )


        for cid, cdata in clients_cfg.items():

            sub_dict = {

                "start":
                    cdata.get(
                        "subscription_start"
                    ),

                "expiry":
                    cdata.get(
                        "subscription_expiry"
                    ),
            }


            for symbol in (
                "XAUTUSD",
                "BTCUSD"
            ):

                bot = BreakoutSARBot(
                    cid,
                    cdata.get(
                        "name",
                        "Client"
                    ),
                    "client",
                    cdata.get(
                        "api_key"
                    ),
                    cdata.get(
                        "api_secret"
                    ),
                    symbol,
                    sub_dict,
                )

                BOT_ACCOUNTS[
                    bot.unique_id
                ] = bot


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


    def do_GET(self):

        parsed = urlparse(
            self.path
        )

        path = parsed.path

        query = parse_qs(
            parsed.query
        )


        if path == "/api/health":

            self.send_json({
                "success": True,
                "online": True
            })

            return


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


            if client_token:

                clients_cfg = (
                    load_clients_config()
                )

                target_cid = None


                for cid, cdata in clients_cfg.items():

                    if (
                        cdata.get("token")
                        == client_token
                    ):

                        target_cid = cid

                        break


                if not target_cid:

                    self.send_json(
                        {
                            "success":
                                False,

                            "message":
                                "Unauthorized client token"
                        },
                        status=403
                    )

                    return


                bots = [
                    b
                    for b in bots
                    if b.base_account_id
                    == target_cid
                ]


            accounts_data = []

            clients_cfg = (
                load_clients_config()
            )


            for b in bots:

                try:

                    price = float(
                        b.client.last_traded_price()
                        or 0
                    )

                    b.last_price = price


                    # -------------------------------------------------
                    # IMPORTANT:
                    #
                    # This is READ ONLY.
                    #
                    # It does NOT change:
                    #     position
                    #     base_breakout_ready
                    #     reversal state
                    #     day_high
                    #     day_low
                    #
                    # Dashboard must never alter strategy state.
                    # -------------------------------------------------

                    if b.bot_enabled:

                        pos = (
                            b.read_exchange_position()
                        )

                    else:

                        pos = {
                            "size": 0,
                            "entry_price": None,
                            "stop_loss":
                                b.stop_loss,
                            "unrealized_pnl": 0,
                            "leverage": None,
                            "liquidation_price":
                                None,
                        }


                    balance = float(
                        b.client.balance()
                    )


                except Exception:

                    pos = {

                        "size": 0,

                        "entry_price":
                            None,

                        "stop_loss":
                            b.stop_loss,

                        "unrealized_pnl":
                            0,

                        "leverage":
                            None,

                        "liquidation_price":
                            None,
                    }

                    balance = 0

                    price = 0


                direction = "FLAT"


                if (
                    b.bot_enabled
                    and pos.get(
                        "size",
                        0
                    ) != 0
                ):

                    direction = (
                        "LONG"
                        if pos["size"] > 0
                        else "SHORT"
                    )


                history = (
                    load_trade_history(
                        b.unique_id
                    )
                )


                stats = (
                    calculate_statistics(
                        history
                    )
                )


                token = (

                    clients_cfg.get(
                        b.base_account_id,
                        {}
                    ).get(
                        "token",
                        ""
                    )

                    if b.account_type
                    == "client"

                    else ""
                )


                accounts_data.append({

                    "account_id":
                        b.unique_id,

                    "account_name":
                        b.account_name,

                    "account_type":
                        b.account_type,

                    "symbol":
                        b.symbol,

                    "token":
                        token,

                    "server_ip":
                        CACHED_SERVER_IP,

                    "balance":
                        balance,

                    "current_price":
                        price,

                    "bot_enabled":
                        (
                            b.bot_enabled
                            and not b.is_expired()
                        ),

                    "is_expired":
                        b.is_expired(),

                    "leverage":
                        (
                            pos.get(
                                "leverage"
                            )
                            or int(
                                b.leverage
                            )
                        ),

                    "balance_fraction":
                        float(
                            b.balance_fraction
                        ),

                    "base_breakout_ready":
                        b.base_breakout_ready,

                    "day_high":
                        (
                            float(
                                b.day_high
                            )
                            if b.day_high
                            is not None
                            else None
                        ),

                    "day_low":
                        (
                            float(
                                b.day_low
                            )
                            if b.day_low
                            is not None
                            else None
                        ),

                    "position": {

                        "size":
                            (
                                pos.get(
                                    "size",
                                    0
                                )
                                if b.bot_enabled
                                else 0
                            ),

                        "direction":
                            direction,

                        "entry_price":
                            (
                                pos.get(
                                    "entry_price"
                                )
                                if b.bot_enabled
                                else None
                            ),

                        "stop_loss":
                            (
                                b.stop_loss
                                if b.bot_enabled
                                else None
                            ),

                        "liquidation_price":
                            (
                                pos.get(
                                    "liquidation_price"
                                )
                                if b.bot_enabled
                                else None
                            ),

                        "unrealized_pnl":
                            (
                                pos.get(
                                    "unrealized_pnl",
                                    0
                                )
                                if b.bot_enabled
                                else 0
                            ),

                        "is_reversal":
                            (
                                b.is_reversal_position
                                if b.bot_enabled
                                else False
                            ),
                    },

                    "statistics":
                        stats,

                    "trade_history":
                        history,

                    "subscription":
                        b.subscription,
                })


            self.send_json({

                "success":
                    True,

                "server_online":
                    True,

                "server_ip":
                    CACHED_SERVER_IP,

                "accounts":
                    accounts_data,
            })

            return


        if path in (
            "/",
            ""
        ):

            self.send_html_dashboard()

            return


        return super().do_GET()


    def do_POST(self):

        parsed = (
            urlparse(
                self.path
            ).path
        )


        try:

            length = int(
                self.headers.get(
                    "Content-Length",
                    0
                )
            )

            body = (
                json.loads(
                    self.rfile.read(
                        length
                    ).decode(
                        "utf-8"
                    )
                )
                if length > 0
                else {}
            )

        except Exception:

            self.send_json(
                {
                    "success":
                        False,
                    "message":
                        "Invalid JSON"
                },
                400
            )

            return


        clients_cfg = (
            load_clients_config()
        )


        if parsed == "/api/bot/start":

            bot = BOT_ACCOUNTS.get(
                body.get(
                    "account_id"
                )
            )

            if bot:

                self.send_json(
                    bot.start_bot()
                )

                return


            self.send_json(
                {
                    "success":
                        False,
                    "message":
                        "Not found"
                },
                404
            )

            return


        if parsed == "/api/bot/stop":

            bot = BOT_ACCOUNTS.get(
                body.get(
                    "account_id"
                )
            )

            if bot:

                self.send_json(
                    bot.stop_bot()
                )

                return


            self.send_json(
                {
                    "success":
                        False,
                    "message":
                        "Not found"
                },
                404
            )

            return


        if parsed == "/api/bot/settings":

            bot = BOT_ACCOUNTS.get(
                body.get(
                    "account_id"
                )
            )

            if bot:

                self.send_json(
                    bot.update_settings(
                        body.get(
                            "leverage"
                        ),
                        body.get(
                            "balance_fraction",
                            0.10
                        ),
                    )
                )

                return


            self.send_json(
                {
                    "success":
                        False,
                    "message":
                        "Not found"
                },
                404
            )

            return


        if parsed == "/api/client/add":

            name = body.get(
                "name"
            )

            key = body.get(
                "api_key"
            )

            secret = body.get(
                "api_secret"
            )

            expiry = body.get(
                "subscription_expiry"
            )


            if (
                not name
                or not key
                or not secret
            ):

                self.send_json(
                    {
                        "success":
                            False,
                        "message":
                            "Missing fields"
                    },
                    400
                )

                return


            cid = (
                f"client_"
                f"{int(time.time())}"
            )


            token = hashlib.sha256(
                f"{cid}_{time.time()}".encode()
            ).hexdigest()[:16]


            clients_cfg[cid] = {

                "name":
                    name,

                "api_key":
                    key,

                "api_secret":
                    secret,

                "token":
                    token,

                "subscription_start":
                    now_ist().strftime(
                        "%Y-%m-%d"
                    ),

                "subscription_expiry":
                    expiry
                    or "2099-12-31",

                "subscription_fee":
                    0,
            }


            save_clients_config(
                clients_cfg
            )

            load_all_accounts()


            self.send_json({

                "success":
                    True,

                "message":
                    "Client added successfully!",
            })

            return


        if parsed == "/api/client/delete":

            acc_id = body.get(
                "account_id",
                ""
            )

            parts = acc_id.split("_")


            if len(parts) >= 2:

                base_cid = (
                    parts[0]
                    + "_"
                    + parts[1]
                )

            else:

                base_cid = acc_id


            if base_cid in clients_cfg:

                del clients_cfg[
                    base_cid
                ]

                save_clients_config(
                    clients_cfg
                )


                with ACCOUNTS_LOCK:

                    keys_to_del = [

                        k

                        for k in BOT_ACCOUNTS

                        if k.startswith(
                            base_cid + "_"
                        )
                    ]


                    for k in keys_to_del:

                        try:

                            BOT_ACCOUNTS[
                                k
                            ].stop_bot()

                        except Exception:
                            pass


                        del BOT_ACCOUNTS[
                            k
                        ]


                self.send_json({

                    "success":
                        True,

                    "message":
                        "Client removed",
                })

                return


            self.send_json({

                "success":
                    False,

                "message":
                    "Client not found",

            }, 404)

            return


        self.send_json(
            {
                "success":
                    False,
                "message":
                    "Not found"
            },
            404
        )


    # -----------------------------------------------------------------
    # DASHBOARD HTML
    # -----------------------------------------------------------------

    def send_html_dashboard(self):

        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Delta Pro AutoTrader</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>

<body class="bg-slate-900 text-slate-100 min-h-screen p-4">

<div class="max-w-md mx-auto space-y-6">

<header class="text-center">

<h1 class="text-2xl font-bold text-amber-400">
Delta Pro AutoTrader
</h1>

<p id="server-ip"
class="text-xs text-slate-400 mt-1">
IP: Loading...
</p>

</header>


<div id="add-client-section"
class="bg-slate-800 rounded-2xl p-4 shadow-xl border border-slate-700 space-y-3">

<h3 class="font-bold text-sm text-amber-400 uppercase">
Add New Client Account
</h3>

<input
id="c-name"
placeholder="Client Name"
class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2 text-xs">

<input
id="c-key"
placeholder="Delta API Key"
class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2 text-xs">

<input
id="c-secret"
type="password"
placeholder="Delta API Secret"
class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2 text-xs">

<input
id="c-expiry"
type="date"
class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2 text-xs">

<button
onclick="addClient()"
class="w-full bg-amber-600 hover:bg-amber-500 text-xs font-semibold py-2 rounded-lg">

Add Client & Generate Link

</button>

</div>


<div id="accounts-container"
class="space-y-6">

<div class="text-center text-slate-400">
Loading Dashboard...
</div>

</div>

</div>


<script>

let isEditingSettings = false;


async function fetchDashboard() {

    if (isEditingSettings) return;

    try {

        const token =
            new URLSearchParams(
                window.location.search
            ).get("token");


        const url = token
            ? `/api/dashboard?token=${encodeURIComponent(token)}&_t=${Date.now()}`
            : `/api/dashboard?_t=${Date.now()}`;


        const res = await fetch(url);

        const data = await res.json();


        if (!data.success) return;


        document.getElementById(
            "server-ip"
        ).innerText =
            "Server IP: "
            + data.server_ip;


        if (token) {

            document.getElementById(
                "add-client-section"
            ).style.display = "none";

        }


        const container =
            document.getElementById(
                "accounts-container"
            );


        container.innerHTML = "";


        data.accounts.forEach(acc => {

            const pos = acc.position;

            const stats = acc.statistics;

            const expiry =
                acc.subscription &&
                acc.subscription.expiry
                    ? acc.subscription.expiry
                    : "N/A";


            const clientLink =
                acc.token
                    ? `${window.location.origin}/?token=${acc.token}`
                    : "";


            const tradeType =
                pos.is_reversal
                    ? "REVERSAL"
                    : "BREAKOUT";


            const html = `

<div class="bg-slate-800 rounded-2xl p-5 shadow-xl border border-slate-700 space-y-4">


<div class="flex justify-between items-center border-b border-slate-700 pb-3">

<div>

<h2 class="font-bold text-base text-amber-300">
${acc.account_name}
</h2>

<p class="text-xs text-slate-400">
Balance: $${Number(acc.balance).toFixed(2)}
|
Price: ${acc.current_price || "N/A"}
</p>

${acc.account_type === "client"
? `<p class="text-[10px] text-amber-400 mt-0.5">
Expiry: ${expiry}${acc.is_expired ? " (EXPIRED)" : ""}
</p>`
: ""}

</div>


<span
class="px-3 py-1 rounded-full text-xs font-semibold
${acc.bot_enabled
    ? "bg-emerald-500/20 text-emerald-400"
    : "bg-rose-500/20 text-rose-400"}">

${acc.bot_enabled
    ? "RUNNING"
    : "STOPPED"}

</span>

</div>


${clientLink
? `<div class="bg-slate-900/60 p-2.5 rounded-xl border border-slate-700 text-xs">

<span class="text-slate-400 text-[10px] block">
Client Unique Link:
</span>

<input
readonly
value="${clientLink}"
class="w-full bg-slate-800 border border-slate-700 rounded p-1 text-[11px] text-amber-300 select-all">

</div>`
: ""}


<div class="bg-slate-900/50 p-3 rounded-xl border border-slate-700/50 space-y-3">


<div class="text-xs font-semibold text-amber-400 uppercase">
Risk Settings
</div>


<div class="grid grid-cols-2 gap-2">


<div>

<label class="block text-[10px] text-slate-400 mb-1">
Max/Default Leverage
</label>


<select
id="lev-${acc.account_id}"
onfocus="isEditingSettings=true"
onblur="isEditingSettings=false"
class="w-full bg-slate-800 border border-slate-700 rounded-lg p-1.5 text-xs">

${acc.symbol.includes("BTC")
? `
<option value="200" ${acc.leverage==200?"selected":""}>200x</option>
<option value="150" ${acc.leverage==150?"selected":""}>150x</option>
<option value="100" ${acc.leverage==100?"selected":""}>100x</option>
<option value="50" ${acc.leverage==50?"selected":""}>50x</option>
`
: `
<option value="100" ${acc.leverage==100?"selected":""}>100x</option>
<option value="50" ${acc.leverage==50?"selected":""}>50x</option>
<option value="25" ${acc.leverage==25?"selected":""}>25x</option>
<option value="10" ${acc.leverage==10?"selected":""}>10x</option>
`}

</select>

</div>


<div>

<label class="block text-[10px] text-slate-400 mb-1">
Margin Fraction
</label>

<select
class="w-full bg-slate-800 border border-slate-700 rounded-lg p-1.5 text-xs">

<option selected>
10%
</option>

</select>

</div>

</div>


<button
onclick="updateSettings('${acc.account_id}')"
class="w-full bg-slate-700 hover:bg-slate-600 text-xs font-semibold py-1.5 rounded-lg">

Save Settings

</button>

</div>


<div class="space-y-2 bg-slate-900/60 p-3 rounded-xl border border-slate-700/60 text-sm">


<div class="flex justify-between">

<span class="text-slate-400">
Session High:
</span>

<span class="font-semibold text-emerald-300">
${acc.day_high ?? "N/A"}
</span>

</div>


<div class="flex justify-between">

<span class="text-slate-400">
Session Low:
</span>

<span class="font-semibold text-rose-300">
${acc.day_low ?? "N/A"}
</span>

</div>


<div class="flex justify-between">

<span class="text-slate-400">
Direction:
</span>

<span class="font-bold
${pos.direction=="LONG"
    ?"text-emerald-400"
    :pos.direction=="SHORT"
        ?"text-rose-400"
        :"text-slate-300"}">

${pos.direction}

</span>

</div>


<div class="flex justify-between">

<span class="text-slate-400">
Trade Type:
</span>

<span class="font-semibold
${pos.is_reversal
    ?"text-purple-400"
    :"text-amber-400"}">

${pos.direction=="FLAT"
    ?"—"
    :tradeType}

</span>

</div>


<div class="flex justify-between">

<span class="text-slate-400">
Size:
</span>

<span>
${pos.size}
</span>

</div>


<div class="flex justify-between">

<span class="text-slate-400">
Entry Price:
</span>

<span class="font-semibold text-amber-300">
${pos.entry_price ?? "N/A"}
</span>

</div>


<div class="flex justify-between">

<span class="text-slate-400">
Trade Leverage:
</span>

<span class="font-semibold text-amber-400">
${acc.leverage}x
</span>

</div>


<div class="flex justify-between">

<span class="text-slate-400">
Trailing SL:
</span>

<span class="font-semibold text-rose-400">
${pos.stop_loss ?? "N/A"}
</span>

</div>


<div class="flex justify-between">

<span class="text-slate-400">
Unrealized P&L:
</span>

<span class="font-semibold
${pos.unrealized_pnl>=0
    ?"text-emerald-400"
    :"text-rose-400"}">

$${Number(
    pos.unrealized_pnl
).toFixed(2)}

</span>

</div>


</div>


<div class="space-y-2">


<div class="text-xs font-bold text-slate-400 uppercase">
Trading Performance
</div>


<div class="grid grid-cols-2 gap-2 text-xs">


<div class="bg-slate-900/50 p-2.5 rounded-xl border border-slate-700/60">

<div class="font-semibold text-amber-400">
TODAY
</div>

<div class="text-slate-400">
Trades: ${stats.today.total_trades}
</div>

<div class="text-slate-400">
Win Rate: ${stats.today.win_rate.toFixed(1)}%
</div>

<div class="font-bold
${stats.today.pnl>=0
    ?"text-emerald-400"
    :"text-rose-400"}">

P&L: $${stats.today.pnl.toFixed(2)}

</div>

</div>


<div class="bg-slate-900/50 p-2.5 rounded-xl border border-slate-700/60">

<div class="font-semibold text-amber-400">
ALL TIME
</div>

<div class="text-slate-400">
Trades: ${stats.all_time.total_trades}
</div>

<div class="text-slate-400">
Win Rate: ${stats.all_time.win_rate.toFixed(1)}%
</div>

<div class="font-bold
${stats.all_time.pnl>=0
    ?"text-emerald-400"
    :"text-rose-400"}">

P&L: $${stats.all_time.pnl.toFixed(2)}

</div>

</div>

</div>

</div>


<div class="flex gap-2">


<button
onclick="toggleBot('${acc.account_id}', ${acc.bot_enabled})"
class="flex-1 py-2.5 rounded-xl font-semibold text-sm
${acc.bot_enabled
    ?"bg-rose-600"
    :"bg-emerald-600"}">

${acc.bot_enabled
    ?"STOP BOT"
    :"START BOT"}

</button>


${acc.account_type=="client" && !token
? `<button
onclick="deleteClient('${acc.account_id}')"
class="bg-slate-700 hover:bg-rose-700 px-3 py-2.5 rounded-xl text-xs font-semibold">

Remove

</button>`
: ""}

</div>


<div class="space-y-2 pt-2 border-t border-slate-700">


<div class="text-xs font-bold text-slate-400 uppercase">

Trade History
(${acc.trade_history.length})

</div>


<div class="max-h-40 overflow-y-auto space-y-1.5 text-xs">


${acc.trade_history.length===0
? '<div class="text-slate-500 text-center py-2">No closed trades yet.</div>'
: ""}


${acc.trade_history
.slice()
.reverse()
.map(t => `

<div class="bg-slate-900/40 p-2 rounded border border-slate-800 flex justify-between">


<div>

<span class="font-bold
${t.direction=="LONG"
    ?"text-emerald-400"
    :"text-rose-400"}">

${t.direction}

</span>


<span class="text-slate-400 ml-1">
(${t.date})
</span>


<div class="text-[10px] text-slate-500">

Entry: ${t.entry_price}
→
Exit: ${t.exit_price}

</div>


<div class="text-[10px]
${t.trade_type=="REVERSAL"
    ?"text-purple-400"
    :"text-amber-400"}">

${t.trade_type || "BREAKOUT"}

</div>

</div>


<div class="text-right font-bold
${t.pnl>=0
    ?"text-emerald-400"
    :"text-rose-400"}">

$${Number(
    t.pnl
).toFixed(2)}

</div>


</div>

`)
.join("")}

</div>

</div>

</div>
`;

            container.innerHTML += html;

        });


    } catch (e) {

        console.error(e);

    }

}


async function addClient() {

    const name =
        document.getElementById(
            "c-name"
        ).value;

    const key =
        document.getElementById(
            "c-key"
        ).value;

    const secret =
        document.getElementById(
            "c-secret"
        ).value;

    const expiry =
        document.getElementById(
            "c-expiry"
        ).value;


    if (
        !name
        || !key
        || !secret
        || !expiry
    ) {

        alert(
            "Please fill all fields!"
        );

        return;
    }


    const res =
        await fetch(
            "/api/client/add",
            {
                method: "POST",

                headers: {
                    "Content-Type":
                        "application/json"
                },

                body: JSON.stringify({
                    name,
                    api_key: key,
                    api_secret: secret,
                    subscription_expiry:
                        expiry
                })
            }
        );


    const data =
        await res.json();


    alert(
        data.message
    );


    document.getElementById(
        "c-name"
    ).value = "";

    document.getElementById(
        "c-key"
    ).value = "";

    document.getElementById(
        "c-secret"
    ).value = "";

    document.getElementById(
        "c-expiry"
    ).value = "";


    fetchDashboard();

}


async function deleteClient(
    accId
) {

    if (
        !confirm(
            "Are you sure you want to remove this client?"
        )
    ) return;


    await fetch(
        "/api/client/delete",
        {
            method: "POST",

            headers: {
                "Content-Type":
                    "application/json"
            },

            body: JSON.stringify({
                account_id: accId
            })
        }
    );


    fetchDashboard();

}


async function toggleBot(
    accId,
    state
) {

    const endpoint =
        state
            ? "/api/bot/stop"
            : "/api/bot/start";


    await fetch(
        endpoint,
        {
            method: "POST",

            headers: {
                "Content-Type":
                    "application/json"
            },

            body: JSON.stringify({
                account_id: accId
            })
        }
    );


    fetchDashboard();

}


async function updateSettings(
    accId
) {

    const lev =
        document.getElementById(
            "lev-" + accId
        ).value;


    isEditingSettings = true;


    const res =
        await fetch(
            "/api/bot/settings",
            {
                method: "POST",

                headers: {
                    "Content-Type":
                        "application/json"
                },

                body: JSON.stringify({

                    account_id:
                        accId,

                    leverage:
                        lev,

                    balance_fraction:
                        0.10

                })
            }
        );


    const data =
        await res.json();


    alert(
        data.message
    );


    isEditingSettings = false;


    fetchDashboard();

}


setInterval(
    fetchDashboard,
    3000
);


fetchDashboard();

</script>

</body>
</html>
"""


        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            html.encode("utf-8")
        )


    def send_json(
        self,
        data,
        status=200
    ):

        raw = json.dumps(
            data
        ).encode("utf-8")


        self.send_response(
            status
        )

        self.send_header(
            "Content-Type",
            "application/json"
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
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
        pass


# =====================================================================
# WEB SERVER
# =====================================================================

def start_dashboard():

    port = int(
        os.getenv(
            "PORT",
            DASHBOARD_PORT
        )
    )

    server = ThreadingHTTPServer(
        (
            "0.0.0.0",
            port
        ),
        DashboardHandler
    )

    logging.warning(
        f"WEB SERVER STARTED ON PORT {port}"
    )

    server.serve_forever()


# =====================================================================
# WEBSOCKET
# =====================================================================

def run_websocket():

    while True:

        try:

            def on_open(ws):

                ws.send(
                    json.dumps({

                        "type":
                            "subscribe",

                        "payload": {

                            "channels": [{

                                "name":
                                    "trades",

                                "symbols": [
                                    "XAUTUSD",
                                    "BTCUSD"
                                ],

                            }]

                        }

                    })
                )


            def on_message(
                ws,
                message
            ):

                try:

                    parsed = json.loads(
                        message
                    )


                    if parsed.get(
                        "type"
                    ) != "trades":

                        return


                    # -------------------------------------------------
                    # Support both direct and nested trade payloads.
                    # -------------------------------------------------

                    payload = parsed.get(
                        "data",
                        parsed
                    )


                    if not isinstance(
                        payload,
                        dict
                    ):

                        payload = parsed


                    p_val = (
                        payload.get("p")
                        or payload.get("price")
                        or parsed.get("p")
                        or parsed.get("price")
                    )


                    sym = (
                        payload.get("sy")
                        or payload.get("symbol")
                        or parsed.get("sy")
                        or parsed.get("symbol")
                    )


                    if p_val is None:
                        return


                    price = Decimal(
                        str(p_val)
                    )


                    with ACCOUNTS_LOCK:

                        bots = list(
                            BOT_ACCOUNTS.values()
                        )


                    for b in bots:

                        if (
                            sym
                            and b.symbol.upper()
                            != str(sym).upper()
                        ):

                            continue


                        b.evaluate(
                            price
                        )


                except Exception as e:

                    logging.warning(
                        f"WEBSOCKET MESSAGE ERROR | {e}"
                    )


            def on_error(
                ws,
                error
            ):

                logging.warning(
                    f"WEBSOCKET ERROR | {error}"
                )


            def on_close(
                ws,
                close_status_code,
                close_msg
            ):

                logging.warning(
                    f"WEBSOCKET CLOSED | "
                    f"code={close_status_code} | "
                    f"msg={close_msg}"
                )


            ws = websocket.WebSocketApp(

                WS_URL,

                on_open=on_open,

                on_message=on_message,

                on_error=on_error,

                on_close=on_close,
            )


            ws.run_forever(

                ping_interval=30,

                ping_timeout=10,
            )


        except Exception as e:

            logging.warning(
                f"WEBSOCKET LOOP ERROR | {e}"
            )


        time.sleep(
            RECONNECT_SECONDS
        )


# =====================================================================
# MAIN
# =====================================================================

if __name__ == "__main__":

    logging.warning(
        "=================================================="
    )

    logging.warning(
        "DELTA DUAL ASSET AUTOTRADER STARTING - SAFE STATE"
    )

    logging.warning(
        "BTCUSD + XAUTUSD"
    )

    logging.warning(
        "WEEKEND LOCK"
    )

    logging.warning(
        "NEW EXTREMES"
    )

    logging.warning(
        "TRAILING 5M SL"
    )

    logging.warning(
        "ONE REVERSAL ONLY"
    )

    logging.warning(
        "EXCHANGE ENTRY/CLOSE CONFIRMATION"
    )

    logging.warning(
        "=================================================="
    )


    update_server_ip()


    load_all_accounts()


    threading.Thread(
        target=run_websocket,
        daemon=True,
    ).start()


    start_dashboard()
