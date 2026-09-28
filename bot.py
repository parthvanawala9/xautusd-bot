import os
import time
import json
import hmac
import hashlib
import logging
import threading
import uuid
import atexit

try:
    import fcntl
except ImportError:
    fcntl = None
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, parse_qs, urlparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import requests
import websocket
from dotenv import load_dotenv
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
DASHBOARD_PORT = int(os.getenv("PORT") or os.getenv("DASHBOARD_PORT") or "8000")
PROCESS_LOCK_FILE = os.path.join(PERSISTENT_DATA_DIR, "delta_bot_process.lock")
PROCESS_LOCK_HANDLE = None
SESSION_START_TIME = dtime(5, 30)
TRADING_START_TIME = dtime(5, 45)
RECONNECT_SECONDS = 5
ENTRY_CONFIRM_TIMEOUT = 10.0
CLOSE_CONFIRM_TIMEOUT = 10.0
POSITION_POLL_INTERVAL = 0.25
EXECUTION_UNKNOWN_RECHECK_INTERVAL = 5.0
STATE_DIR = os.path.join(PERSISTENT_DATA_DIR, "account_states")
HISTORY_DIR = os.path.join(PERSISTENT_DATA_DIR, "account_history")
CLIENTS_FILE = os.path.join(PERSISTENT_DATA_DIR, "clients_config.json")
PRIMARY_ACCOUNT_ID = os.getenv("ACCOUNT_ID", "primary").strip()
PRIMARY_ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "Primary Account").strip()
PRIMARY_API_KEY = os.getenv("DELTA_API_KEY", "").strip()
PRIMARY_API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True,
)
CACHED_SERVER_IP = "Detecting..."
def acquire_single_process_lock():
    """Allow only one bot process for this account/runtime.

    This prevents an old process and a newly deployed process from both
    seeing FLAT locally and sending duplicate entries at the same breakout.
    """
    global PROCESS_LOCK_HANDLE

    if fcntl is None:
        logging.warning("PROCESS LOCK: fcntl unavailable; exchange-position guard remains active.")
        return True

    os.makedirs(os.path.dirname(PROCESS_LOCK_FILE), exist_ok=True)
    PROCESS_LOCK_HANDLE = open(PROCESS_LOCK_FILE, "w", encoding="utf-8")

    try:
        fcntl.flock(
            PROCESS_LOCK_HANDLE.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        logging.critical(
            "ANOTHER BOT PROCESS IS ALREADY RUNNING. "
            "THIS PROCESS WILL NOT START, SO DUPLICATE TRADES ARE BLOCKED."
        )
        try:
            PROCESS_LOCK_HANDLE.close()
        except Exception:
            pass
        PROCESS_LOCK_HANDLE = None
        return False

    PROCESS_LOCK_HANDLE.write(str(os.getpid()))
    PROCESS_LOCK_HANDLE.flush()
    atexit.register(release_single_process_lock)
    logging.info("SINGLE-PROCESS LOCK ACQUIRED | PID=%s", os.getpid())
    return True

def release_single_process_lock():
    global PROCESS_LOCK_HANDLE
    if PROCESS_LOCK_HANDLE is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(
                PROCESS_LOCK_HANDLE.fileno(),
                fcntl.LOCK_UN,
            )
    except Exception:
        pass
    try:
        PROCESS_LOCK_HANDLE.close()
    except Exception:
        pass
    PROCESS_LOCK_HANDLE = None

def now_ist():
    return datetime.now(IST)
def update_server_ip():
    global CACHED_SERVER_IP
    try:
        res = requests.get("https://api.ipify.org?format=json", timeout=5)
        ip = res.json().get("ip")
        if ip:
            CACHED_SERVER_IP = ip
            logging.warning("==================================================")
            logging.warning(" RAILWAY OUTBOUND IP --> %s", ip)
            logging.warning(" WHITELIST THIS IP IN DELTA EXCHANGE API SETTINGS")
            logging.warning("==================================================")
    except Exception as e:
        logging.warning("IP FETCH ERROR | %s", e)
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
def safe_filename(value):
    result = ""
    for char in str(value):
        result += char if char.isalnum() or char in "-_" else "_"
    return result or "account"
def account_state_file(unique_id):
    return os.path.join(STATE_DIR, safe_filename(unique_id) + ".json")
def account_history_file(unique_id):
    return os.path.join(HISTORY_DIR, safe_filename(unique_id) + ".json")
def atomic_write_json(filename, data):
    tmp = filename + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, filename)
def load_clients_config():
    if not os.path.exists(CLIENTS_FILE):
        return {}
    try:
        with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.warning("Client config read error: %s", e)
        return {}
def save_clients_config(cfg):
    atomic_write_json(CLIENTS_FILE, cfg)
def as_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default
def as_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default
class DeltaClient:
    def __init__(self, api_key, api_secret, account_name, symbol):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.account_name = (account_name or "Account").strip()
        self.symbol = (symbol or "").strip().upper()
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "MultiBot/99.0",
        })
    def sign(self, method, path, query="", body=""):
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
            hashlib.sha256,
        ).hexdigest()
        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "User-Agent": "MultiBot/99.0",
        }
    def api(self, method, path, params=None, body=None, auth=False):
        params = params or {}
        body_text = (
            json.dumps(body, separators=(",", ":"))
            if body is not None
            else ""
        )
        query = (
            "?" + urlencode(params, doseq=True)
            if params
            else ""
        )
        headers = (
            self.sign(method, path, query, body_text)
            if auth
            else {}
        )
        response = self.session.request(
            method.upper(),
            BASE_URL + path,
            params=params,
            data=body_text if body is not None else None,
            headers=headers,
            timeout=(3, 8),
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success") is False:
            raise RuntimeError(f"Delta error: {data}")
        return data
    def product(self):
        data = self.api("GET", f"/v2/products/{self.symbol}")
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Invalid product response: {data}")
        return result
    def get_session_high_low(self, session_start_dt):
        try:
            start_ts = int(session_start_dt.timestamp())
            end_ts = int(now_ist().timestamp())
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
            candles = data.get("result", [])
            if not isinstance(candles, list) or not candles:
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
                    elif isinstance(candle, list) and len(candle) >= 4:
                        ts_raw = candle[0]
                        high_raw = candle[2]
                        low_raw = candle[3]
                    else:
                        continue
                    if ts_raw is not None:
                        ts = float(ts_raw)
                        if ts > 100000000000:
                            ts /= 1000.0
                        if ts < start_ts or ts > end_ts:
                            continue
                    high = Decimal(str(high_raw))
                    low = Decimal(str(low_raw))
                    if high > 0 and (highest is None or high > highest):
                        highest = high
                    if low > 0 and (lowest is None or low < lowest):
                        lowest = low
                except Exception:
                    continue
            return highest, lowest
        except Exception as e:
            logging.warning(
                "[%s] Session high/low error: %s",
                self.symbol,
                e,
            )
            return None, None
    def position(self, product_id):
        data = self.api(
            "GET",
            "/v2/positions",
            params={"product_id": int(product_id)},
            auth=True,
        )
        result = data.get("result", {})
        pos_item = {}
        if isinstance(result, dict):
            pos_item = result
        elif isinstance(result, list):
            for p in result:
                if (
                    isinstance(p, dict)
                    and as_int(p.get("product_id"), 0) == int(product_id)
                ):
                    pos_item = p
                    break
            if not pos_item and result and isinstance(result[0], dict):
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
                if raw_entry is not None and float(raw_entry) > 0
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
                return float(value) if value is not None else None
            except Exception:
                return None
        raw_size = as_int(pos_item.get("size"), 0)
        try:
            unrealized = float(pos_item.get("unrealized_pnl", 0) or 0)
        except Exception:
            unrealized = 0.0
        try:
            leverage = int(lev_val) if lev_val else None
        except Exception:
            leverage = None
        return {
            "size": raw_size,
            "entry_price": entry_val,
            "stop_loss": fval("stop_loss"),
            "liquidation_price": fval("liquidation_price"),
            "bankruptcy_price": fval("bankruptcy_price"),
            "margin": fval("margin"),
            "mark_price": fval("mark_price"),
            "unrealized_pnl": unrealized,
            "leverage": leverage,
        }
    def balance(self):
        data = self.api(
            "GET",
            "/v2/wallet/balances",
            auth=True,
        )
        result = data.get("result", [])
        if isinstance(result, dict):
            result = [result]
        for wallet in result:
            if not isinstance(wallet, dict):
                continue
            asset = str(
                wallet.get("asset_symbol", "")
            ).upper()
            if asset in ("USD", "USDT"):
                value = (
                    wallet.get("available_balance")
                    or wallet.get("balance")
                )
                if value is not None:
                    return Decimal(str(value))
        raise RuntimeError("USD/USDT balance not found.")
    def set_leverage(self, product_id, leverage_val):
        self.api(
            "POST",
            f"/v2/products/{product_id}/orders/leverage",
            body={"leverage": str(leverage_val)},
            auth=True,
        )
    def order_size(self, product_info, price, leverage, balance_fraction):
        balance = self.balance()
        margin = balance * balance_fraction
        notional = margin * leverage
        contract_value = Decimal(
            str(
                product_info.get("contract_value")
                or product_info.get("contract_value_usd")
                or "0.001"
            )
        )
        if contract_value <= 0:
            contract_value = Decimal("0.001")
        raw = notional / price / contract_value
        increment = Decimal(
            str(
                product_info.get("lot_size")
                or product_info.get("order_size_increment")
                or "1"
            )
        )
        minimum = Decimal(
            str(
                product_info.get("min_order_size")
                or product_info.get("minimum_order_size")
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
            raise RuntimeError("Order size calculated as zero.")
        return size
    def cancel_all_orders(self, product_id):
        try:
            return self.api(
                "DELETE",
                "/v2/orders/all",
                body={"product_id": int(product_id)},
                auth=True,
            )
        except Exception as e:
            logging.warning(
                "[%s] Cancel all orders failed: %s",
                self.symbol,
                e,
            )
            return None
    def make_client_order_id(self, prefix):
        return (
            f"{prefix}_{int(time.time() * 1000)}_"
            f"{uuid.uuid4().hex[:10]}"
        )[-32:]
    def market_entry_pure(self, product_id, side, size):
        body = {
            "product_id": int(product_id),
            "product_symbol": self.symbol,
            "size": int(abs(size)),
            "side": side,
            "order_type": "market_order",
            "client_order_id": self.make_client_order_id("entry"),
        }
        return self.api(
            "POST",
            "/v2/orders",
            body=body,
            auth=True,
        )
    def close_position(self, product_id, size):
        if size == 0:
            return None
        self.cancel_all_orders(product_id)
        side = "sell" if size > 0 else "buy"
        body = {
            "product_id": int(product_id),
            "product_symbol": self.symbol,
            "size": abs(int(size)),
            "side": side,
            "order_type": "market_order",
            "reduce_only": True,
            "client_order_id": self.make_client_order_id("close"),
        }
        return self.api(
            "POST",
            "/v2/orders",
            body=body,
            auth=True,
        )
    def last_traded_price(self):
        try:
            data = self.api(
                "GET",
                f"/v2/tickers/{self.symbol}",
            )
            result = data.get("result")
            if isinstance(result, dict):
                price = (
                    result.get("close")
                    or result.get("spot_price")
                    or result.get("ltp")
                )
                if price is not None:
                    return Decimal(str(price))
        except Exception:
            pass
        return None
def load_trade_history(unique_id):
    filename = account_history_file(unique_id)
    if not os.path.exists(filename):
        return []
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []
def save_trade_history(unique_id, history):
    atomic_write_json(
        account_history_file(unique_id),
        history,
    )
def calculate_trade_pnl(
    direction,
    entry_price,
    exit_price,
    size,
    product_info,
):
    try:
        entry = Decimal(str(entry_price))
        exit_val = Decimal(str(exit_price))
        qty = Decimal(str(abs(size)))
        contract_value = Decimal(
            str(
                product_info.get("contract_value")
                or product_info.get("contract_value_usd")
                or "0.001"
            )
        )
        if contract_value <= 0:
            contract_value = Decimal("0.001")
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
            t for t in trades
            if float(t.get("pnl", 0) or 0) > 0
        ]
        losses = [
            t for t in trades
            if float(t.get("pnl", 0) or 0) < 0
        ]
        pnl = sum(
            Decimal(
                str(
                    t.get("pnl", 0) or 0
                )
            )
            for t in trades
        )
        return {
            "total_trades": total,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": (
                len(wins) / total * 100
                if total
                else 0.0
            ),
            "pnl": float(pnl),
        }
    today = now_ist().strftime("%Y-%m-%d")
    today_trades = [
        trade for trade in history
        if str(trade.get("date", "")).startswith(today)
    ]
    return {
        "today": compute_stats(today_trades),
        "all_time": compute_stats(history),
    }
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
        self.base_account_id = account_id
        self.symbol = symbol.strip().upper()
        self.strategy_key = (
            f"breakout_sar_{self.symbol.lower()}"
        )
        self.unique_id = (
            f"{account_id}_{self.symbol}_{self.strategy_key}"
        )
        self.account_name = (
            f"{account_name} "
            f"[{self.symbol}: Breakout + Reversal]"
        )
        self.account_type = account_type
        self.subscription = subscription or {}
        self.client = DeltaClient(
            api_key,
            api_secret,
            account_name,
            self.symbol,
        )
        self.product = None
        self.product_id = 0
        self.session_start = None
        self.day_high = None
        self.day_low = None
        self.last_price = None
        self.prev_price = None
        self.last_strategy_price = None
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
        self.balance_fraction = Decimal("0.10")
        self.load_state()
        self.save()
    def is_expired(self):
        if self.account_type == "primary":
            return False
        expiry = self.subscription.get("expiry")
        if not expiry:
            return False
        try:
            return (
                now_ist().date()
                > datetime.strptime(
                    expiry,
                    "%Y-%m-%d",
                ).date()
            )
        except Exception:
            return False
    def load_state(self):
        filename = account_state_file(self.unique_id)
        if not os.path.exists(filename):
            return
        try:
            with open(filename, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("session_start"):
                self.session_start = datetime.fromisoformat(
                    state["session_start"]
                )
            if state.get("day_high") is not None:
                self.day_high = Decimal(str(state["day_high"]))
            if state.get("day_low") is not None:
                self.day_low = Decimal(str(state["day_low"]))
            self.position = state.get("position")
            self.stop_loss = float(
                state.get("stop_loss", 0) or 0
            )
            self.entry_price = state.get("entry_price")
            self.size = int(
                state.get("size", 0) or 0
            )
            if state.get("leverage") is not None:
                self.leverage = Decimal(
                    str(state["leverage"])
                )
            if state.get("balance_fraction") is not None:
                self.balance_fraction = Decimal(
                    str(state["balance_fraction"])
                )
            self.bot_enabled = bool(
                state.get("bot_enabled", False)
            )
            self.stop_reason = state.get("stop_reason")
            self.ready = bool(state.get("ready", False))
            self.trading_armed = bool(
                state.get("trading_armed", False)
            )
            self.is_reversal_position = bool(
                state.get("is_reversal_position", False)
            )
            self.base_breakout_ready = bool(
                state.get("base_breakout_ready", True)
            )
            self.execution_uncertain = bool(
                state.get("execution_uncertain", False)
            )
            if not self.position or self.size <= 0:
                self.position = None
                self.size = 0
                self.entry_price = None
                self.is_reversal_position = False
        except Exception as e:
            logging.warning(
                "[%s] State load error: %s",
                self.symbol,
                e,
            )
    def save(self):
        data = {
            "account_id": self.unique_id,
            "account_name": self.account_name,
            "symbol": self.symbol,
            "session_start": (
                self.session_start.isoformat()
                if self.session_start
                else None
            ),
            "day_high": (
                str(self.day_high)
                if self.day_high is not None
                else None
            ),
            "day_low": (
                str(self.day_low)
                if self.day_low is not None
                else None
            ),
            "position": self.position,
            "stop_loss": self.stop_loss,
            "entry_price": self.entry_price,
            "size": self.size,
            "leverage": int(self.leverage),
            "balance_fraction": float(self.balance_fraction),
            "bot_enabled": self.bot_enabled,
            "stop_reason": self.stop_reason,
            "ready": self.ready,
            "trading_armed": self.trading_armed,
            "is_reversal_position": self.is_reversal_position,
            "base_breakout_ready": self.base_breakout_ready,
            "execution_uncertain": self.execution_uncertain,
        }
        atomic_write_json(
            account_state_file(self.unique_id),
            data,
        )
    def prepare_product(self):
        if self.product_id:
            return True
        try:
            self.product = self.client.product()
            self.product_id = int(self.product["id"])
            return True
        except Exception as e:
            logging.error(
                "[%s] Product prepare error: %s",
                self.symbol,
                e,
            )
            return False
    def get_5m_candles(self, limit=5):
        try:
            end_ts = int(now_ist().timestamp())
            start_ts = end_ts - limit * 5 * 60
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
            candles = data.get("result", [])
            formatted = []
            for candle in candles:
                try:
                    if isinstance(candle, dict):
                        ts = float(
                            candle.get("time")
                            or candle.get("timestamp")
                            or candle.get("start")
                            or 0
                        )
                        if ts > 100000000000:
                            ts /= 1000.0
                        formatted.append({
                            "time": ts,
                            "high": float(candle.get("high")),
                            "low": float(candle.get("low")),
                            "close": float(candle.get("close")),
                        })
                    elif isinstance(candle, list) and len(candle) >= 5:
                        ts = float(candle[0])
                        if ts > 100000000000:
                            ts /= 1000.0
                        formatted.append({
                            "time": ts,
                            "high": float(candle[2]),
                            "low": float(candle[3]),
                            "close": float(candle[4]),
                        })
                except Exception:
                    continue
            formatted.sort(key=lambda x: x["time"])
            return formatted
        except Exception:
            return []
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
            return self.client.position(self.product_id)
        except Exception as e:
            logging.warning(
                "[%s] Exchange position read failed: %s",
                self.symbol,
                e,
            )
            return None
    def wait_for_position(
        self,
        expected_direction=None,
        timeout=ENTRY_CONFIRM_TIMEOUT,
    ):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                position = self.client.position(self.product_id)
                exchange_size = as_int(position.get("size"), 0)
                if exchange_size != 0:
                    if (
                        expected_direction == "LONG"
                        and exchange_size > 0
                    ):
                        return position
                    if (
                        expected_direction == "SHORT"
                        and exchange_size < 0
                    ):
                        return position
                    if expected_direction is None:
                        return position
            except Exception as e:
                logging.warning(
                    "[%s] Position confirmation error: %s",
                    self.symbol,
                    e,
                )
            time.sleep(POSITION_POLL_INTERVAL)
        return None
    def wait_until_flat(self, timeout=CLOSE_CONFIRM_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                position = self.client.position(self.product_id)
                exchange_size = as_int(position.get("size"), 0)
                if exchange_size == 0:
                    return True
            except Exception as e:
                logging.warning(
                    "[%s] Flat confirmation error: %s",
                    self.symbol,
                    e,
                )
            time.sleep(POSITION_POLL_INTERVAL)
        return False
    def reconcile_exchange_state(self, allow_local_updates=True):
        with self.lock:
            if not self.prepare_product():
                return False
            try:
                position = self.client.position(self.product_id)
            except Exception as e:
                logging.error(
                    "[%s] RECONCILE FAILED: %s",
                    self.symbol,
                    e,
                )
                self.execution_uncertain = True
                self.execution_unknown_since = (
                    self.execution_unknown_since or time.time()
                )
                self.save()
                return False
            exchange_size = as_int(position.get("size"), 0)
            if exchange_size != 0:
                exchange_direction = (
                    "LONG" if exchange_size > 0 else "SHORT"
                )
                exchange_entry = position.get("entry_price")
                if self.position is None or self.size <= 0:
                    logging.warning(
                        "[%s] RECONCILE: LOCAL FLAT BUT EXCHANGE HAS "
                        "%s SIZE=%s. ADOPTING EXCHANGE POSITION.",
                        self.symbol,
                        exchange_direction,
                        abs(exchange_size),
                    )
                    self.position = exchange_direction
                    self.size = abs(exchange_size)
                    self.entry_price = (
                        float(exchange_entry)
                        if exchange_entry
                        else self.entry_price
                    )
                    if self.entry_price is None:
                        self.entry_price = self.last_price
                    self.base_breakout_ready = False
                else:
                    if (
                        self.position != exchange_direction
                        or self.size != abs(exchange_size)
                    ):
                        logging.error(
                            "[%s] RECONCILE MISMATCH | LOCAL=%s/%s | "
                            "EXCHANGE=%s/%s",
                            self.symbol,
                            self.position,
                            self.size,
                            exchange_direction,
                            abs(exchange_size),
                        )
                        self.position = exchange_direction
                        self.size = abs(exchange_size)
                        if exchange_entry:
                            self.entry_price = float(exchange_entry)
                self.execution_uncertain = False
                self.execution_unknown_since = None
                self.last_reconciliation_time = time.time()
                self.save()
                return True
            if self.position and self.size > 0:
                logging.warning(
                    "[%s] RECONCILE: LOCAL POSITION EXISTS BUT "
                    "EXCHANGE IS FLAT.",
                    self.symbol,
                )
                self.position = None
                self.entry_price = None
                self.size = 0
                self.stop_loss = 0.0
                self.is_reversal_position = False
                self.base_breakout_ready = True
            self.execution_uncertain = False
            self.execution_unknown_since = None
            self.last_reconciliation_time = time.time()
            self.save()
            return True
    def update_settings(self, new_lev, new_frac=None):
        with self.lock:
            try:
                self.leverage = Decimal(str(new_lev))
                self.balance_fraction = Decimal("0.10")
                if self.product_id:
                    self.client.set_leverage(
                        self.product_id,
                        self.leverage,
                    )
                self.save()
                return {
                    "success": True,
                    "message": (
                        f"Saved {self.symbol} Settings! "
                        f"Default: {int(self.leverage)}x | Margin: 10%"
                    ),
                }
            except Exception as e:
                return {
                    "success": False,
                    "message": str(e),
                }
    def start_bot(self):
        with self.lock:
            if self.is_expired():
                return {
                    "success": False,
                    "message": "Subscription expired.",
                }
            self.manual_squareoff_flag = False
            self.stop_reason = None
            if not self.prepare_product():
                return {
                    "success": False,
                    "message": "Unable to prepare Delta product.",
                }
            if not self.reconcile_exchange_state():
                self.bot_enabled = False
                self.stop_reason = "EXCHANGE RECONCILIATION FAILED"
                self.save()
                return {
                    "success": False,
                    "message": (
                        "Exchange position could not be reconciled. "
                        "Bot remains stopped."
                    ),
                }
            if self.execution_uncertain:
                self.bot_enabled = False
                self.save()
                return {
                    "success": False,
                    "message": (
                        "Execution state is uncertain. "
                        "Bot remains stopped."
                    ),
                }
            self.bot_enabled = True
            if self.position is None or self.size <= 0:
                self.base_breakout_ready = True
            self.save()
            return {
                "success": True,
                "bot_enabled": True,
                "message": f"Bot for {self.symbol} Started.",
            }
    def stop_bot(self):
        with self.lock:
            self.bot_enabled = False
            self.stop_reason = "MANUAL STOP"
            self.manual_squareoff_flag = True
            if self.product_id and self.position and self.size > 0:
                try:
                    self.order_in_progress = True
                    exchange_position = self.client.position(
                        self.product_id
                    )
                    exchange_size = as_int(
                        exchange_position.get("size"),
                        0,
                    )
                    if exchange_size != 0:
                        self.client.close_position(
                            self.product_id,
                            exchange_size,
                        )
                        confirmed = self.wait_until_flat()
                    else:
                        confirmed = True
                    if confirmed:
                        self.finish_trade(
                            "MANUAL",
                            self.last_price or 0,
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
                            self.execution_unknown_since or time.time()
                        )
                        logging.error(
                            "[%s] MANUAL STOP: EXCHANGE DID NOT CONFIRM FLAT.",
                            self.symbol,
                        )
                except Exception as e:
                    self.execution_uncertain = True
                    self.execution_unknown_since = (
                        self.execution_unknown_since or time.time()
                    )
                    logging.error(
                        "[%s] Manual close failed: %s",
                        self.symbol,
                        e,
                    )
                finally:
                    self.order_in_progress = False
            else:
                try:
                    if self.product_id:
                        position = self.client.position(
                            self.product_id
                        )
                        exchange_size = as_int(
                            position.get("size"),
                            0,
                        )
                        if exchange_size != 0:
                            self.execution_uncertain = True
                            self.execution_unknown_since = (
                                self.execution_unknown_since or time.time()
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
                        self.execution_unknown_since or time.time()
                    )
                    logging.error(
                        "[%s] Stop reconciliation error: %s",
                        self.symbol,
                        e,
                    )
            self.save()
            return {
                "success": True,
                "bot_enabled": False,
                "message": f"Bot for {self.symbol} Stopped.",
            }
    def check_session_change(self, now):
        current_session = get_current_session_start(now)
        if self.session_start == current_session:
            return True
        if self.product_id:
            try:
                self.client.cancel_all_orders(self.product_id)
            except Exception:
                pass
            try:
                exchange_position = self.client.position(
                    self.product_id
                )
                exchange_size = as_int(
                    exchange_position.get("size"),
                    0,
                )
                if exchange_size != 0:
                    logging.info(
                        "[%s] SESSION CHANGE: closing position %s",
                        self.symbol,
                        exchange_size,
                    )
                    if self.order_in_progress:
                        return False
                    self.order_in_progress = True
                    try:
                        self.client.close_position(
                            self.product_id,
                            exchange_size,
                        )
                        confirmed = self.wait_until_flat()
                    finally:
                        self.order_in_progress = False
                    if not confirmed:
                        logging.error(
                            "[%s] SESSION CHANGE: POSITION NOT FLAT.",
                            self.symbol,
                        )
                        self.execution_uncertain = True
                        self.execution_unknown_since = (
                            self.execution_unknown_since or time.time()
                        )
                        self.save()
                        return False
                    self.finish_trade(
                        "SESSION_CHANGE_CLOSE",
                        self.last_price or 0,
                    )
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
                    "[%s] Session change error: %s",
                    self.symbol,
                    e,
                )
                self.execution_uncertain = True
                self.execution_unknown_since = (
                    self.execution_unknown_since or time.time()
                )
                self.save()
                return False
        self.session_start = current_session
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
            and not is_weekend(self.symbol, now)
        ):
            high, low = self.client.get_session_high_low(
                self.session_start
            )
            if high is not None and low is not None:
                self.day_high = high
                self.day_low = low
                self.ready = True
        self.save()
        return True
    def prepare(self, now):
        if not self.prepare_product():
            return False
        if self.session_start is None:
            self.session_start = get_current_session_start(now)
        if is_weekend(self.symbol, now):
            return True
        if self.day_high is None or self.day_low is None:
            high, low = self.client.get_session_high_low(
                self.session_start
            )
            if high is not None and low is not None:
                self.day_high = high
                self.day_low = low
                self.ready = True
                self.save()
        return True
    def estimate_liquidation_price(
        self,
        entry_price,
        leverage,
        direction,
    ):
        entry = Decimal(str(entry_price))
        lev = Decimal(str(leverage))
        if entry <= 0 or lev <= 0:
            return None
        maintenance_raw = (
            self.product.get("maintenance_margin", 0)
            if self.product
            else 0
        )
        taker_raw = (
            self.product.get("taker_commission_rate", 0)
            if self.product
            else 0
        )
        try:
            maintenance = (
                Decimal(str(maintenance_raw))
                / Decimal("100")
            )
        except Exception:
            maintenance = Decimal("0")
        try:
            taker_fee = Decimal(str(taker_raw))
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
    def get_leverage_ladder(self):
        if "BTC" in self.symbol:
            return list(range(200, 9, -10))
        return list(range(100, 9, -10))
    def exchange_position_blocks_entry(self):
        """Return True when Delta already has an open position for this symbol.

        Exchange state is the source of truth. A local FLAT state must never
        be allowed to create a second position. If Delta is already in a
        position, adopt that position locally and block the new entry.
        """
        try:
            if not self.product_id:
                return True

            exchange_position = self.client.position(
                self.product_id
            )
            exchange_size = as_int(
                exchange_position.get("size"),
                0,
            )

            if exchange_size == 0:
                return False

            exchange_direction = (
                "LONG" if exchange_size > 0 else "SHORT"
            )

            logging.warning(
                "[%s] NEW ENTRY BLOCKED | EXCHANGE POSITION ALREADY OPEN | "
                "Direction=%s | Size=%s | Entry=%s | Leverage=%s | Liq=%s",
                self.symbol,
                exchange_direction,
                abs(exchange_size),
                exchange_position.get("entry_price"),
                exchange_position.get("leverage"),
                exchange_position.get("liquidation_price"),
            )

            self.position = exchange_direction
            self.size = abs(exchange_size)
            if exchange_position.get("entry_price") is not None:
                self.entry_price = float(
                    exchange_position.get("entry_price")
                )
            if exchange_position.get("leverage") is not None:
                try:
                    self.leverage = Decimal(
                        str(exchange_position.get("leverage"))
                    )
                except Exception:
                    pass

            self.base_breakout_ready = False
            self.execution_uncertain = False
            self.execution_unknown_since = None
            self.last_reconciliation_time = time.time()
            self.save()
            return True

        except Exception as e:
            # Fail closed. If exchange state cannot be verified, do NOT enter.
            logging.error(
                "[%s] ENTRY SAFETY CHECK FAILED | NEW ENTRY BLOCKED | %s",
                self.symbol,
                e,
            )
            self.execution_uncertain = True
            self.execution_unknown_since = (
                self.execution_unknown_since or time.time()
            )
            self.save()
            return True

    def enter(
        self,
        direction,
        price,
        initial_sl,
        is_reversal=False,
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
                "[%s] ENTRY BLOCKED: execution uncertain.",
                self.symbol,
            )
            return False
        if self.order_in_progress:
            logging.warning(
                "[%s] ENTRY BLOCKED: another order active.",
                self.symbol,
            )
            return False

        # FINAL EXCHANGE-SIDE GUARD: never add to an existing position.
        # This check happens immediately before any order is sent.
        if self.exchange_position_blocks_entry():
            logging.warning(
                "[%s] ENTRY CANCELLED: one position already exists or "
                "exchange state could not be verified.",
                self.symbol,
            )
            return False

        self.order_in_progress = True
        try:
            current_position = self.client.position(
                self.product_id
            )
            current_size = as_int(
                current_position.get("size"),
                0,
            )
            if current_size != 0:
                logging.warning(
                    "[%s] ENTRY BLOCKED: exchange already has "
                    "position %s",
                    self.symbol,
                    current_size,
                )
                self.reconcile_exchange_state()
                return False
            leverage_ladder = self.get_leverage_ladder()
            confirmed_position = None
            chosen_leverage = None
            order_completed = False
            for leverage_value in leverage_ladder:
                leverage_decimal = Decimal(str(leverage_value))
                candidate_liquidation = (
                    self.estimate_liquidation_price(
                        price,
                        leverage_decimal,
                        direction,
                    )
                )
                if candidate_liquidation is None:
                    continue
                if (
                    direction == "LONG"
                    and candidate_liquidation >= Decimal(str(initial_sl))
                ):
                    continue
                if (
                    direction == "SHORT"
                    and candidate_liquidation <= Decimal(str(initial_sl))
                ):
                    continue
                try:
                    self.client.set_leverage(
                        self.product_id,
                        leverage_decimal,
                    )
                    size = self.client.order_size(
                        self.product,
                        Decimal(str(price)),
                        leverage_decimal,
                        Decimal("0.10"),
                    )
                    side = "buy" if direction == "LONG" else "sell"
                    response = self.client.market_entry_pure(
                        self.product_id,
                        side,
                        size,
                    )
                    logging.info(
                        "[%s] ENTRY ORDER SENT | %s | Size=%s | "
                        "Lev=%sx | Response=%s",
                        self.symbol,
                        direction,
                        size,
                        leverage_decimal,
                        response,
                    )
                    confirmed_position = self.wait_for_position(
                        expected_direction=direction,
                        timeout=ENTRY_CONFIRM_TIMEOUT,
                    )
                    if confirmed_position is None:
                        self.execution_uncertain = True
                        self.execution_unknown_since = time.time()
                        self.save()
                        logging.error(
                            "[%s] ENTRY EXECUTION UNKNOWN. NO SECOND ORDER.",
                            self.symbol,
                        )
                        return False
                    confirmed_size = as_int(
                        confirmed_position.get("size"),
                        0,
                    )
                    if (
                        direction == "LONG"
                        and confirmed_size <= 0
                    ):
                        self.execution_uncertain = True
                        self.execution_unknown_since = time.time()
                        self.save()
                        return False
                    if (
                        direction == "SHORT"
                        and confirmed_size >= 0
                    ):
                        self.execution_uncertain = True
                        self.execution_unknown_since = time.time()
                        self.save()
                        return False
                    chosen_leverage = leverage_decimal
                    order_completed = True
                    break
                except Exception as e:
                    logging.warning(
                        "[%s] Entry attempt %sx failed: %s",
                        self.symbol,
                        leverage_value,
                        e,
                    )
                    try:
                        check_position = self.client.position(
                            self.product_id
                        )
                        check_size = as_int(
                            check_position.get("size"),
                            0,
                        )
                        if check_size != 0:
                            exchange_direction = (
                                "LONG" if check_size > 0 else "SHORT"
                            )
                            if exchange_direction == direction:
                                confirmed_position = check_position
                                chosen_leverage = leverage_decimal
                                order_completed = True
                                break
                            self.execution_uncertain = True
                            self.execution_unknown_since = time.time()
                            self.save()
                            logging.error(
                                "[%s] ENTRY ERROR BUT OPPOSITE "
                                "POSITION EXISTS. NO MORE ORDERS.",
                                self.symbol,
                            )
                            return False
                    except Exception:
                        self.execution_uncertain = True
                        self.execution_unknown_since = time.time()
                        self.save()
                        return False
            if not order_completed:
                return False
            exchange_size = abs(
                as_int(
                    confirmed_position.get("size"),
                    0,
                )
            )
            exchange_entry = confirmed_position.get("entry_price")
            if exchange_size <= 0:
                self.execution_uncertain = True
                self.execution_unknown_since = time.time()
                self.save()
                return False
            self.position = direction
            self.entry_price = (
                float(exchange_entry)
                if exchange_entry
                else float(price)
            )
            self.size = exchange_size
            self.leverage = chosen_leverage
            self.stop_loss = float(initial_sl)
            self.is_reversal_position = bool(is_reversal)
            self.base_breakout_ready = False
            self.execution_uncertain = False
            self.execution_unknown_since = None
            self.last_execution_time = time.time()
            self.save()
            logging.info(
                "[%s] CONFIRMED ENTER | %s | %s | Entry=%s | "
                "Size=%s | Lev=%sx | SL=%s",
                self.symbol,
                direction,
                "REVERSAL" if is_reversal else "BASE",
                self.entry_price,
                self.size,
                int(chosen_leverage),
                self.stop_loss,
            )
            return True
        except Exception as e:
            logging.error(
                "[%s] Entry error: %s",
                self.symbol,
                e,
            )
            return False
        finally:
            self.order_in_progress = False
    def close_current_position(self, reason, exit_price):
        if not self.position or self.size <= 0:
            try:
                exchange_position = self.client.position(
                    self.product_id
                )
                exchange_size = as_int(
                    exchange_position.get("size"),
                    0,
                )
                if exchange_size == 0:
                    return True
                self.execution_uncertain = True
                self.execution_unknown_since = time.time()
                self.save()
                return False
            except Exception:
                self.execution_uncertain = True
                self.execution_unknown_since = time.time()
                self.save()
                return False
        if self.order_in_progress:
            logging.warning(
                "[%s] CLOSE BLOCKED: execution active.",
                self.symbol,
            )
            return False
        self.order_in_progress = True
        try:
            exchange_position = self.client.position(
                self.product_id
            )
            exchange_size = as_int(
                exchange_position.get("size"),
                0,
            )
            if exchange_size == 0:
                logging.warning(
                    "[%s] %s: exchange already flat.",
                    self.symbol,
                    reason,
                )
                self.finish_trade(reason, exit_price)
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
            self.client.close_position(
                self.product_id,
                exchange_size,
            )
            confirmed = self.wait_until_flat()
            if not confirmed:
                self.execution_uncertain = True
                self.execution_unknown_since = time.time()
                logging.error(
                    "[%s] %s: EXCHANGE DID NOT CONFIRM FLAT.",
                    self.symbol,
                    reason,
                )
                self.save()
                return False
            self.finish_trade(reason, exit_price)
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
                "[%s] CLOSE CONFIRMED | Reason=%s",
                self.symbol,
                reason,
            )
            return True
        except Exception as e:
            logging.error(
                "[%s] Close error: %s",
                self.symbol,
                e,
            )
            self.execution_uncertain = True
            self.execution_unknown_since = (
                self.execution_unknown_since or time.time()
            )
            self.save()
            return False
        finally:
            self.order_in_progress = False
    def reverse_from_sl(self, price, prev_candle):
        if not self.position or self.size <= 0:
            return False
        if self.execution_uncertain:
            return False
        old_direction = self.position
        old_is_reversal = self.is_reversal_position
        if old_is_reversal:
            logging.info(
                "[%s] REVERSAL %s SL HIT -> FLAT -> "
                "NO SECOND REVERSAL",
                self.symbol,
                old_direction,
            )
            closed = self.close_current_position(
                "REVERSAL_SL_HIT",
                price,
            )
            if not closed:
                return False
            price_decimal = Decimal(str(price))
            if (
                self.day_high is None
                or price_decimal > self.day_high
            ):
                self.day_high = price_decimal
            if (
                self.day_low is None
                or price_decimal < self.day_low
            ):
                self.day_low = price_decimal
            self.base_breakout_ready = True
            self.last_strategy_price = float(price)
            self.save()
            logging.info(
                "[%s] REVERSAL COMPLETE -> FLAT | "
                "WAITING FOR NEW CURRENT HIGH/LOW BREAK | "
                "High=%s | Low=%s",
                self.symbol,
                self.day_high,
                self.day_low,
            )
            return True
        if old_direction == "LONG":
            reversal_sl = float(prev_candle["high"])
            logging.info(
                "[%s] BASE LONG SL HIT -> ONE SHORT REVERSAL | "
                "Reversal SL=%s",
                self.symbol,
                reversal_sl,
            )
            closed = self.close_current_position(
                "SL_HIT_REVERSAL",
                price,
            )
            if not closed:
                logging.error(
                    "[%s] LONG close not confirmed. "
                    "SHORT reversal BLOCKED.",
                    self.symbol,
                )
                return False
            self.base_breakout_ready = False
            self.save()
            success = self.enter(
                "SHORT",
                price,
                reversal_sl,
                is_reversal=True,
            )
            if success:
                logging.info(
                    "[%s] ONE SHORT REVERSAL CONFIRMED.",
                    self.symbol,
                )
                return True
            self._handle_failed_reversal()
            return False
        if old_direction == "SHORT":
            reversal_sl = float(prev_candle["low"])
            logging.info(
                "[%s] BASE SHORT SL HIT -> ONE LONG REVERSAL | "
                "Reversal SL=%s",
                self.symbol,
                reversal_sl,
            )
            closed = self.close_current_position(
                "SL_HIT_REVERSAL",
                price,
            )
            if not closed:
                logging.error(
                    "[%s] SHORT close not confirmed. "
                    "LONG reversal BLOCKED.",
                    self.symbol,
                )
                return False
            self.base_breakout_ready = False
            self.save()
            success = self.enter(
                "LONG",
                price,
                reversal_sl,
                is_reversal=True,
            )
            if success:
                logging.info(
                    "[%s] ONE LONG REVERSAL CONFIRMED.",
                    self.symbol,
                )
                return True
            self._handle_failed_reversal()
            return False
        return False
    def _handle_failed_reversal(self):
        try:
            position = self.client.position(self.product_id)
            exchange_size = as_int(position.get("size"), 0)
            if exchange_size == 0:
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
                    self.execution_unknown_since or time.time()
                )
        except Exception:
            self.execution_uncertain = True
            self.execution_unknown_since = (
                self.execution_unknown_since or time.time()
            )
        self.save()
    def force_weekend_flat(self):
        with self.lock:
            if not self.product_id:
                return
            try:
                exchange_position = self.client.position(
                    self.product_id
                )
                exchange_size = as_int(
                    exchange_position.get("size"),
                    0,
                )
                if exchange_size != 0:
                    if self.order_in_progress:
                        return
                    self.order_in_progress = True
                    try:
                        self.client.close_position(
                            self.product_id,
                            exchange_size,
                        )
                        confirmed = self.wait_until_flat()
                    finally:
                        self.order_in_progress = False
                    if confirmed:
                        if self.position and self.size > 0:
                            self.finish_trade(
                                "WEEKEND_CLOSE",
                                self.last_price or 0,
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
                            "[%s] WEEKEND POSITION CLOSED.",
                            self.symbol,
                        )
                    else:
                        self.execution_uncertain = True
                        self.execution_unknown_since = (
                            self.execution_unknown_since or time.time()
                        )
                        logging.error(
                            "[%s] WEEKEND CLOSE NOT CONFIRMED.",
                            self.symbol,
                        )
                        self.save()
                else:
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
                    self.execution_unknown_since or time.time()
                )
                logging.error(
                    "[%s] Weekend close error: %s",
                    self.symbol,
                    e,
                )
                self.save()
    def evaluate(self, price=None):
        with self.lock:
            if not self.bot_enabled or self.is_expired():
                return
            now = now_ist()
            if is_weekend(self.symbol, now):
                self.force_weekend_flat()
                self.prev_price = None
                return
            if self.execution_uncertain:
                if (
                    time.time() - self.last_reconciliation_time
                    >= EXECUTION_UNKNOWN_RECHECK_INTERVAL
                ):
                    self.reconcile_exchange_state()
                return
            if price is None:
                price = self.client.last_traded_price()
                if price is None:
                    return
            new_price = float(price)
            self.last_price = new_price
            if (
                self.last_strategy_price is not None
                and new_price == self.last_strategy_price
            ):
                return
            self.last_strategy_price = new_price
            if not self.check_session_change(now):
                return
            if not self.prepare(now):
                return
            if self.day_high is None or self.day_low is None:
                return
            self.ready = True
            if now.time() < TRADING_START_TIME:
                self.trading_armed = False
                return
            if not self.trading_armed:
                self.trading_armed = True
                self.save()
                return
            candles = self.get_5m_candles(limit=5)
            if len(candles) < 2:
                return
            prev_candle = candles[-2]
            current_candle = candles[-1]
            current_candle_time = current_candle["time"]
            if self.position == "LONG" and self.size > 0:
                if (
                    self.stop_loss > 0
                    and new_price <= self.stop_loss
                ):
                    logging.info(
                        "[%s] LONG SL HIT | Price=%s | SL=%s | "
                        "Reversal=%s",
                        self.symbol,
                        new_price,
                        self.stop_loss,
                        self.is_reversal_position,
                    )
                    self.reverse_from_sl(
                        new_price,
                        prev_candle,
                    )
                    return
                if (
                    current_candle_time
                    != self.last_checked_candle_time
                ):
                    new_sl = float(prev_candle["low"])
                    self.stop_loss = new_sl
                    self.last_checked_candle_time = current_candle_time
                    self.save()
                    logging.info(
                        "[%s] LONG TRAILING SL UPDATED | SL=%s",
                        self.symbol,
                        new_sl,
                    )
                    if new_price <= self.stop_loss:
                        logging.info(
                            "[%s] NEW TRAILING LONG SL HIT | "
                            "Price=%s | SL=%s",
                            self.symbol,
                            new_price,
                            self.stop_loss,
                        )
                        self.reverse_from_sl(
                            new_price,
                            prev_candle,
                        )
                        return
                return
            if self.position == "SHORT" and self.size > 0:
                if (
                    self.stop_loss > 0
                    and new_price >= self.stop_loss
                ):
                    logging.info(
                        "[%s] SHORT SL HIT | Price=%s | SL=%s | "
                        "Reversal=%s",
                        self.symbol,
                        new_price,
                        self.stop_loss,
                        self.is_reversal_position,
                    )
                    self.reverse_from_sl(
                        new_price,
                        prev_candle,
                    )
                    return
                if (
                    current_candle_time
                    != self.last_checked_candle_time
                ):
                    new_sl = float(prev_candle["high"])
                    self.stop_loss = new_sl
                    self.last_checked_candle_time = current_candle_time
                    self.save()
                    logging.info(
                        "[%s] SHORT TRAILING SL UPDATED | SL=%s",
                        self.symbol,
                        new_sl,
                    )
                    if new_price >= self.stop_loss:
                        logging.info(
                            "[%s] NEW TRAILING SHORT SL HIT | "
                            "Price=%s | SL=%s",
                            self.symbol,
                            new_price,
                            self.stop_loss,
                        )
                        self.reverse_from_sl(
                            new_price,
                            prev_candle,
                        )
                        return
                return
            # Local state can become stale after a restart/crash or when a
            # previous process is still alive. Before evaluating a new
            # breakout, query Delta again. If a position exists, never enter.
            if self.position is None and self.size == 0:
                if self.exchange_position_blocks_entry():
                    return

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
            current_high = float(self.day_high)
            current_low = float(self.day_low)
            if new_price > current_high:
                initial_sl = float(prev_candle["low"])
                self.day_high = Decimal(str(new_price))
                self.save()
                logging.info(
                    "[%s] CURRENT HIGH BREAK | OldHigh=%s | "
                    "BreakPrice=%s | BASE LONG | SL=%s",
                    self.symbol,
                    current_high,
                    new_price,
                    initial_sl,
                )
                success = self.enter(
                    "LONG",
                    new_price,
                    initial_sl,
                    is_reversal=False,
                )
                if success:
                    self.base_breakout_ready = False
                    self.is_reversal_position = False
                    self.save()
                return
            if new_price < current_low:
                initial_sl = float(prev_candle["high"])
                self.day_low = Decimal(str(new_price))
                self.save()
                logging.info(
                    "[%s] CURRENT LOW BREAK | OldLow=%s | "
                    "BreakPrice=%s | BASE SHORT | SL=%s",
                    self.symbol,
                    current_low,
                    new_price,
                    initial_sl,
                )
                success = self.enter(
                    "SHORT",
                    new_price,
                    initial_sl,
                    is_reversal=False,
                )
                if success:
                    self.base_breakout_ready = False
                    self.is_reversal_position = False
                    self.save()
                return
            changed = False
            if (
                self.day_high is None
                or new_price > float(self.day_high)
            ):
                self.day_high = Decimal(str(new_price))
                changed = True
            if (
                self.day_low is None
                or new_price < float(self.day_low)
            ):
                self.day_low = Decimal(str(new_price))
                changed = True
            if changed:
                self.save()
    def finish_trade(self, reason, exit_price):
        if not self.position or not self.entry_price:
            return
        pnl = calculate_trade_pnl(
            self.position,
            self.entry_price,
            exit_price,
            self.size,
            self.product or {"contract_value": "0.001"},
        )
        trade = {
            "id": (
                f"{self.symbol.lower()}_"
                f"{int(time.time() * 1000)}_"
                f"{uuid.uuid4().hex[:8]}"
            ),
            "account_id": self.unique_id,
            "account": self.account_name,
            "symbol": self.symbol,
            "date": now_ist().strftime("%Y-%m-%d %H:%M"),
            "direction": self.position,
            "entry_price": float(self.entry_price),
            "exit_price": float(exit_price),
            "size": self.size,
            "pnl": float(pnl),
            "reason": reason,
            "trade_type": (
                "REVERSAL"
                if self.is_reversal_position
                else "BREAKOUT"
            ),
        }
        history = load_trade_history(self.unique_id)
        history.append(trade)
        save_trade_history(self.unique_id, history)
BOT_ACCOUNTS = {}
ACCOUNTS_LOCK = threading.RLock()
def create_all_accounts():
    new_accounts = {}
    if PRIMARY_API_KEY and PRIMARY_API_SECRET:
        for symbol in ("XAUTUSD", "BTCUSD"):
            bot = BreakoutSARBot(
                PRIMARY_ACCOUNT_ID,
                PRIMARY_ACCOUNT_NAME,
                "primary",
                PRIMARY_API_KEY,
                PRIMARY_API_SECRET,
                symbol,
            )
            new_accounts[bot.unique_id] = bot
    clients_cfg = load_clients_config()
    for client_id, client_data in clients_cfg.items():
        if not isinstance(client_data, dict):
            continue
        subscription = {
            "start": client_data.get("subscription_start"),
            "expiry": client_data.get("subscription_expiry"),
        }
        api_key = (
            client_data.get("api_key") or ""
        ).strip()
        api_secret = (
            client_data.get("api_secret") or ""
        ).strip()
        if not api_key or not api_secret:
            logging.warning(
                "Skipping client %s: API credentials missing.",
                client_id,
            )
            continue
        for symbol in ("XAUTUSD", "BTCUSD"):
            bot = BreakoutSARBot(
                client_id,
                client_data.get("name", "Client"),
                "client",
                api_key,
                api_secret,
                symbol,
                subscription,
            )
            new_accounts[bot.unique_id] = bot
    return new_accounts
def load_all_accounts(preserve_running=True):
    global BOT_ACCOUNTS
    new_accounts = create_all_accounts()
    with ACCOUNTS_LOCK:
        old_accounts = BOT_ACCOUNTS
        for key, new_bot in list(new_accounts.items()):
            old_bot = old_accounts.get(key)
            if old_bot is not None and preserve_running:
                new_accounts[key] = old_bot
        BOT_ACCOUNTS = new_accounts
    logging.info(
        "ACCOUNTS LOADED | Total bots=%s",
        len(BOT_ACCOUNTS),
    )
def get_bot(unique_id):
    with ACCOUNTS_LOCK:
        return BOT_ACCOUNTS.get(unique_id)
def serialize_bot(bot):
    position = bot.read_exchange_position()
    if position is None:
        position = {
            "size": bot.size,
            "entry_price": bot.entry_price,
            "stop_loss": bot.stop_loss,
            "unrealized_pnl": 0,
            "leverage": int(bot.leverage),
            "liquidation_price": None,
        }
    history = load_trade_history(bot.unique_id)
    stats = calculate_statistics(history)
    exchange_size = as_int(position.get("size"), 0)
    if exchange_size > 0:
        exchange_direction = "LONG"
    elif exchange_size < 0:
        exchange_direction = "SHORT"
    else:
        exchange_direction = None
    return {
        "id": bot.unique_id,
        "unique_id": bot.unique_id,
        "account_id": bot.base_account_id,
        "account_name": bot.account_name,
        "account_type": bot.account_type,
        "symbol": bot.symbol,
        "strategy": "Breakout + Reversal",
        "bot_enabled": bot.bot_enabled,
        "expired": bot.is_expired(),
        "ready": bot.ready,
        "trading_armed": bot.trading_armed,
        "session_start": (
            bot.session_start.isoformat()
            if bot.session_start
            else None
        ),
        "day_high": (
            float(bot.day_high)
            if bot.day_high is not None
            else None
        ),
        "day_low": (
            float(bot.day_low)
            if bot.day_low is not None
            else None
        ),
        "last_price": bot.last_price,
        "local_position": bot.position,
        "exchange_position": exchange_direction,
        "position_size": exchange_size,
        "size": abs(exchange_size),
        "entry_price": position.get("entry_price"),
        "strategy_stop_loss": bot.stop_loss,
        "exchange_stop_loss": position.get("stop_loss"),
        "liquidation_price": position.get("liquidation_price"),
        "mark_price": position.get("mark_price"),
        "unrealized_pnl": position.get("unrealized_pnl", 0),
        "leverage": (
            position.get("leverage")
            or int(bot.leverage)
        ),
        "margin": position.get("margin"),
        "is_reversal_position": bot.is_reversal_position,
        "base_breakout_ready": bot.base_breakout_ready,
        "execution_uncertain": bot.execution_uncertain,
        "order_in_progress": bot.order_in_progress,
        "stop_reason": bot.stop_reason,
        "balance_fraction": float(bot.balance_fraction),
        "stats": stats,
        "history": history[-50:],
        "server_ip": CACHED_SERVER_IP,
    }
class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(
            *args,
            directory=BASE_DIR,
            **kwargs,
        )
    def log_message(self, format, *args):
        logging.info("HTTP | " + format, *args)
    def send_json(self, payload, status=200):
        raw = json.dumps(
            payload,
            default=str,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(raw)),
        )
        self.send_header(
            "Cache-Control",
            "no-store",
        )
        self.end_headers()
        self.wfile.write(raw)
    def read_json_body(self):
        try:
            length = int(
                self.headers.get("Content-Length", "0")
            )
        except Exception:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(
                raw.decode("utf-8")
            )
        except Exception:
            return {}
    def find_bot_from_query(self, query):
        bot_id = query.get("id", [None])[0]
        if bot_id:
            return get_bot(bot_id)
        symbol = (
            query.get("symbol", [None])[0]
        )
        account_id = (
            query.get("account_id", [None])[0]
        )
        with ACCOUNTS_LOCK:
            for bot in BOT_ACCOUNTS.values():
                if (
                    (not symbol or bot.symbol == symbol.upper())
                    and (not account_id or bot.base_account_id == account_id)
                ):
                    return bot
        return None
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/api/health":
            self.send_json({
                "success": True,
                "online": True,
                "time": now_ist().isoformat(),
                "server_ip": CACHED_SERVER_IP,
            })
            return
        if path == "/api/dashboard":
            client_token = query.get(
                "token",
                [None],
            )[0]
            with ACCOUNTS_LOCK:
                bots = list(BOT_ACCOUNTS.values())
            if client_token:
                clients_cfg = load_clients_config()
                target_client_id = None
                for client_id, client_data in clients_cfg.items():
                    if not isinstance(client_data, dict):
                        continue
                    stored_token = (
                        client_data.get("dashboard_token")
                        or client_data.get("token")
                    )
                    if (
                        stored_token
                        and str(stored_token) == str(client_token)
                    ):
                        target_client_id = client_id
                        break
                if target_client_id is not None:
                    bots = [
                        bot for bot in bots
                        if bot.base_account_id == target_client_id
                    ]
                else:
                    bots = []
            self.send_json({
                "success": True,
                "server_ip": CACHED_SERVER_IP,
                "time": now_ist().isoformat(),
                "bots": [
                    serialize_bot(bot)
                    for bot in bots
                ],
            })
            return
        if path == "/api/accounts":
            with ACCOUNTS_LOCK:
                bots = list(BOT_ACCOUNTS.values())
            self.send_json({
                "success": True,
                "accounts": [
                    {
                        "id": bot.unique_id,
                        "account_id": bot.base_account_id,
                        "name": bot.account_name,
                        "symbol": bot.symbol,
                        "type": bot.account_type,
                        "enabled": bot.bot_enabled,
                    }
                    for bot in bots
                ],
            })
            return
        if path == "/api/history":
            bot = self.find_bot_from_query(query)
            if bot is None:
                self.send_json({
                    "success": False,
                    "message": "Bot not found.",
                }, 404)
                return
            history = load_trade_history(bot.unique_id)
            self.send_json({
                "success": True,
                "history": history,
                "stats": calculate_statistics(history),
            })
            return
        if path == "/api/state":
            bot = self.find_bot_from_query(query)
            if bot is None:
                self.send_json({
                    "success": False,
                    "message": "Bot not found.",
                }, 404)
                return
            self.send_json({
                "success": True,
                "bot": serialize_bot(bot),
            })
            return
        super().do_GET()
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_json_body()
        if path == "/api/start":
            bot_id = body.get("id") or body.get("unique_id")
            bot = get_bot(bot_id)
            if bot is None:
                self.send_json({
                    "success": False,
                    "message": "Bot not found.",
                }, 404)
                return
            result = bot.start_bot()
            self.send_json(result)
            return
        if path == "/api/stop":
            bot_id = body.get("id") or body.get("unique_id")
            bot = get_bot(bot_id)
            if bot is None:
                self.send_json({
                    "success": False,
                    "message": "Bot not found.",
                }, 404)
                return
            result = bot.stop_bot()
            self.send_json(result)
            return
        if path == "/api/settings":
            bot_id = body.get("id") or body.get("unique_id")
            bot = get_bot(bot_id)
            if bot is None:
                self.send_json({
                    "success": False,
                    "message": "Bot not found.",
                }, 404)
                return
            leverage = (
                body.get("leverage")
                or body.get("lev")
                or int(bot.leverage)
            )
            result = bot.update_settings(
                leverage,
                Decimal("0.10"),
            )
            self.send_json(result)
            return
        if path == "/api/reconcile":
            bot_id = body.get("id") or body.get("unique_id")
            bot = get_bot(bot_id)
            if bot is None:
                self.send_json({
                    "success": False,
                    "message": "Bot not found.",
                }, 404)
                return
            result = bot.reconcile_exchange_state()
            self.send_json({
                "success": result,
                "bot": serialize_bot(bot),
            })
            return
        if path == "/api/reload":
            load_all_accounts(preserve_running=True)
            self.send_json({
                "success": True,
                "message": "Accounts reloaded.",
            })
            return
        if path == "/api/client/add":
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
            if not api_key or not api_secret:
                self.send_json({
                    "success": False,
                    "message": "API key and API secret are required.",
                }, 400)
                return
            clients_cfg = load_clients_config()
            clients_cfg[client_id] = {
                "name": name,
                "api_key": api_key,
                "api_secret": api_secret,
                "subscription_start": body.get(
                    "subscription_start"
                ),
                "subscription_expiry": body.get(
                    "subscription_expiry"
                ),
                "dashboard_token": body.get(
                    "dashboard_token"
                ),
            }
            save_clients_config(clients_cfg)
            load_all_accounts(preserve_running=True)
            self.send_json({
                "success": True,
                "message": f"Client {client_id} added.",
            })
            return
        if path == "/api/client/delete":
            client_id = str(
                body.get("client_id")
                or body.get("id")
                or ""
            ).strip()
            if not client_id:
                self.send_json({
                    "success": False,
                    "message": "Client ID required.",
                }, 400)
                return
            clients_cfg = load_clients_config()
            if client_id not in clients_cfg:
                self.send_json({
                    "success": False,
                    "message": "Client not found.",
                }, 404)
                return
            with ACCOUNTS_LOCK:
                running = [
                    bot for bot in BOT_ACCOUNTS.values()
                    if bot.base_account_id == client_id
                    and bot.bot_enabled
                ]
            if running:
                self.send_json({
                    "success": False,
                    "message": (
                        "Stop the client's running bots before deleting "
                        "the client."
                    ),
                }, 409)
                return
            del clients_cfg[client_id]
            save_clients_config(clients_cfg)
            load_all_accounts(preserve_running=True)
            self.send_json({
                "success": True,
                "message": f"Client {client_id} deleted.",
            })
            return
        self.send_json({
            "success": False,
            "message": "Unknown API endpoint.",
        }, 404)
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET,POST,OPTIONS",
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type",
        )
        self.end_headers()
WS_BOTS_LOCK = threading.RLock()
def websocket_on_open(ws):
    symbols = ["XAUTUSD", "BTCUSD"]
    payload = {
        "type": "subscribe",
        "payload": {
            "channels": [
                {
                    "name": "trades",
                    "symbols": symbols,
                }
            ],
        },
    }
    ws.send(json.dumps(payload))
    logging.info(
        "PUBLIC WEBSOCKET SUBSCRIBED | %s",
        ",".join(symbols),
    )
def extract_trade(message):
    try:
        data = json.loads(message)
    except Exception:
        return None, None
    candidates = [data]
    if isinstance(data.get("payload"), dict):
        candidates.append(data["payload"])
    if isinstance(data.get("data"), dict):
        candidates.append(data["data"])
    symbol = None
    price = None
    for item in candidates:
        if not isinstance(item, dict):
            continue
        symbol = (
            item.get("sy")
            or item.get("symbol")
            or item.get("product_symbol")
            or item.get("s")
        )
        price = (
            item.get("p")
            or item.get("price")
            or item.get("last_price")
            or item.get("close")
        )
        if symbol is not None and price is not None:
            break
    if symbol is None or price is None:
        return None, None
    symbol = str(symbol).upper()
    try:
        price = float(price)
    except Exception:
        return None, None
    return symbol, price
def websocket_on_message(ws, message):
    symbol, price = extract_trade(message)
    if symbol not in ("XAUTUSD", "BTCUSD"):
        return
    with ACCOUNTS_LOCK:
        bots = [
            bot for bot in BOT_ACCOUNTS.values()
            if bot.symbol == symbol
            and bot.bot_enabled
        ]
    for bot in bots:
        try:
            bot.evaluate(price)
        except Exception:
            logging.exception(
                "[%s] Evaluation error.",
                bot.symbol,
            )
def websocket_on_error(ws, error):
    logging.error(
        "PUBLIC WEBSOCKET ERROR | %s",
        error,
    )
def websocket_on_close(ws, close_status_code, close_msg):
    logging.warning(
        "PUBLIC WEBSOCKET CLOSED | code=%s | msg=%s",
        close_status_code,
        close_msg,
    )
def run_websocket_forever():
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=websocket_on_open,
                on_message=websocket_on_message,
                on_error=websocket_on_error,
                on_close=websocket_on_close,
            )
            logging.info(
                "CONNECTING PUBLIC WEBSOCKET | %s",
                WS_URL,
            )
            ws.run_forever(
                ping_interval=20,
                ping_timeout=10,
            )
        except Exception as e:
            logging.error(
                "WEBSOCKET LOOP ERROR | %s",
                e,
            )
        logging.info(
            "WEBSOCKET RECONNECT IN %s SECONDS",
            RECONNECT_SECONDS,
        )
        time.sleep(RECONNECT_SECONDS)
def startup_reconcile():
    with ACCOUNTS_LOCK:
        bots = list(BOT_ACCOUNTS.values())
    for bot in bots:
        try:
            if bot.prepare_product():
                bot.reconcile_exchange_state()
        except Exception:
            logging.exception(
                "[%s] Startup reconciliation error.",
                bot.symbol,
            )
def startup_start_all_bots():
    """Automatically start both strategy segments after every application restart.

    This enables the bots (XAUTUSD and BTCUSD) so the websocket can evaluate
    breakouts immediately after the normal trading start time. It does NOT
    place an order merely because the application started. Existing exchange
    positions are reconciled first by start_bot().
    """
    with ACCOUNTS_LOCK:
        bots = list(BOT_ACCOUNTS.values())

    for bot in bots:
        try:
            result = bot.start_bot()
            if result.get("success"):
                logging.info(
                    "[%s] AUTO-START SUCCESS | Bot ACTIVE | Existing position=%s",
                    bot.symbol,
                    bot.position or "FLAT",
                )
            else:
                logging.error(
                    "[%s] AUTO-START FAILED | %s",
                    bot.symbol,
                    result.get("message"),
                )
        except Exception as e:
            logging.exception(
                "[%s] AUTO-START ERROR | %s",
                bot.symbol,
                e,
            )

def main():
    if not acquire_single_process_lock():
        return

    logging.info("==================================================")
    logging.info(" DELTA PRO AUTOTRADER STARTING")
    logging.info(" XAUTUSD + BTCUSD")
    logging.info(" SESSION START  : 05:30 IST")
    logging.info(" TRADING START  : 05:45 IST")
    logging.info(" XAUT LEVERAGE  : 100 -> 10")
    logging.info(" BTC LEVERAGE   : 200 -> 10")
    logging.info(" MARGIN         : 10%%")
    logging.info(" AUTO-START     : XAUTUSD + BTCUSD")
    logging.info("==================================================")
    update_server_ip()
    load_all_accounts(preserve_running=True)
    startup_reconcile()
    startup_start_all_bots()
    ws_thread = threading.Thread(
        target=run_websocket_forever,
        name="delta-public-websocket",
        daemon=True,
    )
    ws_thread.start()
    server = ThreadingHTTPServer(
        ("0.0.0.0", DASHBOARD_PORT),
        DashboardHandler,
    )
    logging.info(
        "DASHBOARD SERVER STARTED | PORT=%s",
        DASHBOARD_PORT,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Shutdown requested.")
    finally:
        server.server_close()
if __name__ == "__main__":
    main()

