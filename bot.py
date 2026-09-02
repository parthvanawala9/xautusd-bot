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
# ============================================================

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip("/")
WS_URL = os.getenv("DELTA_PUBLIC_WS_URL", "wss://public-socket.india.delta.exchange")
SYMBOL = os.getenv("DELTA_SYMBOL", "XAUTUSD").strip()
LEVERAGE = Decimal(os.getenv("LEVERAGE", "50"))
BALANCE_FRACTION = Decimal(os.getenv("BALANCE_FRACTION", "0.10"))
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))

RECONNECT_SECONDS = 3
POSITION_CACHE_SECONDS = float(os.getenv("POSITION_CACHE_SECONDS", "1.0"))
BALANCE_CACHE_SECONDS = float(os.getenv("BALANCE_CACHE_SECONDS", "5.0"))

ACCOUNTS_FILE = os.getenv("ACCOUNTS_FILE", os.path.join(BASE_DIR, "accounts.json"))
STATE_DIR = os.getenv("STATE_DIR", os.path.join(BASE_DIR, "account_states"))
HISTORY_DIR = os.getenv("HISTORY_DIR", os.path.join(BASE_DIR, "account_history"))
ADMIN_PIN = os.getenv("ADMIN_PIN", "").strip()

PRIMARY_ACCOUNT_ID = os.getenv("ACCOUNT_ID", "primary").strip()
PRIMARY_ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "Primary Account").strip()
PRIMARY_API_KEY = os.getenv("DELTA_API_KEY", "").strip()
PRIMARY_API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

def now_ist():
    return datetime.now(IST)

def trading_day_start(dt=None):
    dt = dt or now_ist()
    boundary = dt.replace(hour=5, minute=30, second=0, microsecond=0)
    if dt < boundary:
        boundary -= timedelta(days=1)
    return boundary

def daily_squareoff_time(dt=None):
    dt = dt or now_ist()
    return dt.replace(hour=5, minute=40, second=0, microsecond=0)

def strategy_start(day_start):
    return day_start.replace(hour=5, minute=45, second=0, microsecond=0)

def weekend(dt=None):
    dt = dt or now_ist()
    if dt.weekday() == 5:
        return dt.hour >= 5
    if dt.weekday() == 6:
        return True
    return False

def saturday_squareoff(dt=None):
    dt = dt or now_ist()
    return dt.weekday() == 5 and dt.hour == 5

def safe_filename(value):
    result = ""
    for char in str(value):
        if char.isalnum() or char in ("-", "_"):
            result += char
        else:
            result += "_"
    return result or "account"

def account_state_file(account_id):
    return os.path.join(STATE_DIR, safe_filename(account_id) + ".json")

def account_history_file(account_id):
    return os.path.join(HISTORY_DIR, safe_filename(account_id) + ".json")

def atomic_write_json(filename, data):
    tmp = filename + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, filename)

accounts_file_lock = threading.RLock()

def load_client_accounts():
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return data
    except Exception as e:
        logging.warning(f"ACCOUNTS LOAD ERROR | {e}")
        return []

def save_client_accounts(accounts):
    with accounts_file_lock:
        atomic_write_json(ACCOUNTS_FILE, accounts)

class DeltaClient:
    def __init__(self, api_key, api_secret, account_name):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.account_name = (account_name or "Account").strip()
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "XAUTUSD-Multi-Account-Bot/1.0"
        })

    def sign(self, method, path, query="", body=""):
        timestamp = str(int(time.time()))
        message = method.upper() + timestamp + path + query + body
        signature = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "User-Agent": "XAUTUSD-Multi-Account-Bot/1.0"
        }

    def api(self, method, path, params=None, body=None, auth=False):
        params = params or {}
        body_text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        query = ("?" + urlencode(params, doseq=True)) if params else ""
        headers = self.sign(method, path, query, body_text) if auth else {}

        try:
            response = self.session.request(
                method.upper(),
                BASE_URL + path,
                params=params,
                data=body_text if body is not None else None,
                headers=headers,
                timeout=(5, 15)
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Delta connection error: {e}") from e

        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            try:
                error_body = response.json()
                raise RuntimeError(f"Delta HTTP {response.status_code}: {error_body}") from e
            except ValueError:
                text = (response.text or "").strip()
                raise RuntimeError(f"Delta HTTP {response.status_code}: {text[:300]}") from e

        try:
            data = response.json()
        except ValueError as e:
            raise RuntimeError("Delta returned invalid JSON.") from e

        if data.get("success") is False:
            raise RuntimeError(f"Delta error: {data}")

        return data

    def product(self):
        data = self.api("GET", f"/v2/products/{SYMBOL}")
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Invalid product response: {data}")
        return result

    def position(self, product_id):
        data = self.api("GET", "/v2/positions", params={"product_id": int(product_id)}, auth=True)
        result = data.get("result")
        if not isinstance(result, dict):
            return {"size": 0, "entry": None, "stop_loss": None, "unrealized_pnl": 0}
        return {
            "size": int(result.get("size", 0) or 0),
            "entry": result.get("entry_price"),
            "stop_loss": result.get("stop_loss"),
            "unrealized_pnl": result.get("unrealized_pnl", 0)
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
                value = wallet.get("available_balance")
                if value is None:
                    value = wallet.get("balance")
                if value is not None:
                    return Decimal(str(value))
        raise RuntimeError("USD/USDT balance not found.")

    def set_leverage(self, product_id):
        try:
            self.api("POST", f"/v2/products/{product_id}/orders/leverage", body={"leverage": str(LEVERAGE)}, auth=True)
            logging.info(f"{self.account_name} | LEVERAGE = {LEVERAGE}x")
        except Exception as e:
            logging.warning(f"{self.account_name} | LEVERAGE ERROR | {e}")

    def order_size(self, product_info, price):
        bal = self.balance()
        margin = bal * BALANCE_FRACTION
        notional = margin * LEVERAGE
        contract_value = Decimal(str(product_info.get("contract_value") or product_info.get("contract_value_usd") or "1"))
        if contract_value <= 0:
            contract_value = Decimal("1")
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
        logging.info(f"{self.account_name} | SIZE | Balance={bal} | Margin={margin} | Notional={notional} | Size={size}")
        return size

    def market_entry(self, product_id, side, size, sl):
        body = {
            "product_id": int(product_id),
            "product_symbol": SYMBOL,
            "size": int(abs(size)),
            "side": side,
            "order_type": "market_order",
            "bracket_stop_loss_price": str(sl),
            "bracket_stop_trigger_method": "last_traded_price",
            "client_order_id": (f"simple_{int(time.time() * 1000)}")[-32:]
        }
        logging.warning(f"{self.account_name} | ENTRY {side.upper()} | SIZE={size} | SL={sl}")
        return self.api("POST", "/v2/orders", body=body, auth=True)

    def close_position(self, product_id, size):
        if size == 0:
            return
        side = "sell" if size > 0 else "buy"
        body = {
            "product_id": int(product_id),
            "product_symbol": SYMBOL,
            "size": abs(int(size)),
            "side": side,
            "order_type": "market_order",
            "reduce_only": True,
            "client_order_id": (f"close_{int(time.time() * 1000)}")[-32:]
        }
        logging.warning(f"{self.account_name} | CLOSE POSITION | SIZE={size}")
        return self.api("POST", "/v2/orders", body=body, auth=True)

    def historical_high_low(self, start, end):
        try:
            data = self.api("GET", "/v2/history/candles", params={
                "resolution": "1m",
                "symbol": SYMBOL,
                "start": int(start.timestamp()),
                "end": int(end.timestamp())
            })
            candles = data.get("result", [])
            high, low = None, None
            for candle in candles:
                try:
                    h = Decimal(str(candle["high"]))
                    l = Decimal(str(candle["low"]))
                    if high is None or h > high: high = h
                    if low is None or l < low: low = l
                except Exception:
                    continue
            return high, low
        except Exception as e:
            logging.warning(f"{self.account_name} | HISTORY ERROR | {e}")
            return None, None

def load_trade_history(account_id):
    filename = account_history_file(account_id)
    if not os.path.exists(filename): return []
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list): return data
    except Exception as e:
        logging.warning(f"HISTORY LOAD ERROR | {account_id} | {e}")
    return []

def save_trade_history(account_id, history):
    atomic_write_json(account_history_file(account_id), history)

def contract_value_from_product(product_info):
    value = product_info.get("contract_value") or product_info.get("contract_value_usd") or "1"
    try:
        v = Decimal(str(value))
        return v if v > 0 else Decimal("1")
    except Exception:
        return Decimal("1")

def calculate_trade_pnl(direction, entry_price, exit_price, size, product_info):
    try:
        entry = Decimal(str(entry_price))
        exit_val = Decimal(str(exit_price))
        qty = Decimal(str(abs(size)))
        cv = contract_value_from_product(product_info)
        if direction == "LONG":
            return (exit_val - entry) * qty * cv
        return (entry - exit_val) * qty * cv
    except Exception as e:
        logging.warning(f"PNL ERROR | {e}")
        return Decimal("0")

class AccountBot:
    def __init__(self, account_id, account_name, account_type, api_key, api_secret, subscription=None):
        self.account_id = account_id
        self.account_name = account_name
        self.account_type = account_type
        self.subscription = subscription or {}
        self.client = DeltaClient(api_key, api_secret, account_name)

        self.product = self.client.product()
        self.product_id = int(self.product["id"])

        self.day = None
        self.high = None
        self.low = None
        self.sl = None
        self.trade_high = None
        self.trade_low = None
        self.last_position = 0
        self.last_price = None
        self.ready = False
        self.daily_squared_off = False

        self.lock = threading.RLock()
        self.cached_position = {"size": 0, "entry": None, "stop_loss": None, "unrealized_pnl": 0}
        self.position_cache_time = 0
        self.cached_balance = None
        self.balance_cache_time = 0

        self.websocket_connected = False
        self.last_ws_message_time = None
        self.last_api_ok_time = None
        self.api_error = None

        self.load_state()

        if self.stop_reason == "START REQUIRED" or self.stop_reason is None or self.stop_reason == "":
            self.bot_enabled = True
            self.stop_reason = None
        
        self.save()

    def load_state(self):
        filename = account_state_file(self.account_id)
        if not os.path.exists(filename): return
        try:
            with open(filename, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("day"): self.day = datetime.fromisoformat(state["day"])
            if state.get("high") is not None: self.high = Decimal(str(state["high"]))
            if state.get("low") is not None: self.low = Decimal(str(state["low"]))
            if state.get("sl") is not None: self.sl = Decimal(str(state["sl"]))
            if state.get("trade_high") is not None: self.trade_high = Decimal(str(state["trade_high"]))
            if state.get("trade_low") is not None: self.trade_low = Decimal(str(state["trade_low"]))
            if state.get("active_trade"): self.active_trade = state["active_trade"]
            self.bot_enabled = state.get("bot_enabled", True)
            self.stop_reason = state.get("stop_reason", None)
            self.daily_squared_off = state.get("daily_squared_off", False)
        except Exception as e:
            logging.warning(f"{self.account_name} | STATE LOAD ERROR | {e}")

    def save(self):
        data = {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "symbol": SYMBOL,
            "day": self.day.isoformat() if self.day else None,
            "high": str(self.high) if self.high is not None else None,
            "low": str(self.low) if self.low is not None else None,
            "sl": str(self.sl) if self.sl is not None else None,
            "trade_high": str(self.trade_high) if self.trade_high is not None else None,
            "trade_low": str(self.trade_low) if self.trade_low is not None else None,
            "active_trade": getattr(self, 'active_trade', None),
            "bot_enabled": self.bot_enabled,
            "stop_reason": self.stop_reason,
            "daily_squared_off": self.daily_squared_off
        }
        atomic_write_json(account_state_file(self.account_id), data)

    def refresh_position(self, force=False):
        current = time.time()
        if not force and (current - self.position_cache_time) < POSITION_CACHE_SECONDS:
            return self.cached_position
        try:
            pos = self.client.position(self.product_id)
            self.cached_position = pos
            self.position_cache_time = current
            self.last_api_ok_time = now_ist().isoformat()
            self.api_error = None
            return pos
        except Exception as e:
            self.api_error = str(e)
            logging.warning(f"{self.account_name} | POSITION ERROR | {e}")
            return self.cached_position

    def refresh_balance(self, force=False):
        current = time.time()
        if not force and self.cached_balance is not None and (current - self.balance_cache_time) < BALANCE_CACHE_SECONDS:
            return self.cached_balance
        try:
            val = self.client.balance()
            self.cached_balance = val
            self.balance_cache_time = current
            self.last_api_ok_time = now_ist().isoformat()
            self.api_error = None
            return val
        except Exception as e:
            self.api_error = str(e)
            logging.warning(f"{self.account_name} | BALANCE ERROR | {e}")
            return self.cached_balance

    def start_bot(self):
        with self.lock:
            logging.warning(f"{self.account_name} | START BOT REQUEST")
            if self.account_type != "primary" and not self.subscription_active():
                self.bot_enabled = False
                self.stop_reason = "SUBSCRIPTION EXPIRED"
                self.save()
                return {"success": False, "message": "Client subscription is not active."}

            pos = self.refresh_position(force=True)
            size = int(pos.get("size", 0))

            if size != 0:
                direction = "LONG" if size > 0 else "SHORT"
                recovered_entry = pos.get("entry")
                if recovered_entry is not None:
                    try: recovered_entry = Decimal(str(recovered_entry))
                    except Exception: recovered_entry = None

                if recovered_entry is None and getattr(self, 'active_trade', None):
                    se = self.active_trade.get("entry_price")
                    if se is not None:
                        try: recovered_entry = Decimal(str(se))
                        except Exception: pass

                if recovered_entry is None: recovered_entry = self.last_price

                recovered_sl = pos.get("stop_loss")
                if recovered_sl is not None:
                    try: recovered_sl = Decimal(str(recovered_sl))
                    except Exception: recovered_sl = None

                if recovered_sl is None: recovered_sl = self.sl
                if recovered_sl is None: recovered_sl = self.low if direction == "LONG" else self.high

                if getattr(self, 'active_trade', None) is None:
                    self.active_trade = {
                        "direction": direction,
                        "entry_price": float(recovered_entry) if recovered_entry else None,
                        "entry_time": now_ist().isoformat(),
                        "size": abs(size)
                    }
                else:
                    self.active_trade["direction"] = direction
                    self.active_trade["size"] = abs(size)

                if recovered_sl: self.sl = recovered_sl
                if direction == "LONG":
                    if self.trade_high is None: self.trade_high = self.high or recovered_entry
                    self.trade_low = None
                else:
                    if self.trade_low is None: self.trade_low = self.low or recovered_entry
                    self.trade_high = None

                self.last_position = size
                self.bot_enabled = True
                self.stop_reason = None
                self.save()
                return {"success": True, "bot_enabled": True, "position_recovered": True, "message": f"Bot started with existing {direction} position."}

            self.last_position = 0
            self.active_trade = None
            self.sl = None
            self.trade_high = None
            self.trade_low = None
            self.bot_enabled = True
            self.stop_reason = None
            self.save()
            return {"success": True, "bot_enabled": True, "message": "Bot started. Ready for trades."}

    def subscription_active(self):
        if self.account_type == "primary": return True
        sub = self.subscription or {}
        expiry = sub.get("expiry")
        if not expiry: return False
        try:
            dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=IST)
            return dt > now_ist()
        except Exception: return False

    def stop_bot(self):
        with self.lock:
            self.bot_enabled = False
            self.stop_reason = "MANUAL STOP"
            self.save()
            try:
                pos = self.refresh_position(force=True)
            except Exception as e:
                return {"success": False, "bot_enabled": False, "message": f"Bot stopped, error checking position: {e}"}

            size = int(pos.get("size", 0))
            if size == 0:
                self.last_position = 0
                self.sl = None
                self.active_trade = None
                self.trade_high = None
                self.trade_low = None
                self.save()
                return {"success": True, "bot_enabled": False, "message": "Bot stopped. No position."}

            try:
                self.client.close_position(self.product_id, size)
            except Exception as e:
                return {"success": False, "bot_enabled": False, "message": f"Stop failed to close position: {e}"}

            self.finish_active_trade(self.last_price or self.active_trade.get("entry_price"), "MANUAL_STOP")
            self.last_position = 0
            self.sl = None
            self.trade_high = None
            self.trade_low = None
            self.save()
            return {"success": True, "bot_enabled": False, "message": "Bot stopped and position closed."}

    def new_day(self, now):
        day = trading_day_start(now)
        if self.day == day: return
        logging.warning(f"{self.account_name} | NEW SESSION | {day}")
        self.day = day
        self.high = None
        self.low = None
        self.ready = False
        self.daily_squared_off = False

        live_size = self.last_position
        if live_size == 0:
            try:
                pos = self.refresh_position(force=True)
                live_size = int(pos.get("size", 0))
            except Exception: live_size = 0

        if live_size == 0:
            self.sl = None
            self.trade_high = None
            self.trade_low = None
            self.active_trade = None

        self.save()

    def prepare(self, now, price):
        start = strategy_start(self.day)
        if now < start: return False
        if self.ready: return True

        try: pos = self.refresh_position(force=True)
        except Exception: pos = {"size": self.last_position}
        self.last_position = int(pos.get("size", 0))

        if now > start + timedelta(seconds=5):
            high, low = self.client.historical_high_low(start, now)
            if high is not None and low is not None:
                self.high = high
                self.low = low
                self.ready = True
                self.save()
                logging.warning(f"{self.account_name} | RECOVERED RANGE | HIGH={high} | LOW={low}")
                return True

        self.high = price
        self.low = price
        self.ready = True
        self.save()
        logging.warning(f"{self.account_name} | INITIAL RANGE | HIGH={price} | LOW={price}")
        return True

    def enter(self, direction, price, sl):
        if not self.bot_enabled or self.last_position != 0 or sl is None: return False

        pos = self.refresh_position(force=True)
        if pos["size"] != 0:
            self.last_position = pos["size"]
            return False

        if direction == "LONG":
            if sl >= price: return False
            side = "buy"
        else:
            if sl <= price: return False
            side = "sell"

        try:
            size = self.client.order_size(self.product, price)
            self.client.market_entry(self.product_id, side, size, sl)
        except Exception as e:
            logging.exception(f"{self.account_name} | ENTRY ORDER ERROR | {e}")
            return False

        confirmed = False
        for _ in range(50):
            time.sleep(0.2)
            try:
                p = self.client.position(self.product_id)
                self.cached_position = p
                self.position_cache_time = time.time()
                if (direction == "LONG" and p["size"] > 0) or (direction == "SHORT" and p["size"] < 0):
                    self.last_position = p["size"]
                    confirmed = True
                    break
            except Exception: pass

        if not confirmed: return False

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
        logging.warning(f"{self.account_name} | TRADE LIVE | {direction} | ENTRY={price} | SL={sl}")
        return True

    def finish_active_trade(self, exit_price, reason):
        if not getattr(self, 'active_trade', None) or exit_price is None: return
        direction = self.active_trade.get("direction")
        entry_price = self.active_trade.get("entry_price")
        trade_size = self.active_trade.get("size", abs(int(self.last_position)))
        if entry_price is None:
            self.active_trade = None
            self.save()
            return

        pnl = calculate_trade_pnl(direction, entry_price, exit_price, trade_size, self.product)
        trade = {
            "id": f"trade_{int(time.time() * 1000)}",
            "account_id": self.account_id,
            "account": self.account_name,
            "symbol": SYMBOL,
            "date": now_ist().strftime("%Y-%m-%d"),
            "direction": direction,
            "entry_time": self.active_trade.get("entry_time"),
            "exit_time": now_ist().isoformat(),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "size": abs(int(trade_size)),
            "stop_loss": float(self.sl) if self.sl is not None else None,
            "pnl": float(pnl),
            "reason": reason
        }

        history = load_trade_history(self.account_id)
        history.append(trade)
        save_trade_history(self.account_id, history)

        self.active_trade = None
        self.save()

    def price_tick(self, price):
        with self.lock:
            self.last_price = price
            now = now_ist()
            self.last_ws_message_time = now.isoformat()

            # Saturday Weekend Squareoff
            if saturday_squareoff(now):
                try:
                    pos = self.refresh_position(force=True)
                    if pos["size"] != 0:
                        self.client.close_position(self.product_id, pos["size"])
                        self.finish_active_trade(price, "SATURDAY_SQUAREOFF")
                        self.last_position = 0
                        self.sl = None
                        self.trade_high = None
                        self.trade_low = None
                        self.save()
                except Exception as e:
                    logging.exception(f"{self.account_name} | SATURDAY SQUAREOFF ERROR | {e}")
                return

            if weekend(now): return
            self.new_day(now)

            # ============================================================
            # DAILY 05:40 AM SQUAREOFF LOGIC (Carry-Forward Position Exit)
            # ============================================================
            sq_time = daily_squareoff_time(now)
            if now >= sq_time and now < strategy_start(self.day) and not self.daily_squared_off:
                try:
                    pos = self.refresh_position(force=True)
                    if pos["size"] != 0:
                        logging.warning(f"{self.account_name} | DAILY 05:40 AM SQUAREOFF | Closing open position: {pos['size']}")
                        self.client.close_position(self.product_id, pos["size"])
                        self.finish_active_trade(price, "DAILY_0540_SQUAREOFF")
                        self.last_position = 0
                        self.sl = None
                        self.trade_high = None
                        self.trade_low = None
                    self.daily_squared_off = True
                    self.save()
                except Exception as e:
                    logging.exception(f"{self.account_name} | DAILY SQUAREOFF ERROR | {e}")

            if now < strategy_start(self.day): return
            if not self.prepare(now, price): return

            pos = self.refresh_position()
            size = int(pos.get("size", 0))

            # Stop-Loss Checking Logic
            if size == 0 and self.last_position != 0:
                old = self.last_position
                sl_hit = self.sl is not None and ((old > 0 and price <= self.sl) or (old < 0 and price >= self.sl))
                if self.bot_enabled and sl_hit:
                    if old > 0:
                        peak = self.trade_high or self.high
                        self.finish_active_trade(price, "STOP_LOSS")
                        self.last_position = 0
                        self.sl = None
                        if peak is not None: self.enter("SHORT", price, peak)
                    else:
                        trough = self.trade_low or self.low
                        self.finish_active_trade(price, "STOP_LOSS")
                        self.last_position = 0
                        self.sl = None
                        if trough is not None: self.enter("LONG", price, trough)
                    return

                self.finish_active_trade(price, "EXTERNAL_CLOSE")
                self.last_position = 0
                self.sl = None
                self.trade_high = None
                self.trade_low = None
                self.save()
                return

            if size > 0:
                self.last_position = size
                if self.trade_high is None or price > self.trade_high:
                    self.trade_high = price
                    self.high = max(self.high, price) if self.high is not None else price
                    self.save()
                return

            if size < 0:
                self.last_position = size
                if self.trade_low is None or price < self.trade_low:
                    self.trade_low = price
                    self.low = min(self.low, price) if self.low is not None else price
                    self.save()
                return

            # Flat Position -> Looking for Breakout Entry
            self.last_position = 0
            if not self.bot_enabled: return

            if self.high is not None and price > self.high:
                old_h, sl = self.high, self.low
                if self.enter("LONG", price, sl):
                    self.high = price
                    self.save()
                return

            if self.low is not None and price < self.low:
                old_l, sl = self.low, self.high
                if self.enter("SHORT", price, sl):
                    self.low = price
                    self.save()
                return

# ============================================================
# ACCOUNT MANAGER & HTTP SERVER
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
        account_name=data.get("name", "Client Account"),
        account_type="client",
        api_key=data["api_key"],
        api_secret=data["api_secret"],
        subscription={
            "start": data.get("subscription_start"),
            "expiry": data.get("subscription_expiry"),
            "fee": data.get("subscription_fee", 0)
        }
    )

def load_primary_account():
    try:
        primary = create_primary_account()
        primary.client.set_leverage(primary.product_id)
        with ACCOUNTS_LOCK:
            BOT_ACCOUNTS[primary.account_id] = primary
        logging.warning(f"PRIMARY ACCOUNT LOADED | {primary.account_name}")
        return True
    except Exception as e:
        logging.exception(f"PRIMARY ACCOUNT LOAD ERROR | {e}")
        return False

def load_client_accounts_into_manager():
    clients = load_client_accounts()
    loaded = 0
    for data in clients:
        try:
            account_id = str(data.get("account_id", "")).strip()
            if not account_id or account_id == PRIMARY_ACCOUNT_ID: continue
            if not data.get("api_key") or not data.get("api_secret"): continue

            client_bot = create_client_account(data)
            client_bot.client.set_leverage(client_bot.product_id)
            with ACCOUNTS_LOCK:
                BOT_ACCOUNTS[account_id] = client_bot
            loaded += 1
        except Exception as e:
            logging.exception(f"CLIENT LOAD ERROR | {e}")
    return loaded

def load_all_accounts():
    with ACCOUNTS_LOCK: BOT_ACCOUNTS.clear()
    primary_ok = load_primary_account()
    client_count = load_client_accounts_into_manager()
    return primary_ok

def get_bot(account_id):
    with ACCOUNTS_LOCK: return BOT_ACCOUNTS.get(str(account_id))

def decimal_json(value):
    if value is None: return None
    try: return float(value)
    except Exception: return value

def history_statistics(history):
    today = now_ist().strftime("%Y-%m-%d")
    today_trades = [t for t in history if t.get("date") == today]
    all_count = len(history)
    all_win = sum(1 for t in history if float(t.get("pnl", 0)) > 0)
    all_pnl = sum(float(t.get("pnl", 0)) for t in history)
    
    t_count = len(today_trades)
    t_win = sum(1 for t in today_trades if float(t.get("pnl", 0)) > 0)
    t_pnl = sum(float(t.get("pnl", 0)) for t in today_trades)

    return {
        "today": {
            "total_trades": t_count,
            "win_rate": round(t_win / t_count * 100, 1) if t_count > 0 else 0,
            "pnl": round(t_pnl, 2)
        },
        "all_time": {
            "total_trades": all_count,
            "win_rate": round(all_win / all_count * 100, 1) if all_count > 0 else 0,
            "pnl": round(all_pnl, 2)
        }
    }

def subscription_data(bot):
    if bot.account_type == "primary":
        return {"active": True, "expired": False, "start": None, "expiry": None, "fee": 0}
    sub = bot.subscription or {}
    active = bot.subscription_active()
    return {
        "active": active,
        "expired": bool(sub.get("expiry")) and not active,
        "start": sub.get("start"),
        "expiry": sub.get("expiry"),
        "fee": sub.get("fee", 0)
    }

def account_dashboard(bot):
    with bot.lock:
        live_pos = bot.refresh_position()
        live_bal = bot.refresh_balance()
        size = int(live_pos.get("size", 0))
        direction = "LONG" if size > 0 else ("SHORT" if size < 0 else "FLAT")
        history = load_trade_history(bot.account_id)

        return {
            "account_id": bot.account_id,
            "account_name": bot.account_name,
            "account_type": bot.account_type,
            "online": bot.websocket_connected or bot.last_price is not None,
            "bot_running": bot.bot_enabled,
            "bot_enabled": bot.bot_enabled,
            "bot_status": "RUNNING" if bot.bot_enabled else "STOPPED",
            "stop_reason": bot.stop_reason,
            "symbol": SYMBOL,
            "leverage": float(LEVERAGE),
            "current_price": decimal_json(bot.last_price),
            "high": decimal_json(bot.high),
            "low": decimal_json(bot.low),
            "stop_loss": decimal_json(bot.sl),
            "balance": decimal_json(live_bal),
            "position": {
                "direction": direction,
                "size": abs(size),
                "entry_price": decimal_json(live_pos.get("entry")),
                "stop_loss": decimal_json(bot.sl),
                "unrealized_pnl": decimal_json(live_pos.get("unrealized_pnl", 0))
            },
            "statistics": history_statistics(history),
            "trade_history": list(reversed(history)),
            "subscription": subscription_data(bot)
        }

def dashboard_data():
    with ACCOUNTS_LOCK: bots = list(BOT_ACCOUNTS.values())
    accounts = [account_dashboard(b) for b in bots]
    return {
        "success": True,
        "online": any(b.websocket_connected or b.last_price is not None for b in bots),
        "bot_running": any(b.bot_enabled for b in bots),
        "accounts": accounts
    }

class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            self.send_json({"success": True, "online": True, "symbol": SYMBOL})
            return
        if path == "/api/dashboard":
            self.send_json(dashboard_data())
            return
        if path == "/": self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        bot = get_bot(body.get("account_id"))

        if path == "/api/bot/start" and bot:
            self.send_json(bot.start_bot())
            return
        if path == "/api/bot/stop" and bot:
            self.send_json(bot.stop_bot())
            return
        self.send_json({"success": False, "message": "Unknown endpoint"}, status=404)

    def send_json(self, data, status=200):
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args): return

def start_dashboard():
    def server_thread():
        server = ThreadingHTTPServer(("127.0.0.1", DASHBOARD_PORT), DashboardHandler)
        logging.warning(f"DASHBOARD STARTED | PORT = {DASHBOARD_PORT}")
        server.serve_forever()
    t = threading.Thread(target=server_thread, daemon=True, name="dashboard-server")
    t.start()
    time.sleep(0.2)

def run_websocket():
    while True:
        try:
            def on_open(ws):
                with ACCOUNTS_LOCK:
                    for b in BOT_ACCOUNTS.values(): b.websocket_connected = True
                ws.send(json.dumps({"type": "subscribe", "payload": {"channels": [{"name": "trades", "symbols": [SYMBOL]}]}}))

            def on_message(ws, message):
                data = json.loads(message)
                if data.get("type") != "trades": return
                p_val = data.get("p") or (data.get("data", {}).get("p") if isinstance(data.get("data"), dict) else None)
                if p_val is None: return
                price = Decimal(str(p_val))
                with ACCOUNTS_LOCK: bots = list(BOT_ACCOUNTS.values())
                for b in bots: b.price_tick(price)

            def on_error(ws, error):
                with ACCOUNTS_LOCK:
                    for b in BOT_ACCOUNTS.values(): b.websocket_connected = False

            def on_close(ws, code, msg):
                with ACCOUNTS_LOCK:
                    for b in BOT_ACCOUNTS.values(): b.websocket_connected = False

            ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            logging.exception(f"WS CRASH | {e}")
        time.sleep(RECONNECT_SECONDS)

if __name__ == "__main__":
    logging.warning("============================================")
    logging.warning("XAUTUSD MULTI ACCOUNT BOT - AUTO TRADING ACTIVE")
    logging.warning("============================================")
    try:
        start_dashboard()
        load_all_accounts()
        run_websocket()
    except KeyboardInterrupt:
        logging.warning("BOT STOPPED")
