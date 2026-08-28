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
# XAUTUSD BREAKOUT BOT - VERSION 24.0 (INSTANT NATIVE WS REVERSAL)
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
    "User-Agent": "XAUTUSD-Breakout-Engine/24.0"
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

def trading_execution_start(day_start):
    return day_start + timedelta(minutes=15)

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

def get_candles(start_dt, end_dt):
    return api_call("GET", "/v2/history/candles", params={
        "resolution": "15m",
        "symbol": SYMBOL,
        "start": int(start_dt.astimezone(UTC).timestamp()),
        "end": int(end_dt.astimezone(UTC).timestamp())
    })["result"]

def calculate_candle_extremes(start_dt, end_dt):
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())
    if end_ts <= start_ts:
        return None, None

    candles = get_candles(start_dt, end_dt)
    highest, lowest = None, None

    for candle in candles:
        try:
            candle_start_ts = int(candle["time"])
            if candle_start_ts < start_ts or candle_start_ts + 900 > end_ts:
                continue
            h, l = Decimal(str(candle["high"])), Decimal(str(candle["low"]))
            highest = h if highest is None or h > highest else highest
            lowest = l if lowest is None or l < lowest else lowest
        except Exception:
            continue
    return highest, lowest

# ============================================================
# NATIVE EXCHANGE ORDERS
# ============================================================

def place_native_breakout_order(product_id, side, size, trigger_price, sl_price):
    body = {
        "product_id": int(product_id),
        "product_symbol": SYMBOL,
        "size": int(abs(size)),
        "side": side,
        "order_type": "stop_order",
        "stop_order_type": "stop_market",
        "stop_price": str(trigger_price),
        "stop_trigger_method": "last_traded_price",
        "bracket_stop_loss_price": str(sl_price),
        "bracket_stop_loss_type": "market_order",
        "client_order_id": f"xbo_{int(time.time()*1000)}"[:32]
    }
    logging.warning(f"NATIVE BREAKOUT ORDER PLACED | SIDE={side} | TRIGGER={trigger_price} | SL={sl_price}")
    return api_call("POST", "/v2/orders", body=body, auth=True)

def cancel_all_open_orders(product_id):
    try:
        api_call("DELETE", "/v2/orders/all", body={"product_id": int(product_id)}, auth=True)
        logging.info("All resting open/conditional orders cancelled.")
    except Exception as exc:
        logging.warning(f"Failed to cancel open orders: {exc}")

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
# STRATEGY STATE & WEBSOCKET ENGINE
# ============================================================

class TradingStrategy:
    def __init__(self, product):
        self.product = product
        self.product_id = int(product["id"])

        self.day_start = None
        self.locked_day_high = None
        self.locked_day_low = None
        self.range_ready = False
        
        self.running_day_high = None
        self.running_day_low = None

        self.orders_placed_for_day = False
        self.load_state()

    def load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                s = json.load(file)
            self.day_start = datetime.fromisoformat(s["day_start"]) if s.get("day_start") else None
            self.locked_day_high = Decimal(s["locked_day_high"]) if s.get("locked_day_high") else None
            self.locked_day_low = Decimal(s["locked_day_low"]) if s.get("locked_day_low") else None
            self.running_day_high = Decimal(s["running_day_high"]) if s.get("running_day_high") else None
            self.running_day_low = Decimal(s["running_day_low"]) if s.get("running_day_low") else None
            self.range_ready = bool(s.get("range_ready", False))
            self.orders_placed_for_day = bool(s.get("orders_placed_for_day", False))
        except Exception as exc:
            logging.error(f"STATE LOAD ERROR: {exc}")

    def save_state(self):
        state = {
            "day_start": self.day_start.isoformat() if self.day_start else None,
            "locked_day_high": str(self.locked_day_high) if self.locked_day_high else None,
            "locked_day_low": str(self.locked_day_low) if self.locked_day_low else None,
            "running_day_high": str(self.running_day_high) if self.running_day_high else None,
            "running_day_low": str(self.running_day_low) if self.running_day_low else None,
            "range_ready": self.range_ready,
            "orders_placed_for_day": self.orders_placed_for_day
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
        self.locked_day_high = None
        self.locked_day_low = None
        self.running_day_high = None
        self.running_day_low = None
        self.range_ready = False
        self.orders_placed_for_day = False
        
        cancel_all_open_orders(self.product_id)
        self.save_state()

    def try_lock_range(self, now):
        if self.day_start is None:
            return False
        execution_start = trading_execution_start(self.day_start)
        if now < execution_start:
            return False

        high, low = calculate_candle_extremes(self.day_start, execution_start)
        if high is None or low is None:
            return False

        self.locked_day_high = high
        self.locked_day_low = low
        self.running_day_high = high
        self.running_day_low = low
        self.range_ready = True
        self.save_state()
        logging.warning(f"RANGE LOCKED | HIGH={self.locked_day_high} | LOW={self.locked_day_low}")
        return True

    def place_initial_native_orders(self, current_price):
        if self.orders_placed_for_day or not self.range_ready:
            return

        position = get_position(self.product_id)
        if position["size"] != 0:
            return

        size = calculate_order_size(self.product, current_price)

        place_native_breakout_order(
            self.product_id, "buy", size, 
            trigger_price=self.locked_day_high, 
            sl_price=self.locked_day_low
        )
        place_native_breakout_order(
            self.product_id, "sell", size, 
            trigger_price=self.locked_day_low, 
            sl_price=self.locked_day_high
        )

        self.orders_placed_for_day = True
        self.save_state()
        logging.warning("NATIVE PENDING BREAKOUT ORDERS DEPLOYED TO EXCHANGE BOOK.")

    def on_tick(self, price_str):
        now = now_ist()

        if is_weekend_blocked(now):
            return

        if is_saturday_squareoff_time(now):
            position = get_position(self.product_id)
            if position["size"] != 0:
                close_position_market(self.product_id, position["size"])
                cancel_all_open_orders(self.product_id)
            return

        self.check_new_day(now)

        current_price = Decimal(price_str)

        if self.running_day_high is None or current_price > self.running_day_high:
            self.running_day_high = current_price
        if self.running_day_low is None or current_price < self.running_day_low:
            self.running_day_low = current_price

        execution_start = trading_execution_start(self.day_start)
        if now >= execution_start and not self.range_ready:
            self.try_lock_range(now)

        if self.range_ready and not self.orders_placed_for_day:
            self.place_initial_native_orders(current_price)

    def start_websocket(self):
        def on_message(ws, message):
            try:
                data = json.loads(message)
                if "close" in data or "mark_price" in data or "last_price" in data:
                    p = data.get("close") or data.get("mark_price") or data.get("last_price")
                    if p:
                        self.on_tick(str(p))
            except Exception as e:
                logging.error(f"WS Message Error: {e}")

        def on_open(ws):
            logging.info("WebSocket connected. Subscribing to live ticker stream...")
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
                    logging.error(f"WebSocket connection dropped: {exc}. Reconnecting in 3s...")
                    time.sleep(3)

        set_leverage(self.product_id)
        logging.warning("XAUTUSD INSTANT NATIVE ENGINE v24.0 ONLINE.")
        
        ws_thread = threading.Thread(target=run_ws, daemon=True)
        ws_thread.start()

        while True:
            time.sleep(1)

if __name__ == "__main__":
    product_info = get_product()
    TradingStrategy(product_info).start_websocket()
