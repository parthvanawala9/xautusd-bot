import os
import time
import json
import hmac
import hashlib
import logging
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

# ============================================================
# XAUTUSD BREAKOUT BOT - VERSION 22.2 (FIXED DELTA API BRACKET)
# ============================================================

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip("/")
SYMBOL = os.getenv("DELTA_SYMBOL", "XAUTUSD")
API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()

LEVERAGE = Decimal(os.getenv("LEVERAGE", "50"))
BALANCE_FRACTION = Decimal(os.getenv("BALANCE_FRACTION", "0.10"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "0.50"))
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
    "User-Agent": "XAUTUSD-Breakout-Engine/22.2"
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
    if weekday == 5:  # Saturday from 05:00 IST
        return dt.hour >= 5
    if weekday == 6:  # Entire Sunday
        return True
    if weekday == 0:  # Monday before 05:30 IST
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

def get_price():
    ticker = api_call("GET", f"/v2/tickers/{SYMBOL}")["result"]
    value = ticker.get("close") or ticker.get("last_price") or ticker.get("mark_price")
    if value is None:
        raise RuntimeError("Ticker returned no valid price.")
    return Decimal(str(value))

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

def calculate_candle_extremes(start_dt, end_dt, include_only_completed=True):
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    if end_ts <= start_ts:
        return None, None

    candles = get_candles(start_dt, end_dt)
    highest = None
    lowest = None

    for candle in candles:
        try:
            candle_start_ts = int(candle["time"])
        except Exception:
            continue

        if candle_start_ts < start_ts:
            continue

        if include_only_completed:
            if candle_start_ts + 900 > end_ts:
                continue

        try:
            candle_high = Decimal(str(candle["high"]))
            candle_low = Decimal(str(candle["low"]))
        except Exception:
            continue

        if highest is None or candle_high > highest:
            highest = candle_high
        if lowest is None or candle_low < lowest:
            lowest = candle_low

    return highest, lowest

# ============================================================
# EXECUTION & SIZING
# ============================================================

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
    logging.warning(f"LIVE BRACKET MARKET ORDER | SIDE={side} | SIZE={abs(size)} | SL={sl_price}")
    return api_call("POST", "/v2/orders", body=body, auth=True)

def update_position_bracket_sl(product_id, sl_price):
    body = {
        "product_id": int(product_id),
        "stop_loss_price": str(sl_price),
        "stop_type": "stop_loss",
        "stop_trigger_method": "last_traded_price"
    }
    logging.warning(f"UPDATING BRACKET SL ON POSITION | PRODUCT={product_id} | NEW SL={sl_price}")
    return api_call("POST", "/v2/positions/change_pos_bracket", body=body, auth=True)

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

    contract_value = Decimal(str(product.get("contract_value") or product.get("contract_value_usd") or "1"))
    raw_size = notional / (price * contract_value)

    lot_size = Decimal(str(product.get("lot_size") or product.get("order_size_increment") or "1"))
    min_size = Decimal(str(product.get("min_order_size") or product.get("minimum_order_size") or lot_size))

    size_decimal = (raw_size / lot_size).to_integral_value(rounding=ROUND_DOWN) * lot_size
    if size_decimal < min_size:
        raise RuntimeError("Calculated size is below exchange minimum order size.")

    size = int(size_decimal)
    if size <= 0:
        raise RuntimeError("Calculated order size is zero.")
    return size

# ============================================================
# STRATEGY ENGINE
# ============================================================

class TradingStrategy:
    def __init__(self, product):
        self.product = product
        self.product_id = int(product["id"])

        self.day_start = None
        
        # A. LOCKED BREAKOUT LEVELS (Calculated from 05:30–05:45 Range)
        self.locked_day_high = None
        self.locked_day_low = None
        self.range_ready = False

        # B. DYNAMIC RUNNING DAY EXTREMES (Updated up to entry time)
        self.running_day_high = None
        self.running_day_low = None

        # C. CURRENT POSITION SL
        self.current_sl = None
        self.last_position = 0

        # D. STATE FLAGS
        self.carried_position = False
        self.needs_0545_sl_reset = False

        self.manual_flat = False
        self.manual_exit_high = None
        self.manual_exit_low = None

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
            self.current_sl = Decimal(s["current_sl"]) if s.get("current_sl") else None
            self.carried_position = bool(s.get("carried_position", False))
            self.needs_0545_sl_reset = bool(s.get("needs_0545_sl_reset", False))
            self.manual_flat = bool(s.get("manual_flat", False))
            self.manual_exit_high = Decimal(s["manual_exit_high"]) if s.get("manual_exit_high") else None
            self.manual_exit_low = Decimal(s["manual_exit_low"]) if s.get("manual_exit_low") else None
        except Exception as exc:
            logging.error(f"STATE LOAD ERROR: {exc}")

    def save_state(self):
        state = {
            "day_start": self.day_start.isoformat() if self.day_start else None,
            "locked_day_high": str(self.locked_day_high) if self.locked_day_high is not None else None,
            "locked_day_low": str(self.locked_day_low) if self.locked_day_low is not None else None,
            "running_day_high": str(self.running_day_high) if self.running_day_high is not None else None,
            "running_day_low": str(self.running_day_low) if self.running_day_low is not None else None,
            "range_ready": self.range_ready,
            "current_sl": str(self.current_sl) if self.current_sl is not None else None,
            "carried_position": self.carried_position,
            "needs_0545_sl_reset": self.needs_0545_sl_reset,
            "manual_flat": self.manual_flat,
            "manual_exit_high": str(self.manual_exit_high) if self.manual_exit_high is not None else None,
            "manual_exit_low": str(self.manual_exit_low) if self.manual_exit_low is not None else None
        }
        temp = STATE_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2)
        os.replace(temp, STATE_FILE)

    def handle_new_day(self, now, current_position):
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
        self.manual_flat = False
        self.manual_exit_high = None
        self.manual_exit_low = None

        if current_position != 0:
            self.carried_position = True
            self.needs_0545_sl_reset = True
            logging.warning("POSITION CARRIED INTO NEW DAY. OLD BRACKET SL REMAINS ACTIVE UNTIL 05:45 IST.")
        else:
            self.carried_position = False
            self.needs_0545_sl_reset = False
            self.current_sl = None

        self.save_state()

    def build_initial_range(self, now):
        if self.day_start is None:
            return False
        execution_start = trading_execution_start(self.day_start)
        if now < execution_start:
            return False

        high, low = calculate_candle_extremes(self.day_start, execution_start, include_only_completed=True)
        if high is None or low is None:
            logging.warning("05:30-05:45 RANGE NOT READY YET.")
            return False

        self.locked_day_high = high
        self.locked_day_low = low
        self.running_day_high = high
        self.running_day_low = low
        self.range_ready = True
        self.save_state()
        logging.warning(f"LOCKED BREAKOUT RANGE (05:30-05:45) | HIGH={self.locked_day_high} | LOW={self.locked_day_low}")
        return True

    def update_running_day_extremes(self, now, current_price):
        if self.day_start is None or now <= self.day_start:
            return

        high, low = calculate_candle_extremes(self.day_start, now, include_only_completed=False)
        if high is None or current_price > high:
            high = current_price
        if low is None or current_price < low:
            low = current_price

        updated = False
        if self.running_day_high is None or high > self.running_day_high:
            self.running_day_high = high
            updated = True
        if self.running_day_low is None or low < self.running_day_low:
            self.running_day_low = low
            updated = True

        if updated:
            self.save_state()

    def get_actual_current_day_extremes(self, now, current_price):
        self.update_running_day_extremes(now, current_price)
        high = self.running_day_high or current_price
        low = self.running_day_low or current_price
        return max(high, current_price), min(low, current_price)

    def execute_entry(self, direction, price, sl_price, reason):
        if is_weekend_blocked():
            return False
        if sl_price is None:
            logging.error("ENTRY BLOCKED: SL is None.")
            return False

        if direction == "LONG" and sl_price >= price:
            logging.error(f"LONG ENTRY BLOCKED | PRICE={price} | SL={sl_price}")
            return False
        if direction == "SHORT" and sl_price <= price:
            logging.error(f"SHORT ENTRY BLOCKED | PRICE={price} | SL={sl_price}")
            return False

        existing = get_position(self.product_id)
        if existing["size"] != 0:
            logging.warning("ENTRY BLOCKED: Position already open.")
            return False

        size = calculate_order_size(self.product, price)
        side = "buy" if direction == "LONG" else "sell"

        execute_bracket_market_order(self.product_id, side, size, sl_price)

        filled_size = 0
        for _ in range(30):
            time.sleep(0.20)
            position = get_position(self.product_id)
            if (direction == "LONG" and position["size"] > 0) or (direction == "SHORT" and position["size"] < 0):
                filled_size = position["size"]
                break

        if filled_size == 0:
            raise RuntimeError("Bracket market entry sent but fill was not confirmed.")

        self.last_position = filled_size
        self.current_sl = Decimal(str(sl_price))
        self.manual_flat = False
        self.manual_exit_high = None
        self.manual_exit_low = None
        self.carried_position = False
        
        now = now_ist()
        if now < trading_execution_start(trading_day_start(now)):
            self.needs_0545_sl_reset = True
        else:
            self.needs_0545_sl_reset = False

        self.save_state()
        logging.warning(f"BRACKET ENTRY CONFIRMED [{reason}] | {direction} | SIZE={filled_size} | ENTRY={price} | SL={sl_price}")
        return True

    def handle_closed_position(self, old_size, current_price):
        previous_sl = self.current_sl

        was_sl_triggered = False
        if previous_sl is not None:
            if old_size > 0 and current_price <= previous_sl:
                was_sl_triggered = True
            elif old_size < 0 and current_price >= previous_sl:
                was_sl_triggered = True

        self.current_sl = None
        self.last_position = 0

        if not was_sl_triggered:
            logging.warning("POSITION CLOSED MANUALLY/EXTERNALLY. BASELINE LOCKED.")
            self.manual_flat = True
            self.manual_exit_high = current_price
            self.manual_exit_low = current_price
            self.carried_position = False
            self.needs_0545_sl_reset = False
            self.save_state()
            return

        logging.warning("STOP LOSS FILLED -> EXECUTING IMMEDIATE REVERSAL")
        self.manual_flat = False

        now = now_ist()
        day_high_now, day_low_now = self.get_actual_current_day_extremes(now, current_price)

        if old_size > 0:  # LONG stopped -> Reversal to SHORT
            reverse_sl = day_high_now
            if reverse_sl is None or reverse_sl <= current_price:
                reverse_sl = max(day_high_now, current_price + Decimal("0.50"))
            self.execute_entry("SHORT", current_price, reverse_sl, "LONG SL HIT -> REVERSE SHORT")
            return

        # SHORT stopped -> Reversal to LONG
        reverse_sl = day_low_now
        if reverse_sl is None or reverse_sl >= current_price:
            reverse_sl = min(day_low_now, current_price - Decimal("0.50"))
        self.execute_entry("LONG", current_price, reverse_sl, "SHORT SL HIT -> REVERSE LONG")

    def update_carried_position_stop(self, position_size, current_price):
        if not self.range_ready:
            return False

        new_sl = self.locked_day_low if position_size > 0 else self.locked_day_high
        direction = "LONG" if position_size > 0 else "SHORT"

        if new_sl is None:
            return False

        logging.warning(f"05:45 NEW DAY RANGE BRACKET SL REPLACEMENT | {direction} | NEW SL={new_sl}")
        
        try:
            update_position_bracket_sl(self.product_id, new_sl)
            self.current_sl = new_sl
            self.needs_0545_sl_reset = False
            self.carried_position = False
            self.save_state()
            return True
        except Exception as exc:
            logging.error(f"FAILED TO UPDATE BRACKET SL: {exc}")
            return False

    def fix_active_sl_if_incorrect(self, current_size, current_price):
        now = now_ist()
        self.update_running_day_extremes(now, current_price)

        if current_size > 0 and self.running_day_low is not None:
            correct_sl = self.running_day_low
            if self.current_sl is not None and self.current_sl > correct_sl:
                logging.warning(f"CORRECTING BRACKET SL FROM {self.current_sl} TO TRUE DAY LOW {correct_sl}")
                try:
                    update_position_bracket_sl(self.product_id, correct_sl)
                    self.current_sl = correct_sl
                    self.save_state()
                except Exception as exc:
                    logging.error(f"SL CORRECTION FAILED: {exc}")

        elif current_size < 0 and self.running_day_high is not None:
            correct_sl = self.running_day_high
            if self.current_sl is not None and self.current_sl < correct_sl:
                logging.warning(f"CORRECTING BRACKET SL FROM {self.current_sl} TO TRUE DAY HIGH {correct_sl}")
                try:
                    update_position_bracket_sl(self.product_id, correct_sl)
                    self.current_sl = correct_sl
                    self.save_state()
                except Exception as exc:
                    logging.error(f"SL CORRECTION FAILED: {exc}")

    def run_cycle(self):
        now = now_ist()

        # 1. Saturday Squareoff Check
        if is_saturday_squareoff_time(now):
            position = get_position(self.product_id)
            size = position["size"]
            if size != 0:
                logging.warning(f"SATURDAY 05:00 SQUARE OFF | SIZE={size}")
                close_position_market(self.product_id, size)
                self.last_position = 0
                self.current_sl = None
                self.carried_position = False
                self.needs_0545_sl_reset = False
                self.save_state()
            return

        if is_weekend_blocked(now):
            return

        current_price = get_price()
        position = get_position(self.product_id)
        current_size = position["size"]

        # 2. Check Day Transition
        self.handle_new_day(now, current_size)

        # 3. Position Closure Transition Check
        if current_size == 0 and self.last_position != 0:
            self.handle_closed_position(self.last_position, current_price)
            return

        # Keep updating running extremes dynamically
        if self.day_start and now >= self.day_start:
            self.update_running_day_extremes(now, current_price)

        execution_start = trading_execution_start(self.day_start)

        # 4. Handle Position Management (If Position Exists)
        if current_size != 0:
            self.last_position = current_size

            if now >= execution_start:
                if not self.range_ready:
                    self.build_initial_range(now)

                if self.needs_0545_sl_reset and self.range_ready:
                    self.update_carried_position_stop(current_size, current_price)

                self.fix_active_sl_if_incorrect(current_size, current_price)
            return

        # 5. Hold Flat States Before 05:45 IST
        if now < execution_start:
            return

        # 6. Build 05:30-05:45 Locked Range (Flat State)
        if not self.range_ready:
            if not self.build_initial_range(now):
                return

        # 7. Flat State Breakout Monitoring
        self.last_position = 0
        self.current_sl = None

        if not self.range_ready:
            return

        # Handle Manual Exit Re-entry
        if self.manual_flat:
            if self.manual_exit_high is not None and current_price > self.manual_exit_high:
                sl = self.running_day_low or self.locked_day_low
                if self.execute_entry("LONG", current_price, sl, "MANUAL FLAT -> NEW HIGH"):
                    self.manual_flat = False
                    self.save_state()
                    return
            elif self.manual_exit_low is not None and current_price < self.manual_exit_low:
                sl = self.running_day_high or self.locked_day_high
                if self.execute_entry("SHORT", current_price, sl, "MANUAL FLAT -> NEW LOW"):
                    self.manual_flat = False
                    self.save_state()
                    return
            return

        # Breakout Entry using TRUE RUNNING DAY EXTREMES as SL
        if self.locked_day_high is not None and current_price > self.locked_day_high:
            sl = min(self.locked_day_low, self.running_day_low) if self.running_day_low else self.locked_day_low
            if self.execute_entry("LONG", current_price, sl, "DAY HIGH BREAKOUT"):
                return

        if self.locked_day_low is not None and current_price < self.locked_day_low:
            sl = max(self.locked_day_high, self.running_day_high) if self.running_day_high else self.locked_day_high
            if self.execute_entry("SHORT", current_price, sl, "DAY LOW BREAKOUT"):
                return

    def start(self):
        set_leverage(self.product_id)
        logging.warning("XAUTUSD BREAKOUT ENGINE v22.2 (FIXED DELTA API BRACKET) ONLINE.")

        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                logging.warning("BOT STOPPED BY USER.")
                break
            except Exception as exc:
                logging.exception(f"UNHANDLED BOT ERROR: {exc}")
                time.sleep(3)
            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    product_info = get_product()
    TradingStrategy(product_info).start()
