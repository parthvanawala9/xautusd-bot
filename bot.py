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
# DELTA PRO AUTOTRADER - DUAL STRATEGY (XAUTUSD ONLY)
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
RECONNECT_SECONDS = 3
POSITION_CACHE_SECONDS = float(
    os.getenv("POSITION_CACHE_SECONDS", "0.5")
)

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
    force=True
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
            logging.warning("==================================================")
            logging.warning(f" RAILWAY OUTBOUND IP --> {ip}")
            logging.warning(" WHITELIST THIS IP IN DELTA EXCHANGE API SETTINGS")
            logging.warning("==================================================")
    except Exception as e:
        logging.warning(f"IP FETCH ERROR | {e}")


def now_ist():
    return datetime.now(IST)


def is_weekend(dt=None):
    dt = dt or now_ist()
    wday = dt.weekday()
    t = dt.time()
    if wday == 5:
        return t >= dtime(5, 30)
    if wday == 6:
        return True
    if wday == 0 and t < dtime(5, 30):
        return True
    return False


def get_current_session_start(dt=None):
    dt = dt or now_ist()
    t = dt.time()
    m530 = dt.replace(hour=5, minute=30, second=0, microsecond=0)
    if t >= dtime(5, 30):
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
    return os.path.join(STATE_DIR, safe_filename(unique_id) + ".json")


def account_history_file(unique_id):
    return os.path.join(HISTORY_DIR, safe_filename(unique_id) + ".json")


def atomic_write_json(filename, data):
    tmp = filename + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, filename)


def load_clients_config():
    if not os.path.exists(CLIENTS_FILE):
        return {}
    try:
        with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_clients_config(cfg):
    atomic_write_json(CLIENTS_FILE, cfg)


# =====================================================================
# DELTA CLIENT
# =====================================================================

class DeltaClient:

    def __init__(self, api_key, api_secret, account_name, symbol):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.account_name = (account_name or "Account").strip()
        self.symbol = symbol.strip().upper()
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "MultiBot/94.0"
        })

    def sign(self, method, path, query="", body=""):
        timestamp = str(int(time.time()))
        message = method.upper() + timestamp + path + query + body
        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "User-Agent": "MultiBot/94.0"
        }

    def api(self, method, path, params=None, body=None, auth=False):
        params = params or {}
        body_text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        query = "?" + urlencode(params, doseq=True) if params else ""
        headers = self.sign(method, path, query, body_text) if auth else {}

        response = self.session.request(
            method.upper(),
            BASE_URL + path,
            params=params,
            data=body_text if body is not None else None,
            headers=headers,
            timeout=(3, 8)
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

    def get_session_high_low(self, product_id, session_start_dt):
        try:
            start_ts = int(session_start_dt.timestamp())
            end_ts = int(now_ist().timestamp())
            if end_ts <= start_ts:
                return None, None

            params = {
                "resolution": "1m",
                "symbol": self.symbol,
                "start": start_ts,
                "end": end_ts
            }
            data = self.api("GET", "/v2/history/candles", params=params)
            candles = data.get("result", [])
            if not isinstance(candles, list) or not candles:
                return None, None

            highest, lowest = None, None
            for c in candles:
                try:
                    if isinstance(c, dict):
                        ts_raw = c.get("time") or c.get("timestamp") or c.get("start")
                        h_raw = c.get("high")
                        l_raw = c.get("low")
                    elif isinstance(c, list) and len(c) >= 4:
                        ts_raw = c[0]
                        h_raw = c[2]
                        l_raw = c[3]
                    else:
                        continue

                    if ts_raw is not None:
                        ts = float(ts_raw)
                        if ts > 100000000000:
                            ts /= 1000.0
                        if ts < start_ts or ts > end_ts:
                            continue

                    h = Decimal(str(h_raw))
                    l = Decimal(str(l_raw))

                    if h > 0 and (highest is None or h > highest):
                        highest = h
                    if l > 0 and (lowest is None or l < lowest):
                        lowest = l
                except Exception:
                    continue

            if highest is not None and lowest is not None:
                return highest, lowest
        except Exception as e:
            logging.warning(f"[{self.symbol}] Session high/low fetch error: {e}")
        return None, None

    def get_15m_candles(self, limit=5):
        try:
            end_ts = int(now_ist().timestamp())
            start_ts = end_ts - (limit * 15 * 60)
            params = {
                "resolution": "15m",
                "symbol": self.symbol,
                "start": start_ts,
                "end": end_ts
            }
            data = self.api("GET", "/v2/history/candles", params=params)
            candles = data.get("result", [])
            formatted = []
            for c in candles:
                try:
                    if isinstance(c, dict):
                        formatted.append({
                            "time": float(c.get("time") or c.get("timestamp") or 0),
                            "high": float(c.get("high")),
                            "low": float(c.get("low")),
                            "close": float(c.get("close"))
                        })
                    elif isinstance(c, list) and len(c) >= 5:
                        formatted.append({
                            "time": float(c[0]),
                            "high": float(c[2]),
                            "low": float(c[3]),
                            "close": float(c[4])
                        })
                except Exception:
                    continue
            return formatted
        except Exception:
            return []

    def position(self, product_id):
        try:
            data = self.api("GET", "/v2/positions", params={"product_id": int(product_id)}, auth=True)
        except Exception:
            data = {}

        result = data.get("result", [])
        pos_item = {}
        if isinstance(result, list):
            for p in result:
                if isinstance(p, dict) and int(p.get("product_id", 0)) == int(product_id):
                    pos_item = p
                    break
            if not pos_item and result:
                pos_item = result[0] if isinstance(result[0], dict) else {}
        elif isinstance(result, dict):
            pos_item = result

        if not pos_item:
            return {
                "size": 0, "entry_price": None, "stop_loss": None,
                "liquidation_price": None, "bankruptcy_price": None,
                "margin": None, "mark_price": None, "unrealized_pnl": 0, "leverage": None
            }

        raw_entry = (
            pos_item.get("entry_price") or pos_item.get("entry") or
            pos_item.get("avg_price") or pos_item.get("average_price") or
            pos_item.get("price")
        )
        entry_val = float(raw_entry) if raw_entry is not None and float(raw_entry) > 0 else None
        lev_val = (
            pos_item.get("leverage") or 
            pos_item.get("user_leverage") or 
            pos_item.get("effective_leverage")
        )

        return {
            "size": int(pos_item.get("size", 0) or 0),
            "entry_price": entry_val,
            "stop_loss": float(pos_item.get("stop_loss")) if pos_item.get("stop_loss") else None,
            "liquidation_price": float(pos_item.get("liquidation_price")) if pos_item.get("liquidation_price") else None,
            "bankruptcy_price": float(pos_item.get("bankruptcy_price")) if pos_item.get("bankruptcy_price") else None,
            "margin": float(pos_item.get("margin")) if pos_item.get("margin") else None,
            "mark_price": float(pos_item.get("mark_price")) if pos_item.get("mark_price") else None,
            "unrealized_pnl": float(pos_item.get("unrealized_pnl", 0) or 0),
            "leverage": int(lev_val) if lev_val else None
        }

    def balance(self):
        data = self.api("GET", "/v2/wallet/balances", auth=True)
        result = data.get("result", [])
        if isinstance(result, dict):
            result = [result]
        for wallet in result:
            if not isinstance(wallet, dict):
                continue
            asset = str(wallet.get("asset_symbol", "")).upper()
            if asset in ("USD", "USDT"):
                value = wallet.get("available_balance") or wallet.get("balance")
                if value is not None:
                    return Decimal(str(value))
        raise RuntimeError("USD/USDT balance not found.")

    def set_leverage(self, product_id, leverage_val):
        self.api(
            "POST",
            f"/v2/products/{product_id}/orders/leverage",
            body={"leverage": str(leverage_val)},
            auth=True
        )

    def order_size(self, product_info, price, leverage, balance_fraction):
        bal = self.balance()
        margin = bal * balance_fraction
        notional = margin * leverage
        contract_value = Decimal(str(product_info.get("contract_value") or product_info.get("contract_value_usd") or "0.001"))
        if contract_value <= 0:
            contract_value = Decimal("0.001")

        raw = notional / price / contract_value
        increment = Decimal(str(product_info.get("lot_size") or product_info.get("order_size_increment") or "1"))
        minimum = Decimal(str(product_info.get("min_order_size") or product_info.get("minimum_order_size") or increment))
        if increment <= 0:
            increment = Decimal("1")

        size_decimal = (raw / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        if size_decimal < minimum:
            size_decimal = minimum
        size = int(size_decimal)
        if size <= 0:
            raise RuntimeError("Order size calculated as zero.")
        return size

    def cancel_all_orders(self, product_id):
        try:
            self.api("DELETE", "/v2/orders/all", body={"product_id": int(product_id)}, auth=True)
        except Exception:
            pass

    def market_entry_pure(self, product_id, side, size):
        body = {
            "product_id": int(product_id),
            "product_symbol": self.symbol,
            "size": int(abs(size)),
            "side": side,
            "order_type": "market_order",
            "client_order_id": f"entry_{int(time.time() * 1000)}"[-32:]
        }
        return self.api("POST", "/v2/orders", body=body, auth=True)

    def place_bracket_sl_order(self, product_id, stop_price):
        body = {
            "product_id": int(product_id),
            "product_symbol": self.symbol,
            "stop_loss_order": {
                "order_type": "market_order",
                "stop_price": str(stop_price)
            },
            "bracket_stop_trigger_method": "last_traded_price"
        }
        return self.api("POST", "/v2/orders/bracket", body=body, auth=True)

    def close_position(self, product_id, size):
        if size == 0:
            return
        self.cancel_all_orders(product_id)
        side = "sell" if size > 0 else "buy"
        body = {
            "product_id": int(product_id),
            "product_symbol": self.symbol,
            "size": abs(int(size)),
            "side": side,
            "order_type": "market_order",
            "reduce_only": True,
            "client_order_id": f"close_{int(time.time() * 1000)}"[-32:]
        }
        return self.api("POST", "/v2/orders", body=body, auth=True)

    def last_traded_price(self):
        try:
            data = self.api("GET", f"/v2/tickers/{self.symbol}")
            res = data.get("result")
            if isinstance(res, dict):
                p = res.get("close") or res.get("spot_price") or res.get("ltp")
                if p is not None:
                    return Decimal(str(p))
        except Exception:
            pass
        return None


# =====================================================================
# TRADE HISTORY & STATS HELPERS
# =====================================================================

def load_trade_history(unique_id):
    filename = account_history_file(unique_id)
    if not os.path.exists(filename):
        return []
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_trade_history(unique_id, history):
    atomic_write_json(account_history_file(unique_id), history)


def calculate_trade_pnl(direction, entry_price, exit_price, size, product_info):
    try:
        entry = Decimal(str(entry_price))
        exit_val = Decimal(str(exit_price))
        qty = Decimal(str(abs(size)))
        cv = Decimal(str(product_info.get("contract_value") or product_info.get("contract_value_usd") or "0.001"))
        if cv <= 0:
            cv = Decimal("0.001")
        if direction == "LONG":
            return (exit_val - entry) * qty * cv
        return (entry - exit_val) * qty * cv
    except Exception:
        return Decimal("0")


def calculate_statistics(history):
    def compute_stats(trades):
        total = len(trades)
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) < 0]
        pnl = sum(Decimal(str(t.get("pnl", 0))) for t in trades)
        win_rate = (len(wins) / total * 100) if total > 0 else 0.0
        return {
            "total_trades": total,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": float(win_rate),
            "pnl": float(pnl)
        }
    today_str = now_ist().strftime("%Y-%m-%d")
    today_trades = [t for t in history if str(t.get("date", "")).startswith(today_str)]
    return {
        "today": compute_stats(today_trades),
        "all_time": compute_stats(history)
    }


# =====================================================================
# STRATEGY 1 BOT: DAY HIGH/LOW BREAKOUT
# =====================================================================

class AccountBot:
    def __init__(self, account_id, account_name, account_type, api_key, api_secret, symbol="XAUTUSD", subscription=None):
        self.base_account_id = account_id
        self.symbol = symbol.strip().upper()
        self.strategy_key = "s1"
        self.unique_id = f"{account_id}_{self.symbol}_{self.strategy_key}"
        self.account_name = f"{account_name} [S1: Breakout]"
        self.account_type = account_type
        self.subscription = subscription or {}
        self.client = DeltaClient(api_key, api_secret, account_name, self.symbol)

        self.product = None
        self.product_id = 0
        self.session_start = None
        self.day_high = None
        self.day_low = None
        self.last_position = 0
        self.last_price = None
        self.prev_price = None
        self.ready = False
        self.trading_armed = False
        self.bot_enabled = False
        self.stop_reason = None
        self.active_trade = None
        self.manual_squareoff_flag = False

        self.leverage = Decimal("100")
        self.balance_fraction = Decimal("0.10")
        self.lock = threading.RLock()
        self.cached_position = {"size": 0, "entry_price": None, "stop_loss": None, "unrealized_pnl": 0}
        self.position_cache_time = 0

        self.load_state()
        self.save()

    def is_expired(self):
        if self.account_type == "primary":
            return False
        expiry_str = self.subscription.get("expiry")
        if not expiry_str:
            return False
        try:
            return now_ist().date() > datetime.strptime(expiry_str, "%Y-%m-%d").date()
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
                self.session_start = datetime.fromisoformat(state["session_start"])
            if state.get("day_high") is not None:
                self.day_high = Decimal(str(state["day_high"]))
            if state.get("day_low") is not None:
                self.day_low = Decimal(str(state["day_low"]))
            if state.get("active_trade"):
                self.active_trade = state["active_trade"]
            if state.get("leverage") is not None:
                self.leverage = Decimal(str(state["leverage"]))
            if state.get("balance_fraction") is not None:
                self.balance_fraction = Decimal(str(state["balance_fraction"]))
            self.bot_enabled = state.get("bot_enabled", False)
            self.stop_reason = state.get("stop_reason", None)
            self.ready = state.get("ready", False)
            self.trading_armed = state.get("trading_armed", False)
        except Exception:
            pass

    def save(self):
        data = {
            "account_id": self.unique_id,
            "account_name": self.account_name,
            "symbol": self.symbol,
            "session_start": self.session_start.isoformat() if self.session_start else None,
            "day_high": str(self.day_high) if self.day_high is not None else None,
            "day_low": str(self.day_low) if self.day_low is not None else None,
            "active_trade": getattr(self, "active_trade", None),
            "leverage": int(self.leverage),
            "balance_fraction": float(self.balance_fraction),
            "bot_enabled": self.bot_enabled,
            "stop_reason": self.stop_reason,
            "ready": self.ready,
            "trading_armed": self.trading_armed
        }
        atomic_write_json(account_state_file(self.unique_id), data)

    def refresh_position(self, force=False):
        if not self.bot_enabled:
            return {"size": 0, "entry_price": None, "stop_loss": None, "unrealized_pnl": 0}
        return {"size": 0, "entry_price": None, "stop_loss": None, "unrealized_pnl": 0}

    def update_settings(self, new_lev, new_frac):
        with self.lock:
            try:
                self.leverage = Decimal(str(new_lev))
                self.balance_fraction = Decimal(str(new_frac))
                if self.product_id:
                    self.client.set_leverage(self.product_id, self.leverage)
                self.save()
                return {"success": True, "message": f"Saved S1 Settings! Leverage: {int(self.leverage)}x"}
            except Exception as e:
                return {"success": False, "message": str(e)}

    def start_bot(self):
        with self.lock:
            if self.is_expired():
                return {"success": False, "message": "Subscription expired."}
            self.manual_squareoff_flag = False
            self.bot_enabled = True
            self.save()
            return {"success": True, "bot_enabled": True, "message": "Strategy 1 Started."}

    def stop_bot(self):
        with self.lock:
            self.bot_enabled = False
            self.stop_reason = "MANUAL STOP"
            self.manual_squareoff_flag = True
            self.active_trade = None
            self.save()
            return {"success": True, "bot_enabled": False, "message": "Strategy 1 Stopped."}

    def check_session_change(self, now):
        current_sess = get_current_session_start(now)
        if self.session_start != current_sess:
            if self.product_id:
                self.client.cancel_all_orders(self.product_id)
            self.session_start = current_sess
            self.day_high = None
            self.day_low = None
            self.prev_price = None
            self.last_position = 0
            self.active_trade = None
            self.manual_squareoff_flag = False
            self.ready = False
            self.trading_armed = False

            if self.product_id:
                h, l = self.client.get_session_high_low(self.product_id, self.session_start)
                if h is not None and l is not None:
                    self.day_high = h
                    self.day_low = l
                    self.ready = True
            self.save()

    def prepare(self, now):
        if not self.product_id:
            try:
                self.product = self.client.product()
                self.product_id = int(self.product["id"])
            except Exception:
                return False
        if self.session_start is None:
            self.session_start = get_current_session_start(now)
        if self.day_high is None or self.day_low is None:
            h, l = self.client.get_session_high_low(self.product_id, self.session_start)
            if h is not None and l is not None:
                self.day_high = h
                self.day_low = l
                self.ready = True
                self.save()
        return True

    def estimate_liquidation_price(self, entry_price, leverage, direction):
        entry = Decimal(str(entry_price))
        lev = Decimal(str(leverage))
        if entry <= 0 or lev <= 0:
            return None
        m_raw = self.product.get("maintenance_margin", 0) if self.product else 0
        t_raw = self.product.get("taker_commission_rate", 0) if self.product else 0
        try:
            maintenance = Decimal(str(m_raw)) / Decimal("100")
        except Exception:
            maintenance = Decimal("0")
        try:
            taker_fee = Decimal(str(t_raw))
        except Exception:
            taker_fee = Decimal("0")
        safety = Decimal("0.0010")
        effective_mm = maintenance + taker_fee + safety

        if direction == "LONG":
            return entry * (Decimal("1") - (Decimal("1") / lev) + effective_mm)
        return entry * (Decimal("1") + (Decimal("1") / lev) - effective_mm)

    def enter(self, direction, price, sl_level):
        if self.is_expired() or self.manual_squareoff_flag or not self.bot_enabled:
            return False
        try:
            current_pos = self.client.position(self.product_id)
            if current_pos.get("size", 0) != 0:
                return False

            ladder = [100, 90, 80, 70, 60, 50, 40, 30, 20, 10]
            order_done = False
            chosen_lev = self.leverage
            size = 0

            for lev in ladder:
                lev_decimal = Decimal(str(lev))
                candidate_liq = self.estimate_liquidation_price(price, lev_decimal, direction)
                if candidate_liq is None:
                    continue
                if direction == "LONG" and candidate_liq >= Decimal(str(sl_level)):
                    continue
                if direction == "SHORT" and candidate_liq <= Decimal(str(sl_level)):
                    continue

                try:
                    self.client.set_leverage(self.product_id, lev_decimal)
                    size = self.client.order_size(self.product, price, lev_decimal, self.balance_fraction)
                    side = "buy" if direction == "LONG" else "sell"
                    self.client.market_entry_pure(self.product_id, side, size)
                    chosen_lev = lev_decimal
                    order_done = True
                    break
                except Exception:
                    continue

            if not order_done:
                return False

            time.sleep(0.5)
            self.client.place_bracket_sl_order(self.product_id, sl_level)

            self.active_trade = {
                "direction": direction,
                "entry_price": float(price),
                "entry_time": now_ist().isoformat(),
                "size": size,
                "leverage": int(chosen_lev),
                "sl": float(sl_level)
            }
            self.leverage = chosen_lev
            self.last_position = size if direction == "LONG" else -size
            self.save()
            return True
        except Exception as e:
            logging.error(f"[S1] Entry error: {e}")
            return False

    def evaluate(self, price=None):
        with self.lock:
            if not self.bot_enabled or self.is_expired():
                return
            now = now_ist()
            price = price or self.client.last_traded_price()
            if price is None:
                return
            self.last_price = price

            if self.prev_price is None:
                self.prev_price = price
                return

            old_price = self.prev_price
            new_price = price
            self.prev_price = price

            if is_weekend(now):
                return

            self.check_session_change(now)
            if not self.prepare(now) or not self.ready:
                return

            if now.time() < TRADING_START_TIME:
                self.trading_armed = False
                return
            elif not self.trading_armed:
                self.trading_armed = True
                return

            pos = self.client.position(self.product_id)
            size = int(pos.get("size", 0))

            if self.last_position != 0 and size == 0 and not self.manual_squareoff_flag:
                old_dir = "LONG" if self.last_position > 0 else "SHORT"
                self.finish_trade(old_dir, new_price)
                self.last_position = 0
                self.active_trade = None
                self.save()

            if size == 0 and not self.manual_squareoff_flag and self.active_trade is None:
                if old_price <= self.day_high and new_price > self.day_high:
                    self.enter("LONG", new_price, self.day_low)
                elif old_price >= self.day_low and new_price < self.day_low:
                    self.enter("SHORT", new_price, self.day_high)

            if new_price > (self.day_high or 0):
                self.day_high = new_price
                self.save()
            if new_price < (self.day_low or 999999):
                self.day_low = new_price
                self.save()
            self.last_position = size

    def finish_trade(self, direction, exit_price):
        if not self.active_trade:
            return
        entry = self.active_trade.get("entry_price")
        size = self.active_trade.get("size")
        pnl = calculate_trade_pnl(direction, entry, exit_price, size, self.product or {"contract_value": "0.001"})
        trade = {
            "id": f"s1_{int(time.time()*1000)}",
            "account_id": self.unique_id,
            "account": self.account_name,
            "symbol": self.symbol,
            "date": now_ist().strftime("%Y-%m-%d %H:%M"),
            "direction": direction,
            "entry_price": float(entry),
            "exit_price": float(exit_price),
            "size": size,
            "pnl": float(pnl),
            "reason": "SL_HIT"
        }
        history = load_trade_history(self.unique_id)
        history.append(trade)
        save_trade_history(self.unique_id, history)


# =====================================================================
# STRATEGY 2 BOT: 15-MIN CANDLE TRAILING SAR (INDEPENDENT)
# =====================================================================

class CandleSARBot:
    def __init__(self, account_id, account_name, account_type, api_key, api_secret, symbol="XAUTUSD", subscription=None):
        self.base_account_id = account_id
        self.symbol = symbol.strip().upper()
        self.strategy_key = "s2"
        self.unique_id = f"{account_id}_{self.symbol}_{self.strategy_key}"
        self.account_name = f"{account_name} [S2: 15m SAR]"
        self.account_type = account_type
        self.subscription = subscription or {}
        self.client = DeltaClient(api_key, api_secret, account_name, self.symbol)

        self.product = None
        self.product_id = 0
        self.position = None  # 'LONG', 'SHORT', None
        self.stop_loss = 0.0
        self.entry_price = None
        self.size = 0
        self.last_checked_candle_time = 0
        self.last_price = None
        self.bot_enabled = False
        self.leverage = Decimal("100")
        self.balance_fraction = Decimal("0.10")
        self.lock = threading.RLock()
        self.cached_position = {"size": 0, "entry_price": None, "stop_loss": None, "unrealized_pnl": 0}

        self.load_state()
        self.save()

    def is_expired(self):
        if self.account_type == "primary":
            return False
        expiry_str = self.subscription.get("expiry")
        if not expiry_str:
            return False
        try:
            return now_ist().date() > datetime.strptime(expiry_str, "%Y-%m-%d").date()
        except Exception:
            return False

    def load_state(self):
        filename = account_state_file(self.unique_id)
        if not os.path.exists(filename):
            return
        try:
            with open(filename, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.position = state.get("position")
            self.stop_loss = float(state.get("stop_loss", 0.0))
            self.entry_price = state.get("entry_price")
            self.size = state.get("size", 0)
            self.bot_enabled = state.get("bot_enabled", False)
            if state.get("leverage"):
                self.leverage = Decimal(str(state["leverage"]))
            if state.get("balance_fraction"):
                self.balance_fraction = Decimal(str(state["balance_fraction"]))
        except Exception:
            pass

    def save(self):
        data = {
            "account_id": self.unique_id,
            "account_name": self.account_name,
            "symbol": self.symbol,
            "position": self.position,
            "stop_loss": self.stop_loss,
            "entry_price": self.entry_price,
            "size": self.size,
            "bot_enabled": self.bot_enabled,
            "leverage": int(self.leverage),
            "balance_fraction": float(self.balance_fraction)
        }
        atomic_write_json(account_state_file(self.unique_id), data)

    def refresh_position(self):
        if not self.bot_enabled:
            return {"size": 0, "entry_price": None, "stop_loss": None, "unrealized_pnl": 0}

        if not self.product_id:
            try:
                self.product = self.client.product()
                self.product_id = int(self.product["id"])
            except Exception:
                return {"size": 0, "entry_price": None, "stop_loss": None, "unrealized_pnl": 0}

        try:
            # सीधे एक्सचेंज से लाइव पोजीशन फेच करें ताकि मैन्युअल या बोट द्वारा ली गई पोजीशन डैशबोर्ड में दिखे
            exchange_pos = self.client.position(self.product_id)
            ex_size = exchange_pos.get("size", 0)

            if ex_size != 0:
                direction = "LONG" if ex_size > 0 else "SHORT"
                self.position = direction
                self.size = abs(ex_size)
                if exchange_pos.get("entry_price"):
                    self.entry_price = exchange_pos.get("entry_price")
                
                # यदि स्टॉप लॉस सेट नहीं है, तो कैंडल के हिसाब से डिफॉल्ट सेट करें
                if not self.stop_loss or self.stop_loss == 0.0:
                    candles = self.client.get_15m_candles(limit=2)
                    if len(candles) >= 2:
                        self.stop_loss = candles[-2]["low"] if direction == "LONG" else candles[-2]["high"]

                unrealized_pnl = 0
                if self.entry_price and self.last_price:
                    unrealized_pnl = float(
                        calculate_trade_pnl(
                            self.position,
                            self.entry_price,
                            self.last_price,
                            self.size,
                            self.product or {"contract_value": "0.001"}
                        )
                    )

                return {
                    "size": ex_size,
                    "entry_price": self.entry_price,
                    "stop_loss": self.stop_loss,
                    "leverage": exchange_pos.get("leverage") or int(self.leverage),
                    "liquidation_price": exchange_pos.get("liquidation_price"),
                    "unrealized_pnl": unrealized_pnl
                }
            else:
                self.position = None
                self.entry_price = None
                self.size = 0
                return {"size": 0, "entry_price": None, "stop_loss": None, "unrealized_pnl": 0}
        except Exception:
            return {"size": 0, "entry_price": None, "stop_loss": None, "unrealized_pnl": 0}

    def update_settings(self, new_lev, new_frac):
        with self.lock:
            try:
                self.leverage = Decimal(str(new_lev))
                self.balance_fraction = Decimal(str(new_frac))
                if self.product_id:
                    self.client.set_leverage(self.product_id, self.leverage)
                self.save()
                return {"success": True, "message": f"Saved S2 Settings! Leverage: {int(self.leverage)}x"}
            except Exception as e:
                return {"success": False, "message": str(e)}

    def start_bot(self):
        with self.lock:
            if self.is_expired():
                return {"success": False, "message": "Subscription expired."}
            self.bot_enabled = True
            self.save()
            return {"success": True, "bot_enabled": True, "message": "Strategy 2 (15m SAR) Started."}

    def stop_bot(self):
        with self.lock:
            self.bot_enabled = False
            self.save()
            self.finish_trade("MANUAL", self.last_price or 0)
            self.position = None
            self.entry_price = None
            self.size = 0
            self.save()
            return {"success": True, "bot_enabled": False, "message": "Strategy 2 Stopped."}

    def estimate_liquidation_price(self, entry_price, leverage, direction):
        entry = Decimal(str(entry_price))
        lev = Decimal(str(leverage))
        if entry <= 0 or lev <= 0:
            return None
        m_raw = self.product.get("maintenance_margin", 0) if self.product else 0
        t_raw = self.product.get("taker_commission_rate", 0) if self.product else 0
        try:
            maintenance = Decimal(str(m_raw)) / Decimal("100")
        except Exception:
            maintenance = Decimal("0")
        try:
            taker_fee = Decimal(str(t_raw))
        except Exception:
            taker_fee = Decimal("0")
        safety = Decimal("0.0010")
        effective_mm = maintenance + taker_fee + safety

        if direction == "LONG":
            return entry * (Decimal("1") - (Decimal("1") / lev) + effective_mm)
        return entry * (Decimal("1") + (Decimal("1") / lev) - effective_mm)

    def evaluate(self, price=None):
        with self.lock:
            if not self.bot_enabled or self.is_expired():
                return
            
            now = now_ist()
            if is_weekend(now):
                if self.position and self.size > 0:
                    logging.info("[S2] Weekend detected. Closing active position...")
                    try:
                        close_sz = self.size if self.position == "LONG" else -self.size
                        self.client.close_position(self.product_id, close_sz)
                        self.finish_trade("WEEKEND_CLOSE", self.last_price or 0)
                    except Exception as e:
                        logging.error(f"[S2] Weekend close error: {e}")
                    self.position = None
                    self.entry_price = None
                    self.size = 0
                    self.save()
                return

            if not self.product_id:
                try:
                    self.product = self.client.product()
                    self.product_id = int(self.product["id"])
                except Exception:
                    return

            price = price or self.client.last_traded_price()
            if price is None:
                return
            self.last_price = float(price)

            candles = self.client.get_15m_candles(limit=3)
            if len(candles) < 2:
                return

            prev_candle = candles[-2]
            curr_candle = candles[-1]
            curr_time = curr_candle["time"]

            if self.position is None or self.size == 0:
                current_pos = self.client.position(self.product_id)
                if current_pos.get("size", 0) != 0:
                    self.position = "LONG" if current_pos.get("size", 0) > 0 else "SHORT"
                    self.size = abs(current_pos.get("size", 0))
                    self.entry_price = current_pos.get("entry_price")
                    self.stop_loss = prev_candle["low"] if self.position == "LONG" else prev_candle["high"]
                    self.save()
                    return

                if self.last_price > prev_candle["high"]:
                    self.execute_entry("LONG", prev_candle["low"])
                elif self.last_price < prev_candle["low"]:
                    self.execute_entry("SHORT", prev_candle["high"])
                return

            if self.position == "LONG":
                if self.last_price <= self.stop_loss:
                    logging.info("[S2] LONG SL Hit. Reversing to SHORT...")
                    self.finish_trade("SL_HIT", self.last_price)
                    self.client.close_position(self.product_id, self.size)
                    self.execute_entry("SHORT", prev_candle["high"])
                else:
                    if curr_time != self.last_checked_candle_time:
                        self.stop_loss = prev_candle["low"]
                        self.last_checked_candle_time = curr_time
                        self.save()

            elif self.position == "SHORT":
                if self.last_price >= self.stop_loss:
                    logging.info("[S2] SHORT SL Hit. Reversing to LONG...")
                    self.finish_trade("SL_HIT", self.last_price)
                    self.client.close_position(self.product_id, -self.size)
                    self.execute_entry("LONG", prev_candle["low"])
                else:
                    if curr_time != self.last_checked_candle_time:
                        self.stop_loss = prev_candle["high"]
                        self.last_checked_candle_time = curr_time
                        self.save()

    def execute_entry(self, direction, initial_sl):
        try:
            current_pos = self.client.position(self.product_id)
            if current_pos.get("size", 0) != 0:
                return

            ladder = [100, 90, 80, 70, 60, 50, 40, 30, 20, 10]
            order_done = False
            chosen_lev = self.leverage
            size = 0

            for lev in ladder:
                lev_decimal = Decimal(str(lev))
                candidate_liq = self.estimate_liquidation_price(self.last_price, lev_decimal, direction)
                if candidate_liq is None:
                    continue
                if direction == "LONG" and candidate_liq >= Decimal(str(initial_sl)):
                    continue
                if direction == "SHORT" and candidate_liq <= Decimal(str(initial_sl)):
                    continue

                try:
                    self.client.set_leverage(self.product_id, lev_decimal)
                    size = self.client.order_size(self.product, Decimal(str(self.last_price)), lev_decimal, self.balance_fraction)
                    side = "buy" if direction == "LONG" else "sell"
                    self.client.market_entry_pure(self.product_id, side, size)
                    chosen_lev = lev_decimal
                    order_done = True
                    break
                except Exception:
                    continue

            if not order_done:
                return

            self.position = direction
            self.entry_price = float(self.last_price)
            self.size = size
            self.leverage = chosen_lev
            self.stop_loss = float(initial_sl)
            self.save()
            logging.info(f"[S2] Entered {direction} | Lev: {int(self.leverage)}x | Entry: {self.entry_price} | SL: {initial_sl}")
        except Exception as e:
            logging.error(f"[S2] Entry execution error: {e}")

    def finish_trade(self, reason, exit_price):
        if not self.position or not self.entry_price:
            return
        pnl = calculate_trade_pnl(self.position, self.entry_price, exit_price, self.size, self.product or {"contract_value": "0.001"})
        trade = {
            "id": f"s2_{int(time.time()*1000)}",
            "account_id": self.unique_id,
            "account": self.account_name,
            "symbol": self.symbol,
            "date": now_ist().strftime("%Y-%m-%d %H:%M"),
            "direction": self.position,
            "entry_price": float(self.entry_price),
            "exit_price": float(exit_price),
            "size": self.size,
            "pnl": float(pnl),
            "reason": reason
        }
        history = load_trade_history(self.unique_id)
        history.append(trade)
        save_trade_history(self.unique_id, history)


# =====================================================================
# ACCOUNTS MANAGER & WEB DASHBOARD
# =====================================================================

BOT_ACCOUNTS = {}
ACCOUNTS_LOCK = threading.RLock()
SYMBOL = "XAUTUSD"


def load_all_accounts():
    with ACCOUNTS_LOCK:
        for b_id, b_obj in list(BOT_ACCOUNTS.items()):
            try:
                b_obj.stop_bot()
            except Exception:
                pass
        BOT_ACCOUNTS.clear()

        if PRIMARY_API_KEY and PRIMARY_API_SECRET:
            s1 = AccountBot(PRIMARY_ACCOUNT_ID, PRIMARY_ACCOUNT_NAME, "primary", PRIMARY_API_KEY, PRIMARY_API_SECRET, SYMBOL)
            s2 = CandleSARBot(PRIMARY_ACCOUNT_ID, PRIMARY_ACCOUNT_NAME, "primary", PRIMARY_API_KEY, PRIMARY_API_SECRET, SYMBOL)
            BOT_ACCOUNTS[s1.unique_id] = s1
            BOT_ACCOUNTS[s2.unique_id] = s2

        clients_cfg = load_clients_config()
        for cid, cdata in clients_cfg.items():
            s1 = AccountBot(cid, cdata.get("name", "Client"), "client", cdata.get("api_key"), cdata.get("api_secret"), SYMBOL, {
                "start": cdata.get("subscription_start"),
                "expiry": cdata.get("subscription_expiry")
            })
            s2 = CandleSARBot(cid, cdata.get("name", "Client"), "client", cdata.get("api_key"), cdata.get("api_secret"), SYMBOL, {
                "start": cdata.get("subscription_start"),
                "expiry": cdata.get("subscription_expiry")
            })
            BOT_ACCOUNTS[s1.unique_id] = s1
            BOT_ACCOUNTS[s2.unique_id] = s2


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/health":
            self.send_json({"success": True, "online": True})
            return

        if path == "/api/dashboard":
            client_token = query.get("token", [None])[0]
            server_ip = CACHED_SERVER_IP

            with ACCOUNTS_LOCK:
                bots = list(BOT_ACCOUNTS.values())

            if client_token:
                clients_cfg = load_clients_config()
                target_cid = None
                for cid, cdata in clients_cfg.items():
                    if cdata.get("token") == client_token:
                        target_cid = cid
                        break
                if not target_cid:
                    self.send_json({"success": False, "message": "Unauthorized client token"}, status=403)
                    return
                bots = [b for b in bots if b.base_account_id == target_cid]

            accounts_data = []
            clients_cfg = load_clients_config()

            for b in bots:
                try:
                    price = float(b.client.last_traded_price() or 0)
                    b.last_price = price
                    pos = b.refresh_position() if b.bot_enabled else {"size": 0, "entry_price": None, "stop_loss": None, "unrealized_pnl": 0}
                    balance = float(b.client.balance())
                except Exception:
                    pos = {"size": 0, "entry_price": None, "stop_loss": None, "unrealized_pnl": 0}
                    balance = 0
                    price = 0

                direction = "FLAT"
                if b.bot_enabled and pos.get("size", 0) != 0:
                    if pos.get("size", 0) > 0:
                        direction = "LONG"
                    elif pos.get("size", 0) < 0:
                        direction = "SHORT"

                entry_p = pos.get("entry_price") if b.bot_enabled else None
                active_sl = pos.get("stop_loss") if b.bot_enabled else None
                actual_lev = pos.get("leverage") if (b.bot_enabled and pos.get("leverage")) else int(b.leverage)
                unrealized_pnl = pos.get("unrealized_pnl", 0) if b.bot_enabled else 0

                history = load_trade_history(b.unique_id)
                stats = calculate_statistics(history)

                token = clients_cfg.get(b.base_account_id, {}).get("token", "") if b.account_type == "client" else ""
                sub_info = getattr(b, "subscription", {})

                accounts_data.append({
                    "account_id": b.unique_id,
                    "account_name": b.account_name,
                    "account_type": b.account_type,
                    "symbol": b.symbol,
                    "token": token,
                    "server_ip": server_ip,
                    "balance": balance,
                    "current_price": price,
                    "bot_enabled": b.bot_enabled and not b.is_expired(),
                    "is_expired": b.is_expired(),
                    "leverage": actual_lev,
                    "balance_fraction": float(b.balance_fraction),
                    "position": {
                        "size": pos.get("size", 0) if b.bot_enabled else 0,
                        "direction": direction,
                        "entry_price": entry_p,
                        "stop_loss": active_sl,
                        "liquidation_price": pos.get("liquidation_price") if b.bot_enabled else None,
                        "unrealized_pnl": unrealized_pnl
                    },
                    "statistics": stats,
                    "trade_history": history,
                    "subscription": sub_info
                })

            self.send_json({
                "success": True,
                "server_online": True,
                "server_ip": server_ip,
                "accounts": accounts_data
            })
            return

        if path == "/" or path == "":
            self.send_html_dashboard()
            return
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        clients_cfg = load_clients_config()

        if parsed == "/api/bot/start":
            bot = BOT_ACCOUNTS.get(body.get("account_id"))
            if bot:
                self.send_json(bot.start_bot())
                return
            self.send_json({"success": False, "message": "Not found"}, 404)
            return

        if parsed == "/api/bot/stop":
            bot = BOT_ACCOUNTS.get(body.get("account_id"))
            if bot:
                self.send_json(bot.stop_bot())
                return
            self.send_json({"success": False, "message": "Not found"}, 404)
            return

        if parsed == "/api/bot/settings":
            bot = BOT_ACCOUNTS.get(body.get("account_id"))
            if bot:
                res = bot.update_settings(body.get("leverage"), body.get("balance_fraction"))
                self.send_json(res)
                return
            self.send_json({"success": False, "message": "Not found"}, 404)
            return

        if parsed == "/api/client/add":
            name, key, secret, expiry = body.get("name"), body.get("api_key"), body.get("api_secret"), body.get("subscription_expiry")
            if not name or not key or not secret:
                self.send_json({"success": False, "message": "Missing fields"}, 400)
                return
            cid = f"client_{int(time.time())}"
            token = hashlib.sha256(f"{cid}_{time.time()}".encode()).hexdigest()[:16]
            clients_cfg[cid] = {
                "name": name,
                "api_key": key,
                "api_secret": secret,
                "token": token,
                "subscription_start": now_ist().strftime("%Y-%m-%d"),
                "subscription_expiry": expiry or "2099-12-31",
                "subscription_fee": 0
            }
            save_clients_config(clients_cfg)
            load_all_accounts()
            self.send_json({"success": True, "message": "Client added successfully!"})
            return

        if parsed == "/api/client/delete":
            acc_id = body.get("account_id")
            base_cid = acc_id.split("_")[0] + "_" + acc_id.split("_")[1] if acc_id and "_" in acc_id else acc_id
            if base_cid in clients_cfg:
                del clients_cfg[base_cid]
                save_clients_config(clients_cfg)
                with ACCOUNTS_LOCK:
                    keys_to_del = [k for k in BOT_ACCOUNTS if k.startswith(base_cid)]
                    for k in keys_to_del:
                        BOT_ACCOUNTS[k].stop_bot()
                        del BOT_ACCOUNTS[k]
                self.send_json({"success": True, "message": "Client removed"})
                return
            self.send_json({"success": False, "message": "Client not found"}, 404)
            return

        self.send_json({"success": False, "message": "Not found"}, 404)

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
            <h1 class="text-2xl font-bold text-amber-400">Delta Pro AutoTrader</h1>
            <p id="server-ip" class="text-xs text-slate-400 mt-1">IP: Loading...</p>
        </header>

        <div id="add-client-section" class="bg-slate-800 rounded-2xl p-4 shadow-xl border border-slate-700 space-y-3">
            <h3 class="font-bold text-sm text-amber-400 uppercase">Add New Client Account</h3>
            <input type="text" id="c-name" placeholder="Client Name" class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2 text-xs text-slate-200">
            <input type="text" id="c-key" placeholder="Delta API Key" class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2 text-xs text-slate-200">
            <input type="password" id="c-secret" placeholder="Delta API Secret" class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2 text-xs text-slate-200">
            <div>
                <label class="block text-[10px] text-slate-400 mb-1">Subscription Expiry Date</label>
                <input type="date" id="c-expiry" class="w-full bg-slate-900 border border-slate-700 rounded-lg p-2 text-xs text-slate-200">
            </div>
            <button onclick="addClient()" class="w-full bg-amber-600 hover:bg-amber-500 text-xs font-semibold py-2 rounded-lg transition text-white">Add Client & Generate Link</button>
        </div>

        <div id="accounts-container" class="space-y-6">
            <div class="text-center text-slate-400">Loading Dashboard...</div>
        </div>
    </div>

<script>
let isEditingSettings = false;

async function fetchDashboard() {
    if (isEditingSettings) return;
    try {
        let urlParams = new URLSearchParams(window.location.search);
        let token = urlParams.get("token");
        let fetchUrl = token ? `/api/dashboard?token=${token}&_t=${Date.now()}` : `/api/dashboard?_t=${Date.now()}`;
        let res = await fetch(fetchUrl);
        let data = await res.json();
        if (data.success) {
            document.getElementById("server-ip").innerText = "Server IP: " + data.server_ip;
            if (token) {
                let addSec = document.getElementById("add-client-section");
                if (addSec) addSec.style.display = "none";
            }
            let container = document.getElementById("accounts-container");
            container.innerHTML = "";

            data.accounts.forEach(acc => {
                let pos = acc.position;
                let stats = acc.statistics;
                let clientLink = acc.token ? `${window.location.origin}/?token=${acc.token}` : "";
                let expiryText = acc.subscription && acc.subscription.expiry ? acc.subscription.expiry : "N/A";
                let finalEntry = (pos.entry_price !== null && pos.entry_price !== undefined && pos.entry_price > 0) ? pos.entry_price : "N/A";
                let finalSl = (pos.stop_loss !== null && pos.stop_loss !== undefined && pos.stop_loss > 0) ? pos.stop_loss : "N/A";
                let tradeLevDisplay = acc.leverage + "x";

                let html = `
                <div class="bg-slate-800 rounded-2xl p-5 shadow-xl border border-slate-700 space-y-4">
                    <div class="flex justify-between items-center border-b border-slate-700 pb-3">
                        <div>
                            <h2 class="font-bold text-base text-amber-300">${acc.account_name}</h2>
                            <p class="text-xs text-slate-400">Balance: $${acc.balance.toFixed(2)} | Price: ${acc.current_price || "N/A"}</p>
                            ${acc.account_type == "client" ? `<p class="text-[10px] text-amber-400 mt-0.5">Expiry: ${expiryText}${acc.is_expired ? "(EXPIRED)" : ""}</p>` : ""}
                        </div>
                        <span class="px-3 py-1 rounded-full text-xs font-semibold ${acc.bot_enabled ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-rose-500/20 text-rose-400 border border-rose-500/30'}">
                            ${acc.bot_enabled ? 'RUNNING' : 'STOPPED'}
                        </span>
                    </div>

                    ${clientLink ? `<div class="bg-slate-900/60 p-2.5 rounded-xl border border-slate-700 text-xs space-y-1"><span class="text-slate-400 text-[10px] block">Client Unique Link:</span><input type="text" readonly value="${clientLink}" class="w-full bg-slate-800 border border-slate-700 rounded p-1 text-[11px] text-amber-300 select-all"></div>` : ""}

                    <div class="bg-slate-900/50 p-3 rounded-xl border border-slate-700/50 space-y-3">
                        <div class="text-xs font-semibold text-amber-400 uppercase">Risk Settings</div>
                        <div class="grid grid-cols-2 gap-2">
                            <div>
                                <label class="block text-[10px] text-slate-400 mb-1">Max/Default Leverage</label>
                                <select id="lev-${acc.account_id}" onfocus="isEditingSettings=true" onblur="isEditingSettings=false" class="w-full bg-slate-800 border border-slate-700 rounded-lg p-1.5 text-xs text-slate-200">
                                    <option value="100" ${acc.leverage==100?'selected':''}>100x</option>
                                    <option value="50" ${acc.leverage==50?'selected':''}>50x</option>
                                    <option value="25" ${acc.leverage==25?'selected':''}>25x</option>
                                    <option value="10" ${acc.leverage==10?'selected':''}>10x</option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-[10px] text-slate-400 mb-1">Margin Fraction</label>
                                <select id="frac-${acc.account_id}" onfocus="isEditingSettings=true" onblur="isEditingSettings=false" class="w-full bg-slate-800 border border-slate-700 rounded-lg p-1.5 text-xs text-slate-200">
                                    <option value="0.10" ${acc.balance_fraction==0.1?'selected':''}>10%</option>
                                    <option value="0.25" ${acc.balance_fraction==0.25?'selected':''}>25%</option>
                                    <option value="0.50" ${acc.balance_fraction==0.5?'selected':''}>50%</option>
                                </select>
                            </div>
                        </div>
                        <button onclick="updateSettings('${acc.account_id}')" class="w-full bg-slate-700 hover:bg-slate-600 text-xs font-semibold py-1.5 rounded-lg">Save Settings</button>
                    </div>

                    <div class="space-y-2 bg-slate-900/60 p-3 rounded-xl border border-slate-700/60 text-sm">
                        <div class="flex justify-between"><span class="text-slate-400">Direction:</span><span class="font-bold ${pos.direction=='LONG'?'text-emerald-400':pos.direction=='SHORT'?'text-rose-400':'text-slate-300'}">${pos.direction}</span></div>
                        <div class="flex justify-between"><span class="text-slate-400">Size:</span><span class="font-semibold">${pos.size}</span></div>
                        <div class="flex justify-between"><span class="text-slate-400">Entry Price:</span><span class="font-semibold text-amber-300">${finalEntry}</span></div>
                        <div class="flex justify-between"><span class="text-slate-400">Trade Leverage:</span><span class="font-semibold text-amber-400">${tradeLevDisplay}</span></div>
                        <div class="flex justify-between"><span class="text-slate-400">Stop Loss:</span><span class="font-semibold text-rose-400">${finalSl}</span></div>
                        <div class="flex justify-between"><span class="text-slate-400">Unrealized P&L:</span><span class="font-semibold ${pos.unrealized_pnl>=0?'text-emerald-400':'text-rose-400'}">$${pos.unrealized_pnl.toFixed(2)}</span></div>
                    </div>

                    <div class="space-y-2">
                        <div class="text-xs font-bold text-slate-400 uppercase">Trading Performance</div>
                        <div class="grid grid-cols-2 gap-2 text-xs">
                            <div class="bg-slate-900/50 p-2.5 rounded-xl border border-slate-700/60 space-y-1">
                                <div class="font-semibold text-amber-400">TODAY</div>
                                <div class="text-slate-400">Trades: ${stats.today.total_trades}</div>
                                <div class="text-slate-400">Win Rate: ${stats.today.win_rate.toFixed(1)}%</div>
                                <div class="font-bold ${stats.today.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}">P&L: $${stats.today.pnl.toFixed(2)}</div>
                            </div>
                            <div class="bg-slate-900/50 p-2.5 rounded-xl border border-slate-700/60 space-y-1">
                                <div class="font-semibold text-amber-400">ALL TIME</div>
                                <div class="text-slate-400">Trades: ${stats.all_time.total_trades}</div>
                                <div class="text-slate-400">Win Rate: ${stats.all_time.win_rate.toFixed(1)}%</div>
                                <div class="font-bold ${stats.all_time.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}">P&L: $${stats.all_time.pnl.toFixed(2)}</div>
                            </div>
                        </div>
                    </div>

                    <div class="flex gap-2">
                        <button onclick="toggleBot('${acc.account_id}', ${acc.bot_enabled})" class="flex-1 py-2.5 rounded-xl font-semibold text-sm transition ${acc.bot_enabled ? 'bg-rose-600 hover:bg-rose-500 text-white' : 'bg-emerald-600 hover:bg-emerald-500 text-white'}">
                            ${acc.bot_enabled ? 'STOP BOT' : 'START BOT'}
                        </button>
                        ${acc.account_type == "client" && !token ? `<button onclick="deleteClient('${acc.account_id}')" class="bg-slate-700 hover:bg-rose-700 px-3 py-2.5 rounded-xl text-xs font-semibold transition">Remove</button>` : ""}
                    </div>

                    <div class="space-y-2 pt-2 border-t border-slate-700">
                        <div class="text-xs font-bold text-slate-400 uppercase">Trade History (${acc.trade_history.length})</div>
                        <div class="max-h-40 overflow-y-auto space-y-1.5 text-xs">
                            ${acc.trade_history.length === 0 ? '<div class="text-slate-500 text-center py-2">No closed trades yet.</div>' : ''}
                            ${acc.trade_history.slice().reverse().map(t => `
                                <div class="bg-slate-900/40 p-2 rounded border border-slate-800 flex justify-between items-center">
                                    <div>
                                        <span class="font-bold ${t.direction=='LONG'?'text-emerald-400':'text-rose-400'}">${t.direction}</span>
                                        <span class="text-slate-400 ml-1">(${t.date})</span>
                                        <div class="text-[10px] text-slate-500">Entry: ${t.entry_price} → Exit: ${t.exit_price}</div>
                                    </div>
                                    <div class="text-right font-bold ${t.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}">$${t.pnl.toFixed(2)}</div>
                                </div>
                            `).join("")}
                        </div>
                    </div>
                </div>`;
                container.innerHTML += html;
            });
        }
    } catch(e) { console.error(e); }
}

async function addClient() {
    let name = document.getElementById("c-name").value;
    let key = document.getElementById("c-key").value;
    let secret = document.getElementById("c-secret").value;
    let expiry = document.getElementById("c-expiry").value;
    if(!name || !key || !secret || !expiry) { alert("Please fill all fields!"); return; }
    let res = await fetch("/api/client/add", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name, api_key: key, api_secret: secret, subscription_expiry: expiry})
    });
    let data = await res.json();
    alert(data.message);
    document.getElementById("c-name").value = "";
    document.getElementById("c-key").value = "";
    document.getElementById("c-secret").value = "";
    document.getElementById("c-expiry").value = "";
    fetchDashboard();
}

async function deleteClient(accId) {
    if(!confirm("Are you sure you want to remove this client?")) return;
    await fetch("/api/client/delete", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({account_id: accId})
    });
    fetchDashboard();
}

async function toggleBot(accId, state) {
    let endpoint = state ? "/api/bot/stop" : "/api/bot/start";
    await fetch(endpoint, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({account_id: accId})
    });
    fetchDashboard();
}

async function updateSettings(accId) {
    let lev = document.getElementById("lev-" + accId).value;
    let frac = document.getElementById("frac-" + accId).value;
    isEditingSettings = true;
    let res = await fetch("/api/bot/settings", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({account_id: accId, leverage: lev, balance_fraction: frac})
    });
    let data = await res.json();
    alert(data.message);
    isEditingSettings = false;
    fetchDashboard();
}

setInterval(fetchDashboard, 3000);
fetchDashboard();
</script>
</body>
</html>
"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def send_json(self, data, status=200):
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args):
        pass


def start_dashboard():
    port = int(os.getenv("PORT", DASHBOARD_PORT))
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    logging.warning(f"WEB SERVER STARTED ON PORT {port}")
    server.serve_forever()


def background_timer_loop():
    while True:
        time.sleep(1)
        try:
            with ACCOUNTS_LOCK:
                bots = list(BOT_ACCOUNTS.values())
            for b in bots:
                try:
                    price = b.client.last_traded_price()
                    if price:
                        b.evaluate(price)
                except Exception:
                    b.evaluate()
        except Exception:
            pass


def run_websocket():
    while True:
        try:
            def on_open(ws):
                ws.send(json.dumps({
                    "type": "subscribe",
                    "payload": {"channels": [{"name": "trades", "symbols": [SYMBOL]}]}
                }))

            def on_message(ws, message):
                try:
                    parsed = json.loads(message)
                    if parsed.get("type") != "trades":
                        return
                    payload = parsed.get("data", parsed)
                    p_val = payload.get("p") or payload.get("price") or parsed.get("p")
                    if p_val is None:
                        return
                    price = Decimal(str(p_val))
                    with ACCOUNTS_LOCK:
                        bots = list(BOT_ACCOUNTS.values())
                    for b in bots:
                        b.evaluate(price)
                except Exception:
                    pass

            ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message)
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception:
            pass
        time.sleep(RECONNECT_SECONDS)


if __name__ == "__main__":
    logging.warning("==================================================")
    logging.warning("DELTA DUAL STRATEGY AUTOTRADER STARTING...")
    logging.warning("==================================================")
    update_server_ip()
    load_all_accounts()

    threading.Thread(target=background_timer_loop, daemon=True).start()
    threading.Thread(target=run_websocket, daemon=True).start()
    start_dashboard()
