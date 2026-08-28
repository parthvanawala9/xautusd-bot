import os
import time
import json
import hmac
import hashlib
import logging
import threading
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
import websocket
from dotenv import load_dotenv

# ============================================================
# XAUTUSD BREAKOUT BOT - VERSION 25.0 (ANYTIME DYNAMIC RUNNING EXTREMES)
# ============================================================

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip("/")
WS_URL = os.getenv("DELTA_WS_URL", "wss://socket.india.delta.exchange")
SYMBOL = os.getenv("DELTA_SYMBOL", "XAUTUSD")
API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

LEVERAGE = Decimal(os.getenv("LEVERAGE", "50"))
BALANCE_FRACTION = Decimal(os.getenv("BALANCE_FRACTION", "0.10"))
STATE_FILE = os.getenv("STATE_FILE", "xautusd_state.json")

if not API_KEY or not API_SECRET:
    raise SystemExit("Missing DELTA_API_KEY or DELTA_API_SECRET in environment.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "XAUTUSD-Breakout-Engine/25.0"
})

# ============================================================
# TIME HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)

def trading_day_start(dt=None):
    dt = dt or now_ist()
    boundary = dt.replace(hour=5, minute=30, second=0, microsecond=0)
    if dt < boundary:
        boundary -= timedelta(days=1)
    return boundary

def is_weekend_blocked(dt=None):
    dt = dt or now_ist()
    weekday = dt.weekday()
    if weekday == 5:
        return dt.hour >= 5
    if weekday == 6:
        return True
    if weekday == 0:
        return dt < dt.replace(hour=5, minute=30, second=0, microsecond=0)
    return False

def is_saturday_squareoff_time(dt=None):
    dt = dt or now_ist()
    return dt.weekday() == 5 and dt.hour == 5 and dt.minute < 30

# ============================================================
# API CORE & AUTH
# ============================================================

def sign_request(method, path, query_string="", body=""):
    timestamp = str(int(time.time()))
    message = method.upper() + timestamp + path + query_string + body
    signature = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {
        "api-key": API_KEY,
        "signature": signature,
        "timestamp": timestamp
    }

def api_call(method, path, params=None, body=None, auth=False):
    params = params or {}
    body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
    query_string = "?" + urlencode(params, doseq=True) if params else ""
    headers = sign_request(method, path, query_string, body_text) if auth else {}

    try:
        response = session.request(
            method.upper(),
            BASE_URL + path,
            params=params,
            data=body_text if body is not None else None,
            headers=headers,
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success") is False:
            raise RuntimeError(f"API Error: {data}")
        return data
    except Exception as exc:
        raise RuntimeError(f"HTTP Request failed for {method} {path}: {exc}") from exc

def get_product():
    return api_call("GET", f"/v2/products/{SYMBOL}")["result"]

def get_position(product_id):
    result = api_call("GET", "/v2/positions", params={"product_id": int(product_id)}, auth=True)["result"]
    if not result or not isinstance(result, dict):
        return {"size": 0, "entry_price": None}
    return {
        "size": int(result.get("size", 0)),
        "entry_price": result.get("entry_price")
    }

def get_balance():
    data = api_call("GET", "/v2/wallet/balances", auth=True)
    for wallet in data.get("result", []):
        if str(wallet.get("asset_symbol", "")).upper() in ("USD", "USDT"):
            value = wallet.get("balance") or wallet.get("available_balance")
            if value is not None:
                return Decimal(str(value))
    net_equity = data.get("meta", {}).get("net_equity")
    if net_equity:
        return Decimal(str(net_equity))
    raise RuntimeError("Could not retrieve wallet balance.")

def execute_bracket_market_order(product_id, side, size, sl_price):
    body = {
        "product_id": int(product_id),
        "product_symbol": SYMBOL,
        "size": int(abs(size)),
        "side": side,
        "order_type": "market_order",
        "bracket_stop_loss_price": str(sl_price),
        "bracket_stop_loss_type": "market_order",
        "stop_trigger_method": "last_traded_price",
        "client_order_id": f"xent_{int(time.time()*1000)}"[:32]
    }
    logging.warning(f"MARKET ENTRY WITH SL | SIDE={side} | SIZE={abs(size)} | SL={sl_price}")
    return api_call("POST", "/v2/orders", body=body, auth=True)

def close_position_market(product_id, size):
    side = "sell" if size > 0 else "buy"
    body = {
        "product_id": int(product_id),
        "product_symbol": SYMBOL,
        "size": int(abs(size)),
        "side": side,
        "order_type": "market_order",
        "client_order_id": f"xexit_{int(time.time()*1000)}"[:32]
    }
    logging.warning(f"CLOSING POSITION MARKET | SIDE={side} | SIZE={abs(size)}")
    return api_call("POST", "/v2/orders", body=body, auth=True)

def set_leverage(product_id):
    try:
        api_call("POST", f"/v2/products/{product_id}/orders/leverage", body={"leverage": str(LEVERAGE)}, auth=True)
        logging.info(f"LEVERAGE = {LEVERAGE}x")
    except Exception as exc:
        logging.warning(f"Leverage setting failed: {exc}")

def calculate_order_size(product, price):
    balance = get_balance()
    margin = balance * BALANCE_FRACTION
    notional = margin * LEVERAGE
    contract_value = Decimal(str(product.get("contract_value") or "1"))
    raw_size = notional / (price * contract_value)
    lot_size = Decimal(str(product.get("lot_size") or "1"))
    min_size = Decimal(str(product.get("min_order_size") or lot_size))
    size_decimal = (raw_size / lot_size).to_integral_value(rounding=ROUND_DOWN) * lot_size
    return int(max(min_size, size_decimal))

# ============================================================
# STRATEGY STATE & ENGINE
# ============================================================

class TradingStrategy:
    def __init__(self, product):
        self.product = product
        self.product_id = int(product["id"])

        self.day_start = None
        self.running_high = None
        self.running_low = None
        self.last_position = 0
        self.current_sl = None

        self.load_state()

    def load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                s = json.load(file)
            self.day_start = datetime.fromisoformat(s["day_start"]) if s.get("day_start") else None
            self.running_high = Decimal(s["running_high"]) if s.get("running_high") else None
            self.running_low = Decimal(s["running_low"]) if s.get("running_low") else None
            self.current_sl = Decimal(s["current_sl"]) if s.get("current_sl") else None
        except Exception as exc:
            logging.error(f"STATE LOAD ERROR: {exc}")

    def save_state(self):
        state = {
            "day_start": self.day_start.isoformat() if self.day_start else None,
            "running_high": str(self.running_high) if self.running_high else None,
            "running_low": str(self.running_low) if self.running_low else None,
            "current_sl": str(self.current_sl) if self.current_sl else None
        }
        temp = STATE_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)
        os.replace(temp, STATE_FILE)

    def check_new_day(self, now):
        new_day = trading_day_start(now)
        if self.day_start == new_day:
            return
        logging.warning(f"NEW TRADING DAY ENTERED: {new_day}")
        self.day_start = new_day
        self.running_high = None
        self.running_low = None
        self.save_state()

    def handle_closed_position(self, old_size, current_price):
        old_sl = self.current_sl
        self.current_sl = None
        self.last_position = 0

        was_sl_triggered = False
        if old_sl is not None:
            if old_size > 0 and current_price <= old_sl:
                was_sl_triggered = True
            elif old_size < 0 and current_price >= old_sl:
                was_sl_triggered = True

        if not was_sl_triggered:
            logging.warning("Position closed manually. Waiting for next breakout.")
            self.save_state()
            return

        logging.warning("STOP LOSS HIT -> INSTANT REVERSAL EXECUTING NOW")
        size = calculate_order_size(self.product, current_price)

        if old_size > 0:
            # Long stopped -> Reverse to Short
            reverse_sl = self.running_high or (current_price + Decimal("1.00"))
            execute_bracket_market_order(self.product_id, "sell", size, reverse_sl)
            self.current_sl = reverse_sl
        else:
            # Short stopped -> Reverse to Long
            reverse_sl = self.running_low or (current_price - Decimal("1.00"))
            execute_bracket_market_order(self.product_id, "buy", size, reverse_sl)
            self.current_sl = reverse_sl

        self.save_state()

    def on_tick(self, price_str):
        now = now_ist()
        if is_weekend_blocked(now):
            return

        if is_saturday_squareoff_time(now):
            pos = get_position(self.product_id)
            if pos["size"] != 0:
                close_position_market(self.product_id, pos["size"])
                self.current_sl = None
            return

        self.check_new_day(now)
        current_price = Decimal(price_str)

        # Update running day extremes dynamically on every tick
        if self.running_high is None or current_price > self.running_high:
            self.running_high = current_price
            self.save_state()
        if self.running_low is None or current_price < self.running_low:
            self.running_low = current_price
            self.save_state()

        position = get_position(self.product_id)
        current_size = position["size"]

        # Check if position just got closed (e.g. SL hit)
        if current_size == 0 and self.last_position != 0:
            old_size = self.last_position
            self.last_position = 0
            self.handle_closed_position(old_size, current_price)
            return

        self.last_position = current_size

        # If flat, check for immediate breakout of running extremes at any time
        if current_size == 0 and self.running_high and self.running_low:
            size = calculate_order_size(self.product, current_price)
            
            # If price exceeds previous running high (excluding initial tick)
            if current_price >= self.running_high and current_price != self.running_high:
                sl = self.running_low
                execute_bracket_market_order(self.product_id, "buy", size, sl)
                self.current_sl = sl
                self.last_position = size
                self.save_state()
            elif current_price <= self.running_low and current_price != self.running_low:
                sl = self.running_high
                execute_bracket_market_order(self.product_id, "sell", size, sl)
                self.current_sl = sl
                self.last_position = -size
                self.save_state()

    def start_websocket(self):
        def on_message(ws, message):
            try:
                data = json.loads(message)
                if "close" in data or "mark_price" in data or "last_price" in data:
                    p = data.get("close") or data.get("mark_price") or data.get("last_price")
                    if p:
                        self.on_tick(str(p))
            except Exception as e:
                logging.error(f"WS Error: {e}")

        def on_open(ws):
            logging.info("WebSocket connected. Streaming ticks anytime...")
            sub_payload = {
                "type": "subscribe",
                "payload": {
                    "channels": [{"name": "tickers", "symbols": [SYMBOL]}]
                }
            }
            ws.send(json.dumps(sub_payload))

        def run_ws():
            while True:
                try:
                    ws_app = websocket.WebSocketApp(
                        WS_URL,
                        on_open=on_open,
                        on_message=on_message
                    )
                    ws_app.run_forever(ping_interval=30, ping_timeout=10)
                except Exception as exc:
                    logging.error(f"WS Dropped: {exc}. Reconnecting...")
                    time.sleep(3)

        set_leverage(self.product_id)
        logging.warning("XAUTUSD DYNAMIC ANYTIME ENGINE v25.0 ONLINE.")
        
        ws_thread = threading.Thread(target=run_ws, daemon=True)
        ws_thread.start()

        while True:
            time.sleep(1)

if __name__ == "__main__":
    product_info = get_product()
    TradingStrategy(product_info).start_websocket()
