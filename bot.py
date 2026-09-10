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
# XAUTUSD MULTI ACCOUNT BOT - RAILWAY READY VERSION (CLEANED)
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
                timeout=(3, 8)
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

    def cancel_all_orders(self, product_id):
        try:
            self.api("DELETE", "/v2/orders/all", body={"product_id": int(product_id)}, auth=True)
            logging.info(f"{self.account_name} | ALL OPEN ORDERS CANCELLED")
        except Exception as e:
            logging.warning(f"{self.account_name} | CANCEL ALL ORDERS ERROR | {e}")

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
        logging.warning(f"{self.account_name} | ENTRY {side.upper()} WITH BRACKET SL | SIZE={size} | SL={sl}")
        return self.api("POST", "/v2/orders", body=body, auth=True)

    def close_position(self, product_id, size):
        if size == 0:
            return
        self.cancel_all_orders(product_id)
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

    def last_traded_price(self):
        try:
            data = self.api("GET", f"/v2/tickers/{SYMBOL}")
            res = data.get("result")
            if isinstance(res, dict):
                p = res.get("close") or res.get("spot_price") or res.get("ltp")
                if p is not None:
                    return Decimal(str(p))
        except Exception:
            pass
        return None

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
        self.bot_enabled = True
        self.stop_reason = None

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
            pos = self.refresh_position(force=True)
            size = int(pos.get("size", 0))

            if size != 0:
                direction = "LONG" if size > 0 else "SHORT"
                recovered_entry = pos.get("entry")
                if recovered_entry is not None:
                    try: recovered_entry = Decimal(str(recovered_entry))
                    except Exception: recovered_entry = None

                if recovered_entry is None: recovered_entry = self.last_price or self.client.last_traded_price()
                recovered_sl = pos.get("stop_loss") or self.sl
                if recovered_sl is None: recovered_sl = self.low if direction == "LONG" else self.high

                self.active_trade = {
                    "direction": direction,
                    "entry_price": float(recovered_entry) if recovered_entry else None,
                    "entry_time": now_ist().isoformat(),
                    "size": abs(size)
                }

                if recovered_sl: self.sl = Decimal(str(recovered_sl))
                self.last_position = size
                self.bot_enabled = True
                self.stop_reason = None
                
                self.save()
                return {"success": True, "bot_enabled": True, "message": f"Bot started with existing {direction} position."}

            self.last_position = 0
            self.active_trade = None
            self.sl = None
            self.bot_enabled = True
            self.stop_reason = None
            self.save()
            return {"success": True, "bot_enabled": True, "message": "Bot started. Ready for trades."}

    def stop_bot(self):
        with self.lock:
            self.bot_enabled = False
            self.stop_reason = "MANUAL STOP"
            self.save()
            self.client.cancel_all_orders(self.product_id)
            try:
                pos = self.refresh_position(force=True)
            except Exception:
                pos = {"size": 0}

            size = int(pos.get("size", 0))
            if size != 0:
                try:
                    self.client.close_position(self.product_id, size)
                except Exception:
                    pass

            exit_p = self.last_price or self.client.last_traded_price()
            self.finish_active_trade(exit_p, "MANUAL_STOP")
            self.last_position = 0
            self.sl = None
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
        self.save()

    def prepare(self, now, price):
        start = strategy_start(self.day)
        if now < start: return False
        if self.ready: return True

        try: pos = self.refresh_position(force=True)
        except Exception: pos = {"size": self.last_position}
        self.last_position = int(pos.get("size", 0))

        c_start = self.day
        c_end = start
        high, low = self.client.historical_high_low(c_start, c_end)
        
        if high is not None and low is not None:
            self.high = high
            self.low = low
            self.ready = True
            self.save()
            logging.warning(f"{self.account_name} | 05:30-05:45 RANGE LOADED | HIGH={high} | LOW={low}")
            return True

        if self.high is None or price > self.high: self.high = price
        if self.low is None or price < self.low: self.low = price
        self.ready = True
        self.save()
        return True

    def enter(self, direction, price, sl):
        if not self.bot_enabled or sl is None: return False

        pos = self.refresh_position(force=True)
        if pos["size"] != 0:
            self.last_position = pos["size"]
            return False

        side = "buy" if direction == "LONG" else "sell"

        if (direction == "LONG" and sl >= price) or (direction == "SHORT" and sl <= price):
            return False

        try:
            size = self.client.order_size(self.product, price)
            self.client.market_entry(self.product_id, side, size, sl)
        except Exception as e:
            logging.error(f"{self.account_name} | ENTRY ERROR | {e}")
            return False

        confirmed = False
        for _ in range(15):
            time.sleep(0.1)
            try:
                p = self.client.position(self.product_id)
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
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "size": abs(int(trade_size)),
            "pnl": float(pnl),
            "reason": reason
        }

        history = load_trade_history(self.account_id)
        history.append(trade)
        save_trade_history(self.account_id, history)
        self.active_trade = None
        self.save()

    def execute_540_exit(self):
        with self.lock:
            if self.daily_squared_off: return
            logging.warning(f"{self.account_name} | 05:40 SQUAREOFF")
            for _ in range(3):
                try:
                    self.client.cancel_all_orders(self.product_id)
                    pos = self.refresh_position(force=True)
                    size = int(pos.get("size", 0))
                    if size != 0:
                        exit_price = self.last_price or self.client.last_traded_price()
                        self.client.close_position(self.product_id, size)
                        self.finish_active_trade(exit_price, "DAILY_0540_SQUAREOFF")
                    self.last_position = 0
                    self.sl = None
                    self.daily_squared_off = True
                    self.save()
                    break
                except Exception:
                    time.sleep(1)

    def execute_545_preparation(self):
        with self.lock:
            now = now_ist()
            price = self.last_price or self.client.last_traded_price()
            if price: self.prepare(now, price)

    def evaluate(self, price=None):
        with self.lock:
            now = now_ist()
            if price is None: price = self.last_price or self.client.last_traded_price()
            if price is None: return
            self.last_price = price

            if weekend(now): return
            self.new_day(now)

            sq_time = daily_squareoff_time(now)
            s_start = strategy_start(self.day)
            
            if now >= sq_time and now < s_start and not self.daily_squared_off:
                self.execute_540_exit()

            if now < s_start: return
            if not self.prepare(now, price): return

            pos = self.refresh_position()
            size = int(pos.get("size", 0))

            if self.last_position != 0 and size != 0 and ((self.last_position > 0 and size < 0) or (self.last_position < 0 and size > 0)):
                new_dir = "SHORT" if size < 0 else "LONG"
                self.finish_active_trade(price, "REVERSAL_SLM")
                self.last_position = size
                self.sl = Decimal(str(self.trade_high if new_dir == "SHORT" else self.trade_low)) if (self.trade_high if new_dir == "SHORT" else self.trade_low) else price
                self.active_trade = {"direction": new_dir, "entry_price": float(price), "entry_time": now_ist().isoformat(), "size": abs(int(size))}
                self.save()
                return

            if size == 0 and self.last_position != 0:
                self.finish_active_trade(price, "EXTERNAL_CLOSE")
                self.last_position = 0
                self.sl = None
                self.save()
                return

            if size > 0:
                self.last_position = size
                if self.trade_high is None or price > self.trade_high: self.trade_high = price
                return

            if size < 0:
                self.last_position = size
                if self.trade_low is None or price < self.trade_low: self.trade_low = price
                return

            self.last_position = 0
            if not self.bot_enabled: return

            if self.high is not None and price > self.high:
                if self.enter("LONG", price, self.low): self.high = price
                return

            if self.low is not None and price < self.low:
                if self.enter("SHORT", price, self.high): self.low = price
                return

BOT_ACCOUNTS = {}
ACCOUNTS_LOCK = threading.RLock()

def load_primary_account():
    try:
        primary = AccountBot(
            account_id=PRIMARY_ACCOUNT_ID,
            account_name=PRIMARY_ACCOUNT_NAME,
            account_type="primary",
            api_key=PRIMARY_API_KEY,
            api_secret=PRIMARY_API_SECRET,
            subscription={}
        )
        primary.client.set_leverage(primary.product_id)
        with ACCOUNTS_LOCK: BOT_ACCOUNTS[primary.account_id] = primary
        logging.warning("PRIMARY ACCOUNT LOADED")
        return True
    except Exception as e:
        logging.exception(f"PRIMARY LOAD ERROR | {e}")
        return False

class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/health":
            self.send_json({"success": True, "online": True})
            return
        if self.path.split("?", 1)[0] == "/api/dashboard":
            with ACCOUNTS_LOCK:
                bots = list(BOT_ACCOUNTS.values())
            accounts = [{
                "account_name": b.account_name,
                "current_price": float(b.last_price) if b.last_price else None,
                "bot_running": b.bot_enabled
            } for b in bots]
            self.send_json({"success": True, "accounts": accounts})
            return
        return super().do_GET()

    def send_json(self, data, status=200):
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args): pass

def start_dashboard():
    def server_thread():
        port = int(os.getenv("PORT", DASHBOARD_PORT))
        server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
        server.serve_forever()
    threading.Thread(target=server_thread, daemon=True).start()

def background_timer_loop():
    while True:
        time.sleep(1)
        try:
            now = now_ist()
            time_str = now.strftime("%H:%M")
            with ACCOUNTS_LOCK: bots = list(BOT_ACCOUNTS.values())
            if not bots: continue

            if time_str == "05:40":
                for b in bots: b.execute_540_exit()
            if time_str == "05:45":
                for b in bots: b.execute_545_preparation()

            for b in bots: b.evaluate()
        except Exception:
            pass

def run_websocket():
    while True:
        try:
            def on_open(ws):
                ws.send(json.dumps({"type": "subscribe", "payload": {"channels": [{"name": "trades", "symbols": [SYMBOL]}]}}))

            def on_message(ws, message):
                data = json.loads(message)
                if data.get("type") != "trades": return
                p_val = data.get("p") or (data.get("data", {}).get("p") if isinstance(data.get("data"), dict) else None)
                if p_val is None: return
                price = Decimal(str(p_val))
                with ACCOUNTS_LOCK: bots = list(BOT_ACCOUNTS.values())
                for b in bots: b.evaluate(price)

            ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message)
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception:
            pass
        time.sleep(RECONNECT_SECONDS)

if __name__ == "__main__":
    logging.warning("XAUTUSD BOT STARTING ON RAILWAY")
    start_dashboard()
    load_primary_account()
    threading.Thread(target=background_timer_loop, daemon=True).start()
    run_websocket()
