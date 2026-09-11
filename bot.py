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

# ============================================================
# XAUTUSD BOT + FULL HISTORY, RAILWAY LOGS & DASHBOARD
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

STATE_DIR = os.getenv("STATE_DIR", os.path.join(BASE_DIR, "account_states"))
HISTORY_DIR = os.getenv("HISTORY_DIR", os.path.join(BASE_DIR, "account_history"))
CLIENTS_FILE = os.path.join(BASE_DIR, "clients_config.json")

PRIMARY_ACCOUNT_ID = os.getenv("ACCOUNT_ID", "primary").strip()
PRIMARY_ACCOUNT_NAME = os.getenv("ACCOUNT_NAME", "Primary Account").strip()
PRIMARY_API_KEY = os.getenv("DELTA_API_KEY", "").strip()
PRIMARY_API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(HISTORY_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True
)

CACHED_SERVER_IP = "Detecting..."

def update_server_ip():
    global CACHED_SERVER_IP
    try:
        res = requests.get("https://api.ipify.org?format=json", timeout=5)
        ip = res.json().get("ip")
        if ip:
            CACHED_SERVER_IP = ip
            logging.warning(f"==================================================")
            logging.warning(f" RAILWAY OUTBOUND IP --> {ip}")
            logging.warning(f" WHITELIST THIS IP IN DELTA EXCHANGE API SETTINGS")
            logging.warning(f"==================================================")
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
    m1730 = dt.replace(hour=17, minute=30, second=0, microsecond=0)
    
    if t >= dtime(5, 30) and t < dtime(17, 30):
        return m530
    elif t >= dtime(17, 30):
        return m1730
    else:
        return m1730 - timedelta(days=1)

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

class DeltaClient:
    def __init__(self, api_key, api_secret, account_name):
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.account_name = (account_name or "Account").strip()
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "XAUTUSD-Bot/20.0"
        })

    def sign(self, method, path, query="", body=""):
        timestamp = str(int(time.time()))
        message = method.upper() + timestamp + path + query + body
        signature = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "User-Agent": "XAUTUSD-Bot/20.0"
        }

    def api(self, method, path, params=None, body=None, auth=False):
        params = params or {}
        body_text = json.dumps(body, separators=(",", ":")) if body is not None else ""
        query = ("?" + urlencode(params, doseq=True)) if params else ""
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
                value = wallet.get("available_balance") or wallet.get("balance")
                if value is not None:
                    return Decimal(str(value))
        raise RuntimeError("USD/USDT balance not found.")

    def set_leverage(self, product_id):
        try:
            self.api("POST", f"/v2/products/{product_id}/orders/leverage", body={"leverage": str(LEVERAGE)}, auth=True)
        except Exception:
            pass

    def order_size(self, product_info, price):
        bal = self.balance()
        margin = bal * BALANCE_FRACTION
        notional = margin * LEVERAGE
        contract_value = Decimal(str(product_info.get("contract_value") or product_info.get("contract_value_usd") or "0.001"))
        if contract_value <= 0: contract_value = Decimal("0.001")
        raw = notional / price / contract_value
        increment = Decimal(str(product_info.get("lot_size") or product_info.get("order_size_increment") or "1"))
        minimum = Decimal(str(product_info.get("min_order_size") or product_info.get("minimum_order_size") or increment))
        if increment <= 0: increment = Decimal("1")
        size_decimal = (raw / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        if size_decimal < minimum: size_decimal = minimum
        size = int(size_decimal)
        if size <= 0: raise RuntimeError("Order size calculated as zero.")
        return size

    def cancel_all_orders(self, product_id):
        try:
            self.api("DELETE", "/v2/orders/all", body={"product_id": int(product_id)}, auth=True)
        except Exception:
            pass

    def market_entry(self, product_id, side, size, sl, tp):
        body = {
            "product_id": int(product_id),
            "product_symbol": SYMBOL,
            "size": int(abs(size)),
            "side": side,
            "order_type": "market_order",
            "bracket_stop_loss_price": str(sl),
            "bracket_stop_trigger_method": "last_traded_price",
            "bracket_take_profit_price": str(tp),
            "bracket_take_profit_trigger_method": "last_traded_price",
            "client_order_id": (f"exact_{int(time.time() * 1000)}")[-32:]
        }
        return self.api("POST", "/v2/orders", body=body, auth=True)

    def close_position(self, product_id, size):
        if size == 0: return
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
        return self.api("POST", "/v2/orders", body=body, auth=True)

    def fetch_session_candle(self, session_start):
        try:
            start_time = session_start
            end_time = start_time + timedelta(minutes=30)
            data = self.api("GET", "/v2/history/candles", params={
                "resolution": "15m", "symbol": SYMBOL,
                "start": int(start_time.timestamp()), "end": int(end_time.timestamp())
            })
            candles = data.get("result", [])
            target_ts = int(start_time.timestamp())
            for candle in candles:
                c_time = candle.get("time") or candle.get("timestamp") or candle.get("start")
                if c_time and int(c_time) == target_ts:
                    return Decimal(str(candle["high"])), Decimal(str(candle["low"]))
            if candles:
                return Decimal(str(candles[0]["high"])), Decimal(str(candles[0]["low"]))
            return None, None
        except Exception:
            return None, None

    def last_traded_price(self):
        try:
            data = self.api("GET", f"/v2/tickers/{SYMBOL}")
            res = data.get("result")
            if isinstance(res, dict):
                p = res.get("close") or res.get("spot_price") or res.get("ltp")
                if p is not None: return Decimal(str(p))
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
    except Exception:
        pass
    return []

def save_trade_history(account_id, history):
    atomic_write_json(account_history_file(account_id), history)

def calculate_trade_pnl(direction, entry_price, exit_price, size, product_info):
    try:
        entry = Decimal(str(entry_price))
        exit_val = Decimal(str(exit_price))
        qty = Decimal(str(abs(size)))
        cv = Decimal(str(product_info.get("contract_value") or product_info.get("contract_value_usd") or "0.001"))
        if cv <= 0: cv = Decimal("0.001")
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
        return {"total_trades": total, "winning_trades": len(wins), "losing_trades": len(losses), "win_rate": float(win_rate), "pnl": float(pnl)}
    now = now_ist()
    today_str = now.strftime("%Y-%m-%d")
    today_trades = [t for t in history if str(t.get("date", "")).startswith(today_str)]
    return {"today": compute_stats(today_trades), "all_time": compute_stats(history)}

class AccountBot:
    def __init__(self, account_id, account_name, account_type, api_key, api_secret, subscription=None):
        self.account_id = account_id
        self.account_name = account_name
        self.account_type = account_type
        self.subscription = subscription or {}
        self.client = DeltaClient(api_key, api_secret, account_name)

        self.product = None
        self.product_id = 0
        self.session_start = None
        self.base_high = None
        self.base_low = None
        self.last_position = 0
        self.last_price = None
        self.prev_price = None
        self.ready = False
        self.bot_enabled = True
        self.stop_reason = None

        self.lock = threading.RLock()
        self.cached_position = {"size": 0, "entry": None, "stop_loss": None, "unrealized_pnl": 0}
        self.position_cache_time = 0

        self.load_state()
        self.save()

    def load_state(self):
        filename = account_state_file(self.account_id)
        if not os.path.exists(filename): return
        try:
            with open(filename, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("session_start"): self.session_start = datetime.fromisoformat(state["session_start"])
            if state.get("base_high") is not None: self.base_high = Decimal(str(state["base_high"]))
            if state.get("base_low") is not None: self.base_low = Decimal(str(state["base_low"]))
            if state.get("active_trade"): self.active_trade = state["active_trade"]
            self.bot_enabled = state.get("bot_enabled", True)
            self.stop_reason = state.get("stop_reason", None)
            self.ready = state.get("ready", False)
        except Exception:
            pass

    def save(self):
        data = {
            "account_id": self.account_id, "account_name": self.account_name, "symbol": SYMBOL,
            "session_start": self.session_start.isoformat() if self.session_start else None,
            "base_high": str(self.base_high) if self.base_high is not None else None,
            "base_low": str(self.base_low) if self.base_low is not None else None,
            "active_trade": getattr(self, 'active_trade', None),
            "bot_enabled": self.bot_enabled, "stop_reason": self.stop_reason, "ready": self.ready
        }
        atomic_write_json(account_state_file(self.account_id), data)

    def refresh_position(self, force=False):
        if not self.product_id:
            try:
                self.product = self.client.product()
                self.product_id = int(self.product["id"])
            except Exception:
                return self.cached_position

        current = time.time()
        if not force and (current - self.position_cache_time) < POSITION_CACHE_SECONDS:
            return self.cached_position
        try:
            pos = self.client.position(self.product_id)
            self.cached_position = pos
            self.position_cache_time = current
            
            # --- रेलवे लॉग्स में रनिंग पोजीशन प्रिंट करें ---
            logging.info(f"[{self.account_name}] Position: Size={pos.get('size')} | Entry={pos.get('entry')} | PnL={pos.get('unrealized_pnl')}")
            
            return pos
        except Exception:
            return self.cached_position

    def start_bot(self):
        with self.lock:
            pos = self.refresh_position(force=True)
            size = int(pos.get("size", 0))
            if size != 0:
                direction = "LONG" if size > 0 else "SHORT"
                recovered_entry = pos.get("entry")
                if recovered_entry is not None:
                    try: recovered_entry = Decimal(str(recovered_entry))
                    except Exception: recovered_entry = None
                if recovered_entry is None: recovered_entry = self.last_price or self.client.last_traded_price()
                self.active_trade = {"direction": direction, "entry_price": float(recovered_entry) if recovered_entry else None, "entry_time": now_ist().isoformat(), "size": abs(size)}
                self.last_position = size
                self.bot_enabled = True
                self.save()
                return {"success": True, "bot_enabled": True, "message": "Bot started with existing position."}
            self.last_position = 0
            self.active_trade = None
            self.bot_enabled = True
            self.save()
            return {"success": True, "bot_enabled": True, "message": "Bot started."}

    def stop_bot(self):
        with self.lock:
            self.bot_enabled = False
            self.stop_reason = "MANUAL STOP"
            self.save()
            if self.product_id:
                self.client.cancel_all_orders(self.product_id)
                try: pos = self.refresh_position(force=True)
                except Exception: pos = {"size": 0}
                size = int(pos.get("size", 0))
                if size != 0:
                    try: self.client.close_position(self.product_id, size)
                    except Exception: pass
            exit_p = self.last_price or self.client.last_traded_price()
            self.finish_active_trade(exit_p, "MANUAL_STOP")
            self.last_position = 0
            self.save()
            return {"success": True, "bot_enabled": False, "message": "Bot stopped."}

    def check_session_change(self, now):
        current_sess = get_current_session_start(now)
        if self.session_start != current_sess:
            if self.product_id:
                pos = self.refresh_position(force=True)
                size = int(pos.get("size", 0))
                if size != 0:
                    exit_price = self.last_price or self.client.last_traded_price()
                    try:
                        self.client.close_position(self.product_id, size)
                        self.finish_active_trade(exit_price, "SESSION_SWITCH_SQUAREOFF")
                    except Exception: pass
            self.session_start = current_sess
            self.base_high = None
            self.base_low = None
            self.prev_price = None
            self.ready = False
            self.save()

    def prepare(self, now):
        if self.ready: return True
        if now < self.session_start + timedelta(minutes=15): return False
        high, low = self.client.fetch_session_candle(self.session_start)
        if high is not None and low is not None:
            self.base_high = high
            self.base_low = low
            self.ready = True
            self.save()
            return True
        return False

    def enter(self, direction, price):
        if is_weekend() or not self.bot_enabled or self.base_high is None or self.base_low is None or not self.product_id: return False
        pos = self.refresh_position(force=True)
        if pos["size"] != 0:
            self.last_position = pos["size"]
            return False
        side = "buy" if direction == "LONG" else "sell"
        if direction == "LONG":
            sl = self.base_low
            tp = price + ((price - sl) * Decimal("5"))
        else:
            sl = self.base_high
            tp = price - ((sl - price) * Decimal("5"))
        try:
            size = self.client.order_size(self.product, price)
            self.client.market_entry(self.product_id, side, size, sl, tp)
        except Exception:
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
        self.active_trade = {"direction": direction, "entry_price": float(price), "entry_time": now_ist().isoformat(), "size": abs(int(self.last_position))}
        self.save()
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
        pnl = calculate_trade_pnl(direction, entry_price, exit_price, trade_size, self.product or {"contract_value": "0.001"})
        trade = {
            "id": f"trade_{int(time.time() * 1000)}", "account_id": self.account_id, "account": self.account_name,
            "symbol": SYMBOL, "date": now_ist().strftime("%Y-%m-%d %H:%M"), "direction": direction,
            "entry_price": float(entry_price), "exit_price": float(exit_price), "size": abs(int(trade_size)),
            "pnl": float(pnl), "reason": reason
        }
        history = load_trade_history(self.account_id)
        history.append(trade)
        save_trade_history(self.account_id, history)
        self.active_trade = None
        self.save()

    def evaluate(self, price=None):
        with self.lock:
            now = now_ist()
            if price is None: price = self.last_price or self.client.last_traded_price()
            if price is None: return
            if self.prev_price is None:
                self.prev_price = price
                self.last_price = price
                return
            old_price = self.prev_price
            new_price = price
            self.prev_price = price
            self.last_price = price
            if is_weekend(now):
                if self.product_id:
                    pos = self.refresh_position()
                    size = int(pos.get("size", 0))
                    if size != 0:
                        try:
                            self.client.close_position(self.product_id, size)
                            self.finish_active_trade(price, "WEEKEND_SQUAREOFF")
                        except Exception: pass
                        self.last_position = 0
                        self.save()
                return
            self.check_session_change(now)
            if not self.prepare(now): return
            pos = self.refresh_position()
            size = int(pos.get("size", 0))
            if size == 0 and self.last_position != 0:
                self.finish_active_trade(price, "CLOSED_OR_EXITED")
                self.last_position = 0
                self.save()
                return
            if size != 0:
                self.last_position = size
                return
            self.last_position = 0
            if not self.bot_enabled: return
            if self.base_high is not None and old_price <= self.base_high and new_price > self.base_high:
                self.enter("LONG", self.base_high)
                return
            if self.base_low is not None and old_price >= self.base_low and new_price < self.base_low:
                self.enter("SHORT", self.base_low)
                return

BOT_ACCOUNTS = {}
ACCOUNTS_LOCK = threading.RLock()

def load_all_accounts():
    with ACCOUNTS_LOCK:
        BOT_ACCOUNTS.clear()
        if PRIMARY_API_KEY and PRIMARY_API_SECRET:
            try:
                primary = AccountBot(
                    account_id=PRIMARY_ACCOUNT_ID,
                    account_name=PRIMARY_ACCOUNT_NAME,
                    account_type="primary",
                    api_key=PRIMARY_API_KEY,
                    api_secret=PRIMARY_API_SECRET,
                    subscription={}
                )
                BOT_ACCOUNTS[primary.account_id] = primary
            except Exception as e:
                logging.error(f"Primary account load error: {e}")

        clients_cfg = load_clients_config()
        for cid, cdata in clients_cfg.items():
            try:
                client_bot = AccountBot(
                    account_id=cid,
                    account_name=cdata.get("name", "Client"),
                    account_type="client",
                    api_key=cdata.get("api_key"),
                    api_secret=cdata.get("api_secret"),
                    subscription={
                        "start": cdata.get("subscription_start"),
                        "expiry": cdata.get("subscription_expiry"),
                        "fee": cdata.get("subscription_fee", 0)
                    }
                )
                BOT_ACCOUNTS[cid] = client_bot
            except Exception as e:
                logging.error(f"Client {cid} load error: {e}")

class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        query = parse_qs(parsed_path.query)

        if path == "/api/health":
            self.send_json({"success": True, "online": True})
            return

        if path == "/api/dashboard":
            client_token = query.get("token", [None])[0]
            server_ip = CACHED_SERVER_IP

            with ACCOUNTS_LOCK:
                bots = list(BOT_ACCOUNTS.values())

            if client_token:
                target_bot = None
                clients_cfg = load_clients_config()
                for cid, cdata in clients_cfg.items():
                    if cdata.get("token") == client_token:
                        target_bot = BOT_ACCOUNTS.get(cid)
                        break
                if not target_bot:
                    self.send_json({"success": False, "message": "Unauthorized client token"}, status=403)
                    return
                bots = [target_bot]

            accounts_data = []
            clients_cfg = load_clients_config()

            for b in bots:
                try:
                    pos = b.refresh_position()
                    balance_val = float(b.client.balance()) if b.client else 0
                except Exception:
                    pos = {"size": 0, "entry": None, "stop_loss": None, "unrealized_pnl": 0}
                    balance_val = 0

                exchange_pnl = float(pos.get("unrealized_pnl", 0))
                if exchange_pnl == 0 and pos.get("size", 0) != 0 and pos.get("entry") and b.last_price:
                    try:
                        entry = Decimal(str(pos["entry"]))
                        cur = Decimal(str(b.last_price))
                        sz = Decimal(str(pos["size"]))
                        cv = Decimal("0.001")
                        if sz > 0:
                            exchange_pnl = float((cur - entry) * sz * cv)
                        else:
                            exchange_pnl = float((entry - cur) * abs(sz) * cv)
                    except Exception:
                        pass

                history = load_trade_history(b.account_id)
                stats = calculate_statistics(history)
                
                direction = "FLAT"
                if pos.get("size", 0) > 0: direction = "LONG"
                elif pos.get("size", 0) < 0: direction = "SHORT"

                sub_info = b.subscription
                token = clients_cfg.get(b.account_id, {}).get("token", "") if b.account_type == "client" else ""

                accounts_data.append({
                    "account_id": b.account_id,
                    "account_name": b.account_name,
                    "account_type": b.account_type,
                    "token": token,
                    "server_ip": server_ip,
                    "balance": balance_val,
                    "current_price": float(b.last_price) if b.last_price else None,
                    "bot_enabled": b.bot_enabled,
                    "contract_value": 0.001,
                    "position": {
                        "size": pos.get("size", 0),
                        "direction": direction,
                        "entry_price": float(pos["entry"]) if pos.get("entry") else None,
                        "stop_loss": float(b.base_high) if direction == "SHORT" else (float(b.base_low) if direction == "LONG" else None),
                        "unrealized_pnl": exchange_pnl
                    },
                    "statistics": stats,
                    "trade_history": history,
                    "subscription": sub_info
                })
            
            self.send_json({"success": True, "server_online": True, "server_ip": server_ip, "accounts": accounts_data})
            return

        if path == "/" or path == "":
            self.path = "/index.html"
            
        return super().do_GET()

    def do_POST(self):
        parsed_path = urlparse(self.path).path
        content_length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(content_length).decode('utf-8')) if content_length > 0 else {}
        
        if parsed_path == "/api/bot/start":
            acc_id = body.get("account_id")
            with ACCOUNTS_LOCK:
                bot = BOT_ACCOUNTS.get(acc_id)
                if bot:
                    res = bot.start_bot()
                    self.send_json(res)
                    return
            self.send_json({"success": False, "message": "Account not found"}, status=404)
            return
            
        if parsed_path == "/api/bot/stop":
            acc_id = body.get("account_id")
            with ACCOUNTS_LOCK:
                bot = BOT_ACCOUNTS.get(acc_id)
                if bot:
                    res = bot.stop_bot()
                    self.send_json(res)
                    return
            self.send_json({"success": False, "message": "Account not found"}, status=404)
            return

        if parsed_path == "/api/client/add":
            name = body.get("name")
            api_key = body.get("api_key")
            api_secret = body.get("api_secret")
            if not name or not api_key or not api_secret:
                self.send_json({"success": False, "message": "Missing fields"}, status=400)
                return
            
            cid = f"client_{int(time.time())}"
            token = hashlib.sha256(f"{cid}_{time.time()}".encode()).hexdigest()[:16]
            
            clients_cfg = load_clients_config()
            clients_cfg[cid] = {
                "name": name, "api_key": api_key, "api_secret": api_secret, "token": token,
                "subscription_start": body.get("subscription_start"),
                "subscription_expiry": body.get("subscription_expiry"),
                "subscription_fee": body.get("subscription_fee", 0)
            }
            save_clients_config(clients_cfg)
            load_all_accounts()
            self.send_json({"success": True, "message": "Client added successfully"})
            return

        if parsed_path == "/api/client/delete":
            acc_id = body.get("account_id")
            clients_cfg = load_clients_config()
            if acc_id in clients_cfg:
                del clients_cfg[acc_id]
                save_clients_config(clients_cfg)
                with ACCOUNTS_LOCK:
                    if acc_id in BOT_ACCOUNTS:
                        BOT_ACCOUNTS[acc_id].stop_bot()
                        del BOT_ACCOUNTS[acc_id]
                self.send_json({"success": True, "message": "Client removed"})
                return
            self.send_json({"success": False, "message": "Client not found"}, status=404)
            return
            
        self.send_json({"success": False, "message": "Not found"}, status=404)

    def send_json(self, data, status=200):
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args): pass

def start_dashboard():
    port = int(os.getenv("PORT", DASHBOARD_PORT))
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    logging.warning(f"WEB SERVER STARTED ON PORT {port}")
    server.serve_forever()

def background_timer_loop():
    while True:
        time.sleep(1)
        try:
            with ACCOUNTS_LOCK: bots = list(BOT_ACCOUNTS.values())
            if not bots: continue
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
    logging.warning("XAUTUSD BOT STARTING...")
    update_server_ip()
    load_all_accounts()
    threading.Thread(target=background_timer_loop, daemon=True).start()
    threading.Thread(target=run_websocket, daemon=True).start()
    start_dashboard()
