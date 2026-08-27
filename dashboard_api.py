import os
import time
import json
import hmac
import hashlib
import subprocess
from decimal import Decimal
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify
from flask_cors import CORS
from dotenv import load_dotenv


# ============================================================
# XAUTUSD DASHBOARD API (READ-ONLY)
# ============================================================

BOT_DIR = "/home/opc/xautusd-bot"
DASHBOARD_DIR = os.path.join(BOT_DIR, "dashboard")
BOT_FILE = os.path.join(BOT_DIR, "bot.py")
DEFAULT_STATE_FILE = os.path.join(BOT_DIR, "xautusd_state.json")

# Load environment
ENV_FILES = [
    os.path.join(BOT_DIR, ".env"),
    os.path.join(DASHBOARD_DIR, ".env")
]

for env_file in ENV_FILES:
    if os.path.exists(env_file):
        load_dotenv(env_file, override=False)

load_dotenv(override=False)

IST = ZoneInfo("Asia/Kolkata")
BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip("/")
SYMBOL = os.getenv("DELTA_SYMBOL", "XAUTUSD").strip()
API_KEY = os.getenv("DELTA_API_KEY", "").strip()
API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
STATE_FILE = os.getenv("STATE_FILE", DEFAULT_STATE_FILE)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "XAUTUSD-Dashboard/3.0"
})

# Cache for product_id to avoid redundant requests
CACHED_PRODUCT_ID = None


# ============================================================
# HELPERS
# ============================================================

def decimal_value(value, default=None):
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return default


def json_number(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def now_ist():
    return datetime.now(IST)


def parse_delta_time(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    text = str(value).strip()
    if not text:
        return None

    try:
        number = int(text)
        if number > 10**14:
            return datetime.fromtimestamp(number / 1_000_000, tz=timezone.utc)
        if number > 10**11:
            return datetime.fromtimestamp(number / 1_000, tz=timezone.utc)
        if number > 10**8:
            return datetime.fromtimestamp(number, tz=timezone.utc)
    except Exception:
        pass

    try:
        iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(iso_text)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def iso_ist(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).isoformat()


# ============================================================
# DELTA AUTHENTICATION (FIXED TIMESTAMP IN MS)
# ============================================================

def sign_request(method, path, query_string="", body=""):
    # Fix: Delta API expects timestamp in milliseconds
    timestamp = str(int(time.time() * 1000))

    message = (
        method.upper()
        + timestamp
        + path
        + query_string
        + body
    )

    signature = hmac.new(
        API_SECRET.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    return {
        "api-key": API_KEY,
        "signature": signature,
        "timestamp": timestamp
    }


def delta_request(method, path, params=None, authenticated=False):
    params = params or {}
    clean_params = {k: v for k, v in params.items() if v is not None}

    query_string = ("?" + urlencode(clean_params, doseq=True)) if clean_params else ""
    headers = {}

    if authenticated:
        if not API_KEY or not API_SECRET:
            raise RuntimeError("DELTA_API_KEY or DELTA_API_SECRET missing.")
        headers = sign_request(method, path, query_string, "")

    try:
        response = session.request(
            method.upper(),
            BASE_URL + path,
            params=clean_params,
            headers=headers,
            timeout=15
        )
    except Exception as exc:
        raise RuntimeError(f"Delta connection failed: {exc}") from exc

    if not response.ok:
        raise RuntimeError(f"Delta API HTTP {response.status_code}: {response.text[:500]}")

    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"Delta returned invalid JSON: {response.text[:500]}") from exc

    if data.get("success") is False:
        raise RuntimeError(f"Delta API error: {data}")

    return data


# ============================================================
# DATA FETCHERS
# ============================================================

def get_product():
    global CACHED_PRODUCT_ID
    
    # Try fetching by direct symbol endpoint first
    try:
        data = delta_request("GET", f"/v2/products/{SYMBOL}")
        result = data.get("result", {})
        if isinstance(result, dict) and result.get("id"):
            CACHED_PRODUCT_ID = result.get("id")
            return result
    except Exception:
        pass

    # Fallback: Query all products and filter for symbol
    data = delta_request("GET", "/v2/products")
    products = data.get("result", [])
    for p in products:
        if isinstance(p, dict) and p.get("symbol") == SYMBOL:
            CACHED_PRODUCT_ID = p.get("id")
            return p

    return {}


def get_ticker():
    # Attempt fetching ticker by symbol or product_id
    try:
        data = delta_request("GET", f"/v2/tickers/{SYMBOL}")
        result = data.get("result", {})
        if isinstance(result, dict) and result:
            return result
    except Exception:
        pass

    data = delta_request("GET", "/v2/tickers")
    tickers = data.get("result", [])
    for t in tickers:
        if isinstance(t, dict) and t.get("symbol") == SYMBOL:
            return t

    raise RuntimeError("Ticker data not found.")


def get_current_price():
    ticker = get_ticker()
    for key in ("close", "last_price", "mark_price", "spot_price", "price"):
        value = ticker.get(key)
        if value not in (None, ""):
            price = decimal_value(value)
            if price is not None:
                return price

    raise RuntimeError(f"Ticker returned no usable price. Ticker={ticker}")


def get_balance_data():
    data = delta_request("GET", "/v2/wallet/balances", authenticated=True)
    wallets = data.get("result", [])
    if isinstance(wallets, dict):
        wallets = [wallets]

    for wallet in wallets:
        if not isinstance(wallet, dict):
            continue
        asset = str(wallet.get("asset_symbol", wallet.get("symbol", wallet.get("asset", "")))).upper()
        if asset in ("USD", "USDT"):
            balance = decimal_value(wallet.get("balance"))
            available = decimal_value(wallet.get("available_balance"))
            if balance is None:
                balance = available
            if balance is not None:
                return {"balance": balance, "available_balance": available, "asset": asset, "raw": wallet}

    for wallet in wallets:
        if isinstance(wallet, dict):
            balance = decimal_value(wallet.get("balance"))
            available = decimal_value(wallet.get("available_balance"))
            if balance is None:
                balance = available
            if balance is not None:
                asset = str(wallet.get("asset_symbol", wallet.get("asset", "UNKNOWN"))).upper()
                return {"balance": balance, "available_balance": available, "asset": asset, "raw": wallet}

    meta = data.get("meta", {})
    if isinstance(meta, dict) and meta.get("net_equity") is not None:
        return {"balance": decimal_value(meta.get("net_equity")), "available_balance": None, "asset": "EQUITY", "raw": meta}

    raise RuntimeError(f"No usable wallet balance found. Response={data}")


def get_position_data(product_id):
    data = delta_request("GET", "/v2/positions", params={"product_id": int(product_id)}, authenticated=True)
    result = data.get("result")

    if not result:
        return {"size": 0, "entry_price": None, "realized_pnl": Decimal("0"), "realized_funding": Decimal("0"), "raw": {}}

    if isinstance(result, list) and len(result) > 0:
        result = result[0]

    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected position response: {data}")

    return {
        "size": safe_int(result.get("size", 0)),
        "entry_price": decimal_value(result.get("entry_price")),
        "realized_pnl": decimal_value(result.get("realized_pnl"), Decimal("0")),
        "realized_funding": decimal_value(result.get("realized_funding"), Decimal("0")),
        "raw": result
    }


def load_bot_state():
    candidates = [STATE_FILE, DEFAULT_STATE_FILE, os.path.join(DASHBOARD_DIR, "xautusd_state.json")]
    for filename in list(dict.fromkeys(candidates)):
        try:
            if os.path.exists(filename):
                with open(filename, "r", encoding="utf-8") as file:
                    state = json.load(file)
                    if isinstance(state, dict):
                        return state
        except Exception:
            continue
    return {}


def is_bot_running():
    try:
        result = subprocess.run(["pgrep", "-af", BOT_FILE], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            if "pgrep" in line or "dashboard_api.py" in line:
                continue
            if BOT_FILE in line:
                return True
        return False
    except Exception:
        return False


def get_stop_loss(state):
    if not isinstance(state, dict):
        return None
    for key in ("current_sl", "stop_loss", "active_stop_loss", "current_stop_loss", "sl_price"):
        value = state.get(key)
        if value not in (None, ""):
            res = decimal_value(value)
            if res is not None:
                return res
    return None


def get_open_stop_order(product_id):
    try:
        data = delta_request("GET", "/v2/orders", params={"product_ids": str(product_id), "states": "open,pending"}, authenticated=True)
        result = data.get("result", [])
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            return None

        for order in result:
            if isinstance(order, dict):
                stop_type = str(order.get("stop_order_type", "")).lower()
                if "stop" in stop_type or order.get("stop_price") not in (None, ""):
                    return order
        return None
    except Exception:
        return None


def get_contract_value(product):
    if not isinstance(product, dict):
        return Decimal("1")
    for key in ("contract_value", "contract_value_usd", "contract_unit_value"):
        val = decimal_value(product.get(key))
        if val is not None and val > 0:
            return val
    return Decimal("1")


def calculate_unrealized_pnl(position, current_price, product):
    size = safe_int(position.get("size", 0))
    entry = decimal_value(position.get("entry_price"))
    if size == 0 or entry is None or current_price is None:
        return Decimal("0")
    contract_value = get_contract_value(product)
    return Decimal(size) * (current_price - entry) * contract_value


def get_xautusd_fills(product_id):
    try:
        data = delta_request("GET", "/v2/fills", params={"product_ids": str(product_id), "page_size": 50}, authenticated=True)
        result = data.get("result", [])
        if isinstance(result, dict):
            result = [result]
        return result if isinstance(result, list) else []
    except Exception:
        return []


def calculate_fill_statistics(fills, product):
    contract_value = get_contract_value(product)
    if not fills:
        return {
            "realized_pnl": Decimal("0"), "today_pnl": Decimal("0"),
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "win_rate": 0, "trades": []
        }

    normalized = []
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        side = str(fill.get("side", "")).lower()
        if side not in ("buy", "sell"):
            continue
        size = safe_int(fill.get("size", 0))
        price = decimal_value(fill.get("price"))
        if size <= 0 or price is None:
            continue
        dt = parse_delta_time(fill.get("created_at") or fill.get("timestamp"))
        commission = decimal_value(fill.get("commission"), Decimal("0"))

        normalized.append({
            "side": side, "size": size, "price": price, "dt": dt,
            "commission": commission, "id": fill.get("id"), "order_id": fill.get("order_id")
        })

    normalized.sort(key=lambda x: (x["dt"] or datetime.min.replace(tzinfo=timezone.utc), str(x["id"] or "")))

    lots = []
    realized_gross = Decimal("0")
    realized_commission = Decimal("0")
    completed_trades = []

    for fill in normalized:
        qty = fill["size"] if fill["side"] == "buy" else -fill["size"]
        price = fill["price"]
        realized_commission += fill["commission"]
        remaining = qty

        while remaining != 0 and lots and ((remaining > 0 and lots[0]["qty"] < 0) or (remaining < 0 and lots[0]["qty"] > 0)):
            lot = lots[0]
            match_qty = min(abs(remaining), abs(lot["qty"]))
            if lot["qty"] > 0:
                gross = (price - lot["price"]) * Decimal(match_qty) * contract_value
                direction = "LONG"
            else:
                gross = (lot["price"] - price) * Decimal(match_qty) * contract_value
                direction = "SHORT"

            realized_gross += gross
            completed_trades.append({
                "direction": direction, "size": match_qty,
                "entry_price": json_number(lot["price"]), "exit_price": json_number(price),
                "pnl": json_number(gross), "entry_time": iso_ist(lot["dt"]), "exit_time": iso_ist(fill["dt"])
            })

            lot["qty"] += -match_qty if lot["qty"] > 0 else match_qty
            remaining += -match_qty if remaining > 0 else match_qty
            if lot["qty"] == 0:
                lots.pop(0)

        if remaining != 0:
            lots.append({"qty": remaining, "price": price, "dt": fill["dt"]})

    realized_total = realized_gross - realized_commission
    today = now_ist().date()
    today_pnl = Decimal("0")

    for trade in completed_trades:
        exit_time = trade.get("exit_time")
        if exit_time:
            try:
                if datetime.fromisoformat(exit_time).date() == today:
                    today_pnl += decimal_value(trade.get("pnl"), Decimal("0"))
            except Exception:
                pass

    today_commission = sum(fill["commission"] for fill in normalized if fill.get("dt") and fill["dt"].astimezone(IST).date() == today)
    today_pnl -= today_commission

    winning_trades = sum(1 for t in completed_trades if decimal_value(t.get("pnl"), Decimal("0")) > 0)
    losing_trades = sum(1 for t in completed_trades if decimal_value(t.get("pnl"), Decimal("0")) < 0)
    total_trades = winning_trades + losing_trades
    win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0

    completed_trades.reverse()
    return {
        "realized_pnl": realized_total, "today_pnl": today_pnl,
        "total_trades": total_trades, "winning_trades": winning_trades,
        "losing_trades": losing_trades, "win_rate": round(win_rate, 2),
        "trades": completed_trades[:50]
    }


def serialize_trade(trade):
    return {k: json_number(v) for k, v in trade.items()}


# ============================================================
# DASHBOARD BUILDER
# ============================================================

def build_dashboard():
    errors = []

    product = {}
    try:
        product = get_product()
    except Exception as exc:
        errors.append(f"Product: {exc}")

    product_id = product.get("id")

    current_price = None
    try:
        current_price = get_current_price()
    except Exception as exc:
        errors.append(f"Price: {exc}")

    state = load_bot_state()
    bot_running = is_bot_running()

    balance_data = {"balance": None, "available_balance": None, "asset": None}
    try:
        balance_data = get_balance_data()
    except Exception as exc:
        errors.append(f"Balance: {exc}")

    position = {"size": 0, "entry_price": None, "realized_pnl": Decimal("0"), "realized_funding": Decimal("0"), "raw": {}}
    if product_id is not None:
        try:
            position = get_position_data(product_id)
        except Exception as exc:
            errors.append(f"Position: {exc}")
    else:
        errors.append("Position: Product ID not found.")

    stop_loss = get_stop_loss(state)
    if stop_loss is None and product_id is not None:
        stop_order = get_open_stop_order(product_id)
        if stop_order:
            stop_loss = decimal_value(stop_order.get("stop_price"))

    unrealized_pnl = calculate_unrealized_pnl(position, current_price, product)

    fill_statistics = {"realized_pnl": Decimal("0"), "today_pnl": Decimal("0"), "total_trades": 0, "winning_trades": 0, "losing_trades": 0, "win_rate": 0, "trades": []}
    fills = []
    if product_id is not None:
        try:
            fills = get_xautusd_fills(product_id)
            fill_statistics = calculate_fill_statistics(fills, product)
        except Exception as exc:
            errors.append(f"Fills: {exc}")

    total_realized_pnl = fill_statistics["realized_pnl"] or position.get("realized_pnl", Decimal("0"))
    today_pnl = fill_statistics["today_pnl"]
    total_pnl = total_realized_pnl + unrealized_pnl

    size = safe_int(position.get("size", 0))
    direction = "LONG" if size > 0 else ("SHORT" if size < 0 else "FLAT")

    # Fallback to bot state if position or price is missing from API
    entry_price = position.get("entry_price") or decimal_value(state.get("entry_price"))

    return {
        "status": "ok",
        "symbol": SYMBOL,
        "bot_running": bot_running,
        "current_price": json_number(current_price),
        "balance": json_number(balance_data.get("balance")),
        "available_balance": json_number(balance_data.get("available_balance")),
        "balance_asset": balance_data.get("asset"),
        "position": {
            "direction": direction,
            "size": size,
            "entry_price": json_number(entry_price),
            "stop_loss": json_number(stop_loss),
            "unrealized_pnl": json_number(unrealized_pnl)
        },
        "entry_price": json_number(entry_price),
        "stop_loss": json_number(stop_loss),
        "unrealized_pnl": json_number(unrealized_pnl),
        "today_pnl": json_number(today_pnl),
        "total_pnl": json_number(total_pnl),
        "statistics": {
            "total_trades": fill_statistics["total_trades"],
            "winning_trades": fill_statistics["winning_trades"],
            "losing_trades": fill_statistics["losing_trades"],
            "win_rate": fill_statistics["win_rate"],
            "today_pnl": json_number(today_pnl),
            "total_pnl": json_number(total_pnl)
        },
        "trades": [serialize_trade(t) for t in fill_statistics["trades"]],
        "diagnostics": {
            "api_credentials_loaded": bool(API_KEY and API_SECRET),
            "base_url": BASE_URL,
            "symbol": SYMBOL,
            "product_id": product_id,
            "product_found": bool(product),
            "state_file": STATE_FILE,
            "state_file_exists": os.path.exists(STATE_FILE),
            "bot_file_exists": os.path.exists(BOT_FILE),
            "fill_count": len(fills),
            "errors": errors
        }
    }


# ============================================================
# ENDPOINTS
# ============================================================

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    try:
        return jsonify(build_dashboard())
    except Exception as exc:
        return jsonify({"status": "error", "bot_running": is_bot_running(), "error": str(exc)}), 500


@app.route("/api/debug", methods=["GET"])
def debug():
    result = {
        "status": "ok",
        "symbol": SYMBOL,
        "base_url": BASE_URL,
        "api_credentials_loaded": bool(API_KEY and API_SECRET),
        "api_key_length": len(API_KEY),
        "api_secret_loaded": bool(API_SECRET),
        "bot_running": is_bot_running(),
        "bot_file_exists": os.path.exists(BOT_FILE),
        "state_file": STATE_FILE,
        "state_file_exists": os.path.exists(STATE_FILE),
        "tests": {}
    }

    try:
        product = get_product()
        result["tests"]["product"] = {
            "ok": True,
            "id": product.get("id"),
            "symbol": product.get("symbol"),
            "contract_value": json_number(decimal_value(product.get("contract_value")))
        }
    except Exception as exc:
        result["tests"]["product"] = {"ok": False, "error": str(exc)}

    try:
        ticker = get_ticker()
        price = get_current_price()
        result["tests"]["ticker"] = {"ok": True, "price": json_number(price), "raw": ticker}
    except Exception as exc:
        result["tests"]["ticker"] = {"ok": False, "error": str(exc)}

    try:
        balance = get_balance_data()
        result["tests"]["balance"] = {
            "ok": True,
            "balance": json_number(balance.get("balance")),
            "available_balance": json_number(balance.get("available_balance")),
            "asset": balance.get("asset")
        }
    except Exception as exc:
        result["tests"]["balance"] = {"ok": False, "error": str(exc)}

    try:
        p_id = CACHED_PRODUCT_ID or get_product().get("id")
        if not p_id:
            raise RuntimeError("Product ID not found.")
        position = get_position_data(p_id)
        result["tests"]["position"] = {
            "ok": True,
            "product_id": p_id,
            "size": position.get("size"),
            "entry_price": json_number(position.get("entry_price")),
            "realized_pnl": json_number(position.get("realized_pnl"))
        }
    except Exception as exc:
        result["tests"]["position"] = {"ok": False, "error": str(exc)}

    return jsonify(result)


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "XAUTUSD Dashboard API",
        "status": "ok",
        "dashboard_endpoint": "/api/dashboard",
        "health_endpoint": "/api/health",
        "debug_endpoint": "/api/debug"
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
